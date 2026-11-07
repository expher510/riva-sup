from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
import httpx

from app.database import get_db
from app.models import MiniAppUser, TelegramArchive, Movie, Episode
from app.config import settings

router = APIRouter(prefix="/watch", tags=["Watch"])

class PlayRequest(BaseModel):
    telegram_id: int
    media_type: str # 'movie' or 'episode'
    item_id: int    # movie_id or episode_id

async def _deliver_video_to_chat(telegram_id: int, channel_id: int, message_id: int) -> bool:
    """Uses delivery bot (@Rivaa1_bot) to copyMessage video from Delivery Vault to user."""
    url = f"https://api.telegram.org/bot{settings.DELIVERY_BOT_TOKEN}/copyMessage"
    payload = {
        "chat_id": telegram_id,
        "from_chat_id": channel_id,
        "message_id": message_id
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload)
        res_data = resp.json()
        return bool(res_data.get("ok"))

@router.post("/request-play")
async def request_play(req: PlayRequest, db: AsyncSession = Depends(get_db)):
    """Handles watch intent. VIP users receive instant video; free users are directed to watch an ad."""
    # 1. Fetch user
    user_stmt = select(MiniAppUser).filter(MiniAppUser.telegram_id == req.telegram_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    is_vip = user and user.is_vip and (not user.vip_expires_at or user.vip_expires_at > now)

    # 2. Find archive in Delivery Vault
    arch_stmt = select(TelegramArchive)
    if req.media_type == "movie":
        arch_stmt = arch_stmt.filter(TelegramArchive.movie_id == req.item_id)
    else:
        arch_stmt = arch_stmt.filter(TelegramArchive.episode_id == req.item_id)

    # Prefer delivery vault channel
    arch_stmt = arch_stmt.order_by(desc(TelegramArchive.id))
    arch_res = await db.execute(arch_stmt)
    archives = arch_res.scalars().all()

    if not archives:
        raise HTTPException(status_code=404, detail="عفواً، الفيديو قيد المعالجة وسيتم رفعه قريباً جداً.")

    # Select the archive from delivery vault or fallback to primary
    target_arch = next((a for a in archives if str(a.channel_id) == str(settings.DELIVERY_VAULT_CHANNEL_ID)), archives[0])

    if is_vip:
        # Deliver instantly to chat
        delivered = await _deliver_video_to_chat(req.telegram_id, target_arch.channel_id, target_arch.message_id)
        return {
            "status": "delivered",
            "is_vip": True,
            "message": "👑 أنت مشترك مميز! تم إرسال الفيديو إلى محادثتك فوراً للمشاهدة بدون إعلانات."
        }
    else:
        # Require Adsgram rewarded ad
        return {
            "status": "ad_required",
            "is_vip": False,
            "adsgram_block_id": settings.ADSGRAM_BLOCK_ID,
            "stars_price": settings.VIP_STARS_PRICE,
            "message": "لمساعدتنا في تقديم الخدمة مجاناً شاهد إعلان واحد وهيصلك الفيديو."
        }

@router.post("/ad-completed")
async def ad_completed(req: PlayRequest, db: AsyncSession = Depends(get_db)):
    """Called when user successfully finishes watching a rewarded video ad."""
    # 1. Increment ad count
    user_stmt = select(MiniAppUser).filter(MiniAppUser.telegram_id == req.telegram_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if user:
        user.total_ads_watched = (user.total_ads_watched or 0) + 1
        await db.commit()

    # 2. Find archive
    arch_stmt = select(TelegramArchive)
    if req.media_type == "movie":
        arch_stmt = arch_stmt.filter(TelegramArchive.movie_id == req.item_id)
    else:
        arch_stmt = arch_stmt.filter(TelegramArchive.episode_id == req.item_id)

    arch_stmt = arch_stmt.order_by(desc(TelegramArchive.id))
    arch_res = await db.execute(arch_stmt)
    archives = arch_res.scalars().all()

    if not archives:
        raise HTTPException(status_code=404, detail="الفيديو غير متوفر حالياً.")

    target_arch = next((a for a in archives if str(a.channel_id) == str(settings.DELIVERY_VAULT_CHANNEL_ID)), archives[0])

    # 3. Deliver to user chat
    delivered = await _deliver_video_to_chat(req.telegram_id, target_arch.channel_id, target_arch.message_id)
    return {
        "status": "delivered",
        "delivered": delivered,
        "message": "🍿 شكرًا لمشاهدة الإعلان! تم إرسال الفيديو بجودة عالية إلى محادثتك الآن، استمتع بالمشاهدة!"
    }

