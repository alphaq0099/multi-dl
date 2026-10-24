"""
CLI entry point for MULTI_DOWNLOADER.
"""

import argparse
import asyncio
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Force standard output to UTF-8 on Windows terminals to support emojis and international titles
if sys.platform == "win32" and not os.environ.get("PYTEST_CURRENT_TEST"):
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

__version__ = "4.6.0"

from core.terminal import Style, str_width, truncate_to_width


def _sigint_handler(signum=None, frame=None):
    """Request graceful interruption so workers can preserve resumable queue state."""
    sys.stdout.write(f"\n\n{Style.tag('🛑', 'ABORTED', Style.RED)} {Style.error('Operation cancelled by user.')}\n")
    sys.stdout.flush()
    raise KeyboardInterrupt


from backends.gallerydl_backend import GalleryDlBackend
from backends.telegram_backend import TelegramDlBackend
from backends.terabox_backend import TeraboxDlBackend
from backends.ytdlp_backend import YtDlpBackend
from backends.cyberdrop_backend import CyberdropDlBackend
from core.archive import ArchiveManager
from core.clipboard import (
    ClipboardWatcher,
    clear_failed_urls,
    clear_clipboard_history,
    load_failed_urls,
    play_chime,
    CLIPBOARD_HISTORY_PATH,
)
from core.config import ConfigManager
from core.dispatcher import QueueDispatcher
from core.exceptions import MultiDLError, ConfigError
from core.models import ArchiveEntry, DownloadProgress, DownloadTask, TaskStatus
from core.queue_manager import QueueManager
from core.router import URLRouter
from core.updater import EngineUpdater
from core.models import BatchSummaryReport
from core.storage import atomic_json_write
import uuid


def new_batch_queue():
    queue_file = PROJECT_ROOT / 'data' / 'batches' / (str(uuid.uuid4()) + '.json')
    atomic_json_write(PROJECT_ROOT / 'data' / 'last_batch.json', {'queue_file': str(queue_file)})
    return QueueManager(queue_file)


def last_batch_file():
    pointer = PROJECT_ROOT / 'data' / 'last_batch.json'
    if pointer.exists():
        candidate = Path(json.loads(pointer.read_text(encoding='utf-8'))['queue_file']).resolve()
        if candidate.is_relative_to((PROJECT_ROOT / 'data' / 'batches').resolve()):
            return candidate
        raise ConfigError('Invalid batch queue pointer')
    return PROJECT_ROOT / 'data' / 'batch_queue.json'


def run_download(coroutine):
    report = asyncio.run(coroutine)
    if isinstance(report, BatchSummaryReport) and report.failed_count:
        raise SystemExit(1)
    return report



