"""
Seed Marvel Cinematic Universe (MCU) movies into Riva VOD database.
Fetches official metadata from TMDB and populates active Cyberlocker streaming links.
"""
import asyncio
import httpx
from sqlalchemy.future import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.config import settings
from app.database import AsyncSessionLocal, engine, Base
from app.models import Movie, StreamingLink, User, APIKey
from app.security import hash_api_key

MARVEL_MOVIES = [
    {"tmdb_id": 299536, "title": "Avengers: Infinity War", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 299534, "title": "Avengers: Endgame", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 24428, "title": "The Avengers", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 1726, "title": "Iron Man", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 634649, "title": "Spider-Man: No Way Home", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 271110, "title": "Captain America: Civil War", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 284053, "title": "Thor: Ragnarok", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 118340, "title": "Guardians of the Galaxy", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 284054, "title": "Black Panther", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
    {"tmdb_id": 284052, "title": "Doctor Strange", "dood_code": "hig9h4mf69bj", "tape_code": "NOoMpESvr00"},
]

async def seed():
    print(">>> Initializing database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        # 1. Create or get test user & API key
        user_res = await session.execute(select(User).where(User.email == "demo@client.com"))
        user = user_res.scalars().first()
        if not user:
            user = User(email="demo@client.com")
            session.add(user)
            await session.commit()
            await session.refresh(user)

        demo_raw_key = "riva_live_marvel_demo_enterprise_key"
        key_hash = hash_api_key(demo_raw_key)

        key_res = await session.execute(select(APIKey).where(APIKey.key_hash == key_hash))
        if not key_res.scalars().first():
            api_key = APIKey(
                user_id=user.id,
                key_prefix=demo_raw_key[:12],
                key_hash=key_hash,
                name="Marvel Demo Enterprise Key",
                tier="enterprise",
                daily_limit=-1,
                rate_limit_per_minute=600
            )
            session.add(api_key)
            await session.commit()
            print(f">>> Provisioned Demo API Key: {demo_raw_key}")

        # 2. Fetch and seed Marvel movies from TMDB
        print(f">>> Fetching official metadata from TMDB for {len(MARVEL_MOVIES)} Marvel movies...")
        async with httpx.AsyncClient(timeout=15.0) as client:
            for item in MARVEL_MOVIES:
                tmdb_id = item["tmdb_id"]
                resp = await client.get(
                    f"https://api.themoviedb.org/3/movie/{tmdb_id}",
                    params={"api_key": settings.TMDB_API_KEY}
                )
                if resp.status_code != 200:
                    print(f"Failed to fetch TMDB {tmdb_id}: {resp.status_code}")
                    continue

                tmdb_data = resp.json()
                release_date = tmdb_data.get("release_date")
                year = int(release_date.split("-")[0]) if release_date else None
                genres = [g["name"] for g in tmdb_data.get("genres", [])]

                # Check if movie already exists
                m_res = await session.execute(select(Movie).where(Movie.tmdb_id == tmdb_id))
                movie = m_res.scalars().first()

                if not movie:
                    movie = Movie(
                        tmdb_id=tmdb_id,
                        imdb_id=tmdb_data.get("imdb_id"),
                        title=tmdb_data.get("title"),
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
                    session.add(movie)
                    await session.commit()
                    await session.refresh(movie)
                    print(f"  + Added Movie: {movie.title} ({movie.year})")
                else:
                    print(f"  * Existing Movie: {movie.title} ({movie.year})")

                # Fetch official HD trailer/stream from TMDB
                video_res = await client.get(
                    f"https://api.themoviedb.org/3/movie/{tmdb_id}/videos",
                    params={"api_key": settings.TMDB_API_KEY}
                )
                trailer_key = None
                if video_res.status_code == 200:
                    trailers = [v for v in video_res.json().get("results", []) if v.get("site") == "YouTube"]
                    if trailers:
                        trailer_key = trailers[0].get("key")

                # Build Streaming links
                streams = []
                if trailer_key:
                    streams.append({
                        "provider": "hd-trailer",
                        "quality": "1080p",
                        "embed_url": f"https://www.youtube-nocookie.com/embed/{trailer_key}?autoplay=1&rel=0",
                        "direct_url": f"https://www.youtube.com/watch?v={trailer_key}",
                        "file_code": trailer_key
                    })

                streams.extend([
                    {
                        "provider": "doodstream",
                        "quality": "1080p",
                        "embed_url": f"https://doodstream.com/e/{item['dood_code']}",
                        "direct_url": f"https://doodstream.com/d/{item['dood_code']}",
                        "file_code": item["dood_code"]
                    },
                    {
                        "provider": "streamtape",
                        "quality": "1080p",
                        "embed_url": f"https://streamtape.com/e/{item['tape_code']}",
                        "direct_url": f"https://streamtape.com/v/{item['tape_code']}",
                        "file_code": item["tape_code"]
                    }
                ])

                for s in streams:
                    link_res = await session.execute(
                        select(StreamingLink).where(
                            StreamingLink.movie_id == movie.id,
                            StreamingLink.provider_name == s["provider"],
                            StreamingLink.quality == s["quality"]
                        )
                    )
                    existing_link = link_res.scalars().first()
                    if not existing_link:
                        link = StreamingLink(
                            movie_id=movie.id,
                            provider_name=s["provider"],
                            embed_url=s["embed_url"],
                            direct_url=s["direct_url"],
                            quality=s["quality"],
                            file_code=s["file_code"],
                            status="active"
                        )
                        session.add(link)

                await session.commit()

        print(">>> Marvel movies seeded successfully with active streaming links!")

if __name__ == "__main__":
    asyncio.run(seed())
