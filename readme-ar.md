# CompanyDocuments v2: مولّد مستندات أعمال اصطناعية

[English](README.md) | العربية

يولّد مستندات أعمال واقعية بصيغة PDF، مثل الفواتير وأوامر الشراء وكشوف الرواتب وكشوف الحساب وغيرها،
اعتمادًا على أربع قواعد بيانات نموذجية مفتوحة. يتضمن كل مستند بيانات مرجعية دقيقة ومربعات إحاطة بالكلمات.
تشكّل المخرجات بيانات التدريب لمجموعة **CompanyDocuments v2**، التي تأتي بعد
[مجموعة بيانات CompanyDocuments](https://huggingface.co/datasets/AyoubChLin/CompanyDocuments)، التي تضم 2,677 ملف PDF وأربعة أنواع من المستندات.

```text
SQLite databases ──► normalized records ──► document record ──► HTML (Jinja2) ──► PDF (WeasyPrint)
                                                                                     │
         dataset (CSV) ◄──── export ◄── scans (noise stage) ◄── self-check + gold JSON + word boxes
```

- **13 نوعًا من المستندات** من Northwind وAdventureWorks وChinook وSakila، مع 353 ألف سجل مصدر متاح.
- **60 شركة مُصدرة اصطناعية** في أربعة قطاعات، لكل منها ترويسة ثابتة (4 تخطيطات × 5 سمات بصرية)،
  وترقيم خاص، ونصوص يكتبها نموذج لغوي مجاني.
- **الإنجليزية والفرنسية**، مع تنسيقات الأرقام والتواريخ والعملات والضرائب واستقطاعات الرواتب المناسبة لكل إعداد محلي.
- **بيانات مرجعية دقيقة:** تُحسب القيم المالية باستخدام `Decimal`. يُقرأ كل مستند مجددًا بعد التصيير،
  ويُستبعد إذا غابت قيمة مرجعية عن نص PDF أو لم تصح الحسابات.
- **قابلية إعادة الإنتاج:** تنتج الإعدادات والشركات والبذرة نفسها ملفات PDF متطابقة على مستوى البايتات.

ملاحظات التصميم: [docs/CompanyDocuments_v2_Generator_Design.md](docs/CompanyDocuments_v2_Generator_Design.md).
دليل المطور، بما يشمل المعمارية وإضافة أنواع المستندات والمصادر والتخطيطات: [docgen/README.md](docgen/README.md).

```text
SQLite databases ──► normalized records ──► document record ──► HTML (Jinja2) ──► PDF (WeasyPrint)
                                                                                     │
         dataset (CSV) ◄──── export ◄── scans (noise stage) ◄── self-check + gold JSON + word boxes
```

- **13 نوعًا من المستندات** من Northwind وAdventureWorks وChinook وSakila، مع 353 ألف سجل مصدر متاح.
- **60 شركة مُصدرة اصطناعية** في أربعة قطاعات، لكل منها ترويسة ثابتة (4 تخطيطات × 5 سمات بصرية)، وترقيم خاص، ونصوص يكتبها نموذج لغوي مجاني.
- **الإنجليزية والفرنسية**، مع تنسيقات الأرقام والتواريخ والعملات والضرائب واستقطاعات الرواتب المناسبة لكل إعداد محلي.
- **بيانات مرجعية دقيقة:** تُحسب القيم المالية باستخدام `Decimal`. يُقرأ كل مستند مجددًا بعد التصيير، ويُستبعد إذا غابت قيمة مرجعية عن نص PDF أو لم تصح الحسابات.
- **قابلية إعادة الإنتاج:** تنتج الإعدادات والشركات والبذرة نفسها ملفات PDF متطابقة على مستوى البايتات.

ملاحظات التصميم: [docs/CompanyDocuments_v2_Generator_Design.md](docs/CompanyDocuments_v2_Generator_Design.md).
دليل المطور للبنية وإضافة أنواع المستندات والمصادر والتخطيطات: [docgen/README.md](docgen/README.md).

## الحالة

| العنصر | الحالة |
|---|---|
| موائمات المصادر، و13 نوعًا من المستندات، والقوالب، و4 تخطيطات، و5 سمات بصرية، والإنجليزية والفرنسية | مكتمل؛ جميع المتغيرات تُصيَّر وتجتاز الفحص الذاتي |
| 60 شركة، بواقع 15 شركة لكل قطاع، مع قوالب خاصة بكل شركة | مكتمل |
| نصوص النماذج اللغوية الكبيرة عبر OpenRouter باستخدام نماذج مجانية | اكتملت نصوص 44 من أصل 60 شركة، و20 من أصل 77 وصفًا للمنتجات؛ تُكمل مرحلة `texts` الباقي |
| مخطّط التوليد: أعداد متوازنة، دون تكرار، ودعم `count: all` | مكتمل |
| مرحلة إضافة التشويش (`augment`) والتصدير (`export`: مجلدات أو أجزاء Parquet) | مكتمل ومختبَر من البداية إلى النهاية؛ جرى التحقق من مربعات الإحاطة بعد تدوير الصور وتغيير حجمها، ويكتب التصدير الأجزاء تدريجيًا |
| تشغيل خط المعالجة بأمر واحد (`scripts/run_pipeline.py`) | مكتمل؛ اختُبرت مرحلة التنزيل مع المصادر الأربعة |
| توليد مجموعة البيانات الكاملة بكل السجلات (`scripts/run_in_parts.py`) | **قيد التشغيل** |
| العربية والكتابة من اليمين إلى اليسار (RTL) | مؤجّلة: استخراج النص من PDF يعيد ترتيب النص المختلط بين العربية واللاتينية، لذا لا يزال الفحص الذاتي غير موثوق |

## أمر واحد

```bash
python3 scripts/run_pipeline.py                   # تنزيل ← فهرسة ← شركات ← نصوص ← توليد ← تشويش ← تصدير
```

لا يحتاج [scripts/run_pipeline.py](scripts/run_pipeline.py) إلا إلى مكتبة Python القياسية. ينشئ البيئة
الافتراضية `./venv` أو يصلحها، ثم يشغّل جميع المراحل. يمكن إعادة تشغيل كل مرحلة بأمان، ويستأنف التوليد من
الموضع الذي توقّف عنده التشغيل السابق.

| المرحلة | الوظيفة |
|---|---|
| `venv` | إنشاء `./venv` أو إصلاحها وتثبيت `requirements.txt`، بما في ذلك بعد ترقية Python الخاصة بنظام التشغيل |
| `download` | تنزيل قواعد SQLite الأربع وبناؤها مع تراخيصها عبر [scripts/download_sources.py](scripts/download_sources.py)، وتجاوز القواعد الموجودة |
| `index` | إضافة فهارس البحث |
| `companies` | إنشاء 15 شركة مُصدرة لكل قطاع إذا كانت غير موجودة، ثم توزيع جميع تركيبات التخطيط والسمات البصرية على كل قطاع |
| `texts` | إنشاء نصوص الشركات وأوصاف المنتجات باستخدام نموذج لغوي إذا ضُبط `OPENROUTER_API_KEY`؛ مرحلة اختيارية تملأ النواقص فقط |
| `generate` | توليد جميع أنواع المستندات، وإجراء الفحص الذاتي، وإنتاج JSON المرجعي ومربعات إحاطة الكلمات |
| `augment` | إنشاء صور صفحات تحاكي المسح الضوئي أو التصوير بجودة منخفضة لنسبة 30% من المستندات |
| `export` | تصدير مجموعة البيانات وبطاقتها إلى `dataset/v2`، مقسّمة حسب الشركة ومهيأة لعارض Hugging Face، مع مجموعة فرعية لكل نوع مستند؛ استخدم `--format parquet` للأجزاء بصيغة Parquet في التشغيلات الكبيرة |
| `test` | تشغيل اختبارات الوحدات عند استخدام `--with-tests` |

```bash
python3 scripts/run_pipeline.py --per-type 20         # فحص سريع لكامل المسار، نحو 260 مستندًا
python3 scripts/run_pipeline.py                       # مجموعة متوازنة: 2,000 لكل نوع، نحو 26 ألف مستند في ساعة
python3 scripts/run_pipeline.py --all-records         # كل سجل مصدر مرة واحدة، نحو 354 ألف مستند في 10 ساعات
python3 scripts/run_pipeline.py --everything          # جميع السجلات، مع اكتمال نصوص النماذج اللغوية والاختبارات
python3 scripts/run_pipeline.py --from generate       # الاستئناف من مرحلة محددة
python3 scripts/run_pipeline.py --stages export --out output/v2 --dataset dataset/v2
```

**مساحة القرص.** قبل التوليد، يقدّر خط المعالجة المساحة المطلوبة ويرفض البدء إذا لم تكن كافية.
يمكن تجاوز هذا الفحص باستخدام `--ignore-disk`:

| التشغيل | المستندات | التوليد | صور المسح (30%) | التصدير | الإجمالي |
|---|---|---|---|---|---|
| الافتراضي (`--per-type 2000`) | 26,000 | 0.8 GB | 3.7 GB | 0.6 GB | نحو 7 GB |
| `--everything` | 353,590 | 11.2 GB | 49.8 GB | 7.9 GB | نحو 71 GB |

لتشغيل `--everything` على قرص صغير، وجّه `--out` و`--dataset` إلى قرص أكبر، أو خفّض المساحة المطلوبة:

| الخيارات الإضافية مع `--everything` | الإجمالي |
|---|---|
| `--augment-fraction 0.05` | نحو 29 GB |
| `--augment-fraction 0` | نحو 21 GB |
| `--augment-fraction 0 --no-pdf`؛ تُحفظ ملفات PDF في `--out` فقط، ولا تُضمَّن في Parquet | نحو 15 GB |

**مجموعة البيانات الكاملة على قرص صغير.** يولّد [scripts/run_in_parts.py](scripts/run_in_parts.py) جميع
السجلات وينشرها نوعًا واحدًا من المستندات في كل مرة: توليد، ثم إضافة التشويش، ثم تصدير إلى Parquet، ثم رفع،
ثم حذف الملفات المحلية. يحتفظ القرص بملفات نوع واحد فقط في كل مرة، بحد أقصى يقارب 9 GB لنوع
`purchase_order`. بعد رفع كل نوع، يدمج البرنامج إحصاءات الأجزاء المكتملة، ويحدّث بطاقة المجموعة والعارض،
ويحذف تصدير المجلدات السابق من Hub مع الاحتفاظ بأجزاء Parquet. تتاح الأجزاء المكتملة فورًا في المجموعة
الفرعية الافتراضية `all`، وتعرض البطاقة الأنواع المتبقية حتى يكتمل التشغيل. أعد تشغيل الأمر نفسه بعد الانقطاع؛
يُتجاوز ما سبق رفعه، ويُستأنف التوليد.

```bash
python3 scripts/run_in_parts.py                       # كل سجل من كل نوع إلى HF_REPO_ID
python3 scripts/run_in_parts.py --status              # الأنواع المكتملة والمتبقية
python3 scripts/run_in_parts.py --publish-only        # تحديث البطاقة والعارض من الأجزاء المرفوعة سابقًا
python3 scripts/run_in_parts.py --types payslip --no-push   # تجربة: تصدير نوع واحد دون نشر
```

تستخدم مجموعة البيانات الكاملة Parquet لأن Hub يسمح بحد أقصى قدره 10 آلاف ملف لكل مجلد، ويوصي بأقل من
100 ألف ملف لكل مستودع؛ ولا يمكن الالتزام بذلك عند تخزين ملف مستقل لكل PDF ضمن 354 ألف مستند.
تُحسب تقسيمات البيانات على مستوى جميع الشركات باستخدام البذرة 0، بحيث يتبع كل جزء التقسيم نفسه المستخدم
في مجموعة البيانات المنشورة ذات 2,000 مستند لكل نوع.

يمكن إضافة صور المسح لاحقًا بصورة مستقلة:
`python3 scripts/run_pipeline.py --stages augment export --augment-fraction 0.1`.

يعيد `--everything` محاولة توليد النصوص حتى تتوفر نصوص لكل شركة ومنتج، حتى عدد المحاولات المحدد عبر
`--text-passes`، مع الانتظار بين المحاولات. إذا نفدت الحصة اليومية المجانية يتوقف التشغيل؛ استأنفه في اليوم
التالي باستخدام `--from texts`، أو اسمح بالنصوص البديلة المضمّنة باستخدام `--allow-missing-texts`.

الخيارات: `--out`، `--dataset`، `--workers` (افتراضيًا: جميع الأنوية)، `--seed`، `--augment-fraction`،
`--no-pdf`، `--config <file>` (ملف إعدادات مخصص للتشغيل)، `--skip <stages>`، `--force-download`،
`--rebuild-venv`. تُسجَّل تفاصيل كل تشغيل في `logs/pipeline-<time>.log`.

## الإعداد

يتطلب Python 3.12 أو أحدث، ومكتبتَي Pango وHarfBuzz اللتين تستخدمهما WeasyPrint، وهما مثبتتان افتراضيًا على Ubuntu.

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

> بعد ترقية نظام التشغيل التي تغيّر إصدار Python النظام، مثل الانتقال من 3.12 إلى 3.14، قد تتعطل البيئة
> الافتراضية وتظهر رسالة `No module named 'yaml'`. أعد إنشاءها باستخدام
> `python3 -m venv --clear venv`، ثم أعد تثبيت الاعتماديات.

### البيانات

قواعد البيانات المصدرية **غير محفوظة في git**؛ يبلغ حجمها نحو 140 MB، ويتجاوز ملف `adventureworks.db`
حد GitHub البالغ 100 MB. يتولى برنامج واحد تنزيلها وبناءها، لذا بعد استنساخ المستودع لأول مرة شغّل:

```bash
python3 scripts/run_pipeline.py --stages venv download index   # البيئة الافتراضية والقواعد الأربع والفهارس
```

لتنزيل قواعد البيانات فقط، استخدم `python3 scripts/download_sources.py`. يتجاوز البرنامج القواعد الموجودة؛
استخدم `--force` لإعادة التنزيل، أو `--only sakila chinook` لتنزيل بعضها. يحتاج إلى مكتبة Python القياسية
فقط، وإلى `git` لتنزيل Northwind.

```bash
python -m docgen sources list               # عرض قواعد البيانات المصدرية
python scripts/prepare_sources.py          # إضافة فهارس البحث مرة واحدة
python scripts/build_northwind_sqlite.py   # اختياري: إنشاء Northwind الكلاسيكية، 830 طلبًا
```

تُنزَّل التراخيص أيضًا. يُحفظ `data/textbank/` في git لأن إعادة إنشاء نصوصه تستهلك حصة API.

| المسار | المصدر |
|---|---|
| `data/sample_dbs_part1_northwind_chinook_sakila/sqlite/` | Northwind الموسعة، 16 ألف طلب، وChinook وSakila |
| `data/sample_dbs_part2_adventureworks/sqlite/adventureworks.db` | AdventureWorks OLTP، مع 68 جدولًا |
| `data/northwind/` | Northwind بلغة T-SQL من Microsoft عبر `scripts/clone_sources.py`، اختياري |
| `data/company-documents/` | مجموعة البيانات v1 للمرجعية فقط، وليست مدخلًا للتوليد |

```bash
python3 scripts/download_sources.py        # قواعد البيانات الأربع
python scripts/prepare_sources.py          # إضافة فهارس البحث مرة واحدة
python scripts/build_northwind_sqlite.py   # اختياري: Northwind الكلاسيكية، 830 طلبًا، إلى data/northwind.db
```

### النماذج اللغوية الكبيرة (اختياري)

```bash
cp .env.example .env                       # ضبط OPENROUTER_API_KEY بمفتاح مجاني
python scripts/openrouter_models.py        # عرض النماذج المجانية الحالية واختيار أحدها
python -m docgen llm ping
```

عند غياب المفتاح، تستخدم الشركات النصوص البديلة المضمّنة. تسمح الحسابات المجانية بنحو 50 طلبًا يوميًا،
لذا يملأ النموذج مخزون النصوص فقط، بواقع 4 شركات لكل طلب و20 منتجًا لكل طلب.
لا يُستدعى لكل مستند، وتُخزَّن كل استجابة مؤقتًا في `data/llm_cache.db`.

## الاستخدام

```bash
# 1. الجهات المُصدرة: 15 شركة لكل قطاع، ثم توزيع جميع تركيبات التخطيط والسمات على كل قطاع
python -m docgen companies init --count 15 --llm
python -m docgen companies rebalance
python -m docgen companies list

# 2. النصوص: محتوى الشركات وأوصاف منتجات Northwind، مع التخزين المؤقت وإمكانية إعادة التشغيل بأمان
python -m docgen texts generate

# 3. التصميم: توليد مستند واحد ومعاينته
python -m docgen list
python -m docgen preview invoice --open
python -m docgen preview payslip --company iris-sports-corp --html-only   # HTML لأدوات المطور في المتصفح

# 4. التوليد، وإضافة صور مسح بجودة منخفضة، والتصدير
python -m docgen generate configs/example.yaml
python -m docgen augment output/all-documents-demo --fraction 0.3
python -m docgen export output/all-documents-demo dataset/

python -m unittest discover -s tests
```

## أنواع المستندات

| النوع | المصادر | السجلات |
|---|---|---|
| فاتورة (invoice) | Northwind, AdventureWorks, Chinook | 48,159 |
| عرض سعر (quote) · إشعار دائن (credit note) · قائمة تعبئة (packing slip) · أمر شحن (shipping order) | Northwind, AdventureWorks | 47,747 لكل نوع |
| أمر شراء (purchase order) | Northwind, AdventureWorks | 50,313 |
| أمر عمل (work order) | AdventureWorks | 42,625 |
| إيصال بحجم A5 (receipt) | Chinook, Sakila | 16,456 |
| إشعار استلام بضائع (goods received note) | AdventureWorks | 3,689 |
| كشف حساب (account statement) | Northwind, AdventureWorks | 728 |
| كشف راتب (payslip) · شهادة عمل (employment certificate) | AdventureWorks | 290 لكل نوع |
| تقرير مخزون (inventory report) | Northwind, AdventureWorks | 52 |

يرتبط كل مصدر بقطاع محدد، ولا تُصدر مستنداته إلا شركات من القطاع نفسه:
Northwind للأغذية، وAdventureWorks للدراجات، وChinook للموسيقى، وSakila للفيديو.

## إعدادات التشغيل

```yaml
seed: 42
out_dir: output/v2
workers: 8
documents:
  - {type: invoice, count: 2000}                    # توزيع بالتساوي على المصادر دون تكرار السجلات
  - {type: payslip, count: 2000}                    # 290 موظفًا فقط: إعادة استخدامهم مع شركات وتواريخ مختلفة
  - {type: receipt, count: all}                     # كل سجل مرة واحدة
  - {type: "*", count: 500, locales: [fr]}          # كل الأنواع، مع الشركات الفرنسية فقط
  - {type: invoice, count: 100, sources: [chinook], companies: [quarry-music-llc]}
fx: {USD: "1", EUR: "0.92", GBP: "0.79"}
date_shift: {sakila: 19}                            # نقل نشاط Sakila من 2005-2006 إلى 2024-2025
self_check: true
boxes: true
```

يولّد التشغيل الكامل الذي يستخدم كل سجل مرة واحدة نحو 354 ألف مستند، بحجم 16 GB، خلال نحو 10 ساعات
باستخدام 8 عمليات عاملة. يولّد التشغيل المتوازن الذي يستخدم 2,000 مستند لكل نوع نحو 26 ألف مستند، بحجم
يقارب 1.2 GB، خلال نحو ساعة.

## المخرجات

```text
output/<run>/
  pdf/<type>/<doc_id>.pdf          مستند رقمي أصلي دون تشويش
  gold/<type>/<doc_id>.json        الحقول المرجعية، والنصوص المعروضة، ومربعات الحقول، والخيارات، والشركة، والتخطيط
  words/<type>/<doc_id>.json       كل كلمة في PDF مع رقم الصفحة ومربع الإحاطة
  scans/<type>/<doc_id>_p<n>_<profile>.jpg (+ .json)   صفحات مشوّشة مع مربعات إحاطة محوّلة (augment)
  manifest.jsonl                   سطر لكل مستند مخطط: ok | failed | error
  config.yaml                      الإعدادات الفعلية المستخدمة

dataset/                           (export، صيغة csv الافتراضية التي يقرأها عارض Hugging Face)
  pdf/<type>/<split>/<doc_id>.pdf + metadata.jsonl     سطر لكل PDF: المسارات، والشركة، والمصدر، والحقول الأساسية،
                                                     وJSON المرجعي، ونص PDF
  json/<type>/<doc_id>.json                          الحقول المرجعية لكل PDF
  scans/<type>/<split>/*.jpg + metadata.jsonl          صور صفحات مشوّشة مع مربعات إحاطة مرجعية بالبكسل
  README.md (بطاقة: المجموعة all ومجموعة لكل نوع)، stats.json، licenses/

dataset/                           (export --format parquet: للتشغيلات الكبيرة وحدود Hub)
  data/<type>/<split>-NNNNN.parquet          سطر لكل PDF: ملف PDF (ميزة Pdf)، والأعمدة المذكورة أعلاه،
                                             والخيارات، والقيم المطبوعة، ومربعات الحقول المرجعية والكلمات
  scans/<type>/<split>-NNNNN.parquet         صور صفحات مشوّشة (ميزة Image) مع مربعات إحاطة بالبكسل
  README.md (بطاقة: all ومجموعة لكل نوع وscans)، stats.json، licenses/
```

على Hub، يمثّل كل نوع مستند مجموعة فرعية مثل `load_dataset(repo, "invoice")`، مع تقسيمات التدريب
والتحقق والاختبار، ويعرض العارض ملفات PDF. في تنظيم المجلدات، لا تُعد صور المسح مجموعة فرعية؛ يختار Hub
محمّلًا واحدًا لكل مستودع، وهو محمّل PDF هنا، وتُحمَّل صور المسح باستخدام
`load_dataset(repo, data_dir="scans")`. أما Parquet فيحمل تعريفات أنواع بياناته، لذا تكون `scans` فيه مجموعة
فرعية مثل بقية المجموعات. يناسب تنظيم المجلدات التشغيلات التي لا تتجاوز نحو 10 آلاف مستند لكل نوع وتقسيم.

تُقسَّم البيانات حسب الشركة المُصدرة داخل كل قطاع؛ تأتي مستندات التحقق والاختبار من شركات لم تظهر في بيانات التدريب.

## النشر على Hugging Face

يرفع [scripts/push_to_hf.py](scripts/push_to_hf.py) مجلدًا مُصدَّرًا إلى مستودع مجموعة بيانات. وهو منفصل عن
خط المعالجة، لذا شغّله بعد مرحلة `export` على أي جهاز يحتوي على المجلد.

```bash
# .env: HF_TOKEN=<write token from https://huggingface.co/settings/tokens>, HF_REPO_ID=<user>/<name>
# أو شغّل hf auth login مرة واحدة ومرّر --repo
./venv/bin/python scripts/push_to_hf.py --dry-run                 # معاينة ما سيُرفع
./venv/bin/python scripts/push_to_hf.py                           # رفع dataset/v2 وإنشاء مستودع خاص
./venv/bin/python scripts/push_to_hf.py --repo <user>/<name> --public
./venv/bin/python scripts/push_to_hf.py --exclude "scans/*"       # استبعاد صور المسح المشوّشة
```

يرفع البرنامج 1,000 ملف في كل عملية إيداع، ويمكن تعديل ذلك باستخدام `--batch`، بحيث يلتزم تصدير يحتوي
على 60 ألف ملف بحدود Hub لكل عملية إيداع. إذا انقطع التشغيل، أعد تشغيله؛ تُتجاوز الملفات الموجودة على Hub
بالحجم نفسه. تُقارن بطاقة المجموعة وملفات `stats.json` و`metadata.jsonl` حسب المحتوى، وتُرسل أخيرًا، حتى
لا يشير عارض المجموعة إلى ملفات غير موجودة. يسمح Hub بـ128 عملية إيداع لكل مستودع في الساعة؛ إذا بلغ
البرنامج هذا الحد، يتوقف ويمكن استئنافه لاحقًا. يختار `--folder` مجلد تصدير آخر، ويحدد `--revision` فرعًا،
ويحذف `--delete-stale` الملفات البعيدة التي لم تعد موجودة محليًا.

## بنية المشروع

```text
docgen/
  __main__.py          واجهة سطر الأوامر
  core/                الفئة الأساسية للمستندات، والسجلات، والمبالغ، واللغة والمنطقة، والسمات، والتصيير، والفحص الذاتي،
                       وخط المعالجة، والشركات، وعميل النماذج اللغوية، وبنك النصوص، والتشويش، والتصدير
  sources/             محوّل لكل قاعدة بيانات (northwind, adventureworks, chinook, sakila)
  documents/           أنواع المستندات وقوالب templates/<type>/document.html.j2
  templates/           base.html.j2 وcomponents.html.j2 (ماكرو)، وlayouts/ وcss/
  themes/  locales/    ملفات YAML
companies/<slug>/             company.yaml لكل شركة، مع templates/ اختيارية تتجاوز القوالب المشتركة
configs/               إعدادات التشغيل
scripts/               خط المعالجة، وتنزيل البيانات وإعدادها، والرفع إلى Hugging Face، واختيار نموذج OpenRouter
tests/                 اختبارات الوحدات؛ يُصيَّر كل متغير ويُفحص ذاتيًا
```

## الترخيص

الشيفرة والمستندات المولّدة مرخّصة بموجب Apache-2.0. البيانات المصدرية: Northwind وAdventureWorks
وChinook بموجب MIT، وSakila بموجب BSD-2.
توجد ملفات التراخيص في `data/sample_dbs_*/licenses/`، وتُنسخ إلى كل عملية تصدير.
