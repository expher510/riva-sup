import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import TelegramArchive

logger = logging.getLogger("riva_telegram_vault")
logging.basicConfig(level=logging.INFO)

# Database session for worker
sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)


def upload_to_telegram_bot_api(
    file_path: Path,
    caption: str = "",
    bot_token: Optional[str] = None,
    channel_id: Optional[str] = None,
    api_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Uploads a video to a Telegram channel using the standard HTTP Bot API.
    Note: api.telegram.org has a 50MB file size limit for bot uploads.
    If a local Telegram Bot API server is configured, it supports up to 2000MB (2GB).
    """
    token = bot_token or settings.TELEGRAM_BOT_TOKEN
    target_channel = channel_id or settings.TELEGRAM_CHANNEL_ID
    base_url = api_url or settings.TELEGRAM_LOCAL_API_URL or "https://api.telegram.org"

    if not token or not target_channel:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID must be configured.")

    endpoint = f"{base_url.rstrip('/')}/bot{token}/sendVideo"
    file_size = file_path.stat().st_size

    # Warn if using public api.telegram.org with files > 50MB
    if "api.telegram.org" in base_url and file_size > 50 * 1024 * 1024:
        logger.warning(
            f"File size ({file_size / (1024*1024):.1f}MB) exceeds official 50MB limit of api.telegram.org. "
            f"Consider using MTProto (Pyrogram) or a local Bot API server."
        )

    logger.info(f"Uploading '{file_path.name}' ({file_size / (1024*1024):.1f}MB) to Telegram channel {target_channel}...")

    with open(file_path, "rb") as f:
        files = {"video": (file_path.name, f, "video/mp4")}
        data = {
            "chat_id": target_channel,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "supports_streaming": "true"
        }
        resp = requests.post(endpoint, data=data, files=files, timeout=900.0)
        resp.raise_for_status()
        res_json = resp.json()

    if not res_json.get("ok"):
        raise RuntimeError(f"Telegram upload failed: {res_json}")

    result = res_json.get("result", {})
    message_id = result.get("message_id")
    video_info = result.get("video", {})
    file_id = video_info.get("file_id")
    file_unique_id = video_info.get("file_unique_id")

    logger.info(f"Successfully uploaded to Telegram! Message ID: {message_id}, File ID: {file_id}")

    return {
        "channel_id": str(target_channel),
        "message_id": message_id,
        "telegram_file_id": file_id,
        "telegram_file_unique_id": file_unique_id,
        "file_size_bytes": file_size,
        "caption": caption
    }


def upload_via_telethon(
    file_path: Path,
    caption: str = "",
    api_id: Optional[str] = None,
    api_hash: Optional[str] = None,
    bot_token: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Uploads video up to 2GB directly via MTProto using Telethon.
    """
    import asyncio
    from telethon import TelegramClient

    aid = int(api_id or settings.TELEGRAM_API_ID)
    ahash = api_hash or settings.TELEGRAM_API_HASH
    btoken = bot_token or settings.TELEGRAM_BOT_TOKEN
    target_channel = channel_id or settings.TELEGRAM_CHANNEL_ID

    async def _runner():
        client = TelegramClient("riva_bot_session", aid, ahash)
        await client.start(bot_token=btoken)
        try:
            chat = int(target_channel) if (str(target_channel).startswith("-100") or str(target_channel).lstrip("-").isdigit()) else str(target_channel)
            logger.info(f"Telethon MTProto uploading '{file_path.name}' ({file_path.stat().st_size / (1024*1024):.1f} MB) to {chat}...")
            msg = await client.send_file(
                chat,
                file_path,
                caption=caption[:1024],
                parse_mode="html",
                supports_streaming=True
            )
            file_size = msg.file.size if msg.file else file_path.stat().st_size
            logger.info(f"Telethon upload SUCCESS! Message ID: {msg.id}")
            return {
                "channel_id": str(target_channel),
                "message_id": msg.id,
                "telegram_file_id": f"msg_{msg.id}",
                "telegram_file_unique_id": str(msg.id),
                "file_size_bytes": file_size,
                "caption": caption
            }
        finally:
            await client.disconnect()

    return asyncio.run(_runner())


def upload_video_to_vault(file_path: Path, caption: str = "") -> Dict[str, Any]:
    """
    Smart router: Uses Telethon MTProto (2GB limit) if API credentials are configured,
    or falls back to Bot API HTTP (50MB limit).
    """
    if settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH and settings.TELEGRAM_BOT_TOKEN:
        try:
            return upload_via_telethon(file_path, caption=caption)
        except Exception as exc:
            logger.warning(f"Telethon upload attempt failed, falling back to Bot API: {exc}")
    
    return upload_to_telegram_bot_api(file_path, caption=caption)


def record_telegram_archive(
    telegram_data: Dict[str, Any],
    episode_id: Optional[int] = None,
    movie_id: Optional[int] = None
) -> TelegramArchive:
    """
    Saves or updates TelegramArchive record in PostgreSQL/SQLite.
    """
    with SyncSessionLocal() as session:
        archive = TelegramArchive(
            movie_id=movie_id,
            episode_id=episode_id,
            channel_id=telegram_data["channel_id"],
            message_id=telegram_data["message_id"],
            telegram_file_id=telegram_data["telegram_file_id"],
            telegram_file_unique_id=telegram_data.get("telegram_file_unique_id"),
            file_size_bytes=telegram_data.get("file_size_bytes"),
            caption=telegram_data.get("caption")
        )
        session.add(archive)
        session.commit()
        session.refresh(archive)
        logger.info(f"Recorded TelegramArchive ID {archive.id} for episode={episode_id}, movie={movie_id}")
        return archive


def download_from_telegram_bot_api(
    file_id: str,
    dest_path: Path,
    bot_token: Optional[str] = None,
    api_url: Optional[str] = None
) -> Path:
    """
    Downloads a video file from Telegram for the Auto-Healing engine using Bot API.
    """
    token = bot_token or settings.TELEGRAM_BOT_TOKEN
    base_url = api_url or settings.TELEGRAM_LOCAL_API_URL or "https://api.telegram.org"

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN must be configured.")

    # 1. Get file path
    get_file_url = f"{base_url.rstrip('/')}/bot{token}/getFile"
    r = requests.get(get_file_url, params={"file_id": file_id}, timeout=30.0)
    r.raise_for_status()
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Failed to getFile from Telegram: {res}")

    remote_file_path = res["result"]["file_path"]

    # 2. Download file content
    download_url = f"{base_url.rstrip('/')}/file/bot{token}/{remote_file_path}"
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading Telegram file {file_id} to {dest_path}...")
    with requests.get(download_url, stream=True, timeout=600.0) as stream_resp:
        stream_resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in stream_resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    logger.info(f"Download complete: {dest_path} ({dest_path.stat().st_size} bytes)")
    return dest_path


def send_telegram_notification(text: str, level: str = "info") -> bool:
    """
    Sends an instant notification or error alert to the configured Telegram channel/group.
    """
    token = settings.TELEGRAM_BOT_TOKEN
    target_channel = settings.TELEGRAM_CHANNEL_ID
    if not token or not target_channel:
        return False

    icons = {
        "error": "🚨 <b>[RIVA ERROR ALERT]</b>",
        "ban": "⛔ <b>[RIVA IP BAN / 403 WARNING]</b>",
        "warning": "⚠️ <b>[RIVA WARNING]</b>",
        "success": "✅ <b>[RIVA SUCCESS]</b>",
        "info": "ℹ️ <b>[RIVA SYSTEM INFO]</b>"
    }
    header = icons.get(level, "📢 <b>[RIVA NOTIFICATION]</b>")
    formatted_msg = f"{header}\n\n{text}"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": target_channel,
        "text": formatted_msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=10.0)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Failed to send telegram notification: {e}")
        return False
