"""
Position Sync — reconciles bot state with Alpaca ground truth.

Run at:
  - Bot startup (run_daily)
  - Every monitoring cycle (position_monitor)

Detects and fixes:
  Ghost order  — risk_state has entry, Alpaca has neither order nor position
  Ghost pos    — monitor_state has entry, Alpaca has no position
  Orphan pos   — Alpaca has position, bot doesn't track it in risk_state
  Qty mismatch — monitor_state qty differs from Alpaca qty
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

from config import RISK, LOG_DIR

_log = logging.getLogger(__name__)
_MONITOR_STATE = LOG_DIR / "monitor_state.json"
_RISK_FILE     = LOG_DIR / "risk_state.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Main sync function ────────────────────────────────────────────────────────

def sync_all() -> dict:
    """
    Full reconciliation between Alpaca and bot state.
    Returns a summary dict of changes made.
    """
    from broker import get_positions, get_open_orders

    # Ground truth from Alpaca
    try:
        alpaca_positions = get_positions()
        alpaca_orders    = get_open_orders()
    except Exception as e:
        _log.warning("[sync] Alpaca fetch failed: %s", e)
        return {"error": str(e)}

    held_syms   = {p["symbol"] for p in alpaca_positions}
    # Only buy-stop orders count as "reserved" risk — sell-stops don't add new risk
    buy_syms    = {o["symbol"] for o in alpaca_orders if o["side"] == "buy"}
    alpaca_syms = held_syms | buy_syms          # everything Alpaca knows about

    changes = {
        "ghost_orders_removed": [],
        "ghost_positions_removed": [],
        "orphans_added": [],
        "qty_mismatches_fixed": [],
    }

    # ── 1. Reconcile risk_state ───────────────────────────────────────────────
    risk = _load_risk()
    tracked = set(risk.get("positions_risk", {}).keys())

    # Ghosts: bot tracks symbols that Alpaca doesn't know about at all
    ghosts = tracked - alpaca_syms
    if ghosts:
        _log.warning("[sync] Removing ghost risk entries: %s", ghosts)
        for sym in ghosts:
            risk["positions_risk"].pop(sym, None)
        risk["open_risk_pct"] = sum(risk["positions_risk"].values())
        changes["ghost_orders_removed"] = list(ghosts)

    # Orphans: Alpaca has actual positions not tracked in risk_state
    orphans = held_syms - tracked
    if orphans:
        _log.warning("[sync] Adding orphan positions to risk_state: %s", orphans)
        for sym in orphans:
            risk["positions_risk"][sym] = RISK["risk_per_trade_pct"]
        risk["open_risk_pct"] = sum(risk["positions_risk"].values())
        changes["orphans_added"] = list(orphans)

    if ghosts or orphans:
        _save_risk(risk)

    _log.info("[sync] risk_state: %d tracked | open_risk=%.1f%% | "
              "ghosts=%d orphans=%d",
              len(risk["positions_risk"]),
              risk["open_risk_pct"] * 100,
              len(ghosts), len(orphans))

    # ── 2. Reconcile monitor_state ────────────────────────────────────────────
    mon = _load_monitor()
    mon_tracked = set(mon.keys())

    # Ghost positions in monitor: bot monitors a symbol not in Alpaca positions
    ghost_pos = mon_tracked - held_syms
    if ghost_pos:
        _log.warning("[sync] Removing ghost monitor entries: %s", ghost_pos)
        for sym in ghost_pos:
            mon.pop(sym, None)
        changes["ghost_positions_removed"] = list(ghost_pos)

    # Qty mismatches: monitor_state has wrong qty vs Alpaca
    for pos in alpaca_positions:
        sym = pos["symbol"]
        alpaca_qty = int(float(pos["qty"]))
        if sym in mon:
            mon_qty = mon[sym].get("initial_qty", alpaca_qty)
            if mon_qty != alpaca_qty and alpaca_qty > 0:
                _log.warning("[sync] %s qty mismatch: monitor=%d alpaca=%d — fixing",
                             sym, mon_qty, alpaca_qty)
                mon[sym]["initial_qty"] = alpaca_qty
                changes["qty_mismatches_fixed"].append(sym)

    if ghost_pos or changes["qty_mismatches_fixed"]:
        _save_monitor(mon)

    if any(changes.values()):
        _log.info("[sync] Changes: %s", {k: v for k, v in changes.items() if v})
    else:
        _log.debug("[sync] All states consistent — no changes needed")

    return changes


def log_full_state() -> None:
    """Log a human-readable state summary for debugging."""
    from broker import get_positions, get_open_orders
    try:
        positions = get_positions()
        orders    = get_open_orders()
    except Exception as e:
        _log.warning("[sync] Cannot fetch Alpaca state: %s", e)
        return

    risk = _load_risk()
    mon  = _load_monitor()

    _log.info("[sync] ── State snapshot ──────────────────────────────")
    _log.info("[sync] Alpaca positions (%d): %s",
              len(positions), [p["symbol"] for p in positions])
    _log.info("[sync] Alpaca open orders (%d): %s",
              len(orders), [(o["symbol"], o["side"]) for o in orders])
    _log.info("[sync] risk_state tracked: %s | heat=%.1f%%",
              list(risk.get("positions_risk", {}).keys()),
              risk.get("open_risk_pct", 0) * 100)
    _log.info("[sync] monitor_state symbols: %s", list(mon.keys()))
    _log.info("[sync] ─────────────────────────────────────────────────")
