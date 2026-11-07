from pydantic_settings import BaseSettings
from typing import Optional
import os

class Settings(BaseSettings):
    ENVIRONMENT: str = "production"
    
    # Shared PostgreSQL database (riva_production)
    POSTGRES_USER: str = "riva_admin"
    POSTGRES_PASSWORD: str = "super_secret_db_password_change_in_production"
    POSTGRES_DB: str = "riva_production"
    POSTGRES_HOST: str = "127.0.0.1"
    POSTGRES_PORT: int = 5432
    DATABASE_URL: Optional[str] = None

    # TMDB API
    TMDB_API_KEY: str = "9acdd2e9bdd2c77d2134171ce08da1db"
    
    # Telegram Bots
    MAIN_BOT_TOKEN: str = "9110792514:AAENq1z9SHe8uaJcQkLDc23dKaMw0IddpsI"
    MAIN_BOT_USERNAME: str = "riva108_bot"
    DELIVERY_BOT_TOKEN: str = "9107119612:AAFAzgtzQE-tqWV8bGkAF9Ae_xxHVtXeuPc"
    DELIVERY_BOT_USERNAME: str = "Rivaa1_bot"
    
    # Channels
    REQUIRED_CHANNEL_ID: str = "-1004488480136" # or official news channel
    REQUIRED_CHANNEL_INVITE: str = "https://t.me/riva18_official"
    DELIVERY_VAULT_CHANNEL_ID: str = "-1004488480136"
    
    # Monetization
    VIP_STARS_PRICE: int = 100
    VIP_DURATION_DAYS: int = 30
    ADSGRAM_BLOCK_ID: str = "your_adsgram_block_id"

    # CORS
    CORS_ORIGINS: str = "*"

    @property
    def async_db_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

