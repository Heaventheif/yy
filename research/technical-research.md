# بحث تقني: استخراج الوسائط من الشبكات الاجتماعية على Vercel

**تاريخ البحث:** 18 أكتوبر 2026. المقصد: تفسير 402/403/443 واختيار أسلوب إعادة كتابة لا يوعد بتنزيل كل منشور.

## الخلاصة

المشكلة ليست مجرد اختيار إطار الواجهة. أخطاء 402/403 تصدر عادةً من الموقع أو CDN الذي يستضيف الوسائط، و443 رقم منفذ HTTPS غالبًا يظهر في خطأ اتصال/TLS لا كرمز HTTP. أي extractor يطلب صفحات أو CDN مباشرةً سيظل حساسًا لقيود المصدر، تغيّر HTML، صلاحية الروابط الموقعة، ومكان خروج Vercel. لا يوجد في وثائق المنصات العامة التي فُحصت API موحدًا يعطي تنزيلًا مباشرًا لمقاطع أي مستخدم.

لذلك يعيد المشروع استخدام مستخرجات yt-dlp المحدثة على روابط عامة مدرجة، مع طلبات متزامنة محدودة وكاش قصير وبث `Range`، ويعرض رفض المصدر دون محاولة الالتفاف عليه. رابط CDN الخام قصير العمر ولا يُسرّب في استجابة JSON العامة.

## نتائج المصادر الرسمية والأولية

### Meta: Facebook وInstagram وThreads

إعلان Meta في 15 يونيو 2026 يسمح باستدعاء oEmbed للمحتوى العام بلا access token. لكنه يصف الاستخدام بوضوح كتضمين المحتوى، والاستجابة التقنية تتضمن `html` وبيانات الـprovider والعرض. لا تعيد الاستجابة رابط ملف الفيديو الأصلي القابل للتنزيل. صفحة Meta القديمة لميزة oEmbed Read توضح أن الغرض هو توفير العرض الأمامي؛ تُقرأ مع إعلان التحديث الأحدث الذي حل محل آلية الوصول السابقة.

المحصلة: oEmbed خيار مشروع للمعاينة/التضمين، وليس بديلًا لمستخرج تنزيل Facebook أو Instagram. الوصول إلى منشور عام من خادم Vercel قد يُرفض، ولا ينبغي إخفاء 402/403 بإعادة المحاولات أو استخدام بيانات دخول غير مصرح بها.

### TikTok

وثائق TikTok Embed Videos تقول إن oEmbed يحول رابط الفيديو إلى markup ومعلومات إضافية ويعيد HTML من لاعب TikTok. لا تشير إلى رابط ملف تنزيل مباشر. يظل مستخرج yt-dlp هو مسار best-effort لروابط عامة، وليس ضمانًا رسميًا.

### X

قواعد X المحدثة أبريل 2026 تمنع الأتمتة غير المعتمدة على API، ومن أمثلتها برمجة موقع X، وتحذر من تجاوز حدود API. مستخرج yt-dlp يتعامل مع الموقع نفسه؛ لذلك أُزيل دعم X من قائمة النطاقات بدل تطبيقه دون API رسمي معتمد.

### yt-dlp

قائمة `supportedsites.md` الرسمية تتضمن مستخرجات Facebook وInstagram وTikTok وReddit وPinterest وLinkedIn وSnapchat Spotlight وVimeo وTwitch وDailymotion وSoundCloud وFlickr وStreamable وغيرها. هذه قائمة مستخرجات لا SLA، وتظهر فيها بعض العناصر بوضوح بوصفها `Currently broken`. سجل الإصدار 2026.08.19 يتضمن إصلاحات لمستخرج Instagram وTikTok بما فيها دعم روابط مشاركة TikTok؛ لذا حدّث المشروع متطلب yt-dlp إلى إصدار 2026.08.19 أو أحدث ضمن سنة 2026.

### Vercel

