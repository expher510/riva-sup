"""
Viral, Trending & All-Time Top-Rated Series Hunter for Riva VOD Engine.
1. Pulls live TMDB Trending TV (Day/Week), Popular TV, Top-Rated TV (8.5+ masterpieces),
   and Trending Arabic, Turkish, Korean/Asian & Anime hits.
2. Stores live trending TV IDs in Redis `riva:tmdb_trending_series_ids` for viral priority boosting.
3. Enriches & refreshes `popularity`, `vote_average`, `vote_count`, HD `poster_path`, `backdrop_path`,
   and Arabic `overview` on existing DB Series (especially in-progress series already in the bot).
4. Hunts missing Viral & Top-Rated series across Cimalight + Akwam in parallel, inserting ONLY
   series that start cleanly from Season 1 Episode 1 (`S01E01 -> Latest`) with live mirrors.
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
from app.models import Episode, Season, Series, StreamingLink
from worker.metadata_enricher import TMDB_GENRE_MAP, ensure_arabic_overview
from worker.scrapers.akwam_crawler import (
    BASE_URL as AKWAM_BASE_URL,
    SKIP_PATH_TOKENS,
    _extract_card_title,
    _normalize_series_key,
    parse_series_title,
    resolve_akwam_streams,
)
from worker.scrapers.cimalight_scraper import (
    discover_cimalight_series_episodes,
    resolve_cimalight_episode_streams,
)

logger = logging.getLogger("viral_series_scraper")

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)

TV_GENRE_MAP = {
    **TMDB_GENRE_MAP,
    10759: "Action",
    10765: "Science Fiction",
    10768: "War",
}


def _tmdb_get_json(endpoint_and_query: str) -> Dict[str, Any]:
    tmdb_key = getattr(settings, "TMDB_API_KEY", None) or "9acdd2e9bdd2c77d2134171ce08da1db"
    sep = "&" if "?" in endpoint_and_query else "?"
    url = f"https://api.themoviedb.org/3/{endpoint_and_query}{sep}api_key={tmdb_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _infer_series_category(tmdb_item: Dict[str, Any]) -> str:
    orig_lang = (tmdb_item.get("original_language") or "").lower()
    g_ids = tmdb_item.get("genre_ids") or [
        g.get("id") for g in (tmdb_item.get("genres") or []) if isinstance(g, dict)
    ]
    if orig_lang == "ar":
        return "arabic"
    if orig_lang == "tr":
        return "turkish"
    if 16 in g_ids and orig_lang in ("ja", "zh"):
        return "anime"
    if orig_lang in ("ko", "ja", "zh", "th", "id"):
        return "asian"
    return "western"


def _hunt_akwam_series_season1(title_queries: List[str]) -> Dict[int, Tuple[str, List[Dict[str, Any]]]]:
    """
    Searches Akwam for Season 1 episodes (`Ep 1..N`) matching any of `title_queries`.
    Returns {ep_num: (ep_url, streams_list)} only if Episode 1 is present.
    """
    target_keys = {_normalize_series_key(q) for q in title_queries if q and _normalize_series_key(q)}
    if not target_keys:
        return {}

    candidate_ep_cards: Dict[str, str] = {}
    for q in title_queries[:3]:
        q_clean = re.sub(r"\s*:\s*", " ", q.strip())
        for pg in range(1, 5):
            s_url = f"{AKWAM_BASE_URL}/page/{pg}/?s={quote(q_clean)}" if pg > 1 else f"{AKWAM_BASE_URL}/?s={quote(q_clean)}"
            try:
                r = crequests.get(s_url, impersonate="chrome120", timeout=12)
                if r.status_code != 200 or not r.text:
                    break
                anchors = re.findall(
                    r'<a\s+[^>]*href=["\'](https?://akwams\.org/[^"\'?#]+/)["\'][^>]*>(.*?)</a>',
                    r.text,
                    re.DOTALL,
                )
                found_any = 0
                for u, inner in anchors:
                    if any(tok in u for tok in SKIP_PATH_TOKENS):
                        continue
                    card_t = _extract_card_title(inner)
                    dec_u = unquote(u)
                    if "الحلقة" in card_t or "الحلقة" in dec_u:
                        candidate_ep_cards[u] = card_t
                        found_any += 1
                if found_any == 0:
                    break
            except Exception:
                break

    slot_urls: Dict[int, List[str]] = {}
    for ep_u, card_t in candidate_ep_cards.items():
        s_title, sea_num, ep_num, _ = parse_series_title(card_t, item_url=ep_u)
        if sea_num != 1:
            continue
        nk = _normalize_series_key(s_title)
        if not nk or not any(nk == tk or (len(tk) >= 5 and (tk in nk or nk in tk)) for tk in target_keys):
            continue
        slot_urls.setdefault(ep_num, []).append(ep_u)

    if 1 not in slot_urls:
        return {}

    def _resolve_ep(ep_num: int):
        urls = slot_urls[ep_num]
        merged = []
        seen = set()
        primary = urls[0]
        for u in urls[:2]:
            st_list = resolve_akwam_streams(u)
            if st_list and not merged:
                primary = u
            for s in st_list:
                k = s.get("direct_url") or s.get("embed_url")
                if k and k not in seen:
                    seen.add(k)
                    merged.append(s)
        return ep_num, primary, merged

    ep_nums = sorted(slot_urls.keys())[:30]
    with ThreadPoolExecutor(max_workers=6) as pool:
        resolved = list(pool.map(_resolve_ep, ep_nums))

    out: Dict[int, Tuple[str, List[Dict[str, Any]]]] = {}
    for ep_n, primary_u, st_list in resolved:
        if st_list:
            out[ep_n] = (primary_u, st_list)

    return out if 1 in out else {}


def _hunt_cimalight_series_season1(title_queries: List[str]) -> Dict[int, Tuple[str, List[Dict[str, Any]]]]:
    """
    Searches Cimalight for Season 1 episodes (`Ep 1..N`) matching any of `title_queries`,
    enforcing Episode 1 presence and filtering out pre-air duplicate-embed SEO placeholders.
    """
    episodes_map: Dict[int, Dict[str, Any]] = {}
    for q in title_queries[:3]:
        q_clean = re.sub(r"\s*:\s*", " ", (q or "").strip())
        if not q_clean:
            continue
        found_map, _ = discover_cimalight_series_episodes(q_clean, max_pages=3)
        if 1 in found_map and len(found_map) > len(episodes_map):
            episodes_map = found_map

    if 1 not in episodes_map:
        return {}

    ep_nums = sorted(episodes_map.keys())[:30]

    def _resolve_cl(ep_num: int):
        info = episodes_map[ep_num]
        st_list = resolve_cimalight_episode_streams(info["vid"])
        return ep_num, info["watch_url"], st_list

    with ThreadPoolExecutor(max_workers=6) as pool:
        resolved = list(pool.map(_resolve_cl, ep_nums))

    resolved.sort(key=lambda x: x[0])
    earlier_urls: Set[str] = set()
    out: Dict[int, Tuple[str, List[Dict[str, Any]]]] = {}
    for ep_n, watch_u, st_list in resolved:
        unique_st = []
        for s in st_list:
            u = (s.get("direct_url") or s.get("embed_url") or "").strip()
            emb = (s.get("embed_url") or "").strip()
            if not u or u in earlier_urls or (emb and emb in earlier_urls):
                continue
            unique_st.append(s)
        if not unique_st:
            continue
        for s in unique_st:
            u = (s.get("direct_url") or s.get("embed_url") or "").strip()
            emb = (s.get("embed_url") or "").strip()
            earlier_urls.add(u)
            if emb:
                earlier_urls.add(emb)
        out[ep_n] = (watch_u, unique_st)

    return out if 1 in out else {}


def _enrich_unrated_in_progress_series(max_items: int = 12) -> int:
    """
    Enriches existing in-progress DB series (`archived_count > 0`) that still have
    `popularity == 0` or missing `poster_path` / `overview`, so all active bot series
    carry official TMDB HD posters, ratings, and popularity scores.
    """
    enriched = 0
    with SyncSessionLocal() as session:
        rows = session.execute(
            text("""
                SELECT s.id, s.title, s.original_title, s.year
                FROM series s
                WHERE (COALESCE(s.popularity, 0) = 0 OR s.poster_path IS NULL OR s.overview IS NULL)
                  AND EXISTS (
                      SELECT 1 FROM telegram_archives ta
                      JOIN episodes e ON e.id = ta.episode_id
                      JOIN seasons sea ON sea.id = e.season_id
                      WHERE sea.series_id = s.id AND ta.file_size_bytes > 15000000
                  )
                ORDER BY s.id ASC
                LIMIT :lim;
            """),
            {"lim": max_items},
        ).fetchall()

    for sid, title, orig_title, yr in rows:
        q = (orig_title or title or "").strip()
        q = re.sub(r"\b(?:مترجم|مدبلج|مسلسل|الموسم\s*\d+)\b", "", q).strip()
        if not q:
            continue
        try:
            data = _tmdb_get_json(f"search/tv?query={urllib.parse.quote(q)}&language=ar-SA")
            results = data.get("results") or []
            if not results and title and title != q:
                data = _tmdb_get_json(f"search/tv?query={urllib.parse.quote(title)}&language=ar-SA")
                results = data.get("results") or []
            if not results:
                continue
            item = results[0]
            with SyncSessionLocal() as session:
                s_obj = session.query(Series).filter_by(id=sid).first()
                if not s_obj:
                    continue
                if not s_obj.tmdb_id and item.get("id"):
                    dup = session.query(Series).filter_by(tmdb_id=int(item["id"])).first()
                    if not dup:
                        s_obj.tmdb_id = int(item["id"])
                if item.get("poster_path") and not s_obj.poster_path:
                    s_obj.poster_path = item["poster_path"]
                if item.get("backdrop_path") and not s_obj.backdrop_path:
                    s_obj.backdrop_path = item["backdrop_path"]
                ov = (item.get("overview") or "").strip()
                if ov and not s_obj.overview:
                    s_obj.overview = ov
                pop = float(item.get("popularity") or 0.0)
                if pop > float(s_obj.popularity or 0.0):
                    s_obj.popularity = pop
                rate = float(item.get("vote_average") or 0.0)
                votes = int(item.get("vote_count") or 0)
                if rate > 0 and float(s_obj.vote_average or 0.0) == 0.0:
                    s_obj.vote_average = rate
                    s_obj.vote_count = votes
                session.commit()
                enriched += 1
        except Exception as e:
            logger.debug(f"In-progress series enrichment skipped for ID {sid} ('{title}'): {e}")

    return enriched


def sync_viral_and_top_rated_series_sync(max_new_scrapes: int = 10) -> Dict[str, int]:
    """
    1. Pulls live TMDB Trending TV (Day & Week), Popular TV, All-Time Top-Rated TV,
       and Top Viral Arabic, Turkish, and Korean/Asian Series.
    2. Stores live trending TV IDs in Redis `riva:tmdb_trending_series_ids`.
    3. Refreshes `popularity`, `vote_average`, `vote_count`, `poster_path`, and `overview` on matching DB Series.
    4. Hunts up to `max_new_scrapes` high-priority Viral / Top-Rated series across Cimalight + Akwam!
    """
    from worker.pipeline import redis_sync

    _enrich_unrated_in_progress_series(max_items=12)

    trending_ids: Set[int] = set()
    catalog: Dict[int, Dict[str, Any]] = {}

    endpoints = [
        ("trending/tv/day?language=en-US", True),
        ("trending/tv/week?language=en-US", True),
        ("tv/on_the_air?language=en-US&page=1", True),
        ("tv/on_the_air?language=en-US&page=2", True),
        ("discover/tv?first_air_date.gte=2025-01-01&sort_by=popularity.desc&page=1", True),
        ("discover/tv?first_air_date.gte=2025-01-01&sort_by=popularity.desc&page=2", True),
        ("discover/tv?first_air_date.gte=2025-01-01&sort_by=popularity.desc&page=3", True),
        ("tv/popular?language=en-US&page=1", True),
        ("tv/popular?language=en-US&page=2", True),
        ("tv/popular?language=en-US&page=3", True),
        ("tv/popular?language=en-US&page=4", False),
        ("tv/top_rated?language=en-US&page=1", False),
        ("tv/top_rated?language=en-US&page=2", False),
        ("tv/top_rated?language=en-US&page=3", False),
        ("tv/top_rated?language=en-US&page=4", False),
        ("tv/top_rated?language=en-US&page=5", False),
        # Anime Viral & All-Time Top-Rated on TMDB
        ("discover/tv?with_genres=16&with_original_language=ja&sort_by=popularity.desc&page=1", True),
        ("discover/tv?with_genres=16&with_original_language=ja&sort_by=popularity.desc&page=2", True),
        ("discover/tv?with_genres=16&with_original_language=ja&sort_by=popularity.desc&page=3", True),
        ("discover/tv?with_genres=16&with_original_language=ja&vote_count.gte=200&sort_by=vote_average.desc&page=1", False),
        ("discover/tv?with_genres=16&with_original_language=ja&vote_count.gte=200&sort_by=vote_average.desc&page=2", False),
        # Regional Viral (Arabic, Turkish, Korean)
        ("discover/tv?with_original_language=ar&sort_by=popularity.desc&page=1", True),
        ("discover/tv?with_original_language=ar&sort_by=popularity.desc&page=2", True),
        ("discover/tv?with_original_language=ar&vote_count.gte=8&sort_by=vote_average.desc&page=1", False),
        ("discover/tv?with_original_language=tr&sort_by=popularity.desc&page=1", True),
        ("discover/tv?with_original_language=tr&sort_by=popularity.desc&page=2", True),
        ("discover/tv?with_original_language=ko&sort_by=popularity.desc&page=1", True),
        ("discover/tv?with_original_language=ko&sort_by=popularity.desc&page=2", True),
    ]

    for ep_path, mark_viral in endpoints:
        try:
            data = _tmdb_get_json(ep_path)
            for item in data.get("results") or []:
                tid = item.get("id")
                if not tid:
                    continue
                # Skip talk shows / news / reality soap clutter (genres 10763 News, 10767 Talk, 10764 Reality)
                g_ids = set(item.get("genre_ids") or [])
                if g_ids & {10763, 10767, 10764}:
                    continue
                if mark_viral:
                    trending_ids.add(int(tid))
                if int(tid) not in catalog or float(item.get("popularity") or 0) > float(catalog[int(tid)].get("popularity") or 0):
                    catalog[int(tid)] = item
        except Exception as e:
            logger.debug(f"TMDB TV fetch warning on {ep_path}: {e}")

    if trending_ids:
        try:
            redis_sync.delete("riva:tmdb_trending_series_ids")
            redis_sync.sadd("riva:tmdb_trending_series_ids", *[str(x) for x in trending_ids])
            redis_sync.expire("riva:tmdb_trending_series_ids", 43200)
        except Exception as e:
            logger.debug(f"Redis trending series IDs update warning: {e}")

    updated_existing = 0
    missing_candidates: List[Dict[str, Any]] = []

    with SyncSessionLocal() as session:
        series_with_eps_tmdb = {
            r[0]
            for r in session.execute(
                text("""
                    SELECT DISTINCT s.tmdb_id
                    FROM series s
                    JOIN seasons sea ON sea.series_id = s.id
                    JOIN episodes e ON e.season_id = sea.id
                    WHERE s.tmdb_id IS NOT NULL;
                """)
            ).fetchall()
        }

        existing_by_tmdb = {
            s.tmdb_id: s
            for s in session.query(Series).filter(Series.tmdb_id.in_(list(catalog.keys()))).all()
            if s.tmdb_id
        }
        all_norm_map: Dict[str, Series] = {}
        for s in session.query(Series).all():
            for raw_n in (s.title, s.original_title):
                nk = _normalize_series_key(raw_n or "")
                if nk and nk not in all_norm_map:
                    all_norm_map[nk] = s

        for tid, item in catalog.items():
            name = (item.get("name") or "").strip()
            orig_name = (item.get("original_name") or "").strip()
            air = item.get("first_air_date") or ""
            yr = int(air[:4]) if len(air) >= 4 and air[:4].isdigit() else None
            pop = float(item.get("popularity") or 0.0)
            rate = float(item.get("vote_average") or 0.0)
            votes = int(item.get("vote_count") or 0)

            db_s = existing_by_tmdb.get(tid)
            if not db_s:
                cand_s = all_norm_map.get(_normalize_series_key(name)) or all_norm_map.get(_normalize_series_key(orig_name))
                if cand_s and (not yr or not cand_s.year or abs(int(cand_s.year) - yr) <= 2):
                    db_s = cand_s
                    if not db_s.tmdb_id:
                        db_s.tmdb_id = tid

            if db_s:
                if pop > float(db_s.popularity or 0.0):
                    db_s.popularity = pop
                if votes > int(db_s.vote_count or 0):
                    db_s.vote_count = votes
                    db_s.vote_average = rate
                if item.get("poster_path") and not db_s.poster_path:
                    db_s.poster_path = item["poster_path"]
                if item.get("backdrop_path") and not db_s.backdrop_path:
                    db_s.backdrop_path = item["backdrop_path"]
                updated_existing += 1

                if tid not in series_with_eps_tmdb:
                    ep_cnt = (
                        session.query(Episode)
                        .join(Season, Season.id == Episode.season_id)
                        .filter(Season.series_id == db_s.id)
                        .count()
                    )
                    if ep_cnt == 0:
                        missing_candidates.append(item)
            else:
                if votes >= 15 or pop >= 35.0:
                    missing_candidates.append(item)

        session.commit()

    missing_candidates.sort(
        key=lambda x: (
            (600.0 if int(x.get("id") or 0) in trending_ids else 0.0)
            + min(float(x.get("popularity") or 0.0), 500.0) * 1.8
            + float(x.get("vote_average") or 0.0) * 40.0
            + min(int(x.get("vote_count") or 0), 25000) / 45.0
        ),
        reverse=True,
    )

    # Reserve ~1/3 of hunt slots for top Anime candidates so Anime & Live-Action both stay fresh
    anime_quota = max(3, max_new_scrapes // 3)
    filtered_to_hunt = []
    seen_hunt_tids = set()

    for item in missing_candidates:
        if _infer_series_category(item) != "anime":
            continue
        tid = item.get("id")
        try:
            if redis_sync.exists(f"riva:viral_series_miss:{tid}"):
                continue
        except Exception:
            pass
        filtered_to_hunt.append(item)
        seen_hunt_tids.add(tid)
        if len(filtered_to_hunt) >= anime_quota:
            break

    for item in missing_candidates:
        if len(filtered_to_hunt) >= max_new_scrapes:
            break
        tid = item.get("id")
        if tid in seen_hunt_tids:
            continue
        try:
            if redis_sync.exists(f"riva:viral_series_miss:{tid}"):
                continue
        except Exception:
            pass
        filtered_to_hunt.append(item)
        seen_hunt_tids.add(tid)

    newly_scraped = 0
    if filtered_to_hunt:
        def _hunt_one_series(item: Dict[str, Any]):
            tid = int(item["id"])
            en_name = (item.get("name") or "").strip()
            orig_name = (item.get("original_name") or "").strip()
            air = item.get("first_air_date") or ""
            yr = int(air[:4]) if len(air) >= 4 and air[:4].isdigit() else None
            cat = _infer_series_category(item)

            ar_name = ""
            ar_overview = ""
            ar_poster = None
            ar_backdrop = None
            try:
                ar_d = _tmdb_get_json(f"tv/{tid}?language=ar-SA")
                ar_name = (ar_d.get("name") or "").strip()
                ar_overview = (ar_d.get("overview") or "").strip()
                ar_poster = ar_d.get("poster_path")
                ar_backdrop = ar_d.get("backdrop_path")
            except Exception:
                pass

            queries = []
            ordered_q = (ar_name, en_name, orig_name) if cat in ("arabic", "turkish") else (en_name, ar_name, orig_name)
            for q in ordered_q:
                if q and q not in queries and not re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", q):
                    queries.append(q)
                if q and ":" in q:
                    for part in q.split(":"):
                        p = part.strip()
                        if len(p) >= 5 and p not in queries and not re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", p):
                            queries.append(p)

            cl_eps = _hunt_cimalight_series_season1(queries)
            ak_eps = _hunt_akwam_series_season1(queries) if not cl_eps else {}

            merged_eps = cl_eps or ak_eps
            return item, yr, ar_name, ar_overview, ar_poster, ar_backdrop, merged_eps

        with ThreadPoolExecutor(max_workers=4) as pool:
            hunted_results = list(pool.map(_hunt_one_series, filtered_to_hunt))

        with SyncSessionLocal() as session:
            for item, yr, ar_name, ar_overview, ar_poster, ar_backdrop, eps_map in hunted_results:
                tid = int(item["id"])
                if not eps_map or 1 not in eps_map:
                    try:
                        redis_sync.set(f"riva:viral_series_miss:{tid}", "1", ex=43200)
                    except Exception:
                        pass
                    continue

                en_name = (item.get("name") or "").strip()
                orig_name = (item.get("original_name") or en_name).strip()
                cat = _infer_series_category(item)
                display_title = (
                    ar_name
                    if (cat in ("arabic", "turkish") and ar_name and re.search(r"[\u0600-\u06FF]", ar_name))
                    else (en_name or ar_name or orig_name)
                )

                if not ar_overview or len(re.findall(r"[\u0600-\u06FF]", ar_overview)) < 15:
                    ar_overview = ensure_arabic_overview(item.get("overview") or "") or f"مسلسل {display_title} الأكثر انتشاراً بجودة عالية."

                genres = [TV_GENRE_MAP[g] for g in (item.get("genre_ids") or []) if g in TV_GENRE_MAP] or ["Drama"]
                poster = item.get("poster_path") or ar_poster
                backdrop = item.get("backdrop_path") or ar_backdrop
                first_ep_url = eps_map[1][0]

                db_s = session.query(Series).filter_by(tmdb_id=tid).first()
                if not db_s:
                    for cand in session.query(Series).filter_by(category=cat).all():
                        if _normalize_series_key(cand.title) == _normalize_series_key(display_title):
                            db_s = cand
                            break

                if not db_s:
                    db_s = Series(
                        tmdb_id=tid,
                        source_url=first_ep_url,
                        slug=re.sub(r"\s+", "-", display_title.strip())[:120],
                        category=cat,
                        title=display_title,
                        original_title=orig_name,
                        overview=ar_overview,
                        year=yr,
                        poster_path=poster,
                        backdrop_path=backdrop,
                        vote_average=float(item.get("vote_average") or 0.0),
                        vote_count=int(item.get("vote_count") or 0),
                        popularity=float(item.get("popularity") or 0.0),
                        genres=genres,
                        status="ongoing",
                    )
                    session.add(db_s)
                    session.flush()
                    newly_scraped += 1
                else:
                    if not db_s.tmdb_id:
                        db_s.tmdb_id = tid
                    db_s.popularity = max(float(db_s.popularity or 0.0), float(item.get("popularity") or 0.0))
                    if poster and not db_s.poster_path:
                        db_s.poster_path = poster
                    if backdrop and not db_s.backdrop_path:
                        db_s.backdrop_path = backdrop
                    if ar_overview and not db_s.overview:
                        db_s.overview = ar_overview
                    session.flush()

                season_obj = session.query(Season).filter_by(series_id=db_s.id, season_number=1).first()
                if not season_obj:
                    season_obj = Season(
                        series_id=db_s.id,
                        season_number=1,
                        title="الموسم 1",
                        source_url=first_ep_url,
                    )
                    session.add(season_obj)
                    session.flush()

                for ep_num in sorted(eps_map.keys()):
                    ep_url, st_list = eps_map[ep_num]
                    db_ep = session.query(Episode).filter_by(season_id=season_obj.id, episode_number=ep_num).first()
                    if not db_ep:
                        dup_u = session.query(Episode).filter_by(source_url=ep_url).first()
                        if dup_u:
                            continue
                        db_ep = Episode(
                            season_id=season_obj.id,
                            episode_number=ep_num,
                            title=f"الحلقة {ep_num}",
                            source_url=ep_url,
                            status="active",
                        )
                        session.add(db_ep)
                        session.flush()

                    for s in st_list:
                        u = s.get("direct_url") or s.get("embed_url")
                        if not u:
                            continue
                        exists_sl = (
                            session.query(StreamingLink)
                            .filter(
                                StreamingLink.episode_id == db_ep.id,
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
                                    episode_id=db_ep.id,
                                    provider_name=s["provider_name"],
                                    embed_url=s["embed_url"],
                                    direct_url=u,
                                    quality=s.get("quality", "1080p"),
                                    status="active",
                                )
                            )
            session.commit()

    logger.info(
        f"🔥 Viral & Top-Rated Series Sync complete: {len(trending_ids)} live trending TV IDs, "
        f"{updated_existing} DB series refreshed, +{newly_scraped} brand-new viral/top-rated series scraped!"
    )
    return {
        "trending_ids": len(trending_ids),
        "updated_existing": updated_existing,
        "newly_scraped": newly_scraped,
    }

