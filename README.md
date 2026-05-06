# Three Masters Bot

A systematic swing trading bot combining three legendary investment philosophies into one pipeline. Runs daily at 22:30 CEST (after US close).

## The Three Masters

| Layer | Master | File | Role |
|-------|--------|------|------|
| 1 | **Jim Simons** | `screener.py` | Quantitative screening — Trend Template + RS-line + weekly tight base |
| 2 | **Mark Minervini** | `vcp_analyzer.py` | VCP pattern via Claude AI — Haiku→Sonnet tiered, ATR stops, vol dry-up |
| 3 | **Paul Tudor Jones** | `risk_manager.py` + `position_monitor.py` | Risk sizing, circuit breakers, time stop, exit management |

## Daily Pipeline (22:30 CEST)

```
500+ S&P 500 stocks
    │
    ▼
[Simons] Trend Template filter
  • Price > MA20, MA50, MA150, MA200 (bull stack)
  • MA200 trending up ≥ 20 days
  • Within 25% of 52-week high
  • RS rating ≥ 70 vs SPY
  • RSI ≤ 75 (not chasing)
  • Earnings ≥ 7 days away
  • Weekly chart: price above MA10w + MA40w
  • Weekly chart: last 4-week H-L range < 15% of price  ← tight base filter
  • RS line (stock/SPY ratio) at 52-week high → ⭐ priority
    │
    ▼  (typically 20-60 stocks pass)
[Minervini] VCP Analysis — Tier 0 → 1 → 2
  • Tier 0 (quant): ≥2 of 4 segments tighter, depth <35%, final zone <10%
                    Volume dry-up: 5d avg < 60% of 100d avg → vol_at_multiweek_low flag
  • Tier 1 (Haiku): short prompt — confirm/reject VCP pattern
  • Tier 2 (Sonnet): full 60-bar OHLCV — breakout level, stop, confidence, quality score
  • ATR-adjusted stop: clamps pivot low to 1-3× ATR range
    │
    ▼  (typically 2-8 confirmed VCPs)
[Market Regime Filter]
  • Bull (SPY within 3% of MA200):   full sizing
  • Neutral (SPY 3-8% below MA200):  75% sizing
  • Bear (SPY >8% below MA200):      no new orders
    │
    ▼
[Tudor Jones] Position Sizing + Risk Checks
  • Base risk: 1.5% per trade (→ 2% at high confidence)
  • Consecutive loss factor: 1 loss=75%, 2=50%, 3+=33% of base risk
  • Max positions: 8 | Max per sector: 2
  • Portfolio heat cap: 8%
  • Correlation guard: skip if r ≥ 0.80 with any held position (60-day returns)
    │
    ▼
[Execution] GTC buy-stop orders at breakout level
  • Smart order management: keeps valid unchanged orders, cancels stale/moved ones
  • Morning briefing (09:15 ET): pre-market gap check — cancel if stock >2% above stop
```

## Exit Rules (position_monitor.py — every 15 min during market hours)

| Step | Trigger | Action |
|------|---------|--------|
| A | Position first seen | Place 7% trailing stop |
| B | Gain ≥ +15% | Sell 50%, tighten trailing stop to 5% |
| C | Gain ≥ +8% | Move stop to breakeven |
| **D** | Held ≥ 15 trading days AND gain < +2% | **Time stop — exit stagnant position** |

## Risk Parameters

| Rule | Limit |
|------|-------|
| Risk per trade | 1.5% base (max 2%) |
| Max positions | 8 |
| Max per sector | 2 |
| Portfolio heat | ≤ 8% total open risk |
| Daily loss halt | −4% |
| Max drawdown halt | −12% from ATH |
| Trailing stop | 7% initial → 5% after partial exit |
| Breakeven stop | at +8% |
| Time stop | 15 trading days with < +2% gain |

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Portfolio equity, heat, day P&L, loss streak |
| `/positions` | All open positions with live P&L |
| `/orders` | Pending buy-stop orders |
| `/cancel SYMBOL` | Cancel buy-stop and remove from risk state |
| `/help` | Command list |

## Project Structure

```
three-masters-bot/
├── main.py               # Orchestrator: regime, sector, correlation, sizing, orders
├── screener.py           # Simons: Trend Template + RS-line + weekly tight base
├── vcp_analyzer.py       # Minervini: VCP Tier 0→1→2, ATR stops, vol dry-up
├── risk_manager.py       # Tudor Jones: sizing, circuit breakers, trade journal
├── position_monitor.py   # Tudor Jones: trailing stop, partial exit, time stop
├── position_sync.py      # Bulletproof Alpaca↔state sync (SyncError blocks all trading)
├── broker.py             # Alpaca order execution
├── telegram_commands.py  # Two-way Telegram bot
├── order_stream.py       # WebSocket fill notifications (Alpaca trade stream v3)
├── dashboard.py          # Flask web UI at :5002
├── config.py             # All settings (from .env)
└── logs/
    ├── risk_state.json       # Live heat, losses, daily P&L
    ├── monitor_state.json    # Per-position tracking (entry_date, stop IDs)
    ├── trade_journal.jsonl   # Completed trades with R-multiples
    └── sync_audit.jsonl      # Every sync run (audit trail)
```

## Weekly Report (Fridays)

Reads all-time `trade_journal.jsonl` and reports:
- Win rate, wins/losses
- Avg win R, avg loss R
- **Expectancy per trade** (in R-multiples)
- Scan stats for the week

## Infrastructure

- **Service**: `systemctl --user start/stop/restart three-masters-bot`
- **Watchdog**: `three-masters-watchdog.timer` — restarts if heartbeat stale >15 min
- **Dashboard**: http://docker-nuc:5002 (auto-refresh 60s)
- **Paper account**: `THREE_MASTERS_ALPACA_API_KEY` (separate from other bots)

## Commits

| Hash | Description |
|------|-------------|
| `0f8a934` | 7 optimizations: time stop, loss sizing, win stats, gap check, vol dry-up, correlation, weekly base |
| `fe05a3a` | Fix Stream API v3, dashboard port 5002 |
| `e6e9cda` | Fill notifications, Telegram commands, dashboard, ATR stops, RS-line filter |
| `94fdcf0` | Market regime, sector limit, adaptive sizing, smart orders, trade journal |
| `714e1a8` | Bulletproof sync: orphan buy-orders, close_trade, day_start_equity |
