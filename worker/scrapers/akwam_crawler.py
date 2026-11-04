"""
Akwam (akwams.org) Production Scraper.
Extracts Movies, Series, Seasons, Episodes, and Streaming/Download Links (DoodStream, Mixdrop, HGCloud/StreamWish, Uqload, etc.).
Enforces complete series episode discovery (Episode 1 -> N) and sibling movie franchise discovery
WITHOUT firing unordered direct Celery dispatches (all uploads are strictly sequenced by batch_dispatcher).
"""
import html as html_lib
import re
import datetime
import logging
import asyncio
from typing import Optional, Dict, Any, List
from urllib.parse import unquote, quote

try:
    from curl_cffi import requests as crequests
    HAS_CURL_CFFI = True
except ImportError:
    import httpx
    HAS_CURL_CFFI = False

    class HttpFallback:
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

    crequests = HttpFallback()

from sqlalchemy import select, create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    Series, Season, Episode, Movie, StreamingLink, ScraperCheckpoint
)
from worker.franchise_ordering import parse_movie_franchise_info, sort_franchise_movies

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("akwam_crawler")

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)

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
        "url": "https://akwams.org/category/movies/%d8%a7%d9%81%d9%84%d8%a7%d9%85-%d8%ac%d9%86%d8%a8%d9%8a/".replace("%d8%ac%d9%86%d8%a8%d9%8a", "%d8%a7%d8%ac%d9%86%d8%a8%d9%8a"),
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
    "الثابع": 7,
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

SKIP_PATH_TOKENS = (
    "/category/", "/page/", "/tag/", "/login", "/logout", "/profile",
    "/favorite", "/dmca", "/ad-policy", "/wp-json", "/wp-admin",
    "/wp-content", "/feed/", "/series/", "/author/", "/comments/"
)


def _extract_card_title(inner_html: str) -> str:
    """Extracts the clean post heading from an Akwam grid card's inner HTML."""
    if not inner_html:
        return ""
    for pat in (
        r"<h[1-6][^>]*>(.*?)</h[1-6]>",
        r'class=["\'][^"\']*title[^"\']*["\'][^>]*>(.*?)</',
        r'title=["\']([^"\']{8,})["\']',
    ):
        m = re.search(pat, inner_html, re.DOTALL | re.I)
        if m:
            txt = html_lib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
            txt = re.sub(r"\s+", " ", txt).strip()
            if len(txt) > 6 and txt not in ("مشاهدة", "تحميل", "مشاهدة وتحميل"):
                return txt

    clean_text = html_lib.unescape(re.sub(r"<[^>]+>", " ", inner_html))
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    matches = list(
        re.finditer(
            r"(?:مشاهدة|تحميل)\s+(?:وتحميل\s+)?(?:مسلسل|انمي|أنمي|برنامج|فيلم)\s+.+$",
            clean_text,
        )
    )
    if matches:
        return matches[-1].group(0).strip()
    return clean_text


