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
    """Send a morning Telegram briefing before US open: equity, positions, pending orders."""
    try:
        from broker import get_account, get_positions, get_open_orders
        acct      = get_account()
        equity    = acct["portfolio_value"]
        positions = get_positions()
        buy_stops = [o for o in get_open_orders()
                     if o.get("side") == "buy" and o.get("type") in ("stop", "stop_limit")]

        lines = [f"🌅 *Three Masters — Morning Briefing {date.today()}*",
                 f"Portfolio: ${equity:,.0f}"]

        ret_line = _equity_return_str(equity)
        if ret_line:
            lines.append(ret_line)

        # Risk state summary
        from risk_manager import get_state
        rs = get_state()
        heat   = rs.get("open_risk_pct", 0) * 100
        dpnl   = rs.get("daily_pnl_pct", 0) * 100
        losses = rs.get("consecutive_losses", 0)
        lines.append(f"Heat: {heat:.1f}% | Day P&L: {dpnl:+.1f}% | Loss streak: {losses}")

        if positions:
            lines.append(f"\n*Open positions ({len(positions)}):*")
            for p in positions:
                sym      = p["symbol"]
                qty      = int(float(p["qty"]))
                avg_cost = float(p["avg_entry_price"])
                cur      = float(p["current_price"])
                pnl_pct  = (cur - avg_cost) / avg_cost * 100
                pnl_usd  = (cur - avg_cost) * qty
                tag      = "📈" if pnl_pct >= 0 else "📉"
                lines.append(f"  {tag} *{sym}* {qty}sh  ${cur:.2f}  ({pnl_pct:+.1f}%  ${pnl_usd:+.0f})")
        else:
            lines.append("\nNo open positions")

        # ── Pre-market gap check: cancel buy-stops that have already gapped up ──
        from config import MONITOR as _mcfg
        gap_threshold = _mcfg.get("premarket_gap_pct", 0.02)
        gapped_out = []
        if buy_stops:
            try:
                import yfinance as _yf
                for o in list(buy_stops):
                    sym    = o["symbol"]
                    stop_p = float(o.get("stop_price", 0))
                    if stop_p <= 0:
                        continue
                    try:
                        pre = _yf.Ticker(sym).fast_info.get("last_price", None)
                        if pre and pre > stop_p * (1 + gap_threshold):
                            # Stock has gapped above stop — cancel to avoid chasing
                            from broker import cancel_all_orders
                            from risk_manager import get_state as _grs, _load as _lrs, _save as _srs
                            cancel_all_orders(sym)
                            rs = _lrs()
                            rs.get("positions_risk", {}).pop(sym, None)
                            rs["open_risk_pct"] = sum(rs.get("positions_risk", {}).values())
                            _srs(rs)
                            buy_stops.remove(o)
                            gapped_out.append((sym, pre, stop_p))
                            _log.info("[briefing] PRE-MARKET GAP: %s $%.2f >> stop $%.2f — order cancelled",
                                      sym, pre, stop_p)
                    except Exception:
                        pass
            except Exception as e:
                _log.debug("[briefing] Pre-market price check failed: %s", e)

        if gapped_out:
            lines.append(f"")
            lines.append(f"*⚡ Gap-cancelled orders:*")
            for sym, pre, stop in gapped_out:
                pct = (pre - stop) / stop * 100
                lines.append(f"  ❌ *{sym}* pre-market ${pre:.2f} (+{pct:.1f}% above stop ${stop:.2f}) — order cancelled")

        if buy_stops:
            lines.append(f"\n*Pending buy-stops ({len(buy_stops)}):*")
            for o in buy_stops[:6]:
                lines.append(f"  ⏳ *{o['symbol']}* {int(float(o['qty']))}sh @ ${o['stop_price']:.2f}")

        # Market regime
        regime, spy_price, spy_ma200, spy_pct = _check_market_regime()
        regime_emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}[regime]
        lines.append(f"\nMarket: {regime_emoji} {regime.upper()}  SPY ${spy_price:.0f} ({spy_pct:+.1f}% vs MA200)")

        # ── Breakout volume check for held positions ──────────────────────────
        if positions:
            try:
                import yfinance as _yf_bv
                _vol_warns = []
                for _p in positions:
                    _sym_bv = _p["symbol"]
                    try:
                        _dfbv   = _yf_bv.Ticker(_sym_bv).history(period="60d", interval="1d", auto_adjust=True)
                        if len(_dfbv) >= 25:
                            _v1     = float(_dfbv["Volume"].iloc[-1])
                            _avg50  = float(_dfbv["Volume"].tail(50).mean())
                            _ratio  = _v1 / _avg50 if _avg50 > 0 else 1.0
                            if _ratio < 0.80:
                                _vol_warns.append(f"  ⚠️ *{_sym_bv}* weak volume: {_ratio:.1f}× avg — possible distribution")
                    except Exception:
                        pass
                if _vol_warns:
                    lines.append("\n*Volume alerts:*")
                    lines.extend(_vol_warns)
            except Exception:
                pass

        # ── Sector rotation alert — sectors crossing into leadership ──────────────
        try:
            import yfinance as _yf_sr
            _spy_h  = _yf_sr.Ticker("SPY").history(period="60d", interval="1d", auto_adjust=True)["Close"]
            _sr_etfs = {"XLK":"Tech","XLV":"Health","XLF":"Finance","XLE":"Energy",
                        "XLI":"Industrl","XLC":"Comm","XLY":"Cyclical","XLP":"Defensive",
                        "XLU":"Utilities","XLB":"Materials","XLRE":"Real Estate"}
            _sr_alerts = []
            for _etf, _sname in _sr_etfs.items():
                try:
                    _h = _yf_sr.Ticker(_etf).history(period="60d", interval="1d", auto_adjust=True)["Close"]
                    _n = min(len(_h), len(_spy_h), 30)
                    if _n >= 25:
                        _rel_now  = float(_h.iloc[-1]/_h.iloc[-22] - _spy_h.iloc[-1]/_spy_h.iloc[-22])
                        _rel_prev = float(_h.iloc[-6]/_h.iloc[-27] - _spy_h.iloc[-6]/_spy_h.iloc[-27])
                        if _rel_prev < -0.01 and _rel_now > 0.005:
                            _sr_alerts.append(f"  🔄 *{_sname}* ({_etf}) entering leadership: {_rel_now*100:+.1f}% vs SPY")
                except Exception:
                    pass
            if _sr_alerts:
                lines.append("\n*Sector rotation:*")
                lines.extend(_sr_alerts)
        except Exception:
            pass

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


# Opening range filter: cancel buy-stop if price hasn't confirmed above trigger
# after 30 minutes of trading (10:00 ET = 16:00 CEST). Prevents gap-and-trap fills.
_last_or_check_date: date | None = None


