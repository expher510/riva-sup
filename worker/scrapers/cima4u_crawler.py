"""
Cima4u Comprehensive Resumable Scraper Engine.
Scrapes all movies, anime, and series (Korean, Asian, Turkish, Western, Arabic).
Features checkpoint state tracking for 100% resumable operations.
"""
import os
import re
import json
import logging
import asyncio
import datetime
from typing import Dict, Any, List, Optional
import httpx
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal, engine, Base
from app.models import (
    Series, Season, Episode, Movie, StreamingLink, ScraperCheckpoint
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cima4u_crawler")

BASE_URL = "https://cima4u.mobile"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://cima4u.mobile/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8"
}

CATEGORIES = {
    "asian-series": {
        "slug": "%D9%85%D8%B3%D9%84%D8%B3%D9%84%D8%A7%D8%AA-%D8%A7%D8%B3%D9%8A%D9%88%D9%8A%D8%A9",
        "name": "مسلسلات كورية وآسيوية",
        "type": "series",
        "cat_code": "kdrama"
    },
    "anime-series": {
        "slug": "%D9%85%D8%B3%D9%84%D8%B3%D9%84%D8%A7%D8%AA-%D8%A7%D9%86%D9%85%D9%8A",
        "name": "مسلسلات أنمي",
        "type": "series",
        "cat_code": "anime"
    },
    "anime-movies": {
        "slug": "%D8%A7%D9%81%D9%84%D8%A7%D9%85-%D8%A7%D9%86%D9%85%D9%8A",
        "name": "أفلام أنمي",
        "type": "movie",
        "cat_code": "anime"
    },
    "asian-movies": {
        "slug": "%D8%A7%D9%81%D9%84%D8%A7%D9%85-%D8%A7%D8%B3%D9%8A%D9%88%D9%8A%D8%A9",
        "name": "أفلام كورية وآسيوية",
        "type": "movie",
        "cat_code": "asian"
    },
    "turkish-series": {
        "slug": "%D9%85%D8%B3%D9%84%D8%B3%D9%84%D8%A7%D8%AA-%D8%AA%D8%B1%D9%83%D9%8A%D8%A9",
        "name": "مسلسلات تركية",
        "type": "series",
        "cat_code": "turkish"
    },
    "western-series": {
        "slug": "%D9%85%D8%B3%D9%84%D8%B3%D9%84%D8%A7%D8%AA-%D8%A7%D8%AC%D9%86%D8%A8%D9%8A",
        "name": "مسلسلات أجنبي",
        "type": "series",
        "cat_code": "western"
    },
    "western-movies": {
        "slug": "%D8%A7%D9%81%D9%84%D8%A7%D9%85-%D8%A7%D8%AC%D9%86%D8%A8%D9%8A",
        "name": "أفلام أجنبي",
        "type": "movie",
        "cat_code": "western"
    },
    "arabic-series": {
        "slug": "%D9%85%D8%B3%D9%84%D8%B3%D9%84%D8%A7%D8%AA-%D8%B9%D8%B1%D8%A8%D9%8A",
        "name": "مسلسلات عربي",
        "type": "series",
        "cat_code": "arabic"
    },
    "arabic-movies": {
        "slug": "%D8%A7%D9%81%D9%84%D8%A7%D9%85-%D8%B9%D8%B1%D8%A8%D9%8A",
        "name": "أفلام عربي",
        "type": "movie",
        "cat_code": "arabic"
    }
}


def unpack_dean_edwards_packer(p: str, a: int, c: int, k: list) -> str:
    """Unpacks Dean Edwards packed Javascript code."""
    def base_n(num, b):
        chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        res = ""
        while num > 0:
            res = chars[num % b] + res
            num //= b
        return res or "0"

    k_dict = {}
    for i in range(c):
        key = base_n(i, a)
        k_dict[key] = k[i] if i < len(k) and k[i] else key

    def replace_word(m):
        w = m.group(0)
        return k_dict.get(w, w)

    return re.sub(r'\b\w+\b', replace_word, p)


