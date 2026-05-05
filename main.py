#!/usr/bin/env python3
"""
Three Masters Bot — Main Orchestrator
Runs daily at 22:30 CEST (after US market close, daily bars finalized).

Flow:
  1. [Simons]      Fetch OHLCV for 500+ stocks, apply Trend Template
  2. [Minervini]   Analyze trend-passed stocks for VCP patterns via Claude AI
  3. [Tudor Jones] Size positions: risk 1-2% of capital per trade
  4. [Execution]   Place GTC buy-stop orders at breakout levels
  5. [Report]      Send Telegram summary + save daily log

Background:
  - Position monitor runs every 15 min during US market hours
    (partial exit at +15%, trailing stop 7%, breakeven at +8%)
  - Watchdog reads logs/heartbeat.json every 15 min — auto-restarts if stale
  - Morning briefing at 15:15 CEST before US market opens
"""
from __future__ import annotations
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import (
    LOG_DIR, REPORT_DIR, CHART_DIR,
    DAILY_TRIGGER_HOUR_CET, DAILY_TRIGGER_MIN_CET,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    LOG_LEVEL, LOG_MAX_MB, LOG_BACKUPS,
    MONITOR,
)

LOG_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)
CHART_DIR.mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
def _setup_logging():
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "three_masters.log",
        maxBytes=LOG_MAX_MB * 1_048_576,
        backupCount=LOG_BACKUPS,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL))
    root.addHandler(handler)
    root.addHandler(console)


_log = logging.getLogger("three_masters")


# ── Telegram ─────────────────────────────────────────────────────────────────
def _tg(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        _log.warning("[tg] failed: %s", e)
        return False


# ── Shutdown ──────────────────────────────────────────────────────────────────
_SHUTDOWN = False
_monitor_stop = threading.Event()


def _signal_handler(sig, frame):
    global _SHUTDOWN
    _log.info("[main] Signal %s — shutting down gracefully.", sig)
    _SHUTDOWN = True
    _monitor_stop.set()


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT,  _signal_handler)


# ── Hard timeout for daily run ────────────────────────────────────────────────
class _ScanTimeout(BaseException):
    """BaseException so it cannot be swallowed by broad except Exception blocks."""
    pass


def _timeout_handler(sig, frame):
    raise _ScanTimeout()


signal.signal(signal.SIGALRM, _timeout_handler)
_SCAN_TIMEOUT_SEC = 1200   # 20 min hard ceiling on the daily VCP scan


# ── Heartbeat ─────────────────────────────────────────────────────────────────
_HEARTBEAT_FILE = LOG_DIR / "heartbeat.json"


def _heartbeat() -> None:
    """Write heartbeat.json so the watchdog knows the process is alive."""
    try:
        _HEARTBEAT_FILE.parent.mkdir(exist_ok=True)
        _HEARTBEAT_FILE.write_text(
            json.dumps({"last_run": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()})
        )
    except Exception as e:
        _log.warning("[heartbeat] Failed: %s", e)


# ── Equity baseline tracking ──────────────────────────────────────────────────
_BASELINE_FILE = LOG_DIR / "equity_baseline.json"


def _load_equity_baseline() -> dict | None:
    try:
        if _BASELINE_FILE.exists():
            return json.loads(_BASELINE_FILE.read_text())
    except Exception:
        pass
    return None


def _save_equity_baseline(value: float) -> None:
    """Called once on first successful account fetch — never overwrites."""
    if _BASELINE_FILE.exists():
        return
    try:
        _BASELINE_FILE.write_text(json.dumps({
            "start_date":  str(date.today()),
            "start_value": round(value, 2),
        }, indent=2))
        _log.info("[equity] Baseline saved: $%.2f on %s", value, date.today())
    except Exception as e:
        _log.warning("[equity] Failed to save baseline: %s", e)


def _equity_return_str(current: float) -> str:
    """Return formatted P&L vs baseline for Telegram messages."""
    baseline = _load_equity_baseline()
    if not baseline or baseline["start_value"] <= 0:
        return ""
    start = baseline["start_value"]
    pct   = (current - start) / start * 100
    arrow = "📈" if pct >= 0 else "📉"
    return f"{arrow} Return since {baseline['start_date']}: {pct:+.1f}% (${current - start:+,.0f})"


