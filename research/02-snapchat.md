# Snapchat: واجهات تنزيل الفيديو والتضمين والقيود

**النتيجة:** نعم، توثّق Snap واجهة Marketing API باسم **Public Profile API** تتيح في بيانات أصول بعض ملفات Public Profiles العامة رابط `media_url` موصوفًا بأنه رابط وسائط قابل للتنزيل. هذا ليس API عامًا لكل مقاطع Snapchat أو لكل حسابات المستخدمين: الوصول للواجهة يتطلب تطبيق OAuth تجاريًا وموافقة إدراجه في قائمة السماح لدى Snap، ويقتصر على أنواع وأصول Public Profile التي تدعمها الواجهة. أما محتوى صاحب الحساب نفسه، فالمسار الرسمي الواضح هو التصدير من Memories أو أداة **My Data** بعد تسجيل الدخول، لا واجهة تصدير آلية عامة موثقة في المصادر التي راجعتها. [1] [2] [3] [4]

## تنزيل المحتوى العام عبر API

تصف Snap **Public Profile API** بأنها واجهة لاكتشاف الملفات العامة وإدارة محتوى العلامات التجارية/المنشئين، وتعرض وثائق Creator Discovery نقاطًا عامة تسترجع بيانات أساسية وإحصاءات عن أي Public Profile دون تفويض من صاحب الملف، مع كون البيانات جزئية. وتدرج الوثائق أصول Spotlight وSaved Story ضمن الأصول المتاحة عبر المسارات العامة `/public/v1/...`. [1] [2]

في توثيق إدارة الأصول، يحتوي كائن Spotlight على `media_url`، وتضع الوثيقة علامة «عام» على هذا الحقل. كما تصف `media_url` لوسائط Snaps في القصص المحفوظة/القصص بأنه «Unencrypted media downloadable url»، وتعرض مثالًا لنوع فيديو ورابط وسائط مباشر. لذلك يوجد دعم موثق لرابط تنزيل فعلي لبعض فيديوهات Spotlight وSnaps في نطاق هذه الواجهة، وليس مجرد رابط صفحة مشاهدة. لكن يجب عدم تعميم هذا على كل فيديو عام أو خاص على Snapchat: واجهة Public Profile لا توثق مستودعًا شاملًا لكل منشورات المستخدمين، وبعض أنواع البيانات أو التفاصيل لا تظهر إلا ضمن نقاط النهاية المصرح بها. [2] [4]

تذكر صفحة البدء أن Public Profile API **مقيدة بقائمة سماح (allowlist)**. يلزم إنشاء حساب أعمال لدى Snap وتطبيق OAuth في لوحة الأعمال، ثم إرسال Client ID إلى جهة اتصال Snap لطلب إدراج التطبيق، واستخدام Access Token في كل طلب. لا تتطلب النقاط العامة تفويض صاحب الملف الفردي، لكن النقاط المصرح بها تتطلب أن يوافق المنشئ عبر Creator OAuth أو يفعّل مشاركة البيانات المناسبة. وتنص الوثائق على أن التوكن قصير العمر (مثالها ساعة) ويُجدّد عبر OAuth. لا تعرض صفحات API المستشهد بها سعر اشتراك محددًا؛ لذا لا أستنتج منها أن الوصول متاح بلا موافقة أو أن له رسومًا بعينها. [2] [3]

**تمييز مسارات API:** توثيق Creator Discovery يسرد المسار العام بصيغة `/public/v1/public_profiles/{profile_id}/spotlights`، بينما صفحة إدارة الأصول تعرض تفاصيل نقطة النهاية بصيغة `/v1/public_profiles/{profile_id}/spotlights` وتشرح حقول الأصول. ينبغي استخدام نقطة النهاية العامة المعتمدة في إعداد الحساب، والرجوع إلى Snap للتحقق من إتاحة حقل `media_url` بعينه في صلاحيات التطبيق والردود الفعلية؛ لا تستخدم مسارًا غير موثق في حسابك أو تتجاوز رفض صلاحية. [2] [3] [4]

## المحتوى الذي يملكه صاحب الحساب

