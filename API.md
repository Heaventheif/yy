# API الخدمة

الخدمة عامة بلا مفاتيح. عوّض `https://YOUR-APP.vercel.app` بعنوان مشروعك. واجهة API الحالية `v1`. جميع الردود JSON بترميز UTF-8 عدا مسار التنزيل. **يعيد `resolve` الحقل `direct_url` كرابط CDN مباشر قصير العمر، وقد يعيد أيضًا `download` كمسار تمرير متوافق مع الإصدارات السابقة.**

تقبل واجهة API رابطًا عامًا واحدًا من النطاقات المسجلة فقط. YouTube غير مدعوم داخل API الخاصة بـ`yy`؛ الواجهة تطبع رابط YouTube إلى صيغة `www.youtube.com/watch?v=...`، ثم تطلب الصيغ أولًا من Toshiro عبر `https://toshiro-api-editz6t9.vercel.app/api/downloader/alldl?url=...`. إذا فشل Toshiro، تستخدم VidSnap كخيار احتياطي عبر `https://api.vidsnap.app/parse?id=VIDEO_ID` أو رابط YouTube القياسي.

روابط VidSnap الاحتياطية تُمرر عبر `https://api.vidsnap.app/proxy?u=MEDIA_URL`، بينما روابط Toshiro الناتجة تُستخدم كما يعيدها المزود.

## المسارات

| الطريقة | المسار | الوصف |
|---|---|---|
| GET | `/api/v1/resolve?url=` | استخراج العنوان والبيانات والرابط المباشر `direct_url` |
| POST | `/api/v1/batch` | استخراج عدة روابط دفعة واحدة؛ `?stream=1` يعيد NDJSON |
| GET / HEAD | `/api/v1/download?url=` | تمرير الملف مع `Range` واسم مناسب |
| GET | `/api/v1/health` | حالة الخدمة وقائمة المواقع المعرفة |

### حل رابط واحد

```bash
curl --get 'https://YOUR-APP.vercel.app/api/v1/resolve' \
  --data-urlencode 'url=https://www.facebook.com/watch/?v=123456789'
```

رد مختصر:

```json
{
  "ok": true,
  "input": "https://www.facebook.com/watch/?v=123456789",
  "title": "عنوان الملف",
  "ext": "mp4",
  "size": 12345678,
  "duration": 212,
  "site": "Facebook",
  "direct_url": "https://cdn.example/temporary-signed-url",
  "download": "https://YOUR-APP.vercel.app/api/v1/download?url=…",
  "expires_in": 75
}
```

`direct_url` رابط مؤقت صادر من المنصة وقد ينتهي قبل `expires_in`؛ استخدمه فورًا. `expires_in` هو عمر نتيجة الكاش بالثواني وليس ضمانًا لصلاحية رابط CDN. الحقل `download` باقٍ للتوافق ويستمر في تمرير الملف عبر الخادم. `fresh=1` يتجاوز نتيجة الكاش لهذا الطلب.

### تنزيل الملف

```bash
curl -L -OJ --get 'https://YOUR-APP.vercel.app/api/v1/download' \
  --data-urlencode 'url=https://www.facebook.com/watch/?v=123456789'
curl -r 0-1048575 -o part.bin 'https://YOUR-APP.vercel.app/api/v1/download?url=…'
```

يدعم المسار `GET` و`HEAD` واستئناف `Range`. يعيد استخراج الرابط مرة واحدة إذا رفض المصدر رابط CDN منتهيًا بـ 401/403/404/410. `redirect=1` اختياري ويعيد 302 إلى CDN بدل التمرير؛ قد يفشل إذا كان الرابط مربوطًا بـ IP أو `Referer`. لا تعرض الخدمة raw CDN URL في JSON.

### الدفعات

```bash
curl -X POST 'https://YOUR-APP.vercel.app/api/v1/batch' \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://www.facebook.com/watch/?v=123456789","https://vimeo.com/76979871"]}'
```

الحد الافتراضي 8 روابط (أقصاه 10). تُحفظ النتائج بترتيب الإدخال، ويُعزل فشل الرابط عن بقية النتائج. لا تُرسل مدخلات أكبر من 512 KB.

```bash
curl -N -X POST 'https://YOUR-APP.vercel.app/api/v1/batch?stream=1' \
  -H 'Content-Type: application/json' \
  -d '{"urls":["…","…"]}'
```

عند تفعيل `stream=1` يصل سطر JSON لكل عنصر مع `index`؛ الترتيب هنا هو ترتيب اكتمال التنفيذ، لا ترتيب الإدخال.

## الأخطاء

```json
{
  "ok": false,
  "error": {"code":"source_forbidden","message":"…","upstream_status":403},
  "request_id":"…"
}
```

| الرمز | HTTP لدى الخدمة | التفسير |
|---|---:|---|
| `invalid_url`, `invalid_request` | 400 | صيغة الرابط/الطلب غير صحيحة |
| `youtube_unsupported`, `unsupported`, `official_api_required` | 422 | موقع غير مدعوم أو يحتاج تكامل API رسميًا |
| `forbidden_url` | 403 | نطاق داخلي أو عنوان غير عام أو وجهة تحويل غير آمنة |
| `source_payment_required` | 502 | أعاد المصدر `upstream_status: 402`؛ ليس دليلًا تلقائيًا على وجود اشتراك في خدمة yy |
| `source_forbidden` | 502 أو 422 | أعاد المصدر 401/403 أو يتطلب صلاحية دخول |
| `source_rate_limited` | 502 | المصدر حدّ الطلبات؛ انتظر ثم أعد المحاولة |
| `upstream_tls_error`, `upstream_unreachable` | 502 | فشل اتصال TLS/HTTPS أو تعذر الوصول للمصدر؛ 443 عادة رقم منفذ HTTPS لا HTTP status |
| `not_found` | 404 | المحتوى محذوف أو الرابط غير صالح |
| `rate_limited` | 429 | تجاوز الحد المحلي؛ افحص `Retry-After` |
| `busy` | 503 | حد استخراج متزامن لكل نسخة؛ أعد المحاولة |
| `timeout`, `upstream_timeout` | 504 | تجاوز مهلة الاستخراج/الاتصال |

