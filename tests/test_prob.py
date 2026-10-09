"""Tests for the probe probability engine (pure logic, no MT5)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.prob import (
    ProbeProbabilityEngine,
    cyclic_minutes,
    provisional_direction,
)


class TestCyclicMinutes:
    def test_same_hour(self):
        assert cyclic_minutes(555, 570) == 15

    def test_wrap_midnight(self):
        assert cyclic_minutes(10, 1430) == 20

    def test_half_day(self):
        assert cyclic_minutes(0, 720) == 720


class TestProvisionalDirection:
    def test_up_first(self):
        candles = [
            {"tod": 60, "high": 12.0, "low": 9.0, "close": 11.0},   # máxima aqui
            {"tod": 75, "high": 11.0, "low": 8.0, "close": 8.5},    # mínima aqui
        ]
        assert provisional_direction(candles) == "UP"

    def test_down_first(self):
        candles = [
            {"tod": 60, "high": 10.0, "low": 6.0, "close": 7.0},     # mínima aqui
            {"tod": 75, "high": 12.0, "low": 8.0, "close": 11.0},    # máxima aqui
        ]
        assert provisional_direction(candles) == "DOWN"

    def test_undefined_single_candle(self):
        candles = [{"tod": 60, "high": 10.0, "low": 5.0, "close": 7.0}]
        assert provisional_direction(candles) is None

    def test_empty(self):
        assert provisional_direction([]) is None


class TestProbeProbabilityEngine:
    @staticmethod
    def _make_csv(tmp: Path, days: int = 120) -> Path:
        """Histórico sintético: passeio aleatório, 92 velas M15 por dia."""
        rng = np.random.default_rng(7)
        rows = []
        for d in range(days):
            base = pd.Timestamp("2026-01-01") + pd.Timedelta(days=d)
            price = 4000.0
            for h in range(1, 24):
                for m in (0, 15, 30, 45):
                    o = price
                    price += rng.normal(0.0, 1.5)
                    hi = max(o, price) + abs(rng.normal(0.0, 0.4))
                    lo = min(o, price) - abs(rng.normal(0.0, 0.4))
                    rows.append({
                        "time": base + pd.Timedelta(hours=h, minutes=m),
                        "open": o, "high": hi, "low": lo, "close": price,
                        "tick_volume": 100,
                    })
        path = tmp / "m15_test.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        return path

    def test_disabled_without_file(self):
        engine = ProbeProbabilityEngine(history_csv=Path("nao_existe.csv"))
        result = engine.compute(
            datetime(2026, 1, 2, 9, 15),
            [{"tod": 60, "high": 1.0, "low": 1.0, "close": 1.0}],
        )
        assert result is None

    def test_compute_basic(self, tmp_path):
        path = self._make_csv(tmp_path)
        engine = ProbeProbabilityEngine(history_csv=path)
        candles = [
            {"tod": 60, "high": 4005.0, "low": 3990.0, "close": 4004.0},
            {"tod": 75, "high": 4006.0, "low": 3995.0, "close": 3996.0},
        ]
        result = engine.compute(datetime(2026, 5, 1, 9, 15), candles)
        assert result is not None
        assert result["ok"] is True
        assert result["n"] > 0
        assert result["bias"] in ("BUY", "SELL")
        assert 0.0 <= result["p_tp"] <= 1.0
        assert 0.0 <= result["p_sl"] <= 1.0
        assert 0.0 <= result["p_up_1h"] <= 1.0

    def test_no_definition(self, tmp_path):
        path = self._make_csv(tmp_path)
        engine = ProbeProbabilityEngine(history_csv=path)
        candles = [{"tod": 60, "high": 4005.0, "low": 3990.0, "close": 4000.0}]
        result = engine.compute(datetime(2026, 5, 1, 1, 15), candles)
        assert result is not None
        assert result["ok"] is False

    def test_does_not_use_today_as_analogue(self, tmp_path):
        path = self._make_csv(tmp_path)
        engine = ProbeProbabilityEngine(history_csv=path)
        candles = [
            {"tod": 60, "high": 4005.0, "low": 3990.0, "close": 4004.0},
            {"tod": 75, "high": 4006.0, "low": 3995.0, "close": 3996.0},
        ]
        # "hoje" = último dia do CSV sintético (2026-05-01)
        result = engine.compute(datetime(2026, 5, 1, 9, 15), candles)
        assert result is not None
        assert result["ok"] is True
        # com 120 dias, excluir 1 ainda deixa amostra cheia
        assert result["n"] <= 40
