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
from notifications import _tg
import yfinance as yf

_log = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_MARKET_OPEN  = dt_time(9, 30)
_MARKET_CLOSE = dt_time(16, 0)

_STATE_FILE = os.path.join(os.path.dirname(__file__), "logs", "monitor_state.json")

_sync_fail_count = 0  # consecutive sync failures — Telegram alert fires at 2+
_JOURNAL_LOCK = threading.Lock()  # serialises concurrent postmortem journal writes


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
                state = json.load(f)
            # Schema check: state must be a dict keyed by symbol (str → dict).
            # A corrupted file (e.g. "positions": []) would silently lose all tracking.
            if not isinstance(state, dict):
                _log.error("[monitor] monitor_state.json is not a dict — resetting "
                           "(was: %s)", type(state).__name__)
                return {}
            # Prune any non-dict values that slipped in
            bad = [k for k, v in state.items() if not isinstance(v, dict)]
            if bad:
                _log.warning("[monitor] Removing %d malformed state entries: %s", len(bad), bad)
                for k in bad:
                    del state[k]
            return state
    except Exception:
        _log.debug("[%s] suppressed", __name__, exc_info=True)
    return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        _tmp = _STATE_FILE + ".tmp"
        with open(_tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(_tmp, _STATE_FILE)  # atomic on POSIX
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


def _place_limit_sell(symbol: str, qty: int, limit_price: float) -> str | None:
    """Limit sell for partial exits. Returns Alpaca order ID, 'market' if fell back, or None on failure."""
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
        order_id = r.json().get("id", "")
        _log.info("[monitor] Limit sell %d × %s @ $%.2f submitted id=%s", qty, symbol, limit_price, order_id)
        return order_id or "limit_unknown"
    except Exception as e:
        _log.warning("[monitor] limit_sell(%s, %d) error: %s — falling back to market", symbol, qty, e)
        return "market" if _place_market_sell(symbol, qty) else None


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
    """Return True if the Alpaca order exists and is still open/pending.
    Returns True on transient network/API errors to avoid GTC order churn.
    Only returns False for definitive 404 (order genuinely missing).
    """
    if not order_id:
        return False
    try:
        r = requests.get(
            f"{_alpaca_base()}/orders/{order_id}",
            headers=_alpaca_headers(), timeout=8
        )
        if r.status_code == 404:
            return False
        if not r.ok:
            # P0-fix: transient API error — assume alive to avoid cancelling valid GTC order
            _log.debug("[monitor] _stop_order_alive(%s): HTTP %d — assuming alive", order_id, r.status_code)
            return True
        data = r.json()
        return data.get("status") in ("new", "accepted", "pending_new", "held")
    except requests.exceptions.RequestException:
        # P0-fix: network error — assume order still live, retry next cycle
        _log.debug("[monitor] _stop_order_alive(%s): network error — assuming alive", order_id)
        return True
    except Exception:
        _log.debug("[monitor] _stop_order_alive(%s): unexpected error — assuming alive", order_id)
        return True


# ── Core monitoring logic ─────────────────────────────────────────────────────

def _infer_exit_step(sym_data: dict) -> str:
    """Infer primary exit step from state flags — avoids scattered exit_step assignments."""
    if sym_data.get("slippage_exited"):        return "slippage_close"
    if sym_data.get("max_loss_exited"):       return "max_loss_cap"
    if sym_data.get("weekly_close_exited"):   return "W_weekly_close"
    if sym_data.get("earnings_closed"):       return "earnings_close"
    if sym_data.get("iv_crush_exited"):       return "IV_crush"
    if sym_data.get("failed_breakout_done"):  return "Z_failed_breakout"
    if sym_data.get("lhll_stopped"):          return "LHLL_reversal"
    if sym_data.get("climax_exit_done"):      return "V_climax"
    if sym_data.get("time_stopped"):          return "D_time_stop"
    if sym_data.get("max_hold_exited"):       return "max_hold"
    if sym_data.get("gap_harvest_done"):      return "G_gap_harvest"
    if sym_data.get("partial2_done"):         return "B2_partial"
    if sym_data.get("partial1_done"):         return "B1_partial"
    return "stop"


def _journal_trade(symbol: str, sym_data: dict, pnl_pct: float, portfolio_value: float) -> None:
    """Append completed trade record to logs/trade_journal.jsonl."""
    import json as _json
    avg_cost    = sym_data.get("avg_cost", 0)
    last_price  = sym_data.get("last_price", avg_cost)
    initial_qty = sym_data.get("initial_qty", 0)
    partial_qty = sym_data.get("partial_qty", 0)
    _pyr_qty = (sym_data.get("pyramid_qty", 0) + sym_data.get("step_f_qty", 0) +
                sym_data.get("step_f2_qty", 0) + sym_data.get("step_e_qty", 0))
    exit_qty    = initial_qty + _pyr_qty - partial_qty
    pnl_dollar  = (last_price - avg_cost) * exit_qty if avg_cost > 0 else 0.0
    stop_loss      = sym_data.get("stop_loss", 0.0)
    risk_per_share = (avg_cost - stop_loss) if stop_loss > 0 else avg_cost * 0.07
    r_multiple = (last_price - avg_cost) / risk_per_share if risk_per_share > 0 else 0.0
    buy_stop     = sym_data.get("buy_stop", 0.0)
    slippage_pct = round((avg_cost - buy_stop) / buy_stop * 100, 2) if buy_stop > 0 else 0.0
    _add = []
    if sym_data.get("step_f_done"):  _add.append("F")
    if sym_data.get("step_f2_done"): _add.append("F2")
    if sym_data.get("pyramid_done"): _add.append("P")
    days_held = _trading_days_held(sym_data.get("entry_date", ""))

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
        "exit_step":       _infer_exit_step(sym_data),
        "add_steps":       ",".join(_add),
        "slippage_pct":    slippage_pct,
        "days_held":       days_held,
    }
    journal = os.path.join(os.path.dirname(__file__), "logs", "trade_journal.jsonl")
    try:
        os.makedirs(os.path.dirname(journal), exist_ok=True)
        with open(journal, "a") as jf:
            jf.write(_json.dumps(entry) + "\n")
        _log.info("[monitor] Trade journaled: %s pnl=%.1f%% (%.1fR)",
                  symbol, pnl_pct * 100, r_multiple)
        _tg((
                              f"{_sign_jt} *{symbol}* {pnl_pct*100:+.1f}%"
                              f" ({r_multiple:+.1f}R) via {entry['exit_step']}"
                              f" | {entry['days_held']}d"
                          ))
    except Exception as e:
        _log.warning("[monitor] Journal write failed: %s", e)


def _run_postmortem(symbol: str, sym_data: dict, pnl_pct: float) -> None:
    """Call Haiku for a 2-sentence post-trade analysis. Updates the last journal entry."""
    try:
        import anthropic as _ant
        from config import ANTHROPIC_API_KEY as _ant_key, CLAUDE_MODEL as _haiku_model
        _client = _ant.Anthropic(api_key=_ant_key, timeout=15.0)

        _hist = yf.Ticker(symbol).history(period="30d", interval="1d", auto_adjust=True)
        if _hist.empty:
            return

        avg_cost   = float(sym_data.get("avg_cost", 0) or 0)
        entry_date = sym_data.get("entry_date", "")
        exit_step  = _infer_exit_step(sym_data)
        days_held  = _trading_days_held(entry_date)
        mfe_pct    = round(float(sym_data.get("mfe_pct", 0) or 0) * 100, 1)
        mae_pct    = round(float(sym_data.get("mae_pct", 0) or 0) * 100, 1)
        _add = []
        if sym_data.get("step_f_done"):  _add.append("F")
        if sym_data.get("step_f2_done"): _add.append("F2")
        if sym_data.get("pyramid_done"): _add.append("P")

        _rows = []
        for _dt, _row in _hist.tail(15).iterrows():
            _rows.append(f"{str(_dt)[:10]}  C={_row['Close']:.2f}  V={int(_row['Volume']/1000)}K")

        _prompt = (
            f"Trade post-mortem: {symbol}\n"
            f"Entry avg: ${avg_cost:.2f} | Entry: {entry_date} | Days held: {days_held}\n"
            f"P&L: {pnl_pct*100:+.1f}% | MFE: +{mfe_pct}% | MAE: {mae_pct}%\n"
            f"Exit reason: {exit_step} | Add-ons: {','.join(_add) or 'none'}\n\n"
            f"Last 15 daily closes:\n" + "\n".join(_rows) + "\n\n"
            f"In exactly 2 sentences: (1) what went right or wrong with this trade, "
            f"(2) what could have been managed better. Be specific and actionable."
        )
        _resp = _client.messages.create(
            model=_haiku_model,
            max_tokens=200,
            messages=[{"role": "user", "content": _prompt}],
        )
        postmortem = _resp.content[0].text.strip()

        _jpath = os.path.join(os.path.dirname(__file__), "logs", "trade_journal.jsonl")
        if os.path.exists(_jpath):
            with _JOURNAL_LOCK:  # P0-fix: serialise concurrent postmortem writes
                with open(_jpath, "r") as _jf:
                    _lines = _jf.readlines()
                for _i in range(len(_lines) - 1, -1, -1):
                    try:
                        _entry = json.loads(_lines[_i])
                        if _entry.get("symbol") == symbol:
                            _entry["ai_postmortem"] = postmortem
                            _lines[_i] = json.dumps(_entry) + "\n"
                            break
                    except Exception:
                        continue
                _tmp = _jpath + ".tmp"
                with open(_tmp, "w") as _jf:
                    _jf.writelines(_lines)
                os.replace(_tmp, _jpath)
                _log.info("[monitor] Post-mortem %s: %s", symbol, postmortem[:80])
    except Exception as _e:
        _log.debug("[monitor] postmortem failed %s: %s", symbol, _e)


