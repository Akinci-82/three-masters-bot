"""
Broker layer — Alpaca integration (dedicated account for Three Masters Bot).
Uses THREE_MASTERS_ALPACA_* env vars to keep portfolio separate.
"""
from __future__ import annotations
import logging

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopOrderRequest,
    StopLimitOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.common.exceptions import APIError

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

_log = logging.getLogger(__name__)
_api: TradingClient | None = None


class AlpacaError(RuntimeError):
    """Raised when Alpaca API is persistently unreachable after retries."""


def _retry(fn, *args, _retries: int = 3, _backoff: float = 2.0, **kwargs):
    """Call fn(*args, **kwargs) up to _retries times with exponential backoff.
    Does not retry on 4xx HTTP errors (client logic errors, not transient).
    """
    import time as _t
    delay = 2.0
    for _attempt in range(_retries):
        try:
            return fn(*args, **kwargs)
        except APIError as _exc:
            _code = int(getattr(_exc, "status_code", 0) or 0)
            if 400 <= _code < 500 and _code != 429:
                raise  # client error - don't retry
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


def get_api() -> TradingClient:
    global _api
    if _api is None:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise RuntimeError(
                "THREE_MASTERS_ALPACA_API_KEY and THREE_MASTERS_ALPACA_SECRET_KEY must be set"
            )
        _is_paper = "paper" in ALPACA_BASE_URL.lower()
        _api = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=_is_paper)
    return _api


def get_account() -> dict:
    acct = _retry(get_api().get_account)
    return {
        "equity":          float(acct.equity),
        "cash":            float(acct.cash),
        "portfolio_value": float(acct.portfolio_value),
        "buying_power":    float(acct.buying_power),
        "status":          acct.status.value,
        "account_number":  acct.account_number,
    }


def get_positions() -> list[dict]:
    """Fetch open positions. Raises AlpacaError on persistent failure (never returns [] silently)."""
    try:
        positions = _retry(get_api().get_all_positions)
        return [
            {
                "symbol":          p.symbol,
                "qty":             float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price":   float(p.current_price),
                "market_value":    float(p.market_value),
                "unrealized_pl":   float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
            for p in positions
        ]
    except Exception as e:
        _log.error("[broker] get_all_positions FAILED after retries: %s", e)
        raise AlpacaError(f"get_all_positions failed: {e}") from e


def is_market_open() -> bool:
    try:
        return bool(_retry(get_api().get_clock).is_open)
    except Exception:
        return False


def place_buy_stop(symbol: str, qty: int, stop_price: float) -> dict | None:
    """
    Place a GTC buy-stop-limit order: executes when price crosses stop_price upward.
    Placed after close (22:30 CEST) — triggers during the NEXT trading day if price breaks out.
    """
    try:
        _limit = round(stop_price * 1.008, 2)  # 0.8% above stop — wider than default to survive volatile gap-up fills
        order = _retry(
            get_api().submit_order,
            StopLimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
                limit_price=_limit,
            ),
        )
        _log.info("[broker] BUY-STOP-LIMIT %s qty=%d stop=$%.2f limit=$%.2f id=%s",
                  symbol, qty, stop_price, _limit, order.id)
        return {"id": str(order.id), "symbol": symbol, "qty": qty,
                "stop": stop_price, "type": "buy_stop"}
    except Exception as e:
        _log.error("[broker] BUY-STOP %s FAILED (order may not have reached Alpaca): %s", symbol, e)
        return None


def place_sell_stop(symbol: str, qty: int, stop_price: float) -> dict | None:
    """Place a protective stop-loss (sell-stop) order."""
    try:
        order = _retry(
            get_api().submit_order,
            StopOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
            ),
        )
        _log.info("[broker] SELL-STOP %s qty=%d stop=$%.2f id=%s",
                  symbol, qty, stop_price, order.id)
        return {"id": str(order.id), "symbol": symbol, "qty": qty,
                "stop": stop_price, "type": "sell_stop"}
    except Exception as e:
        _log.error("[broker] SELL-STOP %s FAILED (stop NOT placed): %s", symbol, e)
        return None


def place_market_sell(symbol: str, qty: int) -> dict | None:
    """Immediate market sell (for manual exits or stop-loss override)."""
    try:
        order = _retry(
            get_api().submit_order,
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ),
        )
        _log.info("[broker] MARKET-SELL %s qty=%d id=%s", symbol, qty, order.id)
        return {"id": str(order.id), "symbol": symbol, "qty": qty, "type": "market_sell"}
    except Exception as e:
        _log.error("[broker] MARKET-SELL %s FAILED (exit NOT submitted): %s", symbol, e)
        return None


def place_market_buy(symbol: str, qty: int) -> dict | None:
    """Immediate market buy (used for pyramiding into confirmed winners)."""
    try:
        order = _retry(
            get_api().submit_order,
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ),
        )
        _log.info("[broker] MARKET-BUY %s qty=%d id=%s", symbol, qty, order.id)
        return {"id": str(order.id), "symbol": symbol, "qty": qty, "type": "market_buy"}
    except Exception as e:
        _log.error("[broker] MARKET-BUY %s FAILED: %s", symbol, e)
        return None


def cancel_all_orders(symbol: str | None = None) -> int:
    """Cancel all open orders, or only for a specific symbol. Idempotent - safe to retry."""
    try:
        if symbol:
            orders = _retry(
                get_api().get_orders,
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]),
            )
            success_count = 0
            for o in orders:
                try:
                    _retry(get_api().cancel_order_by_id, str(o.id))
                    success_count += 1
                except Exception as _ce:
                    _log.warning("[broker] cancel_order_by_id %s failed: %s", o.id, _ce)
            return success_count
        else:
            result = _retry(get_api().cancel_orders)
            return len(result) if result else 0
    except Exception as e:
        _log.error("[broker] cancel_orders FAILED after retries: %s", e)
        return 0


def get_open_orders() -> list[dict]:
    """Fetch open orders. Raises AlpacaError on persistent failure (never returns [] silently)."""
    try:
        orders = _retry(
            get_api().get_orders,
            GetOrdersRequest(status=QueryOrderStatus.OPEN),
        )
        return [
            {
                "id":         str(o.id),
                "symbol":     o.symbol,
                "qty":        float(o.qty),
                "type":       o.order_type.value,
                "stop_price": float(o.stop_price) if o.stop_price is not None else 0.0,
                "side":       o.side.value,
                "status":     o.status.value,
                "created_at": str(o.created_at) if o.created_at else "",
            }
            for o in orders
        ]
    except Exception as e:
        _log.error("[broker] get_orders FAILED after retries: %s", e)
        raise AlpacaError(f"get_orders failed: {e}") from e
