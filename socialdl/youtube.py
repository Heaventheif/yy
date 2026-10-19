"""YouTube format lookup through external providers, plus signed same-origin download tokens.

Why this exists: the browser used to call provider media URLs directly. Those URLs are
cross-origin (CORS/CSP blocked), often tied to the provider's IP and expire quickly, so
users saw 502/403 errors. Now the server looks formats up (providers raced in parallel,
with per-provider cool-down) and streams the chosen file through ``/api/v1/youtube/download``,
re-resolving once if the first link was rejected.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import quote, urlsplit

import requests

from .errors import ApiError
from .platforms import is_youtube_host

UA = "yy/3 (+https://vercel.com)"
VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/(?:shorts|embed|live|v)/)([A-Za-z0-9_-]{11})")
TOSHIRO_URL = "https://toshiro-api-editz6t9.vercel.app/api/downloader/alldl"
VIDSNAP_PARSE = "https://api.vidsnap.app/parse"
VIDSNAP_PROXY = "https://api.vidsnap.app/proxy"

PROVIDER_TIMEOUT = (2.5, 6)                                                        
TOTAL_BUDGET = 9.0                                                                       
MAX_FORMATS = 14                                                                
FORMAT_CACHE_TTL = 30
TOKEN_TTL = 600
MIN_FORMATS = 6                                                                        
GRACE = 2.0                                                             
COOLDOWN_AFTER = 3                                                                    
COOLDOWN_SECONDS = 45

def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default

TOKEN_TTL = _int_env("YT_TOKEN_TTL_SEC", TOKEN_TTL)

class ProviderError(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status

def extract_video_id(raw: str | None) -> str:
    raw = (raw or "").strip()
    try:
        host = (urlsplit(raw).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    if not host or not is_youtube_host(host):
        raise ApiError("invalid_youtube_url", "أدخل رابط YouTube صحيحًا.", 400)
    match = VIDEO_ID_RE.search(raw)
    if not match:
        raise ApiError("invalid_youtube_url", "تعذر استخراج معرّف فيديو YouTube من الرابط.", 400)
    return match.group(1)

def _parse_max_height() -> int:
    """MAX_VIDEO_HEIGHT: 0 disables filtering; positive = height cap; invalid/negative -> 720."""
    try:
        value = int(os.getenv("MAX_VIDEO_HEIGHT", "720"))
    except (TypeError, ValueError):
        return 720
    return value if value >= 0 else 720

MAX_VIDEO_HEIGHT = _parse_max_height()

def _height(label: str) -> int:
    """Numeric height from '720p', '720p60', '4K'/'8K'; labels like 'جودة عالية' -> 0."""
    text = label or ""
    match = re.search(r"(\d{3,4})p", text)
    if match:
        return int(match.group(1))
    match = re.match(r"^\s*([48])K\b", text, re.IGNORECASE)
    return int(match.group(1)) * 540 if match else 0

def filter_by_height(formats: list[dict], cap: int | None = None, relaxed: bool = False) -> list[dict]:
    """Keep audio always; keep video only when its height is known and <= cap.

    cap <= 0 disables filtering. relaxed=True also keeps unknown-height labels.
    """
    if cap is None:
        cap = MAX_VIDEO_HEIGHT
    if cap <= 0:
        return list(formats)
    kept = []
    for fmt in formats:
        if fmt.get("kind") == "audio":
            kept.append(fmt)
            continue
        height = _height(fmt.get("quality"))
        if height == 0:
            if relaxed:
                kept.append(fmt)
        elif height <= cap:
            kept.append(fmt)
    return kept

def _to_int(value) -> int | None:
    try:
        number = int(float(value))
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None

def normalize_item(provider: str, index: int, item: dict, *, wrap_vidsnap: bool = False,
                   default_kind: str | None = None) -> dict | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    mime = str(item.get("mimeType") or item.get("mime_type") or item.get("type") or "").split(";")[0].strip()
    kind = item.get("kind") if item.get("kind") in ("progressive", "video", "audio") else default_kind
    if kind is None:
        if mime.startswith("audio"):
            kind = "audio"
        elif item.get("videoOnly") is True or item.get("hasAudio") is False:
            kind = "video"
        else:
            kind = "progressive"
    bitrate = _to_int(item.get("bitrate") or item.get("averageBitrate"))
    quality = str(item.get("qualityLabel") or item.get("quality") or item.get("audioQuality") or "").strip()
    if not quality and item.get("height"):
        quality = f"{item['height']}p"
    if not quality and kind == "audio" and bitrate:
        quality = f"{round(bitrate / 1000)}k"
    if kind == "audio":
        quality = quality.replace("AUDIO_QUALITY_", "").lower() or "صوت"
    if wrap_vidsnap and "api.vidsnap.app/proxy" not in url:
        url = f"{VIDSNAP_PROXY}?u={quote(url, safe='')}"
    return {
        "id": f"{provider}:{item.get('itag') or index}",
        "provider": provider,
        "kind": kind,
        "quality": quality or "—",
        "mime": mime or ("audio/mp4" if kind == "audio" else "video/mp4"),
        "bitrate": bitrate,
        "size": _to_int(item.get("contentLength") or item.get("clen") or item.get("size")),
        "url": url,
    }

def _sort_formats(formats: list[dict]) -> list[dict]:
    order = {"progressive": 0, "audio": 1, "video": 2}
    seen, unique = set(), []
    for fmt in formats:
        if fmt["url"] in seen:
            continue
        seen.add(fmt["url"])
        unique.append(fmt)
    unique.sort(key=lambda f: (order.get(f["kind"], 3), -_height(f["quality"]), -(f["bitrate"] or 0)))
    return unique[:MAX_FORMATS]

def _get_json(url: str, params: dict | None = None):
    session = requests.Session()
    session.trust_env = False
    try:
        try:
            response = session.get(url, params=params, timeout=PROVIDER_TIMEOUT,
                                   headers={"Accept": "application/json", "User-Agent": UA})
        except requests.Timeout:
            raise ProviderError("timeout") from None
        except requests.RequestException:
            raise ProviderError("unreachable") from None
        status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = None
        finally:
            response.close()
        if status >= 400:
            raise ProviderError(f"http_{status}", status)
        if data is None:
            raise ProviderError("bad_response", status)
        return data
    finally:
        session.close()

def _toshiro(video_id: str, normalized: str) -> dict:
    data = _get_json(TOSHIRO_URL, {"url": normalized})
    result = data.get("result") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("success") and isinstance(result, dict) and result.get("status")):
        raise ProviderError("empty")
    formats = []
    for label, key in (("جودة عالية", "high_quality"), ("فيديو", "video"), ("فيديو", "url"), ("جودة منخفضة", "low_quality")):
        fmt = normalize_item("toshiro", len(formats), {"url": result.get(key), "qualityLabel": label,
                                                         "kind": "progressive", "mimeType": "video/mp4"})
        if fmt:
            fmt["id"] = f"toshiro:{key}"
            formats.append(fmt)
    return {"title": result.get("title"), "duration": _to_int(result.get("duration")), "formats": formats}

def _vidsnap(video_id: str, normalized: str) -> dict:
    last: ProviderError | None = None
    for params in ({"id": video_id}, {"url": normalized}):
        if last is not None and last.reason == "timeout":
            break                                                                           
        try:
            data = _get_json(VIDSNAP_PARSE, params)
        except ProviderError as exc:
            last = exc
            continue
        items = data.get("formats") if isinstance(data, dict) else None
        formats = [f for i, item in enumerate(items or [])
                   if (f := normalize_item("vidsnap", i, item, wrap_vidsnap=True))]
        if formats:
            return {"title": data.get("title"), "duration": _to_int(data.get("duration")), "formats": formats}
        last = ProviderError("empty")
    raise last or ProviderError("empty")

def _instances(name: str) -> list[str]:
    return [v.strip().rstrip("/") for v in os.getenv(name, "").split(",") if v.strip().startswith("https://")]

def _piped(video_id: str, normalized: str) -> dict:
    last: ProviderError | None = None
    for base in _instances("PIPED_INSTANCES"):
        try:
            data = _get_json(f"{base}/streams/{video_id}")
        except ProviderError as exc:
            last = exc
            continue
        formats = []
        for i, item in enumerate((data.get("videoStreams") or []) + (data.get("audioStreams") or [])):
            if not isinstance(item, dict):
                continue
            item = dict(item, kind=("video" if item.get("videoOnly") else
                                    "audio" if str(item.get("mimeType", "")).startswith("audio") else "progressive"))
            fmt = normalize_item("piped", i, item)
            if fmt:
                formats.append(fmt)
        if formats:
            return {"title": data.get("title"), "duration": _to_int(data.get("duration")), "formats": formats}
        last = ProviderError("empty")
    raise last or ProviderError("not_configured")

def _invidious(video_id: str, normalized: str) -> dict:
    last: ProviderError | None = None
    for base in _instances("INVIDIOUS_INSTANCES"):
        try:
            data = _get_json(f"{base}/api/v1/videos/{video_id}",
                             {"fields": "title,lengthSeconds,formatStreams,adaptiveFormats"})
        except ProviderError as exc:
            last = exc
            continue
        formats = []
        combined = [(item, "progressive") for item in (data.get("formatStreams") or [])]
        adaptive = [(item, None) for item in (data.get("adaptiveFormats") or [])]
        for i, (item, default_kind) in enumerate(combined + adaptive):
            if not isinstance(item, dict):
                continue
            mime = str(item.get("type", ""))
            kind = default_kind or ("audio" if mime.startswith("audio") else "video")
            fmt = normalize_item("invidious", i, dict(item, mimeType=mime, kind=kind))
            if fmt:
                formats.append(fmt)
        if formats:
            return {"title": data.get("title"), "duration": _to_int(data.get("lengthSeconds")), "formats": formats}
        last = ProviderError("empty")
    raise last or ProviderError("not_configured")

PROVIDERS = {"toshiro": _toshiro, "vidsnap": _vidsnap, "piped": _piped, "invidious": _invidious}

def _chain() -> list[str]:
    wanted = [n.strip().lower() for n in os.getenv("YT_PROVIDERS", "toshiro,vidsnap,piped,invidious").split(",")]
    chain = [n for n in wanted if n in PROVIDERS]
    if "piped" in chain and not _instances("PIPED_INSTANCES"):
        chain.remove("piped")
    if "invidious" in chain and not _instances("INVIDIOUS_INSTANCES"):
        chain.remove("invidious")
    return chain or ["toshiro", "vidsnap"]

_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ytprov")
_health: dict[str, list] = {}                                                                       
_cache: dict[str, tuple[float, dict]] = {}

def _record(name: str, ok: bool) -> None:
    with _lock:
        entry = _health.setdefault(name, [0, 0.0])
        if ok:
            entry[0], entry[1] = 0, 0.0
        else:
            entry[0] += 1
            if entry[0] >= COOLDOWN_AFTER:
                entry[1] = time.monotonic() + COOLDOWN_SECONDS

def _active(chain: list[str]) -> list[str]:
    now = time.monotonic()
    with _lock:
        ready = [n for n in chain if _health.get(n, [0, 0.0])[1] <= now]
    return ready or chain                                                                 

def _merge(successes) -> list[dict]:
    """Combine formats from every provider that answered; same kind+quality+type keeps the higher-priority one.

    The height cap is applied here, before MAX_FORMATS, so 1080p/4K never use up the slots.
    """
    seen, out = set(), []
    for _, _, _, formats in sorted(successes, key=lambda item: item[0]):
        for fmt in formats:
            key = (fmt["kind"], fmt["quality"], fmt["mime"])
            if key in seen:
                continue
            seen.add(key)
            out.append(fmt)
    strict = filter_by_height(out, relaxed=False)
    if any(f["kind"] == "progressive" for f in strict):
        out = strict
    else:                                                                                                 
        relaxed = filter_by_height(out, relaxed=True)
        out = relaxed if any(f["kind"] == "progressive" for f in relaxed) else strict
    return _sort_formats(out)

def lookup(video_id: str, fresh: bool = False) -> dict:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(video_id)
        if hit and not fresh and now - hit[0] < FORMAT_CACHE_TTL:
            return dict(hit[1], cache="hit")

    normalized = f"https://www.youtube.com/watch?v={video_id}"
    names = _active(_chain())
    futures = {_pool.submit(PROVIDERS[name], video_id, normalized): name for name in names}
    deadline = time.monotonic() + TOTAL_BUDGET
    failures: list[dict] = []
    successes: list[tuple[int, str, dict, list[dict]]] = []
    pending = set(futures)
    grace_end = None
    merged: list[dict] = []
    while pending:
        limit = deadline if grace_end is None else min(deadline, grace_end)
        remaining = limit - time.monotonic()
        if remaining <= 0:
            break
        done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
        for future in done:
            name = futures[future]
            try:
                result = future.result()
            except ProviderError as exc:
                _record(name, False)
                failures.append({"provider": name, "reason": exc.reason, **({"status": exc.status} if exc.status else {})})
                continue
            except Exception:
                _record(name, False)
                failures.append({"provider": name, "reason": "error"})
                continue
            formats = result.get("formats") or []
            if not formats:
                _record(name, False)
                failures.append({"provider": name, "reason": "empty"})
                continue
            _record(name, True)
            successes.append((names.index(name), name, result, formats))
        if successes:
            merged = _merge(successes)
            if len(merged) >= MIN_FORMATS:
                break                                                         
            if grace_end is None:                                                                     
                grace_end = time.monotonic() + GRACE
    if successes:
        for future in pending:
            future.cancel()
        successes.sort(key=lambda item: item[0])
        merged = merged or _merge(successes)
        value = {
            "provider": "+".join(item[1] for item in successes),
            "video_id": video_id,
            "normalized": normalized,
            "title": next((item[2].get("title") for item in successes if item[2].get("title")), "YouTube"),
            "duration": next((item[2].get("duration") for item in successes if item[2].get("duration")), None),
            "formats": merged,
        }
        with _lock:
            if len(_cache) > 100:
                _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
            _cache[video_id] = (time.monotonic(), value)
        return dict(value, cache="miss")
    for future in pending:                                                                       
        future.cancel()
        _record(futures[future], False)
        failures.append({"provider": futures[future], "reason": "timeout"})

    only_timeouts = failures and all(f["reason"] == "timeout" for f in failures)
    raise ApiError(
        "youtube_timeout" if only_timeouts else "youtube_providers_unavailable",
        "تأخر مزودو التنزيل في الرد؛ أعد المحاولة بعد لحظات." if only_timeouts else
        "مزودو تنزيل YouTube لا يستجيبون حاليًا؛ هذا عطل خارجي مؤقت وليس من رابطك.",
        504 if only_timeouts else 502, retry_after=3, details={"providers": failures},
    )

def _secret() -> bytes:
    explicit = os.getenv("SIGNING_SECRET")
    if explicit:
        return explicit.encode()
    seed = "|".join(os.getenv(k, "") for k in ("VERCEL_PROJECT_ID", "VERCEL_GIT_REPO_ID", "VERCEL_ORG_ID")) or "yy-dev"
    return hashlib.sha256(("yy|" + seed).encode()).digest()

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

def make_token(video_id: str, title: str, fmt: dict) -> str:
    payload = {"v": video_id, "i": fmt["id"], "p": fmt["provider"], "k": fmt["kind"], "q": fmt["quality"],
               "m": fmt["mime"], "u": fmt["url"], "t": (title or "")[:140], "e": int(time.time()) + TOKEN_TTL}
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:18])
    return f"{body}.{sig}"

def read_token(token: str | None) -> dict:
    try:
        body, sig = (token or "").split(".", 1)
        expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:18])
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ApiError("invalid_token", "رابط التنزيل غير صالح؛ اختر الجودة من جديد.", 400) from None
    if payload.get("e", 0) < time.time():
        raise ApiError("link_expired", "انتهت صلاحية رابط التنزيل؛ اختر الجودة من جديد.", 410, retryable=False)
    return payload

def match_format(formats: list[dict], wanted: dict) -> dict | None:
    """Pick the same format after a refresh: same id, else same kind+quality, else nearest height."""
    for fmt in formats:
        if fmt["id"] == wanted["i"]:
            return fmt
    same_kind = [f for f in formats if f["kind"] == wanted["k"]]
    for fmt in same_kind:
        if fmt["quality"] == wanted["q"]:
            return fmt
    if same_kind:
        target = _height(wanted["q"])
        return min(same_kind, key=lambda f: abs(_height(f["quality"]) - target))
    return formats[0] if formats else None

_EXT = {"video/mp4": "mp4", "video/webm": "webm", "video/3gpp": "3gp", "audio/mp4": "m4a",
        "audio/webm": "webm", "audio/mpeg": "mp3", "audio/ogg": "ogg"}

def file_info(title: str, fmt_like: dict) -> dict:
    mime = fmt_like.get("m") or fmt_like.get("mime") or "video/mp4"
    quality = fmt_like.get("q") or fmt_like.get("quality") or ""
    suffix = f" {quality}" if re.search(r"\d", quality) else ""
    return {"title": f"{title}{suffix}".strip(), "ext": _EXT.get(mime, "mp4" if mime.startswith("video") else "m4a")}
