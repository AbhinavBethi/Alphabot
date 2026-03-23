"""
routers/portfolio.py
─────────────────────
Portfolio endpoints (all protected by JWT):
  GET  /me/portfolio           → view portfolio + stocks + balance
  POST /me/portfolio/stocks    → add a stock (GC=F / SPY / BTC-USD)
  DELETE /me/portfolio/stocks/{ticker} → remove a stock
  GET  /me/trades              → full trade history
  GET  /me/pending             → pending trades awaiting approval
  POST /me/pending/{id}/resolve → approve or reject a pending trade
  PATCH /me/portfolio/settings → toggle auto_trade on/off
"""

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import (
    User, Portfolio, PortfolioStock,
    Trade, PendingTrade, TradeAction, PendingStatus
)
from backend.schemas import (
    PortfolioResponse, PortfolioStockResponse,
    AddStockRequest, TradeResponse,
    PendingTradeResponse, ResolvePendingRequest,
    UpdateAutoTradeRequest,
)
from backend.auth import get_current_user

router = APIRouter(prefix="/me", tags=["Portfolio"])

ALLOWED_TICKERS = {"GC=F", "SPY", "BTC-USD"}


# ─────────────────────────────────────────────
#  Helper — get portfolio or raise 404
# ─────────────────────────────────────────────
def _get_portfolio(user: User, db: Session) -> Portfolio:
    portfolio = db.query(Portfolio).filter(Portfolio.user_id == user.id).first()
    if not portfolio:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Portfolio not found. Please re-register.",
        )
    return portfolio


