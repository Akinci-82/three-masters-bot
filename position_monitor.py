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
from datetime import datetime, time as dt_time, timedelta

import pytz
import requests
import yfinance as yf

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
    return ALPACA_BASE_URL.rstrip("/") + "/v2"


def _market_is_open() -> bool:
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        _log.warning("[monitor] state save error: %s", e)


# ── Alpaca REST calls ─────────────────────────────────────────────────────────

class AlpacaConnectionError(RuntimeError):
    """Raised when Alpaca REST API is persistently unreachable after retries."""


def _pm_retry(fn, *args, retries: int = 3, backoff: float = 2.0, **kwargs):
    """Call fn(*args, **kwargs) up to `retries` times with exponential backoff."""
    import time as _t
    delay = 1.0
    for _attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as _exc:
            if _attempt == retries - 1:
                raise
            _log.warning("[monitor] %s attempt %d/%d failed: %s - retry in %.0fs",
                         getattr(fn, "__name__", "call"), _attempt + 1, retries, _exc, delay)
            _t.sleep(delay)
            delay *= backoff


def _get_positions() -> list[dict]:
    """Fetch current positions. Raises AlpacaConnectionError on persistent failure."""
    def _fetch():
        r = requests.get(
            f"{_alpaca_base()}/positions",
            headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        return r.json()
    try:
        return _pm_retry(_fetch)
    except Exception as e:
        _log.error("[monitor] get_positions FAILED after retries: %s", e)
        raise AlpacaConnectionError(f"get_positions failed: {e}") from e


def _get_open_orders(symbol: str) -> list[dict]:
    def _fetch():
        r = requests.get(
            f"{_alpaca_base()}/orders",
            params={"status": "open", "symbols": symbol, "limit": 20},
            headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        return r.json()
    try:
        return _pm_retry(_fetch)
    except Exception as e:
        _log.error("[monitor] get_orders(%s) FAILED after retries: %s", symbol, e)
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
        _log.error("[monitor] market_sell(%s, %d) FAILED (exit NOT submitted): %s", symbol, qty, e)
        return False


def _place_market_buy(symbol: str, qty: int) -> bool:
    """Market buy for pyramiding into confirmed winners during market hours."""
    try:
        body = {
            "symbol": symbol,
            "qty":    str(qty),
            "side":   "buy",
            "type":   "market",
            "time_in_force": "day",
        }
        r = requests.post(
            f"{_alpaca_base()}/orders",
            json=body, headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        _log.info("[monitor] Market buy %d × %s submitted (pyramid)", qty, symbol)
        return True
    except Exception as e:
        _log.error("[monitor] market_buy(%s, %d) FAILED: %s", symbol, qty, e)
        return False


def _place_limit_sell(symbol: str, qty: int, limit_price: float) -> bool:
    """Limit sell for partial exits — captures slightly better fills than market."""
    try:
        body = {
            "symbol": symbol,
            "qty":    str(qty),
            "side":   "sell",
            "type":   "limit",
            "time_in_force": "day",
            "limit_price":   str(round(limit_price, 2)),
        }
        r = requests.post(
            f"{_alpaca_base()}/orders",
            json=body, headers=_alpaca_headers(), timeout=10
        )
        r.raise_for_status()
        _log.info("[monitor] Limit sell %d × %s @ $%.2f submitted", qty, symbol, limit_price)
        return True
    except Exception as e:
        _log.warning("[monitor] limit_sell(%s, %d) error: %s — falling back to market", symbol, qty, e)
        return _place_market_sell(symbol, qty)


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
        _log.error("[monitor] place_stop(%s) FAILED (stop NOT placed - will retry next cycle): %s", symbol, e)
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
        _log.error("[monitor] trailing_stop(%s) FAILED (stop NOT placed - will retry next cycle): %s", symbol, e)
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
    # R-multiple uses actual VCP stop_loss from order report (falls back to 7% approx)
    stop_loss      = sym_data.get("stop_loss", 0.0)
    risk_per_share = (avg_cost - stop_loss) if stop_loss > 0 else avg_cost * 0.07
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
        "composite_score": round(float(sym_data.get("composite_score", 0.0) or 0.0), 2),
        "mae_pct":         round(float(sym_data.get("mae_pct", 0.0) or 0.0) * 100, 2),
        "mfe_pct":         round(float(sym_data.get("mfe_pct", 0.0) or 0.0) * 100, 2),
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


from config import SECTOR_ETF_MAP as _SECTOR_ETF_PM
_sector_etf_cache: dict[str, tuple[bool, float]] = {}


def _sector_etf_below_ma50(symbol: str) -> bool:
    """True if the symbol's sector ETF is below its 50-day MA (sector headwind).
    Cached per ETF with 1-hour TTL to avoid excessive yfinance calls.
    """
    import time as _time_s
    try:
        from screener import get_sector as _gs
        etf = _SECTOR_ETF_PM.get(_gs(symbol))
        if not etf:
            return False
        _now = _time_s.time()
        if etf in _sector_etf_cache and _now - _sector_etf_cache[etf][1] < 3600:
            return _sector_etf_cache[etf][0]
        _col = yf.Ticker(etf).history(
            period="60d", interval="1d", auto_adjust=True)["Close"]
        _result = len(_col) >= 51 and float(_col.iloc[-1]) < float(_col.tail(50).mean())
        _sector_etf_cache[etf] = (_result, _now)
        return _result
    except Exception:
        return False


# ── Regime cache (1-hour TTL) — for adaptive time stop ───────────────────────
_cached_regime_pm: str   = "neutral"
_regime_ts_pm:    float  = 0.0


def _get_cached_regime() -> str:
    """Return SPY regime (bull/neutral/bear) with a 1-hour cache to avoid per-position fetches."""
    import time as _time_r
    global _cached_regime_pm, _regime_ts_pm
    if _time_r.time() - _regime_ts_pm < 3600:
        return _cached_regime_pm
    try:
        _spy = yf.Ticker("SPY").history(
            period="200d", interval="1d", auto_adjust=True)["Close"]
        if len(_spy) >= 50:
            _ma200 = float(_spy.tail(200).mean())
            _pct   = (float(_spy.iloc[-1]) - _ma200) / _ma200
            _cached_regime_pm = ("bull" if _pct > 0.02
                                  else ("bear" if _pct < -0.02 else "neutral"))
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    _regime_ts_pm = _time_r.time()
    return _cached_regime_pm


def _lookup_position_metadata(symbol: str) -> dict:
    """
    Look up VCP stop_loss, quality_score, composite_score from recent daily reports.
    Searches up to 10 calendar days back to find the order that opened this position.
    """
    from pathlib import Path
    from datetime import date, timedelta
    import json as _json
    report_dir = Path(__file__).parent / "reports"
    for days_ago in range(10):
        d = date.today() - timedelta(days=days_ago)
        rfile = report_dir / f"{d}.json"
        if not rfile.exists():
            continue
        try:
            data = _json.loads(rfile.read_text())
            for order in data.get("orders_placed", []):
                if order.get("symbol") == symbol:
                    return {
                        "stop_loss":         float(order.get("stop_loss", 0) or 0),
                        "quality_score":     int(order.get("quality_score", 0) or 0),
                        "composite_score":   float(order.get("composite_score", 0) or 0),
                        "measured_move_pct": float(order.get("measured_move_pct", 0) or 0),
                        "buy_stop":          float(order.get("buy_stop", 0) or 0),
                        "active_signals":    list(order.get("active_signals", [])),
                    }
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    return {"stop_loss": 0.0, "quality_score": 0, "composite_score": 0.0,
            "measured_move_pct": 0.0, "buy_stop": 0.0, "active_signals": []}


# Module-level: track which dates we've already sent each drawdown alert level
_drawdown_alerted: dict[str, set] = {}  # {date_str: {"2pct", "4pct"}}


def _check_drawdown_proximity() -> None:
    """
    Send Telegram warning at -2% daily P&L (halfway to -4% hard halt).
    Fires at most once per level per trading day.
    """
    try:
        from datetime import date as _date
        from risk_manager import get_state as _grs
        from config import RISK as _risk_cfg
        import requests as _req, os as _os

        state       = _grs()
        daily_pnl   = state.get("daily_pnl_pct", 0.0)
        today_str   = str(_date.today())
        alerted_set = _drawdown_alerted.setdefault(today_str, set())

        halt_pct = _risk_cfg.get("max_daily_loss_pct", 0.04)
        warn_pct = halt_pct / 2

        _tok = _os.getenv("TELEGRAM_BOT_TOKEN", "")
        _cid = _os.getenv("TELEGRAM_CHAT_ID", "")

        if daily_pnl <= -halt_pct and "4pct" not in alerted_set:
            alerted_set.add("4pct")
            if _tok and _cid:
                pct_str = f"{daily_pnl*100:.1f}%"
                halt_str = f"{halt_pct*100:.0f}%"
                msg = (
                    "\U0001F6A8 *DAILY HALT " + pct_str + "*\n"
                    "Daily loss limit reached -- risk_manager blocks new trades.\n"
                    "Portfolio at max drawdown (" + halt_str + ") for today."
                )
                _req.post(
                    f"https://api.telegram.org/bot{_tok}/sendMessage",
                    json={"chat_id": _cid, "parse_mode": "Markdown", "text": msg},
                    timeout=8,
                )

        elif daily_pnl <= -warn_pct and "2pct" not in alerted_set:
            alerted_set.add("2pct")
            if _tok and _cid:
                pct_str  = f"{abs(daily_pnl)*100:.1f}%"
                halt_str = f"{halt_pct*100:.0f}%"
                msg = (
                    "\u26A0\uFE0F *Drawdown Warning -" + pct_str + "*\n"
                    "Portfolio down " + pct_str + " today -- "
                    "halfway to " + halt_str + " daily halt.\n"
                    "Review open positions and tighten stops."
                )
                _req.post(
                    f"https://api.telegram.org/bot{_tok}/sendMessage",
                    json={"chat_id": _cid, "parse_mode": "Markdown", "text": msg},
                    timeout=8,
                )
    except Exception as _e_dd:
        import logging
        logging.getLogger(__name__).debug("[monitor] drawdown check error: %s", _e_dd)


def check_positions() -> None:
    """Run one monitoring cycle. Called every 15 min during market hours."""
    if not _market_is_open():
        return

    _check_drawdown_proximity()

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
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        return   # skip entire monitoring cycle — do NOT touch orders

    try:
        positions = _get_positions()
    except AlpacaConnectionError as e:
        _log.error("[monitor] POSITIONS UNAVAILABLE - skipping entire cycle: %s", e)
        try:
            import os as _os_cp
            _tok_cp = _os_cp.getenv("TELEGRAM_BOT_TOKEN", "")
            _cid_cp = _os_cp.getenv("TELEGRAM_CHAT_ID", "")
            if _tok_cp and _cid_cp:
                requests.post(
                    f"https://api.telegram.org/bot{_tok_cp}/sendMessage",
                    json={"chat_id": _cid_cp, "parse_mode": "Markdown",
                          "text": ("🚨 *Three Masters — Monitor: can't fetch positions*\n"
                                   f"`{e}`\nCycle skipped — positions NOT managed this tick.")},
                    timeout=8,
                )
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        return
    if not positions:
        return

    from config import MONITOR as cfg, RISK as _risk_cfg
    trail_pct         = cfg.get("trailing_stop_pct", 0.07)
    breakeven_trigger = cfg.get("breakeven_trigger", 0.08)
    partial_pct       = cfg.get("partial_exit_pct", 0.50)
    # composite-adjusted thresholds are set per-position inside the loop

    # Soft-drawdown defensive mode: when portfolio approaching daily loss limit,
    # tighten trailing stops and take profits earlier across ALL open positions.
    _soft_dd_mode = False
    try:
        from risk_manager import get_state as _grs_sdd
        _rs_sdd      = _grs_sdd()
        _daily_pnl   = _rs_sdd.get("daily_pnl_pct", 0.0)
        _warn_thresh = -(_risk_cfg.get("max_daily_loss_pct", 0.04) / 2)  # -2%
        if _daily_pnl <= _warn_thresh:
            _soft_dd_mode = True
            trail_pct         = 0.05   # tighten from 7% → 5%
            breakeven_trigger = 0.05   # breakeven sooner (5% vs 8%)
            _log.info("[monitor] SOFT-DD MODE active (day_pnl=%.1f%%) — trail=5%% breakeven=5%%",
                      _daily_pnl * 100)
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
            "active_signals":        [],
            "entry_date":            datetime.now(_ET).strftime("%Y-%m-%d"),
        })
        sym["last_price"] = cur_price   # keep last known price for close_trade P&L

        # Stock split detection: if Alpaca qty diverges significantly from our recorded qty,
        # a corporate action (split / reverse-split) likely occurred. Alert and rescale stop.
        _recorded_qty = sym.get("initial_qty", 0)
        if _recorded_qty > 0 and not sym.get("split_detected"):
            _pyramid_qty = sym.get("pyramid_qty", 0)
            _expected_qty = _recorded_qty + _pyramid_qty
            if _expected_qty > 0:
                _qty_ratio = qty / _expected_qty
                if _qty_ratio >= 1.8 or _qty_ratio <= 0.6:
                    _split_factor = round(_qty_ratio)
                    sym["split_detected"] = True
                    sym["split_factor"]   = _qty_ratio
                    _old_sl = sym.get("stop_loss", 0)
                    if _old_sl > 0 and _split_factor > 0:
                        sym["stop_loss"]         = round(_old_sl / _qty_ratio, 4)
                        sym["stop_loss_initial"] = round(
                            sym.get("stop_loss_initial", _old_sl) / _qty_ratio, 4)
                    _log.warning(
                        "[monitor] %s SPLIT DETECTED — qty %d→%d (ratio=%.2f) "
                        "SL adjusted $%.2f→$%.2f",
                        symbol, _expected_qty, qty, _qty_ratio,
                        _old_sl, sym.get("stop_loss", 0),
                    )
                    try:
                        import requests as _rq_sp, os as _os_sp
                        _tok_sp = _os_sp.getenv("TELEGRAM_BOT_TOKEN", "")
                        _cid_sp = _os_sp.getenv("TELEGRAM_CHAT_ID", "")
                        if _tok_sp and _cid_sp:
                            _rq_sp.post(
                                f"https://api.telegram.org/bot{_tok_sp}/sendMessage",
                                json={"chat_id": _cid_sp, "parse_mode": "Markdown",
                                      "text": (
                                          f"⚠️ *Stock Split Detected — {symbol}*\n"
                                          f"Qty {_expected_qty} → {qty} (ratio {_qty_ratio:.2f}x)\n"
                                          f"Stop adjusted: ${_old_sl:.2f} → ${sym.get('stop_loss', 0):.2f}\n"
                                          "Please verify position parameters."
                                      )},
                                timeout=8,
                            )
                    except Exception:
                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # On first encounter: check intraday breakout volume confirmation.
        # Minervini rule: breakout on < 1.0× avg volume = false breakout, exit fast.
        if not sym.get("_vol_checked"):
            sym["_vol_checked"] = True
            try:
                _dfv = yf.Ticker(symbol).history(period="30d", interval="1d", auto_adjust=True)
                if len(_dfv) >= 20:
                    _avg20_vol = float(_dfv["Volume"].tail(20).mean())
                    _today_vol = float(_dfv["Volume"].iloc[-1])
                    _vol_ratio = _today_vol / _avg20_vol if _avg20_vol > 0 else 1.0
                    sym["breakout_vol_ratio"] = round(_vol_ratio, 2)
                    if _vol_ratio < 1.0:
                        sym["weak_vol_breakout"] = True
                        _log.warning("[monitor] %s WEAK BREAKOUT VOL %.1f×avg — "
                                     "stop will tighten to -3%% on any weakness",
                                     symbol, _vol_ratio)
                        try:
                            import requests as _rqv, os as _osv
                            _tokv = _osv.getenv("TELEGRAM_BOT_TOKEN", "")
                            _cidv = _osv.getenv("TELEGRAM_CHAT_ID", "")
                            if _tokv and _cidv:
                                _rqv.post(
                                    f"https://api.telegram.org/bot{_tokv}/sendMessage",
                                    json={"chat_id": _cidv, "parse_mode": "Markdown",
                                          "text": (
                                              "\u26a0\ufe0f *Weak Vol Breakout \u2014 " + symbol + "*\n"
                                              + f"Volume {_vol_ratio:.1f}x avg (< 1.0x)\n"
                                              + "Stop tightened to -3% on first weakness"
                                          )},
                                    timeout=8)
                        except Exception:
                            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
            except Exception as _ve:
                _log.debug("[monitor] vol check %s: %s", symbol, _ve)

        # Load (or reload) VCP metadata once per trading day so the monitor always
        # uses the latest daily-report values for stop, quality, and composite score.
        _today_str = datetime.now(_ET).strftime("%Y-%m-%d")
        if not sym.get("_meta_loaded") or sym.get("_meta_date") != _today_str:
            meta = _lookup_position_metadata(symbol)
            sym["_meta_loaded"]       = True
            sym["_meta_date"]         = _today_str
            sym["stop_loss"]          = meta["stop_loss"]
            sym["stop_loss_initial"]  = meta["stop_loss"]  # preserved for R-multiple calc
            sym["quality_score"]      = meta["quality_score"]
            sym["composite_score"]    = meta["composite_score"]
            sym["measured_move_pct"]  = meta["measured_move_pct"]
            sym["buy_stop"]           = meta["buy_stop"]
            sym["active_signals"]     = meta.get("active_signals", [])

            # ATR-dynamic trailing stop: 2×ATR(14) as trail %, clamped 4-12%.
            # Low-vol stocks get tighter stops; high-vol stocks get room to breathe.
            try:
                _df_atr_m = yf.Ticker(symbol).history(
                    period="30d", interval="1d", auto_adjust=True)
                if len(_df_atr_m) >= 15:
                    _hi_m = _df_atr_m["High"].values
                    _lo_m = _df_atr_m["Low"].values
                    _cl_m = _df_atr_m["Close"].values
                    _tr_m = [max(_hi_m[i] - _lo_m[i],
                                 abs(_hi_m[i] - _cl_m[i - 1]),
                                 abs(_lo_m[i] - _cl_m[i - 1]))
                             for i in range(1, len(_cl_m))]
                    _atr14 = sum(_tr_m[-14:]) / 14
                    _raw_trail = (_atr14 * 2) / avg_cost if avg_cost > 0 else trail_pct
                    sym["atr_trail_pct"] = round(max(0.04, min(0.12, _raw_trail)), 4)
                    _log.info("[monitor] %s ATR trail: 2×ATR=%.1f%%",
                              symbol, sym["atr_trail_pct"] * 100)
                else:
                    sym["atr_trail_pct"] = trail_pct
            except Exception:
                sym["atr_trail_pct"] = trail_pct
            if meta["stop_loss"] > 0:
                _log.info("[monitor] %s meta: SL=$%.2f Q%d composite=%.1f",
                          symbol, meta["stop_loss"], meta["quality_score"],
                          meta["composite_score"])
            # Fill-slippage guard: verify actual fill vs planned buy-stop
            _planned = meta.get("buy_stop", 0.0)
            if _planned > 0 and avg_cost > _planned:
                _slip = (avg_cost - _planned) / _planned
                if _slip > 0.02:
                    _log.warning("[monitor] %s SLIPPAGE GUARD >2%% (%.1f%%) — closing "
                                 "(fill=$%.2f planned=$%.2f)",
                                 symbol, _slip * 100, avg_cost, _planned)
                    _place_market_sell(symbol, qty)
                elif _slip > 0.01:
                    _log.warning("[monitor] %s slippage >1%% (%.1f%%) — "
                                 "fill=$%.2f planned=$%.2f",
                                 symbol, _slip * 100, avg_cost, _planned)
            # Breakout volume validation: if fill-day volume < 1.5x 60-day avg, tighten stop
            # Low-volume breakouts have 3x higher failure rate — Minervini hard rule
            try:
                _bv_df = yf.Ticker(symbol).history(
                    period="90d", interval="1d", auto_adjust=True)
                if len(_bv_df) >= 61:
                    _bv_avg   = float(_bv_df["Volume"].iloc[-61:-1].mean())
                    _bv_today = float(_bv_df["Volume"].iloc[-1])
                    if _bv_avg > 0 and _bv_today < _bv_avg * 1.5:
                        sym["weak_breakout_vol"] = True
                        if sym.get("stop_loss", 0) > 0:
                            _new_sl = round(avg_cost * 0.95, 2)
                            if _new_sl > sym["stop_loss"]:
                                _log.info("[monitor] %s weak breakout vol (%.0f%% of avg) — "
                                          "tightening stop $%.2f -> $%.2f",
                                          symbol, _bv_today / _bv_avg * 100,
                                          sym["stop_loss"], _new_sl)
                                sym["stop_loss"] = _new_sl
            except Exception:
                _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
            changed = True

        _log.debug("[monitor] %s  qty=%d  avg=$%.2f  cur=$%.2f  pnl=%.1f%%",
                   symbol, qty, avg_cost, cur_price, pnl_pct * 100)

        # ── PM11: Stop order re-validation ───────────────────────────────────────
        # GTC stops can be silently cancelled (corporate actions, margin events, API failures).
        # Once per day: verify expected stop is still active; re-place if missing.
        _sv_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if (sym.get("trailing_stop_placed")
                and sym.get("_sv_date") != _sv_today
                and not sym.get("time_stopped")
                and not sym.get("max_loss_exited")):
            sym["_sv_date"] = _sv_today
            try:
                _open_ords  = _get_open_orders(symbol)
                _stop_ords  = [o for o in _open_ords
                               if o.get("type") in ("stop", "trailing_stop", "stop_limit")
                               and o.get("side") == "sell"]
                _stop_ids   = {o["id"] for o in _stop_ords}
                _tracked_id = sym.get("stop_order_id", "")

                if _tracked_id in _stop_ids:
                    pass  # stop still active and tracked — nothing to do

                elif _stop_ids:
                    # A stop exists but under a different ID (e.g. trailing stop replaced hard stop).
                    # Update tracking instead of alarming — this is NOT a missing stop.
                    _new_id = max(_stop_ords, key=lambda o: o.get("submitted_at",""))["id"]
                    _log.info("[monitor] %s stop ID changed %s → %s (stop active — updating tracking)",
                              symbol, _tracked_id, _new_id)
                    sym["stop_order_id"] = _new_id
                    changed = True

                else:
                    # No stop orders found at all — genuine miss, re-place.
                    # Guard against stop_loss=0.0 in state (falls back to 7% under avg cost).
                    _sv_price = sym.get("stop_loss") or round(avg_cost * 0.93, 2)
                    _sv_rem   = qty - sym.get("partial_qty", 0)
                    _log.warning("[monitor] %s STOP MISSING (was %s) — re-placing trailing stop %.0f%% trail",
                                 symbol, _tracked_id, 7.0)
                    _sv_oid = _place_trailing_stop(symbol, _sv_rem, 0.07) if _sv_rem > 0 else None
                    if _sv_oid:
                        sym["stop_order_id"] = _sv_oid
                        changed = True
                    try:
                        import requests as _rqsv, os as _ossv
                        _tsv = _ossv.getenv("TELEGRAM_BOT_TOKEN", "")
                        _csv2 = _ossv.getenv("TELEGRAM_CHAT_ID", "")
                        if _tsv and _csv2:
                            _status = "Re-placed 7% trailing stop" if _sv_oid else "Re-place FAILED — check manually"
                            _rqsv.post(
                                f"https://api.telegram.org/bot{_tsv}/sendMessage",
                                json={"chat_id": _csv2, "parse_mode": "Markdown",
                                      "text": (
                                          f"\u26a0\ufe0f *Stop Missing — {symbol}*\n"
                                          f"GTC stop not found on Alpaca\n"
                                          f"{_status}"
                                      )},
                                timeout=8)
                    except Exception:
                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
            except Exception as _sve:
                _log.debug("[monitor] stop_revalidation %s: %s", symbol, _sve)

        # MAE/MFE: track worst (most adverse) and best (most favorable) excursion per position
        sym["mae_pct"] = min(sym.get("mae_pct", pnl_pct), pnl_pct)
        sym["mfe_pct"] = max(sym.get("mfe_pct", pnl_pct), pnl_pct)

        # ── PM8: Max loss per trade cap — hard 2R floor (Tudor Jones) ──────────────
        # GTC stops can be gapped through on bad news. This is the last-resort circuit breaker:
        # if loss exceeds 2× initial risk per share, close immediately regardless of stop status.
        _sli_ml = sym.get("stop_loss_initial", 0.0)
        if (_sli_ml > 0
                and _sli_ml < avg_cost
                and not sym.get("max_loss_exited")
                and not sym.get("time_stopped")):
            _rps_ml     = avg_cost - _sli_ml            # risk per share (1R)
            _max_loss_p = avg_cost - 2.0 * _rps_ml      # 2R loss floor (price level)
            if cur_price < _max_loss_p:
                _ml_rem = qty - sym.get("partial_qty", 0)
                _log.warning(
                    "[monitor] %s MAX LOSS CAP: cur $%.2f < 2R floor $%.2f "
                    "(entry $%.2f, stop $%.2f, pnl=%.1f%%) — forced close",
                    symbol, cur_price, _max_loss_p, avg_cost, _sli_ml, pnl_pct * 100)
                _cancel_stop_orders(symbol)
                if _ml_rem > 0 and _place_market_sell(symbol, _ml_rem):
                    sym["max_loss_exited"] = True
                    changed = True
                    try:
                        import requests as _rqml, os as _osml
                        _tml = _osml.getenv("TELEGRAM_BOT_TOKEN", "")
                        _cml = _osml.getenv("TELEGRAM_CHAT_ID", "")
                        if _tml and _cml:
                            _rqml.post(
                                f"https://api.telegram.org/bot{_tml}/sendMessage",
                                json={"chat_id": _cml, "parse_mode": "Markdown",
                                      "text": (
                                          f"🔴 *Max Loss Cap — {symbol}*\n"
                                          f"Price ${cur_price:.2f} breached 2R floor ${_max_loss_p:.2f}\n"
                                          f"Entry ${avg_cost:.2f} | Stop ${_sli_ml:.2f} | "
                                          f"Loss {pnl_pct*100:.1f}%\n"
                                          f"Tudor Jones hard floor — forced exit"
                                      )},
                                timeout=8)
                    except Exception:
                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # Quality-adjusted exits: elite setups get more room to run
        composite      = sym.get("composite_score", 0.0)
        partial_trigger = 0.20 if composite >= 8.0 else cfg.get("partial_exit_trigger", 0.15)
        _regime_ts      = _get_cached_regime()
        _base_ts        = 25 if _regime_ts == "bull" else (10 if _regime_ts == "neutral" else 15)
        time_stop_days  = max(_base_ts, 20) if composite >= 8.0 else _base_ts

        # ── Step G: Gap-up harvest — sell 50% on overnight gap ≥12% ─────────────
        # Large gaps often fill; taking half off protects gains from mean-reversion
        _gap_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if (not sym.get("gap_harvest_done")
                and not sym.get("partial1_done")
                and sym.get("_gap_check_date") != _gap_today
                and pnl_pct > 0):
            sym["_gap_check_date"] = _gap_today
            try:
                _dfg = yf.Ticker(symbol).history(
                    period="5d", interval="1d", auto_adjust=True)
                if len(_dfg) >= 2:
                    _prev_close = float(_dfg["Close"].iloc[-2])
                    _gap_pct    = (cur_price - _prev_close) / _prev_close if _prev_close > 0 else 0.0
                    if _gap_pct >= 0.12:
                        _gap_qty = max(1, round(qty * 0.50))
                        _log.warning("[monitor] %s GAP-UP HARVEST +%.1f%% overnight — selling %d sh (50%%)",
                                     symbol, _gap_pct * 100, _gap_qty)
                        if _place_market_sell(symbol, _gap_qty):
                            sym["gap_harvest_done"] = True
                            sym["partial1_done"]    = True
                            sym["partial_qty"]      = _gap_qty
                            changed = True
            except Exception as _ge:
                _log.debug("[monitor] gap harvest %s: %s", symbol, _ge)

        # ── Earnings protection — tighten or close before earnings report ────────
        _earn_check_date = sym.get("_earnings_checked_date", "")
        _today_str = datetime.now(_ET).strftime("%Y-%m-%d")
        if _earn_check_date != _today_str:
            sym["_earnings_checked_date"] = _today_str
            try:
                from screener import _days_to_earnings
                _days_earn = _days_to_earnings(symbol)
                if _days_earn is not None:
                    sym["days_to_earnings"] = _days_earn
                    if _days_earn <= 5:
                        _remaining = qty - sym.get("partial_qty", 0) - sym.get("partial2_qty", 0)
                        if pnl_pct >= 0.03 and not sym.get("breakeven_done"):
                            # Profitable → protect with breakeven stop
                            _cancel_stop_orders(symbol)
                            _be_oid = _place_stop(symbol, _remaining, round(avg_cost, 2))
                            if _be_oid:
                                sym["breakeven_done"] = True
                                sym["stop_order_id"] = _be_oid
                                changed = True
                                _log.warning("[monitor] EARNINGS GUARD %s — %dd to report, "
                                             "stop moved to breakeven $%.2f",
                                             symbol, _days_earn, avg_cost)
                                try:
                                    import requests as _rq, os as _os
                                    tok = _os.getenv("TELEGRAM_BOT_TOKEN", "")
                                    cid = _os.getenv("TELEGRAM_CHAT_ID", "")
                                    if tok and cid:
                                        _rq.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                                 json={"chat_id": cid, "parse_mode": "Markdown",
                                                       "text": (f"🛡️ *Earnings Guard — {symbol}*\n"
                                                                f"{_days_earn} days to earnings report\n"
                                                                f"Stop moved to breakeven ${avg_cost:.2f}")},
                                                 timeout=8)
                                except Exception:
                                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                        elif pnl_pct < 0.01 and _days_earn <= 3 and _remaining > 0:
                            # Flat/losing with report in 3 days → exit now
                            _cancel_stop_orders(symbol)
                            if _place_market_sell(symbol, _remaining):
                                sym["earnings_closed"] = True
                                changed = True
                                _log.warning("[monitor] EARNINGS CLOSE %s — %dd to report, "
                                             "flat/loss %.1f%%", symbol, _days_earn, pnl_pct*100)
                                try:
                                    import requests as _rq, os as _os
                                    tok = _os.getenv("TELEGRAM_BOT_TOKEN", "")
                                    cid = _os.getenv("TELEGRAM_CHAT_ID", "")
                                    if tok and cid:
                                        _rq.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                                 json={"chat_id": cid, "parse_mode": "Markdown",
                                                       "text": (f"📅 *Earnings Close — {symbol}*\n"
                                                                f"{_days_earn} days to report, gain {pnl_pct*100:+.1f}%\n"
                                                                f"Exiting before earnings risk")},
                                                 timeout=8)
                                except Exception:
                                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
            except Exception as _e:
                _log.debug("[monitor] earnings check %s: %s", symbol, _e)

        # ── PM9: IV Crush Protection — exit runner if ATM implied vol > 50% near earnings ──
        # Options market pricing a large move means IV collapses after the report
        # regardless of whether EPS beats — this erodes the runner's value even on good news.
        _dte_iv = sym.get("days_to_earnings", 99)
        if (not sym.get("iv_crush_exited")
                and 2 <= _dte_iv <= 7
                and pnl_pct >= 0.15):
            _iv_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_iv_check_date") != _iv_today:
                sym["_iv_check_date"] = _iv_today
                try:
                    _tk_iv = yf.Ticker(symbol)
                    _exps  = _tk_iv.options
                    if _exps:
                        _chain = _tk_iv.option_chain(_exps[0])
                        _calls = _chain.calls
                        if not _calls.empty and "impliedVolatility" in _calls.columns:
                            _atm_i  = (_calls["strike"] - cur_price).abs().idxmin()
                            _iv_val = float(_calls.loc[_atm_i, "impliedVolatility"])
                            if _iv_val > 0.50:
                                _iv_rem = qty - sym.get("partial_qty", 0)
                                _log.warning(
                                    "[monitor] %s IV CRUSH EXIT: ATM IV=%.0f%%, "
                                    "%d days to earnings, pnl=+%.1f%% — closing runner",
                                    symbol, _iv_val * 100, _dte_iv, pnl_pct * 100)
                                _cancel_stop_orders(symbol)
                                if _iv_rem > 0 and _place_market_sell(symbol, _iv_rem):
                                    sym["iv_crush_exited"] = True
                                    changed = True
                                    try:
                                        import requests as _rqiv, os as _osiv
                                        _tiv = _osiv.getenv("TELEGRAM_BOT_TOKEN", "")
                                        _civ = _osiv.getenv("TELEGRAM_CHAT_ID", "")
                                        if _tiv and _civ:
                                            _rqiv.post(
                                                f"https://api.telegram.org/bot{_tiv}/sendMessage",
                                                json={"chat_id": _civ, "parse_mode": "Markdown",
                                                      "text": (
                                                          f"\U0001f9e8 *IV Crush Exit — {symbol}*\n"
                                                          f"ATM implied vol {_iv_val*100:.0f}% > 50%\n"
                                                          f"{_dte_iv} days to earnings | "
                                                          f"gain +{pnl_pct*100:.1f}%\n"
                                                          f"Exiting runner before IV collapse"
                                                      )},
                                                timeout=8)
                                    except Exception:
                                        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                except Exception as _ive:
                    _log.debug("[monitor] iv_crush %s: %s", symbol, _ive)

        # ── Step A: Initial stop (placed once when position first seen) ──────────
        # Use HARD STOP at VCP pivot low if we have the planned stop from the order.
        # This protects against false breakouts at exactly the level Minervini intends.
        # Fall back to 7% trailing stop if no metadata (legacy or missing report).
        stop_loss_level = sym.get("stop_loss", 0.0)
        use_hard_stop   = (stop_loss_level > 0 and stop_loss_level < avg_cost * 0.99
                           and not sym.get("breakeven_done")
                           and not sym.get("partial_done"))

        needs_stop = (not sym.get("trailing_stop_placed") or (
            sym.get("trailing_stop_placed") and
            sym.get("stop_order_id") and
            not _stop_order_alive(sym["stop_order_id"])
        ))

        if needs_stop:
            _cancel_stop_orders(symbol)
            if use_hard_stop:
                oid = _place_stop(symbol, qty, stop_loss_level)
                if oid:
                    sym["trailing_stop_placed"] = True
                    sym["stop_order_id"] = oid
                    sym["stop_type"] = "hard_pivot"
                    changed = True
                    _log.info("[monitor] %s HARD STOP at $%.2f (VCP pivot low)",
                              symbol, stop_loss_level)
            else:
                _eff_trail = sym.get("atr_trail_pct", trail_pct)
                oid = _place_trailing_stop(symbol, qty, _eff_trail)
                if oid:
                    sym["trailing_stop_placed"] = True
                    sym["stop_order_id"] = oid
                    sym["stop_type"] = "atr_trailing"
                    changed = True

        # ── Step Z: Failed breakout detection — exit if price falls back under pivot ──
        # Up to 20 trading days: if price decisively back below pivot AND no meaningful
        # gain has developed, the setup is dead. Minervini: pivot break = setup failure.
        # Guard: skip if position is already a winner (pnl > +3%) — treat as a runner.
        _buy_stp_z = sym.get("buy_stop", 0.0)
        _ed_z      = sym.get("entry_date", "")
        if (_buy_stp_z > 0
                and not sym.get("partial1_done")
                and not sym.get("failed_breakout_done")
                and _ed_z
                and 1 <= _trading_days_held(_ed_z) <= 20
                and cur_price < _buy_stp_z * 0.99
                and pnl_pct < 0.03):
            _rem_z = qty - sym.get("partial_qty", 0)
            _log.warning("[monitor] %s FAILED BREAKOUT — cur $%.2f < pivot $%.2f (day %d)",
                         symbol, cur_price, _buy_stp_z, _trading_days_held(_ed_z))
            _cancel_stop_orders(symbol)
            if _rem_z > 0 and _place_market_sell(symbol, _rem_z):
                sym["failed_breakout_done"] = True
                try:
                    from risk_manager import record_pivot_failure as _rpf
                    _rpf(symbol)
                except Exception:
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                changed = True
                try:
                    import requests as _rz, os as _oz
                    _tok = _oz.getenv("TELEGRAM_BOT_TOKEN", "")
                    _cid = _oz.getenv("TELEGRAM_CHAT_ID", "")
                    if _tok and _cid:
                        _rz.post(f"https://api.telegram.org/bot{_tok}/sendMessage",
                                 json={"chat_id": _cid, "parse_mode": "Markdown",
                                       "text": (f"❌ *Failed Breakout — {symbol}*\n"
                                                f"Price ${cur_price:.2f} fell back under pivot ${_buy_stp_z:.2f}\n"
                                                f"Day {_trading_days_held(_ed_z)} — cutting loss")},
                                 timeout=8)
                except Exception:
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # ── Weak vol override: tighten stop to -3% if weak breakout and price drops ───
        if (sym.get("weak_vol_breakout")
                and not sym.get("weak_vol_stop_set")
                and pnl_pct < -0.01
                and sym.get("trailing_stop_placed")
                and avg_cost > 0):
            _wv_stop = round(avg_cost * 0.97, 2)  # -3% tight stop
            _wv_rem  = qty - sym.get("partial_qty", 0)
            if _wv_rem > 0 and _wv_stop < cur_price:
                _cancel_stop_orders(symbol)
                _wv_oid = _place_stop(symbol, _wv_rem, _wv_stop)
                if _wv_oid:
                    sym["stop_order_id"]    = _wv_oid
                    sym["weak_vol_stop_set"] = True
                    changed = True
                    _log.info("[monitor] %s weak-vol stop tightened to $%.2f (-3%%)",
                              symbol, _wv_stop)

        # ── Step A-trail: Pivot-based trailing stop ────────────────────────────────
        # After 5+ days with open profit, ratchet stop up to latest swing low − 1%.
        # Minervini uses pivot lows as natural stop levels; more room than fixed %.
        if (sym.get("trailing_stop_placed")
                and not sym.get("breakeven_done")
                and not sym.get("partial1_done")
                and pnl_pct > 0.01):
            _ed_pt = sym.get("entry_date", "")
            if _ed_pt and _trading_days_held(_ed_pt) >= 5:
                _pt_today = datetime.now(_ET).strftime("%Y-%m-%d")
                if sym.get("_pivot_trail_date") != _pt_today:
                    sym["_pivot_trail_date"] = _pt_today
                    try:
                        _dfp = yf.Ticker(symbol).history(
                            period="30d", interval="1d", auto_adjust=True)
                        if len(_dfp) >= 5:
                            # Find most recent swing low in last 20 bars (skip last 2 incomplete)
                            _lows  = _dfp["Low"].values
                            _n_pt  = min(20, len(_lows) - 2)
                            _swing = None
                            for _i in range(1, _n_pt):
                                if _lows[_i] < _lows[_i - 1] and _lows[_i] < _lows[_i + 1]:
                                    _swing = _lows[_i]  # keep last (most recent) swing low
                            if _swing is not None:
                                _pivot_stop = round(float(_swing) * 0.99, 2)  # 1% cushion
                                _cur_stp    = sym.get("stop_loss", 0.0)
                                # Ratchet up only — never widen the stop
                                if _pivot_stop > _cur_stp and _pivot_stop < cur_price * 0.97:
                                    _rem_pt = qty - sym.get("partial_qty", 0)
                                    if _rem_pt > 0:
                                        _cancel_stop_orders(symbol)
                                        _pt_oid = _place_stop(symbol, _rem_pt, _pivot_stop)
                                        if _pt_oid:
                                            sym["stop_loss"]            = _pivot_stop
                                            sym["stop_order_id"]        = _pt_oid
                                            sym["stop_type"]            = "pivot_trail"
                                            sym["trailing_stop_placed"] = True
                                            changed = True
                                            _log.info(
                                                "[monitor] %s PIVOT TRAIL: stop $%.2f→$%.2f "
                                                "(swing low day %d)",
                                                symbol, _cur_stp, _pivot_stop,
                                                _trading_days_held(_ed_pt))
                    except Exception as _pte:
                        _log.debug("[monitor] pivot trail %s: %s", symbol, _pte)

        # ── Step A+: MA20 dynamic trail — after 10 trading days with profit ────────
        # Switch from fixed pivot stop to MA20*0.98 (ratchet up only).
        # Gives natural room in fast moves; tightens during consolidations.
        _ma20_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if (sym.get("_ma20_check_date") != _ma20_today
                and pnl_pct > 0.02
                and not sym.get("partial1_done")
                and not sym.get("breakeven_done")):
            _ed_ma = sym.get("entry_date", "")
            if _ed_ma and _trading_days_held(_ed_ma) >= 10:
                sym["_ma20_check_date"] = _ma20_today
                try:
                    _dfm = yf.Ticker(symbol).history(
                        period="35d", interval="1d", auto_adjust=True)
                    if len(_dfm) >= 20:
                        _ma20_val  = float(_dfm["Close"].rolling(20).mean().iloc[-1])
                        _ma20_stop = round(_ma20_val * 0.98, 2)
                        _cur_stop  = sym.get("stop_loss", 0.0)
                        # Ratchet up only — never widen the stop
                        if _ma20_stop > _cur_stop and _ma20_stop < cur_price * 0.99:
                            _rem_ma = qty - sym.get("partial_qty", 0)
                            if _rem_ma > 0:
                                _cancel_stop_orders(symbol)
                                _ma_oid = _place_stop(symbol, _rem_ma, _ma20_stop)
                                if _ma_oid:
                                    sym["stop_loss"]            = _ma20_stop
                                    sym["stop_order_id"]        = _ma_oid
                                    sym["stop_type"]            = "ma20_trail"
                                    sym["trailing_stop_placed"] = True
                                    changed = True
                                    _log.info(
                                        "[monitor] %s MA20 trail: stop $%.2f\u2192$%.2f "
                                        "(MA20=$%.2f, day %d)",
                                        symbol, _cur_stop, _ma20_stop,
                                        _ma20_val, _trading_days_held(_ed_ma))
                except Exception as _me:
                    _log.debug("[monitor] ma20 trail %s: %s", symbol, _me)

        # ── 8-Week Hold Rule: O'Neil fast mover detection ───────────────────────
        # Stock that gains ≥20% within first 15 trading days = potential 100%+ winner.
        # Override first partial: hold full position up to 8 weeks (40 trading days).
        _ed_fm = sym.get("entry_date", "")
        _td_fm = _trading_days_held(_ed_fm) if _ed_fm else 0
        if (not sym.get("fast_mover")
                and pnl_pct >= 0.20
                and 0 < _td_fm <= 15):
            sym["fast_mover"] = True
            _log.info("[monitor] %s FAST MOVER: +%.1f%% in %d days — 8-week hold rule activated",
                      symbol, pnl_pct * 100, _td_fm)
            try:
                import requests as _rqfm, os as _osfm
                _tfm = _osfm.getenv("TELEGRAM_BOT_TOKEN", "")
                _cfm = _osfm.getenv("TELEGRAM_CHAT_ID", "")
                if _tfm and _cfm:
                    _rqfm.post(
                        f"https://api.telegram.org/bot{_tfm}/sendMessage",
                        json={"chat_id": _cfm, "parse_mode": "Markdown",
                              "text": (f"🚀 *Fast Mover — {symbol}*\n"
                                       f"+{pnl_pct*100:.1f}% in {_td_fm} trading days\n"
                                       f"O'Neil 8-week hold rule activated — "
                                       f"holding full position to week 8")},
                        timeout=8)
            except Exception:
                _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # ── Step B1: First partial at +10% — sell 33%, keep current stop ─────────
        initial_qty = sym.get("initial_qty", qty)
        # Superperformance skip: composite ≥8 setups (elite VCPs) need more room before first partial
        _cs_val = float(sym.get("composite_score", 0.0) or 0.0)
        partial1_trigger = 0.15 if _cs_val >= 8.0 else 0.10
        mm_pct = sym.get("measured_move_pct", 0.0) or 0.0
        partial2_trigger = max(mm_pct, 0.20) if mm_pct > 0.05 else 0.20

        _skip_b1_8w = sym.get("fast_mover") and _trading_days_held(sym.get("entry_date", "")) < 40
        if pnl_pct >= partial1_trigger and not sym.get("partial1_done") and not _skip_b1_8w:
            sell_qty = max(1, round(initial_qty / 3))
            _lim1 = round(cur_price * 0.999, 2)  # 0.1% below market — fast fill, better price
            if _place_limit_sell(symbol, sell_qty, _lim1):
                sym["partial1_done"] = True
                sym["partial_done"]  = True   # backward-compat for time stop check
                sym["partial_qty"]   = sell_qty
                sym["partial1_price"] = cur_price
                changed = True
                _log.info("[monitor] ✓ %s PARTIAL-1 (33%%): sold %d sh @ $%.2f (+%.1f%%)",
                          symbol, sell_qty, cur_price, pnl_pct * 100)

        # ── Step B2: Second partial at measured move or +20% — sell 33%, tighten ─
        elif (sym.get("partial1_done") and
              pnl_pct >= partial2_trigger and
              not sym.get("partial2_done")):
            already_sold = sym.get("partial_qty", 0)
            sell_qty2 = max(1, round(initial_qty / 3))
            _lim2 = round(cur_price * 0.999, 2)
            if _place_limit_sell(symbol, sell_qty2, _lim2):
                sym["partial2_done"]  = True
                sym["partial2_qty"]   = sell_qty2
                sym["partial_qty"]    = already_sold + sell_qty2
                sym["partial2_price"] = cur_price
                changed = True
                _log.info("[monitor] ✓ %s PARTIAL-2 (33%%): sold %d sh @ $%.2f (+%.1f%%)"
                          " — runner with 5%% trailing",
                          symbol, sell_qty2, cur_price, pnl_pct * 100)
                # Tighten trailing stop for the remaining ~34% runner
                # Use tighter of ATR-derived trail or 5% fixed
                runner_qty = initial_qty - sym["partial_qty"]
                if runner_qty > 0:
                    _cancel_stop_orders(symbol)
                    _runner_trail = min(sym.get("atr_trail_pct", 0.05), 0.05)
                    oid2 = _place_trailing_stop(symbol, runner_qty, _runner_trail)
                    sym["trailing_stop_placed"] = True
                    if oid2:
                        sym["stop_order_id"] = oid2

        # ── Step P: Pyramid — add 30% of initial qty after first partial ────────
        # Conditions: partial1 already taken (position proved itself), gain 12–20%,
        # held at least 3 trading days, not yet pyramided, market is open.
        # Uses market order — pyramid shares are entered at current price.
        if (sym.get("partial1_done")
                and not sym.get("pyramid_done")
                and not sym.get("partial2_done")
                and 0.12 <= pnl_pct <= 0.20):
            _pyr_days = _trading_days_held(sym.get("entry_date", ""))
            if _pyr_days >= 3:
                _pyr_qty = max(1, round(sym.get("initial_qty", qty) * 0.30))
                _pyr_above_ma20 = False
                try:
                    _df_pyr = yf.Ticker(symbol).history(
                        period="40d", interval="1d", auto_adjust=True)
                    if len(_df_pyr) >= 20:
                        _ma20_pyr = float(_df_pyr["Close"].rolling(20).mean().iloc[-1])
                        _pyr_above_ma20 = cur_price > _ma20_pyr
                except Exception:
                    _pyr_above_ma20 = True  # assume OK if data unavailable
                if _pyr_above_ma20:
                    if _place_market_buy(symbol, _pyr_qty):
                        sym["pyramid_done"]  = True
                        sym["pyramid_qty"]   = _pyr_qty
                        sym["pyramid_price"] = cur_price
                        changed = True
                        _log.info("[monitor] ✓ %s PYRAMID: added %d sh @ $%.2f (+%.1f%%, day %d)",
                                  symbol, _pyr_qty, cur_price, pnl_pct * 100, _pyr_days)
                        try:
                            import requests as _rqp, os as _osp
                            _tokp = _osp.getenv("TELEGRAM_BOT_TOKEN", "")
                            _cidp = _osp.getenv("TELEGRAM_CHAT_ID", "")
                            if _tokp and _cidp:
                                _rqp.post(
                                    f"https://api.telegram.org/bot{_tokp}/sendMessage",
                                    json={"chat_id": _cidp, "parse_mode": "Markdown",
                                          "text": (f"📈 *Pyramid* — {symbol}\n"
                                                   f"Added {_pyr_qty} shares @ ${cur_price:.2f} "
                                                   f"(+{pnl_pct*100:.1f}%, day {_pyr_days})")},
                                    timeout=8)
                        except Exception:
                            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
        # ── Step C: Move stop to breakeven at +8% (if no partial yet) ───────────
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

        # ── Step C2: Sector ETF < MA50 — force breakeven on sector weakness ─────
        # If the broader sector is losing leadership, tighten before the position turns.
        # Runs once per day per position (ETF data cached 1h).
        if (not sym.get("breakeven_done")
                and pnl_pct > 0.01):
            _c2_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_sector_be_date") != _c2_today:
                sym["_sector_be_date"] = _c2_today
                if _sector_etf_below_ma50(symbol):
                    _c2_remaining = qty - sym.get("partial_qty", 0)
                    if _c2_remaining > 0:
                        _cancel_stop_orders(symbol)
                        _c2_oid = _place_stop(symbol, _c2_remaining, round(avg_cost, 2))
                        if _c2_oid:
                            sym["breakeven_done"] = True
                            sym["stop_order_id"]  = _c2_oid
                            changed = True
                            _log.warning(
                                "[monitor] %s SECTOR ETF < MA50 — "
                                "forced breakeven $%.2f (pnl=+%.1f%%)",
                                symbol, avg_cost, pnl_pct * 100)

        # ── Profit lock: raise stop to +7% when gain ≥ +15% ─────────────────────
        # Protects runner profit between B1 (+10%) and B2 (measured move).
        # Ensures at least 7%% gain is locked even if position reverses before B2.
        if (sym.get("partial1_done")
                and not sym.get("partial2_done")
                and not sym.get("profit_locked")
                and pnl_pct >= 0.15):
            _lock_stop = round(avg_cost * 1.07, 2)
            _cur_stp_l = sym.get("stop_loss", 0.0)
            if _lock_stop > _cur_stp_l:
                _runner_l = qty - sym.get("partial_qty", 0)
                if _runner_l > 0:
                    _cancel_stop_orders(symbol)
                    _lock_oid = _place_stop(symbol, _runner_l, _lock_stop)
                    if _lock_oid:
                        sym["profit_locked"]  = True
                        sym["stop_loss"]       = _lock_stop
                        sym["stop_order_id"]   = _lock_oid
                        changed = True
                        _log.info("[monitor] %s PROFIT LOCK +15%%: stop raised to +7%% ($%.2f)",
                                  symbol, _lock_stop)

        # ── Step R: 2R profit lock — when gain reaches 2R, stop moves to 1R ──────
        # More precise than the +8% breakeven: uses actual risk of this specific trade.
        # A 5% stop trade needs +10% to hit 2R; a 3% stop needs only +6%.
        _sli = sym.get("stop_loss_initial", 0.0)
        _rps = (avg_cost - _sli) if _sli > 0 and _sli < avg_cost else 0.0
        if (_rps > 0
                and not sym.get("two_r_stop_done")
                and not sym.get("partial2_done")
                and (cur_price - avg_cost) >= 2 * _rps):
            _one_r_stop = round(avg_cost + _rps, 2)
            _cur_stp_r  = sym.get("stop_loss", 0.0)
            if _one_r_stop > _cur_stp_r:
                _rem_r = qty - sym.get("partial_qty", 0)
                if _rem_r > 0:
                    _cancel_stop_orders(symbol)
                    _r_oid = _place_stop(symbol, _rem_r, _one_r_stop)
                    if _r_oid:
                        sym["two_r_stop_done"] = True
                        sym["breakeven_done"]  = True
                        sym["stop_loss"]       = _one_r_stop
                        sym["stop_order_id"]   = _r_oid
                        changed = True
                        _log.info(
                            "[monitor] %s 2R LOCK: stop $%.2f → $%.2f "
                            "(pnl=+%.1f%%, 1R=$%.2f)",
                            symbol, _cur_stp_r, _one_r_stop,
                            pnl_pct * 100, _rps)

        # ── Step G2: Runner upgrade — widen trail to 8% when past 2× measured move ─
        # Position has proven itself past double the expected target; give the runner room.
        if (sym.get("partial2_done")
                and not sym.get("runner_upgraded")
                and mm_pct > 0.05
                and pnl_pct >= mm_pct * 2):
            _runner_2x = qty - sym.get("partial_qty", 0)
            if _runner_2x > 0:
                _cancel_stop_orders(symbol)
                _r2x_oid = _place_trailing_stop(symbol, _runner_2x, 0.08)
                if _r2x_oid:
                    sym["runner_upgraded"] = True
                    sym["stop_order_id"]   = _r2x_oid
                    changed = True
                    _log.info("[monitor] %s RUNNER UPGRADE: trail 5%%→8%% at 2×mm (pnl=+%.1f%%)",
                              symbol, pnl_pct * 100)

        # ── Step V: Volume climax exit — monster-volume day after ≥20% gain ────────
        # Blow-off top signal: churning on extreme volume = likely institutional exit.
        # Minervini explicitly warns: "when everyone wants in on huge volume, take profits."
        if (not sym.get("climax_exit_done")
                and pnl_pct >= 0.20):
            _vc_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_vc_check_date") != _vc_today:
                sym["_vc_check_date"] = _vc_today
                try:
                    _dfvc = yf.Ticker(symbol).history(
                        period="90d", interval="1d", auto_adjust=True)
                    if len(_dfvc) >= 50:
                        _vol_cur  = float(_dfvc["Volume"].iloc[-1])
                        _vol_avg  = float(_dfvc["Volume"].tail(51).iloc[:-1].mean())
                        if _vol_avg > 0 and _vol_cur >= _vol_avg * 3.0:
                            _vc_qty = max(1, round(qty * 0.50))
                            _log.warning(
                                "[monitor] %s VOLUME CLIMAX: %.1f× avg vol, pnl=+%.1f%% — "
                                "selling 50%% (%d sh) at $%.2f",
                                symbol, _vol_cur / _vol_avg, pnl_pct * 100,
                                _vc_qty, cur_price)
                            if _place_market_sell(symbol, _vc_qty):
                                sym["climax_exit_done"] = True
                                # Mark partial1 only if not already done
                                if not sym.get("partial1_done"):
                                    sym["partial1_done"] = True
                                    sym["partial_done"]  = True
                                sym["partial_qty"] = sym.get("partial_qty", 0) + _vc_qty
                                changed = True
                                try:
                                    import requests as _rqv, os as _osv
                                    _tv = _osv.getenv("TELEGRAM_BOT_TOKEN", "")
                                    _cv = _osv.getenv("TELEGRAM_CHAT_ID", "")
                                    if _tv and _cv:
                                        _rqv.post(
                                            f"https://api.telegram.org/bot{_tv}/sendMessage",
                                            json={"chat_id": _cv, "parse_mode": "Markdown",
                                                  "text": (f"🔥 *Volume Climax — {symbol}*\n"
                                                           f"{_vol_cur/_vol_avg:.1f}× avg vol, "
                                                           f"pnl=+{pnl_pct*100:.1f}%\n"
                                                           f"Sold 50% — Minervini blow-off top")},
                                            timeout=8)
                                except Exception:
                                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                except Exception as _vce:
                    _log.debug("[monitor] volume climax %s: %s", symbol, _vce)

        # ── Step LH/LL: Lower-high + lower-low on daily = trend reversal ─────────────
        # After all partials are done, if the runner shows a structural trend break exit.
        # Minervini: "sell when the stock starts acting abnormally — lower highs confirm weakness."
        if (sym.get("partial2_done")
                and not sym.get("lhll_stopped")):
            try:
                _dfll = yf.Ticker(symbol).history(
                    period="15d", interval="1d", auto_adjust=True)
                if len(_dfll) >= 4:
                    _hh = _dfll["High"].values
                    _ll = _dfll["Low"].values
                    _cc = _dfll["Close"].values
                    # Lower high: bar[-2].high < bar[-3].high  (completed bars)
                    # Lower low:  bar[-1].close < bar[-2].low  (today closed below yesterday low)
                    if _hh[-2] < _hh[-3] and _cc[-1] < _ll[-2]:
                        _runner_ll = qty - sym.get("partial_qty", 0)
                        if _runner_ll > 0:
                            _log.warning(
                                "[monitor] %s LH/LL trend reversal — closing runner "
                                "(%d sh) at $%.2f (pnl=%.1f%%)",
                                symbol, _runner_ll, cur_price, pnl_pct * 100)
                            _cancel_stop_orders(symbol)
                            if _place_market_sell(symbol, _runner_ll):
                                sym["lhll_stopped"] = True
                                changed = True
            except Exception as _lle:
                _log.debug("[monitor] LH/LL %s: %s", symbol, _lle)

        # ── PM10: PEAD — Post-Earnings Announcement Drift (60-day time-stop hold) ────
        # Academic finding: stocks that beat EPS estimates by ≥5% drift up ~60 trading days.
        # Suspending the time stop during this window avoids selling the best winners early.
        if not sym.get("pead_hold") and not sym.get("pead_checked") and pnl_pct > 0:
            _pead_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_pead_check_date") != _pead_today:
                sym["_pead_check_date"] = _pead_today
                try:
                    _ed_df = yf.Ticker(symbol).earnings_dates
                    if _ed_df is not None and not _ed_df.empty:
                        _ed_r = _ed_df.reset_index()
                        _now_ts = pd.Timestamp.now(tz="UTC")
                        _past = _ed_r[
                            pd.to_datetime(_ed_r.iloc[:, 0], utc=True, errors="coerce")
                            < _now_ts
                        ]
                        if not _past.empty:
                            _le   = _past.iloc[0]
                            _est  = float(_le.get("EPS Estimate", 0) or 0)
                            _rep  = float(_le.get("Reported EPS", 0) or 0)
                            sym["pead_checked"] = True
                            if _est > 0 and _rep >= _est * 1.05:
                                sym["pead_hold"]  = True
                                sym["pead_date"]  = datetime.now(_ET).strftime("%Y-%m-%d")
                                _log.info(
                                    "[monitor] %s PEAD: EPS $%.2f vs est $%.2f (+%.0f%%) "
                                    "— 60-day time-stop hold activated",
                                    symbol, _rep, _est, (_rep - _est) / _est * 100)
                                try:
                                    import requests as _rqpd, os as _ospd
                                    _tpd = _ospd.getenv("TELEGRAM_BOT_TOKEN", "")
                                    _cpd = _ospd.getenv("TELEGRAM_CHAT_ID", "")
                                    if _tpd and _cpd:
                                        _rqpd.post(
                                            f"https://api.telegram.org/bot{_tpd}/sendMessage",
                                            json={"chat_id": _cpd, "parse_mode": "Markdown",
                                                  "text": (
                                                      f"\U0001f4c8 *PEAD Hold — {symbol}*\n"
                                                      f"EPS ${_rep:.2f} beat est ${_est:.2f}"
                                                      f" (+{(_rep-_est)/_est*100:.0f}%)\n"
                                                      f"60-day time-stop suspended"
                                                  )},
                                            timeout=8)
                                except Exception:
                                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                except Exception as _pde:
                    _log.debug("[monitor] pead_check %s: %s", symbol, _pde)

        # ── RS-line divergence alert (daily, once per position) ────────────────
        # If price makes new 20-day high but RS line (stock/SPY) does not,
        # distribution is likely. Minervini: tighten stop when RS diverges from price.
        _rsd_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if (not sym.get("partial2_done")
                and pnl_pct > 0.03
                and sym.get("_rsd_date") != _rsd_today):
            sym["_rsd_date"] = _rsd_today
            try:
                _df_rsd = yf.Ticker(symbol).history(
                    period="60d", interval="1d", auto_adjust=True)
                _spy_rsd = yf.Ticker("SPY").history(
                    period="60d", interval="1d", auto_adjust=True)["Close"]
                if len(_df_rsd) >= 22 and len(_spy_rsd) >= 22:
                    _c_rsd   = _df_rsd["Close"]
                    _h_rsd   = _df_rsd["High"]
                    _rs_line = (_c_rsd / _spy_rsd.reindex(_c_rsd.index, method="nearest")
                                ).dropna()
                    if len(_rs_line) >= 22:
                        _price_20h = float(_h_rsd.iloc[-21:-1].max())
                        _rs_20h    = float(_rs_line.iloc[-21:-1].max())
                        _price_now = float(_h_rsd.iloc[-1])
                        _rs_now_v  = float(_rs_line.iloc[-1])
                        # Price makes new 20-day high but RS line does not confirm
                        _price_nh  = _price_now >= _price_20h * 0.999
                        _rs_lagging = _rs_now_v < _rs_20h * 0.98
                        if _price_nh and _rs_lagging:
                            _log.warning(
                                "[monitor] %s RS DIVERGENCE: price new 20d high $%.2f "
                                "but RS line %.2f%% below 20d peak — tightening stop",
                                symbol, _price_now, (1 - _rs_now_v / _rs_20h) * 100)
                            # Tighten: if stop not at breakeven, move to breakeven
                            if not sym.get("breakeven_done"):
                                _rem_rsd = qty - sym.get("partial_qty", 0)
                                if _rem_rsd > 0:
                                    _cancel_stop_orders(symbol)
                                    _rsd_oid = _place_stop(symbol, _rem_rsd, round(avg_cost, 2))
                                    if _rsd_oid:
                                        sym["breakeven_done"] = True
                                        sym["stop_order_id"]  = _rsd_oid
                                        sym["stop_loss"]      = avg_cost
                                        changed = True
                            try:
                                import requests as _rqrsd, os as _osrsd
                                _tr = _osrsd.getenv("TELEGRAM_BOT_TOKEN", "")
                                _cr = _osrsd.getenv("TELEGRAM_CHAT_ID", "")
                                if _tr and _cr:
                                    _rqrsd.post(
                                        f"https://api.telegram.org/bot{_tr}/sendMessage",
                                        json={"chat_id": _cr, "parse_mode": "Markdown",
                                              "text": (
                                                  f"⚠️ *RS Divergence — {symbol}*\n"
                                                  f"Price new 20d high ${_price_now:.2f} "
                                                  f"but RS line {(1-_rs_now_v/_rs_20h)*100:.1f}%% "
                                                  f"below its peak\n"
                                                  f"Distribution signal — stop moved to breakeven"
                                              )},
                                        timeout=8)
                            except Exception:
                                _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
            except Exception as _rsd_e:
                _log.debug("[monitor] rs_divergence %s: %s", symbol, _rsd_e)

        # ── Step D: Time stop — exit stagnant positions (Minervini 3-4 week rule) ──
        time_stop_gain = cfg.get("time_stop_min_gain_pct", 0.02)
        entry_date_str = sym.get("entry_date", "")
        _pead_active = (
            sym.get("pead_hold")
            and _trading_days_held(sym.get("pead_date", "")) < 60
        )

        # ── Hard absolute max holding period: 60 trading days ──────────────────────
        # Prevents positions from becoming indefinite anchors. Winners get a tight
        # 3% trailing stop; flat/losers are closed immediately.
        _HARD_MAX_DAYS = 60
        if (entry_date_str
                and not sym.get("max_hold_exited")
                and not sym.get("time_stopped")
                and not _pead_active):
            _abs_days = _trading_days_held(entry_date_str)
            if _abs_days >= _HARD_MAX_DAYS:
                _rem_hm = qty - sym.get("partial_qty", 0)
                if pnl_pct >= 0.05 and _rem_hm > 0:
                    _hm_oid = _place_trailing_stop(symbol, _rem_hm, 0.03)
                    if _hm_oid:
                        sym["stop_order_id"] = _hm_oid
                        sym["max_hold_exited"] = True
                        changed = True
                        _log.warning("[monitor] MAX HOLD %s day %d pnl=+%.1f%% — tightened to 3%% trailing",
                                     symbol, _abs_days, pnl_pct * 100)
                        try:
                            import requests as _rqmh, os as _osmh
                            _tok_mh = _osmh.getenv("TELEGRAM_BOT_TOKEN", "")
                            _cid_mh = _osmh.getenv("TELEGRAM_CHAT_ID", "")
                            if _tok_mh and _cid_mh:
                                _rqmh.post(
                                    f"https://api.telegram.org/bot{_tok_mh}/sendMessage",
                                    json={"chat_id": _cid_mh, "parse_mode": "Markdown",
                                          "text": ("Max Hold " + symbol + "\n"
                                                   + f"Day {_abs_days} - tightened to 3% trailing\n"
                                                   + f"P&L {pnl_pct*100:+.1f}% - locking gains")},
                                    timeout=8)
                        except Exception:
                            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                elif _rem_hm > 0:
                    _cancel_stop_orders(symbol)
                    if _place_market_sell(symbol, _rem_hm):
                        sym["max_hold_exited"] = True
                        changed = True
                        _log.warning("[monitor] MAX HOLD EXIT %s day %d pnl=%.1f%% - closed",
                                     symbol, _abs_days, pnl_pct * 100)
                        try:
                            import requests as _rqmh2, os as _osmh2
                            _tok_mh2 = _osmh2.getenv("TELEGRAM_BOT_TOKEN", "")
                            _cid_mh2 = _osmh2.getenv("TELEGRAM_CHAT_ID", "")
                            if _tok_mh2 and _cid_mh2:
                                _rqmh2.post(
                                    f"https://api.telegram.org/bot{_tok_mh2}/sendMessage",
                                    json={"chat_id": _cid_mh2, "parse_mode": "Markdown",
                                          "text": ("Max Hold Exit " + symbol + "\n"
                                                   + f"Day {_abs_days} - closed at {pnl_pct*100:+.1f}%\n"
                                                   + "60-day absolute cap reached")},
                                    timeout=8)
                        except Exception:
                            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    # -- Stale position review (>30 trading days, no major exit milestone) --------
    for _sym_st, _sym_std in state.items():
        if _sym_st not in {p["symbol"] for p in positions}:
            continue
        _stale_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if _sym_std.get("_stale_alert_date") == _stale_today:
            continue
        _ed_st = _sym_std.get("entry_date", "")
        if not _ed_st:
            continue
        if _trading_days_held(_ed_st) < 30:
            continue
        if (_sym_std.get("partial2_done") or _sym_std.get("climax_exited")
                or _sym_std.get("time_stopped") or _sym_std.get("weekly_close_exited")):
            continue
        _sym_std["_stale_alert_date"] = _stale_today
        try:
            import requests as _rqst, os as _ost
            _tok_st = _ost.getenv("TELEGRAM_BOT_TOKEN", "")
            _cid_st = _ost.getenv("TELEGRAM_CHAT_ID", "")
            if _tok_st and _cid_st:
                _cur_p   = next((p for p in positions if p["symbol"] == _sym_st), None)
                _pnl_st  = float(_cur_p.get("unrealized_plpc", 0)) * 100 if _cur_p else 0.0
                _days_st = _trading_days_held(_ed_st)
                _rqst.post(
                    f"https://api.telegram.org/bot{_tok_st}/sendMessage",
                    json={"chat_id": _cid_st, "parse_mode": "Markdown",
                          "text": ("Stale Position -- " + _sym_st + "\n"
                                   + f"Held {_days_st} trading days - P&L {_pnl_st:+.1f}%\n"
                                   + "Review: is the VCP thesis still valid?")},
                    timeout=5)
                changed = True
        except Exception:
            _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
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
                from risk_manager import close_trade, record_stop_out
                from broker import get_account
                portfolio_value = get_account()["portfolio_value"]
                close_trade(sym, pnl_pct, portfolio_value)   # start_value read from risk_state
                _journal_trade(sym, sym_data, pnl_pct, portfolio_value)

                # OP1: Update signal accuracy on trade close
                try:
                    import json as _jsa2, os as _osa2
                    _sa_path = _osa2.path.join(_osa2.path.dirname(__file__),
                                               "logs", "signal_accuracy.json")
                    if _osa2.path.exists(_sa_path):
                        _sa2 = _jsa2.loads(open(_sa_path).read())
                        _r_mult = float(sym_data.get("composite_score", 0))
                        _won    = pnl_pct > 0
                        _sig_names2 = [
                            "rs_line_at_high", "rs_line_leading", "eps_accelerating",
                            "rev_accelerating", "three_weeks_tight", "pocket_pivot",
                            "insider_buying", "industry_leader", "eps_revision_up",
                            "accum_weeks_strong", "analyst_pt_upside",
                            "inst_ownership_increasing", "near_ath", "weekly_stage2",
                            "pead_hold",
                        ]
                        _stop_init = float(sym_data.get("stop_loss_initial", 0) or 0)
                        _avg_c     = float(sym_data.get("avg_cost", 0) or 0)
                        _last_p    = float(sym_data.get("last_price", 0) or 0)
                        _risk_ps   = max(_avg_c - _stop_init, _avg_c * 0.07, 0.001)
                        _r_val     = (_last_p - _avg_c) / _risk_ps
                        _won       = _last_p > _avg_c
                        _active_sigs = sym_data.get("active_signals", [])
                        for _sig2 in _active_sigs:
                            if _sig2 in _sa2:
                                if _won:
                                    _sa2[_sig2]["wins"]   += 1
                                else:
                                    _sa2[_sig2]["losses"] += 1
                                _sa2[_sig2]["total_r"] = round(
                                    _sa2[_sig2].get("total_r", 0.0) + _r_val, 3)
                        open(_sa_path, "w").write(_jsa2.dumps(_sa2, indent=2))
                except Exception:
                    _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
                # Re-entry cooldown: shorter if pyramid was added (shakeout of add-on
                # shares ≠ full base failure), normal 5 days otherwise
                if pnl_pct < 0:
                    _cd = 2 if sym_data.get("pyramid_done") else 5
                    record_stop_out(sym, breakout_level=float(sym_data.get("buy_stop", 0.0)),
                                    cooldown_days=_cd)
            except Exception as e:
                _log.warning("[monitor] close_trade %s failed: %s", sym, e)

    if changed:
        _save_state(state)


