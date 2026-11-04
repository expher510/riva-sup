import os
import re
import time
import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import requests
from sqlalchemy import create_engine, text
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


def get_video_metadata(file_path: Path) -> Tuple[int, int, int]:
    """
    Extracts real video stream (width, height, duration_in_seconds) using ffprobe.
    Explicitly ignores disguised image streams (png, mjpeg, gif, webp, bmp) and
    returns (0, 0, 0) if no genuine streamable video track exists.
    """
    image_pseudo_codecs = {"png", "mjpeg", "jpg", "jpeg", "gif", "webp", "bmp", "tiff"}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v",
            "-show_entries", "stream=codec_name,width,height,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            str(file_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if res.returncode == 0:
            data = json.loads(res.stdout.decode("utf-8", errors="replace"))
            streams = data.get("streams", [])
            fmt = data.get("format", {})
            fmt_dur = 0
            if fmt.get("duration"):
                try:
                    fmt_dur = int(float(fmt["duration"]))
                except (ValueError, TypeError):
                    fmt_dur = 0

            for st in streams:
                codec = str(st.get("codec_name") or "").lower().strip()
                if codec in image_pseudo_codecs:
                    continue
                w = int(st.get("width") or 0)
                h = int(st.get("height") or 0)
                if w < 160 or h < 90:
                    continue
                st_dur = 0
                if st.get("duration"):
                    try:
                        st_dur = int(float(st["duration"]))
                    except (ValueError, TypeError):
                        st_dur = 0
                d = max(st_dur, fmt_dur)
                return w, h, d
    except Exception as e:
        logger.warning(f"ffprobe metadata extraction failed for {file_path.name}: {e}")
    return 0, 0, 0


def _resolve_tmdb_media_url(path_or_url: Optional[str], size: str = "w500") -> Optional[str]:
    if not path_or_url:
        return None
    s = str(path_or_url).strip()
    if not s or s.startswith("tg_photo:"):
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if not s.startswith("/"):
        s = "/" + s
    return f"https://image.tmdb.org/t/p/{size}{s}"


