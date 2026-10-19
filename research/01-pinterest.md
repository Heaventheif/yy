# Pinterest: API وتنزيل ملفات الفيديو

## النتيجة

**لا توثّق Pinterest في واجهة API العامة آليةً لجلب ملف الفيديو الخام أو رابط تنزيله، سواء من Pins المستخدم نفسه أو من Pins عامة.** الواجهة توثق قراءة Pins في نطاقات محددة، لكنها لا تعرض في مرجع `GET /pins/{pin_id}` حقلًا أو endpoint مخصصًا لتنزيل ملف الفيديو. عينة الاستجابة المنشورة تعرض `media` من نوع صورة وروابط نسخ صور؛ لا تثبت وجود رابط فيديو قابل للتنزيل. لذلك لا ينبغي تفسير إتاحة قراءة بيانات Pin على أنها إذن أو واجهة لتنزيل الفيديو.[1][2]

أما **التنزيل من واجهة Pinterest نفسها** فموثق بشروط مختلفة: يمكن تنزيل فيديو Pin عمودي بملء الشاشة (9:16) إذا فعّل صاحب المحتوى خيار التنزيل؛ ويظهر اسم المستخدم كعلامة مائية. إعداد السماح بالتنزيل يخص فيديوهات الشاشة الكاملة، ويمكن لصاحب الحساب تعطيله أو تفعيله.[3] هذا تنزيل من المنتج للمستخدم، وليس API أو تعهدًا بأن كل فيديو عام قابل للتنزيل. وتوضح صفحة التنزيل العامة أن بعض Pins لا تتاح لها حتى خاصية التنزيل؛ لكنها تخص تنزيل صور Pins، فلا ينبغي اعتبارها إثباتًا لقابلية تنزيل الفيديو.[4]

## ما يتيحه API وما لا يتيحه

- `GET /pins` يسرد Pins التي يملكها حساب المستخدم الذي يجري العملية، ويحتاج `boards:read` و`pins:read`. ويوضح مرجع `GET /pins/{pin_id}` أنه يجلب Pin مملوكًا لحساب العملية أو موجودًا على لوحة جماعية مشارَكة معه؛ كما يذكر صلاحيات Business Access المطلوبة عند استخدام `ad_account_id`.[1][2]
- إرشادات الصلاحيات تقول إن `pins:read` يتيح الاطلاع على Pins العامة، بينما يتطلب الوصول إلى Pins السرية `pins:read_secret`. لكن هذا لا يحول API إلى أداة تنزيل: نطاق الصلاحية يحدد الوصول إلى بيانات Pins، ولا يظهر في مرجع GET رابط فيديو خام أو إجراء تنزيل. كما أن توثيق endpoint المفرد يقيد جلبه بملكية الحساب أو مشاركة اللوحة.[2]
- ما توثقه Pinterest بوضوح للفيديو عبر API هو **اتجاه الرفع/النشر**: يسجل التطبيق رفعًا باستخدام `POST /media`، يرفع ملفه هو إلى عنوان الرفع المؤقت، ثم ينشئ Pin مستخدمًا `media_id` و`cover_image_url`. لا توثق هذه الخطوات استخراج نسخة فيديو موجودة على Pinterest.[5][6]
- **التضمين ليس تنزيلًا.** توثق Pinterest إضافة Pin إلى موقع باستخدام `pinit.js` و`data-pin-do="embedPin"` مع رابط الـPin، وتصف ذلك بأنه عرض Pin على الموقع. لا تسلم هذه الطريقة ملف MP4 ولا تقدم رابط تنزيل. لم أعثر في الوثائق الرسمية التي راجعتها على endpoint مستقل لـoEmbed يتيح تنزيل الملف؛ وحتى لو عُرض Pin بمكوّن تضمين، فهذا عرض للمحتوى وليس تسليمًا للملف الخام.[7]

## الوصول والقيود العملية

يلزم إنشاء تطبيق مطوّر وتقديم طلب **Trial access** ومراجعته. ويتطلب الربط تدفق OAuth والحصول على موافقة المستخدم للـscopes المناسبة؛ نطاقا `boards:read` و`pins:read` هما المطلوبان في مرجع جلب Pin. الوثائق تفرق بين Trial وStandard: القراءة مدعومة في كليهما، لكن Pins/Boards المنشأة في Trial لا تظهر إلا لمنشئها، وStandard يتطلب طلب ترقية ومراجعة مع عرض توضيحي للتطبيق وتدفق OAuth.[2][8][9]

