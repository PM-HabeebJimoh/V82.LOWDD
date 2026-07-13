"""
V82.LOWDD - IG Markets REST API routes.

IG Markets covers EVERY asset class via a single login:
  - FOREX         (50+ major/minor/exotic pairs)
  - CRYPTO CFDs   (BTC, ETH, LTC, BCH, XRP, ADA, DOT, SOL, AVAX, ...)
  - COMMODITIES   (Gold, Silver, Oil, Copper, Natural Gas, Wheat, ...)
  - INDICES       (S&P 500, FTSE 100, DAX, Nikkei, Wall Street, ...)
  - STOCKS        (10,000+)
  - OPTIONS       (vanilla and turbo)

This is the SOLE broker for V82.LOWDD. The trading system is designed to
run live against IG Markets.

Set these in Replit Secrets:
  IG_USERNAME       - your IG account email
  IG_PASSWORD       - your IG account password
  IG_API_KEY        - API key from https://labs.ig.com/api-gateway
  IG_ACCOUNT_NUMBER (optional) - your account ID
  IG_ACCOUNT_TYPE   - "DEMO" (free practice) or "LIVE" (real money)
"""
import os
import logging
from typing import Optional, List, Dict

from flask import jsonify, request
import pandas as pd
import numpy as np

from ..live.ig_broker import IGBroker, IG_AVAILABLE
from ..live.ig_universe import (
    get_default_universe,
    get_universe_by_class,
    get_universe_epics,
    get_universe_names,
    discover_ig_universe,
    flatten_discovered,
    DEFAULT_UNIVERSE,
)

logger = logging.getLogger(__name__)


def jsonify_records(records):
    """Convert numpy/pandas types to native Python for JSON."""
    out = []
    for r in records:
        clean = {}
        for k, v in r.items():
            if isinstance(v, (np.bool_,)):
                clean[k] = bool(v)
            elif isinstance(v, (np.integer,)):
                clean[k] = int(v)
            elif isinstance(v, (np.floating,)):
                clean[k] = float(v)
            elif isinstance(v, pd.Timestamp):
                clean[k] = str(v)
            else:
                clean[k] = v
        out.append(clean)
    return out


import threading

_ig = None
_ig_lock = threading.Lock()
_universe_cache: Optional[Dict] = None  # in-memory discovery cache


def get_ig() -> IGBroker:
    """Singleton IGBroker. Guarded by a lock so concurrent requests
    (e.g. the auto-start thread racing an incoming HTTP request) can't
    each create their own broker and open duplicate IG sessions."""
    global _ig
    if _ig is not None:
        return _ig
    with _ig_lock:
        if _ig is None:
            broker = IGBroker(
                username=os.environ.get('IG_USERNAME'),
                password=os.environ.get('IG_PASSWORD'),
                api_key=os.environ.get('IG_API_KEY'),
                acc_type=os.environ.get('IG_ACCOUNT_TYPE', 'DEMO'),
                acc_number=os.environ.get('IG_ACCOUNT_NUMBER'),
            )
            if broker._has_credentials():
                try:
                    broker.connect()
                except Exception as e:
                    logger.warning(f"IG connect at startup failed: {e}")
            _ig = broker
    return _ig


