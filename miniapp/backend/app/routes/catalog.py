from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, desc, func
from typing import List, Optional, Dict, Any
from app.database import get_db
from app.models import Movie, Series, Season, Episode, TelegramArchive

router = APIRouter(prefix="/catalog", tags=["Catalog"])

TMDB_IMG_BASE = "https://image.tmdb.org/t/p"

def _format_poster(path: Optional[str], size: str = "w500") -> Optional[str]:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"{TMDB_IMG_BASE}/{size}{path}"

@router.get("/featured")
async def get_featured(db: AsyncSession = Depends(get_db)):
    """Returns top featured hero items for the main carousel."""
    # Top 3 movies + Top 2 series with high popularity and backdrops
    q_movies = select(Movie).filter(Movie.backdrop_path.isnot(None), Movie.vote_average >= 7.0).order_by(desc(Movie.popularity)).limit(3)
    q_series = select(Series).filter(Series.backdrop_path.isnot(None), Series.vote_average >= 7.0).order_by(desc(Series.popularity)).limit(2)

    res_m = await db.execute(q_movies)
    res_s = await db.execute(q_series)

    featured = []
    for m in res_m.scalars().all():
        featured.append({
            "id": m.id,
            "type": "movie",
            "title": m.title,
            "original_title": m.original_title,
            "year": m.year,
            "rating": round(m.vote_average or 0, 1),
            "backdrop": _format_poster(m.backdrop_path, "original"),
            "poster": _format_poster(m.poster_path, "w500"),
            "overview": m.overview,
            "genres": m.genres or ["أكشن"],
            "badge": "فيلم"
        })

    for s in res_s.scalars().all():
        featured.append({
            "id": s.id,
            "type": "series",
            "title": s.title,
            "original_title": s.original_title,
            "year": s.release_year,
            "rating": round(s.vote_average or 0, 1),
            "backdrop": _format_poster(s.backdrop_path, "original"),
            "poster": _format_poster(s.poster_path, "w500"),
            "overview": s.overview,
            "genres": s.genres or ["دراما"],
            "badge": "مسلسل"
        })

    return {"featured": featured}

@router.get("/recent")
async def get_recent(db: AsyncSession = Depends(get_db), limit: int = 20):
    """Returns recently archived movies and series (المضاف حديثاً)."""
    # 1. Recent movies
    q_movies = (
        select(Movie)
        .join(TelegramArchive, TelegramArchive.movie_id == Movie.id)
        .order_by(desc(TelegramArchive.id))
        .limit(limit)
    )
    res_m = await db.execute(q_movies)
    seen_ids = set()
    items = []
    for m in res_m.scalars().all():
        if m.id in seen_ids:
            continue
        seen_ids.add(m.id)
        items.append({
            "id": m.id,
            "type": "movie",
            "title": m.title,
            "original_title": m.original_title,
            "year": m.year,
            "rating": round(m.vote_average or 0, 1),
            "poster": _format_poster(m.poster_path, "w500"),
            "badge": "فيلم",
            "badge_color": "red"
        })

    # 2. Recent series
    q_series = (
        select(Series)
        .join(Season, Season.series_id == Series.id)
        .join(Episode, Episode.season_id == Season.id)
        .join(TelegramArchive, TelegramArchive.episode_id == Episode.id)
        .order_by(desc(TelegramArchive.id))
        .limit(limit)
    )
    res_s = await db.execute(q_series)
    seen_s_ids = set()
    series_items = []
    for s in res_s.scalars().all():
        if s.id in seen_s_ids:
            continue
        seen_s_ids.add(s.id)
        series_items.append({
            "id": s.id,
            "type": "series",
            "title": s.title,
            "original_title": s.original_title,
            "year": s.release_year,
            "rating": round(s.vote_average or 0, 1),
            "poster": _format_poster(s.poster_path, "w500"),
            "badge": "مسلسل",
            "badge_color": "cyan"
        })

    return {
        "recent_movies": items[:15],
        "recent_series": series_items[:15]
    }

