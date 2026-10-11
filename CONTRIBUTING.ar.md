<div dir="rtl">

# المساهمة في رفيق

*[English version](CONTRIBUTING.md)*

شكرًا لمساعدتك. رفيق مساعد صوتي لسطح مكتب لينكس يتكلم الإنجليزية والفرنسية والإسبانية والألمانية
والعربية الفصحى والعامية المصرية. هذه الصفحة فيها ما تحتاجه قبل أول طلب دمج (pull request). وفي
[CLAUDE.md](CLAUDE.md) القواعد نفسها باختصار لمساعدي البرمجة بالذكاء الاصطناعي.

## التجهيز

</div>

```bash
git clone https://github.com/nexika/rafiq.git && cd rafiq
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,google,offline-voice]"
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

<div dir="rtl">

الاختبارات لا تحتاج ميكروفونًا ولا سماعة ولا إنترنت ولا مفاتيح: تعمل في أي مكان، كما تعمل في CI على
بايثون من 3.11 إلى 3.14. لتجربة رفيق فعلًا، اقرأ [README](README.md) أو
[INSTALL_WITH_AI.ar.md](INSTALL_WITH_AI.ar.md). الأمر `rafiq ask "..."` يستعمل العقل والأدوات نفسها
بدون ميكروفون.

## كيف تترابط الأجزاء

</div>

```
microphone → audio.py (speech detection) → kws.py / wake.py ("Rafiq"?) → stt.py (Whisper)
  → brain.py (Claude or Ollama) ⇄ tools/ → tts.py (voice) → speaker
                                          ↘ panel.py (127.0.0.1)
```

<div dir="rtl">

| الملف | ما يفعله |
|---|---|
| `rafiq/__main__.py` | الأمر `rafiq`: run و ask و say و enroll و listen و doctor و google-setup |
| `rafiq/assistant.py` | الحلقة الرئيسية؛ كل ما يستعمل الميكروفون أو السماعة يعمل على خيط واحد |
| `rafiq/brain.py` | دورة واحدة مع Claude أو Ollama: النموذج يطلب أدوات، رفيق ينفذها، ثم يجيب النموذج |
| `rafiq/tools/` | المهارات؛ كل مهارة دالة مسجلة بـ `@tool(...)` |
| `rafiq/lang.py` | رموز اللغات، المصرية أم الفصحى، نعم أم لا، وعبارات رفيق نفسه |
| `rafiq/kws.py` و `rafiq/enroll.py` | كلمة التنبيه من تسجيلات صوتك |
| `rafiq/config.py` | الإعدادات في `~/.config/rafiq/config.toml`، مع قيمة افتراضية لكل شيء |
| `rafiq/panel.py` و `rafiq/panel.html` | الصفحة المحلية |

## إضافة مهارة

المهارة دالة عادية في `rafiq/tools/` لها وصف ومخطط JSON لمدخلاتها:

</div>

```python
@tool("volume", "Get or change the speaker volume.",
      {"level": {"type": "integer", "description": "set the volume to this percent, 0 to 100"}})
def volume(level: int | None = None) -> str:
    ...
```

<div dir="rtl">

- أرجِع نصًا قصيرًا للنموذج، وابدأه بـ `error:` إذا حدث خطأ.
- إذا كانت تغيّر شيئًا خارج رفيق (ترسل، تحذف، تشتري، تطفئ الجهاز)، مرّر `confirm=` مع وصف الفعل بكلمات
  يسمعها المستخدم ("shut down the computer"). رفيق يعيده على المستخدم ولا ينفذه إلا بعد "نعم" واضحة.
- النصوص الآتية من الخارج (صفحات الويب، الرسائل، نتائج البحث) بيانات للنموذج، وليست تعليمات أبدًا.
- شغّل الأوامر كقائمة وسائط مع مهلة زمنية، ولا تمررها إلى الصدفة (shell) أبدًا.
- كل ما يسمعه المستخدم من رفيق نفسه يوضع في `lang.PHRASES` باللغات الست، والمصرية مكتوبة كما يقولها
  المصري فعلًا.

## الاختبارات

- اكتب الاختبار الفاشل أولًا في `tests/`، واسمه يقول ما يثبته
  (`test_actions_that_change_something_are_confirmed_first`).
- العتاد والحسابات وخدمات الشبكة (الميكروفون، السماعة، Claude، Ollama، Azure، Google، الويب) تكون خلف
  دالة أو صنف صغير تستبدله الاختبارات بـ `monkeypatch`. الاختبار الذي يحتاج شبكة أو جهازًا لا مكان له
  في مجموعة الاختبارات.
- الملف `tests/conftest.py` يعطي كل اختبار مجلد إعدادات خاصًا به ويزيل المفاتيح الحقيقية.

## الأسلوب

- طابق الكود المحيط: `from __future__ import annotations`، وتلميحات الأنواع على الدوال العامة، وأسطر
  حتى 120 حرفًا، و`ruff check .` بلا ملاحظات.
- التعليقات بالإنجليزية وتشرح السبب بكلمات بسيطة، مع أرقام مقيسة إن وجدت ("caught 6 of 8 phrases").
- اجعل كل تغيير لمشكلة (issue) واحدة.

## طلبات الدمج

1. أنشئ فرعًا من `main`؛ لا تدفع إلى `main` مباشرة أبدًا.
2. أضف ملاحظة تغيير: ملفًا `changelog.d/<issue>.<added|changed|fixed>.md` فيه جملة واحدة بالإنجليزية تقول
   ما يستطيع المستخدم فعله الآن، مثل `changelog.d/78.added.md`.
3. رسائل الإيداع (commit) قصيرة بالإنجليزية على نمط `git log --oneline`، مثل:
   "Wake word: learn the name as said before a request".
4. افتح طلب الدمج مع ماذا ولماذا، وكيف اختبرته، و`Closes #<issue>`. يجب أن يكون الفحص "CI passed"
   أخضر قبل الدمج.

الأخطاء والأفكار تُكتب في [المشكلات (issues)](https://github.com/nexika/rafiq/issues). عند الإبلاغ عن
خطأ، أرفق ما يطبعه `rafiq doctor`.

</div>
