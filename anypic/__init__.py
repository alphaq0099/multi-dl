"""anypic — universal image downloader.

Paste any URL; supported sites go through gallery-dl, everything else is
scraped by the generic engine (static HTTP first, headless browser fallback)
which resolves the original-resolution image instead of the displayed
thumbnail.

Designed to be importable as a library::

    from anypic.pipeline import process_url
    result = process_url("https://example.com/blog/post", cfg)
"""

__version__ = "1.1.0"
