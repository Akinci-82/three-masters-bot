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
from datetime import datetime, timezone
from pathlib import Path

import requests

_BASE = Path(__file__).parent
_LOG_DIR = _BASE / "logs"

_log = logging.getLogger(__name__)
_POLL_INTERVAL = 5   # seconds between getUpdates calls
_last_update_id = 0


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")

def _chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "")

def _tg_escape(text: str) -> str:
    """Escape MarkdownV1 special chars in free-text fields (symbols, names)."""
    for ch in ("*", "_", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


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
            lines.append(f"  *{_tg_escape(o.get('symbol', '?'))}* {int(float(o.get('qty', 0)))}sh @ ${float(o.get('stop_price', 0)):.2f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching orders: {e}"


def _cmd_positions() -> str:
    try:
        from broker import get_positions
        import json as _j, os as _o
        positions = get_positions()
        if not positions:
            return "No open positions."

        # Load monitor state for per-position step info
        _state_path = _o.path.join(_o.path.dirname(__file__), "logs", "monitor_state.json")
        _mon_state: dict = {}
        try:
            if _o.path.exists(_state_path):
                with open(_state_path) as _f:
                    _mon_state = _j.loads(_f.read())
        except Exception:
            pass

        lines = [f"📈 *Open positions ({len(positions)}):*"]
        for p in positions:
            sym      = p["symbol"]
            qty      = int(float(p["qty"]))
            avg      = float(p["avg_entry_price"])
            cur      = float(p["current_price"])
            pnl_pct  = (cur - avg) / avg * 100 if avg != 0 else 0.0
            pnl_usd  = (cur - avg) * qty
            tag      = "📈" if pnl_pct >= 0 else "📉"

            # Step status from monitor state
            ms = _mon_state.get(sym, {})
            _steps = []
            if ms.get("step_f_done"):   _steps.append("F✓")
            if ms.get("partial1_done"): _steps.append("B1✓")
            if ms.get("pyramid_done"):  _steps.append("P✓")
            if ms.get("step_f2_done"):  _steps.append("F2✓")
            if ms.get("partial2_done"): _steps.append("B2✓")
            step_str = " ".join(_steps) if _steps else "–"

            # Stop level
            _sl = ms.get("stop_loss", 0.0)
            _sl_str = f"SL${_sl:.2f}" if _sl > 0 else "SL:?"
            # Days held
            _ed = ms.get("entry_date", "")
            _days = ""
            if _ed:
                try:
                    from pandas.tseries.offsets import BDay
                    import pandas as _pd
                    _days = f" d{int((_pd.Timestamp.today() - _pd.Timestamp(_ed)) / BDay(1))}"
                except Exception:
                    pass

            lines.append(
                f"  {tag} *{_tg_escape(sym)}* {qty}sh  ${cur:.2f}  "
                f"({pnl_pct:+.1f}%  ${pnl_usd:+.0f}){_days}\n"
                f"      {_sl_str}  steps: {step_str}"
            )
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
        return f"✅ Cancelled {n} order(s) for *{_tg_escape(sym)}* and removed from risk state."
    except Exception as e:
        return f"Error cancelling {symbol}: {e}"


def _cmd_watchlist() -> str:
    """Show Stage 2 radar candidates from latest scan (trend_candidates without VCP)."""
    try:
        import json as _j, glob as _g, os as _o
        _rdir = _o.path.join(_o.path.dirname(__file__), "reports")
        _files = sorted(_g.glob(_o.path.join(_rdir, "*.json")), reverse=True)
        if not _files:
            return "📡 Ingen scan-rapport hittad ännu."
        with open(_files[0]) as _f:
            _rpt = _j.load(_f)
        _cands = _rpt.get("trend_candidates", [])
        _date  = _rpt.get("date", "?")
        if not _cands:
            return f"📡 Inga Stage 2-kandidater i senaste scan ({_date})."
        _lines = [
            f"📡 *Watchlist — Stage 2 radar ({_date})*",
            f"_{len(_cands)} aktier i uptrend utan VCP-mönster ännu_",
        ]
        for _c in _cands[:15]:
            _sym  = _tg_escape(_c.get("symbol", "?"))
            _rs   = _c.get("rs_rating", 0)
            _pfh  = abs(_c.get("pct_from_high", 0)) * 100
            _sect = _c.get("sector", "")[:12]
            _bd   = []
            if _c.get("three_weeks_tight"): _bd.append("3WT")
            if _c.get("rs_line_leading"):   _bd.append("RS↑")
            if _c.get("unusual_options"):   _bd.append("🐋")
            if _c.get("pead_hold"):         _bd.append("PEAD")
            if _c.get("eps_accelerating"):  _bd.append("EPS↑")
            _b = " ".join(_bd)
            _lines.append(f"  *{_sym}* RS={_rs:.0f} {_pfh:.1f}%↓  {_sect}  {_b}".rstrip())
        return "\n".join(_lines)
    except Exception as _e:
        return f"Fel vid hämtning av watchlist: {_e}"


def _cmd_briefing() -> str:
    """Trigger morning briefing immediately in background thread."""
    try:
        import main as _m, threading as _thr
        def _bg():
            try:
                _m._send_morning_briefing()
            except Exception as _be:
                _send(f"❌ Briefing-fel: {_be}")
        _thr.Thread(target=_bg, daemon=True, name="tg-briefing").start()
        return "🌅 Kör morning briefing… (levereras om några sekunder)"
    except Exception as _e:
        return f"Fel vid triggning av briefing: {_e}"


def _cmd_size(arg: str) -> str:
    """Position size calculator: /size SYMBOL BUY_PRICE STOP_PRICE"""
    _parts = arg.strip().split()
    if len(_parts) < 3:
        return (
            "📐 *Positionsstorlek-kalkylator*\n"
            "Användning: `/size SYMBOL BUY STOP`\n"
            "Exempel: `/size NVDA 500 475`"
        )
    try:
        _sym = _tg_escape(_parts[0].upper())
        _buy = float(_parts[1])
        _sl  = float(_parts[2])
        if _sl >= _buy:
            return "❌ Stop loss måste vara under köp-priset."
        from broker import get_account
        _acct      = get_account()
        _portfolio = float(_acct["portfolio_value"])
        # 1.5% basrisk (standard), 1.75% för score≥7
        _risk_base  = _portfolio * 0.015
        _risk_mid   = _portfolio * 0.0175
        _risk_hi    = _portfolio * 0.02
        _rps        = _buy - _sl
        _sh_base    = max(1, round(_risk_base / _rps))
        _sh_mid     = max(1, round(_risk_mid  / _rps))
        _sh_hi      = max(1, round(_risk_hi   / _rps))
        return (
            f"📐 *Size — {_sym}*\n"
            f"Portfolio: ${_portfolio:,.0f}\n"
            f"Köp ${_buy:.2f} | Stop ${_sl:.2f} | Risk/sh ${_rps:.2f}\n"
            f"\n"
            f"1.5% risk  → *{_sh_base} sh*  (${_sh_base*_buy:,.0f} notional)\n"
            f"1.75% risk → *{_sh_mid} sh*  (${_sh_mid*_buy:,.0f} notional)\n"
            f"2.0% risk  → *{_sh_hi} sh*  (${_sh_hi*_buy:,.0f} notional)"
        )
    except ValueError:
        return "❌ Ogiltiga siffror. Exempel: `/size NVDA 500 475`"
    except Exception as _e:
        return f"Fel: {_e}"


def _cmd_report() -> str:
    """Show summary of latest daily scan report."""
    try:
        import json as _j, glob as _g, os as _o
        _rdir  = _o.path.join(_o.path.dirname(__file__), "reports")
        _files = sorted(_g.glob(_o.path.join(_rdir, "*.json")), reverse=True)
        if not _files:
            return "📋 Ingen scan-rapport hittad ännu."
        with open(_files[0]) as _f:
            _rpt = _j.load(_f)
        _date    = _rpt.get("date", "?")
        _regime  = _rpt.get("regime", "?")
        _vix     = _rpt.get("vix", 0)
        _spy_pct = _rpt.get("spy_pct", 0) * 100
        _n_trend = len(_rpt.get("trend_passed", []))
        _n_vcp   = len(_rpt.get("vcp_passed", []))
        _orders  = _rpt.get("orders_placed", [])
        _summary = _rpt.get("summary", "")
        _errors  = _rpt.get("errors", [])
        _rem     = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}
        _emo     = _rem.get(_regime, "⚪")
        _lines   = [
            f"📋 *Scan-rapport — {_date}*",
            f"{_emo} {_regime}  SPY {_spy_pct:+.1f}%  VIX {_vix:.1f}",
            f"Trend: {_n_trend}  VCP: {_n_vcp}  Ordrar: {len(_orders)}",
        ]
        if _summary:
            _lines.append(f"Status: `{_summary}`")
        if _orders:
            _lines.append("\n*Lagda ordrar:*")
            for _o_r in _orders[:5]:
                _s  = _tg_escape(_o_r.get("symbol", "?"))
                _sc = _o_r.get("composite_score", 0)
                _en = _o_r.get("buy_stop", 0)
                _st = _o_r.get("stop_loss", 0)
                _lines.append(f"  *{_s}*  score {_sc:.1f}  entry ${_en:.2f}  SL ${_st:.2f}")
        _cands = _rpt.get("trend_candidates", [])[:5]
        if _cands:
            _lines.append("\n*Radar (Stage 2, ej VCP):*")
            for _c in _cands:
                _cs = _tg_escape(_c.get("symbol", "?"))
                _cr = _c.get("rs_rating", 0)
                _lines.append(f"  {_cs}  RS={_cr:.0f}")
        if _errors:
            _lines.append(f"\n⚠️ Fel: {', '.join(str(_e) for _e in _errors[:3])}")
        return "\n".join(_lines)
    except Exception as _e:
        return f"Fel vid hämtning av rapport: {_e}"


def _cmd_risk() -> str:
    """Quick risk state: heat, drawdown, streak, next scan time."""
    try:
        import datetime as _dt
        from risk_manager import get_state
        _s      = get_state()
        _heat   = _s.get("open_risk_pct", 0) * 100
        _dpnl   = _s.get("daily_pnl_pct", 0) * 100
        _losses = _s.get("consecutive_losses", 0)
        _wins   = _s.get("consecutive_wins", 0)
        _halted = _s.get("trading_halted", False)
        _halt_r = _s.get("halt_reason", "")
        _regime = _s.get("confirmed_regime", _s.get("regime", "?"))
        _emo    = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}.get(_regime, "⚪")
        # Next scan at 22:30 local time
        _now  = _dt.datetime.now()
        _next = _now.replace(hour=22, minute=30, second=0, microsecond=0)
        if _now >= _next:
            _next += _dt.timedelta(days=1)
        _mins = int((_next - _now).total_seconds() / 60)
        _streak_str = (f"📉 {_losses} förluster i rad" if _losses > 0
                       else (f"📈 {_wins} vinster i rad" if _wins > 0 else "Ingen svit"))
        _halt_str = f"\n⛔ *HALT*: {_halt_r}" if _halted else ""
        return (
            f"⚡ *Risk State*\n"
            f"{_emo} Regim: *{_regime}*\n"
            f"Heat: {_heat:.1f}% / 8%  |  Dag P&L: {_dpnl:+.1f}%\n"
            f"{_streak_str}\n"
            f"Nästa scan: om {_mins // 60}h {_mins % 60}m"
            f"{_halt_str}"
        )
    except Exception as _e:
        return f"Fel vid hämtning av risk-state: {_e}"


