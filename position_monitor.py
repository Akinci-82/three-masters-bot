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


def _place_stop(symbol: str, qty: int, stop_price: float) -> str | None:
    """Place hard stop. Returns Alpaca order ID on success, None on failure."""
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
        order_id = r.json().get("id")
        _log.info("[monitor] Stop $%.2f placed on %s (%d shares) id=%s",
                  stop_price, symbol, qty, order_id)
        return order_id
    except Exception as e:
        _log.warning("[monitor] place_stop(%s) error: %s", symbol, e)
        return None


def _place_trailing_stop(symbol: str, qty: int, trail_pct: float) -> str | None:
    """Place trailing stop. Returns Alpaca order ID on success, None on failure."""
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
        order_id = r.json().get("id")
        _log.info("[monitor] Trailing stop %.0f%% placed on %s (%d shares) id=%s",
                  trail_val, symbol, qty, order_id)
        return order_id
    except Exception as e:
        _log.warning("[monitor] trailing_stop(%s) error: %s", symbol, e)
        return None


def _stop_order_alive(order_id: str) -> bool:
    """Return True if the Alpaca order exists and is still open/pending."""
    if not order_id:
        return False
    try:
        r = requests.get(
            f"{_alpaca_base()}/orders/{order_id}",
            headers=_alpaca_headers(), timeout=8
        )
        if r.status_code == 404:
            return False
        data = r.json()
        return data.get("status") in ("new", "accepted", "pending_new", "held")
    except Exception:
        return False


# ── Core monitoring logic ─────────────────────────────────────────────────────

def _journal_trade(symbol: str, sym_data: dict, pnl_pct: float, portfolio_value: float) -> None:
    """Append completed trade record to logs/trade_journal.jsonl."""
    import json as _json
    avg_cost    = sym_data.get("avg_cost", 0)
    last_price  = sym_data.get("last_price", avg_cost)
    initial_qty = sym_data.get("initial_qty", 0)
    partial_qty = sym_data.get("partial_qty", 0)
    exit_qty    = initial_qty - partial_qty
    pnl_dollar  = (last_price - avg_cost) * exit_qty if avg_cost > 0 else 0.0
    # Approximate R-multiple using 7% initial risk (trailing stop pct)
    risk_per_share = avg_cost * 0.07
    r_multiple = (last_price - avg_cost) / risk_per_share if risk_per_share > 0 else 0.0

    entry = {
        "ts":              datetime.now().isoformat(),
        "symbol":          symbol,
        "avg_cost":        round(avg_cost, 2),
        "exit_price":      round(last_price, 2),
        "initial_qty":     initial_qty,
        "partial_done":    sym_data.get("partial_done", False),
        "partial_qty":     partial_qty,
        "partial_price":   sym_data.get("partial_price"),
        "pnl_pct":         round(pnl_pct * 100, 2),
        "pnl_dollar":      round(pnl_dollar, 2),
        "r_multiple":      round(r_multiple, 2),
        "portfolio_after": round(portfolio_value, 2),
    }
    journal = os.path.join(os.path.dirname(__file__), "logs", "trade_journal.jsonl")
    try:
        os.makedirs(os.path.dirname(journal), exist_ok=True)
        with open(journal, "a") as jf:
            jf.write(_json.dumps(entry) + "\n")
        _log.info("[monitor] Trade journaled: %s pnl=%.1f%% (%.1fR)",
                  symbol, pnl_pct * 100, r_multiple)
    except Exception as e:
        _log.warning("[monitor] Journal write failed: %s", e)


def _trading_days_held(entry_date_str: str) -> int:
    """Return number of US trading days since entry_date (excluding weekends, not calendar)."""
    try:
        import numpy as np
        entry = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
        today = datetime.now(_ET).date()
        return int(np.busday_count(entry.isoformat(), today.isoformat()))
    except Exception:
        return 0