def _normalize_series_key(title: str) -> str:
    """Normalizes Arabic/Turkish series titles for reliable episode-to-series matching."""
    t = (title or "").strip().lower()
    t = re.sub(r"^(?:مشاهدة\s+)?(?:وتحميل\s+)?(?:مسلسل|انمي|أنمي|برنامج)\s+", "", t)
    t = re.sub(
        r"\b(?:التركي|تركية|تركي|السعودي|سعودي|العربي|عربي|مترجم|مترجمة|مدبلج|مدبلجة|اون\s*لاين|أون\s*لاين|hd|كاملة?|حصريا)\b",
        "",
        t,
    )
    # Strip trailing truncated WordPress slug tokens like "ال 10" or "الح 2" or "الحل 5"
    t = re.sub(r"\s+(?:ال|الح|الحل|الحلق|م|مت|متر|مد|مدب|مدبل)(?:\s+\d+)?$", "", t)
    t = re.sub(r"[أإآ]", "ا", t)
    t = re.sub(r"ة\b", "ه", t)
    t = re.sub(r"ى\b", "ي", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_ARABIC_EP_ORDINAL_WORDS_RE = re.compile(
    r"الحلقة\s+\d+(?:\s+(?:ال)?(?:أولى|اولى|الأولي|اولي|ثانية|ثالثة|رابعة|خامسة|سادسة|سابعة|ثامنة|تاسعة|عاشرة|"
    r"حادية|إحدى|احدى|ثاني|ثالث|رابع|خامس|سادس|سابع|ثامن|تاسع|"
    r"عشرون|عشرين|ثلاثون|ثلاثين|أربعون|اربعون|أربعين|اربعين|خمسون|خمسين|ستون|ستين|سبعون|سبعين|ثمانون|ثمانين|تسعون|تسعين|مائة|مئة|عشرة|عشر|واحدة|واحد)"
    r"(?:\s+و?(?:ال)?(?:عشرون|عشرين|ثلاثون|ثلاثين|أربعون|اربعون|أربعين|اربعين|خمسون|خمسين|ستون|ستين|سبعون|سبعين|ثمانون|ثمانين|تسعون|تسعين|عشرة|عشر))?)?\b",
    flags=re.I,
)


def parse_series_title(raw_text: str, item_url: str = ""):
    card_clean = _extract_card_title(raw_text) if "<" in raw_text or "القصه" in raw_text else raw_text.strip()
    slug = unquote(item_url.strip("/").split("/")[-1]).replace("-", " ") if item_url else ""
    text = (
        card_clean
        if len(card_clean) > 8 and card_clean not in ["مشاهدة", "تحميل", "مشاهدة وتحميل"]
        else (slug or card_clean)
    )
    text = unquote(text).replace("-", " ")

    season_num = 1
    s_match = re.search(r"(?:الموسم|الجزء)\s+(الحادي\s+عشر|الثاني\s+عشر|[^\s]+)", text)
    if s_match:
        val = s_match.group(1).strip()
        season_num = ARABIC_ORDINALS.get(val, int(val) if val.isdigit() else 1)

    ep_num = 1
    e_match = re.search(r"الحلقة\s+(\d+)", text)
    if e_match:
        ep_num = int(e_match.group(1))

    year = None
    y_match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if y_match:
        year = int(y_match.group(1))

    clean = re.sub(r"^(?:مشاهدة\s+)?(?:وتحميل\s+)?(?:مسلسل|انمي|أنمي|برنامج)\s+", "", text, flags=re.I)
    clean = re.sub(r"(?:الموسم|الجزء)\s+(?:الحادي\s+عشر|الثاني\s+عشر|[^\s]+)", "", clean)
    clean = _ARABIC_EP_ORDINAL_WORDS_RE.sub("", clean)
    clean = re.sub(r"\b(19\d{2}|20\d{2})\b", "", clean)
    clean = re.sub(
        r"\b(?:مترجم|مترجمة|مدبلج|مدبلجة|اون\s*لاين|أون\s*لاين|HD|كاملة?|مت|متر|مدب|مدبل|والاخيرة|الاخيرة|والأخيرة|الأخيرة)\b",
        "",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"\s+", " ", clean).strip(" -:|/")
    return clean or "مسلسل", season_num, ep_num, year


def parse_movie_title(raw_text: str, item_url: str = ""):
    slug = unquote(item_url.strip("/").split("/")[-1]).replace("-", " ") if item_url else ""
    text = (
        raw_text.strip()
        if len(raw_text.strip()) > 8 and raw_text.strip() not in ["مشاهدة", "تحميل", "مشاهدة وتحميل"]
        else (slug or raw_text)
    )
    text = unquote(text).replace("-", " ")

    year = None
    y_match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if y_match:
        year = int(y_match.group(1))

    clean = re.sub(r"^(?:مشاهدة\s+)?(?:وتحميل\s+)?(?:فيلم|انمي|أنمي)\s+", "", text, flags=re.I)
    clean = re.sub(r"\b(19\d{2}|20\d{2})\b", "", clean)
    clean = re.sub(r"\b(?:مترجم|مترجمة|مدبلج|مدبلجة|اون\s+لاين|HD|كامل|تحميل)\b", "", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip(" -:|/")
    return clean or "فيلم", year


def resolve_akwam_streams(post_url: str) -> List[Dict[str, Any]]:
    """
    Fetches the /download and /watch pages of an Akwam post and extracts playable streaming links
    across supported fast mirrors (HGCloud/StreamWish/VidHide, DoodStream/Playmogo, Mixdrop, Streamtape, Uqload).
    """
    streams = []
    base_clean = post_url.rstrip("/")

    # 1. Fetch /watch page for iframe players
    try:
        r_watch = crequests.get(f"{base_clean}/watch", impersonate="chrome120", timeout=12)
        if r_watch.status_code == 200:
            iframes = re.findall(r'<iframe\s+[^>]*src=["\']([^"\']+)["\']', r_watch.text)
            for ifr in iframes:
                ifr_l = ifr.lower()
                if not ifr.startswith("http"):
                    continue
                if any(k in ifr_l for k in ["mixdrop", "mxdrop"]):
                    prov = "mixdrop"
                elif any(k in ifr_l for k in ["dood", "playmogo", "dsvplay", "myvidplay", "d000d"]):
                    prov = "doodstream"
                elif "streamtape" in ifr_l:
                    prov = "streamtape"
                elif any(k in ifr_l for k in ["hgcloud", "streamwish", "vidhide", "byseraguci", "vibuxer", "hanerix"]):
                    prov = "hgcloud"
                elif "uqload." in ifr_l:
                    prov = "uqload"
                else:
                    continue
                streams.append({
                    "provider_name": prov,
                    "embed_url": ifr,
                    "direct_url": ifr,
                    "quality": "1080p"
                })
    except Exception as e:
        logger.warning(f"Error fetching watch page {base_clean}/watch: {e}")

    # 2. Fetch /download page for all supported cyberlocker and CDN mirror links
    try:
        r_dl = crequests.get(f"{base_clean}/download", impersonate="chrome120", timeout=12)
        if r_dl.status_code == 200:
            html_dl = r_dl.text
            dl_links = re.findall(r'href=["\']([^"\']+)["\']', html_dl)
            for link in dl_links:
                link_l = link.lower()
                if not link.startswith("http"):
                    continue
                if any(k in link_l for k in ["/category/", "/page/", "/tag/", "/login", "facebook", "twitter", "telegram", "wp-content", "akwams."]):
                    continue

                if any(k in link_l for k in ["doodstream", "playmogo", "dsvplay", "myvidplay", "d000d"]):
                    code_m = re.search(r"/(?:d|e)/([a-zA-Z0-9]+)", link)
                    code = code_m.group(1) if code_m else link.rstrip("/").split("/")[-1]
                    streams.append({
                        "provider_name": "doodstream",
                        "embed_url": f"https://playmogo.com/e/{code}",
                        "direct_url": link,
                        "quality": "1080p"
                    })
                elif "streamtape" in link_l:
                    code_m = re.search(r"/(?:v|e)/([a-zA-Z0-9]+)", link)
                    code = code_m.group(1) if code_m else link.rstrip("/").split("/")[-1]
                    streams.append({
                        "provider_name": "streamtape",
                        "embed_url": f"https://streamtape.com/e/{code}",
                        "direct_url": link,
                        "quality": "1080p"
                    })
                elif "mixdrop" in link_l or "mxdrop" in link_l:
                    code_m = re.search(r"/(?:f|e)/([a-zA-Z0-9]+)", link)
                    code = code_m.group(1) if code_m else link.rstrip("/").split("/")[-1]
                    streams.append({
                        "provider_name": "mixdrop",
                        "embed_url": f"https://mxdrop.click/e/{code}",
                        "direct_url": link,
                        "quality": "1080p"
                    })
                elif any(k in link_l for k in ["hgcloud", "streamwish", "vidhide", "byseraguci", "vibuxer", "hanerix"]):
                    code_m = re.search(r"/(?:d|e|f|v)/([a-zA-Z0-9]+)", link)
                    code = code_m.group(1) if code_m else link.rstrip("/").split("/")[-1]
                    streams.append({
                        "provider_name": "hgcloud",
                        "embed_url": f"https://vibuxer.com/e/{code}",
                        "direct_url": f"https://vibuxer.com/e/{code}",
                        "quality": "1080p"
                    })
                elif "uqload." in link_l:
                    code_m = re.search(r"uqload\.[a-z]+/(?:embed-)?([a-zA-Z0-9]+)", link_l)
                    if code_m:
                        code = code_m.group(1)
                        streams.append({
                            "provider_name": "uqload",
                            "embed_url": f"https://uqload.vc/embed-{code}.html",
                            "direct_url": f"https://uqload.vc/embed-{code}.html",
                            "quality": "1080p"
                        })
    except Exception as e:
        logger.warning(f"Error fetching download page {base_clean}/download: {e}")

    seen = set()
    deduped = []
    for s in streams:
        key = s["direct_url"] or s["embed_url"]
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    return deduped


def search_akwam_episode_streams(series_title: str, sea_num: int, ep_num: int) -> List[Dict[str, Any]]:
    """
    Searches Akwam directly for a specific series episode (e.g. when its old URL died)
    and extracts fresh playable mirror links across all matching posts.
    """
    if not series_title:
        return []
    target_key = _normalize_series_key(series_title)
    streams: List[Dict[str, Any]] = []
    queries = [f"{series_title} الحلقة {ep_num}"]
    if sea_num > 1:
        queries.insert(0, f"{series_title} الموسم {sea_num} الحلقة {ep_num}")

    seen_ep_urls = set()
    for q in queries:
        try:
            r = crequests.get(f"{BASE_URL}/?s={quote(q)}", impersonate="chrome120", timeout=12)
            if r.status_code != 200:
                continue
            anchors = re.findall(
                r'<a\s+[^>]*href=["\'](https?://akwams\.org/[^"\'?#]+/)["\'][^>]*>(.*?)</a>',
                r.text,
                re.DOTALL,
            )
            for ep_u, inner in anchors:
                if ep_u in seen_ep_urls or any(tok in ep_u for tok in SKIP_PATH_TOKENS):
                    continue
                card_t = _extract_card_title(inner)
                if "الحلقة" not in card_t and "الحلقة" not in unquote(ep_u):
                    continue
                s_title, p_sea, p_ep, _ = parse_series_title(card_t, item_url=ep_u)
                if _normalize_series_key(s_title) == target_key and p_ep == ep_num and p_sea == sea_num:
                    seen_ep_urls.add(ep_u)
                    streams.extend(resolve_akwam_streams(ep_u))
        except Exception as e:
            logger.debug(f"search_akwam_episode_streams failed for '{q}': {e}")

    deduped = []
    seen_k = set()
    for s in streams:
        k = s.get("direct_url") or s.get("embed_url")
        if k and k not in seen_k:
            seen_k.add(k)
            deduped.append(s)
    return deduped


def backfill_akwam_series_episodes_sync(
    sample_ep_url: str,
    series_id: int,
    cat_code: str = "western",
    max_search_pages: int = 28,
) -> int:
    """
    Discovers ALL episodes of a series (Season 1..N, Episode 1..M) on Akwam using:
      1. Full WordPress search pagination (?s=<series_title>, pages 1..max_search_pages)
         with HTML card heading extraction (handles truncated Arabic WordPress URL slugs!).
      2. Episode page links + /series/.../ taxonomy pages (if present).
    Sorts episodes strictly in ascending order (S01E01 -> S01E02 -> ...) and inserts
    missing episodes + resolved mirror links in parallel.
    """
    from concurrent.futures import ThreadPoolExecutor

    added_count = 0
    try:
        with SyncSessionLocal() as session:
            series_obj = session.query(Series).filter_by(id=series_id).first()
            if not series_obj or not series_obj.title:
                return 0
            series_title = series_obj.title.strip()
            target_key = _normalize_series_key(series_title)

        # Map ep_url -> best extracted title
        candidate_ep_cards: Dict[str, str] = {}

        def _collect_ep_cards_from_html(page_html: str):
            raw_anchors = re.findall(
                r'<a\s+[^>]*href=["\'](https?://akwams\.org/[^"\'?#]+/)["\'][^>]*>(.*?)</a>',
                page_html,
                re.DOTALL,
            )
            for u, inner in raw_anchors:
                if any(tok in u for tok in SKIP_PATH_TOKENS):
                    continue
                card_title = _extract_card_title(inner)
                dec_u = unquote(u)
                if "الحلقة" in card_title or "الحلقة" in dec_u:
                    prev = candidate_ep_cards.get(u, "")
                    if not prev or ("الحلقة" in card_title and "الحلقة" not in prev) or len(card_title) > len(prev):
                        candidate_ep_cards[u] = card_title

        # 1. Inspect sample_ep_url + any /series/ taxonomy pages
        if sample_ep_url and "akwams." in sample_ep_url:
            try:
                r_ep = crequests.get(sample_ep_url, impersonate="chrome120", timeout=14)
                if r_ep.status_code == 200:
                    _collect_ep_cards_from_html(r_ep.text)
                    series_tax_urls = list(dict.fromkeys(
                        re.findall(r'href=["\'](https?://akwams\.org/series/[^"\'?#]+/)["\']', r_ep.text)
                    ))
                    series_tax_urls = [u for u in series_tax_urls if "/feed/" not in u and "/page/" not in u]
                    visited_tax = set()
                    for tax_url in series_tax_urls[:4]:
                        for pg in range(1, 8):
                            pg_url = f"{tax_url}page/{pg}/" if pg > 1 else tax_url
                            if pg_url in visited_tax:
                                continue
                            visited_tax.add(pg_url)
                            r_tax = crequests.get(pg_url, impersonate="chrome120", timeout=12)
                            if r_tax.status_code != 200:
                                break
                            _collect_ep_cards_from_html(r_tax.text)
                            if f"/page/{pg + 1}/" not in r_tax.text:
                                break
            except Exception as sample_err:
                logger.debug(f"Sample URL inspect warning for series {series_id}: {sample_err}")

        # 2. Full Akwam Search Pagination (?s=<series_title>) to capture Ep 1..Last even with truncated slugs
        q_enc = quote(series_title)
        for pg in range(1, max_search_pages + 1):
            s_url = f"{BASE_URL}/page/{pg}/?s={q_enc}" if pg > 1 else f"{BASE_URL}/?s={q_enc}"
            try:
                r_s = crequests.get(s_url, impersonate="chrome120", timeout=14)
                if r_s.status_code != 200:
                    break
                before_cnt = len(candidate_ep_cards)
                _collect_ep_cards_from_html(r_s.text)
                if f"/page/{pg + 1}/" not in r_s.text and len(candidate_ep_cards) == before_cnt:
                    break
                if f"/page/{pg + 1}/" not in r_s.text and f"page/{pg + 1}" not in r_s.text:
                    break
            except Exception:
                break

        if not candidate_ep_cards:
            return 0

        prefer_dubbed = "مدبل" in unquote(sample_ep_url or "")
        slot_urls: Dict[tuple, List[str]] = {}
        for ep_u, card_title in candidate_ep_cards.items():
            s_title, sea_num, ep_num, _ = parse_series_title(card_title, item_url=ep_u)
            if _normalize_series_key(s_title) != target_key:
                continue
            if "الحلقة" not in (card_title or "") and "الحلقة" not in unquote(ep_u):
                continue
            slot = (sea_num, ep_num)
            slot_urls.setdefault(slot, []).append(ep_u)

        if not slot_urls:
            return 0

        # Order URLs within each slot so preferred dub/sub comes first, keeping alternates as mirror fallback
        ordered_slots = sorted(slot_urls.keys())
        for slot in ordered_slots:
            slot_urls[slot].sort(
                key=lambda u: 0 if (("مدبل" in unquote(u)) == prefer_dubbed) else 1
            )

        # Determine which slots actually need inserting or link refreshing
        slots_to_resolve = []
        with SyncSessionLocal() as session:
            for sea_num, ep_num in ordered_slots:
                season = session.query(Season).filter_by(series_id=series_id, season_number=sea_num).first()
                if season:
                    ep_existing = session.query(Episode).filter_by(season_id=season.id, episode_number=ep_num).first()
                    if ep_existing:
                        active_cnt = (
                            session.query(StreamingLink)
                            .filter_by(episode_id=ep_existing.id, status="active")
                            .count()
                        )
                        if active_cnt > 0:
                            continue
                slots_to_resolve.append((sea_num, ep_num, slot_urls[(sea_num, ep_num)]))

        if not slots_to_resolve:
            return 0

        logger.info(
            f"  🔎 Backfilling {len(slots_to_resolve)} missing/empty episode slot(s) for '{series_title}' (ID {series_id}) in parallel..."
        )

        def _resolve_slot(item):
            sea_n, ep_n, urls = item
            merged_streams = []
            chosen_url = urls[0]
            seen_s = set()
            for u in urls[:2]:
                st_list = resolve_akwam_streams(u)
                if st_list and chosen_url == urls[0] and not merged_streams:
                    chosen_url = u
                for s in st_list:
                    k = s.get("direct_url") or s.get("embed_url")
                    if k and k not in seen_s:
                        seen_s.add(k)
                        merged_streams.append(s)
            return sea_n, ep_n, chosen_url, merged_streams

        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved_items = list(pool.map(_resolve_slot, slots_to_resolve))

        resolved_items.sort(key=lambda x: (x[0], x[1]))

        with SyncSessionLocal() as session:
            for sea_num, ep_num, ep_u, streams in resolved_items:
                season = (
                    session.query(Season)
                    .filter_by(series_id=series_id, season_number=sea_num)
                    .first()
                )
                if not season:
                    season = Season(
                        series_id=series_id,
                        season_number=sea_num,
                        title=f"الموسم {sea_num}",
                    )
                    session.add(season)
                    session.commit()
                    session.refresh(season)

                ep_existing = (
                    session.query(Episode)
                    .filter_by(season_id=season.id, episode_number=ep_num)
                    .first()
                )
                if ep_existing:
                    target_ep = ep_existing
                    if not target_ep.source_url:
                        target_ep.source_url = ep_u
                else:
                    dup_url = session.query(Episode).filter_by(source_url=ep_u).first()
                    if dup_url:
                        continue
                    target_ep = Episode(
                        season_id=season.id,
                        episode_number=ep_num,
                        title=f"الحلقة {ep_num}",
                        source_url=ep_u,
                    )
                    session.add(target_ep)
                    session.commit()
                    session.refresh(target_ep)

                existing_urls = {
                    r[0]
                    for r in session.query(StreamingLink.embed_url)
                    .filter_by(episode_id=target_ep.id)
                    .all()
                    if r[0]
                }
                for s in streams:
                    if s["embed_url"] in existing_urls:
                        continue
                    session.add(
                        StreamingLink(
                            episode_id=target_ep.id,
                            provider_name=s["provider_name"],
                            embed_url=s["embed_url"],
                            direct_url=s.get("direct_url"),
                            quality=s.get("quality", "1080p"),
                            status="active",
                        )
                    )
                session.commit()
                added_count += 1
                logger.info(
                    f"  📥 Backfilled ordered episode: {series_title} S{sea_num:02d}E{ep_num:02d} ({len(streams)} streams)"
                )

    except Exception as e:
        logger.warning(f"Error backfilling Akwam series {series_id} from {sample_ep_url}: {e}")

    return added_count


def backfill_akwam_movie_franchise_sync(movie_url: str, franchise_key: str, cat_code: str = "western") -> int:
    """
    Inspects an Akwam movie page for related sibling parts of the same movie franchise/collection,
    sorts them from Part 1 -> Part N, and inserts any missing earlier/later parts into PostgreSQL!
    """
    if not movie_url or "akwams." not in movie_url or not franchise_key:
        return 0

    added = 0
    try:
        r_m = crequests.get(movie_url, impersonate="chrome120", timeout=15)
        if r_m.status_code != 200:
            return 0

        candidates = []
        seen_urls = set()
        for u in re.findall(r'href=["\'](https?://akwams\.org/[^"\'?#]+/)["\']', r_m.text):
            if u in seen_urls or any(tok in u for tok in SKIP_PATH_TOKENS):
                continue
            dec = unquote(u)
            if "الحلقة" in dec or "مسلسل" in dec:
                continue
            seen_urls.add(u)
            m_title, m_year = parse_movie_title("", item_url=u)
            if not m_title or m_title in ("فيلم", "مشاهدة"):
                continue
            info = parse_movie_franchise_info(m_title, year=m_year)
            if info["franchise_key"] == franchise_key:
                candidates.append({
                    "id": 0,
                    "url": u,
                    "title": m_title,
                    "year": m_year,
                    "info": info,
                })

        if not candidates:
            return 0

        ordered = sort_franchise_movies(candidates)
        with SyncSessionLocal() as session:
            for item in ordered:
                u = item["url"]
                m_title = item["title"]
                m_year = item["year"]
                exists = (
                    session.query(Movie)
                    .filter((Movie.source_url == u) | (Movie.title == m_title))
                    .first()
                )
                if exists:
                    continue
                new_m = Movie(
                    title=m_title,
                    source_url=u,
                    category=cat_code,
                    year=m_year,
                )
                session.add(new_m)
                session.commit()
                session.refresh(new_m)

                streams = resolve_akwam_streams(u)
                for s in streams:
                    session.add(
                        StreamingLink(
                            movie_id=new_m.id,
                            provider_name=s["provider_name"],
                            embed_url=s["embed_url"],
                            direct_url=s.get("direct_url"),
                            quality=s.get("quality", "1080p"),
                            status="active",
                        )
                    )
                session.commit()
                added += 1
                logger.info(f"  🎬 Backfilled franchise part: {m_title} ({m_year or ''}) [{len(streams)} streams]")
    except Exception as e:
        logger.warning(f"Error backfilling movie franchise '{franchise_key}' from {movie_url}: {e}")

    return added


async def crawl_akwam_category(cat_key: str, max_pages: Optional[int] = 5):
    """
    Crawls a specific category on Akwam up to max_pages and saves records + links cleanly.
    All Celery uploads are strictly sequenced by batch_dispatcher (no out-of-order .delay() here!).
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
                category_name=f"أكوام - {cat_info['name']}",
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

            total_pages_match = re.findall(r"/page/(\d+)/?", html)
            if total_pages_match:
                detected_max = max(int(p) for p in total_pages_match)
                async with AsyncSessionLocal() as db:
                    stmt = select(ScraperCheckpoint).where(ScraperCheckpoint.category_slug == checkpoint_slug)
                    cp_up = (await db.execute(stmt)).scalars().first()
                    if cp_up and detected_max > cp_up.total_pages:
                        cp_up.total_pages = detected_max
                        await db.commit()

            raw_links = re.findall(r'<a\s+[^>]*href=["\'](https?://akwams\.org/[^"\'?#]+/)["\'][^>]*>(.*?)</a>', html, re.DOTALL)
            unique_items = {}
            for h, inner in raw_links:
                if not any(k in h for k in SKIP_PATH_TOKENS):
                    clean_text = _extract_card_title(inner)
                    if len(clean_text) > 2:
                        if h not in unique_items or (len(clean_text) > len(unique_items[h]) and unique_items[h] in ["مشاهدة", "تحميل", "مشاهدة وتحميل"]):
                            unique_items[h] = clean_text

            logger.info(f"Akwam Page {page} discovered {len(unique_items)} unique items.")

            items_added_on_page = 0
            backfilled_series_ids = set()

            for item_url, item_title in unique_items.items():
                async with AsyncSessionLocal() as db:
                    if cat_info["type"] == "series":
                        stmt_chk = select(Episode.id).where(Episode.source_url == item_url)
                        if (await db.execute(stmt_chk)).scalar():
                            continue

                        series_title, season_num, episode_num, s_year = parse_series_title(item_title, item_url=item_url)

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

                        stmt_ep = select(Episode).where(
                            Episode.season_id == season.id,
                            Episode.episode_number == episode_num
                        )
                        episode = (await db.execute(stmt_ep)).scalars().first()
                        if not episode:
                            episode = Episode(
                                season_id=season.id,
                                episode_number=episode_num,
                                title=f"الحلقة {episode_num}",
                                source_url=item_url
                            )
                            db.add(episode)
                            await db.commit()
                            await db.refresh(episode)
                        else:
                            if episode.source_url != item_url and episode.source_url:
                                continue

                        streams = await asyncio.to_thread(resolve_akwam_streams, item_url)
                        for s in streams:
                            db.add(
                                StreamingLink(
                                    episode_id=episode.id,
                                    provider_name=s["provider_name"],
                                    embed_url=s["embed_url"],
                                    direct_url=s.get("direct_url"),
                                    quality=s.get("quality", "1080p"),
                                    status="active"
                                )
                            )
                        await db.commit()

                        logger.info(f"  + Added [{cat_info['cat_code']}] {series_title} S{season_num}E{episode_num} ({len(streams)} streams)")
                        items_added_on_page += 1

                        # If episode > 1 or season > 1, backfill full series Episode 1..N so dispatcher uploads from Ep 1!
                        if (episode_num > 1 or season_num > 1) and series.id not in backfilled_series_ids:
                            backfilled_series_ids.add(series.id)
                            await asyncio.to_thread(
                                backfill_akwam_series_episodes_sync,
                                item_url,
                                series.id,
                                cat_info["cat_code"],
                            )

                    else:
                        stmt_m = select(Movie.id).where(Movie.source_url == item_url)
                        if (await db.execute(stmt_m)).scalar():
                            continue

                        movie_title, m_year = parse_movie_title(item_title, item_url=item_url)
                        if not movie_title or movie_title in ("مشاهدة", "فيلم"):
                            continue
                        stmt_m_title = select(Movie.id).where(Movie.title == movie_title)
                        if (await db.execute(stmt_m_title)).scalar():
                            continue

                        movie = Movie(
                            title=movie_title,
                            source_url=item_url,
                            category=cat_info["cat_code"],
                            year=m_year
                        )
                        db.add(movie)
                        await db.commit()
                        await db.refresh(movie)

                        streams = await asyncio.to_thread(resolve_akwam_streams, item_url)
                        for s in streams:
                            db.add(
                                StreamingLink(
                                    movie_id=movie.id,
                                    provider_name=s["provider_name"],
                                    embed_url=s["embed_url"],
                                    direct_url=s.get("direct_url"),
                                    quality=s.get("quality", "1080p"),
                                    status="active"
                                )
                            )
                        await db.commit()

                        logger.info(f"  + Added [{cat_info['cat_code']} Movie] {movie_title} ({len(streams)} streams)")
                        items_added_on_page += 1

                        # If part of a franchise, backfill sibling parts (Part 1..N) from the page!
                        f_info = parse_movie_franchise_info(movie_title, year=m_year)
                        if f_info["is_explicit_sequel"]:
                            await asyncio.to_thread(
                                backfill_akwam_movie_franchise_sync,
                                item_url,
                                f_info["franchise_key"],
                                cat_info["cat_code"],
                            )

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
