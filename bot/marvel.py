"""
Marvel Universe catalog for the Riva bot ("🦸 عالم مارفل").

Pure-Python classification (no DB access here):
  - MARVEL_FRANCHISES: movie franchises in display order, each matched by English/Arabic
    title patterns + a release-year window, movies sorted by release year inside each franchise.
  - MARVEL_SERIES: Marvel TV series in release order (Netflix / ABC / Disney+ / animated).

A single loose SQL prefilter (MARVEL_*_SQL_PREFILTER) narrows rows in Postgres, then the strict
Python patterns below decide the exact franchise / order. This keeps the SQL cheap and the
matching accurate (e.g. "The Avengers" 1998 or "Logan Lucky" are not Marvel).
"""
from __future__ import annotations

import re

_AR = "ء-ي"
_AR_TRANS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})


def normalize(text: str | None) -> str:
    """Lower-case + unify Arabic alef/teh-marbuta/yeh + drop tashkeel so patterns match all spellings."""
    t = re.sub(r"[ً-ٟـ]", "", text or "")
    return t.translate(_AR_TRANS).lower()


def _ar_word(word: str) -> str:
    """Arabic whole-word pattern (so 'ثور' never matches 'ثوره')."""
    return rf"(?<![{_AR}]){word}(?![{_AR}])"


# key, button label, regex (on normalized "original_title | title"), (min_year, max_year)
# ORDER MATTERS twice: it's the display order AND the match priority (first match wins),
# so e.g. Deadpool comes before X-Men ("Deadpool & Wolverine") and Spider-Man before Venom.
MARVEL_FRANCHISES: list[tuple[str, str, str, tuple[int, int]]] = [
    ("avg", "المنتقمون Avengers 🛡️", r"avengers|المنتقمون|افنجرز|الافنجرز", (2012, 2100)),
    ("irn", "الرجل الحديدي Iron Man 🤖", r"iron[\s-]?man|الرجل الحديدي|ايرون ?مان", (2008, 2100)),
    ("cap", "كابتن أمريكا Captain America 🇺🇸", r"captain america|كابتن امريكا", (2011, 2100)),
    ("thr", "ثور Thor ⚡", r"\bthor\b|" + _ar_word("ثور"), (2011, 2100)),
    ("hlk", "هالك Hulk 💚", r"(?<!she-)(?<!she )\bhulk\b|" + _ar_word("هالك"), (2003, 2100)),
    ("gog", "حراس المجرة Guardians 🚀", r"guardians of the galaxy|حراس المجره", (2014, 2100)),
    ("ant", "الرجل النملة Ant-Man 🐜", r"\bant[\s-]?man\b|الرجل النمله|انت ?مان", (2015, 2100)),
    ("drs", "دكتور سترينج Doctor Strange 🔮", r"doctor strange|dr\.? strange|دكتور ستر(ي|ا)نج", (2016, 2100)),
    ("bpn", "النمر الأسود Black Panther 🐾", r"black panther|النمر الاسود|بلاك بانث(ر|ير)", (2018, 2100)),
    ("spd", "سبايدر مان Spider-Man 🕷️", r"spider[\s-]?man|سبايدر ?مان|الرجل العنكبوت", (2002, 2100)),
    ("cmv", "كابتن مارفل Captain Marvel ✨", r"captain marvel|\bthe marvels\b|كابتن مارفل|ذا مارفلز", (2019, 2100)),
    ("bwd", "الأرملة السوداء Black Widow 🕸️", r"black widow|الارمله السوداء|بلاك ويدو", (2021, 2100)),
    ("shc", "شانغ تشي Shang-Chi 🐉", r"shang[\s-]?chi|شان(غ|ج) ?تشي", (2021, 2100)),
    ("etn", "الأبديون Eternals 🌌", r"\beternals\b|الابديون|ايترنالز", (2021, 2100)),
    ("tbt", "ثاندربولتس Thunderbolts ⚡", r"thunderbolts|ثاندربولتس", (2025, 2100)),
    ("ff4", "الفريق المذهل Fantastic Four 4️⃣", r"fantastic (four|4)|الرباعي المذهل|فانتاستك فور|فانتاستيك فور", (2005, 2100)),
    ("ddp", "ديدبول Deadpool 💀", r"deadpool|ديد ?بول", (2016, 2100)),
    ("xmn", "إكس-مين X-Men 🧬", r"\bx[\s-]?men\b|\bwolverine\b|^logan$|اكس ?مين|و?ولفرين", (2000, 2100)),
    ("ven", "فينوم وعالم سوني Venom 🖤", r"\bvenom\b|morbius|madame web|kraven|فينوم|موربيوس|مدام ويب|كرافن", (2018, 2100)),
    ("bld", "بليد Blade 🗡️", r"\bblade\b(?!\s*runner)(?! of the)|" + _ar_word("بليد"), (1998, 2012)),
    ("gsr", "الشبح الراكب Ghost Rider 🔥", r"ghost rider|الشبح الراكب|جوست رايدر", (2007, 2100)),
]