def parse_series_title_and_numbers(raw_title: str):
    """
    Parses full episode title like 'مسلسل اشعرني بالسعادة Make Me Tingle الحلقة 6 مترجمة'
    Returns: (series_title, season_num, episode_num)
    """
    s_match = re.search(r'الموسم\s+(\d+|[^\s]+)', raw_title)
    season_num = 1
    if s_match:
        val = s_match.group(1).strip()
        arabic_num_map = {"الاول": 1, "الثاني": 2, "الثالث": 3, "الرابع": 4, "الخامس": 5, "السادس": 6}
        season_num = int(val) if val.isdigit() else arabic_num_map.get(val, 1)

    ep_match = re.search(r'الحلقة\s+(\d+)', raw_title)
    episode_num = int(ep_match.group(1)) if ep_match else 1

    clean_title = raw_title
    for phrase in [
        r'الموسم\s+[^\s]+', r'الحلقة\s+\d+', r'مترجمة?', r'مدبلجة?',
        r'والاخيرة', r'اون لاين', r'كاملة?', r'HD', r'720p', r'1080p', r'WEB-DL'
    ]:
        clean_title = re.sub(phrase, '', clean_title, flags=re.IGNORECASE)

    clean_title = re.sub(r'\s+', ' ', clean_title).strip(' -_')
    return clean_title, season_num, episode_num


async def resolve_video_streams_for_url(client: httpx.AsyncClient, watch_url: str) -> List[Dict[str, str]]:
    """
    Resolves real video server embed and direct HLS links from Cima4u watch page.
    """
    try:
        resp = await client.get(watch_url, headers=HEADERS, timeout=20.0)
        if resp.status_code != 200:
            return []

        html = resp.text
        key_match = re.search(r'const watchResolveKey = ["\']([^"\']+)["\']', html)
        csrf_match = re.search(r'const csrfToken = ["\']([^"\']+)["\']', html)
        servers_match = re.search(r'const servers = (\[.*?\]);', html)

        if not (key_match and csrf_match and servers_match):
            return []

        key = key_match.group(1)
        csrf = csrf_match.group(1)
        servers = json.loads(servers_match.group(1))

        import urllib.parse
        ascii_referer = urllib.parse.quote(watch_url, safe=':/?#[]@!$&\'()*+,;=')

        resolve_headers = {
            **HEADERS,
            "Referer": ascii_referer,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CSRF-TOKEN": csrf,
            "X-Requested-With": "XMLHttpRequest"
        }

        stream_links = []
        for idx in range(min(4, len(servers))):
            s_name = servers[idx].get("name", f"server_{idx}")
            payload = {"key": key, "index": idx, "human_signal": None}
            res_post = await client.post("https://cima4u.mobile/watch/resolve", json=payload, headers=resolve_headers)
            
            if res_post.status_code == 200:
                data = res_post.json()
                if data.get("ok") and data.get("url"):
                    m_url = f"{BASE_URL}{data['url']}"
                    m_resp = await client.get(m_url, headers={"Referer": ascii_referer})
                    real_embed_url = str(m_resp.url)

                    
                    direct_hls_url = None
                    # If fastvid, unpack direct master.m3u8 stream!
                    if "fastvid" in real_embed_url:
                        pack_match = re.search(
                            r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)",
                            m_resp.text,
                            re.DOTALL
                        )
                        if pack_match:
                            try:
                                p, a, c, k = (
                                    pack_match.group(1),
                                    int(pack_match.group(2)),
                                    int(pack_match.group(3)),
                                    pack_match.group(4).split('|')
                                )
                                unpacked = unpack_dean_edwards_packer(p, a, c, k)
                                m3u8_found = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', unpacked)
                                if m3u8_found:
                                    direct_hls_url = m3u8_found[0]
                            except Exception:
                                pass

                    stream_links.append({
                        "provider_name": s_name,
                        "embed_url": real_embed_url,
                        "direct_url": direct_hls_url,
                        "quality": "1080p"
                    })

        return stream_links

    except Exception as exc:
        logger.warning(f"Failed to resolve streams for {watch_url}: {exc}")
        return []


