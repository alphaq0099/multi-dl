"""
Adapter backend wrapping Terabox-dl for MULTI_DOWNLOADER.
Supports single files, multi-file shares, and directory folders with resume, 
chunked streaming, and in-place dual progress bars.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse
import uuid

import requests
from dotenv import load_dotenv

from backends.base import BaseBackend
from core.exceptions import AuthenticationError, DownloadFailedError, EngineNotFoundError
from core.models import ArchiveEntry, DownloadProgress, DownloadTask, MediaType
from core.files import safe_name, contained_path
from core.storage import file_lock, atomic_json_write
from core.exceptions import ConfigError
from core.workers import run_blocking
from core.archive import ArchiveManager
from core.terminal import Style, format_bytes

APP_ID = "250528"
BASE_URL = "https://www.terabox.com"

TERABOX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.terabox.com/",
    "X-Requested-With": "XMLHttpRequest",
}

ERROR_MESSAGES = {
    -1: "Server error or rate limited. Try again later.",
    -3: "Invalid or missing parameters in request.",
    -6: "Share link has expired or been removed.",
    -7: "File or share requires a password.",
    -9: "File does not exist or has been deleted.",
    -12: "Insufficient storage space on your account.",
    -20: "Session expired. Please refresh your ndus cookie.",
    -21: "Share link has been banned/restricted.",
    -32: "Exceeded download frequency limit. Wait and retry.",
    -33: "File too large for free-tier download.",
    2: "Download link expired. Re-fetching...",
    4: "Request too frequent. Please wait.",
    12: "Access denied — cookie may be invalid or expired.",
    31: "Sign/token verification failed. Cookie may need refresh.",
    105: "Invalid share link format.",
    112: "Session expired or cookie invalid. Please re-authenticate.",
    118: "Download quota exceeded for this file.",
    400141: "TeraBox security verification required ('need verify'). A valid ndus cookie is required.",
    400210: "TeraBox security verification required ('need verify_v2'). Fresh ndus cookie or verification required.",
    4000023: "jsToken expired. Auto-refreshing...",
}


def _get_error_message(code: int) -> str:
    return ERROR_MESSAGES.get(code, f"TeraBox API error (code {code})")


def _get_safe_filename(name: Optional[str]) -> str:
    if not name:
        return "unnamed_file"
    # Remove invalid Windows filesystem characters: < > : " / \ | ? *
    return safe_name(name)


def _detect_media_type(filename: str) -> MediaType:
    ext = Path(filename).suffix.lower()
    if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v"):
        return MediaType.VIDEO
    if ext in (".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus"):
        return MediaType.AUDIO
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"):
        return MediaType.IMAGE
    if ext in (".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".txt", ".epub"):
        return MediaType.DOCUMENT
    return MediaType.UNKNOWN


class _TeraboxBatchTracker:
    """
    Renders an in-place dual progress bar for multi-file TeraBox shares:
    Line 1 (Top): Batch progress (e.g. 2/5 files • 85.30 MB ( 40.0%))
    Line 2 (Bottom): Active file progress (speed, ETA, sizes, filename)
    Strictly budgets visible columns to prevent terminal line-wrapping cascades.
    """

    def __init__(self, total_files: int, total_batch_bytes: int = 0):
        self.total_files: int = total_files
        self.completed_count: int = 0
        self.skipped_count: int = 0
        self.total_batch_bytes: int = total_batch_bytes
        self._file_bytes: Dict[str, int] = {}

        self.current_filename: str = ""
        self.current_downloaded: int = 0
        self.current_total: Optional[int] = None
        self.current_speed: Optional[float] = None
        self.current_eta: Optional[int] = None

        self._lines_printed: int = 0
        self._is_tty: bool = sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else True

    @staticmethod
    def _visible_len(s: str) -> int:
        clean = re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', s)
        extra = sum(1 for ch in clean if ord(ch) > 0x1F000 or ch in "🖼📥📦🎬📁🚀⚠️❌✅•")
        return len(clean) + extra

    def update_file(
        self,
        filename: str,
        downloaded: int,
        total: Optional[int],
        speed: Optional[float],
        eta: Optional[int],
    ) -> None:
        self.current_filename = filename
        self.current_downloaded = downloaded
        self.current_total = total
        self.current_speed = speed
        self.current_eta = eta
        self._file_bytes[filename] = downloaded
        self._render()

    def finish_file(self, filename: str, final_size: int, skipped: bool = False) -> None:
        self.completed_count += 1
        if skipped:
            self.skipped_count += 1
        self._file_bytes[filename] = final_size
        self._render()

    def finish(self) -> None:
        self._render(done=True)

    def _render(self, done: bool = False) -> None:
        term_width = shutil.get_terminal_size((80, 24)).columns
        max_width = max(40, term_width - 2)
        bar_w = 8 if max_width < 75 else 16

        # --- Line 1: Batch Progress ---
        if self.total_files > 0:
            batch_pct = (self.completed_count / self.total_files) * 100.0
            batch_pct_text = f"{batch_pct:5.1f}%"
            batch_bar = Style.progress_bar(batch_pct, width=bar_w)
            batch_count = f"{self.completed_count}/{self.total_files} files"
        else:
            batch_pct = None
            batch_pct_text = "  --% "
            batch_bar = Style.progress_bar(None, width=bar_w)
            batch_count = f"{self.completed_count} files"

        current_total_bytes = sum(self._file_bytes.values())
        if current_total_bytes > 0:
            size_color = Style.GREEN if (batch_pct is not None and batch_pct >= 100.0) else Style.WHITE
            size_str = f" {Style.dim('•')} {size_color}{format_bytes(current_total_bytes)}{Style.RESET}"
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

        # --- Line 2: Active File Progress ---
        if self.current_total and self.current_total > 0:
            file_pct = (self.current_downloaded / self.current_total) * 100.0
            file_pct_text = f"{file_pct:5.1f}%"
            file_bar = Style.progress_bar(file_pct, width=bar_w)
            file_sizes = f"{format_bytes(self.current_downloaded)} / {format_bytes(self.current_total)}"
        else:
            file_pct = None
            file_pct_text = "  --% "
            file_bar = Style.progress_bar(None, width=bar_w)
            file_sizes = f"{format_bytes(self.current_downloaded)} / ??"

        file_color = Style.GREEN if (file_pct is not None and file_pct >= 100.0) else Style.CYAN
        tag_file = Style.tag("📥", file_pct_text, file_color)
        speed_str = f"{format_bytes(int(self.current_speed))}/s" if self.current_speed else "--/s"
        speed_part = f" {Style.dim('@')} {Style.speed(speed_str)}"

        eta_part = ""
        if self.current_eta and self.current_eta > 0 and (file_pct is None or file_pct < 100.0):
            eta_m, eta_s = divmod(int(self.current_eta), 60)
            eta_part = f" {Style.dim('ETA')} {Style.yellow(f'{eta_m:02d}:{eta_s:02d}')}"

        prefix = f"{tag_file} {file_bar} {Style.white(file_sizes)}{speed_part}"
        prefix_vis = self._visible_len(prefix)

        if eta_part and (prefix_vis + self._visible_len(eta_part) + 12 <= max_width):
            prefix += eta_part
            prefix_vis = self._visible_len(prefix)

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


class TeraboxDlBackend(BaseBackend):
    """
    Adapter backend wrapping Terabox-dl for MULTI_DOWNLOADER.
    Features:
    - Auto-engine discovery (local user repo, project engines/, or native implementation)
    - Authentication via ndus cookie (modular config, environment variable, or .env)
    - jsToken dynamic resolution & session caching
    - Direct CDN link retrieval via /api/sharedownload
    - Multi-file share & folder hierarchy recursion
    - HTTP Range resume support
    - In-place dual batch progress reporting
    """

    name: str = "terabox-dl"
    supported_domains: List[str] = [
        "terabox.com",
        "www.terabox.com",
        "1024tera.com",
        "www.1024tera.com",
        "1024terabox.com",
        "www.1024terabox.com",
        "teraboxlink.com",
        "www.teraboxlink.com",
        "freeterabox.com",
        "www.freeterabox.com",
        "mirrobox.com",
        "www.mirrobox.com",
        "nephobox.com",
        "www.nephobox.com",
        "4funbox.com",
        "www.4funbox.com",
        "teraboxapp.com",
        "www.teraboxapp.com",
        "tibibox.com",
        "www.tibibox.com",
        "momobox.com",
        "www.momobox.com",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._cached_js_token: Optional[str] = None
        self._load_modular_config()

    def _load_modular_config(self) -> None:
        """Loads and merges options from configs/terabox-dl.json if present."""
        cfg_path_str = self.config.get("config_file", "configs/terabox-dl.json")
        cfg_path = Path(cfg_path_str)
        if not cfg_path.is_absolute():
            project_root = Path(__file__).resolve().parent.parent
            cfg_path = project_root / cfg_path

        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if not k.startswith("//") and (k not in self.config or self.config[k] in (None, "")):
                            self.config[k] = v
            except Exception:
                pass

    def can_handle(self, url: str) -> bool:
        """Returns True if the URL targets any supported TeraBox domain or contains surl=."""
        url_lower = url.lower()
        try:
            domain = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
            if any(domain == d or domain.endswith("." + d) for d in self.supported_domains):
                return True
            return False
        except Exception:
            return False

    @staticmethod
    def extract_short_key(url: str) -> str:
        """
        Normalizes share keys from various TeraBox link representations:
        - https://www.terabox.com/s/1ABCxyz -> 1ABCxyz
        - https://1024tera.com/sharing/link?surl=ABCxyz -> 1ABCxyz
        - 1ABCxyz -> 1ABCxyz
        """
        raw = url.strip().rstrip('/')
        if '/s/' in raw:
            return raw.split('/s/')[-1].split('?')[0]
        if 'surl=' in raw:
            parsed = urllib.parse.urlparse(raw)
            params = urllib.parse.parse_qs(parsed.query)
            surl = params.get('surl', [None])[0]
            if surl:
                return '1' + surl if not surl.startswith('1') else surl
        if '/' not in raw and ':' not in raw:
            return raw
        raise DownloadFailedError(f"Could not extract TeraBox share key from URL: {url}")

    def _resolve_ndus_cookie(self, task_options: Optional[Dict[str, Any]] = None) -> str:
        """
        Resolves the ndus cookie from:
        1. Task CLI option override (--ndus ...)
        2. Config ndus_cookie
        3. Environment variable TERABOX_NDUS
        4. .env file in user repo (~/Terabox-dl/.env) or project root
        """
        if task_options and task_options.get("ndus"):
            return str(task_options["ndus"]).strip()

        if self.config.get("ndus_cookie"):
            return str(self.config["ndus_cookie"]).strip()

        env_val = os.getenv("TERABOX_NDUS")
        if env_val:
            return env_val.strip()

        custom_env = self.config.get("env_file")
        candidate_envs = [
            Path(custom_env) if custom_env else None,
            Path(__file__).resolve().parent.parent / ".env",
            Path.home() / "Terabox-dl" / ".env",
            Path.home() / "terabox-dl" / ".env",
        ]
        for p in candidate_envs:
            if p and p.exists():
                try:
                    load_dotenv(p)
                    val = os.getenv("TERABOX_NDUS") or os.getenv("TERABOX_NDUS_COOKIE")
                    if val:
                        return val.strip()
                except Exception:
                    pass

        return ""

    def _save_modular_config(self) -> None:
        """Save credentials atomically without rewriting an unrelated .env file."""
        project_root = Path(__file__).resolve().parent.parent
        path = Path(self.config.get('config_file', 'configs/terabox-dl.json'))
        if not path.is_absolute():
            path = project_root / path
        with file_lock(path):
            try:
                current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                if not isinstance(current, dict):
                    raise ValueError('Expected a JSON object')
                for key in ('ndus_cookie',):
                    if key in self.config:
                        current[key] = self.config[key]
                atomic_json_write(path, current)
            except (OSError, ValueError, TypeError) as exc:
                raise ConfigError('Could not save terabox credentials; existing configuration was preserved') from exc
        if path.resolve() == (project_root / 'configs' / 'terabox-dl.json').resolve():
            # The application master store has precedence over .env fallbacks.
            from core.config import ConfigManager
            master = ConfigManager()
            for key in ('ndus_cookie',):
                if key in self.config:
                    master.set('terabox_dl', key, self.config[key])

    def _prompt_ndus_cookie(self, reason: str = "") -> str:
        """Interactively prompts the user for their TeraBox NDUS cookie."""
        existing = self.config.get("ndus_cookie") or self._resolve_ndus_cookie()
        print(f"\n{Style.tag('🍪', 'TERABOX AUTH', Style.CYAN)} {Style.bold('TeraBox account cookie (ndus) required.')}")
        if existing:
            masked = existing[:4] + "..." + existing[-4:] if len(existing) > 8 else "***"
            print(f"   {Style.dim(f'Current configured cookie: {masked}')}")
        if reason:
            print(f"{Style.dim(f'   Notice: {reason}')}")
        print(f"{Style.dim('   How to get your ndus cookie:')}")
        print(f"{Style.dim('   1. Log into https://www.terabox.com (or 1024tera.com) in your web browser.')}")
        print(f"{Style.dim('   2. Open Developer Tools (F12) -> Application / Storage -> Cookies -> terabox.com.')}")
        print(f"{Style.dim('   3. Locate the cookie named \"ndus\" and copy its Value.')}\n")

        prompt_label = "Enter TeraBox ndus cookie"
        if existing:
            prompt_label += " (press Enter to keep existing)"
        val = input(f"{Style.cyan(prompt_label + ': ')}").strip()
        if not val:
            if existing:
                print(f"\n{Style.tag('ℹ️', 'TERABOX', Style.DIM)} Retaining existing cookie.\n")
                return existing
            raise AuthenticationError("TeraBox download cancelled: No ndus cookie provided.")

        self.config["ndus_cookie"] = val
        self._save_modular_config()
        print(f"\n{Style.tag('💾', 'SAVED', Style.GREEN)} Cookie saved to {Style.path('configs/terabox-dl.json')}\n")
        return val

    @classmethod
    def get_base_url(cls, target_url: str) -> str:
        """
        Dynamically resolves active TeraBox API base host from the target link.
        If the target domain is an unblocked mirror (e.g. 1024terabox.com or 1024tera.com),
        uses that domain as the primary API base to bypass ISP blocks on terabox.com.
        """
        try:
            parsed = urllib.parse.urlparse(target_url)
            if parsed.netloc:
                domain = (parsed.hostname or '').lower()
                if any(domain == host or domain.endswith('.' + host) for host in cls.supported_domains):
                    return f'https://{domain}'
        except Exception:
            pass
        return BASE_URL

    def _create_session(self, ndus_cookie: str) -> Any:
        """Initializes a configured Session with cookies and headers."""
        try:
            from curl_cffi import requests as cffi_requests
            session = cffi_requests.Session(impersonate="chrome")
        except Exception:
            session = requests.Session()

        session.headers.update(TERABOX_HEADERS)
        proxy = self.config.get('proxy')
        if proxy:
            session.proxies.update({'http': proxy, 'https': proxy})
        custom_ua = self.config.get("user_agent")
        if custom_ua:
            session.headers["User-Agent"] = custom_ua

        if ndus_cookie:
            for domain in [
                ".terabox.com",
                ".1024tera.com",
                ".1024terabox.com",
                ".teraboxlink.com",
                ".freeterabox.com",
                ".mirrobox.com",
                ".nephobox.com",
                ".4funbox.com",
                ".teraboxapp.com",
                ".tibibox.com",
                ".momobox.com",
            ]:
                try:
                    session.cookies.set("ndus", ndus_cookie, domain=domain)
                except Exception:
                    pass
        return session

    def _fetch_js_token(self, session: Any, target_url: str) -> Optional[str]:
        """Scrapes the dynamic jsToken required for TeraBox signature requests."""
        if self._cached_js_token:
            return self._cached_js_token

        base_url = self.get_base_url(target_url)
        short_key = self.extract_short_key(target_url)
        urls_to_try: List[str] = []
        if short_key:
            canonical_url = f"{BASE_URL}/s/{short_key}"
            urls_to_try.append(canonical_url)
        if target_url not in urls_to_try:
            urls_to_try.append(target_url)
        if base_url != target_url and base_url not in urls_to_try:
            urls_to_try.append(base_url)
        if "1024tera.com" not in base_url and "https://www.1024tera.com" not in urls_to_try:
            urls_to_try.append("https://www.1024tera.com")
        if BASE_URL not in urls_to_try:
            urls_to_try.append(BASE_URL)

        timeout = int(self.config.get("timeout", 30))

        for u in urls_to_try:
            try:
                resp = session.get(u, timeout=timeout)
                html = resp.text

                # Check if TeraBox responded with a verification / CAPTCHA challenge
                if html.strip().startswith("{") and ("400141" in html or "need verify" in html or "verify_v2" in html):
                    self._last_terabox_verify_required = True
                    return None

                # Primary pattern (encoded backticks)
                start_marker = '`function%20fn%28a%29%7Bwindow.jsToken%20%3D%20a%7D%3Bfn%28%22'
                end_marker = '%22%29`'
                if start_marker in html:
                    s_idx = html.index(start_marker) + len(start_marker)
                    e_idx = html.index(end_marker, s_idx)
                    token = html[s_idx:e_idx]
                    if token:
                        self._cached_js_token = token
                        return token

                # Alternative markers
                start_alt = 'function%20fn%28a%29%7Bwindow.jsToken%20%3D%20a%7D%3Bfn%28%22'
                if start_alt in html:
                    s_idx = html.index(start_alt) + len(start_alt)
                    e_idx = html.index('%22%29', s_idx)
                    token = html[s_idx:e_idx]
                    if token:
                        self._cached_js_token = token
                        return token

                # Regex patterns
                m = re.search(r'window\.jsToken\s*=\s*["\']([a-zA-Z0-9_.-]+)["\']', html)
                if m:
                    self._cached_js_token = m.group(1)
                    return self._cached_js_token

                m = re.search(r'["\']jsToken["\']\s*:\s*["\']([a-zA-Z0-9_.-]+)["\']', html)
                if m:
                    self._cached_js_token = m.group(1)
                    return self._cached_js_token

                m = re.search(r'fn\(\s*["\']([a-zA-Z0-9_.-]{16,})["\']\s*\)', html)
                if m:
                    self._cached_js_token = m.group(1)
                    return self._cached_js_token
            except Exception:
                pass

        return None

    def _get_share_info(self, session: Any, short_key: str, target_url: str) -> Dict[str, Any]:
        """Resolves share metadata via /api/shorturlinfo across reachable mirrors."""
        app_id = self.config.get("app_id", APP_ID)
        timeout = int(self.config.get("timeout", 30))
        base_url = self.get_base_url(target_url)

        params = {
            "shorturl": short_key,
            "root": "1",
            "app_id": app_id,
            "web": "1",
            "channel": "dubox",
            "clienttype": "0",
        }

        js_token = self._fetch_js_token(session, target_url)
        if js_token:
            params["jsToken"] = js_token

        # Candidate API base hosts: canonical BASE_URL first (most stable), then active mirror, then 1024tera
        candidate_bases = [BASE_URL]
        if base_url not in candidate_bases:
            candidate_bases.append(base_url)
        if "https://www.1024tera.com" not in candidate_bases:
            candidate_bases.append("https://www.1024tera.com")

        last_error = None
        for base in candidate_bases:
            url = f"{base}/api/shorturlinfo"
            for attempt in range(1, 3):
                try:
                    resp = session.get(url, params=params, timeout=timeout)
                    data = resp.json()

                    errmsg = str(data.get("errmsg", "")).lower()
                    if data.get("errno") in (400210, 4000023):
                        # Pure jsToken expiration: refresh and retry once
                        self._cached_js_token = None
                        js_token = self._fetch_js_token(session, target_url)
                        if js_token:
                            params["jsToken"] = js_token
                        resp = session.get(url, params=params, timeout=timeout)
                        data = resp.json()
                        errmsg = str(data.get("errmsg", "")).lower()

                    errno = data.get("errno", 0)
                    if errno != 0:
                        err_msg = _get_error_message(errno)
                        is_verify = (
                            errno == 400141
                            or "need verify" in errmsg
                            or "verify_v2" in errmsg
                            or "need verify" in err_msg.lower()
                            or getattr(self, "_last_terabox_verify_required", False)
                        )
                        if errno in (-20, 12, 112) or is_verify:
                            if is_verify:
                                err_msg = "TeraBox security verification required ('need verify_v2'). Your ndus cookie has expired or requires browser re-verification."
                            last_error = AuthenticationError(f"{err_msg} (Configure your ndus cookie via 'multi-dl auth terabox' or GUI Settings)")
                        else:
                            last_error = DownloadFailedError(err_msg)
                        # Break out to try the next candidate mirror base
                        break

                    return {
                        "share_id": data.get("shareid"),
                        "uk": data.get("uk"),
                        "sign": data.get("sign"),
                        "timestamp": data.get("timestamp"),
                        "randsk": data.get("randsk"),
                        "title": data.get("title", "Shared Files"),
                        "file_list": data.get("list", []),
                        "api_base": base,
                    }
                except (AuthenticationError, DownloadFailedError) as e:
                    last_error = e
                    break
                except Exception as e:
                    last_error = e
                    # If this host had connection reset / blocked, try next mirror
                    err_s = str(e)
                    if "ConnectionResetError" in err_s or "10054" in err_s or "Connection aborted" in err_s:
                        break
                    time.sleep(1)

        # If all candidate bases failed and authentication error occurred (prompt once per execution):
        if isinstance(last_error, AuthenticationError) and Style.is_interactive() and not getattr(self, "_has_prompted_ndus", False):
            self._has_prompted_ndus = True
            new_cookie = self._prompt_ndus_cookie(reason=str(last_error))
            for domain in [
                ".terabox.com", ".1024tera.com", ".1024terabox.com",
                ".teraboxlink.com", ".freeterabox.com", ".mirrobox.com",
                ".nephobox.com", ".4funbox.com", ".teraboxapp.com",
                ".tibibox.com", ".momobox.com",
            ]:
                try:
                    session.cookies.set("ndus", new_cookie, domain=domain)
                except Exception:
                    pass
            self._cached_js_token = None
            self._last_terabox_verify_required = False
            return self._get_share_info(session, short_key, target_url)

        if last_error:
            if isinstance(last_error, (AuthenticationError, DownloadFailedError)):
                raise last_error
            raise DownloadFailedError(f"Failed to resolve TeraBox share info: {last_error}")
        raise DownloadFailedError("Failed to resolve TeraBox share info after retries.")

    def _get_folder_contents(
        self,
        session: Any,
        share_id: Any,
        uk: Any,
        sign: Any,
        timestamp: Any,
        directory: str,
        target_url: str,
        api_base: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Recursively lists all files in a folder directory."""
        app_id = self.config.get("app_id", APP_ID)
        timeout = int(self.config.get("timeout", 30))
        all_items: List[Dict[str, Any]] = []
        page = 1
        limit = 100
        base = api_base or self.get_base_url(target_url)

        while True:
            params = {
                "shareid": share_id,
                "uk": uk,
                "sign": sign,
                "timestamp": timestamp,
                "dir": directory,
                "root": "1" if directory == "/" else "0",
                "app_id": app_id,
                "web": "1",
                "channel": "dubox",
                "clienttype": "0",
                "page": page,
                "num": limit,
                "order": "name",
            }
            if self._cached_js_token:
                params["jsToken"] = self._cached_js_token

            url = f"{base}/share/list"
            try:
                resp = session.get(url, params=params, timeout=timeout)
                data = resp.json()

                if data.get("errno") in (400210, 4000023):
                    self._cached_js_token = None
                    self._fetch_js_token(session, target_url)
                    if self._cached_js_token:
                        params["jsToken"] = self._cached_js_token
                    resp = session.get(url, params=params, timeout=timeout)
                    data = resp.json()

                if data.get("errno", 0) != 0:
                    break

                items = data.get("list", [])
                if items:
                    all_items.extend(items)
                    page += 1
                    if len(items) < limit:
                        break
                else:
                    break
            except Exception:
                break

        return all_items

    def _get_download_link(
        self,
        session: Any,
        fs_id: Any,
        share_id: Any,
        uk: Any,
        sign: Any,
        timestamp: Any,
        randsk: Any,
        api_base: Optional[str] = None,
    ) -> Optional[str]:
        app_id = self.config.get("app_id", APP_ID)
        timeout = int(self.config.get("timeout", 30))
        decoded_randsk = urllib.parse.unquote(randsk) if '%' in str(randsk) else str(randsk)
        extra = f'{{"sekey":"{decoded_randsk}"}}'
        extra_escaped = urllib.parse.quote(extra)
        data = f"encrypt=0&extra={extra_escaped}&fid_list=[{fs_id}]&primaryid={share_id}&uk={uk}&product=share&type=nolimit"

        post_headers = session.headers.copy()
        post_headers["Content-Type"] = "application/x-www-form-urlencoded"

        candidate_bases = [BASE_URL]
        if api_base and api_base not in candidate_bases:
            candidate_bases.append(api_base)

        for base in candidate_bases:
            uri = f"{base}/api/sharedownload?app_id={app_id}&channel=chunlei&clienttype=12&sign={sign}&timestamp={timestamp}&web=1"
            for _ in range(3):
                try:
                    resp = session.post(uri, headers=post_headers, data=data, timeout=timeout)
                    resp_data = resp.json()
                    if resp_data.get("errno") == 0 and resp_data.get("list"):
                        return resp_data["list"][0].get("dlink")
                    if resp_data.get("errno") in (-20, 12, 112):
                        if Style.is_interactive():
                            err_msg = _get_error_message(resp_data.get("errno"))
                            new_cookie = self._prompt_ndus_cookie(reason=err_msg)
                            for domain in [
                                ".terabox.com", ".1024tera.com", ".1024terabox.com",
                                ".teraboxlink.com", ".freeterabox.com", ".mirrobox.com",
                                ".nephobox.com", ".4funbox.com", ".teraboxapp.com",
                                ".tibibox.com", ".momobox.com",
                            ]:
                                try:
                                    session.cookies.set("ndus", new_cookie, domain=domain)
                                except Exception:
                                    pass
                            continue
                    time.sleep(1)
                except Exception:
                    time.sleep(1)

        return None

    async def extract_info(self, url: str) -> Dict[str, Any]:
        """Extracts metadata from a TeraBox share without downloading."""
        def _extract() -> Dict[str, Any]:
            short_key = self.extract_short_key(url)
            ndus_cookie = self._resolve_ndus_cookie()
            session = self._create_session(ndus_cookie)
            return self._get_share_info(session, short_key, url)

        try:
            return await asyncio.to_thread(_extract)
        except Exception as e:
            raise DownloadFailedError(f"TeraBox metadata extraction failed: {e}")

    async def download(
        self,
        task: DownloadTask,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> ArchiveEntry:
        """Executes the TeraBox download pipeline."""
        out_dir = Path(task.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        quiet = bool(task.options.get("quiet") or progress_callback is not None)

        short_key = self.extract_short_key(task.url)
        out_dir = contained_path(out_dir, safe_name(short_key))
        out_dir.mkdir(parents=True, exist_ok=True)
        ndus_cookie = self._resolve_ndus_cookie(task.options)
        if not ndus_cookie and not quiet:
            print(f"\n{Style.tag('⚠️', 'AUTH', Style.YELLOW)} {Style.yellow('No ndus cookie provided.')}")
            print(f"  {Style.dim('1. Add ndus_cookie to configs/terabox-dl.json')}")
            print(f"  {Style.dim('2. Or set TERABOX_NDUS in your .env file')}")
            print(f"  {Style.dim('3. Or pass --ndus <cookie> on CLI')}\n")

        session = self._create_session(ndus_cookie)
        if task.options.get('proxy'):
            session.proxies.update({'http': task.options['proxy'], 'https': task.options['proxy']})
        cancel_event = threading.Event()

        def _resolve_pipeline() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
            info = self._get_share_info(session, short_key, task.url)
            title = info.get("title", "Shared Files")
            file_list = info.get("file_list", [])

            # Flatten directory trees
            queue: List[Dict[str, Any]] = []

            def _walk(items: List[Dict[str, Any]], rel_dir: str):
                for item in items:
                    is_dir = item.get("isdir") == 1 or str(item.get("isdir")).strip() == "1" or item.get("isdir") is True
                    if is_dir:
                        sub_dir_name = _get_safe_filename(item.get("server_filename"))
                        sub_path = f"{rel_dir}/{sub_dir_name}" if rel_dir else sub_dir_name
                        folder_path = item.get("path") or f"/{sub_dir_name}"
                        sub_items = self._get_folder_contents(
                            session,
                            info["share_id"],
                            info["uk"],
                            info["sign"],
                            info["timestamp"],
                            folder_path,
                            task.url,
                            api_base=info.get("api_base"),
                        )
                        _walk(sub_items, sub_path)
                    else:
                        queue.append({
                            "item": item,
                            "rel_dir": rel_dir,
                            "share_id": info["share_id"],
                            "uk": info["uk"],
                            "sign": info["sign"],
                            "timestamp": info["timestamp"],
                            "randsk": info["randsk"],
                        })

            _walk(file_list, "")
            return info, queue

        try:
            info, queue = await run_blocking(_resolve_pipeline, cancel_event=cancel_event)
        except BaseException:
            if hasattr(session, 'close'):
                session.close()
            raise
        audio_only = bool(task.options.get('audio_only') or task.options.get('extract_audio'))
        video_only = bool(task.options.get('video_only'))
        images_only = bool(task.options.get('images_only'))
        if audio_only or video_only or images_only:
            wanted = MediaType.AUDIO if audio_only else (MediaType.VIDEO if video_only else MediaType.IMAGE)
            queue = [entry for entry in queue if _detect_media_type(entry['item'].get('server_filename', '')) == wanted]
        if not queue:
            if hasattr(session, 'close'):
                session.close()
            raise DownloadFailedError("No downloadable files found in this TeraBox share.")

        share_title = info.get("title", "Shared Files")
        total_files = len(queue)
        total_share_bytes = sum(int(q["item"].get("size", 0)) for q in queue)

        if not quiet:
            print(f"{Style.tag('🎬', 'MEDIA', Style.YELLOW)} {Style.white(share_title)}")
            if total_files > 1:
                print(f"{Style.tag('📦', 'BATCH', Style.MAGENTA)} Detected {Style.cyan(str(total_files))} files in share ({format_bytes(total_share_bytes)})")

        # Multi-file batch tracker or single file progress
        is_batch = total_files > 1
        batch_tracker = _TeraboxBatchTracker(total_files=total_files, total_batch_bytes=total_share_bytes) if (is_batch and not quiet) else None

        downloaded_paths: List[Path] = []
        max_retries = int(self.config.get("max_retries", 10))
        chunk_size = int(self.config.get("chunk_size_kb", 64)) * 1024
        resume_enabled = bool(self.config.get("resume", True))
        timeout = int(self.config.get("timeout", 30))

        download_errors: List[str] = []
        if max_retries <= 0 or chunk_size <= 0 or timeout <= 0:
            if hasattr(session, 'close'):
                session.close()
            raise DownloadFailedError('TeraBox retries, chunk size, and timeout must be positive')

        def _download_all() -> None:
            for idx, q_entry in enumerate(queue, 1):
                item = q_entry["item"]
                rel_dir = q_entry["rel_dir"]
                raw_filename = item.get("server_filename") or f"file_{idx}"
                filename = _get_safe_filename(raw_filename)
                file_size = int(item.get("size", 0))
                fs_id = item.get("fs_id")

                if cancel_event.is_set():
                    raise DownloadFailedError('Download interrupted')
                target_folder = contained_path(out_dir, rel_dir)
                target_folder.mkdir(parents=True, exist_ok=True)
                # Stable file identity prevents collisions after filename sanitization.
                stem, suffix = Path(filename).stem, Path(filename).suffix
                filename = f'{stem} [{safe_name(fs_id or idx)}]{suffix}'
                dest_path = contained_path(target_folder, filename)
                part_path = dest_path.with_name(dest_path.name + '.part')

                # 1. Resolve CDN link
                dlink = item.get("dlink")
                if not dlink:
                    dlink = self._get_download_link(
                        session,
                        fs_id,
                        q_entry["share_id"],
                        q_entry["uk"],
                        q_entry["sign"],
                        q_entry["timestamp"],
                        q_entry["randsk"],
                        api_base=info.get("api_base"),
                    )

                if not dlink:
                    download_errors.append(f"{filename}: Failed to resolve direct download link")
                    if batch_tracker:
                        batch_tracker.finish_file(filename, 0, skipped=True)
                    continue

                # 2. Check existing / collision
                if dest_path.exists() and not task.options.get('force'):
                    exist_sz = dest_path.stat().st_size
                    if exist_sz == file_size and file_size > 0:
                        downloaded_paths.append(dest_path)
                        if batch_tracker:
                            batch_tracker.finish_file(filename, file_size, skipped=True)
                        continue

                # 3. Stream download with retry
                success = False
                for attempt in range(1, max_retries + 1):
                    try:
                        resume_pos = 0
                        if resume_enabled and part_path.exists() and not task.options.get('force'):
                            resume_pos = part_path.stat().st_size
                            if resume_pos >= file_size and file_size > 0:
                                resume_pos = 0

                        req_headers = session.headers.copy()
                        if resume_pos > 0:
                            req_headers["Range"] = f"bytes={resume_pos}-"

                        start_time = time.time()
                        last_tick = start_time
                        last_bytes = resume_pos
                        current_speed = 0.0

                        resp = session.get(dlink, headers=req_headers, stream=True, timeout=timeout)
                        try:
                            if resp.status_code not in (200, 206):
                                resp.raise_for_status()

                            is_append = resp.status_code == 206
                            if is_append:
                                content_range = resp.headers.get('Content-Range', '')
                                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
                                if not match or int(match[1]) != resume_pos or int(match[2]) < int(match[1]) or (file_size and int(match[3]) != file_size):
                                    raise DownloadFailedError('Server returned an invalid Content-Range')
                            mode = "ab" if is_append and resume_pos > 0 else "wb"
                            downloaded_now = resume_pos if is_append else 0

                            with open(part_path, mode) as f:
                                for chunk in resp.iter_content(chunk_size=chunk_size):
                                    if cancel_event.is_set():
                                        raise DownloadFailedError('Download interrupted')
                                    if not chunk:
                                        continue
                                    f.write(chunk)
                                    downloaded_now += len(chunk)
                                    now = time.time()

                                    # Update speed and ETA every 0.2s
                                    if now - last_tick >= 0.2:
                                        delta_t = now - last_tick
                                        delta_b = downloaded_now - last_bytes
                                        current_speed = delta_b / delta_t if delta_t > 0 else 0.0
                                        last_tick = now
                                        last_bytes = downloaded_now

                                        rem_bytes = max(0, file_size - downloaded_now) if file_size > 0 else 0
                                        eta_sec = int(rem_bytes / current_speed) if current_speed > 0 else None

                                        if batch_tracker:
                                            batch_tracker.update_file(
                                                filename=filename,
                                                downloaded=downloaded_now,
                                                total=file_size,
                                                speed=current_speed,
                                                eta=eta_sec,
                                            )
                                        if progress_callback:
                                            prog = DownloadProgress(
                                                downloaded_bytes=downloaded_now,
                                                total_bytes=file_size,
                                                speed_bytes_sec=current_speed,
                                                eta_seconds=eta_sec,
                                                current_file=share_title if is_batch else filename,
                                                file_index=idx,
                                                total_files=total_files,
                                                status_message=filename,
                                                extractor="terabox",
                                                container_title=share_title,
                                            )
                                            prog.update_percent()
                                            progress_callback(prog)
                        finally:
                            if hasattr(resp, "close"):
                                try:
                                    resp.close()
                                except Exception:
                                    pass

                        actual_size = part_path.stat().st_size
                        if file_size > 0 and actual_size != file_size:
                            raise DownloadFailedError(f'Incomplete transfer: expected {file_size} bytes, received {actual_size}')
                        os.replace(part_path, dest_path)
                        downloaded_paths.append(dest_path)
                        if batch_tracker:
                            batch_tracker.finish_file(filename, file_size)
                        success = True
                        break

                    except Exception as err:
                        if attempt == max_retries:
                            download_errors.append(f"{filename}: {err}")
                            if batch_tracker:
                                batch_tracker.finish_file(filename, 0, skipped=True)
                        else:
                            time.sleep(1)

        try:
            await run_blocking(_download_all, cancel_event=cancel_event)
        finally:
            if hasattr(session, 'close'):
                session.close()

        if batch_tracker:
            batch_tracker.finish()

        if not downloaded_paths:
            err_msg = "; ".join(download_errors) if download_errors else "No downloadable files could be retrieved."
            raise DownloadFailedError(f"Failed to download any files from TeraBox: {err_msg}")
        if download_errors:
            raise DownloadFailedError(f'Partial TeraBox download ({len(downloaded_paths)}/{total_files} files): ' + '; '.join(download_errors))

        # Determine primary file or folder path
        primary_path = downloaded_paths[0] if len(downloaded_paths) == 1 else out_dir
        final_filename = primary_path.name if len(downloaded_paths) == 1 else share_title
        total_size = sum(p.stat().st_size for p in downloaded_paths if p.exists())
        media_type = _detect_media_type(primary_path.name) if len(downloaded_paths) == 1 else MediaType.UNKNOWN

        file_hash = await run_blocking(ArchiveManager.calculate_file_hash, primary_path)

        return ArchiveEntry(
            id=str(uuid.uuid4()),
            url=task.url,
            file_path=str(primary_path.resolve()),
            file_name=final_filename,
            file_hash=file_hash,
            file_size=total_size,
            backend=self.name,
            source_site="terabox",
            media_type=media_type,
            metadata={
                "share_title": share_title,
                "total_files": len(downloaded_paths),
                "share_id": info.get("share_id"),
            },
        )
