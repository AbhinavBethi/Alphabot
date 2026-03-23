"""
backend/routers/controls.py
────────────────────────────
Circuit breaker endpoints:
  PATCH /controls/pause          → pause all trade execution
  PATCH /controls/resume         → resume trading
  POST  /controls/emergency-stop → sell all positions + pause
  GET   /controls/status         → current pause state + positions

The trading_paused flag lives in the portfolios table.
train.py checks this flag before writing any pending trade.
This means the pause survives server restarts — it's in the DB.
"""

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.database import get_db
from backend.auth import get_current_user
from backend.models import User, Portfolio, PortfolioStock, Trade, TradeAction
from backend.services.redis_service import get_all_prices

router = APIRouter(prefix="/controls", tags=["Controls"])


# ─────────────────────────────────────────────
#  Response schemas
# ─────────────────────────────────────────────
class ControlStatus(BaseModel):
    trading_paused: bool
    auto_trade:     bool
    positions:      dict        # {ticker: {shares, market_value, avg_buy_price}}
    total_invested: float
    message:        str


class EmergencyStopResult(BaseModel):
    trades_executed: int
    total_sold:      float
    cash_recovered:  float
    message:         str


# ─────────────────────────────────────────────
#  Helper
# ─────────────────────────────────────────────
def _get_portfolio(user: User, db: Session) -> Portfolio:
    p = db.query(Portfolio).filter(Portfolio.user_id == user.id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return p


# ─────────────────────────────────────────────
#  GET /controls/status
# ─────────────────────────────────────────────
@router.get("/status", response_model=ControlStatus)
def get_control_status(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Returns current trading state and open positions."""
    portfolio = _get_portfolio(current_user, db)
    prices    = get_all_prices()

    positions = {}
    total_invested = 0.0

    for stock in portfolio.stocks:
        market_price = prices.get(stock.ticker, 0.0)
        market_value = stock.shares_held * market_price
        positions[stock.ticker] = {
            'shares':        round(stock.shares_held, 4),
            'avg_buy_price': round(stock.avg_buy_price, 2),
            'market_price':  round(market_price, 2),
            'market_value':  round(market_value, 2),
        }
        total_invested += market_value

    paused  = getattr(portfolio, 'trading_paused', False)
    message = "Trading is paused." if paused else "Trading is active."

    return ControlStatus(
        trading_paused = paused,
        auto_trade     = portfolio.auto_trade,
        positions      = positions,
        total_invested = round(total_invested, 2),
        message        = message,
    )


# ─────────────────────────────────────────────
#  PATCH /controls/pause
# ─────────────────────────────────────────────
@router.patch("/pause", response_model=ControlStatus)
def pause_trading(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Pause trading — bot continues running and generating
    signals but no pending trades are created or executed.
    Persists in DB so it survives restarts.
    """
    portfolio = _get_portfolio(current_user, db)

    if getattr(portfolio, 'trading_paused', False):
        raise HTTPException(status_code=400, detail="Trading is already paused.")

    portfolio.trading_paused = True
    db.commit()

    return ControlStatus(
        trading_paused = True,
        auto_trade     = portfolio.auto_trade,
        positions      = {},
        total_invested = 0.0,
        message        = "Trading paused. Bot will continue monitoring but no trades will execute.",
    )


# ─────────────────────────────────────────────
#  PATCH /controls/resume
# ─────────────────────────────────────────────
@router.patch("/resume", response_model=ControlStatus)
def resume_trading(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Resume trading after a pause."""
    portfolio = _get_portfolio(current_user, db)

    if not getattr(portfolio, 'trading_paused', False):
        raise HTTPException(status_code=400, detail="Trading is not paused.")

    portfolio.trading_paused = False
    db.commit()

    return ControlStatus(
        trading_paused = False,
        auto_trade     = portfolio.auto_trade,
        positions      = {},
        total_invested = 0.0,
        message        = "Trading resumed. Bot will start executing signals again.",
    )


# ─────────────────────────────────────────────
#  POST /controls/emergency-stop
#  Sells ALL positions at current market price
#  then pauses trading.
# ─────────────────────────────────────────────
@router.post("/emergency-stop", response_model=EmergencyStopResult)
def emergency_stop(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Circuit breaker — immediately:
    1. Sells all open positions at current market price
    2. Credits proceeds to portfolio balance
    3. Pauses trading (user must manually resume)

    This is the kill switch. Use when you want to exit
    all positions and stop the bot immediately.
    """
    portfolio = _get_portfolio(current_user, db)
    prices    = get_all_prices()

    trades_executed = 0
    total_sold      = 0.0
    cash_recovered  = 0.0

    for stock in portfolio.stocks:
        if stock.shares_held <= 0:
            continue

        market_price = prices.get(stock.ticker, stock.avg_buy_price)
        if market_price <= 0:
            # Fallback to avg buy price if no live price available
            market_price = stock.avg_buy_price

        proceeds = stock.shares_held * market_price

        # Record the emergency sell trade
        trade = Trade(
            portfolio_id = portfolio.id,
            ticker       = stock.ticker,
            action       = TradeAction.SELL,
            price        = market_price,
            quantity     = stock.shares_held,
            signal_value = None,
            total_value  = portfolio.balance + proceeds,
            timestamp    = datetime.utcnow(),
        )
        db.add(trade)

        # Update portfolio
        portfolio.balance += proceeds
        total_sold        += stock.shares_held
        cash_recovered    += proceeds
        trades_executed   += 1

        # Zero out position
        stock.shares_held   = 0.0
        stock.avg_buy_price = 0.0

    # Pause trading
    portfolio.trading_paused = True
    db.commit()

    return EmergencyStopResult(
        trades_executed = trades_executed,
        total_sold      = round(total_sold, 4),
        cash_recovered  = round(cash_recovered, 2),
        message         = (
            f"Emergency stop executed. Sold {trades_executed} position(s), "
            f"recovered ${cash_recovered:,.2f}. Trading is now paused."
        ),
    )