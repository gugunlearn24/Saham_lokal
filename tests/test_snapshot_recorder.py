from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.analytics.snapshot_recorder import (
    SnapshotAlreadyExistsError,
    SnapshotRecorderError,
    record_evaluation_snapshot,
)
from src.db.connection import get_connection
from src.db.migrator import run_migrations


@pytest.fixture
def connection(tmp_path: Path):
    db_path = tmp_path / "portfolio.db"
    run_migrations(db_path)
    db_connection = get_connection(db_path)
    try:
        yield db_connection
    finally:
        db_connection.close()


def _record_snapshot(
    connection: sqlite3.Connection,
    *,
    evaluation_date: str | date = "2026-10-10",
    ticker: str = "BBRI",
    technical_status: str = "PASS",
    fundamental_status: str = "PASS",
    overall_signal: str = "HOLD",
    metrics: Any = None,
    snapshot_id: str | None = None,
) -> str:
    return record_evaluation_snapshot(
        connection,
        evaluation_date=evaluation_date,
        ticker=ticker,
        technical_status=technical_status,
        fundamental_status=fundamental_status,
        overall_signal=overall_signal,
        metrics_evaluated_json=metrics
        if metrics is not None
        else {
            "weekly_rsi14": 55.0,
            "close_vs_weekly_ma30": 1.08,
            "active_thresholds": {"weekly_rsi14": [40.0, 70.0]},
        },
        snapshot_id=snapshot_id,
    )


def test_records_normalized_evaluation_snapshot_and_complete_metric_payload(
    connection: sqlite3.Connection,
) -> None:
    metrics = {
        "evaluated_values": {
            "roe_percentage": 15.4,
            "der_ratio": 0.8,
            "weekly_rsi14": 55.0,
        },
        "active_thresholds": {
            "roe_percentage": {"operator": ">=", "threshold": 12.0},
            "weekly_rsi14": {"range": [40.0, 70.0]},
        },
    }

    snapshot_id = _record_snapshot(
        connection,
        evaluation_date=date(2026, 10, 10),
        ticker=" bbri ",
        technical_status="pass",
        fundamental_status="warn",
        overall_signal="rebalance",
        metrics=metrics,
        snapshot_id="snapshot-001",
    )

    assert snapshot_id == "snapshot-001"
    row = connection.execute(
        """
        SELECT snapshot_id, evaluation_date, ticker, technical_status,
               fundamental_status, overall_signal, metrics_evaluated_json
        FROM evaluation_snapshots
        """
    ).fetchone()
    assert tuple(row[:6]) == (
        "snapshot-001",
        "2026-10-10",
        "BBRI",
        "PASS",
        "WARN",
        "REBALANCE",
    )
    assert json.loads(row["metrics_evaluated_json"]) == metrics


def test_snapshot_is_immutable_for_duplicate_ticker_and_evaluation_date(
    connection: sqlite3.Connection,
) -> None:
    first_metrics = {
        "evaluated": {"roe_percentage": 15.0},
        "thresholds": {"roe_percentage": 12.0},
    }
    _record_snapshot(connection, metrics=first_metrics)

    with pytest.raises(SnapshotAlreadyExistsError, match="cannot be modified"):
        _record_snapshot(
            connection,
            ticker="bbri",
            technical_status="FAIL",
            fundamental_status="FAIL",
            overall_signal="EXIT",
            metrics={
                "evaluated": {"roe_percentage": 5.0},
                "thresholds": {"roe_percentage": 14.0},
            },
        )

    row = connection.execute(
        """
        SELECT technical_status, fundamental_status, overall_signal,
               metrics_evaluated_json
        FROM evaluation_snapshots
        WHERE evaluation_date = '2026-10-10' AND ticker = 'BBRI'
        """
    ).fetchone()
    assert tuple(row[:3]) == ("PASS", "PASS", "HOLD")
    assert json.loads(row["metrics_evaluated_json"]) == first_metrics
    assert connection.execute("SELECT COUNT(*) FROM evaluation_snapshots").fetchone()[0] == 1


