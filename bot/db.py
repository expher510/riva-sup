import re
import json
import time
import html
import logging
import threading
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from bot.config import POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_HOST, POSTGRES_PORT
from bot import marvel

logger = logging.getLogger("riva_bot.db")

_db_pool = None
_pool_lock = threading.Lock()

# Fast in-memory TTL cache for instant (<1ms) UI transitions
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 45.0


def _get_pool():
    global _db_pool
    if _db_pool is None:
        with _pool_lock:
            if _db_pool is None:
                _db_pool = pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=12,
                    user=POSTGRES_USER,
                    password=POSTGRES_PASSWORD,
                    dbname=POSTGRES_DB,
                    host=POSTGRES_HOST,
                    port=POSTGRES_PORT,
                    connect_timeout=10
                )
    return _db_pool


@contextmanager
def db_cursor():
    conn = None
    p = _get_pool()
    try:
        conn = p.getconn()
        if conn.closed:
            p.putconn(conn, close=True)
            conn = p.getconn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield cur
        conn.commit()
    except Exception as e:
        if conn and not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error(f"Database error: {e}")
        raise
    finally:
        if conn:
            p.putconn(conn, close=conn.closed != 0)


def _cache_get(key: str):
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and now < entry[0]:
            return entry[1]
    return None


_last_cache_prune = 0.0
MENU_CACHE_TTL_SECONDS = 240.0  # heavy regex menu/count queries: recompute at most every 4 min


def _cache_set(key: str, val, ttl: float = CACHE_TTL_SECONDS):
    global _last_cache_prune
    now = time.monotonic()
    with _cache_lock:
        _cache[key] = (now + ttl, val)
        # Drop expired entries every ~2 min so the dict never grows unbounded
        if now - _last_cache_prune > 120.0:
            _last_cache_prune = now
            for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
                _cache.pop(k, None)


def clean_display_title(raw_title: str) -> str:
    if not raw_title:
        return ""
    t = html.unescape(raw_title).strip()
    prefix_patterns = [
        r"^\s*مشاهدة\s+وتحميل\s+(?:فيلم|مسلسل)?\s*",
        r"^\s*مشاهدة\s+(?:فيلم|مسلسل)?\s*",
        r"^\s*تحميل\s+(?:فيلم|مسلسل)?\s*",
        r"^\s*مسلسلات\s+\S+\s+مسلسل\s*",
        r"^\s*افلام\s+\S+\s+فيلم\s*",
        r"^\s*أفلام\s+\S+\s+فيلم\s*",
        r"^\s*مسلسل\s+",
        r"^\s*فيلم\s+",
    ]
    for pat in prefix_patterns:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    suffix_patterns = [
        r"\s+اون\s*لاين\s*$",
        r"\s+أون\s*لاين\s*$",
        r"\s+الاخيرة\s*$",
        r"\s+الأخيرة\s*$",
        r"\s+مترجم(?:ة)?\s*$",
    ]
    for pat in suffix_patterns:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()


# Region definitions: (short_code, movie_label, series_label)
REGION_DEFINITIONS = [
    ("ar", "أفلام عربي 🇪🇬", "مسلسلات عربي 🇪🇬"),
    ("en", "أفلام أجنبي 🇺🇸", "مسلسلات أجنبي 🇺🇸"),
    ("as", "أفلام آسيوية 🇰🇷", "مسلسلات آسيوية 🇰🇷"),
    ("an", "أفلام أنمي 🎌", "مسلسلات أنمي 🎌"),
    ("tr", "أفلام تركية 🇹🇷", "مسلسلات تركية 🇹🇷"),
]

REGION_SHORT_NAMES = {
    "ar": "عربي 🇪🇬",
    "en": "أجنبي 🇺🇸",
    "as": "آسيوي 🇰🇷",
    "an": "أنمي 🎌",
    "tr": "تركي 🇹🇷",
    "ad": "للكبار فقط 🔞",
    "all": "جميع اللغات 🌐",
}

# Genre definitions: (short_code, canonical_name, button_label)
GENRE_DEFINITIONS = [
    ("act", "Action", "أكشن وإثارة 💣"),
    ("drm", "Drama", "دراما 🔴"),
    ("hor", "Horror", "رعب 👻"),
    ("com", "Comedy", "كوميديا 🤡"),
    ("thr", "Thriller", "تشويق وغموض 🔥"),
    ("sci", "Science Fiction", "خيال علمي 💡"),
    ("rom", "Romance", "رومانسية 💓"),
    ("crm", "Crime", "جريمة وغموض 🪓"),
    ("adv", "Adventure", "مغامرات وتاريخ ⚔️"),
    ("fan", "Fantasy", "فانتازيا 🔮"),
    ("ani", "Animation", "كرتون وتحريك 🎨"),
    ("fam", "Family", "عائلي 👨‍👩‍👧‍👦"),
    ("war", "War", "حرب 🪖"),
    ("doc", "Documentary", "وثائقي 🌍"),
    ("col", "collections", "سلاسل أفلام 🔗"),
]

GENRE_CODE_TO_LABEL = {code: label for code, _, label in GENRE_DEFINITIONS}
GENRE_CODE_TO_LABEL["all"] = "الكل 🌐"

LEGACY_GENRE_TO_CODE = {canon: code for code, canon, _ in GENRE_DEFINITIONS}
LEGACY_GENRE_TO_CODE["all"] = "all"


def normalize_genre_code(genre_key: str | None) -> str:
    if not genre_key or genre_key == "all":
        return "all"
    if genre_key in GENRE_CODE_TO_LABEL:
        return genre_key
    return LEGACY_GENRE_TO_CODE.get(genre_key, "all")


def _is_adult_sql(alias: str) -> str:
    """Strictly identifies +18 / adult-only items so they never leak into family/general sections."""
    return (
        f"(COALESCE({alias}.category, '') = 'adult' "
        f"OR COALESCE({alias}.title, '') ~* '(للكبار فقط|\\+18|18\\+)' "
        f"OR COALESCE({alias}.overview, '') ~* '(للكبار فقط|\\+18)')"
    )


def _build_region_sql(alias: str, region: str | None) -> str:
    """
    Builds a pure static SQL boolean condition (zero % signs) classifying items by language/region:
    ar (Arabic), en (Foreign/English), as (Asian/Korean), an (Anime), tr (Turkish), ad (+18 Adult), or all.
    Strictly excludes +18 items from ALL general regions (including 'all') unless region == 'ad'.
    """
    not_adult = f"NOT ({_is_adult_sql(alias)})"
    if region == "ad":
        return _is_adult_sql(alias)
    if not region or region == "all":
        return not_adult

    is_ar_orig = f"(COALESCE({alias}.original_title, '') ~ '[ء-ي]' AND COALESCE({alias}.original_title, '') !~ '[A-Za-z]')"
    is_jp_cn_orig = f"(COALESCE({alias}.original_title, '') ~ '[\u3040-\u30FF\u4E00-\u9FFF]')"
    is_asian_orig = f"(COALESCE({alias}.original_title, '') ~ '[\uAC00-\uD7AF\u3040-\u30FF\u4E00-\u9FFF]')"
    is_anime = (
        f"({alias}.category = 'anime' "
        f"OR {alias}.title ~* '(انمي|أنمي)' "
        f"OR (COALESCE({alias}.genres::text, '') ~* 'Animation' AND {is_jp_cn_orig}))"
    )

    if region == "an":
        return f"(({is_anime}) AND {not_adult})"
    if region == "as":
        return (
            f"((({alias}.category IN ('asian', 'kdrama') "
            f"OR {alias}.title ~* '(اسيوي|آسيوي|كوري|ياباني|صيني)' "
            f"OR (COALESCE({alias}.category, '') NOT IN ('anime', 'arabic', 'turkish', 'adult') AND {is_asian_orig})) "
            f"AND NOT ({is_anime})) AND {not_adult})"
        )
    if region == "ar":
        return (
            f"(({alias}.category = 'arabic' "
            f"OR {alias}.title ~* '(فيلم عربي|مسلسل عربي|مسلسلات عربي|افلام عربي|أفلام عربي)' "
            f"OR (COALESCE({alias}.category, '') NOT IN ('asian', 'kdrama', 'anime', 'turkish', 'adult') AND {is_ar_orig})) "
            f"AND {not_adult})"
        )
    if region == "tr":
        return f"(({alias}.category = 'turkish' OR {alias}.title ~* '(تركي|تركية)') AND {not_adult})"
    if region == "en":
        return (
            f"((COALESCE({alias}.category, 'western') = 'western' "
            f"AND NOT ({is_ar_orig}) "
            f"AND NOT ({is_asian_orig}) "
            f"AND NOT ({alias}.title ~* '(انمي|أنمي|اسيوي|آسيوي|كوري|تركي|تركية)')) "
            f"AND {not_adult})"
        )
    return not_adult


def _build_genre_sql(alias: str, genre_key: str | None) -> str:
    """
    Builds a pure static SQL boolean condition (zero % signs) matching TMDB/Arabic genres.
    """
    code = normalize_genre_code(genre_key)
    if code == "all":
        return "TRUE"

    genre_regex_map = {
        "act": "(Action|أكشن|اكشن)",
        "drm": "(Drama|دراما)",
        "hor": "(Horror|رعب)",
        "com": "(Comedy|كوميد)",
        "thr": "(Thriller|تشويق|إثارة|اثارة)",
        "sci": "(Science Fiction|Sci-Fi|خيال علمي)",
        "rom": "(Romance|رومانس|رمنس)",
        "crm": "(Crime|Mystery|جريمة|غموض)",
        "adv": "(Adventure|History|Western|مغامر|تاريخ)",
        "fan": "(Fantasy|Adventure|فانتازيا)",
        "ani": "(Animation|انمي|أنمي|كرتون)",
        "fam": "(Family|عائلي)",
        "war": "(War|حرب)",
        "doc": "(Documentary|وثائقي)",
    }
    if code == "col":
        return (
            f"({alias}.title ~* '(28 |Chapter|Part|[0-9]$|Shazam|Prada|Freddy|Strangers|Twilight|Underworld|Fast|Saw)' "
            f"OR (COALESCE({alias}.original_title, '') || ' ' || COALESCE({alias}.title, '')) ~* '({marvel.MARVEL_MOVIES_SQL_PREFILTER})')"
        )

    rx = genre_regex_map.get(code)
    if not rx:
        return "TRUE"
    return f"(COALESCE({alias}.genres::text, '') ~* '{rx}')"


