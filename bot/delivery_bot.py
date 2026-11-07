import asyncio
import os
import re
import html
import logging
import urllib.parse
import aiohttp
from bot.config import (
    DELIVERY_BOT_TOKEN,
    DELIVERY_BOT_USERNAME,
    TELEGRAM_CHANNEL_ID,
    TELEGRAM_BOT_TOKEN,
)
from bot import db

logger = logging.getLogger("riva_delivery_bot")

_ACTIVE_DELIVERY_USERNAME: str = DELIVERY_BOT_USERNAME
_MAIN_BOT_USERNAME: str = "riva108_bot"


def set_main_bot_username(username: str):
    global _MAIN_BOT_USERNAME
    if username:
        _MAIN_BOT_USERNAME = username.strip().lstrip("@")


def get_main_bot_username() -> str:
    return _MAIN_BOT_USERNAME or "riva108_bot"


def set_active_delivery_bot_username(username: str):
    global _ACTIVE_DELIVERY_USERNAME
    if username:
        _ACTIVE_DELIVERY_USERNAME = username.strip().lstrip("@")


def get_active_delivery_bot_username() -> str:
    global _ACTIVE_DELIVERY_USERNAME
    return _ACTIVE_DELIVERY_USERNAME or DELIVERY_BOT_USERNAME


def is_delivery_bot_enabled() -> bool:
    return bool(DELIVERY_BOT_TOKEN and get_active_delivery_bot_username())


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


def build_share_url(deep_link: str, title: str) -> str:
    q_url = urllib.parse.quote(deep_link, safe="")
    q_txt = urllib.parse.quote(f"🎬 شاهد {title} بجودة عالية مباشرة بدون إعلانات:", safe="")
    return f"https://t.me/share/url?url={q_url}&text={q_txt}"


