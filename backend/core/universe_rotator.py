"""
V82.LOWDD — Universe Rotator.

To run 24/7 across ALL 166+ IG instruments (forex, crypto, commodities,
indices, options, shares, bonds) without hitting IG's rate limits, we
partition the universe into rotating batches. The engine polls one batch
per tick, then rotates. This way every instrument is checked multiple
times per hour, but no single tick overloads IG.

Batches are organized by priority:
  1. 24/7 crypto — always first (bitcoin, ethereum, etc.)
  2. Major forex — rotates
  3. Minor/exotic forex — rotates
  4. Indices (US/EU/Asia) — rotates
  5. Commodities (metals, energy, softs) — rotates
  6. Shares (US tech, UK, EU) — rotates
  7. Options (weekly + daily) — rotates
  8. Bonds — rotates
"""
import time
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# 24/7 crypto — ALWAYS polled every tick (no rotation)
ALWAYS_POLLED = [
    'CS.D.BITCOIN.CFBMU.IP',  # Bitcoin mini ($0.1)
    'CS.D.BITCOIN.CFD.IP',    # Bitcoin full ($1)
    'CS.D.ETHEREUM.CFBMU.IP', # Ethereum mini
    'CS.D.ETHEREUM.CFD.IP',   # Ethereum full
]

# Forex — 7 major + 21 minor + 13 exotic
FOREX_MAJOR = [
    'CS.D.EURUSD.MINI.IP', 'CS.D.GBPUSD.MINI.IP', 'CS.D.USDJPY.MINI.IP',
    'CS.D.USDCHF.MINI.IP', 'CS.D.AUDUSD.MINI.IP', 'CS.D.USDCAD.MINI.IP',
    'CS.D.NZDUSD.MINI.IP',
]
FOREX_MINOR = [
    'CS.D.EURGBP.MINI.IP', 'CS.D.EURJPY.MINI.IP', 'CS.D.GBPJPY.MINI.IP',
    'CS.D.AUDJPY.MINI.IP', 'CS.D.EURCHF.MINI.IP', 'CS.D.CADJPY.MINI.IP',
    'CS.D.AUDCAD.MINI.IP', 'CS.D.AUDNZD.MINI.IP', 'CS.D.CADCHF.MINI.IP',
    'CS.D.CHFJPY.MINI.IP', 'CS.D.EURAUD.MINI.IP', 'CS.D.EURCAD.MINI.IP',
    'CS.D.EURNZD.MINI.IP', 'CS.D.GBPAUD.MINI.IP', 'CS.D.GBPCAD.MINI.IP',
    'CS.D.GBPNZD.MINI.IP', 'CS.D.GBPSGD.MINI.IP', 'CS.D.NZDCAD.MINI.IP',
    'CS.D.NZDCHF.MINI.IP', 'CS.D.NZDJPY.MINI.IP', 'CS.D.SGDJPY.MINI.IP',
]
FOREX_EXOTIC = [
    'CS.D.USDMXN.MINI.IP', 'CS.D.USDZAR.MINI.IP', 'CS.D.USDTRY.MINI.IP',
    'CS.D.USDPLN.MINI.IP', 'CS.D.USDSEK.MINI.IP', 'CS.D.USDNOK.MINI.IP',
    'CS.D.USDDKK.MINI.IP', 'CS.D.USDHUF.MINI.IP', 'CS.D.USDCZK.MINI.IP',
    'CS.D.EURPLN.MINI.IP', 'CS.D.EURSEK.MINI.IP', 'CS.D.EURNOK.MINI.IP',
    'CS.D.EURTRY.MINI.IP',
]

# Indices — rotates by session
INDICES_US = [
    'IX.D.SPTRD.DAILY.IP',  'IX.D.NASDAQ.DAILY.IP', 'IX.D.DOW.DAILY.IP',
    'IX.D.RUS2000.DAILY.IP',
]
INDICES_EU = [
    'IX.D.FTSE.DAILY.IP', 'IX.D.DAX.DAILY.IP', 'IX.D.CAC.DAILY.IP',
    'IX.D.EUSTX50.DAILY.IP', 'IX.D.IBEX.DAILY.IP', 'IX.D.FTSEM.DAILY.IP',
    'IX.D.SMI.DAILY.IP',
]
INDICES_ASIA = [
    'IX.D.NIKKEI.DAILY.IP', 'IX.D.HSI.DAILY.IP', 'IX.D.SSMCOMP.DAILY.IP',
    'IX.D.AUS200.DAILY.IP', 'IX.D.TWSE.DAILY.IP', 'IX.D.KOSPI.DAILY.IP',
    'IX.D.SENSEX.DAILY.IP',
]