def get_vault_media_counts() -> dict:
    """Returns counts of ready-to-stream Movies ('m'), Series ('s'), and Adult +18 ('adult') in Telegram Vault."""
    cache_key = "vault_media_counts"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    ad_m = _is_adult_sql("m")
    ad_s = _is_adult_sql("s")
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT
                (SELECT COUNT(DISTINCT m.id)
                 FROM movies m
                 JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
                 WHERE NOT ({ad_m})) AS clean_movies_count,
                (SELECT COUNT(DISTINCT m.id)
                 FROM movies m
                 JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000) AS total_movies_count,
                (SELECT COUNT(DISTINCT m.id)
                 FROM movies m
                 JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
                 WHERE ({ad_m})) AS adult_movies_count,
                (SELECT COUNT(DISTINCT s.id)
                 FROM series s
                 JOIN seasons sea ON sea.series_id = s.id
                 JOIN episodes e ON e.season_id = sea.id
                 JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                 WHERE NOT ({ad_s})) AS clean_series_count,
                (SELECT COUNT(DISTINCT s.id)
                 FROM series s
                 JOIN seasons sea ON sea.series_id = s.id
                 JOIN episodes e ON e.season_id = sea.id
                 JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000) AS total_series_count,
                (SELECT COUNT(DISTINCT s.id)
                 FROM series s
                 JOIN seasons sea ON sea.series_id = s.id
                 JOIN episodes e ON e.season_id = sea.id
                 JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                 WHERE ({ad_s})) AS adult_series_count,
                (SELECT COUNT(DISTINCT ta.episode_id)
                 FROM telegram_archives ta
                 WHERE ta.episode_id IS NOT NULL AND ta.file_size_bytes > 15000000) AS episodes_count;
        """)
        row = cur.fetchone() or {}

    m_clean = int(row.get("clean_movies_count") or 0)
    s_clean = int(row.get("clean_series_count") or 0)
    m_total = int(row.get("total_movies_count") or 0)
    s_total = int(row.get("total_series_count") or 0)
    ad_total = int(row.get("adult_movies_count") or 0) + int(row.get("adult_series_count") or 0)
    # Also count manually-uploaded +18 movies (adult18_movies table)
    try:
        with db_cursor() as cur2:
            cur2.execute("SELECT COUNT(*) AS cnt FROM adult18_movies")
            ad_total += int((cur2.fetchone() or {}).get("cnt") or 0)
    except Exception:
        pass  # table may not exist yet on first boot before init_adult18_tables()
    e_cnt = int(row.get("episodes_count") or 0)
    res = {
        "m": m_clean,
        "s": s_clean,
        "adult": ad_total,
        "movies": m_total,
        "series": s_total,
        "episodes": e_cnt,
    }
    _cache_set(cache_key, res, ttl=MENU_CACHE_TTL_SECONDS)
    return res


def get_available_regions(media_type: str) -> dict:
    """
    Returns ONLY non-empty regions (count > 0) for 'm' (Movies) or 's' (Series)
    in a single SQL pass. Guarantees zero empty region buttons and strictly excludes +18 items.
    """
    cache_key = f"avail_regions:{media_type}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    alias = "m" if media_type == "m" else "s"
    not_adult = f"NOT ({_is_adult_sql(alias)})"
    filter_exprs = [
        f"COUNT(DISTINCT {alias}.id) FILTER (WHERE {_build_region_sql(alias, rcode)}) AS cnt_{rcode}"
        for rcode, _, _ in REGION_DEFINITIONS
    ]
    select_cols = ",\n                ".join([f"COUNT(DISTINCT {alias}.id) AS total_all"] + filter_exprs)

    if media_type == "m":
        sql = f"""
            SELECT
                {select_cols}
            FROM movies m
            JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
            WHERE {not_adult};
        """
    else:
        sql = f"""
            SELECT
                {select_cols}
            FROM series s
            JOIN seasons sea ON sea.series_id = s.id
            JOIN episodes e ON e.season_id = sea.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE {not_adult};
        """

    with db_cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone() or {}

    regions = []
    for rcode, m_label, s_label in REGION_DEFINITIONS:
        cnt = int(row.get(f"cnt_{rcode}") or 0)
        if cnt > 0:
            regions.append({
                "code": rcode,
                "label": m_label if media_type == "m" else s_label,
                "count": cnt,
            })

    res = {
        "total": int(row.get("total_all") or 0),
        "regions": regions,
    }
    _cache_set(cache_key, res, ttl=MENU_CACHE_TTL_SECONDS)
    return res


def get_available_genres(media_type: str, region: str = "all") -> dict:
    """
    Returns ONLY non-empty genres (count > 0) for the chosen media_type ('m'|'s') and region.
    Runs in a single fast SQL pass. Guarantees zero empty genre buttons.
    """
    cache_key = f"avail_genres:{media_type}:{region}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    alias = "m" if media_type == "m" else "s"
    reg_sql = _build_region_sql(alias, region)

    applicable_genres = []
    for gcode, canon, glabel in GENRE_DEFINITIONS:
        if media_type == "s" and gcode == "col":
            continue
        if region == "an" and gcode == "ani":
            continue
        applicable_genres.append((gcode, canon, glabel))

    filter_exprs = [
        f"COUNT(DISTINCT {alias}.id) FILTER (WHERE {_build_genre_sql(alias, gcode)}) AS cnt_{gcode}"
        for gcode, _, _ in applicable_genres
    ]
    select_cols = ",\n                ".join([f"COUNT(DISTINCT {alias}.id) AS total_all"] + filter_exprs)

    if media_type == "m":
        sql = f"""
            SELECT
                {select_cols}
            FROM movies m
            JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
            WHERE {reg_sql};
        """
    else:
        sql = f"""
            SELECT
                {select_cols}
            FROM series s
            JOIN seasons sea ON sea.series_id = s.id
            JOIN episodes e ON e.season_id = sea.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE {reg_sql};
        """

    with db_cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone() or {}

    genres = []
    for gcode, _, glabel in applicable_genres:
        cnt = int(row.get(f"cnt_{gcode}") or 0)
        if cnt > 0:
            genres.append({
                "code": gcode,
                "label": glabel,
                "count": cnt,
            })

    res = {
        "total": int(row.get("total_all") or 0),
        "genres": genres,
    }
    _cache_set(cache_key, res, ttl=MENU_CACHE_TTL_SECONDS)
    return res


def _movie_fame_order_sql(alias: str = "m") -> str:
    """
    Orders movies by Most Popular / Most Famous ('الأكثر شهرة'):
    combines live TMDB viral popularity, global audience vote reach, and weighted rating.
    """
    return (
        f"("
        f"COALESCE({alias}.popularity, 0) * 1.4 "
        f"+ LEAST(COALESCE({alias}.vote_count, 0), 30000) / 50.0 "
        f"+ CASE WHEN COALESCE({alias}.vote_count, 0) >= 30 "
        f"       THEN COALESCE({alias}.vote_average, 0) * 12.0 "
        f"       ELSE COALESCE({alias}.vote_average, 0) * 4.0 END"
        f") DESC, {alias}.id DESC"
    )


def _series_fame_order_sql(alias: str = "s") -> str:
    """
    Orders series by Most Popular / Most Famous ('الأكثر شهرة'):
    combines live TMDB popularity, audience vote reach, rating, and uploaded episode completeness.
    """
    return (
        f"("
        f"COALESCE({alias}.popularity, 0) * 1.5 "
        f"+ LEAST(COALESCE({alias}.vote_count, 0), 20000) / 40.0 "
        f"+ CASE WHEN COALESCE({alias}.vote_count, 0) >= 15 "
        f"       THEN COALESCE({alias}.vote_average, 0) * 12.0 "
        f"       ELSE COALESCE({alias}.vote_average, 0) * 5.0 END "
        f"+ LEAST(COUNT(DISTINCT ta.episode_id), 100) * 1.5"
        f") DESC, MAX(ta.created_at) DESC"
    )


def get_filtered_movies(region: str = "all", genre: str = "all", page: int = 1, per_page: int = 16) -> dict:
    gcode = normalize_genre_code(genre)
    cache_key = f"f_movies:{region}:{gcode}:{page}:{per_page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    offset = (page - 1) * per_page
    reg_sql = _build_region_sql("m", region)
    gen_sql = _build_genre_sql("m", gcode)
    order_sql = _movie_fame_order_sql("m")

    with db_cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(DISTINCT m.id) AS count
            FROM movies m
            JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
            WHERE ({reg_sql}) AND ({gen_sql});
        """)
        total_count = cur.fetchone()["count"]

        cur.execute(f"""
            SELECT
                m.id,
                m.title,
                m.poster_path,
                m.year,
                m.vote_average,
                m.overview,
                ROUND(ta.file_size_bytes / (1024.0 * 1024.0), 1) AS size_mb,
                ta.message_id,
                ta.channel_id,
                ta.telegram_file_id,
                ta.created_at
            FROM (
                SELECT DISTINCT ON (movie_id) *
                FROM telegram_archives
                WHERE movie_id IS NOT NULL AND file_size_bytes > 15000000
                ORDER BY movie_id, created_at DESC
            ) ta
            JOIN movies m ON m.id = ta.movie_id
            WHERE ({reg_sql}) AND ({gen_sql})
            ORDER BY {order_sql}
            LIMIT %s OFFSET %s;
        """, (per_page, offset))
        items = cur.fetchall()

    res = {
        "items": items,
        "total": total_count,
        "page": page,
        "total_pages": max(1, (total_count + per_page - 1) // per_page),
    }
    _cache_set(cache_key, res, ttl=MENU_CACHE_TTL_SECONDS)
    return res


def get_filtered_series(region: str = "all", genre: str = "all", page: int = 1, per_page: int = 16) -> dict:
    gcode = normalize_genre_code(genre)
    cache_key = f"f_series:{region}:{gcode}:{page}:{per_page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    offset = (page - 1) * per_page
    reg_sql = _build_region_sql("s", region)
    gen_sql = _build_genre_sql("s", gcode)
    s_order_sql = _series_fame_order_sql("s")

    with db_cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) AS count FROM (
                SELECT s.id
                FROM series s
                JOIN seasons sea ON sea.series_id = s.id
                JOIN episodes e ON e.season_id = sea.id
                JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                WHERE ({reg_sql}) AND ({gen_sql})
                GROUP BY s.id
            ) sub;
        """)
        total_count = cur.fetchone()["count"]

        cur.execute(f"""
            SELECT
                s.id,
                s.title,
                s.poster_path,
                s.year,
                s.vote_average,
                s.overview,
                COUNT(DISTINCT e.id) AS total_episodes,
                COUNT(DISTINCT ta.episode_id) AS uploaded_episodes,
                MAX(ta.created_at) AS latest_upload
            FROM series s
            JOIN seasons sea ON sea.series_id = s.id
            JOIN episodes e ON e.season_id = sea.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE ({reg_sql}) AND ({gen_sql})
            GROUP BY s.id, s.title, s.poster_path, s.year, s.overview, s.popularity, s.vote_count, s.vote_average
            ORDER BY {s_order_sql}
            LIMIT %s OFFSET %s;
        """, (per_page, offset))
        items = cur.fetchall()

    res = {
        "items": items,
        "total": total_count,
        "page": page,
        "total_pages": max(1, (total_count + per_page - 1) // per_page),
    }
    _cache_set(cache_key, res, ttl=MENU_CACHE_TTL_SECONDS)
    return res


def get_marvel_catalog() -> dict:
    """
    Builds the full '🦸 عالم مارفل' catalog from ready-to-stream vault items (one cached pass, 5 min):
      - franchises: [{key, label, movies:[...sorted by year]}] in MCU display order (non-empty only)
      - movies_chrono: every Marvel movie in release order
      - series: Marvel series in release order (with uploaded episode counts)
    """
    cache_key = "marvel_catalog"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    not_ad_m = f"NOT ({_is_adult_sql('m')})"
    not_ad_s = f"NOT ({_is_adult_sql('s')})"
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT m.id, m.title, m.original_title, m.year, m.release_date, m.popularity
            FROM movies m
            WHERE EXISTS (
                SELECT 1 FROM telegram_archives ta
                WHERE ta.movie_id = m.id AND ta.file_size_bytes > 15000000
            )
              AND {not_ad_m}
              AND (COALESCE(m.original_title, '') || ' ' || COALESCE(m.title, '')) ~* %s;
        """, (marvel.MARVEL_MOVIES_SQL_PREFILTER,))
        movie_rows = cur.fetchall()

        cur.execute(f"""
            SELECT s.id, s.title, s.original_title, s.year,
                   COUNT(DISTINCT ta.episode_id) AS uploaded_episodes
            FROM series s
            JOIN seasons sea ON sea.series_id = s.id
            JOIN episodes e ON e.season_id = sea.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE {not_ad_s}
              AND (COALESCE(s.original_title, '') || ' ' || COALESCE(s.title, '')) ~* %s
            GROUP BY s.id, s.title, s.original_title, s.year;
        """, (marvel.MARVEL_SERIES_SQL_PREFILTER,))
        series_rows = cur.fetchall()

    def _yr(r):
        return int(r.get("year") or 9999)

    def _release_key(r):
        """Exact release order: full release_date when known, else Jan 1st of the year (unknown last)."""
        rd = r.get("release_date")
        if rd:
            try:
                return (rd.year, rd.month, rd.day, r["id"])
            except AttributeError:
                pass
        return (_yr(r), 1, 1, r["id"])

    by_key: dict[str, list] = {}
    chrono = []
    for r in movie_rows:
        key = marvel.classify_movie(r)
        if not key:
            continue
        item = {
            "id": r["id"],
            "title": r["title"],
            "year": r.get("year") or (r["release_date"].year if r.get("release_date") else None),
            "release_date": r.get("release_date"),
            "franchise": key,
        }
        by_key.setdefault(key, []).append(item)
        chrono.append(item)

    franchises = []
    for key, label, _, _ in marvel.MARVEL_FRANCHISES:
        movies = sorted(by_key.get(key, []), key=_release_key)
        if movies:
            franchises.append({"key": key, "label": label, "movies": movies})
    chrono.sort(key=_release_key)

    series = []
    for r in series_rows:
        idx = marvel.classify_series(r)
        if idx is None:
            continue
        series.append({
            "id": r["id"],
            "title": r["title"],
            "year": r.get("year"),
            "order": idx,
            "uploaded_episodes": int(r.get("uploaded_episodes") or 0),
        })
    series.sort(key=lambda x: (x["order"], _yr(x), x["id"]))

    res = {"franchises": franchises, "movies_chrono": chrono, "series": series}
    _cache_set(cache_key, res, ttl=300.0)
    return res


