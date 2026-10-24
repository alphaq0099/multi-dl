import pytest
from backends.gallerydl_backend import GalleryDlBackend
from gallery_dl import config as gdl_config


def test_gallerydl_can_handle():
    backend = GalleryDlBackend()
    assert backend.can_handle("https://twitter.com/jack/status/20") is True
    assert backend.can_handle("https://x.com/jack/status/20") is True
    assert backend.can_handle("https://www.reddit.com/r/aww/comments/12345/cute_dog/") is True
    assert backend.can_handle("https://imgur.com/gallery/abcde") is True
    assert backend.can_handle("https://danbooru.donmai.us/posts/12345") is True
    # Should not handle telegram
    assert backend.can_handle("https://t.me/c/12345/678") is False


def test_gallerydl_modular_config_loading(tmp_path):
    backend = GalleryDlBackend({
        "config_file": "configs/gallery-dl.json"
    })
    
    test_out = tmp_path / "downloads" / "gallery-dl" / "reddit"
    backend._configure_job(out_dir=test_out, disable_archive=True)
    
    # Verify modular config values from gallery-dl.json were loaded
    assert gdl_config.get(("extractor", "civitai"), "metadata") is True
    assert gdl_config.get(("extractor",), "retries") == 11
    
    # Verify base-directory was overridden to target dir
    assert gdl_config.get(("extractor",), "base-directory") == str(test_out.resolve())
    
    # Verify archive was disabled for test isolation
    assert gdl_config.get(("extractor",), "archive") is None


def test_gallerydl_batch_tracker(tmp_path):
    from backends.gallerydl_backend import _GalleryDlBatchTracker

    tracker = _GalleryDlBatchTracker()
    assert tracker.total_files is None
    assert tracker.completed_count == 0

    # 1. On directory metadata
    tracker.on_directory({"count": 50, "title": "Sample Gallery"})
    assert tracker.total_files == 50
    assert tracker.title_announced is True

    # 2. On URL queued
    tracker.on_url("https://example.com/001.jpg", {"num": 1, "filename": "001.jpg"})
    assert tracker.current_filename == "001.jpg"

    # 3. On start & progress
    dummy_file = tmp_path / "001.jpg"
    dummy_file.write_bytes(b"hello world")

    tracker.start(str(dummy_file))
    assert tracker.current_filename == "001.jpg"

    tracker.progress(1000, 500, 250000)
    assert tracker.current_total == 1000
    assert tracker.current_downloaded == 500
    assert tracker.current_speed == 250000.0

    # 4. On success
    tracker.success(str(dummy_file))
    assert tracker.completed_count == 1
    assert len(tracker.downloaded_files) == 1
    assert sum(tracker._file_bytes.values()) == 11  # len(b"hello world")

    # 5. On skip
    tracker.skip(str(dummy_file))
    assert tracker.completed_count == 2
    assert tracker.skipped_count == 1

    # 6. Finish
    tracker.finish()


def test_gallerydl_nested_album_metadata():
    """Verify extractors like Pixeldrain that store count inside album dict are detected."""
    from backends.gallerydl_backend import _GalleryDlBatchTracker

    tracker = _GalleryDlBatchTracker()
    tracker.on_directory({"album": {"title": "G1", "count": 12}})
    assert tracker.total_files == 12
    assert tracker.title_announced is True

    # Verify visible length calculation ignores ANSI and handles wide characters
    sample = "\033[36m📥\033[0m [\033[36m 50.0%\033[0m]"
    assert tracker._visible_len(sample) == 11


