"""Tier 3: render the page in headless Chromium and harvest images.

Used only when the static engine found nothing usable or was blocked.
Beyond DOM harvesting (which sees lazy-loaded ``currentSrc`` after
scrolling), every network response with an ``image/*`` content type is
captured — this catches images pulled in via JSON APIs that never appear
in the HTML at all.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .config import Config
from .models import ImageCandidate

log = logging.getLogger("anypic.render")

_DOM_HARVEST_JS = """
() => {
  const out = [];
  const push = (src, kind, w, h) => {
    if (!src) return;
    out.push({src: String(src), kind, w: w || 0, h: h || 0});
  };
  for (const img of document.images) {
    push(img.currentSrc || img.src, 'img', img.naturalWidth, img.naturalHeight);
    if (img.srcset) push(img.srcset, 'srcset', 0, 0);
    for (const attr of ['data-src','data-original','data-lazy-src','data-full',
                        'data-large-file','data-actualsrc','data-zoom-image',
                        'data-srcset','data-image','data-big','data-hi-res-src']) {
      const v = img.getAttribute(attr);
      if (v) push(v, 'data', 0, 0);
    }
  }
  for (const a of document.links) {
    if (/\\.(jpe?g|png|webp|gif|avif|bmp)(\\?|$)/i.test(a.href)) push(a.href, 'anchor', 0, 0);
  }
  for (const el of document.querySelectorAll('[style*="url("]')) {
    const m = (el.getAttribute('style') || '').match(/url\\((['"]?)(.*?)\\1\\)/);
    if (m) push(m[2], 'css', 0, 0);
  }
  return out;
}
"""

_MISSING_BROWSER_HINTS = ("executable doesn't exist", "browserType.launch", "install chromium",
                          "Looks like Playwright was just installed")


def _absolutize(page_url: str, raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw or raw.startswith(("data:", "blob:", "javascript:", "#")):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    from urllib.parse import urljoin
    url = urljoin(page_url, raw)
    return url if url.startswith(("http://", "https://")) else None


def render_page(url: str, cfg: Config) -> tuple[list[ImageCandidate], str | None]:
    """Load *url* in headless Chromium.

    Returns (candidates, error_message) — error_message is None on success.
    """
    bcfg = cfg.browser
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], "playwright not installed"

    candidates: list[ImageCandidate] = []
    sniffed: set[str] = set()
    error: str | None = None

    try:
        with sync_playwright() as pw:
            proxy = cfg.get("proxy")
            browser = pw.chromium.launch(
                headless=True,
                proxy={"server": proxy} if proxy else None,
            )
            try:
                context = browser.new_context(
                    user_agent=cfg.user_agent,
                    viewport={"width": 1920, "height": 1080},
                    accept_downloads=False,
                )
                _block_nonessential(context)
                page = context.new_page()

                def on_response(resp):
                    try:
                        ct = (resp.headers or {}).get("content-type", "").lower()
                        if ct.startswith("image/") and resp.status == 200:
                            u = resp.url
                            if u and u not in sniffed:
                                sniffed.add(u)
                                candidates.append(ImageCandidate(
                                    url=u, source="response", score=38, page_url=url))
                    except Exception:  # noqa: BLE001
                        pass

                page.on("response", on_response)

                # connection resets on filtered/Cloudflare sites are
                # intermittent — a fresh attempt often gets through
                attempts = 3
                for i in range(1, attempts + 1):
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=bcfg.get("timeout", 45000))
                        break
                    except Exception as ge:  # noqa: BLE001
                        if i == attempts or "net::ERR_" not in str(ge):
                            raise RuntimeError(
                                f"page unreachable after {i} connection attempt(s): {str(ge).splitlines()[0]}"
                            ) from ge
                        log.debug("goto attempt %d/%d failed (%s) — retrying",
                                  i, attempts, str(ge).splitlines()[0])
                        page.wait_for_timeout(2500)

                # if an anti-bot challenge is showing, a real browser usually
                # solves it on its own; wait for the real page to appear
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    title = (page.title() or "").lower()
                    if not any(m in title for m in (
                            "just a moment", "attention required", "checking your browser",
                            "verifying you are human", "please wait")):
                        break
                    log.debug("challenge page detected, waiting for auto-solve")
                    page.wait_for_timeout(1000)

                # scroll to trigger lazy loading
                steps = bcfg.get("scroll_steps", 12)
                wait = bcfg.get("scroll_wait", 350)
                for _ in range(steps):
                    page.evaluate(
                        "() => window.scrollBy(0, Math.ceil(document.body.scrollHeight"
                        f" / {steps}))"
                    )
                    page.wait_for_timeout(wait)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(500)

                dom: list[dict[str, Any]] = page.evaluate(_DOM_HARVEST_JS) or []
                for item in dom:
                    src = item.get("src") or ""
                    kind = item.get("kind") or "dom"
                    if kind == "srcset":
                        for part in src.split(","):
                            bits = part.strip().split()
                            if not bits:
                                continue
                            u = _absolutize(url, bits[0])
                            w = int(bits[1][:-1]) if len(bits) > 1 and bits[1].endswith("w") else 0
                            if u:
                                candidates.append(ImageCandidate(
                                    url=u, source="dom-srcset", score=30,
                                    declared_width=w or None, page_url=url))
                    else:
                        u = _absolutize(url, src)
                        if u:
                            score = {"anchor": 50, "data": 35, "img": 22, "css": 12}.get(kind, 20)
                            candidates.append(ImageCandidate(
                                url=u, source=f"dom-{kind}", score=score,
                                declared_width=(item.get("w") or None),
                                declared_height=(item.get("h") or None),
                                page_url=url))
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        error = msg[:300]
        if any(h in msg.lower() for h in _MISSING_BROWSER_HINTS):
            error = ("Chromium not installed for Playwright. "
                     "Run: playwright install chromium")
        log.debug("render failed for %s: %s", url, msg)

    # dedup by URL keeping the highest-scoring / dimension-bearing entry
    best: dict[str, ImageCandidate] = {}
    for c in candidates:
        cur = best.get(c.url)
        if cur is None or (c.score, c.declared_width or 0) > (cur.score, cur.declared_width or 0):
            best[c.url] = c
    return list(best.values()), error


def _block_nonessential(context) -> None:
    """Abort font/css/media requests so rendering is faster; keep images+XHR."""
    from playwright.sync_api import Route

    blocked_types = {"font", "stylesheet", "media"}
    extra = ("google-analytics.com", "googletagmanager.com", "doubleclick.net",
             "facebook.com/tr", "scorecardresearch.com", "hotjar.com")

    def handler(route: Route):
        if route.request.resource_type in blocked_types:
            route.abort()
            return
        url = route.request.url.lower()
        if any(h in url for h in extra):
            route.abort()
            return
        route.continue_()

    try:
        context.route("**/*", handler)
    except Exception as e:  # noqa: BLE001
        log.debug("route blocking setup failed: %s", e)
