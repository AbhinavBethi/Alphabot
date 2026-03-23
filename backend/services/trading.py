"""
backend/services/trading.py
────────────────────────────
Service layer between train.py and the database.

Key addition: write_pending_trade() now checks
portfolio.trading_paused before inserting.
If paused → returns empty list, no trade written.
"""

from datetime import datetime, timedelta
from typing import Optional
import logging

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import (
    Portfolio, PortfolioStock, Trade,
    PendingTrade, TradeAction, PendingStatus
)


def get_session() -> Session:
    return SessionLocal()


# ─────────────────────────────────────────────
#  1. Get active tickers
# ─────────────────────────────────────────────
def get_active_tickers() -> list[str]:
    db = get_session()
    try:
        rows = db.query(PortfolioStock.ticker).distinct().all()
        return [row.ticker for row in rows]
    except Exception as e:
        logging.error(f"[DB] get_active_tickers error: {e}")
        return []
    finally:
        db.close()


# ─────────────────────────────────────────────
#  2. Write pending trade
#  ── NOW CHECKS trading_paused FLAG ──
#  If any portfolio is paused, no trade is written
#  for that portfolio. Others still trade normally.
# ─────────────────────────────────────────────
def write_pending_trade(
    ticker:             str,
    action:             str,
    price:              float,
    quantity:           float,
    signal_value:       float,
    expires_in_seconds: int = 30,
) -> list[int]:
    db = get_session()
    created_ids = []
    try:
        stocks = (
            db.query(PortfolioStock)
            .filter(PortfolioStock.ticker == ticker)
            .all()
        )
        if not stocks:
            return []

        expires_at = datetime.utcnow() + timedelta(seconds=expires_in_seconds)

        for stock in stocks:
            portfolio = db.query(Portfolio).filter(
                Portfolio.id == stock.portfolio_id
            ).first()

            if not portfolio:
                continue

            # ── Circuit breaker check ────────────────
            if portfolio.trading_paused:
                logging.info(
                    f"[DB] Trading paused for portfolio {portfolio.id} "
                    f"— skipping {action} {ticker}"
                )
                continue

            # Skip duplicate pending trades
            existing = (
                db.query(PendingTrade)
                .filter(
                    PendingTrade.portfolio_id == stock.portfolio_id,
                    PendingTrade.ticker       == ticker,
                    PendingTrade.status       == PendingStatus.PENDING,
                )
                .first()
            )
            if existing:
                continue

            pending = PendingTrade(
                portfolio_id = stock.portfolio_id,
                ticker       = ticker,
                action       = TradeAction(action),
                price        = price,
                quantity     = quantity,
                signal_value = signal_value,
                status       = PendingStatus.PENDING,
                expires_at   = expires_at,
            )
            db.add(pending)
            db.flush()
            created_ids.append(pending.id)
            logging.info(
                f"[DB] Pending {action} {ticker} qty={quantity:.4f} "
                f"@ ${price:.2f} → portfolio={stock.portfolio_id}"
            )

        db.commit()
        return created_ids

    except Exception as e:
        db.rollback()
        logging.error(f"[DB] write_pending_trade error: {e}")
        return []
    finally:
        db.close()


# ─────────────────────────────────────────────
#  3. Auto-approve expired pending trades
# ─────────────────────────────────────────────
def auto_approve_expired() -> int:
    db = get_session()
    processed = 0
    try:
        now     = datetime.utcnow()
        expired = (
            db.query(PendingTrade)
            .filter(
                PendingTrade.status     == PendingStatus.PENDING,
                PendingTrade.expires_at <= now,
            )
            .all()
        )

        for pending in expired:
            portfolio = db.query(Portfolio).filter(
                Portfolio.id == pending.portfolio_id
            ).first()

            if not portfolio:
                pending.status      = PendingStatus.EXPIRED
                pending.resolved_at = now
                continue

            # If paused, expire instead of executing
            if portfolio.trading_paused:
                pending.status      = PendingStatus.EXPIRED
                pending.resolved_at = now
                logging.info(
                    f"[DB] Expired pending trade (trading paused): "
                    f"{pending.ticker} portfolio={pending.portfolio_id}"
                )
                processed += 1
                continue

            if portfolio.auto_trade:
                success = _execute_trade_in_session(db, pending, portfolio)
                if success:
                    pending.status      = PendingStatus.APPROVED
                    pending.resolved_at = now
                    logging.info(
                        f"[DB] Auto-approved: {pending.action.value} "
                        f"{pending.ticker} qty={pending.quantity:.4f} "
                        f"@ ${pending.price:.2f}"
                    )
                else:
                    pending.status      = PendingStatus.EXPIRED
                    pending.resolved_at = now
            else:
                pending.status      = PendingStatus.EXPIRED
                pending.resolved_at = now

            processed += 1

        db.commit()
        return processed

    except Exception as e:
        db.rollback()
        logging.error(f"[DB] auto_approve_expired error: {e}")
        return 0
    finally:
        db.close()