# ── Morning briefing (15:15 CEST = 09:15 ET, 15 min before US open) ──────────
_last_briefing_date: date | None = None


def _send_morning_briefing() -> None:
    """Send a brief Telegram message before US market opens."""
    try:
        import pytz, requests
        from config import ALPACA_BASE_URL, ALPACA_API_KEY, ALPACA_SECRET_KEY
        hdrs = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }

        positions = requests.get(f"{ALPACA_BASE_URL}/positions",
                                 headers=hdrs, timeout=10).json()
        orders    = requests.get(f"{ALPACA_BASE_URL}/orders",
                                 params={"status": "open", "limit": 50},
                                 headers=hdrs, timeout=10).json()

        buy_stops = [o for o in orders if isinstance(o, dict)
                     and o.get("type") == "stop" and o.get("side") == "buy"]

        lines = [f"🌅 *Three Masters — Morning Briefing {date.today()}*"]

        if positions:
            lines.append(f"\n*Open positions ({len(positions)}):*")
            for p in positions:
                sym      = p["symbol"]
                qty      = int(float(p["qty"]))
                avg_cost = float(p["avg_entry_price"])
                cur      = float(p["current_price"])
                pnl_pct  = (cur - avg_cost) / avg_cost * 100
                tag      = "📈" if pnl_pct >= 0 else "📉"
                lines.append(f"  {tag} *{sym}* {qty}sh  ${cur:.2f}  ({pnl_pct:+.1f}%)")
        else:
            lines.append("\nNo open positions")

        if buy_stops:
            lines.append(f"\n*Pending buy-stops ({len(buy_stops)}):*")
            for o in buy_stops[:5]:
                lines.append(f"  ⏳ *{o['symbol']}* {o['qty']}sh @ ${float(o.get('stop_price',0)):.2f}")

        _tg("\n".join(lines))
        _log.info("[briefing] Morning briefing sent")
    except Exception as e:
        _log.warning("[briefing] Failed: %s", e)


def _maybe_morning_briefing() -> None:
    global _last_briefing_date
    import pytz
    now = datetime.now(pytz.timezone("Europe/Stockholm"))
    if now.weekday() >= 5:   # skip weekends
        return
    if not (now.hour == 15 and 14 <= now.minute <= 28):
        return
    today = now.date()
    if _last_briefing_date == today:
        return
    _last_briefing_date = today
    _send_morning_briefing()


# ── Weekly performance report (sent after Friday's daily scan) ────────────────
def _send_weekly_report(portfolio_value: float) -> None:
    """Summarise the week and send via Telegram."""
    try:
        today = date.today()
        # Collect this week's reports (Mon–today)
        orders_total = 0
        trend_total  = 0
        vcp_total    = 0
        errors_total = 0
        days_scanned = 0

        for d in range(5):
            day = today - timedelta(days=d)
            rfile = REPORT_DIR / f"{day}.json"
            if not rfile.exists():
                continue
            try:
                r = json.loads(rfile.read_text())
                orders_total += len(r.get("orders_placed", []))
                trend_total  += len(r.get("trend_passed", []))
                vcp_total    += len(r.get("vcp_passed", []))
                errors_total += len(r.get("errors", []))
                days_scanned += 1
            except Exception:
                pass

        ret_line = _equity_return_str(portfolio_value)
        lines = [
            f"📊 *Three Masters — Weekly Summary*",
            f"Week ending {today}",
            f"",
            f"Scans run: {days_scanned}/5 days",
            f"Trend Template passed: {trend_total} (total)",
            f"VCP confirmed: {vcp_total} (total)",
            f"Orders placed: {orders_total}",
        ]
        if errors_total:
            lines.append(f"Errors: {errors_total}")
        lines.append(f"")
        if ret_line:
            lines.append(ret_line)
        lines.append(f"Portfolio: ${portfolio_value:,.0f}")

        _tg("\n".join(lines))
        _log.info("[weekly] Weekly report sent")
    except Exception as e:
        _log.warning("[weekly] Report failed: %s", e)


