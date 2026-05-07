# Three Masters Bot

A systematic swing trading bot combining three legendary investment philosophies into one pipeline. Runs daily at 22:30 CEST (after US close).

## The Three Masters

| Layer | Master | File | Role |
|-------|--------|------|------|
| 1 | **Jim Simons** | `screener.py` | Quantitative screening — Trend Template + RS-line + weekly tight base |
| 2 | **Mark Minervini** | `vcp_analyzer.py` | VCP pattern via Claude AI — Haiku→Sonnet→Opus tiered, ATR stops, measured move |
| 3 | **Paul Tudor Jones** | `risk_manager.py` + `position_monitor.py` | Adaptive risk, VIX scaling, circuit breakers, exit management |

## Daily Pipeline (22:30 CEST)

```
500+ stocks (S&P 500 + Nasdaq 100 + extended)
    │
    ▼
[Simons — 30%] Trend Template filter
  • Price > MA20, MA50, MA150, MA200 (bull stack)
  • MA200 trending up ≥ 20 days
  • Within 25% of 52-week high
  • RS rating ≥ 70 vs SPY
  • RSI ≤ 75 (not chasing extended stocks)
  • Earnings ≥ 7 days away
  • Weekly chart: price above MA10w + MA40w
  • Weekly chart: last 4-week H-L range < 15% of price (tight base filter)
  • RS line (stock/SPY ratio) at 52-week high → priority flag
  • RS line at 52w high while price 3–15% below its high → rs_line_leading (strongest VCP signal)
  • Weekly RS confirmation: resampled daily RS/SPY to weekly bars; daily+weekly both at 52w high
    = institutional O'Neil confirmation → +3.0 Simons pts (vs +2.5 daily only)
  • A/D ratio: up-volume / down-volume last 50 bars → >1.5×=+0.5 pts, >1.2×=+0.25 pts
  • Short ratio (days-to-cover): ≥5d=+0.5 pts, ≥3d=+0.25 pts (squeeze fuel at breakout)
  • Pre-earnings sweet spot: 4–8 weeks before report AND EPS growth ≥25% → +0.5 Simons pts
  • Fundamentals: reject if quarterly EPS decline > 10% (Minervini SEPA filter)
    Revenue + EPS growth data passed to Claude Sonnet prompt for context
    │
    ▼  (typically 20–60 stocks pass)
[Minervini — 60%] VCP Analysis — Tier 0 → 1 → 2 → 3
  • Tier 0 (quant):  ≥2 of 4 segments tighter, depth <35%, final handle <8%
                     Volume dry-up: 5d avg < 60% of 100d avg → vol_at_multiweek_low
  • Tier 1 (Haiku):  20-bar OHLCV pre-screen — score 0–10, is_vcp true/false
  • Tier 2 (Sonnet): 100-bar OHLCV step-by-step Minervini protocol:
                       - Trend (Stage 2?), Contraction Map (C1→C2→C3 each tighter),
                         Volume per contraction, Handle identification,
                         Entry precision (handle HIGH + $0.01), Stop (handle LOW − $0.01),
                         Quality score 1–5, Measured move (base height / current price)
  • Tier 3 (Opus):   Final validation for quality_score ≥ 4 — confirms or vetoes Sonnet
  • Breakout level = handle HIGH + $0.01  (pivot, not 20-day high)
  • Stop loss       = handle LOW  − $0.01  (pivot, not 20-day low)
  • ATR-adjusted stop: clamps stop to 1–3× ATR floor
  • Minimum quality_score = 3, minimum confidence = 0.65
    │
    ▼  (typically 1–6 confirmed VCPs)
[Market Regime + Sector Rotation + Market Breadth]
  • Bull (SPY ≥ MA200):         full sizing
  • Neutral (SPY 3–8% below):  ×0.75
  • Bear (SPY >8% below):       no new orders
  • Sector momentum: 21-day return of 11 SPDR sector ETFs vs SPY
    → Stage 2 required (sector ETF above MA200) for full positive bonus
    → sector outperforming >+1.5% AND Stage 2:   +0.5 composite bonus
    → sector outperforming >+1.5% NOT Stage 2:   +0.25 (muted bonus)
    → sector underperforming <−1.5%:             −0.5 composite penalty
  • Market breadth: % of screened universe above MA50 (Tudor Jones signal)
    → >65% above MA50: +2 T-score pts  (healthy internals)
    → 45–65%: +1 pt                     (neutral)
    → <45%:   +0 pts                    (deteriorating breadth)
    │
    ▼
[Three Masters Composite Score (0–10)]
  • Minervini 60%: quality_score + confidence×3 + tight_bonus + vol_dryup + breakout_vol + rs_line_bonus
  • Simons     30%: RS rating, RS line at high, RSI quality, 52w proximity, MA200 slope,
                    A/D ratio, short interest, weekly RS, pre-earnings sweet spot
  • Tudor Jones 10%: regime, consecutive losses, portfolio heat, market breadth, PCR
  • Sector bonus ±0.25/±0.5 added to composite (Stage 2 required for full positive)
  • PCR (CBOE Put/Call ratio): >1.0 fear=+0.5 Tudor pts; <0.6 greed=−0.5 Tudor pts
  • Minimum composite 5.0 (dynamic: auto-raised to 6.5 if low-score bucket negative)
    │
    ▼
[Tudor Jones] Position Sizing + Risk Checks
  • Adaptive risk: composite ≥8.0 → 2%, ≥7.0 → 1.75%, else 1.5%
  • Half-Kelly sizing: risk × Kelly(win_rate, avg_R) / 2; clamped [0.5×, 1.0×] (min 10 trades)
  • VIX scaling:  VIX <15 → 100%, 15–20 → 90%, 20–25 → 80%, 25–30 → 65%, >30 → 50%
  • Consecutive loss factor: 1 loss=75%, 2=50%, 3+=33% of base risk
  • Market regime size factor: neutral=×0.75
  • Profit target: Claude's measured move estimate (base height / entry); floor at 2R
  • Max positions: 8 | Max per sector: 2
  • Portfolio heat cap: 8%
  • Correlation guard: skip if r ≥ 0.80 with any held position (60-day returns)
    │
    ▼
[Execution] GTC buy-stop orders at VCP breakout level
  • Smart order management: keeps valid unchanged orders, cancels stale/moved ones
  • Morning briefing (09:15 ET): pre-market gap check — cancel if stock >2% above stop
  • Opening range filter (10:00 ET): cancel if stock still below breakout after 30 min
  • Fill-slippage guard: on first monitor cycle after fill — >2% above planned buy-stop = close
    immediately; >1% = warning only (Tudor Jones discipline)
```

