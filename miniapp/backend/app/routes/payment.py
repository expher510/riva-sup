from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import json
import httpx

from app.database import get_db
from app.models import MiniAppUser, MiniAppSubscription
from app.config import settings

router = APIRouter(prefix="/payment", tags=["Payment"])

class CreateInvoiceRequest(BaseModel):
    telegram_id: int

class ConfirmPaymentRequest(BaseModel):
    telegram_id: int
    charge_id: str

@router.post("/create-stars-invoice")
async def create_stars_invoice(req: CreateInvoiceRequest):
    """Generates a native 100 Telegram Stars invoice link (XTR) for Telegram Mini App."""
    url = f"https://api.telegram.org/bot{settings.MAIN_BOT_TOKEN}/createInvoiceLink"
    payload = {
        "title": "👑 باقة ريفا سينما VIP (30 يوم)",
        "description": "مشاهدة لا محدودة لجميع الأفلام والمسلسلات بدون إعلانات نهائياً لمدة شهر كامل.",
        "payload": json.dumps({"user_id": req.telegram_id, "plan": "vip_30d", "stars": settings.VIP_STARS_PRICE}),
        "currency": "XTR",
        "prices": [{"label": "اشتراك 30 يوم بدون إعلانات", "amount": settings.VIP_STARS_PRICE}],
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise HTTPException(status_code=500, detail=f"Failed to create Stars invoice: {data.get('description')}")
        return {"invoice_link": data["result"], "stars": settings.VIP_STARS_PRICE}

@router.post("/confirm-payment")
async def confirm_payment(req: ConfirmPaymentRequest, db: AsyncSession = Depends(get_db)):
    """Activates 30-day VIP status upon successful Stars checkout."""
    user_stmt = select(MiniAppUser).filter(MiniAppUser.telegram_id == req.telegram_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()

    if not user:
        user = MiniAppUser(telegram_id=req.telegram_id, is_vip=True)
        db.add(user)

    now = datetime.now(timezone.utc)
    user.is_vip = True
    user.vip_expires_at = now + timedelta(days=settings.VIP_DURATION_DAYS)

    sub = MiniAppSubscription(
        telegram_id=req.telegram_id,
        stars_amount=settings.VIP_STARS_PRICE,
        payment_method="telegram_stars",
        charge_id=req.charge_id,
        status="completed",
        starts_at=now,
        expires_at=user.vip_expires_at
    )
    db.add(sub)
    await db.commit()

    return {
        "success": True,
        "is_vip": True,
        "expires_at": user.vip_expires_at.isoformat(),
        "message": "🎉 مبروك! تم تفعيل اشتراك VIP بنجاح، استمتع بجميع الأعمال بدون إعلانات."
    }

