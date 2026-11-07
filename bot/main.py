import asyncio
import os
import re
import time
import logging
import html
import urllib.parse
from datetime import datetime, timezone
import aiohttp
from bot.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_IDS, BOT_ADMIN_PASSWORD
from bot import db
from bot import delivery_bot

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s: %(levelname)s/%(name)s] %(message)s"
)
logger = logging.getLogger("riva_bot")

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
BOT_USERNAME = "riva18_bot"
DEFAULT_POSTER_URL = "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=800&q=80"
PUBLIC_REDIRECT_BASE = os.getenv(
    "PUBLIC_REDIRECT_BASE_URL",
    os.getenv("WEB_DASHBOARD_URL", "http://186.240.145.87:8090/admin").rsplit("/admin", 1)[0],
).rstrip("/")


def build_affiliate_redirect_url(
    target_type: str,
    target_id: int | None = None,
    user_id: int | None = None,
) -> str:
    """Builds the personal official Telegram Affiliate Link (https://t.me/riva18_bot?start={code}) carrying both the item and the user's affiliate ID."""
    code = db.encode_affiliate_code(target_type, target_id, user_id)
    return f"https://t.me/{BOT_USERNAME}?start={code}"

# Cache Telegram file_ids for poster URLs so subsequent photo sends take <40ms
_poster_file_id_cache: dict[str, str] = {}

# Cache verified user subscriptions for 45s so navigation remains instant
_verified_users_cache: dict[int, float] = {}
SUB_CACHE_TTL = 45.0

# Authenticated admin sessions (unlocked via /admin <PASSWORD>)
_unlocked_admins: set[int] = set()
# Admins currently waiting to send a broadcast message
_awaiting_broadcast: set[int] = set()
# Admins currently waiting to paste 1 or multiple @BotFather uploader worker tokens
_awaiting_worker_tokens: set[int] = set()

# ── +18 Admin Manual Upload States ──────────────────────────────────────────
# Admins who sent a video and we're waiting for their title input
# Value: dict with the pending video info (file_id, message_id, etc.)
_awaiting_adult18_title: dict[int, dict] = {}
# Admins who sent title and we're waiting for year (optional — they can skip)
_awaiting_adult18_year: dict[int, dict] = {}

ADULT18_STARS_PRICE = 100   # Stars required for 30-day access
ADULT18_ACCESS_DAYS = 30    # Days of access per payment

_BOT_TOKEN_REGEX = re.compile(r"\b\d{8,13}:[A-Za-z0-9_-]{25,70}\b")

GENRE_HEADERS = {
    "Drama": "- أفلام الدراما.",
    "Romance": "- افلام رومنسية.",
    "Science Fiction": "- افلام خيال علمي.",
    "Thriller": "- افلام تشويق وإثارة.",
    "Comedy": "- أفلام كوميدية.",
    "Action": "- أفلام الأكشن والإثارة.",
    "Horror": "- افلام رعب.",
    "Adventure": "- افلام مغامرات وتاريخ.",
    "Crime": "- أفلام الجريمة والغموض.",
    "Fantasy": "- افلام فانتازيا.",
    "collections": "- سلاسل افلام.",
    "all": "- جميع الأفلام.",
}


def get_poster_full_url(path: str | None) -> str:
    if not path:
        return DEFAULT_POSTER_URL
    if path.startswith("tg_photo:"):
        return path.split(":", 1)[1]
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"https://image.tmdb.org/t/p/w500{path}"


def btn(text: str, callback_data: str = None, style: str = None, url: str = None, copy_text: str = None) -> dict:
    b = {"text": text}
    if copy_text:
        b["copy_text"] = {"text": copy_text}
    elif url:
        b["url"] = url
    elif callback_data:
        b["callback_data"] = callback_data
    if style:
        b["style"] = style
    return b


def build_smart_title_rows(
    items_with_cb: list[tuple[str, str]],
    short_max_len: int = 20,
    full_max_len: int = 52
) -> list[list[dict]]:
    """
    Smart Dynamic Button Layout:
    - Short titles (<= short_max_len chars) are paired 2 side-by-side in one row.
    - Long titles (> short_max_len chars) automatically get their own full-width row
      so the full name is readable without ugly 20-char truncation.
    """
    rows: list[list[dict]] = []
    pending_short: dict | None = None

    for raw_label, cb_data in items_with_cb:
        label = (raw_label or "").strip()
        if len(label) <= short_max_len:
            b = btn(label, cb_data)
            if pending_short is None:
                pending_short = b
            else:
                rows.append([pending_short, b])
                pending_short = None
        else:
            full_label = label if len(label) <= full_max_len else label[:full_max_len - 2].rstrip() + ".."
            rows.append([btn(full_label, cb_data)])

    if pending_short is not None:
        rows.append([pending_short])

    return rows


def main_menu_keyboard(counts: dict | None = None) -> dict:
    """
    Step 1 Root Menu:
    - Shows Movies & Series sections dynamically (hides any empty section if count == 0)
    - Dedicated 'للكبار فقط 🔞' (+18) gated section when adult items exist (isolated from normal lists)
    - Full-width RED ('danger') comprehensive search button at the bottom
    """
    top_row = []
    rows = []
    if counts is None:
        top_row = [
            btn("قسم الأفلام 🎬", "nt:m", style="success"),
            btn("قسم المسلسلات 📺", "nt:s", style="success"),
        ]
        rows.append(top_row)
        rows.append([
            btn(f"🔞 للكبار فقط (+18) • {ADULT18_STARS_PRICE}⭐", "adult_gate"),
        ])
    else:
        if counts.get("m", 0) > 0:
            top_row.append(btn("قسم الأفلام 🎬", "nt:m", style="success"))
        if counts.get("s", 0) > 0:
            top_row.append(btn("قسم المسلسلات 📺", "nt:s", style="success"))
        if not top_row:
            top_row = [
                btn("قسم الأفلام 🎬", "nt:m", style="success"),
                btn("قسم المسلسلات 📺", "nt:s", style="success"),
            ]
        rows.append(top_row)
        # +18 Stars button — always show regardless of `adult` count
        rows.append([
            btn(f"🔞 للكبار فقط (+18) • {ADULT18_STARS_PRICE}⭐", "adult_gate"),
        ])

    rows.append([
        btn("🎁 نقاطي ومشاركة الربح (Affiliate)", "my_affiliate", style="primary"),
    ])
    rows.append([
        btn("البحث الشامل 🔍", "search_prompt", style="danger"),
    ])
    return {"inline_keyboard": rows}


async def get_dynamic_main_menu_keyboard() -> dict:
    counts = await asyncio.to_thread(db.get_vault_media_counts)
    return main_menu_keyboard(counts)


def build_welcome_text(first_name: str) -> str:
    safe_name = html.escape(first_name or "صديقي")
    return (
        f"<b>اهلا بك صديقي ({safe_name}) 💟\n\n"
        f"اختر القسم الذي ترغب بمشاهدته ⬇️</b>"
    )


def build_movie_poster_caption(m: dict, user_id: int | None = None) -> str:
    title = db.clean_display_title(m.get("title") or "فيلم")
    year_str = f" ({m['year']})" if m.get("year") else ""
    rating = m.get("vote_average")
    rating_str = f"\n⭐ <b>التقييم:</b> {round(float(rating), 1)}/10" if rating else ""
    adult_banner = "🔞 <b>تحذير: محتوى للكبار فقط (+18) — غير مناسب للمشاهدة العائلية</b>\n\n" if m.get("is_adult") else ""
    desc = (m.get("overview") or "مشاهدة ممتعة لأحدث الأفلام بجودة عالية.").strip()
    if len(desc) > 650:
        desc = desc[:650] + "..."
    share_link = build_affiliate_redirect_url("movie", m["id"], user_id)
    return (
        f"{adult_banner}"
        f"🎬 <b>{html.escape(title)}{year_str}</b>"
        f"{rating_str}\n\n"
        f"📝 <b>القصة:</b>\n{html.escape(desc)}\n\n"
        f"🎁 <b>رابط المشاركة الخاص بك (+1 نقطة لكل صديق يدخل ويستخدم البوت):</b>\n{html.escape(share_link)}"
    )


def build_series_poster_caption(s: dict, ep: dict | None = None, user_id: int | None = None) -> str:
    title = db.clean_display_title(s.get("title") or "مسلسل")
    year_str = f" ({s['year']})" if s.get("year") else ""
    rating = s.get("vote_average")
    rating_str = f"\n⭐ <b>التقييم:</b> {round(float(rating), 1)}/10" if rating and float(rating) > 0 else ""
    ep_line = ""
    if ep:
        ep_line = f"\n📌 <b>الموسم {ep['season_number']} - الحلقة {ep['episode_number']}</b>"
    desc = (s.get("overview") or "مشاهدة ممتعة لأحدث المسلسلات بجودة عالية.").strip()
    if len(desc) > 600:
        desc = desc[:600] + "..."
    s_id = s.get("id") or (ep.get("series_id") if ep else "")
    if ep and ep.get("id"):
        share_link = build_affiliate_redirect_url("episode", ep["id"], user_id)
    elif s_id:
        share_link = build_affiliate_redirect_url("series", int(s_id), user_id)
    else:
        share_link = ""
    link_line = (
        f"\n\n🎁 <b>رابط المشاركة الخاص بك (+1 نقطة لكل صديق يدخل ويستخدم البوت):</b>\n{html.escape(share_link)}"
        if share_link
        else ""
    )
    return (
        f"📺 <b>{html.escape(title)}{year_str}</b>"
        f"{rating_str}{ep_line}\n\n"
        f"📝 <b>القصة:</b>\n{html.escape(desc)}\n\n"
        f"📦 <b>الحلقات المتوفرة:</b> {s.get('uploaded_episodes', 1)} حلقة"
        f"{link_line}"
    )


def build_video_share_caption(label: str, share_link: str) -> str:
    safe_lbl = html.escape((label or "مشاهدة ممتعة").strip())
    if not share_link:
        return f"🎬 <b>{safe_lbl}</b>"
    return f"🎬 <b>{safe_lbl}</b>\n🔗 رابط المشاهدة المباشر: {html.escape(share_link)}"


def build_share_url(deep_link: str, title: str) -> str:
    q_url = urllib.parse.quote(deep_link, safe="")
    q_txt = urllib.parse.quote(f"🎬 شاهد {title} بجودة عالية مباشرة بدون إعلانات:", safe="")
    return f"https://t.me/share/url?url={q_url}&text={q_txt}"


def build_episode_buttons_markup(
    series_id: int,
    episodes: list[dict],
    active_ep_id: int,
    s_cat: str,
    s_page: str | int,
    ep_page: int,
    total_ep_pages: int,
    poster_msg_id: int,
    series_title: str = "المسلسل",
    user_id: int | None = None,
    delivery_bot_user: str | None = None,
) -> dict:
    """
    Builds the 3-column episode selector grid + navigation + personal affiliate share + back/exit buttons
    to be placed directly UNDERNEATH the video player or watch card.
    """
    rows = []

    # If secondary delivery bot is active, place a prominent direct watch button at the top
    if delivery_bot_user:
        active_ep_num = 1
        for ep in episodes:
            if ep["id"] == active_ep_id:
                active_ep_num = ep.get("episode_number", 1)
                break
        deliv_url = f"https://t.me/{delivery_bot_user}?start=ep_{active_ep_id}"
        if user_id:
            deliv_url += f"_r_{user_id}"
        rows.append([
            btn(f"🍿 تشغيل الحلقة {active_ep_num} في سيرفر المشاهدة 🎬", url=deliv_url, style="success")
        ])

    row = []
    for ep in episodes:
        ep_id = ep["id"]
        ep_num = ep["episode_number"]
        s_num = ep["season_number"]
        base_label = f"الحلقة {ep_num}" if s_num == 1 else f"S{s_num} E{ep_num}"
        label = f"✅ {base_label}" if ep_id == active_ep_id else base_label
        cb_data = f"ep:{ep_id}:{s_cat}:{s_page}:{ep_page}:{poster_msg_id}"
        row.append(btn(label, cb_data))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if total_ep_pages > 1:
        nav = []
        if ep_page > 1:
            nav.append(btn("⬅️ السابق", f"ep_pg:{series_id}:{active_ep_id}:{s_cat}:{s_page}:{ep_page-1}:{poster_msg_id}"))
        nav.append(btn(f"{ep_page}/{total_ep_pages}", "noop"))
        if ep_page < total_ep_pages:
            nav.append(btn("التالي ➡️", f"ep_pg:{series_id}:{active_ep_id}:{s_cat}:{s_page}:{ep_page+1}:{poster_msg_id}"))
        rows.append(nav)

    aff_ep_link = build_affiliate_redirect_url("episode", active_ep_id, user_id)
    rows.append([
        btn("📋 نسخ رابط الحلقة (+1 نقطة 🎁)", copy_text=aff_ep_link, style="success"),
        btn("📤 مشاركة (+1 نقطة)", url=build_share_url(aff_ep_link, series_title), style="primary"),
    ])
    rows.append([
        btn("🔙 رجوع للمسلسلات", f"back_ser:{s_cat}:{s_page}:{poster_msg_id}"),
        btn("خروج", f"exit_vid:{poster_msg_id}")
    ])
    return {"inline_keyboard": rows}


async def build_user_affiliate_view(user_id: int, first_name: str) -> tuple[str, dict]:
    """Builds the personal Points & Affiliate dashboard inside @riva18_bot for any user."""
    summary = await asyncio.to_thread(db.get_user_affiliate_summary, user_id)
    personal_link = build_affiliate_redirect_url("bot", None, user_id)
    safe_name = html.escape(first_name or "صديقي")

    lines = [
        f"🎁 <b>مركز النقاط ونظام الأفلييت ({safe_name})</b>\n",
        f"🏆 <b>رصيد نقاطك الحالي:</b> <code>{summary['points']}</code> نقطة",
        f"👥 <b>دخلوا البوت عبر روابطك:</b> <code>{summary['total_entered']}</code> شخص",
        f"✅ <b>استخدموا البوت فعلياً (+1 نقطة):</b> <code>{summary['qualified_count']}</code> شخص",
        f"⏳ <b>دخلوا ولم يكملوا الاستخدام بعد:</b> <code>{summary['pending_count']}</code> شخص\n",
        "🔗 <b>رابط الدعوة العام الخاص بك:</b>",
        f"<code>{personal_link}</code>\n",
        "💡 <b>كيف تكسب النقاط بسهولة؟</b>",
        "• أي <b>فيلم أو مسلسل أو حلقة</b> تفتحه داخل البوت ستجد تحته تلقائياً <b>زر نسخ ومشاركة الرابط الخاص بك</b>!",
        "• شارك رابط أي فيلم مع أصدقائك أو على السوشيال ميديا، وكل شخص جديد يدخل ويستخدم البوت من رابطك تحصل فوراً على <b>+1 نقطة</b> ويصلك إشعار باسمه وبالفيلم الذي دخل منه!\n",
    ]

    invited = summary.get("invited_users") or []
    lines.append("📋 <b>آخر الأشخاص الذين دخلوا عبر روابطك:</b>")
    if not invited:
        lines.append("• لم يدخل أحد عبر روابطك بعد — انسخ رابطك أو شارك أي فيلم وابدأ بجمع النقاط الآن 🚀")
    else:
        for idx, inv in enumerate(invited[:10], 1):
            nm = f"{inv.get('first_name') or ''} {inv.get('last_name') or ''}".strip() or f"User {inv['referred_user_id']}"
            un = f" (@{html.escape(inv['username'])})" if inv.get("username") else ""
            tt = html.escape((inv.get("target_title") or "رابط الدعوة")[:38])
            st = "✅ +1 نقطة" if inv.get("status") == "qualified" else "⏳ بانتظار الاستخدام"
            lines.append(f"{idx}. <b>{html.escape(nm)}</b>{un} — <i>{tt}</i> [{st}]")

    lb = summary.get("leaderboard") or []
    if lb:
        lines.append("\n🥇 <b>قائمة المتصدرين في النقاط:</b>")
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, row in enumerate(lb[:5]):
            m_ico = medals[i] if i < len(medals) else "•"
            r_nm = html.escape((row.get("first_name") or "مستخدم").strip()[:20])
            me_tag = " (أنت 🌟)" if int(row["user_id"]) == int(user_id) else ""
            lines.append(f"{m_ico} <b>{r_nm}</b>{me_tag}: <code>{row['points']}</code> نقطة ({row['qualified_count']} إحالة نشطة)")

    markup = {
        "inline_keyboard": [
            [
                btn("📋 نسخ رابط الدعوة الخاص بي", copy_text=personal_link, style="success"),
                btn("📤 مشاركة البوت", url=build_share_url(personal_link, "بوت السينما Riva"), style="primary"),
            ],
            [
                btn("🔄 تحديث نقاطي", callback_data="my_affiliate"),
                btn("🏠 القائمة الرئيسية", callback_data="menu", style="success"),
            ],
        ]
    }
    return "\n".join(lines), markup


class RivaBotAPI:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def start_session(self):
        connector = aiohttp.TCPConnector(limit=100, keepalive_timeout=60, ttl_dns_cache=300)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=35)
        )

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def api_call(self, method: str, payload: dict = None) -> dict:
        if not self.session or self.session.closed:
            await self.start_session()
        url = f"{API_BASE}/{method}"
        try:
            async with self.session.post(url, json=payload or {}) as resp:
                return await resp.json()
        except Exception as e:
            logger.error(f"Bot API error in {method}: {e}")
            return {"ok": False, "description": str(e)}

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict = None,
        reply_to_message_id: int = None,
        parse_mode: str = "HTML"
    ):
        payload = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True
            }
        res = await self.api_call("sendMessage", payload)
        if not res.get("ok") and reply_markup and "style" in str(reply_markup):
            payload["reply_markup"] = self._strip_styles(reply_markup)
            res = await self.api_call("sendMessage", payload)
        return res

    async def send_poster_with_caption(self, chat_id: int, poster_url: str, caption: str) -> int:
        """
        Sends the Poster image at the top with the Description as its caption (no buttons on poster).
        Returns the sent poster's message_id.
        """
        photo_ref = _poster_file_id_cache.get(poster_url, poster_url)
        payload = {
            "chat_id": chat_id,
            "photo": photo_ref,
            "caption": caption,
            "parse_mode": "HTML"
        }
        res = await self.api_call("sendPhoto", payload)
        if res.get("ok"):
            msg_obj = res.get("result", {})
            photos = msg_obj.get("photo", [])
            if photos and poster_url not in _poster_file_id_cache:
                _poster_file_id_cache[poster_url] = photos[-1]["file_id"]
            return msg_obj.get("message_id", 0)

        # Fallback to default cinema poster if TMDB URL failed
        if poster_url != DEFAULT_POSTER_URL:
            fallback_ref = _poster_file_id_cache.get(DEFAULT_POSTER_URL, DEFAULT_POSTER_URL)
            payload["photo"] = fallback_ref
            res2 = await self.api_call("sendPhoto", payload)
            if res2.get("ok"):
                msg_obj = res2.get("result", {})
                photos = msg_obj.get("photo", [])
                if photos and DEFAULT_POSTER_URL not in _poster_file_id_cache:
                    _poster_file_id_cache[DEFAULT_POSTER_URL] = photos[-1]["file_id"]
                return msg_obj.get("message_id", 0)

        # Ultimate fallback: send caption as text message
        res_txt = await self.send_message(chat_id, caption, parse_mode="HTML")
        return res_txt.get("result", {}).get("message_id", 0)

    async def edit_poster_caption(self, chat_id: int, poster_msg_id: int, caption: str):
        if not poster_msg_id:
            return
        res = await self.api_call("editMessageCaption", {
            "chat_id": chat_id,
            "message_id": poster_msg_id,
            "caption": caption,
            "parse_mode": "HTML"
        })
        if not res.get("ok") and "message is not modified" not in res.get("description", ""):
            await self.api_call("editMessageText", {
                "chat_id": chat_id,
                "message_id": poster_msg_id,
                "text": caption,
                "parse_mode": "HTML"
            })

    async def edit_or_send(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict = None,
        parse_mode: str = "HTML"
    ):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        res = await self.api_call("editMessageText", payload)
        if not res.get("ok"):
            desc = res.get("description", "")
            if "message is not modified" in desc:
                return res
            if reply_markup and "style" in str(reply_markup):
                payload["reply_markup"] = self._strip_styles(reply_markup)
                res = await self.api_call("editMessageText", payload)
                if res.get("ok"):
                    return res
            await self.delete_message(chat_id, message_id)
            return await self.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        return res

    async def delete_message(self, chat_id: int, message_id: int):
        if not message_id:
            return
        await self.api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def answer_callback(self, callback_id: str, text: str = None, show_alert: bool = False):
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert
        await self.api_call("answerCallbackQuery", payload)

    async def deliver_video_with_buttons(
        self,
        chat_id: int,
        channel_id: int,
        vault_message_id: int,
        reply_markup: dict,
        video_caption: str = "",
    ) -> bool:
        """
        Delivers the streamable video directly underneath the Poster+Caption,
        with the action/episode buttons attached underneath the video and a
        forwarded-safe personal affiliate link caption.
        """
        copy_payload = {
            "chat_id": chat_id,
            "from_chat_id": channel_id,
            "message_id": vault_message_id,
            "caption": video_caption or "",
            "reply_markup": reply_markup,
        }
        if video_caption:
            copy_payload["parse_mode"] = "HTML"
        copy_res = await self.api_call("copyMessage", copy_payload)
        if isinstance(copy_res, dict) and copy_res.get("ok"):
            return True

        logger.warning(f"copyMessage failed ({copy_res}), trying forward fallback.")
        await self.api_call("forwardMessage", {
            "chat_id": chat_id,
            "from_chat_id": channel_id,
            "message_id": vault_message_id
        })
        await self.send_message(chat_id, video_caption or "🍿 مشاهدة ممتعة!", reply_markup=reply_markup)
        return True

    @staticmethod
    def _strip_styles(markup: dict) -> dict:
        rows = []
        for row in markup.get("inline_keyboard", []):
            new_row = []
            for b in row:
                nb = {"text": b["text"]}
                if "copy_text" in b:
                    nb["copy_text"] = b["copy_text"]
                if "url" in b:
                    nb["url"] = b["url"]
                if "callback_data" in b:
                    nb["callback_data"] = b["callback_data"]
                new_row.append(nb)
            rows.append(new_row)
        return {"inline_keyboard": rows}


