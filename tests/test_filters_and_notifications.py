"""
Unit tests for quick format filters, media type filters, and notification settings.
"""

import json
from core.config import ConfigManager
from core.router import parse_resolution_height, resolve_format_spec
import importlib

multi_dl = importlib.import_module("multi-dl")
extract_inline_options = multi_dl.extract_inline_options


class TestResolutionAndFormatResolvers:
    def test_parse_resolution_height_standard(self):
        assert parse_resolution_height("1080p") == 1080
        assert parse_resolution_height("720P") == 720
        assert parse_resolution_height("480") == 480
        assert parse_resolution_height("360p") == 360
        assert parse_resolution_height("144p") == 144

    def test_parse_resolution_height_presets(self):
        assert parse_resolution_height("4k") == 2160
        assert parse_resolution_height("4K") == 2160
        assert parse_resolution_height("2k") == 1440
        assert parse_resolution_height("2K") == 1440
        assert parse_resolution_height("fhd") == 1080
        assert parse_resolution_height("hd") == 720
        assert parse_resolution_height("sd") == 480

    def test_parse_resolution_height_invalid_and_best(self):
        assert parse_resolution_height("best") is None
        assert parse_resolution_height("max") is None
        assert parse_resolution_height("") is None
        assert parse_resolution_height(None) is None
        assert parse_resolution_height("invalid_preset") is None

    def test_resolve_format_spec_explicit(self):
        assert resolve_format_spec(format_str="bestvideo+bestaudio") == "bestvideo+bestaudio"

    def test_resolve_format_spec_audio_only(self):
        assert resolve_format_spec(audio_only=True) == "bestaudio/best"

    def test_resolve_format_spec_video_only(self):
        assert resolve_format_spec(video_only=True) == "bestvideo/best"
        assert resolve_format_spec(video_only=True, resolution="1080p") == "bestvideo[height<=1080]/best[height<=1080]"

    def test_resolve_format_spec_resolution_preset(self):
        spec = resolve_format_spec(resolution="720p")
        assert "height<=720" in spec
        assert "bestvideo[height<=720]+bestaudio" in spec

    def test_resolve_format_spec_default(self):
        assert resolve_format_spec() is None


class TestNotificationSettings:
    def test_notifications_enabled_default(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"general": {}}), encoding="utf-8")
        cfg = ConfigManager(cfg_file)
        assert cfg.notifications_enabled is True

    def test_notifications_enabled_false(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"general": {"notifications": False, "play_sound": True}}), encoding="utf-8")
        cfg = ConfigManager(cfg_file)
        assert cfg.notifications_enabled is False

    def test_play_sound_false(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"general": {"notifications": True, "play_sound": False}}), encoding="utf-8")
        cfg = ConfigManager(cfg_file)
        assert cfg.notifications_enabled is False

    def test_notifications_toggle_save(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"general": {"notifications": True, "play_sound": True}}), encoding="utf-8")
        cfg = ConfigManager(cfg_file)
        assert cfg.notifications_enabled is True

        cfg.set("general", "notifications", False)
        cfg.set("general", "play_sound", False)
        cfg.save()

        cfg_reloaded = ConfigManager(cfg_file)
        assert cfg_reloaded.notifications_enabled is False


class TestInlineOptionsExtraction:
    def test_extract_audio_only(self):
        url, opts = extract_inline_options("https://www.youtube.com/watch?v=dQw4w9WgXcQ -x")
        assert url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert opts.get("audio_only") is True

    def test_extract_resolution(self):
        url, opts = extract_inline_options("https://www.youtube.com/watch?v=dQw4w9WgXcQ -r 1080p")
        assert url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert opts.get("resolution") == "1080p"

    def test_extract_media_types(self):
        url1, opts1 = extract_inline_options("https://example.com/gallery --images-only")
        assert url1 == "https://example.com/gallery"
        assert opts1.get("images_only") is True

        url2, opts2 = extract_inline_options("https://example.com/video --video-only")
        assert url2 == "https://example.com/video"
        assert opts2.get("video_only") is True

    def test_extract_silent_flag(self):
        url, opts = extract_inline_options("https://example.com/song -x --no-sound")
        assert url == "https://example.com/song"
        assert opts.get("audio_only") is True
        assert opts.get("no_sound") is True

    def test_extract_proxy(self):
        url, opts = extract_inline_options("https://example.com/file --proxy socks5://127.0.0.1:1080")
        assert url == "https://example.com/file"
        assert opts.get("proxy") == "socks5://127.0.0.1:1080"

    def test_extract_empty_or_normal(self):
        assert extract_inline_options("") == ("", {})
        assert extract_inline_options(None) == ("", {})
        url, opts = extract_inline_options("https://example.com/normal")
        assert url == "https://example.com/normal"
        assert opts == {}