def _opening_range_check() -> None:
    """
    Run 30 minutes after US open (16:00 CEST / 10:00 ET). Three checks:
    1. SPY/QQQ strength: if both down >0.5%, cancel ALL orders (bad market day)
    2. Price confirmation: cancel if price still below breakout level
    3. Volume confirmation: cancel if first-30-min volume < 20% of daily average
    """
    global _last_or_check_date
    try:
        import pytz, yfinance as yf
        from broker import get_open_orders, cancel_all_orders
        from risk_manager import _load as _lrs, _save as _srs

        buy_stops = [o for o in get_open_orders()
                     if o.get("side") == "buy" and o.get("type") in ("stop", "stop_limit")]
        if not buy_stops:
            return

        # ── Check 1: Market strength — cancel all if SPY + QQQ both down ────
        try:
            spy_1m = yf.Ticker("SPY").history(period="1d", interval="1m", auto_adjust=True)
            qqq_1m = yf.Ticker("QQQ").history(period="1d", interval="1m", auto_adjust=True)
            spy_chg = float(spy_1m["Close"].iloc[-1] / spy_1m["Close"].iloc[0] - 1) if len(spy_1m) > 1 else 0.0
            qqq_chg = float(qqq_1m["Close"].iloc[-1] / qqq_1m["Close"].iloc[0] - 1) if len(qqq_1m) > 1 else 0.0
        except Exception:
            spy_chg = qqq_chg = 0.0

        if spy_chg < -0.005 and qqq_chg < -0.005:
            mkt_cancelled = []
            for o in buy_stops:
                sym = o["symbol"]
                cancel_all_orders(sym)
                rs = _lrs()
                rs.get("positions_risk", {}).pop(sym, None)
                rs["open_risk_pct"] = sum(rs.get("positions_risk", {}).values())
                _srs(rs)
                mkt_cancelled.append(sym)
            _log.info("[or_check] MARKET WEAK (SPY %.1f%% QQQ %.1f%%) — cancelled: %s",
                      spy_chg * 100, qqq_chg * 100, mkt_cancelled)
            _tg(f"🔴 *Opening Range — Market Weak*\n"
                f"SPY {spy_chg:+.1%} | QQQ {qqq_chg:+.1%}\n"
                f"All orders cancelled: {chr(10).join(mkt_cancelled)}")
            return

        # ── Check 2 + 3: Per-symbol price + volume confirmation ─────────────
        cancelled = []
        kept      = []
        for o in buy_stops:
            sym    = o["symbol"]
            stop_p = float(o.get("stop_price", 0))
            if stop_p <= 0:
                continue
            try:
                df1 = yf.Ticker(sym).history(period="1d", interval="1m", auto_adjust=True)
                if df1.empty:
                    kept.append((sym, "no_data"))
                    continue
                cur_price  = float(df1["Close"].iloc[-1])
                vol_30min  = float(df1["Volume"].sum())

                # Volume threshold: first 30 min must be ≥ 20% of 30-day daily average
                try:
                    vol_daily = float(yf.Ticker(sym).history(
                        period="30d", interval="1d")["Volume"].mean())
                    vol_ok = vol_daily <= 0 or vol_30min >= vol_daily * 0.20
                except Exception:
                    vol_ok = True

                if cur_price < stop_p * 0.998:
                    cancel_all_orders(sym)
                    rs = _lrs()
                    rs.get("positions_risk", {}).pop(sym, None)
                    rs["open_risk_pct"] = sum(rs.get("positions_risk", {}).values())
                    _srs(rs)
                    cancelled.append((sym, "no_price_confirm", cur_price, stop_p, 0))
                    _log.info("[or_check] CANCEL %s — price $%.2f below stop $%.2f",
                              sym, cur_price, stop_p)
                elif not vol_ok:
                    cancel_all_orders(sym)
                    rs = _lrs()
                    rs.get("positions_risk", {}).pop(sym, None)
                    rs["open_risk_pct"] = sum(rs.get("positions_risk", {}).values())
                    _srs(rs)
                    vol_pct = vol_30min / vol_daily if vol_daily > 0 else 0
                    cancelled.append((sym, "low_volume", cur_price, stop_p, vol_pct))
                    _log.info("[or_check] CANCEL %s — weak volume %.0f%% of daily avg",
                              sym, vol_pct * 100)
                else:
                    vol_pct = vol_30min / vol_daily if vol_daily > 0 else 0
                    kept.append((sym, vol_pct))
                    _log.info("[or_check] KEEP %s — price $%.2f ✓ vol=%.0f%% of daily ✓",
                              sym, cur_price, vol_pct * 100)
            except Exception as _e:
                _log.debug("[or_check] %s check failed: %s", sym, _e)
                kept.append((sym, 0))

        if cancelled or kept:
            lines = [f"🕙 *Opening Range Check (10:00 ET)*",
                     f"SPY {spy_chg:+.1%} | QQQ {qqq_chg:+.1%}"]
            for sym, reason, cur, stop, vol in cancelled:
                tag = "no price confirm" if reason == "no_price_confirm" else f"weak vol {vol:.0%}"
                lines.append(f"  ❌ *{sym}* ${cur:.2f} — {tag}")
            for sym, vol in kept:
                lines.append(f"  ✅ *{sym}* — price + vol ({vol:.0%}) confirmed")
            _tg("\n".join(lines))

    except Exception as e:
        _log.warning("[or_check] Opening range check failed: %s", e)


def _maybe_opening_range_check() -> None:
    """Trigger opening range filter at 16:00 CEST (10:00 ET), once per day."""
    global _last_or_check_date
    import pytz
    now = datetime.now(pytz.timezone("Europe/Stockholm"))
    if now.weekday() >= 5:
        return
    if not (now.hour == 16 and 0 <= now.minute <= 8):
        return
    today = now.date()
    if _last_or_check_date == today:
        return
    _last_or_check_date = today
    _opening_range_check()


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

        # ── Read all trade journal entries (full history, not just this week) ──
        total_trades = wins = losses = 0
        win_r_sum = loss_r_sum = 0.0
        journal_file = LOG_DIR / "trade_journal.jsonl"
        score_buckets: dict[str, list] = {"5.0-6.0": [], "6.0-7.0": [], "7.0-8.0": [], "8.0+": []}
        if journal_file.exists():
            for line in journal_file.read_text().splitlines():
                try:
                    t = json.loads(line)
                    total_trades += 1
                    r = t.get("r_multiple", 0)
                    if t.get("pnl_pct", 0) >= 0:
                        wins += 1
                        win_r_sum += r
                    else:
                        losses += 1
                        loss_r_sum += r
                    cs_val = float(t.get("composite_score", 0.0) or 0.0)
                    bkt = ("8.0+" if cs_val >= 8.0 else "7.0-8.0" if cs_val >= 7.0
                           else "6.0-7.0" if cs_val >= 6.0 else "5.0-6.0")
                    score_buckets[bkt].append(r)
                except Exception:
                    pass

        win_rate   = wins / total_trades if total_trades else 0.0
        avg_win_r  = win_r_sum / wins if wins else 0.0
        avg_loss_r = loss_r_sum / losses if losses else 0.0
        expectancy = (win_rate * avg_win_r) + ((1 - win_rate) * avg_loss_r)

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
        if total_trades > 0:
            lines.append(f"")
            lines.append(f"*All-time trade stats ({total_trades} closed):*")
            lines.append(f"Win rate: {win_rate:.0%}  ({wins}W / {losses}L)")
            lines.append(f"Avg win: {avg_win_r:+.2f}R  |  Avg loss: {avg_loss_r:+.2f}R")
            lines.append(f"Expectancy: {expectancy:+.2f}R per trade")
        if any(score_buckets.values()):
            lines.append(f"")
            lines.append(f"*Score-bucket performance:*")
            for bkt, rs_list in score_buckets.items():
                if rs_list:
                    _avg_r = sum(rs_list) / len(rs_list)
                    _wr    = sum(1 for rr in rs_list if rr > 0) / len(rs_list)
                    lines.append(
                        f"  Score {bkt}: {len(rs_list)} trades  "
                        f"avg {_avg_r:+.2f}R  WR={_wr:.0%}")
        try:
            _fb = {
                "updated": today.isoformat(),
                "total_trades": total_trades,
                "win_rate":   round(win_rate,   3),
                "avg_win_r":  round(avg_win_r,  3),
                "avg_loss_r": round(avg_loss_r, 3),
                "expectancy": round(expectancy, 3),
                "score_buckets": {
                    k: {"count": len(v),
                        "avg_r": round(sum(v)/len(v), 3) if v else 0.0,
                        "win_rate": round(sum(1 for rr in v if rr > 0)/len(v), 3) if v else 0.0}
                    for k, v in score_buckets.items()
                },
            }
            (LOG_DIR / "feedback_state.json").write_text(json.dumps(_fb, indent=2))
        except Exception:
            pass
        lines.append(f"")
        if ret_line:
            lines.append(ret_line)
        lines.append(f"Portfolio: ${portfolio_value:,.0f}")

        _tg("\n".join(lines))
        _log.info("[weekly] Weekly report sent")
    except Exception as e:
        _log.warning("[weekly] Report failed: %s", e)



