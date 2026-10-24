"""
Adapter backend wrapping yt-dlp via its official Python API.
Ensures total config isolation by passing ignoreconfig=True.
"""

import asyncio
from pathlib import Path
import re
import shutil
import sys
import threading
from typing import Any, Callable, Dict, Optional
import uuid

import yt_dlp

from backends.base import BaseBackend
from core.exceptions import DownloadFailedError
from core.workers import run_blocking
from core.models import ArchiveEntry, DownloadProgress, DownloadTask, MediaType
from core.pot_manager import POTManager
from core.terminal import Style


class YtDlpStatusLogger:
    """Formats yt-dlp extraction and challenge events into concise terminal status messages."""

    def __init__(self, quiet: bool = False, on_status: Optional[Callable[[str], None]] = None):
        self.quiet = quiet
        self.on_status = on_status
        self._last_msg = ""
        self.last_error = ""
        self.current_status = ""
        self.already_archived = False

    def _safe_print(self, text: str):
        if self.quiet:
            return
        if text != self._last_msg:
            self._last_msg = text
            try:
                sys.stdout.write(f"\r\033[K{text}\n")
                sys.stdout.flush()
            except UnicodeEncodeError:
                ascii_text = text.encode("ascii", "replace").decode("ascii")
                print(ascii_text)
            except Exception:
                print(text)

    def _handle_log(self, msg: str):
        status = None
        if "has already been recorded in the archive" in msg or "has already been downloaded" in msg:
            self.already_archived = True
            status = "SKIPPED: Already recorded in archive"
            self._safe_print(f"{Style.tag('⚠️', 'ARCHIVE', Style.YELLOW)} Video already recorded in download archive.")
        elif "[pot:bgutil:http]" in msg:
            status = "PO-TOKEN: Generating token via sidecar"
            self._safe_print(f"{Style.tag('⚙️', 'PO-TOKEN', Style.MAGENTA)} Generating Proof-of-Origin token via sidecar...")
        elif "[jsc:" in msg or "Solving JS challenges" in msg:
            status = "CHALLENGE: Solving JS challenge"
            self._safe_print(f"{Style.tag('⚡', 'CHALLENGE', Style.YELLOW)} Solving JavaScript challenge...")
        elif "Downloading webpage" in msg:
            status = "METADATA: Fetching media webpage"
            self._safe_print(f"{Style.tag('🌐', 'METADATA', Style.CYAN)} Fetching media webpage...")
        elif "Downloading player" in msg:
            status = "PLAYER: Loading YouTube player"
            self._safe_print(f"{Style.tag('📜', 'PLAYER', Style.BLUE)} Loading YouTube player scripts...")
        elif "Downloading initial data" in msg or "Downloading visionos" in msg or "Downloading web" in msg:
            status = "EXTRACTOR: Querying player APIs"
            self._safe_print(f"{Style.tag('🔍', 'EXTRACTOR', Style.CYAN)} Querying YouTube player APIs...")
        elif "Downloading m3u8" in msg or "Downloading MPD" in msg:
            status = "STREAMS: Resolving DASH/HLS manifests"
            self._safe_print(f"{Style.tag('📡', 'STREAMS', Style.BLUE)} Resolving adaptive DASH/HLS stream manifests...")
        elif "[download] Destination:" in msg:
            filename = msg.split("Destination:")[-1].strip()
            status = f"Preparing: {Path(filename).name}"
            self._safe_print(f"{Style.tag('🎯', 'TARGET', Style.YELLOW)} Preparing output stream: {Style.white(Path(filename).name)}")

        if status:
            self.current_status = status
            if self.on_status:
                try:
                    self.on_status(status)
                except Exception:
                    pass

    def debug(self, msg: str):
        self._handle_log(msg)

    def info(self, msg: str):
        self._handle_log(msg)

    def warning(self, msg: str):
        # Filter noisy yt-dlp internal fallbacks and benign client impersonation warnings
        noisy_patterns = (
            "Incomplete data",
            "unable to extract yt initial data",
            "no impersonate target is available",
        )
        if not any(p in msg for p in noisy_patterns):
            self._safe_print(f"{Style.tag('⚠️', 'WARNING', Style.YELLOW)} {Style.yellow(msg)}")

    def error(self, msg: str):
        self.last_error = msg
        if not self.quiet:
            self._safe_print(f"{Style.tag('❌', 'YT-DLP ERROR', Style.RED)} {Style.red(msg)}")


