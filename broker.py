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


class AlpacaError(RuntimeError):
    """Raised when Alpaca API is persistently unreachable after retries."""


def _retry(fn, *args, _retries: int = 3, _backoff: float = 2.0, **kwargs):
    """Call fn(*args, **kwargs) up to _retries times with exponential backoff.
    Does not retry on 4xx HTTP errors (client logic errors, not transient).
    """
    import time as _t
    import alpaca_trade_api.rest as _alp_rest
    delay = 1.0
    for _attempt in range(_retries):
        try:
            return fn(*args, **kwargs)
        except _alp_rest.APIError as _exc:
            _code = int(getattr(_exc, "status_code", 0) or 0)
            if 400 <= _code < 500 and _code != 429:
                raise   # client error - don't retry
            if _attempt == _retries - 1:
                raise
            _log.warning("[broker] %s attempt %d/%d HTTP %d: %s - retry in %.0fs",
                         getattr(fn, "__name__", "call"), _attempt + 1, _retries, _code, _exc, delay)
        except Exception as _exc:
            if _attempt == _retries - 1:
                raise
            _log.warning("[broker] %s attempt %d/%d failed: %s - retry in %.0fs",
                         getattr(fn, "__name__", "call"), _attempt + 1, _retries, _exc, delay)
        _t.sleep(delay)
        delay *= _backoff


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
    acct = _retry(get_api().get_account)
    return {
        "equity":          float(acct.equity),
        "cash":            float(acct.cash),
        "portfolio_value": float(acct.portfolio_value),
        "buying_power":    float(acct.buying_power),
        "status":          acct.status,
        "account_number":  acct.account_number,
    }


def get_positions() -> list[dict]:
    """Fetch open positions. Raises AlpacaError on persistent failure (never returns [] silently)."""
    try:
        positions = _retry(get_api().list_positions)
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
            for p in positions
        ]
    except Exception as e:
        _log.error("[broker] list_positions FAILED after retries: %s", e)
        raise AlpacaError(f"list_positions failed: {e}") from e


def is_market_open() -> bool:
    try:
        return bool(_retry(get_api().get_clock).is_open)
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
        _log.error("[broker] BUY-STOP %s FAILED (order may not have reached Alpaca): %s", symbol, e)
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
        _log.error("[broker] SELL-STOP %s FAILED (stop NOT placed): %s", symbol, e)
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
        _log.error("[broker] MARKET-SELL %s FAILED (exit NOT submitted): %s", symbol, e)
        return None


def cancel_all_orders(symbol: str | None = None) -> int:
    """Cancel all open orders, or only for a specific symbol. Idempotent - safe to retry."""
    try:
        if symbol:
            orders = [o for o in _retry(get_api().list_orders, status="open") if o.symbol == symbol]
            for o in orders:
                _retry(get_api().cancel_order, o.id)
            return len(orders)
        else:
            result = _retry(get_api().cancel_all_orders)
            return len(result) if result else 0
    except Exception as e:
        _log.error("[broker] cancel_orders FAILED after retries: %s", e)
        return 0


def get_open_orders() -> list[dict]:
    """Fetch open orders. Raises AlpacaError on persistent failure (never returns [] silently)."""
    try:
        orders = _retry(get_api().list_orders, status="open")
        return [
            {"id": o.id, "symbol": o.symbol, "qty": float(o.qty),
             "type": o.type, "stop_price": float(o.stop_price or 0),
             "side": o.side, "status": o.status,
             "created_at": str(o.created_at) if hasattr(o, "created_at") else ""}
            for o in orders
        ]
    except Exception as e:
        _log.error("[broker] list_orders FAILED after retries: %s", e)
        raise AlpacaError(f"list_orders failed: {e}") from e
