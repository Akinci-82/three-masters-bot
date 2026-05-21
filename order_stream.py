"""
Order fill notification via Alpaca trade-update stream (alpaca-py TradingStream).
Sends Telegram alert the moment a buy-stop fills.
Runs in a background daemon thread — restarts automatically on disconnect.

Reconnect strategy:
  alpaca-py's TradingStream has the same internal retry loop as the old library
  (catches auth errors, retries every 10ms, never raises to caller). We work
  around this by running stream.run() in a sub-thread and polling stream._running.
  If auth doesn't succeed within CONNECT_TIMEOUT seconds we call stream.stop()
  to break the internal loop cleanly and apply exponential backoff.

A1 — Missed-fill recovery on reconnect:
  _last_disconnect_ts is updated whenever the stream drops. On the next successful
  auth we call _fetch_missed_fills(since=_last_disconnect_ts) which queries the
  Alpaca REST API for closed orders filled after the disconnect timestamp and
  replays them through _handle_trade_update so no fills are silently lost.
"""
from __future__ import annotations
import logging
import os
import threading
import time
from datetime import datetime, timezone

from alpaca.trading.stream import TradingStream

_log = logging.getLogger(__name__)

_CONNECT_TIMEOUT     = 30   # seconds to wait for successful auth before giving up
_RECONNECT_BASE      = 30   # base reconnect delay after normal disconnect
_RECONNECT_MAX       = 300  # cap exponential backoff at 5 minutes
_ALERT_FAIL_THRESHOLD = 3   # send Telegram alert after this many consecutive auth fails

# A1: track last disconnect time so we can replay missed fills on reconnect
_last_disconnect_ts: datetime | None = None
_last_disconnect_lock = threading.Lock()


from notifications import _tg


def _handle_trade_update(data) -> None:
    """React to Alpaca trade update events."""
    try:
        event     = data.event
        order     = data.order
        symbol    = order["symbol"]
        side      = order.get("side", "")
        qty_fill  = float(order.get("filled_qty", 0) or 0)
        avg_fill  = float(order.get("filled_avg_price", 0) or 0)
        qty_total = float(order.get("qty", 0) or 0)

        order_type = order.get("order_type", "") or order.get("type", "")

        # ── BUY fills ────────────────────────────────────────────────────────
        if side == "buy":
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
            return

        # ── SELL fills — stop / trailing-stop / limit ─────────────────────────
        # These fire when a GTC stop hits overnight or intraday; the monitor only
        # runs every 15 min, so the user would otherwise have no real-time alert.
        _stop_types = ("stop", "trailing_stop", "stop_limit")
        if side == "sell" and event == "fill":
            _pnl_tag = ""
            try:
                _avg_cost = float(order.get("filled_avg_price", avg_fill))
                # avg_entry_price is not in the stream event; we use a best-effort note
            except Exception:
                pass
            if order_type in _stop_types:
                _log.warning("[stream] STOP FILL %s %.0fsh @ $%.2f", symbol, qty_fill, avg_fill)
                _tg(
                    f"🛑 *STOP HIT* — *{symbol}*\n"
                    f"Filled: {qty_fill:.0f}sh @ ${avg_fill:.2f}\n"
                    f"Position closed by stop order. P&L updated next monitor cycle."
                )
            else:
                _log.info("[stream] SELL FILL %s %.0fsh @ $%.2f [%s]",
                          symbol, qty_fill, avg_fill, order_type)
    except Exception as e:
        _log.debug("[stream] handle_trade_update error: %s", e)


def _fetch_missed_fills(since: datetime) -> None:
    """A1: Replay fills that arrived while the WebSocket was disconnected.

    Queries Alpaca REST for all closed orders filled after `since`, then
    replays each one through _handle_trade_update. This ensures fills that
    occurred during a disconnect window are processed — typically within
    seconds of reconnect rather than waiting up to 15 min for the monitor.
    """
    try:
        from broker import get_api, _retry
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        _log.info("[stream] A1: Fetching orders filled since %s (reconnect recovery)",
                  since.strftime("%H:%M:%S UTC"))

        closed_orders = _retry(
            get_api().get_orders,
            GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=since,
                limit=50,
            ),
        )

        if not closed_orders:
            _log.debug("[stream] A1: No missed fills since %s", since.strftime("%H:%M:%S"))
            return

        replayed = 0
        for order in closed_orders:
            # Only replay filled orders (not cancelled/expired)
            filled_qty = float(order.filled_qty or 0)
            if filled_qty <= 0:
                continue

            # Build a synthetic trade-update-like object and replay it
            class _SyntheticUpdate:
                pass

            upd = _SyntheticUpdate()
            upd.event = "fill"
            upd.order = {
                "symbol":             order.symbol,
                "side":               order.side.value,
                "filled_qty":         filled_qty,
                "filled_avg_price":   float(order.filled_avg_price or 0),
                "qty":                float(order.qty or filled_qty),
                "order_type":         order.order_type.value if order.order_type else "",
                "type":               order.order_type.value if order.order_type else "",
            }
            _log.info("[stream] A1: Replaying missed fill — %s %s %.0fsh @ $%.2f",
                      order.symbol, order.side.value, filled_qty,
                      float(order.filled_avg_price or 0))
            _handle_trade_update(upd)
            replayed += 1

        if replayed:
            _log.info("[stream] A1: Replayed %d missed fill(s) from disconnect window", replayed)
            _tg(f"🔄 *Missed fills recovered* — {replayed} fill(s) replayed after stream reconnect")
        else:
            _log.debug("[stream] A1: No filled orders in disconnect window")

    except Exception as e:
        _log.warning("[stream] A1: Missed-fill recovery failed: %s", e)


