from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any


class SnapshotRecorderError(ValueError):
    pass


class SnapshotAlreadyExistsError(SnapshotRecorderError):
    pass


VALID_TECHNICAL_STATUSES = {"PASS", "WARN", "FAIL"}
VALID_FUNDAMENTAL_STATUSES = {"PASS", "WARN", "FAIL"}
VALID_OVERALL_SIGNALS = {"HOLD", "BUY_MORE", "REBALANCE", "EXIT"}


def _normalize_status(value: str, *, field_name: str, allowed: set[str]) -> str:
    if not isinstance(value, str):
        raise SnapshotRecorderError(f"{field_name} must be a string.")
    normalized = value.strip().upper()
    if normalized not in allowed:
        raise SnapshotRecorderError(
            f"{field_name} must be one of: {', '.join(sorted(allowed))}."
        )
    return normalized


def _normalize_date(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise SnapshotRecorderError("evaluation_date must be a date string or date object.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotRecorderError("evaluation_date must use YYYY-MM-DD format.") from exc
    return parsed.isoformat()


def _serialize_metrics(metrics: Any) -> str:
    if metrics is None:
        return json.dumps({}, sort_keys=True)
    if isinstance(metrics, str):
        try:
            payload = json.loads(metrics)
        except json.JSONDecodeError as exc:
            raise SnapshotRecorderError("metrics_evaluated_json must be valid JSON text or a JSON-serializable object.") from exc
        return json.dumps(payload, sort_keys=True)
    try:
        return json.dumps(metrics, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise SnapshotRecorderError("metrics_evaluated_json must be JSON-serializable.") from exc


def record_evaluation_snapshot(
    connection,
    *,
    evaluation_date: str | date,
    ticker: str,
    technical_status: str,
    fundamental_status: str,
    overall_signal: str,
    metrics_evaluated_json: Any,
    snapshot_id: str | None = None,
) -> str:
    """Persist an immutable evaluation snapshot and reject duplicate historical entries."""
    normalized_date = _normalize_date(evaluation_date)
    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise SnapshotRecorderError("ticker must be a non-empty string.")

    technical = _normalize_status(technical_status, field_name="technical_status", allowed=VALID_TECHNICAL_STATUSES)
    fundamental = _normalize_status(fundamental_status, field_name="fundamental_status", allowed=VALID_FUNDAMENTAL_STATUSES)
    overall = _normalize_status(overall_signal, field_name="overall_signal", allowed=VALID_OVERALL_SIGNALS)

    if connection.execute(
        "SELECT 1 FROM evaluation_snapshots WHERE evaluation_date = ? AND ticker = ?",
        (normalized_date, normalized_ticker),
    ).fetchone():
        raise SnapshotAlreadyExistsError(
            f"A snapshot for {normalized_ticker} on {normalized_date} already exists and cannot be modified."
        )

    record_id = snapshot_id or str(uuid.uuid4())
    payload = _serialize_metrics(metrics_evaluated_json)

    connection.execute(
        """
        INSERT INTO evaluation_snapshots (
            snapshot_id,
            evaluation_date,
            ticker,
            technical_status,
            fundamental_status,
            overall_signal,
            metrics_evaluated_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            normalized_date,
            normalized_ticker,
            technical,
            fundamental,
            overall,
            payload,
        ),
    )
    connection.commit()
    return record_id