def get_completed_series(page: int = 1, per_page: int = 24, category: str = None):
    """
    Returns series where at least 1 episode is archived in Telegram Vault with size > 15MB,
    ordered by Most Popular / Most Famous ('الأكثر شهرة').
    """
    cache_key = f"series:{category or 'all'}:{page}:{per_page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    offset = (page - 1) * per_page
    s_order_sql = _series_fame_order_sql("s")
    with db_cursor() as cur:
        if category == "asian":
            cur.execute("""
                SELECT COUNT(*) as count FROM (
                    SELECT s.id
                    FROM series s
                    JOIN seasons sea ON sea.series_id = s.id
                    JOIN episodes e ON e.season_id = sea.id
                    JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                    WHERE s.category IN ('kdrama', 'asian', 'anime')
                    GROUP BY s.id
                ) sub;
            """)
            total_count = cur.fetchone()["count"]

            cur.execute(f"""
                SELECT 
                    s.id, 
                    s.title, 
                    s.poster_path, 
                    s.year, 
                    s.overview,
                    COUNT(DISTINCT e.id) as total_episodes,
                    COUNT(DISTINCT ta.episode_id) as uploaded_episodes,
                    MAX(ta.created_at) as latest_upload
                FROM series s
                JOIN seasons sea ON sea.series_id = s.id
                JOIN episodes e ON e.season_id = sea.id
                JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                WHERE s.category IN ('kdrama', 'asian', 'anime')
                GROUP BY s.id, s.title, s.poster_path, s.year, s.overview, s.popularity, s.vote_count, s.vote_average
                ORDER BY {s_order_sql}
                LIMIT %s OFFSET %s;
            """, (per_page, offset))
            items = cur.fetchall()
        else:
            cur.execute("""
                SELECT COUNT(*) as count FROM (
                    SELECT s.id
                    FROM series s
                    JOIN seasons sea ON sea.series_id = s.id
                    JOIN episodes e ON e.season_id = sea.id
                    JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                    GROUP BY s.id
                ) sub;
            """)
            total_count = cur.fetchone()["count"]

            cur.execute(f"""
                SELECT 
                    s.id, 
                    s.title, 
                    s.poster_path, 
                    s.year, 
                    s.overview,
                    COUNT(DISTINCT e.id) as total_episodes,
                    COUNT(DISTINCT ta.episode_id) as uploaded_episodes,
                    MAX(ta.created_at) as latest_upload
                FROM series s
                JOIN seasons sea ON sea.series_id = s.id
                JOIN episodes e ON e.season_id = sea.id
                JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                GROUP BY s.id, s.title, s.poster_path, s.year, s.overview, s.popularity, s.vote_count, s.vote_average
                ORDER BY {s_order_sql}
                LIMIT %s OFFSET %s;
            """, (per_page, offset))
            items = cur.fetchall()

    res = {
        "items": items,
        "total": total_count,
        "page": page,
        "total_pages": max(1, (total_count + per_page - 1) // per_page)
    }
    _cache_set(cache_key, res)
    return res


get_available_series = get_completed_series


def get_series_details(series_id: int):
    cache_key = f"s_det:{series_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db_cursor() as cur:
        cur.execute("""
            SELECT 
                s.id, 
                s.title, 
                s.poster_path, 
                s.year, 
                s.vote_average,
                s.overview,
                COUNT(DISTINCT sea.id) as total_seasons,
                COUNT(DISTINCT e.id) as total_episodes,
                COUNT(DISTINCT ta.episode_id) as uploaded_episodes
            FROM series s
            LEFT JOIN seasons sea ON sea.series_id = s.id
            LEFT JOIN episodes e ON e.season_id = sea.id
            LEFT JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE s.id = %s
            GROUP BY s.id, s.title, s.poster_path, s.year, s.vote_average, s.overview;
        """, (series_id,))
        row = cur.fetchone()

    if row:
        _cache_set(cache_key, row)
    return row


def get_series_episodes(series_id: int, page: int = 1, per_page: int = 36):
    cache_key = f"s_eps:{series_id}:{page}:{per_page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    offset = (page - 1) * per_page
    with db_cursor() as cur:
        cur.execute("""
            SELECT COUNT(DISTINCT e.id) as count
            FROM episodes e
            JOIN seasons sea ON e.season_id = sea.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE sea.series_id = %s;
        """, (series_id,))
        total_count = cur.fetchone()["count"]

        cur.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (e.id)
                    e.id, 
                    sea.season_number, 
                    e.episode_number, 
                    e.title, 
                    ta.message_id, 
                    ta.channel_id, 
                    ta.file_size_bytes, 
                    ta.telegram_file_id
                FROM episodes e
                JOIN seasons sea ON e.season_id = sea.id
                JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                WHERE sea.series_id = %s
                ORDER BY e.id, ta.created_at DESC
            ) sub
            ORDER BY sub.season_number ASC, sub.episode_number ASC
            LIMIT %s OFFSET %s;
        """, (series_id, per_page, offset))
        episodes = cur.fetchall()

    res = {
        "episodes": episodes,
        "total": total_count,
        "page": page,
        "total_pages": max(1, (total_count + per_page - 1) // per_page)
    }
    _cache_set(cache_key, res)
    return res


def get_episode_archive(episode_id: int):
    cache_key = f"ep_arch:{episode_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db_cursor() as cur:
        cur.execute("""
            SELECT 
                e.id, 
                e.title as ep_title, 
                e.episode_number, 
                sea.season_number,
                s.id as series_id,
                s.title as series_title,
                ta.message_id, 
                ta.channel_id, 
                ta.telegram_file_id, 
                ta.file_size_bytes
            FROM episodes e
            JOIN seasons sea ON e.season_id = sea.id
            JOIN series s ON sea.series_id = s.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE e.id = %s
            ORDER BY ta.created_at DESC
            LIMIT 1;
        """, (episode_id,))
        row = cur.fetchone()

    if row:
        _cache_set(cache_key, row)
    return row


def get_next_episode(series_id: int, season_number: int, episode_number: int):
    """Fetches the next chronological archived episode in the series for 1-click binge watching."""
    cache_key = f"next_ep:{series_id}:{season_number}:{episode_number}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db_cursor() as cur:
        cur.execute("""
            SELECT 
                e.id, 
                e.episode_number, 
                sea.season_number,
                s.id as series_id,
                ta.message_id
            FROM episodes e
            JOIN seasons sea ON e.season_id = sea.id
            JOIN series s ON sea.series_id = s.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE s.id = %s 
              AND (sea.season_number > %s OR (sea.season_number = %s AND e.episode_number > %s))
            ORDER BY sea.season_number ASC, e.episode_number ASC
            LIMIT 1;
        """, (series_id, season_number, season_number, episode_number))
        row = cur.fetchone()

    if row:
        _cache_set(cache_key, row)
    return row


def _build_genre_where_clause(genre: str):
    """Returns (sql_condition, params_tuple) for flexible category matching (excludes +18 unless genre=='adult')."""
    if genre in ("adult", "ad"):
        return _is_adult_sql("m"), ()
    not_ad = f"NOT ({_is_adult_sql('m')})"
    if not genre or genre == "all":
        return not_ad, ()
    if genre == "Crime":
        return f"((m.genres::text ILIKE %s OR m.genres::text ILIKE %s) AND {not_ad})", ("%Crime%", "%Mystery%")
    if genre == "Adventure":
        return f"((m.genres::text ILIKE %s OR m.genres::text ILIKE %s OR m.genres::text ILIKE %s) AND {not_ad})", ("%Adventure%", "%History%", "%Western%")
    if genre == "Fantasy":
        return f"((m.genres::text ILIKE %s OR m.genres::text ILIKE %s) AND {not_ad})", ("%Fantasy%", "%Adventure%")
    if genre == "collections":
        return f"((m.title ~* '(28 |Chapter|Part|[0-9]$|Shazam|Prada|Freddy|Strangers)') AND {not_ad})", ()
    return f"((m.genres::text ILIKE %s) AND {not_ad})", (f"%{genre}%",)


def get_adult_vault_items(page: int = 1, per_page: int = 16) -> dict:
    """
    Returns ready-to-stream +18 ('للكبار فقط') movies & series exclusively for the Adult section.
    """
    cache_key = f"adult_vault:{page}:{per_page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    offset = (page - 1) * per_page
    ad_m = _is_adult_sql("m")
    ad_s = _is_adult_sql("s")
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT
                (SELECT COUNT(DISTINCT m.id)
                 FROM movies m
                 JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
                 WHERE ({ad_m}))
                +
                (SELECT COUNT(DISTINCT s.id)
                 FROM series s
                 JOIN seasons sea ON sea.series_id = s.id
                 JOIN episodes e ON e.season_id = sea.id
                 JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                 WHERE ({ad_s})) AS count;
        """)
        total_count = int((cur.fetchone() or {}).get("count") or 0)

        cur.execute(f"""
            SELECT * FROM (
                SELECT
                    m.id,
                    m.title,
                    m.year,
                    'movie' AS media_type,
                    COALESCE(m.popularity, 0) * 1.4 + COALESCE(m.vote_average, 0) * 8.0 AS score,
                    m.id AS sort_id
                FROM (
                    SELECT DISTINCT ON (movie_id) *
                    FROM telegram_archives
                    WHERE movie_id IS NOT NULL AND file_size_bytes > 15000000
                    ORDER BY movie_id, created_at DESC
                ) ta
                JOIN movies m ON m.id = ta.movie_id
                WHERE ({ad_m})

                UNION ALL

                SELECT
                    s.id,
                    s.title,
                    s.year,
                    'series' AS media_type,
                    COALESCE(s.popularity, 0) * 1.4 + COALESCE(s.vote_average, 0) * 8.0 AS score,
                    s.id AS sort_id
                FROM series s
                JOIN seasons sea ON sea.series_id = s.id
                JOIN episodes e ON e.season_id = sea.id
                JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                WHERE ({ad_s})
                GROUP BY s.id, s.title, s.year, s.popularity, s.vote_average
            ) combined
            ORDER BY combined.score DESC, combined.sort_id DESC
            LIMIT %s OFFSET %s;
        """, (per_page, offset))
        items = cur.fetchall()

    res = {
        "items": items,
        "total": total_count,
        "page": page,
        "total_pages": max(1, (total_count + per_page - 1) // per_page),
    }
    _cache_set(cache_key, res, ttl=MENU_CACHE_TTL_SECONDS)
    return res


def get_ready_movies(page: int = 1, per_page: int = 24, genre: str = None):
    cache_key = f"movies:{genre or 'all'}:{page}:{per_page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    offset = (page - 1) * per_page
    where_sql, where_params = _build_genre_where_clause(genre)
    m_order_sql = _movie_fame_order_sql("m")

    with db_cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(DISTINCT m.id) as count
            FROM movies m
            JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
            WHERE {where_sql};
        """, where_params)
        total_count = cur.fetchone()["count"]

        cur.execute(f"""
            SELECT 
                m.id, 
                m.title, 
                m.poster_path, 
                m.year, 
                m.vote_average,
                m.overview,
                ROUND(ta.file_size_bytes / (1024.0 * 1024.0), 1) as size_mb,
                ta.message_id, 
                ta.channel_id, 
                ta.telegram_file_id,
                ta.created_at
            FROM (
                SELECT DISTINCT ON (movie_id) *
                FROM telegram_archives
                WHERE movie_id IS NOT NULL AND file_size_bytes > 15000000
                ORDER BY movie_id, created_at DESC
            ) ta
            JOIN movies m ON m.id = ta.movie_id
            WHERE {where_sql}
            ORDER BY {m_order_sql}
            LIMIT %s OFFSET %s;
        """, where_params + (per_page, offset))
        items = cur.fetchall()

    res = {
        "items": items,
        "total": total_count,
        "page": page,
        "total_pages": max(1, (total_count + per_page - 1) // per_page)
    }
    _cache_set(cache_key, res)
    return res


def get_movie_details(movie_id: int):
    cache_key = f"m_det:{movie_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    ad_m = _is_adult_sql("m")
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT 
                m.id, 
                m.title, 
                m.poster_path, 
                m.year, 
                m.overview, 
                m.vote_average, 
                m.category,
                ({ad_m}) as is_adult,
                ROUND(ta.file_size_bytes / (1024.0 * 1024.0), 1) as size_mb,
                ta.message_id, 
                ta.channel_id, 
                ta.telegram_file_id
            FROM movies m
            JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
            WHERE m.id = %s
            ORDER BY ta.created_at DESC
            LIMIT 1;
        """, (movie_id,))
        row = cur.fetchone()

    if row:
        _cache_set(cache_key, row)
    return row


def get_next_episode(series_id: int, season_number: int, episode_number: int):
    """Fetches the next chronological archived episode in the series for 1-click binge watching."""
    cache_key = f"next_ep:{series_id}:{season_number}:{episode_number}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db_cursor() as cur:
        cur.execute("""
            SELECT 
                e.id, 
                e.episode_number, 
                sea.season_number,
                s.id as series_id,
                ta.message_id
            FROM episodes e
            JOIN seasons sea ON e.season_id = sea.id
            JOIN series s ON sea.series_id = s.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE s.id = %s 
              AND (sea.season_number > %s OR (sea.season_number = %s AND e.episode_number > %s))
            ORDER BY sea.season_number ASC, e.episode_number ASC
            LIMIT 1;
        """, (series_id, season_number, season_number, episode_number))
        row = cur.fetchone()

    if row:
        _cache_set(cache_key, row)
    return row


