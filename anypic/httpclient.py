"""HTTP fetching with automatic engine escalation.

Try plain ``requests`` first (fastest); when a host answers with a bot-block
(403/429/503 or a Cloudflare challenge page) escalate to ``cloudscraper`` and
finally to ``curl_cffi`` (Firefox TLS impersonation). The engine that worked
for a host is remembered so subsequent requests to that host go straight to
the winner.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import requests

from .config import Config

log = logging.getLogger("anypic.http")

_CHALLENGE_MARKERS = (
    "just a moment", "cf-chl", "challenge-platform", "attention required",
    "verify you are human", "checking your browser", "ddos by",
    "enable javascript and cookies", "requests from the same device",
)

_ENGINE_NAMES = ("requests", "cloudscraper", "curl_cffi")


class FetchError(Exception):
    pass


class _Resp:
    """Thin normalization wrapper so all engines expose the same surface."""

    def __init__(self, resp):
        self._resp = resp

    @property
    def status_code(self) -> int:
        return self._resp.status_code

    @property
    def headers(self):
        return self._resp.headers

    @property
    def content(self) -> bytes:
        return self._resp.content

    @property
    def text(self) -> str:
        return self._resp.text if hasattr(self._resp, "text") else self._resp.content.decode("utf-8", "replace")

    @property
    def url(self) -> str:
        return str(self._resp.url)

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        return self._resp.iter_content(chunk_size=chunk_size)

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass


class Fetcher:
    """Session-based fetcher with engine escalation and retries."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.timeout = cfg.timeout
        self.retries = cfg.retries
        self._ua = cfg.user_agent
        self._preferred: dict[str, str] = {}  # host -> engine name
        self._lock = threading.Lock()
        self._cloudscraper = None
        self._curl = None
        proxy = cfg.get("proxy")
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

        self._requests = requests.Session()
        self._requests.headers.update({"User-Agent": self._ua, "Accept-Language": "en-US,en;q=0.9"})
        if self._proxies:
            self._requests.proxies.update(self._proxies)

    # ------------------------------------------------------------------ helpers
    def _remember(self, host: str, engine: str) -> None:
        with self._lock:
            self._preferred[host] = engine

    def _engine_for(self, host: str) -> list[str]:
        with self._lock:
            preferred = self._preferred.get(host)
        if preferred is None:
            return list(_ENGINE_NAMES)
        return [preferred] + [e for e in _ENGINE_NAMES if e != preferred]

    def _cloudscraper_session(self):
        if self._cloudscraper is None:
            import cloudscraper  # optional dependency
            self._cloudscraper = cloudscraper.create_scraper(
                browser={"browser": "firefox", "platform": "windows", "desktop": True}
            )
            if self._proxies:
                self._cloudscraper.proxies.update(self._proxies)
        return self._cloudscraper

    def _curl_session(self):
        if self._curl is None:
            from curl_cffi import requests as curl_requests  # optional dependency
            self._curl = curl_requests
        return self._curl

    def _looks_like_challenge(self, resp: _Resp) -> bool:
        if resp.status_code in (403, 429, 503):
            return True
        ct = resp.headers.get("Content-Type", "")
        if "text/html" not in ct.lower() or len(resp.content) > 2_000_000:
            return False
        body = resp.content[:200_000].decode("utf-8", "replace").lower()
        return any(m in body for m in _CHALLENGE_MARKERS)

    def _raw_get(self, engine: str, url: str, *, headers: dict, timeout: float, stream: bool = False):
        if engine == "requests":
            return self._requests.get(url, headers=headers, timeout=timeout, stream=stream)
        if engine == "cloudscraper":
            return self._cloudscraper_session().get(url, headers=headers, timeout=timeout, stream=stream)
        r = self._curl_session().get(
            url, headers=headers, timeout=timeout, stream=stream, impersonate="firefox135",
            allow_redirects=True, proxies=self._proxies,
        )
        return r

    # ------------------------------------------------------------------- public
    def get(
        self, url: str, referer: str | None = None, stream: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> _Resp:
        """GET a URL, escalating engines on bot-blocks. Raises FetchError on hard failure."""
        host = urlsplit(url).netloc.lower()
        headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"}
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)

        last_err: Exception | None = None
        for engine in self._engine_for(host):
            for attempt in range(self.retries + 1):
                try:
                    resp = _Resp(self._raw_get(engine, url, headers=headers, timeout=self.timeout, stream=stream))
                    blocked = (
                        resp.status_code in (403, 429, 503)
                        if stream
                        else self._looks_like_challenge(resp)
                    )
                    if blocked:
                        log.debug("[%s] %s blocked (%s) — escalating", engine, host, resp.status_code)
                        resp.close()
                        break  # next engine
                    if resp.status_code >= 500:
                        raise requests.HTTPError(f"HTTP {resp.status_code}")
                    self._remember(host, engine)
                    return resp
                except Exception as e:  # noqa: BLE001 — transport errors differ per engine
                    last_err = e
                    log.debug("[%s] attempt %d for %s failed: %s", engine, attempt + 1, url, e)
                    time.sleep(min(2 ** attempt, 8))
        raise FetchError(f"all engines failed for {url}: {last_err}")

    def head(self, url: str, referer: str | None = None, timeout: float | None = None,
             max_engines: int = 3) -> _Resp | None:
        """HEAD with bounded cost: cap the timeout and/or try only the first
        engine when the caller just needs a quick hint (classification)."""
        host = urlsplit(url).netloc.lower()
        t = timeout if timeout is not None else self.timeout
        headers = {"Referer": referer} if referer else {}
        for engine in self._engine_for(host)[:max_engines]:
            try:
                if engine == "requests":
                    return _Resp(self._requests.head(url, headers=headers, timeout=t, allow_redirects=True))
                if engine == "cloudscraper":
                    return _Resp(self._cloudscraper_session().head(url, headers=headers, timeout=t, allow_redirects=True))
                return _Resp(self._curl_session().head(
                    url, headers=headers, timeout=t, impersonate="firefox135", proxies=self._proxies))
            except Exception as e:  # noqa: BLE001
                log.debug("[%s] HEAD %s failed: %s", engine, url, e)
        return None

    def fetch_bytes(self, url: str, referer: str | None = None, max_bytes: int = 131_072) -> bytes:
        """Fetch (at most) max_bytes of a body via a Range request, for probing.

        Servers that ignore Range will simply have the connection closed early.
        AVIF is excluded from Accept so CDNs serve formats Pillow can measure.
        """
        resp = self.get(
            url, referer=referer, stream=True,
            extra_headers={
                "Range": f"bytes=0-{max_bytes - 1}",
                "Accept": "image/jpeg,image/png,image/gif,image/webp,image/bmp,image/tiff,*/*;q=0.8",
            },
        )
        try:
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} for {url}")
            buf = bytearray()
            for chunk in resp.iter_content(65536):
                buf.extend(chunk)
                if len(buf) >= max_bytes:
                    break
            return bytes(buf[:max_bytes])
        finally:
            resp.close()

    def download_file(
        self, url: str, dest: Path, referer: str | None = None, max_bytes: int | None = None,
        on_progress=None,
    ) -> tuple[Path, int, str]:
        """Stream a URL to dest (``.part`` then atomic rename). Returns (final_path, size, md5).

        ``on_progress(delta_bytes)`` fires once per downloaded chunk so the
        caller can drive a live progress bar.
        """
        limit = max_bytes or self.cfg.max_bytes
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        md5 = hashlib.md5()
        total = 0

        resp = self.get(
            url, referer=referer, stream=True,
            extra_headers={
                "Accept": "image/jpeg,image/png,image/gif,image/webp,image/bmp,image/tiff,*/*;q=0.8",
            },
        )
        try:
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} for {url}")
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise FetchError(f"file exceeds {limit} bytes: {url}")
                    md5.update(chunk)
                    fh.write(chunk)
                    if on_progress:
                        on_progress(len(chunk))
        finally:
            resp.close()

        part.replace(dest)
        return dest, total, md5.hexdigest()
