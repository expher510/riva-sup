"""
Batch Dispatcher for Riva VOD Engine.
Strictly sequences all Series Episodes (S01E01 -> S01E02 -> ...) and Movie Franchises
(Part 1 -> Part 2 -> Part 3 -> ...) using sequential Celery chains, per-series/per-franchise
active locks, live CDN playability pre-flight probes, and automatic Akwam backfill.
"""
import os
import sys
import re
import logging
from collections import defaultdict
from typing import Tuple, List, Dict, Any

from celery import chain
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import StreamingLink
from worker.pipeline import (
    process_episode_transit_pipeline,
    process_video_transit_pipeline,
    rotate_candidates_by_provider,
    mark_stream_link_dead,
    redis_sync,
)
from worker.franchise_ordering import parse_movie_franchise_info, sort_franchise_movies

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("batch_dispatcher")

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True,
)
SyncSession = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)

# Priority mode: Complete all existing in-progress series first + Viral & Top-Rated new series/movies!
TURKISH_PRIORITY_ONLY = os.getenv("TURKISH_PRIORITY_ONLY", "1") == "1"
SERIES_VIRAL_PRIORITY_ENABLED = os.getenv("SERIES_VIRAL_PRIORITY_ENABLED", "1") == "1"
MOVIE_VIRAL_PRIORITY_ENABLED = os.getenv("MOVIE_VIRAL_PRIORITY_ENABLED", "1") == "1"
MAX_CONCURRENT_SERIES_CHAINS = int(os.getenv("MAX_CONCURRENT_SERIES_CHAINS", "4"))
MAX_CONCURRENT_MOVIE_JOBS = int(os.getenv("MAX_CONCURRENT_MOVIE_JOBS", "3"))

UNSUPPORTED_DOMAINS = (
    "1fichier", "openload", "estream", "streamsb",
    "forafile", "megaup", "1cloudfile", "bowfile", "send.now",
    "smoothpre", "minochinos", "streamruby", "stmruby",
    "rapidgator", "nitroflare", "luluvido", "vidmoly",
)

SQL_UNSUPPORTED_FILTER = """
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%1fichier%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%forafile%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%openload%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%estream%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%streamsb%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%megaup%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%1cloudfile%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%bowfile%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%send.now%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%smoothpre%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%minochinos%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%streamruby%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%rapidgator%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%nitroflare%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%luluvido%'
  AND coalesce(sl.direct_url, sl.embed_url) NOT LIKE '%vidmoly%'
"""


def _get_vip_series_ids(session) -> List[int]:
    """
    Returns top-priority VIP series IDs from:
      1. Environment variable VIP_SERIES_IDS (comma-separated IDs)
      2. Redis set `riva:vip_series_ids`
      3. Built-in VIP title match ('أحلى نصيب' / 'احلى نصيب' / 'احلي نصيب')
    VIP series bypass TURKISH_PRIORITY_ONLY filtering and rank #1 ahead of all other series!
    """
    vip_ids = set()
    raw_env = os.getenv("VIP_SERIES_IDS", "")
    for tok in raw_env.split(","):
        tok = tok.strip()
        if tok.isdigit():
            vip_ids.add(int(tok))
    try:
        for member in (redis_sync.smembers("riva:vip_series_ids") or []):
            if str(member).strip().isdigit():
                vip_ids.add(int(str(member).strip()))
    except Exception:
        pass
    try:
        rows = session.execute(
            text("SELECT id FROM series WHERE title ~* '(أحلى نصيب|احلى نصيب|احلي نصيب|نصف الأم|نصف الام|Anne Yar|طالع نازل|القصة الكاملة|القصه الكامله|جريمة في الزمالك)';")
        ).fetchall()
        for r in rows:
            vip_ids.add(int(r[0]))
    except Exception:
        pass
    return sorted(vip_ids)


def _get_vip_movie_ids(session) -> set:
    """
    Returns top-priority VIP user-requested movie IDs from:
      1. Environment variable VIP_MOVIE_IDS (comma-separated IDs)
      2. Redis set `riva:vip_movie_ids`
      3. Built-in VIP Watchlist match (e.g. 'You Can See Everything', tmdb_id=1765072)
    """
    vip_ids = set()
    raw_env = os.getenv("VIP_MOVIE_IDS", "")
    for tok in raw_env.split(","):
        tok = tok.strip()
        if tok.isdigit():
            vip_ids.add(int(tok))
    try:
        for member in (redis_sync.smembers("riva:vip_movie_ids") or []):
            if str(member).strip().isdigit():
                vip_ids.add(int(str(member).strip()))
    except Exception:
        pass
    try:
        rows = session.execute(
            text("SELECT id FROM movies WHERE tmdb_id = 1765072 OR title ILIKE 'You Can See Everything';")
        ).fetchall()
        for r in rows:
            vip_ids.add(int(r[0]))
    except Exception:
        pass
    return vip_ids


def filter_streamable_urls(urls: List[str]) -> List[str]:
    """Filters out known dead/paywall links and deduplicates aliases of the exact same provider file_code."""
    valid = []
    seen_provider_codes = set()
    for u in urls:
        if not u or not isinstance(u, str):
            continue
        u_clean = u.strip()
        if not u_clean.startswith("http"):
            continue
        u_lower = u_clean.lower()
        if any(bad in u_lower for bad in UNSUPPORTED_DOMAINS):
            continue

        # Deduplicate equivalent aliases of the same cyberlocker file code (e.g. hgcloud.to/X vs vibuxer.com/e/X)
        dedup_key = u_clean
        if "1vid." in u_lower:
            m = re.search(r"embed-([a-zA-Z0-9]+)", u_clean)
            if m:
                dedup_key = f"1vid:{m.group(1)}"
        elif "morencius." in u_lower:
            m = re.search(r"/embed/([a-zA-Z0-9]+)", u_clean)
            if m:
                dedup_key = f"morencius:{m.group(1)}"
        elif any(k in u_lower for k in ("hgcloud", "vibuxer", "hanerix", "streamwish", "vidhide", "byseraguci")):
            m = re.search(r"/(?:e|d|f|v)/([a-zA-Z0-9]{8,24})", u_clean) or re.search(r"https?://[^/]+/([a-zA-Z0-9]{10,24})(?:[/?#]|$)", u_clean)
            if m:
                dedup_key = f"hg:{m.group(1)}"
        elif any(k in u_lower for k in ("dood", "playmogo", "dsvplay", "myvidplay", "d000d")):
            m = re.search(r"/(?:d|e)/([a-zA-Z0-9]+)", u_clean)
            if m:
                dedup_key = f"dood:{m.group(1)}"
        elif any(k in u_lower for k in ("mixdrop", "mxdrop")):
            m = re.search(r"/(?:f|e)/([a-zA-Z0-9]+)", u_clean)
            if m:
                dedup_key = f"mix:{m.group(1)}"

        if dedup_key in seen_provider_codes:
            continue
        seen_provider_codes.add(dedup_key)
        valid.append(u_clean)
    return valid


