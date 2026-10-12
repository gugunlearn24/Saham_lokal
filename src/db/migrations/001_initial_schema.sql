PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    account_name TEXT NOT NULL,
    account_type TEXT NOT NULL CHECK (account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')),
    currency TEXT NOT NULL DEFAULT 'IDR',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id TEXT PRIMARY KEY,
    entry_hash TEXT NOT NULL UNIQUE,
    entry_date DATE NOT NULL,
    trade_date DATE,
    source_type TEXT NOT NULL CHECK (source_type IN ('BROKER_CSV', 'BANK_CSV', 'MANUAL', 'DIVIDEND', 'CORP_ACTION')),
    reference_id TEXT,
    description TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS journal_legs (
    leg_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    debit_amount INTEGER NOT NULL DEFAULT 0,
    credit_amount INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (entry_id) REFERENCES journal_entries(entry_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE RESTRICT,
    CHECK ((debit_amount > 0 AND credit_amount = 0) OR (credit_amount > 0 AND debit_amount = 0))
);

CREATE TABLE IF NOT EXISTS stock_lots (
    lot_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    acquired_date DATE NOT NULL,
    original_shares INTEGER NOT NULL CHECK (original_shares > 0),
    remaining_shares INTEGER NOT NULL CHECK (remaining_shares >= 0),
    cost_per_share REAL NOT NULL CHECK (cost_per_share > 0),
    split_adjustment_factor REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'PARTIALLY_CLOSED', 'CLOSED')),
    FOREIGN KEY (entry_id) REFERENCES journal_entries(entry_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS lot_disposals (
    disposal_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL,
    disposal_entry_id TEXT NOT NULL,
    shares_liquidated INTEGER NOT NULL CHECK (shares_liquidated > 0),
    cost_basis_per_share REAL NOT NULL,
    selling_price_per_share REAL NOT NULL,
    realized_pnl INTEGER NOT NULL,
    disposal_date DATE NOT NULL,
    FOREIGN KEY (lot_id) REFERENCES stock_lots(lot_id) ON DELETE RESTRICT,
    FOREIGN KEY (disposal_entry_id) REFERENCES journal_entries(entry_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    action_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('STOCK_SPLIT', 'REVERSE_SPLIT', 'BONUS_SHARES')),
    execution_date DATE NOT NULL,
    ratio_multiplier INTEGER NOT NULL CHECK (ratio_multiplier > 0),
    ratio_divisor INTEGER NOT NULL CHECK (ratio_divisor > 0),
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_prices_eod (
    ticker TEXT NOT NULL,
    price_date DATE NOT NULL,
    raw_close REAL NOT NULL CHECK (raw_close >= 0),
    adjusted_close REAL NOT NULL CHECK (adjusted_close >= 0),
    volume INTEGER,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, price_date)
);

CREATE TABLE IF NOT EXISTS fundamental_snapshots (
    ticker TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_quarter INTEGER NOT NULL CHECK (fiscal_quarter BETWEEN 1 AND 4),
    report_release_date DATE NOT NULL,
    roe_percentage REAL,
    der_ratio REAL,
    per_ratio REAL,
    pbv_ratio REAL,
    net_profit_margin REAL,
    raw_payload_json TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year, fiscal_quarter)
);

CREATE TABLE IF NOT EXISTS evaluation_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    evaluation_date DATE NOT NULL,
    ticker TEXT NOT NULL,
    technical_status TEXT NOT NULL CHECK (technical_status IN ('PASS', 'WARN', 'FAIL')),
    fundamental_status TEXT NOT NULL CHECK (fundamental_status IN ('PASS', 'WARN', 'FAIL')),
    overall_signal TEXT NOT NULL CHECK (overall_signal IN ('HOLD', 'BUY_MORE', 'REBALANCE', 'EXIT')),
    metrics_evaluated_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (evaluation_date, ticker)
);

CREATE TABLE IF NOT EXISTS reconciliation_staging (
    staging_id TEXT PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_lots_ticker_status ON stock_lots(ticker, status);
CREATE INDEX IF NOT EXISTS idx_journal_legs_account ON journal_legs(account_id);
CREATE INDEX IF NOT EXISTS idx_market_prices_lookup ON market_prices_eod(ticker, price_date DESC);
CREATE INDEX IF NOT EXISTS idx_eval_date_ticker ON evaluation_snapshots(evaluation_date, ticker);

INSERT OR IGNORE INTO accounts (account_id, account_name, account_type, currency)
VALUES
    ('ASSET:RDN_CASH', 'RDN Cash', 'ASSET', 'IDR'),
    ('EXPENSE:BROKER_FEE', 'Broker Fee', 'EXPENSE', 'IDR'),
    ('EXPENSE:EXCHANGE_LEVY', 'Exchange Levy', 'EXPENSE', 'IDR'),
    ('EXPENSE:TAX_PPH_FINAL', 'PPh Final Tax', 'EXPENSE', 'IDR'),
    ('EXPENSE:CORPORATE_ACTION_ROUNDING', 'Corporate Action Rounding', 'EXPENSE', 'IDR'),
    ('REVENUE:REALIZED_GAIN_LOSS', 'Realized Gain/Loss', 'REVENUE', 'IDR');
