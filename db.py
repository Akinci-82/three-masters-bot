"""Fas 2 — SQLite data layer for Three Masters.

Two feature flags control the migration rollout:
  SHADOW_WRITE_ENABLED = False  →  True in Fas 3 (dual-write alongside JSONL)
  READ_FROM_SQLITE     = False  →  True in Fas 4 (cutover: reads from SQLite)

All public write functions are re-entrant (RLock) and run against WAL-mode SQLite.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

# ── Migration flags ───────────────────────────────────────────────────────────
SHADOW_WRITE_ENABLED: bool = False
READ_FROM_SQLITE:     bool = False

# ── Paths ─────────────────────────────────────────────────────────────────────
_DB_DIR  = Path(__file__).parent / "state"
_DB_PATH = _DB_DIR / "three_masters.db"

# ── Thread safety ─────────────────────────────────────────────────────────────
_LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_DIR.mkdir(exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.row_factory = sqlite3.Row
    return _conn


# ── Operational position columns (read every monitor cycle) ───────────────────
_OP_COLS = (
    "slippage_exited", "max_loss_exited", "weekly_close_exited",
    "earnings_closed", "time_stopped", "partial_done",
    "breakeven_done", "trailing_stop_placed",
)


def init_db() -> None:
    """Create tables and apply migrations. Called once at bot startup."""
    with _LOCK:
        c = _get_conn()
        schema = (Path(__file__).parent / "schema.sql").read_text()
        c.executescript(schema)
        c.commit()
    _log.info("[db] SQLite initialised: %s", _DB_PATH)


# ── Positions ─────────────────────────────────────────────────────────────────

def upsert_position(symbol: str, data: dict) -> None:
    """Shadow-write one position dict to SQLite. No-op if SHADOW_WRITE_ENABLED is False."""
    if not SHADOW_WRITE_ENABLED:
        return
    op: dict = {col: int(bool(data.get(col, False))) for col in _OP_COLS}
    core = {
        "entry_date":       data.get("entry_date", ""),
        "avg_cost":         float(data.get("avg_cost", 0) or 0),
        "initial_qty":      int(data.get("initial_qty", 0) or 0),
        "partial_qty":      int(data.get("partial_qty", 0) or 0),
        "partial_price":    _f(data.get("partial_price")),
        "stop_loss":        float(data.get("stop_loss", 0) or 0),
        "stop_loss_initial":float(data.get("stop_loss_initial", 0) or 0),
        "stop_order_id":    data.get("stop_order_id", "") or "",
        "stop_type":        data.get("stop_type", "") or "",
        "buy_stop":         float(data.get("buy_stop", 0) or 0),
        "composite_score":  float(data.get("composite_score", 0) or 0),
        "quality_score":    int(data.get("quality_score", 0) or 0),
        "last_price":       float(data.get("last_price", 0) or 0),
        "atr_trail_pct":    float(data.get("atr_trail_pct", 0) or 0),
    }
    known = set(_OP_COLS) | set(core)
    extra = {k: v for k, v in data.items() if k not in known}
    row = {"symbol": symbol, **op, **core, "extra_json": json.dumps(extra)}
    cols = ", ".join(row)
    placeholders = ", ".join(f":{k}" for k in row)
    updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "symbol")
    with _LOCK:
        _get_conn().execute(
            f"INSERT INTO positions ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {updates}",
            row,
        )
        _get_conn().commit()


def delete_position(symbol: str) -> None:
    """Remove a closed position. No-op if SHADOW_WRITE_ENABLED is False."""
    if not SHADOW_WRITE_ENABLED:
        return
    with _LOCK:
        _get_conn().execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        _get_conn().commit()


def get_all_positions() -> dict[str, dict]:
    """Return all positions as {symbol: data}. Only used when READ_FROM_SQLITE is True."""
    if not READ_FROM_SQLITE:
        return {}
    with _LOCK:
        rows = _get_conn().execute("SELECT * FROM positions").fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        d = dict(row)
        sym = d.pop("symbol")
        extra = json.loads(d.pop("extra_json", "{}") or "{}")
        for col in _OP_COLS:
            d[col] = bool(d[col])
        d.update(extra)
        result[sym] = d
    return result


# ── Risk state ────────────────────────────────────────────────────────────────

def save_risk_state(state: dict) -> None:
    """Shadow-write risk state. No-op if SHADOW_WRITE_ENABLED is False."""
    if not SHADOW_WRITE_ENABLED:
        return
    with _LOCK:
        _get_conn().execute(
            "INSERT INTO risk_state (id, state_json) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json",
            (json.dumps(state, default=str),),
        )
        _get_conn().commit()


def load_risk_state() -> dict | None:
    """Return risk state dict from SQLite. Returns None if no row exists."""
    with _LOCK:
        row = _get_conn().execute(
            "SELECT state_json FROM risk_state WHERE id=1"
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["state_json"])


# ── Trade journal ─────────────────────────────────────────────────────────────

def append_trade(entry: dict) -> int:
    """Insert trade row; returns rowid. No-op (returns 0) if SHADOW_WRITE_ENABLED is False."""
    if not SHADOW_WRITE_ENABLED:
        return 0
    cols = (
        "ts", "symbol", "avg_cost", "exit_price", "initial_qty",
        "partial_done", "partial_qty", "partial_price", "pnl_pct",
        "pnl_dollar", "r_multiple", "composite_score", "mae_pct",
        "mfe_pct", "portfolio_after", "exit_step", "add_steps",
        "slippage_pct", "days_held", "postmortem",
    )
    row = {c: entry.get(c) for c in cols}
    row["partial_done"] = int(bool(row["partial_done"]))
    placeholders = ", ".join(f":{c}" for c in cols)
    with _LOCK:
        cur = _get_conn().execute(
            f"INSERT OR IGNORE INTO trades ({', '.join(cols)}) VALUES ({placeholders})",
            row,
        )
        _get_conn().commit()
        return cur.lastrowid or 0


def update_postmortem(ts: str, symbol: str, text: str) -> None:
    """Attach postmortem text to an existing trade row."""
    if not SHADOW_WRITE_ENABLED:
        return
    with _LOCK:
        _get_conn().execute(
            "UPDATE trades SET postmortem=? WHERE ts=? AND symbol=?",
            (text, ts, symbol),
        )
        _get_conn().commit()


# ── Equity history ────────────────────────────────────────────────────────────

def record_equity(date_str: str, value: float) -> None:
    """Upsert daily equity snapshot. No-op if SHADOW_WRITE_ENABLED is False."""
    if not SHADOW_WRITE_ENABLED:
        return
    with _LOCK:
        _get_conn().execute(
            "INSERT INTO equity_history (date, value) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET value=excluded.value",
            (date_str, round(value, 2)),
        )
        _get_conn().commit()


# ── API usage ─────────────────────────────────────────────────────────────────

def record_api_usage(row: dict) -> None:
    """Append one token-usage record. No-op if SHADOW_WRITE_ENABLED is False."""
    if not SHADOW_WRITE_ENABLED:
        return
    with _LOCK:
        _get_conn().execute(
            "INSERT INTO api_usage "
            "(ts, date, symbol, tier, model, input_tokens, output_tokens, cost_usd) "
            "VALUES (:ts, :date, :symbol, :tier, :model, "
            ":input_tokens, :output_tokens, :cost_usd)",
            row,
        )
        _get_conn().commit()


# ── Sync audit ────────────────────────────────────────────────────────────────

def record_sync_audit(row: dict) -> None:
    """Append one sync-audit record. No-op if SHADOW_WRITE_ENABLED is False."""
    if not SHADOW_WRITE_ENABLED:
        return
    with _LOCK:
        _get_conn().execute(
            "INSERT INTO sync_audit (ts, payload_json) VALUES (?, ?)",
            (row.get("ts", ""), json.dumps(row, default=str)),
        )
        _get_conn().commit()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