# ── Main daily run ────────────────────────────────────────────────────────────
def run_daily():
    """Execute the full Three Masters pipeline for today."""
    _heartbeat()

    today = str(date.today())
    _log.info("=" * 70)
    _log.info("  THREE MASTERS BOT — Daily Run %s", today)
    _log.info("  Simons · Minervini · Tudor Jones")
    _log.info("=" * 70)

    report = {
        "date": today,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "trend_passed": [],
        "vcp_passed": [],
        "orders_placed": [],
        "errors": [],
    }

    # ── Account check ─────────────────────────────────────────────────────────
    try:
        from broker import get_account, get_positions, get_open_orders
        acct = get_account()
        portfolio_value = acct["portfolio_value"]
        cash = acct["cash"]
        positions = get_positions()
        _log.info("[main] Account: $%.2f portfolio | $%.2f cash | %d positions",
                  portfolio_value, cash, len(positions))

        _save_equity_baseline(portfolio_value)
        ret_line = _equity_return_str(portfolio_value)

        _tg(f"🎯 *Three Masters* — Daily scan starting\n"
            f"Portfolio: ${portfolio_value:,.0f} | Cash: ${cash:,.0f} | "
            f"Positions: {len(positions)}"
            + (f"\n{ret_line}" if ret_line else ""))
    except Exception as e:
        msg = f"Broker connection failed: {e}"
        _log.error("[main] %s", msg)
        _tg(f"⚠️ *Three Masters* — {msg}")
        report["errors"].append(msg)
        _save_report(report)
        return

    # ── Risk state ────────────────────────────────────────────────────────────
    from risk_manager import check_can_trade, daily_reset, get_state
    daily_reset()
    risk_state = get_state()

    # ── Hard timeout ceiling: 20 min for the entire scan ─────────────────────
    signal.alarm(_SCAN_TIMEOUT_SEC)
    try:
        _run_scan(report, today, portfolio_value, cash, positions)
    except _ScanTimeout:
        msg = "Daily scan timed out after 20 min — check logs"
        _log.error("[main] %s", msg)
        _tg(f"❌ *Three Masters* — {msg}")
        report["errors"].append("scan_timeout")
        _save_report(report)
    finally:
        signal.alarm(0)

    _heartbeat()

    # ── Weekly report on Fridays ──────────────────────────────────────────────
    if datetime.now().weekday() == 4:   # Friday
        _send_weekly_report(portfolio_value)


