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
import yfinance as yf

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import (
    LOG_DIR, REPORT_DIR, CHART_DIR,
    DAILY_TRIGGER_HOUR_CET, DAILY_TRIGGER_MIN_CET,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    LOG_LEVEL, LOG_MAX_MB, LOG_BACKUPS,
    MONITOR, SECTOR_ETF_MAP,
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
_SCAN_LOCK    = threading.Lock()  # prevents concurrent scan runs


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
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
                for o in list(buy_stops):
                    sym    = o["symbol"]
                    stop_p = float(o.get("stop_price", 0))
                    if stop_p <= 0:
                        continue
                    try:
                        pre = yf.Ticker(sym).fast_info.get("last_price", None)
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
                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
                _vol_warns = []
                for _p in positions:
                    _sym_bv = _p["symbol"]
                    try:
                        _dfbv   = yf.Ticker(_sym_bv).history(period="60d", interval="1d", auto_adjust=True)
                        if len(_dfbv) >= 25:
                            _v1     = float(_dfbv["Volume"].iloc[-1])
                            _avg50  = float(_dfbv["Volume"].tail(50).mean())
                            _ratio  = _v1 / _avg50 if _avg50 > 0 else 1.0
                            if _ratio < 0.80:
                                _vol_warns.append(f"  ⚠️ *{_sym_bv}* weak volume: {_ratio:.1f}× avg — possible distribution")
                    except Exception:
                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                if _vol_warns:
                    lines.append("\n*Volume alerts:*")
                    lines.extend(_vol_warns)
            except Exception:
                _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # ── Sector rotation alert — sectors crossing into leadership ──────────────
        try:
            _spy_h  = yf.Ticker("SPY").history(period="60d", interval="1d", auto_adjust=True)["Close"]
            _sr_etfs = {"XLK":"Tech","XLV":"Health","XLF":"Finance","XLE":"Energy",
                        "XLI":"Industrl","XLC":"Comm","XLY":"Cyclical","XLP":"Defensive",
                        "XLU":"Utilities","XLB":"Materials","XLRE":"Real Estate"}
            _sr_alerts = []
            for _etf, _sname in _sr_etfs.items():
                try:
                    _h = yf.Ticker(_etf).history(period="60d", interval="1d", auto_adjust=True)["Close"]
                    _n = min(len(_h), len(_spy_h), 30)
                    if _n >= 25:
                        _rel_now  = float(_h.iloc[-1]/_h.iloc[-22] - _spy_h.iloc[-1]/_spy_h.iloc[-22])
                        _rel_prev = float(_h.iloc[-6]/_h.iloc[-27] - _spy_h.iloc[-6]/_spy_h.iloc[-27])
                        if _rel_prev < -0.01 and _rel_now > 0.005:
                            _sr_alerts.append(f"  🔄 *{_sname}* ({_etf}) entering leadership: {_rel_now*100:+.1f}% vs SPY")
                except Exception:
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
            if _sr_alerts:
                lines.append("\n*Sector rotation:*")
                lines.extend(_sr_alerts)
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # ── Pre-open gap alerts for held positions ─────────────────────────────
        if positions:
            try:
                _pre_alerts = []
                for _pg_p in positions:
                    _pg_sym = _pg_p["symbol"]
                    try:
                        _fi = yf.Ticker(_pg_sym).fast_info
                        _pre_p  = getattr(_fi, "last_price", None) or getattr(_fi, "regularMarketPrice", None)
                        _prev_c = getattr(_fi, "previous_close", None) or getattr(_fi, "regularMarketPreviousClose", None)
                        if _pre_p and _prev_c and _prev_c > 0:
                            _gap = (_pre_p - _prev_c) / _prev_c
                            if abs(_gap) >= 0.03:
                                _dir = "▲" if _gap > 0 else "▼"
                                _pre_alerts.append(
                                    f"  {_dir} *{_pg_sym}* pre-market ${_pre_p:.2f} ({_gap*100:+.1f}% vs prev close)")
                    except Exception:
                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                if _pre_alerts:
                    lines.append("\n*Pre-open gaps (≥3%):*")
                    lines.extend(_pre_alerts)
            except Exception:
                _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # ── PM12: Pre-market gap + volume screen for last night's scan candidates ──
        # Stocks screened last night that weren't ordered but are now moving pre-market.
        # Also checks premarket volume vs 30-day average to flag genuine breakouts
        # vs low-conviction price moves (Minervini: volume confirms institutional intent).
        try:
            import json as _jpm, yfinance as _yf_pm
            _rpt_files = sorted(REPORT_DIR.glob("*.json"), reverse=True)
            if _rpt_files:
                _last_rpt = _jpm.loads(_rpt_files[0].read_text())
                _scanned  = [
                    s for s in _last_rpt.get("vcp_passed", [])
                    if s not in {p["symbol"] for p in positions}
                ]
                _pm_alerts = []
                for _pm_sym in _scanned[:12]:   # cap at 12 to avoid rate-limit
                    try:
                        _fi_pm    = yf.Ticker(_pm_sym).fast_info
                        _pre_pm   = getattr(_fi_pm, "last_price", None)
                        _prev_pm  = getattr(_fi_pm, "previous_close", None)
                        if _pre_pm and _prev_pm and _prev_pm > 0:
                            _g = (_pre_pm - _prev_pm) / _prev_pm
                            if _g >= 0.02:
                                # Check premarket volume via 1m bars
                                _vol_tag = ""
                                try:
                                    _pm_1m = yf.Ticker(_pm_sym).history(
                                        period="1d", interval="1m",
                                        prepost=True, auto_adjust=True)
                                    _pm_only = _pm_1m[
                                        _pm_1m.index.tz_convert("America/New_York")
                                        .time < __import__("datetime").time(9, 30)
                                    ] if not _pm_1m.empty else _pm_1m
                                    _pm_vol = float(_pm_only["Volume"].sum()) if not _pm_only.empty else 0
                                    _avg30_df = yf.Ticker(_pm_sym).history(
                                        period="30d", interval="1d", auto_adjust=True)
                                    _avg30 = float(_avg30_df["Volume"].tail(30).mean()) if len(_avg30_df) >= 5 else 0
                                    if _avg30 > 0:
                                        _pm_vol_ratio = _pm_vol / (_avg30 * 0.15)  # vs ~15% typical premarket share
                                        if _pm_vol_ratio >= 2.0:
                                            _vol_tag = f" 🔥 vol {_pm_vol_ratio:.1f}×avg"
                                        elif _pm_vol_ratio >= 1.0:
                                            _vol_tag = f" vol {_pm_vol_ratio:.1f}×avg"
                                except Exception:
                                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                                _pm_alerts.append(
                                    f"  {'🔥' if _g>=0.03 else '📈'} *{_pm_sym}* "
                                    f"+{_g*100:.1f}% pre-market ${_pre_pm:.2f}{_vol_tag}")
                    except Exception:
                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                if _pm_alerts:
                    lines.append("\n*Pre-market breakout candidates:*")
                    lines.extend(_pm_alerts)
        except Exception as _pme:
            _log.debug("[briefing] PM12 scan candidates: %s", _pme)

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

                # VWAP check: price must be above VWAP (institutional support)
                # Below VWAP = selling pressure dominates, breakout momentum is fake
                _vwap_ok = True
                try:
                    if not df1.empty and len(df1) >= 2:
                        _tp   = (df1["High"] + df1["Low"] + df1["Close"]) / 3
                        _vwap = float((_tp * df1["Volume"]).cumsum().iloc[-1]
                                      / df1["Volume"].cumsum().iloc[-1])
                        _vwap_ok = cur_price >= _vwap * 0.995
                        if not _vwap_ok:
                            _log.info("[or_check] %s price $%.2f below VWAP $%.2f",
                                      sym, cur_price, _vwap)
                except Exception:
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                if cur_price < stop_p * 0.998:
                    cancel_all_orders(sym)
                    rs = _lrs()
                    rs.get("positions_risk", {}).pop(sym, None)
                    rs["open_risk_pct"] = sum(rs.get("positions_risk", {}).values())
                    _srs(rs)
                    cancelled.append((sym, "no_price_confirm", cur_price, stop_p, 0))
                    _log.info("[or_check] CANCEL %s — price $%.2f below stop $%.2f",
                              sym, cur_price, stop_p)
                elif not _vwap_ok:
                    cancel_all_orders(sym)
                    rs = _lrs()
                    rs.get("positions_risk", {}).pop(sym, None)
                    rs["open_risk_pct"] = sum(rs.get("positions_risk", {}).values())
                    _srs(rs)
                    vol_pct = vol_30min / vol_daily if vol_daily > 0 else 0
                    cancelled.append((sym, "below_vwap", cur_price, stop_p, vol_pct))
                    _log.info("[or_check] CANCEL %s — price $%.2f below VWAP (selling pressure)",
                              sym, cur_price)
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
                if reason == "no_price_confirm":
                    tag = f"price ${cur:.2f} < stop ${stop:.2f}"
                elif reason == "below_vwap":
                    tag = "below VWAP — selling pressure"
                else:
                    tag = f"weak vol {vol:.0%}"
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
                _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
        # ── This week's closed trades ─────────────────────────────────────────
        _week_ago  = today - timedelta(days=7)
        _wk_trades = []
        if journal_file.exists():
            for _wkl in journal_file.read_text().splitlines():
                try:
                    _wkt = json.loads(_wkl)
                    _wkts = _wkt.get("ts", "")
                    if _wkts and date.fromisoformat(_wkts[:10]) >= _week_ago:
                        _wk_trades.append(_wkt)
                except Exception:
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        if _wk_trades:
            lines.append(f"")
            _w_sorted = sorted(_wk_trades, key=lambda x: x.get("r_multiple", 0), reverse=True)
            lines.append(f"*This week's {len(_wk_trades)} closed trades:*")
            for _wt in _w_sorted:
                _wi = "✅" if _wt.get("r_multiple", 0) > 0 else "❌"
                _wcs = _wt.get("composite_score")
                _cs_str = f" (score={_wcs:.1f})" if _wcs else ""
                lines.append(
                    f"  {_wi} {_wt.get('symbol','?')}: "
                    f"{_wt.get('r_multiple', 0):+.2f}R "
                    f"({_wt.get('pnl_pct', 0):+.1f}%){_cs_str}"
                )
            _wk_wins = sum(1 for t in _wk_trades if t.get("r_multiple", 0) > 0)
            _wk_avg  = sum(t.get("r_multiple", 0) for t in _wk_trades) / len(_wk_trades)
            lines.append(f"  Week: {_wk_wins}W/{len(_wk_trades)-_wk_wins}L  avg {_wk_avg:+.2f}R")
            _maes = [t.get("mae_pct") for t in _wk_trades if t.get("mae_pct") is not None]
            _mfes = [t.get("mfe_pct") for t in _wk_trades if t.get("mfe_pct") is not None]
            if _maes and _mfes:
                lines.append(
                    f"  avg MAE {sum(_maes)/len(_maes):+.1f}%  |  "
                    f"avg MFE {sum(_mfes)/len(_mfes):+.1f}%")

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
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # ── Signal attribution: which screener flags correlate with winners ─────
        try:
            _sa_path = LOG_DIR / "signal_accuracy.json"
            if _sa_path.exists():
                _sa = json.loads(_sa_path.read_text())
                _sig_lines = []
                for _sname, _sdata in sorted(
                    _sa.items(),
                    key=lambda kv: -(kv[1].get("total_r", 0)),
                ):
                    _sw = _sdata.get("wins", 0)
                    _sl = _sdata.get("losses", 0)
                    _sr = _sdata.get("total_r", 0.0)
                    if _sw + _sl >= 3:
                        _swr = _sw / (_sw + _sl)
                        _sig_lines.append(
                            f"  {_sname}: {_sw}W/{_sl}L  WR={_swr:.0%}  R={_sr:+.1f}")
                if _sig_lines:
                    lines.append(f"\n*Signal accuracy (≥3 trades):*")
                    lines.extend(_sig_lines[:8])   # top 8 by total R
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        lines.append(f"")
        if ret_line:
            lines.append(ret_line)
        lines.append(f"Portfolio: ${portfolio_value:,.0f}")

        # ── Best & worst trades (all-time) ───────────────────────────────────
        if total_trades >= 3:
            _all_trades = []
            if journal_file.exists():
                for _atl in journal_file.read_text().splitlines():
                    try:
                        _all_trades.append(json.loads(_atl))
                    except Exception:
                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
            if _all_trades:
                _best  = max(_all_trades, key=lambda t: t.get("r_multiple", 0))
                _worst = min(_all_trades, key=lambda t: t.get("r_multiple", 0))
                lines.append("")
                lines.append("*Best & worst trades (all-time):*")
                lines.append(
                    f"  🏆 {_best.get('symbol','?')}: "
                    f"{_best.get('r_multiple',0):+.2f}R "
                    f"({_best.get('pnl_pct',0):+.1f}%) on {_best.get('ts','?')[:10]}")
                lines.append(
                    f"  💔 {_worst.get('symbol','?')}: "
                    f"{_worst.get('r_multiple',0):+.2f}R "
                    f"({_worst.get('pnl_pct',0):+.1f}%) on {_worst.get('ts','?')[:10]}")

        # ── Avg hold time: days-to-exit per trade ─────────────────────────────
        if total_trades >= 3 and _all_trades:
            import numpy as _np_ht
            _hold_days = []
            for _ht in _all_trades:
                try:
                    _ent = _ht.get("entry_date") or _ht.get("ts", "")[:10]
                    _ext = _ht.get("ts", "")[:10]
                    if _ent and _ext and len(_ent) == 10 and len(_ext) == 10:
                        _hd = int(_np_ht.busday_count(_ent, _ext))
                        if 0 < _hd < 120:
                            _hold_days.append(_hd)
                except Exception:
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
            if _hold_days:
                _avg_hold = sum(_hold_days) / len(_hold_days)
                lines.append(f"  Avg hold: {_avg_hold:.0f} trading days")

        # ── Last backtest results (written by Sunday cron job) ───────────────
        try:
            _bt_file = LOG_DIR / "weekly_backtest.json"
            if _bt_file.exists():
                import json as _jbt
                _bt = _jbt.loads(_bt_file.read_text())
                _bt_date = _bt.get("run_date", "?")
                _bt_n    = _bt.get("total_trades", 0)
                _bt_wr   = _bt.get("win_rate", 0)
                _bt_exp  = _bt.get("expectancy", 0)
                _bt_cagr = _bt.get("cagr_pct", 0)
                lines.append("")
                lines.append(f"*Backtest ({_bt_date}, 1yr):*")
                lines.append(f"  {_bt_n} trades  WR={_bt_wr:.0%}  "
                              f"E={_bt_exp:+.2f}R  CAGR={_bt_cagr:.1f}%")
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
        vix = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if not vix.empty:
            return float(vix["Close"].iloc[-1])
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
    """Return (True, reason) if within 1-2 calendar days BEFORE an FOMC or CPI release.
    Avoids new entries ahead of binary macro events that can gap past any stop.
    On the event day itself (delta=0): the scan runs at 22:30 CEST (20:30 UTC),
    always after US market close. Both FOMC (14:00 ET) and CPI (08:30 ET) are
    resolved before the scan — lift the blackout.
    """
    today = date.today()
    for (m, d) in _FOMC_2026 + _CPI_2026:
        try:
            event = date(today.year, m, d)
        except ValueError:
            continue
        delta = (event - today).days
        if 1 <= delta <= 2:
            kind = "FOMC" if (m, d) in _FOMC_2026 else "CPI"
            return True, f"{kind} {event}"
    return False, ""


# ── Power Trend (O'Neil / IBD) ────────────────────────────────────────────
def _fetch_power_trend() -> bool:
    """True if SPY 21d EMA > 50d EMA for 8+ consecutive days (O'Neil Power Trend).
    Signals confirmed bull acceleration — adds +1.0 to Tudor Jones score.
    """
    try:
        _df = yf.Ticker("SPY").history(period="90d", interval="1d", auto_adjust=True)
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
        _df = yf.Ticker("SPY").history(period="30d", interval="1d", auto_adjust=True)
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
    # Catalyst scoring: parse ai_reasoning for positive/negative catalyst keywords
    _ai_text = getattr(vcp, "ai_reasoning", "").lower()
    _pos_kws = ("strong catalyst", "positive news", "earnings beat", "guidance raised",
                "buyback", "fda approval", "contract win", "record revenue")
    _neg_kws = ("weak catalyst", "no catalyst", "negative news", "earnings miss",
                "guidance cut", "investigation", "recall")
    _cat_pts = 0.25 * sum(1 for k in _pos_kws if k in _ai_text)
    _cat_pts -= 0.25 * sum(1 for k in _neg_kws if k in _ai_text)
    _cat_pts = max(min(_cat_pts, 0.5), -0.5)
    return min(q + conf + tight_b + vol_b + bvol_b + rs_b + _cat_pts, 10.0)


def _fetch_vix_slope() -> float:
    """
    5-day change in VIX (points). Positive = fear rising = risk-off.
    Returns 0.0 on failure.
    """
    try:
        hist = yf.Ticker("^VIX").history(period="15d", interval="1d", auto_adjust=False)
        if len(hist) >= 6:
            return float(hist["Close"].iloc[-1] - hist["Close"].iloc[-6])
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
        hist = yf.Ticker("^TNX").history(period="35d", interval="1d", auto_adjust=False)
        if len(hist) >= 21:
            # ^TNX is in %, e.g. 4.50 means 4.50% — convert change to bps
            return float((hist["Close"].iloc[-1] - hist["Close"].iloc[-21]) * 100)
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return 0.0


def _dynamic_min_composite() -> float:
    """
    Auto-raise MIN_COMPOSITE based on bucket performance in feedback_state.json.
    Bucket keys: "5.0-6.0", "6.0-7.0", "7.0-8.0", "8.0+".
    Returns 5.0 (default), 6.0, or 7.0 depending on which buckets show negative expectancy.
    """
    try:
        import json as _j
        fb      = _j.loads((LOG_DIR / "feedback_state.json").read_text())
        buckets = fb.get("score_buckets", {})
        bkt_lo  = buckets.get("5.0-6.0", {})
        bkt_mid = buckets.get("6.0-7.0", {})

        lo_bad  = bkt_lo.get("count", 0) >= 5 and bkt_lo.get("avg_r", 0.0) < 0
        mid_bad = bkt_mid.get("count", 0) >= 5 and bkt_mid.get("avg_r", 0.0) < 0

        if lo_bad and mid_bad:
            _log.info("[score] Dynamic MIN raised to 7.0 — both low buckets negative "
                      "(5-6: %.2fR/%d, 6-7: %.2fR/%d)",
                      bkt_lo["avg_r"], bkt_lo["count"], bkt_mid["avg_r"], bkt_mid["count"])
            return 7.0
        if lo_bad:
            _log.info("[score] Dynamic MIN raised to 6.0 — 5.0-6.0 bucket avg=%.2fR (%d trades)",
                      bkt_lo["avg_r"], bkt_lo["count"])
            return 6.0
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return 5.0


def _fetch_pcr() -> float:
    """
    Fetch CBOE total Put/Call ratio as fear/greed gauge. Returns 0.7 (neutral) on failure.
    PCR > 1.0 = fear/contrarian buy (+0.5 Tudor pts); PCR < 0.6 = greed (-0.5 pts).
    """
    try:
        for tkr in ("^PCALL", "^CPC"):
            try:
                h = yf.Ticker(tkr).history(period="5d", interval="1d", auto_adjust=False)
                if not h.empty and not h["Close"].isna().all():
                    return float(h["Close"].dropna().iloc[-1])
            except Exception:
                continue
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
    # ADX: trend strength — >25 confirms price direction, >35 = strong momentum
    adx_val  = getattr(trend, "adx", 0.0)
    adx_pts  = 0.5 if adx_val >= 35 else (0.25 if adx_val >= 25 else 0.0)
    # Float rotation: 40-day vol / float shares — >1.0 = full float turned (institutional demand)
    fr_val   = getattr(trend, "float_rotation", None)
    fr_pts   = (0.5 if fr_val is not None and fr_val >= 1.5
                else (0.25 if fr_val is not None and fr_val >= 0.8 else 0.0))
    # Institutional ownership: smart money backing cushions pullbacks and confirms setup
    inst_val = getattr(trend, "inst_pct", None)
    inst_pts = (0.5 if inst_val is not None and inst_val >= 0.60
                else (0.25 if inst_val is not None and inst_val >= 0.40 else 0.0))
    # EPS beat history: consistent positive earnings surprises = management execution quality
    beat_cnt     = getattr(trend, "eps_beat_count", 0)
    beat_pts     = 0.25 if beat_cnt >= 2 else 0.0
    # Revenue beat proxy: EPS beats + strong revenue growth = double confirmation
    _rev_g_v     = getattr(trend, "revenue_growth", None)
    rev_beat_pts = 0.25 if (beat_cnt >= 2 and _rev_g_v is not None and _rev_g_v >= 0.10) else 0.0
    # 52-week high breakout: no overhead supply — cleanest possible Minervini setup
    at_52w_pts   = 0.5 if getattr(trend, "at_52w_high", False) else 0.0
    # Accumulation days in base: institutional buyers active on up-days (quality base)
    accum_r      = getattr(trend, "accum_ratio", 0.0)
    accum_pts    = 0.25 if accum_r >= 0.60 else 0.0
    # 3-weeks tight: Minervini's strongest base compression signal
    twt_pts = 1.0 if getattr(trend, "three_weeks_tight", False) else 0.0
    # OBV at 52w high: institutional accumulation confirmed in base
    obv_pts = 0.5 if getattr(trend, "obv_new_high", False) else 0.0
    # Base count: later bases have materially higher failure rates (Minervini SEPA)
    _bcnt_s  = getattr(trend, "base_count", 1)
    base_pts = 0.0 if _bcnt_s <= 2 else (-0.5 if _bcnt_s == 3 else -1.0)
    # Base age: stale consolidations (>120 trading days) lose momentum (Minervini)
    _bage    = getattr(trend, "base_age_days", 0)
    bage_pts = 0.0 if _bage <= 60 else (-0.25 if _bage <= 120 else -0.5)
    # Volume contraction quality: consistent volume decline = controlled institutional base
    _vq_s   = getattr(trend, "vol_contraction_quality", 0.0)
    vq_pts  = 0.5 if _vq_s >= 1.0 else (0.25 if _vq_s >= 0.5 else 0.0)
    # Near 3-year ATH: no overhead supply from prior distribution zones
    ath_pts = 0.5 if getattr(trend, "near_ath", False) else 0.0
    # Weinstein Stage: Stage 2 (advancing) = neutral; Stage 3 (topping) = -0.5 penalty
    _ws = getattr(trend, "weinstein_stage", 2)
    ws_pts = 0.5 if _ws == 2 else (-0.5 if _ws == 3 else 0.0)
    # Weekly Stage 2: MA10w > MA30w + MA30w slope rising = multi-timeframe alignment
    ws2_pts = 0.5 if getattr(trend, "weekly_stage2", False) else 0.0
    # Weekly breakout alignment: daily pivot coincides with weekly 5-week high breakout
    wba_pts = 0.5 if getattr(trend, "weekly_breakout_aligned", False) else 0.0
    # Analyst upgrades: net positive analyst activity = institutional attention building
    aug_pts = 0.25 if getattr(trend, "analyst_upgrades", False) else 0.0
    # Institutional accumulation trend: recent 13F filings = smart money building position
    inst_trend_pts = 0.25 if getattr(trend, "inst_ownership_increasing", False) else 0.0
    # EPS revision momentum: analyst consensus raised = earnings acceleration (Minervini SEPA)
    rev_up_pts = 0.5 if getattr(trend, "eps_revision_up", False) else 0.0
    # Pocket pivot: up-day volume exceeds all prior down-day volumes = early institutional entry
    pp_pts = 0.25 if getattr(trend, "pocket_pivot", False) else 0.0
    # Earnings acceleration: EPS growth rate accelerating Q-over-Q = highest-conviction Minervini setups
    accel_pts = 0.5 if getattr(trend, "eps_accelerating", False) else 0.0
    # 13-week accumulation: ≥8/13 up-volume weeks = sustained institutional demand (O'Neil breadth)
    aw_pts = 0.25 if getattr(trend, "accum_weeks_strong", False) else 0.0
    # Insider buying: open-market C-suite purchase = highest conviction alignment signal
    insider_pts = 0.5 if getattr(trend, "insider_buying", False) else 0.0
    # Industry leadership: sector ETF in top-4 by 6-month momentum = tide lifting all boats
    indleader_pts = 0.25 if getattr(trend, "industry_leader", False) else 0.0
    # Revenue acceleration: quarterly revenue growth accelerating Q-over-Q (double SEPA confirmation)
    rev_accel_pts = 0.5 if getattr(trend, "rev_accelerating", False) else 0.0
    # 3-weeks tight: consecutive weekly closes within 1.5% = institutional hold, no distribution
    twt2_pts = 0.25 if getattr(trend, "three_weeks_tight", False) else 0.0
    # Short interest monthly change: covering = squeeze fuel (+0.25), building = warning (-0.25)
    si_mo_pts = float(getattr(trend, "short_mo_pts", 0.0))
    # Analyst PT gap: consensus >25% above price = substantial institutional expected upside
    apt_pts = 0.25 if getattr(trend, "analyst_pt_upside", False) else 0.0
    return min(rs_pts + rs_sig + rsi_pts + hi_pts + sl_pts + eps_pts + trend_pts
               + ad_pts + short_pts + earn_pts + monthly_pts + rev_pts + sec_rs_pts + roe_pts
               + adx_pts + fr_pts + inst_pts + beat_pts + rev_beat_pts + at_52w_pts + accum_pts
               + twt_pts + obv_pts + base_pts + bage_pts + vq_pts + ath_pts + ws2_pts + wba_pts
               + aug_pts + inst_trend_pts + rev_up_pts + pp_pts + accel_pts + aw_pts
               + insider_pts + indleader_pts + rev_accel_pts
               + twt2_pts + si_mo_pts + apt_pts + ws_pts, 10.0)


def _market_follow_through_confirmed() -> bool:
    """
    O'Neil follow-through day: only block new entries when SPY is in a confirmed
    correction (>5% below MA50) AND no follow-through day (≥1.7% gain on above-avg
    volume, day 4+ from the rally low) has occurred in the last 25 sessions.
    Returns True (allow entries) in all other cases including errors.
    """
    try:
        _spy_ftd = yf.Ticker("SPY").history(period="80d", interval="1d", auto_adjust=True)
        if len(_spy_ftd) < 25:
            return True
        _cl_ftd  = _spy_ftd["Close"]
        _ma50    = float(_cl_ftd.tail(50).mean())
        _cur     = float(_cl_ftd.iloc[-1])
        if (_cur - _ma50) / _ma50 >= -0.05:
            return True   # within 5% of MA50 — no restriction
        # In correction: scan last 25 sessions for a follow-through day
        _vl_ftd  = _spy_ftd["Volume"]
        _avg_vol = float(_vl_ftd.tail(25).mean())
        _recent  = _spy_ftd.tail(25).reset_index(drop=True)
        # Find rally low first, then count from there
        _low_idx = int(_recent["Close"].argmin())
        for _i in range(_low_idx + 4, len(_recent)):
            _chg = (float(_recent["Close"].iloc[_i]) - float(_recent["Close"].iloc[_i - 1])) / float(_recent["Close"].iloc[_i - 1])
            if _chg >= 0.017 and float(_recent["Volume"].iloc[_i]) > _avg_vol:
                return True   # follow-through day confirmed
        return False
    except Exception:
        return True   # on error, allow entries


def _qqq_size_factor() -> float:
    """
    Returns 0.75 when QQQ is below its 50-day MA — growth stocks in unfavorable regime.
    VCPs are predominantly growth stocks; QQQ weakness = direct headwind.
    """
    try:
        _qqq = yf.Ticker("QQQ").history(
            period="80d", interval="1d", auto_adjust=True)["Close"]
        if len(_qqq) >= 51:
            _ma50_qq = float(_qqq.tail(50).mean())
            if float(_qqq.iloc[-1]) < _ma50_qq:
                _log.info("[tudor] QQQ below MA50 — growth regime weak, sizing capped 75%%")
                return 0.75
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return 1.0


def _fetch_credit_spread_factor() -> float:
    """
    Returns 0.80 when HYG/LQD ratio drops >2% over 5 days — widening credit spreads
    signal risk-off before it shows in equities. Leading indicator vs VIX (coincident).
    """
    try:
        import pandas as _pd_cs
        _cs_df = yf.download(["HYG", "LQD"], period="15d", interval="1d",
                                   auto_adjust=True, progress=False)["Close"]
        if "HYG" in _cs_df.columns and "LQD" in _cs_df.columns and len(_cs_df) >= 6:
            _ratio   = _cs_df["HYG"] / _cs_df["LQD"]
            _chg5d   = (float(_ratio.iloc[-1]) - float(_ratio.iloc[-6])) / float(_ratio.iloc[-6])
            if _chg5d < -0.02:
                _log.info("[tudor] Credit spread widening (HYG/LQD %.1f%% 5d) — sizing 80%%",
                          abs(_chg5d) * 100)
                return 0.80
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return 1.0


def _extended_market_factor() -> float:
    """
    Returns 0.7 when SPY is >7% above its 50-day MA — historically elevated stop-out risk.
    Markets this extended mean mean-reversion risk is high; we cap new-position sizing.
    """
    try:
        _spy_em = yf.Ticker("SPY").history(
            period="80d", interval="1d", auto_adjust=True)["Close"]
        if len(_spy_em) >= 51:
            _ma50_em = float(_spy_em.tail(50).mean())
            _ext_pct = (float(_spy_em.iloc[-1]) - _ma50_em) / _ma50_em
            if _ext_pct > 0.07:
                _log.info("[tudor] Extended market: SPY %.1f%% above MA50 — sizing capped 70%%",
                          _ext_pct * 100)
                return 0.7
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return 1.0


def _fetch_dxy_factor() -> float:
    """DXY rising >2% in 20 days = dollar strength = headwind for growth stocks.
    Uses UUP (Invesco Dollar Bullish ETF) as proxy. Returns 0.85 on strong dollar.
    """
    try:
        _dxy = yf.Ticker("UUP").history(period="30d", interval="1d", auto_adjust=True)["Close"]
        if len(_dxy) >= 21:
            _dxy_ret = float(_dxy.iloc[-1] / _dxy.iloc[-21] - 1)
            if _dxy_ret > 0.02:
                _log.info("[main] DXY +%.1f%% (20d) — dollar strength, risk_pct -15%%",
                          _dxy_ret * 100)
                return 0.85
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return 1.0


def _portfolio_beta_factor(positions: list) -> float:
    """Weighted portfolio beta vs SPY over 60 days.
    If beta > 1.5 (over-concentrated in high-beta names) → size new entries at 80%%.
    """
    try:
        if not positions:
            return 1.0
        import pandas as _pd_beta
        _syms = [p["symbol"] for p in positions]
        _total_val = sum(float(p.get("qty", 0)) * float(p.get("current_price", 0))
                         for p in positions)
        if _total_val <= 0:
            return 1.0
        _df_b = yf.download(_syms + ["SPY"], period="60d", interval="1d",
                                   auto_adjust=True, progress=False)["Close"]
        _spy_r = _df_b["SPY"].pct_change().dropna() if "SPY" in _df_b.columns else None
        if _spy_r is None or len(_spy_r) < 20:
            return 1.0
        _spy_var = float(_spy_r.var())
        if _spy_var <= 0:
            return 1.0
        _port_beta = 0.0
        for _p in positions:
            _s = _p["symbol"]
            if _s not in _df_b.columns:
                continue
            _w = float(_p.get("qty", 0)) * float(_p.get("current_price", 0)) / _total_val
            _sr = _df_b[_s].pct_change().dropna()
            _al = _pd_beta.concat([_sr, _spy_r], axis=1, join="inner").dropna()
            if len(_al) < 20:
                continue
            _b_i = float(_al.iloc[:, 0].cov(_al.iloc[:, 1])) / _spy_var
            _port_beta += _w * _b_i
        if _port_beta > 1.5:
            _log.info("[main] Portfolio beta=%.2f > 1.5 — new entry size reduced 20%%",
                      _port_beta)
            return 0.80
        return 1.0
    except Exception:
        return 1.0


def _beta_size_factor(symbol: str) -> float:
    """Reduce position size for high-beta stocks.
    Beta > 2.0 → 0.70, Beta 1.5-2.0 → 0.85, else 1.0.
    Uses yfinance info; cached per symbol for the session.
    """
    try:
        _beta = yf.Ticker(symbol).info.get("beta")
        if _beta is None:
            return 1.0
        _beta = float(_beta)
        if _beta > 2.0:
            _log.info("[main] %s beta=%.1f → size 70%%", symbol, _beta)
            return 0.70
        if _beta > 1.5:
            _log.info("[main] %s beta=%.1f → size 85%%", symbol, _beta)
            return 0.85
        return 1.0
    except Exception:
        return 1.0


def _atr_volatility_factor(symbol: str, entry_price: float) -> float:
    """Reduce position size when 14-day ATR/price > 4%% — avoids oversizing volatile stocks.
    High ATR means wider natural swings; 1R per trade requires fewer shares.
    """
    try:
        _df = yf.Ticker(symbol).history(period="30d", interval="1d", auto_adjust=True)
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
        _df = yf.Ticker("SPY").history(period="40d", interval="1d", auto_adjust=True)
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
        _hl = yf.Ticker("^NYHL").history(period="5d", interval="1d", auto_adjust=True)
        if len(_hl) >= 1:
            _net = float(_hl["Close"].iloc[-1])
            if _net > 150:   return 2.0   # strong expansion
            if _net > 50:    return 1.5   # mild expansion
            if _net < -150:  return 0.25  # deteriorating
            if _net < -50:   return 0.5   # weakening
            return 1.0
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return 1.0   # neutral fallback when data unavailable


def _compute_ad_divergence(breadth_pct: float) -> bool:
    """True when SPY is rising but market breadth is falling — classic distribution signal.
    Compares today's breadth to 10-scan-ago breadth vs SPY 10-day price return.
    Uses breadth_history.json built during daily scans.
    """
    try:
        import json as _json_ad, os as _os_ad
        _bh_path = str(BASE_DIR / "logs" / "breadth_history.json")
        if not _os_ad.path.exists(_bh_path):
            return False
        with open(_bh_path) as _f_ad:
            _bh = _json_ad.load(_f_ad)
        if len(_bh) < 10:
            return False
        _breadth_chg = breadth_pct - float(_bh[-10])  # negative = breadth shrinking
        if _breadth_chg >= -0.03:                       # need ≥3pp breadth decline
            return False
        _spy_hist = yf.Ticker("SPY").history(period="20d", interval="1d", auto_adjust=True)["Close"]
        if len(_spy_hist) < 11:
            return False
        _spy_10d = float(_spy_hist.iloc[-1] / _spy_hist.iloc[-10] - 1)
        return _spy_10d >= 0.01   # SPY up ≥1% while breadth fell ≥3pp = hidden weakness
    except Exception:
        return False


_VTS_CACHE: tuple[float, float] = (0.0, 0.0)

def _vix_term_structure_pts() -> float:
    """VIX / VIX3M ratio as precision market-timing signal.
    Backwardation (VIX > VIX3M * 1.05) = fear spike usually exhausting → +0.25 Tudor pts.
    Deep contango (VIX3M > VIX * 1.15) = market pricing in rising future risk → -0.25 pts.
    Cached 4 hours.
    """
    global _VTS_CACHE
    _vts_pts, _vts_ts = _VTS_CACHE
    import time as _time_vts
    if _time_vts.time() - _vts_ts < 14400:
        return _vts_pts
    try:
        _vd = yf.download(["^VIX", "^VIX3M"], period="5d", interval="1d",
                                progress=False, auto_adjust=False)["Close"]
        _vix_now  = float(_vd["^VIX"].dropna().iloc[-1])
        _vix3m    = float(_vd["^VIX3M"].dropna().iloc[-1])
        if _vix3m > 0:
            _ratio = _vix_now / _vix3m
            if _ratio > 1.05:
                _pts = 0.25    # backwardation: fear spike, often marks short-term bottom
            elif _ratio < (1 / 1.15):
                _pts = -0.25   # deep contango: market pricing in sustained future vol
            else:
                _pts = 0.0
        else:
            _pts = 0.0
        _VTS_CACHE = (_pts, _time_vts.time())
        return _pts
    except Exception:
        return 0.0


_YC_CACHE:  tuple[bool, float] = (False, 0.0)
_CSW_CACHE: tuple[bool, float] = (False, 0.0)

def _yield_curve_inverted() -> bool:
    """3-month T-bill yield > 10-year yield = inverted curve = late cycle signal.
    Cached 4 hours to avoid flooding Yahoo Finance. Returns False on error.
    """
    global _YC_CACHE
    _val, _ts = _YC_CACHE
    import time as _time_yc
    if _time_yc.time() - _ts < 14400:
        return _val
    try:
        _yc_df = yf.download(["^IRX", "^TNX"], period="5d", interval="1d",
                                  progress=False, auto_adjust=False)["Close"]
        _3m  = float(_yc_df["^IRX"].dropna().iloc[-1])
        _10y = float(_yc_df["^TNX"].dropna().iloc[-1])
        _inv = _3m > _10y
        _YC_CACHE = (_inv, _time_yc.time())
        return _inv
    except Exception:
        return False


def _credit_spreads_wide() -> bool:
    """HYG (high-yield ETF) 20-day return < -2% AND underperforms TLT by ≥2pp
    = risk-off, credit markets deteriorating. Cached 4 hours.
    """
    global _CSW_CACHE
    _val2, _ts2 = _CSW_CACHE
    import time as _time_cs
    if _time_cs.time() - _ts2 < 14400:
        return _val2
    try:
        _csdf = yf.download(["HYG", "TLT"], period="30d", interval="1d",
                                  progress=False, auto_adjust=True)["Close"]
        _hyg_r = float(_csdf["HYG"].iloc[-1] / _csdf["HYG"].iloc[-21] - 1)
        _tlt_r = float(_csdf["TLT"].iloc[-1] / _csdf["TLT"].iloc[-21] - 1)
        _wide  = _hyg_r < -0.02 and (_hyg_r - _tlt_r) < -0.02
        _CSW_CACHE = (_wide, _time_cs.time())
        return _wide
    except Exception:
        return False


def _tudor_score(risk_state: dict, regime: str, breadth_pct: float = 0.5,
                  power_trend: bool = False, pcr: float = 0.7,
                  rate_slope_bps: float = 0.0, vix_slope: float = 0.0,
                  dist_days: int = 0, nh_nl_ratio: float = 1.0,
                  breadth_trend: int = 0, ad_divergence: bool = False) -> float:
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
    # Breadth trend direction: is market breadth rising or falling vs recent average?
    breadth_dir_pts = 0.5 if breadth_trend > 0 else (-0.5 if breadth_trend < 0 else 0.0)
    # A/D divergence: SPY rising while breadth declining = distribution under the surface
    ad_div_pts = -0.5 if ad_divergence else 0.0
    # Yield curve: 3-month yield > 10-year = inverted = late economic cycle risk
    yc_pts  = -0.5 if _yield_curve_inverted() else 0.0
    # Credit spreads: HYG falling relative to TLT = institutional risk-off signal
    csw_pts = -0.5 if _credit_spreads_wide() else 0.0
    # VIX term structure: backwardation = fear exhausting (+0.25); deep contango = rising risk (-0.25)
    vts_pts = _vix_term_structure_pts()
    return min(reg_pts + loss_pts + heat_pts + breadth_pts + power_pts + pcr_pts + rate_pts + vix_pts + dist_pts + nh_nl_pts + breadth_dir_pts + ad_div_pts + yc_pts + csw_pts + vts_pts, 10.0)


def _composite_score(vcp, trend, risk_state: dict, regime: str,
                     sector_bonus: float = 0.0, breadth_pct: float = 0.5,
                     power_trend: bool = False, pcr: float = 0.7,
                     rate_slope_bps: float = 0.0, vix_slope: float = 0.0,
                     dist_days: int = 0, nh_nl_ratio: float = 1.0,
                     breadth_trend: int = 0, ad_divergence: bool = False) -> float:
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
    t = _tudor_score(risk_state, regime, breadth_pct, power_trend, pcr, rate_slope_bps, vix_slope, dist_days, nh_nl_ratio, breadth_trend, ad_divergence)
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




def _sector_momentum_scores() -> tuple[dict[str, float], dict[str, bool]]:
    """Return ({etf: 21d_return_vs_SPY}, {etf: above_MA200}) for SPDR sector ETFs.
    Stage 2 flag (above MA200) required for positive sector bonus to apply.
    """
    try:
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
            base = 0.5 if in_s2 else 0.25
        elif rel > 0.005:
            base = 0.25
        elif rel < -0.015:
            base = -0.5
        elif rel < -0.005:
            base = -0.25
        else:
            base = 0.0
        # Rank modifier: top-3 sectors get amplified bonus; bottom-3 get forced penalty
        if sector_scores:
            _sorted_etfs = sorted(sector_scores, key=lambda e: -sector_scores[e])
            _n    = len(_sorted_etfs)
            _rank = (_sorted_etfs.index(etf) + 1) if etf in _sorted_etfs else _n // 2
            if _rank <= 3 and base > 0:
                base = min(base + 0.25, 0.5)    # top-3: amplify positive momentum
            elif _n >= 5 and _rank > _n - 2 and base >= 0:
                base = -0.25                     # bottom-3: minimum penalty even if "neutral"
        return base
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
    if not _SCAN_LOCK.acquire(blocking=False):
        _log.warning("[main] Scan already running — ignoring concurrent trigger")
        _tg("⚠️ *Three Masters* — scan already in progress, duplicate trigger ignored")
        return
    try:
        _run_daily_impl()
    finally:
        _SCAN_LOCK.release()


def _archive_old_jsonl(max_age_days: int = 180):
    """Move JSONL log entries older than max_age_days to logs/archive/.
    Keeps the active files lean; archived files are never deleted.
    """
    import json as _j
    archive_dir = LOG_DIR / "archive"
    archive_dir.mkdir(exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    for fname in ("trade_journal.jsonl", "sync_audit.jsonl"):
        src = LOG_DIR / fname
        if not src.exists():
            continue
        try:
            lines = src.read_text().splitlines()
            keep, old = [], []
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    rec = _j.loads(ln)
                    ts_str = rec.get("closed_at") or rec.get("timestamp") or rec.get("date", "")
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else cutoff
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    (old if ts < cutoff else keep).append(ln)
                except Exception:
                    keep.append(ln)  # keep unparseable lines in active file
            if old:
                arc_file = archive_dir / fname
                with open(arc_file, "a") as af:
                    af.write("\n".join(old) + "\n")
                src.write_text("\n".join(keep) + ("\n" if keep else ""))
                _log.info("[main] Archived %d old entries from %s", len(old), fname)
        except Exception as e:
            _log.warning("[main] JSONL archive failed for %s: %s", fname, e)


def _run_daily_impl():
    """Actual pipeline — always called under _SCAN_LOCK."""
    _heartbeat()
    _archive_old_jsonl()

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

    _breadth_pct    = 0.5   # updated after screener run
    _breadth_trend  = 0     # +1=rising -1=falling 0=flat vs 3-day avg
    _ad_divergence  = False # True if SPY up but breadth falling (computed after screener)
    _current_vix    = 20.0  # updated in scoring loop (also needs to be in scope here)

    # ── Layer 1: Simons — Trend Template screening ────────────────────────────
    _log.info("\n[LAYER 1 — SIMONS] Trend Template screening...")
    try:
        from screener import run as screen_universe, load_universe
        symbols = load_universe()
        _log.info("[simons] Universe: %d symbols", len(symbols))
        screen_results = screen_universe(symbols=symbols)
        trend_passed = [r for r in screen_results if r.passed]
        # RVOL pre-filter: remove dormant (rvol_5d < 0.8) AND distribution (rvol_5d > 2.0)
        # Upper bound: sustained elevated volume during the base = distribution, not dry-up
        _before_rvol = len(trend_passed)
        trend_passed = [r for r in trend_passed if 0.8 <= r.rvol_5d <= 2.0]
        if len(trend_passed) < _before_rvol:
            _log.info("[main] RVOL filter: %d candidates removed (rvol outside 0.8-2.0)",
                      _before_rvol - len(trend_passed))
        # Market breadth: % of screened universe above MA50 (Tudor Jones signal)
        _breadth_pct = (sum(1 for r in screen_results if r.price > r.ma50 > 0)
                        / max(len(screen_results), 1))
        _log.info("[tudor] Market breadth: %.0f%% of %d symbols above MA50",
                  _breadth_pct * 100, len(screen_results))
        # Breadth trend: compare today vs 3-day rolling average from history file
        try:
            import json as _json_bt, os as _os_bt
            _bh_path = str(BASE_DIR / "logs" / "breadth_history.json")
            _bh: list = []
            if _os_bt.path.exists(_bh_path):
                with open(_bh_path) as _f_bt:
                    _bh = _json_bt.load(_f_bt)
            if len(_bh) >= 3:
                _avg_prev = sum(_bh[-3:]) / 3
                _breadth_trend = (1 if _breadth_pct > _avg_prev + 0.03
                                  else (-1 if _breadth_pct < _avg_prev - 0.03 else 0))
                _log.info("[tudor] Breadth trend: %s (today=%.0f%% avg3=%.0f%%)",
                          {1:"rising",0:"flat",-1:"falling"}[_breadth_trend],
                          _breadth_pct * 100, _avg_prev * 100)
            _bh.append(round(_breadth_pct, 4))
            _bh = _bh[-30:]
            with open(_bh_path, "w") as _f_bt:
                _json_bt.dump(_bh, _f_bt)
        except Exception as _bte:
            _log.debug("[tudor] Breadth history error: %s", _bte)
        # A/D divergence: SPY rising while breadth deteriorating
        _ad_divergence = _compute_ad_divergence(_breadth_pct)
        if _ad_divergence:
            _log.info("[tudor] A/D DIVERGENCE detected: SPY rising but breadth declining — −0.5 T-pts")
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

    _heartbeat()   # Layer 1 done — screener can take 10-20 min

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
        _cands_str = ", ".join(r.symbol for r in vcp_passed) or "none"
        _log.warning("[main] BEAR regime — skipping long order placement. %d VCPs found.", len(vcp_passed))

        # ── Bear-regime hedge: buy SH (ProShares Short S&P500, 1×inverse) ────
        # Tudor Jones rule: in confirmed bear markets, hold inverse positions to
        # profit from continued downside. 1x inverse (SH) only — no leverage.
        _bear_hedge_placed = False
        _sh_held = any(p["symbol"] in ("SH", "PSQ") for p in positions)
        if not _sh_held:
            try:
                from broker import place_buy_stop as _pbs_sh
                from risk_manager import position_size as _psz_sh, register_trade as _reg_sh
                _sh_fi = yf.Ticker("SH").fast_info
                _sh_px = float(getattr(_sh_fi, "last_price", None) or
                               getattr(_sh_fi, "regularMarketPrice", 0))
                if _sh_px > 0:
                    _sh_stop = round(_sh_px * 0.95, 2)   # 5% stop on the hedge
                    _sh_sz   = _psz_sh(portfolio_value, _sh_px, _sh_stop, risk_pct=0.005)
                    if _sh_sz["shares"] >= 1:
                        _sh_oid = _pbs_sh("SH", _sh_sz["shares"], _sh_px)
                        if _sh_oid:
                            _reg_sh("SH", _sh_sz["risk_pct"])
                            _bear_hedge_placed = True
                            _log.info("[main] BEAR HEDGE: SH %d sh @ $%.2f (1%% risk)",
                                      _sh_sz["shares"], _sh_px)
                            _tg(f"🐻 *Bear Hedge Placed — SH*\n"
                                f"{_sh_sz['shares']} shares @ ${_sh_px:.2f} "
                                f"(risk ${_sh_sz['risk_amount']:.0f})\n"
                                f"SPY {spy_pct*100:+.1f}% vs MA200 — bear market confirmed")
            except Exception as _sh_e:
                _log.warning("[main] Bear hedge (SH) failed: %s", _sh_e)

        msg = (f"SPY ${spy_price:.0f} is {abs(spy_pct):.1f}% below MA200 — bear market.\n"
               f"VCP setups found: {_cands_str}\n"
               f"No long orders placed."
               + (" SH hedge placed." if _bear_hedge_placed else ""))
        _tg(f"🐻 *Three Masters — Bear Regime*\n{msg}")
        report["summary"] = "bear_regime_no_orders"
        report["vcp_found_no_orders"] = [r.symbol for r in vcp_passed]
        _save_report(report)
        _send_daily_summary(report, len(trend_passed), len(vcp_passed), portfolio_value)
        return

    # ── Breadth gate: pause entries when broad market is deteriorating ────────
    if _breadth_pct < 0.45 and regime != "bull":
        _cands_str = ", ".join(r.symbol for r in vcp_passed) or "none"
        msg = (f"Market breadth {_breadth_pct*100:.0f}% above MA50 (threshold 45%) "
               f"in {regime.upper()} regime.\n"
               f"VCP setups found: {_cands_str}\n"
               f"No orders placed — pausing entries during broad market deterioration.")
        _log.warning("[main] BREADTH GATE — %.0f%% above MA50 in %s regime",
                     _breadth_pct * 100, regime)
        _tg(f"\U0001f4c9 *Three Masters — Breadth Gate*\n{msg}")
        report["summary"] = f"breadth_gate_{regime}_no_orders"
        report["vcp_found_no_orders"] = [r.symbol for r in vcp_passed]
        _save_report(report)
        _send_daily_summary(report, len(trend_passed), len(vcp_passed), portfolio_value)
        return

    regime_size_factor = 0.75 if regime == "neutral" else 1.0
    if regime == "neutral":
        _log.info("[main] Neutral regime (SPY %+.1f%% vs MA200) — position sizing at 75%%",
                  spy_pct * 100)

    _extended_factor = _extended_market_factor()
    _qqq_factor      = _qqq_size_factor()
    _cs_factor       = _fetch_credit_spread_factor()
    _beta_f          = _portfolio_beta_factor(positions)
    _dxy_f           = _fetch_dxy_factor()

    # ── Macro blackout: skip new orders within 2 days of FOMC/CPI ────────────
    _blackout, _blackout_reason = _is_macro_blackout()
    if _blackout:
        _cands_str = ", ".join(r.symbol for r in vcp_passed) or "none"
        msg = (f"\U0001f4c5 *Macro Blackout \u2014 {_blackout_reason}*\n"
               f"VCP setups found: {_cands_str}\n"
               f"No orders placed \u2014 FOMC/CPI in 1\u20132 days, avoiding binary event risk.")
        _log.warning("[main] MACRO BLACKOUT (%s) \u2014 skipping order placement",
                     _blackout_reason)
        _tg(msg)
        report["summary"] = f"macro_blackout_{_blackout_reason}"
        report["vcp_found_no_orders"] = [r.symbol for r in vcp_passed]
        _save_report(report)
        _send_daily_summary(report, len(trend_passed), len(vcp_passed), portfolio_value)
        return

    # ── Earnings cluster guard: ≥2 held positions reporting same week ────────
    # Concentrated binary risk: multiple simultaneous earnings = correlated gap risk.
    # Block new orders for the week; existing positions already have earnings protection.
    try:
        from screener import _days_to_earnings as _dte_fn
        _earn_this_week = [
            p["symbol"] for p in positions
            if (_dte_fn(p["symbol"]) or 99) <= 7
        ]
        if len(_earn_this_week) >= 2:
            _vcp_cands_str = ", ".join(r.symbol for r in vcp_passed) or "none"
            _msg_ec = (
                f"📅 *Earnings Cluster — {len(_earn_this_week)} positions reporting this week*\n"
                f"Held positions: {', '.join(_earn_this_week)}\n"
                f"VCP setups found: {_vcp_cands_str}\n"
                f"No new orders placed — reducing binary risk concentration."
            )
            _log.warning("[tudor] EARNINGS CLUSTER: %d positions report this week (%s) — blocking new orders",
                         len(_earn_this_week), _earn_this_week)
            _tg(_msg_ec)
            report["summary"] = f"earnings_cluster_{len(_earn_this_week)}_positions"
            report["vcp_found_no_orders"] = [r.symbol for r in vcp_passed]
            _save_report(report)
            _send_daily_summary(report, len(trend_passed), len(vcp_passed), portfolio_value)
            return
    except Exception as _ec_err:
        _log.debug("[tudor] earnings cluster check failed: %s", _ec_err)

    # ── Layer 3 + Execution: Tudor Jones — Size + Place Orders ────────────────
    _log.info("\n[LAYER 3 — TUDOR JONES] Position sizing & order placement...")
    from risk_manager import position_size, register_trade, check_can_trade
    from broker import place_buy_stop
    from screener import get_sector
    from config import RISK

    held_symbols = {p["symbol"] for p in positions}

    _heartbeat()   # Layer 2 done — Claude VCP analysis can take 10-30 min

    # Re-entry cooldown: skip symbols stopped out within the last 5 trading days
    from risk_manager import check_reentry_cooldown, check_pivot_failure_cooldown
    _before_cd = len(vcp_passed)
    vcp_passed  = [r for r in vcp_passed if not check_reentry_cooldown(r.symbol, r.current_price)]
    vcp_passed  = [r for r in vcp_passed if not check_pivot_failure_cooldown(r.symbol)]
    _skipped_cd = _before_cd - len(vcp_passed)
    if _skipped_cd:
        _log.info("[main] %d candidate(s) in re-entry/pivot-failure cooldown — skipped", _skipped_cd)

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
    # VIX spike: if VIX jumps >15% in a single session, markets are in sudden distress
    _vix_spike_today = False
    try:
        _vix_hist = yf.Ticker("^VIX").history(period="5d", interval="1d", auto_adjust=False)
        if len(_vix_hist) >= 2:
            _vix_prev = float(_vix_hist["Close"].iloc[-2])
            _vix_chg  = (_current_vix - _vix_prev) / _vix_prev if _vix_prev > 0 else 0.0
            if _vix_chg > 0.15:
                _vix_spike_today = True
                _log.warning("[tudor] VIX SPIKE: %.1f → %.1f (+%.0f%%) — sudden market distress",
                             _vix_prev, _current_vix, _vix_chg * 100)
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
        import pandas as _pd_rs
        _etf_closes: dict = {}
        for _etf in set(_SECTOR_ETF_MAP.values()):
            try:
                _h = yf.Ticker(_etf).history(period="1y", interval="1d", auto_adjust=True)
                if not _h.empty:
                    _etf_closes[_etf] = _h["Close"]
            except Exception:
                _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    _log.info("[tudor] 10Y yield slope=%.0fbps (%s)",
              _rate_slope_bps,
              "rising-hard(-1.0)" if _rate_slope_bps > 50 else
              ("rising(-0.5)" if _rate_slope_bps > 25 else "neutral"))
    if _power_trend:
        _log.info("[tudor] Power Trend active (SPY 21d EMA > 50d EMA \u22658 days) +1.0 T-pts")
    _dist_days    = _fetch_distribution_days()
    _ftd_ok       = _market_follow_through_confirmed()
    _nh_nl_ratio  = _fetch_nh_nl_ratio()
    _log.info("[tudor] Distribution days=%d (%s)", _dist_days,
              "institutional-selling(-1.5)" if _dist_days >= 5 else
              ("caution(-0.5)" if _dist_days >= 3 else "healthy"))
    _log.info("[tudor] NH/NL ratio=%.2f (%s)", _nh_nl_ratio,
              "expanding(+0.5)" if _nh_nl_ratio >= 1.5 else
              ("deteriorating(-1.0)" if _nh_nl_ratio < 0.5 else "neutral"))

    # ── Hard NH/NL internal deterioration block ───────────────────────────────
    # NH/NL < 0.25 in neutral/bear regime = critically collapsing tape.
    # Tudor Jones: "when market internals are screaming, listen — don't fight it."
    if _nh_nl_ratio < 0.25 and regime != "bull" and max_new_pos > 0:
        _log.warning("[tudor] NH/NL CRITICAL (%.2f) in %s regime — all new orders blocked",
                     _nh_nl_ratio, regime)
        _tg(f"🚨 *Market Internals Gate*\n"
            f"NYSE NH/NL ratio {_nh_nl_ratio:.2f} — critically few new highs\n"
            f"No new orders in {regime.upper()} regime (Tudor Jones internal breadth rule)")
        max_new_pos = 0
    _log.info("[tudor] PCR=%.2f (%s)", _current_pcr,
              "fear+0.5" if _current_pcr > 1.0 else ("greed-0.5" if _current_pcr < 0.6 else "neutral"))
    scored  = []
    for vcp in vcp_passed:
        trend_r = trend_map.get(vcp.symbol)
        if trend_r is None:
            scored.append((vcp, trend_r, 0.0))
            continue
        sec_bonus = _sector_bonus(vcp.symbol, sector_momentum, sector_stage2)
        cs = _composite_score(vcp, trend_r, _rs_now, regime, sec_bonus, _breadth_pct, _power_trend, _current_pcr, _rate_slope_bps, _vix_slope, _dist_days, _nh_nl_ratio, _breadth_trend, _ad_divergence)
        _log.info("[score] %s  M=%.1f S=%.1f T=%.1f sec=%+.2f breadth=%.0f%% pt=%s pcr=%.2f rate=%+.0fbps vix_sl=%.1f → composite=%.2f",
                  vcp.symbol,
                  _minervini_score(vcp), _simons_score(trend_r),
                  _tudor_score(_rs_now, regime, _breadth_pct, _power_trend, _current_pcr, _rate_slope_bps, _vix_slope, _dist_days, _nh_nl_ratio, _breadth_trend, _ad_divergence), sec_bonus,
                  _breadth_pct * 100, "✓" if _power_trend else "✗", _current_pcr, _rate_slope_bps, _vix_slope, cs)
        scored.append((vcp, trend_r, cs))

    # Filter: require composite >= 5.0 (guards against weak Simons/Tudor context)
    _MIN_COMPOSITE = _dynamic_min_composite()
    # Losing streak quality gate: ≥4 consecutive losses = filter tightens to elite-only setups
    # Separate from consecutive_loss_factor (which reduces SIZE); this reduces QUANTITY of entries.
    if loss_streak >= 4 and _MIN_COMPOSITE < 7.0:
        _log.warning("[score] LOSS STREAK GATE: %d losses — MIN_COMPOSITE %.1f→7.0",
                     loss_streak, _MIN_COMPOSITE)
        _tg("\U0001f4ca *Loss Streak Gate — quality filter raised*\n"
            + f"{loss_streak} consecutive losses\n"
            + "MIN_COMPOSITE raised to 7.0 until next winning trade")
        _MIN_COMPOSITE = 7.0
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

    # Peer sector confirmation: when >=2 VCPs fire in the same sector, sector has a tailwind
    _sec_vcp_cnt: dict[str, int] = {}
    for _pv, _pt, _pcs in vcp_scored:
        _ps = get_sector(_pv.symbol)
        _sec_vcp_cnt[_ps] = _sec_vcp_cnt.get(_ps, 0) + 1
    _hot_sectors = {_s for _s, _n in _sec_vcp_cnt.items() if _n >= 2}
    if _hot_sectors:
        vcp_scored = [
            (_pv, _pt, min(_pcs + (0.3 if get_sector(_pv.symbol) in _hot_sectors else 0.0), 10.0))
            for _pv, _pt, _pcs in vcp_scored
        ]
        _peer_syms = [_pv.symbol for _pv, _pt, _pcs in vcp_scored
                      if get_sector(_pv.symbol) in _hot_sectors]
        _log.info("[score] Peer sector boost +0.3: %s (sectors: %s)", _peer_syms, _hot_sectors)
        vcp_scored.sort(key=lambda x: -x[2])

    _log.info("[score] Order of priority: %s",
              [(v.symbol, cs) for v, t, cs in vcp_scored])

    # Daily P&L guard: Tudor Jones principle — never add risk on a bad day
    _daily_pnl_chk = _rs_now.get("daily_pnl_pct", 0.0)
    if _daily_pnl_chk < -0.02:
        _log.warning("[tudor] DAILY P&L GUARD: portfolio down %.1f%% today — new orders blocked",
                     _daily_pnl_chk * 100)
        _tg(f"🛡️ *Daily P&L Guard*\nPortfolio down {_daily_pnl_chk*100:.1f}% today — "
            f"no new orders (Tudor Jones: never add risk on a bad day)")
        max_new_pos = 0

    # VIX spike filter: single-day VIX jump >15% = sudden market distress → pause entries
    if _vix_spike_today and max_new_pos > 0:
        _log.warning("[tudor] VIX SPIKE >15%% — all new orders blocked for today")
        _tg("🚨 *VIX Spike — orders paused*\nVIX jumped >15% today — no new breakout entries (sudden market distress)")
        max_new_pos = 0

    # Follow-through day gate: SPY >5%% below MA50 without confirmed FTD = block entries
    if not _ftd_ok and max_new_pos > 0:
        _log.warning("[tudor] FTD gate: SPY in correction, no follow-through confirmed — entries blocked")
        _tg("🛑 *Follow-Through Gate — entries paused*\nSPY >5%% below MA50, no FTD confirmed\nWaiting for O'Neil uptrend confirmation")
        max_new_pos = 0

    # Intraday entry window: midday 11:00-14:30 ET = historically weaker breakout follow-through
    # Morning momentum and EOD strength windows are preferred; halve new positions at midday.
    import pytz as _pytz_iw
    from datetime import time as _dtime
    _now_et_iw = datetime.now(_pytz_iw.timezone("America/New_York")).time()
    if _dtime(11, 0) <= _now_et_iw < _dtime(14, 30) and max_new_pos > 1:
        _log.info("[tudor] Midday window (11:00-14:30 ET) — max_new_pos %d→%d (weaker follow-through)",
                  max_new_pos, max(1, max_new_pos // 2))
        max_new_pos = max(1, max_new_pos // 2)

    # Friday weekend risk gate: weekend gaps cannot be managed with GTC stops
    # Tudor Jones: reduce risk before unmanageable uncertainty, restore after confirmation.
    if datetime.now().weekday() == 4 and max_new_pos > 1:
        _log.info("[tudor] Friday — max_new_pos %d→%d (weekend gap risk reduction)",
                  max_new_pos, max(1, max_new_pos // 2))
        max_new_pos = max(1, max_new_pos // 2)

    # Seasonal momentum factor: May-Oct historically weak for momentum strategies
    # "Sell in May" effect is strongest for high-beta momentum names (academic consensus)
    _month_now = datetime.now().month
    if _month_now in (5, 6, 7, 8, 9, 10) and max_new_pos > 1:
        _log.info("[tudor] Seasonal weak window (May-Oct) — max_new_pos %d→%d",
                  max_new_pos, max_new_pos - 1)
        max_new_pos = max(1, max_new_pos - 1)

    # SPY choppiness gate: 14-day ATR/price > 1.5% = directionless market
    # VIX catches fear spikes; this catches sideways grind where breakouts consistently fail
    try:
        _spy_chop = yf.Ticker("SPY").history(period="30d", interval="1d", auto_adjust=True)
        if len(_spy_chop) >= 15:
            _tr_chop = (_spy_chop["High"] - _spy_chop["Low"]).tail(14).mean()
            _pr_chop = float(_spy_chop["Close"].iloc[-1])
            _atr_pct = float(_tr_chop) / _pr_chop if _pr_chop > 0 else 0.0
            if _atr_pct > 0.015 and max_new_pos > 1:
                _log.info("[tudor] SPY CHOPPY: 14d ATR/price=%.1f%% > 1.5%% — max_new_pos %d→%d",
                          _atr_pct * 100, max_new_pos, max(1, max_new_pos // 2))
                max_new_pos = max(1, max_new_pos // 2)
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    _reject_reasons: dict[str, str] = {}
    for vcp, trend_r, composite in vcp_scored:
        if len(orders_placed) >= max_new_pos:
            _log.info("[main] Max new positions reached (%d) — stopping.", RISK["max_positions"])
            break

        if vcp.symbol in held_symbols:
            _log.info("[main] %s already held — skipping.", vcp.symbol)
            _reject_reasons[vcp.symbol] = "already held"
            continue

        if vcp.symbol in orders_to_skip:
            _log.info("[main] %s — existing valid order retained.", vcp.symbol)
            _reject_reasons[vcp.symbol] = "order retained"
            continue

        # Sector concentration check (max_positions_per_sector from config)
        sec = get_sector(vcp.symbol)
        if sector_counts.get(sec, 0) >= max_per_sector:
            _log.info("[main] %s skipped — sector '%s' already at limit (%d/%d)",
                      vcp.symbol, sec, sector_counts.get(sec, 0), max_per_sector)
            _reject_reasons[vcp.symbol] = f"sector cap ({sec})"
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
            _reject_reasons[vcp.symbol] = f"super-sector cap ({_vcp_super})"
            continue

        # Sector risk heat cap: never allocate >3% total risk to one sector
        _sec_risk = sum(v for s, v in _rs_now.get("positions_risk", {}).items()
                        if get_sector(s) == sec)
        if _sec_risk >= 0.03:
            _log.info("[main] %s skipped — sector '%s' risk heat %.1f%% at 3%% cap",
                      vcp.symbol, sec, _sec_risk * 100)
            _reject_reasons[vcp.symbol] = f"sector heat ({sec} {_sec_risk*100:.1f}%)"
            continue

        # Sector RS hard filter: two-tier block
        # Tier 1 (hard): bottom-3 sectors AND momentum < -1% → always skip
        # Tier 2 (soft): bottom-half AND momentum < -0.5% → skip (unchanged)
        _sec_etf_v = _SECTOR_ETF_MAP.get(sec)
        if _sec_etf_v and sector_momentum:
            _all_rnk = sorted(sector_momentum.keys(), key=lambda e: -sector_momentum[e])
            _n_rnk   = len(_all_rnk)
            _sec_rnk = (_all_rnk.index(_sec_etf_v) + 1) if _sec_etf_v in _all_rnk else _n_rnk
            _sec_mom = sector_momentum.get(_sec_etf_v, 0)
            # Tier 1: hard skip — bottom-3 with clearly negative momentum
            if _sec_rnk > _n_rnk - 3 and _sec_mom < -0.01:
                _log.info("[main] %s skipped — sector '%s' rank %d/%d (bottom-3), RS %.1f%% — hard skip",
                          vcp.symbol, sec, _sec_rnk, _n_rnk, _sec_mom * 100)
                _reject_reasons[vcp.symbol] = f"sector bottom-3 negative ({sec} rank {_sec_rnk}/{_n_rnk})"
                continue
            # Tier 2: soft skip — bottom-half with any negative momentum
            if _sec_rnk > _n_rnk // 2 and _sec_mom < -0.005:
                _log.info("[main] %s skipped — sector '%s' rank %d/%d bottom-half, negative RS",
                          vcp.symbol, sec, _sec_rnk, _n_rnk)
                _reject_reasons[vcp.symbol] = f"sector RS weak ({sec} rank {_sec_rnk}/{_n_rnk})"
                continue

        if _is_correlated(vcp.symbol, held_symbols):
            _log.info("[main] %s skipped — high correlation with existing position", vcp.symbol)
            _reject_reasons[vcp.symbol] = "correlation with held"
            continue

        # Adaptive risk: composite score → VIX-adjusted → regime/loss multipliers
        base_risk  = RISK["risk_per_trade_pct"]
        _atr_f     = _atr_volatility_factor(vcp.symbol, vcp.breakout_level)
        _sym_beta_f = _beta_size_factor(vcp.symbol)
        if _atr_f < 1.0:
            _log.info("[main] %s ATR factor %.0f%% — elevated volatility", vcp.symbol, _atr_f * 100)
        # Gap-up breakout: open above prior day's high = institutional conviction → +10% size
        _gap_up_f = 1.0
        try:
            _df_gap = yf.Ticker(vcp.symbol).history(
                period="5d", interval="1d", auto_adjust=True)
            if len(_df_gap) >= 2:
                _gap_open   = float(_df_gap["Open"].iloc[-1])
                _gap_prev_h = float(_df_gap["High"].iloc[-2])
                if _gap_open > _gap_prev_h:
                    _gap_up_f = 1.10
                    _log.info("[main] %s gap-up breakout (open $%.2f > prior high $%.2f) — size +10%%",
                              vcp.symbol, _gap_open, _gap_prev_h)
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        risk_pct  = (_adaptive_risk_pct(composite, base_risk, _current_vix)
                     * regime_size_factor * loss_factor * win_factor
                     * _atr_f * _sym_beta_f * _extended_factor * _qqq_factor
                     * _cs_factor * _gap_up_f * _beta_f * _dxy_f)

        can, reason = check_can_trade(portfolio_value, risk_pct)
        if not can:
            _log.warning("[main] Cannot trade: %s", reason)
            if any(k in reason.lower() for k in ("drawdown", "halted", "heat")):
                _tg("🛡️ *Risk Gate — trading paused*\n" + f"`{reason}`")
            break

        try:
            sizing = position_size(portfolio_value, vcp.breakout_level, vcp.stop_loss,
                                   risk_pct, vcp.measured_move_pct, vcp.symbol)
        except ValueError as e:
            _log.warning("[main] %s sizing error: %s", vcp.symbol, e)
            continue

        if sizing["shares"] < 1:
            _log.info("[main] %s too expensive for risk budget — skip", vcp.symbol)
            continue

        if sizing["rr_ratio"] < 2.5:
            _log.info("[main] %s skipped — R:R %.1f:1 below minimum 2.5:1 "
                      "(stop too wide or measured move too small)",
                      vcp.symbol, sizing["rr_ratio"])
            _reject_reasons[vcp.symbol] = f"R:R {sizing['rr_ratio']:.1f}:1 < 2.5"
            continue

        if sizing["notional"] > cash * 0.95:
            _log.info("[main] %s notional $%.0f > cash $%.0f — skip",
                      vcp.symbol, sizing["notional"], cash)
            continue

        # Liquidity gate: position must not exceed 2% of 20-day avg dollar volume
        try:
            _liq = yf.Ticker(vcp.symbol).history(
                period="30d", interval="1d", auto_adjust=True)
            if len(_liq) >= 20:
                _adv = float((_liq["Close"] * _liq["Volume"]).tail(20).mean())
                if _adv > 0 and sizing["notional"] > _adv * 0.02:
                    _log.info("[main] %s skipped — notional $%.0f > 2%% avg dollar vol ($%.0f)",
                              vcp.symbol, sizing["notional"], _adv)
                    continue
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        if vcp.current_price >= vcp.breakout_level * 1.005:
            _log.info("[main] %s already above breakout ($%.2f >= $%.2f) — skip",
                      vcp.symbol, vcp.current_price, vcp.breakout_level)
            _reject_reasons[vcp.symbol] = "price extended past breakout"
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

        # OP1: Log active signals for this order to signal_accuracy.json
        try:
            import json as _jsa
            _sa_file = LOG_DIR / "signal_accuracy.json"
            _sa = _jsa.loads(_sa_file.read_text()) if _sa_file.exists() else {}
            _sig_names = [
                "rs_line_at_high", "rs_line_leading", "eps_accelerating", "rev_accelerating",
                "three_weeks_tight", "pocket_pivot", "insider_buying", "industry_leader",
                "eps_revision_up", "accum_weeks_strong", "analyst_pt_upside",
                "inst_ownership_increasing", "near_ath", "weekly_stage2",
                "pead_hold",
            ]
            _active = [s for s in _sig_names if getattr(trend_r, s, False)]
            for _sig in _active:
                if _sig not in _sa:
                    _sa[_sig] = {"orders": 0, "wins": 0, "losses": 0, "total_r": 0.0}
                _sa[_sig]["orders"] += 1
            _sa_file.write_text(_jsa.dumps(_sa, indent=2))
            # Store active signal names in order_rec so position_monitor can close the loop
            order_rec["active_signals"] = _active
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        vol_tag = " 🔥" if vcp.breakout_volume else ""
        _log.info(
            "[main] ✅ %s | %d sh | buy-stop=$%.2f | SL=$%.2f | TP=$%.2f | "
            "risk=$%.0f (%.1f%%) | candle=%s | sector=%s | RS=%.0f%s",
            vcp.symbol, sizing["shares"], vcp.breakout_level, vcp.stop_loss,
            sizing["target_price"], sizing["risk_amount"], sizing["risk_pct"] * 100,
            vcp.last_candle, sec, getattr(vcp, "rs_rating", 0), vol_tag,
        )

    report["orders_placed"]   = orders_placed
    report["reject_reasons"]  = _reject_reasons
    report["completed_at"]    = datetime.now(timezone.utc).isoformat()

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

    _noise = {"already held", "order retained", "price extended past breakout"}
    _rej   = {sym: rsn for sym, rsn in report.get("reject_reasons", {}).items()
              if rsn not in _noise}
    if _rej:
        lines.append("")
        lines.append("*Rejected candidates:*")
        for _rs, _rr in list(_rej.items())[:8]:
            lines.append(f"  ✗ {_rs} — {_rr}")
        if len(_rej) > 8:
            lines.append(f"  … and {len(_rej)-8} more")

    _blocked = report.get("vcp_found_no_orders", [])
    if _blocked and not orders and not _rej:
        _summary = report.get("summary", "")
        _gate = (_summary.replace("macro_blackout_", "Macro blackout: ")
                         .replace("bear_regime_no_orders", "Bear regime")
                         .replace("breadth_gate_", "Breadth gate: ").replace("_no_orders", "")
                         .replace("earnings_cluster_", "Earnings cluster: ").replace("_positions", " positions"))
        lines.append("")
        lines.append(f"*Blocked by {_gate}:*")
        for sym in _blocked[:10]:
            lines.append(f"  ⏸ {sym}")
        if len(_blocked) > 10:
            lines.append(f"  … and {len(_blocked)-10} more")

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


def _alpaca_connectivity_watchdog(stop_event: threading.Event) -> None:
    """
    Background thread: pings Alpaca /account every 5 min.
    Sends one Telegram alert on outage, another on recovery.
    Complements the startup health check with runtime connectivity monitoring.
    """
    import time as _tw
    _INTERVAL  = 300   # 5 minutes
    _ALERT_MIN = 900   # re-alert at most every 15 min during sustained outage
    _was_down  = False
    _last_alert: float = 0.0

    while not stop_event.is_set():
        # Skip watchdog pings on weekends — Alpaca paper API is still up but
        # there's nothing to monitor and no trades to protect
        import pytz as _pytz_wd
        _now_et_wd = datetime.now(_pytz_wd.timezone("America/New_York"))
        if _now_et_wd.weekday() >= 5:
            stop_event.wait(_INTERVAL)
            continue

        try:
            import broker as _bk_wd
            _bk_wd.get_account()   # lightweight ping - uses _retry internally
            if _was_down:
                _log.info("[watchdog] Alpaca connectivity RESTORED")
                _tg("✅ *Three Masters — Alpaca connectivity restored*\nBot is back online.")
                _was_down = False
        except Exception as _e_wd:
            _now_wd = _tw.time()
            if not _was_down or (_now_wd - _last_alert) > _ALERT_MIN:
                _log.error("[watchdog] Alpaca UNREACHABLE: %s", _e_wd)
                _tg(f"🚨 *Three Masters — Alpaca UNREACHABLE*\n"
                    f"`{_e_wd}`\nChecking every 5 min until resolved.")
                _last_alert = _now_wd
                _was_down   = True

        stop_event.wait(_INTERVAL)

    _log.info("[watchdog] Alpaca connectivity watchdog stopped")


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

    try:
        _t_wd = threading.Thread(
            target=_alpaca_connectivity_watchdog,
            args=(_monitor_stop,),
            daemon=True,
            name="alpaca-watchdog",
        )
        _t_wd.start()
        _log.info("[main] Alpaca connectivity watchdog started (ping every 5 min)")
    except Exception as e:
        _log.warning("[main] Alpaca watchdog failed to start: %s", e)


# ── Entry point ───────────────────────────────────────────────────────────────
def _startup_healthcheck() -> bool:
    """
    Verify all critical integrations before starting threads.
    Returns True if everything is OK, False if any critical check failed.
    Logs clearly and sends Telegram alert on failure so silent bugs are caught early.
    """
    failures: list[str] = []
    warnings_hc: list[str] = []

    # 1. Required environment variables
    import os as _os_hc
    required_env = {
        "THREE_MASTERS_ALPACA_API_KEY":    "Alpaca API key",
        "THREE_MASTERS_ALPACA_SECRET_KEY": "Alpaca secret key",
        "THREE_MASTERS_ALPACA_URL":        "Alpaca base URL",
        "TELEGRAM_BOT_TOKEN":              "Telegram bot token",
        "TELEGRAM_CHAT_ID":                "Telegram chat ID",
        "ANTHROPIC_API_KEY":               "Claude/Anthropic API key",
    }
    for var, desc in required_env.items():
        if not _os_hc.environ.get(var):
            failures.append(f"Missing env var {var} ({desc})")

    # 2. Alpaca API — account reachable + paper trading confirmed
    try:
        from broker import get_account
        acct = get_account()
        equity = acct.get("equity", 0)
        status = acct.get("status", "?")
        _log.info("[startup] ✓ Alpaca OK — equity=$%.2f  status=%s", equity, status)
        if status != "ACTIVE":
            warnings_hc.append(f"Alpaca account status is {status!r} (expected ACTIVE)")
    except Exception as _e_alp:
        failures.append(f"Alpaca unreachable: {_e_alp}")

    # 3. Position monitor URL sanity — verify /v2/ is in the resolved base URL
    try:
        from position_monitor import _alpaca_base
        _pm_url = _alpaca_base()
        if "/v2" not in _pm_url:
            failures.append(f"position_monitor._alpaca_base() missing /v2: {_pm_url!r}")
        else:
            _log.info("[startup] ✓ Monitor URL OK — %s", _pm_url)
    except Exception as _e_pm:
        failures.append(f"position_monitor._alpaca_base() error: {_e_pm}")

    # 4. Telegram connectivity
    try:
        import requests as _req_hc
        _tok = _os_hc.environ.get("TELEGRAM_BOT_TOKEN", "")
        _cid = _os_hc.environ.get("TELEGRAM_CHAT_ID", "")
        if _tok and _cid:
            _r = _req_hc.get(
                f"https://api.telegram.org/bot{_tok}/getMe", timeout=8)
            if _r.ok:
                _log.info("[startup] ✓ Telegram OK — bot=%s",
                          _r.json().get("result", {}).get("username", "?"))
            else:
                warnings_hc.append(f"Telegram getMe failed: {_r.status_code}")
    except Exception as _e_tg:
        warnings_hc.append(f"Telegram unreachable: {_e_tg}")

    # 5. Anthropic / Claude API key present and non-empty
    _claude_key = _os_hc.environ.get("ANTHROPIC_API_KEY", "")
    if _claude_key and len(_claude_key) > 10:
        _log.info("[startup] ✓ Anthropic API key present")
    else:
        failures.append("ANTHROPIC_API_KEY missing or too short — VCP analysis will fail")

    # 6. Log directory writable
    try:
        _test_path = LOG_DIR / "_healthcheck.tmp"
        _test_path.write_text("ok")
        _test_path.unlink()
        _log.info("[startup] ✓ Log directory writable — %s", LOG_DIR)
    except Exception as _e_log:
        failures.append(f"Log directory not writable: {_e_log}")

    # ── Report results ───────────────────────────────────────────────────────
    if warnings_hc:
        for w in warnings_hc:
            _log.warning("[startup] ⚠ %s", w)

    if failures:
        msg = "\n".join(f"  • {f}" for f in failures)
        _log.error("[startup] ❌ HEALTH CHECK FAILED — %d critical issue(s):\n%s",
                   len(failures), msg)
        try:
            _tg(f"🚨 *Three Masters — Startup FAILED*\n"
                f"{len(failures)} critical issue(s):\n"
                + "\n".join(f"• {f}" for f in failures))
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        return False

    _log.info("[startup] ✅ All health checks passed — bot ready")
    _tg(f"✅ *Three Masters — Startup OK*\n"
        f"Alpaca ${ equity:,.0f} | Monitor URL /v2 ✓ | Telegram ✓ | Claude ✓")
    return True


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

    # Write heartbeat IMMEDIATELY so watchdog knows process is alive during startup
    # (health check can take minutes; without this, watchdog fires false alerts)
    _heartbeat()

    if "--run-now" in sys.argv:
        _log.info("[main] --run-now flag — executing immediately")
        run_daily()
        return

    if not _startup_healthcheck():
        _log.critical("[main] Startup health check failed — aborting. Fix issues and restart.")
        sys.exit(1)

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