bot_api = RivaBotAPI()


async def get_unjoined_channels(
    user_id: int,
    force_recheck: bool = True,
    was_blocked_prompt: bool = False,
    clicked_verify: bool = False
) -> list[dict]:
    """
    Returns list of required channels/groups that user_id is NOT currently a member of.
    Checks all required channels in parallel in real time and persists exact compliance state to DB.
    """
    channels = await asyncio.to_thread(db.get_required_channels)
    if not channels:
        asyncio.create_task(asyncio.to_thread(db.update_user_rules_status, user_id, [], [], False, False))
        return []

    async def check_one(ch: dict):
        res = await bot_api.api_call("getChatMember", {
            "chat_id": ch["chat_id"],
            "user_id": user_id
        })
        if not res.get("ok"):
            logger.warning(f"getChatMember warning for {ch['chat_id']}: {res.get('description')}")
            return None
        member_obj = res.get("result", {})
        status = member_obj.get("status", "")
        if status in ("creator", "administrator", "member"):
            return None
        if status == "restricted" and member_obj.get("is_member") is True:
            return None
        return ch

    results = await asyncio.gather(*(check_one(c) for c in channels), return_exceptions=True)
    missing = [r for r in results if isinstance(r, dict)]

    # Persist real-time compliance state (only treat as blocked prompt if missing > 0)
    asyncio.create_task(
        asyncio.to_thread(
            db.update_user_rules_status,
            user_id,
            channels,
            missing,
            bool(missing and was_blocked_prompt),
            clicked_verify
        )
    )
    return missing


def build_force_sub_prompt(
    first_name: str,
    missing_channels: list[dict],
    resume_payload: str = None
) -> tuple[str, dict]:
    safe_name = html.escape(first_name or "صديقي")
    text = (
        f"⚠️ <b>عذراً صديقي ({safe_name}) 💟</b>\n\n"
        f"لا يمكنك استخدام البوت ومشاهدة الأفلام إلا بعد الاشتراك في الجروبات/القنوات التالية أولاً ⬇️"
    )
    rows = []
    for ch in missing_channels:
        rows.append([btn(f"📢 اشترك هنا: {ch['title']}", url=ch["invite_link"], style="success")])
    cb_val = f"check_sub:{resume_payload}" if resume_payload else "check_sub"
    rows.append([btn("✅ تم الاشتراك - تحقق الآن", callback_data=cb_val, style="primary")])
    return text, {"inline_keyboard": rows}


def build_admin_reply_keyboard() -> dict:
    """
    Native Telegram Group/Channel Picker keyboard using KeyboardButtonRequestChat.
    Pops up a list of all groups/channels where the admin is an administrator.
    """
    admin_rights = {
        "is_anonymous": False,
        "can_manage_chat": True,
        "can_delete_messages": False,
        "can_manage_video_chats": False,
        "can_restrict_members": False,
        "can_promote_members": False,
        "can_change_info": False,
        "can_invite_users": True
    }
    return {
        "keyboard": [
            [
                {
                    "text": "➕ اختيار جروب لإضافته",
                    "request_chat": {
                        "request_id": 101,
                        "chat_is_channel": False,
                        "user_administrator_rights": admin_rights,
                        "bot_administrator_rights": admin_rights,
                        "request_title": True,
                        "request_username": True
                    }
                },
                {
                    "text": "📢 اختيار قناة لإضافتها",
                    "request_chat": {
                        "request_id": 102,
                        "chat_is_channel": True,
                        "user_administrator_rights": admin_rights,
                        "bot_administrator_rights": admin_rights,
                        "request_title": True,
                        "request_username": True
                    }
                }
            ],
            [
                {"text": "🔒 إغلاق لوحة التحكم"}
            ]
        ],
        "resize_keyboard": True
    }


WEB_DASHBOARD_URL = os.getenv("WEB_DASHBOARD_URL", "http://186.240.145.87:8090/admin")


def _fmt_rel_time(dt) -> str:
    if not dt:
        return "—"
    try:
        now = datetime.now(timezone.utc) if getattr(dt, "tzinfo", None) else datetime.utcnow()
        diff = max(0, int((now - dt).total_seconds()))
        if diff < 60:
            return "الآن"
        if diff < 3600:
            return f"منذ {diff // 60} د"
        if diff < 86400:
            return f"منذ {diff // 3600} س"
        if diff < 86400 * 30:
            return f"منذ {diff // 86400} يوم"
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(dt)[:16]


def _fmt_user_html(u: dict) -> str:
    uid = u.get("user_id")
    fn = (u.get("first_name") or "").strip()
    ln = (u.get("last_name") or "").strip()
    full_name = html.escape(f"{fn} {ln}".strip() or "مستخدم")
    uname = (u.get("username") or "").strip()
    user_link = f'<a href="tg://user?id={uid}">{full_name}</a>'
    if uname:
        return f"{user_link} (@{html.escape(uname)}) [<code>{uid}</code>]"
    return f"{user_link} [<code>{uid}</code>]"


async def notify_admins_user_event(text_html: str, exclude_user_id: int = None):
    enabled = await asyncio.to_thread(db.get_bot_setting, "notify_admin_events", "1")
    if enabled != "1":
        return
    targets = set(ADMIN_IDS) | set(_unlocked_admins)
    for adm_id in targets:
        if exclude_user_id and adm_id == exclude_user_id:
            continue
        try:
            await bot_api.send_message(adm_id, text_html)
        except Exception:
            pass


async def notify_referrer_qualification(q_info: dict):
    """Notifies the referrer instantly on Telegram when someone they invited uses the bot (+1 point)."""
    if not q_info or not q_info.get("referrer_id"):
        return
    ref_id = int(q_info["referrer_id"])
    referred_nm = html.escape(q_info.get("referred_name") or "مستخدم جديد")
    referred_un = f" (@{html.escape(q_info['referred_username'])})" if q_info.get("referred_username") else ""
    t_title = html.escape(q_info.get("target_title") or "رابط المشاركة")
    new_pts = int(q_info.get("new_points") or 1)
    qual_cnt = int(q_info.get("qualified_count") or 1)

    congrats_html = (
        "🎉 <b>مبروك! حصلت على +1 نقطة جديدة 🏆</b>\n\n"
        f"👤 <b>الشخص الذي دخل:</b> {referred_nm}{referred_un}\n"
        f"🔗 <b>الرابط الذي دخل منه:</b> {t_title}\n"
        "✅ <b>الحالة:</b> دخل واستخدم البوت بنجاح\n"
        f"💰 <b>رصيد نقاطك الآن:</b> <code>{new_pts}</code> نقطة (إجمالي <code>{qual_cnt}</code> إحالة نشطة)"
    )
    markup = {
        "inline_keyboard": [
            [btn("🎁 عرض نقاطي وقائمة إحالاتي", callback_data="my_affiliate", style="success")]
        ]
    }
    try:
        await bot_api.send_message(ref_id, congrats_html, reply_markup=markup)
    except Exception:
        pass

    ref_lbl = _fmt_user_html({
        "user_id": ref_id,
        "first_name": q_info.get("referrer_first_name") or "مسوق",
        "username": q_info.get("referrer_username"),
    })
    await notify_admins_user_event(
        f"🏆 <b>نقطة أفلييت جديدة (+1)!</b>\n"
        f"🎯 المسوق: {ref_lbl} (رصيده: <code>{new_pts}</code> نقطة)\n"
        f"👤 المدعو: <b>{referred_nm}</b>{referred_un} [<code>{q_info.get('referred_user_id')}</code>]\n"
        f"🔗 عبر رابط: <b>{t_title}</b>"
    )


async def track_bot_user_bg(
    user_obj: dict,
    action_text: str = None,
    event_type: str = None,
    referral_info: dict = None,
):
    uid = user_obj.get("id")
    if not uid:
        return
    uname = user_obj.get("username")
    fname = user_obj.get("first_name") or "صديقي"
    lname = user_obj.get("last_name")
    lang = user_obj.get("language_code")

    res = await asyncio.to_thread(
        db.register_bot_user,
        uid,
        uname,
        fname,
        lname,
        lang,
        action_text,
        event_type,
        referral_info,
    )
    if res.get("is_new"):
        u_lbl = _fmt_user_html({"user_id": uid, "first_name": fname, "last_name": lname, "username": uname})
        aff_ent = res.get("affiliate_entry")
        ent_inf = res.get("entry_info") or {}
        if aff_ent:
            aff_line = (
                f"\n🔗 <b>دخل عبر أفلييت:</b> {html.escape(aff_ent.get('referrer_label') or '')}"
                f" — <i>{html.escape(aff_ent.get('target_title') or '')}</i>"
            )
        elif ent_inf.get("entry_source") == "direct_link":
            aff_line = (
                f"\n📢 <b>مصدر الدخول:</b> رابط فيلم/مسلسل عام بدون كود مسوق"
                f" — <i>{html.escape(ent_inf.get('target_title') or '')}</i> (<code>?start={html.escape(str(ent_inf.get('referral_code') or ''))}</code>)"
            )
        else:
            aff_line = "\n🤖 <b>مصدر الدخول:</b> فتح البوت مباشرة (/start بدون كود إحالة)"
        await notify_admins_user_event(
            f"🟢 <b>مستخدم جديد دخل البوت!</b>\n👤 {u_lbl}{aff_line}",
            exclude_user_id=uid
        )
    elif res.get("rejoined"):
        u_lbl = _fmt_user_html({"user_id": uid, "first_name": fname, "last_name": lname, "username": uname})
        await notify_admins_user_event(
            f"🔄 <b>مستخدم عاد وفك الحظر عن البوت!</b>\n👤 {u_lbl}",
            exclude_user_id=uid
        )

    if res.get("affiliate_qualified"):
        await notify_referrer_qualification(res["affiliate_qualified"])


async def handle_my_chat_member(upd_obj: dict):
    """Real-time Telegram event when a user blocks/stops or unblocks/restarts the bot."""
    chat = upd_obj.get("chat", {})
    if chat.get("type") != "private":
        return

    from_user = upd_obj.get("from") or upd_obj.get("new_chat_member", {}).get("user") or {}
    uid = from_user.get("id") or chat.get("id")
    if not uid:
        return

    uname = from_user.get("username")
    fname = from_user.get("first_name")
    lname = from_user.get("last_name")
    lang = from_user.get("language_code")

    old_status = (upd_obj.get("old_chat_member") or {}).get("status", "")
    new_status = (upd_obj.get("new_chat_member") or {}).get("status", "")

    if new_status in ("kicked", "left"):
        res = await asyncio.to_thread(db.mark_user_left, uid, uname, fname, lname, "blocked")
        if res.get("changed"):
            u_lbl = _fmt_user_html({
                "user_id": uid,
                "first_name": fname or res.get("first_name"),
                "last_name": lname,
                "username": uname or res.get("username"),
            })
            await notify_admins_user_event(
                f"🚪 <b>مستخدم غادر / أوقف البوت:</b>\n👤 {u_lbl}",
                exclude_user_id=uid
            )
    elif new_status in ("member", "administrator") and old_status in ("kicked", "left"):
        await track_bot_user_bg(from_user, action_text="🔄 عاد لتشغيل البوت")


def _fmt_rules_badge_short(u: dict) -> str:
    rs = u.get("rules_status") or "unknown"
    jc = u.get("joined_channels_count") or 0
    rc = u.get("required_channels_count") or 0
    if rs == "compliant":
        return f"✅ ملتزم ({jc}/{rc})"
    if rs == "partial":
        return f"⚠️ جزئي ({jc}/{rc})"
    if rs == "left_channel":
        return f"🚪 ترك جروب ({jc}/{rc})"
    if rs == "unsubscribed":
        return f"❌ لم يشترك ({jc}/{rc})"
    if rs == "no_rules":
        return "🔓 بدون شروط"
    return "⏳ غير مفحوص"


def _fmt_interactions_short(u: dict) -> str:
    mv = int(u.get("movies_watched_count") or 0)
    ep = int(u.get("episodes_watched_count") or 0)
    sr = int(u.get("searches_count") or 0)
    br = int(u.get("browses_count") or 0)
    parts = []
    if mv:
        parts.append(f"🎬{mv} فيلم")
    if ep:
        parts.append(f"📺{ep} حلقة")
    if sr:
        parts.append(f"🔍{sr} بحث")
    if br:
        parts.append(f"📂{br} تصفح")
    return " • ".join(parts) if parts else "فتح البوت فقط"


async def build_admin_panel() -> tuple[str, dict]:
    channels, u_stats, v_counts, notif_val, w_pool = await asyncio.gather(
        asyncio.to_thread(db.get_required_channels),
        asyncio.to_thread(db.get_bot_users_stats),
        asyncio.to_thread(db.get_vault_media_counts),
        asyncio.to_thread(db.get_bot_setting, "notify_admin_events", "1"),
        asyncio.to_thread(db.get_uploader_pool_summary),
    )
    if not channels:
        ch_list_str = "لا توجد جروبات أو قنوات إجبارية مضافة حالياً (البوت مفتوح للجميع)."
    else:
        lines = []
        for idx, c in enumerate(channels, 1):
            lines.append(f"{idx}. <b>{html.escape(c['title'])}</b> — <a href=\"{html.escape(c['invite_link'])}\">الرابط</a>")
        ch_list_str = "\n".join(lines)

    notif_on = (notif_val == "1")
    notif_label = "🔔 إشعارات الدخول/الخروج: مفعلة ✅" if notif_on else "🔕 إشعارات الدخول/الخروج: متوقفة ❌"

    non_comp = (
        int(u_stats.get("unsubscribed_users") or 0)
        + int(u_stats.get("partial_users") or 0)
        + int(u_stats.get("left_channel_users") or 0)
    )
    pool_sz = int(w_pool.get("active_pool_size") or 1)
    tot_helpers = int(w_pool.get("total_helpers") or 0)

    text = (
        "⚙️ <b>لوحة متابعة وإدارة البوت الحية (Admin Panel)</b>\n\n"
        f"👥 <b>المستخدمون:</b> 🟢 نشط: <code>{u_stats['active_users']}</code> | 🚪 طلعوا: <code>{u_stats['left_users']}</code> | الكل: <code>{u_stats['total_users']}</code>\n"
        f"⚡ <b>النشاط:</b> آخر 24س: <code>{u_stats['active_24h']}</code> | انضم اليوم: <code>+{u_stats['joined_today']}</code>\n\n"
        f"🎁 <b>نظام الأفلييت والنقاط:</b>\n"
        f"• 🏆 إجمالي النقاط: <code>{u_stats.get('total_points', 0)}</code> | المسوقون النشطون: <code>{u_stats.get('active_affiliates', 0)}</code>\n"
        f"• 🔗 دخلوا عبر روابط إحالة: <code>{u_stats.get('referred_users', 0)}</code> (منهم مؤهل +1 نقطة: <code>{u_stats.get('qualified_referrals', 0)}</code>)\n\n"
        f"📢 <b>الالتزام بالشروط والجروبات:</b>\n"
        f"• ✅ ملتزم بكل الجروبات: <code>{u_stats.get('compliant_users', 0)}</code>\n"
        f"• ❌ لم يكمل / ترك الجروبات: <code>{non_comp}</code>\n"
        f"• 🍿 دخلوا واتفرجوا فعلاً: <code>{u_stats.get('watchers_users', 0)}</code> مستخدم "
        f"(🎬 <code>{u_stats.get('total_movies_watched', 0)}</code> فيلم | 📺 <code>{u_stats.get('total_episodes_watched', 0)}</code> حلقة)\n\n"
        f"🎬 <b>مكتبة الأرشيف الجاهزة بالبوت:</b>\n"
        f"• أفلام: <code>{v_counts.get('movies', 0)}</code> | مسلسلات: <code>{v_counts.get('series', 0)}</code> (<code>{v_counts.get('episodes', 0)}</code> حلقة)\n"
        f"• ⚡ بوتات الرفع المتوازي النشطة: <code>{pool_sz}</code> بوت (1 أساسي + {tot_helpers} وركر)\n\n"
        f"📋 <b>الجروبات والقنوات الإجبارية المفعلة:</b>\n{ch_list_str}\n\n"
        f"🌐 <b>لوحة الويب المفصلة:</b> <code>{html.escape(WEB_DASHBOARD_URL)}</code>"
    )
    rows = [
        [
            btn(f"⚡ بوتات الرفع السريع ({pool_sz} بوت نشط)", callback_data="adm_workers", style="success"),
        ],
        [
            btn(f"🏆 مسوقو الأفلييت ({u_stats.get('active_affiliates', 0)})", callback_data="adm_users:affiliates:1", style="success"),
            btn(f"🔗 دخلوا بإحالة ({u_stats.get('referred_users', 0)})", callback_data="adm_users:referred:1", style="primary"),
        ],
        [
            btn("📋 تقرير الشروط والمشاهدة", callback_data="adm_compliance", style="primary"),
        ],
        [
            btn(f"🟢 الحاليين ({u_stats['active_users']})", callback_data="adm_users:active:1", style="success"),
            btn(f"🚪 اللي طلعوا ({u_stats['left_users']})", callback_data="adm_users:left:1", style="danger")
        ],
        [
            btn("📜 سجل الدخول والخروج والنشاط", callback_data="adm_events:all:1", style="primary")
        ],
        [
            btn("📦 متابعة الرفع والأرشيف", callback_data="adm_vault"),
            btn("🔍 فحص شامل (نشاط + جروبات)", callback_data="adm_scan")
        ],
        [
            btn("🌐 فتح لوحة الويب المفصلة", url=WEB_DASHBOARD_URL, style="primary")
        ],
        [
            btn(notif_label, callback_data="adm_toggle_notif")
        ],
        [
            btn("📣 إرسال إعلان لجميع المشتركين", callback_data="adm_bcast", style="primary")
        ]
    ]
    for c in channels:
        rows.append([btn(f"🗑️ حذف اشتراك: {c['title']}", callback_data=f"adm_del:{c['id']}", style="danger")])
    rows.append([
        btn("🔄 تحديث", callback_data="adm_home"),
        btn("🔒 قفل اللوحة", callback_data="adm_lock"),
        btn("🏠 الرئيسية", callback_data="menu", style="success")
    ])
    return text, {"inline_keyboard": rows}


async def build_admin_compliance_view() -> tuple[str, dict]:
    rep = await asyncio.to_thread(db.get_compliance_and_funnel_summary)
    st = rep["stats"]
    chs = rep["channels"]
    tot = max(1, int(st.get("total_users") or 0))
    comp = int(st.get("compliant_users") or 0)
    part = int(st.get("partial_users") or 0)
    unsub = int(st.get("unsubscribed_users") or 0)
    left_ch = int(st.get("left_channel_users") or 0)
    watchers = int(st.get("watchers_users") or 0)
    binge = int(st.get("binge_watchers") or 0)

    comp_pct = round((comp / tot) * 100, 1) if st.get("total_users") else 0
    watch_pct = round((watchers / tot) * 100, 1) if st.get("total_users") else 0

    lines = [
        "📋 <b>تقرير التزام المستخدمين بالشروط والمشاهدة</b>\n",
        "1️⃣ <b>قمع رحلة المستخدمين (Funnel):</b>",
        f"• 👥 دخلوا البوت: <code>{st.get('total_users', 0)}</code> (100%)",
        f"• ✅ التزموا واشتركوا في كل الجروبات: <code>{comp}</code> (<code>{comp_pct}%</code>)",
        f"• ⚠️ اشتركوا في بعض الجروبات فقط: <code>{part}</code>",
        f"• ❌ توقفوا عند الشروط ولم يشتركوا: <code>{unsub}</code>",
        f"• 🚪 اشتركوا ثم غادروا جروباً لاحقاً: <code>{left_ch}</code>",
        f"• 🍿 دخلوا وشاهدوا أفلام/حلقات فعلاً: <code>{watchers}</code> (<code>{watch_pct}%</code>)",
        f"• 🔥 كملوا مشاهدة أكثر من عمل/حلقة: <code>{binge}</code>\n",
        "2️⃣ <b>نسبة الاشتراك في كل جروب إجباري على حدة:</b>"
    ]
    if not chs:
        lines.append("لا توجد جروبات إجبارية مضافة حالياً.")
    else:
        for c in chs:
            chk = max(1, c["checked"])
            pct = round((c["joined"] / chk) * 100) if c["checked"] else 0
            lines.append(f"• 📢 <b>{html.escape(c['title'])}</b>: مشترك <code>{c['joined']}/{c['checked']}</code> (<code>{pct}%</code>)")

    rows = [
        [
            btn(f"✅ الملتزمين ({comp})", "adm_users:compliant:1", style="success"),
            btn(f"❌ غير الملتزمين ({part + unsub + left_ch})", "adm_users:noncompliant:1", style="danger"),
        ],
        [
            btn(f"🍿 المشاهدين فعلياً ({watchers})", "adm_users:watchers:1", style="primary"),
            btn("🔍 فحص الجروبات الآن", "adm_scan"),
        ],
        [
            btn("🔄 تحديث", "adm_compliance"),
            btn("🔙 لوحة الأدمن", "adm_home", style="primary")
        ]
    ]
    return "\n".join(lines), {"inline_keyboard": rows}


