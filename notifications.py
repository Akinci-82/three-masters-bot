"""
Shared Telegram notification helper.
Import `_tg` from this module in any layer to send Markdown alerts.
"""
from __future__ import annotations
import logging
import os

try:
    import httpx as _http_lib  # F5: prefer httpx (sync+async capable)
    _USE_HTTPX = True
except ImportError:
    import requests as _http_lib  # type: ignore[no-redef]
    _USE_HTTPX = False

_log = logging.getLogger(__name__)


def _tg(msg: str, parse_mode: str = "Markdown") -> bool:
    """Send a Telegram message. Returns True on success. Handles >4 000-char splitting."""
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cid = os.getenv("TELEGRAM_CHAT_ID", "")
    if not tok or not cid:
        return False
    # Telegram silently truncates >4096 chars — split at 4000 to stay safe
    if len(msg) > 4000:
        parts, rest = [], msg
        while rest:
            parts.append(rest[:4000])
            rest = rest[4000:]
        return all(_tg(p, parse_mode) for p in parts)
    try:
        payload = {"chat_id": cid, "text": msg, "parse_mode": parse_mode}
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        if _USE_HTTPX:
            r = _http_lib.post(url, json=payload, timeout=10)
        else:
            r = _http_lib.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        _log.debug("_tg suppressed: %s", e)
        return False
