"""بوابة API عامة (بلا مفاتيح) لاستخراج روابط التنزيل وتمريرها.

  GET  /api/v1/resolve?url=…     رابط واحد -> JSON
  POST /api/v1/batch             حتى 10 روابط بالتوازي -> JSON أو NDJSON متدفق (?stream=1)
  GET  /api/v1/download?url=…    تمرير الملف (Range / HEAD / redirect=1)
  GET  /api/v1/health            الحالة والحدود

تجنّب N+1: كل رابط يُستخرج مرة واحدة (كاش + دمج الطلبات المتزامنة المتطابقة + كاش سلبي للفشل)،
والدفعات تعمل بالتوازي بدل طلب لكل رابط. التفاصيل في API.md.
"""
import os
import re
import uuid
import socket
import ipaddress
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from urllib.parse import quote, urljoin, urlsplit
import json

import requests
import yt_dlp
from flask import Flask, Response, g, jsonify, redirect, request

# Vercel: نظام الملفات للقراءة فقط عدا /tmp
os.environ.setdefault("HOME", "/tmp")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/.cache")

app = Flask(__name__)
app.json.ensure_ascii = False
app.json.sort_keys = False

# ---------------------------- الإعدادات (كلها اختيارية) ----------------------------
def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


VERSION = "1"
REDIRECT = os.getenv("REDIRECT", "0") == "1"             # الافتراضي لـ download: 302 للرابط المباشر
ALLOW_GENERIC = os.getenv("ALLOW_GENERIC", "0") == "1"
RATE_LIMIT = _int("RATE_LIMIT_PER_MIN", 60)              # وحدات resolve/batch لكل IP في الدقيقة (0 = معطّل)
DL_RATE_LIMIT = _int("DL_RATE_LIMIT_PER_MIN", 20)        # بدايات التنزيل لكل IP في الدقيقة
BATCH_MAX = _int("BATCH_MAX", 10)
EXTRACT_TIMEOUT = _int("EXTRACT_TIMEOUT_SEC", 30)
MAX_PENDING = _int("MAX_PENDING_EXTRACTIONS", 24)        # فوقه: 503 مع Retry-After
CACHE_TTL = _int("CACHE_TTL_SEC", 120)
NEG_TTL = 15                                             # كاش الفشل (يمنع إعادة المحاولة المحمومة)
CACHE_MAX = 128
MAX_URL_LEN = 2048
CHUNK = 256 * 1024
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")

BLOCKED_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com", "ytimg.com")
FORMAT = "b[protocol=https]/b[protocol=http]/ba[protocol=https]/ba[protocol=http]"

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_BARE_RE = re.compile(r"^[\w-]+(\.[\w-]+)+(/\S*)?$")


class ApiError(Exception):
    def __init__(self, code, message, status=400, retry_after=None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


# ---------------------------- الروابط والأمان ----------------------------
def _host_blocked(host):
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS)


