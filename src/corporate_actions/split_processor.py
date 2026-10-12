from __future__ import annotations

import uuid
from math import floor
from typing import Any


class CorporateActionError(ValueError):
    pass


def process_stock_split(connection, *, ticker: str, multiplier: int, divisor: int, execution_date: str, notes: str | None = None) -> dict[str, Any]:
    if multiplier <= 0 or divisor <= 0:
        raise CorporateActionError("multiplier and divisor must be positive integers.")

    rows = connection.execute(
        """
        SELECT lot_id, ticker, acquired_date, original_shares, remaining_shares, cost_per_share, split_adjustment_factor, status
        FROM stock_lots
        WHERE ticker = ? AND status IN ('OPEN', 'PARTIALLY_CLOSED')
        ORDER BY acquired_date ASC, lot_id ASC
        """,
        (ticker.upper(),),
    ).fetchall()

    total_fractional_lost = 0
    adjusted_lot_rows = []

    for row in rows:
        remaining = int(row[4])
        adjusted_shares = floor(remaining * (multiplier / divisor))
        fractional_remainder = (remaining * (multiplier / divisor)) - adjusted_shares
        adjusted_cost = float(row[5]) * (divisor / multiplier)

        total_fractional_lost += fractional_remainder
        adjusted_lot_rows.append({
            "lot_id": row[0],
            "remaining_shares": adjusted_shares,
            "cost_per_share": adjusted_cost,
            "fractional_remainder": fractional_remainder,
        })

    action_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO corporate_actions (action_id, ticker, action_type, execution_date, ratio_multiplier, ratio_divisor, notes) VALUES (?, ?, 'STOCK_SPLIT', ?, ?, ?, ?)",
        (action_id, ticker.upper(), execution_date, multiplier, divisor, notes or f"{multiplier}:{divisor} split"),
    )

    for entry in adjusted_lot_rows:
        connection.execute(
            "UPDATE stock_lots SET remaining_shares = ?, cost_per_share = ?, split_adjustment_factor = ?, status = CASE WHEN remaining_shares = 0 THEN 'CLOSED' ELSE 'OPEN' END WHERE lot_id = ?",
            (entry["remaining_shares"], entry["cost_per_share"], multiplier / divisor, entry["lot_id"]),
        )

    if total_fractional_lost > 0:
        rounding_amount = int(total_fractional_lost * (sum(row[5] for row in rows) / len(rows) if rows else 0))
        entry_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO journal_entries (entry_id, entry_hash, entry_date, trade_date, source_type, reference_id, description) VALUES (?, ?, ?, ?, 'CORP_ACTION', ?, ?)",
            (entry_id, f"corp-split-{ticker.upper()}-{execution_date}", execution_date, execution_date, f"CORP-SPLIT-{ticker.upper()}", f"Corporate action rounding for {ticker.upper()}"),
        )
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, 'EXPENSE:CORPORATE_ACTION_ROUNDING', ?, 0)",
            (str(uuid.uuid4()), entry_id, rounding_amount),
        )
        connection.execute(
            "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, ?, 0, ?)",
            (str(uuid.uuid4()), entry_id, f"ASSET:PORTFOLIO:{ticker.upper()}", rounding_amount),
        )

    return {
        "action_id": action_id,
        "ticker": ticker.upper(),
        "multiplier": multiplier,
        "divisor": divisor,
        "total_fractional_lost": total_fractional_lost,
        "adjusted_lots": adjusted_lot_rows,
    }