async def build_admin_user_detail_view(uid: int, back_filter: str = "active", back_page: int = 1) -> tuple[str, dict]:
    u = await asyncio.to_thread(db.get_single_user_full_profile, uid)
    if not u:
        return "⚠️ لم يتم العثور على هذا المستخدم.", {
            "inline_keyboard": [[btn("🔙 رجوع", f"adm_users:{back_filter}:{back_page}")]]
        }

    u_str = _fmt_user_html(u)
    st_str = "🟢 متواجد بالبوت" if u.get("is_active") else "🚪 غادر / حظر البوت"
    rules_lbl = _fmt_rules_badge_short(u)

    ref_line = "• 🤖 مصدر الدخول: فتح البوت مباشرة (/start بدون كود إحالة)"
    if u.get("referred_by"):
        ref_who = f"@{html.escape(u['referrer_username'])}" if u.get("referrer_username") else html.escape(u.get("referrer_first_name") or str(u["referred_by"]))
        ref_tt = html.escape(u.get("referred_target_title") or "رابط الدعوة")
        ref_q = "✅ مؤهل (+1 نقطة)" if u.get("referral_qualified") else "⏳ لم يكمل الاستخدام بعد"
        ref_line = f"• 🔗 دخل عبر المسوق: <b>{ref_who}</b> [<code>{u['referred_by']}</code>] — عبر «{ref_tt}» ({ref_q})"
    elif u.get("entry_source") == "direct_link":
        ref_tt = html.escape(u.get("referred_target_title") or "عمل فني")
        ref_cd = html.escape(u.get("referred_via_code") or "direct")
        ref_line = f"• 📢 مصدر الدخول: <b>رابط عام بدون كود مسوق</b> — «{ref_tt}» (<code>?start={ref_cd}</code>)"

    lines = [
        f"👤 <b>بطاقة تفصيل المستخدم وتفاعلاته</b>\n",
        f"• المستخدم: {u_str}",
        f"• حالة البوت: <b>{st_str}</b>",
        f"• 🏆 نقاط الأفلييت: <code>{u.get('points', 0)}</code> نقطة (دعا <code>{u.get('total_referrals_entered', 0)}</code> مستخدم | مؤهل: <code>{u.get('qualified_referrals_count', 0)}</code>)",
        ref_line,
        f"• موقف الشروط: <b>{rules_lbl}</b> (ظهرت له شاشة الشروط <code>{u.get('force_sub_blocks_count', 0)}</code> مرة)",
        f"• انضم: {_fmt_rel_time(u.get('joined_at'))} | آخر ظهور: {_fmt_rel_time(u.get('last_active_at'))}\n",
        "📊 <b>كيف تفاعل هذا المستخدم بالضبط؟</b>",
        f"• 🎬 أفلام فتحها وشاهدها: <code>{u.get('movies_watched_count', 0)}</code>",
        f"• 📺 حلقات مسلسلات شاهدها: <code>{u.get('episodes_watched_count', 0)}</code>",
        f"• 🔍 عمليات بحث قام بها: <code>{u.get('searches_count', 0)}</code>",
        f"• 📂 تصفح أقسام وقوائم: <code>{u.get('browses_count', 0)}</code>",
        f"• إجمالي الحركات بالبوت: <code>{u.get('actions_count', 1)}</code>\n",
        "📢 <b>تفاصيل اشتراكه في الجروبات الإجبارية:</b>"
    ]
    ch_list = u.get("channels_list") or []
    if not ch_list:
        lines.append("• لم يتم فحص الجروبات بعد — اضغط «فحص اشتراكه الآن» بالأسفل.")
    else:
        for c in ch_list:
            mark = "✅ مشترك" if c.get("joined") else "❌ غير مشترك"
            lines.append(f"• {mark} — <b>{html.escape(c.get('title') or '')}</b>")

    lines.append("\n📜 <b>آخر خطواته داخل البوت بالترتيب:</b>")
    evs = u.get("events") or []
    if not evs:
        lines.append(f"• {html.escape(u.get('last_action') or 'فتح البوت')}")
    else:
        for ev in evs[:10]:
            t_rel = _fmt_rel_time(ev.get("created_at"))
            det = html.escape((ev.get("details") or ev.get("event_type") or "")[:65])
            lines.append(f"• ({t_rel}) {det}")

    rows = [
        [btn("🔍 فحص اشتراكه بالجروبات الآن", f"adm_uchk:{uid}:{back_filter}:{back_page}", style="success")],
        [btn("🔙 رجوع لقائمة المستخدمين", f"adm_users:{back_filter}:{back_page}", style="primary")]
    ]
    return "\n".join(lines), {"inline_keyboard": rows}


