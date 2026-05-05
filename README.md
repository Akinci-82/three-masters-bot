# Three Masters Bot

A systematic swing trading bot combining three legendary investment philosophies into one pipeline. Runs daily at 07:00 CET.

## The Three Masters

| Layer | Master | File | Role |
|-------|--------|------|------|
| 1 | **Jim Simons** | `screener.py` | Quantitative screening — Minervini Trend Template on 500+ stocks |
| 2 | **Mark Minervini** | `vcp_analyzer.py` | VCP pattern analysis via Claude AI — identify explosive setups |
| 3 | **Paul Tudor Jones** | `risk_manager.py` | Risk-first position sizing — never risk more than 1-2% per trade |

## Daily Pipeline (07:00 CET)

```
500+ stocks
    │
    ▼
[Simons] Trend Template filter
  • Price > MA50, MA150, MA200
  • MA50 > MA150 > MA200 (bull stack)
  • MA200 trending up ≥ 20 days
  • Within 25% of 52-week high
  • At least 30% above 52-week low
  • Relative Strength vs SPY ≥ 70
    │
    ▼  (typically 30-80 stocks pass)
[Minervini] VCP Analysis via Claude AI
  • Quantitative pre-filter (contractions, tight area, volume drying)
  • Claude Haiku confirms/rejects VCP pattern
  • Identifies: breakout level, stop-loss (pivot low)
  • Minimum confidence: 55%
    │
    ▼  (typically 2-10 VCP setups)
[Tudor Jones] Position Sizing
  • Risk exactly 1.5% of portfolio per trade (max 2%)
  • Shares = (Portfolio × 1.5%) / (Entry − Stop Loss)
  • Target = Entry + 3 × Risk (3R reward/risk)
  • Portfolio heat ≤ 8% at any time
    │
    ▼
[Execution] Buy-stop orders at breakout level
  + Protective sell-stop at pivot low (GTC)
    │
    ▼
[Report] Telegram + daily JSON log
```

## Entry/Exit Logic

**Entry**: Buy-stop order at VCP breakout level (executes automatically when price breaks out during market hours)

**Stop-loss**: GTC sell-stop at pivot low (the lowest point of the final VCP contraction)

**Target**: 3R from entry — if risking $1, targeting $3 gain

**Exit signals**:
- Stop-loss hit (automatic via GTC order)
- Stock breaks below 50-day MA (manual override)
- Voluntary cut if pattern fails on high volume

## Risk Rules (Tudor Jones)

| Rule | Limit |
|------|-------|
| Risk per trade | 1.5% of portfolio (max 2%) |
| Max positions | 8 |
| Portfolio heat | ≤ 8% total open risk |
| Daily loss halt | -4% |
| Max drawdown halt | -12% from ATH |
| Max sector | 30% |

## Dedicated Alpaca Account

This bot uses `THREE_MASTERS_ALPACA_API_KEY` — a separate paper account from other bots. This allows clean performance comparison without cross-contamination.

## Setup

```bash
cd /home/habil/three-masters-bot
cp .env.example .env   # add your THREE_MASTERS Alpaca keys
python main.py         # runs daily scheduler at 07:00 CET
python main.py --run-now  # immediate run (testing)
```

## Project Structure

```
three-masters-bot/
├── main.py          # Scheduler + pipeline orchestration
├── screener.py      # Simons: Trend Template filter
├── vcp_analyzer.py  # Minervini: VCP detection + Claude AI
├── risk_manager.py  # Tudor Jones: position sizing + circuit breakers
├── broker.py        # Alpaca order execution
├── config.py        # All settings (loaded from .env)
├── logs/            # Daily run logs
├── reports/         # Daily JSON reports (one per trading day)
└── charts/          # Reserved for price charts
```

## VCP Pattern (Minervini)

A Volatility Contraction Pattern shows:
1. Stock in a confirmed uptrend (passed Trend Template)
2. Price consolidates in 2-5 progressively narrower swings
3. Volume dries up with each contraction (institutions holding, not selling)
4. Final tight area: < 5% price range
5. Breakout on volume surge (≥ 1.5× average)

Claude AI receives the full 60-day OHLCV table and confirms/rejects the pattern with a confidence score and specific price levels.
