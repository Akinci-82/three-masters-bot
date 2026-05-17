"""
Flask dashboard — read-only web UI for Three Masters Bot.
Runs on port 5002 during bot runtime.

Endpoints:
  GET /               — HTML dashboard
  GET /api/state      — full bot state JSON
  GET /api/journal.csv — trade journal CSV
  GET /api/calc       — position sizing calculator
"""
from __future__ import annotations
import csv
import io
import json
import logging
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger(__name__)
BASE = Path(__file__).parent

# ── In-process caches ─────────────────────────────────────────────────────────
_sector_cache:   dict[str, str]  = {}
_earnings_cache: dict[str, dict] = {}   # sym → {checked_at, result}
_spy_cache:      dict[str, list] = {}   # start_date → [{date,value}]
_regime_cache:   dict            = {}   # date → regime string


def _compute_regime() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _regime_cache.get("date") == today:
        return _regime_cache.get("regime", "bull")
    try:
        import yfinance as yf
        df = yf.Ticker("SPY").history(period="220d", interval="1d", auto_adjust=True)
        if len(df) >= 200:
            c     = df["Close"]
            ma200 = float(c.tail(200).mean())
            cur   = float(c.iloc[-1])
            pct   = (cur - ma200) / ma200
            r     = "bull" if pct > 0.02 else "bear" if pct < -0.02 else "neutral"
            _regime_cache.update({"date": today, "regime": r})
            return r
    except Exception:
        pass
    return "bull"


def _get_sector(symbol: str) -> str:
    if symbol in _sector_cache:
        return _sector_cache[symbol]
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        _sector_cache[symbol] = info.get("sector") or info.get("industry") or ""
    except Exception:
        _sector_cache[symbol] = ""
    return _sector_cache[symbol]


def _get_earnings(symbol: str) -> dict | None:
    """Return {date, days_until} or None. Cached 24 h."""
    cached = _earnings_cache.get(symbol, {})
    if cached.get("checked_at"):
        age = (datetime.now(timezone.utc) -
               datetime.fromisoformat(cached["checked_at"])).total_seconds()
        if age < 86400:
            return cached.get("result")
    result = None
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        ed  = None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date") or cal.get("earnings_date")
            if isinstance(ed, list) and ed:
                ed = ed[0]
        if ed is not None:
            ed_date = ed.date() if hasattr(ed, "date") else ed
            days = (ed_date - datetime.now(timezone.utc).date()).days
            if -5 <= days <= 60:
                result = {"date": str(ed_date), "days_until": days}
    except Exception:
        pass
    _earnings_cache[symbol] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    return result


def _get_spy_history(equity_history: list) -> list:
    if not equity_history or len(equity_history) < 2:
        return []
    cache_key = equity_history[0]["date"]
    today_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if (cache_key in _spy_cache and _spy_cache[cache_key]
            and _spy_cache[cache_key][-1]["date"] == today_str):
        return _spy_cache[cache_key]
    try:
        import yfinance as yf
        start = equity_history[0]["date"]
        end   = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
        df    = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
        if df is None or df.empty:
            return []
        spy_close = df["Close"].squeeze()
        date_idx  = {d.strftime("%Y-%m-%d"): float(v)
                     for d, v in zip(spy_close.index, spy_close.values)}
        spy_base  = float(spy_close.iloc[0])
        port_base = equity_history[0]["value"]
        result, last_raw = [], spy_base
        for h in equity_history:
            raw = date_idx.get(h["date"], last_raw)
            last_raw = raw
            result.append({"date": h["date"], "value": round(raw / spy_base * port_base, 2)})
        _spy_cache[cache_key] = result
        return result
    except Exception as e:
        _log.debug("[dash] SPY fetch failed: %s", e)
        return []


def _calc_sharpe(equity_history: list) -> float | None:
    if len(equity_history) < 3:
        return None
    vals = [h["value"] for h in equity_history]
    rets = [(vals[i] - vals[i-1]) / vals[i-1] for i in range(1, len(vals))]
    if len(rets) < 2:
        return None
    try:
        mu  = statistics.mean(rets)
        std = statistics.stdev(rets)
        return round(mu / std * (252 ** 0.5), 2) if std else None
    except Exception:
        return None


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


def _append_equity_snapshot(equity: float):
    path  = BASE / "logs" / "equity_history.jsonl"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        existing = _read_jsonl(path, tail=500)
        if existing and existing[-1].get("date") == today:
            lines = path.read_text().strip().splitlines()
            lines[-1] = json.dumps({"date": today, "value": equity})
            path.write_text("\n".join(lines) + "\n")
        else:
            with open(path, "a") as f:
                f.write(json.dumps({"date": today, "value": equity}) + "\n")
    except Exception:
        pass


def _get_activity_feed(n: int = 20) -> list[dict]:
    """Return recent important log events (buys, sells, warnings, errors)."""
    path = BASE / "logs" / "three_masters.log"
    keywords = ("buy", "sell", "halt", "warn", "error", "position",
                 "started", "shutdown", "fill", "order", "stop", "trailing")
    result = []
    try:
        lines = path.read_text().strip().splitlines()
        for line in reversed(lines):
            low = line.lower()
            if any(kw in low for kw in keywords):
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    result.append({
                        "ts":    parts[0] + " " + parts[1],
                        "level": parts[2].strip(),
                        "msg":   parts[3],
                    })
                    if len(result) >= n:
                        break
    except Exception:
        pass
    return result


def _read_recent_logs(n: int = 30) -> list[dict]:
    path = BASE / "logs" / "three_masters.log"
    result = []
    try:
        lines = path.read_text().strip().splitlines()[-n:]
        for line in lines:
            parts = line.split(None, 3)
            if len(parts) >= 4:
                result.append({
                    "ts":     parts[0] + " " + parts[1],
                    "level":  parts[2].strip(),
                    "logger": parts[3].split()[0] if parts[3] else "",
                    "msg":    " ".join(parts[3].split()[1:]) if len(parts[3].split()) > 1 else parts[3],
                })
            else:
                result.append({"ts": "", "level": "INFO", "logger": "", "msg": line})
    except Exception:
        pass
    return result