لا تذكر صفحات الوصول المذكورة رسمًا مدفوعًا لاستخدام API؛ لذا لا أستنتج منها وجود اشتراك مدفوع أو غيابه. تفرض Pinterest حدود طلبات: حد عام لـTrial مقداره 1,000 طلب يوميًا لكل التطبيق، وStandard له حد عام 100 طلب في الثانية لكل مستخدم ولكل تطبيق، إضافةً إلى حدود حسب فئة endpoint. فئة `org_read` التي ينتمي إليها جلب Pins محددة في الجدول بـ1,000 طلب يوميًا لكل تطبيق في Trial و1,000 طلب في الدقيقة لكل مستخدم/تطبيق في Standard. Pinterest تنبه إلى أن الحدود قد تتغير.[9]

وثائق Pinterest الحالية تنبه أيضًا إلى أن التطبيقات المسجلة ابتداءً من 14 سبتمبر 2026 قد تتلقى `403 PINNER_DATA_ACCESS_DENIED` عند طلب لوحة أو Pin دون تفويض Pinterest لغير مستخدمي الأعمال. وهذا يعزز أن ظهور المحتوى للعامة لا يعني تلقائيًا أن كل تطبيق يستطيع قراءته عبر API.[2][8]

## الحقوق والاستخدام

شروط Pinterest تنص على أن ناشر المحتوى يحتفظ بحقوقه في المحتوى المنشور، وأنه مسؤول عن امتلاك حقوق نشره. وتمنح الشروط Pinterest ومستخدميها ترخيصًا لاستخدام محتوى المستخدم ضمن ما تنص عليه الشروط؛ لكن ذلك لا يلغي حقوق أصحاب المحتوى الآخرين ولا يساوي تصريحًا عامًا بإعادة نشر ملفاتهم أو جمعها آليًا.[10]

وتحظر شروط المطورين إنشاء تطبيق غرضه الأساسي جمع أو نقل أو مشاركة أو بيع محتوى المستخدمين الآخرين دون موافقة Pinterest الخطية المسبقة، كما تحظر الوصول الآلي غير المصرح به أو الوصول إلى بيانات بلا إذن. وتطلب إرشادات المطورين، عند نشر Pins داخل تطبيق، ربطها بمصدرها في Pinterest وإظهار مصدرها، وعدم حجب المحتوى أو إنشاء محتوى جديد من Pins لإعادة توزيعه.[11][12] لذلك ينبغي استخدام التنزيل فقط عبر خيار Pinterest الرسمي حين يكون متاحًا، أو استخدام ملف الفيديو الأصلي الذي يملكه المستخدم/إذن صريح من صاحب الحقوق؛ لا scraping أو استخراج روابط مخفية.

## المراجع

[1]: https://developers.pinterest.com/docs/api/v5/pins-list/ "Pinterest API v5 List Pins"
[2]: https://developers.pinterest.com/docs/api/v5/pins-get/ "Pinterest API v5 Get Pin"
[3]: https://help.pinterest.com/en/article/create-a-pin-from-an-image-or-video "Pinterest Help: Create a Pin from an image or video"
[4]: https://help.pinterest.com/en/article/download-an-image "Pinterest Help: Download an image"
[5]: https://developers.pinterest.com/docs/api/v5/media-create/ "Pinterest API v5 Register media upload"
[6]: https://developers.pinterest.com/docs/api-features/creating-boards-and-pins/ "Pinterest Developers: Create boards and Pins"
[7]: https://developers.pinterest.com/docs/web-features/widgets/ "Pinterest Developers: Widgets"
[8]: https://developers.pinterest.com/docs/getting-started/set-up-authentication-and-authorization/ "Pinterest Developers: Set up authentication and authorization"
[9]: https://developers.pinterest.com/docs/reference/rate-limits/ "Pinterest API rate limits"
[10]: https://policy.pinterest.com/en/terms-of-service "Pinterest Terms of Service"
[11]: https://developers.pinterest.com/terms/ "Pinterest Developer and API Terms of Service"
[12]: https://policy.pinterest.com/developer-guidelines "Pinterest Developer guidelines"
