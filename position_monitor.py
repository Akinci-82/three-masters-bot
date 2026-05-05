"""
Layer 4 — TUDOR JONES (intraday)
Monitors open positions every 15 minutes during US market hours.

Rules:
  - At +8%: replace initial stop with breakeven stop (don't lose a winner)
  - At +15%: sell 50% of position at market (lock in partial profit)
  - After partial exit: place 7% trailing stop on remaining shares
  - Initial trailing stop placed when position first seen
"""
from __future__ import annotations
import json
import logging
import os
import threading
from datetime import datetime, time as dt_time

import pytz
import requests

_log = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_MARKET_OPEN  = dt_time(9, 30)
_MARKET_CLOSE = dt_time(16, 0)

_STATE_FILE = os.path.join(os.path.dirname(__file__), "logs", "monitor_state.json")


def _alpaca_headers() -> dict:
    from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def _alpaca_base() -> str:
    from config import ALPACA_BASE_URL
    return ALPACA_BASE_URL


def _market_is_open() -> bool:
    now_et = datetime.now(_ET).time()
    return _MARKET_OPEN <= now_et < _MARKET_CLOSE


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        _log.warning("[monitor] state save error: %s", e)


# ── Alpaca REST calls ─────────────────────────────────────────────────────────

def _get_positions() -> list[dict]:
    try:
        r = requests.get(
            f"{_alpaca_base()}/positions",
            headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _log.warning("[monitor] get_positions error: %s", e)
        return []


def _get_open_orders(symbol: str) -> list[dict]:
    try:
        r = requests.get(
            f"{_alpaca_base()}/orders",
            params={"status": "open", "symbols": symbol, "limit": 20},
            headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _log.warning("[monitor] get_orders(%s) error: %s", symbol, e)
        return []


def _cancel_order(order_id: str) -> bool:
    try:
        r = requests.delete(
            f"{_alpaca_base()}/orders/{order_id}",
            headers=_alpaca_headers(), timeout=10
        )
        return r.status_code in (200, 204)
    except Exception as e:
        _log.warning("[monitor] cancel_order(%s) error: %s", order_id, e)
        return False


def _cancel_stop_orders(symbol: str) -> None:
    for o in _get_open_orders(symbol):
        if o.get("type") in ("stop", "trailing_stop", "stop_limit"):
            _cancel_order(o["id"])
            _log.debug("[monitor] cancelled %s order %s on %s", o["type"], o["id"], symbol)


def _place_market_sell(symbol: str, qty: int) -> bool:
    try:
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        }
        r = requests.post(
            f"{_alpaca_base()}/orders",
            json=body, headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        _log.info("[monitor] Market sell %d × %s submitted", qty, symbol)
        return True
    except Exception as e:
        _log.warning("[monitor] market_sell(%s, %d) error: %s", symbol, qty, e)
        return False


def _place_stop(symbol: str, qty: int, stop_price: float) -> bool:
    try:
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "stop_price": str(round(stop_price, 2)),
            "time_in_force": "gtc",
        }
        r = requests.post(
            f"{_alpaca_base()}/orders",
            json=body, headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        _log.info("[monitor] Stop $%.2f placed on %s (%d shares)", stop_price, symbol, qty)
        return True
    except Exception as e:
        _log.warning("[monitor] place_stop(%s) error: %s", symbol, e)
        return False


def _place_trailing_stop(symbol: str, qty: int, trail_pct: float) -> bool:
    trail_val = round(trail_pct * 100, 1)
    try:
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "trailing_stop",
            "trail_percent": str(trail_val),
            "time_in_force": "gtc",
        }
        r = requests.post(
            f"{_alpaca_base()}/orders",
            json=body, headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        _log.info("[monitor] Trailing stop %.0f%% placed on %s (%d shares)",
                  trail_val, symbol, qty)
        return True
    except Exception as e:
        _log.warning("[monitor] trailing_stop(%s) error: %s", symbol, e)
        return False


# ── Core monitoring logic ─────────────────────────────────────────────────────

def check_positions() -> None:
    """Run one monitoring cycle. Called every 15 min during market hours."""
    if not _market_is_open():
        return

    # Sync bot state with Alpaca before every monitoring cycle
    try:
        from position_sync import sync_all
        sync_all()
    except Exception as e:
        _log.warning("[monitor] sync_all failed: %s", e)

    positions = _get_positions()
    if not positions:
        return

    from config import MONITOR as cfg
    trail_pct         = cfg.get("trailing_stop_pct", 0.07)
    breakeven_trigger = cfg.get("breakeven_trigger", 0.08)
    partial_trigger   = cfg.get("partial_exit_trigger", 0.15)
    partial_pct       = cfg.get("partial_exit_pct", 0.50)

    state = _load_state()
    changed = False

    for pos in positions:
        symbol    = pos["symbol"]
        qty       = int(float(pos["qty"]))
        avg_cost  = float(pos["avg_entry_price"])
        cur_price = float(pos["current_price"])

        if avg_cost <= 0 or qty <= 0:
            continue

        pnl_pct = (cur_price - avg_cost) / avg_cost

        sym = state.setdefault(symbol, {
            "avg_cost":              avg_cost,
            "initial_qty":           qty,
            "partial_done":          False,
            "breakeven_done":        False,
            "trailing_stop_placed":  False,
        })

        _log.debug("[monitor] %s  qty=%d  avg=$%.2f  cur=$%.2f  pnl=%.1f%%",
                   symbol, qty, avg_cost, cur_price, pnl_pct * 100)

        # ── Step A: Initial trailing stop (placed once when position first seen) ──
        if not sym.get("trailing_stop_placed"):
            _cancel_stop_orders(symbol)
            if _place_trailing_stop(symbol, qty, trail_pct):
                sym["trailing_stop_placed"] = True
                changed = True

        # ── Step B: Partial exit at +partial_trigger (default +15%) ──────────────
        if pnl_pct >= partial_trigger and not sym.get("partial_done"):
            sell_qty = max(1, round(qty * partial_pct))
            if _place_market_sell(symbol, sell_qty):
                sym["partial_done"]       = True
                sym["partial_qty"]        = sell_qty
                sym["partial_price"]      = cur_price
                sym["partial_pnl_pct"]    = round(pnl_pct, 4)
                changed = True
                _log.info("[monitor] ✓ %s partial exit: sold %d @ $%.2f (+%.1f%%)",
                          symbol, sell_qty, cur_price, pnl_pct * 100)
                # Replace trailing stop for remaining qty
                remaining = qty - sell_qty
                if remaining > 0:
                    _cancel_stop_orders(symbol)
                    _place_trailing_stop(symbol, remaining, trail_pct)
                    sym["trailing_stop_placed"] = True

        # ── Step C: Move stop to breakeven at +breakeven_trigger (default +8%) ───
        elif pnl_pct >= breakeven_trigger and not sym.get("breakeven_done"):
            breakeven = round(avg_cost, 2)
            remaining = qty - sym.get("partial_qty", 0)
            if remaining > 0:
                _cancel_stop_orders(symbol)
                if _place_stop(symbol, remaining, breakeven):
                    sym["breakeven_done"] = True
                    changed = True
                    _log.info("[monitor] %s stop moved to breakeven $%.2f (+%.1f%%)",
                              symbol, breakeven, pnl_pct * 100)

    # Clean up state for positions that are now closed
    open_syms = {p["symbol"] for p in positions}
    for sym in list(state.keys()):
        if sym not in open_syms:
            del state[sym]
            changed = True
            _log.info("[monitor] %s closed — removed from state", sym)

    if changed:
        _save_state(state)


# ── Background thread entry point ─────────────────────────────────────────────

def run_monitor(interval_minutes: int = 15,
                stop_event: threading.Event | None = None) -> None:
    """Run the position monitor in a blocking loop. Launch from a daemon thread."""
    if stop_event is None:
        stop_event = threading.Event()

    _log.info("[monitor] Position monitor started (interval=%d min)", interval_minutes)

    while not stop_event.is_set():
        try:
            check_positions()
        except Exception as e:
            _log.error("[monitor] Unexpected error: %s", e, exc_info=True)
        stop_event.wait(interval_minutes * 60)

    _log.info("[monitor] Position monitor stopped")