def test_new_snapshot_keeps_prior_rules_thresholds_as_point_in_time_history(
    connection: sqlite3.Connection,
) -> None:
    first_metrics = {
        "roe_percentage": 13.0,
        "active_thresholds": {"roe_percentage": 12.0},
    }
    second_metrics = {
        "roe_percentage": 13.0,
        "active_thresholds": {"roe_percentage": 14.0},
    }

    _record_snapshot(
        connection,
        evaluation_date="2026-10-10",
        metrics=first_metrics,
    )
    _record_snapshot(
        connection,
        evaluation_date="2026-10-17",
        metrics=second_metrics,
        fundamental_status="FAIL",
        overall_signal="EXIT",
    )

    rows = connection.execute(
        """
        SELECT evaluation_date, fundamental_status, overall_signal,
               metrics_evaluated_json
        FROM evaluation_snapshots
        WHERE ticker = 'BBRI'
        ORDER BY evaluation_date
        """
    ).fetchall()
    assert [row["evaluation_date"] for row in rows] == [
        "2026-10-10",
        "2026-10-17",
    ]
    assert [json.loads(row["metrics_evaluated_json"]) for row in rows] == [
        first_metrics,
        second_metrics,
    ]
    assert tuple(rows[0][:3]) == ("2026-10-10", "PASS", "HOLD")
    assert tuple(rows[1][:3]) == ("2026-10-17", "FAIL", "EXIT")


@pytest.mark.parametrize(
    ("evaluation_date", "ticker"),
    [
        ("2026-10-11", "BBRI"),
        ("2026-10-10", "TLKM"),
    ],
)
def test_unique_key_allows_different_date_or_ticker(
    connection: sqlite3.Connection,
    evaluation_date: str,
    ticker: str,
) -> None:
    _record_snapshot(connection)

    _record_snapshot(
        connection,
        evaluation_date=evaluation_date,
        ticker=ticker,
    )

    assert connection.execute("SELECT COUNT(*) FROM evaluation_snapshots").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("technical_status", "UNKNOWN"),
        ("fundamental_status", "PENDING"),
        ("overall_signal", "SELL"),
    ],
)
def test_rejects_unsupported_status_without_inserting_snapshot(
    connection: sqlite3.Connection,
    field: str,
    value: str,
) -> None:
    with pytest.raises(SnapshotRecorderError):
        _record_snapshot(connection, **{field: value})

    assert connection.execute("SELECT COUNT(*) FROM evaluation_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("evaluation_date", "message"),
    [
        ("2026/10/10", "YYYY-MM-DD"),
        (None, "date string or date object"),
    ],
)
def test_rejects_invalid_evaluation_date_without_inserting_snapshot(
    connection: sqlite3.Connection,
    evaluation_date: Any,
    message: str,
) -> None:
    with pytest.raises(SnapshotRecorderError, match=message):
        _record_snapshot(connection, evaluation_date=evaluation_date)

    assert connection.execute("SELECT COUNT(*) FROM evaluation_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize(
    "metrics",
    [
        "{not-json}",
        {"unsupported": object()},
    ],
)
def test_rejects_invalid_metrics_json_without_inserting_snapshot(
    connection: sqlite3.Connection,
    metrics: Any,
) -> None:
    with pytest.raises(SnapshotRecorderError, match="metrics_evaluated_json"):
        _record_snapshot(connection, metrics=metrics)

    assert connection.execute("SELECT COUNT(*) FROM evaluation_snapshots").fetchone()[0] == 0


def test_accepts_json_text_metrics_and_normalizes_serialized_payload(
    connection: sqlite3.Connection,
) -> None:
    metrics_json = '{"thresholds":{"rsi":[40,70]},"rsi":55}'

    _record_snapshot(connection, metrics=metrics_json)

    row = connection.execute(
        "SELECT metrics_evaluated_json FROM evaluation_snapshots"
    ).fetchone()
    assert json.loads(row["metrics_evaluated_json"]) == {
        "thresholds": {"rsi": [40, 70]},
        "rsi": 55,
    }


def test_database_enforces_composite_unique_key(
    connection: sqlite3.Connection,
) -> None:
    _record_snapshot(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO evaluation_snapshots (
                snapshot_id, evaluation_date, ticker, technical_status,
                fundamental_status, overall_signal, metrics_evaluated_json
            ) VALUES (
                'duplicate-direct', '2026-10-10', 'BBRI', 'PASS',
                'PASS', 'HOLD', '{}'
            )
            """
        )

