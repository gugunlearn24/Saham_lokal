# Technical Specification: Local IHSG Swing Investment Portfolio & Monitoring Engine

## 1. System Overview
Sistem ini dirancang sebagai aplikasi lokal mandiri (*local-first standalone system*) untuk mencatat transaksi dan memantau kinerja portofolio investasi saham di Bursa Efek Indonesia (IHSG) dengan horizon investasi jangka menengah hingga panjang ($\ge 1\text{ tahun}$). 

Sistem mengadopsi prinsip dasar pembukuan ganda (*double-entry bookkeeping*) untuk menjamin integritas matematis arus kas dan kepemilikan aset efek, serta menerapkan mesin evaluasi terpisah yang menggabungkan analisis teknikal mingguan (*weekly technicals*) dan data fundamental kuartalan. Seluruh data keuangan, konfigurasi strategi, dan riwayat audit tersimpan murni pada media penyimpanan lokal komputer pengguna tanpa ketergantungan pada server pihak ketiga.

---

## 2. Key Architecture Decisions (ADR Summary)

* **ADR-01: Ingestion Pipeline & Normalisasi Data**
  * *Keputusan:* Mengeliminasi input berbasis gambar/OCR. Mengadopsi **Multi-Broker Adapter Pattern berbasis YAML** untuk mengurai berkas ekspor transaksi CSV/XLSX dari berbagai sekuritas ke dalam skema internal kanonikal.
  * *Rasional:* Menjamin determinisme dan mengeliminasi risiko galat pengenalan karakter visual (halusinasi angka pada OCR) pada data numerik keuangan.

* **ADR-02: Siklus dan Sumber Data Pasar**
  * *Keputusan:* **Hybrid Ingestion**. Data harga penutupan (*End-of-Day*) dan data historis ditarik secara manual (*on-demand user trigger*) tiap akhir pekan melalui API publik gratis (Yahoo Finance). Data laporan keuangan fundamental diinput secara manual per kuartal.
  * *Rasional:* Mengingat horizon investasi $\ge 1\text{ tahun}$, pembaruan fundamental hanya terjadi 4 kali setahun per emiten, sedangkan pemantauan teknikal mingguan tidak memerlukan koneksi pasar waktu nyata (*real-time feed*).

* **ADR-03: Model Akuntansi & Kepemilikan Aset**
  * *Keputusan:* **Full Double-Entry Ledger** dengan aturan **Cash-Basis Settlement Binding** dan pencatatan kas murni pada tanggal pencairan dividen (*Payment Date*).
  * *Rasional:* Memberikan audit trail yang presisi terhadap Rekening Dana Nasabah (RDN), biaya transaksi, pajak final, serta mencegah pencatatan kepemilikan saham tanpa pasangan mutasi kas bank yang terverifikasi.

* **ADR-04: Penanganan Aksi Korporasi & Alokasi Lot**
  * *Keputusan:* **Strict FIFO (First-In, First-Out)** dengan **Corporate Action Split Events**. Pecahan lembar saham akibat penyesuaian rasio diselesaikan menggunakan metode **Lot-Level Floor Rounding** dengan selisih lembar dibukukan ke akun beban operasional pembulatan.
  * *Rasional:* Memastikan pelacakan usia kepemilikan per lot tetap akurat dan kepatuhan terhadap aturan pembulatan satuan lembar bursa KSEI.

* **ADR-05: Model Rekonsiliasi & Idempotensi Impor**
  * *Keputusan:* **Dual-Stream Reconciliation** (pencocokan berkas transaksi efek dan mutasi rekening koran RDN) dengan pengenalan duplikasi berbasis **Deterministic Natural Hash (`SHA256`)**.
  * *Rasional:* Mencegah entri ganda saat pengguna mengimpor berkas transaksi mingguan yang memiliki rentang tanggal saling tumpang tindih (*overlapping trade dates*).

