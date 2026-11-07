"""
Multi-source backup layer for Riva (public, un-gated Arabic streaming sites only — no challenge bypass).

Sources (in priority order):
  1. topcinemaa.vip  — episode/movie `/watch/` page; servers loaded through the site's public Server.php AJAX.
  2. cimafu.live     — episode/movie `/watch/` page; server embeds are plain `data-server="<url>"` attributes.

Public API (kept compatible with the earlier single-source module):
  find_movie(title_candidates, year)            -> page URL of the best source (or None)
  find_movie_all(title_candidates, year)        -> [page URLs] from every source that has it
  find_series_episodes(names)                   -> {(season, ep): [page URLs from every source]}
  resolve_streams(page_url)                     -> [{provider_name, embed_url, direct_url, quality}]
  resolve_many(urls)                            -> {url: streams}

Sites rotate domains; adjust SOURCES (base + kind) when one changes.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

try:
    from curl_cffi import requests as crequests
except ImportError:  # pragma: no cover
    crequests = None

logger = logging.getLogger("multi_source")

SOURCES: List[Dict[str, str]] = [
    {"name": "topcinemaa", "base": "https://topcinemaa.vip", "kind": "ajax"},
    {"name": "cimafu", "base": "https://cimafu.live", "kind": "data-server"},
]

TC_AJAX = "/wp-content/themes/movies2023/Ajaxat/"
# TopCinemaa server index -> label (UpDown/LuluStream skipped: not resolvable by our pipeline)
TC_SERVERS = {0: "VideoTube", 2: "StreamWish", 3: "Doodstream", 4: "Filelions", 5: "OK", 6: "Uqload", 7: "Streamtape"}

_AR_ORDINALS = {
    "الاول": 1, "الأول": 1, "الثاني": 2, "الثانى": 2, "الثالث": 3, "الرابع": 4, "الخامس": 5,
    "السادس": 6, "السابع": 7, "الثامن": 8, "التاسع": 9, "العاشر": 10,
}
_SKIP_SLUGS = {"recent", "movies", "netflix-movies", "movies-collections", "top-rating-imdb",
               "top-rating-imdb-series", "full-packs", "series", "home", "trending", "latest"}
_SKIP_PREFIX = ("category", "genre", "tag", "page", "release", "wp-", "author", "feed")
_AD_HOSTS = ("tagivi.", "midgerelativelyhoax.", "doubleclick.", "googlesyndication.", "adsterra.", "popads.")
_PROVIDER_BY_HOST = (
    ("vidtube", "VideoTube"), ("streamwish", "StreamWish"), ("hgcloud", "HgCloud"), ("1vid", "1Vid"),
    ("cybervynx", "CyberVynx"), ("dood", "Doodstream"), ("d0o0d", "Doodstream"), ("filelions", "Filelions"),
    ("earnvids", "Filelions"), ("ok.ru", "OK"), ("uqload", "Uqload"), ("streamtape", "Streamtape"),
    ("mixdrop", "Mixdrop"), ("vidhide", "VidHide"), ("lulu", "LuluStream"),
)


def _src_for(url: str) -> Optional[Dict[str, str]]:
    host = urlparse(url).netloc.lower()
    return next((s for s in SOURCES if urlparse(s["base"]).netloc.lower() == host), None)


def _q(url: str) -> str:
    return quote(url, safe=":/%?=&")


def _get(url: str, referer: Optional[str] = None, timeout: int = 20):
    headers = {"Referer": _q(referer)} if referer else None
    return crequests.get(_q(url), impersonate="chrome120", timeout=timeout, allow_redirects=True, headers=headers)


def latin_key(text: str) -> str:
    """Lower-case latin/digit tokens only — 'الطبيب الغريب doctor stranger' -> 'doctorstranger'."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower().replace("&", "and"))


def _search_slugs(src: Dict[str, str], query: str, pages: int = 4) -> List[Tuple[str, str]]:
    base = src["base"]
    host = re.escape(urlparse(base).netloc)
    out: List[Tuple[str, str]] = []
    seen = set()
    for pg in range(1, pages + 1):
        url = f"{base}/page/{pg}/?s={quote(query)}" if pg > 1 else f"{base}/?s={quote(query)}"
        try:
            r = _get(url)
            if r.status_code != 200:
                break
        except Exception as e:
            logger.debug(f"{src['name']} search failed '{query}' p{pg}: {e}")
            break
        new = 0
        for m in re.finditer(rf'<a\s+[^>]*href=["\'](https://{host}/[^"\']+/)["\']', r.text):
            href = m.group(1)
            slug = unquote(href[len(base) + 1:]).strip("/")
            if (not slug or "/" in slug or slug in seen or slug in _SKIP_SLUGS or slug.startswith(_SKIP_PREFIX)):
                continue
            seen.add(slug)
            out.append((slug, href))
            new += 1
        if new == 0 or f"/page/{pg + 1}/" not in r.text:
            break
    return out