def search_vault(query: str, limit: int = 18):
    like_pat = f"%{query.strip()}%"
    m_order_sql = _movie_fame_order_sql("m")
    s_order_sql = _series_fame_order_sql("s")
    ad_m = _is_adult_sql("m")
    ad_s = _is_adult_sql("s")
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT 
                m.id, 
                m.title, 
                'movie' as type,
                m.year, 
                ({ad_m}) as is_adult,
                ROUND(MAX(ta.file_size_bytes) / (1024.0 * 1024.0), 1) as size_mb
            FROM movies m
            JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
            WHERE m.title ILIKE %s OR m.original_title ILIKE %s OR m.overview ILIKE %s
            GROUP BY m.id, m.title, m.original_title, m.overview, m.category, m.year, m.popularity, m.vote_count, m.vote_average
            ORDER BY {m_order_sql}
            LIMIT %s;
        """, (like_pat, like_pat, like_pat, limit))
        movie_matches = cur.fetchall()

        cur.execute(f"""
            SELECT 
                s.id, 
                s.title, 
                'series' as type,
                s.year, 
                ({ad_s}) as is_adult,
                COUNT(DISTINCT ta.episode_id) as total_episodes
            FROM series s
            JOIN seasons sea ON sea.series_id = s.id
            JOIN episodes e ON e.season_id = sea.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000
            WHERE s.title ILIKE %s OR s.original_title ILIKE %s OR s.overview ILIKE %s
            GROUP BY s.id, s.title, s.original_title, s.overview, s.category, s.year, s.popularity, s.vote_count, s.vote_average
            ORDER BY {s_order_sql}
            LIMIT %s;
        """, (like_pat, like_pat, like_pat, limit))
        series_matches = cur.fetchall()

    return movie_matches + series_matches


_force_sub_table_ready = False


def init_force_sub_table():
    """Creates and migrates required_channels, bot_users, bot_user_events, and bot_settings tables (once per process)."""
    global _force_sub_table_ready
    if _force_sub_table_ready:
        return
    try:
        with db_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS required_channels (
                    id SERIAL PRIMARY KEY,
                    chat_id VARCHAR(120) UNIQUE NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    invite_link VARCHAR(500) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(150),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    language_code VARCHAR(20),
                    is_active BOOLEAN DEFAULT TRUE,
                    status VARCHAR(30) DEFAULT 'active',
                    joined_at TIMESTAMP DEFAULT NOW(),
                    last_active_at TIMESTAMP DEFAULT NOW(),
                    left_at TIMESTAMP NULL,
                    rejoined_at TIMESTAMP NULL,
                    actions_count INTEGER DEFAULT 1,
                    last_action VARCHAR(255),
                    rules_status VARCHAR(30) DEFAULT 'unknown',
                    joined_channels_count INTEGER DEFAULT 0,
                    required_channels_count INTEGER DEFAULT 0,
                    joined_channels_json TEXT DEFAULT '[]',
                    rules_checked_at TIMESTAMP NULL,
                    force_sub_blocks_count INTEGER DEFAULT 0,
                    force_sub_passed_at TIMESTAMP NULL,
                    movies_watched_count INTEGER DEFAULT 0,
                    episodes_watched_count INTEGER DEFAULT 0,
                    searches_count INTEGER DEFAULT 0,
                    browses_count INTEGER DEFAULT 0
                );
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_name VARCHAR(255);
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS language_code VARCHAR(20);
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS status VARCHAR(30) DEFAULT 'active';
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS left_at TIMESTAMP NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS rejoined_at TIMESTAMP NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS actions_count INTEGER DEFAULT 1;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_action VARCHAR(255);
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS rules_status VARCHAR(30) DEFAULT 'unknown';
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS joined_channels_count INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS required_channels_count INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS joined_channels_json TEXT DEFAULT '[]';
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS rules_checked_at TIMESTAMP NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS force_sub_blocks_count INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS force_sub_passed_at TIMESTAMP NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS movies_watched_count INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS episodes_watched_count INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS searches_count INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS browses_count INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS total_referrals_entered INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS qualified_referrals_count INTEGER DEFAULT 0;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_by BIGINT NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_via_code VARCHAR(120) NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_target_type VARCHAR(30) NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_target_id INTEGER NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_target_title VARCHAR(300) NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referral_qualified BOOLEAN DEFAULT FALSE;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_at TIMESTAMP NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referral_qualified_at TIMESTAMP NULL;
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS entry_source VARCHAR(30) NULL;

                CREATE TABLE IF NOT EXISTS affiliate_referrals (
                    id SERIAL PRIMARY KEY,
                    referrer_user_id BIGINT NOT NULL,
                    referred_user_id BIGINT UNIQUE NOT NULL,
                    referral_code VARCHAR(120) NOT NULL,
                    target_type VARCHAR(30) NOT NULL DEFAULT 'bot',
                    target_id INTEGER NULL,
                    target_title VARCHAR(300) NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    points_awarded INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    qualified_at TIMESTAMP NULL
                );
                CREATE INDEX IF NOT EXISTS idx_aff_ref_referrer ON affiliate_referrals(referrer_user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_aff_ref_status ON affiliate_referrals(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_bot_users_points ON bot_users(points DESC);
                CREATE INDEX IF NOT EXISTS idx_bot_users_referred_by ON bot_users(referred_by);

                CREATE TABLE IF NOT EXISTS affiliate_point_transactions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    delta INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL DEFAULT 0,
                    reason VARCHAR(300) NOT NULL,
                    related_user_id BIGINT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_aff_pt_tx_uid ON affiliate_point_transactions(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS bot_user_events (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username VARCHAR(150),
                    first_name VARCHAR(255),
                    event_type VARCHAR(40) NOT NULL,
                    details VARCHAR(500),
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_bot_user_events_created_at ON bot_user_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_bot_user_events_type ON bot_user_events(event_type);
                CREATE INDEX IF NOT EXISTS idx_bot_user_events_uid ON bot_user_events(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_bot_users_is_active ON bot_users(is_active);
                CREATE INDEX IF NOT EXISTS idx_bot_users_last_active ON bot_users(last_active_at DESC);

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                INSERT INTO bot_settings (key, value)
                VALUES ('notify_admin_events', '1')
                ON CONFLICT (key) DO NOTHING;

                CREATE TABLE IF NOT EXISTS uploader_bots (
                    id SERIAL PRIMARY KEY,
                    bot_token VARCHAR(200) UNIQUE NOT NULL,
                    bot_id BIGINT,
                    bot_username VARCHAR(150),
                    bot_name VARCHAR(255),
                    is_active BOOLEAN DEFAULT TRUE,
                    channel_ok BOOLEAN DEFAULT FALSE,
                    uploads_count INTEGER DEFAULT 0,
                    total_uploaded_bytes BIGINT DEFAULT 0,
                    last_used_at TIMESTAMP NULL,
                    last_error TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );

                UPDATE bot_users
                SET entry_source = 'affiliate'
                WHERE referred_by IS NOT NULL AND entry_source IS DISTINCT FROM 'affiliate';

                WITH first_instant_watch AS (
                    SELECT DISTINCT ON (u.user_id)
                        u.user_id,
                        e.event_type,
                        TRIM(REGEXP_REPLACE(COALESCE(e.details, ''), '^(🎬\\s*شاهد فيلم:\\s*|📺\\s*شاهد:\\s*)', '')) AS raw_title
                    FROM bot_users u
                    JOIN bot_user_events e
                      ON e.user_id = u.user_id
                     AND e.event_type IN ('watch_movie', 'watch_episode')
                     AND e.created_at BETWEEN u.joined_at - INTERVAL '2 seconds' AND u.joined_at + INTERVAL '6 seconds'
                    WHERE u.referred_by IS NULL AND u.entry_source IS NULL
                    ORDER BY u.user_id, e.created_at ASC
                )
                UPDATE bot_users u
                SET entry_source = 'direct_link',
                    referred_target_type = CASE WHEN fw.event_type = 'watch_movie' THEN 'movie' ELSE 'episode' END,
                    referred_target_title = CASE
                        WHEN fw.event_type = 'watch_movie' THEN LEFT('فيلم ' || fw.raw_title, 295)
                        ELSE LEFT('مسلسل ' || fw.raw_title, 295)
                    END,
                    referred_via_code = COALESCE(
                        (
                            SELECT 'm_' || m.id::text
                            FROM movies m
                            WHERE fw.event_type = 'watch_movie'
                              AND LOWER(m.title) = LOWER(fw.raw_title)
                            ORDER BY m.id DESC
                            LIMIT 1
                        ),
                        'direct_link'
                    )
                FROM first_instant_watch fw
                WHERE u.user_id = fw.user_id;

                UPDATE bot_users
                SET entry_source = 'organic',
                    referred_target_title = COALESCE(referred_target_title, 'دخول مباشر للبوت (/start)')
                WHERE referred_by IS NULL AND entry_source IS NULL;
            """)
        _force_sub_table_ready = True
    except Exception as e:
        logger.warning(f"Could not init bot tables: {e}")


def get_bot_setting(key: str, default: str = "1") -> str:
    cache_key = f"bot_setting:{key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        with db_cursor() as cur:
            cur.execute("SELECT value FROM bot_settings WHERE key = %s;", (key,))
            row = cur.fetchone()
            val = row["value"] if row else default
        _cache_set(cache_key, val, ttl=15.0)
        return val
    except Exception:
        return default


