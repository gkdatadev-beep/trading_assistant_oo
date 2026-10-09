"""Global pytest fixtures — MT5 mock + Flask test client + live probes."""
from __future__ import annotations

import importlib
import socket
import sys
import tempfile
from typing import Optional

import pytest


# ── Session-wide live-service probes ───────────────────────

_live_mt5_cache: Optional[bool] = None
_live_flask_cache: Optional[bool] = None


def _probe_mt5() -> bool:
    global _live_mt5_cache
    if _live_mt5_cache is not None:
        return _live_mt5_cache
    try:
        import MetaTrader5 as mt5
        ok = mt5.initialize()
        if ok:
            mt5.shutdown()
        _live_mt5_cache = ok
    except Exception:
        _live_mt5_cache = False
    return _live_mt5_cache


def _probe_flask() -> bool:
    global _live_flask_cache
    if _live_flask_cache is not None:
        return _live_flask_cache
    try:
        s = socket.create_connection(("127.0.0.1", 8000), timeout=1.0)
        s.close()
        _live_flask_cache = True
    except OSError:
        _live_flask_cache = False
    return _live_flask_cache


@pytest.fixture(scope="session")
def live_mt5():
    """Real MT5 connection — skips if MT5 terminal not open."""
    if not _probe_mt5():
        pytest.skip("MT5 not connected — open MT5 terminal for live tests")
    import MetaTrader5 as mt5
    mt5.initialize()
    yield mt5
    mt5.shutdown()


@pytest.fixture(scope="session")
def live_flask_url() -> str:
    """Base URL of running Flask app — skips if not listening on :8000."""
    if not _probe_flask():
        pytest.skip(
            "Flask not running at http://localhost:8000 — "
            "start with: python -m app.main"
        )
    return "http://localhost:8000"


# ── Helpers ────────────────────────────────────────────────


def _build_minimal_fixture() -> dict:
    """Build a minimal clean fixture (no positions, no deals)."""
    now_ts = 1753992000  # 2026-07-31 12:00 UTC
    base_price = 4080.0
    fixture: dict = {
        "initialize": True,
        "last_error": [0, "ok"],
        "_schema_version": 1,
        "_captured_at": "2026-07-31T00:00:00+00:00",
        "symbol_info_tick": [
            {"time": now_ts, "bid": base_price, "ask": base_price + 0.5}
        ],
        "positions_get": [],
        "history_deals_get": [],
    }
    # M5 bars: 48 candles, 5-min apart, gentle uptrend
    fixture["copy_rates_from_pos_5"] = [
        {"time": now_ts - (48 - i) * 300, "open": base_price + i * 0.1,
         "high": base_price + i * 0.1 + 0.5, "low": base_price + i * 0.1 - 0.3,
         "close": base_price + i * 0.1 + 0.2,
         "tick_volume": 100, "spread": 2, "real_volume": 50}
        for i in range(48)
    ]
    fixture["copy_rates_from_pos_15"] = fixture["copy_rates_from_pos_5"]
    fixture["copy_rates_from_pos"] = fixture["copy_rates_from_pos_5"]
    return fixture


def _patch_and_reload(fixture_data: dict):
    """Replace MetaTrader5 in sys.modules and reload broker to pick it up."""
    from tests.fixtures.mt5_mock import _MockMT5Module
    import app.core.broker

    sys.modules["MetaTrader5"] = _MockMT5Module(fixture_data)
    importlib.reload(app.core.broker)
    # Attach-only guard: pretend the desktop terminal is always running so
    # unit tests don't depend on a real terminal64 process.
    app.core.broker._terminal_process_running = (
        lambda proc_name="terminal64.exe": True
    )
    # Also purge any sub-modules that cached mt5
    for mod_name in list(sys.modules):
        if mod_name.startswith("app.") and "broker" not in mod_name:
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "mt5"):
                importlib.reload(mod)


