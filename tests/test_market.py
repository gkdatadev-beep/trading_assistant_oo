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