def set_bot_setting(key: str, value: str) -> str:
    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO bot_settings (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW();
            """, (key, str(value)))
        with _cache_lock:
            _cache.pop(f"bot_setting:{key}", None)
    except Exception as e:
        logger.warning(f"Error saving bot setting {key}: {e}")
    return str(value)


def log_bot_user_event(
    user_id: int,
    event_type: str,
    details: str = None,
    username: str = None,
    first_name: str = None
):
    """Records a user lifecycle or activity event into bot_user_events."""
    try:
        with db_cursor() as cur:
            if username is None and first_name is None:
                cur.execute("SELECT username, first_name FROM bot_users WHERE user_id = %s;", (user_id,))
                u = cur.fetchone()
                if u:
                    username = u.get("username")
                    first_name = u.get("first_name")
            cur.execute("""
                INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW());
            """, (user_id, username, first_name, event_type, (details or "")[:490]))
            if action_label := (details or ""):
                cur.execute("""
                    UPDATE bot_users
                    SET last_active_at = NOW(),
                        actions_count = COALESCE(actions_count, 0) + 1,
                        last_action = %s
                    WHERE user_id = %s;
                """, (action_label[:250], user_id))
    except Exception as e:
        logger.warning(f"Error logging bot user event ({event_type}) for {user_id}: {e}")


def update_user_rules_status(
    user_id: int,
    all_channels: list[dict],
    missing_channels: list[dict],
    was_blocked_prompt: bool = False,
    clicked_verify: bool = False,
    username: str = None,
    first_name: str = None
) -> str:
    """
    Persists exact Force-Subscribe compliance state and per-channel membership for a user.
    Returns the new rules_status ('compliant', 'partial', 'unsubscribed', 'left_channel', 'no_rules').
    """
    total_req = len(all_channels or [])
    missing_ids = {str(c.get("chat_id")) for c in (missing_channels or [])}
    ch_details = [
        {
            "chat_id": str(c.get("chat_id")),
            "title": c.get("title") or "",
            "invite_link": c.get("invite_link") or "",
            "joined": str(c.get("chat_id")) not in missing_ids,
        }
        for c in (all_channels or [])
    ]
    joined_cnt = sum(1 for c in ch_details if c["joined"])
    ch_json_str = json.dumps(ch_details, ensure_ascii=False)

    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT username, first_name, rules_status, force_sub_passed_at, force_sub_blocks_count
                FROM bot_users WHERE user_id = %s;
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                cur.execute("""
                    INSERT INTO bot_users (user_id, username, first_name, is_active, status, joined_at, last_active_at, actions_count, last_action)
                    VALUES (%s, %s, %s, TRUE, 'active', NOW(), NOW(), 1, '🚀 دخل البوت')
                    ON CONFLICT (user_id) DO NOTHING;
                """, (user_id, username, first_name))
                prev_status = "unknown"
                passed_at = None
            else:
                username = username or row.get("username")
                first_name = first_name or row.get("first_name")
                prev_status = row.get("rules_status") or "unknown"
                passed_at = row.get("force_sub_passed_at")

            if total_req == 0:
                new_status = "no_rules"
                cur.execute("""
                    UPDATE bot_users
                    SET rules_status = 'no_rules',
                        joined_channels_count = 0,
                        required_channels_count = 0,
                        joined_channels_json = '[]',
                        rules_checked_at = NOW()
                    WHERE user_id = %s;
                """, (user_id,))
                return new_status

            if len(missing_ids) == 0:
                new_status = "compliant"
                cur.execute("""
                    UPDATE bot_users
                    SET rules_status = 'compliant',
                        joined_channels_count = %s,
                        required_channels_count = %s,
                        joined_channels_json = %s,
                        rules_checked_at = NOW(),
                        force_sub_passed_at = COALESCE(force_sub_passed_at, NOW())
                    WHERE user_id = %s;
                """, (joined_cnt, total_req, ch_json_str, user_id))

                if clicked_verify or prev_status in ("unsubscribed", "partial", "left_channel"):
                    det = f"✅ التزم بالشروط وأتم الاشتراك في جميع الجروبات ({joined_cnt}/{total_req})"
                    cur.execute("""
                        INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                        VALUES (%s, %s, %s, 'rules_passed', %s, NOW());
                    """, (user_id, username, first_name, det))
                    cur.execute("""
                        UPDATE bot_users SET last_action = %s WHERE user_id = %s;
                    """, (det[:250], user_id))
            else:
                if passed_at is not None or prev_status == "compliant":
                    new_status = "left_channel"
                elif joined_cnt > 0:
                    new_status = "partial"
                else:
                    new_status = "unsubscribed"

                inc_block = 1 if (was_blocked_prompt or clicked_verify) else 0
                cur.execute("""
                    UPDATE bot_users
                    SET rules_status = %s,
                        joined_channels_count = %s,
                        required_channels_count = %s,
                        joined_channels_json = %s,
                        rules_checked_at = NOW(),
                        force_sub_blocks_count = COALESCE(force_sub_blocks_count, 0) + %s
                    WHERE user_id = %s;
                """, (new_status, joined_cnt, total_req, ch_json_str, inc_block, user_id))

                missing_titles = "، ".join((c.get("title") or "") for c in missing_channels[:3])
                if new_status == "left_channel" and prev_status == "compliant":
                    det = f"🚪 غادر جروب إجباري بعد التفعيل (ناقص: {missing_titles})"
                    cur.execute("""
                        INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                        VALUES (%s, %s, %s, 'rules_left_channel', %s, NOW());
                    """, (user_id, username, first_name, det[:490]))
                    cur.execute("UPDATE bot_users SET last_action = %s WHERE user_id = %s;", (det[:250], user_id))
                elif clicked_verify:
                    det = f"❌ ضغط تحقق من الاشتراك ولم يكمل بعد ({joined_cnt}/{total_req} — ناقص: {missing_titles})"
                    cur.execute("""
                        INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                        VALUES (%s, %s, %s, 'rules_failed', %s, NOW());
                    """, (user_id, username, first_name, det[:490]))
                    cur.execute("UPDATE bot_users SET last_action = %s WHERE user_id = %s;", (det[:250], user_id))
                elif was_blocked_prompt:
                    throttle_key = f"fsub_shown_evt:{user_id}"
                    det = f"⚠️ توقف عند شاشة الشروط ({joined_cnt}/{total_req} جروبات)"
                    cur.execute("UPDATE bot_users SET last_action = %s WHERE user_id = %s;", (det[:250], user_id))
                    if not _cache_get(throttle_key):
                        _cache_set(throttle_key, True, ttl=120.0)
                        cur.execute("""
                            INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                            VALUES (%s, %s, %s, 'rules_shown', %s, NOW());
                        """, (user_id, username, first_name, f"⚠️ ظهرت له شاشة الشروط — مشترك في {joined_cnt} من {total_req}"))
            return new_status
    except Exception as e:
        logger.warning(f"Error updating rules status for {user_id}: {e}")
        return "unknown"


_B36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def int_to_base36(num: int) -> str:
    n = abs(int(num))
    if n == 0:
        return "0"
    chars = []
    while n > 0:
        n, rem = divmod(n, 36)
        chars.append(_B36_ALPHABET[rem])
    return "".join(reversed(chars))


def base36_to_int(s: str) -> int | None:
    val = (s or "").strip().lower()
    if not val or not val.isalnum():
        return None
    try:
        return int(val, 36)
    except ValueError:
        return None


def encode_affiliate_code(
    target_type: str,
    target_id: int | None = None,
    referrer_id: int | None = None,
) -> str:
    """
    Produces a compact, deterministic URL slug for /r/{code}:
      - Movie:   m-{movie_id}-{b36_uid}
      - Series:  s-{series_id}-{b36_uid}
      - Episode: e-{ep_id}-{b36_uid}
      - Bot:     u-{b36_uid}
    """
    prefix_map = {"movie": "m", "series": "s", "episode": "e", "bot": "u"}
    p = prefix_map.get(target_type, "u")
    b36 = int_to_base36(int(referrer_id)) if referrer_id and int(referrer_id) > 0 else None
    if p == "u":
        return f"u-{b36}" if b36 else "u"
    tid = int(target_id or 0)
    return f"{p}-{tid}-{b36}" if b36 else f"{p}-{tid}"


def decode_affiliate_code(code: str) -> dict:
    """
    Decodes a /r/{code} slug into:
      {target_type, target_id, referrer_id, tg_start, referral_code}
    """
    raw = (code or "").strip()
    if not raw:
        return {"target_type": "bot", "target_id": None, "referrer_id": None, "tg_start": "", "referral_code": "u"}

    # Also support direct start-style codes if hit via /r/m_123_r_456
    if "_" in raw and "-" not in raw:
        parsed = parse_start_payload(raw)
        ref_id = parsed.get("referrer_id")
        ttype = parsed.get("target_type") or "bot"
        tid = parsed.get("target_id")
        return {
            "target_type": ttype,
            "target_id": tid,
            "referrer_id": ref_id,
            "tg_start": raw,
            "referral_code": parsed.get("referral_code") or raw,
        }

    parts = raw.split("-")
    prefix = parts[0].lower()

    if prefix == "u":
        ref_id = base36_to_int(parts[1]) if len(parts) >= 2 else None
        tg_start = f"ref_{ref_id}" if ref_id else ""
        return {
            "target_type": "bot",
            "target_id": None,
            "referrer_id": ref_id,
            "tg_start": tg_start,
            "referral_code": encode_affiliate_code("bot", None, ref_id),
        }

    type_map = {"m": ("movie", "m"), "s": ("series", "s"), "e": ("episode", "ep")}
    if prefix in type_map and len(parts) >= 2 and parts[1].isdigit():
        ttype, tg_pref = type_map[prefix]
        tid = int(parts[1])
        ref_id = base36_to_int(parts[2]) if len(parts) >= 3 else None
        tg_start = f"{tg_pref}_{tid}_r_{ref_id}" if ref_id else f"{tg_pref}_{tid}"
        return {
            "target_type": ttype,
            "target_id": tid,
            "referrer_id": ref_id,
            "tg_start": tg_start,
            "referral_code": encode_affiliate_code(ttype, tid, ref_id),
        }

    return {"target_type": "bot", "target_id": None, "referrer_id": None, "tg_start": "", "referral_code": raw[:80]}


def parse_start_payload(deep_arg: str) -> dict:
    """
    Parses Telegram /start <deep_arg> into:
      {base_arg, target_type, target_id, referrer_id, referral_code}
    Supports:
      - m_123_r_8511689363 / m_123
      - s_45_r_8511689363  / s_45
      - ep_678_r_8511689363 / ep_678
      - ref_8511689363
      - m-123-3wr96n (fallback if slug passed directly)
    """
    arg = (deep_arg or "").strip()
    if not arg:
        return {"base_arg": None, "target_type": None, "target_id": None, "referrer_id": None, "referral_code": None}

    if "-" in arg and "_" not in arg:
        dec = decode_affiliate_code(arg)
        ttype = dec.get("target_type")
        tid = dec.get("target_id")
        base_map = {"movie": f"m_{tid}", "series": f"s_{tid}", "episode": f"ep_{tid}"}
        return {
            "base_arg": base_map.get(ttype),
            "target_type": ttype,
            "target_id": tid,
            "referrer_id": dec.get("referrer_id"),
            "referral_code": dec.get("referral_code"),
        }

    m_ref = re.match(r"^(m|s|ep)_(\d+)(?:_r_(\d+))?$", arg)
    if m_ref:
        pref, tid_s, ref_s = m_ref.group(1), m_ref.group(2), m_ref.group(3)
        tid = int(tid_s)
        ref_id = int(ref_s) if ref_s else None
        ttype = {"m": "movie", "s": "series", "ep": "episode"}[pref]
        return {
            "base_arg": f"{pref}_{tid}",
            "target_type": ttype,
            "target_id": tid,
            "referrer_id": ref_id,
            "referral_code": encode_affiliate_code(ttype, tid, ref_id) if ref_id else f"{pref}-{tid}",
        }

    u_ref = re.match(r"^ref_(\d+)$", arg)
    if u_ref:
        ref_id = int(u_ref.group(1))
        return {
            "base_arg": None,
            "target_type": "bot",
            "target_id": None,
            "referrer_id": ref_id,
            "referral_code": encode_affiliate_code("bot", None, ref_id),
        }

    return {"base_arg": arg, "target_type": None, "target_id": None, "referrer_id": None, "referral_code": None}


def _resolve_target_title_cur(cur, target_type: str | None, target_id: int | None) -> str:
    if target_type == "movie" and target_id:
        cur.execute("SELECT title, year FROM movies WHERE id = %s;", (int(target_id),))
        r = cur.fetchone()
        if r and r.get("title"):
            t = clean_display_title(r["title"])
            yr = f" ({r['year']})" if r.get("year") else ""
            return f"فيلم {t}{yr}"
        return f"فيلم #{target_id}"
    if target_type == "series" and target_id:
        cur.execute("SELECT title, year FROM series WHERE id = %s;", (int(target_id),))
        r = cur.fetchone()
        if r and r.get("title"):
            t = clean_display_title(r["title"])
            yr = f" ({r['year']})" if r.get("year") else ""
            return f"مسلسل {t}{yr}"
        return f"مسلسل #{target_id}"
    if target_type == "episode" and target_id:
        cur.execute("""
            SELECT s.title AS series_title, sea.season_number, e.episode_number
            FROM episodes e
            JOIN seasons sea ON sea.id = e.season_id
            JOIN series s ON s.id = sea.series_id
            WHERE e.id = %s;
        """, (int(target_id),))
        r = cur.fetchone()
        if r and r.get("series_title"):
            t = clean_display_title(r["series_title"])
            return f"مسلسل {t} — م{r['season_number']} ح{r['episode_number']}"
        return f"حلقة #{target_id}"
    return "رابط الدعوة العام للبوت"


def _qualify_pending_referral_in_tx(cur, user_id: int, trigger_label: str = "استخدم البوت") -> dict | None:
    """
    Atomically transitions a referred user's status from 'pending' -> 'qualified'
    upon actual bot usage and awards +1 point to their referrer exactly once.
    """
    cur.execute("""
        UPDATE affiliate_referrals
        SET status = 'qualified',
            points_awarded = 1,
            qualified_at = NOW()
        WHERE referred_user_id = %s AND status = 'pending'
        RETURNING referrer_user_id, referral_code, target_type, target_id, target_title;
    """, (user_id,))
    q_row = cur.fetchone()
    if not q_row:
        return None

    referrer_id = int(q_row["referrer_user_id"])
    target_title = q_row.get("target_title") or "رابط مشاركة"

    cur.execute("""
        UPDATE bot_users
        SET referral_qualified = TRUE,
            referral_qualified_at = NOW()
        WHERE user_id = %s
        RETURNING username, first_name, last_name;
    """, (user_id,))
    referred_u = cur.fetchone() or {}

    # Ensure referrer exists in bot_users, then award +1 point & +1 qualified count
    cur.execute("""
        INSERT INTO bot_users (user_id, first_name, is_active, status, points, total_referrals_entered, qualified_referrals_count)
        VALUES (%s, 'مسوق أفلييت', TRUE, 'active', 0, 1, 0)
        ON CONFLICT (user_id) DO NOTHING;
    """, (referrer_id,))

    cur.execute("""
        UPDATE bot_users
        SET points = COALESCE(points, 0) + 1,
            qualified_referrals_count = COALESCE(qualified_referrals_count, 0) + 1
        WHERE user_id = %s
        RETURNING points, qualified_referrals_count, total_referrals_entered, username, first_name;
    """, (referrer_id,))
    ref_u = cur.fetchone() or {}
    new_points = int(ref_u.get("points") or 1)

    referred_name = f"{referred_u.get('first_name') or ''} {referred_u.get('last_name') or ''}".strip() or f"User {user_id}"
    reason_txt = f"+1 نقطة لدخول واستخدام «{referred_name}» عبر ({target_title})"[:295]

    cur.execute("""
        INSERT INTO affiliate_point_transactions (user_id, delta, balance_after, reason, related_user_id, created_at)
        VALUES (%s, 1, %s, %s, %s, NOW());
    """, (referrer_id, new_points, reason_txt, user_id))

    cur.execute("""
        INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
        VALUES (%s, %s, %s, 'affiliate_qualified', %s, NOW());
    """, (
        referrer_id,
        ref_u.get("username"),
        ref_u.get("first_name"),
        f"🏆 حصل على +1 نقطة (الرصيد: {new_points}) — دخل واستخدم البوت: {referred_name} عبر «{target_title}»"[:490],
    ))

    return {
        "referrer_id": referrer_id,
        "referrer_username": ref_u.get("username"),
        "referrer_first_name": ref_u.get("first_name"),
        "new_points": new_points,
        "qualified_count": int(ref_u.get("qualified_referrals_count") or 1),
        "referred_user_id": user_id,
        "referred_name": referred_name,
        "referred_username": referred_u.get("username"),
        "target_title": target_title,
        "referral_code": q_row.get("referral_code"),
    }


def qualify_referral_if_pending(user_id: int, trigger_label: str = "استخدم البوت") -> dict | None:
    """Public wrapper to qualify a pending referral when a user passes Force-Sub or uses the bot."""
    try:
        with db_cursor() as cur:
            return _qualify_pending_referral_in_tx(cur, user_id, trigger_label)
    except Exception as e:
        logger.warning(f"Error qualifying referral for {user_id}: {e}")
        return None


def register_bot_user(
    user_id: int,
    username: str = None,
    first_name: str = None,
    last_name: str = None,
    language_code: str = None,
    action_text: str = None,
    event_type: str = None,
    referral_info: dict = None,
) -> dict:
    """
    Saves or updates a user in bot_users, increments typed interaction counters,
    handles atomic affiliate referral entry attribution & usage qualification,
    and tracks join/rejoin/activity events in real time.
    """
    cache_key = f"user_reg:{user_id}"
    if _cache_get(cache_key) and not action_text and not event_type and not referral_info:
        return {"is_new": False, "rejoined": False, "affiliate_entry": None, "affiliate_qualified": None}

    # Throttle identical browse events within 25 seconds so pagination clicks don't spam timeline
    if event_type == "browse" and action_text and not referral_info:
        b_key = f"browse_evt:{user_id}:{action_text}"
        if _cache_get(b_key):
            # Still allow pending referral qualification on first browse even if cached
            q_info = qualify_referral_if_pending(user_id, action_text)
            return {"is_new": False, "rejoined": False, "affiliate_entry": None, "affiliate_qualified": q_info}
        _cache_set(b_key, True, ttl=25.0)

    inc_movie = 1 if event_type == "watch_movie" else 0
    inc_ep = 1 if event_type == "watch_episode" else 0
    inc_search = 1 if event_type == "search" else 0
    inc_browse = 1 if event_type == "browse" else 0

    is_new = False
    rejoined = False
    affiliate_entry = None
    affiliate_qualified = None
    entry_info = None

    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT
                    is_active, status, referred_by, entry_source,
                    COALESCE(movies_watched_count, 0) AS movies_watched_count,
                    COALESCE(episodes_watched_count, 0) AS episodes_watched_count,
                    (joined_at >= NOW() - INTERVAL '90 seconds') AS joined_just_now
                FROM bot_users
                WHERE user_id = %s;
            """, (user_id,))
            existing = cur.fetchone()

            if not existing:
                act = (action_text or "🚀 انضم وفتح البوت")[:250]
                # ON CONFLICT: two concurrent updates from a brand-new user must not raise a PK violation
                cur.execute("""
                    INSERT INTO bot_users (
                        user_id, username, first_name, last_name, language_code,
                        is_active, status, joined_at, last_active_at, actions_count, last_action,
                        movies_watched_count, episodes_watched_count, searches_count, browses_count
                    )
                    VALUES (%s, %s, %s, %s, %s, TRUE, 'active', NOW(), NOW(), 1, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO NOTHING
                    RETURNING user_id;
                """, (user_id, username, first_name, last_name, language_code, act, inc_movie, inc_ep, inc_search, inc_browse))
                is_new = cur.fetchone() is not None
            if not existing and is_new:
                cur.execute("""
                    INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                    VALUES (%s, %s, %s, 'joined', %s, NOW());
                """, (user_id, username, first_name, "انضم للبوت لأول مرة"))
                if event_type:
                    cur.execute("""
                        INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                        VALUES (%s, %s, %s, %s, %s, NOW());
                    """, (user_id, username, first_name, event_type, (action_text or "")[:490]))
            elif not existing:
                pass  # lost the insert race to a concurrent update; that call already logged the join
            elif not existing["is_active"]:
                rejoined = True
                act = (action_text or "🔄 عاد لتشغيل البوت")[:250]
                cur.execute("""
                    UPDATE bot_users
                    SET username = COALESCE(%s, username),
                        first_name = COALESCE(%s, first_name),
                        last_name = COALESCE(%s, last_name),
                        language_code = COALESCE(%s, language_code),
                        is_active = TRUE,
                        status = 'active',
                        rejoined_at = NOW(),
                        left_at = NULL,
                        last_active_at = NOW(),
                        actions_count = COALESCE(actions_count, 0) + 1,
                        movies_watched_count = COALESCE(movies_watched_count, 0) + %s,
                        episodes_watched_count = COALESCE(episodes_watched_count, 0) + %s,
                        searches_count = COALESCE(searches_count, 0) + %s,
                        browses_count = COALESCE(browses_count, 0) + %s,
                        last_action = %s
                    WHERE user_id = %s;
                """, (username, first_name, last_name, language_code, inc_movie, inc_ep, inc_search, inc_browse, act, user_id))
                cur.execute("""
                    INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                    VALUES (%s, %s, %s, 'rejoined', %s, NOW());
                """, (user_id, username, first_name, "فك الحظر وعاد لتشغيل البوت"))
                if event_type:
                    cur.execute("""
                        INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                        VALUES (%s, %s, %s, %s, %s, NOW());
                    """, (user_id, username, first_name, event_type, (action_text or "")[:490]))
            else:
                if action_text:
                    cur.execute("""
                        UPDATE bot_users
                        SET username = COALESCE(%s, username),
                            first_name = COALESCE(%s, first_name),
                            last_name = COALESCE(%s, last_name),
                            language_code = COALESCE(%s, language_code),
                            is_active = TRUE,
                            status = 'active',
                            last_active_at = NOW(),
                            actions_count = COALESCE(actions_count, 0) + 1,
                            movies_watched_count = COALESCE(movies_watched_count, 0) + %s,
                            episodes_watched_count = COALESCE(episodes_watched_count, 0) + %s,
                            searches_count = COALESCE(searches_count, 0) + %s,
                            browses_count = COALESCE(browses_count, 0) + %s,
                            last_action = %s
                        WHERE user_id = %s;
                    """, (username, first_name, last_name, language_code, inc_movie, inc_ep, inc_search, inc_browse, action_text[:250], user_id))
                else:
                    cur.execute("""
                        UPDATE bot_users
                        SET username = COALESCE(%s, username),
                            first_name = COALESCE(%s, first_name),
                            last_name = COALESCE(%s, last_name),
                            language_code = COALESCE(%s, language_code),
                            is_active = TRUE,
                            status = 'active',
                            last_active_at = NOW(),
                            actions_count = COALESCE(actions_count, 0) + 1
                        WHERE user_id = %s;
                    """, (username, first_name, last_name, language_code, user_id))

                if event_type:
                    cur.execute("""
                        INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                        VALUES (%s, %s, %s, %s, %s, NOW());
                    """, (user_id, username, first_name, event_type, (action_text or "")[:490]))

            # Atomic Affiliate Entry Attribution (strictly 1 person = max 1 attribution, never self-referral)
            ref_id_raw = (referral_info or {}).get("referrer_id")
            if ref_id_raw:
                ref_id = int(ref_id_raw)
                prior_watches = (
                    int((existing or {}).get("movies_watched_count") or 0)
                    + int((existing or {}).get("episodes_watched_count") or 0)
                )
                eligible_new = (
                    is_new
                    or rejoined
                    or bool(
                        existing
                        and existing.get("referred_by") is None
                        and (existing.get("joined_just_now") or prior_watches == 0)
                    )
                )
                if ref_id > 0 and ref_id != user_id and eligible_new:
                    ttype = (referral_info.get("target_type") or "bot")[:30]
                    tid = referral_info.get("target_id")
                    rcode = (referral_info.get("referral_code") or encode_affiliate_code(ttype, tid, ref_id))[:120]
                    ttitle = _resolve_target_title_cur(cur, ttype, tid)[:295]

                    cur.execute("""
                        INSERT INTO affiliate_referrals (
                            referrer_user_id, referred_user_id, referral_code,
                            target_type, target_id, target_title, status, points_awarded, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, NOW())
                        ON CONFLICT (referred_user_id) DO NOTHING
                        RETURNING id;
                    """, (ref_id, user_id, rcode, ttype, tid, ttitle))
                    ins_ref = cur.fetchone()
                    if ins_ref:
                        cur.execute("""
                            UPDATE bot_users
                            SET entry_source = 'affiliate',
                                referred_by = %s,
                                referred_via_code = %s,
                                referred_target_type = %s,
                                referred_target_id = %s,
                                referred_target_title = %s,
                                referred_at = NOW()
                            WHERE user_id = %s;
                        """, (ref_id, rcode, ttype, tid, ttitle, user_id))

                        cur.execute("""
                            INSERT INTO bot_users (user_id, first_name, is_active, status, points, total_referrals_entered, qualified_referrals_count)
                            VALUES (%s, 'مسوق أفلييت', TRUE, 'active', 0, 0, 0)
                            ON CONFLICT (user_id) DO NOTHING;
                        """, (ref_id,))

                        cur.execute("""
                            UPDATE bot_users
                            SET total_referrals_entered = COALESCE(total_referrals_entered, 0) + 1
                            WHERE user_id = %s
                            RETURNING username, first_name, points, total_referrals_entered;
                        """, (ref_id,))
                        ref_owner = cur.fetchone() or {}
                        ref_owner_lbl = (
                            f"@{ref_owner['username']}"
                            if ref_owner.get("username")
                            else (ref_owner.get("first_name") or str(ref_id))
                        )
                        cur.execute("""
                            INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                            VALUES (%s, %s, %s, 'affiliate_entered', %s, NOW());
                        """, (
                            user_id,
                            username,
                            first_name,
                            f"🔗 دخل عبر رابط أفلييت ({rcode}) تبع {ref_owner_lbl} — الرابط: {ttitle}"[:490],
                        ))
                        affiliate_entry = {
                            "referrer_id": ref_id,
                            "referrer_label": ref_owner_lbl,
                            "referral_code": rcode,
                            "target_title": ttitle,
                        }
                        entry_info = {
                            "entry_source": "affiliate",
                            "target_title": ttitle,
                            "referral_code": rcode,
                            "referrer_label": ref_owner_lbl,
                        }
            elif referral_info is not None and (
                is_new or (existing and existing.get("referred_by") is None and not existing.get("entry_source"))
            ):
                ttype = referral_info.get("target_type")
                tid = referral_info.get("target_id")
                raw_arg = (referral_info.get("raw_arg") or referral_info.get("referral_code") or referral_info.get("base_arg") or "")[:120]
                if ttype in ("movie", "series", "episode") and tid:
                    ttitle = _resolve_target_title_cur(cur, ttype, tid)[:295]
                    rcode = (raw_arg or f"{ttype}_{tid}")[:120]
                    cur.execute("""
                        UPDATE bot_users
                        SET entry_source = 'direct_link',
                            referred_via_code = %s,
                            referred_target_type = %s,
                            referred_target_id = %s,
                            referred_target_title = %s
                        WHERE user_id = %s AND referred_by IS NULL;
                    """, (rcode, ttype, tid, ttitle, user_id))
                    entry_info = {
                        "entry_source": "direct_link",
                        "target_title": ttitle,
                        "referral_code": rcode,
                    }
                else:
                    cur.execute("""
                        UPDATE bot_users
                        SET entry_source = 'organic',
                            referred_target_title = COALESCE(referred_target_title, 'دخول مباشر للبوت (/start)')
                        WHERE user_id = %s AND referred_by IS NULL AND entry_source IS NULL;
                    """, (user_id,))
                    entry_info = {
                        "entry_source": "organic",
                        "target_title": "دخول مباشر للبوت (/start)",
                        "referral_code": None,
                    }

            # Qualify pending referral (+1 point to referrer) as soon as the user performs real bot usage
            if event_type in ("watch_movie", "watch_episode", "search", "browse"):
                affiliate_qualified = _qualify_pending_referral_in_tx(cur, user_id, action_text or event_type)

        _cache_set(cache_key, True, ttl=60.0)
    except Exception as e:
        logger.warning(f"Error registering bot user {user_id}: {e}")
    return {
        "is_new": is_new,
        "rejoined": rejoined,
        "affiliate_entry": affiliate_entry,
        "affiliate_qualified": affiliate_qualified,
        "entry_info": entry_info,
    }


