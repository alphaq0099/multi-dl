"""
Adapter backend wrapping Telegram downloads using Telethon.
Features:
- Reuses authenticated Telethon sessions from local telegram-dl (zero login prompts)
- Supports public channels, private supergroups (-100 IDs), and forum topic links
- Smart album detection: automatically downloads full media albums (grouped_id)
- Full chat history scraping when a chat root URL is passed
- 512KB chunked streaming with 4KB boundary resumable .part files
- In-place dual batch progress bar strictly constrained to terminal width
"""

import asyncio
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse
import uuid

# Silence verbose background MTProto protocol warnings from polluting terminal output
logging.getLogger("telethon").setLevel(logging.ERROR)

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, MessageMediaWebPage

from backends.base import BaseBackend
from core.exceptions import AuthenticationError, DownloadFailedError, EngineNotFoundError
from core.models import ArchiveEntry, DownloadProgress, DownloadTask, MediaType
from core.files import safe_name, contained_path
from core.storage import file_lock, atomic_json_write
from core.exceptions import ConfigError
from core.archive import ArchiveManager
from core.workers import run_blocking
from core.storage import async_file_lock


def exclusive_session(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        _, _, session = self._resolve_credentials()
        async with async_file_lock(Path(session).with_suffix('.transfer')):
            return await method(self, *args, **kwargs)
    return wrapped
from core.terminal import Style, format_bytes

TG_LINK_REGEX = re.compile(
    r'(?:https?://)?(?:t|telegram)\.me/(c/)?([a-zA-Z0-9_.-]+)(?:/(\d+))?(?:/(\d+))?'
)


def _clean_filename(filename: str) -> str:
    """Removes invalid OS filesystem characters."""
    return safe_name(filename, 'unnamed_media')


def _get_media_type(message: Any) -> MediaType:
    """Determines MediaType enum from Telethon message."""
    if not message or not message.media:
        return MediaType.UNKNOWN
    if getattr(message, 'photo', None):
        return MediaType.IMAGE
    if getattr(message, 'video', None) or getattr(message, 'gif', None):
        return MediaType.VIDEO
    if getattr(message, 'voice', None) or getattr(message, 'audio', None):
        return MediaType.AUDIO
    if getattr(message, 'document', None):
        mime = getattr(message.document, 'mime_type', '') or ''
        if mime.startswith('video/'):
            return MediaType.VIDEO
        if mime.startswith('audio/'):
            return MediaType.AUDIO
        if mime.startswith('image/'):
            return MediaType.IMAGE
        return MediaType.DOCUMENT
    return MediaType.UNKNOWN


def _get_file_size(message: Any) -> int:
    """Safely extracts media byte size from message."""
    if not message or not message.media:
        return 0
    if getattr(message, 'file', None) and getattr(message.file, 'size', None):
        return int(message.file.size)
    doc = getattr(message.media, 'document', None)
    if doc and hasattr(doc, 'size'):
        return int(doc.size)
    return 0


def _get_message_filename(message: Any, default_ext: str = ".jpg") -> str:
    """Derives a clean filename from message attributes."""
    if getattr(message, 'file', None) and message.file.name:
        original = Path(_clean_filename(message.file.name))
        message_id = getattr(message, 'id', None)
        if isinstance(message_id, int):
            chat_id = getattr(message, 'chat_id', None)
            identity = f'{chat_id}_{message_id}' if isinstance(chat_id, int) else str(message_id)
            return f'{original.stem} [{identity}]{original.suffix}'
        return original.name
    mtype = _get_media_type(message).value
    ext = getattr(message.file, 'ext', None) or default_ext
    date_str = message.date.strftime("%Y%m%d_%H%M%S") if message.date else "nodate"
    chat_id = getattr(message, 'chat_id', None)
    identity = f'{chat_id}_{message.id}' if isinstance(chat_id, int) else str(message.id)
    return f"{mtype}_{date_str}_{identity}{ext}"


def _resolve_media_object(message: Any) -> Any:
    """Resolves the raw document or photo object to avoid WebPage preview casting issues."""
    if not message or not message.media:
        return message
    if isinstance(message.media, MessageMediaWebPage):
        webpage = getattr(message.media, 'webpage', None)
        if webpage:
            if getattr(webpage, 'document', None):
                return webpage.document
            if getattr(webpage, 'photo', None):
                return webpage.photo
    else:
        if getattr(message.media, 'document', None):
            return message.media.document
        if getattr(message.media, 'photo', None):
            return message.media.photo
    return message


class _TelegramBatchTracker:
    """
    In-place dual progress bar for multi-file Telegram album & chat downloads.
    Line 1 (Top): Batch progress (e.g. 2/5 files • 14.50 MB ( 40.0%))
    Line 2 (Bottom): Active file progress (speed, ETA, sizes, filename)
    Strictly budgets visible width to prevent terminal line-wrapping cascades.
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

        # Line 1: Batch Progress
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

        batch_color = Style.GREEN if (batch_pct is not None and batch_pct >= 100.0) else Style.BLUE
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

        # Line 2: Active File Progress
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


class TelegramDlBackend(BaseBackend):
    """
    Telethon-based Telegram media downloader backend for MULTI_DOWNLOADER.
    Handles:
    - Single message links: https://t.me/channel/123, https://t.me/c/12345/123
    - Forum topic links: https://t.me/channel/45/123
    - Multi-media albums: automatically downloads grouped media
    - Full chat/channel scraping: https://t.me/channel_name or https://t.me/c/12345
    """

    name: str = "telegram-dl"
    supported_domains: List[str] = ["t.me", "telegram.me"]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.client: Optional[TelegramClient] = None
        self._load_modular_config()

    def _load_modular_config(self) -> None:
        """Loads and merges options from configs/telegram-dl.json if present."""
        cfg_path_str = self.config.get("config_file", "configs/telegram-dl.json")
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
        """Returns True if the URL points to a Telegram link."""
        url_lower = url.lower()
        return "t.me/" in url_lower or "telegram.me/" in url_lower

    @staticmethod
    def parse_link(url: str) -> Tuple[Any, Optional[int], Optional[int]]:
        """
        Parses a Telegram link:
        - https://t.me/channel/123 -> ('channel', 123, None)
        - https://t.me/c/123456789/456 -> (-100123456789, 456, None)
        - https://t.me/c/123456789/7844/11632 -> (-100123456789, 11632, 7844)
        - https://t.me/channel_name -> ('channel_name', None, None)
        - https://t.me/c/123456789 -> (-100123456789, None, None)
        """
        match = TG_LINK_REGEX.search(url.strip())
        if not match:
            return None, None, None

        is_private = bool(match.group(1))
        chat_str = match.group(2)

        topic_id: Optional[int] = None
        message_id: Optional[int] = None

        if match.group(4):
            topic_id = int(match.group(3))
            message_id = int(match.group(4))
        elif match.group(3):
            message_id = int(match.group(3))

        resolved_chat: Any = chat_str
        try:
            resolved_num = int(chat_str)
            if is_private and resolved_num > 0:
                resolved_chat = int(f"-100{resolved_num}")
            else:
                resolved_chat = resolved_num
        except ValueError:
            pass

        return resolved_chat, message_id, topic_id

    def _save_modular_config(self) -> None:
        """Save credentials atomically without rewriting an unrelated .env file."""
        project_root = Path(__file__).resolve().parent.parent
        path = Path(self.config.get('config_file', 'configs/telegram-dl.json'))
        if not path.is_absolute():
            path = project_root / path
        with file_lock(path):
            try:
                current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                if not isinstance(current, dict):
                    raise ValueError('Expected a JSON object')
                for key in ('api_id', 'api_hash', 'session_path'):
                    if key in self.config:
                        current[key] = self.config[key]
                atomic_json_write(path, current)
            except (OSError, ValueError, TypeError) as exc:
                raise ConfigError('Could not save telegram credentials; existing configuration was preserved') from exc
        if path.resolve() == (project_root / 'configs' / 'telegram-dl.json').resolve():
            # The application master store has precedence over .env fallbacks.
            from core.config import ConfigManager
            master = ConfigManager()
            for key in ('api_id', 'api_hash'):
                if key in self.config:
                    master.set('telegram_dl', key, self.config[key])

    def _prompt_api_credentials(self) -> Tuple[int, str]:
        """Interactively prompts the user for Telegram API credentials."""
        print(f"\n{Style.tag('🔑', 'TELEGRAM SETUP', Style.CYAN)} {Style.bold('Telegram API credentials required.')}")
        print(f"{Style.dim('   To obtain free credentials:')}")
        print(f"{Style.dim('   1. Log into https://my.telegram.org with your phone number.')}")
        print(f"{Style.dim('   2. Select \"API development tools\" and create an app (e.g. \"MultiDownloader\").')}")
        print(f"{Style.dim('   3. Copy your API ID (numbers) and API Hash (letters/numbers).')}\n")

        api_id: Optional[int] = None
        while api_id is None:
            raw_id = input(f"{Style.cyan('Enter Telegram API ID: ')}").strip()
            if not raw_id:
                raise AuthenticationError("Telegram setup cancelled: Empty API ID.")
            try:
                api_id = int(raw_id)
            except ValueError:
                print(f"{Style.error('Invalid API ID: must be an integer (e.g. 12345678).')}")

        api_hash: str = ""
        while not api_hash:
            api_hash = input(f"{Style.cyan('Enter Telegram API Hash: ')}").strip()
            if not api_hash:
                raise AuthenticationError("Telegram setup cancelled: Empty API Hash.")
            if len(api_hash) < 16:
                print(f"{Style.error('API Hash looks too short (expected 32 hex characters).')}")
                api_hash = ""

        self.config["api_id"] = api_id
        self.config["api_hash"] = api_hash
        self._save_modular_config()
        print(f"\n{Style.tag('💾', 'SAVED', Style.GREEN)} Credentials saved to {Style.path('configs/telegram-dl.json')}\n")
        return api_id, api_hash

    def _resolve_credentials(self) -> Tuple[Optional[int], Optional[str], str]:
        """Resolves api_id, api_hash, and session_path from configs, .env, or user repo."""
        api_id = self.config.get("api_id")
        api_hash = self.config.get("api_hash")

        # Check env files
        custom_env = self.config.get("env_file")
        candidate_envs = [
            Path(custom_env) if custom_env else None,
            Path(__file__).resolve().parent.parent / ".env",
            Path.home() / "telegram-dl" / ".env",
        ]
        for env_path in candidate_envs:
            if env_path and env_path.exists():
                load_dotenv(env_path)
                if not api_id and os.getenv("TELEGRAM_API_ID"):
                    api_id = os.getenv("TELEGRAM_API_ID")
                if not api_hash and os.getenv("TELEGRAM_API_HASH"):
                    api_hash = os.getenv("TELEGRAM_API_HASH")

        parsed_api_id = int(api_id) if api_id else None

        # Resolve session file
        session_path = self.config.get("session_path")
        valid_configured = False
        if session_path:
            valid_configured = True

        if not valid_configured:
            session_path = None
            candidate_sessions = [
                Path(__file__).resolve().parent.parent / "telegram_dl_session",
                Path(__file__).resolve().parent.parent / "configs" / "telegram_dl_session",
                Path.home() / "telegram-dl" / "telegram_dl_session",
            ]
            for s in candidate_sessions:
                if s.with_suffix(".session").exists():
                    session_path = str(s)
                    break

        if not session_path:
            session_path = str(Path(__file__).resolve().parent.parent / "telegram_dl_session")

        return parsed_api_id, api_hash, session_path

    async def _ensure_client(self) -> TelegramClient:
        """Initializes and connects the Telethon client, verifying authorization."""
        if self.client and self.client.is_connected():
            return self.client

        api_id, api_hash, session_path = self._resolve_credentials()
        if not api_id or not api_hash:
            if Style.is_interactive():
                api_id, api_hash = self._prompt_api_credentials()
            else:
                raise AuthenticationError(
                    "Telegram API credentials not found. Please set api_id and api_hash in configs/telegram-dl.json or .env."
                )

        from urllib.parse import urlsplit, unquote
        kwargs = {}
        proxy = self.config.get('proxy')
        if proxy:
            parsed = urlsplit(proxy)
            kind = {'socks5': 'socks5', 'socks5h': 'socks5', 'socks4': 'socks4', 'http': 'http'}.get(parsed.scheme)
            if not kind or not parsed.hostname or not parsed.port:
                raise AuthenticationError('Telegram proxy requires http/socks4/socks5://host:port')
            kwargs['proxy'] = dict(proxy_type=kind, addr=parsed.hostname, port=parsed.port,
                                   rdns=True, username=unquote(parsed.username or '') or None,
                                   password=unquote(parsed.password or '') or None)
        self.client = TelegramClient(session_path, api_id, api_hash, **kwargs)
        await self.client.connect()

        if not await self.client.is_user_authorized():
            if not Style.is_interactive():
                raise AuthenticationError(
                    f"Telegram session '{session_path}' is not authorized. Please log in using telegram-dl or provide a valid session."
                )

            print(f"\n{Style.tag('📱', 'TELEGRAM LOGIN', Style.BLUE)} {Style.bold('First-time account login required.')}")
            print(f"{Style.dim('   Telethon will now prompt for your phone number and verification code.')}")
            print(f"{Style.dim('   (The code will be sent to your official Telegram app or SMS).')}\n")

            await self.client.start()

            if await self.client.is_user_authorized():
                print(f"\n{Style.tag('✅', 'AUTHORIZED', Style.GREEN)} {Style.bold('Telegram session successfully authorized!')}")
                print(f"{Style.dim(f'   Session saved to: {session_path}.session')}\n")
            else:
                raise AuthenticationError("Telegram login authorization was not completed.")

        return self.client

    async def close(self) -> None:
        """Disconnects Telethon client cleanly if connected."""
        if self.client:
            try:
                if self.client.is_connected():
                    await self.client.disconnect()
            except Exception:
                pass
            finally:
                self.client = None

    @exclusive_session
    async def extract_info(self, url: str) -> Dict[str, Any]:
        """Extracts metadata from a Telegram link without downloading."""
        chat_id, message_id, topic_id = self.parse_link(url)
        if chat_id is None:
            raise DownloadFailedError(f"Invalid Telegram URL: {url}")

        try:
            client = await self._ensure_client()
            entity = await client.get_entity(chat_id)
            chat_title = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(entity.id)

            if message_id:
                msg = await client.get_messages(entity, ids=message_id)
                if not msg:
                    raise DownloadFailedError(f"Could not find message {message_id} in {chat_title}")
                return {
                    "chat_id": str(chat_id),
                    "chat_title": chat_title,
                    "message_id": message_id,
                    "topic_id": topic_id,
                    "has_media": bool(msg.media),
                    "media_type": _get_media_type(msg).value,
                    "file_size": _get_file_size(msg),
                    "caption": msg.message or "",
                }
            else:
                return {
                    "chat_id": str(chat_id),
                    "chat_title": chat_title,
                    "is_full_chat": True,
                }
        finally:
            await self.close()

    async def _download_message_stream(
        self,
        client: TelegramClient,
        message: Any,
        target_path: Path,
        progress_cb: Optional[Callable[[int, int, float, Optional[int]], None]] = None,
        force: bool = False,
    ) -> Tuple[bool, Path]:
        """Streams a Telethon message media file in 512KB chunks with 4KB boundary resuming."""
        file_obj = _resolve_media_object(message)
        file_size = _get_file_size(message)

        if target_path.exists() and not force:
            existing_size = target_path.stat().st_size
            if existing_size == file_size and file_size > 0:
                return True, target_path

        temp_path = target_path.with_suffix(target_path.suffix + ".part")
        target_path.parent.mkdir(parents=True, exist_ok=True)

        offset = 0
        if temp_path.exists() and not force and file_size > 1024 * 1024:
            local_sz = temp_path.stat().st_size
            # Telegram requires offset divisible by 4096 bytes
            offset = (local_sz // 4096) * 4096
            if offset >= file_size:
                offset = 0
            if offset < 4096:
                offset = 0

        mode = "r+b" if offset > 0 else "wb"
        chunk_size = int(self.config.get("chunk_size_kb", 512)) * 1024
        if chunk_size <= 0 or chunk_size > 512 * 1024 or chunk_size % 4096:
            raise DownloadFailedError('Telegram chunk_size_kb must be a multiple of 4 between 4 and 512')

        last_tick = time.time()
        last_bytes = offset
        current_speed = 0.0

        with open(temp_path, mode) as f_out:
            if offset > 0:
                f_out.seek(offset)
                f_out.truncate(offset)

            async for chunk in client.iter_download(
                file=file_obj,
                offset=offset,
                request_size=chunk_size,
                file_size=file_size if file_size > 0 else None,
            ):
                f_out.write(chunk)
                offset += len(chunk)
                now = time.time()

                if now - last_tick >= 0.2:
                    delta_t = now - last_tick
                    delta_b = offset - last_bytes
                    current_speed = delta_b / delta_t if delta_t > 0 else 0.0
                    last_tick = now
                    last_bytes = offset

                    rem_bytes = max(0, file_size - offset) if file_size > 0 else 0
                    eta_sec = int(rem_bytes / current_speed) if current_speed > 0 else None

                    if progress_cb:
                        progress_cb(offset, file_size, current_speed, eta_sec)

        # Download complete -> rename .part to final
        if temp_path.exists():
            if file_size > 0 and temp_path.stat().st_size != file_size:
                raise DownloadFailedError(
                    f"Download stream ended prematurely: expected {file_size} bytes, got {temp_path.stat().st_size} bytes."
                )
            os.replace(temp_path, target_path)

        return True, target_path

    @exclusive_session
    async def download(
        self,
        task: DownloadTask,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> ArchiveEntry:
        """Executes Telegram single, album, or full chat download."""
        chat_id, message_id, topic_id = self.parse_link(task.url)
        if chat_id is None:
            raise DownloadFailedError(f"Could not parse valid Telegram chat or message from: {task.url}")

        quiet = bool(task.options.get("quiet") or progress_callback is not None)
        try:
            client = await self._ensure_client()
            entity = await client.get_entity(chat_id)
            chat_title = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(entity.id)
            clean_chat_title = _clean_filename(chat_title)
            entity_id = getattr(entity, 'id', chat_id)
            if isinstance(entity_id, int):
                clean_chat_title += f' [{entity_id}]'

            out_dir = Path(task.output_dir)
            dest_folder = contained_path(out_dir, clean_chat_title) if self.config.get('organize_by_chat', True) else out_dir.resolve()
            dest_folder.mkdir(parents=True, exist_ok=True)

            downloaded_files: List[Path] = []

            audio_only = bool(task.options.get("audio_only") or task.options.get("extract_audio"))
            video_only = bool(task.options.get("video_only"))
            images_only = bool(task.options.get("images_only") or task.options.get("image_only"))

            def _matches_filter(m_obj: Any) -> bool:
                if not m_obj or not m_obj.media:
                    return False
                mtype = _get_media_type(m_obj)
                allowed = self.config.get('media_types', ['photo', 'video', 'document', 'audio'])
                normalized = {'photo': 'image', 'voice': 'audio', 'sticker': 'image'}
                if mtype.value not in {normalized.get(t, t) for t in allowed}:
                    return False
                size = _get_file_size(m_obj)
                minimum = float(self.config.get('min_file_size_mb', 0)) * 1024 * 1024
                maximum = float(self.config.get('max_file_size_mb', 0)) * 1024 * 1024
                if size < minimum or (maximum > 0 and size > maximum):
                    return False
                if audio_only and mtype != MediaType.AUDIO:
                    return False
                if video_only and mtype != MediaType.VIDEO:
                    return False
                if images_only and mtype != MediaType.IMAGE:
                    return False
                return True

            # ── Mode 1: Single Message or Smart Media Album ──
            if message_id is not None:
                msg = await client.get_messages(entity, ids=message_id)
                if not msg:
                    raise DownloadFailedError(f"Could not find message {message_id} in {chat_title}.")
                if not msg.media:
                    raise DownloadFailedError(f"Telegram message {message_id} contains no downloadable media.")

                # Check if this message belongs to a multi-media album
                messages_to_download = [msg]
                if msg.grouped_id and self.config.get("auto_download_albums", True):
                    # Fetch sibling messages sharing the same grouped_id
                    min_id = max(1, message_id - 9)
                    max_id = message_id + 9
                    siblings = await client.get_messages(entity, ids=list(range(min_id, max_id + 1)))
                    album_siblings = [m for m in siblings if m and getattr(m, 'grouped_id', None) == msg.grouped_id and m.media]
                    if len(album_siblings) > 1:
                        messages_to_download = sorted(album_siblings, key=lambda m: m.id)

                messages_to_download = [m for m in messages_to_download if _matches_filter(m)]
                if not messages_to_download:
                    fname = "audio" if audio_only else ("video" if video_only else "images")
                    raise DownloadFailedError(f"No {fname} media found in Telegram message {message_id}.")

                total_items = len(messages_to_download)
                total_bytes = sum(_get_file_size(m) for m in messages_to_download)

                if not quiet:
                    if total_items > 1:
                        print(f"{Style.tag('🎬', 'MEDIA', Style.YELLOW)} {Style.white(f'{chat_title} (Album of {total_items} items)')}")
                        print(f"{Style.tag('📦', 'BATCH', Style.MAGENTA)} Detected {Style.cyan(str(total_items))} items in album ({format_bytes(total_bytes)})")
                        tracker = _TelegramBatchTracker(total_files=total_items, total_batch_bytes=total_bytes)
                    else:
                        media_name = _get_message_filename(msg)
                        print(f"{Style.tag('🎬', 'MEDIA', Style.YELLOW)} {Style.white(f'{chat_title} / {media_name}')}")
                        tracker = None
                else:
                    tracker = None

                for idx, m in enumerate(messages_to_download, 1):
                    filename = _get_message_filename(m)
                    target_file = contained_path(dest_folder, filename)
                    f_size = _get_file_size(m)

                    def _prog_hook(received: int, total: int, speed: float, eta: Optional[int]):
                        if tracker:
                            tracker.update_file(filename, received, total, speed, eta)
                        if progress_callback:
                            prog = DownloadProgress(
                                downloaded_bytes=received,
                                total_bytes=total,
                                speed_bytes_sec=speed,
                                eta_seconds=eta,
                                current_file=clean_chat_title if total_items > 1 else filename,
                                file_index=idx,
                                total_files=total_items,
                                status_message=filename,
                                extractor="telegram",
                                container_title=chat_title,
                            )
                            prog.update_percent()
                            progress_callback(prog)

                    ok, final_p = await self._download_message_stream(client, m, target_file, progress_cb=_prog_hook, force=bool(task.options.get('force')))
                    if ok:
                        downloaded_files.append(final_p)
                        if tracker:
                            tracker.finish_file(filename, f_size)

                if tracker:
                    tracker.finish()

            # ── Mode 2: Full Channel / Chat History Scraping ──
            else:
                print(f"{Style.tag('🎬', 'MEDIA', Style.YELLOW)} {Style.white(f'Scanning chat media in {chat_title}...')}")

                # Collect all media messages from chat history
                media_messages: List[Any] = []
                async for m in client.iter_messages(entity):
                    if m and m.media:
                        if not _matches_filter(m):
                            continue
                        media_messages.append(m)

                if not media_messages:
                    raise DownloadFailedError(f"No downloadable media found in chat '{chat_title}'.")

                total_items = len(media_messages)
                total_bytes = sum(_get_file_size(m) for m in media_messages)
                print(f"{Style.tag('📦', 'BATCH', Style.MAGENTA)} Found {Style.cyan(str(total_items))} media files ({format_bytes(total_bytes)})\n")

                tracker = _TelegramBatchTracker(total_files=total_items, total_batch_bytes=total_bytes)

                for idx, m in enumerate(media_messages, 1):
                    filename = _get_message_filename(m)
                    target_file = contained_path(dest_folder, filename)
                    f_size = _get_file_size(m)

                    def _prog_hook(received: int, total: int, speed: float, eta: Optional[int]):
                        if tracker:
                            tracker.update_file(filename, received, total, speed, eta)
                        if progress_callback:
                            prog = DownloadProgress(
                                downloaded_bytes=received,
                                total_bytes=total,
                                speed_bytes_sec=speed,
                                eta_seconds=eta,
                                current_file=clean_chat_title if total_items > 1 else filename,
                                file_index=idx,
                                total_files=total_items,
                                status_message=filename,
                                extractor="telegram",
                                container_title=chat_title,
                            )
                            prog.update_percent()
                            progress_callback(prog)

                    ok, final_p = await self._download_message_stream(client, m, target_file, progress_cb=_prog_hook, force=bool(task.options.get('force')))
                    if ok:
                        downloaded_files.append(final_p)
                        tracker.finish_file(filename, f_size)

                tracker.finish()

            if not downloaded_files:
                raise DownloadFailedError("No files were successfully downloaded from Telegram.")

            primary_file = downloaded_files[0]
            final_filename = primary_file.name if len(downloaded_files) == 1 else clean_chat_title
            final_path = primary_file if len(downloaded_files) == 1 else dest_folder
            total_size = sum(p.stat().st_size for p in downloaded_files if p.exists())
            media_type = _get_media_type(messages_to_download[0]) if message_id else MediaType.UNKNOWN

            file_hash = await run_blocking(ArchiveManager.calculate_file_hash, final_path)

            return ArchiveEntry(
                id=str(uuid.uuid4()),
                url=task.url,
                file_path=str(final_path.resolve()),
                file_name=final_filename,
                file_hash=file_hash,
                file_size=total_size,
                backend=self.name,
                source_site="telegram",
                media_type=media_type,
                metadata={
                    "chat_id": str(chat_id),
                    "chat_title": chat_title,
                    "message_id": message_id,
                    "topic_id": topic_id,
                    "total_downloaded": len(downloaded_files),
                },
            )
        finally:
            await self.close()
