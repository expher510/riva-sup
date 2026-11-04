"""
Riva Cinema - Continuous Metadata Enrichment & Title Cleaner
Automated daemon and pipeline module to ensure NO movie or series
remains without an HD poster, Arabic/English synopsis, and TMDB rating.
"""

import os
import sys
import time
import html
import re
import logging
import urllib.request
import urllib.parse
import json
from typing import Optional, Dict, Any, Tuple

from sqlalchemy import select, or_, and_

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s: %(levelname)s/metadata_enricher] %(message)s"
)
logger = logging.getLogger("metadata_enricher")

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "9acdd2e9bdd2c77d2134171ce08da1db")

# Sync database session setup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import settings

db_url = getattr(settings, "SYNC_DATABASE_URL", None) or settings.DATABASE_URL
if "+asyncpg" in db_url:
    db_url = db_url.replace("+asyncpg", "+psycopg2")
if "+aiosqlite" in db_url:
    db_url = db_url.replace("+aiosqlite", "")

sync_engine = create_engine(db_url, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)

# Known aliases mapping for difficult or colloquially scraped titles
KNOWN_ALIASES = {
    "twd daryl dixon": "The Walking Dead: Daryl Dixon",
    "the devil wears prada 1": "The Devil Wears Prada",
    "خواتم السلطة": "The Lord of the Rings: The Rings of Power",
    "vikings valhalla": "Vikings: Valhalla",
    "saw xi": "Saw XI",
    "saw x": "Saw X",
    "twilight 1": "Twilight",
    "twilight 2": "The Twilight Saga: New Moon",
    "twilight 3": "The Twilight Saga: Eclipse",
    "twilight 4": "The Twilight Saga: Breaking Dawn - Part 1",
    "twilight 5": "The Twilight Saga: Breaking Dawn - Part 2",
    "underworld 1": "Underworld",
    "underworld 2": "Underworld: Evolution",
    "underworld 3": "Underworld: Rise of the Lycans",
    "underworld 4": "Underworld: Awakening",
    "underworld 5": "Underworld: Blood Wars",
    "fast and furious 1": "The Fast and the Furious",
    "fast and furious 2": "2 Fast 2 Furious",
    "fast and furious 3": "The Fast and the Furious: Tokyo Drift",
    "fast and furious 4": "Fast & Furious",
    "fast and furious 5": "Fast Five",
    "fast and furious 6": "Fast & Furious 6",
    "fast and furious 7": "Furious 7",
    "fast and furious 8": "The Fate of the Furious",
    "fast and furious 9 f9": "F9",
    "fast and furious 9": "F9",
}

# Standard TMDB Genre ID -> English Genre Name used by Bot sections
TMDB_GENRE_MAP = {
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Science Fiction",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
    10759: "Action",
    10765: "Science Fiction",
}


