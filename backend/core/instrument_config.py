"""
V82.LOWDD — Instrument-Class Configuration.

Each instrument class (forex, crypto, indices, commodities, shares, options,
bonds) has very different volatility, liquidity, and trading hours. The TP/SL/RR
logic MUST be tuned per class. This module provides the per-class
configuration for forecast thresholds, position sizing, stop loss, take
profit, and market-hours filter.

Key insight: the same signal logic produces wildly different trade outcomes
across asset classes. Crypto can move 1% in 5 min; forex moves 0.05% in 5
min. We MUST adapt.
"""
import os
import re
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, Optional, Tuple, List


# ──────────────────────────────────────────────────────────
#  INSTRUMENT-CLASS DETECTION
# ──────────────────────────────────────────────────────────
def classify(epic: str) -> str:
    """Return the instrument class for an EPIC. One of:
    'forex_major', 'forex_minor', 'forex_exotic', 'crypto_cfd',
    'crypto_cfbmn', 'crypto_mini', 'index_us', 'index_eu', 'index_asia',
    'commodity_metal', 'commodity_energy', 'commodity_soft',
    'share_us_mega', 'share_us_tech', 'share_uk', 'share_eu',
    'option_us_idx', 'option_us_stock', 'option_eu_idx', 'option_fx',
    'option_commodity', 'bond'.
    """
    e = epic.upper()
    # Crypto: 24/7
    if 'CFBMU' in e:                       return 'crypto_cfbmn'   # 0.1 notional mini
    if 'BITCOIN' in e or 'ETHEREUM' in e or 'LITECOIN' in e or 'BITCOINCASH' in e \
       or 'XRP' in e.split('.') or 'CARDANO' in e or 'POLKADOT' in e \
       or 'SOLANA' in e or 'DOGECOIN' in e or 'CHAINLINK' in e \
       or 'AVALANCHE' in e or 'STELLAR' in e or 'TEZOS' in e or 'COSMOS' in e:
        return 'crypto_cfd'
    # Bonds
    if e.startswith('EB.D.'):              return 'bond'
    # Options
    if e.startswith('OP.D.'):
        if 'SPXW' in e or 'NDXW' in e or 'DJIW' in e or 'RUTW' in e: return 'option_us_idx'
        if any(s in e for s in ['AAPL', 'TSLA', 'NVDA', 'MSFT', 'AMZN', 'GOOGL']): return 'option_us_stock'
        if 'FTSE' in e or 'DAX' in e or 'EUSTX50' in e: return 'option_eu_idx'
        if 'EURUSD' in e or 'GBPUSD' in e or 'USDJPY' in e: return 'option_fx'
        if 'GOLD' in e or 'OIL' in e: return 'option_commodity'
        return 'option_us_idx'  # default
    # Indices
    if e.startswith('IX.D.'):
        if any(s in e for s in ['SPTRD', 'NASDAQ', 'DOW', 'RUS2000']): return 'index_us'
        if any(s in e for s in ['FTSE', 'DAX', 'CAC', 'EUSTX50', 'IBEX', 'FTSEM', 'SMI']): return 'index_eu'
        if any(s in e for s in ['NIKKEI', 'HSI', 'SSMCOMP', 'AUS200', 'TWSE', 'KOSPI', 'SENSEX']): return 'index_asia'
        return 'index_us'
    # Commodities
    if e.startswith('CC.D.'):
        # Energy: crude oil (CL.USS), Brent (B.USS), nat gas (NG), heating oil (HO), RBOB (RB)
        # Cotton is CT (soft), Coal is C.USC → energy
        if any(s in e for s in ['CL', '.B.', 'NG', 'HO', 'RB']):
            return 'commodity_energy'
        if 'C.USC.IP' in e:  # Coal only
            return 'commodity_energy'
        return 'commodity_soft'
    if 'IN_GOLD' in e or 'IN_SILVER' in e or 'COPPER' in e or 'PALL' in e \
       or 'PLAT' in e or 'IRON' in e:
        return 'commodity_metal'
    # Shares
    if e.startswith('CS.D.') and '.CFD.IP' in e:
        if any(s in e for s in ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META',
                                 'NVDA', 'TSLA', 'BRK', 'JPM', 'V', 'MA',
                                 'JNJ', 'WMT', 'PG', 'DIS', 'NFLX']):
            return 'share_us_mega'
        if any(s in e for s in ['AMD', 'INTC', 'CRM', 'ORCL', 'ADBE',
                                 'CSCO', 'PYPL', 'IBM']):
            return 'share_us_tech'
        if any(s in e for s in ['SHELL', 'AZN', 'HSBA', 'BARC', 'LLOY', 'BP']):
            return 'share_uk'
        if any(s in e for s in ['SAP', 'ALV', 'SIE', 'ASML', 'MC', 'OR']):
            return 'share_eu'
        return 'share_us_mega'
    # Forex
    if '.MINI.IP' in e:
        majors = ['EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD']
        exotics = ['USDMXN', 'USDZAR', 'USDTRY', 'USDPLN', 'USDSEK', 'USDNOK',
                   'USDDKK', 'USDHUF', 'USDCZK', 'EURTRY', 'EURPLN', 'EURSEK', 'EURNOK']
        for m in majors:
            if m in e: return 'forex_major'
        for ex in exotics:
            if ex in e: return 'forex_exotic'
        return 'forex_minor'
    return 'unknown'


