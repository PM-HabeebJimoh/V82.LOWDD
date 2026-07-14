"""
V82.LOWDD — Universe Rotator (FOREX + GOLD ONLY mode).

Polls ALL 42 instruments (41 forex pairs + spot gold XAUUSD) every single
tick (every 60 s) — no rotation, no batching, no dropped symbols.

Forex breakdown:
  - 7 major pairs  (EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD, USD/CAD, NZD/USD)
  - 21 minor pairs (EUR/GBP, EUR/JPY, GBP/JPY, AUD/JPY, EUR/CHF, CAD/JPY …)
  - 13 exotic pairs (USD/MXN, USD/ZAR, USD/TRY, USD/PLN, USD/SEK …)
  Total forex: 41

Gold:
  - CS.D.IN_GOLD.MFI.IP  (spot XAU/USD — IG gold mini)

Grand total: 42 EPICs checked every minute.
"""
import time
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Forex — 7 major + 21 minor + 13 exotic ──────────────────────
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

# ── Gold (spot XAU/USD) ──────────────────────────────────────────
GOLD = ['CS.D.IN_GOLD.MFI.IP']

# ── Full universe — 42 EPICs, polled every tick ──────────────────
ALL_EPICS: List[str] = FOREX_MAJOR + FOREX_MINOR + FOREX_EXOTIC + GOLD

# No crypto "always polled" — this build is forex + gold only
ALWAYS_POLLED: List[str] = []


def build_batches() -> List[List[str]]:
    """Single batch — all 42 EPICs checked every 60-second tick.

    Ordering: majors first (highest priority), then minors, then exotics,
    then gold.  This means if IG throttles mid-tick the most liquid pairs
    are guaranteed to be scanned first.
    """
    return [list(ALL_EPICS)]


class UniverseRotator:
    """Single-batch rotator — every tick returns all 42 forex+gold EPICs.

    The advance() call is a no-op (there is only one batch) but kept for
    API compatibility with live_mode.py.
    """

    def __init__(self, batches: Optional[List[List[str]]] = None):
        self.batches = batches or build_batches()
        self.idx = 0
        self.tick_count = 0
        self.crypto = ALWAYS_POLLED  # empty — no crypto in this build

    def current(self) -> List[str]:
        """Return ALL 42 EPICs, sorted by poll_priority (highest first)."""
        batch = self.batches[self.idx % len(self.batches)]
        seen: set = set()
        out: List[str] = []
        for s in batch:
            if s not in seen:
                seen.add(s)
                out.append(s)
        # Sort by poll_priority descending (majors=90, minors=85, exotics=70, gold=80)
        try:
            from backend.core.instrument_config import get_config
            def _priority(s: str) -> int:
                return get_config(s).get('poll_priority', 50)
            out.sort(key=_priority, reverse=True)
        except Exception:
            pass  # keep insertion order if config not yet available
        return out

    def advance(self):
        """No-op for a single-batch universe; increments tick counter."""
        self.tick_count += 1
        # idx stays 0 — only one batch
        return self.current()

    def stats(self) -> dict:
        all_syms = sorted(set(ALL_EPICS))
        return {
            'n_batches': len(self.batches),
            'batch_idx': self.idx,
            'tick_count': self.tick_count,
            'current_batch': self.batches[self.idx % len(self.batches)],
            'always_polled': self.crypto,
            'all_symbols': all_syms,
            'n_unique_symbols': len(all_syms),
            'mode': 'forex_gold_only',
        }
