"""Thumbnail → original URL rewriting, junk filtering and grouping.

Two things happen here:

* ``rewrite_variants(url)`` produces alternative URLs that *might* be the
  original-resolution version of a thumbnail (WordPress ``-1024x768``
  suffixes, Shopify ``_600x600``, Blogger ``/s320/``, Reddit preview links,
  ...). Nothing here is trusted blindly — the resolver downloads and
  measures each variant with Pillow and keeps the biggest.
* ``canonical_key(url)`` maps every size-variant of the same image to one
  identity so ``pic-150x150.jpg`` and ``pic.jpg`` found on the same page are
  treated as *one* image instead of two downloads.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

log = logging.getLogger("anypic.rules")

EXT = r"(?:jpe?g|png|webp|gif|avif|bmp|tiff?)"
# an image extension, optionally chained (".png.webp"), at end or before ?/#
EXT_TAIL = rf"\.{EXT}(?:\.{EXT})?(?:[?#]|$)"
IMAGE_EXT_RE = re.compile(rf"{EXT_TAIL}", re.I)
DIRECT_IMAGE_RE = re.compile(rf"{EXT_TAIL}", re.I)


@dataclass
class Rule:
    name: str
    pattern: re.Pattern
    replacement: str | Callable  # str or fn(match) -> str
    hosts: tuple[str, ...] = field(default=())  # substring match against netloc


def _wikimedia(m: re.Match) -> str:
    # /thumb/a/ab/File.jpg/300px-File.jpg -> /a/ab/File.jpg
    return m.group(1)


def _substack(m: re.Match) -> str:
    # substackcdn.com/image/fetch/w_1456,.../https%3A%2F%2Foriginal... -> original
    return unquote(m.group(1))


RULES: list[Rule] = [
    # ------------------------------------------------------------- host-specific
    Rule("twitter-name", re.compile(r"([?&]name=)\w+"), r"\g<1>orig", ("twimg.com",)),
    Rule("twitter-legacy-size",
         re.compile(rf"_(?:normal|bigger|mini|small|medium|large)(?={EXT_TAIL})", re.I),
         "", ("twimg.com",)),
    Rule("reddit-preview",
         re.compile(r"^https?://(?:external-)?preview\.redd\.it/([^?/]+)\?.*$"),
         r"https://i.redd.it/\1", ("redd.it",)),
    Rule("imgur-thumb-letter",
         re.compile(rf"(?<=[a-z0-9])[sthmlb](?={EXT_TAIL})", re.I),
         "", ("imgur.com",)),
    Rule("blogger-size",
         re.compile(r"/(?:s|w|h)\d{1,5}(?:-[a-z0-9]+)*/"),
         "/s0/", ("blogspot.com", "googleusercontent.com", "ggpht.com")),
    Rule("googleusercontent-tail",
         re.compile(r"=(?:[swhc][-_\d]+)+$"), "", ("googleusercontent.com", "ggpht.com")),
    Rule("squarespace-format", re.compile(r"([?&]format=)\d+w"), r"\g<1>original", ("squarespace",)),
    Rule("deviantart-intermediary", re.compile(r"/intermediary(?=/)"), "", ("wixmp.com", "deviantart")),
    Rule("deviantart-v1fit", re.compile(r"/v1/(?:fit|fill|crop)/[^/]+/"), "/", ("wixmp.com", "deviantart")),
    Rule("discord-size", re.compile(r"([?&]size=)\d+"), r"\g<1>4096", ("discordapp.com", "discord.com")),
    Rule("pixhost", re.compile(r"^(https?://)t(\d*)\.pixhost\.to/thumbs/"), r"\1img\2.pixhost.to/images/", ("pixhost.to",)),
    Rule("imgbox-host", re.compile(r"^(https?://)thumbs(\d*)\.imgbox\.com/"), r"\1images\2.imgbox.com/", ("imgbox.com",)),
    Rule("imgbox-suffix", re.compile(rf"_[a-z](?={EXT_TAIL})", re.I), "", ("imgbox.com",)),
    Rule("bunkr-thumb", re.compile(rf"\.(?:md|th|sm|lg)(?={EXT_TAIL})", re.I), "", ("bunkr.",)),
    Rule("wikimedia-thumb",
         re.compile(rf"/thumb(/[^/]+/[^/]+/[^/]+\.{EXT})/[^/]+$", re.I), _wikimedia, ("wikimedia.org",)),
    Rule("pinterest-size", re.compile(r"^(https?://[^/]+\.pinimg\.com)/\d+x\d*/"), r"\1/originals/", ("pinimg.com",)),
    Rule("medium-miro", re.compile(r"(max|resize:fit)([:/])\d{2,4}"), r"\g<1>\g<2>4000", ("medium.com", "medium.global.ssl.fastly.net")),
    Rule("tumblr-size", re.compile(rf"_\d{{2,4}}(?={EXT_TAIL})"), "_raw", ("tumblr.com",)),
    Rule("cloudinary-transform",
         re.compile(r"/upload/(?:[a-z]{1,3}_\d+(?:,[a-z]{1,3}_\d+)*)+/"), "/upload/", ("cloudinary.com",)),
    Rule("instagram-size", re.compile(r"/s\d{2,4}x\d{2,4}(?:_[a-z0-9]+)?/?"), "/", ("cdninstagram.com", "fbcdn.net")),
    Rule("ebay-size", re.compile(rf"/s-l\d{{2,4}}(?={EXT_TAIL})", re.I), "", ("ebayimg.com", "ebay.com")),
    Rule("amazon-size",
         re.compile(rf"\._[A-Z0-9]+_(?={EXT_TAIL})"), "", ("amazon.com",)),
    Rule("substack-embed",
         re.compile(r"^https?://substackcdn\.com/image/(?:fetch|resize)/[^/]+/(https?.+)$"),
         _substack, ("substackcdn.com",)),
    Rule("wp-scaled", re.compile(rf"-scaled(?={EXT_TAIL})", re.I), "", ()),

    # ------------------------------------------------------------------- generic
    Rule("vbulletin-thumb", re.compile(r"[?&]thumb=1"), "", ()),
    Rule("size-suffix-dash",
         re.compile(rf"-\d{{2,4}}x\d{{2,4}}(?={EXT_TAIL})"), "", ()),
    Rule("size-suffix-underscore",
         re.compile(rf"_\d{{2,4}}x\d{{2,4}}(?:@\d+x)?(?={EXT_TAIL})"), "", ()),
    Rule("path-size-segment", re.compile(r"/\d{2,4}x\d{2,4}/"), "/", ()),
    Rule("thumb-token-segment",
         re.compile(rf"/(?:thumbs?|thumbnail|small|preview|resized)/(?=(?:[^/]+/)?[^/]*{EXT_TAIL})"),
         "/", ()),
]

# Query params that only control rendered size/quality — safe to drop when
# hunting for the original (and ignored when computing the canonical key).
SIZE_QUERY_KEYS = {
    "w", "width", "h", "height", "size", "d", "dpr", "fit", "crop", "resize",
    "q", "quality", "auto", "fm", "format", "maxwidth", "maxheight", "downsize",
    "widths", "sizes", "resize-width", "resize-height", "scale", "wsize",
}

_JUNK_TOKEN_RE = re.compile(
    r"(?:^|[/.@_-])(?:sprite|sprites|logo|logos|icon|icons|favicon|avatar|avatars|"
    r"emoji|emojis|spacer|1x1|pixel|blank|placeholder|loading|spinner|badge|"
    r"analytics|beacon|tracking|ad_|ads?/|adv?[-_/]|banner|watermark)(?:[/.@_-]|$)",
    re.I,
)
_TINY_SIZE_RE = re.compile(r"/\d{1,2}x\d{1,2}(?:[/.]|$)")


def host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    if not hosts:
        return True
    netloc = urlsplit(url).netloc.lower()
    return any(h in netloc for h in hosts)


def strip_size_query(url: str) -> str:
    """Remove size/quality-only query params (w=, width=, resize=..., ...)."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in SIZE_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _apply_rule(rule: Rule, url: str) -> str:
    if not host_matches(url, rule.hosts):
        return url
    if isinstance(rule.replacement, str):
        return rule.pattern.sub(rule.replacement, url)
    return rule.pattern.sub(rule.replacement, url)


