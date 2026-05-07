"""
Flask dashboard — read-only web UI for Three Masters Bot.
Runs on port 5002 during bot runtime.

Endpoints:
  GET /          — HTML dashboard
  GET /api/state — full bot state JSON
"""
from __future__ import annotations
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)
BASE = Path(__file__).parent

# Sector cache: symbol → sector string (populated lazily, persists for process lifetime)
_sector_cache: dict[str, str] = {}


def _get_sector(symbol: str) -> str:
    """Return sector for symbol, cached in-process. Empty string on failure."""
    if symbol in _sector_cache:
        return _sector_cache[symbol]
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        sector = info.get("sector") or info.get("industry") or ""
        _sector_cache[symbol] = sector
    except Exception:
        _sector_cache[symbol] = ""
    return _sector_cache[symbol]


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
    """Log one equity snapshot per day for the chart."""
    path = BASE / "logs" / "equity_history.jsonl"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        existing = _read_jsonl(path, tail=500)
        if existing and existing[-1].get("date") == today:
            # Update today's value
            lines = path.read_text().strip().splitlines()
            lines[-1] = json.dumps({"date": today, "value": equity})
            path.write_text("\n".join(lines) + "\n")
        else:
            with open(path, "a") as f:
                f.write(json.dumps({"date": today, "value": equity}) + "\n")
    except Exception:
        pass


