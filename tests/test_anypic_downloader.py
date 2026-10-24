"""Regression tests for the downloader's naming/dedup plumbing.

Run:  python tests/test_downloader.py
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anypic.downloader import Downloader, _domain_dir

_SANITIZE_CHECK_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class _NullStore:
    def get_by_url(self, url):
        return None

    def get_by_md5(self, md5):
        return None

    def record(self, *a, **k):
        pass


def make_dl(tmp: Path) -> Downloader:
    from anypic.config import Config

    cfg = Config({"output_dir": str(tmp), "data_dir": str(tmp), "dedup": False,
                  "max_bytes": 1 << 20}, tmp)
    return Downloader(cfg, fetcher=None, state=_NullStore())  # type: ignore[arg-type]


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory(prefix="anypic-test-") as td:
        tmp = Path(td)
        dl = make_dl(tmp)

        # 1) same basename requested twice must yield two distinct paths (and return!)
        d = tmp / "site.com"
        d.mkdir(parents=True)
        first = dl._unique_path(d, "photo.jpg")
        second = dl._unique_path(d, "photo.jpg")
        if first == second:
            failures.append(f"unique_path collision: {first} == {second}")

        # 2) an existing file on disk must also be avoided
        (d / "photo-1.jpg").write_bytes(b"x")
        third = dl._unique_path(d, "photo.jpg")
        if third.name in ("photo.jpg", "photo-1.jpg"):
            failures.append(f"unique_path ignored existing file: {third}")

        # 3) concurrent hammering with the same name — must terminate quickly
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda _: dl._unique_path(d, "same.jpg"), range(64)))
        elapsed = time.monotonic() - start
        if len({p.name for p in paths}) != 64:
            failures.append(f"concurrent unique_path produced duplicates in {elapsed:.1f}s")
        if elapsed > 5:
            failures.append(f"concurrent unique_path took {elapsed:.1f}s (deadlock?)")

        # 4) filename sanitization keeps extensions honest
        name = dl._filename("https://x.com/a<b>c:d?e.jpg", "jpg")
        if "<" in name or "?" in name:
            failures.append(f"filename not sanitized: {name}")
        if dl._filename("https://x.com/noext", "PNG") != "noext.png":
            failures.append("noext name should gain the probed extension")

        # 5) domain dir strips www. and sanitizes
        if _domain_dir("https://www.Example.com/x") != "example.com":
            failures.append("domain dir mishandles www/case")

        # 6) page slugs: last path segment, sanitized; root URLs get a timestamp
        from anypic.downloader import _page_slug
        if _page_slug("https://telegra.ph/nude-indian-girls-club-09-10") != "nude-indian-girls-club-09-10":
            failures.append(f"page slug wrong: {_page_slug('https://telegra.ph/nude-indian-girls-club-09-10')}")
        if _page_slug("https://x.com/blog/My Post?utm=1") != "My Post":
            failures.append(f"page slug should ignore query: {_page_slug('https://x.com/blog/My Post?utm=1')}")
        root_slug = _page_slug("https://example.com/")
        if not root_slug or _SANITIZE_CHECK_RE.search(root_slug):
            failures.append(f"root-URL slug invalid: {root_slug!r}")

        # 7) same filename in two different page subdirs must NOT collide
        d1 = tmp / "dom.com" / "page-a"
        d2 = tmp / "dom.com" / "page-b"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        pa = dl._unique_path(d1, "x.jpg")
        pb = dl._unique_path(d2, "x.jpg")
        if pa.name != "x.jpg" or pb.name != "x.jpg":
            failures.append(f"cross-subdir false collision: {pa.name} / {pb.name}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):\n")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All downloader checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def test_anypic_downloader_suite():
    """AnyPic naming/dedup/slugs suite (ported from the standalone repo)."""
    assert main() == 0
