"""Unit tests for messenger message builders (no network calls)."""
from __future__ import annotations

from app.core.messenger import Messenger


def _config() -> dict:
    return {
        "SYMBOL": "XAUUSDC",
        "TIMEFRAME": "TIMEFRAME_M15",
        "LOT_SIZE": 0.2,
        "TP_MULTIPLIER": 2.5,
        "SL_MULTIPLIER": 1,
        "MAX_PROFIT_PRICE": 10.0,
        "MAX_LOSS_PRICE": 7.0,
        "TRADING_WINDOW_1_START": "01:00",
        "TRADING_WINDOW_1_DURATION_HOURS": 10,
        "TRADING_WINDOW_2_START": "15:00",
        "TRADING_WINDOW_2_DURATION_HOURS": 5,
        "LIMITE_LUCRO_DIARIO": 500,
        "LIMITE_PERDA_DIARIO": -300,
        "LIMITE_OPERACOES": 40,
        "LOCK_ENTRY": 0.7,
        "TRAILING_STOP_BUFFER": 1,
    }


class TestNotifyNewDay:
    def test_skips_send_when_disabled(self):
        m = Messenger(bot_token="", chat_id="", enabled=False)
        assert m.notify_new_day(_config(), "últimos 500 dias (350 úteis)",
                                "2026-08-25") is False

    def test_returns_false_without_credentials(self):
        m = Messenger(enabled=True, bot_token="", chat_id="")
        assert m.notify_new_day(_config(), "") is False


class TestNotifyStartup:
    def test_skips_send_when_disabled(self):
        m = Messenger(enabled=False)
        assert m.notify_startup({"SYMBOL": "XAUUSDC"}) is None
