"""
Probe script.

Calls both price APIs once each and prints the raw JSON.

This was written before any adapter code, to find out what the
APIs actually return rather than assuming. It is what turned up
the two quirks the adapters handle: Alpha Vantage returning
HTTP 200 with no price when rate limited, and Finnhub returning
c=0 for symbols it does not recognise.

Kept in the repository as the evidence behind those design
decisions.

    python probe.py
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

AV_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
FH_KEY = os.getenv("FINNHUB_API_KEY")


def show(title, response):
    print("=" * 55)
    print(title)
    print("=" * 55)
    print("HTTP status:", response.status_code)
    try:
        print(json.dumps(response.json(), indent=2))
    except Exception:
        print("Not JSON. Raw text:")
        print(response.text[:500])
    print("")


def probe_alpha_vantage_stock():
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "GLOBAL_QUOTE",
        "symbol": "AAPL",
        "apikey": AV_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    show("ALPHA VANTAGE - stock quote (AAPL)", r)


def probe_alpha_vantage_crypto():
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "CURRENCY_EXCHANGE_RATE",
        "from_currency": "BTC",
        "to_currency": "USD",
        "apikey": AV_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    show("ALPHA VANTAGE - crypto rate (BTC/USD)", r)


def probe_finnhub_stock():
    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": "AAPL", "token": FH_KEY}
    r = requests.get(url, params=params, timeout=15)
    show("FINNHUB - stock quote (AAPL)", r)


def probe_finnhub_crypto():
    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": "BINANCE:BTCUSDT", "token": FH_KEY}
    r = requests.get(url, params=params, timeout=15)
    show("FINNHUB - crypto quote (BINANCE:BTCUSDT)", r)


if __name__ == "__main__":
    print("")
    print("Keys loaded:")
    print("  Alpha Vantage:", "yes" if AV_KEY else "MISSING")
    print("  Finnhub:      ", "yes" if FH_KEY else "MISSING")
    print("")

    probe_alpha_vantage_stock()
    probe_alpha_vantage_crypto()
    probe_finnhub_stock()
    probe_finnhub_crypto()
