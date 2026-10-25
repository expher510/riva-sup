from pydantic import BaseModel, Field, HttpUrl
from typing import List, Optional, Dict, Any
from datetime import datetime

class StreamItem(BaseModel):
    provider: str = Field(..., example="doodstream")
    quality: str = Field(..., example="1080p")
    embed_url: str = Field(..., example="https://doodstream.com/e/example123")
    direct_url: Optional[str] = Field(None, example="https://doodstream.com/d/example123")
    status: str = Field("active", example="active")

class MovieMetadata(BaseModel):
    tmdb_id: Optional[int] = Field(None, example=550)
    imdb_id: Optional[str] = Field(None, example="tt0137523")
    title: str = Field(..., example="Fight Club")
    original_title: Optional[str] = Field(None, example="Fight Club")
    overview: Optional[str] = Field(None, example="An insomniac office worker...")
    year: Optional[int] = Field(None, example=1999)
    poster: Optional[str] = Field(None, example="https://image.tmdb.org/t/p/w500/bptfVGEQuv6vDTIMVCHjJ9Dz8PX.jpg")
    backdrop: Optional[str] = Field(None, example="https://image.tmdb.org/t/p/original/hZkgoQYus5vegHoetLkCJzb17zJ.jpg")
    rating: float = Field(0.0, example=8.4)
    vote_count: int = Field(0, example=26000)
    genres: List[str] = Field(default_factory=list, example=["Drama"])
    runtime: Optional[int] = Field(None, example=139)

class MovieDetailResponse(BaseModel):
    success: bool = True
    metadata: MovieMetadata
    streams: List[StreamItem]

class TrendingMovieItem(BaseModel):
    tmdb_id: Optional[int]
    title: str
    year: Optional[int]
    poster: Optional[str]
    rating: float
    popularity: float
    has_streams: bool

class TrendingResponse(BaseModel):
    success: bool = True
    page: int = 1
    total_results: int
    results: List[TrendingMovieItem]

class EpisodeItem(BaseModel):
    id: int
    episode_number: int
    title: str
    source_url: str
    thumbnail: Optional[str] = None
    streams_count: int = 0

class SeasonItem(BaseModel):
    id: int
    season_number: int
    title: Optional[str] = None
    episodes: List[EpisodeItem] = []

class SeriesItem(BaseModel):
    id: int
    title: str
    category: str
    year: Optional[int] = None
    poster: Optional[str] = None
    rating: float = 0.0
    status: str = "ongoing"
    seasons_count: int = 0
    total_episodes: int = 0

class SeriesDetailResponse(BaseModel):
    success: bool = True
    series: SeriesItem
    seasons: List[SeasonItem]

class EpisodeDetailResponse(BaseModel):
    success: bool = True
    series_title: str
    season_number: int
    episode_number: int
    title: str
    streams: List[StreamItem]

class CheckpointItem(BaseModel):
    category_slug: str
    category_name: str
    current_page: int
    total_pages: int
    items_count: int
    status: str
    updated_at: Optional[datetime] = None

class APIKeyCreateRequest(BaseModel):
    email: str
    tier: str = "free"
    key_name: str = "Production API Key"

class APIKeyResponse(BaseModel):
    api_key: str
    tier: str
    daily_limit: int
    rate_limit_per_minute: int
    created_at: datetime
