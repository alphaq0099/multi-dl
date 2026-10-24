"""A gallery job owns a subprocess; credentials travel through stdin, not argv."""
import asyncio
from collections import deque
from datetime import date, datetime
from enum import Enum
import json
import os
from pathlib import Path
import subprocess
import sys

from core.exceptions import DownloadFailedError
from core.models import ArchiveEntry, DownloadProgress, MediaType
from core.workers import terminate_process_tree

ROOT = Path(__file__).resolve().parent.parent
FRAME_LIMIT = 8 * 1024 * 1024


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f'Cannot serialize gallery worker value of type {type(value).__name__}')


async def run_gallery_process(config, operation, *, task=None, url=None, progress_callback=None):
    request = {'config': config, 'operation': operation, 'url': url}
    if task is not None:
        request['task'] = dict(url=task.url, backend=task.backend, output_dir=task.output_dir,
                               task_id=task.task_id, options=dict(task.options, _managed_failover=True))
    encoded = json.dumps(request, default=json_default).encode('utf-8')
    if len(encoded) > FRAME_LIMIT:
        raise DownloadFailedError('Gallery job configuration exceeds the worker message limit')
    if progress_callback:
        progress_callback(DownloadProgress(phase='waiting', status_message='Starting gallery worker'))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'core.gallery_worker', cwd=str(ROOT),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=FRAME_LIMIT, env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1'),
        **({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
           if os.name == 'nt' else {'start_new_session': True}))
    diagnostics = deque(maxlen=20)
    async def drain_stderr():
        # Drain in bounded chunks so library logging cannot fill the pipe or grow memory.
        while chunk := await proc.stderr.read(4096):
            diagnostics.append(chunk.decode('utf-8', errors='replace'))
    stderr_task = asyncio.create_task(drain_stderr())
    result = None
    received_result = False
    try:
        proc.stdin.write(encoded)
        await proc.stdin.drain()
        proc.stdin.close()
        while line := await proc.stdout.readline():
            message = json.loads(line)
            kind = message.get('type')
            if kind == 'progress':
                if progress_callback:
                    progress_callback(DownloadProgress(**message['data']))
            elif kind == 'result':
                if received_result:
                    raise DownloadFailedError('Gallery worker returned more than one result')
                result, received_result = message['data'], True
            elif kind == 'error':
                raise DownloadFailedError(message.get('message') or 'Gallery worker failed')
            else:
                raise DownloadFailedError('Gallery worker returned an invalid message')
        code = await proc.wait()
        if code != 0 or not received_result:
            # Raw stderr may contain private URLs/cookies; never dump it to the UI.
            raise DownloadFailedError(f'Gallery worker exited without a successful result (code {code})')
        if operation == 'info':
            return result
        result['media_type'] = MediaType(result['media_type'])
        result['downloaded_at'] = datetime.fromisoformat(result['downloaded_at'])
        return ArchiveEntry(**result)
    except (ValueError, TypeError, KeyError, BrokenPipeError) as exc:
        raise DownloadFailedError('Gallery worker communication failed') from exc
    finally:
        if proc.returncode is None:
            await terminate_process_tree(proc)
        await stderr_task
