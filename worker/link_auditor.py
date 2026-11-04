"""
Link Auditor & Data Quality Cleaner for Riva VOD Engine.
Continuously filters dead cyberlocker links, purges fake archives,
deduplicates records, and maintains a pristine database.
"""
import asyncio
import aiohttp
import re
import logging
from typing import List, Tuple, Dict, Any
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

logger = logging.getLogger("link_auditor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

sync_engine = create_engine(
    settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True
)
SyncSession = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)

# Known dead domains, defunct servers, or paywalled cyberlockers (not streamable)
DEAD_OR_UNSUPPORTED_DOMAINS = [
    "1fichier.com",
    "openload.co",
    "estream.to",
    "streamsb.com",
    "forafile.com",
    "megaup.net",
    "1cloudfile.com",
    "bowfile.com",
    "send.now",
    "smoothpre.com",
    "minochinos.com",
    "streamruby.com",
    "stmruby.com",
    "rapidgator.net",
    "nitroflare.com",
    "luluvido.com",
    "vidmoly.biz",
]

# Regex patterns in HTML response bodies indicating deleted, removed, or takedown files
BODY_DEAD_PATTERNS = [
    re.compile(r'file (?:was )?deleted', re.I),
    re.compile(r'file not found', re.I),
    re.compile(r'file is no longer available', re.I),
    re.compile(r'video (?:has been )?removed', re.I),
    re.compile(r'this file was removed', re.I),
    re.compile(r'404 not found', re.I),
    re.compile(r'copyright (?:infringement|violation)', re.I),
    re.compile(r'we (?:could not|can\'t|cannot) find the video', re.I),
    re.compile(r'we are sorry.*find the video', re.I | re.S),
    re.compile(r'file does not exist', re.I),
]

PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://akwams.org/"
}


def run_bulk_domain_cleaner() -> int:
    """
    Instantly marks links belonging to known dead, defunct, or non-streaming domains as 'dead'.
    Also cleans malformed or self-referential links.
    """
    total_marked = 0
    with SyncSession() as session:
        for domain in DEAD_OR_UNSUPPORTED_DOMAINS:
            res = session.execute(text("""
                UPDATE streaming_links
                SET status = 'dead', last_checked_at = now()
                WHERE status = 'active'
                  AND (coalesce(direct_url, embed_url) LIKE :pattern);
            """), {"pattern": f"%{domain}%"})
            if res.rowcount > 0:
                logger.info(f"🧹 Bulk Cleaner marked {res.rowcount} links from defunct/unsupported host '{domain}' as DEAD.")
                total_marked += res.rowcount

        # Mark malformed links (no protocol or self-referencing without archive)
        res_bad = session.execute(text("""
            UPDATE streaming_links
            SET status = 'dead', last_checked_at = now()
            WHERE status = 'active'
              AND (
                  (direct_url IS NULL AND embed_url IS NULL)
                  OR coalesce(direct_url, embed_url) NOT LIKE 'http%'
                  OR coalesce(direct_url, embed_url) LIKE '%placeholder%'
                  OR coalesce(direct_url, embed_url) LIKE '%undefined%'
              );
        """))
        if res_bad.rowcount > 0:
            logger.info(f"🧹 Bulk Cleaner marked {res_bad.rowcount} malformed/placeholder links as DEAD.")
            total_marked += res_bad.rowcount

        session.commit()
    return total_marked


def clean_bad_database_records() -> Dict[str, int]:
    """
    Cleans corrupted movies, purges fake/tiny archives from Telegram Vault,
    and deduplicates archive rows.
    """
    stats = {}
    with SyncSession() as session:
        # 1. Purge fake Telegram archives (< 15MB or NULL)
        res_fake = session.execute(text("""
            DELETE FROM telegram_archives 
            WHERE file_size_bytes < 15728640 OR file_size_bytes IS NULL;
        """))
        stats["fake_archives_purged"] = res_fake.rowcount

        # 2. Deduplicate episode archives (keep latest)
        res_dup_ep = session.execute(text("""
            DELETE FROM telegram_archives ta1 USING telegram_archives ta2
            WHERE ta1.episode_id IS NOT NULL 
              AND ta1.episode_id = ta2.episode_id 
              AND ta1.id < ta2.id;
        """))
        stats["duplicate_episodes_purged"] = res_dup_ep.rowcount

        # 3. Deduplicate movie archives (keep latest)
        res_dup_m = session.execute(text("""
            DELETE FROM telegram_archives ta1 USING telegram_archives ta2
            WHERE ta1.movie_id IS NOT NULL 
              AND ta1.movie_id = ta2.movie_id 
              AND ta1.id < ta2.id;
        """))
        stats["duplicate_movies_purged"] = res_dup_m.rowcount

        # 4. Delete corrupted movies with title 'مشاهدة' or empty
        res_corrupt_movies = session.execute(text("""
            DELETE FROM movies 
            WHERE title = 'مشاهدة' OR title IS NULL OR trim(title) = '';
        """))
        stats["corrupt_movies_deleted"] = res_corrupt_movies.rowcount

        # 5. Delete orphan streaming links
        res_orphans = session.execute(text("""
            DELETE FROM streaming_links 
            WHERE movie_id IS NULL AND episode_id IS NULL;
        """))
        stats["orphan_links_deleted"] = res_orphans.rowcount

        session.commit()

    if any(v > 0 for v in stats.values()):
        logger.info(f"✨ Database Quality Review: {stats}")
    return stats