def register_routes(app):
    """Register IG Markets routes on the Flask app."""

    # ─── Universe (curated + discovery) ───────────────
    @app.route('/api/ig/universe')
    def api_ig_universe():
        """The full curated default universe across all 5 asset classes.

        100+ EPICs covering forex (majors, minors, exotics), crypto, commodities
        (metals, energy, softs, grains), and indices (US, EU, Asia).
        """
        catalog = get_universe_by_class()
        total = sum(len(v) for v in catalog.values())
        counts = {k: len(v) for k, v in catalog.items()}
        return jsonify({
            'source': 'IG Markets',
            'total_markets': total,
            'asset_classes': counts,
            'catalog': catalog,
            'flat_list': get_default_universe(),
            'note': 'Use /api/ig/search?q=<term> to discover more markets beyond this curated set. '
                    'To trade, set IG credentials in Replit Secrets.',
        })

    @app.route('/api/ig/universe/epics')
    def api_ig_universe_epics():
        """Flat list of just the EPIC codes for the default universe."""
        epics = get_universe_epics()
        return jsonify({
            'count': len(epics),
            'epics': epics,
            'names': get_universe_names(),
        })

    @app.route('/api/ig/universe/discover')
    def api_ig_universe_discover():
        """Live discovery: query IG for all available markets by term.

        Returns 100+ discovered markets grouped by asset class.
        Cached for 24h; pass ?refresh=true to force a fresh query.
        """
        global _universe_cache
        force = request.args.get('refresh', 'false').lower() == 'true'
        if not force and _universe_cache is not None:
            return jsonify({
                'source': 'IG Markets (live discovery, in-memory cache)',
                **{
                    'n_by_class': {k: len(v) for k, v in _universe_cache.items()},
                    'total': sum(len(v) for v in _universe_cache.values()),
                    'discovered': _universe_cache,
                }
            })
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected. Add IG credentials.'}), 400
        if force:
            discovered = discover_ig_universe(b, force_refresh=True)
        else:
            discovered = discover_ig_universe(b)
        _universe_cache = discovered
        return jsonify({
            'source': 'IG Markets (live discovery, real REST API)',
            'n_by_class': {k: len(v) for k, v in discovered.items()},
            'total': sum(len(v) for v in discovered.values()),
            'discovered': discovered,
        })

    # ─── Status & connection ───────────────────────────
    @app.route('/api/ig/status')
    def api_ig_status():
        if not IG_AVAILABLE:
            return jsonify({
                'available': False,
                'error': 'trading_ig not installed. Run: pip install trading-ig',
            }), 500
        b = get_ig()
        has_keys = b._has_credentials()
        result = {
            'available': True,
            'has_credentials': has_keys,
            'account_type': b.acc_type,
            'connected': b.connected,
            'universe_size': len(get_universe_epics()),
            'covers': ['FOREX', 'CRYPTO CFDs', 'INDICES', 'COMMODITIES', 'STOCKS', 'OPTIONS'],
            'note': (
                'No IG credentials set. Add IG_USERNAME, IG_PASSWORD, IG_API_KEY '
                'in Replit Secrets. Get a free DEMO account at ig.com/uk/login.'
                if not has_keys else
                f'Connected to IG {b.acc_type}: ${b.account_info.get("balance", 0):.2f}'
            ),
        }
        if b.connected:
            result['account'] = b.account_info
        return jsonify(result)

    @app.route('/api/ig/connect', methods=['POST'])
    def api_ig_connect():
        b = get_ig()
        ok = b.connect()
        if ok:
            return jsonify({'connected': True, 'account': b.account_info})
        return jsonify({
            'connected': False,
            'error': 'IG connection failed. Check IG_USERNAME, IG_PASSWORD, IG_API_KEY in Replit Secrets.',
        }), 400

    @app.route('/api/ig/account')
    def api_ig_account():
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        return jsonify(b.get_account_summary())

    @app.route('/api/ig/search')
    def api_ig_search():
        """Search IG for markets by name (e.g. EURUSD, Bitcoin, Gold, FTSE)."""
        term = request.args.get('q', '')
        if not term:
            return jsonify({'error': 'Missing q parameter'}), 400
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        results = b.search_market(term)
        return jsonify({
            'query': term,
            'count': len(results),
            'source': 'IG Markets REST API (real, live)',
            'markets': results,
        })

    @app.route('/api/ig/market/<epic>')
    def api_ig_market(epic: str):
        """Get real-time market snapshot for any EPIC in the universe."""
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        info = b.get_market_info(epic)
        if not info:
            return jsonify({'error': f'No market data for {epic}'}), 404
        # Annotate with our curated name if available
        names = get_universe_names()
        info['display_name'] = names.get(epic, epic)
        return jsonify(info)

    @app.route('/api/ig/candles')
    def api_ig_candles():
        """Get historical OHLCV bars for any IG market.

        Query params:
          epic: instrument EPIC (e.g. CS.D.EURUSD.MINI.IP)
          resolution: MINUTE, MINUTE_5, MINUTE_15, HOUR, DAY (default MINUTE_5)
          num_points: how many bars (default 1000, max ~5000)
        """
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected. Add IG credentials in Replit Secrets.'}), 400
        epic = request.args.get('epic', '')
        if not epic:
            return jsonify({'error': 'Missing epic parameter. Use /api/ig/search?q=EURUSD to find EPICs.'}), 400
        resolution = request.args.get('resolution', 'MINUTE_5')
        num_points = min(int(request.args.get('num_points', 1000)), 5000)
        df = b.get_historical_prices(epic, resolution=resolution, num_points=num_points)
        if df is None or df.empty:
            return jsonify({
                'error': f'No candles for {epic} at {resolution}. Check the EPIC is valid.',
            }), 404
        df_out = df.reset_index()
        df_out = df_out.rename(columns={df_out.columns[0]: 'timestamp'})
        records = jsonify_records(df_out.to_dict(orient='records'))
        names = get_universe_names()
        return jsonify({
            'epic': epic,
            'display_name': names.get(epic, epic),
            'resolution': resolution,
            'count': len(records),
            'source': 'IG Markets REST API (real, all asset classes)',
            'first': str(df.index[0]),
            'last': str(df.index[-1]),
            'candles': records,
        })

    # ─── Positions & orders ────────────────────────────
    @app.route('/api/ig/positions')
    def api_ig_positions():
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        positions = b.get_open_positions()
        return jsonify({
            'source': 'IG Markets REST API (real)',
            'account_type': b.acc_type,
            'count': len(positions),
            'positions': positions,
        })

    @app.route('/api/ig/orders')
    def api_ig_orders():
        b = get_ig()
        return jsonify({
            'source': 'IG Markets (real)',
            'count': len(b.orders),
            'orders': [
                {
                    'deal_reference': o.deal_reference,
                    'instrument': o.instrument,
                    'direction': o.direction,
                    'n_units': o.n_units,
                    'status': o.status,
                    'created_at': str(o.created_at),
                    'filled_price': o.filled_price,
                }
                for o in b.orders[-100:]
            ],
        })

    @app.route('/api/ig/order', methods=['POST'])
    def api_ig_place_order():
        """Place a real OTC order.

        body: { epic, direction, size, order_type, limit_level, stop_level, currency_code }
        """
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected. Add IG credentials in Replit Secrets.'}), 400
        body = request.get_json(force=True, silent=True) or {}
        epic = body.get('epic', '')
        direction = body.get('direction', 'BUY').upper()
        size = float(body.get('size', 0))
        if not epic or size <= 0:
            return jsonify({'error': 'Missing epic or invalid size'}), 400
        if b.acc_type == 'LIVE' and not body.get('confirm_live', False):
            return jsonify({'error': 'Refusing LIVE order without confirm_live=true'}), 400
        order = b.submit_order(
            epic=epic, direction=direction, size=size,
            order_type=body.get('order_type', 'MARKET'),
            limit_level=body.get('limit_level'),
            stop_level=body.get('stop_level'),
            currency_code=body.get('currency_code', 'USD'),
        )
        if order is None:
            return jsonify({'error': 'Order submission failed'}), 500
        return jsonify({
            'submitted': True,
            'deal_reference': order.deal_reference,
            'instrument': order.instrument,
            'direction': order.direction,
            'n_units': order.n_units,
            'status': order.status,
        })

    @app.route('/api/ig/close/<deal_id>', methods=['POST'])
    def api_ig_close(deal_id: str):
        body = request.get_json(force=True, silent=True) or {}
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        positions = b.get_open_positions()
        pos = next((p for p in positions if p['deal_id'] == deal_id), None)
        if not pos:
            return jsonify({'error': f'Position {deal_id} not found'}), 404
        opposite = 'SELL' if pos['direction'] == 'BUY' else 'BUY'
        ok = b.close_position(deal_id=deal_id, direction=opposite,
                              epic=pos['epic'], size=pos['size'])
        return jsonify({'closed': ok, 'deal_id': deal_id})

    @app.route('/api/ig/close-all', methods=['POST'])
    def api_ig_close_all():
        body = request.get_json(force=True, silent=True) or {}
        if not body.get('confirm', False):
            return jsonify({'error': 'Set confirm=true in body'}), 400
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        n = b.close_all_positions()
        return jsonify({'closed': n})

    @app.route('/api/ig/cancel-all', methods=['POST'])
    def api_ig_cancel_all():
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        n = b.cancel_all_orders()
        return jsonify({'cancelled': n})

    # ─── Bulk snapshot (for the live loop / dashboard) ─
    @app.route('/api/ig/snapshot')
    def api_ig_snapshot():
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        epics = get_universe_epics()
        names = get_universe_names()
        quotes = []
        unavailable = []
        n_ok = 0
        n_err = 0
        for epic in epics:
            try:
                info = b.get_market_info(epic)
                if info and info.get('bid') is not None and info.get('bid') > 0:
                    info['display_name'] = names.get(epic, epic)
                    quotes.append(info)
                    n_ok += 1
                elif info is None:
                    unavailable.append(epic)
                    n_err += 1
                else:
                    unavailable.append(epic)
                    n_err += 1
            except Exception as e:
                logger.debug(f"Snapshot for {epic} failed: {e}")
                unavailable.append(epic)
                n_err += 1
        # Group by category for the response
        catalog = get_universe_by_class()
        by_class = {}
        for category, items in catalog.items():
            by_class[category] = []
        for q in quotes:
            for category, items in catalog.items():
                if any(it['epic'] == q['epic'] for it in items):
                    by_class[category].append(q)
                    break
        return jsonify({
            'source': 'IG Markets REST API (real, live)',
            'timestamp': pd.Timestamp.now().isoformat(),
            'total_markets': len(epics),
            'n_responded': n_ok,
            'n_unavailable': n_err,
            'unavailable_epics': unavailable,
            'by_class': {k: v for k, v in by_class.items() if v},
            'flat': quotes,
        })

    @app.route('/api/ig/probe')
    def api_ig_probe():
        """Probe which EPICs actually work on this IG account (have historical data).

        Returns:
          available: list of EPICs that returned 200 on /prices
          unavailable: list of EPICs that failed
          by_class: organised view of the working universe

        This is useful to filter the auto-start universe to only instruments
        that have data on the user's specific IG account.
        """
        b = get_ig()
        if not b.connected:
            if not b.connect():
                return jsonify({'error': 'Not connected'}), 400
        epics = get_universe_epics()
        names = get_universe_names()
        available = []
        unavailable = []
        # Use a smaller bar count for probing to save API calls
        for epic in epics:
            try:
                df = b.get_historical_prices(epic, resolution='MINUTE_5', num_points=5)
                if df is not None and not df.empty:
                    available.append({
                        'epic': epic,
                        'name': names.get(epic, epic),
                        'n_bars': len(df),
                    })
                else:
                    unavailable.append({'epic': epic, 'name': names.get(epic, epic)})
            except Exception as e:
                unavailable.append({'epic': epic, 'name': names.get(epic, epic), 'error': str(e)[:80]})
        # Group by class
        by_class = {}
        catalog = get_universe_by_class()
        avail_epics = {a['epic'] for a in available}
        for category, items in catalog.items():
            by_class[category] = [a for a in available if any(it['epic'] == a['epic'] for it in items)]
        return jsonify({
            'total_tested': len(epics),
            'n_available': len(available),
            'n_unavailable': len(unavailable),
            'available': available,
            'unavailable': unavailable,
            'by_class': by_class,
        })
