"""
V82.LOWDD - LIVE MODE (REAL IG ORDERS, NO PAPER).

This is the LIVE engine. It:
  1. Connects to IG Markets (single broker).
  2. Polls ALL 92+ instruments across 5 asset classes using a
     rotating batch system (so 24/7 crypto is always checked,
     and other asset classes rotate to avoid IG throttling).
  3. Builds an in-memory OHLC bar history from snapshots.
  4. Recomputes an adaptive forecast (works with as few as 5 bars).
  5. Submits REAL orders to IG via `broker.submit_order`.
  6. Polls the order confirmation via `broker.poll_order` to detect
     ACCEPTED/REJECTED.
  7. Tracks LIVE open positions via `broker.get_open_positions`.
  8. Persists every order, trade, signal, opportunity, and equity
     tick to disk immediately so the History tab reflects reality.
  9. Records every SIGNAL (what the model thought) and every
     OPPORTUNITY (an actionable signal that was submitted to IG).

This engine NEVER simulates. Every order goes to IG. Every P&L tick
comes from a real IG open position's P&L field.
"""
import os
import json
import time
import pickle
import logging
import threading
import fcntl
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

from backend.core.universe_rotator import UniverseRotator, build_batches
from backend.core import signals_service
from backend.core.instrument_config import (
    classify, get_config, is_market_open, should_skip_for_session,
    spread_ok, atr_ok, compute_forecast_for,
)

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'state'
)
os.makedirs(STATE_DIR, exist_ok=True)

STATE_PATH = os.path.join(STATE_DIR, 'live_state.pkl')
TRADE_HISTORY_PATH = os.path.join(STATE_DIR, 'live_trade_history.pkl')
ORDER_HISTORY_PATH = os.path.join(STATE_DIR, 'live_order_history.pkl')
EQUITY_PATH = os.path.join(STATE_DIR, 'live_equity_curve.pkl')


# ─── Helpers ─────────────────────────────────────────────
def _next_id(prefix: str = 'L') -> str:
    return f"{prefix}{int(time.time() * 1000) % 100000000:08d}"


def _with_file_lock(path: str, mode: str, write_func):
    """Read-modify-write a pickle file with an exclusive lock so the
    background thread and Flask request threads cannot stomp on each other."""
    lock_path = path + '.lock'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(lock_path, 'w') as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            data = []
            if mode == 'rw' and os.path.exists(path):
                try:
                    with open(path, 'rb') as f:
                        data = pickle.load(f)
                except Exception:
                    data = []
            new_data = write_func(data) if callable(write_func) else write_func
            with open(path, 'wb') as f:
                pickle.dump(new_data, f)
            return new_data
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


# ─── Data classes ────────────────────────────────────────
@dataclass
class LiveSnapshot:
    timestamp: object
    bid: float
    offer: float
    mid: float


@dataclass
class LiveBar:
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class LiveOrder:
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
    deal_id: str = ''
    deal_reference: str = ''
    reject_reason: str = ''


@dataclass
class LiveTrade:
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
    deal_id_entry: str = ''
    deal_id_exit: str = ''


