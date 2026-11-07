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

---

## 3. Vault Architecture — Cold Storage + Delivery Vaults

> **CRITICAL — Every AI model and agent MUST understand and enforce this architecture.**

### 3.1 Roles

| Vault | Channel ID | Purpose | Who uploads here |
|-------|-----------|---------|-----------------|
| **Master Vault** (Cold Storage) | `-1004466886745` ("Riva Vault") | Permanent archive — raw originals land here. NOT served to users directly. | Worker bots (`@Riva109_bot`, `@Riva4_bot`, `@Riva03_bot`, `@Rivaa1_bot`) |
| **Delivery Vault(s)** (Backup) | `-1004488480136` ("Riva Vaulte2") + any future channels | Zero-cost mirror copies served to end-users via `@Rivaa1_bot`. | Never uploaded directly — only via `copyMessage` from Master Vault |

### 3.2 Upload Flow (always)

```
Worker downloads video
       ↓
Uploads to Master Vault (-1004466886745)
       ↓  record_telegram_archive() → TelegramArchive row
       ↓
_mirror_archive_to_backup()  ← automatic, zero-bandwidth
  → copyMessage to EACH channel in BACKUP_VAULT_CHANNEL_IDS
  → creates a TelegramArchive row per delivery vault
       ↓
Delivery bot (@Rivaa1_bot) serves users FROM delivery vaults only
```

### 3.3 Key Rules for Any Future Agent

1. **Never upload directly to a delivery vault.** Only `copyMessage` from the Master Vault.
2. **Never delete cross-channel copies.** The dedup query in `link_auditor.py` MUST include `AND ta1.channel_id = ta2.channel_id` — only delete duplicates *within the same channel*.
3. **Master Vault rows must never be purged.** They are the originals. If `link_auditor` ever deletes a primary vault row, it is a bug — fix immediately.
4. **To add a new delivery vault:** Add its channel ID to `BACKUP_VAULT_CHANNEL_IDS` (comma-separated), add `@Rivaa1_bot` as admin (Post Messages) in that channel, then run `vault_mirror_backfill.py` to sync all existing archives.
5. **`copyMessage` not `forwardMessage`:** Always use `copyMessage` to avoid channel-link watermarks.

### 3.4 Config Variables

```env
# Master Vault (cold storage — uploads land here)
TELEGRAM_CHANNEL_ID=-1004466886745
TELEGRAM_BOT_TOKEN=<worker-bot-token>

# Delivery Vaults — comma-separated list; bot must be admin in each
BACKUP_VAULT_CHANNEL_IDS=-1004488480136          # add more: ,-1009999999999
BACKUP_VAULT_BOT_TOKEN=9071347693:AAHkipyXFwJcW7pGuBot2o3PJoPMCDKtEHU  # @Riva109_bot
```

### 3.5 Bots Reference

| Bot username | Bot ID | Token (last 6) | Role |
|---|---|---|---|
| `@riva108_bot` | 9110792514 | `…IddpsI` | Main menu / UI bot |
| `@Rivaa1_bot` | 9107119612 | `…VtXeuPc` | Delivery bot (serves videos to users) — must be admin in ALL delivery vaults |
| `@Riva109_bot` | 9071347693 | `…CDKtEHU` | Mirror/worker bot — admin in Master Vault AND all delivery vaults |

### 3.6 Key Files

| File | Purpose |
|------|---------|
| [`worker/telegram_vault.py`](worker/telegram_vault.py) | Upload logic + `_mirror_archive_to_backup()` — mirrors to ALL delivery vaults after every upload |
| [`worker/vault_mirror_backfill.py`](worker/vault_mirror_backfill.py) | One-shot backfill: syncs all missing archives to ALL delivery vaults |
| [`worker/link_auditor.py`](worker/link_auditor.py) | Dedup cleaner — MUST filter by `channel_id` to never delete cross-vault copies |
| [`bot/delivery_bot.py`](bot/delivery_bot.py) | Serves videos to users from delivery vaults via `copyMessage` |
| [`app/config.py`](app/config.py) | Settings — `BACKUP_VAULT_CHANNEL_IDS`, `BACKUP_VAULT_BOT_TOKEN` |

### 3.7 VPS Deployment Notes

- Files edited locally must be deployed:
  ```bash
  scp worker/telegram_vault.py root@srv1867849.hstgr.cloud:/root/riva/worker/
  docker cp /root/riva/worker/telegram_vault.py riva_worker:/app/worker/
  ```
- `${VAR}` interpolation in `docker-compose.yml` is unreliable — hardcode sensitive values directly in the compose file or use `env_file:` directive.
- After any container restart, re-verify files inside containers: `docker exec riva_worker head -5 /app/worker/telegram_vault.py`