يصدر كل رد من API ترويسة `X-Request-Id`. عند الأخطاء upstream تسجل Vercel رمز الحالة والنطاق (لا تسجل الاستعلام أو رابط CDN) لتسهيل التشخيص.

## المعدلات والكاش

القيم الافتراضية: 60 طلب resolve/batch و20 بداية تنزيل في الدقيقة لكل IP، وكاش استخراج 75 ثانية، وحد دفعة 8، و8 عمليات استخراج متزامنة لكل نسخة. `Range` غير الصفري لا يُحسب كبداية تنزيل. هذه القيود والكاش في ذاكرة Function محلية، لذا تختلف باختلاف النسخة ولا تمثل حدًا عالميًا؛ استخدم Vercel Firewall للحماية الصارمة.

## متغيرات البيئة

| المتغير | الافتراضي | الغرض |
|---|---:|---|
| `RATE_LIMIT_PER_MIN` | 60 | حد resolve/batch لكل IP؛ 0 لتعطيله |
| `DL_RATE_LIMIT_PER_MIN` | 20 | حد بدايات التنزيل لكل IP؛ 0 لتعطيله |
| `BATCH_MAX` | 8 | حد الدفعة (10 كحد أقصى) |
| `EXTRACT_TIMEOUT_SEC` | 10 | مهلة انتظار نتيجة الاستخراج (حد أقصى 10 ثوانٍ؛ لا تقتل الخيط العامل تلقائيًا) |
| `MAX_PENDING_EXTRACTIONS` | 8 | سقف الاستخراجات لكل نسخة |
| `CACHE_TTL_SEC` | 75 | عمر نتيجة الكاش |
| `CORS_ORIGIN` | `*` | أصل CORS؛ الأفضل ضبط أصل موقعك |
| `REDIRECT` | `0` | `1` لتفعيل تحويل CDN افتراضيًا |

Vercel يتيح بث Python، لكنه يفرض حدًا للمدة وحجم الحمولة حسب وثائقه وخطتك؛ لا يصلح هذا API كبديل عن مخزن أو خدمة نقل مخصصة للملفات الكبيرة جدًا.

## YouTube (عبر مزودين خارجيين)

لا يستخرج `yy` من YouTube مباشرة؛ الواجهة تطلب الصيغ من مزودين خارجيين عبر الخادم، ثم تمرر الملف عبر نفس النطاق. لا يظهر رابط المزود الخام في JSON.

| الطريقة | المسار | الوصف |
|---|---|---|
| GET | `/api/v1/youtube/formats?url=` | قائمة الصيغ (حتى 14) مع `download_url` موقّع؛ `fresh=1` يتجاوز الكاش (30 ثانية) |
| GET / HEAD | `/api/v1/youtube/download?t=` | بث الصيغة المختارة مع `Range`؛ عند رفض المصدر للرابط يعيد الاستخراج مرة واحدة ويجرب صيغة مطابقة |

رمز `t` موقّع بـHMAC وصالح 10 دقائق (`YT_TOKEN_TTL_SEC`)؛ بعد انتهائه يعيد `410 link_expired` فتطلب الواجهة الصيغ من جديد. المزودون يُجرَّبون بالتوازي وبحسب الأولوية (`toshiro` ثم `vidsnap` ثم `piped` و`invidious` إن عُرّفت أسماء النسخ)، ومن يفشل 3 مرات متتالية يُتجاهل 45 ثانية.

### غلاف الخطأ (إضافة غير كاسرة)

كل خطأ يحمل الآن `retryable`، وأخطاء YouTube تحمل `details.providers` لتشخيص 502:

```json
{
  "ok": false,
  "error": {
    "code": "youtube_providers_unavailable",
    "message": "…",
    "retryable": true,
    "details": {"providers": [{"provider": "toshiro", "reason": "http_502", "status": 502}, {"provider": "vidsnap", "reason": "timeout"}]}
  },
  "request_id": "…"
}
```

| الرمز | HTTP | المعنى |
|---|---:|---|
| `invalid_youtube_url` | 400 | الرابط ليس رابط YouTube صالحًا |
| `youtube_providers_unavailable` | 502 | كل المزودين فشلوا أو لم يعيدوا صيغًا؛ أعد المحاولة (`Retry-After`) |
| `youtube_timeout` | 504 | كل المزودين تجاوزوا المهلة |
| `youtube_download_failed` | 502 | رفض المصدر الملف بعد إعادة الاستخراج مرة واحدة |
| `youtube_no_format` | 502 | لم تعد الجودة المطلوبة متاحة |
| `invalid_token` | 400 | رمز تنزيل مزوّر أو تالف |
| `link_expired` | 410 | انتهت صلاحية الرمز؛ اطلب الصيغ من جديد |

متغيرات بيئة إضافية: `SIGNING_SECRET` (مهم: عيّنه بقيمة عشوائية طويلة)، `YT_PROVIDERS` (ترتيب المزودين)، `PIPED_INSTANCES` و`INVIDIOUS_INSTANCES` (قائمة عناوين `https://` مفصولة بفواصل، غير مفعّلة افتراضيًا)، `YT_TOKEN_TTL_SEC`.
