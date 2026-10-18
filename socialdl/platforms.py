"""Platform allowlist and URL safety helpers."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

from .errors import ApiError

YOUTUBE_DOMAINS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com", "ytimg.com",
)
X_DOMAINS = ("x.com", "twitter.com", "t.co")

# These platforms provide only scoped, authenticated download paths; this app
# has no user OAuth store or platform credentials, so scraping is not enabled.
OFFICIAL_API_ONLY = {
    "Pinterest": (("pinterest.com", "pin.it"), "Pinterest لا يوثق رابط فيديو خامًا في API العامة؛ يلزم تنزيله من واجهة Pinterest إذا أتاحه صاحبه."),
    "Snapchat": (("snapchat.com", "snap.com"), "تنزيل Snapchat يتطلب أصول Public Profile API مخوّلة عبر OAuth وقائمة سماح."),
    "LinkedIn": (("linkedin.com", "lnkd.in"), "تنزيل LinkedIn يتطلب video URN وصلاحيات API معتمدة للمحتوى المخوّل."),
    "Vimeo": (("vimeo.com",), "تنزيل Vimeo يتطلب فيديو مخوّلًا ورمز API بنطاق video_files؛ هذه الخدمة لا تتصل بحسابك."),
    "Twitch": (("twitch.tv",), "تنزيل Twitch Clips يتطلب OAuth ودور محرر القناة؛ VOD يخص مالك القناة."),
}

PLATFORMS = {
    "Facebook": ("facebook.com", "fb.watch", "fb.me"),
    "Instagram": ("instagram.com",),
    "TikTok": ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
    "Reddit": ("reddit.com", "redd.it"),
    "Dailymotion": ("dailymotion.com", "dai.ly"),
    "SoundCloud": ("soundcloud.com",),
    "Flickr": ("flickr.com",),
    "Streamable": ("streamable.com",),
}
SHORTENERS = ("bit.ly", "tinyurl.com", "is.gd", "ow.ly", "buff.ly", "goo.gl")
MAX_URL_LENGTH = 2048
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE_RE = re.compile(r"^[\w-]+(?:\.[\w-]+)+(?:/\S*)?$", re.UNICODE)


def host_matches(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def is_youtube_host(host: str) -> bool:
    return host_matches(host, YOUTUBE_DOMAINS)


def provider_for_host(host: str) -> str | None:
    for name, domains in PLATFORMS.items():
        if host_matches(host, domains):
            return name
    return None


def _official_api_only(host: str) -> tuple[str, str] | None:
    for name, (domains, message) in OFFICIAL_API_ONLY.items():
        if host_matches(host, domains):
            return name, message
    return None


def _public_addresses(host: str) -> bool:
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    addresses = {record[4][0] for record in records}
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if not ip.is_global:
                return False
        except ValueError:
            return False
    return True


def is_public_url(url: str) -> bool:
    """Reject non-HTTP URLs and any host resolving to a non-global IP."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme.lower() not in ("http", "https") or not host:
            return False
        if parts.port not in (None, 80, 443):
            return False
        if parts.username is not None or parts.password is not None:
            return False
        return _public_addresses(host)
    except (ValueError, OSError):
        return False


def clean_url(raw: str | None) -> str:
    """Extract and validate one allowlisted public social-media URL."""
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw or len(raw) > MAX_URL_LENGTH or any(ord(ch) < 32 for ch in raw):
        raise ApiError("invalid_url", "أدخل رابطًا صحيحًا وقصيرًا.", 400)
    match = _URL_RE.search(raw)
    if match:
        url = match.group(0).rstrip(".,;)!?")
    elif _BARE_RE.fullmatch(raw):
        url = "https://" + raw
    else:
        raise ApiError("invalid_url", "لم أتمكن من العثور على رابط صالح.", 400)

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        _ = parts.port
        if not host or parts.username is not None or parts.password is not None:
            raise ValueError
    except ValueError:
        raise ApiError("invalid_url", "صيغة الرابط غير صحيحة.", 400)

    if is_youtube_host(host):
        raise ApiError("youtube_unsupported", "تنزيل YouTube غير مدعوم.", 422)
    if host_matches(host, X_DOMAINS):
        raise ApiError("official_api_required", "لا نسحب من موقع X آليًا؛ تنزيله يحتاج تكاملًا مصرحًا عبر واجهة X الرسمية.", 422)
    restricted = _official_api_only(host)
    if restricted:
        raise ApiError("official_api_required", restricted[1], 422)
    if provider_for_host(host) is None and not host_matches(host, SHORTENERS):
        raise ApiError("unsupported", "هذا النطاق غير مدرج ضمن المواقع المدعومة.", 422)
    if not is_public_url(url):
        raise ApiError("forbidden_url", "تعذر التحقق من أن عنوان الرابط عام وآمن.", 403)
    return url
