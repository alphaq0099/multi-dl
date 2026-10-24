"""
Adapter backend wrapping cyberdrop-dl for MULTI_DOWNLOADER.
Features:
- Subprocess execution with asynchronous stdout streaming
- Comprehensive support for file lockers (Bunkr, Gofile, Pixeldrain, Catbox, Coomer, Kemono, Saint, etc.)
- Forum thread crawler support (SimpCity, F95zone, ViperGirls)
- Config isolation via configs/cyberdrop-dl.json
- Domain conflict management and automatic failover to gallery-dl on anti-bot/unsupported locker errors
- Integration with unified terminal styling and SQLite archive
"""

import asyncio
from functools import wraps
from dataclasses import replace
import tempfile
import subprocess
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import urllib.parse
import urllib.request
import uuid

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from backends.base import BaseBackend
from core.exceptions import DownloadFailedError, EngineNotFoundError
from core.models import ArchiveEntry, DownloadProgress, DownloadTask, MediaType
from core.storage import async_file_lock
from core.workers import run_blocking, terminate_process_tree
from core.archive import ArchiveManager


def exclusive_destination(method):
    @wraps(method)
    async def wrapped(self, task, *args, **kwargs):
        async with async_file_lock(Path(task.output_dir) / '.multi-dl-transfer'):
            if task.options.get('no_archive'):
                transient = PROJECT_ROOT / 'data' / 'transient'
                transient.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=transient) as state:
                    task = replace(task, options=dict(task.options, _native_state=state))
                    return await method(self, task, *args, **kwargs)
            return await method(self, task, *args, **kwargs)
    return wrapped
from core.terminal import Style


def _detect_media_type(filename: str) -> MediaType:
    """Infers MediaType enum from filename extension."""
    ext = Path(filename).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff"):
        return MediaType.IMAGE
    if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v"):
        return MediaType.VIDEO
    if ext in (".mp3", ".m4a", ".flac", ".ogg", ".wav", ".aac", ".opus"):
        return MediaType.AUDIO
    if ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".pdf", ".txt", ".cbz", ".cbr"):
        return MediaType.DOCUMENT
    return MediaType.UNKNOWN


def _probe_file_size(url: str, proxy=None) -> Tuple[Optional[int], Optional[int]]:
    """Lightweight direct metadata probe for supported file lockers. Returns (total_bytes, total_files)."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy})) if proxy else urllib.request.build_opener()
        parsed = urllib.parse.urlparse(url)
        domain = (parsed.netloc or "").lower()
        parts = parsed.path.strip("/").split("/")

        # Pixeldrain single file
        if "pixeldrain.com" in domain and len(parts) >= 2 and parts[0] in ("u", "file"):
            file_id = parts[1]
            api_url = f"https://pixeldrain.com/api/file/{file_id}/info"
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=2.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("success") and data.get("size"):
                        return int(data["size"]), 1

        # Pixeldrain album / list
        elif "pixeldrain.com" in domain and len(parts) >= 2 and parts[0] == "l":
            list_id = parts[1]
            api_url = f"https://pixeldrain.com/api/list/{list_id}"
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=2.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    files = data.get("files") or []
                    if files:
                        tot_size = sum(f.get("size", 0) for f in files)
                        return tot_size, len(files)

        # Catbox direct links
        elif "catbox.moe" in domain and parsed.path.strip("/"):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
            with opener.open(req, timeout=2.0) as resp:
                cl = resp.headers.get("Content-Length")
                if cl:
                    return int(cl), 1

        # Direct media files
        elif Path(parsed.path).suffix.lower() in (
            ".mp4", ".mkv", ".webm", ".avi", ".mov", ".zip", ".rar", ".7z", ".tar", ".gz",
            ".jpg", ".png", ".gif", ".webp", ".mp3", ".flac"
        ):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
            with opener.open(req, timeout=2.0) as resp:
                cl = resp.headers.get("Content-Length")
                if cl:
                    return int(cl), 1

    except Exception:
        pass
    return None, None


def _query_cdl_db_size(filename: Optional[str] = None, url: Optional[str] = None) -> Tuple[Optional[int], Optional[int]]:
    """
    Attempts to look up expected total size and file count from cyberdrop.db.
    Supports both single items and multi-file albums/threads.
    Returns (total_bytes, total_files).
    """
    try:
        db_path = Path(os.path.expandvars(r"%APPDATA%\cyberdrop-dl\cyberdrop.db"))
        if not db_path.exists():
            return None, None
        import sqlite3
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
            cur = conn.cursor()

            # 1. Look up by source URL, slug, or album_id in media table
            if url:
                parsed = urllib.parse.urlparse(url)
                clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
                slug_parts = [p for p in parsed.path.strip("/").split("/") if p]
                slug = slug_parts[-1] if slug_parts else ""
                cur.execute(
                    """
                    SELECT COUNT(*), SUM(file_size) FROM media 
                    WHERE (album_id = ? OR referer LIKE ? OR referer LIKE ? OR url_path LIKE ?) 
                      AND file_size > 0
                    """,
                    (slug, f"%{clean_url}%", f"%{slug}%", f"%{slug}%")
                )
                row = cur.fetchone()
                if row and row[0] and row[1]:
                    return int(row[1]), int(row[0])

            # 2. Look up by downloaded filename, trace album_id, and aggregate full album
            if filename:
                cur.execute(
                    """
                    SELECT album_id, file_size FROM media 
                    WHERE (download_filename LIKE ? OR original_filename LIKE ? OR url_path LIKE ?) 
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (f"%{filename}%", f"%{filename}%", f"%{filename}%")
                )
                row = cur.fetchone()
                if row:
                    found_album = row[0]
                    if found_album:
                        cur.execute(
                            """
                            SELECT COUNT(*), SUM(file_size) FROM media 
                            WHERE album_id = ? AND file_size > 0
                            """,
                            (found_album,)
                        )
                        alb_row = cur.fetchone()
                        if alb_row and alb_row[0] and alb_row[1]:
                            return int(alb_row[1]), int(alb_row[0])
                    if row[1]:
                        return int(row[1]), 1

    except Exception:
        pass
    return None, None





