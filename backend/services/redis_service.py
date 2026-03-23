"""
backend/services/redis_service.py
───────────────────────────────────
All Redis operations for AlphaBot.

Key schema:
  price:{ticker}          → latest price (string)
  signal:{ticker}         → latest signal JSON
  portfolio:{id}          → portfolio snapshot JSON
  chart:{ticker}          → last 300 prices (Redis list)
  pending:portfolio:{id}  → pending trade IDs (Redis list)
  heartbeat               → last heartbeat timestamp
"""

import json
import logging
from datetime import datetime
from typing import Optional

import redis

# ─────────────────────────────────────────────
#  Connection
#  decode_responses=True → returns strings
#  instead of bytes, much easier to work with
# ─────────────────────────────────────────────
_client = redis.Redis(
    host            = "localhost",
    port            = 6379,
    db              = 0,
    decode_responses = True,
)


def get_client() -> redis.Redis:
    return _client


def ping() -> bool:
    try:
        return _client.ping()
    except Exception as e:
        logging.error(f"[Redis] ping failed: {e}")
        return False


# ─────────────────────────────────────────────
#  Price cache
#  train.py writes after every candle fetch.
#  Frontend reads via WebSocket.
#  TTL: 5 minutes — stale if bot crashes
# ─────────────────────────────────────────────
PRICE_TTL = 300   # 5 minutes


def set_price(ticker: str, price: float) -> None:
    try:
        _client.setex(f"price:{ticker}", PRICE_TTL, str(price))
    except Exception as e:
        logging.error(f"[Redis] set_price {ticker}: {e}")


def get_price(ticker: str) -> Optional[float]:
    try:
        val = _client.get(f"price:{ticker}")
        return float(val) if val else None
    except Exception as e:
        logging.error(f"[Redis] get_price {ticker}: {e}")
        return None


def get_all_prices() -> dict:
    """Returns {ticker: price} for all known tickers."""
    tickers = ["GC=F", "SPY", "BTC-USD"]
    result  = {}
    for t in tickers:
        price = get_price(t)
        if price is not None:
            result[t] = price
    return result


# ─────────────────────────────────────────────
#  Signal cache
#  Stores latest DRL signal per ticker.
#  TTL: 2 minutes
# ─────────────────────────────────────────────
SIGNAL_TTL = 120   # 2 minutes


def set_signal(
    ticker:      str,
    signal:      float,
    signal_std:  float,
    action:      str,
    price:       float,
    portfolio_value: float,
    trades_today: int,
    shares:      float,
) -> None:
    try:
        payload = {
            "ticker":          ticker,
            "signal":          round(signal, 4),
            "signal_std":      round(signal_std, 4),
            "action":          action,
            "price":           round(price, 2),
            "portfolio_value": round(portfolio_value, 2),
            "trades_today":    trades_today,
            "shares":          round(shares, 4),
            "timestamp":       datetime.utcnow().isoformat(),
        }
        _client.setex(
            f"signal:{ticker}",
            SIGNAL_TTL,
            json.dumps(payload),
        )
    except Exception as e:
        logging.error(f"[Redis] set_signal {ticker}: {e}")


def get_signal(ticker: str) -> Optional[dict]:
    try:
        val = _client.get(f"signal:{ticker}")
        return json.loads(val) if val else None
    except Exception as e:
        logging.error(f"[Redis] get_signal {ticker}: {e}")
        return None


def get_all_signals() -> dict:
    """Returns {ticker: signal_dict} for all tickers."""
    tickers = ["GC=F", "SPY", "BTC-USD"]
    result  = {}
    for t in tickers:
        sig = get_signal(t)
        if sig:
            result[t] = sig
    return result


# ─────────────────────────────────────────────
#  Portfolio snapshot cache
#  Stores combined portfolio state.
#  TTL: 10 minutes
# ─────────────────────────────────────────────
PORTFOLIO_TTL = 600   # 10 minutes


