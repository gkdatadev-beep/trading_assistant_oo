"""Risk evaluation and enforcement — pure domain logic, no MT5 dependency."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.trade import Trade
from app.core.session import TradingSession


@dataclass
class RiskLimits:
    """Parameter snapshot — read from config once per tick."""
    limite_lucro_diario: float = 750.0
    limite_perda_diario: float = -350.0
    limite_operacoes: int = 10
    limite_lucro_op: float = 350.0
    limite_perda_op: float = -200.0
    max_profit_retention_pct: float = 50.0
    lock_entry: float = 0.5
    overtrading_protection_minutes: int = 12
    overtrading_loss_streak: int = 2  # perdas consecutivas que travam os trades


@dataclass(frozen=True)
class RiskResult:
    """Output of a risk evaluation — which flags should be active."""
    block_by_profit: bool = False
    block_by_loss: bool = False
    block_by_operations: bool = False
    block_by_time: bool = False
    block_overtrading: bool = False
    overtrading_triggered: bool = False
    overtrading_reason: str = ""
    max_daily_profit: float = 0.0
    current_drawdown: float = 0.0


class RiskManager:
    """Evaluates trading limits and decides which block flags to activate.

    Pure domain logic — no MT5, no I/O.  Takes a TradingSession,
    RiskLimits, and current metrics; returns a RiskResult.
    """

    def __init__(self, limits: Optional[RiskLimits] = None):
        self._limits = limits or RiskLimits()

    @property
    def limits(self) -> RiskLimits:
        """Public read-only access to current risk limits."""
        return self._limits

    # ── lock-entry ──────────────────────────────────────────

    def has_lock_entry(self, trade: Trade, atr: float) -> bool:
        """True when the trade's price distance >= LOCK_ENTRY * ATR."""
        threshold = self._limits.lock_entry * atr
        return trade.distance_from_entry() >= threshold

    def lock_entry_threshold(self, atr: float) -> float:
        return round(self._limits.lock_entry * atr, 2)

    # ── per-trade limits ────────────────────────────────────

    def per_trade_blocked(self, trade: Trade) -> Optional[str]:
        """Return reason string if per-trade limit is breached, else None."""
        if trade.profit > self._limits.limite_lucro_op:
            return "lucro_max_op"
        if trade.profit < self._limits.limite_perda_op:
            return "perda_max_op"
        return None

    # ── overtrading: perdas consecutivas ────────────────────

    @staticmethod
    def count_consecutive_losses(closed_trades: list[dict]) -> int:
        """Conta as perdas consecutivas MAIS RECENTES no dia.

        closed_trades: lista de trades fechados com `profit` e,
        opcionalmente, `time` (HH:MM:SS). A contagem parte do trade
        mais recente e anda para trás enquanto `profit < 0`.
        """
        if not closed_trades:
            return 0
        ordered = sorted(
            closed_trades,
            key=lambda t: str(t.get("time") or ""),
            reverse=True,
        )
        streak = 0
        for trade in ordered:
            try:
                profit = float(trade.get("profit") or 0)
            except (TypeError, ValueError):
                profit = 0.0
            if profit < 0:
                streak += 1
            else:
                break
        return streak

    def should_trigger_overtrading(
        self, streak: int, seen: int, active: bool,
    ) -> bool:
        """True quando o streak atual (>= overtrading_loss_streak) ainda
        não travou os trades e a proteção não está ativa."""
        return (
            not active
            and streak >= max(self._limits.overtrading_loss_streak, 1)
            and streak > seen
        )

    # ── daily evaluation ────────────────────────────────────

    def evaluate(
        self,
        session: TradingSession,
        daily_profit: float,
        operations_count: int,
        is_outside_window: bool,
        overtrading_active: bool,
        overtrading_triggered: bool,
        overtrading_reason: str = "",
    ) -> RiskResult:
        """Return which block flags should be active given current state."""

        L = self._limits

        # Latched flags: once active, stay active until day change
        block_by_profit = session.flag_block_by_profit or (daily_profit > L.limite_lucro_diario)
        block_by_operations = (
            session.flag_block_by_operations or (operations_count >= L.limite_operacoes)
        )

        # Drawdown from peak — LATCHED: never decreases intraday
        max_daily_profit = max(session.max_daily_profit, daily_profit)
        instant_dd = round(max(0.0, max_daily_profit - daily_profit), 2)
        current_drawdown = max(session.current_drawdown, instant_dd)
        drawdown_limit = abs(L.limite_perda_diario)
        block_by_loss = (
            session.flag_block_by_loss or
            (drawdown_limit > 0 and current_drawdown >= drawdown_limit)
        )

        # Non-latched: fresh each tick
        block_by_time = is_outside_window
        block_overtrading = overtrading_active

        return RiskResult(
            block_by_profit=block_by_profit,
            block_by_loss=block_by_loss,
            block_by_operations=block_by_operations,
            block_by_time=block_by_time,
            block_overtrading=block_overtrading,
            overtrading_triggered=overtrading_triggered,
            overtrading_reason=overtrading_reason,
            max_daily_profit=round(max_daily_profit, 2),
            current_drawdown=current_drawdown,
        )

    # ── session application ──────────────────────────────────

    def apply_to_session(
        self,
        session: TradingSession,
        result: RiskResult,
    ) -> TradingSession:
        """Return a new TradingSession with flags from the RiskResult."""
        return (
            session
            .with_flags(
                flag_block_by_profit=result.block_by_profit,
                flag_block_by_loss=result.block_by_loss,
                flag_block_by_operations=result.block_by_operations,
                flag_block_by_time=result.block_by_time,
                flag_overtrading=result.block_overtrading,
            )
            .with_metrics(
                max_daily_profit=result.max_daily_profit,
                current_drawdown=result.current_drawdown,
            )
            .with_overtrading(
                active=result.block_overtrading,
                triggered=result.overtrading_triggered,
                reason=result.overtrading_reason,
            )
        )
