"""Data structures shared across anypic modules."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ImageCandidate:
    """One possible URL for an image found on a page."""

    url: str
    source: str = "unknown"  # img-src / srcset / data-attr / anchor / meta / json-ld / css / json / response / dom
    score: int = 0  # extraction priority; higher wins when grouping
    declared_width: int | None = None  # from srcset descriptor, width attr or naturalWidth
    declared_height: int | None = None
    page_url: str = ""


@dataclass
class ProbeInfo:
    """What we learned by probing a candidate URL."""

    url: str
    ok: bool = False
    content_type: str = ""
    length: int = 0
    width: int = 0
    height: int = 0
    note: str = ""

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass
class ResolvedImage:
    """A group of candidates for the same underlying image, resolved to the best URL."""

    group_key: str
    candidates: list[ProbeInfo] = field(default_factory=list)
    best: ProbeInfo | None = None
    source: str = "unknown"
    declared_width: int | None = None


@dataclass
class DownloadResult:
    url: str
    status: str = ""  # downloaded / skipped-dup / skipped-url / too-small / failed
    file: str = ""
    width: int = 0
    height: int = 0
    bytes: int = 0
    note: str = ""


@dataclass
class PageResult:
    url: str
    tier: str = "none"  # gallery-dl / direct / static / browser
    found: int = 0
    results: list[DownloadResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def downloaded(self) -> list[DownloadResult]:
        return [r for r in self.results if r.status == "downloaded"]

    @property
    def skipped(self) -> list[DownloadResult]:
        return [r for r in self.results if r.status.startswith("skipped")]

    @property
    def failed(self) -> list[DownloadResult]:
        return [r for r in self.results if r.status in ("failed", "too-small")]

    @property
    def ok(self) -> bool:
        return bool(self.downloaded) or bool(self.skipped) or self.tier == "gallery-dl"
