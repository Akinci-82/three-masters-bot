"""
Layer 3 — TUDOR JONES
Risk management: position sizing, circuit breakers, portfolio limits.

Tudor Jones principle: Never risk more than 1-2% of capital on any single trade.
Position size = (Portfolio × Risk%) / (Entry - Stop Loss)

Additional Tudor Jones rules:
  - Cut losers fast, let winners run
  - Never add to a losing position
  - Portfolio heat (total open risk) never exceeds 6-8% at once
  - Respect the 4% daily loss limit absolutely
"""
from __future__ import annotations
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from config import RISK, LOG_DIR

_log = logging.getLogger(__name__)
RISK_FILE = LOG_DIR / "risk_state.json"


def _load() -> dict:
    try:
        with open(RISK_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "date": str(date.today()),
            "daily_pnl_pct": 0.0,
            "portfolio_ath": 0.0,
            "open_risk_pct": 0.0,
            "consecutive_losses": 0,
            "trading_halted": False,
            "halt_reason": "",
            "positions_risk": {},   # symbol → risk_pct
        }


def _save(data: dict):
    RISK_FILE.parent.mkdir(exist_ok=True)
    with open(RISK_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _kelly_factor() -> float:
    """
    Half-Kelly fraction from trade journal win rate and average R.
    Full Kelly = W - (1-W)/R where W=win_rate, R=avg_win/avg_loss.
    Half-Kelly applied for safety; base clamped [0.5, 1.0]. Returns 1.0 if <10 trades.
    Additional loss-streak multiplier: 3 losses -> 0.65x, 5+ losses -> 0.40x.
    """
    import json as _j
    from pathlib import Path as _P
    jpath = _P(__file__).parent / "logs" / "trade_journal.jsonl"
    base_kelly = 1.0
    try:
        if not jpath.exists():
            base_kelly = 1.0
        else:
            trades = [_j.loads(ln) for ln in jpath.read_text().splitlines() if ln.strip()]
            if len(trades) < 10:
                base_kelly = 1.0
            else:
                wins   = [t["r_multiple"] for t in trades if t.get("r_multiple", 0) > 0]
                losses = [abs(t["r_multiple"]) for t in trades if t.get("r_multiple", 0) < 0]
                if not wins or not losses:
                    base_kelly = 1.0
                else:
                    w     = len(wins) / len(trades)
                    r     = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
                    kelly = w - (1 - w) / r
                    base_kelly = float(max(0.5, min(1.0, kelly / 2.0)))
    except Exception:
        base_kelly = 1.0

    # Loss-streak dampener: reduce sizing after consecutive losses
    try:
        streak = _load().get("consecutive_losses", 0)
        if streak >= 5:
            streak_mult = 0.40   # 5+ losses in a row — severe size reduction
        elif streak >= 3:
            streak_mult = 0.65   # 3-4 losses — meaningful reduction
        else:
            streak_mult = 1.0
        if streak >= 3:
            _log.info("[risk] Kelly dampened %.2f → %.2f (loss streak=%d)",
                      base_kelly, base_kelly * streak_mult, streak)
        return float(max(0.25, min(1.0, base_kelly * streak_mult)))
    except Exception:
        return base_kelly


def _stop_distance_factor(entry_price: float, stop_price: float) -> float:
    """Scale position size down for wide stops — tight VCP handle = bigger position."""
    if entry_price <= 0 or stop_price >= entry_price:
        return 1.0
    stop_pct = (entry_price - stop_price) / entry_price
    tiers = RISK.get("stop_distance_tiers", [
        (0.05, 1.00), (0.07, 0.85), (0.10, 0.70), (1.00, 0.55),
    ])
    for threshold, factor in tiers:
        if stop_pct <= threshold:
            return factor
    return tiers[-1][1]


def position_size(portfolio_value: float, entry_price: float,
                  stop_loss: float, risk_pct: float | None = None,
                  measured_move_pct: float = 0.0, symbol: str = "") -> dict:
    """
    Calculate Tudor Jones position size.

    Risk per trade = portfolio_value × risk_pct
    Shares = risk_amount / (entry_price - stop_loss)
    Notional = shares × entry_price

    Returns dict with: shares, notional, risk_amount, risk_pct, rr_ratio
    """
    if risk_pct is None:
        risk_pct = RISK["risk_per_trade_pct"]

    risk_pct = min(risk_pct, RISK["max_risk_per_trade_pct"])

    kelly  = _kelly_factor()
    sdf    = _stop_distance_factor(entry_price, stop_loss)
    risk_amount = portfolio_value * risk_pct * kelly * sdf
    risk_per_share = entry_price - stop_loss

    if risk_per_share <= 0:
        raise ValueError(f"stop_loss ({stop_loss}) >= entry ({entry_price}) — invalid")

    shares = risk_amount / risk_per_share
    notional = shares * entry_price

    # Cap at max_position_pct if set (never exceed 20% of portfolio in one stock)
    max_notional = portfolio_value * 0.20
    if notional > max_notional:
        notional = max_notional
        shares   = notional / entry_price
        risk_amount = shares * risk_per_share

    if 0.05 < measured_move_pct < 1.50:
        # Use Claude's base height estimate; always at least 2R
        measured_move = max(entry_price * measured_move_pct, risk_per_share * 2)
    else:
        measured_move = risk_per_share * 3  # default 3R when no Claude estimate

    # ATR sanity check: cap measured move at 3x the stock's recent ATR.
    # Prevents unrealistic targets on low-volatility stocks from inflating R:R.
    try:
        if not symbol:
            raise ValueError("no symbol")
        import yfinance as _yf_atr
        _h_atr = _yf_atr.Ticker(symbol).history(period="30d", interval="1d", auto_adjust=True)
        if len(_h_atr) >= 14:
            _tr = (_h_atr["High"] - _h_atr["Low"]).tail(14).mean()
            _atr_pct = float(_tr) / entry_price if entry_price > 0 else 0.0
            if _atr_pct > 0:
                _atr_cap = entry_price * (_atr_pct * 3.0)
                if measured_move > _atr_cap:
                    _log.info("[risk] measured_move $%.2f capped at 3×ATR $%.2f (ATR=%.1f%%)",
                              measured_move, _atr_cap, _atr_pct * 100)
                    measured_move = _atr_cap
    except Exception:
        _log.debug("[%s] suppressed: %%s", __name__, exc_info=True)
    rr_ratio = measured_move / risk_per_share

    return {
        "shares": int(shares),
        "notional": round(notional, 2),
        "risk_amount": round(risk_amount, 2),
        "risk_pct": round(risk_pct, 4),
        "risk_per_share": round(risk_per_share, 2),
        "target_price": round(entry_price + measured_move, 2),
        "rr_ratio": round(rr_ratio, 2),
    }


def check_can_trade(portfolio_value: float, new_risk_pct: float) -> tuple[bool, str]:
    """
    Verify all risk limits before placing a new trade.
    Returns (can_trade, reason).
    """
    state = _load()

    # Reset daily state on new trading day
    if state.get("date") != str(date.today()):
        state["date"] = str(date.today())
        state["daily_pnl_pct"] = 0.0
        state["trading_halted"] = False
        state["halt_reason"] = ""
        _save(state)

    if state.get("trading_halted"):
        return False, f"Halted: {state.get('halt_reason')}"

    # Daily loss circuit breaker
    if state["daily_pnl_pct"] <= -RISK["max_daily_loss_pct"]:
        msg = f"Daily loss {state['daily_pnl_pct']:.1%} >= -{RISK['max_daily_loss_pct']:.0%}"
        _halt(msg)
        return False, msg

    # Drawdown guard
    ath = state.get("portfolio_ath", portfolio_value)
    if ath < portfolio_value:
        state["portfolio_ath"] = portfolio_value
        _save(state)
        ath = portfolio_value
    drawdown = (ath - portfolio_value) / ath if ath > 0 else 0
    # Soft pause: slow down entries before hitting the hard halt (8% drawdown)
    soft_dd_pct = RISK.get("soft_drawdown_pause_pct", 0.08)
    if soft_dd_pct <= drawdown < RISK["max_drawdown_pct"]:
        return False, (f"Soft drawdown pause: {drawdown:.1%} >= {soft_dd_pct:.0%} — "
                       "no new entries until portfolio recovers")
    if drawdown >= RISK["max_drawdown_pct"]:
        msg = f"Drawdown {drawdown:.1%} >= {RISK['max_drawdown_pct']:.0%}"
        _halt(msg)
        return False, msg

    # Portfolio heat: total open risk across all positions
    open_risk = state.get("open_risk_pct", 0.0)
    max_heat  = RISK.get("max_portfolio_heat_pct", 0.08)
    if open_risk + new_risk_pct > max_heat:
        return False, f"Portfolio heat {open_risk + new_risk_pct:.1%} exceeds {max_heat:.0%}"

    return True, "OK"


def _halt(reason: str):
    state = _load()
    state["trading_halted"] = True
    state["halt_reason"] = reason
    _save(state)
    _log.warning("[risk] TRADING HALTED: %s", reason)


def register_trade(symbol: str, risk_pct: float):
    """Call when a position is opened."""
    state = _load()
    state.setdefault("positions_risk", {})[symbol] = risk_pct
    state["open_risk_pct"] = sum(state["positions_risk"].values())
    _save(state)
    _log.info("[risk] %s opened — open_risk=%.1f%%", symbol, state["open_risk_pct"] * 100)


def close_trade(symbol: str, pnl_pct: float, portfolio_value: float,
                start_value: float | None = None):
    """Call when a position is closed."""
    state = _load()
    state.get("positions_risk", {}).pop(symbol, None)
    state["open_risk_pct"] = sum(state.get("positions_risk", {}).values())

    # Use day_start_equity stored at daily_reset() if caller doesn't know it
    start = start_value or state.get("day_start_equity", portfolio_value)
    daily_pnl = (portfolio_value - start) / start if start else 0
    state["daily_pnl_pct"] = daily_pnl

    if pnl_pct < 0:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        state["consecutive_wins"]   = 0
    else:
        state["consecutive_losses"] = 0
        state["consecutive_wins"]   = state.get("consecutive_wins", 0) + 1

    _save(state)
    _log.info("[risk] %s closed | pnl=%.1f%% | heat now=%.1f%% | day_pnl=%.1f%%",
              symbol, pnl_pct * 100, state["open_risk_pct"] * 100, daily_pnl * 100)


def record_stop_out(symbol: str, breakout_level: float = 0.0, cooldown_days: int = 5):
    """Record a stop-out so the symbol is in re-entry cooldown.
    Default 5 trading days; pass cooldown_days=2 for pyramided positions
    (shorter cooldown because stop-out after a pyramid often reflects a shakeout
    of the add-on shares, not a full base failure).
    """
    state = _load()
    state.setdefault("stop_out_cooldown", {})[symbol] = {
        "date":           str(date.today()),
        "breakout_level": round(breakout_level, 2),
        "cooldown_days":  cooldown_days,
    }
    _save(state)
    _log.info("[risk] %s stop-out recorded — %d-day cooldown (pivot=$%.2f)",
              symbol, cooldown_days, breakout_level)


def check_reentry_cooldown(symbol: str, current_price: float = 0.0) -> bool:
    """True if symbol is still in re-entry cooldown (5 trading days since stop-out).
    Also blocks re-entry if price has not recovered above the prior breakout level.
    """
    state = _load()
    entry = state.get("stop_out_cooldown", {}).get(symbol)
    if not entry:
        return False
    # Support both old format (plain date string) and new format (dict)
    if isinstance(entry, str):
        stop_date_str   = entry
        prior_breakout  = 0.0
        _cd_days        = 5
    else:
        stop_date_str   = entry.get("date", "")
        prior_breakout  = float(entry.get("breakout_level", 0.0))
        _cd_days        = int(entry.get("cooldown_days", 5))
    if not stop_date_str:
        return False
    try:
        from pandas.tseries.offsets import BDay
        import pandas as _pd
        stop_dt      = _pd.Timestamp(stop_date_str)
        cooldown_end = stop_dt + BDay(_cd_days)
        in_cooldown  = _pd.Timestamp.today() < cooldown_end
        if not in_cooldown:
            # Time cooldown expired — also check pivot recovery
            if prior_breakout > 0 and current_price > 0:
                if current_price < prior_breakout * 1.00:
                    # Price still below or at prior failed pivot — not recovered
                    _log.debug("[risk] %s re-entry blocked: price $%.2f below failed pivot $%.2f",
                               symbol, current_price, prior_breakout)
                    return True
            cooldowns = state.get("stop_out_cooldown", {})
            cooldowns.pop(symbol, None)
            _save(state)
            return False
        return True
    except Exception:
        return False



def get_state() -> dict:
    return _load()


def daily_reset(portfolio_value: float = 0.0):
    """Call at start of each trading day to reset daily counters."""
    state = _load()
    state["date"] = str(date.today())
    state["daily_pnl_pct"] = 0.0
    state["trading_halted"] = False
    state["halt_reason"] = ""
    if portfolio_value > 0:
        state["day_start_equity"] = portfolio_value   # used by close_trade for daily P&L
    _save(state)
    _log.info("[risk] Daily reset complete")

def sync_positions(held_symbols: set):
    """Sync positions_risk with actual held symbols — removes stale entries."""
    state = _load()
    before = dict(state.get("positions_risk", {}))
    state["positions_risk"] = {
        sym: risk for sym, risk in before.items()
        if sym in held_symbols
    }
    state["open_risk_pct"] = sum(state["positions_risk"].values())
    removed = set(before) - set(state["positions_risk"])
    if removed:
        _log.info("[risk] Removed stale risk entries: %s", removed)
    _save(state)


def record_pivot_failure(symbol: str):
    """Record a failed breakout — same pivot rejected for 45 trading days.
    Longer than stop_out_cooldown (5d): same pivot failing twice = distribution signal.
    """
    state = _load()
    state.setdefault("pivot_failure_cooldown", {})[symbol] = str(date.today())
    _save(state)
    _log.info("[risk] %s pivot failure recorded — 45-day re-entry cooldown", symbol)


def check_pivot_failure_cooldown(symbol: str) -> bool:
    """True if symbol is still in 45-day pivot failure cooldown."""
    state = _load()
    fail_date_str = state.get("pivot_failure_cooldown", {}).get(symbol)
    if not fail_date_str:
        return False
    try:
        from pandas.tseries.offsets import BDay
        import pandas as _pd
        fail_dt      = _pd.Timestamp(fail_date_str)
        cooldown_end = fail_dt + BDay(45)
        in_cooldown  = _pd.Timestamp.today() < cooldown_end
        if not in_cooldown:
            state.get("pivot_failure_cooldown", {}).pop(symbol, None)
            _save(state)
        return in_cooldown
    except Exception:
        return False
