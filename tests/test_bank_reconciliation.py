from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from src.db.migrator import run_migrations
from src.ingestion.bank_csv_parser import ingest_bank_statement, load_bank_rows
from src.reconciliation.reconciliation_engine import reconcile_trades


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    run_migrations(path)
    return path


def _write_csv(
    path: Path, headers: list[str], rows: list[list[Any]], delimiter: str = ","
) -> Path:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter=delimiter)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def _bank_row(
    *,
    date: str = "2026-10-12",
    debit: str = "1000000",
    credit: str = "",
    description: str = "Settlement",
    reference: str = "BANK-001",
) -> list[str]:
    return [date, debit, credit, description, reference]


def _insert_staging_row(
    connection: sqlite3.Connection,
    *,
    staging_id: str,
    source_type: str,
    trade_date: str,
    amount: int,
    reference: str,
    ticker: str | None = None,
    action: str | None = None,
    shares: int | None = None,
    price: float | None = None,
) -> None:
    payload = json.dumps({"reference_id": reference})
    connection.execute(
        """
        INSERT INTO reconciliation_staging (
            staging_id, source_type, external_reference, trade_date, ticker, action,
            shares, price_per_share, net_cash_amount, raw_payload, reconciliation_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """,
        (
            staging_id,
            source_type,
            reference,
            trade_date,
            ticker,
            action,
            shares,
            price,
            amount,
            payload,
        ),
    )


@pytest.mark.parametrize(
    ("debit", "credit", "expected_amount", "expected_action"),
    [
        ("1000000", "", -1000000, "WITHDRAW"),
        ("", "2500000", 2500000, "DEPOSIT"),
    ],
)
def test_load_bank_rows_normalizes_debit_and_credit(
    tmp_path: Path,
    debit: str,
    credit: str,
    expected_amount: int,
    expected_action: str,
) -> None:
    path = _write_csv(
        tmp_path / "bank.csv",
        ["Date", "Debit", "Credit", "Description", "Reference"],
        [_bank_row(debit=debit, credit=credit)],
    )

    rows = load_bank_rows(path)

    assert rows == [
        {
            "trade_date": "2026-10-12",
            "external_reference": "BANK-001",
            "description": "Settlement",
            "net_cash_amount": expected_amount,
            "action": expected_action,
            "raw": {
                "date": "2026-10-12",
                "debit": debit,
                "credit": credit,
                "description": "Settlement",
                "reference": "BANK-001",
            },
        }
    ]


def test_load_bank_rows_accepts_alternate_column_names_and_skips_zero_amounts(
    tmp_path: Path,
) -> None:
    path = _write_csv(
        tmp_path / "bank.csv",
        ["Tanggal", "Debet", "Kredit", "Keterangan", "No Ref"],
        [
            ["12/10/2026", "0", "0", "Zero movement", "BANK-000"],
            ["13/10/2026", "150000", "0", "Pembelian saham", "BANK-002"],
        ],
    )

    rows = load_bank_rows(path)

    assert len(rows) == 1
    assert rows[0]["trade_date"] == "2026-10-13"
    assert rows[0]["external_reference"] == "BANK-002"
    assert rows[0]["description"] == "Pembelian saham"
    assert rows[0]["net_cash_amount"] == -150000
    assert rows[0]["action"] == "WITHDRAW"


