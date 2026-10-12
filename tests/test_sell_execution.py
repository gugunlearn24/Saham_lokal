from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.db.connection import get_connection
from src.db.migrator import run_migrations
from src.ledger.balance_verifier import verify_entry_balance
from src.ledger.sell_execution_service import execute_sell_journal
from src.ledger.transaction_service import LedgerValidationError


@pytest.fixture
def connection(tmp_path: Path):
    db_path = tmp_path / "portfolio.db"
    run_migrations(db_path)
    db_connection = get_connection(db_path)
    try:
        yield db_connection
    finally:
        db_connection.close()


def _execute_sell(
    connection: sqlite3.Connection,
    *,
    shares: int = 100,
    price: float = 10_000.0,
    gross_amount: int = 1_000_000,
    broker_fee: int = 2_000,
    exchange_levy: int = 500,
    pph_final: int = 1_000,
    cost_basis: int = 800_000,
) -> str:
    return execute_sell_journal(
        connection,
        ticker="BBRI",
        shares=shares,
        selling_price_per_share=price,
        gross_amount=gross_amount,
        broker_fee=broker_fee,
        exchange_levy=exchange_levy,
        pph_final=pph_final,
        cost_basis=cost_basis,
        trade_date="2026-10-12",
        reference_id="SELL-001",
    )


def _legs_by_account(
    connection: sqlite3.Connection, entry_id: str
) -> dict[str, tuple[int, int]]:
    return {
        account_id: (debit, credit)
        for account_id, debit, credit in connection.execute(
            """
            SELECT account_id, debit_amount, credit_amount
            FROM journal_legs
            WHERE entry_id = ?
            """,
            (entry_id,),
        )
    }


def test_sell_journal_decomposes_gross_proceeds_into_expected_legs(
    connection: sqlite3.Connection,
) -> None:
    entry_id = _execute_sell(connection)
    connection.commit()

    assert verify_entry_balance(entry_id, connection)
    assert _legs_by_account(connection, entry_id) == {
        "ASSET:RDN_CASH": (996_500, 0),
        "EXPENSE:BROKER_FEE": (2_000, 0),
        "EXPENSE:EXCHANGE_LEVY": (500, 0),
        "EXPENSE:TAX_PPH_FINAL": (1_000, 0),
        "ASSET:PORTFOLIO:BBRI": (0, 800_000),
        "REVENUE:REALIZED_GAIN_LOSS": (0, 200_000),
    }


def test_sell_pph_final_matches_point_one_percent_of_gross_amount(
    connection: sqlite3.Connection,
) -> None:
    gross_amount = 2_500_000
    expected_tax = 2_500
    entry_id = _execute_sell(
        connection,
        shares=250,
        price=10_000,
        gross_amount=gross_amount,
        broker_fee=4_000,
        exchange_levy=1_000,
        pph_final=expected_tax,
        cost_basis=2_000_000,
    )

    legs = _legs_by_account(connection, entry_id)
    assert legs["EXPENSE:TAX_PPH_FINAL"] == (expected_tax, 0)
    assert legs["ASSET:RDN_CASH"] == (
        gross_amount - 4_000 - 1_000 - expected_tax,
        0,
    )


def test_sell_at_cost_basis_has_no_realized_gain_or_loss_leg(
    connection: sqlite3.Connection,
) -> None:
    entry_id = _execute_sell(
        connection,
        cost_basis=1_000_000,
    )

    assert verify_entry_balance(entry_id, connection)
    assert "REVENUE:REALIZED_GAIN_LOSS" not in _legs_by_account(connection, entry_id)


def test_sell_at_a_loss_debits_realized_gain_loss_to_balance_entry(
    connection: sqlite3.Connection,
) -> None:
    entry_id = _execute_sell(
        connection,
        cost_basis=1_100_000,
    )

    assert verify_entry_balance(entry_id, connection)
    assert _legs_by_account(connection, entry_id) == {
        "ASSET:RDN_CASH": (996_500, 0),
        "EXPENSE:BROKER_FEE": (2_000, 0),
        "EXPENSE:EXCHANGE_LEVY": (500, 0),
        "EXPENSE:TAX_PPH_FINAL": (1_000, 0),
        "ASSET:PORTFOLIO:BBRI": (0, 1_100_000),
        "REVENUE:REALIZED_GAIN_LOSS": (100_000, 0),
    }


def test_sell_journal_is_tagged_with_trade_metadata(
    connection: sqlite3.Connection,
) -> None:
    entry_id = _execute_sell(connection)

    header = connection.execute(
        """
        SELECT entry_date, trade_date, source_type, reference_id, description
        FROM journal_entries WHERE entry_id = ?
        """,
        (entry_id,),
    ).fetchone()

    assert tuple(header[:4]) == (
        "2026-10-12",
        "2026-10-12",
        "BROKER_CSV",
        "SELL-001",
    )
    assert "Sell 100 shares of BBRI" in header[4]


@pytest.mark.parametrize("shares", [0, -1])
def test_sell_rejects_non_positive_share_quantity(
    connection: sqlite3.Connection, shares: int
) -> None:
    with pytest.raises(ValueError, match="shares must be positive"):
        _execute_sell(connection, shares=shares)

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_sell_rejects_non_integer_monetary_input(connection: sqlite3.Connection) -> None:
    with pytest.raises(LedgerValidationError, match="whole Rupiah"):
        execute_sell_journal(
            connection,
            ticker="BBRI",
            shares=100,
            selling_price_per_share=10_000.0,
            gross_amount=1_000_000,
            broker_fee=2_000,
            exchange_levy=500,
            pph_final=1_000,
            cost_basis=800_000.5,  # type: ignore[arg-type]
            trade_date="2026-10-12",
        )

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_sell_rejects_pph_that_does_not_equal_point_one_percent(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises((LedgerValidationError, ValueError), match="PPh|tax|balanced"):
        _execute_sell(connection, pph_final=999)

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_sell_rejects_fees_that_make_net_cash_negative(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises((LedgerValidationError, ValueError)):
        _execute_sell(
            connection,
            gross_amount=1_000,
            broker_fee=2_000,
            exchange_levy=500,
            pph_final=1,
        )

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