* **ADR-06: Penyimpanan & Konsistensi Evaluasi**
  * *Keputusan:* **Hybrid Flat-File Storage** (SQLite untuk transaksi, lot, dan mutasi; YAML untuk konfigurasi kriteria/skema adapter) dengan mekanisme **Point-in-Time Immutable Snapshots** untuk riwayat evaluasi mingguan. Cadangan sistem dikelola melalui **Bundled Archive with Manifest Checksum**.
  * *Rasional:* Memisahkan kelenturan modifikasi aturan evaluasi oleh pengguna tanpa mendistorsi integritas catatan rekam jejak kepatuhan strategi di masa lalu.

---

## 3. Data Schema (SQLite DDL & Relationships)

### 3.1. Entity Relationship Diagram (Logical)
```
+------------------+       +---------------------+       +---------------------+
|     accounts     |1     *|   journal_entries   |1     *|    journal_legs     |
+------------------+-------+---------------------+-------+---------------------+
                                      | 1                           |
                                      | *                           |
                           +---------------------+                  |
                           |   reconciliation    |                  |
                           +---------------------+                  |
                                                                    |
+------------------+       +---------------------+                  |
| corporate_actions|1     *|      stock_lots     |1                 |
+------------------+-------+---------------------+                  |
                                      | 1                           |
                                      | *                           |
                           +---------------------+                  |
                           |    lot_disposals    |*                 |
                           +---------------------+                  |
                                                                    |
+------------------+       +---------------------+                  |
| market_prices_eod|       | evaluation_snapshots|                  |
+------------------+       +---------------------+                  |
```

### 3.2. DDL Specifications

