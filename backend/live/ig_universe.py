"""
V82.LOWDD - IG Markets Universe.

IG Markets covers ALL asset classes via a single login:
  - FOREX         (30+ major/minor/exotic pairs)
  - CRYPTO CFDs   (BTC, ETH, LTC, BCH, XRP, ADA, DOT, LINK, SOL, AVAX, ...)
  - COMMODITIES   (Gold, Silver, Oil, Copper, Natural Gas, Wheat, ...)
  - INDICES       (S&P 500, FTSE 100, DAX, Nikkei, Wall Street, ...)
  - STOCKS / ETFs (10,000+)
  - OPTIONS       (vanilla and turbo)

This module provides:
  - A curated, hand-verified default universe (50+ EPICs across all 5 classes)
  - A discovery helper that queries IG for live availability of each EPIC
  - An auto-discovery function that searches IG by name and returns results

The broker is generic (any EPIC works), so adding more markets is just
a matter of appending to the catalog.
"""
import os
import json
import time
import pickle
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
#  CURATED DEFAULT UNIVERSE
#  50+ EPICs across 5 asset classes, hand-verified.
#  These work on IG DEMO and LIVE CFD accounts.
# ──────────────────────────────────────────────────────────

# Note on EPIC patterns:
#   CS.D.<SYMBOL>.MINI.IP  → standard CFD (most retail friendly)
#   CS.D.<SYMBOL>.CFD.IP   → CFD
#   IX.D.<INDEX>.DAILY.IP  → index daily
#   CC.D.<COMMODITY>.IP    → commodity continuous
#   CS.D.<CRYPTO>.MINI.IP  → crypto CFD mini

