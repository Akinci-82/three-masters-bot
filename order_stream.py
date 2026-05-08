"""
Order fill notification via Alpaca trade-update stream (alpaca-trade-api v3).
Sends Telegram alert the moment a buy-stop fills.
Runs in a background daemon thread — restarts automatically on disconnect.
"""
from __future__ import annotations
import logging
import os
import threading
import time

_log = logging.getLogger(__name__)

_RECONNECT_BASE  = 30   # seconds before first retry
_RECONNECT_MAX   = 300  # cap at 5 minutes
_HEALTHY_MIN_SEC = 120  # session is "healthy" if it ran > 2 min before dropping


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


def _handle_trade_update(data) -> None:
    """React to Alpaca trade update events."""
    try:
        event     = data.event
        order     = data.order
        symbol    = order.symbol
        side      = order.side
        qty_fill  = float(order.filled_qty or 0)
        avg_fill  = float(order.filled_avg_price or 0)
        qty_total = float(order.qty or 0)

        if side != "buy":
            return

        if event == "fill":
            _log.info("[stream] FILL %s %dsh @ $%.2f", symbol, qty_fill, avg_fill)
            _tg(
                f"✅ *ORDER FILLED* — *{symbol}*\n"
                f"Filled: {qty_fill:.0f}sh @ ${avg_fill:.2f}\n"
                f"Trailing stop will be placed at next monitor cycle (≤15 min)."
            )
        elif event == "partial_fill":
            _log.info("[stream] PARTIAL FILL %s %.0f/%.0f sh @ $%.2f",
                      symbol, qty_fill, qty_total, avg_fill)
            _tg(
                f"⚡ *PARTIAL FILL* — *{symbol}*\n"
                f"Filled: {qty_fill:.0f}/{qty_total:.0f}sh @ ${avg_fill:.2f}"
            )
        elif event == "canceled":
            _log.info("[stream] CANCELED %s", symbol)
    except Exception as e:
        _log.debug("[stream] handle_trade_update error: %s", e)


def _stream_loop(stop_event: threading.Event) -> None:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

    key    = os.getenv("THREE_MASTERS_ALPACA_API_KEY", "")
    secret = os.getenv("THREE_MASTERS_ALPACA_SECRET_KEY", "")
    base   = os.getenv("THREE_MASTERS_ALPACA_URL", "https://paper-api.alpaca.markets")

    if not key or not secret:
        _log.warning("[stream] No Alpaca credentials — order stream disabled")
        return

    import alpaca_trade_api as tradeapi

    fail_count = 0  # consecutive fast-fail count for exponential backoff

    while not stop_event.is_set():
        t_start = time.monotonic()
        try:
            _log.info("[stream] Connecting to Alpaca trade stream...")
            stream = tradeapi.Stream(
                key_id=key,
                secret_key=secret,
                base_url=base,
                data_feed="iex",
            )

            @stream.on_trade_update
            async def on_trade_update(data):
                _handle_trade_update(data)

            stream.subscribe_trade_updates(on_trade_update)
            stream.run()   # blocks until disconnect

            # Normal disconnect (stream.run() returned without exception)
            elapsed = time.monotonic() - t_start
            if elapsed >= _HEALTHY_MIN_SEC:
                fail_count = 0  # healthy session — reset backoff
            _log.info("[stream] Stream ended after %.0fs — reconnecting in %ds",
                      elapsed, _RECONNECT_BASE)
            stop_event.wait(_RECONNECT_BASE)

        except Exception as e:
            if stop_event.is_set():
                break

            elapsed = time.monotonic() - t_start

            if elapsed < _HEALTHY_MIN_SEC:
                # Fast fail: auth error, rate limit, or connection refused.
                # The alpaca_trade_api library retries internally before raising,
                # so by the time we get here it has already hammered Alpaca for
                # several seconds. Apply exponential backoff to avoid HTTP 429 storms.
                fail_count += 1
                delay = min(_RECONNECT_BASE * (2 ** (fail_count - 1)), _RECONNECT_MAX)
                _log.warning(
                    "[stream] Fast fail #%d (%.0fs) — backoff %ds: %s",
                    fail_count, elapsed, delay, e,
                )
            else:
                # Healthy session dropped — reset backoff, short delay
                fail_count = 0
                delay = _RECONNECT_BASE
                _log.warning(
                    "[stream] Disconnected after %.0fs — reconnecting in %ds: %s",
                    elapsed, delay, e,
                )

            stop_event.wait(delay)

    _log.info("[stream] Order fill stream stopped")


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
