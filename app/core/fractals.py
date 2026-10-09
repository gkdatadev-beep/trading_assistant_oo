"""Bill Williams fractal — swing high / swing low detection.

A fractal needs the full window of `candles` candles (3 or 5):

  • Swing High (SH): the middle candle has the HIGHEST high of the window.
  • Swing Low  (SL): the middle candle has the LOWEST low of the window.

The signal is only "confirmed" once the right wing closes, so the last
(candles - 1) // 2 candles can never produce a confirmed fractal.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

VALID_CANDLES = (3, 5)


def normalize_candles(candles) -> int:
    """Coerce a candle-window value to a valid fractal size (3 or 5)."""
    try:
        n = int(candles)
    except (TypeError, ValueError):
        return 5
    return n if n in VALID_CANDLES else 5


def _fmt_time(ts) -> str:
    """HH:MM (server time) label for a pandas timestamp/index entry."""
    try:
        return pd.Timestamp(ts).strftime("%H:%M")
    except Exception:
        return str(ts)


def _iso_time(ts) -> Optional[str]:
    try:
        return pd.Timestamp(ts).isoformat()
    except Exception:
        return None


def detect_fractals(df: pd.DataFrame, candles=5) -> dict:
    """Detect confirmed Bill Williams fractals on an OHLC DataFrame.

    Parameters
    ----------
    df : DataFrame indexed by time with columns high/low (open/close optional).
    candles : 3 or 5 — total candles in the fractal window.

    Returns
    -------
    {"candles": int, "swing_highs": [{"index","time","iso","price"}, ...],
     "swing_lows": [...]} — both lists sorted by index.
    """
    n = normalize_candles(candles)
    wing = (n - 1) // 2
    if len(df) < n:
        return {"candles": n, "swing_highs": [], "swing_lows": []}

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    swing_highs: list[dict] = []
    swing_lows: list[dict] = []

    for i in range(wing, len(df) - wing):
        h = float(highs[i])
        l = float(lows[i])
        if h > float(highs[i - wing:i].max()) and \
           h > float(highs[i + 1:i + wing + 1].max()):
            swing_highs.append({
                "index": int(i),
                "time": _fmt_time(df.index[i]),
                "iso": _iso_time(df.index[i]),
                "price": round(h, 2),
            })
        if l < float(lows[i - wing:i].min()) and \
           l < float(lows[i + 1:i + wing + 1].min()):
            swing_lows.append({
                "index": int(i),
                "time": _fmt_time(df.index[i]),
                "iso": _iso_time(df.index[i]),
                "price": round(l, 2),
            })

    return {"candles": n, "swing_highs": swing_highs, "swing_lows": swing_lows}


def last_swing(df: pd.DataFrame, candles=5) -> dict:
    """Most recent CONFIRMED swing high and swing low.

    Returns {"swing_high": point|None, "swing_low": point|None, "candles": int}.
    """
    det = detect_fractals(df, candles)
    return {
        "swing_high": det["swing_highs"][-1] if det["swing_highs"] else None,
        "swing_low": det["swing_lows"][-1] if det["swing_lows"] else None,
        "candles": det["candles"],
    }


def last_swing_point(df: pd.DataFrame, candles=5) -> Optional[dict]:
    """Most recent CONFIRMED swing — SH or SL, whichever occurred last.

    Returns {"kind": "SH"|"SL", "index", "time", "iso", "price"} or None.
    On a tie (same candle), SH wins.
    """
    ls = last_swing(df, candles)
    sh = ls["swing_high"]
    sl = ls["swing_low"]
    if sh and sl:
        return dict(sh, kind="SH") if sh["index"] >= sl["index"] else dict(sl, kind="SL")
    if sh:
        return dict(sh, kind="SH")
    if sl:
        return dict(sl, kind="SL")
    return None
