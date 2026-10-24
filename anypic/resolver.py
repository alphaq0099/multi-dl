"""Resolve a group of candidate URLs (same underlying image) to the best one.

Strategy: probe up to ``max_candidates`` URLs (ranged GET, first ~128 KB),
read the *actual* pixel dimensions with Pillow, and keep the largest. A
rewritten "original" URL only wins if it is genuinely bigger — that is what
guarantees original dimensions instead of thumbnails.
"""
from __future__ import annotations

import io
import logging

from PIL import Image, UnidentifiedImageError

from .config import Config
from .httpclient import Fetcher, FetchError
from .models import ImageCandidate, ProbeInfo, ResolvedImage
from .rules import rewrite_variants

log = logging.getLogger("anypic.resolve")

PROBE_BYTES = 131_072  # headers + usually enough for JPEG SOF / PNG IHDR

# A verified image this large is almost certainly the original; stop probing.
GOOD_ENOUGH_AREA = 12_000_000


def _sniff(data: bytes) -> tuple[int, int, str | None]:
    """Return (width, height, format) from a (possibly partial) image body."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size[0], im.size[1], im.format
    except Exception as e:  # noqa: BLE001 — truncated/broken payloads raise various errors
        log.debug("pillow sniff failed: %s", e)
        return 0, 0, None


def _guess_content_type(data: bytes, fmt: str | None) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
    if fmt:
        return f"image/{fmt.lower()}"
    return ""


class Resolver:
    def __init__(self, cfg: Config, fetcher: Fetcher):
        self.cfg = cfg
        self.fetcher = fetcher

    # ------------------------------------------------------------------ probing
    def probe(self, url: str, referer: str | None = None) -> ProbeInfo:
        info = ProbeInfo(url=url)
        try:
            data = self.fetcher.fetch_bytes(url, referer=referer, max_bytes=PROBE_BYTES)
        except FetchError as e:
            info.note = str(e)
            return info

        ctype = ""
        try:
            head = self.fetcher.head(url, referer=referer)
            if head is not None:
                info.length = int(head.headers.get("Content-Length") or 0)
                ctype = (head.headers.get("Content-Type") or "").lower()
        except Exception:  # noqa: BLE001
            pass

        w, h, fmt = _sniff(data)
        info.width, info.height = w, h
        info.ok = True
        if not ctype or "octet-stream" in ctype:
            ctype = _guess_content_type(data, fmt)
        info.content_type = ctype or ("image/*" if fmt else "")
        info.note = fmt or ""
        return info

    @staticmethod
    def _is_image(info: ProbeInfo) -> bool:
        return info.content_type.startswith("image/") and info.content_type != "image/*"

    # ----------------------------------------------------------------- resolving
    def resolve_group(self, group_key: str, candidates: list[ImageCandidate],
                      referer: str | None = None) -> ResolvedImage:
        """Probe the most promising URLs for one image group; keep the largest."""
        resolved = ResolvedImage(
            group_key=group_key,
            source=max(candidates, key=lambda c: c.score).source,
            declared_width=max((c.declared_width or 0 for c in candidates), default=0) or None,
        )

        ordered: list[str] = []
        seen: set[str] = set()
        # literal candidates, most promising first (declared width, then score)
        for c in sorted(candidates, key=lambda c: (-(c.declared_width or 0), -c.score)):
            if c.url not in seen:
                seen.add(c.url)
                ordered.append(c.url)
        # rewritten "original" variants of each
        for c in candidates:
            for v in rewrite_variants(c.url):
                if v not in seen:
                    seen.add(v)
                    ordered.append(v)

        best: ProbeInfo | None = None
        for url in ordered[: self.cfg.max_candidates]:
            info = self.probe(url, referer=referer)
            resolved.candidates.append(info)
            log.debug("probe %s -> ok=%s %dx%d len=%s ct=%s note=%s",
                      url, info.ok, info.width, info.height, info.length,
                      info.content_type, info.note)
            if not info.ok or not self._is_image(info):
                continue
            if best is None or info.area > best.area or (
                info.area == best.area and (info.length or 0) > (best.length or 0)
            ):
                best = info
            if best.area >= GOOD_ENOUGH_AREA:
                break

        resolved.best = best
        return resolved
