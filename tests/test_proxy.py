"""
Unit tests for Proxy verification and management in MULTI_DOWNLOADER.
"""

from unittest.mock import MagicMock, patch

from core.config import ConfigManager
import importlib

multi_dl = importlib.import_module("multi-dl")
sanitize_proxy_display = multi_dl.sanitize_proxy_display
run_test_proxy = multi_dl.test_proxy
handle_proxy_command = multi_dl.handle_proxy_command


def test_sanitize_proxy_display():
    assert sanitize_proxy_display(None) == "None (direct connection)"
    assert sanitize_proxy_display("") == "None (direct connection)"
    assert sanitize_proxy_display("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    assert sanitize_proxy_display("http://user:secret123@proxy.example.com:8080") == "http://user:******@proxy.example.com:8080"


@patch("curl_cffi.requests.get")
def test_test_proxy_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "query": "1.2.3.4",
        "country": "Netherlands",
        "city": "Amsterdam",
        "isp": "Proton AG",
    }
    mock_get.return_value = mock_resp

    success = run_test_proxy("socks5://127.0.0.1:1080")
    assert success is True


@patch("curl_cffi.requests.get")
def test_test_proxy_failure(mock_get):
    mock_get.side_effect = Exception("Connection refused")

    success = run_test_proxy("socks5://127.0.0.1:9999", timeout=1)
    assert success is False


def test_handle_proxy_command_set_and_clear(tmp_path):
    cfg_file = tmp_path / "config.json"
    config = ConfigManager(config_path=cfg_file)

    with patch.object(multi_dl, "test_proxy", return_value=True):
        # Set proxy
        handle_proxy_command(config, ["set", "socks5://127.0.0.1:40000"])
        assert config.proxy == "socks5://127.0.0.1:40000"

        # Direct set without "set" keyword
        handle_proxy_command(config, ["http://127.0.0.1:7890"])
        assert config.proxy == "http://127.0.0.1:7890"

        # Clear proxy
        handle_proxy_command(config, ["off"])
        assert config.proxy is None
