"""Integration tests for MT5Broker — requires live MT5 connection.

Run with:  pytest tests/test_broker_live.py -m live_mt5 -v
Skip with: pytest tests/                        (auto-skipped if MT5 not open)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.broker import MT5Broker

pytestmark = pytest.mark.live_mt5


class TestBrokerLiveConnection:
    def test_initialize_real_mt5(self, live_mt5):
        broker = MT5Broker()
        ok = broker.initialize()
        assert ok is True
        assert broker.connected is True

    def test_server_time_is_utc_aware(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        t = broker.get_server_time()
        assert isinstance(t, datetime)
        assert t.tzinfo is not None  # timezone-aware

    def test_broker_local_time_equals_server_time(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        utc = broker.get_server_time()
        local = broker.get_broker_local_time()
        # Both return the same MT5 server time (UTC)
        assert local == utc


class TestBrokerLiveMarketData:
    def test_get_rates_returns_data(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        df = broker.get_rates(count=10)
        assert df is not None
        assert len(df) >= 1
        for col in ["open", "high", "low", "close"]:
            assert col in df.columns

    def test_get_rates_m5(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        df = broker.get_rates(count=5, timeframe=5)
        assert df is not None
        assert len(df) == 5

    def test_get_rates_m15(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        df = broker.get_rates(count=5, timeframe=15)
        assert df is not None
        assert len(df) == 5

    def test_get_rates_range_today(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        now = broker.get_server_time()
        day_start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
        df = broker.get_rates_range(day_start, now, timeframe=5)
        assert df is not None
        assert len(df) >= 1

    def test_get_current_candle(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        candle = broker.get_current_candle()
        assert candle is not None
        for key in ["open", "high", "low", "close"]:
            assert key in candle
            assert isinstance(candle[key], float)


class TestBrokerLivePositions:
    def test_get_positions(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        positions = broker.get_positions()
        assert isinstance(positions, list)

    def test_positions_total(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        total = broker.positions_total()
        assert isinstance(total, int)
        assert total >= 0


class TestBrokerLiveAccount:
    def test_get_account_profit(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        closed, open_pnl = broker.get_account_profit()
        assert isinstance(closed, float)
        assert isinstance(open_pnl, float)

    def test_get_closed_trades_today(self, live_mt5):
        broker = MT5Broker()
        broker.initialize()
        trades = broker.get_closed_trades_today()
        assert isinstance(trades, list)
        if trades:
            assert "ticket" in trades[0]
            assert "profit" in trades[0]
            assert "time" in trades[0]
