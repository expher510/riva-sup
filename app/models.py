import datetime
from typing import List, Optional
from sqlalchemy import (
    Column, Integer, String, Float, Text, Boolean, DateTime,
    ForeignKey, UniqueConstraint, Index, JSON
)
from sqlalchemy.orm import relationship, Mapped, mapped_column
from app.database import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    api_keys: Mapped[List["APIKey"]] = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    
    key_prefix: Mapped[str] = mapped_column(String(12), index=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    
    name: Mapped[str] = mapped_column(String(100), default="Default Key")
    tier: Mapped[str] = mapped_column(String(20), default="free")  # "free", "pro", "enterprise"
    daily_limit: Mapped[int] = mapped_column(Integer, default=100) # Requests per day
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=20) # Max burst per minute
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )

    user: Mapped["User"] = relationship("User", back_populates="api_keys")


class Movie(Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tmdb_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True, nullable=True)
    imdb_id: Mapped[Optional[str]] = mapped_column(String(20), index=True, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(500), unique=True, index=True, nullable=True)
    category: Mapped[str] = mapped_column(String(50), default="western", index=True)
    
    title: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    original_title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    overview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    release_date: Mapped[Optional[datetime.date]] = mapped_column(DateTime, nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    
    poster_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    backdrop_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    vote_average: Mapped[float] = mapped_column(Float, default=0.0)
    vote_count: Mapped[int] = mapped_column(Integer, default=0)
    popularity: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    runtime: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    genres: Mapped[Optional[dict]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    streaming_links: Mapped[List["StreamingLink"]] = relationship(
        "StreamingLink", back_populates="movie", cascade="all, delete-orphan", lazy="selectin"
    )
    telegram_archives: Mapped[List["TelegramArchive"]] = relationship(
        "TelegramArchive", back_populates="movie", cascade="all, delete-orphan", lazy="selectin"
    )


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tmdb_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(500), unique=True, index=True, nullable=True)
    slug: Mapped[Optional[str]] = mapped_column(String(300), index=True, nullable=True)
    category: Mapped[str] = mapped_column(String(50), default="kdrama", index=True) # kdrama, anime, asian, turkish, western, arabic
    
    title: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    original_title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    overview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    
    poster_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    backdrop_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    vote_average: Mapped[float] = mapped_column(Float, default=0.0)
    vote_count: Mapped[int] = mapped_column(Integer, default=0)
    popularity: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    genres: Mapped[Optional[dict]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="ongoing") # ongoing, completed

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    seasons: Mapped[List["Season"]] = relationship(
        "Season", back_populates="series", cascade="all, delete-orphan", lazy="selectin"
    )


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    series_id: Mapped[int] = mapped_column(Integer, ForeignKey("series.id", ondelete="CASCADE"), index=True)
    season_number: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    series: Mapped["Series"] = relationship("Series", back_populates="seasons")
    episodes: Mapped[List["Episode"]] = relationship(
        "Episode", back_populates="season", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("series_id", "season_number", name="uq_series_season"),
    )


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    season_id: Mapped[int] = mapped_column(Integer, ForeignKey("seasons.id", ondelete="CASCADE"), index=True)
    episode_number: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), unique=True, index=True, nullable=False)
    overview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thumbnail: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_filler: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    season: Mapped["Season"] = relationship("Season", back_populates="episodes")
    streaming_links: Mapped[List["StreamingLink"]] = relationship(
        "StreamingLink", back_populates="episode", cascade="all, delete-orphan", lazy="selectin"
    )
    telegram_archives: Mapped[List["TelegramArchive"]] = relationship(
        "TelegramArchive", back_populates="episode", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("season_id", "episode_number", name="uq_season_episode"),
    )


class StreamingLink(Base):
    __tablename__ = "streaming_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    movie_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("movies.id", ondelete="CASCADE"), nullable=True, index=True)
    episode_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("episodes.id", ondelete="CASCADE"), nullable=True, index=True)
    
    provider_name: Mapped[str] = mapped_column(String(50), index=True, nullable=False) # e.g. "doodstream", "streamtape", "fastvid", "direct-hls"
    embed_url: Mapped[str] = mapped_column(Text, nullable=False)
    direct_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quality: Mapped[str] = mapped_column(String(20), default="1080p") # "1080p", "720p", "480p"
    file_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True) # "active", "dead", "pending"
    
    last_checked_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    movie: Mapped[Optional["Movie"]] = relationship("Movie", back_populates="streaming_links")
    episode: Mapped[Optional["Episode"]] = relationship("Episode", back_populates="streaming_links")


class ScraperCheckpoint(Base):
    __tablename__ = "scraper_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    category_slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    category_name: Mapped[str] = mapped_column(String(100), nullable=False)
    current_page: Mapped[int] = mapped_column(Integer, default=1)
    total_pages: Mapped[int] = mapped_column(Integer, default=1)
    last_scraped_title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    last_scraped_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle") # idle, running, completed, error
    items_count: Mapped[int] = mapped_column(Integer, default=0)
    
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )


class TelegramArchive(Base):
    __tablename__ = "telegram_archives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    movie_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("movies.id", ondelete="CASCADE"), nullable=True, index=True)
    episode_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("episodes.id", ondelete="CASCADE"), nullable=True, index=True)

    telegram_file_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    telegram_file_unique_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_id: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    movie: Mapped[Optional["Movie"]] = relationship("Movie", back_populates="telegram_archives")
    episode: Mapped[Optional["Episode"]] = relationship("Episode", back_populates="telegram_archives")
