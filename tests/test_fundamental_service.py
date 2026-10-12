from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.db.connection import get_connection
from src.db.migrator import run_migrations
from src.fundamentals.fundamental_service import (
    DuplicateFundamentalSnapshotError,
    FundamentalDataError,
    upsert_fundamental_snapshot,
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


def _upsert_snapshot(
    connection: sqlite3.Connection,
    *,
    ticker: str = "BBRI",
    fiscal_year: int = 2025,
    fiscal_quarter: int = 4,
    report_release_date: str | date = "2026-03-15",
    roe_percentage: float | None = 15.2,
    der_ratio: float | None = 0.8,
    per_ratio: float | None = 12.5,
    pbv_ratio: float | None = 1.7,
    net_profit_margin: float | None = 8.4,
    raw_payload: dict[str, Any] | None = None,
    confirm_overwrite: bool = False,
) -> str:
    return upsert_fundamental_snapshot(
        connection,
        ticker=ticker,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        report_release_date=report_release_date,
        roe_percentage=roe_percentage,
        der_ratio=der_ratio,
        per_ratio=per_ratio,
        pbv_ratio=pbv_ratio,
        net_profit_margin=net_profit_margin,
        raw_payload=raw_payload,
        confirm_overwrite=confirm_overwrite,
    )


def test_stores_quarterly_fundamental_metrics_and_raw_json_payload(
    connection: sqlite3.Connection,
) -> None:
    payload = {"cash_ratio": 1.25, "segment_data": {"local": 72}}

    result = _upsert_snapshot(
        connection,
        ticker=" bbri ",
        fiscal_year=2025,
        fiscal_quarter=4,
        report_release_date=date(2026, 3, 15),
        raw_payload=payload,
    )

    assert result == "BBRI:2025:Q4"
    snapshot = connection.execute(
        """
        SELECT ticker, fiscal_year, fiscal_quarter, report_release_date,
               roe_percentage, der_ratio, per_ratio, pbv_ratio,
               net_profit_margin, raw_payload_json
        FROM fundamental_snapshots
        """
    ).fetchone()
    assert tuple(snapshot[:9]) == (
        "BBRI",
        2025,
        4,
        "2026-03-15",
        15.2,
        0.8,
        12.5,
        1.7,
        8.4,
    )
    assert json.loads(snapshot["raw_payload_json"]) == payload


def test_accepts_all_valid_quarters_and_normalizes_each_ticker(
    connection: sqlite3.Connection,
) -> None:
    for quarter in range(1, 5):
        _upsert_snapshot(
            connection,
            ticker="tlkm",
            fiscal_year=2024,
            fiscal_quarter=quarter,
        )

    rows = connection.execute(
        """
        SELECT ticker, fiscal_year, fiscal_quarter
        FROM fundamental_snapshots
        ORDER BY fiscal_quarter
        """
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("TLKM", 2024, 1),
        ("TLKM", 2024, 2),
        ("TLKM", 2024, 3),
        ("TLKM", 2024, 4),
    ]


@pytest.mark.parametrize("quarter", [0, -1, 5, 100])
def test_rejects_quarter_outside_one_through_four_without_inserting(
    connection: sqlite3.Connection,
    quarter: int,
) -> None:
    with pytest.raises(FundamentalDataError, match="fiscal_quarter must be between 1 and 4"):
        _upsert_snapshot(connection, fiscal_quarter=quarter)

    assert connection.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0] == 0


def test_rejects_boolean_quarter_even_though_bool_is_an_integer_subclass(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(FundamentalDataError):
        _upsert_snapshot(connection, fiscal_quarter=True)

    assert connection.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0] == 0


