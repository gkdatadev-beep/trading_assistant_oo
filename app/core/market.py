"""Market analysis — pure domain logic, no MT5 dependency."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TradingWindow:
    start_minute: int   # minutes from midnight (UTC)
    end_minute: int     # minutes from midnight (UTC)

    def contains(self, minute_of_day: int) -> bool:
        if self.start_minute == self.end_minute:
            return True
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        # Wraps around midnight
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


class MarketAnalyzer:
    """Trading windows, indicators — pure domain logic."""

    def __init__(
        self,
        window1_start: str = "01:00",
        window1_duration_hours: int = 5,
        window2_start: str = "15:00",
        window2_duration_hours: int = 5,
    ):
        self._windows = self._build_windows(
            window1_start, window1_duration_hours,
            window2_start, window2_duration_hours,
        )

    # ── trading windows ─────────────────────────────────────

    @staticmethod
    def _build_windows(
        ws1: str, d1: int, ws2: str, d2: int,
    ) -> list[TradingWindow]:
        result: list[TradingWindow] = []
        for start_str, hours in [(ws1, d1), (ws2, d2)]:
            h, m = map(int, start_str.split(":"))
            start_min = h * 60 + m
            end_min = (start_min + max(int(hours), 1) * 60) % (24 * 60)
            result.append(TradingWindow(start_min, end_min))
        return result

    def is_outside_trading_hours(self, server_time: datetime) -> bool:
        """True when server_time falls outside ALL configured windows."""
        minute_of_day = server_time.hour * 60 + server_time.minute
        for window in self._windows:
            if window.contains(minute_of_day):
                return False
        return True

    # ── indicators (operate on DataFrames, no MT5) ──────────

    @staticmethod
    def build_day_bounds(server_time: datetime) -> tuple[datetime, datetime]:
        """Return (day_start, day_end) for the given server time."""
        day_start = datetime(
            server_time.year, server_time.month, server_time.day,
        )
        day_end = day_start + timedelta(days=1) - timedelta(seconds=1)
        return day_start, day_end

    @staticmethod
    def format_friendly(server_time: datetime) -> str:
        """'Sex, 30 Jul 14:00' in Portuguese."""
        weekdays = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]
        months = [
            "Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
            "Jul", "Ago", "Set", "Out", "Nov", "Dez",
        ]
        return (
            f"{weekdays[server_time.weekday()]}, "
            f"{server_time.day:02d} {months[server_time.month - 1]} "
            f"{server_time.hour:02d}:{server_time.minute:02d}"
        )
