"""
Position Sync — Alpaca ↔ bot state reconciliation.

SAFETY CONTRACT (must hold at all times, especially with real money):
  sync_all() NEVER returns successfully without having verified Alpaca state.
  If Alpaca is unreachable, sync_all() raises SyncError — callers must abort.
  No trading or position management is permitted without a successful sync.

Called from:
  run_daily()       — before every scan, blocks trading if sync fails
  check_positions() — before every monitoring cycle, skips cycle if sync fails

Detects and auto-fixes:
  Ghost order   — risk_state entry, no matching Alpaca order or position
  Ghost monitor — monitor_state entry, no matching Alpaca position
  Orphan pos    — Alpaca position with no risk_state entry
  Qty mismatch  — monitor_state qty != Alpaca qty

Every run is written to logs/sync_audit.jsonl for audit trail.
Telegram alert sent for any discrepancy found (not just on failure).
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from config import RISK, LOG_DIR

_log = logging.getLogger(__name__)

_MONITOR_STATE = LOG_DIR / "monitor_state.json"
_RISK_FILE     = LOG_DIR / "risk_state.json"
_AUDIT_LOG     = LOG_DIR / "sync_audit.jsonl"

_BROKER_RETRIES    = 3      # attempts before declaring Alpaca unreachable
_BROKER_RETRY_SEC  = 5      # seconds between retries


class SyncError(RuntimeError):
    """Raised when sync cannot verify state — callers must abort trading."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_risk() -> dict:
    try:
        return json.loads(_RISK_FILE.read_text())
    except Exception:
        return {"positions_risk": {}, "open_risk_pct": 0.0,
                "trading_halted": False, "halt_reason": "",
                "daily_pnl_pct": 0.0, "consecutive_losses": 0}


def _save_risk(data: dict) -> None:
    _RISK_FILE.parent.mkdir(exist_ok=True)
    _RISK_FILE.write_text(json.dumps(data, indent=2, default=str))


def _load_monitor() -> dict:
    try:
        if _MONITOR_STATE.exists():
            return json.loads(_MONITOR_STATE.read_text())
    except Exception:
        pass
    return {}


def _save_monitor(data: dict) -> None:
    _MONITOR_STATE.parent.mkdir(exist_ok=True)
    _MONITOR_STATE.write_text(json.dumps(data, indent=2, default=str))


def _audit(entry: dict) -> None:
    """Append one sync run to the audit log."""
    entry["ts"] = datetime.now().isoformat()
    try:
        _AUDIT_LOG.parent.mkdir(exist_ok=True)
        with open(_AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _alert(msg: str) -> None:
    """Send Telegram alert — never raises."""
    try:
        import requests, os
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=8,
        )
    except Exception:
        pass


def _fetch_alpaca_state() -> tuple[list, list]:
    """
    Fetch positions and open orders from Alpaca with retries.
    Raises SyncError if all attempts fail — never returns stale/empty data silently.
    """
    from broker import get_positions, get_open_orders
    last_exc: Exception | None = None
    for attempt in range(1, _BROKER_RETRIES + 1):
        try:
            positions = get_positions()
            orders    = get_open_orders()
            return positions, orders
        except Exception as e:
            last_exc = e
            _log.warning("[sync] Alpaca fetch attempt %d/%d failed: %s",
                         attempt, _BROKER_RETRIES, e)
            if attempt < _BROKER_RETRIES:
                time.sleep(_BROKER_RETRY_SEC)

    raise SyncError(
        f"Alpaca unreachable after {_BROKER_RETRIES} attempts: {last_exc}"
    )


# ── Main sync function ────────────────────────────────────────────────────────

