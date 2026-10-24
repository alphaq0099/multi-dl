"""Private gallery-dl process entry point; emits newline-delimited JSON events."""
import asyncio
from dataclasses import asdict
import json
import re
import sys
import threading
import hashlib
from pathlib import Path

from core.gallery_process import FRAME_LIMIT, json_default
from core.models import DownloadProgress, DownloadTask
from core.storage import async_file_lock


def main():
    transport = sys.stdout
    # Library messages and direct-backend fallback output must not corrupt IPC.
    sys.stdout = sys.stderr
    write_lock = threading.Lock()
    def emit(kind, **payload):
        line = json.dumps(dict(type=kind, **payload), default=json_default, ensure_ascii=True)
        if len(line.encode('utf-8')) >= FRAME_LIMIT:
            raise ValueError('Gallery worker event exceeds its message limit')
        with write_lock:
            transport.write(line + '\n')
            transport.flush()
    try:
        raw = sys.stdin.buffer.read(FRAME_LIMIT + 1)
        if len(raw) > FRAME_LIMIT:
            raise ValueError('Gallery worker request exceeds its message limit')
        request = json.loads(raw)
        from backends.gallerydl_backend import GalleryDlBackend
        backend = GalleryDlBackend(request['config'])
        operation = request['operation']
        if operation == 'info':
            result = asyncio.run(backend._extract_info_local(request['url']))
        elif operation == 'download':
            task = DownloadTask(**request['task'])
            def progress(update):
                emit('progress', data=asdict(update))
            async def download():
                identity = task.url + '\n' + str(Path(task.output_dir).resolve())
                key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
                lock = Path(__file__).resolve().parent.parent / 'data' / 'gallery_locks' / key
                def waiting():
                    progress(DownloadProgress(phase='waiting', status_message='Same URL already active; waiting for that job'))
                async with async_file_lock(lock, on_wait=waiting):
                    progress(DownloadProgress(phase='resolving', status_message='Fetching metadata'))
                    return await backend._download_local(task, progress_callback=progress)
            result = asdict(asyncio.run(download()))
        else:
            raise ValueError('Unsupported gallery worker operation')
        emit('result', data=result)
        return 0
    except Exception as exc:
        message = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@', r'\1***@', str(exc))
        emit('error', message=message or type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
