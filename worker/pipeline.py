import html
import os
import re
import shutil
import logging
import tempfile
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
import httpx
import requests
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.orm import sessionmaker, joinedload
from sqlalchemy.dialects.postgresql import insert
import redis

from app.config import settings
from app.models import Movie, Series, Season, Episode, StreamingLink, TelegramArchive
from worker.celery_app import celery_app

logger = logging.getLogger("riva_pipeline")
logging.basicConfig(level=logging.INFO)

# Sync database session for Celery workers
sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)

# Sync Redis client for worker cache invalidation
redis_sync = redis.from_url(settings.REDIS_URL, decode_responses=True)

# Minimum free disk space required on VPS to start a download job (10 GB)
MIN_FREE_DISK_BYTES = 10 * 1024 * 1024 * 1024

# Strictly disable external streaming host uploads/clones unless explicitly opted in (100% Telegram Vault mode)
ENABLE_EXTERNAL_STREAM_UPLOAD = os.getenv("ENABLE_EXTERNAL_STREAM_UPLOAD", "0") == "1"


_CATEGORY_AR_LABELS = {
    "arabic": "عربي 🇪🇬",
    "turkish": "تركي 🇹🇷",
    "western": "أجنبي 🇺🇸",
    "asian": "آسيوي 🇰🇷",
    "kdrama": "كوري/آسيوي 🇰🇷",
    "anime": "أنمي 🎌",
}


def format_movie_alert_details(movie_id: int) -> str:
    """Builds human-readable HTML details (title, original title, year, category, source URL) for admin alerts."""
    try:
        with SyncSessionLocal() as session:
            m = session.query(Movie).filter_by(id=movie_id).first()
            if not m:
                return f"🎬 <b>الفيلم:</b> غير موجود في قاعدة البيانات | <b>Movie ID:</b> {movie_id}"
            title = (m.title or f"Movie #{movie_id}").strip()
            orig = (m.original_title or "").strip()
            full_name = f"{title} — {orig}" if orig and orig.lower() != title.lower() else title
            yr_str = f" ({m.year})" if m.year else ""
            cat_ar = _CATEGORY_AR_LABELS.get((m.category or "").lower(), m.category or "غير محدد")
            src_raw = urllib.parse.unquote(m.source_url or "").strip()
            src_line = f"\n🔗 <b>المصدر الحالي:</b> {html.escape(src_raw)}" if src_raw else ""
            return (
                f"🎬 <b>الفيلم:</b> {html.escape(full_name)}{yr_str}\n"
                f"🏷 <b>التصنيف:</b> {html.escape(cat_ar)} | <b>Movie ID:</b> {movie_id}"
                f"{src_line}"
            )
    except Exception:
        return f"🎬 <b>Movie ID:</b> {movie_id}"


def format_episode_alert_details(episode_id: int) -> str:
    """Builds human-readable HTML details (series name, season, episode, category, source URL) for admin alerts."""
    try:
        with SyncSessionLocal() as session:
            ep = (
                session.query(Episode)
                .options(joinedload(Episode.season).joinedload(Season.series))
                .filter_by(id=episode_id)
                .first()
            )
            if not ep:
                return f"📺 <b>الحلقة:</b> محذوفة أو غير موجودة | <b>Episode ID:</b> {episode_id}"
            sea = ep.season
            ser = sea.series if sea else None
            s_title = (ser.title if ser and ser.title else "مسلسل غير معروف").strip()
            s_orig = (ser.original_title if ser and ser.original_title else "").strip()
            full_series = f"{s_title} — {s_orig}" if s_orig and s_orig.lower() != s_title.lower() else s_title
            yr_str = f" ({ser.year})" if (ser and ser.year) else ""
            sea_num = sea.season_number if sea else 1
            ep_num = ep.episode_number
            s_id = ser.id if ser else "?"
            cat_ar = _CATEGORY_AR_LABELS.get((ser.category if ser and ser.category else "").lower(), (ser.category if ser else "غير محدد") or "غير محدد")
            src_raw = urllib.parse.unquote(ep.source_url or (ser.source_url if ser else "") or "").strip()
            src_line = f"\n🔗 <b>المصدر الحالي:</b> {html.escape(src_raw)}" if src_raw else ""
            return (
                f"📺 <b>المسلسل:</b> {html.escape(full_series)}{yr_str}\n"
                f"📌 <b>الموسم:</b> {sea_num} | <b>الحلقة:</b> {ep_num} (<code>S{int(sea_num):02d}E{int(ep_num):02d}</code>)\n"
                f"🏷 <b>التصنيف:</b> {html.escape(cat_ar)} | <b>Series ID:</b> {s_id} | <b>Episode ID:</b> {episode_id}"
                f"{src_line}"
            )
    except Exception:
        return f"📺 <b>Episode ID:</b> {episode_id}"


def _is_transient_download_error(err: Exception) -> bool:
    """Returns True for network timeouts / transient resets so live mirrors aren't falsely marked 'dead'."""
    import subprocess
    if isinstance(err, subprocess.TimeoutExpired):
        return True
    msg = str(err).lower()
    if re.search(r"\bhttp\s+(?:404|410)\b", msg) or any(
        fatal in msg for fatal in ("unresolved host landing", "returned html", "invalid file", "segment 0 rejected")
    ):
        return False
    return any(tok in msg for tok in ("timed out", "timeout", "curl: (28)", "connection reset", "temporarily unavailable", "http 503", "http 429"))


