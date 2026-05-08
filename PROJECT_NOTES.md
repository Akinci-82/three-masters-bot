---
name: Three Masters Bot
description: Swing trading bot combining Simons (screener), Minervini (VCP via Claude AI), Tudor Jones (risk) — live on docker-nuc
type: project
originSessionId: 53539f57-540a-469b-99ba-fe09da8b41bf
---
Live swing trading bot at `/home/habil/three-masters-bot/` on docker-nuc.
Runs as systemd user service: `three-masters-bot.service`.
Paper account: `https://paper-api.alpaca.markets` (same key as The Triumvirate — PKP72A...).

**Why:** Dedicated swing-trade bot based on VCP patterns, tracked separately from other bots.
**How to apply:** Daily scan at 22:30 CEST. Position monitor every 15 min during US market hours.

## Architecture

| File | Layer | Purpose |
|------|-------|---------|
| screener.py | Simons | Trend Template + RSI + earnings + weekly context + RS-line-new-high + weekly tight base |
| vcp_analyzer.py | Minervini | Quant VCP → Haiku → Sonnet (tiered), ATR-adjusted stops, vol dry-up flag |
| risk_manager.py | Tudor Jones | 1.5-2% adaptive risk, circuit breakers, trade journal |
| position_monitor.py | Monitor | Trailing stop, partial exit, breakeven, time stop, close detection, trade journal |
| position_sync.py | Sync | Bulletproof Alpaca↔state reconciliation, SyncError blocks trading |
| broker.py | — | GTC buy-stop orders via Alpaca |
| main.py | — | Orchestrator: regime filter, sector limit, smart orders, correlation guard, loss sizing |
| telegram_commands.py | — | Two-way Telegram: /status /orders /positions /cancel /help |
| order_stream.py | — | WebSocket fill notifications (Alpaca trade stream) |
| dashboard.py | — | Flask web UI at :5002 (equity, positions, orders, journal) |
| config.py | — | All settings |

## Key parameters
- Risk per trade: 1.5% adaptive (→2% at high VCP confidence, ×0.75 in neutral regime)
- **Consecutive loss factor**: 1 loss=75%, 2=50%, 3+=33% of base risk (Tudor Jones)
- Max positions: 8 | Max per sector: 2
- Portfolio heat cap: 8%
- Daily loss halt: -4% | Drawdown halt: -12%
- Trend Template: RS ≥ 70, within 25% of 52w high, above all MAs, RSI ≤ 75
- **Weekly tight base**: last 4 weeks H-L range < 15% of price (screener filter)
- VCP: segment-based contraction (≥2/4 segments tighter), Claude Haiku→Sonnet
- **Volume dry-up**: 5-day avg < 60% of 100-day avg → `vol_at_multiweek_low` flag
- **Correlation guard**: skip new order if r ≥ 0.80 with any held position (60-day returns)
- Trailing stop: 7% initial → 5% after partial exit at +15%
- Breakeven stop at +8%
- **Time stop**: close if held ≥15 trading days with <2% gain (Minervini 3-4 week rule)
- **Pre-market gap check**: cancel buy-stop if stock >2% above stop at morning briefing

## Exit rules (position_monitor.py)
- Step A: Place initial 7% trailing stop when position first seen
- Step B: Partial exit at +15% (sell 50%), tighten trailing stop to 5%
- Step C: Move stop to breakeven at +8%
- **Step D (new)**: Time stop — close if held ≥15 trading days AND gain <2%

## Weekly report stats (main.py → _send_weekly_report)
Reads `logs/trade_journal.jsonl` (all-time) and reports:
- Total trades / wins / losses / win rate
- Avg win R, avg loss R, expectancy per trade

## Current live state (2026-05-07)
- Portfolio equity: $5,000 (paper)
- All services running: monitor, Telegram listener, WebSocket stream, dashboard

