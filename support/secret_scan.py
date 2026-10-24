"""Pre-commit guard: refuse commits that contain downloader credentials.

Scans staged files (or paths given as arguments) for secret-shaped content:
TeraBox ndus cookies, Telegram API hashes, session files, and credential
command lines. Exits 1 with a file:line list when a secret is found, so a
renamed config file or an accidental `git add -f` cannot publish credentials
from this public repository.

Usage:
    python support/secret_scan.py                 # scan staged files
    python support/secret_scan.py <path> ...      # scan given paths
"""

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Files that legitimately hold secrets (all gitignored; never staged).
ALLOWED_PATH_SUFFIXES = (
    ".secrets.json",
    "config.secrets.json",
)

# Long base64-ish tokens that look like a TeraBox ndus session cookie.
NDUS_TOKEN = re.compile(r"(?<![A-Za-z0-9_/-])[A-Za-z0-9_-]{32,90}(?![A-Za-z0-9_/-])")

PATTERNS = [
    # .env style: TERABOX_NDUS=<real value> (short placeholders pass untouched)
    (re.compile(r"TERABOX_NDUS(?:_COOKIE)?\s*=\s*([^\s#]{20,})"), "TeraBox ndus cookie (env)"),
    # JSON style: "ndus_cookie": "<real value>"
    (re.compile(r'"ndus_cookie"\s*:\s*"([^"]{20,})"'), "TeraBox ndus cookie (json)"),
    # Telegram API hash: 32 hex chars with a nearby key name
    (re.compile(r'"api_hash"\s*:\s*"([a-fA-F0-9]{30,34})"'), "Telegram api_hash (json)"),
    (re.compile(r"TELEGRAM_API_HASH\s*=\s*([a-fA-F0-9]{30,34})"), "Telegram api_hash (env)"),
    # Cookie values inside arbitrary text (ndus= / ndus: headers)
    (re.compile(r"\bndus(?:=|:)\s*([A-Za-z0-9_-]{32,90})"), "TeraBox ndus cookie (inline)"),
]

BINARY_SNIFF = 1024


def staged_files():
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False,
    )
    return [PROJECT_ROOT / f for f in out.stdout.splitlines() if f.strip()]


def scan_file(path: Path):
    """Returns a list of (line_no, description) findings for one file."""
    findings = []
    try:
        raw = path.read_bytes()
    except OSError:
        return findings
    if b"\0" in raw[:BINARY_SNIFF]:
        return findings
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return findings
    if path.name.endswith((".session", ".session-journal")):
        findings.append((0, "Telethon session file"))
        return findings
    if path.name.endswith(ALLOWED_PATH_SUFFIXES):
        return findings
    for line_no, line in enumerate(text.splitlines(), 1):
        for pattern, description in PATTERNS:
            if pattern.search(line):
                findings.append((line_no, description))
                break
    return findings


def main() -> int:
    if len(sys.argv) > 1:
        paths = [Path(a) for a in sys.argv[1:]]
    else:
        paths = staged_files()
    blocked = False
    for path in paths:
        if not path.is_file():
            continue
        for line_no, description in scan_file(path):
            blocked = True
            rel = path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path
            loc = f"{rel}:{line_no}" if line_no else str(rel)
            print(f"  [SECRET] {loc} — {description}")
    if blocked:
        print("\nRefusing commit: credential-shaped content detected.")
        print("Secrets belong in config.secrets.json / .env (both gitignored).")
        print("If this is a false positive, bypass once with: git commit --no-verify")
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
