"""
Unit tests for TeraboxDlBackend and TeraBox URL routing.
"""

import asyncio
import json
import os
from pathlib import Path
import pytest

from backends.terabox_backend import TeraboxDlBackend, _get_error_message, _detect_media_type
from core.exceptions import AuthenticationError
from core.models import DownloadTask, MediaType
from core.router import URLRouter

# Real ndus cookies are 40+ chars; fakes must pass the plausibility check.
FAKE_NDUS = "ndus_fake_" + "a" * 40


def test_terabox_can_handle():
    backend = TeraboxDlBackend()

    # Valid TeraBox links
    assert backend.can_handle("https://www.terabox.com/s/1ABCxyz") is True
    assert backend.can_handle("https://terabox.com/s/1ABCxyz") is True
    assert backend.can_handle("https://1024tera.com/sharing/link?surl=ABCxyz") is True
    assert backend.can_handle("https://www.1024tera.com/s/1ABCxyz") is True
    assert backend.can_handle("https://1024terabox.com/s/1K3gVFhIrkT1LuTqOuY2_jA") is True
    assert backend.can_handle("https://www.1024terabox.com/s/1K3gVFhIrkT1LuTqOuY2_jA") is True
    assert backend.can_handle("https://tibibox.com/s/1sample") is True
    assert backend.can_handle("https://freeterabox.com/s/1sample") is True
    assert backend.can_handle("https://mirrobox.com/s/1sample") is True
    assert backend.can_handle("https://nephobox.com/s/1sample") is True
    assert backend.can_handle("https://4funbox.com/s/1sample") is True
    assert backend.can_handle("https://teraboxapp.com/s/1sample") is True
    assert backend.can_handle("https://teraboxlink.com/s/1sample") is True

    # Other sites should not be handled
    assert backend.can_handle("https://youtube.com/watch?v=123") is False
    assert backend.can_handle("https://twitter.com/user/status/123") is False
    assert backend.can_handle("https://t.me/c/123/456") is False


def test_extract_short_key():
    # Direct /s/ path
    assert TeraboxDlBackend.extract_short_key("https://www.terabox.com/s/1ABCxyz") == "1ABCxyz"
    assert TeraboxDlBackend.extract_short_key("https://1024tera.com/s/1TestKey?fid=123") == "1TestKey"

    # Query param surl=
    assert TeraboxDlBackend.extract_short_key("https://1024tera.com/sharing/link?surl=DefGhi") == "1DefGhi"
    assert TeraboxDlBackend.extract_short_key("https://www.terabox.com/sharing/link?surl=1AlreadyHasOne") == "1AlreadyHasOne"

    # Bare key
    assert TeraboxDlBackend.extract_short_key("1BareKey123") == "1BareKey123"


def test_error_message_mapping():
    assert "expired" in _get_error_message(-6).lower()
    assert "password" in _get_error_message(-7).lower()
    assert "verify" in _get_error_message(400210).lower()
    assert "jstoken" in _get_error_message(4000023).lower()
    assert "code 9999" in _get_error_message(9999).lower()


def test_media_type_detection():
    assert _detect_media_type("movie.mp4") == MediaType.VIDEO
    assert _detect_media_type("song.flac") == MediaType.AUDIO
    assert _detect_media_type("photo.png") == MediaType.IMAGE
    assert _detect_media_type("archive.zip") == MediaType.DOCUMENT
    assert _detect_media_type("something.xyz") == MediaType.UNKNOWN


def _isolated_env(monkeypatch):
    """Removes machine-specific ndus sources so resolution tests are deterministic."""
    monkeypatch.delenv("TERABOX_NDUS", raising=False)
    monkeypatch.delenv("TERABOX_NDUS_COOKIE", raising=False)
    monkeypatch.setattr("backends.terabox_backend.load_dotenv", lambda *a, **k: None)


def test_ndus_cookie_resolution(monkeypatch, tmp_path):
    _isolated_env(monkeypatch)
    dummy_cfg = tmp_path / "empty_terabox.json"
    backend = TeraboxDlBackend({"config_file": str(dummy_cfg)})

    # 1. From CLI / task options (highest priority)
    assert backend._resolve_ndus_cookie({"ndus": FAKE_NDUS}) == FAKE_NDUS

    # 2. From backend config
    backend_with_cfg = TeraboxDlBackend({"config_file": str(dummy_cfg), "ndus_cookie": FAKE_NDUS})
    assert backend_with_cfg._resolve_ndus_cookie() == FAKE_NDUS

    # 3. From environment variable
    monkeypatch.setenv("TERABOX_NDUS", FAKE_NDUS)
    assert backend._resolve_ndus_cookie() == FAKE_NDUS


