"""
Assessment: how fresh a quote is, whether two quotes agree,
and how much each source has earned our belief.

Trust is not static configuration. Every observation moves a
source's score, so by the end of a session the agent can say
not only which source it believes, but what that source did
to earn it.

Freshness is judged relative to whether the venue was trading.
A two-day-old equity price on a Saturday is old but correct.
A two-minute-old crypto price is broken. Same age, opposite
meaning, so one rule cannot cover both.
"""

import time
from datetime import datetime, timezone

import config

# Freshness labels
FRESH = "FRESH"
ACCEPTABLE = "ACCEPTABLE"
STALE = "STALE"
UNKNOWN_AGE = "UNKNOWN_AGE"

# Trust events
CORROBORATED = "CORROBORATED"
OUTVOTED = "OUTVOTED"
WAS_UNAVAILABLE = "UNAVAILABLE"
WAS_RATE_LIMITED = "RATE_LIMITED"
WAS_STALE = "STALE"
WAS_MALFORMED = "MALFORMED"

# US equities trade 13:30 to 20:00 UTC.
US_OPEN_UTC_MINUTES = 13 * 60 + 30
US_CLOSE_UTC_MINUTES = 20 * 60


def market_is_open(asset, at=None):
    """
    Whether the venue for this asset is currently trading.

    Public holidays are not modelled. That is a known gap:
    on a market holiday the agent would treat a legitimately
    closed market as open and could over-penalise a source
    for staleness. Listed in the README as future work.
    """
    if config.ASSETS[asset]["trades_247"]:
        return True

    dt = datetime.fromtimestamp(
        at if at else time.time(), timezone.utc
    )
    if dt.weekday() >= 5:
        return False

    minutes = dt.hour * 60 + dt.minute
    return US_OPEN_UTC_MINUTES <= minutes < US_CLOSE_UTC_MINUTES


def assess_freshness(quote, asset):
    """
    Classify how old a quote is, in context.
    Returns (label, human readable explanation).
    """
    age = quote.age_seconds
    if age is None:
        return UNKNOWN_AGE, "source reported no timestamp"

    age_text = str(int(age)) + "s"
    limit_live = config.STALENESS_LIMIT_247_SECONDS

    if config.ASSETS[asset]["trades_247"]:
        if age <= limit_live:
            return FRESH, (
                age_text + " old, within the "
                + str(limit_live) + "s limit for a 24/7 venue"
            )
        return STALE, (
            age_text + " old on a venue that never closes. "
            "The market did not stop, so this is the source "
            "lagging, not the market being shut."
        )

    if market_is_open(asset):
        if age <= limit_live:
            return FRESH, (
                age_text + " old while the market is open"
            )
        return STALE, (
            age_text + " old while the market is actively "
            "trading. The source is behind the tape."
        )

    limit_closed = config.STALENESS_LIMIT_CLOSED_SECONDS
    if age <= limit_closed:
        return ACCEPTABLE, (
            age_text + " old, but the market is closed. This "
            "is the last traded price and is correct, not stale."
        )
    return STALE, (
        age_text + " old, which is too old even allowing for "
        "the market being closed."
    )


def spread_pct(price_a, price_b):
    """Percentage gap between two prices, relative to their midpoint."""
    if not price_a or not price_b:
        return None
    midpoint = (price_a + price_b) / 2.0
    return abs(price_a - price_b) / midpoint * 100.0


def quotes_agree(quote_a, quote_b, asset):
    """
    Whether two quotes are close enough to be the same price.
    Returns (agree, spread percentage, explanation).
    """
    spread = spread_pct(quote_a.price, quote_b.price)
    if spread is None:
        return False, None, "one or both quotes carry no price"

    limit = config.tolerance_for(asset)
    agree = spread <= limit
    verdict = "within" if agree else "outside"
    return agree, round(spread, 4), (
        quote_a.source + " and " + quote_b.source + " differ by "
        + str(round(spread, 3)) + "%, " + verdict
        + " the " + str(limit) + "% tolerance for " + asset
    )


class TrustLedger:
    """
    Running reliability score per source.

    Starts every source at the same place. What separates them
    by the end of a run is only what they actually did.
    """

    def __init__(self, source_names):
        self.scores = {
            name: config.INITIAL_TRUST for name in source_names
        }
        self.history = {name: [] for name in source_names}
        self.counts = {
            name: {"queries": 0, "usable": 0, "failures": 0}
            for name in source_names
        }

    def record_query(self, source):
        self.counts[source]["queries"] += 1

    def record(self, source, event, note=""):
        """Apply a trust adjustment and log why."""
        before = self.scores[source]

        if event == CORROBORATED:
            delta = config.TRUST_REWARD_CORROBORATED
            self.counts[source]["usable"] += 1
        elif event == WAS_UNAVAILABLE:
            delta = -config.TRUST_PENALTY_UNAVAILABLE
            self.counts[source]["failures"] += 1
        elif event == WAS_RATE_LIMITED:
            delta = -config.TRUST_PENALTY_RATE_LIMITED
            self.counts[source]["failures"] += 1
        elif event == WAS_STALE:
            delta = -config.TRUST_PENALTY_STALE
            self.counts[source]["failures"] += 1
        elif event == WAS_MALFORMED:
            delta = -config.TRUST_PENALTY_UNAVAILABLE
            self.counts[source]["failures"] += 1
        elif event == OUTVOTED:
            delta = -config.TRUST_PENALTY_OUTVOTED
            self.counts[source]["failures"] += 1
        else:
            delta = 0.0

        after = min(
            config.TRUST_CEILING,
            max(0.0, before + delta),
        )
        self.scores[source] = round(after, 3)

        self.history[source].append({
            "event": event,
            "note": note,
            "before": round(before, 3),
            "after": self.scores[source],
            "delta": round(after - before, 3),
        })
        return self.scores[source]

    def score(self, source):
        return self.scores[source]

    def is_unreliable(self, source):
        return self.scores[source] < config.TRUST_FLOOR

    def most_trusted(self, candidates):
        """Highest scoring source from a list of names."""
        if not candidates:
            return None
        return max(candidates, key=lambda n: self.scores[n])

    def snapshot(self):
        return {
            name: {
                "trust": self.scores[name],
                "unreliable": self.is_unreliable(name),
                "queries": self.counts[name]["queries"],
                "usable": self.counts[name]["usable"],
                "failures": self.counts[name]["failures"],
            }
            for name in self.scores
        }

    def report(self):
        lines = []
        for name in sorted(
            self.scores, key=lambda n: -self.scores[n]
        ):
            c = self.counts[name]
            flag = " UNRELIABLE" if self.is_unreliable(name) else ""
            lines.append(
                "  " + name.ljust(16)
                + "trust " + format(self.scores[name], ".2f")
                + "  queries " + str(c["queries"])
                + "  usable " + str(c["usable"])
                + "  failures " + str(c["failures"])
                + flag
            )
        return "\n".join(lines)