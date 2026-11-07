from sqlalchemy import Column, Integer, BigInteger, String, Text, Float, Boolean, DateTime, Date, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base

class Movie(Base):
    __tablename__ = "movies"

    id = Column(Integer, primary_key=True, index=True)
    tmdb_id = Column(Integer, unique=True, index=True, nullable=True)
    category = Column(String(50), default="western")
    title = Column(String(255), index=True)
    original_title = Column(String(255), nullable=True)
    overview = Column(Text, nullable=True)
    year = Column(Integer, nullable=True)
    release_date = Column(Date, nullable=True)
    poster_path = Column(String(255), nullable=True)
    backdrop_path = Column(String(255), nullable=True)
    vote_average = Column(Float, default=0.0)
    vote_count = Column(Integer, default=0)
    popularity = Column(Float, default=0.0)
    genres = Column(JSON, nullable=True)
    duration = Column(Integer, nullable=True)
    source_url = Column(Text, nullable=True)
    views_count = Column(Integer, default=0)

    archives = relationship("TelegramArchive", back_populates="movie", lazy="selectin")

class Series(Base):
    __tablename__ = "series"

    id = Column(Integer, primary_key=True, index=True)
    tmdb_id = Column(Integer, unique=True, index=True, nullable=True)
    category = Column(String(50), default="western")
    title = Column(String(255), index=True)
    original_title = Column(String(255), nullable=True)
    overview = Column(Text, nullable=True)
    release_year = Column(Integer, nullable=True)
    poster_path = Column(String(255), nullable=True)
    backdrop_path = Column(String(255), nullable=True)
    vote_average = Column(Float, default=0.0)
    popularity = Column(Float, default=0.0)
    genres = Column(JSON, nullable=True)
    views_count = Column(Integer, default=0)

    seasons = relationship("Season", back_populates="series", order_by="Season.season_number", lazy="selectin")

class Season(Base):
    __tablename__ = "seasons"

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("series.id", ondelete="CASCADE"), index=True)
    season_number = Column(Integer, default=1)
    title = Column(String(255), nullable=True)
    overview = Column(Text, nullable=True)
    poster_path = Column(String(255), nullable=True)

    series = relationship("Series", back_populates="seasons")
    episodes = relationship("Episode", back_populates="season", order_by="Episode.episode_number", lazy="selectin")

class Episode(Base):
    __tablename__ = "episodes"

    id = Column(Integer, primary_key=True, index=True)
    season_id = Column(Integer, ForeignKey("seasons.id", ondelete="CASCADE"), index=True)
    episode_number = Column(Integer, default=1)
    title = Column(String(255), nullable=True)
    overview = Column(Text, nullable=True)
    duration = Column(Integer, nullable=True)

    season = relationship("Season", back_populates="episodes")
    archives = relationship("TelegramArchive", back_populates="episode", lazy="selectin")

class TelegramArchive(Base):
    __tablename__ = "telegram_archives"

    id = Column(Integer, primary_key=True, index=True)
    channel_id = Column(BigInteger, nullable=False)
    message_id = Column(Integer, nullable=False)
    file_id = Column(String(255), nullable=True)
    file_size_bytes = Column(BigInteger, default=0)
    movie_id = Column(Integer, ForeignKey("movies.id", ondelete="CASCADE"), nullable=True, index=True)
    episode_id = Column(Integer, ForeignKey("episodes.id", ondelete="CASCADE"), nullable=True, index=True)

    movie = relationship("Movie", back_populates="archives")
    episode = relationship("Episode", back_populates="archives")

# ─────────────────── MiniApp Tables ───────────────────

class MiniAppUser(Base):
    __tablename__ = "miniapp_users"

    telegram_id = Column(BigInteger, primary_key=True, index=True)
    username = Column(String(100), nullable=True)
    first_name = Column(String(150), nullable=True)
    is_vip = Column(Boolean, default=False)
    vip_expires_at = Column(DateTime(timezone=True), nullable=True)
    total_ads_watched = Column(Integer, default=0)
    last_active_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class MiniAppSubscription(Base):
    __tablename__ = "miniapp_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, ForeignKey("miniapp_users.telegram_id", ondelete="CASCADE"), index=True)
    stars_amount = Column(Integer, default=100)
    payment_method = Column(String(50), default="telegram_stars")
    charge_id = Column(String(255), nullable=True)
    status = Column(String(50), default="completed")
    starts_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class MiniAppRequest(Base):
    __tablename__ = "miniapp_requests"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    title = Column(String(255), nullable=False)
    type = Column(String(20), default="movie")
    notes = Column(Text, nullable=True)
    status = Column(String(50), default="pending")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

