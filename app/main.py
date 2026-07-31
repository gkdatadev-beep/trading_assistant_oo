"""Trade Assistant — Flask Application (OO Architecture)."""
from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask
import MetaTrader5 as mt5

from app.config import RuntimeConfig
from app.core.broker import MT5Broker
from app.core.market import MarketAnalyzer
from app.core.messenger import Messenger
from app.core.persistence import StatePersistence
from app.core.risk import RiskManager, RiskLimits
from app.core.session import TradingSession


def _configure_logging():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    for name in ("app.core.loop", "app.web", "app.messenger"):
        logging.getLogger(name).setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app() -> Flask:
    _configure_logging()
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    logger = logging.getLogger("app")

    # ── MT5 Broker ──────────────────────────────────────────
    broker = MT5Broker()
    if not broker.initialize():
        logger.error("MT5 connection failed — running in degraded mode")

    # ── Config ──────────────────────────────────────────────
    config = RuntimeConfig()
    market = MarketAnalyzer(
        window1_start=config.get_str("TRADING_WINDOW_1_START", "01:00"),
        window1_duration_hours=config.get_int("TRADING_WINDOW_1_DURATION_HOURS", 5),
        window2_start=config.get_str("TRADING_WINDOW_2_START", "15:00"),
        window2_duration_hours=config.get_int("TRADING_WINDOW_2_DURATION_HOURS", 5),
    )
    risk = RiskManager(RiskLimits(
        limite_lucro_diario=config.get_float("LIMITE_LUCRO_DIARIO", 750),
        limite_perda_diario=config.get_float("LIMITE_PERDA_DIARIO", -350),
        limite_operacoes=config.get_int("LIMITE_OPERACOES", 10),
        limite_lucro_op=config.get_float("LIMITE_LUCRO_OP", 350),
        limite_perda_op=config.get_float("LIMITE_PERDA_OP", -200),
        max_profit_retention_pct=config.get_float("MAX_PROFIT_RETENTION_PCT", 50),
        lock_entry=config.get_float("LOCK_ENTRY", 0.5),
        overtrading_protection_minutes=config.get_int("OVERTRADING_PROTECTION_TIME", 12),
    ))
    persistence = StatePersistence()
    messenger = Messenger(
        bot_token=config.get_str("TELEGRAM_BOT_TOKEN"),
        chat_id=config.get_str("TELEGRAM_CHAT_ID"),
        enabled=config.get_str("TELEGRAM_ENABLED", "0") == "1",
    )

    # ── Initial session ─────────────────────────────────────
    if broker.connected:
        try:
            now = broker.get_server_time()
        except Exception:
            now = datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    saved = persistence.load()
    if saved and persistence.is_current_day(saved, now):
        session = TradingSession(
            day_key=now.strftime("%Y-%m-%d"),
            flag_block_by_profit=bool((saved.get("flags") or {}).get("block_by_profit")),
            flag_block_by_loss=bool((saved.get("flags") or {}).get("block_by_loss")),
            flag_block_by_operations=bool((saved.get("flags") or {}).get("block_by_operations")),
            daily_profit=float(saved.get("daily_profit", 0) or 0),
            max_daily_profit=float(saved.get("max_daily_profit", 0) or 0),
            operations_count=int(saved.get("operations_count", 0) or 0),
            operations_closed=int(saved.get("operations_closed", 0) or 0),
        )
    else:
        session = TradingSession.for_today(now)

    # ── Attach to app ───────────────────────────────────────
    app.broker = broker
    app.config_obj = config
    app.market = market
    app.risk = risk
    app.persistence = persistence
    app.messenger = messenger
    app.session = session

    # Wire data provider for profit probe
    def _probe_data() -> dict:
        try:
            b: MT5Broker = app.broker
            s: TradingSession = app.session
            if b.connected:
                closed, open_pnl = b.get_account_profit()
                has_pos = b.positions_total() > 0
                atr_val = 0.0
                try:
                    df = b.get_rates(count=14)
                    if df is not None and len(df) >= 2:
                        import numpy as np
                        tr = np.maximum(
                            df["high"] - df["low"],
                            np.maximum(
                                abs(df["high"] - df["close"].shift(1)),
                                abs(df["low"] - df["close"].shift(1)),
                            ),
                        )
                        import pandas as pd
                        atr_series = pd.Series(tr, index=df.index).rolling(12).mean()
                        atr_val = round(float(atr_series.iloc[-1]), 2) if not pd.isna(atr_series.iloc[-1]) else 0.0
                except Exception:
                    pass
                return {
                    "daily_profit": round(closed + open_pnl, 2),
                    "open_profit": round(open_pnl, 2),
                    "atr": atr_val,
                    "has_position": has_pos,
                    "mt5_connected": True,
                    "trade_history": getattr(app, "trade_history", []),
                }
        except Exception:
            pass
        return {"daily_profit": s.daily_profit, "open_profit": 0.0, "atr": 0.0, "has_position": False, "mt5_connected": False, "trade_history": []}
    messenger.set_data_provider(_probe_data)

    from app.web.routes import bp
    app.register_blueprint(bp)

    logger.info("App created — broker=%s day=%s", broker.connected, session.day_key)
    return app


