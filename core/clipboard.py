"""Clipboard auto-monitor, notification chimes, and failed-link recovery for MULTI_DOWNLOADER."""

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Set

from core.terminal import Style
from core.storage import file_lock, atomic_text_write

# Universal URL matcher
URL_REGEX = re.compile(
    r"(?:https?://[^\s<>\"'()\[\]{}]+|tg://[^\s<>\"'()\[\]{}]+)",
    re.IGNORECASE,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
FAILED_LINKS_PATH = DATA_DIR / "failed_links.txt"
CLIPBOARD_HISTORY_PATH = DATA_DIR / "clipboard_history.txt"
HISTORY_MAX_URLS = 1000


def get_clipboard_text() -> Optional[str]:
    """Retrieve raw text from system clipboard without third-party dependencies."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.CloseClipboard.restype = wintypes.BOOL
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
            user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.restype = wintypes.BOOL

            CF_UNICODETEXT = 13

            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None

            # Retry briefly if clipboard is locked by another window
            for _ in range(3):
                if user32.OpenClipboard(None):
                    break
                time.sleep(0.05)
            else:
                return None

            try:
                h_glb = user32.GetClipboardData(CF_UNICODETEXT)
                if not h_glb:
                    return None
                ptr = kernel32.GlobalLock(h_glb)
                if not ptr:
                    return None
                try:
                    return ctypes.c_wchar_p(ptr).value
                finally:
                    kernel32.GlobalUnlock(h_glb)
            finally:
                user32.CloseClipboard()
        except Exception:
            pass

    # Cross-platform fallback via tkinter or system commands
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        text = r.clipboard_get()
        r.destroy()
        return str(text) if text else None
    except Exception:
        pass

    return None


def play_chime() -> None:
    """Play a gentle audio notification chime."""
    if sys.platform == "win32":
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass
    else:
        # Fallback bell
        sys.stdout.write("\a")
        sys.stdout.flush()


def extract_urls(text: Optional[str]) -> List[str]:
    """Extract clean, valid URLs from raw text."""
    if not text:
        return []

    found = URL_REGEX.findall(text)
    clean_urls: List[str] = []
    seen: Set[str] = set()

    for url in found:
        # Strip trailing punctuation often copied accidentally with links
        u = url.rstrip(".,;:!?'\"")
        if u and u not in seen:
            seen.add(u)
            clean_urls.append(u)

    return clean_urls


def save_failed_urls(failed_urls: List[str]) -> None:
    """Append failed download URLs to data/failed_links.txt for recovery."""
    if not failed_urls:
        return
    try:
        FAILED_LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = set(load_failed_urls())
        with open(FAILED_LINKS_PATH, "a", encoding="utf-8") as f:
            for url in failed_urls:
                clean = url.strip()
                if clean and clean not in existing:
                    f.write(f"{clean}\n")
                    existing.add(clean)
    except Exception:
        pass


def load_failed_urls() -> List[str]:
    """Load list of previously failed download URLs."""
    if not FAILED_LINKS_PATH.exists():
        return []
    try:
        with open(FAILED_LINKS_PATH, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except Exception:
        return []


def clear_failed_urls() -> None:
    """Remove data/failed_links.txt once retried."""
    try:
        if FAILED_LINKS_PATH.exists():
            FAILED_LINKS_PATH.unlink()
    except Exception:
        pass


def append_clipboard_history(urls: List[str]) -> None:
    """Keep the most recent captured URLs in a bounded, ignored recovery file."""
    if not urls:
        return
    try:
        with file_lock(CLIPBOARD_HISTORY_PATH):
            entries = load_clipboard_history() + [url.strip() for url in urls if url.strip()]
            entries = entries[-HISTORY_MAX_URLS:]
            atomic_text_write(CLIPBOARD_HISTORY_PATH, '\n'.join(entries) + '\n')
    except Exception:
        pass


def load_clipboard_history() -> List[str]:
    """Load list of previously captured clipboard URLs."""
    if not CLIPBOARD_HISTORY_PATH.exists():
        return []
    try:
        with open(CLIPBOARD_HISTORY_PATH, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except Exception:
        return []


def clear_clipboard_history() -> None:
    with file_lock(CLIPBOARD_HISTORY_PATH):
        CLIPBOARD_HISTORY_PATH.unlink(missing_ok=True)


class ClipboardWatcher:
    """Monitors system clipboard for new media URLs in real time."""

    def __init__(self, poll_interval: float = 0.5, play_sound: bool = True):
        self.poll_interval = poll_interval
        self.play_sound = play_sound
        self.seen_urls: Set[str] = set()
        self.last_raw_text: Optional[str] = None

        # Pre-seed with existing clipboard content so existing text is not re-downloaded immediately
        initial_text = get_clipboard_text()
        if initial_text:
            self.last_raw_text = initial_text
            for u in extract_urls(initial_text):
                self.seen_urls.add(u)

    def poll_new_urls(self) -> List[str]:
        """Poll clipboard once; returns newly detected URLs if text changed."""
        current_text = get_clipboard_text()
        if not current_text or current_text == self.last_raw_text:
            return []

        self.last_raw_text = current_text
        extracted = extract_urls(current_text)
        new_urls: List[str] = []

        for url in extracted:
            if url not in self.seen_urls:
                self.seen_urls.add(url)
                new_urls.append(url)

        if new_urls:
            append_clipboard_history(new_urls)
            if self.play_sound:
                play_chime()

        return new_urls
