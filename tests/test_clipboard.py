"""Unit tests for core/clipboard.py: clipboard watcher, URL extractor, and failed link storage."""

from unittest.mock import patch
import pytest

from core.clipboard import ClipboardWatcher, clear_failed_urls, extract_urls, load_failed_urls, save_failed_urls


def test_extract_urls():
    text = (
        "Check these links: https://www.youtube.com/watch?v=12345678901, "
        "and also https://bunkr.cr/a/example! "
        "tg://resolve?domain=testchannel and some non-url text."
    )
    urls = extract_urls(text)
    assert len(urls) == 3
    assert "https://www.youtube.com/watch?v=12345678901" in urls
    assert "https://bunkr.cr/a/example" in urls
    assert "tg://resolve?domain=testchannel" in urls


def test_extract_urls_empty():
    assert extract_urls("") == []
    assert extract_urls(None) == []
    assert extract_urls("just regular text without links") == []


def test_failed_urls_storage(tmp_path, monkeypatch):
    test_failed_file = tmp_path / "failed_links.txt"
    monkeypatch.setattr("core.clipboard.FAILED_LINKS_PATH", test_failed_file)

    # Initial load is empty
    assert load_failed_urls() == []

    # Save URLs
    save_failed_urls(["https://example.com/failed1", "https://example.com/failed2"])
    loaded = load_failed_urls()
    assert len(loaded) == 2
    assert "https://example.com/failed1" in loaded

    # Deduplicate when saving more
    save_failed_urls(["https://example.com/failed2", "https://example.com/failed3"])
    loaded2 = load_failed_urls()
    assert len(loaded2) == 3
    assert "https://example.com/failed3" in loaded2

    # Clear
    clear_failed_urls()
    assert load_failed_urls() == []
    assert not test_failed_file.exists()


def test_clipboard_watcher_polling():
    with patch("core.clipboard.get_clipboard_text", return_value="initial text with https://site.com/1"):
        watcher = ClipboardWatcher(play_sound=False)
        assert len(watcher.seen_urls) == 1
        assert "https://site.com/1" in watcher.seen_urls

    # Same text polled again -> no new URLs
    with patch("core.clipboard.get_clipboard_text", return_value="initial text with https://site.com/1"):
        new_urls = watcher.poll_new_urls()
        assert new_urls == []

    # New clipboard text copied -> discovers new URL
    with patch("core.clipboard.get_clipboard_text", return_value="New link https://site.com/2"):
        new_urls = watcher.poll_new_urls()
        assert new_urls == ["https://site.com/2"]
        assert "https://site.com/2" in watcher.seen_urls

    # Text changed but contains already seen URL -> no new URLs
    with patch("core.clipboard.get_clipboard_text", return_value="Copied again https://site.com/2"):
        new_urls = watcher.poll_new_urls()
        assert new_urls == []


@pytest.mark.asyncio
async def test_live_watcher_streaming(tmp_path):
    import asyncio
    from core.config import ConfigManager
    from core.archive import ArchiveManager
    from core.queue_manager import QueueManager
    from core.dispatcher import QueueDispatcher

    config = ConfigManager()
    archive = ArchiveManager(tmp_path / "test_archive.db")
    queue_mgr = QueueManager(tmp_path / "test_live_queue.json")
    dispatcher = QueueDispatcher(config=config, queue_manager=queue_mgr, archive=archive)

    stop_event = asyncio.Event()

    class MockWatcher:
        def __init__(self):
            self.calls = 0
        def poll_new_urls(self):
            self.calls += 1
            if self.calls == 1:
                return ["https://site.com/live1"]
            elif self.calls == 2:
                return ["https://site.com/live2"]
            else:
                stop_event.set()
                return []

    async def mock_exec(item, slot_id, multi_bar):
        return True, False, 1000, None, "saved_test"

    dispatcher._execute_item = mock_exec

    await dispatcher.run_live_watcher(MockWatcher(), stop_event)

    assert queue_mgr.count_remaining() == 0


def test_clipboard_history_storage(tmp_path, monkeypatch):
    test_hist_file = tmp_path / "clipboard_history.txt"
    monkeypatch.setattr("core.clipboard.CLIPBOARD_HISTORY_PATH", test_hist_file)

    from core.clipboard import append_clipboard_history, load_clipboard_history

    assert load_clipboard_history() == []

    append_clipboard_history(["https://example.com/item1", "https://example.com/item2"])
    loaded = load_clipboard_history()
    assert len(loaded) == 2
    assert loaded[0] == "https://example.com/item1"
    assert loaded[1] == "https://example.com/item2"
