from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.db.connection import get_connection
from src.db.migrator import run_migrations

EXPECTED_TABLES = {
    "accounts",
    "journal_entries",
    "journal_legs",
    "stock_lots",
    "lot_disposals",
    "corporate_actions",
    "market_prices_eod",
    "fundamental_snapshots",
    "evaluation_snapshots",
    "reconciliation_staging",
}

EXPECTED_INDEXES = {
    "idx_lots_ticker_status",
    "idx_journal_legs_account",
    "idx_market_prices_lookup",
    "idx_eval_date_ticker",
}

EXPECTED_ACCOUNT_IDS = {
    "ASSET:RDN_CASH",
    "EXPENSE:BROKER_FEE",
    "EXPENSE:EXCHANGE_LEVY",
    "EXPENSE:TAX_PPH_FINAL",
    "EXPENSE:CORPORATE_ACTION_ROUNDING",
    "REVENUE:REALIZED_GAIN_LOSS",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "nested" / "portfolio.db"


def test_connection_enables_foreign_keys_and_wal(db_path: Path) -> None:
    connection = get_connection(db_path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert isinstance(connection.execute("SELECT 1").fetchone(), sqlite3.Row)
        assert db_path.parent.is_dir()
    finally:
        connection.close()


def test_migration_creates_all_required_tables_and_indexes(db_path: Path) -> None:
    applied = run_migrations(db_path)

    assert applied == ["001_initial_schema.sql"]

    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

        assert EXPECTED_TABLES <= tables
        assert EXPECTED_INDEXES <= indexes
    finally:
        connection.close()


def test_first_migration_seeds_standard_chart_of_accounts(db_path: Path) -> None:
    run_migrations(db_path)

    connection = sqlite3.connect(db_path)
    try:
        seeded_accounts = {
            row[0]
            for row in connection.execute("SELECT account_id FROM accounts")
        }

        assert EXPECTED_ACCOUNT_IDS <= seeded_accounts
    finally:
        connection.close()


def test_migration_is_idempotent_when_run_again(db_path: Path) -> None:
    first_run = run_migrations(db_path)
    second_run = run_migrations(db_path)

    assert second_run == first_run

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 6
    finally:
        connection.close()


def test_failed_migration_rolls_back_all_migration_scripts(
    tmp_path: Path, db_path: Path
) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    (migration_dir / "001_create_first_table.sql").write_text(
        "CREATE TABLE first_migration_table (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    (migration_dir / "002_fail_after_ddl.sql").write_text(
        "CREATE TABLE second_migration_table (id INTEGER PRIMARY KEY);"
        "THIS IS NOT VALID SQL;",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, migration_dir)

    connection = sqlite3.connect(db_path)
    try:
        created_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "first_migration_table" not in created_tables
        assert "second_migration_table" not in created_tables
    finally:
        connection.close()


def test_schema_enforces_foreign_keys_and_data_constraints(db_path: Path) -> None:
    run_migrations(db_path)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO journal_legs (
                    leg_id, entry_id, account_id, debit_amount, credit_amount
                ) VALUES ('leg-1', 'missing-entry', 'ASSET:RDN_CASH', 100, 0)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO corporate_actions (
                    action_id, ticker, action_type, execution_date,
                    ratio_multiplier, ratio_divisor
                ) VALUES ('action-1', 'BBRI', 'STOCK_SPLIT', '2026-01-01', 0, 1)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO fundamental_snapshots (
                    ticker, fiscal_year, fiscal_quarter, report_release_date
                ) VALUES ('BBRI', 2026, 5, '2026-01-01')
                """
            )
    finally:
        connection.close()