def _trading_days_held(entry_date_str: str) -> int:
    """Return US trading days since entry_date.
    Uses NYSE calendar (pandas_market_calendars) — correctly excludes weekends
    AND US market holidays (MLK Day, Memorial Day, Good Friday, etc.).
    Falls back to BDay if library unavailable.
    """
    try:
        import pandas as _pd
        entry = _pd.Timestamp(entry_date_str).date()
        today = datetime.now(_ET).date()
        if entry >= today:
            return 0
        try:
            import pandas_market_calendars as _mcal
            nyse = _mcal.get_calendar("NYSE")
            schedule = nyse.schedule(start_date=str(entry), end_date=str(today))
            # subtract 1: entry day itself is day 0
            return max(0, len(schedule) - 1)
        except Exception:
            # Fallback: BDay approximation
            from pandas.tseries.offsets import BDay
            delta = _pd.Timestamp(today) - _pd.Timestamp(entry)
            return max(0, int(delta / BDay(1)))
    except Exception:
        return 0


from config import SECTOR_ETF_MAP as _SECTOR_ETF_PM
_sector_etf_cache: dict[str, tuple[bool, float]] = {}
_weekly_cache: dict = {}  # sym → (df, fetch_timestamp)


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
        _log.debug("[%s] suppressed", __name__, exc_info=True)
    _regime_ts_pm = _time_r.time()
    return _cached_regime_pm


def _get_weekly_hist(sym: str):
    """Return weekly OHLCV for sym (6-month history), cached per symbol with 4-hour TTL."""
    import time as _tw
    global _weekly_cache
    _now = _tw.time()
    if sym in _weekly_cache and _now - _weekly_cache[sym][1] < 14400:
        return _weekly_cache[sym][0]
    try:
        _df = yf.Ticker(sym).history(period="6mo", interval="1wk", auto_adjust=True)
        _weekly_cache[sym] = (_df, _now)
        return _df
    except Exception:
        import pandas as _pdw
        return _pdw.DataFrame()


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
            _log.debug("[%s] suppressed", __name__, exc_info=True)
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

        state       = _grs()
        daily_pnl   = state.get("daily_pnl_pct", 0.0)
        today_str   = str(_date.today())
        alerted_set = _drawdown_alerted.setdefault(today_str, set())

        halt_pct = _risk_cfg.get("max_daily_loss_pct", 0.04)
        warn_pct = halt_pct / 2

        if daily_pnl <= -halt_pct and "4pct" not in alerted_set:
            alerted_set.add("4pct")
            pct_str  = f"{daily_pnl*100:.1f}%"
            halt_str = f"{halt_pct*100:.0f}%"
            _tg(
                "\U0001F6A8 *DAILY HALT " + pct_str + "*\n"
                "Daily loss limit reached -- risk_manager blocks new trades.\n"
                "Portfolio at max drawdown (" + halt_str + ") for today."
            )

        elif daily_pnl <= -warn_pct and "2pct" not in alerted_set:
            alerted_set.add("2pct")
            pct_str  = f"{abs(daily_pnl)*100:.1f}%"
            halt_str = f"{halt_pct*100:.0f}%"
            _tg(
                "\u26A0\uFE0F *Drawdown Warning -" + pct_str + "*\n"
                "Portfolio down " + pct_str + " today -- "
                "halfway to " + halt_str + " daily halt.\n"
                "Review open positions and tighten stops."
            )
    except Exception as _e_dd:
        import logging
        logging.getLogger(__name__).debug("[monitor] drawdown check error: %s", _e_dd)


def _step_d_time_stop(sym: dict, symbol: str, qty: int, pnl_pct: float,
                      time_stop_days: int, days_held: int, pead_active: bool,
                      soft_dd_mode: bool, cfg: dict) -> bool:
    """P5.2: Time stop — Minervini 3-4 week rule. Returns True if position closed."""
    tsg = cfg.get("time_stop_min_gain_pct", 0.02)
    tsd = time_stop_days
    if soft_dd_mode:
        tsd = max(int(tsd * 0.7), 10)
        tsg = max(tsg, 0.04)
    if not sym.get("entry_date", ""):
        return False
    if (sym.get("partial_done") or sym.get("time_stopped")
            or sym.get("max_loss_exited") or pead_active):
        return False
    if days_held < tsd or pnl_pct >= tsg:
        return False
    _rem = qty - sym.get("partial_qty", 0)
    _log.warning("[monitor] %s TIME STOP: day %d pnl=%.1f%% (< %.0f%%) — closing",
                 symbol, days_held, pnl_pct * 100, tsg * 100)
    _cancel_stop_orders(symbol)
    if _rem > 0 and _place_market_sell(symbol, _rem):
        sym["time_stopped"] = True
        _tg(f"⏳ *Time Stop — {symbol}*\n"
            f"Held {days_held} days | P&L {pnl_pct*100:+.1f}%\n"
            f"No momentum — Minervini time rule")
        return True
    return False


def _auto_reconcile_stale_state() -> None:
    """
    Auto-clean risk_state + monitor_state after ≥5 consecutive sync failures.

    If Alpaca has returned 0 positions for 75+ minutes it's almost certainly not
    an API glitch — the position was genuinely closed.  This writes a forced
    journal entry, removes the stale entry from both state files, and alerts.
    Called only from the SyncError handler; never during a normal cycle.
    """
    _log.warning("[monitor] AUTO-RECONCILE triggered — cleaning stale state after persistent sync failure")
    try:
        from position_sync import (_fetch_alpaca_state, _load_risk, _save_risk,
                                   _load_monitor, _save_monitor)
        try:
            _pos, _orders = _fetch_alpaca_state()
        except Exception as _fe:
            _log.error("[monitor] AUTO-RECONCILE: cannot reach Alpaca — aborting: %s", _fe)
            return

        _held  = {p["symbol"] for p in _pos}
        _risk  = _load_risk()
        _mon   = _load_monitor()
        stale  = [s for s in list(_risk.get("positions_risk", {}).keys()) if s not in _held]

        if not stale:
            _log.info("[monitor] AUTO-RECONCILE: no stale symbols found — state already clean")
            return

        for sym in stale:
            sym_data   = _mon.pop(sym, {})
            avg_cost   = float(sym_data.get("avg_cost", 0))
            last_price = float(sym_data.get("last_price", avg_cost))
            pnl_pct    = ((last_price - avg_cost) / avg_cost) if avg_cost > 0 else 0.0
            try:
                _journal_trade(sym, sym_data, pnl_pct, 0)
            except Exception as _je:
                _log.warning("[monitor] AUTO-RECONCILE: journal write failed for %s: %s", sym, _je)
            _risk["positions_risk"].pop(sym, None)
            _log.warning("[monitor] AUTO-RECONCILE: removed stale %s "
                         "(last_price=%.2f pnl=%.1f%%)", sym, last_price, pnl_pct * 100)

        _risk["open_risk_pct"] = round(sum(_risk.get("positions_risk", {}).values()), 4)
        _save_risk(_risk)
        _save_monitor(_mon)
        _tg(f"🔧 *Three Masters — Auto-Reconcile*\n"
            f"Stale positions forcibly closed after 5+ failed sync cycles:\n"
            f"`{stale}`\nState cleaned — bot resumes normal operation.")
        _log.warning("[monitor] AUTO-RECONCILE complete: removed %s heat=%.1f%%",
                     stale, _risk["open_risk_pct"] * 100)
    except Exception as _ae:
        _log.error("[monitor] AUTO-RECONCILE failed unexpectedly: %s", _ae, exc_info=True)


