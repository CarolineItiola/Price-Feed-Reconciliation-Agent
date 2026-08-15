"""
Price sources.

Every source implements the same interface: fetch(asset) returns a
Quote. The agent never needs to know which source it is talking to,
which is what lets it reason about all of them uniformly.

Four outcomes are distinguished, because they mean different things
and imply different blame:

  OK            a usable price came back
  UNAVAILABLE   the source could not be reached at all
  RATE_LIMITED  the source refused us, but is otherwise healthy
  MALFORMED     the source answered, but with nothing usable in it

Collapsing these into a single "failed" would throw away exactly the
information the agent needs in order to decide who to stop trusting.
"""

import csv
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import config

load_dotenv()

OK = "OK"
UNAVAILABLE = "UNAVAILABLE"
RATE_LIMITED = "RATE_LIMITED"
MALFORMED = "MALFORMED"


@dataclass
class Quote:
    """A single observation from a single source."""

    source: str
    asset: str
    status: str
    price: float = None
    reported_at: float = None
    observed_at: float = None
    latency_ms: int = 0
    detail: str = ""

    @property
    def age_seconds(self):
        """
        How old the price already was when we received it.

        reported_at is when the source says the price was true.
        observed_at is when it reached us. The gap is what matters:
        a source can respond instantly and still hand us a price
        from two days ago.
        """
        if self.reported_at is None or self.observed_at is None:
            return None
        return max(0.0, self.observed_at - self.reported_at)

    def summary(self):
        if self.status != OK:
            return self.source + ": " + self.status + " (" + self.detail + ")"
        age = self.age_seconds
        if age is None:
            age_text = "age unknown"
        else:
            age_text = str(int(age)) + "s old"
        return (
            self.source + ": " + str(self.price)
            + " (" + age_text + ", " + str(self.latency_ms) + "ms)"
        )

    def to_dict(self):
        return {
            "source": self.source,
            "status": self.status,
            "price": self.price,
            "age_seconds": (
                None if self.age_seconds is None
                else round(self.age_seconds, 1)
            ),
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }


@dataclass
class Fault:
    """
    A deliberately injected failure, used only for the demo.

    Kept as its own object so injected behaviour is always visible
    and can never be confused with a real API response.
    """

    offline: bool = False
    rate_limited: bool = False
    price_delta_pct: float = 0.0
    extra_age_seconds: float = 0.0
    note: str = ""
    after_calls: int = 0

    def active(self):
        return (
            self.offline
            or self.rate_limited
            or self.price_delta_pct != 0.0
            or self.extra_age_seconds != 0.0
        )


class Source:
    """Base class. Subclasses implement _fetch only."""

    name = "base"

    def __init__(self):
        self.fault = Fault()
        self.calls = 0

    def profile(self):
        return config.SOURCE_PROFILES.get(self.name, {})

    def fetch(self, asset):
        started = time.time()
        self.calls += 1
        armed = self.calls > self.fault.after_calls

        if armed and self.fault.offline:
            return self._injected(
                asset, started, UNAVAILABLE,
                "injected outage: " + self.fault.note,
            )

        if armed and self.fault.rate_limited:
            return self._injected(
                asset, started, RATE_LIMITED,
                "injected rate limit: " + self.fault.note,
            )

        quote = self._fetch(asset, started)
        return self._distort(quote) if armed else quote

    def _injected(self, asset, started, status, detail):
        now = time.time()
        return Quote(
            source=self.name,
            asset=asset,
            status=status,
            observed_at=now,
            latency_ms=int((now - started) * 1000),
            detail=detail,
        )

    def _distort(self, quote):
        """Apply injected price or age distortion to a real quote."""
        if quote.status != OK:
            return quote

        if self.fault.price_delta_pct:
            factor = 1 + (self.fault.price_delta_pct / 100.0)
            quote.price = round(quote.price * factor, 2)
            quote.detail = "injected price shift: " + self.fault.note

        if self.fault.extra_age_seconds and quote.reported_at:
            quote.reported_at -= self.fault.extra_age_seconds
            quote.detail = "injected staleness: " + self.fault.note

        return quote

    def _fetch(self, asset, started):
        raise NotImplementedError