def format_bytes(size: int) -> str:
    """Formats bytes to human-readable string (KB, MB, GB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"



def extract_inline_options(raw_line: str) -> Tuple[str, Dict[str, Any]]:
    """
    Parses inline flags from an interactive input string or CLI argument.
    Supports:
      -x / --extract-audio / --audio-only
      --video-only / --videos-only
      --images-only / --image-only / --photos-only
      -r / --resolution / --res <preset> (e.g. 1080p, 720p, 480p, 4k, best)
      --audio-format <format> (e.g. mp3, m4a, flac)
      --format <spec>
      --proxy <proxy_url>
      --force
      --no-archive
      -s / --silent / --no-sound / --no-notification
      -b / --backend <name>
      --cookies-from-browser <browser>
      --cookies <path>
      --ndus <cookie>
    Returns: (cleaned_urls_or_files_str, extracted_options_dict)
    """
    if not raw_line or not raw_line.strip():
        return "", {}

    try:
        tokens = shlex.split(raw_line, posix=False)
    except Exception:
        tokens = raw_line.split()

    opts: Dict[str, Any] = {}
    cleaned_tokens: List[str] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx].strip()
        tok_lower = tok.lower()

        if tok_lower in ("-x", "--extract-audio", "--audio-only"):
            opts["audio_only"] = True
            idx += 1
        elif tok_lower in ("--video-only", "--videos-only"):
            opts["video_only"] = True
            idx += 1
        elif tok_lower in ("--images-only", "--image-only", "--photos-only"):
            opts["images_only"] = True
            idx += 1
        elif tok_lower in ("-r", "--resolution", "--res") and idx + 1 < len(tokens):
            opts["resolution"] = tokens[idx + 1].strip('"\'')
            idx += 2
        elif tok_lower == "--audio-format" and idx + 1 < len(tokens):
            opts["audio_format"] = tokens[idx + 1].strip('"\'').lower()
            idx += 2
        elif tok_lower == "--format" and idx + 1 < len(tokens):
            opts["format"] = tokens[idx + 1].strip('"\'')
            idx += 2
        elif tok_lower == "--proxy" and idx + 1 < len(tokens):
            opts["proxy"] = tokens[idx + 1].strip('"\'')
            idx += 2
        elif tok_lower in ("-b", "--backend") and idx + 1 < len(tokens):
            opts["backend"] = tokens[idx + 1].strip('"\'').lower()
            idx += 2
        elif tok_lower == "--cookies-from-browser" and idx + 1 < len(tokens):
            opts["cookies_from_browser"] = tokens[idx + 1].strip('"\'')
            idx += 2
        elif tok_lower == "--cookies" and idx + 1 < len(tokens):
            opts["cookies"] = tokens[idx + 1].strip('"\'')
            idx += 2
        elif tok_lower == "--ndus" and idx + 1 < len(tokens):
            opts["ndus"] = tokens[idx + 1].strip('"\'')
            idx += 2
        elif tok_lower in ("--force",):
            opts["force"] = True
            idx += 1
        elif tok_lower in ("--no-archive",):
            opts["no_archive"] = True
            idx += 1
        elif tok_lower == "--impersonate" and idx + 1 < len(tokens):
            opts["impersonate"] = tokens[idx + 1].strip('"\'').lower()
            idx += 2
        elif tok_lower in ("--no-sound", "--silent", "-s", "--no-notification", "--no-notifications"):
            opts["no_sound"] = True
            idx += 1
        else:
            cleaned_tokens.append(tok)
            idx += 1

    cleaned_str = " ".join(cleaned_tokens).strip()
    return cleaned_str, opts


def resolve_input_urls(inputs: List[str]) -> Tuple[List[str], List[str]]:
    """
    Parses a list of input arguments which can be individual URLs,
    whitespace-separated pasted blocks, or paths to local text files.
    Returns: (extracted_urls, source_files_found)
    """
    urls: List[str] = []
    source_files: List[str] = []

    for item in inputs:
        token = (item or "").strip().strip('"\'')
        if not token:
            continue

        p = Path(token)
        if not p.is_absolute() and not p.exists():
            if (PROJECT_ROOT / p).exists() and (PROJECT_ROOT / p).is_file():
                p = PROJECT_ROOT / p

        # Check if token is an existing file on disk
        if p.exists() and p.is_file():
            source_files.append(str(p))
            try:
                content = ""
                for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
                    try:
                        content = p.read_text(encoding=enc)
                        break
                    except UnicodeDecodeError:
                        continue
                for line in content.splitlines():
                    cleaned = line.strip().strip('"\'')
                    if cleaned and not cleaned.startswith("#"):
                        # If a line contains multiple space-separated links
                        sub_tokens = cleaned.split()
                        for st in sub_tokens:
                            cleaned_st = st.strip().strip('"\'')
                            if cleaned_st:
                                urls.append(cleaned_st)
            except Exception as e:
                print(f"{Style.tag('⚠️', 'FILE ERROR', Style.YELLOW)} Could not read file {Style.path(str(p))}: {e}")
        elif token.lower().endswith((".txt", ".list", ".urls", ".log")) and not p.exists():
            print(f"{Style.tag('⚠️', 'FILE NOT FOUND', Style.YELLOW)} Text file {Style.path(token)} does not exist.")
        else:
            # Check for multiple URLs in a single string (e.g. multi-pasted string)
            sub_tokens = token.split()
            if len(sub_tokens) > 1:
                for st in sub_tokens:
                    cleaned_st = st.strip().strip('"\'')
                    if cleaned_st:
                        urls.append(cleaned_st)
            else:
                urls.append(token)

    return urls, source_files


def print_queue_list(queue_mgr: QueueManager, status_filter: Optional[str] = None, is_batch: bool = False):
    """Renders a formatted table of all items in the download queue."""
    items = queue_mgr.list_all()
    if status_filter:
        items = [it for it in items if it.status.value == status_filter.lower()]

    q_name = "Active Batch Queue" if is_batch else "Download Queue"
    if not items:
        print(f"\n{Style.tag('📋', 'QUEUE', Style.CYAN)} {q_name} is currently empty.\n")
        return

    term_width = shutil.get_terminal_size((80, 24)).columns
    max_w = min(100, max(60, term_width - 2))

    print(f"\n{Style.tag('📋', 'QUEUE', Style.CYAN)} {Style.bold(f'Current {q_name} ({len(items)} items)')}\n")
    print(f"  {Style.dim('ID')}        {Style.dim('Prio')}  {Style.dim('Engine')}         {Style.dim('Status')}       {Style.dim('URL / Error')}")
    print(f"  {'─' * (max_w - 4)}")

    for item in items:
        status_color = {
            TaskStatus.QUEUED: Style.CYAN,
            TaskStatus.DOWNLOADING: Style.YELLOW,
            TaskStatus.COMPLETED: Style.GREEN,
            TaskStatus.SKIPPED: Style.YELLOW,
            TaskStatus.FAILED: Style.RED,
        }.get(item.status, Style.WHITE)

        eng_badge = Style.engine_badge(item.backend or "auto")
        status_tag = f"{status_color}{item.status.value.upper():<10}{Style.RESET}"

        info = item.error_message if (item.status == TaskStatus.FAILED and item.error_message) else item.url
        avail = max_w - 42
        if len(info) > avail:
            info = info[:avail - 3] + "..."

        print(f"  {Style.dim(item.id):<9} {item.priority:>4}   {eng_badge:<20} {status_tag} {Style.white(info)}")
    print(f"  {'─' * (max_w - 4)}\n")


def _print_archive_table(entries: List[ArchiveEntry]) -> None:
    """Renders a formatted table of download records from the archive matching layout: Date | Engine | extractor | size | file name."""
    term_width = shutil.get_terminal_size((80, 24)).columns
    max_w = min(120, max(75, term_width - 2))

    def _pad_v(txt: str, width: int) -> str:
        vis = str_width(txt)
        return txt + (" " * max(0, width - vis))

    # Column widths matching fffff.png:
    # Date (12), Engine (16), extractor (15), size (13), file name (remainder)
    col_date = _pad_v(Style.dim("Date"), 12)
    col_eng = _pad_v(Style.dim("Engine"), 16)
    col_ext = _pad_v(Style.dim("extractor"), 15)
    col_sz = _pad_v(Style.dim("size"), 13)
    col_file = Style.dim("file name")

    print(f"  {col_date}{col_eng}{col_ext}{col_sz}{col_file}")
    print(f"  {'─' * (max_w - 4)}")
    for r in entries:
        dt_str = r.downloaded_at.strftime("%Y-%m-%d") if r.downloaded_at else "----/--/--"
        eng_badge = Style.engine_badge(r.backend)

        # Resolve extractor name
        ext = (r.metadata or {}).get("extractor")
        if not ext and r.url:
            try:
                from core.router import URLRouter
                ext = URLRouter.get_host_identifier(r.url, r.backend)
            except Exception:
                ext = None
        if not ext and r.source_site:
            try:
                from core.router import URLRouter
                ext = URLRouter.get_host_identifier(f"https://{r.source_site}", r.backend)
            except Exception:
                ext = r.source_site.replace("www.", "").split(".")[0]
        if not ext:
            ext = (r.backend or "auto").replace("-dl", "")
        if ext in ("x", "twitter"):
            ext = "twitter"

        ext_badge = f"{Style.YELLOW}[{ext}]{Style.RESET}"
        sz_str = format_bytes(r.file_size) if r.file_size else "-- MB"
        name_or_url = r.file_name or r.url

        avail = max_w - 58
        if avail > 10:
            name_display = truncate_to_width(name_or_url, avail)
        else:
            name_display = name_or_url

        date_padded = _pad_v(Style.dim(dt_str), 12)
        eng_padded = _pad_v(eng_badge, 16)
        ext_padded = _pad_v(ext_badge, 15)
        sz_padded = _pad_v(Style.white(sz_str), 13)
        print(f"  {date_padded}{eng_padded}{ext_padded}{sz_padded}{Style.white(name_display)}")
    print(f"  {'─' * (max_w - 4)}\n")


async def download_url(url: str, backend_name: str = None, options: dict = None):
    config = ConfigManager()
    archive = ArchiveManager(config.archive_db_path)
    options = options or {}

    if options.get("no_sound"):
        config._data.setdefault("general", {})["notifications"] = False
        config._data.setdefault("general", {})["play_sound"] = False

    batch_queue = new_batch_queue()
    batch_queue.clear()
    batch_queue.add_batch([url], backend=backend_name, options=options)

    dispatcher = QueueDispatcher(config=config, queue_manager=batch_queue, archive=archive)
    return await dispatcher.run(concurrency=1)


def print_banner(config: ConfigManager, archive: ArchiveManager) -> None:
    term_width = shutil.get_terminal_size((80, 24)).columns
    box_width = max(78, min(term_width - 2, 94))
    inner_width = box_width - 2

    def _box_line(text: str) -> str:
        clean = re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', text)
        pad_total = max(0, inner_width - len(clean))
        left = pad_total // 2
        right = pad_total - left
        return f"{Style.CYAN}║{Style.RESET}{' ' * left}{text}{' ' * right}{Style.CYAN}║{Style.RESET}"

    # Five engine badges evenly spaced in between and at both ends
    engine_names = ["yt-dlp", "gallery-dl", "terabox-dl", "telegram-dl", "cyberdrop-dl"]
    badges = [Style.engine_badge(name) for name in engine_names]
    total_badge_w = sum(len(re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', b)) for b in badges)
    rem_space = inner_width - total_badge_w
    gap = max(1, rem_space // 6)
    margin = (rem_space - (gap * 4)) // 2
    extra = (rem_space - (gap * 4)) % 2
    badge_line = (" " * margin) + (" " * gap).join(badges) + (" " * (margin + extra))

    bar = "═" * inner_width
    top_border = f"{Style.CYAN}╔{bar}╗{Style.RESET}"
    bot_border = f"{Style.CYAN}╚{bar}╝{Style.RESET}"

    print(f"\n{top_border}")
    print(_box_line(f"{Style.BOLD}{Style.CYAN}MULTI_DOWNLOADER {Style.YELLOW}v{__version__}{Style.RESET}"))
    print(_box_line(f"{Style.WHITE}Universal Modular Media & File Downloader{Style.RESET}"))
    print(_box_line(""))
    print(_box_line(badge_line))
    print(f"{bot_border}\n")

    # Configuration section
    header_cfg = "─── Configuration "
    sep_len = max(6, box_width - len(header_cfg))
    print(f"{Style.CYAN}{header_cfg}{'─' * sep_len}{Style.RESET}")

    raw_dl = config.download_dir
    try:
        clean_dl = f"~{raw_dl.relative_to(config.download_dir.parent)}"
    except ValueError:
        clean_dl = str(raw_dl)

    print(f"  {Style.dim('Downloads:')}    {Style.white(clean_dl)}")

    archive_enabled = config.get("archive", "enabled", True)
    if archive_enabled:
        rel_arch = f"~data\\{config.archive_db_path.name}"
        print(f"  {Style.dim('Archive:')}      {Style.white(rel_arch)}")
    else:
        print(f"  {Style.dim('Archive:')}      {Style.yellow('Disabled')}")

    workers = config.get("general", "concurrent_downloads", 3)
    bullet = f"{Style.GRAY}•{Style.RESET}"
    notif_badge = Style.green('On') if config.notifications_enabled else Style.yellow('Off')
    print(f"  {Style.dim('Workers:')}      {Style.cyan(str(workers))} {Style.dim('concurrent streams')} {bullet} {Style.dim('Notifications:')} {notif_badge} {bullet} {Style.dim('Deduplication active')}\n")

    # Interactive mode prompt section
    header_mode = "─── Interactive Mode "
    mode_sep_len = max(6, box_width - len(header_mode))
    print(f"{Style.CYAN}{header_mode}{'─' * mode_sep_len}{Style.RESET}")
    print(f"  {Style.dim('Enter URLs (use space for multiple URLs) or a text file. Right-click to paste.')}")
    cmds = [
        Style.cyan('watch'), Style.cyan('gui'), Style.cyan('open'), Style.cyan('archive'),
        Style.cyan('queue'), Style.cyan('retry'), Style.cyan('clear'),
        Style.cyan('config'), Style.cyan('update'), Style.cyan('help'),
        Style.cyan('exit'),
    ]
    cmd_sep = f" {bullet} "
    print(f"  {Style.dim('Commands:')} {cmd_sep.join(cmds)}\n")


async def run_clipboard_watcher_session(config: ConfigManager, archive: ArchiveManager) -> None:
    old_handler = None
    try:
        old_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, AttributeError):
        pass

    play_sound_enabled = getattr(config, "notifications_enabled", config.get("general", "play_sound", True))
    watcher = ClipboardWatcher(poll_interval=0.3, play_sound=play_sound_enabled)
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    print(f"\n{Style.tag('📋', 'WATCHER', Style.CYAN)} {Style.bold('Clipboard Auto-Watcher Active')} "
          f"{Style.dim('• Copy media links to download • Press Ctrl+C to return')}")
    print(f"  {Style.dim('• Link Backup:')} {Style.white(str(CLIPBOARD_HISTORY_PATH))}\n")

    stop_event = asyncio.Event()
    live_queue = QueueManager(PROJECT_ROOT / "data" / "live_queue.json")
    live_queue.clear()
    dispatcher = QueueDispatcher(config=config, queue_manager=live_queue, archive=archive)

    try:
        await dispatcher.run_live_watcher(watcher=watcher, stop_event=stop_event)
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop_event.set()
        print(f"\n{Style.tag('⏹️', 'STOPPED', Style.YELLOW)} Clipboard watcher paused. Back to main prompt.\n")
    finally:
        stop_event.set()
        if old_handler is not None:
            try:
                signal.signal(signal.SIGINT, old_handler)
            except (ValueError, AttributeError):
                pass


async def run_retry_failed(config: ConfigManager, archive: ArchiveManager) -> None:
    failed = load_failed_urls()
    if not failed:
        print(f"\n{Style.tag('✨', 'RETRY', Style.GREEN)} {Style.bold('No failed downloads!')} data/failed_links.txt is clean.\n")
        return

    print(f"\n{Style.tag('🔄', 'RETRY', Style.YELLOW)} Found {Style.bold(str(len(failed)))} failed link(s) to retry:")
    for i, u in enumerate(failed[:5], 1):
        print(f"  {i}. {Style.dim(u)}")
    if len(failed) > 5:
        print(f"  {Style.dim(f'... and {len(failed) - 5} more')}")
    print()

    batch_queue = new_batch_queue()
    batch_queue.clear()
    batch_queue.add_batch(failed)
    clear_failed_urls()
    dispatcher = QueueDispatcher(config=config, queue_manager=batch_queue, archive=archive)
    return await dispatcher.run()


def setup_gallerydl_auth(config: ConfigManager) -> None:
    """Interactive OAuth and cookie configuration wizard for gallery-dl."""
    gdl_cfg_path = (ConfigManager.PROJECT_ROOT / "configs" / "gallery-dl.json").resolve()

    while True:
        print(f"\n{Style.tag('🔑', 'AUTH', Style.MAGENTA)} {Style.bold('Configure gallery-dl Authentication')}")
        print("  Select authentication service / method:")
        print(f"    [1] Pixiv (OAuth browser login)")
        print(f"    [2] Reddit (OAuth browser login)")
        print(f"    [3] Twitter / X (OAuth browser login)")
        print(f"    [4] Import cookies from browser (Firefox / Chrome / Edge / Brave)")
        print(f"    [5] Open configs/gallery-dl.json in editor")
        print(f"    [6] Done / Finish")
        try:
            choice = input(f"\n{Style.bold('Choose option [1-6]: ')}").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if choice == "1":
            print(f"\n{Style.tag('🌐', 'OAUTH', Style.CYAN)} Launching gallery-dl OAuth for Pixiv...")
            print(f"  {Style.dim('Instructions: Login in your browser, copy the code from the callback URL, and paste it below.')}")
            print(f"  {Style.dim('After entering the code, tokens will be automatically saved to configs/gallery-dl.json.')}\n")
            cmd = [sys.executable, "-m", "gallery_dl", "--config", str(gdl_cfg_path), "oauth:pixiv"]
            try:
                subprocess.run(cmd)
                print(f"\n{Style.tag('✅', 'OAUTH', Style.GREEN)} Pixiv authorization completed and tokens saved.")
            except Exception as e:
                print(f"\n{Style.tag('❌', 'ERROR', Style.RED)} OAuth failed: {e}")
        elif choice == "2":
            print(f"\n{Style.tag('🌐', 'OAUTH', Style.CYAN)} Launching gallery-dl OAuth for Reddit...")
            print(f"  {Style.dim('Instructions: Authorize in your browser, copy the response code/URL, and paste it below.')}\n")
            cmd = [sys.executable, "-m", "gallery_dl", "--config", str(gdl_cfg_path), "oauth:reddit"]
            try:
                subprocess.run(cmd)
                print(f"\n{Style.tag('✅', 'OAUTH', Style.GREEN)} Reddit authorization completed and tokens saved.")
            except Exception as e:
                print(f"\n{Style.tag('❌', 'ERROR', Style.RED)} OAuth failed: {e}")
        elif choice == "3":
            print(f"\n{Style.tag('🌐', 'OAUTH', Style.CYAN)} Launching gallery-dl OAuth for Twitter/X...")
            cmd = [sys.executable, "-m", "gallery_dl", "--config", str(gdl_cfg_path), "oauth:twitter"]
            try:
                subprocess.run(cmd)
                print(f"\n{Style.tag('✅', 'OAUTH', Style.GREEN)} Twitter authorization completed and tokens saved.")
            except Exception as e:
                print(f"\n{Style.tag('❌', 'ERROR', Style.RED)} OAuth failed: {e}")
        elif choice == "4":
            browser = input(f"{Style.bold('Enter browser name (firefox/chrome/edge/brave) [firefox]: ')}").strip().lower() or "firefox"
            config.set_path("gallery_dl.cookies_from_browser", browser)
            print(f"{Style.tag('✅', 'CONFIG', Style.GREEN)} Set gallery_dl.cookies_from_browser = {browser} in config.json.")
        elif choice == "5":
            handle_config_command(config, ["edit", "gallery-dl"])
        elif choice in ("6", "q", "exit", "done", ""):
            print(f"\n{Style.tag('✅', 'AUTH', Style.GREEN)} Completed gallery-dl authentication setup.\n")
            break
        else:
            print(f"{Style.dim('Invalid option. Please choose 1-6.')}")


SENSITIVE_CONFIG_KEYS = {
    "api_hash",
    "api_id",
    "ndus_cookie",
    "js_token",
    "token",
    "password",
    "secret",
    "auth_token",
    "access_token",
    "refresh_token",
    "oauth_token",
}


def sanitize_proxy_display(proxy_url: Optional[str]) -> str:
    """Masks credentials in proxy URLs like http://user:pass@host:port."""
    if not proxy_url:
        return "None (direct connection)"
    import urllib.parse
    try:
        parsed = urllib.parse.urlsplit(proxy_url)
        if parsed.password:
            return proxy_url.replace(f":{parsed.password}@", ":******@")
    except Exception:
        pass
    return proxy_url