# ── Market regime filter ──────────────────────────────────────────────────────

def _check_market_regime() -> tuple[str, float, float, float]:
    """
    Determine market regime from SPY vs its 200-day MA.
    Returns (regime, spy_price, ma200, pct_diff).
      'bull'    — SPY above MA200 or within 3% below  → full sizing
      'neutral' — SPY 3-8% below MA200                → 75% sizing
      'bear'    — SPY >8% below MA200                 → no new positions
    On fetch failure returns 'bull' so the bot never blocks itself on error.
    """
    try:
        import yfinance as yf
        df    = yf.Ticker("SPY").history(period="1y", interval="1d", auto_adjust=True)
        close = df["Close"]
        ma200 = float(close.rolling(200).mean().iloc[-1])
        price = float(close.iloc[-1])
        pct   = (price - ma200) / ma200
        if pct > -0.03:
            regime = "bull"
        elif pct > -0.08:
            regime = "neutral"
        else:
            regime = "bear"
        _log.info("[regime] SPY $%.2f | MA200 $%.2f | %+.1f%% → %s",
                  price, ma200, pct * 100, regime.upper())
        return regime, price, ma200, pct
    except Exception as e:
        _log.warning("[regime] Check failed (%s) — defaulting to BULL", e)
        return "bull", 0.0, 0.0, 0.0


def _fetch_vix() -> float:
    """Fetch latest VIX close. Returns 20.0 on failure (neutral assumption)."""
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if not vix.empty:
            return float(vix["Close"].iloc[-1])
    except Exception:
        pass
    return 20.0


def _vix_size_factor(vix: float) -> float:
    """Tudor Jones: scale down position sizes when volatility spikes.
    VIX < 15  → full size (1.00)
    VIX 15-20 → 0.90
    VIX 20-25 → 0.80
    VIX 25-30 → 0.65
    VIX > 30  → 0.50  (fear regime — protect capital)
    """
    if vix < 15:
        return 1.00
    elif vix < 20:
        return 0.90
    elif vix < 25:
        return 0.80
    elif vix < 30:
        return 0.65
    return 0.50


# ── Macro calendar blackout (FOMC + CPI) ────────────────────────────────────
_FOMC_2026 = [(1,29),(3,19),(5,7),(6,18),(7,30),(9,17),(10,29),(12,10)]
_CPI_2026  = [(1,15),(2,12),(3,12),(4,10),(5,13),(6,11),(7,15),(8,12),(9,10),(10,14),(11,12),(12,10)]


def _is_macro_blackout() -> tuple[bool, str]:
    """Return (True, reason) if within 2 calendar days before FOMC or CPI release.
    Avoids new entries ahead of binary macro events that gap past any stop.
    """
    today = date.today()
    for (m, d) in _FOMC_2026 + _CPI_2026:
        try:
            event = date(today.year, m, d)
        except ValueError:
            continue
        delta = (event - today).days
        if 0 <= delta <= 2:
            kind = "FOMC" if (m, d) in _FOMC_2026 else "CPI"
            return True, f"{kind} {event}"
    return False, ""


