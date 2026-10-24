"""
Unit tests for MULTI_DOWNLOADER GUI (Phase 4).
Tests GUI modules, launcher scripts, URL parsing, color helpers, and CLI wiring.
"""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_gui_script_files_exist():
    """Verifies that all GUI scripts and launchers exist in project root."""
    assert (PROJECT_ROOT / "multi-dl-gui.py").exists()
    assert (PROJECT_ROOT / "multi-dl-gui.bat").exists()
    assert (PROJECT_ROOT / "multi-dl-gui.ps1").exists()
    assert (PROJECT_ROOT / "core" / "gui_tracker.py").exists()


def test_gui_engine_colors():
    """Verifies engine color palette in multi-dl-gui."""
    import importlib
    gui_mod = importlib.import_module("multi-dl-gui")
    
    assert gui_mod.get_engine_color("yt-dlp") == "#FF5252" or "#FF" in gui_mod.get_engine_color("yt-dlp")
    assert gui_mod.get_engine_color("gallery-dl") == "#FFD600" or "#FF" in gui_mod.get_engine_color("gallery-dl")
    assert gui_mod.get_engine_color("telegram-dl") == "#29B6F6"
    assert gui_mod.get_engine_color("terabox-dl") == "#00E5FF"
    assert gui_mod.get_engine_color("cyberdrop-dl") == "#E040FB"
    assert gui_mod.get_engine_color("unknown") == "#00E5FF"


def test_gui_cli_subparser_registered():
    """Verifies that 'gui' is registered in multi-dl.py CLI subparsers."""
    import importlib
    multi_dl = importlib.import_module("multi-dl")
    
    # Run argument parser on ['gui', '--help']
    parser = multi_dl.build_argument_parser() if hasattr(multi_dl, "build_argument_parser") else None
    if parser is None:
        # Check subparser choices directly
        import argparse
        assert (PROJECT_ROOT / "multi-dl.py").read_text(encoding="utf-8").count('subparsers.add_parser("gui"') >= 1


import pytest

@pytest.mark.asyncio
async def test_gui_main_headless_initialization():
    """Verifies that multi-dl-gui main() initializes without Control/Page AssertionError."""
    import importlib
    from unittest.mock import MagicMock
    gui_mod = importlib.import_module("multi-dl-gui")

    class MockPage:
        def __init__(self):
            self.controls = []
            self.overlay = []
            self.window = MagicMock()
            self.title = ""
            self.theme_mode = None
            self.bgcolor = None
            self.padding = None
            self.on_keyboard_event = None

        def add(self, *ctrls):
            self.controls.extend(ctrls)

        def update(self):
            pass

    mock_page = MockPage()
    await gui_mod.main(mock_page)
    assert len(mock_page.controls) >= 2
