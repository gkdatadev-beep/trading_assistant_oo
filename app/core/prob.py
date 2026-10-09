"""ProbeProbabilityEngine — probabilidades empíricas para o Profit Probe.

Abordagem (transparente e explicável, sem modelo opaco):

  contexto = (minuto do dia no relógio do servidor, direção provisória do dia)

  1. Direção provisória: qual extremo do dia veio PRIMEIRO até agora
     (máxima provisória primeiro → ALTA definida; mínima → BAIXA) —
     a mesma definição validada no notebook de análise.
  2. Análogos históricos: dias com a MESMA direção provisória no horário
     mais próximo do atual (distância circular no minuto do dia).
  3. Nos análogos, mede empiricamente:
       p_up  = P(fecho subir nas próximas 4 velas M15 (1h))
       p_tp  = P(o preço TOCAR o alvo a favor do fade (+X) em 4h)
       p_sl  = P(o preço TOCAR o lado do stop (−Y) em 4h)
  4. Reporta SEMPRE o tamanho da amostra (n) e o viés (BUY/SELL) —
     quando a amostra é pequena, a mensagem marca o sinal como fraco.

Regra conservadora para toque: se uma única vela toca os dois lados,
o evento é marcado como ambíguo e não conta para nenhum dos lados.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

MINUTES_IN_DAY = 1440

# Parâmetros da consulta (constantes → fáceis de ajustar/testar)
K_BARS_UP = 4        # janela de "próxima 1h" (4 × M15)
HORIZON_BARS = 16    # janela dos toques TP/SL (4h)
N_NEIGHBORS = 40     # número máximo de análogos


def cyclic_minutes(a: int, b: int) -> int:
    """Distância circular (minutos) entre dois horários do dia."""
    d = abs(int(a) - int(b)) % MINUTES_IN_DAY
    return min(d, MINUTES_IN_DAY - d)


def provisional_direction(candles: list[dict]) -> Optional[str]:
    """Direção provisória do dia até agora.

    'UP'  → a máxima provisória veio primeiro (mercado definiu ALTA antes)
    'DOWN'→ a mínima provisória veio primeiro
    None  → sem definição (máx e mín ainda na mesma vela)
    """
    if not candles:
        return None
    hi = max(c["high"] for c in candles)
    lo = min(c["low"] for c in candles)
    hi_first = min(c["tod"] for c in candles if c["high"] == hi)
    lo_first = min(c["tod"] for c in candles if c["low"] == lo)
    if hi_first < lo_first:
        return "UP"
    if lo_first < hi_first:
        return "DOWN"
    return None


class ProbeProbabilityEngine:
    """Probabilidades empíricas por vizinhos históricos (sem MT5)."""

    def __init__(
        self,
        history_csv: Optional[Path] = None,
        data_dir: Optional[Path] = None,
    ) -> None:
        self._path = history_csv
        if self._path is None and data_dir is not None:
            for name in ("m15_500d.csv", "m15_120d.csv"):
                candidate = Path(data_dir) / name
                if candidate.exists():
                    self._path = candidate
                    break
        self._disabled = self._path is None or not Path(self._path).exists()
        self._prepared = False
        self._dates: list = []
        self._day_candles: dict = {}

    # ── load ─────────────────────────────────────────────────

    def _prepare(self) -> bool:
        """Carrega o histórico M15 e pré-processa (uma única vez)."""
        if self._disabled:
            return False
        if self._prepared:
            return True
        try:
            df = pd.read_csv(self._path, parse_dates=["time"])
        except (OSError, ValueError):
            self._disabled = True
            return False
        if df.empty:
            self._disabled = True
            return False
        df["date"] = df["time"].dt.date
        df["tod"] = df["time"].dt.hour * 60 + df["time"].dt.minute
        # Mantém apenas dias completos (mesma regra da análise)
        cnt = df.groupby("date").size()
        df = df[df["date"].isin(cnt[cnt >= 80].index)].copy()
        self._dates = sorted(df["date"].unique())
        for d, g in df.groupby("date"):
            g = g.sort_values("tod")
            self._day_candles[d] = [
                {
                    "tod": int(r.tod),
                    "high": float(r.high),
                    "low": float(r.low),
                    "close": float(r.close),
                }
                for r in g.itertuples(index=False)
            ]
        self._prepared = True
        return True

    # ── analogues ────────────────────────────────────────────

    def _analogues(self, tod: int, direction: str, today, k: int) -> list:
        """(dist, data, tod de referência) dos análogos mais próximos."""
        scored = []
        for d in self._dates:
            if d == today:
                continue  # nunca usar o próprio dia (lookahead)
            candles = self._day_candles[d]
            ref = None
            for c in reversed(candles):
                if c["tod"] <= tod:
                    ref = c["tod"]
                    break
            if ref is None:
                continue
            dir_d = provisional_direction(
                [c for c in candles if c["tod"] <= ref]
            )
            if dir_d != direction:
                continue
            scored.append((cyclic_minutes(tod, ref), d, ref))
        scored.sort(key=lambda t: t[0])
        return scored[:k]

    # ── public API ───────────────────────────────────────────

    def compute(
        self,
        now: datetime,
        today_candles: list[dict],
        x: float = 10.0,
        y: float = 7.0,
        cost: float = 0.0,
        k_bars_up: int = K_BARS_UP,
        horizon_bars: int = HORIZON_BARS,
        n_neighbors: int = N_NEIGHBORS,
    ) -> Optional[dict]:
        """Calcula as probabilidades e o EV para o Profit Probe.

        x/y = alvos em USD/oz de preço; cost = custo por trade em USD/oz
        (spread + slippage) — o EV retornado já desconta esse custo.

        today_candles: velas M15 de HOJE (até agora), cada uma com
                       {"tod": minuto do dia, "high", "low", "close"}.
        """
        if not self._prepare():
            return None
        tod = now.hour * 60 + now.minute
        direction = provisional_direction(today_candles)
        if direction is None:
            return {"ok": False, "reason": "sem definição de direção ainda"}

        analogues = self._analogues(tod, direction, now.date(), n_neighbors)
        n = len(analogues)
        if n == 0:
            return {"ok": False, "reason": "sem análogos históricos"}

        n_up = 0
        n_up_used = 0
        tp_hits = 0
        sl_hits = 0
        ambiguous = 0
        else_deltas: list = []   # resultado (signed) dos casos não resolvidos
        for _, d, ref in analogues:
            candles = self._day_candles[d]
            entry = next(c["close"] for c in candles if c["tod"] == ref)
            ahead = [c for c in candles if c["tod"] > ref][:horizon_bars]

            # p_up: fecho acima da entrada em k_bars velas (1h)
            if len(ahead) >= k_bars_up:
                n_up_used += 1
                if ahead[k_bars_up - 1]["close"] > entry:
                    n_up += 1

            # toques: fade da direção definida (UP → SELL, DOWN → BUY)
            resolved = None
            for c in ahead:
                tp_up = c["high"] >= entry + x      # compra ganharia
                tp_dn = c["low"] <= entry - x       # venda ganharia
                sl_up = c["high"] >= entry + y
                sl_dn = c["low"] <= entry - y
                hit_tp = tp_dn if direction == "UP" else tp_up
                hit_sl = sl_up if direction == "UP" else sl_dn
                if hit_tp and hit_sl:
                    resolved = "amb"
                    break
                if hit_tp:
                    resolved = "tp"
                    break
                if hit_sl:
                    resolved = "sl"
                    break
            if resolved == "tp":
                tp_hits += 1
            elif resolved == "sl":
                sl_hits += 1
            elif resolved == "amb":
                ambiguous += 1
            # resultado no fim da janela para ambíguo ou sem toque
            last_close = float(ahead[-1]["close"]) if ahead else entry
            if resolved != "tp" and resolved != "sl":
                else_deltas.append(
                    entry - last_close if direction == "UP"
                    else last_close - entry
                )

        bias = "SELL" if direction == "UP" else "BUY"
        # EV realista: soma de TODOS os desfechos (TP, SL e não-resolvidos)
        # dividida pela amostra, já descontando o custo por trade.
        ev = (tp_hits * x - sl_hits * y + sum(else_deltas)) / n - cost
        return {
            "ok": True,
            "n": n,
            "direction": direction,
            "bias": bias,
            "p_up_1h": round(n_up / n_up_used, 4) if n_up_used else None,
            "p_tp": round(tp_hits / n, 4),
            "p_sl": round(sl_hits / n, 4),
            "p_neither": round((n - tp_hits - sl_hits - ambiguous) / n, 4),
            "ev": round(ev, 3),
            "cost": round(float(cost), 4),
            "x": float(x),
            "y": float(y),
            "horizon_hours": horizon_bars // 4,
            "weak": n < 15,
        }