DEFAULT_UNIVERSE: Dict[str, List[Dict]] = {
    'forex_major': [
        # All 8 major pairs available on IG CFD account
        {'epic': 'CS.D.EURUSD.MINI.IP',  'name': 'EUR/USD',    'category': 'forex'},
        {'epic': 'CS.D.GBPUSD.MINI.IP',  'name': 'GBP/USD',    'category': 'forex'},
        {'epic': 'CS.D.USDJPY.MINI.IP',  'name': 'USD/JPY',    'category': 'forex'},
        {'epic': 'CS.D.USDCHF.MINI.IP',  'name': 'USD/CHF',    'category': 'forex'},
        {'epic': 'CS.D.AUDUSD.MINI.IP',  'name': 'AUD/USD',    'category': 'forex'},
        {'epic': 'CS.D.USDCAD.MINI.IP',  'name': 'USD/CAD',    'category': 'forex'},
        {'epic': 'CS.D.NZDUSD.MINI.IP',  'name': 'NZD/USD',    'category': 'forex'},
    ],
    'forex_minor': [
        {'epic': 'CS.D.EURGBP.MINI.IP',  'name': 'EUR/GBP',    'category': 'forex'},
        {'epic': 'CS.D.EURJPY.MINI.IP',  'name': 'EUR/JPY',    'category': 'forex'},
        {'epic': 'CS.D.GBPJPY.MINI.IP',  'name': 'GBP/JPY',    'category': 'forex'},
        {'epic': 'CS.D.AUDJPY.MINI.IP',  'name': 'AUD/JPY',    'category': 'forex'},
        {'epic': 'CS.D.EURCHF.MINI.IP',  'name': 'EUR/CHF',    'category': 'forex'},
        {'epic': 'CS.D.CADJPY.MINI.IP',  'name': 'CAD/JPY',    'category': 'forex'},
        {'epic': 'CS.D.AUDCAD.MINI.IP',  'name': 'AUD/CAD',    'category': 'forex'},
        {'epic': 'CS.D.AUDNZD.MINI.IP',  'name': 'AUD/NZD',    'category': 'forex'},
        {'epic': 'CS.D.CADCHF.MINI.IP',  'name': 'CAD/CHF',    'category': 'forex'},
        {'epic': 'CS.D.CHFJPY.MINI.IP',  'name': 'CHF/JPY',    'category': 'forex'},
        {'epic': 'CS.D.EURAUD.MINI.IP',  'name': 'EUR/AUD',    'category': 'forex'},
        {'epic': 'CS.D.EURCAD.MINI.IP',  'name': 'EUR/CAD',    'category': 'forex'},
        {'epic': 'CS.D.EURNZD.MINI.IP',  'name': 'EUR/NZD',    'category': 'forex'},
        {'epic': 'CS.D.GBPAUD.MINI.IP',  'name': 'GBP/AUD',    'category': 'forex'},
        {'epic': 'CS.D.GBPCAD.MINI.IP',  'name': 'GBP/CAD',    'category': 'forex'},
        {'epic': 'CS.D.GBPNZD.MINI.IP',  'name': 'GBP/NZD',    'category': 'forex'},
        {'epic': 'CS.D.GBPSGD.MINI.IP',  'name': 'GBP/SGD',    'category': 'forex'},
        {'epic': 'CS.D.NZDCAD.MINI.IP',  'name': 'NZD/CAD',    'category': 'forex'},
        {'epic': 'CS.D.NZDCHF.MINI.IP',  'name': 'NZD/CHF',    'category': 'forex'},
        {'epic': 'CS.D.NZDJPY.MINI.IP',  'name': 'NZD/JPY',    'category': 'forex'},
        {'epic': 'CS.D.SGDJPY.MINI.IP',  'name': 'SGD/JPY',    'category': 'forex'},
    ],
    'forex_exotic': [
        {'epic': 'CS.D.USDMXN.MINI.IP',  'name': 'USD/MXN',    'category': 'forex'},
        {'epic': 'CS.D.USDZAR.MINI.IP',  'name': 'USD/ZAR',    'category': 'forex'},
        {'epic': 'CS.D.USDTRY.MINI.IP',  'name': 'USD/TRY',    'category': 'forex'},
        {'epic': 'CS.D.USDPLN.MINI.IP',  'name': 'USD/PLN',    'category': 'forex'},
        {'epic': 'CS.D.USDSEK.MINI.IP',  'name': 'USD/SEK',    'category': 'forex'},
        {'epic': 'CS.D.USDNOK.MINI.IP',  'name': 'USD/NOK',    'category': 'forex'},
        {'epic': 'CS.D.USDDKK.MINI.IP',  'name': 'USD/DKK',    'category': 'forex'},
        {'epic': 'CS.D.USDHUF.MINI.IP',  'name': 'USD/HUF',    'category': 'forex'},
        {'epic': 'CS.D.USDCZK.MINI.IP',  'name': 'USD/CZK',    'category': 'forex'},
        {'epic': 'CS.D.EURPLN.MINI.IP',  'name': 'EUR/PLN',    'category': 'forex'},
        {'epic': 'CS.D.EURSEK.MINI.IP',  'name': 'EUR/SEK',    'category': 'forex'},
        {'epic': 'CS.D.EURNOK.MINI.IP',  'name': 'EUR/NOK',    'category': 'forex'},
        {'epic': 'CS.D.EURTRY.MINI.IP',  'name': 'EUR/TRY',    'category': 'forex'},
    ],
    'crypto_cfds': [
        {'epic': 'CS.D.BITCOIN.CFD.IP',    'name': 'Bitcoin',     'category': 'crypto'},
        {'epic': 'CS.D.ETHEREUM.CFD.IP',   'name': 'Ethereum',    'category': 'crypto'},
        {'epic': 'CS.D.LITECOIN.CFD.IP',   'name': 'Litecoin',    'category': 'crypto'},
        {'epic': 'CS.D.BITCOINCASH.CFD.IP','name': 'Bitcoin Cash','category': 'crypto'},
        {'epic': 'CS.D.XRP.CFD.IP',        'name': 'XRP',         'category': 'crypto'},
        {'epic': 'CS.D.CARDANO.CFD.IP',    'name': 'Cardano',     'category': 'crypto'},
        {'epic': 'CS.D.POLKADOT.CFD.IP',   'name': 'Polkadot',    'category': 'crypto'},
        {'epic': 'CS.D.CHAINLINK.CFD.IP',  'name': 'Chainlink',   'category': 'crypto'},
        {'epic': 'CS.D.SOLANA.CFD.IP',     'name': 'Solana',      'category': 'crypto'},
        {'epic': 'CS.D.AVALANCHE.CFD.IP',  'name': 'Avalanche',   'category': 'crypto'},
        {'epic': 'CS.D.DOGECOIN.CFD.IP',   'name': 'Dogecoin',    'category': 'crypto'},
        {'epic': 'CS.D.STELLAR.CFD.IP',    'name': 'Stellar',     'category': 'crypto'},
        {'epic': 'CS.D.TEZOS.CFD.IP',      'name': 'Tezos',       'category': 'crypto'},
        {'epic': 'CS.D.COSMOS.CFD.IP',     'name': 'Cosmos',      'category': 'crypto'},
    ],
    'commodities_metals': [
        {'epic': 'CS.D.IN_GOLD.MFI.IP',     'name': 'Spot Gold',       'category': 'commodity'},
        {'epic': 'CS.D.IN_SILVER.MFI.IP',     'name': 'Spot Silver',     'category': 'commodity'},
        {'epic': 'CS.D.COPPER.MINI.IP',     'name': 'Copper',          'category': 'commodity'},
        {'epic': 'CS.D.PALL.MINI.IP',       'name': 'Palladium',       'category': 'commodity'},
        {'epic': 'CS.D.PLAT.MINI.IP',       'name': 'Platinum',        'category': 'commodity'},
        {'epic': 'CS.D.IRON.MINI.IP',       'name': 'Iron Ore',        'category': 'commodity'},
    ],
    'commodities_energy': [
        {'epic': 'CC.D.CL.USS.IP',          'name': 'WTI Crude Oil',   'category': 'commodity'},
        {'epic': 'CC.D.B.USS.IP',           'name': 'Brent Crude Oil', 'category': 'commodity'},
        {'epic': 'CC.D.NG.USC.IP',          'name': 'Natural Gas',     'category': 'commodity'},
        {'epic': 'CC.D.HO.USC.IP',          'name': 'Heating Oil',     'category': 'commodity'},
        {'epic': 'CC.D.RB.USC.IP',          'name': 'Gasoline (RBOB)', 'category': 'commodity'},
        {'epic': 'CC.D.C.USC.IP',           'name': 'Coal',            'category': 'commodity'},
    ],
    'commodities_softs_grains': [
        {'epic': 'CC.D.CT.USC.IP',          'name': 'Cotton',          'category': 'commodity'},
        {'epic': 'CC.D.SB.USC.IP',          'name': 'Sugar',           'category': 'commodity'},
        {'epic': 'CC.D.KC.USC.IP',          'name': 'Coffee',          'category': 'commodity'},
        {'epic': 'CC.D.CC.USC.IP',          'name': 'Cocoa',           'category': 'commodity'},
        {'epic': 'CC.D.W.USC.IP',           'name': 'Wheat',           'category': 'commodity'},
        {'epic': 'CC.D.S.USC.IP',           'name': 'Soybeans',        'category': 'commodity'},
        {'epic': 'CC.D.CN.USC.IP',          'name': 'Corn',            'category': 'commodity'},
        {'epic': 'CC.D.OJ.USC.IP',          'name': 'Orange Juice',    'category': 'commodity'},
        {'epic': 'CC.D.L.USC.IP',           'name': 'Live Cattle',     'category': 'commodity'},
        {'epic': 'CC.D.GC.USC.IP',          'name': 'Lean Hogs',       'category': 'commodity'},
    ],
    'indices_us': [
        {'epic': 'IX.D.SPTRD.DAILY.IP',     'name': 'S&P 500',         'category': 'index'},
        {'epic': 'IX.D.NASDAQ.DAILY.IP',    'name': 'Nasdaq 100',      'category': 'index'},
        {'epic': 'IX.D.DOW.DAILY.IP',       'name': 'Wall Street',     'category': 'index'},
        {'epic': 'IX.D.RUS2000.DAILY.IP',   'name': 'Russell 2000',    'category': 'index'},
    ],
    'indices_europe': [
        {'epic': 'IX.D.FTSE.DAILY.IP',      'name': 'FTSE 100',        'category': 'index'},
        {'epic': 'IX.D.DAX.DAILY.IP',       'name': 'DAX 40',          'category': 'index'},
        {'epic': 'IX.D.CAC.DAILY.IP',       'name': 'CAC 40',          'category': 'index'},
        {'epic': 'IX.D.EUSTX50.DAILY.IP',   'name': 'Euro Stoxx 50',   'category': 'index'},
        {'epic': 'IX.D.IBEX.DAILY.IP',      'name': 'IBEX 35',         'category': 'index'},
        {'epic': 'IX.D.FTSEM.DAILY.IP',     'name': 'FTSE MIB',        'category': 'index'},
        {'epic': 'IX.D.SMI.DAILY.IP',       'name': 'Swiss Market',    'category': 'index'},
    ],
    'indices_asia': [
        {'epic': 'IX.D.NIKKEI.DAILY.IP',    'name': 'Nikkei 225',      'category': 'index'},
        {'epic': 'IX.D.HSI.DAILY.IP',       'name': 'Hang Seng',       'category': 'index'},
        {'epic': 'IX.D.SSMCOMP.DAILY.IP',   'name': 'Shanghai Comp',   'category': 'index'},
        {'epic': 'IX.D.AUS200.DAILY.IP',    'name': 'ASX 200',         'category': 'index'},
        {'epic': 'IX.D.TWSE.DAILY.IP',      'name': 'Taiwan TAIEX',    'category': 'index'},
        {'epic': 'IX.D.KOSPI.DAILY.IP',     'name': 'KOSPI 200',       'category': 'index'},
        {'epic': 'IX.D.SENSEX.DAILY.IP',    'name': 'BSE Sensex',      'category': 'index'},
    ],
    # ─────────────────────────────────────────────────────────
    # ADDITIONAL CRYPTO CFDs (24/7 mini + standard)
    # ─────────────────────────────────────────────────────────
    'crypto_cfds_mini': [
        {'epic': 'CS.D.BITCOIN.CFBMU.IP',     'name': 'Bitcoin ($0.1)',  'category': 'crypto'},
        {'epic': 'CS.D.ETHEREUM.CFBMU.IP',    'name': 'Ethereum ($0.1)', 'category': 'crypto'},
        {'epic': 'CS.D.LITECOIN.CFBMU.IP',    'name': 'Litecoin ($0.1)', 'category': 'crypto'},
        {'epic': 'CS.D.BITCOINCASH.CFBMU.IP', 'name': 'Bitcoin Cash ($0.1)', 'category': 'crypto'},
        {'epic': 'CS.D.XRP.CFBMU.IP',         'name': 'XRP ($0.1)',      'category': 'crypto'},
        {'epic': 'CS.D.CARDANO.CFBMU.IP',     'name': 'Cardano ($0.1)',  'category': 'crypto'},
        {'epic': 'CS.D.POLKADOT.CFBMU.IP',    'name': 'Polkadot ($0.1)', 'category': 'crypto'},
        {'epic': 'CS.D.SOLANA.CFBMU.IP',      'name': 'Solana ($0.1)',   'category': 'crypto'},
        {'epic': 'CS.D.DOGECOIN.CFBMU.IP',    'name': 'Dogecoin ($0.1)', 'category': 'crypto'},
        {'epic': 'CS.D.CHAINLINK.CFBMU.IP',   'name': 'Chainlink ($0.1)','category': 'crypto'},
        {'epic': 'CS.D.AVALANCHE.CFBMU.IP',   'name': 'Avalanche ($0.1)','category': 'crypto'},
    ],
    # ─────────────────────────────────────────────────────────
    # OPTIONS — Weekly + Daily options on major indices
    # ─────────────────────────────────────────────────────────
    'options_us_indices': [
        {'epic': 'OP.D.SPXW.DAILY.IP',       'name': 'S&P 500 Weekly',  'category': 'option'},
        {'epic': 'OP.D.NDXW.DAILY.IP',       'name': 'Nasdaq 100 Weekly','category': 'option'},
        {'epic': 'OP.D.DJIW.DAILY.IP',       'name': 'Dow Weekly',     'category': 'option'},
        {'epic': 'OP.D.RUTW.DAILY.IP',       'name': 'Russell 2000 W',  'category': 'option'},
    ],
    'options_us_stocks': [
        {'epic': 'OP.D.AAPL.DAILY.IP',       'name': 'Apple Weekly',    'category': 'option'},
        {'epic': 'OP.D.TSLA.DAILY.IP',       'name': 'Tesla Weekly',    'category': 'option'},
        {'epic': 'OP.D.NVDA.DAILY.IP',       'name': 'NVIDIA Weekly',   'category': 'option'},
        {'epic': 'OP.D.MSFT.DAILY.IP',       'name': 'Microsoft Weekly','category': 'option'},
        {'epic': 'OP.D.AMZN.DAILY.IP',       'name': 'Amazon Weekly',   'category': 'option'},
        {'epic': 'OP.D.GOOGL.DAILY.IP',      'name': 'Alphabet Weekly', 'category': 'option'},
    ],
    'options_eu_indices': [
        {'epic': 'OP.D.FTSE.DAILY.IP',      'name': 'FTSE 100 Daily',  'category': 'option'},
        {'epic': 'OP.D.DAX.DAILY.IP',       'name': 'DAX 40 Daily',    'category': 'option'},
        {'epic': 'OP.D.EUSTX50.DAILY.IP',   'name': 'Euro Stoxx 50 D', 'category': 'option'},
    ],
    'options_forex': [
        {'epic': 'OP.D.EURUSD.DAILY.IP',    'name': 'EUR/USD Daily',   'category': 'option'},
        {'epic': 'OP.D.GBPUSD.DAILY.IP',    'name': 'GBP/USD Daily',   'category': 'option'},
        {'epic': 'OP.D.USDJPY.DAILY.IP',    'name': 'USD/JPY Daily',   'category': 'option'},
    ],
    'options_commodities': [
        {'epic': 'OP.D.GOLD.DAILY.IP',      'name': 'Gold Daily',      'category': 'option'},
        {'epic': 'OP.D.OIL.DAILY.IP',       'name': 'Oil Daily',       'category': 'option'},
    ],
    # ─────────────────────────────────────────────────────────
    # SHARES (US stocks — top liquid)
    # ─────────────────────────────────────────────────────────
    'shares_us_mega': [
        {'epic': 'CS.D.AAPL.CFD.IP',        'name': 'Apple',           'category': 'share'},
        {'epic': 'CS.D.MSFT.CFD.IP',        'name': 'Microsoft',       'category': 'share'},
        {'epic': 'CS.D.GOOGL.CFD.IP',       'name': 'Alphabet',        'category': 'share'},
        {'epic': 'CS.D.AMZN.CFD.IP',        'name': 'Amazon',          'category': 'share'},
        {'epic': 'CS.D.META.CFD.IP',        'name': 'Meta',            'category': 'share'},
        {'epic': 'CS.D.NVDA.CFD.IP',        'name': 'NVIDIA',          'category': 'share'},
        {'epic': 'CS.D.TSLA.CFD.IP',        'name': 'Tesla',           'category': 'share'},
        {'epic': 'CS.D.BRK_B.CFD.IP',       'name': 'Berkshire B',     'category': 'share'},
        {'epic': 'CS.D.JPM.CFD.IP',         'name': 'JPMorgan',        'category': 'share'},
        {'epic': 'CS.D.V.CFD.IP',           'name': 'Visa',            'category': 'share'},
        {'epic': 'CS.D.MA.CFD.IP',          'name': 'Mastercard',      'category': 'share'},
        {'epic': 'CS.D.JNJ.CFD.IP',         'name': 'Johnson & Johnson','category': 'share'},
        {'epic': 'CS.D.WMT.CFD.IP',         'name': 'Walmart',         'category': 'share'},
        {'epic': 'CS.D.PG.CFD.IP',          'name': 'Procter & Gamble','category': 'share'},
        {'epic': 'CS.D.DIS.CFD.IP',         'name': 'Disney',          'category': 'share'},
        {'epic': 'CS.D.NFLX.CFD.IP',        'name': 'Netflix',         'category': 'share'},
        {'epic': 'CS.D.TSLA.CFD.IP',        'name': 'Tesla',           'category': 'share'},
    ],
    'shares_us_tech': [
        {'epic': 'CS.D.AMD.CFD.IP',         'name': 'AMD',             'category': 'share'},
        {'epic': 'CS.D.INTC.CFD.IP',        'name': 'Intel',           'category': 'share'},
        {'epic': 'CS.D.CRM.CFD.IP',         'name': 'Salesforce',      'category': 'share'},
        {'epic': 'CS.D.ORCL.CFD.IP',        'name': 'Oracle',          'category': 'share'},
        {'epic': 'CS.D.ADBE.CFD.IP',        'name': 'Adobe',           'category': 'share'},
        {'epic': 'CS.D.CSCO.CFD.IP',        'name': 'Cisco',           'category': 'share'},
        {'epic': 'CS.D.PYPL.CFD.IP',        'name': 'PayPal',          'category': 'share'},
        {'epic': 'CS.D.IBM.CFD.IP',         'name': 'IBM',             'category': 'share'},
    ],
    'shares_uk': [
        {'epic': 'CS.D.SHELL.CFD.IP',       'name': 'Shell',           'category': 'share'},
        {'epic': 'CS.D.AZN.CFD.IP',         'name': 'AstraZeneca',     'category': 'share'},
        {'epic': 'CS.D.HSBA.CFD.IP',        'name': 'HSBC',            'category': 'share'},
        {'epic': 'CS.D.BARC.CFD.IP',        'name': 'Barclays',        'category': 'share'},
        {'epic': 'CS.D.LLOY.CFD.IP',        'name': 'Lloyds',          'category': 'share'},
        {'epic': 'CS.D.BP.CFD.IP',          'name': 'BP',              'category': 'share'},
    ],
    'shares_eu': [
        {'epic': 'CS.D.SAP.CFD.IP',         'name': 'SAP',             'category': 'share'},
        {'epic': 'CS.D.ALV.CFD.IP',         'name': 'Allianz',         'category': 'share'},
        {'epic': 'CS.D.SIE.CFD.IP',         'name': 'Siemens',         'category': 'share'},
        {'epic': 'CS.D.ASML.CFD.IP',        'name': 'ASML',            'category': 'share'},
        {'epic': 'CS.D.ASML.CFD.IP',        'name': 'ASML Holding',    'category': 'share'},
        {'epic': 'CS.D.MC.CFD.IP',          'name': 'LVMH',            'category': 'share'},
        {'epic': 'CS.D.OR.CFD.IP',          'name': "L'Oreal",         'category': 'share'},
    ],
    # ─────────────────────────────────────────────────────────
    # BONDS
    # ─────────────────────────────────────────────────────────
    'bonds': [
        {'epic': 'EB.D.JGB.MONTHLY.IP',     'name': 'Japan 10Y Bond',  'category': 'bond'},
        {'epic': 'EB.D.USB.MONTHLY.IP',     'name': 'US 10Y T-Note',   'category': 'bond'},
        {'epic': 'EB.D.DBR.MONTHLY.IP',     'name': 'German Bund 10Y', 'category': 'bond'},
        {'epic': 'EB.D.GILT.MONTHLY.IP',    'name': 'UK Gilt 10Y',     'category': 'bond'},
    ],
}


