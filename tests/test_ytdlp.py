import pytest
import os
from pathlib import Path
from backends.ytdlp_backend import YtDlpBackend
from core.models import DownloadTask


@pytest.mark.asyncio
async def test_ytdlp_can_handle():
    backend = YtDlpBackend()
    assert backend.can_handle("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is True
    assert backend.can_handle("https://youtu.be/dQw4w9WgXcQ") is True
    assert backend.can_handle("https://vimeo.com/111111") is True
    # Should not handle telegram
    assert backend.can_handle("https://t.me/c/12345/678") is False


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('RUN_NETWORK_TESTS') != '1', reason='Set RUN_NETWORK_TESTS=1 to enable live YouTube integration tests')
async def test_ytdlp_extract_info():
    # Test metadata extraction on a short reliable test video
    backend = YtDlpBackend()
    test_url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"  # Big Buck Bunny clip
    try:
        info = await backend.extract_info(test_url)
        assert info is not None
        assert "title" in info
        assert "id" in info
    except (OSError, TimeoutError) as e:
        pytest.skip(f"Network unavailable during test: {e}")


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('RUN_NETWORK_TESTS') != '1', reason='Set RUN_NETWORK_TESTS=1 to enable live YouTube integration tests')
async def test_ytdlp_download_and_archive(tmp_path):
    # Test downloading to a temporary directory
    backend = YtDlpBackend({
        "format": "worst",  # Smallest download for testing
        "merge_output_format": "mp4",
        "embed_metadata": False,
        "embed_thumbnail": False,
    })
    
    test_url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    task = DownloadTask(
        url=test_url,
        backend=backend.name,
        output_dir=str(tmp_path),
    )

    try:
        entry = await backend.download(task)
        assert entry is not None
        assert entry.file_path is not None
        assert Path(entry.file_path).exists()
        assert Path(entry.file_path).stat().st_size > 0
        assert entry.backend == "yt-dlp"
    except (OSError, TimeoutError) as e:
        pytest.skip(f"Network unavailable during download test: {e}")


def test_ytdlp_waf_error_detection():
    assert YtDlpBackend._is_waf_or_tls_error("ConnectionResetError: [WinError 10054] An existing connection was forcibly closed") is True
    assert YtDlpBackend._is_waf_or_tls_error("Unable to download JSON metadata: Connection aborted") is True
    assert YtDlpBackend._is_waf_or_tls_error("Cloudflare challenge requested") is True
    assert YtDlpBackend._is_waf_or_tls_error("Normal video format not found") is False


def test_ytdlp_impersonate_target_resolution():
    target = YtDlpBackend._resolve_impersonate_target("chrome")
    assert target is not None
    assert str(target).lower().startswith("chrome")

    target_ff = YtDlpBackend._resolve_impersonate_target("firefox")
    assert target_ff is not None
    assert str(target_ff).lower().startswith("firefox")

    assert YtDlpBackend._resolve_impersonate_target(None) is None


def test_ytdlp_cam4_can_handle():
    backend = YtDlpBackend()
    assert backend.can_handle("https://www.cam4.com/corierando") is True