## Three Masters composite scoring (updated Patch 7)
- **Minervini 60%**: quality_score×1 + confidence×3 + tight_bonus + vol_dryup + breakout_vol + rs_line_bonus (max 10)
- **Simons 30%**: RS rating, RS line (leading+weekly=3.0, leading=2.5, at_high=1.5), RSI, 52w proximity, MA200 slope, A/D ratio, short_ratio, pre-earnings sweet spot (max 10)
- **Tudor Jones 10%**: market regime, consecutive losses, portfolio heat, breadth, power trend, PCR (max 10)
- Minimum composite **5.0** (dynamic: auto-raises to 6.5 if low-score bucket negative)
- Adaptive risk: composite ≥8.0 → 2%, ≥7.0 → 1.75%, else 1.5% — further scaled by half-Kelly

## Patch 5 upgrades (added 2026-05-07)
- **EPS filter**: screener rejects stocks with quarterly EPS decline >10% (Minervini SEPA)
- **rs_line_leading**: RS line at 52w high while price 3-15% below = strongest VCP signal → +2.5 Simons pts (vs +1.5 for at-high only)
- **EPS bonus in Simons score**: ≥25% growth→+1.0, ≥10%→+0.5
- **Three-stage exit**: 33% at +10%, 33% at measured move (floor +20%), 34% runner with 5% trail
- **Pyramiding (Step F)**: add 25% shares at +4% confirmation, same pivot stop
- **Earnings protection (Step 0)**: breakeven at ≤5d to earnings; close at ≤3d if flat/loss
- **Opening range filter upgraded**: SPY+QQQ broad check + per-symbol price confirmation + volume check
- **Fundamentals in Sonnet prompt**: EPS/revenue growth context passed to Claude for VCP analysis

## VCP analyzer upgrades (added 2026-05-06)
- **Breakout level fixed**: now uses handle HIGH (last10_h) not 20-day high
- **Stop fixed**: now uses handle LOW (last10_l) not 20-day low
- **Tier 3 Opus** added for quality_score ≥ 4 setups (validates/vetos Sonnet)
- **Sonnet prompt** rewritten: step-by-step Minervini protocol, 100-bar OHLCV, numbered contractions
- **Thresholds tightened**: final_tight_pct 10%→8%, min_confidence 0.55→0.65, min_quality_score=3
- CLAUDE_MODEL_ULTRA = "claude-opus-4-7" added to config

## Patch 7 upgrades (added 2026-05-07)
- **Weekly RS confirmation**: daily+weekly RS both at 52w high → +3.0 Simons pts (vs +2.5 leading only)
- **A/D ratio**: up-vol/down-vol last 50 bars → +0.5/+0.25 Simons pts (institutional accumulation)
- **Short ratio**: days-to-cover ≥5→+0.5, ≥3→+0.25 Simons pts (squeeze fuel)
- **Pre-earnings sweet spot**: 4-8 weeks pre-report + EPS ≥25% → +0.5 Simons pts
- **PCR (Put/Call ratio)**: >1.0 fear=+0.5 Tudor pts, <0.6 greed=-0.5 Tudor pts
- **Dynamic MIN_COMPOSITE**: auto-raises to 6.5 if score bucket 5.0-6.5 has negative avg_R (≥5 trades)
- **Sector Stage 2 filter**: sector ETF must be above MA200 for full +0.5 bonus (else +0.25)
- **Half-Kelly position sizing**: risk_amount × Kelly(win_rate, avg_R)/2, clamped [0.5×, 1.0×]
- **Fill-slippage guard**: >2% above planned buy_stop = immediate close; >1% = warning

## Patch 8 upgrades (added 2026-05-07)
- **IPO age filter**: screener requires ≥240 daily bars (≈1 year); rejects recent listings
- **Monthly Stage 2**: 5-year monthly chart — price > MA10m + MA40m → +0.5 Simons pts (`_check_monthly_context`)
- **Rate slope**: 10Y yield (^TNX) 20-day change >+50 bps → −1.0 Tudor pts (tightening risk)
- **Stop-limit orders**: broker.py buy-stop upgraded to stop-limit (limit = stop × 1.005)
- **Stale composite cancel**: orders cancelled if symbol composite drops below MIN_COMPOSITE in tonight's scan
- **Pivot trailing stop (Step A-trail)**: after 5+ days, ratchet stop to recent swing low − 1%
- **Gap-up harvest (Step G)**: sell 50% on overnight gap ≥12% while profitable; sets partial1_done

