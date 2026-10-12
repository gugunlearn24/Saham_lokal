from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any


class LedgerValidationError(ValueError):
    pass


def _coerce_idr_amount(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise LedgerValidationError(f"{field_name} must be a whole Rupiah amount.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise LedgerValidationError(
            f"{field_name} must be a whole Rupiah amount."
        ) from None
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise LedgerValidationError(f"{field_name} must be a whole Rupiah amount.")
    if amount < 0:
        raise LedgerValidationError(f"{field_name} cannot be negative.")
    return int(amount)


def _ensure_account(connection, account_id: str, account_name: str, account_type: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO accounts (account_id, account_name, account_type, currency) VALUES (?, ?, ?, 'IDR')",
        (account_id, account_name, account_type),
    )


def _get_account_balance(connection, account_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(SUM(CASE WHEN jl.account_id = ? THEN jl.debit_amount ELSE 0 END), 0) - COALESCE(SUM(CASE WHEN jl.account_id = ? THEN jl.credit_amount ELSE 0 END), 0) AS net FROM journal_legs jl WHERE jl.account_id = ?",
        (account_id, account_id, account_id),
    ).fetchone()
    return int((row[0] if row is not None else 0) or 0)


def _validate_balanced_entry(connection, entry_id: str) -> None:
    totals = connection.execute(
        "SELECT COALESCE(SUM(debit_amount), 0), COALESCE(SUM(credit_amount), 0) FROM journal_legs WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()
    debit_total, credit_total = totals
    if debit_total != credit_total:
        raise LedgerValidationError(
            f"Journal entry {entry_id} is not balanced: debit_total={debit_total}, credit_total={credit_total}"
        )


def create_journal_entry(
    connection,
    *,
    entry_date: str,
    source_type: str,
    reference_id: str,
    description: str,
    legs: list[dict[str, Any]],
    trade_date: str | None = None,
) -> str:
    if not legs:
        raise LedgerValidationError("A journal entry requires at least one leg.")

    normalized_legs: list[dict[str, Any]] = []
    for leg in legs:
        account_id = str(leg["account_id"])
        debit_amount = _coerce_idr_amount(
            leg.get("debit_amount", 0) or 0,
            f"Debit amount for {account_id}",
        )
        credit_amount = _coerce_idr_amount(
            leg.get("credit_amount", 0) or 0,
            f"Credit amount for {account_id}",
        )

        if debit_amount == 0 and credit_amount == 0:
            raise LedgerValidationError(f"Journal leg for {account_id} cannot be zero on both sides.")
        if debit_amount > 0 and credit_amount > 0:
            raise LedgerValidationError(f"Journal leg for {account_id} cannot have both debit and credit set.")

        # Ensure account exists before insert
        if account_id.startswith("ASSET:PORTFOLIO:"):
            _ensure_account(connection, account_id, f"Portfolio {account_id.split(':')[-1]}", "ASSET")
        elif account_id == "ASSET:RDN_CASH":
            _ensure_account(connection, account_id, "RDN Cash", "ASSET")
        elif account_id.startswith("EXPENSE:"):
            _ensure_account(connection, account_id, account_id.split(":")[-1].replace("_", " ").title(), "EXPENSE")
        elif account_id.startswith("REVENUE:"):
            _ensure_account(connection, account_id, account_id.split(":")[-1].replace("_", " ").title(), "REVENUE")

        normalized_legs.append({
            "account_id": account_id,
            "debit_amount": debit_amount,
            "credit_amount": credit_amount,
        })

    total_debits = sum(leg["debit_amount"] for leg in normalized_legs)
    total_credits = sum(leg["credit_amount"] for leg in normalized_legs)
    if total_debits != total_credits:
        raise LedgerValidationError(
            f"Double-entry validation failed: total debits ({total_debits}) must equal total credits ({total_credits})."
        )

    entry_id = str(uuid.uuid4())
    payload = {
        "entry_date": entry_date,
        "source_type": source_type,
        "reference_id": reference_id,
        "description": description,
        "trade_date": trade_date,
        "legs": normalized_legs,
    }
    entry_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    connection.execute(
        "INSERT INTO journal_entries (entry_id, entry_hash, entry_date, trade_date, source_type, reference_id, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entry_id, entry_hash, entry_date, trade_date, source_type, reference_id, description),
    )

    for leg in normalized_legs:
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), entry_id, leg["account_id"], leg["debit_amount"], leg["credit_amount"]),
        )

    _validate_balanced_entry(connection, entry_id)
    return entry_id


def validate_cash_balance(connection, account_id: str = "ASSET:RDN_CASH") -> int:
    total_debit = connection.execute(
        "SELECT COALESCE(SUM(debit_amount), 0) FROM journal_legs WHERE account_id = ?",
        (account_id,),
    ).fetchone()[0]
    total_credit = connection.execute(
        "SELECT COALESCE(SUM(credit_amount), 0) FROM journal_legs WHERE account_id = ?",
        (account_id,),
    ).fetchone()[0]
    return int(total_debit - total_credit)


def assert_sufficient_cash_for_purchase(connection, amount: int, account_id: str = "ASSET:RDN_CASH") -> None:
    amount = _coerce_idr_amount(amount, "Purchase amount")
    available_cash = validate_cash_balance(connection, account_id)
    if available_cash < amount:
        raise LedgerValidationError(
            f"Insufficient cash in {account_id}: required {amount}, available {available_cash}."
        )