def _build_guaranteed_jpeg_thumb(
    video_path: Path,
    thumb_path: Path,
    w: int,
    h: int,
    duration: int,
    thumb_url: Optional[str] = None,
    backdrop_url: Optional[str] = None,
) -> Path:
    """
    Produces a guaranteed Telegram-compliant baseline JPEG thumbnail (yuvj420p, <=320x320, <195KB)
    matching the video's aspect ratio so Telegram & Telegram Web Browser ALWAYS display an inline
    playable video box with Poster/Cover preview without requiring opening Media.
    """
    safe_w = max(w, 320)
    safe_h = max(h, 180)
    if safe_w >= safe_h:
        tw = 320
        th = max(90, min(320, int(round((320.0 * safe_h / safe_w) / 2.0) * 2)))
    else:
        th = 320
        tw = max(90, min(320, int(round((320.0 * safe_w / safe_h) / 2.0) * 2)))

    poster_full = _resolve_tmdb_media_url(thumb_url, size="w500")
    backdrop_full = _resolve_tmdb_media_url(backdrop_url, size="w780")
    raw_cover = thumb_path.with_name(f"raw_cover_{thumb_path.stem}.img")

    def _valid_thumb() -> bool:
        return thumb_path.exists() and 400 <= thumb_path.stat().st_size <= 198 * 1024

    # 1. Try Official Movie/Series Poster (blurred cinema backdrop + sharp centered poster) or Backdrop
    try:
        if poster_full:
            r = requests.get(poster_full, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            if r.status_code == 200 and len(r.content) > 800:
                raw_cover.write_bytes(r.content)
                if tw >= th:
                    vf_filter = (
                        f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},"
                        f"boxblur=10:2,eq=brightness=-0.16[bg];"
                        f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=decrease[fg];"
                        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuvj420p"
                    )
                    cmd = [
                        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(raw_cover),
                        "-filter_complex", vf_filter,
                        "-frames:v", "1", "-q:v", "3",
                        str(thumb_path)
                    ]
                else:
                    cmd = [
                        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(raw_cover),
                        "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},format=yuvj420p",
                        "-frames:v", "1", "-q:v", "3",
                        str(thumb_path)
                    ]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

        if not _valid_thumb() and backdrop_full:
            r = requests.get(backdrop_full, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            if r.status_code == 200 and len(r.content) > 800:
                raw_cover.write_bytes(r.content)
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(raw_cover),
                    "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},format=yuvj420p",
                    "-frames:v", "1", "-q:v", "3",
                    str(thumb_path)
                ]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except Exception as e:
        logger.debug(f"Poster/backdrop thumb download skipped for {video_path.name}: {e}")
    finally:
        raw_cover.unlink(missing_ok=True)

    # 2. Fallback: Extract a bright mid-video frame
    if not _valid_thumb():
        candidate_seeks = [
            min(180, max(12, duration // 5)) if duration else 15,
            min(45, max(3, duration // 10)) if duration else 5,
            2,
        ]
        for seek_sec in candidate_seeks:
            try:
                thumb_cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                    "-ss", str(seek_sec),
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,format=yuvj420p",
                    "-q:v", "3",
                    str(thumb_path)
                ]
                subprocess.run(thumb_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
                if _valid_thumb():
                    break
            except Exception as e:
                logger.debug(f"Frame thumb attempt at {seek_sec}s failed: {e}")

    # 3. Recompress if > 195 KB, or generate dark cinema slate as ultimate guarantee
    if thumb_path.exists() and thumb_path.stat().st_size > 195 * 1024:
        recomp = thumb_path.with_name(f"re_{thumb_path.name}")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(thumb_path), "-vf", f"scale={tw}:{th},format=yuvj420p", "-q:v", "6", str(recomp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
        )
        if recomp.exists() and recomp.stat().st_size < 195 * 1024:
            recomp.replace(thumb_path)
        else:
            recomp.unlink(missing_ok=True)

    if not _valid_thumb():
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-f", "lavfi",
                "-i", f"color=c=0x101522:s={tw}x{th}:d=1",
                "-frames:v", "1", "-pix_fmt", "yuvj420p", "-q:v", "4",
                str(thumb_path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
        )

    return thumb_path


def optimize_for_faststart_and_thumb(
    file_path: Path,
    duration: int = 0,
    thumb_url: Optional[str] = None,
    backdrop_url: Optional[str] = None,
    width: int = 1280,
    height: int = 720,
) -> Tuple[Path, Optional[Path]]:
    """
    Remuxes MP4 with +faststart (moov atom at beginning) and builds a guaranteed <=320px JPEG Poster/Thumbnail.
    Skips redundant +faststart rewrite if the downloader already muxed +faststart!
    Returns: (faststart_path, thumb_path)
    """
    faststart_marker = file_path.with_suffix(".faststart_done")
    faststart_path = file_path.with_name(f"fast_{file_path.stem}.mp4")
    thumb_path = file_path.with_name(f"thumb_{file_path.stem}.jpg")

    if faststart_marker.exists():
        faststart_path = file_path
        faststart_marker.unlink(missing_ok=True)
    else:
        # 1. Faststart remux (mapping clean primary video + audio streams only)
        try:
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                "-i", str(file_path),
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-dn",
                "-c", "copy",
                "-movflags", "+faststart",
                str(faststart_path)
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if res.returncode != 0 or not faststart_path.exists() or faststart_path.stat().st_size < 1000:
                cmd_ts = [
                    "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                    "-i", str(file_path),
                    "-map", "0:v:0",
                    "-map", "0:a:0?",
                    "-dn",
                    "-c", "copy",
                    "-bsf:a", "aac_adtstoasc",
                    "-movflags", "+faststart",
                    str(faststart_path)
                ]
                res2 = subprocess.run(cmd_ts, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
                if res2.returncode != 0 or not faststart_path.exists() or faststart_path.stat().st_size < 1000:
                    logger.warning(f"ffmpeg faststart failed, falling back to original: {res.stderr.decode('utf-8', errors='replace')[-200:]}")
                    faststart_path = file_path
        except Exception as e:
            logger.warning(f"Faststart optimization error: {e}")
            faststart_path = file_path

    # 1b. Enforce Telegram MTProto 4000-part (< 2000 MiB) ceiling so oversized streams never raise FilePartsInvalidError
    max_vault_bytes = 1950 * 1024 * 1024
    if faststart_path.exists() and faststart_path.stat().st_size >= max_vault_bytes:
        shrunk_path = file_path.parent / f"shrunk_{file_path.stem}.mp4"
        try:
            dur_sec = max(int(duration or 0), 60)
            target_v_kbps = max(450, min(2200, int((1700 * 8192) / dur_sec) - 128))
            logger.warning(
                f"Video '{faststart_path.name}' ({faststart_path.stat().st_size / 1048576:.1f} MiB) exceeds 1950 MiB limit; "
                f"transcoding to ~1700 MiB ({target_v_kbps} kbps) for MTProto..."
            )
            shrink_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                "-i", str(faststart_path),
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-dn",
                "-vf", "scale='min(1080,iw)':-2",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-threads", os.getenv("FFMPEG_ENCODE_THREADS", "2"),
                "-b:v", f"{target_v_kbps}k",
                "-maxrate", f"{int(target_v_kbps * 1.15)}k",
                "-bufsize", f"{int(target_v_kbps * 2)}k",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ac", "2",
                "-movflags", "+faststart",
                str(shrunk_path),
            ]
            res_sh = subprocess.run(shrink_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2400)
            if (
                res_sh.returncode == 0
                and shrunk_path.exists()
                and 15 * 1024 * 1024 < shrunk_path.stat().st_size < 1990 * 1024 * 1024
            ):
                if faststart_path != file_path:
                    faststart_path.unlink(missing_ok=True)
                faststart_path = shrunk_path
                sw, sh, sdur = get_video_metadata(faststart_path)
                if sw >= 160 and sh >= 90 and sdur >= 30:
                    width, height, duration = sw, sh, sdur
            else:
                shrunk_path.unlink(missing_ok=True)
                logger.warning(f"Oversized shrink failed: {res_sh.stderr.decode('utf-8', errors='replace')[-220:]}")
        except Exception as sh_err:
            shrunk_path.unlink(missing_ok=True)
            logger.warning(f"Oversized shrink error: {sh_err}")

    # 2. Build guaranteed Poster/Frame JPEG thumbnail
    _build_guaranteed_jpeg_thumb(
        video_path=faststart_path,
        thumb_path=thumb_path,
        w=width,
        h=height,
        duration=duration,
        thumb_url=thumb_url,
        backdrop_url=backdrop_url,
    )

    final_thumb = thumb_path if thumb_path.exists() and thumb_path.stat().st_size > 200 else None
    return faststart_path, final_thumb


def upload_to_telegram_bot_api(
    file_path: Path,
    caption: str = "",
    bot_token: Optional[str] = None,
    channel_id: Optional[str] = None,
    api_url: Optional[str] = None,
    thumb_url: Optional[str] = None,
    backdrop_url: Optional[str] = None,
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
    w, h, duration = get_video_metadata(file_path)
    if w < 160 or h < 90 or duration < 30:
        raise RuntimeError(
            f"Streaming Gate Rejected '{file_path.name}': invalid video dimensions/duration ({w}x{h}, {duration}s)."
        )

    fast_file, thumb_file = optimize_for_faststart_and_thumb(
        file_path, duration=duration, thumb_url=thumb_url, backdrop_url=backdrop_url, width=w, height=h
    )
    if fast_file != file_path:
        fw, fh, fdur = get_video_metadata(fast_file)
        if fw >= 160 and fh >= 90 and fdur >= 30:
            w, h, duration = fw, fh, fdur

    if not thumb_file or not thumb_file.exists():
        raise RuntimeError(f"Streaming Gate Rejected '{file_path.name}': missing JPEG thumbnail/poster.")

    file_size = fast_file.stat().st_size

    # Warn if using public api.telegram.org with files > 50MB
    if "api.telegram.org" in base_url and file_size > 50 * 1024 * 1024:
        logger.warning(
            f"File size ({file_size / (1024*1024):.1f}MB) exceeds official 50MB limit of api.telegram.org. "
            f"Consider using MTProto (Telethon) or a local Bot API server."
        )

    logger.info(f"Uploading '{fast_file.name}' ({file_size / (1024*1024):.1f}MB, {w}x{h}, {duration}s) to Telegram channel {target_channel}...")

    try:
        with open(fast_file, "rb") as f:
            clean_name = fast_file.name if fast_file.name.lower().endswith(".mp4") else f"{fast_file.stem}.mp4"
            files = {"video": (clean_name, f, "video/mp4")}
            thumb_fp = open(thumb_file, "rb")
            files["thumbnail"] = ("thumb.jpg", thumb_fp, "image/jpeg")

            tok_id = token.split(":")[0] if ":" in token else "bot"
            tok_lbl = "@riva18_bot (الأساسي)" if token == settings.TELEGRAM_BOT_TOKEN else f"bot_{tok_id}"
            vault_cap = _format_vault_caption_with_bot(caption, tok_lbl, tok_id)
            data = {
                "chat_id": target_channel,
                "caption": vault_cap[:1024],
                "parse_mode": "HTML",
                "duration": int(duration),
                "width": int(w),
                "height": int(h),
                "supports_streaming": "true"
            }
            resp = requests.post(endpoint, data=data, files=files, timeout=900.0)
            thumb_fp.close()
            resp.raise_for_status()
            res_json = resp.json()
    finally:
        if fast_file != file_path and fast_file.exists():
            fast_file.unlink(missing_ok=True)
        if thumb_file and thumb_file.exists():
            thumb_file.unlink(missing_ok=True)

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


_WORKER_SESSION_CACHE: Dict[str, str] = {}


async def _parallel_upload_file(client, file_path: Path, workers: int = 8):
    """
    Uploads a file to Telegram using pipelined concurrent SaveBigFilePartRequest calls
    (up to 8 x 512KB chunks in flight simultaneously) with cryptg C-accelerated AES-IGE encryption.
    Falls back seamlessly to client.upload_file(part_size_kb=512) if needed.
    """
    import asyncio
    import math
    import random
    from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
    from telethon.tl.types import InputFileBig, InputFile

    clean_mp4_name = file_path.name if file_path.name.lower().endswith(".mp4") else f"{file_path.stem}.mp4"
    try:
        file_size = file_path.stat().st_size
        part_size = 512 * 1024  # Maximum 512KB chunk size allowed by MTProto
        part_count = math.ceil(file_size / part_size)
        is_big = file_size > 10 * 1024 * 1024
        file_id = random.randrange(-2**63, 2**63)

        if part_count <= 8 or workers <= 1:
            return await client.upload_file(file_path, part_size_kb=512, file_name=clean_mp4_name)

        sem = asyncio.Semaphore(workers)

        async def _send_part(part_idx: int, chunk_bytes: bytes):
            async with sem:
                for retry in range(4):
                    try:
                        if is_big:
                            req = SaveBigFilePartRequest(file_id, part_idx, part_count, chunk_bytes)
                        else:
                            req = SaveFilePartRequest(file_id, part_idx, chunk_bytes)
                        res = await client(req)
                        if res:
                            return
                        raise RuntimeError(f"SaveBigFilePartRequest returned False on part {part_idx}/{part_count}")
                    except Exception as e:
                        if retry == 3:
                            raise e
                        await asyncio.sleep(1.0 * (retry + 1))

        pending_tasks = []
        with open(file_path, "rb") as f:
            for idx in range(part_count):
                chunk = f.read(part_size)
                if not chunk:
                    break
                # Wait if we already have workers * 2 tasks queued to bound RAM usage (~8 MB max)
                if len(pending_tasks) >= workers * 2:
                    done, pending_set = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
                    for d in done:
                        d.result()
                    pending_tasks = list(pending_set)
                pending_tasks.append(asyncio.create_task(_send_part(idx, chunk)))

        if pending_tasks:
            await asyncio.gather(*pending_tasks)

        if is_big:
            return InputFileBig(file_id, part_count, clean_mp4_name)
        return InputFile(file_id, part_count, clean_mp4_name, "")
    except Exception as parallel_err:
        if "pending_tasks" in locals() and pending_tasks:
            for pt in pending_tasks:
                pt.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if "part_count" in locals() and part_count > 4000:
            raise
        logger.warning(f"Pipelined MTProto upload fallback to standard 512KB upload: {parallel_err}")
        return await client.upload_file(file_path, part_size_kb=512, file_name=clean_mp4_name)


_UPLOADER_SCHEMA_READY = False
_DB_POOL_CACHE: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
_LAST_PENDING_RECHECK_TS: float = 0.0
_SYNC_REDIS_CLIENT = None


def _get_sync_redis():
    global _SYNC_REDIS_CLIENT
    if _SYNC_REDIS_CLIENT is None:
        try:
            import redis
            _SYNC_REDIS_CLIENT = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
        except Exception as e:
            logger.debug(f"Redis client unavailable for uploader load-balancing: {e}")
            return None
    return _SYNC_REDIS_CLIENT


def _ensure_uploader_bots_table() -> None:
    global _UPLOADER_SCHEMA_READY
    if _UPLOADER_SCHEMA_READY:
        return
    try:
        with sync_engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS uploader_bots (
                    id SERIAL PRIMARY KEY,
                    bot_token VARCHAR(200) UNIQUE NOT NULL,
                    bot_id BIGINT,
                    bot_username VARCHAR(150),
                    bot_name VARCHAR(255),
                    is_active BOOLEAN DEFAULT TRUE,
                    channel_ok BOOLEAN DEFAULT FALSE,
                    uploads_count INTEGER DEFAULT 0,
                    total_uploaded_bytes BIGINT DEFAULT 0,
                    last_used_at TIMESTAMP NULL,
                    last_error TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """))
        _UPLOADER_SCHEMA_READY = True
    except Exception as e:
        logger.warning(f"Could not ensure uploader_bots schema: {e}")


def _format_vault_caption_with_bot(raw_caption: str, bot_label: str, bot_id: str) -> str:
    """
    Stamps every Vault channel video caption with the exact uploader bot name, @username, and Bot ID
    so the admin can see inside the Vault channel which bot uploaded every file.
    (Note: End-user copyMessage in @riva18_bot passes caption='' so users still see clean captionless videos.)
    """
    import html as _html
    safe_lbl = _html.escape(str(bot_label or "Bot"))
    safe_id = _html.escape(str(bot_id or ""))
    sig = f"\n\n🤖 <b>البوت الرافع:</b> {safe_lbl} [<code>{safe_id}</code>]"
    base = (raw_caption or "").strip()
    if not base:
        return f"🤖 <b>البوت الرافع:</b> {safe_lbl} [<code>{safe_id}</code>]"
    max_base = max(50, 1024 - len(sig))
    return f"{base[:max_base]}{sig}"


def _send_bot_checkin_to_vault_sync(token: str, target_channel: str, bot_name: str, bot_username: str, bot_id: Any) -> bool:
    """Sends a signed check-in message directly FROM the worker bot's own token into the Vault channel."""
    import html as _html
    try:
        disp = f"{bot_name} (@{bot_username})" if (bot_name and bot_username) else (f"@{bot_username}" if bot_username else (bot_name or f"Bot {bot_id}"))
        msg_html = (
            f"✅ <b>اتصال بوت رفع بالخزنة</b>\n"
            f"🤖 <b>البوت:</b> {_html.escape(disp)}\n"
            f"🆔 <b>المعرف:</b> <code>{_html.escape(str(bot_id or ''))}</code>\n"
            f"📡 <b>الحالة:</b> التوكن وصلاحية النشر في الخزنة يعملان بنجاح 100% 🚀"
        )
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": str(target_channel), "text": msg_html, "parse_mode": "HTML"},
            timeout=8.0,
        ).json()
        return bool(r.get("ok"))
    except Exception:
        return False


def _verify_bot_channel_access_sync(token: str, target_channel: str) -> Tuple[bool, Optional[int], Optional[str], Optional[str], Optional[str]]:
    """Checks Bot API getMe + getChatMember on the Vault channel to confirm posting permission."""
    try:
        me_r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=6.0).json()
        if not me_r.get("ok"):
            return False, None, None, None, me_r.get("description") or "Invalid bot token"
        me = me_r["result"]
        bid = int(me["id"])
        uname = me.get("username") or ""
        fname = me.get("first_name") or ""

        cm_r = requests.get(
            f"https://api.telegram.org/bot{token}/getChatMember",
            params={"chat_id": str(target_channel), "user_id": bid},
            timeout=6.0,
        ).json()
        if cm_r.get("ok"):
            res = cm_r.get("result") or {}
            st = res.get("status", "")
            can_post = res.get("can_post_messages", True if st == "creator" else False)
            if st in ("administrator", "creator") and can_post is not False:
                return True, bid, uname, fname, None
            return False, bid, uname, fname, f"البوت موجود بالقناة بحالة '{st}' ويحتاج صلاحية النشر (Post Messages)"
        return False, bid, uname, fname, "بانتظار إضافة البوت كمشرف (Admin) بصلاحية النشر في قناة الأرشيف"
    except Exception as e:
        return False, None, None, None, str(e)[:250]


def _get_ordered_uploader_bots(explicit_bot_token: Optional[str], target_channel: str) -> List[Dict[str, Any]]:
    """
    Builds the live pool of verified Telegram uploader bots (Primary + env + PostgreSQL `uploader_bots`)
    and orders them by Least-Inflight uploads in Redis with atomic Round-Robin tie-breaking.
    """
    global _DB_POOL_CACHE, _LAST_PENDING_RECHECK_TS

    if explicit_bot_token:
        bid = explicit_bot_token.split(":")[0]
        return [{
            "token": explicit_bot_token,
            "bot_id": bid,
            "label": f"bot_{bid}",
            "is_primary": explicit_bot_token == settings.TELEGRAM_BOT_TOKEN,
        }]

    _ensure_uploader_bots_table()
    candidates: List[Dict[str, Any]] = []
    seen_ids = set()

    primary_tok = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    if primary_tok and ":" in primary_tok:
        p_id = primary_tok.split(":")[0]
        seen_ids.add(p_id)
        candidates.append({
            "token": primary_tok,
            "bot_id": p_id,
            "label": "riva (@riva18_bot — الأساسي)",
            "is_primary": True,
        })

    # Optional comma-separated env tokens
    pool_raw = os.getenv("TELEGRAM_UPLOADER_TOKENS", "").strip()
    if pool_raw:
        for t in pool_raw.split(","):
            tok = t.strip()
            if tok and ":" in tok:
                bid = tok.split(":")[0]
                if bid not in seen_ids:
                    seen_ids.add(bid)
                    candidates.append({
                        "token": tok,
                        "bot_id": bid,
                        "label": f"env_bot_{bid}",
                        "is_primary": False,
                    })

    # Dynamic PostgreSQL `uploader_bots` pool (12s RAM cache per worker process)
    now = time.monotonic()
    cached_ts, db_rows = _DB_POOL_CACHE
    if now - cached_ts > 12.0:
        try:
            with sync_engine.begin() as conn:
                rows = conn.execute(text("""
                    SELECT id, bot_token, bot_id, bot_username, bot_name, channel_ok
                    FROM uploader_bots
                    WHERE is_active = TRUE
                    ORDER BY id ASC;
                """)).mappings().all()
                db_rows = [dict(r) for r in rows]

                # Auto-activate newly promoted bots every 60s without requiring manual click
                if any(not r.get("channel_ok") for r in db_rows) and (now - _LAST_PENDING_RECHECK_TS > 60.0):
                    _LAST_PENDING_RECHECK_TS = now
                    for r in db_rows:
                        if not r.get("channel_ok"):
                            ok, bid, uname, fname, err = _verify_bot_channel_access_sync(r["bot_token"], target_channel)
                            if ok:
                                r["channel_ok"] = True
                                r["bot_id"] = bid or r.get("bot_id")
                                r["bot_username"] = uname or r.get("bot_username")
                                conn.execute(
                                    text("""
                                        UPDATE uploader_bots
                                        SET channel_ok = TRUE,
                                            bot_id = COALESCE(:bid, bot_id),
                                            bot_username = COALESCE(:un, bot_username),
                                            bot_name = COALESCE(:fn, bot_name),
                                            last_error = NULL
                                        WHERE id = :id;
                                    """),
                                    {"bid": bid, "un": uname, "fn": fname, "id": r["id"]},
                                )
                                logger.info(f"🎉 Auto-detected newly promoted helper bot @{uname or bid} in Vault channel!")
                                _send_bot_checkin_to_vault_sync(r["bot_token"], target_channel, fname or "", uname or "", bid)
            _DB_POOL_CACHE = (now, db_rows)
        except Exception as e:
            logger.warning(f"Failed reading uploader_bots pool from DB: {e}")

    for r in db_rows:
        if not r.get("channel_ok"):
            continue
        tok = (r.get("bot_token") or "").strip()
        if not tok or ":" not in tok:
            continue
        bid = tok.split(":")[0]
        if bid in seen_ids:
            continue
        seen_ids.add(bid)
        uname = (r.get("bot_username") or "").strip()
        bname = (r.get("bot_name") or "").strip()
        if bname and uname:
            lbl = f"{bname} (@{uname})"
        elif uname:
            lbl = f"@{uname}"
        else:
            lbl = bname or f"bot_{bid}"
        candidates.append({
            "token": tok,
            "bot_id": bid,
            "label": lbl,
            "is_primary": False,
        })

    if len(candidates) <= 1:
        return candidates

    # Least-Inflight + Atomic Round-Robin ordering via Redis
    r_cli = _get_sync_redis()
    if r_cli is not None:
        try:
            rr = int(r_cli.incr("riva:uploader_rr_seq") or 0)
            shift = rr % len(candidates)
            rotated = candidates[shift:] + candidates[:shift]
            keys = [f"riva:uploader_inflight:{c['bot_id']}" for c in rotated]
            vals = r_cli.mget(keys)
            for c, v in zip(rotated, vals):
                try:
                    c["inflight"] = max(0, int(v or 0))
                except (ValueError, TypeError):
                    c["inflight"] = 0
            rotated.sort(key=lambda item: item.get("inflight", 0))
            return rotated
        except Exception as e:
            logger.debug(f"Redis least-inflight sort fallback: {e}")

    import random
    shuffled = list(candidates)
    random.shuffle(shuffled)
    return shuffled


def _acquire_bot_inflight(bot_id: str) -> None:
    r_cli = _get_sync_redis()
    if r_cli is None:
        return
    try:
        key = f"riva:uploader_inflight:{bot_id}"
        val = int(r_cli.incr(key) or 1)
        if val <= 0:
            r_cli.set(key, 1, ex=1800)
        else:
            r_cli.expire(key, 1800)
    except Exception:
        pass


def _release_bot_inflight(bot_id: str) -> None:
    r_cli = _get_sync_redis()
    if r_cli is None:
        return
    try:
        key = f"riva:uploader_inflight:{bot_id}"
        r_cli.eval(
            "local v = redis.call('DECR', KEYS[1]); if v <= 0 then redis.call('DEL', KEYS[1]); return 0; end; return v;",
            1,
            key,
        )
    except Exception:
        pass


def _record_bot_upload_success(token: str, is_primary: bool, file_size_bytes: int) -> None:
    try:
        _ensure_uploader_bots_table()
        with sync_engine.begin() as conn:
            if is_primary:
                conn.execute(text("""
                    INSERT INTO bot_settings (key, value, updated_at)
                    VALUES ('primary_bot_uploads_count', '1', NOW())
                    ON CONFLICT (key) DO UPDATE
                    SET value = CAST(CAST(COALESCE(NULLIF(bot_settings.value, ''), '0') AS BIGINT) + 1 AS TEXT),
                        updated_at = NOW();
                """))
                conn.execute(
                    text("""
                        INSERT INTO bot_settings (key, value, updated_at)
                        VALUES ('primary_bot_uploaded_bytes', CAST(:sz AS TEXT), NOW())
                        ON CONFLICT (key) DO UPDATE
                        SET value = CAST(CAST(COALESCE(NULLIF(bot_settings.value, ''), '0') AS BIGINT) + :sz AS TEXT),
                            updated_at = NOW();
                    """),
                    {"sz": int(file_size_bytes or 0)},
                )
            else:
                conn.execute(
                    text("""
                        UPDATE uploader_bots
                        SET uploads_count = COALESCE(uploads_count, 0) + 1,
                            total_uploaded_bytes = COALESCE(total_uploaded_bytes, 0) + :sz,
                            last_used_at = NOW(),
                            channel_ok = TRUE,
                            last_error = NULL
                        WHERE bot_token = :tok;
                    """),
                    {"sz": int(file_size_bytes or 0), "tok": token},
                )
    except Exception as e:
        logger.debug(f"Could not update uploader bot metrics: {e}")


def _record_bot_upload_failure(
    token: str,
    is_primary: bool,
    err_msg: str,
    disable_channel_ok: bool = False,
    bot_label: str = "",
    bot_id: str = "",
    target_channel: str = "",
) -> None:
    global _DB_POOL_CACHE
    import html as _html

    if not is_primary:
        try:
            _ensure_uploader_bots_table()
            with sync_engine.begin() as conn:
                if disable_channel_ok:
                    conn.execute(
                        text("""
                            UPDATE uploader_bots
                            SET channel_ok = FALSE,
                                last_error = :err
                            WHERE bot_token = :tok;
                        """),
                        {"err": str(err_msg)[:300], "tok": token},
                    )
                    _DB_POOL_CACHE = (0.0, [])
                else:
                    conn.execute(
                        text("UPDATE uploader_bots SET last_error = :err WHERE bot_token = :tok;"),
                        {"err": str(err_msg)[:300], "tok": token},
                    )
        except Exception:
            pass

    # Notify Vault channel (deduplicated once per 20 mins per bot) if a bot hits a token/permission/upload issue
    try:
        bid = bot_id or (token.split(":")[0] if ":" in (token or "") else "unknown")
        r_cli = _get_sync_redis()
        should_alert = True
        if r_cli is not None:
            alert_key = f"riva:bot_err_alert:{bid}:{'perm' if disable_channel_ok else 'up'}"
            if not r_cli.set(alert_key, "1", nx=True, ex=1200):
                should_alert = False
        if should_alert and settings.TELEGRAM_BOT_TOKEN and (target_channel or settings.TELEGRAM_CHANNEL_ID):
            ch = str(target_channel or settings.TELEGRAM_CHANNEL_ID)
            lbl = bot_label or f"bot_{bid}"
            status_note = (
                "⛔ تم إيقاف الرفع من هذا البوت مؤقتاً لحين مراجعة التوكن أو صلاحيات الأدمن في القناة."
                if disable_channel_ok
                else "🔄 تم التحويل تلقائياً لبوت رفع آخر لاستمرار العمل بدون توقف."
            )
            alert_html = (
                f"⚠️ <b>تنبيه مشكلة في بوت رفع بالخزنة!</b>\n"
                f"🤖 <b>البوت:</b> {_html.escape(lbl)}\n"
                f"🆔 <b>المعرف:</b> <code>{_html.escape(str(bid))}</code>\n"
                f"❌ <b>السبب:</b> <code>{_html.escape(str(err_msg)[:220])}</code>\n"
                f"{status_note}"
            )
            requests.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": ch, "text": alert_html, "parse_mode": "HTML"},
                timeout=6.0,
            )
    except Exception:
        pass


def upload_via_telethon(
    file_path: Path,
    caption: str = "",
    api_id: Optional[str] = None,
    api_hash: Optional[str] = None,
    bot_token: Optional[str] = None,
    channel_id: Optional[str] = None,
    thumb_url: Optional[str] = None,
    backdrop_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Uploads video up to 2GB directly via MTProto using Telethon with:
    - Mandatory Pre-Upload & Post-Upload Inline Browser Streaming Verification
      (real w x h >= 160x90, duration >= 30s, +faststart MP4, JPEG Poster/Thumbnail cover)
    - Dynamic Multi-Bot Worker Pool (`uploader_bots`) with Redis Least-Inflight + Round-Robin balancing
    - Automatic Bot-to-Bot Failover on permission/token/heavy-FloodWait errors
    - Persistent per-worker StringSession + Parallel MTProto 512KB chunk transfer
    """
    import asyncio
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename
    from telethon.errors import FloodWaitError

    aid = int(api_id or settings.TELEGRAM_API_ID)
    ahash = api_hash or settings.TELEGRAM_API_HASH
    target_channel = channel_id or settings.TELEGRAM_CHANNEL_ID

    w, h, duration = get_video_metadata(file_path)
    if w < 160 or h < 90 or duration < 30:
        raise RuntimeError(
            f"Streaming Gate Rejected '{file_path.name}': invalid video dimensions/duration ({w}x{h}, {duration}s)."
        )

    fast_file, thumb_file = optimize_for_faststart_and_thumb(
        file_path, duration=duration, thumb_url=thumb_url, backdrop_url=backdrop_url, width=w, height=h
    )
    if fast_file != file_path:
        fw, fh, fdur = get_video_metadata(fast_file)
        if fw < 160 or fh < 90 or fdur < 30:
            if fast_file.exists():
                fast_file.unlink(missing_ok=True)
            if thumb_file and thumb_file.exists():
                thumb_file.unlink(missing_ok=True)
            raise RuntimeError(
                f"Streaming Gate Rejected remuxed '{fast_file.name}': invalid video dimensions/duration ({fw}x{fh}, {fdur}s)."
            )
        w, h, duration = fw, fh, fdur

    if not thumb_file or not thumb_file.exists() or thumb_file.stat().st_size < 200:
        if fast_file != file_path and fast_file.exists():
            fast_file.unlink(missing_ok=True)
        raise RuntimeError(f"Streaming Gate Rejected '{file_path.name}': missing valid JPEG thumbnail/poster.")

    # Hard Telegram MTProto Bot API limit: max 4000 parts x 512 KB = 2000 MiB (2,097,152,000 bytes)
    max_mtproto_bytes = 4000 * 512 * 1024
    if fast_file.stat().st_size > max_mtproto_bytes:
        oversized_mb = fast_file.stat().st_size / (1024 * 1024)
        if fast_file != file_path and fast_file.exists():
            fast_file.unlink(missing_ok=True)
        if thumb_file and thumb_file.exists():
            thumb_file.unlink(missing_ok=True)
        raise RuntimeError(
            f"Streaming Gate Rejected '{file_path.name}': file size ({oversized_mb:.1f} MiB) exceeds Telegram 2000 MiB (4000 parts) bot limit."
        )

    clean_mp4_name = fast_file.name if fast_file.name.lower().endswith(".mp4") else f"{fast_file.stem}.mp4"
    attributes = [
        DocumentAttributeVideo(
            duration=int(duration),
            w=int(w),
            h=int(h),
            supports_streaming=True,
        ),
        DocumentAttributeFilename(file_name=clean_mp4_name),
    ]

    candidates = _get_ordered_uploader_bots(bot_token, str(target_channel))
    if not candidates:
        if fast_file != file_path and fast_file.exists():
            fast_file.unlink(missing_ok=True)
        if thumb_file and thumb_file.exists():
            thumb_file.unlink(missing_ok=True)
        raise RuntimeError("No Telegram uploader bot token configured.")

    pool_size = len(candidates)
    max_bot_candidates = min(pool_size, 5)

    async def _upload_with_single_bot(cand: Dict[str, Any], has_backup_bot: bool) -> Dict[str, Any]:
        btoken = cand["token"]
        bot_id_prefix = cand["bot_id"]
        bot_label = cand["label"]
        is_primary = bool(cand.get("is_primary"))

        sess_key = f"{os.getpid()}_{bot_id_prefix}"
        sess_file = Path(f"/tmp/riva_tg_sess_{sess_key}.txt")
        cached_sess = _WORKER_SESSION_CACHE.get(sess_key)
        if not cached_sess and sess_file.exists():
            try:
                cached_sess = sess_file.read_text(encoding="utf-8").strip()
            except Exception:
                cached_sess = None

        _acquire_bot_inflight(bot_id_prefix)
        client = TelegramClient(StringSession(cached_sess) if cached_sess else StringSession(), aid, ahash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(bot_token=btoken)
                saved_str = client.session.save()
                _WORKER_SESSION_CACHE[sess_key] = saved_str
                try:
                    sess_file.write_text(saved_str, encoding="utf-8")
                except Exception:
                    pass

            chat = int(target_channel) if (str(target_channel).startswith("-100") or str(target_channel).lstrip("-").isdigit()) else str(target_channel)
            mb_size = fast_file.stat().st_size / (1024 * 1024)
            thumb_kb = thumb_file.stat().st_size / 1024.0
            logger.info(
                f"⚡ Turbo Telethon [{bot_label} | pool={pool_size}] uploading '{clean_mp4_name}' "
                f"({mb_size:.1f} MB, {w}x{h}, {duration}s, thumb={thumb_kb:.1f}KB) to {chat}..."
            )

            default_up_workers = "6" if pool_size >= 2 else "8"
            up_workers = int(os.getenv("TELEGRAM_UPLOAD_WORKERS", default_up_workers))
            vault_caption = _format_vault_caption_with_bot(caption, bot_label, bot_id_prefix)
            force_serial_upload = False

            for attempt in range(1, 4):
                try:
                    if force_serial_upload:
                        input_file = await client.upload_file(fast_file, part_size_kb=512, file_name=clean_mp4_name)
                    else:
                        input_file = await _parallel_upload_file(client, fast_file, workers=up_workers)
                    thumb_input = await client.upload_file(str(thumb_file), file_name="thumb.jpg")
                    msg = await client.send_file(
                        chat,
                        input_file,
                        caption=vault_caption[:1024],
                        parse_mode="html",
                        thumb=thumb_input,
                        mime_type="video/mp4",
                        force_document=False,
                        attributes=attributes,
                        supports_streaming=True,
                    )

                    # Mandatory Post-Upload Verification: confirm Telegram stored real dimensions + thumbnail + streaming!
                    doc = getattr(getattr(msg, "media", None), "document", None)
                    doc_thumbs = len(getattr(doc, "thumbs", None) or []) if doc else 0
                    vid_attr = next(
                        (a for a in (getattr(doc, "attributes", None) or []) if isinstance(a, DocumentAttributeVideo)),
                        None,
                    )
                    vw = getattr(vid_attr, "w", 0) if vid_attr else 0
                    vh = getattr(vid_attr, "h", 0) if vid_attr else 0
                    vdur = getattr(vid_attr, "duration", 0) if vid_attr else 0
                    vstream = bool(getattr(vid_attr, "supports_streaming", False)) if vid_attr else False

                    if vw < 160 or vh < 90 or vdur < 30 or doc_thumbs < 1 or not vstream:
                        logger.error(
                            f"❌ Post-upload verification FAILED on msg {msg.id} "
                            f"(w={vw}, h={vh}, dur={vdur}, thumbs={doc_thumbs}, stream={vstream}). Deleting broken message!"
                        )
                        try:
                            await client.delete_messages(chat, [msg.id])
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"Post-upload inline streaming verification failed (w={vw}, h={vh}, dur={vdur}, thumbs={doc_thumbs})"
                        )

                    file_size = msg.file.size if msg.file else fast_file.stat().st_size
                    _record_bot_upload_success(btoken, is_primary, int(file_size))
                    logger.info(
                        f"✅ Verified Inline-Streamable Upload via {bot_label}! Msg ID: {msg.id} | {vw}x{vh} | {vdur}s | thumbs={doc_thumbs} | stream={vstream}"
                    )
                    return {
                        "channel_id": str(target_channel),
                        "message_id": msg.id,
                        "telegram_file_id": f"msg_{msg.id}",
                        "telegram_file_unique_id": str(msg.id),
                        "file_size_bytes": file_size,
                        "caption": caption,
                        "uploader_bot": bot_label,
                    }
                except FloodWaitError as fw:
                    if has_backup_bot and int(fw.seconds) > 35:
                        raise RuntimeError(f"Bot {bot_label} hit {fw.seconds}s FloodWait; failing over to next bot") from fw
                    wait_s = int(fw.seconds) + 3
                    logger.warning(f"⏳ Telegram FloodWait ({fw.seconds}s) on {bot_label} attempt {attempt}. Sleeping {wait_s}s safely...")
                    await asyncio.sleep(wait_s)
                except Exception as send_err:
                    if "filepartsinvalid" in f"{type(send_err).__name__}: {send_err}".lower() and attempt < 3:
                        logger.warning(f"⚠️ {bot_label} hit FilePartsInvalidError on attempt {attempt}; retrying with clean serial 512KB upload...")
                        force_serial_upload = True
                        await asyncio.sleep(1.5)
                        continue
                    raise
            raise RuntimeError(f"Exceeded FloodWait retry attempts on {bot_label}")
        except Exception as bot_err:
            err_l = f"{type(bot_err).__name__}: {bot_err}".lower()
            perm_keywords = (
                "chatadminrequired",
                "chatwriteforbidden",
                "channelprivate",
                "userbannedinchannel",
                "accesstoken",
                "unauthorized",
                "authkeyunregistered",
                "could not find the input entity",
            )
            is_perm_err = any(k in err_l for k in perm_keywords)
            if is_perm_err:
                _WORKER_SESSION_CACHE.pop(sess_key, None)
                try:
                    sess_file.unlink(missing_ok=True)
                except Exception:
                    pass
            is_file_oversized = "filepartsinvalid" in err_l
            if not is_file_oversized:
                _record_bot_upload_failure(
                    btoken,
                    is_primary,
                    f"{type(bot_err).__name__}: {bot_err}",
                    disable_channel_ok=is_perm_err,
                    bot_label=bot_label,
                    bot_id=str(bot_id_prefix),
                    target_channel=str(target_channel),
                )
            raise
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            _release_bot_inflight(bot_id_prefix)

    async def _runner():
        last_exc = None
        for idx, cand in enumerate(candidates[:max_bot_candidates]):
            has_backup = idx < (max_bot_candidates - 1)
            try:
                return await _upload_with_single_bot(cand, has_backup_bot=has_backup)
            except Exception as exc:
                last_exc = exc
                if "Post-upload inline streaming verification failed" in str(exc) or "filepartsinvalid" in f"{type(exc).__name__}: {exc}".lower():
                    raise
                if has_backup:
                    logger.warning(
                        f"⚠️ Uploader bot {cand['label']} failed ({exc}); automatically failing over to {candidates[idx + 1]['label']}!"
                    )
                    continue
                raise
        raise last_exc

    try:
        return asyncio.run(_runner())
    finally:
        if fast_file != file_path and fast_file.exists():
            fast_file.unlink(missing_ok=True)
        if thumb_file and thumb_file.exists():
            thumb_file.unlink(missing_ok=True)


def upload_video_to_vault(
    file_path: Path,
    caption: str = "",
    thumb_url: Optional[str] = None,
    backdrop_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Smart router: Uses Telethon MTProto (2GB limit) if API credentials are configured,
    or falls back to Bot API HTTP (50MB limit). Always enforces inline streaming dimensions + poster thumb.
    """
    if settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH and settings.TELEGRAM_BOT_TOKEN:
        try:
            return upload_via_telethon(
                file_path,
                caption=caption,
                thumb_url=thumb_url,
                backdrop_url=backdrop_url,
            )
        except Exception as exc:
            if "Streaming Gate Rejected" in str(exc) or "Post-upload inline streaming verification failed" in str(exc):
                raise
            if file_path.exists() and file_path.stat().st_size > 49 * 1024 * 1024 and not settings.TELEGRAM_LOCAL_API_URL:
                raise RuntimeError(f"Telethon MTProto upload failed for >50MB file: {exc}") from exc
            logger.warning(f"Telethon upload attempt failed, falling back to Bot API: {exc}")

    return upload_to_telegram_bot_api(
        file_path,
        caption=caption,
        thumb_url=thumb_url,
        backdrop_url=backdrop_url,
    )


def record_telegram_archive(
    telegram_data: Dict[str, Any],
    episode_id: Optional[int] = None,
    movie_id: Optional[int] = None
) -> Optional[TelegramArchive]:
    """
    Saves or updates TelegramArchive record in PostgreSQL/SQLite.
    Guarantees Data Quality: Rejects any fake, tiny, or corrupted files (< 15MB).
    """
    file_size = telegram_data.get("file_size_bytes") or 0
    if file_size < 15 * 1024 * 1024:
        logger.warning(f"Quality Gate Rejected: file_size={file_size} bytes is under 15MB threshold for ep={episode_id}, movie={movie_id}")
        return None

    with SyncSessionLocal() as session:
        existing = None
        if movie_id is not None:
            existing = session.query(TelegramArchive).filter_by(movie_id=movie_id).first()
        elif episode_id is not None:
            existing = session.query(TelegramArchive).filter_by(episode_id=episode_id).first()

        if existing:
            existing.channel_id = telegram_data["channel_id"]
            existing.message_id = telegram_data["message_id"]
            existing.telegram_file_id = telegram_data["telegram_file_id"]
            existing.telegram_file_unique_id = telegram_data.get("telegram_file_unique_id")
            existing.file_size_bytes = telegram_data.get("file_size_bytes")
            existing.caption = telegram_data.get("caption")
            session.commit()
            session.refresh(existing)
            logger.info(f"Updated existing TelegramArchive ID {existing.id} for episode={episode_id}, movie={movie_id}")
            return existing

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


_sent_notifications_cache: Dict[str, float] = {}


def send_telegram_notification(text: str, level: str = "info", cooldown_seconds: int = 1800) -> bool:
    """
    Sends an instant notification or error alert to the configured Telegram channel/group.
    Includes smart deduplication cache to prevent alert flooding.
    """
    import time
    now = time.time()

    # Deduplicate warning/ban/error messages so identical alerts aren't spammed
    if level in ["ban", "warning", "error"]:
        last_sent = _sent_notifications_cache.get(text, 0)
        if now - last_sent < cooldown_seconds:
            logger.debug(f"Suppressed duplicate Telegram notification (cooldown {cooldown_seconds}s): {text[:60]}")
            return False
        _sent_notifications_cache[text] = now

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
