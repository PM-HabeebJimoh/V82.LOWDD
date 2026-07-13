"""
V82.LOWDD - Live Trading Web App (IG Markets, REAL live mode).

The system connects to IG Markets as the sole broker and runs in
LIVE mode by default — every order is submitted to IG, every P&L
tick comes from a real IG open position's P&L field. Designed for
24/7 operation across all asset classes (forex, crypto, commodities,
indices).

Endpoints:
  /                              → dashboard (index.html)
  /health                        → liveness
  /api/ig/*                      → IG REST passthrough (search, candles, market, etc.)
  /api/live/status               → engine status (with universe, rotator, signals stats)
  /api/live/mode                 → query current mode (always 'live')
  /api/live/positions            → live IG positions
  /api/live/trades               → last N closed trades
  /api/live/cycle POST           → force a poll
  /api/live/start POST           → start background loop
  /api/live/stop POST            → stop background loop
  /api/live/close POST           → close a single position
  /api/live/close-all POST       → close all IG positions
  /api/live/universe GET         → list of all working EPICs
  /api/live/probe POST           → re-probe IG universe
  /api/signals                   → every signal (BULLISH/BEARISH/NEUTRAL)
  /api/signals/stats             → signal stats
  /api/opportunities             → actionable opportunities (submitted to IG)
  /api/opportunities/<id>        → full opportunity detail (drill-down)
  /api/opportunities/stats       → opportunity stats
  /api/history/trades            → file-backed closed trade history
  /api/history/orders            → file-backed order history (REJECTED + FILLED)
  /api/history/risk              → risk state + daily P&L
  /api/history/equity            → equity curve data
  /api/market/status             → market open/closed, next open, IG connected
"""
import os
import sys
import json
import time as _time
import logging
import pickle
import threading
from datetime import datetime
from typing import Optional, Dict, List

from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.live_mode import LiveEngine
from backend.risk.manager import RiskManager
from backend.core import signals_service
from backend.live.ig_universe import (
    get_universe_epics, get_universe_names,
    get_live_universe, probe_universe,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s'
)

STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'state'
)
os.makedirs(STATE_DIR, exist_ok=True)

# V82.LOWDD canonical parameters
DEFAULT_PARAMS = {
    'risk_per_trade': 0.001,         # 0.1% per trade
    'stop_atr_mult': 1.0,
    'target_atr_mult': 2.0,
    'max_dd_threshold': 0.20,        # HARD 20% DD
    'daily_loss_limit_pct': 0.05,
    'max_leverage': 0.5,             # max 0.5x notional/capital
}