def clean_title_for_search(raw_title: str) -> Tuple[str, Optional[int]]:
    """
    Cleans messy scraped titles, removes HTML entities, scraper prefixes/suffixes,
    extracts year and separates English/Arabic titles for optimal search.
    """
    if not raw_title:
        return "", None

    # 1. Unescape HTML entities (e.g. &#8217; -> ')
    text = html.unescape(raw_title).strip()

    # 2. Check known aliases directly (case-insensitive)
    lower_text = text.lower()
    for alias_k, alias_v in KNOWN_ALIASES.items():
        if alias_k in lower_text:
            text = re.sub(re.escape(alias_k), alias_v, text, flags=re.IGNORECASE)
            break

    # 3. Extract year (1900-2099)
    year_match = re.search(r'\b(19\d\d|20\d\d)\b', text)
    year = int(year_match.group(1)) if year_match else None

    # Remove year from query text for broader matching
    text_clean = re.sub(r'[\(\[]?\b(19\d\d|20\d\d)\b[\)\]]?', '', text)

    # 4. Remove common Arabic scraper prefixes
    prefixes = [
        r'^\s*مسلسلات\s+\S+\s+مسلسل\s*',
        r'^\s*مسلسلات\s+\S+\s*',
        r'^\s*مشاهدة\s+وتحميل\s+(?:فيلم|مسلسل)?\s*',
        r'^\s*مشاهدة\s+(?:فيلم|مسلسل)?\s*',
        r'^\s*تحميل\s+(?:فيلم|مسلسل)?\s*',
        r'^\s*فيلم\s*',
        r'^\s*مسلسل\s*'
    ]
    for p in prefixes:
        text_clean = re.sub(p, '', text_clean, flags=re.IGNORECASE)

    # 5. Remove common scraper suffixes
    suffixes = [
        r'\s*الاخيرة\s*$',
        r'\s*الحلقة\s+\d+\s*$',
        r'\s*الموسم\s+\d+\s*$',
        r'\s*مترجم(?:ة)?\s*$',
        r'\s*مدبلج(?:ة)?\s*$',
        r'\s*(?:بجودة\s+عالية|نسخة\s+اصلية|كامل)\s*$',
        r'\s*\b(?:1080p|720p|480p|WEB-DL|BluRay|HDTV)\b\s*$'
    ]
    for s in suffixes:
        text_clean = re.sub(s, '', text_clean, flags=re.IGNORECASE)

    text_clean = re.sub(r'\s+', ' ', text_clean).strip()

    # 6. Extract English title part vs Arabic part
    eng_parts = re.findall(r'[a-zA-Z0-9\s\'\:\-\,\.\!\?\&]+', text_clean)
    eng = " ".join([p.strip() for p in eng_parts if len(p.strip()) > 1]).strip()

    ar_parts = re.findall(r'[\u0600-\u06FF\s]+', text_clean)
    ar = " ".join([p.strip() for p in ar_parts if len(p.strip()) > 1]).strip()

    # Prefer English title if available and reasonable length (TMDB matching is strongest with original English)
    candidate = eng if len(eng) >= 3 else (ar if len(ar) >= 2 else text_clean)
    # Strip trailing " 1" for part-1 movies (e.g. "The Devil Wears Prada 1" -> "The Devil Wears Prada")
    candidate = re.sub(r'\s+1$', '', candidate).strip()
    return candidate, year


def ensure_arabic_overview(text: str) -> str:
    """
    Ensures the movie/series overview is always in Arabic.
    If the text from TMDB is in English or another non-Arabic language,
    automatically translates it into clean Arabic.
    """
    if not text or not text.strip():
        return ""
    text = text.strip()
    ar_chars = len(re.findall(r'[\u0600-\u06FF]', text))
    if ar_chars >= 15:
        return text

    q = urllib.parse.quote(text)
    # 1. Primary fast translation endpoint (Chrome dictionary endpoint - no 429 block)
    try:
        url = f"https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=ar&q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and isinstance(data, list):
                first = data[0]
                if isinstance(first, list) and first and isinstance(first[0], str):
                    return first[0].strip()
                elif isinstance(first, str):
                    return first.strip()
    except Exception as e:
        logger.debug(f"Primary translation fallback failed: {e}")

    # 2. Secondary fallback (MyMemory API)
    try:
        url2 = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(text[:480])}&langpair=en|ar"
        req2 = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=8) as resp2:
            data2 = json.loads(resp2.read().decode("utf-8"))
            tr = (data2.get("responseData") or {}).get("translatedText") or ""
            if tr.strip() and len(re.findall(r'[\u0600-\u06FF]', tr)) >= 10:
                return tr.strip()
    except Exception as e:
        logger.warning(f"Secondary Arabic translation fallback failed: {e}")

    return text