def get_user_affiliate_summary(user_id: int) -> dict:
    """Returns a user's complete points balance, invited users list, and top 5 leaderboard."""
    out = {
        "points": 0,
        "total_entered": 0,
        "qualified_count": 0,
        "pending_count": 0,
        "referral_code": encode_affiliate_code("bot", None, user_id),
        "invited_users": [],
        "leaderboard": [],
    }
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(points, 0) AS points,
                    COALESCE(total_referrals_entered, 0) AS total_entered,
                    COALESCE(qualified_referrals_count, 0) AS qualified_count
                FROM bot_users
                WHERE user_id = %s;
            """, (user_id,))
            row = cur.fetchone()
            if row:
                out["points"] = int(row["points"] or 0)
                out["total_entered"] = int(row["total_entered"] or 0)
                out["qualified_count"] = int(row["qualified_count"] or 0)
                out["pending_count"] = max(0, out["total_entered"] - out["qualified_count"])

            cur.execute("""
                SELECT
                    ar.referred_user_id,
                    ar.referral_code,
                    ar.target_type,
                    ar.target_id,
                    ar.target_title,
                    ar.status,
                    ar.points_awarded,
                    ar.created_at,
                    ar.qualified_at,
                    u.username,
                    u.first_name,
                    u.last_name
                FROM affiliate_referrals ar
                LEFT JOIN bot_users u ON u.user_id = ar.referred_user_id
                WHERE ar.referrer_user_id = %s
                ORDER BY ar.created_at DESC
                LIMIT 12;
            """, (user_id,))
            out["invited_users"] = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT
                    user_id, username, first_name, last_name,
                    COALESCE(points, 0) AS points,
                    COALESCE(qualified_referrals_count, 0) AS qualified_count,
                    COALESCE(total_referrals_entered, 0) AS total_entered
                FROM bot_users
                WHERE COALESCE(points, 0) > 0 OR COALESCE(total_referrals_entered, 0) > 0
                ORDER BY COALESCE(points, 0) DESC, COALESCE(qualified_referrals_count, 0) DESC, COALESCE(total_referrals_entered, 0) DESC
                LIMIT 5;
            """)
            out["leaderboard"] = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"Error getting affiliate summary for {user_id}: {e}")
    return out


def adjust_user_points(user_id: int, delta: int, reason: str = "تعديل نقاط من الإدارة") -> dict | None:
    """Admin helper to add or deduct points for a user with full ledger audit."""
    try:
        with db_cursor() as cur:
            cur.execute("""
                UPDATE bot_users
                SET points = GREATEST(0, COALESCE(points, 0) + %s)
                WHERE user_id = %s
                RETURNING user_id, username, first_name, points;
            """, (int(delta), int(user_id)))
            u = cur.fetchone()
            if not u:
                return None
            new_bal = int(u["points"] or 0)
            cur.execute("""
                INSERT INTO affiliate_point_transactions (user_id, delta, balance_after, reason, created_at)
                VALUES (%s, %s, %s, %s, NOW());
            """, (int(user_id), int(delta), new_bal, (reason or "تعديل إداري")[:295]))
            sign_str = f"+{delta}" if delta >= 0 else str(delta)
            cur.execute("""
                INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                VALUES (%s, %s, %s, 'points_adjusted', %s, NOW());
            """, (
                int(user_id),
                u.get("username"),
                u.get("first_name"),
                f"⚖️ تعديل نقاط ({sign_str}) — الرصيد الجديد: {new_bal} نقطة ({reason})"[:490],
            ))
            return dict(u)
    except Exception as e:
        logger.warning(f"Error adjusting points for {user_id}: {e}")
        return None


