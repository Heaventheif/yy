"""Vercel entry point for the Arabic social-media media resolver."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin, urlsplit

import requests
from flask import Flask, Response, g, jsonify, redirect, request

from socialdl.errors import ApiError, classify_extractor_error, from_upstream_status
from socialdl.platforms import PLATFORMS, clean_url, is_public_url, is_youtube_host
from socialdl.service import resolve_media

os.environ.setdefault("HOME", "/tmp")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/.cache")

app = Flask(__name__)
app.json.ensure_ascii = False
app.json.sort_keys = False
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


VERSION = "2"
RATE_LIMIT = _int_env("RATE_LIMIT_PER_MIN", 60)
DL_RATE_LIMIT = _int_env("DL_RATE_LIMIT_PER_MIN", 20)
BATCH_MAX = min(10, max(1, _int_env("BATCH_MAX", 8)))
CACHE_TTL_PUBLIC = _int_env("CACHE_TTL_SEC", 75)
REDIRECT_DEFAULT = os.getenv("REDIRECT", "0") == "1"
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")
CHUNK_SIZE = 256 * 1024

_BUCKETS: dict[tuple[str, str], tuple[float, float]] = {}
_BUCKET_LOCK = threading.Lock()
_BATCH_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="batch")


def _safe_request_id(value: str | None) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]", "", value or "")[:64]
    return value or uuid.uuid4().hex[:16]


def _client_ip() -> str:
    headers = request.headers
    return (headers.get("X-Vercel-Forwarded-For", "").split(",")[0].strip()
            or headers.get("X-Real-IP", "").strip()
            or request.remote_addr or "unknown")


def _rate_check(kind: str, limit: int, cost: int = 1) -> None:
    if limit <= 0:
        return
    now = time.monotonic()
    key = (kind, _client_ip())
    rate = limit / 60.0
    with _BUCKET_LOCK:
        if len(_BUCKETS) > 5000:
            expired = [k for k, (_, touched) in _BUCKETS.items() if now - touched > 120]
            for old in expired[:2500] or list(_BUCKETS)[:500]:
                _BUCKETS.pop(old, None)
        tokens, _ = _BUCKETS.get(key, (float(limit), now))
        tokens = min(float(limit), tokens + (now - _BUCKETS.get(key, (0.0, now))[1]) * rate)
        if tokens < cost:
            wait = max(1, int((cost - tokens) / rate) + 1)
            _BUCKETS[key] = (tokens, now)
            g.rate_limit = (limit, 0, wait)
            raise ApiError("rate_limited", "تجاوزت حد الطلبات المؤقت؛ أعد المحاولة لاحقًا.", 429, retry_after=wait)
        tokens -= cost
        reset = int((limit - tokens) / rate) + 1
        _BUCKETS[key] = (tokens, now)
        g.rate_limit = (limit, int(tokens), reset)


def _base_url() -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    return f"{proto}://{request.host}"


def _error_body(error: ApiError) -> dict:
    detail = {"code": error.code, "message": error.message}
    if error.upstream_status is not None:
        detail["upstream_status"] = error.upstream_status
    return {"ok": False, "error": detail, "request_id": g.get("request_id", "")}


def _json(body: dict, status: int = 200, retry_after: int | None = None):
    response = jsonify(body)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def _public(url: str, media: dict, base_url: str | None = None) -> dict:
    base_url = base_url or _base_url()
    return {
        "ok": True,
        "input": url,
        "title": media["title"],
        "ext": media["ext"],
        "size": media["size"],
        "duration": media["duration"],
        "site": media["site"],
        "download": f"{base_url}/api/v1/download?url={quote(url, safe='')}",
        "expires_in": CACHE_TTL_PUBLIC,
    }


def _filename(media: dict) -> str:
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(media.get("title") or "download")).strip(" .")[:120]
    ext = re.sub(r"[^A-Za-z0-9]", "", str(media.get("ext") or "mp4")) or "bin"
    name = f"{title or 'download'}.{ext}"
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _open_upstream(media: dict, range_header: str | None, method: str = "GET"):
    headers = dict(media.get("headers") or {})
    if range_header:
        headers["Range"] = range_header
    target = media["url"]
    session = requests.Session()
    session.trust_env = False
    try:
        for _ in range(6):
            if not is_public_url(target):
                raise ApiError("forbidden_url", "رفضت الخدمة وجهة تنزيل غير عامة أو غير آمنة.", 403)
            try:
                upstream = session.request(
                    method, target, headers=headers, stream=True,
                    allow_redirects=False, timeout=(10, 35),
                )
            except requests.RequestException as exc:
                raise classify_extractor_error(exc) from None
            location = upstream.headers.get("Location")
            if upstream.status_code in (301, 302, 303, 307, 308) and location:
                candidate = urljoin(target, location)
                upstream.close()
                if not is_public_url(candidate):
                    raise ApiError("forbidden_url", "رفضت الخدمة وجهة تحويل غير عامة أو غير آمنة.", 403)
                target = candidate
                continue
            return upstream, session
        raise ApiError("upstream_redirects", "أعاد مصدر الوسائط تحويلات كثيرة.", 502)
    except Exception:
        session.close()
        raise


def _stream(upstream, session):
    try:
        for chunk in upstream.iter_content(CHUNK_SIZE):
            if chunk:
                yield chunk
    finally:
        upstream.close()
        session.close()


@app.before_request
def _begin_request():
    g.request_id = _safe_request_id(request.headers.get("X-Request-Id"))
    g.started_at = time.perf_counter()
    g.rate_limit = None


@app.after_request
def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/api/"):
        response.headers["X-Request-Id"] = g.get("request_id", "")
        response.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Range, X-Request-Id"
        response.headers["Access-Control-Expose-Headers"] = (
            "Content-Disposition, Content-Length, Content-Range, Accept-Ranges, Retry-After, "
            "RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset, X-Request-Id")
        response.headers["Access-Control-Max-Age"] = "86400"
        if CORS_ORIGIN != "*":
            response.headers["Vary"] = "Origin"
        limits = g.get("rate_limit")
        if limits:
            response.headers["RateLimit-Limit"], response.headers["RateLimit-Remaining"], response.headers["RateLimit-Reset"] = map(str, limits)
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - g.started_at) * 1000:.0f}"
    return response


@app.errorhandler(ApiError)
def _handle_api_error(error: ApiError):
    if error.upstream_status:
        try:
            parsed = urlsplit(request.args.get("url", ""))
            host = parsed.hostname or "unknown"
        except ValueError:
            host = "unknown"
        app.logger.warning("[%s] upstream status=%s host=%s code=%s", g.get("request_id"),
                           error.upstream_status, host, error.code)
    return _json(_error_body(error), error.status, error.retry_after)


@app.errorhandler(413)
def _too_large(_error):
    error = ApiError("request_too_large", "حجم الطلب أكبر من الحد المسموح.", 413)
    return _json(_error_body(error), 413)


@app.errorhandler(404)
def _not_found(_error):
    return _json(_error_body(ApiError("not_found", "المسار غير موجود.", 404)), 404)


@app.errorhandler(405)
def _method_not_allowed(_error):
    return _json(_error_body(ApiError("method_not_allowed", "طريقة الطلب غير مدعومة.", 405)), 405)


@app.errorhandler(Exception)
def _unexpected(error):
    app.logger.error("[%s] unexpected exception type=%s", g.get("request_id"), type(error).__name__)
    return _json(_error_body(ApiError("internal", "حدث خطأ غير متوقع؛ أرسل رقم الطلب للدعم.", 500)), 500)


@app.get("/api/v1/health")
def health():
    return _json({
        "ok": True,
        "version": VERSION,
        "youtube": "disabled",
        "platforms": list(PLATFORMS),
        "limits": {
            "resolve_per_minute": RATE_LIMIT,
            "download_starts_per_minute": DL_RATE_LIMIT,
            "batch_max": BATCH_MAX,
            "extract_timeout_sec": int(os.getenv("EXTRACT_TIMEOUT_SEC", "28")),
        },
    })


@app.get("/api/v1/resolve")
def api_resolve():
    _rate_check("resolve", RATE_LIMIT)
    url = clean_url(request.args.get("url"))
    media = resolve_media(url, fresh=request.args.get("fresh") == "1")
    return _json(_public(url, media))


@app.get("/api/v1/youtube/formats")
def api_youtube_formats():
    """Fetch YouTube formats server-side so the browser does not depend on VidSnap CORS."""
    _rate_check("resolve", RATE_LIMIT)
    raw = (request.args.get("url") or "").strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    if not is_youtube_host(host):
        raise ApiError("invalid_youtube_url", "أدخل رابط YouTube صحيحًا.", 400)

    match = re.search(r"(?:[?&]v=|youtu\.be/|/(?:shorts|embed|live)/)([A-Za-z0-9_-]{11})", raw)
    if not match:
        raise ApiError("invalid_youtube_url", "تعذر استخراج معرّف فيديو YouTube.", 400)
    video_id = match.group(1)
    normalized = f"https://www.youtube.com/watch?v={video_id}"
    session = requests.Session()
    session.trust_env = False
    try:
        try:
            toshiro = session.get(
                "https://toshiro-api-editz6t9.vercel.app/api/downloader/alldl",
                params={"url": normalized},
                headers={"Accept": "application/json", "User-Agent": "yy/2"},
                timeout=(3, 10),
            )
            toshiro_data = toshiro.json()
            result = toshiro_data.get("result") if isinstance(toshiro_data, dict) else None
            if toshiro.ok and toshiro_data.get("success") and isinstance(result, dict) and result.get("status"):
                seen = set()
                formats = []
                for label, key in (("فيديو", "video"), ("جودة عالية", "high_quality"), ("جودة منخفضة", "low_quality"), ("فيديو", "url")):
                    media_url = result.get(key)
                    if isinstance(media_url, str) and media_url.startswith(("http://", "https://")) and media_url not in seen:
                        seen.add(media_url)
                        formats.append({
                            "kind": "progressive",
                            "qualityLabel": label,
                            "mimeType": "video/mp4",
                            "url": media_url,
                            "download_url": media_url,
                        })
                if formats:
                    toshiro.close()
                    return _json({
                        "ok": True,
                        "provider": "toshiro",
                        "normalized": normalized,
                        "platform": "youtube",
                        "videoId": video_id,
                        "title": result.get("title") or "YouTube",
                        "formats": formats,
                    })
            toshiro.close()
        except (requests.RequestException, ValueError, AttributeError):
            pass

        attempts = (
            {"id": video_id},
            {"url": normalized},
        )
        last_message = "تعذر جلب صيغ YouTube من المزود الخارجي."
        for params in attempts:
            try:
                upstream = session.get(
                    "https://api.vidsnap.app/parse",
                    params=params,
                    headers={"Accept": "application/json", "User-Agent": "yy/2"},
                    timeout=(3, 10),
                )
            except requests.RequestException:
                continue
            try:
                data = upstream.json()
            except ValueError:
                data = {}
            formats = data.get("formats") if isinstance(data, dict) else None
            if upstream.ok and isinstance(formats, list) and any(isinstance(item, dict) and item.get("url") for item in formats):
                upstream.close()
                return _json({"ok": True, "normalized": normalized, **data})
            if isinstance(data, dict) and data.get("error"):
                last_message = str(data["error"])
            upstream.close()
        raise ApiError("youtube_provider", last_message, 502)
    finally:
        session.close()


def _batch_one(index: int, raw: object, base_url: str) -> tuple[int, dict]:
    try:
        url = clean_url(raw if isinstance(raw, str) else None)
        media = resolve_media(url)
        return index, _public(url, media, base_url)
    except ApiError as error:
        return index, {"ok": False, "input": raw if isinstance(raw, str) else None,
                       "error": {"code": error.code, "message": error.message,
                                 **({"upstream_status": error.upstream_status} if error.upstream_status is not None else {})}}
    except Exception:
        return index, {"ok": False, "input": raw if isinstance(raw, str) else None,
                       "error": {"code": "internal", "message": "تعذر معالجة هذا الرابط."}}


@app.post("/api/v1/batch")
def api_batch():
    body = request.get_json(silent=True)
    urls = body.get("urls") if isinstance(body, dict) else None
    if not isinstance(urls, list) or not urls:
        raise ApiError("invalid_request", 'أرسل JSON بالشكل {"urls": ["رابط"]}.', 400)
    if len(urls) > BATCH_MAX:
        raise ApiError("too_many_urls", f"الحد الأقصى للدفعة هو {BATCH_MAX} رابطًا.", 413)
    cost = len({value for value in urls if isinstance(value, str)})
    _rate_check("resolve", RATE_LIMIT, max(cost, 1))

    base_url = _base_url()
    futures = {_BATCH_POOL.submit(_batch_one, index, raw, base_url): index for index, raw in enumerate(urls)}
    streaming = request.args.get("stream") == "1" or "ndjson" in request.headers.get("Accept", "").lower()
    if streaming:
        def lines():
            for future in as_completed(futures):
                index, result = future.result()
                yield json.dumps({"index": index, **result}, ensure_ascii=False) + "\n"
        response = Response(lines(), mimetype="application/x-ndjson")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    results = [None] * len(urls)
    for future in as_completed(futures):
        index, item = future.result()
        results[index] = item
    return _json({"ok": True, "count": len(results), "results": results})


@app.route("/api/v1/download", methods=["GET", "HEAD"])
def api_download():
    url = clean_url(request.args.get("url"))
    range_header = request.headers.get("Range")
    if range_header and not re.fullmatch(r"bytes=\d*-\d*", range_header.strip()):
        raise ApiError("invalid_range", "نطاق التنزيل غير صالح.", 416)
    if not range_header or range_header.startswith("bytes=0-"):
        _rate_check("download", DL_RATE_LIMIT)

    media = resolve_media(url)
    redirect_value = request.args.get("redirect")
    if (REDIRECT_DEFAULT if redirect_value is None else redirect_value == "1"):
        response = redirect(media["url"], 302)
        response.headers["Cache-Control"] = "no-store"
        return response

    upstream = None
    session = None
    try:
        upstream, session = _open_upstream(media, range_header, request.method)
        if upstream.status_code in (401, 403, 404, 410):
            upstream.close()
            session.close()
            media = resolve_media(url, fresh=True)
            upstream, session = _open_upstream(media, range_header, request.method)
        if upstream.status_code not in (200, 206, 416):
            raise from_upstream_status(upstream.status_code)
    except requests.RequestException as error:
        if upstream is not None:
            upstream.close()
        if session is not None:
            session.close()
        raise classify_extractor_error(error) from None
    except Exception:
        if upstream is not None:
            upstream.close()
        if session is not None:
            session.close()
        raise

    response = Response(
        _stream(upstream, session) if request.method == "GET" else b"",
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type") or "application/octet-stream",
        direct_passthrough=True,
    )
    for header in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
        if upstream.headers.get(header):
            response.headers[header] = upstream.headers[header]
    response.headers.setdefault("Accept-Ranges", "bytes")
    response.headers["Content-Disposition"] = _filename(media)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    if request.method == "HEAD":
        upstream.close()
        session.close()
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)