```sql
-- Enforce Foreign Key Constraints
PRAGMA foreign_keys = ON;

-- 1. Master Akun Buku Besar (Double-Entry Chart of Accounts)
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,               -- e.g., 'ASSET:RDN_CASH', 'ASSET:PORTFOLIO:BBRI', 'EXPENSE:TAX_PPH_FINAL'
    account_name TEXT NOT NULL,
    account_type TEXT NOT NULL CHECK (account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')),
    currency TEXT NOT NULL DEFAULT 'IDR',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. Buku Besar Jurnal (Journal Entries Header)
CREATE TABLE journal_entries (
    entry_id TEXT PRIMARY KEY,                 -- UUIDv4
    entry_hash TEXT NOT NULL UNIQUE,          -- Deterministic SHA-256 for deduplication
    entry_date DATE NOT NULL,                 -- Settlement or effective date
    trade_date DATE,                          -- Actual market execution date
    source_type TEXT NOT NULL CHECK (source_type IN ('BROKER_CSV', 'BANK_CSV', 'MANUAL', 'DIVIDEND', 'CORP_ACTION')),
    reference_id TEXT,                        -- External Order ID or Bank Reference ID
    description TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 3. Jurnal Kaki / Pembukuan Ganda (Journal Legs)
CREATE TABLE journal_legs (
    leg_id TEXT PRIMARY KEY,                  -- UUIDv4
    entry_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    debit_amount INTEGER NOT NULL DEFAULT 0,  -- In IDR (Fixed-point integer representation)
    credit_amount INTEGER NOT NULL DEFAULT 0, -- In IDR (Fixed-point integer representation)
    FOREIGN KEY (entry_id) REFERENCES journal_entries(entry_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE RESTRICT,
    CHECK ((debit_amount > 0 AND credit_amount = 0) OR (credit_amount > 0 AND debit_amount = 0))
);

-- 4. Pelacakan Lot Saham FIFO (Stock Lots)
CREATE TABLE stock_lots (
    lot_id TEXT PRIMARY KEY,                  -- UUIDv4
    entry_id TEXT NOT NULL,                   -- Originating journal entry
    ticker TEXT NOT NULL,                     -- IDX Ticker without suffix, e.g., 'BBRI'
    acquired_date DATE NOT NULL,
    original_shares INTEGER NOT NULL CHECK (original_shares > 0),
    remaining_shares INTEGER NOT NULL CHECK (remaining_shares >= 0),
    cost_per_share REAL NOT NULL CHECK (cost_per_share > 0), -- Cost basis per share
    split_adjustment_factor REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'PARTIALLY_CLOSED', 'CLOSED')),
    FOREIGN KEY (entry_id) REFERENCES journal_entries(entry_id) ON DELETE RESTRICT
);

-- 5. Realisasi Penjualan Lot (Lot Disposals / Realized PnL Link)
CREATE TABLE lot_disposals (
    disposal_id TEXT PRIMARY KEY,             -- UUIDv4
    lot_id TEXT NOT NULL,
    disposal_entry_id TEXT NOT NULL,          -- Journal entry of the sell transaction
    shares_liquidated INTEGER NOT NULL CHECK (shares_liquidated > 0),
    cost_basis_per_share REAL NOT NULL,
    selling_price_per_share REAL NOT NULL,
    realized_pnl INTEGER NOT NULL,            -- In IDR (Integer: positive for gain, negative for loss)
    disposal_date DATE NOT NULL,
    FOREIGN KEY (lot_id) REFERENCES stock_lots(lot_id) ON DELETE RESTRICT,
    FOREIGN KEY (disposal_entry_id) REFERENCES journal_entries(entry_id) ON DELETE RESTRICT
);

-- 6. Log Aksi Korporasi (Corporate Actions Registry)
CREATE TABLE corporate_actions (
    action_id TEXT PRIMARY KEY,               -- UUIDv4
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('STOCK_SPLIT', 'REVERSE_SPLIT', 'BONUS_SHARES')),
    execution_date DATE NOT NULL,
    ratio_multiplier INTEGER NOT NULL CHECK (ratio_multiplier > 0),
    ratio_divisor INTEGER NOT NULL CHECK (ratio_divisor > 0),
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 7. Penyimpanan Deret Harga Pasar Harian (Market Data Dual-Series)
CREATE TABLE market_prices_eod (
    ticker TEXT NOT NULL,
    price_date DATE NOT NULL,
    raw_close REAL NOT NULL CHECK (raw_close >= 0),
    adjusted_close REAL NOT NULL CHECK (adjusted_close >= 0),
    volume INTEGER,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, price_date)
);

-- 8. Data Fundamental Kuartalan (Manual Ingestion)
CREATE TABLE fundamental_snapshots (
    ticker TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_quarter INTEGER NOT NULL CHECK (fiscal_quarter BETWEEN 1 AND 4),
    report_release_date DATE NOT NULL,
    roe_percentage REAL,                      -- Return on Equity (%)
    der_ratio REAL,                           -- Debt to Equity Ratio
    per_ratio REAL,                           -- Price to Earnings Ratio
    pbv_ratio REAL,                           -- Price to Book Value Ratio
    net_profit_margin REAL,                   -- Net Profit Margin (%)
    raw_payload_json TEXT,                    -- Flexible JSON storage for custom ratios
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year, fiscal_quarter)
);

-- 9. Hasil Evaluasi Mingguan Imutabel (Point-in-Time Evaluation Snapshots)
CREATE TABLE evaluation_snapshots (
    snapshot_id TEXT PRIMARY KEY,             -- UUIDv4
    evaluation_date DATE NOT NULL,
    ticker TEXT NOT NULL,
    technical_status TEXT NOT NULL CHECK (technical_status IN ('PASS', 'WARN', 'FAIL')),
    fundamental_status TEXT NOT NULL CHECK (fundamental_status IN ('PASS', 'WARN', 'FAIL')),
    overall_signal TEXT NOT NULL CHECK (overall_signal IN ('HOLD', 'BUY_MORE', 'REBALANCE', 'EXIT')),
    metrics_evaluated_json TEXT NOT NULL,     -- Frozen evaluation values & active YAML thresholds
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (evaluation_date, ticker)
);

-- 10. Antrean Rekonsiliasi Belum Selesai (Unsettled / Staging Trades)
CREATE TABLE reconciliation_staging (
    staging_id TEXT PRIMARY KEY,              -- UUIDv4
    source_type TEXT NOT NULL CHECK (source_type IN ('BROKER', 'BANK')),
    external_reference TEXT NOT NULL,
    trade_date DATE NOT NULL,
    ticker TEXT,
    action TEXT CHECK (action IN ('BUY', 'SELL', 'DEPOSIT', 'WITHDRAW', 'DIVIDEND', 'FEES')),
    shares INTEGER,
    price_per_share REAL,
    net_cash_amount INTEGER NOT NULL,
    raw_payload TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (reconciliation_status IN ('PENDING', 'MATCHED', 'FLAGGED')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for Query Performance
CREATE INDEX idx_lots_ticker_status ON stock_lots(ticker, status);
CREATE INDEX idx_journal_legs_account ON journal_legs(account_id);
CREATE INDEX idx_market_prices_lookup ON market_prices_eod(ticker, price_date DESC);
CREATE INDEX idx_eval_date_ticker ON evaluation_snapshots(evaluation_date, ticker);
```

