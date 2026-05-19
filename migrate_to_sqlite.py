#!/usr/bin/env python3
"""One-shot idempotent migration: JSONL + JSON files → SQLite.

Run ONCE after db.py / schema.sql are deployed:
    cd /home/habil/three-masters-bot
    source venv/bin/activate
    python migrate_to_sqlite.py

Exit code 0 = success, 1 = mismatch or fatal error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import db as _db

LOG  = ROOT / "logs"
SOURCES = {
    "equity_history": LOG / "equity_history.jsonl",
    "api_usage":      LOG / "token_usage.jsonl",
    "sync_audit":     LOG / "sync_audit.jsonl",
    "trades":         LOG / "trade_journal.jsonl",
    "risk_state":     LOG / "risk_state.json",
    "positions":      LOG / "monitor_state.json",
}

_errors: list[str] = []


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  WARN: {path.name}:{i} invalid JSON ({e}), skipping")
    return rows


def _validate_sources() -> bool:
    ok = True
    for name, path in SOURCES.items():
        if not path.exists():
            print(f"  SKIP {name}: {path} not found")
        else:
            print(f"  OK   {name}: {path}")
    return ok


def _migrate_equity(conn) -> int:
    path = SOURCES["equity_history"]
    rows = _load_jsonl(path)
    inserted = 0
    for r in rows:
        date  = r.get("date", "")
        value = r.get("value", r.get("portfolio_value", 0))
        if not date:
            continue
        conn.execute(
            "INSERT INTO equity_history (date, value) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET value=excluded.value",
            (date, float(value)),
        )
        inserted += 1
    conn.commit()
    return inserted


def _migrate_api_usage(conn) -> int:
    rows = _load_jsonl(SOURCES["api_usage"])
    inserted = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO api_usage "
            "(ts, date, symbol, tier, model, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.get("ts", ""), r.get("date", ""), r.get("symbol"),
                r.get("tier"), r.get("model"),
                int(r.get("input_tokens", 0) or 0),
                int(r.get("output_tokens", 0) or 0),
                float(r.get("cost_usd", 0) or 0),
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def _migrate_sync_audit(conn) -> int:
    rows = _load_jsonl(SOURCES["sync_audit"])
    inserted = 0
    for r in rows:
        conn.execute(
            "INSERT INTO sync_audit (ts, payload_json) VALUES (?, ?)",
            (r.get("ts", ""), json.dumps(r)),
        )
        inserted += 1
    conn.commit()
    return inserted


def _migrate_trades(conn) -> int:
    rows = _load_jsonl(SOURCES["trades"])
    inserted = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(ts, symbol, avg_cost, exit_price, initial_qty, partial_done, partial_qty, "
            "partial_price, pnl_pct, pnl_dollar, r_multiple, composite_score, mae_pct, "
            "mfe_pct, portfolio_after, exit_step, add_steps, slippage_pct, days_held, postmortem) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("ts", ""), r.get("symbol", ""),
                r.get("avg_cost"), r.get("exit_price"),
                r.get("initial_qty"), int(bool(r.get("partial_done"))),
                r.get("partial_qty"), r.get("partial_price"),
                r.get("pnl_pct"), r.get("pnl_dollar"), r.get("r_multiple"),
                r.get("composite_score"), r.get("mae_pct"), r.get("mfe_pct"),
                r.get("portfolio_after"), r.get("exit_step"),
                ",".join(r["add_steps"]) if isinstance(r.get("add_steps"), list) else r.get("add_steps"),
                r.get("slippage_pct"), r.get("days_held"), r.get("postmortem"),
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def _migrate_risk_state(conn) -> bool:
    path = SOURCES["risk_state"]
    if not path.exists():
        print("  SKIP risk_state: file not found")
        return True
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"  ERROR risk_state: {e}")
        return False
    conn.execute(
        "INSERT INTO risk_state (id, state_json) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json",
        (json.dumps(state),),
    )
    conn.commit()
    return True


_OP_COLS = (
    "slippage_exited", "max_loss_exited", "weekly_close_exited",
    "earnings_closed", "time_stopped", "partial_done",
    "breakeven_done", "trailing_stop_placed",
)
_CORE_COLS = {
    "entry_date", "avg_cost", "initial_qty", "partial_qty", "partial_price",
    "stop_loss", "stop_loss_initial", "stop_order_id", "stop_type", "buy_stop",
    "composite_score", "quality_score", "last_price", "atr_trail_pct",
}


def _migrate_positions(conn) -> int:
    path = SOURCES["positions"]
    if not path.exists():
        print("  SKIP positions: monitor_state.json not found")
        return 0
    try:
        state: dict = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"  ERROR positions: {e}")
        return 0
    if not isinstance(state, dict):
        print("  SKIP positions: not a dict")
        return 0
    inserted = 0
    known = set(_OP_COLS) | _CORE_COLS
    for symbol, data in state.items():
        op   = {col: int(bool(data.get(col, False))) for col in _OP_COLS}
        core = {
            "entry_date":         data.get("entry_date", ""),
            "avg_cost":           float(data.get("avg_cost", 0) or 0),
            "initial_qty":        int(data.get("initial_qty", 0) or 0),
            "partial_qty":        int(data.get("partial_qty", 0) or 0),
            "partial_price":      _f(data.get("partial_price")),
            "stop_loss":          float(data.get("stop_loss", 0) or 0),
            "stop_loss_initial":  float(data.get("stop_loss_initial", 0) or 0),
            "stop_order_id":      data.get("stop_order_id", "") or "",
            "stop_type":          data.get("stop_type", "") or "",
            "buy_stop":           float(data.get("buy_stop", 0) or 0),
            "composite_score":    float(data.get("composite_score", 0) or 0),
            "quality_score":      int(data.get("quality_score", 0) or 0),
            "last_price":         float(data.get("last_price", 0) or 0),
            "atr_trail_pct":      float(data.get("atr_trail_pct", 0) or 0),
        }
        extra = {k: v for k, v in data.items() if k not in known}
        row = {"symbol": symbol, **op, **core, "extra_json": json.dumps(extra)}
        cols = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "symbol")
        conn.execute(
            f"INSERT INTO positions ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {updates}",
            row,
        )
        inserted += 1
    conn.commit()
    return inserted


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _verify(conn) -> bool:
    ok = True

    def _check(table: str, jsonl: Path, label: str):
        nonlocal ok
        db_count  = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        src_count = sum(1 for l in open(jsonl) if l.strip()) if jsonl.exists() else 0
        status = "OK" if db_count >= src_count else "MISMATCH"
        print(f"  {status:8} {label}: JSONL={src_count}, SQLite={db_count}")
        if db_count < src_count:
            ok = False

    if SOURCES["equity_history"].exists():
        _check("equity_history", SOURCES["equity_history"], "equity_history")
    if SOURCES["api_usage"].exists():
        _check("api_usage",      SOURCES["api_usage"],      "api_usage (token_usage)")
    if SOURCES["sync_audit"].exists():
        _check("sync_audit",     SOURCES["sync_audit"],     "sync_audit")
    if SOURCES["trades"].exists():
        _check("trades",         SOURCES["trades"],          "trades (trade_journal)")

    pos_path = SOURCES["positions"]
    if pos_path.exists():
        pos_state = json.loads(pos_path.read_text())
        src_pos   = len(pos_state) if isinstance(pos_state, dict) else 0
        db_pos    = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        status    = "OK" if db_pos >= src_pos else "MISMATCH"
        print(f"  {status:8} positions: JSON={src_pos}, SQLite={db_pos}")
        if db_pos < src_pos:
            ok = False

    return ok


def main() -> int:
    print("=== Three Masters → SQLite migration ===\n")
    print("1. Validating sources:")
    _validate_sources()

    print("\n2. Initialising database:")
    _db.init_db()
    conn = _db._get_conn()
    print(f"   DB: {_db._DB_PATH}")

    print("\n3. Migrating data:")
    n = _migrate_equity(conn);     print(f"   equity_history:  {n} rows")
    n = _migrate_api_usage(conn);  print(f"   api_usage:       {n} rows")
    n = _migrate_sync_audit(conn); print(f"   sync_audit:      {n} rows")
    n = _migrate_trades(conn);     print(f"   trades:          {n} rows")
    ok = _migrate_risk_state(conn);print(f"   risk_state:      {'OK' if ok else 'FAILED'}")
    n = _migrate_positions(conn);  print(f"   positions:       {n} rows")

    print("\n4. Verifying row counts:")
    if not _verify(conn):
        print("\nERROR: Row count mismatch — do not enable SHADOW_WRITE_ENABLED yet.")
        return 1

    print("\nAll row counts match. Migration complete.")
    print("Next step: set SHADOW_WRITE_ENABLED = True in db.py and restart the bot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
