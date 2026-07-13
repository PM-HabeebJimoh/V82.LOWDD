"""
V82.LOWDD - Risk Management with HARD <20% Max DD Constraint.

Ported 1:1 from /home/user/hydra_prime/v82_lowdd_system/risk/manager.py.

The core insight: by sizing positions at 0.1% risk per trade,
we guarantee that no single trade can lose more than 0.1% of capital.

This is a HARD CONSTRAINT — not an outcome of optimization.

Key risk rules:
  - risk_per_trade: 0.1% of current capital (TINY)
  - max_dd_threshold: 20% (HARD LIMIT - pause trading if exceeded)
  - daily_loss_limit: 5% of capital (pause for the day)
  - position sizing: (capital * risk_per_trade) / risk_per_unit
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    """Live risk state for the account."""
    initial_capital: float = 10000.0
    capital: float = 10000.0
    peak_capital: float = 10000.0
    max_dd_threshold: float = 0.20
    daily_loss_limit_pct: float = 0.05
    risk_per_trade: float = 0.001
    cooldown_bars: int = 0

    paused_until_eod: bool = False
    paused_until_recovery: bool = False
    last_dd: float = 0.0
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    total_pnl_today: float = 0.0
    current_day: Optional[str] = None

    def update_pnl(self, pnl: float, day: str):
        if self.current_day != day:
            self.current_day = day
            self.total_pnl_today = 0.0
        self.capital += pnl
        self.peak_capital = max(self.peak_capital, self.capital)
        self.total_pnl_today += pnl
        self.last_dd = (self.peak_capital - self.capital) / self.peak_capital

    def can_trade(self) -> bool:
        if self.paused_until_eod or self.paused_until_recovery:
            return False
        if self.last_dd > self.max_dd_threshold:
            self.paused_until_recovery = True
            logger.warning(
                f"PAUSED: DD {self.last_dd*100:.1f}% > {self.max_dd_threshold*100:.1f}% threshold"
            )
            return False
        if self.total_pnl_today < -self.capital * self.daily_loss_limit_pct:
            self.paused_until_eod = True
            logger.warning(
                f"PAUSED for day: daily loss {self.total_pnl_today:.0f} "
                f"exceeds {self.daily_loss_limit_pct*100:.1f}%"
            )
            return False
        if self.capital <= 0:
            return False
        return True

    def check_dd_breach(self) -> bool:
        if self.last_dd > self.max_dd_threshold and not self.paused_until_recovery:
            self.paused_until_recovery = True
            logger.warning(
                f"DD BREACH: {self.last_dd*100:.1f}% > {self.max_dd_threshold*100:.1f}%"
            )
            return True
        return False

    def reset_daily(self):
        self.total_pnl_today = 0.0
        self.paused_until_eod = False

    def recover_pause_if_better(self):
        if self.paused_until_recovery and self.last_dd < self.max_dd_threshold * 0.8:
            self.paused_until_recovery = False
            logger.info(f"UNPAUSED: DD recovered to {self.last_dd*100:.1f}%")

    @property
    def max_dd(self) -> float:
        return (self.peak_capital - self.capital) / self.peak_capital if self.peak_capital > 0 else 0

    @property
    def total_return_pct(self) -> float:
        return (self.capital / self.initial_capital - 1) * 100

    @property
    def win_rate(self) -> float:
        return (self.n_wins / self.n_trades * 100) if self.n_trades > 0 else 0

    def size_position(self, entry: float, stop: float, max_leverage: float = 1.0,
                      contract_size: float = 1.0) -> float:
        """Size a position by both:
          - risk_dollars = capital * risk_per_trade  (per-trade risk budget)
          - notional cap  = capital * max_leverage      (max position notional)
        Returns the minimum of the two (in units), so we never exceed
        either constraint. Defaults to 1x leverage for safety on the
        $10k demo account with $20k IG balance.
        """
        if not self.can_trade():
            return 0
        if entry <= 0:
            return 0
        risk_per_unit = abs(entry - stop)
        risk_dollars = self.capital * self.risk_per_trade
        if risk_per_unit > 0:
            n_by_risk = risk_dollars / (risk_per_unit * contract_size)
        else:
            n_by_risk = 0
        # Notional cap: position value <= capital * max_leverage
        max_notional = self.capital * max_leverage
        n_by_notional = max_notional / (entry * contract_size)
        n = min(n_by_risk, n_by_notional)
        # Round to 1 decimal place (mini CFDs allow 0.5 increments)
        n = max(0.0, round(n, 2))
        return n


class RiskManager:
    """Risk manager enforcing the <20% Max DD constraint."""

    def __init__(self, initial_capital: float = 10000.0,
                 risk_per_trade: float = 0.001,
                 max_dd_threshold: float = 0.20,
                 daily_loss_limit_pct: float = 0.05):
        self.state = RiskState(
            initial_capital=initial_capital,
            capital=initial_capital,
            peak_capital=initial_capital,
            max_dd_threshold=max_dd_threshold,
            daily_loss_limit_pct=daily_loss_limit_pct,
            risk_per_trade=risk_per_trade,
        )
        self.history: List[Dict] = []
        self.daily_pnl: Dict[str, float] = {}

    def on_new_day(self, day: str):
        if self.state.current_day and self.state.current_day != day:
            self.state.reset_daily()
        self.state.current_day = day
        self.state.total_pnl_today = self.daily_pnl.get(day, 0.0)

    def record_trade(self, pnl: float, day: str) -> bool:
        self.state.update_pnl(pnl, day)
        self.daily_pnl[day] = self.daily_pnl.get(day, 0.0) + pnl
        self.state.n_trades += 1
        if pnl > 0:
            self.state.n_wins += 1
        else:
            self.state.n_losses += 1
        breach = self.state.check_dd_breach()
        if not breach:
            self.state.recover_pause_if_better()
        return not breach

    def can_trade(self) -> bool:
        return self.state.can_trade()

    def size(self, entry: float, stop: float) -> float:
        return self.state.size_position(entry, stop)

    def size_position(self, entry: float, stop: float, max_leverage: float = 1.0,
                      contract_size: float = 1.0) -> float:
        return self.state.size_position(entry, stop, max_leverage=max_leverage,
                                       contract_size=contract_size)

    def snapshot(self) -> Dict:
        return {
            'capital': self.state.capital,
            'initial_capital': self.state.initial_capital,
            'peak': self.state.peak_capital,
            'peak_capital': self.state.peak_capital,
            'last_dd': self.state.last_dd,
            'last_dd_pct': self.state.last_dd * 100,
            'max_dd': self.state.max_dd,
            'max_dd_pct': self.state.max_dd * 100,
            'total_return_pct': self.state.total_return_pct,
            'n_trades': self.state.n_trades,
            'n_wins': self.state.n_wins,
            'n_losses': self.state.n_losses,
            'win_rate': self.state.win_rate,
            'paused': self.state.paused_until_recovery or self.state.paused_until_eod,
            'paused_until_recovery': self.state.paused_until_recovery,
            'paused_until_eod': self.state.paused_until_eod,
            'daily_pnl': self.state.total_pnl_today,
        }
