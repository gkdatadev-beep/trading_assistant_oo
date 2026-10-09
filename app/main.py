"""Trade Assistant — Flask Application (OO Architecture)."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask
import MetaTrader5 as mt5

from app.config import RuntimeConfig
from app.core.auto_signal import AutoSignalEngine, compute_volume
from app.core.broker import MT5Broker
from app.core.channel_alert import ChannelBreakoutMonitor
from app.core.fractals import last_swing_point, normalize_candles
from app.core.ledger import CHALLENGE_START, TradeLedger
from app.core.market import MarketAnalyzer
from app.core.messenger import Messenger
from app.core.persistence import StatePersistence
from app.core.prob import ProbeProbabilityEngine
from app.core.risk import RiskManager, RiskLimits
from app.core.session import TradingSession

# Janela da base estatística do Profit Probe (dias)
HISTORY_DAYS = 500


def _parse_dt(value):
    """Converte string ISO em datetime (None se vazio/inválido)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _configure_logging():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    for name in ("app.core.loop", "app.web", "app.core.messenger",
                 "app.core.signals"):
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
        terminal_path=config.get_str("MT5_PATH") or None,
        attach_only=config.get_str("MT5_ATTACH_ONLY", "1") != "0",
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

    # ── Probabilistic signal engine (Profit Probe) ──────────
    # Usa o histórico M15 cacheado (data/m15_500d.csv) para estimar
    # P(subir/descer) e P(toque de ±X pts) por vizinhos históricos.
    prob_engine = ProbeProbabilityEngine(data_dir=Path(config.config_path).parent)

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
            flag_overtrading=bool((saved.get("flags") or {}).get("overtrading")),
            daily_profit=float(saved.get("daily_profit", 0) or 0),
            max_daily_profit=float(saved.get("max_daily_profit", 0) or 0),
            current_drawdown=float(saved.get("current_drawdown", 0) or 0),
            operations_count=int(saved.get("operations_count", 0) or 0),
            operations_closed=int(saved.get("operations_closed", 0) or 0),
            overtrading_triggered=bool(saved.get("overtrading_triggered")),
            overtrading_reason=str(saved.get("overtrading_reason") or ""),
            overtrading_protection_end=_parse_dt(
                saved.get("overtrading_protection_end")),
            overtrading_streak_seen=int(
                saved.get("overtrading_streak_seen", 0) or 0),
            consecutive_losses=int(
                saved.get("consecutive_losses", 0) or 0),
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
    app.prob_engine = prob_engine
    app._probe_signal_cache = (0.0, None)
    app._swing_cache = (0.0, None)  # fractais H1/M15/M5 (cache 60s)
    app.signal_at_open = {}   # ticket → sinal probabilístico da abertura
    # ── camada de sinais do canal 1h (auto trade) ──────────
    app._auto_plan = {}       # ticket → {sl, tp, volume} planejados pelo sinal
    app.trade_origin = _load_trade_origins(config)  # ticket → AUTO|MANUAL
    app.auto_signal_engine = None   # construído no __main__ (usa o broker)
    app.auto_signal_state = {}
    # Fechamento forçado por bloqueio (retry controlado)
    app._force_close_ticket = None
    app._force_close_notified = False
    app._force_close_last_try = 0.0
    # Detecção do botão Algo Trading (AutoTrading) do terminal
    app._autotrading_check_last = 0.0
    app._autotrading_warn_last = 0.0
    app._last_close_error = ""

    # ── Base estatística: atualizar se o app ficou fora na virada do dia ──
    if broker.connected:
        try:
            import pandas as pd
            prob_csv = Path(config.config_path).parent / "m15_500d.csv"
            stale = True
            if prob_csv.exists():
                try:
                    last = pd.read_csv(prob_csv, usecols=["time"]).iloc[-1, 0]
                    stale = str(last)[:10] < now.strftime("%Y-%m-%d")
                except Exception:
                    stale = True
            if stale:
                base_ok, base_info = _refresh_prob_base(
                    app, broker, config, logger)
                if base_ok:
                    messenger.notify_new_day(
                        config.to_dict(), base_info, now.strftime("%Y-%m-%d"))
                else:
                    logger.warning(
                        "Prob base refresh at startup failed: %s", base_info)
        except Exception:
            logger.warning("Prob base startup check failed", exc_info=True)

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
                # ── sinal probabilístico (cache 60s) ──────────
                signal = None
                try:
                    cache_time, cache_val = app._probe_signal_cache
                    if cache_val is None or time.monotonic() - cache_time > 60:
                        sig = None
                        try:
                            symbol = config.get_str("SYMBOL", "XAUUSD")
                            now_srv = b.get_server_time()
                            rates = b.get_rates(count=96)
                            candles = []
                            if rates is not None:
                                for t, r in rates.iterrows():
                                    if t.date() == now_srv.date():
                                        candles.append({
                                            "tod": t.hour * 60 + t.minute,
                                            "high": float(r["high"]),
                                            "low": float(r["low"]),
                                            "close": float(r["close"]),
                                        })
                            sig = app.prob_engine.compute(
                                now_srv, candles,
                                x=config.get_float("MAX_PROFIT_PRICE", 10),
                                y=config.get_float("MAX_LOSS_PRICE", 7),
                                cost=_custo_por_operacao(symbol),
                            )
                            if sig and sig.get("ok"):
                                lot = config.get_float("LOT_SIZE", 0.2)
                                contract = _contract_size(app, symbol)
                                sig["lot"] = lot
                                sig["ev_usd"] = round(
                                    sig["ev"] * contract * lot, 2)
                        except Exception:
                            logger.warning("Probe signal failed", exc_info=True)
                            sig = None
                        app._probe_signal_cache = (time.monotonic(), sig)
                    signal = app._probe_signal_cache[1]
                except Exception:
                    logger.warning("Probe signal cache failed", exc_info=True)

                # ── fractais Bill Williams H1/M15/M5 (cache 60s) ──
                swings = {}
                try:
                    cache_time, cache_val = app._swing_cache
                    if cache_val is None or time.monotonic() - cache_time > 60:
                        val = {}
                        wing = normalize_candles(
                            config.get_str("SWING_CANDLES", "5"))
                        import MetaTrader5 as _mt5
                        tf_map = (
                            ("H1", _mt5.TIMEFRAME_H1, 300),
                            ("M15", _mt5.TIMEFRAME_M15, 400),
                            ("M5", _mt5.TIMEFRAME_M5, 500),
                        )
                        sym = config.get_str("SYMBOL", "XAUUSD")
                        for tf_name, tf_const, count in tf_map:
                            try:
                                df = b.get_rates(
                                    symbol=sym, timeframe=tf_const, count=count)
                                if df is not None and len(df) > wing:
                                    pt = last_swing_point(df, candles=wing)
                                    val[tf_name] = {
                                        "swing": (
                                            {"kind": pt["kind"],
                                             "price": pt["price"],
                                             "time": pt["time"]}
                                            if pt else None
                                        ),
                                    }
                            except Exception:
                                logger.debug(
                                    "Swing fetch failed for %s", tf_name,
                                    exc_info=True)
                        val["_candles"] = wing
                        app._swing_cache = (time.monotonic(), val)
                    swings = app._swing_cache[1]
                except Exception:
                    logger.warning("Swing cache failed", exc_info=True)

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
                    "signal": signal,
                    "swings": swings,
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
                     sl: float = 0.0, tp: float = 0.0,
                     logger=None) -> tuple:
    """Send a standardized TRADE_ACTION_DEAL with all required fields for non-demo brokers.

    Includes type_filling=ORDER_FILLING_IOC and type_time=ORDER_TIME_GTC
    which are REQUIRED by CTI/prop-firm servers but optional on MetaQuotes demo.

    Optional sl/tp (price units) are attached to the deal request so the
    position opens already protected (usado pela abertura automática).

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
    if sl and sl > 0:
        request["sl"] = sl
    if tp and tp > 0:
        request["tp"] = tp

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
        # Retorna None para os chamadores tratarem como FALHA
        # (antes retornava o objeto result → "posicao fechada" falso).
        return None, msg

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
        app._last_close_error = "tick indisponível"
        return False

    order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
    result, err = _send_deal_order(
        symbol, pos["volume"], order_type, pos["ticket"], close_price,
        reason, logger=logger,
    )
    if result is not None:
        logger.info("Position %s closed: %s", pos["ticket"], reason)
        app._last_close_error = ""
        app.messenger.send(
            f"📕 {label} — posicao fechada\n"
            f"   Ticket #{pos['ticket']} | Motivo: {reason}"
        )
        return True
    else:
        logger.error("Close position %s FAILED: %s", pos["ticket"], err)
        app._last_close_error = str(err or "erro desconhecido")
        return False


def _notify_block_close_failed(app: Flask, pos: dict, logger) -> None:
    """Avisa uma única vez (Telegram) que o fechamento por bloqueio falhou."""
    if getattr(app, "_force_close_notified", False):
        return
    app._force_close_notified = True
    err = getattr(app, "_last_close_error", "") or "MT5 recusou a ordem"
    logger.error(
        "BLOCK CLOSE FAILED ticket=%s (%s) — nova tentativa automatica em ~30s",
        pos["ticket"], err,
    )
    hint = ""
    if "10027" in err:
        hint = ("\n   Erro 10027 = Algo Trading DESLIGADO:\n"
                "   ative o botão \"Algo Trading\" no terminal MT5.")
    app.messenger.send(
        f"⚠️ FALHA AO FECHAR #{pos['ticket']}\n"
        f"   Motivo: {err}\n"
        f"   Bloqueio ativo — novas tentativas a cada ~30s{hint}"
    )


def _refresh_prob_base(app: Flask, broker: MT5Broker, config: RuntimeConfig,
                       logger) -> tuple:
    """Re-baixa os últimos HISTORY_DAYS de M15 e recarrega o engine.

    Returns (ok: bool, info: str).
    """
    try:
        if not broker.connected:
            return False, "MT5 indisponível"
        from datetime import timedelta
        now_srv = broker.get_server_time()
        df = broker.get_rates_range(
            now_srv - timedelta(days=HISTORY_DAYS + 2), now_srv,
        )
        if df is None or len(df) < 100:
            return False, "sem dados suficientes do MT5"
        out = Path(config.config_path).parent / "m15_500d.csv"
        df.reset_index()[["time", "open", "high", "low", "close",
                          "tick_volume"]].to_csv(out, index=False)
        app.prob_engine = ProbeProbabilityEngine(history_csv=out)
        app._probe_signal_cache = (0.0, None)
        n_days = int(df.index.normalize().nunique())
        logger.info("Prob base refreshed: %s (%d days)", out, n_days)
        return True, f"últimos {HISTORY_DAYS} dias ({n_days} dias úteis)"
    except Exception:
        logger.warning("Prob base refresh failed", exc_info=True)
        return False, "erro na atualização"


def _contract_size(app: Flask, symbol: str) -> float:
    """Tamanho do contrato (oz por lote) — consultado uma vez no MT5."""
    cached = getattr(app, "_contract_size", None)
    if cached:
        return cached
    try:
        info = mt5.symbol_info(symbol)
        val = float(info.trade_contract_size) if info else 100.0
    except Exception:
        val = 100.0
    app._contract_size = val
    return val


def _custo_por_operacao(symbol: str) -> float:
    """Custo por trade em USD/oz: spread ao vivo + buffer de slippage."""
    try:
        info = mt5.symbol_info(symbol)
        spread_pts = float(info.spread) if info else 43
        point = float(info.point) if info else 0.01
        spread_usd_oz = spread_pts * point
    except Exception:
        spread_usd_oz = 0.43
    return round(spread_usd_oz + 0.25, 4)   # +0,25 USD/oz de slippage/comissão


# ── origem das operações (manual × automática) ──────────────────────────

def _trade_origins_path(config: RuntimeConfig) -> Path:
    return Path(config.config_path).parent / "trade_origins.json"


def _load_trade_origins(config: RuntimeConfig) -> dict:
    """Carrega {ticket: 'AUTO'|'MANUAL'} do disco (registro persistente)."""
    try:
        path = _trade_origins_path(config)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {int(k): str(v) for k, v in data.items()}
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to load trade origins", exc_info=True)
    return {}


def _save_trade_origins(app: Flask) -> None:
    try:
        path = _trade_origins_path(app.config_obj)
        payload = {
            str(k): v for k, v in getattr(app, "trade_origin", {}).items()
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to save trade origins", exc_info=True)


# ── camada de sinais do canal 1h ─────────────────────────────────────────

def _build_signal_engine(app: Flask, symbol: str):
    """Monta o motor de sinais do canal da 1ª hora → tela + Telegram."""
    broker: MT5Broker = app.broker
    config: RuntimeConfig = app.config_obj

    def get_rates_today():
        try:
            return broker.get_rates(symbol, mt5.TIMEFRAME_M5, count=400)
        except Exception:
            return None

    def get_server_time():
        return broker.get_server_time()

    def get_price():
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None and getattr(tick, "bid", 0):
                return float(tick.bid)
        except Exception:
            pass
        return None

    def on_signal(sig: dict):
        _handle_signal(app, sig, logging.getLogger("app.core.signals"))

    return AutoSignalEngine(
        symbol=symbol,
        get_rates_today=get_rates_today,
        get_server_time=get_server_time,
        on_signal=on_signal,
        get_price=get_price,
        entry_buffer=config.get_float("SIGNAL_ENTRY_BUFFER_USD", 0.5),
        tp_channel=config.get_int("SIGNAL_TP_CHANNEL", 2),
        sl_channel=config.get_int("SIGNAL_SL_CHANNEL", 1),
        end_hour=config.get_int("SIGNAL_END_HOUR", 12),
    )


def _auto_volume(app: Flask, sig: dict) -> float:
    """Lote do sinal — risco fixo dividido pela distância do stop."""
    config: RuntimeConfig = app.config_obj
    symbol = config.get_str("SYMBOL", "XAUUSD")
    stop_dist = float(sig.get("stop_distance") or 0)
    risk_usd = config.get_float("AUTO_RISK_USD", 50)
    contract = _contract_size(app, symbol)
    lot_cap = config.get_float("LOT_SIZE", 0.15)
    try:
        info = mt5.symbol_info(symbol)
        vmin = float(getattr(info, "volume_min", 0) or 0.01)
        vmax = float(getattr(info, "volume_max", 0) or 1000.0)
        vstep = float(getattr(info, "volume_step", 0) or 0.01)
    except Exception:
        vmin, vmax, vstep = 0.01, 1000.0, 0.01
    return compute_volume(
        stop_dist, contract, risk_usd, vmin, vmax, vstep, lot_cap)


def _handle_signal(app: Flask, sig: dict, logger) -> None:
    """Sinal disparado: SEMPRE notifica no Telegram e no log; abre posição
    somente se o auto-trade estiver ligado e não houver impedimentos."""
    config: RuntimeConfig = app.config_obj
    symbol = config.get_str("SYMBOL", "XAUUSD")
    enabled = config.get_str("AUTO_TRADE_ENABLED", "0") == "1"
    arrow = "📈 COMPRA" if sig["direction"] == "buy" else "📉 VENDA"
    lines = [
        f"🎯 SINAL DO CANAL 1H — {arrow} {symbol}",
        "",
        f"⏰ {sig['time']} (servidor) | {sig['day']}",
        f"💰 Preço: {sig['price']:.2f} (gatilho {sig['trigger']:.2f})",
        f"🎯 TP: {sig['tp']:.2f} ({sig['tp_label']})",
        f"🛑 SL: {sig['sl']:.2f} ({sig['sl_label']})",
        f"📐 Distância do stop: {sig['stop_distance']:.2f} USD",
    ]
    try:
        vol = _auto_volume(app, sig)
        risk_usd = config.get_float("AUTO_RISK_USD", 50)
        lines.append(
            f"⚖️ Lote pelo risco: {vol:g} (risco {risk_usd:.2f} USD no stop)")
    except Exception:
        logger.debug("Volume estimado indisponível", exc_info=True)
    lines.append(f"🤖 Auto-trade: {'🟢 LIGADO' if enabled else '⚪ DESLIGADO'}")
    app.messenger.send("\n".join(lines))
    # ── gráfico do setup atingido (Telegram, sempre que houver sinal) ──
    try:
        _send_signal_chart(app, sig, logger)
    except Exception:
        logger.warning("SIGNAL CHART FAILED", exc_info=True)
    logger.info(
        "SIGNAL %s price=%.2f trigger=%.2f tp=%.2f sl=%.2f auto=%s",
        sig["direction"], sig["price"], sig["trigger"],
        sig["tp"], sig["sl"], enabled,
    )
    app.auto_signal_state = (
        app.auto_signal_engine.status() if app.auto_signal_engine else {}
    )

    if not enabled:
        logger.info("AUTO TRADE OFF — sinal apenas informativo")
        return

    # Impedimentos para abertura automática
    try:
        if broker_positions := app.broker.positions_total():
            app.messenger.send(
                "🤖 Auto-trade: posição já aberta — sinal NÃO executado")
            logger.info("AUTO OPEN SKIPPED: posição aberta (%d)", broker_positions)
            return
    except Exception:
        pass
    if app.session.is_any_block_active():
        blocks = ", ".join(app.session.all_blocks_active())
        app.messenger.send(
            f"🤖 Auto-trade: bloqueios ativos — sinal NÃO executado ({blocks})")
        logger.info("AUTO OPEN SKIPPED: bloqueios ativos (%s)", blocks)
        return

    _auto_open_position(app, sig, logger)


def _send_signal_chart(app: Flask, sig: dict, logger) -> None:
    """Gera e envia no Telegram o gráfico do setup atingido pelo sinal."""
    from app.core.auto_signal import render_signal_chart
    import pandas as pd

    symbol = app.config_obj.get_str("SYMBOL", "XAUUSD")
    df = None
    try:
        df = app.broker.get_rates(symbol, mt5.TIMEFRAME_M5, count=400)
    except Exception:
        logger.debug("Signal chart: rates unavailable", exc_info=True)
    if df is None or len(df) == 0:
        logger.warning("SIGNAL CHART SKIPPED: sem candles M5")
        return

    try:
        now = app.broker.get_server_time()
    except Exception:
        now = datetime.now(timezone.utc)
    try:
        normalized = df.index.normalize()
        day_ts = (
            pd.Timestamp(now.date(), tz=normalized.tz)
            if normalized.tz is not None else pd.Timestamp(now.date())
        )
        df = df[normalized == day_ts]
    except Exception:
        logger.debug("Signal chart: day filter skipped", exc_info=True)

    png = render_signal_chart(df, sig, symbol)
    if not png:
        logger.warning("SIGNAL CHART SKIPPED: dados insuficientes")
        return

    path = Path(app.config_obj.config_path).parent / "_signal_chart.png"
    try:
        path.write_bytes(png)
    except Exception:
        logger.warning("SIGNAL CHART: falha ao gravar PNG", exc_info=True)
        return

    caption = (
        f"🎯 {sig['direction'].upper()} {symbol} @ {sig['price']:.2f} | "
        f"TP {sig['tp']:.2f} | SL {sig['sl']:.2f}"
    )
    ok = app.messenger.send_photo(str(path), caption=caption)
    logger.info("SIGNAL CHART sent: %s (ok=%s)", path.name, ok)


def _auto_open_position(app: Flask, sig: dict, logger) -> bool:
    """Abre a posição do sinal com lote calculado pelo risco do stop."""
    config: RuntimeConfig = app.config_obj
    symbol = config.get_str("SYMBOL", "XAUUSD")
    direction = sig["direction"]
    stop_dist = float(sig.get("stop_distance") or 0)
    risk_usd = config.get_float("AUTO_RISK_USD", 50)

    volume = _auto_volume(app, sig)
    if volume <= 0:
        logger.error(
            "AUTO OPEN FAILED: volume inválido (stop=%.2f risk=%.2f)",
            stop_dist, risk_usd)
        app.messenger.send(
            f"⚠️ AUTO OPEN FALHOU — volume inválido\n"
            f"   stop={stop_dist:.2f} USD | risco={risk_usd:.2f} USD")
        return False

    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError("tick indisponível")
        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        price = float(tick.ask) if direction == "buy" else float(tick.bid)
    except Exception as e:
        logger.error("AUTO OPEN FAILED: tick error (%s)", e)
        return False

    result, err = _send_deal_order(
        symbol, volume, order_type, 0, price,
        "auto_channel_signal",
        sl=float(sig["sl"]), tp=float(sig["tp"]), logger=logger,
    )
    if result is None:
        logger.error("AUTO OPEN FAILED: %s", err)
        app.messenger.send(
            f"⚠️ AUTO OPEN FALHOU\n"
            f"   {err}\n"
            f"   Sinal: {direction.upper()} {symbol} @ {price:.2f} "
            f"vol={volume:g}")
        return False

    # Identifica o ticket da posição criada (via comment / ordem)
    ticket = int(getattr(result, "order", 0) or 0)
    try:
        for p in app.broker.get_positions():
            if str(p.get("comment", "")).startswith("auto_channel_signal"):
                ticket = int(p["ticket"])
                break
        else:
            if not ticket:
                raise RuntimeError("ticket não identificado")
    except Exception:
        logger.warning(
            "AUTO OPEN: ticket via comment indisponível — usando result.order=%s",
            ticket)

    app._auto_plan[int(ticket)] = {
        "sl": float(sig["sl"]),
        "tp": float(sig["tp"]),
        "volume": volume,
    }
    app.trade_origin[int(ticket)] = "AUTO"
    _save_trade_origins(app)
    logger.info(
        "AUTO TRADE OPENED ticket=%s dir=%s volume=%.2f entry=%.2f "
        "sl=%.2f tp=%.2f risk=%.2f stop=%.2f",
        ticket, direction, volume, price, sig["sl"], sig["tp"],
        risk_usd, stop_dist,
    )
    app.messenger.send(
        f"🤖 ABERTURA AUTOMÁTICA — {direction.upper()} {symbol}\n"
        f"   Ticket #{ticket} | Entrada {price:.2f}\n"
        f"   Volume {volume:g} (risco {risk_usd:.2f} USD no stop de "
        f"{stop_dist:.2f} USD)\n"
        f"   🎯 TP {sig['tp']:.2f} | 🛑 SL {sig['sl']:.2f}")
    return True


def _build_channel_monitor(app: Flask, symbol: str):
    """Monta o monitor de rompimento dos canais da 1ª hora → Telegram."""
    from datetime import timedelta

    broker: MT5Broker = app.broker
    cooldown_min = max(app.config_obj.get_float("CHANNEL_ALERT_COOLDOWN_MIN", 5), 0.1)

    def get_rates_today():
        try:
            now = broker.get_server_time()
        except Exception:
            return None
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        return broker.get_rates_range(
            start, start + timedelta(days=1),
            symbol=symbol, timeframe=mt5.TIMEFRAME_M5,
        )

    def get_server_time():
        return broker.get_server_time()

    def get_price():
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None and getattr(tick, "bid", 0):
                return float(tick.bid)
        except Exception:
            pass
        return None

    def send_alert(text: str) -> bool:
        return app.messenger.send(text)

    return ChannelBreakoutMonitor(
        symbol=symbol,
        get_rates_today=get_rates_today,
        get_server_time=get_server_time,
        send_alert=send_alert,
        cooldown_seconds=cooldown_min * 60.0,
        get_price=get_price,
    )


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

            # ── AutoTrading (botão Algo Trading do terminal) ──
            # Com o botão OFF o MT5 rejeita TODAS as ordens (retcode 10027):
            # o agente não consegue fechar nem abrir posições.
            try:
                now_mono = time.monotonic()
                if now_mono - getattr(app, "_autotrading_check_last", 0.0) >= 30:
                    app._autotrading_check_last = now_mono
                    ti = mt5.terminal_info()
                    trade_allowed = (
                        bool(getattr(ti, "trade_allowed", True))
                        if ti is not None else True
                    )
                    if not trade_allowed and (
                        now_mono - getattr(app, "_autotrading_warn_last", 0.0) >= 3600
                    ):
                        app._autotrading_warn_last = now_mono
                        logger.error(
                            "AutoTrading DESLIGADO no terminal — ordens serão "
                            "rejeitadas (retcode 10027). Ative o botão Algo "
                            "Trading."
                        )
                        app.messenger.send(
                            "⚠️ ALGO TRADING DESLIGADO NO MT5\n"
                            "   O botão Algo Trading está OFF — o agente não\n"
                            "   consegue fechar nem abrir posições.\n"
                            "   Ative o botão no terminal para liberar o agente."
                        )
            except Exception:
                logger.debug("AutoTrading check failed", exc_info=True)

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
                # ── virada do dia: atualizar a base estatística e avisar ──
                base_ok, base_info = _refresh_prob_base(
                    app, broker, config, logger)
                app.messenger.notify_new_day(
                    config.to_dict(), base_info if base_ok else "",
                    session.day_key,
                )
                if not base_ok:
                    logger.warning(
                        "Prob base refresh at rollover: %s", base_info)

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

            # posição marcada para fechamento forçado sumiu → limpa a marcação
            if not has_position:
                app._force_close_ticket = None
                app._force_close_notified = False

            # Fetch today's closed trades from MT5 history
            try:
                closed_trades = broker.get_closed_trades_today()
                account = config.get_str("MT5_LOGIN", "") if config else ""
                for ct in closed_trades:
                    ct["account"] = account
                    ct["origin"] = app.trade_origin.get(
                        int(ct.get("ticket") or 0), "MANUAL")
                    sig = app.signal_at_open.get(int(ct.get("ticket") or 0))
                    if sig:
                        ct["signal"] = sig
                        ct["signal_bias"] = sig.get("bias")
                        ct["signal_p_tp"] = sig.get("p_tp")
                        ct["signal_p_sl"] = sig.get("p_sl")
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
                                t["origin"] = app.trade_origin.get(
                                    int(t.get("ticket") or 0), "MANUAL")
                                sig = app.signal_at_open.get(int(t.get("ticket") or 0))
                                if sig:
                                    t["signal"] = dict(sig)
                                    t["signal_bias"] = sig.get("bias")
                                    t["signal_p_tp"] = sig.get("p_tp")
                                    t["signal_p_sl"] = sig.get("p_sl")
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
                    app.messenger.send(
                        "✅ OVERTRADING LIBERADO\n"
                        "   O tempo da proteção expirou — trades desbloqueados."
                    )

            # ── overtrading trigger: 2 perdas consecutivas ──
            # Conta as perdas consecutivas mais recentes do dia (pelo
            # histórico do MT5 — sobrevive a reinícios) e trava os trades
            # pelo tempo definido em OVERTRADING_PROTECTION_TIME.
            streak = risk.count_consecutive_losses(closed_trades)
            if streak and streak != session.consecutive_losses:
                logger.info("Consecutive losses streak: %d", streak)
            session = session.with_metrics(consecutive_losses=streak)
            if risk.should_trigger_overtrading(
                streak, session.overtrading_streak_seen, session.flag_overtrading,
            ):
                minutes = max(
                    config.get_int("OVERTRADING_PROTECTION_TIME", 12), 1,
                )
                protection_end = now + timedelta(minutes=minutes)
                session = session.with_overtrading(
                    active=True, triggered=True,
                    reason=f"{streak} perdas consecutivas",
                    protection_end=protection_end,
                    streak_seen=streak,
                )
                logger.warning(
                    "OVERTRADING LOCK: %d perdas consecutivas — trades "
                    "travados por %d min (libera %s)",
                    streak, minutes, protection_end.strftime("%H:%M:%S"),
                )
                app.messenger.send(
                    f"🚫 OVERTRADING — {streak} PERDAS CONSECUTIVAS\n"
                    f"   Trades travados por {minutes} min\n"
                    f"   Libera em {protection_end.strftime('%H:%M:%S')} "
                    f"(servidor)\n"
                    f"   Novas entradas serão bloqueadas até lá."
                )
            elif streak == 0 and session.overtrading_streak_seen:
                # Streak quebrado (houve lucro) — zera a marcação para que
                # uma nova sequência de 2 perdas trave de novo.
                session = session.with_overtrading(streak_seen=0)

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
                now_mono = time.monotonic()
                last_try = getattr(app, "_force_close_last_try", 0.0)
                if now_mono - last_try >= 30:
                    app._force_close_last_try = now_mono
                    logger.warning(
                        "BLOCK ENFORCE: closing new position #%s — blocks active: %s",
                        pos["ticket"], ", ".join(block_names),
                    )
                    closed_ok = _close_position(
                        broker, symbol, pos, reason, logger, app, label="BLOQUEIO")
                    if closed_ok:
                        app._force_close_ticket = None
                        app._force_close_notified = False
                        has_position = False
                        session = session.with_open_trade(None)
                    else:
                        app._force_close_ticket = pos["ticket"]
                        _notify_block_close_failed(app, pos, logger)

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

                # ── registrar o sinal probabilístico na abertura ──
                # Usa o sinal calculado pelo probe (cache ≤60s) para comparar
                # depois a indicação com o resultado real do trade.
                if prev is None or (getattr(prev, "ticket", 0) or 0) != pos_ticket:
                    try:
                        from dataclasses import replace as _replace
                        _, cache_val = app._probe_signal_cache
                        if isinstance(cache_val, dict) and cache_val.get("ok"):
                            trade = _replace(trade, signal=dict(cache_val))
                            app.signal_at_open[int(pos_ticket)] = dict(cache_val)
                    except Exception:
                        logger.debug(
                            "Signal capture at trade open failed", exc_info=True)

                # ── registro da origem da operação (manual × automática) ──
                if int(pos_ticket) not in app.trade_origin:
                    app.trade_origin[int(pos_ticket)] = "MANUAL"
                    _save_trade_origins(app)
                    logger.info(
                        "TRADE OPENED account=%s ticket=%s dir=%s volume=%s "
                        "origin=MANUAL",
                        config.get_str("MT5_LOGIN", ""), pos_ticket, direction,
                        pos["volume"],
                    )
                else:
                    logger.info(
                        "TRADE OPENED account=%s ticket=%s dir=%s volume=%s "
                        "origin=%s",
                        config.get_str("MT5_LOGIN", ""), pos_ticket, direction,
                        pos["volume"], app.trade_origin[int(pos_ticket)],
                    )

                # 1. Set initial SL/TP if not defined
                #    Posições abertas pelo sinal automático usam o plano do
                #    canal 1h (TP no canal par, SL no canal ímpar); as demais
                #    seguem o padrão ATR (SL_MULTIPLIER / TP_MULTIPLIER).
                new_sl = new_tp = None
                if pos_sl == 0.0:
                    plan = app._auto_plan.get(int(pos_ticket))
                    if plan and plan.get("sl"):
                        new_sl = float(plan["sl"])
                        new_tp = float(plan["tp"])
                    elif last_atr > 0:
                        sl_mult = config.get_float("SL_MULTIPLIER", 2)
                        tp_mult = config.get_float("TP_MULTIPLIER", 4)
                        if pos_type == 0:
                            new_sl = round(entry - sl_mult * last_atr, 2)
                            new_tp = round(entry + tp_mult * last_atr, 2)
                        else:
                            new_sl = round(entry + sl_mult * last_atr, 2)
                            new_tp = round(entry - tp_mult * last_atr, 2)
                if new_sl is not None:
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
            # Fecha qualquer posição quando um bloqueio está ativo (ex.: fora
            # da janela de trading). Retenta a cada ~30s enquanto falhar.
            if has_position and session.is_any_block_active():
                block_names = session.all_blocks_active()
                pos = positions[0]
                reason = ("block_" + block_names[0].lower().replace(" ", "_")
                          if block_names else "block_active")
                now_mono = time.monotonic()
                last_try = getattr(app, "_force_close_last_try", 0.0)
                if now_mono - last_try >= 30:
                    app._force_close_last_try = now_mono
                    logger.warning(
                        "BLOCK CLOSE: closing position #%s — blocks: %s",
                        pos["ticket"], ", ".join(block_names),
                    )
                    closed_ok = _close_position(
                        broker, symbol, pos, reason, logger, app, label="BLOQUEIO",
                    )
                    if closed_ok:
                        app._force_close_ticket = None
                        app._force_close_notified = False
                        session = session.with_open_trade(None)
                        app.trade_history = []
                        has_position = False
                    else:
                        app._force_close_ticket = pos["ticket"]
                        _notify_block_close_failed(app, pos, logger)

            # ── force-close retry ────────────────────────────
            # Posição marcada para fechamento ainda aberta (ordem falhou —
            # ex.: mercado fechado ou AutoTrading off). Continua tentando
            # mesmo que o bloqueio atual já tenha sido liberado.
            if (has_position and positions
                    and getattr(app, "_force_close_ticket", None)
                    == positions[0]["ticket"]):
                pos = positions[0]
                now_mono = time.monotonic()
                last_try = getattr(app, "_force_close_last_try", 0.0)
                if now_mono - last_try >= 30:
                    app._force_close_last_try = now_mono
                    closed_ok = _close_position(
                        broker, symbol, pos, "block_force_close_retry",
                        logger, app, label="BLOQUEIO",
                    )
                    if closed_ok:
                        app._force_close_ticket = None
                        app._force_close_notified = False
                        session = session.with_open_trade(None)
                        app.trade_history = []
                        has_position = False
                    else:
                        _notify_block_close_failed(app, pos, logger)

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

    # ── Canal da 1ª hora: alertas de rompimento no Telegram ──
    channel_monitor = None
    try:
        if app.config_obj.get_str("CHANNEL_BREAKOUT_ALERTS", "0") == "1":
            symbol = app.config_obj.get_str("SYMBOL", "XAUUSD")
            channel_monitor = _build_channel_monitor(app, symbol)
            channel_monitor.start()
            if app.config_obj.get_str("TELEGRAM_ENABLED", "0") != "1":
                logging.getLogger("app").warning(
                    "CHANNEL_BREAKOUT_ALERTS=1 mas TELEGRAM_ENABLED=0 — "
                    "alertas de canal não serão entregues")
    except Exception:
        logging.getLogger("app").warning(
            "Channel breakout monitor failed to start", exc_info=True)

    # ── Camada de sinais do canal 1h (sempre ativa: tela + Telegram) ──
    # O sinal é detectado e exibido independente do AUTO_TRADE_ENABLED;
    # a flag apenas controla se a operação é aberta automaticamente.
    signal_engine = None
    try:
        symbol = app.config_obj.get_str("SYMBOL", "XAUUSD")
        signal_engine = _build_signal_engine(app, symbol)
        app.auto_signal_engine = signal_engine
        signal_engine.start()
    except Exception:
        logging.getLogger("app").warning(
            "Auto signal engine failed to start", exc_info=True)

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
        if channel_monitor:
            channel_monitor.stop()
        if signal_engine:
            signal_engine.stop()
        loop_thread.join(timeout=3)
        if app.broker:
            app.broker.shutdown()
