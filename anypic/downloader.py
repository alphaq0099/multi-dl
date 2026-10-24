"""Concurrent image downloads with URL + content-hash deduplication.

State lives in ``data/state.sqlite``:

* ``downloads`` — one row per saved file; ``image_url`` unique, ``md5``
  indexed, so re-running a page (or hitting the same image on another page)
  skips instantly.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import Config
from .httpclient import Fetcher, FetchError
from .models import DownloadResult, ProbeInfo
from rich.markup import escape

log = logging.getLogger("anypic.download")

_SANITIZE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FORMAT_EXT = {
    "JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif",
    "BMP": "bmp", "TIFF": "tiff", "AVIF": "avif", "MPO": "jpg",
}


class StateStore:
    """Thread-safe wrapper around the SQLite dedup database."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, page_url TEXT, image_url TEXT UNIQUE, md5 TEXT,
                    file TEXT, width INTEGER, height INTEGER, bytes INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_dl_md5 ON downloads(md5);
                """
            )
            self._conn.commit()

    def get_by_url(self, url: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM downloads WHERE image_url = ?", (url,))
            return cur.fetchone()

    def get_by_md5(self, md5: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM downloads WHERE md5 = ? LIMIT 1", (md5,))
            return cur.fetchone()

    def record(self, page_url: str, image_url: str, md5: str, file: str,
               width: int, height: int, nbytes: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO downloads (ts, page_url, image_url, md5, file, width, height, bytes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), page_url, image_url, md5,
                 file, width, height, nbytes),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Downloader:
    def __init__(self, cfg: Config, fetcher: Fetcher, state: StateStore, progress=None,
                 transfer_hook=None):
        self.cfg = cfg
        self.fetcher = fetcher
        self.state = state
        self.progress = progress  # rich Progress task (optional)
        # transfer_hook(info: ProbeInfo, delta: int, done: bool = False) fires
        # per downloaded chunk and once per finished file; embedders (e.g. the
        # MULTI_DOWNLOADER adapter) use it to mirror live progress. All hook
        # errors are swallowed — display only.
        self.transfer_hook = transfer_hook
        self._manifest_lock = threading.Lock()
        self._naming_lock = threading.Lock()
        self._taken_names: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ helpers
    def _filename(self, url: str, fmt_hint: str) -> str:
        path = unquote(urlsplit(url).path)
        base = Path(path).name or "image"
        base = _SANITIZE_RE.sub("_", base).strip("._") or "image"
        stem, dot, ext = base.rpartition(".")
        if not dot or len(ext) > 5:
            stem, ext = base, (fmt_hint.lower() if fmt_hint else "jpg")
        elif fmt_hint and fmt_hint.lower() != ext.lower():
            # trust actual bytes over a lying URL extension
            ext = fmt_hint.lower()
        name = f"{stem[:140]}.{ext}"
        return name

    def _unique_path(self, directory: Path, name: str) -> Path:
        with self._naming_lock:
            candidate = directory / name
            n = 1
            while (
                candidate.exists()
                or (str(directory), candidate.name) in self._taken_names
                or candidate.with_suffix(candidate.suffix + ".part").exists()
            ):
                stem, _, ext = name.rpartition(".")
                candidate = directory / f"{stem[:130]}-{n}.{ext}"
                n += 1
            self._taken_names.add((str(directory), candidate.name))
            return candidate

    def _append_manifest(self, row: dict) -> None:
        import csv
        is_new = not self.cfg.manifest_csv.exists() or self.cfg.manifest_csv.stat().st_size == 0
        with self._manifest_lock:
            self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cfg.manifest_csv, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=[
                    "ts", "page_url", "url", "file", "width", "height", "bytes", "md5", "status"])
                if is_new:
                    w.writeheader()
                w.writerow(row)

    # ------------------------------------------------------------------ worker
    def _download_one(self, page_url: str, info: ProbeInfo,
                      on_progress=None, sub_dir: str | None = None) -> DownloadResult:
        url = info.url
        res = DownloadResult(url=url)

        if self.cfg.dedup:
            existing = self.state.get_by_url(url)
            if existing:
                res.status = "skipped-url"
                res.note = f"already saved as {existing['file']}"
                res.width, res.height, res.bytes = existing["width"], existing["height"], existing["bytes"]
                return res

        # layout "domain" nests under a per-domain folder (standalone default);
        # "flat" writes straight into output_dir — for embedders whose host
        # layer is already provided by the orchestrating dispatcher.
        directory = self.cfg.output_dir
        if (self.cfg.get("layout") or "domain") != "flat":
            directory = directory / _domain_dir(page_url)
        if sub_dir:
            # one folder per source page keeps different links' files apart
            directory = directory / sub_dir
        directory.mkdir(parents=True, exist_ok=True)
        fmt_hint = _FORMAT_EXT.get((info.note or "").strip().upper(), "")
        name = self._filename(url, fmt_hint)
        dest = self._unique_path(directory, name)

        try:
            path, nbytes, md5 = self.fetcher.download_file(
                url, dest, referer=page_url, max_bytes=self.cfg.max_bytes,
                on_progress=on_progress)
        except FetchError as e:
            res.status = "failed"
            res.note = str(e)[:200]
            return res

        if self.cfg.dedup:
            dup = self.state.get_by_md5(md5)
            if dup:
                path.unlink(missing_ok=True)
                res.status = "skipped-dup"
                res.note = f"identical to {dup['file']}"
                res.bytes = nbytes
                return res

        # post-download validation: real dimensions, real format, real image
        w, h, magic_ct = _inspect_file(path)
        if w == 0 and h == 0 and magic_ct == "" and not info.content_type.startswith("image/"):
            path.unlink(missing_ok=True)
            res.status = "failed"
            res.note = "downloaded body is not an image"
            return res
        if w and h and w < self.cfg.relaxed_min and h < self.cfg.relaxed_min:
            path.unlink(missing_ok=True)
            res.status = "too-small"
            res.note = f"{w}x{h} — below relaxed minimum {self.cfg.relaxed_min}px"
            res.width, res.height = w, h
            return res
        # fix a lying URL extension when the real format is known
        real_ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                    "image/gif": "gif", "image/avif": "avif", "image/heic": "heic",
                    "image/bmp": "bmp", "image/tiff": "tiff"}.get(magic_ct, "")
        if real_ext and path.suffix.lower().lstrip(".") != real_ext:
            fixed = self._unique_path(path.parent, f"{path.stem[:140]}.{real_ext}")
            path.replace(fixed)
            path = fixed

        width, height = (w, h) if w else (info.width, info.height)
        self.state.record(page_url, url, md5, str(path), width, height, nbytes)
        self._append_manifest({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "page_url": page_url, "url": url,
            "file": str(path), "width": width, "height": height,
            "bytes": nbytes, "md5": md5, "status": "downloaded",
        })
        res.status = "downloaded"
        res.file = str(path)
        res.width, res.height, res.bytes = width, height, nbytes
        return res

    # ------------------------------------------------------------------ public
    def download_many(self, page_url: str, probes: list[ProbeInfo],
                      task_id=None, progress=None,
                      sub_dir: str | None = None) -> list[DownloadResult]:
        """Download probes concurrently.

        When *progress* (a rich Progress) is given, one sub-task per file
        shows byte-level fill (total = the Content-Length learned while
        probing), removed again on completion; *task_id* is advanced by
        downloaded bytes so the overall bar reports real transfer speed.
        *sub_dir* (per-source-page folder) nests under the domain folder.
        """
        if not probes:
            return []
        results: list[DownloadResult] = []
        done = 0
        with ThreadPoolExecutor(max_workers=min(self.cfg.workers, len(probes))) as pool:
            futures = {}
            for p in probes:
                sub, cb = None, None
                if progress is not None:
                    name = unquote(urlsplit(p.url).path).rsplit("/", 1)[-1][:32] or "image"
                    sub = progress.add_task(escape(name), total=(p.length or None))

                if progress is not None or self.transfer_hook:
                    def _cb(delta, _sub=sub, _p=p):
                        if _sub is not None:
                            progress.advance(_sub, delta)
                            if task_id is not None:
                                progress.advance(task_id, delta)
                        if self.transfer_hook:
                            try:
                                self.transfer_hook(_p, delta)
                            except Exception:
                                pass

                    cb = _cb
                futures[pool.submit(self._download_one, page_url, p, cb, sub_dir)] = (p, sub)
            for fut in as_completed(futures):
                p, sub = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = DownloadResult(url=p.url, status="failed", note=str(e)[:200])
                results.append(res)
                if self.transfer_hook:
                    try:
                        self.transfer_hook(p, 0, done=True)
                    except Exception:
                        pass
                if progress is not None:
                    if sub is not None:
                        progress.remove_task(sub)
                    done += 1
                    if task_id is not None:
                        progress.update(task_id, description=f"Downloading… {done}/{len(probes)}")
        return results


def _domain_dir(page_url: str) -> str:
    host = urlsplit(page_url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return _SANITIZE_RE.sub("_", host) or "unknown"


def _page_slug(page_url: str) -> str:
    """Folder name for one page's downloads: its last non-empty path segment.

    ``https://telegra.ph/some-post-09-10`` → ``some-post-09-10``. Root URLs
    (no path) fall back to a timestamp so distinct visits never merge.
    """
    path = unquote(urlsplit(page_url).path)
    for seg in reversed(path.split("/")):
        slug = _SANITIZE_RE.sub("_", seg).strip(" ._")[:60]
        if slug:
            return slug
    return time.strftime("%Y%m%d-%H%M%S")


def _inspect_file(path: Path) -> tuple[int, int, str]:
    """Read the head of a saved file; return (width, height, guessed content type)."""
    from .resolver import _guess_content_type, _sniff
    try:
        with open(path, "rb") as fh:
            head = fh.read(262_144)
        w, h, fmt = _sniff(head)
        ct = _guess_content_type(head, fmt)
        return w, h, ct
    except OSError:
        return 0, 0, ""
