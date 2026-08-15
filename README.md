# Multi-Source Price Feed Reconciliation Agent

Several services can tell you the price of the same asset, and
they do not always agree. Some go offline while some keep
answering but hand back a price from hours ago without saying
so. The easy answers are to average the prices or to fall back to the last value you trusted and both will produce a number that looks fine and is still wrong.
This project builds an agent that takes neither shortcut. It reasons about which feed to believe, and when it cannot justify a price, it says so and escalates rather than guessing.

## What it does

Three sources are queried: two live APIs and a local snapshot
file standing in for an internal system. At every step the
agent chooses its own next action from what it has observed so
far. It may query another source, commit a price, demote a
source it no longer believes, or hand the problem to a human.
Nothing about that sequence is fixed.

Every decision is written to an audit trail with the reason
the agent gave at the time.

## Quick start

git clone < https://github.com/CarolineItiola/Price-Feed-Reconciliation-Agent->
cd Price-Feed-Reconciliation-Agent
pip install -r requirements.txt

Create a `.env` file with three keys:

ANTHROPIC_API_KEY=your-key
ALPHAVANTAGE_API_KEY=your-key
FINNHUB_API_KEY=your-key

Both price API keys are free. Alpha Vantage is at
alphavantage.co, Finnhub at finnhub.io.

Then:

python test_agent.py # 27 invariant tests, no keys needed
python run_demo.py # list the scenarios
python run_demo.py 1 # run one
python run_demo.py all # run all four

The test suite makes no network calls and needs no API keys, so
it runs on a fresh clone immediately.

## The four scenarios

**1. A source goes offline mid session.** finnhub answers once,
then stops. The agent is asked to verify its candidate price
before finishing, discovers its primary source has gone, and
re-plans: it spends its scarce Alpha Vantage query specifically
because one live source only gets it to PROVISIONAL, and it
wants CONFIRMED.

**2. Two fresh sources disagree.** The warehouse snapshot is
written four percent below market. Both it and finnhub are
fresh and equally trusted, so recency cannot settle it. The
agent queries Alpha Vantage as a tiebreaker, finds it agrees
with finnhub to within 0.103%, marks the warehouse unreliable,
and commits finnhub's exact price.

**3. Every source is unavailable.** Nothing responds. The
agent escalates with no price set rather than inventing or
reusing one. QUARANTINED is the correct outcome here.

**4. Old but correct.** An equity price from Friday, queried at
the weekend. The same two-day age that marks a crypto feed
broken is the right answer for a closed market. The agent
accepts it and penalises nobody.

Failures in scenarios 1 to 3 are injected deliberately. A
three-minute demo cannot wait for a live API to break on cue,
and injection makes every run reproducible: anyone can clone
this and see the same decision path. The agent receives exactly
the same response an authentic failure produces and has no way
to tell the difference. The fire is staged; the evacuation is
real.

## How it decides

### Four outcomes, not two

Sources do not simply succeed or fail. Four cases are
distinguished because they imply different blame:

| Status | Meaning | Whose fault |
|---|---|---|
| `OK` | a usable price | — |
| `UNAVAILABLE` | could not be reached | the source |
| `RATE_LIMITED` | refused us, otherwise healthy | ours |
| `MALFORMED` | answered with nothing usable | the source |

This matters because of something found by probing rather than
by reading documentation: **when Alpha Vantage rate limits you
it returns HTTP 200** with an explanatory note and no price. A
naive adapter checks the status code, sees success, and passes
garbage into canonical state. Finnhub has its own version,
returning `c: 0` for symbols it does not recognise.

Rate limiting is also punished far more lightly than staleness,
because being throttled is our fault for querying too often,
not the source misleading us.

### Staleness is relative to whether the venue traded

A price two days old means opposite things depending on the
market behind it:

- Two days old on a crypto feed: **broken**. The market never
  closed, so the source is lagging.
- Two days old on an equity at the weekend: **correct**. That
  is the last traded price and there is no newer one.

The agent checks whether the venue was open before deciding
whether age is a fault. This is the distinction that lets it
tell "old but right" from "quietly wrong", and stale data is
the more dangerous of the two failures because the source still
looks healthy.

### Trust is earned, not configured

Every source starts at 0.70. Scores move on what sources
actually do: corroboration raises them, unavailability and
staleness lower them, and being outvoted by sources with better
evidence lowers them further. Below 0.35 a source is treated as
unreliable.

In scenario 2 the warehouse falls from 0.70 to 0.50 within a
single session, on evidence gathered during that session.

### The anti-averaging invariant