# ─────────────────────────────────────────────
#  4. Execute trade (internal helper)
# ─────────────────────────────────────────────
def _execute_trade_in_session(db, pending, portfolio) -> bool:
    stock = db.query(PortfolioStock).filter(
        PortfolioStock.portfolio_id == portfolio.id,
        PortfolioStock.ticker       == pending.ticker,
    ).first()

    cost = pending.quantity * pending.price

    if pending.action == TradeAction.BUY:
        if portfolio.balance < cost:
            return False
        portfolio.balance -= cost
        if stock:
            total_cost          = stock.shares_held * stock.avg_buy_price + cost
            stock.shares_held  += pending.quantity
            stock.avg_buy_price = total_cost / stock.shares_held if stock.shares_held > 0 else 0
        else:
            return False

    elif pending.action == TradeAction.SELL:
        if not stock or stock.shares_held < pending.quantity:
            return False
        stock.shares_held -= pending.quantity
        portfolio.balance += pending.quantity * pending.price

    trade = Trade(
        portfolio_id = portfolio.id,
        ticker       = pending.ticker,
        action       = pending.action,
        price        = pending.price,
        quantity     = pending.quantity,
        signal_value = pending.signal_value,
        total_value  = portfolio.balance,
        timestamp    = datetime.utcnow(),
    )
    db.add(trade)
    return True


# ─────────────────────────────────────────────
#  5. Sync portfolio to DB
# ─────────────────────────────────────────────
def sync_portfolio_to_db(portfolio_id: int, balance: float, shares: dict) -> bool:
    db = get_session()
    try:
        portfolio = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
        if not portfolio:
            return False
        portfolio.balance    = balance
        portfolio.updated_at = datetime.utcnow()
        for ticker, qty in shares.items():
            stock = db.query(PortfolioStock).filter(
                PortfolioStock.portfolio_id == portfolio_id,
                PortfolioStock.ticker       == ticker,
            ).first()
            if stock:
                stock.shares_held = qty
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logging.error(f"[DB] sync_portfolio_to_db error: {e}")
        return False
    finally:
        db.close()


# ─────────────────────────────────────────────
#  6. Get all portfolio states
# ─────────────────────────────────────────────
def get_all_portfolio_states() -> list[dict]:
    db = get_session()
    try:
        portfolios = db.query(Portfolio).all()
        result = []
        for p in portfolios:
            result.append({
                'portfolio_id':  p.id,
                'user_id':       p.user_id,
                'balance':       p.balance,
                'auto_trade':    p.auto_trade,
                'trading_paused': p.trading_paused,
                'shares':        {s.ticker: s.shares_held for s in p.stocks},
            })
        return result
    except Exception as e:
        logging.error(f"[DB] get_all_portfolio_states error: {e}")
        return []
    finally:
        db.close()


# ─────────────────────────────────────────────
#  7. Check if trading is paused for a portfolio
# ─────────────────────────────────────────────
def is_trading_paused(portfolio_id: int) -> bool:
    """
    Quick check used by train.py heartbeat to log
    pause status without loading full state.
    """
    db = get_session()
    try:
        p = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
        return p.trading_paused if p else False
    except Exception as e:
        logging.error(f"[DB] is_trading_paused error: {e}")
        return False
    finally:
        db.close()


# ─────────────────────────────────────────────
#  8. Portfolio snapshot log
# ─────────────────────────────────────────────
def log_portfolio_snapshot(portfolio_id: int, total_value: float, prices: dict) -> None:
    logging.info(
        f"[SNAPSHOT] portfolio={portfolio_id} "
        f"total=${total_value:,.2f} prices={prices}"
    )