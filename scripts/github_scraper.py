"""
GitHub Actions Automated Scraper for Riva Engine.
Runs on GitHub Actions (Azure clean IPs) to bypass datacenter IP blocks.
Ingests media metadata and direct stream links into the Riva VPS API (http://186.240.145.87:8090).
"""
import os
import re
import json
import time
import httpx
import logging
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("github_scraper")

RIVA_API_BASE = os.getenv("RIVA_API_BASE", "http://186.240.145.87:8090")
RIVA_SECRET_KEY = os.getenv("RIVA_SECRET_KEY", "riva_ingest_token_2026")

AKWAM_BASE_URL = "https://akwams.org"

CATEGORIES = {
    "foreign-movies": {
        "url": f"{AKWAM_BASE_URL}/movies?section=0&category=29&rating=0&year=0&language=0&formats=0&quality=0",
        "name": "أفلام أجنبية",
        "type": "movie",
        "cat_code": "western"
    },
    "foreign-series": {
        "url": f"{AKWAM_BASE_URL}/series?section=0&category=33&rating=0&year=0&language=0&formats=0&quality=0",
        "name": "مسلسلات أجنبية",
        "type": "series",
        "cat_code": "western"
    },
    "arabic-series": {
        "url": f"{AKWAM_BASE_URL}/series?section=0&category=31&rating=0&year=0&language=0&formats=0&quality=0",
        "name": "مسلسلات عربية",
        "type": "series",
        "cat_code": "arabic"
    },
    "arabic-movies": {
        "url": f"{AKWAM_BASE_URL}/movies?section=0&category=30&rating=0&year=0&language=0&formats=0&quality=0",
        "name": "أفلام عربية",
        "type": "movie",
        "cat_code": "arabic"
    },
    "turkish-series": {
        "url": f"{AKWAM_BASE_URL}/series?section=0&category=87&rating=0&year=0&language=0&formats=0&quality=0",
        "name": "مسلسلات تركية",
        "type": "series",
        "cat_code": "turkish"
    },
    "anime-series": {
        "url": f"{AKWAM_BASE_URL}/series?section=0&category=84&rating=0&year=0&language=0&formats=0&quality=0",
        "name": "مسلسلات أنمي",
        "type": "series",
        "cat_code": "anime"
    }
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.7,en;q=0.3",
    "Referer": "https://akwams.org/"
}


def parse_series_title(raw_title: str):
    s_match = re.search(r'الموسم\s+(\d+|[^\s]+)', raw_title)
    season_num = 1
    if s_match:
        val = s_match.group(1).strip()
        arabic_num_map = {"الاول": 1, "الثاني": 2, "الثالث": 3, "الرابع": 4, "الخامس": 5}
        season_num = int(val) if val.isdigit() else arabic_num_map.get(val, 1)

    ep_match = re.search(r'الحلقة\s+(\d+)', raw_title)
    episode_num = int(ep_match.group(1)) if ep_match else 1

    clean_title = raw_title
    for phrase in [
        r'الموسم\s+[^\s]+', r'الحلقة\s+\d+', r'مترجمة?', r'مدبلجة?',
        r'والاخيرة', r'اون لاين', r'كاملة?', r'HD', r'720p', r'1080p', r'WEB-DL', r'BluRay'
    ]:
        clean_title = re.sub(phrase, '', clean_title, flags=re.IGNORECASE)

    clean_title = re.sub(r'\s+', ' ', clean_title).strip(' -_')
    return clean_title, season_num, episode_num


def parse_movie_title(raw_title: str):
    year_match = re.search(r'\b(19\d\d|20\d\d)\b', raw_title)
    year = int(year_match.group(1)) if year_match else None

    clean = raw_title
    for phrase in [
        r'فيلم', r'مترجم', r'مدبلج', r'اون لاين', r'كامل', r'HD', r'720p', r'1080p',
        r'WEB-DL', r'BluRay', r'\b(19\d\d|20\d\d)\b'
    ]:
        clean = re.sub(phrase, '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\s+', ' ', clean).strip(' -_()[]')
    return clean, year


def extract_cyberlocker_links(html: str) -> List[Dict[str, str]]:
    streams = []
    # Doodstream
    dood_matches = re.findall(r'(https?://(?:dood(?:stream)?\.[a-z0-9]+|ds2play\.[a-z]+|dood\.[a-z]+)/[de]/[a-zA-Z0-9]+)', html, re.I)
    for m in set(dood_matches):
        streams.append({"provider_name": "DoodStream", "embed_url": m, "direct_url": m, "quality": "1080p"})

    # Mixdrop
    mix_matches = re.findall(r'(https?://mixdrop\.[a-z0-9]+/[fe]/[a-zA-Z0-9]+)', html, re.I)
    for m in set(mix_matches):
        streams.append({"provider_name": "Mixdrop", "embed_url": m, "direct_url": m, "quality": "1080p"})

    # Vidhide / Streamruby
    vh_matches = re.findall(r'(https?://(?:vidhide\.[a-z0-9]+|streamruby\.[a-z0-9]+)/[ve]/[a-zA-Z0-9]+)', html, re.I)
    for m in set(vh_matches):
        streams.append({"provider_name": "Vidhide", "embed_url": m, "direct_url": m, "quality": "1080p"})

    return streams


