"""Fas 3 — Parity checker for shadow-write validation.

Compares JSONL (source of truth) against SQLite mirror. Called in a background
try/except at the end of each monitor cycle — never blocks the main path.
Divergences are written to logs/parity_errors.jsonl and trigger a Telegram
alert (at most once per hour to avoid flooding).

Run manually for a one-shot check:
    python parity_check.py --manual
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_last_alert_ts: float = 0.0
_ALERT_INTERVAL = 3600.0  # seconds

LOG_DIR = Path(__file__).parent / "logs"
_PARITY_LOG = LOG_DIR / "parity_errors.jsonl"


def _float_eq(a, b) -> bool:
    """Tolerant float comparison: |a-b| < 1e-6 * max(|a|, 1)."""
    try:
        fa, fb = float(a), float(b)
        return abs(fa - fb) < 1e-6 * max(abs(fa), 1.0)
    except (TypeError, ValueError):
        return a == b


def _load_jsonl(path: Path, tail: int | None = None) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows[-tail:] if tail else rows


def _write_error(errors: list[dict]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with open(_PARITY_LOG, "a") as f:
        for e in errors:
            e["ts"] = datetime.now(timezone.utc).isoformat()
            f.write(json.dumps(e) + "\n")


def _maybe_alert(n: int) -> None:
    global _last_alert_ts
    now = time.monotonic()
    with _LOCK:
        if now - _last_alert_ts < _ALERT_INTERVAL:
            return
        _last_alert_ts = now
    try:
        from notifications import _tg
        _tg(f"⚠️ Parity check: {n} divergence(s) — granska logs/parity_errors.jsonl")
    except Exception:
        pass


# ── Check functions ───────────────────────────────────────────────────────────

def check_positions() -> list[dict]:
    """Compare monitor_state.json vs SQLite positions table."""
    import db as _db
    errors: list[dict] = []

    jsonl_path = LOG_DIR / "monitor_state.json"
    if not jsonl_path.exists():
        return errors
    try:
        json_state: dict = json.loads(jsonl_path.read_text())
    except Exception:
        return errors
    if not isinstance(json_state, dict):
        return errors

    db_state = _db.get_all_positions() if _db.READ_FROM_SQLITE else {}
    # During shadow-write (not yet cutover) we query db directly
    if not _db.READ_FROM_SQLITE:
        try:
            conn = _db._get_conn()
            rows = conn.execute("SELECT * FROM positions").fetchall()
            db_state = {}
            for row in rows:
                d = dict(row)
                sym = d.pop("symbol")
                extra = json.loads(d.pop("extra_json", "{}") or "{}")
                for col in _db._OP_COLS:
                    d[col] = bool(d[col])
                d.update(extra)
                db_state[sym] = d
        except Exception as e:
            errors.append({"check": "positions", "error": str(e)})
            return errors

    json_syms = set(json_state)
    db_syms   = set(db_state)

    for sym in json_syms - db_syms:
        errors.append({"check": "positions", "sym": sym, "issue": "missing_in_sqlite"})
    for sym in db_syms - json_syms:
        errors.append({"check": "positions", "sym": sym, "issue": "extra_in_sqlite"})

    for sym in json_syms & db_syms:
        jd, dd = json_state[sym], db_state[sym]
        check_fields = list(_db._OP_COLS) + [
            "avg_cost", "initial_qty", "partial_qty", "stop_loss",
            "stop_order_id", "buy_stop", "composite_score",
        ]
        for field in check_fields:
            jv = jd.get(field)
            dv = dd.get(field)
            if jv is None and dv is None:
                continue
            if not _float_eq(jv, dv):
                errors.append({
                    "check": "positions", "sym": sym, "field": field,
                    "json": jv, "sqlite": dv,
                })
    return errors


def check_risk_state() -> list[dict]:
    """Compare risk_state.json vs SQLite risk_state table."""
    import db as _db
    errors: list[dict] = []

    json_path = LOG_DIR / "risk_state.json"
    if not json_path.exists():
        return errors
    try:
        json_state: dict = json.loads(json_path.read_text())
    except Exception:
        return errors

    db_state = _db.load_risk_state()
    if db_state is None:
        errors.append({"check": "risk_state", "issue": "no_row_in_sqlite"})
        return errors

    check_fields = [
        "date", "daily_pnl_pct", "portfolio_ath", "open_risk_pct",
        "consecutive_losses", "trading_halted", "halt_reason",
    ]
    for field in check_fields:
        jv = json_state.get(field)
        dv = db_state.get(field)
        if not _float_eq(jv, dv):
            errors.append({
                "check": "risk_state", "field": field,
                "json": jv, "sqlite": dv,
            })

    j_pos = json_state.get("positions_risk", {})
    d_pos = db_state.get("positions_risk", {})
    for sym in set(j_pos) | set(d_pos):
        jv = j_pos.get(sym)
        dv = d_pos.get(sym)
        if not _float_eq(jv, dv):
            errors.append({
                "check": "risk_state.positions_risk", "sym": sym,
                "json": jv, "sqlite": dv,
            })
    return errors


def check_trade_count() -> list[dict]:
    """Compare trade_journal.jsonl row count vs SQLite trades table."""
    import db as _db
    errors: list[dict] = []

    jsonl_path = LOG_DIR / "trade_journal.jsonl"
    jsonl_rows = _load_jsonl(jsonl_path)
    jsonl_count = len(jsonl_rows)

    try:
        db_count = _db._get_conn().execute(
            "SELECT count(id) FROM trades"
        ).fetchone()[0]
    except Exception as e:
        errors.append({"check": "trades", "error": str(e)})
        return errors

    if db_count < jsonl_count:
        errors.append({
            "check": "trades",
            "issue": "count_mismatch",
            "jsonl": jsonl_count,
            "sqlite": db_count,
        })
        return errors

    # spot-check last 5 trades
    for row in jsonl_rows[-5:]:
        ts = row.get("ts", "")
        sym = row.get("symbol", "")
        db_row = _db._get_conn().execute(
            "SELECT pnl_pct FROM trades WHERE ts=? AND symbol=?", (ts, sym)
        ).fetchone()
        if db_row is None:
            errors.append({
                "check": "trades", "issue": "missing_row",
                "ts": ts, "sym": sym,
            })
        elif not _float_eq(db_row[0], row.get("pnl_pct")):
            errors.append({
                "check": "trades", "issue": "pnl_mismatch",
                "ts": ts, "sym": sym,
                "json": row.get("pnl_pct"), "sqlite": db_row[0],
            })
    return errors


def check_equity_history() -> list[dict]:
    """Compare equity_history.jsonl vs SQLite equity_history table."""
    import db as _db
    errors: list[dict] = []

    jsonl_path = LOG_DIR / "equity_history.jsonl"
    jsonl_rows = _load_jsonl(jsonl_path)
    jsonl_count = len(jsonl_rows)

    try:
        db_count = _db._get_conn().execute(
            "SELECT count(date) FROM equity_history"
        ).fetchone()[0]
        db_last = _db._get_conn().execute(
            "SELECT date, value FROM equity_history ORDER BY date DESC LIMIT 1"
        ).fetchone()
    except Exception as e:
        errors.append({"check": "equity_history", "error": str(e)})
        return errors

    if db_count < jsonl_count:
        errors.append({
            "check": "equity_history",
            "issue": "count_mismatch",
            "jsonl": jsonl_count,
            "sqlite": db_count,
        })

    if jsonl_rows and db_last:
        j_last = jsonl_rows[-1]
        if not _float_eq(j_last.get("value"), db_last[1]):
            errors.append({
                "check": "equity_history", "issue": "latest_value_mismatch",
                "json_date": j_last.get("date"), "json_val": j_last.get("value"),
                "sqlite_date": db_last[0], "sqlite_val": db_last[1],
            })
    return errors


# ── Public entry point ────────────────────────────────────────────────────────

def run_all() -> None:
    """Run all checks in a background try/except. Never raises."""
    try:
        import db as _db
        if not _db.SHADOW_WRITE_ENABLED:
            return
        errors: list[dict] = []
        errors.extend(check_positions())
        errors.extend(check_risk_state())
        errors.extend(check_trade_count())
        errors.extend(check_equity_history())
        if errors:
            _write_error(errors)
            _log.warning("[parity] %d divergence(s) — see %s", len(errors), _PARITY_LOG)
            _maybe_alert(len(errors))
        else:
            _log.debug("[parity] all checks passed")
    except Exception as e:
        _log.debug("[parity] check error: %s", e)


# ── CLI one-shot ──────────────────────────────────────────────────────────────

def _cli_manual() -> None:
    import db as _db
    _db.SHADOW_WRITE_ENABLED = True  # force-enable for manual run
    errors: list[dict] = []
    for name, fn in [
        ("positions",      check_positions),
        ("risk_state",     check_risk_state),
        ("trade_count",    check_trade_count),
        ("equity_history", check_equity_history),
    ]:
        e = fn()
        if e:
            print(f"FAIL  {name}: {len(e)} error(s)")
            for err in e:
                print(f"      {err}")
        else:
            print(f"OK    {name}")
        errors.extend(e)
    if errors:
        _write_error(errors)
        print(f"\n{len(errors)} divergence(s) written to {_PARITY_LOG}")
    else:
        print("\nAll parity checks passed.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    if "--manual" in sys.argv:
        _cli_manual()
    else:
        print("Usage: python parity_check.py --manual")
