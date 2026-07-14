"""
V82.LOWDD - IG Markets Broker (REAL, full coverage).

IG Markets covers EVERYTHING:
  - Forex (30+ pairs)
  - Cryptocurrencies (BTC, ETH, etc. via CFDs)
  - Commodities (Gold, Silver, Oil, etc.)
  - Indices (S&P 500, FTSE 100, etc.)
  - Stocks/Equities (US, UK, EU)
  - Options (vanilla and turbo)
  - ETFs

This broker talks to IG's real REST API and uses Lightstreamer for
real-time price streaming.

Free practice account: https://www.ig.com/uk/login
Get API key: https://labs.ig.com/api-gateway

The trading_ig library handles all the OAuth session, EPIC resolution,
and order placement.
"""
import os
import json
import time
import logging
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    from trading_ig import IGService, IGStreamService
    IG_AVAILABLE = True
except ImportError as e:
    IG_AVAILABLE = False
    logger.error(f"trading_ig not installed: {e}")

# ── Pandas-version compat shim ──────────────────────────────────────
# trading_ig's conv_resol() builds a dict keyed by
# pandas.tseries.frequencies.to_offset("ME") to detect month-end
# resolution. The "ME" alias only exists on pandas >= 2.2; on the
# pandas version pinned in this project (2.1.x) to_offset("ME") raises
# ValueError, which crashes EVERY call to fetch_historical_prices_by_epic
# regardless of what resolution was actually requested (get_historical_prices
# silently swallows it and returns None, so calling code never sees a real
# error — this looked like "no historical data available" but was actually
# an unrelated pandas/trading_ig version mismatch).
if IG_AVAILABLE:
    try:
        import trading_ig.rest as _ig_rest

        def _safe_conv_resol(resolution):
            _map = {
                '1s': 'SECOND', '1min': 'MINUTE', '2min': 'MINUTE_2',
                '3min': 'MINUTE_3', '5min': 'MINUTE_5', '10min': 'MINUTE_10',
                '15min': 'MINUTE_15', '30min': 'MINUTE_30', '1h': 'HOUR',
                '2h': 'HOUR_2', '3h': 'HOUR_3', '4h': 'HOUR_4', 'd': 'DAY',
                'w': 'WEEK', 'm': 'MONTH', 'me': 'MONTH',
            }
            key = str(resolution).strip().lower()
            if key in _map:
                return _map[key]
            # Already an IG-style resolution string (e.g. 'MINUTE_5') — pass through.
            return resolution

        _ig_rest.conv_resol = _safe_conv_resol
        logger.info("Patched trading_ig.conv_resol for pandas %s compat", __import__('pandas').__version__)
    except Exception as e:
        logger.warning(f"Could not patch trading_ig conv_resol: {e}")


@dataclass
class IGOrder:
    """An order placed with IG."""
    deal_reference: str
    instrument: str  # EPIC code
    direction: str   # 'BUY' or 'SELL'
    n_units: float
    order_type: str
    status: str = 'PENDING'
    created_at: datetime = field(default_factory=datetime.now)
    filled_at: Optional[datetime] = None
    filled_price: Optional[float] = None
    deal_id: str = ''
    reason: str = ''  # rejection reason from IG (e.g. MARKET_CLOSED_WITH_EDITS)


