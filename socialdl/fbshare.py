"""Convert Facebook share links (/share/v/..., /share/r/..., fb.watch) to standard ID URLs yt-dlp accepts.

    https://www.facebook.com/share/v/1HkkyeQLw1/  ->  https://www.facebook.com/watch/?v=1080741757962951
    https://www.facebook.com/share/r/XXXX/        ->  https://www.facebook.com/reel/1080741757962951
"""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests

from .platforms import host_matches

_FB_HOSTS = ("facebook.com", "fb.watch", "fb.me", "fb.gg")
_DESKTOP_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_MOBILE_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")
_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

# (rewrite host, user agent): cloud IPs are often walled on one variant but not the others.
_ATTEMPTS = (
    (None, _DESKTOP_UA),
    ("m.facebook.com", _MOBILE_UA),
    (None, _CRAWLER_UA),
    ("m.facebook.com", _CRAWLER_UA),
)

_LOGIN_PATHS = ("/login", "/checkpoint", "/recover", "/two_step_verification")
_PATH_REEL = re.compile(r"/reel/(\d{6,})")
_PATH_VIDEOS = re.compile(r"/videos/(?:[^/?#]+/)?(\d{6,})")
_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:url|og:video:url|og:video|al:android:url|al:ios:url)["\'][^>]*content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:url|og:video:url|og:video|al:android:url|al:ios:url)["\']|'
    r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', re.I)
_BODY_PATTERNS = (
    re.compile(r'\\?/reel\\?/(\d{6,})'),
    re.compile(r'"(?:video_id|videoId|video_id_str)"\s*:\s*"?(\d{6,})'),
    re.compile(r'\\?/videos\\?/(?:[^/"\\]+\\?/)?(\d{6,})'),
    re.compile(r'fb://(?:video|reel|watch)/?\??(?:v=|id=)?(\d{6,})'),
)


def is_facebook_share(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if host_matches(host, ("fb.watch", "fb.me", "fb.gg")):
        return True
    return host_matches(host, ("facebook.com",)) and parts.path.lower().startswith("/share")


def unwrap_login_wall(url: str) -> str | None:
    """Facebook sends cloud IPs to /login/?next=<real url>: return that target.

    Other URLs come back unchanged; a login URL without a usable target gives None."""
    parts = urlsplit(url)
    if not (host_matches((parts.hostname or "").lower(), ("facebook.com",))
            and parts.path.lower().startswith(_LOGIN_PATHS)):
        return url
    target = urljoin(url, dict(parse_qsl(parts.query)).get("next", ""))
    return target if target.lower().startswith(("http://", "https://")) and target != url else None


def id_from_url(url: str) -> tuple[str, str] | None:
    """Return (video_id, kind) from any Facebook video URL form; kind is 'reel' or 'watch'."""
    try:
        parts = urlsplit(html.unescape(url))
    except ValueError:
        return None
    if not host_matches((parts.hostname or "").lower(), ("facebook.com", "fb.watch")) and parts.scheme.startswith("http"):
        return None
    target = unwrap_login_wall(url)
    if target != url:
        return id_from_url(target) if target else None
    path, query = parts.path, dict(parse_qsl(parts.query))
    match = _PATH_REEL.search(path)
    if match:
        return match.group(1), "reel"
    if re.fullmatch(r"\d{6,}", query.get("v", "")):
        return query["v"], "watch"
    match = _PATH_VIDEOS.search(path)
    if match:
        return match.group(1), "watch"
    return None


def canonical_url(video_id: str, kind: str) -> str:
    if kind == "reel":
        return f"https://www.facebook.com/reel/{video_id}"
    return f"https://www.facebook.com/watch/?v={video_id}"


def _id_from_body(body: str) -> tuple[str, str] | None:
    for match in _META.finditer(body):
        raw = next((g for g in match.groups() if g), None)
        found = id_from_url(raw) if raw and raw.startswith(("http", "/")) else None
        if not found and raw:
            digits = re.search(r"(?:video|reel|watch)/?\??(?:v=|id=)?(\d{6,})", raw)
            found = (digits.group(1), "reel" if "reel" in raw else "watch") if digits else None
        if found:
            return found
    for index, pattern in enumerate(_BODY_PATTERNS):
        match = pattern.search(body)
        if match:
            return match.group(1), "reel" if index == 0 else "watch"
    return None


def _rewrite_host(url: str, host: str | None) -> str:
    if not host:
        return url
    parts = urlsplit(url)
    if host_matches((parts.hostname or "").lower(), ("facebook.com",)):
        return parts._replace(netloc=host).geturl()
    return url


def _try_once(session: requests.Session, url: str, host: str | None, ua: str, max_hops: int = 5) -> tuple[str, str] | None:
    current = _rewrite_host(url, host)
    for _ in range(max_hops):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not host_matches((parts.hostname or "").lower(), _FB_HOSTS):
            return None  # never follow redirects off Facebook
        try:
            response = session.get(current, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.8",
                                                     "Accept": "text/html,application/xhtml+xml"},
                                   stream=True, allow_redirects=False, timeout=(3, 6))
        except requests.RequestException:
            return None
        try:
            location = response.headers.get("Location")
            if response.status_code in (301, 302, 303, 307, 308) and location:
                current = urljoin(current, location)
                found = id_from_url(current)
                if found:
                    return found
                continue
            if response.status_code == 200:
                body = response.raw.read(400_000, decode_content=True).decode("utf-8", "ignore")
                return _id_from_body(body)
            return None
        finally:
            response.close()
    return None


def facebook_share_to_id_url(url: str) -> str | None:
    """Resolve a share/short link to https://www.facebook.com/{reel/ID | watch/?v=ID}; None if it can't be resolved."""
    if not is_facebook_share(url):
        return None
    found = id_from_url(url)
    if found:
        return canonical_url(*found)
    session = requests.Session()
    session.trust_env = False
    try:
        for host, ua in _ATTEMPTS:
            found = _try_once(session, url, host, ua)
            if found:
                video_id, kind = found
                # keep the share type as a hint: /share/r/ links are reels
                if "/share/r/" in url.lower():
                    kind = "reel"
                return canonical_url(video_id, kind)
    finally:
        session.close()
    return None
