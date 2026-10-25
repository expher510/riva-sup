"""
Riva 24/7 Autopilot Orchestrator.
Runs in the background indefinitely, crawling all categories, dispatching uploads,
monitoring health, and sending instant Telegram notifications for errors/bans/milestones.
"""
import time
import logging
import asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from worker.telegram_vault import send_telegram_notification
from worker.scrapers.cima4u_crawler import CATEGORIES as CIMA_CATEGORIES, crawl_category as crawl_cima_category
from worker.scrapers.akwam_crawler import CATEGORIES as AKWAM_CATEGORIES, crawl_akwam_category
from worker.batch_dispatcher import dispatch_pending_episodes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("riva_orchestrator")

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True
)
SyncSession = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)


def get_current_stats():
    with SyncSession() as session:
        series_cnt = session.execute(text("SELECT count(*) FROM series;")).scalar() or 0
        movies_cnt = session.execute(text("SELECT count(*) FROM movies;")).scalar() or 0
        episodes_cnt = session.execute(text("SELECT count(*) FROM episodes;")).scalar() or 0
        streams_cnt = session.execute(text("SELECT count(*) FROM streaming_links;")).scalar() or 0
        tg_cnt = session.execute(text("SELECT count(*) FROM telegram_archives;")).scalar() or 0
        return {
            "series": series_cnt,
            "movies": movies_cnt,
            "episodes": episodes_cnt,
            "streams": streams_cnt,
            "telegram": tg_cnt
        }


async def run_forever():
    logger.info("Starting Riva 24/7 Autopilot Orchestrator...")
    send_telegram_notification(
        "🚀 <b>تم تفعيل محرك Riva Autopilot المتكامل (العمل التلقائي 24/7):</b>\n"
        "النظام يعمل الآن آلياً عبر مصادر متعددة لسحب الأفلام والمسلسلات ورفعها إلى الخزينة وسيرفرات البث.",
        level="success"
    )

    cycle_count = 1
    while True:
        try:
            logger.info(f"=== Starting Autopilot Cycle #{cycle_count} ===")

            # 1. Crawl Akwam (Primary Unblocked Source)
            for cat_key, cat_info in AKWAM_CATEGORIES.items():
                logger.info(f"Orchestrator crawling Akwam: {cat_info['name']} ({cat_key})")
                try:
                    await crawl_akwam_category(cat_key, max_pages=3)
                except Exception as akwam_err:
                    logger.error(f"Error in Akwam category {cat_key}: {akwam_err}")
                await asyncio.sleep(2.0)

            # 2. Crawl Cima4u (Secondary Source)
            for cat_key, cat_info in CIMA_CATEGORIES.items():
                logger.info(f"Orchestrator crawling Cima4u: {cat_info['name']} ({cat_key})")
                try:
                    await crawl_cima_category(cat_key, max_pages=2)
                except Exception as cat_err:
                    err_text = str(cat_err)
                    logger.error(f"Error in Cima4u category {cat_key}: {cat_err}")
                    if "403" in err_text or "Forbidden" in err_text:
                        send_telegram_notification(
                            f"<b>تنبيه حظر/حماية على قسم {cat_info['name']}:</b>\n<code>{err_text[:200]}</code>\nسيتم التخطي للقسم التالي.",
                            level="ban"
                        )
                await asyncio.sleep(2.0)

            # 3. Dispatch any pending items in DB that haven't been uploaded yet
            dispatched = dispatch_pending_episodes(limit=50)
            logger.info(f"Cycle #{cycle_count} dispatched {dispatched} pending tasks to Celery.")

            # 4. Send periodic stats to Telegram every 2 cycles
            stats = get_current_stats()
            if cycle_count % 2 == 0 or cycle_count == 1:
                send_telegram_notification(
                    f"📊 <b>تقرير الإحصائيات الدوري (الدورة #{cycle_count}):</b>\n"
                    f"• إجمالي المسلسلات: <b>{stats['series']}</b>\n"
                    f"• إجمالي الحلقات: <b>{stats['episodes']}</b>\n"
                    f"• إجمالي الأفلام: <b>{stats['movies']}</b>\n"
                    f"• إجمالي الفيديوهات في الخزينة: <b>{stats['telegram']}</b>\n"
                    f"• روابط البث النشطة: <b>{stats['streams']}</b>\n\n"
                    f"<i>المحرك يعمل في الخلفية 24/7 دون توقف.</i>",
                    level="info"
                )

            cycle_count += 1
            logger.info("Cycle completed. Sleeping 5 minutes before next round...")
            await asyncio.sleep(300)

        except Exception as exc:
            logger.critical(f"Orchestrator encountered critical loop error: {exc}")
            send_telegram_notification(
                f"<b>تنبيه عطل في المحرك الرئيسي:</b>\n<code>{str(exc)[:300]}</code>\nسيتم إعادة التشغيل الذاتي خلال 60 ثانية.",
                level="error"
            )
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(run_forever())
