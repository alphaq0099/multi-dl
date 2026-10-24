"""
Data models representing tasks, queue items, archive entries, and progress.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    SKIPPED = "skipped"


class MediaType(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


@dataclass
class DownloadProgress:
    """Normalized progress update reported across all backends."""
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed_bytes_sec: Optional[float] = None
    eta_seconds: Optional[int] = None
    percent: Optional[float] = None
    current_file: Optional[str] = None
    total_files: Optional[int] = None
    file_index: Optional[int] = None
    status_message: Optional[str] = None
    extractor: Optional[str] = None
    container_title: Optional[str] = None
    phase: Optional[str] = None
    file_name: Optional[str] = None
    file_downloaded_bytes: Optional[int] = None
    file_total_bytes: Optional[int] = None
    completed_files: Optional[int] = None

    def update_percent(self) -> None:
        if self.total_bytes and self.total_bytes > 0:
            self.percent = round((self.downloaded_bytes / self.total_bytes) * 100, 2)
        elif self.total_files and self.file_index:
            self.percent = round((self.file_index / self.total_files) * 100, 2)


@dataclass
class DownloadTask:
    """Represents an active or planned download job."""
    url: str
    backend: str
    output_dir: str
    task_id: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    filename: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    file_hash: Optional[str] = None
    media_type: MediaType = MediaType.UNKNOWN
    metadata: Dict[str, Any] = field(default_factory=dict)
    options: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


@dataclass
class QueueItem:
    """Represents a job stored in the queue."""
    id: str
    url: str
    backend: Optional[str] = None  # None indicates auto-detect
    priority: int = 0              # Higher priority processed first
    options: Dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.QUEUED
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: Optional[str] = None


@dataclass
class ArchiveEntry:
    """Record of a completed download saved to the SQLite archive."""
    url: str
    file_path: str
    file_name: str
    backend: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    file_hash: Optional[str] = None
    file_size: Optional[int] = None
    source_site: Optional[str] = None
    media_type: MediaType = MediaType.UNKNOWN
    metadata: Dict[str, Any] = field(default_factory=dict)
    downloaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BatchSummaryReport:
    """Consolidated summary metrics after completing a batch of downloads."""
    total_items: int = 0
    succeeded_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    total_bytes: int = 0
    elapsed_seconds: float = 0.0
    failed_items: List[Dict[str, str]] = field(default_factory=list)

