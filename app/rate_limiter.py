import json
import time
from datetime import datetime, timezone
import redis.asyncio as aioredis
from fastapi import Header, HTTPException, status, Depends
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import APIKey, User
from app.security import hash_api_key

class InMemoryCache:
    def __init__(self):
        self._store = {}
        self._counters = {}

    async def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        val, exp = item
        if exp and time.time() > exp:
            self._store.pop(key, None)
            return None
        return val

    async def set(self, key: str, value: str, ex: int = None):
        exp = time.time() + ex if ex else None
        self._store[key] = (value, exp)

    async def incr(self, key: str):
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def expire(self, key: str, seconds: int):
        pass

    async def delete(self, key: str):
        self._store.pop(key, None)
        self._counters.pop(key, None)

_in_memory_cache = InMemoryCache()
_redis_available = None

# Global Redis client for rate limiting & cache
redis_client = aioredis.from_url(
    settings.REDIS_URL,
    encoding="utf-8",
    decode_responses=True,
    max_connections=50
)

async def get_redis():
    global _redis_available
    if _redis_available is None:
        try:
            await redis_client.ping()
            _redis_available = True
        except Exception:
            _redis_available = False
            
    if _redis_available:
        return redis_client
    return _in_memory_cache

class AuthenticatedAPIKey:
    def __init__(self, key_id: int, user_id: int, tier: str, daily_limit: int, rate_limit_per_minute: int):
        self.key_id = key_id
        self.user_id = user_id
        self.tier = tier
        self.daily_limit = daily_limit
        self.rate_limit_per_minute = rate_limit_per_minute

async def verify_api_key(
    x_api_key: str = Header(..., description="API Key for B2B Client Authentication", alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
) -> AuthenticatedAPIKey:
    """
    Validates API key and enforces tiered rate limits using Redis.
    Uses Redis cache to prevent database bottlenecks on every request.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Missing X-API-Key header"}
        )

    key_hash = hash_api_key(x_api_key)
    cache_key = f"auth:apikey:{key_hash}"
    cached_info = await redis.get(cache_key)

    if cached_info:
        key_data = json.loads(cached_info)
    else:
        # Cache miss: fetch from PostgreSQL
        stmt = select(APIKey).where(
            APIKey.key_hash == key_hash,
            APIKey.is_active == True
        )
        result = await db.execute(stmt)
        api_key_record = result.scalars().first()

        if not api_key_record:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "Invalid or revoked API Key"}
            )

        if api_key_record.expires_at and api_key_record.expires_at < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "API Key has expired"}
            )

        key_data = {
            "key_id": api_key_record.id,
            "user_id": api_key_record.user_id,
            "tier": api_key_record.tier,
            "daily_limit": api_key_record.daily_limit,
            "rate_limit_per_minute": api_key_record.rate_limit_per_minute,
        }
        # Cache authenticated key profile for 10 minutes
        await redis.set(cache_key, json.dumps(key_data), ex=600)

    auth_key = AuthenticatedAPIKey(**key_data)

    # --- 1. Enforce Per-Minute Burst Rate Limit ---
    current_minute = int(time.time() // 60)
    minute_key = f"ratelimit:burst:{auth_key.key_id}:{current_minute}"
    burst_count = await redis.incr(minute_key)
    if burst_count == 1:
        await redis.expire(minute_key, 120)

    if burst_count > auth_key.rate_limit_per_minute:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "Rate limit exceeded (per-minute burst limit)",
                "limit_per_minute": auth_key.rate_limit_per_minute,
                "tier": auth_key.tier
            },
            headers={"Retry-After": "60"}
        )

    # --- 2. Enforce Daily Quota Rate Limit ---
    if auth_key.daily_limit > 0:  # -1 indicates unlimited
        today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_key = f"ratelimit:daily:{auth_key.key_id}:{today_date}"
        daily_usage = await redis.incr(daily_key)
        if daily_usage == 1:
            await redis.expire(daily_key, 86400 * 2)  # Expire after 48h to prevent leak

        if daily_usage > auth_key.daily_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "Daily quota exceeded",
                    "daily_limit": auth_key.daily_limit,
                    "current_usage": daily_usage,
                    "tier": auth_key.tier,
                    "upgrade_url": "https://api.yourdomain.com/v1/billing/upgrade"
                }
            )

    return auth_key