def mark_user_left(
    user_id: int,
    username: str = None,
    first_name: str = None,
    last_name: str = None,
    reason: str = "blocked"
) -> dict:
    """
    Marks a user as having left/stopped/blocked the bot and logs the leave event.
    Returns {'changed': bool, 'username': str, 'first_name': str}.
    """
    with _cache_lock:
        _cache.pop(f"user_reg:{user_id}", None)

    reason_ar = "حساب محذوف" if reason == "deactivated" else "أوقف / حظر البوت"
    changed = False
    try:
        with db_cursor() as cur:
            cur.execute("SELECT is_active, username, first_name FROM bot_users WHERE user_id = %s;", (user_id,))
            row = cur.fetchone()
            eff_username = username or (row["username"] if row else None)
            eff_first = first_name or (row["first_name"] if row else None)

            if not row:
                changed = True
                cur.execute("""
                    INSERT INTO bot_users (
                        user_id, username, first_name, last_name,
                        is_active, status, joined_at, last_active_at, left_at, actions_count, last_action
                    )
                    VALUES (%s, %s, %s, %s, FALSE, %s, NOW(), NOW(), NOW(), 0, %s);
                """, (user_id, eff_username, eff_first, last_name, reason, f"🚪 {reason_ar}"))
            elif row["is_active"]:
                changed = True
                cur.execute("""
                    UPDATE bot_users
                    SET is_active = FALSE,
                        status = %s,
                        left_at = NOW(),
                        username = COALESCE(%s, username),
                        first_name = COALESCE(%s, first_name),
                        last_name = COALESCE(%s, last_name),
                        last_action = %s
                    WHERE user_id = %s;
                """, (reason, eff_username, eff_first, last_name, f"🚪 {reason_ar}", user_id))
            else:
                cur.execute("""
                    UPDATE bot_users
                    SET status = %s,
                        left_at = COALESCE(left_at, NOW())
                    WHERE user_id = %s;
                """, (reason, user_id))

            if changed:
                cur.execute("""
                    INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                    VALUES (%s, %s, %s, 'left', %s, NOW());
                """, (user_id, eff_username, eff_first, reason_ar))

            return {"changed": changed, "username": eff_username, "first_name": eff_first}
    except Exception as e:
        logger.warning(f"Error marking user {user_id} as left: {e}")
        return {"changed": False, "username": username, "first_name": first_name}


def mark_user_inactive(user_id: int):
    mark_user_left(user_id, reason="blocked")


def get_bot_users_stats() -> dict:
    default_stats = {
        "total_users": 0,
        "active_users": 0,
        "left_users": 0,
        "online_1h": 0,
        "active_24h": 0,
        "joined_today": 0,
        "left_today": 0,
        "compliant_users": 0,
        "partial_users": 0,
        "unsubscribed_users": 0,
        "left_channel_users": 0,
        "watchers_users": 0,
        "binge_watchers": 0,
        "total_movies_watched": 0,
        "total_episodes_watched": 0,
        "total_searches": 0,
        "total_points": 0,
        "referred_users": 0,
        "qualified_referrals": 0,
        "active_affiliates": 0,
    }
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT 
                    COUNT(*) as total_users,
                    COUNT(*) FILTER (WHERE is_active = TRUE) as active_users,
                    COUNT(*) FILTER (WHERE is_active = FALSE) as left_users,
                    COUNT(*) FILTER (WHERE is_active = TRUE AND last_active_at >= NOW() - INTERVAL '1 hour') as online_1h,
                    COUNT(*) FILTER (WHERE is_active = TRUE AND last_active_at >= NOW() - INTERVAL '24 hours') as active_24h,
                    COUNT(*) FILTER (WHERE joined_at >= NOW() - INTERVAL '24 hours') as joined_today,
                    COUNT(*) FILTER (WHERE is_active = FALSE AND left_at >= NOW() - INTERVAL '24 hours') as left_today,
                    COUNT(*) FILTER (WHERE rules_status = 'compliant') as compliant_users,
                    COUNT(*) FILTER (WHERE rules_status = 'partial') as partial_users,
                    COUNT(*) FILTER (WHERE rules_status = 'unsubscribed') as unsubscribed_users,
                    COUNT(*) FILTER (WHERE rules_status = 'left_channel') as left_channel_users,
                    COUNT(*) FILTER (WHERE COALESCE(movies_watched_count, 0) + COALESCE(episodes_watched_count, 0) >= 1) as watchers_users,
                    COUNT(*) FILTER (WHERE COALESCE(movies_watched_count, 0) + COALESCE(episodes_watched_count, 0) >= 2) as binge_watchers,
                    COALESCE(SUM(COALESCE(movies_watched_count, 0)), 0) as total_movies_watched,
                    COALESCE(SUM(COALESCE(episodes_watched_count, 0)), 0) as total_episodes_watched,
                    COALESCE(SUM(COALESCE(searches_count, 0)), 0) as total_searches,
                    COALESCE(SUM(COALESCE(points, 0)), 0) as total_points,
                    COUNT(*) FILTER (WHERE referred_by IS NOT NULL) as referred_users,
                    COUNT(*) FILTER (WHERE referred_by IS NOT NULL AND referral_qualified = TRUE) as qualified_referrals,
                    COUNT(*) FILTER (WHERE COALESCE(total_referrals_entered, 0) >= 1 OR COALESCE(points, 0) > 0) as active_affiliates
                FROM bot_users;
            """)
            row = cur.fetchone()
            return dict(row) if row else default_stats
    except Exception as e:
        logger.warning(f"Error getting bot_users stats: {e}")
        return default_stats


def get_compliance_and_funnel_summary() -> dict:
    """Computes full funnel & per-channel subscription breakdown for /admin inside Telegram."""
    stats = get_bot_users_stats()
    channels = get_required_channels()
    per_ch = {str(c["chat_id"]): {"title": c["title"], "joined": 0, "checked": 0} for c in channels}
    try:
        with db_cursor() as cur:
            cur.execute("SELECT joined_channels_json FROM bot_users WHERE joined_channels_json IS NOT NULL AND joined_channels_json != '[]';")
            for r in cur.fetchall():
                try:
                    arr = json.loads(r["joined_channels_json"] or "[]")
                    for item in arr:
                        cid = str(item.get("chat_id"))
                        if cid in per_ch:
                            per_ch[cid]["checked"] += 1
                            if item.get("joined"):
                                per_ch[cid]["joined"] += 1
                except Exception:
                    pass
    except Exception:
        pass
    return {"stats": stats, "channels": list(per_ch.values())}


def get_single_user_full_profile(user_id: int) -> dict | None:
    """Returns a user's complete compliance status, interaction breakdown, affiliate details, and recent timeline."""
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT
                    u.user_id, u.username, u.first_name, u.last_name, u.is_active, u.status,
                    u.joined_at, u.last_active_at, u.left_at, u.rejoined_at,
                    COALESCE(u.actions_count, 1) as actions_count,
                    u.last_action,
                    COALESCE(u.rules_status, 'unknown') as rules_status,
                    COALESCE(u.joined_channels_count, 0) as joined_channels_count,
                    COALESCE(u.required_channels_count, 0) as required_channels_count,
                    COALESCE(u.joined_channels_json, '[]') as joined_channels_json,
                    u.rules_checked_at,
                    COALESCE(u.force_sub_blocks_count, 0) as force_sub_blocks_count,
                    u.force_sub_passed_at,
                    COALESCE(u.movies_watched_count, 0) as movies_watched_count,
                    COALESCE(u.episodes_watched_count, 0) as episodes_watched_count,
                    COALESCE(u.searches_count, 0) as searches_count,
                    COALESCE(u.browses_count, 0) as browses_count,
                    COALESCE(u.points, 0) as points,
                    COALESCE(u.total_referrals_entered, 0) as total_referrals_entered,
                    COALESCE(u.qualified_referrals_count, 0) as qualified_referrals_count,
                    u.referred_by,
                    u.entry_source,
                    u.referred_via_code,
                    u.referred_target_title,
                    COALESCE(u.referral_qualified, FALSE) as referral_qualified,
                    ref.username as referrer_username,
                    ref.first_name as referrer_first_name
                FROM bot_users u
                LEFT JOIN bot_users ref ON ref.user_id = u.referred_by
                WHERE u.user_id = %s;
            """, (user_id,))
            u = cur.fetchone()
            if not u:
                return None
            u_dict = dict(u)
            try:
                u_dict["channels_list"] = json.loads(u_dict.get("joined_channels_json") or "[]")
            except Exception:
                u_dict["channels_list"] = []

            cur.execute("""
                SELECT event_type, details, created_at
                FROM bot_user_events
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 25;
            """, (user_id,))
            u_dict["events"] = [dict(r) for r in cur.fetchall()]
            return u_dict
    except Exception as e:
        logger.warning(f"Error fetching full profile for {user_id}: {e}")
        return None


def get_bot_users_paginated(filter_mode: str = "active", page: int = 1, per_page: int = 10) -> dict:
    offset = max(0, (page - 1) * per_page)
    if filter_mode == "left":
        where_sql = "WHERE u.is_active = FALSE"
        order_sql = "ORDER BY u.left_at DESC NULLS LAST, u.last_active_at DESC NULLS LAST"
    elif filter_mode == "compliant":
        where_sql = "WHERE u.rules_status = 'compliant'"
        order_sql = "ORDER BY u.last_active_at DESC NULLS LAST"
    elif filter_mode == "noncompliant":
        where_sql = "WHERE u.rules_status IN ('unsubscribed', 'partial', 'left_channel')"
        order_sql = "ORDER BY u.last_active_at DESC NULLS LAST"
    elif filter_mode == "watchers":
        where_sql = "WHERE COALESCE(u.movies_watched_count, 0) + COALESCE(u.episodes_watched_count, 0) >= 1"
        order_sql = "ORDER BY (COALESCE(u.movies_watched_count, 0) + COALESCE(u.episodes_watched_count, 0)) DESC, u.last_active_at DESC"
    elif filter_mode == "affiliates":
        where_sql = "WHERE COALESCE(u.points, 0) > 0 OR COALESCE(u.total_referrals_entered, 0) >= 1"
        order_sql = "ORDER BY COALESCE(u.points, 0) DESC, COALESCE(u.total_referrals_entered, 0) DESC, u.last_active_at DESC"
    elif filter_mode == "referred":
        where_sql = "WHERE u.referred_by IS NOT NULL"
        order_sql = "ORDER BY u.referred_at DESC NULLS LAST, u.joined_at DESC NULLS LAST"
    elif filter_mode == "today":
        where_sql = "WHERE u.joined_at >= NOW() - INTERVAL '24 hours'"
        order_sql = "ORDER BY u.joined_at DESC NULLS LAST"
    elif filter_mode == "online24h":
        where_sql = "WHERE u.is_active = TRUE AND u.last_active_at >= NOW() - INTERVAL '24 hours'"
        order_sql = "ORDER BY u.last_active_at DESC NULLS LAST"
    elif filter_mode == "all":
        where_sql = ""
        order_sql = "ORDER BY u.last_active_at DESC NULLS LAST"
    else:
        where_sql = "WHERE u.is_active = TRUE"
        order_sql = "ORDER BY u.last_active_at DESC NULLS LAST"

    try:
        with db_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM bot_users u {where_sql};")
            total = cur.fetchone()["cnt"]

            cur.execute(f"""
                SELECT
                    u.user_id, u.username, u.first_name, u.last_name, u.is_active,
                    COALESCE(u.status, CASE WHEN u.is_active THEN 'active' ELSE 'blocked' END) as status,
                    u.joined_at, u.last_active_at, u.left_at, u.rejoined_at,
                    COALESCE(u.actions_count, 1) as actions_count,
                    u.last_action,
                    COALESCE(u.rules_status, 'unknown') as rules_status,
                    COALESCE(u.joined_channels_count, 0) as joined_channels_count,
                    COALESCE(u.required_channels_count, 0) as required_channels_count,
                    COALESCE(u.movies_watched_count, 0) as movies_watched_count,
                    COALESCE(u.episodes_watched_count, 0) as episodes_watched_count,
                    COALESCE(u.searches_count, 0) as searches_count,
                    COALESCE(u.browses_count, 0) as browses_count,
                    COALESCE(u.points, 0) as points,
                    COALESCE(u.total_referrals_entered, 0) as total_referrals_entered,
                    COALESCE(u.qualified_referrals_count, 0) as qualified_referrals_count,
                    u.referred_by,
                    u.entry_source,
                    u.referred_via_code,
                    u.referred_target_title,
                    COALESCE(u.referral_qualified, FALSE) as referral_qualified,
                    ref.username as referrer_username,
                    ref.first_name as referrer_first_name
                FROM bot_users u
                LEFT JOIN bot_users ref ON ref.user_id = u.referred_by
                {where_sql}
                {order_sql}
                LIMIT %s OFFSET %s;
            """, (per_page, offset))
            items = [dict(r) for r in cur.fetchall()]
        return {
            "items": items,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }
    except Exception as e:
        logger.warning(f"Error getting paginated bot_users: {e}")
        return {"items": [], "total": 0, "page": 1, "total_pages": 1}


def get_recent_bot_events(page: int = 1, per_page: int = 12, event_filter: str = "all") -> dict:
    offset = max(0, (page - 1) * per_page)
    params = []
    where_sql = ""
    if event_filter == "joins_leaves":
        where_sql = "WHERE event_type IN ('joined', 'left', 'rejoined')"
    elif event_filter == "affiliate":
        where_sql = "WHERE event_type IN ('affiliate_entered', 'affiliate_qualified', 'points_adjusted')"
    elif event_filter and event_filter != "all":
        where_sql = "WHERE event_type = %s"
        params.append(event_filter)

    try:
        with db_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM bot_user_events {where_sql};", tuple(params))
            total = cur.fetchone()["cnt"]

            cur.execute(f"""
                SELECT id, user_id, username, first_name, event_type, details, created_at
                FROM bot_user_events
                {where_sql}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s;
            """, tuple(params) + (per_page, offset))
            items = [dict(r) for r in cur.fetchall()]
        return {
            "items": items,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }
    except Exception as e:
        logger.warning(f"Error fetching recent bot events: {e}")
        return {"items": [], "total": 0, "page": 1, "total_pages": 1}


def get_vault_admin_summary() -> dict:
    """Returns live Telegram Vault upload stats and the 8 most recent uploads."""
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(DISTINCT movie_id) FILTER (WHERE movie_id IS NOT NULL AND file_size_bytes > 15000000) as ready_movies,
                    COUNT(DISTINCT episode_id) FILTER (WHERE episode_id IS NOT NULL AND file_size_bytes > 15000000) as ready_episodes,
                    ROUND(COALESCE(SUM(file_size_bytes), 0) / (1024.0 * 1024.0 * 1024.0), 2) as total_gb,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours' AND file_size_bytes > 15000000) as uploaded_24h
                FROM telegram_archives;
            """)
            v = dict(cur.fetchone() or {})

            cur.execute("""
                SELECT COUNT(DISTINCT s.id) as ready_series
                FROM series s
                JOIN seasons sea ON sea.series_id = s.id
                JOIN episodes e ON e.season_id = sea.id
                JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000;
            """)
            v["ready_series"] = (cur.fetchone() or {}).get("ready_series", 0)

            cur.execute("SELECT COUNT(*) as c FROM movies;")
            v["total_db_movies"] = (cur.fetchone() or {}).get("c", 0)
            cur.execute("SELECT COUNT(*) as c FROM episodes;")
            v["total_db_episodes"] = (cur.fetchone() or {}).get("c", 0)

            cur.execute("""
                SELECT
                    ta.id,
                    CASE WHEN ta.movie_id IS NOT NULL THEN 'movie' ELSE 'episode' END as media_type,
                    COALESCE(
                        m.title,
                        s.title || ' - م' || COALESCE(sea.season_number, 1) || ' ح' || COALESCE(e.episode_number, 1)
                    ) as title,
                    ROUND(COALESCE(ta.file_size_bytes, 0) / (1024.0 * 1024.0), 1) as size_mb,
                    ta.created_at
                FROM telegram_archives ta
                LEFT JOIN movies m ON m.id = ta.movie_id
                LEFT JOIN episodes e ON e.id = ta.episode_id
                LEFT JOIN seasons sea ON sea.id = e.season_id
                LEFT JOIN series s ON s.id = sea.series_id
                WHERE ta.file_size_bytes > 15000000
                ORDER BY ta.created_at DESC
                LIMIT 8;
            """)
            v["recent_uploads"] = [dict(r) for r in cur.fetchall()]
            return v
    except Exception as e:
        logger.warning(f"Error getting vault admin summary: {e}")
        return {
            "ready_movies": 0,
            "ready_series": 0,
            "ready_episodes": 0,
            "total_gb": 0,
            "uploaded_24h": 0,
            "total_db_movies": 0,
            "total_db_episodes": 0,
            "recent_uploads": [],
        }


