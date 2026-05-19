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
VAULT_DIR  = Path(os.environ.get("VAULT_DIR", "/home/habil/obsidian-vault"))

# ── Alpaca (dedicated paper account for Three Masters) ──────────────────────
ALPACA_API_KEY    = os.environ.get("THREE_MASTERS_ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("THREE_MASTERS_ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.environ.get("THREE_MASTERS_ALPACA_URL", "https://paper-api.alpaca.markets")

# ── Live trading flag ─────────────────────────────────────────────────────────
# Set ALPACA_LIVE=true in .env to switch from paper to live account.
# When enabled, ALPACA_BASE_URL is overridden to the live endpoint and the
# paper-URL guard in _validate_config() is bypassed.
ALPACA_LIVE = os.environ.get("ALPACA_LIVE", "").lower() == "true"
if ALPACA_LIVE:
    ALPACA_BASE_URL = "https://api.alpaca.markets"

# Optional paper account for paper-vs-live P&L comparison (weekly report).
# Set these in .env when running ALPACA_LIVE=true and you want to benchmark
# the live account against a parallel paper account.
ALPACA_PAPER_COMPARE_KEY    = os.environ.get("THREE_MASTERS_ALPACA_PAPER_KEY", "")
ALPACA_PAPER_COMPARE_SECRET = os.environ.get("THREE_MASTERS_ALPACA_PAPER_SECRET", "")

# ── Claude AI (Minervini VCP layer) ─────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FMP_API_KEY        = os.environ.get("FMP_API_KEY", "")
ALPHA_VANTAGE_KEY   = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
MASSIVE_REST_KEY    = os.environ.get("MASSIVE_SECRET_ACCESS_KEY", "")
MASSIVE_ACCESS_KEY_ID     = os.environ.get("MASSIVE_ACCESS_KEY_ID", "")
MASSIVE_SECRET_ACCESS_KEY = os.environ.get("MASSIVE_SECRET_ACCESS_KEY", "")
MASSIVE_S3_ENDPOINT       = os.environ.get("MASSIVE_S3_ENDPOINT", "https://files.massive.com")
MASSIVE_BUCKET            = os.environ.get("MASSIVE_BUCKET", "flatfiles")
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"
CLAUDE_MODEL_DEEP  = "claude-sonnet-4-6"
CLAUDE_MODEL_ULTRA = "claude-opus-4-7"

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
    "weekly_base_max_range":    0.25,   # P1-fix: 4-week H-L range <= 25% (Minervini standard, was 15%)
    # Earnings filter — never buy within N days of earnings
    "earnings_min_days_away":  7,      # skip if earnings within 7 calendar days
    # Ex-dividend guard — never buy within N days of ex-dividend date
    "exdiv_min_days_away":     3,      # price drops by dividend amount on ex-date
    # MA20 as additional filter
    "price_above_ma20":        True,   # price should be above short-term MA
}

# ── Minervini layer — VCP Analysis ──────────────────────────────────────────
VCP = {
    "min_contractions":         1,     # was 2; loosen quant gate, Claude AI evaluates
    "max_contractions":         5,
    "min_contraction_ratio":    0.50,
    "max_depth_from_high":      0.35,
    "volume_decline_required":  True,
    "final_tight_pct":          0.15,  # was 0.10; allow up to 15% handle range
    "pattern_high_min_bars":    5,     # bars since pattern high; was hardcoded 25
    "lookback_days":            60,
    "min_confidence":           0.65,
    "min_quality_score":        3,
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
    "max_positions_per_sector": 2,
    "max_sector_pct":          0.30,
    "max_daily_loss_pct":      0.04,
    "max_drawdown_pct":        0.12,
    "max_portfolio_heat_pct":  0.08,
    "soft_drawdown_pause_pct": 0.08,   # pause new entries (no halt) when dd >8%
    "position_scale_in":       False,
    # Stop-distance sizing factors: wide stops = lower conviction = smaller size
    # Keys are upper-bound stop-distance %; last entry is the "else" (very wide)
    "stop_distance_tiers": [
        (0.05, 1.00),   # ≤5% stop: ideal tight handle → full size
        (0.07, 0.85),   # ≤7% stop: slightly wide
        (0.10, 0.70),   # ≤10% stop: moderately wide
        (1.00, 0.55),   # >10% stop: very wide — reduced conviction
    ],
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
    "trailing_stop_after_partial": 0.05,  # 5% trailing stop after partial exit (profit locked)
    "trailing_stop_type":     "percent",   # Alpaca native trailing stop type
    # Hard stop upgrade: move stop to breakeven after +8%
    "breakeven_trigger":      0.08,
    # Time stop: close stagnant positions (Minervini 3-4 week rule)
    "time_stop_trading_days": 20,     # max holding days with no breakout
    "time_stop_min_gain_pct":  0.02,  # only close if gain < 2% (not a winner)
    # Pre-market gap check: cancel buy-stop if stock gaps above stop
    "premarket_gap_pct":       0.02,  # cancel if stock >2% above stop price
    # F1: Intraday 5-min entry timing (Haiku analysis at 09:20-09:29 ET)
    "use_intraday_timing":     False,  # enable when ready for live use
}