def test_proxy(proxy_url: Optional[str] = None, timeout: int = 10) -> bool:
    """
    Tests network connectivity through a proxy (or direct connection if None).
    Prints latency, public IP, country, and ISP info.
    Returns True if reachable, False otherwise.
    """
    import time
    config = ConfigManager()
    active_proxy = proxy_url if proxy_url is not None else config.proxy
    display_proxy = sanitize_proxy_display(active_proxy)

    print(f"\n{Style.tag('🌐', 'PROXY TEST', Style.CYAN)} {Style.bold('Testing network connectivity...')}")
    if active_proxy:
        print(f"  • Proxy Target:      {Style.white(display_proxy)}")
    else:
        print(f"  • Proxy Target:      {Style.dim('None (direct connection)')}")

    proxies_dict = {"http": active_proxy, "https": active_proxy} if active_proxy else None

    # Step 1: Probe ip-api.com
    t0 = time.perf_counter()
    data = None
    err_msg = ""
    try:
        try:
            from curl_cffi import requests as c_req
            resp = c_req.get("http://ip-api.com/json", proxy=active_proxy, timeout=timeout)
        except ImportError:
            import requests as c_req
            resp = c_req.get("http://ip-api.com/json", proxies=proxies_dict, timeout=timeout)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code == 200:
            data = resp.json()
    except Exception as e:
        err_msg = str(e)

    # Step 2: Fallback to api.ipify.org if ip-api.com failed or was blocked
    if not data:
        t0 = time.perf_counter()
        try:
            try:
                from curl_cffi import requests as c_req
                resp = c_req.get("https://api.ipify.org?format=json", proxy=active_proxy, timeout=timeout)
            except ImportError:
                import requests as c_req
                resp = c_req.get("https://api.ipify.org?format=json", proxies=proxies_dict, timeout=timeout)

            latency_ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code == 200:
                data = resp.json()
                err_msg = ""
        except Exception as e:
            err_msg = str(e)

    if data:
        ip = data.get("query") or data.get("ip") or "Unknown"
        country = data.get("country", "")
        city = data.get("city", "")
        isp = data.get("isp") or data.get("org", "")
        loc_parts = [p for p in (city, country) if p]
        location_str = ", ".join(loc_parts) if loc_parts else ""

        if latency_ms < 500:
            lat_color = Style.green
        elif latency_ms < 1500:
            lat_color = Style.yellow
        else:
            lat_color = Style.red

        status_str = Style.green("ONLINE / CONNECTED")
        print(f"  • Status:            {status_str}")
        print(f"  • Latency:           {lat_color(f'{latency_ms} ms')}")
        print(f"  • Outgoing IP:       {Style.cyan(ip)}")
        if location_str:
            print(f"  • Location:          {Style.white(location_str)}")
        if isp:
            print(f"  • ISP / Provider:    {Style.dim(isp)}")

        if active_proxy:
            print(f"\n{Style.tag('✅', 'SUCCESS', Style.GREEN)} Proxy is functional and ready for downloads!\n")
        else:
            print(f"\n{Style.tag('ℹ️', 'DIRECT', Style.CYAN)} Direct internet connection verified.\n")
        return True
    else:
        clean_err = err_msg.replace("\n", " ").strip()
        if len(clean_err) > 120:
            clean_err = clean_err[:117] + "..."
        print(f"  • Status:            {Style.red('FAILED / UNREACHABLE')}")
        print(f"  • Error:             {Style.red(clean_err or 'Connection timed out')}")
        print(f"\n{Style.tag('❌', 'PROXY ERROR', Style.RED)} Could not establish connection through {display_proxy}.")
        print(f"  {Style.dim('Tips:')}")
        print(f"    • Check that your proxy application (e.g. WARP, Tor, Clash) is actively running.")
        print(f"    • Verify the host IP and port number.")
        print(f"    • Test in terminal with: {Style.cyan('curl.exe -x ' + (active_proxy or '') + ' https://api.ipify.org')}\n")
        return False


def handle_proxy_command(config: ConfigManager, args_list: List[str]):
    """
    Handles proxy subcommands:
      proxy                  -> shows status & tests active proxy
      proxy test [url]       -> tests current or specified proxy
      proxy set <url>        -> saves proxy to config and tests it
      proxy <url>            -> saves proxy to config and tests it (if looks like a URL)
      proxy clear / off / reset -> removes proxy from config
    """
    if not args_list or args_list[0].lower() in ("status", "info"):
        test_proxy()
        return

    sub = args_list[0].lower()

    if sub in ("test", "check"):
        target_url = args_list[1] if len(args_list) > 1 else None
        test_proxy(target_url)
        return

    if sub in ("off", "clear", "reset", "disable", "none", "remove"):
        config.set("network", "proxy", None)
        config.save()
        print(f"\n{Style.tag('🔄', 'PROXY', Style.YELLOW)} Cleared global proxy setting (now using direct connection).\n")
        return

    if sub == "set":
        if len(args_list) > 1:
            proxy_url = args_list[1].strip().strip('"\'')
            config.set("network", "proxy", proxy_url)
            config.save()
            print(f"\n{Style.tag('✅', 'SAVED', Style.GREEN)} Saved global proxy: {Style.white(sanitize_proxy_display(proxy_url))}")
            test_proxy(proxy_url)
        else:
            print(f"\n{Style.tag('⚠️', 'PROXY', Style.YELLOW)} Please specify a proxy URL, e.g.:")
            print(f"  {Style.cyan('proxy set socks5://127.0.0.1:40000')}  {Style.dim('(Cloudflare WARP)')}")
            print(f"  {Style.cyan('proxy set socks5://127.0.0.1:9150')}   {Style.dim('(Tor Browser)')}\n")
        return

    # If the argument looks like a proxy URL directly (e.g. proxy http://... or proxy socks5://...)
    if any(sub.startswith(proto) for proto in ("http://", "https://", "socks4://", "socks5://", "socks5h://")):
        proxy_url = args_list[0].strip().strip('"\'')
        config.set("network", "proxy", proxy_url)
        config.save()
        print(f"\n{Style.tag('✅', 'SAVED', Style.GREEN)} Saved global proxy: {Style.white(sanitize_proxy_display(proxy_url))}")
        test_proxy(proxy_url)
        return

    print(f"\n{Style.tag('🌐', 'PROXY COMMANDS', Style.CYAN)} {Style.bold('Proxy Management & Verification')}")
    print(f"  • {Style.cyan('proxy')}                  - Test currently configured proxy or direct connection")
    print(f"  • {Style.cyan('proxy test <url>')}       - Test a specific proxy without saving it")
    print(f"  • {Style.cyan('proxy set <url>')}        - Save proxy URL to config and verify connection")
    print(f"  • {Style.cyan('proxy off')}              - Disable proxy and revert to direct connection\n")



def format_config_value(key: str, val: Any) -> str:
    """Formats a configuration value for safe terminal display, masking sensitive credentials."""
    key_lower = str(key).lower().replace("-", "_")
    is_sensitive = any(s in key_lower for s in SENSITIVE_CONFIG_KEYS)

    ENV_CREDENTIAL_HINTS = {
        "api_id": "TELEGRAM_API_ID",
        "api_hash": "TELEGRAM_API_HASH",
        "ndus_cookie": "TERABOX_NDUS_COOKIE",
        "js_token": "TERABOX_JS_TOKEN",
        "proxy": "GLOBAL_PROXY",
    }

    if val is None or val == "":
        if key_lower in ENV_CREDENTIAL_HINTS:
            env_var = ENV_CREDENTIAL_HINTS[key_lower]
            if os.getenv(env_var) or (key_lower == "ndus_cookie" and os.getenv("TERABOX_NDUS")):
                return Style.green("******** (Configured via .env)")
        return Style.dim("None (not configured)")

    if is_sensitive and isinstance(val, (str, int)):
        return Style.yellow("******** (Configured)")

    if isinstance(val, bool):
        return Style.green("True") if val else Style.red("False")

    if isinstance(val, list):
        items_str = ", ".join(str(x) for x in val) if val else ""
        return Style.cyan(f"[{items_str}]")

    return Style.cyan(str(val))


def _print_formatted_section(sec_name: str, data: Any) -> None:
    """Renders a section dictionary in beautiful human-readable terminal styling with credential masking."""
    print(f"\n{Style.tag('⚙️', 'CONFIG', Style.CYAN)} {Style.bold(f'Section: {sec_name}')}\n")
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                print(f"  • {Style.bold(k)}:")
                for sub_k, sub_v in v.items():
                    print(f"      - {Style.white(str(sub_k))}: {format_config_value(sub_k, sub_v)}")
            elif isinstance(v, list):
                items_str = ", ".join(str(x) for x in v) if v else "[]"
                print(f"  • {Style.white(f'{k:<26}')}: {Style.cyan(f'[{items_str}]')}")
            else:
                print(f"  • {Style.white(f'{k:<26}')}: {format_config_value(k, v)}")
    else:
        print(f"  {Style.white(str(data))}")
    print()


