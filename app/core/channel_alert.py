"""Channel breakout alert — Telegram notifications when price crosses the
first-hour channel projections.

The channel comes from the first hour of the server day (12 × M5 candles,
same math as the /chart route). Levels monitored: channel max/min plus
the projected levels above/below (projection distance = 2 × channel width).

Cooldown: after a notification, a new message is only sent when a NEW
crossing occurrence happens at least `cooldown_seconds` after the last one.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from app.core.channel import first_hour_channel_levels

logger = logging.getLogger(__name__)

MIN_CHANNEL_CANDLES = 12  # primeira hora em M5


class ChannelBreakoutMonitor:
    """Background thread watching price against first-hour channel levels."""

    def __init__(
        self,
        symbol: str,
        get_rates_today: Callable[[], Optional[pd.DataFrame]],
        get_server_time: Callable[[], datetime],
        send_alert: Callable[[str], bool],
        cooldown_seconds: float = 300.0,
        get_price: Optional[Callable[[], Optional[float]]] = None,
        poll_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        Parameters
        ----------
        symbol : broker symbol shown in messages.
        get_rates_today : returns today's M5 candles (ascending, time-indexed).
        get_server_time : broker server time (timezone-aware).
        send_alert : callback that delivers the message (Telegram messenger).
        cooldown_seconds : minimum interval between notifications (default 5 min).
        get_price : optional live price (tick bid); falls back to last close.
        clock : monotonic clock (injectable for tests).
        """
        self._symbol = symbol
        self._get_rates_today = get_rates_today
        self._get_server_time = get_server_time
        self._send_alert = send_alert
        self._cooldown = max(float(cooldown_seconds), 0.0)
        self._get_price = get_price
        self._poll = max(float(poll_seconds), 1.0)
        self._clock = clock

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._day_key: Optional[str] = None
        self._levels: list[dict] = []
        self._channel: dict = {}
        self._prev_price: Optional[float] = None
        self._last_alert_mono: Optional[float] = None

    # ── lifecycle ───────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="channel-breakout-alert",
        )
        self._thread.start()
        logger.info("Channel breakout monitor started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logger.warning("Channel breakout check failed", exc_info=True)
            self._stop.wait(self._poll)

    # ── logic ───────────────────────────────────────────────

    def check_once(
        self,
        now: Optional[datetime] = None,
        df: Optional[pd.DataFrame] = None,
    ) -> Optional[dict]:
        """Run one monitoring pass. Returns the alert event dict when a
        message was sent, else None."""
        if now is None:
            now = self._get_server_time()
        day = now.date()

        if df is None:
            df = self._get_rates_today()
        if df is None or len(df) == 0:
            return None
        # Keep only today's candles (server time = UTC in this project)
        normalized = df.index.normalize()
        day_ts = (
            pd.Timestamp(day, tz=normalized.tz)
            if normalized.tz is not None else pd.Timestamp(day)
        )
        df = df[normalized == day_ts]
        if len(df) < MIN_CHANNEL_CANDLES:
            self._prev_price = None  # canal ainda não definido
            return None

        # (Re)build the levels once per day
        if self._day_key != day.isoformat():
            self._channel = first_hour_channel_levels(df)
            self._levels = list(self._channel["levels"])
            self._day_key = day.isoformat()
            self._prev_price = None  # sem alerta retroativo no primeiro dia

        price = None
        if self._get_price is not None:
            try:
                price = self._get_price()
            except Exception:
                price = None
        if price is None:
            price = float(df["close"].iloc[-1])

        prev = self._prev_price
        self._prev_price = price
        if prev is None:
            return None  # primeira amostra do dia — sem cruzamento anterior

        mono = self._clock()
        for lv in self._levels:
            level = float(lv["level"])
            if prev < level <= price:
                direction = "up"
            elif prev > level >= price:
                direction = "down"
            else:
                continue
            # ── cooldown: nova mensagem só com nova ocorrência ≥ 5 min ──
            if self._last_alert_mono is not None and \
                    mono - self._last_alert_mono < self._cooldown:
                logger.info(
                    "Channel crossing %s %s ignored (cooldown %.0fs)",
                    lv["label"], direction,
                    self._cooldown - (mono - self._last_alert_mono),
                )
                return None
            text = self._build_message(now, direction, lv, price)
            if self._send_alert(text):
                self._last_alert_mono = mono
                logger.info(
                    "Channel breakout ALERT: %s %s %.2f @ %s",
                    direction, lv["label"], level, now.strftime("%H:%M:%S"),
                )
                return {
                    "day": day.isoformat(),
                    "time": now.strftime("%H:%M:%S"),
                    "direction": direction,
                    "level": level,
                    "label": lv["label"],
                    "price": price,
                }
            return None
        return None

    def _build_message(
        self, now: datetime, direction: str, lv: dict, price: float,
    ) -> str:
        arrow = "📈 ACIMA" if direction == "up" else "📉 ABAIXO"
        ch = self._channel
        return "\n".join([
            f"🚨 CANAL DA 1ª HORA ROMPIDO — {self._symbol}",
            "",
            f"{arrow} de {lv['level']:.2f} ({lv['label']})",
            f"Preço atual: {price:.2f}",
            f"Canal 1h: {ch['channel_low']:.2f} – {ch['channel_high']:.2f} "
            f"(W={ch['channel_width']:.2f})",
            f"Projeção: {ch['projection_distance']:.2f}",
            f"Hora (servidor): {now.strftime('%H:%M:%S')}",
        ])
