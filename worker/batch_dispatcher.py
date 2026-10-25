"""
Batch Dispatcher for Riva VOD Engine.
Dispatches all pending scraped episodes to Celery for processing (DoodStream + Streamtape + Telegram Vault).
Also coordinates continuous crawler runs.
"""
import sys
import time
import logging
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Episode, StreamingLink, TelegramArchive
from worker.pipeline import process_episode_transit_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("batch_dispatcher")

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True
)
SyncSession = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)


def dispatch_pending_episodes(limit: int = 50) -> int:
    """
    Finds episodes that have direct HLS streams but no Telegram archive yet,
    and dispatches them to Celery.
    """
    with SyncSession() as session:
        # Find distinct episodes with direct stream that aren't yet in telegram_archives
        query = text("""
            SELECT DISTINCT ON (e.id) e.id, sl.direct_url, e.title
            FROM episodes e
            JOIN streaming_links sl ON sl.episode_id = e.id
            WHERE sl.direct_url IS NOT NULL 
              AND sl.direct_url != ''
              AND e.id NOT IN (
                  SELECT episode_id FROM telegram_archives WHERE episode_id IS NOT NULL
              )
            ORDER BY e.id ASC
            LIMIT :lim;
        """)
        rows = session.execute(query, {"lim": limit}).fetchall()

        if not rows:
            logger.info("No pending episodes found to dispatch.")
            return 0

        logger.info(f"Found {len(rows)} pending episodes to dispatch to Celery.")
        count = 0
        for ep_id, direct_url, title in rows:
            try:
                task = process_episode_transit_pipeline.delay(ep_id, direct_url, quality="1080p")
                logger.info(f"  🚀 Dispatched Ep {ep_id} [{title[:30]}] -> Celery Task ID: {task.id}")
                count += 1
            except Exception as e:
                logger.error(f"Failed to dispatch Ep {ep_id}: {e}")

        return count


def print_stats():
    """Prints current database and vault statistics."""
    with SyncSession() as session:
        total_series = session.execute(text("SELECT count(*) FROM series;")).scalar()
        total_episodes = session.execute(text("SELECT count(*) FROM episodes;")).scalar()
        total_streams = session.execute(text("SELECT count(*) FROM streaming_links;")).scalar()
        total_tg = session.execute(text("SELECT count(*) FROM telegram_archives;")).scalar()
        pending = session.execute(text("""
            SELECT count(DISTINCT e.id)
            FROM episodes e
            JOIN streaming_links sl ON sl.episode_id = e.id
            WHERE sl.direct_url IS NOT NULL AND sl.direct_url != ''
              AND e.id NOT IN (SELECT episode_id FROM telegram_archives WHERE episode_id IS NOT NULL);
        """)).scalar()

        print("\n" + "=" * 50)
        print("  RIVA AUTOMATION ENGINE - LIVE STATS")
        print("=" * 50)
        print(f"  • Total Series in DB:         {total_series}")
        print(f"  • Total Episodes Scraped:     {total_episodes}")
        print(f"  • Total Streaming Links:      {total_streams}")
        print(f"  • Uploaded to Telegram Vault: {total_tg}")
        print(f"  • Pending Upload Queue:       {pending}")
        print("=" * 50 + "\n")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "dispatch"
    
    if action == "stats":
        print_stats()
    elif action == "dispatch":
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        dispatched = dispatch_pending_episodes(limit=lim)
        print(f"Dispatched {dispatched} tasks.")
        print_stats()