# Commodities
COMMODITIES_METALS = ['CS.D.IN_GOLD.MFI.IP', 'CS.D.IN_SILVER.MFI.IP',
                      'CS.D.COPPER.MINI.IP', 'CS.D.PALL.MINI.IP',
                      'CS.D.PLAT.MINI.IP', 'CS.D.IRON.MINI.IP']
COMMODITIES_ENERGY = ['CC.D.CL.USS.IP', 'CC.D.B.USS.IP', 'CC.D.NG.USC.IP',
                      'CC.D.HO.USC.IP', 'CC.D.RB.USC.IP', 'CC.D.C.USC.IP']
COMMODITIES_SOFTS = ['CC.D.CT.USC.IP', 'CC.D.SB.USC.IP', 'CC.D.KC.USC.IP',
                     'CC.D.CC.USC.IP', 'CC.D.W.USC.IP', 'CC.D.S.USC.IP',
                     'CC.D.CN.USC.IP', 'CC.D.OJ.USC.IP', 'CC.D.L.USC.IP',
                     'CC.D.GC.USC.IP']

# Shares
SHARES_US_MEGA = [
    'CS.D.AAPL.CFD.IP', 'CS.D.MSFT.CFD.IP', 'CS.D.GOOGL.CFD.IP',
    'CS.D.AMZN.CFD.IP', 'CS.D.META.CFD.IP', 'CS.D.NVDA.CFD.IP',
    'CS.D.TSLA.CFD.IP', 'CS.D.BRK_B.CFD.IP', 'CS.D.JPM.CFD.IP',
    'CS.D.V.CFD.IP', 'CS.D.MA.CFD.IP', 'CS.D.JNJ.CFD.IP',
    'CS.D.WMT.CFD.IP', 'CS.D.PG.CFD.IP', 'CS.D.DIS.CFD.IP',
    'CS.D.NFLX.CFD.IP',
]
SHARES_US_TECH = [
    'CS.D.AMD.CFD.IP', 'CS.D.INTC.CFD.IP', 'CS.D.CRM.CFD.IP',
    'CS.D.ORCL.CFD.IP', 'CS.D.ADBE.CFD.IP', 'CS.D.CSCO.CFD.IP',
    'CS.D.PYPL.CFD.IP', 'CS.D.IBM.CFD.IP',
]
SHARES_UK = [
    'CS.D.SHELL.CFD.IP', 'CS.D.AZN.CFD.IP', 'CS.D.HSBA.CFD.IP',
    'CS.D.BARC.CFD.IP', 'CS.D.LLOY.CFD.IP', 'CS.D.BP.CFD.IP',
]
SHARES_EU = [
    'CS.D.SAP.CFD.IP', 'CS.D.ALV.CFD.IP', 'CS.D.SIE.CFD.IP',
    'CS.D.ASML.CFD.IP', 'CS.D.MC.CFD.IP', 'CS.D.OR.CFD.IP',
]

# Options (weekly + daily)
OPTIONS_US_IDX = [
    'OP.D.SPXW.DAILY.IP', 'OP.D.NDXW.DAILY.IP', 'OP.D.DJIW.DAILY.IP',
    'OP.D.RUTW.DAILY.IP',
]
OPTIONS_US_STOCKS = [
    'OP.D.AAPL.DAILY.IP', 'OP.D.TSLA.DAILY.IP', 'OP.D.NVDA.DAILY.IP',
    'OP.D.MSFT.DAILY.IP', 'OP.D.AMZN.DAILY.IP', 'OP.D.GOOGL.DAILY.IP',
]
OPTIONS_EU_IDX = [
    'OP.D.FTSE.DAILY.IP', 'OP.D.DAX.DAILY.IP', 'OP.D.EUSTX50.DAILY.IP',
]
OPTIONS_FX = [
    'OP.D.EURUSD.DAILY.IP', 'OP.D.GBPUSD.DAILY.IP', 'OP.D.USDJPY.DAILY.IP',
]
OPTIONS_COMM = [
    'OP.D.GOLD.DAILY.IP', 'OP.D.OIL.DAILY.IP',
]

