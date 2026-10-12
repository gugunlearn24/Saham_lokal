from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from src.db.migrator import run_migrations
from src.ingestion.broker_csv_parser import ingest_broker_file, load_trade_rows
from src.ingestion.deduplication import compute_entry_hash, entry_hash_exists

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BROKERS_CONFIG_PATH = PROJECT_ROOT / "config" / "brokers.yaml"
STOCKBIT_HEADERS = [
    "Date",
    "Code",
    "Type",
    "Lot",
    "Price",
    "Brokerage Fee",
    "Exchange Levy",
    "PPh Final",
    "Total Amount",
    "Reference ID",
]
IPOT_HEADERS = [
    "Tgl Transaksi",
    "Kode Efek",
    "Aktivitas",
    "Volume Lembar",
    "Harga",
    "Komisi",
    "Levy",
    "Pajak",
    "Netto",
    "Reference ID",
]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    run_migrations(path)
    return path


def _write_csv(
    path: Path,
    headers: list[str],
    rows: list[list[Any]],
    *,
    delimiter: str = ",",
) -> Path:
    import csv

    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter=delimiter)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def _stockbit_row(
    *,
    trade_date: str = "2026-10-09",
    ticker: str = "BBRI",
    action: str = "Buy",
    lots: str = "2",
    price: str = "5,500.00",
    net_amount: str = "1,101,000",
    reference_id: str = "SB-001",
) -> list[str]:
    return [
        trade_date,
        ticker,
        action,
        lots,
        price,
        "1,000",
        "500",
        "0",
        net_amount,
        reference_id,
    ]


def _ipot_row(
    *,
    trade_date: str = "09/10/2026",
    ticker: str = "bbca",
    action: str = "BELI",
    shares: str = "1.200",
    price: str = "9.250,50",
    net_amount: str = "11.100.600",
    reference_id: str = "IP-001",
) -> list[str]:
    return [
        trade_date,
        ticker,
        action,
        shares,
        price,
        "10.000",
        "2.000",
        "0",
        net_amount,
        reference_id,
    ]


def _expected_entry_hash(
    broker_id: str,
    trade_date: str,
    ticker: str,
    action: str,
    shares: int,
    gross_amount: int,
    reference_id: str,
) -> str:
    signature = (
        f"{broker_id}{trade_date}{ticker}{action}{shares}{gross_amount}{reference_id}"
    )
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def test_parses_stockbit_csv_and_converts_lots_to_shares(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "stockbit.csv", STOCKBIT_HEADERS, [_stockbit_row()]
    )

    rows = load_trade_rows(csv_path, "STOCKBIT", BROKERS_CONFIG_PATH)

    assert rows == [
        {
            "broker_id": "STOCKBIT",
            "trade_date": "2026-10-09",
            "ticker": "BBRI",
            "action": "BUY",
            "shares": 200,
            "price_per_share": 5500.0,
            "gross_amount": 1101000,
            "net_cash_amount": 1101000,
            "reference_id": "SB-001",
            "raw": dict(zip(STOCKBIT_HEADERS, _stockbit_row())),
        }
    ]


def test_parses_ipot_csv_date_decimal_separators_and_share_quantity(
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path / "ipot.csv",
        IPOT_HEADERS,
        [_ipot_row()],
        delimiter=";",
    )

    rows = load_trade_rows(csv_path, "IPOT", BROKERS_CONFIG_PATH)

    assert len(rows) == 1
    assert rows[0]["broker_id"] == "IPOT"
    assert rows[0]["trade_date"] == "2026-10-09"
    assert rows[0]["ticker"] == "BBCA"
    assert rows[0]["action"] == "BUY"
    assert rows[0]["shares"] == 1200
    assert rows[0]["price_per_share"] == 9250.5
    assert rows[0]["gross_amount"] == 11100600
    assert rows[0]["reference_id"] == "IP-001"