---

## 4. Configuration Contracts (YAML Specifications)

### 4.1. Broker Adapter Configuration (`brokers.yaml`)
Menentukan pemetaan kolom berkas CSV dari sekuritas ke skema internal kanonikal.

```yaml
brokers:
  - broker_id: "STOCKBIT"
    broker_name: "Stockbit Sekuritas"
    delimiter: ","
    date_format: "YYYY-MM-DD"
    encoding: "utf-8"
    mappings:
      trade_date: "Date"
      ticker: "Code"
      action: "Type"          # Values: "Buy", "Sell"
      shares: "Lot"           # Multiplied by 100 via multiplier rule
      price_per_share: "Price"
      broker_fee: "Brokerage Fee"
      levy_fee: "Exchange Levy"
      tax_pph: "PPh Final"
      net_amount: "Total Amount"
    conversions:
      lot_multiplier: 100
      decimal_separator: "."
      thousand_separator: ","

  - broker_id: "IPOT"
    broker_name: "Indo Premier Sekuritas"
    delimiter: ";"
    date_format: "DD/MM/YYYY"
    encoding: "utf-8"
    mappings:
      trade_date: "Tgl Transaksi"
      ticker: "Kode Efek"
      action: "Aktivitas"      # Values: "BELI", "JUAL"
      shares: "Volume Lembar"
      price_per_share: "Harga"
      broker_fee: "Komisi"
      levy_fee: "Levy"
      tax_pph: "Pajak"
      net_amount: "Netto"
    conversions:
      lot_multiplier: 1
      decimal_separator: ","
      thousand_separator: "."
```

### 4.2. Strategy Evaluation Rules (`rules.yaml`)
Menentukan ambang batas matriks fundamental dan teknikal mingguan.

```yaml
version: "1.0.0"
strategy_name: "IHSG Swing Value-Momentum 1Y+"

evaluation_matrix:
  fundamental_rules:
    - id: "RULE_ROE"
      metric: "roe_percentage"
      operator: ">="
      threshold: 12.0
      severity: "FAIL"         # If violated, status becomes FAIL
    - id: "RULE_DER"
      metric: "der_ratio"
      operator: "<="
      threshold: 1.5
      severity: "WARN"
    - id: "RULE_NPM"
      metric: "net_profit_margin"
      operator: ">"
      threshold: 5.0
      severity: "WARN"

  technical_rules:
    - id: "RULE_WEEKLY_MA"
      metric: "close_vs_weekly_ma30"
      operator: ">="
      threshold: 1.0           # Price must be above or equal to 30-week Moving Average
      severity: "FAIL"
    - id: "RULE_WEEKLY_RSI"
      metric: "weekly_rsi14"
      operator: "BETWEEN"
      range: [40.0, 70.0]
      severity: "WARN"

  action_matrix:
    - condition: "fundamental == 'PASS' AND technical == 'PASS'"
      action: "HOLD"
    - condition: "fundamental == 'PASS' AND technical == 'WARN'"
      action: "HOLD"
    - condition: "fundamental == 'FAIL' OR technical == 'FAIL'"
      action: "EXIT"
    - condition: "fundamental == 'WARN' AND technical == 'FAIL'"
      action: "EXIT"
```

