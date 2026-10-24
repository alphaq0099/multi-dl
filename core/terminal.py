"""
Terminal formatting and color utilities for MULTI_DOWNLOADER.
Provides unified, consistent ANSI styling and aligned status tags across all CLI outputs.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import unicodedata
from core.progress import infer_phase, plain_text

# Enable ANSI virtual terminal processing and ensure UTF-8 encoding on Windows cmd/powershell
if sys.platform == "win32":
    os.system("")
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def safe_terminal_write(text: str) -> None:
    """Safely writes text to sys.stdout, preventing UnicodeEncodeError crashes on non-UTF-8 terminals."""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        clean = text.encode(enc, errors="replace").decode(enc, errors="replace")
        try:
            sys.stdout.write(clean)
        except Exception:
            pass
    except Exception:
        pass


def char_width(ch: str) -> int:
    """Returns terminal column width (2 for wide/fullwidth CJK and emojis, 1 otherwise)."""
    if unicodedata.combining(ch) or ch in ('\u200d', '\ufe0e', '\ufe0f'):
        return 0
    if ch in "🖼📥📦🎬📁🚀⚠️❌✅⏩":
        return 2
    if ch == "•":
        return 1
    code = ord(ch)
    if code > 0x1F000:
        return 2
    w = unicodedata.east_asian_width(ch)
    return 2 if w in ("W", "F") else 1


def str_width(s: str) -> int:
    """Returns clean column length ignoring ANSI escapes and accounting for wide characters."""
    clean = re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', s)
    return sum(char_width(ch) for ch in clean)


def truncate_to_width(s: str, max_cols: int) -> str:
    """Clip text by display columns while preserving complete ANSI tokens."""
    max_cols = max(0, max_cols)
    if str_width(s) <= max_cols:
        return s
    if max_cols <= 3:
        return '.' * max_cols
    used, result = 0, []
    ansi = False
    for token in re.findall(r'\x1b\[[0-?]*[ -/]*[@-~]|[^\x1b]', s):
        if token.startswith('\x1b['):
            result.append(token)
            ansi = True
            continue
        width = char_width(token)
        if used + width > max_cols - 3:
            break
        result.append(token)
        used += width
    return ''.join(result) + '...' + ('\033[0m' if ansi else '')


def format_bytes(size: Optional[float]) -> str:
    """Formats bytes into human-readable string (B, KB, MB, GB)."""
    if size is None:
        return "??"
    s = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if s < 1024.0:
            return f"{s:.2f} {unit}"
        s /= 1024.0
    return f"{s:.2f} PB"


def format_dual_bytes(downloaded: int, total: Optional[int]) -> str:
    """
    Formats downloaded and total bytes as a concise single-unit pair.
    Removes redundant unit from downloaded when units match and keeps no spaces around '/':
    Examples:
      26.0/52.0 MB
      100.0/500.0 KB
      1.2/2.4 GB
      15.0 MB/?? (when total is unknown)
    """
    if total is None or total <= 0:
        if downloaded <= 0:
            return "0.0 B/??"
        d_val = float(downloaded)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if d_val < 1024.0 or unit == "TB":
                return f"{d_val:.1f} {unit}/??"
            d_val /= 1024.0
        return f"{d_val:.1f} PB/??"

    t_val = float(total)
    d_val = float(downloaded)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if t_val < 1024.0 or unit == "TB":
            return f"{d_val:.1f}/{t_val:.1f} {unit}"
        t_val /= 1024.0
        d_val /= 1024.0
    return f"{d_val:.1f}/{t_val:.1f} PB"


def render_line_bar(percent: Optional[float], width: int = 14, color: str = "\033[96m") -> str:
    """
    Renders sleek line progress bar matching terabox-dl and Drawing-1.sketchpad.png:
    E.g. '[━━━━━━━───────]'
    """
    cyan = "\033[96m"
    gray = "\033[90m"
    green = "\033[92m"
    reset = "\033[0m"

    if percent is None or percent < 0:
        return f"{cyan}[{gray}{'─' * width}{cyan}]{reset}"

    clamped = min(max(percent, 0.0), 100.0)
    filled_len = int(round((clamped / 100.0) * width))
    empty_len = width - filled_len

    if clamped >= 100.0:
        return f"{green}[{green}{'━' * width}{green}]{reset}"

    filled_part = f"{color}{'━' * filled_len}{reset}"
    empty_part = f"{gray}{'─' * empty_len}{reset}"
    return f"{cyan}[{filled_part}{empty_part}{cyan}]{reset}"


class Style:
    """ANSI color and styling helper for CLI rendering."""

    format_bytes = staticmethod(format_bytes)

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"

    # Bright foreground palette
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"

    ENGINE_COLORS = {
        "yt-dlp": "\033[91m",       # YouTube Red
        "gallery-dl": "\033[93m",    # Gallery Amber / Yellow
        "telegram-dl": "\033[94m",   # Telegram Blue
        "terabox-dl": "\033[96m",    # TeraBox Cyan
        "cyberdrop-dl": "\033[95m",  # Cyberdrop Magenta
    }

    @classmethod
    def engine_color(cls, engine_name: str) -> str:
        """Returns the distinct ANSI color code for a download engine."""
        return cls.ENGINE_COLORS.get(engine_name.lower(), cls.CYAN)

    @classmethod
    def engine_badge(cls, engine_name: str) -> str:
        """Formats an engine name in its signature color, e.g. [yt-dlp] in red."""
        color = cls.engine_color(engine_name)
        return f"{color}{cls.BOLD}[{engine_name}]{cls.RESET}"

    @classmethod
    def progress_bar(cls, percent: Optional[float], width: int = 20, color: Optional[str] = None) -> str:
        """
        Renders a sleek Unicode line progress bar of given width.
        E.g. '[━━━━━━━───────]'
        Delegates to render_line_bar for unified sleek progress styling across all engines.
        """
        return render_line_bar(percent, width=width, color=color or cls.CYAN)

    @classmethod
    def tag(cls, emoji: str, text: str, color: str = CYAN) -> str:
        """
        Formats a standard status tag with strict single-space alignment:
        Example: '🎯 [ROUTING]' or '⚙️ [BACKEND]'
        Always strips accidental whitespace from emoji to enforce exact alignment.
        """
        clean_emoji = emoji.strip()
        return f"{clean_emoji} {color}{cls.BOLD}[{text}]{cls.RESET}"

    @classmethod
    def highlight(cls, text: str, color: str = WHITE) -> str:
        return f"{color}{text}{cls.RESET}"

    @classmethod
    def bold(cls, text: str) -> str:
        return f"{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def dim(cls, text: str) -> str:
        return f"{cls.DIM}{text}{cls.RESET}"

    @classmethod
    def muted(cls, text: str) -> str:
        return f"{cls.GRAY}{text}{cls.RESET}"

    @classmethod
    def cyan(cls, text: str) -> str:
        return f"{cls.CYAN}{text}{cls.RESET}"

    @classmethod
    def magenta(cls, text: str) -> str:
        return f"{cls.MAGENTA}{text}{cls.RESET}"

    @classmethod
    def green(cls, text: str) -> str:
        return f"{cls.GREEN}{text}{cls.RESET}"

    @classmethod
    def yellow(cls, text: str) -> str:
        return f"{cls.YELLOW}{text}{cls.RESET}"

    @classmethod
    def red(cls, text: str) -> str:
        return f"{cls.RED}{text}{cls.RESET}"

    @classmethod
    def blue(cls, text: str) -> str:
        return f"{cls.BLUE}{text}{cls.RESET}"

    @classmethod
    def white(cls, text: str) -> str:
        return f"{cls.WHITE}{text}{cls.RESET}"

    @classmethod
    def speed(cls, text: str) -> str:
        return f"{cls.GREEN}{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def path(cls, text: str) -> str:
        return f"{cls.WHITE}{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def error(cls, text: str) -> str:
        return f"{cls.RED}{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def warning(cls, text: str) -> str:
        return f"{cls.YELLOW}{text}{cls.RESET}"

    @classmethod
    def success(cls, text: str) -> str:
        return f"{cls.GREEN}{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def is_interactive(cls) -> bool:
        """Checks if standard input is attached to an interactive terminal."""
        try:
            return sys.stdin is not None and sys.stdin.isatty()
        except Exception:
            return False


@dataclass
class MultiBarSlot:
    """Represents an active visual slot in the download canvas."""
    slot_id: int
    item_id: Optional[str] = None
    backend: str = "idle"
    extractor: Optional[str] = None
    container_title: Optional[str] = None
    url_or_title: str = ""
    percent: Optional[float] = None
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed_bytes_sec: Optional[float] = None
    file_index: Optional[int] = None
    total_files: Optional[int] = None
    status: str = "idle"  # idle, active, completed, failed, skipped
    status_message: Optional[str] = None  # Live phase or verbose event
    phase: str = 'waiting'
    file_name: Optional[str] = None
    file_downloaded_bytes: Optional[int] = None
    file_total_bytes: Optional[int] = None
    completed_files: Optional[int] = None


class MultiBarManager:
    """
    Thread-safe progress slots with a fixed two-row layout in every transfer stage.
    """

    def __init__(self, max_slots: int = 3, is_tty: Optional[bool] = None):
        self.max_slots = max(1, max_slots)
        self.is_tty = is_tty if is_tty is not None else (sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else True)
        self._lock = threading.Lock()
        self._slots: Dict[int, MultiBarSlot] = {
            i: MultiBarSlot(slot_id=i) for i in range(1, self.max_slots + 1)
        }
        self._remaining: int = 0
        self._completed: int = 0
        self._failed: int = 0
        self._skipped: int = 0
        self._pending_items: List[Tuple[str, str]] = []
        self._lines_printed: int = 0
        self._last_render_time: float = 0.0
        self._throttle_interval: float = 0.05
        self._last_frame = []

    def set_pending_items(self, pending_items: List[Tuple[str, str]]) -> None:
        """Updates the upcoming pending queue items displayed in the canvas."""
        with self._lock:
            self._pending_items = list(pending_items)
        self.render(force=True)

    @staticmethod
    def _format_engine_badge(engine_name: str) -> str:
        """Formats engine badge with engine color for backward compatibility."""
        return Style.engine_badge(engine_name)

    @staticmethod
    def _visible_len(s: str) -> int:
        """Returns clean column length ignoring ANSI escapes and accounting for wide characters."""
        return str_width(s)

    def set_queue_counts(self, remaining: int, completed: int, failed: int, skipped: int = 0) -> None:
        """Updates overall queue counter values displayed in footer."""
        with self._lock:
            self._remaining = max(0, remaining)
            self._completed = max(0, completed)
            self._failed = max(0, failed)
            self._skipped = max(0, skipped)
            should_render = (
                self._lines_printed > 0
                or any(s.status == "active" for s in self._slots.values())
                or self._remaining > 0
                or bool(self._pending_items)
            )
        if should_render:
            self.render(force=True)

    def assign_slot(self, item_id: str, backend: str, url_or_title: str,
                    extractor: Optional[str] = None, container_title: Optional[str] = None) -> int:
        """Claim a display slot and retire its pending row in the same frame."""
        if not extractor:
            from core.router import URLRouter
            extractor = URLRouter.get_host_identifier(url_or_title, backend)
        with self._lock:
            sid = next((sid for sid, slot in self._slots.items() if slot.status != 'active'), None)
            if sid is None:
                raise RuntimeError('No free transfer display slot')
            self._slots[sid] = MultiBarSlot(slot_id=sid, item_id=item_id, backend=backend,
                                           extractor=extractor, container_title=container_title,
                                           url_or_title=url_or_title, status='active', phase='waiting',
                                           status_message='Preparing download')
            for index, pending in enumerate(self._pending_items):
                if pending[0] == backend and pending[1] == url_or_title:
                    del self._pending_items[index]
                    break
        self.render(force=True)
        return sid

    def update_slot(
        self,
        slot_id: int,
        percent: Optional[float] = None,
        downloaded: Optional[int] = None,
        total: Optional[int] = None,
        speed: Optional[float] = None,
        title: Optional[str] = None,
        file_index: Optional[int] = None,
        total_files: Optional[int] = None,
        status_message: Optional[str] = None,
        extractor: Optional[str] = None,
        container_title: Optional[str] = None,
        phase: Optional[str] = None,
        file_name: Optional[str] = None,
        file_downloaded_bytes: Optional[int] = None,
        file_total_bytes: Optional[int] = None,
        completed_files: Optional[int] = None,
    ) -> None:
        """Updates live metrics for a specific worker slot."""
        with self._lock:
            if slot_id in self._slots:
                slot = self._slots[slot_id]
                if percent is not None:
                    slot.percent = percent
                if downloaded is not None:
                    slot.downloaded_bytes = downloaded
                if total is not None:
                    slot.total_bytes = total
                if speed is not None:
                    slot.speed_bytes_sec = speed
                if title:
                    slot.url_or_title = title
                if file_index is not None:
                    slot.file_index = file_index
                if total_files is not None:
                    slot.total_files = total_files
                if status_message is not None:
                    slot.status_message = status_message
                if extractor:
                    slot.extractor = extractor
                if container_title:
                    slot.container_title = container_title
                slot.phase = phase or infer_phase(status_message, downloaded, speed, slot.phase)
                if slot.phase != 'downloading':
                    slot.speed_bytes_sec = None
                if file_name is not None:
                    if file_name != slot.file_name and file_downloaded_bytes == 0:
                        slot.speed_bytes_sec = None
                    slot.file_name = file_name
                if file_downloaded_bytes is not None:
                    slot.file_downloaded_bytes = file_downloaded_bytes
                    slot.file_total_bytes = file_total_bytes
                if completed_files is not None:
                    slot.completed_files = completed_files
                slot.status = "active"
        self.render()

    def finish_slot(
        self,
        slot_id: int,
        display_path: str = "",
        status_type: str = "completed",
        error_msg: Optional[str] = None,
    ) -> None:
        """
        Marks a slot idle upon completion or failure without disrupting active canvas.
        """
        with self._lock:
            if slot_id in self._slots:
                slot = self._slots[slot_id]
                slot.status = "idle"
                slot.backend = "idle"
                slot.extractor = None
                slot.container_title = None
                slot.url_or_title = ""
                slot.percent = None
                slot.downloaded_bytes = 0
                slot.total_bytes = None
                slot.speed_bytes_sec = None
                slot.file_index = None
                slot.total_files = None
                slot.status_message = None
        # Always render immediately so slot retirement is reflected without freeze
        self.render(force=True)

    def print_log(self, text: str) -> None:
        """Atomically prints a log message above the active canvas without visual distortion."""
        with self._lock:
            if self.is_tty and self._lines_printed > 0:
                if self._lines_printed > 1:
                    safe_terminal_write(f"\033[{self._lines_printed - 1}A\r\033[J")
                else:
                    safe_terminal_write("\r\033[J")
                self._lines_printed = 0
            safe_terminal_write(f"{text}\n")
            try:
                sys.stdout.flush()
            except Exception:
                pass
        self.render(force=True)

    def render(self, force: bool = False) -> None:
        with self._lock:
            self._render_locked(force)

    def _render_locked(self, force: bool = False) -> None:
        """Atomically renders header, unified 2-line cards (Drawing-1.sketchpad.png), and footer."""
        now = time.time()
        if not force and (now - self._last_render_time) < self._throttle_interval:
            return
        self._last_render_time = now

        term_size = shutil.get_terminal_size((80, 24))
        term_width = term_size.columns
        term_height = term_size.lines
        card_width = max(4, min(term_width - 2, 96))
        inner_w = card_width - 4
        bar_w = 12 if card_width < 80 else 14

        bullet = f"{Style.GRAY}•{Style.RESET}"
        sep = f" {bullet} "

        active_slots = [s for s in self._slots.values() if s.status == "active"]
        active_count = len(active_slots)

        # Every visible slot keeps two content rows in all transfer stages.
        # Leave room for the header, footer, and a bounded pending section.
        compact_spacing = term_height < 30
        card_overhead = (4 if compact_spacing else 5) + (2 if self._pending_items else 0)
        max_cards_fit = max(0, (term_height - card_overhead) // (4 if compact_spacing else 5))
        slots_to_print = active_slots[:max_cards_fit]

        if not active_slots and not self._pending_items and self._lines_printed == 0:
            return

        # 1. Header Card (matching visual theme)
        if self.max_slots == 1:
            header_str = (
                f"\033[48;5;236m\033[97m\033[1m MULTI_DOWNLOADER \033[0m"
                f"\033[48;5;236m\033[90m─\033[0m"
                f"\033[48;5;236m\033[97m Active Download \033[96m(1 Worker) \033[0m"
            )
        else:
            header_str = (
                f"\033[48;5;236m\033[97m\033[1m MULTI_DOWNLOADER \033[0m"
                f"\033[48;5;236m\033[90m─\033[0m"
                f"\033[48;5;236m\033[97m Active Downloads \033[96m({active_count}/{self.max_slots} Workers) \033[0m"
            )

        # Adapt spacing based on terminal height:
        if compact_spacing:
            lines_to_print = [header_str]
        else:
            lines_to_print = [header_str, ""]

        border_c = Style.dim(Style.CYAN)
        top_card = f"{border_c}╭{'─' * (card_width - 2)}╮{Style.RESET}"
        bot_card = f"{border_c}╰{'─' * (card_width - 2)}╯{Style.RESET}"
        side_l = f"{border_c}│{Style.RESET} "
        side_r = f" {border_c}│{Style.RESET}"

        # 2. Worker Slots (framed cards matching ddd.png)
        for idx, slot in enumerate(slots_to_print):
            if slot.status != "active":
                eng_badge = Style.engine_badge("idle")
                content1 = f"{eng_badge}{sep}{Style.dim('connecting...')}"
            else:
                eng_badge = Style.engine_badge(slot.backend)
                # Resolve extractor name (e.g. erome, youtube, telegram, terabox, mega, bunkr)
                raw_extractor = slot.extractor
                if not raw_extractor and slot.url_or_title:
                    try:
                        from core.router import URLRouter
                        raw_extractor = URLRouter.get_host_identifier(slot.url_or_title, slot.backend)
                    except Exception:
                        raw_extractor = None
                if not raw_extractor:
                    raw_extractor = slot.backend.replace("-dl", "")
                raw_extractor = plain_text(raw_extractor)

                ext_badge = f"{Style.YELLOW}[{raw_extractor}]{Style.RESET}"

                # Resolve album/group/folder/chat/channel name, falling back to file name / title
                cand_title = ""
                if slot.container_title:
                    cand_title = plain_text(slot.container_title).strip()
                elif slot.url_or_title and not slot.url_or_title.startswith(("http://", "https://", "t.me/")):
                    cand_title = plain_text(slot.url_or_title).strip()
                elif slot.status_message and not any(slot.status_message.lower().startswith(x) for x in ("connecting", "downloading", "finalizing", "preparing")):
                    cand_title = plain_text(slot.status_message).strip()
                elif slot.url_or_title:
                    clean_path = Path(urlparse(slot.url_or_title).path).name
                    cand_title = plain_text(clean_path or slot.url_or_title)

                if not cand_title:
                    cand_title = "downloading..."

                # Wrap in brackets [album/file name] matching ddd.png
                if not (cand_title.startswith("[") and cand_title.endswith("]")):
                    bracketed_title = f"[{cand_title}]"
                else:
                    bracketed_title = cand_title

                line1_lead = f"{eng_badge}{sep}{ext_badge}{sep}"
                lead_len = str_width(line1_lead)
                avail_title = max(0, inner_w - lead_len)

                if avail_title > 6:
                    trunc_title = truncate_to_width(bracketed_title, avail_title)
                    content1 = f"{line1_lead}{Style.white(trunc_title)}"
                else:
                    content1 = line1_lead.rstrip()

            c1_w = str_width(content1)
            pad1 = " " * max(0, inner_w - c1_w)
            line1 = f"{side_l}{content1}{pad1}{side_r}"

            # Line 2: [━━━━━━━━━━━━━━] 50.0% • 14/28 • 26.0/52.0 MB • @ 4.2 MB/s • [verbose / current file]
            eng_color = Style.engine_color(slot.backend) if slot.status == "active" else Style.CYAN
            percent = slot.percent
            if slot.completed_files is not None and slot.total_files:
                percent = 100 * slot.completed_files / slot.total_files
            bar = render_line_bar(percent, width=bar_w, color=eng_color)

            if slot.status == "active" and percent is not None:
                pct_str = f"{percent:.1f}%"
                pct_color = Style.GREEN if percent >= 100.0 else Style.CYAN
                pct_colored = f"{pct_color}{pct_str}{Style.RESET}"
            else:
                pct_colored = f"{Style.dim('--%')}"

            if slot.status == "active":
                if slot.total_files and slot.total_files > 1:
                    cur_f = slot.completed_files if slot.completed_files is not None else (slot.file_index or 0)
                    tot_f = slot.total_files
                    files_str = f"{cur_f}/{tot_f}"
                else:
                    files_str = "1/1" if slot.phase in ("downloading", "processing") else "--/--"
                received = slot.file_downloaded_bytes if slot.file_downloaded_bytes is not None else slot.downloaded_bytes
                total = slot.file_total_bytes if slot.file_downloaded_bytes is not None else slot.total_bytes
                bytes_str = format_dual_bytes(received, total) if total else format_bytes(received)
                if slot.speed_bytes_sec:
                    sp_b = format_bytes(int(slot.speed_bytes_sec)) + "/s"
                    sp_val = re.sub(r'(\.[0-9])0\s', r'\1 ', sp_b)
                    speed_str = f"{Style.dim('@')} {Style.speed(sp_val)}"
                else:
                    speed_str = f"{Style.dim('@ --/s')}"

                if slot.phase == 'downloading' and slot.file_name:
                    v_msg = plain_text(slot.file_name)
                elif slot.status_message:
                    v_msg = plain_text(slot.status_message)
                elif slot.file_index and slot.total_files and slot.total_files > 1 and slot.url_or_title:
                    v_msg = plain_text(Path(slot.url_or_title).name)
                elif slot.url_or_title:
                    v_msg = plain_text(Path(slot.url_or_title).name)
                else:
                    v_msg = "downloading..."
                verbose_raw = f"[{v_msg}]"
            else:
                files_str = "--/--"
                bytes_str = "--/-- MB"
                speed_str = f"{Style.dim('@ --/s')}"
                verbose_raw = "[connecting...]"

            base_line2 = (
                f"{bar} {pct_colored}{sep}"
                f"{Style.white(files_str)}{sep}"
                f"{Style.white(bytes_str)}{sep}"
                f"{speed_str}{sep}"
            )
            base2_len = str_width(base_line2)
            avail_v = max(0, inner_w - base2_len)

            if avail_v > 6:
                trunc_v = truncate_to_width(verbose_raw, avail_v)
                content2 = f"{base_line2}{Style.dim(Style.cyan(trunc_v))}"
            else:
                content2 = base_line2.rstrip()

            c2_w = str_width(content2)
            pad2 = " " * max(0, inner_w - c2_w)
            line2 = f"{side_l}{content2}{pad2}{side_r}"

            lines_to_print.append(top_card)
            lines_to_print.append(line1)
            lines_to_print.append(line2)
            lines_to_print.append(bot_card)

            if idx < len(slots_to_print) - 1 and not compact_spacing:
                lines_to_print.append("")

        # 3. Upcoming Pending Queue (bounded display: up to max_slots lines + 1 overflow line)
        if self._pending_items:
            if not compact_spacing:
                lines_to_print.append("")
            lines_to_print.append(f"{Style.BOLD}Pending:{Style.RESET}")

            # Dynamically calculate room for pending to never overflow terminal height
            curr_lines = len(lines_to_print)
            room = max(1, term_height - curr_lines - 3)
            max_display = min(self.max_slots, room)
            visible = self._pending_items[:max_display]
            overflow = len(self._pending_items) - len(visible)

            for entry in visible:
                p_eng, p_url = entry[:2]
                p_url = plain_text(p_url)
                p_badge = Style.engine_badge(p_eng)
                prefix = f"  {Style.CYAN}↳{Style.RESET} {p_badge} "
                pref_w = str_width(prefix)
                avail_url_w = max(10, inner_w - pref_w)
                trunc_url = truncate_to_width(p_url, avail_url_w)
                lines_to_print.append(f"{prefix}{Style.white(trunc_url)}")

            if overflow > 0:
                plural = "s are" if overflow > 1 else " is"
                lines_to_print.append(f"  {Style.dim(f'[+{overflow} link{plural} pending]')}")

        # 4. Spacing & Footer Line
        if self.max_slots > 1 or self._remaining > 0 or self._completed > 1 or self._pending_items:
            if not compact_spacing:
                lines_to_print.append("")
            failed_color = Style.RED if self._failed > 0 else Style.DIM
            footer_line = (
                f"{Style.BOLD}Queue:{Style.RESET} "
                f"{Style.CYAN}{self._remaining} remaining{Style.RESET} {bullet} "
                f"{Style.GREEN}{self._completed} completed{Style.RESET} {bullet} "
                f"{failed_color}{self._failed} failed{Style.RESET}"
                + (f" {bullet} {self._skipped} skipped" if self._skipped else "")
            )
            lines_to_print.append(footer_line)

        # Hard safety cap: Ensure lines_to_print never exceeds term_height - 2 to prevent buffer scroll
        max_canvas_lines = max(1, term_height - 2)
        if len(lines_to_print) > max_canvas_lines:
            lines_to_print = lines_to_print[:max_canvas_lines - 1] + [lines_to_print[-1]]

        # Clamp line width so no line reaches term_width and triggers console auto-wrap
        max_col_allowed = max(1, term_width - 1)
        cleaned_lines = []
        for line in lines_to_print:
            if str_width(line) > max_col_allowed:
                cleaned_lines.append(truncate_to_width(line, max_col_allowed))
            else:
                cleaned_lines.append(line)
        lines_to_print = cleaned_lines
        self._last_frame = list(lines_to_print)
        if not self.is_tty:
            return

        # Atomic terminal refresh without scrolling drift: jump from last printed line back to top
        if self._lines_printed > 1:
            safe_terminal_write(f"\033[{self._lines_printed - 1}A\r")
        elif self._lines_printed == 1:
            safe_terminal_write("\r")

        printed_count = 0
        try:
            total_l = len(lines_to_print)
            for idx, line in enumerate(lines_to_print):
                if idx < total_l - 1:
                    safe_terminal_write(f"\r\033[K{line}\n")
                else:
                    # Last line MUST NOT emit trailing \n so terminal buffer never scrolls at the bottom
                    safe_terminal_write(f"\r\033[K{line}")
                printed_count += 1

            # If canvas shrank, clear any extra trailing lines from the previous frame using \033[J (no scrolling)
            if self._lines_printed > printed_count:
                safe_terminal_write("\033[J")

            try:
                sys.stdout.flush()
            except Exception:
                pass
        finally:
            self._lines_printed = printed_count

    def close(self) -> None:
        """Cleans up visual canvas at the end of the batch run."""
        with self._lock:
            if self.is_tty and self._lines_printed > 0:
                if self._lines_printed > 1:
                    safe_terminal_write(f"\033[{self._lines_printed - 1}A\r\033[J")
                else:
                    safe_terminal_write("\r\033[J")
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                self._lines_printed = 0


def print_batch_summary(
    total_items: int,
    succeeded_count: int,
    skipped_count: int,
    failed_count: int,
    total_bytes: int,
    elapsed_seconds: float,
    failed_items: Optional[List[Tuple[str, str]]] = None,
    saved_path: Optional[str] = None,
    saved_paths: Optional[List[str]] = None,
) -> None:
    """Renders a polished, high-visibility summary card after completing downloads (single or batch)."""
    term_width = shutil.get_terminal_size((80, 24)).columns
    box_width = min(66, max(52, term_width - 4))

    # Calculate readable elapsed time
    mins, secs = divmod(int(elapsed_seconds), 60)
    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"

    # Average transfer speed
    if elapsed_seconds > 0 and total_bytes > 0:
        avg_speed_sec = total_bytes / elapsed_seconds
        speed_str = f" (avg {format_bytes(avg_speed_sec)}/s)"
    else:
        speed_str = ""

    def _box_line(label: str, value: str = "") -> str:
        col1_w = 21
        label_vis = str_width(label)
        label_pad = max(1, col1_w - label_vis)
        actual_label_w = label_vis + label_pad
        max_val_w = max(10, box_width - 4 - actual_label_w)
        if str_width(value) > max_val_w:
            value = truncate_to_width(value, max_val_w)
        val_vis = str_width(value)
        rem_pad = max(0, box_width - 4 - actual_label_w - val_vis)
        return f"{Style.CYAN}║ {label}{' ' * label_pad}{value}{' ' * rem_pad} ║{Style.RESET}"

    bar = "═" * (box_width - 2)
    div = "─" * (box_width - 2)

    title_text = f"{Style.BOLD}{Style.WHITE}DOWNLOAD SUMMARY{Style.RESET}" if total_items == 1 else f"{Style.BOLD}{Style.WHITE}BATCH DOWNLOAD SUMMARY{Style.RESET}"
    clean_title = re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', title_text)
    pad_title = max(0, box_width - 2 - len(clean_title))
    pl = pad_title // 2
    pr = pad_title - pl

    print(f"\n{Style.CYAN}╔{bar}╗{Style.RESET}")
    print(f"{Style.CYAN}║{' ' * pl}{title_text}{' ' * pr}║{Style.RESET}")
    print(f"{Style.CYAN}╠{div}╣{Style.RESET}")

    if total_items == 1:
        if succeeded_count > 0:
            status_val = f"{Style.BOLD}{Style.GREEN}✅ Completed{Style.RESET}"
        elif skipped_count > 0:
            status_val = f"{Style.BOLD}{Style.YELLOW}⏩ Skipped (already on disk){Style.RESET}"
        else:
            status_val = f"{Style.BOLD}{Style.RED}❌ Failed{Style.RESET}"
        print(_box_line("• Status:", status_val))
        if saved_path:
            print(_box_line("• Saved To:", f"{Style.BOLD}{Style.WHITE}{saved_path}{Style.RESET}"))
        print(_box_line("• Transferred:", f"{Style.BOLD}{Style.CYAN}{format_bytes(total_bytes)}{Style.RESET}"))
        print(_box_line("• Elapsed Time:", f"{Style.BOLD}{Style.WHITE}{time_str}{Style.dim(speed_str)}{Style.RESET}"))
    else:
        print(_box_line("• Total Processed:", f"{Style.BOLD}{Style.WHITE}{total_items}{Style.RESET}"))
        print(_box_line("• Succeeded:", f"{Style.BOLD}{Style.GREEN}{succeeded_count} completed{Style.RESET}"))
        if skipped_count > 0:
            print(_box_line("• Skipped:", f"{Style.BOLD}{Style.YELLOW}{skipped_count} (already on disk){Style.RESET}"))
        if failed_count > 0:
            print(_box_line("• Failed / Errors:", f"{Style.BOLD}{Style.RED}{failed_count}{Style.RESET}"))

        # Determine destination folder for batch
        dest_display = None
        if saved_paths:
            cleaned_dirs = []
            for p in saved_paths:
                if p:
                    try:
                        p_str = p.lstrip("~")
                        p_obj = Path(p_str)
                        parent_dir = str(p_obj.parent) if p_obj.suffix else str(p_obj)
                        prefix = "~" if p.startswith("~") else ""
                        cleaned_dirs.append(f"{prefix}{parent_dir}")
                    except Exception:
                        cleaned_dirs.append(p)
            unique_dirs = sorted(set(cleaned_dirs))
            if len(unique_dirs) == 1:
                dest_display = unique_dirs[0]
            elif len(unique_dirs) > 1:
                try:
                    common = os.path.commonpath([d.lstrip("~") for d in unique_dirs])
                    dest_display = f"~{common}" if common else "~downloads"
                except Exception:
                    dest_display = "~downloads"
        elif saved_path:
            dest_display = saved_path

        if not dest_display and succeeded_count > 0:
            dest_display = "~downloads"

        if dest_display:
            print(_box_line("• Saved To:", f"{Style.BOLD}{Style.WHITE}{dest_display}{Style.RESET}"))

        print(_box_line("• Total Transferred:", f"{Style.BOLD}{Style.CYAN}{format_bytes(total_bytes)}{Style.RESET}"))
        print(_box_line("• Elapsed Time:", f"{Style.BOLD}{Style.WHITE}{time_str}{Style.dim(speed_str)}{Style.RESET}"))

    print(f"{Style.CYAN}╚{bar}╝{Style.RESET}\n")

    if total_items > 1 and saved_paths:
        distinct_dirs = sorted(set(
            f"~{Path(p.lstrip('~')).parent}" if Path(p.lstrip('~')).suffix else p
            for p in saved_paths if p
        ))
        if len(distinct_dirs) > 1 and len(distinct_dirs) <= 8:
            print(f"  {Style.bold('📁 Saved Folders:')}")
            for d in distinct_dirs:
                print(f"    • {Style.white(d)}")
            print()

    if failed_items:
        print(f"{Style.tag('❌', 'FAILED ITEMS', Style.RED)} The following links could not be downloaded:")
        for idx, (url, err) in enumerate(failed_items, 1):
            print(f"  {Style.dim(f'[{idx}]')} {Style.white(url)}")
            print(f"      {Style.red(f'Error: {err}')}")
        print(f"\n{Style.tag('💡', 'TIP', Style.CYAN)} You can retry all failed items anytime with: {Style.bold('python multi-dl.py queue retry')}\n")


