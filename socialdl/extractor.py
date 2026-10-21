"""Public-media extraction using yt-dlp's maintained site extractors."""
from __future__ import annotations

import html
import re
import threading
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests
import yt_dlp

from .errors import ApiError, classify_extractor_error
from .fbshare import facebook_share_to_id_url
from .platforms import ALIAS_HOSTS, SHORTENERS, normalize_url, unwrap_redirect, clean_url, host_matches, is_public_url, is_youtube_host, provider_for_host, site_allowed

_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_META_URL = re.compile(
    r'<meta[^>]+property=["\']og:url["\'][^>]*content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:url["\']|'
    r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', re.IGNORECASE,
)
_FB_VIDEO_ID = re.compile(r'"(?:video_id|videoId)"\s*:\s*"(\d{6,})"')
_FB_PATH_ID = re.compile(r'facebook\.com\\?/(?:reel|watch\\?/\\?\?v=)(?:\\?/)?(\d{6,})|\\?/videos\\?/(\d{6,})')
_FB_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
_FB_LOGIN_PATHS = ("/login", "/checkpoint", "/recover", "/two_step_verification")


def _fb_clean_candidate(url: str | None) -> str | None:
    """Facebook sends cloud IPs to /login/?next=<real url>: unwrap it, or drop it if there is no target."""
    if not url:
        return None
    parts = urlsplit(url)
    if host_matches((parts.hostname or "").lower(), ("facebook.com",)) and parts.path.lower().startswith(_FB_LOGIN_PATHS):
        nxt = dict(parse_qsl(parts.query)).get("next", "")
        if nxt.startswith("/"):
            nxt = urljoin(url, nxt)
        return nxt if nxt.lower().startswith(("http://", "https://")) else None
    return url


