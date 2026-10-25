"""
Riva High-Performance Telegram Video Streaming Engine.
Provides HTTP 206 Partial Content (Byte-Range) streaming directly from Telegram MTProto,
allowing browsers, mobile apps, and video players to seek and stream smoothly with zero buffering.
"""
import os
import re
import asyncio
import logging
from typing import Optional, Tuple, AsyncGenerator
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from telethon import TelegramClient
from telethon.sessions import MemorySession

from app.config import settings

logger = logging.getLogger("riva_streamer")

class TelegramStreamer:
    """Singleton connection pool to stream media directly from Telegram MTProto."""
    _instance: Optional["TelegramStreamer"] = None
    _client: Optional[TelegramClient] = None
    _lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "TelegramStreamer":
        if cls._instance is None:
            cls._instance = TelegramStreamer()
        return cls._instance

    async def get_client(self) -> TelegramClient:
        if self._client is None or not self._client.is_connected():
            async with self._lock:
                if self._client is None or not self._client.is_connected():
                    if not (settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH and settings.TELEGRAM_BOT_TOKEN):
                        raise RuntimeError("Telegram credentials are not configured in settings.")
                    
                    aid = int(settings.TELEGRAM_API_ID)
                    ahash = settings.TELEGRAM_API_HASH
                    btoken = settings.TELEGRAM_BOT_TOKEN
                    
                    logger.info("Initializing persistent Telethon client for streaming...")
                    client = TelegramClient(MemorySession(), aid, ahash)
                    await client.start(bot_token=btoken)
                    self._client = client
                    logger.info("Telethon client ready for streaming requests.")
        return self._client

    async def stream_telegram_message(
        self,
        channel_id: int | str,
        message_id: int,
        range_header: Optional[str] = None
    ) -> StreamingResponse:
        client = await self.get_client()
        chat = int(channel_id) if (str(channel_id).startswith("-100") or str(channel_id).lstrip("-").isdigit()) else str(channel_id)

        try:
            msg = await client.get_messages(chat, ids=message_id)
        except Exception as e:
            logger.error(f"Failed to fetch Telegram message {message_id} from {chat}: {e}")
            raise HTTPException(status_code=404, detail="Video file not found in vault")

        if not msg or not msg.file:
            raise HTTPException(status_code=404, detail="Media file not found in Telegram message")

        total_size = msg.file.size
        mime_type = msg.file.mime_type or "video/mp4"

        # Parse HTTP Range header (e.g. "bytes=1048576-" or "bytes=0-1048575")
        start = 0
        end = total_size - 1
        is_range = False

        if range_header:
            range_match = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
            if range_match:
                is_range = True
                start = int(range_match.group(1))
                if range_match.group(2):
                    end = int(range_match.group(2))
                    if end >= total_size:
                        end = total_size - 1

        if start >= total_size or end < start:
            raise HTTPException(
                status_code=416,
                detail="Requested Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{total_size}"}
            )

        content_length = end - start + 1

        # Generator yielding chunks from Telegram MTProto safely bounded to content_length
        async def file_chunk_generator() -> AsyncGenerator[bytes, None]:
            bytes_left = content_length
            chunk_size = 256 * 1024
            try:
                async for chunk in client.iter_download(
                    msg.media,
                    offset=start,
                    limit=content_length,
                    chunk_size=chunk_size,
                    request_size=chunk_size
                ):
                    if not chunk or bytes_left <= 0:
                        break
                    if len(chunk) > bytes_left:
                        chunk = chunk[:bytes_left]
                    bytes_left -= len(chunk)
                    yield chunk
            except Exception as stream_err:
                logger.warning(f"Client disconnected or stream interrupted: {stream_err}")

        headers = {
            "Content-Type": mime_type,
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Range": f"bytes {start}-{end}/{total_size}",
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="{msg.file.name or f"video_{message_id}.mp4"}"'
        }

        status_code = 206 if is_range else 200
        return StreamingResponse(
            file_chunk_generator(),
            status_code=status_code,
            headers=headers
        )


def render_html5_player(
    title: str,
    stream_url: str,
    poster_url: Optional[str] = None,
    subtitle: Optional[str] = None
) -> str:
    """Renders a sleek, dark-themed, zero-ad HTML5 player powered by Plyr."""
    poster_attr = f'data-poster="{poster_url}"' if poster_url else ""
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - Riva Cloud Player</title>
    <link rel="stylesheet" href="https://cdn.plyr.io/3.7.8/plyr.css" />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;900&display=swap" rel="stylesheet">
    <style>
        :root {{
            --plyr-color-main: #6366f1;
            --plyr-video-background: #09090b;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #09090b;
            color: #f4f4f5;
            font-family: 'Cairo', -apple-system, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }}
        .player-wrapper {{
            width: 100vw;
            height: 100vh;
            position: relative;
            background: #000;
        }}
        .header-bar {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            padding: 20px 30px;
            background: linear-gradient(180deg, rgba(0,0,0,0.85) 0%, transparent 100%);
            z-index: 10;
            display: flex;
            justify-content: space-between;
            align-items: center;
            pointer-events: none;
            transition: opacity 0.3s ease;
        }}
        .badge {{
            background: rgba(99, 102, 241, 0.25);
            border: 1px solid rgba(99, 102, 241, 0.5);
            color: #a5b4fc;
            padding: 4px 12px;
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.5px;
            pointer-events: auto;
        }}
        .title-box {{
            text-align: right;
            pointer-events: auto;
        }}
        .title-box h1 {{
            font-size: 20px;
            font-weight: 700;
            text-shadow: 0 2px 8px rgba(0,0,0,0.8);
        }}
        .title-box p {{
            font-size: 13px;
            color: #a1a1aa;
        }}
        video {{
            width: 100%;
            height: 100%;
            object-fit: contain;
        }}
    </style>
</head>
<body>
    <div class="player-wrapper">
        <div class="header-bar" id="headerBar">
            <span class="badge">⚡ Riva VIP Stream (Direct)</span>
            <div class="title-box">
                <h1>{title}</h1>
                {f"<p>{subtitle}</p>" if subtitle else ""}
            </div>
        </div>
        <video id="player" playsinline controls {poster_attr}>
            <source src="{stream_url}" type="video/mp4" />
        </video>
    </div>

    <script src="https://cdn.plyr.io/3.7.8/plyr.polyfilled.js"></script>
    <script>
        const player = new Plyr('#player', {{
            autoplay: false,
            seekTime: 10,
            controls: [
                'play-large', 'restart', 'rewind', 'play', 'fast-forward',
                'progress', 'current-time', 'duration', 'mute', 'volume',
                'captions', 'settings', 'pip', 'airplay', 'fullscreen'
            ],
            settings: ['speed'],
            speed: {{ selected: 1, options: [0.75, 1, 1.25, 1.5, 2] }}
        }});

        // Auto hide header bar on playing
        const header = document.getElementById('headerBar');
        player.on('play', () => {{
            setTimeout(() => {{ if (player.playing) header.style.opacity = '0'; }}, 3000);
        }});
        player.on('pause', () => {{ header.style.opacity = '1'; }});
        player.on('seeking', () => {{ header.style.opacity = '1'; }});
    </script>
</body>
</html>
"""