def _parse_season(token: Optional[str]) -> int:
    if not token:
        return 1
    token = token.strip("-")
    return int(token) if token.isdigit() else _AR_ORDINALS.get(token, 1)


def find_movie_all(title_candidates: List[str], year: Optional[int]) -> List[str]:
    """Exact-title + year match on `فيلم-<title>-<year>-...` slugs, one URL per source that has it."""
    keys = {latin_key(t) for t in title_candidates if latin_key(t)}
    if not keys:
        return []
    found: List[str] = []
    for src in SOURCES:
        hit = None
        for q in list(dict.fromkeys(t for t in title_candidates if t))[:3]:
            for slug, href in _search_slugs(src, q, pages=2):
                m = re.match(r"^فيلم-(.+?)-((?:19|20)\d{2})(?:-|$)", slug)
                if not m or latin_key(m.group(1).replace("-", " ")) not in keys:
                    continue
                if year and int(m.group(2)) != int(year):
                    continue
                hit = href
                break
            if hit:
                break
        if hit:
            found.append(hit)
    return found


def find_movie(title_candidates: List[str], year: Optional[int]) -> Optional[str]:
    urls = find_movie_all(title_candidates, year)
    return urls[0] if urls else None


def find_series_episodes(name_candidates: List[str], max_pages: int = 8) -> Dict[Tuple[int, int], List[str]]:
    """{(season, episode): [urls from every source]} for slugs whose name EXACTLY equals a candidate."""
    keys = {latin_key(n) for n in name_candidates if latin_key(n)}
    if not keys:
        return {}
    merged: Dict[Tuple[int, int], List[str]] = {}
    for src in SOURCES:
        per_src: Dict[Tuple[int, int], str] = {}
        for q in list(dict.fromkeys(n for n in name_candidates if n))[:3]:
            for slug, href in _search_slugs(src, q, pages=max_pages):
                m = re.match(r"^(?:مسلسل|انمي)-(.+?)(?:-الموسم-([^-]+))?-الحلقة-(\d+)(?:-|$)", slug)
                if not m or latin_key(m.group(1).replace("-", " ")) not in keys:
                    continue
                per_src.setdefault((_parse_season(m.group(2)), int(m.group(3))), href)
            if per_src:
                break
        for slot, href in per_src.items():
            merged.setdefault(slot, []).append(href)
    return merged


def _label(url: str, fallback: str = "Mirror") -> str:
    u = url.lower()
    return next((lbl for key, lbl in _PROVIDER_BY_HOST if key in u), fallback)


def _resolve_ajax(src: Dict[str, str], page_url: str) -> List[Dict[str, Any]]:
    watch = _q(page_url.rstrip("/")) + "/watch/"
    r = _get(watch, referer=page_url)
    if r.status_code != 200:
        return []
    m = re.search(r'data-id="(\d+)"', r.text)
    if not m:
        return []
    pid = m.group(1)
    available = {int(x) for x in re.findall(r'data-server="(\d+)"', r.text)}
    out, seen = [], set()
    for idx, label in TC_SERVERS.items():
        if available and idx not in available:
            continue
        try:
            rr = crequests.post(
                src["base"] + TC_AJAX + "Single/Server.php", data={"id": pid, "i": idx},
                impersonate="chrome120", timeout=15, headers={"Referer": watch, "X-Requested-With": "XMLHttpRequest"},
            )
            ifr = re.findall(r'<iframe[^>]+src=["\']([^"\']+)', rr.text or "")
        except Exception:
            continue
        for u in ifr[:1]:
            u = "https:" + u if u.startswith("//") else u
            if u.startswith("http") and u not in seen:
                seen.add(u)
                out.append({"provider_name": label, "embed_url": u, "direct_url": u, "quality": "1080p"})
    return out


def _resolve_data_server(src: Dict[str, str], page_url: str) -> List[Dict[str, Any]]:
    watch = _q(page_url.rstrip("/")) + "/watch/"
    r = _get(watch, referer=page_url)
    if r.status_code != 200:
        return []
    out, seen = [], set()
    cands = re.findall(r'data-server=["\'](https?://[^"\']+)["\']', r.text) + \
        re.findall(r'<iframe[^>]+(?:src|data-src)=["\'](https?://[^"\']+)["\']', r.text)
    for u in cands:
        if u in seen or any(ad in u.lower() for ad in _AD_HOSTS):
            continue
        seen.add(u)
        out.append({"provider_name": _label(u), "embed_url": u, "direct_url": u, "quality": "1080p"})
    return out


def resolve_streams(page_url: str) -> List[Dict[str, Any]]:
    src = _src_for(page_url)
    if not src:
        return []
    try:
        return _resolve_ajax(src, page_url) if src["kind"] == "ajax" else _resolve_data_server(src, page_url)
    except Exception as e:
        logger.debug(f"{src['name']} resolve failed {page_url}: {e}")
        return []


def resolve_many(urls: List[str], workers: int = 6) -> Dict[str, List[Dict[str, Any]]]:
    urls = list(dict.fromkeys(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(urls, pool.map(resolve_streams, urls)))

