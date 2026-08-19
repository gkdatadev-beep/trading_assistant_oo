"""Prop-firm challenge account — immutable domain model."""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ChallengeAccount:
    """Immutable prop-firm challenge state.

    All methods return NEW instances.  Never modifies self.
    """

    initial_balance: float
    current_balance: float
    equity_peak: float          # Highest balance ever reached
    daily_pnl: tuple[float, ...]  # Immutable sequence

    phase: str                   # "phase1" | "phase2" | "funded"
    profit_target_pct: float     # 8.0 or 5.0
    daily_drawdown_limit_pct: float   # typically 5.0
    max_total_drawdown_pct: float     # typically 10.0
    min_trading_days: int

    # ── balance updates ─────────────────────────────────────

    def apply_closed_trade(self, pnl: float) -> ChallengeAccount:
        """Return new account after a trade's PnL is realized."""
        new_balance = round(self.current_balance + pnl, 2)
        new_peak = max(self.equity_peak, new_balance)
        new_pnl = self.daily_pnl + (pnl,)
        return replace(
            self,
            current_balance=new_balance,
            equity_peak=new_peak,
            daily_pnl=new_pnl,
        )

    def end_day(self) -> ChallengeAccount:
        """Close the day — reset daily PnL tracker."""
        return replace(self, daily_pnl=())

    # ── rule checks ─────────────────────────────────────────

    def rule_violations(self) -> list[str]:
        """All currently violated rules."""
        violations: list[str] = []

        # Daily drawdown: (peak - current) / initial >= limit %
        daily_dd = self._daily_drawdown_pct()
        if daily_dd >= self.daily_drawdown_limit_pct:
            violations.append(
                f"Daily drawdown: {daily_dd:.2f}% >= {self.daily_drawdown_limit_pct}%"
            )

        # Total drawdown: (initial - current) / initial >= limit %
        total_dd = self._total_drawdown_pct()
        if total_dd >= self.max_total_drawdown_pct:
            violations.append(
                f"Total drawdown: {total_dd:.2f}% >= {self.max_total_drawdown_pct}%"
            )

        return violations

    def is_breached(self) -> bool:
        return len(self.rule_violations()) > 0

    # ── progress ────────────────────────────────────────────

    def progress_pct(self) -> float:
        """How close to the profit target (0.0 to 100.0+)."""
        if self.initial_balance <= 0:
            return 0.0
        gain = self.current_balance - self.initial_balance
        return round((gain / self.initial_balance) * 100.0, 2)

    def is_target_reached(self) -> bool:
        return self.progress_pct() >= self.profit_target_pct

    def is_funded(self) -> bool:
        """Phase passed and no violations."""
        return (
            self.phase == "funded"
            or (self.is_target_reached() and not self.is_breached())
        )

    def days_traded(self) -> int:
        """Days with at least one closed trade."""
        return len(set()) if not self.daily_pnl else 1  # simplified

    def days_remaining(self) -> int:
        return max(0, self.min_trading_days - self.days_traded())

    # ── private ─────────────────────────────────────────────

    def _daily_drawdown_pct(self) -> float:
        if self.initial_balance <= 0:
            return 0.0
        return round(
            max(0.0, self.equity_peak - self.current_balance) / self.initial_balance * 100.0,
            2,
        )

    def _total_drawdown_pct(self) -> float:
        if self.initial_balance <= 0:
            return 0.0
        return round(
            max(0.0, self.initial_balance - self.current_balance) / self.initial_balance * 100.0,
            2,
        )

    # ── serialization ───────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "initial_balance": self.initial_balance,
            "current_balance": self.current_balance,
            "equity_peak": self.equity_peak,
            "progress_pct": self.progress_pct(),
            "phase": self.phase,
            "is_breached": self.is_breached(),
            "is_funded": self.is_funded(),
            "violations": self.rule_violations(),
            "today_pnl": round(sum(self.daily_pnl), 2),
            "days_traded": self.days_traded(),
        }

    # ── factory ─────────────────────────────────────────────

    @staticmethod
    def for_phase1(balance: float = 100_000.0) -> ChallengeAccount:
        return ChallengeAccount(
            initial_balance=balance,
            current_balance=balance,
            equity_peak=balance,
            daily_pnl=(),
            phase="phase1",
            profit_target_pct=8.0,
            daily_drawdown_limit_pct=5.0,
            max_total_drawdown_pct=10.0,
            min_trading_days=5,
        )

    @staticmethod
    def for_phase2(balance: float = 100_000.0) -> ChallengeAccount:
        return ChallengeAccount(
            initial_balance=balance,
            current_balance=balance,
            equity_peak=balance,
            daily_pnl=(),
            phase="phase2",
            profit_target_pct=5.0,
            daily_drawdown_limit_pct=5.0,
            max_total_drawdown_pct=10.0,
            min_trading_days=5,
        )