@router.get("/search")
async def search_catalog(q: str = Query(..., min_length=2), db: AsyncSession = Depends(get_db)):
    """Search across movies and series."""
    q_term = f"%{q.strip()}%"
    m_stmt = select(Movie).filter(or_(Movie.title.ilike(q_term), Movie.original_title.ilike(q_term))).limit(20)
    s_stmt = select(Series).filter(or_(Series.title.ilike(q_term), Series.original_title.ilike(q_term))).limit(20)

    res_m = await db.execute(m_stmt)
    res_s = await db.execute(s_stmt)

    results = []
    for m in res_m.scalars().all():
        results.append({
            "id": m.id,
            "type": "movie",
            "title": m.title,
            "year": m.year,
            "rating": round(m.vote_average or 0, 1),
            "poster": _format_poster(m.poster_path, "w500"),
            "badge": "فيلم"
        })
    for s in res_s.scalars().all():
        results.append({
            "id": s.id,
            "type": "series",
            "title": s.title,
            "year": s.release_year,
            "rating": round(s.vote_average or 0, 1),
            "poster": _format_poster(s.poster_path, "w500"),
            "badge": "مسلسل"
        })

    return {"results": results}

@router.get("/details/{media_type}/{item_id}")
async def get_details(media_type: str, item_id: int, db: AsyncSession = Depends(get_db)):
    """Returns full item metadata, seasons, episodes, and stream status."""
    if media_type == "movie":
        stmt = select(Movie).filter(Movie.id == item_id)
        res = await db.execute(stmt)
        m = res.scalar_one_or_none()
        if not m:
            raise HTTPException(status_code=404, detail="Movie not found")
        
        # Check archive availability
        arch_stmt = select(TelegramArchive).filter(TelegramArchive.movie_id == item_id).limit(1)
        arch_res = await db.execute(arch_stmt)
        has_video = arch_res.scalar_one_or_none() is not None

        return {
            "id": m.id,
            "type": "movie",
            "title": m.title,
            "original_title": m.original_title,
            "overview": m.overview or "لا يوجد ملخص متاح حالياً.",
            "year": m.year,
            "rating": round(m.vote_average or 0, 1),
            "poster": _format_poster(m.poster_path, "w500"),
            "backdrop": _format_poster(m.backdrop_path, "original"),
            "genres": m.genres or ["أكشن", "إثارة"],
            "country": "الولايات المتحدة الأمريكية",
            "duration": m.duration or 120,
            "views": m.views_count or 1250,
            "has_video": has_video
        }

    elif media_type == "series":
        stmt = select(Series).filter(Series.id == item_id)
        res = await db.execute(stmt)
        s = res.scalar_one_or_none()
        if not s:
            raise HTTPException(status_code=404, detail="Series not found")

        seasons_data = []
        for sea in s.seasons:
            episodes_data = []
            for ep in sea.episodes:
                has_ep_video = len(ep.archives) > 0
                episodes_data.append({
                    "id": ep.id,
                    "episode_number": ep.episode_number,
                    "title": ep.title or f"الحلقة {ep.episode_number}",
                    "has_video": has_ep_video
                })
            seasons_data.append({
                "id": sea.id,
                "season_number": sea.season_number,
                "title": sea.title or f"الموسم {sea.season_number}",
                "episodes": episodes_data
            })

        return {
            "id": s.id,
            "type": "series",
            "title": s.title,
            "original_title": s.original_title,
            "overview": s.overview or "لا يوجد ملخص متاح حالياً.",
            "year": s.release_year,
            "rating": round(s.vote_average or 0, 1),
            "poster": _format_poster(s.poster_path, "w500"),
            "backdrop": _format_poster(s.backdrop_path, "original"),
            "genres": s.genres or ["دراما", "تشويق"],
            "country": "الولايات المتحدة الأمريكية",
            "seasons_count": len(seasons_data),
            "views": s.views_count or 3840,
            "seasons": seasons_data
        }

    raise HTTPException(status_code=400, detail="Invalid media type")

