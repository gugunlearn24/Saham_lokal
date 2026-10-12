from __future__ import annotations

from src.db.connection import get_connection


def verify_account_balance(connection, account_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(SUM(debit_amount), 0) - COALESCE(SUM(credit_amount), 0) AS net FROM journal_legs WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return int(row[0] if row else 0)


def verify_entry_balance(entry_id: str, connection=None) -> bool:
    if connection is None:
        connection = get_connection()

    totals = connection.execute(
        "SELECT COALESCE(SUM(debit_amount), 0), COALESCE(SUM(credit_amount), 0) FROM journal_legs WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()
    return bool(totals and totals[0] == totals[1])
