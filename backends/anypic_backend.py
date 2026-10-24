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

import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

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
from anypic.downloader import Downloader, StateStore, _domain_dir, _page_slug
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

    def _build_anypic_config(self, task: DownloadTask) -> Any:
        """Bridges the anypic_dl config section + task options into engine Config."""
        overrides: Dict[str, Any] = {
            "output_dir": str(task.output_dir),
            "data_dir": ANYPIC_DATA_DIR,
            "gallery_dl": {"enabled": False},
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
                status_message='Probing page for original-resolution images',
            ))

        cfg = self._build_anypic_config(task)

        def _run() -> PageResult:
            fetcher = Fetcher(cfg)
            resolver = Resolver(cfg, fetcher)
            state = StateStore(cfg.state_db)
            try:
                downloader = Downloader(cfg, fetcher, state)
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
        # in <out>/<domain>/<page-slug>/ (the engine's own nesting).
        directory = Path(task.output_dir) / _domain_dir(task.url)
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
            source_site=_domain_dir(task.url),
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
