"""Tests for main.py order/close helpers — failure handling on block close."""
from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

from app import main as main_mod


class _Result(SimpleNamespace):
    pass


def _fake_mt5(retcode_send=10009, check_retcode=0):
    """Fake MetaTrader5 module surface used by _send_deal_order."""
    mt5 = SimpleNamespace()
    mt5.TRADE_ACTION_DEAL = 1
    mt5.ORDER_FILLING_IOC = 2
    mt5.ORDER_TIME_GTC = 3
    mt5.TRADE_RETCODE_DONE = 10009
    mt5.ORDER_TYPE_BUY = 0
    mt5.ORDER_TYPE_SELL = 1

    check = _Result()
    check.retcode = check_retcode
    check.comment = "check_ok"

    sent = _Result()
    sent.retcode = retcode_send
    sent.comment = "simulado"

    tick = SimpleNamespace()
    tick.bid = 100.0
    tick.ask = 100.5

    mt5.order_check = lambda request: check
    mt5.order_send = lambda request: sent
    mt5.symbol_info_tick = lambda sym: tick
    return mt5


def _fake_app():
    class _Msg:
        def __init__(self):
            self.sent = []

        def send(self, text: str) -> bool:
            self.sent.append(text)
            return True

    app = SimpleNamespace(messenger=_Msg())
    return app


class TestSendDealOrder:
    def test_success_returns_result(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "MetaTrader5", _fake_mt5(10009))
        result, err = main_mod._send_deal_order(
            "XAUUSD", 0.1, 1, 123, 100.0, "test_ok")
        assert result is not None
        assert result.retcode == 10009
        assert err is None

    def test_retcode_failure_returns_none(self, monkeypatch):
        # 10027 = AutoTrading disabled by client — order_send returns an
        # object, but the code must treat it as FAILURE (None result).
        monkeypatch.setitem(sys.modules, "MetaTrader5", _fake_mt5(10027))
        result, err = main_mod._send_deal_order(
            "XAUUSD", 0.1, 1, 123, 100.0, "block_fora_da_janela")
        assert result is None
        assert "10027" in err

    def test_order_check_failure_returns_none(self, monkeypatch):
        # 10018 = market closed
        monkeypatch.setitem(sys.modules, "MetaTrader5", _fake_mt5(check_retcode=10018))
        result, err = main_mod._send_deal_order(
            "XAUUSD", 0.1, 1, 123, 100.0, "block_x")
        assert result is None
        assert "10018" in err


class TestClosePosition:
    def test_returns_false_when_order_fails(self, monkeypatch):
        fake = _fake_mt5(retcode_send=10027)
        monkeypatch.setattr(main_mod, "mt5", fake)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        app = _fake_app()
        pos = {"ticket": 123, "type": 0, "volume": 0.1}
        ok = main_mod._close_position(
            None, "XAUUSD", pos, "block_fora_da_janela",
            logging.getLogger("test"), app, label="BLOQUEIO")
        assert ok is False
        assert app.messenger.sent == []  # não pode notificar fechamento falso
        assert "10027" in getattr(app, "_last_close_error", "")

    def test_returns_true_and_notifies_when_ok(self, monkeypatch):
        fake = _fake_mt5(retcode_send=10009)
        monkeypatch.setattr(main_mod, "mt5", fake)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        app = _fake_app()
        pos = {"ticket": 123, "type": 0, "volume": 0.1}
        ok = main_mod._close_position(
            None, "XAUUSD", pos, "block_fora_da_janela",
            logging.getLogger("test"), app, label="BLOQUEIO")
        assert ok is True
        assert len(app.messenger.sent) == 1
        assert "posicao fechada" in app.messenger.sent[0]


class TestNotifyBlockCloseFailed:
    def test_notifies_only_once(self):
        app = _fake_app()
        pos = {"ticket": 123}
        main_mod._notify_block_close_failed(app, pos, logging.getLogger("test"))
        main_mod._notify_block_close_failed(app, pos, logging.getLogger("test"))
        assert len(app.messenger.sent) == 1
        assert "FALHA AO FECHAR" in app.messenger.sent[0]

    def test_reset_flag_allows_new_notification(self):
        app = _fake_app()
        pos = {"ticket": 123}
        main_mod._notify_block_close_failed(app, pos, logging.getLogger("test"))
        app._force_close_notified = False  # reset ao fechar com sucesso
        main_mod._notify_block_close_failed(app, pos, logging.getLogger("test"))
        assert len(app.messenger.sent) == 2

    def test_notify_includes_last_close_error(self):
        app = _fake_app()
        app._last_close_error = "retcode=10027 comment=AutoTrading disabled"
        pos = {"ticket": 123}
        main_mod._notify_block_close_failed(app, pos, logging.getLogger("test"))
        assert len(app.messenger.sent) == 1
        assert "10027" in app.messenger.sent[0]
        assert "Algo Trading" in app.messenger.sent[0]
