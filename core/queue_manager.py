"""
Queue manager for batch downloads, priorities, and concurrent worker dispatching.
Supports persistence in data/queue.json and engine-aware slot constraints.
"""

from collections import Counter
from datetime import datetime, timezone
import json
import copy
import os
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.parse
import uuid

from core.models import QueueItem, TaskStatus
from core.router import URLRouter
from core.exceptions import ConfigError
from core.storage import atomic_json_write, file_lock, process_alive


def transaction(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with file_lock(self.queue_file):
            self.load()
            return method(self, *args, **kwargs)
    return wrapped


def extract_url_domain(url: str) -> str:
    """Extracts lowercased hostname/domain from a URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUEUE_FILE = PROJECT_ROOT / "data" / "queue.json"


class QueueManager:
    """Manages ordered download queue with persistence and engine-aware scheduling."""

    DEFAULT_ENGINE_LIMITS = {
        "telegram-dl": 1,  # Prevent SQLite session file lock collisions
        "yt-dlp": 2,        # Cap concurrent FFmpeg muxing jobs
        "gallery-dl": 4,
        "terabox-dl": 2,
        "cyberdrop-dl": 2,
    }

    def __init__(self, queue_file: Optional[Path] = None):
        if queue_file is not None:
            p = Path(queue_file)
            self.queue_file = p if p.is_absolute() else (PROJECT_ROOT / p)
        else:
            self.queue_file = DEFAULT_QUEUE_FILE
        self._items: Dict[str, QueueItem] = {}
        self._owners = {}
        self.load()

    @transaction
    def add(
        self,
        url: str,
        backend: Optional[str] = None,
        priority: int = 0,
        options: Optional[Dict[str, Any]] = None,
    ) -> QueueItem:
        """Add a single URL to the queue with automatic pre-routing classification."""
        resolved_backend = backend or URLRouter.detect_backend_name(url)
        item_id = str(uuid.uuid4())
        item = QueueItem(
            id=item_id,
            url=url,
            backend=resolved_backend,
            priority=priority,
            options=copy.deepcopy(options or {}),
            status=TaskStatus.QUEUED,
        )
        self._items[item_id] = item
        self.save()
        return item

    @transaction
    def add_batch(
        self,
        urls: List[str],
        priority: int = 0,
        options: Optional[Dict[str, Any]] = None,
        backend: Optional[str] = None,
    ) -> List[QueueItem]:
        """Adds a collection of URLs to the queue, pre-classifying each link in bulk."""
        added = []
        for url in urls:
            clean_url = url.strip()
            if not clean_url or clean_url.startswith("#"):
                continue
            resolved_backend = backend or URLRouter.detect_backend_name(clean_url)
            item_id = str(uuid.uuid4())
            item = QueueItem(
                id=item_id,
                url=clean_url,
                backend=resolved_backend,
                priority=priority,
                options=copy.deepcopy(options or {}),
                status=TaskStatus.QUEUED,
            )
            self._items[item_id] = item
            added.append(item)
        if added:
            self.save()
        return added

    @staticmethod
    def _sort_key(item: QueueItem):
        dt = item.added_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (-item.priority, dt)

    @transaction
    def get_next(self) -> Optional[QueueItem]:
        """Fetch the next pending queue item by priority, then creation time."""
        queued_items = [
            item for item in self._items.values()
            if item.status in (TaskStatus.QUEUED, TaskStatus.PENDING)
        ]
        if not queued_items:
            return None
        # Sort by priority desc, then added_at asc
        queued_items.sort(key=self._sort_key)
        return queued_items[0]

    @transaction
    def get_next_eligible(
        self,
        active_engine_counts: Dict[str, int],
        engine_limits: Optional[Dict[str, int]] = None,
        active_domain_counts: Optional[Dict[str, int]] = None,
        max_per_domain: Optional[int] = None,
    ) -> Optional[QueueItem]:
        """
        Fetches the highest-priority pending task whose backend engine is not saturated
        and whose domain has not exceeded max_per_domain.
        If an engine or domain limit is reached, gracefully skips ahead to other pending items.
        """
        limits = dict(self.DEFAULT_ENGINE_LIMITS)
        if engine_limits:
            limits.update(engine_limits)

        queued_items = [
            item for item in self._items.values()
            if item.status in (TaskStatus.QUEUED, TaskStatus.PENDING)
        ]
        if not queued_items:
            return None

        # Sort by priority desc, then added_at asc
        queued_items.sort(key=self._sort_key)

        active_urls = {item.url for item in self._items.values() if item.status == TaskStatus.DOWNLOADING}
        for candidate in queued_items:
            if candidate.url in active_urls:
                continue
            engine_name = candidate.backend or "unknown"
            engine_key = engine_name.replace("_", "-")
            active_count = active_engine_counts.get(engine_name, 0)
            if engine_key in active_engine_counts:
                active_count = max(active_count, active_engine_counts[engine_key])
            max_allowed = limits.get(engine_key, limits.get(engine_name, 999))

            if active_count >= max_allowed:
                continue

            # Domain rate-limiting check
            if max_per_domain is not None and max_per_domain > 0 and active_domain_counts is not None:
                domain = extract_url_domain(candidate.url)
                if domain and active_domain_counts.get(domain, 0) >= max_per_domain:
                    continue

            return candidate

        return None

    def get_item(self, item_id: str) -> Optional[QueueItem]:
        """Retrieves a specific item by its ID."""
        return self._items.get(item_id)

    @transaction
    def mark_status(self, item_id: str, status: TaskStatus, error: Optional[str] = None) -> bool:
        """Update the status of a specific queue entry."""
        if item_id in self._items:
            if status == TaskStatus.DOWNLOADING:
                if self._items[item_id].status not in (TaskStatus.QUEUED, TaskStatus.PENDING):
                    return False
                self._owners[item_id] = os.getpid()
            else:
                self._owners.pop(item_id, None)
            self._items[item_id].status = status
            self._items[item_id].error_message = error
            self.save()
            return True
        return False

    @transaction
    def remove(self, item_id: str) -> None:
        """Remove an item from the queue."""
        if item_id in self._items:
            if self._items[item_id].status == TaskStatus.DOWNLOADING:
                raise ConfigError('Cannot remove an active download; stop it first.')
            del self._items[item_id]
            self.save()

    @transaction
    def retry_failed(self) -> int:
        """Resets all failed items back to QUEUED status and clears error messages."""
        count = 0
        for item in self._items.values():
            if item.status == TaskStatus.FAILED:
                item.status = TaskStatus.QUEUED
                item.error_message = None
                count += 1
        if count > 0:
            self.save()
        return count

    @transaction
    def list_all(self, status_filter: Optional[TaskStatus] = None) -> List[QueueItem]:
        """List all items in the queue, optionally filtered by status."""
        items = list(self._items.values())
        if status_filter:
            items = [item for item in items if item.status == status_filter]
        items.sort(key=self._sort_key)
        return items

    @transaction
    def clear(self, status_filter: Optional[TaskStatus] = None) -> int:
        """Clear items matching status_filter, or all items if None."""
        to_remove = [k for k, v in self._items.items()
                     if v.status != TaskStatus.DOWNLOADING
                     and (status_filter is None or v.status == status_filter)]
        count = len(to_remove)
        for k in to_remove:
            del self._items[k]
            self._owners.pop(k, None)
        self.save()
        return count

    @transaction
    def count_remaining(self) -> int:
        """Returns the number of items that are still queued or pending."""
        return sum(
            1 for item in self._items.values()
            if item.status in (TaskStatus.QUEUED, TaskStatus.PENDING)
        )

    @transaction
    def get_pending_items(self, limit: Optional[int] = None) -> List[QueueItem]:
        """Returns ordered pending/queued items waiting to be dispatched."""
        queued_items = [
            item for item in self._items.values()
            if item.status in (TaskStatus.QUEUED, TaskStatus.PENDING)
        ]
        queued_items.sort(key=self._sort_key)
        if limit is not None:
            return queued_items[:limit]
        return queued_items

    @transaction
    def stats(self) -> Dict[str, Any]:
        """Calculates current queue statistics grouped by status and backend."""
        status_counts = Counter(item.status.value for item in self._items.values())
        backend_counts = Counter((item.backend or "auto") for item in self._items.values())
        return {
            "total": len(self._items),
            "by_status": dict(status_counts),
            "by_backend": dict(backend_counts),
        }

    def save(self) -> None:
        """Save queue items to disk."""
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        serialized = [
            {
                "id": item.id,
                "url": item.url,
                "backend": item.backend,
                "priority": item.priority,
                "options": item.options,
                "status": item.status.value,
                "added_at": item.added_at.isoformat(),
                "error_message": item.error_message,
                "owner_pid": self._owners.get(item.id),
            }
            for item in self._items.values()
        ]
        with file_lock(self.queue_file):
            atomic_json_write(self.queue_file, serialized)

    def load(self) -> None:
        """Load queue from disk if present."""
        with file_lock(self.queue_file):
            self._load_locked()

    def _load_locked(self) -> None:
        if not self.queue_file.exists():
            self._items = {}
            self._owners = {}
            return
        try:
            with open(self.queue_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    raise ValueError('Queue must contain a JSON list')
                items, owners = {}, {}
                recovered = False
                for entry in data:
                    try:
                        added_at = datetime.fromisoformat(entry.get("added_at")) if entry.get("added_at") else datetime.now(timezone.utc)
                    except Exception:
                        added_at = datetime.now(timezone.utc)
                    if added_at.tzinfo is None:
                        added_at = added_at.replace(tzinfo=timezone.utc)
                    status = TaskStatus(entry.get('status', TaskStatus.QUEUED.value))
                    owner = entry.get('owner_pid')
                    if status == TaskStatus.DOWNLOADING and not process_alive(owner):
                        status = TaskStatus.QUEUED
                        recovered = True
                    elif status == TaskStatus.DOWNLOADING:
                        owners[entry['id']] = owner
                    items[entry["id"]] = QueueItem(
                        id=entry["id"],
                        url=entry["url"],
                        backend=entry.get("backend"),
                        priority=entry.get("priority", 0),
                        options=entry.get("options", {}),
                        status=status,
                        added_at=added_at,
                        error_message=entry.get("error_message"),
                    )
                self._items, self._owners = items, owners
            if recovered:
                self.save()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f'Cannot load queue {self.queue_file.name}; original file preserved: {exc}') from exc
