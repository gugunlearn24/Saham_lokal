from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.db.connection import get_connection
from src.db.migrator import run_migrations
from src.views.dashboard_presenter import present_dashboard
from src.views.portfolio_view_service import (
    build_portfolio_dashboard,
    get_active_positions,
    get_pending_clearance_positions,
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


def _insert_lot(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    acquired_date: str,
    shares: int,
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
        ) VALUES (?, ?, ?, ?, 'MANUAL', ?, 'Dashboard lot')
        """,
        (
            entry_id,
            f"dashboard-lot-{entry_id}",
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
            shares,
            remaining_shares,
            cost_per_share,
            status,
        ),
    )
    return lot_id


def _insert_price(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    price_date: str,
    raw_close: float,
    adjusted_close: float,
    fetched_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO market_prices_eod (
            ticker, price_date, raw_close, adjusted_close, volume, fetched_at
        ) VALUES (?, ?, ?, ?, 1000, ?)
        """,
        (ticker, price_date, raw_close, adjusted_close, fetched_at),
    )


def _insert_pending_trade(
    connection: sqlite3.Connection,
    *,
    staging_id: str,
    source_type: str = "BROKER",
    ticker: str | None = "BBRI",
    shares: int | None = 100,
    net_cash_amount: int = -1_000,
    status: str = "PENDING",
) -> None:
    connection.execute(
        """
        INSERT INTO reconciliation_staging (
            staging_id, source_type, external_reference, trade_date, ticker,
            action, shares, price_per_share, net_cash_amount, raw_payload,
            reconciliation_status
        ) VALUES (?, ?, ?, '2026-10-10', ?, 'BUY', ?, 10.0, ?, ?, ?)
        """,
        (
            staging_id,
            source_type,
            staging_id,
            ticker,
            shares,
            net_cash_amount,
            json.dumps({"staging_id": staging_id}),
            status,
        ),
    )


def _insert_evaluation(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    evaluation_date: str,
    fundamental_status: str,
    technical_status: str,
    overall_signal: str,
) -> None:
    connection.execute(
        """
        INSERT INTO evaluation_snapshots (
            snapshot_id, evaluation_date, ticker, technical_status,
            fundamental_status, overall_signal, metrics_evaluated_json
        ) VALUES (?, ?, ?, ?, ?, ?, '{}')
        """,
        (
            str(uuid.uuid4()),
            evaluation_date,
            ticker,
            technical_status,
            fundamental_status,
            overall_signal,
        ),
    )


