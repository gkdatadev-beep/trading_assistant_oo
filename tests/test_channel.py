"""Tests for channel/liquidity analysis — synthetic grid OHLC data."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.channel import (
    analyze_channels,
    build_price_profile,
    detect_liquidity_zones,
    find_channel_width,
)


def _grid_ohlc(anchor=100.0, width=5.0, n=600, drift=0.0, seed=7) -> pd.DataFrame:
    """Synthetic candles cycling a known price grid (anchor + k*width)."""
    rng = np.random.default_rng(seed)
    levels = anchor + np.arange(-8, 9) * width
    rows = []
    for i in range(n):
        lv = levels[i % len(levels)] + drift * i
        noise = rng.normal(0.0, width * 0.12)
        o = lv + noise
        h = max(o, lv) + abs(rng.normal(0.3, 0.15))
        l = min(o, lv) - abs(rng.normal(0.3, 0.15))
        c = lv + noise * 0.3
        rows.append({"open": o, "high": h, "low": l, "close": c})
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    return pd.DataFrame(rows, index=idx)


class TestPriceProfile:
    def test_profile_shape(self):
        df = _grid_ohlc()
        profile, edges, centers = build_price_profile(df)
        assert len(profile) == len(centers)
        assert len(edges) == len(profile) + 1
        assert profile.sum() > 0

    def test_profile_peaks_at_grid_levels(self):
        df = _grid_ohlc(anchor=100.0, width=5.0)
        profile, edges, centers = build_price_profile(df)
        zones = detect_liquidity_zones(profile, centers, top_n=6)
        assert len(zones) >= 3
        # Strongest zones must be close to some grid level (k*5 around 100)
        grid = 100.0 + np.arange(-8, 9) * 5.0
        for z in zones[:3]:
            assert np.min(np.abs(grid - z["level"])) < 1.5


class TestChannelWidth:
    def test_width_recovers_grid_spacing(self):
        df = _grid_ohlc(anchor=100.0, width=5.0)
        analysis = analyze_channels(df, min_channel_width=3.0)
        assert 4.2 <= analysis["primary_width"] <= 5.8

    def test_secondary_is_half_of_primary(self):
        for w in (3.0, 5.0, 7.0):
            analysis = analyze_channels(_grid_ohlc(width=w), min_channel_width=3.0)
            assert abs(analysis["secondary_width"] - analysis["primary_width"] / 2) < 1e-9

    def test_default_min_width_keeps_max_10_channels(self):
        # default min = span/10; spacing 5 is doubled to its multiple 10
        analysis = analyze_channels(_grid_ohlc(width=5.0))
        assert 9.0 <= analysis["primary_width"] <= 11.0

    def test_find_width_keys(self):
        df = _grid_ohlc()
        profile, edges, centers = build_price_profile(df)
        width = find_channel_width(profile, centers)
        assert width["width_price"] > 0
        assert "secondary_width_price" in width
        assert abs(width["secondary_width_price"] - width["width_price"] / 2) < 1e-9


class TestStrongLevels:
    def test_strong_levels_align_with_grid(self):
        df = _grid_ohlc(n=400)
        profile, edges, centers = build_price_profile(df)
        from app.core.channel import _select_strong_levels
        levels = _select_strong_levels(profile, centers, min_width=3.0)
        assert len(levels) >= 5
        grid = 100.0 + np.arange(-8, 9) * 5.0
        for lv in levels:
            assert np.min(np.abs(grid - lv)) < 1.5


class TestTrend:
    def test_trend_up(self):
        analysis = analyze_channels(_grid_ohlc(drift=0.02))
        assert analysis["trend"]["direction"] == "ALTA"
        assert analysis["trend"]["slope"] > 0

    def test_trend_down(self):
        analysis = analyze_channels(_grid_ohlc(drift=-0.02))
        assert analysis["trend"]["direction"] == "BAIXA"
        assert analysis["trend"]["slope"] < 0

    def test_trend_r2_high_on_clean_grid(self):
        analysis = analyze_channels(_grid_ohlc(drift=0.0, n=800))
        # Prices oscillate around flat grid → slope near zero, low R2
        assert analysis["trend"]["r2"] < 0.2


class TestRender:
    def test_render_returns_b64_png(self):
        df = _grid_ohlc(n=120)
        analysis = analyze_channels(df)
        from app.core.channel import render_channel_chart
        img = render_channel_chart(df, analysis, tf_label="H4", months=6)
        assert img.startswith("data:image/png;base64,")
        assert len(img) > 10_000
