"""
Unit tests for TelegramDlBackend and Telegram URL routing.
"""

from datetime import datetime
import pytest
from unittest.mock import MagicMock

from backends.telegram_backend import TelegramDlBackend, _clean_filename, _get_file_size, _get_media_type, _get_message_filename
from core.exceptions import AuthenticationError
from core.models import MediaType
from core.router import URLRouter


def test_telegram_can_handle():
    backend = TelegramDlBackend()

    # Valid Telegram URLs
    assert backend.can_handle("https://t.me/channel_name/123") is True
    assert backend.can_handle("https://telegram.me/channel_name/123") is True
    assert backend.can_handle("https://t.me/c/1234567890/100") is True
    assert backend.can_handle("https://t.me/c/1234567890/45/678") is True
    assert backend.can_handle("https://t.me/public_channel") is True
    assert backend.can_handle("http://telegram.me/c/987654321") is True

    # Non-Telegram URLs
    assert backend.can_handle("https://youtube.com/watch?v=123") is False
    assert backend.can_handle("https://twitter.com/user/status/123") is False
    assert backend.can_handle("https://terabox.com/s/1abc") is False


def test_parse_link_public():
    # Public channel message
    chat, msg_id, topic_id = TelegramDlBackend.parse_link("https://t.me/durov/123")
    assert chat == "durov"
    assert msg_id == 123
    assert topic_id is None

    # Public channel root
    chat, msg_id, topic_id = TelegramDlBackend.parse_link("https://t.me/telegram")
    assert chat == "telegram"
    assert msg_id is None
    assert topic_id is None


def test_parse_link_private():
    # Private channel / supergroup message (-100 prefix)
    chat, msg_id, topic_id = TelegramDlBackend.parse_link("https://t.me/c/1234567890/456")
    assert chat == -1001234567890
    assert msg_id == 456
    assert topic_id is None

    # Private supergroup root
    chat, msg_id, topic_id = TelegramDlBackend.parse_link("https://t.me/c/1234567890")
    assert chat == -1001234567890
    assert msg_id is None
    assert topic_id is None


def test_parse_link_forum_topic():
    # Forum topic with thread ID
    chat, msg_id, topic_id = TelegramDlBackend.parse_link("https://t.me/c/1234567890/42/999")
    assert chat == -1001234567890
    assert msg_id == 999
    assert topic_id == 42


def test_parse_link_invalid():
    chat, msg_id, topic_id = TelegramDlBackend.parse_link("https://not-telegram.org/abc")
    assert chat is None
    assert msg_id is None
    assert topic_id is None


def test_clean_filename():
    assert _clean_filename('bad:file/name?*.mp4') == "bad_file_name__.mp4"
    assert _clean_filename('   ') == "unnamed_media"
    assert _clean_filename('normal_video.mkv') == "normal_video.mkv"


def test_get_media_type():
    # No media
    msg = MagicMock(media=None)
    assert _get_media_type(msg) == MediaType.UNKNOWN

    # Photo
    msg = MagicMock(media=True, photo=MagicMock())
    assert _get_media_type(msg) == MediaType.IMAGE

    # Video
    msg = MagicMock(media=True, photo=None, video=MagicMock(), gif=None)
    assert _get_media_type(msg) == MediaType.VIDEO

    # Audio
    msg = MagicMock(media=True, photo=None, video=None, gif=None, audio=MagicMock(), voice=None)
    assert _get_media_type(msg) == MediaType.AUDIO

    # Document with mime
    doc_mock = MagicMock(mime_type="application/pdf")
    msg = MagicMock(media=True, photo=None, video=None, audio=None, voice=None, gif=None, document=doc_mock)
    assert _get_media_type(msg) == MediaType.DOCUMENT

    doc_img = MagicMock(mime_type="image/webp")
    msg = MagicMock(media=True, photo=None, video=None, audio=None, voice=None, gif=None, document=doc_img)
    assert _get_media_type(msg) == MediaType.IMAGE


def test_get_file_size():
    # Message with file.size
    file_mock = MagicMock()
    file_mock.size = 1048576
    msg = MagicMock(media=True, file=file_mock)
    assert _get_file_size(msg) == 1048576

    # Message with media.document.size
    doc_mock = MagicMock(size=2048)
    msg = MagicMock(media=MagicMock(document=doc_mock), file=None)
    assert _get_file_size(msg) == 2048

    # Empty
    msg = MagicMock(media=None)
    assert _get_file_size(msg) == 0


def test_get_message_filename():
    # Message with file.name
    file_mock = MagicMock()
    file_mock.name = "presentation.pdf"
    msg = MagicMock(media=True, file=file_mock)
    assert _get_message_filename(msg) == "presentation.pdf"

    # Message without file.name generates fallback
    file_mock_no_name = MagicMock()
    file_mock_no_name.name = None
    file_mock_no_name.ext = ".jpg"
    msg = MagicMock(
        media=True,
        photo=MagicMock(),
        file=file_mock_no_name,
        date=datetime(2026, 10, 7, 12, 30, 0),
        id=4242,
    )
    fname = _get_message_filename(msg)
    assert fname == "image_20261007_123000_4242.jpg"


def test_credentials_resolution():
    backend = TelegramDlBackend({
        "api_id": "12345",
        "api_hash": "abcdef",
        "session_path": "custom_session",
    })
    api_id, api_hash, session = backend._resolve_credentials()
    assert api_id == 12345
    assert api_hash == "abcdef"
    assert session == "custom_session"


def test_router_integration():
    backend = TelegramDlBackend()
    router = URLRouter({"telegram-dl": backend})

    # Route public URL
    routed = router.route("https://t.me/durov/123")
    assert routed.name == "telegram-dl"

    # Route private URL
    routed_priv = router.route("https://t.me/c/123456/789")
    assert routed_priv.name == "telegram-dl"

    # Host identifier resolution
    host_pub = URLRouter.get_host_identifier("https://t.me/durov/123", "telegram-dl")
    assert host_pub == "durov"

    host_priv = URLRouter.get_host_identifier("https://t.me/c/123456/789", "telegram-dl")
    assert host_priv == "chat_123456"


def test_prompt_api_credentials(monkeypatch, tmp_path):
    cfg_file = tmp_path / "telegram-dl.json"
    backend = TelegramDlBackend({"config_file": str(cfg_file)})

    inputs = iter(["998877", "0123456789abcdef0123456789abcdef"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    api_id, api_hash = backend._prompt_api_credentials()
    assert api_id == 998877
    assert api_hash == "0123456789abcdef0123456789abcdef"
    assert cfg_file.exists()


@pytest.mark.asyncio
async def test_non_interactive_auth_error(monkeypatch):
    backend = TelegramDlBackend({"api_id": None, "api_hash": None})
    monkeypatch.setattr(backend, "_resolve_credentials", lambda: (None, None, "test_session"))
    monkeypatch.setattr("core.terminal.Style.is_interactive", lambda: False)

    with pytest.raises(AuthenticationError) as exc_info:
        await backend._ensure_client()
    assert "Telegram API credentials not found" in str(exc_info.value)
