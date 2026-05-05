"""
Three Masters Bot — Configuration
API keys and settings. Loaded from environment variables.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"
CHART_DIR = BASE_DIR / "charts"
REPORT_DIR = BASE_DIR / "reports"

# ── Alpaca (dedicated paper account for Three Masters) ──────────────────────
ALPACA_API_KEY    = os.environ.get("THREE_MASTERS_ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("THREE_MASTERS_ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.environ.get("THREE_MASTERS_ALPACA_URL", "https://paper-api.alpaca.markets")

# ── Claude AI (Minervini VCP layer) ─────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"   # fast + cheap for VCP screening
CLAUDE_MODEL_DEEP = "claude-sonnet-4-6"            # for high-confidence re-check

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Simons layer — Trend Template ────────────────────────────────────────────
TREND_TEMPLATE = {
    "min_price": 10.0,
    "min_avg_volume": 500_000,        # 500k shares/day minimum
    "ma50_above_ma150": True,
    "ma50_above_ma200": True,
    "ma150_above_ma200": True,
    "price_above_ma50": True,
    "price_above_ma150": True,
    "price_above_ma200": True,
    "within_pct_of_52w_high": 0.25,   # within 25% of 52-week high
    "above_pct_of_52w_low":  0.30,    # at least 30% above 52-week low
    "rs_min": 70,                      # Relative Strength vs SPY >= 70
    "ma200_trending_up_days": 20,      # 200MA trending up for 20 days
}

# ── Minervini layer — VCP Analysis ──────────────────────────────────────────
VCP = {
    "min_contractions": 2,            # at least 2 contractions
    "max_contractions": 5,            # typically 3-4 for ideal VCP
    "min_contraction_ratio": 0.50,    # each contraction ≤ 50% of previous
    "max_depth_from_high": 0.35,      # pattern depth ≤ 35% from pivot high
    "volume_decline_required": True,   # volume must decline on contractions
    "final_tight_pct": 0.05,          # final contraction ≤ 5% range
    "lookback_days": 60,              # analyze last 60 trading days
    "breakout_volume_min": 1.5,       # breakout volume ≥ 1.5x avg
}

# ── Tudor Jones layer — Risk Management ──────────────────────────────────────
RISK = {
    "risk_per_trade_pct": 0.015,      # 1.5% of portfolio per trade
    "max_risk_per_trade_pct": 0.02,   # never exceed 2%
    "max_positions": 8,
    "max_sector_pct": 0.30,           # max 30% in one sector
    "max_daily_loss_pct": 0.04,       # -4% halts trading
    "max_drawdown_pct": 0.12,         # -12% from ATH → defensive
    "position_scale_in": False,        # single entry (no pyramid for now)
}

# ── Universe ─────────────────────────────────────────────────────────────────
UNIVERSE_FILE = BASE_DIR / "universe.txt"   # list of symbols, one per line
SP500_UNIVERSE = True                        # use dynamic S&P 500 + Nasdaq 100

# ── Scheduling ────────────────────────────────────────────────────────────────
DAILY_TRIGGER_HOUR_CET = 22   # 22:30 CEST — after US close
DAILY_TRIGGER_MIN_CET  = 30   # :30 to let daily bars finalize
MARKET_OPEN_ET  = (9, 30)     # 09:30 ET
MARKET_CLOSE_ET = (16, 0)     # 16:00 ET

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL   = "INFO"
LOG_MAX_MB  = 20
LOG_BACKUPS = 5
