# Riva Cinema — Standing Project Rules (`GEMINI.md`)

## 1. Mandatory Inline Video Streaming in Telegram & Telegram Web Browser (`أساسي في أي رفع`)
Every video uploaded to the Telegram Master Vault (via [`worker/pipeline.py`](worker/pipeline.py), [`worker/telegram_vault.py`](worker/telegram_vault.py), or any manual/VIP script) **MUST** stream directly inline in the chat bubble on **Telegram Web Browser**, Desktop, and Mobile without entering Media:
- **Verified Real Dimensions & Duration:** Probe real video codecs (`h264`, `hevc`, `vp9`, `av1`, `mpeg4`), reject disguised image codecs (`png`, `mjpeg`, `gif`, `webp`), and enforce `w >= 160`, `h >= 90`, `duration >= 30`. Never upload `1x1` or `0s` files.
- **Strip Disguised HLS Segment Headers:** Unencrypted HLS `.ts` segments from CDNs (e.g., Vibuxer / TikTokCDN `.image` segments) may prepend a PNG/JPEG/GIF header before the `0x47` 188-byte MPEG-TS stream. Always strip bytes before `0x47` sync (`b[i]==0x47 and b[i+188]==0x47 and b[i+376]==0x47`) prior to `ffmpeg` concat, and mux with `-map 0:v:0 -map 0:a:0? -dn -movflags +faststart`.
- **Mandatory Poster / Thumbnail (`thumb` + DB `poster_path`):**
  - Attach a valid `yuvj420p` JPEG `thumb` (`width <= 320` AND `height <= 320`, `< 200 KB`) using the official TMDB backdrop/poster composited to the video's `w:h` aspect ratio (with mid-video frame fallback) so `document.thumbs` is NEVER empty.
  - Ensure `Movie.poster_path` / `Series.poster_path` is populated in PostgreSQL so `@riva18_bot` sends the official Poster card above the video.
- **Explicit Streaming Attributes:** Always send with `mime_type="video/mp4"`, `force_document=False`, `supports_streaming=True`, and `DocumentAttributeVideo(duration=int(duration), w=int(w), h=int(h), supports_streaming=True)`.

## 2. Telegram Vault Only & Strict Ordering
- **Telegram Vault Only:** Keep `ENABLE_EXTERNAL_STREAM_UPLOAD=0` unless explicitly instructed otherwise.
- **Sequential Ordering:** Always upload series episodes (`S01E01 -> S01E02 -> ...`) and multi-part movie franchises (`Part 1 -> Part 2 -> ...`) in ascending order, prioritizing popularity (`الأكثر شهرة`) across lists and search.
- **Movies, Series & Anime TMDB Priority Policy:** Interleave parallel upload lanes across **Movies**, **Series**, and **Anime** (`MOVIE_VIRAL_PRIORITY_ENABLED=1`, `SERIES_VIRAL_PRIORITY_ENABLED=1`) prioritizing: (1) VIP user requests, (2) Live TMDB Trending & Current-Period Most Viral works, (3) All-Time Highest-Rated TMDB masterpieces (including dedicated Anime Movie & Anime Series lanes), and (4) completing in-progress series & franchises cleanly without gaps.

