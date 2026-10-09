"""Tests for the first-hour channel auto-signal layer."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.core.auto_signal import (
    AutoSignalEngine,
    MIN_CHANNEL_CANDLES,
    compute_signal_plan,
    compute_volume,
    render_signal_chart,
)
from app.core.channel import first_hour_channel_levels


def _m5_day(n=30, first_high=3352.0, first_low=3348.0, tail_step=2.0,
            tail_dir=1.0):
    """M5 candles: first hour defines channel (high/low), then a tail."""
    rows = []
    for i in range(n):
        if i < 12:
            rows.append({"open": 3350.0, "high": first_high, "low": first_low,
                         "close": 3350.0})
        else:
            p = 3350.0 + (i - 11) * tail_step * tail_dir
            rows.append({"open": p - 0.5, "high": p + 1.0, "low": p - 1.0,
                         "close": p})
    idx = pd.date_range("2026-08-26 00:00", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(rows, index=idx)


def _make_engine(df, signals, prices, now=None, buffer=0.5, end_hour=23):
    """Engine wired with injected data; returns (engine, now)."""
    now = now or datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

    def get_price():
        return prices.pop(0) if prices else None

    engine = AutoSignalEngine(
        symbol="XAUUSD",
        get_rates_today=lambda: df,
        get_server_time=lambda: now,
        on_signal=lambda m: signals.append(m),
        get_price=get_price,
        entry_buffer=buffer,
        end_hour=end_hour,
    )
    return engine, now


class TestSignalPlan:
    def test_buy_plan_tp_even_sl_odd(self):
        fh = first_hour_channel_levels(_m5_day())
        plan = compute_signal_plan(fh, day_high=3370.0, day_low=3346.0,
                                   direction="buy")
        assert plan["tp"] == 3368.0          # ch_high 3352 + 2×8
        assert plan["sl"] == 3338.0          # day_low 3346 − 1×8
        assert plan["trigger"] == 3370.5     # day_high + 0.5
        assert plan["tp_label"] == "Proj +2"

    def test_sell_plan_is_mirror(self):
        fh = first_hour_channel_levels(_m5_day())
        plan = compute_signal_plan(fh, day_high=3370.0, day_low=3346.0,
                                   direction="sell")
        assert plan["tp"] == 3332.0          # ch_low 3348 − 2×8
        assert plan["sl"] == 3378.0          # day_high + 1×8
        assert plan["trigger"] == 3345.5     # day_low − 0.5
        assert plan["tp_label"] == "Proj −2"


class TestVolume:
    def test_risk_based_volume(self):
        # stop 10 USD × 100 oz = 1000 USD/lote → risco 50 → 0.05 lote
        v = compute_volume(10.0, 100.0, 50.0)
        assert v == 0.05

    def test_clamps_to_lot_cap(self):
        # stop 2 USD → 25 lotes brutos; cap 0.15 limita
        v = compute_volume(2.0, 100.0, 50.0, lot_cap=0.15)
        assert v == 0.15

    def test_floor_to_volume_step(self):
        # raw = 0.0333 → step 0.01 → 0.03
        v = compute_volume(15.0, 100.0, 50.0, vol_step=0.01)
        assert v == 0.03

    def test_min_volume_floor(self):
        v = compute_volume(500.0, 100.0, 50.0, vol_min=0.01)
        assert v == 0.01

    def test_invalid_inputs_return_zero(self):
        assert compute_volume(0.0, 100.0, 50.0) == 0.0
        assert compute_volume(10.0, 0.0, 50.0) == 0.0
        assert compute_volume(10.0, 100.0, 0.0) == 0.0


class TestEngine:
    def test_no_signal_before_channel_formed(self):
        short = _m5_day(n=MIN_CHANNEL_CANDLES - 2)
        signals = []
        engine, _ = _make_engine(short, signals, [3400.0, 3450.0])
        assert engine.check_once() is None
        assert engine.check_once() is None
        assert signals == []
        assert engine.status()["channel_ready"] is False

    def test_buy_signal_fires_on_day_high_plus_buffer(self):
        # canal 3352/3348; tail sobe 2.0/candle → máx do dia = 3389
        df = _m5_day(n=30, tail_step=2.0)
        day_high = float(df["high"].max())
        day_low = float(df["low"].min())
        signals = []
        prices = [3351.5, 3353.0, day_high + 0.6]
        engine, _ = _make_engine(df, signals, prices)
        assert engine.check_once() is None   # 1ª amostra
        assert engine.check_once() is None   # arma compra (rompeu 3352), sem gatilho
        sig = engine.check_once()
        assert sig is not None
        assert sig["direction"] == "buy"
        assert sig["price"] == day_high + 0.6
        assert sig["tp"] == 3368.0           # Proj +2
        assert sig["sl"] == round(day_low - 8.0, 2)
        assert sig["trigger"] == round(day_high + 0.5, 2)
        assert len(signals) == 1

    def test_no_fire_below_trigger(self):
        df = _m5_day(n=30, tail_step=2.0)
        day_high = float(df["high"].max())
        signals = []
        # sobe um pouco acima do canal, mas fica abaixo de day_high+0.5
        prices = [3351.5, 3353.0]
        engine, _ = _make_engine(df, signals, prices)
        engine.check_once()
        assert engine.check_once() is None
        assert signals == []
        assert engine.status()["armed"] == "buy"

    def test_sell_signal_is_mirror(self):
        df = _m5_day(n=30, tail_step=2.0, tail_dir=-1.0)
        day_high = float(df["high"].max())
        day_low = float(df["low"].min())
        signals = []
        prices = [3350.0, 3347.5, day_low - 1.2]
        engine, _ = _make_engine(df, signals, prices)
        engine.check_once()
        assert engine.check_once() is None   # arma venda, sem gatilho
        sig = engine.check_once()
        assert sig is not None
        assert sig["direction"] == "sell"
        assert sig["tp"] == 3332.0           # Proj −2
        assert sig["sl"] == round(day_high + 8.0, 2)

    def test_one_signal_per_day(self):
        df = _m5_day(n=30, tail_step=2.0)
        day_high = float(df["high"].max())
        signals = []
        prices = [3351.5, day_high + 0.6, day_high + 2.0, day_high + 3.0]
        engine, _ = _make_engine(df, signals, prices)
        engine.check_once()
        assert engine.check_once() is not None  # arma + dispara
        assert engine.check_once() is None      # já disparou hoje
        assert engine.check_once() is None
        assert len(signals) == 1

    def test_no_arming_after_end_hour(self):
        df = _m5_day(n=30, tail_step=2.0)
        day_high = float(df["high"].max())
        signals = []
        now = datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc)  # ≥ 12h
        prices = [3353.0, day_high + 2.0]
        engine, _ = _make_engine(df, signals, prices, now=now, end_hour=12)
        engine.check_once()
        assert engine.check_once() is None   # cutoff — não arma
        assert signals == []
        assert engine.status()["cutoff_reached"] is True

    def test_returns_none_without_data(self):
        signals = []
        engine, _ = _make_engine(None, signals, [3350.0])
        assert engine.check_once() is None

    def test_status_exposes_plans_and_channel(self):
        df = _m5_day(n=30, tail_step=2.0)
        engine, _ = _make_engine(df, [], [3351.0])
        engine.check_once()
        st = engine.status()
        assert st["channel_ready"] is True
        assert st["channel"]["channel_high"] == 3352.0
        assert st["plan"]["buy"]["tp"] == 3368.0
        assert st["plan"]["sell"]["sl"] == round(
            float(df["high"].max()) + 8.0, 2)


def _fired_signal(direction="buy", **over):
    base = {
        "direction": direction,
        "day": "2026-08-26",
        "time": "10:00:00",
        "price": 3390.0,
        "trigger": 3389.5,
        "tp": 3368.0 if direction == "buy" else 3332.0,
        "sl": 3340.0 if direction == "buy" else 3360.0,
        "tp_label": "Proj +2" if direction == "buy" else "Proj −2",
        "sl_label": "mín. dia − 1×proj" if direction == "buy"
        else "máx. dia + 1×proj",
        "channel_high": 3352.0,
        "channel_low": 3348.0,
        "channel_width": 4.0,
        "projection_distance": 8.0,
        "day_high": 3389.0,
        "day_low": 3348.0,
        "stop_distance": 50.0,
    }
    base.update(over)
    return base


class TestSignalChart:
    def test_render_chart_returns_png(self):
        df = _m5_day(n=30, tail_step=2.0)
        png = render_signal_chart(df, _fired_signal("buy"), "XAUUSD")
        assert png is not None
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_chart_sell(self):
        df = _m5_day(n=30, tail_step=2.0, tail_dir=-1.0)
        png = render_signal_chart(df, _fired_signal("sell"), "XAUUSD")
        assert png is not None
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_chart_none_for_short_data(self):
        short = _m5_day(n=MIN_CHANNEL_CANDLES - 2)
        assert render_signal_chart(short, _fired_signal("buy")) is None

    def test_render_chart_none_for_missing_data(self):
        assert render_signal_chart(None, _fired_signal("buy")) is None
