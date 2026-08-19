"""Mock MT5 module — replays pre-recorded data for offline testing.

Usage in tests:
    from tests.fixtures.mt5_mock import MT5Mock
    mock = MT5Mock("tests/data/broker_v1.json")
    with mock.patch():
        broker = MT5Broker()
        broker.initialize()  # uses mock data

Capture real data:
    python -c "from tests.fixtures.mt5_mock import capture_and_save; capture_and_save()"
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import warnings
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEAL_COLUMNS = [
    "ticket", "order", "time", "time_msc", "type", "entry", "magic",
    "position_id", "reason", "volume", "price", "commission", "swap",
    "profit", "fee", "symbol", "comment", "unused",
]
ORDER_COLUMNS = [
    "ticket", "time_setup", "type", "state", "time_expiration", "time_done",
    "time_setup_msc", "time_done_msc", "type_filling", "type_time", "magic",
    "position_id", "position_by_id", "reason", "volume_initial",
    "volume_current", "price_open", "sl", "tp", "price_current", "symbol",
    "comment",
]

DealRow = namedtuple("DealRow", DEAL_COLUMNS, defaults=[0] * len(DEAL_COLUMNS))
OrderRow = namedtuple("OrderRow", ORDER_COLUMNS, defaults=[0] * len(ORDER_COLUMNS))


class _MockTick(SimpleNamespace):
    pass


class _MockOrderResult(SimpleNamespace):
    pass


def _compute_schema_hash(data: dict) -> str:
    """Deterministic hash of data keys + value types (ignores values)."""

    def _structure(obj):
        if isinstance(obj, dict):
            return tuple(sorted((k, _structure(v)) for k, v in obj.items()))
        if isinstance(obj, list):
            return tuple(_structure(v) for v in obj[:3]) if obj else "empty_list"
        return type(obj).__name__

    raw = json.dumps(_structure(data), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class MT5Mock:
    """Replay MT5 API responses from a JSON fixture file.

    Detects schema changes via hash comparison and emits UserWarning
    when the fixture is stale vs the current expected schema.
    """

    def __init__(self, fixture_path: str = "tests/data/broker_v1.json"):
        self._fixture_path = Path(fixture_path)
        self._data: dict = {}
        self._original_module: Any = None

    # ── load / save ────────────────────────────────────────

    def load(self) -> dict:
        if not self._fixture_path.exists():
            raise FileNotFoundError(
                f"MT5 fixture not found: {self._fixture_path}\n"
                f"Generate it with MT5 connected:\n"
                f"  python -c \"from tests.fixtures.mt5_mock import "
                f"capture_and_save; capture_and_save()\""
            )
        raw = json.loads(self._fixture_path.read_text(encoding="utf-8"))
        version = raw.get("_schema_version", 0)
        stored_hash = raw.get("_schema_hash", "")

        if version != SCHEMA_VERSION:
            warnings.warn(
                f"⚠ MT5 fixture schema VERSION mismatch: fixture=v{version}, "
                f"current=v{SCHEMA_VERSION}. Re-capture with capture_and_save().",
                UserWarning,
            )

        data_only = {k: v for k, v in raw.items() if not k.startswith("_")}
        current_hash = _compute_schema_hash(data_only)
        if stored_hash and stored_hash != current_hash:
            warnings.warn(
                f"⚠ MT5 fixture schema HASH changed! MT5 API format may have "
                f"drifted.\n"
                f"  fixture: {stored_hash[:12]}...\n"
                f"  current: {current_hash[:12]}...\n"
                f"  Re-capture: python -c \"from tests.fixtures.mt5_mock "
                f"import capture_and_save; capture_and_save()\"",
                UserWarning,
            )

        self._data = raw
        return raw

    def save(self, captured: dict) -> Path:
        data_only = {k: v for k, v in captured.items() if not k.startswith("_")}
        captured["_schema_hash"] = _compute_schema_hash(data_only)
        captured["_schema_version"] = SCHEMA_VERSION
        captured["_captured_at"] = datetime.now(timezone.utc).isoformat()
        self._fixture_path.parent.mkdir(parents=True, exist_ok=True)
        self._fixture_path.write_text(
            json.dumps(captured, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("MT5 fixture saved → %s", self._fixture_path)
        return self._fixture_path

    # ── patch / unpatch ────────────────────────────────────

    def patch(self):
        self.load()
        self._original_module = sys.modules.get("MetaTrader5")
        from tests.fixtures.mt5_mock import _MockMT5Module
        sys.modules["MetaTrader5"] = _MockMT5Module(self._data)
        return self

    def unpatch(self):
        if self._original_module is not None:
            sys.modules["MetaTrader5"] = self._original_module
        else:
            sys.modules.pop("MetaTrader5", None)
        self._original_module = None

    def __enter__(self):
        return self.patch()

    def __exit__(self, *args):
        self.unpatch()


# ── Mock MT5 module replacement ────────────────────────────


class _MockMT5Module:
    """Drop-in replacement for MetaTrader5 that replays fixture data."""

    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408

    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self, data: dict):
        self._data = data
        self._tick_idx = 0

    def initialize(self, *a, **kw) -> bool:
        return self._data.get("initialize", True)

    def shutdown(self):
        pass

    def last_error(self):
        return self._data.get("last_error", (0, "ok"))

    def account_info(self):
        ai = self._data.get("account_info")
        if not ai:
            return None
        a = _MockTick()
        a.login = ai.get("login", 0)
        a.server = ai.get("server", "")
        return a

    def login(self, *a, **kw):
        login = kw.get("login") or (a[0] if a else None)
        self._data.setdefault("login_calls", []).append(kw)
        if not self._data.get("login_result", True):
            return False
        if login:
            self._data["account_info"] = {"login": int(login), "server": kw.get("server", "")}
        return True

    def symbol_info_tick(self, symbol: str):
        ticks = self._data.get("symbol_info_tick", [])
        if isinstance(ticks, list) and ticks:
            idx = self._tick_idx % len(ticks)
            self._tick_idx += 1
            td = ticks[idx]
        elif isinstance(ticks, dict):
            td = ticks
        else:
            return None
        t = _MockTick()
        t.time = td.get("time", 0)
        t.bid = td.get("bid", 0.0)
        t.ask = td.get("ask", 0.0)
        return t

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        key = f"copy_rates_from_pos_{timeframe}"
        records = self._data.get(key, self._data.get("copy_rates_from_pos", []))
        if not records:
            return None
        return _records_to_array(records, start_pos, count)

    def copy_rates_range(self, symbol, timeframe, from_dt, to_dt):
        key = f"copy_rates_from_pos_{timeframe}"
        records = self._data.get(key, self._data.get("copy_rates_from_pos", []))
        if not records:
            return None
        return _records_to_array(records, 0, len(records))

    def positions_get(self, *a):
        pd = self._data.get("positions_get", [])
        if not pd:
            return None
        result = []
        for p in pd:
            pos = _MockTick()
            pos.ticket = p.get("ticket", 0)
            pos.symbol = p.get("symbol", "")
            pos.type = p.get("type", 0)
            pos.volume = p.get("volume", 0.0)
            pos.price_open = p.get("price_open", 0.0)
            pos.price_current = p.get("price_current", 0.0)
            pos.sl = p.get("sl", 0.0)
            pos.tp = p.get("tp", 0.0)
            pos.profit = p.get("profit", 0.0)
            pos.comment = p.get("comment", "")
            result.append(pos)
        return tuple(result)

    def positions_total(self):
        return len(self._data.get("positions_get", []))

    def history_deals_get(self, from_dt, to_dt):
        deals = self._data.get("history_deals_get", [])
        if not deals:
            return None
        return tuple(DealRow(**d) for d in deals)

    def history_orders_get(self, from_dt, to_dt):
        orders = self._data.get("history_orders_get", [])
        if not orders:
            return None
        return tuple(OrderRow(**o) for o in orders)

    def order_send(self, request: dict):
        r = _MockOrderResult()
        r.retcode = self.TRADE_RETCODE_DONE
        r.order = 1000000
        r.deal = 2000000
        r.comment = "mock_order"
        return r


def _records_to_array(records: list, start: int, count: int):
    arr = np.array(
        [(r["time"], r["open"], r["high"], r["low"], r["close"],
          r.get("tick_volume", 0), r.get("spread", 0), r.get("real_volume", 0))
         for r in records],
        dtype=[
            ("time", "<i8"), ("open", "<f8"), ("high", "<f8"),
            ("low", "<f8"), ("close", "<f8"), ("tick_volume", "<i8"),
            ("spread", "<i4"), ("real_volume", "<i8"),
        ],
    )
    arr = arr[start:start + count]
    return arr if len(arr) > 0 else None


# ── Capture tool (run once with MT5 connected) ─────────────


def capture_and_save(
    symbol: str = "XAUUSD",
    output: str = "tests/data/broker_v1.json",
) -> Path:
    """Capture live MT5 data and save as a fixture file."""
    import MetaTrader5 as real_mt5
    if not real_mt5.initialize():
        raise RuntimeError(f"MT5 connect failed: {real_mt5.last_error()}")

    data: dict[str, Any] = {"initialize": True, "last_error": (0, "ok")}
    try:
        tick = real_mt5.symbol_info_tick(symbol)
        data["symbol_info_tick"] = [{
            "time": tick.time if tick and tick.time else 0,
            "bid": tick.bid if tick else 0.0,
            "ask": tick.ask if tick else 0.0,
        }]

        for tf, key in [
            (real_mt5.TIMEFRAME_M5, "copy_rates_from_pos_5"),
            (real_mt5.TIMEFRAME_M15, "copy_rates_from_pos_15"),
        ]:
            rates = real_mt5.copy_rates_from_pos(symbol, tf, 0, 48)
            if rates is not None and len(rates) > 0:
                data[key] = [
                    {"time": int(r["time"]), "open": float(r["open"]),
                     "high": float(r["high"]), "low": float(r["low"]),
                     "close": float(r["close"]),
                     "tick_volume": int(r.get("tick_volume", 0)),
                     "spread": int(r.get("spread", 0)),
                     "real_volume": int(r.get("real_volume", 0))}
                    for r in rates
                ]
            else:
                data[key] = []

        if "copy_rates_from_pos_15" in data:
            data["copy_rates_from_pos"] = data["copy_rates_from_pos_15"]

        positions = real_mt5.positions_get()
        data["positions_get"] = [
            {"ticket": p.ticket, "symbol": p.symbol, "type": p.type,
             "volume": p.volume, "price_open": p.price_open,
             "price_current": p.price_current, "sl": p.sl,
             "tp": p.tp, "profit": p.profit, "comment": p.comment}
            for p in positions
        ] if positions else []

        now = datetime.now(timezone.utc)
        ds = datetime(now.year, now.month, now.day)
        utc = timezone.utc
        deals = real_mt5.history_deals_get(
            ds.replace(tzinfo=utc),
            (ds + timedelta(days=1) - timedelta(seconds=1)).replace(tzinfo=utc),
        )
        data["history_deals_get"] = [
            dict(zip(DEAL_COLUMNS, d)) for d in deals
        ] if deals is not None and len(deals) > 0 else []

        print(
            f"✓ Captured {len(data.get('symbol_info_tick', []))} ticks, "
            f"{len(data.get('copy_rates_from_pos_5', []))} M5 bars, "
            f"{len(data.get('positions_get', []))} positions, "
            f"{len(data.get('history_deals_get', []))} deals"
        )
    finally:
        real_mt5.shutdown()

    mock = MT5Mock(output)
    return mock.save(data)