# Bonds
BONDS = [
    'EB.D.JGB.MONTHLY.IP', 'EB.D.USB.MONTHLY.IP',
    'EB.D.DBR.MONTHLY.IP', 'EB.D.GILT.MONTHLY.IP',
]


def build_batches() -> List[List[str]]:
    """Build ordered list of batches (each ≤ 12 symbols)."""
    batches = []
    # Batch 0: 24/7 crypto + a few majors
    batches.append(ALWAYS_POLLED + FOREX_MAJOR[:5])  # 4 + 5 = 9
    # Batch 1: Rest of forex majors + minor
    batches.append(FOREX_MAJOR[5:] + FOREX_MINOR[:7])  # 2 + 7 = 9
    # Batch 2: More forex minor + exotic
    batches.append(FOREX_MINOR[7:14] + FOREX_EXOTIC[:5])  # 7 + 5 = 12
    # Batch 3: Rest of exotic + commodities metals
    batches.append(FOREX_EXOTIC[5:] + COMMODITIES_METALS[:4])  # 8 + 4 = 12
    # Batch 4: Commodities energy + softs
    batches.append(COMMODITIES_ENERGY + COMMODITIES_SOFTS[:6])  # 6 + 6 = 12
    # Batch 5: Softs + indices US
    batches.append(COMMODITIES_SOFTS[6:] + INDICES_US)  # 4 + 4 = 8
    # Batch 6: Indices EU + Asia
    batches.append(INDICES_EU + INDICES_ASIA[:5])  # 7 + 5 = 12
    # Batch 7: Asia + shares US mega
    batches.append(INDICES_ASIA[5:] + SHARES_US_MEGA[:9])  # 2 + 9 = 11
    # Batch 8: Shares US mega + tech
    batches.append(SHARES_US_MEGA[9:] + SHARES_US_TECH[:6])  # 7 + 5 = 12
    # Batch 9: Rest of tech + UK + EU shares
    batches.append(SHARES_US_TECH[6:] + SHARES_UK + SHARES_EU[:3])  # 2 + 6 + 3 = 11
    # Batch 10: Rest of EU shares + options US idx
    batches.append(SHARES_EU[3:] + OPTIONS_US_IDX)  # 3 + 4 = 7
    # Batch 11: Options US stocks + EU idx
    batches.append(OPTIONS_US_STOCKS + OPTIONS_EU_IDX)  # 6 + 3 = 9
    # Batch 12: Options FX + comm + bonds
    batches.append(OPTIONS_FX + OPTIONS_COMM + BONDS)  # 3 + 2 + 4 = 9
    return batches


class UniverseRotator:
    """Rotates through all batches across ticks. 24/7 crypto is always polled."""

    def __init__(self, batches: Optional[List[List[str]]] = None):
        self.batches = batches or build_batches()
        self.idx = 0
        self.tick_count = 0
        # 24/7 crypto always first
        self.crypto = ALWAYS_POLLED

    def current(self) -> List[str]:
        """Return the symbols to poll this tick (always includes crypto).
        Sorts by poll_priority so highest-priority classes are polled first.
        """
        batch = self.batches[self.idx % len(self.batches)]
        seen = set()
        out = []
        for s in self.crypto + list(batch):
            if s not in seen:
                seen.add(s)
                out.append(s)
        # Sort by poll_priority from instrument_config
        from backend.core.instrument_config import get_config
        def _priority(s):
            cfg = get_config(s)
            return cfg.get('poll_priority', 50)
        # Stable sort by priority desc, keep crypto first
        crypto_set = set(self.crypto)
        crypto_part = sorted([s for s in out if s in crypto_set],
                             key=_priority, reverse=True)
        rest_part = sorted([s for s in out if s not in crypto_set],
                           key=_priority, reverse=True)
        return crypto_part + rest_part

    def advance(self):
        """Move to the next batch. Returns the new current batch."""
        self.tick_count += 1
        self.idx = (self.idx + 1) % len(self.batches)
        return self.current()

    def stats(self) -> dict:
        all_syms = sorted({s for b in self.batches for s in b} | set(self.crypto))
        return {
            'n_batches': len(self.batches),
            'batch_idx': self.idx,
            'tick_count': self.tick_count,
            'current_batch': self.batches[self.idx % len(self.batches)],
            'always_paid': self.crypto,
            'all_symbols': all_syms,
            'n_unique_symbols': len(all_syms),
        }
