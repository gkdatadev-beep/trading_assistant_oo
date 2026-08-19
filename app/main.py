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
from app.core.ledger import CHALLENGE_START, TradeLedger
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
    for name in ("app.core.loop", "app.web", "app.core.messenger"):
        logging.getLogger(name).setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app() -> Flask:
    _configure_logging()
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    logger = logging.getLogger("app")

    # ── Config ──────────────────────────────────────────────
    config = RuntimeConfig()

    # ── MT5 Broker (auto-login via config credentials) ──────
    broker = MT5Broker(
        login=config.get_str("MT5_LOGIN") or None,
        password=config.get_str("MT5_PASSWORD") or None,
        server=config.get_str("MT5_SERVER") or None,
    )
    if not broker.initialize():
        logger.error("MT5 connection failed — running in degraded mode")
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
    ledger = TradeLedger()
    ledger.set_account(config.get_str("MT5_LOGIN") or "")
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
            current_drawdown=float(saved.get("current_drawdown", 0) or 0),
            operations_count=int(saved.get("operations_count", 0) or 0),
            operations_closed=int(saved.get("operations_closed", 0) or 0),
        )
    else:
        # Archive previous day's state before resetting
        if saved:
            old_day = (saved.get("day_key") or "")[:10]
            if old_day and old_day != now.strftime("%Y-%m-%d"):
                try:
                    persistence.save_daily_snapshot(saved, old_day)
                    logger.info("Archived previous day snapshot: %s", old_day)
                except Exception:
                    pass
        session = TradingSession.for_today(now)

    # ── Attach to app ───────────────────────────────────────
    app.broker = broker
    app.config_obj = config
    app.market = market
    app.risk = risk
    app.persistence = persistence
    app.ledger = ledger
    app.ledger_last_sync = 0.0
    app.messenger = messenger
    app.session = session
    app.start_time = datetime.now(timezone.utc)
    app.last_loop_heartbeat = time.monotonic()  # for health monitoring

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
                    "server_start_time": getattr(app, "start_time", None),
                    "blocks_active": s.all_blocks_active() if s else [],
                    "is_any_block": s.is_any_block_active() if s else False,
                }
        except Exception:
            logger.warning("Probe data provider failed — MT5 may be unresponsive", exc_info=True)
        return {
            "daily_profit": s.daily_profit, "open_profit": 0.0, "atr": 0.0,
            "has_position": False, "mt5_connected": False, "trade_history": [],
        }
    messenger.set_data_provider(_probe_data)
    # Also provide broker local time for probe scheduling
    messenger.set_time_provider(
        lambda: (
            broker.get_server_time()
            if broker.connected
            else datetime.now(timezone.utc)
        ),
    )

    from app.web.routes import bp
    app.register_blueprint(bp)

    logger.info("App created — broker=%s day=%s", broker.connected, session.day_key)
    return app


def _send_deal_order(symbol: str, volume: float, order_type: int, position: int,
                     price: float, comment: str, deviation: int = 20, magic: int = 0,
                     logger=None) -> tuple:
    """Send a standardized TRADE_ACTION_DEAL with all required fields for non-demo brokers.

    Includes type_filling=ORDER_FILLING_IOC and type_time=ORDER_TIME_GTC
    which are REQUIRED by CTI/prop-firm servers but optional on MetaQuotes demo.

    Runs order_check() first for diagnostics, then order_send().

    Returns (result, error_message).
      - result: mt5.order_send result object on success, None on failure.
      - error_message: None on success, diagnostic string on failure.
    """
    import MetaTrader5 as _mt5
    _log = logger or logging.getLogger(__name__)

    request = {
        "action": _mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "position": position,
        "price": price,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_filling": _mt5.ORDER_FILLING_IOC,
        "type_time": _mt5.ORDER_TIME_GTC,
    }

    # ── Diagnostic check first ──────────────────────────
    try:
        check = _mt5.order_check(request)
    except Exception:
        _log.error("order_check exception for %s", comment)
        return None, "order_check exception"

    if check is None:
        msg = "order_check returned None — MT5 may be unresponsive"
        _log.error("%s (%s)", msg, comment)
        return None, msg
    if check.retcode != 0:
        msg = f"order_check: retcode={check.retcode} comment={check.comment}"
        _log.error("%s (%s)", msg, comment)
        return None, msg

    # ── Send ───────────────────────────────────────────
    try:
        result = _mt5.order_send(request)
    except Exception:
        _log.exception("order_send exception for %s", comment)
        return None, "order_send exception"

    if result is None:
        return None, "order_send returned None"
    if result.retcode != _mt5.TRADE_RETCODE_DONE:
        msg = f"retcode={result.retcode} comment={getattr(result, 'comment', '?')}"
        _log.error("order_send FAILED: %s (%s)", msg, comment)
        return result, msg

    _log.info("DEAL order OK: %s retcode=%s", comment, result.retcode)
    return result, None


