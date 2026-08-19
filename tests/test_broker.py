"""Tests for MT5Broker using mock MT5 data."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from app.core.broker import MT5Broker


class TestBrokerLifecycle:
    def test_initialize_with_mock(self, mt5_mock):
        broker = MT5Broker()
        assert broker.initialize() is True
        assert broker.connected is True

    def test_shutdown(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        broker.shutdown()
        assert broker.connected is False

    def test_reconnect(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        broker.shutdown()
        assert broker.reconnect() is True
        assert broker.connected is True

    def test_double_initialize(self, mt5_mock):
        broker = MT5Broker()
        assert broker.initialize() is True
        assert broker.initialize() is True  # already connected → no-op

    def test_no_connection(self, mt5_mock_no_connection):
        broker = MT5Broker()
        assert broker.initialize() is False
        assert broker.connected is False


class TestBrokerLogin:
    """Auto-login: credentials are applied and the resulting
    connection is verified at the end of the login flow."""

    @staticmethod
    def _mock():
        return sys.modules["MetaTrader5"]

    def test_no_login_when_no_credentials(self, mt5_mock):
        broker = MT5Broker()
        assert broker.initialize() is True
        assert self._mock()._data.get("login_calls", []) == []
        assert broker.connected is True

    def test_login_called_and_verified_when_logged_out(self, mt5_mock):
        broker = MT5Broker(login="12345678", password="pw", server="Broker-Real")
        assert broker.initialize() is True
        calls = self._mock()._data.get("login_calls", [])
        assert len(calls) == 1
        assert calls[0]["login"] == 12345678
        assert calls[0]["password"] == "pw"
        assert calls[0]["server"] == "Broker-Real"
        # Connection verified at the end of the login: account_info confirms login
        ai = self._mock()._data.get("account_info")
        assert ai is not None
        assert ai["login"] == 12345678
        assert broker.last_error == ""

    def test_login_not_repeated_when_already_logged_in(self, mt5_mock):
        mock = self._mock()
        mock._data["account_info"] = {"login": 12345678, "server": "Broker-Real"}
        broker = MT5Broker(login="12345678", password="pw", server="Broker-Real")
        assert broker.initialize() is True
        assert mock._data.get("login_calls", []) == []

    def test_login_rejection_is_reported(self, mt5_mock):
        self._mock()._data["login_result"] = False
        broker = MT5Broker(login="12345678", password="wrong", server="Broker-Real")
        # Terminal-level connection still succeeds
        assert broker.initialize() is True
        assert broker.connected is True
        assert "login" in broker.last_error.lower()

    def test_login_ok_but_unverified_is_failure(self, mt5_mock):
        mock = self._mock()

        def fake_login(*a, **kw):
            # Simulate mt5.login returning True but account state not changing
            mock._data["login_calls"] = mock._data.get("login_calls", []) + [kw]
            return True

        mock.login = fake_login
        broker = MT5Broker(login="12345678", password="pw", server="Broker-Real")
        assert broker.initialize() is True
        # login() was attempted once, but account_info does not confirm the
        # target account -> the login flow must be treated as failed
        assert len(mock._data.get("login_calls", [])) == 1
        assert "account_info" in broker.last_error


class TestBrokerTime:
    def test_get_server_time(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        t = broker.get_server_time()
        assert isinstance(t, datetime)
        assert t.tzinfo == timezone.utc

    def test_get_broker_local_time(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        t = broker.get_broker_local_time()
        assert isinstance(t, datetime)
        # Now returns server time (UTC), same as get_server_time()
        assert t.tzinfo == timezone.utc


class TestBrokerMarketData:
    def test_get_rates(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        df = broker.get_rates(count=10)
        assert df is not None
        assert len(df) == 10
        assert "open" in df.columns
        assert "high" in df.columns
        assert "low" in df.columns
        assert "close" in df.columns

    def test_get_rates_range(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        now = broker.get_server_time()
        from_dt = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        to_dt = now
        df = broker.get_rates_range(from_dt, to_dt, timeframe=5)
        assert df is not None
        assert len(df) > 0

    def test_get_current_candle(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        candle = broker.get_current_candle()
        assert candle is not None
        assert "open" in candle
        assert "high" in candle
        assert "low" in candle
        assert "close" in candle
        assert isinstance(candle["time"], datetime)


class TestBrokerPositions:
    def test_get_positions_empty(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        positions = broker.get_positions()
        assert positions == []

    def test_get_positions_with_open(self, mt5_mock_with_position):
        broker = MT5Broker()
        broker.initialize()
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticket"] == 12345
        assert positions[0]["type"] == 0  # buy
        assert positions[0]["profit"] == 75.0

    def test_positions_total_empty(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        assert broker.positions_total() == 0

    def test_positions_total_with_open(self, mt5_mock_with_position):
        broker = MT5Broker()
        broker.initialize()
        assert broker.positions_total() == 1


class TestBrokerClosedTradesRange:
    def _deals_fixture(self):
        t0 = 1753992000  # 2026-07-31 12:00 UTC
        return [
            {"ticket": 1111, "order": 1110, "time": t0, "type": 0, "entry": 0,
             "position_id": 123, "volume": 0.15, "price": 4050.0, "profit": 0.0,
             "symbol": "XAUUSDC", "comment": ""},
            {"ticket": 2222, "order": 2221, "time": t0 + 3600, "type": 0, "entry": 1,
             "position_id": 123, "volume": 0.15, "price": 4060.0, "profit": 75.0,
             "symbol": "XAUUSDC", "comment": ""},
        ]

    def test_range_reconstructs_trade(self, mt5_mock):
        mock = sys.modules["MetaTrader5"]
        mock._data["history_deals_get"] = self._deals_fixture()
        mock._data["history_orders_get"] = [
            {"ticket": 3333, "type": 6, "position_id": 123,
             "sl": 4045.0, "tp": 4070.0},
        ]
        broker = MT5Broker()
        broker.initialize()
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 7, 31, tzinfo=timezone.utc)
        trades = broker.get_closed_trades_range(start, start + timedelta(days=1))
        assert len(trades) == 1
        t = trades[0]
        assert t["ticket"] == 123
        assert t["direction"] == "buy"
        assert t["entry_price"] == 4050.0
        assert t["exit_price"] == 4060.0
        assert t["profit"] == 75.0
        assert t["sl"] == 4045.0
        assert t["tp"] == 4070.0
        assert t["delta_price"] == 10.0

    def test_range_empty_without_deals(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 7, 31, tzinfo=timezone.utc)
        assert broker.get_closed_trades_range(start, start + timedelta(days=1)) == []

    def test_range_sltp_from_comment(self, mt5_mock):
        mock = sys.modules["MetaTrader5"]
        mock._data["history_deals_get"] = self._deals_fixture()
        # Some MT5 builds store modified SL/TP in price_open with a tag comment
        mock._data["history_orders_get"] = [
            {"ticket": 3333, "type": 1, "position_id": 123,
             "price_open": 4045.0, "comment": "[sl 4045.00]"},
            {"ticket": 3334, "type": 1, "position_id": 123,
             "price_open": 4070.0, "comment": "[tp 4070.00]"},
        ]
        broker = MT5Broker()
        broker.initialize()
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 7, 31, tzinfo=timezone.utc)
        trades = broker.get_closed_trades_range(start, start + timedelta(days=1))
        assert len(trades) == 1
        assert trades[0]["sl"] == 4045.0
        assert trades[0]["tp"] == 4070.0


class TestBrokerAccount:
    def test_get_account_profit(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        closed, open_pnl = broker.get_account_profit()
        assert isinstance(closed, float)
        assert isinstance(open_pnl, float)

    def test_get_closed_trades_today(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        trades = broker.get_closed_trades_today()
        assert isinstance(trades, list)


class TestBrokerTrading:
    def test_send_order(self, mt5_mock):
        broker = MT5Broker()
        broker.initialize()
        result = broker.send_order({"action": 1, "symbol": "XAUUSD"})
        assert result is not None
        assert result["retcode"] == 10009  # TRADE_RETCODE_DONE
        assert "order" in result
        assert "deal" in result
