"""TikTok media URL resolution through Toshiro and VidSnap providers."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests

from .errors import ApiError
from .platforms import is_public_url

TOSHIRO_URL = "https://toshiro-api-editz6t9.vercel.app/api/downloader/alldl"
VIDSNAP_PARSE = "https://api.vidsnap.app/parse"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PROVIDER_ORDER = ("toshiro", "vidsnap")
PROVIDER_TIMEOUT = (3, 12)


class ProviderError(Exception):
    pass


def _trusted_tiktok_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return (
        host == "tiktok.com" or host.endswith(".tiktok.com")
        or host == "tiktokv.com" or host.endswith(".tiktokv.com")
        or bool(re.search(r"(^|\.)tiktokcdn(?:-[a-z0-9]+)?\.com$", host))
        or host == "byteoversea.com" or host.endswith(".byteoversea.com")
    )


def _positive_int(value):
    try:
        number = int(float(value))
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _get_json(endpoint: str, params: dict) -> dict:
    session = requests.Session()
    session.trust_env = False
    try:
        try:
            response = session.get(endpoint, params=params, timeout=PROVIDER_TIMEOUT,
                                   headers={"Accept": "application/json", "User-Agent": UA})
        except requests.Timeout:
            raise ProviderError("timeout") from None
        except requests.RequestException:
            raise ProviderError("unreachable") from None
        try:
            if response.status_code >= 400:
                raise ProviderError(f"http_{response.status_code}")
            data = response.json()
        except ValueError:
            raise ProviderError("bad_response") from None
        finally:
            response.close()
        if not isinstance(data, dict):
            raise ProviderError("bad_response")
        return data
    finally:
        session.close()


def _as_media(url: str, *, title: str, duration=None, size=None, thumbnail=None, author="") -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or not _trusted_tiktok_host(parsed.hostname)
            or not is_public_url(url)):
        raise ProviderError("unsafe_media_url")
    if thumbnail:
        thumb = urlsplit(str(thumbnail))
        if (thumb.scheme != "https" or not thumb.hostname or not _trusted_tiktok_host(thumb.hostname)
                or not is_public_url(str(thumbnail))):
            thumbnail = None
    return {
        "title": str(title or "TikTok video")[:240],
        "ext": "mp4",
        "url": url,
        "headers": {"User-Agent": UA, "Referer": "https://www.tiktok.com/"},
        "size": _positive_int(size),
        "duration": _positive_int(duration),
        "thumbnail": thumbnail,
        "site": "TikTok",
        "has_audio": True,
        "audio_url": None,
        "audio_headers": None,
        "hls": False,
        "author": str(author or "")[:120],
    }


def _toshiro(url: str) -> dict:
    data = _get_json(TOSHIRO_URL, {"url": url})
    result = data.get("result")
    if not data.get("success") or not isinstance(result, dict) or result.get("success") is False:
        raise ProviderError("empty")
    media_url = result.get("video") or result.get("url") or result.get("download_url")
    if not isinstance(media_url, str) or not media_url.startswith("https://"):
        raise ProviderError("empty")
    return _as_media(media_url, title=result.get("title") or data.get("title") or "TikTok video",
                     duration=result.get("duration"), size=result.get("size"),
                     thumbnail=result.get("cover") or result.get("thumbnail"),
                     author=result.get("author") or data.get("author") or "")


def _vidsnap(url: str) -> dict:
    data = _get_json(VIDSNAP_PARSE, {"url": url})
    formats = data.get("formats") or []
    candidates = []
    for item in formats:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        mime = str(item.get("mimeType") or item.get("mime_type") or item.get("type") or "").lower()
        kind = str(item.get("kind") or "").lower()
        if kind == "audio" or mime.startswith("audio/"):
            continue
        if kind not in ("", "progressive", "video") and not mime.startswith("video/"):
            continue
        candidates.append(item)
    if not candidates:
        raise ProviderError("empty")

    def score(item: dict) -> tuple[int, int]:
        has_audio = item.get("hasAudio") is not False and item.get("videoOnly") is not True
        try:
            height = int(item.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        return (int(has_audio), height)

    selected = max(candidates, key=score)
    thumbs = data.get("thumbnails") or []
    thumbnail = next((x.get("url") for x in thumbs if isinstance(x, dict) and x.get("url")), None)
    return _as_media(selected["url"], title=data.get("title") or "TikTok video",
                     duration=data.get("lengthSeconds"), size=selected.get("size"),
                     thumbnail=thumbnail, author=data.get("author") or "")


PROVIDERS = {"toshiro": _toshiro, "vidsnap": _vidsnap}


def resolve_tiktok(url: str) -> dict:
    """Resolve a public TikTok URL with Toshiro first and VidSnap as fallback."""
    failures = []
    for name in PROVIDER_ORDER:
        try:
            return PROVIDERS[name](url)
        except ProviderError as error:
            failures.append({"provider": name, "reason": str(error) or "failed"})
        except Exception:
            failures.append({"provider": name, "reason": "error"})
    raise ApiError(
        "tiktok_providers_unavailable",
        "تعذر استخراج رابط فيديو TikTok عبر مزودي التنزيل؛ أعد المحاولة لاحقًا.",
        502,
        retry_after=3,
        details={"providers": failures},
        retryable=True,
    )