# ── Power Trend (O'Neil / IBD) ────────────────────────────────────────────
def _fetch_power_trend() -> bool:
    """True if SPY 21d EMA > 50d EMA for 8+ consecutive days (O'Neil Power Trend).
    Signals confirmed bull acceleration — adds +1.0 to Tudor Jones score.
    """
    try:
        import yfinance as _yf
        _df = _yf.Ticker("SPY").history(period="90d", interval="1d", auto_adjust=True)
        c = _df["Close"]
        ema21 = c.ewm(span=21, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()
        return all(ema21.iloc[-i] > ema50.iloc[-i] for i in range(1, 9))
    except Exception:
        return False


# ── Choppy market detection ────────────────────────────────────────────────
def _is_market_choppy() -> bool:
    """True if SPY ATR(14)/price < 0.6% for 10 consecutive days.
    Compressed volatility = institutions waiting; momentum setups fail in chop.
    When choppy: halve max_positions to preserve capital.
    """
    try:
        import yfinance as _yf
        _df = _yf.Ticker("SPY").history(period="30d", interval="1d", auto_adjust=True)
        if len(_df) < 15:
            return False
        h = _df["High"].values
        l = _df["Low"].values
        c = _df["Close"].values
        tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
        spy_px = c[-1]
        return all(t / spy_px < 0.006 for t in tr[-10:])
    except Exception:
        return False


def _adaptive_risk_pct(composite: float, base_pct: float, vix: float = 20.0) -> float:
    """Scale position risk 1.5–2% based on composite score, then VIX-adjusted."""
    if composite >= 8.0:
        score_factor = min(base_pct * (4 / 3), 0.020)
    elif composite >= 7.0:
        score_factor = min(base_pct * (7 / 6), 0.020)
    else:
        score_factor = base_pct
    return score_factor * _vix_size_factor(vix)


def _consecutive_loss_factor(losses: int) -> float:
    """Tudor Jones: reduce position size after losing streaks to protect capital."""
    if losses >= 3:
        return 0.33   # 3+ losses → 33% of normal size
    if losses == 2:
        return 0.50   # 2 losses → 50% of normal size
    if losses == 1:
        return 0.75   # 1 loss → 75% of normal size
    return 1.00       # no streak → full size


# ── Three Masters composite scoring ──────────────────────────────────────────

def _minervini_score(vcp) -> float:
    """0–10, weight 60%. VCP setup quality from Haiku→Sonnet→Opus analysis."""
    q      = min(max(getattr(vcp, "quality_score", 0), 0), 5) * 1.0
    conf   = getattr(vcp, "confidence", 0.0) * 3.0
    tight  = getattr(vcp, "tight_pct", 1.0)
    tight_b = 1.0 if tight < 0.05 else (0.5 if tight < 0.07 else 0.0)
    vol_b  = 0.5 if getattr(vcp, "vol_at_multiweek_low", False) else 0.0
    bvol_b = 0.5 if getattr(vcp, "breakout_volume", False) else 0.0
    rs_b   = 1.0 if getattr(vcp, "rs_line_at_high", False) else 0.0
    return min(q + conf + tight_b + vol_b + bvol_b + rs_b, 10.0)


def _fetch_vix_slope() -> float:
    """
    5-day change in VIX (points). Positive = fear rising = risk-off.
    Returns 0.0 on failure.
    """
    try:
        import yfinance as _yf
        hist = _yf.Ticker("^VIX").history(period="15d", interval="1d", auto_adjust=False)
        if len(hist) >= 6:
            return float(hist["Close"].iloc[-1] - hist["Close"].iloc[-6])
    except Exception:
        pass
    return 0.0


def _consecutive_win_factor(wins: int) -> float:
    """Tudor Jones: press winners — increase size modestly after consecutive wins."""
    if wins >= 3:
        return 1.25   # 3+ wins → 125% of base risk
    if wins >= 2:
        return 1.10   # 2 wins → 110%
    return 1.00


def _fetch_10y_yield_slope() -> float:
    """
    Return 20-day change in US 10Y Treasury yield in basis points (^TNX).
    Positive = yields rising = headwind for growth/tech stocks.
    Returns 0.0 on failure.
    """
    try:
        import yfinance as _yf
        hist = _yf.Ticker("^TNX").history(period="35d", interval="1d", auto_adjust=False)
        if len(hist) >= 21:
            # ^TNX is in %, e.g. 4.50 means 4.50% — convert change to bps
            return float((hist["Close"].iloc[-1] - hist["Close"].iloc[-21]) * 100)
    except Exception:
        pass
    return 0.0


def _dynamic_min_composite() -> float:
    """
    Auto-raise MIN_COMPOSITE to 6.5 if low-score bucket (5.0–6.5) has negative expectancy.
    Reads feedback_state.json written weekly. Returns 5.0 (default) or 6.5.
    """
    try:
        import json as _j
        fb  = _j.loads((LOG_DIR / "feedback_state.json").read_text())
        bkt = fb.get("score_buckets", {}).get("5.0-6.5", {})
        if bkt.get("count", 0) >= 5 and bkt.get("avg_r", 0.0) < 0:
            _log.info("[score] Dynamic MIN raised to 6.5 — low-score bucket avg=%.2fR (%d trades)",
                      bkt["avg_r"], bkt["count"])
            return 6.5
    except Exception:
        pass
    return 5.0


def _fetch_pcr() -> float:
    """
    Fetch CBOE total Put/Call ratio as fear/greed gauge. Returns 0.7 (neutral) on failure.
    PCR > 1.0 = fear/contrarian buy (+0.5 Tudor pts); PCR < 0.6 = greed (-0.5 pts).
    """
    try:
        import yfinance as _yf
        for tkr in ("^PCALL", "^CPC"):
            try:
                h = _yf.Ticker(tkr).history(period="5d", interval="1d", auto_adjust=False)
                if not h.empty and not h["Close"].isna().all():
                    return float(h["Close"].dropna().iloc[-1])
            except Exception:
                continue
    except Exception:
        pass
    return 0.7


def _simons_score(trend) -> float:
    """
    0–10, weight 30%. Trend quality, RS strength, fundamentals (Simons layer).
    rs_line_leading (RS at high while price in base) = strongest Minervini signal.
    """
    rs     = getattr(trend, "rs_rating", 70.0)
    rs_pts = min((rs - 70) / 29 * 4.0, 4.0)
    # RS line signal: weekly confirmation elevates score
    rs_leading = getattr(trend, "rs_line_leading",  False)
    rs_at_high = getattr(trend, "rs_line_at_high",  False)
    rs_weekly  = getattr(trend, "rs_weekly_confirmed", False)
    if rs_leading and rs_weekly:
        rs_sig = 3.0   # daily leading + weekly confirmed = institutional breakout
    elif rs_leading:
        rs_sig = 2.5   # RS breaks out before price
    elif rs_at_high and rs_weekly:
        rs_sig = 2.0   # at high on both timeframes
    elif rs_at_high:
        rs_sig = 1.5   # daily high only
    else:
        rs_sig = 0.0
    rsi    = getattr(trend, "rsi", 65.0)
    rsi_pts = 2.0 if rsi <= 65 else (1.0 if rsi <= 72 else 0.0)
    pfh    = abs(getattr(trend, "pct_from_high", -0.25))
    hi_pts = 1.5 if pfh <= 0.05 else (1.0 if pfh <= 0.10 else (0.5 if pfh <= 0.20 else 0.0))
    slope  = getattr(trend, "ma200_slope_20d", 0.0)
    sl_pts = 0.5 if slope > 0.005 else 0.0
    # Fundamental quality bonus: EPS growth (Minervini SEPA requirement)
    eps_g   = getattr(trend, "eps_growth", None)
    eps_pts = (1.0 if eps_g is not None and eps_g >= 0.25
               else (0.5 if eps_g is not None and eps_g >= 0.10 else 0.0))
    # RS momentum: line trending up 4w>8w>12w = institutional accumulation building
    rs_trend  = getattr(trend, "rs_trending", False)
    trend_pts = 0.5 if rs_trend and not rs_leading else 0.0
    # Accumulation/Distribution: up-vol > down-vol = institutional buying pressure
    ad       = getattr(trend, "ad_ratio", 1.0)
    ad_pts   = 0.5 if ad >= 1.5 else (0.25 if ad >= 1.2 else 0.0)
    # Short interest: high days-to-cover = squeeze fuel at breakout
    srat     = getattr(trend, "short_ratio", None)
    short_pts = (0.5 if srat is not None and srat >= 5.0
                 else (0.25 if srat is not None and srat >= 3.0 else 0.0))
    # Pre-earnings sweet spot: 4–8 weeks pre-report + strong EPS = upcoming catalyst
    days_earn = getattr(trend, "days_to_earnings", None)
    earn_pts  = (0.5 if days_earn is not None and 28 <= days_earn <= 56
                 and eps_g is not None and eps_g >= 0.25 else 0.0)
    # Monthly Stage 2: three-timeframe alignment (monthly+weekly+daily) confirms uptrend
    monthly_s2  = getattr(trend, "monthly_stage2", True)
    monthly_pts = 0.5 if monthly_s2 else 0.0
    # Earnings estimate revision: forwardEps growing faster than trailing = analyst upgrades
    eps_rev     = getattr(trend, "eps_revision", None)
    rev_pts     = (0.5 if eps_rev is not None and eps_rev >= 0.15
                   else (0.25 if eps_rev is not None and eps_rev >= 0.05 else 0.0))
    # RS vs own sector: outperforming sector AND SPY = double confirmation of leadership
    rs_sec      = getattr(trend, "rs_vs_sector", None)
    sec_rs_pts  = (0.5 if rs_sec is not None and rs_sec >= 0.05
                   else (0.25 if rs_sec is not None and rs_sec >= 0.02 else 0.0))
    # Return on equity: ≥15% = capital-efficient compounding machine (Simons quality)
    roe_val   = getattr(trend, "roe", None)
    roe_pts   = (0.5 if roe_val is not None and roe_val >= 0.15
                 else (0.25 if roe_val is not None and roe_val >= 0.10 else 0.0))
    return min(rs_pts + rs_sig + rsi_pts + hi_pts + sl_pts + eps_pts + trend_pts
               + ad_pts + short_pts + earn_pts + monthly_pts + rev_pts + sec_rs_pts + roe_pts, 10.0)


def _atr_volatility_factor(symbol: str, entry_price: float) -> float:
    """Reduce position size when 14-day ATR/price > 4%% — avoids oversizing volatile stocks.
    High ATR means wider natural swings; 1R per trade requires fewer shares.
    """
    try:
        import yfinance as _yf_atr
        _df = _yf_atr.Ticker(symbol).history(period="30d", interval="1d", auto_adjust=True)
        if len(_df) < 15:
            return 1.0
        _hi, _lo, _cl = _df["High"].values, _df["Low"].values, _df["Close"].values
        _tr = [max(_hi[i] - _lo[i], abs(_hi[i] - _cl[i-1]), abs(_lo[i] - _cl[i-1]))
               for i in range(1, len(_cl))]
        _atr14 = sum(_tr[-14:]) / 14
        _pct   = _atr14 / entry_price if entry_price > 0 else 0
        if _pct <= 0.02:  return 1.00   # low volatility — full size
        if _pct <= 0.04:  return 0.90   # normal
        if _pct <= 0.06:  return 0.75   # elevated
        return 0.60                      # high volatility — reduced
    except Exception:
        return 1.0


def _fetch_distribution_days() -> int:
    """Count SPY distribution days in last 25 sessions.
    Distribution day = SPY closes down >0.2%% on higher volume than prior day.
    4-5 distribution days signal institutional selling (O'Neil market health).
    """
    try:
        import yfinance as _yf_dd
        _df = _yf_dd.Ticker("SPY").history(period="40d", interval="1d", auto_adjust=True)
        if len(_df) < 5:
            return 0
        _df = _df.tail(26)
        _count = 0
        for _i in range(1, len(_df)):
            _chg = (_df["Close"].iloc[_i] - _df["Close"].iloc[_i - 1]) / _df["Close"].iloc[_i - 1]
            if _chg < -0.002 and _df["Volume"].iloc[_i] > _df["Volume"].iloc[_i - 1]:
                _count += 1
        return _count
    except Exception:
        return 0


def _fetch_nh_nl_ratio() -> float:
    """NYSE new highs vs new lows ratio via ^NYHL (net = NH - NL).
    >1.5 = market breadth expanding; <0.5 = deteriorating internals.
    Falls back to neutral (1.0) if data unavailable.
    """
    try:
        import yfinance as _yf_nl
        _hl = _yf_nl.Ticker("^NYHL").history(period="5d", interval="1d", auto_adjust=True)
        if len(_hl) >= 1:
            _net = float(_hl["Close"].iloc[-1])
            if _net > 150:   return 2.0   # strong expansion
            if _net > 50:    return 1.5   # mild expansion
            if _net < -150:  return 0.25  # deteriorating
            if _net < -50:   return 0.5   # weakening
            return 1.0
    except Exception:
        pass
    return 1.0   # neutral fallback when data unavailable


def _tudor_score(risk_state: dict, regime: str, breadth_pct: float = 0.5,
                  power_trend: bool = False, pcr: float = 0.7,
                  rate_slope_bps: float = 0.0, vix_slope: float = 0.0,
                  dist_days: int = 0, nh_nl_ratio: float = 1.0) -> float:
    """
    0–10, weight 10%. Market regime, portfolio health, breadth (Tudor Jones layer).
    power_trend = O'Neil SPY 21d EMA > 50d EMA for ≥8 days (+1.0 pts).
    pcr = CBOE Put/Call ratio: >1.0 fear=+0.5, <0.6 greed=-0.5.
    rate_slope_bps = 20-day change in 10Y yield; >50bps rising = -1.0 pts.
    vix_slope = 5-day VIX change; >3pts = fear rising (-0.5), <-2pts = complacency (+0.25).
    dist_days = distribution days in last 25 sessions; >=5 = -1.5 pts (O'Neil sell signal).
    nh_nl_ratio = NYSE new highs/lows ratio; <0.5 = -1.0 pts (internal deterioration).
    """
    reg_pts  = {"bull": 3.0, "neutral": 1.5, "bear": 0.0}.get(regime, 3.0)
    losses   = risk_state.get("consecutive_losses", 0)
    loss_pts = 3.0 if losses == 0 else (1.5 if losses == 1 else 0.0)
    heat     = risk_state.get("open_risk_pct", 0.0)
    heat_pts = 1.5 if heat < 0.02 else (0.75 if heat < 0.04 else 0.0)
    # Market breadth: % of screened universe trading above MA50
    breadth_pts = 2.0 if breadth_pct > 0.65 else (1.0 if breadth_pct > 0.45 else 0.0)
    # Power Trend: SPY 21d EMA > 50d EMA ≥8 days (O'Neil confirmation)
    power_pts = 1.0 if power_trend else 0.0
    # Put/Call ratio: >1.0 = fear (contrarian buy), <0.6 = complacency (caution)
    pcr_pts   = 0.5 if pcr > 1.0 else (-0.5 if pcr < 0.6 else 0.0)
    # Rate sensitivity: rapidly rising yields = headwind for growth stocks
    rate_pts  = -1.0 if rate_slope_bps > 50 else (-0.5 if rate_slope_bps > 25 else 0.0)
    # VIX direction: rising fear = caution, falling = environment improving
    vix_pts   = -0.5 if vix_slope > 3.0 else (0.25 if vix_slope < -2.0 else 0.0)
    # Distribution days: institutional selling on heavy volume (O'Neil market health)
    dist_pts  = -1.5 if dist_days >= 5 else (-0.5 if dist_days >= 3 else 0.0)
    # NH/NL internals: expanding new highs = healthy tape; collapsing = distribution
    nh_nl_pts = 0.5 if nh_nl_ratio >= 1.5 else (-1.0 if nh_nl_ratio < 0.5 else 0.0)
    return min(reg_pts + loss_pts + heat_pts + breadth_pts + power_pts + pcr_pts + rate_pts + vix_pts + dist_pts + nh_nl_pts, 10.0)


def _composite_score(vcp, trend, risk_state: dict, regime: str,
                     sector_bonus: float = 0.0, breadth_pct: float = 0.5,
                     power_trend: bool = False, pcr: float = 0.7,
                     rate_slope_bps: float = 0.0, vix_slope: float = 0.0,
                     dist_days: int = 0, nh_nl_ratio: float = 1.0) -> float:
    """
    Three Masters weighted composite score (0–10).
      Minervini 60% — VCP quality, confidence, handle tightness, volume
      Simons     30% — RS strength, RS line at high, RSI quality, 52w proximity, monthly Stage 2
      Tudor      10% — regime, loss streak, heat, breadth, power trend, PCR, rate slope
      sector_bonus ±0.5 — sector outperforming/underperforming SPY (Stage 2 required)
    Minimum 5.0 required to place an order.
    """
    m = _minervini_score(vcp)
    s = _simons_score(trend)
    t = _tudor_score(risk_state, regime, breadth_pct, power_trend, pcr, rate_slope_bps, vix_slope, dist_days, nh_nl_ratio)
    return round(min(10.0, m * 0.60 + s * 0.30 + t * 0.10 + sector_bonus), 2)


# ── Sector rotation helpers ──────────────────────────────────────────────────
# Keys are the exact strings yfinance returns for sector info
_SUPER_SECTOR: dict[str, str] = {
    "Technology":             "growth",
    "Communication Services": "growth",
    "Consumer Cyclical":      "cyclical",
    "Consumer Discretionary": "cyclical",
    "Industrials":            "cyclical",
    "Basic Materials":        "cyclical",
    "Materials":              "cyclical",
    "Energy":                 "cyclical",
    "Consumer Defensive":     "defensive",
    "Consumer Staples":       "defensive",
    "Healthcare":             "defensive",
    "Health Care":            "defensive",
    "Real Estate":            "defensive",
    "Utilities":              "defensive",
    "Financial Services":     "financial",
    "Financials":             "financial",
}

_SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Financial Services":     "XLF",
    "Financials":             "XLF",   # legacy alias
    "Healthcare":             "XLV",
    "Health Care":            "XLV",   # legacy alias
    "Energy":                 "XLE",
    "Consumer Cyclical":      "XLY",
    "Consumer Discretionary": "XLY",   # legacy alias
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Materials":              "XLB",   # legacy alias
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Consumer Defensive":     "XLP",
    "Consumer Staples":       "XLP",   # legacy alias
    "Communication Services": "XLC",
}


