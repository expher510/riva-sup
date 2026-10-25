from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    ENVIRONMENT: str = "production"
    DATABASE_URL: str = "postgresql+asyncpg://riva_user:riva_secure_password@postgres:5432/riva_db"
    SYNC_DATABASE_URL: Optional[str] = "postgresql+psycopg2://riva_user:riva_secure_password@postgres:5432/riva_db"
    REDIS_URL: str = "redis://:redis_secure_password@redis:6379/0"
    CELERY_BROKER_URL: str = "redis://:redis_secure_password@redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://:redis_secure_password@redis:6379/2"

    TMDB_API_KEY: str = ""
    DOODSTREAM_API_KEY: str = ""
    STREAMTAPE_API_LOGIN: str = ""
    STREAMTAPE_API_KEY: str = ""

    # Telegram Master Vault Credentials
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHANNEL_ID: str = ""
    TELEGRAM_API_ID: Optional[str] = None
    TELEGRAM_API_HASH: Optional[str] = None
    TELEGRAM_LOCAL_API_URL: Optional[str] = None

    TRANSIT_TEMP_DIR: str = "/tmp/riva_transit"

    # Cache TTLs (seconds)
    CACHE_TTL_MOVIE_METADATA: int = 86400  # 24 hours
    CACHE_TTL_TRENDING: int = 3600         # 1 hour

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