def _run_scan(report: dict, today: str, portfolio_value: float,
              cash: float, positions: list) -> None:
    """Inner scan — separated so the hard timeout can wrap it cleanly."""

    # ── Layer 1: Simons — Trend Template screening ────────────────────────────
    _log.info("\n[LAYER 1 — SIMONS] Trend Template screening...")
    try:
        from screener import run as screen_universe, load_universe
        symbols = load_universe()
        _log.info("[simons] Universe: %d symbols", len(symbols))
        screen_results = screen_universe(symbols=symbols)
        trend_passed = [r for r in screen_results if r.passed]
        report["trend_passed"] = [r.symbol for r in trend_passed]
        _log.info("[simons] %d/%d passed Trend Template",
                  len(trend_passed), len(screen_results))
        _log.info("[simons] Top 10: %s", [r.symbol for r in trend_passed[:10]])
    except Exception as e:
        _log.exception("[simons] Screening failed: %s", e)
        report["errors"].append(f"screener: {e}")
        _save_report(report)
        return

    if not trend_passed:
        msg = "No stocks passed Trend Template today."
        _log.info("[main] %s", msg)
        _tg(f"📊 *Three Masters*\n{msg}\nMarket may be in a downtrend — no trades.")
        report["summary"] = msg
        _save_report(report)
        return

    # ── Layer 2: Minervini — VCP Analysis ────────────────────────────────────
    _log.info("\n[LAYER 2 — MINERVINI] VCP pattern analysis...")
    try:
        from vcp_analyzer import batch_analyze
        top_candidates = sorted(trend_passed, key=lambda r: -r.rs_rating)[:40]
        vcp_results = batch_analyze(top_candidates, max_symbols=40)
        vcp_passed  = [r for r in vcp_results if r.passed]
        report["vcp_passed"] = [r.symbol for r in vcp_passed]
        _log.info("[minervini] %d/%d have confirmed VCP",
                  len(vcp_passed), len(top_candidates))
    except Exception as e:
        _log.exception("[minervini] VCP analysis failed: %s", e)
        report["errors"].append(f"vcp: {e}")
        _save_report(report)
        return

    if not vcp_passed:
        msg = f"{len(trend_passed)} in uptrend but 0 show VCP pattern today."
        _log.info("[main] %s", msg)
        _tg(f"📊 *Three Masters*\n{msg}")
        report["summary"] = msg
        _save_report(report)
        return

    # ── Layer 3 + Execution: Tudor Jones — Size + Place Orders ────────────────
    _log.info("\n[LAYER 3 — TUDOR JONES] Position sizing & order placement...")
    from risk_manager import position_size, register_trade, check_can_trade, sync_positions
    from broker import place_buy_stop, cancel_all_orders
    from config import RISK

    # Sync risk state with actual Alpaca positions — removes stale entries from test runs
    held_symbols = {p["symbol"] for p in positions}
    sync_positions(held_symbols)

    cancelled = cancel_all_orders()
    if cancelled:
        _log.info("[main] Cancelled %d stale orders", cancelled)

    orders_placed = []
    max_new_pos   = RISK["max_positions"] - len(positions)

    vcp_sorted = sorted(vcp_passed,
                        key=lambda r: (-(1 if r.breakout_volume else 0), -r.confidence))

    for vcp in vcp_sorted:
        if len(orders_placed) >= max_new_pos:
            _log.info("[main] Max positions reached (%d) — stopping.", RISK["max_positions"])
            break

        if vcp.symbol in held_symbols:
            _log.info("[main] %s already held — skipping.", vcp.symbol)
            continue

        can, reason = check_can_trade(portfolio_value, RISK["risk_per_trade_pct"])
        if not can:
            _log.warning("[main] Cannot trade: %s", reason)
            break

        try:
            sizing = position_size(portfolio_value, vcp.breakout_level, vcp.stop_loss)
        except ValueError as e:
            _log.warning("[main] %s sizing error: %s", vcp.symbol, e)
            continue

        if sizing["shares"] < 1:
            _log.info("[main] %s too expensive for risk budget — skip", vcp.symbol)
            continue

        if sizing["notional"] > cash * 0.95:
            _log.info("[main] %s notional $%.0f > cash $%.0f — skip",
                      vcp.symbol, sizing["notional"], cash)
            continue

        # Skip if stock already above breakout level — it's broken out, buy-stop won't fire
        if vcp.current_price >= vcp.breakout_level * 1.005:
            _log.info("[main] %s already above breakout ($%.2f >= $%.2f) — already broke out, skip",
                      vcp.symbol, vcp.current_price, vcp.breakout_level)
            continue

        buy_order = place_buy_stop(vcp.symbol, sizing["shares"], vcp.breakout_level)
        if not buy_order:
            continue

        # Note: sell-stop placed by position_monitor after buy fills (avoids Alpaca wash-trade block)
        register_trade(vcp.symbol, sizing["risk_pct"])

        order_rec = {
            "symbol":         vcp.symbol,
            "shares":         sizing["shares"],
            "buy_stop":       vcp.breakout_level,
            "stop_loss":      vcp.stop_loss,
            "target":         sizing["target_price"],
            "risk_amount":    sizing["risk_amount"],
            "risk_pct":       sizing["risk_pct"],
            "rr_ratio":       sizing["rr_ratio"],
            "vcp_confidence": vcp.confidence,
            "breakout_vol":   vcp.breakout_volume,
            "last_candle":    vcp.last_candle,
            "vcp_notes":      vcp.ai_reasoning[:100],
            "buy_order_id":   buy_order.get("id"),
            "sl_order_id":    None,  # placed by position_monitor after fill
        }
        orders_placed.append(order_rec)
        cash -= sizing["notional"]
        held_symbols.add(vcp.symbol)

        vol_tag = " 🔥" if vcp.breakout_volume else ""
        _log.info(
            "[main] ✅ %s | %d sh | buy-stop=$%.2f | SL=$%.2f | TP=$%.2f | "
            "risk=$%.0f (%.1f%%) | candle=%s%s",
            vcp.symbol, sizing["shares"], vcp.breakout_level, vcp.stop_loss,
            sizing["target_price"], sizing["risk_amount"], sizing["risk_pct"] * 100,
            vcp.last_candle, vol_tag,
        )

    report["orders_placed"] = orders_placed
    report["completed_at"]  = datetime.now(timezone.utc).isoformat()

    _save_report(report)
    _send_daily_summary(report, len(trend_passed), len(vcp_passed), portfolio_value)