class _QuietLogger:
    def debug(self, _message):
        pass

    def info(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass


def _page_target(body: str, base: str) -> str | None:
    for match in _META_URL.finditer(body or ""):
        raw = next((value for value in match.groups() if value), None)
        if not raw:
            continue
        candidate = urljoin(base, html.unescape(raw))
        candidate_path = urlsplit(candidate).path
        if candidate.rstrip("/") != base.rstrip("/") and candidate_path not in ("", "/", "/share"):
            return candidate
    if "facebook.com" in (urlsplit(base).hostname or ""):
        match = _FB_VIDEO_ID.search(body or "")
        if match:
            return f"https://www.facebook.com/watch/?v={match.group(1)}"
        match = _FB_PATH_ID.search(body or "")
        if match:
            return f"https://www.facebook.com/watch/?v={match.group(1) or match.group(2)}"
    return None


def _needs_expand(url: str) -> bool:
    """True only for short links and real share links (not ordinary video pages)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path.lower()
    if host_matches(host, SHORTENERS) or host_matches(host, ALIAS_HOSTS):
        return True
    if host_matches(host, ("facebook.com", "instagram.com")):
        return path.startswith("/share")
    if host_matches(host, ("tiktok.com",)):
        return path.startswith("/t/")
    if host_matches(host, ("reddit.com",)):
        return "/s/" in path
    return False


def _canonical_share_target(url: str) -> str | None:
    """Ask yt-dlp's extractor for the canonical URL without processing formats (fast)."""
    try:
        with yt_dlp.YoutubeDL(_options()) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    if info.get("_type") in ("url", "url_transparent"):
        target = info.get("url") or info.get("webpage_url")
    else:
        target = info.get("webpage_url")
    if not isinstance(target, str) or not target.lower().startswith(("http://", "https://")) or target == url:
        return None
    return target


_EXPAND_TTL = 600
_EXPAND_MAX = 2000
_expand_cache: dict[str, tuple[float, str]] = {}
_expand_lock = threading.Lock()


def _youtube_redirect(target: str) -> ApiError:
    return ApiError("youtube_redirect", "الرابط يقود إلى YouTube؛ يُعالَج عبر مسار YouTube الخاص.", 422,
                    details={"url": target}, retryable=False)


def expand_url(url: str, max_hops: int = 6) -> str:
    """Expand short/share links (cached); every redirect target is validated."""
    now = time.monotonic()
    with _expand_lock:
        hit = _expand_cache.get(url)
        if hit and now - hit[0] < _EXPAND_TTL:
            return hit[1]
    result = _expand_uncached(url, max_hops)
    with _expand_lock:
        if len(_expand_cache) >= _EXPAND_MAX:
            for old in sorted(_expand_cache, key=lambda k: _expand_cache[k][0])[:_EXPAND_MAX // 4]:
                _expand_cache.pop(old, None)
        _expand_cache[url] = (time.monotonic(), result)
    return result


def _http_hop(session, current: str, cur_host: str, user_agent: str = _BROWSER_UA) -> str | None:
    """One cheap request: follow a single redirect, or read og:url/canonical from a 200 page."""
    try:
        response = session.get(
            current,
            headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.8"},
            stream=True, allow_redirects=False, timeout=(3, 5),
        )
    except requests.RequestException:
        return None
    try:
        location = response.headers.get("Location")
        if response.status_code in (301, 302, 303, 307, 308) and location:
            return urljoin(current, location)
        if response.status_code == 200 and not host_matches(cur_host, SHORTENERS):
            body = response.raw.read(250_000, decode_content=True).decode("utf-8", "ignore")
            return _page_target(body, current)
        return None
    finally:
        response.close()


def _expand_uncached(url: str, max_hops: int = 6) -> str:
    """Resolve short/share links to the canonical video URL (HTTP redirect first, yt-dlp as fallback)."""
    current = url
    # Facebook share links: convert to a standard ID URL (reel/ID or watch/?v=ID) that yt-dlp accepts.
    try:
        id_url = facebook_share_to_id_url(url)
    except Exception:
        id_url = None
    if id_url:
        return id_url
    session = requests.Session()
    session.trust_env = False
    try:
        for _ in range(max_hops):
            if not _needs_expand(current):
                return current
            cur_host = (urlsplit(current).hostname or "").lower().rstrip(".")
            candidate = _fb_clean_candidate(_http_hop(session, current, cur_host))
            if (not candidate or candidate == current) and host_matches(cur_host, ("facebook.com",)):
                # logged-out/cloud request got a login wall: the crawler UA is served og:url / video id
                candidate = _fb_clean_candidate(_http_hop(session, current, cur_host, _FB_CRAWLER_UA))
            if not candidate or candidate == current:
                # no usable redirect (login wall, JS page...): let the site extractor name the canonical URL
                candidate = None if host_matches(cur_host, SHORTENERS) else _canonical_share_target(current)
                if not candidate:
                    return current
            host = (urlsplit(candidate).hostname or "").lower().rstrip(".")
            if is_youtube_host(host):
                raise _youtube_redirect(candidate)
            if not site_allowed(host):
                raise ApiError("unsupported_redirect", "قاد رابط المشاركة إلى موقع غير مدعوم.", 422)
            if not is_public_url(candidate):
                raise ApiError("forbidden_url", "رفضت الخدمة وجهة تحويل غير عامة أو غير آمنة.", 403)
            candidate = normalize_url(unwrap_redirect(candidate))
            if candidate == current:
                return current
            current = candidate
        return current
    finally:
        session.close()


def _options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "skip_download": True,
        "noplaylist": True,
        "playlist_items": "1",
        "format": "b[protocol=https]/b[protocol=http]/ba[protocol=https]/ba[protocol=http]",
        "cachedir": False,
        "ignoreconfig": True,
        "socket_timeout": 12,
        "retries": 1,
        "extractor_retries": 1,
        "color": "never",
        # Only dedicated site extractors: no "generic" crawler, never YouTube.
        "allowed_extractors": ["default", "-generic", "-(?i:.*youtube.*)"],
    }


def _safe_headers(headers: dict | None) -> dict[str, str]:
    blocked = {"cookie", "authorization", "proxy-authorization", "host", "content-length", "accept-encoding"}
    safe = {}
    for key, value in (headers or {}).items():
        if key.lower() not in blocked and isinstance(value, str) and len(value) < 2048:
            safe[key] = value
    return safe


def _pick_thumbnail(info: dict) -> str | None:
    """Best http(s) thumbnail URL from yt-dlp metadata (never YouTube)."""
    candidates = [info.get("thumbnail")]
    thumbs = [t for t in (info.get("thumbnails") or []) if isinstance(t, dict) and t.get("url")]
    thumbs.sort(key=lambda t: (t.get("preference") or 0, (t.get("width") or 0) * (t.get("height") or 0)))
    candidates += [t["url"] for t in reversed(thumbs)]
    for value in candidates:
        if isinstance(value, str) and value.lower().startswith(("http://", "https://")) and len(value) < 2048:
            if not is_youtube_host((urlsplit(value).hostname or "").lower()):
                return value
    return None


_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "avif", "heic", "bmp"}