def create_app():
    from flask import Flask, jsonify, render_template_string, Response, request as freq
    app = Flask(__name__)

    DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Three Masters Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }

    /* ── Themes ─────────────────────────────────────────────────────── */
    :root, [data-theme="dark"] {
      --bg:        #080b10; --surface:#0f1318; --surface2:#151a21;
      --border:    rgba(255,255,255,0.07); --border2:rgba(255,255,255,0.04);
      --text:      #e2e8f0; --muted:#64748b;
      --green:     #22d3a5; --green-bg:rgba(34,211,165,0.1);
      --red:       #f87171; --red-bg:rgba(248,113,113,0.1);
      --yellow:    #fbbf24; --yellow-bg:rgba(251,191,36,0.1);
      --blue:      #60a5fa; --blue-bg:rgba(96,165,250,0.1);
      --accent:    #f59e0b; --accent2:#fbbf24;
      --chart-grid:rgba(255,255,255,0.04); --chart-tick:#64748b;
    }
    [data-theme="light"] {
      --bg:        #f1f5f9; --surface:#ffffff; --surface2:#f8fafc;
      --border:    rgba(0,0,0,0.08); --border2:rgba(0,0,0,0.04);
      --text:      #0f172a; --muted:#64748b;
      --green:     #059669; --green-bg:rgba(5,150,105,0.1);
      --red:       #dc2626; --red-bg:rgba(220,38,38,0.1);
      --yellow:    #d97706; --yellow-bg:rgba(217,119,6,0.1);
      --blue:      #2563eb; --blue-bg:rgba(37,99,235,0.1);
      --accent:    #d97706; --accent2:#f59e0b;
      --chart-grid:rgba(0,0,0,0.05); --chart-tick:#94a3b8;
    }

    body { background:var(--bg); color:var(--text); font-family:'Inter',system-ui,sans-serif; font-size:14px; line-height:1.5; min-height:100vh; padding:24px; transition:background 0.2s,color 0.2s; }

    /* ── Header ─────────────────────────────────────────────────────── */
    .header { display:flex; align-items:center; justify-content:space-between; margin-bottom:28px; flex-wrap:wrap; gap:12px; }
    .header-left { display:flex; align-items:center; gap:14px; }
    .logo { width:40px; height:40px; border-radius:10px; background:linear-gradient(135deg,var(--accent),var(--accent2)); display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0; box-shadow:0 0 20px rgba(245,158,11,0.25); }
    .header-title h1 { font-size:20px; font-weight:700; letter-spacing:-0.3px; }
    .header-title p  { font-size:12px; color:var(--muted); margin-top:1px; }
    .header-right { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
    .countdown { font-size:12px; color:var(--muted); background:var(--surface); border:1px solid var(--border); padding:6px 12px; border-radius:8px; }
    .countdown span { color:var(--yellow); font-weight:600; font-variant-numeric:tabular-nums; }
    .refresh-dot { width:7px; height:7px; border-radius:50%; background:var(--green); animation:pulse 2s infinite; flex-shrink:0; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
    .theme-btn { background:var(--surface); border:1px solid var(--border); color:var(--muted); padding:6px 10px; border-radius:8px; cursor:pointer; font-size:14px; line-height:1; transition:color 0.15s; }
    .theme-btn:hover { color:var(--text); }

    /* ── Stat grid ──────────────────────────────────────────────────── */
    .stat-grid { display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin-bottom:20px; }
    .stat-card { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:20px 22px; position:relative; overflow:hidden; }
    .stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg,var(--accent),var(--accent2)); opacity:0.6; }
    .stat-label { font-size:11px; font-weight:500; text-transform:uppercase; letter-spacing:0.8px; color:var(--muted); margin-bottom:10px; }
    .stat-value { font-size:26px; font-weight:700; letter-spacing:-0.5px; }
    .stat-sub { font-size:12px; color:var(--muted); margin-top:6px; }

    /* ── Badge ──────────────────────────────────────────────────────── */
    .badge { display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:20px; font-size:12px; font-weight:600; }
    .badge-green { background:var(--green-bg); color:var(--green); }
    .badge-red   { background:var(--red-bg);   color:var(--red); }
    .badge-dot   { width:6px; height:6px; border-radius:50%; background:currentColor; }

    /* ── Layout ─────────────────────────────────────────────────────── */
    .two-col   { display:grid; grid-template-columns:2fr 1fr; gap:16px; margin-bottom:16px; }
    .three-col { display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; margin-bottom:16px; }

    /* ── Cards ──────────────────────────────────────────────────────── */
    .table-card  { background:var(--surface); border:1px solid var(--border); border-radius:14px; overflow:hidden; margin-bottom:16px; }
    .chart-card  { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:20px; }
    .table-header { padding:16px 20px; border-bottom:1px solid var(--border2); display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }
    .table-header h2 { font-size:13px; font-weight:600; }
    .table-header-right { display:flex; align-items:center; gap:10px; }
    .chart-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; flex-wrap:wrap; gap:8px; }
    .chart-header h2 { font-size:13px; font-weight:600; }
    .count { font-size:11px; background:var(--surface2); border:1px solid var(--border); padding:2px 8px; border-radius:20px; color:var(--muted); white-space:nowrap; }

    /* ── Table ──────────────────────────────────────────────────────── */
    .table-scroll { overflow-x:auto; }
    table { width:100%; border-collapse:collapse; min-width:560px; }
    thead th { padding:10px 14px; font-size:11px; font-weight:500; text-transform:uppercase; letter-spacing:0.6px; color:var(--muted); text-align:left; background:var(--surface2); border-bottom:1px solid var(--border2); white-space:nowrap; }
    tbody tr:hover { background:rgba(128,128,128,0.04); }
    tbody td { padding:10px 14px; border-bottom:1px solid var(--border2); font-size:13px; }
    tbody tr:last-child td { border-bottom:none; }
    .sym  { font-weight:700; font-size:14px; letter-spacing:0.3px; }
    .num  { font-variant-numeric:tabular-nums; font-weight:500; }
    .muted { color:var(--muted); }
    .pnl-badge { display:inline-flex; padding:3px 8px; border-radius:6px; font-size:12px; font-weight:600; font-variant-numeric:tabular-nums; }
    .pnl-up   { background:var(--green-bg); color:var(--green); }
    .pnl-down { background:var(--red-bg);   color:var(--red); }
    .pnl-flat { background:var(--surface2); color:var(--muted); }
    .r-badge  { display:inline-flex; padding:3px 8px; border-radius:6px; font-size:12px; font-weight:700; }
    .r-win  { background:var(--green-bg); color:var(--green); }
    .r-loss { background:var(--red-bg);   color:var(--red); }
    .empty-row td { text-align:center; color:var(--muted); padding:28px; font-size:13px; }
    .sector-badge { font-size:11px; color:var(--muted); background:var(--surface2); border:1px solid var(--border); padding:2px 8px; border-radius:20px; white-space:nowrap; }
    .gap-close { color:var(--yellow); font-weight:600; }

    /* ── SL progress bar ────────────────────────────────────────────── */
    .sl-bar-wrap { display:flex; align-items:center; gap:7px; min-width:110px; }
    .sl-bar-bg   { flex:1; height:5px; border-radius:3px; background:var(--surface2); overflow:hidden; }
    .sl-bar-fill { height:100%; border-radius:3px; transition:width 0.4s; }
    .sl-pct      { font-size:11px; font-variant-numeric:tabular-nums; font-weight:600; min-width:36px; text-align:right; }

    /* ── Earnings badge ─────────────────────────────────────────────── */
    .earn-warn  { font-size:11px; font-weight:600; background:var(--yellow-bg); color:var(--yellow); padding:2px 7px; border-radius:5px; white-space:nowrap; }
    .earn-ok    { font-size:11px; color:var(--muted); }
    .earn-soon  { font-size:11px; font-weight:600; background:var(--red-bg); color:var(--red); padding:2px 7px; border-radius:5px; white-space:nowrap; }

    /* ── RS badge ───────────────────────────────────────────────────── */
    .rs-high  { font-weight:700; color:var(--green); font-variant-numeric:tabular-nums; }
    .rs-mid   { font-weight:600; color:var(--yellow); font-variant-numeric:tabular-nums; }
    .rs-low   { font-weight:500; color:var(--muted); font-variant-numeric:tabular-nums; }

    /* ── Pipeline ───────────────────────────────────────────────────── */
    .pipeline { display:flex; align-items:center; padding:20px; flex-wrap:wrap; }
    .pipe-stage { flex:1; text-align:center; min-width:80px; }
    .pipe-num   { font-size:28px; font-weight:700; letter-spacing:-0.5px; }
    .pipe-label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.6px; margin-top:4px; }
    .pipe-sub   { font-size:11px; color:var(--muted); margin-top:2px; }
    .pipe-arrow { color:var(--border); font-size:20px; padding:0 8px; flex-shrink:0; }
    .pipe-simons    .pipe-num { color:var(--blue); }
    .pipe-minervini .pipe-num { color:var(--yellow); }
    .pipe-tudor     .pipe-num { color:var(--green); }

    /* ── Screener accordion ─────────────────────────────────────────── */
    .screener-toggle { width:100%; background:none; border:none; color:var(--muted); font-size:12px; padding:10px 20px; text-align:left; cursor:pointer; display:flex; align-items:center; gap:6px; border-top:1px solid var(--border2); font-family:inherit; }
    .screener-toggle:hover { color:var(--text); }
    .screener-body { display:none; padding:0 20px 16px; }
    .screener-body.open { display:block; }
    .screener-tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
    .tag-ordered { background:var(--green-bg); color:var(--green); padding:3px 10px; border-radius:6px; font-size:12px; font-weight:600; }
    .tag-vcp     { background:var(--yellow-bg); color:var(--yellow); padding:3px 10px; border-radius:6px; font-size:12px; font-weight:600; }
    .tag-trend   { background:var(--blue-bg); color:var(--blue); padding:3px 10px; border-radius:6px; font-size:12px; }

    /* ── Chart wrappers ─────────────────────────────────────────────── */
    .chart-wrap      { position:relative; height:180px; }
    .chart-wrap-sm   { position:relative; height:160px; }
    .chart-wrap-donut{ position:relative; height:160px; }

    /* ── Timeframe buttons ──────────────────────────────────────────── */
    .tf-btns { display:flex; gap:4px; }
    .tf-btn  { background:var(--surface2); border:1px solid var(--border); color:var(--muted); font-size:11px; padding:3px 9px; border-radius:6px; cursor:pointer; font-family:inherit; transition:all 0.15s; }
    .tf-btn:hover, .tf-btn.active { background:var(--accent); border-color:var(--accent); color:#000; font-weight:600; }

    /* ── Chart legend ───────────────────────────────────────────────── */
    .chart-legend { display:flex; align-items:center; gap:14px; }
    .legend-item  { display:flex; align-items:center; gap:5px; font-size:11px; color:var(--muted); }
    .legend-dot   { width:10px; height:3px; border-radius:2px; }

    /* ── DD/Sharpe badges ───────────────────────────────────────────── */
    .dd-badge { font-size:12px; padding:2px 8px; border-radius:6px; font-weight:600; background:var(--red-bg); color:var(--red); }
    .dd-ok    { background:var(--green-bg); color:var(--green); }

    /* ── Perf stats ─────────────────────────────────────────────────── */
    .perf-grid { display:grid; grid-template-columns:repeat(2,1fr); padding:16px 20px; gap:14px; }
    .perf-item { text-align:center; }
    .perf-val  { font-size:20px; font-weight:700; }
    .perf-lbl  { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.6px; margin-top:4px; }

    /* ── Token usage ────────────────────────────────────────────────── */
    .token-row   { display:flex; gap:8px; padding:11px 20px; border-bottom:1px solid var(--border2); font-size:13px; align-items:center; }
    .token-row:last-child { border-bottom:none; }
    .token-model { flex:1; font-weight:500; }
    .token-calls { color:var(--muted); font-size:12px; min-width:60px; }
    .token-cost  { font-variant-numeric:tabular-nums; font-weight:600; min-width:70px; text-align:right; }
    .tier-haiku  { color:#a78bfa; }
    .tier-sonnet { color:var(--yellow); }
    .tier-opus   { color:var(--green); }

    /* ── Position calculator ────────────────────────────────────────── */
    .calc-toggle { background:var(--surface); border:1px solid var(--border); color:var(--muted); font-size:12px; padding:6px 12px; border-radius:8px; cursor:pointer; font-family:inherit; transition:color 0.15s; }
    .calc-toggle:hover { color:var(--text); }
    .calc-panel { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:20px; margin-bottom:16px; display:none; }
    .calc-panel.open { display:block; }
    .calc-form  { display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end; margin-bottom:16px; }
    .calc-field { display:flex; flex-direction:column; gap:5px; }
    .calc-field label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.6px; }
    .calc-field input { background:var(--surface2); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; font-size:14px; font-family:inherit; width:140px; outline:none; }
    .calc-field input:focus { border-color:var(--accent); }
    .calc-btn   { background:var(--accent); border:none; color:#000; padding:9px 18px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; font-family:inherit; }
    .calc-btn:hover { opacity:0.9; }
    .calc-result { display:none; }
    .calc-result.show { display:grid; grid-template-columns:repeat(5,1fr); gap:12px; }
    .calc-out { background:var(--surface2); border:1px solid var(--border); border-radius:10px; padding:14px 16px; text-align:center; }
    .calc-out-val { font-size:20px; font-weight:700; }
    .calc-out-lbl { font-size:11px; color:var(--muted); margin-top:4px; }
    .calc-error  { color:var(--red); font-size:13px; padding:8px 0; }

    /* ── Activity feed ──────────────────────────────────────────────── */
    .activity-item { display:flex; gap:12px; padding:10px 20px; border-bottom:1px solid var(--border2); align-items:flex-start; }
    .activity-item:last-child { border-bottom:none; }
    .activity-ts  { font-size:11px; color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums; min-width:42px; }
    .activity-msg { font-size:13px; line-height:1.4; word-break:break-word; }
    .activity-warn  { color:var(--yellow); }
    .activity-error { color:var(--red); }

    /* ── Log viewer ─────────────────────────────────────────────────── */
    .log-wrap { max-height:260px; overflow-y:auto; padding:12px 20px; font-family:'SF Mono',ui-monospace,monospace; font-size:11.5px; line-height:1.7; }
    .log-line  { display:flex; gap:10px; }
    .log-ts    { color:var(--muted); white-space:nowrap; flex-shrink:0; }
    .log-lvl   { font-weight:600; flex-shrink:0; min-width:52px; }
    .log-msg   { word-break:break-all; }
    .lvl-info  { color:var(--muted); }
    .lvl-warn  { color:var(--yellow); }
    .lvl-error { color:var(--red); }

    /* ── Buttons ────────────────────────────────────────────────────── */
    .btn-csv { font-size:11px; background:var(--surface2); border:1px solid var(--border); color:var(--muted); padding:3px 10px; border-radius:6px; cursor:pointer; text-decoration:none; font-family:inherit; transition:color 0.15s; }
    .btn-csv:hover { color:var(--text); }

    /* ── Regime badge ───────────────────────────────────────────────── */
    .regime-bull    { display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:700;background:var(--green-bg);color:var(--green);border:1px solid rgba(34,211,165,0.2); }
    .regime-neutral { display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:700;background:var(--yellow-bg);color:var(--yellow);border:1px solid rgba(251,191,36,0.2); }
    .regime-bear    { display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:700;background:var(--red-bg);color:var(--red);border:1px solid rgba(248,113,113,0.2); }

    /* ── Stat-grid with 6 cards ─────────────────────────────────────── */
    .stat-grid { grid-template-columns:repeat(6,1fr) !important; }

    /* ── Steps badges ───────────────────────────────────────────────── */
    .step-done   { display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:6px;font-size:10px;font-weight:700;background:var(--green-bg);color:var(--green); }
    .step-pending{ display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:6px;font-size:10px;font-weight:700;background:var(--surface2);color:var(--muted);border:1px solid var(--border); }
    .steps-wrap  { display:flex;gap:3px;align-items:center; }

    /* ── Vol / RS-high badges on orders ─────────────────────────────── */
    .vol-confirmed{ font-size:11px;font-weight:700;color:var(--green); }
    .rs-at-high   { font-size:11px;font-weight:700;color:var(--yellow);margin-left:4px; }

    /* ── Composite / quality score ──────────────────────────────────── */
    .cs-badge { font-size:12px;font-weight:700;font-variant-numeric:tabular-nums; }
    .cs-high  { color:var(--green); }
    .cs-mid   { color:var(--yellow); }
    .cs-low   { color:var(--muted); }

    /* ── Column header tooltip ──────────────────────────────────────── */
    thead th[title] { cursor:help;border-bottom:1px dashed var(--border); }

    /* ── Signal accuracy panel ──────────────────────────────────────── */
    .sig-grid { display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;padding:16px 20px; }
    .sig-row  { display:flex;align-items:center;justify-content:space-between;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:8px 12px;gap:8px; }
    .sig-name { font-size:12px;color:var(--text);flex:1; }
    .sig-stats{ display:flex;align-items:center;gap:8px;font-size:11px;font-variant-numeric:tabular-nums; }
    .sig-wr   { font-weight:700; }
    .sig-r    { color:var(--muted); }
    .sig-n    { color:var(--muted);font-size:10px; }
    .sig-empty{ padding:20px;text-align:center;color:var(--muted);font-size:13px; }

    /* ── Risk state panel ───────────────────────────────────────────── */
    .risk-grid  { display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;padding:16px 20px; }
    .risk-tile  { background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px 14px; }
    .risk-tile-lbl { font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:0.7px;color:var(--muted);margin-bottom:6px; }
    .risk-tile-val { font-size:18px;font-weight:700;letter-spacing:-0.3px; }
    .risk-tile-bar { margin-top:7px; }
    .risk-bar-track { height:4px;border-radius:2px;background:var(--border);overflow:hidden;margin-top:4px; }
    .risk-bar-fill  { height:100%;border-radius:2px;transition:width 0.4s; }
    .risk-tile-sub  { font-size:11px;color:var(--muted);margin-top:4px; }

    /* ── Stop price cell ────────────────────────────────────────────── */
    .stop-raised { font-size:10px;color:var(--green);margin-left:4px; }

    /* ── Mobile ─────────────────────────────────────────────────────── */
    @media (max-width:1200px) { .stat-grid { grid-template-columns:repeat(3,1fr) !important; } }
    @media (max-width:1100px) { .three-col { grid-template-columns:1fr 1fr; } }
    @media (max-width:900px)  { body { padding:16px; } .stat-grid { grid-template-columns:repeat(2,1fr) !important; } .two-col { grid-template-columns:1fr; } .three-col { grid-template-columns:1fr; } }
    @media (max-width:540px)  { .stat-grid { grid-template-columns:1fr 1fr !important; } .header-right { display:none; } .pipe-arrow { display:none; } .pipeline { justify-content:space-around; } .calc-result.show { grid-template-columns:repeat(2,1fr); } }
  </style>
</head>
<body>

  <!-- ── Header ──────────────────────────────────────────────────────── -->
  <div class="header">
    <div class="header-left">
      <div class="logo">&#x1F451;</div>
      <div class="header-title">
        <h1>Three Masters Bot</h1>
        <p>Simons &nbsp;&middot;&nbsp; Minervini &nbsp;&middot;&nbsp; Tudor Jones</p>
      </div>
    </div>
    <div class="header-right">
      <span id="regime-badge"></span>
      <button class="calc-toggle" onclick="document.getElementById('calc-panel').classList.toggle('open')">&#x1F9EE; Position Calculator</button>
      <div class="countdown">Next scan <span id="countdown">—</span></div>
      <button class="theme-btn" onclick="toggleTheme()" title="Toggle dark/light">&#x25D1;</button>
      <div class="refresh-dot" title="Live — refreshes every 30s"></div>
    </div>
  </div>

  <!-- ── Stat cards ──────────────────────────────────────────────────── -->
  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">Portfolio Equity</div>
      <div class="stat-value" id="s-equity">—</div>
      <div class="stat-sub" id="s-equity-sub">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Portfolio Heat</div>
      <div class="stat-value" id="s-heat">—</div>
      <div class="stat-sub">Max 8%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Day P&amp;L</div>
      <div class="stat-value" id="s-pnl">—</div>
      <div class="stat-sub" id="s-daypnl-usd">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Today's API Cost</div>
      <div class="stat-value" id="s-cost">—</div>
      <div class="stat-sub" id="s-cost-sub">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Status</div>
      <div id="s-status" style="margin-top:4px">—</div>
      <div class="stat-sub" id="s-losses" style="margin-top:10px">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label" title="Andel S&amp;P 500-aktier ovanför sitt 50-dagars glidande medelvärde">Market Breadth</div>
      <div class="stat-value" id="s-breadth">—</div>
      <div class="stat-sub" id="s-breadth-sub">% above MA50</div>
    </div>
  </div>

  <!-- ── Risk State Panel ─────────────────────────────────────────────── -->
  <div class="table-card" style="margin-bottom:16px">
    <div class="table-header">
      <h2>Risk State — Tudor Jones Circuit Breakers</h2>
      <span id="risk-halt-badge"></span>
    </div>
    <div class="risk-grid" id="risk-state-grid"></div>
  </div>

  <!-- ── Position Calculator ─────────────────────────────────────────── -->
  <div class="calc-panel" id="calc-panel">
    <h2 style="font-size:13px;font-weight:600;margin-bottom:16px">Position Sizing Calculator</h2>
    <div class="calc-form">
      <div class="calc-field">
        <label>Symbol</label>
        <input id="calc-sym" type="text" placeholder="AAPL" style="text-transform:uppercase;width:100px">
      </div>
      <div class="calc-field">
        <label>Risk %</label>
        <input id="calc-risk" type="number" value="1" step="0.1" min="0.1" max="5" style="width:90px">
      </div>
      <div class="calc-field">
        <label>Portfolio $</label>
        <input id="calc-equity" type="number" value="5000" step="100" style="width:120px">
      </div>
      <button class="calc-btn" onclick="runCalc()">Calculate</button>
    </div>
    <div id="calc-error" class="calc-error" style="display:none"></div>
    <div class="calc-result" id="calc-result">
      <div class="calc-out"><div class="calc-out-val" id="c-price">—</div><div class="calc-out-lbl">Current Price</div></div>
      <div class="calc-out"><div class="calc-out-val" id="c-sl">—</div><div class="calc-out-lbl">Stop-Loss (2×ATR)</div></div>
      <div class="calc-out"><div class="calc-out-val" id="c-shares">—</div><div class="calc-out-lbl">Shares</div></div>
      <div class="calc-out"><div class="calc-out-val" id="c-size">—</div><div class="calc-out-lbl">Position Size</div></div>
      <div class="calc-out"><div class="calc-out-val" id="c-target">—</div><div class="calc-out-lbl">Target (3R)</div></div>
    </div>
  </div>

  <!-- ── Equity chart + Performance ──────────────────────────────────── -->
  <div class="two-col">
    <div class="chart-card">
      <div class="chart-header">
        <div>
          <h2>Equity Curve</h2>
          <div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">
            <span id="dd-badge" style="display:none" class="dd-badge"></span>
            <span id="sharpe-badge" style="display:none" class="count"></span>
          </div>
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
          <div class="tf-btns">
            <button class="tf-btn" onclick="setTf('1W')">1W</button>
            <button class="tf-btn" onclick="setTf('1M')">1M</button>
            <button class="tf-btn" onclick="setTf('3M')">3M</button>
            <button class="tf-btn active" onclick="setTf('ALL')">All</button>
          </div>
          <div class="chart-legend">
            <div class="legend-item"><div class="legend-dot" style="background:#22d3a5"></div>Portfolio</div>
            <div class="legend-item" id="spy-legend" style="display:none"><div class="legend-dot" style="background:#60a5fa"></div>SPY</div>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="equity-chart"></canvas></div>
    </div>
    <div class="table-card" style="margin-bottom:0">
      <div class="table-header"><h2>Performance</h2><span class="count" id="perf-trades">—</span></div>
      <div class="perf-grid">
        <div class="perf-item"><div class="perf-val" id="p-winrate">—</div><div class="perf-lbl">Win Rate</div></div>
        <div class="perf-item"><div class="perf-val" id="p-avgr">—</div><div class="perf-lbl">Avg R</div></div>
        <div class="perf-item"><div class="perf-val" id="p-totalpnl">—</div><div class="perf-lbl">Total P&amp;L</div></div>
        <div class="perf-item"><div class="perf-val" id="p-streak">—</div><div class="perf-lbl">Loss Streak</div></div>
        <div class="perf-item"><div class="perf-val" id="p-best">—</div><div class="perf-lbl">Best Trade</div></div>
        <div class="perf-item"><div class="perf-val" id="p-worst">—</div><div class="perf-lbl">Worst Trade</div></div>
      </div>
    </div>
  </div>

  <!-- ── 3-col charts: Sector donut | Monthly P&L | Risk metrics ─────── -->
  <div class="three-col">
    <div class="chart-card">
      <div class="chart-header"><h2>Sector Distribution</h2></div>
      <div class="chart-wrap-donut"><canvas id="sector-chart"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-header"><h2>Monthly P&amp;L</h2></div>
      <div class="chart-wrap-sm"><canvas id="monthly-chart"></canvas></div>
    </div>
    <div class="table-card" style="margin-bottom:0">
      <div class="table-header"><h2>Risk Metrics</h2></div>
      <div class="perf-grid" style="grid-template-columns:1fr 1fr">
        <div class="perf-item"><div class="perf-val" id="rm-dd">—</div><div class="perf-lbl">Drawdown</div></div>
        <div class="perf-item"><div class="perf-val" id="rm-sharpe">—</div><div class="perf-lbl">Sharpe</div></div>
        <div class="perf-item"><div class="perf-val" id="rm-heat">—</div><div class="perf-lbl">Heat</div></div>
        <div class="perf-item"><div class="perf-val" id="rm-ath">—</div><div class="perf-lbl">Port ATH</div></div>
      </div>
    </div>
  </div>

  <!-- ── Pipeline ─────────────────────────────────────────────────────── -->
  <div class="table-card">
    <div class="table-header"><h2>Today's Scan Pipeline</h2><span class="count" id="scan-date">—</span></div>
    <div class="pipeline">
      <div class="pipe-stage pipe-simons">
        <div class="pipe-num" id="p-screened">—</div>
        <div class="pipe-label">Universe</div><div class="pipe-sub">Screened</div>
      </div>
      <div class="pipe-arrow">›</div>
      <div class="pipe-stage pipe-simons">
        <div class="pipe-num" id="p-simons">—</div>
        <div class="pipe-label">Simons</div><div class="pipe-sub">Trend Template</div>
      </div>
      <div class="pipe-arrow">›</div>
      <div class="pipe-stage pipe-minervini">
        <div class="pipe-num" id="p-minervini">—</div>
        <div class="pipe-label">Minervini</div><div class="pipe-sub">VCP Pattern</div>
      </div>
      <div class="pipe-arrow">›</div>
      <div class="pipe-stage pipe-tudor">
        <div class="pipe-num" id="p-orders">—</div>
        <div class="pipe-label">Tudor Jones</div><div class="pipe-sub">Orders placed</div>
      </div>
    </div>
    <button class="screener-toggle" onclick="this.nextElementSibling.classList.toggle('open')">
      &#9660; Screener symbols (click to expand)
    </button>
    <div class="screener-body">
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px">
        <span style="color:var(--green)">&#9632;</span> Ordered &nbsp;
        <span style="color:var(--yellow)">&#9632;</span> VCP passed &nbsp;
        <span style="color:var(--blue)">&#9632;</span> Trend only
      </div>
      <div class="screener-tags" id="screener-tags"></div>
    </div>
  </div>

  <!-- ── Open Positions ───────────────────────────────────────────────── -->
  <div class="table-card">
    <div class="table-header">
      <h2>Open Positions</h2><span class="count" id="pos-count">—</span>
    </div>
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Qty</th><th>Sector</th><th>Days</th>
          <th title="BE=Breakeven höjd (+8%), P1=50% partiell exit (+15%), P2=2:a partiell exit">Steps</th>
          <th title="Relative Strength Rating mot SPY (0–100)">RS</th>
          <th title="Nästa earnings-datum">Earnings</th>
          <th>Avg Cost</th><th>Current</th><th>P&amp;L %</th><th>P&amp;L $</th>
          <th title="Maximum Adverse Excursion / Maximum Favorable Excursion — sämsta och bästa kurs sedan entry">MAE / MFE</th>
          <th title="Stop-loss: nuvarande pris och initial pris">Stop $</th>
          <th title="Avstånd i % från nuvarande pris till stop-loss">SL Distance</th>
          <th title="Maximalt riskbelopp i USD om stop triggas">Max Risk</th>
        </tr></thead>
        <tbody id="positions-body"><tr class="empty-row"><td colspan="15">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ── Pending buy-stops ────────────────────────────────────────────── -->
  <div class="table-card" id="orders-card" style="display:none">
    <div class="table-header">
      <h2>Pending Buy-Stops</h2><span class="count" id="orders-count">—</span>
    </div>
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Qty</th><th>Stop</th><th>Current</th><th>Gap</th>
          <th title="Vol=volymsbekräftad breakout, ⭐=RS-line på 52v-high">Vol / RS</th>
          <th title="Risk:Reward-ratio (mål: ≥2.5:1)">R:R</th>
          <th title="AI-konfidenspoäng från Minervini Tier 2-analys (0–100%)">Conf</th>
          <th title="Quality Score 0–5 (Minervini breakout-kvalitet)">Q</th>
          <th title="Composite Score 0–10 (Simons 60% + Minervini 30% + Tudor Jones 10%)">Score</th>
        </tr></thead>
        <tbody id="orders-body"></tbody>
      </table>
    </div>
  </div>

  <!-- ── Signal Accuracy ─────────────────────────────────────────────────── -->
  <div class="table-card">
    <button class="screener-toggle" onclick="this.nextElementSibling.classList.toggle('open')">
      &#9660; Signal Accuracy — win rate per screener-signal (klicka för att visa)
    </button>
    <div class="screener-body" id="signal-accuracy-body">
      <div class="sig-empty">Laddar…</div>
    </div>
  </div>

  <!-- ── Token usage ──────────────────────────────────────────────────── -->
  <div class="table-card">
    <div class="table-header"><h2>API Usage — Today</h2><span class="count" id="token-total-cost">—</span></div>
    <div id="token-body"></div>
  </div>

  <!-- ── Trade journal ────────────────────────────────────────────────── -->
  <div class="table-card">
    <div class="table-header">
      <h2>Trade Journal</h2>
      <div class="table-header-right">
        <span class="count" id="journal-count">—</span>
        <a href="/api/journal.csv" download="trade_journal.csv" class="btn-csv">&#8595; CSV</a>
      </div>
    </div>
    <div style="padding:10px 20px;display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--border2);align-items:center">
      <input id="jf-sym" type="text" placeholder="Symbol…" oninput="filterJournal()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px 10px;border-radius:6px;font-size:12px;width:90px;outline:none;text-transform:uppercase;font-family:inherit">
      <select id="jf-step" onchange="filterJournal()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px 10px;border-radius:6px;font-size:12px;outline:none;font-family:inherit">
        <option value="">All exit steps</option>
        <option value="stop">Stop</option>
        <option value="B1">B1 (+15%)</option>
        <option value="B2">B2 (+20%)</option>
        <option value="C">C (breakeven)</option>
        <option value="D">D (time stop)</option>
        <option value="W">W (weekly)</option>
        <option value="P">P (pyramid)</option>
      </select>
      <select id="jf-r" onchange="filterJournal()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px 10px;border-radius:6px;font-size:12px;outline:none;font-family:inherit">
        <option value="">All R</option>
        <option value="win">Winners only</option>
        <option value="loss">Losers only</option>
        <option value="1r">≥ 1R</option>
        <option value="2r">≥ 2R</option>
      </select>
    </div>
    <div class="table-scroll">
      <table>
        <thead><tr><th>Date</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>Exit Step</th><th>P&amp;L %</th><th>P&amp;L $</th><th>R-Multiple</th></tr></thead>
        <tbody id="journal-body"><tr class="empty-row"><td colspan="8">No closed trades yet</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ── Activity feed ────────────────────────────────────────────────── -->
  <div class="table-card">
    <div class="table-header"><h2>Activity Feed</h2><span class="count">key events</span></div>
    <div id="activity-body"></div>
  </div>

  <!-- ── Bot log ──────────────────────────────────────────────────────── -->
  <div class="table-card">
    <div class="table-header"><h2>Bot Log</h2><span class="count">last 30 lines</span></div>
    <div class="log-wrap" id="log-wrap">
      <div class="muted" style="text-align:center;padding:16px">Loading…</div>
    </div>
  </div>

  <script>
    // ── State ──────────────────────────────────────────────────────────────
    let equityChart = null, sectorChart = null, monthlyChart = null;
    let _fullHistory = [], _fullSpy = [];
    let _tf = 'ALL';
    let _allJournal  = [];

    // ── Theme ──────────────────────────────────────────────────────────────
    function toggleTheme() {
      const html  = document.documentElement;
      const theme = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', theme);
      localStorage.setItem('tm-theme', theme);
      if (equityChart) renderEquityChart(_fullHistory, _fullSpy);
      if (sectorChart)  sectorChart.update();
      if (monthlyChart) monthlyChart.update();
    }
    (function() {
      const saved = localStorage.getItem('tm-theme');
      if (saved) document.documentElement.setAttribute('data-theme', saved);
    })();

    // ── Countdown ──────────────────────────────────────────────────────────
    function updateCountdown() {
      const now = new Date();
      const cet = new Date(now.toLocaleString('en-US', {timeZone:'Europe/Stockholm'}));
      const next = new Date(cet); next.setHours(7,0,0,0);
      if (cet >= next) next.setDate(next.getDate()+1);
      const diff = Math.floor((next - cet) / 1000);
      const h = Math.floor(diff/3600), m = Math.floor((diff%3600)/60), s = diff%60;
      document.getElementById('countdown').textContent =
        `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    }
    setInterval(updateCountdown, 1000); updateCountdown();

    // ── Helpers ────────────────────────────────────────────────────────────
    function fmt(v,d=2)  { return '$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}); }
    function fmtK(v)     { return '$'+Number(v).toLocaleString('en-US',{maximumFractionDigits:0}); }

    function pnlBadge(val, suffix='') {
      const cls  = val > 0.05 ? 'pnl-up' : val < -0.05 ? 'pnl-down' : 'pnl-flat';
      const sign = val >= 0 ? '+' : '';
      return `<span class="pnl-badge ${cls}">${sign}${Number(val).toFixed(2)}${suffix}</span>`;
    }

    function slBar(pct) {
      if (pct === null || pct === undefined) return '<span class="muted">—</span>';
      const cl = Math.max(0,Math.min(100,pct));
      const c  = pct>10?'var(--green)':pct>5?'var(--yellow)':'var(--red)';
      return `<div class="sl-bar-wrap"><div class="sl-bar-bg"><div class="sl-bar-fill" style="width:${cl}%;background:${c}"></div></div><span class="sl-pct" style="color:${c}">${Number(pct).toFixed(1)}%</span></div>`;
    }

    function earnBadge(e) {
      if (!e) return '<span class="earn-ok">—</span>';
      const dateStr = e.date ? new Date(e.date+'T00:00:00').toLocaleDateString('sv-SE',{month:'short',day:'numeric'}) : '';
      const label   = dateStr ? `${dateStr} (${e.days_until}d)` : `${e.days_until}d`;
      if (e.days_until <= 7)  return `<span class="earn-soon" title="${e.date||''}">&#9888; ${label}</span>`;
      if (e.days_until <= 21) return `<span class="earn-warn" title="${e.date||''}">&#9675; ${label}</span>`;
      return `<span class="earn-ok" title="${e.date||''}">${label}</span>`;
    }

    function stepsBadge(p) {
      const be = p.breakeven_done  ? '<span class="step-done"  title="Breakeven höjd">BE</span>'  : '<span class="step-pending" title="Väntar på +8%">BE</span>';
      const p1 = p.partial1_done   ? '<span class="step-done"  title="50% såld vid +15%">P1</span>' : '<span class="step-pending" title="Väntar på +15%">P1</span>';
      const p2 = p.partial2_done   ? '<span class="step-done"  title="2:a partiell exit gjord">P2</span>' : '<span class="step-pending" title="Ännu ej">P2</span>';
      return `<div class="steps-wrap">${be}${p1}${p2}</div>`;
    }

    function csBadge(v) {
      if (v === null || v === undefined || v === 0) return '<span class="muted">—</span>';
      const cls = v >= 7 ? 'cs-high' : v >= 5 ? 'cs-mid' : 'cs-low';
      return `<span class="cs-badge ${cls}">${Number(v).toFixed(1)}</span>`;
    }

    function rsBadge(rs) {
      if (rs === null || rs === undefined) return '<span class="muted">—</span>';
      const cls = rs >= 80 ? 'rs-high' : rs >= 60 ? 'rs-mid' : 'rs-low';
      return `<span class="${cls}">${Number(rs).toFixed(0)}</span>`;
    }

    // ── Journal filter ─────────────────────────────────────────────────────
    function filterJournal() {
      const sym  = (document.getElementById('jf-sym').value||'').trim().toUpperCase();
      const step = document.getElementById('jf-step').value;
      const rF   = document.getElementById('jf-r').value;
      let rows   = _allJournal;
      if (sym)          rows = rows.filter(t=>(t.symbol||'').includes(sym));
      if (step)         rows = rows.filter(t=>(t.exit_step||t.reason||'').includes(step));
      if (rF==='win')   rows = rows.filter(t=>(t.pnl_pct||0)>=0);
      if (rF==='loss')  rows = rows.filter(t=>(t.pnl_pct||0)<0);
      if (rF==='1r')    rows = rows.filter(t=>(t.r_multiple||0)>=1);
      if (rF==='2r')    rows = rows.filter(t=>(t.r_multiple||0)>=2);
      document.getElementById('journal-count').textContent=rows.length+' trade'+(rows.length!==1?'s':'');
      document.getElementById('journal-body').innerHTML=rows.length
        ?rows.map(t=>`<tr>
            <td class="muted" style="font-size:12px">${(t.ts||'').slice(0,10)}</td>
            <td><a href="https://finance.yahoo.com/quote/${t.symbol}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none"><span class="sym">${t.symbol}</span></a></td>
            <td class="num">$${t.avg_cost}</td>
            <td class="num">$${t.exit_price}</td>
            <td class="muted" style="font-size:12px">${t.exit_step||t.reason||'—'}</td>
            <td>${pnlBadge(t.pnl_pct,'%')}</td>
            <td>${pnlBadge(t.pnl_dollar,'')}</td>
            <td><span class="r-badge ${(t.r_multiple||0)>=1?'r-win':'r-loss'}">${t.r_multiple!=null?t.r_multiple+'R':'—'}</span></td>
          </tr>`).join('')
        :'<tr class="empty-row"><td colspan="8">No matching trades</td></tr>';
    }

    // ── Timeframe filter ───────────────────────────────────────────────────
    function setTf(tf) {
      _tf = tf;
      document.querySelectorAll('.tf-btn').forEach(b => b.classList.toggle('active', b.textContent === tf));
      renderEquityChart(_fullHistory, _fullSpy);
    }

    function filterByTf(data) {
      if (_tf === 'ALL' || !data.length) return data;
      const days = {1:7,3:30,30:91}[parseInt(_tf)] || 7;  // fallback
      const cutoffDays = _tf==='1W'?7:_tf==='1M'?30:91;
      const cutoff = new Date(); cutoff.setDate(cutoff.getDate()-cutoffDays);
      const cutoffStr = cutoff.toISOString().slice(0,10);
      return data.filter(h => h.date >= cutoffStr);
    }

    // ── Equity chart ───────────────────────────────────────────────────────
    function renderEquityChart(history, spy) {
      const h   = filterByTf(history);
      const s   = filterByTf(spy);
      const lbl = h.map(x => x.date.slice(5));
      const ctx = document.getElementById('equity-chart').getContext('2d');
      if (equityChart) equityChart.destroy();

      const datasets = [{
        label:'Portfolio', data:h.map(x=>x.value),
        borderColor:'#22d3a5', backgroundColor:'rgba(34,211,165,0.07)',
        borderWidth:2, pointRadius:h.length<10?4:1, pointBackgroundColor:'#22d3a5',
        fill:true, tension:0.3, order:1,
      }];

      if (s.length) {
        document.getElementById('spy-legend').style.display='flex';
        datasets.push({
          label:'SPY', data:s.map(x=>x.value),
          borderColor:'#60a5fa', backgroundColor:'transparent',
          borderWidth:1.5, borderDash:[4,3], pointRadius:0,
          fill:false, tension:0.3, order:2,
        });
      }

      equityChart = new Chart(ctx, {
        type:'line', data:{labels:lbl, datasets},
        options:{
          responsive:true, maintainAspectRatio:false,
          interaction:{mode:'index',intersect:false},
          plugins:{legend:{display:false}, tooltip:{callbacks:{
            label:c=>` ${c.dataset.label}: $${c.parsed.y.toLocaleString('en-US',{maximumFractionDigits:0})}`
          }}},
          scales:{
            x:{grid:{color:'rgba(128,128,128,0.07)'}, ticks:{color:'#64748b',font:{size:10}}},
            y:{grid:{color:'rgba(128,128,128,0.07)'}, ticks:{color:'#64748b',font:{size:10},
              callback:v=>'$'+v.toLocaleString('en-US',{maximumFractionDigits:0})}}
          }
        }
      });
    }

    // ── Sector donut ───────────────────────────────────────────────────────
    function renderSectorChart(positions) {
      const counts = {};
      positions.forEach(p => { const s=p.sector||'Other'; counts[s]=(counts[s]||0)+1; });
      const labels = Object.keys(counts);
      const data   = labels.map(l=>counts[l]);
      const COLORS = ['#22d3a5','#60a5fa','#fbbf24','#f87171','#a78bfa','#34d399','#fb923c','#e879f9'];
      const ctx = document.getElementById('sector-chart').getContext('2d');
      if (sectorChart) sectorChart.destroy();
      if (!labels.length) { sectorChart=null; return; }
      sectorChart = new Chart(ctx, {
        type:'doughnut',
        data:{labels, datasets:[{data, backgroundColor:COLORS.slice(0,labels.length), borderWidth:2, borderColor:'transparent', hoverOffset:6}]},
        options:{
          responsive:true, maintainAspectRatio:false,
          cutout:'68%',
          plugins:{
            legend:{position:'right', labels:{color:'#64748b',font:{size:11},boxWidth:10,padding:10}},
            tooltip:{callbacks:{label:c=>`${c.label}: ${c.parsed} position${c.parsed!==1?'s':''}`}}
          }
        }
      });
    }

    // ── Monthly P&L bars ───────────────────────────────────────────────────
    function renderMonthlyChart(monthly) {
      const labels = monthly.map(m=>m.month.slice(0,7));
      const data   = monthly.map(m=>m.pnl);
      const colors = data.map(v=>v>=0?'rgba(34,211,165,0.7)':'rgba(248,113,113,0.7)');
      const ctx = document.getElementById('monthly-chart').getContext('2d');
      if (monthlyChart) monthlyChart.destroy();
      monthlyChart = new Chart(ctx, {
        type:'bar',
        data:{labels, datasets:[{data, backgroundColor:colors, borderRadius:4, borderSkipped:false}]},
        options:{
          responsive:true, maintainAspectRatio:false,
          plugins:{legend:{display:false}, tooltip:{callbacks:{
            label:c=>(c.parsed.y>=0?'+':'')+fmt(c.parsed.y,'')
          }}},
          scales:{
            x:{grid:{display:false}, ticks:{color:'#64748b',font:{size:10}}},
            y:{grid:{color:'rgba(128,128,128,0.07)'}, ticks:{color:'#64748b',font:{size:10},
              callback:v=>'$'+v}}
          }
        }
      });
    }

    // ── Position calculator ────────────────────────────────────────────────
    async function runCalc() {
      const sym    = document.getElementById('calc-sym').value.trim().toUpperCase();
      const risk   = document.getElementById('calc-risk').value;
      const equity = document.getElementById('calc-equity').value;
      if (!sym) return;
      const errEl = document.getElementById('calc-error');
      const resEl = document.getElementById('calc-result');
      errEl.style.display='none'; resEl.classList.remove('show');
      try {
        const r = await fetch(`/api/calc?symbol=${sym}&risk_pct=${risk}&equity=${equity}`).then(x=>x.json());
        if (r.error) { errEl.textContent=r.error; errEl.style.display='block'; return; }
        document.getElementById('c-price').textContent  = fmt(r.price);
        document.getElementById('c-sl').textContent     = fmt(r.stop_loss);
        document.getElementById('c-shares').textContent = r.shares;
        document.getElementById('c-size').textContent   = fmt(r.position_size);
        document.getElementById('c-target').textContent = fmt(r.target);
        resEl.classList.add('show');
      } catch(e) { errEl.textContent='Request failed: '+e.message; errEl.style.display='block'; }
    }
    document.addEventListener('keydown', e => {
      if (e.key==='Enter' && document.activeElement?.closest?.('#calc-panel')) runCalc();
    });

    // ── Main refresh ───────────────────────────────────────────────────────
    async function refresh() {
      try {
        const d = await fetch('/api/state').then(r=>r.json());
        _fullHistory = d.equity_history || [];
        _fullSpy     = d.spy_history    || [];

        // Stat cards
        const sinceStart = _fullHistory.length>1
          ? ((_fullHistory[_fullHistory.length-1].value - _fullHistory[0].value)/_fullHistory[0].value*100).toFixed(2)
          : null;
        document.getElementById('s-equity').textContent = fmtK(d.equity);
        document.getElementById('s-equity-sub').textContent = sinceStart!==null
          ? `${sinceStart>=0?'+':''}${sinceStart}% since start` : '—';
        const hEl = document.getElementById('s-heat');
        hEl.textContent = d.heat_pct+'%';
        hEl.style.color = d.heat_pct>7?'var(--red)':'var(--green)';
        const pEl = document.getElementById('s-pnl');
        pEl.textContent = (d.day_pnl>=0?'+':'')+d.day_pnl+'%';
        pEl.style.color = d.day_pnl<0?'var(--red)':d.day_pnl>0?'var(--green)':'var(--text)';
        const dayUsd = d.day_start_equity?((d.day_pnl/100)*d.day_start_equity):null;
        document.getElementById('s-daypnl-usd').textContent = dayUsd!==null?(dayUsd>=0?'+':'')+'$'+Math.abs(dayUsd).toFixed(0):'';
        document.getElementById('s-cost').textContent    = '$'+(d.token_today||0).toFixed(3);
        document.getElementById('s-cost-sub').textContent= 'Total: $'+(d.token_total||0).toFixed(2);
        document.getElementById('s-status').innerHTML = d.halted
          ? `<span class="badge badge-red"><span class="badge-dot"></span>Halted</span><div style="font-size:11px;color:var(--red);margin-top:6px">${d.halt_reason}</div>`
          : `<span class="badge badge-green"><span class="badge-dot"></span>Active</span>`;
        document.getElementById('s-losses').textContent = `Consec. losses: ${d.losses}`;

        // Regime badge (prefer confirmed_regime from bot's 2-scan hysteresis)
        const regime = d.confirmed_regime || d.regime || 'bull';
        const regimeCls = regime === 'bear' ? 'regime-bear' : regime === 'neutral' ? 'regime-neutral' : 'regime-bull';
        const regimeIcon = regime === 'bear' ? '🐻' : regime === 'neutral' ? '⚡' : '🐂';
        document.getElementById('regime-badge').innerHTML = `<span class="${regimeCls}">${regimeIcon} ${regime.toUpperCase()}</span>`;

        // Risk State Panel
        (function() {
          const heat     = d.heat_pct   || 0;
          const dayPnl   = d.day_pnl    || 0;
          const losses   = d.losses     || 0;
          const dd       = Math.abs(d.drawdown_pct || 0);
          const halted   = d.halted;
          const haltRsn  = d.halt_reason || '';

          function riskBar(val, limit, invert) {
            const pct = Math.min(100, Math.abs(val / limit) * 100);
            const c   = pct > 80 ? 'var(--red)' : pct > 50 ? 'var(--yellow)' : 'var(--green)';
            return `<div class="risk-bar-track"><div class="risk-bar-fill" style="width:${pct.toFixed(0)}%;background:${c}"></div></div>`;
          }

          let kellyLabel = '<span style="color:var(--green)">Normal (1.0×)</span>';
          if (losses >= 5)      kellyLabel = '<span style="color:var(--red)">Severe (0.40×)</span>';
          else if (losses >= 3) kellyLabel = '<span style="color:var(--yellow)">Reduced (0.65×)</span>';

          const pnlColor = dayPnl < -2 ? 'var(--red)' : dayPnl < 0 ? 'var(--yellow)' : 'var(--green)';
          const ddColor  = dd > 8 ? 'var(--red)' : dd > 4 ? 'var(--yellow)' : 'var(--green)';
          const heatColor= heat > 6 ? 'var(--red)' : heat > 4 ? 'var(--yellow)' : 'var(--green)';

          document.getElementById('risk-halt-badge').innerHTML = halted
            ? `<span class="badge badge-red">⛔ HALTED — ${haltRsn}</span>`
            : '';

          document.getElementById('risk-state-grid').innerHTML = `
            <div class="risk-tile">
              <div class="risk-tile-lbl">Market Regime (confirmed)</div>
              <div class="risk-tile-val"><span class="${regimeCls}">${regimeIcon} ${regime.toUpperCase()}</span></div>
              <div class="risk-tile-sub">2-scan hysteresis</div>
            </div>
            <div class="risk-tile">
              <div class="risk-tile-lbl">Portfolio Heat</div>
              <div class="risk-tile-val" style="color:${heatColor}">${heat.toFixed(1)}%</div>
              <div class="risk-tile-bar">${riskBar(heat, 8)}</div>
              <div class="risk-tile-sub">Limit: 8% — ${(8-heat).toFixed(1)}% headroom</div>
            </div>
            <div class="risk-tile">
              <div class="risk-tile-lbl">Day P&amp;L</div>
              <div class="risk-tile-val" style="color:${pnlColor}">${dayPnl>=0?'+':''}${dayPnl.toFixed(2)}%</div>
              <div class="risk-tile-bar">${riskBar(Math.abs(dayPnl), 4)}</div>
              <div class="risk-tile-sub">Circuit breaker: −4%</div>
            </div>
            <div class="risk-tile">
              <div class="risk-tile-lbl">Drawdown from ATH</div>
              <div class="risk-tile-val" style="color:${ddColor}">−${dd.toFixed(1)}%</div>
              <div class="risk-tile-bar">${riskBar(dd, 12)}</div>
              <div class="risk-tile-sub">Halt trigger: −12%</div>
            </div>
            <div class="risk-tile">
              <div class="risk-tile-lbl">Consecutive Losses</div>
              <div class="risk-tile-val" style="color:${losses>=3?'var(--red)':losses>=1?'var(--yellow)':'var(--green)'}">${losses}</div>
              <div class="risk-tile-bar">${riskBar(losses, 5)}</div>
              <div class="risk-tile-sub">Kelly: ${kellyLabel}</div>
            </div>
            <div class="risk-tile">
              <div class="risk-tile-lbl">Trading Status</div>
              <div class="risk-tile-val">${halted?'<span class="badge badge-red">HALTED</span>':'<span class="badge badge-green">ACTIVE</span>'}</div>
              <div class="risk-tile-sub">${halted?haltRsn:'All circuit breakers clear'}</div>
            </div>
          `;
        })();


        // Market breadth
        const bEl = document.getElementById('s-breadth');
        if (d.market_breadth !== null && d.market_breadth !== undefined) {
          bEl.textContent = d.market_breadth + '%';
          bEl.style.color = d.market_breadth > 60 ? 'var(--green)' : d.market_breadth < 40 ? 'var(--red)' : 'var(--yellow)';
          document.getElementById('s-breadth-sub').textContent = d.market_breadth > 50 ? '% above MA50 — broad strength' : '% above MA50 — weakening breadth';
        }

        // DD + Sharpe badges
        const ddEl = document.getElementById('dd-badge');
        if (d.drawdown_pct!==null && d.drawdown_pct!==undefined) {
          ddEl.style.display='inline'; ddEl.textContent='DD '+Math.abs(d.drawdown_pct).toFixed(1)+'%';
          ddEl.className='dd-badge'+(d.drawdown_pct<-5?'':' dd-ok');
        }
        const shEl = document.getElementById('sharpe-badge');
        if (d.sharpe_ratio!==null && d.sharpe_ratio!==undefined) {
          shEl.style.display='inline'; shEl.textContent='Sharpe '+d.sharpe_ratio;
        }

        // Charts
        renderEquityChart(_fullHistory, _fullSpy);
        renderSectorChart(d.positions||[]);
        renderMonthlyChart(d.monthly_pnl||[]);

        // Performance
        _allJournal = d.journal||[];
        const j = _allJournal;
        const wins = j.filter(t=>t.pnl_pct>=0).length;
        const wr   = j.length?(wins/j.length*100).toFixed(0)+'%':'—';
        const avgR = j.length?(j.reduce((s,t)=>s+(t.r_multiple||0),0)/j.length).toFixed(2)+'R':'—';
        const tPnl = j.length?'$'+j.reduce((s,t)=>s+(t.pnl_dollar||0),0).toFixed(0):'—';
        const pnls = j.map(t=>t.pnl_pct||0);
        const best = j.length?((Math.max(...pnls))>=0?'+':'')+Math.max(...pnls).toFixed(1)+'%':'—';
        const worst= j.length?Math.min(...pnls).toFixed(1)+'%':'—';
        document.getElementById('perf-trades').textContent = j.length+' trade'+(j.length!==1?'s':'');
        document.getElementById('p-winrate').textContent = wr;
        document.getElementById('p-winrate').style.color = j.length?(wins/j.length>=0.5?'var(--green)':'var(--red)'):'';
        document.getElementById('p-avgr').textContent = avgR;
        document.getElementById('p-totalpnl').textContent = tPnl;
        document.getElementById('p-streak').textContent = d.losses;
        document.getElementById('p-best').textContent = best;
        document.getElementById('p-best').style.color = 'var(--green)';
        document.getElementById('p-worst').textContent = worst;
        document.getElementById('p-worst').style.color = 'var(--red)';

        // Risk metrics panel
        document.getElementById('rm-dd').textContent    = d.drawdown_pct!==null?(d.drawdown_pct>=0?'+':'')+d.drawdown_pct+'%':'—';
        document.getElementById('rm-dd').style.color    = (d.drawdown_pct||0)<-5?'var(--red)':'var(--green)';
        document.getElementById('rm-sharpe').textContent= d.sharpe_ratio!==null?d.sharpe_ratio:'—';
        document.getElementById('rm-heat').textContent  = d.heat_pct+'%';
        document.getElementById('rm-heat').style.color  = d.heat_pct>7?'var(--red)':'var(--green)';
        document.getElementById('rm-ath').textContent   = d.portfolio_ath?fmtK(d.portfolio_ath):'—';

        // Pipeline
        const rpt = d.today_report||{};
        document.getElementById('scan-date').textContent   = rpt.date||'—';
        document.getElementById('p-screened').textContent  = rpt.universe_size||'—';
        document.getElementById('p-simons').textContent    = (rpt.trend_passed||[]).length||'—';
        document.getElementById('p-minervini').textContent = (rpt.vcp_passed||[]).length||'—';
        document.getElementById('p-orders').textContent    = (rpt.orders_placed||[]).length||'—';
        const sc = d.screener_results||[];
        document.getElementById('screener-tags').innerHTML = sc.map(s=>{
          const cls = s.ordered?'tag-ordered':s.vcp?'tag-vcp':'tag-trend';
          return `<span class="${cls}">${s.symbol}</span>`;
        }).join('')||'<span class="muted">No data</span>';

        // Positions
        const pos = d.positions||[];
        document.getElementById('pos-count').textContent = pos.length+' position'+(pos.length!==1?'s':'');
        document.getElementById('positions-body').innerHTML = pos.length
          ? pos.map(p=>{
              const slCell = p.stop_loss
                ? (() => {
                    const raised = p.stop_loss_initial && p.stop_loss > p.stop_loss_initial;
                    return `<span class="num" style="font-size:12px;font-variant-numeric:tabular-nums">$${p.stop_loss}`
                      + (raised ? `<span class="stop-raised" title="Höjd från $${p.stop_loss_initial}">&#9650;</span>` : '')
                      + (p.stop_loss_initial && p.stop_loss === p.stop_loss_initial ? `<span class="muted" style="font-size:10px;margin-left:4px">(initial)</span>` : '')
                      + '</span>';
                  })()
                : '<span class="muted">—</span>';
              return `<tr>
              <td><span class="sym">${p.symbol}</span></td>
              <td class="num muted">${p.qty!==undefined?p.qty:'—'}</td>
              <td><span class="sector-badge">${p.sector||'—'}</span></td>
              <td class="num muted">${p.days_held!==null?p.days_held+'d':'—'}</td>
              <td>${stepsBadge(p)}</td>
              <td>${rsBadge(p.rs_rating)}</td>
              <td>${earnBadge(p.earnings)}</td>
              <td class="num">${fmt(p.avg_cost)}</td>
              <td class="num">${fmt(p.current)}</td>
              <td>${pnlBadge(p.pnl_pct,'%')}</td>
              <td>${pnlBadge(p.pnl_usd,'')}</td>
              <td class="muted" style="font-size:12px;font-variant-numeric:tabular-nums">${p.mae_pct!==null?p.mae_pct.toFixed(1)+'% / '+p.mfe_pct.toFixed(1)+'%':'—'}</td>
              <td>${slCell}</td>
              <td>${slBar(p.sl_dist_pct)}</td>
              <td class="num muted">${p.max_risk_usd!==null?fmt(p.max_risk_usd):'—'}</td>
            </tr>`;}).join('')
          : '<tr class="empty-row"><td colspan="15">No open positions</td></tr>';

        // Orders
        const ord = d.orders||[];
        document.getElementById('orders-card').style.display = ord.length?'block':'none';
        if (ord.length) {
          document.getElementById('orders-count').textContent = ord.length+' order'+(ord.length!==1?'s':'');
          document.getElementById('orders-body').innerHTML = ord.map(o=>{
            const volCell  = o.breakout_vol ? '<span class="vol-confirmed" title="Volym bekräftad">🔥</span>' : '<span class="muted">—</span>';
            const rsHi     = o.rs_line_high ? '<span class="rs-at-high" title="RS-line på 52v-high">⭐</span>' : '';
            const tooltip  = o.vcp_notes ? ` title="${o.vcp_notes.replace(/"/g,"'")}"` : '';
            return `<tr${tooltip} style="${o.vcp_notes?'cursor:help':''}">
              <td><span class="sym">${o.symbol}</span>${rsHi}</td>
              <td class="num muted">${o.qty}</td>
              <td class="num">$${o.stop}</td>
              <td class="num">$${o.current}</td>
              <td class="${o.gap_pct<2?'gap-close':'muted'}">+${o.gap_pct}%</td>
              <td>${volCell}</td>
              <td class="muted">${o.rr_ratio?o.rr_ratio+':1':'—'}</td>
              <td class="muted">${o.vcp_confidence?(o.vcp_confidence*100).toFixed(0)+'%':'—'}</td>
              <td class="muted">${o.quality_score||'—'}</td>
              <td>${csBadge(o.composite_score)}</td>
            </tr>`;
          }).join('');
        }

        // Tokens
        const tok = d.token_breakdown||[];
        document.getElementById('token-total-cost').textContent='$'+(d.token_today||0).toFixed(3)+' today';
        document.getElementById('token-body').innerHTML = tok.length
          ? tok.map(t=>{
              const cls=t.tier.includes('haiku')?'tier-haiku':t.tier.includes('sonnet')?'tier-sonnet':'tier-opus';
              return `<div class="token-row"><div class="token-model ${cls}">${t.model}</div><div class="token-calls">${t.calls} calls</div><div class="token-cost">$${t.cost.toFixed(4)}</div></div>`;
            }).join('')
          : '<div class="token-row"><div class="muted" style="flex:1">No API calls today</div></div>';

        // Journal — filter + render via filterJournal()
        filterJournal();

        // Signal accuracy
        const sa   = d.signal_accuracy || {};
        const saEl = document.getElementById('signal-accuracy-body');
        const saEntries = Object.entries(sa).filter(([,v])=>v.wins+v.losses>0);
        const SIG_LABELS = {
          rs_line_at_high:'RS-line på 52v-high', rs_line_leading:'RS-line ledande',
          eps_accelerating:'EPS-acceleration', rev_accelerating:'Omsättningstillväxt',
          three_weeks_tight:'3 Weeks Tight', pocket_pivot:'Pocket Pivot',
          insider_buying:'Insider Buying', industry_leader:'Industry Leader',
          eps_revision_up:'EPS-revision upp', accum_weeks_strong:'Starka ackumulationsveckor',
          analyst_pt_upside:'Analytiker PT-upside', inst_ownership_increasing:'Inst. ägande ökar',
          near_ath:'Nära ATH', weekly_stage2:'Veckodiagram Stage 2',
          pead_hold:'PEAD Hold', vol_contraction_quality:'Vol-kontraktion',
          weekly_stage2:'Veckodiagram Stage 2', analyst_upgrades:'Analytiker-uppgraderingar',
          at_52w_high:'52v-high', obv_new_high:'OBV New High',
          weekly_breakout_aligned:'Weekly Breakout Aligned',
        };
        if (saEntries.length === 0) {
          saEl.innerHTML = '<div class="sig-empty">Ingen data ännu — win rates byggs upp efter avslutade trades.</div>';
        } else {
          const sorted = saEntries.sort(([,a],[,b])=>(b.wins+b.losses)-(a.wins+a.losses));
          saEl.innerHTML = '<div class="sig-grid">' + sorted.map(([key,v])=>{
            const n  = v.wins + v.losses;
            const wr = n > 0 ? (v.wins/n*100).toFixed(0)+'%' : '—';
            const avgR = n > 0 ? (v.total_r/n).toFixed(2)+'R' : '—';
            const wrColor = v.wins/n >= 0.6 ? 'color:var(--green)' : v.wins/n >= 0.4 ? 'color:var(--yellow)' : 'color:var(--red)';
            const label = SIG_LABELS[key] || key.replace(/_/g,' ');
            return `<div class="sig-row"><span class="sig-name">${label}</span><span class="sig-stats"><span class="sig-wr" style="${wrColor}">${wr}</span><span class="sig-r">${avgR}</span><span class="sig-n">(${n})</span></span></div>`;
          }).join('') + '</div>';
        }

        // Activity feed
        const acts = d.activity_feed||[];
        document.getElementById('activity-body').innerHTML = acts.length
          ? acts.map(a=>{
              const lvlCls = a.level.startsWith('WARN')?'activity-warn':a.level.startsWith('ERR')?'activity-error':'';
              return `<div class="activity-item"><span class="activity-ts">${(a.ts||'').slice(11,16)}</span><span class="activity-msg ${lvlCls}">${a.msg}</span></div>`;
            }).join('')
          : '<div class="activity-item"><span class="muted">No recent events</span></div>';

        // Log
        const logs = d.recent_logs||[];
        document.getElementById('log-wrap').innerHTML = logs.length
          ? logs.map(l=>{
              const lc=l.level.startsWith('WARN')?'lvl-warn':l.level.startsWith('ERR')?'lvl-error':'lvl-info';
              return `<div class="log-line"><span class="log-ts">${(l.ts||'').slice(11,19)}</span><span class="log-lvl ${lc}">${l.level.slice(0,4)}</span><span class="log-msg">${l.logger?`<span style="color:var(--muted)">[${l.logger}]</span> `:''}${l.msg}</span></div>`;
            }).join('')
          : '<div class="muted" style="text-align:center;padding:16px">No log data</div>';
        const lw=document.getElementById('log-wrap'); lw.scrollTop=lw.scrollHeight;

      } catch(e) { console.error('refresh error:',e); }
    }

    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>"""

    @app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/api/state")
    def api_state():
        return jsonify(_build_state())

    @app.route("/api/journal.csv")
    def journal_csv():
        rows = _read_jsonl(BASE / "logs" / "trade_journal.jsonl", tail=10000)
        buf  = io.StringIO()
        w    = csv.writer(buf)
        w.writerow(["ts","symbol","avg_cost","exit_price","pnl_pct","pnl_dollar","r_multiple"])
        for r in rows:
            w.writerow([r.get("ts",""),r.get("symbol",""),r.get("avg_cost",""),
                        r.get("exit_price",""),r.get("pnl_pct",""),
                        r.get("pnl_dollar",""),r.get("r_multiple","")])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition":"attachment; filename=trade_journal.csv"})

    @app.route("/health")
    def health():
        from datetime import timezone as _tz
        hb      = _read_json(BASE / "logs" / "heartbeat.json")
        ts      = hb.get("last_run") or hb.get("last_heartbeat", "")
        stale   = True
        age_min = None
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                age_min = round((datetime.now(_tz.utc) - dt).total_seconds() / 60, 1)
                stale   = age_min > 20
            except Exception:
                pass
        risk   = _read_json(BASE / "logs" / "risk_state.json")
        halted = bool(risk.get("trading_halted", False))
        ok     = not stale and bool(ts)
        return jsonify({
            "status":            "ok" if ok else "error",
            "last_heartbeat":    ts or None,
            "heartbeat_age_min": age_min,
            "trading_halted":    halted,
            "halt_reason":       risk.get("halt_reason", "") if halted else "",
            "service":           "running" if ok else "stale",
        }), 200 if ok else 503

    @app.route("/api/calc")
    def calc_position():
        sym      = freq.args.get("symbol","").upper().strip()
        risk_pct = float(freq.args.get("risk_pct", 1)) / 100
        equity_v = float(freq.args.get("equity", 5000))
        if not sym:
            return jsonify({"error": "Symbol required"}), 400
        try:
            import yfinance as yf
            import pandas as pd
            ticker    = yf.Ticker(sym)
            price     = float(ticker.fast_info.last_price)
            hist      = ticker.history(period="1mo")
            h, l, c   = hist["High"], hist["Low"], hist["Close"]
            tr        = pd.concat([(h-l),(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
            atr_val   = float(tr.rolling(14).mean().iloc[-1])
            risk_amt  = equity_v * risk_pct
            stop_loss = round(price - 2 * atr_val, 2)
            rps       = price - stop_loss
            shares    = int(risk_amt / rps) if rps > 0 else 0
            pos_size  = round(shares * price, 2)
            target    = round(price + 3 * rps, 2)
            return jsonify({
                "symbol": sym, "price": round(price,2), "atr": round(atr_val,2),
                "stop_loss": stop_loss, "shares": shares, "position_size": pos_size,
                "risk_amount": round(risk_amt,2), "target": target, "rr": 3.0,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    return app


def _build_state() -> dict:
    risk    = _read_json(BASE / "logs" / "risk_state.json")
    journal = _read_jsonl(BASE / "logs" / "trade_journal.jsonl", tail=10000)
    monitor = _read_json(BASE / "logs" / "monitor_state.json")

    signal_accuracy = _read_json(BASE / "logs" / "signal_accuracy.json")

    breadth_raw    = _read_json(BASE / "logs" / "breadth_history.json")
    market_breadth = None
    if isinstance(breadth_raw, list) and breadth_raw:
        market_breadth = round(float(breadth_raw[-1]) * 100, 1)

    regime = _compute_regime()

    # Latest daily report
    report_dir   = BASE / "reports"
    today_report = {}
    universe_size = 0
    rs_lookup: dict[str, float] = {}
    if report_dir.exists():
        reports = sorted(report_dir.glob("*.json"))
        if reports:
            today_report = _read_json(reports[-1])
            uc = _read_json(BASE / "logs" / "universe_cache.json")
            universe_size = len(uc) if isinstance(uc, list) else uc.get("count", 0) if isinstance(uc, dict) else 0
        today_report["universe_size"] = universe_size or "500+"
        # Build RS lookup from all reports
        for rpt_file in reports:
            rpt = _read_json(rpt_file)
            for order in rpt.get("orders_placed", []):
                sym = order.get("symbol")
                if sym and order.get("rs_rating") is not None:
                    rs_lookup[sym] = order["rs_rating"]

    # Screener results
    trend_set   = set(today_report.get("trend_passed", []))
    vcp_set     = set(today_report.get("vcp_passed", []))
    ordered_set = {o["symbol"] for o in today_report.get("orders_placed", [])}
    screener_results = sorted([
        {"symbol": sym, "trend": True, "vcp": sym in vcp_set, "ordered": sym in ordered_set}
        for sym in trend_set
    ], key=lambda x: (not x["ordered"], not x["vcp"], x["symbol"]))

    # Token usage
    today_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_tokens = _read_jsonl(BASE / "logs" / "token_usage.jsonl", tail=5000)
    today_toks = [t for t in all_tokens if t.get("date") == today_str]
    token_today = sum(t.get("cost_usd",0) for t in today_toks)
    token_total = sum(t.get("cost_usd",0) for t in all_tokens)
    tier_map: dict[str, dict] = {}
    for t in today_toks:
        tier = t.get("tier","unknown"); model = t.get("model","")
        if tier not in tier_map:
            tier_map[tier] = {"tier":tier,"model":model,"calls":0,"cost":0.0}
        tier_map[tier]["calls"] += 1
        tier_map[tier]["cost"]  += t.get("cost_usd",0)
    token_breakdown = sorted(tier_map.values(), key=lambda x: x["tier"])

    # Monthly P&L from all journal entries
    all_journal = _read_jsonl(BASE / "logs" / "trade_journal.jsonl", tail=10000)
    monthly_map: dict[str, float] = {}
    for trade in all_journal:
        ts = trade.get("ts","")
        if ts:
            month = ts[:7]
            monthly_map[month] = monthly_map.get(month,0) + trade.get("pnl_dollar",0)
    monthly_pnl = [{"month":k,"pnl":round(v,2)} for k,v in sorted(monthly_map.items())]

    # Equity history
    equity_history = _read_jsonl(BASE / "logs" / "equity_history.jsonl", tail=90)

    # Sharpe ratio
    sharpe_ratio = _calc_sharpe(equity_history)

    # Recent logs + activity feed
    recent_logs   = _read_recent_logs(30)
    activity_feed = _get_activity_feed(20)

    # Live data from Alpaca
    positions, orders, equity = [], [], 0.0
    all_stop_orders: list[dict] = []
    try:
        import sys; sys.path.insert(0, str(BASE))
        from broker import get_positions, get_open_orders, get_account
        import yfinance as yf

        acct   = get_account()
        equity = round(float(acct.get("portfolio_value",0)), 2)
        if equity > 0:
            _append_equity_snapshot(equity)
            equity_history = _read_jsonl(BASE / "logs" / "equity_history.jsonl", tail=90)
            sharpe_ratio   = _calc_sharpe(equity_history)

        all_stop_orders = [o for o in get_open_orders() if o.get("type") == "stop"]

        for p in get_positions():
            sym = p["symbol"]
            avg = float(p["avg_entry_price"])
            cur = float(p["current_price"])
            qty = int(float(p["qty"]))

            # Days held
            days_held = None
            mon = monitor.get(sym,{}) if isinstance(monitor,dict) else {}
            if mon.get("entry_date"):
                try:
                    days_held = (datetime.now(timezone.utc).date() -
                                 datetime.fromisoformat(mon["entry_date"]).date()).days
                except Exception:
                    pass

            # Stop-loss
            sl = float(mon.get("stop_loss",0)) if mon else 0.0
            if sl <= 0:
                stop_ord = next((o for o in all_stop_orders if o.get("symbol")==sym), None)
                if stop_ord:
                    sl = float(stop_ord.get("stop_price",0) or 0)

            sl_dist_pct  = round((cur-sl)/cur*100,2) if sl>0 and cur>0 else None
            max_risk_usd = round((cur-sl)*qty,2)     if sl>0 else None

            # MAE / MFE
            mae_pct = round(mon.get("mae_pct",0)*100,2) if mon.get("mae_pct") is not None else None
            mfe_pct = round(mon.get("mfe_pct",0)*100,2) if mon.get("mfe_pct") is not None else None

            sl_initial = float(mon.get("stop_loss_initial", sl)) if mon else sl
            positions.append({
                "symbol":           sym,
                "qty":              qty,
                "avg_cost":         round(avg,2),
                "current":          round(cur,2),
                "pnl_pct":          round((cur-avg)/avg*100,2),
                "pnl_usd":          round((cur-avg)*qty,2),
                "sector":           _get_sector(sym),
                "days_held":        days_held,
                "rs_rating":        rs_lookup.get(sym),
                "earnings":         _get_earnings(sym),
                "mae_pct":          mae_pct,
                "mfe_pct":          mfe_pct,
                "stop_loss":        round(sl,2) if sl>0 else None,
                "stop_loss_initial":round(sl_initial,2) if sl_initial>0 else None,
                "sl_dist_pct":      sl_dist_pct,
                "max_risk_usd":     max_risk_usd,
                "breakeven_done":   mon.get("breakeven_done", False) if mon else False,
                "partial1_done":    mon.get("partial1_done", False) if mon else False,
                "partial2_done":    mon.get("partial2_done", False) if mon else False,
                "quality_score":    mon.get("quality_score", 0) if mon else 0,
                "composite_score":  mon.get("composite_score", 0.0) if mon else 0.0,
                "measured_move_pct":mon.get("measured_move_pct", 0.0) if mon else 0.0,
            })

        rpt_order_map = {o["symbol"]: o for o in today_report.get("orders_placed", [])}
        for o in [x for x in all_stop_orders if x.get("side")=="buy"]:
            sym   = o["symbol"]
            stop  = float(o["stop_price"])
            rpt_o = rpt_order_map.get(sym, {})
            try:   cur_p = float(yf.Ticker(sym).fast_info.last_price)
            except: cur_p = stop
            orders.append({
                "symbol":          sym,
                "qty":             int(float(o["qty"])),
                "stop":            round(stop,2),
                "current":         round(cur_p,2),
                "gap_pct":         round((stop-cur_p)/cur_p*100,2),
                "breakout_vol":    rpt_o.get("breakout_vol", False),
                "vcp_notes":       rpt_o.get("vcp_notes", ""),
                "quality_score":   rpt_o.get("quality_score", 0),
                "composite_score": round(rpt_o.get("composite_score", 0.0), 2),
                "rs_line_high":    rpt_o.get("rs_line_high", False),
                "rr_ratio":        rpt_o.get("rr_ratio"),
                "vcp_confidence":  rpt_o.get("vcp_confidence"),
            })
    except Exception as e:
        equity = round(float(risk.get("portfolio_value",0)),2)
        _log.debug("[dash] Live fetch failed: %s", e)

    # Drawdown
    ath          = risk.get("portfolio_ath", equity or 1)
    drawdown_pct = round((equity-ath)/ath*100,2) if ath and equity else None

    # SPY benchmark
    spy_history = _get_spy_history(equity_history)

    return {
        "equity":           equity,
        "heat_pct":         round(risk.get("open_risk_pct",0)*100,1),
        "day_pnl":          round(risk.get("daily_pnl_pct",0)*100,2),
        "day_start_equity": risk.get("day_start_equity"),
        "losses":           risk.get("consecutive_losses",0),
        "halted":           risk.get("trading_halted",False),
        "halt_reason":      risk.get("halt_reason",""),
        "portfolio_ath":    ath,
        "drawdown_pct":     drawdown_pct,
        "sharpe_ratio":     sharpe_ratio,
        "positions":        positions,
        "orders":           orders,
        "journal":          list(reversed(journal)),
        "today_report":     today_report,
        "screener_results": screener_results,
        "monthly_pnl":      monthly_pnl,
        "token_today":      round(token_today,4),
        "token_total":      round(token_total,4),
        "token_breakdown":  token_breakdown,
        "equity_history":   equity_history,
        "spy_history":      spy_history,
        "recent_logs":      recent_logs,
        "activity_feed":    activity_feed,
        "regime":           regime,
        "market_breadth":   market_breadth,
        "signal_accuracy":  signal_accuracy,
        "confirmed_regime": risk.get("confirmed_regime", regime),
        "kelly_factor":     round(float(risk.get("open_risk_pct", 0)), 4),  # hint for panel
    }


def start(stop_event: threading.Event, port: int = 5002) -> threading.Thread | None:
    """Start Flask dashboard in a daemon thread."""
    try:
        import flask  # noqa: F401
    except ImportError:
        _log.info("[dash] Flask not installed — dashboard disabled")
        return None

    app = create_app()

    def _run():
        import logging as _logging
        _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
        try:
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except OSError as e:
            _log.warning("[dash] Could not bind port %d: %s", port, e)

    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()
    _log.info("[dash] Dashboard started at http://0.0.0.0:%d", port)
    return t