def preflight_verify_stream_candidates(candidate_urls: List[str]) -> Tuple[List[str], List[str]]:
    """
    Verifies if candidate host links resolve AND respond with playable media bytes/playlists
    BEFORE dispatching to Celery.
    IMPORTANT: Returns the ORIGINAL database URLs (not ephemeral CDN tokens) so:
      1. Tokens never expire while waiting in sequential Celery chains.
      2. If a link later fails mid-download, mark_stream_link_dead() matches the exact DB URL!
    """
    from worker.media_resolver import verify_stream_playable

    live = []
    dead = []
    for u in candidate_urls:
        if not u or not u.startswith("http"):
            continue
        try:
            if verify_stream_playable(u, timeout=8):
                live.append(u)
                # Once we have 3 verified live mirrors across providers, no need to probe 10 more
                if len(live) >= 4:
                    break
            else:
                dead.append(u)
                mark_stream_link_dead(u, reason="Preflight probe failed (502/404/unresolvable)")
        except Exception as e:
            dead.append(u)
            mark_stream_link_dead(u, reason=f"Preflight error: {e}")

    return live, dead


def _try_refresh_akwam_links(
    session,
    source_url: str,
    movie_id: int = None,
    episode_id: int = None,
    series_title: str = None,
    sea_num: int = 1,
    ep_num: int = 1,
    movie_title: str = None,
    movie_orig_title: str = None,
    movie_year: int = None,
) -> List[str]:
    """If an item's existing links all died, attempts a fresh scrape of Akwam + cross-source Cimalight rescue."""
    try:
        from worker.scrapers.akwam_crawler import resolve_akwam_streams, search_akwam_episode_streams
        from worker.scrapers.cimalight_scraper import (
            resolve_cimalight_episode_streams,
            search_cimalight_episode_streams,
            search_cimalight_movie_streams,
        )

        existing_rows = (
            session.query(StreamingLink)
            .filter(StreamingLink.movie_id == movie_id if movie_id else StreamingLink.episode_id == episode_id)
            .all()
        )
        known_dead_urls = set()
        for r in existing_rows:
            if r.status == "dead":
                if r.direct_url:
                    known_dead_urls.add(r.direct_url.strip())
                if r.embed_url:
                    known_dead_urls.add(r.embed_url.strip())

        def _only_non_dead(st_list):
            out = []
            for s in st_list or []:
                u = (s.get("direct_url") or s.get("embed_url") or "").strip()
                emb = (s.get("embed_url") or "").strip()
                if not u or u in known_dead_urls or (emb and emb in known_dead_urls):
                    continue
                out.append(s)
            return out

        fresh_streams = []
        matched_movie_post = None
        if source_url and "cimalight." in source_url:
            fresh_streams.extend(_only_non_dead(resolve_cimalight_episode_streams(source_url)))
        if source_url and "akwams." in source_url:
            fresh_streams.extend(_only_non_dead(resolve_akwam_streams(source_url)))

        if not fresh_streams and episode_id and series_title:
            fresh_streams.extend(_only_non_dead(search_cimalight_episode_streams(series_title, sea_num, ep_num)))
            if not fresh_streams:
                fresh_streams.extend(_only_non_dead(search_akwam_episode_streams(series_title, sea_num, ep_num)))

        if not fresh_streams and movie_id and (movie_title or movie_orig_title):
            from worker.scrapers.viral_movies_scraper import search_akwam_movie_streams
            matched_movie_post, hunted_streams = search_akwam_movie_streams(
                movie_title or movie_orig_title,
                year=movie_year,
                orig_title=movie_orig_title,
            )
            fresh_streams.extend(_only_non_dead(hunted_streams))
            if not fresh_streams:
                cl_post, cl_streams = search_cimalight_movie_streams(
                    movie_title or movie_orig_title,
                    year=movie_year,
                    orig_title=movie_orig_title,
                )
                fresh_streams.extend(_only_non_dead(cl_streams))
                if cl_post and not matched_movie_post:
                    matched_movie_post = cl_post

        if matched_movie_post and movie_id:
            session.execute(
                text("UPDATE movies SET source_url = :u WHERE id = :mid AND source_url IS NULL;"),
                {"u": matched_movie_post, "mid": movie_id},
            )

        all_candidate_urls = []
        for s in fresh_streams:
            u = s.get("direct_url") or s.get("embed_url")
            if not u:
                continue
            all_candidate_urls.append(u)
            exists = (
                session.query(StreamingLink)
                .filter(
                    StreamingLink.movie_id == movie_id if movie_id else StreamingLink.episode_id == episode_id,
                    (StreamingLink.direct_url == u) | (StreamingLink.embed_url == s["embed_url"]),
                )
                .first()
            )
            if not exists:
                session.add(
                    StreamingLink(
                        movie_id=movie_id,
                        episode_id=episode_id,
                        provider_name=s["provider_name"],
                        embed_url=s["embed_url"],
                        direct_url=s.get("direct_url"),
                        quality=s.get("quality", "1080p"),
                        status="active",
                    )
                )
        if fresh_streams:
            session.commit()
            logger.info(
                f"  ♻️ Rescued {len(all_candidate_urls)} fresh mirror links (Akwam/Cimalight) for {'Movie ' + str(movie_id) if movie_id else 'Episode ' + str(episode_id)}"
            )
        return filter_streamable_urls(all_candidate_urls)
    except Exception as e:
        logger.debug(f"Could not refresh links from {source_url}: {e}")
        return []