def check_disk_space_available(directory: str, required_bytes: int = MIN_FREE_DISK_BYTES) -> bool:
    """Verifies that the VPS transit disk has sufficient capacity before beginning."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    total, used, free = shutil.disk_usage(directory)
    if free < required_bytes:
        logger.error(f"Insufficient disk space in {directory}: {free / (1024**3):.2f} GB free, need {required_bytes / (1024**3):.2f} GB")
        return False
    return True


def is_valid_video_file(file_path: Path, min_bytes: int = 15 * 1024 * 1024) -> bool:
    """Verifies that the file exists, meets minimum size, is not HTML, and contains a real streamable video track."""
    if not file_path or not file_path.exists():
        return False
    if file_path.stat().st_size < min_bytes:
        return False
    try:
        with open(file_path, "rb") as f:
            header = f.read(256)
            h_lower = header.lower()
            if b"<html" in h_lower or b"<!doctype" in h_lower or b"<head" in h_lower or b"<script" in h_lower:
                return False
        from worker.telegram_vault import get_video_metadata
        w, h, dur = get_video_metadata(file_path)
        if w < 160 or h < 90 or dur < 30:
            logger.warning(f"Rejected non-streamable video file {file_path.name}: w={w}, h={h}, dur={dur}s")
            return False
        return True
    except Exception:
        return False


def mark_stream_link_dead(url: str, reason: str = "failed"):
    """Marks a streaming link as dead so future crawler and dispatcher cycles skip it."""
    if not url:
        return
    try:
        clean_url = url.strip()
        u_no_slash = clean_url.rstrip("/")
        u_with_slash = u_no_slash + "/"
        with SyncSessionLocal() as session:
            res = session.execute(
                text("""
                    UPDATE streaming_links
                    SET status = 'dead', last_checked_at = now()
                    WHERE status != 'dead'
                      AND (
                          embed_url IN (:u1, :u2, :u3)
                          OR direct_url IN (:u1, :u2, :u3)
                      );
                """),
                {"u1": clean_url, "u2": u_no_slash, "u3": u_with_slash}
            )
            session.commit()
            if res.rowcount > 0:
                logger.info(f"🚫 Marked {res.rowcount} link(s) as DEAD in DB: {clean_url[:65]} (Reason: {reason})")
    except Exception as e:
        logger.warning(f"Could not mark link as dead: {e}")


def rotate_candidates_by_provider(candidate_urls: List[str]) -> List[str]:
    """
    Groups candidates by domain/provider and interleaves them
    so we alternate fast healthy mirrors instead of retrying a single failing server.
    """
    from collections import defaultdict
    from urllib.parse import urlparse
    grouped = defaultdict(list)
    for u in candidate_urls:
        if not u:
            continue
        domain = urlparse(u).netloc.lower().replace("www.", "")
        grouped[domain].append(u)

    priority_order = [
        "cdnplus", "morencius", "vidoba", "mp4plus", "1vid", "anafast", "vidspeed", "liiivideo",
        "ok.ru", "vkvideo", "vk.ru", "vk.com",
        "playmogo", "dsvplay", "dood", "myvidplay", "cloudatacdn",
        "mxdrop", "mixdrop", "mxcontent",
        "streamtape", "uqload",
        "vibuxer", "hgcloud", "hanerix", "streamwish", "vidhide", "byseraguci",
    ]
    sorted_domains = sorted(
        grouped.keys(),
        key=lambda d: next((i for i, p in enumerate(priority_order) if p in d), 99)
    )

    rotated = []
    max_len = max((len(lst) for lst in grouped.values()), default=0)
    for i in range(max_len):
        for domain in sorted_domains:
            if i < len(grouped[domain]):
                rotated.append(grouped[domain][i])
    return rotated


def clone_doodstream_file(file_code: str, api_key: str) -> Optional[Dict[str, str]]:
    """Instant server-to-server clone on DoodStream cloud (disabled when ENABLE_EXTERNAL_STREAM_UPLOAD=0)."""
    if not ENABLE_EXTERNAL_STREAM_UPLOAD:
        return None
    try:
        url = f"https://doodapi.co/api/file/clone?key={api_key}&file_code={file_code}"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                res_json = resp.json()
                if res_json.get("status") == 200 and res_json.get("result"):
                    res_data = res_json["result"]
                    new_code = res_data.get("filecode")
                    return {
                        "provider": "doodstream",
                        "file_code": new_code,
                        "embed_url": res_data.get("embed_url") or f"https://doodstream.com/e/{new_code}",
                        "direct_url": res_data.get("download_url") or f"https://doodstream.com/d/{new_code}"
                    }
    except Exception as e:
        logger.warning(f"DoodStream clone API failed for {file_code}: {e}")
    return None


def clone_streamtape_file(file_id: str, login: str, key: str) -> Optional[Dict[str, str]]:
    """Instant server-to-server clone on Streamtape cloud (disabled when ENABLE_EXTERNAL_STREAM_UPLOAD=0)."""
    if not ENABLE_EXTERNAL_STREAM_UPLOAD:
        return None
    try:
        url = "https://api.streamtape.com/file/clone"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params={"login": login, "key": key, "file_id": file_id})
            if resp.status_code == 200:
                res_json = resp.json()
                if res_json.get("status") == 200 and res_json.get("result"):
                    res_data = res_json["result"]
                    new_id = res_data.get("id")
                    return {
                        "provider": "streamtape",
                        "file_code": new_id,
                        "embed_url": f"https://streamtape.com/e/{new_id}",
                        "direct_url": f"https://streamtape.com/v/{new_id}"
                    }
    except Exception as e:
        logger.warning(f"Streamtape clone API failed for {file_id}: {e}")
    return None


def _strip_disguised_ts_prefix(data: bytes) -> bytes:
    """
    Strips disguised image/junk headers (e.g. 70-byte 1x1 PNG on Vibuxer/TikTokCDN .image segments,
    GIF/JPEG headers, or leading ID3 tags) prepended before the real 188-byte 0x47 MPEG-TS stream.
    """
    if not data or len(data) < 188:
        return data
    if len(data) >= 377 and data[0] == 0x47 and data[188] == 0x47 and data[376] == 0x47:
        return data
    max_scan = min(len(data) - 376, 32768)
    for i in range(max(0, max_scan)):
        if data[i] == 0x47 and data[i + 188] == 0x47 and data[i + 376] == 0x47:
            return data[i:] if i > 0 else data
    max_short = min(len(data) - 187, 4096)
    for i in range(max(0, max_short)):
        if data[i] == 0x47 and (i + 188 == len(data) or (i + 188 < len(data) and data[i + 188] == 0x47)):
            return data[i:] if i > 0 else data
    return data


def _download_hls_parallel(direct_url: str, referer: str, output_path: Path, max_workers: int = 16) -> bool:
    """
    Turbo 16-connection parallel HLS (.m3u8) downloader for BOTH unencrypted MPEG-TS streams
    (HGCloud / Vibuxer / StreamWish / Morencius) AND standard RFC 8216 AES-128 streams (1vid).
    Downloads segments concurrently via persistent TLS sessions, then concat/local-m3u8 remuxes
    with +faststart in a single local FFmpeg pass (~6x-10x faster than single-TCP FFmpeg!).
    """
    import time
    import threading
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from urllib.parse import urljoin
    from worker.media_resolver import _session_get, _select_best_hls_variant, BROWSER_UA

    st, m3u8_text, _, final_m3u8, sess = _session_get(direct_url, referer=referer, timeout=15)
    if st != 200 or "#EXTM3U" not in (m3u8_text or ""):
        if st in (404, 410):
            raise RuntimeError(f"HLS playlist returned HTTP {st}")
        return False

    if "#EXT-X-STREAM-INF" in m3u8_text:
        best_var = _select_best_hls_variant(final_m3u8 or direct_url, referer=referer, session_obj=sess)
        st, m3u8_text, _, final_m3u8, sess = _session_get(best_var, referer=referer, session_obj=sess, timeout=15)
        if st != 200 or "#EXTM3U" not in (m3u8_text or ""):
            return False

    # Fall back to FFmpeg only if playlist uses non-AES-128 encryption (e.g. SAMPLE-AES) or fMP4 init map
    has_aes_key = False
    for ln in m3u8_text.splitlines():
        s_ln = ln.strip()
        if s_ln.startswith("#EXT-X-KEY"):
            u_ln = s_ln.upper()
            if "METHOD=AES-128" in u_ln:
                has_aes_key = True
            elif "METHOD=NONE" not in u_ln:
                return False
        if s_ln.startswith("#EXT-X-MAP"):
            return False

    base_playlist_url = final_m3u8 or direct_url
    seg_dir = output_path.parent / f"hls_segs_{output_path.stem}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    try:
        from curl_cffi import requests as creq
        has_cffi = True
    except ImportError:
        has_cffi = False

    origin = referer.rstrip("/")
    seg_headers = {
        "Accept": "*/*",
        "Referer": referer,
        "Origin": origin,
    }
    if not has_cffi:
        seg_headers["User-Agent"] = BROWSER_UA

    tls_local = threading.local()

    def _fetch_seg(idx_and_url):
        idx, s_url = idx_and_url
        seg_file = seg_dir / f"seg_{idx:05d}.ts"
        last_exc = None
        for attempt in range(4):
            try:
                if has_cffi:
                    c_sess = getattr(tls_local, "sess", None)
                    if c_sess is None:
                        c_sess = creq.Session(impersonate="chrome120")
                        tls_local.sess = c_sess
                    r = c_sess.get(s_url, headers=seg_headers, timeout=120)
                    code = r.status_code
                    body = r.content
                else:
                    h_cli = getattr(tls_local, "httpx_cli", None)
                    if h_cli is None:
                        h_cli = httpx.Client(timeout=120.0, follow_redirects=True)
                        tls_local.httpx_cli = h_cli
                    r = h_cli.get(s_url, headers=seg_headers)
                    code = r.status_code
                    body = r.content

                min_seg_len = 512 if idx == 0 else 188
                if code == 200 and body and len(body) >= min_seg_len:
                    head_low = body[:64].lower()
                    if b"<html" not in head_low and b"<!doc" not in head_low:
                        if not has_aes_key:
                            body = _strip_disguised_ts_prefix(body)
                        seg_file.write_bytes(body)
                        return idx, len(body)
                if code in (403, 404, 410) and idx == 0 and attempt >= 1:
                    raise RuntimeError(f"HLS segment 0 rejected with HTTP {code}")
                last_exc = RuntimeError(f"Segment {idx} HTTP {code} ({len(body) if body else 0}B)")
            except Exception as exc:
                last_exc = exc
                if "segment 0 rejected" in str(exc).lower():
                    raise
            time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"Failed HLS segment {idx}/{len(seg_urls)}: {last_exc}")

    try:
        seg_urls = []
        local_m3u8_lines = []
        key_counter = 0

        for raw_ln in m3u8_text.splitlines():
            s_ln = raw_ln.strip()
            if not s_ln:
                continue
            if s_ln.startswith("#EXT-X-KEY") and "URI=" in s_ln:
                km = re.search(r'URI=["\']([^"\']+)["\']', s_ln)
                if not km:
                    return False
                key_url = urljoin(base_playlist_url, km.group(1))
                key_filename = f"key_{key_counter}.bin"
                key_counter += 1
                if has_cffi:
                    k_sess = sess if hasattr(sess, "get") else creq.Session(impersonate="chrome120")
                    rk = k_sess.get(key_url, headers=seg_headers, timeout=20)
                    k_status, k_bytes = rk.status_code, rk.content
                else:
                    rk = httpx.get(key_url, headers=seg_headers, timeout=20.0, follow_redirects=True)
                    k_status, k_bytes = rk.status_code, rk.content
                if k_status != 200 or not k_bytes or len(k_bytes) != 16:
                    return False
                (seg_dir / key_filename).write_bytes(k_bytes)
                rewritten_key_ln = s_ln[:km.start(1)] + key_filename + s_ln[km.end(1):]
                local_m3u8_lines.append(rewritten_key_ln)
            elif s_ln.startswith("#"):
                local_m3u8_lines.append(s_ln)
            else:
                seg_idx = len(seg_urls)
                seg_urls.append(urljoin(base_playlist_url, s_ln))
                local_m3u8_lines.append(f"seg_{seg_idx:05d}.ts")

        if not seg_urls:
            return False

        t0 = time.time()
        # Probe segment 0 first so 403/dead edges fail in <1s without spawning 800 futures
        _, first_bytes = _fetch_seg((0, seg_urls[0]))
        total_bytes = first_bytes

        if len(seg_urls) > 1:
            workers = min(max_workers, len(seg_urls) - 1)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_fetch_seg, (i, seg_urls[i])) for i in range(1, len(seg_urls))]
                for fut in as_completed(futures):
                    _, b_len = fut.result()
                    total_bytes += b_len

        dl_secs = max(0.1, time.time() - t0)
        mb_total = total_bytes / (1024 * 1024)
        logger.info(
            f"⚡ Parallel HLS fetched {len(seg_urls)} segments ({mb_total:.1f} MB, aes128={has_aes_key}) in {dl_secs:.1f}s ({mb_total / dl_secs:.2f} MB/s). Muxing..."
        )

        if has_aes_key:
            if "#EXT-X-ENDLIST" not in local_m3u8_lines:
                local_m3u8_lines.append("#EXT-X-ENDLIST")
            local_m3u8 = seg_dir / "local.m3u8"
            local_m3u8.write_text("\n".join(local_m3u8_lines) + "\n", encoding="utf-8")
            mux_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                "-allowed_extensions", "ALL",
                "-protocol_whitelist", "file,crypto,data",
                "-i", str(local_m3u8),
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-dn",
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart",
                str(output_path),
            ]
        else:
            concat_txt = seg_dir / "concat.txt"
            concat_txt.write_text(
                "\n".join(f"file 'seg_{i:05d}.ts'" for i in range(len(seg_urls))) + "\n",
                encoding="utf-8",
            )
            mux_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_txt),
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-dn",
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart",
                str(output_path),
            ]

        res_mux = subprocess.run(mux_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if res_mux.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 15 * 1024 * 1024:
            raise RuntimeError(
                f"Parallel HLS mux failed: {res_mux.stderr.decode('utf-8', errors='replace')[-250:]}"
            )
        from worker.telegram_vault import get_video_metadata
        mw, mh, mdur = get_video_metadata(output_path)
        if mw < 160 or mh < 90 or mdur < 30:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(f"Parallel HLS mux produced invalid video metadata ({mw}x{mh}, {mdur}s)")
        output_path.with_suffix(".faststart_done").write_text("1", encoding="utf-8")
        return True
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)


def download_media_source(url: str, output_path: Path):
    """
    Downloads media file:
    1. Uses 16-fragment yt-dlp downloader for ok.ru / vkvideo.ru / vk.ru / dailymotion streams.
    2. Resolves cyberlocker/host URLs fresh on the spot into real direct video streams + exact Referer.
    3. Uses 16-connection Parallel HLS segment downloader for .m3u8 streams (with FFmpeg fallback).
    4. Uses 16-connection aria2c Range downloader for direct MP4 streams.
    """
    import subprocess
    from worker.media_resolver import resolve_stream_with_headers, BROWSER_UA

    u_low_orig = (url or "").lower()
    if any(k in u_low_orig for k in ("ok.ru/", "vkvideo.ru", "vk.ru/video", "vk.com/video", "dailymotion.com")) and shutil.which("yt-dlp"):
        max_vault_bytes = 1950 * 1024 * 1024  # Stay safely under Telegram MTProto 4000-part (2000 MiB) ceiling
        try:
            logger.info(f"Using 16-fragment yt-dlp downloader for: {url[:80]}")
            ytdlp_cmd = [
                "yt-dlp",
                "--no-playlist",
                "--no-warnings",
                "--concurrent-fragments", "16",
                "-f", (
                    "bestvideo[height<=720][filesize<1750M]+bestaudio/"
                    "bestvideo[height<=720][filesize_approx<1750M]+bestaudio/"
                    "bestvideo[height<=720][tbr<=1850]+bestaudio/"
                    "best[height<=720][filesize<1850M]/"
                    "best[height<=720][filesize_approx<1850M]/"
                    "best[height<=720][tbr<=1950]/"
                    "bestvideo[height<=480]+bestaudio/"
                    "best[height<=480]/"
                    "best[filesize<1850M]/best"
                ),
                "--merge-output-format", "mp4",
                "-o", str(output_path),
                url,
            ]
            res_ydl = subprocess.run(ytdlp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2400)
            if res_ydl.returncode == 0 and output_path.exists() and output_path.stat().st_size >= max_vault_bytes:
                logger.warning(
                    f"yt-dlp output ({output_path.stat().st_size / 1048576:.1f} MB) exceeds 1950 MB MTProto cap; "
                    f"re-fetching crisp <=480p stream for {url[:65]}..."
                )
                output_path.unlink(missing_ok=True)
                ytdlp_sd_cmd = [
                    "yt-dlp",
                    "--no-playlist",
                    "--no-warnings",
                    "--concurrent-fragments", "16",
                    "-f", "bestvideo[height<=480]+bestaudio/best[height<=480]/worst[height>=240]/worst",
                    "--merge-output-format", "mp4",
                    "-o", str(output_path),
                    url,
                ]
                res_ydl = subprocess.run(ytdlp_sd_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2400)
            if res_ydl.returncode == 0 and output_path.exists() and output_path.stat().st_size > 15 * 1024 * 1024:
                return
            err_tail = res_ydl.stderr.decode("utf-8", errors="replace")[-220:]
            logger.warning(f"yt-dlp exited {res_ydl.returncode} for {url[:65]} ({err_tail}), trying direct resolver fallback...")
        except Exception as ydl_err:
            logger.warning(f"yt-dlp fallback triggered for {url[:65]}: {ydl_err}")
        for p_part in output_path.parent.glob(f"{output_path.stem}*"):
            if p_part.is_file():
                p_part.unlink(missing_ok=True)

    direct_url, referer = resolve_stream_with_headers(url)
    logger.info(f"Target stream URL for download: {direct_url[:80]}... (ref={referer})")

    # Guard: If URL is still an unresolved host landing page, raise immediately so next mirror is tried
    if direct_url == url and not any(k in direct_url.lower() for k in [".mp4", ".m3u8", "cloudatacdn", "get_video", "mxcontent", "premilkyway", "1vid.online", "acek-cdn", "brqz.online", "cdnz.quest", "okcdn.ru", "vkuser"]):
        raise RuntimeError(f"Unresolved host landing page: {direct_url}")

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": referer,
    }

    if ".m3u8" in direct_url.lower() or "urlset" in direct_url.lower():
        try:
            hls_threads = int(os.getenv("HLS_PARALLEL_WORKERS", "8"))
            if _download_hls_parallel(direct_url, referer, output_path, max_workers=hls_threads):
                return
        except Exception as par_hls_err:
            err_l = str(par_hls_err).lower()
            logger.warning(f"Parallel HLS fallback to FFmpeg ({par_hls_err})")
            if re.search(r"\bhttp\s+(?:404|410)\b", err_l) or "segment 0 rejected" in err_l:
                raise
            if output_path.exists():
                output_path.unlink(missing_ok=True)

        logger.info(f"Using FFmpeg HLS fallback for: {output_path.name}")
        origin = referer.rstrip("/")
        ffmpeg_headers = f"Referer: {referer}\r\nOrigin: {origin}\r\nAccept: */*\r\n"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-user_agent", BROWSER_UA,
            "-headers", ffmpeg_headers,
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", direct_url,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-dn",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            str(output_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2700)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg download error: {res.stderr.decode('utf-8', errors='replace')[-300:]}")
        output_path.with_suffix(".faststart_done").write_text("1", encoding="utf-8")
    else:
        # Try ultra-fast multi-connection aria2c first (16 parallel Range streams)
        if shutil.which("aria2c"):
            try:
                aria_cmd = [
                    "aria2c",
                    "-x", "16",
                    "-s", "16",
                    "-k", "1M",
                    "--user-agent", BROWSER_UA,
                    "--referer", referer,
                    "--header", "Accept: */*",
                    "--check-certificate=false",
                    "--allow-overwrite=true",
                    "--auto-file-renaming=false",
                    "--summary-interval=0",
                    "--console-log-level=warn",
                    "--timeout=60",
                    "--connect-timeout=15",
                    "--max-tries=2",
                    "-d", str(output_path.parent),
                    "-o", output_path.name,
                    direct_url,
                ]
                res_aria = subprocess.run(aria_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1200)
                if res_aria.returncode == 0 and output_path.exists() and output_path.stat().st_size > 15 * 1024 * 1024:
                    return
                if output_path.exists():
                    output_path.unlink(missing_ok=True)
            except Exception as aria_err:
                logger.debug(f"aria2c fallback triggered: {aria_err}")
                if output_path.exists():
                    output_path.unlink(missing_ok=True)

        try:
            from curl_cffi import requests as crequests
            has_curl_cffi = True
        except ImportError:
            has_curl_cffi = False

        if has_curl_cffi:
            resp = crequests.get(direct_url, headers=headers, impersonate="chrome120", stream=True, timeout=900)
            if resp.status_code >= 400:
                raise RuntimeError(f"Download failed with HTTP {resp.status_code} on {direct_url}")
            c_type = resp.headers.get("content-type", "").lower()
            if "text/html" in c_type:
                raise RuntimeError(f"Stream returned HTML instead of video content ({c_type})")
            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024 * 8):
                    f.write(chunk)
        else:
            with httpx.stream("GET", direct_url, headers=headers, timeout=900.0, follow_redirects=True) as stream:
                stream.raise_for_status()
                c_type = stream.headers.get("content-type", "").lower()
                if "text/html" in c_type:
                    raise RuntimeError(f"Stream returned HTML instead of video content ({c_type})")
                with open(output_path, "wb") as f:
                    for chunk in stream.iter_bytes(chunk_size=1024 * 1024 * 8):
                        f.write(chunk)


def upload_to_doodstream(file_path: Path, api_key: str) -> Dict[str, str]:
    """Uploads a local file to DoodStream using their Upload API (disabled unless ENABLE_EXTERNAL_STREAM_UPLOAD=1)."""
    if not ENABLE_EXTERNAL_STREAM_UPLOAD:
        raise RuntimeError("External streaming server uploads are disabled (Telegram Vault ONLY mode).")
    server_resp = requests.get(
        "https://doodapi.co/api/upload/server",
        params={"key": api_key},
        timeout=30.0
    )
    server_resp.raise_for_status()
    upload_url = server_resp.json().get("result")

    if not upload_url:
        raise RuntimeError(f"Failed to obtain DoodStream upload server: {server_resp.text}")

    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f, "video/mp4")}
        data = {"api_key": api_key}
        upload_resp = requests.post(upload_url, data=data, files=files, timeout=600.0)
        upload_resp.raise_for_status()
        res_json = upload_resp.json()

    results = res_json.get("result", [])
    if not results or not results[0].get("filecode"):
        raise RuntimeError(f"DoodStream upload failed: {res_json}")

    file_code = results[0]["filecode"]
    return {
        "provider": "doodstream",
        "file_code": file_code,
        "embed_url": f"https://doodstream.com/e/{file_code}",
        "direct_url": f"https://doodstream.com/d/{file_code}"
    }


def upload_to_streamtape(file_path: Path, login: str, key: str) -> Dict[str, str]:
    """Uploads a local file to Streamtape using their Upload API (disabled unless ENABLE_EXTERNAL_STREAM_UPLOAD=1)."""
    if not ENABLE_EXTERNAL_STREAM_UPLOAD:
        raise RuntimeError("External streaming server uploads are disabled (Telegram Vault ONLY mode).")
    server_resp = httpx.get(
        "https://api.streamtape.com/file/ul",
        params={"login": login, "key": key},
        timeout=30.0
    )
    server_resp.raise_for_status()
    res_data = server_resp.json().get("result", {})
    upload_url = res_data.get("url")

    if not upload_url:
        raise RuntimeError(f"Failed to obtain Streamtape upload server: {server_resp.text}")

    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f, "video/mp4")}
        upload_resp = httpx.post(upload_url, files=files, timeout=600.0)
        upload_resp.raise_for_status()
        res_json = upload_resp.json()

    result = res_json.get("result", {})
    file_id = result.get("id")
    if not file_id:
        raise RuntimeError(f"Streamtape upload failed: {res_json}")

    return {
        "provider": "streamtape",
        "file_code": file_id,
        "embed_url": f"https://streamtape.com/e/{file_id}",
        "direct_url": f"https://streamtape.com/v/{file_id}"
    }


def build_episode_telegram_caption(episode_id: int, quality: str = "1080p") -> str:
    """Constructs rich HTML caption for episode uploads."""
    series_title = "مسلسل"
    season_num = 1
    episode_num = episode_id
    ep_title = ""
    year = None

    try:
        with SyncSessionLocal() as session:
            stmt = (
                select(Episode)
                .options(joinedload(Episode.season).joinedload(Season.series))
                .where(Episode.id == episode_id)
            )
            ep = session.execute(stmt).scalar_one_or_none()
            if ep:
                episode_num = ep.episode_number
                ep_title = (ep.title or "").strip()
                if ep.season:
                    season_num = ep.season.season_number
                    if ep.season.series:
                        series_title = ep.season.series.title or series_title
                        year = ep.season.series.year
    except Exception as e:
        logger.warning(f"Could not load episode {episode_id} metadata for caption: {e}")

    year_str = f" ({year})" if year else ""
    lines = [
        f"🎬 <b>مسلسل:</b> {series_title}{year_str}",
        f"📌 <b>الموسم:</b> {season_num} | <b>الحلقة:</b> {episode_num}",
    ]
    if ep_title and ep_title not in [f"الحلقة {episode_num}", f"حلقة {episode_num}", f"Episode {episode_num}"]:
        lines.append(f"🏷️ <b>العنوان:</b> {ep_title}")

    lines.extend([
        f"⚡ <b>الجودة:</b> {quality}",
        f"🆔 <b>Episode ID:</b> {episode_id}"
    ])
    return "\n".join(lines)


def build_movie_telegram_caption(
    movie_id: int,
    tmdb_metadata: Optional[Dict[str, Any]] = None,
    quality: str = "1080p"
) -> str:
    """Constructs rich HTML caption for movie uploads."""
    movie_title = "فيلم"
    year_str = ""
    db_id = movie_id

    try:
        with SyncSessionLocal() as session:
            stmt = select(Movie).where((Movie.id == movie_id) | (Movie.tmdb_id == movie_id))
            m = session.execute(stmt).scalar_one_or_none()
            if m:
                db_id = m.id
                if m.title:
                    movie_title = m.title
                if m.year:
                    year_str = f" ({m.year})"
    except Exception as e:
        logger.warning(f"Could not load movie {movie_id} metadata from DB for caption: {e}")

    if tmdb_metadata:
        t = tmdb_metadata.get("title") or tmdb_metadata.get("original_title")
        if t:
            movie_title = t
        rel = tmdb_metadata.get("release_date") or ""
        if len(rel) >= 4:
            year_str = f" ({rel[:4]})"

    lines = [
        f"🎬 <b>فيلم:</b> {movie_title}{year_str}",
        f"⚡ <b>الجودة:</b> {quality}",
        f"🆔 <b>Movie ID:</b> {db_id}"
    ]
    return "\n".join(lines)


def upsert_movie_and_streams_sync(
    movie_id_or_tmdb: int,
    tmdb_metadata: Dict[str, Any],
    stream_results: list[Dict[str, str]],
    quality: str = "1080p"
):
    """Atomically upserts movie metadata and stream embed links into PostgreSQL."""
    with SyncSessionLocal() as session:
        try:
            existing_movie = (
                session.query(Movie)
                .filter((Movie.id == movie_id_or_tmdb) | (Movie.tmdb_id == movie_id_or_tmdb))
                .first()
            )

            if existing_movie:
                movie_id = existing_movie.id
                if tmdb_metadata:
                    if tmdb_metadata.get("poster_path"):
                        existing_movie.poster_path = tmdb_metadata.get("poster_path")
                    if tmdb_metadata.get("backdrop_path"):
                        existing_movie.backdrop_path = tmdb_metadata.get("backdrop_path")
                    if tmdb_metadata.get("overview"):
                        existing_movie.overview = tmdb_metadata.get("overview")
                    if tmdb_metadata.get("vote_average"):
                        existing_movie.vote_average = float(tmdb_metadata.get("vote_average", 0.0))
                    if tmdb_metadata.get("genres"):
                        existing_movie.genres = tmdb_metadata.get("genres")
                session.commit()
            else:
                release_date = tmdb_metadata.get("release_date")
                year = int(release_date.split("-")[0]) if release_date and len(release_date) >= 4 else None
                raw_genres = tmdb_metadata.get("genres", [])
                genres = [g["name"] if isinstance(g, dict) else str(g) for g in raw_genres]

                stmt_movie = insert(Movie).values(
                    tmdb_id=movie_id_or_tmdb,
                    imdb_id=tmdb_metadata.get("imdb_id"),
                    title=tmdb_metadata.get("title", f"Movie #{movie_id_or_tmdb}"),
                    original_title=tmdb_metadata.get("original_title"),
                    overview=tmdb_metadata.get("overview"),
                    year=year,
                    poster_path=tmdb_metadata.get("poster_path"),
                    backdrop_path=tmdb_metadata.get("backdrop_path"),
                    vote_average=float(tmdb_metadata.get("vote_average", 0.0)),
                    vote_count=int(tmdb_metadata.get("vote_count", 0)),
                    popularity=float(tmdb_metadata.get("popularity", 0.0)),
                    runtime=tmdb_metadata.get("runtime"),
                    genres=genres
                ).on_conflict_do_update(
                    index_elements=[Movie.tmdb_id],
                    set_={
                        "vote_average": float(tmdb_metadata.get("vote_average", 0.0)),
                        "popularity": float(tmdb_metadata.get("popularity", 0.0)),
                        "poster_path": tmdb_metadata.get("poster_path")
                    }
                ).returning(Movie.id)

                movie_id = session.execute(stmt_movie).scalar_one()
                session.commit()

            for stream in stream_results:
                existing_link = (
                    session.query(StreamingLink)
                    .filter(
                        StreamingLink.movie_id == movie_id,
                        StreamingLink.provider_name == stream["provider"],
                        StreamingLink.quality == quality,
                    )
                    .first()
                )
                if existing_link:
                    existing_link.embed_url = stream["embed_url"]
                    existing_link.direct_url = stream.get("direct_url")
                    existing_link.file_code = stream.get("file_code")
                    existing_link.status = "active"
                else:
                    session.add(
                        StreamingLink(
                            movie_id=movie_id,
                            provider_name=stream["provider"],
                            embed_url=stream["embed_url"],
                            direct_url=stream.get("direct_url"),
                            quality=quality,
                            file_code=stream.get("file_code"),
                            status="active",
                        )
                    )

            session.commit()

            redis_sync.delete(f"cache:movie_v1:{movie_id_or_tmdb}")
            redis_sync.delete(f"cache:movie_v1:{movie_id}")
            logger.info(f"Database upsert and cache invalidation completed for Movie {movie_id}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error persisting stream records to DB: {e}")
            raise


def _check_unarchived_earlier_franchise_movie(session, movie_obj: Movie) -> Optional[str]:
    """
    Strict Franchise Predecessor Gate:
    If movie_obj belongs to a multi-part franchise in DB, verifies that EVERY earlier part
    present in DB has already been archived in TelegramArchive before allowing movie_obj to upload!
    """
    from worker.franchise_ordering import parse_movie_franchise_info, sort_franchise_movies

    target_info = parse_movie_franchise_info(movie_obj.title, original_title=movie_obj.original_title, year=movie_obj.year)
    f_key = target_info["franchise_key"]
    if not f_key:
        return None

    rows = session.execute(
        text("""
            SELECT m.id,
                   m.title,
                   m.original_title,
                   m.year,
                   EXISTS(
                       SELECT 1 FROM telegram_archives ta
                       WHERE ta.movie_id = m.id AND ta.file_size_bytes > 15000000
                   ) AS is_archived
            FROM movies m
            WHERE m.title IS NOT NULL AND trim(m.title) != '' AND m.title != 'مشاهدة';
        """)
    ).fetchall()

    same_franchise = []
    archived_titles = set()
    for mid, t, ot, yr, is_arch in rows:
        nt = re.sub(r"\s+", " ", (t or "").strip().lower())
        if is_arch and nt:
            archived_titles.add(nt)
        inf = parse_movie_franchise_info(t, original_title=ot, year=yr)
        if inf["franchise_key"] == f_key:
            same_franchise.append({
                "id": mid,
                "title": t,
                "norm_title": nt,
                "year": yr,
                "is_archived": bool(is_arch),
                "info": inf,
            })

    if len(same_franchise) < 2:
        return None

    for it in same_franchise:
        if it["norm_title"] in archived_titles:
            it["is_archived"] = True

    ordered = sort_franchise_movies(same_franchise)
    target_norm = re.sub(r"\s+", " ", (movie_obj.title or "").strip().lower())

    seen_nt = set()
    for it in ordered:
        if it["norm_title"] in seen_nt:
            continue
        seen_nt.add(it["norm_title"])
        if it["id"] == movie_obj.id or it["norm_title"] == target_norm:
            break
        # Ignore duplicate slot of the exact same part+sub_part if years match
        if (
            it["info"]["effective_part"] == target_info["effective_part"]
            and it["info"]["sub_part"] == target_info["sub_part"]
            and it["info"]["year_sort"] == target_info["year_sort"]
        ):
            continue
        if not it["is_archived"]:
            return f"{it['title']} (ID {it['id']})"

    return None


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_video_transit_pipeline(
    self,
    tmdb_id: int,
    source_download_url: Union[str, List[str]],
    quality: str = "1080p"
):
    """
    Strict Transit Pipeline: Predecessor Gate -> Download -> Upload to Telegram Vault -> Immediate Deletion.
    """
    from worker.franchise_ordering import parse_movie_franchise_info

    transit_root = Path(settings.TRANSIT_TEMP_DIR)
    transit_root.mkdir(parents=True, exist_ok=True)

    if not check_disk_space_available(str(transit_root), required_bytes=MIN_FREE_DISK_BYTES):
        logger.error(f"[Task {self.request.id}] VPS disk space critical. Postponing task.")
        raise self.retry(countdown=300)

    lock_keys = [f"riva:lock:movie:{tmdb_id}"]
    title_disp_keys_to_clean = set()
    franchise_key_for_cleanup: Optional[str] = None

    with SyncSessionLocal() as chk_session:
        existing_tg = chk_session.query(TelegramArchive).filter(
            TelegramArchive.movie_id == tmdb_id,
            TelegramArchive.file_size_bytes > 15000000,
        ).first()
        if existing_tg:
            logger.info(f"Movie {tmdb_id} is ALREADY archived in Telegram Vault. Skipping transit completely.")
            redis_sync.delete(f"riva:dispatched:movie:{tmdb_id}")
            return {"status": "skipped", "reason": "already_archived", "movie_id": tmdb_id}

        m_obj = chk_session.query(Movie).filter_by(id=tmdb_id).first()
        if m_obj and m_obj.title:
            norm_title = re.sub(r"\s+", " ", m_obj.title.strip().lower())
            if norm_title:
                lock_keys.append(f"riva:lock:movie_title:{norm_title}")
                title_disp_keys_to_clean.add(f"riva:dispatched:movie_title:{norm_title}")
                dup_title_tg = (
                    chk_session.query(TelegramArchive.id)
                    .join(Movie, Movie.id == TelegramArchive.movie_id)
                    .filter(
                        func.lower(func.trim(Movie.title)) == norm_title,
                        TelegramArchive.file_size_bytes > 15000000,
                    )
                    .first()
                )
                if dup_title_tg:
                    logger.info(f"Movie '{m_obj.title}' (ID {tmdb_id}) already has an archive under same title. Skipping.")
                    for tk in title_disp_keys_to_clean:
                        redis_sync.delete(tk)
                    redis_sync.delete(f"riva:dispatched:movie:{tmdb_id}")
                    return {"status": "skipped", "reason": "title_already_archived", "movie_id": tmdb_id}

            f_inf = parse_movie_franchise_info(m_obj.title, original_title=m_obj.original_title, year=m_obj.year)
            franchise_key_for_cleanup = f_inf["franchise_key"]

            # Strict Franchise Predecessor Gate: NEVER upload Part N if Part 1..N-1 in DB is unarchived
            # (unless explicitly requested by Admin/User in riva:vip_movie_ids)
            is_vip_movie = False
            try:
                is_vip_movie = bool(redis_sync.sismember("riva:vip_movie_ids", str(tmdb_id)))
            except Exception:
                pass
            blocking_part = None if is_vip_movie else _check_unarchived_earlier_franchise_movie(chk_session, m_obj)
            if blocking_part:
                logger.warning(
                    f"⏸️ Postponing Movie '{m_obj.title}' (ID {tmdb_id}): earlier franchise part '{blocking_part}' is NOT archived yet!"
                )
                for tk in title_disp_keys_to_clean:
                    redis_sync.delete(tk)
                redis_sync.delete(f"riva:dispatched:movie:{tmdb_id}")
                if franchise_key_for_cleanup:
                    redis_sync.delete(f"riva:lock:franchise:{franchise_key_for_cleanup}")
                return {
                    "status": "postponed",
                    "reason": "waiting_for_earlier_franchise_part",
                    "blocking_part": blocking_part,
                    "movie_id": tmdb_id,
                }

    acquired_locks = []
    task_owner = str(self.request.id or f"worker_{os.getpid()}")
    for lk in lock_keys:
        if not redis_sync.set(lk, task_owner, nx=True, ex=7200):
            for acq in acquired_locks:
                redis_sync.delete(acq)
            logger.info(f"Movie {tmdb_id} is currently being processed by another worker ({lk}). Skipping duplicate task.")
            return {"status": "skipped", "reason": "already_in_progress", "movie_id": tmdb_id}
        acquired_locks.append(lk)

    task_temp_dir = Path(tempfile.mkdtemp(prefix=f"task_{tmdb_id}_", dir=transit_root))
    local_video_file: Optional[Path] = None
    stream_results = []

    logger.info(f"Starting transit pipeline for Movie ID: {tmdb_id} in {task_temp_dir}")

    try:
        tmdb_metadata = {}
        try:
            from worker.metadata_enricher import enrich_movie_metadata
            with SyncSessionLocal() as meta_session:
                enrich_movie_metadata(meta_session, movie_id=tmdb_id)
                m_record = meta_session.query(Movie).filter_by(id=tmdb_id).first()
                if m_record:
                    if m_record.title:
                        nt_after = re.sub(r"\s+", " ", m_record.title.strip().lower())
                        if nt_after:
                            title_disp_keys_to_clean.add(f"riva:dispatched:movie_title:{nt_after}")
                    tmdb_metadata = {
                        "title": m_record.title,
                        "original_title": m_record.original_title,
                        "overview": m_record.overview,
                        "poster_path": m_record.poster_path,
                        "backdrop_path": m_record.backdrop_path,
                        "vote_average": m_record.vote_average,
                        "genres": m_record.genres,
                        "release_date": f"{m_record.year}-01-01" if m_record.year else ""
                    }
        except Exception as enrich_err:
            logger.warning(f"Auto-enrichment error for movie {tmdb_id}: {enrich_err}")

        candidate_urls = [source_download_url] if isinstance(source_download_url, str) else list(source_download_url)
        candidate_urls = list(dict.fromkeys([u for u in candidate_urls if u]))
        candidate_urls = rotate_candidates_by_provider(candidate_urls)

        if ENABLE_EXTERNAL_STREAM_UPLOAD and settings.DOODSTREAM_API_KEY:
            for cand in candidate_urls:
                if any(k in cand.lower() for k in ["doodstream", "playmogo", "dsvplay"]):
                    c_code = re.search(r"/(?:d|e)/([a-zA-Z0-9]+)", cand)
                    if c_code:
                        cloned = clone_doodstream_file(c_code.group(1), settings.DOODSTREAM_API_KEY)
                        if cloned:
                            stream_results.append(cloned)
                            break

        if ENABLE_EXTERNAL_STREAM_UPLOAD and settings.STREAMTAPE_API_KEY and settings.STREAMTAPE_API_LOGIN:
            for cand in candidate_urls:
                if "streamtape" in cand.lower():
                    c_id = re.search(r"/(?:v|e)/([a-zA-Z0-9]+)", cand)
                    if c_id:
                        cloned = clone_streamtape_file(c_id.group(1), settings.STREAMTAPE_API_LOGIN, settings.STREAMTAPE_API_KEY)
                        if cloned:
                            stream_results.append(cloned)
                            break

        local_video_file = task_temp_dir / f"{tmdb_id}_{quality}.mp4"
        download_success = False
        last_dl_error = None

        for idx, cand_url in enumerate(candidate_urls, 1):
            logger.info(f"Trying stream candidate {idx}/{len(candidate_urls)} for Movie {tmdb_id}: {cand_url}")
            try:
                download_media_source(cand_url, local_video_file)
                if is_valid_video_file(local_video_file, min_bytes=30 * 1024 * 1024):
                    download_success = True
                    file_size_mb = local_video_file.stat().st_size / (1024 * 1024)
                    logger.info(f"Stream candidate {idx} SUCCEEDED! ({file_size_mb:.2f} MB)")
                    break
                else:
                    sz = local_video_file.stat().st_size if local_video_file.exists() else 0
                    logger.warning(f"Stream candidate {idx} produced invalid file or HTML ({sz} bytes). Marking dead & trying next...")
                    mark_stream_link_dead(cand_url, reason=f"Invalid file or HTML ({sz} bytes)")
                    if local_video_file.exists():
                        local_video_file.unlink(missing_ok=True)
            except Exception as dl_err:
                last_dl_error = dl_err
                if _is_transient_download_error(dl_err):
                    logger.warning(f"Stream candidate {idx} hit transient timeout/reset ({dl_err}). Trying next candidate without marking dead...")
                else:
                    logger.warning(f"Stream candidate {idx} failed: {dl_err}. Marking dead & trying next candidate...")
                    mark_stream_link_dead(cand_url, reason=str(dl_err)[:100])
                if local_video_file.exists():
                    local_video_file.unlink(missing_ok=True)

        if not download_success:
            m_fail_cnt = redis_sync.incr(f"riva:movie_dl_fail_cnt:{tmdb_id}")
            redis_sync.expire(f"riva:movie_dl_fail_cnt:{tmdb_id}", 86400)
            if m_fail_cnt >= 2:
                try:
                    if redis_sync.set(f"riva:notified_dead_movie:{tmdb_id}", "1", nx=True, ex=43200):
                        from worker.telegram_vault import send_telegram_notification
                        send_telegram_notification(
                            f"<b>تعطلت سيرفرات تحميل الفيلم (يحتاج مصدر بديل):</b>\n"
                            f"{format_movie_alert_details(tmdb_id)}\n"
                            f"⚠️ <b>السبب:</b> فشلت كل السيرفرات المتاحة ({len(candidate_urls)}) بعد {m_fail_cnt} جولات.\n"
                            f"<code>{html.escape(str(last_dl_error)[:220])}</code>",
                            level="warning",
                        )
                except Exception:
                    pass
            logger.warning(f"All {len(candidate_urls)} stream candidates failed download for Movie {tmdb_id}. Skipping without retry.")
            if franchise_key_for_cleanup:
                redis_sync.set(f"riva:franchise_blocked:{franchise_key_for_cleanup}", "dl_failed", ex=900)
            return {
                "status": "failed",
                "reason": "all_candidates_dead",
                "tmdb_id": tmdb_id,
                "last_error": str(last_dl_error)
            }

        if download_success and local_video_file and local_video_file.exists():
            if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHANNEL_ID:
                try:
                    from worker.telegram_vault import upload_video_to_vault, record_telegram_archive
                    with SyncSessionLocal() as pre_up_sess:
                        already_uploaded_tg = pre_up_sess.query(TelegramArchive).filter(
                            TelegramArchive.movie_id == tmdb_id,
                            TelegramArchive.file_size_bytes > 15000000,
                        ).first()
                    if already_uploaded_tg:
                        logger.info(f"Movie {tmdb_id} was archived while downloading (msg {already_uploaded_tg.message_id}). Skipping duplicate Vault upload.")
                        stream_results.append({
                            "provider": "riva-server",
                            "file_code": f"tg_{already_uploaded_tg.message_id}",
                            "embed_url": f"/v1/stream/embed/movie/{tmdb_id}",
                            "direct_url": f"/v1/stream/video/movie/{tmdb_id}"
                        })
                    else:
                        logger.info(f"Backing up Movie {tmdb_id} to Telegram Master Vault...")
                        caption = build_movie_telegram_caption(tmdb_id, tmdb_metadata=tmdb_metadata, quality=quality)
                        m_poster = (tmdb_metadata or {}).get("poster_path")
                        m_backdrop = (tmdb_metadata or {}).get("backdrop_path")
                        if not m_poster or not m_backdrop:
                            with SyncSessionLocal() as p_sess:
                                m_db = p_sess.query(Movie).filter_by(id=tmdb_id).first()
                                if m_db:
                                    m_poster = m_poster or m_db.poster_path
                                    m_backdrop = m_backdrop or m_db.backdrop_path
                        tg_res = upload_video_to_vault(
                            local_video_file,
                            caption=caption,
                            thumb_url=m_poster,
                            backdrop_url=m_backdrop,
                        )
                        record_telegram_archive(tg_res, movie_id=tmdb_id)
                        stream_results.append({
                            "provider": "riva-server",
                            "file_code": f"tg_{tg_res['message_id']}",
                            "embed_url": f"/v1/stream/embed/movie/{tmdb_id}",
                            "direct_url": f"/v1/stream/video/movie/{tmdb_id}"
                        })
                except Exception as exc:
                    logger.error(f"Telegram vault backup failed for Movie {tmdb_id}: {exc}")

        if not stream_results:
            raise RuntimeError("Telegram vault upload failed. Retrying pipeline.")

        upsert_movie_and_streams_sync(tmdb_id, tmdb_metadata, stream_results, quality=quality)

        return {
            "status": "success",
            "tmdb_id": tmdb_id,
            "streams_created": len(stream_results)
        }

    except Exception as exc:
        logger.exception(f"Transit pipeline encountered error: {exc}")
        err_str = str(exc)
        try:
            from worker.telegram_vault import send_telegram_notification
            retries = getattr(self.request, "retries", 0)
            max_retries = getattr(self, "max_retries", 3)
            if retries >= max_retries:
                with SyncSessionLocal() as chk_s:
                    already_done = chk_s.query(TelegramArchive.id).filter(
                        TelegramArchive.movie_id == tmdb_id,
                        TelegramArchive.file_size_bytes > 15000000,
                    ).first()
                if not already_done:
                    send_telegram_notification(
                        f"<b>فشل معالجة الفيلم نهائياً (يحتاج مصدر بديل):</b>\n"
                        f"{format_movie_alert_details(tmdb_id)}\n"
                        f"⚠️ <b>السبب:</b> <code>{html.escape(err_str[:250])}</code>",
                        level="error",
                    )
        except Exception:
            pass
        raise self.retry(exc=exc)

    finally:
        for lk in acquired_locks:
            try:
                if redis_sync.get(lk) == task_owner:
                    redis_sync.delete(lk)
            except Exception:
                pass
        try:
            redis_sync.delete(f"riva:dispatched:movie:{tmdb_id}")
            for tk in title_disp_keys_to_clean:
                redis_sync.delete(tk)
            if franchise_key_for_cleanup:
                redis_sync.delete(f"riva:lock:franchise:{franchise_key_for_cleanup}")
        except Exception:
            pass

        logger.info(f"Triggering immediate transit cleanup for task dir: {task_temp_dir}")
        try:
            if local_video_file and local_video_file.exists():
                local_video_file.unlink(missing_ok=True)
            if task_temp_dir.exists():
                shutil.rmtree(task_temp_dir, ignore_errors=True)
        except Exception as cleanup_err:
            logger.critical(f"FATAL: Failed to clean transit directory {task_temp_dir}: {cleanup_err}")


def upsert_episode_streams_sync(
    episode_id: int,
    stream_results: list[Dict[str, str]],
    quality: str = "1080p"
):
    """Atomically adds or updates streaming links for a series episode."""
    with SyncSessionLocal() as session:
        try:
            for stream in stream_results:
                existing_link = (
                    session.query(StreamingLink)
                    .filter(
                        StreamingLink.episode_id == episode_id,
                        StreamingLink.provider_name == stream["provider"],
                        StreamingLink.quality == quality,
                    )
                    .first()
                )
                if existing_link:
                    existing_link.embed_url = stream["embed_url"]
                    existing_link.direct_url = stream.get("direct_url")
                    existing_link.file_code = stream.get("file_code")
                    existing_link.status = "active"
                else:
                    link = StreamingLink(
                        episode_id=episode_id,
                        provider_name=stream["provider"],
                        embed_url=stream["embed_url"],
                        direct_url=stream.get("direct_url"),
                        quality=quality,
                        file_code=stream.get("file_code"),
                        status="active"
                    )
                    session.add(link)
            session.commit()
            logger.info(f"Persisted {len(stream_results)} stream links for Episode ID: {episode_id}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error persisting episode stream records: {e}")
            raise


def _check_unarchived_earlier_episode(session, series_id: int, sea_num: int, ep_num: int) -> Optional[str]:
    """
    Strict Episode Predecessor Gate:
    Verifies that NO earlier episode slot in the same series exists in PostgreSQL
    without a completed TelegramArchive (ignoring only episodes confirmed 100% dead at source
    after multiple Akwam rescue rounds so 1 broken source episode doesn't freeze a 100-ep series).
    """
    missing_rows = session.execute(
        text("""
            SELECT sea.season_number, e.episode_number, e.id
            FROM episodes e
            JOIN seasons sea ON sea.id = e.season_id
            WHERE sea.series_id = :sid
              AND (
                  sea.season_number < :s_num
                  OR (sea.season_number = :s_num AND e.episode_number < :e_num)
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM telegram_archives ta
                  JOIN episodes e2 ON e2.id = ta.episode_id
                  JOIN seasons s2 ON s2.id = e2.season_id
                  WHERE s2.series_id = :sid
                    AND s2.season_number = sea.season_number
                    AND e2.episode_number = e.episode_number
                    AND ta.file_size_bytes > 15000000
              )
            ORDER BY sea.season_number ASC, e.episode_number ASC;
        """),
        {"sid": series_id, "s_num": sea_num, "e_num": ep_num},
    ).fetchall()

    for prev_s, prev_e, prev_id in missing_rows:
        if redis_sync.exists(f"riva:ep_source_unavailable:{prev_id}"):
            continue
        return f"S{prev_s:02d}E{prev_e:02d} (Ep {prev_id})"
    return None


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_episode_transit_pipeline(
    self,
    episode_id: int,
    source_download_url: Union[str, List[str]],
    quality: str = "1080p"
):
    """
    Strict Episode Transit Pipeline:
    Predecessor Gate (S01E01 -> S01E02 -> ...) -> Download -> Telegram Vault Upload -> Cleanup.
    """
    transit_root = Path(settings.TRANSIT_TEMP_DIR)
    transit_root.mkdir(parents=True, exist_ok=True)

    if not check_disk_space_available(str(transit_root), required_bytes=MIN_FREE_DISK_BYTES):
        logger.error(f"[Task {self.request.id}] VPS disk space critical. Postponing episode task.")
        raise self.retry(countdown=300)

    lock_keys = [f"riva:lock:ep:{episode_id}"]
    series_id_for_cleanup: Optional[int] = None

    with SyncSessionLocal() as chk_session:
        existing_tg = chk_session.query(TelegramArchive).filter(
            TelegramArchive.episode_id == episode_id,
            TelegramArchive.file_size_bytes > 15000000,
        ).first()
        if existing_tg:
            logger.info(f"Episode {episode_id} is ALREADY archived in Telegram Vault. Skipping transit completely.")
            redis_sync.delete(f"riva:dispatched:ep:{episode_id}")
            return {"status": "skipped", "reason": "already_archived", "episode_id": episode_id}

        ep_obj = (
            chk_session.query(Episode)
            .options(joinedload(Episode.season))
            .filter_by(id=episode_id)
            .first()
        )
        if not ep_obj:
            logger.info(f"Episode {episode_id} no longer exists in PostgreSQL. Skipping deleted episode.")
            redis_sync.delete(f"riva:dispatched:ep:{episode_id}")
            return {"status": "skipped", "reason": "episode_deleted", "episode_id": episode_id}
        if ep_obj and ep_obj.season:
            s_id = ep_obj.season.series_id
            series_id_for_cleanup = s_id
            sea_num = ep_obj.season.season_number
            ep_num = ep_obj.episode_number
            lock_keys.append(f"riva:lock:ep_slot:{s_id}_{sea_num}_{ep_num}")
            dup_slot_tg = (
                chk_session.query(TelegramArchive.id)
                .join(Episode, Episode.id == TelegramArchive.episode_id)
                .join(Season, Season.id == Episode.season_id)
                .filter(
                    Season.series_id == s_id,
                    Season.season_number == sea_num,
                    Episode.episode_number == ep_num,
                    TelegramArchive.file_size_bytes > 15000000,
                )
                .first()
            )
            if dup_slot_tg:
                logger.info(f"Episode {episode_id} slot (Series {s_id} S{sea_num}E{ep_num}) is ALREADY archived. Skipping.")
                redis_sync.delete(f"riva:dispatched:ep:{episode_id}")
                return {"status": "skipped", "reason": "slot_already_archived", "episode_id": episode_id}

            # Strict Episode Predecessor Gate: NEVER upload SxxEyy if an earlier episode of this series is still unarchived!
            missing_prev = _check_unarchived_earlier_episode(chk_session, s_id, sea_num, ep_num)
            if missing_prev:
                logger.warning(
                    f"⏸️ Postponing Episode {episode_id} (Series {s_id} S{sea_num:02d}E{ep_num:02d}): earlier episode {missing_prev} is NOT archived yet!"
                )
                redis_sync.delete(f"riva:dispatched:ep:{episode_id}")
                redis_sync.delete(f"riva:lock:series_chain:{s_id}")
                return {
                    "status": "postponed",
                    "reason": "waiting_for_earlier_episode",
                    "missing_previous": missing_prev,
                    "episode_id": episode_id,
                }

    acquired_locks = []
    task_owner = str(self.request.id or f"worker_{os.getpid()}")
    for lk in lock_keys:
        if not redis_sync.set(lk, task_owner, nx=True, ex=7200):
            for acq in acquired_locks:
                redis_sync.delete(acq)
            logger.info(f"Episode {episode_id} is currently being processed by another worker ({lk}). Skipping duplicate task.")
            return {"status": "skipped", "reason": "already_in_progress", "episode_id": episode_id}
        acquired_locks.append(lk)

    task_temp_dir = Path(tempfile.mkdtemp(prefix=f"task_ep_{episode_id}_", dir=transit_root))
    local_video_file: Optional[Path] = None
    stream_results = []

    logger.info(f"Starting episode transit pipeline for Episode ID: {episode_id} in {task_temp_dir}")

    try:
        try:
            from worker.metadata_enricher import enrich_series_metadata
            with SyncSessionLocal() as meta_session:
                ep_rec = meta_session.query(Episode).filter_by(id=episode_id).first()
                if ep_rec and ep_rec.season and ep_rec.season.series_id:
                    s_id = ep_rec.season.series_id
                    s_rec = meta_session.query(Series).filter_by(id=s_id).first()
                    if s_rec and (not s_rec.poster_path or not s_rec.overview):
                        enrich_series_metadata(meta_session, s_id)
        except Exception as s_enrich_err:
            logger.warning(f"Could not auto-enrich parent series for episode {episode_id}: {s_enrich_err}")

        candidate_urls = [source_download_url] if isinstance(source_download_url, str) else list(source_download_url)
        candidate_urls = list(dict.fromkeys([u for u in candidate_urls if u]))
        candidate_urls = rotate_candidates_by_provider(candidate_urls)

        if ENABLE_EXTERNAL_STREAM_UPLOAD and settings.DOODSTREAM_API_KEY:
            for cand in candidate_urls:
                if any(k in cand.lower() for k in ["doodstream", "playmogo", "dsvplay"]):
                    c_code = re.search(r"/(?:d|e)/([a-zA-Z0-9]+)", cand)
                    if c_code:
                        cloned = clone_doodstream_file(c_code.group(1), settings.DOODSTREAM_API_KEY)
                        if cloned:
                            stream_results.append(cloned)
                            break

        if ENABLE_EXTERNAL_STREAM_UPLOAD and settings.STREAMTAPE_API_KEY and settings.STREAMTAPE_API_LOGIN:
            for cand in candidate_urls:
                if "streamtape" in cand.lower():
                    c_id = re.search(r"/(?:v|e)/([a-zA-Z0-9]+)", cand)
                    if c_id:
                        cloned = clone_streamtape_file(c_id.group(1), settings.STREAMTAPE_API_LOGIN, settings.STREAMTAPE_API_KEY)
                        if cloned:
                            stream_results.append(cloned)
                            break

        local_video_file = task_temp_dir / f"ep_{episode_id}_{quality}.mp4"
        download_success = False
        last_dl_error = None

        for idx, cand_url in enumerate(candidate_urls, 1):
            logger.info(f"Trying stream candidate {idx}/{len(candidate_urls)} for Episode {episode_id}: {cand_url}")
            try:
                download_media_source(cand_url, local_video_file)
                if is_valid_video_file(local_video_file, min_bytes=15 * 1024 * 1024):
                    download_success = True
                    file_size_mb = local_video_file.stat().st_size / (1024 * 1024)
                    logger.info(f"Stream candidate {idx} SUCCEEDED! ({file_size_mb:.2f} MB)")
                    break
                else:
                    sz = local_video_file.stat().st_size if local_video_file.exists() else 0
                    logger.warning(f"Stream candidate {idx} produced invalid file or HTML ({sz} bytes). Marking dead & trying next...")
                    mark_stream_link_dead(cand_url, reason=f"Invalid file or HTML ({sz} bytes)")
                    if local_video_file.exists():
                        local_video_file.unlink(missing_ok=True)
            except Exception as dl_err:
                last_dl_error = dl_err
                if _is_transient_download_error(dl_err):
                    logger.warning(f"Stream candidate {idx} hit transient timeout/reset ({dl_err}). Trying next candidate without marking dead...")
                else:
                    logger.warning(f"Stream candidate {idx} failed: {dl_err}. Marking dead & trying next candidate...")
                    mark_stream_link_dead(cand_url, reason=str(dl_err)[:100])
                if local_video_file.exists():
                    local_video_file.unlink(missing_ok=True)

        if not download_success:
            fail_cnt = redis_sync.incr(f"riva:ep_dl_fail_cnt:{episode_id}")
            redis_sync.expire(f"riva:ep_dl_fail_cnt:{episode_id}", 86400)
            if fail_cnt >= 3:
                redis_sync.set(f"riva:ep_source_unavailable:{episode_id}", "dl_exhausted", ex=21600)
                logger.warning(
                    f"⚠️ Episode {episode_id} failed {fail_cnt} full multi-mirror download rounds; marking unavailable for 6h so series does not stall."
                )
                try:
                    if redis_sync.set(f"riva:notified_dead_ep:{episode_id}", "1", nx=True, ex=43200):
                        from worker.telegram_vault import send_telegram_notification
                        send_telegram_notification(
                            f"<b>تعطلت جميع سيرفرات تحميل الحلقة (تحتاج مصدر بديل):</b>\n"
                            f"{format_episode_alert_details(episode_id)}\n"
                            f"⚠️ <b>السبب:</b> فشلت كل السيرفرات المتاحة ({len(candidate_urls)}) عبر {fail_cnt} جولات.\n"
                            f"<code>{html.escape(str(last_dl_error)[:220])}</code>",
                            level="warning",
                        )
                except Exception:
                    pass
            elif series_id_for_cleanup:
                redis_sync.set(f"riva:series_blocked:{series_id_for_cleanup}", "dl_failed", ex=180)
            logger.warning(f"All {len(candidate_urls)} stream candidates failed download for Episode {episode_id}.")
            return {
                "status": "failed",
                "reason": "all_candidates_dead",
                "episode_id": episode_id,
                "last_error": str(last_dl_error)
            }

        if download_success and local_video_file and local_video_file.exists():
            if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHANNEL_ID:
                try:
                    from worker.telegram_vault import upload_video_to_vault, record_telegram_archive
                    with SyncSessionLocal() as pre_up_sess:
                        already_uploaded_tg = pre_up_sess.query(TelegramArchive).filter(
                            TelegramArchive.episode_id == episode_id,
                            TelegramArchive.file_size_bytes > 15000000,
                        ).first()
                    if already_uploaded_tg:
                        logger.info(f"Episode {episode_id} was archived while downloading (msg {already_uploaded_tg.message_id}). Skipping duplicate Vault upload.")
                        stream_results.append({
                            "provider": "riva-server",
                            "file_code": f"tg_{already_uploaded_tg.message_id}",
                            "embed_url": f"/v1/stream/embed/episode/{episode_id}",
                            "direct_url": f"/v1/stream/video/episode/{episode_id}"
                        })
                    else:
                        logger.info(f"Backing up Episode {episode_id} to Telegram Master Vault...")
                        caption = build_episode_telegram_caption(episode_id, quality=quality)
                        ep_poster = None
                        ep_backdrop = None
                        with SyncSessionLocal() as ep_p_sess:
                            ep_db = (
                                ep_p_sess.query(Episode)
                                .options(joinedload(Episode.season).joinedload(Season.series))
                                .filter_by(id=episode_id)
                                .first()
                            )
                            if ep_db:
                                ep_poster = ep_db.thumbnail
                                if ep_db.season and ep_db.season.series:
                                    ep_poster = ep_db.season.series.poster_path or ep_poster
                                    ep_backdrop = ep_db.season.series.backdrop_path
                        tg_res = upload_video_to_vault(
                            local_video_file,
                            caption=caption,
                            thumb_url=ep_poster,
                            backdrop_url=ep_backdrop,
                        )
                        record_telegram_archive(tg_res, episode_id=episode_id)
                        stream_results.append({
                            "provider": "riva-server",
                            "file_code": f"tg_{tg_res['message_id']}",
                            "embed_url": f"/v1/stream/embed/episode/{episode_id}",
                            "direct_url": f"/v1/stream/video/episode/{episode_id}"
                        })
                except Exception as exc:
                    logger.error(f"Telegram vault backup failed for Episode {episode_id}: {exc}")

        if not stream_results:
            raise RuntimeError("Telegram vault upload failed for episode. Retrying.")

        upsert_episode_streams_sync(episode_id, stream_results, quality=quality)

        return {
            "status": "success",
            "episode_id": episode_id,
            "streams_created": len(stream_results)
        }

    except Exception as exc:
        logger.exception(f"Episode transit pipeline error: {exc}")
        err_str = str(exc)
        try:
            from worker.telegram_vault import send_telegram_notification
            retries = getattr(self.request, "retries", 0)
            max_retries = getattr(self, "max_retries", 3)
            if retries >= max_retries:
                with SyncSessionLocal() as chk_s:
                    already_done = chk_s.query(TelegramArchive.id).filter(
                        TelegramArchive.episode_id == episode_id,
                        TelegramArchive.file_size_bytes > 15000000,
                    ).first()
                if not already_done:
                    send_telegram_notification(
                        f"<b>فشل معالجة الحلقة نهائياً (تحتاج مصدر بديل):</b>\n"
                        f"{format_episode_alert_details(episode_id)}\n"
                        f"⚠️ <b>السبب:</b> <code>{html.escape(err_str[:250])}</code>",
                        level="error",
                    )
        except Exception:
            pass
        raise self.retry(exc=exc)

    finally:
        for lk in acquired_locks:
            try:
                if redis_sync.get(lk) == task_owner:
                    redis_sync.delete(lk)
            except Exception:
                pass
        try:
            redis_sync.delete(f"riva:dispatched:ep:{episode_id}")
            if series_id_for_cleanup:
                redis_sync.delete(f"riva:lock:series_chain:{series_id_for_cleanup}")
        except Exception:
            pass
        if local_video_file and local_video_file.exists():
            local_video_file.unlink(missing_ok=True)
        if task_temp_dir.exists():
            shutil.rmtree(task_temp_dir, ignore_errors=True)