وثائق Python Runtime تذكر أن Vercel يشغّل WSGI/ASGI، ويتعرف على `app.py` واسم handler مثل `app`، وأن Python 3.12 هو الإصدار الافتراضي الحالي. وثائق Streaming تقول إن دوال Python تدعم بث الاستجابة. جدول حدود Functions يذكر لخطة Hobby عند تفعيل Fluid Compute أقصى مدة 300 ثانية، وحدًا لحجم حمولة الطلب/الاستجابة 4.5 MB. لذلك يبث التطبيق بدلاً من تخزين الملف كاملًا، لكن يجب اختبار استجابة ملفات الإنتاج لأن البث لا يلغي سقف المدة أو حدود المنصة؛ التنزيلات الكبيرة قد تحتاج إلى CDN/تخزين/خادم نقل متخصص.

## المهارات (`/find-skills`)

راجعت لوحة Skills.sh ثم بحثت عبر `npx skills find 'social media downloader API'`. لم يظهر skill تنزيل وسائط مناسب؛ النتيجة الوحيدة ذات الصلة اللفظية كانت `inference-sh/skills@twitter-automation` (14 تثبيتًا)، وهي لأتمتة النشر على X وليست تنزيل فيديو أو معالجة أخطاء Vercel، كما أن X policy تمنع الأتمتة خارج API. لذلك لم أضف أو أثبت مهارة غير مناسبة للمستودع.

## المصادر

1. Meta Developers, [Introducing Tokenless Access to Meta oEmbed APIs](https://developers.facebook.com/blog/post/2026/06/15/tokenless-access-to-meta-oembed-apis/) (15 يونيو 2026).
2. Meta Developers, [oEmbed Read](https://developers.facebook.com/docs/features-reference/oembed-read/) و[Meta IG Media](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-media).
3. TikTok for Developers, [Embed Videos](https://developers.tiktok.com/doc/embed-videos) (آخر تحديث 4 أغسطس 2026).
4. X Help Center, [Automation rules](https://help.x.com/en/rules-and-policies/x-automation) (تحديث أبريل 2026).
5. yt-dlp, [Supported sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) و[release 2026.08.19](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19).
6. Vercel, [Python Runtime](https://vercel.com/docs/functions/runtimes/python), [Streaming Functions](https://vercel.com/docs/functions/streaming-functions), [Function limitations](https://vercel.com/docs/functions/limitations).
7. Reddit API docs, [API reference](https://www.reddit.com/dev/api/).
8. Skills.sh, [Twitter automation skill](https://skills.sh/inference-sh/skills/twitter-automation), 14 installs at lookup.


## نتيجة البحث المقارن للمنصات الإضافية

أثبت البحث المستقل في وثائق Pinterest وSnapchat وLinkedIn وVimeo وTwitch أن وجود API لا يعني تنزيلًا عامًا. Pinterest لا يوثق endpoint لإرجاع فيديو MP4؛ Snapchat Public Profile API قد يعيد `media_url` لأصول مؤهلة لكنه يتطلب حساب أعمال وOAuth وإدراجًا في قائمة السماح؛ LinkedIn يعيد `downloadUrl` لفيديو URN مخول وصلاحيات اعتماد؛ Vimeo يتطلب OAuth ونطاق `video_files` وفيديو يخص عضوًا مخولًا؛ Twitch يتيح API لتنزيل Clips فقط لمحرر القناة المخول، أما VOD فينزله صاحب القناة عبر Video Producer. لذلك ترفض النسخة الحالية هذه المنصات إلى أن تُبنى تكاملات OAuth رسمية، بدل كشط صفحاتها. التقرير التفصيلي: [platforms-synthesis.md](platforms-synthesis.md)، وتقارير المنصة: [Pinterest](01-pinterest.md)، [Snapchat](02-snapchat.md)، [LinkedIn](03-linkedin.md)، [Vimeo](04-vimeo.md)، [Twitch](05-twitch.md).
