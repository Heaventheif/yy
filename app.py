"""Vercel entry point for the Arabic social-media media resolver."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin, urlsplit

import requests
from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory

from socialdl.errors import ApiError, classify_extractor_error, from_upstream_status
from socialdl import youtube as yt
from socialdl.platforms import PLATFORMS, clean_url, is_public_url
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
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

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
    detail = {"code": error.code, "message": error.message, "retryable": error.retryable}
    if error.upstream_status is not None:
        detail["upstream_status"] = error.upstream_status
    if error.details:
        detail["details"] = error.details
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
        "direct_url": media["url"],
        "download": f"{base_url}/api/v1/download?url={quote(url, safe='')}",
        "expires_in": CACHE_TTL_PUBLIC,
    }

def _filename(media: dict) -> str:
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(media.get("title") or "download")).strip(" .")[:120]
    ext = re.sub(r"[^A-Za-z0-9]", "", str(media.get("ext") or "mp4")) or "bin"
    name = f"{title or 'download'}.{ext}"
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"

def _open_upstream(media: dict, range_header: str | None, method: str = "GET", timeout: tuple = (10, 35)):
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
                    allow_redirects=False, timeout=timeout,
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

def _upstream_response(upstream, session, media: dict):
    """Turn an opened upstream response into a streamed attachment (GET) or headers only (HEAD)."""
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
        "youtube_via_providers": yt._chain(),
        "platforms": list(PLATFORMS),
        "limits": {
            "resolve_per_minute": RATE_LIMIT,
            "download_starts_per_minute": DL_RATE_LIMIT,
            "batch_max": BATCH_MAX,
            "extract_timeout_sec": min(10, max(1, _int_env("EXTRACT_TIMEOUT_SEC", 10))),
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
    """List downloadable YouTube formats (video <= MAX_VIDEO_HEIGHT, audio always). Links point back at this service."""
    _rate_check("resolve", RATE_LIMIT)
    video_id = yt.extract_video_id(request.args.get("url"))
    found = yt.lookup(video_id, fresh=request.args.get("fresh") == "1")
    base_url = _base_url()
    formats = [{
        "id": fmt["id"],
        "kind": fmt["kind"],
        "quality": fmt["quality"],
        "qualityLabel": fmt["quality"],
        "mimeType": fmt["mime"],
        "bitrate": fmt["bitrate"],
        "size": fmt["size"],
        "provider": fmt["provider"],
        "height": yt._height(fmt["quality"]) or None,
        "download_url": f"{base_url}/api/v1/youtube/download?t={yt.make_token(video_id, found['title'], fmt)}",
    } for fmt in found["formats"]]
    return _json({
        "ok": True,
        "provider": found["provider"],
        "platform": "youtube",
        "videoId": video_id,
        "normalized": found["normalized"],
        "title": found["title"],
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "duration": found["duration"],
        "formats": formats,
        "max_height": yt.MAX_VIDEO_HEIGHT,
        "cache": found["cache"],
        "expires_in": yt.TOKEN_TTL,
    })

@app.route("/api/v1/youtube/download", methods=["GET", "HEAD"])
def api_youtube_download():
    """Stream a chosen YouTube format. On a rejected link, re-resolve once and retry."""
    token = yt.read_token(request.args.get("t"))
    range_header = request.headers.get("Range")
    if range_header and not re.fullmatch(r"bytes=\d*-\d*", range_header.strip()):
        raise ApiError("invalid_range", "نطاق التنزيل غير صالح.", 416)
    if not range_header or range_header.startswith("bytes=0-"):
        _rate_check("download", DL_RATE_LIMIT)

    def attempt(target_url: str, fmt_like: dict):
        media = {"url": target_url, "headers": {"User-Agent": yt.UA},
                 **yt.file_info(token.get("t") or "YouTube", fmt_like)}
        upstream, session = _open_upstream(media, range_header, request.method, timeout=(4, 12))
        ctype = (upstream.headers.get("Content-Type") or "").lower()
        if upstream.status_code not in (200, 206, 416) or ctype.startswith(("text/", "application/json")):
            status = upstream.status_code
            upstream.close()
            session.close()
            raise ApiError("youtube_source_rejected", "رفض مصدر الملف الرابط الحالي.", 502,
                           upstream_status=status if status >= 400 else None)
        return media, upstream, session

    try:
        media, upstream, session = attempt(token["u"], token)
    except ApiError as first:
        if first.code in ("forbidden_url", "invalid_range"):
            raise
        found = yt.lookup(token["v"], fresh=True)
        fmt = yt.match_format(found["formats"], token)
        if not fmt:
            raise ApiError("youtube_no_format", "لم تعد هذه الجودة متاحة؛ اختر جودة أخرى.", 502, retry_after=3) from None
        try:
            media, upstream, session = attempt(fmt["url"], {"m": fmt["mime"], "q": fmt["quality"]})
        except ApiError as second:
            raise ApiError("youtube_download_failed",
                           "تعذر بدء التنزيل من المصدر بعد محاولتين؛ جرّب جودة أخرى أو أعد المحاولة لاحقًا.",
                           502, retry_after=5, upstream_status=second.upstream_status or first.upstream_status,
                           details={"first": first.code, "second": second.code}) from None
    return _upstream_response(upstream, session, media)

def _ffmpeg_exe() -> str | None:
    """Locate ffmpeg: the static binary bundled by imageio-ffmpeg, else one on PATH."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return shutil.which("ffmpeg")