async def _probe_single_link(client: aiohttp.ClientSession, link_id: int, url: str) -> Tuple[int, bool, str]:
    """Inspects a single link via async HTTP GET and checks for dead status / body patterns."""
    if not url or not url.startswith("http"):
        return link_id, False, "invalid_url"

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with client.get(url, headers=PROBE_HEADERS, timeout=timeout, ssl=False, allow_redirects=True) as resp:
            if resp.status in [404, 410]:
                return link_id, False, f"http_{resp.status}"
            if resp.status >= 500:
                return link_id, False, f"server_error_{resp.status}"

            # Inspect first 8KB of response
            content_chunk = await resp.content.read(8192)
            decoded = content_chunk.decode("utf-8", errors="ignore")
            for pat in BODY_DEAD_PATTERNS:
                if pat.search(decoded):
                    return link_id, False, f"body_match:{pat.pattern}"

            return link_id, True, "live"
    except asyncio.TimeoutError:
        return link_id, False, "timeout"
    except Exception as exc:
        return link_id, False, f"error:{type(exc).__name__}"


async def audit_active_links_batch(batch_size: int = 100) -> Dict[str, int]:
    """
    Audits a batch of active links in the database asynchronously.
    Updates dead links to status='dead' and refreshes last_checked_at.
    """
    with SyncSession() as session:
        rows = session.execute(text("""
            SELECT id, coalesce(direct_url, embed_url) as u
            FROM streaming_links
            WHERE status = 'active'
              AND (direct_url IS NOT NULL OR embed_url IS NOT NULL)
            ORDER BY last_checked_at ASC NULLS FIRST
            LIMIT :lim;
        """), {"lim": batch_size}).fetchall()

    if not rows:
        return {"audited": 0, "live": 0, "dead": 0}

    logger.info(f"Auditing batch of {len(rows)} active links across cyberlockers...")
    conn = aiohttp.TCPConnector(limit=30, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as client:
        tasks = [_probe_single_link(client, r[0], r[1]) for r in rows]
        results = await asyncio.gather(*tasks)

    dead_ids = [r[0] for r in results if not r[1]]
    live_ids = [r[0] for r in results if r[1]]

    with SyncSession() as session:
        if dead_ids:
            session.execute(text("""
                UPDATE streaming_links 
                SET status = 'dead', last_checked_at = now() 
                WHERE id = ANY(:ids);
            """), {"ids": dead_ids})

        if live_ids:
            session.execute(text("""
                UPDATE streaming_links 
                SET last_checked_at = now() 
                WHERE id = ANY(:ids);
            """), {"ids": live_ids})

        session.commit()

    logger.info(f"Batch audit completed: {len(live_ids)} live, {len(dead_ids)} DEAD marked.")
    return {"audited": len(results), "live": len(live_ids), "dead": len(dead_ids)}


def get_health_stats() -> Dict[str, Any]:
    """Returns database health metrics regarding clean vs bad links and archives."""
    with SyncSession() as session:
        total_links = session.execute(text("SELECT count(*) FROM streaming_links;")).scalar() or 0
        active_links = session.execute(text("SELECT count(*) FROM streaming_links WHERE status = 'active';")).scalar() or 0
        dead_links = session.execute(text("SELECT count(*) FROM streaming_links WHERE status = 'dead';")).scalar() or 0
        real_archives = session.execute(text("SELECT count(*) FROM telegram_archives WHERE file_size_bytes >= 15728640;")).scalar() or 0
        fake_archives = session.execute(text("SELECT count(*) FROM telegram_archives WHERE file_size_bytes < 15728640 OR file_size_bytes IS NULL;")).scalar() or 0

        return {
            "total_links": total_links,
            "active_links": active_links,
            "dead_links": dead_links,
            "real_vault_archives": real_archives,
            "fake_archives": fake_archives
        }
