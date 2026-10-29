import pytest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from core.archive import ArchiveManager
from core.dispatcher import QueueDispatcher
from core.exceptions import ArchiveDuplicateError
from core.models import ArchiveEntry, MediaType
from core.queue_manager import QueueManager
from core.router import URLRouter


@pytest.fixture
def temp_archive(tmp_path: Path) -> ArchiveManager:
    db_file = tmp_path / "test_archive.db"
    return ArchiveManager(db_file)


def test_archive_initialization(temp_archive: ArchiveManager):
    assert temp_archive.db_path.exists()
    with temp_archive._get_connection() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t["name"] for t in tables]
        assert "downloads" in table_names


def test_add_and_get_by_url(temp_archive: ArchiveManager):
    entry = ArchiveEntry(
        id=str(uuid.uuid4()),
        url="https://youtube.com/watch?v=test1234",
        file_path="C:/downloads/yt-dlp/test1234.mp4",
        file_name="test1234.mp4",
        file_hash="abc123hash",
        file_size=1048576,
        backend="yt-dlp",
        source_site="youtube",
        media_type=MediaType.VIDEO,
        metadata={"uploader": "Test Channel"},
    )
    temp_archive.add_entry(entry)

    # Check is_url_downloaded
    assert temp_archive.is_url_downloaded("https://youtube.com/watch?v=test1234") is True
    assert temp_archive.is_url_downloaded("https://youtube.com/watch?v=test1234", backend="yt-dlp") is True
    assert temp_archive.is_url_downloaded("https://youtube.com/watch?v=test1234", backend="gallery-dl") is False
    assert temp_archive.is_url_downloaded("https://unknown.com/file") is False

    # Check get_by_url
    retrieved = temp_archive.get_by_url("https://youtube.com/watch?v=test1234")
    assert retrieved is not None
    assert retrieved.file_name == "test1234.mp4"
    assert retrieved.file_size == 1048576
    assert retrieved.media_type == MediaType.VIDEO
    assert retrieved.metadata.get("uploader") == "Test Channel"


def test_add_upsert_behavior(temp_archive: ArchiveManager):
    entry1 = ArchiveEntry(
        url="https://example.com/video.mp4",
        file_path="C:/downloads/old.mp4",
        file_name="old.mp4",
        backend="yt-dlp",
        file_size=500,
    )
    temp_archive.add_entry(entry1)

    # Re-insert with updated values using upsert=True
    entry2 = ArchiveEntry(
        url="https://example.com/video.mp4",
        file_path="C:/downloads/new.mp4",
        file_name="new.mp4",
        backend="yt-dlp",
        file_size=1200,
    )
    temp_archive.add_entry(entry2, upsert=True)

    updated = temp_archive.get_by_url("https://example.com/video.mp4")
    assert updated is not None
    assert updated.file_name == "new.mp4"
    assert updated.file_size == 1200

    # With upsert=False, inserting a duplicate should raise ArchiveDuplicateError
    with pytest.raises(ArchiveDuplicateError):
        temp_archive.add_entry(entry2, upsert=False)


def test_get_by_hash(temp_archive: ArchiveManager):
    entry = ArchiveEntry(
        url="https://example.com/file1.png",
        file_path="C:/downloads/file1.png",
        file_name="file1.png",
        file_hash="unique_sha256_hash_123",
        file_size=2048,
        backend="gallery-dl",
        media_type=MediaType.IMAGE,
    )
    temp_archive.add_entry(entry)

    found = temp_archive.get_by_hash("unique_sha256_hash_123")
    assert found is not None
    assert found.url == "https://example.com/file1.png"

    not_found = temp_archive.get_by_hash("nonexistent_hash")
    assert not_found is None


def test_search_and_list_recent(temp_archive: ArchiveManager):
    for i in range(5):
        temp_archive.add_entry(
            ArchiveEntry(
                url=f"https://example.com/video_{i}.mp4",
                file_path=f"C:/downloads/video_{i}.mp4",
                file_name=f"Cool_Video_Part_{i}.mp4",
                backend="yt-dlp" if i % 2 == 0 else "gallery-dl",
                file_size=1000 * (i + 1),
            )
        )

    # Search
    matches = temp_archive.search("Part_2")
    assert len(matches) == 1
    assert matches[0].file_name == "Cool_Video_Part_2.mp4"

    # List recent
    recent = temp_archive.list_recent(limit=3)
    assert len(recent) == 3


def test_delete_and_clear(temp_archive: ArchiveManager):
    entry = ArchiveEntry(
        url="https://example.com/delete_me.mp4",
        file_path="C:/downloads/del.mp4",
        file_name="del.mp4",
        backend="yt-dlp",
    )
    temp_archive.add_entry(entry)
    assert temp_archive.is_url_downloaded("https://example.com/delete_me.mp4") is True

    # Delete by URL
    deleted = temp_archive.delete_by_url("https://example.com/delete_me.mp4")
    assert deleted is True
    assert temp_archive.is_url_downloaded("https://example.com/delete_me.mp4") is False

    # Clear
    temp_archive.add_entry(entry)
    cleared_count = temp_archive.clear_archive()
    assert cleared_count == 1
    assert temp_archive.get_stats()["total_count"] == 0