def handle_config_command(config: ConfigManager, args_list: List[str]) -> None:
    """Handles viewing, querying, modifying, and resetting configuration settings."""
    if not args_list or (args_list[0].lower() in ("show", "list") and len(args_list) == 1):
        engine_limits = config.engine_limits
        domain_limits = config.domain_limits
        max_domain = config.get_max_per_domain()
        cfg_ver = config.get("version", "4.6.0")

        print(f"\n{Style.tag('⚙️', 'CONFIG', Style.CYAN)} {Style.bold(f'Current Configuration (v{cfg_ver})')}\n")
        print(f"  {Style.bold('🌐 General & Network:')}")
        print(f"    • Download Dir      {Style.dim('(download_dir)')}:      {Style.white(str(config.download_dir))}")
        print(f"    • Concurrent Pool   {Style.dim('(concurrent_downloads)')}: {Style.cyan(str(config.get('general', 'concurrent_downloads', 3)))} workers")
        proxy_val = sanitize_proxy_display(config.proxy)
        print(f"    • Global Proxy      {Style.dim('(proxy)')}:             {Style.white(str(proxy_val))}")
        print(f"    • Global Rate Limit {Style.dim('(rate_limit)')}:        {Style.white(str(config.get('network', 'rate_limit') or 'Unlimited'))}")
        print(f"    • Socket Timeout    {Style.dim('(timeout)')}:           {Style.white(str(config.get('network', 'timeout', 30)))}s")
        print(f"    • Notifications     {Style.dim('(notifications)')}:     {Style.green('Enabled') if config.notifications_enabled else Style.red('Disabled')}")
        print(f"    • Auto-Organization: By Backend={config.get('general', 'organize_by_backend', True)}, By Site={config.get('general', 'organize_by_site', True)}")

        print(f"\n  {Style.bold('⚡ Engine Concurrency Limits:')}")
        for eng, lim in engine_limits.items():
            print(f"    • {Style.engine_badge(eng)} {Style.dim(f'({eng})')}: {Style.cyan(str(lim))} concurrent jobs")

        print(f"\n  {Style.bold('🛡️ Domain Rate-Limiting Guard:')}")
        print(f"    • Max per domain    {Style.dim('(max_per_domain)')}:    {Style.cyan(str(max_domain if max_domain < 999 else 'Disabled'))} connection(s)/host")
        print(f"    • Guard Enabled     {Style.dim('(domain_limits.enabled)')}: {Style.green('True') if domain_limits.get('enabled', True) else Style.red('False')}")

        print(f"\n  {Style.bold('📦 Archive & Deduplication:')}")
        print(f"    • Database          {Style.dim('(archive.db_path)')}:   {Style.white(str(config.archive_db_path))} ({Style.green('Enabled') if config.archive_enabled else Style.red('Disabled')})")
        print(f"    • Dedup by URL      {Style.dim('(dedup_by_url)')}:      {config.get('archive', 'dedup_by_url', True)}")
        print(f"    • Verify on Disk    {Style.dim('(verify_file_exists)')}: {config.get('archive', 'verify_file_exists', True)}")

        print(f"\n  {Style.bold('🔧 Native Engine Config Files:')}")
        print(f"    • yt-dlp:            {Style.path('configs/yt-dlp.conf')}")
        print(f"    • gallery-dl:        {Style.path('configs/gallery-dl.json')}")
        print(f"    • cyberdrop-dl:      {Style.path('configs/cyberdrop-dl.json')}")
        print(f"    • telegram-dl:       {Style.path('configs/telegram-dl.json')}")
        print(f"    • terabox-dl:        {Style.path('configs/terabox-dl.json')}\n")

        print(f"  {Style.dim('💡 Usage Examples:')}")
        print(f"    • config set max_per_domain 3          (or domain_limits.max_per_domain)")
        print(f"    • config set yt-dlp 3                  (or engine_limits.yt-dlp)")
        print(f"    • config set proxy http://127.0.0.1:7890")
        print(f"    • config show telegram-dl              (safe masked view)")
        print(f"    • config edit gallery-dl               (open config in editor)\n")
        return

    sub = args_list[0].lower()

    if sub in ("show", "list"):
        if len(args_list) < 2:
            print(f"{Style.tag('⚠️', 'CONFIG', Style.YELLOW)} Usage: config show <section> (e.g. config show telegram-dl or config show yt_dlp)")
            return
        sec = args_list[1]
        val = config.get_path(sec)
        if val is None:
            val = config.get(sec)
        if val is not None:
            _print_formatted_section(sec, val)
        else:
            print(f"\n{Style.tag('⚠️', 'CONFIG', Style.YELLOW)} Section or key '{sec}' not found in configuration.\n")
        return

    if sub == "get":
        if len(args_list) < 2:
            print(f"{Style.tag('⚠️', 'CONFIG', Style.YELLOW)} Usage: config get <key> (e.g. config get max_per_domain or config get general.download_dir)")
            return
        key = args_list[1]
        resolved = config.resolve_key_path(key) or key
        val = config.get_path(key)
        if val is not None:
            last_key = resolved.split(".")[-1]
            formatted_val = format_config_value(last_key, val)
            print(f"\n{Style.tag('⚙️', 'CONFIG', Style.CYAN)} {Style.bold(resolved)} = {formatted_val}\n")
        else:
            print(f"{Style.tag('⚠️', 'CONFIG', Style.YELLOW)} Setting '{key}' not found.")
        return

    if sub == "set":
        if len(args_list) < 3:
            print(f"{Style.tag('⚠️', 'CONFIG', Style.YELLOW)} Usage: config set <key> <value> (e.g. config set max_per_domain 3 or config set engine_limits.yt-dlp 3)")
            return
        key = args_list[1]
        val_str = " ".join(args_list[2:])
        try:
            resolved_key = config.set_path(key, val_str)
            updated_val = config.get_path(resolved_key)
            last_key = resolved_key.split(".")[-1]
            formatted_val = format_config_value(last_key, updated_val)
            print(f"\n{Style.tag('✅', 'SAVED', Style.GREEN)} Set {Style.bold(resolved_key)} = {formatted_val} (saved to config.json)\n")
        except ConfigError as e:
            print(f"\n{Style.tag('❌', 'CONFIG ERROR', Style.RED)} {e}\n")
        return

    if sub == "edit":
        target = args_list[1].lower() if len(args_list) > 1 else "master"
        target_map = {
            "yt-dlp": "configs/yt-dlp.conf",
            "ytdlp": "configs/yt-dlp.conf",
            "gallery-dl": "configs/gallery-dl.json",
            "gallerydl": "configs/gallery-dl.json",
            "cyberdrop-dl": "configs/cyberdrop-dl.json",
            "cyberdrop": "configs/cyberdrop-dl.json",
            "telegram-dl": "configs/telegram-dl.json",
            "telegram": "configs/telegram-dl.json",
            "terabox-dl": "configs/terabox-dl.json",
            "terabox": "configs/terabox-dl.json",
            "master": "config.json",
            "main": "config.json",
            "all": "config.json",
            "config": "config.json",
        }
        rel_path = target_map.get(target, "config.json")
        full_path = (ConfigManager.PROJECT_ROOT / rel_path).resolve()
        if not full_path.exists():
            print(f"{Style.tag('⚠️', 'CONFIG', Style.YELLOW)} File does not exist: {full_path}")
            return

        print(f"\n{Style.tag('📝', 'EDIT', Style.CYAN)} Opening {Style.path(str(full_path))} in editor...")
        try:
            if sys.platform == "win32":
                DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                subprocess.Popen(
                    f'cmd.exe /c start "" "{full_path}"',
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=DETACHED | NEW_GROUP,
                )
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(full_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", str(full_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"{Style.tag('✅', 'EDIT', Style.GREEN)} File opened in default editor.\n")
        except Exception as e:
            print(f"{Style.tag('⚠️', 'ERROR', Style.YELLOW)} Could not open editor automatically: {e}")
            print(f"Edit manually at: {Style.path(str(full_path))}\n")
        return

    if sub == "reset":
        confirm = input(f"{Style.tag('⚠️', 'RESET CONFIG', Style.YELLOW)} Reset config.json to factory defaults? (y/N): ").strip().lower()
        if confirm in ("y", "yes"):
            config.reset_defaults()
            print(f"\n{Style.tag('✅', 'CONFIG', Style.GREEN)} Configuration reset to factory defaults (v{config.get('version')}).\n")
        else:
            print(f"{Style.tag('ℹ️', 'CONFIG', Style.DIM)} Reset cancelled.\n")
        return

    # Check if user queried a single key or section: e.g. 'config proxy' or 'config telegram-dl'
    if len(args_list) == 1:
        single_key = args_list[0]
        val = config.get_path(single_key)
        if val is not None:
            if isinstance(val, dict):
                _print_formatted_section(single_key, val)
            else:
                resolved = config.resolve_key_path(single_key) or single_key
                formatted_val = format_config_value(resolved.split(".")[-1], val)
                print(f"\n{Style.tag('⚙️', 'CONFIG', Style.CYAN)} {Style.bold(resolved)} = {formatted_val}\n")
        else:
            print(f"\n{Style.tag('⚠️', 'CONFIG', Style.YELLOW)} Unknown config command or setting '{single_key}'.")
            print(f"  {Style.dim('Type')} {Style.cyan('config')} {Style.dim('to see all available settings and usage examples.')}\n")
        return

    # User may have omitted 'set' (e.g. 'config max_per_domain 3' or 'config proxy http://...')
    candidate_key = args_list[0]
    resolved = config.resolve_key_path(candidate_key)
    if resolved:
        val_str = " ".join(args_list[1:])
        try:
            resolved_key = config.set_path(candidate_key, val_str)
            updated_val = config.get_path(resolved_key)
            last_key = resolved_key.split(".")[-1]
            formatted_val = format_config_value(last_key, updated_val)
            print(f"\n{Style.tag('✅', 'SAVED', Style.GREEN)} Set {Style.bold(resolved_key)} = {formatted_val} (saved to config.json)\n")
        except ConfigError as e:
            print(f"\n{Style.tag('❌', 'CONFIG ERROR', Style.RED)} {e}\n")
        return

    # Unrecognized command/phrase (e.g. 'config Max per domain 3')
    print(f"\n{Style.tag('❌', 'CONFIG ERROR', Style.RED)} Unknown config command or setting '{candidate_key}'.")
    if "domain" in " ".join(args_list).lower():
        print(f"  {Style.dim('Did you mean:')} {Style.cyan(f'config set max_per_domain {args_list[-1]}')}?")
    print(f"  {Style.dim('Valid subcommands: set, get, show, edit, reset. Type')} {Style.cyan('config')} {Style.dim('for overview.')}\n")


async def interactive_mode():
    config = ConfigManager()
    archive = ArchiveManager(config.archive_db_path)
    print_banner(config, archive)

    while True:
        try:
            url_input = input(f"{Style.bold(Style.cyan('Enter URL: '))}{Style.RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{Style.dim('Goodbye!')}")
            break

        if not url_input:
            continue

        cmd_candidate = url_input.lstrip(":/").strip().lower()

        # 1. Exit / Quit
        if cmd_candidate in ("exit", "quit", "q"):
            print(f"{Style.dim('Goodbye!')}")
            break

        # 2. Help
        if cmd_candidate in ("help", "?"):
            print(f"\n{Style.tag('💡', 'COMMANDS', Style.CYAN)} {Style.bold('Interactive Mode Commands')}")
            print(f"  • {Style.cyan('watch')}             - Monitor system clipboard to auto-capture & download links")
            print(f"  • {Style.cyan('retry')}             - Re-download failed links from previous session")
            print(f"  • {Style.cyan('open')}              - Open downloads directory in File Explorer")
            print(f"  • {Style.cyan('archive')}           - View download history, summary stats & recent files")
            print(f"  • {Style.cyan('queue')}             - View pending and queued downloads")
            print(f"  • {Style.cyan('notifications on/off')} - Toggle sound notification alerts (persisted to config)")
            print(f"  • {Style.cyan('proxy')} [test|url|off]   - Test or configure network proxy (e.g. proxy test, proxy off)")
            print(f"  • {Style.cyan('clear')}             - Clear terminal screen and redraw banner")
            print(f"  • {Style.cyan('config')}            - View full configuration summary")
            print(f"  • {Style.cyan('config set <k> <v>')} - Modify setting (e.g. config set engine_limits.yt-dlp 3)")
            print(f"  • {Style.cyan('config get <k>')}    - View specific setting value")
            print(f"  • {Style.cyan('config edit <eng>')} - Open engine config in editor (yt-dlp, gallery-dl, etc.)")
            print(f"  • {Style.cyan('config reset')}      - Reset configuration to defaults")
            print(f"  • {Style.cyan('auth')}              - Run engine authorization wizard (Telegram, TeraBox, gallery-dl)")
            print(f"  • {Style.cyan('update')}            - Check and update all download engines")
            print(f"  • {Style.cyan('exit')}              - Exit interactive mode (or Ctrl+C)\n")
            print(f"  {Style.dim('Quick Filters: You can append inline flags when pasting links:')}")
            print(f"    • {Style.white('-x, --audio-only')}          {Style.dim('Extract audio only as MP3')}")
            print(f"    • {Style.white('-r 1080p, -r 720p')}         {Style.dim('Resolution limit preset')}")
            print(f"    • {Style.white('--video-only')}              {Style.dim('Filter to video streams/files only')}")
            print(f"    • {Style.white('--images-only')}             {Style.dim('Filter to image files only')}")
            print(f"    • {Style.white('--no-sound, -s')}            {Style.dim('Mute notification chime for this download')}\n")
            continue

        # 2b. Notifications Toggle Commands
        if cmd_candidate in ("notifications off", "notification off", "sound off", "config notifications off", "config sound off"):
            config.set("general", "notifications", False)
            config.set("general", "play_sound", False)
            config.save()
            print(f"\n{Style.tag('🔇', 'NOTIFICATIONS', Style.YELLOW)} Audio completion chime {Style.red('DISABLED')} (saved to config.json)\n")
            continue

        if cmd_candidate in ("notifications on", "notification on", "sound on", "config notifications on", "config sound on"):
            config.set("general", "notifications", True)
            config.set("general", "play_sound", True)
            config.save()
            print(f"\n{Style.tag('🔔', 'NOTIFICATIONS', Style.GREEN)} Audio completion chime {Style.green('ENABLED')} (saved to config.json)\n")
            continue

        if cmd_candidate in ("notifications", "notification", "sound"):
            st = Style.green("ENABLED") if config.notifications_enabled else Style.red("DISABLED")
            print(f"\n{Style.tag('🔔', 'NOTIFICATIONS', Style.CYAN)} Audio completion chime is currently {st}")
            print(f"  {Style.dim('Type \"notifications off\" or \"notifications on\" to change.')}\n")
            continue

        # 2c. Proxy Test & Management Commands
        if cmd_candidate in ("proxy", "check proxy", "test proxy") or cmd_candidate.startswith("proxy ") or cmd_candidate.startswith("test proxy "):
            parts = url_input.strip().split()[1:]
            handle_proxy_command(config, parts)
            continue

        # 2d. Launch Desktop GUI
        if cmd_candidate in ("gui", "ui", "desktop"):
            launch_desktop_gui(spawn_process=True)
            continue

        # 3. Open downloads folder
        if cmd_candidate in ("open", "folder", "downloads"):
            dl_dir = config.download_dir
            dl_dir.mkdir(parents=True, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(str(dl_dir.resolve()))
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(dl_dir.resolve())])
                else:
                    subprocess.run(["xdg-open", str(dl_dir.resolve())])
                print(f"\n{Style.tag('📂', 'OPEN', Style.GREEN)} Opened in File Explorer: {Style.white(str(dl_dir))}\n")
            except Exception as e:
                print(f"\n{Style.tag('⚠️', 'ERROR', Style.YELLOW)} Could not open folder: {e}\n")
            continue

        # 4. Archive view
        if cmd_candidate in ("archive", "history", "stats"):
            stats = archive.get_stats()
            print(f"\n{Style.tag('📊', 'STATS', Style.CYAN)} {Style.bold('Download Archive Summary')}")
            print(f"  • Database:        {Style.white(str(config.archive_db_path))}")
            print(f"  • Total Downloads: {Style.green(str(stats['total_count']))}")
            print(f"  • Total Size:      {Style.green(format_bytes(stats['total_bytes']))}")
            print(f"  • By Backend:")
            for b, cnt in stats["by_backend"].items():
                print(f"    - {Style.engine_badge(b)}: {Style.white(str(cnt))}")
            recent = archive.list_recent(limit=10)
            if recent:
                print(f"\n{Style.dim('Recent Entries:')}")
                _print_archive_table(recent)
            else:
                print()
            continue

        # 5. Queue view
        if cmd_candidate in ("queue", "list"):
            persist_q = QueueManager(PROJECT_ROOT / "data" / "queue.json")
            print_queue_list(persist_q, is_batch=False)
            continue

        # 6. Clear screen
        if cmd_candidate in ("clear", "cls"):
            print("\033[2J\033[H", end="")
            print_banner(config, archive)
            continue

        # 7. Config view & management
        if cmd_candidate in ("config", "cfg", "settings") or cmd_candidate.startswith("config ") or cmd_candidate.startswith("cfg "):
            parts = url_input.strip().split()[1:]
            handle_config_command(config, parts)
            continue

        # 7b. Auth management
        if cmd_candidate in ("auth", "login") or cmd_candidate.startswith("auth ") or cmd_candidate.startswith("login "):
            parts = url_input.strip().split()[1:]
            target = parts[0].lower() if parts else "all"
            if target in ("telegram", "telegram-dl", "all"):
                print(f"\n{Style.tag('🔑', 'AUTH', Style.BLUE)} Initializing Telegram authorization...")
                tg_backend = TelegramDlBackend(config.get_backend_config("telegram-dl"))
                try:
                    await tg_backend._ensure_client()
                    print(f"{Style.tag('✅', 'READY', Style.GREEN)} Telegram account is authorized and ready.")
                except Exception as e:
                    print(f"{Style.tag('❌', 'AUTH ERROR', Style.RED)} {Style.error(str(e))}")
            if target in ("terabox", "terabox-dl", "all"):
                print(f"\n{Style.tag('🔑', 'AUTH', Style.CYAN)} Configuring TeraBox credentials...")
                tb_backend = TeraboxDlBackend(config.get_backend_config("terabox-dl"))
                existing_cookie = tb_backend._resolve_ndus_cookie()
                if existing_cookie:
                    masked = existing_cookie[:4] + "..." + existing_cookie[-4:] if len(existing_cookie) > 8 else "***"
                    print(f"{Style.tag('✅', 'READY', Style.GREEN)} TeraBox account is already authorized with cookie ({Style.cyan(masked)}).")
                    try:
                        ans = input(f"  {Style.dim('Update existing cookie? [y/N]: ')}").strip().lower()
                    except (KeyboardInterrupt, EOFError):
                        ans = "n"
                    if ans in ("y", "yes"):
                        try:
                            tb_backend._prompt_ndus_cookie()
                            print(f"{Style.tag('✅', 'READY', Style.GREEN)} TeraBox credentials configured.")
                        except Exception as e:
                            print(f"{Style.tag('❌', 'AUTH ERROR', Style.RED)} {Style.error(str(e))}")
                else:
                    try:
                        tb_backend._prompt_ndus_cookie()
                        print(f"{Style.tag('✅', 'READY', Style.GREEN)} TeraBox credentials configured.")
                    except Exception as e:
                        print(f"{Style.tag('❌', 'AUTH ERROR', Style.RED)} {Style.error(str(e))}")
            if target in ("gallery", "gallery-dl", "all"):
                setup_gallerydl_auth(config)
            continue

        # 8. Engine Update
        if cmd_candidate in ("update", "u"):
            print(f"\n{Style.tag('🔄', 'UPDATE', Style.CYAN)} Checking and updating download engines...\n")
            updater = EngineUpdater()
            updater.update_all()
            print()
            continue

        # 9. Clipboard Auto-Watcher
        if cmd_candidate in ("watch", "clipboard", "sniff"):
            await run_clipboard_watcher_session(config, archive)
            continue

        # 10. Retry failed downloads
        if cmd_candidate in ("retry", "failed"):
            await run_retry_failed(config, archive)
            continue

        # Extract any inline options (e.g. -x, --video-only, --images-only, -r 1080p, --no-sound)
        cleaned_input, inline_opts = extract_inline_options(url_input)
        if not cleaned_input and not inline_opts:
            continue

        extracted_urls, source_files = resolve_input_urls([cleaned_input])
        if not extracted_urls:
            continue

        forced_backend = inline_opts.pop("backend", None)

        # Show feedback if inline filters were passed
        filter_tags = []
        if inline_opts.get("audio_only"):
            filter_tags.append(f"{Style.magenta('[Audio Only / MP3]')}")
        if inline_opts.get("video_only"):
            filter_tags.append(f"{Style.blue('[Video Only]')}")
        if inline_opts.get("images_only"):
            filter_tags.append(f"{Style.yellow('[Images Only]')}")
        if inline_opts.get("resolution"):
            res_val = inline_opts['resolution']
            filter_tags.append(Style.cyan(f"[{res_val}]"))
        if inline_opts.get("no_sound"):
            filter_tags.append(f"{Style.dim('[Silent]')}")
        if filter_tags:
            print(f"  {Style.dim('Filter Active:')} {' '.join(filter_tags)}")

        if len(extracted_urls) > 1 or source_files:
            if source_files:
                file_names = ", ".join(Path(f).name for f in source_files)
                print(f"\n{Style.tag('📄', 'BATCH FILE', Style.CYAN)} Read {Style.bold(str(len(extracted_urls)))} links from {Style.white(file_names)}\n")
            else:
                print(f"\n{Style.tag('📦', 'BATCH', Style.MAGENTA)} Detected {Style.bold(str(len(extracted_urls)))} links pasted\n")

            batch_queue = new_batch_queue()
            batch_queue.clear()
            batch_queue.add_batch(extracted_urls, backend=forced_backend, options=inline_opts)

            dispatcher = QueueDispatcher(config=config, queue_manager=batch_queue, archive=archive)
            await dispatcher.run()
        else:
            await download_url(extracted_urls[0], backend_name=forced_backend, options=inline_opts)

        term_width = shutil.get_terminal_size((80, 24)).columns
        sep_width = min(66, max(40, term_width - 4))
        print(f"\n{Style.CYAN}{'─' * sep_width}{Style.RESET}\n")


