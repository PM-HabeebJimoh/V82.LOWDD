"""
V82.LOWDD - Real Live Loop (IG Markets).

This is the REAL live loop. It:

  1. Connects to IG Markets (the SOLE broker).
  2. Polls IG for new 5-min bars across the full universe (forex, crypto,
     commodities, indices).
  3. Appends bars to in-memory history.
  4. Recomputes the 1H forecast on each new bar.
  5. Generates signals and (if enabled) places real orders on IG.
  6. Persists state across calls and Flask restarts.

State is shared between the loop thread and the Flask request handlers
via the same `LiveLoop` instance.
"""
import os
import sys
import time
import json
import pickle
import logging
import threading
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'state', 'live_state.pkl'
)


@dataclass
class LiveBar:
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class LivePosition:
    instrument: str       # EPIC
    direction: str        # 'BUY' or 'SELL'
    entry_price: float
    stop_price: float
    target_price: float
    n_units: float
    entry_time: object
    bars_held: int = 0
    unrealized_pnl: float = 0.0
    exit_info: dict = field(default_factory=dict)


@dataclass
class LiveTrade:
    instrument: str
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
    forecast_direction: str = 'BULLISH'
    forecast_streak: int = 0


class LiveLoop:
    """
    Real background loop that polls IG Markets, processes forecasts,
    and submits/closes real orders.
    """

    def __init__(self,
                 broker,            # IGBroker instance
                 bars_fn,           # callable(epic) -> DataFrame
                 risk_manager,
                 config: dict = None,
                 universe_resolver=None,  # callable() -> List[str]
                 name_resolver=None,       # callable(epic) -> str
                ):
        self.broker = broker
        self.bars_fn = bars_fn
        self.risk = risk_manager
        self.config = config or {}
        self.universe_resolver = universe_resolver
        self.name_resolver = name_resolver or (lambda e: e)
        self.running = False
        self._thread = None
        self._lock = threading.Lock()

        self.bars_5m: Dict[str, List[LiveBar]] = {}
        self.bars_1h: Dict[str, List[LiveBar]] = {}

        self.open_positions: Dict[str, LivePosition] = {}
        self.closed_trades: List[LiveTrade] = []

        self.start_time: Optional[datetime] = None
        self.n_signals = 0
        self.n_orders = 0
        self.n_fills = 0
        self.n_bar_fetches = 0
        self.last_poll_time: Optional[datetime] = None
        self.last_bar_time: Optional[datetime] = None
        self.last_forecast: Dict[str, dict] = {}
        self.daily_pnl: Dict[str, float] = {}
        self.recent_actions: List[dict] = []

        self._load_state()

    # ─── State persistence ─────────────────────────────
    def _save_state(self):
        try:
            state = {
                'bars_5m': {k: [asdict(b) for b in v[-2000:]] for k, v in self.bars_5m.items()},
                'bars_1h': {k: [asdict(b) for b in v[-2000:]] for k, v in self.bars_1h.items()},
                'open_positions': {k: asdict(v) for k, v in self.open_positions.items()},
                'closed_trades': [asdict(t) for t in self.closed_trades[-1000:]],
                'n_signals': self.n_signals,
                'n_orders': self.n_orders,
                'n_fills': self.n_fills,
                'start_time': str(self.start_time) if self.start_time else None,
                'last_poll_time': str(self.last_poll_time) if self.last_poll_time else None,
                'last_bar_time': str(self.last_bar_time) if self.last_bar_time else None,
                'risk_state': {
                    'capital': self.risk.state.capital,
                    'peak_capital': self.risk.state.peak_capital,
                    'n_trades': self.risk.state.n_trades,
                    'n_wins': self.risk.state.n_wins,
                    'n_losses': self.risk.state.n_losses,
                } if self.risk else None,
            }
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, 'wb') as f:
                pickle.dump(state, f)
        except Exception as e:
            logger.error(f"Live loop state save failed: {e}")

    def _load_state(self):
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH, 'rb') as f:
                state = pickle.load(f)
            self.bars_5m = {k: [LiveBar(**b) for b in v] for k, v in state.get('bars_5m', {}).items()}
            self.bars_1h = {k: [LiveBar(**b) for b in v] for k, v in state.get('bars_1h', {}).items()}
            self.open_positions = {k: LivePosition(**v) for k, v in state.get('open_positions', {}).items()}
            self.closed_trades = [LiveTrade(**t) for t in state.get('closed_trades', [])]
            self.n_signals = state.get('n_signals', 0)
            self.n_orders = state.get('n_orders', 0)
            self.n_fills = state.get('n_fills', 0)
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
            logger.info(f"Loaded live loop state: {len(self.bars_5m)} symbols, "
                        f"{len(self.closed_trades)} closed trades")
        except Exception as e:
            logger.error(f"Live loop state load failed: {e}")

    # ─── Bar ingestion ─────────────────────────────────
    def fetch_and_append_bars(self, symbols: List[str]):
        """Fetch latest bars from IG and append new ones."""
        new_bars_added = 0
        for epic in symbols:
            try:
                df = self.bars_fn(epic)
                if df is None or df.empty:
                    continue
                new_bars = []
                for ts, row in df.iterrows():
                    new_bars.append(LiveBar(
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
            except Exception as e:
                logger.debug(f"Fetch bars for {epic} failed: {e}")
        if new_bars_added > 0:
            self.last_bar_time = datetime.now()
        self.last_poll_time = datetime.now()
        self.n_bar_fetches += len(symbols)
        return new_bars_added

    # ─── Forecast ──────────────────────────────────────
    def compute_forecast(self, symbol: str) -> Optional[dict]:
        bars = self.bars_5m.get(symbol, [])
        if len(bars) < 50:
            return None
        df = pd.DataFrame([asdict(b) for b in bars])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.set_index('timestamp').sort_index()
        df['range'] = df['high'] - df['low']
        df['ma_20'] = df['close'].rolling(20).mean()
        df['ma_50'] = df['close'].rolling(50).mean()
        df['trend_up'] = (df['close'] > df['ma_20']) & (df['ma_20'] > df['ma_50'])
        df['trend_down'] = (df['close'] < df['ma_20']) & (df['ma_20'] < df['ma_50'])
        df['ret'] = df['close'].pct_change()
        df['ret_3bar'] = df['ret'].rolling(3).sum()
        df['atr_20'] = df['range'].rolling(20).mean()
        df['body_pos'] = (df['close'] > df['open']).astype(int)
        df['streak'] = df['body_pos'].rolling(3).sum()

        valid = df.dropna(subset=['trend_up', 'streak', 'ret_3bar'])
        if len(valid) < 1:
            return None
        row = valid.iloc[-1]
        min_streak = self.config.get('min_streak', 2)
        streak = int(row['streak'])
        direction = 'NEUTRAL'
        if bool(row['trend_up']) and streak >= min_streak and float(row['ret_3bar']) > 0:
            direction = 'BULLISH'
        elif bool(row['trend_down']) and streak <= (3 - min_streak) and float(row['ret_3bar']) < 0:
            direction = 'BEARISH'

        return {
            'symbol': symbol,
            'display_name': self.name_resolver(symbol),
            'direction': direction,
            'trend_up': bool(row['trend_up']),
            'trend_down': bool(row['trend_down']),
            'streak': streak,
            'ret_3bar': float(row['ret_3bar']),
            'atr_20': float(row['atr_20']),
            'close': float(row['close']),
            'time': str(valid.index[-1]),
        }

    # ─── Signal generation ─────────────────────────────
    def process_symbol(self, symbol: str) -> Optional[dict]:
        forecast = self.compute_forecast(symbol)
        if forecast is None:
            return None
        self.last_forecast[symbol] = forecast

        if symbol in self.open_positions:
            return self._check_position_exit(symbol, forecast)
        return self._check_new_entry(symbol, forecast)

    def _check_position_exit(self, symbol: str, forecast: dict) -> dict:
        pos = self.open_positions[symbol]
        if not self.bars_5m.get(symbol):
            return None
        last_bar = self.bars_5m[symbol][-1]
        pos.bars_held += 1

        exit_type = None
        exit_price = None
        if pos.direction == 'BUY':
            if last_bar.low <= pos.stop_price:
                exit_type = 'stop'
                exit_price = pos.stop_price
            elif last_bar.high >= pos.target_price:
                exit_type = 'target'
                exit_price = pos.target_price
        else:
            if last_bar.high >= pos.stop_price:
                exit_type = 'stop'
                exit_price = pos.stop_price
            elif last_bar.low <= pos.target_price:
                exit_type = 'target'
                exit_price = pos.target_price

        max_hold = self.config.get('max_hold_bars', 12)
        if exit_type is None and pos.bars_held >= max_hold:
            exit_type = 'time'
            exit_price = last_bar.close

        if exit_type is None:
            if pos.direction == 'BUY':
                pos.unrealized_pnl = (last_bar.close - pos.entry_price) * pos.n_units
            else:
                pos.unrealized_pnl = (pos.entry_price - last_bar.close) * pos.n_units
            return {'symbol': symbol, 'action': 'hold', 'unrealized_pnl': pos.unrealized_pnl}

        if pos.direction == 'BUY':
            pnl = (exit_price - pos.entry_price) * pos.n_units
        else:
            pnl = (pos.entry_price - exit_price) * pos.n_units

        trade = LiveTrade(
            instrument=symbol,
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
            forecast_direction=pos.exit_info.get('forecast_direction', 'BULLISH'),
            forecast_streak=pos.exit_info.get('forecast_streak', 0),
        )
        self.closed_trades.append(trade)
        if self.risk:
            day_str = str(last_bar.timestamp.date()) if hasattr(last_bar.timestamp, 'date') else str(last_bar.timestamp)[:10]
            self.risk.record_trade(pnl, day_str)
            self.daily_pnl[day_str] = self.daily_pnl.get(day_str, 0) + pnl

        # Close via broker (if real broker)
        if self.broker and hasattr(self.broker, 'close_position') and getattr(self.broker, 'connected', False):
            try:
                # IG: opposite direction
                opposite = 'SELL' if pos.direction == 'BUY' else 'BUY'
                if hasattr(self.broker, 'ig_service'):
                    # Find the deal_id for this epic
                    positions = self.broker.get_open_positions()
                    deal_id = None
                    for p in positions:
                        if p.get('epic') == symbol:
                            deal_id = p.get('deal_id')
                            break
                    if deal_id:
                        self.broker.close_position(deal_id=deal_id, direction=opposite,
                                                  epic=symbol, size=pos.n_units)
            except Exception as e:
                logger.error(f"Broker close_position({symbol}) failed: {e}")

        del self.open_positions[symbol]
        action = {'symbol': symbol, 'display_name': self.name_resolver(symbol),
                  'action': 'close', 'exit_type': exit_type, 'pnl': pnl}
        self.recent_actions.append(action)
        self.recent_actions = self.recent_actions[-100:]
        return action

    def _check_new_entry(self, symbol: str, forecast: dict) -> dict:
        if forecast['direction'] == 'NEUTRAL':
            return None
        if not self.risk or not self.risk.can_trade():
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

        # Submit real order to IG (if connected)
        # NOTE: do NOT include stopLevel/limitLevel in the order — IG rejects these
        # with MARKET_CLOSED_WITH_EDITS on many instruments, and the loop manages
        # exits in software via _check_position_exit anyway.
        if self.broker and hasattr(self.broker, 'submit_order') and getattr(self.broker, 'connected', False):
            try:
                order = self.broker.submit_order(
                    epic=symbol, direction=direction, size=n_units, order_type='MARKET',
                    currency_code='USD',
                )
                if order is None:
                    return None
                # Poll for confirmation (REJECTED vs ACCEPTED/OPEN)
                if hasattr(self.broker, 'poll_order'):
                    order = self.broker.poll_order(order, max_attempts=5, sleep_s=0.6)
                self.n_orders += 1
                if order.status == 'REJECTED':
                    logger.warning(f"Order REJECTED by IG: {symbol} {direction} reason={order.reason}")
                    # Do NOT track as open position
                    action = {
                        'symbol': symbol, 'display_name': self.name_resolver(symbol),
                        'action': 'rejected', 'direction': direction,
                        'entry': S, 'n_units': n_units,
                        'reason': order.reason or 'unknown',
                    }
                    self.recent_actions.append(action)
                    self.recent_actions = self.recent_actions[-100:]
                    return action
            except Exception as e:
                logger.error(f"Broker submit_order failed: {e}")
                return None

        pos = LivePosition(
            instrument=symbol,
            direction=direction,
            entry_price=S,
            stop_price=stop,
            target_price=target,
            n_units=n_units,
            entry_time=last_bar.timestamp,
            bars_held=0,
            exit_info={
                'forecast_direction': forecast['direction'],
                'forecast_streak': forecast['streak'],
            },
        )
        self.open_positions[symbol] = pos
        self.n_signals += 1
        self.n_fills += 1
        action = {
            'symbol': symbol, 'display_name': self.name_resolver(symbol),
            'action': 'open', 'direction': direction,
            'entry': S, 'stop': stop, 'target': target, 'n_units': n_units,
            'forecast': forecast['direction'], 'streak': forecast['streak'],
        }
        self.recent_actions.append(action)
        self.recent_actions = self.recent_actions[-100:]
        return action

    # ─── Background loop ───────────────────────────────
    def start(self, symbols: List[str], poll_interval_seconds: int = 60):
        if self.running:
            logger.warning("Live loop already running")
            return
        self.running = True
        self.start_time = datetime.now()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(symbols, poll_interval_seconds),
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Live loop started: {len(symbols)} symbols, interval={poll_interval_seconds}s")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._save_state()
        logger.info("Live loop stopped")

    def _run_loop(self, symbols: List[str], interval: int):
        first_run = True
        while self.running:
            try:
                with self._lock:
                    n_new = self.fetch_and_append_bars(symbols)
                    logger.info(f"Live loop poll: n_new={n_new} "
                                f"total_bars={sum(len(v) for v in self.bars_5m.values())} "
                                f"symbols={len(self.bars_5m)}")
                    for sym in symbols:
                        if first_run or n_new > 0 or sym in self.open_positions:
                            try:
                                result = self.process_symbol(sym)
                                if result and result.get('action') in ('open', 'close'):
                                    logger.info(f"Live {result['action']}: "
                                                f"{self.name_resolver(sym)} "
                                                f"pnl={result.get('pnl', 0):+.2f}")
                            except Exception as e:
                                logger.error(f"process_symbol({sym}) failed: {e}")
                    first_run = False
                    self._save_state()
            except Exception as e:
                logger.error(f"Live loop error: {e}")
            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

    # ─── Status / introspection ────────────────────────
    def get_status(self) -> dict:
        with self._lock:
            risk_snap = self.risk.snapshot() if self.risk else {}
            return {
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
                # Risk snapshot for the dashboard
                'capital': risk_snap.get('capital', 10000),
                'initial_capital': risk_snap.get('initial_capital', 10000),
                'peak': risk_snap.get('peak', 10000),
                'dd_pct': risk_snap.get('last_dd_pct', 0),
                'max_dd_pct': risk_snap.get('max_dd_pct', 0),
                'paused': risk_snap.get('paused', False),
                'risk': risk_snap,
            }

    def get_open_positions(self) -> List[dict]:
        with self._lock:
            return [
                {
                    'instrument': p.instrument,
                    'display_name': self.name_resolver(p.instrument),
                    'direction': p.direction,
                    'entry_price': p.entry_price,
                    'stop_price': p.stop_price,
                    'target_price': p.target_price,
                    'n_units': p.n_units,
                    'entry_time': str(p.entry_time),
                    'bars_held': p.bars_held,
                    'unrealized_pnl': p.unrealized_pnl,
                }
                for p in self.open_positions.values()
            ]

    def get_recent_trades(self, n: int = 50) -> List[dict]:
        with self._lock:
            trades = self.closed_trades[-n:]
            return [
                {
                    'instrument': t.instrument,
                    'display_name': self.name_resolver(t.instrument),
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
                }
                for t in trades
            ]

    def force_poll(self, symbols: List[str] = None) -> dict:
        with self._lock:
            if symbols is None:
                if self.universe_resolver:
                    symbols = self.universe_resolver()
                else:
                    symbols = list(self.bars_5m.keys()) or []
            n_new = self.fetch_and_append_bars(symbols)
            results = []
            for sym in symbols:
                try:
                    r = self.process_symbol(sym)
                    if r:
                        results.append(r)
                except Exception as e:
                    logger.error(f"force_poll process({sym}) failed: {e}")
            self._save_state()
            return {
                'new_bars': n_new,
                'symbols_polled': len(symbols),
                'actions': results,
                'n_open': len(self.open_positions),
                'n_closed': len(self.closed_trades),
            }