# ── Background thread entry point ─────────────────────────────────────────────

def _seconds_until_market_open() -> int:
    """Seconds until next NYSE open (09:30 ET on a weekday).
    Used to sleep through weekends and after-hours without wasteful polling.
    """
    now_et = datetime.now(_ET)
    # Find next weekday with market open
    candidate = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et.time() >= _MARKET_OPEN:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(60, int((candidate - now_et).total_seconds()))


def run_monitor(interval_minutes: int = 15,
                stop_event: threading.Event | None = None) -> None:
    """Run the position monitor in a blocking loop. Launch from a daemon thread."""
    if stop_event is None:
        stop_event = threading.Event()

    _log.info("[monitor] Position monitor started (interval=%d min)", interval_minutes)

    while not stop_event.is_set():
        now_et = datetime.now(_ET)
        if now_et.weekday() >= 5 or now_et.time() >= _MARKET_CLOSE:
            # Weekend or after close — sleep until next market open
            secs = _seconds_until_market_open()
            _log.info("[monitor] Market closed — sleeping %dh %dm until next open",
                      secs // 3600, (secs % 3600) // 60)
            stop_event.wait(secs)
            continue

        try:
            check_positions()
        except Exception as e:
            _log.error("[monitor] Unexpected error: %s", e, exc_info=True)
        stop_event.wait(interval_minutes * 60)

    _log.info("[monitor] Position monitor stopped")
