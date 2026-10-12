# Implementation Tickets: IHSG Swing Portfolio Engine

Dokumen ini memecah spesifikasi teknis `tech_spec_ihsg_portfolio.md` menjadi serangkaian tiket implementasi independen yang terurut berdasarkan ketergantungan logika dan data.

---

### TICKET-01: Inisialisasi Skema Basis Data SQLite & Migration Runner
* **ID:** TICKET-01
* **Title:** Setup Database Connection, WAL Mode, and DDL Schema Migration
* **Scope/Files:**
  * `src/db/connection.*`
  * `src/db/migrations/001_initial_schema.sql`
  * `src/db/migrator.*`
* **Prerequisites:** None
* **Acceptance Criteria:**
  1. Koneksi SQLite berhasil diinisialisasi dengan konfigurasi `PRAGMA foreign_keys = ON;` dan `PRAGMA journal_mode = WAL;`.
  2. Modul migrasi mengeksekusi skrip SQL DDL secara atomik dalam transaksi tunggal.
  3. Seluruh 10 tabel (`accounts`, `journal_entries`, `journal_legs`, `stock_lots`, `lot_disposals`, `corporate_actions`, `market_prices_eod`, `fundamental_snapshots`, `evaluation_snapshots`, `reconciliation_staging`) beserta indeks terkait terbuat dengan benar.
  4. Akun standar (*Chart of Accounts*) terinisialisasi secara otomatis saat skema pertama kali dimigrasikan (`ASSET:RDN_CASH`, `EXPENSE:BROKER_FEE`, `EXPENSE:EXCHANGE_LEVY`, `EXPENSE:TAX_PPH_FINAL`, `EXPENSE:CORPORATE_ACTION_ROUNDING`, `REVENUE:REALIZED_GAIN_LOSS`).

---

### TICKET-02: Loader & Validator Konfigurasi YAML (Brokers & Rules)
* **ID:** TICKET-02
* **Title:** YAML Configuration Loader and Validation Schemas
* **Scope/Files:**
  * `config/brokers.yaml`
  * `config/rules.yaml`
  * `src/config/schema_validator.*`
  * `src/config/config_loader.*`
* **Prerequisites:** None
* **Acceptance Criteria:**
  1. Parser membaca berkas `config/brokers.yaml` dan memvalidasi tipe data serta pemetaan kolom wajib (`trade_date`, `ticker`, `action`, `shares`, `price_per_share`, `net_amount`).
  2. Parser membaca berkas `config/rules.yaml` dan memvalidasi format matriks evaluasi fundamental dan teknikal beserta pemetaan aksi portofolio.
  3. Sistem melempar pesan kesalahan yang deskriptif dan menolak inisialisasi jika struktur YAML tidak sesuai format kontrak atau nilai pembagi (*multiplier*) bernilai non-positif.

---

### TICKET-03: Multi-Broker Ingestion Engine & Deduplikasi Hash
* **ID:** TICKET-03
* **Title:** Broker CSV Parser and Deterministic Natural Hash Deduplication
* **Scope/Files:**
  * `src/ingestion/broker_csv_parser.*`
  * `src/ingestion/deduplication.*`
* **Prerequisites:** TICKET-01, TICKET-02
* **Acceptance Criteria:**
  1. Berkas mutasi perdagangan CSV/XLSX dari sekuritas (Stockbit, IPOT) berhasil diurai sesuai definisi adapter pada `brokers.yaml`.
  2. Nilai lot dikonversi secara tepat ke satuan lembar saham menggunakan `lot_multiplier`.
  3. Formula hash deterministik `SHA256(broker_id + trade_date + ticker + action + shares + gross_amount + reference_id)` dihitung per baris data.
  4. Baris dengan `entry_hash` yang sudah ada pada tabel `journal_entries` atau `reconciliation_staging` dilewati (*skipped*) dan dicatat dalam log impor sebagai duplikat.
  5. Baris transaksi yang lolos validasi disimpan ke tabel `reconciliation_staging` dengan status `PENDING`.

---

### TICKET-04: Rekonsiliasi Dual-Stream & Binding Kas RDN
* **ID:** TICKET-04
* **Title:** Bank RDN Statement Ingestion and Trade Reconciliation Engine
* **Scope/Files:**
  * `src/ingestion/bank_csv_parser.*`
  * `src/reconciliation/reconciliation_engine.*`
