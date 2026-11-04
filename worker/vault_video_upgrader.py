"""
Riva Cinema - Vault Video Upgrader & FastStart Stream Optimizer
Downloads existing unstreamable videos from Telegram Vault, applies +faststart,
generates thumbnails, probes true dimensions/duration, re-uploads as native web-streamable videos,
and updates the database.
"""

import os
import sys
import time
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.types import DocumentAttributeVideo

from app.config import settings
from worker.telegram_vault import get_video_metadata, optimize_for_faststart_and_thumb

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s: %(levelname)s/vault_upgrader] %(message)s"
)
logger = logging.getLogger("vault_upgrader")

# Sync database session
db_url = getattr(settings, "SYNC_DATABASE_URL", None) or settings.DATABASE_URL
if "+asyncpg" in db_url:
    db_url = db_url.replace("+asyncpg", "+psycopg2")
if "+aiosqlite" in db_url:
    db_url = db_url.replace("+aiosqlite", "")

sync_engine = create_engine(db_url, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)


async def upgrade_single_vault_archive(client: TelegramClient, archive_id: int, scratch_dir: Path) -> bool:
    """
    Upgrades a single TelegramArchive row:
    1. Fetches old message from Telegram.
    2. Checks if already streamable with valid dimensions/duration.
    3. Downloads from Telegram directly to VPS scratch.
    4. Applies +faststart, extracts thumbnail, probes width/height/duration.
    5. Re-uploads as full native video with streaming attributes.
    6. Updates database and deletes old message.
    """
    with SyncSessionLocal() as session:
        row = session.execute(
            text("SELECT id, movie_id, episode_id, channel_id, message_id FROM telegram_archives WHERE id = :id"),
            {"id": archive_id}
        ).fetchone()

    if not row or not row.message_id:
        logger.warning(f"Archive #{archive_id} has no valid message_id. Skipping.")
        return False

    channel_id = int(row.channel_id) if row.channel_id else int(settings.TELEGRAM_CHANNEL_ID)
    old_msg_id = int(row.message_id)

    try:
        old_msg = await client.get_messages(channel_id, ids=old_msg_id)
    except Exception as e:
        logger.error(f"Could not fetch message {old_msg_id} from Telegram: {e}")
        return False

    if not old_msg or not old_msg.media:
        logger.warning(f"Message {old_msg_id} not found in channel {channel_id} or has no media. Skipping.")
        return False

    # Check if already upgraded
    if hasattr(old_msg, 'document') and old_msg.document:
        for attr in old_msg.document.attributes:
            if type(attr).__name__ == "DocumentAttributeVideo":
                if getattr(attr, 'w', 0) > 1 and getattr(attr, 'duration', 0) > 0:
                    logger.info(f"Archive #{archive_id} (msg {old_msg_id}) is ALREADY streamable ({attr.w}x{attr.h}, {attr.duration}s). Skipping.")
                    return True

    logger.info(f"🚀 Upgrading Archive #{archive_id} (old msg {old_msg_id})...")
    caption = old_msg.text or ""

    # Download from Telegram
    dl_path = scratch_dir / f"upgrade_{archive_id}_raw.mp4"
    try:
        logger.info(f"Downloading from Telegram to VPS ({dl_path.name})...")
        await client.download_media(old_msg, file=str(dl_path))

        if not dl_path.exists() or dl_path.stat().st_size < 1000000:
            logger.error(f"Download failed or empty file for archive #{archive_id}")
            return False

        # Metadata & FastStart
        w, h, duration = get_video_metadata(dl_path)
        fast_path, thumb_path = optimize_for_faststart_and_thumb(dl_path, duration)
        new_size = fast_path.stat().st_size

        attributes = [
            DocumentAttributeVideo(
                duration=int(duration),
                w=int(w),
                h=int(h),
                supports_streaming=True
            )
        ]

        logger.info(f"Uploading upgraded streamable video ({new_size / (1024*1024):.1f} MB, {w}x{h}, {duration}s)...")
        new_msg = await client.send_file(
            channel_id,
            fast_path,
            caption=caption[:1024],
            parse_mode="html",
            thumb=str(thumb_path) if thumb_path and thumb_path.exists() else None,
            attributes=attributes,
            supports_streaming=True
        )

        # Update database
        with SyncSessionLocal() as session:
            session.execute(
                text("""
                    UPDATE telegram_archives
                    SET message_id = :new_msg_id,
                        file_size_bytes = :new_size,
                        telegram_file_id = :tg_fid
                    WHERE id = :id
                """),
                {
                    "new_msg_id": new_msg.id,
                    "new_size": new_size,
                    "tg_fid": f"msg_{new_msg.id}",
                    "id": archive_id
                }
            )
            session.commit()

        logger.info(f"✅ Successfully upgraded Archive #{archive_id}: old msg {old_msg_id} -> new msg {new_msg.id}")

        # Delete old message from channel to avoid clutter
        try:
            await client.delete_messages(channel_id, [old_msg_id])
            logger.info(f"Deleted old unstreamable message {old_msg_id} from channel.")
        except Exception as del_err:
            logger.warning(f"Could not delete old message {old_msg_id}: {del_err}")

        return True

    finally:
        # Clean up local temporary files immediately
        if dl_path.exists():
            dl_path.unlink(missing_ok=True)
        fast_p = scratch_dir / f"fast_{dl_path.name}"
        if fast_p.exists():
            fast_p.unlink(missing_ok=True)
        thumb_p = scratch_dir / f"thumb_{dl_path.stem}.jpg"
        if thumb_p.exists():
            thumb_p.unlink(missing_ok=True)


