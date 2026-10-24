"""
Loopback integration test: the real anypic pipeline (adapter, engine,
Fetcher, Downloader, hooks) against a local HTTP image page.

Exercises everything the stubbed tests cannot: live tier decisions,
HTTP probing with Pillow sniffing, real transfer_hook/batch_hook
firing, and slot-renderable progress events — with zero external
network dependency.
"""

import asyncio
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from backends.anypic_backend import AnyPicBackend
from core.models import DownloadTask

_JPEG_CACHE = {}


def _jpeg(key, w, h):
    if (key, w, h) not in _JPEG_CACHE:
        buf = io.BytesIO()
        # deterministic color per path (hash() is per-process random and can
        # collide, which would turn distinct photos into md5 dedup skips)
        Image.new("RGB", (w, h), color=(sum(key.encode()) % 255, 90, 120)).save(buf, format="JPEG")
        _JPEG_CACHE[(key, w, h)] = buf.getvalue()
    return _JPEG_CACHE[(key, w, h)]


class _ImagePageHandler(BaseHTTPRequestHandler):
    page = None  # set per test

    def log_message(self, *a):
        pass

    def _send(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _announce(self, length, ctype):
        """HEAD must advertise the true body length, like real image hosts."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(self.page.encode("utf-8"), "text/html")
        elif path.endswith(".jpg"):
            body = _jpeg(path, 120, 90) if "/thumb" in path else _jpeg(path, 900, 700)
            self._send(body, "image/jpeg")
        else:
            self.send_error(404)

    def do_HEAD(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._announce(len(self.page.encode("utf-8")), "text/html")
        elif path.endswith(".jpg"):
            data = _jpeg(path, 120, 90) if "/thumb" in path else _jpeg(path, 900, 700)
            self._announce(len(data), "image/jpeg")
        else:
            self.send_error(404)


@pytest.fixture()
def image_page_server():
    _ImagePageHandler.page = """<html><body>{imgs}</body></html>""".format(imgs="".join(
        f'<a href="/img/photo{n}.jpg"><img src="/img/thumb{n}.jpg" width="120"></a>'
        for n in range(4)
    ))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ImagePageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_anypic_download_over_loopback_streams_progress(tmp_path, image_page_server):
    # isolated engine state so the project-wide dedup db cannot interfere
    backend = AnyPicBackend({"config_file": str(tmp_path / "none.json"),
                             "data_dir": str(tmp_path / "anypic-data"), "workers": 2})
    events = []

    task = DownloadTask(url=image_page_server, backend="anypic-dl",
                        output_dir=str(tmp_path / "downloads"), options={})
    entry = asyncio.run(backend.download(task, progress_callback=events.append))

    assert entry.metadata["file_count"] == 4
    assert entry.file_size > 0

    # Probing stage: counter + bar source, no byte-field leakage
    probing = [e for e in events if e.phase == 'resolving' and 'Probing' in (e.status_message or '')]
    assert probing, "no probe events reached the progress callback"
    assert probing[-1].total_files == 8  # 4 originals + 4 thumbs grouped
    assert all(e.file_downloaded_bytes is None for e in probing)

    # Transfer stage: per-file bytes, batch totals, completed counter
    transfers = [e for e in events if e.phase == 'downloading']
    assert transfers
    assert all(e.total_files == 4 for e in transfers), "batch total never announced"
    assert transfers[-1].completed_files == 4
    assert any(e.file_total_bytes for e in transfers)
    assert sum(1 for e in transfers if e.file_downloaded_bytes == e.file_total_bytes) >= 4

    # Files landed in the flat layout with a page-slug folder
    out = tmp_path / "downloads"
    saved = list(out.rglob("*.jpg"))
    assert len(saved) == 4
    assert out.name == "downloads" and saved[0].parent.parent == out