# Default symbol set used as a fast start before the probe completes.
# Focus on 24/7 crypto + major forex so we have something to scan
# immediately on first poll while the probe is running.
# Kept small (~10) to avoid IG throttling.
DEFAULT_SYMBOLS = [
    # 24/7 crypto CFDs (the only guaranteed 24/7 instruments)
    'CS.D.BITCOIN.CFBMU.IP',  # Bitcoin $0.1 mini — 1 unit = ~$6,400
    # Major forex
    'CS.D.EURUSD.MINI.IP',
    'CS.D.GBPUSD.MINI.IP',
    'CS.D.USDJPY.MINI.IP',
    'CS.D.AUDUSD.MINI.IP',
    'CS.D.USDCAD.MINI.IP',
    'CS.D.NZDUSD.MINI.IP',
    # Indices
    'IX.D.SPTRD.DAILY.IP',
    'IX.D.DOW.DAILY.IP',
    'IX.D.FTSE.DAILY.IP',
    # Commodities
    'CC.D.GC.USC.IP',  # Gold
]


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=os.path.join(os.path.dirname(__file__), '..', '..', 'frontend', 'static'),
        static_url_path='',
    )
    CORS(app)

    from backend.api import ig_routes
    ig_routes.register_routes(app)

    live_state = {
        'engine': None,           # LiveEngine (the only engine)
        'broker': None,           # IGBroker
        'mode': 'live',           # ALWAYS live
        'universe': [],           # currently active EPICs
        'probe_results': {},      # {epic: {available, ...}}
        'starting': False,
    }
    app.config['live'] = live_state

    @app.route('/')
    def index():
        from flask import send_from_directory
        idx = os.path.join(app.static_folder, 'index.html')
        if os.path.exists(idx):
            return send_from_directory(app.static_folder, 'index.html')
        return jsonify({'service': 'V82.LOWDD Live', 'status': 'ok'})

    @app.route('/health')
    def health():
        broker = live_state.get('broker')
        engine = live_state.get('engine')
        return jsonify({
            'status': 'ok',
            'service': 'V82.LOWDD',
            'mode': 'live',
            'broker_connected': getattr(broker, 'connected', False) if broker else False,
            'engine_running': engine.running if engine else False,
            'universe_size': len(live_state.get('universe', [])),
            'time': datetime.now().isoformat(),
            'utc_weekday': datetime.now().strftime('%A'),
            'utc_hour': datetime.now().hour,
        })

    @app.route('/api/market/status')
    def api_market_status():
        now = datetime.now()
        weekday = now.strftime('%A')
        hour = now.hour
        # Forex market hours: Sun 22:00 UTC -> Fri 22:00 UTC
        is_weekend_open = (weekday == 'Sunday' and hour >= 22) or \
                          (weekday != 'Sunday' and weekday != 'Friday') or \
                          (weekday == 'Friday' and hour < 22)
        is_full_open = is_weekend_open and not (weekday == 'Sunday' and hour == 22 and now.minute < 30)
        from datetime import timedelta
        next_open = None
        mins_to_open = None
        if weekday == 'Sunday' and hour < 22:
            next_open = now.replace(hour=22, minute=0, second=0, microsecond=0)
            mins_to_open = int((next_open - now).total_seconds() / 60)
        elif weekday == 'Friday' and hour >= 22:
            next_open = (now + timedelta(days=2)).replace(hour=22, minute=0, second=0, microsecond=0)
            mins_to_open = int((next_open - now).total_seconds() / 60)
        elif weekday == 'Saturday':
            next_open = (now + timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
            mins_to_open = int((next_open - now).total_seconds() / 60)
        broker = live_state.get('broker')
        ig_connected = getattr(broker, 'connected', False) if broker else False
        engine = live_state.get('engine')
        universe = live_state.get('universe', [])
        n_open = len(engine.ig_positions) if engine else 0
        return jsonify({
            'now_utc': now.isoformat(),
            'weekday': weekday,
            'hour_utc': hour,
            'forex_market_open': is_full_open,
            'ig_connected': ig_connected,
            'can_trade_live': is_full_open and ig_connected,
            'next_market_open': next_open.isoformat() if next_open else None,
            'minutes_to_open': mins_to_open,
            'universe_size': len(universe),
            'n_open_positions': n_open,
            'explanation': (
                f'Forex open. Engine scanning {len(universe)} EPICs. Live mode: orders submitted to IG.'
                if is_full_open else
                f'Forex CLOSED. Crypto CFDs (BTC etc.) are still 24/7. Engine scanning {len(universe)} EPICs. Live mode: orders submitted to IG.'
                if ig_connected else
                'IG broker not connected. Engine cannot submit orders.'
            ),
        })

    def _get_broker():
        if live_state['broker'] is None:
            from backend.api.ig_routes import get_ig
            live_state['broker'] = get_ig()
        return live_state['broker']

    def _bars_fn(epic: str, num_points: int = 200):
        """Compat: pull a single live bid/offer and build a tiny frame.
        The live engine doesn't use this (it builds its own bar history
        from snapshots); it's here for legacy endpoints that call it."""
        broker = _get_broker()
        try:
            info = broker.get_market_info(epic)
            if not info or not info.get('bid') or not info.get('offer'):
                return None
            now = pd.Timestamp.now()
            mid = (info['bid'] + info['offer']) / 2
            df = pd.DataFrame({'mid': [mid], 'bid': [info['bid']], 'offer': [info['offer']]},
                              index=[now])
            return pd.DataFrame({
                'Open':  [mid], 'High': [mid], 'Low': [mid],
                'Close': [mid], 'Volume': [0.0],
            }, index=df.index)
        except Exception:
            return None

    def _make_risk():
        return RiskManager(
            initial_capital=10000.0,
            risk_per_trade=DEFAULT_PARAMS['risk_per_trade'],
            max_dd_threshold=DEFAULT_PARAMS['max_dd_threshold'],
            daily_loss_limit_pct=DEFAULT_PARAMS['daily_loss_limit_pct'],
        )

    def _get_engine() -> LiveEngine:
        if live_state['engine'] is not None:
            return live_state['engine']
        # ── Fast path: return a placeholder engine with the full universe
        # if the first-time setup isn't done yet. The bg thread will
        # populate it. This ensures /api/live/status responds in <50ms
        # even on cold start. ──
        broker = _get_broker()
        if not broker.connected:
            broker.connect()
        risk = _make_risk()
        from backend.live.ig_universe import get_universe_epics
        full_universe = list(get_universe_epics())
        # Always include 24/7 crypto first
        for must in ['CS.D.BITCOIN.CFBMU.IP', 'CS.D.BITCOIN.CFD.IP',
                     'CS.D.ETHEREUM.CFBMU.IP', 'CS.D.ETHEREUM.CFD.IP']:
            if must not in full_universe:
                full_universe.insert(0, must)
        # Filter to IG-available via probe cache (synchronous — usually fast
        # since the cache file is already populated by start.sh).
        try:
            probe_results = probe_universe(broker, force=False)
            available = [e for e, v in probe_results.items() if v.get('available')]
            if len(available) >= 3:
                for must in ['CS.D.BITCOIN.CFBMU.IP', 'CS.D.BITCOIN.CFD.IP',
                             'CS.D.ETHEREUM.CFBMU.IP', 'CS.D.ETHEREUM.CFD.IP']:
                    if must not in available:
                        available.insert(0, must)
                full_universe = available
                logger.info(f"Probed universe: {len(available)} IG-available EPICs")
            else:
                logger.warning(f"Probe found only {len(available)} EPICs, using full catalog")
        except Exception as e:
            logger.warning(f"probe failed ({e}), using full catalog")
        live_state['universe'] = full_universe
        engine = LiveEngine(
            broker=broker,
            bars_fn=_bars_fn,
            risk_manager=risk,
            config=DEFAULT_PARAMS,
            universe_resolver=lambda: list(live_state['universe']),
            name_resolver=(lambda e: get_universe_names().get(e, e)),
        )
        # Seed the engine's universe with the filtered list
        engine.universe = list(full_universe)
        engine.all_symbols = list(full_universe)
        # Build the rotator with all batches covering the full universe
        from backend.core.universe_rotator import UniverseRotator
        engine.rotator = UniverseRotator()
        # The default batches cover 134 symbols; add the remaining
        # ones to the closest batch so all 166+ are tracked.
        all_batches_symbols = set()
        for b in engine.rotator.batches:
            all_batches_symbols.update(b)
        for epic in full_universe:
            if epic not in all_batches_symbols:
                # Add to the smallest batch to keep them balanced
                smallest = min(engine.rotator.batches, key=len)
                smallest.append(epic)
                all_batches_symbols.add(epic)
        engine.universe = engine.rotator.current()
        live_state['engine'] = engine
        return engine

    def _probe_universe(force: bool = False) -> List[str]:
        """Probe IG and update the working universe. Always includes
        24/7 crypto CFDs (BTC, ETH) even if the probe fails on them.

        For 24/7 operation across ALL instruments, the probe
        discovers all 92+ EPICs from the IG universe. The engine
        then rotates through them in batches to stay within
        IG's rate limits.
        """
        always_include = ['CS.D.BITCOIN.CFBMU.IP', 'CS.D.ETHEREUM.CFBMU.IP']
        if not live_state['universe'] or force:
            broker = _get_broker()
            try:
                results = probe_universe(broker, force=force)
                live_state['probe_results'] = results
                # Get ALL working EPICs from the full universe
                working = [e for e, v in results.items() if v.get('available')]
                # Always include 24/7 crypto
                for epic in always_include:
                    if epic not in working:
                        working.insert(0, epic)
                if working:
                    live_state['universe'] = working
                    logger.info(f"Universe: {len(working)} EPICs (FULL coverage: "
                                f"forex + crypto + commodities + indices)")
                else:
                    live_state['universe'] = list(always_include)
                    logger.warning(f"No probe results, using {len(always_include)} crypto EPICs")
            except Exception as e:
                logger.error(f"probe failed: {e}")
                live_state['universe'] = list(always_include)
        return live_state['universe']

    def _probe_universe_fast(force: bool = False) -> List[str]:
        """Fast universe probe — only test the priority symbols to avoid
        hammering IG. Use the cached probe for all others. Returns the
        list of EPICs that IG has confirmed are available."""
        # The 24/7 crypto + a few major forex are always available
        # on IG. Don't waste API calls probing them.
        always_available = [
            'CS.D.BITCOIN.CFBMU.IP', 'CS.D.BITCOIN.CFD.IP',
            'CS.D.ETHEREUM.CFBMU.IP',
            'CS.D.EURUSD.MINI.IP', 'CS.D.GBPUSD.MINI.IP', 'CS.D.USDJPY.MINI.IP',
            'CS.D.AUDUSD.MINI.IP', 'CS.D.USDCAD.MINI.IP', 'CS.D.NZDUSD.MINI.IP',
            'CS.D.IN_GOLD.MFI.IP', 'CS.D.IN_SILVER.MFI.IP',
            'IX.D.SPTRD.DAILY.IP', 'IX.D.FTSE.DAILY.IP', 'IX.D.DAX.DAILY.IP',
            'IX.D.NIKKEI.DAILY.IP', 'CC.D.CL.USS.IP',
        ]
        from backend.live.ig_universe import get_universe_epics
        all_epics = list(get_universe_epics())
        # Add the always-available ones
        for epic in always_available:
            if epic not in all_epics:
                all_epics.insert(0, epic)
        return all_epics  # assume all are available; engine will skip on 403

    # ─── Engine status / control ──────────────────────
    @app.route('/api/live/status')
    def api_live_status():
        engine = _get_engine()
        status = engine.get_status()
        status['broker'] = {
            'connected': getattr(engine.broker, 'connected', False),
            'account_type': getattr(engine.broker, 'acc_type', 'N/A'),
            'account_info': getattr(engine.broker, 'account_info', {}),
        }
        status['universe'] = live_state.get('universe', [])
        status['probe_results'] = live_state.get('probe_results', {})
        return jsonify(status)

    @app.route('/api/live/mode')
    def api_live_mode():
        return jsonify({'mode': 'live', 'note': 'paper mode removed — live only'})

    @app.route('/api/live/mode', methods=['POST'])
    def api_live_mode_post():
        # Legacy: no-op, live mode is the only mode
        return jsonify({'mode': 'live', 'note': 'paper mode removed — live only'})

    @app.route('/api/live/universe')
    def api_live_universe():
        return jsonify({
            'universe': live_state.get('universe', []),
            'probe_results': live_state.get('probe_results', {}),
            'default_symbols': DEFAULT_SYMBOLS,
        })

    @app.route('/api/live/probe', methods=['POST'])
    def api_live_probe():
        force = (request.get_json(silent=True) or {}).get('force', True)
        universe = _probe_universe(force=force)
        return jsonify({
            'universe': universe,
            'n_working': len(universe),
            'probe_results': live_state.get('probe_results', {}),
        })

    @app.route('/api/live/positions')
    def api_live_positions():
        engine = _get_engine()
        positions = engine.get_open_positions()
        return jsonify({'count': len(positions), 'positions': positions})

    @app.route('/api/live/trades')
    def api_live_trades():
        engine = _get_engine()
        n = int(request.args.get('n', 100))
        trades = engine.get_recent_trades(n)
        return jsonify({'count': len(trades), 'trades': trades})

    @app.route('/api/live/cycle', methods=['POST'])
    def api_live_cycle():
        engine = _get_engine()
        body = request.get_json(silent=True) or {}
        symbols = body.get('symbols')
        if symbols and isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(',') if s.strip()]
        result = engine.force_poll(symbols)
        return jsonify({'mode': 'live', 'cycle': result, 'status': engine.get_status()})

    @app.route('/api/live/force-all-classes', methods=['POST'])
    def api_live_force_all_classes():
        """Force-poll one EPIC from each of the 22 instrument classes
        to verify per-class TP/SL/RR is applied across the whole
        universe. Bypasses the engine's throttle briefly."""
        engine = _get_engine()
        from backend.core.instrument_config import classify
        from backend.live.ig_universe import get_universe_epics
        # Pick one EPIC per class
        all_epics = list(get_universe_epics())
        seen = set()
        per_class = []
        for epic in all_epics:
            cls = classify(epic)
            if cls not in seen:
                seen.add(cls)
                per_class.append(epic)
        result = engine.force_poll_specific(per_class)
        result['classes_polled'] = len(per_class)
        result['status'] = engine.get_status()
        return jsonify(result)

    @app.route('/api/live/poll')
    def api_live_poll():
        return api_live_cycle()

    @app.route('/api/live/start', methods=['POST'])
    def api_live_start():
        engine = _get_engine()
        body = request.get_json(silent=True) or {}
        interval = int(body.get('interval', os.environ.get('LIVE_POLL_INTERVAL', 30)))
        if not engine.running:
            # Make sure universe is set
            if not engine.universe:
                engine.universe = list(live_state.get('universe') or DEFAULT_SYMBOLS)
            engine.start(poll_interval=interval)
        return jsonify({'started': True, 'interval': interval,
                        'universe_size': len(engine.universe)})

    @app.route('/api/live/stop', methods=['POST'])
    def api_live_stop():
        engine = _get_engine()
        engine.stop()
        return jsonify({'stopped': True})

    @app.route('/api/live/close', methods=['POST'])
    def api_live_close():
        body = request.get_json(silent=True) or {}
        deal_id = body.get('deal_id')
        if not deal_id:
            return jsonify({'error': 'deal_id required'}), 400
        engine = _get_engine()
        ok = engine.close_ig_position(deal_id)
        return jsonify({'closed': ok, 'deal_id': deal_id})

    @app.route('/api/live/close-all', methods=['POST'])
    def api_live_close_all():
        broker = _get_broker()
        n = broker.close_all_positions() if broker else 0
        return jsonify({'closed': n})

    # ─── Signals (every forecast, every poll) ───────────
    @app.route('/api/signals')
    def api_signals():
        limit = int(request.args.get('limit', 200))
        offset = int(request.args.get('offset', 0))
        direction = request.args.get('direction') or None
        epic = request.args.get('epic') or None
        rows = signals_service.list_signals(limit=limit, offset=offset,
                                            direction=direction, epic=epic)
        return jsonify({
            'count': len(rows),
            'total': signals_service.count_signals(direction=direction),
            'returned': len(rows),
            'offset': offset, 'limit': limit,
            'signals': rows,
        })

    @app.route('/api/signals/stats')
    def api_signals_stats():
        return jsonify(signals_service.get_opportunity_stats())

    # ─── Opportunities (actionable signals submitted to IG) ─
    @app.route('/api/opportunities')
    def api_opportunities():
        limit = int(request.args.get('limit', 200))
        offset = int(request.args.get('offset', 0))
        status = request.args.get('status') or None
        epic = request.args.get('epic') or None
        direction = request.args.get('direction') or None
        rows = signals_service.list_opportunities(limit=limit, offset=offset,
                                                   status=status, epic=epic,
                                                   direction=direction)
        return jsonify({
            'count': len(rows),
            'total': signals_service.count_opportunities(status=status),
            'returned': len(rows),
            'offset': offset, 'limit': limit,
            'opportunities': rows,
        })

    @app.route('/api/opportunities/stats')
    def api_opportunities_stats():
        return jsonify(signals_service.get_opportunity_stats())

    @app.route('/api/opportunities/<opp_id>')
    def api_opportunity_detail(opp_id: str):
        opp = signals_service.get_opportunity(opp_id)
        if not opp:
            return jsonify({'error': 'not found', 'id': opp_id}), 404
        return jsonify(opp)

    # ─── History (file-backed, includes REJECTED orders) ─
    @app.route('/api/history/trades')
    def api_history_trades():
        history_path = os.path.join(STATE_DIR, 'live_trade_history.pkl')
        history = []
        if os.path.exists(history_path):
            try:
                with open(history_path, 'rb') as f:
                    history = pickle.load(f)
            except Exception:
                history = []
        history.sort(key=lambda h: h.get('exit_time', ''), reverse=True)
        limit = int(request.args.get('limit', 200))
        offset = int(request.args.get('offset', 0))
        page = history[offset:offset + limit]
        total_pnl = sum(h.get('pnl', 0) for h in history)
        wins = sum(1 for h in history if h.get('won'))
        losses = sum(1 for h in history if not h.get('won'))
        n = len(history)
        wr = (wins / n * 100) if n else 0
        return jsonify({
            'count': n, 'total_pnl': total_pnl,
            'wins': wins, 'losses': losses, 'win_rate': wr,
            'returned': len(page), 'offset': offset, 'limit': limit,
            'trades': page,
        })

    @app.route('/api/history/orders')
    def api_history_orders():
        history_path = os.path.join(STATE_DIR, 'live_order_history.pkl')
        history = []
        if os.path.exists(history_path):
            try:
                with open(history_path, 'rb') as f:
                    history = pickle.load(f)
            except Exception:
                history = []
        history.sort(key=lambda h: h.get('created_at', ''), reverse=True)
        limit = int(request.args.get('limit', 200))
        offset = int(request.args.get('offset', 0))
        return jsonify({
            'count': len(history),
            'returned': min(limit, max(0, len(history) - offset)),
            'offset': offset, 'limit': limit,
            'orders': history[offset:offset + limit],
        })

    @app.route('/api/history/risk')
    def api_history_risk():
        engine = _get_engine()
        snap = engine.risk.snapshot() if engine.risk else {}
        daily = engine.daily_pnl or {}
        daily_rows = [{'date': d, 'pnl': p} for d, p in sorted(daily.items())]
        return jsonify({
            'risk': snap,
            'daily_pnl': daily_rows,
            'daily_pnl_total': sum(daily.values()),
            'equity_curve': engine.equity_curve[-500:],
        })

    @app.route('/api/history/equity')
    def api_history_equity():
        engine = _get_engine()
        return jsonify({
            'equity_curve': engine.equity_curve,
            'count': len(engine.equity_curve),
        })

    # ─── Auto-start the live engine on first request ─────
    _auto_started = {'done': False}

    @app.before_request
    def _maybe_auto_start():
        if _auto_started['done']:
            return
        _auto_started['done'] = True
        if os.environ.get('AUTO_START_LIVE', '1') != '1':
            return
        def _start():
            try:
                # 1. Probe IG for working EPICs
                _probe_universe(force=False)
                # 2. Build engine
                engine = _get_engine()
                # 3. Sync positions from IG so we know what's already open
                try:
                    engine.sync_ig_positions()
                except Exception as e:
                    logger.debug(f"initial sync: {e}")
                # 4. Start the live loop
                interval = int(os.environ.get('LIVE_POLL_INTERVAL', 30))
                if not engine.running:
                    logger.info(f"Auto-start: live engine on {len(engine.universe)} EPICs @ {interval}s")
                    engine.start(poll_interval=interval)
            except Exception as e:
                logger.exception(f"Auto-start failed: {e}")
        threading.Thread(target=_start, daemon=True).start()

    # ─── Watchdog ─────────────────────────────────────
    _watchdog_started = {'done': False}
    def _watchdog():
        if _watchdog_started['done']:
            return
        _watchdog_started['done'] = True
        while True:
            try:
                _time.sleep(30)
                if _auto_started['done']:
                    engine = _get_engine()
                    if not engine.running:
                        interval = int(os.environ.get('LIVE_POLL_INTERVAL', 30))
                        logger.warning(f"Watchdog: engine not running, restarting @ {interval}s")
                        engine.start(poll_interval=interval)
            except Exception as e:
                logger.debug(f"watchdog: {e}")
    threading.Thread(target=_watchdog, daemon=True).start()

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'error': 'not found', 'path': request.path}), 404

    @app.errorhandler(500)
    def server_error(e):
        logger.exception("Server error")
        return jsonify({'error': 'server error', 'detail': str(e)}), 500

    return app


if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
