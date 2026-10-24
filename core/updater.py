"""
Engine updater module for MULTI_DOWNLOADER.
Handles checking, updating, and synchronizing all download engines (pip packages and git repositories).
"""

import importlib.metadata
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from core.config import ConfigManager
from core.terminal import Style


class EngineUpdater:
    """Manages updates for all MULTI_DOWNLOADER engines and dependencies."""

    def __init__(self, config: Optional[ConfigManager] = None):
        self.config = config or ConfigManager()
        self.project_root = Path(__file__).resolve().parent.parent
        self.engines_dir = self.project_root / "engines"

    @staticmethod
    def _get_pkg_version(pkg_name: str) -> Optional[str]:
        """Safely gets installed package version."""
        try:
            return importlib.metadata.version(pkg_name)
        except (importlib.metadata.PackageNotFoundError, Exception):
            return None

    def _pip_upgrade(self, pkg_name: str, display_name: Optional[str] = None) -> Tuple[bool, str]:
        """Upgrades a python package via pip in the current environment."""
        display = display_name or pkg_name
        v_before = self._get_pkg_version(pkg_name)

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", pkg_name],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            if proc.returncode != 0:
                err_msg = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "Unknown pip error"
                return False, f"pip update error: {err_msg}"

            if hasattr(importlib.metadata, "invalidate_caches"):
                importlib.metadata.invalidate_caches()

            v_after = self._get_pkg_version(pkg_name)

            if v_before and v_after and v_before == v_after:
                return True, f"Up to date ({Style.dim(f'v{v_after}')})"
            elif v_before and v_after:
                return True, f"Updated ({Style.yellow(f'v{v_before}')} -> {Style.green(f'v{v_after}')})"
            elif v_after:
                return True, f"Installed ({Style.green(f'v{v_after}')})"
            else:
                return True, "Updated successfully"
        except subprocess.TimeoutExpired:
            return False, "Update timed out after 120s"
        except Exception as e:
            return False, f"Failed: {e}"

    def _update_or_clone_git_repo(
        self,
        name: str,
        repo_url: str,
        candidate_paths: List[Path],
    ) -> Tuple[bool, str]:
        """
        Updates an existing git repository or clones it from GitHub if missing.
        Also runs pip install on requirements.txt if present.
        """
        git_bin = shutil.which("git")
        if not git_bin:
            return False, "Git binary not found in system PATH"

        # Locate existing local directory
        repo_dir: Optional[Path] = None
        for p in candidate_paths:
            if p.exists() and (p / ".git").is_dir():
                repo_dir = p
                break

        # Case 1: Repo exists locally -> Git pull
        if repo_dir:
            try:
                # Check commit before
                rev_before = subprocess.run(
                    [git_bin, "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()

                # Pull updates
                pull_res = subprocess.run(
                    [git_bin, "-C", str(repo_dir), "pull"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90,
                )

                if pull_res.returncode != 0:
                    # Fallback to origin main pull
                    pull_res = subprocess.run(
                        [git_bin, "-C", str(repo_dir), "pull", "origin", "main"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=90,
                    )

                if pull_res.returncode != 0:
                    err_line = pull_res.stderr.strip().splitlines()[-1] if pull_res.stderr else "Git pull error"
                    return False, f"Git pull failed: {err_line}"

                rev_after = subprocess.run(
                    [git_bin, "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()

                # Refresh requirements if present
                req_file = repo_dir / "requirements.txt"
                if req_file.exists():
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--quiet"],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=180,
                    )

                if rev_before and rev_after and rev_before == rev_after:
                    return True, f"Up to date ({Style.dim(rev_after)}) [{Style.dim(str(repo_dir))}]"
                else:
                    return True, f"Updated ({Style.yellow(rev_before)} -> {Style.green(rev_after)}) [{Style.dim(str(repo_dir))}]"

            except subprocess.TimeoutExpired:
                return False, "Git pull timed out"
            except Exception as e:
                return False, f"Git update error: {e}"

        # Case 2: Repo does NOT exist locally (e.g. fresh clone on another PC) -> Clone from GitHub
        target_dir = self.engines_dir / name.lower()
        self.engines_dir.mkdir(parents=True, exist_ok=True)

        sys.stdout.write(f"\n  {Style.tag('📥', 'CLONING', Style.CYAN)} {name} not found locally. Cloning from {Style.dim(repo_url)}...\n")
        sys.stdout.flush()

        try:
            clone_res = subprocess.run(
                [git_bin, "clone", repo_url, str(target_dir)],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            if clone_res.returncode != 0:
                err_line = clone_res.stderr.strip().splitlines()[-1] if clone_res.stderr else "Clone failed"
                return False, f"Clone failed: {err_line}"

            # Install dependencies
            req_file = target_dir / "requirements.txt"
            if req_file.exists():
                sys.stdout.write(f"  {Style.tag('📦', 'DEPS', Style.CYAN)} Installing requirements for {name}...\n")
                sys.stdout.flush()
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--quiet"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=180,
                )

            rev = subprocess.run(
                [git_bin, "-C", str(target_dir), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()

            return True, f"Cloned & initialized ({Style.green(rev)}) [{Style.dim(str(target_dir))}]"

        except subprocess.TimeoutExpired:
            return False, "Git clone timed out"
        except Exception as e:
            return False, f"Clone error: {e}"

    def update_ytdlp(self) -> Tuple[bool, str]:
        """Updates yt-dlp python package and standalone binary if present."""
        ok, msg = self._pip_upgrade("yt-dlp", "yt-dlp")

        # Also check for standalone yt-dlp binary (yt-dlp.exe -U)
        yt_bin = shutil.which("yt-dlp")
        if not yt_bin:
            # Common user location fallback
            user_yt = Path.home() / "yt-dlp" / "yt-dlp.exe"
            if user_yt.exists():
                yt_bin = str(user_yt)

        if yt_bin:
            try:
                subprocess.run([yt_bin, "-U"], capture_output=True, text=True, check=False, timeout=60)
            except Exception:
                pass

        return ok, msg

    def update_gallerydl(self) -> Tuple[bool, str]:
        """Updates gallery-dl python package."""
        return self._pip_upgrade("gallery-dl", "gallery-dl")

    def update_curl_cffi(self) -> Tuple[bool, str]:
        """Updates or installs curl_cffi for TLS impersonation."""
        return self._pip_upgrade("curl_cffi", "curl_cffi")

    def update_telegramdl(self) -> Tuple[bool, str]:
        """Updates or clones Telegram-dl."""
        repo_url = self.config.get("telegram_dl", "github_repo", "https://github.com/Alex-0099/Telegram-dl.git")
        candidate_paths = [
            self.project_root.parent / "telegram-dl",
            self.project_root.parent / "Telegram-dl",
            self.engines_dir / "telegram-dl",
        ]
        custom_path = self.config.get("telegram_dl", "engine_path")
        if custom_path:
            candidate_paths.insert(0, Path(custom_path))

        return self._update_or_clone_git_repo("Telegram-dl", repo_url, candidate_paths)

    def update_teraboxdl(self) -> Tuple[bool, str]:
        """Updates or clones Terabox-dl."""
        repo_url = self.config.get("terabox_dl", "github_repo", "https://github.com/Alex-0099/Terabox-DL.git")
        candidate_paths = [
            self.project_root.parent / "Terabox-dl",
            self.project_root.parent / "terabox-dl",
            self.engines_dir / "terabox-dl",
        ]
        custom_path = self.config.get("terabox_dl", "engine_path")
        if custom_path:
            candidate_paths.insert(0, Path(custom_path))

        return self._update_or_clone_git_repo("Terabox-dl", repo_url, candidate_paths)

    def update_cyberdropdl(self) -> Tuple[bool, str]:
        """Updates cyberdrop-dl if installed (standalone binary or via pip in .venv)."""
        # 1. Check for standalone binary (PATH, ~/.local/bin, etc.)
        cdl_bin = shutil.which("cyberdrop-dl") or shutil.which("cyberdrop-dl.exe")
        if not cdl_bin:
            local_candidate = Path.home() / ".local" / "bin" / "cyberdrop-dl.exe"
            if local_candidate.exists():
                cdl_bin = str(local_candidate)

        if cdl_bin:
            commands = []
            uv_bin = shutil.which('uv')
            pipx_bin = shutil.which('pipx')
            if uv_bin:
                commands.extend([[uv_bin, 'tool', 'upgrade', package] for package in ('cyberdrop-dl-patched', 'cyberdrop-dl')])
            if pipx_bin:
                commands.extend([[pipx_bin, 'upgrade', package] for package in ('cyberdrop-dl-patched', 'cyberdrop-dl')])
            if not commands:
                return False, 'Standalone binary found; no supported tool manager is available to update it.'
            for command in commands:
                try:
                    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=90)
                    if result.returncode == 0:
                        return True, 'Tool manager completed the update successfully'
                except (OSError, subprocess.TimeoutExpired):
                    continue
            return False, 'Cyberdrop tool update failed; installed files were left in place.'

        # 2. Fallback: check if installed in current Python environment via pip
        if self._get_pkg_version("cyberdrop-dl-patched"):
            return self._pip_upgrade("cyberdrop-dl-patched", "cyberdrop-dl-patched")
        if self._get_pkg_version("cyberdrop-dl"):
            return self._pip_upgrade("cyberdrop-dl", "cyberdrop-dl")

        return True, "Not installed (skipped)"

    def update_self(self) -> Tuple[bool, str]:
        """Checks and pulls updates for MULTI_DOWNLOADER core repository."""
        git_bin = shutil.which("git")
        if not git_bin or not (self.project_root / ".git").is_dir():
            return True, "Not a git repository (skipped)"

        try:
            rev_before = subprocess.run(
                [git_bin, "-C", str(self.project_root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()

            pull_res = subprocess.run(
                [git_bin, "-C", str(self.project_root), "pull"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            if pull_res.returncode != 0:
                return False, 'Git pull failed; see repository status and network access'

            rev_after = subprocess.run(
                [git_bin, "-C", str(self.project_root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()

            if rev_before and rev_after and rev_before == rev_after:
                return True, f"Up to date ({Style.dim(rev_after)})"
            else:
                return True, f"Updated ({Style.yellow(rev_before)} -> {Style.green(rev_after)})"
        except Exception as e:
            return False, f"Git error: {e}"

    def update_all(self, check_only: bool = False) -> Dict[str, bool]:
        """Runs update check and upgrade routine across all download engines."""
        if check_only:
            # Read installed state only; never invoke updater functions in check mode.
            packages = {'yt-dlp': 'yt-dlp', 'gallery-dl': 'gallery-dl', 'telegram-dl': 'telethon'}
            return {name: bool(self._get_pkg_version(package)) for name, package in packages.items()}
        print(f"\n{Style.tag('🔄', 'UPDATE', Style.CYAN)} {Style.bold('Checking and updating all download engines...')}\n")

        engines = [
            ("multi-dl", "MULTI_DOWNLOADER", self.update_self),
            ("yt-dlp", "yt-dlp", self.update_ytdlp),
            ("gallery-dl", "gallery-dl", self.update_gallerydl),
            ("curl_cffi", "curl_cffi", self.update_curl_cffi),
            ("telegram-dl", "Telegram-dl", self.update_telegramdl),
            ("terabox-dl", "Terabox-dl", self.update_teraboxdl),
            ("cyberdrop-dl", "cyberdrop-dl", self.update_cyberdropdl),
        ]

        results = {}
        for key, name, func in engines:
            color = Style.engine_color(key)
            badge = f"{color}{Style.BOLD}[{name}]{Style.RESET}"
            sys.stdout.write(f"  • {badge:<22} Checking... ")
            sys.stdout.flush()

            try:
                ok, msg = func()
                results[key] = ok
                status_tag = Style.tag("✅", "OK", Style.GREEN) if ok else Style.tag("❌", "FAIL", Style.RED)
                sys.stdout.write(f"\r\033[K  • {badge:<22} {status_tag} {msg}\n")
                sys.stdout.flush()
            except Exception as e:
                results[key] = False
                sys.stdout.write(f"\r\033[K  • {badge:<22} {Style.tag('❌', 'FAIL', Style.RED)} Unexpected error: {e}\n")
                sys.stdout.flush()

        print(f"\n{Style.tag('✨', 'FINISHED', Style.GREEN)} {Style.success('Engine update check complete!')}\n")
        return results
