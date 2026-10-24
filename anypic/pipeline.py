"""URL processing pipeline — the tier orchestration.

    direct image URL  → generic engine (with rewrite upgrades)
    page URL          → gallery-dl tier → static engine → browser engine

Every page ends with a :class:`PageResult` that the CLI turns into a
summary row. Designed so the whole function is importable/reusable (a
future MULTI_DOWNLOADER backend can call ``process_url`` directly).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import (Progress, SpinnerColumn, BarColumn, TextColumn,
                           TimeElapsedColumn, TransferSpeedColumn)

from . import extract, gdl_bridge, renderer
from .config import Config
from .downloader import Downloader, _page_slug
from .httpclient import Fetcher, FetchError
from .models import DownloadResult, ImageCandidate, PageResult, ProbeInfo
from . import report as _report
from .resolver import Resolver
from .rules import DIRECT_IMAGE_RE, canonical_key, is_junk

log = logging.getLogger("anypic.pipeline")


def is_direct_image(url: str, fetcher: Fetcher) -> bool:
    """True when the URL itself is an image file, not a page.

    The extension is only a hint — pages like ``/wiki/File:Example.jpg``
    end in .jpg but are HTML, so an authoritative HEAD Content-Type wins
    whenever the server answers.
    """
    ext_hint = DIRECT_IMAGE_RE.search(url.split("?")[0]) is not None
    # bounded probe: a dead/hanging host must not stall the whole run here
    head = fetcher.head(url, timeout=10, max_engines=1)
    if head is not None and head.status_code < 400:
        ct = (head.headers.get("Content-Type") or "").lower()
        if ct.startswith("image/"):
            return True
        if ct.startswith(("text/html", "application/xhtml", "text/", "application/json")):
            return False
        if ct:
            return False
    return ext_hint


def _resolve_groups(groups: dict[str, list[ImageCandidate]], resolver: Resolver,
                    referer: str, workers: int):
    resolved = []
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  TimeElapsedColumn(), console=console, transient=True) as prog:
        task = prog.add_task("Probing originals…", total=len(groups))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(resolver.resolve_group, key, cands, referer): key
                for key, cands in groups.items()
            }
            for fut in as_completed(futures):
                try:
                    resolved.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    log.debug("resolve failed for %s: %s", futures[fut], e)
                prog.advance(task, 1)
    return resolved


def _filter_by_size(resolved, cfg: Config) -> tuple[list, list, list]:
    """Split into (kept, too_small, unverified); relax if nothing is kept."""
    kept, too_small, unverified = [], [], []
    for r in resolved:
        if r.best is None:
            continue
        w, h = r.best.width, r.best.height
        if w == 0 or h == 0:
            unverified.append(r)  # e.g. AVIF Pillow can't decode — keep, rank last
        elif w < cfg.min_width and h < cfg.min_height:
            too_small.append(r)
        else:
            kept.append(r)

    if not kept:
        acceptable = [
            r for r in too_small
            if r.best.width >= cfg.relaxed_min and r.best.height >= cfg.relaxed_min
        ]
        if acceptable:
            acceptable.sort(key=lambda r: r.best.area, reverse=True)
            rescued = acceptable[: cfg.relaxed_keep]
            kept = rescued
            too_small = [r for r in too_small if r not in rescued]
            log.info("relaxed size filter: kept %d small images", len(rescued))
    return kept, too_small, unverified


def _process_candidates(url: str, tier: str, candidates: list[ImageCandidate], cfg: Config,
                        resolver: Resolver, downloader: Downloader,
                        sub_dir: str | None = None) -> PageResult:
    result = PageResult(url=url, tier=tier)

    groups: dict[str, list[ImageCandidate]] = {}
    for c in candidates:
        groups.setdefault(canonical_key(c.url), []).append(c)

    # most promising groups first (max extraction score in the group)
    ordered_groups = dict(sorted(
        groups.items(),
        key=lambda kv: max(c.score for c in kv[1]), reverse=True,
    )[: cfg.max_images_per_page])
    result.found = len(ordered_groups)

    resolved = _resolve_groups(ordered_groups, resolver, url, cfg.workers)
    kept, too_small, unverified = _filter_by_size(resolved, cfg)

    for r in too_small:
        result.results.append(DownloadResult(
            url=r.best.url, status="too-small",
            width=r.best.width, height=r.best.height,
            note=f"below {cfg.min_width}x{cfg.min_height}"))
    if unverified:
        kept.extend(unverified)  # dimensions unknown; download and see

    probes = [r.best for r in sorted(kept, key=lambda r: -(r.best.area or 0))]
    if cfg.best_only and len(probes) > cfg.best_only:
        result.notes.append(f"best-only: kept top {cfg.best_only} of {len(probes)} images")
        probes = probes[: cfg.best_only]
    log.info("%s: %d groups, %d kept after size filter", url, len(groups), len(probes))

    # per-file bars (with Content-Length totals) appear under an overall bar
    # whose speed is measured on actual bytes transferred
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(bar_width=None), TransferSpeedColumn(), TimeElapsedColumn(),
                  console=console, transient=True) as prog:
        task = prog.add_task(f"Downloading {len(probes)} file(s)…", total=None)
        result.results.extend(
            downloader.download_many(url, probes, task_id=task, progress=prog, sub_dir=sub_dir))
    return result


def _clean_candidates(cands: list[ImageCandidate], cfg: Config) -> list[ImageCandidate]:
    return [c for c in cands if not is_junk(c.url, cfg.include_svg)]


def process_url(
    url: str,
    cfg: Config,
    fetcher: Fetcher,
    downloader: Downloader,
    resolver: Resolver,
    *,
    use_gallery_dl: bool = True,
    force_browser: bool = False,
    allow_browser: bool = True,
) -> PageResult:
    """Run one URL through the tier pipeline."""
    result = PageResult(url=url)

    # ---------------------------------------------------------------- direct file
    try:
        direct = is_direct_image(url, fetcher)
    except Exception:  # noqa: BLE001
        direct = DIRECT_IMAGE_RE.search(url.split("?")[0]) is not None

    if direct and not (use_gallery_dl and cfg.gallery_dl.get("handle_direct", False)):
        _report.console.print(f"[cyan]→[/] {url} [dim](direct image)[/]")
        cands = [ImageCandidate(url=url, source="direct", score=100, page_url=url)]
        return _process_candidates(url, "direct", cands, cfg, resolver, downloader)

    # ---------------------------------------------------------------- gallery-dl
    if use_gallery_dl:
        handled, msg = gdl_bridge.run(url, cfg)
        result.notes.append(msg)
        if handled:
            result.tier = "gallery-dl"
            _report.console.print(f"[cyan]→[/] {url} [green]gallery-dl[/] — {msg}")
            return result
        log.info("gallery-dl passed on %s (%s)", url, msg)

    # ---------------------------------------------------------------- static tier
    candidates: list[ImageCandidate] = []
    static_ok = False
    if not force_browser:
        _report.console.print(f"[cyan]→[/] {url}")
        try:
            resp = fetcher.get(url)
            if resp.status_code < 400 and "html" in (resp.headers.get("Content-Type") or "html").lower():
                candidates = _clean_candidates(
                    extract.extract_candidates(url, resp.text), cfg)
                static_ok = True
            elif resp.status_code < 400 and direct:
                # HEAD lied but the body is an image after all
                cands = [ImageCandidate(url=url, source="direct", score=100, page_url=url)]
                return _process_candidates(url, "direct", cands, cfg, resolver, downloader)
            else:
                result.notes.append(f"static fetch: HTTP {resp.status_code}")
        except FetchError as e:
            result.notes.append(f"static fetch failed: {e}")

    static_result: PageResult | None = None
    if static_ok and candidates:
        static_result = _process_candidates(url, "static", candidates, cfg, resolver, downloader,
                                            sub_dir=_page_slug(url))

    good_enough = static_result is not None and (
        static_result.downloaded or static_result.skipped
    )
    if good_enough:
        static_result.notes.extend(result.notes)
        return static_result

    # ---------------------------------------------------------------- browser tier
    if allow_browser and cfg.browser.get("enabled", True):
        reason = "forced" if force_browser else (
            "static engine found nothing usable" if static_ok else "static fetch blocked/failed")
        _report.console.print(f"[yellow]  ↳ browser fallback[/] ({reason})")
        bcands, err = renderer.render_page(url, cfg)
        if err:
            result.notes.append(f"browser: {err}")
            _report.console.print(f"[red]  ↳ browser failed:[/] {err}")
        bcands = _clean_candidates(bcands, cfg)
        if bcands:
            browser_result = _process_candidates(url, "browser", bcands, cfg, resolver, downloader,
                                                 sub_dir=_page_slug(url))
            if static_result is not None:
                browser_result.notes.extend(static_result.notes)
            browser_result.notes.append(reason)
            return browser_result

    if static_result is not None:
        static_result.notes.extend(result.notes)
        return static_result

    result.notes.append("no images found by any engine")
    return result
