"""
Flask dashboard — read-only web UI for Three Masters Bot.
Runs on port 5001 during bot runtime.

Endpoints:
  GET /          — HTML dashboard
  GET /api/state — full bot state JSON
"""
from __future__ import annotations
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)
BASE = Path(__file__).parent


def _read_json(path: Path) -> dict | list:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _read_jsonl(path: Path, tail: int = 20) -> list:
    try:
        if path.exists():
            lines = path.read_text().strip().splitlines()
            return [json.loads(l) for l in lines[-tail:] if l]
    except Exception:
        pass
    return []


def create_app():
    from flask import Flask, jsonify, render_template_string
    app = Flask(__name__)

    DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="60">
  <title>Three Masters Bot</title>
  <style>
    body { font-family: monospace; background:#0d1117; color:#c9d1d9; margin:0; padding:20px; }
    h1 { color:#58a6ff; border-bottom:1px solid #30363d; padding-bottom:8px; }
    h2 { color:#79c0ff; margin-top:24px; }
    .card { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:16px; margin:12px 0; }
    .green { color:#3fb950; } .red { color:#f85149; } .yellow { color:#d29922; }
    table { width:100%; border-collapse:collapse; }
    th { color:#8b949e; font-weight:normal; text-align:left; padding:4px 8px; border-bottom:1px solid #21262d; }
    td { padding:4px 8px; }
    tr:hover { background:#1c2128; }
    .badge { padding:2px 8px; border-radius:12px; font-size:12px; }
    .badge-green { background:#1a4d2e; color:#3fb950; }
    .badge-red   { background:#4d1a1a; color:#f85149; }
    .badge-blue  { background:#1a2d4d; color:#58a6ff; }
    .meta { color:#8b949e; font-size:12px; }
  </style>
</head>
<body>
  <h1>Three Masters Bot</h1>
  <p class="meta">Auto-refresh every 60s &nbsp;|&nbsp; {{ now }}</p>

  <div class="card">
    <h2>Account</h2>
    <table>
      <tr><th>Equity</th><td><b>${{ state.equity }}</b></td></tr>
      <tr><th>Heat</th><td class="{{ 'red' if state.heat_pct > 7 else 'green' }}">{{ state.heat_pct }}%</td></tr>
      <tr><th>Day P&amp;L</th><td class="{{ 'red' if state.day_pnl < 0 else 'green' }}">{{ state.day_pnl }}%</td></tr>
      <tr><th>Consecutive losses</th><td>{{ state.losses }}</td></tr>
      <tr><th>Status</th><td>
        {% if state.halted %}
          <span class="badge badge-red">HALTED: {{ state.halt_reason }}</span>
        {% else %}
          <span class="badge badge-green">ACTIVE</span>
        {% endif %}
      </td></tr>
    </table>
  </div>

  {% if state.positions %}
  <div class="card">
    <h2>Open Positions ({{ state.positions | length }})</h2>
    <table>
      <tr><th>Symbol</th><th>Qty</th><th>Avg Cost</th><th>Current</th><th>P&amp;L %</th><th>P&amp;L $</th></tr>
      {% for p in state.positions %}
      <tr>
        <td><b>{{ p.symbol }}</b></td>
        <td>{{ p.qty }}</td>
        <td>${{ p.avg_cost }}</td>
        <td>${{ p.current }}</td>
        <td class="{{ 'green' if p.pnl_pct >= 0 else 'red' }}">{{ p.pnl_pct }}%</td>
        <td class="{{ 'green' if p.pnl_usd >= 0 else 'red' }}">${{ p.pnl_usd }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}

  {% if state.orders %}
  <div class="card">
    <h2>Pending Buy-Stops ({{ state.orders | length }})</h2>
    <table>
      <tr><th>Symbol</th><th>Qty</th><th>Stop Price</th><th>Current</th><th>Gap</th></tr>
      {% for o in state.orders %}
      <tr>
        <td><b>{{ o.symbol }}</b></td>
        <td>{{ o.qty }}</td>
        <td>${{ o.stop }}</td>
        <td>${{ o.current }}</td>
        <td class="{{ 'yellow' if o.gap_pct < 2 else '' }}">+{{ o.gap_pct }}%</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}

  {% if state.journal %}
  <div class="card">
    <h2>Recent Trades</h2>
    <table>
      <tr><th>Date</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&amp;L %</th><th>P&amp;L $</th><th>R</th></tr>
      {% for t in state.journal %}
      <tr>
        <td class="meta">{{ t.ts[:10] }}</td>
        <td><b>{{ t.symbol }}</b></td>
        <td>${{ t.avg_cost }}</td>
        <td>${{ t.exit_price }}</td>
        <td class="{{ 'green' if t.pnl_pct >= 0 else 'red' }}">{{ t.pnl_pct }}%</td>
        <td class="{{ 'green' if t.pnl_dollar >= 0 else 'red' }}">${{ t.pnl_dollar }}</td>
        <td class="{{ 'green' if t.r_multiple >= 1 else 'red' }}">{{ t.r_multiple }}R</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}
</body>
</html>"""

    @app.route("/")
    def index():
        state = _build_state()
        return render_template_string(DASHBOARD_HTML, state=state,
                                      now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @app.route("/api/state")
    def api_state():
        return jsonify(_build_state())

    return app


def _build_state() -> dict:
    risk   = _read_json(BASE / "logs" / "risk_state.json")
    mon    = _read_json(BASE / "logs" / "monitor_state.json")
    journal = _read_jsonl(BASE / "logs" / "trade_journal.jsonl", tail=15)

    # Try to fetch live positions/orders from Alpaca
    positions, orders = [], []
    try:
        import sys; sys.path.insert(0, str(BASE))
        from broker import get_positions, get_open_orders, get_account
        import yfinance as yf
        acct      = get_account()
        equity    = round(float(acct.get("portfolio_value", 0)), 0)
        positions_raw = get_positions()
        orders_raw    = [o for o in get_open_orders()
                         if o.get("side") == "buy" and o.get("type") == "stop"]

        for p in positions_raw:
            avg = float(p["avg_entry_price"])
            cur = float(p["current_price"])
            qty = int(float(p["qty"]))
            positions.append({
                "symbol":   p["symbol"],
                "qty":      qty,
                "avg_cost": round(avg, 2),
                "current":  round(cur, 2),
                "pnl_pct":  round((cur - avg) / avg * 100, 2),
                "pnl_usd":  round((cur - avg) * qty, 2),
            })

        for o in orders_raw:
            sym  = o["symbol"]
            stop = float(o["stop_price"])
            try:
                cur = float(yf.Ticker(sym).fast_info.last_price)
            except Exception:
                cur = stop
            orders.append({
                "symbol":  sym,
                "qty":     int(float(o["qty"])),
                "stop":    round(stop, 2),
                "current": round(cur, 2),
                "gap_pct": round((stop - cur) / cur * 100, 2),
            })
    except Exception as e:
        equity = round(float(risk.get("portfolio_value", 0)), 0)
        _log.debug("[dash] Live fetch failed: %s", e)

    return {
        "equity":      equity,
        "heat_pct":    round(risk.get("open_risk_pct", 0) * 100, 1),
        "day_pnl":     round(risk.get("daily_pnl_pct", 0) * 100, 2),
        "losses":      risk.get("consecutive_losses", 0),
        "halted":      risk.get("trading_halted", False),
        "halt_reason": risk.get("halt_reason", ""),
        "positions":   positions,
        "orders":      orders,
        "journal":     list(reversed(journal)),
    }


def start(stop_event: threading.Event, port: int = 5001) -> threading.Thread | None:
    """Start Flask dashboard in a daemon thread."""
    try:
        import flask  # noqa: F401
    except ImportError:
        _log.info("[dash] Flask not installed — dashboard disabled")
        return None

    app = create_app()

    def _run():
        import logging as _logging
        wlog = _logging.getLogger("werkzeug")
        wlog.setLevel(_logging.ERROR)   # suppress Flask request logs
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()
    _log.info("[dash] Dashboard started at http://0.0.0.0:%d", port)
    return t
