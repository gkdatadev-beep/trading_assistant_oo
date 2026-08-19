"""Flask routes — minimal, stable."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, render_template, request

from app.config import SECRET_KEYS
from app.core.channel import analyze_channels, render_channel_chart
from app.core.ledger import CHALLENGE_START

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/api/state")
def api_state():
    """Return current session + challenge state as JSON.

    Query params:
        ?day=YYYY-MM-DD  → load historical snapshot for that day
        (omit)           → live current session
    """
    try:
        session = getattr(current_app, "session", None)
        broker = getattr(current_app, "broker", None)
        persistence = getattr(current_app, "persistence", None)
        request_day = request.args.get("day", "").strip()

        # ── Historical mode: load snapshot for a past day ──
        if request_day and len(request_day) == 10 and persistence:
            snap = persistence.load_day(request_day)
            if snap:
                state = dict(snap)
                state["server_time"] = request_day + " 00:00:00 (historico)"
                state["mt5_connected"] = False
                state["trade_history"] = []
                state["closed_trades"] = []
                state["max_profit_price"] = 0
                state["max_loss_price"] = 0
                state["block_release_countdown"] = "--:--"
                state["block_details"] = _build_block_details(state)
                state["is_historical"] = True
                return jsonify(state)

        # ── Live mode: current session ──────────────────────
        from app.web.dashboard import Dashboard
        state = Dashboard.get_state(
            session=session or _empty_session(),
            challenge=None,
        )

        # Always use broker time when available
        if broker is not None and getattr(broker, "connected", False):
            try:
                server_str = broker.get_server_time().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                server_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        else:
            server_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        state["server_time"] = server_str
        state["mt5_connected"] = broker is not None and getattr(broker, "connected", False)

        # Server start time for uptime counter
        start_time = getattr(current_app, "start_time", None)
        state["server_start_time"] = start_time.isoformat() if start_time else None

        # Price-based stop limits for the bar gauge on the frontend
        config = getattr(current_app, "config_obj", None)
        state["max_profit_price"] = config.get_float("MAX_PROFIT_PRICE", 10.0) if config else 10.0
        state["max_loss_price"] = config.get_float("MAX_LOSS_PRICE", 5.0) if config else 5.0

        # Countdown to block release (next midnight broker local time)
        from app.core.market import MarketAnalyzer
        bt = broker.get_server_time() if broker and broker.connected else datetime.now(timezone.utc)
        state["block_release_countdown"] = MarketAnalyzer.countdown_hhmm(bt)

        state["block_details"] = _build_block_details(state)
        state["is_historical"] = False

        # Trade history for chart + closed trades (send all for cumulative chart)
        trade_history = getattr(current_app, "trade_history", [])
        state["trade_history"] = trade_history if trade_history else []

        # Fetch today's closed trades via broker
        closed_trades = getattr(current_app, "closed_trades", None)
        if not closed_trades and broker is not None and broker.connected:
            try:
                closed_trades = broker.get_closed_trades_today()
            except Exception:
                closed_trades = []
        state["closed_trades"] = (closed_trades or [])[-20:]

        return jsonify(state)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/days")
def api_days():
    """List available historical snapshot days."""
    persistence = getattr(current_app, "persistence", None)
    days = persistence.list_available_days() if persistence else []
    return jsonify({"days": days})


@bp.route("/api/health")
def api_health():
    """Health check endpoint for external watchdog monitoring.

    Returns:
        200 OK with health details when everything is healthy.
        503 Service Unavailable when the update loop is stalled or MT5 is down.
    """
    import time as _time
    now_mono = _time.monotonic()
    broker = getattr(current_app, "broker", None)
    last_heartbeat = getattr(current_app, "last_loop_heartbeat", 0.0)
    start_time = getattr(current_app, "start_time", None)
    session = getattr(current_app, "session", None)

    # How long since the last loop heartbeat?
    heartbeat_age = now_mono - last_heartbeat if last_heartbeat else 999.0

    # Broker health
    broker_health = broker.health_dict() if broker else {"mt5_connected": False}

    # Determine overall status
    issues: list[str] = []

    # Loop stall detection: no heartbeat in > 30 seconds
    if heartbeat_age > 30:
        issues.append(f"update_loop_stalled:{heartbeat_age:.0f}s")

    # MT5 disconnected
    if not broker_health.get("mt5_connected", False):
        issues.append("mt5_disconnected")

    # MT5 health failures accumulating
    if broker_health.get("health_failures", 0) >= 3:
        issues.append(f"mt5_health_failures:{broker_health['health_failures']}")

    # Uptime
    uptime_seconds = (datetime.now(timezone.utc) - start_time).total_seconds() if start_time else 0

    healthy = len(issues) == 0
    status_code = 200 if healthy else 503

    return jsonify({
        "status": "healthy" if healthy else "degraded",
        "uptime_seconds": round(uptime_seconds, 0),
        "heartbeat_age_seconds": round(heartbeat_age, 1),
        "mt5": broker_health,
        "session_day": session.day_key if session else "unknown",
        "issues": issues,
    }), status_code


@bp.route("/parm", methods=["GET", "POST"])
def parm():
    config = getattr(current_app, "config_obj", None)
    message = ""
    if request.method == "POST":
        try:
            updates = {}
            for key in request.form:
                val = request.form[key]
                if val:
                    updates[key] = val
            if updates and config:
                config.update(updates)
                message = "Parametros atualizados com sucesso."
                # Telegram notification
                messenger = getattr(current_app, "messenger", None)
                if messenger:
                    changed = "\n".join(
                        f"  {k}: {'***' if k in SECRET_KEYS else v}"
                        for k, v in updates.items()
                    )
                    messenger.send(f"⚙️ Parametros atualizados:\n{changed}")
        except Exception as e:
            message = f"Erro: {e}"

    raw = config.to_dict() if config else {}

    # Grouped parameters with explanations for the 3-column layout
    param_groups = [
        {
            "title": "📐 Entrada & Dimensionamento",
            "fields": [
                {"key": "SYMBOL", "label": "Ativo (Symbol)",
                 "hint": "Ex: XAUUSD, XAGUSD", "example": "XAUUSD"},
                {"key": "TIMEFRAME", "label": "Timeframe",
                 "hint": "M1 / M5 / M15 / M30 / H1 / H4",
                 "example": "TIMEFRAME_M15"},
                {"key": "LOT_SIZE", "label": "Lote (Volume)",
                 "hint": "Tamanho da posicao em lotes", "example": "0.15"},
                {"key": "TP_MULTIPLIER", "label": "Multiplicador TP",
                 "hint": "TP = entry ± TP_MULT × ATR", "example": "4"},
                {"key": "SL_MULTIPLIER", "label": "Multiplicador SL",
                 "hint": "SL = entry ± SL_MULT × ATR", "example": "2"},
            ],
        },
        {
            "title": "🛡️ Stops & Gestao de Risco",
            "fields": [
                {"key": "MAX_PROFIT_PRICE", "label": "Take Profit (USD)",
                 "hint": "Fecha se preco variar este valor a favor. "
                        "Ex: compra sobe $10 → fecha",
                 "example": "10.0"},
                {"key": "MAX_LOSS_PRICE", "label": "Stop Loss (USD)",
                 "hint": "Fecha se preco variar este valor contra. "
                        "Ex: compra cai $5 → fecha",
                 "example": "5.0"},
                {"key": "LOCK_ENTRY", "label": "Lock Entry (× ATR)",
                 "hint": "Move SL ao breakeven se distancia ≥ LOCK × ATR",
                 "example": "0.5"},
                {"key": "TRAILING_STOP_BUFFER",
                 "label": "Buffer Trail M15 (USD)",
                 "hint": "Fecha se preco romper minima (compra) ou maxima "
                        "(venda) do M15 anterior − buffer",
                 "example": "0.5"},
                {"key": "M5_RANGE_ATR_STOP_MULTIPLIER",
                 "label": "M5 Impulse Stop (× ATR)",
                 "hint": "Fecha se impulso do candle M5 atual ≥ MULT × ATR",
                 "example": "1.0"},
                {"key": "MAX_PROFIT_RETENTION_PCT",
                 "label": "Retencao Max Lucro (%)",
                 "hint": "Fecha se lucro cai abaixo de X% do pico "
                        "(so apos lock-entry)",
                 "example": "50"},
                {"key": "LIMITE_LUCRO_DIARIO",
                 "label": "Limite Lucro Diario (USD)",
                 "hint": "Bloqueia operacoes se lucro diario ≥ este valor",
                 "example": "750"},
                {"key": "LIMITE_PERDA_DIARIO",
                 "label": "Limite Perda Diario (USD)",
                 "hint": "Bloqueia operacoes se drawdown diario ≥ |este valor|",
                 "example": "-600"},
                {"key": "LIMITE_OPERACOES",
                 "label": "Limite de Operacoes Diarias",
                 "hint": "Numero maximo de trades por dia",
                 "example": "40"},
                {"key": "LIMITE_LUCRO_OP",
                 "label": "Lucro Max por Operacao (USD)",
                 "hint": "Fecha trade se lucro desta operacao ≥ este valor",
                 "example": "350"},
                {"key": "LIMITE_PERDA_OP",
                 "label": "Perda Max por Operacao (USD)",
                 "hint": "Fecha trade se prejuizo desta operacao ≤ este valor (negativo)",
                 "example": "-200"},
            ],
        },
        {
            "title": "⏰ Sessao, Janelas & Notificacoes",
            "fields": [
                {"key": "TRADING_WINDOW_1_START",
                 "label": "Janela 1 — Inicio (UTC)",
                 "hint": "Horario UTC de abertura da primeira janela",
                 "example": "01:00"},
                {"key": "TRADING_WINDOW_1_DURATION_HOURS",
                 "label": "Janela 1 — Duracao (h)",
                 "hint": "Quantas horas dura a primeira janela",
                 "example": "5"},
                {"key": "TRADING_WINDOW_2_START",
                 "label": "Janela 2 — Inicio (UTC)",
                 "hint": "Horario UTC de abertura da segunda janela",
                 "example": "15:00"},
                {"key": "TRADING_WINDOW_2_DURATION_HOURS",
                 "label": "Janela 2 — Duracao (h)",
                 "hint": "Quantas horas dura a segunda janela",
                 "example": "5"},
                {"key": "OVERTRADING_PROTECTION_TIME",
                 "label": "Protecao Overtrading (min)",
                 "hint": "Tempo minimo entre operacoes",
                 "example": "1"},
                {"key": "TELEGRAM_ENABLED",
                 "label": "Telegram Ativo",
                 "hint": "1 = ligado, 0 = desligado",
                 "example": "0"},
                {"key": "TELEGRAM_BOT_TOKEN",
                 "label": "Token do Bot Telegram",
                 "hint": "Obtido via @BotFather",
                 "example": ""},
                {"key": "TELEGRAM_CHAT_ID",
                 "label": "Chat ID Telegram",
                 "hint": "ID do chat/grupo para notificacoes",
                 "example": ""},
            ],
        },
        {
            "title": "🔐 Conexao MT5 (auto-login)",
            "fields": [
                {"key": "MT5_SERVER", "label": "Servidor",
                 "hint": "Nome do servidor do broker (exatamente como no login do MT5)",
                 "example": "Exness-Real2"},
                {"key": "MT5_LOGIN", "label": "Conta (Login)",
                 "hint": "Numero da conta de trading",
                 "example": "12345678"},
                {"key": "MT5_PASSWORD", "label": "Senha",
                 "hint": "Senha da conta — usada pelo app para auto-login apos boot/reconexao",
                 "example": "", "secret": True},
            ],
        },
    ]

    # Inject current values into each field
    for group in param_groups:
        for field in group["fields"]:
            field["value"] = raw.get(field["key"], "")

    return render_template("parm.html", param_groups=param_groups, message=message)


@bp.route("/chart")
def chart():
    day = request.args.get("day", "")
    broker = getattr(current_app, "broker", None)

    # Determine the target day using broker local time
    if not day:
        try:
            if broker and broker.connected:
                day = broker.get_server_time().strftime("%Y-%m-%d")
            else:
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    image_data = None
    summary = {}
    error = None

    try:
        config = getattr(current_app, "config_obj", None)
        if broker and broker.connected and config:
            symbol = config.get_str("SYMBOL", "XAUUSD")
            import MetaTrader5 as mt5

            import pandas as pd
            from datetime import timedelta as _td
            target = pd.to_datetime(day).date()

            # Day boundaries in server time (UTC) for MT5 query
            utc_start = datetime(target.year, target.month, target.day, tzinfo=timezone.utc)
            utc_end = utc_start + _td(days=1) - _td(seconds=1)

            # Fetch a slightly wider window to ensure we catch all candles
            df = broker.get_rates_range(
                utc_start - _td(hours=1), utc_end + _td(hours=1),
                symbol=symbol, timeframe=mt5.TIMEFRAME_M5,
            )
            if df is not None and len(df) >= 12:
                import numpy as np
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                from io import BytesIO
                import base64

                df = df.reset_index()
                # Use server time (UTC) directly for filtering & display
                df["time_local"] = pd.to_datetime(df["time"])
                df["day"] = df["time_local"].dt.date
                day_data = df[df["day"] == target].sort_values("time_local").reset_index(drop=True)

                if len(day_data) < 12:
                    error = f"Dados insuficientes para {day} (apenas {len(day_data)} candles M5)"
                    return render_template("chart.html", day=day, image_data=None, summary={}, error=error)

                first_hour = day_data.iloc[:12]
                ch_high = float(first_hour["high"].max())
                ch_low = float(first_hour["low"].min())
                ch_width = ch_high - ch_low
                proj_dist = ch_width * 2

                day_high = float(day_data["high"].max())
                day_low = float(day_data["low"].min())
                price_range = day_high - day_low
                pad = max(price_range * 0.15, ch_width * 0.3)
                y_floor = day_low - pad
                y_ceil = day_high + pad

                x = np.arange(len(day_data))
                fig, ax = plt.subplots(figsize=(14, 5))
                for i, row in day_data.iterrows():
                    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
                    color = "forestgreen" if c >= o else "firebrick"
                    ax.vlines(i, l, h, color=color, linewidth=1.0, alpha=0.95)
                    body_bottom = min(o, c)
                    body_height = max(abs(c - o), 0.02)
                    ax.add_patch(plt.Rectangle((i - 0.3, body_bottom), 0.6, body_height, color=color, alpha=0.8))

                ax.axhline(ch_high, color="royalblue", linestyle="--", linewidth=1.6, label="Canal 1h - Max")
                ax.axhline(ch_low, color="royalblue", linestyle="-.", linewidth=1.6, label="Canal 1h - Min")

                # Projection levels — only if price touched them
                tol = ch_width * 0.1
                proj_data = {}
                for n in range(1, 4):
                    up = ch_high + proj_dist * n
                    down = ch_low - proj_dist * n
                    up_touched = any((r["low"] - tol) <= up <= (r["high"] + tol) for _, r in day_data.iterrows())
                    down_touched = any((r["low"] - tol) <= down <= (r["high"] + tol) for _, r in day_data.iterrows())
                    proj_data[f"proj_up_{n}"] = up
                    proj_data[f"proj_down_{n}"] = down
                    proj_data[f"proj_up_{n}_visible"] = up_touched
                    proj_data[f"proj_down_{n}_visible"] = down_touched
                    if up_touched:
                        ax.axhline(up, color="darkorange", linestyle=":", linewidth=1.4, label=f"Proj +{n}")
                    if down_touched:
                        ax.axhline(down, color="purple", linestyle=":", linewidth=1.4, label=f"Proj -{n}")

                tick_step = max(1, len(day_data) // 12)
                ax.set_xticks(x[::tick_step])
                ax.set_xticklabels(day_data.loc[::tick_step, "time_local"].dt.strftime("%H:%M"), rotation=45)
                ax.set_xlim(x[0] - 0.5, x[-1] + 0.5)
                ax.set_ylim(y_floor, y_ceil)

                # Trading session vertical lines (UTC → broker local)
                sessions = [
                    ("Asia/Tokyo", 0, "cyan"),
                    ("Europa/London", 8, "orange"),
                    ("America/NY", 13, "magenta"),
                ]
                for s_name, s_hour_utc, s_color in sessions:
                    s_hour_local = s_hour_utc  # server time = UTC
                    # Find first candle at or after this hour
                    s_idx = None
                    for idx, row in day_data.iterrows():
                        if row["time_local"].hour >= s_hour_local:
                            s_idx = idx
                            break
                    if s_idx is not None and s_idx < len(day_data):
                        ax.axvline(s_idx, color=s_color, linestyle="--",
                                   linewidth=1.0, alpha=0.5, label=s_name)
                handles, labels = ax.get_legend_handles_labels()
                ax.legend(dict(zip(labels, handles)).values(), dict(zip(labels, handles)).keys(), loc="best", ncols=2)
                ax.set_title(f"Candles M5 com canal/projecoes — {day}")
                ax.set_xlabel("Hora (servidor MT5)")
                ax.set_ylabel("Preco")
                plt.tight_layout()

                buf = BytesIO()
                fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
                plt.close(fig)
                buf.seek(0)
                image_data = f"data:image/png;base64,{base64.b64encode(buf.read()).decode('ascii')}"

                summary = {
                    "day": day, "channel_high": ch_high, "channel_low": ch_low,
                    "channel_width": ch_width, "projection_distance": proj_dist,
                    **proj_data,
                }
    except Exception as e:
        error = str(e)

    # ── Higher-timeframe channel analysis (H4/D1, 3/6/9/12 meses) ────────
    # Roda somente apos a escolha explicita dos parametros (tf + meses)
    tf_arg = request.args.get("tf", "").upper()
    months_raw = request.args.get("months", "").strip()
    tf_label = tf_arg if tf_arg in ("H4", "D1") else ""
    if tf_label and months_raw in ("3", "6", "9", "12"):
        months = int(months_raw)
    else:
        months = 0

    image_data_hf = None
    hf_summary = {}
    hf_error = None

    if tf_label and months:
        if broker and broker.connected:
            try:
                import MetaTrader5 as mt5
                from datetime import timedelta as _td_hf
                symbol = config.get_str("SYMBOL", "XAUUSD") if config else "XAUUSD"
                to_dt = broker.get_server_time()
                from_dt = to_dt - _td_hf(days=months * 30)
                mt5_tf = mt5.TIMEFRAME_D1 if tf_label == "D1" else mt5.TIMEFRAME_H4
                hf_df = broker.get_rates_range(from_dt, to_dt, symbol=symbol, timeframe=mt5_tf)
                if hf_df is None or len(hf_df) < 40:
                    hf_error = f"Dados insuficientes ({0 if hf_df is None else len(hf_df)} candles) para {tf_label}/{months} meses"
                else:
                    # Piso de 200: no maximo ~10 canais no range 3000-5000
                    analysis = analyze_channels(hf_df, min_channel_width=200.0)
                    image_data_hf = render_channel_chart(
                        hf_df, analysis, tf_label=tf_label, months=months, symbol=symbol)
                    hf_summary = {
                        "tf": tf_label,
                        "months": months,
                        "n_candles": int(len(hf_df)),
                        "primary_width": analysis["primary_width"],
                        "secondary_width": analysis["secondary_width"],
                        "anchor": analysis["anchor"],
                        "zones": analysis["zones"],
                    }
            except Exception as e:
                hf_error = str(e)
        else:
            hf_error = "MT5 indisponivel para o grafico de canais"

    return render_template(
        "chart.html", day=day, image_data=image_data, summary=summary, error=error,
        image_data_hf=image_data_hf, hf_summary=hf_summary, hf_error=hf_error,
        tf=tf_label, months=months,
    )


def _render_equity_chart(series: list, account: str, subtitle: str = "") -> str:
    """Cumulative balance evolution, trade by trade → PNG b64."""
    import base64
    from io import BytesIO

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    if not series:
        return ""
    x = np.arange(len(series))
    equity = [s["equity"] for s in series]
    fig, ax = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#0d0d12")
    ax.set_facecolor("#0d0d12")
    ax.plot(x, equity, color="#FFD700", linewidth=1.6, marker="o", markersize=5)
    ax.fill_between(x, 0, equity, color="#FFD700", alpha=0.15)
    ax.axhline(0, color="#555555", linewidth=1)
    title = f"Evolucao do Saldo (trade a trade) — conta {account}"
    if subtitle:
        title += f" | {subtitle}"
    ax.set_title(title, color="#D4AF37", fontsize=12)
    ax.set_xlabel("Trade (ordem de fechamento)", color="#e0e0e0")
    ax.set_ylabel("Saldo acumulado (USD)", color="#e0e0e0")
    ax.tick_params(colors="#e0e0e0", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    ax.grid(alpha=0.15)
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def _render_trade_chart(df, trade: dict, tf_label: str) -> str:
    """Candles + entry/exit prices + connecting line for one trade → PNG b64."""
    import base64
    from io import BytesIO

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    times = df.index.to_numpy()

    entry = float(trade.get("entry_price") or 0)
    exit_p = float(trade.get("exit_price") or 0)
    open_dt = datetime.fromisoformat(trade["open_time"])
    close_dt = datetime.fromisoformat(trade["close_time"])

    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor("#0d0d12")
    ax.set_facecolor("#0d0d12")
    for i in range(len(df)):
        color = "#00c853" if c[i] >= o[i] else "#ff3d3d"
        ax.vlines(times[i], l[i], h[i], color=color, linewidth=0.8, alpha=0.9)
        ax.vlines(times[i], min(o[i], c[i]), max(o[i], c[i]),
                  color=color, linewidth=4.0, alpha=0.9)

    ax.hlines(entry, open_dt, close_dt, color="#FFD700", linewidth=1.4, linestyle="--")
    ax.hlines(exit_p, open_dt, close_dt, color="#00BFFF", linewidth=1.4, linestyle="--")
    ax.plot([open_dt, close_dt], [entry, exit_p], color="#FFD700", linewidth=2.2)
    ax.annotate(f"Entrada {entry:.2f}", (open_dt, entry),
                textcoords="offset points", xytext=(8, 8), color="#FFD700", fontsize=9)
    ax.annotate(f"Saida {exit_p:.2f}", (close_dt, exit_p),
                textcoords="offset points", xytext=(-8, 8), color="#00BFFF",
                fontsize=9, ha="right")

    # Margins: show full candles above/below and >= 2 candles on each side
    lo = min(float(l.min()), entry, exit_p)
    hi = max(float(h.max()), entry, exit_p)
    pad_y = max((hi - lo) * 0.18, 1e-6)
    ax.set_ylim(lo - pad_y, hi + pad_y)
    ax.set_xlim(times[0], times[-1])

    profit = float(trade.get("profit") or 0)
    ax.set_title(
        f"Trade #{trade['ticket']} ({trade.get('direction')}) — {tf_label} | "
        f"Lucro: {profit:.2f} | Δ Preco: {trade.get('delta_price')}",
        color="#D4AF37", fontsize=12)
    ax.tick_params(colors="#e0e0e0", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    ax.grid(alpha=0.15)

    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def _series_from(trades: list) -> list:
    """Cumulative equity series from a filtered trade list."""
    cum = 0.0
    series = []
    for t in trades:
        cum = round(cum + float(t.get("profit", 0) or 0), 2)
        series.append({
            "ticket": t["ticket"],
            "profit": t.get("profit", 0),
            "equity": cum,
            "close_time": t.get("close_time") or t.get("open_time"),
        })
    return series


@bp.route("/conta")
def conta():
    """Extrato da conta: evolucao do saldo trade a trade + lista de trades."""
    ledger = getattr(current_app, "ledger", None)
    broker = getattr(current_app, "broker", None)
    config = getattr(current_app, "config_obj", None)

    selected = request.args.get("account", "").strip()
    start_arg = request.args.get("start", "").strip()
    end_arg = request.args.get("end", "").strip()
    last_arg = request.args.get("last", "").strip()
    accounts: list = []
    if ledger is not None:
        accounts = ledger.accounts()
        if not selected and accounts:
            selected = accounts[0]

    trades: list = []
    if ledger is not None:
        trades = ledger.trades(selected) if selected else []
        # On-demand backfill when the ledger is empty (first visit)
        if not trades and broker is not None and broker.connected and config:
            try:
                start_str = config.get_str("CHALLENGE_START", CHALLENGE_START)
                start_dt = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc)
                to_dt = broker.get_server_time()
                ranged = broker.get_closed_trades_range(start_dt, to_dt)
                if ranged:
                    account = config.get_str("MT5_LOGIN", "")
                    for t in ranged:
                        t["account"] = account
                    ledger.upsert(ranged)
                    trades = ledger.trades(selected)
            except Exception:
                logger.exception("Backfill ledger failed")

    # ── window filter: dates have priority over last-N ──
    filter_label = ""
    if trades:
        if start_arg or end_arg:
            try:
                s = (datetime.strptime(start_arg, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                     if start_arg else None)
                e = (datetime.strptime(end_arg, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                     if end_arg else None)
                if e is not None:
                    e = e + timedelta(days=1) - timedelta(seconds=1)
                filtered = []
                for t in trades:
                    ct = t.get("close_time") or t.get("open_time")
                    if not ct:
                        continue
                    d = datetime.fromisoformat(ct)
                    if s is not None and d < s:
                        continue
                    if e is not None and d > e:
                        continue
                    filtered.append(t)
                trades = filtered
                filter_label = f"{start_arg or 'inicio'} a {end_arg or 'hoje'}"
            except (ValueError, TypeError):
                filter_label = ""
        elif last_arg:
            try:
                n = int(last_arg)
                if n > 0:
                    trades = trades[-n:]
                    filter_label = f"ultimos {n} trades"
            except (TypeError, ValueError):
                pass

    image_data = None
    if ledger is not None and selected and trades:
        image_data = _render_equity_chart(_series_from(trades), selected, filter_label)

    return render_template(
        "conta.html", accounts=accounts, selected=selected, trades=trades,
        image_data=image_data, challenge_start=CHALLENGE_START,
        start=start_arg, end=end_arg, last=last_arg, filter_label=filter_label,
    )


@bp.route("/conta/trade/<int:ticket>")
def conta_trade_chart(ticket: int):
    """Popup chart for one trade: candles + entry/exit + connecting line."""
    ledger = getattr(current_app, "ledger", None)
    broker = getattr(current_app, "broker", None)
    config = getattr(current_app, "config_obj", None)
    trade = ledger.get(ticket) if ledger is not None else None
    if not trade:
        return jsonify({"error": "trade nao encontrado"}), 404
    try:
        open_dt = datetime.fromisoformat(trade["open_time"])
        close_dt = datetime.fromisoformat(trade["close_time"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "datas invalidas no registro"}), 400
    if broker is None or not broker.connected:
        return jsonify({"error": "MT5 indisponivel"}), 503

    import MetaTrader5 as mt5
    from datetime import timedelta as _td
    duration = close_dt - open_dt
    seconds = max(duration.total_seconds(), 60.0)
    if seconds <= 3600:
        tf, tf_label = mt5.TIMEFRAME_M1, "M1"
    elif seconds <= 6 * 3600:
        tf, tf_label = mt5.TIMEFRAME_M5, "M5"
    elif seconds <= 24 * 3600:
        tf, tf_label = mt5.TIMEFRAME_M15, "M15"
    elif seconds <= 7 * 24 * 3600:
        tf, tf_label = mt5.TIMEFRAME_H1, "H1"
    else:
        tf, tf_label = mt5.TIMEFRAME_H4, "H4"

    # Guarantee at least 2 candles before and after the trade
    tf_seconds = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400}[tf_label]
    pad = max(_td(seconds=tf_seconds * 2), duration * 0.35)
    symbol = trade.get("symbol") or (config.get_str("SYMBOL", "XAUUSDC") if config else "XAUUSDC")
    try:
        df = broker.get_rates_range(open_dt - pad, close_dt + pad,
                                    symbol=symbol, timeframe=tf)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if df is None or len(df) < 2:
        return jsonify({"error": "sem candles para esse periodo"}), 404

    image = _render_trade_chart(df, trade, tf_label)
    return jsonify({
        "image": image,
        "trade": {
            "ticket": trade["ticket"], "account": trade.get("account"),
            "direction": trade.get("direction"),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "profit": trade.get("profit"), "sl": trade.get("sl"),
            "tp": trade.get("tp"), "open_time": trade.get("open_time"),
            "close_time": trade.get("close_time"),
        },
    })


@bp.route("/debug")
def debug():
    session = getattr(current_app, "session", None)
    broker = getattr(current_app, "broker", None)
    closed_trades = getattr(current_app, "closed_trades", [])
    # If empty, try direct MT5 query
    if not closed_trades and broker and broker.connected:
        try:
            import pandas as pd
            from datetime import timedelta as _td
            now = broker.get_server_time()
            day_start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
            day_end = day_start + _td(days=1) - _td(seconds=1)
            deals = __import__("MetaTrader5", fromlist=["history_deals_get"]).history_deals_get(
                day_start, day_end,
            )
            if deals is not None and len(deals) > 0:
                df = pd.DataFrame(list(deals))
                if "entry" in df.columns:
                    closed_deals = df[df["entry"] == 1]
                    for _, row in closed_deals.iterrows():
                        closed_trades.append({
                            "ticket": int(row.get("position_id", 0)),
                            "profit": float(row.get("profit", 0)),
                            "time": str(pd.to_datetime(row["time"], unit="s")),
                        })
        except Exception:
            pass

    return jsonify({
        "server_time": datetime.now(timezone.utc).isoformat(),
        "session": session.to_dict() if session else None,
        "closed_trades": closed_trades[-20:],
    })


def _empty_session():
    from app.core.session import TradingSession
    broker = getattr(current_app, "broker", None)
    try:
        if broker and broker.connected:
            now = broker.get_server_time()
        else:
            now = datetime.now(timezone.utc)
    except Exception:
        now = datetime.now(timezone.utc)
    return TradingSession(day_key=now.strftime("%Y-%m-%d"))


def _build_block_details(state: dict) -> list[dict]:
    """Build block_details list from state dict + active risk limits."""
    from flask import current_app
    flags = state.get("flags", {})
    risk = getattr(current_app, "risk", None)
    limits = risk.limits if risk else None
    return [
        {
            "name": "Lucro Diario", "key": "block_by_profit",
            "current": round(state.get("daily_profit", 0), 2),
            "limit": limits.limite_lucro_diario if limits else 750,
            "unit": "USD", "blocked": bool(flags.get("block_by_profit")),
        },
        {
            "name": "Drawdown Diario", "key": "block_by_loss",
            "current": round(state.get("current_drawdown", 0), 2),
            "limit": abs(limits.limite_perda_diario) if limits else 350,
            "unit": "USD", "blocked": bool(flags.get("block_by_loss")),
        },
        {
            "name": "Operacoes", "key": "block_by_operations",
            "current": state.get("operations_count", 0),
            "limit": limits.limite_operacoes if limits else 10,
            "unit": "ops", "blocked": bool(flags.get("block_by_operations")),
        },
        {
            "name": "Janela de Trading", "key": "block_by_time",
            "current": "-", "limit": "-", "unit": "",
            "blocked": bool(flags.get("block_by_time")),
        },
        {
            "name": "Overtrading", "key": "overtrading",
            "current": "-",
            "limit": f"{limits.overtrading_protection_minutes if limits else 12}min",
            "unit": "", "blocked": bool(flags.get("overtrading")),
        },
    ]
