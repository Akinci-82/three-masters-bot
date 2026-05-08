"""
Order fill notification via Alpaca trade-update stream (alpaca-trade-api v3).
Sends Telegram alert the moment a buy-stop fills.
Runs in a background daemon thread — restarts automatically on disconnect.

Reconnect strategy:
  The alpaca_trade_api library catches auth errors internally and retries with
  only a 10ms delay — it never raises to our code. We work around this by
  running stream.run() in a sub-thread and polling _trading_ws._running.
  If auth doesn't succeed within CONNECT_TIMEOUT seconds we call stream.stop()
  to break the library's internal loop and apply exponential backoff.
"""
from __future__ import annotations
import logging
import os
import threading
import time

_log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30   # seconds to wait for successful auth before giving up
_RECONNECT_BASE  = 30   # base reconnect delay after normal disconnect
_RECONNECT_MAX   = 300  # cap exponential backoff at 5 minutes
_ALERT_FAIL_THRESHOLD = 3  # send Telegram alert after this many consecutive auth fails


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


def _stop_stream(stream) -> None:
    """Stop stream safely — works even if event loop hasn't started yet."""
    try:
        stream.stop()
    except Exception:
        # _loop may be None if stream never started; set flag directly as fallback
        try:
            if stream._trading_ws:
                stream._trading_ws._should_run = False
        except Exception:
            pass


def _is_connected(stream) -> bool:
    """Return True if the trading WebSocket has successfully authenticated."""
    try:
        return bool(stream._trading_ws._running)
    except Exception:
        return False


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

    fail_count   = 0   # consecutive auth-fail count
    alerted      = False  # suppress duplicate Telegram alerts

    while not stop_event.is_set():
        t_start = time.monotonic()

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

        # Run stream in a sub-thread so we can stop it externally on auth timeout.
        # The library's internal _run_forever catches auth errors with a 10ms retry
        # and never raises them to the caller — we must interrupt from outside.
        stream_thread = threading.Thread(
            target=stream.run, daemon=True, name="stream-ws"
        )
        stream_thread.start()

        # Poll for successful auth (stream._trading_ws._running goes True on success)
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while time.monotonic() < deadline and not stop_event.is_set():
            if _is_connected(stream):
                break
            time.sleep(1)

        if stop_event.is_set():
            _stop_stream(stream)
            stream_thread.join(timeout=10)
            break

        if not _is_connected(stream):
            # Auth timed out — library is stuck in its internal retry loop.
            # Stop it cleanly, apply exponential backoff before next attempt.
            fail_count += 1
            delay = min(_RECONNECT_BASE * (2 ** (fail_count - 1)), _RECONNECT_MAX)
            _log.warning(
                "[stream] Auth timeout after %ds (fail #%d) — stopping and waiting %ds",
                _CONNECT_TIMEOUT, fail_count, delay,
            )
            _stop_stream(stream)
            stream_thread.join(timeout=10)

            if fail_count >= _ALERT_FAIL_THRESHOLD and not alerted:
                _tg(
                    f"⚠️ *Three Masters — WebSocket stream down*\n"
                    f"Failed to authenticate {fail_count}× in a row.\n"
                    f"Retrying every {delay//60}min. Position monitor (15 min) still active."
                )
                alerted = True

            stop_event.wait(delay)
            continue

        # Successfully authenticated
        if fail_count > 0:
            _log.info("[stream] Auth recovered after %d failure(s)", fail_count)
            if alerted:
                _tg("✅ *Three Masters — WebSocket stream recovered*")
        fail_count = 0
        alerted    = False
        _log.info("[stream] Connected to Alpaca trade stream")

        # Wait for the stream to disconnect naturally
        stream_thread.join()

        if stop_event.is_set():
            break

        elapsed = time.monotonic() - t_start
        _log.info(
            "[stream] Disconnected after %.0fs — reconnecting in %ds",
            elapsed, _RECONNECT_BASE,
        )
        stop_event.wait(_RECONNECT_BASE)

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
