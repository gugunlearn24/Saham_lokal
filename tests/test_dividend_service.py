from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src.corporate_actions.dividend_service import (
    DividendBookingError,
    book_cash_dividend,
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


def test_cash_dividend_is_booked_on_payment_date_to_cash_and_ticker_income(
    connection: sqlite3.Connection,
) -> None:
    payment_date = "2026-10-12"
    net_cash_received = 125_000
    reference_id = "RDN-DIV-001"

    entry_id = book_cash_dividend(
        connection,
        ticker="bbri",
        payment_date=payment_date,
        net_cash_received=net_cash_received,
        reference_id=reference_id,
    )
    connection.commit()

    entry = connection.execute(
        """
        SELECT entry_date, trade_date, source_type, reference_id, description
        FROM journal_entries
        WHERE entry_id = ?
        """,
        (entry_id,),
    ).fetchone()
    assert tuple(entry) == (
        payment_date,
        payment_date,
        "DIVIDEND",
        reference_id,
        "Cash dividend for BBRI",
    )

    legs = connection.execute(
        """
        SELECT account_id, debit_amount, credit_amount
        FROM journal_legs
        WHERE entry_id = ?
        ORDER BY account_id
        """,
        (entry_id,),
    ).fetchall()
    assert [tuple(leg) for leg in legs] == [
        ("ASSET:RDN_CASH", net_cash_received, 0),
        ("REVENUE:DIVIDEND_INCOME:BBRI", 0, net_cash_received),
    ]
    assert sum(leg["debit_amount"] for leg in legs) == sum(
        leg["credit_amount"] for leg in legs
    )


def test_dividend_is_not_accrued_before_payment_and_posts_no_receivable(
    connection: sqlite3.Connection,
) -> None:
    cum_date = "2026-09-20"
    ex_date = "2026-09-21"
    payment_date = "2026-10-12"

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM journal_legs").fetchone()[0] == 0

    entry_id = book_cash_dividend(
        connection,
        ticker="TLKM",
        payment_date=payment_date,
        net_cash_received=75_000,
        reference_id=f"DIV-{cum_date}-{ex_date}",
    )
    connection.commit()

    entry = connection.execute(
        "SELECT entry_date, trade_date FROM journal_entries WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()
    assert tuple(entry) == (payment_date, payment_date)

    accounts = {
        row["account_id"]
        for row in connection.execute(
            "SELECT DISTINCT account_id FROM journal_legs WHERE entry_id = ?",
            (entry_id,),
        )
    }
    assert accounts == {
        "ASSET:RDN_CASH",
        "REVENUE:DIVIDEND_INCOME:TLKM",
    }
    assert not any("RECEIVABLE" in account for account in accounts)


@pytest.mark.parametrize(
    "net_cash_received",
    [0, -1, True, 1.5, float("nan"), float("inf")],
)
def test_rejects_invalid_net_dividend_amount_without_persisting_journal(
    connection: sqlite3.Connection,
    net_cash_received: Any,
) -> None:
    with pytest.raises(DividendBookingError):
        book_cash_dividend(
            connection,
            ticker="BBRI",
            payment_date="2026-10-12",
            net_cash_received=net_cash_received,
            reference_id="RDN-DIV-INVALID",
        )

    assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM journal_legs").fetchone()[0] == 0


def test_uses_supplied_description_for_payment_date_booking(
    connection: sqlite3.Connection,
) -> None:
    entry_id = book_cash_dividend(
        connection,
        ticker="BBRI",
        payment_date="2026-10-12",
        net_cash_received=10_000,
        reference_id="RDN-DIV-002",
        description="BBRI cash dividend paid",
    )
    connection.commit()

    description = connection.execute(
        "SELECT description FROM journal_entries WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()[0]
    assert description == "BBRI cash dividend paid"
