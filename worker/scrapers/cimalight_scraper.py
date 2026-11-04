"""
Cimalight (cimalight.charity) Series Scraper & Episode Sync Helper.
Discovers full episode lists (Episode 1 -> Latest) for Arabic/Turkish series
and resolves fast multi-server HLS/MP4 mirrors (1vid.xyz, morencius.com, Mixdrop, Dood, HGCloud).
"""
import json
import logging
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin

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

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Episode, Season, Series, StreamingLink
from worker.scrapers.akwam_crawler import _normalize_series_key, search_akwam_episode_streams

logger = logging.getLogger("cimalight_scraper")

CIMALIGHT_BASE = "https://cimalight.charity"

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)


def _extract_vid(url_or_vid: str) -> Optional[str]:
    if not url_or_vid:
        return None
    m = re.search(r"vid=([a-zA-Z0-9]+)", url_or_vid)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]{6,16}", url_or_vid.strip()):
        return url_or_vid.strip()
    return None


def resolve_cimalight_episode_streams(watch_url_or_vid: str) -> List[Dict[str, Any]]:
    """
    Fetches play.php?vid=<id> on Cimalight and extracts supported fast streaming mirrors
    (1vid.xyz, morencius.com, Mixdrop, DoodStream/Playmogo, HGCloud/Vibuxer, Streamtape, Uqload).
    """
    vid = _extract_vid(watch_url_or_vid)
    if not vid:
        return []

    watch_url = f"{CIMALIGHT_BASE}/watch.php?vid={vid}"
    play_url = f"{CIMALIGHT_BASE}/play.php?vid={vid}"
    streams: List[Dict[str, Any]] = []

    try:
        resp = crequests.get(
            play_url,
            headers={"Referer": watch_url},
            impersonate="chrome120",
            timeout=14,
        )
        if resp.status_code != 200 or not resp.text:
            return []

        embed_urls = re.findall(r'data-embed-url=["\']([^"\']+)["\']', resp.text)
        for raw_u in embed_urls:
            u = raw_u.strip()
            if not u.startswith("http"):
                continue
            u_low = u.lower()

            if "1vid." in u_low:
                prov = "1vid"
            elif "morencius." in u_low:
                prov = "morencius"
            elif "cdnplus." in u_low:
                prov = "cdnplus"
            elif "vidoba." in u_low:
                prov = "vidoba"
            elif "mp4plus." in u_low:
                prov = "mp4plus"
            elif "vidspeed." in u_low:
                prov = "vidspeed"
            elif "anafast." in u_low:
                prov = "anafast"
            elif "mixdrop" in u_low or "mxdrop" in u_low:
                prov = "mixdrop"
            elif any(k in u_low for k in ("dood", "playmogo", "dsvplay", "myvidplay", "d000d")):
                prov = "doodstream"
            elif any(k in u_low for k in ("hgcloud", "streamwish", "vidhide", "byseraguci", "vibuxer", "hanerix")):
                prov = "hgcloud"
            elif "streamtape" in u_low:
                prov = "streamtape"
            elif "uqload." in u_low:
                prov = "uqload"
            else:
                continue

            streams.append(
                {
                    "provider_name": prov,
                    "embed_url": u,
                    "direct_url": u,
                    "quality": "1080p",
                }
            )
    except Exception as e:
        logger.warning(f"Failed resolving Cimalight streams for vid={vid}: {e}")

    deduped = []
    seen = set()
    for s in streams:
        k = s["direct_url"] or s["embed_url"]
        if k not in seen:
            seen.add(k)
            deduped.append(s)
    return deduped


