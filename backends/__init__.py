"""
Exposes all backend adapters.
"""

from backends.base import BaseBackend
from backends.ytdlp_backend import YtDlpBackend
from backends.gallerydl_backend import GalleryDlBackend
from backends.telegram_backend import TelegramDlBackend
from backends.terabox_backend import TeraboxDlBackend
from backends.cyberdrop_backend import CyberdropDlBackend
from backends.anypic_backend import AnyPicBackend

__all__ = [
    "BaseBackend",
    "YtDlpBackend",
    "GalleryDlBackend",
    "TelegramDlBackend",
    "TeraboxDlBackend",
    "CyberdropDlBackend",
    "AnyPicBackend",
]