def test_parses_xlsx_broker_statement(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(STOCKBIT_HEADERS)
    sheet.append(_stockbit_row(action="Sell", ticker="tlkm", reference_id="SB-XLSX"))
    sheet.append([None] * len(STOCKBIT_HEADERS))
    xlsx_path = tmp_path / "stockbit.xlsx"
    workbook.save(xlsx_path)
    workbook.close()

    rows = load_trade_rows(xlsx_path, "STOCKBIT", BROKERS_CONFIG_PATH)

    assert len(rows) == 1
    assert rows[0]["ticker"] == "TLKM"
    assert rows[0]["action"] == "SELL"
    assert rows[0]["shares"] == 200
    assert rows[0]["reference_id"] == "SB-XLSX"


def test_rejects_unknown_broker_unsupported_extension_and_invalid_trade_data(
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path / "stockbit.csv", STOCKBIT_HEADERS, [_stockbit_row()]
    )
    with pytest.raises(ValueError, match="Unknown broker id"):
        load_trade_rows(csv_path, "UNKNOWN", BROKERS_CONFIG_PATH)

    unsupported_path = tmp_path / "stockbit.txt"
    unsupported_path.write_text("not a broker statement", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported file format"):
        load_trade_rows(unsupported_path, "STOCKBIT", BROKERS_CONFIG_PATH)

    invalid_action_path = _write_csv(
        tmp_path / "invalid-action.csv",
        STOCKBIT_HEADERS,
        [_stockbit_row(action="TRANSFER")],
    )
    with pytest.raises(ValueError, match="Unsupported trade action"):
        load_trade_rows(invalid_action_path, "STOCKBIT", BROKERS_CONFIG_PATH)

    invalid_date_path = _write_csv(
        tmp_path / "invalid-date.csv",
        STOCKBIT_HEADERS,
        [_stockbit_row(trade_date="not-a-date")],
    )
    with pytest.raises(ValueError, match="Unsupported trade date format"):
        load_trade_rows(invalid_date_path, "STOCKBIT", BROKERS_CONFIG_PATH)


def test_entry_hash_matches_exact_sha256_contract_and_is_deterministic() -> None:
    arguments = ("STOCKBIT", "2026-10-09", "BBRI", "BUY", 200, 1101000, "SB-001")
    expected = _expected_entry_hash(*arguments)

    assert compute_entry_hash(*arguments) == expected
    assert compute_entry_hash(*arguments) == compute_entry_hash(*arguments)
    assert len(expected) == 64

    changed = compute_entry_hash(
        "STOCKBIT", "2026-10-09", "BBRI", "BUY", 201, 1101000, "SB-001"
    )
    assert changed != expected


def test_entry_hash_exists_checks_journal_entries_and_staging_payload(
    db_path: Path,
) -> None:
    entry_hash = compute_entry_hash(
        "STOCKBIT", "2026-10-09", "BBRI", "BUY", 200, 1101000, "SB-001"
    )
    connection = sqlite3.connect(db_path)
    try:
        assert not entry_hash_exists(connection, entry_hash)
        connection.execute(
            """
            INSERT INTO journal_entries (
                entry_id, entry_hash, entry_date, source_type, description
            ) VALUES ('entry-1', ?, '2026-10-10', 'BROKER_CSV', 'existing')
            """,
            (entry_hash,),
        )
        assert entry_hash_exists(connection, entry_hash)
        connection.execute("DELETE FROM journal_entries WHERE entry_id = 'entry-1'")
        connection.execute(
            """
            INSERT INTO reconciliation_staging (
                staging_id, source_type, external_reference, trade_date,
                net_cash_amount, raw_payload
            ) VALUES ('staging-1', 'BROKER', 'SB-001', '2026-10-09', 1101000, ?)
            """,
            (json.dumps({"entry_hash": entry_hash}),),
        )
        assert entry_hash_exists(connection, entry_hash)
    finally:
        connection.close()


@pytest.mark.parametrize("duplicate_table", ["journal_entries", "reconciliation_staging"])
def test_ingestion_skips_existing_duplicate_and_reports_it(
    db_path: Path, tmp_path: Path, duplicate_table: str
) -> None:
    csv_path = _write_csv(
        tmp_path / "stockbit.csv", STOCKBIT_HEADERS, [_stockbit_row()]
    )
    entry_hash = compute_entry_hash(
        "STOCKBIT", "2026-10-09", "BBRI", "BUY", 200, 1101000, "SB-001"
    )
    connection = sqlite3.connect(db_path)
    try:
        if duplicate_table == "journal_entries":
            connection.execute(
                """
                INSERT INTO journal_entries (
                    entry_id, entry_hash, entry_date, source_type, description
                ) VALUES ('existing-entry', ?, '2026-10-10', 'BROKER_CSV', 'existing')
                """,
                (entry_hash,),
            )
        else:
            connection.execute(
                """
                INSERT INTO reconciliation_staging (
                    staging_id, source_type, external_reference, trade_date,
                    net_cash_amount, raw_payload
                ) VALUES ('existing-staging', 'BROKER', 'SB-001', '2026-10-09', 1101000, ?)
                """,
                (json.dumps({"entry_hash": entry_hash}),),
            )
        connection.commit()
    finally:
        connection.close()

    result = ingest_broker_file(
        csv_path,
        "STOCKBIT",
        db_path=db_path,
        config_path=BROKERS_CONFIG_PATH,
    )

    assert result["inserted"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["entry_hash"] == entry_hash
    assert result["skipped"][0]["reason"] == "duplicate"


def test_ingestion_inserts_valid_rows_as_pending_and_skips_repeated_rows(
    db_path: Path, tmp_path: Path
) -> None:
    first_row = _stockbit_row()
    second_row = _stockbit_row(
        ticker="TLKM",
        action="Sell",
        lots="3",
        price="3,250",
        net_amount="975,000",
        reference_id="SB-002",
    )
    csv_path = _write_csv(
        tmp_path / "stockbit.csv",
        STOCKBIT_HEADERS,
        [first_row, first_row, second_row],
    )

    result = ingest_broker_file(
        csv_path,
        "STOCKBIT",
        db_path=db_path,
        config_path=BROKERS_CONFIG_PATH,
    )

    assert len(result["inserted"]) == 2
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "duplicate"

    connection = sqlite3.connect(db_path)
    try:
        staged_rows = connection.execute(
            """
            SELECT staging_id, source_type, external_reference, trade_date,
                   ticker, action, shares, price_per_share, net_cash_amount,
                   raw_payload, reconciliation_status
            FROM reconciliation_staging
            ORDER BY external_reference
            """
        ).fetchall()
    finally:
        connection.close()

    assert len(staged_rows) == 2
    for row in staged_rows:
        assert row[0] in result["inserted"]
        assert row[1] == "BROKER"
        assert row[10] == "PENDING"
        payload = json.loads(row[9])
        assert payload["entry_hash"] == _expected_entry_hash(
            payload["broker_id"],
            payload["trade_date"],
            payload["ticker"],
            payload["action"],
            payload["shares"],
            payload["gross_amount"],
            payload["reference_id"],
        )

    by_ticker = {row[4]: row for row in staged_rows}
    assert by_ticker["BBRI"][5:9] == ("BUY", 200, 5500.0, 1101000)
    assert by_ticker["TLKM"][5:9] == ("SELL", 300, 3250.0, 975000)
