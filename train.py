import os
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import pandas as pd
import yfinance as yf
from sklearn.preprocessing import MinMaxScaler
import random
import logging
import time
import pickle
import threading
from datetime import datetime, timedelta, timezone
from collections import deque
from ta.momentum import RSIIndicator
from ta.trend import MACD

from backend.services.trading import (
    get_active_tickers,
    write_pending_trade,
    auto_approve_expired,
    sync_portfolio_to_db,
    get_all_portfolio_states,
    log_portfolio_snapshot,
)

from backend.services.redis_service import (
    set_price,
    set_signal,
    set_portfolio_snapshot,
    append_chart_point,
    publish_signal,
    set_heartbeat,
    ping as redis_ping,
)

logging.basicConfig(
    filename='realtime_trading.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

ALL_TICKERS = ['GC=F', 'SPY', 'BTC-USD']

TICKER_CONFIG = {
    'GC=F':    {'max_shares': 100,  'label': 'Gold Futures'},
    'SPY':     {'max_shares': 500,  'label': 'S&P 500 ETF'},
    'BTC-USD': {'max_shares': 5,    'label': 'Bitcoin'},
}

T                      = 15
batch_size             = 32
actor_lr               = 1e-3
critic_lr              = 5e-4
gamma                  = 0.99
tau                    = 0.005
initial_balance        = 10_000_000
checkpoint_dir         = './realtime_checkpoints'
min_buffer_size        = 29
num_base_models        = 3
max_trades_per_day     = 1000
DB_SYNC_EVERY_N_TRADES = 5


# ─────────────────────────────────────────────
#  yfinance MultiIndex fix
#  yfinance v0.2+ returns MultiIndex columns:
#  ('Close', 'GC=F'), ('Close', 'SPY') etc.
#  We need to extract just the rows for OUR
#  specific ticker and get plain OHLCV columns.
# ─────────────────────────────────────────────
def clean_yf_df(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """
    Safely extract OHLCV data for a specific ticker
    from a yfinance DataFrame, handling both:
    - MultiIndex columns: ('Close', 'GC=F')
    - Flat columns: 'Close'
    """
    needed = ['Open', 'High', 'Low', 'Close', 'Volume']

    if isinstance(df.columns, pd.MultiIndex):
        # MultiIndex format — extract this ticker's columns
        # e.g. df['Close']['GC=F'] or df[('Close','GC=F')]
        try:
            # Try to select ticker slice
            df_t = df.xs(ticker, axis=1, level=1)
            # Now columns should be ['Open','High','Low','Close','Volume']
            df_t = df_t.loc[:, ~df_t.columns.duplicated()]
            for col in needed:
                if col not in df_t.columns:
                    df_t[col] = 0.0
            return df_t[needed]
        except Exception:
            # Fallback: just take first level values
            df.columns = df.columns.get_level_values(0)

    # Flat columns
    df = df.loc[:, ~df.columns.duplicated()]
    for col in needed:
        if col not in df.columns:
            df[col] = 0.0
    return df[needed]


# ─────────────────────────────────────────────
#  Shared Portfolio State
# ─────────────────────────────────────────────
class SharedPortfolio:
    def __init__(self, balance: float, portfolio_id: int = None):
        self._lock             = threading.Lock()
        self.balance           = balance
        self.portfolio_id      = portfolio_id
        self.shares            = {t: 0.0 for t in ALL_TICKERS}
        self.total_assets      = [balance]
        self.trades_today      = 0
        self.trades_since_sync = 0
        self.last_trade_date   = datetime.now().date()

    def execute_buy(self, ticker, qty, price, timestamp, signal):
        with self._lock:
            cost = qty * price
            if self.balance < cost:
                return False
            self.balance           -= cost
            self.shares[ticker]    += qty
            self.trades_today      += 1
            self.trades_since_sync += 1
            logging.info(f"[{ticker}] BUY {qty:.4f} @ ${price:.2f} | cash=${self.balance:,.2f}")
            return True

    def execute_sell(self, ticker, qty, price, timestamp, signal):
        with self._lock:
            qty = min(qty, self.shares[ticker])
            if qty <= 0:
                return False
            self.balance           += qty * price
            self.shares[ticker]    -= qty
            self.trades_today      += 1
            self.trades_since_sync += 1
            logging.info(f"[{ticker}] SELL {qty:.4f} @ ${price:.2f} | cash=${self.balance:,.2f}")
            return True

    def total_value(self, prices: dict) -> float:
        with self._lock:
            return self.balance + sum(
                self.shares[t] * prices[t]
                for t in ALL_TICKERS
                if prices.get(t, 0.0) > 0.0
            )

    def should_sync_db(self) -> bool:
        with self._lock:
            return self.trades_since_sync >= DB_SYNC_EVERY_N_TRADES

    def reset_sync_counter(self):
        with self._lock:
            self.trades_since_sync = 0

    def reset_daily_trades(self):
        with self._lock:
            today = datetime.now().date()
            if today != self.last_trade_date:
                self.trades_today    = 0
                self.last_trade_date = today

    def trades_remaining(self) -> int:
        with self._lock:
            return max(0, max_trades_per_day - self.trades_today)

    def get_shares_snapshot(self) -> dict:
        with self._lock:
            return dict(self.shares)


# ─────────────────────────────────────────────
#  Neural Network Models
# ─────────────────────────────────────────────
class ReplayBuffer:
    def __init__(self, capacity):
        self.capacity = capacity
        self.buffer   = []
        self.position = 0

    def push(self, state, action, reward, next_state):
        if len(self.buffer) < self.capacity:
            self.buffer.append(None)
        self.buffer[self.position] = (state, action, reward, next_state)
        self.position = (self.position + 1) % self.capacity

    def sample(self, batch_size):
        batch = random.sample(self.buffer, batch_size)
        states, actions, rewards, next_states = zip(*batch)
        return (
            torch.FloatTensor(np.array(states)).to(device),
            torch.FloatTensor(np.array(actions)).to(device),
            torch.FloatTensor(rewards).unsqueeze(1).to(device),
            torch.FloatTensor(np.array(next_states)).to(device),
        )

    def __len__(self):
        return len(self.buffer)


class GRUModel(nn.Module):
    def __init__(self, input_size=5, hidden_size=64, output_size=1):
        super().__init__()
        self.gru = nn.GRU(input_size, hidden_size, batch_first=True)
        self.fc  = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        out, _ = self.gru(x)
        return self.fc(out[:, -1, :])


class ALSTMModel(nn.Module):
    def __init__(self, input_size=5, hidden_size=64, output_size=1):
        super().__init__()
        self.lstm      = nn.LSTM(input_size, hidden_size, batch_first=True)
        self.attention = nn.Linear(hidden_size, 1)
        self.fc        = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        out, _  = self.lstm(x)
        weights = torch.softmax(self.attention(out).squeeze(-1), dim=1)
        context = torch.sum(out * weights.unsqueeze(-1), dim=1)
        return self.fc(context)


class TransformerModel(nn.Module):
    def __init__(self, input_size=5, d_model=64, output_size=1, nhead=4):
        super().__init__()
        self.embedding   = nn.Linear(input_size, d_model)
        encoder_layer    = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, batch_first=True)
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=2)
        self.fc          = nn.Linear(d_model, output_size)

    def forward(self, x):
        x = self.embedding(x)
        h = self.transformer(x)
        return self.fc(h.mean(dim=1))


