from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from src.db.connection import get_connection

ROUNDING_TOLERANCE_IDR = 1000


def _fetch_pending_broker_rows(connection):
    return connection.execute(
        """
        SELECT staging_id, external_reference, trade_date, ticker, action, shares, price_per_share, net_cash_amount,
               raw_payload
        FROM reconciliation_staging
        WHERE source_type = 'BROKER' AND reconciliation_status = 'PENDING'
        ORDER BY trade_date ASC, created_at ASC
        """
    ).fetchall()


def _fetch_bank_rows(connection):
    return connection.execute(
        """
        SELECT staging_id, external_reference, trade_date, ticker, action, shares, price_per_share, net_cash_amount,
               raw_payload
        FROM reconciliation_staging
        WHERE source_type = 'BANK' AND reconciliation_status = 'PENDING'
        ORDER BY trade_date ASC, created_at ASC
        """
    ).fetchall()


def _date_delta_days(date_a: str, date_b: str) -> int:
    first = datetime.strptime(date_a, "%Y-%m-%d").date()
    second = datetime.strptime(date_b, "%Y-%m-%d").date()
    return abs((first - second).days)


def _matching_bank_candidate(trade_row, bank_rows) -> dict[str, Any] | None:
    trade_amount = abs(int(trade_row["net_cash_amount"]))
    trade_date = str(trade_row["trade_date"])

    best_match = None
    best_distance = None
    for bank_row in bank_rows:
        bank_amount = abs(int(bank_row["net_cash_amount"]))
        bank_date = str(bank_row["trade_date"])
        day_gap = _date_delta_days(trade_date, bank_date)

        if day_gap > 3:
            continue

        diff = abs(trade_amount - bank_amount)
        score = (diff, day_gap)
        if best_distance is None or score < best_distance:
            best_match = bank_row
            best_distance = score

    return best_match


def _create_journal_entry_for_trade(connection, trade_row, bank_row) -> str:
    entry_id = str(uuid.uuid4())
    trade_amount = abs(int(trade_row["net_cash_amount"]))
    ticker = str(trade_row["ticker"] or "UNKNOWN")
    action = str(trade_row["action"]).upper()

    portfolio_account_id = f"ASSET:PORTFOLIO:{ticker}"
    connection.execute(
        "INSERT OR IGNORE INTO accounts (account_id, account_name, account_type, currency) VALUES (?, ?, 'ASSET', 'IDR')",
        (portfolio_account_id, f"Portfolio {ticker}"),
    )

    payload = {
        "source": "reconciliation",
        "trade_staging_id": trade_row["staging_id"],
        "bank_staging_id": bank_row["staging_id"],
        "ticker": ticker,
        "action": action,
        "trade_amount": trade_amount,
        "matched_date": trade_row["trade_date"],
    }
    entry_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    connection.execute(
        """
        INSERT INTO journal_entries (
            entry_id, entry_hash, entry_date, trade_date, source_type, reference_id, description, created_at
        ) VALUES (?, ?, ?, ?, 'BROKER_CSV', ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            entry_id,
            entry_hash,
            trade_row["trade_date"],
            trade_row["trade_date"],
            trade_row["external_reference"],
            f"Matched settlement for {ticker} {action} via RDN cash reconciliation",
        ),
    )

    if action == "BUY":
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, ?, ?, 0)",
            (str(uuid.uuid4()), entry_id, portfolio_account_id, trade_amount),
        )
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, 'ASSET:RDN_CASH', 0, ?)",
            (str(uuid.uuid4()), entry_id, trade_amount),
        )
    elif action == "SELL":
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, 'ASSET:RDN_CASH', ?, 0)",
            (str(uuid.uuid4()), entry_id, trade_amount),
        )
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, ?, 0, ?)",
            (str(uuid.uuid4()), entry_id, portfolio_account_id, trade_amount),
        )
    else:
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, 'ASSET:RDN_CASH', ?, 0)",
            (str(uuid.uuid4()), entry_id, trade_amount),
        )
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, ?, 0, ?)",
            (str(uuid.uuid4()), entry_id, portfolio_account_id, trade_amount),
        )

    return entry_id


def reconcile_trades(db_path: str | None = None, rounding_tolerance_idr: int = ROUNDING_TOLERANCE_IDR):
    connection = get_connection(db_path) if db_path is not None else get_connection()
    rows = _fetch_pending_broker_rows(connection)
    bank_rows = _fetch_bank_rows(connection)

    reconciliation_summary: dict[str, list[dict[str, Any]]] = {
        "matched": [],
        "pending": [],
        "flagged": [],
    }

    try:
        for trade_row in rows:
            candidate = _matching_bank_candidate(trade_row, bank_rows)
            if candidate is None:
                reconciliation_summary["pending"].append({
                    "staging_id": trade_row["staging_id"],
                    "external_reference": trade_row["external_reference"],
                    "ticker": trade_row["ticker"],
                    "action": trade_row["action"],
                    "reference": "Pending Clearance",
                })
                continue

            trade_amount = abs(int(trade_row["net_cash_amount"]))
            bank_amount = abs(int(candidate["net_cash_amount"]))
            delta = abs(trade_amount - bank_amount)

            if delta > rounding_tolerance_idr:
                connection.execute(
                    "UPDATE reconciliation_staging SET reconciliation_status = 'FLAGGED' WHERE staging_id = ?",
                    (trade_row["staging_id"],),
                )
                reconciliation_summary["flagged"].append({
                    "staging_id": trade_row["staging_id"],
                    "external_reference": trade_row["external_reference"],
                    "ticker": trade_row["ticker"],
                    "delta": delta,
                    "reason": "nominal mismatch beyond rounding tolerance",
                })
                continue

            connection.execute(
                "UPDATE reconciliation_staging SET reconciliation_status = 'MATCHED' WHERE staging_id = ?",
                (trade_row["staging_id"],),
            )
            connection.execute(
                "UPDATE reconciliation_staging SET reconciliation_status = 'MATCHED' WHERE staging_id = ?",
                (candidate["staging_id"],),
            )
            entry_id = _create_journal_entry_for_trade(connection, trade_row, candidate)
            reconciliation_summary["matched"].append({
                "trade_staging_id": trade_row["staging_id"],
                "bank_staging_id": candidate["staging_id"],
                "entry_id": entry_id,
                "external_reference": trade_row["external_reference"],
                "amount": trade_amount,
            })

        connection.commit()
        return reconciliation_summary
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
