"""Tier 1: hand supported URLs to gallery-dl.

Runs the user's gallery-dl executable with their own config (Firefox
cookies, per-site archives, postprocessors). When gallery-dl reports the
URL as unsupported (exit code 64 / "No extractor"), the pipeline falls
through to the generic engine.

The subprocess runs with a live status line (files so far / elapsed), so
slow configs (sleep between requests, many retries) are visible instead
of looking hung; ``-v`` streams gallery-dl's output directly.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from . import report as _report

log = logging.getLogger("anypic.gdl")

# gallery-dl exit codes (see docs): 64 = unsupported URL / no extractor found
EXIT_UNSUPPORTED = 64

_UNSUPPORTED_MARKERS = ("no extractor", "unsupported url", "unsupported url format")


def native_extractor_category(url: str) -> str | None:
    """Classify a URL with gallery-dl's own matcher.

    Returns the extractor category (e.g. "pixiv"), or None when no *native*
    extractor matches. Uses the Python API so the ytdl-generic fallback in
    the user's config never kicks in for URLs we want to handle ourselves.
    Returns "unknown" when the gallery_dl package can't be consulted.
    """
    try:
        from gallery_dl.extractor import find
    except ImportError:
        return "unknown"
    try:
        ext = find(url)
    except Exception:  # noqa: BLE001
        return "unknown"
    return None if ext is None else (getattr(ext, "category", "?") or "?")


def _available(cfg: Config) -> tuple[str | None, str | None]:
    g = cfg.gallery_dl
    exe = g.get("executable") if isinstance(g, dict) else None
    conf = g.get("config") if isinstance(g, dict) else None
    if not exe or not Path(exe).exists():
        return None, None
    return exe, conf


def probe_supported(cfg: Config) -> bool:
    """True when the gallery-dl executable exists and runs."""
    exe, _ = _available(cfg)
    if exe is None:
        log.warning("gallery-dl executable not found; tier 1 disabled")
        return False
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("gallery-dl not runnable: %s", e)
        return False


def run(url: str, cfg: Config) -> tuple[bool, str]:
    """Try to download *url* with gallery-dl.

    Returns (handled, message): ``handled=False`` means "unsupported here,
    fall through to the generic engine".
    """
    exe, conf = _available(cfg)
    if exe is None:
        return False, "gallery-dl unavailable"
    if not cfg.gallery_dl.get("enabled", True):
        return False, "gallery-dl disabled in config"

    category = native_extractor_category(url)
    if category is None:
        return False, "no gallery-dl extractor for this URL"

    cmd = [exe, "--no-color", "--ignore-config"]
    if conf and Path(conf).exists():
        cmd += ["--config", conf]
    cmd.append(url)

    log.info("gallery-dl (%s): %s", category or "?", " ".join(cmd))
    verbose = logging.getLogger("anypic").isEnabledFor(logging.DEBUG)
    timeout_s = cfg.gallery_dl.get("timeout", 3600)
    started = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as e:
        return False, f"could not run gallery-dl: {e}"

    stderr_buf: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_buf.append(line)

    threading.Thread(target=_drain_stderr, daemon=True).start()

    killed = threading.Event()

    def _watchdog() -> None:
        killed.set()
        proc.kill()

    watchdog = threading.Timer(timeout_s, _watchdog)
    watchdog.daemon = True
    watchdog.start()

    files = 0
    status = None
    if not verbose:
        status = _report.console.status(
            f"gallery-dl [{category}] running — your config sleeps between requests; Ctrl+C to cancel")
        status.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.strip() and not line.startswith("#"):
                files += 1
            if verbose:
                print(f"  gdl| {line.rstrip()}")
            elif status is not None:
                status.update(
                    f"gallery-dl [{category}] running… {files} file(s), "
                    f"{int(time.monotonic() - started)}s elapsed — Ctrl+C to cancel")
        proc.wait(timeout=60)
    except KeyboardInterrupt:
        proc.kill()
        proc.wait(timeout=30)
        raise
    finally:
        watchdog.cancel()
        if status is not None:
            status.stop()

    elapsed = int(time.monotonic() - started)
    err = "".join(stderr_buf).lower()
    if killed.is_set():
        return True, (f"gallery-dl [{category}] killed after {timeout_s}s timeout "
                      f"({files} file(s) before then; raise gallery_dl.timeout in config.json if needed)")

    if proc.returncode == EXIT_UNSUPPORTED or any(m in err for m in _UNSUPPORTED_MARKERS):
        return False, "no gallery-dl extractor for this URL"

    if proc.returncode == 0:
        return True, f"gallery-dl [{category or '?'}] OK ({files} file(s), {elapsed}s)"
    # Any other non-zero code means gallery-dl *did* recognize the URL but
    # something failed (HTTP errors, partial downloads, auth). Do not fall
    # through — the generic engine would duplicate a supported site.
    tail = [l for l in "".join(stderr_buf).strip().splitlines() if l.strip()][-3:]
    return True, f"gallery-dl [{category or '?'}] exit {proc.returncode} after {elapsed}s: " + " | ".join(tail)[:300]