class DeliveryBotRunner:
    def __init__(self, token: str):
        self.token = token.strip()
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        self.session: aiohttp.ClientSession | None = None
        self.bot_username: str = get_active_delivery_bot_username()
        self.bot_id: int | None = None

    async def start_session(self):
        connector = aiohttp.TCPConnector(limit=50, keepalive_timeout=60, ttl_dns_cache=300)
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
        url = f"{self.api_base}/{method}"
        try:
            async with self.session.post(url, json=payload or {}) as resp:
                return await resp.json()
        except Exception as e:
            logger.error(f"Delivery Bot API call error in {method}: {e}")
            return {"ok": False, "description": str(e)}

    async def send_message(self, chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = "HTML"):
        payload = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.api_call("sendMessage", payload)

    async def copy_video(
        self,
        chat_id: int,
        channel_id: int,
        vault_message_id: int,
        caption: str = "",
        reply_markup: dict = None
    ) -> bool:
        copy_payload = {
            "chat_id": chat_id,
            "from_chat_id": channel_id,
            "message_id": vault_message_id,
            "caption": caption or "",
            "reply_markup": reply_markup,
        }
        if caption:
            copy_payload["parse_mode"] = "HTML"
        res = await self.api_call("copyMessage", copy_payload)
        if isinstance(res, dict) and res.get("ok"):
            return True

        logger.warning(f"DeliveryBot copyMessage failed ({res}), attempting forward fallback.")
        fwd_res = await self.api_call("forwardMessage", {
            "chat_id": chat_id,
            "from_chat_id": channel_id,
            "message_id": vault_message_id
        })
        if isinstance(fwd_res, dict) and fwd_res.get("ok"):
            if reply_markup:
                await self.send_message(chat_id, caption or "🍿 مشاهدة ممتعة!", reply_markup=reply_markup)
            return True
        return False

    async def handle_start(self, chat_id: int, from_user: dict, arg: str):
        main_bot = get_main_bot_username()
        user_id = from_user.get("id", chat_id)
        first_name = from_user.get("first_name", "صديقي")

        parsed = db.parse_start_payload(arg)
        base_arg = parsed.get("base_arg") or ""
        referrer_id = parsed.get("referrer_id")

        if referrer_id:
            try:
                ref_id_int = int(referrer_id) if str(referrer_id).isdigit() else None
                ref_info = {
                    "referrer_id": ref_id_int,
                    "target_type": "delivery",
                    "target_id": None,
                    "raw_code": arg,
                    "entry_source": "delivery_bot",
                } if ref_id_int else None
                await asyncio.to_thread(
                    db.register_bot_user,
                    user_id=user_id,
                    username=from_user.get("username"),
                    first_name=first_name,
                    last_name=from_user.get("last_name"),
                    referral_info=ref_info,
                )
            except Exception as e:
                logger.warning(f"Error logging referral in delivery bot: {e}")

        # 1. Movie Delivery (m_123)
        if base_arg.startswith("m_") and base_arg[2:].isdigit():
            movie_id = int(base_arg[2:])
            m = await asyncio.to_thread(db.get_movie_details, movie_id)
            if not m or not m.get("message_id"):
                await self.send_message(
                    chat_id,
                    "⚠️ <b>عذراً، لم يتم العثور على هذا الفيلم أو جاري تحديثه.</b>\n\nتفضل بالبحث في البوت الرئيسي:",
                    reply_markup={"inline_keyboard": [[btn("🎬 الدخول للبوت الرئيسي", url=f"https://t.me/{main_bot}")]]}
                )
                return

            clean_title = db.clean_display_title(m.get("title") or "الفيلم")
            channel = int(m["channel_id"]) if m.get("channel_id") else TELEGRAM_CHANNEL_ID
            aff_m_link = f"https://t.me/{main_bot}?start=m_{movie_id}"
            if user_id:
                aff_m_link += f"_r_{user_id}"

            caption = (
                f"🎬 <b>فيلم {html.escape(clean_title)}</b>\n"
                f"🍿 مشاهدة ممتعة بجودة عالية وسيرفر تشغيل سريع وبدون إعلانات!\n\n"
                f"🔗 رابط المشاهدة المباشر: {html.escape(aff_m_link)}"
            )
            buttons = {
                "inline_keyboard": [
                    [
                        btn("🎬 العودة إلى البوت الرئيسي", url=f"https://t.me/{main_bot}"),
                        btn("📤 مشاركة (+1 نقطة)", url=build_share_url(aff_m_link, clean_title))
                    ],
                    [
                        btn("📋 نسخ رابط الفيلم (+1 نقطة)", copy_text=aff_m_link)
                    ]
                ]
            }
            ok = await self.copy_video(chat_id, channel, int(m["message_id"]), caption=caption, reply_markup=buttons)
            if not ok:
                await self.send_message(
                    chat_id,
                    "⚠️ تعذر تشغيل الفيديو حالياً، يرجى المحاولة لاحقاً أو تجربة فيلم آخر من البوت الرئيسي:",
                    reply_markup={"inline_keyboard": [[btn("🎬 الدخول للبوت الرئيسي", url=f"https://t.me/{main_bot}")]]}
                )
            return

        # 2. Episode Delivery (ep_123)
        if base_arg.startswith("ep_") and base_arg[3:].isdigit():
            ep_id = int(base_arg[3:])
            ep = await asyncio.to_thread(db.get_episode_archive, ep_id)
            if not ep or not ep.get("message_id"):
                await self.send_message(
                    chat_id,
                    "⚠️ <b>عذراً، لم يتم العثور على هذه الحلقة.</b>",
                    reply_markup={"inline_keyboard": [[btn("🎬 الدخول للبوت الرئيسي", url=f"https://t.me/{main_bot}")]]}
                )
                return

            series_id = ep["series_id"]
            s_title = db.clean_display_title(ep.get("series_title") or "المسلسل")
            ep_num = ep.get("episode_number", 1)
            sea_num = ep.get("season_number", 1)
            channel = int(ep["channel_id"]) if ep.get("channel_id") else TELEGRAM_CHANNEL_ID

            # Look up next episode for 1-click continuous binge-watching
            next_ep = await asyncio.to_thread(db.get_next_episode, series_id, sea_num, ep_num)
            deliv_user = self.bot_username or get_active_delivery_bot_username()

            aff_ep_link = f"https://t.me/{main_bot}?start=ep_{ep_id}"
            if user_id:
                aff_ep_link += f"_r_{user_id}"

            caption = (
                f"📺 <b>مسلسل {html.escape(s_title)} — الموسم {sea_num} الحلقة {ep_num}</b>\n"
                f"🍿 مشاهدة ممتعة بجودة عالية وسيرفر سريع بدون إعلانات!\n\n"
                f"🔗 رابط الحلقة: {html.escape(aff_ep_link)}"
            )

            row_top = []
            if next_ep:
                next_url = f"https://t.me/{deliv_user}?start=ep_{next_ep['id']}"
                if user_id:
                    next_url += f"_r_{user_id}"
                row_top.append(btn(f"⏭️ الحلقة التالية ({next_ep['episode_number']})", url=next_url))

            buttons_rows = []
            if row_top:
                buttons_rows.append(row_top)
            buttons_rows.append([
                btn("📋 باقي الحلقات في البوت الرئيسي 📺", url=f"https://t.me/{main_bot}?start=s_{series_id}"),
                btn("🎬 البوت الرئيسي", url=f"https://t.me/{main_bot}"),
            ])
            buttons_rows.append([
                btn("📤 مشاركة (+1 نقطة)", url=build_share_url(aff_ep_link, f"{s_title} ح{ep_num}")),
                btn("📋 نسخ رابط الحلقة", copy_text=aff_ep_link)
            ])

            ok = await self.copy_video(chat_id, channel, int(ep["message_id"]), caption=caption, reply_markup={"inline_keyboard": buttons_rows})
            if not ok:
                await self.send_message(
                    chat_id,
                    "⚠️ تعذر تشغيل الحلقة حالياً، يرجى المحاولة لاحقاً.",
                    reply_markup={"inline_keyboard": [[btn("🎬 الدخول للبوت الرئيسي", url=f"https://t.me/{main_bot}")]]}
                )
            return

        # 3. Series First Episode Delivery (s_123)
        if base_arg.startswith("s_") and base_arg[2:].isdigit():
            series_id = int(base_arg[2:])
            res_eps = await asyncio.to_thread(db.get_series_episodes, series_id, 1, 1)
            episodes = res_eps.get("episodes", [])
            if episodes:
                first_ep = episodes[0]
                await self.handle_start(chat_id, from_user, f"ep_{first_ep['id']}" + (f"_r_{referrer_id}" if referrer_id else ""))
                return
            await self.send_message(
                chat_id,
                "⚠️ لم يتم العثور على حلقات لهذا المسلسل حالياً.",
                reply_markup={"inline_keyboard": [[btn("🎬 الدخول للبوت الرئيسي", url=f"https://t.me/{main_bot}")]]}
            )
            return

        # 4. Default Welcome Greeting
        welcome_text = (
            f"🍿 <b>أهلاً بك صديقي ({html.escape(first_name)}) في بوت المشاهدة والتشغيل السريع!</b>\n\n"
            "هذا البوت مخصص لتشغيل وبث الفيديوهات بدقة عالية وبدون إعلانات لحماية واستقرار الخدمة.\n\n"
            "🎬 <b>لتصفح آلاف الأفلام والمسلسلات، سلاسل مارفل، والبحث الشامل:</b>\n"
            f"تفضل بالدخول إلى البوت الرئيسي بالضغط على الزر أدناه:"
        )
        markup = {
            "inline_keyboard": [
                [btn("🎬 الدخول إلى البوت الرئيسي وتصفح الكل", url=f"https://t.me/{main_bot}")],
                [btn("🎁 مركز النقاط والأفلييت", url=f"https://t.me/{main_bot}?start=affiliate")]
            ]
        }
        await self.send_message(chat_id, welcome_text, reply_markup=markup)

    async def handle_message(self, msg: dict):
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        from_user = msg.get("from", {})
        text = (msg.get("text") or "").strip()

        if not chat_id:
            return

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            await self.handle_start(chat_id, from_user, arg)
            return

        # Any other message redirects user to the main bot
        main_bot = get_main_bot_username()
        await self.send_message(
            chat_id,
            "🍿 لتصفح جميع الأفلام والمسلسلات والبحث، تفضل بالدخول إلى البوت الرئيسي:",
            reply_markup={"inline_keyboard": [[btn("🎬 الانتقال إلى البوت الرئيسي", url=f"https://t.me/{main_bot}")]]}
        )

    async def run(self):
        logger.info("Initializing Secondary Delivery Bot Engine...")
        await self.start_session()

        me_res = await self.api_call("getMe")
        if me_res.get("ok"):
            me = me_res["result"]
            self.bot_id = me.get("id")
            uname = me.get("username") or ""
            if uname:
                self.bot_username = uname
                set_active_delivery_bot_username(uname)
            logger.info(f"✅ Secondary Delivery Bot connected as @{self.bot_username} (ID: {self.bot_id})")
        else:
            logger.error(f"❌ Failed to verify Secondary Delivery Bot token: {me_res}")
            return

        await self.api_call("deleteWebhook", {"drop_pending_updates": False})

        offset = 0
        while True:
            try:
                res = await self.api_call("getUpdates", {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": ["message"]
                })
                if not res.get("ok"):
                    await asyncio.sleep(2)
                    continue

                for upd in res.get("result", []):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        asyncio.create_task(self.handle_message(upd["message"]))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Delivery Bot polling loop error: {e}")
                await asyncio.sleep(2)

        await self.close_session()


_runner_instance: DeliveryBotRunner | None = None


def start_delivery_bot_background():
    """Starts the delivery bot background runner if DELIVERY_BOT_TOKEN is configured."""
    global _runner_instance
    tok = DELIVERY_BOT_TOKEN.strip() if DELIVERY_BOT_TOKEN else ""
    if not tok:
        logger.info("ℹ️ DELIVERY_BOT_TOKEN is not configured in .env. Bot running in DIRECT mode.")
        return None

    _runner_instance = DeliveryBotRunner(tok)
    return asyncio.create_task(_runner_instance.run())

