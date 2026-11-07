"""
🦸 Marvel full-catalog seeder for the Riva pipeline.

Run inside riva_worker:   python -m worker.marvel_seeder [--movies] [--series] [--dry-run]

For every curated Marvel movie / series (release order) it:
  1. Resolves the official TMDB entry (poster, backdrop, Arabic overview, exact release date, rating).
  2. Re-uses the existing DB row (tmdb_id or normalized title + year) so NO duplicate rows are created.
  3. Skips anything already archived in the Telegram Vault.
  4. Movies: hunts live Akwam mirrors and stores them as active StreamingLinks.
     Series: finds the Akwam series title, then backfills ALL seasons/episodes (S01E01 -> latest) with mirrors.
  5. Registers the item in Redis VIP sets (riva:vip_movie_ids / riva:vip_series_ids) so the dispatcher
     uploads Marvel first; the pipeline itself enforces Part 1 -> Part 2 and S01E01 -> S01E02 ordering.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import time
import urllib.parse
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from sqlalchemy import text

from app.models import Episode, Movie, Season, Series, StreamingLink, TelegramArchive
from bot import marvel
from worker.metadata_enricher import TMDB_GENRE_MAP, ensure_arabic_overview
from worker.scrapers.akwam_crawler import (
    BASE_URL as AKWAM_BASE_URL,
    SKIP_PATH_TOKENS,
    _extract_card_title,
    _normalize_series_key,
    backfill_akwam_series_episodes_sync,
    parse_series_title,
)
from worker.scrapers import multi_source as topcinemaa
from worker.scrapers.viral_movies_scraper import (
    SyncSessionLocal,
    _norm_movie_key,
    _tmdb_get_json,
    search_akwam_movie_streams,
)
from worker.scrapers.viral_series_scraper import TV_GENRE_MAP, crequests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("marvel_seeder")

# (display title used in the bot, release year). Display titles are chosen so bot/marvel.py classifies them.
MARVEL_MOVIES: List[Tuple[str, int]] = [
    ("Blade", 1998), ("X-Men", 2000), ("Spider-Man", 2002), ("Blade II", 2002), ("X2: X-Men United", 2003),
    ("Hulk", 2003), ("Spider-Man 2", 2004), ("Blade: Trinity", 2004), ("Fantastic Four", 2005),
    ("X-Men: The Last Stand", 2006), ("Ghost Rider", 2007), ("Spider-Man 3", 2007),
    ("Fantastic Four: Rise of the Silver Surfer", 2007), ("Iron Man", 2008), ("The Incredible Hulk", 2008),
    ("X-Men Origins: Wolverine", 2009), ("Iron Man 2", 2010), ("Thor", 2011),
    ("X-Men: First Class", 2011), ("Captain America: The First Avenger", 2011),
    ("Ghost Rider: Spirit of Vengeance", 2011), ("The Avengers", 2012), ("The Amazing Spider-Man", 2012),
    ("Iron Man 3", 2013), ("The Wolverine", 2013), ("Thor: The Dark World", 2013),
    ("Captain America: The Winter Soldier", 2014), ("The Amazing Spider-Man 2", 2014),
    ("X-Men: Days of Future Past", 2014), ("Guardians of the Galaxy", 2014),
    ("Avengers: Age of Ultron", 2015), ("Ant-Man", 2015), ("Fantastic Four", 2015), ("Deadpool", 2016),
    ("Captain America: Civil War", 2016), ("X-Men: Apocalypse", 2016), ("Doctor Strange", 2016),
    ("Logan", 2017), ("Guardians of the Galaxy Vol. 2", 2017), ("Spider-Man: Homecoming", 2017),
    ("Thor: Ragnarok", 2017), ("Black Panther", 2018), ("Avengers: Infinity War", 2018), ("Deadpool 2", 2018),
    ("Ant-Man and the Wasp", 2018), ("Venom", 2018), ("Spider-Man: Into the Spider-Verse", 2018),
    ("Captain Marvel", 2019), ("Avengers: Endgame", 2019), ("X-Men: Dark Phoenix", 2019),
    ("Spider-Man: Far From Home", 2019), ("Black Widow", 2021),
    ("Shang-Chi and the Legend of the Ten Rings", 2021), ("Venom: Let There Be Carnage", 2021),
    ("Eternals", 2021), ("Spider-Man: No Way Home", 2021), ("Morbius", 2022),
    ("Doctor Strange in the Multiverse of Madness", 2022), ("Thor: Love and Thunder", 2022),
    ("Black Panther: Wakanda Forever", 2022), ("Ant-Man and the Wasp: Quantumania", 2023),
    ("Guardians of the Galaxy Vol. 3", 2023), ("Spider-Man: Across the Spider-Verse", 2023),
    ("The Marvels", 2023), ("Madame Web", 2024), ("Deadpool & Wolverine", 2024),
    ("Venom: The Last Dance", 2024), ("Kraven the Hunter", 2024), ("Captain America: Brave New World", 2025),
    ("Thunderbolts*", 2025), ("The Fantastic Four: First Steps", 2025), ("Spider-Man: Brand New Day", 2026),
]

# (TMDB search name, first air year)
MARVEL_SERIES: List[Tuple[str, int]] = [
    ("X-Men: The Animated Series", 1992), ("Spider-Man: The Animated Series", 1994),
    ("Marvel's Agents of S.H.I.E.L.D.", 2013), ("Marvel's Agent Carter", 2015), ("Marvel's Daredevil", 2015),
    ("Marvel's Jessica Jones", 2015), ("Marvel's Luke Cage", 2016), ("Marvel's Iron Fist", 2017),
    ("Marvel's The Defenders", 2017), ("Marvel's The Punisher", 2017), ("Marvel's Inhumans", 2017),
    ("Legion", 2017), ("Marvel's Runaways", 2017), ("The Gifted", 2017), ("Marvel's Cloak & Dagger", 2018),
    ("Helstrom", 2020), ("WandaVision", 2021), ("The Falcon and the Winter Soldier", 2021), ("Loki", 2021),
    ("What If...?", 2021), ("Hawkeye", 2021), ("Moon Knight", 2022), ("Ms. Marvel", 2022),
    ("She-Hulk: Attorney at Law", 2022), ("Secret Invasion", 2023), ("Echo", 2024), ("X-Men '97", 2024),
    ("Agatha All Along", 2024), ("Your Friendly Neighborhood Spider-Man", 2025),
    ("Daredevil: Born Again", 2025), ("Ironheart", 2025), ("Eyes of Wakanda", 2025),
    ("Marvel Zombies", 2025), ("Wonder Man", 2026),
]

TODAY = datetime.date.today()


def _redis():
    from worker.pipeline import redis_sync
    return redis_sync


def _parse_date(s: Optional[str]) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _tmdb_search_movie(title: str, year: int) -> Optional[Dict[str, Any]]:
    q = re.sub(r"[*]", "", title)
    for params in (f"&year={year}", f"&primary_release_year={year}", ""):
        data = _tmdb_get_json(f"search/movie?query={urllib.parse.quote(q)}{params}&language=en-US")
        for it in data.get("results") or []:
            rd = _parse_date(it.get("release_date"))
            if rd and abs(rd.year - year) <= 1:
                return it
    return None


def _tmdb_search_tv(name: str, year: int) -> Optional[Dict[str, Any]]:
    variants = [name, re.sub(r"^Marvel's\s+", "", name)]
    for q in dict.fromkeys(variants):
        data = _tmdb_get_json(f"search/tv?query={urllib.parse.quote(q)}&first_air_date_year={year}&language=en-US")
        res = data.get("results") or []
        if not res:
            data = _tmdb_get_json(f"search/tv?query={urllib.parse.quote(q)}&language=en-US")
            res = [r for r in (data.get("results") or []) if (r.get("first_air_date") or "")[:4] == str(year)]
        if res:
            return res[0]
    return None


def _arabic_meta(kind: str, tmdb_id: int) -> Dict[str, Any]:
    try:
        return _tmdb_get_json(f"{kind}/{tmdb_id}?language=ar-SA")
    except Exception:
        return {}


def _ar_overview(ar: Dict[str, Any], en_overview: str, fallback: str) -> str:
    ov = (ar.get("overview") or "").strip()
    if len(re.findall(r"[\u0600-\u06FF]", ov)) >= 15:
        return ov
    return ensure_arabic_overview(en_overview or "") or fallback


# ───────────────────────────── Movies ─────────────────────────────

def _find_existing_movie(session, tmdb_id: int, names: List[str], year: int) -> Optional[Movie]:
    m = session.query(Movie).filter_by(tmdb_id=tmdb_id).first()
    if m:
        return m
    keys = set()
    for n in names:
        k = _norm_movie_key(n)
        if k:
            keys.update({k, re.sub(r"\s+1$", "", k), f"{k} 1"})
    cands = (
        session.query(Movie)
        .filter(Movie.year.between(year - 1, year + 1))
        .filter(text("category IS DISTINCT FROM 'adult'"))
        .all()
    )
    for c in cands:
        ck = {_norm_movie_key(c.title or ""), _norm_movie_key(c.original_title or "")}
        if ck & keys:
            return c
    return None


def seed_movies(dry_run: bool = False) -> Dict[str, Any]:
    stats = Counter()
    report: List[str] = []
    redis_sync = _redis()
    for display, year in MARVEL_MOVIES:
        try:
            if not marvel.classify_movie({"title": display, "original_title": display, "year": year}):
                report.append(f"⚠️ classify-miss {display} ({year})")
            item = _tmdb_search_movie(display, year)
            if not item:
                stats["tmdb_miss"] += 1
                report.append(f"❓ TMDB miss: {display} ({year})")
                continue
            tid = int(item["id"])
            rd = _parse_date(item.get("release_date"))
            if rd and rd > TODAY:
                stats["unreleased"] += 1
                continue
            orig = (item.get("original_title") or display).strip()
            en_title = (item.get("title") or display).strip()

            with SyncSessionLocal() as session:
                m = _find_existing_movie(session, tid, [display, en_title, orig], year)
                if m:
                    archived = session.query(TelegramArchive.id).filter(
                        TelegramArchive.movie_id == m.id, TelegramArchive.file_size_bytes > 15000000
                    ).first()
                    if archived:
                        stats["already_archived"] += 1
                        if not m.release_date and rd:
                            m.release_date = rd
                            session.commit()
                        continue
                if dry_run:
                    report.append(f"DRY {'update' if m else 'create'} {display} ({year}) tmdb={tid}")
                    continue

                ar = _arabic_meta("movie", tid)
                overview = _ar_overview(ar, item.get("overview") or "", f"فيلم {display} من عالم مارفل.")
                genres = [TMDB_GENRE_MAP[g] for g in (item.get("genre_ids") or []) if g in TMDB_GENRE_MAP] or ["Action"]
                pop = max(float(item.get("popularity") or 0.0), 60.0)
                if not m:
                    m = Movie(
                        tmdb_id=tid, category="western", title=display, original_title=orig,
                        overview=overview, year=year, release_date=rd,
                        poster_path=item.get("poster_path") or ar.get("poster_path"),
                        backdrop_path=item.get("backdrop_path") or ar.get("backdrop_path"),
                        vote_average=float(item.get("vote_average") or 0.0),
                        vote_count=int(item.get("vote_count") or 0), popularity=pop, genres=genres,
                    )
                    session.add(m)
                    session.flush()
                    stats["created"] += 1
                else:
                    if not m.tmdb_id and not session.query(Movie.id).filter_by(tmdb_id=tid).first():
                        m.tmdb_id = tid
                    m.release_date = m.release_date or rd
                    m.poster_path = m.poster_path or item.get("poster_path")
                    m.backdrop_path = m.backdrop_path or item.get("backdrop_path")
                    if not m.overview or len(re.findall(r"[\u0600-\u06FF]", m.overview or "")) < 15:
                        m.overview = overview
                    m.popularity = max(float(m.popularity or 0.0), pop)
                    stats["updated"] += 1
                session.commit()
                mid = m.id
                active = session.query(StreamingLink).filter_by(movie_id=mid, status="active").count()

            if active == 0:
                streams = []
                post_u = None

                # 1. Search Akwam
                ak_post, ak_streams = search_akwam_movie_streams(en_title, year=year, orig_title=orig)
                if not ak_streams and display != en_title:
                    ak_post, ak_streams = search_akwam_movie_streams(display, year=year, orig_title=orig)
                if ak_streams:
                    post_u = ak_post
                    streams.extend(ak_streams)

                # 2. Always search Multi-source (TopCinemaa, etc.) to guarantee fresh live mirrors
                tc_urls = topcinemaa.find_movie_all([en_title, display, orig, f"فيلم {en_title}"], year)
                if tc_urls:
                    resolved_pages = topcinemaa.resolve_many(tc_urls)
                    seen_e = {s["embed_url"] for s in streams}
                    for pu in tc_urls:
                        for s_ in resolved_pages.get(pu) or []:
                            if s_["embed_url"] not in seen_e:
                                seen_e.add(s_["embed_url"])
                                streams.append(s_)
                    if not post_u:
                        post_u = next((pu for pu in tc_urls if resolved_pages.get(pu)), None)

                if streams:
                    with SyncSessionLocal() as session:
                        m = session.query(Movie).filter_by(id=mid).first()
                        if post_u and m and not m.source_url and not session.query(Movie.id).filter_by(source_url=post_u).first():
                            m.source_url = post_u

                        existing_links = {
                            l.embed_url: l
                            for l in session.query(StreamingLink).filter_by(movie_id=mid).all()
                        }

                        for s in streams:
                            u = s.get("direct_url") or s.get("embed_url")
                            if not u:
                                continue
                            emb = s["embed_url"]
                            if emb in existing_links:
                                existing_links[emb].status = "active"
                                existing_links[emb].direct_url = u
                            else:
                                session.add(StreamingLink(
                                    movie_id=mid, provider_name=s["provider_name"], embed_url=emb,
                                    direct_url=u, quality="1080p", status="active",
                                ))
                        session.commit()
                        active = session.query(StreamingLink).filter_by(movie_id=mid, status="active").count()
                    stats["mirrors_found"] += 1
                else:
                    stats["no_source"] += 1
                    report.append(f"🚫 no source found: {display} ({year}) id={mid}")

            if active:
                redis_sync.sadd("riva:vip_movie_ids", str(mid))
                redis_sync.delete(f"riva:cd:mv:{mid}")
                redis_sync.delete(f"riva:cool:movie:{mid}")
                stats["queued_vip"] += 1
                report.append(f"✅ queued {display} ({year}) id={mid} mirrors={active}")
        except Exception as e:
            stats["errors"] += 1
            report.append(f"💥 {display} ({year}): {e}")
        time.sleep(0.3)
    return {"stats": dict(stats), "report": report}


# ───────────────────────────── Series ─────────────────────────────

def _akwam_series_title(queries: List[str], target_keys: set) -> Tuple[Optional[str], Optional[str]]:
    """Returns (akwam_series_title, sample_episode_url) for the best-matching Akwam series cards."""
    titles = Counter()
    sample: Dict[str, str] = {}
    for q in queries[:3]:
        q_clean = re.sub(r"\s*:\s*", " ", q.strip())
        for pg in (1, 2):
            url = f"{AKWAM_BASE_URL}/page/{pg}/?s={quote(q_clean)}" if pg > 1 else f"{AKWAM_BASE_URL}/?s={quote(q_clean)}"
            try:
                r = crequests.get(url, impersonate="chrome120", timeout=12)
                if r.status_code != 200 or not r.text:
                    break
                anchors = re.findall(
                    r'<a\s+[^>]*href=["\'](https?://akwams\.org/[^"\'?#]+/)["\'][^>]*>(.*?)</a>', r.text, re.DOTALL
                )
                for u, inner in anchors:
                    if any(tok in u for tok in SKIP_PATH_TOKENS):
                        continue
                    card_t = _extract_card_title(inner)
                    if "الحلقة" not in card_t and "الحلقة" not in unquote(u):
                        continue
                    s_title, _sea, _ep, _ = parse_series_title(card_t, item_url=u)
                    nk = _normalize_series_key(s_title)
                    if nk and any(nk == tk or (len(tk) >= 5 and (tk in nk or nk in tk)) for tk in target_keys):
                        titles[s_title.strip()] += 1
                        sample.setdefault(s_title.strip(), u)
            except Exception:
                break
        if titles:
            break
    if not titles:
        return None, None
    best = titles.most_common(1)[0][0]
    return best, sample[best]


def _find_existing_series(session, tmdb_id: int, names: List[str], year: Optional[int] = None) -> Optional[Series]:
    s = session.query(Series).filter_by(tmdb_id=tmdb_id).first()
    if s:
        return s
    keys = {_normalize_series_key(n) for n in names if n and _normalize_series_key(n)}
    for c in session.query(Series).filter(text("category IS DISTINCT FROM 'adult'")).all():
        if year and c.year and abs(int(c.year) - int(year)) > 1:
            continue  # same name, different era (e.g. X-Men 1992 vs X-Men '97)
        if {_normalize_series_key(c.title or ""), _normalize_series_key(c.original_title or "")} & keys:
            return c
    return None


def _tmdb_tv_structure(tid: int) -> Dict[int, int]:
    """{season_number: episode_count} from TMDB (specials excluded)."""
    try:
        d = _tmdb_get_json(f"tv/{tid}?language=en-US")
        return {int(s["season_number"]): int(s.get("episode_count") or 0) for s in d.get("seasons") or [] if s.get("season_number")}
    except Exception:
        return {}


def _topcinemaa_validated_episodes(names: List[str], tid: int) -> Dict[Tuple[int, int], List[str]]:
    """Backup-source episodes ({slot: [urls from every source]}) for an exact-name match, kept only if they fit
    TMDB's season/episode structure (guards against namesake shows such as another series called 'Echo')."""
    found = topcinemaa.find_series_episodes(names)
    if not found:
        return {}
    structure = _tmdb_tv_structure(tid)
    if not structure:
        return found
    ok = {(s, e): u for (s, e), u in found.items() if s in structure and 1 <= e <= max(structure[s], 1)}
    return ok if len(ok) >= max(1, int(0.6 * len(found))) else {}


