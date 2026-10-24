"""Filesystem boundaries shared by native download engines."""
import re
from pathlib import Path


def safe_name(value, fallback='unnamed_file'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', str(value or '')).strip().rstrip('. ')
    if not name or name in ('.', '..'):
        return fallback
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name, re.I):
        name = '_' + name
    return name[:200].rstrip('. ') or fallback


def contained_path(root, *parts):
    root = Path(root).resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Download destination escapes its configured directory')
    return target
