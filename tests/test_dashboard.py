"""Tests for Dashboard — data aggregation for templates."""
from __future__ import annotations

from app.core.session import TradingSession
from app.core.trade import Trade
from app.core.trade_state import TradeState
from app.web.dashboard import Dashboard


def _trade(**kw) -> Trade:
    defaults = dict(
        ticket=1, symbol="XAUUSD", direction="buy",
        volume=0.1, entry_price=100.0, current_price=100.0,
        profit=0.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
        max_profit_seen=0.0, lock_entry_achieved=False,
    )
    defaults.update(kw)
    return Trade(**defaults)


class TestDashboard:
    def test_get_state_basic(self):
        session = TradingSession(day_key="2026-07-31", daily_profit=150.0)
        state = Dashboard.get_state(session)
        assert state["day_key"] == "2026-07-31"
        assert state["daily_profit"] == 150.0
        assert state["open_trade"] is None
        assert "blocks_active" in state
        assert "flags" in state

    def test_get_state_with_trade(self):
        trade = _trade(ticket=42, direction="sell", profit=25.0, lock_entry_achieved=True)
        session = TradingSession(day_key="2026-07-31", open_trade=trade)
        state = Dashboard.get_state(session)
        assert state["open_trade"]["ticket"] == 42
        assert state["open_trade"]["direction"] == "sell"
        assert state["open_trade"]["lock_entry_achieved"] is True

    def test_get_state_with_challenge_none(self):
        session = TradingSession(day_key="2026-07-31")
        state = Dashboard.get_state(session, challenge=None)
        assert "challenge" not in state

    def test_get_state_blocks_active(self):
        session = TradingSession(
            day_key="2026-07-31",
            flag_block_by_profit=True,
            flag_block_by_time=True,
        )
        state = Dashboard.get_state(session)
        assert len(state["blocks_active"]) == 2
        assert "Limite de lucro diario" in state["blocks_active"]