The state store will not write a price that no source reported.
`accept()` checks the value against the observations offered to
justify it, and raises rather than storing anything it cannot
trace to a single quote.

This is enforced in code rather than requested in the prompt,
and it earns its place. In one run the agent justified its
choice partly on the grounds that the price was *"in the middle
of the three observed prices"* — the averaging instinct
reappearing as a virtue even under explicit instruction not to
average. It happened to pick a real quote that time. The guard
rail means it could not have done otherwise.

## Thresholds, and where they came from

Agreement tolerance is per asset, because venue divergence
differs by asset class:

| Asset | Measured spread | Tolerance | Why |
|---|---|---|---|
| BTC | 0.13% | 0.5% | Binance and Alpha Vantage are genuinely different markets. Headroom covers price movement between non-simultaneous fetches. |
| AAPL | ~0.005% | 0.1% | US equities are consolidated across venues, so live feeds agree far more tightly. |

The 0.13% is measured, not assumed: it is the observed gap
between finnhub and Alpha Vantage quoting BTC at the same
moment. A single global tolerance would either miss real
conflicts in equities or fire constantly on crypto noise.

There is deliberately **no fallback tolerance**. An asset with
no measured value raises rather than inheriting a number from a
different asset class. Guessing a threshold is the same class
of mistake as guessing a price.

## Tests

python test_agent.py


27 tests covering the guarantees the agent cannot violate
however it reasons: blended prices are refused, prices no
source reported are refused, a refused write leaves state
untouched, evidence cannot be cited from a source that was
never queried, freshness flips correctly with the venue, trust
moves in the right direction and never goes negative, and every
decision reaches the audit trail with its reason.

No network, no keys, no fixtures to download.

## What I found while building it

**My own test harness had a false-independence bug.** The first
version seeded the warehouse snapshot directly from finnhub's
live price. The two then agreed to the penny, and the agent
marked the result CONFIRMED on what it believed were two
independent sources. It was one observation counted twice.
Corroboration from a mirror is worth nothing, which is exactly
the failure this agent exists to catch, and I had built it into
the demo. The snapshot is now seeded with a small offset and
the fix is visible in `refresh_warehouse`.

**Agreement from a stale source is not evidence.** In scenario
1 two sources agreed to 0.024%, comfortably inside tolerance.
The agent declined to treat that as corroboration because one
of them was two hours old on a market that never closes, and
spent a scarce query to get a genuinely fresh second opinion
instead. A lagging feed can agree with you and tell you
nothing.

**Conserving a scarce resource can defeat a test.** An earlier
version of scenario 2 put the injected conflict on Alpha
Vantage. The agent never queried it, having already obtained
two agreeing sources, and correctly noted it had no reason to
spend the quota. Good behaviour, useless test. The conflict was
moved into a source the agent reaches for by default.

## Known limitations

**No exchange holiday calendar.** Market hours are computed
from weekday and time of day only. On Christmas Day 2026, a
Friday, `market_is_open("AAPL")` returns `True` and the agent
would penalise a source for staleness that was in fact correct.
Verified, not suspected.

**Equity timestamps are day-granular.** Alpha Vantage reports
only the trading day for equities, so 20:00 UTC is assumed as
the close. Fine for weekend reasoning, too coarse for intraday
work.

**Trust does not decay.** A source demoted early in a session
stays demoted for its duration. There is no path back within a
run and no memory across runs.

**Single asset per session.** State is held for one asset at a
time.

**The warehouse snapshot is synthetic.** It stands in for an
internal system rather than being an independent venue, and is
seeded from market data with an offset. Genuine independence
would need a third real feed.

## What I would do next

**A third genuinely independent feed.** Two live sources means
a disagreement is a coin flip until a tiebreaker arrives. Three
real venues would let the agent reason about a minority report
rather than a stalemate.

**Trust that persists and decays.** Scores should survive
across runs, so a source with a bad week starts from where it
left off, and recover slowly with good behaviour rather than
being condemned permanently by one outage.

**A proper exchange calendar.** The holiday gap above is the
clearest correctness bug in the system and it is a solved
problem, not a research one.

**Volatility-aware tolerance.** A fixed 0.5% is generous in
calm markets and too tight in fast ones. Tolerance should scale
with recent realised volatility so the same threshold does not
mean different things at different times of day.

**Cost as an explicit budget.** The agent already reasons about
Alpha Vantage being scarce, but only because it is told so in
prose. Giving it a real remaining-quota figure would let it
trade certainty against cost deliberately rather than
approximately.

**Replay from recorded fixtures.** Capturing real responses and
replaying them would make runs fully deterministic and let the
same decision path be regression tested rather than observed.