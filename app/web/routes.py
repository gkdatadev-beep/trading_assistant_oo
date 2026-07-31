"""Flask routes — minimal, stable."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/api/state")
def api_state():
    """Return current session + challenge state as JSON."""
    try:
        session = getattr(current_app, "session", None)
        broker = getattr(current_app, "broker", None)
        config = getattr(current_app, "config_obj", None)

        from app.web.dashboard import Dashboard
        state = Dashboard.get_state(
            session=session or _empty_session(),
            challenge=None,
        )

        server_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if broker is not None and getattr(broker, "connected", False):
            try:
                server_str = broker.get_server_time().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        state["server_time"] = server_str
        state["mt5_connected"] = broker is not None and getattr(broker, "connected", False)

        # Block details: current value + limit for each flag
        from app.config import DISCIPLINE
        flags = state.get("flags", {})
        state["block_details"] = [
            {
                "name": "Lucro Diario",
                "key": "block_by_profit",
                "current": round(state.get("daily_profit", 0), 2),
                "limit": DISCIPLINE["LIMITE_LUCRO_DIARIO"],
                "unit": "USD",
                "blocked": bool(flags.get("block_by_profit")),
            },
            {
                "name": "Drawdown Diario",
                "key": "block_by_loss",
                "current": round(state.get("current_drawdown", 0), 2),
                "limit": abs(DISCIPLINE["LIMITE_PERDA_DIARIO"]),
                "unit": "USD",
                "blocked": bool(flags.get("block_by_loss")),
            },
            {
                "name": "Operacoes",
                "key": "block_by_operations",
                "current": state.get("operations_count", 0),
                "limit": DISCIPLINE["LIMITE_OPERACOES"],
                "unit": "ops",
                "blocked": bool(flags.get("block_by_operations")),
            },
            {
                "name": "Janela de Trading",
                "key": "block_by_time",
                "current": "-",
                "limit": "-",
                "unit": "",
                "blocked": bool(flags.get("block_by_time")),
            },
            {
                "name": "Overtrading",
                "key": "overtrading",
                "current": "-",
                "limit": f"{DISCIPLINE['OVERTRADING_PROTECTION_TIME']}min",
                "unit": "",
                "blocked": bool(flags.get("overtrading")),
            },
        ]

        # Trade history for chart
        trade_history = getattr(current_app, "trade_history", [])
        state["trade_history"] = trade_history[-120:] if trade_history else []

        return jsonify(state)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
                    changed = "\n".join(f"  {k}: {v}" for k, v in updates.items())
                    messenger.send(f"⚙️ Parametros atualizados:\n{changed}")
        except Exception as e:
            message = f"Erro: {e}"

    params = config.to_dict() if config else {}
    return render_template("parm.html", params=params, message=message)


@bp.route("/chart")
def chart():
    day = request.args.get("day", "")
    if not day:
        try:
            broker = getattr(current_app, "broker", None)
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
        broker = getattr(current_app, "broker", None)
        config = getattr(current_app, "config_obj", None)
        if broker and broker.connected and config:
            symbol = config.get_str("SYMBOL", "XAUUSD")
            import MetaTrader5 as mt5  # use M5 explicitly for 1st-hour channel

            import pandas as pd
            from datetime import timedelta as _td
            target = pd.to_datetime(day).date()
            day_start = datetime(target.year, target.month, target.day)
            day_end = day_start + _td(days=1) - _td(seconds=1)

            df = broker.get_rates_range(day_start, day_end, symbol=symbol, timeframe=mt5.TIMEFRAME_M5)
            if df is not None and len(df) >= 12:
                import numpy as np
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                from io import BytesIO
                import base64

                df = df.reset_index()
                df["day"] = pd.to_datetime(df["time"]).dt.date
                day_data = df[df["day"] == target].sort_values("time").reset_index(drop=True)

                first_hour = day_data.iloc[:12]
                ch_high = float(first_hour["high"].max())
                ch_low = float(first_hour["low"].min())
                ch_width = ch_high - ch_low
                proj_dist = ch_width * 2

                day_high = float(day_data["high"].max())
                day_low = float(day_data["low"].min())
                # Padding: 10% of the channel width above/below for visual comfort
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
                ax.set_xticklabels(day_data.loc[::tick_step, "time"].dt.strftime("%H:%M"), rotation=45)
                ax.set_xlim(x[0] - 0.5, x[-1] + 0.5)
                ax.set_ylim(y_floor, y_ceil)
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

    return render_template("chart.html", day=day, image_data=image_data, summary=summary, error=error)


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
            day_start = datetime(now.year, now.month, now.day)
            day_end = day_start + _td(days=1) - _td(seconds=1)
            utc = __import__("datetime").timezone.utc
            deals = __import__("MetaTrader5", fromlist=["history_deals_get"]).history_deals_get(
                day_start.replace(tzinfo=utc), day_end.replace(tzinfo=utc),
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
    return TradingSession(day_key=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