async def build_admin_users_view(filter_mode: str = "active", page: int = 1) -> tuple[str, dict]:
    res, stats = await asyncio.gather(
        asyncio.to_thread(db.get_bot_users_paginated, filter_mode, page, 8),
        asyncio.to_thread(db.get_bot_users_stats)
    )
    items = res["items"]
    total = res["total"]
    total_pages = res["total_pages"]

    titles = {
        "active": f"🟢 المستخدمون الحاليون النشطون ({total})",
        "left": f"🚪 المستخدمون الذين غادروا / حظروا البوت ({total})",
        "compliant": f"✅ الملتزمون بكل الجروبات ({total})",
        "noncompliant": f"❌ غير الملتزمين بالجروبات ({total})",
        "watchers": f"🍿 المشاهدون فعلياً للأفلام والمسلسلات ({total})",
        "affiliates": f"🏆 مسوقو الأفلييت وأصحاب النقاط ({total})",
        "referred": f"🔗 الأشخاص الذين دخلوا عبر روابط الأفلييت ({total})",
        "online24h": f"⚡ المتفاعلون آخر 24 ساعة ({total})",
        "today": f"🆕 المنضمون آخر 24 ساعة ({total})",
    }
    header = titles.get(filter_mode, titles["active"])
    lines = [
        f"<b>{header}</b> — صفحة {page}/{total_pages}",
        "<i>اضغط على زر المستخدم بالأسفل لعرض كل تفاصيل تفاعله وإحالاته ⬇️</i>\n"
    ]

    detail_btns = []
    if not items:
        lines.append("لا يوجد مستخدمون في هذا القسم حالياً.")
    else:
        start_idx = (page - 1) * 8 + 1
        cur_btn_row = []
        for idx, u in enumerate(items, start_idx):
            u_str = _fmt_user_html(u)
            r_badge = _fmt_rules_badge_short(u)
            i_summary = _fmt_interactions_short(u)
            pts = int(u.get("points") or 0)
            pts_tag = f" | 🏆{pts}ن ({u.get('qualified_referrals_count', 0)}/{u.get('total_referrals_entered', 0)})" if (pts > 0 or u.get("total_referrals_entered")) else ""
            ref_tag = ""
            if u.get("referred_by"):
                r_by = f"@{html.escape(u['referrer_username'])}" if u.get("referrer_username") else html.escape(u.get("referrer_first_name") or str(u["referred_by"]))
                r_tt = html.escape((u.get("referred_target_title") or "رابط")[:24])
                ref_tag = f"\n   └ 🔗 دخل عبر: <b>{r_by}</b> ({r_tt})"
            elif u.get("entry_source") == "direct_link":
                r_tt = html.escape((u.get("referred_target_title") or "رابط عام")[:28])
                ref_tag = f"\n   └ 📢 رابط عام بدون مسوق: <b>{r_tt}</b>"
            if filter_mode == "left" or not u.get("is_active"):
                left_t = _fmt_rel_time(u.get("left_at") or u.get("last_active_at"))
                lines.append(
                    f"{idx}. {u_str}\n"
                    f"   └ {r_badge} | {i_summary}{pts_tag} | خرج {left_t}{ref_tag}"
                )
            else:
                seen_t = _fmt_rel_time(u.get("last_active_at"))
                lines.append(
                    f"{idx}. {u_str}\n"
                    f"   └ {r_badge} | {i_summary}{pts_tag} | ظهور: {seen_t}{ref_tag}"
                )
            short_nm = ((u.get("first_name") or "مستخدم").strip())[:12]
            cur_btn_row.append(btn(f"📊 #{idx} {short_nm}", f"adm_uinfo:{u['user_id']}:{filter_mode}:{page}"))
            if len(cur_btn_row) == 2:
                detail_btns.append(cur_btn_row)
                cur_btn_row = []
        if cur_btn_row:
            detail_btns.append(cur_btn_row)

    rows = []
    rows.extend(detail_btns)
    rows.append([
        btn(f"🟢 حاليين ({stats['active_users']})", "adm_users:active:1"),
        btn(f"🏆 مسوقين ({stats.get('active_affiliates', 0)})", "adm_users:affiliates:1"),
        btn(f"🔗 بإحالة ({stats.get('referred_users', 0)})", "adm_users:referred:1"),
    ])
    rows.append([
        btn(f"✅ ملتزم ({stats.get('compliant_users', 0)})", "adm_users:compliant:1"),
        btn(f"🍿 شاهدوا ({stats.get('watchers_users', 0)})", "adm_users:watchers:1"),
        btn(f"🚪 طلعوا ({stats['left_users']})", "adm_users:left:1"),
    ])
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(btn("⬅️ السابق", f"adm_users:{filter_mode}:{page-1}"))
        nav.append(btn(f"{page}/{total_pages}", "noop"))
        if page < total_pages:
            nav.append(btn("التالي ➡️", f"adm_users:{filter_mode}:{page+1}"))
        rows.append(nav)

    rows.append([
        btn("🔄 تحديث", f"adm_users:{filter_mode}:{page}"),
        btn("🔙 لوحة الأدمن", "adm_home", style="primary")
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


async def build_admin_events_view(event_filter: str = "all", page: int = 1) -> tuple[str, dict]:
    res = await asyncio.to_thread(db.get_recent_bot_events, page, 12, event_filter)
    items = res["items"]
    total = res["total"]
    total_pages = res["total_pages"]

    icons = {
        "joined": "🟢 دخول جديد",
        "left": "🚪 خروج/حظر",
        "rejoined": "🔄 عودة للبوت",
        "affiliate_entered": "🔗 دخل برابط أفلييت",
        "affiliate_qualified": "🏆 نقطة أفلييت (+1)",
        "points_adjusted": "⚖️ تعديل نقاط",
        "rules_shown": "⚠️ شاشة الشروط",
        "rules_failed": "❌ فشل تحقق اشتراك",
        "rules_passed": "✅ أتم الجروبات",
        "rules_left_channel": "🚪 ترك جروب إجباري",
        "watch_movie": "🎬 مشاهدة فيلم",
        "watch_episode": "📺 مشاهدة حلقة",
        "search": "🔍 بحث",
        "browse": "📂 تصفح قسم",
    }
    sub_map = {"all": "جميع التفاعلات", "joins_leaves": "الدخول والخروج فقط", "affiliate": "الأفلييت والنقاط"}
    sub = sub_map.get(event_filter, "مخصص")
    lines = [f"📜 <b>سجل أحداث البوت اللحظي ({sub})</b> — إجمالي <code>{total}</code>\n"]

    if not items:
        lines.append("لا توجد أحداث مسجلة بعد.")
    else:
        for ev in items:
            ev_t = _fmt_rel_time(ev.get("created_at"))
            badge = icons.get(ev.get("event_type"), "📌 نشاط")
            u_str = _fmt_user_html(ev)
            det = html.escape((ev.get("details") or "")[:75])
            lines.append(f"• <b>{badge}</b> ({ev_t})\n  👤 {u_str}" + (f"\n  └ {det}" if det else ""))

    rows = [
        [
            btn("📋 كل الأحداث", "adm_events:all:1"),
            btn("🎁 أحداث الأفلييت", "adm_events:affiliate:1"),
            btn("🚪 دخول وخروج", "adm_events:joins_leaves:1"),
        ]
    ]
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(btn("⬅️ السابق", f"adm_events:{event_filter}:{page-1}"))
        nav.append(btn(f"{page}/{total_pages}", "noop"))
        if page < total_pages:
            nav.append(btn("التالي ➡️", f"adm_events:{event_filter}:{page+1}"))
        rows.append(nav)

    rows.append([
        btn("🔄 تحديث", f"adm_events:{event_filter}:{page}"),
        btn("🔙 لوحة الأدمن", "adm_home", style="primary")
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


async def build_admin_vault_view() -> tuple[str, dict]:
    v = await asyncio.to_thread(db.get_vault_admin_summary)
    rem_movies = max(0, v.get("total_db_movies", 0) - v.get("ready_movies", 0))
    rem_eps = max(0, v.get("total_db_episodes", 0) - v.get("ready_episodes", 0))

    lines = [
        "📦 <b>متابعة الأرشيف والرفع التلقائي</b>\n",
        f"🎬 <b>الأفلام الجاهزة:</b> <code>{v.get('ready_movies', 0)}</code> (متبقي بالطابور: <code>{rem_movies}</code>)",
        f"📺 <b>المسلسلات المتاحة:</b> <code>{v.get('ready_series', 0)}</code> مسلسل",
        f"🎞️ <b>الحلقات الجاهزة:</b> <code>{v.get('ready_episodes', 0)}</code> (متبقي بالطابور: <code>{rem_eps}</code>)",
        f"💾 <b>إجمالي المساحة المرفوعة:</b> <code>{v.get('total_gb', 0)} GB</code>",
        f"🚀 <b>تم رفعه آخر 24 ساعة:</b> <code>{v.get('uploaded_24h', 0)}</code> فيديو\n",
        "🆕 <b>آخر الفيديوهات المرفوعة للقناة المخزنية:</b>"
    ]
    recent = v.get("recent_uploads") or []
    if not recent:
        lines.append("لا توجد رفوعات حديثة.")
    else:
        for r in recent:
            ico = "🎬" if r.get("media_type") == "movie" else "📺"
            t_str = html.escape((r.get("title") or "بدون عنوان")[:45])
            rel = _fmt_rel_time(r.get("created_at"))
            lines.append(f"• {ico} <b>{t_str}</b> — <code>{r.get('size_mb', 0)} MB</code> ({rel})")

    rows = [
        [btn("⚡ إدارة بوتات الرفع السريع (Workers)", "adm_workers", style="success")],
        [btn("🌐 فتح لوحة الويب المفصلة", url=WEB_DASHBOARD_URL, style="primary")],
        [
            btn("🔄 تحديث", "adm_vault"),
            btn("🔙 لوحة الأدمن", "adm_home", style="primary")
        ]
    ]
    return "\n".join(lines), {"inline_keyboard": rows}


async def verify_uploader_token_live(token: str) -> dict:
    """Verifies a @BotFather token via getMe + getChatMember on TELEGRAM_CHANNEL_ID."""
    tok = (token or "").strip()
    if tok == (TELEGRAM_BOT_TOKEN or "").strip():
        return {
            "valid": False,
            "is_primary": True,
            "error": "هذا هو توكن البوت الأساسي (@riva18_bot) وهو مفعل بالفعل كبوت رفع أساسي.",
        }
    try:
        sess = bot_api.session
        async with sess.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=aiohttp.ClientTimeout(total=10)) as r:
            me_data = await r.json()
        if not me_data.get("ok"):
            return {"valid": False, "error": me_data.get("description") or "توكن غير صالح أو ملغي من BotFather"}
        me = me_data["result"]
        bid = int(me["id"])
        uname = me.get("username") or ""
        fname = me.get("first_name") or ""

        channel_ok = False
        ch_err = None
        async with sess.get(
            f"https://api.telegram.org/bot{tok}/getChatMember",
            params={"chat_id": str(TELEGRAM_CHANNEL_ID), "user_id": bid},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as cr:
            cm_data = await cr.json()

        if cm_data.get("ok"):
            res = cm_data.get("result") or {}
            st = res.get("status", "")
            can_post = res.get("can_post_messages", True if st == "creator" else False)
            if st in ("administrator", "creator") and can_post is not False:
                channel_ok = True
            else:
                ch_err = f"موجود بالقناة بحالة ({st}) ويحتاج صلاحية النشر (Post Messages)"
        else:
            ch_err = "لم يتم إضافته كأدمن في قناة الأرشيف بعد"

        return {
            "valid": True,
            "bot_id": bid,
            "username": uname,
            "first_name": fname,
            "channel_ok": channel_ok,
            "error": ch_err,
        }
    except Exception as e:
        return {"valid": False, "error": f"خطأ أثناء الاتصال بتيليجرام: {str(e)[:120]}"}


async def send_worker_vault_checkin(token: str, bot_name: str, bot_username: str, bot_id: int | str) -> tuple[bool, str | None]:
    """Sends a live test message FROM the worker bot's own token into TELEGRAM_CHANNEL_ID bearing its exact name."""
    try:
        disp = f"{bot_name} (@{bot_username})" if (bot_name and bot_username) else (f"@{bot_username}" if bot_username else (bot_name or f"Bot {bot_id}"))
        msg_html = (
            f"✅ <b>رسالة فحص وتأكيد من البوت نفسه</b>\n"
            f"🤖 <b>اسم البوت:</b> {html.escape(disp)}\n"
            f"🆔 <b>المعرف:</b> <code>{html.escape(str(bot_id or ''))}</code>\n"
            f"📡 <b>الحالة:</b> التوكن وصلاحية النشر في الخزنة يعملان 100% 🚀"
        )
        sess = bot_api.session
        async with sess.post(
            f"https://api.telegram.org/bot{token.strip()}/sendMessage",
            json={"chat_id": str(TELEGRAM_CHANNEL_ID), "text": msg_html, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            res = await r.json()
        if res.get("ok"):
            return True, None
        return False, res.get("description") or "فشل إرسال الرسالة للقناة"
    except Exception as e:
        return False, str(e)[:120]


async def ingest_worker_tokens_from_text(raw_text: str) -> tuple[list[dict], list[str]]:
    """Extracts 1 or multiple bot tokens from raw text, verifies each, and saves to uploader_bots."""
    found = []
    seen = set()
    for m in _BOT_TOKEN_REGEX.finditer(raw_text or ""):
        t = m.group(0).strip()
        if t not in seen:
            seen.add(t)
            found.append(t)

    if not found:
        return [], ["⚠️ لم يتم العثور على أي توكن بوت صالح في النص المرسل."]

    checks = await asyncio.gather(*(verify_uploader_token_live(t) for t in found))
    saved_rows = []
    errors = []
    for tok, info in zip(found, checks):
        masked = db.mask_bot_token(tok)
        if not info.get("valid"):
            errors.append(f"❌ <code>{html.escape(masked)}</code>: {html.escape(info.get('error') or 'غير صالح')}")
            continue
        is_ch_ok = bool(info.get("channel_ok"))
        ch_err = info.get("error")
        if is_ch_ok:
            sent_ok, send_err = await send_worker_vault_checkin(
                tok,
                info.get("first_name") or "",
                info.get("username") or "",
                info.get("bot_id") or "",
            )
            if not sent_ok:
                is_ch_ok = False
                ch_err = send_err
        row = await asyncio.to_thread(
            db.upsert_uploader_bot,
            tok,
            info.get("bot_id"),
            info.get("username"),
            info.get("first_name"),
            is_ch_ok,
            ch_err,
        )
        row["masked_token"] = masked
        saved_rows.append(row)
    return saved_rows, errors


async def recheck_all_worker_bots() -> tuple[int, int]:
    """Re-checks Vault channel admin permissions for all saved uploader_bots."""
    workers = await asyncio.to_thread(db.get_uploader_bots)
    if not workers:
        return 0, 0
    checks = await asyncio.gather(*(verify_uploader_token_live(w["bot_token"]) for w in workers))
    ok_cnt = 0
    for w, info in zip(workers, checks):
        if info.get("valid"):
            is_ok = bool(info.get("channel_ok"))
            if is_ok:
                ok_cnt += 1
            await asyncio.to_thread(
                db.upsert_uploader_bot,
                w["bot_token"],
                info.get("bot_id") or w.get("bot_id"),
                info.get("username") or w.get("bot_username"),
                info.get("first_name") or w.get("bot_name"),
                is_ok,
                info.get("error"),
            )
    return ok_cnt, len(workers)


async def build_admin_workers_view() -> tuple[str, dict]:
    pool = await asyncio.to_thread(db.get_uploader_pool_summary)
    workers = pool["workers"]
    active_pool = pool["active_pool_size"]
    ch_id_str = str(TELEGRAM_CHANNEL_ID)

    lines = [
        "⚡ <b>إدارة بوتات الرفع المتوازي السريع (Uploader Workers)</b>\n",
        f"🚀 <b>إجمالي قوة الرفع الحالية:</b> <code>{active_pool}</code> بوت يرفعون بالتوازي بدون انتظار!",
        f"📡 <b>قناة الأرشيف المخزنية (Vault ID):</b> <code>{html.escape(ch_id_str)}</code>\n",
        "📌 <b>خطوات إضافة وركر جديد في ثواني:</b>",
        "1️⃣ أنشئ بوت جديد من @BotFather وانسخ التوكن.",
        f"2️⃣ ارفعه <b>مشرف (Admin)</b> بصلاحية <b>نشر الرسائل (Post Messages)</b> داخل قناة الأرشيف (<code>{html.escape(ch_id_str)}</code>).",
        "3️⃣ اضغط <b>«➕ إضافة توكن بوت وركر»</b> بالأسفل وأرسل التوكن (أو عدة توكنات معاً في رسالة واحدة).\n",
        "🤖 <b>قائمة بوتات الرفع المسجلة:</b>",
        f"👑 <b>البوت الأساسي:</b> @{html.escape(BOT_USERNAME)} — 🟢 نشط دائماً "
        f"(رفع منذ التفعيل: <code>{pool.get('primary_uploads', 0)}</code> عمل | <code>{pool.get('primary_gb', 0)} GB</code>)",
    ]

    bot_rows = []
    if not workers:
        lines.append("• لا يوجد بوتات وركر إضافية مضافة بعد.")
    else:
        for idx, w in enumerate(workers, 1):
            uname = f"@{html.escape(w['bot_username'])}" if w.get("bot_username") else html.escape(w.get("bot_name") or f"Bot #{w['id']}")
            bname = html.escape(w.get("bot_name") or "")
            if not w.get("is_active"):
                st = "⏸ متوقف مؤقتاً"
            elif w.get("channel_ok"):
                st = "🟢 نشط وأدمن بالقناة"
            else:
                st = "🟡 بانتظار ترقيته أدمن بالقناة"

            up_cnt = int(w.get("uploads_count") or 0)
            up_gb = w.get("uploaded_gb", 0)
            masked = html.escape(w.get("masked_token") or "")
            err_line = ""
            if w.get("is_active") and w.get("last_error"):
                err_line = f"\n   ⚠️ <i>{html.escape(str(w['last_error'])[:95])}</i>"

            lines.append(
                f"{idx}. <b>{uname}</b> ({bname}) — {st}\n"
                f"   └ تم رفع: <code>{up_cnt}</code> فيديو (<code>{up_gb} GB</code>) | <code>{masked}</code>{err_line}"
            )
            short_lbl = f"@{w['bot_username']}" if w.get("bot_username") else f"#{idx}"
            tog_lbl = f"⏸ إيقاف {short_lbl}" if w.get("is_active") else f"▶️ تشغيل {short_lbl}"
            bot_rows.append([
                btn(tog_lbl, f"adm_wkr_tog:{w['id']}"),
                btn(f"🗑 حذف {short_lbl}", f"adm_wkr_del:{w['id']}", style="danger"),
            ])

    rows = [
        [btn("➕ إضافة توكن بوت وركر (واحد أو عدة توكنات)", "adm_wkr_add", style="success")],
        [btn("🧪 تجربة إرسال باسم كل بوت للخزنة", "adm_wkr_ping", style="primary")],
        [
            btn("📋 نسخ ID قناة الأرشيف", copy_text=ch_id_str),
            btn("🔄 فحص صلاحية القناة للكل", "adm_wkr_recheck", style="primary"),
        ],
    ]
    rows.extend(bot_rows)
    rows.append([
        btn("🔄 تحديث", "adm_workers"),
        btn("🔙 لوحة الأدمن", "adm_home", style="primary"),
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


async def run_silent_users_scan(admin_chat_id: int, message_id: int = None):
    """
    Silently checks every user in bot_users via sendChatAction('typing') AND getChatMember on all required channels!
    Detects both bot blocks AND exact group compliance without sending a single message.
    """
    users = await asyncio.to_thread(db.get_all_bot_users_basic)
    total = len(users)
    if total == 0:
        markup = {"inline_keyboard": [[btn("🔙 رجوع للوحة الأدمن", "adm_home", style="primary")]]}
        await bot_api.edit_or_send(admin_chat_id, message_id, "⚠️ لا يوجد مستخدمون مسجلون لفحصهم بعد.", reply_markup=markup)
        return

    await bot_api.edit_or_send(
        admin_chat_id,
        message_id,
        f"🔍 <b>جاري الفحص الصامت الشامل لـ <code>{total}</code> مستخدم (حالة البوت + اشتراك الجروبات الإجبارية)...</b>"
    )

    active_now = 0
    left_now = 0
    compliant_now = 0
    non_compliant_now = 0

    for u in users:
        uid = u["user_id"]
        res = await bot_api.api_call("sendChatAction", {"chat_id": uid, "action": "typing"})
        if res.get("ok"):
            active_now += 1
            if not u.get("is_active"):
                await asyncio.to_thread(
                    db.register_bot_user,
                    uid,
                    u.get("username"),
                    u.get("first_name"),
                    None,
                    None,
                    "🔄 نشط (تم التأكد بالفحص)"
                )
        else:
            desc = (res.get("description") or "").lower()
            if "blocked" in desc or "deactivated" in desc or "chat not found" in desc or "forbidden" in desc:
                left_now += 1
                reason = "deactivated" if "deactivated" in desc else "blocked"
                await asyncio.to_thread(db.mark_user_left, uid, u.get("username"), u.get("first_name"), None, reason)
            else:
                if u.get("is_active"):
                    active_now += 1
                else:
                    left_now += 1

        # Also check required channel memberships silently
        missing = await get_unjoined_channels(uid, force_recheck=True, was_blocked_prompt=False, clicked_verify=False)
        if not missing:
            compliant_now += 1
        else:
            non_compliant_now += 1
        await asyncio.sleep(0.05)

    summary = (
        "✅ <b>اكتمل الفحص الصامت الشامل (البوت + الجروبات)!</b>\n\n"
        f"👥 إجمالي من تم فحصهم: <code>{total}</code>\n"
        f"🟢 المتواجدون النشطون بالبوت: <code>{active_now}</code> | 🚪 غادروا البوت: <code>{left_now}</code>\n"
        f"✅ <b>ملتزمون بكل الجروبات الإجبارية:</b> <code>{compliant_now}</code>\n"
        f"❌ <b>غير مشتركين في كل الجروبات:</b> <code>{non_compliant_now}</code>"
    )
    markup = {
        "inline_keyboard": [
            [btn("📋 عرض تقرير الشروط التفصيلي", "adm_compliance", style="primary")],
            [
                btn(f"✅ الملتزمين ({compliant_now})", "adm_users:compliant:1", style="success"),
                btn(f"❌ غير الملتزمين ({non_compliant_now})", "adm_users:noncompliant:1", style="danger"),
            ],
            [btn("🔙 العودة للوحة الأدمن", "adm_home", style="primary")]
        ]
    }
    await bot_api.edit_or_send(admin_chat_id, message_id, summary, reply_markup=markup)


async def run_broadcast(admin_chat_id: int, source_chat_id: int, source_msg_id: int):
    user_ids = await asyncio.to_thread(db.get_all_active_user_ids)
    total = len(user_ids)
    if total == 0:
        await bot_api.send_message(admin_chat_id, "⚠️ لا يوجد مشتركين مسجلين بعد.")
        return

    status_res = await bot_api.send_message(
        admin_chat_id,
        f"⏳ <b>جاري إرسال الإعلان إلى {total} مشترك...</b>"
    )
    status_msg_id = status_res.get("result", {}).get("message_id")

    sent = 0
    failed = 0
    for uid in user_ids:
        res = await bot_api.api_call("copyMessage", {
            "chat_id": uid,
            "from_chat_id": source_chat_id,
            "message_id": source_msg_id
        })
        if res.get("ok"):
            sent += 1
        else:
            failed += 1
            desc = (res.get("description") or "").lower()
            if "blocked" in desc or "deactivated" in desc or "chat not found" in desc or "forbidden" in desc:
                reason = "deactivated" if "deactivated" in desc else "blocked"
                await asyncio.to_thread(db.mark_user_left, uid, None, None, None, reason)
        await asyncio.sleep(0.04)  # Safe ~25 msgs/sec rate limit

    summary = (
        f"✅ <b>اكتمل إرسال الإعلان بنجاح!</b>\n\n"
        f"📬 وصل بنجاح إلى: <b>{sent}</b> مشترك\n"
        f"🚫 تعذر الإرسال (حظروا/غادروا البوت): <b>{failed}</b>"
    )
    if status_msg_id:
        await bot_api.edit_or_send(admin_chat_id, status_msg_id, summary)
    else:
        await bot_api.send_message(admin_chat_id, summary)


def _parse_filter_token(token: str, is_series: bool = False) -> tuple[str, str]:
    """
    Parses combined '{region}-{gcode}' token (e.g. 'en-act', 'as-drm', 'all-all')
    as well as legacy tokens ('Drama', 'asian', 'all').
    """
    if not token:
        return "all", "all"
    if "-" in token:
        reg, gcode = token.split("-", 1)
        return reg or "all", db.normalize_genre_code(gcode)
    if is_series:
        if token == "asian":
            return "as", "all"
        if token in ("ar", "en", "as", "an", "tr", "ad", "all"):
            return token, "all"
        return "all", db.normalize_genre_code(token)
    if token in ("ar", "en", "as", "an", "tr", "ad"):
        return token, "all"
    return "all", db.normalize_genre_code(token)


async def render_region_menu(chat_id: int, message_id: int | None, media_type: str):
    """
    Step 2 Menu: Choose Language / Region (Arabic, English/Foreign, Asian, Anime, Turkish).
    Strictly displays ONLY non-empty options (count > 0).
    """
    reg_info = await asyncio.to_thread(db.get_available_regions, media_type)
    regions = reg_info.get("regions", [])
    total_all = reg_info.get("total", 0)
    type_title = "🎬 قسم الأفلام" if media_type == "m" else "📺 قسم المسلسلات"

    if total_all == 0 or not regions:
        kb = await get_dynamic_main_menu_keyboard()
        text = f"<b>{type_title}</b>\n\nجاري إضافة أعمال جديدة لهذا القسم قريباً..."
        if message_id:
            await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=kb)
        else:
            await bot_api.send_message(chat_id, text, reply_markup=kb)
        return

    rows: list[list[dict]] = []
    # 🦸 Marvel always first: movies → all Marvel films in release order, series → Marvel series in order
    mcat = await asyncio.to_thread(db.get_marvel_catalog)
    if media_type == "m" and mcat["movies_chrono"]:
        rows.append([btn(f"🦸‍♂️ مارفل ({len(mcat['movies_chrono'])} فيلم بالترتيب)", "mv:o:1", style="danger")])
    elif media_type == "s" and mcat["series"]:
        rows.append([btn(f"🦸‍♂️ مارفل ({len(mcat['series'])} مسلسل بالترتيب)", "mv:s:1", style="danger")])

    pair: list[dict] = []
    for r in regions:
        pair.append(btn(r["label"], f"nr:{media_type}:{r['code']}", style="success"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    if len(regions) > 1:
        all_label = "جميع الأفلام 🌐" if media_type == "m" else "جميع المسلسلات 🌐"
        rows.append([btn(all_label, f"nr:{media_type}:all", style="primary")])

    rows.append([btn("🔙 القائمة الرئيسية", "menu")])
    text = f"<b>{type_title}\n\nاختر اللغة أو الفئة التي ترغب بمشاهدتها ⬇️</b>"
    markup = {"inline_keyboard": rows}

    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup)
    else:
        await bot_api.send_message(chat_id, text, reply_markup=markup)


async def render_genre_menu(chat_id: int, message_id: int | None, media_type: str, region: str):
    """
    Step 3 Menu: Choose Genre within the selected (media_type, region).
    Strictly displays ONLY non-empty genres (count > 0).
    If items exist in the region but none have genre tags yet, jumps straight to the items list.
    """
    gen_info = await asyncio.to_thread(db.get_available_genres, media_type, region)
    genres = gen_info.get("genres", [])
    total_in_region = gen_info.get("total", 0)

    if total_in_region == 0:
        await render_region_menu(chat_id, message_id, media_type)
        return

    # Zero-empty safeguard: if items exist but genre tags aren't populated yet, open items directly
    if not genres:
        if media_type == "m":
            await render_filtered_movies(chat_id, message_id, region, "all", 1, direct_from_region=True)
        else:
            await render_filtered_series(chat_id, message_id, region, "all", 1, direct_from_region=True)
        return

    rows: list[list[dict]] = []
    pair: list[dict] = []
    for g in genres:
        pair.append(btn(g["label"], f"ng:{media_type}:{region}:{g['code']}:1", style="success"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    reg_name = db.REGION_SHORT_NAMES.get(region, "")
    type_word = "الأفلام" if media_type == "m" else "المسلسلات"
    rows.append([btn(f"عرض كل {type_word} ({reg_name}) 📋", f"ng:{media_type}:{region}:all:1", style="primary")])
    rows.append([
        btn("🔙 رجوع للغات", f"nt:{media_type}"),
        btn("🏠 الرئيسية", "menu"),
    ])

    icon = "🎬" if media_type == "m" else "📺"
    text = f"<b>{icon} {type_word} — {reg_name}\n\nاختر التصنيف (Genre) المفضل لديك ⬇️</b>"
    markup = {"inline_keyboard": rows}

    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup)
    else:
        await bot_api.send_message(chat_id, text, reply_markup=markup)


async def render_filtered_movies(
    chat_id: int,
    message_id: int | None,
    region: str,
    gcode: str,
    page: int,
    direct_from_region: bool = False
):
    gcode = db.normalize_genre_code(gcode)
    reg_name = db.REGION_SHORT_NAMES.get(region, "الكل")
    gen_name = db.GENRE_CODE_TO_LABEL.get(gcode, gcode)
    header_text = (
        f"🎬 الأفلام ({reg_name}) • 🔥 الأكثر شهرة"
        if gcode == "all"
        else f"🎬 الأفلام ({reg_name}) — {gen_name} • 🔥 الأكثر شهرة"
    )

    res = await asyncio.to_thread(db.get_filtered_movies, region, gcode, page, 16)
    items = res["items"]
    total_pages = res["total_pages"]

    if not items:
        back_cb = f"nt:m" if direct_from_region else f"nr:m:{region}"
        markup = {"inline_keyboard": [[btn("🔙 رجوع", back_cb), btn("🏠 الرئيسية", "menu")]]}
        if gcode == "col":
            markup["inline_keyboard"].insert(0, [btn("🦸‍♂️ مارفل — عالم مارفل الكامل", "mv:h", style="danger")])
        text = f"{header_text}\n\nلا توجد أفلام متوفرة في هذا التصنيف حالياً."
        if message_id:
            await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup, parse_mode=None)
        else:
            await bot_api.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
        return

    token = f"{region}-{gcode}"
    btn_pairs = [
        (db.clean_display_title(m["title"]), f"m_play:{m['id']}:{token}:{page}")
        for m in items
    ]
    rows = build_smart_title_rows(btn_pairs)
    if gcode == "col":
        mcat = await asyncio.to_thread(db.get_marvel_catalog)
        if mcat["movies_chrono"] or mcat["series"]:
            rows.insert(0, [btn("🦸‍♂️ مارفل — عالم مارفل الكامل", "mv:h", style="danger")])

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(btn("⬅️ السابق", f"ng:m:{region}:{gcode}:{page-1}"))
        nav.append(btn(f"{page}/{total_pages}", "noop"))
        if page < total_pages:
            nav.append(btn("التالي ➡️", f"ng:m:{region}:{gcode}:{page+1}"))
        rows.append(nav)

    back_cb = "nt:m" if direct_from_region else f"nr:m:{region}"
    rows.append([
        btn("🔙 رجوع للتصنيفات", back_cb),
        btn("🏠 الرئيسية", "menu"),
    ])
    markup = {"inline_keyboard": rows}

    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, header_text, reply_markup=markup, parse_mode=None)
    else:
        await bot_api.send_message(chat_id, header_text, reply_markup=markup, parse_mode=None)


async def render_filtered_series(
    chat_id: int,
    message_id: int | None,
    region: str,
    gcode: str,
    page: int,
    direct_from_region: bool = False
):
    gcode = db.normalize_genre_code(gcode)
    reg_name = db.REGION_SHORT_NAMES.get(region, "الكل")
    gen_name = db.GENRE_CODE_TO_LABEL.get(gcode, gcode)
    header_text = (
        f"📺 المسلسلات ({reg_name}) • 🔥 الأكثر شهرة"
        if gcode == "all"
        else f"📺 المسلسلات ({reg_name}) — {gen_name} • 🔥 الأكثر شهرة"
    )

    res = await asyncio.to_thread(db.get_filtered_series, region, gcode, page, 16)
    items = res["items"]
    total_pages = res["total_pages"]

    if not items:
        back_cb = "nt:s" if direct_from_region else f"nr:s:{region}"
        markup = {"inline_keyboard": [[btn("🔙 رجوع", back_cb), btn("🏠 الرئيسية", "menu")]]}
        text = f"{header_text}\n\nلا توجد مسلسلات متوفرة في هذا التصنيف حالياً."
        if message_id:
            await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup, parse_mode=None)
        else:
            await bot_api.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
        return

    token = f"{region}-{gcode}"
    btn_pairs = [
        (db.clean_display_title(s["title"]), f"s_det:{s['id']}:{token}:{page}")
        for s in items
    ]
    rows = build_smart_title_rows(btn_pairs)

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(btn("⬅️ السابق", f"ng:s:{region}:{gcode}:{page-1}"))
        nav.append(btn(f"{page}/{total_pages}", "noop"))
        if page < total_pages:
            nav.append(btn("التالي ➡️", f"ng:s:{region}:{gcode}:{page+1}"))
        rows.append(nav)

    back_cb = "nt:s" if direct_from_region else f"nr:s:{region}"
    rows.append([
        btn("🔙 رجوع للتصنيفات", back_cb),
        btn("🏠 الرئيسية", "menu"),
    ])
    markup = {"inline_keyboard": rows}

    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, header_text, reply_markup=markup, parse_mode=None)
    else:
        await bot_api.send_message(chat_id, header_text, reply_markup=markup, parse_mode=None)


async def render_adult_gate(chat_id: int, message_id: int | None):
    """
    Explicit +18 confirmation gate so no user accidentally enters the 'للكبار فقط' section.
    """
    text = (
        "🔞 <b>تنبيه هام — قسم للكبار فقط (+18)</b>\n\n"
        "⚠️ هذا القسم معزول بالكامل ومخصص <b>للبالغين فقط (+18)</b> لاحتوائه على أفلام وأعمال جريئة "
        "غير مناسبة للمشاهدة العائلية أو لمن هم دون السن القانوني.\n\n"
        "هل تؤكد أنك بالغ وتود المتابعة لدخول القسم؟ ⬇️"
    )
    markup = {
        "inline_keyboard": [
            [btn("🔞 نعم، عمري +18 — دخول القسم", "adult_list:1", style="danger")],
            [btn("🔙 رجوع للقائمة الرئيسية", "menu", style="success")],
        ]
    }
    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup)
    else:
        await bot_api.send_message(chat_id, text, reply_markup=markup)


_adult18_stars_invoice_link: str | None = None


async def get_or_create_adult18_invoice_link() -> str:
    """
    Creates (and caches) a universal Telegram Stars invoice link (https://t.me/$...).
    Works on 100% of Telegram clients: Desktop, Web, Android, iOS.
    """
    global _adult18_stars_invoice_link
    if _adult18_stars_invoice_link:
        return _adult18_stars_invoice_link
    payload = {
        "title": "🔞 اشتراك قسم +18 (30 يوم)",
        "description": f"وصول كامل لقسم الأفلام الجريئة (+18) لمدة {ADULT18_ACCESS_DAYS} يوماً.",
        "payload": "adult18_access_30d",
        "currency": "XTR",
        "prices": [{"label": f"وصول {ADULT18_ACCESS_DAYS} يوم", "amount": ADULT18_STARS_PRICE}],
        "provider_token": "",
    }
    res = await bot_api.api_call("createInvoiceLink", payload)
    if res.get("ok") and res.get("result"):
        _adult18_stars_invoice_link = res.get("result")
        return _adult18_stars_invoice_link
    return "https://t.me"


async def send_adult18_stars_invoice(chat_id: int) -> bool:
    """
    Sends a universal Telegram Stars invoice message.
    Works on Mobile, Telegram Desktop, and Telegram Web.
    """
    inv_link = await get_or_create_adult18_invoice_link()
    markup = {
        "inline_keyboard": [
            [btn(f"⭐ دفع {ADULT18_STARS_PRICE} نجمة (30 يوم) 🔞", url=inv_link, style="danger")],
            [btn("🔙 رجوع للقائمة الرئيسية", "menu", style="success")],
        ]
    }
    text = (
        "🔞 <b>فاتورة اشتراك قسم +18 (30 يوماً)</b>\n\n"
        f"💫 <b>القيمة:</b> {ADULT18_STARS_PRICE} نجمة تيليجرام\n"
        f"📅 <b>المدة:</b> {ADULT18_ACCESS_DAYS} يوماً كاملاً\n\n"
        "اضغط على زر الدفع أدناه للمتابعة 👇"
    )
    res = await bot_api.send_message(chat_id, text, reply_markup=markup)
    return bool(res.get("ok"))


async def render_adult18_stars_gate(chat_id: int, message_id: int | None, user_id: int):
    """
    New Stars-gated +18 entry point:
    - If user already has active Stars access → go straight to the content list
    - If not → show the payment gate with a universal Stars invoice button (Mobile + Desktop + Web)
    """
    has_access = await asyncio.to_thread(db.check_adult18_access, user_id)

    if has_access:
        # Already paid — show the +18 movies list directly
        await render_adult18_movies_section(chat_id, message_id, page=1)
        return

    inv_link = await get_or_create_adult18_invoice_link()
    text = (
        "🔞 <b>قسم للكبار فقط (+18) — محتوى حصري</b>\n\n"
        "⚠️ هذا القسم يحتوي على <b>أفلام جريئة وحصرية للبالغين (+18)</b> "
        "مرفوعة يدوياً وغير متاحة في القسم العادي.\n\n"
        f"💫 <b>للوصول الكامل:</b> {ADULT18_STARS_PRICE} نجمة تيليجرام ({ADULT18_ACCESS_DAYS} يوماً)\n\n"
        "اضغط الزر أدناه لإتمام الدفع مباشرة من أي جهاز 👇"
    )
    markup = {
        "inline_keyboard": [
            [btn(f"⭐ ادفع {ADULT18_STARS_PRICE} نجمة — دخول {ADULT18_ACCESS_DAYS} يوم 🔞", url=inv_link, style="danger")],
            [btn("🔙 رجوع للقائمة الرئيسية", "menu", style="success")],
        ]
    }
    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup)
    else:
        await bot_api.send_message(chat_id, text, reply_markup=markup)


async def render_adult18_movies_section(chat_id: int, message_id: int | None, page: int = 1):
    """
    Renders the manually-uploaded +18 movies list (from adult18_movies table).
    Only accessible after paying Stars.
    """
    res = await asyncio.to_thread(db.get_adult18_movies, page, 16)
    items = res["items"]
    total_pages = res["total_pages"]
    header_text = "🔞 قسم الأفلام الحصرية (+18) • محتوى للبالغين فقط"

    if not items:
        markup = {"inline_keyboard": [[btn("🏠 القائمة الرئيسية", "menu")]]}
        text = f"{header_text}\n\nلا توجد أفلام في هذا القسم حالياً، ترقب الإضافات قريباً! 🎬"
        if message_id:
            await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup, parse_mode=None)
        else:
            await bot_api.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
        return

    btn_pairs = []
    for it in items:
        clean_t = db.clean_display_title(it["title"])
        yr = f" ({it['year']})" if it.get("year") else ""
        btn_pairs.append((f"🔞 {clean_t}{yr}", f"a18_play:{it['id']}:{page}"))

    rows = build_smart_title_rows(btn_pairs)
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(btn("⬅️ السابق", f"a18_list:{page-1}"))
        nav.append(btn(f"{page}/{total_pages}", "noop"))
        if page < total_pages:
            nav.append(btn("التالي ➡️", f"a18_list:{page+1}"))
        rows.append(nav)

    rows.append([btn("🏠 القائمة الرئيسية", "menu", style="success")])
    markup = {"inline_keyboard": rows}
    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, header_text, reply_markup=markup, parse_mode=None)
    else:
        await bot_api.send_message(chat_id, header_text, reply_markup=markup, parse_mode=None)


async def render_adult18_movie_play(chat_id: int, movie_id: int, list_page: int, user_id: int):
    """
    Delivers a +18 movie video directly to the user (copyMessage from Master Vault).
    Only callable after Stars access is verified.
    """
    res = await asyncio.to_thread(db.get_adult18_movies, 1, 9999)  # fetch all to find by id
    movie = next((m for m in res["items"] if m["id"] == movie_id), None)
    if not movie:
        await bot_api.send_message(chat_id, "⚠️ لم يتم العثور على هذا الفيلم.")
        return

    title = db.clean_display_title(movie["title"])
    yr = f" ({movie['year']})" if movie.get("year") else ""
    desc = (movie.get("description") or "مشاهدة ممتعة 🎬").strip()

    # Send poster if available
    if movie.get("poster_file_id"):
        caption = f"🔞 <b>{html.escape(title)}{yr}</b>\n\n{html.escape(desc)}"
        await bot_api.api_call("sendPhoto", {
            "chat_id": chat_id,
            "photo": movie["poster_file_id"],
            "caption": caption,
            "parse_mode": "HTML",
        })

    # Deliver the video
    back_markup = {
        "inline_keyboard": [
            [btn("🔙 رجوع لقائمة +18", f"a18_list:{list_page}"), btn("🏠 القائمة الرئيسية", "menu")]
        ]
    }
    copy_res = await bot_api.api_call("copyMessage", {
        "chat_id": chat_id,
        "from_chat_id": int(movie["vault_channel_id"]),
        "message_id": int(movie["vault_message_id"]),
        "caption": f"🔞 <b>{html.escape(title)}{yr}</b>",
        "parse_mode": "HTML",
        "reply_markup": back_markup,
    })
    if not copy_res.get("ok"):
        await bot_api.send_message(
            chat_id,
            f"⚠️ تعذر تشغيل الفيلم حالياً. يُرجى المحاولة لاحقاً.\n\n{copy_res.get('description', '')}",
            reply_markup=back_markup
        )


async def render_adult_section(chat_id: int, message_id: int | None, page: int = 1):
    """
    Renders the isolated 'للكبار فقط 🔞' (+18) section list.
    """
    res = await asyncio.to_thread(db.get_adult_vault_items, page, 16)
    items = res["items"]
    total_pages = res["total_pages"]
    header_text = "🔞 قسم للكبار فقط (+18) • مخصص للبالغين فقط"

    if not items:
        markup = {"inline_keyboard": [[btn("🏠 القائمة الرئيسية", "menu")]]}
        text = f"{header_text}\n\nلا توجد أعمال في هذا القسم حالياً."
        if message_id:
            await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup, parse_mode=None)
        else:
            await bot_api.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
        return

    btn_pairs = []
    for it in items:
        clean_t = db.clean_display_title(it["title"])
        yr = f" ({it['year']})" if it.get("year") else ""
        if it.get("media_type") == "series":
            btn_pairs.append((f"🔞 {clean_t}{yr}", f"s_det:{it['id']}:ad-all:{page}"))
        else:
            btn_pairs.append((f"🔞 {clean_t}{yr}", f"m_play:{it['id']}:ad-all:{page}"))

    rows = build_smart_title_rows(btn_pairs)
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(btn("⬅️ السابق", f"adult_list:{page-1}"))
        nav.append(btn(f"{page}/{total_pages}", "noop"))
        if page < total_pages:
            nav.append(btn("التالي ➡️", f"adult_list:{page+1}"))
        rows.append(nav)

    rows.append([btn("🏠 القائمة الرئيسية", "menu", style="success")])
    markup = {"inline_keyboard": rows}
    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, header_text, reply_markup=markup, parse_mode=None)
    else:
        await bot_api.send_message(chat_id, header_text, reply_markup=markup, parse_mode=None)