def _sector_momentum_scores() -> tuple[dict[str, float], dict[str, bool]]:
    """Return ({etf: 21d_return_vs_SPY}, {etf: above_MA200}) for SPDR sector ETFs.
    Stage 2 flag (above MA200) required for positive sector bonus to apply.
    """
    try:
        import yfinance as yf
        unique_etfs = list(set(_SECTOR_ETF_MAP.values()))
        df = yf.download(unique_etfs + ["SPY"], period="1y", interval="1d",
                         auto_adjust=True, progress=False)["Close"]
        spy_ret = (float(df["SPY"].iloc[-1] / df["SPY"].iloc[-21] - 1)
                   if "SPY" in df.columns and len(df) >= 22 else 0.0)
        scores, stage2 = {}, {}
        for etf in unique_etfs:
            if etf not in df.columns:
                continue
            col = df[etf].dropna()
            if len(col) < 22:
                continue
            ret = float(col.iloc[-1] / col.iloc[-21] - 1)
            scores[etf] = round(ret - spy_ret, 4)
            ma200 = float(col.tail(200).mean()) if len(col) >= 200 else float(col.mean())
            stage2[etf] = float(col.iloc[-1]) > ma200
        _log.info("[sector] momentum vs SPY (21d): %s | Stage2: %s",
                  {k: f"{v:+.1%}" for k, v in sorted(scores.items(), key=lambda x: -x[1])},
                  {k: v for k, v in stage2.items()})
        return scores, stage2
    except Exception as e:
        _log.debug("[sector] momentum fetch failed: %s", e)
        return {}, {}