def _parse_time_param(name: str) -> float | None:
    """Parse '30', '0:30', '1:02:03' into seconds; reject NaN/Inf/negative."""
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    parts = str(raw).strip().split(":")
    try:
        if len(parts) == 3:
            value = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            value = int(parts[0]) * 60 + float(parts[1])
        else:
            value = float(parts[0])
    except ValueError:
        raise ApiError("invalid_time", f"قيمة {name} غير صالحة؛ استخدم ثانية أو m:ss أو h:mm:ss.", 400) from None
    if not math.isfinite(value) or value < 0:
        raise ApiError("invalid_time", f"قيمة {name} يجب أن تكون رقمًا موجبًا.", 400)
    return value

@app.route("/api/v1/youtube/trim", methods=["GET", "HEAD"])
def api_youtube_trim():
    """Fetch an audio format, trim it with ffmpeg and stream the result.

    Query: t=<audio token>  start=<sec>  end=<sec>  reencode=0|1. Only 'audio' tokens are accepted.
    """
    token = yt.read_token(request.args.get("t"))
    if token.get("k") != "audio":
        raise ApiError("not_audio", "هذا الرابط ليس لصيغة صوتية.", 400)

    start = _parse_time_param("start")
    end = _parse_time_param("end")
    if start is None and end is None:
        raise ApiError("invalid_request", "حدد start و/أو end للاقتصاص.", 400)
    s = start or 0.0
    if end is not None and end <= s:
        raise ApiError("invalid_request", "end يجب أن يكون أكبر من start.", 400)
    reencode = request.args.get("reencode") == "1"

    if not request.headers.get("Range"):
        _rate_check("download", DL_RATE_LIMIT)
    if request.method == "HEAD":
        return Response(b"", status=200, headers={"Cache-Control": "no-store"})

    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise ApiError("ffmpeg_missing", "أداة ffmpeg غير متوفرة على الخادم.", 500)

    ext = "m4a" if "mp4" in (token.get("m") or "").lower() else "webm"
    rid = uuid.uuid4().hex                                                                            
    src = f"/tmp/yt_src_{rid}.bin"
    out = f"/tmp/yt_out_{rid}.{ext}"

    def _cleanup() -> None:
        for path in (src, out):
            try:
                os.remove(path)
            except OSError:
                pass

    try:
        media = {"url": token["u"], "headers": {"User-Agent": yt.UA}}
        upstream, session = _open_upstream(media, None, "GET", timeout=(10, 60))
        try:
            if upstream.status_code not in (200, 206):
                raise from_upstream_status(upstream.status_code)
            with open(src, "wb") as fh:
                for chunk in upstream.iter_content(CHUNK_SIZE):
                    if chunk:
                        fh.write(chunk)
        finally:
            upstream.close()
            session.close()

        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if s > 0:
            args += ["-ss", f"{s:.3f}"]
        args += ["-i", src]
        if end is not None:
            args += ["-t", f"{end - s:.3f}"]
        if reencode:                                                  
            args += ["-c:a", "aac", "-b:a", "192k"] if ext == "m4a" else ["-c:a", "libopus", "-b:a", "128k"]
        else:
            args += ["-c", "copy"]
        args += [out]
        try:
            subprocess.run(args, check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired:
            raise ApiError("ffmpeg_timeout", "تجاوز الاقتصاص المهلة القصوى.", 504, retry_after=5) from None
        except subprocess.CalledProcessError as exc:
            snippet = (exc.stderr or b"").decode("utf-8", "replace")[:200]
            raise ApiError("ffmpeg_failed", f"فشل الاقتصاص: {snippet}", 500) from None

        tag = f"{s:g}s-{end:g}s" if end is not None else f"from {s:g}s"
        media_info = {"title": f"{token.get('t') or 'audio'} [{tag}]", "ext": ext}
        try:
            os.remove(src)                                                                            
        except OSError:
            pass

        def _iter():
            try:
                with open(out, "rb") as fh:
                    while True:
                        chunk = fh.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                _cleanup()

        return Response(_iter(), mimetype="audio/mp4" if ext == "m4a" else "audio/webm", headers={
            "Content-Disposition": _filename(media_info),
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        })
    except BaseException:
        _cleanup()
        raise

@app.get("/")
def index_page():
    return send_from_directory(PUBLIC_DIR, "index.html")

@app.get("/styles.css")
def styles_page():
    return send_from_directory(PUBLIC_DIR, "styles.css", mimetype="text/css")

@app.get("/img/<path:name>")
def static_img(name: str):
    return send_from_directory(os.path.join(PUBLIC_DIR, "img"), name)

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

    return _upstream_response(upstream, session, media)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)
