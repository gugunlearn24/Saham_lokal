from __future__ import annotations

import hashlib
import json
import sqlite3


def compute_entry_hash(
    broker_id: str,
    trade_date: str,
    ticker: str,
    action: str,
    shares: int,
    gross_amount: int,
    reference_id: str,
) -> str:
    raw_signature = (
        f"{broker_id}{trade_date}{ticker}{action}{shares}{gross_amount}{reference_id}"
    )
    return hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()


def _table_has_column(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in columns)


def entry_hash_exists(connection: sqlite3.Connection, entry_hash: str) -> bool:
    if connection.execute(
        "SELECT 1 FROM journal_entries WHERE entry_hash = ? LIMIT 1",
        (entry_hash,),
    ).fetchone():
        return True

    if _table_has_column(connection, "reconciliation_staging", "entry_hash") and connection.execute(
    "SELECT 1 FROM reconciliation_staging WHERE entry_hash = ? LIMIT 1",
    (entry_hash,),
    ).fetchone():
        return True
    
    raw_rows = connection.execute(
        "SELECT raw_payload FROM reconciliation_staging WHERE raw_payload IS NOT NULL"
    ).fetchall()
    for raw_row in raw_rows:
        raw_payload = raw_row[0]
        if not raw_payload:
            continue
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("entry_hash") == entry_hash:
            return True
    return False
