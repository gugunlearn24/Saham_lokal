from __future__ import annotations

import json
from datetime import date
from typing import Any


class FundamentalDataError(ValueError):
    pass


class DuplicateFundamentalSnapshotError(FundamentalDataError):
    pass


def _normalize_ticker(ticker: str) -> str:
    if not isinstance(ticker, str):
        raise FundamentalDataError("ticker must be a non-empty string.")

    normalized = ticker.strip().upper()
    if not normalized:
        raise FundamentalDataError("ticker must be a non-empty string.")
    return normalized


def _coerce_date(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()

    if not isinstance(value, str):
        raise FundamentalDataError("report_release_date must be a date string or date object.")

    try:
        normalized = date.fromisoformat(value)
    except ValueError as exc:
        raise FundamentalDataError("report_release_date must use YYYY-MM-DD format.") from exc
    return normalized.isoformat()


def _coerce_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise FundamentalDataError(f"{field_name} must be numeric or null.") from exc


def upsert_fundamental_snapshot(
    connection,
    *,
    ticker: str,
    fiscal_year: int,
    fiscal_quarter: int,
    report_release_date: str | date,
    roe_percentage: float | None = None,
    der_ratio: float | None = None,
    per_ratio: float | None = None,
    pbv_ratio: float | None = None,
    net_profit_margin: float | None = None,
    raw_payload: dict[str, Any] | None = None,
    confirm_overwrite: bool = False,
) -> str:
    """Insert a quarterly fundamental snapshot or update it only after explicit confirmation."""
    normalized_ticker = _normalize_ticker(ticker)

    if isinstance(fiscal_year, bool) or not isinstance(fiscal_year, int):
        raise FundamentalDataError("fiscal_year must be an integer.")
    if fiscal_year <= 0:
        raise FundamentalDataError("fiscal_year must be a positive integer.")

    if isinstance(fiscal_quarter, bool) or not isinstance(fiscal_quarter, int):
        raise FundamentalDataError("fiscal_quarter must be an integer.")
    if not 1 <= fiscal_quarter <= 4:
        raise FundamentalDataError("fiscal_quarter must be between 1 and 4.")

    normalized_date = _coerce_date(report_release_date)

    payload = raw_payload or {}
    if not isinstance(payload, dict):
        raise FundamentalDataError("raw_payload must be a dictionary when provided.")

    snapshot_values = {
        "ticker": normalized_ticker,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "report_release_date": normalized_date,
        "roe_percentage": _coerce_float(roe_percentage, "roe_percentage"),
        "der_ratio": _coerce_float(der_ratio, "der_ratio"),
        "per_ratio": _coerce_float(per_ratio, "per_ratio"),
        "pbv_ratio": _coerce_float(pbv_ratio, "pbv_ratio"),
        "net_profit_margin": _coerce_float(net_profit_margin, "net_profit_margin"),
        "raw_payload_json": json.dumps(payload, sort_keys=True),
    }

    existing = connection.execute(
        """
        SELECT 1
        FROM fundamental_snapshots
        WHERE ticker = ? AND fiscal_year = ? AND fiscal_quarter = ?
        """,
        (normalized_ticker, fiscal_year, fiscal_quarter),
    ).fetchone()

    if existing and not confirm_overwrite:
        raise DuplicateFundamentalSnapshotError(
            f"A fundamental snapshot for {normalized_ticker} in FY{fiscal_year} Q{fiscal_quarter} already exists. "
            "Pass confirm_overwrite=True to update it explicitly."
        )

    if existing:
        connection.execute(
            """
            UPDATE fundamental_snapshots
            SET report_release_date = ?,
                roe_percentage = ?,
                der_ratio = ?,
                per_ratio = ?,
                pbv_ratio = ?,
                net_profit_margin = ?,
                raw_payload_json = ?
            WHERE ticker = ? AND fiscal_year = ? AND fiscal_quarter = ?
            """,
            (
                snapshot_values["report_release_date"],
                snapshot_values["roe_percentage"],
                snapshot_values["der_ratio"],
                snapshot_values["per_ratio"],
                snapshot_values["pbv_ratio"],
                snapshot_values["net_profit_margin"],
                snapshot_values["raw_payload_json"],
                normalized_ticker,
                fiscal_year,
                fiscal_quarter,
            ),
        )
    else:
        connection.execute(
            """
            INSERT INTO fundamental_snapshots (
                ticker,
                fiscal_year,
                fiscal_quarter,
                report_release_date,
                roe_percentage,
                der_ratio,
                per_ratio,
                pbv_ratio,
                net_profit_margin,
                raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_values["ticker"],
                snapshot_values["fiscal_year"],
                snapshot_values["fiscal_quarter"],
                snapshot_values["report_release_date"],
                snapshot_values["roe_percentage"],
                snapshot_values["der_ratio"],
                snapshot_values["per_ratio"],
                snapshot_values["pbv_ratio"],
                snapshot_values["net_profit_margin"],
                snapshot_values["raw_payload_json"],
            ),
        )

    connection.commit()
    return f"{normalized_ticker}:{fiscal_year}:Q{fiscal_quarter}"
