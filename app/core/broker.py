"""MT5Broker — single connection point to MetaTrader 5."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd

logger = logging.getLogger(__name__)

MT5_TIMEFRAMES = {
    "TIMEFRAME_M1": mt5.TIMEFRAME_M1,
    "TIMEFRAME_M5": mt5.TIMEFRAME_M5,
    "TIMEFRAME_M15": mt5.TIMEFRAME_M15,
    "TIMEFRAME_M30": mt5.TIMEFRAME_M30,
    "TIMEFRAME_H1": mt5.TIMEFRAME_H1,
    "TIMEFRAME_H4": mt5.TIMEFRAME_H4,
    "TIMEFRAME_D1": mt5.TIMEFRAME_D1,
}


class MT5Broker:
    """Singleton connection to MT5 — one instance per process."""

    def __init__(self):
        self._connected = False

    # ── lifecycle ───────────────────────────────────────────

    def initialize(self) -> bool:
        if self._connected:
            return True
        try:
            self._connected = mt5.initialize()
            if self._connected:
                logger.info("MT5 connected")
            else:
                logger.error("MT5 init failed: %s", mt5.last_error())
        except Exception:
            logger.exception("MT5 init exception")
            self._connected = False
        return self._connected

    def shutdown(self):
        if self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 shutdown")

    def reconnect(self) -> bool:
        self.shutdown()
        return self.initialize()

    @property
    def connected(self) -> bool:
        return self._connected

    # ── time ────────────────────────────────────────────────

    def get_server_time(self) -> datetime:
        """Live MT5 server time (naive UTC)."""
        tick = mt5.symbol_info_tick(self._get_symbol())
        if tick is not None and getattr(tick, "time", 0):
            return datetime.utcfromtimestamp(int(tick.time))
        rates = mt5.copy_rates_from_pos(
            self._get_symbol(), self._get_timeframe(), 0, 1,
        )
        if rates is not None and len(rates) > 0:
            return datetime.utcfromtimestamp(int(rates[0]["time"]))
        raise RuntimeError("MT5 server time unavailable")

    # ── market data ─────────────────────────────────────────

    def get_rates(
        self, symbol: Optional[str] = None, timeframe: Optional[int] = None, count: int = 48,
    ) -> Optional[pd.DataFrame]:
        sym = symbol or self._get_symbol()
        tf = timeframe or self._get_timeframe()
        rates = mt5.copy_rates_from_pos(sym, tf, 0, count)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        return df.sort_index()

    def get_rates_range(
        self, from_dt: datetime, to_dt: datetime,
        symbol: Optional[str] = None, timeframe: Optional[int] = None,
    ) -> Optional[pd.DataFrame]:
        sym = symbol or self._get_symbol()
        tf = timeframe or self._get_timeframe()
        rates = mt5.copy_rates_range(sym, tf, from_dt, to_dt)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        return df.sort_index()

    def get_current_candle(
        self, symbol: Optional[str] = None, timeframe: Optional[int] = None,
    ) -> Optional[dict]:
        sym = symbol or self._get_symbol()
        tf = timeframe or self._get_timeframe()
        rates = mt5.copy_rates_from_pos(sym, tf, 0, 1)
        if rates is None or len(rates) == 0:
            return None
        row = rates[0]
        return {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "time": datetime.utcfromtimestamp(int(row["time"])),
        }

    # ── positions ───────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        positions = mt5.positions_get()
        if not positions:
            return []
        return [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.type,  # 0=buy, 1=sell
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "comment": p.comment,
            }
            for p in positions
        ]

    def positions_total(self) -> int:
        return mt5.positions_total() or 0

    # ── account ─────────────────────────────────────────────

    def get_account_profit(
        self, from_dt: Optional[datetime] = None, to_dt: Optional[datetime] = None,
    ) -> tuple[float, float]:
        """Return (closed_profit, open_profit)."""
        if from_dt is None:
            server = self.get_server_time()
            from_dt = datetime(server.year, server.month, server.day)
        if to_dt is None:
            to_dt = self.get_server_time()

        # MT5 requires UTC timezone-aware datetimes
        utc = __import__("datetime").timezone.utc
        _from = from_dt.replace(tzinfo=utc) if from_dt.tzinfo is None else from_dt
        _to = to_dt.replace(tzinfo=utc) if to_dt.tzinfo is None else to_dt

        deals = mt5.history_deals_get(_from, _to)
        closed_pnl = 0.0
        if deals is not None and len(deals) > 0:
            df = pd.DataFrame(list(deals))
            if "profit" in df.columns:
                closed_pnl = round(float(df["profit"].sum()), 2)

        positions = mt5.positions_get()
        open_pnl = 0.0
        if positions:
            open_pnl = round(sum(float(getattr(p, "profit", 0) or 0) for p in positions), 2)

        return closed_pnl, open_pnl

    # ── trading ─────────────────────────────────────────────

    def send_order(self, request: dict) -> Optional[dict]:
        result = mt5.order_send(request)
        if result is None:
            return None
        return {
            "retcode": result.retcode,
            "order": getattr(result, "order", None),
            "deal": getattr(result, "deal", None),
            "comment": getattr(result, "comment", ""),
        }

    # ── helpers ─────────────────────────────────────────────

    def _get_symbol(self) -> str:
        try:
            from app.config import RuntimeConfig
            return RuntimeConfig().get_str("SYMBOL", "XAUUSD")
        except Exception:
            return "XAUUSD"

    def _get_timeframe(self) -> int:
        try:
            from app.config import RuntimeConfig
            key = RuntimeConfig().get_str("TIMEFRAME", "TIMEFRAME_M15")
            return MT5_TIMEFRAMES.get(key, mt5.TIMEFRAME_M15)
        except Exception:
            return mt5.TIMEFRAME_M15
