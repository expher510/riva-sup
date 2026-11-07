from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import catalog, user, watch, payment, requests
from app.config import settings

app = FastAPI(
    title="Riva Cinema Telegram Mini App API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS Middleware (allows Telegram WebApp origins)
origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Routes
app.include_router(catalog.router, prefix="/api/v1")
app.include_router(user.router, prefix="/api/v1")
app.include_router(watch.router, prefix="/api/v1")
app.include_router(payment.router, prefix="/api/v1")
app.include_router(requests.router, prefix="/api/v1")

@app.get("/health")
async def health():
    return {"status": "ok", "app": "Riva Cinema MiniApp API", "env": settings.ENVIRONMENT}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

