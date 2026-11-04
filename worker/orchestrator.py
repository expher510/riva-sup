"""
Riva 24/7 Autopilot Orchestrator.
Runs in the background indefinitely, dispatching strictly ordered series & movie uploads,
crawling categories, monitoring link health, and enriching TMDB metadata.
"""
import os
import time
import shutil
import logging
import asyncio
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from worker.telegram_vault import send_telegram_notification
from worker.scrapers.cima4u_crawler import CATEGORIES as CIMA_CATEGORIES, crawl_category as crawl_cima_category
from worker.scrapers.akwam_crawler import CATEGORIES as AKWAM_CATEGORIES, crawl_akwam_category
from worker.batch_dispatcher import (
    dispatch_pending_episodes,
    dispatch_pending_movies,
    TURKISH_PRIORITY_ONLY,
    SERIES_VIRAL_PRIORITY_ENABLED,
    MOVIE_VIRAL_PRIORITY_ENABLED,
    MAX_CONCURRENT_MOVIE_JOBS,
)
from worker.link_auditor import (
    run_bulk_domain_cleaner,
    clean_bad_database_records,
    get_health_stats
)
from worker.metadata_enricher import run_enrichment_cycle

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True
)
SyncSession = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("riva_orchestrator")

# Turbo-loop tick (seconds). Each tick runs dispatch SQL + Redis scans, so a calmer tick = less idle CPU.
ORCH_TICK_SECONDS = max(15, int(os.getenv("ORCH_TICK_SECONDS", "40")))


def _every(seconds: int) -> int:
    """Converts a wall-clock period into a cycle count for the current tick length."""
    return max(1, round(seconds / ORCH_TICK_SECONDS))


CLEANUP_EVERY = _every(300)    # stale transit cleanup ~5 min
SYNC_EVERY = _every(1800)      # TMDB viral / VIP sync ~30 min
REPORT_EVERY = _every(1800)    # Telegram stats report ~30 min


def get_current_stats():
    with SyncSession() as session:
        series_cnt = session.execute(text("SELECT count(*) FROM series;")).scalar() or 0
        movies_cnt = session.execute(text("SELECT count(*) FROM movies WHERE title != 'مشاهدة' AND title IS NOT NULL;")).scalar() or 0
        episodes_cnt = session.execute(text("SELECT count(*) FROM episodes;")).scalar() or 0
        health = get_health_stats()
        return {
            "series": series_cnt,
            "movies": movies_cnt,
            "episodes": episodes_cnt,
            "streams_active": health["active_links"],
            "streams_dead": health["dead_links"],
            "telegram_real": health["real_vault_archives"]
        }


async def _dispatch_round(tag: str = "cycle"):
    """Runs ordered movie & episode dispatch in a worker thread so async loop stays responsive."""
    try:
        mv_cnt = (
            await asyncio.to_thread(dispatch_pending_movies, MAX_CONCURRENT_MOVIE_JOBS)
            if (MOVIE_VIRAL_PRIORITY_ENABLED or not TURKISH_PRIORITY_ONLY)
            else 0
        )
        chain_len = 3 if TURKISH_PRIORITY_ONLY else 6
        ep_cnt = await asyncio.to_thread(dispatch_pending_episodes, chain_len)
        if ep_cnt or mv_cnt:
            logger.info(f"[{tag}] Dispatched {mv_cnt} viral/top-rated movies and {ep_cnt} ordered episodes to Celery.")
        return ep_cnt, mv_cnt
    except Exception as disp_err:
        logger.error(f"[{tag}] Dispatch error: {disp_err}")
        return 0, 0


