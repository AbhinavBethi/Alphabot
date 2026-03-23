"""
api.py — AlphaBot API entry point
Run: python -m uvicorn api:app --reload
"""

from pathlib import Path
from typing import List

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.database import engine, Base, get_db
from backend.auth import get_current_user
from backend.models import User

from backend.routers.auth      import router as auth_router
from backend.routers.portfolio import router as portfolio_router
from backend.routers.websocket import router as ws_router
from backend.routers.analytics import router as analytics_router
from backend.routers.controls  import router as controls_router

# Create / migrate tables on startup
Base.metadata.create_all(bind=engine)

LOG_FILE     = Path("realtime_trading.log")
FRONTEND_DIR = Path("frontend")

app = FastAPI(
    title       = "AlphaBot API",
    description = "Real-time multi-ticker DRL trading bot with circuit breaker",
    version     = "5.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(portfolio_router)
app.include_router(ws_router)
app.include_router(analytics_router)
app.include_router(controls_router)


@app.get("/health")
async def health():
    from backend.services.redis_service import ping as redis_ping, is_bot_online
    return {
        "status":     "ok",
        "redis":      "online" if redis_ping() else "offline",
        "bot_online": is_bot_online(),
    }


@app.get("/logs")
async def get_logs(limit: int = 200) -> dict:
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        with LOG_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return {"lines": []}
    return {"lines": [l.rstrip("\n") for l in lines[-limit:]]}


@app.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "username": current_user.username, "email": current_user.email}


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")