FRANCHISE_LABELS = {k: lbl for k, lbl, _, _ in MARVEL_FRANCHISES}
_FRANCHISE_RX = [(k, re.compile(rx, re.IGNORECASE), yr) for k, _, rx, yr in MARVEL_FRANCHISES]

# Marvel TV series in release order: (regex, (min_year, max_year))
MARVEL_SERIES: list[tuple[str, tuple[int, int]]] = [
    (r"x-men:? the animated series|^x-men$", (1992, 1997)),
    (r"spider-man:? the animated series", (1994, 1998)),
    (r"agents of s\.?h\.?i\.?e\.?l\.?d|عملاء شيلد|عملاء اس اتش اي اي ال دي", (2013, 2100)),
    (r"agent carter|العميله كارتر|ايجنت كارتر", (2015, 2100)),
    (r"^(marvel'?s )?daredevil$|^ديرديفل$|^دير ديفل$|daredevil(?!.*born again)(?!:)|ديرديفل(?!.*(ولاده|بورن))", (2015, 2024)),
    (r"jessica jones|جيسيكا جونز", (2015, 2100)),
    (r"luke cage|لوك كيج", (2016, 2100)),
    (r"iron fist|القبضه الحديديه|ايرون فيست", (2017, 2100)),
    (r"\bthe defenders\b|المدافعون|ذا ديفندرز", (2017, 2017)),
    (r"punisher|المعاقب|بانيشر", (2017, 2100)),
    (r"inhumans|اللابشريون|انهيومنز", (2017, 2100)),
    (r"^legion$|^ليجن$", (2017, 2019)),
    (r"runaways", (2017, 2019)),
    (r"^the gifted$|ذا جيفتد", (2017, 2019)),
    (r"cloak (&|and) dagger|كلوك (و|اند) داجر", (2018, 2100)),
    (r"helstrom|هيلستروم", (2020, 2100)),
    (r"wanda ?vision|واندا ?فيجن|واندا ?فيجين", (2021, 2100)),
    (r"falcon and the winter soldier|الصقر و ?جندي الشتاء|فالكون و", (2021, 2100)),
    (r"\bloki\b|" + _ar_word("لوكي"), (2021, 2100)),
    (r"^what if\b|what if\.\.\.", (2021, 2100)),
    (r"hawkeye|هوك ?اي|عين الصقر", (2021, 2100)),
    (r"moon knight|فارس القمر|مون نايت", (2022, 2100)),
    (r"ms\.? marvel|مس مارفل|الانسه مارفل", (2022, 2100)),
    (r"she[\s-]?hulk|شي[\s-]?هالك", (2022, 2100)),
    (r"secret invasion|الغزو السري", (2023, 2100)),
    (r"^echo$|^ايكو$", (2024, 2024)),
    (r"x-men '?97|اكس ?مين 97", (2024, 2100)),
    (r"agatha|اجاثا", (2024, 2100)),
    (r"friendly neighborhood spider|سبايدر ?مان الودود", (2025, 2100)),
    (r"daredevil:? born again|ديرديفل.*(ولاده|بورن)", (2025, 2100)),
    (r"ironheart|ايرون ?هارت", (2025, 2100)),
    (r"eyes of wakanda|عيون واكاندا", (2025, 2100)),
    (r"marvel zombies|زومبي مارفل|زومبيز مارفل", (2025, 2100)),
    (r"wonder man|وندر مان", (2025, 2100)),
]
_SERIES_RX = [(i, re.compile(rx, re.IGNORECASE), yr) for i, (rx, yr) in enumerate(MARVEL_SERIES)]

