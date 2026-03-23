"""
backend/routers/websocket.py
─────────────────────────────
WebSocket endpoint that streams live trading data
to connected frontend clients.

Flow:
  1. Frontend connects to ws://localhost:8000/ws/{portfolio_id}
  2. Server sends full snapshot immediately on connect
  3. Server subscribes to Redis pub/sub channel
  4. Every time train.py publishes a signal, server
     pushes it to all connected clients instantly
  5. If client disconnects, server cleans up gracefully

Why this beats polling:
  - Polling: frontend asks "any new data?" every 1s
  - WebSocket: server pushes the moment data exists
  - Latency: 1000ms → ~50ms
  - Server load: constant requests → one persistent connection
"""

import asyncio
import json
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.auth import decode_access_token
from backend.models import User, Portfolio
from backend.services.redis_service import (
    get_all_signals,
    get_all_prices,
    get_portfolio_snapshot,
    get_all_chart_data,
    get_pubsub,
    is_bot_online,
    CHANNEL,
)

router = APIRouter(tags=["WebSocket"])


# ─────────────────────────────────────────────
#  Connection Manager
#  Tracks all active WebSocket connections.
#  When a new signal arrives, broadcasts to all.
# ─────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        # Set of active WebSocket connections
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)
        logging.info(f"[WS] Client connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)
        logging.info(f"[WS] Client disconnected. Total: {len(self.active)}")

    async def send(self, ws: WebSocket, data: dict):
        try:
            await ws.send_json(data)
        except Exception as e:
            logging.warning(f"[WS] Send failed: {e}")
            self.disconnect(ws)

    async def broadcast(self, data: dict):
        """Send data to all connected clients."""
        dead = set()
        for ws in self.active.copy():
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ─────────────────────────────────────────────
#  Build initial snapshot
#  Sent immediately when a client connects
#  so they see data instantly, not a blank screen
# ─────────────────────────────────────────────
def build_snapshot(portfolio_id: int) -> dict:
    return {
        "type":       "snapshot",
        "signals":    get_all_signals(),
        "prices":     get_all_prices(),
        "portfolio":  get_portfolio_snapshot(portfolio_id),
        "charts":     get_all_chart_data(),
        "bot_online": is_bot_online(),
    }


# ─────────────────────────────────────────────
#  WebSocket endpoint
#  URL: ws://localhost:8000/ws?token=<jwt>
#
#  Auth: JWT passed as query param because
#  browsers can't set Authorization headers
#  on WebSocket connections.
# ─────────────────────────────────────────────
@router.websocket("/ws")
async def websocket_endpoint(
    ws:    WebSocket,
    token: str = Query(...),
    db:    Session = Depends(get_db),
):
    # ── Authenticate ─────────────────────────
    payload = decode_access_token(token)
    if not payload:
        await ws.close(code=4001, reason="Invalid token")
        return

    user_id = int(payload.get("sub", 0))
    user    = db.query(User).filter(User.id == user_id).first()
    if not user:
        await ws.close(code=4001, reason="User not found")
        return

    portfolio = db.query(Portfolio).filter(
        Portfolio.user_id == user_id).first()
    if not portfolio:
        await ws.close(code=4002, reason="Portfolio not found")
        return

    portfolio_id = portfolio.id

    # ── Accept connection ─────────────────────
    await manager.connect(ws)

    # ── Send immediate snapshot ───────────────
    # Client gets full data the moment they connect
    await manager.send(ws, build_snapshot(portfolio_id))

    # ── Subscribe to Redis pub/sub ────────────
    # Run Redis listener in a thread since it's blocking
    pubsub   = get_pubsub()
    stop_evt = asyncio.Event()

    async def redis_listener():
        """
        Listens to Redis pub/sub channel in background.
        When train.py publishes a signal, pushes it
        to this client's WebSocket immediately.
        """
        loop = asyncio.get_event_loop()
        try:
            while not stop_evt.is_set():
                # get_message is non-blocking with timeout
                message = await loop.run_in_executor(
                    None,
                    lambda: pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                )
                if message and message.get("type") == "message":
                    try:
                        data = json.loads(message["data"])
                        # Enrich with portfolio-specific data
                        data["portfolio"] = get_portfolio_snapshot(portfolio_id)
                        data["bot_online"] = is_bot_online()
                        await manager.send(ws, data)
                    except Exception as e:
                        logging.warning(f"[WS] Message parse error: {e}")

                # Small sleep to prevent CPU spinning
                await asyncio.sleep(0.05)
        except Exception as e:
            logging.error(f"[WS] Redis listener error: {e}")
        finally:
            pubsub.unsubscribe(CHANNEL)
            pubsub.close()

    # Start Redis listener as background task
    listener_task = asyncio.create_task(redis_listener())

    # ── Keep connection alive ─────────────────
    # Wait for client to disconnect or send a ping
    try:
        while True:
            # Receive keeps connection alive
            # Client can send {"type": "ping"} for keepalive
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await manager.send(ws, {"type": "pong"})
                elif msg.get("type") == "refresh":
                    # Client requests full snapshot refresh
                    await manager.send(ws, build_snapshot(portfolio_id))
            except Exception:
                pass

    except WebSocketDisconnect:
        logging.info(f"[WS] User {user.username} disconnected")
    except Exception as e:
        logging.error(f"[WS] Unexpected error: {e}")
    finally:
        stop_evt.set()
        listener_task.cancel()
        manager.disconnect(ws)


# ─────────────────────────────────────────────
#  HTTP endpoint — current state snapshot
#  Useful for initial page load before WS connects
# ─────────────────────────────────────────────
@router.get("/api/snapshot/{portfolio_id}")
async def get_snapshot(portfolio_id: int):
    """
    REST fallback — returns current Redis state as JSON.
    Frontend can call this if WebSocket isn't available.
    """
    return build_snapshot(portfolio_id)