يمكن لصاحب الحساب تنزيل Snap من Memories داخل التطبيق: فتح Memories، الضغط المطوّل على Snap، اختيار **Export** ثم وجهة التنزيل. وتسمح أداة **My Data** الرسمية بطلب تصدير Memories منفردة أو مع بيانات الحساب من `accounts.snapchat.com`، ثم تنزيل ملف ZIP وفكّه وتنزيل عناصر Memories من صفحة Memories في ملف التصدير. يتطلب المسار تسجيل الدخول للحساب؛ وهو تصدير يطلبه المستخدم، لا واجهة API عامة قابلة للاستدعاء آليًا. قد يستغرق الإعداد حتى سبعة أيام بحسب Snap، وقد تتأخر الطلبات الأكبر، كما أن التصدير لا يضمن بيانات لم تعد محفوظة أو متاحة لدى Snap. [5] [6]

## التضمين ليس تنزيلًا

تتيح Snap تضمين Lenses وSpotlight videos وPublic Stories وPublic Profiles في صفحات الويب. وصف Snap للتضمين صريح: إنه يعرض المحتوى الخارجي داخل الصفحة **من دون استضافته محليًا**؛ ويُنسخ رمز تضمين مع `embed.js` من Snapchat. هذا حل للعرض/المشاهدة داخل صفحة، وليس رابط ملف فيديو خام ولا طريقة تنزيل. توثيق Social Plugins الذي راجعته يوثق web embeds؛ لا يصف واجهة oEmbed عامة للتنزيل. [7] [8]

## الحقوق والاستخدام

تقول شروط الخدمة إن المستخدم يحتفظ بحقوق الملكية التي كانت له أصلًا في المحتوى، لكنه يمنح Snap ترخيصًا واسعًا لتشغيل الخدمات وتطويرها والترويج لها. وبالنسبة إلى Public Content، تمنح الشروط أيضًا Snap ومستخدمي الخدمات وشركاء الأعمال حقوقًا واسعة ودائمة لاستخدامه ضمن الأغراض المبينة في الشروط. هذا لا يساوي إذنًا آليًا لأي مطوّر لتنزيل ملف شخص آخر أو إعادة نشره: احصل على الإذن/الحقوق اللازمة، واتبع شروط Snap وأي حقوق نشر أو موسيقى أو حقوق أشخاص ظاهرة في الفيديو. [9]

## القرار العملي

- **فيديوهات Public Profile العامة المؤهلة:** يمكن بحث استخدام Public Profile API الرسمي للحصول على بيانات الأصول ورابط `media_url` حيث تعيده الواجهة، بعد استيفاء حساب الأعمال وOAuth وإدراج التطبيق في قائمة السماح. قيّد الجمع بالملفات العامة والأصول التي تسمح بها الوثائق والصلاحيات.
- **محتوى الحساب نفسه:** استخدم Export داخل Memories أو My Data. لا تفترض وجود API رسمي عام لتصدير مكتبة الحساب آليًا.
- **المشاهدة داخل موقع:** استخدم رمز التضمين الرسمي. لا تعامل كود التضمين أو عنوان صفحة Spotlight على أنه تنزيل ملف خام.
- لا تحاول تجاوز تسجيل الدخول أو CAPTCHA أو قائمة السماح أو حدود الوصول، ولا تحاول استخراج ملف من واجهة غير موثقة.

## المراجع

[1]: https://developers.snap.com/marketing-api/Public-Profile-API/Introduction "Public Profile API - Snap for Developers"
[2]: https://developers.snap.com/marketing-api/Public-Profile-API/CreatorDiscovery "Creator Discovery - Snap for Developers"
[3]: https://developers.snap.com/marketing-api/Public-Profile-API/GetStarted "Get Started - Snap for Developers"
[4]: https://developers.snap.com/marketing-api/Public-Profile-API/ProfileAssetManagement "Profile Asset Management - Snap for Developers"
[5]: https://help.snapchat.com/hc/en-us/articles/7047492542484-How-do-I-download-a-Snap-from-Memories "How do I download a Snap from Memories? - Snapchat Support"
[6]: https://help.snapchat.com/hc/en-us/articles/7012305371156-How-do-I-download-my-data-from-Snapchat "How do I download my data from Snapchat? - Snapchat Support"
[7]: https://developers.snap.com/api/snapchat-for-web/social-plugins/introduction "Social Plugins - Snap for Developers"
[8]: https://developers.snap.com/api/snapchat-for-web/social-plugins/embedding-web-content "How to Embed Snapchat for Web Content - Snap for Developers"
[9]: https://www.snap.com/terms "Terms of Service - Snap Inc."