def discover_cimalight_series_episodes(series_title: str, max_pages: int = 4) -> Tuple[Dict[int, Dict[str, Any]], Optional[str]]:
    """
    Discovers all episodes (Episode 1 -> Latest) for `series_title` on Cimalight
    using both search pagination and the in-page episode selector buttons.
    Returns: (episodes_by_num_dict, fallback_poster_url)
    """
    target_key = _normalize_series_key(series_title)
    episodes: Dict[int, Dict[str, Any]] = {}
    fallback_poster: Optional[str] = None

    for pg in range(1, max_pages + 1):
        search_url = f"{CIMALIGHT_BASE}/search.php?keywords={quote(series_title)}&page={pg}"
        try:
            r = crequests.get(search_url, impersonate="chrome120", timeout=14)
            if r.status_code != 200 or not r.text:
                break
            anchors = re.findall(
                r'<a([^>]*href=["\']([^"\']*watch\.php\?vid=[^"\']+)["\'][^>]*)>(.*?)</a>',
                r.text,
                re.DOTALL,
            )
            found_on_page = 0
            for attr_str, href, inner in anchors:
                txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
                if not txt:
                    m_title = re.search(r'title=["\']([^"\']+)["\']', attr_str)
                    if m_title:
                        txt = m_title.group(1).strip()
                if not txt:
                    continue
                if target_key and target_key not in _normalize_series_key(txt):
                    continue
                if re.search(r"الموسم\s*(?:[2-9]|\d{2}|الثاني|الثالث|الرابع|الخامس|السادس|السابع|الثامن|التاسع|العاشر)", txt):
                    continue
                m_ep = re.search(r"الحلقة\s+(\d+)", txt)
                if not m_ep:
                    continue
                ep_num = int(m_ep.group(1))
                vid = _extract_vid(href)
                if not vid:
                    continue
                watch_u = f"{CIMALIGHT_BASE}/watch.php?vid={vid}"
                episodes[ep_num] = {
                    "ep_num": ep_num,
                    "vid": vid,
                    "watch_url": watch_u,
                    "title": f"مسلسل {series_title} - الحلقة {ep_num}",
                }
                found_on_page += 1
            if found_on_page == 0 and pg >= 2:
                break
        except Exception as e:
            logger.debug(f"Cimalight search page {pg} failed for '{series_title}': {e}")

    # Open the latest (or first) discovered episode page to collect all sibling episode buttons (Ep 1..N)
    if episodes:
        anchor_ep = episodes[max(episodes.keys())]
        try:
            rw = crequests.get(anchor_ep["watch_url"], impersonate="chrome120", timeout=14)
            if rw.status_code == 200 and rw.text:
                og_m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', rw.text)
                if og_m:
                    fallback_poster = og_m.group(1).strip()

                btn_anchors = re.findall(
                    r'<a[^>]*href=["\']([^"\']*watch\.php\?vid=[^"\']+)["\'][^>]*>(.*?)</a>',
                    rw.text,
                    re.DOTALL,
                )
                for href, inner in btn_anchors:
                    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
                    m_btn = re.match(r"^(\d+)\s*الحلقة$", txt) or re.match(r"^الحلقة\s*(\d+)$", txt)
                    if m_btn:
                        ep_num = int(m_btn.group(1))
                        vid = _extract_vid(href)
                        if vid and ep_num not in episodes:
                            episodes[ep_num] = {
                                "ep_num": ep_num,
                                "vid": vid,
                                "watch_url": urljoin(CIMALIGHT_BASE + "/", href),
                                "title": f"مسلسل {series_title} - الحلقة {ep_num}",
                            }
        except Exception as e:
            logger.debug(f"Cimalight watch button expansion failed for '{series_title}': {e}")

    return episodes, fallback_poster


