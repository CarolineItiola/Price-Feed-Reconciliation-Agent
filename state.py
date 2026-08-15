"""
Canonical price state and the audit log.

The canonical state is the single price the agent currently
believes to be true. It is deliberately difficult to change:
every transition records what caused it, when, and why.

The central invariant is enforced in accept(): the canonical
price must be a price that some source actually reported. The
agent is structurally incapable of inventing a blended value,
because the state store rejects any price it cannot trace back
to an observation.
"""

import time
from dataclasses import dataclass, field

import config

UNSET = "UNSET"
PROVISIONAL = "PROVISIONAL"
CONFIRMED = "CONFIRMED"
QUARANTINED = "QUARANTINED"


class StateIntegrityError(Exception):
    """
    Raised when something tries to write a price that no source
    reported. This is the guard against averaging or inventing.
    """


@dataclass
class Transition:
    step: int
    at: float
    action: str
    reason: str
    price: float = None
    status: str = ""
    sources: tuple = ()

    def line(self):
        stamp = time.strftime("%H:%M:%S", time.localtime(self.at))
        head = "[" + stamp + "] step " + str(self.step) + " " + self.action
        if self.price is not None:
            head += " -> " + str(self.price) + " (" + self.status + ")"
        if self.sources:
            head += " via " + ", ".join(self.sources)
        return head + "\n    reason: " + self.reason


class CanonicalState:
    """The one price the agent currently stands behind."""

    def __init__(self, asset):
        self.asset = asset
        self.price = None
        self.status = UNSET
        self.supporting_sources = ()
        self.corroborated_by = ()
        self.accepted_at = None
        self.reported_at = None
        self.confidence = 0.0
        self.transitions = []
        self.open_review = None

    def accept(self, price, quotes, reason, status, step,
               confidence):
        """
        Commit a price as canonical.

        quotes is the list of observations that justify it. The
        price must match one of them exactly.
        """
        reported = [
            q.price for q in quotes if q.price is not None
        ]
        if not reported:
            raise StateIntegrityError(
                "cannot accept a price with no supporting quotes"
            )
        if price not in reported:
            raise StateIntegrityError(
                "refused: " + str(price) + " was not reported by "
                "any source. Supporting quotes were "
                + str(reported)
                + ". The canonical price must be traceable to a "
                "single observation, never a blend or an average."
            )

        self.price = price
        self.status = status
        self.supporting_sources = tuple(
            q.source for q in quotes if q.price == price
        )
        self.corroborated_by = tuple(
            q.source for q in quotes if q.price != price
        )
        self.accepted_at = time.time()
        self.reported_at = max(
            (q.reported_at for q in quotes
             if q.price == price and q.reported_at),
            default=None,
        )
        self.confidence = round(confidence, 2)
        self.open_review = None

        self._record(
            step, "ACCEPT", reason,
            price=price, status=status,
            sources=self.supporting_sources,
        )

    def quarantine(self, reason, step, quotes=()):
        """
        Freeze the state and escalate. The previous canonical
        price is retained but no longer treated as current.
        """
        self.status = QUARANTINED
        self.confidence = 0.0
        self.open_review = {
            "opened_at": time.time(),
            "reason": reason,
            "observations": [q.to_dict() for q in quotes],
        }
        self._record(step, "QUARANTINE", reason)

    def note(self, action, reason, step):
        """Record a decision that did not change the price."""
        self._record(step, action, reason)

    def _record(self, step, action, reason, price=None,
                status="", sources=()):
        self.transitions.append(
            Transition(
                step=step,
                at=time.time(),
                action=action,
                reason=reason,
                price=price,
                status=status,
                sources=tuple(sources),
            )
        )

    def age_seconds(self):
        if self.reported_at is None:
            return None
        return time.time() - self.reported_at

    def snapshot(self):
        """What the agent is shown at the start of each step."""
        return {
            "asset": self.asset,
            "canonical_price": self.price,
            "status": self.status,
            "price_taken_from": list(self.supporting_sources),
            "corroborated_by": list(self.corroborated_by),
            "confidence": self.confidence,
            "age_seconds": (
                None if self.age_seconds() is None
                else round(self.age_seconds(), 1)
            ),
            "under_review": self.open_review is not None,
        }

    def audit(self):
        return "\n".join(t.line() for t in self.transitions)