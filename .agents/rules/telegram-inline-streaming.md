---
trigger: always_on
description: Mandatory inline Telegram & Web Browser video streaming metadata (real dimensions w x h, duration, poster/thumbnail cover, +faststart MP4, supports_streaming=True) for ALL uploads.
---

# Mandatory Telegram Inline Browser Video Streaming Rule

Every single video uploaded to Telegram (whether via `worker/pipeline.py`, `worker/telegram_vault.py`, or any custom/manual upload script) **MUST** play directly inline inside **Telegram Web Browser (`web.telegram.org`)**, Desktop, and Mobile without requiring the user to open the Media gallery or download the file externally.

## Non-Negotiable Pre-Upload & Upload Requirements

1. **Real Video Stream & Dimensions (`w`, `h`, `duration`):**
   - Inspect video streams via `ffprobe` (`-select_streams v -show_entries stream=codec_name,width,height,duration`).
   - Ignore/reject disguised image streams (`png`, `mjpeg`, `gif`, `webp`, `bmp`) and enforce **`w >= 160`**, **`h >= 90`**, and **`duration >= 30`**. Never default broken probes to fake dimensions or upload `1x1` / `duration=0` files.
2. **Disguised HLS `.ts` Segment Stripping:**
   - CDNs such as Vibuxer / TikTokCDN prepend small `1x1` PNG/GIF/JPEG headers before the real MPEG-TS payload.
   - For unencrypted HLS segments where `body[0] != 0x47`, always scan for valid 188-byte MPEG-TS sync (`body[i] == 0x47 and body[i+188] == 0x47 and body[i+376] == 0x47`) and slice `body[i:]` before writing/concatenating segments.
   - Always mux with `-map 0:v:0 -map 0:a:0? -dn -c copy -bsf:a aac_adtstoasc -movflags +faststart`.
3. **Poster / Thumbnail Cover (`thumb` / `البوست`):**
   - Every uploaded video **MUST** include a valid baseline JPEG (`yuvj420p`) thumbnail (`<= 320px` on BOTH width and height, `< 200 KB`) matching the video's aspect ratio (`thumbs >= 1` on the resulting Telegram `Document`).
   - Prefer the official movie/series TMDB backdrop or blurred-backdrop + centered poster cover (`thumb_url` / `backdrop_url`), falling back to a bright mid-video frame (`min(180, duration // 5)`) if no poster URL is available.
   - Ensure the `Movie` / `Series` database row also has `poster_path` and Arabic `overview` enriched so the Bot's Poster card above the video is always populated.
4. **Telegram MTProto / Bot API Streaming Attributes:**
   - Always pass `mime_type="video/mp4"`, `force_document=False`, `supports_streaming=True`, `thumb=...`, and `DocumentAttributeVideo(duration=int(duration), w=int(w), h=int(h), supports_streaming=True)` (plus `DocumentAttributeFilename(file_name="...mp4")`).
5. **Standing Pipeline Constraints:**
   - Keep `ENABLE_EXTERNAL_STREAM_UPLOAD=0` (upload to Telegram Master Vault ONLY; never upload to external streaming hosts).
   - Maintain strict sequential ordering for series episodes (`S01E01 -> S01E02 -> ...`) and multi-part movie franchises (`Part 1 -> Part 2 -> ...`).

