import asyncio
import csv
import html as html_lib
import io
import json
import os
import re
import secrets
from datetime import datetime
from typing import List, Optional

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.rate_limiter import get_redis

admin_router = APIRouter(tags=["Bot Admin & Monitoring"])

ADMIN_PASSWORD = os.getenv("BOT_ADMIN_PASSWORD", "")
PUBLIC_REDIRECT_BASE = os.getenv("PUBLIC_REDIRECT_BASE_URL", "http://186.240.145.87:8090").rstrip("/")
BOT_USERNAME_PUBLIC = os.getenv("BOT_USERNAME", "riva18_bot").lstrip("@")
_B36_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz"
_CRAWLER_UA_RE = re.compile(
    r"(TelegramBot|WhatsApp|facebookexternalhit|Twitterbot|Discordbot|Slackbot|LinkedInBot|Pinterest)",
    re.IGNORECASE,
)
_schema_initialized = False


def _int_to_b36(num: int) -> str:
    n = abs(int(num))
    if n == 0:
        return "0"
    out = []
    while n > 0:
        n, r = divmod(n, 36)
        out.append(_B36_CHARS[r])
    return "".join(reversed(out))


def _b36_to_int(s: str) -> Optional[int]:
    val = (s or "").strip().lower()
    if not val or not val.isalnum():
        return None
    try:
        return int(val, 36)
    except ValueError:
        return None


def _encode_aff_code(target_type: str, target_id: Optional[int] = None, referrer_id: Optional[int] = None) -> str:
    pmap = {"movie": "m", "series": "s", "episode": "e", "bot": "u"}
    p = pmap.get(target_type, "u")
    b36 = _int_to_b36(int(referrer_id)) if referrer_id and int(referrer_id) > 0 else None
    if p == "u":
        return f"u-{b36}" if b36 else "u"
    tid = int(target_id or 0)
    return f"{p}-{tid}-{b36}" if b36 else f"{p}-{tid}"


def _decode_aff_code(code: str) -> dict:
    raw = (code or "").strip()
    if not raw:
        return {"target_type": "bot", "target_id": None, "referrer_id": None, "tg_start": ""}

    if "_" in raw and "-" not in raw:
        m_ref = re.match(r"^(m|s|ep)_(\d+)(?:_r_(\d+))?$", raw)
        if m_ref:
            pref, tid_s, ref_s = m_ref.group(1), m_ref.group(2), m_ref.group(3)
            ttype = {"m": "movie", "s": "series", "ep": "episode"}[pref]
            return {
                "target_type": ttype,
                "target_id": int(tid_s),
                "referrer_id": int(ref_s) if ref_s else None,
                "tg_start": raw,
            }
        u_ref = re.match(r"^ref_(\d+)$", raw)
        if u_ref:
            return {
                "target_type": "bot",
                "target_id": None,
                "referrer_id": int(u_ref.group(1)),
                "tg_start": raw,
            }
        return {"target_type": "bot", "target_id": None, "referrer_id": None, "tg_start": raw[:60]}

    parts = raw.split("-")
    pref = parts[0].lower()
    if pref == "u":
        ref_id = _b36_to_int(parts[1]) if len(parts) >= 2 else None
        return {
            "target_type": "bot",
            "target_id": None,
            "referrer_id": ref_id,
            "tg_start": f"ref_{ref_id}" if ref_id else "",
        }

    tmap = {"m": ("movie", "m"), "s": ("series", "s"), "e": ("episode", "ep")}
    if pref in tmap and len(parts) >= 2 and parts[1].isdigit():
        ttype, tg_pref = tmap[pref]
        tid = int(parts[1])
        ref_id = _b36_to_int(parts[2]) if len(parts) >= 3 else None
        tg_start = f"{tg_pref}_{tid}_r_{ref_id}" if ref_id else f"{tg_pref}_{tid}"
        return {
            "target_type": ttype,
            "target_id": tid,
            "referrer_id": ref_id,
            "tg_start": tg_start,
        }

    return {"target_type": "bot", "target_id": None, "referrer_id": None, "tg_start": ""}


def _get_effective_public_base(request: Optional[Request] = None) -> str:
    if os.getenv("PUBLIC_REDIRECT_BASE_URL"):
        return os.getenv("PUBLIC_REDIRECT_BASE_URL", PUBLIC_REDIRECT_BASE).rstrip("/")
    if request is not None:
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
        scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").strip()
        if host and not host.startswith(("127.0.0.1", "localhost")):
            return f"{scheme}://{host}".rstrip("/")
    return PUBLIC_REDIRECT_BASE