def _store_topcinemaa_episodes(series_id: int, eps: Dict[Tuple[int, int], List[str]]) -> int:
    """Creates Season/Episode rows (ascending) + StreamingLinks (all sources merged). Returns new episodes."""
    added = 0
    ordered = sorted(eps.items())
    resolved = topcinemaa.resolve_many([u for _, urls in ordered for u in urls])
    with SyncSessionLocal() as session:
        for (sea_n, ep_n), urls in ordered:
            streams, seen_e, primary = [], set(), None
            for u in urls:
                got = resolved.get(u) or []
                if got and primary is None:
                    primary = u
                for s_ in got:
                    if s_["embed_url"] not in seen_e:
                        seen_e.add(s_["embed_url"])
                        streams.append(s_)
            if not streams:
                continue
            url = primary
            season = session.query(Season).filter_by(series_id=series_id, season_number=sea_n).first()
            if not season:
                season = Season(series_id=series_id, season_number=sea_n, title=f"الموسم {sea_n}")
                session.add(season)
                session.flush()
            ep = session.query(Episode).filter_by(season_id=season.id, episode_number=ep_n).first()
            if not ep:
                if session.query(Episode.id).filter_by(source_url=url).first():
                    continue
                ep = Episode(season_id=season.id, episode_number=ep_n, title=f"الحلقة {ep_n}", source_url=url, status="active")
                session.add(ep)
                session.flush()
                added += 1
            known = {r[0] for r in session.query(StreamingLink.embed_url).filter_by(episode_id=ep.id).all()}
            for s in streams:
                if s["embed_url"] in known:
                    continue
                session.add(StreamingLink(
                    episode_id=ep.id, provider_name=s["provider_name"], embed_url=s["embed_url"],
                    direct_url=s["direct_url"], quality=s["quality"], status="active",
                ))
        session.commit()
    return added


