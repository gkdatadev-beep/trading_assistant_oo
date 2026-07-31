import pytest
from datetime import datetime

from app.core.trade import Trade
from app.core.trade_state import TradeState


class TestTradeCreation:
    def test_created_with_correct_fields(self):
        t = Trade(
            ticket=12345, symbol="XAUUSD", direction="buy",
            volume=0.15, entry_price=2650.0, current_price=2650.0,
            profit=0.0, sl=0.0, tp=0.0, state=TradeState.OPENING,
            max_profit_seen=0.0, lock_entry_achieved=False,
            opened_at=datetime(2026, 7, 30, 12, 0, 0),
        )
        assert t.ticket == 12345
        assert t.direction == "buy"
        assert t.state == TradeState.OPENING

    def test_is_immutable(self):
        t = Trade(
            ticket=1, symbol="XAUUSD", direction="buy",
            volume=0.1, entry_price=100.0, current_price=100.0,
            profit=0.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
            max_profit_seen=0.0, lock_entry_achieved=False,
        )
        with pytest.raises(Exception):
            t.profit = 10.0  # frozen dataclass


class TestTradePrices:
    def test_with_prices_returns_new_instance(self):
        t = Trade(
            ticket=1, symbol="XAUUSD", direction="buy",
            volume=0.1, entry_price=100.0, current_price=100.0,
            profit=0.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
            max_profit_seen=0.0, lock_entry_achieved=False,
        )
        t2 = t.with_prices(101.0, 10.0)
        assert t2 is not t
        assert t2.current_price == 101.0
        assert t2.profit == 10.0
        assert t2.max_profit_seen == 10.0
        assert t.current_price == 100.0  # unchanged

    def test_max_profit_preserves_peak(self):
        t = Trade(
            ticket=1, symbol="XAUUSD", direction="buy",
            volume=0.1, entry_price=100.0, current_price=100.0,
            profit=0.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
            max_profit_seen=15.0, lock_entry_achieved=False,
        )
        t2 = t.with_prices(101.0, 5.0)
        assert t2.max_profit_seen == 15.0

    def test_distance_from_entry_buy(self):
        t = Trade(
            ticket=1, symbol="XAUUSD", direction="buy",
            volume=0.1, entry_price=2650.0, current_price=2652.5,
            profit=25.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
            max_profit_seen=0.0, lock_entry_achieved=False,
        )
        assert t.distance_from_entry() == 2.5

    def test_distance_from_entry_sell(self):
        t = Trade(
            ticket=1, symbol="XAUUSD", direction="sell",
            volume=0.1, entry_price=2650.0, current_price=2647.5,
            profit=25.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
            max_profit_seen=0.0, lock_entry_achieved=False,
        )
        assert t.distance_from_entry() == 2.5


class TestTradeStateTransitions:
    def test_valid_forward(self):
        assert TradeState.OPENING.can_transition_to(TradeState.OPEN)
        assert TradeState.OPEN.can_transition_to(TradeState.LOCKED)
        assert TradeState.LOCKED.can_transition_to(TradeState.TRAILING)
        assert TradeState.TRAILING.can_transition_to(TradeState.CLOSING)
        assert TradeState.CLOSING.can_transition_to(TradeState.CLOSED)

    def test_invalid_backwards(self):
        assert not TradeState.LOCKED.can_transition_to(TradeState.OPEN)
        assert not TradeState.CLOSED.can_transition_to(TradeState.OPENING)

    def test_with_state_valid(self):
        t = Trade(
            ticket=1, symbol="XAUUSD", direction="buy",
            volume=0.1, entry_price=100.0, current_price=100.0,
            profit=0.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
            max_profit_seen=0.0, lock_entry_achieved=False,
        )
        t2 = t.with_state(TradeState.LOCKED)
        assert t2.state == TradeState.LOCKED

    def test_with_state_invalid_raises(self):
        t = Trade(
            ticket=1, symbol="XAUUSD", direction="buy",
            volume=0.1, entry_price=100.0, current_price=100.0,
            profit=0.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
            max_profit_seen=0.0, lock_entry_achieved=False,
        )
        with pytest.raises(ValueError, match="Invalid transition"):
            t.with_state(TradeState.OPENING)


class TestTrailClose:
    def _trade(self, **kw) -> Trade:
        defaults = dict(
            ticket=1, symbol="XAUUSD", direction="buy",
            volume=0.1, entry_price=100.0, current_price=100.0,
            profit=0.0, sl=0.0, tp=0.0, state=TradeState.LOCKED,
            max_profit_seen=0.0, lock_entry_achieved=False,
        )
        defaults.update(kw)
        return Trade(**defaults)

    def test_no_close_without_lock_entry(self):
        t = self._trade(profit=2.0, max_profit_seen=10.0)
        assert not t.should_trail_close(50)

    def test_close_when_below_retention(self):
        t = self._trade(
            profit=3.0, max_profit_seen=10.0, lock_entry_achieved=True,
        )
        assert t.should_trail_close(50)

    def test_no_close_when_above_retention(self):
        t = self._trade(
            profit=7.0, max_profit_seen=10.0, lock_entry_achieved=True,
        )
        assert not t.should_trail_close(50)

    def test_retention_zero_disables(self):
        t = self._trade(
            profit=1.0, max_profit_seen=10.0, lock_entry_achieved=True,
        )
        assert not t.should_trail_close(0)

    def test_negative_peak_ignored(self):
        t = self._trade(
            profit=-5.0, max_profit_seen=-5.0, lock_entry_achieved=True,
        )
        assert not t.should_trail_close(50)
