from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class MarketDataFetchError(RuntimeError):
    pass


def _yahoo_ticker_symbol(ticker: str) -> str:
    cleaned = ticker.strip().upper()
    if cleaned.endswith(".JK"):
        return cleaned
    return f"{cleaned}.JK"


def _fetch_json(url: str, timeout: int = 15) -> Any:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urlopen(request, timeout=timeout)
    body = response.read().decode("utf-8")
    return json.loads(body)


def fetch_eod_history(ticker: str, *, max_retries: int = 3, delay_seconds: float = 1.5) -> dict[str, Any]:
    """Fetch EOD market history for a ticker from Yahoo Finance using a resilient retry strategy."""
    symbol = _yahoo_ticker_symbol(ticker)
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?interval=1d&range=5y&includeAdjustedClose=true"
    )

    for attempt in range(max_retries):
        try:
            return _fetch_json(url)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            status_code = getattr(exc, "code", None)
            if status_code is not None and status_code in {429, 500, 502, 503, 504}:
                if attempt < max_retries - 1:
                    time.sleep((2 ** attempt) * delay_seconds)
                    continue
            if attempt < max_retries - 1:
                time.sleep(delay_seconds)
                continue
            raise MarketDataFetchError(f"Failed to fetch Yahoo Finance data for {symbol}: {exc}") from exc

    raise MarketDataFetchError(f"Failed to fetch Yahoo Finance data for {symbol} after {max_retries} attempts.")