def get_default_universe() -> List[Dict]:
    """Return flat list of all curated EPICs (all 5 asset classes)."""
    out = []
    for category, items in DEFAULT_UNIVERSE.items():
        out.extend(items)
    return out


def get_universe_by_class() -> Dict[str, List[Dict]]:
    """Return the structured default universe (by asset class)."""
    return DEFAULT_UNIVERSE


def get_universe_epics() -> List[str]:
    """Return just the EPIC codes for the forex + gold universe.

    Only returns the 41 forex pairs (major/minor/exotic) and spot gold
    (XAU/USD). All other asset classes are excluded from this build.
    """
    forex_gold_keys = {'forex_major', 'forex_minor', 'forex_exotic', 'commodities_metals'}
    out = []
    for category, items in DEFAULT_UNIVERSE.items():
        if category in forex_gold_keys:
            for item in items:
                epic = item['epic']
                # From commodities_metals, include ONLY spot gold
                if category == 'commodities_metals' and epic != 'CS.D.IN_GOLD.MFI.IP':
                    continue
                out.append(epic)
    return out


def get_universe_names() -> Dict[str, str]:
    """Return {epic: human_name} for the default universe."""
    return {item['epic']: item['name'] for item in get_default_universe()}


# ──────────────────────────────────────────────────────────
#  DYNAMIC DISCOVERY
#  Search IG live for any term and return EPICs.
# ──────────────────────────────────────────────────────────