def _cmd_live_orders() -> str:
    """List pending live buy-stop orders awaiting /confirm_live."""
    try:
        from broker import is_live
        if not is_live():
            return "ℹ️ Bot kör i paper-läge — inga live-ordrar."
        pending_file = _LOG_DIR / "pending_live_orders.json"
        if not pending_file.exists():
            return "✅ Inga väntande live-ordrar."
        pending = json.loads(pending_file.read_text())
        # Purge expired entries
        now = datetime.now(timezone.utc)
        active = {}
        for sym, o in pending.items():
            try:
                exp = datetime.fromisoformat(o["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if now <= exp:
                    active[sym] = o
            except Exception:
                active[sym] = o
        if not active:
            return "✅ Inga väntande live-ordrar (alla har löpt ut)."
        lines = ["📋 *Väntande live-ordrar* — bekräfta med /confirm\\_live:\n"]
        for sym, o in active.items():
            lines.append(
                f"• *{_tg_escape(sym)}* {o['qty']}sh @ ${o['stop_price']:.2f} "
                f"| Score: {o.get('composite_score', 0):.1f} "
                f"| Conf: {o.get('vcp_confidence', 0)*100:.0f}%\n"
                f"  `/confirm_live {sym} {o['qty']} {o['stop_price']:.2f}`"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Fel: {e}"


def _cmd_confirm_live(arg: str) -> str:
    """Confirm and execute a pending live buy-stop order.

    Usage: /confirm_live SYMBOL QTY PRICE
    Example: /confirm_live NVDA 50 213.40
    """
    try:
        from broker import is_live, place_buy_stop
        if not is_live():
            return "⚠️ Bot kör i paper-läge (ALPACA\\_LIVE=true krävs för live-trading)."
        parts = arg.strip().split()
        if len(parts) != 3:
            return ("Användning: /confirm\\_live SYMBOL QTY PRICE\n"
                    "Exempel: /confirm\\_live NVDA 50 213.40")
        sym = parts[0].upper()
        try:
            qty   = int(parts[1])
            price = float(parts[2])
        except ValueError:
            return "Ogiltigt format — QTY måste vara heltal och PRICE ett decimaltal."

        pending_file = _LOG_DIR / "pending_live_orders.json"
        if not pending_file.exists():
            return f"Ingen väntande order för {sym}."
        pending = json.loads(pending_file.read_text())
        order = pending.get(sym)
        if not order:
            return (f"Ingen väntande order för *{_tg_escape(sym)}*.\n"
                    "Visa alla med /live\\_orders")

        # Check expiry
        try:
            exp = datetime.fromisoformat(order["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                del pending[sym]
                _tmp = pending_file.with_suffix(".json.tmp")
                _tmp.write_text(json.dumps(pending, indent=2))
                _tmp.replace(pending_file)
                return (f"⏰ Order för *{_tg_escape(sym)}* har löpt ut (24h).\n"
                        "Kör /scan för att generera en ny order.")
        except Exception:
            pass

        # Validate qty and price match
        if order["qty"] != qty:
            return (f"Qty matchar inte: förväntad {order['qty']}, fick {qty}.\n"
                    f"Korrekt: `/confirm_live {sym} {order['qty']} {order['stop_price']:.2f}`")
        if abs(order["stop_price"] - price) > 0.02:
            return (f"Pris matchar inte: förväntad ${order['stop_price']:.2f}, fick ${price:.2f}.\n"
                    f"Korrekt: `/confirm_live {sym} {qty} {order['stop_price']:.2f}`")

        # Place the live order
        result = place_buy_stop(sym, qty, price)
        if not result:
            return f"❌ Order för *{_tg_escape(sym)}* misslyckades — se Alpaca-loggar."

        # Register trade in risk manager now that the order is actually placed.
        # This was intentionally deferred from queue time to prevent permanently
        # locking up risk budget for unconfirmed orders.
        risk_pct = float(order.get("risk_pct", 0.0))
        if risk_pct > 0:
            try:
                from risk_manager import register_trade as _reg
                _reg(sym, risk_pct)
            except Exception:
                pass

        # Remove from pending queue (atomic write)
        del pending[sym]
        _tmp = pending_file.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps(pending, indent=2))
        _tmp.replace(pending_file)

        return (f"✅ *LIVE ORDER PLACERAD*\n"
                f"Symbol: *{_tg_escape(sym)}* BUY-STOP {qty}sh @ ${price:.2f}\n"
                f"Order ID: `{result.get('id', '?')}`")
    except Exception as e:
        return f"❌ Fel vid /confirm\\_live: {e}"


def _cmd_help() -> str:
    try:
        from broker import is_live
        live_section = (
            "\n"
            "*Live-trading:*\n"
            "/live\\_orders — visa väntande live-ordrar\n"
            "/confirm\\_live SYMBOL QTY PRICE — bekräfta och skicka live-order\n"
        ) if is_live() else ""
    except Exception:
        live_section = ""
    return (
        "🤖 *Three Masters Bot — Kommandon*\n"
        "\n"
        "*Övervakning:*\n"
        "/status — equity, heat, P&L\n"
        "/risk — snabb riskstatus (heat, drawdown, streak)\n"
        "/report — senaste scan-rapport (regime, VCP, ordrar)\n"
        "/watchlist — Stage 2-radar utan VCP-mönster ännu\n"
        "\n"
        "*Positioner & ordrar:*\n"
        "/positions — öppna positioner med P&L och steg\n"
        "/orders — väntande buy-stop-ordrar\n"
        "/cancel SYMBOL — avboka köp-stop för symbol\n"
        "/size SYMBOL BUY STOP — positionsstorlek-kalkylator\n"
        "\n"
        "*Åtgärder:*\n"
        "/scan — kör dagens VCP-scan manuellt\n"
        "/briefing — utlös morning briefing nu\n"
        + live_section +
        "/help — denna lista"
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
    elif cmd == "/risk":
        _send(_cmd_risk())
    elif cmd == "/report":
        _send(_cmd_report())
    elif cmd == "/watchlist":
        _send(_cmd_watchlist())
    elif cmd == "/orders":
        _send(_cmd_orders())
    elif cmd == "/positions":
        _send(_cmd_positions())
    elif cmd == "/cancel":
        if not arg:
            _send("Anv\u00e4ndning: /cancel SYMBOL  (t.ex. /cancel NVDA)")
        else:
            _send(_cmd_cancel(arg))
    elif cmd == "/size":
        _send(_cmd_size(arg))
    elif cmd == "/briefing":
        _send(_cmd_briefing())
    elif cmd == "/live_orders":
        _send(_cmd_live_orders())
    elif cmd == "/confirm_live":
        if not arg:
            _send("Användning: /confirm\\_live SYMBOL QTY PRICE\nEx: /confirm\\_live NVDA 50 213.40")
        else:
            _send(_cmd_confirm_live(arg))
    elif cmd == "/scan":
        try:
            import main as _main_mod
            if not _main_mod._SCAN_LOCK.acquire(blocking=False):
                _send("\u23f3 En scan k\u00f6rs redan \u2014 resultaten kommer n\u00e4r den \u00e4r klar.")
            else:
                _main_mod._SCAN_LOCK.release()
                _send("\ud83d\udd0d K\u00f6r manuell scan\u2026 (2\u20135 min)")
                import threading as _thr
                def _bg_scan(_send_fn=_send, _m=_main_mod):
                    try:
                        _m.run_daily()
                    except Exception as _e:
                        _send_fn(f"\u274c Scan-fel: {_e}")
                _thr.Thread(target=_bg_scan, daemon=True, name="tg-manual-scan").start()
        except Exception as _scan_ex:
            _send(f"\u274c Kunde inte starta scan: {_scan_ex}")
    elif cmd == "/help":
        _send(_cmd_help())
    else:
        _send(f"Ok\u00e4nt kommando: {cmd}\nPr\u00f6va /help")


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