@pytest.mark.asyncio
async def test_gallerydl_failover_to_cyberdrop(monkeypatch, tmp_path):
    from unittest.mock import MagicMock, AsyncMock, patch
    from core.models import DownloadTask

    backend = GalleryDlBackend({"enable_failover_to_cyberdrop": True})
    task = DownloadTask(
        url="https://bunkr.cr/a/sample123",
        backend="gallery-dl",
        output_dir=str(tmp_path),
    )

    # Force gallery-dl download to raise an exception
    def failing_run(*args, **kwargs):
        raise RuntimeError("Simulated gallery-dl HTTP 500 failure")

    monkeypatch.setattr(backend, "_configure_job", failing_run)

    # Mock cyberdrop-dl backend
    mock_cdl_instance = MagicMock()
    mock_cdl_instance.can_handle.return_value = True
    mock_entry = MagicMock(file_path=str(tmp_path / "fallback.mp4"), backend="cyberdrop-dl")
    mock_cdl_instance.download = AsyncMock(return_value=mock_entry)

    with patch("backends.cyberdrop_backend.CyberdropDlBackend", return_value=mock_cdl_instance):
        result = await backend._download_local(task)
        assert result.backend == "cyberdrop-dl"
        mock_cdl_instance.download.assert_called_once()


def test_gallerydl_concurrent_thread_safety():
    """Verifies that multiple concurrent threads can instantiate jobs without generator already executing error."""
    import threading
    from gallery_dl import job

    GalleryDlBackend.pre_initialize()

    errors = []

    def run_worker(url):
        try:
            j = job.DownloadJob(url)
            assert j is not None
        except Exception as e:
            errors.append((url, e))

    urls = [
        "https://nhentai.net/g/361344/",
        "https://nhentai.net/g/576655/",
        "https://imgur.com/gallery/abcde",
        "https://danbooru.donmai.us/posts/12345",
    ]

    threads = [threading.Thread(target=run_worker, args=(u,)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread safety errors encountered: {errors}"


def test_gallerydl_warning_routed_to_verbose_tag():
    """Verifies that network/urllib warnings are captured by _GalleryDlLogHandler and routed to tracker without printing to stdout."""
    import logging
    from backends.gallerydl_backend import _GalleryDlBatchTracker, _GalleryDlLogHandler, _active_trackers
    from core.models import DownloadProgress

    captured_progress = []

    def cb(prog: DownloadProgress):
        captured_progress.append(prog)

    tracker = _GalleryDlBatchTracker(progress_callback=cb)
    _active_trackers.current = tracker

    handler = _GalleryDlLogHandler()
    logger = logging.getLogger("test_logger")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    try:
        logger.warning("[downloader.http][warning] Connection reset by peer, retrying in 5.0s")
        assert len(captured_progress) == 1
        assert "WARNING: Connection reset" in captured_progress[0].status_message
    finally:
        logger.removeHandler(handler)
        _active_trackers.current = None


def test_gallerydl_bunkr_album_folder_title_resolution():
    """Verifies that Bunkr albums resolve their folder/album name on Row 1 and individual filenames on Row 2."""
    from backends.gallerydl_backend import _GalleryDlBatchTracker
    from core.models import DownloadProgress

    captured = []

    def cb(prog: DownloadProgress):
        captured.append(prog)

    tracker = _GalleryDlBatchTracker(progress_callback=cb)

    # 1. Bunkr metadata passed to on_directory
    bunkr_meta = {
        "album_id": "BnSIi8je",
        "album_name": "faixa",
        "album_size": "100.56 MB",
        "count": 30,
        "category": "bunkr",
        "subcategory": "album",
    }
    tracker.on_directory(bunkr_meta)
    assert tracker.album_title == "faixa (BnSIi8je)"
    assert tracker.total_files == 30

    # 2. Start file transfer
    tracker.start(r"C:\downloads\gallery-dl\bunkr\faixa (BnSIi8je)\eromedia.app_video_1791562031346_zy5vbq0g3ec.mp4")
    tracker.progress(52000000, 26000000, 40000000)

    # The latest progress object should have:
    # Row 1 Title: faixa (BnSIi8je)
    # Row 2 Active file: eromedia.app_video_1791562031346_zy5vbq0g3ec.mp4
    last_prog = captured[-1]
    assert last_prog.current_file == "faixa (BnSIi8je)"
    assert last_prog.status_message == "eromedia.app_video_1791562031346_zy5vbq0g3ec.mp4"
    assert last_prog.total_files == 30







