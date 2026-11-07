"""
TopCinemaa source for Riva (topcinemaa.vip) — movies + series (Arabic-subtitled, 1080p WEB-DL).

Public, un-gated pages only (no challenge bypass). Each episode/movie page exposes a `/watch/` page whose
server list is loaded by the site's own public AJAX endpoint `Single/Server.php` (POST id + server index).
VideoTube (`vidtube.one`) resolves to playable HLS via worker.media_resolver; other servers are kept as
fallback mirrors and are filtered by the dispatcher's preflight like every other provider.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

try:
    from curl_cffi import requests as crequests
except ImportError:  # pragma: no cover
    crequests = None

logger = logging.getLogger("topcinemaa_scraper")

BASE_URL = "https://topcinemaa.vip"
AJAX_URL = f"{BASE_URL}/wp-content/themes/movies2023/Ajaxat/"
# server index -> provider label. UpDown(1)/LuluStream(8) are skipped: not resolvable by our pipeline.
USABLE_SERVERS = {0: "VideoTube", 2: "StreamWish", 3: "Doodstream", 4: "Filelions", 5: "OK", 6: "Uqload", 7: "Streamtape"}

_AR_ORDINALS = {
    "الاول": 1, "الأول": 1, "الثاني": 2, "الثانى": 2, "الثالث": 3, "الرابع": 4, "الخامس": 5,
    "السادس": 6, "السابع": 7, "الثامن": 8, "التاسع": 9, "العاشر": 10,
}
_SKIP_SLUGS = ("category", "genre", "tag", "page", "release", "recent", "movies", "netflix-movies",
               "movies-collections", "top-rating-imdb", "top-rating-imdb-series", "full-packs", "series")


def _q(url: str) -> str:
    return quote(url, safe=":/%?=&")


def _get(url: str, referer: Optional[str] = None, timeout: int = 20):
    headers = {"Referer": _q(referer)} if referer else None
    return crequests.get(_q(url), impersonate="chrome120", timeout=timeout, allow_redirects=True, headers=headers)


def latin_key(text: str) -> str:
    """Lower-case latin/digit tokens only — 'الطبيب الغريب doctor stranger' -> 'doctorstranger'."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower().replace("&", "and"))


def _search_slugs(query: str, pages: int = 4) -> List[Tuple[str, str]]:
    """Returns [(decoded_slug, absolute_url)] for every post link in the search result pages."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for pg in range(1, pages + 1):
        url = f"{BASE_URL}/page/{pg}/?s={quote(query)}" if pg > 1 else f"{BASE_URL}/?s={quote(query)}"
        try:
            r = _get(url)
            if r.status_code != 200:
                break
        except Exception as e:
            logger.debug(f"topcinemaa search failed '{query}' p{pg}: {e}")
            break
        found_new = 0
        for m in re.finditer(r'<a\s+[^>]*href=["\'](https://topcinemaa\.vip/[^"\']+/)["\']', r.text):
            href = m.group(1)
            slug = unquote(href[len(BASE_URL) + 1:]).strip("/")
            if not slug or "/" in slug or slug in seen or slug in _SKIP_SLUGS or slug.startswith(_SKIP_SLUGS[:6]):
                continue
            seen.add(slug)
            out.append((slug, href))
            found_new += 1
        if found_new == 0 or f"/page/{pg + 1}/" not in r.text:
            break
    return out


def find_movie(title_candidates: List[str], year: Optional[int]) -> Optional[str]:
    """Exact-title + year match on `فيلم-<title>-<year>-مترجم-...` slugs. Returns page URL or None."""
    keys = {latin_key(t) for t in title_candidates if latin_key(t)}
    if not keys:
        return None
    for q in list(dict.fromkeys(t for t in title_candidates if t))[:3]:
        for slug, href in _search_slugs(q, pages=2):
            m = re.match(r"^فيلم-(.+?)-((?:19|20)\d{2})(?:-|$)", slug)
            if not m:
                continue
            if latin_key(m.group(1).replace("-", " ")) not in keys:
                continue
            if year and abs(int(m.group(2)) - int(year)) > 0:
                continue
            return href
    return None


def _parse_season(token: Optional[str]) -> int:
    if not token:
        return 1
    token = token.strip("-")
    if token.isdigit():
        return int(token)
    return _AR_ORDINALS.get(token, 1)


def find_series_episodes(name_candidates: List[str], max_pages: int = 8) -> Dict[Tuple[int, int], str]:
    """{(season, episode): page_url} for series whose slug name EXACTLY equals one of the candidates."""
    keys = {latin_key(n) for n in name_candidates if latin_key(n)}
    if not keys:
        return {}
    eps: Dict[Tuple[int, int], str] = {}
    for q in list(dict.fromkeys(n for n in name_candidates if n))[:3]:
        for slug, href in _search_slugs(q, pages=max_pages):
            m = re.match(r"^(?:مسلسل|انمي)-(.+?)(?:-الموسم-([^-]+))?-الحلقة-(\d+)(?:-|$)", slug)
            if not m:
                continue
            if latin_key(m.group(1).replace("-", " ")) not in keys:
                continue
            eps.setdefault((_parse_season(m.group(2)), int(m.group(3))), href)
        if eps:
            break
    return eps


def resolve_streams(page_url: str) -> List[Dict[str, Any]]:
    """Collects playable-candidate embeds from the page's server list (public Server.php AJAX)."""
    watch = _q(page_url.rstrip("/")) + "/watch/"
    try:
        r = _get(watch, referer=page_url)
        if r.status_code != 200:
            return []
        m = re.search(r'data-id="(\d+)"', r.text)
        if not m:
            return []
        pid = m.group(1)
        available = {int(x) for x in re.findall(r'data-server="(\d+)"', r.text)}
    except Exception as e:
        logger.debug(f"topcinemaa watch page failed {page_url}: {e}")
        return []

    streams: List[Dict[str, Any]] = []
    seen = set()
    for idx, label in USABLE_SERVERS.items():
        if available and idx not in available:
            continue
        try:
            rr = crequests.post(
                AJAX_URL + "Single/Server.php", data={"id": pid, "i": idx}, impersonate="chrome120", timeout=15,
                headers={"Referer": watch, "X-Requested-With": "XMLHttpRequest"},
            )
            ifr = re.findall(r'<iframe[^>]+src=["\']([^"\']+)', rr.text or "")
        except Exception:
            continue
        for u in ifr[:1]:
            if u.startswith("//"):
                u = "https:" + u
            if u.startswith("http") and u not in seen:
                seen.add(u)
                streams.append({"provider_name": label, "embed_url": u, "direct_url": u, "quality": "1080p"})
    return streams


def resolve_many(urls: List[str], workers: int = 6) -> Dict[str, List[Dict[str, Any]]]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(urls, pool.map(resolve_streams, urls)))

