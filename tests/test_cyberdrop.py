"""
Unit tests for CyberdropDlBackend and cyberdrop-dl URL routing.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backends.cyberdrop_backend import CyberdropDlBackend, _detect_media_type
from core.models import DownloadTask, MediaType
from core.router import URLRouter


def test_cyberdrop_can_handle():
    backend = CyberdropDlBackend()

    # Valid locker & forum links
    assert backend.can_handle("https://bunkr.si/a/12345") is True
    assert backend.can_handle("https://bunkr.is/v/sample") is True
    assert backend.can_handle("https://bunkr.cr/d/file") is True
    assert backend.can_handle("https://gofile.io/d/abcdef") is True
    assert backend.can_handle("https://pixeldrain.com/u/xyz123") is True
    assert backend.can_handle("https://catbox.moe/c/sample") is True
    assert backend.can_handle("https://files.catbox.moe/abc.mp4") is True
    assert backend.can_handle("https://coomer.su/onlyfans/user/123") is True
    assert backend.can_handle("https://kemono.su/patreon/user/456") is True
    assert backend.can_handle("https://simpcity.su/threads/sample.12345/") is True
    assert backend.can_handle("https://f95zone.to/threads/game.56789/") is True
    assert backend.can_handle("https://mega.nz/file/abc#xyz") is True
    assert backend.can_handle("https://erome.com/a/album123") is True
    assert backend.can_handle("https://fapello.com/model-name/") is True

    # Other non-cyberdrop domains
    assert backend.can_handle("https://youtube.com/watch?v=123") is False
    assert backend.can_handle("https://twitter.com/user/status/123") is False
    assert backend.can_handle("https://t.me/durov/123") is False
    assert backend.can_handle("https://terabox.com/s/1abc") is False


def test_cyberdrop_media_type_detection():
    assert _detect_media_type("photo.jpg") == MediaType.IMAGE
    assert _detect_media_type("image.png") == MediaType.IMAGE
    assert _detect_media_type("clip.mp4") == MediaType.VIDEO
    assert _detect_media_type("movie.mkv") == MediaType.VIDEO
    assert _detect_media_type("song.mp3") == MediaType.AUDIO
    assert _detect_media_type("track.flac") == MediaType.AUDIO
    assert _detect_media_type("archive.zip") == MediaType.DOCUMENT
    assert _detect_media_type("manual.pdf") == MediaType.DOCUMENT
    assert _detect_media_type("unknown.xyz") == MediaType.UNKNOWN


def test_cyberdrop_executable_resolution():
    backend = CyberdropDlBackend()
    exe = backend._resolve_executable()
    assert isinstance(exe, list)
    assert len(exe) >= 1
    # Either binary path or python -c invocation
    assert any("cyberdrop" in part.lower() for part in exe)


def test_cyberdrop_router_integration():
    backend = CyberdropDlBackend()
    router = URLRouter({"cyberdrop-dl": backend})

    # Route Bunkr
    routed_bunkr = router.route("https://bunkr.si/a/12345")
    assert routed_bunkr.name == "cyberdrop-dl"

    # Route Gofile
    routed_gofile = router.route("https://gofile.io/d/123")
    assert routed_gofile.name == "cyberdrop-dl"

    # Route Pixeldrain
    routed_pixel = router.route("https://pixeldrain.com/u/123")
    assert routed_pixel.name == "cyberdrop-dl"

    # Route Forum thread
    routed_forum = router.route("https://simpcity.su/threads/sample.123/")
    assert routed_forum.name == "cyberdrop-dl"

    # Host identifier normalizations
    assert URLRouter.get_host_identifier("https://bunkr.is/a/123", "cyberdrop-dl") == "bunkr"
    assert URLRouter.get_host_identifier("https://bunkr.cr/d/456", "cyberdrop-dl") == "bunkr"
    assert URLRouter.get_host_identifier("https://catbox.moe/c/789", "cyberdrop-dl") == "catbox"
    assert URLRouter.get_host_identifier("https://coomer.su/user/1", "cyberdrop-dl") == "coomer"
    assert URLRouter.get_host_identifier("https://kemono.su/user/2", "cyberdrop-dl") == "kemono"


@pytest.mark.asyncio
async def test_cyberdrop_failover_to_gallerydl(monkeypatch, tmp_path):
    backend = CyberdropDlBackend({"enable_failover_to_gallerydl": True})
    task = DownloadTask(
        url="https://kemono.su/patreon/user/123",
        backend="cyberdrop-dl",
        output_dir=str(tmp_path),
    )

    # Mock subprocess execution to simulate a 403 / anti-bot error
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout.readline = AsyncMock(side_effect=[
        b"INFO Scraping https://kemono.su/patreon/user/123\n",
        b"ERROR 403 Forbidden Cloudflare challenge failed\n",
        b"",
    ])
    mock_proc.wait = AsyncMock()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    # Mock GalleryDlBackend to verify failover was invoked
    mock_entry = MagicMock(file_path=str(tmp_path / "fallback.jpg"), backend="gallery-dl")
    mock_gdl_instance = MagicMock()
    mock_gdl_instance.can_handle.return_value = True
    mock_gdl_instance.download = AsyncMock(return_value=mock_entry)

    with patch("backends.gallerydl_backend.GalleryDlBackend", return_value=mock_gdl_instance):
        result = await backend.download(task)
        assert result.backend == "gallery-dl"
        mock_gdl_instance.download.assert_called_once()


@pytest.mark.asyncio
async def test_cyberdrop_download_progress_and_completion(monkeypatch, tmp_path):
    backend = CyberdropDlBackend()
    task = DownloadTask(
        url="https://gofile.io/d/sample123",
        backend="cyberdrop-dl",
        output_dir=str(tmp_path),
    )

    test_file = tmp_path / "sample_video.mp4"

    async def fake_wait():
        # Simulate file being written to disk by cyberdrop-dl
        test_file.write_bytes(b"A" * 1024)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(side_effect=[
        b"INFO Scraping https://gofile.io/d/sample123\n",
        b"INFO Download finished: https://gofile.io/d/sample123\n",
        b"",
    ])
    mock_proc.wait = AsyncMock(side_effect=fake_wait)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    progress_events = []
    def on_progress(p):
        progress_events.append(p)

    entry = await backend.download(task, progress_callback=on_progress)

    assert entry.file_name == "sample_video.mp4"
    assert entry.file_size == 1024
    assert len(progress_events) >= 1
    assert progress_events[-1].percent == 100.0
    assert progress_events[-1].downloaded_bytes == 1024


def test_cyberdrop_flatten_loose_files(tmp_path):
    backend = CyberdropDlBackend()
    site_dir = tmp_path / "bunkr"
    site_dir.mkdir()
    loose_dir = site_dir / "Loose Files (Bunkr)"
    loose_dir.mkdir()

    file1 = loose_dir / "clip1.mp4"
    file1.write_text("content1")
    file2 = loose_dir / "photo.jpg"
    file2.write_text("content2")

    backend._flatten_loose_files(site_dir)

    assert not loose_dir.exists()
    assert (site_dir / "clip1.mp4").exists()
    assert (site_dir / "photo.jpg").exists()
    assert (site_dir / "clip1.mp4").read_text() == "content1"


def test_cyberdrop_deduplicate_domain_folder(tmp_path):
    backend = CyberdropDlBackend()
    site_dir = tmp_path / "gofile"
    site_dir.mkdir()
    dup_dir = site_dir / "gofile"
    dup_dir.mkdir()
    album_dir = dup_dir / "album_123"
    album_dir.mkdir()
    file1 = album_dir / "video.mp4"
    file1.write_text("album content")

    backend._deduplicate_domain_folder(site_dir)

    assert not dup_dir.exists()
    assert (site_dir / "album_123").is_dir()
    assert (site_dir / "album_123" / "video.mp4").read_text() == "album content"


@pytest.mark.asyncio
async def test_cyberdrop_command_flags(monkeypatch, tmp_path):
    backend = CyberdropDlBackend({
        "subfolders": True,
        "include_domain_in_subfolders": False,
        "delete_empty_folders": True,
        "rate_limit": 15,
        "ignore_history": True,
        "ignore_hashes": True,
    })
    site_dir = tmp_path / "pixeldrain"
    task = DownloadTask(
        url="https://pixeldrain.com/u/sample",
        backend="cyberdrop-dl",
        output_dir=str(site_dir),
    )

    test_file = site_dir / "sample.mp4"

    executed_cmd = []

    async def fake_exec(*args, **kwargs):
        nonlocal executed_cmd
        executed_cmd = list(args)
        test_file.write_bytes(b"DATA")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline = AsyncMock(side_effect=[b""])
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await backend.download(task)

    assert "--download-folder" in executed_cmd
    assert str(site_dir) in executed_cmd
    assert "--subfolders" in executed_cmd
    assert "--subfolders.include.no-domain" in executed_cmd
    assert "--delete-empty-folders" in executed_cmd
    assert "--rate-limit" in executed_cmd
    assert "15" in executed_cmd


def test_router_gallerydl_primary_lockers():
    from backends.gallerydl_backend import GalleryDlBackend
    gdl = GalleryDlBackend()
    cdl = CyberdropDlBackend()
    router = URLRouter({"gallery-dl": gdl, "cyberdrop-dl": cdl})

    # Common lockers should route to gallery-dl
    assert router.route("https://bunkr.cr/a/sample").name == "gallery-dl"
    assert router.route("https://gofile.io/d/sample").name == "gallery-dl"
    assert router.route("https://pixeldrain.com/u/sample").name == "gallery-dl"
    assert router.route("https://catbox.moe/c/sample").name == "gallery-dl"
    assert router.route("https://coomer.su/onlyfans/user/sample").name == "gallery-dl"
    assert router.route("https://kemono.su/patreon/user/sample").name == "gallery-dl"

    # Non-gallerydl hosts should route to cyberdrop-dl
    assert router.route("https://saint.to/video/123").name == "cyberdrop-dl"
    assert router.route("https://mega.nz/file/123#abc").name == "cyberdrop-dl"


@pytest.mark.asyncio
async def test_cyberdrop_partial_failure_triggers_gallerydl_failover(monkeypatch, tmp_path):
    backend = CyberdropDlBackend({"enable_failover_to_gallerydl": True})
    task = DownloadTask(
        url="https://gofile.io/d/sample_album",
        backend="cyberdrop-dl",
        output_dir=str(tmp_path),
    )

    # Simulate 1 file succeeding, but errors detected on others
    test_file = tmp_path / "file1.mp4"

    async def fake_wait():
        test_file.write_bytes(b"DATA")

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(side_effect=[
        b"INFO Scraping https://gofile.io/d/sample_album\n",
        b"ERROR Download Failed: file 2\n",
        b"ERROR Download Failed: file 3\n",
        b"",
    ])
    mock_proc.wait = AsyncMock(side_effect=fake_wait)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    mock_gdl_instance = MagicMock()
    mock_gdl_instance.can_handle.return_value = True
    mock_entry = MagicMock(file_path=str(tmp_path / "fallback.mp4"), backend="gallery-dl")
    mock_gdl_instance.download = AsyncMock(return_value=mock_entry)

    with patch("backends.gallerydl_backend.GalleryDlBackend", return_value=mock_gdl_instance):
        result = await backend.download(task)
        assert result.backend == "gallery-dl"
        mock_gdl_instance.download.assert_called_once()



