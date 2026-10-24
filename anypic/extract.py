"""Extract image candidates from raw HTML.

Everything found here is a *candidate* — the resolver later probes each one
and keeps the largest actual pixels, so extraction can be generous.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import ImageCandidate
from .rules import DIRECT_IMAGE_RE, IMAGE_EXT_RE

log = logging.getLogger("anypic.extract")

# data-* attributes that commonly carry the full-resolution URL
LAZY_ATTRS = (
    "data-src", "data-original", "data-lazy-src", "data-lazy", "data-full",
    "data-full-url", "data-full-src", "data-large-file", "data-large",
    "data-large-src", "data-actualsrc", "data-actualsrcset", "data-hi-res-src",
    "data-highres-src", "data-high-src", "data-zoom-image", "data-image",
    "data-image-hd", "data-big", "data-href", "data-url", "data-srcset",
)

META_SELECTORS = (
    'meta[property="og:image"]',
    'meta[property="og:image:url"]',
    'meta[property="og:image:secure_url"]',
    'meta[name="twitter:image"]',
    'meta[name="twitter:image:src"]',
    'meta[itemprop="image"]',
    'link[rel="image_src"]',
)

_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")\s]+)", re.I)
_EMBEDDED_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]{3,300}\.(?:jpe?g|png|webp|gif|avif|bmp)(?:\?[^\s\"'<>]{0,200})?",
    re.I,
)


def _abs(page_url: str, raw: str) -> str | None:
    if not raw:
        return None
    raw = html_mod.unescape(raw).strip()
    if not raw or raw.startswith(("data:", "javascript:", "blob:")):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        return urljoin(page_url, raw)
    except ValueError:
        return None


def _looks_like_image_url(url: str) -> bool:
    return bool(DIRECT_IMAGE_RE.search(url))


def _parse_srcset(page_url: str, srcset: str) -> list[tuple[str, int | None]]:
    """Return (url, declared_width) pairs; ``x`` descriptors map to width=None."""
    out = []
    for part in re.split(r",\s*(?=\S+\s+(?:\d+(?:\.\d+)?[wx]|https?:)|\S+$)", srcset or ""):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        width = None
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                pass
        absu = _abs(page_url, url)
        if absu:
            out.append((absu, width))
    return out


def extract_candidates(page_url: str, html: str) -> list[ImageCandidate]:
    """Harvest every plausible image URL from a page."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, ImageCandidate] = {}

    def add(raw_url: str, source: str, score: int,
            width: int | None = None, height: int | None = None) -> None:
        url = _abs(page_url, raw_url) if raw_url else None
        if not url or not url.startswith(("http://", "https://")):
            return
        cur = found.get(url)
        if cur is None or cur.score < score:
            entry = cur or ImageCandidate(url=url, page_url=page_url)
            entry.source = source
            entry.score = score
            if width:
                entry.declared_width = max(entry.declared_width or 0, width)
            if height:
                entry.declared_height = max(entry.declared_height or 0, height)
            found[url] = entry
        elif cur is not None and (width or height):
            cur.declared_width = max(cur.declared_width or 0, width or 0) or None
            cur.declared_height = max(cur.declared_height or 0, height or 0) or None

    # --- <img> tags -----------------------------------------------------------
    for img in soup.find_all("img"):
        width = int(img.get("width")) if str(img.get("width") or "").isdigit() else None
        height = int(img.get("height")) if str(img.get("height") or "").isdigit() else None

        src = img.get("src") or ""
        if src and not src.endswith((".svg", ".svgz")):
            add(src, "img-src", 15, width, height)

        for attr in LAZY_ATTRS:
            val = img.get(attr)
            if not val:
                continue
            hi_res = attr in ("data-original", "data-full", "data-full-url", "data-full-src",
                              "data-large-file", "data-hi-res-src", "data-highres-src",
                              "data-zoom-image", "data-image-hd", "data-actualsrc")
            if "srcset" in attr:
                for u, w in _parse_srcset(page_url, val):
                    add(u, f"img-{attr}", 35 if hi_res else 28, w)
            else:
                add(val, f"img-{attr}", 40 if hi_res else 25, width, height)

        for ss in (img.get("srcset"), img.get("data-lazy-srcset")):
            for u, w in _parse_srcset(page_url, ss or ""):
                add(u, "img-srcset", 30, w)

    # --- anchors pointing straight at image files (lightbox pattern) ----------
    for a in soup.find_all("a", href=True):
        href = html_mod.unescape(a["href"])
        if not _looks_like_image_url(href):
            continue
        add(href, "anchor-full", 50)

    # --- picture <source> ------------------------------------------------------
    for source in soup.find_all("source"):
        for ss in (source.get("srcset"), source.get("data-srcset")):
            for u, w in _parse_srcset(page_url, ss or ""):
                add(u, "picture-source", 32, w)

    # --- meta tags -------------------------------------------------------------
    for sel in META_SELECTORS:
        for tag in soup.select(sel):
            add(tag.get("content") or tag.get("href") or "", "meta", 45)

    # --- JSON-LD ---------------------------------------------------------------
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for url in _walk_json_images(data):
            add(url, "json-ld", 42)

    # --- inline CSS backgrounds -------------------------------------------------
    for tag in soup.find_all(style=True):
        for m in _CSS_URL_RE.finditer(tag.get("style") or ""):
            add(m.group(1), "css-bg", 12)
    for style in soup.find_all("style"):
        for m in _CSS_URL_RE.finditer(style.get_text() or ""):
            add(m.group(1), "css-bg", 10)

    # --- raw scan for image URLs inside embedded JSON/JS -----------------------
    text = html.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
    for m in _EMBEDDED_URL_RE.finditer(text):
        add(m.group(0), "json", 20)

    return list(found.values())


def _walk_json_images(node, depth: int = 0):
    """Yield image-ish URL strings from parsed JSON(-LD) structures."""
    if depth > 8:
        return
    if isinstance(node, str):
        if node.startswith(("http://", "https://")) and IMAGE_EXT_RE.search(node.split("?")[0]):
            yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in ("image", "image:url", "image:secure_url", "thumbnailUrl", "contentUrl", "url"):
                if isinstance(v, str):
                    yield from _walk_json_images(v, depth + 1)
                elif isinstance(v, list):
                    for item in v[:20]:
                        yield from _walk_json_images(item, depth + 1)
                elif isinstance(v, dict):
                    yield from _walk_json_images(v, depth + 1)
            elif isinstance(v, (dict, list)):
                yield from _walk_json_images(v, depth + 1)
    elif isinstance(node, list):
        for item in node[:50]:
            yield from _walk_json_images(item, depth + 1)


def group_candidates(candidates: list[ImageCandidate], key_fn) -> dict[str, list[ImageCandidate]]:
    """Group candidates by canonical identity; each group = one real image."""
    groups: dict[str, list[ImageCandidate]] = {}
    for c in candidates:
        groups.setdefault(key_fn(c.url), []).append(c)
    return groups
