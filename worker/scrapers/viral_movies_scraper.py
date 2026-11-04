"""
Viral, Trending & All-Time Top-Rated Movie Hunter for Riva VOD Engine.
1. Tracks VIP user-requested movies (including theatrical-to-VOD watchlists like 'You Can See Everything' 2026)
   and attaches playable mirrors the instant they release online.
2. Syncs live TMDB Trending (Day/Week), Popular, Top-Rated (8.0+ masterpieces), and Arabic hits,
   updating DB popularity/ratings and scraping missing viral/high-rated movies from Akwam in parallel.
"""
import json
import logging
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote

try:
    from curl_cffi import requests as crequests
except ImportError:
    import httpx

    class _FallbackHttp:
        @staticmethod
        def get(url, **kwargs):
            kwargs.pop("impersonate", None)
            headers = kwargs.get("headers", {})
            headers.setdefault(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )
            kwargs["headers"] = headers
            return httpx.get(url, follow_redirects=True, **kwargs)

    crequests = _FallbackHttp()

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Movie, StreamingLink
from worker.metadata_enricher import TMDB_GENRE_MAP, ensure_arabic_overview
from worker.scrapers.akwam_crawler import (
    BASE_URL as AKWAM_BASE_URL,
    SKIP_PATH_TOKENS,
    _extract_card_title,
    parse_movie_title,
    resolve_akwam_streams,
)

logger = logging.getLogger("viral_movies_scraper")

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)

# Built-in VIP Requested Movies Watchlist (auto-monitored every cycle until archived in Telegram Vault)
VIP_REQUESTED_TMDB_MOVIES = [
    {
        "tmdb_id": 1368337,
        "imdb_id": "tt34707827",
        "title": "The Odyssey",
        "year": 2026,
        "category": "western",
        "default_overview": (
            "ملحمة أسطورية ضخمة للمخرج العالمي كريستوفر نولان مستوحاة من أوديسة هوميروس، "
            "تتبع رحلة أوديسيوس المحفوفة بالمخاطر والوحوش والآلهة عبر البحار للعودة إلى وطنه إيثاكا بعد حرب طروادة."
        ),
    },
    {
        "tmdb_id": 687163,
        "imdb_id": "tt12042730",
        "title": "Project Hail Mary",
        "year": 2026,
        "category": "western",
        "default_overview": (
            "فيلم خيال علمي ملحمي من بطولة رايان غوسلينغ، يستيقظ فيه رائد فضاء وحيداً على متن مركبة فضائية "
            "على بُعد سنوات ضوئية من الأرض فاقداً للذاكرة، ليكتشف أنه الأمل الأخير لإنقاذ البشرية من انقراض وشيك."
        ),
    },
    {
        "tmdb_id": 969681,
        "imdb_id": "tt22084616",
        "title": "Spider-Man: Brand New Day",
        "year": 2026,
        "category": "western",
        "default_overview": (
            "فصل جديد ومثير في عالم مارفل السينمائي يبدأ فيه بيتر باركر حياة جديدة تماماً بعد أن نسي العالم هويته، "
            "ليواجه تهديدات غير مسبوقة تختبر حدود قوته وتضحياته كسبايدر مان."
        ),
    },
    {
        "tmdb_id": 1765072,
        "imdb_id": "tt45368240",
        "title": "You Can See Everything",
        "year": 2026,
        "category": "western",
        "default_overview": (
            "فيلم وثائقي درامي تشويقي من إنتاج A24 وإخراج ناثان فيلدر ولانس أوبنهايم، "
            "يتتبع الأيام الأخيرة لمؤسسة ثيرانوس إليزابيث هولمز قبل دخولها السجن الفيدرالي، "
            "ليتحول البورتريه الوثائقي إلى دراما نفسية مذهلة حول الحقيقة والإعلام والإدراك."
        ),
    },
]


