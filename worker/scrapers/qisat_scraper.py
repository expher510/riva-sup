"""
Qisat Eshq (v5.qisat.sbs / qisat.tv) Turkish Series Scraper & Episode Sync Helper.
Discovers full episode lists (Episode 1 -> Latest) for Turkish series (e.g. 'نصف الأم' / 'Anne Yarısı'),
extracts AlbaPlayer multi-server HD HLS/MP4 embeds (CDNPlus 720p60, Vidoba, MP4Plus, AnaFast, VidSpeed),
enriches TMDB poster/backdrop/Arabic story, and syncs cleanly into PostgreSQL in strict ascending order.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

try:
    from curl_cffi import requests as crequests
    HAS_CFFI = True
except ImportError:
    import httpx
    HAS_CFFI = False

from sqlalchemy import create_engine, or_
from sqlalchemy.orm import sessionmaker
import redis

from app.config import settings
from app.models import Episode, Season, Series, StreamingLink
from worker.metadata_enricher import search_tmdb_metadata

logger = logging.getLogger("qisat_scraper")

QISAT_MIRRORS = [
    "https://v5.qisat.sbs",
    "https://www.qisat.tv",
]

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)


def _http_get(url: str, referer: Optional[str] = None, timeout: int = 15) -> Tuple[int, str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    if HAS_CFFI:
        r = crequests.get(url, headers=headers, impersonate="chrome120", timeout=timeout, allow_redirects=True)
        return r.status_code, r.text, str(r.url)
    r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    return r.status_code, r.text, str(r.url)


def _classify_provider(embed_url: str) -> Optional[str]:
    u = embed_url.lower()
    if "cdnplus." in u:
        return "cdnplus"
    if "vidoba." in u:
        return "vidoba"
    if "mp4plus." in u:
        return "mp4plus"
    if "anafast." in u:
        return "anafast"
    if "vidspeed." in u:
        return "vidspeed"
    if "1vid." in u:
        return "1vid"
    if "morencius." in u:
        return "morencius"
    if any(k in u for k in ("vibuxer", "hgcloud", "hanerix", "streamwish", "vidhide", "byseraguci")):
        return "hgcloud"
    if any(k in u for k in ("dood", "playmogo", "dsvplay", "myvidplay", "d000d")):
        return "doodstream"
    if any(k in u for k in ("mixdrop", "mxdrop")):
        return "mixdrop"
    if "uqload." in u:
        return "uqload"
    if "streamtape" in u:
        return "streamtape"
    return None


def _parse_episode_number(ep_url: str, html_snippet: str = "") -> Optional[int]:
    dec = unquote(ep_url)
    for pat in (
        r"الحلقة[-\s_]+(\d{1,4})(?:\D|$)",
        r"حلقة[-\s_]+(\d{1,4})(?:\D|$)",
        r"s\d{1,2}e(\d{1,4})(?:\D|$)",
        r"-ep(?:isode)?-(\d{1,4})(?:\D|$)",
    ):
        m = re.search(pat, dec, re.I)
        if m:
            return int(m.group(1))
    if html_snippet:
        m2 = re.search(r"الحلقة\s+(\d{1,4})\b", html_snippet)
        if m2:
            return int(m2.group(1))
    return None


def resolve_qisat_episode_streams(ep_url: str) -> List[Dict[str, str]]:
    """
    Fetches a Qisat episode page, locates its AlbaPlayer multi-server wrapper,
    and extracts all supported HD streaming embeds (?serv=1..6).
    """
    streams: List[Dict[str, str]] = []
    seen_urls = set()

    try:
        st, ep_html, final_ep_url = _http_get(ep_url, timeout=15)
        if st != 200 or not ep_html:
            return []

        ep_origin = f"https://{urlparse(final_ep_url or ep_url).netloc}/"

        # Locate AlbaPlayer wrapper URL (e.g. https://w.anaplayer.online/albaplayer/anne-yarisi-2026-s01e01)
        m_alba = re.search(r'(https?://[^"\'\s<>]+/albaplayer/[a-zA-Z0-9\-_]+)', ep_html, re.I)
        if m_alba:
            alba_base = m_alba.group(1).rstrip("/")

            def _fetch_serv(s_idx: int) -> List[str]:
                try:
                    s_url = f"{alba_base}/?serv={s_idx}"
                    sc, s_html, _ = _http_get(s_url, referer=ep_origin, timeout=12)
                    if sc != 200 or not s_html:
                        return []
                    return re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', s_html, re.I)
                except Exception:
                    return []

            with ThreadPoolExecutor(max_workers=6) as pool:
                for ifr_list in pool.map(_fetch_serv, range(1, 7)):
                    for raw_u in ifr_list:
                        u = raw_u.strip()
                        if u.startswith("//"):
                            u = "https:" + u
                        if not u.startswith("http") or "albaplayer" in u.lower():
                            continue
                        prov = _classify_provider(u)
                        if prov and u not in seen_urls:
                            seen_urls.add(u)
                            code_m = re.search(r"(?:embed-|/(?:e|d|f|v)/)([a-zA-Z0-9]{8,24})", u)
                            streams.append({
                                "provider": prov,
                                "embed_url": u,
                                "direct_url": None,
                                "quality": "1080p",
                                "file_code": code_m.group(1) if code_m else None,
                            })

        # Also inspect direct embeds on episode page if any
        for raw_u in re.findall(r'(?:src|data-src|data-embed)=["\'](https?://[^"\']+)["\']', ep_html, re.I):
            u = raw_u.strip()
            prov = _classify_provider(u)
            if prov and u not in seen_urls:
                seen_urls.add(u)
                code_m = re.search(r"(?:embed-|/(?:e|d|f|v)/)([a-zA-Z0-9]{8,24})", u)
                streams.append({
                    "provider": prov,
                    "embed_url": u,
                    "direct_url": None,
                    "quality": "1080p",
                    "file_code": code_m.group(1) if code_m else None,
                })
    except Exception as e:
        logger.warning(f"Failed resolving Qisat episode {ep_url}: {e}")

    return streams


def sync_qisat_series_to_db(
    slug_or_url: str = "https://v5.qisat.sbs/series/anne-yarisi/",
    arabic_title: str = "نصف الأم",
    original_title: str = "Anne Yarısı",
    category: str = "turkish",
    year: int = 2026,
    mark_vip: bool = True,
) -> Dict[str, Any]:
    """
    Discovers all available episodes of a Turkish series on Qisat Eshq, enriches TMDB metadata
    (poster, backdrop, Arabic overview), inserts Episodes 1..N + active stream mirrors into PostgreSQL,
    and optionally registers the series in `riva:vip_series_ids` for #1 upload priority.
    """
    if slug_or_url.startswith("http"):
        parsed = urlparse(slug_or_url)
        path_part = parsed.path.strip("/")
        candidate_series_urls = [slug_or_url]
        for mir in QISAT_MIRRORS:
            alt = f"{mir}/{path_part}/"
            if alt not in candidate_series_urls:
                candidate_series_urls.append(alt)
    else:
        slug_clean = slug_or_url.strip("/")
        if not slug_clean.startswith("series/"):
            slug_clean = f"series/{slug_clean}"
        candidate_series_urls = [f"{mir}/{slug_clean}/" for mir in QISAT_MIRRORS]

    series_html = ""
    active_series_url = candidate_series_urls[0]
    active_base = QISAT_MIRRORS[0]
    for s_url in candidate_series_urls:
        try:
            st, body, fin_u = _http_get(s_url, timeout=15)
            if st == 200 and body and "/episode/" in body:
                series_html = body
                active_series_url = fin_u or s_url
                active_base = f"https://{urlparse(active_series_url).netloc}"
                break
        except Exception:
            continue

    if not series_html:
        logger.warning(f"Could not fetch Qisat series page for {arabic_title} ({slug_or_url})")
        return {"series_id": None, "episodes_synced": 0}

    # Discover all episode links on the series page
    raw_ep_hrefs = set(re.findall(r'href=["\'](https?://[^"\']+/episode/[^"\']+)["\']', series_html, re.I))
    ep_map: Dict[int, str] = {}
    for href in raw_ep_hrefs:
        ep_num = _parse_episode_number(href)
        if not ep_num or ep_num <= 0:
            continue
        # Rewrite domain to active_base so mirror blocks never affect episode fetches
        p_h = urlparse(href)
        norm_ep_url = f"{active_base}{p_h.path}"
        if ep_num not in ep_map:
            ep_map[ep_num] = norm_ep_url

    ordered_ep_nums = sorted(ep_map.keys())
    if not ordered_ep_nums:
        logger.warning(f"No episodes discovered on Qisat page {active_series_url}")
        return {"series_id": None, "episodes_synced": 0}

    # Fetch TMDB metadata for official HD Poster, Backdrop & Arabic Overview
    tmdb_meta = search_tmdb_metadata(original_title or arabic_title, media_type="tv", year=year)
    if not tmdb_meta and original_title:
        tmdb_meta = search_tmdb_metadata(arabic_title, media_type="tv", year=year)

    slug_token = urlparse(active_series_url).path.strip("/").split("/")[-1] or "anne-yarisi"

    with SyncSessionLocal() as session:
        series = (
            session.query(Series)
            .filter(
                or_(
                    Series.title == arabic_title,
                    Series.slug == slug_token,
                    Series.source_url == active_series_url,
                    (Series.tmdb_id == tmdb_meta["tmdb_id"]) if (tmdb_meta and tmdb_meta.get("tmdb_id")) else False,
                )
            )
            .first()
        )

        if not series:
            series = Series(
                tmdb_id=tmdb_meta.get("tmdb_id") if tmdb_meta else None,
                source_url=active_series_url,
                slug=slug_token,
                category=category,
                title=arabic_title,
                original_title=original_title or (tmdb_meta.get("original_title") if tmdb_meta else None),
                overview=(tmdb_meta.get("overview") if tmdb_meta else None)
                or f"مسلسل تركي درامي مشوق ({arabic_title} - {original_title}) مترجم بجودة عالية.",
                year=year or (tmdb_meta.get("year") if tmdb_meta else 2026),
                poster_path=tmdb_meta.get("poster_path") if tmdb_meta else None,
                backdrop_path=tmdb_meta.get("backdrop_path") if tmdb_meta else None,
                vote_average=float(tmdb_meta.get("vote_average") or 7.5) if tmdb_meta else 7.5,
                vote_count=int(tmdb_meta.get("vote_count") or 10) if tmdb_meta else 10,
                popularity=max(float(tmdb_meta.get("popularity") or 0.0) if tmdb_meta else 0.0, 150.0),
                genres=(tmdb_meta.get("genres") if tmdb_meta and tmdb_meta.get("genres") else ["Drama"]),
                status="ongoing",
            )
            session.add(series)
            session.commit()
            session.refresh(series)
        else:
            if tmdb_meta:
                if not series.poster_path and tmdb_meta.get("poster_path"):
                    series.poster_path = tmdb_meta["poster_path"]
                if not series.backdrop_path and tmdb_meta.get("backdrop_path"):
                    series.backdrop_path = tmdb_meta["backdrop_path"]
                if not series.overview and tmdb_meta.get("overview"):
                    series.overview = tmdb_meta["overview"]
                if not series.tmdb_id and tmdb_meta.get("tmdb_id"):
                    series.tmdb_id = tmdb_meta["tmdb_id"]
            if (series.popularity or 0.0) < 150.0:
                series.popularity = 150.0
            session.commit()

        season = (
            session.query(Season)
            .filter_by(series_id=series.id, season_number=1)
            .first()
        )
        if not season:
            season = Season(
                series_id=series.id,
                season_number=1,
                title="الموسم 1",
                source_url=active_series_url,
            )
            session.add(season)
            session.commit()
            session.refresh(season)

        # Scrape stream mirrors in parallel for episodes needing active links
        eps_to_scrape: List[Tuple[int, str]] = []
        for ep_num in ordered_ep_nums:
            ep_url = ep_map[ep_num]
            existing_ep = (
                session.query(Episode)
                .filter_by(season_id=season.id, episode_number=ep_num)
                .first()
            )
            if existing_ep:
                active_cnt = (
                    session.query(StreamingLink)
                    .filter(
                        StreamingLink.episode_id == existing_ep.id,
                        StreamingLink.provider_name != "riva-server",
                        StreamingLink.status == "active",
                    )
                    .count()
                )
                if active_cnt >= 2:
                    continue
            eps_to_scrape.append((ep_num, ep_url))

        scraped_streams_by_ep: Dict[int, List[Dict[str, str]]] = {}
        if eps_to_scrape:
            with ThreadPoolExecutor(max_workers=min(6, len(eps_to_scrape))) as pool:
                futs = {ep_n: pool.submit(resolve_qisat_episode_streams, ep_u) for ep_n, ep_u in eps_to_scrape}
                for ep_n, fut in futs.items():
                    scraped_streams_by_ep[ep_n] = fut.result() or []

        synced_count = 0
        total_links_added = 0
        for ep_num in ordered_ep_nums:
            ep_url = ep_map[ep_num]
            ep = (
                session.query(Episode)
                .filter_by(season_id=season.id, episode_number=ep_num)
                .first()
            )
            if not ep:
                # Ensure source_url uniqueness
                url_collision = session.query(Episode).filter_by(source_url=ep_url).first()
                safe_src = ep_url if not url_collision else f"{ep_url}#s{series.id}e{ep_num}"
                ep = Episode(
                    season_id=season.id,
                    episode_number=ep_num,
                    title=f"الحلقة {ep_num}",
                    source_url=safe_src,
                    status="active",
                )
                session.add(ep)
                session.commit()
                session.refresh(ep)
            synced_count += 1

            for st_item in scraped_streams_by_ep.get(ep_num, []):
                exists_ln = (
                    session.query(StreamingLink)
                    .filter_by(episode_id=ep.id, embed_url=st_item["embed_url"])
                    .first()
                )
                if exists_ln:
                    exists_ln.status = "active"
                else:
                    session.add(
                        StreamingLink(
                            episode_id=ep.id,
                            provider_name=st_item["provider"],
                            embed_url=st_item["embed_url"],
                            direct_url=st_item.get("direct_url"),
                            quality=st_item.get("quality", "1080p"),
                            file_code=st_item.get("file_code"),
                            status="active",
                        )
                    )
                    total_links_added += 1
            session.commit()

        series_id = series.id

    if mark_vip and series_id:
        try:
            r_cli = redis.from_url(settings.REDIS_URL, decode_responses=True)
            r_cli.sadd("riva:vip_series_ids", str(series_id))
        except Exception as e:
            logger.debug(f"Could not mark VIP series in Redis: {e}")

    logger.info(
        f"✅ Synced Qisat Series '{arabic_title}' (ID={series_id}): {synced_count} episodes ({ordered_ep_nums}), +{total_links_added} stream mirrors."
    )
    return {
        "series_id": series_id,
        "title": arabic_title,
        "episodes_synced": synced_count,
        "episode_numbers": ordered_ep_nums,
        "links_added": total_links_added,
    }