---

## 5. System Workflows & Accounting Specifications

### 5.1. Multi-Leg Journaling for Sell Executions
Setiap eksekusi penjualan saham di BEI menguraikan nilai transaksi kotor (*gross value*), biaya broker, PPN, levy bursa, dan PPh Final 0,1% ke dalam pos buku besar terpisah.

$$\text{Gross Amount} = \text{Shares} \times \text{Selling Price}$$

$$\text{PPh Final 0.1\%} = \text{Gross Amount} \times 0.001$$

$$\text{Cost of Goods Sold (FIFO)} = \sum_{i \in \text{Lots}} (\text{Liquidated Shares}_i \times \text{Cost Basis}_i)$$

$$\text{Realized PnL} = \text{Gross Amount} - \text{Cost of Goods Sold}$$

$$\text{Net Cash Inflow to RDN} = \text{Gross Amount} - \text{Broker Fee} - \text{Levy} - \text{PPh Final}$$

#### Journal Legs Pattern (Balanced Entry):
* **DEBIT** `ASSET:RDN_CASH`: Nominal Kas Bersih yang Masuk
* **DEBIT** `EXPENSE:BROKER_FEE`: Komisi Broker + PPN
* **DEBIT** `EXPENSE:EXCHANGE_LEVY`: Pungutan BEI / KPEI / KSEI
* **DEBIT** `EXPENSE:TAX_PPH_FINAL`: PPh Final Penjualan Saham (0,1%)
* **CREDIT** `ASSET:PORTFOLIO:{TICKER}`: Nilai Pokok FIFO (*Cost Basis*)
* **CREDIT/DEBIT** `REVENUE:REALIZED_GAIN_LOSS`: Penyeimbang Selisih Laba Bersih (Kredit jika untung, Debit jika rugi)

$$\sum \text{Debits} = \sum \text{Credits}$$

---

### 5.2. Corporate Action Split Algorithm (Strict FIFO & Floor Rounding)
Ketika suatu emiten mengeksekusi *stock split* dengan rasio $M : D$ ($M = \text{Multiplier}, D = \text{Divisor}$):

1. **Pemindaian Lot:** Ambil semua baris pada `stock_lots` untuk emiten terkait dengan `status IN ('OPEN', 'PARTIALLY_CLOSED')` secara terurut ascending berdasarkan `acquired_date`.
2. **Kalkulasi per Lot:** Untuk setiap lot $k$:
   $$\text{Adjusted Shares}_k = \left\lfloor \text{Remaining Shares}_k \times \frac{M}{D} \right\rfloor$$
   $$\text{Fractional Remainder}_k = \left( \text{Remaining Shares}_k \times \frac{M}{D} \right) - \text{Adjusted Shares}_k$$
   $$\text{Adjusted Cost Per Share}_k = \text{Cost Per Share}_k \times \frac{D}{M}$$
3. **Penyelesaian Sisa Pecahan (*Residual Accounting*):**
   $$\text{Total Fractional Lost} = \sum_k \text{Fractional Remainder}_k$$
   Jika $\text{Total Fractional Lost} > 0$, sistem membuat *journal entry* penyesuaian:
   * **DEBIT** `EXPENSE:CORPORATE_ACTION_ROUNDING`: $\text{Total Fractional Lost} \times \text{Adjusted Cost Per Share}$
   * **CREDIT** `ASSET:PORTFOLIO:{TICKER}`: Nilai lembar saham yang terpotong.
