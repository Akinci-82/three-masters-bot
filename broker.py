"""
Broker layer — Alpaca integration (dedicated account for Three Masters Bot).
Uses THREE_MASTERS_ALPACA_* env vars to keep portfolio separate.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone

import alpaca_trade_api as tradeapi

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

_log = logging.getLogger(__name__)
_api: tradeapi.REST | None = None


def get_api() -> tradeapi.REST:
    global _api
    if _api is None:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise RuntimeError(
                "THREE_MASTERS_ALPACA_API_KEY and THREE_MASTERS_ALPACA_SECRET_KEY must be set"
            )
        _api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY,
                             base_url=ALPACA_BASE_URL, api_version="v2")
    return _api


def get_account() -> dict:
    acct = get_api().get_account()
    return {
        "equity":          float(acct.equity),
        "cash":            float(acct.cash),
        "portfolio_value": float(acct.portfolio_value),
        "buying_power":    float(acct.buying_power),
        "status":          acct.status,
        "account_number":  acct.account_number,
    }


def get_positions() -> list[dict]:
    try:
        return [
            {
                "symbol":            p.symbol,
                "qty":               float(p.qty),
                "avg_entry_price":   float(p.avg_entry_price),
                "current_price":     float(p.current_price),
                "market_value":      float(p.market_value),
                "unrealized_pl":     float(p.unrealized_pl),
                "unrealized_plpc":   float(p.unrealized_plpc),
            }
            for p in get_api().list_positions()
        ]
    except Exception as e:
        _log.warning("[broker] list_positions failed: %s", e)
        return []


def is_market_open() -> bool:
    try:
        return bool(get_api().get_clock().is_open)
    except Exception:
        return False


def place_buy_stop(symbol: str, qty: int, stop_price: float) -> dict | None:
    """
    Place a GTC buy-stop order: executes when price crosses stop_price upward.
    Placed after close (22:30 CEST) — triggers during the NEXT trading day if price breaks out.
    """
    try:
        _limit = round(stop_price * 1.005, 2)  # 0.5% above stop — caps slippage at entry
        order = get_api().submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="stop_limit",
            stop_price=round(stop_price, 2),
            limit_price=_limit,
            time_in_force="gtc",   # GTC: survives overnight, executes next trading day
        )
        _log.info("[broker] BUY-STOP-LIMIT %s qty=%d stop=$%.2f limit=$%.2f id=%s",
                  symbol, qty, stop_price, _limit, order.id)
        return {"id": order.id, "symbol": symbol, "qty": qty, "stop": stop_price, "type": "buy_stop"}
    except Exception as e:
        _log.error("[broker] BUY-STOP %s failed: %s", symbol, e)
        return None


def place_sell_stop(symbol: str, qty: int, stop_price: float) -> dict | None:
    """Place a protective stop-loss (sell-stop) order."""
    try:
        order = get_api().submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="stop",
            stop_price=round(stop_price, 2),
            time_in_force="gtc",   # GTC for stop-loss
        )
        _log.info("[broker] SELL-STOP %s qty=%d stop=$%.2f id=%s", symbol, qty, stop_price, order.id)
        return {"id": order.id, "symbol": symbol, "qty": qty, "stop": stop_price, "type": "sell_stop"}
    except Exception as e:
        _log.error("[broker] SELL-STOP %s failed: %s", symbol, e)
        return None


def place_market_sell(symbol: str, qty: int) -> dict | None:
    """Immediate market sell (for manual exits or stop-loss override)."""
    try:
        order = get_api().submit_order(
            symbol=symbol, qty=qty, side="sell",
            type="market", time_in_force="day",
        )
        _log.info("[broker] MARKET-SELL %s qty=%d id=%s", symbol, qty, order.id)
        return {"id": order.id, "symbol": symbol, "qty": qty, "type": "market_sell"}
    except Exception as e:
        _log.error("[broker] MARKET-SELL %s failed: %s", symbol, e)
        return None


def cancel_all_orders(symbol: str | None = None) -> int:
    """Cancel all open orders, or only for a specific symbol."""
    try:
        if symbol:
            orders = [o for o in get_api().list_orders(status="open") if o.symbol == symbol]
            for o in orders:
                get_api().cancel_order(o.id)
            return len(orders)
        else:
            result = get_api().cancel_all_orders()
            return len(result) if result else 0
    except Exception as e:
        _log.warning("[broker] cancel_orders: %s", e)
        return 0


def get_open_orders() -> list[dict]:
    try:
        return [
            {"id": o.id, "symbol": o.symbol, "qty": float(o.qty),
             "type": o.type, "stop_price": float(o.stop_price or 0),
             "side": o.side, "status": o.status,
             "created_at": str(o.created_at) if hasattr(o, "created_at") else ""}
            for o in get_api().list_orders(status="open")
        ]
    except Exception:
        return []
