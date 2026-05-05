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


def position_size(portfolio_value: float, entry_price: float,
                  stop_loss: float, risk_pct: float | None = None) -> dict:
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

    risk_amount = portfolio_value * risk_pct
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

    measured_move = (entry_price - stop_loss) * 3   # 3R target
    rr_ratio      = measured_move / risk_per_share

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


def close_trade(symbol: str, pnl_pct: float, portfolio_value: float, start_value: float):
    """Call when a position is closed."""
    state = _load()
    state.get("positions_risk", {}).pop(symbol, None)
    state["open_risk_pct"] = sum(state.get("positions_risk", {}).values())

    # Update daily P&L
    daily_pnl = (portfolio_value - start_value) / start_value if start_value else 0
    state["daily_pnl_pct"] = daily_pnl

    if pnl_pct < 0:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
    else:
        state["consecutive_losses"] = 0

    _save(state)


def get_state() -> dict:
    return _load()


def daily_reset():
    """Call at start of each trading day to reset daily counters."""
    state = _load()
    state["date"] = str(date.today())
    state["daily_pnl_pct"] = 0.0
    state["trading_halted"] = False
    state["halt_reason"] = ""
    _save(state)
    _log.info("[risk] Daily reset complete")
