from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from src.corporate_actions.split_processor import (
    CorporateActionError,
    process_stock_split,
)
from src.db.connection import get_connection
from src.db.migrator import run_migrations


@pytest.fixture
def connection(tmp_path: Path):
    db_path = tmp_path / "portfolio.db"
    run_migrations(db_path)
    db_connection = get_connection(db_path)
    db_connection.execute(
        """
        INSERT INTO accounts (account_id, account_name, account_type)
        VALUES ('ASSET:PORTFOLIO:BBRI', 'BBRI Portfolio', 'ASSET')
        """
    )
    db_connection.commit()
    try:
        yield db_connection
    finally:
        db_connection.close()


def _create_lot(
    connection: sqlite3.Connection,
    *,
    ticker: str = "BBRI",
    acquired_date: str = "2026-01-01",
    original_shares: int,
    remaining_shares: int,
    cost_per_share: float,
    status: str = "OPEN",
) -> str:
    entry_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO journal_entries (
            entry_id, entry_hash, entry_date, trade_date, source_type,
            reference_id, description
        ) VALUES (?, ?, ?, ?, 'MANUAL', ?, 'Split test lot')
        """,
        (
            entry_id,
            f"lot-{entry_id}",
            acquired_date,
            acquired_date,
            entry_id,
        ),
    )

    lot_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO stock_lots (
            lot_id, entry_id, ticker, acquired_date, original_shares,
            remaining_shares, cost_per_share, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lot_id,
            entry_id,
            ticker,
            acquired_date,
            original_shares,
            remaining_shares,
            cost_per_share,
            status,
        ),
    )
    return lot_id


def test_split_records_action_and_adjusts_each_active_lot_with_floor_rounding(
    connection: sqlite3.Connection,
) -> None:
    open_lot_id = _create_lot(
        connection,
        acquired_date="2026-01-01",
        original_shares=2,
        remaining_shares=2,
        cost_per_share=90.0,
    )
    partial_lot_id = _create_lot(
        connection,
        acquired_date="2026-02-01",
        original_shares=10,
        remaining_shares=5,
        cost_per_share=90.0,
        status="PARTIALLY_CLOSED",
    )
    closed_lot_id = _create_lot(
        connection,
        acquired_date="2025-12-01",
        original_shares=10,
        remaining_shares=0,
        cost_per_share=80.0,
        status="CLOSED",
    )
    other_ticker_lot_id = _create_lot(
        connection,
        ticker="TLKM",
        original_shares=10,
        remaining_shares=10,
        cost_per_share=70.0,
    )
    connection.commit()

    result = process_stock_split(
        connection,
        ticker="bbri",
        multiplier=2,
        divisor=3,
        execution_date="2026-03-01",
        notes="2-for-3 reverse split",
    )
    connection.commit()

    assert result["ticker"] == "BBRI"
    assert result["multiplier"] == 2
    assert result["divisor"] == 3
    assert result["total_fractional_lost"] == pytest.approx(2 / 3)
    assert [
        (lot["lot_id"], lot["remaining_shares"], lot["cost_per_share"])
        for lot in result["adjusted_lots"]
    ] == [
        (open_lot_id, 1, 135.0),
        (partial_lot_id, 3, 135.0),
    ]

    action = connection.execute(
        """
        SELECT ticker, action_type, execution_date, ratio_multiplier,
               ratio_divisor, notes
        FROM corporate_actions WHERE action_id = ?
        """,
        (result["action_id"],),
    ).fetchone()
    assert tuple(action) == (
        "BBRI",
        "STOCK_SPLIT",
        "2026-03-01",
        2,
        3,
        "2-for-3 reverse split",
    )

    lot_rows = connection.execute(
        """
        SELECT lot_id, remaining_shares, cost_per_share,
               split_adjustment_factor
        FROM stock_lots
        WHERE lot_id IN (?, ?, ?, ?)
        """,
        (open_lot_id, partial_lot_id, closed_lot_id, other_ticker_lot_id),
    ).fetchall()
    lots_by_id = {row["lot_id"]: row for row in lot_rows}
    assert lots_by_id[open_lot_id]["remaining_shares"] == 1
    assert lots_by_id[partial_lot_id]["remaining_shares"] == 3
    assert lots_by_id[open_lot_id]["cost_per_share"] == pytest.approx(135.0)
    assert lots_by_id[partial_lot_id]["cost_per_share"] == pytest.approx(135.0)
    assert lots_by_id[open_lot_id]["split_adjustment_factor"] == pytest.approx(2 / 3)
    assert tuple(lots_by_id[closed_lot_id][key] for key in (
        "remaining_shares",
        "cost_per_share",
    )) == (0, 80.0)
    assert tuple(lots_by_id[other_ticker_lot_id][key] for key in (
        "remaining_shares",
        "cost_per_share",
    )) == (10, 70.0)


def test_fractional_lost_shares_are_aggregated_into_balanced_rounding_journal(
    connection: sqlite3.Connection,
) -> None:
    _create_lot(
        connection,
        original_shares=2,
        remaining_shares=2,
        cost_per_share=90.0,
    )
    _create_lot(
        connection,
        acquired_date="2026-02-01",
        original_shares=5,
        remaining_shares=5,
        cost_per_share=90.0,
    )
    connection.commit()

    result = process_stock_split(
        connection,
        ticker="BBRI",
        multiplier=2,
        divisor=3,
        execution_date="2026-03-02",
    )
    connection.commit()

    adjustment_entry = connection.execute(
        """
        SELECT entry_id FROM journal_entries
        WHERE source_type = 'CORP_ACTION'
          AND reference_id = 'CORP-SPLIT-BBRI'
        """
    ).fetchone()
    assert adjustment_entry is not None

    legs = connection.execute(
        """
        SELECT account_id, debit_amount, credit_amount
        FROM journal_legs WHERE entry_id = ?
        ORDER BY account_id
        """,
        (adjustment_entry["entry_id"],),
    ).fetchall()
    assert [tuple(leg) for leg in legs] == [
        ("ASSET:PORTFOLIO:BBRI", 0, 90),
        ("EXPENSE:CORPORATE_ACTION_ROUNDING", 90, 0),
    ]
    assert sum(leg["debit_amount"] for leg in legs) == sum(
        leg["credit_amount"] for leg in legs
    )
    assert result["total_fractional_lost"] == pytest.approx(2 / 3)


def test_exact_split_records_action_without_rounding_journal(
    connection: sqlite3.Connection,
) -> None:
    _create_lot(
        connection,
        original_shares=10,
        remaining_shares=10,
        cost_per_share=50.0,
    )
    connection.commit()

    result = process_stock_split(
        connection,
        ticker="BBRI",
        multiplier=2,
        divisor=1,
        execution_date="2026-03-03",
    )
    connection.commit()

    assert result["total_fractional_lost"] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM corporate_actions WHERE action_id = ?",
        (result["action_id"],),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE source_type = 'CORP_ACTION'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT remaining_shares, cost_per_share FROM stock_lots"
    ).fetchone()[:] == (20, 25.0)


def test_split_with_no_active_ticker_lots_still_records_action(
    connection: sqlite3.Connection,
) -> None:
    closed_lot_id = _create_lot(
        connection,
        original_shares=10,
        remaining_shares=0,
        cost_per_share=50.0,
        status="CLOSED",
    )
    connection.commit()

    result = process_stock_split(
        connection,
        ticker="BBRI",
        multiplier=3,
        divisor=2,
        execution_date="2026-03-04",
    )
    connection.commit()

    assert result["adjusted_lots"] == []
    assert result["total_fractional_lost"] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM corporate_actions WHERE action_id = ?",
        (result["action_id"],),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT remaining_shares, cost_per_share FROM stock_lots WHERE lot_id = ?",
        (closed_lot_id,),
    ).fetchone()[:] == (0, 50.0)
    assert connection.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE source_type = 'CORP_ACTION'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("multiplier", "divisor"),
    [
        (0, 1),
        (-1, 1),
        (1, 0),
        (1, -1),
        (1.5, 1),
        (1, 1.5),
        (True, 1),
        (1, False),
    ],
)
def test_split_rejects_non_positive_or_non_integer_ratios_without_changes(
    connection: sqlite3.Connection,
    multiplier: Any,
    divisor: Any,
) -> None:
    _create_lot(
        connection,
        original_shares=10,
        remaining_shares=10,
        cost_per_share=50.0,
    )
    connection.commit()

    with pytest.raises(CorporateActionError):
        process_stock_split(
            connection,
            ticker="BBRI",
            multiplier=multiplier,
            divisor=divisor,
            execution_date="2026-03-05",
        )

    assert connection.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 0
    assert connection.execute(
        "SELECT remaining_shares, cost_per_share FROM stock_lots"
    ).fetchone()[:] == (10, 50.0)


def test_failed_rounding_journal_leaves_lots_and_action_unchanged(
    connection: sqlite3.Connection,
) -> None:
    lot_id = _create_lot(
        connection,
        original_shares=2,
        remaining_shares=2,
        cost_per_share=90.0,
    )
    connection.commit()
    connection.execute(
        """
        CREATE TRIGGER fail_corporate_action_journal
        BEFORE INSERT ON journal_entries
        WHEN NEW.source_type = 'CORP_ACTION'
        BEGIN
            SELECT RAISE(ABORT, 'injected journal failure');
        END
        """
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected journal failure"):
        process_stock_split(
            connection,
            ticker="BBRI",
            multiplier=2,
            divisor=3,
            execution_date="2026-03-06",
        )

    lot = connection.execute(
        "SELECT remaining_shares, cost_per_share FROM stock_lots WHERE lot_id = ?",
        (lot_id,),
    ).fetchone()
    assert tuple(lot) == (2, 90.0)
    assert connection.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE source_type = 'CORP_ACTION'"
    ).fetchone()[0] == 0
