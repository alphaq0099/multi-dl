"""
URL Router for MULTI_DOWNLOADER.
Directs incoming URLs to the appropriate backend using a 3-tier matching engine.
"""

from typing import Any, Dict, Optional
from urllib.parse import urlparse

from backends.base import BaseBackend
from core.exceptions import UnsupportedURLError


class URLRouter:
    """
    3-Tier URL routing engine:
    1. Fast domain dictionary match (covers 90%+ cases)
    2. Deep backend can_handle() check
    3. Fallback priority chain
    """

    # Tier 1 lookup table mapping domains to backend names
    DOMAIN_MAP: Dict[str, str] = {
        # yt-dlp
        "youtube.com": "yt-dlp",
        "www.youtube.com": "yt-dlp",
        "m.youtube.com": "yt-dlp",
        "youtu.be": "yt-dlp",
        "twitch.tv": "yt-dlp",
        "www.twitch.tv": "yt-dlp",
        "tiktok.com": "yt-dlp",
        "www.tiktok.com": "yt-dlp",
        "vimeo.com": "yt-dlp",
        "soundcloud.com": "yt-dlp",
        "dailymotion.com": "yt-dlp",

        # gallery-dl
        "twitter.com": "gallery-dl",
        "www.twitter.com": "gallery-dl",
        "x.com": "gallery-dl",
        "www.x.com": "gallery-dl",
        "reddit.com": "gallery-dl",
        "www.reddit.com": "gallery-dl",
        "pixiv.net": "gallery-dl",
        "www.pixiv.net": "gallery-dl",
        "instagram.com": "gallery-dl",
        "www.instagram.com": "gallery-dl",
        "imgur.com": "gallery-dl",
        "danbooru.donmai.us": "gallery-dl",
        "deviantart.com": "gallery-dl",
        "nhentai.net": "gallery-dl",
        "www.nhentai.net": "gallery-dl",

        # Common lockers where gallery-dl extractors are faster, more reliable, and do not drop files
        "bunkr.si": "gallery-dl",
        "bunkr.is": "gallery-dl",
        "bunkr.cr": "gallery-dl",
        "bunkr.black": "gallery-dl",
        "bunkr.site": "gallery-dl",
        "bunkr.ws": "gallery-dl",
        "bunkr.ac": "gallery-dl",
        "gofile.io": "gallery-dl",
        "pixeldrain.com": "gallery-dl",
        "coomer.su": "gallery-dl",
        "coomer.party": "gallery-dl",
        "kemono.su": "gallery-dl",
        "kemono.party": "gallery-dl",
        "catbox.moe": "gallery-dl",
        "files.catbox.moe": "gallery-dl",
        "erome.com": "gallery-dl",
        "fapello.com": "gallery-dl",
        "cyberdrop.me": "gallery-dl",
        # Forums & Community Boards (gallery-dl has native extractors for XenForo, vBulletin, phpBB)
        "socialmediagirls.com": "gallery-dl",
        "forums.socialmediagirls.com": "gallery-dl",
        "simpcity.su": "gallery-dl",
        "simpcity.to": "gallery-dl",
        "simpcity.is": "gallery-dl",
        "vipergirls.to": "gallery-dl",

        # cyberdrop-dl (Primary for deep forum threads and hosts without gallery-dl support)
        "hotpic.cc": "cyberdrop-dl",
        "www.hotpic.cc": "cyberdrop-dl",
        "hotpic2.cc": "cyberdrop-dl",
        "www.hotpic2.cc": "cyberdrop-dl",
        "2385290.xyz": "cyberdrop-dl",
        "myhostdata.space": "cyberdrop-dl",
        "saint.to": "cyberdrop-dl",
        "f95zone.to": "cyberdrop-dl",
        "mega.nz": "cyberdrop-dl",
        "sendvid.com": "cyberdrop-dl",
        "streamtape.com": "cyberdrop-dl",
        "puter.com": "cyberdrop-dl",
        "jpg.church": "cyberdrop-dl",

        # telegram-dl
        "t.me": "telegram-dl",
        "telegram.me": "telegram-dl",

        # terabox-dl
        "terabox.com": "terabox-dl",
        "www.terabox.com": "terabox-dl",
        "1024tera.com": "terabox-dl",
        "www.1024tera.com": "terabox-dl",
        "1024terabox.com": "terabox-dl",
        "www.1024terabox.com": "terabox-dl",
        "teraboxlink.com": "terabox-dl",
        "www.teraboxlink.com": "terabox-dl",
        "freeterabox.com": "terabox-dl",
        "www.freeterabox.com": "terabox-dl",
        "mirrobox.com": "terabox-dl",
        "www.mirrobox.com": "terabox-dl",
        "nephobox.com": "terabox-dl",
        "www.nephobox.com": "terabox-dl",
        "4funbox.com": "terabox-dl",
        "www.4funbox.com": "terabox-dl",
        "teraboxapp.com": "terabox-dl",
        "www.teraboxapp.com": "terabox-dl",
        "tibibox.com": "terabox-dl",
        "www.tibibox.com": "terabox-dl",
        "momobox.com": "terabox-dl",
        "www.momobox.com": "terabox-dl",
    }

    def __init__(self, backends: Optional[Dict[str, BaseBackend]] = None):
        self.backends: Dict[str, BaseBackend] = backends or {}

    def route(self, url: str, backend_override: Optional[str] = None) -> BaseBackend:
        """
        Identify and return the proper backend instance for the provided URL.
        """
        # Manual user override
        if backend_override and backend_override != "auto":
            clean_name = backend_override.lower().replace("_", "-")
            if clean_name in self.backends:
                return self.backends[clean_name]
            raise UnsupportedURLError(f"Specified backend '{backend_override}' is not registered.")

        # Tier 1: Fast domain lookup
        parsed = urlparse(url)
        domain = (parsed.netloc or "").lower().split(":")[0]  # strip port if present
        
        target_name = self.DOMAIN_MAP.get(domain)
        if target_name and target_name in self.backends:
            return self.backends[target_name]

        # Tier 1b: Domain boundary lookup (subdomain.example.com -> example.com)
        for d, b in self.DOMAIN_MAP.items():
            if (domain == d or domain.endswith("." + d)) and b in self.backends:
                return self.backends[b]

        # Tier 2: Query each registered backend's can_handle()
        for name, backend in self.backends.items():
            try:
                if backend.can_handle(url):
                    return backend
            except Exception:
                continue

        # Tier 2b: Dynamic heuristic identification before fallback
        try:
            detected = self.detect_backend_name(url)
            if detected and detected in self.backends:
                return self.backends[detected]
        except Exception:
            pass

        # Tier 3: Fallback priority (gallery-dl then yt-dlp)
        for fallback_name in ["gallery-dl", "yt-dlp"]:
            if fallback_name in self.backends:
                return self.backends[fallback_name]

        raise UnsupportedURLError(f"No backend available to handle URL: {url}")

    @classmethod
    def match(cls, url: str):
        """
        Convenience matching helper returning an object with .backend attribute and str representation.
        """
        backend_name = cls.detect_backend_name(url)
        class MatchResult:
            def __init__(self, b: str):
                self.backend = b
            def __str__(self):
                return self.backend
            def __repr__(self):
                return f"<MatchResult backend='{self.backend}'>"
        return MatchResult(backend_name)

    @classmethod
    def detect_backend_name(cls, url: str, backend_override: Optional[str] = None) -> str:
        """
        Lightweight, fast detection of backend engine name without instantiating heavy backends.
        Uses Tier 1 DOMAIN_MAP, fast regex checks, and domain heuristics.
        """
        if backend_override and backend_override != "auto":
            return backend_override.lower().replace("_", "-")

        parsed = urlparse(url)
        domain = (parsed.netloc or "").lower().split(":")[0]
        url_lower = url.lower()

        # Tier 1 direct match
        if domain in cls.DOMAIN_MAP:
            return cls.DOMAIN_MAP[domain]

        # Domain boundary matching (e.g. sub.domain.com matching domain.com)
        for d, b in cls.DOMAIN_MAP.items():
            if domain == d or domain.endswith("." + d):
                return b

        # Telegram detection
        if "t.me" in domain or "telegram.me" in domain or url.startswith("t.me/"):
            return "telegram-dl"

        # TeraBox mirror detection
        if any(t in domain for t in ("terabox", "1024tera", "teraboxlink", "freeterabox", "mirrobox", "nephobox", "4funbox", "teraboxapp", "tibibox", "momobox")):
            return "terabox-dl"

        # Direct video files (.mp4, .mkv, .webm, .mov, etc.) route to yt-dlp
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(v_ext) for v_ext in (".mp4", ".mkv", ".webm", ".m4v", ".mov", ".mp3", ".m4a", ".flv")):
            return "yt-dlp"

        # Forum indicators (XenForo / vBulletin / phpBB forum threads and posts)
        if (
            "/threads/" in url_lower
            or "/posts/" in url_lower
            or "/post-" in url_lower
            or any(f in domain for f in ("forum.", "forums.", "board.", "community."))
        ):
            try:
                import gallery_dl.extractor
                if gallery_dl.extractor.find(url):
                    return "gallery-dl"
            except Exception:
                pass
            return "gallery-dl"

        # Fast query of gallery-dl registered extractors (instantaneous in-memory regex match)
        try:
            import gallery_dl.extractor
            g_ext = gallery_dl.extractor.find(url)
            if g_ext and g_ext.__class__.__name__ != "DirectlinkExtractor":
                return "gallery-dl"
            elif g_ext and any(path_lower.endswith(i_ext) for i_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
                return "gallery-dl"
        except Exception:
            pass

        # Cyberdrop-dl hosts (hotpic, etc.)
        if any(h in domain for h in ("hotpic", "saint.to", "f95zone", "puter.com", "streamtape")):
            return "cyberdrop-dl"

        # Image/gallery heuristic vs video/audio
        if any(img in domain for img in ("image", "photo", "pic", "gallery", "album", "danbooru", "gelbooru")):
            return "gallery-dl"

        return "yt-dlp"

    @classmethod
    def create_default(cls, config: Optional[Any] = None) -> "URLRouter":
        """Instantiates all production backends with configured settings and returns a fully initialized router.

        anypic-dl registers last on purpose: Tier-2 routing consults
        ``can_handle`` in dict order, and anypic only claims URLs no other
        engine wanted.
        """
        from backends.anypic_backend import AnyPicBackend
        from backends.cyberdrop_backend import CyberdropDlBackend
        from backends.gallerydl_backend import GalleryDlBackend
        from backends.telegram_backend import TelegramDlBackend
        from backends.terabox_backend import TeraboxDlBackend
        from backends.ytdlp_backend import YtDlpBackend

        if config is None:
            from core.config import ConfigManager
            config = ConfigManager()

        return cls({
            "yt-dlp": YtDlpBackend(config.get_backend_config("yt-dlp")),
            "gallery-dl": GalleryDlBackend(config.get_backend_config("gallery-dl")),
            "telegram-dl": TelegramDlBackend(config.get_backend_config("telegram-dl")),
            "terabox-dl": TeraboxDlBackend(config.get_backend_config("terabox-dl")),
            "cyberdrop-dl": CyberdropDlBackend(config.get_backend_config("cyberdrop-dl")),
            "anypic-dl": AnyPicBackend(config.get_backend_config("anypic-dl")),
        })

    @staticmethod
    def get_host_identifier(url: str, backend_name: str) -> str:
        """
        Derives a clean host/site or channel folder name for organizing downloads.
        Examples:
          - youtube.com/shorts/... -> 'youtube'
          - twitter.com/... -> 'twitter'
          - t.me/channel_name/123 -> 'channel_name'
          - t.me/c/12345/678 -> 'chat_12345'
        """
        parsed = urlparse(url)
        domain = (parsed.netloc or "").lower().split(":")[0]

        # Telegram specific extraction (channel/chat name)
        if "telegram" in backend_name or "t.me" in domain:
            import re
            m = re.search(r'(?:https?://)?(?:t|telegram)\.me/(c/)?([a-zA-Z0-9_.-]+)', url)
            if m:
                is_private = bool(m.group(1))
                name = m.group(2)
                return f"chat_{name}" if is_private else name
            return "telegram"

        # YouTube specific normalization (youtube.com, youtu.be -> youtube)
        if "youtube" in domain or "youtu.be" in domain:
            return "youtube"

        # TeraBox specific normalization
        if any(d in domain for d in ("terabox", "1024tera", "teraboxlink", "freeterabox", "mirrobox", "nephobox", "4funbox", "teraboxapp", "tibibox", "momobox")):
            return "terabox"

        # Locker & community host normalization
        if "bunkr" in domain:
            return "bunkr"
        if "catbox" in domain:
            return "catbox"
        if "coomer" in domain:
            return "coomer"
        if "kemono" in domain:
            return "kemono"


        # General domain simplification (e.g. www.twitter.com -> twitter)
        parts = domain.split(".")
        if len(parts) >= 2:
            # Drop www / m subdomains
            if parts[0] in ("www", "m"):
                parts = parts[1:]
            # Return primary domain name (e.g. twitter, instagram, pixiv)
            return parts[0] if parts else domain

        return domain or "misc"

    @staticmethod
    def is_container_url(url: str, backend_name: Optional[str] = None) -> bool:
        """
        Determines whether a URL represents a collection/container (playlist, album, channel,
        user bookmark folder, subreddit, forum thread, or locker album) that contains multiple
        items and may receive new items over time.

        Container URLs should not be skipped as a whole at the queue level just because they
        were processed once. Instead, their individual items are checked against backend archives.
        """
        if not url:
            return False

        parsed = urlparse(url)
        domain = (parsed.netloc or "").lower().split(":")[0]
        path = parsed.path.rstrip("/")
        path_lower = path.lower()
        query = (parsed.query or "").lower()

        # Direct media files are NEVER containers
        if any(path_lower.endswith(ext) for ext in (
            ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".mp3", ".m4a", ".flac",
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".rar", ".7z", ".pdf"
        )):
            return False

        # 1. YouTube & video platforms
        if "youtube" in domain or "youtu.be" in domain:
            if "list=" in query:
                return True
            if any(path_lower.startswith(p) for p in ("/channel/", "/c/", "/user/", "/@")):
                return True
            return False

        if "vimeo.com" in domain:
            if any(p in path_lower for p in ("/channels/", "/groups/", "/album/")):
                return True

        if "tiktok.com" in domain:
            if "/@" in path_lower and "/video/" not in path_lower:
                return True
            return False

        # 2. Instagram
        if "instagram.com" in domain:
            if any(s in path_lower for s in ("/saved", "/all-posts", "/bookmarks", "/stories", "/tagged")):
                return True
            if any(s in path_lower for s in ("/p/", "/reel/", "/tv/")):
                return False
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1 and parts[0] not in ("explore", "direct", "accounts"):
                return True
            return False

        # 3. Twitter / X
        if "twitter.com" in domain or "x.com" in domain:
            if "/status/" in path_lower or "/i/web/status/" in path_lower:
                return False
            if any(s in path_lower for s in ("/likes", "/media", "/bookmarks")):
                return True
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1 and parts[0] not in ("home", "explore", "messages", "settings"):
                return True
            return False

        # 4. Reddit
        if "reddit.com" in domain:
            if "/comments/" in path_lower:
                return False
            if "/r/" in path_lower or "/user/" in path_lower:
                return True
            return False

        # 5. Lockers (Bunkr, Coomer, Kemono, Cyberdrop, Saint, Gofile)
        if "bunkr" in domain:
            if "/a/" in path_lower or "/album/" in path_lower:
                return True
            return False

        if any(d in domain for d in ("coomer", "kemono")):
            if "/post/" in path_lower:
                return False
            if "/user/" in path_lower:
                return True
            return False

        if any(d in domain for d in ("cyberdrop", "saint.to", "gofile")):
            if "/a/" in path_lower or "/album/" in path_lower or "/d/" in path_lower:
                return True

        # 6. Forums & Community Threads
        if any(f in domain for f in ("forum", "forums", "board", "community", "simpcity", "socialmediagirls", "vipergirls")):
            if any(t in path_lower for t in ("/threads/", "/thread-", "/forum/", "/forums/", "/boards/")):
                return True

        # 7. Telegram
        if "t.me" in domain or "telegram.me" in domain:
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1:
                return True
            elif len(parts) == 2 and parts[0] == "c":
                return True
            return False

        # 8. TeraBox (share folder links often have many files)
        if any(d in domain for d in ("terabox", "1024tera", "teraboxlink", "freeterabox", "mirrobox", "nephobox", "4funbox", "teraboxapp", "tibibox", "momobox")):
            return True

        # 9. Pixiv
        if "pixiv.net" in domain:
            if any(p in path_lower for p in ("/users/", "/bookmarks", "/series/")):
                return True
            if "/artworks/" in path_lower:
                return False

        return False


def parse_resolution_height(resolution_str: Optional[str]) -> Optional[int]:
    """Extracts integer pixel height from resolution string like '1080p', '1080', '720p', '4k', etc."""
    if not resolution_str:
        return None
    r = str(resolution_str).strip().lower()
    if r in ("4k", "2160", "2160p", "uhd"):
        return 2160
    if r in ("2k", "1440", "1440p", "qhd"):
        return 1440
    if r in ("1080", "1080p", "fhd"):
        return 1080
    if r in ("720", "720p", "hd"):
        return 720
    if r in ("480", "480p", "sd"):
        return 480
    if r in ("360", "360p"):
        return 360
    import re
    digits = re.findall(r"\d+", r)
    if digits:
        return int(digits[0])
    return None


def resolve_format_spec(
    format_str: Optional[str] = None,
    resolution: Optional[str] = None,
    audio_only: bool = False,
    video_only: bool = False,
) -> Optional[str]:
    """Generates optimal yt-dlp format specification from format string, resolution, or type filters."""
    if format_str:
        return format_str
    if audio_only:
        return "bestaudio/best"
    height = parse_resolution_height(resolution)
    if video_only:
        if height:
            return f"bestvideo[height<={height}]/best[height<={height}]"
        return "bestvideo/best"
    if height:
        return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    return None
