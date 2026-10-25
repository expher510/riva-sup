import json
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
import httpx
from pydantic import BaseModel
import redis.asyncio as aioredis
from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
import os
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db, engine, Base
from app.models import Movie, StreamingLink, User, APIKey, Series, Season, Episode, ScraperCheckpoint
from app.schemas import (
    MovieDetailResponse, MovieMetadata, StreamItem,
    TrendingResponse, TrendingMovieItem,
    APIKeyCreateRequest, APIKeyResponse,
    SeriesItem, SeriesDetailResponse, EpisodeItem, SeasonItem, EpisodeDetailResponse, CheckpointItem
)
from app.security import generate_api_key, hash_api_key
from app.rate_limiter import verify_api_key, AuthenticatedAPIKey, get_redis

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables on startup if they don't exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Cleanup connections
    await engine.dispose()

app = FastAPI(
    title="Riva VOD Engine API",
    version="1.0.0",
    description="Monetized B2B Video-on-Demand (VOD) Metadata & Stream Link Delivery API",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"

@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "healthy", "service": "riva-api"}

@app.get(
    "/v1/movies/{movie_id}",
    response_model=MovieDetailResponse,
    tags=["Movies"],
    summary="Get Movie Metadata and Active Streaming Embed Links"
)
async def get_movie_details(
    movie_id: int,
    auth_key: AuthenticatedAPIKey = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    """
    Returns complete TMDB metadata and active cyberlocker streaming embed links.
    Caches aggregated output in Redis to deliver optimal throughput.
    """
    cache_key = f"cache:movie_v1:{movie_id}"
    cached_payload = await redis.get(cache_key)
    if cached_payload:
        return json.loads(cached_payload)

    # 1. Fetch Movie & Streaming Links from PostgreSQL
    stmt = (
        select(Movie)
        .options(selectinload(Movie.streaming_links))
        .where(Movie.tmdb_id == movie_id)
    )
    result = await db.execute(stmt)
    movie = result.scalars().first()

    # 2. If movie metadata is not yet populated locally, fetch from TMDB & cache
    if not movie:
        tmdb_url = f"{TMDB_BASE_URL}/movie/{movie_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(tmdb_url, params={"api_key": settings.TMDB_API_KEY})
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="Movie not found on TMDB")
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to fetch metadata from upstream provider")
            tmdb_data = resp.json()

        # Build movie record
        release_date = tmdb_data.get("release_date")
        year = int(release_date.split("-")[0]) if release_date and len(release_date) >= 4 else None
        genres = [g["name"] for g in tmdb_data.get("genres", [])]

        movie = Movie(
            tmdb_id=movie_id,
            imdb_id=tmdb_data.get("imdb_id"),
            title=tmdb_data.get("title", ""),
            original_title=tmdb_data.get("original_title"),
            overview=tmdb_data.get("overview"),
            year=year,
            poster_path=tmdb_data.get("poster_path"),
            backdrop_path=tmdb_data.get("backdrop_path"),
            vote_average=float(tmdb_data.get("vote_average", 0.0)),
            vote_count=int(tmdb_data.get("vote_count", 0)),
            popularity=float(tmdb_data.get("popularity", 0.0)),
            runtime=tmdb_data.get("runtime"),
            genres=genres
        )
        db.add(movie)
        await db.commit()
        await db.refresh(movie)

    # 3. Format Stream links
    stream_items = [
        StreamItem(
            provider=link.provider_name,
            quality=link.quality,
            embed_url=link.embed_url,
            direct_url=link.direct_url,
            status=link.status
        )
        for link in movie.streaming_links
        if link.status == "active"
    ]

    response_data = MovieDetailResponse(
        success=True,
        metadata=MovieMetadata(
            tmdb_id=movie.tmdb_id,
            imdb_id=movie.imdb_id,
            title=movie.title,
            original_title=movie.original_title,
            overview=movie.overview,
            year=movie.year,
            poster=f"{TMDB_IMAGE_BASE}/w500{movie.poster_path}" if movie.poster_path else None,
            backdrop=f"{TMDB_IMAGE_BASE}/original{movie.backdrop_path}" if movie.backdrop_path else None,
            rating=round(movie.vote_average, 1),
            vote_count=movie.vote_count,
            genres=movie.genres or [],
            runtime=movie.runtime
        ),
        streams=stream_items
    )

    # 4. Cache response in Redis
    await redis.set(cache_key, response_data.model_dump_json(), ex=settings.CACHE_TTL_MOVIE_METADATA)

    return response_data


