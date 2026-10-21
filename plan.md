# خطة إعادة كتابة yy

## الهدف

تحويل المستودع إلى خدمة تنزيل عربية على Vercel، ما عدا YouTube، مع تفسير واضح لرفض المصدر 402/403 ومشكلات HTTPS/443، وكفاءة أفضل في إعادة الاستخراج. لن تتجاوز الخدمة تسجيل الدخول أو CAPTCHA أو OAuth أو قيود التنزيل، ولن تستخدم cookies أو بروكسيًا لإخفاء الرفض.

## هندسة التنفيذ

تبقى نقطة دخول Flask في `app.py` وواجهة HTML في `public/index.html`. تقسم الوظائف إلى `socialdl/platforms.py` (قائمة النطاقات/حظر YouTube/تحقق DNS)، `socialdl/errors.py` (تصنيف أخطاء المصدر)، `socialdl/extractor.py` (yt-dlp دون تنزيل مرحلي أو cookies)، و`socialdl/service.py` (كاش قصير وsingle-flight وسقف تزامن). تحافظ المسارات على `/api/v1/resolve`, `/batch`, `/download`, `/health`؛ وتمرر ملفات التنزيل بالبث مع دعم Range، من دون كشف روابط CDN المؤقتة في JSON.

روابط أفضل جهد للاستخراج العام في النسخة الحالية: Facebook، Instagram، TikTok، Reddit، Dailymotion، SoundCloud، Flickr، Streamable. تنجح فقط إذا أتاح الموقع ملفًا مباشرًا عامًا ولم يمنع خروج Vercel. YouTube وX محظوران. وتُرفض Pinterest وSnapchat وLinkedIn وVimeo وTwitch حتى يتوفر تكامل API/OAuth رسمي مخوّل، لأن البحث الرسمي لا يثبت تنزيلًا عامًا حرًا؛ Threads غير مدرج لعدم وجود endpoint تنزيل مناسب. اطلع على `research/platforms-synthesis.md` و`research/technical-research.md`.

## التصميم

الأنماط البديلة: لوحة أدوات تحريرية هادئة (احتمال 0.06)، واجهة تقنية أحادية اللون (احتمال 0.04)، وبساطة تحريرية عالية التباين (احتمال 0.08). الاختيار: **بساطة تحريرية عالية التباين**.

الحركة التصميمية: واجهة أدوات ويب عربية عملية، لا صفحة تسويقية. الفعل الرئيسي هو لصق رابط ثم عرض حالة يمكن فهمها؛ خلفية دافئة فاتحة وحبر كحلي مع لمسة أخضر ليموني؛ تخطيط رأسي واسع يضع حقل الرابط في مركز الصفحة؛ حالات نجاح ورفض بارزة؛ لا صور أو خطوط خارجية؛ حركة انتقال قصيرة وتحترم `prefers-reduced-motion`. النبرة موجزة ولا تعد بتنزيل كل منشور.

## الأمان والحدود

قبول نطاقات محددة فقط، رفض IP خاص وبيانات اعتماد URL والمنافذ غير المعتادة، إعادة فحص كل وجهة تحويل، ومنع الروابط المختصرة من الانتقال إلى نطاق غير مدرج. لا تُسجل query ولا headers أو روابط CDN. الكاش ومحدد المعدل والـsingle-flight داخل الذاكرة لكل Function instance؛ ليست آليات عالمية. Vercel يدعم بث Python، لكن مهلة التنفيذ وحد الحمولة قد يؤثران على الملفات الكبيرة. لا توجد API رسمية موحدة تتيح تنزيل كل محتوى الشبكات الاجتماعية؛ oEmbed يعني عرض المحتوى لا تنزيله.

## مصادر أساسية

Meta oEmbed: https://developers.facebook.com/blog/post/2026/06/15/tokenless-access-to-meta-oembed-apis/
TikTok Embed: https://developers.tiktok.com/doc/embed-videos
X automation: https://help.x.com/en/rules-and-policies/x-automation
yt-dlp supported sites: https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
Vercel Python / streaming / limits: https://vercel.com/docs/functions/runtimes/python · https://vercel.com/docs/functions/streaming-functions · https://vercel.com/docs/functions/limitations

## تحقق

معايير القبول: YouTube يرفض صراحة؛ مسارات API تفسر 402 و403 و443؛ الواجهة عربية RTL؛ لا يُعاد CDN URL في JSON؛ التوسعة لا تتبع وجهات داخلية؛ اختبارات الوحدات تغطي المنصات والأخطاء والدفعات وHEAD/Range؛ وثائق API والنشر تصف الحدود.

## ملحق — تعديلات ما بعد البناء

- **سقف جودة الفيديو**: `MAX_VIDEO_HEIGHT` (افتراضي 720، `0` يعطّل الفلترة). تُطبَّق في `youtube._merge` قبل `MAX_FORMATS`. الصوت يظهر دائمًا؛ تسميات بلا ارتفاع (مثل توشيرو) تُقبل فقط إن لم توجد صيغة progressive معروفة الارتفاع.
- **اقتصاص الصوت**: أُلغي بقرار المالك؛ الصوت يُنزَّل كاملًا عبر `/api/v1/audio` (تحويل ffmpeg إلى MP3/M4A).
- **تحويل 307 السريع مؤجَّل**: `connect-src 'self'` في `vercel.json` يحجب تحويل `fetch` إلى `api.vidsnap.app`.