def launch_desktop_gui(spawn_process: bool = False) -> None:
    """Launches the modern desktop GUI application with robust interpreter resolution."""
    gui_script = PROJECT_ROOT / "multi-dl-gui.py"
    if not gui_script.exists():
        print(f"{Style.tag('❌', 'ERROR', Style.RED)} GUI script not found at: {gui_script}")
        return

    # Check if flet is available in current python or alternative interpreters
    candidates = [sys.executable]
    venv_py = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        candidates.append(str(venv_py))
    candidates.extend(["python", "pythonw"])

    python_bin = None
    for cand in candidates:
        try:
            res = subprocess.run([cand, "-c", "import flet"], capture_output=True, timeout=5)
            if res.returncode == 0:
                python_bin = cand
                break
        except Exception:
            continue

    if not python_bin:
        print(f"\n{Style.tag('❌', 'DEPENDENCY MISSING', Style.RED)} Desktop GUI requires {Style.bold('flet')}.")
        print(f"{Style.dim('   Please install it with: ')}{Style.cyan('pip install flet')} (or {Style.cyan('pip install -r requirements.txt')})\n")
        return

    print(f"\n{Style.tag('🖥️', 'GUI', Style.CYAN)} Launching MULTI_DOWNLOADER Desktop GUI...")
    if spawn_process:
        try:
            subprocess.Popen([python_bin, str(gui_script)])
            print(f"{Style.tag('✅', 'LAUNCHED', Style.GREEN)} GUI process spawned in separate window.\n")
        except Exception as e:
            print(f"{Style.tag('❌', 'GUI ERROR', Style.RED)} Failed to start GUI: {e}\n")
    else:
        try:
            import flet as ft
            import importlib
            gui_module = importlib.import_module("multi-dl-gui")
            if hasattr(ft, "app"):
                ft.app(target=gui_module.main)
            elif hasattr(ft, "run"):
                ft.run(gui_module.main)
        except Exception:
            subprocess.run([python_bin, str(gui_script)])