class Actor(nn.Module):
    def __init__(self, state_dim, action_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, 256), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(256, 256),       nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(256, 128),       nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(128, 64),        nn.ReLU(),
            nn.Linear(64, action_dim),
            nn.Softmax(dim=-1),
        )

    def forward(self, state):
        return self.net(state)


class Critic(nn.Module):
    def __init__(self, state_dim, action_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim + action_dim, 256), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(256, 128),                    nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(128, 64),                     nn.ReLU(),
            nn.Linear(64, 1),
        )

    def forward(self, state, action):
        return self.net(torch.cat([state, action], dim=1))


# ─────────────────────────────────────────────
#  Environment
# ─────────────────────────────────────────────
class RealTimeStockEnv:
    def __init__(self, window_size):
        self.window_size    = window_size
        self.scaler         = MinMaxScaler()
        self.data_buffer    = deque(maxlen=window_size * 2)
        self.last_timestamp = None

    def update(self, new_data, timestamp):
        new_data = np.array(new_data, dtype=float).flatten()[:5]
        if len(new_data) < 5:
            new_data = np.pad(new_data, (0, 5 - len(new_data)))

        timestamp_minute = timestamp.replace(second=0, microsecond=0)
        if self.last_timestamp and timestamp_minute <= self.last_timestamp:
            return None

        self.data_buffer.append(new_data)

        if len(self.data_buffer) < min_buffer_size:
            remaining = min_buffer_size - len(self.data_buffer)
            print(f"Buffering: {len(self.data_buffer)}/{min_buffer_size} (~{remaining} min remaining)")
            return None

        if not hasattr(self.scaler, 'data_min_'):
            fit_data = np.array(self.data_buffer)
            if fit_data.ndim == 2 and fit_data.shape[1] == 5:
                self.scaler.fit(fit_data)
                print("Scaler fitted — trading can begin")
            else:
                logging.error(f"Scaler fit failed: shape={fit_data.shape}")
                return None

        self.last_timestamp = timestamp_minute
        try:
            return self.scaler.transform([new_data])[0]
        except Exception as e:
            logging.error(f"Scaler transform error: {e}")
            return None

    def get_state(self):
        if len(self.data_buffer) >= T and hasattr(self.scaler, 'data_min_'):
            try:
                buf_array = np.array(self.data_buffer)
                if buf_array.shape[1] != 5:
                    return None
                return self.scaler.transform(buf_array)[-T:]
            except Exception as e:
                logging.error(f"get_state error: {e}")
                return None
        return None


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def compute_technical_indicators(data_buffer):
    try:
        df   = pd.DataFrame(list(data_buffer), columns=['Open','High','Low','Close','Volume'])
        rsi  = RSIIndicator(df['Close'], window=14).rsi().iloc[-1] / 100 if len(df) >= 14 else 0.5
        macd = MACD(df['Close']).macd_diff().iloc[-1] / (df['Close'].iloc[-1] + 1e-6) if len(df) >= 26 else 0.0
        roc  = (df['Close'].iloc[-1] - df['Close'].iloc[-5]) / (df['Close'].iloc[-5] + 1e-6) if len(df) >= 5 else 0.0
        return np.nan_to_num(np.array([rsi, macd, roc]), nan=0.0)
    except Exception as e:
        logging.error(f"Technical indicator error: {e}")
        return np.zeros(3)