# ───────────────────────── 🦸 عالم مارفل (Marvel Universe) ─────────────────────────
MARVEL_PAGE_SIZE = 16


async def _send_or_edit(chat_id: int, message_id: int | None, text: str, rows: list[list[dict]]):
    markup = {"inline_keyboard": rows}
    if message_id:
        await bot_api.edit_or_send(chat_id, message_id, text, reply_markup=markup)
    else:
        await bot_api.send_message(chat_id, text, reply_markup=markup)


def _marvel_item_label(n: int, title: str, year) -> str:
    clean = db.clean_display_title(title)
    yr = f" ({year})" if year and str(year) not in clean else ""
    return f"{n}. {clean}{yr}"


def _paged_nav(page: int, total_pages: int, cb_prefix: str) -> list[dict]:
    nav = []
    if page > 1:
        nav.append(btn("⬅️ السابق", f"{cb_prefix}:{page - 1}"))
    nav.append(btn(f"{page}/{total_pages}", "noop"))
    if page < total_pages:
        nav.append(btn("التالي ➡️", f"{cb_prefix}:{page + 1}"))
    return nav


async def render_marvel_hub(chat_id: int, message_id: int | None):
    cat = await asyncio.to_thread(db.get_marvel_catalog)
    n_movies = len(cat["movies_chrono"])
    n_fr = len(cat["franchises"])
    n_series = len(cat["series"])
    rows: list[list[dict]] = []
    if n_fr:
        rows.append([btn(f"🎬 سلاسل أفلام مارفل ({n_fr})", "mv:f", style="success")])
        rows.append([btn(f"📅 كل أفلام مارفل بترتيب العرض ({n_movies})", "mv:o:1", style="primary")])
    if n_series:
        rows.append([btn(f"📺 مسلسلات مارفل مرتبة ({n_series})", "mv:s:1", style="success")])
    rows.append([
        btn("🔙 رجوع لسلاسل الأفلام", "ng:m:all:col:1"),
        btn("🏠 الرئيسية", "menu"),
    ])
    if not (n_movies or n_series):
        text = "<b>🦸‍♂️ عالم مارفل</b>\n\nجاري رفع أعمال مارفل قريباً..."
    else:
        text = (
            "<b>🦸‍♂️ عالم مارفل الكامل</b>\n\n"
            f"🎬 أفلام: <b>{n_movies}</b> فيلم في <b>{n_fr}</b> سلسلة\n"
            f"📺 مسلسلات: <b>{n_series}</b> مسلسل\n\n"
            "<b>اختر طريقة التصفح ⬇️</b>"
        )
    await _send_or_edit(chat_id, message_id, text, rows)


