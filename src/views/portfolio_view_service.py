from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any


DEFAULT_STALE_DAYS = 7


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def get_active_positions(connection) -> list[dict[str, Any]]:
    """Return all active portfolio positions from stock_lots, grouped by ticker."""
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT
            lots.ticker,
            SUM(lots.remaining_shares) AS total_shares,
            SUM(lots.remaining_shares * lots.cost_per_share) AS total_cost_basis,
            MAX(CAST(strftime('%Y-%m-%d', lots.acquired_date) AS TEXT)) AS latest_acquired_date,
            evaluation.evaluation_date,
            evaluation.fundamental_status,
            evaluation.technical_status,
            evaluation.overall_signal
        FROM stock_lots AS lots
        LEFT JOIN evaluation_snapshots AS evaluation
            ON evaluation.ticker = lots.ticker
            AND evaluation.evaluation_date = (
                SELECT MAX(latest.evaluation_date)
                FROM evaluation_snapshots AS latest
                WHERE latest.ticker = lots.ticker
            )
        WHERE lots.status IN ('OPEN', 'PARTIALLY_CLOSED')
        GROUP BY lots.ticker
        ORDER BY lots.ticker ASC
        """
    ).fetchall()

    positions: list[dict[str, Any]] = []
    for row in rows:
        ticker = row["ticker"]
        total_shares = _as_int(row["total_shares"], 0)
        total_cost_basis = _safe_float(row["total_cost_basis"], 0.0)
        average_cost = (total_cost_basis / total_shares) if total_shares > 0 else 0.0

        latest_price_row = connection.execute(
            """
            SELECT raw_close, adjusted_close, price_date, fetched_at
            FROM market_prices_eod
            WHERE ticker = ?
            ORDER BY price_date DESC, fetched_at DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()

        latest_price = 0.0
        price_date = None
        stale_warning = False
        if latest_price_row is not None:
            latest_price = _safe_float(latest_price_row["adjusted_close"], _safe_float(latest_price_row["raw_close"], 0.0))
            price_date = latest_price_row["price_date"]
            fetched_at = latest_price_row["fetched_at"]
            if fetched_at is not None:
                try:
                    fetched_dt = datetime.strptime(str(fetched_at), "%Y-%m-%d %H:%M:%S")
                    stale_warning = (date.today() - fetched_dt.date()).days > DEFAULT_STALE_DAYS
                except ValueError:
                    stale_warning = False

        market_value = latest_price * total_shares
        unrealized_pnl = market_value - total_cost_basis

        positions.append(
            {
                "ticker": ticker,
                "total_shares": total_shares,
                "average_cost_per_share": average_cost,
                "latest_price": latest_price,
                "price_date": price_date,
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "stale_data_warning": stale_warning,
                "evaluation_date": row["evaluation_date"],
                "fundamental_status": row["fundamental_status"],
                "technical_status": row["technical_status"],
                "overall_signal": row["overall_signal"],
            }
        )

    return positions


def get_pending_clearance_positions(connection) -> list[dict[str, Any]]:
    """Return unsettled broker rows in reconciliation_staging as a separate pending-clearance view."""
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT
            ticker,
            COUNT(*) AS record_count,
            SUM(CASE WHEN shares IS NOT NULL THEN shares ELSE 0 END) AS total_shares,
            SUM(net_cash_amount) AS total_cash_amount,
            MAX(reconciliation_status) AS status
        FROM reconciliation_staging
        WHERE source_type = 'BROKER' AND reconciliation_status IN ('PENDING', 'FLAGGED')
        GROUP BY ticker
        ORDER BY ticker ASC
        """
    ).fetchall()

    pending: list[dict[str, Any]] = []
    for row in rows:
        ticker = row["ticker"]
        if ticker is None:
            continue
        pending.append(
            {
                "ticker": ticker,
                "record_count": _as_int(row["record_count"], 0),
                "total_shares": _as_int(row["total_shares"], 0),
                "total_cash_amount": _as_int(row["total_cash_amount"], 0),
                "status": row["status"] or "PENDING",
                "warning_label": "Unsettled / Pending RDN",
            }
        )
    return pending


def build_portfolio_dashboard(connection) -> dict[str, Any]:
    """Build the aggregate portfolio dashboard with the active and pending views separated."""
    active_positions = get_active_positions(connection)
    pending_positions = get_pending_clearance_positions(connection)
    total_nav = sum(float(position["market_value"]) for position in active_positions)
    total_unrealized_pnl = sum(float(position["unrealized_pnl"]) for position in active_positions)

    stale_warnings = [
        position["ticker"]
        for position in active_positions
        if position.get("stale_data_warning")
    ]

    return {
        "nav_total": total_nav,
        "unrealized_pnl_total": total_unrealized_pnl,
        "active_positions": active_positions,
        "pending_clearance": pending_positions,
        "stale_data_warning_tickers": stale_warnings,
        "warnings": [
            f"Unsettled / Pending RDN" if pending_positions else None,
            "Stale Data Warning" if stale_warnings else None,
        ],
    }
