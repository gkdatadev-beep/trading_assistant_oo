"""Camada de sinais — canal da primeira hora (auto trade).

Referência: canal da primeira hora (12 velas M5 iniciais do dia).

Regras de projeção:
  • Multiplicadores PARES (2, 4, 6, ...) = resistências mais fortes → TP.
  • Multiplicadores ÍMPARES (1, 3, 5, ...) = usados para stops seguros → SL.

Cenário COMPRA (venda é o espelho):
  1. O preço rompe acima do nível superior do canal 1h (Canal 1h - Max).
  2. Entrada quando o preço passa BUFFER (0,5 USD) acima da máxima atual do dia.
  3. TP no primeiro canal par forte: Canal 1h - Max + 2 × projeção.
  4. SL no canal (-1) da mínima do dia: mínima do dia − 1 × projeção.

Cenário VENDA:
  1. O preço rompe abaixo do nível inferior do canal 1h (Canal 1h - Min).
  2. Entrada quando o preço passa BUFFER abaixo da mínima atual do dia.
  3. TP: Canal 1h - Min − 2 × projeção.
  4. SL: máxima do dia + 1 × projeção.

O sinal é INFORMATIVO por padrão (sempre exibido na tela e enviado ao
Telegram). A abertura automática da operação é decidida pelo chamador
via callback `on_signal` (flag AUTO_TRADE_ENABLED do app).
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

MIN_CHANNEL_CANDLES = 12  # mesma referência do channel_alert


def compute_signal_plan(
    channel: dict,
    day_high: float,
    day_low: float,
    direction: str,
    entry_buffer: float = 0.5,
    tp_channel: int = 2,
    sl_channel: int = 1,
) -> dict:
    """TP no canal PAR forte e SL no canal ÍMPAR seguro.

    Returns {"tp", "sl", "tp_label", "sl_label", "trigger"}.
    """
    proj = float(channel["projection_distance"])
    ch_high = float(channel["channel_high"])
    ch_low = float(channel["channel_low"])
    if direction == "buy":
        tp = round(ch_high + tp_channel * proj, 2)
        sl = round(day_low - sl_channel * proj, 2)
        trigger = round(day_high + entry_buffer, 2)
        tp_label = f"Proj +{tp_channel}"
        sl_label = f"mín. dia − {sl_channel}×proj"
    else:
        tp = round(ch_low - tp_channel * proj, 2)
        sl = round(day_high + sl_channel * proj, 2)
        trigger = round(day_low - entry_buffer, 2)
        tp_label = f"Proj −{tp_channel}"
        sl_label = f"máx. dia + {sl_channel}×proj"
    return {
        "tp": tp,
        "sl": sl,
        "tp_label": tp_label,
        "sl_label": sl_label,
        "trigger": trigger,
    }


def compute_volume(
    stop_distance: float,
    contract_size: float,
    risk_usd: float,
    vol_min: float = 0.01,
    vol_max: float = 100.0,
    vol_step: float = 0.01,
    lot_cap: float = 0.0,
) -> float:
    """Lote calculado pela gestão de risco do stop.

    volume = risco_monetário / (distância_do_stop × tamanho_do_contrato),
    limitado ao intervalo [vol_min, min(vol_max, lot_cap)] e arredondado
    para baixo ao múltiplo de vol_step (nunca acima do teto).
    """
    if stop_distance <= 0 or contract_size <= 0 or risk_usd <= 0:
        return 0.0
    raw = risk_usd / (stop_distance * contract_size)
    cap = min(vol_max, lot_cap) if lot_cap and lot_cap > 0 else vol_max
    if cap < vol_min:
        return 0.0
    v = max(vol_min, min(raw, cap))
    if vol_step and vol_step > 0:
        v = int(v / vol_step) * vol_step
    v = max(vol_min, min(v, cap))
    return round(v, 2)


class AutoSignalEngine:
    """Vigia o rompimento do canal da 1ª hora e dispara o sinal de entrada.

    Um sinal por dia (primeiro gatilho vence). O callback `on_signal`
    recebe o dict do sinal; a decisão de abrir posição fica fora daqui.
    """

    def __init__(
        self,
        symbol: str,
        get_rates_today: Callable[[], Optional[pd.DataFrame]],
        get_server_time: Callable[[], datetime],
        on_signal: Optional[Callable[[dict], None]] = None,
        get_price: Optional[Callable[[], Optional[float]]] = None,
        entry_buffer: float = 0.5,
        tp_channel: int = 2,
        sl_channel: int = 1,
        end_hour: int = 12,
        poll_seconds: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._symbol = symbol
        self._get_rates_today = get_rates_today
        self._get_server_time = get_server_time
        self._on_signal = on_signal
        self._get_price = get_price
        self._entry_buffer = float(entry_buffer)
        self._tp_channel = max(int(tp_channel), 1)
        self._sl_channel = max(int(sl_channel), 1)
        self._end_hour = int(end_hour)
        self._poll = max(float(poll_seconds), 1.0)
        self._clock = clock

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._day_key: Optional[str] = None
        self._channel: dict = {}
        self._prev_price: Optional[float] = None
        self._armed: Optional[str] = None      # "buy" | "sell" | None
        self._fired = False
        self._last_signal: Optional[dict] = None
        self._last_price: Optional[float] = None
        self._day_high: Optional[float] = None
        self._day_low: Optional[float] = None
        self._last_checked_hour: Optional[int] = None

    # ── lifecycle ───────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="auto-signal-engine",
        )
        self._thread.start()
        logger.info("Auto signal engine started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logger.warning("Auto signal check failed", exc_info=True)
            self._stop.wait(self._poll)

    # ── logic ───────────────────────────────────────────────

    def check_once(
        self,
        now: Optional[datetime] = None,
        df: Optional[pd.DataFrame] = None,
        price: Optional[float] = None,
    ) -> Optional[dict]:
        """Roda uma passada de detecção. Retorna o sinal quando disparado."""
        if now is None:
            now = self._get_server_time()
        day = now.date()

        if df is None:
            df = self._get_rates_today()
        if df is None or len(df) == 0:
            return None
        normalized = df.index.normalize()
        day_ts = (
            pd.Timestamp(day, tz=normalized.tz)
            if normalized.tz is not None else pd.Timestamp(day)
        )
        df = df[normalized == day_ts]
        if len(df) < MIN_CHANNEL_CANDLES:
            self._prev_price = None  # canal ainda não definido
            return None

        # (Re)constrói o canal uma vez por dia
        if self._day_key != day.isoformat():
            self._channel = first_hour_channel_levels(df)
            self._day_key = day.isoformat()
            self._prev_price = None
            self._armed = None
            self._fired = False
            self._last_signal = None

        day_high = float(df["high"].max())
        day_low = float(df["low"].min())
        self._day_high = day_high
        self._day_low = day_low

        if price is None and self._get_price is not None:
            try:
                price = self._get_price()
            except Exception:
                price = None
        if price is None:
            price = float(df["close"].iloc[-1])
        self._last_price = float(price)
        self._last_checked_hour = now.hour

        prev = self._prev_price
        self._prev_price = price
        if prev is None:
            return None  # primeira amostra do dia — sem cruzamento anterior

        # Sinal apenas no início do dia (configurável)
        if now.hour >= self._end_hour:
            return None

        if self._fired:
            return None  # um sinal por dia

        ch_high = float(self._channel["channel_high"])
        ch_low = float(self._channel["channel_low"])

        # 1. Rompimento do canal arma a direção (sticky pelo dia)
        if self._armed is None:
            if prev <= ch_high < price:
                self._armed = "buy"
                logger.info(
                    "Auto signal: canal 1h rompido PARA CIMA %.2f", price)
            elif prev >= ch_low > price:
                self._armed = "sell"
                logger.info(
                    "Auto signal: canal 1h rompido PARA BAIXO %.2f", price)
            else:
                return None

        # 2. Gatilho: buffer acima da máxima (compra) / abaixo da mínima (venda)
        if self._armed == "buy":
            trigger = round(day_high + self._entry_buffer, 2)
            if price < trigger:
                return None
        else:
            trigger = round(day_low - self._entry_buffer, 2)
            if price > trigger:
                return None

        plan = compute_signal_plan(
            self._channel, day_high, day_low, self._armed,
            self._entry_buffer, self._tp_channel, self._sl_channel,
        )
        sig = {
            "direction": self._armed,
            "day": day.isoformat(),
            "time": now.strftime("%H:%M:%S"),
            "price": round(float(price), 2),
            "trigger": trigger,
            "tp": plan["tp"],
            "sl": plan["sl"],
            "tp_label": plan["tp_label"],
            "sl_label": plan["sl_label"],
            "stop_distance": round(abs(round(float(price), 2) - plan["sl"]), 2),
            "channel_high": ch_high,
            "channel_low": ch_low,
            "channel_width": float(self._channel["channel_width"]),
            "projection_distance": float(self._channel["projection_distance"]),
            "day_high": day_high,
            "day_low": day_low,
        }
        self._fired = True
        self._last_signal = sig
        logger.info(
            "SIGNAL fired: %s trigger=%.2f price=%.2f tp=%.2f sl=%.2f",
            sig["direction"], trigger, float(price), plan["tp"], plan["sl"],
        )
        if self._on_signal is not None:
            try:
                self._on_signal(dict(sig))
            except Exception:
                logger.warning("on_signal callback failed", exc_info=True)
        return sig

    # ── status (para a tela principal) ──────────────────────

    def status(self) -> dict:
        plan: dict = {}
        if self._channel and self._day_high is not None:
            plan = {
                "buy": compute_signal_plan(
                    self._channel, self._day_high, self._day_low, "buy",
                    self._entry_buffer, self._tp_channel, self._sl_channel,
                ),
                "sell": compute_signal_plan(
                    self._channel, self._day_high, self._day_low, "sell",
                    self._entry_buffer, self._tp_channel, self._sl_channel,
                ),
            }
        return {
            "symbol": self._symbol,
            "day": self._day_key,
            "channel_ready": bool(self._channel),
            "channel": self._channel or {},
            "day_high": self._day_high,
            "day_low": self._day_low,
            "price": self._last_price,
            "armed": self._armed,
            "fired_today": self._fired,
            "signal": self._last_signal,
            "plan": plan,
            "entry_buffer": self._entry_buffer,
            "end_hour": self._end_hour,
            "cutoff_reached": (
                self._last_checked_hour is not None
                and self._last_checked_hour >= self._end_hour
            ),
        }


def render_signal_chart(
    day_m5: pd.DataFrame,
    sig: dict,
    symbol: str = "XAUUSD",
) -> Optional[bytes]:
    """Renderiza as velas M5 do dia com o setup do sinal atingido.

    Desenha: candles, canal 1h (max/min), máx/mín do dia, gatilho,
    TP (canal par), SL (canal ímpar) e o ponto do sinal. Retorna os
    bytes do PNG (para envio ao Telegram) ou None se faltarem dados.
    """
    if day_m5 is None or len(day_m5) < MIN_CHANNEL_CANDLES:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from io import BytesIO

    df = day_m5.reset_index(drop=True)
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    x = np.arange(len(df), dtype=float)

    direction = sig.get("direction", "buy")
    arrow = "COMPRA" if direction == "buy" else "VENDA"
    price = float(sig["price"])
    tp = float(sig["tp"])
    sl = float(sig["sl"])

    fig, ax = plt.subplots(figsize=(13, 6.5))
    fig.patch.set_facecolor("#0b1118")
    ax.set_facecolor("#0b1118")

    # Candles
    span = float(highs.max() - lows.min()) or 1.0
    for i in range(len(df)):
        color = "#00c853" if closes[i] >= opens[i] else "#ff3d3d"
        ax.vlines(x[i], lows[i], highs[i], color=color,
                  linewidth=1.0, alpha=0.95)
        body_bottom = min(opens[i], closes[i])
        body_height = max(abs(closes[i] - opens[i]), span * 0.001)
        ax.add_patch(plt.Rectangle(
            (x[i] - 0.3, body_bottom), 0.6, body_height,
            color=color, alpha=0.85))

    # Setup do sinal
    ax.axhline(sig["channel_high"], color="#D4AF37", linestyle="--",
               linewidth=1.4, label="Canal 1h - Max")
    ax.axhline(sig["channel_low"], color="#D4AF37", linestyle="-.",
               linewidth=1.4, label="Canal 1h - Min")
    ax.axhline(sig["day_high"], color="#9aa5b1", linestyle=":",
               linewidth=1.0, alpha=0.7, label="Máx do dia")
    ax.axhline(sig["day_low"], color="#9aa5b1", linestyle=":",
               linewidth=1.0, alpha=0.7, label="Mín do dia")
    ax.axhline(sig["trigger"], color="#ff9800", linestyle=":",
               linewidth=1.4, label=f"Gatilho {sig['trigger']:.2f}")
    ax.axhline(tp, color="#00FF41", linestyle="--", linewidth=1.5,
               label=f"TP {tp:.2f} ({sig['tp_label']})")
    ax.axhline(sl, color="#ff4444", linestyle="--", linewidth=1.5,
               label=f"SL {sl:.2f} ({sig['sl_label']})")

    # Ponto do sinal
    ax.scatter([x[-1]], [price], color="#FFD700", s=70, zorder=5,
               label=f"Sinal {arrow} @ {price:.2f}")
    ax.text(
        x[-1] - max(len(df) * 0.16, 3), price,
        f"SINAL {arrow}\n{price:.2f}",
        color="#FFD700", fontsize=10, fontweight="bold", va="center",
        bbox={"facecolor": "#0b1118", "edgecolor": "#FFD700", "alpha": 0.85},
    )

    y_lo = min(float(lows.min()), sl)
    y_hi = max(float(highs.max()), tp)
    pad = (y_hi - y_lo) * 0.06
    ax.set_ylim(y_lo - pad, y_hi + pad)

    ax.set_title(
        f"Sinal CANAL 1H — {arrow} {symbol} — {sig.get('time', '')}",
        color="#e0e0e0", fontsize=13, fontweight="bold")
    ax.set_xlabel("Velas M5 do dia (servidor)", color="#90a4b8", fontsize=10)
    ax.set_ylabel("Preço", color="#90a4b8", fontsize=10)
    ax.tick_params(colors="#90a4b8", labelsize=9)
    for spine in ax.spines.values():
        spine.set_color("#2a3a4a")
    ax.grid(True, color="#ffffff", alpha=0.05)
    ax.legend(loc="upper left", fontsize=8, facecolor="#0b1118",
              edgecolor="#2a3a4a", labelcolor="#c0c8d0", framealpha=0.9)
    plt.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