def _pick_audio_format(info: dict) -> dict | None:
    """Best audio-only HTTP(S) format (capped near 192 kbps to keep conversion fast)."""
    found = []
    for f in info.get("formats") or []:
        if not isinstance(f, dict) or not f.get("url"):
            continue
        if (f.get("vcodec") or "none") != "none" or (f.get("acodec") or "none") == "none":
            continue
        if (f.get("protocol") or "https").split("+", 1)[0] not in ("http", "https"):
            continue
        found.append(f)
    if not found:
        return None
    rate = lambda f: f.get("abr") or f.get("tbr") or 0  # noqa: E731
    within = [f for f in found if rate(f) <= 192]
    return max(within, key=rate) if within else min(found, key=rate)


def extract_media(url: str) -> dict:
    """Return metadata and a short-lived CDN URL; never downloads or merges media."""
    url = clean_url(url)
    final_url = expand_url(url)
    host = (urlsplit(final_url).hostname or "").lower()
    if is_youtube_host(host):
        raise ApiError("youtube_unsupported", "تنزيل YouTube غير مدعوم.", 422)
    if not site_allowed(host):
        raise ApiError("unsupported", "هذا الموقع غير مدعوم.", 422)

    try:
        with yt_dlp.YoutubeDL(_options()) as ydl:
            info = ydl.extract_info(final_url, download=False)
    except Exception as exc:
        raise classify_extractor_error(exc) from None

    if info and info.get("_type") == "playlist":
        info = next((entry for entry in (info.get("entries") or []) if entry), None)
    if not info or info.get("requested_formats"):
        raise ApiError("no_single_file", "لا يتوفر ملف واحد جاهز؛ لا تدمج الخدمة مقاطع منفصلة.", 422)

    media_url = info.get("url")
    if not media_url or (info.get("protocol") or "https").split("+", 1)[0] not in ("http", "https"):
        raise ApiError("no_single_file", "لم يتوفر رابط HTTP مباشر قابل للتنزيل.", 422)
    extractor_key = str(info.get("extractor_key") or "")
    media_host = (urlsplit(media_url).hostname or "").lower()
    if extractor_key.lower().startswith("youtube") or is_youtube_host(media_host):
        raise ApiError("youtube_unsupported", "تنزيل YouTube غير مدعوم.", 422)
    if not is_public_url(media_url):
        raise ApiError("forbidden_url", "رفضت الخدمة رابط وسائط غير عام أو غير آمن.", 403)

    ext = str(info.get("ext") or "mp4").lower()
    audio_fmt = _pick_audio_format(info)
    audio_url = audio_fmt["url"] if audio_fmt and is_public_url(audio_fmt["url"]) else None
    has_audio = ext not in _IMAGE_EXTS and (info.get("acodec") != "none" or bool(audio_url))
    return {
        "has_audio": has_audio,
        "audio_url": audio_url,
        "audio_headers": _safe_headers(audio_fmt.get("http_headers")) if audio_url else None,
        "title": info.get("title") or "media",
        "ext": info.get("ext") or "mp4",
        "url": media_url,
        "headers": _safe_headers(info.get("http_headers")),
        "size": info.get("filesize") or info.get("filesize_approx"),
        "duration": info.get("duration"),
        "thumbnail": _pick_thumbnail(info),
        "site": extractor_key or provider_for_host(host) or "Social media",
    }
