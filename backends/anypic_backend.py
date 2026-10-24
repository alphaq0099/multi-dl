"""
Adapter backend wrapping anypic-dl (vendored as the ``anypic`` package).

anypic-dl is the generic image-page engine: for URLs no other engine owns it
extracts original-resolution images via a direct-image check, a static HTML
engine (requests/cloudscraper/curl_cffi), and an optional Playwright browser
fallback. Its internal gallery-dl tier is disabled here because the router
already dispatches gallery-dl-supported URLs to the dedicated backend.

Phase-1 integration: anypic-dl is reachable via explicit selection
(``-b anypic-dl`` / GUI dropdown); automatic unknown-URL routing is a
follow-up behind a config flag.
"""

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse, unquote, urlsplit

from backends.base import BaseBackend
from core.exceptions import DownloadFailedError
from core.models import ArchiveEntry, DownloadProgress, DownloadTask, MediaType
from core.workers import run_blocking

# The vendored engine renders its own rich output; MULTI_DOWNLOADER's canvas
# owns the terminal, so point the engine's console at its quiet sink once.
# pipeline/gdl_bridge read report.console at call time, making this race-free.
from anypic import report as _anypic_report
_anypic_report.console = _anypic_report.quiet_console

from anypic.config import load_config as _anypic_load_config
from anypic.downloader import Downloader, StateStore, _page_slug
from anypic.httpclient import Fetcher
from anypic.models import PageResult
from anypic.pipeline import process_url as _anypic_process_url
from anypic.resolver import Resolver


ANYPIC_DATA_DIR = "data/anypic"