async def run_vault_upgrade(target_type: str = "movies", limit: int = 50):
    """
    Iterates over telegram_archives and upgrades unstreamable videos.
    """
    aid = int(settings.TELEGRAM_API_ID)
    ahash = settings.TELEGRAM_API_HASH
    btoken = settings.TELEGRAM_BOT_TOKEN

    scratch_dir = Path("/tmp/riva_upgrade")
    scratch_dir.mkdir(parents=True, exist_ok=True)

    with SyncSessionLocal() as session:
        if target_type == "movies":
            sql = "SELECT id FROM telegram_archives WHERE movie_id IS NOT NULL ORDER BY id DESC LIMIT :limit;"
        elif target_type == "episodes":
            sql = "SELECT id FROM telegram_archives WHERE episode_id IS NOT NULL ORDER BY id DESC LIMIT :limit;"
        else:
            sql = "SELECT id FROM telegram_archives ORDER BY id DESC LIMIT :limit;"

        rows = session.execute(text(sql), {"limit": limit}).fetchall()
        archive_ids = [r[0] for r in rows]

    logger.info(f"Starting Vault Upgrade for {len(archive_ids)} {target_type} items...")

    client = TelegramClient(MemorySession(), aid, ahash)
    await client.start(bot_token=btoken)

    success_count = 0
    try:
        for idx, aid_val in enumerate(archive_ids, 1):
            logger.info(f"Processing [{idx}/{len(archive_ids)}] Archive ID: {aid_val}")
            res = await upgrade_single_vault_archive(client, aid_val, scratch_dir)
            if res:
                success_count += 1
            await asyncio.sleep(2.0)  # Gentle delay to prevent flood waits
    finally:
        await client.disconnect()

    logger.info(f"Upgrade completed! Successfully processed {success_count}/{len(archive_ids)} items.")


if __name__ == "__main__":
    t_type = "movies"
    if "--episodes" in sys.argv:
        t_type = "episodes"
    elif "--all" in sys.argv:
        t_type = "all"

    lim = 50
    for arg in sys.argv:
        if arg.startswith("--limit="):
            lim = int(arg.split("=")[1])

    asyncio.run(run_vault_upgrade(target_type=t_type, limit=lim))
