from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_MIN_REQUEST_INTERVAL_SECONDS = 1.5
_last_requested_symbol: str | None = None
_last_request_started_at: float | None = None
_request_throttle_lock = threading.Lock()


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


def _wait_for_next_symbol(symbol: str) -> None:
    global _last_requested_symbol, _last_request_started_at

    with _request_throttle_lock:
        now = time.monotonic()
        if _last_request_started_at is not None and now < _last_request_started_at:
            _last_requested_symbol = None
            _last_request_started_at = None

        if (
            _last_requested_symbol is not None
            and _last_requested_symbol != symbol
            and _last_request_started_at is not None
        ):
            elapsed = now - _last_request_started_at
            remaining_delay = _MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if remaining_delay > 0:
                time.sleep(remaining_delay)
                now = time.monotonic()

        _last_requested_symbol = symbol
        _last_request_started_at = now


def fetch_eod_history(
    ticker: str,
    *,
    max_retries: int = 3,
    delay_seconds: float = 1.0,
) -> dict[str, Any]:
    """Fetch EOD market history for a ticker from Yahoo Finance using a resilient retry strategy."""
    if max_retries <= 0:
        raise ValueError("max_retries must be positive.")
    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative.")

    symbol = _yahoo_ticker_symbol(ticker)
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?interval=1d&range=5y&includeAdjustedClose=true"
    )
    _wait_for_next_symbol(symbol)

    for attempt in range(max_retries):
        try:
            return _fetch_json(url)
        except HTTPError as exc:
            status_code = exc.code
            retryable = status_code == 429 or 500 <= status_code <= 599
            if retryable and attempt < max_retries - 1:
                time.sleep((2**attempt) * delay_seconds)
                continue
            raise MarketDataFetchError(f"Failed to fetch Yahoo Finance data for {symbol}: {exc}") from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise MarketDataFetchError(
                f"Failed to fetch Yahoo Finance data for {symbol}: {exc}"
            ) from exc

    raise MarketDataFetchError(f"Failed to fetch Yahoo Finance data for {symbol} after {max_retries} attempts.")
