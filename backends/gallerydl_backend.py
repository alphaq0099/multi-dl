"""
Adapter backend wrapping gallery-dl via its Python API.
Ensures config isolation by resetting memory config state and loading from modular configs.
"""

import copy
import logging
from pathlib import Path
import re
import shutil
import sys
import threading
from typing import Any, Callable, Dict, List, Optional
import uuid

import gallery_dl
from gallery_dl import config as gdl_config
from gallery_dl import job

from backends.base import BaseBackend
from core.exceptions import DownloadFailedError
from core.models import ArchiveEntry, DownloadProgress, DownloadTask, MediaType
from core.terminal import Style, format_bytes
from core.workers import run_blocking
from core.gallery_process import run_gallery_process


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_active_trackers = threading.local()


class _GalleryDlLogHandler(logging.Handler):
    """Formats gallery-dl and network library logs into the slot's verbose status tag."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            # Suppress benign netrc warning if it ever appears
            if "netrc" in msg.lower() and "no authentication info" in msg.lower():
                return

            tracker = getattr(_active_trackers, "current", None)
            clean_msg = re.sub(r'\[.*?\]\s*', '', msg).strip()

            if tracker and tracker.progress_callback:
                prefix = "ERROR" if record.levelno >= logging.ERROR else "WARNING"
                tracker.on_warning(f"{prefix}: {clean_msg}")
                return

            if tracker and tracker.quiet:
                return

            if record.levelno >= logging.ERROR:
                sys.stdout.write(f"\r\033[K{Style.tag('❌', 'ERROR', Style.RED)} {Style.error(clean_msg)}\n")
            elif record.levelno >= logging.WARNING:
                sys.stdout.write(f"\r\033[K{Style.tag('⚠️', 'WARNING', Style.YELLOW)} {Style.warning(clean_msg)}\n")
            sys.stdout.flush()
        except Exception:
            pass


class _GalleryDlBatchTracker:
    """
    Renders an in-place dual progress bar for gallery-dl downloads:
    Line 1 (Top): Batch / Gallery progress (e.g. 45/464 images downloaded)
    Line 2 (Bottom): Current file progress (speed, ETA, sizes, filename)
    """

    def __init__(self, progress_callback: Optional[Callable[[DownloadProgress], None]] = None, quiet: bool = False):
        self.progress_callback = progress_callback
        self.quiet = quiet
        self.album_title: str = ""
        self.extractor_name: str = ""
        self.total_files: Optional[int] = None
        self.completed_count: int = 0
        self.skipped_count: int = 0
        self.downloaded_files: List[str] = []
        self.skipped_files: List[str] = []
        self._file_bytes: Dict[str, int] = {}

        # Current file metrics
        self.current_filename: str = ""
        self.current_downloaded: int = 0
        self.current_total: Optional[int] = None
        self.current_speed: Optional[float] = None

        self.title_announced: bool = False
        self.phase = 'resolving'
        self._lines_printed: int = 0
        self._is_tty: bool = sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else True

    def _emit(self, progress):
        progress.phase = self.phase
        progress.file_name = self.current_filename or None
        progress.file_downloaded_bytes = self.current_downloaded
        progress.file_total_bytes = self.current_total
        progress.completed_files = self.completed_count
        if self.progress_callback:
            self.progress_callback(progress)

    def on_warning(self, warn_msg: str) -> None:
        """Pushes a connection warning/issue directly into the live verbose tag without disturbing stdout."""
        self.phase = 'waiting'
        if self.progress_callback:
            tot_b = sum(self._file_bytes.values())
            pct = ((self.completed_count / self.total_files) * 100.0) if self.total_files else None
            clean_tag = warn_msg.strip()
            if len(clean_tag) > 36:
                clean_tag = clean_tag[:33] + "..."
            prog = DownloadProgress(
                downloaded_bytes=tot_b,
                total_bytes=None,
                speed_bytes_sec=self.current_speed,
                percent=pct,
                current_file=self.album_title or self.current_filename or "Media",
                total_files=self.total_files,
                file_index=self.completed_count,
                status_message=clean_tag,
                extractor=self.extractor_name,
                container_title=self.album_title,
            )
            self._emit(prog)

    @staticmethod
    def _visible_len(s: str) -> int:
        """Returns the visible column width of a string, accounting for ANSI codes and wide characters/emojis."""
        clean = re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', s)
        extra = sum(1 for ch in clean if ord(ch) > 0x1F000 or ch in "🖼📥📦🎬📁🚀⚠️❌✅•")
        return len(clean) + extra

    def on_directory(self, kwdict: Dict[str, Any], folder_name: Optional[str] = None) -> None:
        """Called when gallery metadata is resolved before downloads begin."""
        album_meta = kwdict.get("album") if isinstance(kwdict.get("album"), dict) else {}
        count = kwdict.get("count") or kwdict.get("total") or album_meta.get("count") or album_meta.get("file_count")
        if count and not self.total_files:
            try:
                self.total_files = int(count)
            except (ValueError, TypeError):
                pass

        # 1. Direct folder_name passed from job.pathfmt.directory
        title = None
        if folder_name and folder_name.lower() not in ("downloads", "gallery-dl", "bunkr", "misc", "tmp", ""):
            title = folder_name

        # 2. Bunkr / Lolisafe album format: {album_name} ({album_id}) or {album_name}
        if not title:
            album_name = kwdict.get("album_name") or album_meta.get("album_name")
            album_id = kwdict.get("album_id") or album_meta.get("album_id")
            if album_name and album_id:
                title = f"{album_name} ({album_id})"
            elif album_name:
                title = str(album_name)
            elif album_id:
                title = f"Album ({album_id})"

        # 3. Standard title keys across all gallery-dl extractors
        if not title:
            title = (
                kwdict.get("title")
                or kwdict.get("album_title")
                or kwdict.get("gallery_title")
                or kwdict.get("thread_title")
                or album_meta.get("title")
                or album_meta.get("name")
            )

        if title:
            self.album_title = str(title)

        cat = kwdict.get("category") or kwdict.get("_extractor") or kwdict.get("subcategory")
        if cat and not self.extractor_name:
            self.extractor_name = str(cat).lower()

        if title and not self.title_announced:
            self.title_announced = True
            if not self.quiet:
                sys.stdout.write(f"\r\033[K{Style.tag('🎬', 'MEDIA', Style.YELLOW)} {Style.white(title)}\n")
                if self.total_files:
                    sys.stdout.write(f"\r\033[K{Style.tag('📦', 'BATCH', Style.MAGENTA)} Detected {Style.cyan(str(self.total_files))} files in gallery\n")
                sys.stdout.flush()

        if self.progress_callback:
            msg = f"METADATA: Detected {self.total_files} files" if self.total_files else "METADATA: Fetching album metadata"
            self._emit(
                DownloadProgress(
                    current_file=self.album_title or "Gallery",
                    total_files=self.total_files,
                    file_index=self.completed_count,
                    status_message=msg,
                    extractor=self.extractor_name,
                    container_title=self.album_title,
                )
            )

    def on_url(self, url: str, kwdict: Dict[str, Any]) -> None:
        """Called for each queued image URL."""
        if not self.total_files:
            album_meta = kwdict.get("album") if isinstance(kwdict.get("album"), dict) else {}
            count = kwdict.get("count") or kwdict.get("total") or album_meta.get("count") or album_meta.get("file_count")
            if count:
                try:
                    self.total_files = int(count)
                except (ValueError, TypeError):
                    pass

        # If album title is not yet resolved, try extracting from the file's kwdict
        if not self.album_title:
            album_meta = kwdict.get("album") if isinstance(kwdict.get("album"), dict) else {}
            album_name = kwdict.get("album_name") or album_meta.get("album_name")
            album_id = kwdict.get("album_id") or album_meta.get("album_id")
            if album_name and album_id:
                self.album_title = f"{album_name} ({album_id})"
            elif album_name:
                self.album_title = str(album_name)
            else:
                title = (
                    kwdict.get("title")
                    or kwdict.get("album_title")
                    or kwdict.get("gallery_title")
                    or kwdict.get("thread_title")
                    or album_meta.get("title")
                )
                if title:
                    self.album_title = str(title)

        fname = kwdict.get("filename")
        if fname:
            self.current_filename = str(fname)
        self.current_downloaded = 0
        self.current_total = None

        if not self.extractor_name:
            cat = kwdict.get("category") or kwdict.get("_extractor") or kwdict.get("subcategory")
            if cat:
                self.extractor_name = str(cat).lower()

        if self.progress_callback and not self.completed_count:
            self._emit(
                DownloadProgress(
                    current_file=self.album_title or "Gallery",
                    total_files=self.total_files,
                    file_index=self.completed_count,
                    status_message=f"Preparing: {fname}" if fname else "Queueing media URLs",
                    extractor=self.extractor_name,
                    container_title=self.album_title,
                )
            )

    def start(self, path: str) -> None:
        """Called when download starts for a specific file."""
        p = Path(path)
        self.current_filename = p.name
        self.current_downloaded = 0
        self.current_total = None
        self.current_speed = None
        self.phase = 'downloading'
        # Derive album title from parent folder if not set or generic
        if not self.album_title and p.parent.name and p.parent.name.lower() not in ("downloads", "gallery-dl", "bunkr", "misc", "tmp", ""):
            self.album_title = p.parent.name
        self._emit(DownloadProgress(downloaded_bytes=sum(self._file_bytes.values()),
                                    current_file=self.album_title or p.name, total_files=self.total_files,
                                    file_index=self.completed_count, status_message='Downloading',
                                    extractor=self.extractor_name, container_title=self.album_title))
        if not self.quiet:
            self._render()

    def progress(self, bytes_total: Optional[int], bytes_downloaded: int, bytes_per_second: int) -> None:
        """Called with live byte stream progress for the current file."""
        self.phase = 'downloading'
        self.current_total = bytes_total
        self.current_downloaded = bytes_downloaded
        if bytes_per_second:
            self.current_speed = float(bytes_per_second)
        if self.current_filename:
            self._file_bytes[self.current_filename] = bytes_downloaded

        if self.progress_callback:
            tot_b = sum(self._file_bytes.values())
            pct = ((self.completed_count / self.total_files) * 100.0) if self.total_files else None
            prog = DownloadProgress(
                downloaded_bytes=tot_b,
                total_bytes=None,
                speed_bytes_sec=self.current_speed,
                percent=pct,
                current_file=self.album_title or self.current_filename,
                total_files=self.total_files,
                file_index=self.completed_count,
                status_message=self.current_filename,
                extractor=self.extractor_name,
                container_title=self.album_title,
            )
            self._emit(prog)

        if not self.quiet:
            self._render()

    def success(self, path: str) -> None:
        """Called when a file completes downloading successfully."""
        self.downloaded_files.append(path)
        self.completed_count += 1
        p = Path(path)
        self.current_filename = p.name
        if p.exists():
            self.current_total = p.stat().st_size
            self.current_downloaded = self.current_total
            self._file_bytes[p.name] = self.current_total

        is_completed = bool(self.total_files and self.completed_count >= self.total_files)
        self.phase = 'processing' if is_completed else 'resolving'
        status_msg = "Finalizing archive..." if is_completed else 'Fetching next file'
        reported_speed = None if is_completed else self.current_speed

        if self.progress_callback:
            tot_b = sum(self._file_bytes.values())
            pct = 100.0 if is_completed else (((self.completed_count / self.total_files) * 100.0) if self.total_files else None)
            prog = DownloadProgress(
                downloaded_bytes=tot_b,
                total_bytes=None,
                speed_bytes_sec=reported_speed,
                percent=pct,
                current_file=self.album_title or p.name,
                total_files=self.total_files,
                file_index=self.completed_count,
                status_message=status_msg,
                extractor=self.extractor_name,
                container_title=self.album_title,
            )
            self._emit(prog)

        if not self.quiet:
            self._render()

    def skip(self, path: str) -> None:
        """Called when an existing or archived file is skipped."""
        self.skipped_count += 1
        self.completed_count += 1
        p = Path(path)
        self.skipped_files.append(str(p.resolve()))
        self.current_filename = p.name
        if p.exists():
            self._file_bytes[p.name] = p.stat().st_size

        is_completed = bool(self.total_files and self.completed_count >= self.total_files)
        self.phase = 'processing' if is_completed else 'resolving'
        status_msg = "Finalizing archive..." if is_completed else f"Skipped: {p.name}"
        reported_speed = None if is_completed else self.current_speed

        if self.progress_callback:
            tot_b = sum(self._file_bytes.values())
            pct = 100.0 if is_completed else (((self.completed_count / self.total_files) * 100.0) if self.total_files else None)
            prog = DownloadProgress(
                downloaded_bytes=tot_b,
                total_bytes=None,
                speed_bytes_sec=reported_speed,
                percent=pct,
                current_file=self.album_title or p.name,
                total_files=self.total_files,
                file_index=self.completed_count,
                status_message=status_msg,
            )
            self._emit(prog)

        if not self.quiet:
            self._render()

    def error(self, msg: str) -> None:
        """Called on gallery-dl error."""
        if not self.quiet:
            sys.stdout.write(f"\r\033[K{Style.tag('❌', 'GALLERY-DL ERROR', Style.RED)} {Style.red(msg)}\n")
            self._lines_printed = 0
            sys.stdout.flush()

    def finish(self) -> None:
        """Finalizes the dual progress display when download completes."""
        self.phase = 'processing'
        if self.progress_callback:
            tot_b = sum(self._file_bytes.values()) if self._file_bytes else 0
            self._emit(DownloadProgress(
                downloaded_bytes=tot_b,
                total_bytes=None,
                speed_bytes_sec=None,
                percent=min(100.0, 100.0 * self.completed_count / self.total_files) if self.total_files else None,
                current_file=self.album_title,
                total_files=self.total_files,
                file_index=self.completed_count,
                status_message="Finalizing archive...",
            ))
        if not self.quiet:
            self._render(done=True)

    def _render(self, done: bool = False) -> None:
        if self.quiet:
            return
        term_width = shutil.get_terminal_size((80, 24)).columns
        max_width = max(40, term_width - 2)

        # Adapt bar width if terminal is narrow
        bar_w = 8 if max_width < 75 else 16

        # --- Line 1: Batch / Gallery Progress ---
        if self.total_files and self.total_files > 0:
            batch_pct = (self.completed_count / self.total_files) * 100.0
            batch_pct_text = f"{batch_pct:5.1f}%"
            batch_bar = Style.progress_bar(batch_pct, width=bar_w)
            batch_count = f"{self.completed_count}/{self.total_files} files"
        else:
            batch_pct = None
            batch_pct_text = "  --% "
            batch_bar = Style.progress_bar(None, width=bar_w)
            batch_count = f"{self.completed_count} files"

        total_batch_bytes = sum(self._file_bytes.values())
        if total_batch_bytes > 0:
            size_color = Style.GREEN if (batch_pct is not None and batch_pct >= 100.0) else Style.WHITE
            size_str = f" {Style.dim('•')} {size_color}{format_bytes(total_batch_bytes)}{Style.RESET}"
        else:
            size_str = ""

        batch_color = Style.GREEN if (batch_pct is not None and batch_pct >= 100.0) else Style.MAGENTA
        tag_batch = Style.tag("🖼️", "BATCH", batch_color)
        pct_color = Style.GREEN if (batch_pct is not None and batch_pct >= 100.0) else Style.CYAN
        skip_notice = f" {Style.dim(f'[{self.skipped_count} skipped]')}" if self.skipped_count else ""

        base_line1 = f"{tag_batch} {batch_bar} {Style.white(batch_count)}"
        pct_part = f" {Style.dim('(')}{pct_color}{batch_pct_text}{Style.RESET}{Style.dim(')')}"

        line1 = f"{base_line1}{size_str}{pct_part}{skip_notice}"
        if self._visible_len(line1) > max_width:
            line1 = f"{base_line1}{size_str}{pct_part}"
            if self._visible_len(line1) > max_width:
                line1 = f"{base_line1}{pct_part}"

        # --- Line 2: Current File Progress ---
        if self.current_total and self.current_total > 0:
            file_pct = (self.current_downloaded / self.current_total) * 100.0
            file_pct_text = f"{file_pct:5.1f}%"
            file_bar = Style.progress_bar(file_pct, width=bar_w)
            file_sizes = f"{format_bytes(self.current_downloaded)} / {format_bytes(self.current_total)}"
            rem_bytes = max(0, self.current_total - self.current_downloaded)
            eta_sec = (rem_bytes / self.current_speed) if (self.current_speed and self.current_speed > 0) else None
        else:
            file_pct = None
            file_pct_text = "  --% "
            file_bar = Style.progress_bar(None, width=bar_w)
            file_sizes = f"{format_bytes(self.current_downloaded)} / ??"
            eta_sec = None

        file_color = Style.GREEN if (file_pct is not None and file_pct >= 100.0) else Style.CYAN
        tag_file = Style.tag("📥", file_pct_text, file_color)
        speed_str = f"{format_bytes(int(self.current_speed))}/s" if self.current_speed else "--/s"
        speed_part = f" {Style.dim('@')} {Style.speed(speed_str)}"

        eta_part = ""
        if eta_sec and eta_sec > 0 and (file_pct is None or file_pct < 100.0):
            eta_m, eta_s = divmod(int(eta_sec), 60)
            eta_part = f" {Style.dim('ETA')} {Style.yellow(f'{eta_m:02d}:{eta_s:02d}')}"

        prefix = f"{tag_file} {file_bar} {Style.white(file_sizes)}{speed_part}"
        prefix_vis = self._visible_len(prefix)

        # Include ETA if it fits comfortably before filename
        if eta_part and (prefix_vis + self._visible_len(eta_part) + 12 <= max_width):
            prefix += eta_part
            prefix_vis = self._visible_len(prefix)

        # Allocate remaining width to filename (leaving 3 cols for ' [' and ']')
        fname = self.current_filename or "..."
        avail_fname = max_width - prefix_vis - 3
        if avail_fname >= 6:
            if len(fname) > avail_fname:
                fname_display = fname[:avail_fname - 3] + "..."
            else:
                fname_display = fname
            fname_part = f" {Style.dim('[')}{Style.white(fname_display)}{Style.dim(']')}"
            line2 = f"{prefix}{fname_part}"
        else:
            line2 = prefix

        if self._is_tty:
            if self._lines_printed == 0:
                sys.stdout.write(f"\r\033[K{line1}\n\r\033[K{line2}")
                self._lines_printed = 2
            else:
                sys.stdout.write(f"\033[A\r\033[K{line1}\n\r\033[K{line2}")

            if done:
                sys.stdout.write("\n")
                self._lines_printed = 0
            sys.stdout.flush()
        else:
            if done:
                sys.stdout.write(f"{line1}\n")
                sys.stdout.flush()

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


class _MultiDlGalleryJob(job.DownloadJob):
    """
    Custom DownloadJob subclass that routes metadata and items to _GalleryDlBatchTracker.
    Fully supports child/sub-jobs spawned by redirect extractors (e.g. Reddit, RedGifs, Twitter).
    """

    def __init__(self, url_or_extr: Any, parent: Any = None, tracker: Optional[_GalleryDlBatchTracker] = None):
        if isinstance(parent, _MultiDlGalleryJob):
            # Child job spawned by gallery-dl: inherit tracker and pass parent to super()
            super().__init__(url_or_extr, parent)
            self.tracker = parent.tracker
        elif isinstance(parent, _GalleryDlBatchTracker):
            # Called with (url, tracker) as positional args
            super().__init__(url_or_extr, None)
            self.tracker = parent
        else:
            # Root job with optional tracker kwarg or parent
            super().__init__(url_or_extr, parent if not isinstance(parent, _GalleryDlBatchTracker) else None)
            self.tracker = tracker or getattr(parent, "tracker", None)

        if self.tracker:
            self.out = self.tracker

    def handle_directory(self, kwdict: Dict[str, Any]):
        res = super().handle_directory(kwdict)
        if self.tracker and hasattr(self.tracker, "on_directory"):
            folder_name = None
            if getattr(self, "pathfmt", None) and getattr(self.pathfmt, "directory", None):
                folder_name = Path(self.pathfmt.directory).name
            self.tracker.on_directory(kwdict, folder_name=folder_name)
        return res

    def handle_url(self, url: str, kwdict: Dict[str, Any]):
        if self.tracker and hasattr(self.tracker, "on_url"):
            self.tracker.on_url(url, kwdict)
        return super().handle_url(url, kwdict)

    def on_directory(self, kwdict: Dict[str, Any], folder_name: Optional[str] = None):
        """Fallback in case gallery-dl calls on_directory directly on the job."""
        if self.tracker and hasattr(self.tracker, "on_directory"):
            if not folder_name and getattr(self, "pathfmt", None) and getattr(self.pathfmt, "directory", None):
                folder_name = Path(self.pathfmt.directory).name
            self.tracker.on_directory(kwdict, folder_name=folder_name)

    def on_url(self, url: str, kwdict: Dict[str, Any]):
        """Fallback in case gallery-dl calls on_url directly on the job."""
        if self.tracker and hasattr(self.tracker, "on_url"):
            self.tracker.on_url(url, kwdict)


class GalleryDlBackend(BaseBackend):
    """Integrates gallery-dl via its Python library interfaces with modular config isolation."""

    name: str = "gallery-dl"

    _init_lock = threading.Lock()
    _pre_initialized = False
    _config_lock = threading.RLock()
    _configured = False

    @classmethod
    def pre_initialize(cls) -> None:
        """Pre-initializes gallery-dl extractor registry serially to eliminate generator collision race conditions."""
        with cls._init_lock:
            if not cls._pre_initialized:
                try:
                    list(gallery_dl.extractor.extractors())
                except Exception:
                    pass
                cls._pre_initialized = True

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.pre_initialize()

    def _isolated_run(self, function):
        """Keep the global configuration private until the entire worker finishes."""
        with self._config_lock:
            previous = copy.deepcopy(gdl_config._config)
            previous_files = list(gdl_config._files)
            logs = [(logging.getLogger(name), list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
                    for name in ('gallery-dl', 'downloader', 'urllib3')]
            configured = GalleryDlBackend._configured
            try:
                gdl_config.clear()
                GalleryDlBackend._configured = False
                return function()
            finally:
                # gallery-dl functions retain this dict in default arguments.
                # Restore it in place; rebinding _config leaves those functions stale.
                gdl_config.clear()
                gdl_config._config.update(previous)
                gdl_config._files[:] = previous_files
                for logger, handlers, propagate in logs:
                    logger.handlers = handlers
                    logger.propagate = propagate
                GalleryDlBackend._configured = configured

    def _configure_job(self, out_dir: Optional[Path] = None, disable_archive: bool = False, quiet: bool = False) -> None:
        """Loads configuration from the project's config file safely without wiping active worker memory."""
        with self._config_lock:
            if not GalleryDlBackend._configured:
                # Load engine-specific config file (e.g. configs/gallery-dl.json)
                config_file = self.config.get("config_file", "configs/gallery-dl.json")
                cfg_path = Path(config_file)
                if not cfg_path.is_absolute():
                    project_root = Path(__file__).resolve().parent.parent
                    cfg_path = project_root / cfg_path
                if not cfg_path.exists():
                    example_path = cfg_path.with_name("gallery-dl.example.json")
                    if example_path.exists():
                        cfg_path = example_path
                if cfg_path.exists():
                    gdl_config.load([str(cfg_path.resolve())])

                # Configure fine-grained progress notification and network timeouts
                gdl_config.set(("downloader", "http"), "progress", 0.1)
                gdl_config.set(("downloader", "http"), "timeout", self.config.get('timeout', 20.0))
                gdl_config.set(("downloader", "http"), "retries", self.config.get('retries', 4))
                gdl_config.set(("downloader", "ytdl"), "progress", 0.1)
                gdl_config.set(("downloader", "ytdl"), "timeout", 20.0)
                gdl_config.set(("downloader", "ytdl"), "retries", 4)
                gdl_config.set(("output",), "shorten", False)

                # Disable netrc lookups to prevent unwanted 'No authentication info' warnings
                gdl_config.set((), "netrc", False)
                GalleryDlBackend._configured = True

            # Anchor cache database file to PROJECT_ROOT / data / cache.sqlite3
            cache_file = PROJECT_ROOT / "data" / "cache.sqlite3"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            gdl_config.set(("cache",), "file", str(cache_file.resolve()))
            if self.config.get('proxy'):
                gdl_config.set((), 'proxy', self.config['proxy'])

            # Dynamic archive configuration based on task options and global setting
            if disable_archive:
                gdl_config.set(("extractor",), "archive", None)
            else:
                archive_dir = PROJECT_ROOT / "data" / "archives"
                archive_dir.mkdir(parents=True, exist_ok=True)
                arc = gdl_config.get(("extractor",), "archive")
                if not arc or arc == ["data", "archives", "{category}.sqlite3"]:
                    gdl_config.set(("extractor",), "archive", [str(archive_dir), "{category}.sqlite3"])
                elif isinstance(arc, list) and arc and not Path(arc[0]).is_absolute():
                    gdl_config.set(("extractor",), "archive", [str(PROJECT_ROOT / arc[0])] + arc[1:])
                elif isinstance(arc, str) and not Path(arc).is_absolute():
                    gdl_config.set(("extractor",), "archive", str((PROJECT_ROOT / arc).resolve()))

            # Enforce MULTI_DOWNLOADER output directory override
            if out_dir:
                out_dir.mkdir(parents=True, exist_ok=True)
                gdl_config.set(("extractor",), "base-directory", str(out_dir.resolve()))

            # Route gallery-dl logger through MULTI_DOWNLOADER terminal handler
            gdl_logger = logging.getLogger("gallery-dl")
            if quiet:
                gdl_logger.handlers = []
            elif not gdl_logger.handlers:
                gdl_logger.handlers = [_GalleryDlLogHandler()]
            gdl_logger.propagate = False

    @staticmethod
    def _apply_cookies(cookies_browser: Optional[str] = None, cookie_file: Optional[str] = None) -> None:
        """Configures browser cookies or Netscape cookie file into gallery-dl."""
        if cookies_browser:
            browser, _, profile = cookies_browser.partition(":")
            browser, _, keyring = browser.partition("+")
            browser, _, domain = browser.partition("/")
            if profile and profile.startswith(":"):
                container = profile[1:]
                profile = None
            else:
                profile, _, container = profile.partition("::")
            gdl_config.set((), "cookies", (browser, profile or None, keyring, container, domain))
        elif cookie_file:
            cf_path = Path(cookie_file)
            if not cf_path.is_absolute():
                cf_path = PROJECT_ROOT / cf_path
            if cf_path.exists():
                gdl_config.set((), "cookies", str(cf_path.resolve()))

    def can_handle(self, url: str) -> bool:
        """Query gallery-dl's extractor registry for support."""
        self.pre_initialize()
        try:
            extractor = gallery_dl.extractor.find(url)
            return extractor is not None
        except Exception:
            return False

    def _failover_cyberdrop(self) -> "CyberdropDlBackend":
        """Failover instance that keeps this backend's proxy/network settings."""
        from backends.cyberdrop_backend import CyberdropDlBackend
        settings = {k: v for k, v in self.config.items()
                    if k in ('proxy', 'timeout', 'user_agent', 'cookies_from_browser', 'cookies_file')}
        return CyberdropDlBackend(settings)

    async def extract_info(self, url: str) -> Dict[str, Any]:
        return await run_gallery_process(self.config, 'info', url=url)

    async def _extract_info_local(self, url: str) -> Dict[str, Any]:
        """Extract metadata without downloading media."""
        cookies_browser = self.config.get("cookies_from_browser")
        cookie_file = self.config.get("cookies_file")

        def _extract() -> Dict[str, Any]:
            self._configure_job()
            self._apply_cookies(cookies_browser=cookies_browser, cookie_file=cookie_file)
            data_job = job.DataJob(url)
            data_job.run()
            return getattr(data_job, "data", {})

        try:
            data = await run_blocking(self._isolated_run, _extract)
            # Check for AuthRequired in data
            if isinstance(data, list) and len(data) == 1 and isinstance(data[0], (list, tuple)) and isinstance(data[0][-1], dict) and data[0][-1].get("error") == "AuthRequired":
                fallback_browser = self.config.get("fallback_browser_cookies", "firefox")
                if not cookies_browser and fallback_browser:
                    cookies_browser = fallback_browser
                    return await run_blocking(self._isolated_run, _extract)
            return data
        except Exception as e:
            fallback_browser = self.config.get("fallback_browser_cookies", "firefox")
            if not cookies_browser and fallback_browser and any(a in str(e).lower() for a in ("auth", "login", "password", "logged-in")):
                cookies_browser = fallback_browser
                try:
                    return await run_blocking(self._isolated_run, _extract)
                except Exception as retry_err:
                    raise DownloadFailedError(f"gallery-dl metadata extraction failed with fallback cookies: {retry_err}")
            raise DownloadFailedError(f"gallery-dl metadata extraction failed: {e}")

    async def download(
        self,
        task: DownloadTask,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> ArchiveEntry:
        """Run each gallery in a process with independent library configuration."""
        try:
            return await run_gallery_process(self.config, 'download', task=task,
                                              progress_callback=progress_callback)
        except DownloadFailedError:
            if self.config.get('enable_failover_to_cyberdrop', True) and not task.options.get('_is_failover') and not task.options.get('_managed_failover'):
                backend = self._failover_cyberdrop()
                if backend.can_handle(task.url):
                    from dataclasses import replace
                    fallback = replace(task, backend=backend.name, options=dict(task.options, _is_failover=True))
                    return await backend.download(fallback, progress_callback=progress_callback)
            raise

    async def _download_local(
        self,
        task: DownloadTask,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> ArchiveEntry:
        """Download images/galleries using gallery-dl."""
        out_dir = Path(task.output_dir)
        tracker: Optional[_GalleryDlBatchTracker] = None
        quiet = bool(task.options.get("quiet") or progress_callback is not None)

        cookies_browser = task.options.get("cookies_from_browser", self.config.get("cookies_from_browser"))
        cookie_file = task.options.get("cookies", self.config.get("cookies_file"))
        cancel_event = threading.Event()
        original_callback = progress_callback
        def progress_callback(progress):
            if cancel_event.is_set():
                raise DownloadFailedError('Download interrupted')
            if original_callback:
                original_callback(progress)

        def _run_download() -> int:
            nonlocal tracker
            disable_archive = bool(task.options.get("no_archive") or task.options.get("force"))
            self._configure_job(out_dir=out_dir, disable_archive=disable_archive, quiet=quiet)
            if task.options.get('force'):
                gdl_config.set(('extractor',), 'skip', False)
            self._apply_cookies(cookies_browser=cookies_browser, cookie_file=cookie_file)

            tracker = _GalleryDlBatchTracker(progress_callback=progress_callback, quiet=quiet)
            _active_trackers.current = tracker

            # Apply proxy if configured
            proxy = task.options.get("proxy") or self.config.get("proxy")
            if proxy:
                gdl_config.set((), "proxy", str(proxy))

            # Apply media type filter
            audio_only = bool(task.options.get("audio_only") or task.options.get("extract_audio"))
            video_only = bool(task.options.get("video_only"))
            images_only = bool(task.options.get("images_only") or task.options.get("image_only"))
            if images_only:
                gdl_config.set(("extractor",), "filter", "extension in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'avif')")
            elif video_only:
                gdl_config.set(("extractor",), "filter", "extension in ('mp4', 'mkv', 'webm', 'mov', 'avi', 'flv', 'wmv')")
            elif audio_only:
                gdl_config.set(("extractor",), "filter", "extension in ('mp3', 'm4a', 'flac', 'wav', 'ogg', 'opus')")

            # Intercept warnings/errors from gallery-dl, downloader, and urllib network layers
            handler = _GalleryDlLogHandler()
            loggers_to_hook = [
                logging.getLogger("gallery-dl"),
                logging.getLogger("downloader"),
                logging.getLogger("urllib3"),
            ]
            for lg in loggers_to_hook:
                lg.addHandler(handler)

            try:
                dl_job = _MultiDlGalleryJob(task.url, tracker=tracker)
                return dl_job.run()
            finally:
                for lg in loggers_to_hook:
                    try:
                        lg.removeHandler(handler)
                    except Exception:
                        pass
                _active_trackers.current = None

        try:
            status_code = await run_blocking(self._isolated_run, _run_download, cancel_event=cancel_event)
            if status_code == 20:  # ERROR_AUTHENTICATION in gallery-dl
                fallback_browser = self.config.get("fallback_browser_cookies", "firefox")
                if not cookies_browser and fallback_browser:
                    auth_msg = f"AUTH: Retrying with {fallback_browser} cookies"
                    if progress_callback:
                        progress_callback(DownloadProgress(status_message=auth_msg))
                    if not quiet:
                        print(f"\n{Style.tag('⚠️', 'AUTH-REQUIRED', Style.YELLOW)} Forum or gallery requires sign-in. Automatically retrying with {Style.bold(fallback_browser)} cookies...")
                    cookies_browser = fallback_browser
                    status_code = await run_blocking(self._isolated_run, _run_download, cancel_event=cancel_event)

            if tracker:
                tracker.finish()
            if status_code != 0:
                raise DownloadFailedError(f"gallery-dl returned status code: {status_code}")
        except Exception as e:
            if tracker:
                tracker.finish()

            err_str = str(e)
            fallback_browser = self.config.get("fallback_browser_cookies", "firefox")
            if not cookies_browser and fallback_browser and any(a in err_str.lower() for a in ("auth", "login", "password", "logged-in")):
                try:
                    auth_msg = f"AUTH: Retrying with {fallback_browser} cookies"
                    if progress_callback:
                        progress_callback(DownloadProgress(status_message=auth_msg))
                    if not quiet:
                        print(f"\n{Style.tag('⚠️', 'AUTH-REQUIRED', Style.YELLOW)} Forum or gallery requires sign-in. Automatically retrying with {Style.bold(fallback_browser)} cookies...")
                    cookies_browser = fallback_browser
                    status_code = await run_blocking(self._isolated_run, _run_download, cancel_event=cancel_event)
                    if status_code == 0:
                        e = None
                    else:
                        raise DownloadFailedError(f"gallery-dl returned status code: {status_code}")
                except Exception as retry_err:
                    e = retry_err

            if e is not None:
                # ── Automatic Failover to cyberdrop-dl ──
                if self.config.get("enable_failover_to_cyberdrop", True) and not task.options.get("_is_failover") and not task.options.get("_managed_failover"):
                    try:
                        cdl = self._failover_cyberdrop()
                        is_no_extractor = "NoExtractorError" in type(e).__name__ or str(e) in ("None", "")
                        if cdl.can_handle(task.url) or is_no_extractor:
                            err_hint = "no extractor" if is_no_extractor else str(e)
                            print(f"\n{Style.tag('🔄', 'FAILOVER', Style.YELLOW)} gallery-dl encountered an issue ({err_hint}).")
                            print(f"{Style.tag('⚙️', 'FAILOVER', Style.CYAN)} Attempting automatic fallback with {Style.engine_badge('cyberdrop-dl')}...")
                            fallback_opts = dict(task.options)
                            fallback_opts["_is_failover"] = True
                            fallback_task = DownloadTask(
                                url=task.url,
                                backend="cyberdrop-dl",
                                output_dir=task.output_dir,
                                options=fallback_opts,
                            )
                            return await cdl.download(fallback_task, progress_callback=progress_callback)
                    except Exception:
                        pass

                clean_err = "No extractor found for URL" if str(e) in ("None", "") or "NoExtractorError" in type(e).__name__ else str(e)
                raise DownloadFailedError(f"gallery-dl download failed: {clean_err}")

        downloaded_files = tracker.downloaded_files if tracker else []
        skipped_files = tracker.skipped_files if tracker else []
        all_skipped = len(downloaded_files) == 0 and len(skipped_files) > 0
        
        # Resolve primary file or folder path
        files_to_check = downloaded_files if downloaded_files else skipped_files
        if files_to_check:
            primary_path = Path(files_to_check[0])
            if tracker and tracker._file_bytes:
                total_size = sum(tracker._file_bytes.values())
            else:
                total_size = sum(Path(f).stat().st_size for f in files_to_check if Path(f).exists())
            if len(files_to_check) == 1:
                file_name = primary_path.name
                file_path = str(primary_path.resolve())
            else:
                album_dir = primary_path.parent
                file_name = album_dir.name
                file_path = str(album_dir.resolve())
                # Check for postprocessed archive (e.g. cbz/zip created from individual files)
                cbz_candidates = list(album_dir.glob("*.cbz")) + list(album_dir.parent.glob(f"{album_dir.name}*.cbz"))
                if cbz_candidates:
                    file_name = cbz_candidates[0].name
                    file_path = str(cbz_candidates[0].resolve())
                    if total_size == 0 and cbz_candidates[0].exists():
                        total_size = cbz_candidates[0].stat().st_size
        else:
            # Fallback when no files were tracked by DownloadJob callbacks
            total_size = 0
            if tracker and tracker._file_bytes:
                total_size = sum(tracker._file_bytes.values())

            if total_size == 0:
                raise DownloadFailedError(f"No media files found or extracted for URL: {task.url}")

            primary_path = out_dir
            file_name = Path(task.url.split("?")[0].rstrip("/")).name or "gallery-dl"
            file_path = str(out_dir.resolve())

        # If individual files were deleted by postprocessor, fall back to tracked download bytes
        if tracker and tracker._file_bytes:
            tracker_bytes = sum(tracker._file_bytes.values())
            if tracker_bytes > total_size:
                total_size = tracker_bytes

        if total_size == 0 and file_path and Path(file_path).exists() and Path(file_path).resolve() != out_dir.resolve():
            p_check = Path(file_path)
            if p_check.is_file():
                total_size = p_check.stat().st_size
            elif p_check.is_dir():
                total_size = sum(f.stat().st_size for f in p_check.rglob("*") if f.is_file())

        # Determine media type based on extensions
        first_ext = primary_path.suffix.lower() if primary_path.is_file() else ""
        if first_ext in (".mp4", ".webm", ".mkv", ".mov"):
            media_type = MediaType.VIDEO
        elif first_ext in (".mp3", ".m4a", ".aac", ".flac"):
            media_type = MediaType.AUDIO
        elif first_ext in (".zip", ".cbz", ".tar"):
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
            source_site=task.url.split("/")[2] if "://" in task.url else "gallery-dl",
            media_type=media_type,
            metadata={
                "downloaded_files": downloaded_files,
                "skipped_files": skipped_files,
                "all_skipped": all_skipped,
                "file_count": len(downloaded_files),
                "skipped_count": len(skipped_files),
            },
        )