def search_cimalight_episode_streams(series_title: str, season_num: int = 1, ep_num: int = 1) -> List[Dict[str, Any]]:
    """
    Cross-source rescue helper: searches Cimalight for a specific (series_title, season_num, ep_num)
    and returns live Cimalight mirrors (morencius, 1vid, vidspeed, etc.).
    """
    clean_title = re.sub(r"\s*:\s*", " ", (series_title or "").strip())
    if not clean_title:
        return []
    target_key = _normalize_series_key(clean_title)
    if not target_key:
        return []

    query = f"{clean_title} الحلقة {ep_num}" if season_num == 1 else f"{clean_title} الموسم {season_num} الحلقة {ep_num}"
    matched_vids: List[str] = []
    try:
        r = crequests.get(f"{CIMALIGHT_BASE}/search.php?keywords={quote(query)}", impersonate="chrome120", timeout=12)
        if r.status_code == 200 and r.text:
            anchors = re.findall(
                r'<a([^>]*href=["\']([^"\']*watch\.php\?vid=[^"\']+)["\'][^>]*)>(.*?)</a>',
                r.text,
                re.DOTALL,
            )
            for attr_str, href, inner in anchors:
                txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
                if not txt:
                    m_t = re.search(r'title=["\']([^"\']+)["\']', attr_str)
                    if m_t:
                        txt = m_t.group(1).strip()
                if not txt or target_key not in _normalize_series_key(txt):
                    continue
                m_ep = re.search(r"الحلقة\s+(\d+)", txt)
                if not m_ep or int(m_ep.group(1)) != int(ep_num):
                    continue
                if season_num == 1 and re.search(r"الموسم\s*(?:[2-9]|\d{2}|الثاني|الثالث|الرابع|الخامس)", txt):
                    continue
                vid = _extract_vid(href)
                if vid and vid not in matched_vids:
                    matched_vids.append(vid)
    except Exception as e:
        logger.debug(f"Cimalight episode rescue search failed for '{series_title}' S{season_num:02d}E{ep_num:02d}: {e}")

    out: List[Dict[str, Any]] = []
    seen = set()
    for vid in matched_vids[:2]:
        for s in resolve_cimalight_episode_streams(vid):
            k = s.get("direct_url") or s.get("embed_url")
            if k and k not in seen:
                seen.add(k)
                out.append(s)
    return out