def _enforce_lot_size(app: Flask, broker, config, symbol: str, positions: list, logger) -> None:
    """Ensure no position exceeds the configured LOT_SIZE.

    Rules (applied in order):
    1. Multiple positions → close all except one (keep the first/oldest).
    2. Single position with volume > LOT_SIZE → partial close to LOT_SIZE.

    Runs every tick, independent of trading blocks.
    """
    max_lot = config.get_float("LOT_SIZE", 0.15)
    if max_lot <= 0:
        return

    if not positions:
        return

    # ── Rule 1: too many positions ──────────────────────────
    if len(positions) > 1:
        logger.warning(
            "LOT ENFORCE: %d positions detected (max 1) — closing excess",
            len(positions),
        )
        # Keep the first position, close all others
        for pos in positions[1:]:
            _close_position(broker, symbol, pos, "lot_enforce_multi", logger, app)
        # After closing extras, re-read positions for volume check
        try:
            positions = broker.get_positions()
        except Exception:
            return
        if not positions:
            return

    # ── Rule 2: volume exceeds max ─────────────────────────
    pos = positions[0]
    current_vol = float(pos.get("volume", 0))
    if current_vol <= max_lot:
        return  # All good

    excess = round(current_vol - max_lot, 2)
    if excess <= 0:
        return

    logger.warning(
        "LOT ENFORCE: ticket=%s volume=%.2f > max=%.2f — closing excess %.2f",
        pos["ticket"], current_vol, max_lot, excess,
    )

    # Build a partial-close request for the excess volume
    pos_type = pos["type"]  # 0=buy, 1=sell
    try:
        tick_info = mt5.symbol_info_tick(symbol)
        close_price = tick_info.bid if pos_type == 0 else tick_info.ask
    except Exception:
        logger.error("LOT ENFORCE: cannot get tick for %s", symbol)
        return

    order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
    result, err = _send_deal_order(
        symbol, excess, order_type, pos["ticket"], close_price,
        "lot_size_enforce", logger=logger,
    )
    if result is not None:
        logger.info(
            "LOT ENFORCE: partial close OK — ticket=%s excess=%.2f remaining=%.2f",
            pos["ticket"], excess, max_lot,
        )
        app.messenger.send(
            f"⚠️ LOT ENFORCE #{pos['ticket']}\n"
            f"   Volume {current_vol} > max {max_lot}\n"
            f"   Fechado excesso: {excess} — restante: {max_lot}"
        )
    else:
        logger.error("LOT ENFORCE FAILED: %s ticket=%s", err, pos["ticket"])


def _close_position(broker, symbol: str, pos: dict, reason: str, logger, app,
                   label: str = "LOT ENFORCE") -> bool:
    """Close a single position entirely. Returns True on success."""
    pos_type = pos["type"]
    try:
        tick_info = mt5.symbol_info_tick(symbol)
        close_price = tick_info.bid if pos_type == 0 else tick_info.ask
    except Exception:
        logger.error("Cannot get tick to close position %s", pos["ticket"])
        return False

    order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
    result, err = _send_deal_order(
        symbol, pos["volume"], order_type, pos["ticket"], close_price,
        reason, logger=logger,
    )
    if result is not None:
        logger.info("Position %s closed: %s", pos["ticket"], reason)
        app.messenger.send(
            f"📕 {label} — posicao fechada\n"
            f"   Ticket #{pos['ticket']} | Motivo: {reason}"
        )
        return True
    else:
        logger.error("Close position %s FAILED: %s", pos["ticket"], err)
        return False


