"""MT5Broker — single connection point to MetaTrader 5."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd

logger = logging.getLogger(__name__)

UTC = timezone.utc

MT5_TIMEFRAMES = {
    "TIMEFRAME_M1": mt5.TIMEFRAME_M1,
    "TIMEFRAME_M5": mt5.TIMEFRAME_M5,
    "TIMEFRAME_M15": mt5.TIMEFRAME_M15,
    "TIMEFRAME_M30": mt5.TIMEFRAME_M30,
    "TIMEFRAME_H1": mt5.TIMEFRAME_H1,
    "TIMEFRAME_H4": mt5.TIMEFRAME_H4,
    "TIMEFRAME_D1": mt5.TIMEFRAME_D1,
}

# How often to run the health check (seconds)
HEALTH_CHECK_INTERVAL = 30
# Maximum number of consecutive health failures before triggering reconnect
MAX_HEALTH_FAILURES = 3
# How long to wait between reconnect attempts (seconds)
RECONNECT_BACKOFF_BASE = 5
RECONNECT_BACKOFF_MAX = 60


class MT5Broker:
    """Singleton connection to MT5 — one instance per process.

    Includes automatic health-check and self-healing reconnection.
    """

    def __init__(self, login=None, password=None, server=None):
        self._connected = False
        self._last_health_check: float = 0.0
        self._health_failures: int = 0
        self._reconnect_lock = Lock()
        self._last_error: str = ""
        self._last_successful_call: float = 0.0
        try:
            self._mt5_login: Optional[int] = int(login) if login else None
        except (TypeError, ValueError):
            self._mt5_login = None
        self._mt5_password: str = str(password or "")
        self._mt5_server: str = str(server or "")

    # ── lifecycle ───────────────────────────────────────────

    def initialize(self) -> bool:
        if self._connected:
            return True
        try:
            self._connected = mt5.initialize()
            if self._connected:
                self._select_symbol()
                if self._ensure_login():
                    logger.info("MT5 connected and logged in")
                    self._health_failures = 0
                    self._last_error = ""
                    self._last_successful_call = time.monotonic()
                else:
                    logger.warning("MT5 terminal connected but login failed: %s", self._last_error)
            else:
                err = mt5.last_error()
                self._last_error = str(err) if err else "unknown init error"
                logger.error("MT5 init failed: %s", self._last_error)
        except Exception:
            self._last_error = "exception during init"
            logger.exception("MT5 init exception")
            self._connected = False
        return self._connected

    def _ensure_login(self) -> bool:
        """Log the terminal into the configured account and verify it.

        Returns True when no credentials are configured (nothing to verify),
        when the terminal is already logged into the target account, or when
        mt5.login() succeeds AND account_info() confirms the login afterwards.
        """
        if not self._mt5_login:
            return True
        try:
            info = mt5.account_info()
            if info is not None and int(getattr(info, "login", 0) or 0) == self._mt5_login:
                return True
        except Exception:
            pass
        try:
            kwargs = {"login": self._mt5_login, "password": self._mt5_password}
            if self._mt5_server:
                kwargs["server"] = self._mt5_server
            if not mt5.login(**kwargs):
                err = mt5.last_error()
                self._last_error = f"mt5.login rejected: {err}"
                logger.error("MT5 login rejected for account %s: %s", self._mt5_login, self._last_error)
                return False
        except Exception:
            self._last_error = "exception during mt5.login"
            logger.exception("MT5 login exception")
            return False
        # mt5.login returned True — verify the login actually took effect
        try:
            info = mt5.account_info()
            if info is not None and int(getattr(info, "login", 0) or 0) == self._mt5_login:
                logger.info("MT5 login verified: account %s (%s)",
                            self._mt5_login, self._mt5_server or "default server")
                return True
        except Exception:
            pass
        self._last_error = f"mt5.login returned OK but account_info does not confirm account {self._mt5_login}"
        logger.error("%s", self._last_error)
        return False

    def shutdown(self):
        if self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 shutdown")

    def reconnect(self) -> bool:
        """Reconnect to MT5 with exponential backoff."""
        with self._reconnect_lock:
            self.shutdown()
            for attempt in range(1, 6):
                backoff = min(RECONNECT_BACKOFF_BASE * (2 ** (attempt - 1)), RECONNECT_BACKOFF_MAX)
                logger.warning("MT5 reconnect attempt %d/5 (waiting %ds)...", attempt, backoff)
                time.sleep(backoff)
                if self.initialize():
                    logger.info("MT5 reconnected on attempt %d", attempt)
                    return True
            logger.critical("MT5 reconnect FAILED after 5 attempts — manual intervention needed")
            return False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def seconds_since_last_success(self) -> float:
        """Seconds since the last successful MT5 operation."""
        if self._last_successful_call == 0.0:
            return 0.0
        return time.monotonic() - self._last_successful_call

    # ── health check ────────────────────────────────────────

    def check_health(self) -> bool:
        """Verify MT5 connection is alive. Returns True if healthy.

        Call this periodically from the update loop.
        Performs a lightweight MT5 call to verify the terminal is responsive.
        If unhealthy, attempts automatic reconnection.
        """
        now = time.monotonic()
        # Throttle health checks
        if now - self._last_health_check < HEALTH_CHECK_INTERVAL:
            return self._connected
        self._last_health_check = now

        try:
            # Lightweight check: verify MT5 terminal is running
            terminal = mt5.terminal_info()
            if terminal is None:
                self._health_failures += 1
                self._last_error = "terminal_info returned None — MT5 may not be running"
                logger.error("Health check FAILED (%d/%d): MT5 terminal not responding",
                             self._health_failures, MAX_HEALTH_FAILURES)
            elif not getattr(terminal, "connected", False):
                self._health_failures += 1
                self._last_error = "terminal connected=False — MT5 logged out or disconnected"
                logger.error("Health check FAILED (%d/%d): MT5 terminal disconnected",
                             self._health_failures, MAX_HEALTH_FAILURES)
            else:
                # Terminal is healthy — do a quick data call to verify full round-trip
                sym = self._get_symbol()
                tick = mt5.symbol_info_tick(sym)
                if tick is None:
                    self._health_failures += 1
                    self._last_error = f"symbol_info_tick({sym}) returned None"
                    logger.error("Health check FAILED (%d/%d): cannot get tick for %s",
                                 self._health_failures, MAX_HEALTH_FAILURES, sym)
                else:
                    self._health_failures = 0
                    self._last_error = ""
                    self._last_successful_call = now
                    logger.debug("Health check OK: tick=%.2f", tick.bid if tick else 0)
                    return True

        except Exception:
            self._health_failures += 1
            self._last_error = "health check exception"
            logger.exception("Health check exception (%d/%d)",
                             self._health_failures, MAX_HEALTH_FAILURES)

        # If too many failures, mark as disconnected and try reconnect
        if self._health_failures >= MAX_HEALTH_FAILURES:
            logger.critical("MT5 health check failed %d times — triggering reconnect",
                            self._health_failures)
            self._connected = False
            if self.reconnect():
                self._health_failures = 0
                return True
        return self._connected

    def _mark_success(self):
        """Call after any successful MT5 operation to reset health state."""
        self._last_successful_call = time.monotonic()
        self._health_failures = 0

    def _mark_error(self, context: str):
        """Call after any failed MT5 operation."""
        self._health_failures += 1
        self._last_error = context
        logger.warning("MT5 operation failed (%d consecutive): %s",
                       self._health_failures, context)

    # ── time ────────────────────────────────────────────────

    def get_server_time(self) -> datetime:
        """Live MT5 server time as timezone-aware UTC datetime."""
        sym = self._get_symbol()
        try:
            tick = mt5.symbol_info_tick(sym)
            if tick is not None and getattr(tick, "time", 0):
                self._mark_success()
                return datetime.fromtimestamp(int(tick.time), UTC)
        except Exception:
            self._mark_error("symbol_info_tick")
        try:
            rates = mt5.copy_rates_from_pos(sym, self._get_timeframe(), 0, 1)
            if rates is not None and len(rates) > 0:
                self._mark_success()
                return datetime.fromtimestamp(int(rates[0]["time"]), UTC)
        except Exception:
            self._mark_error("copy_rates_from_pos")
        self._mark_error("server_time_unavailable")
        raise RuntimeError("MT5 server time unavailable")

    def get_broker_local_time(self) -> datetime:
        """Broker server time (UTC) — all dates/hours reference the MT5 server clock."""
        return self.get_server_time()

    # ── market data ─────────────────────────────────────────

    def get_rates(
        self, symbol: Optional[str] = None, timeframe: Optional[int] = None, count: int = 48,
    ) -> Optional[pd.DataFrame]:
        sym = symbol or self._get_symbol()
        tf = timeframe or self._get_timeframe()
        try:
            rates = mt5.copy_rates_from_pos(sym, tf, 0, count)
        except Exception:
            self._mark_error("copy_rates_from_pos")
            logger.exception("get_rates failed")
            return None
        if rates is None or len(rates) == 0:
            self._mark_error("get_rates returned empty")
            return None
        self._mark_success()
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
            "time": datetime.fromtimestamp(int(row["time"]), UTC),
        }

    # ── positions ───────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        try:
            positions = mt5.positions_get()
        except Exception:
            self._mark_error("positions_get")
            logger.exception("get_positions failed")
            return []
        if not positions:
            self._mark_success()
            return []
        self._mark_success()
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
        try:
            return mt5.positions_total() or 0
        except Exception:
            return 0

    # ── account ─────────────────────────────────────────────

    def get_account_profit(
        self, from_dt: Optional[datetime] = None, to_dt: Optional[datetime] = None,
    ) -> tuple[float, float]:
        """Return (closed_profit, open_profit).

        Day boundaries default to the broker's LOCAL trading day.
        """
        try:
            if from_dt is None:
                local = self.get_broker_local_time()
                from_dt = datetime(local.year, local.month, local.day, tzinfo=local.tzinfo)
            if to_dt is None:
                to_dt = self.get_broker_local_time()

            # MT5 expects UTC-aware datetimes
            _from = from_dt if from_dt.tzinfo else from_dt.replace(tzinfo=UTC)
            _to = to_dt if to_dt.tzinfo else to_dt.replace(tzinfo=UTC)

            deals = mt5.history_deals_get(_from, _to)
            closed_pnl = 0.0
            if deals is not None and len(deals) > 0:
                df = pd.DataFrame(list(deals))
                # MT5 deal tuple columns must be set explicitly
                df.columns = [
                    "ticket", "order", "time", "time_msc", "type", "entry", "magic",
                    "position_id", "reason", "volume", "price", "commission", "swap",
                    "profit", "fee", "symbol", "comment", "unused",
                ]
                if "profit" in df.columns:
                    closed_pnl = round(float(df["profit"].sum()), 2)

            positions = mt5.positions_get()
            open_pnl = 0.0
            if positions:
                open_pnl = round(sum(float(getattr(p, "profit", 0) or 0) for p in positions), 2)

            self._mark_success()
            return closed_pnl, open_pnl
        except Exception:
            self._mark_error("get_account_profit")
            logger.exception("get_account_profit failed")
            return 0.0, 0.0

    def get_closed_trades_today(self) -> list[dict]:
        """Return today's closed trades from MT5 history (broker local day)."""
        try:
            local = self.get_broker_local_time()
            from_dt = datetime(local.year, local.month, local.day, tzinfo=local.tzinfo)
            to_dt = from_dt + timedelta(days=1) - timedelta(seconds=1)

            deals = mt5.history_deals_get(from_dt, to_dt)
            result: list[dict] = []
            if deals is not None and len(deals) > 0:
                df = pd.DataFrame(list(deals))
                df.columns = [
                    "ticket", "order", "time", "time_msc", "type", "entry", "magic",
                    "position_id", "reason", "volume", "price", "commission", "swap",
                    "profit", "fee", "symbol", "comment", "unused",
                ]
                closed = df[df["entry"] == 1]
                for _, row in closed.iterrows():
                    result.append({
                        "ticket": int(row.get("position_id", 0)),
                        "profit": float(row.get("profit", 0)),
                        "time": pd.to_datetime(row["time"], unit="s").strftime("%H:%M:%S"),
                    })
            self._mark_success()
            return result
        except Exception:
            self._mark_error("get_closed_trades_today")
            logger.exception("get_closed_trades_today failed")
            return []

    def get_closed_trades_range(self, from_dt: datetime, to_dt: datetime) -> list[dict]:
        """Closed trades in a period, reconstructed from terminal deal history.

        Joins IN deals (entry) with OUT deals (exit) by position_id and
        attaches the last SL/TP values from history orders. This uses only
        the terminal's local history - no external database.
        """
        try:
            deals = mt5.history_deals_get(from_dt, to_dt)
            if deals is None or len(deals) == 0:
                return []

            # SL/TP per position from order history (attribute access is
            # layout-independent; chronological → last wins). Some builds
            # store modified SL/TP values in price_open with a comment tag.
            sltp: dict[int, tuple[float, float]] = {}
            try:
                orders = mt5.history_orders_get(from_dt, to_dt)
                if orders is not None and len(orders) > 0:
                    for o in orders:
                        pid = int(getattr(o, "position_id", 0) or 0)
                        if not pid:
                            continue
                        comment = str(getattr(o, "comment", "") or "")
                        price_open = float(getattr(o, "price_open", 0) or 0)
                        sl = float(getattr(o, "sl", 0) or 0)
                        tp = float(getattr(o, "tp", 0) or 0)
                        if "[sl" in comment and price_open > 0:
                            sl = price_open
                        if "[tp" in comment and price_open > 0:
                            tp = price_open
                        cur_sl, cur_tp = sltp.get(pid, (0.0, 0.0))
                        sltp[pid] = (sl if sl > 0 else cur_sl,
                                     tp if tp > 0 else cur_tp)
            except Exception:
                logger.debug("SLTP orders unavailable", exc_info=True)

            opens: dict[int, dict] = {}
            closed: list[dict] = []
            for d in deals:
                pid = int(getattr(d, "position_id", 0) or 0)
                if not pid:
                    continue
                entry_flag = int(getattr(d, "entry", -1))
                ts = datetime.fromtimestamp(int(getattr(d, "time", 0)), UTC)
                price = float(getattr(d, "price", 0) or 0)
                if entry_flag == 0:  # IN deal
                    opens[pid] = {
                        "open_time": ts.isoformat(),
                        "entry_price": price,
                        "volume": float(getattr(d, "volume", 0) or 0),
                        "direction": "buy" if int(getattr(d, "type", 0)) == 0 else "sell",
                        "symbol": str(getattr(d, "symbol", "") or ""),
                        "comment": str(getattr(d, "comment", "") or ""),
                    }
                elif entry_flag == 1:  # OUT deal — position closed
                    o = opens.get(pid)
                    sl, tp = sltp.get(pid, (0.0, 0.0))
                    entry_price = o.get("entry_price", 0.0) if o else 0.0
                    direction = o.get("direction", "") if o else ""
                    delta = (
                        (price - entry_price)
                        if direction == "buy"
                        else (entry_price - price)
                    )
                    closed.append({
                        "ticket": pid,
                        "account": "",
                        "symbol": o.get("symbol", "") if o else "",
                        "direction": direction,
                        "volume": o.get("volume", 0.0) if o else 0.0,
                        "entry_price": entry_price,
                        "exit_price": price,
                        "profit": float(getattr(d, "profit", 0) or 0),
                        "open_time": o.get("open_time") if o else None,
                        "close_time": ts.isoformat(),
                        "sl": sl,
                        "tp": tp,
                        "comment": o.get("comment", "") if o else "",
                        "delta_price": round(delta, 5),
                    })
            closed.sort(key=lambda t: t.get("close_time") or "")
            self._mark_success()
            return closed
        except Exception:
            self._mark_error("get_closed_trades_range")
            logger.exception("get_closed_trades_range failed")
            return []

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

    def _select_symbol(self) -> None:
        """Ensure the configured symbol is visible in MT5 Market Watch.

        Required for order_send to work on non-demo accounts — MT5
        will reject orders for symbols not in Market Watch.
        """
        sym = self._get_symbol()
        try:
            if not mt5.symbol_select(sym, True):
                logger.warning("symbol_select(%s) failed — orders may be rejected", sym)
            else:
                logger.info("Symbol '%s' selected in Market Watch", sym)
        except Exception:
            logger.warning("symbol_select(%s) exception", sym, exc_info=True)

    # ── health monitoring API ───────────────────────────────

    def health_dict(self) -> dict:
        """Return health status suitable for the /api/health endpoint."""
        return {
            "mt5_connected": self._connected,
            "health_failures": self._health_failures,
            "last_error": self._last_error,
            "seconds_since_last_success": round(self.seconds_since_last_success, 1),
            "reconnecting": self._reconnect_lock.locked(),
        }