def set_portfolio_snapshot(
    portfolio_id: int,
    balance:      float,
    total_value:  float,
    shares:       dict,
    prices:       dict,
    trades_today: int,
) -> None:
    try:
        # Calculate per-ticker market value
        holdings = {
            ticker: {
                "shares":       round(qty, 4),
                "price":        round(prices.get(ticker, 0.0), 2),
                "market_value": round(qty * prices.get(ticker, 0.0), 2),
            }
            for ticker, qty in shares.items()
        }
        total_invested = sum(
            h["market_value"] for h in holdings.values()
        )
        payload = {
            "portfolio_id":  portfolio_id,
            "balance":       round(balance, 2),
            "total_value":   round(total_value, 2),
            "total_invested": round(total_invested, 2),
            "pnl":           round(total_value - 10_000_000, 2),
            "pnl_pct":       round((total_value - 10_000_000) / 10_000_000 * 100, 4),
            "holdings":      holdings,
            "trades_today":  trades_today,
            "timestamp":     datetime.utcnow().isoformat(),
        }
        _client.setex(
            f"portfolio:{portfolio_id}",
            PORTFOLIO_TTL,
            json.dumps(payload),
        )
    except Exception as e:
        logging.error(f"[Redis] set_portfolio_snapshot: {e}")


def get_portfolio_snapshot(portfolio_id: int) -> Optional[dict]:
    try:
        val = _client.get(f"portfolio:{portfolio_id}")
        return json.loads(val) if val else None
    except Exception as e:
        logging.error(f"[Redis] get_portfolio_snapshot: {e}")
        return None


# ─────────────────────────────────────────────
#  Chart data  (price history per ticker)
#  Uses Redis List — lpush adds to front,
#  ltrim keeps only last 300 points.
#  Frontend reads this for the price charts.
# ─────────────────────────────────────────────
CHART_MAX_POINTS = 300
CHART_TTL        = 3600   # 1 hour


def append_chart_point(ticker: str, price: float, timestamp: str) -> None:
    try:
        key   = f"chart:{ticker}"
        point = json.dumps({"price": round(price, 2), "time": timestamp})
        _client.lpush(key, point)
        _client.ltrim(key, 0, CHART_MAX_POINTS - 1)
        _client.expire(key, CHART_TTL)
    except Exception as e:
        logging.error(f"[Redis] append_chart_point {ticker}: {e}")


def get_chart_data(ticker: str) -> list:
    """
    Returns list of {price, time} dicts ordered oldest→newest.
    """
    try:
        key    = f"chart:{ticker}"
        raw    = _client.lrange(key, 0, -1)
        points = [json.loads(p) for p in raw]
        points.reverse()   # lpush stores newest first, reverse for chart
        return points
    except Exception as e:
        logging.error(f"[Redis] get_chart_data {ticker}: {e}")
        return []


def get_all_chart_data() -> dict:
    """Returns {ticker: [chart points]} for all tickers."""
    tickers = ["GC=F", "SPY", "BTC-USD"]
    return {t: get_chart_data(t) for t in tickers}


# ─────────────────────────────────────────────
#  Pub/Sub channel
#  train.py publishes to "alphabot:signals"
#  WebSocket handler subscribes and pushes
#  to all connected frontend clients.
# ─────────────────────────────────────────────
CHANNEL = "alphabot:signals"


def publish_signal(data: dict) -> None:
    """
    Publishes a signal event to the Redis pub/sub channel.
    Any connected WebSocket subscriber receives it instantly.
    """
    try:
        _client.publish(CHANNEL, json.dumps(data))
    except Exception as e:
        logging.error(f"[Redis] publish_signal: {e}")


def get_pubsub():
    """
    Returns a Redis PubSub object subscribed to the signals channel.
    Used by the WebSocket router to listen for new signals.
    """
    ps = _client.pubsub()
    ps.subscribe(CHANNEL)
    return ps


# ─────────────────────────────────────────────
#  Heartbeat
#  train.py writes every 60s.
#  Frontend uses this to show "bot online/offline"
# ─────────────────────────────────────────────
def set_heartbeat() -> None:
    try:
        _client.setex("heartbeat", 90, datetime.utcnow().isoformat())
    except Exception as e:
        logging.error(f"[Redis] set_heartbeat: {e}")


def get_heartbeat() -> Optional[str]:
    try:
        return _client.get("heartbeat")
    except Exception as e:
        logging.error(f"[Redis] get_heartbeat: {e}")
        return None


def is_bot_online() -> bool:
    """Returns True if heartbeat was set within last 90 seconds."""
    return get_heartbeat() is not None