class IGBroker:
    """Real IG Markets broker for V82.LOWDD."""

    # REST API base URLs
    DEMO_URL = 'https://demo-api.ig.com/gateway/deal'
    LIVE_URL = 'https://api.ig.com/gateway/deal'
    # Lightstreamer endpoint
    LS_DEMO = 'https://demo-apd.marketdatasystems.com'
    LS_LIVE = 'https://apd.marketdatasystems.com'

    def __init__(self,
                 username: str = None,
                 password: str = None,
                 api_key: str = None,
                 acc_type: str = 'DEMO',
                 acc_number: str = None):
        self.username = username or os.environ.get('IG_USERNAME', '')
        self.password = password or os.environ.get('IG_PASSWORD', '')
        self.api_key = api_key or os.environ.get('IG_API_KEY', '')
        self.acc_type = acc_type.upper() if acc_type else 'DEMO'
        self.acc_number = acc_number or os.environ.get('IG_ACCOUNT_NUMBER', '')
        self.ig_service: Optional[IGService] = None
        self.account_info: Dict = {}
        self.connected = False
        self.orders: List[IGOrder] = []
        self.open_positions: Dict[str, Dict] = {}
        # Throttle / allowance tracking (visible to dashboard)
        self._throttle_until: Optional[datetime] = None
        self._n_throttle_hits: int = 0
        self._n_403_responses: int = 0
        self._n_successful_fetches: int = 0
        self._n_empty_responses: int = 0
        self._last_successful_epic: Optional[str] = None
        self._last_successful_time: Optional[datetime] = None
        self._allowance_remaining: Optional[int] = None
        self._allowance_reset: Optional[datetime] = None

    def _get_base_url(self) -> str:
        return self.DEMO_URL if self.acc_type == 'DEMO' else self.LIVE_URL

    def _has_credentials(self) -> bool:
        return bool(self.username and self.password and self.api_key)

    def connect(self) -> bool:
        """Connect to IG and create a session. Retries on 403."""
        if not IG_AVAILABLE:
            logger.error("trading_ig not installed")
            return False
        if not self._has_credentials():
            logger.warning("IG: no credentials set (set IG_USERNAME, IG_PASSWORD, IG_API_KEY in Replit Secrets)")
            return False
        if self.connected:
            return True
        # Try up to 3 times with backoff
        import time
        for attempt in range(3):
            try:
                self.ig_service = IGService(
                    username=self.username,
                    password=self.password,
                    api_key=self.api_key,
                    acc_type=self.acc_type,
                )
                self.ig_service.create_session()
                if self.acc_number:
                    try:
                        self.ig_service.switch_account(self.acc_number, False)
                    except Exception as e:
                        logger.warning(f"switch_account failed: {e}")
                # Fetch account info
                try:
                    accounts_df = self.ig_service.fetch_accounts()
                    if accounts_df is not None and len(accounts_df) > 0:
                        for _, a in accounts_df.iterrows():
                            acc_id = a.get('accountId')
                            if not self.acc_number or acc_id == self.acc_number:
                                self.account_info = {
                                    'account_id': acc_id,
                                    'account_name': a.get('accountName'),
                                    'account_type': a.get('accountType'),
                                    'currency': a.get('currency'),
                                    'balance': float(a.get('balance', 0) or 0),
                                    'deposit': float(a.get('deposit', 0) or 0),
                                    'available': float(a.get('available', 0) or 0),
                                    'profit_loss': float(a.get('profitLoss', 0) or 0),
                                }
                                break
                except Exception as e:
                    logger.error(f"fetch_accounts failed: {e}")
                self.connected = True
                logger.info(f"Connected to IG {self.acc_type}: ${self.account_info.get('balance', 0):.2f} {self.account_info.get('currency', '')}")
                return True
            except Exception as e:
                err = str(e)[:100]
                logger.warning(f"IG connect attempt {attempt+1}/3 failed: {err}")
                if attempt < 2:
                    time.sleep(15 * (attempt + 1))
        return False

    def get_account_summary(self) -> Dict:
        """Refresh and return account summary."""
        if self.connected and self.ig_service:
            try:
                accounts_df = self.ig_service.fetch_accounts()
                if accounts_df is not None and len(accounts_df) > 0:
                    for _, a in accounts_df.iterrows():
                        acc_id = a.get('accountId')
                        if not self.acc_number or acc_id == self.acc_number:
                            self.account_info = {
                                'account_id': acc_id,
                                'account_name': a.get('accountName'),
                                'account_type': a.get('accountType'),
                                'currency': a.get('currency'),
                                'balance': float(a.get('balance', 0) or 0),
                                'deposit': float(a.get('deposit', 0) or 0),
                                'available': float(a.get('available', 0) or 0),
                                'profit_loss': float(a.get('profitLoss', 0) or 0),
                            }
                            break
            except Exception as e:
                logger.error(f"IG account refresh failed: {e}")
        return {
            **self.account_info,
            'connected': self.connected,
            'n_open_positions': len(self.open_positions),
            'n_orders': len(self.orders),
            'account_type': self.acc_type,
            'practice': self.acc_type == 'DEMO',
        }

    # ─── Market data (REAL FX/Crypto/CFD/Index/Commodity bars) ──
    def search_market(self, term: str) -> List[Dict]:
        """Search for markets by name (e.g. 'EURUSD', 'Bitcoin', 'Gold')."""
        if not self.connected:
            return []
        try:
            result = self.ig_service.search_markets(term)
            if hasattr(result, 'to_dict'):
                markets = result.to_dict('records')
            elif isinstance(result, dict):
                markets = result.get('markets', [])
            else:
                markets = []
            return [{
                'epic': m.get('epic'),
                'instrument_name': m.get('instrumentName'),
                'instrument_type': m.get('instrumentType'),
                'expiry': m.get('expiry'),
                'lot_size': m.get('lotSize'),
                'currency': m.get('currency'),
            } for m in markets[:20]]
        except Exception as e:
            logger.error(f"IG search_market failed: {e}")
            return []

    # IG Markets frequency format conversion
    # IG uses pandas-style frequency codes
    _IG_FREQ_MAP = {
        'SECOND': 's',
        'MINUTE': 'min',
        'MINUTE_5': '5min',
        'MINUTE_15': '15min',
        'MINUTE_30': '30min',
        'HOUR': 'h',
        'HOUR_4': '4h',
        'DAY': 'D',
        'WEEK': 'W',
        'MONTH': 'M',
    }

    def get_historical_prices(self, epic: str, resolution: str = 'MINUTE_5',
                              num_points: int = 5000) -> Optional['pd.DataFrame']:
        """Get real historical OHLCV bars from IG for any instrument.

        Args:
            epic: IG epic code (e.g. 'CS.D.EURUSD.MINI.IP')
            resolution: 'MINUTE_5', 'MINUTE', 'HOUR', 'DAY', etc.
            num_points: number of bars
        """
        import pandas as pd
        if not self.connected:
            return None
        # Translate to pandas frequency code
        ig_resolution = self._IG_FREQ_MAP.get(resolution.upper(), resolution)
        try:
            result = self.ig_service.fetch_historical_prices_by_epic(
                epic=epic, resolution=ig_resolution, numpoints=num_points
            )
            if not result:
                return None
            # trading_ig returns dict with 'prices' (a DataFrame), 'instrumentType', 'metadata'
            df = None
            if isinstance(result, dict) and 'prices' in result:
                prices_data = result['prices']
                if hasattr(prices_data, 'to_dict'):
                    # DataFrame with multi-level columns
                    df = prices_data
                elif isinstance(prices_data, list):
                    rows = []
                    for p in prices_data:
                        op = p.get('openPrice', {}) or {}
                        hp = p.get('highPrice', {}) or {}
                        lp = p.get('lowPrice', {}) or {}
                        cp = p.get('closePrice', {}) or {}
                        rows.append({
                            'Open': float(op.get('bid', 0) or op.get('lastTraded', 0) or 0),
                            'High': float(hp.get('bid', 0) or hp.get('lastTraded', 0) or 0),
                            'Low': float(lp.get('bid', 0) or lp.get('lastTraded', 0) or 0),
                            'Close': float(cp.get('bid', 0) or cp.get('lastTraded', 0) or 0),
                            'Volume': int(p.get('lastTradedVolume', 0) or 0),
                        })
                    if rows:
                        import pandas as pd
                        times = [pd.Timestamp(p.get('snapshotTimeUTC')).tz_localize(None)
                                 if p.get('snapshotTimeUTC') else pd.Timestamp.now()
                                 for p in prices_data]
                        df = pd.DataFrame(rows, index=pd.DatetimeIndex(times))
            elif hasattr(result, 'to_dict'):
                df = result
            if df is None or len(df) == 0:
                return None
            # Flatten multi-level columns if present (IG returns multi-index like ('bid','Open'))
            if isinstance(df.columns, pd.MultiIndex):
                # Build flat column names like 'bid_Open' then we'll keep 'bid_' side
                try:
                    df.columns = [f"{c[0]}_{c[1]}" if isinstance(c, tuple) else str(c)
                                  for c in df.columns]
                except Exception:
                    df.columns = [str(c) for c in df.columns]
            else:
                df.columns = [str(c) for c in df.columns]
            # Pick the 'bid' side (standard for IG CFD forex/commodities/crypto)
            # If 'bid_Open' exists, prefer that; else fall back to 'last_Open' or any 'Open'
            def _pick(side_prefixes):
                for side in side_prefixes:
                    cols = [c for c in df.columns if c.startswith(f"{side}_")]
                    if len(cols) >= 4:
                        mapping = {}
                        for c in cols:
                            short = c.split('_', 1)[1]  # 'Open', 'High', etc.
                            mapping[c] = short
                        df.rename(columns=mapping, inplace=True)
                        keep = [k for k in ['Open', 'High', 'Low', 'Close', 'Volume']
                                if k in df.columns]
                        return df[keep]
                # No prefix matched — try to find any OHLC columns directly
                if 'Open' in df.columns:
                    keep = [k for k in ['Open', 'High', 'Low', 'Close', 'Volume']
                            if k in df.columns]
                    return df[keep]
                return None
            df = _pick(['bid', 'last', 'ask'])
            if df is None or len(df) == 0:
                return None
            df.index.name = 'timestamp'
            # Drop any rows with all NaN
            df = df.dropna(how='all')
            return df
        except Exception as e:
            logger.error(f"IG get_historical_prices({epic}) failed: {e}")
            return None

    def get_market_info(self, epic: str) -> Optional[Dict]:
        """Get real-time market snapshot for an EPIC."""
        if not self.connected:
            return None
        # Respect our own backoff
        if self._throttle_until and datetime.now() < self._throttle_until:
            return None
        try:
            details = self.ig_service.fetch_market_by_epic(epic)
            # Handle 403 / allowance errors that come back as error dicts
            if isinstance(details, dict) and 'errorCode' in details:
                err_code = details.get('errorCode', '')
                if 'allowance' in err_code.lower() or 'exceeded' in err_code.lower() or '403' in str(err_code):
                    self._throttle_until = datetime.now() + timedelta(seconds=20)
                    self._n_throttle_hits += 1
                    self._n_403_responses += 1
                    logger.warning(f"IG throttle on {epic} ({err_code}): backing off 20s")
                return None
            node = details.get('snapshot', {}) if isinstance(details, dict) else {}
            # Track throttling via the underlying response
            if not node or (float(node.get('bid', 0)) == 0 and float(node.get('offer', 0)) == 0):
                self._n_empty_responses += 1
                return None
            self._n_successful_fetches += 1
            self._last_successful_epic = epic
            self._last_successful_time = datetime.now()
            # Try to extract allowance from headers if available
            try:
                resp = details.get('_response') if isinstance(details, dict) else None
                if resp and hasattr(resp, 'headers'):
                    hdr = resp.headers
                    if 'X-IG-ALLOWANCE-REMAINING' in hdr:
                        try:
                            self._allowance_remaining = int(hdr['X-IG-ALLOWANCE-REMAINING'])
                        except (ValueError, TypeError):
                            pass
                    if 'X-IG-ALLOWANCE-RESET' in hdr:
                        try:
                            self._allowance_reset = datetime.fromtimestamp(
                                int(hdr['X-IG-ALLOWANCE-RESET']))
                        except (ValueError, TypeError):
                            pass
            except Exception:
                pass
            return {
                'epic': epic,
                'bid': float(node.get('bid', 0)),
                'offer': float(node.get('offer', 0)),
                'mid': (float(node.get('bid', 0)) + float(node.get('offer', 0))) / 2,
                'spread': float(node.get('offer', 0)) - float(node.get('bid', 0)),
                'market_state': node.get('marketState'),
                'market_status': node.get('marketStatus'),
                'update_time': node.get('updateTime'),
            }
        except Exception as e:
            err_str = str(e)
            # Detect rate-limit (403) — surface to the caller
            if '403' in err_str or 'allowance' in err_str.lower() or 'exceeded' in err_str.lower() or 'ApiExceeded' in err_str:
                self._throttle_until = datetime.now() + timedelta(seconds=15)
                self._n_throttle_hits += 1
                self._n_403_responses += 1
                logger.warning(f"IG throttle on {epic}: backing off 15s ({err_str[:80]})")
            else:
                logger.error(f"IG get_market_info({epic}) failed: {e}")
            return None

    def get_health(self) -> dict:
        """Return broker health/throttle stats for the dashboard."""
        now = datetime.now()
        throttled = self._throttle_until and now < self._throttle_until
        return {
            'connected': self.connected,
            'account_type': self.acc_type,
            'account_id': self.account_info.get('account_id', ''),
            'throttled': throttled,
            'throttle_seconds_left': max(0, int((self._throttle_until - now).total_seconds())) if throttled else 0,
            'n_throttle_hits': self._n_throttle_hits,
            'n_403_responses': self._n_403_responses,
            'n_successful_fetches': self._n_successful_fetches,
            'n_empty_responses': self._n_empty_responses,
            'last_successful_epic': self._last_successful_epic,
            'last_successful_time': self._last_successful_time.isoformat() if self._last_successful_time else None,
            'seconds_since_last_success': int((now - self._last_successful_time).total_seconds()) if self._last_successful_time else None,
            'allowance_remaining': self._allowance_remaining,
            'allowance_reset': self._allowance_reset.isoformat() if self._allowance_reset else None,
        }

    # ─── Order placement (REAL) ─────────────────────────
    def submit_order(self, epic: str, direction: str, size: float,
                    order_type: str = 'MARKET', limit_level: float = None,
                    stop_level: float = None, currency_code: str = 'USD',
                    expiry: str = '-', force_open: bool = True) -> Optional[IGOrder]:
        """Submit a real OTC order to IG.

        Args:
            epic: instrument EPIC code
            direction: 'BUY' or 'SELL'
            size: number of units/lots
            order_type: 'MARKET' or 'LIMIT'
            limit_level: take profit price (optional)
            stop_level: stop loss price (optional)
            expiry: '-' for CFD accounts, 'DFB' for spread bet. Defaults to '-' (CFD).
        """
        if not self.connected or not self.ig_service:
            logger.error("IG: not connected")
            return None
        if size <= 0:
            return None
        try:
            # For CFD accounts, we need to use the v2 API directly with forceOpen=true
            # and NOT pass the expiry parameter (which is spread bet specific)
            # Detect account type
            account_type = self.account_info.get('account_type', 'CFD')
            if account_type == 'CFD':
                # Use v1 endpoint for CFD accounts (v2 requires expiry which is for spread bets)
                order_data = {
                    'epic': epic,
                    'direction': direction,
                    'size': size,
                    'orderType': order_type,
                    'expiry': '-',  # '-' for CFD
                    'forceOpen': True,
                    'guaranteedStop': False,
                }
                if limit_level:
                    order_data['limitLevel'] = limit_level
                if stop_level:
                    order_data['stopLevel'] = stop_level
                if currency_code:
                    order_data['currencyCode'] = currency_code

                # Get the session token from the IGService
                cst = self.ig_service.session.headers.get('CST', '')
                token = self.ig_service.session.headers.get('X-SECURITY-TOKEN', '')

                # The base URL is https://demo-api.ig.com/gateway/deal
                # The order endpoint is /positions/otc (relative to base)
                base_url = self._get_base_url()
                order_url = f'{base_url}/positions/otc'

                headers = {
                    'Content-Type': 'application/json; charset=UTF-8',
                    'Accept': 'application/json; charset=UTF-8',
                    'X-IG-API-KEY': self.api_key,
                    'CST': cst,
                    'X-SECURITY-TOKEN': token,
                    'Version': '1',  # v1 for CFD
                }

                import requests
                resp = requests.post(order_url, json=order_data, headers=headers)
                if resp.status_code != 200:
                    logger.error(f"IG order HTTP {resp.status_code}: {resp.text[:300]}")
                    return None
                result = resp.json()
            else:
                # Spread bet account
                result = self.ig_service.create_open_position(
                    currency_code=currency_code,
                    direction=direction,
                    epic=epic,
                    expiry='DFB',
                    force_open=True,
                    guaranteed_stop=False,
                    level=None,
                    limit_distance=None,
                    limit_level=limit_level,
                    order_type=order_type,
                    quote_id=None,
                    size=size,
                    stop_distance=None,
                    stop_level=stop_level,
                    trailing_stop=False,
                    trailing_stop_increment=0,
                )
            if result is None:
                logger.error(f"IG order failed: no result")
                return None
            deal_ref = result.get('dealReference') if isinstance(result, dict) else None
            if deal_ref:
                order = IGOrder(
                    deal_reference=deal_ref,
                    instrument=epic,
                    direction=direction,
                    n_units=size,
                    order_type=order_type,
                    status='PENDING',
                )
                self.orders.append(order)
                logger.info(f"[IG] Order submitted: {direction} {size} {epic} ref={deal_ref}")
                return order
            return None
        except Exception as e:
            logger.error(f"IG submit_order failed: {e}")
            return None

    def get_open_positions(self) -> List[Dict]:
        """Get real open positions from IG."""
        if not self.connected or not self.ig_service:
            return []
        try:
            resp = self.ig_service.fetch_open_positions()
            # resp is a DataFrame in modern trading_ig
            out = []
            if resp is None:
                return []
            # Normalize: could be DataFrame or dict
            if hasattr(resp, 'iterrows'):
                positions = resp.to_dict('records')
            elif isinstance(resp, dict):
                positions = resp.get('positions', [])
            else:
                positions = []
            for p in positions:
                pos = p.get('position', {}) if isinstance(p, dict) else {}
                market = p.get('market', {}) if isinstance(p, dict) else {}
                if not pos and 'epic' in p:
                    # Already flat
                    pos = p
                    market = p
                direction = pos.get('direction') or p.get('direction')
                size = float(pos.get('size', 0) or p.get('size', 0) or 0)
                level = float(pos.get('level', 0) or p.get('level', 0) or 0)
                bid = float(market.get('bid', 0) or p.get('bid', 0) or 0)
                offer = float(market.get('offer', 0) or p.get('offer', 0) or 0)
                # NOTE: IG's /positions endpoint does NOT return a live 'pnl'
                # field — it must be derived from the current bid/offer vs.
                # the position's open level, scaled by IG's own
                # 'scalingFactor' (the documented conversion from price
                # points to account-currency P&L for a given instrument).
                scaling_factor = float(market.get('scalingFactor', 1) or p.get('scalingFactor', 1) or 1)
                pnl = 0.0
                if bid and offer and level and size:
                    if direction == 'BUY':
                        pnl = (bid - level) * size * scaling_factor
                    elif direction == 'SELL':
                        pnl = (level - offer) * size * scaling_factor
                out.append({
                    'deal_id': pos.get('dealId') or pos.get('deal_id') or p.get('dealId') or p.get('deal_id'),
                    'epic': pos.get('epic') or p.get('epic'),
                    'instrument_name': market.get('instrumentName') or market.get('instrument_name') or p.get('instrumentName') or p.get('instrument_name'),
                    'direction': direction,
                    'size': size,
                    'level': level,
                    'currency': pos.get('currency') or p.get('currency'),
                    'current_bid': bid,
                    'current_offer': offer,
                    'pnl': round(pnl, 2),
                    'createdDate': pos.get('createdDate') or p.get('createdDate'),
                })
            self.open_positions = {p['epic']: p for p in out if p.get('epic')}
            return out
        except Exception as e:
            logger.error(f"IG get_open_positions failed: {e}")
            return []

    def close_position(self, deal_id: str, direction: str, epic: str,
                      expiry: str = '-', level: float = None, size: float = None) -> bool:
        """Close a real position on IG using the raw REST API.

        We bypass trading_ig.close_open_position because it always sends
        BOTH 'level' and 'size' to IG, which triggers the
        'mutual-exclusive-value.request' validation error.
        """
        if not self.connected or not self.ig_service:
            return False
        try:
            import requests
            # For CFD accounts, use '-' (not 'DFB' which is spread bet)
            account_type = self.account_info.get('account_type', 'CFD')
            if account_type == 'CFD' and expiry == 'DFB':
                expiry = '-'
            # Build params with ONLY ONE of level/size
            params = {
                'dealId': deal_id,
                'direction': direction,
                'epic': epic,
                'expiry': expiry,
                'orderType': 'MARKET',
                'currencyCode': 'USD',
                'forceOpen': False,  # closing = not force open
                'guaranteedStop': False,
            }
            # For CFD closes, we pass the full position size so IG knows
            # exactly how much to close. Don't pass 'level' (it would
            # be treated as a new position price).
            if size is not None and size > 0:
                params['size'] = float(size)
            cst = self.ig_service.session.headers.get('CST', '')
            token = self.ig_service.session.headers.get('X-SECURITY-TOKEN', '')
            headers = {
                'Content-Type': 'application/json; charset=UTF-8',
                'Accept': 'application/json; charset=UTF-8',
                'X-IG-API-KEY': self.api_key,
                'CST': cst,
                'X-SECURITY-TOKEN': token,
                'Version': '1',
            }
            url = f'{self._get_base_url()}/positions/otc'
            resp = requests.post(url, json=params, headers=headers)
            if resp.status_code != 200:
                logger.error(f"IG close HTTP {resp.status_code}: {resp.text[:300]}")
                return False
            result = resp.json()
            deal_ref = result.get('dealReference')
            if deal_ref:
                logger.info(f"[IG] Closed position: {epic} ref={deal_ref}")
                return True
            logger.warning(f"close_position: no dealReference: {result}")
            return False
        except Exception as e:
            logger.error(f"IG close_position failed: {e}")
            return False

    def close_all_positions(self) -> int:
        """Close all open positions."""
        if not self.connected:
            return 0
        positions = self.get_open_positions()
        n = 0
        for p in positions:
            opposite = 'SELL' if p['direction'] == 'BUY' else 'BUY'
            if self.close_position(deal_id=p['deal_id'], direction=opposite,
                                   epic=p['epic'], size=p['size']):
                n += 1
        return n

    def cancel_all_orders(self) -> int:
        """Cancel all working orders."""
        if not self.connected:
            return 0
        try:
            resp = self.ig_service.fetch_working_orders()
            orders = resp.get('workingOrders', [])
            n = 0
            for o in orders:
                deal_id = o.get('workingOrderData', {}).get('dealId')
                if deal_id:
                    try:
                        self.ig_service.delete_working_order(deal_id=deal_id)
                        n += 1
                    except Exception:
                        pass
            return n
        except Exception as e:
            logger.error(f"IG cancel_all_orders failed: {e}")
            return 0

    def poll_order(self, order: IGOrder, max_attempts: int = 3,
                   sleep_s: float = 0.5) -> IGOrder:
        """Poll IG's confirm endpoint to update an order's status.

        Updates order.status to one of: 'ACCEPTED', 'REJECTED', 'OPEN', 'FILLED',
        'DELETED', 'EXPIRED', 'UNKNOWN'.
        Populates order.deal_id and order.filled_price.
        """
        if not self.connected or not order.deal_reference:
            return order
        for _ in range(max_attempts):
            try:
                conf = self.ig_service.fetch_deal_by_deal_reference(order.deal_reference)
            except Exception as e:
                logger.debug(f"poll_order({order.deal_reference}) failed: {e}")
                conf = None
            if isinstance(conf, dict):
                status = conf.get('dealStatus') or conf.get('status') or 'UNKNOWN'
                order.status = status
                if conf.get('dealId'):
                    order.deal_id = conf.get('dealId')
                if conf.get('level') is not None:
                    try:
                        order.filled_price = float(conf.get('level'))
                        order.filled_at = datetime.now()
                    except (TypeError, ValueError):
                        pass
                if conf.get('reason'):
                    order.reason = conf.get('reason')
                if status in ('ACCEPTED', 'OPEN', 'FILLED', 'REJECTED', 'DELETED', 'EXPIRED'):
                    return order
            time.sleep(sleep_s)
        return order

    def get_working_orders(self) -> List[Dict]:
        """Get all open working orders from IG."""
        if not self.connected or not self.ig_service:
            return []
        try:
            resp = self.ig_service.fetch_working_orders()
            if hasattr(resp, 'to_dict'):
                rows = resp.to_dict('records')
                out = []
                for r in rows:
                    out.append({
                        'deal_id': r.get('dealId'),
                        'epic': r.get('epic'),
                        'direction': r.get('direction'),
                        'size': r.get('orderSize'),
                        'level': r.get('orderLevel'),
                        'order_type': r.get('orderType'),
                        'created_at': str(r.get('createdDate', '')),
                    })
                return out
            elif isinstance(resp, dict):
                return resp.get('workingOrders', [])
        except Exception as e:
            logger.error(f"IG get_working_orders failed: {e}")
        return []
