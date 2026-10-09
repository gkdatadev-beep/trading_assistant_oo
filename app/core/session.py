from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from app.core.trade import Trade


@dataclass(frozen=True)
class TradingSession:
    """Daily trading state — immutable, updates return new instances."""

    day_key: str                        # "YYYY-MM-DD"

    # Latched block flags (reset only at midnight)
    flag_block_by_profit: bool = False
    flag_block_by_loss: bool = False
    flag_block_by_operations: bool = False

    # Non-latched flags (re-evaluated every tick)
    flag_block_by_time: bool = False
    flag_overtrading: bool = False

    # Daily metrics
    daily_profit: float = 0.0
    max_daily_profit: float = 0.0
    current_drawdown: float = 0.0
    operations_count: int = 0
    operations_closed: int = 0

    # Active trade
    open_trade: Optional[Trade] = None

    # Overtrading protection
    overtrading_triggered: bool = False
    overtrading_reason: str = ""
    overtrading_protection_end: Optional[datetime] = None
    overtrading_streak_seen: int = 0   # streak que já travou os trades
    consecutive_losses: int = 0        # perdas consecutivas mais recentes

    # ── day lifecycle ───────────────────────────────────────

    @staticmethod
    def for_today(server_now: datetime) -> TradingSession:
        return TradingSession(day_key=server_now.strftime("%Y-%m-%d"))

    def check_day_change(self, server_now: datetime) -> bool:
        """True when server_now is a different day."""
        return server_now.strftime("%Y-%m-%d") != self.day_key

    def for_new_day(self, server_now: datetime) -> TradingSession:
        """Fresh session — all flags and metrics reset."""
        return TradingSession(day_key=server_now.strftime("%Y-%m-%d"))

    # ── flag updates ────────────────────────────────────────

    def with_flags(self, **flags: bool) -> TradingSession:
        return replace(self, **flags)

    def with_overtrading(
        self,
        active: bool,
        triggered: bool = False,
        reason: str = "",
        protection_end: Optional[datetime] = None,
        streak_seen: Optional[int] = None,
    ) -> TradingSession:
        return replace(
            self,
            flag_overtrading=active,
            overtrading_triggered=triggered,
            overtrading_reason=reason,
            overtrading_protection_end=protection_end,
            overtrading_streak_seen=(
                self.overtrading_streak_seen if streak_seen is None else streak_seen
            ),
        )

    # ── metric updates ──────────────────────────────────────

    def with_metrics(
        self,
        daily_profit: Optional[float] = None,
        max_daily_profit: Optional[float] = None,
        current_drawdown: Optional[float] = None,
        operations_count: Optional[int] = None,
        operations_closed: Optional[int] = None,
        consecutive_losses: Optional[int] = None,
    ) -> TradingSession:
        kwargs: dict = {}
        if daily_profit is not None:
            kwargs["daily_profit"] = daily_profit
        if max_daily_profit is not None:
            kwargs["max_daily_profit"] = max_daily_profit
        if current_drawdown is not None:
            kwargs["current_drawdown"] = current_drawdown
        if operations_count is not None:
            kwargs["operations_count"] = operations_count
        if operations_closed is not None:
            kwargs["operations_closed"] = operations_closed
        if consecutive_losses is not None:
            kwargs["consecutive_losses"] = consecutive_losses
        return replace(self, **kwargs) if kwargs else self

    # ── trade management ────────────────────────────────────

    def with_open_trade(self, trade: Optional[Trade]) -> TradingSession:
        return replace(self, open_trade=trade)

    # ── queries ─────────────────────────────────────────────

    def all_blocks_active(self) -> list[str]:
        blocks: list[str] = []
        if self.flag_block_by_profit:
            blocks.append("Limite de lucro diario")
        if self.flag_block_by_loss:
            blocks.append("Drawdown diario")
        if self.flag_block_by_operations:
            blocks.append("Limite de operacoes")
        if self.flag_block_by_time:
            blocks.append("Fora da janela de trading")
        if self.flag_overtrading:
            blocks.append("Overtrading protection")
        return blocks

    def is_any_block_active(self) -> bool:
        return any([
            self.flag_block_by_profit,
            self.flag_block_by_loss,
            self.flag_block_by_operations,
            self.flag_block_by_time,
            self.flag_overtrading,
        ])

    # ── serialization ───────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "day_key": self.day_key,
            "flags": {
                "block_by_profit": self.flag_block_by_profit,
                "block_by_loss": self.flag_block_by_loss,
                "block_by_operations": self.flag_block_by_operations,
                "block_by_time": self.flag_block_by_time,
                "overtrading": self.flag_overtrading,
            },
            "daily_profit": self.daily_profit,
            "max_daily_profit": self.max_daily_profit,
            "current_drawdown": self.current_drawdown,
            "operations_count": self.operations_count,
            "operations_closed": self.operations_closed,
            "overtrading_triggered": self.overtrading_triggered,
            "overtrading_reason": self.overtrading_reason,
            "overtrading_protection_end": (
                self.overtrading_protection_end.isoformat()
                if self.overtrading_protection_end else None
            ),
            "overtrading_streak_seen": self.overtrading_streak_seen,
            "consecutive_losses": self.consecutive_losses,
            "open_trade": self.open_trade.to_dict() if self.open_trade else None,
            "blocks_active": self.all_blocks_active(),
        }
