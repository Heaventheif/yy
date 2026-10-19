"""Safe, user-facing API errors and upstream status classification."""
from __future__ import annotations

import re

class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry_after: int | None = None,
                 upstream_status: int | None = None, details: dict | None = None,
                 retryable: bool | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.upstream_status = upstream_status
        self.details = details
                                                                                                
        self.retryable = (status in (408, 425, 429, 500, 502, 503, 504)
                          if retryable is None else retryable)

_HTTP_STATUS = re.compile(r"(?:HTTP\s+Error|HTTP\s+|status(?:\s+code)?\s*[:=]?\s*)\s*(\d{3})", re.IGNORECASE)

def from_upstream_status(status: int) -> ApiError:
    if status == 402:
        return ApiError("source_payment_required", "أعاد مصدر الوسائط HTTP 402؛ لم تُستخدم خدمة API مدفوعة هنا.", 502, upstream_status=402)
    if status in (401, 403):
        return ApiError("source_forbidden", f"رفضت المنصة الوصول (HTTP {status}). قد يكون المحتوى خاصًا أو محميًا أو غير متاح من خادم سحابي.", 502, upstream_status=status)
    if status in (404, 410):
        return ApiError("not_found", "المحتوى غير موجود أو لم يعد متاحًا.", 404, upstream_status=status)
    if status == 429:
        return ApiError("source_rate_limited", "حدّت المنصة من الطلبات مؤقتًا؛ انتظر ثم أعد المحاولة.", 502, retry_after=10, upstream_status=status)
    return ApiError("upstream_error", f"رفض مصدر الوسائط الطلب (HTTP {status}).", 502, upstream_status=status)

def classify_extractor_error(exc: BaseException) -> ApiError:
    text = f"{type(exc).__name__}: {exc}"
    match = _HTTP_STATUS.search(text)
    if match:
        return from_upstream_status(int(match.group(1)))

    low = text.lower()
    if any(token in low for token in ("unsupported url", "no suitable extractor", "no video formats")):
        return ApiError("unsupported", "لم يعثر المستخرج على وسيط قابل للتنزيل لهذا الرابط.", 422)
    if any(token in low for token in ("login", "log in", "sign in", "private video", "members-only",
                                      "age-restricted", "confirm your age", "cookies are needed")):
        return ApiError("source_forbidden", "المحتوى يتطلب تسجيل الدخول أو صلاحية لا توفرها هذه الخدمة.", 422)
    if any(token in low for token in ("sslerror", "ssl: ", "certificate verify failed", "tls handshake", "tls error")):
        return ApiError("upstream_tls_error", "تعذر إتمام اتصال HTTPS بالمصدر على المنفذ 443.", 502)
    if any(token in low for token in ("timed out", "timeout")):
        return ApiError("upstream_timeout", "انتهت مهلة الاتصال بمصدر الوسائط.", 504, retry_after=5)
    if any(token in low for token in ("connection error", "connectionerror", "connection reset", "connection refused",
                                      "connection aborted", "name or service not known", "failed to establish a new connection",
                                      "temporary failure in name resolution", "network is unreachable", "no route to host",
                                      "max retries exceeded", "httpsconnectionpool")):
        return ApiError("upstream_unreachable", "تعذر الاتصال بخادم المنصة عبر HTTPS؛ قد تكون وجهة الخروج السحابية محجوبة.", 502)
    return ApiError("extract_failed", "تعذر استخراج رابط التنزيل من هذا المحتوى.", 502)