def _is_connected(stream: TradingStream) -> bool:
    """Return True if TradingStream has successfully authenticated."""
    try:
        return bool(stream._running)
    except Exception:
        return False


def _stop_stream(stream: TradingStream) -> None:
    """Stop stream safely from a non-asyncio thread."""
    try:
        stream.stop()
    except Exception:
        # _loop may be None if the event loop hasn't started yet
        try:
            stream._should_run = False
        except Exception:
            pass


def _stream_loop(stop_event: threading.Event) -> None:
    global _last_disconnect_ts   # A1: declared at top so reads and writes are both valid
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

    key    = os.getenv("THREE_MASTERS_ALPACA_API_KEY", "")
    secret = os.getenv("THREE_MASTERS_ALPACA_SECRET_KEY", "")
    base   = os.getenv("THREE_MASTERS_ALPACA_URL", "https://paper-api.alpaca.markets")

    if not key or not secret:
        _log.warning("[stream] No Alpaca credentials — order stream disabled")
        return

    _is_paper = "paper" in base.lower()

    fail_count = 0
    alerted    = False

    while not stop_event.is_set():
        t_start = time.monotonic()

        stream = TradingStream(key, secret, paper=_is_paper)

        @stream.subscribe_trade_updates
        async def on_trade_update(data):
            _handle_trade_update(data)

        # Run stream in a sub-thread so we can stop it externally on auth timeout.
        # TradingStream._run_forever catches auth errors internally with a 10ms
        # retry delay and never raises to the caller — we must interrupt from outside.
        stream_thread = threading.Thread(
            target=stream.run, daemon=True, name="stream-ws"
        )
        stream_thread.start()

        # Poll for successful auth (stream._running goes True only after auth succeeds)
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
            # Auth timed out — library stuck in its internal retry loop.
            # Stop it cleanly and apply exponential backoff before next attempt.
            fail_count += 1
            delay = min(_RECONNECT_BASE * (2 ** (fail_count - 1)), _RECONNECT_MAX)
            _log.warning(
                "[stream] Auth timeout after %ds (fail #%d) — waiting %ds before retry",
                _CONNECT_TIMEOUT, fail_count, delay,
            )
            _stop_stream(stream)
            stream_thread.join(timeout=10)

            if fail_count >= _ALERT_FAIL_THRESHOLD and not alerted:
                _tg(
                    f"⚠️ *Three Masters — WebSocket stream down*\n"
                    f"Failed to authenticate {fail_count}× in a row.\n"
                    f"Retrying every {delay // 60 or 1}min. "
                    f"Position monitor (15 min) still active."
                )
                alerted = True

            stop_event.wait(delay)
            continue

        # Successfully authenticated — A1: replay any fills missed during disconnect
        if fail_count > 0:
            _log.info("[stream] Auth recovered after %d failure(s)", fail_count)
            if alerted:
                _tg("✅ *Three Masters — WebSocket stream recovered*")
        fail_count = 0
        alerted    = False
        _log.info("[stream] Connected to Alpaca trade stream")

        # A1: fetch fills that arrived while the stream was down
        with _last_disconnect_lock:
            _missed_since = _last_disconnect_ts
        if _missed_since is not None:
            _fetch_missed_fills(_missed_since)
            with _last_disconnect_lock:
                _last_disconnect_ts = None  # reset after recovery (global declared at top)

        # Monitor the connected stream every 60 s instead of a bare join().
        # A bare join() hangs forever if the WS stalls without firing a disconnect.
        _REST_PING_INTERVAL = 90   # ping Alpaca REST every 90 s to verify auth
        _t_last_ping = time.monotonic()

        while not stop_event.is_set() and stream_thread.is_alive():
            now = time.monotonic()
            if now - _t_last_ping >= _REST_PING_INTERVAL:
                try:
                    import broker as _bk_ping
                    _bk_ping.get_account()
                    _t_last_ping = now
                except Exception as _ping_err:
                    _log.error("[stream] REST ping failed — forcing reconnect: %s", _ping_err)
                    _stop_stream(stream)
                    break
            stop_event.wait(10)

        if stop_event.is_set():
            _stop_stream(stream)
            stream_thread.join(timeout=10)
            break

        # Give stream thread a moment to clean up before we reconnect
        stream_thread.join(timeout=10)

        # A1: record disconnect time so the next successful auth can replay missed fills
        with _last_disconnect_lock:
            _last_disconnect_ts = datetime.now(timezone.utc)

        elapsed = time.monotonic() - t_start
        _log.info(
            "[stream] Disconnected after %.0fs — reconnecting in %ds",
            elapsed, _RECONNECT_BASE,
        )
        stop_event.wait(_RECONNECT_BASE)

    _log.info("[stream] Order fill stream stopped")


def start(stop_event: threading.Event) -> threading.Thread | None:
    """Start fill-notification stream in a daemon thread."""
    t = threading.Thread(
        target=_stream_loop, args=(stop_event,),
        daemon=True, name="order-stream",
    )
    t.start()
    _log.info("[stream] Order fill stream started")
    return t