* **Prerequisites:** TICKET-01, TICKET-03
* **Acceptance Criteria:**
  1. Parser mutasi rekening koran RDN mengimpor mutasi debit/kredit kas ke antrean `reconciliation_staging` dengan `source_type = 'BANK'`.
  2. Mesin rekonsiliasi mencocokkan transaksi perdagangan saham dengan mutasi kas RDN yang bersesuaian berdasarkan tanggal efektif ($T+0$ hingga $T+3$) dan nilai nominal kas bersih.
  3. Jika transaksi cocok, status diperbarui menjadi `MATCHED` dan memicu pembuatan jurnal transaksi resmi pada buku besar.
  4. Transaksi beli saham yang belum menemukan mutasi kas pendamping tetap berstatus `PENDING` dan ditandai sebagai posisi *Pending Clearance*.
  5. Transaksi dengan selisih nominal di luar aturan pembulatan ditandai sebagai `FLAGGED` untuk tinjauan manual.

---

### TICKET-05: Modul Ledger Pembukuan Ganda (Double-Entry Engine)
* **ID:** TICKET-05
* **Title:** Double-Entry General Ledger and Transaction Booking
* **Scope/Files:**
  * `src/ledger/transaction_service.*`
  * `src/ledger/balance_verifier.*`
* **Prerequisites:** TICKET-01
* **Acceptance Criteria:**
  1. Setiap entri jurnal memverifikasi rumus keseimbangan akuntansi dasar secara ketat:
     $$\sum \text{Debit Amount} = \sum \text{Credit Amount}$$
  2. Transaksi deposit dan penarikan kas RDN tercatat dengan benar antara akun `ASSET:RDN_CASH` dan akun modal/ekuitas penyeimbang.
  3. Seluruh pencatatan moneter menggunakan tipe data `INTEGER` (satuan rupiah penuh tanpa representasi desimal mengambang).
  4. Modul memvalidasi ketersediaan saldo kas RDN sebelum mengeksekusi pembelian saham berstatus *Settled* guna mencegah saldo kas bernilai negatif.

---

### TICKET-06: Engine Penjualan Saham Multi-Leg & Dekomposisi Biaya BEI
* **ID:** TICKET-06
* **Title:** Multi-Leg Sell Journaling and Tax/Fee Decomposition
* **Scope/Files:**
  * `src/ledger/sell_execution_service.*`
* **Prerequisites:** TICKET-05
* **Acceptance Criteria:**
  1. Eksekusi jual menguraikan nilai transaksi bruto menjadi jurnal multi-kaki (*multi-leg entry*):
     * Debit `ASSET:RDN_CASH` senilai penerimaan kas bersih.
     * Debit `EXPENSE:BROKER_FEE` senilai komisi broker beserta PPN.
     * Debit `EXPENSE:EXCHANGE_LEVY` senilai pungutan BEI/KPEI/KSEI.
     * Debit `EXPENSE:TAX_PPH_FINAL` senilai tepat $0{,}1\%$ dari nilai transaksi bruto:
       $$\text{PPh Final} = \text{Shares} \times \text{Price} \times 0.001$$
     * Kredit `ASSET:PORTFOLIO:{TICKER}` senilai nilai pokok modal (FIFO cost basis).
     * Kredit/Debit `REVENUE:REALIZED_GAIN_LOSS` sebagai penyeimbang keuntungan atau kerugian bersih.
  2. Transaksi gagal dibukukan jika jumlah debit tidak setara dengan jumlah kredit.

---

### TICKET-07: Mesin Alokasi Lot FIFO & Realisasi Keuntungan (PnL)
* **ID:** TICKET-07
* **Title:** Strict FIFO Lot Matching and Disposal Tracking
* **Scope/Files:**
  * `src/portfolio/fifo_lot_engine.*`
  * `src/portfolio/disposal_service.*`
* **Prerequisites:** TICKET-01, TICKET-05, TICKET-06
* **Acceptance Criteria:**
  1. Pembelian saham baru berstatus *Settled* membuat baris baru pada `stock_lots` dengan status `OPEN`.
  2. Saat penjualan saham parsial maupun total dieksekusi, sistem melikuidasi lembar saham dari lot paling tua terlebih dahulu (*Strict FIFO* berdasarkan `acquired_date`).
  3. Status lot diperbarui menjadi `PARTIALLY_CLOSED` atau `CLOSED` sesuai sisa lembar (`remaining_shares`).
  4. Setiap penutupan lot mencatat baris riwayat pada tabel `lot_disposals` yang memuat tautan `lot_id`, `disposal_entry_id`, lembar yang dilikuidasi, harga modal, harga jual, dan laba/rugi yang direalisasikan.

---