def _score_tmdb_candidate(r: Dict[str, Any], clean_q: str, base_q: str, parsed_yr: Optional[int], media_type: str) -> float:
    """
    Scores a TMDB search result by title relevance, year match, poster availability, and popularity/vote_count.
    Prevents numbered sequels or popular titles from matching obscure shorts or unrelated foreign movies.
    """
    score = 0.0
    if r.get("poster_path"):
        score += 50.0

    cand_titles = [
        (r.get("title") or "").lower().strip(),
        (r.get("name") or "").lower().strip(),
        (r.get("original_title") or "").lower().strip(),
        (r.get("original_name") or "").lower().strip(),
    ]
    cand_titles = [t for t in cand_titles if t]

    q_low = clean_q.lower().strip()
    b_low = base_q.lower().strip()

    # Main words (3+ chars) from base query
    words = [w for w in re.findall(r'[a-z0-9\u0600-\u06FF]+', b_low) if len(w) >= 3]

    if any(t == q_low or t == b_low for t in cand_titles):
        score += 220.0
    elif any(q_low in t or b_low in t for t in cand_titles):
        score += 140.0
    elif words and any(all(w in t for w in words) for t in cand_titles):
        score += 100.0
    elif words and any(any(w in t for w in words) for t in cand_titles):
        score += 30.0
    else:
        score -= 150.0

    rel_date = r.get("release_date") if media_type == "movie" else r.get("first_air_date")
    cand_yr = int(rel_date[:4]) if rel_date and len(rel_date) >= 4 and rel_date[:4].isdigit() else None
    if parsed_yr and cand_yr:
        if cand_yr == parsed_yr:
            score += 80.0
        elif abs(cand_yr - parsed_yr) == 1:
            score += 30.0
        else:
            score -= 60.0

    vote_count = int(r.get("vote_count") or 0)
    popularity = float(r.get("popularity") or 0.0)
    score += min(100.0, vote_count / 25.0)
    score += min(40.0, popularity / 5.0)
    return score


