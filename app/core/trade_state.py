from enum import Enum


class TradeState(Enum):
    """States a trade can be in — linear progression."""
    OPENING = "opening"       # Just opened, initial SL not yet defined
    OPEN = "open"             # SL defined, waiting for lock-entry
    LOCKED = "locked"         # Lock-entry reached, SL at breakeven
    TRAILING = "trailing"     # Max-profit trailing active
    CLOSING = "closing"       # Close requested, awaiting MT5 confirmation
    CLOSED = "closed"         # Confirmed closed

    def can_transition_to(self, target: "TradeState") -> bool:
        order = {
            TradeState.OPENING: 0,
            TradeState.OPEN: 1,
            TradeState.LOCKED: 2,
            TradeState.TRAILING: 3,
            TradeState.CLOSING: 4,
            TradeState.CLOSED: 5,
        }
        return order.get(self, -1) < order.get(target, -1)