def seed_series(dry_run: bool = False) -> Dict[str, Any]:
    stats = Counter()
    report: List[str] = []
    redis_sync = _redis()
    for name, year in MARVEL_SERIES:
        try:
            item = _tmdb_search_tv(name, year)
            if not item:
                stats["tmdb_miss"] += 1
                report.append(f"❓ TMDB miss: {name} ({year})")
                continue
            tid = int(item["id"])
            fa = _parse_date(item.get("first_air_date"))
            if fa and fa > TODAY:
                stats["unreleased"] += 1
                continue
            en_name = (item.get("name") or name).strip()
            orig = (item.get("original_name") or en_name).strip()
            short = re.sub(r"^Marvel's\s+", "", en_name)
            ar_name = (_arabic_meta("tv", tid).get("name") or "").strip()
            queries = [short, en_name, short.split(":")[0].strip()]
            if ar_name and re.search(r"[\u0600-\u06FF]", ar_name):
                queries.append(ar_name)
            queries = list(dict.fromkeys(q for q in queries if q))
            target_keys = {_normalize_series_key(q) for q in queries if _normalize_series_key(q)}

            with SyncSessionLocal() as session:
                s = _find_existing_series(session, tid, [en_name, short, orig], fa.year if fa else year)
                sid = s.id if s else None
                s_title = s.title if s else None
                s_src = s.source_url if s else None

            ak_title, sample_url = _akwam_series_title(queries, target_keys)
            # Never create a NEW row from a loose substring match (e.g. "Daredevil" vs "Daredevil Born Again"):
            # the Akwam series title must equal one of the TMDB names exactly after normalization.
            if ak_title and _normalize_series_key(ak_title) not in target_keys:
                report.append(f"⛔ loose match rejected: {name} ({year}) != akwam '{ak_title}'")
                ak_title = None

            # Fallback source: TopCinemaa (exact-name slugs, validated against TMDB season/episode structure)
            tc_eps: Dict[Tuple[int, int], List[str]] = {}
            if not ak_title:
                tc_eps = _topcinemaa_validated_episodes([short, en_name, orig], tid)
            if not s and not ak_title and not tc_eps:
                stats["no_source"] += 1
                report.append(f"🚫 no source (Akwam/TopCinemaa): {name} ({year})")
                continue
            if dry_run:
                report.append(
                    f"DRY {'existing id=' + str(sid) if sid else 'create'} {name} -> akwam='{ak_title}' topcinemaa_eps={len(tc_eps)}"
                )
                continue

            ar = _arabic_meta("tv", tid)
            overview = _ar_overview(ar, item.get("overview") or "", f"مسلسل {short} من عالم مارفل.")
            genres = [TV_GENRE_MAP[g] for g in (item.get("genre_ids") or []) if g in TV_GENRE_MAP] or ["Action"]
            with SyncSessionLocal() as session:
                if sid:
                    s = session.query(Series).filter_by(id=sid).first()
                    if not s.tmdb_id and not session.query(Series.id).filter_by(tmdb_id=tid).first():
                        s.tmdb_id = tid
                    s.poster_path = s.poster_path or item.get("poster_path")
                    s.backdrop_path = s.backdrop_path or item.get("backdrop_path")
                    if not s.overview or len(re.findall(r"[\u0600-\u06FF]", s.overview or "")) < 15:
                        s.overview = overview
                    s.original_title = s.original_title or orig
                    stats["existing"] += 1
                else:
                    new_title = ak_title or short
                    first_tc = tc_eps[min(tc_eps)][0] if tc_eps else None
                    src_candidate = sample_url or first_tc
                    src = src_candidate if src_candidate and not session.query(Series.id).filter_by(source_url=src_candidate).first() else None
                    s = Series(
                        tmdb_id=tid, source_url=src, slug=re.sub(r"\s+", "-", new_title)[:120], category="western",
                        title=new_title, original_title=orig, overview=overview, year=fa.year if fa else year,
                        poster_path=item.get("poster_path") or ar.get("poster_path"),
                        backdrop_path=item.get("backdrop_path") or ar.get("backdrop_path"),
                        vote_average=float(item.get("vote_average") or 0.0),
                        vote_count=int(item.get("vote_count") or 0),
                        popularity=max(float(item.get("popularity") or 0.0), 60.0),
                        genres=genres, status="ongoing",
                    )
                    session.add(s)
                    session.flush()
                    stats["created"] += 1
                session.commit()
                sid, s_title, s_src = s.id, s.title, s.source_url

            added = 0
            if ak_title or (s_src and "akwams." in (s_src or "")):
                added += backfill_akwam_series_episodes_sync(sample_url or s_src or "", sid, "western", max_search_pages=28)
            if tc_eps:
                added += _store_topcinemaa_episodes(sid, tc_eps)
            with SyncSessionLocal() as session:
                total_eps = session.execute(text("""
                    SELECT COUNT(*) FROM episodes e JOIN seasons sea ON sea.id = e.season_id WHERE sea.series_id = :sid
                """), {"sid": sid}).scalar() or 0
                done_eps = session.execute(text("""
                    SELECT COUNT(DISTINCT ta.episode_id) FROM telegram_archives ta
                    JOIN episodes e ON e.id = ta.episode_id JOIN seasons sea ON sea.id = e.season_id
                    WHERE sea.series_id = :sid AND ta.file_size_bytes > 15000000
                """), {"sid": sid}).scalar() or 0
            if total_eps > done_eps:
                # Held back by the dispatcher until all Marvel movies are uploaded (movies first, then series)
                redis_sync.sadd("riva:marvel_series_ids", str(sid))
                redis_sync.srem("riva:vip_series_ids", str(sid))
                redis_sync.delete(f"riva:series_blocked:{sid}")
                stats["queued_after_movies"] += 1
            stats["episodes_added"] += added
            report.append(
                f"✅ {s_title} (id={sid}) +{added} eps | total={total_eps} uploaded={done_eps}"
                + ("" if marvel.classify_series({"title": s_title, "original_title": orig, "year": year}) is not None
                   else " ⚠️classify-miss")
            )
        except Exception as e:
            stats["errors"] += 1
            report.append(f"💥 {name} ({year}): {e}")
        time.sleep(0.3)
    return {"stats": dict(stats), "report": report}


