"""
Telegram two-way command handler.
Polls getUpdates every 5 seconds and responds to bot commands.

Commands:
  /status   — equity, heat, daily P&L, loss streak
  /orders   — pending buy-stop orders
  /positions — open positions with P&L
  /cancel SYMBOL — cancel a specific symbol's buy-stop order
  /help     — list available commands
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

_log = logging.getLogger(__name__)
_POLL_INTERVAL = 5   # seconds between getUpdates calls
_last_update_id = 0


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")

def _chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "")

def _send(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{_token()}/sendMessage",
            json={"chat_id": _chat_id(), "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        _log.warning("[tgcmd] send failed: %s", e)


def _get_updates(offset: int) -> list[dict]:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{_token()}/getUpdates",
            params={"offset": offset, "timeout": 4, "allowed_updates": ["message"]},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []


def _cmd_status() -> str:
    try:
        from risk_manager import get_state
        from broker import get_account
        acct = get_account()
        eq   = acct["portfolio_value"]
        s    = get_state()
        heat = s.get("open_risk_pct", 0) * 100
        dpnl = s.get("daily_pnl_pct", 0) * 100
        loss = s.get("consecutive_losses", 0)
        halt = s.get("trading_halted", False)
        halt_str = f"\n⛔ HALTED: {s.get('halt_reason','')}" if halt else ""
        return (
            f"📊 *Three Masters — Status*\n"
            f"Portfolio: ${eq:,.0f}\n"
            f"Heat: {heat:.1f}% | Day P&L: {dpnl:+.1f}% | Loss streak: {loss}{halt_str}"
        )
    except Exception as e:
        return f"Error fetching status: {e}"


def _cmd_orders() -> str:
    try:
        from broker import get_open_orders
        orders = [o for o in get_open_orders()
                  if o.get("side") == "buy" and o.get("type") == "stop"]
        if not orders:
            return "No pending buy-stop orders."
        lines = [f"⏳ *Pending buy-stops ({len(orders)}):*"]
        for o in orders:
            lines.append(f"  *{o['symbol']}* {int(o['qty'])}sh @ ${o['stop_price']:.2f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching orders: {e}"


def _cmd_positions() -> str:
    try:
        from broker import get_positions
        positions = get_positions()
        if not positions:
            return "No open positions."
        lines = [f"📈 *Open positions ({len(positions)}):*"]
        for p in positions:
            sym      = p["symbol"]
            qty      = int(float(p["qty"]))
            avg      = float(p["avg_entry_price"])
            cur      = float(p["current_price"])
            pnl_pct  = (cur - avg) / avg * 100
            pnl_usd  = (cur - avg) * qty
            tag      = "📈" if pnl_pct >= 0 else "📉"
            lines.append(f"  {tag} *{sym}* {qty}sh  ${cur:.2f}  ({pnl_pct:+.1f}%  ${pnl_usd:+.0f})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching positions: {e}"


def _cmd_cancel(symbol: str) -> str:
    try:
        from broker import cancel_all_orders, get_open_orders
        sym = symbol.strip().upper()
        before = [o for o in get_open_orders()
                  if o.get("symbol") == sym and o.get("side") == "buy"]
        if not before:
            return f"No open buy-stop orders found for {sym}."
        n = cancel_all_orders(sym)
        from risk_manager import get_state, _load, _save
        state = _load()
        state.get("positions_risk", {}).pop(sym, None)
        state["open_risk_pct"] = sum(state.get("positions_risk", {}).values())
        _save(state)
        return f"✅ Cancelled {n} order(s) for *{sym}* and removed from risk state."
    except Exception as e:
        return f"Error cancelling {symbol}: {e}"


def _cmd_help() -> str:
    return (
        "🤖 *Three Masters Bot Commands*\n"
        "/status — equity, heat, P&L\n"
        "/orders — pending buy-stop orders\n"
        "/positions — open positions with P&L\n"
        "/cancel SYMBOL — cancel buy-stop for a symbol\n"
        "/help — this message"
    )


def _handle_update(update: dict) -> None:
    msg = update.get("message", {})
    text = msg.get("text", "").strip()
    from_id = str(msg.get("chat", {}).get("id", ""))

    # Only respond to the configured chat
    if from_id != _chat_id():
        return

    if not text.startswith("/"):
        return

    parts  = text.split(maxsplit=1)
    cmd    = parts[0].lower().split("@")[0]   # strip @botname suffix
    arg    = parts[1] if len(parts) > 1 else ""

    if cmd == "/status":
        _send(_cmd_status())
    elif cmd == "/orders":
        _send(_cmd_orders())
    elif cmd == "/positions":
        _send(_cmd_positions())
    elif cmd == "/cancel":
        if not arg:
            _send("Usage: /cancel SYMBOL  (e.g. /cancel BG)")
        else:
            _send(_cmd_cancel(arg))
    elif cmd == "/help":
        _send(_cmd_help())
    else:
        _send(f"Unknown command: {cmd}\nTry /help")


def _poll_loop(stop_event: threading.Event) -> None:
    global _last_update_id
    _log.info("[tgcmd] Telegram command listener started")
    while not stop_event.is_set():
        try:
            updates = _get_updates(_last_update_id + 1)
            for upd in updates:
                _last_update_id = max(_last_update_id, upd.get("update_id", 0))
                _handle_update(upd)
        except Exception as e:
            _log.warning("[tgcmd] poll error: %s", e)
        stop_event.wait(_POLL_INTERVAL)
    _log.info("[tgcmd] Telegram command listener stopped")


def start(stop_event: threading.Event) -> threading.Thread:
    """Start Telegram command listener in background thread."""
    if not _token() or not _chat_id():
        _log.info("[tgcmd] No Telegram credentials — command listener disabled")
        return None
    t = threading.Thread(target=_poll_loop, args=(stop_event,), daemon=True, name="tg-commands")
    t.start()
    return t