def search_tmdb_metadata(title: str, media_type: str = "movie", year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Queries TMDB API for movie or TV show.
    Strict order:
    1. Fetches the Original Language Poster & Backdrop (en-US / original_language).
    2. Fetches the Arabic Description (ar-SA), translating to Arabic if TMDB only has a foreign overview.
    """
    if not TMDB_API_KEY:
        logger.warning("TMDB_API_KEY is not configured.")
        return None

    endpoint = "movie" if media_type == "movie" else "tv"
    clean_q, parsed_yr = clean_title_for_search(title)
    if not parsed_yr and year:
        parsed_yr = year

    if not clean_q:
        return None

    base_q = re.sub(r'\s+\d+$', '', clean_q).strip() or clean_q
    headers = {"User-Agent": "RivaCinemaEnricher/1.0"}

    def _fetch_search(query_str: str, yr: Optional[int], lang: str = "en-US") -> list:
        enc = urllib.parse.quote(query_str)
        u = f"https://api.themoviedb.org/3/search/{endpoint}?api_key={TMDB_API_KEY}&query={enc}&language={lang}"
        if yr:
            u += f"&year={yr}" if media_type == "movie" else f"&first_air_date_year={yr}"
        req = urllib.request.Request(u, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8")).get("results", [])

    try:
        results = _fetch_search(clean_q, parsed_yr, "en-US")

        # If numbered sequel (e.g. "Twilight 3", "Underworld 5", "Pirates of the Caribbean 3"), also search base title with year
        if base_q != clean_q and parsed_yr:
            base_results = _fetch_search(base_q, parsed_yr, "en-US")
            seen_ids = {r["id"] for r in results}
            for br in base_results:
                if br["id"] not in seen_ids:
                    results.append(br)
                    seen_ids.add(br["id"])

        # If zero results with year constraint, retry without year
        if not results and parsed_yr:
            results = _fetch_search(clean_q, None, "en-US")
            if not results and base_q != clean_q:
                results = _fetch_search(base_q, None, "en-US")

        if not results:
            return None

        # Rank all candidates by relevance + popularity so we never pick obscure shorts or unrelated foreign matches
        results.sort(key=lambda r: _score_tmdb_candidate(r, clean_q, base_q, parsed_yr, media_type), reverse=True)
        best = results[0]
        tmdb_id = best["id"]

        # STEP 1: Fetch Original Poster & Backdrop first (in the work's original language / en-US)
        orig_lang = best.get("original_language") or "en"
        poster_lang = "en-US" if orig_lang == "en" else orig_lang
        poster_path = best.get("poster_path")
        backdrop_path = best.get("backdrop_path")
        fallback_overview = best.get("overview", "")

        orig_url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={TMDB_API_KEY}&language={poster_lang}"
        try:
            orig_req = urllib.request.Request(orig_url, headers=headers)
            with urllib.request.urlopen(orig_req, timeout=10) as orig_resp:
                orig_data = json.loads(orig_resp.read().decode("utf-8"))
                if orig_data.get("poster_path"):
                    poster_path = orig_data["poster_path"]
                if orig_data.get("backdrop_path"):
                    backdrop_path = orig_data["backdrop_path"]
                if not fallback_overview and orig_data.get("overview"):
                    fallback_overview = orig_data["overview"]
        except Exception:
            pass

        # STEP 2: Fetch Arabic Description (ar-SA) second, and translate to Arabic if TMDB lacks an Arabic overview
        ar_overview = ""
        ar_url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={TMDB_API_KEY}&language=ar-SA"
        try:
            ar_req = urllib.request.Request(ar_url, headers=headers)
            with urllib.request.urlopen(ar_req, timeout=10) as ar_resp:
                ar_data = json.loads(ar_resp.read().decode("utf-8"))
                ar_overview = (ar_data.get("overview") or "").strip()
        except Exception:
            pass

        raw_overview = ar_overview if ar_overview else fallback_overview
        if not raw_overview or not raw_overview.strip():
            en_url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"
            try:
                en_req = urllib.request.Request(en_url, headers=headers)
                with urllib.request.urlopen(en_req, timeout=10) as en_resp:
                    en_data = json.loads(en_resp.read().decode("utf-8"))
                    raw_overview = en_data.get("overview", "")
                    if not poster_path and en_data.get("poster_path"):
                        poster_path = en_data["poster_path"]
            except Exception:
                pass

        overview = ensure_arabic_overview(raw_overview)

        # Parse release year
        rel_date = best.get("release_date") if media_type == "movie" else best.get("first_air_date")
        final_year = int(rel_date[:4]) if rel_date and len(rel_date) >= 4 and rel_date[:4].isdigit() else parsed_yr

        # Map TMDB genre_ids to standard genre names for automatic bot section categorization
        genre_ids = best.get("genre_ids") or []
        genres = []
        for gid in genre_ids:
            gname = TMDB_GENRE_MAP.get(gid)
            if gname and gname not in genres:
                genres.append(gname)

        return {
            "tmdb_id": tmdb_id,
            "title": best.get("title") or best.get("name") or title,
            "original_title": best.get("original_title") or best.get("original_name"),
            "overview": overview,
            "poster_path": poster_path,
            "backdrop_path": backdrop_path,
            "vote_average": float(best.get("vote_average", 0.0)),
            "vote_count": int(best.get("vote_count", 0)),
            "popularity": float(best.get("popularity", 0.0)),
            "year": final_year,
            "genres": genres
        }

    except Exception as e:
        logger.warning(f"Error querying TMDB for '{clean_q}' ({media_type}): {e}")
        return None


def enrich_movie_metadata(session, movie_id: int) -> bool:
    """
    Enriches a single movie in database using TMDB.
    """
    from app.models import Movie
    movie = session.query(Movie).filter_by(id=movie_id).first()
    if not movie:
        return False

    # Clean HTML entities in movie title automatically
    if movie.title and ("&#" in movie.title or "&amp;" in movie.title):
        movie.title = html.unescape(movie.title).strip()

    meta = search_tmdb_metadata(movie.title, media_type="movie", year=movie.year)
    if not meta:
        if not movie.genres:
            movie.genres = ["Drama"]
            session.commit()
        return False

    # Apply updates (preserve curated provocative poster/backdrop/overview on 'adult' movies if already set)
    is_curated_adult = getattr(movie, "category", None) == "adult"
    if meta.get("poster_path") and not (is_curated_adult and movie.poster_path):
        movie.poster_path = meta["poster_path"]
    if meta.get("backdrop_path") and not (is_curated_adult and movie.backdrop_path):
        movie.backdrop_path = meta["backdrop_path"]
    if meta.get("overview") and not (is_curated_adult and movie.overview):
        movie.overview = meta["overview"]
    if meta.get("vote_average"):
        movie.vote_average = meta["vote_average"]
    if meta.get("vote_count"):
        movie.vote_count = meta["vote_count"]
    if meta.get("popularity"):
        movie.popularity = meta["popularity"]
    if meta.get("genres"):
        movie.genres = meta["genres"]
    elif not movie.genres:
        movie.genres = ["Drama"]
    if meta.get("original_title"):
        movie.original_title = meta["original_title"]
    if meta.get("tmdb_id"):
        # Check if tmdb_id collides with another movie
        collision = session.query(Movie).filter(Movie.tmdb_id == meta["tmdb_id"], Movie.id != movie.id).first()
        if not collision:
            movie.tmdb_id = meta["tmdb_id"]
    if meta.get("year") and not movie.year:
        movie.year = meta["year"]

    session.commit()
    logger.info(f"✨ Enriched Movie #{movie.id} ({movie.title}) -> Genres: {movie.genres}, Poster: {movie.poster_path}")
    return True


def enrich_series_metadata(session, series_id: int) -> bool:
    """
    Enriches a single series in database using TMDB.
    """
    from app.models import Series
    series = session.query(Series).filter_by(id=series_id).first()
    if not series:
        return False

    if series.title and ("&#" in series.title or "&amp;" in series.title):
        series.title = html.unescape(series.title).strip()

    meta = search_tmdb_metadata(series.title, media_type="tv", year=series.year)
    if not meta:
        return False

    # Apply updates (preserve curated poster/backdrop/overview/tmdb_id on 'adult' series if already set)
    is_curated_adult = getattr(series, "category", None) == "adult"
    if meta.get("poster_path") and not (is_curated_adult and series.poster_path):
        series.poster_path = meta["poster_path"]
    if meta.get("backdrop_path") and not (is_curated_adult and series.backdrop_path):
        series.backdrop_path = meta["backdrop_path"]
    if meta.get("overview") and not (is_curated_adult and series.overview):
        series.overview = meta["overview"]
    if meta.get("vote_average"):
        series.vote_average = meta["vote_average"]
    if meta.get("vote_count"):
        series.vote_count = meta["vote_count"]
    if meta.get("popularity") and not (is_curated_adult and (series.popularity or 0) > meta["popularity"]):
        series.popularity = meta["popularity"]
    if meta.get("genres"):
        series.genres = meta["genres"]
    elif not series.genres:
        series.genres = ["Drama"]
    if meta.get("original_title") and not (is_curated_adult and series.original_title):
        series.original_title = meta["original_title"]
    if meta.get("tmdb_id") and not (is_curated_adult and series.tmdb_id):
        collision = session.query(Series).filter(Series.tmdb_id == meta["tmdb_id"], Series.id != series.id).first()
        if not collision:
            series.tmdb_id = meta["tmdb_id"]
    if meta.get("year") and not series.year:
        series.year = meta["year"]

    session.commit()
    logger.info(f"✨ Enriched Series #{series.id} ({series.title}) -> Poster: {series.poster_path}, TMDB ID: {series.tmdb_id}")
    return True


def run_enrichment_cycle():
    """
    Executes one full enrichment pass:
    1. Active Telegram Vault movies missing poster/overview (Top Priority).
    2. Active Telegram Vault series missing poster/overview (Top Priority).
    3. General movies/series in DB missing metadata.
    """
    from sqlalchemy import text

    logger.info("Starting metadata enrichment pass...")

    with SyncSessionLocal() as session:
        # 1. Telegram Vault Movies without poster, Arabic overview, or genres
        sql_vault_movies = """
            SELECT DISTINCT m.id
            FROM movies m
            JOIN telegram_archives ta ON ta.movie_id = m.id
            WHERE ta.file_size_bytes > 15000000
              AND (
                  m.poster_path IS NULL OR m.poster_path = ''
                  OR m.overview IS NULL OR m.overview = '' OR m.overview !~ '[ء-ي]'
                  OR m.genres IS NULL OR m.genres::text = '[]' OR m.genres::text = 'null'
              );
        """
        vault_movie_ids = [r[0] for r in session.execute(text(sql_vault_movies)).fetchall()]
        logger.info(f"Found {len(vault_movie_ids)} Telegram Vault movies needing enrichment.")
        for mid in vault_movie_ids:
            try:
                enrich_movie_metadata(session, mid)
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Error enriching movie {mid}: {e}")
                session.rollback()

        # 2. Telegram Vault Series without poster, Arabic overview, or genres
        sql_vault_series = """
            SELECT DISTINCT s.id
            FROM series s
            JOIN seasons sea ON sea.series_id = s.id
            JOIN episodes e ON e.season_id = sea.id
            JOIN telegram_archives ta ON ta.episode_id = e.id
            WHERE ta.file_size_bytes > 15000000
              AND (
                  s.poster_path IS NULL OR s.poster_path = ''
                  OR s.overview IS NULL OR s.overview = '' OR s.overview !~ '[ء-ي]'
                  OR s.genres IS NULL OR s.genres::text = '[]' OR s.genres::text = 'null'
              );
        """
        vault_series_ids = [r[0] for r in session.execute(text(sql_vault_series)).fetchall()]
        logger.info(f"Found {len(vault_series_ids)} Telegram Vault series needing enrichment.")
        for sid in vault_series_ids:
            try:
                enrich_series_metadata(session, sid)
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Error enriching series {sid}: {e}")
                session.rollback()

        # 3. General non-vault movies (batch of 30 per cycle)
        sql_gen_movies = """
            SELECT id FROM movies
            WHERE (
                poster_path IS NULL OR poster_path = ''
                OR overview IS NULL OR overview = ''
                OR genres IS NULL OR genres::text = '[]' OR genres::text = 'null'
            )
            LIMIT 30;
        """
        gen_movie_ids = [r[0] for r in session.execute(text(sql_gen_movies)).fetchall()]
        for mid in gen_movie_ids:
            try:
                enrich_movie_metadata(session, mid)
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Error in general movie {mid}: {e}")
                session.rollback()

        # 4. General non-vault series (batch of 20 per cycle)
        sql_gen_series = """
            SELECT id FROM series
            WHERE (poster_path IS NULL OR poster_path = '' OR overview IS NULL OR overview = '')
            LIMIT 20;
        """
        gen_series_ids = [r[0] for r in session.execute(text(sql_gen_series)).fetchall()]
        for sid in gen_series_ids:
            try:
                enrich_series_metadata(session, sid)
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Error in general series {sid}: {e}")
                session.rollback()

    logger.info("Enrichment pass completed successfully.")


def run_continuous_daemon(interval_seconds: int = 120):
    """
    Runs continuously as a background service daemon.
    """
    logger.info(f"Starting continuous Metadata Enricher Daemon (interval: {interval_seconds}s)...")
    while True:
        try:
            run_enrichment_cycle()
        except Exception as e:
            logger.error(f"Unexpected error in daemon cycle: {e}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    if "--continuous" in sys.argv:
        interval = 120
        run_continuous_daemon(interval)
    else:
        run_enrichment_cycle()