def sync_all() -> dict:
    """
    Reconcile bot state with Alpaca.

    RAISES SyncError if Alpaca is unreachable — caller must treat this as fatal
    and abort any trading or position management until sync succeeds.

    Returns a summary dict of changes made (empty dict = clean state).
    """
    positions, orders = _fetch_alpaca_state()   # raises SyncError on failure

    held_syms  = {p["symbol"] for p in positions}
    buy_syms   = {o["symbol"] for o in orders if o["side"] == "buy"}
    alpaca_syms = held_syms | buy_syms

    changes: dict = {
        "ghost_orders_removed":    [],
        "ghost_positions_removed": [],
        "orphans_added":           [],
        "qty_mismatches_fixed":    [],
    }

    # ── 1. Reconcile risk_state ───────────────────────────────────────────────
    risk    = _load_risk()
    tracked = set(risk.get("positions_risk", {}).keys())

    ghosts = tracked - alpaca_syms
    if ghosts:
        _log.warning("[sync] GHOST entries removed from risk_state: %s", ghosts)
        for sym in ghosts:
            risk["positions_risk"].pop(sym, None)
        risk["open_risk_pct"] = sum(risk["positions_risk"].values())
        changes["ghost_orders_removed"] = sorted(ghosts)

    orphans = held_syms - tracked
    if orphans:
        _log.warning("[sync] ORPHAN positions added to risk_state: %s", orphans)
        for sym in orphans:
            risk["positions_risk"][sym] = RISK["risk_per_trade_pct"]
        risk["open_risk_pct"] = sum(risk["positions_risk"].values())
        changes["orphans_added"] = sorted(orphans)

    if ghosts or orphans:
        _save_risk(risk)

    # ── 2. Reconcile monitor_state ────────────────────────────────────────────
    mon         = _load_monitor()
    mon_tracked = set(mon.keys())

    ghost_pos = mon_tracked - held_syms
    if ghost_pos:
        _log.warning("[sync] GHOST positions removed from monitor_state: %s", ghost_pos)
        for sym in ghost_pos:
            mon.pop(sym, None)
        changes["ghost_positions_removed"] = sorted(ghost_pos)

    qty_fixed = []
    for pos in positions:
        sym        = pos["symbol"]
        alpaca_qty = int(float(pos["qty"]))
        if sym in mon and alpaca_qty > 0:
            stored_qty = mon[sym].get("initial_qty", alpaca_qty)
            if stored_qty != alpaca_qty:
                _log.warning("[sync] QTY MISMATCH %s: monitor=%d alpaca=%d — fixed",
                             sym, stored_qty, alpaca_qty)
                mon[sym]["initial_qty"] = alpaca_qty
                qty_fixed.append(sym)

    if ghost_pos or qty_fixed:
        _save_monitor(mon)
        changes["qty_mismatches_fixed"] = qty_fixed

    # ── 3. Audit log ─────────────────────────────────────────────────────────
    _audit({
        "alpaca_positions": sorted(held_syms),
        "alpaca_buy_orders": sorted(buy_syms),
        "risk_tracked": sorted(risk.get("positions_risk", {}).keys()),
        "open_risk_pct": round(risk.get("open_risk_pct", 0) * 100, 2),
        "changes": {k: v for k, v in changes.items() if v},
    })

    # ── 4. Alert on any discrepancy ───────────────────────────────────────────
    any_fix = any(changes.values())
    if any_fix:
        lines = ["⚠️ *Three Masters — Sync fixed discrepancies*"]
        if changes["ghost_orders_removed"]:
            lines.append(f"Ghost orders removed: `{changes['ghost_orders_removed']}`")
        if changes["orphans_added"]:
            lines.append(f"Orphan positions added: `{changes['orphans_added']}`")
        if changes["ghost_positions_removed"]:
            lines.append(f"Ghost monitor entries removed: `{changes['ghost_positions_removed']}`")
        if changes["qty_mismatches_fixed"]:
            lines.append(f"Qty mismatches fixed: `{changes['qty_mismatches_fixed']}`")
        lines.append(f"Heat after sync: {risk.get('open_risk_pct',0)*100:.1f}%")
        _alert("\n".join(lines))
        _log.warning("[sync] Discrepancies fixed: %s",
                     {k: v for k, v in changes.items() if v})
    else:
        _log.info("[sync] State verified — positions=%s orders=%s heat=%.1f%%",
                  sorted(held_syms), sorted(buy_syms),
                  risk.get("open_risk_pct", 0) * 100)

    return changes


def log_full_state() -> None:
    """Log a human-readable snapshot — call after sync_all() for debugging."""
    try:
        positions, orders = _fetch_alpaca_state()
    except SyncError as e:
        _log.error("[sync] Cannot log state: %s", e)
        return
    risk = _load_risk()
    mon  = _load_monitor()
    _log.info("[sync] ── State snapshot ──────────────────────────────")
    _log.info("[sync] Alpaca positions (%d): %s",
              len(positions), [p["symbol"] for p in positions])
    _log.info("[sync] Alpaca open orders (%d): %s",
              len(orders), [(o["symbol"], o["side"]) for o in orders])
    _log.info("[sync] risk_state: %s | heat=%.1f%%",
              list(risk.get("positions_risk", {}).keys()),
              risk.get("open_risk_pct", 0) * 100)
    _log.info("[sync] monitor_state: %s", list(mon.keys()))
    _log.info("[sync] ─────────────────────────────────────────────────")
