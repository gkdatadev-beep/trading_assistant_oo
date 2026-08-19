"""Persistent trade ledger — app-collected trading history per account.

The ledger is the app's own source of truth for closed trades, so that
statements/analytics can be produced WITHOUT querying MT5 again.

File: data/trades_ledger.json
Structure: {"account": "<login>", "trades": [ {trade record}, ... ]}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# First trade of the challenge: ticket 5329764 on 2026-08-07
CHALLENGE_START = "2026-08-07"


class TradeLedger:
    """Upsert-only ledger of closed trades keyed by ticket."""

    def __init__(self, data_dir: str = "data", account: str = ""):
        self._path = Path(data_dir) / "trades_ledger.json"
        self._account = str(account or "")
        self._trades: dict[int, dict] = {}
        self._load()

    # ── persistence ────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._account = str(raw.get("account") or self._account)
                    for t in raw.get("trades", []):
                        if isinstance(t, dict) and t.get("ticket"):
                            self._trades[int(t["ticket"])] = t
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load trade ledger — starting empty")

    def save(self) -> None:
        try:
            payload = {
                "account": self._account,
                "trades": sorted(
                    self._trades.values(),
                    key=lambda t: t.get("close_time") or t.get("open_time") or "",
                ),
            }
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("Failed to save trade ledger")

    # ── account ────────────────────────────────────────────

    @property
    def account(self) -> str:
        return self._account

    def set_account(self, account: str) -> None:
        if account and str(account) != self._account:
            self._account = str(account)
            self.save()

    def accounts(self) -> list[str]:
        """Accounts present in the ledger (or the configured account)."""
        found = {t.get("account") for t in self._trades.values() if t.get("account")}
        found.add(self._account)
        return sorted(a for a in found if a)

    # ── trades ─────────────────────────────────────────────

    def upsert(self, trades: list[dict]) -> list[dict]:
        """Insert/update trades by ticket. Returns the list of NEW tickets."""
        new: list[dict] = []
        for t in trades:
            ticket = t.get("ticket")
            if not ticket:
                continue
            rec = dict(t)
            rec["account"] = str(rec.get("account") or self._account)
            existing = self._trades.get(int(ticket))
            if existing:
                # Merge: fill missing fields, keep richer values
                merged = dict(existing)
                for k, v in rec.items():
                    if v not in (None, "", 0, 0.0) or k not in merged:
                        merged[k] = v
                if merged != existing:
                    self._trades[int(ticket)] = merged
            else:
                self._trades[int(ticket)] = rec
                new.append(rec)
        if trades:
            self.save()
        return new

    def trades(self, account: Optional[str] = None) -> list[dict]:
        acc = str(account or self._account)
        out = [t for t in self._trades.values() if not acc or t.get("account") == acc]
        return sorted(
            out,
            key=lambda t: t.get("close_time") or t.get("open_time") or "",
        )

    def get(self, ticket: int) -> Optional[dict]:
        return self._trades.get(int(ticket))

    def equity_series(self, account: Optional[str] = None) -> list[dict]:
        """Trade-by-trade cumulative balance evolution."""
        cumulative = 0.0
        series: list[dict] = []
        for t in self.trades(account):
            cumulative = round(cumulative + float(t.get("profit", 0) or 0), 2)
            series.append({
                "ticket": t["ticket"],
                "label": f"#{t['ticket']}",
                "profit": t.get("profit", 0),
                "equity": cumulative,
                "close_time": t.get("close_time") or t.get("open_time"),
            })
        return series
