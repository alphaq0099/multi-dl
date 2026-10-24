"""Exercise real isolated gallery workers against a loopback-only media server."""
import asyncio
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from unittest.mock import AsyncMock, patch

import pytest

from backends.gallerydl_backend import GalleryDlBackend
from core.exceptions import DownloadFailedError
from core.models import DownloadTask

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9k8AAAAASUVORK5CYII=')


@pytest.fixture
def media_server():
    barrier = threading.Barrier(2, timeout=10)
    both_requested = threading.Event()
    hold_started = threading.Event()
    release = threading.Event()
    failures = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path.startswith('/parallel/'):
                    barrier.wait()
                    both_requested.set()
                if self.path.startswith('/hold/'):
                    hold_started.set()
                    release.wait(45)
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)
            except threading.BrokenBarrierError:
                failures.append('Gallery jobs were serialized')
                self.send_error(500)
            except OSError:
                pass  # Expected when a cancellation test closes the connection.
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', both_requested, hold_started, release, failures
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_backend(tmp_path, name):
    config_file = tmp_path / f'{name}.json'
    config_file.write_text(json.dumps({'extractor': {'directory': [], 'filename': name + '.{extension}'},
                                       'downloader': {'http': {'validate': False}}}))
    return GalleryDlBackend({'config_file': str(config_file), 'fallback_browser_cookies': None,
                             'enable_failover_to_cyberdrop': False, 'timeout': 3, 'retries': 0})


@pytest.mark.asyncio
async def test_real_workers_overlap_and_keep_configuration_private(tmp_path, media_server):
    base, both, _, _, failures = media_server
    first, second = make_backend(tmp_path, 'first'), make_backend(tmp_path, 'second')
    events = [[], []]
    tasks = [DownloadTask(url=base + f'/parallel/{name}.png', backend='gallery-dl',
                          output_dir=str(tmp_path / name), options={'no_archive': True, 'quiet': True})
             for name in ('first', 'second')]
    result = await asyncio.wait_for(asyncio.gather(
        first.download(tasks[0], progress_callback=events[0].append),
        second.download(tasks[1], progress_callback=events[1].append)), timeout=30)
    assert both.is_set(), failures
    assert not failures
    for index, name in enumerate(('first', 'second')):
        path = Path(result[index].file_path)
        assert path.name == name + '.png'
        assert path.is_relative_to(tmp_path / name)
        assert path.read_bytes() == PNG
        assert not list((tmp_path / name).rglob(('second' if index == 0 else 'first') + '.png'))
        assert {'waiting', 'resolving', 'downloading', 'processing'} <= {event.phase for event in events[index]}
        assert events[index][-1].completed_files == 1


@pytest.mark.asyncio
async def test_worker_cancellation_terminates_and_waits_for_process(tmp_path, media_server):
    import core.gallery_process as transport
    base, _, started, release, _ = media_server
    backend = make_backend(tmp_path, 'cancelled')
    request = DownloadTask(url=base + '/hold/image.png', backend='gallery-dl',
                           output_dir=str(tmp_path / 'cancelled'), options={'no_archive': True, 'quiet': True})
    create = asyncio.create_subprocess_exec
    children = []
    async def capture(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        return child
    with patch.object(transport.asyncio, 'create_subprocess_exec', side_effect=capture):
        task = asyncio.create_task(backend.download(request))
        try:
            assert await asyncio.to_thread(started.wait, 12)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
            assert children[0].returncode is not None
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert not list((tmp_path / 'cancelled').rglob('*.png'))


@pytest.mark.asyncio
async def test_parent_can_fail_over_after_worker_error(tmp_path):
    backend = GalleryDlBackend({'enable_failover_to_cyberdrop': True})
    request = DownloadTask(url='https://example.invalid/album', backend='gallery-dl', output_dir=str(tmp_path))
    alternative = AsyncMock()
    alternative.name = 'cyberdrop-dl'
    alternative.can_handle = lambda url: True
    alternative.download.return_value = 'fallback-result'
    with patch('backends.gallerydl_backend.run_gallery_process', AsyncMock(side_effect=DownloadFailedError('worker failed'))), patch('backends.cyberdrop_backend.CyberdropDlBackend', return_value=alternative):
        assert await backend.download(request) == 'fallback-result'
    assert alternative.download.call_args.args[0].options['_is_failover'] is True


@pytest.mark.asyncio
async def test_same_url_workers_wait_without_writing_the_same_file_together(tmp_path, media_server):
    base, _, started, release, _ = media_server
    backend = make_backend(tmp_path, 'shared')
    backend.config['timeout'] = 30
    request = DownloadTask(url=base + '/hold/image.png', backend='gallery-dl',
                           output_dir=str(tmp_path / 'shared'), options={'no_archive': True, 'quiet': True})
    waiting = asyncio.Event()
    def on_progress(event):
        if event.phase == 'waiting' and 'Same URL already active' in (event.status_message or ''):
            waiting.set()
    first = asyncio.create_task(backend.download(request))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 12)
        second = asyncio.create_task(backend.download(request, progress_callback=on_progress))
        await asyncio.wait_for(waiting.wait(), timeout=12)
        assert not second.done()
        release.set()
        one, two = await asyncio.wait_for(asyncio.gather(first, second), timeout=15)
        assert one.file_path == two.file_path
        assert two.metadata['all_skipped'] is True
        assert Path(one.file_path).read_bytes() == PNG
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)