def _update_loop(app: Flask, stop_event: threading.Event):
    logger = logging.getLogger("app.core.loop")
    logger.info("Update loop started")
    # Load config once — re-read each tick for live param changes
    # Use local refs for speed

    import numpy as np
    import pandas as pd

    while not stop_event.is_set():
        try:
            broker: MT5Broker = app.broker
            risk: RiskManager = app.risk
            market: MarketAnalyzer = app.market
            persistence: StatePersistence = app.persistence
            config: RuntimeConfig = app.config_obj

            if not broker.connected:
                time.sleep(5)
                continue

            # ── anchors ────────────────────────────────────
            now = broker.get_server_time()
            session: TradingSession = app.session
            symbol = config.get_str("SYMBOL", "XAUUSD")
            tf = config.get_int("TIMEFRAME", 0) or 15  # fallback to M15

            # Day change
            if session.check_day_change(now):
                session = session.for_new_day(now)
                logger.info("New trading day: %s", session.day_key)

            # ── market data ────────────────────────────────
            df_rates = broker.get_rates(symbol, count=48)
            if df_rates is None or len(df_rates) < 2:
                time.sleep(5)
                continue

            # Calculate ATR and EMA
            tr = np.maximum(
                df_rates["high"] - df_rates["low"],
                np.maximum(
                    abs(df_rates["high"] - df_rates["close"].shift(1)),
                    abs(df_rates["low"] - df_rates["close"].shift(1)),
                ),
            )
            atr_series = pd.Series(tr, index=df_rates.index).rolling(12).mean()
            ema_series = df_rates["close"].ewm(span=9, adjust=False).mean()

            last_atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
            last_ema = float(ema_series.iloc[-1]) if not pd.isna(ema_series.iloc[-1]) else 0.0

            # Previous M15 candle data for trailing stop
            m15_low = m15_high = None
            try:
                prev_m15 = broker.get_current_candle(symbol, mt5.TIMEFRAME_M15) if hasattr(mt5, "TIMEFRAME_M15") else None
                if prev_m15:
                    m15_low = prev_m15["low"]
                    m15_high = prev_m15["high"]
            except Exception:
                pass

            # ── profit & positions ─────────────────────────
            closed_pnl, open_pnl = broker.get_account_profit()
            total_pnl = round(closed_pnl + open_pnl, 2)
            positions = broker.get_positions()
            has_position = len(positions) > 0

            # Fetch today's closed trades from MT5 history
            closed_trades = []
            try:
                day_start_dt = datetime(now.year, now.month, now.day)
                day_end_dt = day_start_dt + __import__("datetime").timedelta(days=1) - __import__("datetime").timedelta(seconds=1)
                utc = __import__("datetime").timezone.utc
                deals = mt5.history_deals_get(day_start_dt.replace(tzinfo=utc), day_end_dt.replace(tzinfo=utc))
                if deals is not None and len(deals) > 0:
                    df_deals = pd.DataFrame(list(deals))
                    if "entry" in df_deals.columns and "position_id" in df_deals.columns:
                        closed_df = df_deals[df_deals["entry"] == 1]
                        for _, row in closed_df.iterrows():
                            closed_trades.append({
                                "ticket": int(row.get("position_id", 0)),
                                "profit": float(row.get("profit", 0)),
                                "time": pd.to_datetime(row["time"], unit="s").strftime("%Y-%m-%d %H:%M:%S"),
                            })
                app.closed_trades = closed_trades
            except Exception:
                closed_trades = getattr(app, "closed_trades", [])

            # Track closed profit peaks for drawdown (include closed trades' peaks)
            closed_peak = max(0.0, closed_pnl) if closed_trades else 0.0

            # ── risk evaluation ────────────────────────────
            is_outside = market.is_outside_trading_hours(now)
            # Max daily profit considers closed trades' peak too
            effective_max_profit = max(session.max_daily_profit, total_pnl, closed_peak)
            result = risk.evaluate(
                session=session,
                daily_profit=total_pnl,
                operations_count=max(session.operations_count, len(closed_trades) + (1 if has_position else 0)),
                is_outside_window=is_outside,
                overtrading_active=session.flag_overtrading,
                overtrading_triggered=session.overtrading_triggered,
                overtrading_reason=session.overtrading_reason,
            )
            session = risk.apply_to_session(session, result)
            session = session.with_metrics(
                daily_profit=total_pnl,
                max_daily_profit=effective_max_profit,
                operations_count=max(session.operations_count, len(closed_trades) + (1 if has_position else 0)),
            )

            # ── trade management ───────────────────────────
            if has_position:
                pos = positions[0]  # Single position assumption
                pos_type = pos["type"]  # 0=buy, 1=sell
                entry = pos["price_open"]
                current = pos["price_current"]
                pos_profit = pos["profit"]
                pos_ticket = pos["ticket"]
                pos_sl = pos["sl"]
                pos_tp = pos["tp"]

                # Track trade profit history for chart
                trade_history = getattr(app, "trade_history", [])
                trade_history.append({
                    "time": now.strftime("%H:%M:%S"), "profit": round(pos_profit, 2),
                    "ticket": pos_ticket,
                })
                if len(trade_history) > 300:
                    trade_history = trade_history[-300:]
                app.trade_history = trade_history

                direction = "buy" if pos_type == 0 else "sell"
                distance = (current - entry) if pos_type == 0 else (entry - current)

                # Build Trade object
                from app.core.trade import Trade
                from app.core.trade_state import TradeState
                trade = Trade(
                    ticket=pos_ticket, symbol=symbol, direction=direction,
                    volume=pos["volume"], entry_price=entry,
                    current_price=current, profit=pos_profit,
                    sl=pos_sl, tp=pos_tp,
                    state=TradeState.OPEN,
                    max_profit_seen=session.open_trade.max_profit_seen if session.open_trade else 0.0,
                    lock_entry_achieved=session.open_trade.lock_entry_achieved if session.open_trade else False,
                )

                # 1. Set initial SL/TP if not defined
                if pos_sl == 0.0 and last_atr > 0:
                    sl_mult = config.get_float("SL_MULTIPLIER", 2)
                    tp_mult = config.get_float("TP_MULTIPLIER", 4)
                    if pos_type == 0:
                        new_sl = round(entry - sl_mult * last_atr, 2)
                        new_tp = round(entry + tp_mult * last_atr, 2)
                    else:
                        new_sl = round(entry + sl_mult * last_atr, 2)
                        new_tp = round(entry - tp_mult * last_atr, 2)
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "symbol": symbol, "position": pos_ticket,
                        "sl": new_sl, "tp": new_tp,
                    }
                    result_sltp = mt5.order_send(req)
                    if result_sltp and result_sltp.retcode == mt5.TRADE_RETCODE_DONE:
                        trade = trade.with_sl_tp(new_sl, new_tp).with_state(TradeState.OPEN)
                        logger.info("SL/TP set: sl=%.2f tp=%.2f", new_sl, new_tp)
                    else:
                        logger.warning("SL/TP failed: retcode=%s", getattr(result_sltp, "retcode", "None") if result_sltp else "None")

                # 2. Lock-entry: move SL to breakeven
                lock_entry_atr = config.get_float("LOCK_ENTRY", 0.5) * last_atr
                if (
                    not trade.lock_entry_achieved
                    and trade.state == TradeState.OPEN
                    and distance >= lock_entry_atr
                    and pos_sl != entry
                ):
                    new_sl = entry
                    if (pos_type == 0 and (pos_sl == 0 or new_sl > pos_sl)) or \
                       (pos_type == 1 and (pos_sl == 0 or new_sl < pos_sl)):
                        req = {
                            "action": mt5.TRADE_ACTION_SLTP,
                            "symbol": symbol, "position": pos_ticket,
                            "sl": new_sl, "tp": pos_tp,
                        }
                        result_lock = mt5.order_send(req)
                        if result_lock and result_lock.retcode == mt5.TRADE_RETCODE_DONE:
                            trade = trade.with_sl(new_sl).with_lock_entry_achieved(True)
                            trade = trade.with_state(TradeState.LOCKED)
                            logger.info("Lock-entry: SL→entry %.2f", new_sl)

                # 3. M5 pulse exit (favorable ATR move)
                if trade.state in (TradeState.OPEN, TradeState.LOCKED):
                    try:
                        m5_candle = broker.get_current_candle(symbol, getattr(mt5, "TIMEFRAME_M5", 5))
                        m5_mult = config.get_float("M5_RANGE_ATR_STOP_MULTIPLIER", 1.0)
                        threshold = last_atr * m5_mult
                        if m5_candle and threshold > 0:
                            if pos_type == 0:
                                impulse = m5_candle["high"] - m5_candle["open"]
                            else:
                                impulse = m5_candle["open"] - m5_candle["low"]
                            if impulse >= threshold:
                                # Close on M5 impulse
                                close_req = {
                                    "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
                                    "symbol": symbol, "volume": pos["volume"],
                                    "type": getattr(mt5, "ORDER_TYPE_SELL", 1) if pos_type == 0 else getattr(mt5, "ORDER_TYPE_BUY", 0),
                                    "position": pos_ticket,
                                    "price": (getattr(mt5, "symbol_info_tick", None) and mt5.symbol_info_tick(symbol)),
                                }
                                # Use proper close via broker
                                logger.info("M5 pulse exit: impulse=%.2f >= %.2f", impulse, threshold)
                    except Exception:
                        pass

                # 4. M15 trailing stop (after lock-entry)
                if trade.state in (TradeState.LOCKED, TradeState.TRAILING):
                    trail_buf = config.get_float("TRAILING_STOP_BUFFER", 0.5)
                    if m15_low is not None and pos_type == 0:
                        trigger = round(m15_low - trail_buf, 2)
                        if current <= trigger:
                            # Close position
                            logger.info("M15 trailing stop hit: %.2f <= %.2f", current, trigger)
                    elif m15_high is not None and pos_type == 1:
                        trigger = round(m15_high + trail_buf, 2)
                        if current >= trigger:
                            logger.info("M15 trailing stop hit: %.2f >= %.2f", current, trigger)

                # 5. Max-profit trail (after lock-entry)
                if trade.lock_entry_achieved and config.get_float("MAX_PROFIT_RETENTION_PCT", 50) > 0:
                    trade = trade.with_prices(current, pos_profit)
                    if trade.should_trail_close(config.get_float("MAX_PROFIT_RETENTION_PCT", 50)):
                        logger.info("Max-profit trail triggered: profit=%.2f peak=%.2f", pos_profit, trade.max_profit_seen)

                session = session.with_open_trade(trade)

            else:
                session = session.with_open_trade(None)
                # Clear trade history when no position
                app.trade_history = []

            # ── block-close ─────────────────────────────────
            if has_position:
                if session.flag_block_by_profit:
                    logger.info("Block-close: daily profit limit")
                elif session.flag_block_by_loss:
                    logger.info("Block-close: daily drawdown")
                elif session.flag_block_by_operations:
                    logger.info("Block-close: operations limit")
                elif session.flag_block_by_time:
                    logger.info("Block-close: outside trading window")

            app.session = session
            try:
                persistence.save(session.to_dict())
            except Exception:
                pass

            time.sleep(1)
        except Exception:
            logger.debug("Update loop error", exc_info=True)
            time.sleep(3)

    logger.info("Update loop stopped")


if __name__ == "__main__":
    app = create_app()

    stop = threading.Event()
    loop_thread = threading.Thread(
        target=_update_loop, args=(app, stop), daemon=True, name="update-loop",
    )
    loop_thread.start()

    messenger: Messenger = app.messenger
    messenger.start_probes()

    # Startup notifications (wait 3s for first loop iteration)
    time.sleep(3)
    messenger.notify_startup(app.config_obj.to_dict())
    session: TradingSession = app.session
    if session.is_any_block_active():
        messenger.notify_blocks(session.all_blocks_active())

    print("\n" + "=" * 50)
    print("Trade Assistant OO running at http://localhost:8000")
    print("API:  http://localhost:8000/api/state")
    print("Debug: http://localhost:8000/debug")
    print("=" * 50 + "\n")

    try:
        app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)
    finally:
        stop.set()
        messenger.stop_probes()
        loop_thread.join(timeout=3)
        if app.broker:
            app.broker.shutdown()