## Exit Rules (position_monitor.py — every 15 min during market hours)

| Step | Trigger | Action |
|------|---------|--------|
| **0** | ≤ 5 days to earnings AND profitable | Move stop to breakeven (earnings protection) |
| **0** | ≤ 3 days to earnings AND flat/loss | **Close position** (earnings protection) |
| **A** | Position first seen | **Hard stop at VCP pivot low**; fallback to 7% trailing if no pivot data |
| **B1** | Gain ≥ +10% | Sell 33%, hold runner |
| **B2** | Gain ≥ measured move (floor +20%) | Sell another 33%, tighten trailing stop to 5% for remaining 34% |
| **C** | Gain ≥ +8% (if B1 not done yet) | Move stop to breakeven |
| **D** | Held ≥ 15 days (20 days if composite ≥ 8.0) AND gain < +2% | **Time stop — exit stagnant position** |
| **E** | Gain ≥ +25% AND 3 consecutive up-days AND volume >1.5× avg | **Climax run — sell all into strength** |
| **F** | Gain ≥ +4% AND not yet pyramided | **Add 25% shares at confirmation** — same pivot stop, subject to heat cap |

Three-stage exit: lock 33% early, capture measured move with second 33%, ride runner with 5% trail.

## Composite Scoring Details

### Minervini score (0–10, weight 60%)
| Component | Points |
|-----------|--------|
| Quality score 1–5 (from Claude) | ×1.0 |
| Confidence 0–1 (from Claude) | ×3.0 |
| Handle tightness < 5% | +1.0 |
| Handle tightness 5–7% | +0.5 |
| Volume dry-up flag | +0.5 |
| Breakout volume flag | +0.5 |
| RS line at 52-week high | +1.0 |

### Simons score (0–10, weight 30%)
| Component | Points |
|-----------|--------|
| RS rating 70–99 | 0–4.0 |
| RS line leading price + weekly confirmed | +3.0 |
| RS line leading price only | +2.5 |
| RS line at 52-week high + weekly confirmed | +2.0 |
| RS line at 52-week high only | +1.5 |
| A/D ratio ≥ 1.5× (strong accumulation) | +0.5 |
| A/D ratio ≥ 1.2× | +0.25 |
| Short ratio ≥ 5 days-to-cover | +0.5 |
| Short ratio ≥ 3 days-to-cover | +0.25 |
| Pre-earnings sweet spot (4–8w + EPS ≥25%) | +0.5 |
| EPS quarterly growth ≥ 25% | +1.0 |
| EPS quarterly growth ≥ 10% | +0.5 |
| RSI ≤ 65 | +2.0 (→ +1.0 if ≤ 72) |
| Within 5% of 52w high | +1.5 (→ +1.0 / +0.5 further out) |
| MA200 slope positive | +0.5 |

