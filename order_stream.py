"""
Order fill notification via Alpaca trade-update stream.
Sends Telegram alert the moment a buy-stop fills.

Uses alpaca-trade-api v3 WebSocket (trade_updates channel).
Runs in a background daemon thread — restarts automatically on disconnect.
"""
from __future__ import annotations
import logging
import os
import threading
import time

_log = logging.getLogger(__name__)
_RECONNECT_DELAY = 30   # seconds before reconnect attempt


def _tg(msg: str) -> None:
    try:
        import requests
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=8,
            )
    except Exception:
        pass


def _handle_fill(order: dict, event: str) -> None:
    """React to a filled or partially-filled buy order."""
    symbol    = order.get("symbol", "?")
    side      = order.get("side", "?")
    qty_fill  = float(order.get("filled_qty", 0))
    avg_fill  = float(order.get("filled_avg_price") or 0)
    qty_total = float(order.get("qty", 0))
    partial   = qty_fill < qty_total

    if side != "buy":
        return

    tag    = "⚡ *PARTIAL FILL*" if partial else "✅ *ORDER FILLED*"
    filled = f"{qty_fill:.0f}/{qty_total:.0f}sh" if partial else f"{qty_fill:.0f}sh"

    _log.info("[stream] %s %s %s @ $%.2f", event, symbol, filled, avg_fill)
    _tg(
        f"{tag} — *{symbol}*\n"
        f"Filled: {filled} @ ${avg_fill:.2f}\n"
        f"Position monitor will place trailing stop at next cycle (≤15 min)."
    )


def _stream_loop(stop_event: threading.Event) -> None:
    """Inner loop: connect, stream, reconnect on failure."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

    key    = os.getenv("THREE_MASTERS_ALPACA_API_KEY", "")
    secret = os.getenv("THREE_MASTERS_ALPACA_SECRET_KEY", "")
    base   = os.getenv("THREE_MASTERS_ALPACA_URL", "https://paper-api.alpaca.markets")

    if not key or not secret:
        _log.warning("[stream] No Alpaca credentials — order stream disabled")
        return

    import alpaca_trade_api as tradeapi

    while not stop_event.is_set():
        try:
            _log.info("[stream] Connecting to Alpaca trade stream...")
            conn = tradeapi.StreamConn(
                key_id=key, secret_key=secret,
                base_url=base, data_stream="",
            )

            @conn.on(r"trade_updates")
            async def on_trade(conn, channel, data):
                event = data.event
                order = data.order if hasattr(data, "order") else {}
                if isinstance(order, dict):
                    pass
                else:
                    order = order._raw if hasattr(order, "_raw") else {}

                if event in ("fill", "partial_fill"):
                    _handle_fill(order, event)
                elif event == "canceled":
                    sym = order.get("symbol", "?")
                    _log.info("[stream] Order cancelled: %s", sym)

            conn.run(["trade_updates"])

        except Exception as e:
            if stop_event.is_set():
                break
            _log.warning("[stream] Stream disconnected: %s — reconnecting in %ds",
                         e, _RECONNECT_DELAY)
            stop_event.wait(_RECONNECT_DELAY)

    _log.info("[stream] Order stream stopped")


def start(stop_event: threading.Event) -> threading.Thread | None:
    """Start fill-notification stream in a daemon thread."""
    try:
        import alpaca_trade_api  # noqa: F401
    except ImportError:
        _log.warning("[stream] alpaca-trade-api not installed — fill stream disabled")
        return None

    t = threading.Thread(
        target=_stream_loop, args=(stop_event,),
        daemon=True, name="order-stream",
    )
    t.start()
    _log.info("[stream] Order fill stream started")
    return t
