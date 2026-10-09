from datetime import datetime

from app.core.session import TradingSession
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


class TestSessionDayChange:
    def test_new_session_defaults(self):
        s = TradingSession(day_key="2026-07-30")
        assert s.day_key == "2026-07-30"
        assert not s.is_any_block_active()

    def test_check_day_change_true(self):
        s = TradingSession(day_key="2026-07-29")
        assert s.check_day_change(datetime(2026, 7, 30, 0, 1, 0))

    def test_check_day_change_false(self):
        s = TradingSession(day_key="2026-07-30")
        assert not s.check_day_change(datetime(2026, 7, 30, 12, 0, 0))

    def test_for_new_day_resets_all(self):
        s = TradingSession(
            day_key="2026-07-29",
            flag_block_by_profit=True,
            flag_overtrading=True,
            daily_profit=500.0,
            operations_count=5,
        )
        new_s = s.for_new_day(datetime(2026, 7, 30, 0, 5, 0))
        assert new_s.day_key == "2026-07-30"
        assert not new_s.flag_block_by_profit
        assert not new_s.flag_overtrading
        assert new_s.daily_profit == 0.0
        assert new_s.operations_count == 0

    def test_for_today(self):
        s = TradingSession.for_today(datetime(2026, 7, 30, 14, 0, 0))
        assert s.day_key == "2026-07-30"


class TestSessionFlags:
    def test_with_flags_immutable(self):
        s1 = TradingSession(day_key="2026-07-30")
        s2 = s1.with_flags(flag_block_by_profit=True)
        assert not s1.flag_block_by_profit
        assert s2.flag_block_by_profit

    def test_all_blocks_active(self):
        s = TradingSession(
            day_key="2026-07-30",
            flag_block_by_profit=True,
            flag_block_by_time=True,
        )
        blocks = s.all_blocks_active()
        assert "Limite de lucro diario" in blocks
        assert "Fora da janela de trading" in blocks
        assert len(blocks) == 2

    def test_is_any_block_active(self):
        s = TradingSession(day_key="2026-07-30")
        assert not s.is_any_block_active()
        s2 = s.with_flags(flag_block_by_time=True)
        assert s2.is_any_block_active()

    def test_with_overtrading(self):
        s = TradingSession(day_key="2026-07-30")
        s2 = s.with_overtrading(
            active=True, triggered=True,
            reason="Duas perdas", protection_end=datetime(2026, 7, 30, 14, 0),
        )
        assert s2.flag_overtrading
        assert s2.overtrading_triggered
        assert s2.overtrading_reason == "Duas perdas"

    def test_with_overtrading_streak_seen(self):
        s = TradingSession(day_key="2026-07-30")
        s2 = s.with_overtrading(
            active=True, triggered=True,
            reason="2 perdas consecutivas",
            protection_end=datetime(2026, 7, 30, 14, 0),
            streak_seen=2,
        )
        assert s2.overtrading_streak_seen == 2
        # release mantém o streak visto (não re-trava na mesma sequência)
        s3 = s2.with_overtrading(active=False, triggered=False, reason="")
        assert not s3.flag_overtrading
        assert s3.overtrading_streak_seen == 2


class TestSessionMetrics:
    def test_with_metrics_partial(self):
        s = TradingSession(
            day_key="2026-07-30", daily_profit=100.0, max_daily_profit=150.0,
        )
        s2 = s.with_metrics(daily_profit=80.0)
        assert s2.daily_profit == 80.0
        assert s2.max_daily_profit == 150.0

    def test_open_trade_lifecycle(self):
        s = TradingSession(day_key="2026-07-30")
        trade = _trade(ticket=1, direction="sell")
        s2 = s.with_open_trade(trade)
        assert s.open_trade is None
        assert s2.open_trade is not None
        assert s2.open_trade.ticket == 1
        s3 = s2.with_open_trade(None)
        assert s3.open_trade is None


class TestSessionSerialization:
    def test_to_dict_no_trade(self):
        s = TradingSession(day_key="2026-07-30", daily_profit=50.0)
        d = s.to_dict()
        assert d["day_key"] == "2026-07-30"
        assert d["daily_profit"] == 50.0
        assert d["open_trade"] is None
        assert d["blocks_active"] == []

    def test_to_dict_with_trade(self):
        trade = _trade(ticket=1, direction="sell", profit=20.0,
                       sl=2652.0, tp=2640.0, lock_entry_achieved=True)
        s = TradingSession(day_key="2026-07-30", open_trade=trade)
        d = s.to_dict()
        assert d["open_trade"]["ticket"] == 1
        assert d["open_trade"]["state"] == "open"

    def test_to_dict_overtrading_fields(self):
        s = TradingSession(
            day_key="2026-07-30",
            flag_overtrading=True,
            overtrading_triggered=True,
            overtrading_reason="2 perdas consecutivas",
            overtrading_protection_end=datetime(2026, 7, 30, 14, 0),
            overtrading_streak_seen=2,
            consecutive_losses=2,
        )
        d = s.to_dict()
        assert d["overtrading_triggered"] is True
        assert d["overtrading_reason"] == "2 perdas consecutivas"
        assert d["overtrading_protection_end"] == "2026-07-30T14:00:00"
        assert d["overtrading_streak_seen"] == 2
        assert d["consecutive_losses"] == 2

    def test_with_metrics_consecutive_losses(self):
        s = TradingSession(day_key="2026-07-30")
        s2 = s.with_metrics(consecutive_losses=3)
        assert s2.consecutive_losses == 3
        assert s.consecutive_losses == 0
