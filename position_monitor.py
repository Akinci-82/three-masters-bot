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
    return ALPACA_BASE_URL.rstrip("/") + "/v2"


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


# ── Sector ETF map (minimal copy for position monitor) ─────────────────────
_SECTOR_ETF_PM: dict[str, str] = {
    "Technology": "XLK", "Financial Services": "XLF", "Financials": "XLF",
    "Healthcare": "XLV", "Health Care": "XLV", "Energy": "XLE",
    "Consumer Cyclical": "XLY", "Consumer Discretionary": "XLY",
    "Industrials": "XLI", "Basic Materials": "XLB", "Materials": "XLB",
    "Real Estate": "XLRE", "Utilities": "XLU",
    "Consumer Defensive": "XLP", "Consumer Staples": "XLP",
    "Communication Services": "XLC",
}
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
        import yfinance as _yf_etf
        _col = _yf_etf.Ticker(etf).history(
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
        import yfinance as _yf_rg
        _spy = _yf_rg.Ticker("SPY").history(
            period="200d", interval="1d", auto_adjust=True)["Close"]
        if len(_spy) >= 50:
            _ma200 = float(_spy.tail(200).mean())
            _pct   = (float(_spy.iloc[-1]) - _ma200) / _ma200
            _cached_regime_pm = ("bull" if _pct > 0.02
                                  else ("bear" if _pct < -0.02 else "neutral"))
    except Exception:
        pass
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
                    }
        except Exception:
            pass
    return {"stop_loss": 0.0, "quality_score": 0, "composite_score": 0.0, "measured_move_pct": 0.0, "buy_stop": 0.0}


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
            pass
        return
    if not positions:
        return

    from config import MONITOR as cfg
    trail_pct         = cfg.get("trailing_stop_pct", 0.07)
    breakeven_trigger = cfg.get("breakeven_trigger", 0.08)
    partial_pct       = cfg.get("partial_exit_pct", 0.50)
    # composite-adjusted thresholds are set per-position inside the loop

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

        # On first encounter: look up VCP stop loss + quality from daily report
        if not sym.get("_meta_loaded"):
            meta = _lookup_position_metadata(symbol)
            sym["_meta_loaded"]       = True
            sym["stop_loss"]          = meta["stop_loss"]
            sym["stop_loss_initial"]  = meta["stop_loss"]  # preserved for R-multiple calc
            sym["quality_score"]      = meta["quality_score"]
            sym["composite_score"]    = meta["composite_score"]
            sym["measured_move_pct"]  = meta["measured_move_pct"]
            sym["buy_stop"]           = meta["buy_stop"]
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
                import yfinance as _yf_bv
                _bv_df = _yf_bv.Ticker(symbol).history(
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
                pass
            changed = True

        _log.debug("[monitor] %s  qty=%d  avg=$%.2f  cur=$%.2f  pnl=%.1f%%",
                   symbol, qty, avg_cost, cur_price, pnl_pct * 100)

        # ── PM11: Stop order re-validation ───────────────────────────────────────
        # GTC stops can be silently cancelled (corporate actions, margin events, API failures).
        # Once per day: verify expected stop is still active; re-place if missing.
        _sv_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if (sym.get("trailing_stop_placed")
                and sym.get("stop_order_id")
                and sym.get("_sv_date") != _sv_today
                and not sym.get("time_stopped")
                and not sym.get("max_loss_exited")):
            sym["_sv_date"] = _sv_today
            try:
                _open_ords = _get_open_orders(symbol)
                _stop_ids  = {o.get("id") for o in _open_ords
                              if o.get("type") in ("stop", "trailing_stop", "stop_limit")}
                if sym["stop_order_id"] not in _stop_ids and not _stop_ids:
                    _sv_price = sym.get("stop_loss", round(avg_cost * 0.93, 2))
                    _sv_rem   = qty - sym.get("partial_qty", 0)
                    _log.warning("[monitor] %s STOP MISSING (was %s) — re-placing at $%.2f",
                                 symbol, sym["stop_order_id"], _sv_price)
                    _sv_oid = _place_stop(symbol, _sv_rem, _sv_price) if _sv_rem > 0 else None
                    if _sv_oid:
                        sym["stop_order_id"] = _sv_oid
                        changed = True
                    try:
                        import requests as _rqsv, os as _ossv
                        _tsv = _ossv.getenv("TELEGRAM_BOT_TOKEN", "")
                        _csv2 = _ossv.getenv("TELEGRAM_CHAT_ID", "")
                        if _tsv and _csv2:
                            _rqsv.post(
                                f"https://api.telegram.org/bot{_tsv}/sendMessage",
                                json={"chat_id": _csv2, "parse_mode": "Markdown",
                                      "text": (
                                          f"\u26a0\ufe0f *Stop Missing — {symbol}*\n"
                                          f"GTC stop not found on Alpaca\n"
                                          f"Re-placed at ${_sv_price:.2f}"
                                      )},
                                timeout=8)
                    except Exception:
                        pass
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
                        pass

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
                import yfinance as _yf_g
                _dfg = _yf_g.Ticker(symbol).history(
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
                                    pass
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
                                    pass
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
                    import yfinance as _yf_iv
                    _tk_iv = _yf_iv.Ticker(symbol)
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
                                        pass
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
                oid = _place_trailing_stop(symbol, qty, trail_pct)
                if oid:
                    sym["trailing_stop_placed"] = True
                    sym["stop_order_id"] = oid
                    sym["stop_type"] = "trailing"
                    changed = True

        # ── Step Z: Failed breakout detection — exit if price falls back under pivot ──
        # Within 5 trading days of entry: price back below buy_stop = failed breakout.
        # Minervini exits immediately — cut losses before they compound.
        _buy_stp_z = sym.get("buy_stop", 0.0)
        _ed_z      = sym.get("entry_date", "")
        if (_buy_stp_z > 0
                and not sym.get("partial1_done")
                and not sym.get("failed_breakout_done")
                and _ed_z
                and 1 <= _trading_days_held(_ed_z) <= 5
                and cur_price < _buy_stp_z):
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
                    pass
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
                    pass

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
                        import yfinance as _yf_pt
                        _dfp = _yf_pt.Ticker(symbol).history(
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
                    import yfinance as _yf_ma
                    _dfm = _yf_ma.Ticker(symbol).history(
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
                pass

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
                runner_qty = initial_qty - sym["partial_qty"]
                if runner_qty > 0:
                    _cancel_stop_orders(symbol)
                    oid2 = _place_trailing_stop(symbol, runner_qty, 0.05)
                    sym["trailing_stop_placed"] = True
                    if oid2:
                        sym["stop_order_id"] = oid2

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
                    import yfinance as _yf_vc
                    _dfvc = _yf_vc.Ticker(symbol).history(
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
                                    pass
                except Exception as _vce:
                    _log.debug("[monitor] volume climax %s: %s", symbol, _vce)

        # ── Step LH/LL: Lower-high + lower-low on daily = trend reversal ─────────────
        # After all partials are done, if the runner shows a structural trend break exit.
        # Minervini: "sell when the stock starts acting abnormally — lower highs confirm weakness."
        if (sym.get("partial2_done")
                and not sym.get("lhll_stopped")):
            try:
                import yfinance as _yf_ll
                _dfll = _yf_ll.Ticker(symbol).history(
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
                    import yfinance as _yf_pd
                    _ed_df = _yf_pd.Ticker(symbol).earnings_dates
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
                                    pass
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
                import yfinance as _yf_rsd
                _df_rsd = _yf_rsd.Ticker(symbol).history(
                    period="60d", interval="1d", auto_adjust=True)
                _spy_rsd = _yf_rsd.Ticker("SPY").history(
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
                                pass
            except Exception as _rsd_e:
                _log.debug("[monitor] rs_divergence %s: %s", symbol, _rsd_e)

        # ── Step D: Time stop — exit stagnant positions (Minervini 3-4 week rule) ──
        time_stop_gain = cfg.get("time_stop_min_gain_pct", 0.02)
        entry_date_str = sym.get("entry_date", "")
        _pead_active = (
            sym.get("pead_hold")
            and _trading_days_held(sym.get("pead_date", "")) < 60
        )
        if (entry_date_str
                and not sym.get("partial_done")
                and pnl_pct < time_stop_gain
                and not _pead_active):
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

        # ── Step RD: Reversal day — intraday new high closes in bottom 25% of range ────
        # Minervini: stock makes a new recent high intraday but sellers overwhelm → close is weak.
        # After ≥20% gain this pattern on high volume signals distribution at the top.
        # Action: Telegram alert + raise stop to breakeven (if not already above).
        if pnl_pct >= 0.20 and not sym.get("reversal_day_exited"):
            _rd_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_rd_check_date") != _rd_today:
                sym["_rd_check_date"] = _rd_today
                try:
                    import yfinance as _yf_rd
                    _df_rd = _yf_rd.Ticker(symbol).history(
                        period="60d", interval="1d", auto_adjust=True)
                    if len(_df_rd) >= 30:
                        _rd_high  = float(_df_rd["High"].iloc[-1])
                        _rd_low   = float(_df_rd["Low"].iloc[-1])
                        _rd_close = float(_df_rd["Close"].iloc[-1])
                        _rd_30h   = float(_df_rd["High"].iloc[-31:-1].max())
                        _rd_vol   = float(_df_rd["Volume"].iloc[-1])
                        _rd_avgv  = float(_df_rd["Volume"].iloc[-21:-1].mean())
                        _rd_range = _rd_high - _rd_low
                        _rd_pos   = (_rd_close - _rd_low) / _rd_range if _rd_range > 0 else 1.0
                        if (_rd_high > _rd_30h                         # new 30-day intraday high
                                and _rd_pos <= 0.25                    # closes in bottom 25% of range
                                and _rd_avgv > 0
                                and _rd_vol > _rd_avgv * 1.4):         # above-avg volume = conviction
                            _log.warning(
                                "[monitor] %s REVERSAL DAY: intraday high $%.2f > 30d high $%.2f, "
                                "close in bottom 25%% of range on %.1f× avg vol (pnl=+%.1f%%)",
                                symbol, _rd_high, _rd_30h, _rd_vol / _rd_avgv, pnl_pct * 100)
                            # Raise stop to breakeven if not already protected
                            if not sym.get("breakeven_done"):
                                _rd_rem = qty - sym.get("partial_qty", 0)
                                if _rd_rem > 0:
                                    _cancel_stop_orders(symbol)
                                    _rd_oid = _place_stop(symbol, _rd_rem, round(avg_cost, 2))
                                    if _rd_oid:
                                        sym["breakeven_done"]  = True
                                        sym["stop_order_id"]   = _rd_oid
                                        sym["stop_loss"]       = avg_cost
                                        changed = True
                            try:
                                import requests as _rqrd, os as _osrd
                                _trd = _osrd.getenv("TELEGRAM_BOT_TOKEN", "")
                                _crd = _osrd.getenv("TELEGRAM_CHAT_ID", "")
                                if _trd and _crd:
                                    _rqrd.post(
                                        f"https://api.telegram.org/bot{_trd}/sendMessage",
                                        json={"chat_id": _crd, "parse_mode": "Markdown",
                                              "text": (
                                                  f"⚠️ *Reversal Day — {symbol}*\n"
                                                  f"New 30d intraday high ${_rd_high:.2f} "
                                                  f"but closed in bottom 25%% of range\n"
                                                  f"{_rd_vol/_rd_avgv:.1f}× avg vol — "
                                                  f"distribution signal\n"
                                                  f"Stop raised to breakeven ${avg_cost:.2f}"
                                              )},
                                        timeout=8)
                            except Exception:
                                pass
                except Exception as _rde:
                    _log.debug("[monitor] reversal_day %s: %s", symbol, _rde)

        # ── Step F: Pyramid — add 25% at +4% confirmation ──────────────────────
        # Minervini adds to winners: buy more when the breakout is confirmed
        # Uses same pivot-low stop. Only once, only if heat cap allows.
        # RS gate: only pyramid when stock is still leading the market (RS at/near high)
        _rs_pyr_date = datetime.now(_ET).strftime("%Y-%m-%d")
        if sym.get("_rs_pyr_date") != _rs_pyr_date:
            sym["_rs_pyr_date"] = _rs_pyr_date
            try:
                import yfinance as _yf_pyr
                _s_pyr  = _yf_pyr.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)["Close"]
                _sp_pyr = _yf_pyr.Ticker("SPY").history(period="1y", interval="1d", auto_adjust=True)["Close"]
                if len(_s_pyr) >= 20 and len(_sp_pyr) >= 20:
                    _rs_l = _s_pyr / _sp_pyr.reindex(_s_pyr.index, method="ffill")
                    sym["_rs_at_high"] = float(_rs_l.iloc[-1]) >= float(_rs_l.max()) * 0.98
            except Exception:
                sym["_rs_at_high"] = True
        _rs_ok = sym.get("_rs_at_high", True)
        if (pnl_pct >= 0.04
                and not sym.get("pyramid_done")
                and not sym.get("partial1_done")
                and _rs_ok):
            try:
                from risk_manager import get_state as _prs, check_can_trade
                from broker import place_market_buy, get_account
                _pstate = _prs()
                _pf_val = float(get_account()["portfolio_value"])
                _add_qty = max(1, round(initial_qty * 0.25))
                _stop_l  = sym.get("stop_loss", 0.0)
                if _stop_l > 0 and _stop_l < avg_cost * 0.99:
                    _add_risk = (_add_qty * (cur_price - _stop_l)) / _pf_val
                    _heat_ok, _ = check_can_trade(_pf_val, _add_risk)
                    if _heat_ok and _add_qty >= 1:
                        _pyo = place_market_buy(symbol, _add_qty)
                        if _pyo:
                            sym["pyramid_done"]  = True
                            sym["pyramid_qty"]   = _add_qty
                            sym["pyramid_price"] = cur_price
                            changed = True
                            _log.info("[monitor] 📐 PYRAMID %s — added %d sh @ $%.2f (+%.1f%% from entry)",
                                      symbol, _add_qty, cur_price, pnl_pct * 100)
                            try:
                                import requests as _rq, os as _os
                                tok = _os.getenv("TELEGRAM_BOT_TOKEN", "")
                                cid = _os.getenv("TELEGRAM_CHAT_ID", "")
                                if tok and cid:
                                    _rq.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                             json={"chat_id": cid, "parse_mode": "Markdown",
                                                   "text": (f"📐 *Pyramid — {symbol}*\n"
                                                            f"Added {_add_qty} sh @ ${cur_price:.2f} (+{pnl_pct*100:.1f}%)\n"
                                                            f"Position confirmed — same pivot stop ${_stop_l:.2f}")},
                                             timeout=8)
                            except Exception:
                                pass
            except Exception as _e:
                _log.debug("[monitor] pyramid check %s: %s", symbol, _e)

        # ── Step F2: 10-week MA follow-on buy (O'Neil second add-on) ───────────
        # After partial1 (+10%): if price pulls back to MA10w on drying volume,
        # add 25% — classic second buy point, improves average cost on winners.
        if (sym.get("partial1_done")
                and not sym.get("partial2_done")
                and not sym.get("f2_done")
                and pnl_pct >= 0.05):
            _f2_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_f2_check_date") != _f2_today:
                sym["_f2_check_date"] = _f2_today
                try:
                    import yfinance as _yf_f2
                    _dfw2 = _yf_f2.Ticker(symbol).history(
                        period="6mo", interval="1wk", auto_adjust=True)
                    _dfd2 = _yf_f2.Ticker(symbol).history(
                        period="30d", interval="1d", auto_adjust=True)
                    if len(_dfw2) >= 12 and len(_dfd2) >= 10:
                        _ma10w_f2 = float(_dfw2["Close"].tail(10).mean())
                        _near_10w = abs(cur_price - _ma10w_f2) / _ma10w_f2 <= 0.03
                        _vol5_f2  = float(_dfd2["Volume"].tail(5).mean())
                        _vol20_f2 = float(_dfd2["Volume"].mean())
                        _dryup_f2 = _vol5_f2 < _vol20_f2 * 0.80  # volume contracting = healthy
                        if _near_10w and _dryup_f2:
                            from risk_manager import check_can_trade as _cct_f2
                            from broker import place_market_buy as _pmb_f2, get_account as _ga_f2
                            _pf_f2   = float(_ga_f2()["portfolio_value"])
                            _add_f2  = max(1, round(sym.get("initial_qty", qty) * 0.25))
                            _sl_f2   = sym.get("stop_loss", 0.0)
                            if _sl_f2 > 0 and cur_price > _sl_f2:
                                _arisk_f2 = (_add_f2 * (cur_price - _sl_f2)) / _pf_f2
                                _ok_f2, _ = _cct_f2(_pf_f2, _arisk_f2)
                                if _ok_f2:
                                    _ord_f2 = _pmb_f2(symbol, _add_f2)
                                    if _ord_f2:
                                        sym["f2_done"]  = True
                                        sym["f2_qty"]   = _add_f2
                                        sym["f2_price"] = cur_price
                                        changed = True
                                        _log.info(
                                            "[monitor] 📈 F2 FOLLOW-ON %s — added %d sh @ $%.2f"
                                            " (MA10w $%.2f, vol drying up)",
                                            symbol, _add_f2, cur_price, _ma10w_f2)
                                        try:
                                            import requests as _rf2, os as _of2
                                            _tok = _of2.getenv("TELEGRAM_BOT_TOKEN", "")
                                            _cid = _of2.getenv("TELEGRAM_CHAT_ID", "")
                                            if _tok and _cid:
                                                _rf2.post(
                                                    f"https://api.telegram.org/bot{_tok}/sendMessage",
                                                    json={"chat_id": _cid, "parse_mode": "Markdown",
                                                          "text": (f"📈 *F2 Follow-On — {symbol}*\n"
                                                                   f"Pulled back to MA10w ${_ma10w_f2:.2f}, vol drying up\n"
                                                                   f"Added {_add_f2} sh @ ${cur_price:.2f}")},
                                                    timeout=8)
                                        except Exception:
                                            pass
                except Exception as _ef2:
                    _log.debug("[monitor] F2 check %s: %s", symbol, _ef2)

        # ── Step E: Climax run / parabolic exit ────────────────────────────────
        # If stock has moved ≥ 25% in the last 5 trading days AND we see 3 up-days
        # in a row → climax run. Minervini sells into strength, not at the stop.
        if (not sym.get("climax_exited")
                and not sym.get("partial_done")
                and pnl_pct >= 0.25):
            try:
                import yfinance as _yf
                _df5 = _yf.Ticker(symbol).history(
                    period="10d", interval="1d", auto_adjust=True)
                if len(_df5) >= 5:
                    _c5 = _df5["Close"]
                    _v5 = _df5["Volume"]
                    _vol_avg20 = float(_yf.Ticker(symbol).history(
                        period="30d", interval="1d")["Volume"].mean())
                    # 3 consecutive up-days AND last-day volume > 1.5× 20-day avg
                    _three_up = all(
                        _c5.iloc[i] > _c5.iloc[i-1]
                        for i in range(-3, 0)
                    )
                    _vol_surge = (float(_v5.iloc[-1]) > _vol_avg20 * 1.5
                                  if _vol_avg20 > 0 else False)
                    if _three_up and _vol_surge:
                        remaining = qty - sym.get("partial_qty", 0)
                        _log.warning(
                            "[monitor] CLIMAX RUN %s — +%.1f%% in 5d, 3 up-days, vol surge. "
                            "Selling into strength (%d sh).",
                            symbol, pnl_pct * 100, remaining)
                        _cancel_stop_orders(symbol)
                        if remaining > 0 and _place_market_sell(symbol, remaining):
                            sym["climax_exited"] = True
                            changed = True
                            try:
                                import requests as _req, os as _os
                                tok = _os.getenv("TELEGRAM_BOT_TOKEN", "")
                                cid = _os.getenv("TELEGRAM_CHAT_ID", "")
                                if tok and cid:
                                    _req.post(
                                        f"https://api.telegram.org/bot{tok}/sendMessage",
                                        json={"chat_id": cid, "parse_mode": "Markdown",
                                              "text": (f"🚀 *Climax Run Exit — {symbol}*\n"
                                                       f"3 up-days + volume surge at +{pnl_pct*100:.1f}%\n"
                                                       f"Selling into strength — Minervini rule")},
                                        timeout=8,
                                    )
                            except Exception:
                                pass
            except Exception as _e:
                _log.debug("[monitor] Climax check %s failed: %s", symbol, _e)

        # ── Step W: Weekly close rule — exit if weekly bar closes below MA10w ──────
        # O'Neil: a close below the 10-week MA is institutional selling — loss of trend
        # Use last completed weekly bar (iloc[-2]) to avoid reacting to intraweek noise.
        if (not sym.get("weekly_close_exited")
                and not sym.get("climax_exited")
                and pnl_pct > 0):
            _wc_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_wc_check_date") != _wc_today:
                sym["_wc_check_date"] = _wc_today
                try:
                    import yfinance as _yf_wc
                    _dfw = _yf_wc.Ticker(symbol).history(
                        period="6mo", interval="1wk", auto_adjust=True)
                    if len(_dfw) >= 12:
                        _w_close = float(_dfw["Close"].iloc[-2])   # last completed week
                        _ma10w   = float(_dfw["Close"].iloc[-12:-2].mean())  # MA10w of prior 10
                        if _w_close < _ma10w:
                            _rem_wc = qty - sym.get("partial_qty", 0)
                            _log.warning(
                                "[monitor] %s WEEKLY CLOSE BELOW MA10w ($%.2f < MA10w $%.2f) — exiting",
                                symbol, _w_close, _ma10w)
                            _cancel_stop_orders(symbol)
                            if _rem_wc > 0 and _place_market_sell(symbol, _rem_wc):
                                sym["weekly_close_exited"] = True
                                changed = True
                                try:
                                    import requests as _rwc, os as _owc
                                    _tok = _owc.getenv("TELEGRAM_BOT_TOKEN", "")
                                    _cid = _owc.getenv("TELEGRAM_CHAT_ID", "")
                                    if _tok and _cid:
                                        _rwc.post(
                                            f"https://api.telegram.org/bot{_tok}/sendMessage",
                                            json={"chat_id": _cid, "parse_mode": "Markdown",
                                                  "text": (f"📉 *Weekly Close Exit — {symbol}*\n"
                                                           f"Weekly close ${_w_close:.2f} below MA10w ${_ma10w:.2f}\n"
                                                           f"O'Neil institutional selling signal")},
                                            timeout=8)
                                except Exception:
                                    pass
                except Exception as _we:
                    _log.debug("[monitor] weekly close %s: %s", symbol, _we)

    # ── Telegram proximity alerts (once per day per position) ────────────────
    for _sym_a, _sym_d in state.items():
        if _sym_a not in {p["symbol"] for p in positions}:
            continue
        _alert_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if _sym_d.get("_alert_date") == _alert_today:
            continue
        _sym_d["_alert_date"] = _alert_today
        _alerts = []
        _ac    = float(_sym_d.get("avg_cost", 0) or 0)
        _lp    = float(_sym_d.get("last_price", _ac) or _ac)
        _pnl   = (_lp - _ac) / _ac if _ac > 0 else 0.0
        _sl    = float(_sym_d.get("stop_loss", 0) or 0)
        # Alert 1: position within 2% of stop — danger zone
        if _sl > 0 and _lp > 0 and _lp < _sl * 1.02:
            _margin = (_lp / _sl - 1) * 100
            _alerts.append(f"⚠️ *{_sym_a}* near stop ${_sl:.2f} — {_margin:+.1f}% above")
        # Alert 2: approaching B1 (+10%) partial trigger
        if not _sym_d.get("partial1_done") and 0.07 <= _pnl < 0.10:
            _alerts.append(f"🎯 *{_sym_a}* approaching +10%% target (now {_pnl*100:+.1f}%%)")
        # Alert 3: earnings within 3–7 days — review protection
        _de = _sym_d.get("days_to_earnings")
        if _de is not None and 3 <= _de <= 7:
            _alerts.append(f"📅 *{_sym_a}* earnings in {_de} days — check protection")
        # Alert 4: RS line divergence — stock at/near new high but RS declining
        if _pnl >= 0.05 and not _sym_d.get("_rs_div_alerted"):
            try:
                import yfinance as _yf_div
                _sd = _yf_div.Ticker(_sym_a).history(period="3mo", interval="1d", auto_adjust=True)["Close"]
                _bd = _yf_div.Ticker("SPY").history(period="3mo", interval="1d", auto_adjust=True)["Close"]
                if len(_sd) >= 20 and len(_bd) >= 20:
                    _rs_d = _sd / _bd.reindex(_sd.index, method="ffill")
                    _price_hi = _lp >= float(_sd.max()) * 0.97   # near 3mo high
                    _rs_weak  = float(_rs_d.iloc[-1]) < float(_rs_d.tail(20).max()) * 0.95
                    if _price_hi and _rs_weak:
                        _sym_d["_rs_div_alerted"] = _alert_today
                        _alerts.append(f"📉 *{_sym_a}* RS divergence — price at high but RS declining")
            except Exception:
                pass
        if _alerts:
            try:
                import requests as _rqa, os as _osa
                _tok = _osa.getenv("TELEGRAM_BOT_TOKEN", "")
                _cid = _osa.getenv("TELEGRAM_CHAT_ID", "")
                if _tok and _cid:
                    for _alert_msg in _alerts:
                        _rqa.post(f"https://api.telegram.org/bot{_tok}/sendMessage",
                                  json={"chat_id": _cid, "parse_mode": "Markdown",
                                        "text": _alert_msg},
                                  timeout=5)
                        changed = True
            except Exception:
                pass

    # ── Stale position review (>30 trading days, no major exit milestone) ─────────
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
            _tok = _ost.getenv("TELEGRAM_BOT_TOKEN", "")
            _cid = _ost.getenv("TELEGRAM_CHAT_ID", "")
            if _tok and _cid:
                _cur_p  = next((p for p in positions if p["symbol"] == _sym_st), None)
                _pnl_st = float(_cur_p.get("unrealized_plpc", 0)) * 100 if _cur_p else 0.0
                _days_st = _trading_days_held(_ed_st)
                _rqst.post(f"https://api.telegram.org/bot{_tok}/sendMessage",
                           json={"chat_id": _cid, "parse_mode": "Markdown",
                                 "text": (f"⏳ *Stale Position — {_sym_st}*\n"
                                          f"Held {_days_st} trading days • P&L {_pnl_st:+.1f}%%\n"
                                          f"Review: is the VCP thesis still valid?")},
                           timeout=5)
                changed = True
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
                        _r_val = ((float(sym_data.get("last_price", 0))
                                   - float(sym_data.get("avg_cost", 0)))
                                  / max(float(sym_data.get("avg_cost", 0))
                                        - float(sym_data.get("stop_loss_initial", 0)), 0.001))
                        for _sig2 in _sig_names2:
                            if _sig2 in _sa2 and sym_data.get(_sig2):
                                if _won:
                                    _sa2[_sig2]["wins"]   += 1
                                else:
                                    _sa2[_sig2]["losses"] += 1
                                _sa2[_sig2]["total_r"] = round(
                                    _sa2[_sig2].get("total_r", 0.0) + _r_val, 3)
                        open(_sa_path, "w").write(_jsa2.dumps(_sa2, indent=2))
                except Exception:
                    pass
                # Re-entry cooldown: block stop-outs from re-entering for 5 trading days
                if pnl_pct < 0:
                    record_stop_out(sym)
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
