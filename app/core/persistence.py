"""State persistence — JSON save/load with day-change detection."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class StatePersistence:
    """Saves and restores operational state to/from JSON files.

    Handles day-change detection so stale state from a previous
    trading day is never restored.
    """

    def __init__(self, data_dir: str = "data"):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # ── snapshot file ───────────────────────────────────────

    @property
    def snapshot_path(self) -> Path:
        return self._data_dir / "session_state.json"

    def save(self, data: dict) -> Path:
        """Persist a dict snapshot to disk (atomic-ish)."""
        payload = dict(data)
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        text = json.dumps(payload, ensure_ascii=True, indent=2, default=str)
        tmp = self.snapshot_path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.snapshot_path)
        return self.snapshot_path

    def save_daily_snapshot(self, data: dict, day_key: str) -> Path:
        """Save a historical snapshot for the given day (never overwritten)."""
        path = self._data_dir / f"session_{day_key}.json"
        payload = dict(data)
        payload["day_key"] = day_key
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        text = json.dumps(payload, ensure_ascii=True, indent=2, default=str)
        path.write_text(text, encoding="utf-8")
        logger.info("Daily snapshot saved: %s", path.name)
        return path

    def load_day(self, day_key: str) -> Optional[dict]:
        """Load persisted state for a specific day (YYYY-MM-DD)."""
        path = self._data_dir / f"session_{day_key}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def list_available_days(self) -> list[str]:
        """Return sorted list of YYYY-MM-DD keys that have snapshots."""
        days = []
        for f in self._data_dir.glob("session_????-??-??.json"):
            key = f.stem.replace("session_", "")
            if len(key) == 10:
                days.append(key)
        return sorted(days, reverse=True)

    def load(self) -> Optional[dict]:
        """Load persisted state if it matches today, else None."""
        if not self.snapshot_path.exists():
            return None
        try:
            data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def is_current_day(self, data: dict, server_time: datetime) -> bool:
        """True when the stored day_key matches server_time's date."""
        stored_day = data.get("day_key") or data.get("session", {}).get("day_key")
        if not stored_day:
            return False
        today = server_time.strftime("%Y-%m-%d")
        return str(stored_day)[:10] == today

    # ── helpers ─────────────────────────────────────────────

    def current_day_key(self, server_time: datetime) -> str:
        return server_time.strftime("%Y-%m-%d")
