from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from src.db.connection import get_connection
from src.db.migrator import run_migrations
from src.portfolio.disposal_service import dispose_lots
from src.portfolio.fifo_lot_engine import (
    FIFOValidationError,
    create_stock_lot,
    get_open_lots,
    liquidate_fifo_lots,
)


@pytest.fixture
def connection(tmp_path: Path):
    db_path = tmp_path / "portfolio.db"
    run_migrations(db_path)
    db_connection = get_connection(db_path)
    try:
        yield db_connection
    finally:
        db_connection.close()


def _create_source_journal(
    connection: sqlite3.Connection, *, entry_id: str | None = None
) -> str:
    source_entry_id = entry_id or str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO journal_entries (
            entry_id, entry_hash, entry_date, trade_date, source_type,
            reference_id, description
        ) VALUES (?, ?, '2026-10-12', '2026-10-12', 'MANUAL', ?, 'Lot test source')
        """,
        (source_entry_id, f"hash-{source_entry_id}", source_entry_id),
    )
    connection.commit()
    return source_entry_id


def _create_lot(
    connection: sqlite3.Connection,
    *,
    acquired_date: str,
    shares: int,
    cost_per_share: float,
    ticker: str = "BBRI",
) -> str:
    entry_id = _create_source_journal(connection)
    return create_stock_lot(
        connection,
        entry_id=entry_id,
        ticker=ticker,
        acquired_date=acquired_date,
        original_shares=shares,
        cost_per_share=cost_per_share,
    )


def test_creates_new_settled_purchase_lot_open_with_full_remaining_quantity(
    connection: sqlite3.Connection,
) -> None:
    source_entry_id = _create_source_journal(connection)

    lot_id = create_stock_lot(
        connection,
        entry_id=source_entry_id,
        ticker="bbri",
        acquired_date="2026-10-10",
        original_shares=250,
        cost_per_share=5_500.0,
    )
    connection.commit()

    lot = connection.execute(
        """
        SELECT entry_id, ticker, acquired_date, original_shares,
               remaining_shares, cost_per_share, status
        FROM stock_lots WHERE lot_id = ?
        """,
        (lot_id,),
    ).fetchone()
    assert tuple(lot) == (
        source_entry_id,
        "BBRI",
        "2026-10-10",
        250,
        250,
        5_500.0,
        "OPEN",
    )


@pytest.mark.parametrize(
    ("original_shares", "cost_per_share", "message"),
    [
        (0, 100.0, "original_shares must be positive"),
        (-1, 100.0, "original_shares must be positive"),
        (10, 0.0, "cost_per_share must be positive"),
        (10, -1.0, "cost_per_share must be positive"),
    ],
)
def test_rejects_lot_with_non_positive_shares_or_cost(
    connection: sqlite3.Connection,
    original_shares: int,
    cost_per_share: float,
    message: str,
) -> None:
    source_entry_id = _create_source_journal(connection)

    with pytest.raises(FIFOValidationError, match=message):
        create_stock_lot(
            connection,
            entry_id=source_entry_id,
            ticker="BBRI",
            acquired_date="2026-10-10",
            original_shares=original_shares,
            cost_per_share=cost_per_share,
        )

    assert connection.execute("SELECT COUNT(*) FROM stock_lots").fetchone()[0] == 0


def test_get_open_lots_orders_oldest_first_and_excludes_closed_lots(
    connection: sqlite3.Connection,
) -> None:
    oldest_id = _create_lot(
        connection,
        acquired_date="2026-01-01",
        shares=100,
        cost_per_share=10.0,
    )
    middle_id = _create_lot(
        connection,
        acquired_date="2026-02-01",
        shares=100,
        cost_per_share=12.0,
    )
    closed_id = _create_lot(
        connection,
        acquired_date="2025-12-01",
        shares=100,
        cost_per_share=8.0,
    )
    connection.execute(
        "UPDATE stock_lots SET status = 'CLOSED', remaining_shares = 0 WHERE lot_id = ?",
        (closed_id,),
    )
    connection.execute(
        "UPDATE stock_lots SET status = 'PARTIALLY_CLOSED', remaining_shares = 40 WHERE lot_id = ?",
        (middle_id,),
    )
    connection.commit()

    open_lots = get_open_lots(connection, "bbri")

    assert [lot["lot_id"] for lot in open_lots] == [oldest_id, middle_id]
    assert [lot["remaining_shares"] for lot in open_lots] == [100, 40]
    assert all(lot["ticker"] == "BBRI" for lot in open_lots)


def test_partial_sale_liquidates_oldest_lot_first_and_records_disposal(
    connection: sqlite3.Connection,
) -> None:
    oldest_id = _create_lot(
        connection,
        acquired_date="2026-01-01",
        shares=100,
        cost_per_share=10.0,
    )
    newer_id = _create_lot(
        connection,
        acquired_date="2026-02-01",
        shares=100,
        cost_per_share=12.0,
    )
    disposal_entry_id = _create_source_journal(connection)

    records = liquidate_fifo_lots(
        connection,
        ticker="BBRI",
        shares_to_sell=60,
        disposal_entry_id=disposal_entry_id,
        selling_price_per_share=15.0,
        disposal_date="2026-03-01",
    )
    connection.commit()

    assert len(records) == 1
    assert records[0] == {
        "lot_id": oldest_id,
        "shares_liquidated": 60,
        "cost_basis_per_share": 10.0,
        "selling_price_per_share": 15.0,
        "realized_pnl": 300,
        "disposal_entry_id": disposal_entry_id,
    }

    statuses = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT lot_id, remaining_shares, status FROM stock_lots"
        )
    }
    assert statuses == {
        oldest_id: (40, "PARTIALLY_CLOSED"),
        newer_id: (100, "OPEN"),
    }

    disposal = connection.execute(
        """
        SELECT lot_id, disposal_entry_id, shares_liquidated, cost_basis_per_share,
               selling_price_per_share, realized_pnl, disposal_date
        FROM lot_disposals
        """
    ).fetchone()
    assert tuple(disposal) == (
        oldest_id,
        disposal_entry_id,
        60,
        10.0,
        15.0,
        300,
        "2026-03-01",
    )


def test_full_sale_consumes_oldest_lots_before_newer_lots(
    connection: sqlite3.Connection,
) -> None:
    oldest_id = _create_lot(
        connection,
        acquired_date="2026-01-01",
        shares=100,
        cost_per_share=10.0,
    )
    newer_id = _create_lot(
        connection,
        acquired_date="2026-02-01",
        shares=80,
        cost_per_share=12.0,
    )
    latest_id = _create_lot(
        connection,
        acquired_date="2026-03-01",
        shares=100,
        cost_per_share=14.0,
    )
    disposal_entry_id = _create_source_journal(connection)

    records = dispose_lots(
        connection,
        ticker="BBRI",
        shares_to_sell=150,
        disposal_entry_id=disposal_entry_id,
        selling_price_per_share=13.0,
        disposal_date="2026-04-01",
    )
    connection.commit()

    assert [(record["lot_id"], record["shares_liquidated"]) for record in records] == [
        (oldest_id, 100),
        (newer_id, 50),
    ]
    assert [(record["cost_basis_per_share"], record["realized_pnl"]) for record in records] == [
        (10.0, 300),
        (12.0, 50),
    ]

    statuses = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT lot_id, remaining_shares, status FROM stock_lots"
        )
    }
    assert statuses == {
        oldest_id: (0, "CLOSED"),
        newer_id: (30, "PARTIALLY_CLOSED"),
        latest_id: (100, "OPEN"),
    }
    assert connection.execute("SELECT COUNT(*) FROM lot_disposals").fetchone()[0] == 2


def test_equal_acquisition_dates_use_stable_lot_id_order(
    connection: sqlite3.Connection,
) -> None:
    source_entry_id = _create_source_journal(connection)
    connection.execute(
        """
        INSERT INTO stock_lots (
            lot_id, entry_id, ticker, acquired_date, original_shares,
            remaining_shares, cost_per_share, status
        ) VALUES
            ('lot-b', ?, 'BBRI', '2026-01-01', 10, 10, 20, 'OPEN'),
            ('lot-a', ?, 'BBRI', '2026-01-01', 10, 10, 10, 'OPEN')
        """,
        (source_entry_id, source_entry_id),
    )
    disposal_entry_id = _create_source_journal(connection)

    records = liquidate_fifo_lots(
        connection,
        ticker="BBRI",
        shares_to_sell=5,
        disposal_entry_id=disposal_entry_id,
        selling_price_per_share=30.0,
        disposal_date="2026-02-01",
    )

    assert [(record["lot_id"], record["cost_basis_per_share"]) for record in records] == [
        ("lot-a", 10.0)
    ]


@pytest.mark.parametrize("shares_to_sell", [0, -1])
def test_liquidation_rejects_non_positive_share_count(
    connection: sqlite3.Connection, shares_to_sell: int
) -> None:
    _create_lot(
        connection,
        acquired_date="2026-01-01",
        shares=10,
        cost_per_share=10.0,
    )
    disposal_entry_id = _create_source_journal(connection)

    with pytest.raises(FIFOValidationError, match="shares_to_sell must be positive"):
        liquidate_fifo_lots(
            connection,
            ticker="BBRI",
            shares_to_sell=shares_to_sell,
            disposal_entry_id=disposal_entry_id,
            selling_price_per_share=12.0,
        )

    assert connection.execute("SELECT COUNT(*) FROM lot_disposals").fetchone()[0] == 0


def test_sale_larger_than_all_open_lots_is_rejected_without_changes(
    connection: sqlite3.Connection,
) -> None:
    lot_id = _create_lot(
        connection,
        acquired_date="2026-01-01",
        shares=10,
        cost_per_share=10.0,
    )
    disposal_entry_id = _create_source_journal(connection)

    with pytest.raises(FIFOValidationError, match="Not enough open shares"):
        liquidate_fifo_lots(
            connection,
            ticker="BBRI",
            shares_to_sell=11,
            disposal_entry_id=disposal_entry_id,
            selling_price_per_share=12.0,
        )

    lot_state = connection.execute(
        "SELECT remaining_shares, status FROM stock_lots WHERE lot_id = ?",
        (lot_id,),
    ).fetchone()
    assert tuple(lot_state) == (10, "OPEN")
    assert connection.execute("SELECT COUNT(*) FROM lot_disposals").fetchone()[0] == 0


def test_liquidating_closed_or_different_ticker_lots_fails(
    connection: sqlite3.Connection,
) -> None:
    lot_id = _create_lot(
        connection,
        acquired_date="2026-01-01",
        shares=10,
        cost_per_share=10.0,
    )
    other_ticker_id = _create_lot(
        connection,
        acquired_date="2026-01-01",
        shares=10,
        cost_per_share=10.0,
        ticker="TLKM",
    )
    connection.execute(
        "UPDATE stock_lots SET remaining_shares = 0, status = 'CLOSED' WHERE lot_id = ?",
        (lot_id,),
    )
    connection.commit()
    disposal_entry_id = _create_source_journal(connection)

    with pytest.raises(FIFOValidationError, match="Not enough open shares"):
        liquidate_fifo_lots(
            connection,
            ticker="BBRI",
            shares_to_sell=1,
            disposal_entry_id=disposal_entry_id,
            selling_price_per_share=12.0,
        )

    assert connection.execute(
        "SELECT remaining_shares FROM stock_lots WHERE lot_id = ?",
        (other_ticker_id,),
    ).fetchone()[0] == 10
    assert connection.execute("SELECT COUNT(*) FROM lot_disposals").fetchone()[0] == 0


def test_disposal_history_links_valid_lot_and_journal_entries(
    connection: sqlite3.Connection,
) -> None:
    lot_id = _create_lot(
        connection,
        acquired_date="2026-01-01",
        shares=10,
        cost_per_share=10.0,
    )
    disposal_entry_id = _create_source_journal(connection)

    dispose_lots(
        connection,
        ticker="BBRI",
        shares_to_sell=10,
        disposal_entry_id=disposal_entry_id,
        selling_price_per_share=8.0,
        disposal_date="2026-02-01",
    )
    connection.commit()

    disposal = connection.execute(
        """
        SELECT d.lot_id, d.disposal_entry_id, d.shares_liquidated,
               d.cost_basis_per_share, d.selling_price_per_share, d.realized_pnl,
               l.status, l.remaining_shares
        FROM lot_disposals AS d
        JOIN stock_lots AS l ON l.lot_id = d.lot_id
        WHERE d.disposal_entry_id = ?
        """,
        (disposal_entry_id,),
    ).fetchone()

    assert tuple(disposal) == (
        lot_id,
        disposal_entry_id,
        10,
        10.0,
        8.0,
        -20,
        "CLOSED",
        0,
    )