def rewrite_variants(url: str, max_variants: int = 5) -> list[str]:
    """Generate possible original-resolution variants of a thumbnail URL.

    Does NOT include the input URL itself. Chained application (a rule
    output feeding later rules) is explored breadth-first and capped.
    """
    out: list[str] = []
    seen = {url}
    frontier = [url]
    for _depth in range(3):
        next_frontier = []
        for u in frontier:
            u2 = strip_size_query(u)
            if u2 != u and u2 not in seen:
                seen.add(u2)
                out.append(u2)
                next_frontier.append(u2)
            for rule in RULES:
                try:
                    v = _apply_rule(rule, u)
                except re.error as e:  # pragma: no cover
                    log.debug("rule %s failed on %s: %s", rule.name, u, e)
                    continue
                if v != u and v not in seen:
                    seen.add(v)
                    out.append(v)
                    next_frontier.append(v)
                    if len(out) >= max_variants:
                        return out
        frontier = next_frontier
        if not frontier:
            break
    return out


def canonical_key(url: str) -> str:
    """Identity of the underlying image, independent of size variants.

    Size rules are applied, size-only query params dropped, and the trailing
    format extension (including chained ``.png.webp``) removed, so every
    variant of one image collapses to a single key.
    """
    u = strip_size_query(url)
    for rule in RULES:
        u = _apply_rule(rule, u)
    u = strip_size_query(u)
    parts = urlsplit(u)
    m = re.search(rf"\.{EXT}(?:\.{EXT})?$", parts.path)
    path = parts.path[: m.start()] if m else parts.path
    query = "&".join(
        f"{k}={v}" for k, v in sorted(parse_qsl(parts.query, keep_blank_values=True))
    )
    return f"{parts.netloc.lower()}|{path}|{query}"


def is_junk(url: str, include_svg: bool = False) -> bool:
    """Heuristic junk detection: trackers, UI chrome, avatars, placeholders."""
    u = url.strip()
    low = u.lower()
    if low.startswith(("data:", "javascript:", "blob:", "about:", "#")):
        return True
    path = urlsplit(u).path.lower()
    if path.endswith(".svg") and not include_svg:
        return True
    if not IMAGE_EXT_RE.search(path):
        # Allow extension-less CDN URLs through; probing will validate.
        pass
    if _JUNK_TOKEN_RE.search(path):
        return True
    if _TINY_SIZE_RE.search(path):
        return True
    return False
