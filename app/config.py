"""Runtime configuration — key-value store with locking."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── HARDCODED DISCIPLINE RULES ────────────────────────────
# These CANNOT be changed via /parm. They are the immutable
# trading discipline contract.
DISCIPLINE = {
    "LIMITE_LUCRO_DIARIO": 750.0,
    "LIMITE_PERDA_DIARIO": -600.0,
    "LIMITE_LUCRO_OP": 500.0,
    "LIMITE_PERDA_OP": -200.0,
    "LIMITE_OPERACOES": 40,
    "MAX_PROFIT_RETENTION_PCT": 50.0,
    "OVERTRADING_PROTECTION_TIME": 1,
}

# Default parameter values — user-editable
DEFAULTS: dict[str, Any] = {
    "SYMBOL": "XAUUSD",
    "TIMEFRAME": "TIMEFRAME_M15",
    "LOT_SIZE": 0.15,
    "TP_MULTIPLIER": 4,
    "SL_MULTIPLIER": 2,
    "TRADING_WINDOW_1_START": "01:00",
    "TRADING_WINDOW_1_DURATION_HOURS": 5,
    "TRADING_WINDOW_2_START": "15:00",
    "TRADING_WINDOW_2_DURATION_HOURS": 5,
    "LOCK_ENTRY": 0.5,
    "TRAILING_STOP_BUFFER": 0.5,
    "M5_RANGE_ATR_STOP_MULTIPLIER": 1.0,
    "LIMITE_LUCRO_DIARIO": float(DISCIPLINE["LIMITE_LUCRO_DIARIO"]),
    "LIMITE_PERDA_DIARIO": float(DISCIPLINE["LIMITE_PERDA_DIARIO"]),
    "LIMITE_LUCRO_OP": float(DISCIPLINE["LIMITE_LUCRO_OP"]),
    "LIMITE_PERDA_OP": float(DISCIPLINE["LIMITE_PERDA_OP"]),
    "LIMITE_OPERACOES": int(DISCIPLINE["LIMITE_OPERACOES"]),
    "MAX_PROFIT_RETENTION_PCT": float(DISCIPLINE["MAX_PROFIT_RETENTION_PCT"]),
    "OVERTRADING_PROTECTION_TIME": int(DISCIPLINE["OVERTRADING_PROTECTION_TIME"]),
    "TELEGRAM_ENABLED": "0",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
}


class RuntimeConfig:
    """Simple key-value config with file persistence and lock mechanism."""

    LOCK_DATE_KEY = "_PARAMETERS_LOCK_DATE"
    LOCKED_VALUES_KEY = "_LOCKED_VALUES"

    def __init__(self, data_dir: str = "data"):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._config: dict[str, Any] = dict(DEFAULTS)
        self._load_from_disk()

    @property
    def config_path(self) -> Path:
        return self._data_dir / "runtime_config.json"

    # ── read ────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, DEFAULTS.get(key, default))

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_str(self, key: str, default: str = "") -> str:
        return str(self.get(key, default))

    # ── write ───────────────────────────────────────────────

    def update(self, values: dict[str, Any]) -> None:
        self._config.update(values)
        self._save_to_disk()

    # ── persistence ─────────────────────────────────────────

    def _load_from_disk(self) -> None:
        try:
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._config.update(data)
        except (json.JSONDecodeError, OSError):
            pass

    def _save_to_disk(self) -> None:
        try:
            self.config_path.write_text(
                json.dumps(self._config, ensure_ascii=True, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Failed to save runtime config")

    # ── lock ────────────────────────────────────────────────

    def is_locked_today(self, server_date: str) -> bool:
        locked_date = self._config.get(self.LOCK_DATE_KEY)
        return locked_date == server_date

    def lock_for_today(self, server_date: str) -> None:
        if not self.is_locked_today(server_date):
            self._config[self.LOCK_DATE_KEY] = server_date
            self._config[self.LOCKED_VALUES_KEY] = {
                k: self._config.get(k) for k in DEFAULTS
            }
            self._save_to_disk()

    def to_dict(self) -> dict[str, Any]:
        return {
            k: self._config.get(k, v) for k, v in DEFAULTS.items()
        }