def search_cimalight_movie_streams(
    movie_title: str,
    year: Optional[int] = None,
    orig_title: Optional[str] = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Searches Cimalight for a movie or anime movie by title/original_title (+ optional year)
    and returns (watch_url, resolved_streams).
    """
    queries = []
    for cand in (movie_title, orig_title):
        if cand and cand.strip():
            q = re.sub(r"\s*:\s*", " ", cand.strip())
            if q not in queries:
                queries.append(q)

    def _norm_mv(t: str) -> str:
        s = (t or "").strip().lower()
        s = re.sub(r"^(?:مشاهدة\s+)?(?:وتحميل\s+)?(?:فيلم|انمي|أنمي)\s+", "", s)
        s = re.sub(r"\b(?:مترجم|مترجمة|مدبلج|مدبلجة|اون\s*لاين|أون\s*لاين|hd|bluray|web-dl|كامل)\b", "", s)
        s = re.sub(r"\b(19\d{2}|20\d{2})\b", "", s)
        s = re.sub(r"[أإآ]", "ا", s)
        s = re.sub(r"ة\b", "ه", s)
        s = re.sub(r"ى\b", "ي", s)
        s = re.sub(r"[^a-z0-9\u0600-\u06FF\s]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    target_keys = {_norm_mv(q) for q in queries if _norm_mv(q)}
    if not target_keys:
        return None, []

    matched_vids: List[str] = []
    for q in queries[:2]:
        try:
            r = crequests.get(f"{CIMALIGHT_BASE}/search.php?keywords={quote(q)}", impersonate="chrome120", timeout=12)
            if r.status_code != 200 or not r.text:
                continue
            anchors = re.findall(
                r'<a([^>]*href=["\']([^"\']*watch\.php\?vid=[^"\']+)["\'][^>]*)>(.*?)</a>',
                r.text,
                re.DOTALL,
            )
            for attr_str, href, inner in anchors:
                txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
                if not txt:
                    m_t = re.search(r'title=["\']([^"\']+)["\']', attr_str)
                    if m_t:
                        txt = m_t.group(1).strip()
                if not txt:
                    continue
                if "الحلقة" in txt or "الموسم" in txt or "مسلسل" in txt:
                    continue
                if "فيلم" not in txt and "انمي" not in txt and "أنمي" not in txt:
                    continue
                m_yr = re.search(r"\b(19\d{2}|20\d{2})\b", txt)
                if year and m_yr and abs(int(m_yr.group(1)) - int(year)) > 1:
                    continue
                cand_k = _norm_mv(txt)
                if not cand_k or not any(cand_k == tk or (len(tk) >= 5 and tk in cand_k and len(cand_k) <= len(tk) + 8) for tk in target_keys):
                    continue
                vid = _extract_vid(href)
                if vid and vid not in matched_vids:
                    matched_vids.append(vid)
        except Exception as e:
            logger.debug(f"Cimalight movie search failed for '{q}': {e}")

    primary_watch = None
    all_streams: List[Dict[str, Any]] = []
    seen = set()
    for vid in matched_vids[:2]:
        st = resolve_cimalight_episode_streams(vid)
        if st and not primary_watch:
            primary_watch = f"{CIMALIGHT_BASE}/watch.php?vid={vid}"
        for s in st:
            k = s.get("direct_url") or s.get("embed_url")
            if k and k not in seen:
                seen.add(k)
                all_streams.append(s)
    return primary_watch, all_streams


def _fetch_tmdb_tv_metadata(series_title: str, year: Optional[int] = None) -> Dict[str, Any]:
    """Fetches official Arabic TV metadata (HD poster, overview, tmdb_id, vote_average, genres) from TMDB."""
    tmdb_key = getattr(settings, "TMDB_API_KEY", None) or "9acdd2e9bdd2c77d2134171ce08da1db"
    url = f"https://api.themoviedb.org/3/search/tv?api_key={tmdb_key}&query={urllib.parse.quote(series_title)}&language=ar-SA"
    norm_q = _normalize_series_key(series_title)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results") or []
            matched_item = None
            for item in results:
                name = (item.get("name") or "").strip()
                orig = (item.get("original_name") or "").strip()
                n_name = _normalize_series_key(name)
                n_orig = _normalize_series_key(orig)
                air_yr = int(item["first_air_date"][:4]) if item.get("first_air_date") and len(item["first_air_date"]) >= 4 else None
                exact = (n_name == norm_q or n_orig == norm_q)
                partial = bool(norm_q and ((n_name and (norm_q in n_name or n_name in norm_q)) or (n_orig and (norm_q in n_orig or n_orig in norm_q))))
                if exact or partial:
                    if year and air_yr and abs(air_yr - year) > 1 and not exact:
                        continue
                    matched_item = item
                    if exact or (year and air_yr == year):
                        break
            if matched_item:
                item = matched_item
                genre_map = {35: "Comedy", 18: "Drama", 10749: "Romance", 10751: "Family", 80: "Crime", 9648: "Mystery", 10759: "Action"}
                genres = [genre_map[g] for g in (item.get("genre_ids") or []) if g in genre_map]
                air_yr = None
                if item.get("first_air_date") and len(item["first_air_date"]) >= 4:
                    air_yr = int(item["first_air_date"][:4])
                return {
                    "tmdb_id": item.get("id"),
                    "poster_path": item.get("poster_path"),
                    "backdrop_path": item.get("backdrop_path"),
                    "overview": (item.get("overview") or "").strip(),
                    "vote_average": float(item.get("vote_average") or 0.0),
                    "vote_count": int(item.get("vote_count") or 0),
                    "popularity": float(item.get("popularity") or 0.0),
                    "year": air_yr or year,
                    "genres": genres or ["Drama"],
                }
    except Exception as e:
        logger.debug(f"TMDB TV lookup failed for '{series_title}': {e}")
    return {}


def sync_cimalight_series_to_db(
    series_title: str = "أحلى نصيب",
    category: str = "arabic",
    year: int = 2026,
    mark_vip: bool = True,
) -> Optional[int]:
    """
    Synchronizes all available episodes (Episode 1 -> Latest) of `series_title` from Cimalight
    (+ optional Akwam supplementary mirrors) into PostgreSQL in strict ascending order,
    enriches TMDB poster/synopsis/genres, and optionally registers the series in `riva:vip_series_ids`.
    """
    episodes_map, fallback_poster = discover_cimalight_series_episodes(series_title)
    if not episodes_map:
        logger.warning(f"No episodes found on Cimalight for '{series_title}'.")
        return None

    tmdb_meta = _fetch_tmdb_tv_metadata(series_title, year=year)
    norm_target = _normalize_series_key(series_title)

    with SyncSessionLocal() as session:
        series_obj = None
        if tmdb_meta.get("tmdb_id"):
            series_obj = session.query(Series).filter_by(tmdb_id=tmdb_meta["tmdb_id"]).first()
        if not series_obj:
            for cand in session.query(Series).filter(Series.category == category).all():
                if _normalize_series_key(cand.title) == norm_target:
                    series_obj = cand
                    break

        if "نصيب" in series_title:
            default_overview = (
                "تدور أحداث المسلسل في إطار درامي كوميدي رومانسي حول راكان ونورة، "
                "حيث يضطران لخوض ترتيب زواج مؤقت أمام العائلة والمجتمع قبل أن تتحول التمثيلية إلى مشاعر حقيقية ومفارقات غير متوقعة."
            )
            orig_title_val = series_title
        elif "دافنين" in series_title or "دفنين" in series_title:
            default_overview = (
                "تدور الأحداث في إطار كوميدي حول لصين مبتدئين هما «ميمي» و«حلاوة» ينجحان في سرقة كنز ثمين من الألماس "
                "ويدفنانه لإخفائه قبل القبض عليهما وسجنهما لمدة 3 سنوات، وبعد خروجهما يفاجآن ببناء كمبوند سكني حديث فوق مكان الكنز، "
                "فيضطران للعمل كحارسي أمن داخل الكمبوند لاستعادته وسط مواقف كوميدية مفاجئة."
            )
            orig_title_val = "دفنينو سوى | دفنينو سوا | دفنينه سوا | دفنينه سوى | دافنينو سوا | دافنينو سوى | دافنينه سوا"
        else:
            default_overview = f"مسلسل {series_title} درامي مشوق بجودة عالية."
            orig_title_val = series_title

        if not series_obj:
            series_obj = Series(
                tmdb_id=tmdb_meta.get("tmdb_id"),
                source_url=episodes_map[min(episodes_map.keys())]["watch_url"],
                slug=series_title.replace(" ", "-"),
                category=category,
                title=series_title,
                original_title=orig_title_val,
                overview=tmdb_meta.get("overview") or default_overview,
                year=tmdb_meta.get("year") or year,
                poster_path=tmdb_meta.get("poster_path") or fallback_poster,
                backdrop_path=tmdb_meta.get("backdrop_path"),
                vote_average=tmdb_meta.get("vote_average") or 7.8,
                vote_count=tmdb_meta.get("vote_count") or 25,
                popularity=max(float(tmdb_meta.get("popularity") or 0.0), 150.0),
                genres=tmdb_meta.get("genres") or ["Comedy", "Drama"],
                status="ongoing",
            )
            session.add(series_obj)
            session.flush()
            logger.info(f"✨ Created Series '{series_title}' (ID {series_obj.id}) in PostgreSQL!")
        else:
            if not series_obj.tmdb_id and tmdb_meta.get("tmdb_id"):
                series_obj.tmdb_id = tmdb_meta["tmdb_id"]
            if (not series_obj.poster_path or series_obj.poster_path.startswith("http")) and tmdb_meta.get("poster_path"):
                series_obj.poster_path = tmdb_meta["poster_path"]
            elif not series_obj.poster_path and fallback_poster:
                series_obj.poster_path = fallback_poster
            if not series_obj.backdrop_path and tmdb_meta.get("backdrop_path"):
                series_obj.backdrop_path = tmdb_meta["backdrop_path"]
            if not series_obj.overview and tmdb_meta.get("overview"):
                series_obj.overview = tmdb_meta["overview"]
            if not series_obj.genres:
                series_obj.genres = tmdb_meta.get("genres") or ["Drama"]
            series_obj.category = category
            session.flush()

        series_id = series_obj.id

        season_obj = session.query(Season).filter_by(series_id=series_id, season_number=1).first()
        if not season_obj:
            season_obj = Season(
                series_id=series_id,
                season_number=1,
                title="الموسم 1",
                source_url=series_obj.source_url,
            )
            session.add(season_obj)
            session.flush()

        existing_eps = {
            ep.episode_number: ep
            for ep in session.query(Episode).filter_by(season_id=season_obj.id).all()
        }

        # Find episodes needing stream extraction (brand new OR currently have 0 active links)
        to_resolve: List[Dict[str, Any]] = []
        for ep_num in sorted(episodes_map.keys()):
            ep_info = episodes_map[ep_num]
            db_ep = existing_eps.get(ep_num)
            if not db_ep:
                to_resolve.append(ep_info)
            else:
                active_cnt = (
                    session.query(StreamingLink)
                    .filter(StreamingLink.episode_id == db_ep.id, StreamingLink.status == "active")
                    .count()
                )
                if active_cnt == 0:
                    to_resolve.append(ep_info)

        session.commit()

    if to_resolve:
        def _fetch_ep_streams(ep_info: Dict[str, Any]) -> Tuple[int, Dict[str, Any], List[Dict[str, Any]]]:
            ep_n = ep_info["ep_num"]
            streams = resolve_cimalight_episode_streams(ep_info["vid"])
            # Also merge any supplementary Akwam mirrors if available
            try:
                ak_streams = search_akwam_episode_streams(series_title, 1, ep_n)
                if ak_streams:
                    streams.extend(ak_streams)
            except Exception:
                pass
            return ep_n, ep_info, streams

        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved_batch = list(pool.map(_fetch_ep_streams, to_resolve))

        resolved_batch.sort(key=lambda x: x[0])

        added_eps = 0
        added_links = 0
        with SyncSessionLocal() as session:
            season_obj = session.query(Season).filter_by(series_id=series_id, season_number=1).first()
            earlier_urls: Dict[str, int] = {}
            for ex_ep in session.query(Episode).filter_by(season_id=season_obj.id).all():
                for ex_sl in session.query(StreamingLink).filter_by(episode_id=ex_ep.id).all():
                    if ex_sl.embed_url:
                        earlier_urls.setdefault(ex_sl.embed_url, ex_ep.episode_number)
                    if ex_sl.direct_url:
                        earlier_urls.setdefault(ex_sl.direct_url, ex_ep.episode_number)

            for ep_n, ep_info, streams in resolved_batch:
                unique_streams = []
                for s in streams:
                    em = s.get("embed_url") or ""
                    du = s.get("direct_url") or em
                    if not du:
                        continue
                    owner_em = earlier_urls.get(em)
                    owner_du = earlier_urls.get(du)
                    if (owner_em is not None and owner_em != ep_n) or (owner_du is not None and owner_du != ep_n):
                        continue
                    unique_streams.append(s)

                if not unique_streams:
                    logger.warning(
                        f"⚠️ Skipping '{series_title}' Ep {ep_n}: placeholder page (all {len(streams)} embeds duplicate earlier episodes)."
                    )
                    continue

                db_ep = session.query(Episode).filter_by(season_id=season_obj.id, episode_number=ep_n).first()
                if not db_ep:
                    db_ep = session.query(Episode).filter_by(source_url=ep_info["watch_url"]).first()
                if not db_ep:
                    db_ep = Episode(
                        season_id=season_obj.id,
                        episode_number=ep_n,
                        title=ep_info["title"],
                        source_url=ep_info["watch_url"],
                        status="active",
                    )
                    session.add(db_ep)
                    session.flush()
                    added_eps += 1

                for s in unique_streams:
                    u = s.get("direct_url") or s.get("embed_url")
                    if not u:
                        continue
                    earlier_urls.setdefault(u, ep_n)
                    if s.get("embed_url"):
                        earlier_urls.setdefault(s["embed_url"], ep_n)
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
                        added_links += 1
            session.commit()
        logger.info(
            f"✅ Synced '{series_title}' (ID {series_id}): {len(episodes_map)} total episodes (Ep 1..{max(episodes_map.keys())}), "
            f"+{added_eps} new episodes, +{added_links} active mirror links!"
        )

    if mark_vip:
        try:
            from worker.pipeline import redis_sync
            redis_sync.sadd("riva:vip_series_ids", str(series_id))
            redis_sync.delete(f"riva:series_blocked:{series_id}")
        except Exception as e:
            logger.debug(f"Could not mark VIP in Redis: {e}")

    return series_id