def create_app():
    from flask import Flask, jsonify, render_template_string
    app = Flask(__name__)

    DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Three Masters Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:        #080b10;
      --surface:   #0f1318;
      --surface2:  #151a21;
      --border:    rgba(255,255,255,0.07);
      --border2:   rgba(255,255,255,0.04);
      --text:      #e2e8f0;
      --muted:     #64748b;
      --green:     #22d3a5;
      --green-bg:  rgba(34,211,165,0.1);
      --red:       #f87171;
      --red-bg:    rgba(248,113,113,0.1);
      --yellow:    #fbbf24;
      --yellow-bg: rgba(251,191,36,0.1);
      --accent:    #f59e0b;
      --accent2:   #fbbf24;
    }
    body { background:var(--bg); color:var(--text); font-family:'Inter',system-ui,sans-serif; font-size:14px; line-height:1.5; min-height:100vh; padding:24px; }

    /* Header */
    .header { display:flex; align-items:center; justify-content:space-between; margin-bottom:28px; }
    .header-left { display:flex; align-items:center; gap:14px; }
    .logo { width:40px; height:40px; border-radius:10px; background:linear-gradient(135deg,var(--accent),var(--accent2)); display:flex; align-items:center; justify-content:center; font-size:20px; box-shadow:0 0 20px rgba(245,158,11,0.3); }
    .header-title h1 { font-size:20px; font-weight:700; letter-spacing:-0.3px; }
    .header-title p { font-size:12px; color:var(--muted); margin-top:1px; }
    .header-right { display:flex; align-items:center; gap:16px; }
    .countdown { font-size:12px; color:var(--muted); background:var(--surface); border:1px solid var(--border); padding:6px 12px; border-radius:8px; }
    .countdown span { color:var(--yellow); font-weight:600; font-variant-numeric:tabular-nums; }
    .refresh-dot { width:7px; height:7px; border-radius:50%; background:var(--green); animation:pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

    /* Stat grid */
    .stat-grid { display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin-bottom:20px; }
    .stat-card { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:20px 22px; position:relative; overflow:hidden; }
    .stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg,var(--accent),var(--accent2)); opacity:0.6; }
    .stat-label { font-size:11px; font-weight:500; text-transform:uppercase; letter-spacing:0.8px; color:var(--muted); margin-bottom:10px; }
    .stat-value { font-size:26px; font-weight:700; letter-spacing:-0.5px; }
    .stat-sub { font-size:12px; color:var(--muted); margin-top:6px; }

    /* Badge */
    .badge { display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:20px; font-size:12px; font-weight:600; }
    .badge-green { background:var(--green-bg); color:var(--green); }
    .badge-red   { background:var(--red-bg);   color:var(--red); }
    .badge-dot { width:6px; height:6px; border-radius:50%; background:currentColor; }

    /* Two-col layout */
    .two-col { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }

    /* Table card */
    .table-card { background:var(--surface); border:1px solid var(--border); border-radius:14px; overflow:hidden; margin-bottom:16px; }
    .table-header { padding:16px 20px; border-bottom:1px solid var(--border2); display:flex; align-items:center; justify-content:space-between; }
    .table-header h2 { font-size:13px; font-weight:600; }
    .count { font-size:11px; background:var(--surface2); border:1px solid var(--border); padding:2px 8px; border-radius:20px; color:var(--muted); }
    table { width:100%; border-collapse:collapse; }
    thead th { padding:10px 20px; font-size:11px; font-weight:500; text-transform:uppercase; letter-spacing:0.6px; color:var(--muted); text-align:left; background:var(--surface2); border-bottom:1px solid var(--border2); }
    tbody tr:hover { background:rgba(255,255,255,0.02); }
    tbody td { padding:12px 20px; border-bottom:1px solid var(--border2); font-size:13px; }
    tbody tr:last-child td { border-bottom:none; }
    .sym { font-weight:700; font-size:14px; letter-spacing:0.3px; }
    .sector-badge { font-size:11px; color:var(--muted); background:var(--surface2); border:1px solid var(--border); padding:2px 8px; border-radius:20px; white-space:nowrap; }
    .num { font-variant-numeric:tabular-nums; font-weight:500; }
    .muted { color:var(--muted); }
    .pnl-badge { display:inline-flex; padding:3px 8px; border-radius:6px; font-size:12px; font-weight:600; font-variant-numeric:tabular-nums; }
    .pnl-up   { background:var(--green-bg); color:var(--green); }
    .pnl-down { background:var(--red-bg);   color:var(--red); }
    .pnl-flat { background:var(--surface2); color:var(--muted); }
    .r-badge { display:inline-flex; padding:3px 8px; border-radius:6px; font-size:12px; font-weight:700; }
    .r-win  { background:var(--green-bg); color:var(--green); }
    .r-loss { background:var(--red-bg);   color:var(--red); }
    .gap-close { color:var(--yellow); font-weight:600; }
    .empty-row td { text-align:center; color:var(--muted); padding:28px; font-size:13px; }

    /* Pipeline */
    .pipeline { display:flex; align-items:center; padding:20px; gap:0; }
    .pipe-stage { flex:1; text-align:center; }
    .pipe-num { font-size:28px; font-weight:700; letter-spacing:-0.5px; }
    .pipe-label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.6px; margin-top:4px; }
    .pipe-sub { font-size:11px; color:var(--muted); margin-top:2px; }
    .pipe-arrow { color:var(--border); font-size:20px; padding:0 8px; flex-shrink:0; }
    .pipe-simons   .pipe-num { color:var(--blue, #60a5fa); }
    .pipe-minervini .pipe-num { color:var(--yellow); }
    .pipe-tudor    .pipe-num { color:var(--green); }

    /* Chart */
    .chart-card { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:20px; margin-bottom:16px; }
    .chart-card h2 { font-size:13px; font-weight:600; margin-bottom:16px; }
    .chart-wrap { position:relative; height:160px; }

    /* Token cost */
    .token-row { display:flex; gap:8px; padding:12px 20px; border-bottom:1px solid var(--border2); font-size:13px; align-items:center; }
    .token-row:last-child { border-bottom:none; }
    .token-model { flex:1; font-weight:500; }
    .token-calls { color:var(--muted); font-size:12px; min-width:60px; }
    .token-cost { font-variant-numeric:tabular-nums; font-weight:600; min-width:70px; text-align:right; }
    .tier-haiku  { color:#a78bfa; }
    .tier-sonnet { color:var(--yellow); }
    .tier-opus   { color:var(--green); }

    /* Perf stats */
    .perf-grid { display:grid; grid-template-columns:repeat(4,1fr); padding:16px 20px; gap:16px; }
    .perf-item { text-align:center; }
    .perf-val { font-size:22px; font-weight:700; }
    .perf-lbl { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.6px; margin-top:4px; }
  </style>
</head>
<body>
  <div class="header">
    <div class="header-left">
      <div class="logo">👑</div>
      <div class="header-title">
        <h1>Three Masters Bot</h1>
        <p>Simons &nbsp;·&nbsp; Minervini &nbsp;·&nbsp; Tudor Jones</p>
      </div>
    </div>
    <div class="header-right">
      <div class="countdown">Next scan <span id="countdown">—</span></div>
      <div class="refresh-dot" title="Live — refreshes every 30s"></div>
    </div>
  </div>

  <!-- Stat cards -->
  <div class="stat-grid" id="stat-grid">
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
      <div class="stat-label">Day P&L</div>
      <div class="stat-value" id="s-pnl">—</div>
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
  </div>

  <!-- Equity chart + Performance stats -->
  <div class="two-col">
    <div class="chart-card">
      <h2>Equity Curve</h2>
      <div class="chart-wrap"><canvas id="equity-chart"></canvas></div>
    </div>
    <div class="table-card" style="margin-bottom:0">
      <div class="table-header"><h2>Performance</h2><span class="count" id="perf-trades">—</span></div>
      <div class="perf-grid" id="perf-grid">
        <div class="perf-item"><div class="perf-val" id="p-winrate">—</div><div class="perf-lbl">Win Rate</div></div>
        <div class="perf-item"><div class="perf-val" id="p-avgr">—</div><div class="perf-lbl">Avg R</div></div>
        <div class="perf-item"><div class="perf-val" id="p-totalpnl">—</div><div class="perf-lbl">Total P&L</div></div>
        <div class="perf-item"><div class="perf-val" id="p-streak">—</div><div class="perf-lbl">Losses streak</div></div>
      </div>
    </div>
  </div>

  <!-- Pipeline -->
  <div class="table-card">
    <div class="table-header"><h2>Today's Scan Pipeline</h2><span class="count" id="scan-date">—</span></div>
    <div class="pipeline" id="pipeline">
      <div class="pipe-stage pipe-simons">
        <div class="pipe-num" id="p-screened">—</div>
        <div class="pipe-label">Universe screened</div>
      </div>
      <div class="pipe-arrow">›</div>
      <div class="pipe-stage pipe-simons">
        <div class="pipe-num" id="p-simons">—</div>
        <div class="pipe-label">Simons passed</div>
        <div class="pipe-sub">Trend Template</div>
      </div>
      <div class="pipe-arrow">›</div>
      <div class="pipe-stage pipe-minervini">
        <div class="pipe-num" id="p-minervini">—</div>
        <div class="pipe-label">Minervini VCP</div>
        <div class="pipe-sub">Pattern confirmed</div>
      </div>
      <div class="pipe-arrow">›</div>
      <div class="pipe-stage pipe-tudor">
        <div class="pipe-num" id="p-orders">—</div>
        <div class="pipe-label">Orders placed</div>
        <div class="pipe-sub">Tudor Jones sized</div>
      </div>
    </div>
    <!-- VCP symbols -->
    <div id="vcp-symbols" style="padding:0 20px 16px;display:none">
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px;margin-bottom:8px">VCP candidates</div>
      <div id="vcp-tags" style="display:flex;flex-wrap:wrap;gap:6px"></div>
    </div>
  </div>

  <!-- Positions + Orders -->
  <div class="table-card">
    <div class="table-header">
      <h2>Open Positions</h2>
      <span class="count" id="pos-count">—</span>
    </div>
    <table>
      <thead><tr><th>Symbol</th><th>Sector</th><th>Qty</th><th>Avg Cost</th><th>Current</th><th>P&L %</th><th>P&L $</th></tr></thead>
      <tbody id="positions-body"><tr class="empty-row"><td colspan="7">Loading…</td></tr></tbody>
    </table>
  </div>

  <div class="table-card" id="orders-card" style="display:none">
    <div class="table-header">
      <h2>Pending Buy-Stops</h2>
      <span class="count" id="orders-count">—</span>
    </div>
    <table>
      <thead><tr><th>Symbol</th><th>Qty</th><th>Stop</th><th>Current</th><th>Gap</th><th>R:R</th><th>Confidence</th></tr></thead>
      <tbody id="orders-body"></tbody>
    </table>
  </div>

  <!-- Token usage -->
  <div class="table-card">
    <div class="table-header"><h2>API Usage — Today</h2><span class="count" id="token-total-cost">—</span></div>
    <div id="token-body"></div>
  </div>

  <!-- Trade journal -->
  <div class="table-card">
    <div class="table-header"><h2>Recent Trades</h2><span class="count">last 15</span></div>
    <table>
      <thead><tr><th>Date</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L %</th><th>P&L $</th><th>R-Multiple</th></tr></thead>
      <tbody id="journal-body"><tr class="empty-row"><td colspan="7">No closed trades yet</td></tr></tbody>
    </table>
  </div>

  <script>
    let equityChart = null;

    function fmt(v, dec=2) { return '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits:dec, maximumFractionDigits:dec}); }
    function fmtK(v)       { return '$' + Number(v).toLocaleString('en-US', {maximumFractionDigits:0}); }
    function pct(v, sign=true) { return (sign && v > 0 ? '+' : '') + Number(v).toFixed(2) + '%'; }

    function pnlBadge(val, suffix='') {
      const cls = val > 0.05 ? 'pnl-up' : val < -0.05 ? 'pnl-down' : 'pnl-flat';
      const sign = val >= 0 ? '+' : '';
      return `<span class="pnl-badge ${cls}">${sign}${Number(val).toFixed(2)}${suffix}</span>`;
    }

    // Countdown to next scan (07:00 CET = UTC+2 in summer)
    function updateCountdown() {
      const now = new Date();
      const cet = new Date(now.toLocaleString('en-US', {timeZone:'Europe/Stockholm'}));
      const next = new Date(cet);
      next.setHours(7, 0, 0, 0);
      if (cet >= next) next.setDate(next.getDate() + 1);
      const diff = Math.floor((next - cet) / 1000);
      const h = Math.floor(diff / 3600), m = Math.floor((diff % 3600) / 60), s = diff % 60;
      document.getElementById('countdown').textContent =
        `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    }
    setInterval(updateCountdown, 1000);
    updateCountdown();

    function renderChart(history) {
      const labels = history.map(h => h.date.slice(5));
      const values = history.map(h => h.value);
      const ctx = document.getElementById('equity-chart').getContext('2d');
      if (equityChart) equityChart.destroy();
      equityChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [{
            data: values,
            borderColor: '#22d3a5',
            backgroundColor: 'rgba(34,211,165,0.08)',
            borderWidth: 2,
            pointRadius: history.length < 10 ? 4 : 2,
            pointBackgroundColor: '#22d3a5',
            fill: true,
            tension: 0.3,
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false }, tooltip: {
            callbacks: { label: ctx => ' $' + ctx.parsed.y.toLocaleString('en-US', {maximumFractionDigits:0}) }
          }},
          scales: {
            x: { grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: '#64748b', font: { size: 10 } } },
            y: { grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: '#64748b', font: { size: 10 },
              callback: v => '$' + v.toLocaleString('en-US', {maximumFractionDigits:0}) } }
          }
        }
      });
    }

    async function refresh() {
      try {
        const d = await fetch('/api/state').then(r => r.json());

        // Stat cards
        const sinceStart = d.equity_history && d.equity_history.length > 1
          ? ((d.equity - d.equity_history[0].value) / d.equity_history[0].value * 100).toFixed(2)
          : null;
        document.getElementById('s-equity').textContent = fmtK(d.equity);
        document.getElementById('s-equity-sub').textContent = sinceStart !== null
          ? `${sinceStart >= 0 ? '+' : ''}${sinceStart}% since start` : 'Since start: —';

        const heatEl = document.getElementById('s-heat');
        heatEl.textContent = d.heat_pct + '%';
        heatEl.style.color = d.heat_pct > 7 ? 'var(--red)' : 'var(--green)';

        const pnlEl = document.getElementById('s-pnl');
        pnlEl.textContent = (d.day_pnl >= 0 ? '+' : '') + d.day_pnl + '%';
        pnlEl.style.color = d.day_pnl < 0 ? 'var(--red)' : d.day_pnl > 0 ? 'var(--green)' : 'var(--text)';

        document.getElementById('s-cost').textContent = '$' + (d.token_today || 0).toFixed(3);
        document.getElementById('s-cost-sub').textContent = 'Total: $' + (d.token_total || 0).toFixed(2);

        document.getElementById('s-status').innerHTML = d.halted
          ? `<span class="badge badge-red"><span class="badge-dot"></span>Halted</span><div style="font-size:11px;color:var(--red);margin-top:6px">${d.halt_reason}</div>`
          : `<span class="badge badge-green"><span class="badge-dot"></span>Active</span>`;
        document.getElementById('s-losses').textContent = `Consec. losses: ${d.losses}`;

        // Equity chart
        if (d.equity_history && d.equity_history.length > 0) renderChart(d.equity_history);

        // Performance
        const j = d.journal || [];
        const wins = j.filter(t => t.pnl_pct >= 0).length;
        const winRate = j.length ? (wins / j.length * 100).toFixed(0) + '%' : '—';
        const avgR = j.length ? (j.reduce((s,t) => s + (t.r_multiple||0), 0) / j.length).toFixed(2) + 'R' : '—';
        const totalPnl = j.length ? '$' + j.reduce((s,t) => s + (t.pnl_dollar||0), 0).toFixed(0) : '—';
        document.getElementById('perf-trades').textContent = j.length + ' trade' + (j.length !== 1 ? 's' : '');
        document.getElementById('p-winrate').textContent  = winRate;
        document.getElementById('p-winrate').style.color  = j.length ? (wins/j.length >= 0.5 ? 'var(--green)' : 'var(--red)') : '';
        document.getElementById('p-avgr').textContent     = avgR;
        document.getElementById('p-totalpnl').textContent = totalPnl;
        document.getElementById('p-streak').textContent   = d.losses;

        // Pipeline
        const rpt = d.today_report || {};
        document.getElementById('scan-date').textContent = rpt.date || '—';
        document.getElementById('p-screened').textContent = rpt.universe_size || '—';
        document.getElementById('p-simons').textContent   = (rpt.trend_passed || []).length || '—';
        document.getElementById('p-minervini').textContent = (rpt.vcp_passed || []).length || '—';
        document.getElementById('p-orders').textContent   = (rpt.orders_placed || []).length || '—';
        const vcpSyms = rpt.vcp_passed || [];
        const vcpDiv = document.getElementById('vcp-symbols');
        if (vcpSyms.length) {
          vcpDiv.style.display = 'block';
          document.getElementById('vcp-tags').innerHTML = vcpSyms.map(s =>
            `<span style="background:var(--yellow-bg);color:var(--yellow);padding:3px 10px;border-radius:6px;font-size:12px;font-weight:600">${s}</span>`
          ).join('');
        }

        // Positions
        const pos = d.positions || [];
        document.getElementById('pos-count').textContent = pos.length + ' position' + (pos.length !== 1 ? 's' : '');
        document.getElementById('positions-body').innerHTML = pos.length
          ? pos.map(p => `<tr>
              <td><span class="sym">${p.symbol}</span></td>
              <td><span class="sector-badge">${p.sector || '—'}</span></td>
              <td class="num muted">${p.qty}</td>
              <td class="num">${fmt(p.avg_cost)}</td>
              <td class="num">${fmt(p.current)}</td>
              <td>${pnlBadge(p.pnl_pct, '%')}</td>
              <td>${pnlBadge(p.pnl_usd, '')}</td>
            </tr>`).join('')
          : '<tr class="empty-row"><td colspan="7">No open positions</td></tr>';

        // Orders
        const ord = d.orders || [];
        const ordCard = document.getElementById('orders-card');
        ordCard.style.display = ord.length ? 'block' : 'none';
        if (ord.length) {
          document.getElementById('orders-count').textContent = ord.length + ' order' + (ord.length !== 1 ? 's' : '');
          document.getElementById('orders-body').innerHTML = ord.map(o => {
            const meta = (d.today_report?.orders_placed || []).find(x => x.symbol === o.symbol) || {};
            return `<tr>
              <td><span class="sym">${o.symbol}</span></td>
              <td class="num muted">${o.qty}</td>
              <td class="num">$${o.stop}</td>
              <td class="num">$${o.current}</td>
              <td class="${o.gap_pct < 2 ? 'gap-close' : 'muted'}">+${o.gap_pct}%</td>
              <td class="muted">${meta.rr_ratio ? meta.rr_ratio + ':1' : '—'}</td>
              <td class="muted">${meta.vcp_confidence ? (meta.vcp_confidence*100).toFixed(0)+'%' : '—'}</td>
            </tr>`;
          }).join('');
        }

        // Token usage
        const tokens = d.token_breakdown || [];
        document.getElementById('token-total-cost').textContent = '$' + (d.token_today||0).toFixed(3) + ' today';
        document.getElementById('token-body').innerHTML = tokens.length
          ? tokens.map(t => {
              const cls = t.tier.includes('haiku') ? 'tier-haiku' : t.tier.includes('sonnet') ? 'tier-sonnet' : 'tier-opus';
              return `<div class="token-row">
                <div class="token-model ${cls}">${t.model}</div>
                <div class="token-calls">${t.calls} calls</div>
                <div class="token-cost">$${t.cost.toFixed(4)}</div>
              </div>`;
            }).join('')
          : '<div class="token-row"><div class="muted" style="flex:1">No API calls today</div></div>';

        // Journal
        const jrnl = d.journal || [];
        document.getElementById('journal-body').innerHTML = jrnl.length
          ? jrnl.map(t => `<tr>
              <td class="muted" style="font-size:12px">${t.ts.slice(0,10)}</td>
              <td><span class="sym">${t.symbol}</span></td>
              <td class="num">$${t.avg_cost}</td>
              <td class="num">$${t.exit_price}</td>
              <td>${pnlBadge(t.pnl_pct, '%')}</td>
              <td>${pnlBadge(t.pnl_dollar, '')}</td>
              <td><span class="r-badge ${t.r_multiple >= 1 ? 'r-win' : 'r-loss'}">${t.r_multiple}R</span></td>
            </tr>`).join('')
          : '<tr class="empty-row"><td colspan="7">No closed trades yet</td></tr>';

      } catch(e) { console.error('refresh error:', e); }
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

    return app


def _build_state() -> dict:
    risk    = _read_json(BASE / "logs" / "risk_state.json")
    journal = _read_jsonl(BASE / "logs" / "trade_journal.jsonl", tail=15)

    # Latest daily report
    report_dir = BASE / "reports"
    today_report = {}
    universe_size = 0
    if report_dir.exists():
        reports = sorted(report_dir.glob("*.json"))
        if reports:
            today_report = _read_json(reports[-1])
            # Estimate universe from sector_cache or universe_cache
            uc = _read_json(BASE / "logs" / "universe_cache.json")
            if isinstance(uc, list):
                universe_size = len(uc)
            elif isinstance(uc, dict):
                universe_size = uc.get("count", len(uc.get("symbols", [])))
        today_report["universe_size"] = universe_size or "500+"

    # Token usage — today and totals, grouped by tier
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_tokens = _read_jsonl(BASE / "logs" / "token_usage.jsonl", tail=2000)
    today_tokens = [t for t in all_tokens if t.get("date") == today_str]
    token_today = sum(t.get("cost_usd", 0) for t in today_tokens)
    token_total = sum(t.get("cost_usd", 0) for t in all_tokens)

    # Group by tier
    tier_map: dict[str, dict] = {}
    for t in today_tokens:
        tier = t.get("tier", "unknown")
        model = t.get("model", "")
        if tier not in tier_map:
            tier_map[tier] = {"tier": tier, "model": model, "calls": 0, "cost": 0.0}
        tier_map[tier]["calls"] += 1
        tier_map[tier]["cost"]  += t.get("cost_usd", 0)
    token_breakdown = sorted(tier_map.values(), key=lambda x: x["tier"])

    # Equity history
    equity_history = _read_jsonl(BASE / "logs" / "equity_history.jsonl", tail=90)

    # Live data from Alpaca
    positions, orders, equity = [], [], 0.0
    try:
        import sys; sys.path.insert(0, str(BASE))
        from broker import get_positions, get_open_orders, get_account
        import yfinance as yf
        acct = get_account()
        equity = round(float(acct.get("portfolio_value", 0)), 2)

        if equity > 0:
            _append_equity_snapshot(equity)
            equity_history = _read_jsonl(BASE / "logs" / "equity_history.jsonl", tail=90)

        for p in get_positions():
            avg = float(p["avg_entry_price"])
            cur = float(p["current_price"])
            qty = int(float(p["qty"]))
            positions.append({
                "symbol": p["symbol"], "qty": qty,
                "avg_cost": round(avg, 2), "current": round(cur, 2),
                "pnl_pct": round((cur - avg) / avg * 100, 2),
                "pnl_usd": round((cur - avg) * qty, 2),
                "sector": _get_sector(p["symbol"]),
            })

        for o in [x for x in get_open_orders() if x.get("side") == "buy" and x.get("type") == "stop"]:
            sym  = o["symbol"]
            stop = float(o["stop_price"])
            try:
                cur = float(yf.Ticker(sym).fast_info.last_price)
            except Exception:
                cur = stop
            orders.append({
                "symbol": sym, "qty": int(float(o["qty"])),
                "stop": round(stop, 2), "current": round(cur, 2),
                "gap_pct": round((stop - cur) / cur * 100, 2),
            })
    except Exception as e:
        equity = round(float(risk.get("portfolio_value", 0)), 2)
        _log.debug("[dash] Live fetch failed: %s", e)

    return {
        "equity":           equity,
        "heat_pct":         round(risk.get("open_risk_pct", 0) * 100, 1),
        "day_pnl":          round(risk.get("daily_pnl_pct", 0) * 100, 2),
        "losses":           risk.get("consecutive_losses", 0),
        "halted":           risk.get("trading_halted", False),
        "halt_reason":      risk.get("halt_reason", ""),
        "positions":        positions,
        "orders":           orders,
        "journal":          list(reversed(journal)),
        "today_report":     today_report,
        "token_today":      round(token_today, 4),
        "token_total":      round(token_total, 4),
        "token_breakdown":  token_breakdown,
        "equity_history":   equity_history,
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
        wlog = _logging.getLogger("werkzeug")
        wlog.setLevel(_logging.ERROR)
        try:
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except OSError as e:
            _log.warning("[dash] Could not bind port %d: %s", port, e)

    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()
    _log.info("[dash] Dashboard started at http://0.0.0.0:%d", port)
    return t
