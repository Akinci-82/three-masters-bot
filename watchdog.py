"""
Three Masters Bot — Watchdog
Run every 15 minutes via systemd timer (three-masters-watchdog.timer).
Reads logs/heartbeat.json. If stale (> STALE_MINUTES):
  1. Auto-restart via `systemctl --user start three-masters-bot` (no sudo needed)
  2. Send Telegram alert (with cooldown to avoid spam)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE          = Path(__file__).parent
HEARTBEAT     = BASE / "logs" / "heartbeat.json"
ALERT_FLAG    = BASE / "logs" / "watchdog_alerted.json"
RESTART_LOG   = BASE / "logs" / "watchdog_restart.log"
ENV_FILE      = BASE / ".env"
VENV_PYTHON   = BASE / "venv" / "bin" / "python"
MAIN_PY       = BASE / "main.py"
SERVICE_NAME  = "three-masters-bot.service"

STALE_MINUTES    = 20   # heartbeat updates every 60s; 20 min covers slow startup
COOLDOWN_MINUTES = 120  # don't send Telegram more than once per 2 hours


def _heartbeat_age_minutes() -> float | None:
    if not HEARTBEAT.exists():
        return None
    try:
        data = json.loads(HEARTBEAT.read_text())
        ts   = data.get("last_run") or data.get("last_heartbeat")
        if not ts:
            return None
        dt  = datetime.fromisoformat(ts)
        from datetime import timezone as _tz
        dt_utc = dt.astimezone(_tz.utc) if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
        return round((datetime.now(_tz.utc) - dt_utc).total_seconds() / 60, 1)
    except Exception:
        return None


def _process_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "three-masters-bot/main.py"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _restart() -> str:
    # systemctl --user — no sudo required
    try:
        r = subprocess.run(
            ["systemctl", "--user", "start", SERVICE_NAME],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return "restarted via systemctl --user"
    except Exception:
        pass

    if _process_running():
        return "process already running (heartbeat lag?)"

    # Direct launch fallback
    try:
        log_path = BASE / "logs" / "watchdog_stdout.log"
        with open(log_path, "a") as lf:
            subprocess.Popen(
                [str(VENV_PYTHON), str(MAIN_PY)],
                cwd=str(BASE),
                stdout=lf, stderr=lf,
                start_new_session=True,
            )
        return "restarted via direct launch"
    except Exception as e:
        return f"restart FAILED: {e}"


def _log_restart(age: float, status: str) -> None:
    RESTART_LOG.parent.mkdir(exist_ok=True)
    with open(RESTART_LOG, "a") as f:
        f.write(f"{datetime.now().isoformat()} | age={age:.0f}min | {status}\n")


def _alert_on_cooldown() -> bool:
    try:
        data = json.loads(ALERT_FLAG.read_text())
        last = datetime.fromisoformat(data.get("alerted_at", ""))
        return (datetime.now() - last).total_seconds() / 60 < COOLDOWN_MINUTES
    except Exception:
        return False


def _record_alert() -> None:
    ALERT_FLAG.parent.mkdir(exist_ok=True)
    ALERT_FLAG.write_text(json.dumps({"alerted_at": datetime.now().isoformat()}))


def _clear_alert() -> None:
    try:
        ALERT_FLAG.unlink()
    except FileNotFoundError:
        pass


def _send_telegram(msg: str) -> None:
    try:
        import requests
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[watchdog] Telegram error: {e}")


def run() -> None:
    age = _heartbeat_age_minutes()

    if age is None:
        print("[watchdog] heartbeat.json missing")
        if not _process_running():
            status = _restart()
            _log_restart(999, f"no heartbeat — {status}")
            if not _alert_on_cooldown():
                _send_telegram(
                    "🔄 *Three Masters Watchdog*\n"
                    "heartbeat.json missing — bot restarted\n"
                    f"`{status}`"
                )
                _record_alert()
        return

    print(f"[watchdog] Last heartbeat: {age:.1f} min ago", end="")

    if age <= STALE_MINUTES:
        print(" — OK ✓")
        _clear_alert()
        return

    print(f" — STALE (>{STALE_MINUTES} min)")
    status = _restart()
    print(f"[watchdog] {status}")
    _log_restart(age, status)

    if not _alert_on_cooldown():
        if "already running" in status:
            # Process is live — heartbeat lag, not a real outage. Log but don't alert.
            print(f"[watchdog] Suppressing alert — {status}")
        else:
            _send_telegram(
                f"🔄 *Three Masters Watchdog* — Bot restarted\n"
                f"Last heartbeat: *{age:.0f} min ago*\n"
                f"`{status}`"
            )
            _record_alert()


if __name__ == "__main__":
    run()
