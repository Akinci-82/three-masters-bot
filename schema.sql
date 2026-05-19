-- Three Masters — SQLite schema (Fas 2)
-- Applied idempotently via init_db() at startup.

CREATE TABLE IF NOT EXISTS schema_version (
    version  INTEGER PRIMARY KEY,
    applied  TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

-- ── Positions ─────────────────────────────────────────────────────────────────
-- Hybrid layout: operational boolean columns + extra_json for rarely-queried flags.
CREATE TABLE IF NOT EXISTS positions (
    symbol                TEXT PRIMARY KEY,

    -- core numeric fields
    entry_date            TEXT    DEFAULT '',
    avg_cost              REAL    DEFAULT 0,
    initial_qty           INTEGER DEFAULT 0,
    partial_qty           INTEGER DEFAULT 0,
    partial_price         REAL,
    stop_loss             REAL    DEFAULT 0,
    stop_loss_initial     REAL    DEFAULT 0,
    stop_order_id         TEXT    DEFAULT '',
    stop_type             TEXT    DEFAULT '',
    buy_stop              REAL    DEFAULT 0,
    composite_score       REAL    DEFAULT 0,
    quality_score         INTEGER DEFAULT 0,
    last_price            REAL    DEFAULT 0,
    atr_trail_pct         REAL    DEFAULT 0,

    -- operational boolean flags (read every monitor cycle)
    slippage_exited       INTEGER DEFAULT 0,
    max_loss_exited       INTEGER DEFAULT 0,
    weekly_close_exited   INTEGER DEFAULT 0,
    earnings_closed       INTEGER DEFAULT 0,
    time_stopped          INTEGER DEFAULT 0,
    partial_done          INTEGER DEFAULT 0,
    breakeven_done        INTEGER DEFAULT 0,
    trailing_stop_placed  INTEGER DEFAULT 0,

    -- all other flags and metadata fields
    extra_json            TEXT    DEFAULT '{}'
);

-- ── Risk state ────────────────────────────────────────────────────────────────
-- Single row (id must be 1). Entire dict stored as JSON for schema-free evolution.
CREATE TABLE IF NOT EXISTS risk_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    state_json TEXT    NOT NULL DEFAULT '{}'
);

-- ── Trade journal ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    trade_date     TEXT    GENERATED ALWAYS AS (substr(ts, 1, 10)) VIRTUAL,
    avg_cost       REAL,
    exit_price     REAL,
    initial_qty    INTEGER,
    partial_done   INTEGER DEFAULT 0,
    partial_qty    INTEGER DEFAULT 0,
    partial_price  REAL,
    pnl_pct        REAL,
    pnl_dollar     REAL,
    r_multiple     REAL,
    composite_score REAL,
    mae_pct        REAL,
    mfe_pct        REAL,
    portfolio_after REAL,
    exit_step      TEXT,
    add_steps      TEXT,
    slippage_pct   REAL,
    days_held      INTEGER,
    postmortem     TEXT,
    UNIQUE (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_date   ON trades (trade_date);

-- ── Equity history ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS equity_history (
    date   TEXT PRIMARY KEY,
    value  REAL NOT NULL
);

-- ── API usage ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    date          TEXT    NOT NULL,
    symbol        TEXT,
    tier          TEXT,
    model         TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd      REAL    DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_usage_date_sym ON api_usage (date, symbol);

-- ── Sync audit ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    payload_json TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sync_audit_ts ON sync_audit (ts);
