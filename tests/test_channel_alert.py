"""Tests for the first-hour channel breakout monitor (Telegram alerts)."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.core.channel import first_hour_channel_levels
from app.core.channel_alert import ChannelBreakoutMonitor, MIN_CHANNEL_CANDLES


def _m5_day(n=30, first_high=3352.0, first_low=3348.0, tail_step=2.0):
    """M5 candles: first hour defines channel (high/low), then rising tail."""
    rows = []
    for i in range(n):
        if i < 12:
            rows.append({"open": 3350.0, "high": first_high, "low": first_low,
                         "close": 3350.0})
        else:
            p = 3350.0 + (i - 11) * tail_step
            rows.append({"open": p - 0.5, "high": p + 1.0, "low": p - 1.0,
                         "close": p})
    idx = pd.date_range("2026-08-26 00:00", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(rows, index=idx)


class TestFirstHourLevels:
    def test_levels_match_chart_math(self):
        fh = first_hour_channel_levels(_m5_day())
        assert fh["channel_high"] == 3352.0
        assert fh["channel_low"] == 3348.0
        assert fh["channel_width"] == 4.0
        assert fh["projection_distance"] == 8.0
        by_label = {lv["label"]: lv["level"] for lv in fh["levels"]}
        assert by_label["Canal 1h - Min"] == 3348.0
        assert by_label["Canal 1h - Max"] == 3352.0
        assert by_label["Proj +1"] == 3360.0
        assert by_label["Proj +2"] == 3368.0
        assert by_label["Proj +3"] == 3376.0
        assert by_label["Proj -1"] == 3340.0
        assert by_label["Proj -2"] == 3332.0
        assert by_label["Proj -3"] == 3324.0
        # ascending order
        levels = [lv["level"] for lv in fh["levels"]]
        assert levels == sorted(levels)


def _make_monitor(df, sent, clock_state, prices, cooldown=300.0):
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

    def get_price():
        return prices.pop(0) if prices else None

    mon = ChannelBreakoutMonitor(
        symbol="XAUUSD",
        get_rates_today=lambda: df,
        get_server_time=lambda: now,
        send_alert=lambda m: sent.append(m) or True,
        cooldown_seconds=cooldown,
        get_price=get_price,
        clock=lambda: clock_state[0],
    )
    return mon


class TestCrossingAlerts:
    def test_first_sample_does_not_alert(self):
        sent = []
        mon = _make_monitor(_m5_day(), sent, [100.0], [3350.0])
        assert mon.check_once() is None
        assert sent == []

    def test_crossing_up_sends_alert(self):
        sent = []
        mon = _make_monitor(_m5_day(), sent, [100.0], [3350.0, 3353.0])
        mon.check_once()  # primeiro preço — sem alerta
        event = mon.check_once()
        assert event is not None
        assert event["direction"] == "up"
        assert event["level"] == 3352.0
        assert len(sent) == 1
        assert "ACIMA" in sent[0]
        assert "Canal 1h - Max" in sent[0]

    def test_crossing_down_sends_alert(self):
        sent = []
        mon = _make_monitor(_m5_day(), sent, [100.0], [3350.0, 3347.5])
        mon.check_once()
        event = mon.check_once()
        assert event is not None
        assert event["direction"] == "down"
        assert event["level"] == 3348.0
        assert "ABAIXO" in sent[0]

    def test_cooldown_blocks_new_crossing_within_5_min(self):
        sent = []
        clock_state = [100.0]
        prices = [3350.0, 3353.0, 3361.0]
        mon = _make_monitor(_m5_day(), sent, clock_state, prices, cooldown=300.0)
        mon.check_once()
        event = mon.check_once()
        assert event is not None  # 1º alerta
        # cruza Proj +1 (3360) logo em seguida — dentro do cooldown
        assert mon.check_once() is None
        assert len(sent) == 1

    def test_new_occurrence_after_cooldown_alerts_again(self):
        sent = []
        clock_state = [100.0]
        prices = [3350.0, 3353.0, 3361.0, 3369.0]
        mon = _make_monitor(_m5_day(), sent, clock_state, prices, cooldown=300.0)
        mon.check_once()
        assert mon.check_once() is not None  # alerta 1 (Canal Max)
        assert mon.check_once() is None      # Proj +1 dentro do cooldown
        clock_state[0] += 301.0              # 5+ minutos depois
        event = mon.check_once()             # nova ocorrência: Proj +2 (3368)
        assert event is not None
        assert event["level"] == 3368.0
        assert len(sent) == 2

    def test_no_alert_when_channel_not_formed(self):
        sent = []
        short_df = _m5_day(n=MIN_CHANNEL_CANDLES - 2)
        mon = _make_monitor(short_df, sent, [100.0], [3350.0, 3400.0])
        assert mon.check_once() is None
        assert mon.check_once() is None
        assert sent == []

    def test_returns_none_without_data(self):
        sent = []
        mon = _make_monitor(None, sent, [100.0], [3350.0])
        assert mon.check_once() is None
