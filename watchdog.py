"""
Three Masters Bot — Watchdog
Runs in a loop (--loop) inside its own Docker container, checking every 15 min.
Reads logs/heartbeat.json. If stale (> STALE_MINUTES):
  1. Auto-restart via Docker SDK (via /var/run/docker.sock)
  2. Send Telegram alert (with cooldown to avoid spam)
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime
from pathlib import Path

BASE           = Path(__file__).parent
HEARTBEAT      = BASE / "logs" / "heartbeat.json"
ALERT_FLAG     = BASE / "logs" / "watchdog_alerted.json"
RESTART_LOG    = BASE / "logs" / "watchdog_restart.log"
CONTAINER_NAME = "three-masters-bot"

STALE_MINUTES    = 20
DEADLOCK_MINUTES = 45
COOLDOWN_MINUTES = 120
LOOP_INTERVAL_S  = 15 * 60


def _docker_client():
    import docker
    return docker.DockerClient(base_url="unix:///var/run/docker.sock")


def _heartbeat_age_minutes() -> float | None:
    if not HEARTBEAT.exists():
        return None
    try:
        data = json.loads(HEARTBEAT.read_text())
        ts   = data.get("last_run") or data.get("last_heartbeat")
        if not ts:
            return None
        dt = datetime.fromisoformat(ts)
        from datetime import timezone as _tz
        dt_utc = dt.astimezone(_tz.utc) if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
        return round((datetime.now(_tz.utc) - dt_utc).total_seconds() / 60, 1)
    except Exception:
        return None


def _process_running() -> bool:
    try:
        client = _docker_client()
        container = client.containers.get(CONTAINER_NAME)
        return container.status == "running"
    except Exception:
        return False


def _restart() -> str:
    try:
        client = _docker_client()
        container = client.containers.get(CONTAINER_NAME)
        container.restart()
        return "restarted via Docker SDK"
    except Exception as e:
        return "restart FAILED: " + str(e)


def _log_restart(age: float, status: str) -> None:
    RESTART_LOG.parent.mkdir(exist_ok=True)
    with open(RESTART_LOG, "a") as f:
        f.write(datetime.now().isoformat() + " | age=" + str(int(age)) + "min | " + status + "\n")


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
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        requests.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print("[watchdog] Telegram error: " + str(e))


def run() -> None:
    age = _heartbeat_age_minutes()

    if age is None:
        print("[watchdog] heartbeat.json missing")
        if not _process_running():
            status = _restart()
            _log_restart(999, "no heartbeat — " + status)
            if not _alert_on_cooldown():
                _send_telegram(
                    "\U0001f504 *Three Masters Watchdog*\n"
                    "heartbeat.json missing — bot restarted\n"
                    "`" + status + "`"
                )
                _record_alert()
        return

    print("[watchdog] Last heartbeat: " + str(age) + " min ago", end="")

    if age <= STALE_MINUTES:
        print(" — OK ✓")
        _clear_alert()
        return

    print(" — STALE (>" + str(STALE_MINUTES) + " min)")

    if _process_running():
        if age <= DEADLOCK_MINUTES:
            print("[watchdog] Process running, scan likely in progress (" + str(int(age)) + " min) — OK")
            return
        else:
            print("[watchdog] DEADLOCK SUSPECTED — process running but " + str(int(age)) + " min no heartbeat")
            status = "process running but no heartbeat for " + str(int(age)) + " min — possible deadlock"
            _log_restart(age, status)
            if not _alert_on_cooldown():
                _send_telegram(
                    "⚠️ *Three Masters Watchdog — Possible Deadlock*\n"
                    "Process is running but no heartbeat for *" + str(int(age)) + " min*\n"
                    "Bot may be frozen — check logs"
                )
                _record_alert()
            return

    status = _restart()
    print("[watchdog] " + status)
    _log_restart(age, status)

    if not _alert_on_cooldown():
        _send_telegram(
            "\U0001f504 *Three Masters Watchdog* — Bot restarted\n"
            "Last heartbeat: *" + str(int(age)) + " min ago*\n"
            "`" + status + "`"
        )
        _record_alert()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run continuously every 15 min")
    args = parser.parse_args()

    if args.loop:
        print("[watchdog] Loop mode — checking every " + str(LOOP_INTERVAL_S // 60) + " min")
        while True:
            run()
            time.sleep(LOOP_INTERVAL_S)
    else:
        run()
