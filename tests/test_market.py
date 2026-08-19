from datetime import datetime

from app.core.market import MarketAnalyzer, TradingWindow


class TestTradingWindow:
    def test_contains_inside(self):
        w = TradingWindow(60, 360)  # 01:00-06:00
        assert w.contains(150)       # 02:30

    def test_contains_boundary_start(self):
        w = TradingWindow(60, 360)
        assert w.contains(60)        # 01:00 exactly

    def test_contains_boundary_end_excluded(self):
        w = TradingWindow(60, 360)
        assert not w.contains(360)   # 06:00 excluded

    def test_contains_outside(self):
        w = TradingWindow(60, 360)
        assert not w.contains(500)   # 08:20

    def test_wraps_midnight(self):
        w = TradingWindow(1380, 120)  # 23:00-02:00
        assert w.contains(0)          # 00:00
        assert w.contains(60)         # 01:00
        assert w.contains(1430)       # 23:50
        assert not w.contains(500)    # 08:20


class TestIsOutsideTradingHours:
    def test_inside_default_window1(self):
        ma = MarketAnalyzer()
        server = datetime(2026, 7, 30, 2, 30, 0)  # UTC
        assert not ma.is_outside_trading_hours(server)

    def test_inside_default_window2(self):
        ma = MarketAnalyzer()
        server = datetime(2026, 7, 30, 17, 0, 0)
        assert not ma.is_outside_trading_hours(server)

    def test_outside_between_windows(self):
        ma = MarketAnalyzer()
        server = datetime(2026, 7, 30, 12, 0, 0)  # UTC noon
        assert ma.is_outside_trading_hours(server)

    def test_outside_after_window2(self):
        ma = MarketAnalyzer()
        server = datetime(2026, 7, 30, 21, 0, 0)
        assert ma.is_outside_trading_hours(server)

    def test_custom_windows(self):
        ma = MarketAnalyzer(
            window1_start="08:00", window1_duration_hours=4,
            window2_start="20:00", window2_duration_hours=2,
        )
        assert not ma.is_outside_trading_hours(datetime(2026, 7, 30, 9, 0))
        assert ma.is_outside_trading_hours(datetime(2026, 7, 30, 14, 0))


class TestBuildDayBounds:
    def test_midnight_start(self):
        server = datetime(2026, 7, 30, 14, 30, 0)
        start, end = MarketAnalyzer.build_day_bounds(server)
        assert start == datetime(2026, 7, 30, 0, 0, 0)
        assert end == datetime(2026, 7, 30, 23, 59, 59)


class TestFormatFriendly:
    def test_format(self):
        server = datetime(2026, 7, 30, 14, 0, 0)
        result = MarketAnalyzer.format_friendly(server)
        assert "Qui" in result   # 30 Jul 2026 is a Thursday
        assert "30 Jul" in result
        assert "14:00" in result


class TestNextMidnight:
    def test_next_midnight_midday(self):
        server = datetime(2026, 7, 30, 14, 30, 0)
        result = MarketAnalyzer.next_midnight(server)
        assert result == datetime(2026, 7, 31, 0, 0, 0)

    def test_next_midnight_late_night(self):
        server = datetime(2026, 7, 30, 23, 59, 59)
        result = MarketAnalyzer.next_midnight(server)
        assert result == datetime(2026, 7, 31, 0, 0, 0)

    def test_next_midnight_exactly_midnight(self):
        """At exactly midnight, next midnight is 24h away (next calendar day)."""
        server = datetime(2026, 7, 31, 0, 0, 0)
        result = MarketAnalyzer.next_midnight(server)
        assert result == datetime(2026, 8, 1, 0, 0, 0)

    def test_countdown_hhmm(self):
        server = datetime(2026, 7, 30, 22, 30, 0)
        result = MarketAnalyzer.countdown_hhmm(server)
        assert result == "01:30"  # 1h30min until midnight

    def test_countdown_hhmm_near_midnight(self):
        server = datetime(2026, 7, 30, 23, 59, 30)
        result = MarketAnalyzer.countdown_hhmm(server)
        assert result == "00:00"  # rounded down

    def test_countdown_hhmm_at_midnight(self):
        server = datetime(2026, 7, 31, 0, 0, 0)
        result = MarketAnalyzer.countdown_hhmm(server)
        assert result == "00:00"

    def test_block_release_is_midnight(self):
        """Simulate: if blocked at 14:30, release is at midnight same day."""
        from app.core.session import TradingSession
        s = TradingSession(day_key="2026-07-30", flag_block_by_profit=True)
        # Day change check at midnight next day
        assert s.check_day_change(datetime(2026, 7, 31, 0, 0, 1))
        new_s = s.for_new_day(datetime(2026, 7, 31, 0, 0, 1))
        assert not new_s.flag_block_by_profit
        assert new_s.day_key == "2026-07-31"
