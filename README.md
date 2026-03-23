# ⚡ AlphaBot — Real-Time DRL Trading System

A production-grade algorithmic trading platform powered by a Deep Reinforcement Learning ensemble. AlphaBot trades Gold Futures, S&P 500 ETF and Bitcoin simultaneously using parallel ML models, with a full-stack web dashboard for real-time monitoring and control.

> **Paper trading only. No real money is involved.**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        train.py                             │
│   GRU + ALSTM + Transformer  →  Actor-Critic (DDPG)        │
│   3 parallel threads (GC=F, SPY, BTC-USD)                   │
└──────────────┬──────────────────────────┬───────────────────┘
               │                          │
               ▼                          ▼
        PostgreSQL DB               Redis Cache
        (trades, users,             (live prices,
         portfolios,                 signals, pub/sub)
         pending_trades)                  │
               │                          │
               └──────────┬───────────────┘
                           ▼
                     FastAPI Backend
                     (JWT auth, REST + WebSocket)
                           │
                           ▼
                   Browser Dashboard
                   (live charts, controls,
                    trade approval UI)
```

---

## Features

### Machine Learning
- **Ensemble DRL** — GRU, Attention-LSTM and Transformer base models
- **Actor-Critic agent** — DDPG with Gumbel-Softmax and Dirichlet action sampling
- **Sharpe ratio reward shaping** — risk-adjusted returns
- **Per-ticker checkpointing** — models persist across restarts
- **Replay buffer** — 500K experience capacity with shape validation

### Backend
- **FastAPI** — async REST API + WebSocket server
- **PostgreSQL** — users, portfolios, trades, pending trades
- **Redis** — price cache, signal cache, pub/sub channel
- **JWT authentication** — bcrypt password hashing, token-based sessions
- **Circuit breaker** — pause trading, resume, emergency stop (liquidates all positions)

### Real-Time Pipeline
- **3 parallel trading threads** — one per ticker, shared portfolio balance
- **Pending trade system** — bot writes signals as PENDING, user can approve/reject
- **Auto-approval** — 30-second window, then executes automatically
- **WebSocket push** — ~50ms latency from signal to dashboard update

### Dashboard
- Live signal display (tabbed per ticker)
- Portfolio value chart + allocation donut
- Per-ticker price charts
- Analytics: Sharpe ratio, win rate, max drawdown, best/worst trade
- Pending trades with Approve / Reject buttons
- Trading controls: Pause, Resume, Emergency Stop

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| ML    | PyTorch, NumPy, scikit-learn, ta |
| API   | FastAPI, SQLAlchemy, Pydantic |
| DB    | PostgreSQL (via psycopg2) |
| Cache | Redis |
| Auth  | JWT (python-jose), bcrypt (passlib) |
| Data  | yfinance |
| Frontend | Vanilla JS, Chart.js, WebSockets |

---

## Project Structure

```
alphabot/
├── train.py                    # ML training loop (3 tickers, parallel)
├── api.py                      # FastAPI entry point
├── .env                        # Environment variables (not committed)
├── backend/
│   ├── database.py             # SQLAlchemy engine + session
│   ├── models.py               # ORM table definitions
│   ├── schemas.py              # Pydantic request/response shapes
│   ├── auth.py                 # JWT + bcrypt logic
│   ├── routers/
│   │   ├── auth.py             # /auth/register, /auth/login
│   │   ├── portfolio.py        # /me/portfolio, /me/trades, /me/pending
│   │   ├── analytics.py        # /analytics/summary, /analytics/tickers
│   │   ├── controls.py         # /controls/pause, /controls/resume, /controls/emergency-stop
│   │   └── websocket.py        # ws://localhost:8000/ws
│   └── services/
│       ├── trading.py          # DB service layer for train.py
│       └── redis_service.py    # Redis read/write operations
└── frontend/
    ├── index.html
    ├── app.js
    └── styles.css
```

---

## Setup

### Prerequisites
- Python 3.10+
- PostgreSQL 15+
- Redis 7+

### Installation

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/alphabot.git
cd alphabot

# Install dependencies
pip install torch torchvision numpy pandas scikit-learn
pip install fastapi uvicorn sqlalchemy psycopg2-binary
pip install python-jose[cryptography] passlib[bcrypt] python-dotenv
pip install yfinance ta redis websockets email-validator
pip install bcrypt==4.0.1
```

### Environment Setup

Create a `.env` file in the project root:

```
DATABASE_URL=postgresql://your_user:your_password@localhost:5432/alphabot
JWT_SECRET=your_secret_key_here
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=1440
```

### Database Setup

```bash
# Create database in PostgreSQL
psql -U postgres -c "CREATE DATABASE alphabot;"
psql -U postgres -c "CREATE USER alphabot_user WITH PASSWORD 'your_password';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE alphabot TO alphabot_user;"

# Tables are created automatically on first run via SQLAlchemy
```

### Run

```bash
# Terminal 1 — API server
python -m uvicorn api:app --reload

# Terminal 2 — Trading engine
python train.py
```

Open **http://localhost:8000** in your browser.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/register` | Create account |
| POST | `/auth/login` | Get JWT token |
| GET | `/me/portfolio` | Portfolio state |
| POST | `/me/portfolio/stocks` | Add ticker (GC=F, SPY, BTC-USD) |
| GET | `/me/trades` | Trade history |
| GET | `/me/pending` | Pending trades |
| POST | `/me/pending/{id}/resolve` | Approve or reject |
| GET | `/analytics/summary` | Sharpe, win rate, drawdown |
| PATCH | `/controls/pause` | Pause trading |
| PATCH | `/controls/resume` | Resume trading |
| POST | `/controls/emergency-stop` | Liquidate all + pause |
| WS | `/ws?token=...` | Live signal stream |

---

## How It Works

1. `train.py` starts 3 threads — one per ticker
2. Each thread buffers 29 historical candles, fits a MinMaxScaler, then begins trading
3. The DRL model generates a signal every 60 seconds
4. The signal is written to PostgreSQL as a `PENDING` trade and cached in Redis
5. FastAPI publishes the signal via Redis pub/sub to all connected WebSocket clients
6. The frontend receives the push in ~50ms and updates the dashboard
7. If `auto_trade=True`, the pending trade executes after 30 seconds automatically
8. Users can approve/reject pending trades or trigger the circuit breaker at any time

---

## Screenshots

*Dashboard with live BTC signal, portfolio value chart, and trading controls*

---

## License

MIT