def _unpatch():
    """Restore original MetaTrader5 and reload broker."""
    import app.core.broker
    sys.modules.pop("MetaTrader5", None)
    try:
        importlib.reload(app.core.broker)
    except Exception:
        pass


# ── MT5 Mock fixtures ─────────────────────────────────────


@pytest.fixture(scope="function")
def mt5_mock():
    """Clean MT5 mock: connected, no positions, no deals."""
    _patch_and_reload(_build_minimal_fixture())
    yield
    _unpatch()


@pytest.fixture(scope="function")
def mt5_mock_with_position():
    """MT5 mock with one open BUY position (ticket=12345)."""
    data = _build_minimal_fixture()
    data["positions_get"] = [{
        "ticket": 12345, "symbol": "XAUUSD", "type": 0,
        "volume": 0.15, "price_open": 4075.0,
        "price_current": 4080.0, "sl": 0.0, "tp": 0.0,
        "profit": 75.0, "comment": "",
    }]
    _patch_and_reload(data)
    yield
    _unpatch()


@pytest.fixture(scope="function")
def mt5_mock_no_connection():
    """MT5 mock where initialize() returns False."""
    data = {
        "initialize": False,
        "last_error": [1, "Connection failed"],
    }
    _patch_and_reload(data)
    yield
    _unpatch()


# ── Flask test client ─────────────────────────────────────


@pytest.fixture(scope="function")
def flask_client(mt5_mock):
    """Flask test client with mocked MT5 and in-memory config."""
    import logging
    logging.basicConfig(level=logging.WARNING)

    from app.config import RuntimeConfig
    tmp = tempfile.mkdtemp()
    config = RuntimeConfig(data_dir=tmp)
    config.update({
        "SYMBOL": "XAUUSD", "TIMEFRAME": "TIMEFRAME_M15",
        "TELEGRAM_ENABLED": "0",
    })

    from flask import Flask
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )

    from app.core.broker import MT5Broker
    from app.core.market import MarketAnalyzer
    from app.core.messenger import Messenger
    from app.core.persistence import StatePersistence
    from app.core.risk import RiskManager, RiskLimits
    from app.core.session import TradingSession

    broker = MT5Broker()
    broker.initialize()

    market = MarketAnalyzer(
        window1_start=config.get_str("TRADING_WINDOW_1_START", "01:00"),
        window1_duration_hours=config.get_int("TRADING_WINDOW_1_DURATION_HOURS", 5),
        window2_start=config.get_str("TRADING_WINDOW_2_START", "15:00"),
        window2_duration_hours=config.get_int("TRADING_WINDOW_2_DURATION_HOURS", 5),
    )
    risk = RiskManager(RiskLimits(
        limite_lucro_diario=config.get_float("LIMITE_LUCRO_DIARIO", 750),
        limite_perda_diario=config.get_float("LIMITE_PERDA_DIARIO", -350),
        limite_operacoes=config.get_int("LIMITE_OPERACOES", 10),
        limite_lucro_op=config.get_float("LIMITE_LUCRO_OP", 350),
        limite_perda_op=config.get_float("LIMITE_PERDA_OP", -200),
        max_profit_retention_pct=config.get_float("MAX_PROFIT_RETENTION_PCT", 50),
        lock_entry=config.get_float("LOCK_ENTRY", 0.5),
        overtrading_protection_minutes=config.get_int(
            "OVERTRADING_PROTECTION_TIME", 12,
        ),
    ))
    persistence = StatePersistence(data_dir=tmp)
    messenger = Messenger(enabled=False)

    app.broker = broker
    app.config_obj = config
    app.market = market
    app.risk = risk
    app.persistence = persistence
    app.messenger = messenger
    app.session = TradingSession.for_today(broker.get_broker_local_time())
    app.trade_history = []
    app.closed_trades = []

    from app.web.routes import bp
    app.register_blueprint(bp)

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.test_client() as client:
        yield client