async def run_forever():
    if TURKISH_PRIORITY_ONLY and MOVIE_VIRAL_PRIORITY_ENABLED:
        logger.info("Starting Riva Orchestrator in TMDB VIRAL + TOP-RATED MOVIES, SERIES & ANIME TURBO MODE (Telegram Vault ONLY)...")
        send_telegram_notification(
            "🔥 <b>تم تفعيل المحرك الشامل للأكثر شهرة وترند وأعلى تقييم على TMDB (أفلام + مسلسلات + أنمي):</b>\n"
            "• 🎬 <b>الأفلام:</b> رفع أكثر الأفلام ترند ومفيرلة هذه الفترة + أعلى الأفلام تقييماً + أفلام الأنمي بالتوازي\n"
            "• 📺 <b>المسلسلات:</b> رفع أشهر المسلسلات المفيرلة والأعلى تقييماً على TMDB + استكمال المسلسلات الجارية\n"
            "• 🎌 <b>الأنمي:</b> مسار مخصص لأشهر وأعلى مسلسلات وأفلام الأنمي على TMDB بالترتيب الصارم\n"
            "• ✅ <b>معايير ثابتة:</b> أبعاد حقيقية + بوستر رسمي + تشغيل مباشر في متصفح تيلجرام (`+faststart`) + تسلسل صارم (`Ep 1 → 2` / `Part 1 → 2`).",
            level="success"
        )
    elif TURKISH_PRIORITY_ONLY:
        logger.info("Starting Riva Orchestrator in TURKISH SERIES TURBO PRIORITY MODE (Telegram Vault ONLY)...")
        send_telegram_notification(
            "🔥 <b>تم تفعيل وضع الأولوية القصوى للمسلسلات التركية (Turbo Mode):</b>\n"
            "• استكمال المسلسلات التركية الموجودة في البوت من الحلقة الأولى للأخيرة بالترتيب الصارم\n"
            "• رفع مباشر على تيلجرام فقط (إلغاء سيرفرات الستريم الخارجية بالكامل).",
            level="success"
        )
    else:
        logger.info("Starting Riva 24/7 Autopilot Orchestrator (Strict Ordered Series & Franchise Mode)...")
        send_telegram_notification(
            "🚀 <b>تم تفعيل محرك Riva Autopilot المحدّث (رفع منظم ومتسلسل 100%).</b>",
            level="success"
        )

    try:
        cleaned_domains = run_bulk_domain_cleaner()
        cleaned_records = clean_bad_database_records()
        logger.info(f"Initial boot audit: Filtered {cleaned_domains} dead domain links. Purged: {cleaned_records}")
    except Exception as boot_err:
        logger.error(f"Error during initial boot clean: {boot_err}")

    cycle_count = 1
    while True:
        try:
            if TURKISH_PRIORITY_ONLY:
                await _dispatch_round(tag=f"tr-turbo#{cycle_count}")

                # Periodic stale transit cleanup every ~5 minutes
                if cycle_count % CLEANUP_EVERY == 1 or CLEANUP_EVERY == 1:
                    transit_root = Path(settings.TRANSIT_TEMP_DIR)
                    if transit_root.exists():
                        now_ts = time.time()
                        for d in transit_root.iterdir():
                            if d.is_dir() and d.name.startswith("task_") and (now_ts - d.stat().st_mtime > 5400):
                                shutil.rmtree(d, ignore_errors=True)
                    clean_bad_database_records()

                # Sync TMDB Viral + Top-Rated movies & series + VIP Watchlist on cycle 1 and every ~30 minutes
                if cycle_count == 1 or cycle_count % SYNC_EVERY == 0:
                    try:
                        from worker.scrapers.qisat_scraper import sync_qisat_series_to_db
                        from worker.scrapers.cimalight_scraper import sync_cimalight_series_to_db
                        await asyncio.to_thread(
                            sync_qisat_series_to_db,
                            "https://v5.qisat.sbs/series/anne-yarisi/",
                            "نصف الأم",
                            "Anne Yarısı",
                            "turkish",
                            2026,
                            True,
                        )
                        await asyncio.to_thread(sync_cimalight_series_to_db, "طالع نازل", "arabic", 2026, True)
                        await asyncio.to_thread(sync_cimalight_series_to_db, "دافنينه سوا", "arabic", 2026, True)
                        if SERIES_VIRAL_PRIORITY_ENABLED:
                            from worker.scrapers.viral_series_scraper import sync_viral_and_top_rated_series_sync
                            await asyncio.to_thread(sync_viral_and_top_rated_series_sync, 15)
                        if MOVIE_VIRAL_PRIORITY_ENABLED:
                            from worker.scrapers.viral_movies_scraper import sync_viral_and_top_rated_movies_sync
                            await asyncio.to_thread(sync_viral_and_top_rated_movies_sync, 24)
                            await crawl_akwam_category("anime-movies", max_pages=2)
                            await crawl_akwam_category("anime-series", max_pages=2)
                            if cycle_count % SYNC_EVERY == 0:
                                await crawl_akwam_category("foreign-movies", max_pages=2)
                                await crawl_akwam_category("arabic-movies", max_pages=1)
                        if cycle_count % SYNC_EVERY == 0:
                            await asyncio.to_thread(sync_cimalight_series_to_db, "أحلى نصيب", "arabic", 2026, True)
                            if not SERIES_VIRAL_PRIORITY_ENABLED:
                                await crawl_akwam_category("turkish-series", max_pages=2)
                            await asyncio.to_thread(run_enrichment_cycle)
                    except Exception as tr_crawl_err:
                        logger.warning(f"Periodic Viral/VIP sync warning: {tr_crawl_err}")

                if cycle_count % REPORT_EVERY == 0:
                    stats = get_current_stats()
                    send_telegram_notification(
                        f"📊 <b>تقرير الرفع السريع (الدورة #{cycle_count}):</b>\n"
                        f"• إجمالي فيديوهات خزينة تيلجرام: <b>{stats['telegram_real']}</b>\n"
                        f"• إجمالي الأفلام المسجلة: <b>{stats['movies']}</b>\n"
                        f"• إجمالي الحلقات المسجلة: <b>{stats['episodes']}</b>\n"
                        f"• روابط البث النشطة: <b>{stats['streams_active']}</b>\n"
                        f"<i>رفع الأفلام المفيرلة والأعلى تقييماً + المسلسلات المرتبة يعمل بأقصى سرعة (تيلجرام فقط).</i>",
                        level="info"
                    )

                cycle_count += 1
                await asyncio.sleep(ORCH_TICK_SECONDS)
                continue

            logger.info(f"=== Starting Autopilot Cycle #{cycle_count} ===")

            # 1. Immediate Ordered Dispatch FIRST so workers start uploading right away without waiting for crawlers!
            await _dispatch_round(tag=f"cycle#{cycle_count}-start")

            # 2. Data Cleaning & Stale Transit Cleanup
            try:
                transit_root = Path(settings.TRANSIT_TEMP_DIR)
                if transit_root.exists():
                    now_ts = time.time()
                    for d in transit_root.iterdir():
                        if d.is_dir() and d.name.startswith("task_") and (now_ts - d.stat().st_mtime > 5400):
                            shutil.rmtree(d, ignore_errors=True)
                run_bulk_domain_cleaner()
                clean_bad_database_records()
            except Exception as audit_err:
                logger.error(f"Audit cycle error: {audit_err}")

            # 3. Metadata Enrichment (Continuous TMDB HD Posters, Genres & Arabic Overview)
            try:
                await asyncio.to_thread(run_enrichment_cycle)
            except Exception as enrich_err:
                logger.error(f"Metadata enrichment error: {enrich_err}")

            # 4. Crawl Akwam Categories while periodically topping up ordered upload queue
            for idx, (cat_key, cat_info) in enumerate(AKWAM_CATEGORIES.items(), 1):
                logger.info(f"Orchestrator crawling Akwam: {cat_info['name']} ({cat_key})")
                try:
                    await crawl_akwam_category(cat_key, max_pages=2)
                except Exception as akwam_err:
                    logger.error(f"Error in Akwam category {cat_key}: {akwam_err}")
                if idx % 3 == 0:
                    await _dispatch_round(tag=f"cycle#{cycle_count}-mid")
                await asyncio.sleep(1.5)

            # 5. Crawl Cima4u (Secondary Source with Circuit Breaker)
            cima_blocked = False
            for cat_key, cat_info in CIMA_CATEGORIES.items():
                if cima_blocked:
                    break
                logger.info(f"Orchestrator crawling Cima4u: {cat_info['name']} ({cat_key})")
                try:
                    await crawl_cima_category(cat_key, max_pages=1)
                except Exception as cat_err:
                    err_text = str(cat_err)
                    logger.error(f"Error in Cima4u category {cat_key}: {cat_err}")
                    if "403" in err_text or "Forbidden" in err_text:
                        cima_blocked = True
                        logger.warning("Cima4u is protected by Cloudflare (403). Pausing Cima4u categories for this cycle.")
                await asyncio.sleep(1.5)

            # 6. End-of-cycle Ordered Dispatch
            await _dispatch_round(tag=f"cycle#{cycle_count}-end")

            # 7. Periodic stats to Telegram
            stats = get_current_stats()
            if cycle_count % 2 == 0 or cycle_count == 1:
                send_telegram_notification(
                    f"📊 <b>تقرير الإحصائيات وجودة البيانات (الدورة #{cycle_count}):</b>\n"
                    f"• إجمالي المسلسلات: <b>{stats['series']}</b>\n"
                    f"• إجمالي الحلقات: <b>{stats['episodes']}</b>\n"
                    f"• إجمالي الأفلام السليمة: <b>{stats['movies']}</b>\n"
                    f"• فيديوهات الخزينة الحقيقية (Telegram Vault): <b>{stats['telegram_real']}</b>\n"
                    f"• روابط البث النشطة والصالحة: <b>{stats['streams_active']}</b>\n"
                    f"• روابط ميتة تم عزلها وفلترتها: <b>{stats['streams_dead']}</b>\n\n"
                    f"<i>الرفع المتسلسل المنظم نشط 24/7.</i>",
                    level="info"
                )

            cycle_count += 1
            logger.info("Cycle completed. Sleeping 3 minutes before next round...")
            await asyncio.sleep(180)

        except Exception as exc:
            logger.critical(f"Orchestrator encountered critical loop error: {exc}")
            send_telegram_notification(
                f"<b>تنبيه عطل في المحرك الرئيسي:</b>\n<code>{str(exc)[:300]}</code>\nسيتم إعادة التشغيل الذاتي خلال 60 ثانية.",
                level="error"
            )
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(run_forever())
