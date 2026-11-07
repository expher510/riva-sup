"""
vault_mirror_backfill.py
========================
Backfill script: copies ALL existing primary-vault archives to EVERY configured
delivery vault channel using @Riva109_bot's copyMessage.

Channels are read from (priority order):
  1. BACKUP_VAULT_CHANNEL_IDS — comma-separated list (preferred, multi-vault)
  2. BACKUP_VAULT_CHANNEL_ID  — legacy single-channel value

• Zero bandwidth cost — files stay on Telegram servers.
• 2-second throttle between copies to avoid FloodWait.
• Skips archives that already exist in the target channel.
• On FloodWait (429): sleeps (retry_after + 5) seconds and retries once.
• Logs progress every 100 copies.

Run on VPS:
    nohup python3 -m worker.vault_mirror_backfill >> /tmp/mirror_backfill.log 2>&1 &
"""

import os
import sys
import time
import logging
import requests
from typing import List
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── ensure project root is on path when run as __main__ ──────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.models import TelegramArchive

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [vault_mirror_backfill] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vault_mirror_backfill")

# ── Config ───────────────────────────────────────────────────────────────────
BACKUP_TOKEN    = (getattr(settings, "BACKUP_VAULT_BOT_TOKEN", "") or os.getenv("BACKUP_VAULT_BOT_TOKEN", "")).strip()
PRIMARY_CHANNEL = str(settings.TELEGRAM_CHANNEL_ID or "").strip()

# Multi-channel: prefer BACKUP_VAULT_CHANNEL_IDS, fall back to BACKUP_VAULT_CHANNEL_ID
_raw_ids = (
    getattr(settings, "BACKUP_VAULT_CHANNEL_IDS", "") or os.getenv("BACKUP_VAULT_CHANNEL_IDS", "")
    or getattr(settings, "BACKUP_VAULT_CHANNEL_ID", "") or os.getenv("BACKUP_VAULT_CHANNEL_ID", "")
).strip()
BACKUP_CHANNELS: List[str] = [c.strip() for c in _raw_ids.split(",") if c.strip()]

THROTTLE_SECONDS   = 2          # delay between each copyMessage
FLOOD_EXTRA_SLEEP  = 5          # extra buffer after FloodWait
MIN_FILE_SIZE      = 15 * 1024 * 1024  # 15 MB — only real video archives
LOG_INTERVAL       = 100        # print summary every N copies

# ── DB ───────────────────────────────────────────────────────────────────────
engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True,
)
Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def copy_message(backup_channel: str, from_chat_id: int, message_id: int) -> dict:
    """Calls copyMessage API for a single backup channel; returns the JSON result dict."""
    return requests.post(
        f"https://api.telegram.org/bot{BACKUP_TOKEN}/copyMessage",
        json={
            "chat_id":      int(backup_channel),
            "from_chat_id": from_chat_id,
            "message_id":   message_id,
        },
        timeout=40,
    ).json()