4. **Pembaruan Basis Data:** Perbarui baris `stock_lots` dengan nilai lembar dan harga pokok baru secara atomik.

---

### 5.3. Ingestion & Natural Hash Deduplication Engine
Untuk menjamin sifat idempotensi saat mengimpor berkas transaksi, setiap baris dikonversi menjadi hash deterministik:

$$\text{Raw Signature} = \text{broker\_id} \parallel \text{trade\_date} \parallel \text{ticker} \parallel \text{action} \parallel \text{shares} \parallel \text{gross\_amount} \parallel \text{reference\_id}$$

$$\text{entry\_hash} = \text{SHA256}(\text{Raw Signature})$$

* Logika Import Engine:
  1. Ekstrak baris data dari CSV.
  2. Hitung `entry_hash`.
  3. Lakukan query pengecekan ke tabel `journal_entries` dan `reconciliation_staging`.
  4. Jika `entry_hash` ditemukan, baris diabaikan (*skip*) dan dicatat dalam log impor sebagai entri duplikat.
  5. Jika belum ada, masukkan ke `reconciliation_staging` dengan status `PENDING`.

---

### 5.4. Data Ingestion Fallback & Rate Throttling
Klien HTTP lokal yang menangani penarikan harga mingguan dari Yahoo Finance menerapkan batasan:

* **Sequential Interval:** Waktu jeda tetap sebesar $1500\text{ ms}$ antar emiten.
* **Retry Strategy:** Maksimal 3 kali percobaan ulang dengan *Exponential Backoff* ($2^n \times 1000\text{ ms}$) saat menerima respons status HTTP 429 atau 5xx.
* **Circuit Breaker / Fallback:** Jika percobaan ketiga gagal:
  1. Berhenti memproses emiten tersebut.
  2. Catat galat pada log sesi.
  3. Gunakan data `market_prices_eod` terakhir yang tersimpan di SQLite (*Stale Cache Fallback*).
  4. Munculkan peringatan *Stale Data Warning* pada dasbor mingguan emiten terkait.

---

## 6. System Constraints & Boundary Conditions

1. **Local-Only Boundary:** Sistem beroperasi 100% pada mesin lokal pengguna tanpa database server terpusat. Basis data tunggal menggunakan file SQLite berorientasi mode WAL (*Write-Ahead Logging*) untuk mendukung integritas transaksi lokal.
2. **No Intraday Streaming:** Sistem tidak mendukung pembacaan kuotasi harga waktu nyata (*tick-by-tick* atau *intraday minute data*). Seluruh kalkulasi teknikal beroperasi pada deret waktu mingguan (*Weekly Close*).
3. **Immutability of Evaluated History:** Status matriks evaluasi mingguan bersifat permanen setelah diterbitkan pada akhir pekan. Modifikasi berkas `rules.yaml` di kemudian hari hanya memengaruhi kalkulasi evaluasi di masa depan dan tidak boleh merevisi baris pada tabel `evaluation_snapshots`.
4. **Strict Cash Invariance:** Portofolio efek tidak dapat mencatat saldo kepemilikan saham berstatus *Settled* tanpa adanya entri kredit yang seimbang pada akun Kas RDN. Seluruh pembelian saham yang belum dicocokkan dengan rekening koran RDN tetap berstatus *Pending Clearance* dan ditampilkan dalam status terpisah (*Dual-View Dashboard*).
5. **Currency and Precision:** Seluruh nilai moneter tunai (IDR) disimpan dalam format bilangan bulat (*Integer*) untuk menghindari kesalahan pembulatan *floating-point*. Nilai harga per lembar dan rasio teknikal/fundamental diperbolehkan menggunakan tipe data riil dengan presisi ganda (*64-bit IEEE Floating Point*).