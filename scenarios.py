"""
Demo scenarios.

Failures are injected deliberately rather than waited for. A
three minute demo cannot depend on a live API happening to
break on cue, and injection makes every run reproducible:
anyone can clone this repository and see the same decision
path rather than taking a recording on trust.

Injection is always announced. Each scenario prints exactly
what it has rigged before the agent starts, so nothing in the
transcript is hidden and nothing is a surprise.
"""

import random
import csv
import json
import time

import agent
import sources
import state as state_module
import tools
import trust

SNAPSHOT_PATH = "data/warehouse_snapshot.csv"


def banner(number, title, description):
    print("")
    print("=" * 64)
    print("  SCENARIO " + str(number) + ": " + title)
    print("=" * 64)
    for chunk in description.split("\n"):
        print("  " + chunk)
    print("")


def build(asset):
    registry = sources.build_sources()
    ledger = trust.TrustLedger(list(registry))
    canonical = state_module.CanonicalState(asset)
    return tools.AgentTools(asset, registry, canonical, ledger)


def refresh_warehouse(asset, offset_pct=0.0, age_seconds=0.0,
                      jitter_pct=0.03):
    """
    Rewrite the local snapshot from the live market.

    A small random jitter is applied deliberately. Seeding the
    snapshot straight from finnhub would make it a copy rather
    than a source: two feeds that always agree to the penny are
    not two feeds, and the agent would be treating one
    observation as two independent confirmations. The jitter
    represents a snapshot taken a moment apart from the tick it
    was derived from.
    """
    probe = sources.FinnhubSource()
    quote = probe.fetch(asset)
    if quote.status != sources.OK:
        raise RuntimeError(
            "could not seed the warehouse snapshot: "
            + quote.detail
        )

    drift = random.uniform(-jitter_pct, jitter_pct)
    price = round(
        quote.price * (1 + (offset_pct + drift) / 100.0), 2
    )
    with open(SNAPSHOT_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["asset", "price", "reported_at"])
        writer.writerow([asset, price, time.time() - age_seconds])
    return price

def report(kit):
    print("")
    print("-" * 64)
    print("  FINAL STATE")
    print(json.dumps(kit.state.snapshot(), indent=2))
    print("")
    print("  TRUST")
    print(kit.ledger.report())
    print("")
    print("  AUDIT TRAIL")
    print(kit.state.audit())
    print("")


def scenario_one(asset="BTC"):
    banner(
        1,
        "a source goes offline mid session",
        "finnhub answers once, then stops responding.\n"
        "The agent must notice its primary source has gone and\n"
        "re-plan, rather than quietly reusing the value it\n"
        "already holds.",
    )

    seeded = refresh_warehouse(asset)
    kit = build(asset)
    kit.sources["finnhub"].fault = sources.Fault(
        offline=True,
        after_calls=1,
        note="finnhub drops out after its first response",
    )

    print("  rigged: warehouse seeded fresh at " + str(seeded))
    print("  rigged: finnhub offline from its second call onward")

    agent.run_with(kit, opening_note=(
        "This session requires verification. Once you have a "
        "candidate price, check it again before finishing. A "
        "price that was true a minute ago may not be true now, "
        "and a source that answered once may not answer twice."
    ))
    report(kit)
    return kit


def scenario_two(asset="BTC"):
    banner(
        2,
        "two fresh sources disagree",
        "The warehouse snapshot is written four percent below\n"
        "the market. It is fresh, so recency cannot settle it,\n"
        "and it sits in the agent's normal path. To find out\n"
        "which feed is wrong it has to spend its scarce query.",
    )

    seeded = refresh_warehouse(asset, offset_pct=-4.0)
    kit = build(asset)

    print("  rigged: warehouse seeded 4% below market at "
          + str(seeded))
    print("  rigged: nothing else, finnhub and alphavantage "
          "are untouched")

    agent.run_with(kit, opening_note=(
        "Two of your sources may disagree materially. Averaging "
        "is not available to you. Decide which one to believe "
        "and say why, or escalate if you cannot."
    ))
    report(kit)
    return kit


def scenario_three(asset="BTC"):
    banner(
        3,
        "every source is unavailable",
        "Nothing responds at all. There is no price to be had.\n"
        "The only correct outcome is to escalate with the state\n"
        "left unset, rather than inventing or reusing a value.",
    )

    kit = build(asset)
    for name in kit.sources:
        kit.sources[name].fault = sources.Fault(
            offline=True,
            note="total feed blackout",
        )

    print("  rigged: all three sources offline")

    agent.run_with(kit)
    report(kit)
    return kit


def scenario_four(asset="AAPL"):
    banner(
        4,
        "old but correct, on a closed market",
        "The same two day old price that would mean a broken\n"
        "feed for crypto is the right answer for an equity at\n"
        "the weekend. The agent should accept it rather than\n"
        "penalising the sources for staleness.",
    )

    kit = build(asset)
    print("  rigged: nothing. The market being shut is real.")

    agent.run_with(kit)
    report(kit)
    return kit


SCENARIOS = {
    "1": ("a source goes offline mid session", scenario_one),
    "2": ("two fresh sources disagree", scenario_two),
    "3": ("every source is unavailable", scenario_three),
    "4": ("old but correct on a closed market", scenario_four),
}