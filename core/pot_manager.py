"""Proof-of-Origin (PO) Token Provider sidecar manager for MULTI_DOWNLOADER."""

import sys
import time
import json
import logging
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from core.pot_security import harden_provider, provider_token

logger = logging.getLogger("multi_dl.pot_manager")


def _safe_print(msg: str):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


class POTManager:
    """Manages the lifecycle and healthchecks of the local bgutil PO token server."""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 4416

    @classmethod
    def get_server_script_path(cls) -> Optional[Path]:
        """Resolves the compiled main.js file for the bgutil server."""
        project_root = Path(__file__).resolve().parent.parent
        possible_paths = [
            project_root / "tools" / "pot-provider" / "server" / "build" / "main.js",
            Path.home() / "bgutil-ytdlp-pot-provider" / "server" / "build" / "main.js",
        ]
        for p in possible_paths:
            if p.is_file():
                return p
        return None

    @classmethod
    def is_server_running(cls, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
        """Checks if the local PO Token server is responding to /ping."""
        url = f"http://{host}:{port}/ping"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MULTI_DOWNLOADER/POTManager", 'Authorization': 'Bearer ' + provider_token()})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=1.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return 'version' in data and data.get('multi_dl_hardened') is True
        except Exception:
            return False
        return False

    @classmethod
    def ensure_server_running(
        cls,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = 15.0,
        silent: bool = False,
    ) -> bool:
        """Verifies if the PO Token server is active; if not, spawns it as a background sidecar."""
        if host not in ('localhost', '127.0.0.1'):
            if not silent:
                _safe_print('[POT-SERVER] Only a loopback provider is supported.')
            return False
        if cls.is_server_running(host, port):
            return True

        script_path = cls.get_server_script_path()
        if not script_path:
            if not silent:
                _safe_print("[POT-SERVER] Provider script (main.js) not found. Skipping auto-start.")
            return False

        try:
            harden_provider(script_path.parent.parent)
            provider_token()
        except Exception as exc:
            if not silent:
                _safe_print(f'[POT-SERVER] Safe startup refused: {exc}')
            return False

        if not silent:
            _safe_print("[POT-SERVER] Starting local Proof-of-Origin sidecar (127.0.0.1:4416)...")

        log_dir = Path(__file__).resolve().parent.parent / "data"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "pot_server.log"

        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NO_WINDOW (0x08000000) prevents command prompt window popups
            # DETACHED_PROCESS (0x00000008) lets it survive independent of caller
            creationflags = 0x08000000 | 0x00000008

        with open(log_file, "a", encoding="utf-8") as out:
            try:
                subprocess.Popen(
                    ["node", str(script_path), "--port", str(port)],
                    cwd=str(script_path.parent.parent),
                    stdout=out,
                    stderr=out,
                    creationflags=creationflags,
                    close_fds=True if sys.platform != "win32" else False,
                )
            except Exception as e:
                if not silent:
                    _safe_print(f"❌ [POT-SERVER] Failed to launch sidecar process: {e}")
                return False

        # Wait for ping healthcheck
        start_time = time.time()
        while time.time() - start_time < timeout:
            if cls.is_server_running(host, port):
                if not silent:
                    _safe_print("✅ [POT-SERVER] Token sidecar is ready and healthy.")
                return True
            time.sleep(0.3)

        if not silent:
            _safe_print("⚠️  [POT-SERVER] Timed out waiting for sidecar health check.")
        return False
