# توثيق الـ API (عام، بلا مفاتيح)

أي شخص يملك رابط النشر يستخدمه مباشرة. استبدل `https://YOUR-APP.vercel.app` برابطك.
الإصدار الحالي: `v1`. عند الفشل يحتوي `error.detail` على السبب التقني للتشخيص (ويُطبع أيضا في سجلات Vercel). كل الردود JSON بترميز UTF-8 عدا التنزيل.

> روابط المشاركة والاختصار (facebook.com/share/…، fb.watch، t.co، bit.ly، vm.tiktok.com…) تُفكّ تلقائيا قبل الاستخراج.
>
> يوتيوب غير مدعوم عمدا. يعمل مع المواقع التي يدعمها yt-dlp ويقدّم ملفا واحدا جاهزا (صوت+فيديو أو صوت فقط) عبر http(s).

## النقاط

| الطريقة | المسار | الوظيفة |
|---|---|---|
| GET | `/api/v1/resolve?url=` | رابط واحد ← بيانات + رابط تنزيل |
| POST | `/api/v1/batch` | حتى 10 روابط بالتوازي (`?stream=1` لـ NDJSON متدفق) |
| GET / HEAD | `/api/v1/download?url=` | تمرير الملف (يدعم `Range`، و`redirect=1`) |
| GET | `/api/v1/health` | الحالة والحدود الحالية |

### `GET /api/v1/resolve`
```bash
curl "https://YOUR-APP.vercel.app/api/v1/resolve?url=https://vimeo.com/76979871"
```
```json
{
  "ok": true,
  "input": "https://vimeo.com/76979871",
  "title": "…", "ext": "mp4", "size": 12345678, "duration": 212,
  "thumbnail": "https://…", "site": "Vimeo",
  "download": "https://YOUR-APP.vercel.app/api/v1/download?url=https%3A%2F%2Fvimeo.com%2F76979871",
  "source": { "url": "https://cdn…/file.mp4", "headers": { "User-Agent": "…" } }
}
```
- `download`: رابط جاهز للمتصفح أو `<a href>`؛ يرجع الملف باسم العنوان (`Content-Disposition`).
- `source`: الرابط المباشر عند المصدر مع الهيدرات اللازمة. استخدمه من خادمك إن أردت تجاوز تمرير الملف عبر هذه الخدمة، لكن قد يكون مقيّدا بـ IP/Referer وينتهي بسرعة.

### `POST /api/v1/batch`
```bash
curl -X POST https://YOUR-APP.vercel.app/api/v1/batch \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://vimeo.com/76979871","https://soundcloud.com/a/b"]}'
```
```json
{ "ok": true, "count": 2, "results": [ { "ok": true, "...": "…" }, { "ok": false, "input": "…", "error": { "code": "unsupported", "message": "…" } } ] }
```
- فشل عنصر لا يُفشل الدفعة: افحص `ok` لكل عنصر. الترتيب كترتيب الإدخال بعد حذف المكرر.
- **تدفق:** أضف `?stream=1` (أو `Accept: application/x-ndjson`) فيصلك سطر JSON لكل رابط فور جاهزيته مع `index`، دون انتظار أبطأ رابط:
```bash
curl -N -X POST "https://YOUR-APP.vercel.app/api/v1/batch?stream=1" -H "Content-Type: application/json" -d '{"urls":["…","…"]}'
```

### `GET /api/v1/download`
```bash
curl -L -OJ "https://YOUR-APP.vercel.app/api/v1/download?url=https://vimeo.com/76979871"
curl -r 0-1048575 -o part.bin "…/api/v1/download?url=…"      # جزء من الملف
```
- يدعم `Range` (استئناف التنزيل) و`HEAD`.
- `redirect=1` ← `302` إلى الرابط المباشر بدل التمرير (يوفّر عرض النطاق؛ قد يفشل مع روابط مقيّدة).
- أقصى مدة للطلب 300 ثانية؛ المتصفحات ومدراء التنزيل يكملون الباقي بـ `Range` تلقائيا.

## الأخطاء
شكل موحّد وكود HTTP مناسب:
```json
{ "ok": false, "error": { "code": "rate_limited", "message": "…" } }
```
| code | HTTP | المعنى |
|---|---|---|
| `invalid_url` / `invalid_request` | 400 | مدخل غير صحيح |
| `blocked_site` | 400 | موقع محجوب (يوتيوب) |
| `forbidden_url` | 400/403 | عنوان داخلي أو غير مسموح |
| `not_found` | 404 | المحتوى محذوف أو المسار خاطئ |
| `too_many_urls` | 413 | تجاوز حد الدفعة |
| `unsupported` / `private` / `no_format` | 422 | موقع غير مدعوم / يتطلب دخولا / لا ملف مباشر |
| `rate_limited` | 429 | تجاوز الحد (انظر `Retry-After`) |
| `extract_failed` / `upstream_error` | 502 | فشل الاستخراج أو المصدر |
| `busy` | 503 | ازدحام (انظر `Retry-After`) |
| `timeout` | 504 | تجاوز مهلة الاستخراج |

