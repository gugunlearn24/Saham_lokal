from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


class PriceRepositoryError(RuntimeError):
    pass


def _coerce_row_mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, dict):
        return row
    if isinstance(row, (list, tuple)) and len(row) >= 5:
        return {
            "ticker": row[0],
            "price_date": row[1],
            "raw_close": row[2],
            "adjusted_close": row[3],
            "volume": row[4],
        }
    raise PriceRepositoryError(f"Unsupported market row payload: {type(row).__name__}")


def parse_eod_payload(ticker: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not payload:
        return []

    chart = payload.get("chart", {}) if isinstance(payload, dict) else {}
    result = chart.get("result") or []
    if not result:
        return []

    series = result[0]
    timestamps = series.get("timestamp") or []
    indicators = series.get("indicators", {}) or {}
    quote = (indicators.get("quote") or [{}])[0]
    adj_close_values = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []
    close_values = quote.get("close") or []
    volume_values = quote.get("volume") or []

    rows: list[dict[str, Any]] = []
    for index, ts in enumerate(timestamps):
        raw_close = close_values[index] if index < len(close_values) else None
        adjusted_close = adj_close_values[index] if index < len(adj_close_values) else raw_close
        if raw_close is None and adjusted_close is None:
            continue

        rows.append(
            {
                "ticker": ticker,
                "price_date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                "raw_close": float(raw_close) if raw_close is not None else float(adjusted_close),
                "adjusted_close": float(adjusted_close) if adjusted_close is not None else float(raw_close),
                "volume": volume_values[index] if index < len(volume_values) else None,
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return rows


def upsert_market_prices(connection: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    for row in rows:
        connection.execute(
            """
            INSERT INTO market_prices_eod (ticker, price_date, raw_close, adjusted_close, volume, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, price_date) DO UPDATE SET
                raw_close = excluded.raw_close,
                adjusted_close = excluded.adjusted_close,
                volume = excluded.volume,
                fetched_at = excluded.fetched_at
            """,
            (
                row["ticker"].upper(),
                row["price_date"],
                float(row["raw_close"]),
                float(row["adjusted_close"]),
                row.get("volume"),
                row.get("fetched_at")
                or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def get_latest_market_price(connection: sqlite3.Connection, ticker: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT ticker, price_date, raw_close, adjusted_close, volume
        FROM market_prices_eod
        WHERE ticker = ?
        ORDER BY price_date DESC
        LIMIT 1
        """,
        (ticker.upper(),),
    ).fetchone()
    if row is None:
        return None
    return _coerce_row_mapping(row)


def persist_eod_history(connection: sqlite3.Connection, ticker: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = parse_eod_payload(ticker, payload)
    if not rows:
        raise PriceRepositoryError(f"No EOD price data found for {ticker}.")

    upsert_market_prices(connection, rows)
    connection.commit()
    return rows


def fetch_with_stale_cache_fallback(
    connection: sqlite3.Connection,
    ticker: str,
    fetcher: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    try:
        payload = fetcher(ticker)
        if not payload:
            raise PriceRepositoryError("Empty market payload received.")
        rows = persist_eod_history(connection, ticker, payload)
        return rows[-1]
    except Exception:
        stale_row = get_latest_market_price(connection, ticker)
        if stale_row is None:
            raise
        stale_row["stale_data"] = True
        stale_row["warning"] = "Stale Data"
        return stale_row
