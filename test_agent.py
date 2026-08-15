"""
Tests for the invariants.

These cover the guarantees the agent cannot violate however it
reasons: that a canonical price is always traceable to a real
observation, that staleness is judged against whether the venue
was trading, that trust moves in the right direction, and that
a rejected write leaves the state untouched.

They run entirely offline. No API calls, no keys needed, no
network. That means they are fast, free, deterministic, and a
reviewer can run them on a clone without any setup.

    python test_agent.py
"""

import time

import config
import sources
import state as state_module
import tools
import trust

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  PASS  " + name)
    else:
        FAILED.append((name, detail))
        print("  FAIL  " + name)
        if detail:
            print("        " + str(detail))


def quote(source, price, age_seconds=5.0, asset="BTC",
          status=sources.OK):
    """Build an observation without touching the network."""
    now = time.time()
    return sources.Quote(
        source=source,
        asset=asset,
        status=status,
        price=price,
        reported_at=now - age_seconds,
        observed_at=now,
        latency_ms=10,
    )


def section(title):
    print("")
    print("  " + title)
    print("  " + "-" * len(title))


def test_averaging_is_rejected():
    section("the canonical price must be traceable")

    st = state_module.CanonicalState("BTC")
    a = quote("finnhub", 63000.00)
    b = quote("alphavantage", 63100.00)
    blended = (a.price + b.price) / 2

    rejected = False
    try:
        st.accept(
            blended, [a, b], "averaged the two",
            state_module.CONFIRMED, 1, 0.9,
        )
    except state_module.StateIntegrityError:
        rejected = True

    check("a blended price is refused", rejected)
    check(
        "a refused write leaves the state unset",
        st.price is None and st.status == state_module.UNSET,
        "price=" + str(st.price) + " status=" + st.status,
    )


def test_invented_price_is_rejected():
    st = state_module.CanonicalState("BTC")
    a = quote("finnhub", 63000.00)

    rejected = False
    try:
        st.accept(
            62999.99, [a], "close enough",
            state_module.PROVISIONAL, 1, 0.9,
        )
    except state_module.StateIntegrityError:
        rejected = True

    check("a price no source reported is refused", rejected)


def test_accept_with_no_quotes_is_rejected():
    st = state_module.CanonicalState("BTC")

    rejected = False
    try:
        st.accept(
            63000.0, [], "no evidence",
            state_module.PROVISIONAL, 1, 0.5,
        )
    except state_module.StateIntegrityError:
        rejected = True

    check("accepting with no supporting quotes is refused", rejected)


def test_exact_price_is_accepted():
    st = state_module.CanonicalState("BTC")
    a = quote("finnhub", 63000.00)
    b = quote("alphavantage", 63050.00)

    st.accept(
        63000.00, [a, b], "finnhub is fresher",
        state_module.CONFIRMED, 1, 0.9,
    )

    check("an exactly reported price is accepted", st.price == 63000.00)
    check(
        "the source that supplied it is recorded",
        st.supporting_sources == ("finnhub",),
        st.supporting_sources,
    )
    check(
        "the corroborating source is recorded separately",
        st.corroborated_by == ("alphavantage",),
        st.corroborated_by,
    )


def test_staleness_depends_on_the_venue():
    section("staleness is relative to whether the venue traded")

    two_days = 2 * 24 * 3600
    crypto = quote("finnhub", 63000.0, age_seconds=two_days,
                   asset="BTC")
    equity = quote("finnhub", 305.0, age_seconds=two_days,
                   asset="AAPL")

    crypto_label, _ = trust.assess_freshness(crypto, "BTC")
    equity_label, _ = trust.assess_freshness(equity, "AAPL")

    check(
        "two day old crypto is stale, the market never closed",
        crypto_label == trust.STALE,
        crypto_label,
    )

    if trust.market_is_open("AAPL"):
        check(
            "two day old equity is stale while the market trades",
            equity_label == trust.STALE,
            equity_label,
        )
    else:
        check(
            "two day old equity is acceptable while shut",
            equity_label == trust.ACCEPTABLE,
            equity_label,
        )
        check(
            "the same age gives opposite verdicts by venue",
            crypto_label != equity_label,
            crypto_label + " vs " + equity_label,
        )


def test_missing_timestamp_is_not_assumed_fresh():
    q = sources.Quote(
        source="finnhub", asset="BTC", status=sources.OK,
        price=63000.0, reported_at=None,
        observed_at=time.time(), latency_ms=10,
    )
    label, _ = trust.assess_freshness(q, "BTC")
    check(
        "a quote with no timestamp is flagged, not assumed fresh",
        label == trust.UNKNOWN_AGE,
        label,
    )