def _sector_bonus(symbol: str, sector_scores: dict[str, float],
                   stage2: dict[str, bool] | None = None) -> float:
    """
    Composite bonus (±0.5) based on sector momentum vs SPY.
    Stage 2 filter: sector ETF must be above its MA200 for full positive bonus.
    """
    if not sector_scores:
        return 0.0
    try:
        from screener import get_sector
        sector   = get_sector(symbol)
        etf      = _SECTOR_ETF_MAP.get(sector)
        if etf is None:
            return 0.0
        rel      = sector_scores.get(etf, 0.0)
        in_s2    = (stage2 or {}).get(etf, True)  # default True if no stage2 data
        if rel > 0.015:
            return 0.5 if in_s2 else 0.25  # outperforming but below MA200 = muted bonus
        elif rel > 0.005:
            return 0.25
        elif rel < -0.015:
            return -0.5
        elif rel < -0.005:
            return -0.25
        return 0.0
    except Exception:
        return 0.0


def _is_correlated(candidate: str, held: set, threshold: float = 0.80) -> bool:
    """
    Return True if candidate has pearson r >= threshold with any currently-held symbol
    based on 60 trading days of returns.  If data fetch fails, returns False (don't block).
    """
    if not held:
        return False
    try:
        import yfinance as yf
        import pandas as pd
        syms = [candidate] + list(held)
        df = yf.download(syms, period="3mo", interval="1d",
                         auto_adjust=True, progress=False)["Close"]
        if isinstance(df, pd.Series):
            return False  # only one column
        rets = df.pct_change().dropna()
        if candidate not in rets.columns:
            return False
        cand_col = rets[candidate]
        for sym in held:
            if sym not in rets.columns:
                continue
            corr = float(cand_col.corr(rets[sym]))
            if corr >= threshold:
                _log.info("[main] %s skipped — corr=%.2f with %s (>= %.0f%%)",
                          candidate, corr, sym, threshold * 100)
                return True
        return False
    except Exception as e:
        _log.debug("[main] Correlation check failed for %s: %s", candidate, e)
        return False