async def crawl_category(
    cat_key: str,
    max_pages: Optional[int] = None,
    session_maker=AsyncSessionLocal
):
    """
    Crawls a given category with automatic checkpoint resumption.
    """
    cat_info = CATEGORIES.get(cat_key)
    if not cat_info:
        logger.error(f"Unknown category key: {cat_key}")
        return

    logger.info(f"=== Starting Crawl for Category: {cat_info['name']} ({cat_key}) ===")

    async with session_maker() as db:
        # Load or create checkpoint
        stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == cat_key)
        res = await db.execute(stmt)
        checkpoint = res.scalars().first()

        if not checkpoint:
            checkpoint = ScraperCheckpoint(
                category_slug=cat_key,
                category_name=cat_info["name"],
                current_page=1,
                total_pages=1,
                status="running"
            )
            db.add(checkpoint)
            await db.commit()
            await db.refresh(checkpoint)

        start_page = checkpoint.current_page
        logger.info(f"Resuming {cat_info['name']} from page {start_page} (Total items so far: {checkpoint.items_count})")

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25.0) as client:
        page = start_page
        pages_crawled = 0

        while True:
            if max_pages and pages_crawled >= max_pages:
                logger.info(f"Reached max pages limit ({max_pages}) for {cat_key}. Pausing.")
                break

            page_url = f"{BASE_URL}/category/{cat_info['slug']}/page/{page}" if page > 1 else f"{BASE_URL}/category/{cat_info['slug']}"
            logger.info(f"Fetching page {page}: {page_url}")

            try:
                resp = await client.get(page_url)
                if resp.status_code == 404:
                    logger.info(f"Category {cat_key} finished! Page {page} returned 404.")
                    async with session_maker() as db:
                        stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == cat_key)
                        cp = (await db.execute(stmt)).scalars().first()
                        if cp:
                            cp.status = "completed"
                            await db.commit()
                    break

                if resp.status_code == 403:
                    logger.error(f"Page {page} returned 403 Forbidden (Cloudflare challenge).")
                    raise RuntimeError(f"HTTP 403 Forbidden on {page_url} (Cloudflare challenge)")

                if resp.status_code != 200:
                    logger.warning(f"Page {page} returned {resp.status_code}. Retrying in 5s...")
                    await asyncio.sleep(5)
                    continue

                html = resp.text

                # Detect total pages from pagination bar if not yet set
                total_pages_match = re.findall(r'/page/(\d+)', html)
                if total_pages_match:
                    detected_max = max(int(p) for p in total_pages_match)
                    async with session_maker() as db:
                        stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == cat_key)
                        cp = (await db.execute(stmt)).scalars().first()
                        if cp and detected_max > cp.total_pages:
                            cp.total_pages = detected_max
                            await db.commit()

                # Extract items
                # Pattern: <article class="post... or finding links with /episode/ or /movie/
                if cat_info["type"] == "series":
                    item_matches = re.findall(
                        r'<a[^>]+href=["\'](https?://[^/]+/episode/[^"\']+)["\'][^>]*>(.*?)</a>',
                        html,
                        re.DOTALL
                    )
                else:
                    item_matches = re.findall(
                        r'<a[^>]+href=["\'](https?://[^/]+/(?:movie|film)/[^"\']+)["\'][^>]*>(.*?)</a>',
                        html,
                        re.DOTALL
                    )

                unique_items = {}
                for href, inner in item_matches:
                    clean_text = re.sub(r'<[^>]+>', ' ', inner).strip()
                    clean_text = re.sub(r'\s+', ' ', clean_text)
                    if len(clean_text) > 4 and href not in unique_items:
                        unique_items[href] = clean_text

                logger.info(f"Page {page} discovered {len(unique_items)} unique items.")

                items_added_on_page = 0
                for item_url, item_title in unique_items.items():
                    async with session_maker() as db:
                        if cat_info["type"] == "series":
                            # Check if episode already exists
                            stmt_chk = select(Episode.id).where(Episode.source_url == item_url)
                            if (await db.execute(stmt_chk)).scalar():
                                continue  # Already scraped, zero duplicate work!

                            series_title, season_num, episode_num = parse_series_title_and_numbers(item_title)

                            # 1. Find or create Series
                            stmt_s = select(Series).where(Series.title == series_title)
                            series = (await db.execute(stmt_s)).scalars().first()
                            if not series:
                                series = Series(
                                    title=series_title,
                                    category=cat_info["cat_code"],
                                    status="ongoing"
                                )
                                db.add(series)
                                await db.commit()
                                await db.refresh(series)

                            # 2. Find or create Season
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

                            # 3. Create Episode
                            watch_url = item_url.replace('/episode/', '/watch/')
                            episode = Episode(
                                season_id=season.id,
                                episode_number=episode_num,
                                title=item_title,
                                source_url=item_url
                            )
                            db.add(episode)
                            await db.commit()
                            await db.refresh(episode)

                            # 4. Resolve video streams
                            streams = await resolve_video_streams_for_url(client, watch_url)
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
                                    logger.info(f"  🚀 Dispatched transit pipeline for Episode {episode.id} (Telegram + Cyberlockers)")
                                except Exception as pipe_err:
                                    logger.warning(f"Could not dispatch transit pipeline for episode: {pipe_err}")

                        else:
                            # Movies
                            stmt_m = select(Movie.id).where(Movie.source_url == item_url)
                            if (await db.execute(stmt_m)).scalar():
                                continue

                            watch_url = item_url.replace('/movie/', '/watch/').replace('/film/', '/watch/')
                            movie = Movie(
                                title=item_title,
                                source_url=item_url,
                                category=cat_info["cat_code"]
                            )
                            db.add(movie)
                            await db.commit()
                            await db.refresh(movie)

                            streams = await resolve_video_streams_for_url(client, watch_url)
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

                            logger.info(f"  + Added [{cat_info['cat_code']} Movie] {item_title} ({len(streams)} streams)")
                            items_added_on_page += 1

                            # Dispatch transit pipeline to download & upload to Telegram + cyberlockers
                            direct_stream = next((s["direct_url"] for s in streams if s.get("direct_url")), None)
                            if direct_stream:
                                try:
                                    from worker.pipeline import process_video_transit_pipeline
                                    process_video_transit_pipeline.delay(movie.id, direct_stream, quality="1080p")
                                    logger.info(f"  🚀 Dispatched transit pipeline for Movie {movie.id} (Telegram + Cyberlockers)")
                                except Exception as pipe_err:
                                    logger.warning(f"Could not dispatch transit pipeline for movie: {pipe_err}")

                # Update Checkpoint after completing page
                async with session_maker() as db:
                    stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == cat_key)
                    cp = (await db.execute(stmt)).scalars().first()
                    if cp:
                        cp.current_page = page + 1
                        cp.items_count += items_added_on_page
                        cp.status = "running"
                        cp.updated_at = datetime.datetime.utcnow()
                        await db.commit()

                page += 1
                pages_crawled += 1
                # Polite throttle to respect server
                await asyncio.sleep(1.0)

            except RuntimeError as re_err:
                if "403" in str(re_err):
                    logger.warning(f"Category {cat_key} blocked by Cloudflare (403). Skipping category.")
                    raise re_err
                raise
            except Exception as e:
                logger.error(f"Error crawling page {page} for {cat_key}: {e}")
                page += 1
                pages_crawled += 1
                await asyncio.sleep(3.0)
                continue


async def run_all_categories(max_pages_per_category: Optional[int] = 5):
    """Crawls all configured categories in sequence."""
    for cat_key in CATEGORIES.keys():
        try:
            await crawl_category(cat_key, max_pages=max_pages_per_category)
        except Exception as e:
            logger.error(f"Failed processing category {cat_key}: {e}")


if __name__ == "__main__":
    import sys
    cat = sys.argv[1] if len(sys.argv) > 1 else "asian-series"
    max_p = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    if cat == "--all":
        asyncio.run(run_all_categories(max_pages_per_category=max_p))
    else:
        asyncio.run(crawl_category(cat, max_pages=max_p))
