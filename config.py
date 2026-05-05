from dotenv import load_dotenv
load_dotenv()

"""
Three Masters Bot — Configuration
API keys and settings. Loaded from environment variables.
"""
import os
from pathlib import Path

BASE_DIR   = Path(__file__).parent
LOG_DIR    = BASE_DIR / "logs"
CHART_DIR  = BASE_DIR / "charts"
REPORT_DIR = BASE_DIR / "reports"

# ── Alpaca (dedicated paper account for Three Masters) ──────────────────────
ALPACA_API_KEY    = os.environ.get("THREE_MASTERS_ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("THREE_MASTERS_ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.environ.get("THREE_MASTERS_ALPACA_URL", "https://paper-api.alpaca.markets")

# ── Claude AI (Minervini VCP layer) ─────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"
CLAUDE_MODEL_DEEP = "claude-sonnet-4-6"

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Simons layer — Trend Template ────────────────────────────────────────────
TREND_TEMPLATE = {
    "min_price":               10.0,
    "min_avg_volume":          500_000,
    "ma50_above_ma150":        True,
    "ma50_above_ma200":        True,
    "ma150_above_ma200":       True,
    "price_above_ma50":        True,
    "price_above_ma150":       True,
    "price_above_ma200":       True,
    "within_pct_of_52w_high":  0.25,
    "above_pct_of_52w_low":    0.30,
    "rs_min":                  70,
    "ma200_trending_up_days":  20,
    # RSI filter — skip overbought entries even in uptrend
    "rsi_max_entry":           75,     # RSI > 75 = skip (chasing)
    "rsi_period":              14,
    # Weekly context — require weekly trend to confirm daily
    "weekly_context":          True,   # price above MA10w + MA40w on weekly chart
    # Earnings filter — never buy within N days of earnings
    "earnings_min_days_away":  7,      # skip if earnings within 7 calendar days
    # MA20 as additional filter
    "price_above_ma20":        True,   # price should be above short-term MA
}

# ── Minervini layer — VCP Analysis ──────────────────────────────────────────
VCP = {
    "min_contractions":         2,
    "max_contractions":         5,
    "min_contraction_ratio":    0.50,
    "max_depth_from_high":      0.35,
    "volume_decline_required":  True,
    "final_tight_pct":          0.10,
    "lookback_days":            60,
    "breakout_volume_min":      1.5,   # today's volume ≥ 1.5x avg = breakout confirmed
    # Candlestick confirmation on entry candle
    "require_bullish_candle":   True,  # last candle should be bullish (close > open)
    "check_candle_patterns":    True,  # check for hammer / engulfing
}

# ── Tudor Jones layer — Risk Management ──────────────────────────────────────
RISK = {
    "risk_per_trade_pct":      0.015,
    "max_risk_per_trade_pct":  0.02,
    "max_positions":           8,
    "max_sector_pct":          0.30,
    "max_daily_loss_pct":      0.04,
    "max_drawdown_pct":        0.12,
    "max_portfolio_heat_pct":  0.08,
    "position_scale_in":       False,
}

# ── Position Monitor — Intraday Exit Management ───────────────────────────────
MONITOR = {
    "enabled":                True,
    "interval_minutes":       15,          # check every 15 min during market hours
    # Partial profit (Minervini's rule: take some off the table)
    "partial_exit_pct":       0.50,        # sell 50% of position at first target
    "partial_exit_trigger":   0.15,        # take partial at +15% gain
    # Trailing stop on remaining position
    "trailing_stop_pct":      0.07,        # 7% trailing stop on remainder
    "trailing_stop_type":     "percent",   # Alpaca native trailing stop type
    # Hard stop upgrade: move stop to breakeven after +8%
    "breakeven_trigger":      0.08,
}

# ── Universe ─────────────────────────────────────────────────────────────────
SP500_UNIVERSE = True

# ── Scheduling ────────────────────────────────────────────────────────────────
DAILY_TRIGGER_HOUR_CET = 22   # 22:30 CEST — after US close
DAILY_TRIGGER_MIN_CET  = 30   # :30 to let daily bars finalize
MARKET_OPEN_ET         = (9, 30)
MARKET_CLOSE_ET        = (16, 0)

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL   = "INFO"
LOG_MAX_MB  = 20
LOG_BACKUPS = 5