def sample_dirichlet_action_probs(num_actions):
    return torch.FloatTensor(np.random.dirichlet(np.ones(num_actions) * 0.5)).to(device)


def save_replay_buffer(replay, path):
    try:
        with open(path, 'wb') as f:
            pickle.dump(replay.buffer, f)
    except Exception as e:
        logging.error(f"Error saving replay buffer: {e}")


def load_replay_buffer(path, capacity):
    replay = ReplayBuffer(capacity)
    if os.path.exists(path):
        try:
            with open(path, 'rb') as f:
                loaded = pickle.load(f)
            if loaded and loaded[0] is not None:
                state_arr    = np.array(loaded[0][0])
                expected_dim = T * 5 + num_base_models * 2 + 3
                if state_arr.shape == (expected_dim,):
                    replay.buffer   = loaded
                    replay.position = len(loaded) % capacity
                    print(f"Loaded replay buffer: {len(replay)} experiences")
                else:
                    print(f"Replay buffer shape mismatch — starting fresh")
        except Exception as e:
            logging.error(f"Error loading replay buffer: {e}")
            print(f"Could not load replay buffer — starting fresh")
    return replay


def initialize_env_with_history(ticker, env, initial_points=29, max_retries=5):
    for attempt in range(max_retries):
        try:
            df = yf.download(ticker, period='7d', interval='1m',
                             auto_adjust=False, progress=False)
            if df.empty:
                time.sleep(15 * (2 ** attempt))
                continue

            df = clean_yf_df(df, ticker)

            points = 0
            for timestamp, row in df.iterrows():
                if points >= initial_points:
                    break
                data = row[['Open','High','Low','Close','Volume']].values.astype(float)
                env.update(data, timestamp)
                points += 1

            print(f"[{ticker}] Pre-populated {points} historical points")
            if len(env.data_buffer) >= initial_points:
                return True
        except Exception as e:
            logging.error(f"[{ticker}] History attempt {attempt+1}: {e}")
            time.sleep(15 * (2 ** attempt))
    return False


