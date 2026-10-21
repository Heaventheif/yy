"""Platform allowlist and URL safety helpers."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
SHORTENERS = (
    "bit.ly", "bitly.com", "tinyurl.com", "is.gd", "v.gd", "ow.ly", "buff.ly", "goo.gl", "t.co", "lnkd.in",
    "rebrand.ly", "cutt.ly", "shorturl.at", "t.ly", "s.id", "rb.gy", "tiny.cc", "bl.ink", "soo.gd",
    "short.io", "tr.ee", "vk.cc", "amzn.to", "wp.me", "dlvr.it", "snip.ly", "clck.ru", "l.wl.co", "linktr.ee",
)
# Hosts whose short links must be expanded before yt-dlp can use them.
ALIAS_HOSTS = (
    "fb.watch", "fb.me", "fb.gg", "vm.tiktok.com", "vt.tiktok.com", "redd.it", "dai.ly",
    "on.soundcloud.com", "snd.sc", "instagr.am", "pin.it", "flic.kr",
)
# Redirect wrappers whose real target is a query parameter (unwrapped without any network call).
_WRAPPERS = (
    (("google.com", "www.google.com"), "/url", ("url", "q")),
    (("l.facebook.com", "lm.facebook.com"), "/l.php", ("u",)),
    (("l.instagram.com",), "/", ("u",)),
    (("out.reddit.com",), "/", ("url",)),
    (("away.vk.com",), "/away.php", ("to",)),
    (("t.umblr.com",), "/redirect", ("z",)),
    (("steamcommunity.com",), "/linkfilter/", ("u", "url")),
    (("youtube.com", "www.youtube.com", "m.youtube.com"), "/redirect", ("q",)),
)
_TRACKING = {
    "fbclid", "gclid", "gbraid", "wbraid", "igshid", "igsh", "ref_src", "ref_url", "mc_cid", "mc_eid",
    "_branch_match_id", "s_cid", "mibextid", "sfnsn", "share_id", "is_from_webapp", "sender_device",
    "sender_web_id", "_r", "_t", "ttclid", "twclid", "msclkid", "feature", "app", "context",
}
MAX_URL_LENGTH = 2048
# Open mode: accept any public site that a dedicated yt-dlp extractor supports,
# except YouTube. Set ALLOW_ALL_SITES=0 to go back to the strict allowlist.
ALLOW_ALL_SITES = os.getenv("ALLOW_ALL_SITES", "1").strip().lower() not in ("0", "false", "no")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE_RE = re.compile(r"^[\w-]+(?:\.[\w-]+)+(?:/\S*)?$", re.UNICODE)


def unwrap_redirect(url: str) -> str:
    """Resolve known tracking/redirect wrappers locally (no network)."""
    for _ in range(3):
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        host = (parts.hostname or "").lower().rstrip(".")
        hit = None
        for hosts, path, params in _WRAPPERS:
            if host in hosts and (parts.path == path or (path != "/" and parts.path.startswith(path))
                                  or (path == "/" and parts.path in ("", "/"))):
                query = dict(parse_qsl(parts.query))
                hit = next((query[p] for p in params if query.get(p, "").lower().startswith(("http://", "https://"))), None)
                break
        if not hit:
            return url
        url = hit
    return url


def normalize_url(url: str) -> str:
    """Lower-case the host, drop the fragment and well-known tracking parameters."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING and not k.lower().startswith("utm_")]
    netloc = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, urlencode(query, safe="/:@,+"), ""))


def host_matches(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def is_youtube_host(host: str) -> bool:
    return host_matches(host, YOUTUBE_DOMAINS)


def site_allowed(host: str) -> bool:
    """True if the host may be processed (YouTube is always refused)."""
    if is_youtube_host(host):
        return False
    if ALLOW_ALL_SITES:
        return True
    return provider_for_host(host) is not None or host_matches(host, SHORTENERS)


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
        url = match.group(0).rstrip(".,;:)]}!?،؛؟»”’\u200f\u200e")
    elif _BARE_RE.fullmatch(raw):
        url = "https://" + raw
    else:
        raise ApiError("invalid_url", "لم أتمكن من العثور على رابط صالح.", 400)

    original_host = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        url = normalize_url(unwrap_redirect(url))
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        _ = parts.port
        if not host or parts.username is not None or parts.password is not None:
            raise ValueError
    except ValueError:
        raise ApiError("invalid_url", "صيغة الرابط غير صحيحة.", 400)

    if is_youtube_host(host):
        if not is_youtube_host(original_host):  # wrapper (e.g. google.com/url?q=...) that points to YouTube
            raise ApiError("youtube_redirect", "الرابط يقود إلى YouTube؛ يُعالَج عبر مسار YouTube الخاص.", 422,
                           details={"url": url}, retryable=False)
        raise ApiError("youtube_unsupported", "تنزيل YouTube غير مدعوم.", 422)
    if not ALLOW_ALL_SITES and host_matches(host, X_DOMAINS):
        raise ApiError("official_api_required", "لا نسحب من موقع X آليًا؛ تنزيله يحتاج تكاملًا مصرحًا عبر واجهة X الرسمية.", 422)
    restricted = None if ALLOW_ALL_SITES else _official_api_only(host)
    if restricted:
        raise ApiError("official_api_required", restricted[1], 422)
    if not site_allowed(host):
        raise ApiError("unsupported", "هذا النطاق غير مدرج ضمن المواقع المدعومة.", 422)
    if not is_public_url(url):
        raise ApiError("forbidden_url", "تعذر التحقق من أن عنوان الرابط عام وآمن.", 403)
    return url