def test_duplicate_quarter_requires_explicit_overwrite_confirmation(
    connection: sqlite3.Connection,
) -> None:
    _upsert_snapshot(connection, roe_percentage=10.0, raw_payload={"source": "first"})

    with pytest.raises(DuplicateFundamentalSnapshotError, match="confirm_overwrite=True"):
        _upsert_snapshot(connection, roe_percentage=20.0, raw_payload={"source": "second"})

    snapshot = connection.execute(
        """
        SELECT roe_percentage, raw_payload_json
        FROM fundamental_snapshots
        WHERE ticker = 'BBRI' AND fiscal_year = 2025 AND fiscal_quarter = 4
        """
    ).fetchone()
    assert snapshot["roe_percentage"] == 10.0
    assert json.loads(snapshot["raw_payload_json"]) == {"source": "first"}


def test_confirmed_overwrite_updates_existing_quarter_instead_of_duplicating(
    connection: sqlite3.Connection,
) -> None:
    _upsert_snapshot(
        connection,
        report_release_date="2026-03-15",
        roe_percentage=10.0,
        der_ratio=1.4,
        per_ratio=20.0,
        pbv_ratio=2.0,
        net_profit_margin=4.0,
        raw_payload={"source": "initial"},
    )

    result = _upsert_snapshot(
        connection,
        report_release_date="2026-03-20",
        roe_percentage=16.0,
        der_ratio=0.7,
        per_ratio=11.0,
        pbv_ratio=1.2,
        net_profit_margin=9.0,
        raw_payload={"source": "updated"},
        confirm_overwrite=True,
    )

    assert result == "BBRI:2025:Q4"
    assert connection.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0] == 1
    snapshot = connection.execute(
        """
        SELECT report_release_date, roe_percentage, der_ratio, per_ratio,
               pbv_ratio, net_profit_margin, raw_payload_json
        FROM fundamental_snapshots
        """
    ).fetchone()
    assert tuple(snapshot[:6]) == ("2026-03-20", 16.0, 0.7, 11.0, 1.2, 9.0)
    assert json.loads(snapshot["raw_payload_json"]) == {"source": "updated"}


def test_allows_null_optional_metrics_and_defaults_raw_payload_to_empty_json(
    connection: sqlite3.Connection,
) -> None:
    _upsert_snapshot(
        connection,
        fiscal_quarter=2,
        roe_percentage=None,
        der_ratio=None,
        per_ratio=None,
        pbv_ratio=None,
        net_profit_margin=None,
        raw_payload=None,
    )

    snapshot = connection.execute(
        """
        SELECT roe_percentage, der_ratio, per_ratio, pbv_ratio,
               net_profit_margin, raw_payload_json
        FROM fundamental_snapshots
        """
    ).fetchone()
    assert tuple(snapshot[:5]) == (None, None, None, None, None)
    assert json.loads(snapshot["raw_payload_json"]) == {}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"fiscal_year": 0}, "fiscal_year must be a positive integer"),
        ({"fiscal_year": 2025.5}, "fiscal_year must be an integer"),
        ({"ticker": "   "}, "ticker must be a non-empty string"),
        ({"report_release_date": "2026/03/15"}, "YYYY-MM-DD"),
        ({"raw_payload": ["not", "an", "object"]}, "raw_payload must be a dictionary"),
    ],
)
def test_rejects_invalid_snapshot_fields_without_inserting(
    connection: sqlite3.Connection,
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(FundamentalDataError, match=message):
        _upsert_snapshot(connection, **overrides)

    assert connection.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0] == 0


def test_different_ticker_or_fiscal_period_can_store_independent_snapshot(
    connection: sqlite3.Connection,
) -> None:
    first = _upsert_snapshot(
        connection, ticker="BBRI", fiscal_year=2025, fiscal_quarter=4
    )
    second = _upsert_snapshot(
        connection, ticker="TLKM", fiscal_year=2025, fiscal_quarter=4
    )
    third = _upsert_snapshot(
        connection, ticker="BBRI", fiscal_year=2026, fiscal_quarter=1
    )

    assert (first, second, third) == (
        "BBRI:2025:Q4",
        "TLKM:2025:Q4",
        "BBRI:2026:Q1",
    )
    assert connection.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0] == 3
