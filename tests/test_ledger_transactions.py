from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src.db.connection import get_connection
from src.db.migrator import run_migrations
from src.ledger.balance_verifier import verify_account_balance, verify_entry_balance
from src.ledger.transaction_service import (
    LedgerValidationError,
    assert_sufficient_cash_for_purchase,
    create_journal_entry,
    validate_cash_balance,
)


@pytest.fixture
def connection(tmp_path: Path):
    db_path = tmp_path / "portfolio.db"
    run_migrations(db_path)
    db_connection = get_connection(db_path)
    db_connection.execute(
        """
        INSERT INTO accounts (account_id, account_name, account_type, currency)
        VALUES ('EQUITY:CONTRIBUTED_CAPITAL', 'Contributed Capital', 'EQUITY', 'IDR')
        """
    )
    db_connection.commit()
    try:
        yield db_connection
    finally:
        db_connection.close()


def _create_journal(
    connection: sqlite3.Connection,
    *,
    reference_id: str,
    legs: list[dict[str, Any]],
) -> str:
    return create_journal_entry(
        connection,
        entry_date="2026-10-12",
        trade_date="2026-10-12",
        source_type="MANUAL",
        reference_id=reference_id,
        description=f"Test transaction {reference_id}",
        legs=legs,
    )


def test_balanced_journal_is_persisted_and_verifiers_report_balances(
    connection: sqlite3.Connection,
) -> None:
    entry_id = _create_journal(
        connection,
        reference_id="BALANCED-001",
        legs=[
            {
                "account_id": "ASSET:RDN_CASH",
                "debit_amount": 1_500_000,
            },
            {
                "account_id": "EQUITY:CONTRIBUTED_CAPITAL",
                "credit_amount": 1_500_000,
            },
        ],
    )
    connection.commit()

    assert verify_entry_balance(entry_id, connection)
    assert verify_account_balance(connection, "ASSET:RDN_CASH") == 1_500_000
    assert validate_cash_balance(connection) == 1_500_000
    totals = connection.execute(
        "SELECT SUM(debit_amount), SUM(credit_amount) FROM journal_legs WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()
    assert tuple(totals) == (1_500_000, 1_500_000)


def test_unbalanced_journal_is_rejected_without_persisting_partial_entry(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(LedgerValidationError, match="must equal"):
        _create_journal(
            connection,
            reference_id="UNBALANCED-001",
            legs=[
                {
                    "account_id": "ASSET:RDN_CASH",
                    "debit_amount": 1_000_000,
                },
                {
                    "account_id": "EQUITY:CONTRIBUTED_CAPITAL",
                    "credit_amount": 999_999,
                },
            ],
        )

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM journal_legs").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("legs", "message"),
    [
        ([], "requires at least one leg"),
        (
            [{"account_id": "ASSET:RDN_CASH"}],
            "cannot be zero on both sides",
        ),
        (
            [
                {
                    "account_id": "ASSET:RDN_CASH",
                    "debit_amount": 100,
                    "credit_amount": 100,
                }
            ],
            "cannot have both debit and credit",
        ),
    ],
)
def test_rejects_empty_or_invalid_journal_legs(
    connection: sqlite3.Connection,
    legs: list[dict[str, Any]],
    message: str,
) -> None:
    with pytest.raises(LedgerValidationError, match=message):
        _create_journal(connection, reference_id="INVALID-LEG", legs=legs)

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_deposit_and_withdrawal_move_cash_against_equity(
    connection: sqlite3.Connection,
) -> None:
    deposit_id = _create_journal(
        connection,
        reference_id="DEPOSIT-001",
        legs=[
            {
                "account_id": "ASSET:RDN_CASH",
                "debit_amount": 2_000_000,
            },
            {
                "account_id": "EQUITY:CONTRIBUTED_CAPITAL",
                "credit_amount": 2_000_000,
            },
        ],
    )
    withdrawal_id = _create_journal(
        connection,
        reference_id="WITHDRAW-001",
        legs=[
            {
                "account_id": "EQUITY:CONTRIBUTED_CAPITAL",
                "debit_amount": 500_000,
            },
            {
                "account_id": "ASSET:RDN_CASH",
                "credit_amount": 500_000,
            },
        ],
    )
    connection.commit()

    assert verify_entry_balance(deposit_id, connection)
    assert verify_entry_balance(withdrawal_id, connection)
    assert validate_cash_balance(connection) == 1_500_000
    assert verify_account_balance(connection, "EQUITY:CONTRIBUTED_CAPITAL") == -1_500_000

    deposit_legs = connection.execute(
        """
        SELECT account_id, debit_amount, credit_amount
        FROM journal_legs WHERE entry_id = ?
        """,
        (deposit_id,),
    ).fetchall()
    deposit_legs = [tuple(leg) for leg in deposit_legs]
    assert deposit_legs == [
        ("ASSET:RDN_CASH", 2_000_000, 0),
        ("EQUITY:CONTRIBUTED_CAPITAL", 0, 2_000_000),
    ]

    withdrawal_legs = connection.execute(
        """
        SELECT account_id, debit_amount, credit_amount
        FROM journal_legs WHERE entry_id = ?
        """,
        (withdrawal_id,),
    ).fetchall()
    withdrawal_legs = [tuple(leg) for leg in withdrawal_legs]
    assert withdrawal_legs == [
        ("EQUITY:CONTRIBUTED_CAPITAL", 500_000, 0),
        ("ASSET:RDN_CASH", 0, 500_000),
    ]


def test_persisted_monetary_legs_use_integer_sqlite_storage(
    connection: sqlite3.Connection,
) -> None:
    entry_id = _create_journal(
        connection,
        reference_id="INTEGER-001",
        legs=[
            {"account_id": "ASSET:RDN_CASH", "debit_amount": 123_456_789},
            {
                "account_id": "EQUITY:CONTRIBUTED_CAPITAL",
                "credit_amount": 123_456_789,
            },
        ],
    )
    connection.commit()

    storage_types = connection.execute(
        """
        SELECT typeof(debit_amount), typeof(credit_amount)
        FROM journal_legs WHERE entry_id = ?
        """,
        (entry_id,),
    ).fetchall()
    storage_types = [tuple(row) for row in storage_types]

    assert storage_types == [("integer", "integer"), ("integer", "integer")]


@pytest.mark.parametrize("amount", [100.25, "100.25"])
def test_rejects_fractional_monetary_amount_instead_of_truncating(
    connection: sqlite3.Connection, amount: Any
) -> None:
    with pytest.raises((LedgerValidationError, TypeError, ValueError)):
        _create_journal(
            connection,
            reference_id="FRACTIONAL-001",
            legs=[
                {"account_id": "ASSET:RDN_CASH", "debit_amount": amount},
                {
                    "account_id": "EQUITY:CONTRIBUTED_CAPITAL",
                    "credit_amount": amount,
                },
            ],
        )

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_cash_check_allows_exact_balance_and_rejects_insufficient_purchase(
    connection: sqlite3.Connection,
) -> None:
    _create_journal(
        connection,
        reference_id="FUND-RDN",
        legs=[
            {"account_id": "ASSET:RDN_CASH", "debit_amount": 750_000},
            {
                "account_id": "EQUITY:CONTRIBUTED_CAPITAL",
                "credit_amount": 750_000,
            },
        ],
    )
    connection.commit()

    assert_sufficient_cash_for_purchase(connection, 750_000)
    assert_sufficient_cash_for_purchase(connection, 0)
    with pytest.raises(LedgerValidationError, match="Insufficient cash"):
        assert_sufficient_cash_for_purchase(connection, 750_001)

    assert validate_cash_balance(connection) == 750_000
    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 1


def test_account_balance_for_unknown_account_is_zero(
    connection: sqlite3.Connection,
) -> None:
    assert verify_account_balance(connection, "ASSET:RDN_CASH") == 0
    assert validate_cash_balance(connection) == 0
