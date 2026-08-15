"""
Configuration for the price feed reconciliation agent.

Every threshold lives here rather than being buried in logic,
so each one can be pointed at, explained and defended.
"""

# Assets
# Crypto trades around the clock. Equities do not.
# The agent must treat an old price differently depending
# on whether the venue was open at the time.

ASSETS = {
    "BTC": {
        "finnhub_symbol": "BINANCE:BTCUSDT",
        "alphavantage_symbol": "BTC",
        "venue": "crypto",
        "trades_247": True,

        # Measured venue divergence 0.13%, plus headroom for
        # timing skew between non-simultaneous fetches.

        "tolerance_pct": 0.5,

    },
    "AAPL": {
        "finnhub_symbol": "AAPL",
        "alphavantage_symbol": "AAPL",
        "venue": "equity",
        "trades_247": False,

        # US equities are consolidated across venues, so live
        # feeds agree to roughly 0.005%. A tighter band is safe
        # and catches genuine conflicts.

        "tolerance_pct": 0.1,
    },
}

DEFAULT_ASSET = "BTC"

# Agreement
# Two quotes agree if they differ by less than the tolerance
# for that asset. Below it, ordinary noise between venues.
# Above it, a genuine conflict the agent has to resolve.
# Tolerance is per-asset because venue divergence differs by
# asset class, so it lives in ASSETS above.

def tolerance_for(asset):
    """
    No fallback by design. An asset with no measured tolerance
    must fail loudly rather than silently inherit a number from
    a different asset class. Guessing a threshold is the same
    class of mistake as averaging two prices.
    """
    return ASSETS[asset]["tolerance_pct"]

DEFAULT_ASSET = "BTC"

# Agreement
# Two quotes agree if they differ by less than the tolerance
# for that asset. Below it, ordinary noise between venues.
# Above it, a genuine conflict the agent has to resolve.
# Tolerance is per-asset because venue divergence differs by
# asset class, so it lives in ASSETS above.

def tolerance_for(asset):
    """
    No fallback by design. An asset with no measured tolerance
    must fail loudly rather than silently inherit a number from
    a different asset class. Guessing a threshold is the same
    class of mistake as averaging two prices.
    """
    return ASSETS[asset]["tolerance_pct"]
    
# Staleness
# A 24/7 asset should never be older than this. If it is,
# the source is broken, not the market.
STALENESS_LIMIT_247_SECONDS = 300

# A closed market legitimately returns an old price.
# Four days covers a weekend plus a public holiday.
STALENESS_LIMIT_CLOSED_SECONDS = 4 * 24 * 3600
# Trust
# Scores run from 0.0 to 1.0.
INITIAL_TRUST = 0.70
TRUST_FLOOR = 0.35
TRUST_CEILING = 0.99

# Rewards and penalties.
# Stale is punished hardest: a source that returns old data
# while appearing healthy is more dangerous than one that
# is honestly offline.
# Rate limiting is punished least: that is our fault for
# querying too often, not the source lying to us.
TRUST_REWARD_CORROBORATED = 0.05
TRUST_PENALTY_UNAVAILABLE = 0.15
TRUST_PENALTY_RATE_LIMITED = 0.05
TRUST_PENALTY_STALE = 0.25
TRUST_PENALTY_OUTVOTED = 0.20

# Source characteristics
# The agent is told about these so it can weigh whether
# spending a scarce query is worth it.
SOURCE_PROFILES = {
    "finnhub": {
        "cost": "cheap",
        "limit": "60 per minute",
        "timestamp_precision": "second",
    },
    "alphavantage": {
        "cost": "scarce",
        "limit": "25 per day, 1 per second",
        "timestamp_precision": "day",
    },
    "warehouse_csv": {
        "cost": "free",
        "limit": "none, local file",
        "timestamp_precision": "second",
    },
}

# Agent loop
# Hard cap on decision steps. An agent that cannot conclude
# within this many moves must escalate rather than loop.
MAX_AGENT_STEPS = 12

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 2048

HTTP_TIMEOUT_SECONDS = 10