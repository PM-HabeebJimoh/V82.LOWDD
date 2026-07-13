"""
V82.LOWDD — Signals & Opportunities Service.

A "signal" is every forecast produced by the engine on every poll
(both BULLISH/BEARISH and NEUTRAL — full history of what the model
"thought"). An "opportunity" is a signal that the engine acted on
(decided to open a position). For every opportunity we keep the
full context: forecast, bar data at decision time, sizing, target,
stop, and the resulting order/trade.

Files (under backend/state/):
  signals.pkl       — every signal (BULLISH, BEARISH, NEUTRAL)
  opportunities.pkl — every actionable opportunity (BULLISH+BUY, BEARISH+SELL)
"""
import os
import json
import time
import pickle
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict
from threading import RLock

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'state'
)
os.makedirs(STATE_DIR, exist_ok=True)

SIGNALS_PATH = os.path.join(STATE_DIR, 'signals.pkl')
OPPORTUNITIES_PATH = os.path.join(STATE_DIR, 'opportunities.pkl')

_lock = RLock()


def _atomic_write(path: str, data):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump(data, f)
    os.replace(tmp, path)


def _atomic_read(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'rb') as f:
            return pickle.load(f)
    except Exception as e:
        logger.error(f"read {path} failed: {e}")
        return default


def _next_id(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000) % 100000000:08d}"


# ─── Public API ──────────────────────────────────────────
def record_signal(epic: str, display_name: str, forecast: dict,
                  bar_data: dict, decision: str = 'NONE',
                  note: str = '') -> str:
    """Record every signal. Returns the signal ID."""
    with _lock:
        sig_id = _next_id('S')
        record = {
            'id': sig_id,
            't': datetime.now().isoformat(),
            'epic': epic,
            'symbol': display_name,
            'decision': decision,            # 'OPENED' | 'REJECTED' | 'SKIPPED' | 'NONE'
            'note': note,
            'forecast': {
                'direction': forecast.get('direction'),
                'close': float(forecast.get('close', 0)),
                'atr': float(forecast.get('atr', 0)),
                'ma_fast': float(forecast.get('ma_fast', 0)),
                'ma_slow': float(forecast.get('ma_slow', 0)),
                'ret_3': float(forecast.get('ret_3', 0)),
                'streak': int(forecast.get('streak', 0)),
                'n_bars': int(forecast.get('n_bars', 0)),
            },
            'bar': {
                'open': float(bar_data.get('open', 0)),
                'high': float(bar_data.get('high', 0)),
                'low': float(bar_data.get('low', 0)),
                'close': float(bar_data.get('close', 0)),
                'volume': float(bar_data.get('volume', 0)),
            },
        }
        signals = _atomic_read(SIGNALS_PATH, [])
        signals.append(record)
        # Keep last 5000 signals
        signals = signals[-5000:]
        _atomic_write(SIGNALS_PATH, signals)
        return sig_id


def record_opportunity(signal_id: str, epic: str, display_name: str,
                       forecast: dict, sizing: dict, decision: dict,
                       order: Optional[dict] = None,
                       status: str = 'PENDING') -> str:
    """Record an actionable opportunity (BULLISH+BUY or BEARISH+SELL).
    status: PENDING (signal fired) → OPENED (order filled) →
            CLOSED (trade closed) → REJECTED (order rejected)
    """
    with _lock:
        opp_id = _next_id('O')
        record = {
            'id': opp_id,
            'signal_id': signal_id,
            't': datetime.now().isoformat(),
            'epic': epic,
            'symbol': display_name,
            'status': status,
            'forecast': {
                'direction': forecast.get('direction'),
                'close': float(forecast.get('close', 0)),
                'atr': float(forecast.get('atr', 0)),
                'ma_fast': float(forecast.get('ma_fast', 0)),
                'ma_slow': float(forecast.get('ma_slow', 0)),
                'ret_3': float(forecast.get('ret_3', 0)),
                'streak': int(forecast.get('streak', 0)),
                'n_bars': int(forecast.get('n_bars', 0)),
            },
            'sizing': {
                'n_units': sizing.get('n_units', 0),
                'notional': sizing.get('notional', 0),
                'risk_dollars': sizing.get('risk_dollars', 0),
                'risk_per_trade_pct': sizing.get('risk_per_trade_pct', 0),
                'leverage': sizing.get('leverage', 0),
                'stop_distance': sizing.get('stop_distance', 0),
            },
            'decision': {
                'side': decision.get('side'),
                'entry': decision.get('entry', 0),
                'stop': decision.get('stop', 0),
                'target': decision.get('target', 0),
                'risk_reward': decision.get('risk_reward', 0),
                'reason': decision.get('reason', ''),
            },
            'order': order or {},
        }
        opps = _atomic_read(OPPORTUNITIES_PATH, [])
        opps.append(record)
        opps = opps[-5000:]
        _atomic_write(OPPORTUNITIES_PATH, opps)
        return opp_id


