import os
import shutil
import logging
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
import httpx
import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker, joinedload
from sqlalchemy.dialects.postgresql import insert
import redis

from app.config import settings
from app.models import Movie, Series, Season, Episode, StreamingLink
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


def check_disk_space_available(directory: str, required_bytes: int = MIN_FREE_DISK_BYTES) -> bool:
    """Verifies that the VPS transit disk has sufficient capacity before beginning."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    total, used, free = shutil.disk_usage(directory)
    if free < required_bytes:
        logger.error(f"Insufficient disk space in {directory}: {free / (1024**3):.2f} GB free, need {required_bytes / (1024**3):.2f} GB")
        return False
    return True


def download_media_source(url: str, output_path: Path):
    """
    Downloads media file:
    1. Resolves cyberlocker/host URLs (DoodStream, Playmogo, Streamtape, Mixdrop) to real direct video streams.
    2. Streams download with browser headers to prevent 403 Forbidden / Cloudflare blocks.
    3. Handles HLS m3u8 playlists via FFmpeg remuxer.
    """
    import subprocess
    from worker.media_resolver import resolve_stream_to_direct_url, BROWSER_UA

    # Resolve direct downloadable CDN/MP4 stream URL
    direct_url = resolve_stream_to_direct_url(url)
    logger.info(f"Target stream URL for download: {direct_url[:80]}...")

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": "https://playmogo.com/" if ("playmogo" in direct_url or "dood" in direct_url) else "https://akwams.org/"
    }

    if ".m3u8" in direct_url or "hls" in direct_url:
        logger.info(f"Detected HLS m3u8 stream. Using FFmpeg remuxer to download: {output_path.name}")
        cmd = [
            "ffmpeg", "-y",
            "-user_agent", BROWSER_UA,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", direct_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(output_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg download error: {res.stderr.decode('utf-8', errors='replace')[-300:]}")
    else:
        with httpx.stream("GET", direct_url, headers=headers, timeout=900.0, follow_redirects=True) as stream:
            stream.raise_for_status()
            c_type = stream.headers.get("content-type", "").lower()
            if "text/html" in c_type:
                raise RuntimeError(f"Stream returned HTML instead of video content ({c_type})")
            with open(output_path, "wb") as f:
                for chunk in stream.iter_bytes(chunk_size=1024 * 1024 * 4):
                    f.write(chunk)


def upload_to_doodstream(file_path: Path, api_key: str) -> Dict[str, str]:
    """
    Uploads a local file to DoodStream using their Upload API.
    Returns: dict with 'embed_url' and 'file_code'
    """
    # Step 1: Get upload server URL
    server_resp = requests.get(
        "https://doodapi.co/api/upload/server",
        params={"key": api_key},
        timeout=30.0
    )
    server_resp.raise_for_status()
    upload_url = server_resp.json().get("result")

    if not upload_url:
        raise RuntimeError(f"Failed to obtain DoodStream upload server: {server_resp.text}")

    # Step 2: Stream multipart upload using requests
    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f, "video/mp4")}
        data = {"api_key": api_key}
        upload_resp = requests.post(upload_url, data=data, files=files, timeout=600.0)
        upload_resp.raise_for_status()
        res_json = upload_resp.json()

    # DoodStream returns result array with file code
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
    """
    Uploads a local file to Streamtape using their Upload API.
    Returns: dict with 'embed_url' and 'file_code'
    """
    # Step 1: Get upload server URL
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

    # Step 2: Stream multipart upload
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
    """
    Constructs rich HTML caption for episode uploads containing:
    - Series Name and Year
    - Season/Part
    - Episode Number and Title
    - Quality & Episode ID
    """
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
    """
    Constructs rich HTML caption for movie uploads containing:
    - Movie Title
    - Release Year
    - Quality & Movie ID
    """
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
    """
    Atomically upserts movie metadata and stream embed links into PostgreSQL.
    Supports both TMDB ID and local Movie ID.
    """
    with SyncSessionLocal() as session:
        try:
            # Check if Movie already exists by internal id or tmdb_id
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
                session.commit()
            else:
                # Upsert Movie via TMDB metadata
                release_date = tmdb_metadata.get("release_date")
                year = int(release_date.split("-")[0]) if release_date and len(release_date) >= 4 else None
                genres = [g["name"] for g in tmdb_metadata.get("genres", [])]

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

            # 2. Upsert Streaming Links
            for stream in stream_results:
                stmt_stream = insert(StreamingLink).values(
                    movie_id=movie_id,
                    provider_name=stream["provider"],
                    embed_url=stream["embed_url"],
                    direct_url=stream.get("direct_url"),
                    quality=quality,
                    file_code=stream.get("file_code"),
                    status="active"
                ).on_conflict_do_update(
                    constraint="uq_movie_provider_quality",
                    set_={
                        "embed_url": stream["embed_url"],
                        "direct_url": stream.get("direct_url"),
                        "status": "active"
                    }
                )
                session.execute(stmt_stream)

            session.commit()

            # 3. Invalidate Redis Cache so API returns new streams immediately
            redis_sync.delete(f"cache:movie_v1:{movie_id_or_tmdb}")
            redis_sync.delete(f"cache:movie_v1:{movie_id}")
            logger.info(f"Database upsert and cache invalidation completed for Movie {movie_id}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error persisting stream records to DB: {e}")
            raise


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_video_transit_pipeline(
    self,
    tmdb_id: int,
    source_download_url: Union[str, List[str]],
    quality: str = "1080p"
):
    """
    Strict Transit Pipeline: Download -> Upload to Cyberlockers & Telegram -> Immediate Deletion.
    Supports a fallback chain of multiple candidate stream links. If one fails, it tries the rest.
    """
    transit_root = Path(settings.TRANSIT_TEMP_DIR)
    transit_root.mkdir(parents=True, exist_ok=True)

    # 1. Pre-flight disk space safety check
    if not check_disk_space_available(str(transit_root), required_bytes=MIN_FREE_DISK_BYTES):
        logger.error(f"[Task {self.request.id}] VPS disk space critical. Postponing task.")
        try:
            from worker.telegram_vault import send_telegram_notification
            send_telegram_notification("مساحة القرص المؤقت غير كافية لمتابعة تنزيل الأفلام.", level="warning")
        except Exception:
            pass
        raise self.retry(countdown=300)

    # Create an isolated temporary working directory for this task
    task_temp_dir = Path(tempfile.mkdtemp(prefix=f"task_{tmdb_id}_", dir=transit_root))
    local_video_file: Optional[Path] = None
    stream_results = []

    logger.info(f"Starting transit pipeline for TMDB/Movie ID: {tmdb_id} in {task_temp_dir}")

    try:
        # 2. Fetch TMDB metadata safely if configured
        tmdb_metadata = {}
        if settings.TMDB_API_KEY:
            try:
                tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
                with httpx.Client(timeout=15.0) as client:
                    resp = client.get(tmdb_url, params={"api_key": settings.TMDB_API_KEY})
                    if resp.status_code == 200:
                        tmdb_metadata = resp.json()
            except Exception as e:
                logger.warning(f"TMDB API fetch failed for {tmdb_id}: {e}")

        # 3. Stream chunked download from candidate URLs (Multi-Link Fallback Chain)
        candidate_urls = [source_download_url] if isinstance(source_download_url, str) else list(source_download_url)
        candidate_urls = list(dict.fromkeys([u for u in candidate_urls if u]))

        local_video_file = task_temp_dir / f"{tmdb_id}_{quality}.mp4"
        download_success = False
        last_dl_error = None

        for idx, cand_url in enumerate(candidate_urls, 1):
            logger.info(f"Trying stream candidate {idx}/{len(candidate_urls)} for Movie {tmdb_id}: {cand_url}")
            try:
                download_media_source(cand_url, local_video_file)
                if local_video_file.exists() and local_video_file.stat().st_size > 500 * 1024:
                    download_success = True
                    file_size_mb = local_video_file.stat().st_size / (1024 * 1024)
                    logger.info(f"Stream candidate {idx} SUCCEEDED! ({file_size_mb:.2f} MB)")
                    break
                else:
                    sz = local_video_file.stat().st_size if local_video_file.exists() else 0
                    logger.warning(f"Stream candidate {idx} produced invalid file ({sz} bytes). Trying next candidate...")
                    if local_video_file.exists():
                        local_video_file.unlink(missing_ok=True)
            except Exception as dl_err:
                last_dl_error = dl_err
                logger.warning(f"Stream candidate {idx} failed: {dl_err}. Trying next candidate...")
                if local_video_file.exists():
                    local_video_file.unlink(missing_ok=True)

        if not download_success:
            raise RuntimeError(f"All {len(candidate_urls)} stream candidates failed for Movie {tmdb_id}. Last error: {last_dl_error}")

        # 4. Upload to remote cyberlockers
        # Primary: DoodStream
        if settings.DOODSTREAM_API_KEY:
            try:
                logger.info(f"Uploading to DoodStream for TMDB/Movie {tmdb_id}...")
                dood_res = upload_to_doodstream(local_video_file, settings.DOODSTREAM_API_KEY)
                stream_results.append(dood_res)
            except Exception as exc:
                logger.error(f"DoodStream upload failed: {exc}")

        # Secondary: Streamtape
        if settings.STREAMTAPE_API_KEY and settings.STREAMTAPE_API_LOGIN:
            try:
                logger.info(f"Uploading to Streamtape for TMDB/Movie {tmdb_id}...")
                tape_res = upload_to_streamtape(local_video_file, settings.STREAMTAPE_API_LOGIN, settings.STREAMTAPE_API_KEY)
                stream_results.append(tape_res)
            except Exception as exc:
                logger.error(f"Streamtape upload failed: {exc}")

        # Permanent Backup: Telegram Master Vault
        if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHANNEL_ID:
            try:
                from worker.telegram_vault import upload_video_to_vault, record_telegram_archive
                logger.info(f"Backing up TMDB/Movie {tmdb_id} to Telegram Master Vault...")
                caption = build_movie_telegram_caption(tmdb_id, tmdb_metadata=tmdb_metadata, quality=quality)
                tg_res = upload_video_to_vault(
                    local_video_file,
                    caption=caption
                )
                record_telegram_archive(tg_res, movie_id=tmdb_id)
                stream_results.append({
                    "provider": "riva-server",
                    "file_code": f"tg_{tg_res['message_id']}",
                    "embed_url": f"/v1/stream/embed/movie/{tmdb_id}",
                    "direct_url": f"/v1/stream/video/movie/{tmdb_id}"
                })
            except Exception as exc:
                logger.error(f"Telegram vault backup failed for TMDB/Movie {tmdb_id}: {exc}")

        if not stream_results:
            raise RuntimeError("All cyberlocker uploads failed. Retrying pipeline.")

        # 5. Upsert results into PostgreSQL
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
            if "403" in err_str or "Forbidden" in err_str:
                send_telegram_notification(f"<b>تنبيه حظر/انتهاء صلاحية رابط (Movie ID: {tmdb_id}):</b>\n<code>{err_str[:250]}</code>", level="ban")
            elif getattr(self.request, "retries", 0) >= getattr(self, "max_retries", 3):
                send_telegram_notification(f"<b>فشل معالجة الفيلم (Movie ID: {tmdb_id}):</b>\n<code>{err_str[:250]}</code>", level="error")
        except Exception:
            pass
        raise self.retry(exc=exc)

    finally:
        # 6. ABSOLUTE GUARANTEED CLEANUP
        # Immediately delete the video file and its parent temp dir
        logger.info(f"Triggering immediate transit cleanup for task dir: {task_temp_dir}")
        try:
            if local_video_file and local_video_file.exists():
                local_video_file.unlink(missing_ok=True)
                logger.info(f"Successfully deleted local transit file: {local_video_file}")
            
            if task_temp_dir.exists():
                shutil.rmtree(task_temp_dir, ignore_errors=True)
                logger.info(f"Successfully pruned temporary task directory: {task_temp_dir}")
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


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_episode_transit_pipeline(
    self,
    episode_id: int,
    source_download_url: Union[str, List[str]],
    quality: str = "1080p"
):
    """
    Downloads source episode video on VPS, uploads to DoodStream & Streamtape,
    and immediately deletes local file to preserve disk space.
    Supports a fallback chain of multiple candidate stream links.
    """
    transit_root = Path(settings.TRANSIT_TEMP_DIR)
    transit_root.mkdir(parents=True, exist_ok=True)

    if not check_disk_space_available(str(transit_root), required_bytes=MIN_FREE_DISK_BYTES):
        logger.error(f"[Task {self.request.id}] VPS disk space critical. Postponing episode task.")
        try:
            from worker.telegram_vault import send_telegram_notification
            send_telegram_notification("مساحة القرص المؤقت غير كافية لمتابعة تنزيل الحلقات.", level="warning")
        except Exception:
            pass
        raise self.retry(countdown=300)

    task_temp_dir = Path(tempfile.mkdtemp(prefix=f"task_ep_{episode_id}_", dir=transit_root))
    local_video_file: Optional[Path] = None
    stream_results = []

    logger.info(f"Starting episode transit pipeline for Episode ID: {episode_id} in {task_temp_dir}")

    try:
        # 3. Stream chunked download from candidate URLs (Multi-Link Fallback Chain)
        candidate_urls = [source_download_url] if isinstance(source_download_url, str) else list(source_download_url)
        candidate_urls = list(dict.fromkeys([u for u in candidate_urls if u]))

        local_video_file = task_temp_dir / f"ep_{episode_id}_{quality}.mp4"
        download_success = False
        last_dl_error = None

        for idx, cand_url in enumerate(candidate_urls, 1):
            logger.info(f"Trying stream candidate {idx}/{len(candidate_urls)} for Episode {episode_id}: {cand_url}")
            try:
                download_media_source(cand_url, local_video_file)
                if local_video_file.exists() and local_video_file.stat().st_size > 500 * 1024:
                    download_success = True
                    file_size_mb = local_video_file.stat().st_size / (1024 * 1024)
                    logger.info(f"Stream candidate {idx} SUCCEEDED! ({file_size_mb:.2f} MB)")
                    break
                else:
                    sz = local_video_file.stat().st_size if local_video_file.exists() else 0
                    logger.warning(f"Stream candidate {idx} produced invalid file ({sz} bytes). Trying next candidate...")
                    if local_video_file.exists():
                        local_video_file.unlink(missing_ok=True)
            except Exception as dl_err:
                last_dl_error = dl_err
                logger.warning(f"Stream candidate {idx} failed: {dl_err}. Trying next candidate...")
                if local_video_file.exists():
                    local_video_file.unlink(missing_ok=True)

        if not download_success:
            raise RuntimeError(f"All {len(candidate_urls)} stream candidates failed for Episode {episode_id}. Last error: {last_dl_error}")

        # Upload to DoodStream
        if settings.DOODSTREAM_API_KEY:
            try:
                logger.info(f"Uploading episode to DoodStream for Ep {episode_id}...")
                dood_res = upload_to_doodstream(local_video_file, settings.DOODSTREAM_API_KEY)
                stream_results.append(dood_res)
            except Exception as exc:
                logger.error(f"DoodStream upload failed: {exc}")

        # Upload to Streamtape
        if settings.STREAMTAPE_API_KEY and settings.STREAMTAPE_API_LOGIN:
            try:
                logger.info(f"Uploading episode to Streamtape for Ep {episode_id}...")
                tape_res = upload_to_streamtape(local_video_file, settings.STREAMTAPE_API_LOGIN, settings.STREAMTAPE_API_KEY)
                stream_results.append(tape_res)
            except Exception as exc:
                logger.error(f"Streamtape upload failed: {exc}")

        # Permanent Backup: Telegram Master Vault
        if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHANNEL_ID:
            try:
                from worker.telegram_vault import upload_video_to_vault, record_telegram_archive
                logger.info(f"Backing up Episode {episode_id} to Telegram Master Vault...")
                caption = build_episode_telegram_caption(episode_id, quality=quality)
                tg_res = upload_video_to_vault(
                    local_video_file,
                    caption=caption
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
            raise RuntimeError("All cyberlocker uploads failed for episode. Retrying.")

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
            if "403" in err_str or "Forbidden" in err_str:
                send_telegram_notification(f"<b>تنبيه حظر/انتهاء صلاحية رابط (Episode ID: {episode_id}):</b>\n<code>{err_str[:250]}</code>", level="ban")
            elif getattr(self.request, "retries", 0) >= getattr(self, "max_retries", 3):
                send_telegram_notification(f"<b>فشل معالجة الحلقة نهائياً (Episode ID: {episode_id}):</b>\n<code>{err_str[:250]}</code>", level="error")
        except Exception:
            pass
        raise self.retry(exc=exc)

    finally:
        if local_video_file and local_video_file.exists():
            local_video_file.unlink(missing_ok=True)
        if task_temp_dir.exists():
            shutil.rmtree(task_temp_dir, ignore_errors=True)