class YtDlpBackend(BaseBackend):
    """Integrates yt-dlp cleanly via Python API with isolated configuration."""

    name: str = "yt-dlp"

    _init_lock = threading.Lock()
    _pre_initialized = False

    @classmethod
    def pre_initialize(cls) -> None:
        """Pre-initializes yt-dlp plugin extractors serially to prevent multi-threading registration race conditions."""
        with cls._init_lock:
            if not cls._pre_initialized:
                try:
                    from core.pot_security import harden_provider, provider_token
                    bundled = Path(__file__).resolve().parent.parent / 'tools' / 'pot-provider'
                    if (bundled / 'server' / 'build' / 'main.js').exists():
                        harden_provider(bundled / 'server')
                        provider_token()
                        from yt_dlp.globals import plugin_dirs
                        if str(bundled) not in plugin_dirs.value:
                            plugin_dirs.value.insert(0, str(bundled))
                    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as _:
                        pass
                except Exception:
                    pass
                cls._pre_initialized = True

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.pre_initialize()

    def _is_youtube(self, url: str) -> bool:
        """Determines if the given URL targets YouTube."""
        u = url.lower()
        return "youtube.com" in u or "youtu.be" in u

    def _ensure_po_provider_if_needed(self, url: str, silent: bool = False) -> None:
        """Auto-starts local PO Token provider sidecar if downloading from YouTube."""
        if self._is_youtube(url) and self.config.get("auto_start_po_provider", True):
            host = self.config.get("po_provider_host", "127.0.0.1")
            port = self.config.get("po_provider_port", 4416)
            POTManager.ensure_server_running(host=host, port=port, silent=silent)

    def _resolve_ffmpeg(self) -> Optional[str]:
        """Locates ffmpeg from config or checks standard paths."""
        configured_path = self.config.get("ffmpeg_location")
        if configured_path and Path(configured_path).exists():
            return configured_path
        
        # Check standard PATH
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg
            
        # Check common local path in user directory
        local_candidate = Path.home() / "yt-dlp" / "ffmpeg.exe"
        if local_candidate.exists():
            return str(local_candidate)

        return None

    def can_handle(self, url: str) -> bool:
        """Check if yt-dlp has an extractor capable of handling this URL."""
        # Yield Telegram URLs exclusively to telegram-dl backend
        url_lower = url.lower()
        if "t.me/" in url_lower or "telegram.me/" in url_lower:
            return False

        classes = yt_dlp.extractor.gen_extractor_classes()
        for ie_class in classes:
            if ie_class.suitable(url) and ie_class.IE_NAME not in ("generic", "telegram:embed"):
                return True
        return False

    @staticmethod
    def _is_auth_error(err_str: str, is_youtube: bool = False) -> bool:
        """Checks if a yt-dlp error indicates that authentication/cookies are required."""
        lowered = err_str.lower()
        triggers = [
            "sign in",
            "please sign in",
            "confirm your age",
            "age-restricted",
            "inappropriate",
            "age gate",
            "requires authentication",
            "login_required",
            "cookies-from-browser",
            "cookies for the authentication",
            "private video",
            "members-only",
            "bot detection",
            "confirm you're not a bot",
        ]
        # Only check generic 403/429 for non-YouTube sites; on YouTube, browser cookies cause 360p resolution caps
        if not is_youtube:
            triggers.extend(["http error 429", "too many requests", "http error 403", "forbidden"])
        return any(t in lowered for t in triggers)

    @staticmethod
    def _is_waf_or_tls_error(err_str: str) -> bool:
        """Checks if a yt-dlp error indicates a WAF, Cloudflare bot-challenge, or TLS connection reset."""
        lowered = err_str.lower()
        triggers = [
            "connectionreseterror",
            "10054",
            "connection was forcibly closed",
            "connection reset",
            "connection aborted",
            "cloudflare",
            "cf_clearance",
            "just a moment",
            "blocked by cloudflare",
            "turnstile",
            "handshake failure",
            "transport error",
            "unable to download json metadata",
        ]
        return any(t in lowered for t in triggers)

    @staticmethod
    def _resolve_impersonate_target(target_name: Optional[Any]) -> Optional[Any]:
        """Resolves target string (e.g. 'chrome', 'firefox') into yt-dlp ImpersonateTarget."""
        if not target_name:
            return None
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            if isinstance(target_name, ImpersonateTarget):
                return target_name
            return ImpersonateTarget.from_str(str(target_name).strip().lower())
        except Exception:
            return None

    def _load_config_file_opts(self) -> Dict[str, Any]:
        """Loads options from the project's modular yt-dlp config file (e.g. configs/yt-dlp.conf)."""
        cfg_path_str = self.config.get("config_file", "configs/yt-dlp.conf")
        cfg_path = Path(cfg_path_str)
        if not cfg_path.is_absolute():
            project_root = Path(__file__).resolve().parent.parent
            cfg_path = project_root / cfg_path
        if not cfg_path.exists():
            return {}
        try:
            _, _, _, file_opts = yt_dlp.parse_options(["--config-locations", str(cfg_path.resolve())])
            # Remove output template and error swallowing flags so MULTI_DOWNLOADER controls orchestration
            file_opts.pop("outtmpl", None)
            file_opts.pop("ignoreerrors", None)
            return file_opts
        except Exception:
            return {}

    async def extract_info(self, url: str) -> Dict[str, Any]:
        """Extract metadata asynchronously without downloading the media."""
        await run_blocking(self._ensure_po_provider_if_needed, url)
        # Built-in defaults: bypass GVS PO token skips for age-restricted / DASH formats
        extractor_args = {
            "youtube": {
                "formats": ["missing_pot"]
            }
        }
        cfg_extractor_args = self.config.get("extractor_args")
        if isinstance(cfg_extractor_args, dict):
            for k, v in cfg_extractor_args.items():
                if k not in extractor_args:
                    extractor_args[k] = v
                elif isinstance(extractor_args[k], dict) and isinstance(v, dict):
                    extractor_args[k].update(v)

        opts = self._load_config_file_opts()
        if self.config.get('proxy'):
            opts['proxy'] = self.config['proxy']
        opts.update({
            "logger": YtDlpStatusLogger(),
            "ignoreconfig": True,  # Prevent reading system %APPDATA%/yt-dlp/config
            "skip_download": True,
            "remote_components": ["ejs:github"],
            "extractor_args": extractor_args,
        })

        ffmpeg_bin = self._resolve_ffmpeg()
        if ffmpeg_bin:
            opts["ffmpeg_location"] = ffmpeg_bin

        cookies_browser = self.config.get("cookies_from_browser")
        if cookies_browser:
            opts["cookiesfrombrowser"] = (cookies_browser,)

        imp_target = self._resolve_impersonate_target(self.config.get("impersonate"))
        if imp_target:
            opts["impersonate"] = imp_target

        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            return await run_blocking(_extract)
        except Exception as e:
            err_str = str(e)
            fallback_browser = self.config.get("fallback_browser_cookies")
            is_yt = self._is_youtube(url)

            # Retry with browser impersonation on Cloudflare / WAF / TLS connection reset
            if "impersonate" not in opts and self._is_waf_or_tls_error(err_str):
                browser_pref = self.config.get("cookies_from_browser") or fallback_browser or "chrome"
                target_name = "firefox" if "firefox" in str(browser_pref).lower() else "chrome"
                fallback_imp = self._resolve_impersonate_target(target_name)
                if fallback_imp:
                    opts["impersonate"] = fallback_imp
                    try:
                        return await run_blocking(_extract)
                    except Exception as waf_retry_err:
                        err_str = f"{str(waf_retry_err)} {err_str}".strip()

            if not cookies_browser and fallback_browser and self._is_auth_error(err_str, is_youtube=is_yt):
                opts["cookiesfrombrowser"] = (fallback_browser,)
                try:
                    return await run_blocking(_extract)
                except Exception as retry_err:
                    raise DownloadFailedError(f"yt-dlp metadata extraction failed with fallback cookies: {retry_err}")
            raise DownloadFailedError(f"yt-dlp metadata extraction failed: {e}")

    async def download(
        self,
        task: DownloadTask,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> ArchiveEntry:
        """Downloads media using yt-dlp with real-time progress callbacks."""
        quiet = bool(task.options.get("quiet") or progress_callback is not None)
        await run_blocking(self._ensure_po_provider_if_needed, task.url, silent=quiet)
        cancel_event = threading.Event()
        out_dir = Path(task.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        downloaded_filepath = None
        announced_media = False

        def _on_logger_status(status_str: str):
            if progress_callback:
                progress_callback(
                    DownloadProgress(
                        status_message=status_str,
                        total_files=1,
                        file_index=1,
                    )
                )

        status_logger = YtDlpStatusLogger(quiet=quiet, on_status=_on_logger_status)

        def _yt_progress_hook(d: Dict[str, Any]):
            if cancel_event.is_set():
                raise DownloadFailedError('Download interrupted')
            nonlocal downloaded_filepath, announced_media
            try:
                if d.get("status") == "downloading":
                    info_dict = d.get("info_dict", {})
                    title = info_dict.get("title") or (Path(d.get("filename", "")).name if d.get("filename") else "")
                    if not announced_media:
                        announced_media = True
                        if not quiet:
                            res = info_dict.get("resolution") or (f"{info_dict.get('height')}p" if info_dict.get("height") else "")
                            fmt_id = info_dict.get("format_id")
                            if title:
                                print(f"{Style.tag('🎬', 'MEDIA', Style.YELLOW)} {Style.white(title)}")
                            if res or fmt_id:
                                print(f"{Style.tag('🎞️', 'FORMAT', Style.CYAN)} Stream: {Style.white(f'{fmt_id} ({res})')}")

                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or 0
                    speed = d.get("speed")
                    eta = d.get("eta")

                    stream_file = Path(d.get("filename", "")).name if d.get("filename") else ""
                    active_status = stream_file or status_logger.current_status or "Downloading media"

                    info_dict = d.get("info_dict") or {}
                    raw_ext = info_dict.get("extractor_key") or info_dict.get("extractor") or ""
                    channel_or_pl = info_dict.get("playlist_title") or info_dict.get("channel") or info_dict.get("uploader")

                    prog = DownloadProgress(
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                        speed_bytes_sec=speed,
                        eta_seconds=eta,
                        current_file=title or d.get("filename"),
                        total_files=1,
                        file_index=1,
                        status_message=active_status,
                        extractor=raw_ext.lower() if raw_ext else None,
                        container_title=channel_or_pl or title,
                    )
                    prog.update_percent()
                    if progress_callback:
                        progress_callback(prog)

                elif d.get("status") == "finished":
                    downloaded_filepath = d.get("filename")
                    if not quiet:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
            except Exception:
                pass

        # Built-in defaults: bypass GVS PO token skips for age-restricted / DASH formats
        extractor_args = {
            "youtube": {
                "formats": ["missing_pot"]
            }
        }
        cfg_extractor_args = self.config.get("extractor_args")
        if isinstance(cfg_extractor_args, dict):
            for k, v in cfg_extractor_args.items():
                if k not in extractor_args:
                    extractor_args[k] = v
                elif isinstance(extractor_args[k], dict) and isinstance(v, dict):
                    extractor_args[k].update(v)

        task_extractor_args = task.options.get("extractor_args")
        if isinstance(task_extractor_args, dict):
            for k, v in task_extractor_args.items():
                if k not in extractor_args:
                    extractor_args[k] = v
                elif isinstance(extractor_args[k], dict) and isinstance(v, dict):
                    extractor_args[k].update(v)

        audio_only = bool(task.options.get("audio_only") or task.options.get("extract_audio"))
        video_only = bool(task.options.get("video_only"))
        res_opt = task.options.get("resolution") or self.config.get("preferred_resolution")

        ydl_opts = self._load_config_file_opts()

        from core.router import resolve_format_spec
        resolved_fmt = task.options.get("format")
        if not resolved_fmt:
            resolved_fmt = resolve_format_spec(
                format_str=None,
                resolution=res_opt,
                audio_only=audio_only,
                video_only=video_only,
            )
        if not resolved_fmt:
            resolved_fmt = self.config.get("format", ydl_opts.get("format", "bestvideo*+bestaudio/best"))

        ydl_opts.update({
            "ignoreconfig": True,  # Complete isolation from standalone yt-dlp configs
            "ignoreerrors": False, # Ensure exceptions are raised so smart fallbacks trigger
            "logger": status_logger,
            "noprogress": True,   # Suppress internal yt-dlp stdout progress bar
            "format": resolved_fmt,
            "merge_output_format": task.options.get("merge_output_format", self.config.get("merge_output_format", ydl_opts.get("merge_output_format", "mp4"))),
            "outtmpl": str(out_dir / "%(title)s [%(id)s].%(ext)s"),
            "progress_hooks": [_yt_progress_hook],
            "remote_components": ["ejs:github"],  # Automatic JS challenge solver for age-restricted / n-sig
            "extractor_args": extractor_args,
            "socket_timeout": self.config.get('timeout', 30),
            "retries": 4,
            "fragment_retries": 4,
        })

        if audio_only:
            audio_codec = task.options.get("audio_format", "mp3")
            postprocessors = list(ydl_opts.get("postprocessors") or [])
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_codec,
                "preferredquality": "192",
            })
            ydl_opts["postprocessors"] = postprocessors

        proxy = task.options.get("proxy") or self.config.get("proxy")
        if proxy:
            ydl_opts["proxy"] = str(proxy)
        # Configure download archive if enabled
        archive_path = task.options.get("download_archive", self.config.get("download_archive", "data/archives/ytdlp_archive.txt"))
        if task.options.get("force"):
            ydl_opts["overwrites"] = True
            ydl_opts['post_overwrites'] = True

        if task.options.get("no_archive") or task.options.get("force"):
            ydl_opts.pop("download_archive", None)
            ydl_opts["download_archive"] = None
        elif archive_path:
            arc_p = Path(archive_path)
            if not arc_p.is_absolute():
                project_root = Path(__file__).resolve().parent.parent
                arc_p = project_root / arc_p
            arc_p.parent.mkdir(parents=True, exist_ok=True)
            ydl_opts["download_archive"] = str(arc_p.resolve())

        if quiet:
            ydl_opts["quiet"] = True
            ydl_opts["no_warnings"] = True

        # Post-processor hook to capture final merged file (e.g. .mp4 instead of temp .webm/.m4a)
        final_filepath = None
        def _yt_postprocessor_hook(d: Dict[str, Any]):
            nonlocal final_filepath
            if d.get("status") == "started":
                postprocessor = d.get("postprocessor", "")
                msg = f"FFMPEG: Processing ({postprocessor})" if postprocessor else "FFMPEG: Merging formats into MP4"
                status_logger.current_status = msg
                if progress_callback:
                    progress_callback(DownloadProgress(status_message=msg, total_files=1, file_index=1, percent=100.0))
            elif d.get("status") == "finished":
                merged = d.get("info_dict", {}).get("_filename")
                if merged and Path(merged).exists():
                    final_filepath = merged
                status_logger.current_status = "FFMPEG: Merge completed"
                if progress_callback:
                    progress_callback(DownloadProgress(status_message="FFMPEG: Merge completed", total_files=1, file_index=1, percent=100.0))

        ydl_opts["postprocessor_hooks"] = [_yt_postprocessor_hook]

        # Resolve and bind ffmpeg
        ffmpeg_bin = self._resolve_ffmpeg()
        if ffmpeg_bin:
            ydl_opts["ffmpeg_location"] = ffmpeg_bin

        # Optional Cookies (Browser or cookie file)
        cookies_browser = task.options.get("cookies_from_browser", self.config.get("cookies_from_browser"))
        if cookies_browser:
            ydl_opts["cookiesfrombrowser"] = (cookies_browser,)

        cookie_file = task.options.get("cookies", self.config.get("cookies_file"))
        if cookie_file:
            cf_p = Path(cookie_file)
            if not cf_p.is_absolute():
                project_root = Path(__file__).resolve().parent.parent
                cf_p = project_root / cf_p
            if cf_p.exists():
                ydl_opts["cookiefile"] = str(cf_p.resolve())

        # Optional Impersonation Target (e.g. chrome, firefox)
        user_imp = task.options.get("impersonate", self.config.get("impersonate"))
        imp_target = self._resolve_impersonate_target(user_imp)
        if imp_target:
            ydl_opts["impersonate"] = imp_target

        ydl_opts['concurrent_fragment_downloads'] = self.config.get('concurrent_fragments', 1)
        if self.config.get('sponsorblock'):
            ydl_opts.setdefault('postprocessors', []).extend([
                {'key': 'SponsorBlock', 'categories': ['sponsor']},
                {'key': 'ModifyChapters', 'remove_sponsor_segments': ['sponsor']},
            ])

        # Optional Rate Limit
        rate_limit = task.options.get("rate_limit_bytes", self.config.get("rate_limit_bytes"))
        if rate_limit:
            ydl_opts["ratelimit"] = rate_limit

        # Add optional post-processing settings from config
        if self.config.get("embed_metadata", True):
            ydl_opts.setdefault("postprocessors", []).append({"key": "FFmpegMetadata"})
        if self.config.get("embed_thumbnail", False):
            ydl_opts["writethumbnail"] = True
            ydl_opts.setdefault("postprocessors", []).append({"key": "EmbedThumbnail"})
        if self.config.get("write_subtitles", False):
            ydl_opts["writesubtitles"] = True
            ydl_opts.setdefault("postprocessors", []).append({"key": "FFmpegEmbedSubtitle"})

        def _run_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(task.url, download=True)
                if not info and status_logger.last_error:
                    raise DownloadFailedError(status_logger.last_error)
                return info

        try:
            info = await run_blocking(_run_download, cancel_event=cancel_event)
        except Exception as e:
            err_str = f"{str(e)} {status_logger.last_error}".strip()
            fallback_browser = self.config.get("fallback_browser_cookies")
            user_provided_cookies = task.options.get("cookies_from_browser") or task.options.get("cookies")
            is_yt = self._is_youtube(task.url)

            # 1. Handle subtitle rate-limiting or failures first without falling back to cookies
            if "subtitles" in err_str.lower():
                sub_msg = "SUBTITLES: Retrying without subtitles"
                status_logger.current_status = sub_msg
                if progress_callback:
                    progress_callback(DownloadProgress(status_message=sub_msg, total_files=1, file_index=1))
                if not quiet:
                    print(f"\n{Style.tag('⚠️', 'SUBTITLES', Style.YELLOW)} Subtitle download failed/rate-limited. Retrying without subtitles...")
                ydl_opts["writesubtitles"] = False
                ydl_opts["writeautomaticsub"] = False
                try:
                    info = await run_blocking(_run_download, cancel_event=cancel_event)
                    err_str = None
                except Exception as sub_retry_err:
                    err_str = f"{str(sub_retry_err)} {status_logger.last_error}".strip()

            # 2. Handle Cloudflare / WAF / TLS connection reset with browser impersonation fallback
            if err_str and "impersonate" not in ydl_opts and self._is_waf_or_tls_error(err_str):
                browser_pref = task.options.get("cookies_from_browser") or self.config.get("cookies_from_browser") or fallback_browser or "chrome"
                target_name = "firefox" if "firefox" in str(browser_pref).lower() else "chrome"
                fallback_imp = self._resolve_impersonate_target(target_name)
                if fallback_imp:
                    waf_msg = f"WAF: Retrying with {target_name} browser impersonation"
                    status_logger.current_status = waf_msg
                    if progress_callback:
                        progress_callback(DownloadProgress(status_message=waf_msg, total_files=1, file_index=1))
                    if not quiet:
                        print(f"\n{Style.tag('🛡️', 'WAF-BYPASS', Style.YELLOW)} Cloudflare/TLS reset detected. Retrying with {Style.bold(target_name)} impersonation...")
                    ydl_opts["impersonate"] = fallback_imp
                    try:
                        info = await run_blocking(_run_download, cancel_event=cancel_event)
                        err_str = None
                    except Exception as waf_retry_err:
                        err_str = f"{str(waf_retry_err)} {status_logger.last_error}".strip()

            # 2b. Handle YouTube transient HTTP 403 Forbidden (PO Token warm-up or CDN token refresh race)
            if err_str and is_yt and ("HTTP Error 403" in err_str or "403: Forbidden" in err_str):
                retry_403_msg = "RETRY: YouTube HTTP 403 Forbidden. Refreshing stream token..."
                status_logger.current_status = retry_403_msg
                if progress_callback:
                    progress_callback(DownloadProgress(status_message=retry_403_msg, total_files=1, file_index=1))
                if not quiet:
                    print(f"\n{Style.tag('🔄', 'RETRY', Style.YELLOW)} YouTube HTTP 403 Forbidden detected. Refreshing token and retrying...")
                await asyncio.sleep(2.0)
                try:
                    info = await run_blocking(_run_download, cancel_event=cancel_event)
                    err_str = None
                except Exception as retry_403_err:
                    err_str = f"{str(retry_403_err)} {status_logger.last_error}".strip()

            # 3. Handle actual sign-in / age-restriction requirements with fallback cookies
            if err_str and not user_provided_cookies and fallback_browser and self._is_auth_error(err_str, is_youtube=is_yt):
                auth_msg = f"AUTH: Retrying with {fallback_browser} cookies"
                status_logger.current_status = auth_msg
                if progress_callback:
                    progress_callback(DownloadProgress(status_message=auth_msg, total_files=1, file_index=1))
                if not quiet:
                    print(f"\n{Style.tag('⚠️', 'AUTH-REQUIRED', Style.YELLOW)} Media requires sign-in. Automatically retrying with {Style.bold(fallback_browser)} cookies...")
                ydl_opts["cookiesfrombrowser"] = (fallback_browser,)
                try:
                    info = await run_blocking(_run_download, cancel_event=cancel_event)
                    err_str = None
                except Exception as retry_err:
                    raise DownloadFailedError(f"yt-dlp download failed with fallback {fallback_browser} cookies: {retry_err}")
            elif err_str:
                raise DownloadFailedError(f"yt-dlp download failed: {err_str}")

        # Resolve downloaded file location: check postprocessor first, then finished hook
        actual_path = final_filepath or downloaded_filepath
        if not actual_path and info:
            actual_path = info.get("_filename")
        if not actual_path and info and "requested_downloads" in info:
            actual_path = info["requested_downloads"][0].get("filepath")

        path_obj = Path(actual_path) if actual_path else None

        # If path points to an intermediate fragment/stream, check if the merged .mp4 exists
        if path_obj and (not path_obj.exists() or re.search(r'\.f[a-zA-Z0-9_-]+$', path_obj.stem)):
            target_ext = ydl_opts.get("merge_output_format", "mp4")
            clean_stem = re.sub(r'\.f[a-zA-Z0-9_-]+$', '', path_obj.stem)
            candidate = out_dir / f"{clean_stem}.{target_ext}"
            if candidate.exists():
                path_obj = candidate
            elif (out_dir / f"{clean_stem}{path_obj.suffix}").exists():
                path_obj = out_dir / f"{clean_stem}{path_obj.suffix}"

        if not path_obj or not path_obj.exists() or not path_obj.is_file():
            if status_logger.already_archived:
                clean_title = info.get("title", "Video") if info else "Video"
                return ArchiveEntry(
                    id=str(uuid.uuid4()),
                    url=task.url,
                    file_path=str(out_dir),
                    file_name=clean_title,
                    file_hash=None,
                    file_size=0,
                    backend=self.name,
                    source_site=info.get("extractor_key") or info.get("extractor") if info else "unknown",
                    media_type=MediaType.VIDEO,
                    metadata={"all_skipped": True, "title": clean_title},
                )
            err_msg = status_logger.last_error or "yt-dlp completed without producing a valid media file."
            raise DownloadFailedError(err_msg)

        file_size = path_obj.stat().st_size if path_obj.is_file() else None

        # Clean title fallback
        title = info.get("title", path_obj.stem) if info else path_obj.stem
        uploader = info.get("uploader", "Unknown") if info else "Unknown"
        is_skipped = bool(status_logger.already_archived) and not task.options.get("force")

        return ArchiveEntry(
            id=str(uuid.uuid4()),
            url=task.url,
            file_path=str(path_obj.resolve()),
            file_name=path_obj.name,
            file_hash=None,  # Hashed afterwards by ArchiveManager
            file_size=file_size,
            backend=self.name,
            source_site=info.get("extractor_key") or info.get("extractor") if info else "unknown",
            media_type=MediaType.VIDEO if (info and info.get("vcodec") != "none") else MediaType.AUDIO,
            metadata={
                "title": title,
                "uploader": uploader,
                "duration": info.get("duration") if info else None,
                "id": info.get("id") if info else None,
                "resolution": info.get("resolution") if info else None,
                "all_skipped": is_skipped,
            },
        )
