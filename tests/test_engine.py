"""Tests for TradingEngine — SL/TP management orchestration."""
from __future__ import annotations

from app.core.engine import TradingEngine
from app.core.risk import RiskManager, RiskLimits
from app.core.trade import Trade
from app.core.trade_state import TradeState


def _trade(**kw) -> Trade:
    defaults = dict(
        ticket=1, symbol="XAUUSD", direction="buy",
        volume=0.1, entry_price=100.0, current_price=100.0,
        profit=0.0, sl=0.0, tp=0.0, state=TradeState.OPEN,
        max_profit_seen=0.0, lock_entry_achieved=False,
    )
    defaults.update(kw)
    return Trade(**defaults)


class TestTradingEngineLockEntry:
    def test_lock_entry_triggered_buy(self):
        limits = RiskLimits(lock_entry=0.5)
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="buy", entry_price=100.0, current_price=102.0,
            sl=90.0,
        )
        updated, reason = engine.manage_sl_tp(trade, atr=4.0)
        # distance = 2.0, threshold = 0.5 * 4.0 = 2.0 → triggered
        assert updated.lock_entry_achieved is True
        assert updated.sl == 100.0  # moved to entry
        assert updated.state == TradeState.LOCKED
        assert reason is None

    def test_lock_entry_not_triggered_insufficient_distance(self):
        limits = RiskLimits(lock_entry=0.5)
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="buy", entry_price=100.0, current_price=101.0,
        )
        updated, reason = engine.manage_sl_tp(trade, atr=4.0)
        # distance = 1.0, threshold = 2.0 → not triggered
        assert updated.lock_entry_achieved is False
        assert updated.state == TradeState.OPEN

    def test_lock_entry_already_achieved_skips(self):
        limits = RiskLimits(lock_entry=0.5)
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="buy", entry_price=100.0, current_price=105.0,
            lock_entry_achieved=True, sl=100.0,
        )
        updated, reason = engine.manage_sl_tp(trade, atr=4.0)
        # Already locked, should not re-lock
        assert updated.sl == 100.0
        assert updated.lock_entry_achieved is True

    def test_lock_entry_sell_triggered(self):
        limits = RiskLimits(lock_entry=0.5)
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="sell", entry_price=100.0, current_price=98.0,
            sl=110.0,
        )
        updated, reason = engine.manage_sl_tp(trade, atr=4.0)
        # distance = 2.0, threshold = 2.0 → triggered
        assert updated.lock_entry_achieved is True
        assert updated.sl == 100.0
        assert updated.state == TradeState.LOCKED

    def test_lock_entry_sell_sl_already_better(self):
        limits = RiskLimits(lock_entry=0.5)
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="sell", entry_price=100.0, current_price=98.0,
            sl=95.0,  # already better than entry
        )
        updated, reason = engine.manage_sl_tp(trade, atr=4.0)
        # new_sl (100) > current sl (95) for sell → NOT an improvement
        assert updated.sl == 95.0  # unchanged


class TestTradingEngineTrailingStop:
    def test_m15_trailing_buy_hit(self):
        limits = RiskLimits()
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="buy", current_price=99.0,
            state=TradeState.LOCKED, lock_entry_achieved=True,
        )
        updated, reason = engine.manage_sl_tp(
            trade, atr=4.0,
            previous_m15_low=99.5, trailing_buffer=0.5,
        )
        # trigger = 99.5 - 0.5 = 99.0, current = 99.0 → triggered
        assert reason == "trailing_prev_m15_low"

    def test_m15_trailing_sell_hit(self):
        limits = RiskLimits()
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="sell", current_price=101.0,
            state=TradeState.LOCKED, lock_entry_achieved=True,
        )
        updated, reason = engine.manage_sl_tp(
            trade, atr=4.0,
            previous_m15_high=100.5, trailing_buffer=0.5,
        )
        # trigger = 100.5 + 0.5 = 101.0, current = 101.0 → triggered
        assert reason == "trailing_prev_m15_high"

    def test_m15_trailing_not_hit(self):
        limits = RiskLimits()
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="buy", current_price=100.0,
            state=TradeState.LOCKED, lock_entry_achieved=True,
        )
        updated, reason = engine.manage_sl_tp(
            trade, atr=4.0,
            previous_m15_low=100.0, trailing_buffer=0.5,
        )
        # trigger = 100.0 - 0.5 = 99.5, current = 100.0 → not hit
        assert reason is None

    def test_no_trailing_without_lock_entry(self):
        limits = RiskLimits()
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            direction="buy", current_price=98.0,
            state=TradeState.OPEN, lock_entry_achieved=False,
        )
        updated, reason = engine.manage_sl_tp(
            trade, atr=4.0,
            previous_m15_low=99.0, trailing_buffer=0.5,
        )
        # Not LOCKED or TRAILING → no trailing
        assert reason is None


class TestTradingEngineMaxProfit:
    def test_should_trail_close_true(self):
        limits = RiskLimits(max_profit_retention_pct=50)
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            profit=30.0, max_profit_seen=100.0, lock_entry_achieved=True,
        )
        # 30 < 100 * 0.5 = 50 → should close
        assert engine.should_trail_close(trade) is True

    def test_should_trail_close_false(self):
        limits = RiskLimits(max_profit_retention_pct=50)
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            profit=80.0, max_profit_seen=100.0, lock_entry_achieved=True,
        )
        # 80 >= 50 → keep
        assert engine.should_trail_close(trade) is False

    def test_should_trail_close_no_lock_entry(self):
        limits = RiskLimits(max_profit_retention_pct=50)
        risk = RiskManager(limits)
        engine = TradingEngine(risk)
        trade = _trade(
            profit=10.0, max_profit_seen=100.0, lock_entry_achieved=False,
        )
        # lock_entry_achieved=False → should_trail_close always False
        assert engine.should_trail_close(trade) is False
