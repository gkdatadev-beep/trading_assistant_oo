"""Higher-timeframe channel & liquidity-zone detection from OHLC data.

Approach:
  1. Build a price profile: frequency of OHLC occurrences per price bin
     (exact O/H/L/C levels weight 1.0, candle range coverage weight 0.25).
     High-frequency bands indicate liquidity-relevant price zones.
  2. Detect liquidity zones as profile peaks (local maxima).
  3. Find the ideal channel width via autocorrelation of the profile
     (dominant periodic spacing between relevant levels); the secondary
     channel is 50% of the primary width.
  4. Fit a linear regression on typical prices to define movement direction.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ── profile ────────────────────────────────────────────────────────────────

def build_price_profile(
    df: pd.DataFrame,
    bin_size: Optional[float] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Accumulate OHLC occurrence frequency per price bin.

    Returns (profile, bin_edges, bin_centers).
    """
    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)

    lo = float(lows.min())
    hi = float(highs.max())
    if bin_size is None or bin_size <= 0:
        median_range = float(np.percentile(highs - lows, 50)) * 0.25
        bin_size = max(median_range, (hi - lo) / 150.0)

    n_bins = int(np.ceil((hi - lo) / bin_size)) + 1
    edges = lo + np.arange(n_bins + 1) * bin_size
    centers = (edges[:-1] + edges[1:]) / 2.0
    profile = np.zeros(n_bins, dtype=float)

    for o, h, l, c in zip(opens, highs, lows, closes):
        # Exact OHLC level occurrences
        for v in (o, h, l, c):
            idx = int((v - lo) / bin_size)
            profile[min(max(idx, 0), n_bins - 1)] += 1.0
        # Candle range coverage (liquidity touch) — lighter weight
        i0 = int((l - lo) / bin_size)
        i1 = int((h - lo) / bin_size)
        i0 = min(max(i0, 0), n_bins - 1)
        i1 = min(max(i1, 0), n_bins - 1)
        if i1 >= i0:
            profile[i0:i1 + 1] += 0.25

    return profile, edges, centers


# ── liquidity zones ────────────────────────────────────────────────────────

def detect_liquidity_zones(
    profile: np.ndarray,
    centers: np.ndarray,
    top_n: int = 6,
) -> list[dict]:
    """Peak-pick the profile → liquidity zones sorted by frequency weight."""
    p = np.asarray(profile, dtype=float)
    n = len(p)
    if n == 0:
        return []
    kernel = np.ones(3) / 3.0
    s = np.convolve(p, kernel, mode="same")

    peaks: list[int] = []
    for i in range(1, n - 1):
        if s[i] >= s[i - 1] and s[i] > s[i + 1]:
            peaks.append(i)
    if not peaks:
        peaks = [int(np.argmax(p))]

    peaks.sort(key=lambda i: float(p[i]), reverse=True)
    zones: list[dict] = []
    used: set[int] = set()
    for i in peaks:
        if any(abs(i - u) <= 1 for u in used):
            continue
        used.add(i)
        zones.append({
            "level": float(centers[i]),
            "weight": float(p[i]),
            "bin": int(i),
        })
    zones.sort(key=lambda z: z["weight"], reverse=True)
    return zones[:top_n]


# ── channel width ──────────────────────────────────────────────────────────

def _select_strong_levels(
    profile: np.ndarray,
    centers: np.ndarray,
    min_width: float,
) -> list[float]:
    """Select the strongest price levels by occurrence frequency.

    1. Profile local maxima as candidate levels.
    2. Merge levels closer than 60% of min_width (keep the stronger one).
    3. Separation point: largest relative drop in weight between consecutive
       ranked levels — everything above that drop is a strong level.
    """
    n = len(profile)
    if n < 3:
        return []
    s = np.convolve(profile, np.ones(3) / 3.0, mode="same")
    peaks = [i for i in range(1, n - 1) if s[i] >= s[i - 1] and s[i] > s[i + 1]]
    if not peaks:
        return []
    bin_w = float(centers[1] - centers[0]) if len(centers) > 1 else 1.0
    sep_bins = max(1, int(round((min_width * 0.6) / bin_w)))

    peaks.sort(key=lambda i: float(profile[i]), reverse=True)
    kept: list[int] = []
    for i in peaks:
        if all(abs(i - u) > sep_bins for u in kept):
            kept.append(i)

    kept.sort(key=lambda i: float(profile[i]), reverse=True)
    if len(kept) <= 2:
        kept.sort()
        return [float(centers[i]) for i in kept]

    weights = [float(profile[i]) for i in kept]
    if weights[0] <= weights[-1] * 1.3:
        # No clear separation — all merged peaks are strong
        kept.sort()
        return [float(centers[i]) for i in kept]

    # Separation point: biggest relative drop separates strong from weak
    ratios = [weights[k - 1] / max(weights[k], 1e-9) for k in range(1, len(weights))]
    cut = int(np.argmax(ratios)) + 1
    strong = kept[:max(cut, 2)]
    strong.sort()
    return [float(centers[i]) for i in strong]


