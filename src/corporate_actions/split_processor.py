from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any


class CorporateActionError(ValueError):
    pass


def process_stock_split(
    connection,
    *,
    ticker: str,
    multiplier: int,
    divisor: int,
    execution_date: str,
    notes: str | None = None,
) -> dict[str, Any]:
    if (
        type(multiplier) is not int
        or type(divisor) is not int
        or multiplier <= 0
        or divisor <= 0
    ):
        raise CorporateActionError("multiplier and divisor must be positive integers.")

    normalized_ticker = ticker.upper()
    savepoint = f"split_{uuid.uuid4().hex}"
    connection.execute(f"SAVEPOINT {savepoint}")

    try:
        rows = connection.execute(
            """
            SELECT lot_id, ticker, acquired_date, original_shares, remaining_shares,
                   cost_per_share, split_adjustment_factor, status
            FROM stock_lots
            WHERE ticker = ? AND status IN ('OPEN', 'PARTIALLY_CLOSED')
            ORDER BY acquired_date ASC, lot_id ASC
            """,
            (normalized_ticker,),
        ).fetchall()

        ratio = Fraction(multiplier, divisor)
        total_fractional_lost = Fraction(0)
        rounding_value = Decimal(0)
        adjusted_lot_rows: list[dict[str, Any]] = []

        for row in rows:
            remaining = int(row[4])
            adjusted_share_count = Fraction(remaining) * ratio
            adjusted_shares = adjusted_share_count.numerator // adjusted_share_count.denominator
            fractional_remainder = adjusted_share_count - adjusted_shares
            adjusted_cost = (
                Decimal(str(row[5])) * Decimal(divisor) / Decimal(multiplier)
            )

            total_fractional_lost += fractional_remainder
            rounding_value += (
                Decimal(fractional_remainder.numerator)
                / Decimal(fractional_remainder.denominator)
                * adjusted_cost
            )
            adjusted_lot_rows.append(
                {
                    "lot_id": row[0],
                    "remaining_shares": adjusted_shares,
                    "cost_per_share": float(adjusted_cost),
                    "fractional_remainder": float(fractional_remainder),
                }
            )

        action_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO corporate_actions (
                action_id, ticker, action_type, execution_date,
                ratio_multiplier, ratio_divisor, notes
            ) VALUES (?, ?, 'STOCK_SPLIT', ?, ?, ?, ?)
            """,
            (
                action_id,
                normalized_ticker,
                execution_date,
                multiplier,
                divisor,
                notes or f"{multiplier}:{divisor} split",
            ),
        )

        for entry in adjusted_lot_rows:
            connection.execute(
                """
                UPDATE stock_lots
                SET remaining_shares = ?, cost_per_share = ?,
                    split_adjustment_factor = split_adjustment_factor * ?,
                    status = CASE
                        WHEN ? = 0 THEN 'CLOSED'
                        WHEN status = 'PARTIALLY_CLOSED' THEN 'PARTIALLY_CLOSED'
                        ELSE 'OPEN'
                    END
                WHERE lot_id = ?
                """,
                (
                    entry["remaining_shares"],
                    entry["cost_per_share"],
                    multiplier / divisor,
                    entry["remaining_shares"],
                    entry["lot_id"],
                ),
            )

        if total_fractional_lost > 0:
            rounding_amount = int(
                rounding_value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
            if rounding_amount <= 0:
                raise CorporateActionError(
                    "Fractional-share rounding amount must be at least one IDR."
                )

            entry_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO journal_entries (
                    entry_id, entry_hash, entry_date, trade_date, source_type,
                    reference_id, description
                ) VALUES (?, ?, ?, ?, 'CORP_ACTION', ?, ?)
                """,
                (
                    entry_id,
                    f"corp-split-{normalized_ticker}-{execution_date}",
                    execution_date,
                    execution_date,
                    f"CORP-SPLIT-{normalized_ticker}",
                    f"Corporate action rounding for {normalized_ticker}",
                ),
            )
            connection.execute(
                """
                INSERT INTO journal_legs (
                    leg_id, entry_id, account_id, debit_amount, credit_amount
                ) VALUES (?, ?, 'EXPENSE:CORPORATE_ACTION_ROUNDING', ?, 0)
                """,
                (str(uuid.uuid4()), entry_id, rounding_amount),
            )
            connection.execute(
                """
                INSERT INTO journal_legs (
                    leg_id, entry_id, account_id, debit_amount, credit_amount
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (
                    str(uuid.uuid4()),
                    entry_id,
                    f"ASSET:PORTFOLIO:{normalized_ticker}",
                    rounding_amount,
                ),
            )

        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise

    return {
        "action_id": action_id,
        "ticker": normalized_ticker,
        "multiplier": multiplier,
        "divisor": divisor,
        "total_fractional_lost": float(total_fractional_lost),
        "adjusted_lots": adjusted_lot_rows,
    }
