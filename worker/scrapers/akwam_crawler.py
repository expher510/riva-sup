"""
Akwam (akwams.org) Production Scraper.
Extracts Movies, Series, Seasons, Episodes, and Streaming/Download Links (DoodStream, Mixdrop, etc.)
and dispatches them into the Riva transit pipeline (Telegram Vault + Cyberlockers).
"""
import re
import datetime
import logging
import asyncio
from typing import Optional, Dict, Any, List
from urllib.parse import unquote

from curl_cffi import requests as crequests
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models import (
    Series, Season, Episode, Movie, StreamingLink, ScraperCheckpoint
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("akwam_crawler")

BASE_URL = "https://akwams.org"

CATEGORIES = {
    "foreign-series": {
        "url": "https://akwams.org/category/%d9%85%d8%b3%d9%84%d8%b3%d9%84%d8%a7%d8%aa-%d8%a7%d8%ac%d9%86%d8%a8%d9%8a/",
        "name": "مسلسلات أجنبي",
        "type": "series",
        "cat_code": "western"
    },
    "arabic-series": {
        "url": "https://akwams.org/category/%d9%85%d8%b3%d9%84%d8%b3%d9%84%d8%a7%d8%aa-%d8%b9%d8%b1%d8%a8%d9%8a/",
        "name": "مسلسلات عربي",
        "type": "series",
        "cat_code": "arabic"
    },
    "turkish-series": {
        "url": "https://akwams.org/category/%d9%85%d8%b3%d9%84%d8%b3%d9%84%d8%a7%d8%aa-%d8%aa%d8%b1%d9%83%d9%8a%d8%a9/",
        "name": "مسلسلات تركية",
        "type": "series",
        "cat_code": "turkish"
    },
    "asian-series": {
        "url": "https://akwams.org/category/%d9%85%d8%b3%d9%84%d8%b3%d9%84%d8%a7%d8%aa-%d8%a7%d8%b3%d9%8a%d9%88%d9%8a%d8%a9/",
        "name": "مسلسلات آسيوية",
        "type": "series",
        "cat_code": "kdrama"
    },
    "anime-series": {
        "url": "https://akwams.org/category/%d9%85%d8%b3%d9%84%d8%b3%d9%84%d8%a7%d8%aa-%d8%a7%d9%86%d9%85%d9%8a/",
        "name": "مسلسلات أنمي",
        "type": "series",
        "cat_code": "anime"
    },
    "foreign-movies": {
        "url": "https://akwams.org/category/movies/%d8%a7%d9%81%d9%84%d8%a7%d9%85-%d8%a7%d8%ac%d9%86%d8%a8%d9%8a/",
        "name": "أفلام أجنبي",
        "type": "movie",
        "cat_code": "western"
    },
    "arabic-movies": {
        "url": "https://akwams.org/category/%d8%a7%d9%81%d9%84%d8%a7%d9%85-%d8%b9%d8%b1%d8%a8%d9%8a/",
        "name": "أفلام عربي",
        "type": "movie",
        "cat_code": "arabic"
    },
    "turkish-movies": {
        "url": "https://akwams.org/category/%d8%a7%d9%81%d9%84%d8%a7%d9%85-%d8%aa%d8%b1%d9%83%d9%8a%d8%a9/",
        "name": "أفلام تركية",
        "type": "movie",
        "cat_code": "turkish"
    },
    "asian-movies": {
        "url": "https://akwams.org/category/%d8%a7%d9%81%d9%84%d8%a7%d9%85-%d8%a7%d8%b3%d9%8a%d9%88%d9%8a%d8%a9/",
        "name": "أفلام آسيوية",
        "type": "movie",
        "cat_code": "asian"
    },
    "anime-movies": {
        "url": "https://akwams.org/category/%d8%a7%d9%81%d9%84%d8%a7%d9%85-%d8%a7%d9%86%d9%85%d9%8a/",
        "name": "أفلام أنمي",
        "type": "movie",
        "cat_code": "anime"
    }
}

ARABIC_ORDINALS = {
    "الاول": 1, "الأول": 1, "1": 1,
    "الثاني": 2, "2": 2,
    "الثالث": 3, "3": 3,
    "الرابع": 4, "4": 4,
    "الخامس": 5, "5": 5,
    "السادس": 6, "6": 6,
    "السابع": 7, "7": 7,
    "الثامن": 8, "8": 8,
    "التاسع": 9, "9": 9,
    "العاشر": 10, "10": 10,
    "الحادي عشر": 11, "11": 11,
    "الثاني عشر": 12, "12": 12
}


def parse_series_title(raw_text: str):
    text = unquote(raw_text).replace('-', ' ')
    season_num = 1
    s_match = re.search(r'الموسم\s+([^\s]+)', text)
    if s_match:
        val = s_match.group(1).strip()
        season_num = ARABIC_ORDINALS.get(val, int(val) if val.isdigit() else 1)

    ep_num = 1
    e_match = re.search(r'الحلقة\s+(\d+)', text)
    if e_match:
        ep_num = int(e_match.group(1))

    year = None
    y_match = re.search(r'\b(19\d{2}|20\d{2})\b', text)
    if y_match:
        year = int(y_match.group(1))

    clean = re.sub(r'^(?:مشاهدة\s+)?مسلسل\s+', '', text, flags=re.I)
    clean = re.sub(r'الموسم\s+[^\s]+', '', clean)
    clean = re.sub(r'الحلقة\s+\d+', '', clean)
    clean = re.sub(r'\b(19\d{2}|20\d{2})\b', '', clean)
    clean = re.sub(r'\b(?:مترجم|مترجمة|مدبلج|مدبلجة|اون\s+لاين|HD|كاملة?|مت|متر|والاخيرة|الاخيرة)\b', '', clean, flags=re.I)
    clean = re.sub(r'\s+', ' ', clean).strip(' -:|/')
    return clean or "مسلسل", season_num, ep_num, year


def parse_movie_title(raw_text: str):
    text = unquote(raw_text).replace('-', ' ')
    year = None
    y_match = re.search(r'\b(19\d{2}|20\d{2})\b', text)
    if y_match:
        year = int(y_match.group(1))

    clean = re.sub(r'^(?:مشاهدة\s+)?فيلم\s+', '', text, flags=re.I)
    clean = re.sub(r'\b(19\d{2}|20\d{2})\b', '', clean)
    clean = re.sub(r'\b(?:مترجم|مترجمة|مدبلج|مدبلجة|اون\s+لاين|HD|كامل|تحميل)\b', '', clean, flags=re.I)
    clean = re.sub(r'\s+', ' ', clean).strip(' -:|/')
    return clean or "فيلم", year


def resolve_akwam_streams(post_url: str) -> List[Dict[str, Any]]:
    """
    Fetches the /download and /watch pages of an Akwam post and extracts streaming links.
    """
    streams = []
    base_clean = post_url.rstrip('/')
    
    # 1. Fetch /watch page for iframe players
    try:
        r_watch = crequests.get(f"{base_clean}/watch", impersonate="chrome120", timeout=12)
        if r_watch.status_code == 200:
            iframes = re.findall(r'<iframe\s+[^>]*src=["\']([^"\']+)["\']', r_watch.text)
            for ifr in iframes:
                if "mixdrop" in ifr:
                    code = ifr.split("/")[-1]
                    streams.append({
                        "provider_name": "mixdrop",
                        "embed_url": ifr,
                        "direct_url": ifr.replace('/e/', '/f/'),
                        "quality": "1080p"
                    })
    except Exception as e:
        logger.warning(f"Error fetching watch page {base_clean}/watch: {e}")

    # 2. Fetch /download page for cyberlocker links
    try:
        r_dl = crequests.get(f"{base_clean}/download", impersonate="chrome120", timeout=12)
        if r_dl.status_code == 200:
            html_dl = r_dl.text
            # DoodStream
            dood_codes = re.findall(r'doodstream\.com/d/([a-zA-Z0-9]+)', html_dl)
            for d in dood_codes:
                streams.append({
                    "provider_name": "doodstream",
                    "embed_url": f"https://doodstream.com/e/{d}",
                    "direct_url": f"https://doodstream.com/d/{d}",
                    "quality": "1080p"
                })

            # Mixdrop
            mix_codes = re.findall(r'mixdrop\.[a-z]+/f/([a-zA-Z0-9]+)', html_dl)
            for m in mix_codes:
                streams.append({
                    "provider_name": "mixdrop",
                    "embed_url": f"https://mixdrop.ps/e/{m}",
                    "direct_url": f"https://mixdrop.ps/f/{m}",
                    "quality": "1080p"
                })

            # Vidhide
            vidhide_codes = re.findall(r'vidhide[^/]+/d/([a-zA-Z0-9]+)', html_dl)
            for v in vidhide_codes:
                streams.append({
                    "provider_name": "vidhide",
                    "embed_url": f"https://vidhidevip.com/e/{v}",
                    "direct_url": f"https://vidhidevip.com/d/{v}",
                    "quality": "1080p"
                })

    except Exception as e:
        logger.warning(f"Error fetching download page {base_clean}/download: {e}")

    # Deduplicate by embed_url
    seen = set()
    deduped = []
    for s in streams:
        if s["embed_url"] not in seen:
            seen.add(s["embed_url"])
            deduped.append(s)
    return deduped


async def crawl_akwam_category(cat_key: str, max_pages: Optional[int] = 5):
    """
    Crawls a specific category on Akwam up to max_pages, saves records,
    and dispatches transit tasks.
    """
    if cat_key not in CATEGORIES:
        logger.error(f"Unknown category: {cat_key}")
        return

    cat_info = CATEGORIES[cat_key]
    logger.info(f"Starting Akwam crawl for: {cat_info['name']} ({cat_key})")

    checkpoint_slug = f"akwam_{cat_key}"
    start_page = 1

    async with AsyncSessionLocal() as session:
        stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == checkpoint_slug)
        cp = (await session.execute(stmt)).scalars().first()
        if cp:
            start_page = cp.current_page
            logger.info(f"Resuming Akwam {cat_key} from checkpoint page: {start_page}")
        else:
            cp = ScraperCheckpoint(
                category_slug=checkpoint_slug,
                current_page=1,
                total_pages=50,
                status="running"
            )
            session.add(cp)
            await session.commit()

    page = start_page
    pages_crawled = 0

    while True:
        if max_pages and pages_crawled >= max_pages:
            logger.info(f"Reached max pages limit ({max_pages}) for Akwam {cat_key}. Pausing.")
            break

        cat_url = cat_info["url"]
        page_url = f"{cat_url}page/{page}/" if page > 1 else cat_url
        logger.info(f"Fetching Akwam page {page}: {page_url}")

        try:
            resp = crequests.get(page_url, impersonate="chrome120", timeout=15)
            if resp.status_code == 404:
                logger.info(f"Akwam category {cat_key} finished! Page {page} returned 404.")
                async with AsyncSessionLocal() as db:
                    stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == checkpoint_slug)
                    cp_end = (await db.execute(stmt)).scalars().first()
                    if cp_end:
                        cp_end.status = "completed"
                        await db.commit()
                break

            if resp.status_code != 200:
                logger.warning(f"Akwam page {page} returned {resp.status_code}. Skipping.")
                break

            html = resp.text

            # Detect total pages
            total_pages_match = re.findall(r'/page/(\d+)/?', html)
            if total_pages_match:
                detected_max = max(int(p) for p in total_pages_match)
                async with AsyncSessionLocal() as db:
                    stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == checkpoint_slug)
                    cp_up = (await db.execute(stmt)).scalars().first()
                    if cp_up and detected_max > cp_up.total_pages:
                        cp_up.total_pages = detected_max
                        await db.commit()

            # Extract item links
            raw_links = re.findall(r'<a\s+[^>]*href=["\'](https?://akwams\.org/[^"\'?#]+/)["\'][^>]*>(.*?)</a>', html, re.DOTALL)
            unique_items = {}
            for h, inner in raw_links:
                if not any(k in h for k in ['/category/', '/page/', '/tag/', '/login', '/logout', '/profile', '/favorite', '/dmca', '/ad-policy']):
                    clean_text = re.sub(r'<[^>]+>', ' ', inner).strip()
                    clean_text = re.sub(r'\s+', ' ', clean_text)
                    if len(clean_text) > 3 and h not in unique_items:
                        unique_items[h] = clean_text

            logger.info(f"Akwam Page {page} discovered {len(unique_items)} unique items.")

            items_added_on_page = 0
            for item_url, item_title in unique_items.items():
                async with AsyncSessionLocal() as db:
                    if cat_info["type"] == "series":
                        stmt_chk = select(Episode.id).where(Episode.source_url == item_url)
                        if (await db.execute(stmt_chk)).scalar():
                            continue

                        series_title, season_num, episode_num, s_year = parse_series_title(item_title)

                        # Find or create Series
                        stmt_s = select(Series).where(Series.title == series_title)
                        series = (await db.execute(stmt_s)).scalars().first()
                        if not series:
                            series = Series(
                                title=series_title,
                                category=cat_info["cat_code"],
                                year=s_year,
                                status="ongoing"
                            )
                            db.add(series)
                            await db.commit()
                            await db.refresh(series)

                        # Find or create Season
                        stmt_season = select(Season).where(
                            Season.series_id == series.id,
                            Season.season_number == season_num
                        )
                        season = (await db.execute(stmt_season)).scalars().first()
                        if not season:
                            season = Season(
                                series_id=series.id,
                                season_number=season_num,
                                title=f"الموسم {season_num}"
                            )
                            db.add(season)
                            await db.commit()
                            await db.refresh(season)

                        # Create Episode
                        episode = Episode(
                            season_id=season.id,
                            episode_number=episode_num,
                            title=item_title,
                            source_url=item_url
                        )
                        db.add(episode)
                        await db.commit()
                        await db.refresh(episode)

                        # Resolve streaming links from Akwam download/watch pages
                        streams = resolve_akwam_streams(item_url)
                        for s in streams:
                            s_link = StreamingLink(
                                episode_id=episode.id,
                                provider_name=s["provider_name"],
                                embed_url=s["embed_url"],
                                direct_url=s.get("direct_url"),
                                quality=s.get("quality", "1080p"),
                                status="active"
                            )
                            db.add(s_link)
                        await db.commit()

                        logger.info(f"  + Added [{cat_info['cat_code']}] {series_title} S{season_num}E{episode_num} ({len(streams)} streams)")
                        items_added_on_page += 1

                        # Dispatch transit pipeline to download & upload to Telegram + cyberlockers
                        direct_stream = next((s["direct_url"] for s in streams if s.get("direct_url")), None)
                        if direct_stream:
                            try:
                                from worker.pipeline import process_episode_transit_pipeline
                                process_episode_transit_pipeline.delay(episode.id, direct_stream, quality="1080p")
                                logger.info(f"  🚀 Dispatched transit pipeline for Episode {episode.id}")
                            except Exception as pipe_err:
                                logger.warning(f"Could not dispatch transit pipeline for episode: {pipe_err}")

                    else:
                        # Movies
                        stmt_m = select(Movie.id).where(Movie.source_url == item_url)
                        if (await db.execute(stmt_m)).scalar():
                            continue

                        movie_title, m_year = parse_movie_title(item_title)
                        movie = Movie(
                            title=movie_title,
                            source_url=item_url,
                            category=cat_info["cat_code"],
                            year=m_year
                        )
                        db.add(movie)
                        await db.commit()
                        await db.refresh(movie)

                        streams = resolve_akwam_streams(item_url)
                        for s in streams:
                            s_link = StreamingLink(
                                movie_id=movie.id,
                                provider_name=s["provider_name"],
                                embed_url=s["embed_url"],
                                direct_url=s.get("direct_url"),
                                quality=s.get("quality", "1080p"),
                                status="active"
                            )
                            db.add(s_link)
                        await db.commit()

                        logger.info(f"  + Added [{cat_info['cat_code']} Movie] {movie_title} ({len(streams)} streams)")
                        items_added_on_page += 1

                        direct_stream = next((s["direct_url"] for s in streams if s.get("direct_url")), None)
                        if direct_stream:
                            try:
                                from worker.pipeline import process_video_transit_pipeline
                                process_video_transit_pipeline.delay(movie.id, direct_stream, quality="1080p")
                                logger.info(f"  🚀 Dispatched transit pipeline for Movie {movie.id}")
                            except Exception as pipe_err:
                                logger.warning(f"Could not dispatch transit pipeline for movie: {pipe_err}")

            # Update Checkpoint after completing page
            async with AsyncSessionLocal() as db:
                stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == checkpoint_slug)
                cp_done = (await db.execute(stmt)).scalars().first()
                if cp_done:
                    cp_done.current_page = page + 1
                    cp_done.items_count += items_added_on_page
                    cp_done.status = "running"
                    cp_done.updated_at = datetime.datetime.utcnow()
                    await db.commit()

            page += 1
            pages_crawled += 1
            await asyncio.sleep(1.5)

        except Exception as e:
            logger.error(f"Error crawling Akwam page {page} for {cat_key}: {e}")
            break


async def run_all_akwam_categories(max_pages: int = 5):
    """Iterates through all Akwam categories in sequence."""
    for cat_key in CATEGORIES.keys():
        try:
            await crawl_akwam_category(cat_key, max_pages=max_pages)
        except Exception as e:
            logger.error(f"Failed Akwam category {cat_key}: {e}")
