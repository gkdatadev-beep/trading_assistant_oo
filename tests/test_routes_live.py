"""Integration tests for Flask routes — requires live Flask + MT5.

Run with:  pytest tests/test_routes_live.py -m "live_mt5 and live_flask" -v
           (auto-skipped if either MT5 or Flask is not running)
"""
from __future__ import annotations

import pytest
import requests

pytestmark = [pytest.mark.live_mt5, pytest.mark.live_flask]


class TestLiveIndex:
    def test_index_html(self, live_flask_url):
        resp = requests.get(f"{live_flask_url}/", timeout=10)
        assert resp.status_code == 200
        assert "Trade Assistant" in resp.text

    def test_parm_page(self, live_flask_url):
        resp = requests.get(f"{live_flask_url}/parm", timeout=10)
        assert resp.status_code == 200
        assert "SYMBOL" in resp.text or "Parametros" in resp.text

    def test_chart_page(self, live_flask_url):
        resp = requests.get(f"{live_flask_url}/chart", timeout=10)
        assert resp.status_code == 200
        assert "Grafico" in resp.text or "chart" in resp.text.lower()


class TestLiveApiState:
    def test_api_state_json(self, live_flask_url):
        resp = requests.get(f"{live_flask_url}/api/state", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "day_key" in data
        assert "daily_profit" in data
        assert "flags" in data
        assert "server_time" in data
        assert "mt5_connected" in data
        assert data["mt5_connected"] is True

    def test_api_state_block_details(self, live_flask_url):
        resp = requests.get(f"{live_flask_url}/api/state", timeout=10)
        data = resp.json()
        assert "block_details" in data
        details = data["block_details"]
        assert len(details) == 5
        for block in details:
            assert "name" in block
            assert "key" in block
            assert "blocked" in block

    def test_api_state_trade_history(self, live_flask_url):
        resp = requests.get(f"{live_flask_url}/api/state", timeout=10)
        data = resp.json()
        assert "trade_history" in data
        assert "closed_trades" in data


class TestLiveDebug:
    def test_debug_json(self, live_flask_url):
        resp = requests.get(f"{live_flask_url}/debug", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "server_time" in data
        assert "session" in data
        assert "closed_trades" in data


class TestLiveChart:
    def test_chart_today_image(self, live_flask_url):
        resp = requests.get(f"{live_flask_url}/chart", timeout=15)
        assert resp.status_code == 200
        # Should contain a base64-encoded PNG
        if "data:image/png;base64," in resp.text:
            assert len(resp.text) > 5000  # non-trivial image
