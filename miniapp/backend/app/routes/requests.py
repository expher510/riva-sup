from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional
from app.database import get_db
from app.models import MiniAppRequest

router = APIRouter(prefix="/requests", tags=["Requests"])

class NewRequestPayload(BaseModel):
    telegram_id: int
    title: str
    type: str = "movie"
    notes: Optional[str] = None

@router.post("/submit")
async def submit_request(payload: NewRequestPayload, db: AsyncSession = Depends(get_db)):
    """Saves user media request from 'صفحة الطلبات'."""
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="العنوان مطلوب")

    req = MiniAppRequest(
        telegram_id=payload.telegram_id,
        title=payload.title.strip(),
        type=payload.type,
        notes=payload.notes,
        status="pending"
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)

    return {
        "success": True,
        "request_id": req.id,
        "message": "✅ تم استلام طلبك بنجاح! سيتم فحص العمل وتوفيره في أقرب وقت ممكن."
    }

