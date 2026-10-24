"""
Unit tests for TeraboxDlBackend and TeraBox URL routing.
"""

import os
from pathlib import Path
import pytest

from backends.terabox_backend import TeraboxDlBackend, _get_error_message, _detect_media_type
from core.models import MediaType
from core.router import URLRouter


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


def test_ndus_cookie_resolution(monkeypatch, tmp_path):
    dummy_cfg = tmp_path / "empty_terabox.json"
    backend = TeraboxDlBackend({"config_file": str(dummy_cfg)})

    # 1. From CLI / task options (highest priority)
    assert backend._resolve_ndus_cookie({"ndus": "cookie_from_cli"}) == "cookie_from_cli"

    # 2. From backend config
    backend_with_cfg = TeraboxDlBackend({"config_file": str(dummy_cfg), "ndus_cookie": "cookie_from_cfg"})
    assert backend_with_cfg._resolve_ndus_cookie() == "cookie_from_cfg"

    # 3. From environment variable
    monkeypatch.setenv("TERABOX_NDUS", "cookie_from_env")
    assert backend._resolve_ndus_cookie() == "cookie_from_env"


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

    monkeypatch.setattr("builtins.input", lambda _: "sample_ndus_12345")

    cookie = backend._prompt_ndus_cookie()
    assert cookie == "sample_ndus_12345"
    assert backend.config["ndus_cookie"] == "sample_ndus_12345"
    assert cfg_file.exists()


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
