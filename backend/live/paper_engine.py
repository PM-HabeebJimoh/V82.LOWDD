"""
V82.LOWDD - Paper Trade Engine (REAL IG prices, simulated execution).

This is a real execution simulator. It uses 100% live IG prices but does
not submit orders to IG's order endpoint. Instead it simulates fills
at the live bid/offer.

This is NOT fake data. The prices come from IG. The forecasts come
from the same V82.LOWDD engine. The risk manager is the same. The
P&L math is real. Only the order placement is local.

When IG's historical data endpoint is rate-limited (daily allowance
exhausted), the engine falls back to building bar history from
live bid/offer snapshots. This keeps the system producing real
signals and trades even when IG blocks the /prices endpoint.

Every trade and order is written to a permanent file the moment it
happens — not on flush. The file is the source of truth for History.
"""
import os
import json
import time
import pickle
import logging
import threading
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'state'
)
os.makedirs(STATE_DIR, exist_ok=True)
STATE_PATH = os.path.join(STATE_DIR, 'paper_state.pkl')
TRADE_HISTORY_PATH = os.path.join(STATE_DIR, 'paper_trade_history.pkl')
ORDER_HISTORY_PATH = os.path.join(STATE_DIR, 'paper_order_history.pkl')


@dataclass
class PaperBar:
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PaperPosition:
    instrument: str
    display_name: str
    direction: str
    entry_price: float
    entry_time: object
    stop_price: float
    target_price: float
    n_units: float
    bars_held: int = 0
    unrealized_pnl: float = 0.0
    exit_info: dict = field(default_factory=dict)
    entry_reason: str = ''


@dataclass
class PaperTrade:
    trade_id: str
    instrument: str
    display_name: str
    direction: str
    entry_time: object
    entry_price: float
    exit_time: object
    exit_price: float
    n_units: float
    pnl: float
    won: bool
    exit_type: str
    bars_held: int
    entry_forecast: str
    entry_streak: float
    entry_atr: float
    entry_reason: str = ''


@dataclass
class PaperOrder:
    order_id: str
    instrument: str
    display_name: str
    direction: str
    n_units: float
    order_type: str
    status: str
    created_at: object
    filled_at: Optional[object] = None
    filled_price: Optional[float] = None
    reject_reason: str = ''
    reference_price: float = 0.0
    spread_at_fill: float = 0.0


def _next_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000) % 100000000:08d}"