async def ensure_bot_admin_schema(db: AsyncSession):
    """Idempotently ensures bot_users, bot_user_events, and bot_settings have all compliance & interaction columns."""
    global _schema_initialized
    if _schema_initialized:
        return
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS required_channels (
            id SERIAL PRIMARY KEY,
            chat_id VARCHAR(120) UNIQUE NOT NULL,
            title VARCHAR(255) NOT NULL,
            invite_link VARCHAR(500) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id BIGINT PRIMARY KEY,
            username VARCHAR(150),
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            language_code VARCHAR(20),
            is_active BOOLEAN DEFAULT TRUE,
            status VARCHAR(30) DEFAULT 'active',
            joined_at TIMESTAMP DEFAULT NOW(),
            last_active_at TIMESTAMP DEFAULT NOW(),
            left_at TIMESTAMP NULL,
            rejoined_at TIMESTAMP NULL,
            actions_count INTEGER DEFAULT 1,
            last_action VARCHAR(255),
            rules_status VARCHAR(30) DEFAULT 'unknown',
            joined_channels_count INTEGER DEFAULT 0,
            required_channels_count INTEGER DEFAULT 0,
            joined_channels_json TEXT DEFAULT '[]',
            rules_checked_at TIMESTAMP NULL,
            force_sub_blocks_count INTEGER DEFAULT 0,
            force_sub_passed_at TIMESTAMP NULL,
            movies_watched_count INTEGER DEFAULT 0,
            episodes_watched_count INTEGER DEFAULT 0,
            searches_count INTEGER DEFAULT 0,
            browses_count INTEGER DEFAULT 0
        )
        """,
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_name VARCHAR(255)",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS language_code VARCHAR(20)",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS status VARCHAR(30) DEFAULT 'active'",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS left_at TIMESTAMP NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS rejoined_at TIMESTAMP NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS actions_count INTEGER DEFAULT 1",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_action VARCHAR(255)",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS rules_status VARCHAR(30) DEFAULT 'unknown'",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS joined_channels_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS required_channels_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS joined_channels_json TEXT DEFAULT '[]'",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS rules_checked_at TIMESTAMP NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS force_sub_blocks_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS force_sub_passed_at TIMESTAMP NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS movies_watched_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS episodes_watched_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS searches_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS browses_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS total_referrals_entered INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS qualified_referrals_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_by BIGINT NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_via_code VARCHAR(120) NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_target_type VARCHAR(30) NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_target_id INTEGER NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_target_title VARCHAR(300) NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referral_qualified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referred_at TIMESTAMP NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referral_qualified_at TIMESTAMP NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS entry_source VARCHAR(30) NULL",
        """
        CREATE TABLE IF NOT EXISTS affiliate_referrals (
            id SERIAL PRIMARY KEY,
            referrer_user_id BIGINT NOT NULL,
            referred_user_id BIGINT UNIQUE NOT NULL,
            referral_code VARCHAR(120) NOT NULL,
            target_type VARCHAR(30) NOT NULL DEFAULT 'bot',
            target_id INTEGER NULL,
            target_title VARCHAR(300) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            points_awarded INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            qualified_at TIMESTAMP NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_aff_ref_referrer ON affiliate_referrals(referrer_user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_aff_ref_status ON affiliate_referrals(status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bot_users_points ON bot_users(points DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bot_users_referred_by ON bot_users(referred_by)",
        """
        CREATE TABLE IF NOT EXISTS affiliate_point_transactions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL DEFAULT 0,
            reason VARCHAR(300) NOT NULL,
            related_user_id BIGINT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_aff_pt_tx_uid ON affiliate_point_transactions(user_id, created_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS bot_user_events (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            username VARCHAR(150),
            first_name VARCHAR(255),
            event_type VARCHAR(40) NOT NULL,
            details VARCHAR(500),
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_bot_user_events_created_at ON bot_user_events(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bot_user_events_type ON bot_user_events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_bot_user_events_uid ON bot_user_events(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bot_users_is_active ON bot_users(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_bot_users_last_active ON bot_users(last_active_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS bot_settings (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        INSERT INTO bot_settings (key, value)
        VALUES ('notify_admin_events', '1')
        ON CONFLICT (key) DO NOTHING
        """,
        """
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
        )
        """,
        # Backfill typed counters from any historical bot_user_events rows if still 0
        """
        UPDATE bot_users u
        SET movies_watched_count = GREATEST(
                COALESCE(u.movies_watched_count, 0),
                (SELECT COUNT(*) FROM bot_user_events e WHERE e.user_id = u.user_id AND e.event_type = 'watch_movie')
            ),
            episodes_watched_count = GREATEST(
                COALESCE(u.episodes_watched_count, 0),
                (SELECT COUNT(*) FROM bot_user_events e WHERE e.user_id = u.user_id AND e.event_type = 'watch_episode')
            ),
            searches_count = GREATEST(
                COALESCE(u.searches_count, 0),
                (SELECT COUNT(*) FROM bot_user_events e WHERE e.user_id = u.user_id AND e.event_type = 'search')
            ),
            browses_count = GREATEST(
                COALESCE(u.browses_count, 0),
                (SELECT COUNT(*) FROM bot_user_events e WHERE e.user_id = u.user_id AND e.event_type = 'browse')
            )
        WHERE EXISTS (SELECT 1 FROM bot_user_events e WHERE e.user_id = u.user_id)
        """,
        """
        UPDATE bot_users
        SET entry_source = 'affiliate'
        WHERE referred_by IS NOT NULL AND entry_source IS DISTINCT FROM 'affiliate'
        """,
        """
        WITH first_instant_watch AS (
            SELECT DISTINCT ON (u.user_id)
                u.user_id,
                e.event_type,
                TRIM(REGEXP_REPLACE(COALESCE(e.details, ''), '^(🎬\\s*شاهد فيلم:\\s*|📺\\s*شاهد:\\s*)', '')) AS raw_title
            FROM bot_users u
            JOIN bot_user_events e
              ON e.user_id = u.user_id
             AND e.event_type IN ('watch_movie', 'watch_episode')
             AND e.created_at BETWEEN u.joined_at - INTERVAL '2 seconds' AND u.joined_at + INTERVAL '6 seconds'
            WHERE u.referred_by IS NULL AND u.entry_source IS NULL
            ORDER BY u.user_id, e.created_at ASC
        )
        UPDATE bot_users u
        SET entry_source = 'direct_link',
            referred_target_type = CASE WHEN fw.event_type = 'watch_movie' THEN 'movie' ELSE 'episode' END,
            referred_target_title = CASE
                WHEN fw.event_type = 'watch_movie' THEN LEFT('فيلم ' || fw.raw_title, 295)
                ELSE LEFT('مسلسل ' || fw.raw_title, 295)
            END,
            referred_via_code = COALESCE(
                (
                    SELECT 'm_' || CAST(m.id AS TEXT)
                    FROM movies m
                    WHERE fw.event_type = 'watch_movie'
                      AND LOWER(m.title) = LOWER(fw.raw_title)
                    ORDER BY m.id DESC
                    LIMIT 1
                ),
                'direct_link'
            )
        FROM first_instant_watch fw
        WHERE u.user_id = fw.user_id
        """,
        """
        UPDATE bot_users
        SET entry_source = 'organic',
            referred_target_title = COALESCE(referred_target_title, 'دخول مباشر للبوت (/start)')
        WHERE referred_by IS NULL AND entry_source IS NULL
        """,
    ]
    for s in stmts:
        await db.execute(text(s))
    await db.commit()
    _schema_initialized = True


def verify_admin_password(
    x_admin_password: Optional[str] = Header(None, alias="X-Admin-Password"),
    password: Optional[str] = Query(None),
):
    candidate = (x_admin_password or password or "").strip()
    expected = os.getenv("BOT_ADMIN_PASSWORD", ADMIN_PASSWORD)
    if not candidate or not expected or not secrets.compare_digest(candidate, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="كلمة سر الأدمن غير صحيحة",
        )
    return True


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat() + ("Z" if dt.tzinfo is None else "")
    return str(dt)


async def _check_telegram_channels_for_user(
    client: httpx.AsyncClient,
    bot_token: str,
    user_id: int,
    required_channels: list[dict],
) -> tuple[str, int, int, list[dict]]:
    """Checks live Telegram membership across all required channels for a single user."""
    total_req = len(required_channels)
    if total_req == 0:
        return "no_rules", 0, 0, []

    api_url = f"https://api.telegram.org/bot{bot_token}/getChatMember"

    async def _one(ch: dict) -> dict:
        joined = True
        try:
            resp = await client.post(api_url, json={"chat_id": ch["chat_id"], "user_id": user_id})
            data = resp.json()
            if data.get("ok"):
                m = data.get("result") or {}
                st = m.get("status", "")
                if st in ("creator", "administrator", "member"):
                    joined = True
                elif st == "restricted" and m.get("is_member") is True:
                    joined = True
                else:
                    joined = False
        except Exception:
            joined = True
        return {
            "chat_id": str(ch["chat_id"]),
            "title": ch["title"],
            "invite_link": ch["invite_link"],
            "joined": joined,
        }

    details = list(await asyncio.gather(*(_one(c) for c in required_channels)))
    joined_cnt = sum(1 for d in details if d["joined"])
    if joined_cnt == total_req:
        r_status = "compliant"
    elif joined_cnt > 0:
        r_status = "partial"
    else:
        r_status = "unsubscribed"
    return r_status, joined_cnt, total_req, details


class AdminLoginRequest(BaseModel):
    password: str


@admin_router.api_route("/r/poster/{media_type}/{media_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_redirect_poster(
    media_type: str,
    media_id: int,
    db: AsyncSession = Depends(get_db),
):
    fallback_img = "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=800&q=80"
    poster_path = None
    if media_type == "movie":
        res = await db.execute(text("SELECT poster_path FROM movies WHERE id = :id;"), {"id": media_id})
        r = res.mappings().first()
        poster_path = r["poster_path"] if r else None
    elif media_type == "series":
        res = await db.execute(text("SELECT poster_path FROM series WHERE id = :id;"), {"id": media_id})
        r = res.mappings().first()
        poster_path = r["poster_path"] if r else None

    if not poster_path:
        return RedirectResponse(url=fallback_img, status_code=302)
    if poster_path.startswith("http://") or poster_path.startswith("https://"):
        return RedirectResponse(url=poster_path, status_code=302)
    if poster_path.startswith("/"):
        return RedirectResponse(url=f"https://image.tmdb.org/t/p/w780{poster_path}", status_code=302)
    if poster_path.startswith("tg_photo:"):
        file_id = poster_path.split(":", 1)[1].strip()
        bot_token = (settings.TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        if bot_token and file_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    gf = await client.get(
                        f"https://api.telegram.org/bot{bot_token}/getFile",
                        params={"file_id": file_id},
                    )
                    gf_data = gf.json()
                    if gf_data.get("ok") and gf_data["result"].get("file_path"):
                        fpath = gf_data["result"]["file_path"]
                        img_r = await client.get(f"https://api.telegram.org/file/bot{bot_token}/{fpath}")
                        if img_r.status_code == 200 and img_r.content:
                            return Response(
                                content=img_r.content,
                                media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400"},
                            )
            except Exception:
                pass
    return RedirectResponse(url=fallback_img, status_code=302)


@admin_router.api_route("/r/{code}", methods=["GET", "HEAD"], include_in_schema=False)
@admin_router.api_route("/s/{code}", methods=["GET", "HEAD"], include_in_schema=False)
async def affiliate_redirect_landing(
    code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Affiliate Redirect Engine:
    - Social crawlers (Telegram/WhatsApp/X/FB) get 200 OK with rich Movie/Series OpenGraph Poster + Title.
    - Real users get an immediate 302 redirect straight into @riva18_bot?start=<target>_r_<referrer_id>,
      where person-level attribution & +1 point qualification occur inside Telegram (zero fake click counting!).
    """
    dec = _decode_aff_code(code)
    tg_start = dec.get("tg_start") or ""
    tg_url = f"https://t.me/{BOT_USERNAME_PUBLIC}?start={tg_start}" if tg_start else f"https://t.me/{BOT_USERNAME_PUBLIC}"

    ua = request.headers.get("user-agent") or ""
    if not _CRAWLER_UA_RE.search(ua):
        return RedirectResponse(
            url=tg_url,
            status_code=302,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    # Social Preview Crawler: build rich OpenGraph card from DB metadata
    base_url = _get_effective_public_base(request)
    og_title = "🎬 شاهد أحدث الأفلام والمسلسلات مجاناً بجودة عالية | Riva Cinema"
    og_desc = "اضغط للمشاهدة المباشرة الفورية داخل البوت بدون أي إعلانات مزعجة 🍿"
    og_img = "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=800&q=80"

    ttype = dec.get("target_type")
    tid = dec.get("target_id")
    try:
        if ttype == "movie" and tid:
            m_res = await db.execute(
                text("SELECT id, title, year, overview, poster_path, category FROM movies WHERE id = :id;"),
                {"id": tid},
            )
            m = m_res.mappings().first()
            if m:
                yr = f" ({m['year']})" if m.get("year") else ""
                ad_tag = " 🔞 (+18)" if (m.get("category") or "") == "adult" else ""
                og_title = f"🎬 شاهد فيلم {m['title']}{yr}{ad_tag} مجاناً بجودة عالية"
                if m.get("overview"):
                    og_desc = str(m["overview"]).strip()[:220]
                pp = m.get("poster_path") or ""
                if pp.startswith(("http://", "https://")):
                    og_img = pp
                elif pp.startswith("/"):
                    og_img = f"https://image.tmdb.org/t/p/w780{pp}"
                elif pp.startswith("tg_photo:"):
                    og_img = f"{base_url}/r/poster/movie/{m['id']}"
        elif ttype == "series" and tid:
            s_res = await db.execute(
                text("SELECT id, title, year, overview, poster_path FROM series WHERE id = :id;"),
                {"id": tid},
            )
            s = s_res.mappings().first()
            if s:
                yr = f" ({s['year']})" if s.get("year") else ""
                og_title = f"📺 شاهد مسلسل {s['title']}{yr} جميع الحلقات بجودة عالية"
                if s.get("overview"):
                    og_desc = str(s["overview"]).strip()[:220]
                pp = s.get("poster_path") or ""
                if pp.startswith(("http://", "https://")):
                    og_img = pp
                elif pp.startswith("/"):
                    og_img = f"https://image.tmdb.org/t/p/w780{pp}"
                elif pp.startswith("tg_photo:"):
                    og_img = f"{base_url}/r/poster/series/{s['id']}"
        elif ttype == "episode" and tid:
            e_res = await db.execute(
                text("""
                    SELECT s.id AS series_id, s.title, s.overview, s.poster_path, sea.season_number, e.episode_number
                    FROM episodes e
                    JOIN seasons sea ON sea.id = e.season_id
                    JOIN series s ON s.id = sea.series_id
                    WHERE e.id = :id;
                """),
                {"id": tid},
            )
            e = e_res.mappings().first()
            if e:
                og_title = f"📺 شاهد مسلسل {e['title']} — الموسم {e['season_number']} الحلقة {e['episode_number']}"
                if e.get("overview"):
                    og_desc = str(e["overview"]).strip()[:220]
                pp = e.get("poster_path") or ""
                if pp.startswith(("http://", "https://")):
                    og_img = pp
                elif pp.startswith("/"):
                    og_img = f"https://image.tmdb.org/t/p/w780{pp}"
                elif pp.startswith("tg_photo:"):
                    og_img = f"{base_url}/r/poster/series/{e['series_id']}"
    except Exception:
        pass

    safe_title = html_lib.escape(og_title)
    safe_desc = html_lib.escape(og_desc)
    safe_img = html_lib.escape(og_img)
    safe_tg = html_lib.escape(tg_url)

    html_doc = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8" />
  <title>{safe_title}</title>
  <meta property="og:type" content="video.movie" />
  <meta property="og:title" content="{safe_title}" />
  <meta property="og:description" content="{safe_desc}" />
  <meta property="og:image" content="{safe_img}" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{safe_title}" />
  <meta name="twitter:description" content="{safe_desc}" />
  <meta name="twitter:image" content="{safe_img}" />
  <meta http-equiv="refresh" content="0;url={safe_tg}" />
  <script>window.location.replace({json.dumps(tg_url)});</script>
</head>
<body style="background:#070b14;color:#fff;font-family:sans-serif;text-align:center;padding:40px;">
  <p>جاري فتح الفيلم مباشرة في تيليجرام... <a href="{safe_tg}" style="color:#60a5fa;">اضغط هنا إذا لم يفتح تلقائياً</a></p>
</body>
</html>"""
    return HTMLResponse(content=html_doc, status_code=200)


@admin_router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
@admin_router.get("/monitor", response_class=HTMLResponse, include_in_schema=False)
async def serve_admin_dashboard():
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    admin_html = os.path.join(static_dir, "admin.html")
    if os.path.exists(admin_html):
        return FileResponse(admin_html)
    return HTMLResponse("<h1>Admin Dashboard UI not found.</h1>", status_code=404)


@admin_router.post("/v1/bot-admin/auth")
async def authenticate_admin(req: AdminLoginRequest):
    expected = os.getenv("BOT_ADMIN_PASSWORD", ADMIN_PASSWORD)
    if not expected or not secrets.compare_digest(req.password.strip(), expected):
        raise HTTPException(status_code=401, detail="كلمة السر غير صحيحة")
    return {"success": True, "bot_username": "riva18_bot"}


@admin_router.get("/v1/bot-admin/overview")
async def get_admin_overview(
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    await ensure_bot_admin_schema(db)

    # 1. User, Compliance Funnel & Typed Interaction KPIs
    u_res = await db.execute(
        text("""
            SELECT
                COUNT(*) AS total_users,
                COUNT(*) FILTER (WHERE is_active = TRUE) AS active_users,
                COUNT(*) FILTER (WHERE is_active = FALSE) AS left_users,
                COUNT(*) FILTER (WHERE is_active = TRUE AND last_active_at >= NOW() - INTERVAL '1 hour') AS online_1h,
                COUNT(*) FILTER (WHERE is_active = TRUE AND last_active_at >= NOW() - INTERVAL '24 hours') AS active_24h,
                COUNT(*) FILTER (WHERE is_active = TRUE AND last_active_at >= NOW() - INTERVAL '7 days') AS active_7d,
                COUNT(*) FILTER (WHERE joined_at >= NOW() - INTERVAL '24 hours') AS joined_today,
                COUNT(*) FILTER (WHERE joined_at >= NOW() - INTERVAL '7 days') AS joined_7d,
                COUNT(*) FILTER (WHERE is_active = FALSE AND left_at >= NOW() - INTERVAL '24 hours') AS left_today,
                COALESCE(SUM(COALESCE(actions_count, 1)), 0) AS total_interactions,
                COUNT(*) FILTER (WHERE rules_status = 'compliant') AS compliant_users,
                COUNT(*) FILTER (WHERE rules_status = 'partial') AS partial_users,
                COUNT(*) FILTER (WHERE rules_status = 'unsubscribed') AS unsubscribed_users,
                COUNT(*) FILTER (WHERE rules_status = 'left_channel') AS left_channel_users,
                COUNT(*) FILTER (WHERE COALESCE(rules_status, 'unknown') = 'unknown') AS unknown_rules_users,
                COUNT(*) FILTER (WHERE COALESCE(movies_watched_count, 0) + COALESCE(episodes_watched_count, 0) >= 1) AS watchers_users,
                COUNT(*) FILTER (WHERE COALESCE(movies_watched_count, 0) + COALESCE(episodes_watched_count, 0) >= 2) AS binge_watchers,
                COALESCE(SUM(COALESCE(movies_watched_count, 0)), 0) AS total_movies_watched,
                COALESCE(SUM(COALESCE(episodes_watched_count, 0)), 0) AS total_episodes_watched,
                COALESCE(SUM(COALESCE(searches_count, 0)), 0) AS total_searches,
                COALESCE(SUM(COALESCE(browses_count, 0)), 0) AS total_browses,
                COALESCE(SUM(COALESCE(force_sub_blocks_count, 0)), 0) AS total_rules_blocks
            FROM bot_users;
        """)
    )
    u_row = dict(u_res.mappings().first() or {})
    total_u = int(u_row.get("total_users") or 0)
    active_u = int(u_row.get("active_users") or 0)
    compliant_u = int(u_row.get("compliant_users") or 0)
    watchers_u = int(u_row.get("watchers_users") or 0)
    binge_u = int(u_row.get("binge_watchers") or 0)

    retention_pct = round((active_u / total_u) * 100.0, 1) if total_u > 0 else 100.0
    compliance_pct = round((compliant_u / total_u) * 100.0, 1) if total_u > 0 else 0.0
    watch_pct = round((watchers_u / total_u) * 100.0, 1) if total_u > 0 else 0.0

    # 2. Required Channels + Per-Channel Membership Breakdown
    ch_res = await db.execute(
        text("SELECT id, chat_id, title, invite_link, created_at FROM required_channels ORDER BY id ASC;")
    )
    required_channels = [
        {
            "id": r["id"],
            "chat_id": str(r["chat_id"]),
            "title": r["title"],
            "invite_link": r["invite_link"],
            "joined_count": 0,
            "checked_count": 0,
            "pct": 0.0,
        }
        for r in ch_res.mappings().all()
    ]
    ch_map = {c["chat_id"]: c for c in required_channels}

    if ch_map:
        uj_res = await db.execute(
            text("SELECT joined_channels_json FROM bot_users WHERE joined_channels_json IS NOT NULL AND joined_channels_json != '[]';")
        )
        for r in uj_res.mappings().all():
            try:
                arr = json.loads(r["joined_channels_json"] or "[]")
                for item in arr:
                    cid = str(item.get("chat_id"))
                    if cid in ch_map:
                        ch_map[cid]["checked_count"] += 1
                        if item.get("joined"):
                            ch_map[cid]["joined_count"] += 1
            except Exception:
                pass
        for c in required_channels:
            if c["checked_count"] > 0:
                c["pct"] = round((c["joined_count"] / c["checked_count"]) * 100.0, 1)

    # 3. 7-Day Daily Joins vs Leaves Chart
    chart_res = await db.execute(
        text("""
            WITH days AS (
                SELECT generate_series(
                     date_trunc('day', NOW()) - INTERVAL '6 days',
                     date_trunc('day', NOW()),
                     INTERVAL '1 day'
                ) AS d
            )
            SELECT
                to_char(days.d, 'MM-DD') AS day_label,
                (SELECT COUNT(*) FROM bot_users u WHERE date_trunc('day', u.joined_at) = days.d) AS joined_count,
                (SELECT COUNT(*) FROM bot_users u WHERE u.left_at IS NOT NULL AND date_trunc('day', u.left_at) = days.d) AS left_count,
                (SELECT COUNT(*) FROM bot_user_events ev WHERE date_trunc('day', ev.created_at) = days.d) AS events_count
            FROM days
            ORDER BY days.d ASC;
        """)
    )
    daily_chart = [
        {
            "day": r["day_label"],
            "joined": int(r["joined_count"] or 0),
            "left": int(r["left_count"] or 0),
            "events": int(r["events_count"] or 0),
        }
        for r in chart_res.mappings().all()
    ]

    # 4. Vault & Upload Pipeline Live Metrics
    v_res = await db.execute(
        text("""
            SELECT
                COUNT(DISTINCT movie_id) FILTER (WHERE movie_id IS NOT NULL AND file_size_bytes > 15000000) AS ready_movies,
                COUNT(DISTINCT episode_id) FILTER (WHERE episode_id IS NOT NULL AND file_size_bytes > 15000000) AS ready_episodes,
                ROUND(COALESCE(SUM(file_size_bytes), 0) / (1024.0 * 1024.0 * 1024.0), 2) AS total_gb,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours' AND file_size_bytes > 15000000) AS uploaded_24h
            FROM telegram_archives;
        """)
    )
    v_row = dict(v_res.mappings().first() or {})

    s_res = await db.execute(
        text("""
            SELECT COUNT(DISTINCT s.id) AS ready_series
            FROM series s
            JOIN seasons sea ON sea.series_id = s.id
            JOIN episodes e ON e.season_id = sea.id
            JOIN telegram_archives ta ON ta.episode_id = e.id AND ta.file_size_bytes > 15000000;
        """)
    )
    ready_series = int((s_res.mappings().first() or {}).get("ready_series") or 0)

    totals_res = await db.execute(
        text("""
            SELECT
                (SELECT COUNT(*) FROM movies) AS total_db_movies,
                (SELECT COUNT(*) FROM series) AS total_db_series,
                (SELECT COUNT(*) FROM episodes) AS total_db_episodes;
        """)
    )
    t_row = dict(totals_res.mappings().first() or {})

    recent_uploads_res = await db.execute(
        text("""
            SELECT
                ta.id,
                ta.message_id,
                CASE WHEN ta.movie_id IS NOT NULL THEN 'movie' ELSE 'episode' END AS media_type,
                COALESCE(
                    m.title,
                    s.title || ' - م' || COALESCE(sea.season_number, 1) || ' ح' || COALESCE(e.episode_number, 1)
                ) AS title,
                ROUND(COALESCE(ta.file_size_bytes, 0) / (1024.0 * 1024.0), 1) AS size_mb,
                ta.created_at
            FROM telegram_archives ta
            LEFT JOIN movies m ON m.id = ta.movie_id
            LEFT JOIN episodes e ON e.id = ta.episode_id
            LEFT JOIN seasons sea ON sea.id = e.season_id
            LEFT JOIN series s ON s.id = sea.series_id
            WHERE ta.file_size_bytes > 15000000
            ORDER BY ta.created_at DESC
            LIMIT 12;
        """)
    )
    recent_uploads = [
        {
            "id": r["id"],
            "message_id": r["message_id"],
            "media_type": r["media_type"],
            "title": r["title"] or "بدون عنوان",
            "size_mb": float(r["size_mb"] or 0),
            "created_at": _iso(r["created_at"]),
        }
        for r in recent_uploads_res.mappings().all()
    ]

    # 5. Active In-Flight Worker Dispatches from Redis
    active_queue = []
    try:
        m_keys = await redis.keys("riva:dispatched:movie:*")
        e_keys = await redis.keys("riva:dispatched:episode:*")
        m_ids = [int(k.split(":")[-1]) for k in m_keys if k.split(":")[-1].isdigit()]
        e_ids = [int(k.split(":")[-1]) for k in e_keys if k.split(":")[-1].isdigit()]

        if m_ids:
            qm = await db.execute(
                text("""
                    SELECT m.id, m.title, m.year
                    FROM movies m
                    WHERE m.id = ANY(:ids)
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_archives ta
                          WHERE ta.movie_id = m.id AND ta.file_size_bytes > 15000000
                      )
                    ORDER BY m.year ASC NULLS LAST, m.id ASC
                    LIMIT 15;
                """),
                {"ids": m_ids},
            )
            for r in qm.mappings().all():
                active_queue.append({
                    "type": "movie",
                    "id": r["id"],
                    "title": f"{r['title']}" + (f" ({r['year']})" if r.get("year") else ""),
                })

        if e_ids:
            qe = await db.execute(
                text("""
                    SELECT e.id, s.title AS series_title, sea.season_number, e.episode_number
                    FROM episodes e
                    JOIN seasons sea ON sea.id = e.season_id
                    JOIN series s ON s.id = sea.series_id
                    WHERE e.id = ANY(:ids)
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_archives ta
                          WHERE ta.episode_id = e.id AND ta.file_size_bytes > 15000000
                      )
                    ORDER BY s.id ASC, sea.season_number ASC, e.episode_number ASC
                    LIMIT 15;
                """),
                {"ids": e_ids},
            )
            for r in qe.mappings().all():
                active_queue.append({
                    "type": "episode",
                    "id": r["id"],
                    "title": f"{r['series_title']} — م{r['season_number']} ح{r['episode_number']}",
                })
    except Exception:
        pass

    set_res = await db.execute(
        text("SELECT value FROM bot_settings WHERE key = 'notify_admin_events';")
    )
    notif_row = set_res.mappings().first()
    notify_enabled = (notif_row["value"] == "1") if notif_row else True

    aff_res = await db.execute(
        text("""
            SELECT
                (SELECT COALESCE(SUM(COALESCE(points, 0)), 0) FROM bot_users) AS total_points,
                (SELECT COUNT(*) FROM bot_users WHERE COALESCE(points, 0) > 0 OR COALESCE(total_referrals_entered, 0) >= 1) AS active_affiliates,
                COUNT(*) AS total_entered,
                COUNT(*) FILTER (WHERE status = 'qualified') AS qualified_referrals,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending_referrals
            FROM affiliate_referrals;
        """)
    )
    aff_row = dict(aff_res.mappings().first() or {})
    aff_entered = int(aff_row.get("total_entered") or 0)
    aff_qual = int(aff_row.get("qualified_referrals") or 0)
    aff_conv_pct = round((aff_qual / aff_entered) * 100.0, 1) if aff_entered > 0 else 0.0

    return {
        "success": True,
        "server_time": _iso(datetime.utcnow()),
        "users": {
            "total": total_u,
            "active": active_u,
            "left": int(u_row.get("left_users") or 0),
            "retention_pct": retention_pct,
            "online_1h": int(u_row.get("online_1h") or 0),
            "active_24h": int(u_row.get("active_24h") or 0),
            "active_7d": int(u_row.get("active_7d") or 0),
            "joined_today": int(u_row.get("joined_today") or 0),
            "joined_7d": int(u_row.get("joined_7d") or 0),
            "left_today": int(u_row.get("left_today") or 0),
            "total_interactions": int(u_row.get("total_interactions") or 0),
        },
        "affiliate": {
            "total_points": int(aff_row.get("total_points") or 0),
            "active_affiliates": int(aff_row.get("active_affiliates") or 0),
            "total_entered": aff_entered,
            "qualified_referrals": aff_qual,
            "pending_referrals": int(aff_row.get("pending_referrals") or 0),
            "conversion_pct": aff_conv_pct,
            "public_redirect_base": f"https://t.me/{BOT_USERNAME_PUBLIC}?start=",
        },
        "funnel": {
            "total_entered": total_u,
            "compliant": compliant_u,
            "partial": int(u_row.get("partial_users") or 0),
            "unsubscribed": int(u_row.get("unsubscribed_users") or 0),
            "left_channel": int(u_row.get("left_channel_users") or 0),
            "unknown": int(u_row.get("unknown_rules_users") or 0),
            "watchers": watchers_u,
            "binge_watchers": binge_u,
            "compliance_pct": compliance_pct,
            "watch_conversion_pct": watch_pct,
            "interaction_totals": {
                "movies_watched": int(u_row.get("total_movies_watched") or 0),
                "episodes_watched": int(u_row.get("total_episodes_watched") or 0),
                "searches": int(u_row.get("total_searches") or 0),
                "browses": int(u_row.get("total_browses") or 0),
                "rules_blocks": int(u_row.get("total_rules_blocks") or 0),
            },
        },
        "daily_chart": daily_chart,
        "vault": {
            "ready_movies": int(v_row.get("ready_movies") or 0),
            "total_db_movies": int(t_row.get("total_db_movies") or 0),
            "ready_series": ready_series,
            "total_db_series": int(t_row.get("total_db_series") or 0),
            "ready_episodes": int(v_row.get("ready_episodes") or 0),
            "total_db_episodes": int(t_row.get("total_db_episodes") or 0),
            "total_gb": float(v_row.get("total_gb") or 0.0),
            "uploaded_24h": int(v_row.get("uploaded_24h") or 0),
            "active_queue": active_queue,
            "recent_uploads": recent_uploads,
        },
        "required_channels": required_channels,
        "settings": {
            "notify_admin_events": notify_enabled,
        },
    }


class AdjustPointsRequest(BaseModel):
    delta: int
    reason: Optional[str] = "تعديل نقاط من لوحة التحكم"
    notify_user: bool = False


@admin_router.get("/v1/bot-admin/affiliates")
async def get_admin_affiliates_dashboard(
    request: Request,
    status_filter: str = Query("all", alias="status"),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)
    base_url = _get_effective_public_base(request)

    # 1. Global Summary + Today's Full Entry Source Breakdown
    sum_res = await db.execute(
        text("""
            SELECT
                (SELECT COALESCE(SUM(COALESCE(points, 0)), 0) FROM bot_users) AS total_points,
                (SELECT COUNT(*) FROM bot_users WHERE COALESCE(points, 0) > 0 OR COALESCE(total_referrals_entered, 0) >= 1) AS active_affiliates,
                (SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours') AS today_total,
                (SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours' AND (referred_by IS NOT NULL OR entry_source = 'affiliate')) AS today_affiliate,
                (SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours' AND referred_by IS NULL AND entry_source = 'direct_link') AS today_direct_link,
                (SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours' AND referred_by IS NULL AND COALESCE(entry_source, 'organic') = 'organic') AS today_organic,
                COUNT(*) AS total_entered,
                COUNT(*) FILTER (WHERE status = 'qualified') AS qualified_referrals,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending_referrals
            FROM affiliate_referrals;
        """)
    )
    s_row = dict(sum_res.mappings().first() or {})
    tot_ent = int(s_row.get("total_entered") or 0)
    tot_qual = int(s_row.get("qualified_referrals") or 0)

    # 2. Top Affiliates Leaderboard
    lb_res = await db.execute(
        text("""
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                u.last_name,
                u.is_active,
                COALESCE(u.points, 0) AS points,
                COALESCE(u.total_referrals_entered, 0) AS total_referrals_entered,
                COALESCE(u.qualified_referrals_count, 0) AS qualified_referrals_count,
                u.last_active_at
            FROM bot_users u
            WHERE COALESCE(u.points, 0) > 0 OR COALESCE(u.total_referrals_entered, 0) >= 1
            ORDER BY COALESCE(u.points, 0) DESC,
                     COALESCE(u.qualified_referrals_count, 0) DESC,
                     COALESCE(u.total_referrals_entered, 0) DESC,
                     u.last_active_at DESC NULLS LAST
            LIMIT 35;
        """)
    )
    leaderboard = []
    for r in lb_res.mappings().all():
        ent = int(r["total_referrals_entered"] or 0)
        qual = int(r["qualified_referrals_count"] or 0)
        code = _encode_aff_code("bot", None, int(r["user_id"]))
        leaderboard.append({
            "user_id": r["user_id"],
            "username": r["username"],
            "first_name": r["first_name"] or "",
            "last_name": r["last_name"] or "",
            "is_active": bool(r["is_active"]),
            "points": int(r["points"] or 0),
            "total_referrals_entered": ent,
            "qualified_referrals_count": qual,
            "pending_count": max(0, ent - qual),
            "conversion_pct": round((qual / ent) * 100.0, 1) if ent > 0 else 0.0,
            "personal_redirect_url": f"https://t.me/{BOT_USERNAME_PUBLIC}?start={code}",
            "last_active_at": _iso(r["last_active_at"]),
        })

    # 3. Top Shared Movies / Series / Links Driving Actual Users (Affiliate + Direct Links)
    top_links_res = await db.execute(
        text("""
            SELECT
                COALESCE(referred_target_type, 'bot') AS target_type,
                referred_target_id AS target_id,
                COALESCE(referred_target_title, 'رابط الدعوة العام للبوت') AS target_title,
                CASE WHEN referred_by IS NOT NULL THEN 'affiliate' ELSE 'direct_link' END AS source_kind,
                COUNT(*) AS entered_count,
                COUNT(*) FILTER (
                    WHERE referral_qualified = TRUE
                       OR COALESCE(movies_watched_count, 0) + COALESCE(episodes_watched_count, 0) >= 1
                ) AS qualified_count
            FROM bot_users
            WHERE referred_by IS NOT NULL OR entry_source = 'direct_link'
            GROUP BY
                COALESCE(referred_target_type, 'bot'),
                referred_target_id,
                COALESCE(referred_target_title, 'رابط الدعوة العام للبوت'),
                CASE WHEN referred_by IS NOT NULL THEN 'affiliate' ELSE 'direct_link' END
            ORDER BY
                CASE WHEN (CASE WHEN referred_by IS NOT NULL THEN 'affiliate' ELSE 'direct_link' END) = 'affiliate' THEN 0 ELSE 1 END ASC,
                COUNT(*) DESC
            LIMIT 10;
        """)
    )
    top_links = [
        {
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "target_title": r["target_title"],
            "source_kind": r["source_kind"],
            "entered_count": int(r["entered_count"] or 0),
            "qualified_count": int(r["qualified_count"] or 0),
        }
        for r in top_links_res.mappings().all()
    ]

    # 4. Paginated Person-by-Person Attribution Table (مين دخل تبع مين وبأي لينك بالظبط — أو كل من دخل اليوم)
    referrals = []
    if status_filter == "today_all":
        conds = ["u.joined_at >= NOW() - INTERVAL '24 hours'"]
        params = {}
        if search and search.strip():
            q = f"%{search.strip()}%"
            conds.append(
                "("
                "CAST(u.user_id AS TEXT) ILIKE :q OR "
                "COALESCE(u.username, '') ILIKE :q OR "
                "COALESCE(u.first_name, '') ILIKE :q OR "
                "COALESCE(u.referred_via_code, '') ILIKE :q OR "
                "COALESCE(u.referred_target_title, '') ILIKE :q OR "
                "COALESCE(ref.username, '') ILIKE :q OR "
                "COALESCE(ref.first_name, '') ILIKE :q"
                ")"
            )
            params["q"] = q
        where_sql = "WHERE " + " AND ".join(conds)
        cnt_res = await db.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM bot_users u
                LEFT JOIN bot_users ref ON ref.user_id = u.referred_by
                {where_sql};
            """),
            params,
        )
        total_refs = int((cnt_res.mappings().first() or {}).get("cnt") or 0)
        params["lim"] = per_page
        params["off"] = (page - 1) * per_page

        td_rows = await db.execute(
            text(f"""
                SELECT
                    u.user_id AS referred_user_id,
                    u.username AS referred_username,
                    u.first_name AS referred_first_name,
                    u.last_name AS referred_last_name,
                    COALESCE(u.is_active, TRUE) AS referred_is_active,
                    COALESCE(u.rules_status, 'unknown') AS referred_rules_status,
                    COALESCE(u.movies_watched_count, 0) AS referred_movies_watched,
                    COALESCE(u.episodes_watched_count, 0) AS referred_episodes_watched,
                    COALESCE(u.searches_count, 0) AS referred_searches,
                    COALESCE(u.actions_count, 1) AS referred_actions_count,
                    u.last_action AS referred_last_action,
                    u.joined_at,
                    COALESCE(u.entry_source, CASE WHEN u.referred_by IS NOT NULL THEN 'affiliate' ELSE 'organic' END) AS entry_source,
                    u.referred_by AS referrer_user_id,
                    COALESCE(ar.referral_code, u.referred_via_code) AS referral_code,
                    COALESCE(ar.target_type, u.referred_target_type, 'bot') AS target_type,
                    COALESCE(ar.target_id, u.referred_target_id) AS target_id,
                    COALESCE(ar.target_title, u.referred_target_title, 'دخول مباشر للبوت (/start)') AS target_title,
                    COALESCE(ar.status, CASE WHEN u.referred_by IS NOT NULL THEN (CASE WHEN u.referral_qualified THEN 'qualified' ELSE 'pending' END) ELSE 'no_referrer' END) AS status,
                    COALESCE(ar.points_awarded, CASE WHEN u.referral_qualified THEN 1 ELSE 0 END) AS points_awarded,
                    COALESCE(ar.created_at, u.joined_at) AS created_at,
                    COALESCE(ar.qualified_at, u.referral_qualified_at) AS qualified_at,
                    ref.username AS referrer_username,
                    ref.first_name AS referrer_first_name,
                    ref.last_name AS referrer_last_name,
                    COALESCE(ref.points, 0) AS referrer_points
                FROM bot_users u
                LEFT JOIN affiliate_referrals ar ON ar.referred_user_id = u.user_id
                LEFT JOIN bot_users ref ON ref.user_id = COALESCE(ar.referrer_user_id, u.referred_by)
                {where_sql}
                ORDER BY u.joined_at DESC NULLS LAST
                LIMIT :lim OFFSET :off;
            """),
            params,
        )
        for r in td_rows.mappings().all():
            ent_src = r["entry_source"] or ("affiliate" if r["referrer_user_id"] else "organic")
            rcode = r["referral_code"] or ""
            redir = f"https://t.me/{BOT_USERNAME_PUBLIC}?start={rcode}" if rcode and rcode != "direct_link" else f"https://t.me/{BOT_USERNAME_PUBLIC}"
            referrals.append({
                "id": r["referred_user_id"],
                "entry_source": ent_src,
                "referral_code": rcode,
                "redirect_url": redir,
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "target_title": r["target_title"],
                "status": r["status"],
                "points_awarded": int(r["points_awarded"] or 0),
                "created_at": _iso(r["created_at"]),
                "qualified_at": _iso(r["qualified_at"]),
                "referred": {
                    "user_id": r["referred_user_id"],
                    "username": r["referred_username"],
                    "first_name": r["referred_first_name"] or "",
                    "last_name": r["referred_last_name"] or "",
                    "is_active": bool(r["referred_is_active"]),
                    "rules_status": r["referred_rules_status"],
                    "movies_watched_count": int(r["referred_movies_watched"] or 0),
                    "episodes_watched_count": int(r["referred_episodes_watched"] or 0),
                    "searches_count": int(r["referred_searches"] or 0),
                    "actions_count": int(r["referred_actions_count"] or 1),
                    "last_action": r["referred_last_action"] or "فتح البوت",
                },
                "referrer": (
                    {
                        "user_id": r["referrer_user_id"],
                        "username": r["referrer_username"],
                        "first_name": r["referrer_first_name"] or "",
                        "last_name": r["referrer_last_name"] or "",
                        "points": int(r["referrer_points"] or 0),
                    }
                    if r["referrer_user_id"]
                    else None
                ),
            })
    else:
        conds = []
        params = {}
        if status_filter == "qualified":
            conds.append("ar.status = 'qualified'")
        elif status_filter == "pending":
            conds.append("ar.status = 'pending'")

        if search and search.strip():
            q = f"%{search.strip()}%"
            conds.append(
                "("
                "CAST(ar.referred_user_id AS TEXT) ILIKE :q OR "
                "CAST(ar.referrer_user_id AS TEXT) ILIKE :q OR "
                "COALESCE(ar.referral_code, '') ILIKE :q OR "
                "COALESCE(ar.target_title, '') ILIKE :q OR "
                "COALESCE(inv.username, '') ILIKE :q OR "
                "COALESCE(inv.first_name, '') ILIKE :q OR "
                "COALESCE(ref.username, '') ILIKE :q OR "
                "COALESCE(ref.first_name, '') ILIKE :q"
                ")"
            )
            params["q"] = q

        where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""

        cnt_res = await db.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM affiliate_referrals ar
                LEFT JOIN bot_users inv ON inv.user_id = ar.referred_user_id
                LEFT JOIN bot_users ref ON ref.user_id = ar.referrer_user_id
                {where_sql};
            """),
            params,
        )
        total_refs = int((cnt_res.mappings().first() or {}).get("cnt") or 0)

        params["lim"] = per_page
        params["off"] = (page - 1) * per_page

        ref_rows_res = await db.execute(
            text(f"""
                SELECT
                    ar.id,
                    ar.referrer_user_id,
                    ar.referred_user_id,
                    ar.referral_code,
                    ar.target_type,
                    ar.target_id,
                    COALESCE(ar.target_title, 'رابط الدعوة العام للبوت') AS target_title,
                    ar.status,
                    COALESCE(ar.points_awarded, 0) AS points_awarded,
                    ar.created_at,
                    ar.qualified_at,
                    inv.username AS referred_username,
                    inv.first_name AS referred_first_name,
                    inv.last_name AS referred_last_name,
                    COALESCE(inv.is_active, TRUE) AS referred_is_active,
                    COALESCE(inv.rules_status, 'unknown') AS referred_rules_status,
                    COALESCE(inv.movies_watched_count, 0) AS referred_movies_watched,
                    COALESCE(inv.episodes_watched_count, 0) AS referred_episodes_watched,
                    COALESCE(inv.searches_count, 0) AS referred_searches,
                    COALESCE(inv.actions_count, 1) AS referred_actions_count,
                    inv.last_action AS referred_last_action,
                    ref.username AS referrer_username,
                    ref.first_name AS referrer_first_name,
                    ref.last_name AS referrer_last_name,
                    COALESCE(ref.points, 0) AS referrer_points
                FROM affiliate_referrals ar
                LEFT JOIN bot_users inv ON inv.user_id = ar.referred_user_id
                LEFT JOIN bot_users ref ON ref.user_id = ar.referrer_user_id
                {where_sql}
                ORDER BY ar.created_at DESC
                LIMIT :lim OFFSET :off;
            """),
            params,
        )

        for r in ref_rows_res.mappings().all():
            rcode = r["referral_code"] or _encode_aff_code(r["target_type"], r["target_id"], r["referrer_user_id"])
            referrals.append({
                "id": r["id"],
                "entry_source": "affiliate",
                "referral_code": rcode,
                "redirect_url": f"https://t.me/{BOT_USERNAME_PUBLIC}?start={rcode}",
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "target_title": r["target_title"],
                "status": r["status"],
                "points_awarded": int(r["points_awarded"] or 0),
                "created_at": _iso(r["created_at"]),
                "qualified_at": _iso(r["qualified_at"]),
                "referred": {
                    "user_id": r["referred_user_id"],
                    "username": r["referred_username"],
                    "first_name": r["referred_first_name"] or "",
                    "last_name": r["referred_last_name"] or "",
                    "is_active": bool(r["referred_is_active"]),
                    "rules_status": r["referred_rules_status"],
                    "movies_watched_count": int(r["referred_movies_watched"] or 0),
                    "episodes_watched_count": int(r["referred_episodes_watched"] or 0),
                    "searches_count": int(r["referred_searches"] or 0),
                    "actions_count": int(r["referred_actions_count"] or 1),
                    "last_action": r["referred_last_action"] or "فتح البوت",
                },
                "referrer": {
                    "user_id": r["referrer_user_id"],
                    "username": r["referrer_username"],
                    "first_name": r["referrer_first_name"] or "",
                    "last_name": r["referrer_last_name"] or "",
                    "points": int(r["referrer_points"] or 0),
                },
            })

    return {
        "success": True,
        "summary": {
            "total_points": int(s_row.get("total_points") or 0),
            "active_affiliates": int(s_row.get("active_affiliates") or 0),
            "total_entered": tot_ent,
            "qualified_referrals": tot_qual,
            "pending_referrals": int(s_row.get("pending_referrals") or 0),
            "conversion_pct": round((tot_qual / tot_ent) * 100.0, 1) if tot_ent > 0 else 0.0,
            "today_total": int(s_row.get("today_total") or 0),
            "today_affiliate": int(s_row.get("today_affiliate") or 0),
            "today_direct_link": int(s_row.get("today_direct_link") or 0),
            "today_organic": int(s_row.get("today_organic") or 0),
            "public_redirect_base": f"https://t.me/{BOT_USERNAME_PUBLIC}?start=",
        },
        "leaderboard": leaderboard,
        "top_links": top_links,
        "referrals": {
            "total": total_refs,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total_refs + per_page - 1) // per_page),
            "items": referrals,
        },
    }


@admin_router.post("/v1/bot-admin/users/{user_id}/points")
async def admin_adjust_user_points(
    user_id: int,
    req: AdjustPointsRequest,
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)
    if req.delta == 0:
        raise HTTPException(status_code=400, detail="يرجى إدخال قيمة نقاط موجبة للإضافة أو سالبة للخصم")

    reason = (req.reason or "تعديل إداري من لوحة التحكم").strip()[:280]
    u_res = await db.execute(
        text("""
            UPDATE bot_users
            SET points = GREATEST(0, COALESCE(points, 0) + :delta)
            WHERE user_id = :uid
            RETURNING user_id, username, first_name, points;
        """),
        {"delta": int(req.delta), "uid": user_id},
    )
    u = u_res.mappings().first()
    if not u:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    new_points = int(u["points"] or 0)
    sign_str = f"+{req.delta}" if req.delta > 0 else str(req.delta)

    await db.execute(
        text("""
            INSERT INTO affiliate_point_transactions (user_id, delta, balance_after, reason, created_at)
            VALUES (:uid, :delta, :bal, :reason, NOW());
        """),
        {"uid": user_id, "delta": int(req.delta), "bal": new_points, "reason": reason},
    )
    await db.execute(
        text("""
            INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
            VALUES (:uid, :un, :fn, 'points_adjusted', :det, NOW());
        """),
        {
            "uid": user_id,
            "un": u["username"],
            "fn": u["first_name"],
            "det": f"⚖️ تعديل نقاط ({sign_str}) — الرصيد الحالي: {new_points} نقطة ({reason})"[:490],
        },
    )
    await db.commit()

    if req.notify_user:
        bot_token = (settings.TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        if bot_token:
            try:
                msg_txt = (
                    f"🎁 <b>تحديث في رصيد نقاطك (Riva Affiliate):</b>\n\n"
                    f"• التغيير: <b>{html_lib.escape(sign_str)} نقطة</b>\n"
                    f"• السبب: {html_lib.escape(reason)}\n"
                    f"• 🏆 رصيدك الحالي: <b>{new_points} نقطة</b>"
                )
                async with httpx.AsyncClient(timeout=8.0) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={"chat_id": user_id, "text": msg_txt, "parse_mode": "HTML"},
                    )
            except Exception:
                pass

    return {"success": True, "user_id": user_id, "points": new_points, "delta": req.delta}


@admin_router.get("/v1/bot-admin/users")
async def list_bot_users(
    status_filter: str = Query("all", alias="status"),
    search: Optional[str] = Query(None),
    sort: str = Query("last_active"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=5, le=100),
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)

    conditions = []
    params = {}

    if status_filter == "active":
        conditions.append("u.is_active = TRUE")
    elif status_filter == "left":
        conditions.append("u.is_active = FALSE")
    elif status_filter == "compliant":
        conditions.append("u.rules_status = 'compliant'")
    elif status_filter == "noncompliant":
        conditions.append("u.rules_status IN ('unsubscribed', 'partial', 'left_channel')")
    elif status_filter == "watchers":
        conditions.append("COALESCE(u.movies_watched_count, 0) + COALESCE(u.episodes_watched_count, 0) >= 1")
    elif status_filter == "binge":
        conditions.append("COALESCE(u.movies_watched_count, 0) + COALESCE(u.episodes_watched_count, 0) >= 2")
    elif status_filter == "affiliates":
        conditions.append("(COALESCE(u.points, 0) > 0 OR COALESCE(u.total_referrals_entered, 0) >= 1)")
    elif status_filter == "referred":
        conditions.append("u.referred_by IS NOT NULL")
    elif status_filter == "online24h":
        conditions.append("u.is_active = TRUE AND u.last_active_at >= NOW() - INTERVAL '24 hours'")
    elif status_filter == "today":
        conditions.append("u.joined_at >= NOW() - INTERVAL '24 hours'")

    if search and search.strip():
        q = f"%{search.strip()}%"
        conditions.append(
            "(CAST(u.user_id AS TEXT) ILIKE :q OR COALESCE(u.username, '') ILIKE :q "
            "OR COALESCE(u.first_name, '') ILIKE :q OR COALESCE(u.last_name, '') ILIKE :q "
            "OR COALESCE(u.referred_target_title, '') ILIKE :q OR COALESCE(ref.username, '') ILIKE :q "
            "OR COALESCE(ref.first_name, '') ILIKE :q)"
        )
        params["q"] = q

    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sort_map = {
        "last_active": "ORDER BY u.last_active_at DESC NULLS LAST",
        "joined": "ORDER BY u.joined_at DESC NULLS LAST",
        "left": "ORDER BY u.left_at DESC NULLS LAST, u.last_active_at DESC NULLS LAST",
        "actions": "ORDER BY (COALESCE(u.movies_watched_count,0)*3 + COALESCE(u.episodes_watched_count,0)*3 + COALESCE(u.actions_count, 1)) DESC, u.last_active_at DESC NULLS LAST",
        "points": "ORDER BY COALESCE(u.points, 0) DESC, COALESCE(u.qualified_referrals_count, 0) DESC, COALESCE(u.total_referrals_entered, 0) DESC, u.last_active_at DESC NULLS LAST",
    }
    order_sql = sort_map.get(sort, sort_map["last_active"])
    if status_filter == "left" and sort == "last_active":
        order_sql = sort_map["left"]
    elif status_filter == "affiliates" and sort == "last_active":
        order_sql = sort_map["points"]

    count_res = await db.execute(
        text(f"""
            SELECT COUNT(*) AS cnt
            FROM bot_users u
            LEFT JOIN bot_users ref ON ref.user_id = u.referred_by
            {where_sql};
        """),
        params,
    )
    total = int((count_res.mappings().first() or {}).get("cnt") or 0)

    offset = (page - 1) * per_page
    params["lim"] = per_page
    params["off"] = offset

    rows_res = await db.execute(
        text(f"""
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                u.last_name,
                u.language_code,
                u.is_active,
                COALESCE(u.status, CASE WHEN u.is_active THEN 'active' ELSE 'blocked' END) AS status,
                u.joined_at,
                u.last_active_at,
                u.left_at,
                u.rejoined_at,
                COALESCE(u.actions_count, 1) AS actions_count,
                u.last_action,
                COALESCE(u.rules_status, 'unknown') AS rules_status,
                COALESCE(u.joined_channels_count, 0) AS joined_channels_count,
                COALESCE(u.required_channels_count, 0) AS required_channels_count,
                COALESCE(u.joined_channels_json, '[]') AS joined_channels_json,
                COALESCE(u.force_sub_blocks_count, 0) AS force_sub_blocks_count,
                COALESCE(u.movies_watched_count, 0) AS movies_watched_count,
                COALESCE(u.episodes_watched_count, 0) AS episodes_watched_count,
                COALESCE(u.searches_count, 0) AS searches_count,
                COALESCE(u.browses_count, 0) AS browses_count,
                COALESCE(u.points, 0) AS points,
                COALESCE(u.total_referrals_entered, 0) AS total_referrals_entered,
                COALESCE(u.qualified_referrals_count, 0) AS qualified_referrals_count,
                u.referred_by,
                COALESCE(u.entry_source, CASE WHEN u.referred_by IS NOT NULL THEN 'affiliate' ELSE 'organic' END) AS entry_source,
                u.referred_via_code,
                u.referred_target_type,
                u.referred_target_id,
                u.referred_target_title,
                COALESCE(u.referral_qualified, FALSE) AS referral_qualified,
                u.referred_at,
                ref.username AS referrer_username,
                ref.first_name AS referrer_first_name,
                ref.last_name AS referrer_last_name
            FROM bot_users u
            LEFT JOIN bot_users ref ON ref.user_id = u.referred_by
            {where_sql}
            {order_sql}
            LIMIT :lim OFFSET :off;
        """),
        params,
    )

    items = []
    for r in rows_res.mappings().all():
        try:
            ch_arr = json.loads(r["joined_channels_json"] or "[]")
        except Exception:
            ch_arr = []
        ref_full = f"{r['referrer_first_name'] or ''} {r['referrer_last_name'] or ''}".strip()
        items.append({
            "user_id": r["user_id"],
            "username": r["username"],
            "first_name": r["first_name"] or "",
            "last_name": r["last_name"] or "",
            "language_code": r["language_code"] or "",
            "is_active": bool(r["is_active"]),
            "status": r["status"],
            "joined_at": _iso(r["joined_at"]),
            "last_active_at": _iso(r["last_active_at"]),
            "left_at": _iso(r["left_at"]),
            "rejoined_at": _iso(r["rejoined_at"]),
            "actions_count": int(r["actions_count"] or 1),
            "last_action": r["last_action"] or "فتح البوت",
            "rules_status": r["rules_status"],
            "joined_channels_count": int(r["joined_channels_count"] or 0),
            "required_channels_count": int(r["required_channels_count"] or 0),
            "channels_list": ch_arr,
            "force_sub_blocks_count": int(r["force_sub_blocks_count"] or 0),
            "movies_watched_count": int(r["movies_watched_count"] or 0),
            "episodes_watched_count": int(r["episodes_watched_count"] or 0),
            "searches_count": int(r["searches_count"] or 0),
            "browses_count": int(r["browses_count"] or 0),
            "points": int(r["points"] or 0),
            "total_referrals_entered": int(r["total_referrals_entered"] or 0),
            "qualified_referrals_count": int(r["qualified_referrals_count"] or 0),
            "referred_by": r["referred_by"],
            "entry_source": r["entry_source"],
            "referrer_username": r["referrer_username"],
            "referrer_name": ref_full or (f"User {r['referred_by']}" if r["referred_by"] else None),
            "referred_via_code": r["referred_via_code"],
            "referred_target_type": r["referred_target_type"],
            "referred_target_id": r["referred_target_id"],
            "referred_target_title": r["referred_target_title"],
            "referral_qualified": bool(r["referral_qualified"]),
            "referred_at": _iso(r["referred_at"]),
        })

    return {
        "success": True,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "items": items,
    }


@admin_router.get("/v1/bot-admin/users/{user_id}/details")
async def get_single_bot_user_details(
    user_id: int,
    request: Request,
    refresh_live: bool = Query(False),
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns complete, human-understandable breakdown of how a user interacted with the bot,
    whether they followed the rules & joined every group, what movies/episodes they watched,
    affiliate attribution (who invited them & via which link + everyone they invited & points),
    and their chronological timeline.
    """
    await ensure_bot_admin_schema(db)
    base_url = _get_effective_public_base(request)

    if refresh_live:
        bot_token = settings.TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token:
            ch_res = await db.execute(text("SELECT id, chat_id, title, invite_link FROM required_channels ORDER BY id ASC;"))
            req_chs = [dict(r) for r in ch_res.mappings().all()]
            async with httpx.AsyncClient(timeout=10.0) as client:
                r_stat, j_cnt, req_cnt, ch_details = await _check_telegram_channels_for_user(
                    client, bot_token, user_id, req_chs
                )
                await db.execute(
                    text("""
                        UPDATE bot_users
                        SET rules_status = :rs,
                            joined_channels_count = :jc,
                            required_channels_count = :rc,
                            joined_channels_json = :jjson,
                            rules_checked_at = NOW()
                        WHERE user_id = :uid;
                    """),
                    {
                        "rs": r_stat,
                        "jc": j_cnt,
                        "rc": req_cnt,
                        "jjson": json.dumps(ch_details, ensure_ascii=False),
                        "uid": user_id,
                    },
                )
                await db.commit()

    u_res = await db.execute(
        text("""
            SELECT
                u.user_id, u.username, u.first_name, u.last_name, u.language_code,
                u.is_active, COALESCE(u.status, 'active') AS status,
                u.joined_at, u.last_active_at, u.left_at, u.rejoined_at,
                COALESCE(u.actions_count, 1) AS actions_count,
                u.last_action,
                COALESCE(u.rules_status, 'unknown') AS rules_status,
                COALESCE(u.joined_channels_count, 0) AS joined_channels_count,
                COALESCE(u.required_channels_count, 0) AS required_channels_count,
                COALESCE(u.joined_channels_json, '[]') AS joined_channels_json,
                u.rules_checked_at,
                COALESCE(u.force_sub_blocks_count, 0) AS force_sub_blocks_count,
                u.force_sub_passed_at,
                COALESCE(u.movies_watched_count, 0) AS movies_watched_count,
                COALESCE(u.episodes_watched_count, 0) AS episodes_watched_count,
                COALESCE(u.searches_count, 0) AS searches_count,
                COALESCE(u.browses_count, 0) AS browses_count,
                COALESCE(u.points, 0) AS points,
                COALESCE(u.total_referrals_entered, 0) AS total_referrals_entered,
                COALESCE(u.qualified_referrals_count, 0) AS qualified_referrals_count,
                u.referred_by,
                COALESCE(u.entry_source, CASE WHEN u.referred_by IS NOT NULL THEN 'affiliate' ELSE 'organic' END) AS entry_source,
                u.referred_via_code,
                u.referred_target_type,
                u.referred_target_id,
                u.referred_target_title,
                COALESCE(u.referral_qualified, FALSE) AS referral_qualified,
                u.referred_at,
                u.referral_qualified_at,
                ref.username AS referrer_username,
                ref.first_name AS referrer_first_name,
                ref.last_name AS referrer_last_name
            FROM bot_users u
            LEFT JOIN bot_users ref ON ref.user_id = u.referred_by
            WHERE u.user_id = :uid;
        """),
        {"uid": user_id},
    )
    row = u_res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    try:
        channels_list = json.loads(row["joined_channels_json"] or "[]")
    except Exception:
        channels_list = []

    ev_res = await db.execute(
        text("""
            SELECT id, event_type, details, created_at
            FROM bot_user_events
            WHERE user_id = :uid
            ORDER BY created_at DESC
            LIMIT 80;
        """),
        {"uid": user_id},
    )
    events = [
        {
            "id": e["id"],
            "event_type": e["event_type"],
            "details": e["details"] or "",
            "created_at": _iso(e["created_at"]),
        }
        for e in ev_res.mappings().all()
    ]

    # Invited users list (people who entered via this user's links)
    inv_res = await db.execute(
        text("""
            SELECT
                ar.referred_user_id,
                ar.referral_code,
                ar.target_type,
                ar.target_id,
                COALESCE(ar.target_title, 'رابط الدعوة العام للبوت') AS target_title,
                ar.status,
                COALESCE(ar.points_awarded, 0) AS points_awarded,
                ar.created_at,
                ar.qualified_at,
                inv.username,
                inv.first_name,
                inv.last_name,
                COALESCE(inv.is_active, TRUE) AS is_active,
                COALESCE(inv.movies_watched_count, 0) AS movies_watched_count,
                COALESCE(inv.episodes_watched_count, 0) AS episodes_watched_count
            FROM affiliate_referrals ar
            LEFT JOIN bot_users inv ON inv.user_id = ar.referred_user_id
            WHERE ar.referrer_user_id = :uid
            ORDER BY ar.created_at DESC
            LIMIT 50;
        """),
        {"uid": user_id},
    )
    invited_users = [
        {
            "user_id": r["referred_user_id"],
            "username": r["username"],
            "first_name": r["first_name"] or "",
            "last_name": r["last_name"] or "",
            "is_active": bool(r["is_active"]),
            "referral_code": r["referral_code"],
            "redirect_url": f"https://t.me/{BOT_USERNAME_PUBLIC}?start={r['referral_code']}",
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "target_title": r["target_title"],
            "status": r["status"],
            "points_awarded": int(r["points_awarded"] or 0),
            "movies_watched_count": int(r["movies_watched_count"] or 0),
            "episodes_watched_count": int(r["episodes_watched_count"] or 0),
            "created_at": _iso(r["created_at"]),
            "qualified_at": _iso(r["qualified_at"]),
        }
        for r in inv_res.mappings().all()
    ]

    tx_res = await db.execute(
        text("""
            SELECT id, delta, balance_after, reason, related_user_id, created_at
            FROM affiliate_point_transactions
            WHERE user_id = :uid
            ORDER BY created_at DESC
            LIMIT 30;
        """),
        {"uid": user_id},
    )
    point_transactions = [
        {
            "id": t["id"],
            "delta": int(t["delta"] or 0),
            "balance_after": int(t["balance_after"] or 0),
            "reason": t["reason"] or "",
            "related_user_id": t["related_user_id"],
            "created_at": _iso(t["created_at"]),
        }
        for t in tx_res.mappings().all()
    ]

    referred_by_info = None
    entry_source = row["entry_source"] or ("affiliate" if row["referred_by"] else "organic")
    if row["referred_by"]:
        ref_nm = f"{row['referrer_first_name'] or ''} {row['referrer_last_name'] or ''}".strip() or f"User {row['referred_by']}"
        rcode = row["referred_via_code"] or ""
        referred_by_info = {
            "entry_source": "affiliate",
            "referrer_user_id": row["referred_by"],
            "referrer_username": row["referrer_username"],
            "referrer_name": ref_nm,
            "referral_code": rcode,
            "redirect_url": f"https://t.me/{BOT_USERNAME_PUBLIC}?start={rcode}" if rcode else "",
            "target_type": row["referred_target_type"],
            "target_id": row["referred_target_id"],
            "target_title": row["referred_target_title"] or "رابط الدعوة العام للبوت",
            "qualified": bool(row["referral_qualified"]),
            "referred_at": _iso(row["referred_at"]),
            "qualified_at": _iso(row["referral_qualified_at"]),
        }
    entry_info = {
        "entry_source": entry_source,
        "referral_code": row["referred_via_code"] or "",
        "target_type": row["referred_target_type"],
        "target_id": row["referred_target_id"],
        "target_title": row["referred_target_title"] or "دخول مباشر للبوت (/start)",
    }

    personal_code = _encode_aff_code("bot", None, user_id)

    watched_movies = [e for e in events if e["event_type"] == "watch_movie"]
    watched_episodes = [e for e in events if e["event_type"] == "watch_episode"]
    searches = [e for e in events if e["event_type"] == "search"]
    browses = [e for e in events if e["event_type"] == "browse"]
    rules_events = [e for e in events if e["event_type"].startswith("rules_")]

    return {
        "success": True,
        "user": {
            "user_id": row["user_id"],
            "username": row["username"],
            "first_name": row["first_name"] or "",
            "last_name": row["last_name"] or "",
            "is_active": bool(row["is_active"]),
            "status": row["status"],
            "joined_at": _iso(row["joined_at"]),
            "last_active_at": _iso(row["last_active_at"]),
            "left_at": _iso(row["left_at"]),
            "rejoined_at": _iso(row["rejoined_at"]),
            "actions_count": int(row["actions_count"] or 1),
            "last_action": row["last_action"] or "فتح البوت",
            "rules_status": row["rules_status"],
            "joined_channels_count": int(row["joined_channels_count"] or 0),
            "required_channels_count": int(row["required_channels_count"] or 0),
            "channels_list": channels_list,
            "rules_checked_at": _iso(row["rules_checked_at"]),
            "force_sub_blocks_count": int(row["force_sub_blocks_count"] or 0),
            "force_sub_passed_at": _iso(row["force_sub_passed_at"]),
            "movies_watched_count": int(row["movies_watched_count"] or 0),
            "episodes_watched_count": int(row["episodes_watched_count"] or 0),
            "searches_count": int(row["searches_count"] or 0),
            "browses_count": int(row["browses_count"] or 0),
            "points": int(row["points"] or 0),
            "total_referrals_entered": int(row["total_referrals_entered"] or 0),
            "qualified_referrals_count": int(row["qualified_referrals_count"] or 0),
        },
        "affiliate": {
            "points": int(row["points"] or 0),
            "total_referrals_entered": int(row["total_referrals_entered"] or 0),
            "qualified_referrals_count": int(row["qualified_referrals_count"] or 0),
            "personal_redirect_url": f"https://t.me/{BOT_USERNAME_PUBLIC}?start={personal_code}",
            "entry_info": entry_info,
            "referred_by_info": referred_by_info,
            "invited_users": invited_users,
            "point_transactions": point_transactions,
        },
        "breakdown": {
            "watched_movies": watched_movies,
            "watched_episodes": watched_episodes,
            "searches": searches,
            "browses": browses,
            "rules_events": rules_events,
        },
        "timeline": events,
    }


@admin_router.get("/v1/bot-admin/events")
async def list_bot_events(
    event_type: str = Query("all"),
    limit: int = Query(45, ge=5, le=150),
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)

    where_sql = ""
    params = {"lim": limit}
    if event_type == "joins_leaves":
        where_sql = "WHERE event_type IN ('joined', 'left', 'rejoined')"
    elif event_type == "rules":
        where_sql = "WHERE event_type IN ('rules_shown', 'rules_failed', 'rules_passed', 'rules_left_channel')"
    elif event_type == "watches":
        where_sql = "WHERE event_type IN ('watch_movie', 'watch_episode')"
    elif event_type == "affiliate":
        where_sql = "WHERE event_type IN ('affiliate_entered', 'affiliate_qualified', 'points_adjusted')"
    elif event_type and event_type != "all":
        where_sql = "WHERE event_type = :etype"
        params["etype"] = event_type

    res = await db.execute(
        text(f"""
            SELECT id, user_id, username, first_name, event_type, details, created_at
            FROM bot_user_events
            {where_sql}
            ORDER BY created_at DESC
            LIMIT :lim;
        """),
        params,
    )
    events = [
        {
            "id": r["id"],
            "user_id": r["user_id"],
            "username": r["username"],
            "first_name": r["first_name"] or "مستخدم",
            "event_type": r["event_type"],
            "details": r["details"] or "",
            "created_at": _iso(r["created_at"]),
        }
        for r in res.mappings().all()
    ]
    return {"success": True, "events": events}


@admin_router.post("/v1/bot-admin/scan-users")
async def silent_scan_bot_users(
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    """
    Silently pings registered users via sendChatAction('typing') AND checks live membership
    in all required Force-Subscribe channels via getChatMember!
    """
    await ensure_bot_admin_schema(db)

    bot_token = settings.TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN غير مضبوط")

    ch_res = await db.execute(text("SELECT id, chat_id, title, invite_link FROM required_channels ORDER BY id ASC;"))
    req_chs = [dict(r) for r in ch_res.mappings().all()]

    res = await db.execute(
        text("SELECT user_id, username, first_name, is_active, force_sub_passed_at FROM bot_users ORDER BY joined_at DESC LIMIT 1000;")
    )
    users = [dict(r) for r in res.mappings().all()]

    active_now = 0
    left_now = 0
    newly_left = 0
    newly_rejoined = 0
    compliant_now = 0
    non_compliant_now = 0

    api_url = f"https://api.telegram.org/bot{bot_token}/sendChatAction"
    async with httpx.AsyncClient(timeout=12.0) as client:
        for u in users:
            uid = u["user_id"]
            try:
                resp = await client.post(api_url, json={"chat_id": uid, "action": "typing"})
                data = resp.json()
            except Exception:
                data = {"ok": bool(u["is_active"])}

            if data.get("ok"):
                active_now += 1
                if not u["is_active"]:
                    newly_rejoined += 1
                    await db.execute(
                        text("""
                            UPDATE bot_users
                            SET is_active = TRUE,
                                status = 'active',
                                rejoined_at = NOW(),
                                left_at = NULL,
                                last_action = '🔄 نشط (تأكيد الفحص)'
                            WHERE user_id = :uid;
                        """),
                        {"uid": uid},
                    )
            else:
                desc = (data.get("description") or "").lower()
                if "blocked" in desc or "deactivated" in desc or "chat not found" in desc or "forbidden" in desc:
                    left_now += 1
                    reason = "deactivated" if "deactivated" in desc else "blocked"
                    reason_ar = "حساب محذوف" if reason == "deactivated" else "أوقف / حظر البوت"
                    if u["is_active"]:
                        newly_left += 1
                        await db.execute(
                            text("""
                                UPDATE bot_users
                                SET is_active = FALSE,
                                    status = :st,
                                    left_at = NOW(),
                                    last_action = :act
                                WHERE user_id = :uid;
                            """),
                            {"st": reason, "act": f"🚪 {reason_ar}", "uid": uid},
                        )
                        await db.execute(
                            text("""
                                INSERT INTO bot_user_events (user_id, username, first_name, event_type, details, created_at)
                                VALUES (:uid, :uname, :fname, 'left', :det, NOW());
                            """),
                            {"uid": uid, "uname": u.get("username"), "fname": u.get("first_name"), "det": reason_ar},
                        )
                else:
                    if u["is_active"]:
                        active_now += 1
                    else:
                        left_now += 1

            # Check all required channels for this user too
            r_stat, j_cnt, req_cnt, ch_details = await _check_telegram_channels_for_user(
                client, bot_token, uid, req_chs
            )
            if r_stat != "compliant" and u.get("force_sub_passed_at") is not None:
                r_stat = "left_channel"
            if r_stat == "compliant":
                compliant_now += 1
            else:
                non_compliant_now += 1

            await db.execute(
                text("""
                    UPDATE bot_users
                    SET rules_status = :rs,
                        joined_channels_count = :jc,
                        required_channels_count = :rc,
                        joined_channels_json = :jjson,
                        rules_checked_at = NOW(),
                        force_sub_passed_at = CASE WHEN :is_comp THEN COALESCE(force_sub_passed_at, NOW()) ELSE force_sub_passed_at END
                    WHERE user_id = :uid;
                """),
                {
                    "rs": r_stat,
                    "is_comp": (r_stat == "compliant"),
                    "jc": j_cnt,
                    "rc": req_cnt,
                    "jjson": json.dumps(ch_details, ensure_ascii=False),
                    "uid": uid,
                },
            )

    await db.commit()
    return {
        "success": True,
        "checked": len(users),
        "active": active_now,
        "left": left_now,
        "newly_left": newly_left,
        "newly_rejoined": newly_rejoined,
        "compliant": compliant_now,
        "non_compliant": non_compliant_now,
    }


@admin_router.post("/v1/bot-admin/toggle-notifications")
async def toggle_admin_notifications(
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)
    cur = await db.execute(text("SELECT value FROM bot_settings WHERE key = 'notify_admin_events';"))
    row = cur.mappings().first()
    cur_val = row["value"] if row else "1"
    new_val = "0" if cur_val == "1" else "1"
    await db.execute(
        text("""
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES ('notify_admin_events', :val, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
        """),
        {"val": new_val},
    )
    await db.commit()
    return {"success": True, "notify_admin_events": new_val == "1"}


@admin_router.get("/v1/bot-admin/export-csv")
async def export_bot_users_csv(
    status_filter: str = Query("all", alias="status"),
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)
    where_sql = ""
    if status_filter == "active":
        where_sql = "WHERE is_active = TRUE"
    elif status_filter == "left":
        where_sql = "WHERE is_active = FALSE"
    elif status_filter == "compliant":
        where_sql = "WHERE rules_status = 'compliant'"
    elif status_filter == "noncompliant":
        where_sql = "WHERE rules_status IN ('unsubscribed', 'partial', 'left_channel')"
    elif status_filter == "affiliates":
        where_sql = "WHERE COALESCE(points, 0) > 0 OR COALESCE(total_referrals_entered, 0) >= 1"
    elif status_filter == "referred":
        where_sql = "WHERE referred_by IS NOT NULL"

    res = await db.execute(
        text(f"""
            SELECT user_id, username, first_name, last_name, is_active, status,
                   rules_status, joined_channels_count, required_channels_count,
                   movies_watched_count, episodes_watched_count, searches_count,
                   COALESCE(points, 0) AS points,
                   COALESCE(total_referrals_entered, 0) AS total_referrals_entered,
                   COALESCE(qualified_referrals_count, 0) AS qualified_referrals_count,
                   referred_by, referred_via_code, referred_target_title, COALESCE(referral_qualified, FALSE) AS referral_qualified,
                   joined_at, last_active_at, left_at, actions_count, last_action
            FROM bot_users
            {where_sql}
            ORDER BY joined_at DESC;
        """)
    )

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "User ID", "Username", "First Name", "Last Name", "Bot Status",
        "Points", "Referrals Entered", "Qualified Referrals (+1)",
        "Referred By ID", "Referred Via Link Code", "Referred Via Movie/Link", "Referral Qualified",
        "Rules Status", "Joined Channels", "Movies Watched", "Episodes Watched", "Searches",
        "Total Interactions", "Last Action", "Joined At", "Last Active At", "Left At"
    ])
    for r in res.mappings().all():
        writer.writerow([
            r["user_id"],
            f"@{r['username']}" if r["username"] else "",
            r["first_name"] or "",
            r["last_name"] or "",
            "نشط" if r["is_active"] else "غادر/حظر",
            r["points"] or 0,
            r["total_referrals_entered"] or 0,
            r["qualified_referrals_count"] or 0,
            r["referred_by"] or "",
            r["referred_via_code"] or "",
            r["referred_target_title"] or "",
            "نعم (+1 نقطة)" if r["referral_qualified"] else ("بانتظار الاستخدام" if r["referred_by"] else ""),
            r["rules_status"] or "",
            f"{r['joined_channels_count'] or 0}/{r['required_channels_count'] or 0}",
            r["movies_watched_count"] or 0,
            r["episodes_watched_count"] or 0,
            r["searches_count"] or 0,
            r["actions_count"] or 1,
            r["last_action"] or "",
            _iso(r["joined_at"]) or "",
            _iso(r["last_active_at"]) or "",
            _iso(r["left_at"]) or "",
        ])

    output.seek(0)
    filename = f"riva_bot_users_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_BOT_TOKEN_REGEX = re.compile(r"\b\d{8,13}:[A-Za-z0-9_-]{25,70}\b")


def _mask_bot_token(tok: str) -> str:
    t = (tok or "").strip()
    if ":" not in t:
        return t[:8] + "..."
    prefix, secret = t.split(":", 1)
    if len(secret) <= 8:
        return f"{prefix}:***"
    return f"{prefix}:{secret[:4]}...{secret[-4:]}"


async def _verify_uploader_bot_token(
    client: httpx.AsyncClient,
    token: str,
    primary_tok: str,
    channel_id: str,
) -> dict:
    tok = (token or "").strip()
    if primary_tok and tok == primary_tok.strip():
        return {
            "valid": False,
            "is_primary": True,
            "error": "هذا هو توكن البوت الأساسي (@riva18_bot) وهو مفعل تلقائياً كبوت رفع أساسي.",
        }
    try:
        me_r = await client.get(f"https://api.telegram.org/bot{tok}/getMe")
        me_data = me_r.json()
        if not me_data.get("ok"):
            return {"valid": False, "error": me_data.get("description") or "توكن غير صالح أو ملغي من BotFather"}
        me = me_data["result"]
        bid = int(me["id"])
        uname = me.get("username") or ""
        fname = me.get("first_name") or ""

        channel_ok = False
        ch_err = None
        if channel_id:
            cm_r = await client.get(
                f"https://api.telegram.org/bot{tok}/getChatMember",
                params={"chat_id": str(channel_id), "user_id": bid},
            )
            cm_data = cm_r.json()
            if cm_data.get("ok"):
                res = cm_data.get("result") or {}
                st = res.get("status", "")
                can_post = res.get("can_post_messages", True if st == "creator" else False)
                if st in ("administrator", "creator") and can_post is not False:
                    channel_ok = True
                else:
                    ch_err = f"موجود بالقناة بحالة ({st}) ويحتاج صلاحية النشر (Post Messages)"
            else:
                ch_err = "لم يتم رفعه كمشرف (Admin) في قناة الأرشيف بعد"

        return {
            "valid": True,
            "bot_id": bid,
            "username": uname,
            "first_name": fname,
            "channel_ok": channel_ok,
            "error": ch_err,
        }
    except Exception as e:
        return {"valid": False, "error": f"خطأ أثناء فحص التوكن: {str(e)[:120]}"}


async def _send_worker_vault_checkin_http(
    client: httpx.AsyncClient,
    token: str,
    channel_id: str,
    bot_name: str,
    bot_username: str,
    bot_id: Optional[int],
) -> tuple[bool, Optional[str]]:
    """Sends a live test message FROM the bot's own token into the Vault channel bearing its own name."""
    import html as _html

    if not channel_id:
        return False, "TELEGRAM_CHANNEL_ID غير مضبوط"
    disp = (
        f"{bot_name} (@{bot_username})"
        if (bot_name and bot_username)
        else (f"@{bot_username}" if bot_username else (bot_name or f"Bot {bot_id}"))
    )
    msg_html = (
        f"✅ <b>رسالة فحص وتأكيد من البوت نفسه</b>\n"
        f"🤖 <b>اسم البوت:</b> {_html.escape(disp)}\n"
        f"🆔 <b>المعرف:</b> <code>{_html.escape(str(bot_id or ''))}</code>\n"
        f"📡 <b>الحالة:</b> التوكن وصلاحية النشر في الخزنة يعملان 100% 🚀"
    )
    try:
        r = await client.post(
            f"https://api.telegram.org/bot{token.strip()}/sendMessage",
            json={"chat_id": str(channel_id), "text": msg_html, "parse_mode": "HTML"},
        )
        data = r.json()
        if data.get("ok"):
            return True, None
        return False, data.get("description") or "فشل إرسال الرسالة التجريبية للخزنة"
    except Exception as e:
        return False, str(e)[:120]


class AddWorkerBotsRequest(BaseModel):
    tokens_text: str


@admin_router.get("/v1/bot-admin/workers")
async def list_uploader_workers(
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    await ensure_bot_admin_schema(db)

    primary_tok = (settings.TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    primary_id = primary_tok.split(":")[0] if ":" in primary_tok else ""
    channel_id = str(settings.TELEGRAM_CHANNEL_ID or os.getenv("TELEGRAM_CHANNEL_ID", ""))

    p_uploads = 0
    p_bytes = 0
    set_res = await db.execute(
        text("SELECT key, value FROM bot_settings WHERE key IN ('primary_bot_uploads_count', 'primary_bot_uploaded_bytes');")
    )
    for r in set_res.mappings().all():
        if r["key"] == "primary_bot_uploads_count":
            p_uploads = int(r["value"] or 0)
        elif r["key"] == "primary_bot_uploaded_bytes":
            p_bytes = int(r["value"] or 0)

    w_res = await db.execute(
        text("""
            SELECT
                id, bot_id, bot_username, bot_name, bot_token,
                COALESCE(is_active, TRUE) AS is_active,
                COALESCE(channel_ok, FALSE) AS channel_ok,
                COALESCE(uploads_count, 0) AS uploads_count,
                COALESCE(total_uploaded_bytes, 0) AS total_uploaded_bytes,
                last_used_at, last_error, created_at
            FROM uploader_bots
            ORDER BY id ASC;
        """)
    )
    raw_rows = [dict(r) for r in w_res.mappings().all()]

    # Live inflight counters from Redis
    inflight_map = {}
    try:
        bot_ids = [primary_id] + [str(r["bot_id"] or str(r["bot_token"]).split(":")[0]) for r in raw_rows]
        bot_ids = [b for b in bot_ids if b]
        if bot_ids:
            vals = await redis.mget([f"riva:uploader_inflight:{b}" for b in bot_ids])
            for b, v in zip(bot_ids, vals):
                try:
                    inflight_map[b] = max(0, int(v or 0))
                except (ValueError, TypeError):
                    inflight_map[b] = 0
    except Exception:
        pass

    workers_out = []
    ready_helpers = 0
    for r in raw_rows:
        bid_str = str(r["bot_id"] or str(r["bot_token"]).split(":")[0])
        is_ready = bool(r["is_active"] and r["channel_ok"])
        if is_ready:
            ready_helpers += 1
        workers_out.append({
            "id": r["id"],
            "bot_id": r["bot_id"],
            "bot_username": r["bot_username"] or "",
            "bot_name": r["bot_name"] or "",
            "masked_token": _mask_bot_token(r["bot_token"] or ""),
            "is_active": bool(r["is_active"]),
            "channel_ok": bool(r["channel_ok"]),
            "inflight": inflight_map.get(bid_str, 0),
            "uploads_count": int(r["uploads_count"] or 0),
            "uploaded_gb": round(float(r["total_uploaded_bytes"] or 0) / (1024.0 ** 3), 2),
            "last_used_at": _iso(r["last_used_at"]),
            "last_error": r["last_error"],
            "created_at": _iso(r["created_at"]),
        })

    primary_inflight = inflight_map.get(primary_id, 0)
    total_inflight = primary_inflight + sum(w["inflight"] for w in workers_out)

    return {
        "success": True,
        "channel_id": channel_id,
        "primary_bot": {
            "bot_id": primary_id,
            "username": "riva18_bot",
            "inflight": primary_inflight,
            "uploads_count": p_uploads,
            "uploaded_gb": round(p_bytes / (1024.0 ** 3), 2),
        },
        "workers": workers_out,
        "summary": {
            "active_pool_size": 1 + ready_helpers,
            "ready_helpers": ready_helpers,
            "total_helpers": len(workers_out),
            "total_inflight": total_inflight,
        },
    }


@admin_router.post("/v1/bot-admin/workers")
async def add_uploader_workers(
    req: AddWorkerBotsRequest,
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)

    found = []
    seen = set()
    for m in _BOT_TOKEN_REGEX.finditer(req.tokens_text or ""):
        t = m.group(0).strip()
        if t not in seen:
            seen.add(t)
            found.append(t)

    if not found:
        raise HTTPException(status_code=400, detail="لم يتم العثور على أي توكن صالح بصيغة BotFather في النص المدخل.")

    primary_tok = (settings.TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    channel_id = str(settings.TELEGRAM_CHANNEL_ID or os.getenv("TELEGRAM_CHANNEL_ID", ""))

    async with httpx.AsyncClient(timeout=12.0) as client:
        checks = await asyncio.gather(*(
            _verify_uploader_bot_token(client, tok, primary_tok, channel_id)
            for tok in found
        ))
        for tok, info in zip(found, checks):
            if info.get("valid") and info.get("channel_ok"):
                ping_ok, ping_err = await _send_worker_vault_checkin_http(
                    client,
                    tok,
                    channel_id,
                    info.get("first_name") or "",
                    info.get("username") or "",
                    info.get("bot_id"),
                )
                if not ping_ok:
                    info["channel_ok"] = False
                    info["error"] = f"فشل إرسال الرسالة التجريبية للخزنة: {ping_err}"

    added = []
    errors = []
    for tok, info in zip(found, checks):
        masked = _mask_bot_token(tok)
        if not info.get("valid"):
            errors.append({"masked_token": masked, "error": info.get("error") or "توكن غير صالح"})
            continue

        res = await db.execute(
            text("""
                INSERT INTO uploader_bots (
                    bot_token, bot_id, bot_username, bot_name, is_active, channel_ok, last_error, created_at
                )
                VALUES (:tok, :bid, :un, :fn, TRUE, :cok, :err, NOW())
                ON CONFLICT (bot_token) DO UPDATE
                SET bot_id = COALESCE(EXCLUDED.bot_id, uploader_bots.bot_id),
                    bot_username = COALESCE(EXCLUDED.bot_username, uploader_bots.bot_username),
                    bot_name = COALESCE(EXCLUDED.bot_name, uploader_bots.bot_name),
                    is_active = TRUE,
                    channel_ok = EXCLUDED.channel_ok,
                    last_error = EXCLUDED.last_error
                RETURNING id, bot_id, bot_username, bot_name, is_active, channel_ok, last_error;
            """),
            {
                "tok": tok,
                "bid": info.get("bot_id"),
                "un": info.get("username"),
                "fn": info.get("first_name"),
                "cok": bool(info.get("channel_ok")),
                "err": info.get("error"),
            },
        )
        row = dict(res.mappings().first() or {})
        row["masked_token"] = masked
        added.append(row)

    await db.commit()
    return {"success": True, "added": added, "errors": errors}


@admin_router.post("/v1/bot-admin/workers/recheck")
async def recheck_uploader_workers(
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)

    w_res = await db.execute(text("SELECT id, bot_token, bot_id, bot_username, bot_name FROM uploader_bots ORDER BY id ASC;"))
    rows = [dict(r) for r in w_res.mappings().all()]
    if not rows:
        return {"success": True, "checked": 0, "ready": 0}

    primary_tok = (settings.TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    channel_id = str(settings.TELEGRAM_CHANNEL_ID or os.getenv("TELEGRAM_CHANNEL_ID", ""))

    async with httpx.AsyncClient(timeout=12.0) as client:
        checks = await asyncio.gather(*(
            _verify_uploader_bot_token(client, r["bot_token"], primary_tok, channel_id)
            for r in rows
        ))

    ready = 0
    for r, info in zip(rows, checks):
        if info.get("valid"):
            is_ok = bool(info.get("channel_ok"))
            if is_ok:
                ready += 1
            await db.execute(
                text("""
                    UPDATE uploader_bots
                    SET bot_id = COALESCE(:bid, bot_id),
                        bot_username = COALESCE(:un, bot_username),
                        bot_name = COALESCE(:fn, bot_name),
                        channel_ok = :cok,
                        last_error = :err
                    WHERE id = :id;
                """),
                {
                    "bid": info.get("bot_id"),
                    "un": info.get("username"),
                    "fn": info.get("first_name"),
                    "cok": is_ok,
                    "err": info.get("error"),
                    "id": r["id"],
                },
            )
    await db.commit()
    return {"success": True, "checked": len(rows), "ready": ready}


@admin_router.post("/v1/bot-admin/workers/{worker_id}/toggle")
async def toggle_uploader_worker(
    worker_id: int,
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)
    res = await db.execute(
        text("""
            UPDATE uploader_bots
            SET is_active = NOT COALESCE(is_active, TRUE)
            WHERE id = :id
            RETURNING id, bot_username, is_active, channel_ok;
        """),
        {"id": worker_id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    await db.commit()
    return {"success": True, "worker": dict(row)}


@admin_router.delete("/v1/bot-admin/workers/{worker_id}")
async def delete_uploader_worker(
    worker_id: int,
    _: bool = Depends(verify_admin_password),
    db: AsyncSession = Depends(get_db),
):
    await ensure_bot_admin_schema(db)
    res = await db.execute(
        text("DELETE FROM uploader_bots WHERE id = :id RETURNING id;"),
        {"id": worker_id},
    )
    if not res.mappings().first():
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    await db.commit()
    return {"success": True}