# ─────────────────────────────────────────────
#  GET /me/portfolio
# ─────────────────────────────────────────────
@router.get("/portfolio", response_model=PortfolioResponse)
def get_portfolio(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Returns the user's portfolio including:
    - Available cash balance
    - All stocks being tracked
    - Shares held per stock
    - P&L vs initial balance
    """
    portfolio = _get_portfolio(current_user, db)

    # Calculate total invested (shares * avg_buy_price)
    total_invested = sum(
        s.shares_held * s.avg_buy_price
        for s in portfolio.stocks
    )
    pnl     = portfolio.balance + total_invested - portfolio.initial_balance
    pnl_pct = (pnl / portfolio.initial_balance) * 100 if portfolio.initial_balance else 0

    response             = PortfolioResponse.model_validate(portfolio)
    response.total_invested = total_invested
    response.pnl         = round(pnl, 2)
    response.pnl_pct     = round(pnl_pct, 4)
    return response


# ─────────────────────────────────────────────
#  POST /me/portfolio/stocks
# ─────────────────────────────────────────────
@router.post("/portfolio/stocks", response_model=PortfolioStockResponse, status_code=201)
def add_stock(
    payload:      AddStockRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Add a stock to the user's portfolio.
    Only GC=F, SPY, BTC-USD are allowed.
    A stock can only be added once per portfolio.
    """
    portfolio = _get_portfolio(current_user, db)

    # Check not already added
    existing = db.query(PortfolioStock).filter(
        PortfolioStock.portfolio_id == portfolio.id,
        PortfolioStock.ticker       == payload.ticker,
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{payload.ticker} is already in your portfolio.",
        )

    stock = PortfolioStock(
        portfolio_id  = portfolio.id,
        ticker        = payload.ticker,
        shares_held   = 0.0,
        avg_buy_price = 0.0,
    )
    db.add(stock)
    db.commit()
    db.refresh(stock)
    return stock


# ─────────────────────────────────────────────
#  DELETE /me/portfolio/stocks/{ticker}
# ─────────────────────────────────────────────
@router.delete("/portfolio/stocks/{ticker}", status_code=204)
def remove_stock(
    ticker:       str,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Remove a stock from the portfolio (only if no shares held)."""
    portfolio = _get_portfolio(current_user, db)

    stock = db.query(PortfolioStock).filter(
        PortfolioStock.portfolio_id == portfolio.id,
        PortfolioStock.ticker       == ticker.upper(),
    ).first()

    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found in portfolio.")

    if stock.shares_held > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot remove {ticker} while holding {stock.shares_held:.4f} shares. Sell first.",
        )

    db.delete(stock)
    db.commit()


# ─────────────────────────────────────────────
#  GET /me/trades
# ─────────────────────────────────────────────
@router.get("/trades", response_model=List[TradeResponse])
def get_trades(
    limit:        int     = 100,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Returns the last N executed trades, newest first."""
    portfolio = _get_portfolio(current_user, db)

    trades = (
        db.query(Trade)
        .filter(Trade.portfolio_id == portfolio.id)
        .order_by(Trade.timestamp.desc())
        .limit(limit)
        .all()
    )
    return trades


# ─────────────────────────────────────────────
#  GET /me/pending
# ─────────────────────────────────────────────
@router.get("/pending", response_model=List[PendingTradeResponse])
def get_pending_trades(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Returns all PENDING trades waiting for user approval."""
    portfolio = _get_portfolio(current_user, db)

    pending = (
        db.query(PendingTrade)
        .filter(
            PendingTrade.portfolio_id == portfolio.id,
            PendingTrade.status       == PendingStatus.PENDING,
        )
        .order_by(PendingTrade.created_at.desc())
        .all()
    )
    return pending


# ─────────────────────────────────────────────
#  POST /me/pending/{id}/resolve
# ─────────────────────────────────────────────
@router.post("/pending/{trade_id}/resolve", response_model=PendingTradeResponse)
def resolve_pending_trade(
    trade_id:     int,
    payload:      ResolvePendingRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Approve or reject a pending trade.
    If approved → creates an executed Trade record + updates portfolio balance.
    If rejected → marks as REJECTED, no balance change.
    """
    portfolio = _get_portfolio(current_user, db)

    pending = db.query(PendingTrade).filter(
        PendingTrade.id           == trade_id,
        PendingTrade.portfolio_id == portfolio.id,
    ).first()

    if not pending:
        raise HTTPException(status_code=404, detail="Pending trade not found.")

    if pending.status != PendingStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Trade already {pending.status.value.lower()}.",
        )

    # Check if expired
    if datetime.utcnow() > pending.expires_at:
        pending.status      = PendingStatus.EXPIRED
        pending.resolved_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=400, detail="This trade has expired.")

    if payload.action == "approve":
        cost = pending.quantity * pending.price

        if pending.action == TradeAction.BUY:
            if portfolio.balance < cost:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient balance. Need ${cost:,.2f}, have ${portfolio.balance:,.2f}",
                )
            # Deduct balance
            portfolio.balance -= cost

            # Update shares held + avg buy price
            stock = db.query(PortfolioStock).filter(
                PortfolioStock.portfolio_id == portfolio.id,
                PortfolioStock.ticker       == pending.ticker,
            ).first()
            if stock:
                total_cost       = stock.shares_held * stock.avg_buy_price + cost
                stock.shares_held   += pending.quantity
                stock.avg_buy_price  = total_cost / stock.shares_held

        elif pending.action == TradeAction.SELL:
            stock = db.query(PortfolioStock).filter(
                PortfolioStock.portfolio_id == portfolio.id,
                PortfolioStock.ticker       == pending.ticker,
            ).first()
            if not stock or stock.shares_held < pending.quantity:
                raise HTTPException(status_code=400, detail="Insufficient shares to sell.")
            stock.shares_held -= pending.quantity
            portfolio.balance += pending.quantity * pending.price

        # Record executed trade
        trade = Trade(
            portfolio_id = portfolio.id,
            ticker       = pending.ticker,
            action       = pending.action,
            price        = pending.price,
            quantity     = pending.quantity,
            signal_value = pending.signal_value,
            total_value  = portfolio.balance,
        )
        db.add(trade)
        pending.status      = PendingStatus.APPROVED
        pending.resolved_at = datetime.utcnow()

    else:  # reject
        pending.status      = PendingStatus.REJECTED
        pending.resolved_at = datetime.utcnow()

    db.commit()
    db.refresh(pending)
    return pending


# ─────────────────────────────────────────────
#  PATCH /me/portfolio/settings
# ─────────────────────────────────────────────
@router.patch("/portfolio/settings", response_model=PortfolioResponse)
def update_settings(
    payload:      UpdateAutoTradeRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Toggle auto_trade on or off for the user's portfolio."""
    portfolio             = _get_portfolio(current_user, db)
    portfolio.auto_trade  = payload.auto_trade
    db.commit()
    db.refresh(portfolio)
    return portfolio