def _is_series_busy_in_redis(session, series_id: int) -> bool:
    """Returns True if ANY episode of series_id is currently queued or executing in Celery."""
    if redis_sync.exists(f"riva:lock:series_chain:{series_id}"):
        return True
    ep_rows = session.execute(
        text("""
            SELECT e.id, sea.season_number, e.episode_number
            FROM episodes e
            JOIN seasons sea ON sea.id = e.season_id
            WHERE sea.series_id = :sid;
        """),
        {"sid": series_id},
    ).fetchall()
    for eid, s_num, e_num in ep_rows:
        if (
            redis_sync.exists(f"riva:dispatched:ep:{eid}")
            or redis_sync.exists(f"riva:lock:ep:{eid}")
            or redis_sync.exists(f"riva:lock:ep_slot:{series_id}_{s_num}_{e_num}")
        ):
            return True
    return False


def dispatch_pending_episodes(limit: int = 6) -> int:
    """
    Dispatches series episodes in 100% strict sequential order (S01E01 -> S01E02 -> ...):
      - Runs up to MAX_CONCURRENT_SERIES_CHAINS DIFFERENT series concurrently across Celery workers,
        while strictly enforcing 1 sequential chain per series (never 2 chains for the same series!).
      - When TURKISH_PRIORITY_ONLY=1:
          * Priority 0: Complete ALL Turkish series currently visible in the bot (archived_count > 0)
            from Episode 1 through the very last episode!
          * Priority 1: Remaining Turkish series from Episode 1 through the last episode.
      - Auto-backfills full episode lists (Ep 1..Last) from Akwam search + episode pages.
    """
    try:
        q_len = redis_sync.llen("celery") or 0
        max_q = MAX_CONCURRENT_SERIES_CHAINS * max(limit, 4)
        if q_len >= max_q:
            logger.info(f"Celery queue currently has {q_len} pending tasks. Skipping episode dispatch until queue drains.")
            return 0
    except Exception:
        pass

    with SyncSession() as session:
        vip_ids = _get_vip_series_ids(session)
        vip_set = set(vip_ids)
        vip_sql_list = ",".join(str(x) for x in vip_ids) if vip_ids else "-1"

        try:
            trending_series_tids = [
                int(str(x).strip())
                for x in (redis_sync.smembers("riva:tmdb_trending_series_ids") or [])
                if str(x).strip().isdigit()
            ]
        except Exception:
            trending_series_tids = []
        trending_sql_list = ",".join(str(x) for x in trending_series_tids) if trending_series_tids else "-1"

        if SERIES_VIRAL_PRIORITY_ENABLED:
            series_filter_where = f"""
              AND (
                  id IN ({vip_sql_list})
                  OR archived_count > 0
                  OR (
                      has_s01e01 = TRUE
                      AND (
                          tmdb_id IN ({trending_sql_list})
                          OR pop >= 15.0
                          OR vote_cnt >= 15
                          OR vote_avg >= 7.2
                      )
                  )
              )
            """
        elif TURKISH_PRIORITY_ONLY:
            series_filter_where = f"""
              AND (
                  id IN ({vip_sql_list})
                  OR category = 'turkish'
                  OR title ~* '(تركي|تركية)'
              )
            """
        else:
            series_filter_where = ""

        candidate_series = session.execute(text(f"""
            WITH series_stats AS (
                SELECT s.id,
                       s.title,
                       s.category,
                       s.tmdb_id,
                       COALESCE(s.popularity, 0) AS pop,
                       COALESCE(s.vote_average, 0) AS vote_avg,
                       COALESCE(s.vote_count, 0) AS vote_cnt,
                       (SELECT count(*) FROM episodes ep_p
                        JOIN seasons se_p ON se_p.id = ep_p.season_id
                        WHERE se_p.series_id = s.id
                          AND ep_p.id NOT IN (
                              SELECT episode_id FROM telegram_archives
                              WHERE episode_id IS NOT NULL AND file_size_bytes > 15000000
                          )) AS pending_count,
                       (SELECT count(*) FROM telegram_archives ta
                        JOIN episodes ea ON ea.id = ta.episode_id
                        JOIN seasons sa ON sa.id = ea.season_id
                        WHERE sa.series_id = s.id AND ta.file_size_bytes > 15000000) AS archived_count,
                       EXISTS(
                           SELECT 1 FROM episodes e1
                           JOIN seasons s1 ON s1.id = e1.season_id
                           WHERE s1.series_id = s.id AND s1.season_number = 1 AND e1.episode_number = 1
                       ) AS has_s01e01
                FROM series s
                WHERE EXISTS (
                    SELECT 1 FROM seasons sea_chk
                    JOIN episodes ep_chk ON ep_chk.season_id = sea_chk.id
                    WHERE sea_chk.series_id = s.id
                )
            )
            SELECT id,
                   title,
                   category,
                   tmdb_id,
                   pop,
                   vote_avg,
                   vote_cnt,
                   pending_count,
                   archived_count,
                   has_s01e01
            FROM series_stats
            WHERE pending_count > 0
              {series_filter_where}
            ORDER BY (CASE WHEN id IN ({vip_sql_list}) THEN 1 ELSE 0 END) DESC,
                     (
                         (CASE WHEN tmdb_id IN ({trending_sql_list}) THEN 700.0 ELSE 0.0 END)
                         + LEAST(pop, 600.0) * 1.8
                         + (CASE WHEN vote_cnt >= 15 THEN vote_avg * 42.0 ELSE vote_avg * 12.0 END)
                         + LEAST(vote_cnt, 25000) / 45.0
                         + (CASE WHEN archived_count > 0 THEN 220.0 ELSE 0.0 END)
                     ) DESC,
                     id ASC
            LIMIT 160;
        """)).fetchall()

        if not candidate_series:
            logger.info("No series with pending episodes found to dispatch.")
            return 0

        trending_set = set(trending_series_tids)
        vip_lane = []
        viral_tmdb_lane = []
        anime_lane = []
        complete_existing_lane = []
        general_viral_lane = []

        for r in candidate_series:
            sid_r, title_r, cat_r, tid_r, pop_r, vavg_r, vcnt_r, pend_r, arch_r, has_e1_r = r
            pop_f = float(pop_r or 0.0)
            vavg_f = float(vavg_r or 0.0)
            vcnt_i = int(vcnt_r or 0)
            pend_i = int(pend_r or 0)
            arch_i = int(arch_r or 0)
            in_tr = bool(tid_r and int(tid_r) in trending_set)

            v_score = (
                (700.0 if in_tr else 0.0)
                + min(pop_f, 600.0) * 1.8
                + (vavg_f * 45.0 if vcnt_i >= 15 else vavg_f * 12.0)
                + min(vcnt_i, 25000) / 45.0
            )
            item_tup = (sid_r, title_r, cat_r, pend_i, arch_i, bool(has_e1_r))

            if sid_r in vip_set:
                vip_lane.append((-(v_score + 10000.0), item_tup))
            elif (cat_r or "").lower() == "anime":
                # Finish active medium-length anime cleanly, while launching top TMDB/Viral anime from S01E01
                an_priority = (1 if (arch_i > 0 and pend_i <= 35) else 0, v_score)
                anime_lane.append((an_priority, item_tup))
            elif in_tr or pop_f >= 80.0 or (vavg_f >= 8.2 and vcnt_i >= 250):
                # Once an elite viral/TMDB series starts (arch_i > 0 and manageable remaining eps), finish it completely!
                vr_priority = (1 if (arch_i > 0 and pend_i <= 40) else 0, v_score)
                viral_tmdb_lane.append((vr_priority, item_tup))
            elif arch_i > 0:
                ip_priority = (1 if pend_i <= 12 else 0, v_score)
                complete_existing_lane.append((ip_priority, item_tup))
            else:
                general_viral_lane.append((v_score, item_tup))

        vip_lane.sort(key=lambda x: x[0])
        viral_tmdb_lane.sort(key=lambda x: x[0], reverse=True)
        anime_lane.sort(key=lambda x: x[0], reverse=True)
        complete_existing_lane.sort(key=lambda x: x[0], reverse=True)
        general_viral_lane.sort(key=lambda x: x[0], reverse=True)

        interleaved_series = []
        seen_sids = set()

        def _push_s(tup):
            if tup[0] not in seen_sids:
                seen_sids.add(tup[0])
                interleaved_series.append(tup)

        for _, it in vip_lane:
            _push_s(it)

        max_s_lanes = max(len(viral_tmdb_lane), len(anime_lane), len(complete_existing_lane))
        for idx in range(max_s_lanes):
            if idx < len(viral_tmdb_lane):
                _push_s(viral_tmdb_lane[idx][1])
            if idx < len(anime_lane):
                _push_s(anime_lane[idx][1])
            if idx < len(complete_existing_lane):
                _push_s(complete_existing_lane[idx][1])

        for _, it in general_viral_lane:
            _push_s(it)

        # Count how many series currently have an active chain executing in Redis/Celery
        busy_series_ids = set()
        for row in interleaved_series:
            sid_chk = row[0]
            if _is_series_busy_in_redis(session, sid_chk):
                busy_series_ids.add(sid_chk)

        has_idle_vip = any(
            (r[0] in vip_set and r[0] not in busy_series_ids and int(r[3] or 0) > 0)
            for r in interleaved_series
        )
        effective_max_chains = MAX_CONCURRENT_SERIES_CHAINS + (len(vip_set) if has_idle_vip else 0)

        active_chains_count = len(busy_series_ids)
        if active_chains_count >= effective_max_chains:
            logger.info(
                f"⚡ All {active_chains_count}/{effective_max_chains} parallel series chain slots are actively uploading."
            )
            return 0

        total_episodes_dispatched = 0

        for series_id, series_title, cat_code, pending_count, archived_count, has_s01e01 in interleaved_series:
            is_vip_series = series_id in vip_set
            if active_chains_count >= effective_max_chains and not is_vip_series:
                break

            if series_id in busy_series_ids:
                continue

            if redis_sync.exists(f"riva:series_blocked:{series_id}") and not is_vip_series:
                continue

            # Run full episode discovery (Ep 1..Last) once per 6h for every prioritized series
            # or whenever S01E01 is missing, so missing early/middle/late episodes are all backfilled!
            need_full_backfill = (
                not redis_sync.exists(f"riva:akwam:full_backfill_v4:{series_id}")
                or (not has_s01e01 and not redis_sync.exists(f"riva:akwam:backfilled_series:{series_id}"))
            )
            if need_full_backfill:
                sample_url = session.execute(
                    text("""
                        SELECT e.source_url
                        FROM episodes e
                        JOIN seasons sea ON sea.id = e.season_id
                        WHERE sea.series_id = :sid AND e.source_url IS NOT NULL
                        ORDER BY sea.season_number ASC, e.episode_number ASC
                        LIMIT 1;
                    """),
                    {"sid": series_id},
                ).scalar() or ""
                redis_sync.set(f"riva:akwam:full_backfill_v4:{series_id}", "1", ex=21600)
                redis_sync.set(f"riva:akwam:backfilled_series:{series_id}", "1", ex=21600)
                if "cimalight." in sample_url:
                    try:
                        from worker.scrapers.cimalight_scraper import sync_cimalight_series_to_db
                        sync_cimalight_series_to_db(series_title, cat_code or "arabic", 2026, is_vip_series)
                    except Exception as cl_err:
                        logger.debug(f"Cimalight backfill skipped for '{series_title}': {cl_err}")
                elif "akwams." in sample_url or not has_s01e01:
                    logger.info(f"  🔍 Full Akwam episode discovery (1..Last) for '{series_title}' (ID {series_id})...")
                    from worker.scrapers.akwam_crawler import backfill_akwam_series_episodes_sync
                    backfill_akwam_series_episodes_sync(sample_url, series_id, cat_code or "western")

            # Fetch ALL episodes of this series in strict Season ASC, Episode ASC order (archived + unarchived)
            all_eps = session.execute(
                text("""
                    SELECT e.id,
                           e.title,
                           sea.season_number,
                           e.episode_number,
                           e.source_url,
                           EXISTS(
                               SELECT 1 FROM telegram_archives ta
                               WHERE ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                           ) AS is_archived
                    FROM episodes e
                    JOIN seasons sea ON sea.id = e.season_id
                    WHERE sea.series_id = :sid
                    ORDER BY sea.season_number ASC, e.episode_number ASC, e.id ASC;
                """),
                {"sid": series_id},
            ).fetchall()

            if not all_eps:
                continue

            # If ALL episodes currently in DB for this series are already archived, move on cleanly!
            if all(bool(r[5]) for r in all_eps):
                continue

            # Check if a brand-new series starts cleanly at Episode 1 (unless already in bot OR full search confirmed no Ep 1 exists on Akwam)
            first_sea, first_ep = all_eps[0][2], all_eps[0][3]
            if (
                archived_count == 0
                and first_ep > 1
                and not redis_sync.exists(f"riva:akwam:tr_full_backfill_v3:{series_id}")
            ):
                logger.info(
                    f"  ⏸️ Series '{series_title}' (ID {series_id}) earliest episode in DB is S{first_sea:02d}E{first_ep:02d} (missing Ep 1). Holding until Ep 1 is scraped."
                )
                redis_sync.set(f"riva:series_blocked:{series_id}", "missing_ep1", ex=1800)
                continue

            tasks = []
            seen_slots = set()
            blocked_on_missing_link = False

            for ep_id, ep_title, sea_num, ep_num, ep_source_url, is_archived in all_eps:
                slot = (sea_num, ep_num)
                if slot in seen_slots:
                    continue
                seen_slots.add(slot)

                if is_archived:
                    continue

                if redis_sync.exists(f"riva:ep_source_unavailable:{ep_id}"):
                    continue

                if len(tasks) >= limit:
                    break

                # Fetch active streamable links for this exact next episode
                raw_urls = [
                    r[0]
                    for r in session.execute(
                        text(f"""
                            SELECT DISTINCT coalesce(sl.direct_url, sl.embed_url)
                            FROM streaming_links sl
                            WHERE sl.episode_id = :eid
                              AND (sl.direct_url IS NOT NULL OR sl.embed_url IS NOT NULL)
                              AND coalesce(sl.status, 'active') = 'active'
                              {SQL_UNSUPPORTED_FILTER};
                        """),
                        {"eid": ep_id},
                    ).fetchall()
                ]

                clean_candidates = filter_streamable_urls(raw_urls)
                live_candidates, _ = preflight_verify_stream_candidates(clean_candidates) if clean_candidates else ([], [])

                if not live_candidates:
                    # If we already queued earlier episodes in this chain, dispatch them immediately without waiting!
                    if tasks:
                        break
                    refreshed = _try_refresh_akwam_links(
                        session,
                        ep_source_url or "",
                        episode_id=ep_id,
                        series_title=series_title,
                        sea_num=sea_num,
                        ep_num=ep_num,
                    )
                    if refreshed:
                        live_candidates, _ = preflight_verify_stream_candidates(refreshed)

                if not live_candidates:
                    dead_rounds = redis_sync.incr(f"riva:ep_source_dead_cnt:{ep_id}")
                    redis_sync.expire(f"riva:ep_source_dead_cnt:{ep_id}", 86400)
                    if dead_rounds >= 2:
                        logger.warning(
                            f"  ⚠️ Episode S{sea_num:02d}E{ep_num:02d} (Ep {ep_id}) of '{series_title}' confirmed 0 live mirrors across {dead_rounds} rescue rounds (Akwam+Cimalight). Holding series for 6h to preserve order!"
                        )
                        redis_sync.set(f"riva:ep_source_unavailable:{ep_id}", "1", ex=21600)
                        redis_sync.set(f"riva:series_blocked:{series_id}", "dead_episode_source", ex=21600)
                        try:
                            if redis_sync.set(f"riva:notified_dead_ep:{ep_id}", "1", nx=True, ex=43200):
                                from worker.pipeline import format_episode_alert_details
                                from worker.telegram_vault import send_telegram_notification
                                send_telegram_notification(
                                    f"<b>لا توجد سيرفرات شغالة للحلقة على المصادر الحالية (تحتاج مصدر بديل):</b>\n"
                                    f"{format_episode_alert_details(ep_id)}\n"
                                    f"⚠️ <b>السبب:</b> جميع روابط المشاهدة والتحميل للحلقة متوقفة بعد جولتي فحص وتحديث (أكوام + سيما لايت).",
                                    level="warning",
                                )
                        except Exception:
                            pass
                        blocked_on_missing_link = True
                        break
                    logger.warning(
                        f"  ⛔ Series '{series_title}' paused briefly at S{sea_num:02d}E{ep_num:02d} (Ep {ep_id}): 0 playable streams (round {dead_rounds}/2)."
                    )
                    blocked_on_missing_link = True
                    break

                rotated_urls = rotate_candidates_by_provider(live_candidates)
                redis_sync.set(f"riva:dispatched:ep:{ep_id}", "1", ex=3600)
                tasks.append(process_episode_transit_pipeline.si(ep_id, rotated_urls, quality="1080p"))
                logger.info(
                    f"  📋 Queued strictly in order: {series_title} | S{sea_num:02d}E{ep_num:02d} (Ep {ep_id}) [✅ {len(rotated_urls)} live mirrors]"
                )

            if tasks:
                redis_sync.set(f"riva:lock:series_chain:{series_id}", "1", ex=900)
                chain(*tasks).apply_async()
                active_chains_count += 1
                total_episodes_dispatched += len(tasks)
                logger.info(
                    f"🚀 Dispatched sequential chain ({active_chains_count}/{MAX_CONCURRENT_SERIES_CHAINS}) of {len(tasks)} contiguous episodes for '{series_title}'."
                )
            elif blocked_on_missing_link:
                if not redis_sync.exists(f"riva:series_blocked:{series_id}"):
                    redis_sync.set(f"riva:series_blocked:{series_id}", "dead_links", ex=180)

        return total_episodes_dispatched