# ──────────────────────────────────────────────────────────
#  PER-CLASS TRADING CONFIG
#  Each class has its own optimal TP/SL/RR/spread filter
# ──────────────────────────────────────────────────────────
# Required: every config MUST have these keys
#  - stop_atr_mult:  ATR multiple for stop-loss
#  - target_atr_mult: ATR multiple for take-profit
#  - min_atr_pct:  minimum ATR as % of price (otherwise too tight)
#  - max_spread_pct: maximum spread as % of price (else skip)
#  - min_bars_for_forecast: minimum bars to compute a forecast
#  - ret_3_threshold: minimum 3-bar return to trigger BULLISH/BEARISH
#  - streak_threshold: minimum body-streak to trigger
#  - max_hold_bars: maximum bars to hold a position
#  - poll_priority: 0-100 (higher = polled more often)
#  - session: '24/7' | 'forex' | 'us' | 'eu' | 'asia'
#  - market_hours_utc: (open_h, open_m, close_h, close_m, weekdays)
#  - tradeable_on_weekend: bool
#  - contract_size:  notional per unit
#  - min_size_increment: smallest unit (1.0 for shares, 0.1 for crypto minis)
#  - max_units_cap: hard cap on units (prevents runaway size)

INSTRUMENT_CONFIG: Dict[str, dict] = {
    # ── FOREX ──────────────────────────────────────────
    'forex_major': {
        'stop_atr_mult': 1.0, 'target_atr_mult': 2.0,  # 1:2 RR
        'min_atr_pct': 0.0005,                         # 0.05% of price
        'max_spread_pct': 0.0005,                      # 5 pips on EURUSD
        'min_bars_for_forecast': 5,
        'ret_3_threshold': 0.0001,                       # 1 pip over 3 bars
        'streak_threshold': 1,
        'max_hold_bars': 12,                             # 60 min on 5-min bars
        'poll_priority': 90,
        'session': 'forex',
        'market_hours_utc': (22, 0, 22, 0, (0, 1, 2, 3, 4)),  # Sun 22:00 → Fri 22:00 UTC
        'tradeable_on_weekend': False,
        'contract_size': 1000.0,                         # 1 MINI lot = 1000 base
        'min_size_increment': 0.5,                       # 0.5 MINI lot
        'max_units_cap': 50,                             # never > 50 MINI lots
    },
    'forex_minor': {
        'stop_atr_mult': 1.2, 'target_atr_mult': 2.4,
        'min_atr_pct': 0.0008,
        'max_spread_pct': 0.001,                         # 10 pips
        'min_bars_for_forecast': 5,
        'ret_3_threshold': 0.0002,
        'streak_threshold': 1,
        'max_hold_bars': 12,
        'poll_priority': 70,
        'session': 'forex',
        'market_hours_utc': (22, 0, 22, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1000.0,
        'min_size_increment': 0.5,
        'max_units_cap': 50,
    },
    'forex_exotic': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,    # exotics have wider stops
        'min_atr_pct': 0.0015,
        'max_spread_pct': 0.003,                          # exotics have wider spreads
        'min_bars_for_forecast': 6,                       # need more bars for noisy exotics
        'ret_3_threshold': 0.0005,
        'streak_threshold': 2,                            # need stronger signal
        'max_hold_bars': 10,
        'poll_priority': 50,
        'session': 'forex',
        'market_hours_utc': (22, 0, 22, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1000.0,
        'min_size_increment': 0.5,
        'max_units_cap': 30,
    },
    # ── CRYPTO (24/7) ──────────────────────────────────
    'crypto_cfd': {
        'stop_atr_mult': 2.0, 'target_atr_mult': 4.0,    # crypto volatile, wider stops
        'min_atr_pct': 0.003,                            # 0.3% of price
        'max_spread_pct': 0.005,                         # crypto spreads ~ 0.1-0.5%
        'min_bars_for_forecast': 5,
        'ret_3_threshold': 0.001,                        # 0.1% over 3 bars
        'streak_threshold': 2,                            # crypto noisy, need 2-bar streak
        'max_hold_bars': 8,                              # 40 min on 5-min bars
        'poll_priority': 100,                            # ALWAYS FIRST
        'session': '24/7',
        'market_hours_utc': None,
        'tradeable_on_weekend': True,
        'contract_size': 1.0,                              # 1 unit = 1 BTC
        'min_size_increment': 0.1,                         # 0.1 BTC mini lots available
        'max_units_cap': 5,                                # never > 5 BTC
    },
    'crypto_cfbmn': {
        'stop_atr_mult': 1.8, 'target_atr_mult': 3.6,    # smaller mini, slightly tighter
        'min_atr_pct': 0.003,
        'max_spread_pct': 0.005,
        'min_bars_for_forecast': 5,
        'ret_3_threshold': 0.001,
        'streak_threshold': 2,
        'max_hold_bars': 8,
        'poll_priority': 100,
        'session': '24/7',
        'market_hours_utc': None,
        'tradeable_on_weekend': True,
        'contract_size': 0.1,                              # 1 unit = 0.1 BTC
        'min_size_increment': 0.1,
        'max_units_cap': 50,                               # 50 mini = 5 BTC equivalent
    },
    # ── INDICES ────────────────────────────────────────
    'index_us': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,    # indices trend
        'min_atr_pct': 0.001,                            # 0.1% per bar
        'max_spread_pct': 0.0008,                         # 0.08% for SPX
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0003,
        'streak_threshold': 2,
        'max_hold_bars': 6,                              # indices move fast
        'poll_priority': 70,
        'session': 'us',                                 # US market hours
        'market_hours_utc': (14, 30, 21, 0, (0, 1, 2, 3, 4)),  # 14:30-21:00 UTC, weekdays
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
    'index_eu': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,
        'min_atr_pct': 0.0008,
        'max_spread_pct': 0.001,
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0003,
        'streak_threshold': 2,
        'max_hold_bars': 6,
        'poll_priority': 60,
        'session': 'eu',
        'market_hours_utc': (8, 0, 16, 30, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
    'index_asia': {
        'stop_atr_mult': 1.8, 'target_atr_mult': 3.6,    # Asian markets more volatile
        'min_atr_pct': 0.001,
        'max_spread_pct': 0.0015,
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0004,
        'streak_threshold': 2,
        'max_hold_bars': 5,
        'poll_priority': 50,
        'session': 'asia',
        'market_hours_utc': (0, 0, 7, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
    # ── COMMODITIES ────────────────────────────────────
    'commodity_metal': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,
        'min_atr_pct': 0.0015,                           # gold ~ $30/day on $2000 = 1.5%
        'max_spread_pct': 0.002,                          # gold spread ~ 0.1-0.3%
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0003,
        'streak_threshold': 2,
        'max_hold_bars': 8,
        'poll_priority': 60,
        'session': 'metals',                              # ~22h/day
        'market_hours_utc': (0, 0, 22, 0, (0, 1, 2, 3, 4)),  # 1h break
        'tradeable_on_weekend': True,                       # gold/silver close Sat/Sun 22-23
        'contract_size': 1.0,
        'min_size_increment': 0.1,
        'max_units_cap': 20,
    },
    'commodity_energy': {
        'stop_atr_mult': 1.8, 'target_atr_mult': 3.6,    # oil is volatile
        'min_atr_pct': 0.003,                            # oil ~ $1/day on $80 = 1.25%
        'max_spread_pct': 0.003,
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0005,
        'streak_threshold': 2,
        'max_hold_bars': 6,
        'poll_priority': 60,
        'session': 'us',                                 # oil is US-driven
        'market_hours_utc': (0, 0, 22, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': True,
        'contract_size': 1.0,
        'min_size_increment': 0.1,
        'max_units_cap': 20,
    },
    'commodity_soft': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,
        'min_atr_pct': 0.002,
        'max_spread_pct': 0.005,
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0005,
        'streak_threshold': 2,
        'max_hold_bars': 6,
        'poll_priority': 50,
        'session': 'us',
        'market_hours_utc': (0, 0, 22, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': True,
        'contract_size': 1.0,
        'min_size_increment': 0.1,
        'max_units_cap': 20,
    },
    # ── SHARES ────────────────────────────────────────
    'share_us_mega': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,    # big caps trend well
        'min_atr_pct': 0.002,                            # 0.2% per bar
        'max_spread_pct': 0.001,                          # 0.1% spread typical
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0005,
        'streak_threshold': 2,
        'max_hold_bars': 8,
        'poll_priority': 80,
        'session': 'us',
        'market_hours_utc': (14, 30, 21, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 100,
    },
    'share_us_tech': {
        'stop_atr_mult': 1.8, 'target_atr_mult': 3.6,    # tech is volatile
        'min_atr_pct': 0.003,
        'max_spread_pct': 0.0015,
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0007,
        'streak_threshold': 2,
        'max_hold_bars': 6,
        'poll_priority': 70,
        'session': 'us',
        'market_hours_utc': (14, 30, 21, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 100,
    },
    'share_uk': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,
        'min_atr_pct': 0.002,
        'max_spread_pct': 0.0015,
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0005,
        'streak_threshold': 2,
        'max_hold_bars': 8,
        'poll_priority': 50,
        'session': 'eu',                                 # UK = EU session
        'market_hours_utc': (8, 0, 16, 30, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 100,
    },
    'share_eu': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,
        'min_atr_pct': 0.002,
        'max_spread_pct': 0.0015,
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.0005,
        'streak_threshold': 2,
        'max_hold_bars': 8,
        'poll_priority': 50,
        'session': 'eu',
        'market_hours_utc': (8, 0, 16, 30, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 100,
    },
    # ── OPTIONS (high volatility, very wide stops) ────
    'option_us_idx': {
        'stop_atr_mult': 3.0, 'target_atr_mult': 6.0,    # options are very volatile
        'min_atr_pct': 0.01,                             # 1% per bar
        'max_spread_pct': 0.02,                           # options have wide spreads
        'min_bars_for_forecast': 8,                       # need more bars
        'ret_3_threshold': 0.002,
        'streak_threshold': 3,
        'max_hold_bars': 4,                              # exit options fast
        'poll_priority': 40,
        'session': 'us',
        'market_hours_utc': (14, 30, 21, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
    'option_us_stock': {
        'stop_atr_mult': 3.0, 'target_atr_mult': 6.0,
        'min_atr_pct': 0.01,
        'max_spread_pct': 0.025,
        'min_bars_for_forecast': 8,
        'ret_3_threshold': 0.002,
        'streak_threshold': 3,
        'max_hold_bars': 4,
        'poll_priority': 40,
        'session': 'us',
        'market_hours_utc': (14, 30, 21, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
    'option_eu_idx': {
        'stop_atr_mult': 3.0, 'target_atr_mult': 6.0,
        'min_atr_pct': 0.01,
        'max_spread_pct': 0.02,
        'min_bars_for_forecast': 8,
        'ret_3_threshold': 0.002,
        'streak_threshold': 3,
        'max_hold_bars': 4,
        'poll_priority': 30,
        'session': 'eu',
        'market_hours_utc': (8, 0, 16, 30, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
    'option_fx': {
        'stop_atr_mult': 2.5, 'target_atr_mult': 5.0,    # FX options less volatile
        'min_atr_pct': 0.005,
        'max_spread_pct': 0.01,
        'min_bars_for_forecast': 6,
        'ret_3_threshold': 0.001,
        'streak_threshold': 2,
        'max_hold_bars': 6,
        'poll_priority': 30,
        'session': 'forex',
        'market_hours_utc': (0, 0, 22, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
    'option_commodity': {
        'stop_atr_mult': 3.0, 'target_atr_mult': 6.0,
        'min_atr_pct': 0.01,
        'max_spread_pct': 0.02,
        'min_bars_for_forecast': 8,
        'ret_3_threshold': 0.002,
        'streak_threshold': 3,
        'max_hold_bars': 4,
        'poll_priority': 30,
        'session': 'metals',
        'market_hours_utc': (0, 0, 22, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': True,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
    # ── BONDS (very low volatility) ──────────────────
    'bond': {
        'stop_atr_mult': 1.0, 'target_atr_mult': 1.5,    # bonds barely move
        'min_atr_pct': 0.0005,                           # ~ 1 bp/day
        'max_spread_pct': 0.005,                          # bond spreads wide
        'min_bars_for_forecast': 8,
        'ret_3_threshold': 0.0001,
        'streak_threshold': 2,
        'max_hold_bars': 10,
        'poll_priority': 30,
        'session': 'us',
        'market_hours_utc': (0, 0, 22, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': True,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 50,
    },
    # ── UNKNOWN (fallback) ────────────────────────────
    'unknown': {
        'stop_atr_mult': 1.5, 'target_atr_mult': 3.0,
        'min_atr_pct': 0.001,
        'max_spread_pct': 0.005,
        'min_bars_for_forecast': 8,
        'ret_3_threshold': 0.0005,
        'streak_threshold': 2,
        'max_hold_bars': 8,
        'poll_priority': 50,
        'session': 'us',
        'market_hours_utc': (14, 30, 21, 0, (0, 1, 2, 3, 4)),
        'tradeable_on_weekend': False,
        'contract_size': 1.0,
        'min_size_increment': 1.0,
        'max_units_cap': 10,
    },
}


def get_config(epic: str) -> dict:
    """Get the trading config for an EPIC."""
    cls = classify(epic)
    cfg = INSTRUMENT_CONFIG.get(cls, INSTRUMENT_CONFIG['unknown']).copy()
    cfg['_class'] = cls
    return cfg


# ──────────────────────────────────────────────────────────
#  MARKET HOURS CHECK
# ──────────────────────────────────────────────────────────
def is_market_open(epic: str, now: Optional[datetime] = None) -> bool:
    """Return True if the market for this EPIC is currently open."""
    cfg = get_config(epic)
    if cfg.get('session') == '24/7':
        return True
    hours = cfg.get('market_hours_utc')
    if hours is None:
        return True
    if now is None:
        now = datetime.utcnow()
    open_h, open_m, close_h, close_m, weekdays = hours
    if now.weekday() not in weekdays:
        return False
    open_t = dt_time(open_h, open_m)
    close_t = dt_time(close_h, close_m)
    cur_t = now.time()
    if close_t > open_t:
        return open_t <= cur_t < close_t
    # Session crosses midnight (e.g. 22:00-22:00 next day)
    return cur_t >= open_t or cur_t < close_t


def should_skip_for_session(epic: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Return (skip, reason) for session filter."""
    if not is_market_open(epic, now):
        cfg = get_config(epic)
        return True, f"market closed (session: {cfg.get('session')})"
    return False, ''


# ──────────────────────────────────────────────────────────
#  SPREAD / VOLATILITY GATE
# ──────────────────────────────────────────────────────────
def spread_ok(epic: str, bid: float, offer: float) -> Tuple[bool, str]:
    """Check whether the current spread is acceptable for this class."""
    cfg = get_config(epic)
    if bid <= 0 or offer <= 0:
        return False, "no quote"
    spread = offer - bid
    spread_pct = spread / max(bid, 1e-9)
    max_pct = cfg.get('max_spread_pct', 0.005)
    if spread_pct > max_pct:
        return False, f"spread too wide ({spread_pct*100:.3f}% > {max_pct*100:.3f}%)"
    return True, ''


def atr_ok(epic: str, atr: float, price: float) -> Tuple[bool, str]:
    """Check whether the current ATR is large enough to be tradeable."""
    cfg = get_config(epic)
    if price <= 0:
        return False, "no price"
    atr_pct = atr / price
    min_pct = cfg.get('min_atr_pct', 0.001)
    if atr_pct < min_pct:
        return False, f"ATR too small ({atr_pct*100:.3f}% < {min_pct*100:.3f}%)"
    return True, ''


# ──────────────────────────────────────────────────────────
#  FORECAST WITH PER-CLASS THRESHOLDS
# ──────────────────────────────────────────────────────────
def compute_forecast_for(epic: str, df) -> Optional[dict]:
    """Compute forecast for an EPIC using class-specific thresholds.

    df: pandas DataFrame with columns [open, high, low, close, volume]
    indexed by timestamp.
    """
    from backend.core.live_mode import _next_id
    cfg = get_config(epic)
    cls = cfg['_class']
    n = len(df)
    min_bars = cfg.get('min_bars_for_forecast', 5)
    if n < min_bars:
        return None
    import pandas as pd
    # Adaptive MA windows
    ma_fast = max(2, min(20, n // 2))
    ma_slow = max(3, min(50, n - 1))
    df = df.copy()
    df['ma_fast'] = df['close'].rolling(ma_fast, min_periods=1).mean()
    df['ma_slow'] = df['close'].rolling(ma_slow, min_periods=1).mean()
    df['range'] = df['high'] - df['low']
    atr_n = max(2, min(20, n // 2))
    df['atr'] = df['range'].rolling(atr_n, min_periods=1).mean()
    df['ret'] = df['close'].pct_change()
    df['ret_3'] = df['ret'].rolling(3, min_periods=1).sum()
    df['body_pos'] = (df['close'] > df['open']).astype(int)
    df['streak'] = df['body_pos'].rolling(3, min_periods=1).sum()
    last = df.iloc[-1]
    ret_3_thr = cfg.get('ret_3_threshold', 0.0001)
    streak_thr = cfg.get('streak_threshold', 1)
    trend_up = (last['close'] > last['ma_fast']) and (last['ma_fast'] >= last['ma_slow'])
    trend_down = (last['close'] < last['ma_fast']) and (last['ma_fast'] <= last['ma_slow'])
    streak = int(last['streak'])
    if trend_up and streak >= streak_thr and last['ret_3'] > ret_3_thr:
        direction = 'BULLISH'
    elif trend_down and streak <= (3 - streak_thr) and last['ret_3'] < -ret_3_thr:
        direction = 'BEARISH'
    else:
        direction = 'NEUTRAL'
    return {
        'symbol': epic,
        'class': cls,
        'direction': direction,
        'close': float(last['close']),
        'ma_fast': float(last['ma_fast']),
        'ma_slow': float(last['ma_slow']),
        'atr': float(last['atr']),
        'ret_3': float(last['ret_3']),
        'streak': streak,
        'n_bars': n,
        'time': str(df.index[-1]),
    }
