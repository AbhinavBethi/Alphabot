"""
backend/routers/analytics.py
─────────────────────────────
Analytics endpoints:
  GET /analytics/summary  → Sharpe, win rate, drawdown, best/worst trade
  GET /analytics/tickers  → Per-ticker breakdown
"""

import numpy as np
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.database import get_db
from backend.auth import get_current_user
from backend.models import User, Portfolio, Trade, TradeAction

router = APIRouter(prefix="/analytics", tags=["Analytics"])


class AnalyticsSummary(BaseModel):
    total_trades:    int
    winning_trades:  int
    losing_trades:   int
    win_rate:        float
    sharpe_ratio:    float
    max_drawdown:    float
    best_trade_pnl:  float
    worst_trade_pnl: float
    avg_trade_pnl:   float
    total_pnl:       float
    total_pnl_pct:   float
    most_traded:     Optional[str]


class TickerStats(BaseModel):
    ticker:        str
    total_trades:  int
    buy_trades:    int
    sell_trades:   int
    total_volume:  float
    avg_signal:    float


def _get_portfolio(user: User, db: Session) -> Portfolio:
    p = db.query(Portfolio).filter(Portfolio.user_id == user.id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return p


@router.get("/summary", response_model=AnalyticsSummary)
def get_analytics_summary(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    portfolio = _get_portfolio(current_user, db)
    trades = (
        db.query(Trade)
        .filter(Trade.portfolio_id == portfolio.id)
        .order_by(Trade.timestamp.asc())
        .all()
    )

    if not trades:
        return AnalyticsSummary(
            total_trades=0, winning_trades=0, losing_trades=0,
            win_rate=0.0, sharpe_ratio=0.0, max_drawdown=0.0,
            best_trade_pnl=0.0, worst_trade_pnl=0.0, avg_trade_pnl=0.0,
            total_pnl=0.0, total_pnl_pct=0.0, most_traded=None,
        )

    values  = [t.total_value for t in trades if t.total_value and t.total_value > 0]
    returns = []
    if len(values) > 1:
        for i in range(1, len(values)):
            if values[i-1] > 0:
                returns.append((values[i] - values[i-1]) / values[i-1])

    sharpe = 0.0
    if len(returns) > 1:
        mean_r = np.mean(returns)
        std_r  = np.std(returns) + 1e-10
        sharpe = float(np.clip((mean_r / std_r) * np.sqrt(525_600), -99, 99))

    max_drawdown = 0.0
    if values:
        peak = values[0]
        for v in values:
            if v > peak: peak = v
            dd = (peak - v) / (peak + 1e-10) * 100
            if dd > max_drawdown: max_drawdown = dd

    buy_prices, buy_qtys, trade_pnls = {}, {}, []
    for t in trades:
        if t.action == TradeAction.BUY:
            if t.ticker not in buy_prices:
                buy_prices[t.ticker] = t.price
                buy_qtys[t.ticker]   = t.quantity
            else:
                total_qty = buy_qtys[t.ticker] + t.quantity
                buy_prices[t.ticker] = (
                    buy_prices[t.ticker] * buy_qtys[t.ticker] +
                    t.price * t.quantity
                ) / total_qty
                buy_qtys[t.ticker] = total_qty
        elif t.action == TradeAction.SELL and t.ticker in buy_prices:
            trade_pnls.append((t.price - buy_prices[t.ticker]) * t.quantity)
            buy_qtys[t.ticker] = max(0, buy_qtys[t.ticker] - t.quantity)

    winning = [p for p in trade_pnls if p > 0]
    losing  = [p for p in trade_pnls if p <= 0]
    win_rate = len(winning) / len(trade_pnls) * 100 if trade_pnls else 0.0

    total_pnl     = (values[-1] - portfolio.initial_balance) if values else 0.0
    total_pnl_pct = total_pnl / portfolio.initial_balance * 100 if portfolio.initial_balance else 0.0

    ticker_counts = {}
    for t in trades:
        ticker_counts[t.ticker] = ticker_counts.get(t.ticker, 0) + 1
    most_traded = max(ticker_counts, key=ticker_counts.get) if ticker_counts else None

    return AnalyticsSummary(
        total_trades    = len(trades),
        winning_trades  = len(winning),
        losing_trades   = len(losing),
        win_rate        = round(win_rate, 2),
        sharpe_ratio    = round(sharpe, 4),
        max_drawdown    = round(max_drawdown, 4),
        best_trade_pnl  = round(float(max(trade_pnls)), 2) if trade_pnls else 0.0,
        worst_trade_pnl = round(float(min(trade_pnls)), 2) if trade_pnls else 0.0,
        avg_trade_pnl   = round(float(np.mean(trade_pnls)), 2) if trade_pnls else 0.0,
        total_pnl       = round(total_pnl, 2),
        total_pnl_pct   = round(total_pnl_pct, 4),
        most_traded     = most_traded,
    )


@router.get("/tickers", response_model=List[TickerStats])
def get_ticker_stats(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    portfolio = _get_portfolio(current_user, db)
    trades    = db.query(Trade).filter(Trade.portfolio_id == portfolio.id).all()
    stats = {}
    for t in trades:
        if t.ticker not in stats:
            stats[t.ticker] = {'ticker': t.ticker, 'total_trades': 0,
                               'buy_trades': 0, 'sell_trades': 0,
                               'total_volume': 0.0, 'signals': []}
        s = stats[t.ticker]
        s['total_trades'] += 1
        s['total_volume'] += t.quantity * t.price
        if t.action == TradeAction.BUY:   s['buy_trades']  += 1
        elif t.action == TradeAction.SELL: s['sell_trades'] += 1
        if t.signal_value: s['signals'].append(t.signal_value)

    return sorted([
        TickerStats(
            ticker       = s['ticker'],
            total_trades = s['total_trades'],
            buy_trades   = s['buy_trades'],
            sell_trades  = s['sell_trades'],
            total_volume = round(s['total_volume'], 2),
            avg_signal   = round(float(np.mean(s['signals'])), 4) if s['signals'] else 0.0,
        ) for s in stats.values()
    ], key=lambda x: x.total_trades, reverse=True)