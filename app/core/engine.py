"""Trading engine — executes orders and manages SL/TP.

Requires an MT5Broker to interact with the platform.
Pure orchestration — all decisions are delegated to RiskManager.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.core.trade import Trade
from app.core.trade_state import TradeState
from app.core.risk import RiskManager

logger = logging.getLogger(__name__)


class TradingEngine:
    """Executes trades and manages position lifecycle.

    Delegates risk decisions to RiskManager.
    Uses Broker for MT5 communication.
    """

    def __init__(self, risk: RiskManager):
        self._risk = risk
        # broker injected later via set_broker()

    # ── SL / TP management ──────────────────────────────────

    def manage_sl_tp(
        self,
        trade: Trade,
        atr: float,
        previous_m15_low: Optional[float] = None,
        previous_m15_high: Optional[float] = None,
        trailing_buffer: float = 0.5,
    ) -> tuple[Trade, Optional[str]]:
        """Evaluate and apply SL/TP changes. Returns (updated_trade, close_reason_or_None).

        Order of evaluation:
        1. Lock-entry → move SL to entry price
        2. M15 trailing stop
        """
        close_reason: Optional[str] = None

        # 1. Lock-entry
        if (
            not trade.lock_entry_achieved
            and trade.state == TradeState.OPEN
            and self._risk.has_lock_entry(trade, atr)
        ):
            new_sl = trade.entry_price
            if trade.direction == "buy" and (trade.sl == 0.0 or new_sl > trade.sl):
                trade = trade.with_sl(new_sl).with_lock_entry_achieved(True)
                trade = trade.with_state(TradeState.LOCKED)
            elif trade.direction == "sell" and (trade.sl == 0.0 or new_sl < trade.sl):
                trade = trade.with_sl(new_sl).with_lock_entry_achieved(True)
                trade = trade.with_state(TradeState.LOCKED)

        # 2. M15 trailing stop (only after lock-entry)
        if trade.state in (TradeState.LOCKED, TradeState.TRAILING):
            if trade.direction == "buy" and previous_m15_low is not None:
                trigger = round(previous_m15_low - trailing_buffer, 2)
                if trade.current_price <= trigger:
                    close_reason = "trailing_prev_m15_low"
            elif trade.direction == "sell" and previous_m15_high is not None:
                trigger = round(previous_m15_high + trailing_buffer, 2)
                if trade.current_price >= trigger:
                    close_reason = "trailing_prev_m15_high"

        return trade, close_reason

    # ── max-profit trail ────────────────────────────────────

    def should_trail_close(self, trade: Trade) -> bool:
        """Check max-profit trailing stop after lock-entry."""
        pct = self._risk._limits.max_profit_retention_pct
        return trade.should_trail_close(pct)
