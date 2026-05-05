#!/usr/bin/env python3
"""
Three Masters Bot — Main Orchestrator
Runs daily at 22:30 CEST — right after US market close.
Orders placed are GTC buy-stops for the next trading day.

Flow:
  1. [Simons]      Fetch OHLCV for 500+ stocks, apply Trend Template
  2. [Minervini]   Analyze trend-passed stocks for VCP patterns via Claude AI
  3. [Tudor Jones] Size positions: risk 1-2% of capital per trade
  4. [Execution]   Place buy-stop orders at breakout levels
  5. [Report]      Send Telegram summary + save daily log
"""
from __future__ import annotations
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import (
    LOG_DIR, REPORT_DIR, CHART_DIR,
    DAILY_TRIGGER_HOUR_CET, DAILY_TRIGGER_MIN_CET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    LOG_LEVEL, LOG_MAX_MB, LOG_BACKUPS,
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


# ── Shutdown flag ─────────────────────────────────────────────────────────────
_SHUTDOWN = False

def _signal_handler(sig, frame):
    global _SHUTDOWN
    _log.info("[main] Signal %s — shutting down gracefully.", sig)
    _SHUTDOWN = True

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT,  _signal_handler)


# ── Main daily run ────────────────────────────────────────────────────────────
def run_daily():
    """Execute the full Three Masters pipeline for today."""
    today = str(date.today())
    _log.info("=" * 70)
    _log.info("  THREE MASTERS BOT — Daily Run %s", today)
    _log.info("  Simons · Minervini · Tudor Jones")
    _log.info("=" * 70)

    report = {
        "date": today,
        "started_at": datetime.utcnow().isoformat(),
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
        _tg(f"🎯 *Three Masters* — Daily scan starting\n"
            f"Portfolio: ${portfolio_value:,.0f} | Cash: ${cash:,.0f} | "
            f"Positions: {len(positions)}")
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

    # ── Layer 1: Simons — Trend Template screening ────────────────────────────
    _log.info("\n[LAYER 1 — SIMONS] Trend Template screening...")
    try:
        from screener import run as screen_universe, load_universe
        symbols = load_universe()
        _log.info("[simons] Universe: %d symbols", len(symbols))
        screen_results = screen_universe(symbols=symbols)
        trend_passed = [r for r in screen_results if r.passed]
        trend_failed = len(screen_results) - len(trend_passed)
        report["trend_passed"] = [r.symbol for r in trend_passed]
        _log.info("[simons] %d/%d passed Trend Template", len(trend_passed), len(screen_results))
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
        # Limit to top 40 by RS rating to control API costs
        top_candidates = sorted(trend_passed, key=lambda r: -r.rs_rating)[:40]
        vcp_results = batch_analyze(top_candidates, max_symbols=40)
        vcp_passed  = [r for r in vcp_results if r.passed]
        report["vcp_passed"] = [r.symbol for r in vcp_passed]
        _log.info("[minervini] %d/%d have confirmed VCP", len(vcp_passed), len(top_candidates))
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
    from risk_manager import position_size, register_trade
    from broker import place_buy_stop, place_sell_stop, cancel_all_orders
    from config import RISK

    # Cancel any stale buy-stops from previous day
    cancelled = cancel_all_orders()
    if cancelled:
        _log.info("[main] Cancelled %d stale orders", cancelled)

    orders_placed = []
    held_symbols  = {p["symbol"] for p in positions}
    max_new_pos   = RISK["max_positions"] - len(positions)

    # Sort VCP results by confidence × RS_rating (best quality first)
    vcp_sorted = sorted(vcp_passed, key=lambda r: -r.confidence)

    for vcp in vcp_sorted:
        if len(orders_placed) >= max_new_pos:
            _log.info("[main] Max positions reached (%d) — stopping.", RISK["max_positions"])
            break

        if vcp.symbol in held_symbols:
            _log.info("[main] %s already held — skipping.", vcp.symbol)
            continue

        # Check risk limits
        can, reason = check_can_trade(portfolio_value, RISK["risk_per_trade_pct"])
        if not can:
            _log.warning("[main] Cannot trade: %s", reason)
            break

        # Calculate position size
        try:
            sizing = position_size(portfolio_value, vcp.breakout_level, vcp.stop_loss)
        except ValueError as e:
            _log.warning("[main] %s sizing error: %s", vcp.symbol, e)
            continue

        if sizing["shares"] < 1:
            _log.info("[main] %s too expensive for risk budget — skip", vcp.symbol)
            continue

        if sizing["notional"] > cash * 0.95:
            _log.info("[main] %s notional $%.0f > cash $%.0f — skip", vcp.symbol, sizing["notional"], cash)
            continue

        # Place buy-stop at breakout level
        buy_order = place_buy_stop(vcp.symbol, sizing["shares"], vcp.breakout_level)
        if not buy_order:
            continue

        # Place protective stop-loss (GTC)
        sl_order = place_sell_stop(vcp.symbol, sizing["shares"], vcp.stop_loss)

        register_trade(vcp.symbol, sizing["risk_pct"])

        order_rec = {
            "symbol": vcp.symbol,
            "shares": sizing["shares"],
            "buy_stop": vcp.breakout_level,
            "stop_loss": vcp.stop_loss,
            "target": sizing["target_price"],
            "risk_amount": sizing["risk_amount"],
            "risk_pct": sizing["risk_pct"],
            "rr_ratio": sizing["rr_ratio"],
            "vcp_confidence": vcp.confidence,
            "vcp_notes": vcp.ai_reasoning[:100],
            "buy_order_id": buy_order.get("id"),
            "sl_order_id": sl_order.get("id") if sl_order else None,
        }
        orders_placed.append(order_rec)
        cash -= sizing["notional"]
        held_symbols.add(vcp.symbol)

        _log.info("[main] ✅ %s | %d sh | buy-stop=$%.2f | SL=$%.2f | TP=$%.2f | risk=$%.0f (%.1f%%)",
                  vcp.symbol, sizing["shares"], vcp.breakout_level, vcp.stop_loss,
                  sizing["target_price"], sizing["risk_amount"], sizing["risk_pct"] * 100)

    report["orders_placed"] = orders_placed
    report["completed_at"] = datetime.utcnow().isoformat()

    # ── Generate report & notify ──────────────────────────────────────────────
    _save_report(report)
    _send_daily_summary(report, len(trend_passed), len(vcp_passed), portfolio_value)


def _save_report(report: dict):
    today = report.get("date", str(date.today()))
    path  = REPORT_DIR / f"{today}.json"
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log.info("[main] Report saved: %s", path)


def _send_daily_summary(report: dict, trend_n: int, vcp_n: int, portfolio: float):
    orders = report.get("orders_placed", [])
    lines  = [
        f"📈 *Three Masters — {report['date']}*",
        f"Trend passed: {trend_n} | VCP confirmed: {vcp_n}",
        f"Orders placed: {len(orders)}",
        f"Portfolio: ${portfolio:,.0f}",
        "",
    ]
    for o in orders:
        lines.append(
            f"  🎯 *{o['symbol']}* {o['shares']}sh @ ${o['buy_stop']:.2f}\n"
            f"     SL=${o['stop_loss']:.2f} | TP=${o['target']:.2f} | "
            f"Risk=${o['risk_amount']:.0f} ({o['risk_pct']*100:.1f}%) | {o['rr_ratio']:.1f}R\n"
            f"     _{o['vcp_notes'][:80]}_"
        )
    if not orders:
        lines.append("No new orders — conditions not met.")
    if report.get("errors"):
        lines.append(f"\n⚠️ Errors: {'; '.join(report['errors'])}")
    _tg("\n".join(lines))


# ── Scheduler ─────────────────────────────────────────────────────────────────
def _seconds_until_trigger() -> int:
    """Seconds until next 22:30 CEST trigger (after US market close)."""
    import pytz
    cet = pytz.timezone("Europe/Stockholm")
    now_cet = datetime.now(cet)
    target  = now_cet.replace(hour=DAILY_TRIGGER_HOUR_CET, minute=DAILY_TRIGGER_MIN_CET, second=0, microsecond=0)
    if now_cet >= target:
        # Already past today's trigger — schedule for next occurrence
        from datetime import timedelta
        target += timedelta(days=1)

    # Skip weekends: if target lands on Saturday (5) or Sunday (6), push to Monday
    from datetime import timedelta as _td
    while target.weekday() in (5, 6):
        target += _td(days=1)

    return int((target - now_cet).total_seconds())


def main():
    _setup_logging()
    _log.info("=" * 70)
    _log.info("  Three Masters Bot — Starting scheduler")
    _log.info("  Daily trigger: %02d:00 CET", DAILY_TRIGGER_HOUR_CET)
    _log.info("=" * 70)

    # Allow --run-now flag for immediate execution (testing / manual trigger)
    if "--run-now" in sys.argv:
        _log.info("[main] --run-now flag detected — running immediately")
        run_daily()
        return

    while not _SHUTDOWN:
        wait_sec = _seconds_until_trigger()
        next_run = datetime.utcnow().replace(microsecond=0)
        _log.info("[main] Next run in %dh %dm (%s UTC)",
                  wait_sec // 3600, (wait_sec % 3600) // 60, next_run)
        _tg(f"⏰ Three Masters — next scan in {wait_sec//3600}h {(wait_sec%3600)//60}m")

        # Sleep in 60-second intervals so SIGTERM is handled promptly
        elapsed = 0
        while elapsed < wait_sec and not _SHUTDOWN:
            time.sleep(min(60, wait_sec - elapsed))
            elapsed += 60

        if _SHUTDOWN:
            break

        try:
            run_daily()
        except Exception as e:
            _log.exception("[main] Daily run failed: %s", e)
            _tg(f"❌ Three Masters — daily run crashed: {e}")

    _log.info("[main] Shutdown complete.")


if __name__ == "__main__":
    main()