## Patch 9 upgrades (added 2026-05-07)
- **VIX slope**: ^VIX 5-day change >+3 pts → −0.5 Tudor pts (`_fetch_vix_slope`)
- **EPS revision**: forwardEps/trailingEps − 1 ≥ 15% → +0.5 Simons pts (in `_get_fundamentals` 5-tuple)
- **RS vs sector**: stock vs own sector ETF outperformance >5% → +0.5 Simons pts
- **Consecutive wins factor**: `_consecutive_win_factor`: 2 wins=×1.10, 3+=×1.25 sizing; tracked in `close_trade`
- **Super-sector guard**: `_SUPER_SECTOR` map (growth/cyclical/defensive/financial); max 60% concentration
- **Limit exits**: B1/B2 partials use `_place_limit_sell` (cur_price×0.999, market fallback)
- **Telegram proximity alerts**: once/day/position — stop within 2%, approaching +10%, earnings 3-7d


## Patch 10 upgrades (added 2026-05-08)
- **FOMC post-announcement lift**: `_is_macro_blackout()` now allows trades on FOMC day if UTC clock >= 20:00 (14:00 ET). Decision already announced => binary risk resolved. Pre-announcement protection (delta 1-2 days + same day before 20:00 UTC) unchanged.
- **Why**: Scanner runs 22:30 CEST (20:30 UTC). Without this fix, evening scan on FOMC day blocked orders that would execute *next morning* -- after Fed reaction is fully priced in. CPI blackout logic unchanged (no single known announcement time).
- **WebSocket stream monitoring loop** (`order_stream.py`): replaced bare `stream_thread.join()` with a 10 s polling loop. The old bare join hung forever if the WS stalled without firing a disconnect event -- reconnect never triggered. New loop also pings Alpaca REST every 90 s; if ping fails (auth error), stream is force-stopped and reconnects immediately via the existing exponential backoff.
## Commits (latest first)
- `PENDING` -- fix: WS stream polling loop + REST auth ping every 90s (order_stream.py) (2026-05-08)
- `cf0a1b2` -- fix: lift FOMC blackout post-announcement (>=20:00 UTC same day) (2026-05-08)
- `cf82859` — docs: README for Patches 8 and 9 (2026-05-07)
- `daed90c` — Patch 9: VIX slope, EPS revision, RS vs sector, win streak, super-sector, limit exits, alerts (2026-05-07)
- `dce9cef` — Patch 8: IPO filter, monthly Stage 2, rate slope, stop-limit, pivot trail, gap harvest (2026-05-07)
- `17e5639` — fix: remove market cap upper cap, $500M floor only (2026-05-07)
- `ce288cd` — docs: README update for Patch 7 (2026-05-07)
- `5a87d18` — Patch 7 cont: position monitor buy_stop, slippage guard, Kelly sizing (2026-05-07)
- `901473d` — Patch 7: A/D ratio, weekly RS, PCR, Kelly, slippage guard, dynamic threshold (2026-05-07)
- `d38697f` — Patch 6: market cap filter, power trend, FOMC blackout, choppy market, MA20 trail, CwH (2026-05-07)
- `124857a` — Patch 5: EPS filter, RS-leading, three-stage exit, pyramiding, earnings protection (2026-05-07)
- `ced84e2` — Composite scoring + 9 strategy improvements (2026-05-06)
- `0f8a934` — 7 Minervini/Tudor Jones optimizations (2026-05-06)

## Background services (all started at boot)
- Position monitor thread (15 min cycles during market hours)
- Telegram command listener (polls getUpdates every 5s)
- Alpaca WebSocket stream (fill notifications)
- Flask dashboard at http://docker-nuc:5002

## Watchdog
`three-masters-watchdog.timer` restarts bot if heartbeat.json is stale >15 min.

## Key logs
- `logs/trade_journal.jsonl` — completed trades with R-multiples
- `logs/sync_audit.jsonl` — every sync run with changes made
- `logs/risk_state.json` — live portfolio heat, consecutive losses, daily P&L
- `logs/monitor_state.json` — per-position tracking (entry_date, stop order IDs, partial exits)
