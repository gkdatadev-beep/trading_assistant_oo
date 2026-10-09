"""Tests for Bill Williams fractal (swing high/low) detection."""
from __future__ import annotations

import pandas as pd

from app.core.fractals import (
    detect_fractals,
    last_swing,
    last_swing_point,
    normalize_candles,
)


def _df(highs, lows=None, freq="1h", start="2026-01-01 00:00"):
    n = len(highs)
    if lows is None:
        lows = [h - 1.0 for h in highs]
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame(
        {
            "open": [h - 0.2 for h in highs],
            "high": highs,
            "low": lows,
            "close": [h - 0.1 for h in highs],
        },
        index=idx,
    )


class TestNormalize:
    def test_valid_values(self):
        assert normalize_candles(3) == 3
        assert normalize_candles(5) == 5
        assert normalize_candles("3") == 3
        assert normalize_candles("5") == 5

    def test_invalid_falls_back_to_5(self):
        assert normalize_candles(7) == 5
        assert normalize_candles(0) == 5
        assert normalize_candles(None) == 5
        assert normalize_candles("x") == 5


class TestDetectFractals:
    def test_swing_high_5_candles(self):
        df = _df([10, 10, 12, 10, 10, 10, 10])
        det = detect_fractals(df, candles=5)
        assert [p["index"] for p in det["swing_highs"]] == [2]
        assert det["swing_highs"][0]["price"] == 12.0

    def test_swing_low_5_candles(self):
        df = _df([15, 15, 15, 15, 15, 15, 15], lows=[10, 10, 8, 10, 10, 10, 10])
        det = detect_fractals(df, candles=5)
        assert [p["index"] for p in det["swing_lows"]] == [2]
        assert det["swing_lows"][0]["price"] == 8.0

    def test_strict_comparison_no_fractal_on_ties(self):
        df = _df([10, 12, 12, 10, 10])
        det = detect_fractals(df, candles=5)
        assert det["swing_highs"] == []

    def test_three_candle_variant(self):
        df = _df([10, 12, 10])
        assert [p["index"] for p in detect_fractals(df, candles=3)["swing_highs"]] == [1]
        # The same data cannot confirm a 5-candle fractal (not enough candles)
        assert detect_fractals(df, candles=5)["swing_highs"] == []

    def test_right_wing_unconfirmed(self):
        # high=9 at index 5 has no right wing of 2 candles → not confirmed
        df = _df([5, 5, 9, 5, 5, 9, 5])
        det = detect_fractals(df, candles=5)
        assert [p["index"] for p in det["swing_highs"]] == [2]

    def test_time_label_hhmm(self):
        df = _df([10, 10, 12, 10, 10])
        det = detect_fractals(df, candles=5)
        assert det["swing_highs"][0]["time"] == "02:00"


class TestLastSwing:
    def test_most_recent_of_each(self):
        df = _df(
            [10, 10, 12, 10, 10, 10, 13, 10, 10],
            lows=[9, 9, 9, 8, 9, 9, 9, 9, 9],
        )
        ls = last_swing(df, candles=5)
        assert ls["swing_high"]["price"] == 13.0
        assert ls["swing_low"]["price"] == 8.0
        assert ls["candles"] == 5

    def test_empty_when_too_short(self):
        ls = last_swing(_df([10, 10, 12, 10]), candles=5)
        assert ls["swing_high"] is None
        assert ls["swing_low"] is None


class TestLastSwingPoint:
    def test_sl_when_it_occurred_last(self):
        # SH em idx 2 (12), SL em idx 3 (8) → o mais recente é o SL
        df = _df([10, 10, 12, 10, 10, 10, 10], lows=[9, 9, 9, 8, 9, 9, 9])
        pt = last_swing_point(df, candles=5)
        assert pt["kind"] == "SL"
        assert pt["price"] == 8.0
        assert pt["index"] == 3

    def test_sh_when_it_occurred_last(self):
        # SH em idx 6 (13), SL em idx 4 (8) → o mais recente é o SH
        df = _df(
            [10, 10, 12, 10, 10, 10, 13, 10, 10],
            lows=[9, 9, 9, 9, 8, 9, 9, 9, 9],
        )
        pt = last_swing_point(df, candles=5)
        assert pt["kind"] == "SH"
        assert pt["price"] == 13.0

    def test_tie_same_candle_prefers_sh(self):
        # SH e SL no mesmo candle (idx 2) → empate favorece SH
        df = _df([10, 10, 12, 10, 10], lows=[10, 10, 8, 10, 10])
        pt = last_swing_point(df, candles=5)
        assert pt["kind"] == "SH"
        assert pt["price"] == 12.0

    def test_none_when_too_short(self):
        assert last_swing_point(_df([10, 10, 12, 10]), candles=5) is None

    def test_only_sl_present(self):
        df = _df([9, 9, 9, 9, 9], lows=[9, 9, 8, 9, 9])
        pt = last_swing_point(df, candles=5)
        assert pt["kind"] == "SL"
        assert pt["price"] == 8.0
