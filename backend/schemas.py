"""
schemas.py
──────────
Pydantic models for request validation and response serialization.
These are what FastAPI uses to:
  - Validate incoming JSON from the frontend
  - Shape the JSON responses sent back
"""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr, field_validator
from backend.models import TradeAction, PendingStatus


# ─────────────────────────────────────────────
#  Auth schemas
# ─────────────────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    email:    EmailStr
    password: str

    @field_validator("username")
    @classmethod
    def username_valid(cls, v):
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        if len(v) > 50:
            raise ValueError("Username must be under 50 characters")
        return v

    @field_validator("password")
    @classmethod
    def password_valid(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    username:     str


# ─────────────────────────────────────────────
#  User schemas
# ─────────────────────────────────────────────
class UserResponse(BaseModel):
    id:         int
    username:   str
    email:      str
    created_at: datetime

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
#  Portfolio Stock schemas
# ─────────────────────────────────────────────

# Allowed tickers — strictly enforced
ALLOWED_TICKERS = {"GC=F", "SPY", "BTC-USD"}

class AddStockRequest(BaseModel):
    ticker: str

    @field_validator("ticker")
    @classmethod
    def ticker_allowed(cls, v):
        v = v.upper().strip()
        # BTC-USD keeps its hyphen so handle case
        v = v.replace("BTC_USD", "BTC-USD")
        if v not in ALLOWED_TICKERS:
            raise ValueError(
                f"Ticker must be one of: {', '.join(ALLOWED_TICKERS)}"
            )
        return v


class PortfolioStockResponse(BaseModel):
    id:            int
    ticker:        str
    shares_held:   float
    avg_buy_price: float
    added_at:      datetime

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
#  Portfolio schemas
# ─────────────────────────────────────────────
class PortfolioResponse(BaseModel):
    id:              int
    balance:         float
    initial_balance: float
    auto_trade:      bool
    created_at:      datetime
    stocks:          List[PortfolioStockResponse] = []

    # Computed fields — calculated at response time
    total_invested:  float = 0.0
    pnl:             float = 0.0          # profit / loss vs initial balance
    pnl_pct:         float = 0.0

    class Config:
        from_attributes = True


class UpdateAutoTradeRequest(BaseModel):
    auto_trade: bool


# ─────────────────────────────────────────────
#  Trade schemas
# ─────────────────────────────────────────────
class TradeResponse(BaseModel):
    id:           int
    ticker:       str
    action:       TradeAction
    price:        float
    quantity:     float
    signal_value: Optional[float]
    total_value:  Optional[float]
    timestamp:    datetime

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
#  Pending Trade schemas
# ─────────────────────────────────────────────
class PendingTradeResponse(BaseModel):
    id:           int
    ticker:       str
    action:       TradeAction
    price:        float
    quantity:     float
    signal_value: Optional[float]
    status:       PendingStatus
    expires_at:   datetime
    created_at:   datetime

    class Config:
        from_attributes = True


class ResolvePendingRequest(BaseModel):
    """Frontend sends this when user approves or rejects a pending trade."""
    action: str   # "approve" or "reject"

    @field_validator("action")
    @classmethod
    def action_valid(cls, v):
        if v not in ("approve", "reject"):
            raise ValueError("action must be 'approve' or 'reject'")
        return v


# ─────────────────────────────────────────────
#  Analytics schemas  (used in Stage 3)
# ─────────────────────────────────────────────
class PortfolioSummary(BaseModel):
    total_value:     float
    cash:            float
    invested:        float
    pnl:             float
    pnl_pct:         float
    total_trades:    int
    winning_trades:  int
    win_rate:        float
    sharpe_ratio:    float