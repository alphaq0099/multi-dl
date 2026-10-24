"""Configuration loading with sane defaults.

On first run a ``config.json`` is created next to the project root with all
defaults written out so the user can tweak everything without touching code.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("anypic")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "output_dir": "downloads",  # relative to project root
    "data_dir": "data",         # relative to project root
    "min_width": 250,
    "min_height": 250,
    "relaxed_min": 100,         # used when a page has nothing above min
    "relaxed_keep": 3,          # how many under-min images to keep when relaxing
    "max_images_per_page": 300,
    "best_only": 0,             # >0: download only the N largest images per page
    "layout": "domain",         # "domain" nests under <domain>/; "flat" writes into output_dir directly
    "workers": 6,
    "timeout": 30,
    "retries": 3,
    "max_candidates": 3,        # URLs probed per image group
    "max_bytes": 268435456,     # 256 MB safety cap per file
    "include_svg": False,
    "dedup": True,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
        "Gecko/20100101 Firefox/135.0"
    ),
    "proxy": None,              # e.g. "http://127.0.0.1:2080" — for blocked sites; gallery-dl uses its own config
    "gallery_dl": {
        "enabled": True,
        "handle_direct": False,  # direct image URLs go to the generic engine
        "executable": r"C:\Users\LENOVO\AppData\Local\Programs\Python\Python313\Scripts\gallery-dl.exe",
        "config": r"C:\Users\LENOVO\Gallery-dl\config.json",
        "timeout": 3600,
    },
    "browser": {
        "enabled": True,
        "timeout": 45_000,      # Playwright milliseconds
        "scroll_steps": 12,
        "scroll_wait": 350,     # ms between scroll steps
    },
}


class Config:
    """Attribute-style access wrapper over the merged config dict."""

    def __init__(self, data: dict[str, Any], project_root: Path):
        self._data = data
        self.project_root = project_root

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(name) from e

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)

    @property
    def output_dir(self) -> Path:
        p = Path(self._data["output_dir"])
        return p if p.is_absolute() else self.project_root / p

    @property
    def data_dir(self) -> Path:
        p = Path(self._data["data_dir"])
        return p if p.is_absolute() else self.project_root / p

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state.sqlite"

    @property
    def manifest_csv(self) -> Path:
        return self.data_dir / "downloads.csv"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "anypic.log"

    @property
    def urls_file(self) -> Path:
        return self.project_root / "urls.txt"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(project_root: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Load config.json (creating it with defaults if missing) and apply overrides."""
    root = project_root or PROJECT_ROOT
    cfg_path = root / "config.json"

    user_data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            user_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not read %s (%s); using defaults", cfg_path, e)
    else:
        cfg_path.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
        log.info("Created default config at %s", cfg_path)

    merged = _deep_merge(DEFAULTS, user_data)
    if overrides:
        merged = _deep_merge(merged, overrides)
    return Config(merged, root)