def initialize_models(ticker: str):
    ticker_safe = ticker.replace('=', '_').replace('-', '_')
    ckpt_dir    = os.path.join(checkpoint_dir, ticker_safe)
    os.makedirs(ckpt_dir, exist_ok=True)

    models = {
        'GRU':         GRUModel().to(device),
        'ALSTM':       ALSTMModel().to(device),
        'Transformer': TransformerModel().to(device),
    }
    state_dim     = T * 5 + num_base_models * 2 + 3
    actor         = Actor(state_dim, num_base_models).to(device)
    critic        = Critic(state_dim, num_base_models).to(device)
    target_actor  = Actor(state_dim, num_base_models).to(device)
    target_critic = Critic(state_dim, num_base_models).to(device)

    for name, model in models.items():
        path = os.path.join(ckpt_dir, f'{name}.pth')
        if os.path.exists(path):
            try:
                model.load_state_dict(torch.load(path, map_location=device))
            except Exception as e:
                logging.error(f"[{ticker}] Load {name}: {e}")

    critic_path = os.path.join(ckpt_dir, 'critic.pth')
    if os.path.exists(critic_path):
        try:
            critic.load_state_dict(torch.load(critic_path, map_location=device))
        except Exception as e:
            logging.error(f"[{ticker}] Load critic: {e}")

    target_actor.load_state_dict(actor.state_dict())
    target_critic.load_state_dict(critic.state_dict())
    return models, actor, critic, target_actor, target_critic, ckpt_dir


