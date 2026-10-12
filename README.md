# Portfolio Management Engine (`Saham_lokal`)

Sistem mesin portofolio berbasis Python dengan arsitektur modular, validasi ketat, dan penegakan akuntansi entri-ganda (double-entry ledger).

## Fitur Utama
- **Database & Migrations**: Migrasi skema SQLite atomik dengan WAL mode dan foreign keys enforcement.
- **Config & Validation**: Skema konfigurasi aturan broker dan parameter portofolio via YAML.
- **Ingestion & Deduplication**: Pipeline parsing CSV broker dengan hashing transaksi natural.
- **Ledger & FIFO Disposal**: Double-entry accounting validator dan eksekusi lot berbasis FIFO.
- **Market Data & Strategy**: Sinkronisasi harga EOD, fallback cache basi, dan evaluasi sinyal teknikal/fundamental.

## Quality Gates
- **Tests**: 225 unit & integration tests (`pytest`) lulus 100%.
- **Type Checking**: Strict type checking via `mypy`.
- **Linting**: Code standard compliance via `ruff`.

## Menjalankan Pengujian Lokal
```bash
# Aktifkan virtual environment
source .venv-linux/bin/activate

# Jalankan seluruh test suite
pytest

# Jalankan static analysis
ruff check src/ tests/
mypy src/ tests/
eof