### TICKET-08: Penanganan Aksi Korporasi & Pembulatan Lembar (Floor Rounding)
* **ID:** TICKET-08
* **Title:** Corporate Action Split Processing with Lot-Level Floor Rounding
* **Scope/Files:**
  * `src/corporate_actions/split_processor.*`
* **Prerequisites:** TICKET-05, TICKET-07
* **Acceptance Criteria:**
  1. Modul memproses pencatatan aksi korporasi pemecahan saham (*stock split*) dengan rasio $M : D$.
  2. Seluruh lot aktif (`OPEN`, `PARTIALLY_CLOSED`) untuk emiten terkait disesuaikan kuantitas dan harga pokoknya menggunakan metode pembulatan ke bawah (*floor rounding*):
     $$\text{Adjusted Shares}_k = \left\lfloor \text{Remaining Shares}_k \times \frac{M}{D} \right\rfloor$$
     $$\text{Adjusted Cost}_k = \text{Cost}_k \times \frac{D}{M}$$
  3. Total lembar pecahan yang hilang dihitung secara agregat:
     $$\text{Total Fractional Lost} = \sum \left( \left(\text{Remaining Shares}_k \times \frac{M}{D}\right) - \text{Adjusted Shares}_k \right)$$
  4. Jika terdapat pecahan yang hilang, sistem secara otomatis membukukan entri jurnal penyeimbang:
     * Debit `EXPENSE:CORPORATE_ACTION_ROUNDING`
     * Kredit `ASSET:PORTFOLIO:{TICKER}`
  5. Seluruh pembaruan lot dan pencatatan aksi korporasi dieksekusi dalam satu transaksi atomik.

---

### TICKET-09: Penanganan Dividen Tunai Berbasis Kas (Cash-Basis Realization)
* **ID:** TICKET-09
* **Title:** Pure Cash-Basis Cash Dividend Booking
* **Scope/Files:**
  * `src/corporate_actions/dividend_service.*`
* **Prerequisites:** TICKET-04, TICKET-05
* **Acceptance Criteria:**
  1. Dividen tunai dicatat ke dalam buku besar hanya saat dana efektif masuk ke rekening koran RDN pada tanggal bayar (*Payment Date*).
  2. Jurnal transaksi membukukan:
     * Debit `ASSET:RDN_CASH` senilai dividen bersih yang diterima.
     * Kredit `REVENUE:DIVIDEND_INCOME:{TICKER}`.
  3. Sistem tidak membuat entri piutang akrual pada saat periode *Cum/Ex-Date*.

---

### TICKET-10: Klien API Publik EOD Pasar & Rate-Throttling Resilient Fetcher
* **ID:** TICKET-10
* **Title:** Market Data Fetcher with Rate Limiting, Retry, and Disk Cache Fallback
* **Scope/Files:**
  * `src/market_data/market_api_client.*`
  * `src/market_data/price_repository.*`
* **Prerequisites:** TICKET-01
* **Acceptance Criteria:**
  1. Klien HTTP mengambil data harga penutupan EOD dan deret historis dari Yahoo Finance (menggunakan akhiran ticker `.JK`) secara manual atas pemicu pengguna (*on-demand*).
  2. Menerapkan jeda sekuensial minimal $1500\text{ ms}$ antar permintaan HTTP per emiten.
  3. Menerapkan *Exponential Backoff* (maksimal 3 kali percobaan ulang) jika menjumpai kode status HTTP 429 atau 5xx.
  4. Menyimpan deret harga ganda (`raw_close` dan `adjusted_close`) ke dalam tabel `market_prices_eod`.
  5. Jika penarikan data gagal setelah batas maksimal percobaan, sistem mengembalikan data harga terakhir dari basis data lokal (*stale cache fallback*) disertai tanda peringatan *Stale Data*.

---

### TICKET-11: Modul Input Manual Data Fundamental Kuartalan
* **ID:** TICKET-11
* **Title:** Quarterly Fundamental Data Ingestion Interface and Storage
* **Scope/Files:**
  * `src/fundamentals/fundamental_service.*`
* **Prerequisites:** TICKET-01
* **Acceptance Criteria:**
  1. Modul menerima input manual data fundamental kuartalan per emiten (`ticker`, `fiscal_year`, `fiscal_quarter`, `report_release_date`).
  2. Menyimpan metrik utama (`roe_percentage`, `der_ratio`, `per_ratio`, `pbv_ratio`, `net_profit_margin`) dan payload JSON tambahan ke tabel `fundamental_snapshots`.
  3. Menolak input jika kuartal berada di luar rentang $1 \le \text{quarter} \le 4$ atau jika data untuk kuartal tahun buku yang sama sudah terdaftar (mewajibkan konfirmasi pembaruan eksplisit).

