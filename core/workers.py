"""Join blocking workers on cancellation before releasing shared state."""
import asyncio
import os
import signal
import subprocess


async def run_blocking(function, *args, cancel_event=None, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if cancel_event is not None:
            cancel_event.set()
        # Do not release a backend lock while its underlying worker is running.
        try:
            await asyncio.shield(worker)
        except Exception:
            pass
        raise


async def terminate_process_tree(proc):
    if proc.returncode is not None:
        return
    pid = getattr(proc, 'pid', None)
    if os.name == 'nt' and isinstance(pid, int):
        killer = await asyncio.create_subprocess_exec(
            'taskkill', '/PID', str(pid), '/T', '/F',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        await killer.wait()
    elif os.name != 'nt' and isinstance(pid, int):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