def is_public(url):
    """منع SSRF: http(s) فقط، وكل عناوين المضيف عامة."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme not in ("http", "https") or not host:
            return False
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        for a in addrs:
            ip = ipaddress.ip_address(a.split("%")[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if not ip.is_global:
                return False
        return bool(addrs)
    except (OSError, ValueError):
        return False


def clean_url(raw):
    """أول رابط في النص (يقبل نص المشاركة، أو دومين بلا https)."""
    raw = (raw or "").strip() if isinstance(raw, str) else ""
    if not raw or len(raw) > MAX_URL_LEN or any(ord(c) < 32 for c in raw):
        raise ApiError("invalid_url", "الرابط غير صحيح")
    m = _URL_RE.search(raw)
    if m:
        url = m.group(0).rstrip(".,;)!?")
    elif _BARE_RE.match(raw):
        url = "https://" + raw
    else:
        raise ApiError("invalid_url", "الرابط غير صحيح")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.username is not None or parts.password is not None or not host:
            raise ValueError
    except ValueError:
        raise ApiError("invalid_url", "الرابط غير صحيح")
    if _host_blocked(host):
        raise ApiError("blocked_site", "تنزيل يوتيوب غير مدعوم")
    if not is_public(url):
        raise ApiError("forbidden_url", "الرابط غير مسموح")
    return url


# ---------------------------- تحديد المعدل (token bucket لكل IP) ----------------------------
_buckets = {}
_rl_lock = threading.Lock()


def client_ip():
    h = request.headers
    ip = (h.get("X-Vercel-Forwarded-For") or h.get("X-Real-IP") or
          (h.get("X-Forwarded-For") or "").split(",")[0].strip() or request.remote_addr or "?")
    return ip.strip()


def rate_check(kind, limit, cost=1):
    """يستهلك cost من حصة IP. يرجع (limit, remaining, reset_sec) أو يرمي 429."""
    if limit <= 0:
        return None
    now, key = time.monotonic(), (kind, client_ip())
    rate = limit / 60.0
    with _rl_lock:
        if len(_buckets) > 5000:
            for k in [k for k, (t, ts) in _buckets.items() if now - ts > 120][:2500] or list(_buckets)[:1000]:
                _buckets.pop(k, None)
        tokens, ts = _buckets.get(key, (float(limit), now))
        tokens = min(float(limit), tokens + (now - ts) * rate)
        if tokens < cost:
            wait = max(1, int((cost - tokens) / rate) + 1)
            _buckets[key] = (tokens, now)
            g.rl = (limit, 0, wait)
            raise ApiError("rate_limited", "طلبات كثيرة، أعد المحاولة بعد قليل", 429, retry_after=wait)
        tokens -= cost
        _buckets[key] = (tokens, now)
    reset = int((limit - tokens) / rate) + 1
    g.rl = (limit, int(tokens), reset)
    return g.rl


# ---------------------------- الاستخراج: كاش + دمج الطلبات + مهلة ----------------------------
_cache = {}          # url -> (ts, ttl, media | ApiError)
_inflight = {}       # url -> Future   (singleflight)
_lock = threading.Lock()
_pending = threading.BoundedSemaphore(MAX_PENDING)
_EXTRACT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="extract")
_BATCH_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="batch")


def _opts():
    allowed = ["default", "-.*youtube.*"]
    if not ALLOW_GENERIC:
        allowed.append("-generic")
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
        "playlist_items": "1", "format": FORMAT, "cachedir": False, "ignoreconfig": True,
        "socket_timeout": 15, "retries": 2, "extractor_retries": 1, "color": "never",
        "allowed_extractors": allowed,
    }
    if os.getenv("PROXY"):
        opts["proxy"] = os.getenv("PROXY")
    return opts


def _friendly(e):
    m = str(e).lower()
    if "unsupported url" in m or "no suitable extractor" in m:
        return ("unsupported", "الموقع أو الرابط غير مدعوم", 422)
    if any(k in m for k in ("log in", "login", "sign in", "private video", "members-only",
                            "confirm your age", "age-restricted", "cookies")):
        return ("private", "المحتوى خاص أو يتطلب تسجيل دخول", 422)
    if any(k in m for k in ("http error 404", "not found", "has been removed", "video unavailable",
                            "does not exist")):
        return ("not_found", "المحتوى غير موجود أو محذوف", 404)
    if "requested format is not available" in m or "no video formats" in m:
        return ("no_format", "لا يوجد ملف قابل للتنزيل المباشر لهذا الرابط", 422)
    return ("extract_failed", "تعذر استخراج رابط التنزيل", 502)


def _extract(url):
    try:
        with yt_dlp.YoutubeDL(_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as e:
        raise ApiError(*_friendly(e))
    finally:
        _pending.release()
    if info and info.get("_type") == "playlist":
        info = next((x for x in (info.get("entries") or []) if x), None)
    if info and info.get("requested_formats"):   # يتطلب دمجا: غير مدعوم
        info = None
    media = info and info.get("url")
    if not media or (info.get("protocol") or "https") not in ("http", "https"):
        raise ApiError("no_format", "لا يوجد ملف قابل للتنزيل المباشر لهذا الرابط", 422)
    if (info.get("extractor_key") or "").lower().startswith("youtube") or not is_public(media):
        raise ApiError("forbidden_url", "الرابط غير مسموح", 403)
    headers = {k: v for k, v in (info.get("http_headers") or {}).items() if k.lower() != "accept-encoding"}
    return {"title": info.get("title") or "download", "ext": info.get("ext") or "mp4", "url": media,
            "headers": headers, "size": info.get("filesize") or info.get("filesize_approx"),
            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
            "site": info.get("extractor_key")}


def _run_extract(url):
    if not _pending.acquire(blocking=False):
        raise ApiError("busy", "الخدمة مشغولة، أعد المحاولة بعد لحظات", 503, retry_after=3)
    fut = _EXTRACT_POOL.submit(_extract, url)
    try:
        return fut.result(timeout=EXTRACT_TIMEOUT)
    except FutureTimeout:
        raise ApiError("timeout", "استغرق الاستخراج وقتا أطول من المسموح", 504, retry_after=5)


def resolve(url, fresh=False):
    """استخراج واحد لكل رابط مهما تكرر الطلب: كاش، ثم دمج المتزامن، ثم كاش سلبي للفشل."""
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and not fresh and now - hit[0] < hit[1]:
            if isinstance(hit[2], ApiError):
                raise hit[2]
            return hit[2]
        fut = _inflight.get(url)
        owner = fut is None
        if owner:
            fut = _inflight[url] = Future()
    if not owner:
        try:
            return fut.result(timeout=EXTRACT_TIMEOUT + 5)
        except FutureTimeout:
            raise ApiError("timeout", "استغرق الاستخراج وقتا أطول من المسموح", 504, retry_after=5)
    try:
        media, ttl = _run_extract(url), CACHE_TTL
        result = media
    except ApiError as e:
        media, ttl, result = e, (0 if e.status >= 500 and e.code in ("busy", "timeout") else NEG_TTL), e
    except Exception as e:           # خطأ غير متوقع: لا يُخزَّن
        result, media, ttl = e, e, 0
    with _lock:
        if ttl:
            if len(_cache) >= CACHE_MAX:
                _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
            _cache[url] = (now, ttl, media)
        _inflight.pop(url, None)
    if isinstance(result, Exception):
        fut.set_exception(result)
        raise result
    fut.set_result(result)
    return result


# ---------------------------- التمرير ----------------------------
def _open_upstream(media, rng):
    """يفتح المصدر مع تتبع التحويلات يدويا (كل قفزة تُفحص ضد SSRF)."""
    headers = dict(media["headers"])
    if rng:
        headers["Range"] = rng
    target = media["url"]
    for _ in range(5):
        up = requests.get(target, headers=headers, stream=True, timeout=(10, 30), allow_redirects=False)
        if up.status_code in (301, 302, 303, 307, 308) and up.headers.get("Location"):
            target = urljoin(target, up.headers["Location"])
            up.close()
            if not is_public(target):
                raise ApiError("forbidden_url", "الرابط غير مسموح", 403)
            continue
        return up
    raise ApiError("upstream_error", "تحويلات كثيرة من المصدر", 502)


def _filename(media):
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", media["title"]).strip(" .")[:120] or "download"
    ext = re.sub(r"[^\w]", "", media["ext"]) or "bin"
    name = f"{title}.{ext}"
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _stream(up):
    try:
        for piece in up.iter_content(CHUNK):
            if piece:
                yield piece
    finally:
        up.close()


def _base():
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    return f"{proto}://{request.host}"


def _public(url, media, base):
    """الشكل العام لنتيجة ناجحة"""
    return {"ok": True, "input": url, "title": media["title"], "ext": media["ext"], "size": media["size"],
            "duration": media["duration"], "thumbnail": media["thumbnail"], "site": media["site"],
            "download": f"{base}/api/v1/download?url={quote(url, safe='')}",
            "source": {"url": media["url"], "headers": media["headers"]}}


def _error_body(e):
    return {"ok": False, "error": {"code": e.code, "message": e.message}}


# ---------------------------- الهيدرات المشتركة / CORS / الأخطاء ----------------------------
@app.before_request
def _begin():
    g.rid = request.headers.get("X-Request-Id", "")[:64] or uuid.uuid4().hex[:16]
    g.t0 = time.perf_counter()
    g.rl = None


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        resp.headers["X-Request-Id"] = g.get("rid", "")
        resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Range, X-Request-Id"
        resp.headers["Access-Control-Expose-Headers"] = (
            "Content-Disposition, Content-Length, Content-Range, Accept-Ranges, Retry-After, "
            "RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset, X-Request-Id, Server-Timing")
        resp.headers["Access-Control-Max-Age"] = "86400"
        if CORS_ORIGIN != "*":
            resp.headers["Vary"] = "Origin"
        rl = g.get("rl")
        if rl:
            resp.headers["RateLimit-Limit"], resp.headers["RateLimit-Remaining"], resp.headers["RateLimit-Reset"] = map(str, rl)
        resp.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - g.t0) * 1000:.0f}"
    return resp


def _json(body, status=200, retry_after=None):
    resp = jsonify(body)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    if retry_after:
        resp.headers["Retry-After"] = str(retry_after)
    return resp


@app.errorhandler(ApiError)
def _api_error(e):
    return _json(_error_body(e), e.status, e.retry_after)


@app.errorhandler(404)
def _nf(e):
    return _json(_error_body(ApiError("not_found", "المسار غير موجود", 404)), 404)


@app.errorhandler(405)
def _na(e):
    return _json(_error_body(ApiError("method_not_allowed", "الطريقة غير مسموحة", 405)), 405)


@app.errorhandler(Exception)
def _any_error(e):
    app.logger.exception("[%s] unhandled: %s", g.get("rid"), e)
    return _json(_error_body(ApiError("internal", "حدث خطأ غير متوقع", 500)), 500)


# ---------------------------- المسارات ----------------------------
@app.get("/api/v1/health")
def health():
    return _json({"ok": True, "version": VERSION,
                  "limits": {"rate_per_min": RATE_LIMIT, "download_starts_per_min": DL_RATE_LIMIT,
                             "batch_max": BATCH_MAX, "extract_timeout_sec": EXTRACT_TIMEOUT}})


@app.get("/api/v1/resolve")
def api_resolve():
    rate_check("r", RATE_LIMIT, 1)
    url = clean_url(request.args.get("url"))
    return _json(_public(url, resolve(url), _base()))


def _one(raw, base):
    """نتيجة عنصر واحد في الدفعة (لا ترمي أبدا)"""
    try:
        url = clean_url(raw)
        return _public(url, resolve(url), base)
    except ApiError as e:
        return {"ok": False, "input": raw if isinstance(raw, str) else None, **_error_body(e)}
    except Exception as e:
        app.logger.exception("batch item failed: %s", e)
        return {"ok": False, "input": raw if isinstance(raw, str) else None,
                **_error_body(ApiError("internal", "حدث خطأ غير متوقع", 500))}


@app.post("/api/v1/batch")
def api_batch():
    body = request.get_json(silent=True)
    urls = body.get("urls") if isinstance(body, dict) else None
    if not isinstance(urls, list) or not urls:
        raise ApiError("invalid_request", 'الجسم يجب أن يكون {"urls": ["…"]}')
    if len(urls) > BATCH_MAX:
        raise ApiError("too_many_urls", f"الحد الأقصى {BATCH_MAX} روابط في الطلب", 413)
    unique = list(dict.fromkeys(u if isinstance(u, str) else "" for u in urls))   # بلا تكرار
    rate_check("r", RATE_LIMIT, len(unique))
    base = _base()                     # يُحسب هنا: الخيوط الفرعية بلا سياق طلب
    futs = {_BATCH_POOL.submit(_one, u, base): i for i, u in enumerate(unique)}
    if request.args.get("stream") == "1" or "ndjson" in request.headers.get("Accept", ""):
        def lines():                       # كل سطر يخرج فور جاهزيته (لا انتظار لأبطأ رابط)
            for f in as_completed(futs):
                yield json.dumps({"index": futs[f], **f.result()}, ensure_ascii=False) + "\n"
        resp = Response(lines(), mimetype="application/x-ndjson")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp
    results = [None] * len(unique)
    for f, i in futs.items():
        results[i] = f.result()
    return _json({"ok": True, "count": len(results), "results": results})


@app.route("/api/v1/download", methods=["GET", "HEAD"])
def api_download():
    url = clean_url(request.args.get("url"))
    rng = request.headers.get("Range")
    if not rng or re.match(r"^bytes=0-", rng):          # أجزاء الاستئناف اللاحقة لا تُحسب
        rate_check("d", DL_RATE_LIMIT, 1)
    media = resolve(url)
    mode = request.args.get("redirect")
    if (REDIRECT if mode is None else mode == "1"):
        resp = redirect(media["url"], 302)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    try:
        up = _open_upstream(media, rng)
        if up.status_code in (401, 403, 404, 410):      # الرابط المباشر انتهى: استخراج جديد مرة واحدة
            up.close()
            media = resolve(url, fresh=True)
            up = _open_upstream(media, rng)
    except requests.RequestException:
        raise ApiError("upstream_error", "تعذر الاتصال بمصدر الملف", 502, retry_after=2)
    if up.status_code not in (200, 206):
        code = up.status_code
        up.close()
        raise ApiError("upstream_error", f"رفض المصدر الطلب ({code})", 502)
    out = Response(_stream(up), status=up.status_code,
                   content_type=up.headers.get("Content-Type") or "application/octet-stream",
                   direct_passthrough=True)
    for h in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
        if up.headers.get(h):
            out.headers[h] = up.headers[h]
    out.headers.setdefault("Accept-Ranges", "bytes")
    out.headers["Content-Disposition"] = _filename(media)
    out.headers["Cache-Control"] = "no-store"
    out.headers["X-Accel-Buffering"] = "no"
    return out


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)