# ─────────────────────────────────────────────
#  Per-Ticker Trading Thread
# ─────────────────────────────────────────────
def ticker_trading_loop(ticker, portfolio, current_prices, prices_lock):
    print(f"[{ticker}] Thread started")
    logging.info(f"[{ticker}] Thread started")

    ticker_safe = ticker.replace('=', '_').replace('-', '_')
    ckpt_dir_t  = os.path.join(checkpoint_dir, ticker_safe)
    replay_path = os.path.join(ckpt_dir_t, 'replay_buffer.pkl')
    max_shares  = TICKER_CONFIG[ticker]['max_shares']

    env = RealTimeStockEnv(T)
    initialize_env_with_history(ticker, env)

    models, actor, critic, target_actor, target_critic, ckpt_dir = \
        initialize_models(ticker)

    replay        = load_replay_buffer(replay_path, 500_000)
    error_windows = {name: deque(maxlen=100) for name in models}

    base_optimizers = {n: optim.Adam(m.parameters(), lr=1e-4)
                       for n, m in models.items()}
    actor_optim  = optim.Adam(actor.parameters(),  lr=actor_lr)
    critic_optim = optim.Adam(critic.parameters(), lr=critic_lr)
    criterion    = nn.MSELoss()

    drl_started       = False
    exploration_steps = 0
    stall_count       = 0
    last_buffer_size  = 0

    while True:
        try:
            portfolio.reset_daily_trades()
            auto_approve_expired()

            df = yf.download(ticker, period='1d', interval='1m',
                             auto_adjust=False, progress=False)
            if df.empty:
                stall_count += 1
                logging.warning(f"[{ticker}] Empty data (market may be closed)")
                time.sleep(min(15 * (2 ** stall_count), 300))
                continue

            # ── Fix MultiIndex with ticker-aware extraction ──
            df = clean_yf_df(df, ticker)

            timestamp     = df.index[-1]
            latest_data   = df.iloc[-1][['Open','High','Low','Close','Volume']].values.astype(float)
            current_price = float(latest_data[3])

            if current_price <= 0 or np.isnan(current_price):
                logging.warning(f"[{ticker}] Invalid price {current_price}, skipping")
                time.sleep(60)
                continue

            set_price(ticker, current_price)
            timestamp_str = timestamp.isoformat()
            append_chart_point(ticker, current_price, timestamp_str)

            with prices_lock:
                current_prices[ticker] = current_price

            scaled = env.update(latest_data, timestamp)
            if scaled is None:
                cur_buf = len(env.data_buffer)
                stall_count = stall_count + 1 if cur_buf == last_buffer_size else 0
                last_buffer_size = cur_buf
                time.sleep(60)
                continue

            stall_count      = 0
            last_buffer_size = len(env.data_buffer)

            state = env.get_state()
            if state is None:
                time.sleep(60)
                continue

            if len(env.data_buffer) > T + 1:
                X = np.array([state[:-1]])
                y = float(scaled[3])
                for name, model in models.items():
                    try:
                        pred = model(torch.FloatTensor(X).to(device))
                        loss = criterion(pred, torch.FloatTensor([[y]]).to(device))
                        base_optimizers[name].zero_grad()
                        loss.backward()
                        base_optimizers[name].step()
                        error_windows[name].append(abs(pred.item() - y) / (abs(y) + 1e-6))
                    except Exception as e:
                        logging.error(f"[{ticker}] Base model {name}: {e}")

            state_tensor  = torch.FloatTensor(state).unsqueeze(0).to(device)
            preds         = {}
            for n, m in models.items():
                try: preds[n] = m(state_tensor).item()
                except: preds[n] = 0.0

            errors        = {n: np.mean(list(e)) if e else 1.0 for n, e in error_windows.items()}
            inv_errors    = np.array([1 / (e + 1e-6) for e in errors.values()])
            model_weights = inv_errors / (inv_errors.sum() + 1e-6)
            tech          = compute_technical_indicators(env.data_buffer)

            state_vector  = np.nan_to_num(
                np.concatenate([state.flatten(), list(preds.values()), model_weights, tech]),
                nan=0.0
            )
            state_vector += np.random.normal(0, 0.05, state_vector.shape)
            exploration_steps += 1

            with torch.no_grad():
                actor(torch.FloatTensor(state_vector).to(device))
                action_probs = sample_dirichlet_action_probs(num_base_models)

            signal     = action_probs.mean().item()
            signal_std = action_probs.std().item()
            denom      = action_probs.max().item() - action_probs.min().item() + 1e-6
            signal     = 0.05 + 0.9 * (signal - action_probs.min().item()) / denom

            action_taken   = np.random.choice(['BUY','SELL','HOLD'], p=[0.45,0.45,0.1])
            qty            = 0.0
            current_shares = portfolio.shares[ticker]

            if action_taken == 'BUY' and portfolio.trades_remaining() > 0:
                qty = max(1.0, min(
                    portfolio.balance / (current_price + 1e-6),
                    max_shares - current_shares,
                    signal * 10.0,
                ))
                if qty > 0:
                    write_pending_trade(
                        ticker=ticker, action='BUY', price=current_price,
                        quantity=qty, signal_value=signal, expires_in_seconds=30,
                    )

            elif action_taken == 'SELL' and current_shares > 0 \
                    and portfolio.trades_remaining() > 0:
                qty = max(1.0, min(current_shares, (1 - signal) * 10.0))
                write_pending_trade(
                    ticker=ticker, action='SELL', price=current_price,
                    quantity=qty, signal_value=signal, expires_in_seconds=30,
                )

            elif current_shares > max_shares:
                excess = current_shares - max_shares
                portfolio.execute_sell(ticker, excess, current_price, timestamp, signal)
                action_taken = 'SELL'
                qty = excess

            with prices_lock:
                prices_snapshot = dict(current_prices)

            total_value = portfolio.total_value(prices_snapshot)
            if np.isnan(total_value):
                total_value = portfolio.balance
                logging.warning(f"[{ticker}] NaN total_value, using balance")

            prev_value = portfolio.total_assets[-1] if portfolio.total_assets else initial_balance

            set_signal(
                ticker=ticker, signal=signal, signal_std=signal_std,
                action=action_taken, price=current_price,
                portfolio_value=total_value,
                trades_today=portfolio.trades_today,
                shares=portfolio.shares[ticker],
            )

            if portfolio.portfolio_id:
                set_portfolio_snapshot(
                    portfolio_id=portfolio.portfolio_id,
                    balance=portfolio.balance,
                    total_value=total_value,
                    shares=portfolio.get_shares_snapshot(),
                    prices=prices_snapshot,
                    trades_today=portfolio.trades_today,
                )

            publish_signal({
                "type":            "signal",
                "ticker":          ticker,
                "price":           round(current_price, 2),
                "signal":          round(signal, 4),
                "signal_std":      round(signal_std, 4),
                "action":          action_taken,
                "portfolio_value": round(total_value, 2),
                "trades_today":    portfolio.trades_today,
                "shares":          round(portfolio.shares[ticker], 4),
                "timestamp":       timestamp_str,
            })

            if portfolio.should_sync_db() and portfolio.portfolio_id:
                sync_portfolio_to_db(
                    portfolio_id=portfolio.portfolio_id,
                    balance=portfolio.balance,
                    shares=portfolio.shares,
                )
                log_portfolio_snapshot(
                    portfolio_id=portfolio.portfolio_id,
                    total_value=total_value,
                    prices=prices_snapshot,
                )
                portfolio.reset_sync_counter()

            assets_hist = portfolio.total_assets[-100:]
            returns     = np.diff(assets_hist) / (np.array(assets_hist[:-1]) + 1e-6)
            sharpe      = np.mean(returns) / (np.std(returns) + 1e-6) if len(returns) > 1 else 0
            reward      = np.nan_to_num(
                (total_value - prev_value) / (prev_value + 1e-6)
                + 0.001 * sharpe
                + 0.2 * (qty / max_shares if action_taken != 'HOLD' else 0),
                nan=0.0
            )
            portfolio.total_assets.append(total_value)

            next_state = env.get_state()
            if next_state is not None:
                next_vector = np.nan_to_num(
                    np.concatenate([next_state.flatten(), list(preds.values()), model_weights, tech]),
                    nan=0.0
                )
                replay.push(state_vector, action_probs.cpu().detach().numpy(), reward, next_vector)
                if exploration_steps % 10 == 0:
                    save_replay_buffer(replay, replay_path)

            if len(replay) >= batch_size and not drl_started:
                print(f"[{ticker}] DRL training started ({len(replay)} experiences)")
                drl_started = True

            if len(replay) >= batch_size:
                try:
                    s, a, r, ns = replay.sample(batch_size)
                    with torch.no_grad():
                        tq = r + gamma * target_critic(ns, target_actor(ns))
                    critic_loss = criterion(critic(s, a), tq)
                    critic_optim.zero_grad(); critic_loss.backward(); critic_optim.step()
                    ap         = actor(s)
                    entropy    = -torch.sum(ap * torch.log(ap + 1e-6), dim=-1).mean()
                    actor_loss = -critic(s, ap).mean() - entropy
                    actor_optim.zero_grad(); actor_loss.backward(); actor_optim.step()
                    for tp, p in zip(target_actor.parameters(), actor.parameters()):
                        tp.data.copy_(tau * p.data + (1 - tau) * tp.data)
                    for tp, p in zip(target_critic.parameters(), critic.parameters()):
                        tp.data.copy_(tau * p.data + (1 - tau) * tp.data)
                except Exception as e:
                    logging.error(f"[{ticker}] DRL training error: {e}")

            try:
                timestamp_ist = timestamp.astimezone(timezone(timedelta(hours=5, minutes=30)))
            except Exception:
                timestamp_ist = timestamp

            log_msg = (
                f"{timestamp_ist} | Ticker: {ticker} | Price: ${current_price:.2f} | "
                f"Signal: {signal:.3f} | Signal Std: {signal_std:.3f} | "
                f"Action: {action_taken} | Portfolio: ${total_value:.2f} | "
                f"Trades Today: {portfolio.trades_today}/{max_trades_per_day} | "
                f"Shares [{ticker}]: {portfolio.shares[ticker]:.4f}"
            )
            logging.info(log_msg)
            print(log_msg)

            if datetime.now().minute % 5 == 0:
                try:
                    for name, model in models.items():
                        torch.save(model.state_dict(), os.path.join(ckpt_dir, f'{name}.pth'))
                    torch.save(actor.state_dict(),  os.path.join(ckpt_dir, 'actor.pth'))
                    torch.save(critic.state_dict(), os.path.join(ckpt_dir, 'critic.pth'))
                except Exception as e:
                    logging.error(f"[{ticker}] Checkpoint error: {e}")

            time.sleep(60)

        except Exception as e:
            logging.error(f"[{ticker}] Loop error: {e}")
            print(f"[{ticker}] Error: {e}")
            time.sleep(30)


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
def main():
    os.makedirs(checkpoint_dir, exist_ok=True)

    if not redis_ping():
        print("ERROR: Cannot connect to Redis. Start with: sc start Redis")
        return
    print("✓ Redis connected")

    print("Loading portfolio state from DB...")
    portfolio_states = get_all_portfolio_states()

    if portfolio_states:
        state_data = portfolio_states[0]
        portfolio  = SharedPortfolio(
            balance=state_data['balance'],
            portfolio_id=state_data['portfolio_id'],
        )
        for ticker, qty in state_data['shares'].items():
            if ticker in portfolio.shares:
                portfolio.shares[ticker] = qty
        print(f"Restored portfolio #{state_data['portfolio_id']} | "
              f"balance=${state_data['balance']:,.2f} | "
              f"paused={state_data.get('trading_paused', False)}")
    else:
        portfolio = SharedPortfolio(initial_balance)
        print("No DB portfolios found — starting fresh at $10M")

    active_tickers = get_active_tickers()
    if not active_tickers:
        print("No active tickers in DB — using all tickers")
        active_tickers = ALL_TICKERS

    current_prices = {t: 0.0 for t in ALL_TICKERS}
    prices_lock    = threading.Lock()

    print("=" * 60)
    print("  AlphaBot — Redis + DB Connected Trading Engine")
    print(f"  Active tickers : {', '.join(active_tickers)}")
    print(f"  Portfolio ID   : {portfolio.portfolio_id}")
    print(f"  Balance        : ${portfolio.balance:,.2f}")
    print(f"  Device         : {device}")
    print("=" * 60)

    threads = []
    for ticker in active_tickers:
        if ticker not in TICKER_CONFIG:
            print(f"[WARNING] Unknown ticker {ticker}, skipping")
            continue
        t = threading.Thread(
            target=ticker_trading_loop,
            args=(ticker, portfolio, current_prices, prices_lock),
            name=f"thread-{ticker}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        time.sleep(5)

    try:
        while True:
            time.sleep(60)
            set_heartbeat()
            with prices_lock:
                prices_snap = dict(current_prices)
            total = portfolio.total_value(prices_snap)
            print(
                f"[HEARTBEAT] Total=${total:,.2f} | "
                f"Cash=${portfolio.balance:,.2f} | "
                f"Holdings={ {t: round(s,4) for t,s in portfolio.shares.items()} }"
            )
    except KeyboardInterrupt:
        print("\nShutting down AlphaBot...")
        if portfolio.portfolio_id:
            sync_portfolio_to_db(
                portfolio_id=portfolio.portfolio_id,
                balance=portfolio.balance,
                shares=portfolio.shares,
            )
            print("Final state saved to DB.")
        logging.info("AlphaBot shutdown")


if __name__ == "__main__":
    main()