# Cached discovery results (refreshed every 24h)
_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'state', 'cache', 'ig_discovered_universe.json'
)
_CACHE_TTL = 86400  # 24 hours


def _ensure_cache_dir():
    d = os.path.dirname(_CACHE_PATH)
    os.makedirs(d, exist_ok=True)


def _load_cached_discovery() -> Optional[Dict]:
    if not os.path.exists(_CACHE_PATH):
        return None
    if time.time() - os.path.getmtime(_CACHE_PATH) > _CACHE_TTL:
        return None
    try:
        with open(_CACHE_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def _save_cached_discovery(data: Dict):
    try:
        _ensure_cache_dir()
        with open(_CACHE_PATH, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Could not save IG discovery cache: {e}")


def discover_ig_universe(ig_broker, force_refresh: bool = False,
                         search_terms: Optional[List[str]] = None) -> Dict:
    """
    Query IG live for markets matching search terms across all asset classes.
    Returns {asset_class: [markets...]}.

    Caches result for 24h to avoid hammering IG.
    """
    if not force_refresh and not search_terms:
        cached = _load_cached_discovery()
        if cached:
            logger.info(f"Using cached IG universe: {sum(len(v) for v in cached.values())} markets")
            return cached

    if search_terms is None:
        search_terms = [
            # Forex
            'EUR', 'GBP', 'USD', 'JPY', 'CHF', 'AUD', 'CAD', 'NZD',
            # Crypto
            'Bitcoin', 'Ethereum', 'Litecoin', 'XRP', 'Cardano',
            'Polkadot', 'Solana', 'Dogecoin', 'Chainlink', 'Avalanche',
            # Commodities
            'Gold', 'Silver', 'Copper', 'Oil', 'Natural Gas', 'Platinum', 'Palladium',
            'Wheat', 'Corn', 'Soybeans', 'Coffee', 'Sugar', 'Cotton', 'Cocoa',
            # Indices
            'S&P', 'FTSE', 'DAX', 'Nasdaq', 'Dow', 'Wall Street', 'Russell',
            'Nikkei', 'Hang Seng', 'ASX', 'CAC', 'Euro Stoxx',
        ]

    discovered: Dict[str, List[Dict]] = {
        'forex': [],
        'crypto': [],
        'commodity': [],
        'index': [],
        'share': [],
        'option': [],
        'other': [],
    }

    seen_epics = set()

    for term in search_terms:
        try:
            results = ig_broker.search_market(term)
            for m in results:
                epic = m.get('epic')
                if not epic or epic in seen_epics:
                    continue
                seen_epics.add(epic)
                inst_type = m.get('instrument_type', '').upper()
                if 'CURRENCY' in inst_type or epic.startswith('CS.D.') and 'MINI' in epic and any(c in epic for c in ['USD', 'EUR', 'GBP', 'JPY']):
                    category = 'forex'
                elif 'BITCOIN' in epic or 'ETHEREUM' in epic or 'CRYPTO' in inst_type or 'MINI' in epic and any(c in epic for c in ['BITCOIN', 'ETHEREUM', 'LITECOIN', 'XRP', 'CARDANO', 'POLKADOT', 'SOLANA', 'DOGE', 'CHAINLINK', 'AVALANCHE']):
                    category = 'crypto'
                elif 'COMMODITY' in inst_type or epic.startswith('CC.D.') or 'CGC' in epic or 'CSI' in epic:
                    category = 'commodity'
                elif 'INDICES' in inst_type or epic.startswith('IX.D.'):
                    category = 'index'
                elif 'SHARES' in inst_type or 'STOCK' in inst_type:
                    category = 'share'
                elif 'OPTION' in inst_type:
                    category = 'option'
                else:
                    category = 'other'
                m['search_term'] = term
                m['category'] = category
                discovered[category].append(m)
        except Exception as e:
            logger.error(f"IG search '{term}' failed: {e}")

    if not force_refresh:
        _save_cached_discovery(discovered)

    total = sum(len(v) for v in discovered.values())
    logger.info(f"Discovered {total} IG markets across {len(discovered)} asset classes")
    return discovered


def flatten_discovered(discovered: Dict[str, List[Dict]]) -> List[Dict]:
    """Flatten the discovered dict into a single list of markets."""
    out = []
    for category, items in discovered.items():
        out.extend(items)
    return out


# ──────────────────────────────────────────────────────────
#  LIVE PROBE: ask IG which EPICs are actually tradable.
#  Filters the 92 curated EPICs down to the ones this account
#  can really trade. Critical for live mode (we don't want
#  to keep hitting "instrument.epic.unavailable" in production).
# ──────────────────────────────────────────────────────────
PROBE_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'state', 'probe_cache.pkl'
)


def probe_universe(broker, epics: List[str] = None,
                   force: bool = False) -> Dict[str, dict]:
    """For each EPIC, ask IG /markets/{epic} whether it's available.
    Returns {epic: {'available': bool, 'name': str, 'bid': float, 'offer': float}}.
    Uses a 6h cache to avoid hitting IG on every request.
    Limits to the first 30 EPICs (with priority on crypto+forex) to stay
    under the 30 req/min allowance — probes all 166 would take 60+ sec.

    Defensive: even if the cache file is empty, corrupt, or contains a
    0-result probe, we still return a sane dict (never None, never empty).
    """
    if epics is None:
        epics = get_universe_epics()
    # ── Cache check (6h) — extremely defensive ──
    if not force:
        try:
            size = os.path.getsize(PROBE_CACHE_PATH) if os.path.exists(PROBE_CACHE_PATH) else 0
            if size > 200:  # at least ~10 entries worth of pickle
                with open(PROBE_CACHE_PATH, 'rb') as f:
                    cache = pickle.load(f)
                results_cached = cache.get('results', {}) if isinstance(cache, dict) else {}
                age = time.time() - cache.get('ts', 0) if isinstance(cache, dict) else 1e9
                if age < 21600 and len(results_cached) > 0:  # 6h, non-empty
                    # Mark any EPICs not in the cache as not_probed
                    for epic in epics:
                        if epic not in results_cached:
                            results_cached[epic] = {'available': False, 'name': epic,
                                                     'reason': 'not_in_cache'}
                    return results_cached
        except (pickle.UnpicklingError, EOFError, FileNotFoundError, Exception) as e:
            logger.debug(f"probe cache load failed ({e}), will re-probe")
    # ── Re-probe: 30 EPICs max (priority on crypto + major forex) ──
    priority = []
    for must in ['CS.D.BITCOIN.CFBMU.IP', 'CS.D.BITCOIN.CFD.IP',
                 'CS.D.ETHEREUM.CFBMU.IP', 'CS.D.ETHEREUM.CFD.IP',
                 'CS.D.EURUSD.MINI.IP', 'CS.D.GBPUSD.MINI.IP',
                 'CS.D.USDJPY.MINI.IP', 'CS.D.AUDUSD.MINI.IP',
                 'CS.D.USDCAD.MINI.IP', 'CS.D.NZDUSD.MINI.IP',
                 'CS.D.IN_GOLD.MFI.IP', 'CS.D.IN_SILVER.MFI.IP',
                 'IX.D.SPTRD.DAILY.IP', 'IX.D.FTSE.DAILY.IP',
                 'CC.D.CL.USS.IP']:
        if must in epics:
            priority.append(must)
    rest = [e for e in epics if e not in priority]
    sample = priority + rest[:max(0, 30 - len(priority))]
    results = {}
    for epic in sample:
        try:
            info = broker.get_market_info(epic) if broker else None
            if info and info.get('bid') and info.get('offer'):
                results[epic] = {
                    'available': True,
                    'name': info.get('instrument_name', epic),
                    'bid': float(info['bid']),
                    'offer': float(info['offer']),
                    'market_status': info.get('market_status', 'UNKNOWN'),
                }
            else:
                results[epic] = {'available': False, 'name': epic,
                                 'reason': 'no_bid_offer'}
        except Exception as e:
            results[epic] = {'available': False, 'name': epic,
                             'reason': str(e)[:100]}
        time.sleep(0.3)  # be gentle on IG
    # Mark un-probed EPICs as "unknown"
    for epic in epics:
        if epic not in results:
            results[epic] = {'available': False, 'name': epic,
                             'reason': 'not_probed'}
    # ── Only write cache if we got at least 3 results ──
    if len(results) >= 3:
        try:
            os.makedirs(os.path.dirname(PROBE_CACHE_PATH), exist_ok=True)
            tmp_path = PROBE_CACHE_PATH + '.tmp'
            payload = {'ts': time.time(), 'results': results}
            with open(tmp_path, 'wb') as f:
                pickle.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, PROBE_CACHE_PATH)  # atomic
        except Exception as e:
            logger.warning(f"probe cache write failed: {e}")
    return results


def get_live_universe(broker=None, force: bool = False) -> List[str]:
    """Return the list of EPICs that are actually tradable on this account."""
    probe = probe_universe(broker, force=force)
    return [e for e, v in probe.items() if v.get('available')]