def _is_franchise_busy_in_redis(franchise_key: str, group_movies: List[Dict[str, Any]]) -> bool:
    """Returns True if any movie in this franchise group is already queued or running."""
    if redis_sync.exists(f"riva:lock:franchise:{franchise_key}"):
        return True
    for m in group_movies:
        mid = m["id"]
        nt = m["norm_title"]
        if (
            redis_sync.exists(f"riva:dispatched:movie:{mid}")
            or redis_sync.exists(f"riva:lock:movie:{mid}")
            or (nt and (redis_sync.exists(f"riva:dispatched:movie_title:{nt}") or redis_sync.exists(f"riva:lock:movie_title:{nt}")))
        ):
            return True
    return False


def _count_active_movie_jobs() -> int:
    """
    Counts movies currently queued or downloading/uploading in Celery,
    automatically reaping orphaned `riva:dispatched:movie:*` keys left behind by worker restarts
    or aborted chains when no worker holds `riva:lock:movie:{mid}` and the Celery queue is empty.
    """
    try:
        q_len = redis_sync.llen("celery") or 0
        active = 0
        for key in list(redis_sync.scan_iter("riva:dispatched:movie:[0-9]*", count=200)):
            mid_str = str(key).rsplit(":", 1)[-1]
            has_worker_lock = bool(redis_sync.exists(f"riva:lock:movie:{mid_str}"))
            ttl = redis_sync.ttl(key)
            # Initial TTL is 2400s (40m). If >5m elapsed (ttl < 2100), OR legacy key (>2400s),
            # AND Celery queue is empty AND no worker holds the movie lock -> reap stale key!
            if not has_worker_lock and q_len == 0 and (ttl > 2400 or (0 <= ttl < 2100)):
                redis_sync.delete(key)
                continue
            active += 1
        return active
    except Exception:
        return 0


