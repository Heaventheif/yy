"""Per-instance cache, single-flight request coalescing and bounded extraction."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout

from .errors import ApiError
from .extractor import extract_media


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


CACHE_TTL = min(300, _int_env("CACHE_TTL_SEC", 75))
EXTRACT_TIMEOUT = min(10, _int_env("EXTRACT_TIMEOUT_SEC", 10))
MAX_PENDING = min(8, _int_env("MAX_PENDING_EXTRACTIONS", 4))
CACHE_MAX = 200
_NEGATIVE_TTL = 8

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
_inflight: dict[str, Future] = {}
_pool = ThreadPoolExecutor(max_workers=MAX_PENDING, thread_name_prefix="socialdl")
_slots = threading.BoundedSemaphore(MAX_PENDING)


def _work(url: str):
    try:
        return extract_media(url)
    finally:
        _slots.release()


def resolve_media(url: str, fresh: bool = False) -> dict:
    """Resolve once per URL, cache briefly and share concurrent work per instance."""
    now = time.monotonic()
    with _lock:
        if fresh:
            _cache.pop(url, None)
        cached = _cache.get(url)
        if cached and not fresh and now - cached[0] < CACHE_TTL:
            value = cached[1]
            if isinstance(value, ApiError):
                raise value
            return value
        running = _inflight.get(url)
        owner = running is None
        if owner:
            if not _slots.acquire(blocking=False):
                raise ApiError("busy", "الخدمة مزدحمة الآن؛ أعد المحاولة بعد لحظات.", 503, retry_after=3)
            running = Future()
            _inflight[url] = running

    if not owner:
        try:
            return running.result(timeout=EXTRACT_TIMEOUT + 1)
        except FutureTimeout:
            raise ApiError("timeout", "انتهت مهلة انتظار نتيجة الاستخراج.", 504, retry_after=5)

    try:
        worker = _pool.submit(_work, url)
    except Exception:
        _slots.release()
        with _lock:
            _inflight.pop(url, None)
        raise ApiError("busy", "تعذر بدء عملية الاستخراج حاليًا.", 503, retry_after=3)

    try:
        result = worker.result(timeout=EXTRACT_TIMEOUT)
        cache_ttl = CACHE_TTL
    except FutureTimeout:
        result = ApiError("timeout", "انتهت مهلة استخراج الوسائط من المنصة.", 504, retry_after=5)
        cache_ttl = 0
    except ApiError as exc:
        result = exc
        cache_ttl = _NEGATIVE_TTL if exc.status < 500 else 0
    except Exception:
        result = ApiError("extract_failed", "تعذر استخراج رابط التنزيل.", 502)
        cache_ttl = 0

    with _lock:
        _inflight.pop(url, None)
        if cache_ttl:
            if len(_cache) >= CACHE_MAX:
                oldest = min(_cache, key=lambda key: _cache[key][0])
                _cache.pop(oldest, None)
            _cache[url] = (time.monotonic(), result)

    if isinstance(result, ApiError):
        running.set_exception(result)
        raise result
    running.set_result(result)
    return result