class FinnhubSource(Source):
    """
    Fast, generous limits, precise second-level timestamps.
    The workhorse source.
    """

    name = "finnhub"

    def __init__(self):
        super().__init__()
        self.key = os.getenv("FINNHUB_API_KEY")

    def _fetch(self, asset, started):
        symbol = config.ASSETS[asset]["finnhub_symbol"]
        url = "https://finnhub.io/api/v1/quote"
        params = {"symbol": symbol, "token": self.key}

        try:
            r = requests.get(
                url,
                params=params,
                timeout=config.HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            return self._injected(
                asset, started, UNAVAILABLE, type(e).__name__
            )

        now = time.time()
        latency = int((now - started) * 1000)

        if r.status_code == 429:
            return Quote(
                source=self.name, asset=asset, status=RATE_LIMITED,
                observed_at=now, latency_ms=latency,
                detail="HTTP 429",
            )

        if r.status_code != 200:
            return Quote(
                source=self.name, asset=asset, status=UNAVAILABLE,
                observed_at=now, latency_ms=latency,
                detail="HTTP " + str(r.status_code),
            )

        try:
            data = r.json()
        except ValueError:
            return Quote(
                source=self.name, asset=asset, status=MALFORMED,
                observed_at=now, latency_ms=latency,
                detail="response was not JSON",
            )

        price = data.get("c")
        reported = data.get("t")

        # Finnhub returns c=0 for a symbol it does not recognise.
        # That is a successful HTTP call carrying no usable price.
        if not price:
            return Quote(
                source=self.name, asset=asset, status=MALFORMED,
                observed_at=now, latency_ms=latency,
                detail="no price in payload (c=0)",
            )

        return Quote(
            source=self.name,
            asset=asset,
            status=OK,
            price=float(price),
            reported_at=float(reported) if reported else None,
            observed_at=now,
            latency_ms=latency,
            detail="",
        )


class AlphaVantageSource(Source):
    """
    Scarce: 25 calls a day, one per second.

    Two quirks worth knowing, both found by probing rather than
    reading the docs:

      1. When rate limited it returns HTTP 200 with an
         "Information" key and no price. A naive adapter would
         treat that as success.
      2. For equities it reports only the trading DAY, not a
         timestamp. We assume 20:00 UTC, which is the US close.
    """

    name = "alphavantage"

    def __init__(self):
        super().__init__()
        self.key = os.getenv("ALPHAVANTAGE_API_KEY")

    def _fetch(self, asset, started):
        meta = config.ASSETS[asset]
        symbol = meta["alphavantage_symbol"]
        url = "https://www.alphavantage.co/query"

        if meta["trades_247"]:
            params = {
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": symbol,
                "to_currency": "USD",
                "apikey": self.key,
            }
        else:
            params = {
                "function": "GLOBAL_QUOTE",
                "symbol": symbol,
                "apikey": self.key,
            }

        try:
            r = requests.get(
                url,
                params=params,
                timeout=config.HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            return self._injected(
                asset, started, UNAVAILABLE, type(e).__name__
            )

        now = time.time()
        latency = int((now - started) * 1000)

        if r.status_code != 200:
            return Quote(
                source=self.name, asset=asset, status=UNAVAILABLE,
                observed_at=now, latency_ms=latency,
                detail="HTTP " + str(r.status_code),
            )

        try:
            data = r.json()
        except ValueError:
            return Quote(
                source=self.name, asset=asset, status=MALFORMED,
                observed_at=now, latency_ms=latency,
                detail="response was not JSON",
            )

        # The fake success. HTTP 200, no price, a polite note.
        for key in ("Information", "Note", "Error Message"):
            if key in data:
                text = str(data[key])[:120]
                status = (
                    RATE_LIMITED
                    if key in ("Information", "Note")
                    else MALFORMED
                )
                return Quote(
                    source=self.name, asset=asset, status=status,
                    observed_at=now, latency_ms=latency,
                    detail="HTTP 200 but no price: " + text,
                )

        if meta["trades_247"]:
            return self._parse_crypto(data, asset, now, latency)
        return self._parse_equity(data, asset, now, latency)

    def _parse_crypto(self, data, asset, now, latency):
        block = data.get("Realtime Currency Exchange Rate", {})
        price = block.get("5. Exchange Rate")
        refreshed = block.get("6. Last Refreshed")

        if not price:
            return Quote(
                source=self.name, asset=asset, status=MALFORMED,
                observed_at=now, latency_ms=latency,
                detail="no exchange rate in payload",
            )

        reported = None
        if refreshed:
            try:
                dt = datetime.strptime(refreshed, "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=timezone.utc)
                reported = dt.timestamp()
            except ValueError:
                reported = None

        return Quote(
            source=self.name, asset=asset, status=OK,
            price=float(price), reported_at=reported,
            observed_at=now, latency_ms=latency,
        )

    def _parse_equity(self, data, asset, now, latency):
        block = data.get("Global Quote", {})
        price = block.get("05. price")
        day = block.get("07. latest trading day")

        if not price:
            return Quote(
                source=self.name, asset=asset, status=MALFORMED,
                observed_at=now, latency_ms=latency,
                detail="no price in Global Quote",
            )

        reported = None
        if day:
            try:
                dt = datetime.strptime(day, "%Y-%m-%d")
                # Assume 20:00 UTC, the US equity close.
                dt = dt.replace(hour=20, tzinfo=timezone.utc)
                reported = dt.timestamp()
            except ValueError:
                reported = None

        return Quote(
            source=self.name, asset=asset, status=OK,
            price=float(price), reported_at=reported,
            observed_at=now, latency_ms=latency,
            detail="timestamp is day-granularity, assumed 20:00 UTC",
        )


class WarehouseCsvSource(Source):
    """
    A local snapshot file, standing in for an internal system.

    Free and instant, but it only knows what was written to it.
    This is the source we control, so it is the one we take
    offline or make stale during the demo.
    """

    name = "warehouse_csv"
    path = "data/warehouse_snapshot.csv"

    def _fetch(self, asset, started):
        now = time.time()
        latency = int((now - started) * 1000)

        if not os.path.exists(self.path):
            return Quote(
                source=self.name, asset=asset, status=UNAVAILABLE,
                observed_at=now, latency_ms=latency,
                detail="snapshot file not found",
            )

        try:
            with open(self.path, newline="") as f:
                rows = list(csv.DictReader(f))
        except OSError as e:
            return Quote(
                source=self.name, asset=asset, status=UNAVAILABLE,
                observed_at=now, latency_ms=latency,
                detail="could not read snapshot: " + type(e).__name__,
            )

        match = None
        for row in rows:
            if row.get("asset", "").strip().upper() == asset.upper():
                match = row
                break

        if match is None:
            return Quote(
                source=self.name, asset=asset, status=MALFORMED,
                observed_at=now, latency_ms=latency,
                detail="asset not present in snapshot",
            )

        try:
            price = float(match["price"])
            reported = float(match["reported_at"])
        except (KeyError, ValueError):
            return Quote(
                source=self.name, asset=asset, status=MALFORMED,
                observed_at=now, latency_ms=latency,
                detail="snapshot row could not be parsed",
            )

        return Quote(
            source=self.name, asset=asset, status=OK,
            price=price, reported_at=reported,
            observed_at=now, latency_ms=latency,
        )


def build_sources():
    """Return a fresh registry of all sources, keyed by name."""
    return {
        s.name: s
        for s in (
            FinnhubSource(),
            AlphaVantageSource(),
            WarehouseCsvSource(),
        )
    }


if __name__ == "__main__":
    # Quick manual check: query every source once for the
    # default asset and print what came back.
    registry = build_sources()
    asset = config.DEFAULT_ASSET
    print("")
    print("Querying all sources for " + asset)
    print("=" * 55)
    for name, source in registry.items():
        quote = source.fetch(asset)
        print("  " + quote.summary())
    print("")