def update_opportunity(opp_id: str, **kwargs) -> bool:
    """Patch an opportunity with new fields (e.g. status='OPENED')."""
    with _lock:
        opps = _atomic_read(OPPORTUNITIES_PATH, [])
        for o in opps:
            if o['id'] == opp_id:
                for k, v in kwargs.items():
                    if k in ('order', 'sizing', 'decision', 'forecast'):
                        o[k].update(v if isinstance(v, dict) else {k: v})
                    else:
                        o[k] = v
                _atomic_write(OPPORTUNITIES_PATH, opps)
                return True
        return False


def list_signals(limit: int = 200, offset: int = 0,
                 direction: Optional[str] = None,
                 epic: Optional[str] = None) -> List[dict]:
    """List signals (newest first) with optional filters."""
    signals = _atomic_read(SIGNALS_PATH, [])
    if direction:
        signals = [s for s in signals if s.get('forecast', {}).get('direction') == direction]
    if epic:
        signals = [s for s in signals if s.get('epic') == epic]
    signals = list(reversed(signals))  # newest first
    return signals[offset:offset + limit]


def list_opportunities(limit: int = 200, offset: int = 0,
                       status: Optional[str] = None,
                       epic: Optional[str] = None,
                       direction: Optional[str] = None) -> List[dict]:
    """List opportunities (newest first) with optional filters."""
    opps = _atomic_read(OPPORTUNITIES_PATH, [])
    if status:
        opps = [o for o in opps if o.get('status') == status]
    if epic:
        opps = [o for o in opps if o.get('epic') == epic]
    if direction:
        opps = [o for o in opps if o.get('forecast', {}).get('direction') == direction]
    opps = list(reversed(opps))  # newest first
    return opps[offset:offset + limit]


def get_opportunity(opp_id: str) -> Optional[dict]:
    opps = _atomic_read(OPPORTUNITIES_PATH, [])
    for o in opps:
        if o['id'] == opp_id:
            return o
    return None


def count_signals(direction: Optional[str] = None) -> int:
    signals = _atomic_read(SIGNALS_PATH, [])
    if direction:
        return sum(1 for s in signals if s.get('forecast', {}).get('direction') == direction)
    return len(signals)


def count_opportunities(status: Optional[str] = None) -> int:
    opps = _atomic_read(OPPORTUNITIES_PATH, [])
    if status:
        return sum(1 for o in opps if o.get('status') == status)
    return len(opps)


def get_opportunity_stats() -> dict:
    """Compute stats for the Opportunities tab."""
    opps = _atomic_read(OPPORTUNITIES_PATH, [])
    signals = _atomic_read(SIGNALS_PATH, [])
    total_opps = len(opps)
    total_signals = len(signals)
    by_status = {}
    by_epic = {}
    by_direction = {}
    for o in opps:
        s = o.get('status', 'UNKNOWN')
        by_status[s] = by_status.get(s, 0) + 1
        e = o.get('epic', '?')
        by_epic[e] = by_epic.get(e, 0) + 1
        d = o.get('forecast', {}).get('direction', 'NEUTRAL')
        by_direction[d] = by_direction.get(d, 0) + 1
    for s in signals:
        d = s.get('forecast', {}).get('direction', 'NEUTRAL')
        by_direction[d] = by_direction.get(d, 0) + 1
    return {
        'total_signals': total_signals,
        'total_opportunities': total_opps,
        'by_status': by_status,
        'by_epic': by_epic,
        'by_direction': by_direction,
    }