def check_positions() -> None:
    """Run one monitoring cycle. Called every 15 min during market hours."""
    if not _market_is_open():
        return

    _check_drawdown_proximity()

    # Sync MUST succeed — never manage positions with unverified state.
    # SyncError means Alpaca is unreachable: skip this cycle entirely.
    global _sync_fail_count
    from position_sync import sync_all, SyncError
    try:
        sync_all()
        _sync_fail_count = 0   # reset on success
    except SyncError as e:
        _sync_fail_count += 1
        _log.error("[monitor] SYNC FAILED (consecutive=%d) — skipping cycle: %s",
                   _sync_fail_count, e)
        # Alert on 2nd consecutive failure (not every tick — avoids spam)
        if _sync_fail_count >= 2:
            try:
                _tg(f"🚨 *Three Masters — Monitor sync FAILED ×{_sync_fail_count}*\n"
                    f"`{e}`\nPositions NOT managed for {_sync_fail_count} cycles.")
            except Exception:
                _log.debug("[%s] suppressed", __name__, exc_info=True)
        # Auto-reconcile after 5 consecutive failures (≈75 min): at this point
        # the empty-state guard has been firing repeatedly, which means Alpaca
        # genuinely has 0 positions while bot state still shows open ones.
        # Clean up automatically so manual intervention is not required.
        if _sync_fail_count >= 5:
            _auto_reconcile_stale_state()
            _sync_fail_count = 0
        return   # skip entire monitoring cycle — do NOT touch orders

    try:
        positions = _get_positions()
    except AlpacaConnectionError as e:
        _log.error("[monitor] POSITIONS UNAVAILABLE - skipping entire cycle: %s", e)
        try:
            _tg("🚨 *Three Masters — Monitor: can't fetch positions*\n"
                f"`{e}`\nCycle skipped — positions NOT managed this tick.")
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        return
    if not positions:
        return

    # ── Pre-fetch 90-day OHLCV for all positions once per cycle ──────────────
    # Without this, each position triggers 7+ individual yfinance calls per cycle:
    # 8 positions × 7 calls × 4 cycles/hr = 224 calls/hr = 5,376/day.
    # A single batch download + in-memory slice cuts this to ~32 calls/day.
    _price_cache: dict = {}
    try:
        _batch_syms = [p["symbol"] for p in positions]
        if _batch_syms:
            _raw_batch = yf.download(
                _batch_syms, period="90d", interval="1d",
                auto_adjust=True, progress=False, group_by="ticker", threads=True
            )
            if len(_batch_syms) == 1:
                if not _raw_batch.empty:
                    _price_cache[_batch_syms[0]] = _raw_batch
            elif not _raw_batch.empty:
                for _bs in _batch_syms:
                    try:
                        _df_bs = _raw_batch[_bs].dropna(how="all")
                        if not _df_bs.empty:
                            _price_cache[_bs] = _df_bs
                    except Exception:
                        pass
    except Exception as _e_batch:
        _log.debug("[monitor] price batch pre-fetch failed: %s", _e_batch)

    def _get_hist(sym: str):
        """Return cached 90-day OHLCV, or fetch fresh if cache missed for this symbol."""
        if sym in _price_cache:
            return _price_cache[sym]
        try:
            return yf.Ticker(sym).history(period="90d", interval="1d", auto_adjust=True)
        except Exception:
            import pandas as _pd_miss
            return _pd_miss.DataFrame()

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
        _log.debug("[%s] suppressed", __name__, exc_info=True)
    state = _load_state()
    changed = False

    # Retroactively tighten stops when soft-DD activates: existing positions that
    # already have stops placed at a wider trail need to be updated NOW, not only
    # when their per-position logic next runs. Without this, a 7% trailing stop
    # set 3 hours ago stays in place even though the portfolio is bleeding.
    if _soft_dd_mode:
        _dd_today = datetime.now(_ET).strftime("%Y-%m-%d")
        for _sdd_pos in positions:
            _sdd_sym  = _sdd_pos["symbol"]
            _sdd_sym_state = state.get(_sdd_sym, {})
            if (_sdd_sym_state.get("trailing_stop_placed")
                    and not _sdd_sym_state.get("stop_tightened_for_dd")
                    and _sdd_sym_state.get("_dd_tighten_date") != _dd_today):
                _sdd_qty  = int(float(_sdd_pos["qty"]))
                _sdd_cost = float(_sdd_pos["avg_entry_price"])
                _sdd_cur  = float(_sdd_pos["current_price"])
                _sdd_pnl  = (_sdd_cur - _sdd_cost) / _sdd_cost if _sdd_cost > 0 else 0
                if _sdd_pnl > 0:  # only tighten winning positions, don't move stops on losers
                    _sdd_remaining = _sdd_qty - _sdd_sym_state.get("partial_qty", 0)
                    if _sdd_remaining > 0:
                        _cancel_stop_orders(_sdd_sym)
                        _sdd_oid = _place_trailing_stop(_sdd_sym, _sdd_remaining, 0.05)
                        if _sdd_oid:
                            _sdd_sym_state["stop_order_id"]         = _sdd_oid
                            _sdd_sym_state["stop_tightened_for_dd"] = True
                            _sdd_sym_state["_dd_tighten_date"]      = _dd_today
                            changed = True
                            _log.info(
                                "[monitor] %s SOFT-DD retroactive tighten: "
                                "stop updated to 5%% trailing (pnl=+%.1f%%)",
                                _sdd_sym, _sdd_pnl * 100
                            )

    # P2-fix: fetch SPY close once per cycle — reused in Step F and RS divergence
    _spy_close_cycle = None
    try:
        _spy_close_cycle = yf.Ticker("SPY").history(
            period="90d", interval="1d", auto_adjust=True)["Close"]
    except Exception:
        _log.debug("[monitor] SPY pre-fetch failed — will retry per-step")

    for pos in positions:
        symbol    = pos["symbol"]
        qty       = int(float(pos["qty"]))
        avg_cost  = float(pos["avg_entry_price"])
        cur_price = float(pos["current_price"])

        if avg_cost <= 0 or qty <= 0:
            continue

        # P0-fix: skip position while an emergency market sell is pending at open
        if state.get(symbol, {}).get("emergency_exit_submitted"):
            _log.info("[monitor] %s emergency exit pending — skipping cycle to avoid double-sell", symbol)
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
        # P2-fix: compute once and reuse — was called 10+ times per symbol per cycle
        _days_held = _trading_days_held(sym.get("entry_date", ""))

        # Stock split detection: if Alpaca qty diverges significantly from our recorded qty,
        # a corporate action (split / reverse-split) likely occurred. Alert and rescale stop.
        _recorded_qty = sym.get("initial_qty", 0)
        if _recorded_qty > 0 and not sym.get("split_detected"):
            _pyramid_qty = sym.get("pyramid_qty", 0)
            _expected_qty = _recorded_qty + _pyramid_qty
            if _expected_qty > 0:
                _qty_ratio = qty / _expected_qty
                if _qty_ratio >= 1.4 or _qty_ratio <= 0.65:  # catches 1.5× splits and 2:3 reverse
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
                    _tg((
                                          f"⚠️ *Stock Split Detected — {symbol}*\n"
                                          f"Qty {_expected_qty} → {qty} (ratio {_qty_ratio:.2f}x)\n"
                                          f"Stop adjusted: ${_old_sl:.2f} → ${sym.get('stop_loss', 0):.2f}\n"
                                          "Please verify position parameters."
                                      ))
        # On first encounter: check intraday breakout volume confirmation.
        # Minervini rule: breakout on < 1.0× avg volume = false breakout, exit fast.
        if not sym.get("_vol_checked"):
            sym["_vol_checked"] = True
            try:
                _dfv = _get_hist(symbol)
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
                        _tg((
                                              "\u26a0\ufe0f *Weak Vol Breakout \u2014 " + symbol + "*\n"
                                              + f"Volume {_vol_ratio:.1f}x avg (< 1.0x)\n"
                                              + "Stop tightened to -3% on first weakness"
                                          ))
            except Exception as _ve:
                _log.debug("[monitor] vol check %s: %s", symbol, _ve)

        # Load (or reload) VCP metadata once per trading day so the monitor always
        # uses the latest daily-report values for stop, quality, and composite score.
        _today_str = datetime.now(_ET).strftime("%Y-%m-%d")
        if not sym.get("_meta_loaded") or sym.get("_meta_date") != _today_str:
            meta = _lookup_position_metadata(symbol)
            _first_load = not sym.get("_meta_loaded")
            sym["_meta_loaded"]       = True
            sym["_meta_date"]         = _today_str
            # Only set stop levels on first load — subsequent daily refreshes must not
            # overwrite stops that have been ratcheted up by the trailing-stop logic.
            if _first_load:
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
                _df_atr_m = _get_hist(symbol)
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
                    _cancel_stop_orders(symbol)   # avoid orphan GTC stop → unintended short
                    _place_market_sell(symbol, qty)
                    sym["slippage_exited"] = True
                    changed = True
                elif _slip > 0.01:
                    _log.warning("[monitor] %s slippage >1%% (%.1f%%) — "
                                 "fill=$%.2f planned=$%.2f",
                                 symbol, _slip * 100, avg_cost, _planned)
            # Breakout volume validation: if fill-day volume < 1.5x 60-day avg, tighten stop
            # Low-volume breakouts have 3x higher failure rate — Minervini hard rule
            try:
                _bv_df = _get_hist(symbol)
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
                _log.debug("[%s] suppressed", __name__, exc_info=True)
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
                    _tg((
                                          f"\u26a0\ufe0f *Stop Missing — {symbol}*\n"
                                          f"GTC stop not found on Alpaca\n"
                                          f"{_status}"
                                      ))
            except Exception as _sve:
                _log.debug("[monitor] stop_revalidation %s: %s", symbol, _sve)

        # MAE/MFE: track worst (most adverse) and best (most favorable) excursion per position
        sym["mae_pct"] = min(sym.get("mae_pct", pnl_pct), pnl_pct)
        sym["mfe_pct"] = max(sym.get("mfe_pct", pnl_pct), pnl_pct)

        # P1-fix: reset hwm_tightened when stock rallies to a new MFE peak (+4% above trigger)
        # so the stop can ratchet again if the stock continues higher after a shakeout
        if (sym.get("hwm_tightened")
                and sym.get("hwm_mfe_at_trigger", 0) > 0
                and sym.get("mfe_pct", 0) > sym["hwm_mfe_at_trigger"] + 0.04):
            sym["hwm_tightened"] = False
            _log.info("[monitor] %s HWM reset: MFE %.1f%% exceeded trigger %.1f%% + 4%% — ratchet re-armed",
                      symbol, sym["mfe_pct"] * 100, sym["hwm_mfe_at_trigger"] * 100)

        # ── PM-HWM: High-water-mark stop — tighten when >8% pulled back from MFE peak ──
        # If MFE reached >12% but price has since dropped >8% from that peak (yet pnl >3%),
        # ratchet stop up to avg_cost × 1.05 to lock in at least 5% profit.
        _hwm_mfe = sym.get("mfe_pct", 0.0)
        if (_hwm_mfe > 0.12
                and pnl_pct > 0.03
                and pnl_pct < _hwm_mfe - 0.08
                and not sym.get("hwm_tightened")
                and sym.get("stop_loss", 0) > 0
                and not sym.get("max_loss_exited")
                and not sym.get("time_stopped")):
            _hwm_stop = round(avg_cost * 1.05, 2)
            _hwm_old  = sym["stop_loss"]
            if _hwm_stop > _hwm_old and _hwm_stop < cur_price * 0.99:
                _runner_hwm = qty - sym.get("partial_qty", 0)
                if _runner_hwm > 0:
                    _cancel_stop_orders(symbol)
                    _hwm_oid = _place_stop(symbol, _runner_hwm, _hwm_stop)
                    if _hwm_oid:
                        sym["hwm_tightened"]       = True
                        sym["hwm_mfe_at_trigger"]  = _hwm_mfe  # P1-fix: track MFE when triggered
                        sym["stop_loss"]            = _hwm_stop
                        sym["stop_order_id"]        = _hwm_oid
                        changed = True
                        _log.info(
                            "[monitor] %s HWM-STOP: MFE %.1f%% → now %.1f%% "
                            "(>8%% drawdown from peak) — stop $%.2f → $%.2f",
                            symbol, _hwm_mfe * 100, pnl_pct * 100, _hwm_old, _hwm_stop)
                        _tg((
                                              f"📉 *HWM Stop — {symbol}*\n"
                                              f"MFE {_hwm_mfe*100:.1f}% → now {pnl_pct*100:.1f}%"
                                              f" (>8% drawdown from peak)\n"
                                              f"Stop: ${_hwm_old:.2f} → ${_hwm_stop:.2f} (+5%)"
                                          ))

        # ── PM-VWAP: Intraday VWAP weakness — tighten stop 11-14 ET ─────────────
        # If price falls below intraday VWAP with rising volume during 11-14 ET =
        # institutional selling pressure. Tighten trailing stop one step (to 97% of entry).
        # Only fires once per day and only on profitable positions (> +2%).
        _vwap_now   = datetime.now(_ET)
        _vwap_today = _vwap_now.strftime("%Y-%m-%d")
        if (11 <= _vwap_now.hour < 14
                and sym.get("_vwap_check_date") != _vwap_today
                and pnl_pct > 0.02
                and sym.get("stop_loss", 0) > 0
                and not sym.get("time_stopped")
                and not sym.get("max_loss_exited")):
            sym["_vwap_check_date"] = _vwap_today
            try:
                _dfin = yf.Ticker(symbol).history(period="1d", interval="5m", auto_adjust=True)
                if len(_dfin) >= 10:
                    _tp_in   = (_dfin["High"] + _dfin["Low"] + _dfin["Close"]) / 3
                    _cum_vol = _dfin["Volume"].cumsum()
                    _vwap_in = (float((_tp_in * _dfin["Volume"]).cumsum().iloc[-1] / _cum_vol.iloc[-1])
                                if float(_cum_vol.iloc[-1]) > 0 else cur_price)
                    _vol_3   = float(_dfin["Volume"].tail(3).mean())
                    _vol_pre = float(_dfin["Volume"].iloc[:-3].mean()) if len(_dfin) > 3 else _vol_3
                    _vol_rising = _vol_pre > 0 and _vol_3 > _vol_pre * 1.2
                    if cur_price < _vwap_in * 0.999 and _vol_rising:
                        _old_sl  = sym["stop_loss"]
                        _new_sl  = round(max(_old_sl, avg_cost * 0.97), 2)
                        if _new_sl > _old_sl:
                            sym["stop_loss"] = _new_sl
                            sym["vwap_tightened"] = True
                            changed = True
                            _log.info(
                                "[monitor] %s VWAP weakness (price $%.2f < VWAP $%.2f + rising vol) "
                                "— stop tightened $%.2f → $%.2f",
                                symbol, cur_price, _vwap_in, _old_sl, _new_sl)
                            _tg((f"⚠️ *VWAP Weakness — {symbol}*\n"
                                                       f"Price ${cur_price:.2f} < VWAP ${_vwap_in:.2f} "
                                                       f"+ rising volume (11-14 ET)\n"
                                                       f"Stop tightened: ${_old_sl:.2f} → ${_new_sl:.2f}"))
            except Exception:
                _log.debug("[%s] suppressed", __name__, exc_info=True)

        # ── PM-Keltner: Dynamic Keltner Channel stop — ratchet up once per day ──────
        # Stop climbs to EMA20 - 2×ATR14 (lower Keltner band) when that level is above
        # the current stop. Gives more room in fast-moving trends, tightens in volatile dips.
        # Guard: only fires once/day, only when keltner_low > current stop AND < 98.5% of price.
        _kelt_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if (not sym.get("max_loss_exited")
                and not sym.get("time_stopped")
                and sym.get("stop_loss", 0) > 0
                and sym.get("_keltner_date") != _kelt_today
                and pnl_pct > 0.0):
            sym["_keltner_date"] = _kelt_today
            try:
                _kdf = _get_hist(symbol)
                if len(_kdf) >= 22:
                    _kclose  = _kdf["Close"].values
                    _khigh   = _kdf["High"].values
                    _klow    = _kdf["Low"].values
                    # EMA20 of close — seed with mean of oldest 20 bars to eliminate bias
                    _alpha   = 2.0 / (20 + 1)
                    _ema20   = sum(_kclose[-21:-1]) / 20.0
                    for _cv in _kclose[-1:]:
                        _ema20 = _alpha * _cv + (1 - _alpha) * _ema20
                    # ATR14
                    _ktr = [max(_khigh[i] - _klow[i],
                                abs(_khigh[i] - _kclose[i-1]),
                                abs(_klow[i]  - _kclose[i-1]))
                            for i in range(1, len(_kclose))]
                    _katr14 = sum(_ktr[-14:]) / 14
                    # Lower Keltner band = EMA20 - 2×ATR14
                    _keltner_low = round(_ema20 - 2.0 * _katr14, 2)
                    _old_ksl     = sym["stop_loss"]
                    # Only ratchet UP — never lower the stop
                    if (_keltner_low > _old_ksl
                            and _keltner_low < cur_price * 0.985   # needs breathing room
                            and _keltner_low > avg_cost * 0.90):   # at least near breakeven
                        sym["stop_loss"]      = _keltner_low
                        sym["keltner_raised"] = True
                        changed               = True
                        _log.info(
                            "[monitor] %s Keltner stop ratchet: $%.2f → $%.2f "
                            "(EMA20=%.2f ATR14=%.2f)",
                            symbol, _old_ksl, _keltner_low, _ema20, _katr14)
                        # P0-fix: also update the live Alpaca GTC stop order
                        _kelt_qty = int(sym.get("shares", sym.get("qty", 0)))
                        if _kelt_qty > 0:
                            _cancel_stop_orders(symbol)
                            _new_kelt_oid = _place_stop(symbol, _kelt_qty, _keltner_low)
                            if _new_kelt_oid:
                                sym["stop_order_id"] = _new_kelt_oid
                                _log.info("[monitor] %s Keltner GTC stop updated $%.2f id=%s",
                                          symbol, _keltner_low, _new_kelt_oid)
                            else:
                                _log.warning("[monitor] %s Keltner state updated but Alpaca stop NOT placed — retries next cycle",
                                             symbol)
                        _tg((
                                              f"📐 *Keltner Stop — {symbol}*\n"
                                              f"Stop ratcheted ${_old_ksl:.2f} → ${_keltner_low:.2f}\n"
                                              f"EMA20 ${_ema20:.2f} − 2×ATR14 ${_katr14:.2f}\n"
                                              f"P&L {pnl_pct*100:+.1f}% | Price ${cur_price:.2f}"
                                          ))
            except Exception:
                _log.debug("[%s] suppressed", __name__, exc_info=True)

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
                    _tg((
                                          f"🔴 *Max Loss Cap — {symbol}*\n"
                                          f"Price ${cur_price:.2f} breached 2R floor ${_max_loss_p:.2f}\n"
                                          f"Entry ${avg_cost:.2f} | Stop ${_sli_ml:.2f} | "
                                          f"Loss {pnl_pct*100:.1f}%\n"
                                          f"Tudor Jones hard floor — forced exit"
                                      ))
        # Quality-adjusted exits: elite setups get more room to run
        composite      = sym.get("composite_score", 0.0)
        _regime_ts      = _get_cached_regime()
        _base_ts        = 25 if _regime_ts == "bull" else (15 if _regime_ts == "neutral" else 10)
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
                _dfg = _get_hist(symbol)
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
                        _remaining = qty  # Alpaca qty already reflects all filled partial sells
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
                                _tg((f"🛡️ *Earnings Guard — {symbol}*\n"
                                                                f"{_days_earn} days to earnings report\n"
                                                                f"Stop moved to breakeven ${avg_cost:.2f}"))
                        elif pnl_pct < 0.01 and _days_earn <= 3 and _remaining > 0:
                            # Flat/losing with report in 3 days → exit now
                            _cancel_stop_orders(symbol)
                            if _place_market_sell(symbol, _remaining):
                                sym["earnings_closed"] = True
                                changed = True
                                _log.warning("[monitor] EARNINGS CLOSE %s — %dd to report, "
                                             "flat/loss %.1f%%", symbol, _days_earn, pnl_pct*100)
                                _tg((f"📅 *Earnings Close — {symbol}*\n"
                                                                f"{_days_earn} days to report, gain {pnl_pct*100:+.1f}%\n"
                                                                f"Exiting before earnings risk"))
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
                                    _tg((
                                                          f"\U0001f9e8 *IV Crush Exit — {symbol}*\n"
                                                          f"ATM implied vol {_iv_val*100:.0f}% > 50%\n"
                                                          f"{_dte_iv} days to earnings | "
                                                          f"gain +{pnl_pct*100:.1f}%\n"
                                                          f"Exiting runner before IV collapse"
                                                      ))
                except Exception as _ive:
                    _log.debug("[monitor] iv_crush %s: %s", symbol, _ive)

        # ── Ex-dividend guard ─────────────────────────────────────────────────────
        # Stock gaps down by the dividend amount on ex-date (typically 0.5–3%).
        # If ex-date is ≤ 2 calendar days away: profitable → breakeven; flat/loss → close.
        # Checked once per trading day to avoid repeated stop moves.
        _exdiv_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if (not sym.get("exdiv_guarded")
                and sym.get("_exdiv_check_date") != _exdiv_today
                and not sym.get("time_stopped")
                and not sym.get("max_loss_exited")):
            sym["_exdiv_check_date"] = _exdiv_today
            try:
                import pandas as _pd_ex
                _cal = yf.Ticker(symbol).calendar
                _exdiv_days = None
                if _cal is not None and not _cal.empty:
                    for _fld in ("Ex-Dividend Date", "Dividend Date"):
                        _row = None
                        if _fld in _cal.index:
                            _row = _cal.loc[_fld].iloc[0]
                        elif _fld in _cal.columns:
                            _row = _cal[_fld].iloc[0]
                        if _row is not None and not _pd_ex.isna(_row):
                            _ex = _pd_ex.Timestamp(_row).date()
                            _delta = (_ex - datetime.now(_ET).date()).days
                            if 0 <= _delta <= 2:
                                _exdiv_days = _delta
                            break
                if _exdiv_days is not None:
                    _rem_ex = qty - sym.get("partial_qty", 0)
                    if pnl_pct >= 0.03 and _rem_ex > 0 and not sym.get("breakeven_done"):
                        _cancel_stop_orders(symbol)
                        _ex_oid = _place_stop(symbol, _rem_ex, round(avg_cost, 2))
                        if _ex_oid:
                            sym["breakeven_done"] = True
                            sym["stop_order_id"]  = _ex_oid
                            sym["exdiv_guarded"]  = True
                            changed = True
                            _log.warning("[monitor] %s EX-DIV GUARD: %dd to ex-date — "
                                         "stop moved to breakeven $%.2f",
                                         symbol, _exdiv_days, avg_cost)
                    elif pnl_pct < 0.01 and _rem_ex > 0:
                        _cancel_stop_orders(symbol)
                        if _place_market_sell(symbol, _rem_ex):
                            sym["exdiv_guarded"] = True
                            changed = True
                            _log.warning("[monitor] %s EX-DIV CLOSE: %dd to ex-date, "
                                         "pnl=%.1f%% — closing pre-dividend",
                                         symbol, _exdiv_days, pnl_pct * 100)
                    _tg((f"📅 *Ex-Div Guard — {symbol}*\n"
                                               f"{_exdiv_days} day(s) to ex-dividend\n"
                                               f"Action: {_action} (P&L {pnl_pct*100:+.1f}%)"))
            except Exception as _exe:
                _log.debug("[monitor] exdiv_guard %s: %s", symbol, _exe)

        # ── Step A: Initial stop (placed once when position first seen) ──────────
        # Use HARD STOP at VCP pivot low if we have the planned stop from the order.
        # This protects against false breakouts at exactly the level Minervini intends.
        # Fall back to 7% trailing stop if no metadata (legacy or missing report).
        stop_loss_level = sym.get("stop_loss", 0.0)
        use_hard_stop   = (stop_loss_level > 0 and stop_loss_level < avg_cost * 0.99
                           and not sym.get("breakeven_done")
                           and not sym.get("partial_done"))

        needs_stop = (not sym.get("slippage_exited") and (
            not sym.get("trailing_stop_placed") or (
                sym.get("trailing_stop_placed") and
                sym.get("stop_order_id") and
                not _stop_order_alive(sym["stop_order_id"])
            )
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
                and 1 <= _days_held <= 20
                and cur_price < _buy_stp_z * 0.99
                and pnl_pct < 0.03):
            _rem_z = qty - sym.get("partial_qty", 0)
            _log.warning("[monitor] %s FAILED BREAKOUT — cur $%.2f < pivot $%.2f (day %d)",
                         symbol, cur_price, _buy_stp_z, _days_held)
            _cancel_stop_orders(symbol)
            if _rem_z > 0 and _place_market_sell(symbol, _rem_z):
                sym["failed_breakout_done"] = True
                try:
                    from risk_manager import record_pivot_failure as _rpf
                    _rpf(symbol)
                except Exception:
                    _log.debug("[%s] suppressed", __name__, exc_info=True)
                changed = True
                _tg((f"❌ *Failed Breakout — {symbol}*\n"
                                                f"Price ${cur_price:.2f} fell back under pivot ${_buy_stp_z:.2f}\n"
                                                f"Day {_days_held} — cutting loss"))
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
            if _ed_pt and _days_held >= 5:
                _pt_today = datetime.now(_ET).strftime("%Y-%m-%d")
                if sym.get("_pivot_trail_date") != _pt_today:
                    sym["_pivot_trail_date"] = _pt_today
                    try:
                        _dfp = _get_hist(symbol)
                        if len(_dfp) >= 5:
                            # Find most recent swing low in last 20 bars (skip last 2 incomplete)
                            _lows  = _dfp["Low"].values
                            _n_start = max(1, len(_lows) - 21)
                            _swing = None
                            for _i in range(_n_start, len(_lows) - 2):
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
                                                _days_held)
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
            if _ed_ma and _days_held >= 10:
                sym["_ma20_check_date"] = _ma20_today
                try:
                    _dfm = _get_hist(symbol)
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
                                        _ma20_val, _days_held)
                except Exception as _me:
                    _log.debug("[monitor] ma20 trail %s: %s", symbol, _me)

        # ── 8-Week Hold Rule: O'Neil fast mover detection ───────────────────────
        # Stock that gains ≥20% within first 15 trading days = potential 100%+ winner.
        # Override first partial: hold full position up to 8 weeks (40 trading days).
        _ed_fm = sym.get("entry_date", "")
        _td_fm = _days_held
        if (not sym.get("fast_mover")
                and pnl_pct >= 0.20
                and 0 < _td_fm <= 15):
            sym["fast_mover"] = True
            _log.info("[monitor] %s FAST MOVER: +%.1f%% in %d days — 8-week hold rule activated",
                      symbol, pnl_pct * 100, _td_fm)
            _tg((f"🚀 *Fast Mover — {symbol}*\n"
                                       f"+{pnl_pct*100:.1f}% in {_td_fm} trading days\n"
                                       f"O'Neil 8-week hold rule activated — "
                                       f"holding full position to week 8"))
        # ── Step B1: First partial at +10% — sell 33%, keep current stop ─────────
        initial_qty = sym.get("initial_qty", qty)
        # Superperformance skip: composite ≥8 setups (elite VCPs) need more room before first partial
        _cs_val = float(sym.get("composite_score", 0.0) or 0.0)
        partial1_trigger = 0.15 if _cs_val >= 8.0 else 0.10
        mm_pct = sym.get("measured_move_pct", 0.0) or 0.0
        partial2_trigger = max(mm_pct, 0.20) if mm_pct > 0.05 else 0.20

        # ── Confirm pending B1 fill: check if limit order has left open orders ─────
        # B1 sets _b1_fill_pending=True when a limit sell is placed but not yet
        # confirmed. Once the order disappears from Alpaca open orders it has filled
        # (or expired). Only then is partial1 fully confirmed — this prevents B2 from
        # triggering against an unconfirmed B1.
        if sym.get("_b1_fill_pending") and sym.get("_b1_order_id"):
            _open_oids = {o.get("id", "") for o in _get_open_orders(symbol)}
            if sym["_b1_order_id"] not in _open_oids:
                sym.pop("_b1_fill_pending", None)
                sym.pop("_b1_order_id", None)
                changed = True
                _log.info("[monitor] %s B1 fill confirmed (order left open list)", symbol)

        # ── Confirm pending B2 fill: same pattern as B1 ────────────────────────
        if sym.get("_b2_fill_pending") and sym.get("_b2_order_id"):
            _open_oids_b2 = {o.get("id", "") for o in _get_open_orders(symbol)}
            if sym["_b2_order_id"] not in _open_oids_b2:
                sym.pop("_b2_fill_pending", None)
                sym.pop("_b2_order_id", None)
                changed = True
                _log.info("[monitor] %s B2 fill confirmed (order left open list)", symbol)

        _skip_b1_8w = sym.get("fast_mover") and _days_held < 40 and pnl_pct > 0.05
        if pnl_pct >= partial1_trigger and not sym.get("partial1_done") and not _skip_b1_8w:
            sell_qty = max(1, round(initial_qty / 3))
            _lim1 = round(cur_price * 0.999, 2)  # 0.1% below market — fast fill, better price
            _b1_oid = _place_limit_sell(symbol, sell_qty, _lim1)
            if _b1_oid is not None:
                sym["partial1_done"]  = True
                sym["partial_done"]   = True   # backward-compat for time stop check
                sym["partial_qty"]    = sell_qty
                sym["partial1_price"] = cur_price
                # Track fill confirmation unless it was a market-sell fallback (instant fill)
                if _b1_oid not in ("market", ""):
                    sym["_b1_order_id"]    = _b1_oid
                    sym["_b1_fill_pending"] = True
                changed = True
                _log.info("[monitor] ✓ %s PARTIAL-1 (33%%): sold %d sh @ $%.2f (+%.1f%%)",
                          symbol, sell_qty, cur_price, pnl_pct * 100)

        # ── Step B2: Second partial at measured move or +20% — sell 33%, tighten ─
        elif (sym.get("partial1_done") and
              not sym.get("_b1_fill_pending") and   # don't fire B2 until B1 confirmed filled
              not sym.get("_b2_fill_pending") and   # don't re-fire B2 while awaiting confirmation
              pnl_pct >= partial2_trigger and
              not sym.get("partial2_done")):
            already_sold = sym.get("partial_qty", 0)
            sell_qty2 = max(1, round(initial_qty / 3))
            _lim2 = round(cur_price * 0.999, 2)
            _b2_oid_val = _place_limit_sell(symbol, sell_qty2, _lim2)
            if _b2_oid_val:
                sym["partial2_done"]  = True
                sym["partial2_qty"]   = sell_qty2
                sym["partial_qty"]    = already_sold + sell_qty2
                sym["partial2_price"] = cur_price
                # Track fill confirmation unless it was a market-sell fallback (instant fill)
                if _b2_oid_val not in ("market", ""):
                    sym["_b2_order_id"]    = _b2_oid_val
                    sym["_b2_fill_pending"] = True
                changed = True
                _log.info("[monitor] ✓ %s PARTIAL-2 (33%%): sold %d sh @ $%.2f (+%.1f%%)"
                          " — runner with 5%% trailing",
                          symbol, sell_qty2, cur_price, pnl_pct * 100)
                # Tighten trailing stop for the remaining runner.
                # Use current Alpaca qty minus the just-placed B2 sell qty so pyramid
                # shares (if any were added by Step P) are included in the runner count.
                runner_qty = qty - sell_qty2
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
        # Strict pnl < partial2_trigger guard: belt-and-suspenders to prevent pyramid
        # firing in the same cycle as B2 if partial2_done was somehow not yet written.
        if (sym.get("partial1_done")
                and not sym.get("pyramid_done")
                and not sym.get("partial2_done")
                and 0.12 <= pnl_pct < partial2_trigger):
            _pyr_days = _days_held
            if _pyr_days >= 3:
                _pyr_qty = max(1, round(qty * 0.30))
                _pyr_above_ma20 = False
                try:
                    _df_pyr = _get_hist(symbol)
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
                        _tg((f"📈 *Pyramid* — {symbol}\n"
                                                   f"Added {_pyr_qty} shares @ ${cur_price:.2f} "
                                                   f"(+{pnl_pct*100:.1f}%, day {_pyr_days})"))
        # ── Step C: Move stop to breakeven at +8% (if no partial yet) ───────────
        if pnl_pct >= breakeven_trigger and not sym.get("breakeven_done"):
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

        # ── Step F: Early pyramid at +4% when RS line near high ──────────────────
        # Add 25% of initial qty when position has +4% AND RS line confirms strength
        # by sitting at/near its 90-day high. Fires before B1 (+10%) — reserved for
        # the strongest setups where relative strength leads from the start.
        # Guard: P not yet triggered (each pyramid step fires at most once).
        if (not sym.get("step_f_done")
                and not sym.get("pyramid_done")
                and not sym.get("partial1_done")
                and 0.04 <= pnl_pct < 0.10):
            _f_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_f_check_date") != _f_today:
                sym["_f_check_date"] = _f_today
                _rs_f_near_high = False
                try:
                    _df_f  = _get_hist(symbol)
                    _spy_f = _spy_close_cycle if _spy_close_cycle is not None else (
                        yf.Ticker("SPY").history(period="90d", interval="1d", auto_adjust=True)["Close"])
                    if len(_df_f) >= 22 and len(_spy_f) >= 22:
                        _c_f  = _df_f["Close"]
                        _rs_f = (_c_f / _spy_f.reindex(_c_f.index, method="ffill")).dropna()
                        if len(_rs_f) >= 10:
                            _rs_f_high      = float(_rs_f.max())
                            _rs_f_now       = float(_rs_f.iloc[-1])
                            _rs_f_near_high = _rs_f_high > 0 and _rs_f_now >= _rs_f_high * 0.98
                except Exception as _fe:
                    _log.debug("[monitor] step_f rs %s: %s", symbol, _fe)
                if _rs_f_near_high:
                    _f_qty = max(1, round(sym.get("initial_qty", qty) * 0.25))
                    if _place_market_buy(symbol, _f_qty):
                        sym["step_f_done"]  = True
                        sym["step_f_qty"]   = _f_qty
                        sym["step_f_price"] = cur_price
                        changed = True
                        _log.info("[monitor] ✓ %s STEP-F early pyramid: +%d sh @ $%.2f "
                                  "(+%.1f%%, RS at 90d high)",
                                  symbol, _f_qty, cur_price, pnl_pct * 100)
                        _tg((f"📈 *Step F — Early Pyramid — {symbol}*\n"
                                                   f"Added {_f_qty} sh @ ${cur_price:.2f} "
                                                   f"(+{pnl_pct*100:.1f}%)\n"
                                                   f"RS line at 90-day high — leading strength"))

        # ── Step F2: Follow-on add at MA10w pullback with volume dry-up ───────────
        # When an active profitable position pulls back to the 10-week MA with
        # drying volume, add 25% — classic Minervini re-entry on a proven winner.
        # MFE guard (≥8%): ensures stock genuinely rallied first before pulling back.
        # Volume dry-up: 5-day avg < 60% of 50-day avg (mirrors VCP Tier 0 logic).
        if (not sym.get("step_f2_done")
                and not sym.get("partial2_done")
                and pnl_pct > 0.04
                and sym.get("mfe_pct", 0) >= 0.08):
            _f2_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_f2_check_date") != _f2_today:
                sym["_f2_check_date"] = _f2_today
                _f2_triggered = False
                try:
                    _dfw2 = _get_weekly_hist(symbol)
                    _dfd2 = _get_hist(symbol)
                    if len(_dfw2) >= 12 and len(_dfd2) >= 51:
                        _wc2      = _dfw2["Close"]
                        _ma10w_f2 = float(_wc2.iloc[-11:-1].mean())  # 10 completed weekly closes
                        _near_ma10w = _ma10w_f2 * 0.98 <= cur_price <= _ma10w_f2 * 1.03
                        _vol5_f2    = float(_dfd2["Volume"].tail(5).mean())
                        _vol50_f2   = float(_dfd2["Volume"].tail(51).iloc[:-1].mean())
                        _vol_dry_f2 = _vol50_f2 > 0 and _vol5_f2 < _vol50_f2 * 0.60
                        _f2_triggered = _near_ma10w and _vol_dry_f2
                except Exception as _f2e:
                    _log.debug("[monitor] step_f2 %s: %s", symbol, _f2e)
                if _f2_triggered:
                    _f2_qty = max(1, round(sym.get("initial_qty", qty) * 0.25))
                    if _place_market_buy(symbol, _f2_qty):
                        sym["step_f2_done"]  = True
                        sym["step_f2_qty"]   = _f2_qty
                        sym["step_f2_price"] = cur_price
                        changed = True
                        _log.info("[monitor] ✓ %s STEP-F2 follow-on: +%d sh @ $%.2f "
                                  "(+%.1f%%, MA10w pull + vol dry)",
                                  symbol, _f2_qty, cur_price, pnl_pct * 100)
                        _tg((f"📈 *Step F2 — Follow-on — {symbol}*\n"
                                                   f"Added {_f2_qty} sh @ ${cur_price:.2f} "
                                                   f"(+{pnl_pct*100:.1f}%)\n"
                                                   f"MA10w pull with vol dry-up — re-entry"))

        # ── Step E: 21-EMA pullback scale-in (+15% shares) ───────────────────────
        # Minervini: add to winners on tight pullbacks to the 21-EMA with low volume.
        # Only fires once per position, requires pnl >5%, vol < 60% of 50d avg,
        # and MFE ahead of current pnl (not extended past the peak).
        if (not sym.get("step_e_done")
                and not sym.get("partial2_done")
                and pnl_pct > 0.05
                and sym.get("mfe_pct", 0) > pnl_pct + 0.03):
            _e_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_step_e_check_date") != _e_today:
                sym["_step_e_check_date"] = _e_today
                _e_triggered = False
                try:
                    _dfe = _get_hist(symbol)
                    if len(_dfe) >= 43:
                        _ce = _dfe["Close"].values
                        _alpha_e = 2.0 / (21 + 1)
                        _ema21 = float(sum(_ce[-42:-21]) / 21)
                        for _v_e in _ce[-21:]:
                            _ema21 = _alpha_e * float(_v_e) + (1 - _alpha_e) * _ema21
                        _near_ema21 = abs(cur_price - _ema21) / _ema21 <= 0.02
                        _vol5_e  = float(_dfe["Volume"].tail(5).mean())
                        _vol50_e = float(_dfe["Volume"].tail(51).iloc[:-1].mean())
                        _vol_dry_e = _vol50_e > 0 and _vol5_e < _vol50_e * 0.60
                        _e_triggered = _near_ema21 and _vol_dry_e
                except Exception as _ee:
                    _log.debug("[monitor] step_e %s: %s", symbol, _ee)
                if _e_triggered:
                    _e_qty = max(1, round(sym.get("initial_qty", qty) * 0.15))
                    if _place_market_buy(symbol, _e_qty):
                        sym["step_e_done"]  = True
                        sym["step_e_qty"]   = _e_qty
                        sym["step_e_price"] = cur_price
                        changed = True
                        _log.info("[monitor] ✓ %s STEP-E 21-EMA scale-in: +%d sh @ $%.2f "
                                  "(+%.1f%%, EMA21 pull + vol dry)",
                                  symbol, _e_qty, cur_price, pnl_pct * 100)
                        _tg((
                                              f"📊 *Step E — 21-EMA Scale-in — {symbol}*\n"
                                              f"Added {_e_qty} sh @ ${cur_price:.2f}"
                                              f" (+{pnl_pct*100:.1f}%)\n"
                                              f"21-EMA pull with vol dry-up"
                                          ))

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

        # ── Step G2: Runner upgrade — 25% partial exit + 10% trail at 2× measured move ─
        # P3.6-fix: at 2× target take 25% off the table, then give the true runner 10% room.
        # Widens from 5% to 10% so normal pullbacks don't stop out an extended winner.
        if (sym.get("partial2_done")
                and not sym.get("runner_upgraded")
                and mm_pct > 0.05
                and pnl_pct >= mm_pct * 2):
            _runner_rem = qty - sym.get("partial_qty", 0)
            if _runner_rem > 0:
                # sell 25% of total position as partial
                _g2_partial = max(1, round(qty * 0.25))
                _g2_partial = min(_g2_partial, _runner_rem)
                _g2_sold = False
                if _g2_partial > 0:
                    _g2_sold = bool(_place_market_sell(symbol, _g2_partial))
                    if _g2_sold:
                        sym["partial_qty"] = sym.get("partial_qty", 0) + _g2_partial
                        changed = True
                        _log.info("[monitor] %s G2 PARTIAL: sold %d sh (25%%) at 2×mm pnl=+%.1f%%",
                                  symbol, _g2_partial, pnl_pct * 100)
                _runner_after = _runner_rem - (_g2_partial if _g2_sold else 0)
                if _runner_after > 0:
                    _cancel_stop_orders(symbol)
                    _r2x_oid = _place_trailing_stop(symbol, _runner_after, 0.10)
                    if _r2x_oid:
                        sym["runner_upgraded"] = True
                        sym["stop_order_id"]   = _r2x_oid
                        changed = True
                        _log.info("[monitor] %s RUNNER UPGRADE: trail→10%% at 2×mm (pnl=+%.1f%%)",
                                  symbol, pnl_pct * 100)
                        _tg((
                                              f"\U0001f680 *Runner Upgrade G2 — {symbol}*\n"
                                              f"Sold {_g2_partial if _g2_sold else 0} sh (25%%)\n"
                                              f"Trail widened → 10%% | P&L +{pnl_pct*100:.1f}%%"
                                          ))

        # ── Step V: Volume climax exit — monster-volume day after ≥20% gain ────────
        # Blow-off top signal: churning on extreme volume = likely institutional exit.
        # Minervini explicitly warns: "when everyone wants in on huge volume, take profits."
        if (not sym.get("climax_exit_done")
                and pnl_pct >= 0.20):
            _vc_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_vc_check_date") != _vc_today:
                sym["_vc_check_date"] = _vc_today
                try:
                    _dfvc = _get_hist(symbol)
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
                                _tg((f"🔥 *Volume Climax — {symbol}*\n"
                                                           f"{_vol_cur/_vol_avg:.1f}× avg vol, "
                                                           f"pnl=+{pnl_pct*100:.1f}%\n"
                                                           f"Sold 50% — Minervini blow-off top"))
                except Exception as _vce:
                    _log.debug("[monitor] volume climax %s: %s", symbol, _vce)

        # ── Step LH/LL: Lower-high + lower-low on daily = trend reversal ─────────────
        # After all partials are done, if the runner shows a structural trend break exit.
        # Minervini: "sell when the stock starts acting abnormally — lower highs confirm weakness."
        if (sym.get("partial2_done")
                and not sym.get("lhll_stopped")):
            try:
                _dfll = _get_hist(symbol)
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

        # ── Step PAR: Parabolic gain protection — sell 25% on blow-off day ────────
        # If today's intraday gain >8% AND total pnl >15%, the stock is in a parabolic
        # extension. Sell 25% to lock gains before the vertical drop.
        if (not sym.get("parabolic_done")
                and pnl_pct > 0.15
                and not sym.get("_b1_fill_pending")
                and not sym.get("_b2_fill_pending")):
            _par_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_par_check_date") != _par_today:
                sym["_par_check_date"] = _par_today
                try:
                    _dfpar = _get_hist(symbol)
                    if len(_dfpar) >= 1:
                        _open_par = float(_dfpar["Open"].iloc[-1])
                        _day_gain = (cur_price - _open_par) / _open_par if _open_par > 0 else 0.0
                        if _day_gain > 0.08:
                            _par_qty = max(1, round(qty * 0.25))
                            _log.warning(
                                "[monitor] %s PARABOLIC: day gain +%.1f%%, pnl +%.1f%% — "
                                "selling 25%% (%d sh) at $%.2f",
                                symbol, _day_gain * 100, pnl_pct * 100, _par_qty, cur_price)
                            if _place_market_sell(symbol, _par_qty):
                                sym["parabolic_done"] = True
                                if not sym.get("partial1_done"):
                                    sym["partial1_done"] = True
                                    sym["partial_done"]  = True
                                sym["partial_qty"] = sym.get("partial_qty", 0) + _par_qty
                                changed = True
                                _tg((
                                                      f"🚀 *Parabolic Protection — {symbol}*\n"
                                                      f"Day gain +{_day_gain*100:.1f}%,"
                                                      f" total +{pnl_pct*100:.1f}%\n"
                                                      f"Sold 25% ({_par_qty} sh) — locking gains"
                                                  ))
                except Exception as _pare:
                    _log.debug("[monitor] parabolic %s: %s", symbol, _pare)

        # ── PM10: PEAD — Post-Earnings Announcement Drift (60-day time-stop hold) ────
        # Academic finding: stocks that beat EPS estimates by ≥5% drift up ~60 trading days.
        # Suspending the time stop during this window avoids selling the best winners early.
        if not sym.get("pead_hold") and not sym.get("pead_checked") and pnl_pct > 0:
            _pead_today = datetime.now(_ET).strftime("%Y-%m-%d")
            if sym.get("_pead_check_date") != _pead_today:
                sym["_pead_check_date"] = _pead_today
                try:
                    import pandas as _pd
                    _ed_df = yf.Ticker(symbol).earnings_dates
                    if _ed_df is None or _ed_df.empty:
                        # P3.5: no data — mark checked so we don't retry every cycle
                        sym["pead_checked"] = True
                    elif _ed_df is not None and not _ed_df.empty:
                        _ed_r = _ed_df.reset_index()
                        _now_ts = _pd.Timestamp.now(tz="UTC")
                        _past = _ed_r[
                            _pd.to_datetime(_ed_r.iloc[:, 0], utc=True, errors="coerce")
                            < _now_ts
                        ]
                        if not _past.empty:
                            _le   = _past.iloc[0]
                            _est  = float(_le.get("EPS Estimate", 0) or 0)
                            _rep  = float(_le.get("Reported EPS", 0) or 0)
                            sym["pead_checked"] = True
                            if _est > 0 and _rep >= _est * 1.05:
                                sym["pead_hold"]  = True
                                _pead_dt = _pd.to_datetime(_le.iloc[0], utc=True, errors="coerce")
                                sym["pead_date"]  = (_pead_dt.astimezone(_ET).strftime("%Y-%m-%d")
                                                     if _pd.notna(_pead_dt)
                                                     else datetime.now(_ET).strftime("%Y-%m-%d"))
                                _log.info(
                                    "[monitor] %s PEAD: EPS $%.2f vs est $%.2f (+%.0f%%) "
                                    "— 60-day time-stop hold activated",
                                    symbol, _rep, _est, (_rep - _est) / _est * 100)
                                _tg((
                                                      f"\U0001f4c8 *PEAD Hold — {symbol}*\n"
                                                      f"EPS ${_rep:.2f} beat est ${_est:.2f}"
                                                      f" (+{(_rep-_est)/_est*100:.0f}%)\n"
                                                      f"60-day time-stop suspended"
                                                  ))
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
                _df_rsd = _get_hist(symbol)
                _spy_rsd = (_spy_close_cycle.iloc[-60:] if _spy_close_cycle is not None else
                            yf.Ticker("SPY").history(period="60d", interval="1d", auto_adjust=True)["Close"])
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
                            _tg((
                                                  f"⚠️ *RS Divergence — {symbol}*\n"
                                                  f"Price new 20d high ${_price_now:.2f} "
                                                  f"but RS line {(1-_rs_now_v/_rs_20h)*100:.1f}%% "
                                                  f"below its peak\n"
                                                  f"Distribution signal — stop moved to breakeven"
                                              ))
            except Exception as _rsd_e:
                _log.debug("[monitor] rs_divergence %s: %s", symbol, _rsd_e)

        # ── Step W: Weekly close under MA10w — exit on trend breakdown ─────────────
        # O'Neil / Minervini rule: a full week closing below the 10-week moving average
        # signals that the intermediate uptrend is broken — position should be closed.
        # Checked once per trading day; always acts on the last COMPLETED weekly bar
        # (iloc[-2]) so an in-progress week never triggers a premature exit.
        _w_today = datetime.now(_ET).strftime("%Y-%m-%d")
        if (not sym.get("weekly_close_exited")
                and not sym.get("time_stopped")
                and not sym.get("max_loss_exited")
                and sym.get("_w_check_date") != _w_today):
            sym["_w_check_date"] = _w_today
            try:
                _dfw = _get_weekly_hist(symbol)
                if len(_dfw) >= 12:
                    _wc         = _dfw["Close"]
                    # MA10w at the last completed bar: average of that bar + 9 preceding bars
                    _last_wk_close = float(_wc.iloc[-2])
                    _ma10w_w       = float(_wc.iloc[-11:-1].mean())
                    if _last_wk_close < _ma10w_w:
                        _rem_w = qty - sym.get("partial_qty", 0)
                        _log.warning(
                            "[monitor] %s STEP-W: last weekly close $%.2f < MA10w $%.2f — closing",
                            symbol, _last_wk_close, _ma10w_w)
                        _cancel_stop_orders(symbol)
                        if _rem_w > 0 and _place_market_sell(symbol, _rem_w):
                            sym["weekly_close_exited"] = True
                            changed = True
                            _tg((f"📉 *Step W — Weekly Close Exit — {symbol}*\n"
                                                       f"Last weekly close ${_last_wk_close:.2f} "
                                                       f"< MA10w ${_ma10w_w:.2f}\n"
                                                       f"Trend breakdown confirmed — position closed"))
            except Exception as _we:
                _log.debug("[monitor] step_w %s: %s", symbol, _we)

        # ── Step D: Time stop — P5.2 extracted to _step_d_time_stop() ──────
        _pead_active = (
            sym.get("pead_hold")
            and _trading_days_held(sym.get("pead_date", "")) < 60
        )
        if _step_d_time_stop(sym, symbol, qty, pnl_pct,
                             time_stop_days, _days_held, _pead_active,
                             _soft_dd_mode, cfg):
            changed = True

        # ── Hard absolute max holding period: 60 trading days ──────────────────────
        # Prevents positions from becoming indefinite anchors. Winners get a tight
        # 3% trailing stop; flat/losers are closed immediately.
        _HARD_MAX_DAYS = 60
        if (sym.get("entry_date")
                and not sym.get("max_hold_exited")
                and not sym.get("time_stopped")
                and not _pead_active):
            _abs_days = _days_held
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
                        _tg(("Max Hold " + symbol + "\n"
                                                   + f"Day {_abs_days} - tightened to 3% trailing\n"
                                                   + f"P&L {pnl_pct*100:+.1f}% - locking gains"))
                elif _rem_hm > 0:
                    _cancel_stop_orders(symbol)
                    if _place_market_sell(symbol, _rem_hm):
                        sym["max_hold_exited"] = True
                        changed = True
                        _log.warning("[monitor] MAX HOLD EXIT %s day %d pnl=%.1f%% - closed",
                                     symbol, _abs_days, pnl_pct * 100)
                        _tg(("Max Hold Exit " + symbol + "\n"
                                                   + f"Day {_abs_days} - closed at {pnl_pct*100:+.1f}%\n"
                                                   + "60-day absolute cap reached"))
    # Checkpoint: persist all live-position changes (sells, stop updates) before cleanup
    # phases run.  A crash in cleanup won't lose any sell events written above.
    if changed:
        _save_state(state)
        changed = False
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
            _cur_p   = next((p for p in positions if p["symbol"] == _sym_st), None)
            if _cur_p:
                _raw_plpc = float(_cur_p.get("unrealized_plpc", 0))
                if not (-1.0 <= _raw_plpc <= 5.0):
                    _log.warning("[monitor] %s unrealized_plpc=%.4f outside expected [-1, 5] — check Alpaca API format", _sym_st, _raw_plpc)
                _pnl_st = _raw_plpc * 100
            else:
                _pnl_st = 0.0
            _days_st = _trading_days_held(_ed_st)
            _tg(("Stale Position -- " + _sym_st + "\n"
                 + f"Held {_days_st} trading days - P&L {_pnl_st:+.1f}%\n"
                 + "Review: is the VCP thesis still valid?"))
            changed = True
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
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

                # Post-trade AI post-mortem via Haiku (background thread, non-blocking)
                threading.Thread(
                    target=_run_postmortem,
                    args=(sym, dict(sym_data), pnl_pct),
                    daemon=True,
                    name=f"postmortem-{sym}",
                ).start()

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
                        _sa_content = _jsa2.dumps(_sa2, indent=2)
                        _sa_tmp = _sa_path + ".tmp"
                        with open(_sa_tmp, "w") as _f_sa_tmp:
                            _f_sa_tmp.write(_sa_content)
                        _osa2.replace(_sa_tmp, _sa_path)
                except Exception:
                    _log.debug("[%s] suppressed", __name__, exc_info=True)
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
