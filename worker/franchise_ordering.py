"""
Franchise & Series Ordering Engine for Riva VOD Pipeline.
Extracts normalized franchise keys and installment sort keys for movies (Arabic & Foreign)
so multi-part movie collections always upload strictly in order (Part 1 -> Part 2 -> Part 3...).
"""
import html
import re
from typing import Optional, Tuple, Dict, Any, List

ARABIC_ORDINAL_MAP = {
    "الاول": 1, "الأول": 1, "اول": 1, "أول": 1,
    "الثاني": 2, "التاني": 2, "ثاني": 2,
    "الثالث": 3, "التالت": 3, "ثالث": 3,
    "الرابع": 4, "رابع": 4,
    "الخامس": 5, "خامس": 5,
    "السادس": 6, "سادس": 6,
    "السابع": 7, "سابع": 7,
    "الثامن": 8, "ثامن": 8,
    "التاسع": 9, "تاسع": 9,
    "العاشر": 10, "عاشر": 10,
    "الحادي عشر": 11,
    "الثاني عشر": 12,
}

ROMAN_MAP = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "xi": 11, "xii": 12,
}

WORD_NUM_MAP = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Normalize Arabic alef/teh-marbuta/tashkeel for reliable franchise matching
_AR_NORM_TRANS = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي",
})


def _normalize_ar(text: str) -> str:
    t = re.sub(r"[\u064B-\u065F\u0640]", "", text)
    return t.translate(_AR_NORM_TRANS)


def _parse_token_int(tok: str) -> Optional[int]:
    if not tok:
        return None
    t = tok.strip().lower()
    if t.isdigit():
        val = int(t)
        if 1 <= val <= 30:
            return val
        return None
    if t in ROMAN_MAP:
        return ROMAN_MAP[t]
    if t in WORD_NUM_MAP:
        return WORD_NUM_MAP[t]
    t_ar = _normalize_ar(t)
    for k, v in ARABIC_ORDINAL_MAP.items():
        if _normalize_ar(k) == t_ar:
            return v
    return None