def dispatch_pending_movies(limit: int = 4) -> int:
    """
    Dispatches movies with full Franchise / Collection awareness (Part 1 -> Part 2 -> Part 3)
    while interleaving:
      - Lane 0 (🎯 VIP): User-requested VIP Watchlist movies (e.g. 'You Can See Everything' 2026)
      - Lane 1 (🔥 VIRAL): Most Viral & Live TMDB Trending / Popular hits
      - Lane 2 (⭐ TOP-RATED): All-Time Highest-Rated Masterpieces (Bayesian weighted 8.0+ / 7.5+ classics)
      - Lane 3 (🔗 FRANCHISE / FAME): Partially uploaded collections & remaining famous movies
    """
    if TURKISH_PRIORITY_ONLY and not MOVIE_VIRAL_PRIORITY_ENABLED:
        return 0

    try:
        q_len = redis_sync.llen("celery") or 0
        if q_len >= 16:
            logger.info(f"Celery queue currently has {q_len} pending tasks. Skipping movie dispatch until queue drains.")
            return 0
    except Exception:
        pass

    active_movie_jobs = _count_active_movie_jobs()
    if active_movie_jobs >= MAX_CONCURRENT_MOVIE_JOBS:
        logger.info(
            f"🎬 All {active_movie_jobs}/{MAX_CONCURRENT_MOVIE_JOBS} parallel Viral/Top-Rated movie slots are actively uploading."
        )
        return 0

    effective_limit = min(limit, max(1, MAX_CONCURRENT_MOVIE_JOBS - active_movie_jobs))

    with SyncSession() as session:
        vip_movie_ids = _get_vip_movie_ids(session)
        try:
            trending_tmdb_ids = {
                int(str(x).strip())
                for x in (redis_sync.smembers("riva:tmdb_trending_movie_ids") or [])
                if str(x).strip().isdigit()
            }
        except Exception:
            trending_tmdb_ids = set()

        all_movie_rows = session.execute(text(f"""
            SELECT m.id,
                   m.tmdb_id,
                   m.title,
                   m.original_title,
                   m.year,
                   m.source_url,
                   m.category,
                   coalesce(m.vote_average, 0),
                   coalesce(m.vote_count, 0),
                   coalesce(m.popularity, 0),
                   EXISTS(
                       SELECT 1 FROM telegram_archives ta
                       WHERE ta.movie_id = m.id AND ta.file_size_bytes > 15000000
                   ) AS is_archived,
                   EXISTS(
                       SELECT 1 FROM streaming_links sl
                       WHERE sl.movie_id = m.id
                         AND (sl.direct_url IS NOT NULL OR sl.embed_url IS NOT NULL)
                         AND coalesce(sl.status, 'active') = 'active'
                         {SQL_UNSUPPORTED_FILTER}
                   ) AS has_active_links
            FROM movies m
            WHERE m.title IS NOT NULL
              AND trim(m.title) != ''
              AND m.title != 'مشاهدة';
        """)).fetchall()

        if not all_movie_rows:
            logger.info("No movies found in DB.")
            return 0

        # Build franchise groups across ALL movies (archived + pending) so we know every existing part!
        franchises: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        archived_norm_titles = set()
        for mid, tmdb_id, title, orig_title, yr, src_url, cat, v_avg, v_cnt, pop, is_arch, has_links in all_movie_rows:
            nt = re.sub(r"\s+", " ", (title or "").strip().lower())
            if is_arch and nt:
                archived_norm_titles.add(nt)
            info = parse_movie_franchise_info(title, original_title=orig_title, year=yr)
            franchises[info["franchise_key"]].append({
                "id": int(mid),
                "tmdb_id": int(tmdb_id) if tmdb_id else None,
                "title": title,
                "norm_title": nt,
                "original_title": orig_title,
                "year": int(yr) if yr else None,
                "source_url": src_url,
                "category": cat or "western",
                "vote_average": float(v_avg or 0.0),
                "vote_count": int(v_cnt or 0),
                "popularity": float(pop or 0.0),
                "is_archived": bool(is_arch),
                "has_active_links": bool(has_links),
                "info": info,
            })

        # Propagate title-level archive status across duplicate DB rows
        for f_key, items in franchises.items():
            for it in items:
                if it["norm_title"] in archived_norm_titles:
                    it["is_archived"] = True

        vip_units = []
        viral_units = []
        anime_units = []
        top_rated_units = []
        in_progress_units = []
        general_units = []

        for f_key, items in franchises.items():
            ordered = sort_franchise_movies(items)
            deduped = []
            seen_nt = set()
            for it in ordered:
                if it["norm_title"] in seen_nt:
                    continue
                seen_nt.add(it["norm_title"])
                deduped.append(it)

            unarchived = [x for x in deduped if not x["is_archived"]]
            if not unarchived:
                continue

            is_vip = any(x["id"] in vip_movie_ids for x in unarchived)
            is_anime_mv = any((x.get("category") or "").lower() == "anime" for x in unarchived)
            in_trending = any((x["tmdb_id"] in trending_tmdb_ids) for x in unarchived if x["tmdb_id"])
            best_pop = max(x["popularity"] for x in deduped)
            best_rate = max(x["vote_average"] for x in deduped)
            best_votes = max(x["vote_count"] for x in deduped)
            newest_yr = max((x["year"] or 0) for x in unarchived)

            # Allow high-value viral/top-rated/VIP/Anime movies without active links to attempt Akwam+Cimalight search rescue
            has_any_active = any(x["has_active_links"] for x in unarchived)
            high_value_rescue_eligible = (
                is_vip
                or in_trending
                or best_pop >= 90.0
                or (best_rate >= 8.0 and best_votes >= 800)
                or (is_anime_mv and (best_rate >= 7.8 or best_pop >= 35.0))
            )
            if not has_any_active and not high_value_rescue_eligible:
                continue

            # Skip standalone items whose Akwam rescue already confirmed 0 mirrors recently
            if len(unarchived) == 1 and redis_sync.exists(f"riva:movie_dead_cooldown:{unarchived[0]['id']}"):
                continue

            is_multi = len(deduped) >= 2 or any(x["info"]["is_explicit_sequel"] for x in deduped)
            has_archived_part = any(x["is_archived"] for x in deduped)

            # Viral score: Live TMDB trending + 2024-2026 buzz + TMDB popularity + audience reach
            viral_score = (
                (650.0 if in_trending else 0.0)
                + (240.0 if newest_yr >= 2025 else (120.0 if newest_yr == 2024 else 0.0))
                + min(best_pop, 1000.0) * 1.8
                + (best_rate * 25.0 if best_votes >= 20 else best_rate * 8.0)
                + min(best_votes, 25000) / 80.0
                + (80.0 if has_any_active else 0.0)
            )

            # Top-Rated score: IMDB Bayesian Weighted Rating WR = (v/(v+250))*R + (250/(v+250))*7.0
            v = float(max(best_votes, 0))
            wr = ((v / (v + 250.0)) * best_rate + (250.0 / (v + 250.0)) * 7.0) if best_rate > 0 else 0.0
            rating_score = (
                wr * 150.0
                + min(best_votes, 35000) / 35.0
                + min(best_pop, 400.0) * 0.6
                + (80.0 if has_any_active else 0.0)
            )

            fame_score = best_pop * 1.4 + min(best_votes, 30000) / 50.0 + best_rate * 12.0

            if is_vip:
                vip_only_items = [x for x in unarchived if x["id"] in vip_movie_ids]
                for vip_it in vip_only_items:
                    vip_units.append((-(viral_score + rating_score + 10000.0), "🎯 VIP", f"{f_key}:vip:{vip_it['id']}", False, [vip_it]))
                non_vip_remaining = [x for x in unarchived if x["id"] not in vip_movie_ids]
                if not non_vip_remaining:
                    continue

            if is_anime_mv:
                anime_units.append((-(viral_score + rating_score), "🎌 ANIME", f_key, is_multi, deduped))
            if in_trending or (newest_yr >= 2024 and best_pop >= 30.0) or best_pop >= 75.0:
                viral_units.append((-viral_score, "🔥 VIRAL", f_key, is_multi, deduped))
            if best_rate >= 7.2 and (best_votes >= 50 or best_rate >= 8.0):
                top_rated_units.append((-rating_score, "⭐ TOP-RATED", f_key, is_multi, deduped))
            if is_multi and has_archived_part and has_any_active:
                in_progress_units.append((-fame_score, "🔗 FRANCHISE", f_key, is_multi, deduped))
            elif has_any_active:
                general_units.append((-fame_score, "🎬 FAMOUS", f_key, is_multi, deduped))

        vip_units.sort(key=lambda x: x[0])
        viral_units.sort(key=lambda x: x[0])
        anime_units.sort(key=lambda x: x[0])
        top_rated_units.sort(key=lambda x: x[0])
        in_progress_units.sort(key=lambda x: x[0])
        general_units.sort(key=lambda x: x[0])

        # Interleave: VIP -> 1 Viral + 1 Top-Rated + 1 Anime + (every 2nd round: 1 In-Progress Franchise) -> General
        work_units = []
        seen_fkeys = set()

        def _push_unit(u):
            fk = u[2]
            if fk not in seen_fkeys:
                seen_fkeys.add(fk)
                work_units.append(u)

        for u in vip_units:
            _push_unit(u)

        max_lane_len = max(len(viral_units), len(top_rated_units), len(anime_units), len(in_progress_units))
        ip_idx = 0
        for idx in range(max_lane_len):
            if idx < len(viral_units):
                _push_unit(viral_units[idx])
            if idx < len(top_rated_units):
                _push_unit(top_rated_units[idx])
            if idx < len(anime_units):
                _push_unit(anime_units[idx])
            if idx % 2 == 1 and ip_idx < len(in_progress_units):
                _push_unit(in_progress_units[ip_idx])
                ip_idx += 1

        while ip_idx < len(in_progress_units):
            _push_unit(in_progress_units[ip_idx])
            ip_idx += 1

        for u in general_units:
            _push_unit(u)

        total_dispatched = 0
        akwam_rescues_used = 0
        max_akwam_rescues = 4

        for _, lane_tag, f_key, is_multi, group in work_units:
            if total_dispatched >= effective_limit:
                break

            if redis_sync.exists(f"riva:franchise_blocked:{f_key}") and lane_tag != "🎯 VIP":
                continue

            if _is_franchise_busy_in_redis(f_key, group):
                continue

            # If this is an explicit sequel (e.g. Part 2) and Part 1 isn't in DB yet, try Akwam franchise backfill once!
            first_item = group[0]
            if (
                is_multi
                and not first_item["is_archived"]
                and first_item["info"]["effective_part"] > 1
                and not redis_sync.exists(f"riva:akwam:backfilled_franchise:{f_key}")
            ):
                redis_sync.set(f"riva:akwam:backfilled_franchise:{f_key}", "1", ex=86400)
                sample_src = next((x["source_url"] for x in group if x.get("source_url") and "akwams." in x["source_url"]), None)
                if sample_src:
                    from worker.scrapers.akwam_crawler import backfill_akwam_movie_franchise_sync
                    added = backfill_akwam_movie_franchise_sync(sample_src, f_key, first_item["category"])
                    if added > 0:
                        logger.info(f"  🔄 Backfilled {added} sibling part(s) for franchise '{f_key}'. Reloading in strict order!")
                        new_rows = session.execute(
                            text("""
                                SELECT id, tmdb_id, title, original_title, year, source_url, category,
                                       coalesce(vote_average, 0), coalesce(vote_count, 0), coalesce(popularity, 0)
                                FROM movies
                                WHERE category = :cat
                                ORDER BY id DESC LIMIT 20;
                            """),
                            {"cat": first_item["category"]},
                        ).fetchall()
                        existing_ids = {x["id"] for x in group}
                        for n_id, n_tid, n_t, n_ot, n_yr, n_src, n_cat, n_va, n_vc, n_pop in new_rows:
                            if n_id in existing_ids:
                                continue
                            n_info = parse_movie_franchise_info(n_t, original_title=n_ot, year=n_yr)
                            if n_info["franchise_key"] == f_key:
                                group.append({
                                    "id": int(n_id),
                                    "tmdb_id": int(n_tid) if n_tid else None,
                                    "title": n_t,
                                    "norm_title": re.sub(r"\s+", " ", (n_t or "").strip().lower()),
                                    "original_title": n_ot,
                                    "year": int(n_yr) if n_yr else None,
                                    "source_url": n_src,
                                    "category": n_cat or "western",
                                    "vote_average": float(n_va or 0.0),
                                    "vote_count": int(n_vc or 0),
                                    "popularity": float(n_pop or 0.0),
                                    "is_archived": False,
                                    "has_active_links": True,
                                    "info": n_info,
                                })
                        group = sort_franchise_movies(group)

            chain_tasks = []
            halted_franchise = False

            for item in group:
                if item["is_archived"]:
                    continue
                if total_dispatched + len(chain_tasks) >= effective_limit:
                    break

                m_id = item["id"]
                m_title = item["title"]
                norm_title = item["norm_title"]
                m_src = item["source_url"] or ""

                if redis_sync.exists(f"riva:movie_dead_cooldown:{m_id}") and lane_tag != "🎯 VIP":
                    if is_multi:
                        halted_franchise = True
                        break
                    continue

                raw_urls = [
                    r[0]
                    for r in session.execute(
                        text(f"""
                            SELECT DISTINCT coalesce(sl.direct_url, sl.embed_url)
                            FROM streaming_links sl
                            WHERE sl.movie_id = :mid
                              AND (sl.direct_url IS NOT NULL OR sl.embed_url IS NOT NULL)
                              AND coalesce(sl.status, 'active') = 'active'
                              {SQL_UNSUPPORTED_FILTER};
                        """),
                        {"mid": m_id},
                    ).fetchall()
                ]

                clean_candidates = filter_streamable_urls(raw_urls)
                live_candidates, _ = preflight_verify_stream_candidates(clean_candidates) if clean_candidates else ([], [])

                if not live_candidates and (lane_tag == "🎯 VIP" or akwam_rescues_used < max_akwam_rescues):
                    akwam_rescues_used += 1
                    refreshed = _try_refresh_akwam_links(
                        session,
                        m_src,
                        movie_id=m_id,
                        movie_title=m_title,
                        movie_orig_title=item.get("original_title"),
                        movie_year=item.get("year"),
                    )
                    if refreshed:
                        live_candidates, _ = preflight_verify_stream_candidates(refreshed)

                if not live_candidates:
                    cooldown_sec = 900 if lane_tag == "🎯 VIP" else 21600
                    redis_sync.set(f"riva:movie_dead_cooldown:{m_id}", "1", ex=cooldown_sec)
                    if is_multi:
                        if chain_tasks:
                            break
                        logger.warning(
                            f"  ⛔ Franchise '{f_key}' halted at '{m_title}' (ID {m_id}): 0 playable streams. Will NOT upload later sequels out of order!"
                        )
                        halted_franchise = True
                        break
                    else:
                        logger.info(f"  ⏭️ {lane_tag} Movie {m_id} [{m_title[:28]}] currently has 0 live streams. Cooldown set.")
                        continue

                rotated_urls = rotate_candidates_by_provider(live_candidates)
                redis_sync.set(f"riva:dispatched:movie:{m_id}", "1", ex=2400)
                if norm_title:
                    redis_sync.set(f"riva:dispatched:movie_title:{norm_title}", "1", ex=2400)

                chain_tasks.append((m_id, m_title, rotated_urls, item.get("vote_average", 0.0), item.get("popularity", 0.0)))
                # For multi-part franchises, dispatch 1 unarchived part per cycle so a failure on Part K
                # never strands Part K+1..N in Redis; the next 25s cycle dispatches Part K+1 in strict order!
                if is_multi:
                    break

            if chain_tasks:
                mid, m_title, urls, m_rate, m_pop = chain_tasks[0]
                if is_multi:
                    redis_sync.set(f"riva:lock:franchise:{f_key}", "1", ex=900)
                task = process_video_transit_pipeline.delay(mid, urls, quality="1080p")
                logger.info(
                    f"  🚀 [{lane_tag}] Dispatched Movie {mid} [{m_title[:32]}] "
                    f"(⭐{m_rate:.1f} | 🔥{m_pop:.0f} | ✅ {len(urls)} mirrors) -> Task {task.id}"
                )

                total_dispatched += len(chain_tasks)
            elif halted_franchise:
                redis_sync.set(f"riva:franchise_blocked:{f_key}", "dead_links", ex=1800)

        return total_dispatched


