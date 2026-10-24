"""
Unit tests for AnyPicBackend: engine ownership, config bridging, and
PageResult -> ArchiveEntry mapping (pipeline stubbed; no network).
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from backends.anypic_backend import AnyPicBackend
from anypic.models import DownloadResult, PageResult
from core.exceptions import DownloadFailedError
from core.models import DownloadTask


def make_backend(tmp_path, **extra):
    return AnyPicBackend({"config_file": str(tmp_path / "nonexistent.json"), **extra})


def make_task(tmp_path, url="https://some-random-blog.example/2026/10/photo-dump"):
    return DownloadTask(url=url, backend="anypic-dl", output_dir=str(tmp_path / "downloads"))


def page_result(tier="static", downloaded=1, skipped=0, notes=None):
    result = PageResult(url="https://some-random-blog.example/2026/10/photo-dump", tier=tier)
    for i in range(downloaded):
        result.results.append(DownloadResult(
            url=f"https://cdn.example/img{i}.jpg", status="downloaded",
            file=f"/out/some-random-blog.example/photo-dump/img{i}.jpg",
            width=1200, height=800, bytes=10_000 + i,
        ))
    for i in range(skipped):
        result.results.append(DownloadResult(
            url=f"https://cdn.example/s{i}.jpg", status="skipped-url",
            note=f"already saved as /out/some-random-blog.example/photo-dump/s{i}.jpg",
        ))
    if notes:
        result.notes.extend(notes)
    return result


# --------------------------------------------------------------------- ownership

def test_can_handle_claims_only_unowned_urls():
    backend = make_backend(Path("/tmp"))

    # A plain unknown page with no gallery-dl / yt-dlp extractor: claimed.
    with patch("gallery_dl.extractor.find", return_value=None), \
         patch("backends.ytdlp_backend.YtDlpBackend.can_handle", return_value=False):
        assert backend.can_handle("https://some-random-blog.example/post") is True

    # Native gallery-dl extractor present: gallery-dl owns it.
    with patch("gallery_dl.extractor.find", return_value=object()):
        assert backend.can_handle("https://some-random-blog.example/post") is False

    # yt-dlp extractor present: yt-dlp owns it.
    with patch("gallery_dl.extractor.find", return_value=None), \
         patch("backends.ytdlp_backend.YtDlpBackend.can_handle", return_value=True):
        assert backend.can_handle("https://some-random-blog.example/post") is False

    # Router domain map (telegram/terabox/lockers/known sites): never claimed.
    assert backend.can_handle("https://t.me/somechannel") is False
    assert backend.can_handle("https://teraboxlink.com/s/1abc") is False
    assert backend.can_handle("https://bunkr.si/a/album") is False

    # Non-http(s) schemes and bare hosts: never claimed.
    assert backend.can_handle("tg://resolve?domain=x") is False
    assert backend.can_handle("ftp://files.example/x") is False


def test_router_registers_anypic_last():
    from core.router import URLRouter
    backend = make_backend(Path("/tmp"))
    router = URLRouter({"anypic-dl": backend})
    assert list(router.backends)[-1] == "anypic-dl"


def test_get_backend_config_provides_anypic_section():
    from core.config import ConfigManager
    cm = ConfigManager()
    cfg = cm.get_backend_config("anypic-dl")
    assert cfg.get("min_width") == 250
    assert cfg.get("config_file") == "configs/anypic-dl.json"
    assert cm.engine_limits.get("anypic-dl") == 2


# ------------------------------------------------------------------- mapping

def test_entry_mapping_single_file(tmp_path):
    backend = make_backend(tmp_path)
    entry = backend._entry_from_result(make_task(tmp_path), page_result(downloaded=1))
    assert entry.backend == "anypic-dl"
    assert entry.media_type.value == "image"
    assert entry.file_name == "img0.jpg"
    assert entry.file_size == 10_000
    assert entry.metadata["file_count"] == 1
    assert entry.metadata["all_skipped"] is False


def test_entry_mapping_multi_file_uses_page_folder(tmp_path):
    backend = make_backend(tmp_path)
    entry = backend._entry_from_result(make_task(tmp_path), page_result(downloaded=3, skipped=1))
    assert entry.file_name == "photo-dump"          # page slug folder
    assert entry.file_size == 10_000 + 10_001 + 10_002
    assert entry.metadata["skipped_count"] == 1
    assert entry.metadata["skipped_files"] == ["/out/some-random-blog.example/photo-dump/s0.jpg"]


def test_entry_mapping_all_skipped(tmp_path):
    backend = make_backend(tmp_path)
    entry = backend._entry_from_result(make_task(tmp_path), page_result(downloaded=0, skipped=2))
    assert entry.metadata["all_skipped"] is True
    assert entry.file_size == 0


def test_entry_mapping_nothing_found_raises(tmp_path):
    backend = make_backend(tmp_path)
    result = page_result(downloaded=0, skipped=0, notes=["static fetch: HTTP 404", "browser: playwright not installed"])
    with pytest.raises(DownloadFailedError):
        backend._entry_from_result(make_task(tmp_path), result)


def test_direct_tier_has_no_page_slug_folder(tmp_path):
    backend = make_backend(tmp_path)
    task = make_task(tmp_path)
    result = page_result(tier="direct", downloaded=1)
    entry = backend._entry_from_result(task, result)
    # direct files live at <out>/<domain>/, not nested under the page slug
    assert Path(entry.file_path).name == "img0.jpg"


# ---------------------------------------------------------------- config bridge

def test_config_bridge_forces_gallery_dl_off(tmp_path):
    backend = make_backend(tmp_path, min_width=400, best_only=5, proxy="http://127.0.0.1:2080")
    cfg = backend._build_anypic_config(make_task(tmp_path))
    assert cfg.min_width == 400
    assert cfg.best_only == 5
    assert cfg.proxy == "http://127.0.0.1:2080"
    assert cfg.gallery_dl["enabled"] is False
    assert cfg.get("layout") == "flat"   # dispatcher already provides the host layer
    assert cfg.browser["enabled"] is True
    assert str(cfg.data_dir).endswith("data\\anypic") or str(cfg.data_dir).endswith("data/anypic")


def test_progress_hooks_emit_updates(tmp_path):
    """Probe and transfer events surface as DownloadProgress updates."""
    backend = make_backend(tmp_path)
    events = []
    probe, transfer = backend._make_progress_hooks(events.append)

    probe(2, 7)
    assert events[-1].phase == 'resolving'
    assert '2/7' in events[-1].status_message

    class FakeInfo:
        url = "https://cdn.example/img.jpg"
        length = 100

    transfer(FakeInfo(), 40)
    transfer(FakeInfo(), 60)
    transfer(FakeInfo(), 0, done=True)
    last = events[-1]
    assert last.phase == 'downloading'
    assert last.file_name == "img.jpg"
    assert last.file_downloaded_bytes == 100
    assert last.file_total_bytes == 100


def test_transfer_hook_fires_from_engine_downloader(tmp_path):
    """The real Downloader calls transfer_hook per chunk and once when done."""
    from anypic.config import load_config as anypic_load_config
    from anypic.downloader import Downloader
    from anypic.models import ProbeInfo

    class _Store:
        def get_by_url(self, url):
            return None

        def get_by_md5(self, md5):
            return None

        def record(self, *a):
            pass

    class _Fetch:
        def download_file(self, url, dest, referer=None, max_bytes=None, on_progress=None):
            dest.write_bytes(b"x" * 50)
            if on_progress:
                on_progress(25)
                on_progress(25)
            return Path(dest), 50, "deadbeef"

    cfg = anypic_load_config(overrides={
        "output_dir": str(tmp_path / "out"),
        "data_dir": str(tmp_path / "data"),
        "layout": "flat",
        "dedup": False,
    }, project_root=tmp_path)

    hook_events = []
    dl = Downloader(cfg, fetcher=_Fetch(), state=_Store(),
                    transfer_hook=lambda info, delta, done=False: hook_events.append((info.url, delta, done)))
    info = ProbeInfo(url="https://cdn.example/pic.jpg", ok=True, content_type="image/jpeg",
                     width=600, height=600, length=50)

    class _FetchBytes:
        def fetch_bytes(self, url, referer=None, max_bytes=None):
            return _make_probe_bytes()

    results = dl.download_many("https://page.example/post", [info])
    assert len(results) == 1 and results[0].status == "downloaded"
    deltas = [e for e in hook_events if e[2] is False]
    dones = [e for e in hook_events if e[2] is True]
    assert sum(d for _, d, _ in deltas) == 50
    assert len(dones) == 1 and dones[0][0] == info.url
    # flat layout: file written directly under output_dir, no domain folder
    assert (tmp_path / "out" / "pic.jpg").exists()


def _make_probe_bytes() -> bytes:
    import io as _io
    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new("RGB", (600, 600)).save(buf, format="JPEG")
    return buf.getvalue()


def test_config_bridge_disables_browser(tmp_path):
    backend = make_backend(tmp_path, browser_enabled=False)
    cfg = backend._build_anypic_config(make_task(tmp_path))
    assert cfg.browser["enabled"] is False


def test_download_raises_wrapped_failure(tmp_path):
    backend = make_backend(tmp_path)
    with patch("backends.anypic_backend._anypic_process_url", side_effect=RuntimeError("boom")):
        with pytest.raises(DownloadFailedError, match="boom"):
            asyncio.run(backend.download(make_task(tmp_path)))


def test_quiet_console_is_engine_default():
    """The vendored engine must render into the quiet sink, never the terminal."""
    from anypic import report
    assert report.console is report.quiet_console