# ── Sector ETF map (SPDR) — shared across screener, main, position_monitor ───
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology":             "XLK",
    "Financial Services":     "XLF",
    "Financials":             "XLF",   # legacy alias
    "Healthcare":             "XLV",
    "Health Care":            "XLV",   # legacy alias
    "Energy":                 "XLE",
    "Consumer Cyclical":      "XLY",
    "Consumer Discretionary": "XLY",   # legacy alias
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Materials":              "XLB",   # legacy alias
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Consumer Defensive":     "XLP",
    "Consumer Staples":       "XLP",   # legacy alias
    "Communication Services": "XLC",
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


def _validate_config() -> None:
    """Assert that all critical config values are sane. Call at bot startup."""
    errors: list[str] = []

    def _chk(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # RISK sanity
    r = RISK
    _chk(0 < r["risk_per_trade_pct"] <= 0.03,
         f"risk_per_trade_pct={r['risk_per_trade_pct']} must be in (0, 0.03]")
    _chk(r["max_risk_per_trade_pct"] >= r["risk_per_trade_pct"],
         "max_risk_per_trade_pct must be >= risk_per_trade_pct")
    _chk(0 < r["max_daily_loss_pct"] <= 0.10,
         f"max_daily_loss_pct={r['max_daily_loss_pct']} must be in (0, 0.10]")
    _chk(r["max_drawdown_pct"] > r["max_daily_loss_pct"],
         "max_drawdown_pct must be > max_daily_loss_pct")
    _chk(r["max_portfolio_heat_pct"] > r["risk_per_trade_pct"],
         "max_portfolio_heat_pct must be > risk_per_trade_pct")
    _chk(isinstance(r["max_positions"], int) and 1 <= r["max_positions"] <= 20,
         f"max_positions={r['max_positions']} must be int in [1, 20]")
    _chk(isinstance(r.get("stop_distance_tiers", []), list) and r["stop_distance_tiers"],
         "stop_distance_tiers must be a non-empty list")

    # MONITOR sanity
    m = MONITOR
    _chk(1 <= m["interval_minutes"] <= 60,
         f"interval_minutes={m['interval_minutes']} must be in [1, 60]")
    _chk(0 < m["partial_exit_trigger"] < 1.0,
         f"partial_exit_trigger={m['partial_exit_trigger']} must be in (0, 1)")
    _chk(0 < m["trailing_stop_pct"] < 1.0,
         f"trailing_stop_pct={m['trailing_stop_pct']} must be in (0, 1)")
    _chk(0 < m["breakeven_trigger"] < m["partial_exit_trigger"],
         "breakeven_trigger must be > 0 and < partial_exit_trigger")

    # API key presence
    _chk(bool(ALPACA_API_KEY), "ALPACA_API_KEY is not set")
    _chk(bool(ALPACA_SECRET_KEY), "ALPACA_SECRET_KEY is not set")
    _chk(bool(ANTHROPIC_API_KEY), "ANTHROPIC_API_KEY is not set — Claude VCP analysis will fail")

    # Sector cap must not exceed total position cap
    _chk(r.get("max_positions_per_sector", 1) <= r["max_positions"],
         f"max_positions_per_sector ({r.get('max_positions_per_sector')}) > max_positions ({r['max_positions']})")

    # Paper-URL guard: refuse to run against live account unless explicitly opted in
    if not ALPACA_LIVE:
        _chk("paper" in ALPACA_BASE_URL.lower(),
             f"ALPACA_BASE_URL '{ALPACA_BASE_URL}' does not contain 'paper' — "
             "refusing to run against live account. Set ALPACA_LIVE=true in .env to enable live trading.")

    if errors:
        raise ValueError("Config validation failed:\n" + "\n".join(f"  • {e}" for e in errors))


_validate_config()