def _update_loop(app: Flask, stop_event: threading.Event):
    logger = logging.getLogger("app.core.loop")
    logger.debug("Update loop started")

    import numpy as np
    import pandas as pd

    # Heartbeat tracking for external watchdog
    last_heartbeat = time.monotonic()
    heartbeat_interval = 10  # seconds — if no heartbeat in 30s, watchdog should restart

    # Track consecutive empty/failure iterations
    consecutive_failures = 0
    max_consecutive_failures = 10

    while not stop_event.is_set():
        iteration_start = time.monotonic()
        try:
            broker: MT5Broker = app.broker
            risk: RiskManager = app.risk
            market: MarketAnalyzer = app.market
            persistence: StatePersistence = app.persistence
            config: RuntimeConfig = app.config_obj

            # ── HEALTH CHECK ──────────────────────────────────
            # Verify MT5 is actually alive before doing anything
            if not broker.check_health():
                logger.warning("MT5 health check failed — retrying in 5s")
                # Update heartbeat even when degraded
                last_heartbeat = time.monotonic()
                time.sleep(5)
                continue

            # ── anchors ─────────────────────────────────────
            try:
                now = broker.get_server_time()
            except Exception:
                logger.error("Cannot get broker server time — MT5 may be unresponsive")
                consecutive_failures += 1
                time.sleep(3)
                continue

            session: TradingSession = app.session
            symbol = config.get_str("SYMBOL", "XAUUSD")

            # ── Day change (based on MT5 server time) ──────
            if session.check_day_change(now):
                old_day = session.day_key
                try:
                    persistence.save_daily_snapshot(
                        session.to_dict(), session.day_key,
                    )
                except Exception:
                    logger.warning("Failed to save daily snapshot", exc_info=True)
                session = session.for_new_day(now)
                app.trade_history = []
                logger.info("New trading day: %s (was %s)", session.day_key, old_day)
                # Telegram notification
                app.messenger.send(
                    f"🌅 Novo dia de trading: {session.day_key}\n"
                    f"✅ Todos os bloqueios resetados\n"
                    f"🟢 Trades permitidos"
                )

            # ── market data ─────────────────────────────────
            try:
                df_rates = broker.get_rates(symbol, count=48)
            except Exception:
                logger.error("Failed to get rates", exc_info=True)
                consecutive_failures += 1
                time.sleep(5)
                continue

            if df_rates is None or len(df_rates) < 2:
                consecutive_failures += 1
                if consecutive_failures % 30 == 0:
                    logger.warning("No rate data for %d iterations — MT5 may be stale",
                                   consecutive_failures)
                time.sleep(5)
                continue

            consecutive_failures = 0  # Reset on successful data fetch

            # Calculate ATR and EMA
            tr = np.maximum(
                df_rates["high"] - df_rates["low"],
                np.maximum(
                    abs(df_rates["high"] - df_rates["close"].shift(1)),
                    abs(df_rates["low"] - df_rates["close"].shift(1)),
                ),
            )
            atr_series = pd.Series(tr, index=df_rates.index).rolling(12).mean()
            last_atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0

            # Previous (completed) M15 candle data for trailing stop
            m15_low = m15_high = None
            try:
                if hasattr(mt5, "TIMEFRAME_M15"):
                    prev_m15_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 1, 1)
                    if prev_m15_rates is not None and len(prev_m15_rates) > 0:
                        row = prev_m15_rates[0]
                        m15_low = float(row["low"])
                        m15_high = float(row["high"])
                    else:
                        logger.debug("M15 trailing: no previous candle data (len=%s)",
                                     len(prev_m15_rates) if prev_m15_rates is not None else "None")
                else:
                    logger.debug("M15 trailing: TIMEFRAME_M15 not available in mt5 module")
            except Exception:
                logger.debug("M15 candle fetch failed", exc_info=True)

            # ── profit & positions ──────────────────────────
            try:
                closed_pnl, open_pnl = broker.get_account_profit()
            except Exception:
                logger.error("Failed to get account profit", exc_info=True)
                closed_pnl, open_pnl = 0.0, 0.0

            total_pnl = round(closed_pnl + open_pnl, 2)

            try:
                positions = broker.get_positions()
            except Exception:
                logger.error("Failed to get positions", exc_info=True)
                positions = []
            has_position = len(positions) > 0

            # Fetch today's closed trades from MT5 history
            try:
                closed_trades = broker.get_closed_trades_today()
                account = config.get_str("MT5_LOGIN", "") if config else ""
                for ct in closed_trades:
                    ct["account"] = account
                app.closed_trades = closed_trades
            except Exception:
                logger.error("Failed to get closed trades", exc_info=True)
                closed_trades = getattr(app, "closed_trades", [])

            # ── trade ledger sync (challenge start → now) ──
            try:
                ledger = getattr(app, "ledger", None)
                if ledger is not None:
                    account = config.get_str("MT5_LOGIN", "") if config else ""
                    ledger.set_account(account)
                    last_sync = getattr(app, "ledger_last_sync", 0.0)
                    if time.monotonic() - last_sync >= 60:
                        start_str = (
                            config.get_str("CHALLENGE_START", CHALLENGE_START)
                            if config else CHALLENGE_START
                        )
                        start_dt = datetime.fromisoformat(start_str).replace(
                            tzinfo=timezone.utc)
                        now_utc = (
                            broker.get_server_time()
                            if broker.connected
                            else datetime.now(timezone.utc)
                        )
                        ranged = broker.get_closed_trades_range(start_dt, now_utc)
                        if ranged:
                            for t in ranged:
                                t["account"] = account
                            new_trades = ledger.upsert(ranged)
                            for nt in new_trades:
                                logger.info(
                                    "TRADE CLOSED account=%s ticket=%s dir=%s "
                                    "entry=%.2f exit=%.2f sl=%.2f tp=%.2f "
                                    "profit=%.2f delta=%.2f",
                                    account, nt["ticket"], nt.get("direction"),
                                    nt.get("entry_price", 0), nt.get("exit_price", 0),
                                    nt.get("sl", 0), nt.get("tp", 0),
                                    nt.get("profit", 0), nt.get("delta_price", 0),
                                )
                        app.ledger_last_sync = time.monotonic()
            except Exception:
                logger.error("Ledger sync failed", exc_info=True)

            # ── lot-size enforcement ───────────────────────
            # Runs EVERY tick regardless of blocks — ensures position
            # volume never exceeds the configured LOT_SIZE.
            _enforce_lot_size(app, broker, config, symbol, positions, logger)

            # Re-read positions after enforcement (partial close may have changed them)
            if has_position:
                try:
                    positions = broker.get_positions()
                    has_position = len(positions) > 0
                except Exception:
                    pass

            # Track closed profit peaks for drawdown
            closed_peak = max(0.0, closed_pnl) if closed_trades else 0.0

            # ── overtrading timed release ──────────────────
            if session.flag_overtrading and session.overtrading_protection_end is not None:
                if now >= session.overtrading_protection_end:
                    session = session.with_overtrading(
                        active=False, triggered=False, reason="", protection_end=None,
                    )
                    logger.info("Overtrading protection released (time expired)")

            # ── risk evaluation ────────────────────────────
            is_outside = market.is_outside_trading_hours(now)
            # Peak profit: session + current total + closed-trade peak
            effective_max_profit = max(
                session.max_daily_profit, total_pnl, closed_peak,
            )
            # Pre-update session so risk.evaluate sees the true peak
            session = session.with_metrics(max_daily_profit=effective_max_profit)
            result = risk.evaluate(
                session=session,
                daily_profit=total_pnl,
                operations_count=max(
                    session.operations_count,
                    len(closed_trades) + (1 if has_position else 0),
                ),
                is_outside_window=is_outside,
                overtrading_active=session.flag_overtrading,
                overtrading_triggered=session.overtrading_triggered,
                overtrading_reason=session.overtrading_reason,
            )
            session = risk.apply_to_session(session, result)
            # Recompute drawdown using the true peak (latched, never decreases)
            final_drawdown = max(
                session.current_drawdown,
                round(max(0.0, effective_max_profit - total_pnl), 2),
            )
            # Robust operations count: use MT5 history, fall back to session counter
            mt5_ops = len(closed_trades) + (1 if has_position else 0)
            robust_ops = max(session.operations_count, mt5_ops)
            session = session.with_metrics(
                daily_profit=total_pnl,
                current_drawdown=final_drawdown,
                operations_count=robust_ops,
            )

            # ── block enforcement: reject newly-opened positions ──
            if has_position and session.is_any_block_active() and session.open_trade is None:
                # A new position was opened while blocks are already active → close it
                pos = positions[0]
                block_names = session.all_blocks_active()
                reason = ("block_" + block_names[0].lower().replace(" ", "_")
                          if block_names else "block_active")
                logger.warning(
                    "BLOCK ENFORCE: closing new position #%s — blocks active: %s",
                    pos["ticket"], ", ".join(block_names),
                )
                _close_position(broker, symbol, pos, reason, logger, app, label="BLOQUEIO")
                has_position = False
                session = session.with_open_trade(None)

            # ── trade management ───────────────────────────
            from app.core.trade import Trade
            from app.core.trade_state import TradeState
            if has_position:
                pos = positions[0]  # Single position assumption
                pos_type = pos["type"]  # 0=buy, 1=sell
                direction = "buy" if pos_type == 0 else "sell"
                entry = pos["price_open"]
                current = pos["price_current"]
                pos_profit = pos["profit"]
                pos_ticket = pos["ticket"]
                pos_sl = pos["sl"]
                pos_tp = pos["tp"]
                distance = (current - entry) if pos_type == 0 else (entry - current)

                # Track trade profit history for chart (cumulative, full position life)
                trade_history = getattr(app, "trade_history", [])
                trade_history.append({
                    "time": now.strftime("%H:%M:%S"), "profit": round(pos_profit, 2),
                    "ticket": pos_ticket, "direction": direction,
                    "account": config.get_str("MT5_LOGIN", "") if config else "",
                })
                # Keep full history for the current position; trim to last 7200 points (2h at 1s)
                if len(trade_history) > 7200:
                    trade_history = trade_history[-7200:]
                app.trade_history = trade_history

                # Build Trade object — preserve previous state, lock flag and profit peak
                prev = session.open_trade
                trade = Trade(
                    ticket=pos_ticket, symbol=symbol, direction=direction,
                    volume=pos["volume"], entry_price=entry,
                    current_price=current, profit=pos_profit,
                    sl=pos_sl, tp=pos_tp,
                    state=prev.state if prev else TradeState.OPEN,
                    max_profit_seen=prev.max_profit_seen if prev else 0.0,
                    lock_entry_achieved=prev.lock_entry_achieved if prev else False,
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
                        # Trade already in OPEN state — just update SL/TP values
                        trade = trade.with_sl_tp(new_sl, new_tp)
                        logger.info("SL/TP set: sl=%.2f tp=%.2f", new_sl, new_tp)
                    else:
                        logger.warning(
                            "SL/TP failed: retcode=%s",
                            getattr(result_sltp, "retcode", "None")
                            if result_sltp else "None",
                        )

                # 1.5 Per-trade price-based stops (ALWAYS active, no lock-entry required)
                #   XAUUSD: 1.00 price move = 100 points. Config is in raw price units.
                #   Buy:  close when price rises  MAX_PROFIT_PRICE above entry  → take profit
                #         close when price falls  MAX_LOSS_PRICE  below entry   → stop loss
                #   Sell: reverse logic
                if trade.state != TradeState.CLOSED:
                    price_dist = abs(current - entry)
                    max_prof = config.get_float("MAX_PROFIT_PRICE", 0)
                    max_loss = config.get_float("MAX_LOSS_PRICE", 0)

                    def _notify_close(label: str, pnl: float) -> None:
                        msg = (
                            f"📕 {label} #{pos_ticket}\n"
                            f"   {direction.upper()} {symbol} "
                            f"Entry={entry} Now={current}\n"
                            f"   PnL={pnl:+.2f} | Dist={price_dist:.2f} USD"
                        )
                        logger.info("%s: ticket=%s pnl=%.2f dist=%.2f",
                                    label, pos_ticket, pnl, price_dist)
                        app.messenger.send(msg)

                    if max_prof > 0 and price_dist >= max_prof and pos_profit > 0:
                        tick_info = mt5.symbol_info_tick(symbol)
                        close_price = tick_info.bid if pos_type == 0 else tick_info.ask
                        order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
                        result_close, _ = _send_deal_order(
                            symbol, pos["volume"], order_type, pos_ticket, close_price,
                            f"take_profit_{max_prof}", logger=logger,
                        )
                        if result_close is not None:
                            _notify_close("TAKE PROFIT", pos_profit)
                            trade = trade.with_state(TradeState.CLOSED)
                        else:
                            logger.warning("TAKE PROFIT FAILED")

                    elif max_loss > 0 and price_dist >= max_loss and pos_profit < 0:
                        tick_info = mt5.symbol_info_tick(symbol)
                        close_price = tick_info.bid if pos_type == 0 else tick_info.ask
                        order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
                        result_close, _ = _send_deal_order(
                            symbol, pos["volume"], order_type, pos_ticket, close_price,
                            f"stop_loss_{max_loss}", logger=logger,
                        )
                        if result_close is not None:
                            _notify_close("STOP LOSS", pos_profit)
                            trade = trade.with_state(TradeState.CLOSED)
                        else:
                            logger.warning("STOP LOSS FAILED")

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
                                tick_info = mt5.symbol_info_tick(symbol)
                                close_price = tick_info.bid if pos_type == 0 else tick_info.ask
                                order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
                                result_close, _ = _send_deal_order(
                                    symbol, pos["volume"], order_type, pos_ticket, close_price,
                                    "M5_pulse_exit", logger=logger,
                                )
                                if result_close is not None:
                                    logger.info(
                                        "M5 pulse exit EXECUTED: impulse=%.2f >= %.2f",
                                        impulse, threshold,
                                    )
                                    app.messenger.send(
                                        f"📕 M5 PULSE EXIT #{pos_ticket}\n"
                                        f"   {direction.upper()} {symbol} "
                                        f"PnL={pos_profit:+.2f}"
                                    )
                                    trade = trade.with_state(TradeState.CLOSED)
                                else:
                                    logger.warning("M5 pulse exit FAILED")
                    except Exception:
                        pass

                # 4. M15 trailing stop (after lock-entry) — actually close position
                close_reason = None
                if trade.state in (TradeState.LOCKED, TradeState.TRAILING):
                    trail_buf = config.get_float("TRAILING_STOP_BUFFER", 0.5)
                    if m15_low is not None and pos_type == 0:
                        trigger = round(m15_low - trail_buf, 2)
                        if current <= trigger:
                            close_reason = f"M15_trail_buy({trigger})"
                        else:
                            logger.debug(
                                "M15 trail buy NOT hit: current=%.2f > trigger=%.2f "
                                "(m15_low=%.2f - buf=%.2f)",
                                current, trigger, m15_low, trail_buf,
                            )
                    elif m15_high is not None and pos_type == 1:
                        trigger = round(m15_high + trail_buf, 2)
                        if current >= trigger:
                            close_reason = f"M15_trail_sell({trigger})"
                        else:
                            logger.debug(
                                "M15 trail sell NOT hit: current=%.2f < trigger=%.2f "
                                "(m15_high=%.2f + buf=%.2f)",
                                current, trigger, m15_high, trail_buf,
                            )
                    else:
                        logger.debug(
                            "M15 trailing skip: m15_low=%s m15_high=%s pos_type=%s",
                            m15_low, m15_high, pos_type,
                        )
                else:
                    logger.debug(
                        "M15 trailing skip: trade.state=%s (need LOCKED/TRAILING)",
                        trade.state.value,
                    )

                if close_reason:
                    tick_info = mt5.symbol_info_tick(symbol)
                    close_price = tick_info.bid if pos_type == 0 else tick_info.ask
                    order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
                    result_close, _ = _send_deal_order(
                        symbol, pos["volume"], order_type, pos_ticket, close_price,
                        close_reason, logger=logger,
                    )
                    if result_close is not None:
                        logger.info("M15 trailing stop EXECUTED: %s", close_reason)
                        app.messenger.send(
                            f"📕 M15 TRAIL #{pos_ticket}\n"
                            f"   {direction.upper()} {symbol} "
                            f"PnL={pos_profit:+.2f}"
                        )
                        trade = trade.with_state(TradeState.CLOSED)
                    else:
                        logger.warning("M15 trailing stop FAILED")

                # 4.5 Per-trade profit/loss limits (enforced at runtime)
                if trade.state not in (TradeState.CLOSED, TradeState.CLOSING):
                    block_reason = risk.per_trade_blocked(trade)
                    if block_reason:
                        tick_info = mt5.symbol_info_tick(symbol)
                        close_price = tick_info.bid if pos_type == 0 else tick_info.ask
                        order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
                        result_close, _ = _send_deal_order(
                            symbol, pos["volume"], order_type, pos_ticket, close_price,
                            block_reason, logger=logger,
                        )
                        if result_close is not None:
                            logger.info(
                                "Per-trade limit EXECUTED: %s profit=%.2f",
                                block_reason, pos_profit,
                            )
                            app.messenger.send(
                                f"📕 PER-TRADE LIMIT #{pos_ticket}\n"
                                f"   {direction.upper()} {symbol} "
                                f"PnL={pos_profit:+.2f} | Motivo: {block_reason}"
                            )
                            trade = trade.with_state(TradeState.CLOSED)
                        else:
                            logger.warning("Per-trade limit FAILED: %s", block_reason)

                # 5. Max-profit trail (after lock-entry)
                retention_pct = config.get_float("MAX_PROFIT_RETENTION_PCT", 50)
                if trade.lock_entry_achieved and trade.state != TradeState.CLOSED and retention_pct > 0:
                    trade = trade.with_prices(current, pos_profit)
                    if trade.should_trail_close(retention_pct):
                        tick_info = mt5.symbol_info_tick(symbol)
                        close_price = tick_info.bid if pos_type == 0 else tick_info.ask
                        order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
                        result_close, _ = _send_deal_order(
                            symbol, pos["volume"], order_type, pos_ticket, close_price,
                            "max_profit_trail", logger=logger,
                        )
                        if result_close is not None:
                            logger.info(
                                "Max-profit trail EXECUTED: "
                                "profit=%.2f peak=%.2f",
                                pos_profit, trade.max_profit_seen,
                            )
                            app.messenger.send(
                                f"📕 MAX-PROFIT TRAIL #{pos_ticket}\n"
                                f"   {direction.upper()} {symbol} "
                                f"PnL={pos_profit:+.2f} Peak={trade.max_profit_seen:+.2f}"
                            )
                            trade = trade.with_state(TradeState.CLOSED)
                        else:
                            logger.warning("Max-profit trail FAILED")

                session = session.with_open_trade(trade)

            else:
                # Position was closed — detect how and notify
                prev_trade = session.open_trade
                if prev_trade is not None:
                    if prev_trade.state == TradeState.CLOSED:
                        reason = "Fechado pelo sistema"
                    else:
                        reason = "Fechado pelo MT5 (SL/TP/Manual)"
                    msg = (
                        f"📕 Trade #{prev_trade.ticket} FECHADO\n"
                        f"   {prev_trade.direction.upper()} {prev_trade.symbol} "
                        f"Entry={prev_trade.entry_price} "
                        f"PnL={prev_trade.profit:+.2f}\n"
                        f"   Motivo: {reason}"
                    )
                    logger.info("Trade closed: ticket=%s pnl=%.2f reason=%s",
                                prev_trade.ticket, prev_trade.profit, reason)
                    app.messenger.send(msg)
                session = session.with_open_trade(None)
                app.trade_history = []

            # ── block-close ─────────────────────────────────
            # Close any remaining position when daily limits are breached.
            # (New positions are caught above; this handles positions that were
            #  already open when the limit was hit.)
            if has_position and session.is_any_block_active():
                block_names = session.all_blocks_active()
                pos = positions[0]
                reason = ("block_" + block_names[0].lower().replace(" ", "_")
                          if block_names else "block_active")
                logger.warning(
                    "BLOCK CLOSE: closing position #%s — blocks: %s",
                    pos["ticket"], ", ".join(block_names),
                )
                closed_ok = _close_position(
                    broker, symbol, pos, reason, logger, app, label="BLOQUEIO",
                )
                if closed_ok:
                    session = session.with_open_trade(None)
                    app.trade_history = []
                    has_position = False

            app.session = session
            try:
                persistence.save(session.to_dict())
            except Exception:
                logger.warning("Failed to save session state", exc_info=True)

            # ── HEARTBEAT ────────────────────────────────────
            # Update heartbeat so external watchdog knows the loop is alive
            last_heartbeat = time.monotonic()
            app.last_loop_heartbeat = last_heartbeat

            time.sleep(1)
        except Exception:
            logger.error("Update loop iteration FAILED", exc_info=True)
            consecutive_failures += 1
            # Update heartbeat even on failure so we know the loop itself isn't dead
            last_heartbeat = time.monotonic()
            app.last_loop_heartbeat = last_heartbeat
            # Exponential backoff on repeated failures
            backoff = min(1.0 * (2 ** min(consecutive_failures, 5)), 30)
            logger.warning("Backing off for %.1fs after %d consecutive failures",
                           backoff, consecutive_failures)
            time.sleep(backoff)

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
        cd = MarketAnalyzer.countdown_hhmm(app.broker.get_server_time())
        messenger.notify_blocks(session.all_blocks_active(), release_countdown=cd)

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