def check_positions() -> None:
    """Run one monitoring cycle. Called every 15 min during market hours."""
    if not _market_is_open():
        return

    # Sync MUST succeed — never manage positions with unverified state.
    # SyncError means Alpaca is unreachable: skip this cycle entirely.
    from position_sync import sync_all, SyncError
    try:
        sync_all()
    except SyncError as e:
        _log.error("[monitor] SYNC FAILED — skipping cycle: %s", e)
        try:
            import requests, os
            token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            if token and chat_id:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "parse_mode": "Markdown",
                          "text": f"🚨 *Three Masters — Monitor sync FAILED*\n`{e}`\nCycle skipped — positions NOT managed this tick."},
                    timeout=8,
                )
        except Exception:
            pass
        return   # skip entire monitoring cycle — do NOT touch orders

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
            "entry_date":            datetime.now(_ET).strftime("%Y-%m-%d"),
        })
        sym["last_price"] = cur_price   # keep last known price for close_trade P&L

        _log.debug("[monitor] %s  qty=%d  avg=$%.2f  cur=$%.2f  pnl=%.1f%%",
                   symbol, qty, avg_cost, cur_price, pnl_pct * 100)

        # ── Step A: Initial trailing stop (placed once when position first seen) ──
        # If we have a saved order ID, verify it still lives in Alpaca before re-placing.
        if not sym.get("trailing_stop_placed") or (
            sym.get("trailing_stop_placed") and
            sym.get("stop_order_id") and
            not _stop_order_alive(sym["stop_order_id"])
        ):
            _cancel_stop_orders(symbol)
            oid = _place_trailing_stop(symbol, qty, trail_pct)
            if oid:
                sym["trailing_stop_placed"] = True
                sym["stop_order_id"] = oid
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
                # Replace trailing stop for remaining qty — tighter after locking profits
                remaining = qty - sell_qty
                if remaining > 0:
                    tight_trail = cfg.get("trailing_stop_after_partial", 0.05)
                    _cancel_stop_orders(symbol)
                    oid2 = _place_trailing_stop(symbol, remaining, tight_trail)
                    sym["trailing_stop_placed"] = True
                    if oid2:
                        sym["stop_order_id"] = oid2
                    _log.info("[monitor] %s trailing stop tightened to %.0f%% after partial exit",
                              symbol, tight_trail * 100)

        # ── Step C: Move stop to breakeven at +breakeven_trigger (default +8%) ───
        elif pnl_pct >= breakeven_trigger and not sym.get("breakeven_done"):
            breakeven = round(avg_cost, 2)
            remaining = qty - sym.get("partial_qty", 0)
            if remaining > 0:
                _cancel_stop_orders(symbol)
                oid3 = _place_stop(symbol, remaining, breakeven)
                if oid3:
                    sym["breakeven_done"] = True
                    sym["stop_order_id"] = oid3
                    changed = True
                    _log.info("[monitor] %s stop moved to breakeven $%.2f (+%.1f%%)",
                              symbol, breakeven, pnl_pct * 100)

        # ── Step D: Time stop — exit stagnant positions (Minervini 3-4 week rule) ──
        time_stop_days = cfg.get("time_stop_trading_days", 15)
        time_stop_gain = cfg.get("time_stop_min_gain_pct", 0.02)
        entry_date_str = sym.get("entry_date", "")
        if (entry_date_str
                and not sym.get("partial_done")
                and pnl_pct < time_stop_gain):
            days_held = _trading_days_held(entry_date_str)
            if days_held >= time_stop_days:
                remaining = qty - sym.get("partial_qty", 0)
                _log.warning("[monitor] TIME STOP %s — held %d days, pnl=%.1f%%",
                             symbol, days_held, pnl_pct * 100)
                _cancel_stop_orders(symbol)
                if remaining > 0 and _place_market_sell(symbol, remaining):
                    sym["time_stopped"] = True
                    changed = True
                    try:
                        import requests as _req, os as _os
                        tok = _os.getenv("TELEGRAM_BOT_TOKEN", "")
                        cid = _os.getenv("TELEGRAM_CHAT_ID", "")
                        if tok and cid:
                            _req.post(
                                f"https://api.telegram.org/bot{tok}/sendMessage",
                                json={"chat_id": cid, "parse_mode": "Markdown",
                                      "text": (f"⏰ *Time Stop — {symbol}*\n"
                                               f"Held {days_held} trading days, "
                                               f"gain only {pnl_pct*100:+.1f}%\n"
                                               f"Minervini rule: exit stagnant positions")},
                                timeout=8,
                            )
                    except Exception:
                        pass

    # Clean up state for positions that are now closed — call close_trade()
    # so risk_state (portfolio heat, daily P&L, consecutive losses) stays accurate.
    open_syms = {p["symbol"] for p in positions}
    for sym in list(state.keys()):
        if sym not in open_syms:
            sym_data = state.pop(sym)
            changed  = True
            avg_cost  = sym_data.get("avg_cost", 0)
            last_price = sym_data.get("last_price", avg_cost)
            pnl_pct   = ((last_price - avg_cost) / avg_cost) if avg_cost > 0 else 0.0
            try:
                from risk_manager import close_trade
                from broker import get_account
                portfolio_value = get_account()["portfolio_value"]
                close_trade(sym, pnl_pct, portfolio_value)   # start_value read from risk_state
                _journal_trade(sym, sym_data, pnl_pct, portfolio_value)
            except Exception as e:
                _log.warning("[monitor] close_trade %s failed: %s", sym, e)

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
