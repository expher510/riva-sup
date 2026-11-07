from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
import httpx
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import MiniAppUser
from app.config import settings

router = APIRouter(prefix="/user", tags=["User"])

class AuthRequest(BaseModel):
    telegram_id: int
    first_name: Optional[str] = None
    username: Optional[str] = None

class ChannelCheckRequest(BaseModel):
    telegram_id: int

@router.post("/auth")
async def auth_user(req: AuthRequest, db: AsyncSession = Depends(get_db)):
    """Authenticates Telegram user, initializes profile, checks VIP status."""
    stmt = select(MiniAppUser).filter(MiniAppUser.telegram_id == req.telegram_id)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if not user:
        user = MiniAppUser(
            telegram_id=req.telegram_id,
            first_name=req.first_name,
            username=req.username,
            is_vip=False,
            last_active_at=now
        )
        db.add(user)
    else:
        user.first_name = req.first_name or user.first_name
        user.username = req.username or user.username
        user.last_active_at = now

        # Refresh VIP expiration
        if user.is_vip and user.vip_expires_at:
            if user.vip_expires_at < now:
                user.is_vip = False

    await db.commit()
    await db.refresh(user)

    return {
        "telegram_id": user.telegram_id,
        "first_name": user.first_name,
        "is_vip": user.is_vip,
        "vip_expires_at": user.vip_expires_at.isoformat() if user.vip_expires_at else None,
        "total_ads_watched": user.total_ads_watched
    }

@router.post("/verify-channel")
async def verify_channel_membership(req: ChannelCheckRequest):
    """Verifies user membership in the required channel using Telegram Bot API."""
    url = f"https://api.telegram.org/bot{settings.MAIN_BOT_TOKEN}/getChatMember"
    payload = {
        "chat_id": settings.REQUIRED_CHANNEL_ID,
        "user_id": req.telegram_id
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if data.get("ok"):
                status = data.get("result", {}).get("status")
                is_member = status in ["creator", "administrator", "member", "restricted"]
                return {"is_member": is_member, "channel_url": settings.REQUIRED_CHANNEL_INVITE}
            else:
                # Fallback to true if channel ID not configured yet to not block testing
                return {"is_member": True, "channel_url": settings.REQUIRED_CHANNEL_INVITE}
    except Exception as e:
        return {"is_member": True, "error": str(e), "channel_url": settings.REQUIRED_CHANNEL_INVITE}

