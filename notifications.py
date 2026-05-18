"""
Shared Telegram notification helper.
Import `_tg` from this module in any layer to send Markdown alerts.
"""
from __future__ import annotations
import logging
import os

import requests

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
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": cid, "text": msg, "parse_mode": parse_mode},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        _log.debug("_tg suppressed: %s", e)
        return False