def main():
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    raw_args = sys.argv[1:]

    # Interactive mode: running without arguments or with -i / --interactive / interactive
    if not raw_args or raw_args[0] in ("-i", "--interactive", "interactive"):
        asyncio.run(interactive_mode())
        return

    # Fast-path for Update command / flag: multi-dl -U / multi-dl --update / multi-dl update [engine]
    if any(arg in ("-U", "--update") for arg in raw_args) or (raw_args and raw_args[0] == "update"):
        remaining = [a for a in raw_args if a not in ("-U", "--update", "update")]
        target = remaining[0].lower() if remaining else "all"
        updater = EngineUpdater()
        if target in ("all", ""):
            updater.update_all()
        elif target in ("yt-dlp", "ytdlp"):
            ok, msg = updater.update_ytdlp()
            print(f"yt-dlp: {msg}")
        elif target in ("gallery-dl", "gallerydl"):
            ok, msg = updater.update_gallerydl()
            print(f"gallery-dl: {msg}")
        elif target in ("curl_cffi", "curlcffi"):
            ok, msg = updater.update_curl_cffi()
            print(f"curl_cffi: {msg}")
        elif target in ("telegram-dl", "telegramdl", "telegram"):
            ok, msg = updater.update_telegramdl()
            print(f"Telegram-dl: {msg}")
        elif target in ("terabox-dl", "teraboxdl", "terabox"):
            ok, msg = updater.update_teraboxdl()
            print(f"Terabox-dl: {msg}")
        elif target in ("cyberdrop-dl", "cyberdropdl", "cyberdrop"):
            ok, msg = updater.update_cyberdropdl()
            print(f"cyberdrop-dl: {msg}")
        elif target in ("multi-dl", "self"):
            ok, msg = updater.update_self()
            print(f"MULTI_DOWNLOADER: {msg}")
        else:
            print(f"{Style.tag('❌', 'UNKNOWN', Style.RED)} Unknown engine '{target}'. Use: yt-dlp, gallery-dl, terabox-dl, telegram-dl, cyberdrop-dl, multi-dl (this app), curl_cffi (network package), or all.")
        return

    # Version flag check
    if any(arg in sys.argv[1:] for arg in ("-v", "--version")):
        print(f"MULTI_DOWNLOADER v{__version__}")
        return

    # Fast-path for Clipboard Watcher: multi-dl watch / -w / --watch / clipboard
    if raw_args and raw_args[0] in ("watch", "clipboard", "--watch", "-w"):
        if '--clear-history' in raw_args:
            clear_clipboard_history()
            print('Clipboard URL history cleared.')
            return
        config = ConfigManager()
        archive = ArchiveManager(config.archive_db_path)
        asyncio.run(run_clipboard_watcher_session(config, archive))
        return

    # Fast-path for Retry failed downloads: multi-dl retry / --retry
    if raw_args and raw_args[0] in ("retry", "--retry"):
        if '--clear' in raw_args:
            clear_failed_urls()
            print('Failed-link recovery history cleared.')
            return
        config = ConfigManager()
        archive = ArchiveManager(config.archive_db_path)
        run_download(run_retry_failed(config, archive))
        return

    # Fast-path for Desktop GUI: multi-dl gui / ui / --gui
    if raw_args and raw_args[0] in ("gui", "ui", "--gui"):
        launch_desktop_gui(spawn_process=False)
        return

    # Subcommands list
    subcommands = {"archive", "route", "queue", "config", "update", "auth", "login", "download", "interactive", "watch", "retry", "proxy", "gui", "ui"}

    # Direct URL or batch execution mode: e.g. python multi-dl.py urls.txt / python multi-dl.py https://... [-c 3]
    if raw_args and raw_args[0] not in subcommands and not raw_args[0].startswith("-h") and not raw_args[0] == "--help":
        direct_parser = argparse.ArgumentParser(
            description="MULTI_DOWNLOADER CLI - Direct & Batch Downloader",
            epilog="Tip: You can download directly with: python multi-dl.py <URL or textfile>"
        )
        direct_parser.add_argument("urls", nargs="*", help="Media URLs or text file paths to download")
        direct_parser.add_argument("-f", "--file", "--input", dest="file", help="Path to text file containing URLs")
        direct_parser.add_argument("-c", "--concurrency", type=int, help="Number of concurrent worker streams")
        direct_parser.add_argument("-b", "--backend", help="Force specific backend (yt-dlp, gallery-dl, terabox-dl, telegram-dl, cyberdrop-dl)")
        direct_parser.add_argument("--format", help="Format selection string (e.g. bestvideo*+bestaudio/best)")
        direct_parser.add_argument("-x", "--extract-audio", "--audio-only", dest="audio_only", action="store_true", help="Extract audio only (converts to mp3 / audio format)")
        direct_parser.add_argument("--audio-format", help="Audio format when extracting audio (mp3, m4a, flac, wav, opus)")
        direct_parser.add_argument("--video-only", "--videos-only", dest="video_only", action="store_true", help="Download video streams/files only")
        direct_parser.add_argument("--images-only", "--image-only", "--photos-only", dest="images_only", action="store_true", help="Download image files only")
        direct_parser.add_argument("-r", "--resolution", "--res", dest="resolution", help="Max resolution limit preset (e.g. 1080p, 720p, 480p, 4k, best)")
        direct_parser.add_argument("--proxy", help="Proxy URL (e.g. socks5://127.0.0.1:1080, http://127.0.0.1:8080)")
        direct_parser.add_argument("-s", "--silent", "--no-sound", "--no-notification", "--no-notifications", dest="no_sound", action="store_true", help="Disable audio completion chime")
        direct_parser.add_argument("--cookies-from-browser", help="Load cookies from browser (e.g. chrome, firefox, edge, brave, opera)")
        direct_parser.add_argument("--cookies", help="Path to cookies.txt file")
        direct_parser.add_argument("--ndus", help="TeraBox ndus session cookie")
        direct_parser.add_argument("--force", action="store_true", help="Force re-download even if already in archive or on disk")
        direct_parser.add_argument("--no-archive", action="store_true", help="Do not check or record download in the archive")
        direct_parser.add_argument("--impersonate", help="Client impersonation target (e.g. chrome, firefox, edge, safari)")
        parsed_direct = direct_parser.parse_args(raw_args)

        raw_inputs = list(parsed_direct.urls or [])
        format_val = parsed_direct.format
        if parsed_direct.file:
            f_str = parsed_direct.file
            p_check = Path(f_str.strip('"\''))
            if (p_check.exists() and p_check.is_file()) or f_str.lower().endswith((".txt", ".list", ".urls")):
                raw_inputs.append(f_str)
            elif not format_val:
                format_val = f_str
            else:
                raw_inputs.append(f_str)

        opts = {}
        cleaned_inputs = []
        for inp in raw_inputs:
            c_inp, i_opts = extract_inline_options(inp)
            if i_opts:
                opts.update(i_opts)
            if c_inp:
                cleaned_inputs.append(c_inp)

        extracted_urls, source_files = resolve_input_urls(cleaned_inputs)
        if not extracted_urls:
            if not raw_inputs:
                direct_parser.print_help()
            else:
                print(f"{Style.tag('⚠️', 'DOWNLOAD', Style.YELLOW)} No valid URLs found to download.")
            return

        if parsed_direct.cookies_from_browser:
            opts["cookies_from_browser"] = parsed_direct.cookies_from_browser
        if parsed_direct.cookies:
            opts["cookies"] = parsed_direct.cookies
        if format_val:
            opts["format"] = format_val
        if parsed_direct.ndus:
            opts["ndus"] = parsed_direct.ndus
        if parsed_direct.force:
            opts["force"] = True
        if parsed_direct.no_archive:
            opts["no_archive"] = True
        if parsed_direct.audio_only:
            opts["audio_only"] = True
        if parsed_direct.audio_format:
            opts["audio_format"] = parsed_direct.audio_format
        if parsed_direct.video_only:
            opts["video_only"] = True
        if parsed_direct.images_only:
            opts["images_only"] = True
        if parsed_direct.resolution:
            opts["resolution"] = parsed_direct.resolution
        if parsed_direct.proxy:
            opts["proxy"] = parsed_direct.proxy
        if parsed_direct.no_sound:
            opts["no_sound"] = True
        if parsed_direct.impersonate:
            opts["impersonate"] = parsed_direct.impersonate

        # Single direct URL mode
        if len(extracted_urls) == 1 and not source_files and not parsed_direct.concurrency:
            run_download(download_url(extracted_urls[0], parsed_direct.backend, options=opts))
            return

        # Immediate concurrent batch mode
        if source_files:
            file_names = ", ".join(Path(f).name for f in source_files)
            print(f"\n{Style.tag('📄', 'BATCH FILE', Style.CYAN)} Read {Style.bold(str(len(extracted_urls)))} links from {Style.white(file_names)}")
        else:
            print(f"\n{Style.tag('📦', 'BATCH', Style.MAGENTA)} Ingested {Style.bold(str(len(extracted_urls)))} links for immediate concurrent download...")

        config = ConfigManager()
        if opts.get("no_sound"):
            config._data.setdefault("general", {})["notifications"] = False
            config._data.setdefault("general", {})["play_sound"] = False
        archive = ArchiveManager(config.archive_db_path)
        batch_queue = new_batch_queue()
        batch_queue.clear()
        batch_queue.add_batch(extracted_urls, backend=parsed_direct.backend, options=opts)

        dispatcher = QueueDispatcher(config=config, queue_manager=batch_queue, archive=archive)
        run_download(dispatcher.run(concurrency=parsed_direct.concurrency))
        return

    # Subcommand mode: e.g. python multi-dl.py archive --stats
    parser = argparse.ArgumentParser(
        description="MULTI_DOWNLOADER CLI - Universal Media Downloader",
        epilog="Tip: You can download directly with: python multi-dl.py <URL or textfile>"
    )
    parser.add_argument("-v", "--version", action="version", version=f"MULTI_DOWNLOADER v{__version__}")
    parser.add_argument("-U", "--update", action="store_true", help="Update all download engines to their latest versions")
    parser.add_argument("-i", "--interactive", action="store_true", help="Launch interactive link pasting session")
    parser.add_argument("-f", "--file", "--input", dest="file", help="Process a text file of URLs with concurrent workers")
    parser.add_argument("-c", "--concurrency", type=int, help="Number of concurrent worker streams")
    parser.add_argument("-x", "--extract-audio", "--audio-only", dest="audio_only", action="store_true", help="Extract audio only (converts to mp3 / audio format)")
    parser.add_argument("--audio-format", help="Audio format when extracting audio (mp3, m4a, flac, wav, opus)")
    parser.add_argument("--video-only", "--videos-only", dest="video_only", action="store_true", help="Download video streams/files only")
    parser.add_argument("--images-only", "--image-only", "--photos-only", dest="images_only", action="store_true", help="Download image files only")
    parser.add_argument("-r", "--resolution", "--res", dest="resolution", help="Max resolution limit preset (e.g. 1080p, 720p, 480p, 4k, best)")
    parser.add_argument("--proxy", help="Proxy URL (e.g. socks5://127.0.0.1:1080, http://127.0.0.1:8080)")
    parser.add_argument("-s", "--silent", "--no-sound", "--no-notification", "--no-notifications", dest="no_sound", action="store_true", help="Disable audio completion chime")
    parser.add_argument("--force", action="store_true", help="Force re-download even if already archived or on disk")
    parser.add_argument("--no-archive", action="store_true", help="Bypass download archive recording and deduplication")
    subparsers = parser.add_subparsers(dest="command")

    # 'interactive' command
    subparsers.add_parser("interactive", help="Start interactive link pasting session")

    # 'gui' command
    subparsers.add_parser("gui", help="Launch MULTI_DOWNLOADER Modern Desktop GUI application")
    subparsers.add_parser("ui", help="Alias for 'gui'")

    # 'auth' / 'login' command
    auth_parser = subparsers.add_parser("auth", help="Configure engine authentication (telegram, terabox, gallery-dl, all)")
    auth_parser.add_argument("engine", nargs="?", default="all", help="Engine to authenticate (telegram, terabox, gallery-dl, all)")
    subparsers.add_parser("login", help="Alias for 'auth'")

    # 'config' command
    cfg_parser = subparsers.add_parser("config", help="View, query, or modify system configuration settings")
    cfg_parser.add_argument("subargs", nargs="*", help="Config action or arguments (e.g. set engine_limits.yt-dlp 3, get general.download_dir, edit yt-dlp, reset)")

    # 'proxy' command
    proxy_parser = subparsers.add_parser("proxy", help="Inspect, test, or configure network proxy")
    proxy_parser.add_argument("proxy_args", nargs="*", help="Proxy action or URL (e.g. test, set <url>, off)")

    # Optional 'download' subcommand (for backward compatibility)
    dl_parser = subparsers.add_parser("download", help="Download URLs or text files directly")
    dl_parser.add_argument("urls", nargs="*", help="Media URLs or text file paths to download")
    dl_parser.add_argument("-f", "--file", "--input", dest="file", help="Path to text file containing URLs")
    dl_parser.add_argument("-c", "--concurrency", type=int, help="Number of concurrent worker streams")
    dl_parser.add_argument("-b", "--backend", help="Force specific backend (yt-dlp, gallery-dl, terabox-dl, telegram-dl, cyberdrop-dl)")
    dl_parser.add_argument("--format", help="Format selection string")
    dl_parser.add_argument("-x", "--extract-audio", "--audio-only", dest="audio_only", action="store_true", help="Extract audio only (converts to mp3 / audio format)")
    dl_parser.add_argument("--audio-format", help="Audio format when extracting audio (mp3, m4a, flac, wav, opus)")
    dl_parser.add_argument("--video-only", "--videos-only", dest="video_only", action="store_true", help="Download video streams/files only")
    dl_parser.add_argument("--images-only", "--image-only", "--photos-only", dest="images_only", action="store_true", help="Download image files only")
    dl_parser.add_argument("-r", "--resolution", "--res", dest="resolution", help="Max resolution limit preset (e.g. 1080p, 720p, 480p, 4k, best)")
    dl_parser.add_argument("--proxy", help="Proxy URL (e.g. socks5://127.0.0.1:1080, http://127.0.0.1:8080)")
    dl_parser.add_argument("-s", "--silent", "--no-sound", "--no-notification", "--no-notifications", dest="no_sound", action="store_true", help="Disable audio completion chime")
    dl_parser.add_argument("--cookies-from-browser", help="Load cookies from browser (e.g. chrome, firefox, edge, brave)")
    dl_parser.add_argument("--cookies", help="Path to cookies.txt file")
    dl_parser.add_argument("--ndus", help="TeraBox ndus session cookie")
    dl_parser.add_argument("--force", action="store_true", help="Force re-download even if already archived or on disk")
    dl_parser.add_argument("--no-archive", action="store_true", help="Bypass download archive recording and deduplication")
    dl_parser.add_argument("--impersonate", help="Client impersonation target (e.g. chrome, firefox, edge, safari)")

    # 'update' command
    up_parser = subparsers.add_parser("update", help="Update download engines to their latest versions")
    up_parser.add_argument("engine", nargs="?", default="all", help="Specific engine to update (all, yt-dlp, gallery-dl, terabox-dl, telegram-dl, cyberdrop-dl)")

    # 'archive' command
    arc_parser = subparsers.add_parser("archive", help="Inspect download archive")
    arc_parser.add_argument("--stats", action="store_true", help="Show summary metrics")
    arc_parser.add_argument("--search", "-s", help="Search history by keyword")
    arc_parser.add_argument("--list", "-l", action="store_true", help="List recent download records")
    arc_parser.add_argument("--limit", type=int, default=20, help="Max entries to display (default: 20)")
    arc_parser.add_argument("--remove", help="Remove an entry by URL or ID from the archive")
    arc_parser.add_argument("--clear", action="store_true", help="Clear all records from the archive")

    # 'route' command (test routing)
    route_parser = subparsers.add_parser("route", help="Test URL routing")
    route_parser.add_argument("url", help="URL to test")

    # 'queue' command
    q_parser = subparsers.add_parser("queue", help="Manage download queue and run concurrent worker pool")
    q_subparsers = q_parser.add_subparsers(dest="queue_action")

    # queue add
    q_add = q_subparsers.add_parser("add", help="Add URLs to queue (with automatic pre-routing)")
    q_add.add_argument("urls", nargs="*", help="Media URLs or text file paths to queue")
    q_add.add_argument("-f", "--file", "--input", dest="file", help="Path to text file containing URLs")
    q_add.add_argument("-p", "--priority", type=int, default=0, help="Priority (higher processed first)")
    q_add.add_argument("-b", "--backend", help="Force specific backend engine")
    q_add.add_argument("--batch", action="store_true", help="Add to batch_queue.json instead of persistent queue.json")

    # queue list
    q_list = q_subparsers.add_parser("list", help="Display all queued downloads")
    q_list.add_argument("--status", help="Filter by status (queued, downloading, completed, failed, skipped)")
    q_list.add_argument("--batch", action="store_true", help="View active batch queue instead of persistent queue")

    # queue start
    q_start = q_subparsers.add_parser("start", help="Process download queue with concurrent workers")
    q_start.add_argument("-c", "--concurrency", type=int, help="Number of concurrent worker streams")
    q_start.add_argument("--batch", action="store_true", help="Process active batch queue instead of persistent queue")

    # queue retry
    q_retry = q_subparsers.add_parser("retry", help="Reset all failed downloads back to queued status")
    q_retry.add_argument("--batch", action="store_true", help="Retry failed items in batch queue")

    # queue clear
    q_clear = q_subparsers.add_parser("clear", help="Clear downloads from queue")
    q_clear.add_argument("--status", help="Clear only specific status (completed, failed, queued, or all)")
    q_clear.add_argument("--batch", action="store_true", help="Clear batch queue instead of persistent queue")

    args = parser.parse_args()

    if getattr(args, "interactive", False) or args.command == "interactive":
        asyncio.run(interactive_mode())
    elif getattr(args, "update", False) or args.command == "update":
        target = getattr(args, "engine", "all")
        updater = EngineUpdater()
        if target == "all":
            updater.update_all()
        else:
            updater.update_all()
    elif args.command == "download":
        raw_inputs = list(args.urls or [])
        if getattr(args, "file", None):
            raw_inputs.append(args.file)

        opts = {}
        cleaned_inputs = []
        for inp in raw_inputs:
            c_inp, i_opts = extract_inline_options(inp)
            if i_opts:
                opts.update(i_opts)
            if c_inp:
                cleaned_inputs.append(c_inp)

        extracted_urls, source_files = resolve_input_urls(cleaned_inputs)
        if not extracted_urls:
            print(f"{Style.tag('⚠️', 'DOWNLOAD', Style.YELLOW)} No valid URLs found to download.")
            return

        if getattr(args, "cookies_from_browser", None):
            opts["cookies_from_browser"] = args.cookies_from_browser
        if getattr(args, "cookies", None):
            opts["cookies"] = args.cookies
        if getattr(args, "format", None):
            opts["format"] = args.format
        if getattr(args, "ndus", None):
            opts["ndus"] = args.ndus
        if getattr(args, "audio_only", False):
            opts["audio_only"] = True
        if getattr(args, "audio_format", None):
            opts["audio_format"] = args.audio_format
        if getattr(args, "video_only", False):
            opts["video_only"] = True
        if getattr(args, "images_only", False):
            opts["images_only"] = True
        if getattr(args, "resolution", None):
            opts["resolution"] = args.resolution
        if getattr(args, "proxy", None):
            opts["proxy"] = args.proxy
        if getattr(args, "no_sound", False):
            opts["no_sound"] = True
        if getattr(args, "force", False):
            opts["force"] = True
        if getattr(args, "no_archive", False):
            opts["no_archive"] = True
        if getattr(args, "impersonate", None):
            opts["impersonate"] = args.impersonate

        config = ConfigManager()
        if opts.get("no_sound"):
            config._data.setdefault("general", {})["notifications"] = False
            config._data.setdefault("general", {})["play_sound"] = False
        archive = ArchiveManager(config.archive_db_path)

        if len(extracted_urls) == 1 and not source_files and not getattr(args, "concurrency", None):
            run_download(download_url(extracted_urls[0], getattr(args, "backend", None), options=opts))
            return

        if source_files:
            file_names = ", ".join(Path(f).name for f in source_files)
            print(f"\n{Style.tag('📄', 'BATCH FILE', Style.CYAN)} Read {Style.bold(str(len(extracted_urls)))} links from {Style.white(file_names)}")
        else:
            print(f"\n{Style.tag('📦', 'BATCH', Style.MAGENTA)} Ingested {Style.bold(str(len(extracted_urls)))} links for immediate concurrent download...")

        batch_queue = new_batch_queue()
        batch_queue.clear()
        batch_queue.add_batch(extracted_urls, backend=getattr(args, "backend", None), options=opts)

        dispatcher = QueueDispatcher(config=config, queue_manager=batch_queue, archive=archive)
        run_download(dispatcher.run(concurrency=getattr(args, "concurrency", None)))
        return
    elif args.command in ("gui", "ui"):
        launch_desktop_gui(spawn_process=False)
        return
    elif args.command == "config":
        config = ConfigManager()
        subargs = getattr(args, "subargs", [])
        handle_config_command(config, subargs)
        return
    elif args.command == "route":
        config = ConfigManager()
        router = URLRouter.create_default(config=config)
        try:
            b = router.route(args.url)
            print(f"{Style.tag('🎯', 'ROUTING', Style.CYAN)} URL: {Style.white(args.url)}")
            print(f"{Style.tag('⚙️', 'BACKEND', Style.MAGENTA)} Detected Backend: {Style.engine_badge(b.name)}")
        except MultiDLError as e:
            print(f"{Style.tag('❌', 'ROUTING ERROR', Style.RED)} Could not route: {e}")
    elif args.command == "archive":
        config = ConfigManager()
        archive = ArchiveManager(config.archive_db_path)
        if getattr(args, "clear", False):
            confirm = input(f"{Style.tag('⚠️', 'CLEAR ARCHIVE', Style.YELLOW)} Are you sure you want to clear the entire archive? (y/N): ").strip().lower()
            if confirm in ("y", "yes"):
                deleted = archive.clear_archive()
                print(f"{Style.tag('✅', 'ARCHIVE', Style.GREEN)} Cleared {deleted} download records from {config.archive_db_path}")
            else:
                print(f"{Style.tag('ℹ️', 'ARCHIVE', Style.DIM)} Operation cancelled.")
        elif getattr(args, "remove", None):
            target = args.remove
            removed = archive.delete_by_url(target) or archive.delete_by_id(target)
            if removed:
                print(f"{Style.tag('✅', 'REMOVED', Style.GREEN)} Removed record: {Style.white(target)}")
            else:
                print(f"{Style.tag('⚠️', 'NOT FOUND', Style.YELLOW)} No matching entry found for: {target}")
        elif getattr(args, "search", None):
            results = archive.search(args.search)
            if not results:
                print(f"\n{Style.tag('🔍', 'SEARCH', Style.CYAN)} No records found matching: {Style.bold(args.search)}")
            else:
                print(f"\n{Style.tag('🔍', 'SEARCH', Style.CYAN)} Found {Style.cyan(str(len(results)))} matches for '{Style.bold(args.search)}':\n")
                _print_archive_table(results[:args.limit or 20])
        elif getattr(args, "list", False):
            recent = archive.list_recent(limit=args.limit or 20)
            if not recent:
                print(f"\n{Style.tag('📁', 'ARCHIVE', Style.CYAN)} Download archive is currently empty.")
            else:
                print(f"\n{Style.tag('📁', 'ARCHIVE', Style.CYAN)} {Style.bold(f'Recent Downloads (Last {len(recent)})')}:\n")
                _print_archive_table(recent)
        else:
            # Default or --stats: display stats and recent entries
            stats = archive.get_stats()
            print(f"\n{Style.tag('📊', 'STATS', Style.CYAN)} {Style.bold('Download Archive Summary')}")
            print(f"  • Database:        {Style.white(str(config.archive_db_path))}")
            print(f"  • Total Downloads: {Style.green(str(stats['total_count']))}")
            print(f"  • Total Size:      {Style.green(format_bytes(stats['total_bytes']))}")
            print(f"  • By Backend:")
            for b, cnt in stats["by_backend"].items():
                print(f"    - {Style.engine_badge(b)}: {Style.white(str(cnt))}")
            recent = archive.list_recent(limit=5)
            if recent:
                print(f"\n{Style.dim('Recent Entries:')}")
                _print_archive_table(recent)
            else:
                print()
    elif args.command == "proxy":
        config = ConfigManager()
        handle_proxy_command(config, getattr(args, "proxy_args", []) or [])
    elif args.command in ("auth", "login"):
        target = (getattr(args, "engine", "all") or "all").lower()
        config = ConfigManager()

        if target in ("telegram", "telegram-dl", "all"):
            print(f"\n{Style.tag('🔑', 'AUTH', Style.BLUE)} Initializing Telegram authorization...")
            tg_backend = TelegramDlBackend(config.get_backend_config("telegram-dl"))
            try:
                asyncio.run(tg_backend._ensure_client())
                print(f"{Style.tag('✅', 'READY', Style.GREEN)} Telegram account is authorized and ready for downloads.")
            except Exception as e:
                print(f"{Style.tag('❌', 'AUTH ERROR', Style.RED)} {Style.error(str(e))}")

        if target in ("terabox", "terabox-dl", "all"):
            print(f"\n{Style.tag('🔑', 'AUTH', Style.CYAN)} Configuring TeraBox credentials...")
            tb_backend = TeraboxDlBackend(config.get_backend_config("terabox-dl"))
            existing_cookie = tb_backend._resolve_ndus_cookie()
            if existing_cookie:
                masked = existing_cookie[:4] + "..." + existing_cookie[-4:] if len(existing_cookie) > 8 else "***"
                print(f"{Style.tag('✅', 'READY', Style.GREEN)} TeraBox account is already authorized with cookie ({Style.cyan(masked)}).")
                try:
                    ans = input(f"  {Style.dim('Update existing cookie? [y/N]: ')}").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    ans = "n"
                if ans in ("y", "yes"):
                    try:
                        tb_backend._prompt_ndus_cookie()
                        print(f"{Style.tag('✅', 'READY', Style.GREEN)} TeraBox credentials configured.")
                    except Exception as e:
                        print(f"{Style.tag('❌', 'AUTH ERROR', Style.RED)} {Style.error(str(e))}")
            else:
                try:
                    tb_backend._prompt_ndus_cookie()
                    print(f"{Style.tag('✅', 'READY', Style.GREEN)} TeraBox credentials configured.")
                except Exception as e:
                    print(f"{Style.tag('❌', 'AUTH ERROR', Style.RED)} {Style.error(str(e))}")

        if target in ("gallery", "gallery-dl", "all"):
            setup_gallerydl_auth(config)
    elif args.command == "queue":
        q_action = getattr(args, "queue_action", None)
        queue_file = last_batch_file() if getattr(args, "batch", False) else (PROJECT_ROOT / "data" / "queue.json")
        target_queue = QueueManager(queue_file)

        if q_action == "add":
            raw_inputs = list(args.urls or [])
            if getattr(args, "file", None):
                raw_inputs.append(args.file)

            extracted_urls, source_files = resolve_input_urls(raw_inputs)
            if not extracted_urls:
                print(f"{Style.tag('⚠️', 'QUEUE', Style.YELLOW)} No URLs provided or found to add.")
                return

            if source_files:
                file_names = ", ".join(Path(f).name for f in source_files)
                print(f"\n{Style.tag('📄', 'BATCH FILE', Style.CYAN)} Read {Style.bold(str(len(extracted_urls)))} links from {Style.white(file_names)}")

            added = target_queue.add_batch(extracted_urls, priority=args.priority, backend=args.backend)
            q_label = "batch queue" if getattr(args, "batch", False) else "download queue"
            print(f"\n{Style.tag('✅', 'QUEUE', Style.GREEN)} Added {Style.bold(str(len(added)))} item(s) to {q_label}:")
            for it in added[:8]:
                print(f"  • {Style.engine_badge(it.backend or 'auto')} {Style.white(it.url)}")
            if len(added) > 8:
                print(f"  ... and {len(added) - 8} more.")
            print(f"\n{Style.dim('Tip: Run')} {Style.bold('python multi-dl.py queue start')} {Style.dim('to begin downloading.')}\n")

        elif q_action == "list" or q_action is None:
            print_queue_list(target_queue, status_filter=getattr(args, "status", None), is_batch=getattr(args, "batch", False))

        elif q_action == "start":
            config = ConfigManager()
            archive = ArchiveManager(config.archive_db_path)
            stats = target_queue.stats()
            queued_cnt = stats["by_status"].get("queued", 0) + stats["by_status"].get("pending", 0)
            if queued_cnt == 0:
                q_label = "batch queue" if getattr(args, "batch", False) else "download queue"
                print(f"\n{Style.tag('📋', 'QUEUE', Style.CYAN)} No pending items in {q_label} to download.")
                print(f"  {Style.dim('Add items first with:')} {Style.bold('python multi-dl.py queue add <url/file>')}\n")
                return

            dispatcher = QueueDispatcher(config=config, queue_manager=target_queue, archive=archive)
            run_download(dispatcher.run(concurrency=getattr(args, "concurrency", None)))

        elif q_action == "retry":
            count = target_queue.retry_failed()
            q_label = "batch queue" if getattr(args, "batch", False) else "download queue"
            if count > 0:
                print(f"\n{Style.tag('🔄', 'QUEUE RETRY', Style.CYAN)} Re-queued {Style.bold(str(count))} failed item(s) in {q_label} back to QUEUED.\n")
            else:
                print(f"\n{Style.tag('ℹ️', 'QUEUE', Style.CYAN)} No failed items found in {q_label} to retry.\n")

        elif q_action == "clear":
            status_f = getattr(args, "status", None)
            sf = None
            if status_f:
                valid_statuses = [s.value for s in TaskStatus]
                if status_f.lower() in valid_statuses:
                    sf = TaskStatus(status_f.lower())
                else:
                    valid_list = ", ".join(valid_statuses)
                    print(f"\n{Style.tag('❌', 'INVALID STATUS', Style.RED)} Invalid status filter '{status_f}'.")
                    print(f"  {Style.dim(f'Valid statuses are: {valid_list}')}\n")
                    return
            count = target_queue.clear(status_filter=sf)
            filter_text = f" ({status_f})" if status_f else ""
            q_label = "batch queue" if getattr(args, "batch", False) else "download queue"
            print(f"\n{Style.tag('🗑️', 'QUEUE CLEAR', Style.YELLOW)} Removed {Style.bold(str(count))} item(s){filter_text} from {q_label}.\n")

    elif getattr(args, "file", None):
        extracted_urls, source_files = resolve_input_urls([args.file])
        if not extracted_urls:
            print(f"{Style.tag('⚠️', 'DOWNLOAD', Style.YELLOW)} No URLs found in {args.file}")
            return
        file_names = ", ".join(Path(f).name for f in source_files)
        print(f"\n{Style.tag('📄', 'BATCH FILE', Style.CYAN)} Read {Style.bold(str(len(extracted_urls)))} links from {Style.white(file_names)}")

        opts = {}
        if getattr(args, "audio_only", False):
            opts["audio_only"] = True
        if getattr(args, "audio_format", None):
            opts["audio_format"] = args.audio_format
        if getattr(args, "video_only", False):
            opts["video_only"] = True
        if getattr(args, "images_only", False):
            opts["images_only"] = True
        if getattr(args, "resolution", None):
            opts["resolution"] = args.resolution
        if getattr(args, "proxy", None):
            opts["proxy"] = args.proxy
        if getattr(args, "no_sound", False):
            opts["no_sound"] = True

        config = ConfigManager()
        if opts.get("no_sound"):
            config._data.setdefault("general", {})["notifications"] = False
            config._data.setdefault("general", {})["play_sound"] = False
        archive = ArchiveManager(config.archive_db_path)
        batch_queue = new_batch_queue()
        batch_queue.clear()
        batch_queue.add_batch(extracted_urls, options=opts)

        dispatcher = QueueDispatcher(config=config, queue_manager=batch_queue, archive=archive)
        run_download(dispatcher.run(concurrency=getattr(args, "concurrency", None)))
        return
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
        main()
    except KeyboardInterrupt:
        sys.exit(130)