def test_ndus_skips_placeholder_values(monkeypatch, tmp_path):
    """Short placeholder cookies (test fixtures, truncated pastes) must never win."""
    _isolated_env(monkeypatch)
    dummy_cfg = tmp_path / "empty_terabox.json"

    backend = TeraboxDlBackend({"config_file": str(dummy_cfg), "ndus_cookie": "sample_ndus_12345"})
    assert backend._resolve_ndus_cookie() == ""
    assert backend._ndus_warning and "short" in backend._ndus_warning.lower()

    monkeypatch.setenv("TERABOX_NDUS", "cookie_from_env")  # 15 chars — also skipped
    assert backend._resolve_ndus_cookie() == ""

    # A valid config cookie still beats a broken environment value
    good = TeraboxDlBackend({"config_file": str(dummy_cfg), "ndus_cookie": FAKE_NDUS})
    assert good._resolve_ndus_cookie() == FAKE_NDUS

    # An explicit --ndus override fails loudly instead of 403-ing later
    with pytest.raises(AuthenticationError):
        backend._resolve_ndus_cookie({"ndus": "too_short_to_be_real"})


def test_terabox_router_integration():
    backend = TeraboxDlBackend()
    router = URLRouter({"terabox-dl": backend})

    routed = router.route("https://www.terabox.com/s/1ABCxyz")
    assert routed.name == "terabox-dl"

    routed_1024 = router.route("https://1024tera.com/sharing/link?surl=1ABCxyz")
    assert routed_1024.name == "terabox-dl"

    routed_1024terabox = router.route("https://1024terabox.com/s/1K3gVFhIrkT1LuTqOuY2_jA")
    assert routed_1024terabox.name == "terabox-dl"

    # Fast detection test (ensuring x.com substring in 1024terabox does NOT falsely trigger gallery-dl)
    assert URLRouter.detect_backend_name("https://1024terabox.com/s/1K3gVFhIrkT1LuTqOuY2_jA") == "terabox-dl"
    assert URLRouter.match("https://1024terabox.com/s/1K3gVFhIrkT1LuTqOuY2_jA").backend == "terabox-dl"
    assert URLRouter.detect_backend_name("https://x.com/jack/status/20") == "gallery-dl"

    # Verify site/host folder normalization
    host_id = URLRouter.get_host_identifier("https://www.terabox.com/s/1ABCxyz", "terabox-dl")
    assert host_id == "terabox"

    host_id_1024 = URLRouter.get_host_identifier("https://1024tera.com/s/1ABCxyz", "terabox-dl")
    assert host_id_1024 == "terabox"

    host_id_1024terabox = URLRouter.get_host_identifier("https://1024terabox.com/s/1K3gVFhIrkT1LuTqOuY2_jA", "terabox-dl")
    assert host_id_1024terabox == "terabox"