def _norm_movie_key(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r"^(?:مشاهدة\s+)?(?:وتحميل\s+)?(?:فيلم|انمي|أنمي)\s+", "", t)
    t = re.sub(r"\b(?:مترجم|مترجمة|مدبلج|مدبلجة|اون\s*لاين|أون\s*لاين|hd|كامل|نسخة|اصلية)\b", "", t)
    t = re.sub(r"[أإآ]", "ا", t)
    t = re.sub(r"ة\b", "ه", t)
    t = re.sub(r"ى\b", "ي", t)
    t = re.sub(r"[^a-z0-9\u0600-\u06FF\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tmdb_get_json(endpoint_and_query: str) -> Dict[str, Any]:
    tmdb_key = getattr(settings, "TMDB_API_KEY", None) or "9acdd2e9bdd2c77d2134171ce08da1db"
    sep = "&" if "?" in endpoint_and_query else "?"
    url = f"https://api.themoviedb.org/3/{endpoint_and_query}{sep}api_key={tmdb_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _infer_movie_category(tmdb_item: Dict[str, Any]) -> str:
    orig_lang = (tmdb_item.get("original_language") or "").lower()
    g_ids = tmdb_item.get("genre_ids") or [g.get("id") for g in (tmdb_item.get("genres") or []) if isinstance(g, dict)]
    if orig_lang == "ar":
        return "arabic"
    if 16 in g_ids and orig_lang in ("ja", "zh"):
        return "anime"
    if orig_lang in ("ko", "ja", "zh", "th", "id"):
        return "asian"
    if orig_lang == "tr":
        return "turkish"
    return "western"


def search_akwam_movie_streams(movie_title: str, year: Optional[int] = None, orig_title: Optional[str] = None) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Searches Akwam directly for a specific movie by title (and optional original title + year)
    and returns (matched_post_url, resolved_streams).
    Strictly verifies normalized title & year to avoid matching 'Making Of' shorts or unrelated years.
    """
    queries = []
    for cand_t in (movie_title, orig_title):
        if cand_t and cand_t.strip():
            clean_q = re.sub(r"\s*:\s*", " ", cand_t.strip())
            if clean_q not in queries:
                queries.append(clean_q)

    target_keys = {_norm_movie_key(t) for t in (movie_title, orig_title) if t and _norm_movie_key(t)}
    # Also allow ' Part 1' / ' 1' suffix equivalence on Akwam (e.g. 'Dune' vs 'Dune 1')
    expanded_keys = set(target_keys)
    for tk in list(target_keys):
        expanded_keys.add(f"{tk} 1")
        if tk.endswith(" 1"):
            expanded_keys.add(tk[:-2].strip())

    matched_urls: List[str] = []
    seen_u = set()

    for q in queries[:2]:
        try:
            r = crequests.get(f"{AKWAM_BASE_URL}/?s={quote(q)}", impersonate="chrome120", timeout=12)
            if r.status_code != 200 or not r.text:
                continue
            anchors = re.findall(
                r'<a\s+[^>]*href=["\'](https?://akwams\.org/[^"\'?#]+/)["\'][^>]*>(.*?)</a>',
                r.text,
                re.DOTALL,
            )
            for post_u, inner in anchors:
                if post_u in seen_u or any(tok in post_u for tok in SKIP_PATH_TOKENS):
                    continue
                dec_u = unquote(post_u)
                card_t = _extract_card_title(inner)
                combined_check = f"{card_t} {dec_u}"
                if "الحلقة" in combined_check or "الموسم" in combined_check or "مسلسل" in card_t:
                    continue
                if "فيلم" not in combined_check and "انمي" not in combined_check and "أنمي" not in combined_check:
                    continue
                if "making of" in combined_check.lower():
                    continue

                parsed_t, parsed_yr = parse_movie_title(card_t, item_url=post_u)
                norm_cand = _norm_movie_key(parsed_t)
                if norm_cand not in expanded_keys:
                    continue
                if year and parsed_yr and abs(int(parsed_yr) - int(year)) > 1:
                    continue

                seen_u.add(post_u)
                matched_urls.append(post_u)
        except Exception as e:
            logger.debug(f"Akwam movie search failed for '{q}': {e}")

    all_streams: List[Dict[str, Any]] = []
    primary_url: Optional[str] = None
    for u in matched_urls[:3]:
        streams = resolve_akwam_streams(u)
        if streams:
            if not primary_url:
                primary_url = u
            all_streams.extend(streams)

    deduped = []
    seen_s = set()
    for s in all_streams:
        k = s.get("direct_url") or s.get("embed_url")
        if k and k not in seen_s:
            seen_s.add(k)
            deduped.append(s)
    return primary_url, deduped


def _ensure_vip_watchlist_synced(redis_client) -> List[int]:
    """Ensures VIP requested movies are in PostgreSQL, registered in Redis, and checked on Akwam."""
    vip_db_ids: List[int] = []
    for vip in VIP_REQUESTED_TMDB_MOVIES:
        tmdb_id = vip["tmdb_id"]
        ar_meta = {}
        en_meta = {}
        try:
            ar_meta = _tmdb_get_json(f"movie/{tmdb_id}?language=ar-SA")
            en_meta = _tmdb_get_json(f"movie/{tmdb_id}?language=en-US")
        except Exception:
            pass

        with SyncSessionLocal() as session:
            m_obj = session.query(Movie).filter_by(tmdb_id=tmdb_id).first()
            if not m_obj:
                m_obj = (
                    session.query(Movie)
                    .filter(Movie.title.ilike(vip["title"]))
                    .first()
                )
            overview_ar = (ar_meta.get("overview") or "").strip()
            if not overview_ar or len(re.findall(r"[\u0600-\u06FF]", overview_ar)) < 15:
                overview_ar = ensure_arabic_overview(en_meta.get("overview") or "") or vip["default_overview"]

            genres = [g["name"] for g in (en_meta.get("genres") or []) if g.get("name")] or ["Documentary", "Drama"]
            poster = en_meta.get("poster_path") or ar_meta.get("poster_path")
            backdrop = en_meta.get("backdrop_path") or ar_meta.get("backdrop_path")
            vote_avg = float(en_meta.get("vote_average") or 8.9)
            vote_cnt = int(en_meta.get("vote_count") or 76)
            pop = max(float(en_meta.get("popularity") or 85.0), 120.0)

            if not m_obj:
                m_obj = Movie(
                    tmdb_id=tmdb_id,
                    imdb_id=vip.get("imdb_id"),
                    category=vip.get("category", "western"),
                    title=vip["title"],
                    original_title=en_meta.get("original_title") or vip["title"],
                    overview=overview_ar,
                    year=vip["year"],
                    poster_path=poster,
                    backdrop_path=backdrop,
                    vote_average=vote_avg,
                    vote_count=vote_cnt,
                    popularity=pop,
                    runtime=en_meta.get("runtime"),
                    genres=genres,
                )
                session.add(m_obj)
                session.flush()
            else:
                m_obj.popularity = max(float(m_obj.popularity or 0.0), pop)
                if poster and not m_obj.poster_path:
                    m_obj.poster_path = poster
                if overview_ar and not m_obj.overview:
                    m_obj.overview = overview_ar

            m_id = m_obj.id
            vip_db_ids.append(m_id)
            active_links = (
                session.query(StreamingLink)
                .filter(StreamingLink.movie_id == m_id, StreamingLink.status == "active")
                .count()
            )
            session.commit()

        if redis_client:
            try:
                redis_client.sadd("riva:vip_movie_ids", str(m_id))
            except Exception:
                pass

        # Actively check Akwam if VIP movie has 0 active links yet
        if active_links == 0:
            post_u, streams = search_akwam_movie_streams(vip["title"], year=vip["year"])
            if streams:
                with SyncSessionLocal() as session:
                    m_obj = session.query(Movie).filter_by(id=m_id).first()
                    if post_u and m_obj and not m_obj.source_url:
                        m_obj.source_url = post_u
                    for s in streams:
                        u = s.get("direct_url") or s.get("embed_url")
                        if u:
                            session.add(
                                StreamingLink(
                                    movie_id=m_id,
                                    provider_name=s["provider_name"],
                                    embed_url=s["embed_url"],
                                    direct_url=u,
                                    quality="1080p",
                                    status="active",
                                )
                            )
                    session.commit()
                logger.info(f"🎯 VIP Watchlist Movie '{vip['title']}' NOW HAS {len(streams)} LIVE MIRRORS!")

    return vip_db_ids


def sync_viral_and_top_rated_movies_sync(max_new_scrapes: int = 24) -> Dict[str, int]:
    """
    1. Pulls TMDB Trending (Day & Week), Popular (p1..4), Top-Rated (p1..5), and Top Arabic Movies.
    2. Stores live trending IDs in Redis `riva:tmdb_trending_movie_ids` for viral priority boosting.
    3. Refreshes `popularity`, `vote_average`, `vote_count` on matching DB movies.
    4. Scrapes up to `max_new_scrapes` high-priority Viral / Top-Rated movies from Akwam in parallel!
    """
    from worker.pipeline import redis_sync

    _ensure_vip_watchlist_synced(redis_sync)

    trending_ids: Set[int] = set()
    catalog: Dict[int, Dict[str, Any]] = {}

    endpoints = [
        ("trending/movie/day?language=en-US", True),
        ("trending/movie/week?language=en-US", True),
        ("movie/now_playing?language=en-US&page=1", True),
        ("movie/now_playing?language=en-US&page=2", True),
        ("discover/movie?primary_release_date.gte=2025-01-01&sort_by=popularity.desc&page=1", True),
        ("discover/movie?primary_release_date.gte=2025-01-01&sort_by=popularity.desc&page=2", True),
        ("discover/movie?primary_release_date.gte=2025-01-01&sort_by=popularity.desc&page=3", True),
        ("movie/popular?language=en-US&page=1", True),
        ("movie/popular?language=en-US&page=2", True),
        ("movie/popular?language=en-US&page=3", True),
        ("movie/popular?language=en-US&page=4", False),
        ("movie/top_rated?language=en-US&page=1", False),
        ("movie/top_rated?language=en-US&page=2", False),
        ("movie/top_rated?language=en-US&page=3", False),
        ("movie/top_rated?language=en-US&page=4", False),
        ("movie/top_rated?language=en-US&page=5", False),
        ("movie/top_rated?language=en-US&page=6", False),
        # Anime Viral & All-Time Top-Rated Movies on TMDB
        ("discover/movie?with_genres=16&with_original_language=ja&sort_by=popularity.desc&page=1", True),
        ("discover/movie?with_genres=16&with_original_language=ja&sort_by=popularity.desc&page=2", True),
        ("discover/movie?with_genres=16&with_original_language=ja&vote_count.gte=200&sort_by=vote_average.desc&page=1", False),
        ("discover/movie?with_genres=16&with_original_language=ja&vote_count.gte=200&sort_by=vote_average.desc&page=2", False),
        # Regional Viral Movies (Arabic, Korean, Turkish)
        ("discover/movie?with_original_language=ar&sort_by=popularity.desc&page=1", True),
        ("discover/movie?with_original_language=ar&sort_by=popularity.desc&page=2", True),
        ("discover/movie?with_original_language=ar&vote_count.gte=25&sort_by=vote_average.desc&page=1", False),
        ("discover/movie?with_original_language=ko&sort_by=popularity.desc&page=1", True),
        ("discover/movie?with_original_language=tr&sort_by=popularity.desc&page=1", True),
    ]

    for ep_path, mark_viral in endpoints:
        try:
            data = _tmdb_get_json(ep_path)
            for item in data.get("results") or []:
                tid = item.get("id")
                if not tid:
                    continue
                if mark_viral:
                    trending_ids.add(int(tid))
                if tid not in catalog or float(item.get("popularity") or 0) > float(catalog[tid].get("popularity") or 0):
                    catalog[int(tid)] = item
        except Exception as e:
            logger.debug(f"TMDB fetch warning on {ep_path}: {e}")

    if trending_ids:
        try:
            redis_sync.delete("riva:tmdb_trending_movie_ids")
            redis_sync.sadd("riva:tmdb_trending_movie_ids", *[str(x) for x in trending_ids])
            redis_sync.expire("riva:tmdb_trending_movie_ids", 43200)
        except Exception as e:
            logger.debug(f"Redis trending IDs update warning: {e}")

    updated_existing = 0
    missing_candidates: List[Dict[str, Any]] = []

    with SyncSessionLocal() as session:
        archived_tmdb_ids = {
            r[0]
            for r in session.execute(
                text("""
                    SELECT DISTINCT m.tmdb_id
                    FROM movies m
                    JOIN telegram_archives ta ON ta.movie_id = m.id AND ta.file_size_bytes > 15000000
                    WHERE m.tmdb_id IS NOT NULL;
                """)
            ).fetchall()
        }

        existing_by_tmdb = {
            m.tmdb_id: m
            for m in session.query(Movie).filter(Movie.tmdb_id.in_(list(catalog.keys()))).all()
            if m.tmdb_id
        }
        all_norm_map: Dict[str, Movie] = {}
        for m in session.query(Movie).all():
            nk = _norm_movie_key(m.title)
            if nk and nk not in all_norm_map:
                all_norm_map[nk] = m

        for tid, item in catalog.items():
            title = (item.get("title") or "").strip()
            orig_title = (item.get("original_title") or "").strip()
            rel = item.get("release_date") or ""
            yr = int(rel[:4]) if len(rel) >= 4 and rel[:4].isdigit() else None
            pop = float(item.get("popularity") or 0.0)
            rate = float(item.get("vote_average") or 0.0)
            votes = int(item.get("vote_count") or 0)

            db_m = existing_by_tmdb.get(tid)
            if not db_m:
                cand_m = all_norm_map.get(_norm_movie_key(title)) or all_norm_map.get(_norm_movie_key(orig_title))
                if cand_m and (not yr or not cand_m.year or abs(int(cand_m.year) - yr) <= 1):
                    db_m = cand_m
                    if not db_m.tmdb_id:
                        db_m.tmdb_id = tid

            if db_m:
                if pop > float(db_m.popularity or 0.0):
                    db_m.popularity = pop
                if votes > int(db_m.vote_count or 0):
                    db_m.vote_count = votes
                    db_m.vote_average = rate
                updated_existing += 1

                if tid not in archived_tmdb_ids:
                    active_cnt = (
                        session.query(StreamingLink)
                        .filter(StreamingLink.movie_id == db_m.id, StreamingLink.status == "active")
                        .count()
                    )
                    if active_cnt == 0:
                        missing_candidates.append(item)
            else:
                if tid not in archived_tmdb_ids and (votes >= 25 or pop >= 60.0):
                    missing_candidates.append(item)

        session.commit()

    # Sort missing candidates by Viral + High-Rating score so we hunt the hottest & best ones first!
    missing_candidates.sort(
        key=lambda x: (
            (500.0 if int(x.get("id") or 0) in trending_ids else 0.0)
            + min(float(x.get("popularity") or 0.0), 500.0) * 1.5
            + float(x.get("vote_average") or 0.0) * 40.0
            + min(int(x.get("vote_count") or 0), 25000) / 50.0
        ),
        reverse=True,
    )

    # Reserve ~1/4 of hunt slots for top Anime Movies + 3/4 for Viral & Top-Rated Live-Action Movies
    anime_mv_quota = max(4, max_new_scrapes // 4)
    filtered_to_hunt = []
    seen_mv_tids = set()

    for item in missing_candidates:
        if _infer_movie_category(item) != "anime":
            continue
        tid = item.get("id")
        try:
            if redis_sync.exists(f"riva:viral_hunt_miss:{tid}"):
                continue
        except Exception:
            pass
        filtered_to_hunt.append(item)
        seen_mv_tids.add(tid)
        if len(filtered_to_hunt) >= anime_mv_quota:
            break

    for item in missing_candidates:
        if len(filtered_to_hunt) >= max_new_scrapes:
            break
        tid = item.get("id")
        if tid in seen_mv_tids:
            continue
        try:
            if redis_sync.exists(f"riva:viral_hunt_miss:{tid}"):
                continue
        except Exception:
            pass
        filtered_to_hunt.append(item)
        seen_mv_tids.add(tid)

    newly_scraped = 0
    if filtered_to_hunt:
        from worker.scrapers.cimalight_scraper import search_cimalight_movie_streams

        def _hunt_one(item: Dict[str, Any]):
            tid = int(item["id"])
            title = (item.get("title") or "").strip()
            orig_title = (item.get("original_title") or "").strip()
            clean_orig = "" if re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", orig_title) else orig_title
            rel = item.get("release_date") or ""
            yr = int(rel[:4]) if len(rel) >= 4 and rel[:4].isdigit() else None
            post_u, streams = search_akwam_movie_streams(title, year=yr, orig_title=clean_orig)
            if not streams:
                cl_u, cl_streams = search_cimalight_movie_streams(title, year=yr, orig_title=clean_orig)
                if cl_streams:
                    post_u, streams = cl_u, cl_streams
            return item, yr, post_u, streams

        with ThreadPoolExecutor(max_workers=6) as pool:
            hunted_results = list(pool.map(_hunt_one, filtered_to_hunt))

        with SyncSessionLocal() as session:
            for item, yr, post_u, streams in hunted_results:
                tid = int(item["id"])
                if not streams:
                    try:
                        redis_sync.set(f"riva:viral_hunt_miss:{tid}", "1", ex=43200)
                    except Exception:
                        pass
                    continue

                title = (item.get("title") or "").strip()
                orig_title = (item.get("original_title") or title).strip()
                ar_overview = ""
                try:
                    ar_d = _tmdb_get_json(f"movie/{tid}?language=ar-SA")
                    ar_overview = (ar_d.get("overview") or "").strip()
                except Exception:
                    pass
                if not ar_overview or len(re.findall(r"[\u0600-\u06FF]", ar_overview)) < 15:
                    ar_overview = ensure_arabic_overview(item.get("overview") or "")

                genres = [TMDB_GENRE_MAP[g] for g in (item.get("genre_ids") or []) if g in TMDB_GENRE_MAP]
                cat = _infer_movie_category(item)

                db_m = session.query(Movie).filter_by(tmdb_id=tid).first()
                if not db_m and post_u:
                    db_m = session.query(Movie).filter_by(source_url=post_u).first()

                if not db_m:
                    db_m = Movie(
                        tmdb_id=tid,
                        source_url=post_u,
                        category=cat,
                        title=orig_title if cat == "arabic" else title,
                        original_title=orig_title,
                        overview=ar_overview,
                        year=yr,
                        poster_path=item.get("poster_path"),
                        backdrop_path=item.get("backdrop_path"),
                        vote_average=float(item.get("vote_average") or 0.0),
                        vote_count=int(item.get("vote_count") or 0),
                        popularity=float(item.get("popularity") or 0.0),
                        genres=genres,
                    )
                    session.add(db_m)
                    session.flush()
                    newly_scraped += 1
                else:
                    if not db_m.tmdb_id:
                        db_m.tmdb_id = tid
                    db_m.popularity = max(float(db_m.popularity or 0.0), float(item.get("popularity") or 0.0))
                    if item.get("poster_path") and not db_m.poster_path:
                        db_m.poster_path = item.get("poster_path")
                    if ar_overview and not db_m.overview:
                        db_m.overview = ar_overview

                for s in streams:
                    u = s.get("direct_url") or s.get("embed_url")
                    if not u:
                        continue
                    exists_sl = (
                        session.query(StreamingLink)
                        .filter(
                            StreamingLink.movie_id == db_m.id,
                            (StreamingLink.direct_url == u) | (StreamingLink.embed_url == s["embed_url"]),
                        )
                        .first()
                    )
                    if exists_sl:
                        if exists_sl.status == "dead":
                            exists_sl.status = "active"
                    else:
                        session.add(
                            StreamingLink(
                                movie_id=db_m.id,
                                provider_name=s["provider_name"],
                                embed_url=s["embed_url"],
                                direct_url=u,
                                quality=s.get("quality", "1080p"),
                                status="active",
                            )
                        )
            session.commit()

    logger.info(
        f"🔥 Viral & Top-Rated Sync complete: {len(trending_ids)} live trending IDs, "
        f"{updated_existing} DB movies refreshed, +{newly_scraped} brand-new viral/top-rated movies scraped from Akwam!"
    )
    return {
        "trending_ids": len(trending_ids),
        "updated_existing": updated_existing,
        "newly_scraped": newly_scraped,
    }