---

### TICKET-12: Mesin Evaluasi Mingguan & Penghasil Sinyal Strategi
* **ID:** TICKET-12
* **Title:** Weekly Strategy Evaluation Engine and Indicator Computation
* **Scope/Files:**
  * `src/analytics/technical_indicators.*`
  * `src/analytics/strategy_evaluator.*`
* **Prerequisites:** TICKET-02, TICKET-10, TICKET-11
* **Acceptance Criteria:**
  1. Menghitung indikator teknikal mingguan (Weekly MA30 dan Weekly RSI14) murni menggunakan deret `adjusted_close`.
  2. Mengevaluasi data fundamental kuartalan aktif terakhir terhadap aturan fundamental pada `rules.yaml` dan menghasilkan status (`PASS`, `WARN`, `FAIL`).
  3. Mengevaluasi posisi harga dan indikator mingguan terhadap aturan teknikal pada `rules.yaml` dan menghasilkan status (`PASS`, `WARN`, `FAIL`).
  4. Memetakan kombinasi status ke sinyal portofolio akhir (`HOLD`, `BUY_MORE`, `REBALANCE`, `EXIT`) berdasarkan matriks aksi.

---

### TICKET-13: Pembuat Snapshot Evaluasi Imutabel (Point-in-Time History)
* **ID:** TICKET-13
* **Title:** Immutable Point-in-Time Evaluation Snapshot Persistence
* **Scope/Files:**
  * `src/analytics/snapshot_recorder.*`
* **Prerequisites:** TICKET-01, TICKET-12
* **Acceptance Criteria:**
  1. Hasil evaluasi akhir pekan dibekukan dan disimpan ke tabel `evaluation_snapshots` dengan kunci unik komposit `(evaluation_date, ticker)`.
  2. Nilai parameter metrik yang dievaluasi dan ambang batas aturan aktif saat itu disimpan lengkap dalam kolom `metrics_evaluated_json`.
  3. Baris snapshot yang telah tersimpan bersifat *read-only* (tidak dapat diubah) meskipun berkas `rules.yaml` dimodifikasi di kemudian hari.

---

### TICKET-14: Penyaji Dasbor Portofolio Mingguan (Dual-View State)
* **ID:** TICKET-14
* **Title:** Weekly Portfolio Dashboard View and Dual-State Aggregator
* **Scope/Files:**
  * `src/views/portfolio_view_service.*`
  * `src/views/dashboard_presenter.*`
* **Prerequisites:** TICKET-04, TICKET-05, TICKET-07, TICKET-12, TICKET-13
* **Acceptance Criteria:**
  1. Menampilkan daftar emiten aktif dengan rincian total lembar, harga rata-rata perolehan FIFO, harga pasar penutupan terakhir, dan keuntungan/kerugian belum terealisasi (*Unrealized PnL*).
  2. Menerapkan pemisahan status *Dual-View*: transaksi yang berstatus *Settled* dihitung dalam *Net Asset Value* (NAV) utama, sedangkan transaksi berstatus *Pending Clearance* ditampilkan dalam tabel terpisah bertanda peringatan *Unsettled / Pending RDN*.
  3. Menampilkan status evaluasi fundamental, teknikal, dan sinyal aksi portofolio mingguan per emiten.
  4. Menampilkan label peringatan jika data harga pasar berasal dari cache usang (*Stale Data Warning*).

---

### TICKET-15: Modul Pencadangan Arsip Atomik (Bundled Archive & Checksum)
* **ID:** TICKET-15
* **Title:** Backup and Restore Engine with SHA-256 Manifest Verification
* **Scope/Files:**
  * `src/backup/archive_service.*`
  * `src/backup/manifest_verifier.*`
* **Prerequisites:** TICKET-01, TICKET-02
* **Acceptance Criteria:**
  1. Perintah pencadangan mengeksekusi *SQLite Vacuum Backup* ke direktori penampungan sementara guna menjamin kekonsistenan berkas basis data.
  2. Modul mengompresi berkas basis data SQLite dan direktori konfigurasi `config/` ke dalam satu berkas arsip terkompresi (`.tar.gz` atau `.zip`).
  3. Menghasilkan berkas `manifest.json` di dalam arsip yang memuat nilai hash `SHA256` dari masing-masing berkas penyusun dan stempel waktu pencadangan.
  4. Prosedur pemulihan (*restore*) memverifikasi kecocokan checksum pada manifest sebelum mengekstrak dan menimpa berkas lokal; proses dibatalkan jika terdapat ketidaksesuaian checksum.