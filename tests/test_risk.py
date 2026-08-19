from app.core.risk import RiskManager, RiskLimits, RiskResult
from app.core.trade import Trade
from app.core.trade_state import TradeState
from app.core.session import TradingSession


def _trade(**kw) -> Trade:
    defaults = dict(
        ticket=1, symbol="XAUUSD", direction="buy",
        volume=0.1, entry_price=100.0, current_price=101.0,
        profit=10.0, sl=99.0, tp=105.0, state=TradeState.OPEN,
        max_profit_seen=0.0, lock_entry_achieved=False,
    )
    defaults.update(kw)
    return Trade(**defaults)


class TestLockEntry:
    def test_has_lock_entry_true(self):
        rm = RiskManager(RiskLimits(lock_entry=0.5))
        trade = _trade(entry_price=100.0, current_price=101.0)  # distance = 1.0
        assert rm.has_lock_entry(trade, atr=2.0)  # 1.0 >= 0.5*2.0

    def test_has_lock_entry_false(self):
        rm = RiskManager(RiskLimits(lock_entry=0.5))
        trade = _trade(entry_price=100.0, current_price=100.4)  # distance = 0.4
        assert not rm.has_lock_entry(trade, atr=2.0)

    def test_has_lock_entry_sell(self):
        rm = RiskManager(RiskLimits(lock_entry=0.5))
        trade = _trade(direction="sell", entry_price=100.0, current_price=98.5)
        assert rm.has_lock_entry(trade, atr=2.0)


class TestPerTradeLimits:
    def test_profit_block(self):
        rm = RiskManager(RiskLimits(limite_lucro_op=50))
        trade = _trade(profit=60.0)
        assert rm.per_trade_blocked(trade) == "lucro_max_op"

    def test_loss_block(self):
        rm = RiskManager(RiskLimits(limite_perda_op=-100))
        trade = _trade(profit=-150.0)
        assert rm.per_trade_blocked(trade) == "perda_max_op"

    def test_no_block_within_limits(self):
        rm = RiskManager(RiskLimits(limite_lucro_op=100, limite_perda_op=-100))
        trade = _trade(profit=50.0)
        assert rm.per_trade_blocked(trade) is None


class TestDailyEvaluation:
    def test_profit_limit_latches(self):
        rm = RiskManager(RiskLimits(limite_lucro_diario=750))
        session = TradingSession(day_key="2026-07-30")
        result = rm.evaluate(
            session, daily_profit=800.0, operations_count=1,
            is_outside_window=False, overtrading_active=False,
            overtrading_triggered=False,
        )
        assert result.block_by_profit

    def test_profit_limit_stays_latched(self):
        rm = RiskManager(RiskLimits(limite_lucro_diario=750))
        session = TradingSession(
            day_key="2026-07-30", flag_block_by_profit=True,
        )
        result = rm.evaluate(
            session, daily_profit=100.0, operations_count=1,
            is_outside_window=False, overtrading_active=False,
            overtrading_triggered=False,
        )
        assert result.block_by_profit  # stays latched

    def test_operations_limit(self):
        rm = RiskManager(RiskLimits(limite_operacoes=5))
        session = TradingSession(day_key="2026-07-30")
        result = rm.evaluate(
            session, daily_profit=0.0, operations_count=6,
            is_outside_window=False, overtrading_active=False,
            overtrading_triggered=False,
        )
        assert result.block_by_operations

    def test_time_block(self):
        rm = RiskManager()
        session = TradingSession(day_key="2026-07-30")
        result = rm.evaluate(
            session, daily_profit=0.0, operations_count=0,
            is_outside_window=True, overtrading_active=False,
            overtrading_triggered=False,
        )
        assert result.block_by_time

    def test_overtrading_passthrough(self):
        rm = RiskManager()
        session = TradingSession(day_key="2026-07-30")
        result = rm.evaluate(
            session, daily_profit=0.0, operations_count=0,
            is_outside_window=False, overtrading_active=True,
            overtrading_triggered=True, overtrading_reason="Duas perdas",
        )
        assert result.block_overtrading
        assert result.overtrading_triggered
        assert result.overtrading_reason == "Duas perdas"

    def test_drawdown_tracking(self):
        rm = RiskManager(RiskLimits(limite_perda_diario=-350))
        session = TradingSession(
            day_key="2026-07-30", max_daily_profit=500.0,
        )
        result = rm.evaluate(
            session, daily_profit=100.0, operations_count=0,
            is_outside_window=False, overtrading_active=False,
            overtrading_triggered=False,
        )
        assert result.max_daily_profit == 500.0
        assert result.current_drawdown == 400.0  # 500 - 100

    def test_drawdown_latched_never_decreases(self):
        """Drawdown must be monotonic: once 400, profit recovery to 450
        should NOT reduce drawdown below 400."""
        rm = RiskManager(RiskLimits(limite_perda_diario=-350))
        # Simulate: we already had a 400 drawdown from previous tick
        session = TradingSession(
            day_key="2026-07-30",
            max_daily_profit=500.0,
            current_drawdown=400.0,   # previously latched
        )
        # Profit recovers from 100 → 450
        result = rm.evaluate(
            session, daily_profit=450.0, operations_count=0,
            is_outside_window=False, overtrading_active=False,
            overtrading_triggered=False,
        )
        assert result.max_daily_profit == 500.0
        # Instant = 500-450=50, but latched = max(400, 50) = 400
        assert result.current_drawdown == 400.0

    def test_drawdown_increases_when_worse(self):
        """Drawdown increases when profit drops further from peak."""
        rm = RiskManager(RiskLimits(limite_perda_diario=-350))
        session = TradingSession(
            day_key="2026-07-30",
            max_daily_profit=500.0,
            current_drawdown=400.0,
        )
        # Profit drops to -100
        result = rm.evaluate(
            session, daily_profit=-100.0, operations_count=0,
            is_outside_window=False, overtrading_active=False,
            overtrading_triggered=False,
        )
        assert result.max_daily_profit == 500.0
        # Instant = 500-(-100) = 600, latched = max(400, 600) = 600
        assert result.current_drawdown == 600.0


class TestApplyToSession:
    def test_flags_applied_to_new_session(self):
        rm = RiskManager()
        session = TradingSession(day_key="2026-07-30")
        result = RiskResult(
            block_by_profit=True, block_by_time=True,
            max_daily_profit=200.0, current_drawdown=50.0,
        )
        updated = rm.apply_to_session(session, result)
        assert updated.flag_block_by_profit
        assert updated.flag_block_by_time
        assert not updated.flag_block_by_loss
        assert updated.max_daily_profit == 200.0
