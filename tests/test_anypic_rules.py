"""Smoke tests for the rewrite table, grouping and junk filter.

Run:  python tests/test_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anypic.rules import canonical_key, is_junk, rewrite_variants, strip_size_query

# (input URL, expected substring that MUST appear in some variant)
CASES = [
    # WordPress sized suffix
    ("https://blog.example.com/wp-content/uploads/2024/03/photo-1024x768.jpg",
     "photo.jpg"),
    # WordPress scaled
    ("https://blog.example.com/wp-content/uploads/2024/03/photo-scaled.jpg",
     "photo.jpg"),
    # Shopify
    ("https://cdn.shopify.com/s/files/1/0001/products/item_600x600.jpg?v=12",
     "item.jpg"),
    # Blogger
    ("https://1.bp.blogspot.com/-abc/XYZ/s320/pic.jpg",
     "/s0/pic.jpg"),
    ("https://lh3.googleusercontent.com/proxy/AbC=w400-h300",
     "proxy/AbC"),
    # Twitter / X
    ("https://pbs.twimg.com/media/AbCdEf?format=jpg&name=small",
     "name=orig"),
    ("https://pbs.twimg.com/profile_images/123/x_normal.jpg",
     "x.jpg"),
    # Reddit preview
    ("https://preview.redd.it/abc123.jpg?width=640&format=pjpg&auto=webp&s=xyz",
     "https://i.redd.it/abc123.jpg"),
    # imgur thumbnail letter
    ("https://i.imgur.com/AbCdEal.jpg",
     "AbCdEa.jpg"),
    # Wikimedia thumb
    ("https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Foo.jpg/300px-Foo.jpg",
     "/commons/a/ab/Foo.jpg"),
    # Pinterest
    ("https://i.pinimg.com/236x/aa/bb/cc/foo.jpg",
     "/originals/aa/bb/cc/foo.jpg"),
    # Medium
    ("https://miro.medium.com/max/700/1*xYz.png",
     "max/4000/1*xYz.png"),
    # Tumblr
    ("https://64.media.tumblr.com/abc/tumblr_xyz_1280.jpg",
     "tumblr_xyz_raw.jpg"),
    # pixhost
    ("https://t1.pixhost.to/thumbs/123/456.jpg",
     "https://img1.pixhost.to/images/123/456.jpg"),
    # bunkr
    ("https://i.bunkr.ru/file.name.md.jpg",
     "file.name.jpg"),
    # generic path size segment
    ("https://cdn.example.com/images/300x300/pic.jpg",
     "/images/pic.jpg"),
    # double extension (.png.webp) sized variants
    ("https://torrentfreak.com/images/pic-300x200.png.webp",
     "pic.png.webp"),
    ("https://cdn.example.com/pic_600x600.jpg.webp",
     "pic.jpg.webp"),
    # generic thumb token segment
    ("https://cdn.example.com/media/thumbs/pic.jpg",
     "/media/pic.jpg"),
    # query size params
    ("https://img.example.net/pic.jpg?width=640&height=480&token=abc",
     "token=abc"),
    # DeviantArt
    ("https://images-wixmp-_30a.wixmp.com/f/a/b/intermediary/f/a/b/c.png?token=x",
     "f/a/b/c.png"),
    # Squarespace
    ("https://images.squarespace-cdn.com/content/v1/x/pic.jpg?format=750w",
     "format=original"),
    # instagram
    ("https://scontent.cdninstagram.com/v/t51/a/s640x640/123.jpg",
     "a/123.jpg"),
    # ebay
    ("https://i.ebayimg.com/images/g/abc/s-l500.jpg",
     "abc.jpg"),
    # cloudinary
    ("https://res.cloudinary.com/demo/image/upload/w_300,h_300/v1/shoes.jpg",
     "/upload/v1/shoes.jpg"),
]

JUNK = [
    "data:image/png;base64,AAAA",
    "https://example.com/static/logo.png",
    "https://example.com/img/avatar-32.png",
    "https://example.com/assets/sprite-icons.png",
    "https://example.com/tracking/pixel.gif",
    "https://example.com/icons/favicon.ico",
    "https://example.com/1x1/blank.png",
    "https://example.com/images/loading-spinner.webp",
]

NOT_JUNK = [
    "https://example.com/photos/vacation-2024.jpg",
    "https://example.com/uploads/DSC_0421.png",
    "https://cdn.example.net/media/1600x900/landscape.jpg",
]

GROUPING = [
    # same image, different sizes -> same key
    ("https://blog.example.com/wp-content/uploads/photo-150x150.jpg",
     "https://blog.example.com/wp-content/uploads/photo.jpg"),
    ("https://i.pinimg.com/236x/aa/bb/cc/foo.jpg",
     "https://i.pinimg.com/originals/aa/bb/cc/foo.jpg"),
    # format twins merge too (keep one copy per identity)
    ("https://torrentfreak.com/images/cloudblock-1.png",
     "https://torrentfreak.com/images/cloudblock-1-300x200.png.webp"),
]

GROUPING_DISTINCT = [
    # query-string image id must NOT be merged
    ("https://forum.example.com/download/file.php?id=111&t=1",
     "https://forum.example.com/download/file.php?id=222&t=1"),
]


def main() -> int:
    failures = []

    for url, expected in CASES:
        variants = rewrite_variants(url) + [strip_size_query(url)]
        if not any(expected in v for v in variants):
            failures.append(f"RULE MISS: {url}\n    expected variant containing: {expected}\n    got: {variants}")

    for url in JUNK:
        if not is_junk(url):
            failures.append(f"JUNK MISS: {url} not flagged as junk")

    for url in NOT_JUNK:
        if is_junk(url):
            failures.append(f"JUNK FALSE POSITIVE: {url} flagged as junk")

    for a, b in GROUPING:
        if canonical_key(a) != canonical_key(b):
            failures.append(f"GROUP MISS: {a} != {b}\n    {canonical_key(a)}\n    {canonical_key(b)}")

    for a, b in GROUPING_DISTINCT:
        if canonical_key(a) == canonical_key(b):
            failures.append(f"GROUP OVER-MERGE: {a} == {b}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1
    print(f"All checks passed: {len(CASES)} rule cases, {len(JUNK)} junk, "
          f"{len(NOT_JUNK)} non-junk, {len(GROUPING)} groupings, {len(GROUPING_DISTINCT)} distinct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def test_anypic_rules_suite():
    """AnyPic rewrite-table/junk/grouping suite (ported from the standalone repo)."""
    assert main() == 0