def _dominant_spacing(levels: list[float], min_width: float, tolerance: float = 0.15) -> Optional[float]:
    """Most common spacing between consecutive strong levels."""
    diffs = [b - a for a, b in zip(levels, levels[1:]) if (b - a) >= min_width * 0.5]
    if not diffs:
        return None
    if len(diffs) == 1:
        return float(diffs[0])
    clusters: list[dict] = []
    for d in sorted(diffs):
        placed = False
        for cl in clusters:
            if abs(d - cl["center"]) <= tolerance * max(cl["center"], 1e-9):
                cl["vals"].append(d)
                cl["center"] = float(np.median(cl["vals"]))
                placed = True
                break
        if not placed:
            clusters.append({"center": d, "vals": [d]})
    best = max(clusters, key=lambda cl: len(cl["vals"]))
    if len(best["vals"]) >= 2:
        return float(np.median(best["vals"]))
    return float(np.median(diffs))


def _autocorr_lag(
    profile: np.ndarray,
    bin_w: float,
    min_width: float,
    min_lag_bins: int,
) -> tuple[int, float]:
    """Fallback: autocorrelation peak inside a window around min_width."""
    p = np.asarray(profile, dtype=float)
    p = p - p.mean()
    denom = float(np.dot(p, p))
    if denom == 0:
        return 0, 0.0
    ac = np.correlate(p, p, mode="full")[len(p) - 1:] / denom
    lo = max(min_lag_bins, int((min_width * 0.5) / bin_w))
    hi = min(len(ac) - 1, int((min_width * 4.0) / bin_w))
    if lo >= len(ac) or hi <= lo:
        return 0, 0.0
    lag = int(np.argmax(ac[lo:hi + 1])) + lo
    return lag, float(ac[lag])


def find_channel_width(
    profile: np.ndarray,
    centers: np.ndarray,
    min_width: Optional[float] = None,
    min_lag_bins: int = 2,
) -> dict:
    """Ideal channel width from the strongest price levels.

    Primary method: dominant spacing between the strongest levels
    (separation-point subset). Fallback: profile autocorrelation.
    The width is then doubled (channel multiples) until >= min_width.
    Secondary channel = 50% of the primary width.
    """
    n = len(profile)
    span = float(centers[-1] - centers[0]) if n > 1 else 0.0
    if span <= 0:
        return {"lag_bins": 0, "width_price": 0.0, "score": 0.0,
                "double_score": 0.0, "secondary_width_price": 0.0}
    if min_width is None or min_width <= 0:
        min_width = span / 10.0  # at most ~10 channels inside the data range
    bin_w = float(centers[1] - centers[0]) if n > 1 else 1.0

    candidate: Optional[float] = None
    score = 0.0
    lag_bins = 0

    strong = _select_strong_levels(profile, centers, min_width)
    if len(strong) >= 2:
        candidate = _dominant_spacing(strong, min_width)

    if candidate is None:
        lag_bins, score = _autocorr_lag(profile, bin_w, min_width, min_lag_bins)
        if lag_bins > 0:
            candidate = float(lag_bins * bin_w)

    if not candidate or candidate <= 0:
        candidate = span

    # Channel multiples: double until the width satisfies the minimum
    while candidate < min_width:
        candidate *= 2.0

    return {
        "lag_bins": int(lag_bins),
        "width_price": float(candidate),
        "score": float(score),
        "double_score": 0.0,
        "secondary_width_price": float(candidate) / 2.0,
    }