@app.get(
    "/v1/movies/trending",
    response_model=TrendingResponse,
    tags=["Movies"],
    summary="Get Daily Trending Movies"
)
async def get_trending_movies(
    page: int = Query(1, ge=1, le=10),
    auth_key: AuthenticatedAPIKey = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    """
    Returns trending movies list cached from TMDB and cross-referenced with available streams.
    """
    cache_key = f"cache:trending:movies:page:{page}"
    cached_payload = await redis.get(cache_key)
    if cached_payload:
        return json.loads(cached_payload)

    # Fetch from TMDB Trending API
    tmdb_url = f"{TMDB_BASE_URL}/trending/movie/day"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(tmdb_url, params={"api_key": settings.TMDB_API_KEY, "page": page})
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch trending movies from TMDB")
        data = resp.json()

    tmdb_movies = data.get("results", [])
    tmdb_ids = [m["id"] for m in tmdb_movies]

    # Query local DB for existing streaming links for these movies
    stmt = (
        select(Movie.tmdb_id)
        .join(StreamingLink)
        .where(Movie.tmdb_id.in_(tmdb_ids), StreamingLink.status == "active")
        .distinct()
    )
    result = await db.execute(stmt)
    available_tmdb_ids = set(result.scalars().all())

    trending_results = []
    for m in tmdb_movies:
        release_date = m.get("release_date")
        year = int(release_date.split("-")[0]) if release_date and len(release_date) >= 4 else None
        poster_path = m.get("poster_path")

        trending_results.append(
            TrendingMovieItem(
                tmdb_id=m["id"],
                title=m.get("title", ""),
                year=year,
                poster=f"{TMDB_IMAGE_BASE}/w500{poster_path}" if poster_path else None,
                rating=round(float(m.get("vote_average", 0.0)), 1),
                popularity=float(m.get("popularity", 0.0)),
                has_streams=(m["id"] in available_tmdb_ids)
            )
        )

    response = TrendingResponse(
        success=True,
        page=page,
        total_results=data.get("total_results", len(trending_results)),
        results=trending_results
    )

    await redis.set(cache_key, response.model_dump_json(), ex=settings.CACHE_TTL_TRENDING)
    return response


@app.post(
    "/v1/admin/api-keys",
    response_model=APIKeyResponse,
    tags=["Admin"],
    summary="Provision an API Key for B2B Client"
)
async def create_api_key_for_client(
    payload: APIKeyCreateRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Provisions a new API key with configured tier and rate limits.
    """
    # 1. Fetch or create user
    stmt = select(User).where(User.email == payload.email)
    res = await db.execute(stmt)
    user = res.scalars().first()

    if not user:
        user = User(email=payload.email)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    # 2. Configure limits based on tier
    tier = payload.tier.lower()
    limits = {
        "free": {"daily": 100, "burst": 20},
        "pro": {"daily": 50000, "burst": 120},
        "enterprise": {"daily": -1, "burst": 600}
    }
    tier_config = limits.get(tier, limits["free"])

    raw_key, prefix, key_hash = generate_api_key()

    new_api_key = APIKey(
        user_id=user.id,
        key_prefix=prefix,
        key_hash=key_hash,
        name=payload.key_name,
        tier=tier,
        daily_limit=tier_config["daily"],
        rate_limit_per_minute=tier_config["burst"]
    )
    db.add(new_api_key)
    await db.commit()
    await db.refresh(new_api_key)

    return APIKeyResponse(
        api_key=raw_key,
        tier=new_api_key.tier,
        daily_limit=new_api_key.daily_limit,
        rate_limit_per_minute=new_api_key.rate_limit_per_minute,
        created_at=new_api_key.created_at
    )

@app.get(
    "/v1/catalog",
    tags=["Catalog"],
    summary="List available movies in catalog with streaming links status"
)
async def get_catalog(
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Movie).options(selectinload(Movie.streaming_links)).order_by(Movie.popularity.desc())
    if search:
        stmt = stmt.where(Movie.title.ilike(f"%{search}%"))
    res = await db.execute(stmt)
    movies = res.scalars().all()

    catalog_data = []
    for m in movies:
        active_streams = [s for s in m.streaming_links if s.status == "active"]
        catalog_data.append({
            "id": m.id,
            "tmdb_id": m.tmdb_id,
            "title": m.title,
            "year": m.year,
            "poster": f"{TMDB_IMAGE_BASE}/w500{m.poster_path}" if m.poster_path else None,
            "backdrop": f"{TMDB_IMAGE_BASE}/original{m.backdrop_path}" if m.backdrop_path else None,
            "rating": round(m.vote_average, 1),
            "vote_count": m.vote_count,
            "overview": m.overview,
            "genres": m.genres or [],
            "streams_count": len(active_streams),
            "providers": list({s.provider_name for s in active_streams}),
            "streams": [
                {
                    "provider": s.provider_name,
                    "quality": s.quality,
                    "embed_url": s.embed_url,
                    "direct_url": s.direct_url,
                    "file_code": s.file_code
                }
                for s in active_streams
            ]
        })
    return {"success": True, "count": len(catalog_data), "movies": catalog_data}


@app.get(
    "/v1/series",
    tags=["Series"],
    summary="List Series with category filter, search, and pagination"
)
async def list_series(
    category: Optional[str] = Query(None, description="Category filter (kdrama, anime, asian, turkish, western, arabic)"),
    search: Optional[str] = Query(None, description="Search by title"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Series).options(selectinload(Series.seasons).selectinload(Season.episodes)).order_by(Series.created_at.desc())
    if category:
        stmt = stmt.where(Series.category == category)
    if search:
        stmt = stmt.where(Series.title.ilike(f"%{search}%"))
        
    offset = (page - 1) * limit
    stmt = stmt.offset(offset).limit(limit)
    res = await db.execute(stmt)
    series_list = res.scalars().all()
    
    results = []
    for s in series_list:
        total_eps = sum(len(season.episodes) for season in s.seasons)
        results.append({
            "id": s.id,
            "title": s.title,
            "category": s.category,
            "year": s.year,
            "poster": s.poster_path,
            "rating": s.vote_average,
            "status": s.status,
            "seasons_count": len(s.seasons),
            "total_episodes": total_eps
        })
        
    return {"success": True, "page": page, "count": len(results), "series": results}


@app.get(
    "/v1/series/{series_id}",
    tags=["Series"],
    summary="Get Series full details with seasons and episodes"
)
async def get_series_detail(
    series_id: int,
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(Series)
        .options(
            selectinload(Series.seasons).selectinload(Season.episodes).selectinload(Episode.streaming_links)
        )
        .where(Series.id == series_id)
    )
    res = await db.execute(stmt)
    s = res.scalars().first()
    if not s:
        raise HTTPException(status_code=404, detail="Series not found")
        
    seasons_data = []
    total_eps = 0
    for season in sorted(s.seasons, key=lambda x: x.season_number):
        eps_data = []
        for ep in sorted(season.episodes, key=lambda x: x.episode_number):
            total_eps += 1
            eps_data.append({
                "id": ep.id,
                "episode_number": ep.episode_number,
                "title": ep.title,
                "source_url": ep.source_url,
                "thumbnail": ep.thumbnail,
                "streams_count": len([link for link in ep.streaming_links if link.status == "active"])
            })
        seasons_data.append({
            "id": season.id,
            "season_number": season.season_number,
            "title": season.title,
            "episodes": eps_data
        })
        
    return {
        "success": True,
        "series": {
            "id": s.id,
            "title": s.title,
            "category": s.category,
            "year": s.year,
            "overview": s.overview,
            "poster": s.poster_path,
            "backdrop": s.backdrop_path,
            "rating": s.vote_average,
            "status": s.status,
            "seasons_count": len(s.seasons),
            "total_episodes": total_eps
        },
        "seasons": seasons_data
    }


@app.get(
    "/v1/episodes/{episode_id}",
    tags=["Series"],
    summary="Get Episode stream links and details"
)
async def get_episode_detail(
    episode_id: int,
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(Episode)
        .options(
            selectinload(Episode.season).selectinload(Season.series),
            selectinload(Episode.streaming_links)
        )
        .where(Episode.id == episode_id)
    )
    res = await db.execute(stmt)
    ep = res.scalars().first()
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
        
    active_streams = [s for s in ep.streaming_links if s.status == "active"]
    return {
        "success": True,
        "series_title": ep.season.series.title if ep.season and ep.season.series else "",
        "season_number": ep.season.season_number if ep.season else 1,
        "episode_number": ep.episode_number,
        "title": ep.title,
        "streams": [
            {
                "provider": s.provider_name,
                "quality": s.quality,
                "embed_url": s.embed_url,
                "direct_url": s.direct_url,
                "status": s.status
            }
            for s in active_streams
        ]
    }


@app.get(
    "/v1/crawler/status",
    tags=["Crawler"],
    summary="Get Scraper Progress and Checkpoints for all categories"
)
async def get_crawler_status(db: AsyncSession = Depends(get_db)):
    stmt = select(ScraperCheckpoint).order_by(ScraperCheckpoint.items_count.desc())
    res = await db.execute(stmt)
    checkpoints = res.scalars().all()
    return {
        "success": True,
        "count": len(checkpoints),
        "checkpoints": [
            {
                "category_slug": cp.category_slug,
                "category_name": cp.category_name,
                "current_page": cp.current_page,
                "total_pages": cp.total_pages,
                "items_count": cp.items_count,
                "status": cp.status,
                "updated_at": cp.updated_at
            }
            for cp in checkpoints
        ]
    }


class IngestStreamItem(BaseModel):
    provider_name: str
    embed_url: str
    direct_url: Optional[str] = None
    quality: str = "1080p"


class IngestEpisodeRequest(BaseModel):
    series_title: str
    season_number: int = 1
    episode_number: int = 1
    episode_title: Optional[str] = None
    source_url: str
    category: str = "western"
    year: Optional[int] = None
    streams: List[IngestStreamItem] = []
    secret_key: Optional[str] = None


class IngestMovieRequest(BaseModel):
    title: str
    source_url: str
    category: str = "western"
    year: Optional[int] = None
    streams: List[IngestStreamItem] = []
    secret_key: Optional[str] = None


@app.post("/v1/ingest/episode", tags=["Ingest"])
async def ingest_episode(req: IngestEpisodeRequest, db: AsyncSession = Depends(get_db)):
    if req.secret_key != "riva_ingest_token_2026":
        raise HTTPException(status_code=403, detail="Invalid ingest secret key")

    # 1. Find or create Series
    stmt_s = select(Series).where(Series.title == req.series_title)
    series = (await db.execute(stmt_s)).scalars().first()
    if not series:
        series = Series(
            title=req.series_title,
            category=req.category,
            year=req.year,
            status="ongoing"
        )
        db.add(series)
        await db.commit()
        await db.refresh(series)

    # 2. Find or create Season
    stmt_season = select(Season).where(
        Season.series_id == series.id,
        Season.season_number == req.season_number
    )
    season = (await db.execute(stmt_season)).scalars().first()
    if not season:
        season = Season(
            series_id=series.id,
            season_number=req.season_number,
            title=f"الموسم {req.season_number}"
        )
        db.add(season)
        await db.commit()
        await db.refresh(season)

    # 3. Find or create Episode
    stmt_ep = select(Episode).where(Episode.source_url == req.source_url)
    episode = (await db.execute(stmt_ep)).scalars().first()
    if not episode:
        episode = Episode(
            season_id=season.id,
            episode_number=req.episode_number,
            title=req.episode_title or f"الحلقة {req.episode_number}",
            source_url=req.source_url
        )
        db.add(episode)
        await db.commit()
        await db.refresh(episode)

    # 4. Add streams
    for s in req.streams:
        stmt_st = select(StreamingLink).where(
            StreamingLink.episode_id == episode.id,
            StreamingLink.provider_name == s.provider_name
        )
        existing = (await db.execute(stmt_st)).scalars().first()
        if not existing:
            link = StreamingLink(
                episode_id=episode.id,
                provider_name=s.provider_name,
                embed_url=s.embed_url,
                direct_url=s.direct_url,
                quality=s.quality,
                status="active"
            )
            db.add(link)
    await db.commit()

    # 5. Dispatch transit pipeline if direct stream is available
    dispatched = False
    direct_stream = next((s.direct_url for s in req.streams if s.direct_url), None)
    if direct_stream:
        try:
            from worker.pipeline import process_episode_transit_pipeline
            process_episode_transit_pipeline.delay(episode.id, direct_stream, quality="1080p")
            dispatched = True
        except Exception:
            pass

    return {
        "success": True,
        "episode_id": episode.id,
        "dispatched_to_worker": dispatched
    }


@app.post("/v1/ingest/movie", tags=["Ingest"])
async def ingest_movie(req: IngestMovieRequest, db: AsyncSession = Depends(get_db)):
    if req.secret_key != "riva_ingest_token_2026":
        raise HTTPException(status_code=403, detail="Invalid ingest secret key")

    stmt_m = select(Movie).where(Movie.source_url == req.source_url)
    movie = (await db.execute(stmt_m)).scalars().first()
    if not movie:
        movie = Movie(
            title=req.title,
            source_url=req.source_url,
            category=req.category,
            year=req.year
        )
        db.add(movie)
        await db.commit()
        await db.refresh(movie)

    for s in req.streams:
        stmt_st = select(StreamingLink).where(
            StreamingLink.movie_id == movie.id,
            StreamingLink.provider_name == s.provider_name
        )
        existing = (await db.execute(stmt_st)).scalars().first()
        if not existing:
            link = StreamingLink(
                movie_id=movie.id,
                provider_name=s.provider_name,
                embed_url=s.embed_url,
                direct_url=s.direct_url,
                quality=s.quality,
                status="active"
            )
            db.add(link)
    await db.commit()

    dispatched = False
    direct_stream = next((s.direct_url for s in req.streams if s.direct_url), None)
    if direct_stream:
        try:
            from worker.pipeline import process_video_transit_pipeline
            process_video_transit_pipeline.delay(movie.id, direct_stream, quality="1080p")
            dispatched = True
        except Exception:
            pass

    return {
        "success": True,
        "movie_id": movie.id,
        "dispatched_to_worker": dispatched
    }


# Mount static folder for frontend UI
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", response_class=FileResponse, include_in_schema=False)
async def serve_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>Riva VOD Engine API is Running.</h1><p>Visit <a href='/docs'>/docs</a></p>")