def _save_report(report: dict):
    today = report.get("date", str(date.today()))
    path  = REPORT_DIR / f"{today}.json"
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log.info("[main] Report saved: %s", path)


def _send_daily_summary(report: dict, trend_n: int, vcp_n: int, portfolio: float):
    orders   = report.get("orders_placed", [])
    ret_line = _equity_return_str(portfolio)
    lines    = [
        f"📈 *Three Masters — {report['date']}*",
        f"Trend passed: {trend_n} | VCP confirmed: {vcp_n}",
        f"Orders placed: {len(orders)}",
        f"Portfolio: ${portfolio:,.0f}",
    ]
    if ret_line:
        lines.append(ret_line)
    lines.append("")

    for o in orders:
        vol_tag = " 🔥" if o.get("breakout_vol") else ""
        lines.append(
            f"  🎯 *{o['symbol']}* {o['shares']}sh @ ${o['buy_stop']:.2f}{vol_tag}\n"
            f"     SL=${o['stop_loss']:.2f} | TP=${o['target']:.2f} | "
            f"Risk=${o['risk_amount']:.0f} ({o['risk_pct']*100:.1f}%) | {o['rr_ratio']:.1f}R\n"
            f"     candle={o.get('last_candle','?')} | _{o['vcp_notes'][:80]}_"
        )
    if not orders:
        lines.append("No new orders — conditions not met.")
    if report.get("errors"):
        lines.append(f"\n⚠️ Errors: {'; '.join(report['errors'])}")
    _tg("\n".join(lines))


# ── Scheduler ─────────────────────────────────────────────────────────────────
def _seconds_until_trigger() -> int:
    """Seconds until next 22:30 CEST trigger, skipping weekends."""
    import pytz
    cet     = pytz.timezone("Europe/Stockholm")
    now_cet = datetime.now(cet)
    target  = now_cet.replace(
        hour=DAILY_TRIGGER_HOUR_CET, minute=DAILY_TRIGGER_MIN_CET,
        second=0, microsecond=0,
    )
    if now_cet >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return int((target - now_cet).total_seconds())


# ── Position monitor background thread ───────────────────────────────────────
def _start_position_monitor() -> threading.Thread | None:
    if not MONITOR.get("enabled", True):
        return None
    from position_monitor import run_monitor
    interval = MONITOR.get("interval_minutes", 15)
    t = threading.Thread(
        target=run_monitor,
        args=(interval, _monitor_stop),
        daemon=True,
        name="position-monitor",
    )
    t.start()
    _log.info("[main] Position monitor started (every %d min during market hours)", interval)
    return t


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    _setup_logging()
    _log.info("=" * 70)
    _log.info("  Three Masters Bot — Starting")
    _log.info("  Daily scan:      %02d:%02d CEST (after US close)",
              DAILY_TRIGGER_HOUR_CET, DAILY_TRIGGER_MIN_CET)
    _log.info("  Morning briefing: 15:15 CEST (before US open)")
    _log.info("  Position monitor: every %d min during market hours",
              MONITOR.get("interval_minutes", 15))
    _log.info("  Watchdog interval: 15 min (reads logs/heartbeat.json)")
    _log.info("=" * 70)

    if "--run-now" in sys.argv:
        _log.info("[main] --run-now flag — executing immediately")
        run_daily()
        return

    _start_position_monitor()
    _heartbeat()

    while not _SHUTDOWN:
        wait_sec = _seconds_until_trigger()
        _log.info("[main] Next scan in %dh %dm",
                  wait_sec // 3600, (wait_sec % 3600) // 60)
        _tg(f"⏰ Three Masters — next scan in {wait_sec//3600}h {(wait_sec%3600)//60}m")

        elapsed = 0
        while elapsed < wait_sec and not _SHUTDOWN:
            time.sleep(min(60, wait_sec - elapsed))
            elapsed += 60
            _heartbeat()
            _maybe_morning_briefing()

        if _SHUTDOWN:
            break

        try:
            run_daily()
        except Exception as e:
            _log.exception("[main] Daily run crashed: %s", e)
            _tg(f"❌ Three Masters — daily run crashed: {e}")

    _log.info("[main] Shutdown complete.")


if __name__ == "__main__":
    main()