class CyberdropDlBackend(BaseBackend):
    """
    Subprocess-based cyberdrop-dl adapter for MULTI_DOWNLOADER.
    Handles bulk file lockers, media hosts, and recursive forum threads.
    """

    name: str = "cyberdrop-dl"
    supported_domains: List[str] = [
        "cyberdrop.me", "bunkr.is", "bunkr.si", "bunkr.cr", "bunkr.black", "bunkr.site", "bunkr.ws", "bunkr.ac",
        "gofile.io", "pixeldrain.com", "catbox.moe", "files.catbox.moe",
        "coomer.su", "coomer.party", "kemono.su", "kemono.party",
        "saint.to", "sendvid.com", "streamtape.com", "jpg.church", "puter.com",
        "simpcity.su", "simpcity.to", "f95zone.to", "vipergirls.to", "mega.nz",
        "scrolller.com", "erome.com", "fapello.com", "postimg.cc", "pixl.is", "redgifs.com",
        "imageban.ru", "imgbox.com", "ibb.co", "xbunker.cc", "hotpic.cc", "hotpic2.cc", "2385290.xyz", "myhostdata.space"
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._load_modular_config()

    def _load_modular_config(self) -> None:
        """Loads and merges options from configs/cyberdrop-dl.json if present."""
        cfg_path_str = self.config.get("config_file", "configs/cyberdrop-dl.json")
        cfg_path = Path(cfg_path_str)
        if not cfg_path.is_absolute():
            project_root = Path(__file__).resolve().parent.parent
            cfg_path = project_root / cfg_path

        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if not k.startswith("//") and k not in self.config:
                            self.config[k] = v
            except Exception:
                pass

    def _resolve_executable(self) -> List[str]:
        """
        Locates the cyberdrop-dl binary or python module invocation.
        Checks:
        1. Configured custom executable_path
        2. System PATH via shutil.which('cyberdrop-dl')
        3. ~/.local/bin/cyberdrop-dl.exe
        4. Current Python environment Scripts/
        5. Python -c module fallback
        """
        custom = self.config.get("executable_path")
        if custom and Path(custom).exists():
            return [str(custom)]

        # 1. System PATH
        which_path = shutil.which("cyberdrop-dl") or shutil.which("cyberdrop-dl.exe")
        if which_path:
            return [which_path]

        # 2. User local bin (~/.local/bin)
        user_local = Path.home() / ".local" / "bin"
        for candidate in [user_local / "cyberdrop-dl.exe", user_local / "cyberdrop-dl"]:
            if candidate.exists():
                return [str(candidate)]

        # 3. Virtual environment Scripts directory
        scripts_dir = Path(sys.executable).parent
        for candidate in [scripts_dir / "cyberdrop-dl.exe", scripts_dir / "Scripts" / "cyberdrop-dl.exe"]:
            if candidate.exists():
                return [str(candidate)]

        # 4. Direct Python fallback
        return [sys.executable, "-c", "import sys, cyberdrop_dl.main; sys.exit(cyberdrop_dl.main.main())"]

    def is_installed(self) -> bool:
        """Checks whether cyberdrop-dl binary or python module is available on this system."""
        custom = self.config.get("executable_path")
        if custom and Path(custom).exists():
            return True
        if shutil.which("cyberdrop-dl") or shutil.which("cyberdrop-dl.exe"):
            return True
        user_local = Path.home() / ".local" / "bin"
        for candidate in [user_local / "cyberdrop-dl.exe", user_local / "cyberdrop-dl"]:
            if candidate.exists():
                return True
        scripts_dir = Path(sys.executable).parent
        for candidate in [scripts_dir / "cyberdrop-dl.exe", scripts_dir / "Scripts" / "cyberdrop-dl.exe"]:
            if candidate.exists():
                return True
        import importlib.util
        return importlib.util.find_spec("cyberdrop_dl") is not None

    def can_handle(self, url: str) -> bool:
        """Returns True if the URL targets any supported domain or forum thread."""
        try:
            parsed = urllib.parse.urlparse(url)
            domain = (parsed.netloc or "").lower().split(":")[0]
            if not domain:
                return False
            for d in self.supported_domains:
                if domain == d or domain.endswith("." + d):
                    return True
            if "hotpic" in domain:
                return True
            return False
        except Exception:
            return False

    def _scan_files(self, directory: Path) -> Set[Path]:
        """Returns a set of all existing file paths under a directory."""
        if not directory.exists():
            return set()
        return {p for p in directory.rglob("*") if p.is_file() and not p.name.endswith(('.part', '.lock'))}

    async def extract_info(self, url: str) -> Dict[str, Any]:
        """Extracts basic URL metadata without running full download."""
        parsed = urllib.parse.urlparse(url)
        domain = (parsed.netloc or "").lower().split(":")[0]
        return {
            "url": url,
            "domain": domain,
            "backend": self.name,
            "can_handle": self.can_handle(url),
        }

    def _flatten_loose_files(self, directory: Path) -> None:
        """
        Flattens any 'Loose Files (*)' directory created by cyberdrop-dl
        directly into the target site directory, matching gallery-dl's clean structure.
        """
        try:
            for item in list(directory.glob("Loose Files*")):
                if item.is_dir():
                    for child in list(item.iterdir()):
                        dest = directory / child.name
                        if not dest.exists():
                            shutil.move(str(child), str(dest))
                        elif dest.resolve() != child.resolve():
                            if child.is_dir() and dest.is_dir():
                                for sub in list(child.iterdir()):
                                    sub_dest = dest / sub.name
                                    if not sub_dest.exists():
                                        shutil.move(str(sub), str(sub_dest))
                                    else:
                                        s_stem, s_suffix = sub.stem, sub.suffix
                                        s_counter = 1
                                        while (dest / f"{s_stem}_{s_counter}{s_suffix}").exists():
                                            s_counter += 1
                                        shutil.move(str(sub), str(dest / f"{s_stem}_{s_counter}{s_suffix}"))
                                shutil.rmtree(str(child), ignore_errors=True)
                            else:
                                stem, suffix = child.stem, child.suffix
                                counter = 1
                                while (directory / f"{stem}_{counter}{suffix}").exists():
                                    counter += 1
                                shutil.move(str(child), str(directory / f"{stem}_{counter}{suffix}"))
                    try:
                        shutil.rmtree(str(item), ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass

    def _deduplicate_domain_folder(self, directory: Path) -> None:
        """
        If a nested subfolder with the same name as directory exists (e.g. gofile/gofile),
        moves its contents up into directory and removes the duplicate subfolder.
        """
        try:
            dup = directory / directory.name
            if dup.is_dir() and dup.resolve() != directory.resolve():
                for child in list(dup.iterdir()):
                    dest = directory / child.name
                    if not dest.exists():
                        shutil.move(str(child), str(dest))
                    elif dest.resolve() != child.resolve():
                        if child.is_dir() and dest.is_dir():
                            for inner in list(child.iterdir()):
                                inner_dest = dest / inner.name
                                if not inner_dest.exists():
                                    shutil.move(str(inner), str(inner_dest))
                                else:
                                    i_stem, i_suffix = inner.stem, inner.suffix
                                    i_counter = 1
                                    while (dest / f"{i_stem}_{i_counter}{i_suffix}").exists():
                                        i_counter += 1
                                    shutil.move(str(inner), str(dest / f"{i_stem}_{i_counter}{i_suffix}"))
                            shutil.rmtree(str(child), ignore_errors=True)
                        else:
                            stem, suffix = child.stem, child.suffix
                            counter = 1
                            while (directory / f"{stem}_{counter}{suffix}").exists():
                                counter += 1
                            shutil.move(str(child), str(directory / f"{stem}_{counter}{suffix}"))
                try:
                    shutil.rmtree(str(dup), ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    @exclusive_destination
    async def download(
        self,
        task: DownloadTask,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> ArchiveEntry:
        """
        Executes cyberdrop-dl download via async subprocess.
        Streams stdout, formats terminal output, tracks newly downloaded files,
        reports real-time byte progress, and provides automatic failover to gallery-dl.
        """
        out_dir = Path(task.output_dir)
        if task.options.get('force'):
            out_dir = out_dir / ('forced_' + uuid.uuid4().hex)
        out_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_installed():
            # Check if gallery-dl can handle it as an automatic failover
            if self.config.get("enable_failover_to_gallerydl", True) and not task.options.get("_managed_failover"):
                from backends.gallerydl_backend import GalleryDlBackend
                from core.router import URLRouter
                gdl = GalleryDlBackend()
                if gdl.can_handle(task.url):
                    host_id = URLRouter.get_host_identifier(task.url, self.name)
                    failover_dir = str(out_dir.parent) if out_dir.name.lower() == host_id.lower() else str(out_dir)
                    failover_task = DownloadTask(
                        url=task.url,
                        backend="gallery-dl",
                        output_dir=failover_dir,
                        options=task.options,
                    )
                    return await gdl.download(failover_task, progress_callback=progress_callback)

            raise EngineNotFoundError(
                "cyberdrop-dl is not installed on this system.\n"
                "To install it into your virtual environment, run:\n"
                "  .\\.venv\\Scripts\\pip install cyberdrop-dl\n"
                "Or as an isolated tool:\n"
                "  uv tool install cyberdrop-dl\n"
                "  pipx install cyberdrop-dl"
            )

        exe_cmd = self._resolve_executable()
        cmd = list(exe_cmd) + [
            "download",
            task.url,
            "--ui", "disabled",
            "--no-stats",
            "--no-print-traffic",
            "--download-folder", str(out_dir),
        ]
        state = task.options.get('_native_state')
        if state:
            cmd.extend(['--database-file', str(Path(state) / 'history.db'),
                        '--cache-file', str(Path(state) / 'cache.json')])

        # Directory placement and subfolder controls matching gallery-dl
        if self.config.get("subfolders", True):
            cmd.append("--subfolders")
            if not self.config.get("include_domain_in_subfolders", False):
                cmd.append("--subfolders.include.no-domain")
        else:
            cmd.append("--no-subfolders")

        if self.config.get("separate_posts", False):
            cmd.append("--separate-posts")
        else:
            cmd.append("--no-separate-posts")

        if self.config.get("delete_empty_folders", True):
            cmd.append("--delete-empty-folders")

        rate_limit = self.config.get("rate_limit")
        if rate_limit:
            cmd.extend(["--rate-limit", str(rate_limit)])

        if task.options.get('force') or task.options.get('no_archive') or self.config.get("ignore_history", True):
            cmd.append("--ignore-history")
        if task.options.get('force') or task.options.get('no_archive') or self.config.get("ignore_hashes", True):
            cmd.append("--ignore-hashes")

        # Apply cookies if provided
        cookies = task.options.get("cookies") or self.config.get("cookies_file")
        if cookies:
            c_p = Path(str(cookies))
            if not c_p.is_absolute():
                c_p = PROJECT_ROOT / c_p
            if c_p.exists():
                cmd.extend(["--cookies", str(c_p.resolve())])

        # Apply proxy if provided
        proxy = task.options.get("proxy") or self.config.get("proxy")
        if proxy:
            cmd.extend(["--proxy", str(proxy)])

        # Apply media type filter
        audio_only = bool(task.options.get("audio_only") or task.options.get("extract_audio"))
        video_only = bool(task.options.get("video_only"))
        images_only = bool(task.options.get("images_only") or task.options.get("image_only"))
        if images_only:
            cmd.extend(['--images', '--no-videos', '--no-audio', '--no-non-media'])
        elif video_only:
            cmd.extend(['--videos', '--no-images', '--no-audio', '--no-non-media'])
        elif audio_only:
            cmd.extend(['--audio', '--no-images', '--no-videos', '--no-non-media'])

        # Apply FlareSolverr if configured
        flaresolverr = self.config.get("flaresolverr")
        if flaresolverr:
            cmd.extend(["--flaresolverr", str(flaresolverr)])

        # Take snapshot of files before download to isolate new transfers
        before_files = self._scan_files(out_dir)
        before_state = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in before_files}

        quiet = bool(task.options.get("quiet") or progress_callback is not None)
        if not quiet:
            print(f"{Style.tag('🚀', 'CYBERDROP', Style.MAGENTA)} Launching {Style.engine_badge('cyberdrop-dl')} engine...")

        stdout_lines: List[str] = []
        failover_detected = False
        unsupported_detected = False
        already_skipped = False

        # Setup UTF-8 subprocess environment
        sub_env = dict(os.environ)
        sub_env["PYTHONIOENCODING"] = "utf-8"
        sub_env["PYTHONUTF8"] = "1"

        # Probe expected size from locker API, HEAD headers, or cyberdrop.db before transfer
        probed_bytes, probed_files = await run_blocking(_probe_file_size, task.url, proxy)
        if not probed_bytes:
            probed_bytes, probed_files = await run_blocking(_query_cdl_db_size, url=task.url)

        stop_monitor = asyncio.Event()
        progress_active: Dict[str, Any] = {
            "last_file": None,
            "total_bytes": probed_bytes,
            "total_files": probed_files,
        }

        async def _track_progress():
            last_bytes = 0
            last_time = time.time()
            speed_samples: List[float] = []

            while not stop_monitor.is_set():
                try:
                    part_files = [f for f in out_dir.rglob("*.part") if f.is_file()]
                    completed_files = [
                        f for f in out_dir.rglob("*")
                        if f.is_file() and not f.name.endswith(".part") and f not in before_files
                    ]

                    cur_bytes = sum(f.stat().st_size for f in completed_files)
                    active_name = None

                    if part_files:
                        latest_part = max(part_files, key=lambda f: f.stat().st_mtime)
                        part_size = latest_part.stat().st_size
                        cur_bytes += part_size
                        clean_name = latest_part.name[:-5]
                        active_name = clean_name
                        if clean_name != progress_active["last_file"]:
                            progress_active["last_file"] = clean_name
                            b, count = _query_cdl_db_size(clean_name, task.url)
                            if b and count and count > 1:
                                progress_active["total_bytes"] = b
                                progress_active["total_files"] = count
                            elif b and not progress_active.get("total_bytes"):
                                progress_active["total_bytes"] = b
                                progress_active["total_files"] = count

                        if progress_active.get("total_bytes") is None:
                            b, count = _query_cdl_db_size(clean_name, task.url)
                            if b:
                                progress_active["total_bytes"] = b
                                progress_active["total_files"] = count
                    elif completed_files:
                        active_name = max(completed_files, key=lambda f: f.stat().st_mtime).name

                    # Detect album folder title if files are placed in an album subfolder
                    if not progress_active.get("album_title"):
                        sample_files = part_files or completed_files
                        if sample_files:
                            try:
                                rel_parts = sample_files[0].relative_to(out_dir).parts
                                if len(rel_parts) > 1 and not rel_parts[0].startswith("Loose Files"):
                                    progress_active["album_title"] = rel_parts[0]
                            except Exception:
                                pass

                    now = time.time()
                    dt = now - last_time
                    if dt >= 0.25:
                        inst_speed = max(0.0, (cur_bytes - last_bytes) / dt) if dt > 0 else 0.0
                        speed_samples.append(inst_speed)
                        if len(speed_samples) > 6:
                            speed_samples.pop(0)
                        avg_speed = sum(speed_samples) / len(speed_samples) if speed_samples else 0.0
                        last_bytes = cur_bytes
                        last_time = now

                        total_b = progress_active.get("total_bytes")
                        total_cnt = progress_active.get("total_files")
                        current_file_index = len(completed_files) + (1 if part_files else 0)

                        # If downloaded more than total_b or total_cnt, refresh from database
                        if (
                            (total_cnt and current_file_index > total_cnt)
                            or (total_b and cur_bytes > total_b)
                        ):
                            b_rf, count_rf = _query_cdl_db_size(active_name, task.url)
                            if count_rf and count_rf >= current_file_index:
                                total_cnt = count_rf
                                progress_active["total_files"] = count_rf
                            else:
                                total_cnt = max(total_cnt or 0, current_file_index)
                                progress_active["total_files"] = total_cnt

                            if b_rf and b_rf >= cur_bytes:
                                total_b = b_rf
                                progress_active["total_bytes"] = b_rf
                            else:
                                total_b = max(total_b or 0, cur_bytes)
                                progress_active["total_bytes"] = total_b

                        pct = None
                        eta = None
                        if total_b and total_b > 0:
                            pct = min(100.0, (cur_bytes / total_b) * 100.0)
                            if avg_speed > 0 and cur_bytes < total_b:
                                eta = int((total_b - cur_bytes) / avg_speed)

                        if progress_callback and (cur_bytes > 0 or part_files):
                            from core.router import URLRouter
                            cdl_host = URLRouter.get_host_identifier(task.url, self.name)
                            prog = DownloadProgress(
                                downloaded_bytes=cur_bytes,
                                total_bytes=total_b,
                                speed_bytes_sec=avg_speed,
                                eta_seconds=eta,
                                percent=pct,
                                current_file=progress_active.get("album_title") or active_name,
                                total_files=total_cnt,
                                file_index=current_file_index,
                                status_message=active_name,
                                extractor=cdl_host,
                                container_title=progress_active.get("album_title"),
                            )
                            progress_callback(prog)
                except Exception:
                    pass

                await asyncio.sleep(0.3)

        monitor_task = None
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=sub_env,
                **({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == 'nt' else {'start_new_session': True}),
            )

            monitor_task = asyncio.create_task(_track_progress())

            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                stdout_lines.append(line)

                # Filter and style key log events cleanly
                lower_line = line.lower()
                if "unsupported url" in lower_line:
                    unsupported_detected = True
                    if not quiet:
                        print(f"{Style.tag('⚠️', 'UNSUPPORTED', Style.YELLOW)} {Style.warning(line)}")
                elif (
                    line.startswith("ERROR")
                    or line.startswith("CRITICAL")
                    or "403 forbidden" in lower_line
                    or "anti-bot" in lower_line
                    or "challenge failed" in lower_line
                ):
                    failover_detected = True
                    if not quiet:
                        print(f"{Style.tag('⚠️', 'NOTICE', Style.YELLOW)} {Style.dim(line)}")
                elif "scraping" in lower_line and ("http" in lower_line or "[" in lower_line):
                    if not quiet:
                        target_info = line.split("Scraping", 1)[-1].strip() if "Scraping" in line else line
                        print(f"{Style.tag('🔍', 'SCRAPING', Style.CYAN)} {Style.dim(target_info)}")
                elif "skipping" in lower_line and ("hash" in lower_line or "already" in lower_line):
                    already_skipped = True
                    if not quiet:
                        print(f"{Style.tag('⚠️', 'SKIPPED', Style.YELLOW)} {Style.dim('File already downloaded in database')}")

            await proc.wait()

        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                await terminate_process_tree(proc)
            raise
        except Exception as e:
            raise DownloadFailedError(f"Failed to execute cyberdrop-dl subprocess: {e}")
        finally:
            stop_monitor.set()
            if monitor_task:
                await monitor_task

        # Post-download folder normalization matching gallery-dl structure:
        # 1. Flatten "Loose Files (*)" into out_dir so single files sit cleanly in the site directory
        if self.config.get("flatten_loose_files", True):
            self._flatten_loose_files(out_dir)

        # 2. Prevent/repair double-nested domain folder (e.g. out_dir / out_dir.name)
        self._deduplicate_domain_folder(out_dir)

        # Scan for newly created files
        after_files = self._scan_files(out_dir)
        new_files = sorted([p for p in after_files
                            if p not in before_state or before_state[p] != (p.stat().st_size, p.stat().st_mtime_ns)],
                           key=lambda p: p.stat().st_mtime)

        # ── Automatic Failover Check ──
        if not new_files and (proc.returncode != 0 or failover_detected or unsupported_detected):
            if self.config.get("enable_failover_to_gallerydl", True) and not task.options.get("_is_failover") and not task.options.get("_managed_failover"):
                from backends.gallerydl_backend import GalleryDlBackend
                from core.router import URLRouter
                gdl = GalleryDlBackend()
                if gdl.can_handle(task.url):
                    print(f"\n{Style.tag('🔄', 'FAILOVER', Style.YELLOW)} cyberdrop-dl locker error encountered.")
                    print(f"{Style.tag('⚙️', 'FAILOVER', Style.CYAN)} Attempting automatic fallback with {Style.engine_badge('gallery-dl')}...")
                    host_id = URLRouter.get_host_identifier(task.url, self.name)
                    failover_dir = str(out_dir.parent) if out_dir.name.lower() == host_id.lower() else str(out_dir)
                    failover_opts = dict(task.options)
                    failover_opts["_is_failover"] = True
                    failover_task = DownloadTask(
                        url=task.url,
                        backend="gallery-dl",
                        output_dir=failover_dir,
                        options=failover_opts,
                    )
                    return await gdl.download(failover_task, progress_callback=progress_callback)

            err_details = "\n".join(stdout_lines[-5:]) if stdout_lines else "Process exited with code 1"
            raise DownloadFailedError(f"cyberdrop-dl download failed:\n{err_details}")

        # Partial failure recovery: some files transferred but download errors occurred on others
        if new_files and (proc.returncode != 0 or failover_detected) and self.config.get("enable_failover_to_gallerydl", True) and not task.options.get("_is_failover") and not task.options.get("_managed_failover"):
            from backends.gallerydl_backend import GalleryDlBackend
            from core.router import URLRouter
            gdl = GalleryDlBackend()
            if gdl.can_handle(task.url):
                print(f"\n{Style.tag('⚠️', 'PARTIAL ERROR', Style.YELLOW)} cyberdrop-dl dropped files during batch/album download.")
                print(f"{Style.tag('⚙️', 'FAILOVER', Style.CYAN)} Passing to {Style.engine_badge('gallery-dl')} to ensure all files are completely retrieved...")
                host_id = URLRouter.get_host_identifier(task.url, self.name)
                failover_dir = str(out_dir.parent) if out_dir.name.lower() == host_id.lower() else str(out_dir)
                failover_opts = dict(task.options)
                failover_opts["_is_failover"] = True
                failover_task = DownloadTask(
                    url=task.url,
                    backend="gallery-dl",
                    output_dir=failover_dir,
                    options=failover_opts,
                )
                return await gdl.download(failover_task, progress_callback=progress_callback)

        if proc.returncode != 0 or failover_detected:
            raise DownloadFailedError('Cyberdrop reported an incomplete download; completed files were preserved.')

        if not new_files:
            if already_skipped:
                return ArchiveEntry(
                    id=str(uuid.uuid4()),
                    url=task.url,
                    file_path=str(out_dir.resolve()),
                    file_name="cyberdrop-dl",
                    file_hash=None,
                    file_size=0,
                    backend=self.name,
                    source_site="cyberdrop",
                    media_type=MediaType.IMAGE,
                    metadata={"all_skipped": True},
                )
            raise DownloadFailedError("cyberdrop-dl completed but no downloaded files were found.")

        primary_file = new_files[0]
        total_size = sum(f.stat().st_size for f in new_files if f.exists())
        file_count = len(new_files)

        # Final progress update on completion
        if progress_callback and total_size > 0:
            prog = DownloadProgress(
                downloaded_bytes=total_size,
                total_bytes=total_size,
                speed_bytes_sec=0.0,
                eta_seconds=0,
                percent=100.0,
                current_file=primary_file.name,
                total_files=file_count,
                file_index=file_count,
            )
            progress_callback(prog)
            print()  # Finalize the carriage return line

        file_hash = await run_blocking(ArchiveManager.calculate_file_hash, primary_file)

        final_name = primary_file.name if file_count == 1 else f"{primary_file.parent.name} ({file_count} files)"

        return ArchiveEntry(
            id=str(uuid.uuid4()),
            url=task.url,
            file_path=str(primary_file.resolve()),
            file_name=final_name,
            file_hash=file_hash,
            file_size=total_size,
            backend=self.name,
            source_site="cyberdrop",
            media_type=_detect_media_type(primary_file.name),
            metadata={
                "total_downloaded": file_count,
                "engine": self.name,
            },
        )
