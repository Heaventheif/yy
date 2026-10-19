"""Public-media extraction using yt-dlp's maintained site extractors."""
from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlsplit

import requests
import yt_dlp

from .errors import ApiError, classify_extractor_error
from .platforms import SHORTENERS, clean_url, host_matches, is_public_url, is_youtube_host, provider_for_host

_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_META_URL = re.compile(
    r'<meta[^>]+property=["\']og:url["\'][^>]*content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:url["\']|'
    r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', re.IGNORECASE,
)
_FB_VIDEO_ID = re.compile(r'"(?:video_id|videoId)"\s*:\s*"(\d{6,})"')


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
    return None


def _needs_expand(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path.lower()
    share_path = (path.startswith(("/share", "/stories/", "/t/")) or "/s/" in path)
    alias_host = host_matches(host, (
        "fb.watch", "fb.me", "vm.tiktok.com", "vt.tiktok.com", "redd.it", "dai.ly",
    ))
    return host_matches(host, SHORTENERS) or alias_host or (
        provider_for_host(host) is not None and share_path
    )


def _canonical_share_target(url: str) -> str | None:
    """Resolve a supported share/short URL to yt-dlp's canonical media URL."""
    try:
        with yt_dlp.YoutubeDL(_options()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    target = info.get("webpage_url") if isinstance(info, dict) else None
    if not isinstance(target, str) or target == url:
        return None
    return target


def expand_url(url: str, max_hops: int = 6) -> str:
    """Expand common share/short links, validating every redirect target."""
    current = url
    session = requests.Session()
    session.trust_env = False
    try:
        for _ in range(max_hops):
            if not _needs_expand(current):
                return current
            if not is_youtube_host((urlsplit(current).hostname or "").lower()):
                candidate = _canonical_share_target(current)
                if candidate:
                    parts = urlsplit(candidate)
                    host = (parts.hostname or "").lower().rstrip(".")
                    if is_youtube_host(host):
                        raise ApiError("youtube_unsupported", "تنزيل YouTube غير مدعوم.", 422)
                    if provider_for_host(host) is None or not is_public_url(candidate):
                        raise ApiError("unsupported_redirect", "قاد رابط المشاركة إلى موقع غير مدعوم.", 422)
                    current = candidate
                    continue
            try:
                response = session.get(
                    current,
                    headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"},
                    stream=True, allow_redirects=False, timeout=(5, 8),
                )
            except requests.RequestException:
                return current
            try:
                location = response.headers.get("Location")
                if response.status_code in (301, 302, 303, 307, 308) and location:
                    candidate = urljoin(current, location)
                elif response.status_code == 200:
                    body = response.raw.read(250_000, decode_content=True).decode("utf-8", "ignore")
                    candidate = _page_target(body, current) or current
                else:
                    return current
            finally:
                response.close()

            parts = urlsplit(candidate)
            host = (parts.hostname or "").lower().rstrip(".")
            if is_youtube_host(host):
                raise ApiError("youtube_unsupported", "تنزيل YouTube غير مدعوم.", 422)
            if provider_for_host(host) is None and not host_matches(host, SHORTENERS):
                raise ApiError("unsupported_redirect", "قاد رابط المشاركة إلى موقع غير مدعوم.", 422)
            if not is_public_url(candidate):
                raise ApiError("forbidden_url", "رفضت الخدمة وجهة تحويل غير عامة أو غير آمنة.", 403)
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
    }


def _safe_headers(headers: dict | None) -> dict[str, str]:
    blocked = {"cookie", "authorization", "proxy-authorization", "host", "content-length", "accept-encoding"}
    safe = {}
    for key, value in (headers or {}).items():
        if key.lower() not in blocked and isinstance(value, str) and len(value) < 2048:
            safe[key] = value
    return safe


def extract_media(url: str) -> dict:
    """Return metadata and a short-lived CDN URL; never downloads or merges media."""
    url = clean_url(url)
    final_url = expand_url(url)
    host = (urlsplit(final_url).hostname or "").lower()
    if is_youtube_host(host):
        raise ApiError("youtube_unsupported", "تنزيل YouTube غير مدعوم.", 422)
    if provider_for_host(host) is None:
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

    return {
        "title": info.get("title") or "media",
        "ext": info.get("ext") or "mp4",
        "url": media_url,
        "headers": _safe_headers(info.get("http_headers")),
        "size": info.get("filesize") or info.get("filesize_approx"),
        "duration": info.get("duration"),
        "site": extractor_key or provider_for_host(host) or "Social media",
    }