def test_tolerance_is_per_asset():
    section("agreement is judged per asset class")

    a = quote("finnhub", 63000.00)
    b = quote("alphavantage", 63082.00)
    agree, spread, _ = trust.quotes_agree(a, b, "BTC")
    check(
        "0.13 percent is noise for crypto",
        agree,
        "spread=" + str(spread),
    )

    c = quote("finnhub", 63000.00)
    d = quote("warehouse_csv", 60480.00)
    agree, spread, _ = trust.quotes_agree(c, d, "BTC")
    check(
        "4 percent is a conflict for crypto",
        not agree,
        "spread=" + str(spread),
    )

    e = quote("finnhub", 305.00, asset="AAPL")
    f = quote("alphavantage", 305.62, asset="AAPL")
    agree, spread, _ = trust.quotes_agree(e, f, "AAPL")
    check(
        "the same 0.2 percent is a conflict for equities",
        not agree,
        "spread=" + str(spread),
    )


def test_no_silent_tolerance_fallback():
    raised = False
    try:
        config.tolerance_for("TSLA")
    except KeyError:
        raised = True
    check(
        "an unmeasured asset fails loudly rather than guessing",
        raised,
    )


def test_trust_moves_in_the_right_direction():
    section("trust reflects what sources actually did")

    led = trust.TrustLedger(["a", "b", "c"])
    start = led.score("a")

    led.record("a", trust.CORROBORATED, "agreed")
    check("corroboration raises trust", led.score("a") > start)

    led.record("b", trust.WAS_STALE, "old data")
    led.record("c", trust.WAS_RATE_LIMITED, "refused us")
    check(
        "stale is punished harder than being rate limited",
        led.score("b") < led.score("c"),
        "stale=" + str(led.score("b"))
        + " limited=" + str(led.score("c")),
    )


def test_repeated_failure_demotes_a_source():
    led = trust.TrustLedger(["warehouse_csv"])
    check(
        "a source starts reliable",
        not led.is_unreliable("warehouse_csv"),
    )

    for _ in range(2):
        led.record("warehouse_csv", trust.WAS_STALE, "still old")

    check(
        "repeated staleness drops a source through the floor",
        led.is_unreliable("warehouse_csv"),
        "trust=" + str(led.score("warehouse_csv")),
    )
    check(
        "trust never goes negative",
        led.score("warehouse_csv") >= 0.0,
    )


def test_tools_reject_unqueried_sources():
    section("the agent cannot cite evidence it never gathered")

    registry = sources.build_sources()
    led = trust.TrustLedger(list(registry))
    st = state_module.CanonicalState("BTC")
    kit = tools.AgentTools("BTC", registry, st, led)

    result = kit.accept_canonical(
        63000.0, ["finnhub"], "never actually asked it", 0.9
    )
    check(
        "accepting on a source never queried is refused",
        result.get("rejected") is True,
        result,
    )
    check(
        "the state is untouched by the refusal",
        st.price is None,
    )


def test_quarantine_clears_confidence():
    registry = sources.build_sources()
    led = trust.TrustLedger(list(registry))
    st = state_module.CanonicalState("BTC")
    kit = tools.AgentTools("BTC", registry, st, led)

    kit.flag_for_review("sources irreconcilable")

    check(
        "escalating quarantines the state",
        st.status == state_module.QUARANTINED,
        st.status,
    )
    check("escalating drops confidence to zero", st.confidence == 0.0)
    check("escalation opens a review record", st.open_review is not None)


def test_audit_trail_records_every_decision():
    section("every decision is recorded with its reason")

    st = state_module.CanonicalState("BTC")
    a = quote("finnhub", 63000.00)
    st.accept(
        63000.00, [a], "only fresh source available",
        state_module.PROVISIONAL, 1, 0.6,
    )
    st.quarantine("later contradicted", 2)

    check("both transitions were recorded", len(st.transitions) == 2)
    check(
        "the reason is preserved on the record",
        "only fresh source available" in st.audit(),
    )


def main():
    print("")
    print("=" * 60)
    print("  INVARIANT TESTS")
    print("=" * 60)

    test_averaging_is_rejected()
    test_invented_price_is_rejected()
    test_accept_with_no_quotes_is_rejected()
    test_exact_price_is_accepted()
    test_staleness_depends_on_the_venue()
    test_missing_timestamp_is_not_assumed_fresh()
    test_tolerance_is_per_asset()
    test_no_silent_tolerance_fallback()
    test_trust_moves_in_the_right_direction()
    test_repeated_failure_demotes_a_source()
    test_tools_reject_unqueried_sources()
    test_quarantine_clears_confidence()
    test_audit_trail_records_every_decision()

    total = len(PASSED) + len(FAILED)
    print("")
    print("=" * 60)
    print("  " + str(len(PASSED)) + "/" + str(total) + " passed")
    if FAILED:
        print("")
        for name, detail in FAILED:
            print("  FAILED: " + name)
    print("=" * 60)
    print("")


if __name__ == "__main__":
    main()