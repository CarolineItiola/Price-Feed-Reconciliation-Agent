"""
The actions the agent can take.

Division of labour:

  Code computes facts. Freshness, spread, whether two prices
  sit within tolerance, whether a trust score has fallen
  through the floor. All arithmetic, and arithmetic should not
  be delegated to a language model.

  The agent makes judgements. Which source to spend a query
  on, whether what it holds is enough to commit, who to stop
  believing, when to stop and ask a human. Those are the
  decisions it exists to make.

The tools also enforce the invariants. accept_canonical cannot
write a price that no source reported, so the agent is
structurally unable to average or invent one, however it is
prompted.
"""

import json

import config
import sources
import state as state_module
import trust

# Map a failed quote status onto the trust event it implies.
STATUS_TO_EVENT = {
    sources.UNAVAILABLE: trust.WAS_UNAVAILABLE,
    sources.RATE_LIMITED: trust.WAS_RATE_LIMITED,
    sources.MALFORMED: trust.WAS_MALFORMED,
}


class AgentTools:
    """
    Everything the agent can do, and the bookkeeping that
    happens automatically whenever it does it.
    """

    def __init__(self, asset, registry, canonical, ledger):
        self.asset = asset
        self.sources = registry
        self.state = canonical
        self.ledger = ledger
        self.step = 0
        self.observations = []
        self.finished = False
        self.final_summary = ""

    # helpers

    def latest_usable(self, source_name):
        """Most recent OK quote from a given source."""
        for quote in reversed(self.observations):
            if (quote.source == source_name
                    and quote.status == sources.OK):
                return quote
        return None

    def usable_quotes(self):
        """One most recent usable quote per source."""
        seen = {}
        for quote in reversed(self.observations):
            if (quote.status == sources.OK
                    and quote.source not in seen):
                seen[quote.source] = quote
        return list(seen.values())

    # tools

    def query_source(self, source_name):
        if source_name not in self.sources:
            return {
                "error": "unknown source: " + str(source_name),
                "available": sorted(self.sources),
            }

        source = self.sources[source_name]
        self.ledger.record_query(source_name)
        quote = source.fetch(self.asset)
        self.observations.append(quote)

        result = quote.to_dict()
        result["asset"] = self.asset

        if quote.status != sources.OK:
            event = STATUS_TO_EVENT.get(quote.status)
            if event:
                self.ledger.record(
                    source_name, event, quote.detail
                )
            result["trust_now"] = self.ledger.score(source_name)
            result["unreliable"] = self.ledger.is_unreliable(
                source_name
            )
            return result

        label, why = trust.assess_freshness(quote, self.asset)
        result["freshness"] = label
        result["freshness_reason"] = why

        if label == trust.STALE:
            self.ledger.record(source_name, trust.WAS_STALE, why)

        # Compare against what we already hold from others.
        comparisons = []
        for other in self.usable_quotes():
            if other.source == source_name:
                continue
            agree, spread, explanation = trust.quotes_agree(
                quote, other, self.asset
            )
            comparisons.append({
                "against": other.source,
                "their_price": other.price,
                "spread_pct": spread,
                "agree": agree,
                "explanation": explanation,
            })
        result["comparisons"] = comparisons

        result["trust_now"] = self.ledger.score(source_name)
        result["unreliable"] = self.ledger.is_unreliable(
            source_name
        )
        return result

    def accept_canonical(self, price, supporting_sources,
                         reason, confidence):
        quotes = []
        missing = []
        for name in supporting_sources:
            quote = self.latest_usable(name)
            if quote is None:
                missing.append(name)
            else:
                quotes.append(quote)

        if missing:
            return {
                "rejected": True,
                "error": (
                    "no usable observation this session from: "
                    + ", ".join(missing)
                    + ". A price can only be accepted on the "
                    "strength of a source you actually queried."
                ),
            }

        status = (
            state_module.CONFIRMED if len(quotes) >= 2
            else state_module.PROVISIONAL
        )

        try:
            self.state.accept(
                price=price,
                quotes=quotes,
                reason=reason,
                status=status,
                step=self.step,
                confidence=confidence,
            )
        except state_module.StateIntegrityError as e:
            return {
                "rejected": True,
                "error": str(e),
                "hint": (
                    "Pick one source's price and commit that, or "
                    "escalate. Do not blend."
                ),
            }

        for quote in quotes:
            if quote.price == price:
                self.ledger.record(
                    quote.source,
                    trust.CORROBORATED,
                    "supplied the accepted price " + str(price),
                )
                continue
            spread = trust.spread_pct(quote.price, price)
            tolerance = config.tolerance_for(self.asset)
            if spread is not None and spread <= tolerance:
                self.ledger.record(
                    quote.source,
                    trust.CORROBORATED,
                    "independently corroborated " + str(price)
                    + " to within " + str(round(spread, 3)) + "%",
                )

        return {
            "accepted": True,
            "state": self.state.snapshot(),
            "trust": self.ledger.snapshot(),
        }

    def mark_source_unreliable(self, source_name, reason):
        if source_name not in self.sources:
            return {"error": "unknown source: " + str(source_name)}

        before = self.ledger.score(source_name)
        after = self.ledger.record(
            source_name, trust.OUTVOTED, reason
        )
        return {
            "source": source_name,
            "trust_before": before,
            "trust_now": after,
            "unreliable": self.ledger.is_unreliable(source_name),
            "reason": reason,
        }

    def flag_for_review(self, reason):
        self.state.quarantine(
            reason=reason,
            step=self.step,
            quotes=self.usable_quotes(),
        )
        return {
            "escalated": True,
            "state": self.state.snapshot(),
            "reason": reason,
        }

    def finish(self, summary):
        self.finished = True
        self.final_summary = summary
        self.state.note("FINISH", summary, self.step)
        return {"finished": True, "summary": summary}

    # dispatch

    def call(self, name, arguments):
        handlers = {
            "query_source": self.query_source,
            "accept_canonical": self.accept_canonical,
            "mark_source_unreliable": self.mark_source_unreliable,
            "flag_for_review": self.flag_for_review,
            "finish": self.finish,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": "no such tool: " + str(name)}
        try:
            return handler(**arguments)
        except TypeError as e:
            return {"error": "bad arguments: " + str(e)}


TOOL_SCHEMAS = [
    {
        "name": "query_source",
        "description": (
            "Fetch the current price from one source. Returns the "
            "price, how old it was when it arrived, a freshness "
            "verdict, how it compares against every other source "
            "you have already queried this session, and that "
            "source's current trust score. Costs one query, so "
            "consider the source's rate limit before spending it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_name": {
                    "type": "string",
                    "enum": [
                        "finnhub",
                        "alphavantage",
                        "warehouse_csv",
                    ],
                    "description": "Which source to query.",
                },
            },
            "required": ["source_name"],
        },
    },
    {
        "name": "accept_canonical",
        "description": (
            "Commit a price as the canonical value. The price MUST "
            "be exactly the price one of your supporting sources "
            "reported. Averaging, rounding or adjusting will be "
            "rejected. Two or more agreeing sources gives a "
            "CONFIRMED state; a single source gives PROVISIONAL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "price": {
                    "type": "number",
                    "description": (
                        "Exactly as reported by one source."
                    ),
                },
                "supporting_sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Sources whose observations justify this."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why this price, from these sources, now."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0 to 1.0.",
                },
            },
            "required": [
                "price",
                "supporting_sources",
                "reason",
                "confidence",
            ],
        },
    },
    {
        "name": "mark_source_unreliable",
        "description": (
            "Reduce a source's trust score after it contradicted "
            "sources you have better reason to believe. Use when "
            "you have concluded a source is wrong, not merely "
            "unavailable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_name": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["source_name", "reason"],
        },
    },
    {
        "name": "flag_for_review",
        "description": (
            "Escalate to a human and quarantine the state. Use "
            "when the evidence does not support committing to any "
            "price. Escalating is a correct outcome, not a "
            "failure. It is always better than guessing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "What is unresolved and what a human "
                        "should look at."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "finish",
        "description": (
            "End the session. Call this once the canonical state "
            "is settled, or once you have escalated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "What you concluded and why."
                    ),
                },
            },
            "required": ["summary"],
        },
    },
]