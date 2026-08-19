"""Tests for the persistent trade ledger."""
from __future__ import annotations

import tempfile

from app.core.ledger import TradeLedger


def _sample(ticket, profit, account="2306509"):
    return {
        "ticket": ticket,
        "account": account,
        "symbol": "XAUUSDC",
        "direction": "buy",
        "volume": 0.15,
        "entry_price": 4050.0,
        "exit_price": 4060.0,
        "profit": profit,
        "open_time": "2026-08-07T13:00:00+00:00",
        "close_time": "2026-08-07T14:00:00+00:00",
        "sl": 4045.0,
        "tp": 4070.0,
        "comment": "",
        "delta_price": 10.0,
    }


class TestLedger:
    def test_upsert_and_read(self):
        ledger = TradeLedger(data_dir=tempfile.mkdtemp(), account="2306509")
        new = ledger.upsert([_sample(5329764, 75.0)])
        assert len(new) == 1
        trades = ledger.trades()
        assert len(trades) == 1
        assert trades[0]["ticket"] == 5329764
        assert trades[0]["account"] == "2306509"

    def test_upsert_merges_existing(self):
        ledger = TradeLedger(data_dir=tempfile.mkdtemp(), account="2306509")
        base = _sample(5329764, 75.0)
        base["sl"] = 0.0
        base["tp"] = 0.0
        ledger.upsert([base])
        # Later sync brings SL/TP → merged in
        enriched = _sample(5329764, 75.0)
        ledger.upsert([enriched])
        t = ledger.get(5329764)
        assert t["sl"] == 4045.0
        assert t["tp"] == 4070.0

    def test_equity_series_cumulative(self):
        ledger = TradeLedger(data_dir=tempfile.mkdtemp(), account="2306509")
        ledger.upsert([_sample(1, 50.0), _sample(2, -20.0), _sample(3, 10.0)])
        series = ledger.equity_series()
        assert [s["equity"] for s in series] == [50.0, 30.0, 40.0]
        assert series[2]["ticket"] == 3

    def test_persists_across_instances(self):
        d = tempfile.mkdtemp()
        ledger = TradeLedger(data_dir=d, account="2306509")
        ledger.upsert([_sample(5329764, 75.0)])
        ledger2 = TradeLedger(data_dir=d)
        assert ledger2.account == "2306509"
        assert ledger2.get(5329764) is not None

    def test_accounts(self):
        ledger = TradeLedger(data_dir=tempfile.mkdtemp(), account="2306509")
        ledger.upsert([_sample(1, 1.0), _sample(2, 2.0, account="999999")])
        accounts = ledger.accounts()
        assert "2306509" in accounts
        assert "999999" in accounts
