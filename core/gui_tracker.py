"""
GUI Progress Tracker Adapter for MULTI_DOWNLOADER.
Implements the MultiBarManager visual slot interface to feed real-time download
progress, speeds, ETAs, and status updates directly to the Flet Desktop GUI.
"""

from dataclasses import dataclass, field
import copy
import threading
from functools import wraps
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from core.progress import infer_phase


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def format_bytes(size: Optional[float]) -> str:
    """Formats bytes into human-readable string (B, KB, MB, GB, TB)."""
    if size is None or size < 0:
        return "??"
    s = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if s < 1024.0:
            return f"{s:.2f} {unit}"
        s /= 1024.0
    return f"{s:.2f} PB"


def format_dual_bytes(downloaded: int, total: Optional[int]) -> str:
    """Formats downloaded and total bytes into single-unit pair."""
    if total is None or total <= 0:
        if downloaded <= 0:
            return "0.0 B / ??"
        return f"{format_bytes(downloaded)} / ??"

    t_val = float(total)
    d_val = float(downloaded)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if t_val < 1024.0 or unit == "TB":
            return f"{d_val:.1f} / {t_val:.1f} {unit}"
        t_val /= 1024.0
        d_val /= 1024.0
    return f"{d_val:.1f} / {t_val:.1f} PB"


@dataclass
class GuiDownloadSlot:
    slot_id: int
    task_id: str
    backend: str
    url: str
    title: str = ""
    percent: Optional[float] = None
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed_bytes_sec: Optional[float] = None
    eta_seconds: Optional[float] = None
    file_index: Optional[int] = None
    total_files: Optional[int] = None
    status: str = "active"  # "active", "completed", "skipped", "failed"
    status_message: str = "Starting..."
    saved_path: Optional[str] = None
    error_message: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    phase: str = 'waiting'
    file_name: Optional[str] = None
    file_downloaded_bytes: Optional[int] = None
    file_total_bytes: Optional[int] = None
    completed_files: Optional[int] = None


class GuiMultiBarTracker:
    """
    Adapter passed to QueueDispatcher.run(tracker=...) to capture all slot
    events and invoke a UI callback without ANSI terminal formatting.
    """

    def __init__(self, on_update_callback: Optional[Callable[[], None]] = None):
        self._lock = threading.RLock()
        self._next_slot = 1
        self.on_update_callback = on_update_callback
        self.slots: Dict[int, GuiDownloadSlot] = {}
        self.history: List[GuiDownloadSlot] = []
        self.queue_remaining: int = 0
        self.queue_completed: int = 0
        self.queue_failed: int = 0
        self.queue_skipped: int = 0
        self.pending_items: List[Tuple[str, str]] = []
        self._last_notified = 0.0
        self._min_interval = 0.08  # Max ~12.5 FPS update rate to avoid Flet event loop thrashing

    def _notify(self, force: bool = False):
        now = time.time()
        if force or (now - self._last_notified >= self._min_interval):
            self._last_notified = now
            if self.on_update_callback:
                try:
                    self.on_update_callback()
                except Exception:
                    pass

    @synchronized
    def assign_slot(self, slot_id_or_task_id: Any, backend: str, url: str) -> int:
        # Generate an integer slot index if slot_id_or_task_id is string
        if isinstance(slot_id_or_task_id, int):
            sid = slot_id_or_task_id
            tid = str(slot_id_or_task_id)
        else:
            tid = str(slot_id_or_task_id)
            # Find first available slot ID
            sid = self._next_slot
        self._next_slot = max(self._next_slot, sid + 1)

        slot = GuiDownloadSlot(
            slot_id=sid,
            task_id=tid,
            backend=backend or "auto",
            url=url,
            title=url,
            status="active",
            status_message="Preparing download",
        )
        self.slots[sid] = slot
        self._notify(force=True)
        return sid

    @synchronized
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
    ):
        slot = self.slots.get(slot_id)
        if not slot:
            return

        if percent is not None:
            slot.percent = percent
        if downloaded is not None:
            slot.downloaded_bytes = downloaded
        if total is not None:
            slot.total_bytes = total
        if speed is not None:
            slot.speed_bytes_sec = speed
        if title:
            slot.title = title
        elif container_title and not slot.title:
            slot.title = container_title

        if file_index is not None:
            slot.file_index = file_index
        if total_files is not None:
            slot.total_files = total_files
        if status_message:
            slot.status_message = status_message
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

        # Calculate ETA if possible
        if slot.total_bytes and slot.downloaded_bytes and slot.speed_bytes_sec and slot.speed_bytes_sec > 0:
            remaining = max(0, slot.total_bytes - slot.downloaded_bytes)
            slot.eta_seconds = remaining / slot.speed_bytes_sec
        else:
            slot.eta_seconds = None

        slot.last_update = time.time()
        self._notify(force=False)

    @synchronized
    def finish_slot(
        self,
        slot_id: int,
        saved_path: Optional[str] = None,
        status_type: str = "completed",
        error_msg: Optional[str] = None,
    ):
        slot = self.slots.get(slot_id)
        if slot:
            slot.status = status_type
            slot.saved_path = saved_path
            slot.error_message = error_msg
            if status_type == "completed":
                slot.percent = 100.0
                slot.status_message = "Completed"
            elif status_type == "skipped":
                slot.status_message = "Skipped (Already archived)"
            elif status_type == "failed":
                slot.status_message = f"Failed: {error_msg or 'Unknown error'}"

            # Keep in history and notify
            self.history.append(slot)
            self.history = self.history[-100:]
            finished = [sid for sid, value in self.slots.items() if value.status != 'active']
            for sid in finished[:-20]:
                del self.slots[sid]
            self._notify(force=True)

    @synchronized
    def set_queue_counts(self, remaining: int = 0, completed: int = 0, failed: int = 0, skipped: int = 0):
        self.queue_remaining = remaining
        self.queue_completed = completed
        self.queue_failed = failed
        self.queue_skipped = skipped
        self._notify(force=False)

    @synchronized
    def set_pending_items(self, pending: List[Tuple[str, str]]):
        self.pending_items = list(pending)
        self._notify(force=False)

    def close(self):
        self._notify(force=True)

    @synchronized
    def clear_finished(self):
        """Clears completed or failed slots from the active list."""
        self.slots = {sid: slot for sid, slot in self.slots.items() if slot.status == "active"}
        self._notify(force=True)

    @synchronized
    def snapshot(self):
        return copy.deepcopy(self.slots), copy.deepcopy(self.history)
