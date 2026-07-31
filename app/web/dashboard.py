"""Dashboard data aggregator — prepares data for templates."""
from __future__ import annotations

from typing import Optional

from app.core.session import TradingSession
from app.core.challenge import ChallengeAccount


class Dashboard:
    """Aggregates all data needed by the web frontend into a single dict."""

    @staticmethod
    def get_state(
        session: TradingSession,
        challenge: Optional[ChallengeAccount] = None,
    ) -> dict:
        state = session.to_dict()
        if challenge:
            state["challenge"] = challenge.to_dict()
        return state