def backfill_to_channel(backup_channel: str):
    """Mirror all missing primary-vault archives to a single delivery vault channel."""
    logger.info(f"═══ Starting backfill → channel {backup_channel} ═══")

    with Session() as session:
        rows = session.execute(
            text("""
                SELECT ta.id,
                       ta.movie_id,
                       ta.episode_id,
                       ta.channel_id,
                       ta.message_id,
                       ta.telegram_file_id,
                       ta.telegram_file_unique_id,
                       ta.file_size_bytes,
                       ta.caption
                FROM telegram_archives ta
                WHERE ta.file_size_bytes > :min_size
                  AND ta.channel_id != :backup_ch
                  AND ta.message_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM telegram_archives bk
                      WHERE bk.channel_id = :backup_ch
                        AND (
                            (bk.movie_id   IS NOT NULL AND bk.movie_id   = ta.movie_id)
                            OR
                            (bk.episode_id IS NOT NULL AND bk.episode_id = ta.episode_id)
                        )
                  )
                ORDER BY ta.id ASC
            """),
            {"min_size": MIN_FILE_SIZE, "backup_ch": backup_channel},
        ).fetchall()

    total = len(rows)
    logger.info(f"Found {total} archives to mirror to {backup_channel}")

    if total == 0:
        logger.info(f"Nothing to do for {backup_channel} — already up to date! ✅")
        return

    done = 0
    skipped = 0
    errors = 0

    for row in rows:
        (arc_id, movie_id, episode_id, src_channel, message_id,
         file_id, file_unique_id, file_size, caption) = row

        from_channel = int(src_channel) if src_channel else int(PRIMARY_CHANNEL)

        # ── copyMessage with FloodWait retry ─────────────────────────────────
        result = None
        for attempt in range(2):
            try:
                result = copy_message(backup_channel, from_channel, int(message_id))
            except Exception as exc:
                logger.warning(f"arc {arc_id}: request error attempt {attempt+1}: {exc}")
                time.sleep(THROTTLE_SECONDS * 3)
                continue

            if result.get("ok"):
                break  # success

            err_code = result.get("error_code", 0)
            err_desc = result.get("description", "")

            if err_code == 429:  # FloodWait
                retry_after = result.get("parameters", {}).get("retry_after", 30)
                sleep_time = int(retry_after) + FLOOD_EXTRA_SLEEP
                logger.warning(f"FloodWait {retry_after}s on arc {arc_id}. Sleeping {sleep_time}s…")
                time.sleep(sleep_time)
                try:
                    result = copy_message(backup_channel, from_channel, int(message_id))
                except Exception:
                    pass
                break

            elif err_code in (400, 403):
                logger.warning(f"arc {arc_id}: Telegram error {err_code} — '{err_desc}'. Skipping.")
                skipped += 1
                result = None
                break

            else:
                logger.warning(f"arc {arc_id}: unexpected error {err_code} — '{err_desc}'")
                break

        if not result or not result.get("ok"):
            errors += 1
            time.sleep(THROTTLE_SECONDS)
            continue

        backup_msg_id = result["result"]["message_id"]

        # ── Persist backup archive row ────────────────────────────────────────
        with Session() as session:
            try:
                backup_arc = TelegramArchive(
                    movie_id=movie_id,
                    episode_id=episode_id,
                    channel_id=backup_channel,
                    message_id=backup_msg_id,
                    telegram_file_id=file_id,
                    telegram_file_unique_id=file_unique_id,
                    file_size_bytes=file_size,
                    caption=caption,
                )
                session.add(backup_arc)
                session.commit()
            except Exception as db_err:
                session.rollback()
                logger.error(f"arc {arc_id}: DB insert failed: {db_err}")
                errors += 1
                time.sleep(THROTTLE_SECONDS)
                continue

        done += 1

        if done % LOG_INTERVAL == 0:
            pct = (done / total) * 100
            logger.info(
                f"Progress → {backup_channel}: {done}/{total} ({pct:.1f}%) done | "
                f"{skipped} skipped | {errors} errors | "
                f"~{int((total - done) * THROTTLE_SECONDS / 60)} min remaining"
            )

        time.sleep(THROTTLE_SECONDS)

    logger.info(
        f"Backfill → {backup_channel} complete! ✅  "
        f"Mirrored: {done} | Skipped: {skipped} | Errors: {errors} | Total: {total}"
    )


def run_backfill():
    if not BACKUP_TOKEN:
        logger.error("BACKUP_VAULT_BOT_TOKEN not set. Abort.")
        sys.exit(1)
    if not BACKUP_CHANNELS:
        logger.error("No backup channels configured (BACKUP_VAULT_CHANNEL_IDS or BACKUP_VAULT_CHANNEL_ID). Abort.")
        sys.exit(1)

    logger.info(f"Delivery vaults to backfill: {BACKUP_CHANNELS}")
    for ch in BACKUP_CHANNELS:
        backfill_to_channel(ch)

    logger.info("All delivery vaults backfill complete! 🎉")


if __name__ == "__main__":
    run_backfill()


