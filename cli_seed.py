"""
Administrative CLI script to bootstrap an admin user, generate an initial API key,
or trigger a test ingestion pipeline task.
"""
import asyncio
import sys
from sqlalchemy.future import select
from app.database import AsyncSessionLocal, engine, Base
from app.models import User, APIKey
from app.security import generate_api_key

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def create_key(email: str, tier: str = "enterprise"):
    await init_db()
    async with AsyncSessionLocal() as session:
        # Check or create user
        res = await session.execute(select(User).where(User.email == email))
        user = res.scalars().first()
        if not user:
            user = User(email=email)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        raw_key, prefix, key_hash = generate_api_key()
        limits = {
            "free": (100, 20),
            "pro": (50000, 120),
            "enterprise": (-1, 600)
        }
        daily, burst = limits.get(tier, (100, 20))

        api_key = APIKey(
            user_id=user.id,
            key_prefix=prefix,
            key_hash=key_hash,
            name=f"Initial {tier.title()} Key",
            tier=tier,
            daily_limit=daily,
            rate_limit_per_minute=burst
        )
        session.add(api_key)
        await session.commit()

        print("=" * 60)
        print("API KEY CREATED SUCCESSFULLY")
        print("=" * 60)
        print(f"Client Email    : {email}")
        print(f"Tier            : {tier}")
        print(f"Daily Limit     : {'Unlimited' if daily == -1 else daily}")
        print(f"Burst Limit/min : {burst}")
        print(f"X-API-Key       : {raw_key}")
        print("=" * 60)
        print("Keep this key safe! Plaintext is never stored in the database.")

if __name__ == "__main__":
    email_arg = sys.argv[1] if len(sys.argv) > 1 else "client@example.com"
    tier_arg = sys.argv[2] if len(sys.argv) > 2 else "enterprise"
    asyncio.run(create_key(email_arg, tier_arg))