def test_prompt_ndus_cookie(monkeypatch, tmp_path):
    cfg_file = tmp_path / "terabox-dl.json"
    backend = TeraboxDlBackend({"config_file": str(cfg_file)})

    monkeypatch.setattr("core.terminal.Style.is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: FAKE_NDUS)

    cookie = backend._prompt_ndus_cookie()
    assert cookie == FAKE_NDUS
    assert backend.config["ndus_cookie"] == FAKE_NDUS
    assert cfg_file.exists()


def test_prompt_ndus_rejects_placeholder_then_accepts(monkeypatch, tmp_path):
    """A pasted placeholder is rejected with feedback; the next valid entry wins."""
    monkeypatch.setattr("core.terminal.Style.is_interactive", lambda: True)
    answers = iter(["sample_ndus_12345", FAKE_NDUS])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    backend = TeraboxDlBackend({"config_file": str(tmp_path / "terabox-dl.json")})
    assert backend._prompt_ndus_cookie() == FAKE_NDUS


def test_prompt_ndus_non_interactive_raises(monkeypatch):
    monkeypatch.setattr("core.terminal.Style.is_interactive", lambda: False)
    backend = TeraboxDlBackend({"config_file": "nonexistent-terabox.json"})
    with pytest.raises(AuthenticationError):
        backend._prompt_ndus_cookie()


def test_save_default_layout_uses_secret_store(monkeypatch, tmp_path):
    """The default layout writes the cookie only to the secret store and
    blanks any legacy value left in the modular config file."""
    modular = tmp_path / "terabox-dl.json"
    modular.write_text(json.dumps({"ndus_cookie": FAKE_NDUS, "max_retries": 10}), encoding="utf-8")

    set_calls = []

    class FakeMaster:
        def set(self, section, key, value):
            set_calls.append((section, key, value))

    monkeypatch.setattr("core.config.ConfigManager", FakeMaster)
    monkeypatch.setattr("backends.terabox_backend._default_modular_config_path", lambda: modular)

    backend = TeraboxDlBackend({"config_file": str(modular), "ndus_cookie": FAKE_NDUS})
    backend._save_modular_config()

    assert set_calls == [("terabox_dl", "ndus_cookie", FAKE_NDUS)]
    saved = json.loads(modular.read_text(encoding="utf-8"))
    assert saved["ndus_cookie"] == ""
    assert saved["max_retries"] == 10


def test_download_refreshes_dlink_on_403(monkeypatch, tmp_path):
    """A CDN 403 resolves a fresh dlink once instead of retrying the dead link."""
    backend = TeraboxDlBackend({"config_file": str(tmp_path / "terabox-dl.json")})

    info = {
        "share_id": 1, "uk": 2, "sign": "s", "timestamp": "3", "randsk": "r",
        "title": "T", "api_base": "https://www.terabox.com",
        "file_list": [{"fs_id": 123, "server_filename": "video.mp4", "size": 4}],
    }
    monkeypatch.setattr(backend, "_get_share_info", lambda session, key, url: info)

    resolve_calls = []

    def fake_resolve(session, fs_id, *a, **k):
        resolve_calls.append(fs_id)
        return f"https://cdn.example/dlink{len(resolve_calls)}"

    monkeypatch.setattr(backend, "_get_download_link", fake_resolve)

    class Resp403:
        status_code = 403
        headers = {}
        def raise_for_status(self):
            raise RuntimeError("HTTP Error 403: Forbidden")
        def close(self):
            pass

    class Resp206:
        status_code = 206
        headers = {"Content-Range": "bytes 0-3/4"}
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            yield b"abcd"
        def close(self):
            pass

    class FakeSession:
        headers = {}
        def __init__(self):
            self.responses = [Resp403(), Resp206()]
        def get(self, url, **kwargs):
            resp = self.responses.pop(0)
            resp.url = url
            return resp
        def close(self):
            pass

    fake_session = FakeSession()
    monkeypatch.setattr(backend, "_create_session", lambda cookie: fake_session)
    _isolated_env(monkeypatch)

    task = DownloadTask(
        url="https://teraboxlink.com/s/1abcDEFghi-JK", backend="terabox-dl",
        output_dir=str(tmp_path / "out"), options={"quiet": True},
    )
    entry = asyncio.run(backend.download(task))

    assert len(resolve_calls) == 2  # initial resolve + exactly one refresh
    assert entry.file_size == 4
    assert Path(entry.file_path).exists()
    assert Path(entry.file_path).read_bytes() == b"abcd"


def test_non_interactive_auth_errno(monkeypatch):
    from unittest.mock import MagicMock
    from core.exceptions import AuthenticationError

    backend = TeraboxDlBackend()
    monkeypatch.setattr("core.terminal.Style.is_interactive", lambda: False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errno": -20}

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    with pytest.raises(AuthenticationError) as exc_info:
        backend._get_share_info(mock_session, "1testkey", "https://terabox.com/s/1testkey")
    assert "ndus cookie" in str(exc_info.value).lower()


def test_terabox_get_base_url():
    assert TeraboxDlBackend.get_base_url("https://1024terabox.com/s/1K3gVFhIrkT1LuTqOuY2_jA") == "https://1024terabox.com"
    assert TeraboxDlBackend.get_base_url("https://www.1024tera.com/s/1sample") == "https://www.1024tera.com"
    assert TeraboxDlBackend.get_base_url("https://teraboxlink.com/s/1sample") == "https://teraboxlink.com"
    assert TeraboxDlBackend.get_base_url("https://www.terabox.com/s/1sample") == "https://www.terabox.com"


def test_non_interactive_auth_verify_v2(monkeypatch):
    from unittest.mock import MagicMock
    from core.exceptions import AuthenticationError

    backend = TeraboxDlBackend()
    monkeypatch.setattr("core.terminal.Style.is_interactive", lambda: False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errno": 400210, "errmsg": "need verify_v2"}

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    with pytest.raises(AuthenticationError) as exc_info:
        backend._get_share_info(mock_session, "1testkey", "https://1024terabox.com/s/1testkey")
    assert "verify_v2" in str(exc_info.value).lower() or "verification" in str(exc_info.value).lower()
