"""Tests for POTManager (Proof-of-Origin Token Sidecar Provider)."""

from core.pot_manager import POTManager


def test_pot_manager_is_running():
    # Healthcheck check returns boolean without crashing
    running = POTManager.is_server_running()
    assert isinstance(running, bool)


def test_pot_manager_get_script_path():
    path = POTManager.get_server_script_path()
    assert path is not None
    assert path.name == "main.js"
    assert path.exists()


def test_pot_manager_ensure_server(monkeypatch):
    # Test ensure_server_running
    monkeypatch.setattr(POTManager, "is_server_running", lambda *args, **kwargs: True)
    success = POTManager.ensure_server_running(silent=True)
    assert success is True
    assert POTManager.is_server_running() is True