def get_all_active_user_ids() -> list[int]:
    try:
        with db_cursor() as cur:
            cur.execute("SELECT user_id FROM bot_users WHERE is_active = TRUE ORDER BY joined_at DESC;")
            return [r["user_id"] for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"Error fetching active user IDs: {e}")
        return []


def get_all_bot_users_basic() -> list[dict]:
    try:
        with db_cursor() as cur:
            cur.execute("SELECT user_id, username, first_name, is_active FROM bot_users ORDER BY joined_at DESC;")
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"Error fetching all bot users: {e}")
        return []


def get_required_channels() -> list[dict]:
    cache_key = "force_sub_channels"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id, chat_id, title, invite_link FROM required_channels ORDER BY id ASC;")
            rows = [dict(r) for r in cur.fetchall()]
        _cache_set(cache_key, rows, ttl=30.0)
        return rows
    except Exception as e:
        logger.warning(f"Error fetching required_channels: {e}")
        return []


def add_required_channel(chat_id: str, title: str, invite_link: str) -> bool:
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO required_channels (chat_id, title, invite_link)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE
            SET title = EXCLUDED.title, invite_link = EXCLUDED.invite_link;
        """, (chat_id.strip(), title.strip(), invite_link.strip()))
    with _cache_lock:
        _cache.pop("force_sub_channels", None)
    return True


def remove_required_channel(channel_db_id_or_chat_id: str) -> bool:
    val = str(channel_db_id_or_chat_id).strip()
    with db_cursor() as cur:
        if val.isdigit():
            cur.execute("DELETE FROM required_channels WHERE id = %s OR chat_id = %s;", (int(val), val))
        else:
            cur.execute("DELETE FROM required_channels WHERE chat_id = %s;", (val,))
    with _cache_lock:
        _cache.pop("force_sub_channels", None)
    return True


def mask_bot_token(tok: str) -> str:
    t = (tok or "").strip()
    if ":" not in t:
        return t[:8] + "..."
    prefix, secret = t.split(":", 1)
    if len(secret) <= 8:
        return f"{prefix}:***"
    return f"{prefix}:{secret[:4]}...{secret[-4:]}"


def get_uploader_bots() -> list[dict]:
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT
                    id, bot_token, bot_id, bot_username, bot_name,
                    COALESCE(is_active, TRUE) AS is_active,
                    COALESCE(channel_ok, FALSE) AS channel_ok,
                    COALESCE(uploads_count, 0) AS uploads_count,
                    COALESCE(total_uploaded_bytes, 0) AS total_uploaded_bytes,
                    last_used_at, last_error, created_at
                FROM uploader_bots
                ORDER BY id ASC;
            """)
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["masked_token"] = mask_bot_token(d.get("bot_token") or "")
                d["uploaded_gb"] = round(float(d.get("total_uploaded_bytes") or 0) / (1024.0 ** 3), 2)
                rows.append(d)
            return rows
    except Exception as e:
        logger.warning(f"Error fetching uploader_bots: {e}")
        return []


def get_uploader_pool_summary() -> dict:
    workers = get_uploader_bots()
    ready_helpers = sum(1 for w in workers if w.get("is_active") and w.get("channel_ok"))
    p_cnt = 0
    p_bytes = 0
    try:
        with db_cursor() as cur:
            cur.execute("SELECT key, value FROM bot_settings WHERE key IN ('primary_bot_uploads_count', 'primary_bot_uploaded_bytes');")
            for r in cur.fetchall():
                if r["key"] == "primary_bot_uploads_count":
                    p_cnt = int(r["value"] or 0)
                elif r["key"] == "primary_bot_uploaded_bytes":
                    p_bytes = int(r["value"] or 0)
    except Exception:
        pass
    return {
        "workers": workers,
        "ready_helpers": ready_helpers,
        "total_helpers": len(workers),
        "active_pool_size": 1 + ready_helpers,
        "primary_uploads": p_cnt,
        "primary_gb": round(p_bytes / (1024.0 ** 3), 2),
    }


def upsert_uploader_bot(
    bot_token: str,
    bot_id: int | None,
    bot_username: str | None,
    bot_name: str | None,
    channel_ok: bool,
    last_error: str | None = None,
) -> dict:
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO uploader_bots (
                bot_token, bot_id, bot_username, bot_name, is_active, channel_ok, last_error, created_at
            )
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, NOW())
            ON CONFLICT (bot_token) DO UPDATE
            SET bot_id = COALESCE(EXCLUDED.bot_id, uploader_bots.bot_id),
                bot_username = COALESCE(EXCLUDED.bot_username, uploader_bots.bot_username),
                bot_name = COALESCE(EXCLUDED.bot_name, uploader_bots.bot_name),
                is_active = TRUE,
                channel_ok = EXCLUDED.channel_ok,
                last_error = EXCLUDED.last_error
            RETURNING id, bot_id, bot_username, bot_name, is_active, channel_ok, uploads_count, last_error;
        """, (bot_token.strip(), bot_id, bot_username, bot_name, bool(channel_ok), last_error))
        row = cur.fetchone()
        return dict(row) if row else {}


def toggle_uploader_bot(worker_id: int) -> dict | None:
    with db_cursor() as cur:
        cur.execute("""
            UPDATE uploader_bots
            SET is_active = NOT COALESCE(is_active, TRUE)
            WHERE id = %s
            RETURNING id, bot_username, bot_name, is_active, channel_ok;
        """, (int(worker_id),))
        row = cur.fetchone()
        return dict(row) if row else None


def delete_uploader_bot(worker_id: int) -> bool:
    with db_cursor() as cur:
        cur.execute("DELETE FROM uploader_bots WHERE id = %s;", (int(worker_id),))
        return cur.rowcount > 0


def warm_up_cache():
    """Pre-loads all dynamic menus and main categories into RAM on startup for 0ms first-click response."""
    try:
        init_adult18_tables()
        init_force_sub_table()
        get_required_channels()
        get_vault_media_counts()
        get_adult_vault_items(1, 16)
        for mtype in ("m", "s"):
            reg_info = get_available_regions(mtype)
            get_available_genres(mtype, "all")
            for r in reg_info.get("regions", []):
                rcode = r["code"]
                get_available_genres(mtype, rcode)
                if mtype == "m":
                    get_filtered_movies(rcode, "all", 1, 16)
                else:
                    get_filtered_series(rcode, "all", 1, 16)
        logger.info("Bot RAM cache pre-warmed successfully.")
    except Exception as e:
        logger.warning(f"Cache warm-up warning: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  🔞 Adult +18 — Telegram Stars Access & Manual Upload
# ═══════════════════════════════════════════════════════════════════════════════

def init_adult18_tables():
    """
    Creates the two +18 tables if they don't exist yet.
    Safe to call on every startup (idempotent).
      - adult18_access  : tracks which users paid Stars and when their 30-day access expires
      - adult18_movies  : manually-uploaded +18 films (admin-only)
    """
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS adult18_access (
                id                          SERIAL PRIMARY KEY,
                user_id                     BIGINT NOT NULL UNIQUE,
                stars_paid                  INTEGER NOT NULL DEFAULT 100,
                granted_at                  TIMESTAMPTZ DEFAULT NOW(),
                expires_at                  TIMESTAMPTZ NOT NULL,
                telegram_payment_charge_id  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_adult18_access_user ON adult18_access(user_id);

            CREATE TABLE IF NOT EXISTS adult18_movies (
                id                SERIAL PRIMARY KEY,
                title             TEXT NOT NULL,
                year              INTEGER,
                description       TEXT,
                poster_file_id    TEXT,
                vault_message_id  BIGINT NOT NULL,
                vault_channel_id  TEXT NOT NULL DEFAULT '-1004466886745',
                added_by_admin_id BIGINT,
                created_at        TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    logger.info("adult18_access and adult18_movies tables ensured.")


def check_adult18_access(user_id: int) -> bool:
    """Returns True if user_id has paid Stars access that has not yet expired."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM adult18_access WHERE user_id = %s AND expires_at > NOW()",
            (user_id,)
        )
        return cur.fetchone() is not None


def grant_adult18_access(user_id: int, stars_paid: int, charge_id: str) -> None:
    """
    Records (or refreshes) a user's paid +18 access for 30 days.
    If the user already has access, the expiry is extended from the later of
    NOW() or their current expiry, so back-to-back payments stack.
    """
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO adult18_access (user_id, stars_paid, granted_at, expires_at, telegram_payment_charge_id)
            VALUES (%s, %s, NOW(),
                    GREATEST(NOW(), (SELECT expires_at FROM adult18_access WHERE user_id = %s)) + INTERVAL '30 days',
                    %s)
            ON CONFLICT (user_id) DO UPDATE
                SET stars_paid                  = EXCLUDED.stars_paid,
                    granted_at                  = NOW(),
                    expires_at                  = GREATEST(adult18_access.expires_at, NOW()) + INTERVAL '30 days',
                    telegram_payment_charge_id  = EXCLUDED.telegram_payment_charge_id;
        """, (user_id, stars_paid, user_id, charge_id))


def get_adult18_movies(page: int = 1, per_page: int = 16) -> dict:
    """Returns paginated list of manually-uploaded +18 movies."""
    cache_key = f"adult18_movies:{page}:{per_page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    offset = (page - 1) * per_page
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM adult18_movies")
        total = int((cur.fetchone() or {}).get("cnt") or 0)

        cur.execute("""
            SELECT id, title, year, description, poster_file_id,
                   vault_message_id, vault_channel_id, created_at
            FROM adult18_movies
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))
        items = cur.fetchall()

    res = {
        "items": items,
        "total": total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }
    _cache_set(cache_key, res, ttl=60.0)
    return res


def add_adult18_movie(
    title: str,
    vault_message_id: int,
    vault_channel_id: str,
    year: int | None = None,
    description: str | None = None,
    poster_file_id: str | None = None,
    added_by_admin_id: int | None = None,
) -> int:
    """Inserts a new +18 movie record and returns its DB id."""
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO adult18_movies
                (title, year, description, poster_file_id, vault_message_id, vault_channel_id, added_by_admin_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (title, year, description, poster_file_id, vault_message_id, vault_channel_id, added_by_admin_id))
        row = cur.fetchone()
    # Invalidate cached pages
    with _cache_lock:
        for k in [k for k in _cache if k.startswith("adult18_movies:")]:
            _cache.pop(k, None)
    return int(row["id"])


def delete_adult18_movie(movie_id: int) -> bool:
    """Deletes a +18 movie by id. Returns True if a row was deleted."""
    with db_cursor() as cur:
        cur.execute("DELETE FROM adult18_movies WHERE id = %s", (movie_id,))
        deleted = cur.rowcount > 0
    with _cache_lock:
        for k in [k for k in _cache if k.startswith("adult18_movies:")]:
            _cache.pop(k, None)
    return deleted


def get_adult18_access_info(user_id: int) -> dict | None:
    """Returns the full access row for a user, or None if no active access."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT user_id, stars_paid, granted_at, expires_at FROM adult18_access "
            "WHERE user_id = %s AND expires_at > NOW()",
            (user_id,)
        )
        return cur.fetchone()