def _smart_order_management(vcp_passed: list, held_symbols: set) -> set:
    """
    Compare existing Alpaca buy-stop orders against new VCP candidates.
    Cancels stale or price-drifted orders; keeps valid ones.
    Returns set of symbols whose existing order is retained (skip re-placing).
    """
    from broker import get_open_orders, cancel_all_orders as _cancel_sym
    existing = {
        o["symbol"]: o for o in get_open_orders()
        if o.get("side") == "buy" and o.get("type") in ("stop", "stop_limit")
    }
    new_map  = {r.symbol: r.breakout_level for r in vcp_passed}
    keep: set[str] = set()

    for sym, order in list(existing.items()):
        if sym in held_symbols:
            _cancel_sym(sym)
            _log.info("[main] Cancelled buy-stop for %s — position already filled", sym)
            continue
        if sym not in new_map:
            _cancel_sym(sym)
            _log.info("[main] Cancelled stale order: %s (no longer a VCP candidate)", sym)
            continue
        old_stop   = float(order.get("stop_price", 0))
        new_stop   = new_map[sym]
        price_drift = abs(new_stop - old_stop) / old_stop if old_stop > 0 else 1.0
        if price_drift > 0.005:
            _cancel_sym(sym)
            _log.info("[main] Cancelled %s — breakout level moved $%.2f→$%.2f (%.1f%%)",
                      sym, old_stop, new_stop, price_drift * 100)
        else:
            keep.add(sym)
            _log.info("[main] Keeping valid order: %s @ $%.2f (unchanged)", sym, old_stop)

    if existing:
        _log.info("[main] Smart order mgmt: %d kept, %d cancelled",
                  len(keep), len(existing) - len(keep))
    return keep


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

    # ── Position sync — MUST succeed before any trading is allowed ───────────
    # SyncError means Alpaca is unreachable — abort the scan entirely.
    # Never proceed with unverified state, especially with real money.
    from position_sync import sync_all as _sync_all, log_full_state, SyncError
    try:
        _sync_all()
        log_full_state()
    except SyncError as e:
        msg = f"SYNC FAILED — scan aborted, no orders placed: {e}"
        _log.error("[main] %s", msg)
        _tg(f"🚨 *Three Masters — SYNC FAILURE*\n`{e}`\nScan aborted — no orders placed.")
        report["errors"].append(f"sync_failed: {e}")
        _save_report(report)
        return

    # ── Risk state ────────────────────────────────────────────────────────────
    from risk_manager import check_can_trade, daily_reset, get_state
    daily_reset(portfolio_value)   # stores day_start_equity for close_trade P&L tracking
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

    _breadth_pct = 0.5   # updated after screener run
    _current_vix = 20.0  # updated in scoring loop (also needs to be in scope here)

    # ── Layer 1: Simons — Trend Template screening ────────────────────────────
    _log.info("\n[LAYER 1 — SIMONS] Trend Template screening...")
    try:
        from screener import run as screen_universe, load_universe
        symbols = load_universe()
        _log.info("[simons] Universe: %d symbols", len(symbols))
        screen_results = screen_universe(symbols=symbols)
        trend_passed = [r for r in screen_results if r.passed]
        # Market breadth: % of screened universe above MA50 (Tudor Jones signal)
        _breadth_pct = (sum(1 for r in screen_results if r.price > r.ma50 > 0)
                        / max(len(screen_results), 1))
        _log.info("[tudor] Market breadth: %.0f%% of %d symbols above MA50",
                  _breadth_pct * 100, len(screen_results))
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
        top_candidates = sorted(trend_passed, key=lambda r: -_simons_score(r))[:40]
        trend_map      = {r.symbol: r for r in top_candidates}
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

    # ── Market regime filter ─────────────────────────────────────────────────
    regime, spy_price, spy_ma200, spy_pct = _check_market_regime()
    if regime == "bear":
        msg = (f"SPY ${spy_price:.0f} is {abs(spy_pct):.1f}% below MA200 "
               f"— bear market. {len(vcp_passed)} VCP setup(s) found but no orders placed.")
        _log.warning("[main] BEAR regime — skipping order placement. %d VCPs found.", len(vcp_passed))
        _tg(f"🐻 *Three Masters — Bear Regime*\n{msg}")
        report["summary"] = "bear_regime_no_orders"
        report["vcp_found_no_orders"] = [r.symbol for r in vcp_passed]
        _save_report(report)
        _send_daily_summary(report, len(trend_passed), len(vcp_passed), portfolio_value)
        return

    regime_size_factor = 0.75 if regime == "neutral" else 1.0
    if regime == "neutral":
        _log.info("[main] Neutral regime (SPY %+.1f%% vs MA200) — position sizing at 75%%",
                  spy_pct * 100)

    # ── Macro blackout: skip new orders within 2 days of FOMC/CPI ────────────
    _blackout, _blackout_reason = _is_macro_blackout()
    if _blackout:
        msg = (f"\U0001f4c5 *Macro Blackout \u2014 {_blackout_reason}*\n"
               f"{len(vcp_passed)} VCP setup(s) found but no orders placed.\n"
               f"FOMC/CPI within 2 days \u2014 avoiding binary event risk.")
        _log.warning("[main] MACRO BLACKOUT (%s) \u2014 skipping order placement",
                     _blackout_reason)
        _tg(msg)
        report["summary"] = f"macro_blackout_{_blackout_reason}"
        report["vcp_found_no_orders"] = [r.symbol for r in vcp_passed]
        _save_report(report)
        _send_daily_summary(report, len(trend_passed), len(vcp_passed), portfolio_value)
        return

    # ── Layer 3 + Execution: Tudor Jones — Size + Place Orders ────────────────
    _log.info("\n[LAYER 3 — TUDOR JONES] Position sizing & order placement...")
    from risk_manager import position_size, register_trade, check_can_trade
    from broker import place_buy_stop
    from screener import get_sector
    from config import RISK

    held_symbols = {p["symbol"] for p in positions}

    # Smart order management: retain unchanged orders, cancel stale/moved ones
    orders_to_skip = _smart_order_management(vcp_passed, held_symbols)
    # Choppy market: halve max positions when SPY ATR is compressed 10+ days
    _choppy = _is_market_choppy()
    _eff_max = max(1, RISK["max_positions"] // 2) if _choppy else RISK["max_positions"]
    if _choppy:
        _log.warning("[tudor] CHOPPY MARKET detected — max positions halved to %d", _eff_max)
    max_new_pos    = max(0, _eff_max - len(positions) - len(orders_to_skip))

    # Sector concentration tracking: count existing positions + retained orders
    sector_counts: dict[str, int] = {}
    for sym in held_symbols | orders_to_skip:
        sec = get_sector(sym)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
    max_per_sector = RISK.get("max_positions_per_sector", 2)

    # Tudor Jones: reduce sizing after losing streaks
    from risk_manager import get_state as _get_rs
    _rs_now = _get_rs()
    loss_streak = _rs_now.get("consecutive_losses", 0)
    loss_factor = _consecutive_loss_factor(loss_streak)
    win_streak  = _rs_now.get("consecutive_wins", 0)
    win_factor  = _consecutive_win_factor(win_streak)
    if loss_streak > 0:
        _log.info("[main] Loss streak %d — sizing factor %.0f%%",
                  loss_streak, loss_factor * 100)
    if win_streak >= 2:
        _log.info("[main] Win streak %d — pressing winners, size factor %.0f%%",
                  win_streak, win_factor * 100)

    orders_placed = []

    # ── Three Masters composite scoring: weight all three layers ─────────────
    _rs_now = _get_rs()
    _current_vix = _fetch_vix()
    _log.info("[tudor] VIX=%.1f → size factor=%.0f%%", _current_vix, _vix_size_factor(_current_vix)*100)
    sector_momentum, sector_stage2 = _sector_momentum_scores()
    _power_trend      = _fetch_power_trend()
    _current_pcr      = _fetch_pcr()
    _rate_slope_bps   = _fetch_10y_yield_slope()
    _vix_slope        = _fetch_vix_slope()
    _log.info("[tudor] VIX slope=%.1f pts/5d (%s)",
              _vix_slope,
              "fear-rising(-0.5)" if _vix_slope > 3.0 else
              ("complacency(+0.25)" if _vix_slope < -2.0 else "neutral"))
    # Enrich trend results with RS vs own sector ETF (pre-fetched sector data)
    try:
        import yfinance as _yf_rs
        import pandas as _pd_rs
        _etf_closes: dict = {}
        for _etf in set(_SECTOR_ETF_MAP.values()):
            try:
                _h = _yf_rs.Ticker(_etf).history(period="1y", interval="1d", auto_adjust=True)
                if not _h.empty:
                    _etf_closes[_etf] = _h["Close"]
            except Exception:
                pass
        for _tr in vcp_passed:
            _sec = get_sector(_tr.symbol)
            _etf = _SECTOR_ETF_MAP.get(_sec)
            if _etf and _etf in _etf_closes and _tr.df is not None:
                try:
                    _etf_c  = _etf_closes[_etf]
                    _stk_c  = _tr.df["Close"]
                    _n      = min(len(_stk_c), len(_etf_c), 252)
                    if _n >= 60:
                        _sp = float(_stk_c.iloc[-1] / _stk_c.iloc[-_n] - 1)
                        _ep = float(_etf_c.iloc[-1] / _etf_c.iloc[-_n] - 1)
                        _tr.rs_vs_sector = round(_sp - _ep, 4)
                except Exception:
                    pass
    except Exception:
        pass
    _log.info("[tudor] 10Y yield slope=%.0fbps (%s)",
              _rate_slope_bps,
              "rising-hard(-1.0)" if _rate_slope_bps > 50 else
              ("rising(-0.5)" if _rate_slope_bps > 25 else "neutral"))
    if _power_trend:
        _log.info("[tudor] Power Trend active (SPY 21d EMA > 50d EMA \u22658 days) +1.0 T-pts")
    _dist_days    = _fetch_distribution_days()
    _nh_nl_ratio  = _fetch_nh_nl_ratio()
    _log.info("[tudor] Distribution days=%d (%s)", _dist_days,
              "institutional-selling(-1.5)" if _dist_days >= 5 else
              ("caution(-0.5)" if _dist_days >= 3 else "healthy"))
    _log.info("[tudor] NH/NL ratio=%.2f (%s)", _nh_nl_ratio,
              "expanding(+0.5)" if _nh_nl_ratio >= 1.5 else
              ("deteriorating(-1.0)" if _nh_nl_ratio < 0.5 else "neutral"))
    _log.info("[tudor] PCR=%.2f (%s)", _current_pcr,
              "fear+0.5" if _current_pcr > 1.0 else ("greed-0.5" if _current_pcr < 0.6 else "neutral"))
    scored  = []
    for vcp in vcp_passed:
        trend_r = trend_map.get(vcp.symbol)
        if trend_r is None:
            scored.append((vcp, trend_r, 0.0))
            continue
        sec_bonus = _sector_bonus(vcp.symbol, sector_momentum, sector_stage2)
        cs = _composite_score(vcp, trend_r, _rs_now, regime, sec_bonus, _breadth_pct, _power_trend, _current_pcr, _rate_slope_bps, _vix_slope, _dist_days, _nh_nl_ratio)
        _log.info("[score] %s  M=%.1f S=%.1f T=%.1f sec=%+.2f breadth=%.0f%% pt=%s pcr=%.2f rate=%+.0fbps vix_sl=%.1f → composite=%.2f",
                  vcp.symbol,
                  _minervini_score(vcp), _simons_score(trend_r),
                  _tudor_score(_rs_now, regime, _breadth_pct, _power_trend, _current_pcr, _rate_slope_bps, _vix_slope, _dist_days, _nh_nl_ratio), sec_bonus,
                  _breadth_pct * 100, "✓" if _power_trend else "✗", _current_pcr, _rate_slope_bps, _vix_slope, cs)
        scored.append((vcp, trend_r, cs))

    # Filter: require composite >= 5.0 (guards against weak Simons/Tudor context)
    _MIN_COMPOSITE = _dynamic_min_composite()
    vcp_scored = [(v, t, cs) for v, t, cs in scored if cs >= _MIN_COMPOSITE]
    below = [v.symbol for v, t, cs in scored if cs < _MIN_COMPOSITE]
    if below:
        _log.info("[score] Filtered out (composite < %.1f): %s", _MIN_COMPOSITE, below)
    # Cancel retained orders for symbols that now score below threshold
    _scored_syms = {v.symbol for v, t, cs in vcp_scored}
    _stale_orders = [sym for sym in orders_to_skip if sym not in _scored_syms]
    if _stale_orders:
        from broker import cancel_all_orders as _cancel_stale
        for _sym in _stale_orders:
            _cancel_stale(_sym)
            orders_to_skip.discard(_sym)
            _log.info("[score] Cancelled stale order %s — composite dropped below %.1f", _sym, _MIN_COMPOSITE)

    # Sort by composite descending — Minervini dominates but Simons/Tudor contribute
    vcp_scored.sort(key=lambda x: -x[2])
    _log.info("[score] Order of priority: %s",
              [(v.symbol, cs) for v, t, cs in vcp_scored])

    for vcp, trend_r, composite in vcp_scored:
        if len(orders_placed) >= max_new_pos:
            _log.info("[main] Max new positions reached (%d) — stopping.", RISK["max_positions"])
            break

        if vcp.symbol in held_symbols:
            _log.info("[main] %s already held — skipping.", vcp.symbol)
            continue

        if vcp.symbol in orders_to_skip:
            _log.info("[main] %s — existing valid order retained.", vcp.symbol)
            continue

        # Sector concentration check (max_positions_per_sector from config)
        sec = get_sector(vcp.symbol)
        if sector_counts.get(sec, 0) >= max_per_sector:
            _log.info("[main] %s skipped — sector '%s' already at limit (%d/%d)",
                      vcp.symbol, sec, sector_counts.get(sec, 0), max_per_sector)
            continue

        # Correlation check: skip if too similar to any held position
        # Super-sector concentration guard: limit growth/cyclical/defensive exposure
        _vcp_super = _SUPER_SECTOR.get(get_sector(vcp.symbol), "other")
        _max_super  = max(3, round(RISK["max_positions"] * 0.60))
        _super_cnt  = sum(
            1 for _s in (held_symbols | orders_to_skip)
            if _SUPER_SECTOR.get(get_sector(_s), "other") == _vcp_super
        )
        if _super_cnt >= _max_super:
            _log.info("[main] %s skipped — super-sector '%s' at cap (%d/%d)",
                      vcp.symbol, _vcp_super, _super_cnt, _max_super)
            continue

        if _is_correlated(vcp.symbol, held_symbols):
            _log.info("[main] %s skipped — high correlation with existing position", vcp.symbol)
            continue

        # Adaptive risk: composite score → VIX-adjusted → regime/loss multipliers
        base_risk = RISK["risk_per_trade_pct"]
        _atr_f    = _atr_volatility_factor(vcp.symbol, vcp.breakout_level)
        if _atr_f < 1.0:
            _log.info("[main] %s ATR factor %.0f%% — elevated volatility", vcp.symbol, _atr_f * 100)
        risk_pct  = _adaptive_risk_pct(composite, base_risk, _current_vix) * regime_size_factor * loss_factor * win_factor * _atr_f

        can, reason = check_can_trade(portfolio_value, risk_pct)
        if not can:
            _log.warning("[main] Cannot trade: %s", reason)
            break

        try:
            sizing = position_size(portfolio_value, vcp.breakout_level, vcp.stop_loss,
                                   risk_pct, vcp.measured_move_pct)
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

        if vcp.current_price >= vcp.breakout_level * 1.005:
            _log.info("[main] %s already above breakout ($%.2f >= $%.2f) — skip",
                      vcp.symbol, vcp.current_price, vcp.breakout_level)
            continue

        buy_order = place_buy_stop(vcp.symbol, sizing["shares"], vcp.breakout_level)
        if not buy_order:
            continue

        register_trade(vcp.symbol, sizing["risk_pct"])
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

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
            "sl_order_id":    None,  # placed by position_monitor after buy fills
            "sector":         sec,
            "regime":         regime,
            "rs_rating":      round(getattr(vcp, "rs_rating", 0), 1),
            "quality_score":  getattr(vcp, "quality_score", 0),
            "rs_line_high":   getattr(vcp, "rs_line_at_high", False),
            "adaptive_risk":  round(risk_pct, 4),
            "composite_score":   composite,
            "measured_move_pct": round(getattr(vcp, "measured_move_pct", 0.0), 4),
        }
        orders_placed.append(order_rec)
        cash -= sizing["notional"]
        held_symbols.add(vcp.symbol)

        vol_tag = " 🔥" if vcp.breakout_volume else ""
        _log.info(
            "[main] ✅ %s | %d sh | buy-stop=$%.2f | SL=$%.2f | TP=$%.2f | "
            "risk=$%.0f (%.1f%%) | candle=%s | sector=%s | RS=%.0f%s",
            vcp.symbol, sizing["shares"], vcp.breakout_level, vcp.stop_loss,
            sizing["target_price"], sizing["risk_amount"], sizing["risk_pct"] * 100,
            vcp.last_candle, sec, getattr(vcp, "rs_rating", 0), vol_tag,
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
        vol_tag  = " 🔥" if o.get("breakout_vol") else ""
        rs_hi    = " ⭐RS-HIGH" if o.get("rs_line_high") else ""
        qs_str   = f" Q{o['quality_score']}/5" if o.get("quality_score") else ""
        rs_str   = f" RS={o['rs_rating']:.0f}" if o.get("rs_rating") else ""
        cs_str   = f" ⚡{o['composite_score']:.1f}/10" if o.get("composite_score") else ""
        sect     = f" [{o['sector']}]" if o.get("sector") else ""
        lines.append(
            f"  🎯 *{o['symbol']}* {o['shares']}sh @ ${o['buy_stop']:.2f}{vol_tag}{rs_hi}{qs_str}{rs_str}{cs_str}\n"
            f"     SL=${o['stop_loss']:.2f} | TP=${o['target']:.2f} | "
            f"Risk=${o['risk_amount']:.0f} ({o['risk_pct']*100:.1f}%) | {o['rr_ratio']:.1f}R{sect}\n"
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


def _start_background_services() -> None:
    """Start Telegram commands, fill stream, and dashboard alongside main loop."""
    try:
        import telegram_commands
        telegram_commands.start(_monitor_stop)
    except Exception as e:
        _log.warning("[main] Telegram command listener failed to start: %s", e)

    try:
        import order_stream
        order_stream.start(_monitor_stop)
    except Exception as e:
        _log.warning("[main] Order fill stream failed to start: %s", e)

    try:
        import dashboard
        dashboard.start(_monitor_stop)
    except Exception as e:
        _log.warning("[main] Dashboard failed to start: %s", e)


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
    _start_background_services()
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
            _maybe_opening_range_check()

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