async def render_marvel_franchises(chat_id: int, message_id: int | None):
    cat = await asyncio.to_thread(db.get_marvel_catalog)
    rows: list[list[dict]] = []
    pair: list[dict] = []
    for fr in cat["franchises"]:
        pair.append(btn(f"{fr['label']} ({len(fr['movies'])})", f"mv:fr:{fr['key']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("🔙 رجوع لأفلام مارفل", "mv:o:1"), btn("🏠 الرئيسية", "menu")])
    text = "<b>🎬 سلاسل أفلام مارفل</b>\n\nكل سلسلة مرتبة من الجزء الأول للأخير ⬇️"
    if not cat["franchises"]:
        text = "<b>🎬 سلاسل أفلام مارفل</b>\n\nلا توجد أفلام مارفل متوفرة حالياً."
    await _send_or_edit(chat_id, message_id, text, rows)


async def render_marvel_franchise(chat_id: int, message_id: int | None, key: str):
    cat = await asyncio.to_thread(db.get_marvel_catalog)
    fr = next((f for f in cat["franchises"] if f["key"] == key), None)
    if not fr:
        await render_marvel_franchises(chat_id, message_id)
        return
    rows = [
        [btn(_marvel_item_label(i, m["title"], m.get("year")), f"m_play:{m['id']}:mv-{key}:1")]
        for i, m in enumerate(fr["movies"], 1)
    ]
    rows.append([btn("🔙 رجوع للسلاسل", "mv:f"), btn("🦸‍♂️ كل أفلام مارفل", "mv:o:1")])
    text = f"<b>{html.escape(fr['label'])}</b>\n\nالأجزاء مرتبة حسب تاريخ العرض ⬇️"
    await _send_or_edit(chat_id, message_id, text, rows)


async def render_marvel_chrono(chat_id: int, message_id: int | None, page: int):
    cat = await asyncio.to_thread(db.get_marvel_catalog)
    movies = cat["movies_chrono"]
    total_pages = max(1, (len(movies) + MARVEL_PAGE_SIZE - 1) // MARVEL_PAGE_SIZE)
    page = min(max(1, page), total_pages)
    start = (page - 1) * MARVEL_PAGE_SIZE
    rows: list[list[dict]] = []
    if cat["franchises"]:
        rows.append([btn(f"🎬 تصفح حسب السلسلة ({len(cat['franchises'])})", "mv:f", style="success")])
    rows += [
        [btn(_marvel_item_label(start + i, m["title"], m.get("year")), f"m_play:{m['id']}:mv-o:{page}")]
        for i, m in enumerate(movies[start:start + MARVEL_PAGE_SIZE], 1)
    ]
    if total_pages > 1:
        rows.append(_paged_nav(page, total_pages, "mv:o"))
    rows.append([btn("🔙 رجوع لقسم الأفلام", "nt:m"), btn("🏠 الرئيسية", "menu")])
    text = (
        f"<b>🦸‍♂️ مارفل — كل الأفلام بالترتيب ({len(movies)})</b>\n\n"
        "مرتبة حسب تاريخ العرض من الأقدم للأحدث ⬇️"
    )
    if not movies:
        text = "<b>🦸‍♂️ مارفل</b>\n\nجاري رفع أفلام مارفل قريباً..."
    await _send_or_edit(chat_id, message_id, text, rows)


async def render_marvel_series(chat_id: int, message_id: int | None, page: int):
    cat = await asyncio.to_thread(db.get_marvel_catalog)
    series = cat["series"]
    total_pages = max(1, (len(series) + MARVEL_PAGE_SIZE - 1) // MARVEL_PAGE_SIZE)
    page = min(max(1, page), total_pages)
    start = (page - 1) * MARVEL_PAGE_SIZE
    rows = [
        [btn(_marvel_item_label(start + i, s["title"], s.get("year")), f"s_det:{s['id']}:mvs:{page}")]
        for i, s in enumerate(series[start:start + MARVEL_PAGE_SIZE], 1)
    ]
    if total_pages > 1:
        rows.append(_paged_nav(page, total_pages, "mv:s"))
    rows.append([btn("🔙 رجوع لقسم المسلسلات", "nt:s"), btn("🏠 الرئيسية", "menu")])
    text = (
        f"<b>🦸‍♂️ مارفل — كل المسلسلات بالترتيب ({len(series)})</b>\n\n"
        "مرتبة حسب ترتيب العرض من الأقدم للأحدث ⬇️"
    )
    if not series:
        text = "<b>📺 مسلسلات مارفل</b>\n\nلا توجد مسلسلات مارفل متوفرة حالياً."
    await _send_or_edit(chat_id, message_id, text, rows)


async def render_category_movies(chat_id: int, message_id: int | None, genre: str, page: int):
    if genre.startswith("mv-"):
        mv_key = genre[3:]
        if mv_key == "o":
            await render_marvel_chrono(chat_id, message_id, page)
        else:
            await render_marvel_franchise(chat_id, message_id, mv_key)
        return
    reg, gcode = _parse_filter_token(genre, is_series=False)
    if reg == "ad" or genre in ("adult", "ad", "ad-all"):
        await render_adult_section(chat_id, message_id, page)
        return
    await render_filtered_movies(chat_id, message_id, reg, gcode, page)


async def render_series_category(chat_id: int, message_id: int | None, s_cat: str, page: int):
    if s_cat == "mvs":
        await render_marvel_series(chat_id, message_id, page)
        return
    reg, gcode = _parse_filter_token(s_cat, is_series=True)
    if reg == "ad" or s_cat in ("adult", "ad", "ad-all"):
        await render_adult_section(chat_id, message_id, page)
        return
    gen_info = await asyncio.to_thread(db.get_available_genres, "s", reg)
    direct = len(gen_info.get("genres", [])) == 0
    await render_filtered_series(chat_id, message_id, reg, gcode, page, direct_from_region=direct)


async def handle_message(msg: dict):
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return

    chat_id = chat["id"]
    message_id = msg.get("message_id")
    text = (msg.get("text") or "").strip()
    from_user = msg.get("from", {})
    user_id = from_user.get("id", chat_id)
    username = from_user.get("username")
    first_name = from_user.get("first_name") or "صديقي"

    deep_arg = None
    if text.startswith("/start ") and len(text.split(maxsplit=1)) > 1:
        deep_arg = text.split(maxsplit=1)[1].strip()
    parsed_start = db.parse_start_payload(deep_arg) if deep_arg else {}
    if deep_arg:
        parsed_start["raw_arg"] = deep_arg
    start_entry_info = parsed_start if text.startswith("/start") else None

    # Automatically save/update user in PostgreSQL database & detect new joins/rejoins.
    # Await /start synchronously so referral attribution & entry source commit BEFORE Force-Subscribe writes a skeleton row!
    initial_act = "🚀 فتح البوت (/start)" if text.startswith("/start") else None
    if text.startswith("/start"):
        await track_bot_user_bg(from_user, action_text=initial_act, referral_info=start_entry_info)
    else:
        asyncio.create_task(track_bot_user_bg(from_user, action_text=initial_act))

    # If Admin is currently sending a Broadcast Ad message (supports text, photo, video, etc.)
    if user_id in _awaiting_broadcast and "chat_shared" not in msg:
        if text in ("/cancel", "🔒 إغلاق لوحة التحكم") or text.startswith("/admin"):
            _awaiting_broadcast.discard(user_id)
        else:
            _awaiting_broadcast.discard(user_id)
            asyncio.create_task(run_broadcast(chat_id, chat_id, message_id))
            return

    # If Admin is currently pasting 1 or multiple @BotFather uploader worker tokens
    if user_id in _awaiting_worker_tokens and "chat_shared" not in msg:
        raw_tok_body = (msg.get("text") or msg.get("caption") or "").strip()
        if raw_tok_body in ("/cancel", "🔒 إغلاق لوحة التحكم") or raw_tok_body.startswith("/admin"):
            _awaiting_worker_tokens.discard(user_id)
        else:
            _awaiting_worker_tokens.discard(user_id)
            wait_msg = await bot_api.send_message(chat_id, "⏳ <b>جاري فحص التوكنات والتحقق من صلاحية النشر في قناة الأرشيف...</b>", reply_to_message_id=message_id)
            saved, errs = await ingest_worker_tokens_from_text(raw_tok_body)
            summary_lines = []
            for r in saved:
                un = f"@{html.escape(r['bot_username'])}" if r.get("bot_username") else html.escape(r.get("bot_name") or "Bot")
                if r.get("channel_ok"):
                    summary_lines.append(f"✅ تم تفعيل <b>{un}</b> فوراً (أدمن بالقناة ويشارك في الرفع الآن 🚀)")
                else:
                    summary_lines.append(
                        f"🟡 تم حفظ <b>{un}</b> — يتبقى فقط رفعه <b>مشرف (Admin) بصلاحية النشر</b> في القناة <code>{html.escape(str(TELEGRAM_CHANNEL_ID))}</code> وسيبدأ الرفع تلقائياً!"
                    )
            summary_lines.extend(errs)
            w_text, w_markup = await build_admin_workers_view()
            banner = "\n".join(summary_lines)
            full_reply = f"{banner}\n\n{'─' * 22}\n\n{w_text}" if banner else w_text
            if wait_msg:
                await bot_api.edit_or_send(chat_id, wait_msg, full_reply, reply_markup=w_markup)
            else:
                await bot_api.send_message(chat_id, full_reply, reply_markup=w_markup, reply_to_message_id=message_id)
            return

    # ── 🔞 Admin +18 Upload Flow ─────────────────────────────────────────────
    is_adm = (user_id in _unlocked_admins) or (user_id in ADMIN_IDS)

    # Step 3: Admin sent the year (or skipped)
    if user_id in _awaiting_adult18_year:
        pending = _awaiting_adult18_year.pop(user_id)
        year_input = text.strip()
        year_val = None
        if year_input.isdigit() and 1900 <= int(year_input) <= 2100:
            year_val = int(year_input)
        movie_db_id = await asyncio.to_thread(
            db.add_adult18_movie,
            pending["title"],
            pending["vault_message_id"],
            pending["vault_channel_id"],
            year_val,
            pending.get("description"),
            pending.get("poster_file_id"),
            user_id,
        )
        yr_disp = f" ({year_val})" if year_val else ""
        confirm_markup = {
            "inline_keyboard": [[
                btn("🗑️ حذف", f"a18_del:{movie_db_id}"),
                btn("🔞 عرض قائمة +18", "a18_list:1"),
            ]]
        }
        await bot_api.send_message(
            chat_id,
            f"✅ <b>تم حفظ الفيلم في قسم +18 بنجاح!</b>\n\n"
            f"🎬 <b>{html.escape(pending['title'])}{html.escape(yr_disp)}</b>\n"
            f"🆔 معرّف DB: <code>{movie_db_id}</code>",
            reply_markup=confirm_markup,
        )
        return

    # Step 2: Admin sent the title
    if user_id in _awaiting_adult18_title:
        pending = _awaiting_adult18_title.pop(user_id)
        if not text or text.startswith("/"):
            await bot_api.send_message(chat_id, "❌ تم إلغاء رفع فيلم +18.")
            return
        pending["title"] = text.strip()
        _awaiting_adult18_year[user_id] = pending
        await bot_api.send_message(
            chat_id,
            f"✅ العنوان: <b>{html.escape(text.strip())}</b>\n\n"
            "الآن أرسل <b>سنة الإنتاج</b> (مثال: 2024)\n"
            "أو أرسل أي نص آخر للتخطي.",
        )
        return

    # Step 1: Admin sent a video — forward to Master Vault and start +18 flow
    if is_adm and ("video" in msg or "document" in msg):
        vid = msg.get("video") or msg.get("document")
        mime = (vid.get("mime_type") or "").lower() if vid else ""
        if "video" in mime or msg.get("video"):
            fwd_res = await bot_api.api_call("copyMessage", {
                "chat_id": int(TELEGRAM_CHANNEL_ID),
                "from_chat_id": chat_id,
                "message_id": message_id,
            })
            if not fwd_res.get("ok"):
                await bot_api.send_message(
                    chat_id,
                    f"⚠️ فشل نسخ الفيديو إلى Master Vault: {fwd_res.get('description', '')}\n\n"
                    "تأكد أن البوت أدمن في قناة الأرشيف.",
                )
                return
            vault_msg_id = fwd_res["result"]["message_id"]
            _awaiting_adult18_title[user_id] = {
                "vault_message_id": vault_msg_id,
                "vault_channel_id": str(TELEGRAM_CHANNEL_ID),
                "poster_file_id": None,
                "description": (msg.get("caption") or "").strip() or None,
            }
            await bot_api.send_message(
                chat_id,
                f"📹 <b>تم استلام الفيديو ونسخه للـ Vault</b>\n"
                f"🆔 Vault Message ID: <code>{vault_msg_id}</code>\n\n"
                "الآن أرسل <b>عنوان الفيلم</b> (مثال: Fifty Shades of Grey)\n"
                "أو أرسل /cancel للإلغاء.",
            )
            return

    # Handle Native Group/Channel Selection (chat_shared event)
    if "chat_shared" in msg and user_id in _unlocked_admins:
        shared = msg["chat_shared"]
        shared_chat_id = str(shared["chat_id"])
        title = shared.get("title") or ""
        username = shared.get("username") or ""
        invite_link = f"https://t.me/{username}" if username else ""

        # Fetch full chat details or generate invite link for private groups/channels
        chat_info_res = await bot_api.api_call("getChat", {"chat_id": shared_chat_id})
        if chat_info_res.get("ok"):
            c_obj = chat_info_res["result"]
            if not title:
                title = c_obj.get("title") or c_obj.get("username") or shared_chat_id
            if not invite_link:
                if c_obj.get("username"):
                    invite_link = f"https://t.me/{c_obj['username']}"
                elif c_obj.get("invite_link"):
                    invite_link = c_obj["invite_link"]

        if not invite_link:
            exp_res = await bot_api.api_call("exportChatInviteLink", {"chat_id": shared_chat_id})
            if exp_res.get("ok"):
                invite_link = exp_res.get("result", "")

        if not title:
            title = f"Chat {shared_chat_id}"
        if not invite_link:
            invite_link = "https://t.me/"

        await asyncio.to_thread(db.add_required_channel, shared_chat_id, title, invite_link)
        _verified_users_cache.clear()
        adm_text, adm_markup = await build_admin_panel()
        await bot_api.send_message(
            chat_id,
            f"✅ <b>تم ربط وإضافة «{html.escape(title)}» للاشتراك الإجباري تلقائياً!</b>\n\n{adm_text}",
            reply_markup=adm_markup
        )
        return

    if not text:
        return

    if text == "🔒 إغلاق لوحة التحكم" and user_id in _unlocked_admins:
        _unlocked_admins.discard(user_id)
        await bot_api.send_message(
            chat_id,
            "🔒 تم إغلاق لوحة التحكم بنجاح.",
            reply_markup={"remove_keyboard": True}
        )
        kb = await get_dynamic_main_menu_keyboard()
        await bot_api.send_message(chat_id, build_welcome_text(first_name), reply_markup=kb)
        return

    # Admin Commands protected by Secret Password / API Key
    if text.startswith("/admin"):
        parts = text.split(maxsplit=1)
        provided_pass = parts[1].strip() if len(parts) > 1 else ""
        if BOT_ADMIN_PASSWORD and provided_pass == BOT_ADMIN_PASSWORD:
            _unlocked_admins.add(user_id)
            ADMIN_IDS.add(user_id)
            await bot_api.delete_message(chat_id, message_id)
            await bot_api.send_message(
                chat_id,
                "🔓 <b>تم فتح وضع الأدمن بنجاح!</b>\nاستخدم الأزرار بالأسفل لاختيار أي جروب أو قناة من قائمتك بضغطة واحدة ⬇️",
                reply_markup=build_admin_reply_keyboard()
            )
            adm_text, adm_markup = await build_admin_panel()
            await bot_api.send_message(chat_id, adm_text, reply_markup=adm_markup)
            return
        elif user_id in _unlocked_admins:
            await bot_api.send_message(
                chat_id,
                "👇 اختر جروب أو قناة من الأزرار بالأسفل:",
                reply_markup=build_admin_reply_keyboard()
            )
            adm_text, adm_markup = await build_admin_panel()
            await bot_api.send_message(chat_id, adm_text, reply_markup=adm_markup, reply_to_message_id=message_id)
            return
        else:
            await bot_api.send_message(
                chat_id,
                "🔒 <b>هذه اللوحة محمية بكلمة سر:</b>\nأرسل الأمر متبوعاً بكلمة السر السرية:\n<code>/admin PASSWORD</code>",
                reply_to_message_id=message_id
            )
            return

    if user_id in _unlocked_admins:
        if text.startswith("/addchat"):
            parts = text.split(maxsplit=3)
            if len(parts) < 2:
                await bot_api.send_message(
                    chat_id,
                    "⚠️ الصيغة الصحيحة:\n<code>/addchat @username اسم الجروب</code>\nأو للجروب الخاص:\n<code>/addchat -100xxx https://t.me/+xxx اسم الجروب</code>",
                    reply_to_message_id=message_id
                )
                return

            target_chat = parts[1].strip()
            invite_link = ""
            title = ""

            if len(parts) >= 3 and (parts[2].startswith("http://") or parts[2].startswith("https://") or parts[2].startswith("t.me/")):
                invite_link = parts[2].strip()
                if not invite_link.startswith("http"):
                    invite_link = "https://" + invite_link
                title = parts[3].strip() if len(parts) >= 4 else target_chat
            else:
                title = " ".join(parts[2:]).strip() if len(parts) >= 3 else ""

            # Fetch chat info from Telegram if title or invite_link is missing
            chat_info_res = await bot_api.api_call("getChat", {"chat_id": target_chat})
            if chat_info_res.get("ok"):
                c_obj = chat_info_res["result"]
                if not title:
                    title = c_obj.get("title") or c_obj.get("username") or target_chat
                if not invite_link:
                    if c_obj.get("username"):
                        invite_link = f"https://t.me/{c_obj['username']}"
                    elif c_obj.get("invite_link"):
                        invite_link = c_obj["invite_link"]

            if not title:
                title = target_chat.lstrip("@")
            if not invite_link:
                if target_chat.startswith("@"):
                    invite_link = f"https://t.me/{target_chat.lstrip('@')}"
                else:
                    await bot_api.send_message(
                        chat_id,
                        "⚠️ الجروب خاص، يرجى وضع رابط الدعوة بعد الـ ID:\n<code>/addchat -100xxx https://t.me/+xxx اسم الجروب</code>",
                        reply_to_message_id=message_id
                    )
                    return

            await asyncio.to_thread(db.add_required_channel, target_chat, title, invite_link)
            _verified_users_cache.clear()
            adm_text, adm_markup = await build_admin_panel()
            await bot_api.send_message(
                chat_id,
                f"✅ تم إضافة <b>{html.escape(title)}</b> لقائمة الاشتراك الإجباري بنجاح!\n\n{adm_text}",
                reply_markup=adm_markup,
                reply_to_message_id=message_id
            )
            return

        if text.startswith("/delchat"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await bot_api.send_message(chat_id, "⚠️ أرسل: <code>/delchat @username</code>", reply_to_message_id=message_id)
                return
            await asyncio.to_thread(db.remove_required_channel, parts[1].strip())
            _verified_users_cache.clear()
            adm_text, adm_markup = await build_admin_panel()
            await bot_api.send_message(chat_id, f"✅ تم الحذف بنجاح!\n\n{adm_text}", reply_markup=adm_markup, reply_to_message_id=message_id)
            return

        if text.startswith("/workers"):
            w_text, w_markup = await build_admin_workers_view()
            await bot_api.send_message(chat_id, w_text, reply_markup=w_markup, reply_to_message_id=message_id)
            return

        if text.startswith("/addpoints"):
            parts = text.split(maxsplit=3)
            if len(parts) < 3 or not parts[1].lstrip("-").isdigit() or not parts[2].lstrip("+-").isdigit():
                await bot_api.send_message(
                    chat_id,
                    "⚠️ الصيغة الصحيحة لتعديل النقاط:\n<code>/addpoints USER_ID +5 مكافأة ترويج</code>\nأو لخصم نقاط:\n<code>/addpoints USER_ID -2 خصم إداري</code>",
                    reply_to_message_id=message_id,
                )
                return
            t_uid = int(parts[1])
            p_delta = int(parts[2])
            p_reason = parts[3].strip() if len(parts) >= 4 else "تعديل إداري من البوت"
            adj = await asyncio.to_thread(db.adjust_user_points, t_uid, p_delta, p_reason)
            if not adj:
                await bot_api.send_message(chat_id, "⚠️ لم يتم العثور على هذا المستخدم في قاعدة البيانات.", reply_to_message_id=message_id)
                return
            if p_delta != 0:
                sign_s = f"+{p_delta}" if p_delta > 0 else str(p_delta)
                try:
                    await bot_api.send_message(
                        t_uid,
                        f"🎁 <b>تحديث في رصيد نقاطك!</b>\n\nتم إضافة/تعديل: <code>{sign_s}</code> نقطة\n📝 السبب: {html.escape(p_reason)}\n💰 رصيدك الحالي: <code>{adj['new_points']}</code> نقطة 🏆",
                    )
                except Exception:
                    pass
            await bot_api.send_message(
                chat_id,
                f"✅ تم تعديل نقاط <b>{html.escape(adj.get('first_name') or str(t_uid))}</b> بنجاح!\n💰 الرصيد السابق: <code>{adj['old_points']}</code> ⬅️ الرصيد الجديد: <code>{adj['new_points']}</code> نقطة.",
                reply_to_message_id=message_id,
            )
            return

        if text.startswith("/addworker") or _BOT_TOKEN_REGEX.search(text):
            wait_msg = await bot_api.send_message(
                chat_id,
                "⏳ <b>جاري فحص توكن البوت وتفعيله في منظومة الرفع السريع...</b>",
                reply_to_message_id=message_id,
            )
            saved, errs = await ingest_worker_tokens_from_text(text)
            summary_lines = []
            for r in saved:
                un = f"@{html.escape(r['bot_username'])}" if r.get("bot_username") else html.escape(r.get("bot_name") or "Bot")
                if r.get("channel_ok"):
                    summary_lines.append(f"✅ تم تفعيل <b>{un}</b> فوراً (أدمن بالقناة ويشارك في الرفع الآن 🚀)")
                else:
                    summary_lines.append(
                        f"🟡 تم حفظ <b>{un}</b> — ارفعه <b>مشرف (Admin) بصلاحية النشر</b> في القناة <code>{html.escape(str(TELEGRAM_CHANNEL_ID))}</code> وسيبدأ الرفع تلقائياً!"
                    )
            summary_lines.extend(errs)
            w_text, w_markup = await build_admin_workers_view()
            banner = "\n".join(summary_lines)
            full_reply = f"{banner}\n\n{'─' * 22}\n\n{w_text}" if banner else w_text
            if wait_msg:
                await bot_api.edit_or_send(chat_id, wait_msg, full_reply, reply_markup=w_markup)
            else:
                await bot_api.send_message(chat_id, full_reply, reply_markup=w_markup, reply_to_message_id=message_id)
            return

    # Force-Subscribe Check for ALL users (including shared deep-links, with auto-resume after joining!)
    missing = await get_unjoined_channels(user_id, force_recheck=True, was_blocked_prompt=True)
    if missing:
        f_text, f_markup = build_force_sub_prompt(first_name, missing, resume_payload=deep_arg)
        await bot_api.send_message(chat_id, f_text, reply_markup=f_markup, reply_to_message_id=message_id)
        return

    # Direct Deep-Link Playback (/start m_123, /start m_123_r_999, /start s_45_r_999, /start ep_678_r_999) once subscribed
    if deep_arg:
        delivered = await deliver_deep_link_target(chat_id, from_user, deep_arg)
        if delivered:
            return

    if text.startswith("/points") or text.startswith("/affiliate"):
        asyncio.create_task(track_bot_user_bg(from_user, action_text="🎁 فتح مركز نقاطي والأفلييت", event_type="browse"))
        aff_text, aff_markup = await build_user_affiliate_view(user_id, first_name)
        await bot_api.send_message(chat_id, aff_text, reply_markup=aff_markup, reply_to_message_id=message_id)
        return

    if text.startswith("/start") or text.startswith("/menu"):
        kb = await get_dynamic_main_menu_keyboard()
        await bot_api.send_message(
            chat_id=chat_id,
            text=build_welcome_text(first_name),
            reply_markup=kb,
            reply_to_message_id=message_id
        )
        return

    if text.startswith("/"):
        return

    # Direct comprehensive search
    asyncio.create_task(track_bot_user_bg(from_user, action_text=f"🔍 بحث: {text[:45]}", event_type="search"))
    results = await asyncio.to_thread(db.search_vault, text, 24)
    if not results:
        no_res_text = (
            f"⚠️ لم يتم العثور على نتائج مطابقة لـ: <b>{html.escape(text)}</b>\n\n"
            f"اختر القسم الذي ترغب بمشاهدته ⬇️"
        )
        kb = await get_dynamic_main_menu_keyboard()
        await bot_api.send_message(
            chat_id=chat_id,
            text=no_res_text,
            reply_markup=kb,
            reply_to_message_id=message_id
        )
        return

    search_pairs = []
    for r in results:
        clean_t = db.clean_display_title(r["title"])
        is_ad = bool(r.get("is_adult"))
        tok = "ad-all" if is_ad else "all-all"
        if r["type"] == "movie":
            lbl = f"🔞 {clean_t} (+18)" if is_ad else f"🎬 {clean_t}"
            search_pairs.append((lbl, f"m_play:{r['id']}:{tok}:1"))
        else:
            lbl = f"🔞 {clean_t} (+18)" if is_ad else f"📺 {clean_t}"
            search_pairs.append((lbl, f"s_det:{r['id']}:{tok}:1"))

    rows = build_smart_title_rows(search_pairs)
    rows.append([btn("🏠 القائمة الرئيسية", "menu")])

    await bot_api.send_message(
        chat_id=chat_id,
        text=f"🔍 نتائج البحث عن: {text}",
        reply_markup={"inline_keyboard": rows},
        reply_to_message_id=message_id,
        parse_mode=None
    )


async def deliver_deep_link_target(chat_id: int, from_user: dict, deep_arg: str) -> bool:
    """Delivers a deep-linked movie, series, or episode (including affiliate _r_ payloads) and logs the watch event."""
    user_id = from_user.get("id", chat_id)
    parsed = db.parse_start_payload(deep_arg)
    base_arg = parsed.get("base_arg") or ""
    if not base_arg:
        return False

    if base_arg.startswith("m_") and base_arg[2:].isdigit():
        movie_id = int(base_arg[2:])
        m = await asyncio.to_thread(db.get_movie_details, movie_id)
        if m and m.get("message_id"):
            clean_m_title = db.clean_display_title(m.get("title") or "الفيلم")
            asyncio.create_task(
                track_bot_user_bg(
                    from_user,
                    action_text=f"🎬 شاهد فيلم: {clean_m_title[:45]}",
                    event_type="watch_movie"
                )
            )
            poster_url = get_poster_full_url(m.get("poster_path"))
            caption = build_movie_poster_caption(m, user_id=user_id)
            poster_msg_id = await bot_api.send_poster_with_caption(chat_id, poster_url, caption)
            aff_m_link = build_affiliate_redirect_url("movie", movie_id, user_id)
            is_ad_m = bool(m.get("is_adult"))
            back_lbl = "🔙 رجوع لقسم للكبار فقط" if is_ad_m else "🔙 رجوع للأفلام"
            back_tok = "ad-all" if is_ad_m else "all"

            deliv_user = delivery_bot.get_active_delivery_bot_username()
            if deliv_user:
                deliv_url = f"https://t.me/{deliv_user}?start=m_{movie_id}"
                if user_id:
                    deliv_url += f"_r_{user_id}"
                video_buttons = {
                    "inline_keyboard": [
                        [
                            btn("🍿 اضغط هنا للمشاهدة والتحميل 🎬", url=deliv_url, style="success")
                        ],
                        [
                            btn("📋 نسخ رابط الفيلم (+1 نقطة 🎁)", copy_text=aff_m_link, style="primary"),
                            btn("📤 مشاركة (+1 نقطة)", url=build_share_url(aff_m_link, clean_m_title)),
                        ],
                        [
                            btn(back_lbl, f"back_cat:{back_tok}:1:{poster_msg_id}"),
                            btn("خروج", f"exit_vid:{poster_msg_id}")
                        ]
                    ]
                }
                await bot_api.send_message(
                    chat_id,
                    f"🍿 <b>جاهز للمشاهدة الفورية!</b>\n\nاضغط على الزر الأخضر بالأسفل لتشغيل فيلم <b>{clean_m_title}</b> فوراً في سيرفر المشاهدة السريع وبدون إعلانات:",
                    reply_markup=video_buttons
                )
                return True

            video_buttons = {
                "inline_keyboard": [
                    [
                        btn("📋 نسخ رابط الفيلم (+1 نقطة 🎁)", copy_text=aff_m_link, style="success"),
                        btn("📤 مشاركة (+1 نقطة)", url=build_share_url(aff_m_link, clean_m_title), style="primary"),
                    ],
                    [
                        btn(back_lbl, f"back_cat:{back_tok}:1:{poster_msg_id}"),
                        btn("خروج", f"exit_vid:{poster_msg_id}")
                    ]
                ]
            }
            channel = int(m["channel_id"]) if m.get("channel_id") else TELEGRAM_CHANNEL_ID
            v_cap = build_video_share_caption(f"فيلم {clean_m_title}", aff_m_link)
            await bot_api.deliver_video_with_buttons(chat_id, channel, int(m["message_id"]), video_buttons, video_caption=v_cap)
            return True

    elif base_arg.startswith("s_") and base_arg[2:].isdigit():
        series_id = int(base_arg[2:])
        s = await asyncio.to_thread(db.get_series_details, series_id)
        res_eps = await asyncio.to_thread(db.get_series_episodes, series_id, 1, 18)
        episodes = res_eps.get("episodes", [])
        total_ep_pages = res_eps.get("total_pages", 1)
        if s and episodes:
            first_ep = episodes[0]
            s_title = db.clean_display_title(s.get("title") or "المسلسل")
            asyncio.create_task(
                track_bot_user_bg(
                    from_user,
                    action_text=f"📺 شاهد: {s_title[:35]} ح{first_ep.get('episode_number', 1)}",
                    event_type="watch_episode"
                )
            )
            poster_url = get_poster_full_url(s.get("poster_path"))
            caption = build_series_poster_caption(s, first_ep, user_id=user_id)
            poster_msg_id = await bot_api.send_poster_with_caption(chat_id, poster_url, caption)
            deliv_user = delivery_bot.get_active_delivery_bot_username()
            video_buttons = build_episode_buttons_markup(
                series_id=series_id,
                episodes=episodes,
                active_ep_id=first_ep["id"],
                s_cat="all",
                s_page="1",
                ep_page=1,
                total_ep_pages=total_ep_pages,
                poster_msg_id=poster_msg_id,
                series_title=f"مسلسل {s_title}",
                user_id=user_id,
                delivery_bot_user=deliv_user,
            )
            if deliv_user:
                await bot_api.send_message(
                    chat_id,
                    f"🍿 <b>مسلسل {s_title} — الحلقة {first_ep.get('episode_number', 1)}</b>\n\nاختر الحلقة واضغط على زر التشغيل بالأسفل لمشاهدتها فوراً في سيرفر المشاهدة السريع:",
                    reply_markup=video_buttons
                )
                return True

            channel = int(first_ep["channel_id"]) if first_ep.get("channel_id") else TELEGRAM_CHANNEL_ID
            aff_ep_link = build_affiliate_redirect_url("episode", first_ep["id"], user_id)
            v_cap = build_video_share_caption(f"مسلسل {s_title} — ح{first_ep.get('episode_number', 1)}", aff_ep_link)
            await bot_api.deliver_video_with_buttons(chat_id, channel, int(first_ep["message_id"]), video_buttons, video_caption=v_cap)
            return True

    elif base_arg.startswith("ep_") and base_arg[3:].isdigit():
        ep_id = int(base_arg[3:])
        ep = await asyncio.to_thread(db.get_episode_archive, ep_id)
        if ep and ep.get("message_id"):
            series_id = ep["series_id"]
            s = await asyncio.to_thread(db.get_series_details, series_id)
            res_eps = await asyncio.to_thread(db.get_series_episodes, series_id, 1, 18)
            episodes = res_eps.get("episodes", [])
            total_ep_pages = res_eps.get("total_pages", 1)
            s_title = db.clean_display_title((s or {}).get("title") or ep.get("series_title") or "المسلسل")
            asyncio.create_task(
                track_bot_user_bg(
                    from_user,
                    action_text=f"📺 شاهد: {s_title[:35]} ح{ep.get('episode_number', 1)}",
                    event_type="watch_episode"
                )
            )
            poster_url = get_poster_full_url(s.get("poster_path") if s else None)
            caption = build_series_poster_caption(s or {"id": series_id, "title": ep.get("series_title")}, ep, user_id=user_id)
            poster_msg_id = await bot_api.send_poster_with_caption(chat_id, poster_url, caption)
            deliv_user = delivery_bot.get_active_delivery_bot_username()
            video_buttons = build_episode_buttons_markup(
                series_id=series_id,
                episodes=episodes,
                active_ep_id=ep_id,
                s_cat="all",
                s_page="1",
                ep_page=1,
                total_ep_pages=total_ep_pages,
                poster_msg_id=poster_msg_id,
                series_title=f"مسلسل {s_title}",
                user_id=user_id,
                delivery_bot_user=deliv_user,
            )
            if deliv_user:
                await bot_api.send_message(
                    chat_id,
                    f"🍿 <b>مسلسل {s_title} — الحلقة {ep.get('episode_number', 1)}</b>\n\nاضغط على زر التشغيل بالأسفل لمشاهدتها فوراً في سيرفر المشاهدة السريع:",
                    reply_markup=video_buttons
                )
                return True

            channel = int(ep["channel_id"]) if ep.get("channel_id") else TELEGRAM_CHANNEL_ID
            aff_ep_link = build_affiliate_redirect_url("episode", ep_id, user_id)
            v_cap = build_video_share_caption(f"مسلسل {s_title} — ح{ep.get('episode_number', 1)}", aff_ep_link)
            await bot_api.deliver_video_with_buttons(chat_id, channel, int(ep["message_id"]), video_buttons, video_caption=v_cap)
            return True

    return False


async def handle_callback(cb: dict):
    cb_id = cb["id"]
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        await bot_api.answer_callback(cb_id)
        return

    chat_id = chat["id"]
    message_id = msg.get("message_id")
    from_user = cb.get("from", {})
    user_id = from_user.get("id", chat_id)
    username = from_user.get("username")
    first_name = from_user.get("first_name") or "صديقي"

    if data == "noop":
        await bot_api.answer_callback(cb_id)
        return

    # Automatically save/update user in PostgreSQL database on any button click
    asyncio.create_task(track_bot_user_bg(from_user))

    is_adm = (user_id in _unlocked_admins) or (user_id in ADMIN_IDS)

    if data == "adm_home" and is_adm:
        _unlocked_admins.add(user_id)
        _awaiting_worker_tokens.discard(user_id)
        await bot_api.answer_callback(cb_id, "🔄 تم التحديث")
        adm_text, adm_markup = await build_admin_panel()
        await bot_api.edit_or_send(chat_id, message_id, adm_text, reply_markup=adm_markup)
        return

    if data == "adm_workers" and is_adm:
        _unlocked_admins.add(user_id)
        _awaiting_worker_tokens.discard(user_id)
        await bot_api.answer_callback(cb_id)
        w_text, w_markup = await build_admin_workers_view()
        await bot_api.edit_or_send(chat_id, message_id, w_text, reply_markup=w_markup)
        return

    if data == "adm_wkr_add" and is_adm:
        _unlocked_admins.add(user_id)
        _awaiting_broadcast.discard(user_id)
        _awaiting_worker_tokens.add(user_id)
        await bot_api.answer_callback(cb_id)
        prompt = (
            "⚡ <b>إضافة بوتات رفع جديدة (Uploader Workers):</b>\n\n"
            f"1️⃣ تأكد من إضافة البوت كـ <b>مشرف (Admin)</b> بصلاحية <b>نشر الرسائل</b> في قناة الأرشيف (<code>{html.escape(str(TELEGRAM_CHANNEL_ID))}</code>).\n"
            "2️⃣ أرسل الآن <b>توكن البوت</b> (أو الصق عدة توكنات تحت بعضها في رسالة واحدة، أو اعمل Forward لرسالة @BotFather مباشرة) ⬇️"
        )
        markup = {
            "inline_keyboard": [
                [btn("📋 نسخ ID قناة الأرشيف", copy_text=str(TELEGRAM_CHANNEL_ID))],
                [btn("❌ إلغاء والرجوع", callback_data="adm_wkr_cancel", style="danger")],
            ]
        }
        await bot_api.edit_or_send(chat_id, message_id, prompt, reply_markup=markup)
        return

    if data == "adm_wkr_cancel" and is_adm:
        _awaiting_worker_tokens.discard(user_id)
        await bot_api.answer_callback(cb_id, "تم الإلغاء.")
        w_text, w_markup = await build_admin_workers_view()
        await bot_api.edit_or_send(chat_id, message_id, w_text, reply_markup=w_markup)
        return

    if data == "adm_wkr_recheck" and is_adm:
        _unlocked_admins.add(user_id)
        await bot_api.answer_callback(cb_id, "🔄 جاري فحص صلاحيات النشر بالقناة...")
        ok_cnt, tot_cnt = await recheck_all_worker_bots()
        w_text, w_markup = await build_admin_workers_view()
        note = f"✅ <b>نتيجة الفحص:</b> <code>{ok_cnt}</code> من أصل <code>{tot_cnt}</code> بوت وركر يمتلك صلاحية النشر بالقناة.\n\n"
        await bot_api.edit_or_send(chat_id, message_id, note + w_text, reply_markup=w_markup)
        return

    if data == "adm_wkr_ping" and is_adm:
        _unlocked_admins.add(user_id)
        await bot_api.answer_callback(cb_id, "🧪 جاري إرسال رسالة تجربة باسم كل بوت للخزنة...")
        workers = await asyncio.to_thread(db.get_uploader_bots)
        ping_lines = ["🧪 <b>نتيجة اختبار الإرسال الفعلي للخزنة باسم كل بوت:</b>"]
        p_id = TELEGRAM_BOT_TOKEN.split(":")[0] if ":" in TELEGRAM_BOT_TOKEN else ""
        p_ok, p_err = await send_worker_vault_checkin(TELEGRAM_BOT_TOKEN, "riva (الأساسي)", BOT_USERNAME, p_id)
        if p_ok:
            ping_lines.append(f"• 👑 <b>@{html.escape(BOT_USERNAME)}</b>: ✅ أرسل للخزنة باسمه بنجاح")
        else:
            ping_lines.append(f"• 👑 <b>@{html.escape(BOT_USERNAME)}</b>: ❌ فشل (<code>{html.escape(p_err or '')}</code>)")
        for w in workers:
            lbl = f"@{w['bot_username']}" if w.get("bot_username") else (w.get("bot_name") or f"Bot #{w['id']}")
            if not w.get("is_active"):
                ping_lines.append(f"• ⏸ <b>{html.escape(lbl)}</b>: متوقف مؤقتاً")
                continue
            ok_s, err_s = await send_worker_vault_checkin(
                w["bot_token"],
                w.get("bot_name") or "",
                w.get("bot_username") or "",
                w.get("bot_id") or "",
            )
            await asyncio.to_thread(
                db.upsert_uploader_bot,
                w["bot_token"],
                w.get("bot_id"),
                w.get("bot_username"),
                w.get("bot_name"),
                ok_s,
                err_s,
            )
            if ok_s:
                ping_lines.append(f"• ✅ <b>{html.escape(lbl)}</b>: أرسل للخزنة باسمه بنجاح")
            else:
                ping_lines.append(f"• ❌ <b>{html.escape(lbl)}</b>: فشل (<code>{html.escape(err_s or '')}</code>)")
        w_text, w_markup = await build_admin_workers_view()
        await bot_api.edit_or_send(chat_id, message_id, "\n".join(ping_lines) + "\n\n" + w_text, reply_markup=w_markup)
        return

    if data.startswith("adm_wkr_tog:") and is_adm:
        _unlocked_admins.add(user_id)
        wid_s = data.split(":", 1)[1]
        if wid_s.isdigit():
            res = await asyncio.to_thread(db.toggle_uploader_bot, int(wid_s))
            if res:
                st_msg = "▶️ تم تفعيل البوت في الرفع" if res.get("is_active") else "⏸ تم إيقاف البوت مؤقتاً"
                await bot_api.answer_callback(cb_id, st_msg)
            else:
                await bot_api.answer_callback(cb_id, "⚠️ لم يتم العثور على البوت")
        w_text, w_markup = await build_admin_workers_view()
        await bot_api.edit_or_send(chat_id, message_id, w_text, reply_markup=w_markup)
        return

    if data.startswith("adm_wkr_del:") and is_adm:
        _unlocked_admins.add(user_id)
        wid_s = data.split(":", 1)[1]
        if wid_s.isdigit():
            await asyncio.to_thread(db.delete_uploader_bot, int(wid_s))
        await bot_api.answer_callback(cb_id, "🗑 تم حذف البوت من القائمة.")
        w_text, w_markup = await build_admin_workers_view()
        await bot_api.edit_or_send(chat_id, message_id, w_text, reply_markup=w_markup)
        return

    if data == "adm_compliance" and is_adm:
        _unlocked_admins.add(user_id)
        await bot_api.answer_callback(cb_id)
        c_text, c_markup = await build_admin_compliance_view()
        await bot_api.edit_or_send(chat_id, message_id, c_text, reply_markup=c_markup)
        return

    if data.startswith("adm_users:") and is_adm:
        _unlocked_admins.add(user_id)
        parts = data.split(":")
        fmode = parts[1] if len(parts) > 1 else "active"
        pg = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await bot_api.answer_callback(cb_id)
        u_text, u_markup = await build_admin_users_view(fmode, pg)
        await bot_api.edit_or_send(chat_id, message_id, u_text, reply_markup=u_markup)
        return

    if data.startswith("adm_uinfo:") and is_adm:
        _unlocked_admins.add(user_id)
        parts = data.split(":")
        target_uid = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0
        bf = parts[2] if len(parts) > 2 else "active"
        bp = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        await bot_api.answer_callback(cb_id)
        d_text, d_markup = await build_admin_user_detail_view(target_uid, bf, bp)
        await bot_api.edit_or_send(chat_id, message_id, d_text, reply_markup=d_markup)
        return

    if data.startswith("adm_uchk:") and is_adm:
        _unlocked_admins.add(user_id)
        parts = data.split(":")
        target_uid = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0
        bf = parts[2] if len(parts) > 2 else "active"
        bp = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        await get_unjoined_channels(target_uid, force_recheck=True, was_blocked_prompt=False, clicked_verify=False)
        await bot_api.answer_callback(cb_id, "✅ تم فحص وتحديث اشتراكات المستخدم!")
        d_text, d_markup = await build_admin_user_detail_view(target_uid, bf, bp)
        await bot_api.edit_or_send(chat_id, message_id, d_text, reply_markup=d_markup)
        return

    if data.startswith("adm_events:") and is_adm:
        _unlocked_admins.add(user_id)
        parts = data.split(":")
        efilt = parts[1] if len(parts) > 1 else "all"
        pg = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await bot_api.answer_callback(cb_id)
        ev_text, ev_markup = await build_admin_events_view(efilt, pg)
        await bot_api.edit_or_send(chat_id, message_id, ev_text, reply_markup=ev_markup)
        return

    if data == "adm_vault" and is_adm:
        _unlocked_admins.add(user_id)
        await bot_api.answer_callback(cb_id)
        v_text, v_markup = await build_admin_vault_view()
        await bot_api.edit_or_send(chat_id, message_id, v_text, reply_markup=v_markup)
        return

    if data == "adm_scan" and is_adm:
        _unlocked_admins.add(user_id)
        await bot_api.answer_callback(cb_id, "🔍 جاري الفحص الصامت الشامل...")
        asyncio.create_task(run_silent_users_scan(chat_id, message_id))
        return

    if data == "adm_toggle_notif" and is_adm:
        _unlocked_admins.add(user_id)
        cur_val = await asyncio.to_thread(db.get_bot_setting, "notify_admin_events", "1")
        new_val = "0" if cur_val == "1" else "1"
        await asyncio.to_thread(db.set_bot_setting, "notify_admin_events", new_val)
        msg_toast = "🔔 تم تفعيل إشعارات الدخول والخروج" if new_val == "1" else "🔕 تم إيقاف إشعارات الدخول والخروج"
        await bot_api.answer_callback(cb_id, msg_toast)
        adm_text, adm_markup = await build_admin_panel()
        await bot_api.edit_or_send(chat_id, message_id, adm_text, reply_markup=adm_markup)
        return

    # Admin callback: start broadcast mode
    if data == "adm_bcast" and is_adm:
        _unlocked_admins.add(user_id)
        _awaiting_broadcast.add(user_id)
        await bot_api.answer_callback(cb_id)
        bcast_prompt = (
            "📣 <b>إرسال إعلان عام لجميع المشتركين:</b>\n\n"
            "أرسل الآن رسالة الإعلان (سواء كانت <b>نص، صورة بكابشن، فيديو، أو رابط</b>) وسوف يقوم البوت بإرسالها فوراً لكل المشتركين المسجلين في قاعدة البيانات ⬇️"
        )
        markup = {"inline_keyboard": [[btn("❌ إلغاء الإعلان", callback_data="adm_bcast_cancel", style="danger")]]}
        await bot_api.edit_or_send(chat_id, message_id, bcast_prompt, reply_markup=markup)
        return

    if data == "adm_bcast_cancel" and is_adm:
        _awaiting_broadcast.discard(user_id)
        await bot_api.answer_callback(cb_id, "تم إلغاء الإعلان.")
        adm_text, adm_markup = await build_admin_panel()
        await bot_api.edit_or_send(chat_id, message_id, adm_text, reply_markup=adm_markup)
        return

    # Admin callback: lock admin panel
    if data == "adm_lock" and is_adm:
        _unlocked_admins.discard(user_id)
        _awaiting_broadcast.discard(user_id)
        _awaiting_worker_tokens.discard(user_id)
        await bot_api.answer_callback(cb_id, "🔒 تم قفل لوحة التحكم بنجاح.")
        await bot_api.delete_message(chat_id, message_id)
        await bot_api.send_message(
            chat_id,
            "🔒 تم قفل لوحة التحكم وإخفاء أزرار الإضافة.",
            reply_markup={"remove_keyboard": True}
        )
        welcome_text = build_welcome_text(first_name)
        kb = await get_dynamic_main_menu_keyboard()
        await bot_api.send_message(chat_id, welcome_text, reply_markup=kb)
        return

    # Admin callback: delete required channel
    if data.startswith("adm_del:") and is_adm:
        ch_db_id = data.split(":")[1]
        await asyncio.to_thread(db.remove_required_channel, ch_db_id)
        _verified_users_cache.clear()
        await bot_api.answer_callback(cb_id, "✅ تم حذف الجروب/القناة من الاشتراك الإجباري.")
        adm_text, adm_markup = await build_admin_panel()
        await bot_api.edit_or_send(chat_id, message_id, adm_text, reply_markup=adm_markup)
        return

    # User clicked "✅ تم الاشتراك - تحقق الآن" (check_sub or check_sub:<deep_arg>)
    if data == "check_sub" or data.startswith("check_sub:"):
        resume_arg = data.split(":", 1)[1] if ":" in data else None
        missing = await get_unjoined_channels(user_id, force_recheck=True, was_blocked_prompt=False, clicked_verify=True)
        if missing:
            await bot_api.answer_callback(cb_id, "⚠️ لم تقم بالاشتراك في جميع الجروبات/القنوات المطلوبة بعد!", show_alert=True)
            f_text, f_markup = build_force_sub_prompt(first_name, missing, resume_payload=resume_arg)
            await bot_api.edit_or_send(chat_id, message_id, f_text, reply_markup=f_markup)
            return
        await bot_api.answer_callback(cb_id, "✅ تم التحقق من اشتراكك بنجاح! أهلاً بك.")
        q_info = await asyncio.to_thread(db.qualify_referral_if_pending, user_id, "أتم الاشتراك الإجباري وفتح البوت")
        if q_info:
            asyncio.create_task(notify_referrer_qualification(q_info))
        if resume_arg:
            await bot_api.delete_message(chat_id, message_id)
            delivered = await deliver_deep_link_target(chat_id, from_user, resume_arg)
            if delivered:
                return
        welcome_text = build_welcome_text(first_name)
        kb = await get_dynamic_main_menu_keyboard()
        await bot_api.edit_or_send(chat_id, message_id, welcome_text, reply_markup=kb)
        return

    # Enforce Force-Subscribe on EVERY callback action in real-time
    missing = await get_unjoined_channels(user_id, force_recheck=True, was_blocked_prompt=True)
    if missing:
        await bot_api.answer_callback(
            cb_id,
            "⚠️ لا يمكنك استخدام البوت إلا بعد الاشتراك في جميع القنوات والجروبات المطلوبة!",
            show_alert=True
        )
        # If the user clicked a button under a video/poster, delete the poster too
        if any(data.startswith(p) for p in ("ep:", "ep_pg:", "back_cat:", "back_ser:", "exit_vid:")):
            parts = data.split(":")
            last_part = parts[-1] if len(parts) > 1 else ""
            if last_part.isdigit() and int(last_part) > 0:
                await bot_api.delete_message(chat_id, int(last_part))
        f_text, f_markup = build_force_sub_prompt(first_name, missing)
        await bot_api.edit_or_send(chat_id, message_id, f_text, reply_markup=f_markup)
        return

    # 0.9 User Affiliate & Points Center ("🎁 نقاطي ومشاركة الربح")
    if data == "my_affiliate":
        await bot_api.answer_callback(cb_id)
        asyncio.create_task(track_bot_user_bg(from_user, action_text="🎁 فتح مركز نقاطي والأفلييت", event_type="browse"))
        aff_text, aff_markup = await build_user_affiliate_view(user_id, first_name)
        await bot_api.edit_or_send(chat_id, message_id, aff_text, reply_markup=aff_markup)
        return

    # 1. Main Menu or Exit from Video+Poster
    if data == "menu" or data.startswith("exit_vid"):
        await bot_api.answer_callback(cb_id)
        welcome_text = build_welcome_text(first_name)
        kb = await get_dynamic_main_menu_keyboard()
        if data.startswith("exit_vid"):
            parts = data.split(":")
            poster_msg_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            await asyncio.gather(
                bot_api.delete_message(chat_id, poster_msg_id),
                bot_api.delete_message(chat_id, message_id),
                return_exceptions=True
            )
            await bot_api.send_message(chat_id, welcome_text, reply_markup=kb)
        else:
            await bot_api.edit_or_send(chat_id, message_id, welcome_text, reply_markup=kb)
        return

    # 1.1 Step 2 Menu: Choose Language/Region (nt:m or nt:s)
    if data.startswith("nt:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        media_type = parts[1] if len(parts) > 1 and parts[1] in ("m", "s") else "m"
        t_lbl = "الأفلام" if media_type == "m" else "المسلسلات"
        asyncio.create_task(track_bot_user_bg(from_user, action_text=f"📂 تصفح قسم: {t_lbl}", event_type="browse"))
        await render_region_menu(chat_id, message_id, media_type)
        return

    # 1.2 Step 3 Menu: Choose Genre inside Language/Region (nr:m:en, nr:s:as, etc.)
    if data.startswith("nr:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        media_type = parts[1] if len(parts) > 1 and parts[1] in ("m", "s") else "m"
        region = parts[2] if len(parts) > 2 else "all"
        t_lbl = "أفلام" if media_type == "m" else "مسلسلات"
        r_lbl = db.REGION_SHORT_NAMES.get(region, "الكل")
        asyncio.create_task(track_bot_user_bg(from_user, action_text=f"📂 تصفح: {t_lbl} ({r_lbl})", event_type="browse"))
        await render_genre_menu(chat_id, message_id, media_type, region)
        return

    # 1.3 Step 4 Grid: Filtered Movies or Series List (ng:m:en:act:1)
    if data.startswith("ng:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        media_type = parts[1] if len(parts) > 1 and parts[1] in ("m", "s") else "m"
        region = parts[2] if len(parts) > 2 else "all"
        gcode = parts[3] if len(parts) > 3 else "all"
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
        if page == 1:
            t_lbl = "أفلام" if media_type == "m" else "مسلسلات"
            r_lbl = db.REGION_SHORT_NAMES.get(region, "الكل")
            g_lbl = db.GENRE_CODE_TO_LABEL.get(db.normalize_genre_code(gcode), gcode)
            asyncio.create_task(track_bot_user_bg(from_user, action_text=f"📂 تصفح: {t_lbl} ({r_lbl} - {g_lbl})", event_type="browse"))
        gen_info = await asyncio.to_thread(db.get_available_genres, media_type, region)
        direct = len(gen_info.get("genres", [])) == 0
        if media_type == "m":
            await render_filtered_movies(chat_id, message_id, region, gcode, page, direct_from_region=direct)
        else:
            await render_filtered_series(chat_id, message_id, region, gcode, page, direct_from_region=direct)
        return

    # 1.4 🦸 Marvel Universe (mv:h | mv:f | mv:fr:<key> | mv:o:<page> | mv:s:<page>)
    if data.startswith("mv:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        sub = parts[1] if len(parts) > 1 else "h"
        arg = parts[2] if len(parts) > 2 else ""
        if sub == "h":
            asyncio.create_task(track_bot_user_bg(from_user, action_text="🦸 تصفح عالم مارفل", event_type="browse"))
            await render_marvel_hub(chat_id, message_id)
        elif sub == "f":
            await render_marvel_franchises(chat_id, message_id)
        elif sub == "fr":
            await render_marvel_franchise(chat_id, message_id, arg)
        elif sub == "o":
            await render_marvel_chrono(chat_id, message_id, int(arg) if arg.isdigit() else 1)
        elif sub == "s":
            await render_marvel_series(chat_id, message_id, int(arg) if arg.isdigit() else 1)
        else:
            await render_marvel_hub(chat_id, message_id)
        return

    # 2. Back from Movie Video+Poster to Category Grid
    if data.startswith("back_cat:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        genre = parts[1] if len(parts) > 1 else "all"
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        poster_msg_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await asyncio.gather(
            bot_api.delete_message(chat_id, poster_msg_id),
            bot_api.delete_message(chat_id, message_id),
            return_exceptions=True
        )
        await render_category_movies(chat_id, None, genre, page)
        return

    # 3. Back from Series Video+Poster to Series Grid
    if data.startswith("back_ser:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        s_cat = parts[1] if len(parts) > 1 else "all"
        s_page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        poster_msg_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await asyncio.gather(
            bot_api.delete_message(chat_id, poster_msg_id),
            bot_api.delete_message(chat_id, message_id),
            return_exceptions=True
        )
        await render_series_category(chat_id, None, s_cat, s_page)
        return

    # 4. Search Prompt
    if data == "search_prompt":
        await bot_api.answer_callback(cb_id)
        asyncio.create_task(track_bot_user_bg(from_user, action_text="🔍 فتح شاشة البحث الشامل", event_type="browse"))
        prompt_text = (
            "🔍 <b>البحث الشامل:</b>\n\n"
            "اكتب اسم الفيلم أو المسلسل الذي تبحث عنه وأرسله الآن في المحادثة ⬇️"
        )
        markup = {"inline_keyboard": [[btn("خروج", "menu")]]}
        await bot_api.edit_or_send(chat_id, message_id, prompt_text, reply_markup=markup)
        return

    # 4.5 Adult (+18) Stars-Gated Section
    if data == "adult_gate":
        await bot_api.answer_callback(cb_id)
        asyncio.create_task(track_bot_user_bg(from_user, action_text="🔞 فتح بوابة قسم للكبار فقط (+18 Stars)", event_type="browse"))
        await render_adult18_stars_gate(chat_id, message_id, user_id)
        return

    # Stars invoice trigger (user clicked "Pay X Stars")
    if data == "adult18_pay":
        await bot_api.answer_callback(cb_id)
        # Delete the gate message first so the invoice appears cleanly
        if message_id:
            await bot_api.delete_message(chat_id, message_id)
        sent = await send_adult18_stars_invoice(chat_id)
        if not sent:
            await bot_api.send_message(chat_id, "⚠️ تعذر إرسال فاتورة الدفع، يُرجى المحاولة لاحقاً.")
        return

    # Paid users browsing +18 movies list
    if data.startswith("a18_list:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        has_access = await asyncio.to_thread(db.check_adult18_access, user_id)
        if not has_access:
            await render_adult18_stars_gate(chat_id, message_id, user_id)
            return
        await render_adult18_movies_section(chat_id, message_id, page)
        return

    # Paid users playing a +18 movie
    if data.startswith("a18_play:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        movie_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        list_page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        has_access = await asyncio.to_thread(db.check_adult18_access, user_id)
        if not has_access:
            await render_adult18_stars_gate(chat_id, message_id, user_id)
            return
        asyncio.create_task(track_bot_user_bg(from_user, action_text="🔞 مشاهدة فيلم +18", event_type="watch"))
        await render_adult18_movie_play(chat_id, movie_id, list_page, user_id)
        return

    # (Legacy) old-style adult section — keep for backward compatibility
    if data.startswith("adult_list:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        if page == 1:
            asyncio.create_task(track_bot_user_bg(from_user, action_text="📂 تصفح قسم: للكبار فقط 🔞", event_type="browse"))
        await render_adult_section(chat_id, message_id, page)
        return

    # Admin: delete a +18 movie from the list
    if data.startswith("a18_del:"):
        is_adm = (user_id in _unlocked_admins) or (user_id in ADMIN_IDS)
        if not is_adm:
            await bot_api.answer_callback(cb_id, "⛔ ليس لديك صلاحية.")
            return
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        movie_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        deleted = await asyncio.to_thread(db.delete_adult18_movie, movie_id)
        await bot_api.send_message(
            chat_id,
            f"{'✅ تم حذف الفيلم بنجاح.' if deleted else '⚠️ لم يتم العثور على الفيلم.'}"
        )
        return


    # 5. Category Movies Grid
    if data.startswith("cat:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        genre = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 1
        if page == 1:
            asyncio.create_task(track_bot_user_bg(from_user, action_text=f"📂 تصفح أفلام ({genre})", event_type="browse"))
        await render_category_movies(chat_id, message_id, genre, page)
        return

    # 6. Movie View: [Poster + Description Caption] -> [Video Itself] -> [Buttons Under Video]
    if data.startswith("m_play:"):
        parts = data.split(":")
        movie_id = int(parts[1])
        genre = parts[2] if len(parts) > 2 else "all"
        page = parts[3] if len(parts) > 3 else "1"

        m = await asyncio.to_thread(db.get_movie_details, movie_id)
        if not m or not m.get("message_id"):
            await bot_api.answer_callback(cb_id, "⚠️ الفيلم غير متوفر حالياً.", show_alert=True)
            return

        await bot_api.answer_callback(cb_id)
        clean_m_title = db.clean_display_title(m.get("title") or "الفيلم")
        asyncio.create_task(
            track_bot_user_bg(
                from_user,
                action_text=f"🎬 شاهد فيلم: {clean_m_title[:45]}",
                event_type="watch_movie"
            )
        )

        poster_url = get_poster_full_url(m.get("poster_path"))
        caption = build_movie_poster_caption(m, user_id=user_id)

        # Delete category menu & send Poster with Description as Caption at the top
        _, poster_msg_id = await asyncio.gather(
            bot_api.delete_message(chat_id, message_id),
            bot_api.send_poster_with_caption(chat_id, poster_url, caption)
        )

        # Send the Movie Video right underneath with the user's personal Affiliate Redirect Share Buttons
        aff_m_link = build_affiliate_redirect_url("movie", movie_id, user_id)
        is_ad_m = bool(m.get("is_adult")) or genre.startswith("ad")
        back_lbl = "🔙 رجوع لقسم للكبار فقط" if is_ad_m else "🔙 رجوع للأفلام"
        back_tok = "ad-all" if is_ad_m else genre

        deliv_user = delivery_bot.get_active_delivery_bot_username()
        if deliv_user:
            deliv_url = f"https://t.me/{deliv_user}?start=m_{movie_id}"
            if user_id:
                deliv_url += f"_r_{user_id}"
            video_buttons = {
                "inline_keyboard": [
                    [
                        btn("🍿 اضغط هنا للمشاهدة والتحميل 🎬", url=deliv_url, style="success")
                    ],
                    [
                        btn("📋 نسخ رابط الفيلم (+1 نقطة 🎁)", copy_text=aff_m_link, style="primary"),
                        btn("📤 مشاركة (+1 نقطة)", url=build_share_url(aff_m_link, clean_m_title)),
                    ],
                    [
                        btn(back_lbl, f"back_cat:{back_tok}:{page}:{poster_msg_id}"),
                        btn("خروج", f"exit_vid:{poster_msg_id}")
                    ]
                ]
            }
            await bot_api.send_message(
                chat_id,
                f"🍿 <b>جاهز للمشاهدة الفورية!</b>\n\nاضغط على الزر الأخضر بالأسفل لتشغيل فيلم <b>{clean_m_title}</b> فوراً في سيرفر المشاهدة السريع وبدون إعلانات:",
                reply_markup=video_buttons
            )
            return

        video_buttons = {
            "inline_keyboard": [
                [
                    btn("📋 نسخ رابط الفيلم (+1 نقطة 🎁)", copy_text=aff_m_link, style="success"),
                    btn("📤 مشاركة (+1 نقطة)", url=build_share_url(aff_m_link, clean_m_title), style="primary"),
                ],
                [
                    btn(back_lbl, f"back_cat:{back_tok}:{page}:{poster_msg_id}"),
                    btn("خروج", f"exit_vid:{poster_msg_id}")
                ]
            ]
        }
        channel = int(m["channel_id"]) if m.get("channel_id") else TELEGRAM_CHANNEL_ID
        v_cap = build_video_share_caption(f"فيلم {clean_m_title}", aff_m_link)
        await bot_api.deliver_video_with_buttons(chat_id, channel, int(m["message_id"]), video_buttons, video_caption=v_cap)
        return

    # 7. Series Category Grid
    if data.startswith("series_cat:") or data.startswith("series:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        if data.startswith("series_cat:"):
            s_cat = parts[1]
            page = int(parts[2]) if len(parts) > 2 else 1
        else:
            s_cat = "all"
            page = int(parts[1]) if len(parts) > 1 else 1
        if page == 1:
            asyncio.create_task(track_bot_user_bg(from_user, action_text="📂 تصفح المسلسلات", event_type="browse"))
        await render_series_category(chat_id, message_id, s_cat, page)
        return

    # 8. Series View: [Poster + Description Caption] -> [Episode 1 Video] -> [Episode Buttons Under Video]
    if data.startswith("s_det:"):
        parts = data.split(":")
        series_id = int(parts[1])
        s_cat = parts[2] if len(parts) > 2 else "all"
        s_page = parts[3] if len(parts) > 3 else "1"
        ep_page = int(parts[4]) if len(parts) > 4 else 1

        s = await asyncio.to_thread(db.get_series_details, series_id)
        if not s:
            await bot_api.answer_callback(cb_id, "⚠️ لم يتم العثور على المسلسل.", show_alert=True)
            return

        res_eps = await asyncio.to_thread(db.get_series_episodes, series_id, ep_page, 18)
        episodes = res_eps.get("episodes", [])
        total_ep_pages = res_eps.get("total_pages", 1)

        if not episodes:
            await bot_api.answer_callback(cb_id, "⚠️ لا توجد حلقات متوفرة حالياً.", show_alert=True)
            return

        await bot_api.answer_callback(cb_id)

        first_ep = episodes[0]
        s_title = db.clean_display_title(s.get("title") or "المسلسل")
        asyncio.create_task(
            track_bot_user_bg(
                from_user,
                action_text=f"📺 شاهد: {s_title[:35]} ح{first_ep.get('episode_number', 1)}",
                event_type="watch_episode"
            )
        )
        poster_url = get_poster_full_url(s.get("poster_path"))
        caption = build_series_poster_caption(s, first_ep, user_id=user_id)

        # 1. Delete series list & send Poster + Description Caption at the top
        _, poster_msg_id = await asyncio.gather(
            bot_api.delete_message(chat_id, message_id),
            bot_api.send_poster_with_caption(chat_id, poster_url, caption)
        )

        # 2. Send Episode 1 Video right underneath the Poster, with Episode Buttons underneath the Video
        deliv_user = delivery_bot.get_active_delivery_bot_username()
        video_buttons = build_episode_buttons_markup(
            series_id=series_id,
            episodes=episodes,
            active_ep_id=first_ep["id"],
            s_cat=s_cat,
            s_page=s_page,
            ep_page=ep_page,
            total_ep_pages=total_ep_pages,
            poster_msg_id=poster_msg_id,
            series_title=f"مسلسل {s_title}",
            user_id=user_id,
            delivery_bot_user=deliv_user,
        )
        if deliv_user:
            await bot_api.send_message(
                chat_id,
                f"🍿 <b>مسلسل {s_title} — الحلقة {first_ep.get('episode_number', 1)}</b>\n\nاختر الحلقة واضغط على زر التشغيل بالأسفل لمشاهدتها فوراً في سيرفر المشاهدة السريع:",
                reply_markup=video_buttons
            )
            return

        channel = int(first_ep["channel_id"]) if first_ep.get("channel_id") else TELEGRAM_CHANNEL_ID
        aff_ep_link = build_affiliate_redirect_url("episode", first_ep["id"], user_id)
        v_cap = build_video_share_caption(f"مسلسل {s_title} — ح{first_ep.get('episode_number', 1)}", aff_ep_link)
        await bot_api.deliver_video_with_buttons(chat_id, channel, int(first_ep["message_id"]), video_buttons, video_caption=v_cap)
        return

    # 9. Paginate Episode Buttons Underneath Video (in-place without re-sending video or poster)
    if data.startswith("ep_pg:"):
        await bot_api.answer_callback(cb_id)
        parts = data.split(":")
        series_id = int(parts[1])
        active_ep_id = int(parts[2])
        s_cat = parts[3] if len(parts) > 3 else "all"
        s_page = parts[4] if len(parts) > 4 else "1"
        ep_page = int(parts[5]) if len(parts) > 5 else 1
        poster_msg_id = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0

        s = await asyncio.to_thread(db.get_series_details, series_id)
        s_title = db.clean_display_title((s or {}).get("title") or "المسلسل")
        res_eps = await asyncio.to_thread(db.get_series_episodes, series_id, ep_page, 18)
        episodes = res_eps.get("episodes", [])
        total_ep_pages = res_eps.get("total_pages", 1)

        deliv_user = delivery_bot.get_active_delivery_bot_username()
        video_buttons = build_episode_buttons_markup(
            series_id=series_id,
            episodes=episodes,
            active_ep_id=active_ep_id,
            s_cat=s_cat,
            s_page=s_page,
            ep_page=ep_page,
            total_ep_pages=total_ep_pages,
            poster_msg_id=poster_msg_id,
            series_title=f"مسلسل {s_title}",
            user_id=user_id,
            delivery_bot_user=deliv_user,
        )
        await bot_api.api_call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": video_buttons
        })
        return

    # 10. Switch Episode: Keep Poster at top (update its episode caption), swap Video below it + Buttons under Video
    if data.startswith("ep:"):
        parts = data.split(":")
        ep_id = int(parts[1])
        s_cat = parts[2] if len(parts) > 2 else "all"
        s_page = parts[3] if len(parts) > 3 else "1"
        ep_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
        poster_msg_id = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0

        ep = await asyncio.to_thread(db.get_episode_archive, ep_id)
        if not ep or not ep.get("message_id"):
            await bot_api.answer_callback(cb_id, "⚠️ الحلقة غير متوفرة حالياً.", show_alert=True)
            return

        await bot_api.answer_callback(cb_id)
        series_id = ep["series_id"]
        s = await asyncio.to_thread(db.get_series_details, series_id)
        s_title = db.clean_display_title((s or {}).get("title") or ep.get("series_title") or "المسلسل")
        asyncio.create_task(
            track_bot_user_bg(
                from_user,
                action_text=f"📺 شاهد: {s_title[:35]} ح{ep.get('episode_number', 1)}",
                event_type="watch_episode"
            )
        )
        res_eps = await asyncio.to_thread(db.get_series_episodes, series_id, ep_page, 18)
        episodes = res_eps.get("episodes", [])
        total_ep_pages = res_eps.get("total_pages", 1)

        caption = build_series_poster_caption(s or {"id": series_id, "title": ep.get("series_title")}, ep, user_id=user_id)

        deliv_user = delivery_bot.get_active_delivery_bot_username()
        if deliv_user:
            if poster_msg_id > 0:
                await asyncio.gather(
                    bot_api.edit_poster_caption(chat_id, poster_msg_id, caption),
                    return_exceptions=True
                )
            video_buttons = build_episode_buttons_markup(
                series_id=series_id,
                episodes=episodes,
                active_ep_id=ep_id,
                s_cat=s_cat,
                s_page=s_page,
                ep_page=ep_page,
                total_ep_pages=total_ep_pages,
                poster_msg_id=poster_msg_id,
                series_title=f"مسلسل {s_title}",
                user_id=user_id,
                delivery_bot_user=deliv_user,
            )
            await bot_api.edit_or_send(
                chat_id,
                message_id,
                f"🍿 <b>مسلسل {s_title} — الحلقة {ep.get('episode_number', 1)}</b>\n\nاضغط على زر التشغيل بالأسفل لمشاهدة الحلقة في سيرفر المشاهدة السريع:",
                reply_markup=video_buttons
            )
            return

        if poster_msg_id > 0:
            # Poster is already right above the video: update its caption & delete old video concurrently
            await asyncio.gather(
                bot_api.edit_poster_caption(chat_id, poster_msg_id, caption),
                bot_api.delete_message(chat_id, message_id),
                return_exceptions=True
            )
        else:
            poster_url = get_poster_full_url(s.get("poster_path") if s else None)
            _, poster_msg_id = await asyncio.gather(
                bot_api.delete_message(chat_id, message_id),
                bot_api.send_poster_with_caption(chat_id, poster_url, caption),
                return_exceptions=True
            )

        video_buttons = build_episode_buttons_markup(
            series_id=series_id,
            episodes=episodes,
            active_ep_id=ep_id,
            s_cat=s_cat,
            s_page=s_page,
            ep_page=ep_page,
            total_ep_pages=total_ep_pages,
            poster_msg_id=poster_msg_id,
            series_title=f"مسلسل {s_title}",
            user_id=user_id,
        )
        channel = int(ep["channel_id"]) if ep.get("channel_id") else TELEGRAM_CHANNEL_ID
        aff_ep_link = build_affiliate_redirect_url("episode", ep_id, user_id)
        v_cap = build_video_share_caption(f"مسلسل {s_title} — ح{ep.get('episode_number', 1)}", aff_ep_link)
        await bot_api.deliver_video_with_buttons(chat_id, channel, int(ep["message_id"]), video_buttons, video_caption=v_cap)
        return

    await bot_api.answer_callback(cb_id)


async def periodic_cache_refresher():
    """Keeps RAM cache warm in the background so user clicks never wait for SQL."""
    while True:
        await asyncio.to_thread(db.warm_up_cache)
        # Menu cache lives 240s; refreshing every 200s keeps clicks instant without hammering Postgres
        await asyncio.sleep(200)


async def initial_silent_health_sync():
    """Runs once gently after startup to ensure historical blocked/left users AND group memberships are accurately detected."""
    await asyncio.sleep(5)
    try:
        already = await asyncio.to_thread(db.get_bot_setting, "compliance_sync_v2_done", "0")
        if already == "1":
            return
        users = await asyncio.to_thread(db.get_all_bot_users_basic)
        for u in users[:500]:
            uid = u["user_id"]
            res = await bot_api.api_call("sendChatAction", {"chat_id": uid, "action": "typing"})
            if not res.get("ok"):
                desc = (res.get("description") or "").lower()
                if "blocked" in desc or "deactivated" in desc or "chat not found" in desc or "forbidden" in desc:
                    reason = "deactivated" if "deactivated" in desc else "blocked"
                    await asyncio.to_thread(db.mark_user_left, uid, u.get("username"), u.get("first_name"), None, reason)
            await get_unjoined_channels(uid, force_recheck=True, was_blocked_prompt=False, clicked_verify=False)
            await asyncio.sleep(0.06)
        await asyncio.to_thread(db.set_bot_setting, "compliance_sync_v2_done", "1")
        logger.info(f"Initial silent user & group compliance scan completed for {len(users[:500])} users.")
    except Exception as e:
        logger.warning(f"Initial silent health scan warning: {e}")


async def run_bot():
    logger.info("Starting High-Speed Riva Cinema Bot (Bot API Engine)...")
    await bot_api.start_session()

    global BOT_USERNAME
    me_res = await bot_api.api_call("getMe")
    if me_res.get("ok"):
        me = me_res["result"]
        if me.get("username"):
            BOT_USERNAME = me["username"]
        logger.info(f"Connected as @{me.get('username')} (ID: {me.get('id')})")
    else:
        logger.error(f"Failed to verify bot token: {me_res}")

    delivery_bot.set_main_bot_username(BOT_USERNAME)
    delivery_bot.start_delivery_bot_background()

    await bot_api.api_call("deleteWebhook", {"drop_pending_updates": False})
    asyncio.create_task(periodic_cache_refresher())
    asyncio.create_task(initial_silent_health_sync())

    offset = 0
    while True:
        try:
            res = await bot_api.api_call("getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": [
                    "message", "callback_query",
                    "pre_checkout_query",   # ← Telegram Stars payment step 1
                    "my_chat_member", "chat_member"
                ]
            })
            if not res.get("ok"):
                await asyncio.sleep(2)
                continue

            updates = res.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                if "message" in upd:
                    msg_upd = upd["message"]
                    # ── Telegram Stars: successful_payment lands as a message ──
                    if "successful_payment" in msg_upd:
                        asyncio.create_task(handle_successful_payment(msg_upd))
                    else:
                        asyncio.create_task(handle_message(msg_upd))
                elif "callback_query" in upd:
                    asyncio.create_task(handle_callback(upd["callback_query"]))
                elif "pre_checkout_query" in upd:
                    # Telegram Stars: always approve — real validation happens on successful_payment
                    pcq = upd["pre_checkout_query"]
                    asyncio.create_task(bot_api.api_call("answerPreCheckoutQuery", {
                        "pre_checkout_query_id": pcq["id"],
                        "ok": True,
                    }))
                elif "my_chat_member" in upd:
                    asyncio.create_task(handle_my_chat_member(upd["my_chat_member"]))
        except Exception as e:
            logger.error(f"Polling loop error: {e}")
            await asyncio.sleep(2)


async def handle_successful_payment(msg: dict):
    """
    Handles Telegram Stars successful_payment for the +18 section.
    Grants the user 30 days of access and shows them the content list immediately.
    """
    chat_id  = msg["chat"]["id"]
    user_id  = msg.get("from", {}).get("id", chat_id)
    payment  = msg.get("successful_payment", {})
    stars    = payment.get("total_amount", ADULT18_STARS_PRICE)
    charge_id = payment.get("telegram_payment_charge_id", "")
    payload  = payment.get("invoice_payload", "")

    if payload != "adult18_access_30d":
        # Not our invoice — ignore
        return

    # Persist access
    await asyncio.to_thread(db.grant_adult18_access, user_id, stars, charge_id)

    logger.info(f"Stars payment: user {user_id} paid {stars}⭐ for adult18 access (charge={charge_id})")

    await bot_api.send_message(
        chat_id,
        f"🎉 <b>تم الدفع بنجاح! مرحباً بك في قسم +18 🔞</b>\n\n"
        f"⭐ دفعت <b>{stars} نجمة</b> — وصولك مفعّل لمدة <b>{ADULT18_ACCESS_DAYS} يوماً</b> كاملاً.\n\n"
        "الآن يمكنك مشاهدة جميع الأفلام الحصرية في القسم 👇",
    )
    await render_adult18_movies_section(chat_id, None, page=1)



if __name__ == "__main__":
    asyncio.run(run_bot())