def parse_movie_franchise_info(
    title: str,
    original_title: Optional[str] = None,
    year: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Analyzes a movie's title, original_title, and year to determine:
      - franchise_key: normalized base series/collection identifier
      - main_part: explicit main sequel number if present (e.g. Harry Potter 8 -> 8), else None
      - sub_part: explicit sub-part if after a subtitle (e.g. Deathly Hallows: Part 2 -> 2), else 1
      - year: parsed or DB release year
      - sort_key: tuple guaranteeing chronological/part order within the same franchise_key
    """
    raw = html.unescape(title or "").strip()
    orig = html.unescape(original_title or "").strip()

    # Extract year from title if missing
    detected_year = year
    if not detected_year:
        ym = re.search(r"\b(19\d{2}|20\d{2})\b", raw + " " + orig)
        if ym:
            detected_year = int(ym.group(1))

    # Clean scraper noise from raw title
    cleaned = re.sub(r"[\(\[]?\b(19\d{2}|20\d{2})\b[\)\]]?", " ", raw)
    cleaned = re.sub(
        r"\b(?:مشاهدة|وتحميل|تحميل|فيلم|انمي|أنمي|مترجم|مترجمة|مدبلج|مدبلجة|اون\s*لاين|أون\s*لاين|كامل|كاملة|بجودة\s*عالية|HD|FHD|4K|1080p|720p|480p|BluRay|WEB-DL|WEBRip|HDRip)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:|/–—")

    # Extract Arabic part if present in raw title (even if we use English base)
    ar_part: Optional[int] = None
    ar_part_m = re.search(
        r"(?:الجزء|جزء|بارت)\s+(الحادي\s+عشر|الثاني\s+عشر|[^\s\-:|]+)",
        cleaned,
    )
    if ar_part_m:
        ar_part = _parse_token_int(ar_part_m.group(1))
        cleaned = (cleaned[:ar_part_m.start()] + " " + cleaned[ar_part_m.end():]).strip()

    # Decide working title: prefer English if substantial (handles bilingual scraped titles cleanly)
    eng_chunks = re.findall(r"[A-Za-z0-9][A-Za-z0-9\s\'\:\-\.\!\&]*", cleaned)
    eng_from_raw = " ".join(c.strip() for c in eng_chunks if len(c.strip()) >= 2).strip()

    # Notice: raw scraped title often has explicit sequel number ("Twilight 2") while TMDB original_title
    # might have subtitle ("The Twilight Saga: New Moon"). Check raw English for explicit number FIRST!
    raw_seq_num: Optional[int] = None
    if eng_from_raw:
        m_raw_num = re.search(r"^(?P<base>[A-Za-z][A-Za-z0-9\s\'\.\&\-]{2,}?)\s+(?P<num>\d{1,2}|ii|iii|iv|v|vi|vii|viii|ix|x)\b", eng_from_raw, flags=re.I)
        if m_raw_num:
            parsed_n = _parse_token_int(m_raw_num.group("num"))
            if parsed_n:
                raw_seq_num = parsed_n

    work = orig if (orig and re.search(r"[A-Za-z]{3,}", orig)) else (eng_from_raw if len(eng_from_raw) >= 3 else cleaned)
    work = re.sub(r"[\(\[]?\b(19\d{2}|20\d{2})\b[\)\]]?", " ", work)
    work = re.sub(r"\s+", " ", work).strip(" -:|/–—")

    is_english = bool(re.search(r"[A-Za-z]{3,}", work))
    main_part: Optional[int] = raw_seq_num or ar_part
    sub_part: int = 1
    base = work

    if is_english:
        w_low = work.lower()
        # Normalize known saga wrappers (e.g. "The Twilight Saga: New Moon" -> "twilight: new moon")
        w_low = re.sub(r"^the\s+([a-z0-9\s]+?)\s+saga\b", r"\1", w_low)
        # Normalize "2 Fast 2 Furious" / "Fast Five" / "Furious 7" / "F9" -> "fast and furious"
        if any(
            pat in w_low
            for pat in ("fast & furious", "fast and furious", "fast and the furious", "2 fast 2 furious", "fast five", "furious 7", "fate of the furious", "fast x")
        ) or w_low in ("f9", "f9: the fast saga"):
            base = "fast and furious"
            ff_map = {
                "the fast and the furious": 1,
                "2 fast 2 furious": 2,
                "tokyo drift": 3,
                "fast & furious": 4,
                "fast five": 5,
                "fast & furious 6": 6,
                "furious 7": 7,
                "fate of the furious": 8,
                "f9": 9,
                "fast x": 10,
            }
            for k_ff, v_ff in ff_map.items():
                if k_ff in w_low:
                    main_part = main_part or v_ff
            return _finalize_info(base, main_part, sub_part, detected_year)

        # Check sub-part ("... : Part 2" or "... - Part II")
        sub_m = re.search(
            r"(?:[:\-–—]\s*|\s+)(?:part|vol\.?|volume)\s+(\d{1,2}|i|ii|iii|iv|v|vi|vii|viii|ix|x|one|two|three|four)\s*$",
            w_low,
            flags=re.I,
        )
        if sub_m:
            sp = _parse_token_int(sub_m.group(1))
            if sp:
                sub_part = sp
            w_low = w_low[:sub_m.start()].strip(" -:|/–—")

        # Check explicit Chapter / Episode / Part right after main title ("John Wick: Chapter 4", "Star Wars: Episode III ...")
        chap_m = re.search(
            r"^(?P<b1>[a-z0-9\'\.\&\s]{2,}?)(?:\s*[:\-–—]\s*|\s+)(?:chapter|episode|part)\s+(?P<num>\d{1,2}|i|ii|iii|iv|v|vi|vii|viii|ix|x|one|two|three|four)\b",
            w_low,
            flags=re.I,
        )
        if chap_m:
            b_cand = chap_m.group("b1").strip()
            p_cand = _parse_token_int(chap_m.group("num"))
            if len(b_cand) >= 2 and p_cand:
                base = b_cand
                main_part = main_part or p_cand
                return _finalize_info(base, main_part, sub_part, detected_year)

        # Split subtitle on ":" or " - " if prefix is >= 3 chars
        if ":" in w_low:
            prefix = w_low.split(":", 1)[0].strip()
            if len(prefix) >= 3:
                w_low = prefix
        elif " - " in w_low:
            prefix = w_low.split(" - ", 1)[0].strip()
            if len(prefix) >= 3:
                w_low = prefix

        # Handle "<Hero> and the <Quest>" franchises (Harry Potter and the..., Indiana Jones and the...)
        and_the_m = re.match(r"^(harry potter|indiana jones|percy jackson|fantastic beasts|alvin and the chipmunks)\s+(?:and\s+the|&\s+the)\s+.+$", w_low)
        if and_the_m:
            w_low = and_the_m.group(1)

        # Check embedded or trailing number / Roman numeral ("Harry Potter 8 ...", "Saw III", "Iron Man 3", "Rocky IV")
        trail_m = re.match(
            r"^(?P<b2>[a-z0-9\'\.\&\s]{2,}?)\s+(?P<num>\d{1,2}|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)(?:\s+.+)?$",
            w_low,
        )
        if trail_m:
            b_cand = trail_m.group("b2").strip()
            p_cand = _parse_token_int(trail_m.group("num"))
            if len(b_cand) >= 2 and p_cand:
                w_low = b_cand
                main_part = main_part or p_cand

        w_low = re.sub(r"^the\s+", "", w_low)
        w_low = re.sub(r"[^a-z0-9\s]", " ", w_low)
        base = re.sub(r"\s+", " ", w_low).strip()
    else:
        # Arabic movie title normalization (also strips trailing subtitle after part number, e.g. "ولاد رزق 3 القاضية")
        ar_norm = _normalize_ar(cleaned)
        if ":" in ar_norm:
            p0 = ar_norm.split(":", 1)[0].strip()
            if len(p0) >= 3:
                ar_norm = p0
        trail_ar = re.match(r"^(?P<b_ar>.{3,}?)\s+(?P<num>\d{1,2})(?:\s+.+)?$", ar_norm)
        if trail_ar:
            p_cand = _parse_token_int(trail_ar.group("num"))
            if p_cand:
                ar_norm = trail_ar.group("b_ar").strip()
                main_part = main_part or p_cand
        ar_norm = re.sub(r"[^\u0600-\u06FF0-9\s]", " ", ar_norm)
        base = re.sub(r"\s+", " ", ar_norm).strip()

    if not base:
        base = _normalize_ar(raw.lower())
    return _finalize_info(base, main_part, sub_part, detected_year)


def _finalize_info(
    base: str,
    main_part: Optional[int],
    sub_part: int,
    year: Optional[int],
) -> Dict[str, Any]:
    """
    Constructs consistent sort tuple for movies within the same franchise.
    - If explicit main_part is known on numbered series, main_part orders 1 -> 2 -> 3;
      when main_part is None (e.g. subtitle movies like Fellowship / Two Towers, or Part 1 without '1'),
      release year orders chronologically, and plain untitled original ('John Wick' before 'John Wick 2')
      defaults to part 1 when year matches or precedes sequels!
    """
    yr_val = year if (year and 1900 <= year <= 2100) else 9999
    # Effective part when comparing numbered franchises without years: plain base title = Part 1
    eff_part = main_part if main_part is not None else 1
    # Sort key: if year is valid, (year, eff_part, sub_part) handles both subtitled & numbered franchises
    # EXCEPT if year is missing (9999), where eff_part still orders Part 1 -> Part 2 -> Part 3!
    # Wait: what if main_part IS explicitly set (e.g. Part 1 vs Part 2 where one row has year=None)?
    # Using (main_part if main_part is not None else (1 if yr_val == 9999 else None)) -> see compare helper below!
    return {
        "franchise_key": base,
        "main_part": main_part,
        "effective_part": eff_part,
        "sub_part": sub_part,
        "year": year,
        "year_sort": yr_val,
        "is_explicit_sequel": (main_part is not None and main_part >= 2) or sub_part >= 2,
    }


def sort_franchise_movies(movies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sorts a list of movie dicts belonging to the SAME franchise_key in strict viewing/release order:
    Part 1 -> Part 2 -> Part 3 ...
    Handles mixed cases (e.g. some rows have explicit part numbers 1..8 without years,
    some have subtitles + release years, and Part 1 often omits the number '1').
    """
    has_any_explicit_num = any(m["info"]["main_part"] is not None for m in movies)
    all_have_explicit_num_or_base = all(
        (m["info"]["main_part"] is not None) or (not m["info"]["is_explicit_sequel"])
        for m in movies
    )

    def _key(item: Dict[str, Any]):
        inf = item["info"]
        m_id = item.get("id", 0)
        # If the franchise uses explicit numbering (e.g. Twilight 1..5, Harry Potter 1..8, ولاد رزق 1..3)
        # and doesn't rely solely on unnumbered subtitles (like Fellowship / Two Towers / Return of the King
        # where all main_part are None):
        distinct_parts = {x["info"]["effective_part"] for x in movies}
        if has_any_explicit_num and len(distinct_parts) >= 2:
            # Wait: if two distinct movies both have main_part=None (two subtitled sequels), fall back to year first
            none_count = sum(1 for x in movies if x["info"]["main_part"] is None)
            if none_count <= 1:
                return (inf["effective_part"], inf["sub_part"], inf["year_sort"], m_id)
        # Chronological release year primary, then effective_part, then sub_part
        return (inf["year_sort"], inf["effective_part"], inf["sub_part"], m_id)

    return sorted(movies, key=_key)
