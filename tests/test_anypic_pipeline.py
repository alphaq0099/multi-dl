"""
End-to-end offline test for the vendored anypic pipeline.

process_url runs for real (tier orchestration, probing, size filtering,
rich Progress rendering) against stubbed network objects, so a broken
engine integration surfaces here instead of on a live download.
"""

import io
from pathlib import Path

from anypic.config import load_config as anypic_load_config
from anypic.httpclient import FetchError
from anypic.models import DownloadResult
from anypic.pipeline import process_url


class _HeadResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class StubFetcher:
    """Serves one HTML page and one in-memory JPEG; everything else 404s."""

    def __init__(self, html: str, jpeg: bytes):
        self.html = html
        self.jpeg = jpeg

    def head(self, url, referer=None, timeout=None, max_engines=4):
        if url.endswith(".jpg"):
            return _HeadResp(200, {"Content-Type": "image/jpeg"})
        return _HeadResp(200, {"Content-Type": "text/html"})

    def get(self, url, referer=None, timeout=None):
        outer = self

        class _GetResp:
            status_code = 200
            headers = {"Content-Type": "text/html"}

            @property
            def text(self):
                return outer.html

            @property
            def content(self):
                return outer.html.encode("utf-8")

            def iter_content(self, chunk_size=65536):
                return iter(())

            def close(self):
                pass

        resp = _GetResp()
        resp.url = url
        return resp

    def fetch_bytes(self, url, referer=None, max_bytes=131072):
        return self.jpeg


class StubDownloader:
    """Records what the pipeline asked it to download; returns canned results."""

    def __init__(self):
        self.requests = []

    def download_many(self, page_url, probes, task_id=None, progress=None, sub_dir=None):
        self.requests.append((page_url, [p.url for p in probes], sub_dir))
        return [
            DownloadResult(url=p.url, status="downloaded", file=f"/out/{i}.jpg",
                           width=p.width, height=p.height, bytes=1000)
            for i, p in enumerate(probes)
        ]


def _make_jpeg(width=600, height=600) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(120, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_cfg(tmp_path):
    return anypic_load_config(overrides={
        "output_dir": str(tmp_path / "downloads"),
        "data_dir": str(tmp_path / "data"),
        "min_width": 250,
        "min_height": 250,
        "gallery_dl": {"enabled": False},
        "browser": {"enabled": False},
    }, project_root=tmp_path)


HTML = """
<html><body>
  <a href="https://cdn.examplehost.test/photos/scene.jpg">
    <img src="https://cdn.examplehost.test/photos/thumb_scene.jpg" width="300" height="200">
  </a>
</body></html>
"""


def test_process_url_end_to_end_offline(tmp_path):
    """Static tier: page HTML -> candidate -> probe -> downloader, with the
    rewrite rule upgrading the thumb URL to the linked original."""
    cfg = _make_cfg(tmp_path)
    fetcher = StubFetcher(HTML, _make_jpeg())
    downloader = StubDownloader()
    from anypic.resolver import Resolver
    resolver = Resolver(cfg, fetcher)

    url = "https://www.examplehost.test/gallery/becky-page.shtml"
    result = process_url(url, cfg, fetcher, downloader, resolver,
                         use_gallery_dl=False, allow_browser=False)

    assert result.tier == "static"
    assert result.found >= 1
    assert len(result.downloaded) >= 1
    # the anchor's original URL must be probed alongside the thumb variant
    probed = downloader.requests[0][1]
    assert any("scene.jpg" in u and "thumb_" not in u for u in probed)
    # sub-dir nesting: one folder per source page under the domain folder
    assert downloader.requests[0][2] == "becky-page.shtml"


def test_process_url_survives_unreachable_page(tmp_path):
    """A dead page must return a result with notes, never raise."""
    cfg = _make_cfg(tmp_path)

    class DeadFetcher:
        def head(self, url, referer=None, timeout=None, max_engines=4):
            return _HeadResp(404)

        def get(self, url, referer=None, timeout=None):
            raise FetchError("connection refused")

    from anypic.resolver import Resolver
    result = process_url(
        "https://dead-host.test/page", cfg, DeadFetcher(), StubDownloader(),
        Resolver(cfg, DeadFetcher()), use_gallery_dl=False, allow_browser=False)
    assert result.downloaded == []
    assert result.notes  # failure reasons recorded
