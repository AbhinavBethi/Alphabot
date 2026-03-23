"""
backend/models.py
─────────────────
SQLAlchemy ORM models — one class per DB table.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, Float, String, Boolean,
    DateTime, ForeignKey, Enum
)
from sqlalchemy.orm import relationship
import enum

from backend.database import Base


class TradeAction(str, enum.Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class PendingStatus(str, enum.Enum):
    PENDING  = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED  = "EXPIRED"


class TickerChoice(str, enum.Enum):
    GOLD    = "GC=F"
    SPY     = "SPY"
    BITCOIN = "BTC-USD"


class User(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True, index=True)
    username      = Column(String(50),  unique=True, nullable=False, index=True)
    email         = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    portfolio     = relationship("Portfolio", back_populates="user", uselist=False)


class Portfolio(Base):
    __tablename__ = "portfolios"
    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    balance         = Column(Float, default=10_000_000.0)
    initial_balance = Column(Float, default=10_000_000.0)
    auto_trade      = Column(Boolean, default=True)
    trading_paused  = Column(Boolean, default=False)   # ← circuit breaker
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user            = relationship("User",           back_populates="portfolio")
    stocks          = relationship("PortfolioStock", back_populates="portfolio", cascade="all, delete-orphan")
    trades          = relationship("Trade",          back_populates="portfolio", cascade="all, delete-orphan")
    pending_trades  = relationship("PendingTrade",   back_populates="portfolio", cascade="all, delete-orphan")


class PortfolioStock(Base):
    __tablename__ = "portfolio_stocks"
    id            = Column(Integer, primary_key=True, index=True)
    portfolio_id  = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    ticker        = Column(String(20), nullable=False)
    shares_held   = Column(Float, default=0.0)
    avg_buy_price = Column(Float, default=0.0)
    added_at      = Column(DateTime, default=datetime.utcnow)
    portfolio     = relationship("Portfolio", back_populates="stocks")


class Trade(Base):
    __tablename__ = "trades"
    id           = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    ticker       = Column(String(20), nullable=False)
    action       = Column(Enum(TradeAction), nullable=False)
    price        = Column(Float, nullable=False)
    quantity     = Column(Float, nullable=False)
    signal_value = Column(Float, nullable=True)
    total_value  = Column(Float, nullable=True)
    timestamp    = Column(DateTime, default=datetime.utcnow)
    portfolio    = relationship("Portfolio", back_populates="trades")


class PendingTrade(Base):
    __tablename__ = "pending_trades"
    id           = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    ticker       = Column(String(20), nullable=False)
    action       = Column(Enum(TradeAction), nullable=False)
    price        = Column(Float, nullable=False)
    quantity     = Column(Float, nullable=False)
    signal_value = Column(Float, nullable=True)
    status       = Column(Enum(PendingStatus), default=PendingStatus.PENDING)
    expires_at   = Column(DateTime, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)
    resolved_at  = Column(DateTime, nullable=True)
    portfolio    = relationship("Portfolio", back_populates="pending_trades")