def test_is_container_url():
    # Single media URLs (NOT containers)
    assert URLRouter.is_container_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False
    assert URLRouter.is_container_url("https://youtu.be/dQw4w9WgXcQ") is False
    assert URLRouter.is_container_url("https://www.instagram.com/p/DAabcdefg/") is False
    assert URLRouter.is_container_url("https://www.instagram.com/reel/DAabcdefg/") is False
    assert URLRouter.is_container_url("https://x.com/user/status/1234567890") is False
    assert URLRouter.is_container_url("https://twitter.com/user/status/1234567890") is False
    assert URLRouter.is_container_url("https://bunkr.cr/f/sample_file_123") is False
    assert URLRouter.is_container_url("https://bunkr.cr/v/testvideo123") is False
    assert URLRouter.is_container_url("https://t.me/channel/42") is False
    assert URLRouter.is_container_url("https://example.com/direct_video.mp4") is False

    # Container URLs (Playlists, Channels, Albums, Bookmarks)
    assert URLRouter.is_container_url("https://www.youtube.com/playlist?list=PL12345") is True
    assert URLRouter.is_container_url("https://www.youtube.com/@mkbhd/videos") is True
    assert URLRouter.is_container_url("https://www.instagram.com/user/saved/all-posts/") is True
    assert URLRouter.is_container_url("https://www.instagram.com/user/saved/") is True
    assert URLRouter.is_container_url("https://www.instagram.com/artist_profile/") is True
    assert URLRouter.is_container_url("https://x.com/user/media") is True
    assert URLRouter.is_container_url("https://x.com/user/likes") is True
    assert URLRouter.is_container_url("https://bunkr.cr/a/sample_album_123") is True
    assert URLRouter.is_container_url("https://t.me/telegram_channel") is True
    assert URLRouter.is_container_url("https://reddit.com/r/pics") is True
    assert URLRouter.is_container_url("https://simpcity.su/threads/sample-thread.1234/") is True


@pytest.mark.asyncio
async def test_dispatcher_skips_archived_single_url(tmp_path: Path):
    db_file = tmp_path / "archive.db"
    archive = ArchiveManager(db_file)

    # Pre-record a downloaded video
    dummy_file = tmp_path / "sample.mp4"
    dummy_file.write_bytes(b"12345678")

    archive.add_entry(
        ArchiveEntry(
            url="https://youtube.com/watch?v=already_downloaded",
            file_path=str(dummy_file),
            file_name="sample.mp4",
            backend="yt-dlp",
            file_size=8,
        )
    )

    # Config setup
    mock_config = MagicMock()
    mock_config.download_dir = tmp_path / "downloads"
    mock_config.archive_enabled = True
    mock_config.get.side_effect = lambda sec, key, def_val=None: {
        ("archive", "enabled"): True,
        ("archive", "dedup_by_url"): True,
        ("archive", "verify_file_exists"): True,
        ("general", "organize_by_backend"): False,
        ("general", "organize_by_site"): False,
    }.get((sec, key), def_val)

    queue = QueueManager(tmp_path / "queue.json")
    item = queue.add_batch(["https://youtube.com/watch?v=already_downloaded"], backend="yt-dlp")[0]

    dispatcher = QueueDispatcher(config=mock_config, queue_manager=queue, archive=archive)

    # Execute item
    mock_multi_bar = MagicMock()
    success, skipped, bytes_transferred, err, display_path = await dispatcher._execute_item(
        item, slot_id=1, multi_bar=mock_multi_bar
    )

    assert success is True
    assert skipped is True
    assert bytes_transferred == 0
    assert err is None
    mock_multi_bar.finish_slot.assert_called_with(1, "~sample.mp4", status_type="skipped")


@pytest.mark.asyncio
async def test_dispatcher_does_not_skip_container_even_if_in_archive(tmp_path: Path):
    db_file = tmp_path / "archive.db"
    archive = ArchiveManager(db_file)

    # Record container URL
    archive.add_entry(
        ArchiveEntry(
            url="https://bunkr.cr/a/sample_album_123",
            file_path=str(tmp_path),
            file_name="AlbumFolder",
            backend="gallery-dl",
        )
    )

    mock_config = MagicMock()
    mock_config.download_dir = tmp_path / "downloads"
    mock_config.archive_enabled = True
    mock_config.get.side_effect = lambda sec, key, def_val=None: {
        ("archive", "enabled"): True,
        ("archive", "dedup_by_url"): True,
        ("archive", "verify_file_exists"): True,
        ("general", "organize_by_backend"): False,
        ("general", "organize_by_site"): False,
    }.get((sec, key), def_val)

    queue = QueueManager(tmp_path / "queue.json")
    item = queue.add_batch(["https://bunkr.cr/a/sample_album_123"], backend="gallery-dl")[0]

    dispatcher = QueueDispatcher(config=mock_config, queue_manager=queue, archive=archive)

    # Mock the backend router so it returns a dummy backend that downloads
    mock_backend = MagicMock()
    mock_backend.download = AsyncMock(
        return_value=ArchiveEntry(
            url="https://bunkr.cr/a/sample_album_123",
            file_path=str(tmp_path / "new_album"),
            file_name="new_album",
            backend="gallery-dl",
            file_size=2048,
        )
    )
    dispatcher.router.route = MagicMock(return_value=mock_backend)

    mock_multi_bar = MagicMock()
    success, skipped, bytes_transferred, err, display_path = await dispatcher._execute_item(
        item, slot_id=1, multi_bar=mock_multi_bar
    )

    # Container should NOT be skipped at dispatcher level; it must be dispatched to the backend!
    mock_backend.download.assert_called_once()
    assert success is True
    assert skipped is False