def test_active_positions_report_total_shares_fifo_average_cost_latest_price_and_pnl(
    connection: sqlite3.Connection,
) -> None:
    _insert_lot(
        connection,
        ticker="BBRI",
        acquired_date="2026-01-01",
        shares=100,
        remaining_shares=100,
        cost_per_share=10.0,
    )
    _insert_lot(
        connection,
        ticker="BBRI",
        acquired_date="2026-02-01",
        shares=100,
        remaining_shares=50,
        cost_per_share=20.0,
        status="PARTIALLY_CLOSED",
    )
    _insert_lot(
        connection,
        ticker="BBRI",
        acquired_date="2025-12-01",
        shares=100,
        remaining_shares=0,
        cost_per_share=5.0,
        status="CLOSED",
    )
    _insert_price(
        connection,
        ticker="BBRI",
        price_date="2026-10-09",
        raw_close=28.0,
        adjusted_close=29.0,
        fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    connection.commit()

    positions = get_active_positions(connection)

    assert len(positions) == 1
    position = positions[0]
    assert position["ticker"] == "BBRI"
    assert position["total_shares"] == 150
    assert position["average_cost_per_share"] == pytest.approx(40 / 3)
    assert position["latest_price"] == pytest.approx(29.0)
    assert position["price_date"] == "2026-10-09"
    assert position["market_value"] == pytest.approx(4_350.0)
    assert position["unrealized_pnl"] == pytest.approx(2_350.0)
    assert position["stale_data_warning"] is False


def test_dashboard_separates_pending_clearance_from_settled_nav_and_aggregates_pending(
    connection: sqlite3.Connection,
) -> None:
    _insert_lot(
        connection,
        ticker="BBRI",
        acquired_date="2026-01-01",
        shares=100,
        remaining_shares=100,
        cost_per_share=20.0,
    )
    _insert_price(
        connection,
        ticker="BBRI",
        price_date="2026-10-09",
        raw_close=25.0,
        adjusted_close=24.0,
        fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    _insert_pending_trade(
        connection,
        staging_id="pending-1",
        shares=300,
        net_cash_amount=-7_500,
        status="PENDING",
    )
    _insert_pending_trade(
        connection,
        staging_id="flagged-1",
        shares=200,
        net_cash_amount=-5_000,
        status="FLAGGED",
    )
    _insert_pending_trade(
        connection,
        staging_id="matched-1",
        shares=900,
        net_cash_amount=-20_000,
        status="MATCHED",
    )
    _insert_pending_trade(
        connection,
        staging_id="bank-pending",
        source_type="BANK",
        shares=None,
        net_cash_amount=100_000,
        status="PENDING",
    )
    connection.commit()

    dashboard = build_portfolio_dashboard(connection)

    assert dashboard["nav_total"] == pytest.approx(2_400.0)
    assert dashboard["unrealized_pnl_total"] == pytest.approx(400.0)
    assert len(dashboard["active_positions"]) == 1
    assert len(dashboard["pending_clearance"]) == 1
    pending = dashboard["pending_clearance"][0]
    assert pending == {
        "ticker": "BBRI",
        "record_count": 2,
        "total_shares": 500,
        "total_cash_amount": -12_500,
        "status": "PENDING",
        "warning_label": "Unsettled / Pending RDN",
    }
    assert "Unsettled / Pending RDN" in dashboard["warnings"]


def test_active_position_includes_latest_fundamental_technical_and_action_statuses(
    connection: sqlite3.Connection,
) -> None:
    _insert_lot(
        connection,
        ticker="BBRI",
        acquired_date="2026-01-01",
        shares=100,
        remaining_shares=100,
        cost_per_share=20.0,
    )
    _insert_evaluation(
        connection,
        ticker="BBRI",
        evaluation_date="2026-10-03",
        fundamental_status="PASS",
        technical_status="WARN",
        overall_signal="HOLD",
    )
    _insert_evaluation(
        connection,
        ticker="BBRI",
        evaluation_date="2026-10-10",
        fundamental_status="WARN",
        technical_status="FAIL",
        overall_signal="EXIT",
    )
    connection.commit()

    positions = get_active_positions(connection)

    assert len(positions) == 1
    assert positions[0]["fundamental_status"] == "WARN"
    assert positions[0]["technical_status"] == "FAIL"
    assert positions[0]["overall_signal"] == "EXIT"
    assert positions[0]["evaluation_date"] == "2026-10-10"


def test_dashboard_and_presenter_surface_stale_data_warning(
    connection: sqlite3.Connection,
) -> None:
    _insert_lot(
        connection,
        ticker="BBRI",
        acquired_date="2026-01-01",
        shares=100,
        remaining_shares=100,
        cost_per_share=20.0,
    )
    _insert_price(
        connection,
        ticker="BBRI",
        price_date="2026-01-01",
        raw_close=25.0,
        adjusted_close=24.0,
        fetched_at="2020-01-01 00:00:00",
    )
    connection.commit()

    dashboard = build_portfolio_dashboard(connection)
    presented = present_dashboard(dashboard)

    assert dashboard["stale_data_warning_tickers"] == ["BBRI"]
    assert "Stale Data Warning" in dashboard["warnings"]
    assert presented["primary_view"]["stale_data_warning_tickers"] == ["BBRI"]
    assert "Stale Data Warning" in presented["warnings"]


def test_presenter_keeps_primary_and_pending_views_separate() -> None:
    dashboard = {
        "nav_total": 12_000.0,
        "unrealized_pnl_total": 2_000.0,
        "active_positions": [{"ticker": "BBRI", "total_shares": 100}],
        "pending_clearance": [
            {
                "ticker": "TLKM",
                "total_shares": 50,
                "warning_label": "Unsettled / Pending RDN",
            }
        ],
        "stale_data_warning_tickers": [],
        "warnings": ["Unsettled / Pending RDN", None],
    }

    presented = present_dashboard(dashboard)

    assert presented == {
        "primary_view": {
            "nav_total": 12_000.0,
            "unrealized_pnl_total": 2_000.0,
            "positions": [{"ticker": "BBRI", "total_shares": 100}],
            "stale_data_warning_tickers": [],
        },
        "unsettled_view": {
            "warning_label": "Unsettled / Pending RDN",
            "positions": [
                {
                    "ticker": "TLKM",
                    "total_shares": 50,
                    "warning_label": "Unsettled / Pending RDN",
                }
            ],
        },
        "warnings": ["Unsettled / Pending RDN"],
    }


def test_dashboard_handles_empty_portfolio_and_ignores_pending_rows_without_ticker(
    connection: sqlite3.Connection,
) -> None:
    _insert_pending_trade(
        connection,
        staging_id="pending-no-ticker",
        ticker=None,
    )
    connection.commit()

    dashboard = build_portfolio_dashboard(connection)
    presented = present_dashboard(dashboard)

    assert dashboard["nav_total"] == 0.0
    assert dashboard["unrealized_pnl_total"] == 0.0
    assert dashboard["active_positions"] == []
    assert dashboard["pending_clearance"] == []
    assert dashboard["stale_data_warning_tickers"] == []
    assert dashboard["warnings"] == [None, None]
    assert presented["primary_view"]["positions"] == []
    assert presented["unsettled_view"]["positions"] == []
    assert presented["warnings"] == []


def test_pending_clearance_summary_excludes_rows_with_unrelated_action_or_status(
    connection: sqlite3.Connection,
) -> None:
    _insert_pending_trade(
        connection,
        staging_id="pending-buy",
        shares=10,
        net_cash_amount=-100,
        status="PENDING",
    )
    connection.execute(
        """
        INSERT INTO reconciliation_staging (
            staging_id, source_type, external_reference, trade_date, ticker,
            action, shares, price_per_share, net_cash_amount, raw_payload,
            reconciliation_status
        ) VALUES (
            'pending-bank-dividend', 'BANK', 'BANK-DIV', '2026-10-10', 'BBRI',
            'DIVIDEND', NULL, NULL, 100, '{}', 'PENDING'
        )
        """
    )
    connection.commit()

    pending = get_pending_clearance_positions(connection)

    assert len(pending) == 1
    assert pending[0]["record_count"] == 1
    assert pending[0]["total_shares"] == 10
