"""
Unit tests for the Queue Manager, Fast Pre-Routing, and Queue Dispatcher.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.models import ArchiveEntry, DownloadTask, TaskStatus, BatchSummaryReport
from core.queue_manager import QueueManager
from core.router import URLRouter
from core.dispatcher import QueueDispatcher


# ---------------------------------------------------------------------------
# 1. Fast Pre-Routing Tests
# ---------------------------------------------------------------------------

def test_detect_backend_name_known_patterns():
    # YouTube / video streaming -> yt-dlp
    assert URLRouter.detect_backend_name("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "yt-dlp"
    assert URLRouter.detect_backend_name("https://youtu.be/dQw4w9WgXcQ") == "yt-dlp"
    assert URLRouter.detect_backend_name("https://vimeo.com/76979871") == "yt-dlp"
    assert URLRouter.detect_backend_name("https://twitch.tv/videos/123456") == "yt-dlp"

    # Telegram -> telegram-dl
    assert URLRouter.detect_backend_name("https://t.me/examplechannel/123") == "telegram-dl"
    assert URLRouter.detect_backend_name("https://telegram.me/joinchat/ABCDEF") == "telegram-dl"

    # TeraBox -> terabox-dl
    assert URLRouter.detect_backend_name("https://terabox.com/s/1abcdefg") == "terabox-dl"
    assert URLRouter.detect_backend_name("https://1024tera.com/s/1xyz123") == "terabox-dl"

    # Cyberdrop & bunkr file lockers
    # Common lockers (cyberdrop.me, bunkr.is) route to gallery-dl by design for high reliability
    assert URLRouter.detect_backend_name("https://cyberdrop.me/a/album1") == "gallery-dl"
    assert URLRouter.detect_backend_name("https://bunkr.is/a/xyz789") == "gallery-dl"

    # Dedicated cyberdrop-dl forum / locker domains
    assert URLRouter.detect_backend_name("https://saint.to/video/123") == "cyberdrop-dl"
    assert URLRouter.detect_backend_name("https://f95zone.to/threads/123") == "cyberdrop-dl"

    # Image galleries & social feeds -> gallery-dl
    assert URLRouter.detect_backend_name("https://imgur.com/a/albumid") == "gallery-dl"
    assert URLRouter.detect_backend_name("https://www.instagram.com/p/Cxyz123/") == "gallery-dl"
    assert URLRouter.detect_backend_name("https://danbooru.donmai.us/posts/1234") == "gallery-dl"


def test_detect_backend_name_unknown_falls_back_to_ytdlp():
    # Unknown site defaults to yt-dlp as universal fallback extractor
    detected = URLRouter.detect_backend_name("https://some-unrecognized-domain.org/media/video.mp4")
    assert detected == "yt-dlp"


# ---------------------------------------------------------------------------
# 2. QueueManager Core & Engine Constraint Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_queue_file(tmp_path):
    return tmp_path / "test_queue.json"


def test_queue_add_and_pre_routing(temp_queue_file):
    qm = QueueManager(temp_queue_file)

    # 1. Add YouTube URL without explicit backend -> auto-detects yt-dlp
    item1 = qm.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ", priority=10)
    assert item1.backend == "yt-dlp"
    assert item1.priority == 10
    assert item1.status == TaskStatus.QUEUED

    # 2. Add Telegram URL without explicit backend -> auto-detects telegram-dl
    item2 = qm.add("https://t.me/somechannel/42")
    assert item2.backend == "telegram-dl"

    # 3. Add URL with explicit backend override
    item3 = qm.add("https://example.com/test", backend="gallery-dl", priority=5)
    assert item3.backend == "gallery-dl"
    assert item3.priority == 5

    assert len(qm.list_all()) == 3


def test_queue_add_batch(temp_queue_file):
    qm = QueueManager(temp_queue_file)
    urls = [
        "https://www.youtube.com/watch?v=video1",
        "https://t.me/channel/100",
        "https://imgur.com/a/album2",
    ]
    items = qm.add_batch(urls, priority=20)
    assert len(items) == 3
    assert items[0].backend == "yt-dlp"
    assert items[1].backend == "telegram-dl"
    assert items[2].backend == "gallery-dl"
    assert all(it.priority == 20 for it in items)

    # Verify persistence
    qm_reloaded = QueueManager(temp_queue_file)
    assert len(qm_reloaded.list_all()) == 3


def test_get_next_eligible_with_engine_limits(temp_queue_file):
    """
    Verifies that get_next_eligible skips saturated engines (e.g. telegram-dl limit=1)
    to prevent pipeline starvation and lockup.
    """
    qm = QueueManager(temp_queue_file)
    # Add items in queue: item 1 (telegram-dl, priority 10), item 2 (yt-dlp, priority 5)
    qm.add("https://t.me/channel/1", priority=10)
    qm.add("https://www.youtube.com/watch?v=abc", priority=5)

    engine_limits = {"telegram-dl": 1, "yt-dlp": 2}

    # Scenario A: No engine currently active -> Telegram (higher priority) is selected
    active_counts = {"telegram-dl": 0, "yt-dlp": 0}
    next_item = qm.get_next_eligible(active_counts, engine_limits)
    assert next_item is not None
    assert next_item.backend == "telegram-dl"

    # Scenario B: Telegram slot is saturated (1/1 active)
    # get_next_eligible MUST skip Telegram and pick yt-dlp to keep the worker pool busy
    active_counts = {"telegram-dl": 1, "yt-dlp": 0}
    next_item = qm.get_next_eligible(active_counts, engine_limits)
    assert next_item is not None
    assert next_item.backend == "yt-dlp"

    # Scenario C: Both Telegram (1/1) and yt-dlp (2/2) are saturated
    active_counts = {"telegram-dl": 1, "yt-dlp": 2}
    next_item = qm.get_next_eligible(active_counts, engine_limits)
    assert next_item is None


def test_queue_retry_failed_and_stats(temp_queue_file):
    qm = QueueManager(temp_queue_file)
    item1 = qm.add("https://example.com/1")
    item2 = qm.add("https://example.com/2")
    item3 = qm.add("https://example.com/3")

    qm.mark_status(item1.id, TaskStatus.COMPLETED)
    qm.mark_status(item2.id, TaskStatus.FAILED, error="Network Timeout")
    qm.mark_status(item3.id, TaskStatus.FAILED, error="HTTP 404")

    stats = qm.stats()
    assert stats["by_status"].get("completed", 0) == 1
    assert stats["by_status"].get("failed", 0) == 2
    assert stats["by_status"].get("queued", 0) == 0

    # Retry all failed
    retried_count = qm.retry_failed()
    assert retried_count == 2

    # Verify status reset
    stats_after = qm.stats()
    assert stats_after["by_status"].get("failed", 0) == 0
    assert stats_after["by_status"].get("queued", 0) == 2


def test_queue_clear(temp_queue_file):
    qm = QueueManager(temp_queue_file)
    i1 = qm.add("https://example.com/1")
    i2 = qm.add("https://example.com/2")
    qm.mark_status(i1.id, TaskStatus.COMPLETED)

    # Clear only completed
    qm.clear(status_filter=TaskStatus.COMPLETED)
    remaining = qm.list_all()
    assert len(remaining) == 1
    assert remaining[0].id == i2.id

    # Clear all
    qm.clear()
    assert len(qm.list_all()) == 0


# ---------------------------------------------------------------------------
# 3. Concurrent QueueDispatcher Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_dispatcher_concurrent_execution(tmp_path):
    queue_file = tmp_path / "dispatcher_queue.json"
    archive_db = tmp_path / "archive.db"
    dl_dir = tmp_path / "downloads"

    qm = QueueManager(queue_file)
    qm.add("https://www.youtube.com/watch?v=test1")
    qm.add("https://imgur.com/a/album1")
    qm.add("https://t.me/channel/msg1")

    # Mock ConfigManager and ArchiveManager
    mock_config = MagicMock()
    mock_config.download_dir = dl_dir
    mock_config.archive_db_path = archive_db
    mock_config.archive_enabled = False
    mock_config.get.side_effect = lambda section, key, default=None: {
        ("archive", "enabled"): False,
        ("archive", "dedup_by_url"): False,
        ("general", "organize_by_backend"): True,
        ("general", "organize_by_site"): True,
        ("general", "concurrent_downloads"): 3,
    }.get((section, key), default)

    mock_archive = MagicMock()
    mock_archive.is_url_downloaded.return_value = False

    # Mock router & download execution
    mock_backend = AsyncMock()
    mock_backend.name = "test-backend"
    sample_file = dl_dir / "test_file.mp4"
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_text("dummy video content")

    mock_backend.download.return_value = ArchiveEntry(
        id="test-1",
        url="https://example.com",
        file_path=str(sample_file),
        file_name=sample_file.name,
        file_hash="hash123",
        file_size=1024,
        backend="test-backend",
        source_site="example.com",
        metadata={"title": "Test Media"},
    )

    mock_router = MagicMock()
    mock_router.route.return_value = mock_backend

    dispatcher = QueueDispatcher(
        config=mock_config,
        queue_manager=qm,
        archive=mock_archive,
        router=mock_router,
    )

    report = await dispatcher.run(concurrency=3)

    assert isinstance(report, BatchSummaryReport)
    assert report.total_items == 3
    assert report.succeeded_count == 3
    assert report.failed_count == 0
    assert report.total_bytes == 3 * 1024
    assert len(report.failed_items) == 0

    # All items should now be COMPLETED in the queue manager
    stats = qm.stats()
    assert stats["by_status"].get("completed", 0) == 3
    assert stats["by_status"].get("queued", 0) == 0


# ---------------------------------------------------------------------------
# 4. Input URL & Batch File Resolution Tests
# ---------------------------------------------------------------------------

def test_resolve_input_urls_with_text_file(tmp_path):
    import importlib
    multi_dl = importlib.import_module("multi-dl")
    resolve_input_urls = multi_dl.resolve_input_urls

    sample_file = tmp_path / "links.txt"
    sample_file.write_text(
        "# Header comment\n"
        "https://youtu.be/BZziHPgE_MI\n"
        "\n"
        "https://nhentai.net/g/633508/\n"
        "   https://youtu.be/eMys566gEbg   \n"
        "# Another comment\n"
        "https://teraboxlink.com/s/1yjwKQEm9J_RBnjbHbG34nw\n"
        "\n",
        encoding="utf-8"
    )

    extracted, sources = resolve_input_urls([str(sample_file)])
    assert len(sources) == 1
    assert str(sample_file) in sources[0]
    assert len(extracted) == 4
    assert extracted[0] == "https://youtu.be/BZziHPgE_MI"
    assert extracted[1] == "https://nhentai.net/g/633508/"
    assert extracted[2] == "https://youtu.be/eMys566gEbg"
    assert extracted[3] == "https://teraboxlink.com/s/1yjwKQEm9J_RBnjbHbG34nw"


def test_resolve_input_urls_mixed_file_and_urls(tmp_path):
    import importlib
    multi_dl = importlib.import_module("multi-dl")
    resolve_input_urls = multi_dl.resolve_input_urls

    sample_file = tmp_path / "urls.txt"
    sample_file.write_text("https://example.com/from_file_1\nhttps://example.com/from_file_2\n")

    extracted, sources = resolve_input_urls([
        str(sample_file),
        "https://example.com/direct_url"
    ])
    assert len(sources) == 1
    assert len(extracted) == 3
    assert "https://example.com/from_file_1" in extracted
    assert "https://example.com/from_file_2" in extracted
    assert "https://example.com/direct_url" in extracted


def test_separate_batch_queue_isolation(tmp_path):
    """
    Verifies that executing or adding to an active batch queue (data/batch_queue.json)
    leaves persistent queued items (data/queue.json) untouched.
    """
    persistent_file = tmp_path / "persistent_queue.json"
    batch_file = tmp_path / "active_batch_queue.json"

    persistent_qm = QueueManager(persistent_file)
    persistent_qm.add("https://t.me/channel/waiting_overnight")

    # Immediate batch run uses separate batch queue
    batch_qm = QueueManager(batch_file)
    batch_qm.clear()
    batch_qm.add("https://youtube.com/watch?v=immediate_1")
    batch_qm.add("https://imgur.com/a/immediate_2")

    # Verify isolation
    assert len(persistent_qm.list_all()) == 1
    assert persistent_qm.list_all()[0].url == "https://t.me/channel/waiting_overnight"

    assert len(batch_qm.list_all()) == 2
    assert all("immediate" in item.url for item in batch_qm.list_all())


@pytest.mark.asyncio
async def test_three_links_concurrent_slot_allocation(tmp_path):
    """
    Verifies that 3 links (1 yt-dlp + 2 gallery-dl) all allocate and run in 3 concurrent slots
    without running into the gallery-dl generator already executing race condition.
    """
    from core.archive import ArchiveManager
    from core.config import ConfigManager
    from core.dispatcher import QueueDispatcher
    from core.models import ArchiveEntry, DownloadTask

    queue_file = tmp_path / "test_3_slots_queue.json"
    qm = QueueManager(queue_file)

    links = [
        "https://youtu.be/Sy_X33lcg9g",
        "https://nhentai.net/g/361344/",
        "https://nhentai.net/g/576655/",
    ]
    qm.add_batch(links)

    # Mock backends to verify concurrent slot allocation
    mock_ytdlp = MagicMock()
    mock_ytdlp.name = "yt-dlp"
    mock_ytdlp.can_handle.side_effect = lambda u: "youtu" in u

    mock_gdl = MagicMock()
    mock_gdl.name = "gallery-dl"
    mock_gdl.can_handle.side_effect = lambda u: "nhentai" in u

    active_slots_seen = set()

    async def mock_dl(task: DownloadTask, progress_callback=None):
        await asyncio.sleep(0.05)
        return ArchiveEntry(
            id=f"test-{task.url}",
            url=task.url,
            file_path=str(tmp_path / "mock.mp4"),
            file_name="mock.mp4",
            file_hash=None,
            file_size=2048,
            backend=task.backend,
            source_site="mock",
        )

    mock_ytdlp.download = AsyncMock(side_effect=mock_dl)
    mock_gdl.download = AsyncMock(side_effect=mock_dl)

    router = URLRouter({
        "yt-dlp": mock_ytdlp,
        "gallery-dl": mock_gdl,
    })

    cfg = ConfigManager()
    archive = ArchiveManager(tmp_path / "archive.db")
    dispatcher = QueueDispatcher(config=cfg, queue_manager=qm, archive=archive, router=router)

    report = await dispatcher.run(concurrency=3)

    assert report.total_items == 3
    assert report.succeeded_count == 3
    assert report.failed_count == 0
    assert qm.count_remaining() == 0


def test_forum_routing_to_gallery_dl():
    """Verifies forum threads (XenForo / vBulletin) route to gallery-dl, not yt-dlp."""
    from core.router import URLRouter

    forum_urls = [
        "https://forums.socialmediagirls.com/threads/jhennifer_rodriguess_.363022/post-5394644",
        "https://socialmediagirls.com/threads/model-name.12345/",
        "https://simpcity.su/threads/thread-name.999/",
        "https://simpcity.to/threads/thread-name.888/",
        "https://vipergirls.to/threads/12345",
        "https://randomforum.com/threads/photo-pack.555/",
    ]
    for url in forum_urls:
        backend = URLRouter.detect_backend_name(url)
        assert backend == "gallery-dl", f"Expected gallery-dl for {url}, got {backend}"


@pytest.mark.asyncio
async def test_single_download_summary_printed(tmp_path, capsys):
    """Verifies that single downloads display a completion summary card with the saved path."""
    from core.archive import ArchiveManager
    from core.config import ConfigManager
    from core.dispatcher import QueueDispatcher
    from core.models import ArchiveEntry, DownloadTask

    queue_file = tmp_path / "single_summary_queue.json"
    qm = QueueManager(queue_file)
    qm.add("https://nhentai.net/g/533286/")

    mock_gdl = MagicMock()
    mock_gdl.name = "gallery-dl"
    mock_gdl.can_handle.return_value = True

    saved_file = tmp_path / "downloads" / "gallery-dl" / "nhentai" / "533286" / "001.jpg"
    saved_file.parent.mkdir(parents=True, exist_ok=True)
    saved_file.write_text("test")

    async def mock_dl(task: DownloadTask, progress_callback=None):
        return ArchiveEntry(
            id="test-id",
            url=task.url,
            file_path=str(saved_file),
            file_name=saved_file.name,
            file_hash=None,
            file_size=1024,
            backend="gallery-dl",
            source_site="nhentai",
        )

    mock_gdl.download = AsyncMock(side_effect=mock_dl)
    router = URLRouter({"gallery-dl": mock_gdl})

    cfg = ConfigManager(config_path=tmp_path / "config.json")
    cfg.set("general", "download_dir", str(tmp_path / "downloads"))
    archive = ArchiveManager(tmp_path / "archive.db")

    dispatcher = QueueDispatcher(config=cfg, queue_manager=qm, archive=archive, router=router)
    report = await dispatcher.run(concurrency=1)

    assert report.total_items == 1
    assert report.succeeded_count == 1

    out = capsys.readouterr().out
    assert "DOWNLOAD SUMMARY" in out
    assert "Completed" in out
    assert "Saved To:" in out


def test_queue_get_pending_items(temp_queue_file):
    qm = QueueManager(temp_queue_file)
    i1 = qm.add("https://example.com/1", priority=1)
    i2 = qm.add("https://example.com/2", priority=5)
    i3 = qm.add("https://example.com/3", priority=2)
    qm.mark_status(i1.id, TaskStatus.COMPLETED)

    pending = qm.get_pending_items()
    # i1 is completed, so only i2 (pri 5) and i3 (pri 2) are pending
    assert len(pending) == 2
    assert pending[0].id == i2.id
    assert pending[1].id == i3.id

    # Test limit
    limited = qm.get_pending_items(limit=1)
    assert len(limited) == 1
    assert limited[0].id == i2.id