def print_stats():
    """Prints current database and vault statistics."""
    with SyncSession() as session:
        total_series = session.execute(text("SELECT count(*) FROM series;")).scalar()
        total_episodes = session.execute(text("SELECT count(*) FROM episodes;")).scalar()
        total_movies = session.execute(text("SELECT count(*) FROM movies;")).scalar()
        total_streams = session.execute(text("SELECT count(*) FROM streaming_links WHERE status = 'active';")).scalar()
        total_tg = session.execute(text("SELECT count(*) FROM telegram_archives WHERE file_size_bytes > 15000000;")).scalar()

        print("\n" + "=" * 50)
        print("  RIVA AUTOMATION ENGINE - LIVE STATS")
        print("=" * 50)
        print(f"  • Total Series in DB:         {total_series}")
        print(f"  • Total Episodes Scraped:     {total_episodes}")
        print(f"  • Total Movies Scraped:       {total_movies}")
        print(f"  • Active Streaming Links:     {total_streams}")
        print(f"  • Uploaded to Telegram Vault: {total_tg}")
        print("=" * 50 + "\n")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "dispatch"

    if action == "stats":
        print_stats()
    elif action == "dispatch":
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        ep_d = dispatch_pending_episodes(limit=lim)
        mv_d = dispatch_pending_movies(limit=6)
        print(f"Dispatched {ep_d} episodes and {mv_d} movies.")
        print_stats()
