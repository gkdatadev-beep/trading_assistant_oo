"""Tests for Flask routes using test client with mocked dependencies."""
from __future__ import annotations

import json


class TestIndexRoute:
    def test_index_returns_html(self, flask_client):
        resp = flask_client.get("/")
        assert resp.status_code == 200
        assert b"GK DEV TRADING" in resp.data


class TestParmRoute:
    def test_parm_get(self, flask_client):
        resp = flask_client.get("/parm")
        assert resp.status_code == 200
        # Template should contain "Parametros" or form fields
        assert b"SYMBOL" in resp.data or b"Parametros" in resp.data

    def test_parm_post_updates_config(self, flask_client):
        resp = flask_client.post("/parm", data={
            "SYMBOL": "XAGUSD",
            "LOT_SIZE": "0.5",
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_parm_renders_mt5_login_fields(self, flask_client):
        resp = flask_client.get("/parm")
        assert resp.status_code == 200
        assert b"MT5_SERVER" in resp.data
        assert b"MT5_LOGIN" in resp.data
        assert b"MT5_PASSWORD" in resp.data
        # Password field must be masked in the browser
        assert b'type="password"' in resp.data

    def test_parm_post_updates_mt5_credentials(self, flask_client):
        resp = flask_client.post("/parm", data={
            "MT5_SERVER": "Exness-Real2",
            "MT5_LOGIN": "12345678",
            "MT5_PASSWORD": "topsecret",
        }, follow_redirects=True)
        assert resp.status_code == 200
        config = flask_client.application.config_obj
        assert config.get_str("MT5_SERVER") == "Exness-Real2"
        assert config.get_str("MT5_LOGIN") == "12345678"
        assert config.get_str("MT5_PASSWORD") == "topsecret"


class TestChartChannelRoute:
    def test_chart_with_hf_channels(self, flask_client):
        resp = flask_client.get("/chart?tf=D1&months=3")
        assert resp.status_code == 200
        assert b"Canal Principal" in resp.data
        assert b"data:image/png;base64" in resp.data

    def test_chart_without_params_has_no_hf_chart(self, flask_client):
        # O grafico de canais so roda apos escolher tf + meses
        resp = flask_client.get("/chart")
        assert resp.status_code == 200
        assert b"Canal Principal" not in resp.data


class TestApiStateRoute:
    def test_api_state_returns_json(self, flask_client):
        resp = flask_client.get("/api/state")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "day_key" in data
        assert "daily_profit" in data
        assert "flags" in data
        assert "server_time" in data
        assert "mt5_connected" in data
        assert data["mt5_connected"] is True

    def test_api_state_has_block_details(self, flask_client):
        resp = flask_client.get("/api/state")
        data = json.loads(resp.data)
        assert "block_details" in data
        assert isinstance(data["block_details"], list)
        assert len(data["block_details"]) == 5

        # Verify structure of each block detail
        for bd in data["block_details"]:
            assert "name" in bd
            assert "key" in bd
            assert "blocked" in bd
            assert "current" in bd
            assert "limit" in bd

    def test_api_state_has_trade_history(self, flask_client):
        resp = flask_client.get("/api/state")
        data = json.loads(resp.data)
        assert "trade_history" in data
        assert isinstance(data["trade_history"], list)

    def test_api_state_has_closed_trades(self, flask_client):
        resp = flask_client.get("/api/state")
        data = json.loads(resp.data)
        assert "closed_trades" in data
        assert isinstance(data["closed_trades"], list)

    def test_api_state_no_position(self, flask_client):
        resp = flask_client.get("/api/state")
        data = json.loads(resp.data)
        assert data["open_trade"] is None


class TestChartRoute:
    def test_chart_no_args(self, flask_client):
        resp = flask_client.get("/chart")
        # Chart may return 200 even without data (shows empty page)
        assert resp.status_code == 200

    def test_chart_with_day(self, flask_client):
        resp = flask_client.get("/chart?day=2026-07-31")
        assert resp.status_code == 200


class TestContaRoute:
    def test_conta_get_empty_ledger(self, flask_client):
        resp = flask_client.get("/conta")
        assert resp.status_code == 200
        assert b"Extrato da Conta" in resp.data

    def test_conta_with_window_filters(self, flask_client):
        resp = flask_client.get("/conta?last=30")
        assert resp.status_code == 200
        resp = flask_client.get("/conta?start=2026-08-01&end=2026-08-15")
        assert resp.status_code == 200
        assert b"Extrato da Conta" in resp.data

    def test_conta_trade_chart_unknown_ticket(self, flask_client):
        resp = flask_client.get("/conta/trade/999999")
        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert "error" in data


class TestDebugRoute:
    def test_debug_returns_json(self, flask_client):
        resp = flask_client.get("/debug")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "server_time" in data
        assert "session" in data
