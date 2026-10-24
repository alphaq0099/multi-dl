"""
Unit test suite for MULTI_DOWNLOADER Configuration System.
Tests schema validation, deep-merge migrations, dot-notation access,
type coercion, dynamic engine limits, and domain throttling guards.
"""

import json
from pathlib import Path
import tempfile
import pytest

from core.config import ConfigManager, coerce_value
from core.models import QueueItem, TaskStatus
from core.queue_manager import QueueManager, extract_url_domain


def test_coerce_value_types():
    """Verifies string to Python type conversion."""
    # Booleans
    assert coerce_value("true") is True
    assert coerce_value("yes") is True
    assert coerce_value("on") is True
    assert coerce_value("false") is False
    assert coerce_value("no") is False
    assert coerce_value("off") is False

    # Numbers
    assert coerce_value("42") == 42
    assert coerce_value("3.14") == 3.14

    # Nulls
    assert coerce_value("null") is None
    assert coerce_value("none") is None

    # JSON structures
    assert coerce_value('["photo", "video"]') == ["photo", "video"]
    assert coerce_value('{"key": "val"}') == {"key": "val"}

    # Strings
    assert coerce_value('"bestvideo"') == "bestvideo"
    assert coerce_value("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_config_fresh_creation(tmp_path):
    """Verifies that an absent config file is initialized with the full default schema."""
    cfg_file = tmp_path / "config.json"
    assert not cfg_file.exists()

    cm = ConfigManager(config_path=cfg_file)
    assert cfg_file.exists()
    assert cm.get("version") == "4.6.0"
    assert cm.get("general", "concurrent_downloads") == 3
    assert cm.engine_limits["yt-dlp"] == 2
    assert cm.engine_limits["telegram-dl"] == 1
    assert cm.get_max_per_domain() == 1


def test_config_deep_merge_migration(tmp_path):
    """Verifies that missing keys in legacy config are merged without overwriting customizations."""
    cfg_file = tmp_path / "config.json"
    legacy_data = {
        "version": "1.0.0",
        "general": {
            "concurrent_downloads": 7,  # Custom value
        },
        "yt_dlp": {
            "format": "custom_format_string",
        }
    }
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(legacy_data, f)

    cm = ConfigManager(config_path=cfg_file)

    # Custom values preserved
    assert cm.get("general", "concurrent_downloads") == 7
    assert cm.get("yt_dlp", "format") == "custom_format_string"

    # Missing schema keys merged
    assert cm.get("version") == "4.6.0"
    assert cm.get("network", "timeout") == 30
    assert cm.engine_limits["yt-dlp"] == 2
    assert cm.domain_limits["max_per_domain"] == 1
    assert cm.get("archive", "enabled") is True


def test_dot_notation_get_and_set(tmp_path):
    """Verifies get_path and set_path with persistence."""
    cfg_file = tmp_path / "config.json"
    cm = ConfigManager(config_path=cfg_file)

    # Get
    assert cm.get_path("engine_limits.yt-dlp") == 2
    assert cm.get_path("nonexistent.key", default="fallback") == "fallback"

    # Set integer
    cm.set_path("engine_limits.yt-dlp", "4")
    assert cm.get_path("engine_limits.yt-dlp") == 4
    assert cm.engine_limits["yt-dlp"] == 4

    # Verify persisted to disk
    with open(cfg_file, "r", encoding="utf-8") as f:
        disk_data = json.load(f)
    assert disk_data["engine_limits"]["yt-dlp"] == 4

    # Set nested string
    cm.set_path("network.proxy", "http://127.0.0.1:9090")
    assert cm.proxy == "http://127.0.0.1:9090"

    # Set boolean
    cm.set_path("domain_limits.enabled", "false")
    assert cm.domain_limits["enabled"] is False
    assert cm.get_max_per_domain() == 999  # Disabled returns 999


def test_engine_limits_normalization(tmp_path):
    """Verifies that hyphenated and underscored engine names both resolve properly."""
    cfg_file = tmp_path / "config.json"
    cm = ConfigManager(config_path=cfg_file)

    assert cm.get_engine_limit("yt-dlp") == 2
    assert cm.get_engine_limit("yt_dlp") == 2
    assert cm.get_engine_limit("telegram-dl") == 1
    assert cm.get_engine_limit("telegram_dl") == 1
    assert cm.get_engine_limit("gallery-dl") == 4
    assert cm.get_engine_limit("unknown_engine", default=99) == 99


def test_global_proxy_inheritance(tmp_path):
    """Verifies that backend configurations inherit the global proxy if not overridden."""
    cfg_file = tmp_path / "config.json"
    cm = ConfigManager(config_path=cfg_file)

    assert cm.proxy is None
    gdl_cfg = cm.get_backend_config("gallery-dl")
    assert gdl_cfg.get("proxy") is None

    # Set global proxy
    cm.set_path("network.proxy", "socks5://127.0.0.1:1080")
    assert cm.proxy == "socks5://127.0.0.1:1080"

    # Backends now inherit it
    yt_cfg = cm.get_backend_config("yt-dlp")
    assert yt_cfg.get("proxy") == "socks5://127.0.0.1:1080"
    gdl_cfg2 = cm.get_backend_config("gallery-dl")
    assert gdl_cfg2.get("proxy") == "socks5://127.0.0.1:1080"


def test_domain_extraction():
    """Verifies hostname extraction from diverse URLs."""
    assert extract_url_domain("https://www.youtube.com/watch?v=123") == "www.youtube.com"
    assert extract_url_domain("http://cam4.com/model") == "cam4.com"
    assert extract_url_domain("https://sub.domain.org:8080/path") == "sub.domain.org"
    assert extract_url_domain("invalid-url") == ""


def test_queue_domain_throttling_guard(tmp_path):
    """Verifies that QueueManager.get_next_eligible enforces max_per_domain rate limit."""
    q_file = tmp_path / "queue.json"
    qm = QueueManager(queue_file=q_file)

    # Add 2 items from youtube.com and 1 item from twitter.com
    item1 = qm.add("https://www.youtube.com/watch?v=aaa", backend="yt-dlp", priority=10)
    item2 = qm.add("https://www.youtube.com/watch?v=bbb", backend="yt-dlp", priority=5)
    item3 = qm.add("https://x.com/user/status/111", backend="gallery-dl", priority=1)

    active_engines = {"yt-dlp": 1, "gallery-dl": 0}
    active_domains = {"www.youtube.com": 1}  # youtube is currently active!

    # youtube limit max_per_domain = 1 is reached.
    # get_next_eligible should skip item2 (youtube) and pick item3 (x.com)!
    eligible = qm.get_next_eligible(
        active_engine_counts=active_engines,
        engine_limits={"yt-dlp": 2, "gallery-dl": 4},
        active_domain_counts=active_domains,
        max_per_domain=1,
    )

    assert eligible is not None
    assert eligible.id == item3.id
    assert eligible.url == "https://x.com/user/status/111"


def test_flat_key_aliases_and_resolution(tmp_path):
    """Verifies that flat key shortcuts correctly resolve and update canonical section settings."""
    cfg_file = tmp_path / "config.json"
    cm = ConfigManager(config_path=cfg_file)

    # 1. max_per_domain -> domain_limits.max_per_domain
    assert cm.get_max_per_domain() == 1
    resolved = cm.set_path("max_per_domain", "3")
    assert resolved == "domain_limits.max_per_domain"
    assert cm.get_max_per_domain() == 3
    assert cm.get_path("max_per_domain") == 3
    assert cm.get_path("domain_limits.max_per_domain") == 3

    # Check persisted on disk under domain_limits, NOT at root
    with open(cfg_file, "r", encoding="utf-8") as f:
        disk = json.load(f)
    assert disk["domain_limits"]["max_per_domain"] == 3
    assert "max_per_domain" not in disk  # Not saved at root!

    # 2. concurrent_downloads -> general.concurrent_downloads
    resolved_cd = cm.set_path("concurrent_downloads", "6")
    assert resolved_cd == "general.concurrent_downloads"
    assert cm.get_path("general.concurrent_downloads") == 6
    assert cm.get_path("concurrent_downloads") == 6

    # 3. proxy -> network.proxy
    resolved_px = cm.set_path("proxy", "http://127.0.0.1:8888")
    assert resolved_px == "network.proxy"
    assert cm.proxy == "http://127.0.0.1:8888"

    # 4. Engine name shortcut -> engine_limits.yt-dlp
    resolved_eng = cm.set_path("yt-dlp", "5")
    assert resolved_eng == "engine_limits.yt-dlp"
    assert cm.get_engine_limit("yt-dlp") == 5


def test_unknown_key_validation(tmp_path):
    """Verifies that invalid or multi-word keys raise ConfigError instead of polluting the config file."""
    from core.exceptions import ConfigError

    cfg_file = tmp_path / "config.json"
    cm = ConfigManager(config_path=cfg_file)

    with pytest.raises(ConfigError):
        cm.set_path("Max per domain", "3")

    with pytest.raises(ConfigError):
        cm.set_path("nonexistent_random_key", "123")


def test_credential_masking(monkeypatch):
    """Verifies that sensitive credentials are never displayed in raw plaintext in terminal output."""
    import importlib
    multi_dl = importlib.import_module("multi-dl")

    format_config_value = multi_dl.format_config_value
    sanitize_proxy_display = multi_dl.sanitize_proxy_display

    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TERABOX_NDUS", raising=False)
    monkeypatch.delenv("TERABOX_NDUS_COOKIE", raising=False)
    monkeypatch.delenv("TERABOX_JS_TOKEN", raising=False)

    # Sensitive keys masked entirely
    masked_hash = format_config_value("api_hash", "0123456789abcdef0123456789abcdef")
    assert "0123456789abcdef0123456789abcdef" not in masked_hash
    assert "********" in masked_hash
    assert "Configured" in masked_hash

    masked_id = format_config_value("api_id", "12345678")
    assert "12345678" not in masked_id
    assert "********" in masked_id
    assert "Configured" in masked_id

    masked_cookie = format_config_value("ndus_cookie", "ndus=secret_session_token_xyz")
    assert "secret_session_token_xyz" not in masked_cookie
    assert "********" in masked_cookie
    assert "Configured" in masked_cookie

    masked_jstoken = format_config_value("js_token", "secret_token_12345")
    assert "secret_token_12345" not in masked_jstoken
    assert "********" in masked_jstoken
    assert "Configured" in masked_jstoken

    # Empty credentials
    empty_val = format_config_value("api_hash", "")
    assert "None (not configured)" in empty_val

    # Non-sensitive keys NOT masked
    plain_dir = format_config_value("download_dir", "downloads")
    assert "downloads" in plain_dir
    assert "masked" not in plain_dir

    # Proxy URL credential sanitization
    raw_proxy = "http://username:secret_password@proxy.example.com:8080"
    sanitized = sanitize_proxy_display(raw_proxy)
    assert "secret_password" not in sanitized
    assert "******" in sanitized
    assert "proxy.example.com:8080" in sanitized


def test_env_credential_fallback_and_injection(tmp_path, monkeypatch):
    """Verifies that .env/environment variables supply credentials to backends and CLI displays."""
    import importlib
    multi_dl = importlib.import_module("multi-dl")
    format_config_value = multi_dl.format_config_value

    monkeypatch.setenv("TELEGRAM_API_ID", "998877")
    monkeypatch.setenv("TELEGRAM_API_HASH", "env_secret_hash_value")
    monkeypatch.setenv("TERABOX_NDUS_COOKIE", "ndus_from_env")

    cfg_file = tmp_path / "config.json"
    cm = ConfigManager(config_path=cfg_file)

    # get_path fallback
    assert cm.get_path("telegram_dl.api_id") == "998877"
    assert cm.get_path("telegram_dl.api_hash") == "env_secret_hash_value"
    assert cm.get_path("terabox_dl.ndus_cookie") == "ndus_from_env"

    # Backend config injection
    tg_cfg = cm.get_backend_config("telegram-dl")
    assert tg_cfg["api_id"] == "998877"
    assert tg_cfg["api_hash"] == "env_secret_hash_value"

    tb_cfg = cm.get_backend_config("terabox-dl")
    assert tb_cfg["ndus_cookie"] == "ndus_from_env"

    # Terminal safe display indicates .env source
    env_display = format_config_value("api_id", "")
    assert "Configured via .env" in env_display
    assert "********" in env_display


def test_cross_directory_path_anchoring():
    """Verifies all core manager paths are permanently anchored to PROJECT_ROOT regardless of CWD."""
    from core.archive import ArchiveManager
    from core.router import URLRouter

    cm = ConfigManager()
    assert cm.download_dir.is_absolute()
    assert cm.archive_db_path.is_absolute()
    assert cm.PROJECT_ROOT in cm.download_dir.parents or cm.download_dir == cm.PROJECT_ROOT / "downloads"
    assert cm.PROJECT_ROOT in cm.archive_db_path.parents

    # ArchiveManager
    am = ArchiveManager(Path("data/test_archive.db"))
    try:
        assert am.db_path.is_absolute()
        assert cm.PROJECT_ROOT in am.db_path.parents
    finally:
        if am.db_path.exists():
            try:
                am.db_path.unlink()
            except Exception:
                pass

    # QueueManager
    qm = QueueManager(Path("data/test_queue.json"))
    assert qm.queue_file.is_absolute()
    assert cm.PROJECT_ROOT in qm.queue_file.parents

    # Router handles hotpic2
    assert URLRouter.detect_backend_name("https://hotpic2.cc/album/45oGqtErHtZuT") == "cyberdrop-dl"
    assert URLRouter.detect_backend_name("https://www.hotpic2.cc/album/45oGqtErHtZuT") == "cyberdrop-dl"