# Loose Postgres (~*) prefilters — no '%' and no quotes so they are safe as bound params/static SQL.
MARVEL_MOVIES_SQL_PREFILTER = (
    "avengers|iron.?man|captain america|thor|hulk|guardians of the galaxy|ant.?man|strange|"
    "black panther|spider.?man|captain marvel|the marvels|black widow|shang.?chi|eternals|thunderbolts|"
    "fantastic|deadpool|x.?men|wolverine|^logan( [^a-z]|$)|venom|morbius|madame web|kraven|blade|ghost rider|"
    "المنتقمون|افنجرز|أفنجرز|الحديدي|ايرون|آيرون|كابتن|ثور|هالك|المجر|النمل|انت مان|أنت مان|سترينج|سترانج|"
    "النمر|بانثر|سبايدر|العنكبوت|الأرمل|الارمل|ويدو|شانغ|شانج|الأبديون|الابديون|ترنالز|ثاندربولتس|"
    "الرباعي|فانتاست|ديدبول|ديد بول|مين|ولفرين|فينوم|موربيوس|مدام ويب|كرافن|بليد|الشبح الراكب|جوست"
)
MARVEL_SERIES_SQL_PREFILTER = (
    "x-men|spider-man|s\\.?h\\.?i\\.?e\\.?l\\.?d|agent carter|daredevil|jessica jones|luke cage|iron fist|"
    "defenders|punisher|inhumans|legion|runaways|gifted|cloak|helstrom|wanda|falcon|loki|what if|hawkeye|"
    "moon knight|marvel|hulk|secret invasion|echo|agatha|spider|ironheart|wakanda|zombies|wonder man|"
    "شيلد|كارتر|ديرديفل|دير ديفل|جيسيكا|لوك كيج|القبضة|القبضه|فيست|المدافعون|ديفندرز|المعاقب|بانيشر|"
    "اللابشريون|نهيومنز|ليجن|جيفتد|كلوك|هيلستروم|واندا|الصقر|فالكون|لوكي|ماذا لو|هوك|عين الصقر|"
    "فارس القمر|مون نايت|مارفل|هالك|الغزو السري|ايكو|إيكو|اكس مين|إكس مين|اجاثا|أجاثا|سبايدر|هارت|واكاندا|زومبي|وندر مان"
)


def _year_ok(year, window: tuple[int, int]) -> bool:
    if not year:
        return True  # unknown year: trust the title match
    try:
        y = int(year)
    except (TypeError, ValueError):
        return True
    return window[0] <= y <= window[1]


def _match_text(row: dict) -> list[str]:
    """Candidates to test: original title alone (for ^...$ patterns) and full combined text."""
    orig = normalize(row.get("original_title")).strip()
    title = normalize(row.get("title")).strip()
    return [t for t in (orig, title, f"{orig} | {title}") if t]


def classify_movie(row: dict) -> str | None:
    """Returns the Marvel franchise key for a movie row, or None if it's not a Marvel movie."""
    texts = _match_text(row)
    for key, rx, window in _FRANCHISE_RX:
        if any(rx.search(t) for t in texts) and _year_ok(row.get("year"), window):
            return key
    return None


def classify_series(row: dict) -> int | None:
    """Returns the release-order index of a Marvel series row, or None if it's not Marvel."""
    texts = _match_text(row)
    for idx, rx, window in _SERIES_RX:
        if any(rx.search(t) for t in texts) and _year_ok(row.get("year"), window):
            return idx
    return None