def test_load_bank_rows_parses_xlsx_and_signed_parenthesized_amount(
    tmp_path: Path,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Transaction Date", "Debit Amount", "Credit Amount", "Memo", "ID"])
    sheet.append(["2026-10-12", "(125000)", None, "Cash debit", "BANK-XLSX"])
    path = tmp_path / "bank.xlsx"
    workbook.save(path)
    workbook.close()

    rows = load_bank_rows(path)

    assert len(rows) == 1
    assert rows[0]["net_cash_amount"] == -125000
    assert rows[0]["action"] == "WITHDRAW"
    assert rows[0]["external_reference"] == "BANK-XLSX"


def test_bank_ingestion_stages_bank_rows_with_pending_status(db_path: Path, tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "bank.csv",
        ["Date", "Debit", "Credit", "Description", "Reference"],
        [
            _bank_row(reference="BANK-001"),
            _bank_row(
                date="2026-10-13",
                debit="",
                credit="750000",
                description="Dividend",
                reference="BANK-002",
            ),
        ],
    )

    result = ingest_bank_statement(path, db_path)

    assert result["count"] == 2
    assert len(result["inserted"]) == 2
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT staging_id, source_type, external_reference, trade_date,
                   action, net_cash_amount, reconciliation_status
            FROM reconciliation_staging ORDER BY external_reference
            """
        ).fetchall()
    finally:
        connection.close()

    assert rows == [
        (result["inserted"][0], "BANK", "BANK-001", "2026-10-12", "WITHDRAW", -1000000, "PENDING"),
        (result["inserted"][1], "BANK", "BANK-002", "2026-10-13", "DEPOSIT", 750000, "PENDING"),
    ]


@pytest.mark.parametrize("day_offset", [0, 1, 2, 3])
def test_reconciliation_matches_bank_cash_on_t_plus_zero_through_t_plus_three(
    db_path: Path, day_offset: int
) -> None:
    from datetime import date, timedelta

    trade_date = date(2026, 10, 12)
    bank_date = (trade_date + timedelta(days=day_offset)).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        _insert_staging_row(
            connection,
            staging_id="trade-1",
            source_type="BROKER",
            trade_date=trade_date.isoformat(),
            amount=1000000,
            reference="ORDER-1",
            ticker="BBRI",
            action="BUY",
            shares=100,
            price=10000,
        )
        _insert_staging_row(
            connection,
            staging_id="bank-1",
            source_type="BANK",
            trade_date=bank_date,
            amount=-1000500,
            reference="BANK-1",
        )
        connection.commit()
    finally:
        connection.close()

    summary = reconcile_trades(db_path)

    assert len(summary["matched"]) == 1
    assert summary["pending"] == []
    assert summary["flagged"] == []
    assert summary["matched"][0]["trade_staging_id"] == "trade-1"
    assert summary["matched"][0]["bank_staging_id"] == "bank-1"

    connection = sqlite3.connect(db_path)
    try:
        statuses = dict(
            connection.execute(
                "SELECT staging_id, reconciliation_status FROM reconciliation_staging"
            ).fetchall()
        )
        assert statuses == {"trade-1": "MATCHED", "bank-1": "MATCHED"}
        journal = connection.execute(
            """
            SELECT entry_id, source_type, trade_date
            FROM journal_entries
            """
        ).fetchone()
        assert journal is not None
        assert journal[1] == "BROKER_CSV"
        assert journal[2] == trade_date.isoformat()
        legs = connection.execute(
            """
            SELECT account_id, debit_amount, credit_amount
            FROM journal_legs WHERE entry_id = ?
            """,
            (journal[0],),
        ).fetchall()
        assert sum(leg[1] for leg in legs) == sum(leg[2] for leg in legs)
        assert len(legs) == 2
    finally:
        connection.close()


def test_unmatched_buy_remains_pending_clearance_without_journal(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        _insert_staging_row(
            connection,
            staging_id="trade-pending",
            source_type="BROKER",
            trade_date="2026-10-12",
            amount=500000,
            reference="ORDER-PENDING",
            ticker="BBRI",
            action="BUY",
            shares=50,
            price=10000,
        )
        connection.commit()
    finally:
        connection.close()

    summary = reconcile_trades(db_path)

    assert summary["matched"] == []
    assert summary["flagged"] == []
    assert summary["pending"] == [
        {
            "staging_id": "trade-pending",
            "external_reference": "ORDER-PENDING",
            "ticker": "BBRI",
            "action": "BUY",
            "reference": "Pending Clearance",
        }
    ]

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT reconciliation_status FROM reconciliation_staging WHERE staging_id = 'trade-pending'"
        ).fetchone()[0] == "PENDING"
        assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    finally:
        connection.close()


def test_nominal_difference_beyond_tolerance_is_flagged(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        _insert_staging_row(
            connection,
            staging_id="trade-diff",
            source_type="BROKER",
            trade_date="2026-10-12",
            amount=1000000,
            reference="ORDER-DIFF",
            ticker="BBRI",
            action="BUY",
            shares=100,
            price=10000,
        )
        _insert_staging_row(
            connection,
            staging_id="bank-diff",
            source_type="BANK",
            trade_date="2026-10-14",
            amount=-1002000,
            reference="BANK-DIFF",
        )
        connection.commit()
    finally:
        connection.close()

    summary = reconcile_trades(db_path)

    assert summary["matched"] == []
    assert summary["pending"] == []
    assert summary["flagged"] == [
        {
            "staging_id": "trade-diff",
            "external_reference": "ORDER-DIFF",
            "ticker": "BBRI",
            "delta": 2000,
            "reason": "nominal mismatch beyond rounding tolerance",
        }
    ]

    connection = sqlite3.connect(db_path)
    try:
        status = connection.execute(
            "SELECT reconciliation_status FROM reconciliation_staging WHERE staging_id = 'trade-diff'"
        ).fetchone()[0]
        assert status == "FLAGGED"
        assert connection.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    finally:
        connection.close()


def test_out_of_window_bank_row_does_not_match_or_create_journal(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        _insert_staging_row(
            connection,
            staging_id="trade-window",
            source_type="BROKER",
            trade_date="2026-10-12",
            amount=1000000,
            reference="ORDER-WINDOW",
            ticker="BBRI",
            action="BUY",
            shares=100,
            price=10000,
        )
        _insert_staging_row(
            connection,
            staging_id="bank-window",
            source_type="BANK",
            trade_date="2026-10-16",
            amount=-1000000,
            reference="BANK-WINDOW",
        )
        connection.commit()
    finally:
        connection.close()

    summary = reconcile_trades(db_path)

    assert summary["matched"] == []
    assert len(summary["pending"]) == 1
    assert summary["pending"][0]["staging_id"] == "trade-window"


def test_bank_file_without_recognizable_date_is_rejected(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "bank.csv",
        ["Description", "Debit", "Credit"],
        [["Settlement", "100000", ""]],
    )

    with pytest.raises(ValueError, match="missing a recognizable date column"):
        load_bank_rows(path)


def test_unsupported_bank_file_extension_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bank.txt"
    path.write_text("not a statement", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported bank file format"):
        load_bank_rows(path)