def publish_marvel_order() -> int:
    """Writes riva:marvel_rank {movie_id: release-order rank} so the dispatcher uploads Marvel first, in order."""
    with SyncSessionLocal() as session:
        rows = session.execute(text(
            "SELECT id, title, original_title, year, release_date FROM movies "
            "WHERE category IS DISTINCT FROM 'adult';"
        )).mappings().all()
    items = []
    for r in rows:
        d = dict(r)
        if marvel.classify_movie(d):
            rd = d.get("release_date")
            key = (rd.year, rd.month, rd.day) if rd else (int(d.get("year") or 9999), 1, 1)
            items.append((key, d["id"]))
    items.sort()
    redis_sync = _redis()
    redis_sync.delete("riva:marvel_rank")
    if items:
        redis_sync.hset("riva:marvel_rank", mapping={str(mid): str(i + 1) for i, (_, mid) in enumerate(items)})

    # Marvel series: migrate anything previously queued as plain VIP into the "after movies" set
    with SyncSessionLocal() as session:
        srows = session.execute(text(
            "SELECT id, title, original_title, year FROM series WHERE category IS DISTINCT FROM 'adult';"
        )).mappings().all()
    marvel_series = [r["id"] for r in srows if marvel.classify_series(dict(r)) is not None]
    for sid in marvel_series:
        if redis_sync.sismember("riva:vip_series_ids", str(sid)):
            redis_sync.srem("riva:vip_series_ids", str(sid))
        redis_sync.sadd("riva:marvel_series_ids", str(sid))
    return len(items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--movies", action="store_true")
    ap.add_argument("--series", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--publish-order", action="store_true", help="only refresh riva:marvel_rank and exit")
    ap.add_argument("--out", default="/tmp/marvel_seed_report.json")
    a = ap.parse_args()
    if a.publish_order:
        logger.info(f"Published release-order rank for {publish_marvel_order()} Marvel movies")
        return
    do_m = a.movies or not a.series
    do_s = a.series or not a.movies
    result: Dict[str, Any] = {}
    if do_m:
        result["movies"] = seed_movies(a.dry_run)
        logger.info(f"Movies: {result['movies']['stats']}")
        if not a.dry_run:
            logger.info(f"Published release-order rank for {publish_marvel_order()} Marvel movies")
    if do_s:
        result["series"] = seed_series(a.dry_run)
        logger.info(f"Series: {result['series']['stats']}")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    logger.info(f"Report written to {a.out}")


if __name__ == "__main__":
    main()