class PaperEngine:
    """Real-time paper trading engine using live IG prices."""

    def __init__(self, broker, bars_fn, risk_manager,
                 config: dict = None,
                 universe_resolver=None,
                 name_resolver=None):
        self.broker = broker
        self.bars_fn = bars_fn
        self.risk = risk_manager
        self.config = config or {}
        self.universe_resolver = universe_resolver
        self.name_resolver = name_resolver or (lambda e: e)

        self.running = False
        self._thread = None
        self._lock = threading.Lock()

        self.bars_5m: Dict[str, List[PaperBar]] = {}
        self.open_positions: Dict[str, PaperPosition] = {}
        self.closed_trades: List[PaperTrade] = []
        self.orders: List[PaperOrder] = []

        self.start_time: Optional[datetime] = None
        self.last_poll_time: Optional[datetime] = None
        self.last_bar_time: Optional[datetime] = None
        self.last_forecast: Dict[str, dict] = {}
        self.recent_actions: List[dict] = []
        self.n_signals = 0
        self.n_orders = 0
        self.n_fills = 0
        self.n_bar_fetches = 0
        self.daily_pnl: Dict[str, float] = {}
        self.equity_curve: List[dict] = []

        # Fallback: snapshot history per epic (used when IG /prices is rate-limited)
        self._snapshot_history: Dict[str, List[dict]] = {}

        self._load_state()

    # ─── State persistence ─────────────────────────
    def _save_state(self):
        try:
            state = {
                'bars_5m': {k: [asdict(b) for b in v[-2000:]]
                            for k, v in self.bars_5m.items()},
                'open_positions': {k: asdict(v) for k, v in self.open_positions.items()},
                'closed_trades': [asdict(t) for t in self.closed_trades[-2000:]],
                'orders': [asdict(o) for o in self.orders[-1000:]],
                'n_signals': self.n_signals,
                'n_orders': self.n_orders,
                'n_fills': self.n_fills,
                'start_time': str(self.start_time) if self.start_time else None,
                'last_poll_time': str(self.last_poll_time) if self.last_poll_time else None,
                'last_bar_time': str(self.last_bar_time) if self.last_bar_time else None,
                'daily_pnl': self.daily_pnl,
                'equity_curve': self.equity_curve[-5000:],
                'snapshot_history': {ep: hist[-200:] for ep, hist in self._snapshot_history.items()},
                'risk_state': {
                    'capital': self.risk.state.capital,
                    'peak_capital': self.risk.state.peak_capital,
                    'n_trades': self.risk.state.n_trades,
                    'n_wins': self.risk.state.n_wins,
                    'n_losses': self.risk.state.n_losses,
                } if self.risk else None,
            }
            with open(STATE_PATH, 'wb') as f:
                pickle.dump(state, f)
        except Exception as e:
            logger.error(f"Paper state save failed: {e}")

    def _load_state(self):
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH, 'rb') as f:
                state = pickle.load(f)
            self.bars_5m = {k: [PaperBar(**b) for b in v]
                            for k, v in state.get('bars_5m', {}).items()}
            self.open_positions = {k: PaperPosition(**v)
                                   for k, v in state.get('open_positions', {}).items()}
            self.closed_trades = [PaperTrade(**t)
                                 for t in state.get('closed_trades', [])]
            self.orders = [PaperOrder(**o) for o in state.get('orders', [])]
            self.n_signals = state.get('n_signals', 0)
            self.n_orders = state.get('n_orders', 0)
            self.n_fills = state.get('n_fills', 0)
            self.daily_pnl = state.get('daily_pnl', {})
            self.equity_curve = state.get('equity_curve', [])
            self._snapshot_history = state.get('snapshot_history', {})
            if state.get('start_time'):
                self.start_time = datetime.fromisoformat(state['start_time'])
            if state.get('last_poll_time'):
                self.last_poll_time = datetime.fromisoformat(state['last_poll_time'])
            if state.get('last_bar_time'):
                self.last_bar_time = datetime.fromisoformat(state['last_bar_time'])
            if self.risk and state.get('risk_state'):
                rs = state['risk_state']
                self.risk.state.capital = rs.get('capital', self.risk.state.initial_capital)
                self.risk.state.peak_capital = rs.get('peak_capital', self.risk.state.capital)
                self.risk.state.n_trades = rs.get('n_trades', 0)
                self.risk.state.n_wins = rs.get('n_wins', 0)
                self.risk.state.n_losses = rs.get('n_losses', 0)
            logger.info(f"Paper engine loaded: {len(self.bars_5m)} symbols with bars, "
                        f"{len(self._snapshot_history)} with snapshots, "
                        f"{len(self.closed_trades)} closed trades")
        except Exception as e:
            logger.error(f"Paper state load failed: {e}")

    # ─── Fallback bars: from live bid/offer snapshots ────
    def _bars_from_synthetic(self, epic: str):
        """DISABLED — no fake data. Returns None.

        The previous version of this engine built synthetic OHLC bars
        from a random walk when IG bid/offer was unavailable. That was
        a lie: those bars were not from any live source, and trades
        filled against them were not real.

        This method now returns None. The engine refuses to trade
        when IG bid/offer is unavailable. The /api/live/status endpoint
        reports the actual data source (ig_bars / ig_snapshot / none)
        and the reason no live data is available.
        """
        logger.debug(f"synthetic disabled for {epic} — would lie about prices")
        return None

    def _bars_from_snapshot(self, epic: str, num_points: int = 200):
        """Build bar history from live IG bid/offer snapshots.

        This IS real IG data — just sampled at a coarser rate (1
        snapshot per poll). Each poll captures the current IG bid/offer
        and appends a bar to an in-memory rolling window. The close
        is the actual IG mid; the high/low are the rolling-window
        extremes (no random walks). Volume is 0.
        """
        if not self.broker or not getattr(self.broker, 'connected', False):
            return None
        try:
            info = self.broker.get_market_info(epic)
        except Exception:
            return None
        if not info or not info.get('bid') or not info.get('offer'):
            return None
        mid = (info['bid'] + info['offer']) / 2
        now = pd.Timestamp.now()
        hist = self._snapshot_history.setdefault(epic, [])
        hist.append({'t': now, 'mid': mid, 'bid': info['bid'], 'offer': info['offer']})
        if len(hist) > 500:
            self._snapshot_history[epic] = hist[-500:]
        if len(hist) < 5:  # need at least 5 snapshots
            return None
        df = pd.DataFrame(hist).set_index('t')
        df['close'] = df['mid']
        df['open'] = df['close'].shift(1)
        # High/low = max/min of the rolling 3-snapshot window
        window = 3
        df['high'] = df['close'].rolling(window, min_periods=1).max()
        df['low'] = df['close'].rolling(window, min_periods=1).min()
        df['Volume'] = 0
        df = df.dropna()
        if df.empty or len(df) < 5:
            return None
        return df[['Open', 'High', 'Low', 'Close', 'Volume']]

    # ─── Bar ingestion (REAL IG data only) ─────────
    def fetch_and_append_bars(self, symbols: List[str]) -> int:
        """Fetch latest bars from IG. NO synthetic fallback.

        If IG is unavailable, the engine simply doesn't accumulate
        bars. With fewer than 30 bars, the forecast returns None and
        no signals are generated. The /api/live/status endpoint
        reports the data source (ig_bars / ig_snapshot / none).
        """
        new_bars_added = 0
        self._last_data_source = 'none'
        for epic in symbols:
            df = None
            try:
                df = self.bars_fn(epic)
            except Exception as e:
                logger.debug(f"bars_fn({epic}) failed: {e}")
            if df is not None and not df.empty:
                self._last_data_source = 'ig_bars'
            else:
                # Try the snapshot fallback (builds bars from live bid/offer)
                df = self._bars_from_snapshot(epic)
                if df is not None and not df.empty:
                    self._last_data_source = 'ig_snapshot'
            if df is None or df.empty:
                # No fake data. Just skip this epic.
                continue
            new_bars = []
            for ts, row in df.iterrows():
                new_bars.append(PaperBar(
                    timestamp=ts,
                    open=float(row['Open']),
                    high=float(row['High']),
                    low=float(row['Low']),
                    close=float(row['Close']),
                    volume=float(row.get('Volume', 0)),
                ))
            existing_ts = {b.timestamp for b in self.bars_5m.get(epic, [])}
            added = [b for b in new_bars if b.timestamp not in existing_ts]
            if epic not in self.bars_5m:
                self.bars_5m[epic] = list(new_bars[-2000:])
            else:
                self.bars_5m[epic].extend(added)
            if len(self.bars_5m[epic]) > 2000:
                self.bars_5m[epic] = self.bars_5m[epic][-2000:]
            new_bars_added += len(added)
        if new_bars_added > 0:
            self.last_bar_time = datetime.now()
        self.last_poll_time = datetime.now()
        self.n_bar_fetches += len(symbols)
        return new_bars_added

    # ─── Forecast ─────────────────────────────────
    def compute_forecast(self, symbol: str) -> Optional[dict]:
        bars = self.bars_5m.get(symbol, [])
        if len(bars) < 10:
            return None
        df = pd.DataFrame([asdict(b) for b in bars])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.set_index('timestamp').sort_index()
        df['range'] = df['high'] - df['low']
        # Adaptive MA windows: use min(actual_len // 2, 20) so we can
        # forecast with as few as 10 bars
        n = len(df)
        ma_fast = max(2, min(20, n // 2))
        ma_slow = max(3, min(50, n - 1))
        df['ma_fast'] = df['close'].rolling(ma_fast, min_periods=1).mean()
        df['ma_slow'] = df['close'].rolling(ma_slow, min_periods=1).mean()
        df['trend_up'] = (df['close'] > df['ma_fast']) & (df['ma_fast'] > df['ma_slow'])
        df['trend_down'] = (df['close'] < df['ma_fast']) & (df['ma_fast'] < df['ma_slow'])
        df['ret'] = df['close'].pct_change()
        df['ret_3bar'] = df['ret'].rolling(3, min_periods=1).sum()
        df['atr_20'] = df['range'].rolling(min(20, n), min_periods=1).mean()

        valid = df.dropna(subset=['trend_up', 'ret_3bar'])
        if len(valid) < 1:
            return None
        row = valid.iloc[-1]
        ret_3bar = float(row['ret_3bar'])
        direction = 'NEUTRAL'
        if bool(row['trend_up']) and ret_3bar > 0:
            direction = 'BULLISH'
        elif bool(row['trend_down']) and ret_3bar < 0:
            direction = 'BEARISH'

        return {
            'symbol': symbol,
            'display_name': self.name_resolver(symbol),
            'direction': direction,
            'trend_up': bool(row['trend_up']),
            'trend_down': bool(row['trend_down']),
            'ret_3bar': ret_3bar,
            'atr_20': float(row['atr_20']),
            'close': float(row['close']),
            'time': str(valid.index[-1]),
        }

    # ─── Order fill (paper) ───────────────────────
    def _fill_paper_order(self, symbol: str, direction: str, n_units: float,
                          stop: float, target: float, S: float,
                          forecast_direction: str, forecast_streak: float,
                          atr: float, last_bar) -> Optional[PaperOrder]:
        # PAPER MODE policy: only fill at REAL IG bid/offer. If IG is
        # closed or rate-limited, the order is REJECTED. No fake fills.
        # The /api/live/status endpoint will report why no live data.
        info = None
        if self.broker and getattr(self.broker, 'connected', False):
            try:
                info = self.broker.get_market_info(symbol)
            except Exception as e:
                logger.debug(f"get_market_info for fill failed: {e}")
        if not info or not info.get('bid') or not info.get('offer'):
            # Refuse to fill without a real quote
            order = PaperOrder(
                order_id=_next_id('P'),
                instrument=symbol,
                display_name=self.name_resolver(symbol),
                direction=direction,
                n_units=n_units,
                order_type='MARKET',
                status='REJECTED',
                created_at=datetime.now(),
                reject_reason='NO_LIVE_QUOTE',
            )
            self.orders.append(order)
            return order
        fill_price = float(info['offer'] if direction == 'BUY' else info['bid'])
        spread = float(info['offer']) - float(info['bid'])
        order = PaperOrder(
            order_id=_next_id('P'),
            instrument=symbol,
            display_name=self.name_resolver(symbol),
            direction=direction,
            n_units=n_units,
            order_type='MARKET',
            status='FILLED',
            created_at=datetime.now(),
            filled_at=datetime.now(),
            filled_price=fill_price,
            reference_price=fill_price,
            spread_at_fill=spread,
        )
        self.orders.append(order)
        self.n_orders += 1
        self.n_fills += 1
        pos = PaperPosition(
            instrument=symbol,
            display_name=self.name_resolver(symbol),
            direction=direction,
            entry_price=fill_price,
            entry_time=last_bar.timestamp,
            stop_price=stop,
            target_price=target,
            n_units=n_units,
            bars_held=0,
            exit_info={'forecast_direction': forecast_direction,
                       'forecast_streak': forecast_streak},
            entry_reason=(f"{forecast_direction} forecast, "
                          f"ret_3bar={forecast_streak:.5f}, ATR={atr:.5f}, "
                          f"filled @ {fill_price} ({fill_source}, spread {spread:.5f})"),
        )
        self.open_positions[symbol] = pos
        self.n_signals += 1
        action = {
            'symbol': symbol, 'display_name': self.name_resolver(symbol),
            'action': 'open', 'direction': direction,
            'entry': fill_price, 'stop': stop, 'target': target, 'n_units': n_units,
            'forecast': forecast_direction, 'streak': forecast_streak,
            'spread': spread, 'fill_source': fill_source,
            'reason': pos.entry_reason,
        }
        self.recent_actions.append(action)
        self.recent_actions = self.recent_actions[-100:]
        return order

    def _check_new_entry(self, symbol: str, forecast: dict) -> Optional[dict]:
        if forecast['direction'] == 'NEUTRAL':
            return None
        if not self.risk or not self.risk.can_trade():
            return None
        if not self.bars_5m.get(symbol):
            return None
        last_bar = self.bars_5m[symbol][-1]
        t = pd.Timestamp(last_bar.timestamp)
        if t.weekday() == 4 and t.hour >= 20:
            return None
        if t.weekday() == 0 and t.hour < 1:
            return None
        atr = forecast['atr_20']
        if atr <= 0:
            return None
        S = forecast['close']
        if forecast['direction'] == 'BULLISH':
            direction = 'BUY'
            stop = S - atr * self.config.get('stop_atr_mult', 1.0)
            target = S + atr * self.config.get('target_atr_mult', 2.0)
        else:
            direction = 'SELL'
            stop = S + atr * self.config.get('stop_atr_mult', 1.0)
            target = S - atr * self.config.get('target_atr_mult', 2.0)
        n_units = self.risk.size(S, stop)
        if n_units <= 0:
            return None
        order = self._fill_paper_order(
            symbol, direction, n_units, stop, target, S,
            forecast['direction'], forecast['ret_3bar'],
            atr, last_bar,
        )
        if order is None:
            return None
        # Write EVERY order (REJECTED + FILLED) to the persistent file
        # immediately so the History tab reflects what actually happened.
        self._append_order_to_file(order)
        if order.status == 'REJECTED':
            action = {
                'symbol': symbol, 'display_name': self.name_resolver(symbol),
                'action': 'rejected', 'direction': direction,
                'reason': order.reject_reason if order else 'no broker',
            }
            self.recent_actions.append(action)
            self.recent_actions = self.recent_actions[-100:]
            return action
        return action

    def _check_position_exit(self, symbol: str, forecast: dict) -> Optional[dict]:
        pos = self.open_positions[symbol]
        if not self.bars_5m.get(symbol):
            return None
        last_bar = self.bars_5m[symbol][-1]
        pos.bars_held += 1
        exit_type = None
        exit_price = None
        if pos.direction == 'BUY':
            if last_bar.low <= pos.stop_price:
                exit_type, exit_price = 'stop', pos.stop_price
            elif last_bar.high >= pos.target_price:
                exit_type, exit_price = 'target', pos.target_price
        else:
            if last_bar.high >= pos.stop_price:
                exit_type, exit_price = 'stop', pos.stop_price
            elif last_bar.low <= pos.target_price:
                exit_type, exit_price = 'target', pos.target_price
        max_hold = self.config.get('max_hold_bars', 12)
        if exit_type is None and pos.bars_held >= max_hold:
            exit_type, exit_price = 'time', last_bar.close
        if exit_type is None:
            if pos.direction == 'BUY':
                pos.unrealized_pnl = (last_bar.close - pos.entry_price) * pos.n_units
            else:
                pos.unrealized_pnl = (pos.entry_price - last_bar.close) * pos.n_units
            return {'symbol': symbol, 'action': 'hold',
                    'unrealized_pnl': pos.unrealized_pnl}
        if pos.direction == 'BUY':
            pnl = (exit_price - pos.entry_price) * pos.n_units
        else:
            pnl = (pos.entry_price - exit_price) * pos.n_units
        trade = PaperTrade(
            trade_id=_next_id('T'),
            instrument=symbol,
            display_name=pos.display_name,
            direction=pos.direction,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=last_bar.timestamp,
            exit_price=exit_price,
            n_units=pos.n_units,
            pnl=pnl,
            won=pnl > 0,
            exit_type=exit_type,
            bars_held=pos.bars_held,
            entry_forecast=pos.exit_info.get('forecast_direction', 'BULLISH'),
            entry_streak=pos.exit_info.get('forecast_streak', 0),
            entry_atr=0,
            entry_reason=pos.entry_reason,
        )
        self.closed_trades.append(trade)
        if self.risk:
            day_str = (str(last_bar.timestamp.date())
                       if hasattr(last_bar.timestamp, 'date')
                       else str(last_bar.timestamp)[:10])
            self.risk.record_trade(pnl, day_str)
            self.daily_pnl[day_str] = self.daily_pnl.get(day_str, 0) + pnl
        if self.risk:
            self.equity_curve.append({
                't': str(last_bar.timestamp),
                'equity': self.risk.state.capital,
                'pnl': pnl,
                'peak': self.risk.state.peak_capital,
            })
        # Write trade to the persistent file immediately
        self._append_trade_to_file(trade)
        del self.open_positions[symbol]
        action = {
            'symbol': symbol, 'display_name': pos.display_name,
            'action': 'close', 'exit_type': exit_type, 'pnl': pnl,
        }
        self.recent_actions.append(action)
        self.recent_actions = self.recent_actions[-100:]
        return action

    def _append_trade_to_file(self, trade: PaperTrade):
        try:
            if os.path.exists(TRADE_HISTORY_PATH):
                with open(TRADE_HISTORY_PATH, 'rb') as f:
                    history = pickle.load(f)
            else:
                history = []
            record = {
                'trade_id': trade.trade_id,
                'instrument': trade.instrument,
                'display_name': trade.display_name,
                'direction': trade.direction,
                'entry_time': str(trade.entry_time),
                'entry_price': trade.entry_price,
                'exit_time': str(trade.exit_time),
                'exit_price': trade.exit_price,
                'n_units': trade.n_units,
                'pnl': trade.pnl,
                'won': trade.won,
                'exit_type': trade.exit_type,
                'bars_held': trade.bars_held,
                'forecast_direction': trade.entry_forecast,
                'forecast_streak': trade.entry_streak,
                'entry_reason': trade.entry_reason,
            }
            if not any(h.get('trade_id') == record['trade_id'] for h in history):
                history.append(record)
            history = history[-10000:]
            with open(TRADE_HISTORY_PATH, 'wb') as f:
                pickle.dump(history, f)
        except Exception as e:
            logger.error(f"append trade to file failed: {e}")

    def _append_order_to_file(self, order: PaperOrder):
        try:
            if os.path.exists(ORDER_HISTORY_PATH):
                with open(ORDER_HISTORY_PATH, 'rb') as f:
                    history = pickle.load(f)
            else:
                history = []
            record = {
                'order_id': order.order_id,
                'instrument': order.instrument,
                'display_name': order.display_name,
                'direction': order.direction,
                'n_units': order.n_units,
                'order_type': order.order_type,
                'status': order.status,
                'created_at': str(order.created_at),
                'filled_at': str(order.filled_at) if order.filled_at else None,
                'filled_price': order.filled_price,
                'reject_reason': order.reject_reason,
            }
            if not any(h.get('order_id') == record['order_id'] for h in history):
                history.append(record)
            history = history[-10000:]
            with open(ORDER_HISTORY_PATH, 'wb') as f:
                pickle.dump(history, f)
        except Exception as e:
            logger.error(f"append order to file failed: {e}")

    def process_symbol(self, symbol: str) -> Optional[dict]:
        forecast = self.compute_forecast(symbol)
        if forecast is None:
            return None
        self.last_forecast[symbol] = forecast
        if symbol in self.open_positions:
            return self._check_position_exit(symbol, forecast)
        return self._check_new_entry(symbol, forecast)

    def force_poll(self, symbols: List[str] = None) -> dict:
        with self._lock:
            if symbols is None:
                symbols = self.universe_resolver() if self.universe_resolver else list(self.bars_5m.keys()) or []
            n_new = self.fetch_and_append_bars(symbols)
            results = []
            for sym in symbols:
                try:
                    r = self.process_symbol(sym)
                    if r:
                        results.append(r)
                except Exception as e:
                    logger.error(f"paper process({sym}) failed: {e}")
            self._save_state()
            return {
                'new_bars': n_new,
                'symbols_polled': len(symbols),
                'actions': results,
                'n_open': len(self.open_positions),
                'n_closed': len(self.closed_trades),
            }

    def start(self, symbols: List[str], poll_interval_seconds: int = 60):
        if self.running:
            return
        self.running = True
        self.start_time = datetime.now()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(symbols, poll_interval_seconds),
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Paper engine started: {len(symbols)} symbols, interval={poll_interval_seconds}s")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._save_state()
        logger.info("Paper engine stopped")

    def _run_loop(self, symbols: List[str], interval: int):
        first_run = True
        while self.running:
            try:
                with self._lock:
                    n_new = self.fetch_and_append_bars(symbols)
                    logger.info(f"Paper poll: n_new={n_new} symbols={len(self.bars_5m)} "
                                f"bars_per={list(self.bars_5m.keys())[:2]}")
                    for sym in symbols:
                        if first_run or n_new > 0 or sym in self.open_positions:
                            try:
                                result = self.process_symbol(sym)
                                if result and result.get('action') in ('open', 'close'):
                                    logger.info(f"Paper {result['action']}: "
                                                f"{self.name_resolver(sym)} "
                                                f"pnl={result.get('pnl', 0):+.2f}")
                            except Exception as e:
                                logger.error(f"paper process({sym}) failed: {e}")
                    first_run = False
                    self._save_state()
            except Exception as e:
                logger.error(f"Paper loop error: {e}")
            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

    def get_status(self) -> dict:
        with self._lock:
            risk_snap = self.risk.snapshot() if self.risk else {}
            # Determine the most recent data source used
            ds = getattr(self, '_last_data_source', 'none')
            return {
                'mode': 'paper',
                'data_source': ds,
                'running': self.running,
                'start_time': str(self.start_time) if self.start_time else None,
                'uptime_seconds': (datetime.now() - self.start_time).total_seconds() if self.start_time else 0,
                'last_poll_time': str(self.last_poll_time) if self.last_poll_time else None,
                'last_bar_time': str(self.last_bar_time) if self.last_bar_time else None,
                'symbols_tracked': list(self.bars_5m.keys()),
                'bars_per_symbol': {self.name_resolver(k): len(v) for k, v in self.bars_5m.items()},
                'n_open_positions': len(self.open_positions),
                'n_closed_trades': len(self.closed_trades),
                'n_signals': self.n_signals,
                'n_orders': self.n_orders,
                'n_fills': self.n_fills,
                'n_bar_fetches': self.n_bar_fetches,
                'forecasts': {self.name_resolver(k): v for k, v in self.last_forecast.items()},
                'recent_actions': self.recent_actions[-20:],
                'broker_connected': getattr(self.broker, 'connected', False) if self.broker else False,
                'broker_account_type': getattr(self.broker, 'acc_type', 'N/A') if self.broker else 'N/A',
                'capital': risk_snap.get('capital', 10000),
                'initial_capital': risk_snap.get('initial_capital', 10000),
                'peak': risk_snap.get('peak', 10000),
                'last_dd_pct': risk_snap.get('last_dd_pct', 0),
                'max_dd_pct': risk_snap.get('max_dd_pct', 0),
                'paused': risk_snap.get('paused', False),
                'risk': risk_snap,
            }

    def get_open_positions(self) -> List[dict]:
        with self._lock:
            return [{
                'instrument': p.instrument,
                'display_name': p.display_name,
                'direction': p.direction,
                'entry_price': p.entry_price,
                'entry_time': str(p.entry_time),
                'stop_price': p.stop_price,
                'target_price': p.target_price,
                'n_units': p.n_units,
                'bars_held': p.bars_held,
                'unrealized_pnl': p.unrealized_pnl,
                'entry_reason': p.entry_reason,
            } for p in self.open_positions.values()]

    def get_recent_trades(self, n: int = 50) -> List[dict]:
        with self._lock:
            return [{
                'trade_id': t.trade_id,
                'instrument': t.instrument,
                'display_name': t.display_name,
                'direction': t.direction,
                'entry_time': str(t.entry_time),
                'entry_price': t.entry_price,
                'exit_time': str(t.exit_time),
                'exit_price': t.exit_price,
                'n_units': t.n_units,
                'pnl': t.pnl,
                'won': t.won,
                'exit_type': t.exit_type,
                'bars_held': t.bars_held,
                'forecast_direction': t.entry_forecast,
                'entry_reason': t.entry_reason,
            } for t in self.closed_trades[-n:]]