# ── trend ──────────────────────────────────────────────────────────────────

def regression_trend(df: pd.DataFrame) -> dict:
    """Linear regression on typical price (H+L+C)/3."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    y = tp.to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = intercept + slope * x
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return {
        "slope": float(slope),
        "intercept": float(intercept),
        "direction": "ALTA" if slope > 0 else "BAIXA",
        "r2": float(r2),
    }


# ── combined analysis ──────────────────────────────────────────────────────

def analyze_channels(
    df: pd.DataFrame,
    top_zones: int = 6,
    min_channel_width: Optional[float] = None,
) -> dict:
    """Full channel analysis for a higher-timeframe OHLC DataFrame."""
    profile, edges, centers = build_price_profile(df)
    zones = detect_liquidity_zones(profile, centers, top_n=top_zones)
    width = find_channel_width(profile, centers, min_width=min_channel_width)
    if zones:
        anchor = zones[0]["level"]
    else:
        anchor = float(np.median((df["high"] + df["low"]) / 2.0))
    bin_size = float(centers[1] - centers[0]) if len(centers) > 1 else 0.0
    return {
        "profile": profile.tolist(),
        "centers": centers.tolist(),
        "bin_size": bin_size,
        "zones": zones,
        "anchor": float(anchor),
        "primary_width": float(width["width_price"]),
        "secondary_width": float(width["secondary_width_price"]),
        "width_score": float(width["score"]),
        "width_double_score": float(width["double_score"]),
        "trend": regression_trend(df),
    }


# ── plotting ───────────────────────────────────────────────────────────────

def render_channel_chart(
    df: pd.DataFrame,
    analysis: dict,
    tf_label: str = "H4",
    months: int = 6,
    symbol: str = "XAUUSD",
) -> str:
    """Render candles + horizontal channel lines only → PNG b64.

    Primary channel: strong gold solid lines.
    Secondary channel (50% of primary): dimmer dashed lines.
    """
    import base64
    from io import BytesIO

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    closes = df["close"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    t = np.arange(len(df))

    anchor = float(analysis["anchor"])
    W = float(analysis["primary_width"])
    W2 = float(analysis["secondary_width"])

    price_lo = float(lows.min())
    price_hi = float(highs.max())
    pad = (price_hi - price_lo) * 0.05
    ylo = price_lo - pad
    yhi = price_hi + pad

    fig, ax = plt.subplots(figsize=(16, 8))
    fig.patch.set_facecolor("#0d0d12")
    ax.set_facecolor("#0d0d12")

    # Candles
    min_body = max((price_hi - price_lo) * 0.001, 1e-6)
    for i in range(len(df)):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        color = "#00c853" if c >= o else "#ff3d3d"
        ax.vlines(i, l, h, color=color, linewidth=0.9, alpha=0.95)
        body_bottom = min(o, c)
        body_height = max(abs(c - o), min_body)
        ax.add_patch(plt.Rectangle(
            (i - 0.3, body_bottom), 0.6, body_height, color=color, alpha=0.85))

    # Horizontal channel lines only
    if W > 0:
        k0 = int(np.floor((ylo - anchor) / W))
        k1 = int(np.ceil((yhi - anchor) / W))
        for k in range(k0, k1 + 1):
            lv = anchor + k * W
            if ylo <= lv <= yhi:
                ax.axhline(lv, color="#FFD700", linewidth=1.6, alpha=1.0)
            lv2 = anchor + (k + 0.5) * W
            if ylo <= lv2 <= yhi:
                ax.axhline(lv2, color="#8a6d1f", linewidth=1.0,
                           linestyle="--", alpha=0.55)

    ax.set_xlim(float(t[0]), float(t[-1]))
    ax.set_ylim(ylo, yhi)
    tick_idx = np.linspace(0, len(df) - 1, 8).astype(int)
    ax.set_xticks(tick_idx)
    ax.set_xticklabels(
        [df.index[i].strftime("%d/%m/%y") for i in tick_idx], fontsize=8)
    ax.tick_params(colors="#e0e0e0", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    ax.set_title(
        f"Canais {symbol} {tf_label} — {months} meses | Canal: {W:.2f} | "
        f"Sub-canal 50%: {W2:.2f}",
        color="#D4AF37", fontsize=12)

    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")