async def scrape_and_ingest():
    logger.info(">>> Starting GitHub Actions Scraper for Riva Engine...")
    logger.info(f"Target API Endpoint: {RIVA_API_BASE}")

    async with httpx.AsyncClient(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        for cat_key, cat_info in CATEGORIES.items():
            logger.info(f"\n==========================================")
            logger.info(f"Processing Category: {cat_info['name']} ({cat_key})")
            logger.info(f"==========================================")

            for page in range(1, 3):  # Crawl 2 pages per category per workflow run
                page_url = f"{cat_info['url']}&page={page}"
                logger.info(f"Fetching {page_url}...")

                try:
                    resp = await client.get(page_url)
                    if resp.status_code != 200:
                        logger.warning(f"Page {page} returned status {resp.status_code}. Skipping.")
                        continue

                    html = resp.text

                    # Akwam cards: <div class="entry-box ..."> ... <a href="https://akwams.org/movie/..." or /series/...
                    links = re.findall(r'href=["\'](https?://akwams\.org/(?:movie|series|episode)/[^"\']+)["\']', html)
                    unique_links = list(dict.fromkeys(links))
                    logger.info(f"Page {page} discovered {len(unique_links)} item URLs.")

                    for item_url in unique_links:
                        try:
                            # 1. Fetch item page
                            item_resp = await client.get(item_url)
                            if item_resp.status_code != 200:
                                continue

                            item_html = item_resp.text
                            # Extract page title
                            t_match = re.search(r'<h1[^>]*>(.*?)</h1>', item_html, re.DOTALL)
                            if not t_match:
                                continue
                            raw_title = re.sub(r'<[^>]+>', ' ', t_match.group(1)).strip()

                            # Extract direct download/stream page links
                            # Akwam /download/ or /watch/ links
                            dl_links = re.findall(r'href=["\'](https?://akwams\.org/(?:download|watch)/[^"\']+)["\']', item_html)
                            streams = []

                            for dl_url in dl_links[:3]:
                                try:
                                    dl_resp = await client.get(dl_url)
                                    if dl_resp.status_code == 200:
                                        streams.extend(extract_cyberlocker_links(dl_resp.text))
                                except Exception:
                                    pass

                            # Ingest into Riva VPS Backend
                            if "/movie/" in item_url:
                                title, year = parse_movie_title(raw_title)
                                payload = {
                                    "secret_key": RIVA_SECRET_KEY,
                                    "title": title,
                                    "source_url": item_url,
                                    "category": cat_info["cat_code"],
                                    "year": year,
                                    "streams": streams
                                }
                                ingest_resp = await client.post(f"{RIVA_API_BASE}/v1/ingest/movie", json=payload)
                                if ingest_resp.status_code == 200:
                                    res_data = ingest_resp.json()
                                    logger.info(f"Ingested Movie: '{title}' (ID: {res_data.get('movie_id')}, Dispatched: {res_data.get('dispatched_to_worker')})")
                                else:
                                    logger.warning(f"Ingest failed for '{title}': {ingest_resp.status_code} {ingest_resp.text}")

                            elif "/episode/" in item_url or "/series/" in item_url:
                                series_title, s_num, ep_num = parse_series_title(raw_title)
                                payload = {
                                    "secret_key": RIVA_SECRET_KEY,
                                    "series_title": series_title,
                                    "season_number": s_num,
                                    "episode_number": ep_num,
                                    "episode_title": f"الحلقة {ep_num}",
                                    "source_url": item_url,
                                    "category": cat_info["cat_code"],
                                    "streams": streams
                                }
                                ingest_resp = await client.post(f"{RIVA_API_BASE}/v1/ingest/episode", json=payload)
                                if ingest_resp.status_code == 200:
                                    res_data = ingest_resp.json()
                                    logger.info(f"Ingested Episode: '{series_title}' S{s_num}E{ep_num} (ID: {res_data.get('episode_id')}, Dispatched: {res_data.get('dispatched_to_worker')})")
                                else:
                                    logger.warning(f"Ingest failed for '{series_title}': {ingest_resp.status_code} {ingest_resp.text}")

                            time.sleep(1.0)
                        except Exception as item_err:
                            logger.error(f"Error processing {item_url}: {item_err}")

                except Exception as cat_err:
                    logger.error(f"Error on page {page}: {cat_err}")

    logger.info(">>> GitHub Scraper run completed.")


if __name__ == "__main__":
    import asyncio
    asyncio.run(scrape_and_ingest())
