"""Telegram messenger — notifications, probes, trade monitors."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Type alias for the data provider callback
DataProvider = Callable[[], dict]


class Messenger:
    """Sends Telegram notifications and runs background probes."""

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = False,
    ):
        self._token = bot_token
        self._chat_id = chat_id
        self._enabled = enabled

        # Probe state
        self._probe_stop = threading.Event()
        self._probe_thread: Optional[threading.Thread] = None
        self._probe_lock = threading.Lock()
        self._probe_last_emission: Optional[str] = None
        self._data_provider: Optional[DataProvider] = None

    # ── lifecycle ───────────────────────────────────────────

    def set_data_provider(self, provider: DataProvider) -> None:
        """Callback that returns live trading data for probe messages."""
        self._data_provider = provider

    def start_probes(self) -> None:
        """Start background profit probe thread."""
        if self._probe_thread is not None and self._probe_thread.is_alive():
            return
        self._probe_stop.clear()
        self._probe_thread = threading.Thread(
            target=self._probe_loop, daemon=True, name="profit-probe",
        )
        self._probe_thread.start()
        logger.info("Profit probe started")

    def stop_probes(self) -> None:
        self._probe_stop.set()
        if self._probe_thread is not None:
            self._probe_thread.join(timeout=3)

    # ── send ────────────────────────────────────────────────

    def send(self, text: str) -> bool:
        if not self._enabled or not self._token or not self._chat_id:
            return False
        try:
            import urllib.parse
            import urllib.request
            import json
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self._chat_id, "text": text,
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode())
                return bool(body.get("ok"))
        except Exception:
            return False

    # ── notifications ───────────────────────────────────────

    def notify_blocks(self, blocks: list[str]) -> None:
        if not blocks:
            return
        msg = "🛑 TRADING BLOQUEADO — bloqueios ativos:\n"
        msg += "\n".join(f"  • {b}" for b in blocks)
        self.send(msg)

    def notify_startup(self, config: dict) -> None:
        lines = ["✅ Trade Assistant iniciado"]
        lines.append(f"S: {config.get('SYMBOL', '?')} | TF: {config.get('TIMEFRAME', '?')}")
        lines.append(f"Lote: {config.get('LOT_SIZE', '?')} | SLx{config.get('SL_MULTIPLIER', '?')} | TPx{config.get('TP_MULTIPLIER', '?')}")
        self.send("\n".join(lines))

    # ── profit probe loop ───────────────────────────────────

    def _probe_loop(self) -> None:
        """Check every 60s — emit on 5-min or 15-min boundary."""
        logger.info("Profit probe loop started")
        while not self._probe_stop.is_set():
            try:
                now = datetime.now(timezone.utc)
                has_positions = False

                # Try to detect positions via data provider
                if self._data_provider:
                    try:
                        data = self._data_provider()
                        has_positions = data.get("has_position", False)
                    except Exception:
                        pass

                interval = 5 if has_positions else 15
                on_boundary = now.minute % interval == 0

                with self._probe_lock:
                    bucket = now.strftime("%H:%M")
                    already = (self._probe_last_emission == bucket)

                if on_boundary and not already:
                    self._emit_probe_snapshot(now)
                    with self._probe_lock:
                        self._probe_last_emission = bucket

                # Sleep 15s, checking stop
                for _ in range(15):
                    if self._probe_stop.is_set():
                        return
                    time.sleep(1)
            except Exception:
                logger.debug("Probe loop error", exc_info=True)
                time.sleep(10)

    def _emit_probe_snapshot(self, now: datetime) -> None:
        """Build and send one probe message with real data."""
        total_pnl = 0.0
        open_pnl = 0.0
        closed_pnl = 0.0
        atr_val = 0.0
        has_positions = False
        connected = False

        if self._data_provider:
            try:
                data = self._data_provider()
                total_pnl = data.get("daily_profit", 0.0)
                open_pnl = data.get("open_profit", 0.0)
                closed_pnl = total_pnl - open_pnl
                atr_val = data.get("atr", 0.0)
                has_positions = data.get("has_position", False)
                connected = data.get("mt5_connected", False)
            except Exception:
                pass

        weekdays = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]
        months = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
                  "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
        ts = (
            f"{weekdays[now.weekday()]}, "
            f"{now.day:02d} {months[now.month - 1]} "
            f"{now.hour:02d}:{now.minute:02d}"
        )

        if connected:
            pulse = "🟢" if has_positions else "⏳"
            lines = [
                f"📊 {pulse} Profit Probe — {ts}",
                f"💰 total={total_pnl:+.2f}  •  aberto={open_pnl:+.2f}  •  fechado={closed_pnl:+.2f}",
                f"📐 ATR: {atr_val:.2f}",
            ]
            self.send("\n".join(lines))

            # Send trade chart if position is open
            if has_positions and self._data_provider:
                try:
                    data = self._data_provider()
                    history = data.get("trade_history", [])
                    if history and len(history) > 1:
                        self._send_trade_chart(history, ts)
                except Exception:
                    pass
        else:
            lines = [f"📊 Profit Probe — {ts}", "⚠️ MT5 indisponivel"]

        self.send("\n".join(lines))

    # ── photo ──────────────────────────────────────────────

    def _send_trade_chart(self, history: list, ts: str) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from io import BytesIO
        import os
        profits = [h.get("profit", 0) for h in history]
        times = list(range(len(profits)))
        fig, ax = plt.subplots(figsize=(4, 2))
        pos = [p >= 0 for p in profits]
        neg = [p < 0 for p in profits]
        if any(pos):
            ax.fill_between(times, 0, profits, where=pos, color="#31b46c", alpha=0.3)
        if any(neg):
            ax.fill_between(times, 0, profits, where=neg, color="#ef6b5a", alpha=0.3)
        ax.plot(times, profits, color="#2d7ce3", linewidth=1.5)
        ax.axhline(0, color="white", linewidth=0.5, alpha=0.3)
        ax.set_title(f"Trade PnL — {ts}", fontsize=9, color="white")
        ax.set_facecolor("#0b1118")
        fig.patch.set_facecolor("#0b1118")
        ax.tick_params(colors="#90a4b8", labelsize=7)
        for spine in ax.spines.values():
            spine.set_color("#2a3a4a")
        plt.tight_layout()
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=80, bbox_inches="tight")
        plt.close(fig)
        tmp_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "_probe_chart.png")
        os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
        with open(tmp_path, "wb") as f:
            f.write(buf.getvalue())
        self.send_photo(tmp_path, caption=f"📈 Trade PnL — {ts}")

    def send_photo(self, image_path: str, caption: str = "") -> bool:
        if not self._enabled or not self._token or not self._chat_id:
            return False
        try:
            import requests
            url = f"https://api.telegram.org/bot{self._token}/sendPhoto"
            with open(image_path, "rb") as f:
                resp = requests.post(url, data={"chat_id": self._chat_id, "caption": caption},
                                     files={"photo": f}, timeout=15)
            return bool(resp.json().get("ok"))
        except Exception:
            return False
