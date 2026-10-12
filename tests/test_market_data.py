from __future__ import annotations

import json
import sqlite3
import time
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from src.db.connection import get_connection
from src.db.migrator import run_migrations
from src.market_data import market_api_client
from src.market_data.market_api_client import MarketDataFetchError, fetch_eod_history
from src.market_data.price_repository import (
    PriceRepositoryError,
    fetch_with_stale_cache_fallback,
    get_latest_market_price,
    persist_eod_history,
    upsert_market_prices,
)


@pytest.fixture
def connection(tmp_path: Path):
    db_path = tmp_path / "portfolio.db"
    run_migrations(db_path)
    db_connection = get_connection(db_path)
    try:
        yield db_connection
    finally:
        db_connection.close()


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body


def _yahoo_payload() -> dict[str, Any]:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [1_767_225_600, 1_767_312_000],
                    "indicators": {
                        "quote": [{"close": [100.0, 102.0], "volume": [1000, 1200]}],
                        "adjclose": [{"adjclose": [98.0, 101.0]}],
                    },
                }
            ],
            "error": None,
        }
    }


@pytest.mark.parametrize(
    ("ticker", "symbol"),
    [("bbri", "BBRI.JK"), ("BBRI.JK", "BBRI.JK")],
)
def test_fetch_is_on_demand_and_uses_yahoo_chart_endpoint_with_jk_suffix(
    monkeypatch: pytest.MonkeyPatch,
    ticker: str,
    symbol: str,
) -> None:
    calls: list[str] = []

    def fake_urlopen(request, timeout: int):
        calls.append(request.full_url)
        assert timeout > 0
        return _FakeResponse(_yahoo_payload())

    monkeypatch.setattr(market_api_client, "urlopen", fake_urlopen)
    assert calls == []

    payload = fetch_eod_history(ticker)

    assert payload == _yahoo_payload()
    assert len(calls) == 1
    assert calls[0].startswith(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?"
    )
    assert "interval=1d" in calls[0]
    assert "range=5y" in calls[0]
    assert "includeAdjustedClose=true" in calls[0]


@pytest.mark.parametrize("status_code", [429, 500, 503, 599])
def test_retries_rate_limit_and_server_errors_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    attempts = 0
    delays: list[float] = []

    def fake_urlopen(request, timeout: int):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(
                request.full_url, status_code, "temporary failure", Message(), None
            )
        return _FakeResponse(_yahoo_payload())

    monkeypatch.setattr(market_api_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(market_api_client.time, "sleep", delays.append)

    assert fetch_eod_history("BBRI") == _yahoo_payload()
    assert attempts == 2
    assert delays == [1.0]


def test_stops_after_third_failed_http_attempt_and_raises_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    def fake_urlopen(request, timeout: int):
        nonlocal attempts
        attempts += 1
        raise HTTPError(request.full_url, 503, "service unavailable", Message(), None)

    monkeypatch.setattr(market_api_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(market_api_client.time, "sleep", delays.append)

    with pytest.raises(MarketDataFetchError, match="Failed to fetch Yahoo Finance data"):
        fetch_eod_history("BBRI")

    assert attempts == 3
    assert delays == [1.0, 2.0]


def test_does_not_retry_client_error_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    def fake_urlopen(request, timeout: int):
        nonlocal attempts
        attempts += 1
        raise HTTPError(request.full_url, 404, "not found", Message(), None)

    monkeypatch.setattr(market_api_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(market_api_client.time, "sleep", delays.append)

    with pytest.raises(MarketDataFetchError):
        fetch_eod_history("BBRI")

    assert attempts == 1
    assert delays == []


def test_sequential_ticker_requests_are_separated_by_at_least_1500ms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [time.monotonic()]
    request_start_times: list[float] = []

    def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    def fake_urlopen(request, timeout: int):
        request_start_times.append(now[0])
        return _FakeResponse(_yahoo_payload())

    monkeypatch.setattr(market_api_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(market_api_client.time, "sleep", fake_sleep)
    monkeypatch.setattr(market_api_client, "urlopen", fake_urlopen)

    fetch_eod_history("BBRI")
    fetch_eod_history("TLKM")

    assert len(request_start_times) == 2
    assert request_start_times[1] - request_start_times[0] >= 1.5


def test_persists_raw_and_adjusted_close_history_and_updates_existing_dates(
    connection: sqlite3.Connection,
) -> None:
    rows = persist_eod_history(connection, "bbri", _yahoo_payload())

    assert [row["price_date"] for row in rows] == ["2026-01-01", "2026-01-02"]
    assert [(row["raw_close"], row["adjusted_close"]) for row in rows] == [
        (100.0, 98.0),
        (102.0, 101.0),
    ]

    saved_rows = connection.execute(
        """
        SELECT ticker, price_date, raw_close, adjusted_close, volume
        FROM market_prices_eod ORDER BY price_date
        """
    ).fetchall()
    assert [tuple(row) for row in saved_rows] == [
        ("BBRI", "2026-01-01", 100.0, 98.0, 1000),
        ("BBRI", "2026-01-02", 102.0, 101.0, 1200),
    ]

    updated_payload = _yahoo_payload()
    updated_payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = 105.0
    persist_eod_history(connection, "BBRI", updated_payload)

    assert connection.execute(
        "SELECT COUNT(*) FROM market_prices_eod WHERE ticker = 'BBRI'"
    ).fetchone()[0] == 2
    latest = get_latest_market_price(connection, "BBRI")
    assert latest is not None
    assert latest["raw_close"] == 105.0


def test_stale_cache_fallback_returns_last_price_with_warning(
    connection: sqlite3.Connection,
) -> None:
    upsert_market_prices(
        connection,
        [
            {
                "ticker": "BBRI",
                "price_date": "2026-01-01",
                "raw_close": 100.0,
                "adjusted_close": 98.0,
                "volume": 1000,
            },
            {
                "ticker": "BBRI",
                "price_date": "2026-01-02",
                "raw_close": 102.0,
                "adjusted_close": 101.0,
                "volume": 1200,
            },
        ],
    )
    connection.commit()

    def failed_fetcher(ticker: str) -> dict[str, object]:
        raise MarketDataFetchError(f"request failed for {ticker}")

    result = fetch_with_stale_cache_fallback(connection, "bbri", failed_fetcher)

    assert result["ticker"] == "BBRI"
    assert result["price_date"] == "2026-01-02"
    assert result["raw_close"] == 102.0
    assert result["adjusted_close"] == 101.0
    assert result["stale_data"] is True
    assert result["warning"] == "Stale Data"


def test_failed_fetch_without_local_cache_propagates_fetch_error(
    connection: sqlite3.Connection,
) -> None:
    def failed_fetcher(ticker: str) -> dict[str, object]:
        raise MarketDataFetchError(f"request failed for {ticker}")

    with pytest.raises(MarketDataFetchError, match="request failed for BBRI"):
        fetch_with_stale_cache_fallback(connection, "BBRI", failed_fetcher)


def test_empty_market_payload_is_rejected_by_repository(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(PriceRepositoryError, match="No EOD price data found"):
        persist_eod_history(connection, "BBRI", {"chart": {"result": []}})

    assert connection.execute("SELECT COUNT(*) FROM market_prices_eod").fetchone()[0] == 0