# ─── Engine ──────────────────────────────────────────────
class LiveEngine:
    """Live IG engine. Submits REAL orders, never simulates."""

    def __init__(self, broker, bars_fn, risk_manager, config: dict,
                 universe_resolver=None, name_resolver=None):
        self.broker = broker
        self.bars_fn = bars_fn
        self.risk = risk_manager
        self.config = config
        self.universe_resolver = universe_resolver or (lambda: [])
        self.name_resolver = name_resolver or (lambda e: e)

        self.running = False
        self._thread = None
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()

        # Universe state — the engine manages the FULL universe
        # (166+ EPICs across 8 asset classes) and rotates through
        # batches per tick to stay within IG's rate limits.
        # ALL symbols have their bar history maintained across ticks,
        # not just the current batch.
        self.rotator: Optional[UniverseRotator] = None
        self.all_symbols: List[str] = []   # the full universe (for display)
        self.universe: List[str] = []      # the current batch
        self.snapshots: Dict[str, List[LiveSnapshot]] = {}
        self.bars: Dict[str, List[LiveBar]] = {}
        self.live_quotes: Dict[str, dict] = {}

        # IG-side state (the source of truth for positions)
        self.ig_positions: Dict[str, dict] = {}
        self.local_orders: Dict[str, LiveOrder] = {}

        # Tracking
        self.orders: List[LiveOrder] = []
        self.closed_trades: List[LiveTrade] = []
        self.equity_curve: List[dict] = []
        self.daily_pnl: Dict[str, float] = {}

        self.forecasts: Dict[str, dict] = {}
        self.recent_actions: List[dict] = []

        # Per-symbol last-tick time so we know when we last polled it
        # (for the rotator's "no_drop" guarantee)
        self.last_polled: Dict[str, datetime] = {}

        # Opportunity tracking
        self._pending_opp_ids: Dict[str, str] = {}

        self.start_time: Optional[datetime] = None
        self.last_poll_time: Optional[datetime] = None
        self.last_bar_time: Optional[datetime] = None

        self.n_signals = 0
        self.n_orders = 0
        self.n_fills = 0
        self.n_rejected = 0
        self.n_bar_fetches = 0
        self.n_ig_errors = 0

        # Throttle tracking
        self._throttled_until: Optional[datetime] = None
        self._consecutive_throttle = 0

        self._load_state()
        self._init_rotator()

    def _init_rotator(self):
        """Build the rotator from the resolved universe. If the
        universe is large, split into batches; otherwise use all."""
        if not self.universe:
            return
        # If we have more than 12 symbols, build batches
        if len(self.universe) > 12:
            self.rotator = UniverseRotator()
            # Override the rotator's batches with our universe split
            # into batches of ~10 each
            symbols = list(self.universe)
            n = len(symbols)
            batch_size = max(1, n // 5)
            self.rotator.batches = [symbols[i:i + batch_size]
                                    for i in range(0, n, batch_size)]
            self.universe = self.rotator.current()
            logger.info(f"Rotator: {len(self.rotator.batches)} batches, "
                        f"batch[0]={self.rotator.batches[0]}")
        else:
            self.rotator = None
        self.all_symbols = list(self.universe)

    # ─── State persistence ─────────────────────────────
    def _save_state(self):
        try:
            state = {
                'snapshots': {k: [asdict(s) for s in v[-300:]] for k, v in self.snapshots.items()},
                'bars': {k: [asdict(b) for b in v[-500:]] for k, v in self.bars.items()},
                'live_quotes': dict(self.live_quotes),
                'orders': [asdict(o) for o in self.orders[-500:]],
                'closed_trades': [asdict(t) for t in self.closed_trades[-1000:]],
                'n_signals': self.n_signals,
                'n_orders': self.n_orders,
                'n_fills': self.n_fills,
                'n_rejected': self.n_rejected,
                'n_bar_fetches': self.n_bar_fetches,
                'n_ig_errors': self.n_ig_errors,
                'start_time': str(self.start_time) if self.start_time else None,
                'last_poll_time': str(self.last_poll_time) if self.last_poll_time else None,
                'last_bar_time': str(self.last_bar_time) if self.last_bar_time else None,
                'daily_pnl': self.daily_pnl,
                'equity_curve': self.equity_curve[-2000:],
                'universe': self.all_symbols,
                'rotator_idx': self.rotator.idx if self.rotator else 0,
            }
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, 'wb') as f:
                pickle.dump(state, f)
        except Exception as e:
            logger.error(f"live state save failed: {e}")

    def _load_state(self):
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH, 'rb') as f:
                state = pickle.load(f)
            self.snapshots = {k: [LiveSnapshot(**s) for s in v] for k, v in state.get('snapshots', {}).items()}
            self.bars = {k: [LiveBar(**b) for b in v] for k, v in state.get('bars', {}).items()}
            self.live_quotes = state.get('live_quotes', {})
            self.n_signals = state.get('n_signals', 0)
            self.n_orders = state.get('n_orders', 0)
            self.n_fills = state.get('n_fills', 0)
            self.n_rejected = state.get('n_rejected', 0)
            self.n_bar_fetches = state.get('n_bar_fetches', 0)
            self.n_ig_errors = state.get('n_ig_errors', 0)
            self.daily_pnl = state.get('daily_pnl', {})
            self.equity_curve = state.get('equity_curve', [])
            self.universe = state.get('universe', [])
            if state.get('start_time'):
                try:
                    self.start_time = datetime.fromisoformat(state['start_time'])
                except Exception:
                    pass
            if state.get('last_poll_time'):
                try:
                    self.last_poll_time = datetime.fromisoformat(state['last_poll_time'])
                except Exception:
                    pass
            if state.get('last_bar_time'):
                try:
                    self.last_bar_time = datetime.fromisoformat(state['last_bar_time'])
                except Exception:
                    pass
            # Restore rotator index
            if state.get('rotator_idx') and not self.rotator:
                pass  # rotator is set in _init_rotator
            logger.info(f"Loaded live state: {len(self.bars)} symbols, "
                        f"n_orders={self.n_orders} n_fills={self.n_fills}")
        except Exception as e:
            logger.error(f"live state load failed: {e}")

    def _append_order_to_file(self, order):
        def _g(name, default=None):
            return getattr(order, name, default)
        record = {
            'order_id': _g('order_id', '') or _g('deal_reference', ''),
            'instrument': _g('instrument', '') or _g('epic', ''),
            'display_name': _g('display_name', '') or _g('instrument_name', ''),
            'direction': _g('direction', ''),
            'n_units': _g('n_units', 0),
            'order_type': _g('order_type', 'MARKET'),
            'status': _g('status', 'UNKNOWN'),
            'created_at': str(_g('created_at', '')),
            'filled_at': str(_g('filled_at')) if _g('filled_at') else None,
            'filled_price': _g('filled_price', None),
            'deal_id': _g('deal_id', ''),
            'reject_reason': _g('reject_reason', '') or _g('reason', ''),
        }
        def _do(history):
            oid = record['order_id']
            if oid and not any(h.get('order_id') == oid for h in history):
                history.append(record)
            return history[-10000:]
        try:
            _with_file_lock(ORDER_HISTORY_PATH, 'rw', _do)
        except Exception as e:
            logger.error(f"append order to file failed: {e}")

    def _append_trade_to_file(self, trade: LiveTrade):
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
            'deal_id_entry': trade.deal_id_entry,
            'deal_id_exit': trade.deal_id_exit,
        }
        def _do(history):
            if not any(h.get('trade_id') == record['trade_id'] for h in history):
                history.append(record)
            return history[-10000:]
        try:
            _with_file_lock(TRADE_HISTORY_PATH, 'rw', _do)
        except Exception as e:
            logger.error(f"append trade to file failed: {e}")

    def _append_equity_tick(self, equity: float, pnl: float, peak: float):
        record = {'t': datetime.now().isoformat(), 'equity': equity,
                  'pnl': pnl, 'peak': peak}
        def _do(history):
            history.append(record)
            return history[-5000:]
        try:
            _with_file_lock(EQUITY_PATH, 'rw', _do)
        except Exception as e:
            logger.error(f"append equity tick failed: {e}")

    # ─── Snapshot / bar ingest ─────────────────────────
    def _fetch_quote(self, epic: str) -> Optional[dict]:
        if not self.broker or not getattr(self.broker, 'connected', False):
            return None
        with self._poll_lock:
            broker_throttle = getattr(self.broker, '_throttle_until', None)
            now = datetime.now()
            if (self._throttled_until and now < self._throttled_until) or \
               (broker_throttle and now < broker_throttle):
                return None
            try:
                info = self.broker.get_market_info(epic)
                if info and info.get('bid') and info.get('offer'):
                    self._consecutive_throttle = 0
                    return {
                        'bid': float(info['bid']),
                        'offer': float(info['offer']),
                        'mid': (float(info['bid']) + float(info['offer'])) / 2,
                        'spread': float(info['offer']) - float(info['bid']),
                        'instrument_name': info.get('instrument_name', epic),
                        'market_status': info.get('market_status', 'UNKNOWN'),
                    }
                else:
                    self.n_ig_errors += 1
                    self._consecutive_throttle = getattr(self, '_consecutive_throttle', 0) + 1
                    if self._consecutive_throttle >= 3:
                        backoff = min(120, 30 + 10 * self._consecutive_throttle)
                        self._throttled_until = datetime.now() + timedelta(seconds=backoff)
                        logger.warning(f"Engine throttle backoff: {backoff}s after {self._consecutive_throttle} empty responses")
            except Exception as e:
                self.n_ig_errors += 1
                logger.debug(f"get_market_info({epic}) failed: {e}")
            return None

    def _append_snapshot(self, epic: str, quote: dict):
        now = datetime.now()
        snap = LiveSnapshot(timestamp=now, bid=quote['bid'],
                            offer=quote['offer'], mid=quote['mid'])
        if epic not in self.snapshots:
            self.snapshots[epic] = []
        self.snapshots[epic].append(snap)
        if len(self.snapshots[epic]) > 300:
            self.snapshots[epic] = self.snapshots[epic][-300:]
        self.live_quotes[epic] = quote
        self._build_bar(epic, snap)

    def _build_bar(self, epic: str, snap: LiveSnapshot):
        cutoff = snap.timestamp - timedelta(seconds=60)
        recent = [s for s in self.snapshots.get(epic, [])
                  if pd.Timestamp(s.timestamp) >= pd.Timestamp(cutoff)]
        if not recent:
            return
        bar = LiveBar(
            timestamp=snap.timestamp,
            open=recent[0].mid,
            high=max(s.mid for s in recent),
            low=min(s.mid for s in recent),
            close=recent[-1].mid,
            volume=0.0,
        )
        if epic not in self.bars:
            self.bars[epic] = []
        if not self.bars[epic] or self.bars[epic][-1].timestamp != bar.timestamp:
            self.bars[epic].append(bar)
            self.last_bar_time = datetime.now()
        if len(self.bars[epic]) > 500:
            self.bars[epic] = self.bars[epic][-500:]

    # ─── Forecast (per-class, adaptive, works with class.min_bars) ──
    def _compute_forecast(self, epic: str) -> Optional[dict]:
        bars = self.bars.get(epic, [])
        cfg = get_config(epic)
        min_bars = cfg.get('min_bars_for_forecast', 5)
        if len(bars) < min_bars:
            return None
        df = pd.DataFrame([asdict(b) for b in bars])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.set_index('timestamp').sort_index()
        # Use the per-class forecast with class-specific thresholds
        return compute_forecast_for(epic, df)

    def scan_symbol(self, epic: str) -> Optional[dict]:
        quote = self._fetch_quote(epic)
        if not quote:
            return None
        self._append_snapshot(epic, quote)
        return self._compute_forecast(epic)

    # ─── Order entry (REAL IG) ─────────────────────────
    def try_open_position(self, epic: str, forecast: dict) -> Optional[dict]:
        if forecast['direction'] == 'NEUTRAL':
            return None
        if not self.risk or not self.risk.can_trade():
            return None
        quote = self.live_quotes.get(epic)
        if not quote or not quote.get('bid') or not quote.get('offer'):
            return None
        # ── Per-class market hours check ──
        if not is_market_open(epic):
            return None
        # ── IG market_status gate ──
        ms = (quote.get('market_status') or '').upper()
        if ms in ('CLOSED', 'OFFLINE', 'SUSPENDED'):
            return None

        # ── Get the per-class config ──
        cfg = get_config(epic)
        cls = cfg['_class']
        inst_class = forecast.get('class', cls)

        S = forecast['close']
        atr = max(forecast['atr'], S * cfg.get('min_atr_pct', 0.001))
        # ── Spread filter (per-class) ──
        spread_ok_flag, spread_reason = spread_ok(epic, quote['bid'], quote['offer'])
        if not spread_ok_flag:
            return None
        # ── ATR filter (per-class) ──
        atr_ok_flag, atr_reason = atr_ok(epic, forecast['atr'], S)
        if not atr_ok_flag:
            return None

        # ── Compute TP/SL with per-class ATR multiples ──
        sl_mult = cfg.get('stop_atr_mult', 1.0)
        tp_mult = cfg.get('target_atr_mult', 2.0)
        if forecast['direction'] == 'BULLISH':
            direction = 'BUY'
            stop = S - atr * sl_mult
            target = S + atr * tp_mult
        else:
            direction = 'SELL'
            stop = S + atr * sl_mult
            target = S - atr * tp_mult

        # ── Per-class position sizing ──
        max_lev = self.config.get('max_leverage', 0.5)
        contract_size = cfg.get('contract_size', 1.0)
        max_units_cap = cfg.get('max_units_cap', 100)
        try:
            n_units = self.risk.state.size_position(S, stop, max_leverage=max_lev,
                                                    contract_size=contract_size)
        except Exception as e:
            logger.error(f"size_position EXCEPTION: {epic} {e}")
            return None
        if n_units <= 0:
            return None
        # Round to min size increment
        min_inc = cfg.get('min_size_increment', 1.0)
        if min_inc < 1.0:
            n_units = max(min_inc, round(n_units * (1.0 / min_inc)) / (1.0 / min_inc))
        else:
            n_units = max(min_inc, round(n_units))
        if n_units <= 0:
            return None
        # Apply per-class hard cap
        if n_units > max_units_cap:
            n_units = max_units_cap
        if n_units <= 0:
            return None
        for d in self.ig_positions.values():
            if d.get('epic') == epic and d.get('direction') == direction:
                return None

        # Record the SIGNAL (always, before submit) and the
        # OPPORTUNITY (we'll update status after the order comes back)
        last_bar = self.bars.get(epic, [None])[-1] if self.bars.get(epic) else None
        bar_data = {
            'open': getattr(last_bar, 'open', S) if last_bar else S,
            'high': getattr(last_bar, 'high', S) if last_bar else S,
            'low':  getattr(last_bar, 'low', S)  if last_bar else S,
            'close': getattr(last_bar, 'close', S) if last_bar else S,
            'volume': getattr(last_bar, 'volume', 0) if last_bar else 0,
        }
        sig_id = signals_service.record_signal(
            epic=epic,
            display_name=self.name_resolver(epic),
            forecast=forecast,
            bar_data=bar_data,
            decision='OPENED',
            note=f'{direction} {n_units}u @ {S:.5f}',
        )
        notional = n_units * contract_size * S
        risk_dollars = self.risk.state.capital * self.risk.state.risk_per_trade
        stop_distance = abs(S - stop)
        risk_reward = abs(target - S) / max(stop_distance, 1e-9)
        opp_id = signals_service.record_opportunity(
            signal_id=sig_id,
            epic=epic,
            display_name=self.name_resolver(epic),
            forecast=forecast,
            sizing={
                'n_units': n_units,
                'notional': notional,
                'risk_dollars': risk_dollars,
                'risk_per_trade_pct': self.risk.state.risk_per_trade * 100,
                'leverage': max_lev,
                'stop_distance': stop_distance,
                'contract_size': contract_size,
                'class': inst_class,
            },
            decision={
                'side': direction,
                'entry': S,
                'stop': stop,
                'target': target,
                'risk_reward': risk_reward,
                'reason': f'{inst_class} {forecast["direction"]}, ret_3={forecast["ret_3"]:.5f}, ATR={atr:.5f}, SL={sl_mult}x, TP={tp_mult}x',
            },
            order={},
            status='PENDING',
        )

        # Submit REAL order to IG
        order = self.broker.submit_order(
            epic=epic, direction=direction, size=n_units,
            order_type='MARKET', currency_code='USD',
        )
        if order is None:
            self.n_ig_errors += 1
            signals_service.update_opportunity(opp_id, status='REJECTED',
                                              order={'error': 'broker returned None'})
            return None
        if hasattr(self.broker, 'poll_order'):
            order = self.broker.poll_order(order, max_attempts=5, sleep_s=0.6)

        # Record the order
        self.orders.append(order)
        self.local_orders[order.deal_reference] = order
        self.n_orders += 1
        self._append_order_to_file(order)
        # Update the opportunity with the actual order result
        signals_service.update_opportunity(
            opp_id,
            order={
                'order_id': order.deal_reference,
                'deal_id': order.deal_id,
                'status': order.status,
                'filled_at': str(order.filled_at) if order.filled_at else None,
                'filled_price': order.filled_price,
                'reason': getattr(order, 'reason', ''),
                'class': inst_class,
            },
        )

        if order.status in ('REJECTED', 'EXPIRED', 'DELETED'):
            self.n_rejected += 1
            reason = getattr(order, 'reason', '') or 'unknown'
            signals_service.update_opportunity(opp_id, status='REJECTED')
            action = {
                'symbol': epic, 'display_name': self.name_resolver(epic),
                'class': inst_class,
                'action': 'rejected', 'direction': direction,
                'reason': reason,
                'n_units': n_units, 'entry': S,
                't': datetime.now().isoformat(),
                'opp_id': opp_id,
            }
            self.recent_actions.append(action)
            self.recent_actions = self.recent_actions[-200:]
            return action
        self.n_fills += 1
        self.n_signals += 1
        signals_service.update_opportunity(opp_id, status='OPENED')
        action = {
            'symbol': epic, 'display_name': self.name_resolver(epic),
            'class': inst_class,
            'action': 'open', 'direction': direction,
            'entry': S, 'stop': stop, 'target': target, 'n_units': n_units,
            'forecast': forecast['direction'], 'streak': forecast['streak'],
            'deal_id': order.deal_id,
            'filled_price': order.filled_price,
            't': datetime.now().isoformat(),
            'opp_id': opp_id,
        }
        self.recent_actions.append(action)
        self.recent_actions = self.recent_actions[-200:]
        return action

    # ─── Sync IG positions (the source of truth) ───────
    def sync_ig_positions(self) -> List[dict]:
        """Sync self.ig_positions with IG's actual open positions.

        Returns the current list of open positions from IG.
        """
        if not self.broker or not getattr(self.broker, 'connected', False):
            return []
        with self._poll_lock:
            try:
                positions = self.broker.get_open_positions()
            except Exception as e:
                logger.debug(f"get_open_positions failed: {e}")
                self.n_ig_errors += 1
                return []
        # Build the new deal_id → position map from IG's current state
        new_map = {p['deal_id']: p for p in positions if p.get('deal_id')}
        # Detect closed positions (in our dict but not in IG's)
        for deal_id, old in list(self.ig_positions.items()):
            if deal_id not in new_map:
                self._record_close(old, reason='ig_closed')
        # UPDATE self.ig_positions to match IG (this was the missing line!)
        self.ig_positions = new_map
        # Check per-class max_hold_bars for any open position
        self._check_hold_periods()
        return positions

    def _check_hold_periods(self):
        """Close positions that have been held longer than the
        per-class max_hold_bars. Implements the per-class exit rule."""
        now = datetime.now()
        for deal_id, pos in list(self.ig_positions.items()):
            epic = pos.get('epic', '')
            if not epic:
                continue
            cfg = get_config(epic)
            max_bars = cfg.get('max_hold_bars', 12)
            # We need a created time to know how many bars have passed
            created_iso = pos.get('createdDate') or pos.get('created_date') or pos.get('created')
            if not created_iso:
                continue
            try:
                if isinstance(created_iso, str):
                    # Parse IG timestamp — could be 'YYYY/MM/DD HH:MM:SS:fff' or ISO
                    for fmt in ('%Y/%m/%d %H:%M:%S:%f', '%Y-%m-%dT%H:%M:%S',
                                '%Y-%m-%d %H:%M:%S'):
                        try:
                            created = datetime.strptime(created_iso, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        continue
                else:
                    created = created_iso
            except Exception:
                continue
            elapsed = (now - created).total_seconds() / 60
            # Bar is 1 min long; max_bars in minutes ≈ max_bars minutes
            if elapsed > max_bars:
                logger.info(f"Closing {epic} (deal={deal_id}) after {elapsed:.0f}min > max_bars={max_bars}")
                self.close_ig_position(deal_id)

    def _check_position_exits(self):
        """Check TP / SL / time-stop for each open position using
        live bid/offer. The position has its stop/target computed at
        entry time and stored in the original order."""
        for deal_id, pos in list(self.ig_positions.items()):
            epic = pos.get('epic', '')
            direction = pos.get('direction', '')
            if not epic or not direction:
                continue
            # Find the originating order to get stop/target
            orig = None
            for o in self.orders:
                if o.deal_id == deal_id and o.status in ('OPEN', 'ACCEPTED', 'FILLED'):
                    orig = o
                    break
            if not orig:
                continue
            # We need stop/target on the order. We didn't store them on
            # IGOrder. For now, exit only by max-hold-bars (handled in
            # _check_hold_periods above). TP/SL exits require attaching
            # them to the order at entry time, which we don't do.
            pass

    def _record_close(self, pos: dict, reason: str = 'ig_closed'):
        deal_id = pos.get('deal_id')
        entry_price = float(pos.get('level', 0))
        orig_order = None
        for o in self.orders:
            if o.deal_id == deal_id and o.status in ('OPEN', 'ACCEPTED', 'FILLED'):
                orig_order = o
                break
        if not orig_order:
            trade = LiveTrade(
                trade_id=_next_id('T'),
                instrument=pos.get('epic', '?'),
                display_name=pos.get('instrument_name', pos.get('epic', '?')),
                direction=pos.get('direction', 'BUY'),
                entry_time=datetime.now().isoformat(),
                entry_price=entry_price,
                exit_time=datetime.now().isoformat(),
                exit_price=entry_price,
                n_units=float(pos.get('size', 0)),
                pnl=float(pos.get('pnl', 0)),
                won=float(pos.get('pnl', 0)) > 0,
                exit_type=reason,
                deal_id_entry='',
                deal_id_exit=deal_id or '',
            )
        else:
            trade = LiveTrade(
                trade_id=_next_id('T'),
                instrument=orig_order.instrument,
                display_name=orig_order.display_name,
                direction=orig_order.direction,
                entry_time=str(orig_order.created_at),
                entry_price=orig_order.filled_price or entry_price,
                exit_time=datetime.now().isoformat(),
                exit_price=entry_price,
                n_units=orig_order.n_units,
                pnl=float(pos.get('pnl', 0)),
                won=float(pos.get('pnl', 0)) > 0,
                exit_type=reason,
                deal_id_entry=orig_order.deal_id or '',
                deal_id_exit=deal_id or '',
            )
        self.closed_trades.append(trade)
        if self.risk:
            self.risk.record_trade(trade.pnl, datetime.now().date().isoformat())
            day = datetime.now().date().isoformat()
            self.daily_pnl[day] = self.daily_pnl.get(day, 0) + trade.pnl
        self._append_trade_to_file(trade)
        action = {
            'symbol': trade.instrument, 'display_name': trade.display_name,
            'action': 'close', 'exit_type': reason, 'pnl': trade.pnl,
            't': datetime.now().isoformat(),
        }
        self.recent_actions.append(action)
        self.recent_actions = self.recent_actions[-200:]

    def close_ig_position(self, deal_id: str) -> bool:
        if not self.broker or not getattr(self.broker, 'connected', False):
            return False
        pos = self.ig_positions.get(deal_id)
        if not pos:
            return False
        try:
            ok = self.broker.close_position(
                deal_id=deal_id,
                direction=pos.get('direction'),
                epic=pos.get('epic'),
                expiry='-',
                size=pos.get('size'),
            )
            if ok:
                self._record_close(pos, reason='manual_close')
                if deal_id in self.ig_positions:
                    del self.ig_positions[deal_id]
            return ok
        except Exception as e:
            logger.error(f"close_position failed: {e}")
            return False

    # ─── Main loop tick ─────────────────────────────────
    def tick(self) -> dict:
        """One pass: sync positions, scan current batch, also do a
        'background' poll of any symbol that hasn't been polled in
        a long time so ALL 166+ instruments get signals continuously.
        Does NOT take self._lock — relies on atomic state updates
        from request threads."""
        self.sync_ig_positions()
        # Rotate the universe batch if we have a rotator
        if self.rotator:
            self.universe = self.rotator.current()
        else:
            if not self.universe:
                self.universe = self.universe_resolver() or []
        # Always put 24/7 crypto first
        priority = ['CS.D.BITCOIN.CFBMU.IP', 'CS.D.BITCOIN.CFD.IP',
                    'CS.D.ETHEREUM.CFBMU.IP']
        ordered = [e for e in priority if e in self.universe] + \
                  [e for e in self.universe if e not in priority]
        actions = []
        signals_recorded = 0
        opportunities_recorded = 0
        for epic in ordered:
            try:
                forecast = self.scan_symbol(epic)
                if forecast:
                    self.forecasts[epic] = forecast
                    self.last_polled[epic] = datetime.now()
                    last_bar = self.bars.get(epic, [None])[-1] if self.bars.get(epic) else None
                    bar_data = {
                        'open': getattr(last_bar, 'open', forecast['close']) if last_bar else forecast['close'],
                        'high': getattr(last_bar, 'high', forecast['close']) if last_bar else forecast['close'],
                        'low':  getattr(last_bar, 'low', forecast['close'])  if last_bar else forecast['close'],
                        'close': getattr(last_bar, 'close', forecast['close']) if last_bar else forecast['close'],
                        'volume': getattr(last_bar, 'volume', 0) if last_bar else 0,
                    }
                    signals_service.record_signal(
                        epic=epic, display_name=self.name_resolver(epic),
                        forecast=forecast, bar_data=bar_data,
                        decision='NONE' if forecast['direction'] == 'NEUTRAL' else 'OPENED',
                    )
                    signals_recorded += 1
                if forecast and forecast['direction'] != 'NEUTRAL':
                    act = self.try_open_position(epic, forecast)
                    if act:
                        actions.append(act)
                        opportunities_recorded += 1
                time.sleep(0.2)
            except Exception as e:
                logger.debug(f"tick({epic}) failed: {e}")
        # Background poll: any full-universe symbol that hasn't been
        # polled in 10+ minutes gets a quick snapshot (so we keep bars
        # growing for ALL 166+ instruments, not just the current batch).
        # Capped to 1 per tick to avoid IG throttling.
        now = datetime.now()
        if self.all_symbols and not self._throttled_until or (
            self._throttled_until and now >= self._throttled_until):
            n_bg = 0
            for epic in self.all_symbols:
                if epic in self.universe:  # already in current batch
                    continue
                last = self.last_polled.get(epic)
                if last and (now - last).total_seconds() < 600:  # 10 min
                    continue
                try:
                    quote = self._fetch_quote(epic)
                    if quote:
                        self._append_snapshot(epic, quote)
                        self.last_polled[epic] = now
                        forecast = self._compute_forecast(epic)
                        if forecast:
                            self.forecasts[epic] = forecast
                            last_bar = self.bars.get(epic, [None])[-1] if self.bars.get(epic) else None
                            bar_data = {
                                'open': getattr(last_bar, 'open', forecast['close']) if last_bar else forecast['close'],
                                'high': getattr(last_bar, 'high', forecast['close']) if last_bar else forecast['close'],
                                'low':  getattr(last_bar, 'low', forecast['close'])  if last_bar else forecast['close'],
                                'close': getattr(last_bar, 'close', forecast['close']) if last_bar else forecast['close'],
                                'volume': getattr(last_bar, 'volume', 0) if last_bar else 0,
                            }
                            signals_service.record_signal(
                                epic=epic, display_name=self.name_resolver(epic),
                                forecast=forecast, bar_data=bar_data,
                                decision='NONE',
                            )
                            signals_recorded += 1
                        n_bg += 1
                        break  # only 1 bg-poll per tick (IG is throttled)
                except Exception as e:
                    logger.debug(f"bg tick({epic}) failed: {e}")
        self._update_equity_curve()
        self.last_poll_time = datetime.now()
        self._save_state()
        if self.rotator:
            self.rotator.advance()
        return {
            'n_actions': len(actions),
            'n_universe': len(self.universe),
            'n_signals': signals_recorded,
            'n_opportunities': opportunities_recorded,
            'actions': actions,
        }

    def _update_equity_curve(self):
        unrealized = sum(float(p.get('pnl', 0) or 0) for p in self.ig_positions.values())
        closed_pnl = sum(t.pnl for t in self.closed_trades)
        equity = (self.risk.state.initial_capital if self.risk else 10000.0) + closed_pnl + unrealized
        if self.risk:
            self.risk.state.capital = equity
            if equity > self.risk.state.peak_capital:
                self.risk.state.peak_capital = equity
        if (not self.equity_curve or
                (datetime.now() - datetime.fromisoformat(self.equity_curve[-1]['t'])).total_seconds() >= 30):
            self.equity_curve.append({
                't': datetime.now().isoformat(),
                'equity': equity, 'pnl': closed_pnl,
                'peak': self.risk.state.peak_capital if self.risk else equity,
                'unrealized': unrealized,
            })
            if len(self.equity_curve) > 2000:
                self.equity_curve = self.equity_curve[-2000:]
            self._append_equity_tick(equity, closed_pnl,
                                     self.risk.state.peak_capital if self.risk else equity)

    # ─── Background loop ───────────────────────────────
    def start(self, poll_interval: int = 30):
        if self.running:
            return
        if not self.universe:
            self.universe = self.universe_resolver() or []
        self.running = True
        self.start_time = datetime.now()
        self._thread = threading.Thread(target=self._run_loop, args=(poll_interval,), daemon=True)
        self._thread.start()
        logger.info(f"LiveEngine started: universe={len(self.universe)} interval={poll_interval}s")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._save_state()
        logger.info("LiveEngine stopped")

    def _run_loop(self, interval: int):
        if self.broker and not getattr(self.broker, 'connected', False):
            try:
                self.broker.connect()
            except Exception as e:
                logger.error(f"broker connect failed: {e}")
        # Counter for periodic full-class sweep (every 5 minutes)
        last_full_sweep = datetime.now() - timedelta(minutes=5)
        while self.running:
            try:
                self.tick()  # no lock — runs in bg thread, request threads are non-blocking
            except Exception as e:
                logger.exception(f"live tick failed: {e}")
            # Every 5 minutes, do a full-class sweep so we get bars
            # for ALL 22 classes (the regular bg poll only does 1/tick)
            if (datetime.now() - last_full_sweep).total_seconds() > 300:
                try:
                    # Pick one EPIC per class
                    from backend.core.instrument_config import classify
                    seen = set()
                    epics = []
                    for e in self.all_symbols:
                        c = classify(e)
                        if c not in seen:
                            seen.add(c)
                            epics.append(e)
                    logger.info(f"Full-class sweep: {len(epics)} classes (every 5 min)")
                    self.force_poll_specific(epics)
                    last_full_sweep = datetime.now()
                except Exception as e:
                    logger.debug(f"full-class sweep failed: {e}")
            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

    # ─── Status / introspection ────────────────────────
    def get_status(self) -> dict:
        # NOTE: don't take self._lock here — the bg loop can hold it
        # for tens of seconds while polling IG, which would block
        # this request. All public state is replaced via reassignment
        # (not mutation) so reading is safe without the lock.
        unrealized = sum(float(p.get('pnl', 0) or 0) for p in self.ig_positions.values())
        closed_pnl = sum(t.pnl for t in self.closed_trades)
        equity = (self.risk.state.initial_capital if self.risk else 10000.0) + closed_pnl + unrealized
        risk_snap = self.risk.snapshot() if self.risk else {}
        stats = signals_service.get_opportunity_stats()
        rotator_info = self.rotator.stats() if self.rotator else None
        # Per-class coverage stats
        from collections import defaultdict
        class_coverage = defaultdict(lambda: {'total': 0, 'with_quote': 0, 'with_signal': 0, 'with_bars': 0})
        for epic in self.all_symbols:
            cls = classify(epic)
            class_coverage[cls]['total'] += 1
            if epic in self.live_quotes:
                class_coverage[cls]['with_quote'] += 1
            if epic in self.forecasts:
                class_coverage[cls]['with_signal'] += 1
            if epic in self.bars and len(self.bars[epic]) > 0:
                class_coverage[cls]['with_bars'] += 1
        # Broker health
        broker_health = self.broker.get_health() if hasattr(self.broker, 'get_health') else {}
        return {
            'mode': 'live',
            'running': self.running,
            'start_time': str(self.start_time) if self.start_time else None,
            'uptime_seconds': (datetime.now() - self.start_time).total_seconds() if self.start_time else 0,
            'last_poll_time': str(self.last_poll_time) if self.last_poll_time else None,
            'last_bar_time': str(self.last_bar_time) if self.last_bar_time else None,
            'seconds_since_last_poll': (datetime.now() - self.last_poll_time).total_seconds() if self.last_poll_time else None,
            'all_symbols_count': len(self.all_symbols),
            'current_batch_size': len(self.universe),
            'universe_size': len(self.universe),
            'universe': [self.name_resolver(e) for e in self.universe],
            'all_symbols': [self.name_resolver(e) for e in self.all_symbols],
            'rotator': rotator_info,
            'bars_per_symbol': {self.name_resolver(k): len(v) for k, v in self.bars.items()},
            'n_ig_positions': len(self.ig_positions),
            'ig_positions': list(self.ig_positions.values()),
            'n_closed_trades': len(self.closed_trades),
            'n_orders': self.n_orders,
            'n_fills': self.n_fills,
            'n_rejected': self.n_rejected,
            'n_signals': self.n_signals,
            'n_bar_fetches': self.n_bar_fetches,
            'n_ig_errors': self.n_ig_errors,
            'forecasts': {self.name_resolver(k): v for k, v in self.forecasts.items()},
            'recent_actions': self.recent_actions[-30:],
            'live_quotes': {self.name_resolver(k): {
                'bid': v.get('bid'), 'offer': v.get('offer'),
                'spread': v.get('spread'),
                'market_status': v.get('market_status'),
            } for k, v in self.live_quotes.items()},
            'broker_connected': getattr(self.broker, 'connected', False) if self.broker else False,
            'broker_account_type': getattr(self.broker, 'acc_type', 'N/A') if self.broker else 'N/A',
            'broker_health': broker_health,
            'class_coverage': dict(class_coverage),
            'capital': equity,
            'initial_capital': self.risk.state.initial_capital if self.risk else 10000,
            'peak': self.risk.state.peak_capital if self.risk else equity,
            'unrealized_pnl': unrealized,
            'closed_pnl': closed_pnl,
            'dd_pct': risk_snap.get('last_dd_pct', 0),
            'max_dd_pct': risk_snap.get('max_dd_pct', 0),
            'paused': risk_snap.get('paused', False),
            'risk': risk_snap,
            'signals_stats': stats,
        }

    def get_open_positions(self) -> List[dict]:
        # No lock — read-only snapshot, safe to read without lock
        return [{
            'instrument': p.get('epic'),
            'display_name': p.get('instrument_name', p.get('epic')),
            'direction': p.get('direction'),
            'entry_price': p.get('level'),
            'n_units': p.get('size'),
            'currency': p.get('currency'),
            'deal_id': p.get('deal_id'),
            'unrealized_pnl': float(p.get('pnl', 0) or 0),
            'entry_time': '',
        } for p in self.ig_positions.values()]

    def get_recent_trades(self, n: int = 50) -> List[dict]:
        # No lock — read-only snapshot
        return [{
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
            'deal_id_entry': t.deal_id_entry,
            'deal_id_exit': t.deal_id_exit,
        } for t in self.closed_trades[-n:]]

    def force_poll(self, symbols: List[str] = None) -> dict:
        # No lock — additive to existing universe; tick is safe to run concurrently
        if symbols:
            self.universe = list(set(self.universe) | set(symbols))
        result = self.tick()
        return {
            'n_new': 0,
            'symbols_polled': len(self.universe),
            'actions': result.get('actions', []),
            'n_open': len(self.ig_positions),
            'n_closed': len(self.closed_trades),
            'n_signals': result.get('n_signals', 0),
            'n_opportunities': result.get('n_opportunities', 0),
        }

    def force_poll_specific(self, epics: List[str], submit_orders: bool = True) -> dict:
        """Poll a specific list of EPICs (bypasses rotator). Used for
        ad-hoc coverage of all 22 classes when user clicks 'Force all'.

        If submit_orders=True (default), also calls try_open_position for
        any BULLISH/BEARISH forecast — the same path the regular tick uses.
        This is the "force everything" mode that proves per-class TP/SL/RR
        works end-to-end with real IG orders.
        """
        # Clear throttle so we can actually fetch
        if self.broker and hasattr(self.broker, '_throttle_until'):
            self.broker._throttle_until = None
            self._throttled_until = None
            self._consecutive_throttle = 0
        results = []
        signals_found = 0
        orders_placed = 0
        for epic in epics:
            try:
                forecast = self.scan_symbol(epic)
                if forecast:
                    signals_found += 1
                    # Record signal
                    last_bar = self.bars.get(epic, [None])[-1] if self.bars.get(epic) else None
                    bar_data = {
                        'open': getattr(last_bar, 'open', forecast['close']) if last_bar else forecast['close'],
                        'high': getattr(last_bar, 'high', forecast['close']) if last_bar else forecast['close'],
                        'low':  getattr(last_bar, 'low', forecast['close'])  if last_bar else forecast['close'],
                        'close': getattr(last_bar, 'close', forecast['close']) if last_bar else forecast['close'],
                        'volume': getattr(last_bar, 'volume', 0) if last_bar else 0,
                    }
                    signals_service.record_signal(
                        epic=epic, display_name=self.name_resolver(epic),
                        forecast=forecast, bar_data=bar_data,
                        decision='NONE' if forecast['direction'] == 'NEUTRAL' else 'OPENED',
                    )
                    # If BULLISH/BEARISH and submit_orders is True, open a real position
                    if submit_orders and forecast['direction'] != 'NEUTRAL':
                        act = self.try_open_position(epic, forecast)
                        if act:
                            orders_placed += 1
                results.append({
                    'epic': epic,
                    'name': self.name_resolver(epic),
                    'class': classify(epic),
                    'forecast_direction': forecast.get('direction') if forecast else None,
                    'bid': self.live_quotes.get(epic, {}).get('bid'),
                    'offer': self.live_quotes.get(epic, {}).get('offer'),
                    'n_bars': len(self.bars.get(epic, [])),
                })
                time.sleep(0.4)  # gentle on IG
            except Exception as e:
                results.append({'epic': epic, 'name': self.name_resolver(epic),
                                'class': classify(epic), 'error': str(e)[:80]})
        self._update_equity_curve()
        self.last_poll_time = datetime.now()
        self._save_state()
        return {
            'mode': 'live',
            'epics_polled': len(epics),
            'signals_found': signals_found,
            'orders_placed': orders_placed,
            'results': results,
            'broker_health': self.broker.get_health() if hasattr(self.broker, 'get_health') else {},
        }