### Tudor Jones score (0–10, weight 10%)
| Component | Points |
|-----------|--------|
| Bull regime | +3.0 |
| Zero consecutive losses | +3.0 (→ +1.5 after 1 loss) |
| Portfolio heat < 2% | +1.5 (→ +0.75 if < 4%) |
| Market breadth > 65% above MA50 | +2.0 (→ +1.0 if > 45%) |
| PCR > 1.0 (fear/contrarian buy signal) | +0.5 |
| PCR < 0.6 (greed/complacency) | −0.5 |

## Risk Parameters

| Rule | Value |
|------|-------|
| Base risk per trade | 1.5% (→ 1.75% at composite ≥ 7.0, → 2% at composite ≥ 8.0) |
| Kelly factor | Half-Kelly from journal win rate + avg R; clamped [0.5×, 1.0×] |
| VIX scaling | 50–100% of position size (steps: <15, 15–20, 20–25, 25–30, >30) |
| Max positions | 8 |
| Max per sector | 2 |
| Portfolio heat cap | 8% |
| Daily loss halt | −4% |
| Max drawdown halt | −12% from ATH |
| Hard stop | VCP pivot low (handle LOW − $0.01) |
| Trailing stop | 7% initial → 5% after partial exit |
| Breakeven stop | at +8% |
| Time stop | 15 trading days with < +2% gain (20 days for elite setups) |
| Climax run exit | +25% gain + 3 up-days + volume >1.5× avg |

## Daily Schedule

| Time (CEST) | Event |
|-------------|-------|
| 22:30 | Daily scan — Simons → Minervini → Tudor Jones → GTC orders placed |
| 22:30 | FOMC/CPI check — if macro event within 2 days: scan runs but no orders placed |
| 15:15 (09:15 ET) | Morning briefing — equity, positions, pre-market gap check |
| 16:00 (10:00 ET) | Opening range filter — cancel unconfirmed buy-stops |
| Market hours | Position monitor every 15 min — Steps 0/A/A+/B1/B2/C/D/E/F |

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
├── main.py               # Orchestrator: composite scoring, sector/breadth/VIX, orders
├── screener.py           # Simons: Trend Template + RS-line + weekly tight base
├── vcp_analyzer.py       # Minervini: VCP Tier 0→1→2→3, measured move, pivot levels
├── risk_manager.py       # Tudor Jones: VIX-scaled sizing, circuit breakers, trade journal
├── position_monitor.py   # Exit engine: Steps 0/A/A+/B1/B2/C/D/E/F (earnings guard → pivot stop → MA20 trail → partials → time → climax → pyramid)
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
    ├── feedback_state.json   # Weekly score-bucket stats (avg R / win rate per composite range)
    └── sync_audit.jsonl      # Every sync run (audit trail)
```

## Weekly Report (Fridays)

Reads all-time `trade_journal.jsonl` and reports:
- Win rate, wins/losses
- Avg win R, avg loss R
- Expectancy per trade (in R-multiples)
- Scan stats for the week
- **Score-bucket breakdown**: avg R and win rate per composite range (5–6, 6–7, 7–8, 8+)

Results saved to `logs/feedback_state.json` for long-term calibration of scoring thresholds.

## Infrastructure

- **Service**: `systemctl --user start/stop/restart three-masters-bot`
- **Watchdog**: `three-masters-watchdog.timer` — restarts if heartbeat stale >15 min
- **Dashboard**: http://docker-nuc:5002 (auto-refresh 60s)
- **Paper account**: `THREE_MASTERS_ALPACA_API_KEY` (separate from other bots)

## Commit History

| Hash | Description |
|------|-------------|
| `124857a` | EPS filter (SEPA), RS-line-leading signal, three-stage exit (33/33/34), pyramiding at +4%, earnings protection |
| `ced84e2` | Composite scoring, VCP Opus tier, sector rotation, VIX scaling, breadth, climax exit, opening range filter, measured move |
| `0f8a934` | 7 Minervini/Tudor Jones optimizations: time stop, loss sizing, win stats, gap check, vol dry-up, correlation, weekly base |
| `fe05a3a` | Fix Stream API v3, dashboard port 5002 |
| `e6e9cda` | Fill notifications, Telegram commands, dashboard, ATR stops, RS-line filter |
| `94fdcf0` | Market regime, sector limit, adaptive sizing, smart orders, trade journal |
| `714e1a8` | Bulletproof sync: orphan buy-orders, close_trade, day_start_equity |