## الحدود وتحديد المعدل
لا مفاتيح؛ الحماية بحد لكل IP (token bucket):
- `resolve`/`batch`: **60 وحدة/دقيقة** (الدفعة تستهلك بعدد روابطها الفريدة).
- `download`: **20 بداية تنزيل/دقيقة** (أجزاء الاستئناف `Range` لا تُحسب).

كل رد فيه: `RateLimit-Limit` · `RateLimit-Remaining` · `RateLimit-Reset` (ثوان)، وعند 429/503: `Retry-After`.
كما يرجع `X-Request-Id` (أرسله أنت لتتبّع طلبك) و`Server-Timing`.

> على Vercel كل نسخة دالة تعدّ بذاكرتها، فالحد تقريبي وليس ضمانا صارما. للحد الصارم فعّل Vercel Firewall (Rate Limiting) أمام `/api/*`.

## تجنّب N+1 والتأخير
- **كاش 120 ث** لكل رابط، و**كاش سلبي 15 ث** للفشل (لا إعادة استخراج فاشل متكررة).
- **دمج الطلبات المتزامنة:** عشرة طلبات لنفس الرابط تنتج استخراجا واحدا.
- **الدفعات** تُنفَّذ بالتوازي وبلا تكرار: استعمل `/batch` بدل حلقة تستدعي `/resolve` لكل رابط.
- مهلة استخراج 30 ث (`504`) وسقف طوابير (`503` مع `Retry-After`) بدل تعليق الطلبات.
- التنزيل يبدأ بثا فور أول بايت (أجزاء 256KB بلا تخزين مؤقت).
- لا تستدعِ `resolve` قبل `download` إن كنت ستنزّل فقط: `download` يستخرج بنفسه ويستفيد من الكاش.

## الربط

### متصفح (بدون أي كود خادم)
```html
<a href="https://YOUR-APP.vercel.app/api/v1/download?url=https%3A%2F%2Fvimeo.com%2F76979871">تنزيل</a>
```
CORS مفتوح (`*`) فيعمل `fetch` من أي موقع. للتقييد: متغير `CORS_ORIGIN=https://mysite.com`.

### JavaScript (مع التعامل مع 429)
```js
const API = "https://YOUR-APP.vercel.app/api/v1";
async function call(path, init, tries = 3) {
  for (let i = 0; i < tries; i++) {
    const r = await fetch(API + path, init);
    if (r.status !== 429 && r.status !== 503) return r.json();
    const wait = Number(r.headers.get("Retry-After") || 2);
    await new Promise(res => setTimeout(res, wait * 1000));
  }
  throw new Error("busy");
}
const one = await call("/resolve?url=" + encodeURIComponent(url));
const many = await call("/batch", { method: "POST", headers: {"Content-Type": "application/json"},
                                    body: JSON.stringify({ urls }) });
```

### JavaScript: قراءة NDJSON المتدفق
```js
const r = await fetch(API + "/batch?stream=1", { method: "POST",
  headers: {"Content-Type": "application/json"}, body: JSON.stringify({ urls }) });
const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
let buf = "";
for (;;) {
  const { value, done } = await reader.read(); if (done) break;
  buf += value; let i;
  while ((i = buf.indexOf("\n")) >= 0) { console.log(JSON.parse(buf.slice(0, i))); buf = buf.slice(i + 1); }
}
```

### Python
```python
import requests
API = "https://YOUR-APP.vercel.app/api/v1"
r = requests.post(f"{API}/batch", json={"urls": urls}, timeout=60)
for item in r.json()["results"]:
    if item["ok"]:
        with requests.get(item["download"], stream=True, timeout=(10, 60)) as d, open("out.bin", "wb") as f:
            for chunk in d.iter_content(256 * 1024):
                f.write(chunk)
```

### بوت / سكربت (Node)
```js
const res = await fetch(`${API}/resolve?url=${encodeURIComponent(url)}`);
const { ok, download, title } = await res.json();
if (ok) sendToUser(download, title);   // أرسل الرابط للمستخدم أو نزّله
```

## المتغيرات (اختيارية، من لوحة Vercel ← Environment Variables)
| المتغير | الافتراضي | الوصف |
|---|---|---|
| `RATE_LIMIT_PER_MIN` | 60 | حد resolve/batch لكل IP (0 = معطّل) |
| `DL_RATE_LIMIT_PER_MIN` | 20 | حد بدايات التنزيل لكل IP |
| `BATCH_MAX` | 10 | أقصى روابط في الدفعة |
| `EXTRACT_TIMEOUT_SEC` | 30 | مهلة الاستخراج |
| `MAX_PENDING_EXTRACTIONS` | 24 | سقف الاستخراجات المنتظرة قبل 503 |
| `CACHE_TTL_SEC` | 120 | مدة كاش النجاح |
| `CORS_ORIGIN` | `*` | الأصل المسموح |
| `REDIRECT` | 0 | `1` = download يحوّل 302 افتراضيا |
| `ALLOW_GENERIC` | 0 | `1` = السماح بأي رابط ملف (وسيط عام، غير موصى به) |
| `PROXY` | — | بروكسي لطلبات الاستخراج والتنزيل |
