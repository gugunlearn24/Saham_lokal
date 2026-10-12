from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from src.db.connection import get_connection


class FIFOValidationError(ValueError):
    pass


def create_stock_lot(connection, *, entry_id: str, ticker: str, acquired_date: str, original_shares: int, cost_per_share: float, split_adjustment_factor: float = 1.0) -> str:
    if original_shares <= 0:
        raise FIFOValidationError("original_shares must be positive.")
    if cost_per_share <= 0:
        raise FIFOValidationError("cost_per_share must be positive.")

    lot_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO stock_lots (
            lot_id, entry_id, ticker, acquired_date, original_shares, remaining_shares,
            cost_per_share, split_adjustment_factor, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """,
        (
            lot_id,
            entry_id,
            ticker.upper(),
            acquired_date,
            original_shares,
            original_shares,
            cost_per_share,
            split_adjustment_factor,
        ),
    )
    return lot_id


def get_open_lots(connection, ticker: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT lot_id, entry_id, ticker, acquired_date, original_shares, remaining_shares, cost_per_share, split_adjustment_factor, status
        FROM stock_lots
        WHERE ticker = ? AND status IN ('OPEN', 'PARTIALLY_CLOSED')
        ORDER BY acquired_date ASC, lot_id ASC
        """,
        (ticker.upper(),),
    ).fetchall()
    return [
        {
            "lot_id": row[0],
            "entry_id": row[1],
            "ticker": row[2],
            "acquired_date": row[3],
            "original_shares": row[4],
            "remaining_shares": row[5],
            "cost_per_share": row[6],
            "split_adjustment_factor": row[7],
            "status": row[8],
        }
        for row in rows
    ]


def liquidate_fifo_lots(connection, *, ticker: str, shares_to_sell: int, disposal_entry_id: str, selling_price_per_share: float, disposal_date: str | None = None) -> list[dict[str, Any]]:
    if shares_to_sell <= 0:
        raise FIFOValidationError("shares_to_sell must be positive.")

    lots = get_open_lots(connection, ticker)
    total_open = sum(int(item["remaining_shares"]) for item in lots)
    if total_open < shares_to_sell:
        raise FIFOValidationError(f"Not enough open shares for {ticker}: requested {shares_to_sell}, available {total_open}.")

    remaining_to_sell = shares_to_sell
    disposal_records: list[dict[str, Any]] = []

    for lot in lots:
        if remaining_to_sell <= 0:
            break

        available = int(lot["remaining_shares"])
        liquidated = min(available, remaining_to_sell)
        cost_basis_per_share = float(lot["cost_per_share"])
        realized_pnl = int(round((selling_price_per_share - cost_basis_per_share) * liquidated))

        new_remaining = available - liquidated
        if new_remaining <= 0:
            new_status = "CLOSED"
        else:
            new_status = "PARTIALLY_CLOSED"

        connection.execute(
            "UPDATE stock_lots SET remaining_shares = ?, status = ? WHERE lot_id = ?",
            (new_remaining, new_status, lot["lot_id"]),
        )

        disposal_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO lot_disposals (
                disposal_id, lot_id, disposal_entry_id, shares_liquidated, cost_basis_per_share,
                selling_price_per_share, realized_pnl, disposal_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                disposal_id,
                lot["lot_id"],
                disposal_entry_id,
                liquidated,
                cost_basis_per_share,
                selling_price_per_share,
                realized_pnl,
                disposal_date or datetime.utcnow().strftime("%Y-%m-%d"),
            ),
        )

        disposal_records.append({
            "lot_id": lot["lot_id"],
            "shares_liquidated": liquidated,
            "cost_basis_per_share": cost_basis_per_share,
            "selling_price_per_share": selling_price_per_share,
            "realized_pnl": realized_pnl,
            "disposal_entry_id": disposal_entry_id,
        })
        remaining_to_sell -= liquidated

    if remaining_to_sell != 0:
        raise FIFOValidationError("FIFO liquidation did not consume all requested shares.")

    return disposal_records
