from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from app.core.trade_state import TradeState


@dataclass(frozen=True)
class Trade:
    """Immutable representation of one MT5 position.

    All mutation methods return a NEW Trade instance.
    External code CANNOT modify a Trade directly — it must go
    through TradingEngine which validates state transitions.
    """

    ticket: int
    symbol: str
    direction: str               # "buy" | "sell"
    volume: float
    entry_price: float
    current_price: float
    profit: float
    sl: float
    tp: float
    state: TradeState
    max_profit_seen: float       # Monetary peak of this position
    lock_entry_achieved: bool
    opened_at: Optional[datetime] = None
    signal: Optional[dict] = None  # probabilidades registradas na abertura

    # ── price updates ──────────────────────────────────────

    def with_prices(self, current_price: float, profit: float) -> Trade:
        """Return a new Trade with fresh prices and updated peak."""
        new_max = max(self.max_profit_seen, profit)
        return replace(
            self,
            current_price=current_price,
            profit=profit,
            max_profit_seen=new_max,
        )

    # ── SL / TP ─────────────────────────────────────────────

    def with_sl_tp(self, sl: float, tp: float) -> Trade:
        return replace(self, sl=sl, tp=tp)

    def with_sl(self, new_sl: float) -> Trade:
        return replace(self, sl=new_sl)

    # ── state transitions ───────────────────────────────────

    def with_state(self, new_state: TradeState) -> Trade:
        if not self.state.can_transition_to(new_state):
            raise ValueError(
                f"Invalid transition: {self.state.value} → {new_state.value}"
            )
        return replace(self, state=new_state)

    def with_lock_entry_achieved(self, achieved: bool = True) -> Trade:
        return replace(self, lock_entry_achieved=achieved)

    # ── derived values ──────────────────────────────────────

    def distance_from_entry(self) -> float:
        """Price distance from entry (same unit as ATR)."""
        if self.direction == "buy":
            return round(self.current_price - self.entry_price, 5)
        return round(self.entry_price - self.current_price, 5)

    def should_trail_close(self, retention_pct: float) -> bool:
        """True when profit fell below retention-% of peak (post lock-entry)."""
        if not self.lock_entry_achieved:
            return False
        if self.max_profit_seen <= 0:
            return False
        if retention_pct <= 0:
            return False
        return self.profit < (self.max_profit_seen * retention_pct / 100.0)

    # ── serialization ───────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction,
            "volume": self.volume,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "profit": self.profit,
            "sl": self.sl,
            "tp": self.tp,
            "state": self.state.value,
            "max_profit_seen": self.max_profit_seen,
            "lock_entry_achieved": self.lock_entry_achieved,
            "distance_from_entry": self.distance_from_entry(),
            "signal": self.signal or None,
        }