class AnyPicBackend(BaseBackend):
    """Generic image-page downloader adapter for MULTI_DOWNLOADER."""

    name: str = "anypic-dl"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._load_modular_config()

    def _load_modular_config(self) -> None:
        """Loads and merges options from configs/anypic-dl.json if present."""
        cfg_path_str = self.config.get("config_file", "configs/anypic-dl.json")
        cfg_path = Path(cfg_path_str)
        if not cfg_path.is_absolute():
            project_root = Path(__file__).resolve().parent.parent
            cfg_path = project_root / cfg_path
        if cfg_path.exists():
            try:
                import json
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if not k.startswith("//") and (k not in self.config or self.config[k] in (None, "")):
                            self.config[k] = v
            except Exception:
                pass

    def can_handle(self, url: str) -> bool:
        """
        Conservative claim: only URLs no other engine owns.

        Requires http(s), rejects every DOMAIN_MAP domain (telegram, terabox,
        lockers, known gallery sites), rejects URLs with a gallery-dl native
        extractor, and rejects anything a non-generic yt-dlp extractor fits.
        """
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        domain = (parsed.netloc or "").lower().split(":")[0]
        if not domain:
            return False

        from core.router import URLRouter
        for mapped in URLRouter.DOMAIN_MAP:
            if domain == mapped or domain.endswith("." + mapped):
                return False

        try:
            import gallery_dl.extractor
            if gallery_dl.extractor.find(url) is not None:
                return False
        except Exception:
            pass

        try:
            from backends.ytdlp_backend import YtDlpBackend
            if YtDlpBackend.can_handle(url):
                return False
        except Exception:
            pass

        return True

    async def extract_info(self, url: str) -> Dict[str, Any]:
        parsed = urlparse(url)
        return {
            "url": url,
            "domain": (parsed.netloc or "").lower(),
            "backend": self.name,
            "can_handle": self.can_handle(url),
        }

    def _build_anypic_config(self, task: DownloadTask, probe_hook=None) -> Any:
        """Bridges the anypic_dl config section + task options into engine Config."""
        overrides: Dict[str, Any] = {
            "output_dir": str(task.output_dir),
            "data_dir": ANYPIC_DATA_DIR,
            "gallery_dl": {"enabled": False},
            # The dispatcher already nests us under downloads/anypic-dl/<host>/;
            # the engine's own domain layer would duplicate it.
            "layout": "flat",
        }
        passthrough = (
            "min_width", "min_height", "relaxed_min", "relaxed_keep",
            "max_images_per_page", "best_only", "workers", "timeout",
            "retries", "max_candidates", "max_bytes", "include_svg",
            "dedup", "user_agent", "proxy",
        )
        for key in passthrough:
            value = self.config.get(key)
            if value is not None and value != "":
                overrides[key] = value

        if probe_hook is not None:
            overrides["progress_hook"] = probe_hook

        proxy = task.options.get("proxy") or self.config.get("proxy")
        if proxy:
            overrides["proxy"] = str(proxy)

        browser_enabled = self.config.get("browser_enabled", True)
        if isinstance(browser_enabled, str):
            browser_enabled = str(browser_enabled).lower() not in ("false", "0", "no", "off")
        overrides["browser"] = {
            "enabled": bool(browser_enabled),
            "timeout": self.config.get("browser_timeout", 45_000),
        }
        return _anypic_load_config(overrides=overrides)

    def _make_progress_hooks(self, progress_callback):
        """Maps engine events into MULTI_DOWNLOADER DownloadProgress updates.

        probe(done, total) fires once per resolved image group; transfer(info,
        delta, done) fires per downloaded chunk and once per finished file.
        Both run on engine worker threads, so all state is lock-guarded.
        """
        lock = threading.Lock()
        state = {
            "files": {},   # url -> {"done": int, "total": int}
            "bytes": 0,
            "speed": None,
            "_last_t": None,
            "_last_bytes": 0,
        }

        def _emit(phase, message, file_name=None, file_done=None, file_total=None):
            try:
                progress_callback(DownloadProgress(
                    phase=phase,
                    status_message=message,
                    downloaded_bytes=state["bytes"] or None,
                    speed_bytes_sec=state["speed"],
                    current_file=file_name,
                    file_name=file_name,
                    file_downloaded_bytes=file_done,
                    file_total_bytes=file_total,
                ))
            except Exception:
                pass

        def probe(done, total):
            if not progress_callback:
                return
            message = f"Probing originals ({done}/{total})" if total else "Probing originals"
            with lock:
                _emit('resolving', message, file_done=done, file_total=total)

        def transfer(info, delta, done=False):
            if not progress_callback:
                return
            with lock:
                f = state["files"].setdefault(info.url, {"done": 0, "total": int(info.length or 0)})
                f["done"] += max(0, delta)
                state["bytes"] += max(0, delta)
                if done and f["total"]:
                    f["done"] = max(f["done"], f["total"])

                now = time.monotonic()
                if delta > 0:
                    if state["_last_t"] is None:
                        state["_last_t"], state["_last_bytes"] = now, state["bytes"]
                    else:
                        dt = now - state["_last_t"]
                        if dt >= 0.25:
                            inst = (state["bytes"] - state["_last_bytes"]) / dt
                            state["speed"] = inst if state["speed"] is None else (state["speed"] * 0.6 + inst * 0.4)
                            state["_last_t"], state["_last_bytes"] = now, state["bytes"]

                name = unquote(urlsplit(info.url).path.rsplit("/", 1)[-1]) or "image"
                _emit('downloading', name, file_name=name,
                      file_done=f["done"], file_total=f["total"] or None)

        return probe, transfer

    async def download(
        self,
        task: DownloadTask,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> ArchiveEntry:
        """Runs the page through anypic's tier pipeline in a worker thread."""
        cancel_event = None  # Phase-3: cooperative cancellation hook inside Fetcher/Downloader

        if progress_callback:
            progress_callback(DownloadProgress(
                phase='resolving',
                status_message='Fetching page',
            ))

        probe_hook, transfer_hook = self._make_progress_hooks(progress_callback)
        cfg = self._build_anypic_config(task, probe_hook=probe_hook)

        def _run() -> PageResult:
            fetcher = Fetcher(cfg)
            resolver = Resolver(cfg, fetcher)
            state = StateStore(cfg.state_db)
            try:
                downloader = Downloader(cfg, fetcher, state, transfer_hook=transfer_hook)
                return _anypic_process_url(
                    task.url, cfg, fetcher, downloader, resolver,
                    use_gallery_dl=False,
                    allow_browser=bool((cfg.get("browser") or {}).get("enabled", True)),
                )
            finally:
                state.close()

        try:
            result = await run_blocking(_run, cancel_event=cancel_event)
        except Exception as e:
            raise DownloadFailedError(f"anypic-dl failed: {e}") from e

        if progress_callback:
            try:
                progress_callback(DownloadProgress(phase='processing', status_message='Finalizing'))
            except Exception:
                pass
        return self._entry_from_result(task, result)

    def _entry_from_result(self, task: DownloadTask, result: PageResult) -> ArchiveEntry:
        """Maps an anypic PageResult onto the ArchiveEntry contract used by the dispatcher."""
        downloaded = result.downloaded
        skipped = result.skipped
        all_skipped = not downloaded and bool(skipped)

        if not downloaded and not skipped:
            notes = "; ".join(result.notes[-3:]) if result.notes else "no images found"
            raise DownloadFailedError(f"anypic-dl found no images for {task.url} ({notes})")

        downloaded_files = [r.file for r in downloaded if r.file]
        skipped_files: List[str] = []
        for r in skipped:
            if r.file:
                skipped_files.append(r.file)
            elif "already saved as " in r.note:
                skipped_files.append(r.note.split("already saved as ", 1)[1].strip())

        # Output layout: direct image links land in <out>/<domain>/, page tiers
        # Layout (flat mode): the dispatcher's output_dir already ends at
        # downloads/anypic-dl/<host>/, so pages nest one more level by slug
        # and direct image links land directly in output_dir.
        directory = Path(task.output_dir)
        if result.tier != "direct":
            directory = directory / _page_slug(task.url)

        if len(downloaded_files) == 1:
            primary = Path(downloaded_files[0])
            file_name = primary.name
            file_path = str(primary.resolve())
        elif downloaded_files:
            file_name = directory.name
            file_path = str(directory.resolve())
        else:
            file_name = directory.name
            file_path = str(directory.resolve())

        total_size = sum(int(r.bytes or 0) for r in downloaded)
        if not total_size and downloaded_files:
            total_size = sum(
                p.stat().st_size for p in (Path(f) for f in downloaded_files) if p.exists()
            )

        first_file = downloaded_files[0] if downloaded_files else None
        suffix = Path(first_file).suffix.lower() if first_file else ".jpg"
        if suffix in (".mp4", ".webm", ".mkv", ".mov"):
            media_type = MediaType.VIDEO
        elif suffix in (".mp3", ".m4a", ".flac", ".wav", ".ogg"):
            media_type = MediaType.AUDIO
        elif suffix in (".zip", ".rar", ".7z", ".pdf"):
            media_type = MediaType.DOCUMENT
        else:
            media_type = MediaType.IMAGE

        return ArchiveEntry(
            id=str(uuid.uuid4()),
            url=task.url,
            file_path=file_path,
            file_name=file_name,
            file_hash=None,
            file_size=total_size,
            backend=self.name,
            source_site=(urlsplit(task.url).netloc or "").lower().removeprefix("www.") or "anypic",
            media_type=media_type,
            metadata={
                "tier": result.tier,
                "downloaded_files": downloaded_files,
                "skipped_files": skipped_files,
                "all_skipped": all_skipped,
                "file_count": len(downloaded_files),
                "skipped_count": len(skipped),
            },
        )
