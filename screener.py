"""
Layer 1 — SIMONS
Quantitative screening with full filter chain:
  1. Minervini Trend Template  (MA stack, 52w levels)
  2. MA20 short-term filter    (price above 20-day MA)
  3. RSI filter                (not overbought at entry, RSI <= 75)
  4. Weekly context            (weekly chart confirms daily trend)
  5. Earnings filter           (skip if report within 7 days)
"""
from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

from pathlib import Path
from config import TREND_TEMPLATE, LOG_DIR

_log = logging.getLogger(__name__)


# Stocks that cause issues (dual-class, non-US, warrants)
_BAD_SYMBOLS = {"BRK.B", "BF.B", "BRK-B", "BF-B"}

# Nasdaq 100 extras not typically in S&P 500 — high VCP potential
_NDQ_EXTRAS = [
    # Original Nasdaq extras
    "MELI","BKNG","LULU","REGN","VRTX","IDXX","ANSS","CDNS","SNPS",
    "DXCM","ALGN","ILMN","ZBRA","NXPI","MCHP","KLAC","MPWR",
    "ENPH","FSLR","CELH","APP","DUOL","AXON","DECK","CROX","LNTH",
    "ELF","SMCI","AEHR","FTNT","DDOG","MDB","NET","ZS","PANW","CRWD",
    "SNOW","NOW","HUBS","BILL","GTLB","IOT","TMDX","AAON","DOCS",
    # Russell 1000 mid-cap growth additions
    "PAYC","PODD","MEDP","CRNX","RXRX","RVMD","TGTX","ARDX","PRCT",
    "ICUI","INSP","IRTC","TNDM","NVST","RGEN","ITGR","KRYS","BBIO",
    "ACLS","ALGM","FORM","GTLS","HQY","LFST","OMCL","PDFS","PLUS",
    "POWL","PRVA","SKYW","SPSC","TBBK","TFIN","TRMK","UFPI","WDFC",
    "WTS","XPEL","YETI","ZWS","GFAI","MGEE","NRC","NTST",
]

_UNIVERSE_CACHE = LOG_DIR / "universe_cache.json"
_CACHE_DAYS = 7


def _load_universe_cache() -> list[str] | None:
    try:
        import json
        if _UNIVERSE_CACHE.exists():
            data = json.loads(_UNIVERSE_CACHE.read_text())
            age = (datetime.now() - datetime.fromisoformat(data["saved_at"])).days
            if age < _CACHE_DAYS:
                _log.info("[screen] Universe from cache: %d symbols (%d days old)",
                          len(data["symbols"]), age)
                return data["symbols"]
    except Exception:
        _log.debug("[%s] suppressed", __name__, exc_info=True)
    return None


def _save_universe_cache(symbols: list[str]) -> None:
    try:
        import json
        _UNIVERSE_CACHE.parent.mkdir(exist_ok=True)
        _UNIVERSE_CACHE.write_text(json.dumps({
            "saved_at": datetime.now().isoformat(),
            "symbols": symbols,
        }))
    except Exception as e:
        _log.warning("[screen] Universe cache save failed: %s", e)


def load_universe() -> list[str]:
    cached = _load_universe_cache()
    if cached:
        return cached

    symbols = set()

    # ── Source 1: GitHub CSV (reliable, no 403) ───────────────────────────────
    try:
        import requests as _req
        r = _req.get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
            "/main/data/constituents.csv",
            timeout=15,
        )
        r.raise_for_status()
        lines = r.text.strip().splitlines()[1:]   # skip header
        sp500 = [ln.split(",")[0].replace(".", "-") for ln in lines if ln]
        symbols.update(sp500)
        _log.info("[screen] S&P 500 from GitHub CSV: %d symbols", len(sp500))
    except Exception as e:
        _log.warning("[screen] GitHub CSV failed: %s", e)

    # ── Source 2: Wikipedia S&P 500 (fallback) ────────────────────────────────
    if len(symbols) < 400:
        try:
            sp500_wiki = pd.read_html(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            )[0]
            wiki_syms = sp500_wiki["Symbol"].str.replace(".", "-", regex=False).tolist()
            symbols.update(wiki_syms)
            _log.info("[screen] S&P 500 from Wikipedia: %d symbols", len(wiki_syms))
        except Exception as e:
            _log.warning("[screen] Wikipedia S&P 500 failed: %s", e)

    # ── Source 3: Nasdaq 100 extras ───────────────────────────────────────────
    symbols.update(_NDQ_EXTRAS)

    # ── Source 4: Alpaca — fractionable large-caps (quality proxy) ────────────
    if len(symbols) < 400:
        try:
            from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
            import requests as _req
            hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
            for exch in ("NYSE", "NASDAQ"):
                r = _req.get(f"{ALPACA_BASE_URL}/v2/assets",
                    params={"status": "active", "asset_class": "us_equity",
                            "exchange": exch},
                    headers=hdrs, timeout=15)
                for a in r.json():
                    if (a.get("fractionable") and a.get("tradable")
                            and "/" not in a["symbol"]
                            and len(a["symbol"]) <= 5):
                        symbols.add(a["symbol"])
            _log.info("[screen] Alpaca fractionable added, total now: %d", len(symbols))
        except Exception as e:
            _log.warning("[screen] Alpaca assets failed: %s", e)

    # ── Hardcoded quality fallback (always included) ──────────────────────────
    _QUALITY_CORE = [
        "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AVGO","LLY","V",
        "MA","JPM","UNH","PG","HD","JNJ","ABBV","MRK","CRM","NFLX","AMD",
        "QCOM","ADBE","ORCL","TXN","AMAT","LRCX","KLAC","ASML","TSM","INTC",
        "GS","MS","BAC","WFC","C","BLK","SPGI","MCO","ICE","CME",
        "UNP","CSX","NSC","FDX","UPS","LMT","RTX","NOC","GD","BA",
        "CAT","DE","ETN","PH","ROK","EMR","HON","MMM","GE","ITW",
        "COST","TGT","WMT","AMZN","EBAY","ETSY","W","ZM","PINS","SNAP",
    ]
    symbols.update(_QUALITY_CORE)

    symbols -= _BAD_SYMBOLS
    result = sorted(s for s in symbols if s and s.isalpha() or "-" in s)
    _log.info("[screen] Final universe: %d symbols", len(result))
    _save_universe_cache(result)
    return result


# ── Technical helpers ─────────────────────────────────────────────────────────

def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def _rs_rating(close: pd.Series, spy_close: pd.Series) -> float:
    n = min(len(close), len(spy_close), 252)
    if n < 60:
        return 50.0
    stock_perf = close.iloc[-1] / close.iloc[-n] - 1
    spy_perf   = spy_close.iloc[-1] / spy_close.iloc[-n] - 1
    rs = 50 + (stock_perf - spy_perf) * 200
    return float(np.clip(rs, 1, 99))


def _check_monthly_context(symbol: str) -> tuple[bool, str]:
    """
    Verify monthly chart is in Stage 2 uptrend: price above MA10m and MA40m.
    Uses 5-year monthly bars so we have enough history for MA40m.
    Returns (True, note) on failure — never blocks for missing data.
    """
    try:
        df_m    = yf.Ticker(symbol).history(period="5y", interval="1mo", auto_adjust=True)
        if len(df_m) < 12:
            return True, "insufficient_monthly_data"
        close_m  = df_m["Close"]
        ma10m    = float(close_m.rolling(10).mean().iloc[-1])
        price_m  = float(close_m.iloc[-1])
        if price_m < ma10m:
            return False, f"monthly_below_MA10m(${ma10m:.0f})"
        if len(df_m) >= 40:
            ma40m = float(close_m.rolling(40).mean().iloc[-1])
            if price_m < ma40m:
                return False, f"monthly_below_MA40m(${ma40m:.0f})"
            if ma10m < ma40m:
                return False, "monthly_MA10m_below_MA40m"
        return True, "monthly_stage2_ok"
    except Exception as e:
        return True, f"monthly_check_skipped:{e}"


def _check_weekly_context(symbol: str) -> tuple[bool, str]:
    """
    Verify that the weekly chart confirms the uptrend.
    Checks: price above MA10w (≈ MA50d) and MA40w (≈ MA200d) on weekly bars.
    """
    try:
        df_w  = yf.Ticker(symbol).history(period="2y", interval="1wk", auto_adjust=True)
        if len(df_w) < 45:
            return True, "insufficient_weekly_data"   # don't penalize for lack of data
        close_w = df_w["Close"]
        ma10w   = float(close_w.rolling(10).mean().iloc[-1])
        ma40w   = float(close_w.rolling(40).mean().iloc[-1])
        price_w = float(close_w.iloc[-1])
        if price_w < ma10w:
            return False, f"weekly_price_below_MA10w(${ma10w:.0f})"
        if price_w < ma40w:
            return False, f"weekly_price_below_MA40w(${ma40w:.0f})"
        if ma10w < ma40w:
            return False, "weekly_MA10w_below_MA40w"
        # Tight base check: last 4 weeks H-L range < 15% of price
        last4w  = df_w.tail(4)
        wk_high = float(last4w["High"].max())
        wk_low  = float(last4w["Low"].min())
        wk_rng  = (wk_high - wk_low) / price_w if price_w > 0 else 1.0
        if wk_rng > 0.15:
            return False, f"weekly_base_too_wide_{wk_rng:.1%}"
        return True, f"weekly_ok_tight_{wk_rng:.1%}"
    except Exception as e:
        return True, f"weekly_check_error:{e}"   # don't block on error


def _days_to_earnings(symbol: str) -> int | None:
    """
    Return number of calendar days until next earnings.
    Returns None if date cannot be determined.
    """
    try:
        ticker   = yf.Ticker(symbol)
        calendar = ticker.calendar
        if calendar is None or calendar.empty:
            return None
        # calendar may have 'Earnings Date' as index or column
        if "Earnings Date" in calendar.index:
            earn_date = calendar.loc["Earnings Date"].iloc[0]
        elif "Earnings Date" in calendar.columns:
            earn_date = calendar["Earnings Date"].iloc[0]
        else:
            return None
        if pd.isna(earn_date):
            return None
        earn_date = pd.Timestamp(earn_date).date()
        today     = date.today()
        delta     = (earn_date - today).days
        return delta if delta >= 0 else None
    except Exception:
        return None


def _days_to_exdividend(ticker) -> int | None:
    """Return calendar days until next ex-dividend date, or None if unavailable."""
    try:
        cal = ticker.calendar
        if cal is None or cal.empty:
            return None
        for field in ("Ex-Dividend Date", "Dividend Date"):
            if field in cal.index:
                val = cal.loc[field].iloc[0]
            elif field in cal.columns:
                val = cal[field].iloc[0]
            else:
                continue
            if pd.isna(val):
                continue
            ex_date = pd.Timestamp(val).date()
            delta   = (ex_date - date.today()).days
            return delta if delta >= 0 else None
    except Exception:
        _log.debug("[%s] suppressed", __name__, exc_info=True)
    return None


def _detect_candlestick(df: pd.DataFrame) -> str:
    """
    Detect the last candle's pattern.
    Returns: 'hammer', 'bullish_engulfing', 'doji', 'bullish', 'bearish', 'neutral'
    """
    if len(df) < 2:
        return "neutral"
    o1, h1, l1, c1 = (float(df["Open"].iloc[-2]), float(df["High"].iloc[-2]),
                       float(df["Low"].iloc[-2]),  float(df["Close"].iloc[-2]))
    o0, h0, l0, c0 = (float(df["Open"].iloc[-1]), float(df["High"].iloc[-1]),
                       float(df["Low"].iloc[-1]),  float(df["Close"].iloc[-1]))
    body0   = abs(c0 - o0)
    range0  = h0 - l0 if h0 > l0 else 0.001
    lower0  = min(o0, c0) - l0
    upper0  = h0 - max(o0, c0)

    # Doji: body < 10% of total range
    if body0 < range0 * 0.10:
        return "doji"

    # Hammer: small body in upper third, long lower shadow (>= 2x body), minimal upper shadow
    if lower0 >= 2 * body0 and upper0 <= body0 * 0.5 and (l0 + range0 * 0.67) >= min(o0, c0):
        return "hammer"

    # Bullish engulfing: today bullish, body completely engulfs yesterday's bearish body
    if c0 > o0 and c1 < o1 and c0 > o1 and o0 < c1:
        return "bullish_engulfing"

    # Simple bullish/bearish
    return "bullish" if c0 > o0 else "bearish"


def _rs_line_new_high(close: pd.Series, spy_close: pd.Series, lookback: int = 252) -> bool:
    """
    Return True if the RS line (stock/SPY ratio) is at a 52-week high today.
    Minervini's strongest single signal: RS line leads the price breakout.
    """
    n = min(len(close), len(spy_close), lookback)
    if n < 60:
        return False
    stock = close.iloc[-n:].values
    spy   = spy_close.iloc[-n:].values
    if len(stock) != len(spy) or spy[-1] == 0:
        return False
    rs_line = stock / spy
    return float(rs_line[-1]) >= float(rs_line.max()) * 0.995  # within 0.5% of 52w high



# ── Per-symbol full check ─────────────────────────────────────────────────────

@dataclass
class TrendResult:
    symbol: str
    passed: bool
    price: float              = 0.0
    ma20: float               = 0.0
    ma50: float               = 0.0
    ma150: float              = 0.0
    ma200: float              = 0.0
    ma200_slope_20d: float    = 0.0
    high_52w: float           = 0.0
    low_52w: float            = 0.0
    pct_from_high: float      = 0.0
    pct_from_low: float       = 0.0
    rs_rating: float          = 0.0
    rsi: float                = 0.0
    avg_volume: float         = 0.0
    weekly_ok: bool           = True
    weekly_note: str          = ""
    days_to_earnings: int | None = None
    last_candle: str          = ""
    rs_line_at_high: bool     = False  # RS line making 52-week high today
    rs_line_leading: bool     = False  # RS line at high while price still in base (strongest signal)
    eps_growth: float | None  = None   # trailing quarterly EPS growth (yfinance)
    revenue_growth: float | None = None  # trailing revenue growth (yfinance)
    market_cap: float | None     = None   # yfinance marketCap in USD
    rs_trending: bool            = False  # RS line rising: 4w > 8w > 12w (momentum building)
    rs_weekly_confirmed: bool    = False  # weekly RS also at 52w high (daily+weekly = institutional)
    ad_ratio: float              = 1.0    # up-vol / down-vol last 50 bars (>1 = accumulation)
    short_ratio: float | None    = None   # days to cover (high = squeeze potential)
    monthly_stage2: bool         = True   # monthly chart price > MA10m + MA40m (Stage 2 uptrend)
    eps_revision: float | None   = None   # forwardEps/trailingEps-1 (analyst revision proxy)
    rs_vs_sector: float | None   = None   # RS outperformance vs own sector ETF (>0 = leader)
    roe: float | None            = None   # returnOnEquity (>=0.15 = efficient capital use)
    adx: float               = 0.0    # Average Directional Index — trend strength (>25 = trending)
    float_rotation: float | None = None  # base vol / float shares (>1.0 = full float turned over)
    inst_pct: float | None   = None   # % held by institutions (sponsorship quality)
    eps_beat_count: int       = 0      # consecutive EPS beats in last 3 quarters
    at_52w_high: bool         = False  # price within 2% of 52w high (no overhead supply)
    accum_ratio: float        = 0.0   # accum days on above-avg vol / total up-days (base quality)
    three_weeks_tight: bool   = False  # price in <=1.5% range for 3+ consecutive weeks (Minervini)
    obv_new_high: bool        = False  # OBV at 52w high = institutional accumulation in base
    base_count: int           = 1      # prior VCP bases last 18mo (1-2=fresh, 3+=late stage)
    base_age_days: int        = 0      # trading days since 52w high peak (current base start)
    vol_contraction_quality: float = 0.0  # 0=none 0.5=partial 1.0=perfect vol decline in base
    near_ath: bool            = False  # price within 2% of 3-year high (no major overhead supply)
    weekly_stage2: bool       = False  # MA10w > MA30w AND MA30w slope rising (multi-TF Stage 2)
    rvol_5d: float            = 0.0   # 5d avg vol / 60d avg vol (>1.5 = stock is in play)
    weekly_breakout_aligned: bool = False  # daily breakout aligns with weekly 5-week high
    analyst_upgrades: bool    = False  # net analyst upgrades > downgrades in last 60 days
    inst_ownership_increasing: bool = False  # ≥3 institutional holders filed 13F within 90 days
    eps_revision_up: bool     = False  # analyst EPS consensus revised up last 30 days (upLast30 > downLast30)
    pocket_pivot: bool        = False  # today up-vol > max prior down-day vol in last 10 days
    eps_accelerating: bool    = False  # EPS growth rate accelerating Q-over-Q (Minervini SEPA core)
    accum_weeks_strong: bool  = False  # ≥8/13 up-volume weeks = sustained institutional accumulation
    insider_buying: bool      = False  # C-suite/director purchase in last 90 days (not option exercise)
    industry_leader: bool     = False  # stock's sector ETF in top-4 by 6-month momentum
    rev_accelerating: bool    = False  # quarterly revenue growth rate accelerating Q-over-Q
    three_weeks_tight: bool   = False  # last 3 weekly closes within 1.5% (O'Neil institutional hold)
    short_mo_pts: float       = 0.0   # short interest monthly change: +0.25 covering, -0.25 building
    analyst_pt_upside: bool   = False  # analyst consensus PT > current price × 1.25
    weinstein_stage: int      = 2      # 1=base, 2=uptrend, 3=top, 4=downtrend (Weinstein)
    fail_reason: str          = ""
    df: pd.DataFrame          = field(default=None, repr=False)

    def summary(self) -> str:
        status = "✓" if self.passed else "✗"
        earn   = f"  earn={self.days_to_earnings}d" if self.days_to_earnings is not None else ""
        return (f"{status} {self.symbol:<6} ${self.price:.2f}  "
                f"RSI={self.rsi:.0f}  RS={self.rs_rating:.0f}  "
                f"MA20={self.ma20:.0f}/{self.ma50:.0f}/{self.ma200:.0f}{earn}")


def _get_fundamentals(ticker) -> tuple:
    """Return (eps_g, rev_g, market_cap, short_ratio, eps_revision, roe, float_shares, inst_pct) from yfinance."""
    try:
        info    = ticker.info
        eps_g   = info.get("earningsQuarterlyGrowth") or info.get("earningsGrowth")
        rev_g   = info.get("revenueGrowth")
        mcap    = info.get("marketCap")
        srat    = info.get("shortRatio")
        fwd     = info.get("forwardEps")
        trail   = info.get("trailingEps")
        eps_rev = (float(fwd) / float(trail) - 1
                   if fwd and trail and float(trail) > 0 else None)
        roe_raw  = info.get("returnOnEquity")
        roe      = float(roe_raw) if roe_raw is not None else None
        float_sh = info.get("floatShares")
        inst_raw = info.get("heldPercentInstitutions")
        return (
            float(eps_g)   if eps_g   is not None else None,
            float(rev_g)   if rev_g   is not None else None,
            float(mcap)    if mcap    is not None else None,
            float(srat)    if srat    is not None else None,
            eps_rev,
            roe,
            float(float_sh) if float_sh is not None else None,
            float(inst_raw) if inst_raw is not None else None,
        )
    except Exception:
        return None, None, None, None, None, None, None, None


def _count_eps_beats(ticker) -> int:
    """Count how many of last 3 quarters the stock beat EPS estimates.
    Returns 0 if data unavailable.
    """
    try:
        cal = ticker.earnings_dates
        if cal is None or cal.empty:
            return 0
        needed = [c for c in cal.columns if "estimate" in c.lower() or "reported" in c.lower()]
        if len(needed) < 2:
            return 0
        est_col = next(c for c in needed if "estimate" in c.lower())
        rep_col = next(c for c in needed if "reported" in c.lower())
        recent  = cal[[est_col, rep_col]].dropna().head(3)
        if recent.empty:
            return 0
        return int(sum(
            1 for _, row in recent.iterrows()
            if float(row[rep_col]) > float(row[est_col])
        ))
    except Exception:
        return 0


def _compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """ADX via Wilder EWM smoothing. Returns 0.0 on error or insufficient data."""
    try:
        if len(df) < period * 2 + 1:
            return 0.0
        hi, lo, cl = df["High"], df["Low"], df["Close"]
        tr   = pd.concat([(hi - lo), (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
        up   = hi.diff()
        dn   = -lo.diff()
        pdm  = up.where((up > dn) & (up > 0), 0.0)
        ndm  = dn.where((dn > up) & (dn > 0), 0.0)
        a    = 1 / period
        atr_s  = tr.ewm(alpha=a, adjust=False).mean()
        pdi_s  = pdm.ewm(alpha=a, adjust=False).mean()
        ndi_s  = ndm.ewm(alpha=a, adjust=False).mean()
        p_pct  = (pdi_s / atr_s * 100).fillna(0)
        n_pct  = (ndi_s / atr_s * 100).fillna(0)
        denom  = (p_pct + n_pct).replace(0, float("nan"))
        dx     = ((p_pct - n_pct).abs() / denom * 100).fillna(0)
        adx    = dx.ewm(alpha=a, adjust=False).mean()
        return round(float(adx.iloc[-1]), 1)
    except Exception:
        return 0.0


def _check_symbol(symbol: str, spy_close: pd.Series, cfg: dict,
                  prefetched_df: pd.DataFrame | None = None) -> TrendResult:
    try:
        ticker = yf.Ticker(symbol)
        if prefetched_df is not None and not prefetched_df.empty and len(prefetched_df) >= 150:
            df = prefetched_df
        else:
            df = ticker.history(period="1y", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 150:
            return TrendResult(symbol=symbol, passed=False, fail_reason="insufficient_data")
        # IPO age filter: <240 bars on 1-year download = stock public <~1 year
        # New listings lack the base-building and institutional accumulation VCPs require
        if len(df) < 240:
            return TrendResult(symbol=symbol, passed=False, fail_reason="ipo_too_recent")

        close  = df["Close"]
        volume = df["Volume"]
        price  = float(close.iloc[-1])

        if price < cfg.get("min_price", 10):
            return TrendResult(symbol=symbol, passed=False, price=price, fail_reason="price_too_low")
        avg_vol = float(volume.tail(50).mean())
        if avg_vol < cfg.get("min_avg_volume", 500_000):
            return TrendResult(symbol=symbol, passed=False, price=price, fail_reason="volume_too_low")
        if price * avg_vol < cfg.get("min_dollar_volume", 5_000_000):
            return TrendResult(symbol=symbol, passed=False, price=price, fail_reason="dollar_volume_too_low")

        ma20  = float(close.rolling(20).mean().iloc[-1])
        ma50  = float(close.rolling(50).mean().iloc[-1])
        ma150 = float(close.rolling(150).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1])

        ma200_series  = close.rolling(200).mean()
        ma200_20d_ago = float(ma200_series.iloc[-20]) if len(ma200_series) >= 220 else ma200
        ma200_slope   = (ma200 - ma200_20d_ago) / ma200_20d_ago

        high_52w     = float(close.tail(252).max())
        low_52w      = float(close.tail(252).min())
        pct_from_high = (price - high_52w) / high_52w
        pct_from_low  = (price - low_52w) / low_52w

        rs  = _rs_rating(close, spy_close)
        rsi = _calc_rsi(close, cfg.get("rsi_period", 14))

        # Accumulation/Distribution ratio: up-vol vs down-vol last 50 bars
        _n_ad = min(50, len(df))
        _df50 = df.tail(_n_ad)
        _up_v = float(_df50.loc[_df50["Close"] >= _df50["Open"], "Volume"].sum())
        _dn_v = float(_df50.loc[_df50["Close"] <  _df50["Open"], "Volume"].sum())
        ad_ratio = (_up_v / _dn_v) if _dn_v > 0 else 2.0
        adx_val  = _compute_adx(df)
        # At 52-week high: within 2% = no overhead supply from prior holders
        at_52w_high_val = abs(pct_from_high) <= 0.02
        # Accumulation days: up-close bars on above-avg volume in the base
        _up_days_df     = _df50[_df50["Close"] >= _df50["Open"]]
        _accum_d        = _up_days_df[_up_days_df["Volume"] >= avg_vol]
        accum_ratio_val = round(_accum_d.shape[0] / max(_up_days_df.shape[0], 1), 3)

        # Earnings check (do early — cheap to skip)
        days_earn = _days_to_earnings(symbol)
        earn_min  = cfg.get("earnings_min_days_away", 7)

        # Candlestick pattern on last bar
        last_candle = _detect_candlestick(df)

        result = TrendResult(
            symbol=symbol, price=price,
            ma20=ma20, ma50=ma50, ma150=ma150, ma200=ma200,
            ma200_slope_20d=ma200_slope,
            high_52w=high_52w, low_52w=low_52w,
            pct_from_high=pct_from_high, pct_from_low=pct_from_low,
            rs_rating=rs, rsi=rsi, avg_volume=avg_vol,
            days_to_earnings=days_earn, last_candle=last_candle,
            ad_ratio=ad_ratio,
            passed=False, df=df,
        )

        # ── Filter chain ─────────────────────────────────────────────────────

        # 1. Trend Template (Minervini)
        checks = [
            (price > ma150 and price > ma200,       "price_below_ma150/200"),
            (ma150 > ma200,                          "ma150_below_ma200"),
            (ma200_slope > 0,                        "ma200_not_trending_up"),
            (ma50 > ma150 and ma50 > ma200,          "ma50_below_ma150/200"),
            (price > ma50,                           "price_below_ma50"),
            (pct_from_high >= -cfg.get("within_pct_of_52w_high", 0.25),
                                                     "too_far_from_52w_high"),
            (pct_from_low >= cfg.get("above_pct_of_52w_low", 0.30),
                                                     "too_close_to_52w_low"),
            (rs >= cfg.get("rs_min", 70),            "rs_too_low"),
        ]
        for condition, reason in checks:
            if not condition:
                result.fail_reason = reason
                return result

        # 2. MA20 filter
        if cfg.get("price_above_ma20", True) and price < ma20:
            result.fail_reason = f"price_below_ma20(${ma20:.0f})"
            return result

        # 3. RSI filter — skip overbought entries
        rsi_max = cfg.get("rsi_max_entry", 75)
        if rsi > rsi_max:
            result.fail_reason = f"rsi_overbought({rsi:.0f}>{rsi_max})"
            return result

        # 4. Earnings filter — skip if report imminent
        if days_earn is not None and days_earn <= earn_min:
            result.fail_reason = f"earnings_in_{days_earn}_days"
            return result

        # 4b. Ex-dividend guard: skip if ex-date within 3 days
        # Buying before ex-date means holding through the dividend drop — a guaranteed
        # opening-gap loss that ruins a VCP breakout entry.
        _exdiv_days = _days_to_exdividend(ticker)
        if _exdiv_days is not None and _exdiv_days <= cfg.get("exdiv_min_days_away", 3):
            result.fail_reason = f"exdiv_in_{_exdiv_days}_days"
            return result

        # 5. Weekly context
        if cfg.get("weekly_context", True):
            weekly_ok, weekly_note = _check_weekly_context(symbol)
            result.weekly_ok   = weekly_ok
            result.weekly_note = weekly_note
            if not weekly_ok:
                result.fail_reason = weekly_note
                return result

        # 5b. Monthly Stage 2: three-timeframe alignment (monthly+weekly+daily)
        if cfg.get("monthly_context", True):
            monthly_ok, monthly_note = _check_monthly_context(symbol)
            result.monthly_stage2 = monthly_ok
            if not monthly_ok:
                # Soft reject: only block if weekly also shows weakness
                # (monthly alone may lag; require both to fail before blocking)
                if not result.weekly_ok or weekly_note.startswith("weekly_MA"):
                    result.fail_reason = monthly_note
                    return result
                _log.debug("[screen] %s monthly Stage 2 weak (%s) — weekly OK, continuing", symbol, monthly_note)

        # 6. Fundamental filter — fetch only for stocks that passed all technical checks
        # Hard-reject only on clearly declining earnings (>10%); unknown data passes through
        eps_g, rev_g, market_cap, short_ratio, eps_rev, roe, float_sh, inst_pct = _get_fundamentals(ticker)
        result.eps_growth     = eps_g
        result.revenue_growth = rev_g
        result.market_cap     = market_cap
        result.short_ratio    = short_ratio
        result.eps_revision   = eps_rev
        result.roe            = roe
        result.adx            = adx_val
        result.at_52w_high    = at_52w_high_val
        result.accum_ratio    = accum_ratio_val
        result.inst_pct       = inst_pct
        result.eps_beat_count = _count_eps_beats(ticker)
        if float_sh and float_sh > 0:
            result.float_rotation = round(float(df.tail(40)["Volume"].sum()) / float_sh, 3)
        # Liquidity floor: $500M+ to avoid thin-volume micro-caps
        # No upper cap — VCP logic is size-agnostic (NVDA, AAPL, MSFT all form VCPs)
        if market_cap is not None and market_cap < 500_000_000:
            result.fail_reason = f"market_cap_too_small(${market_cap/1e9:.1f}B)"
            return result
        if eps_g is not None and eps_g < -0.10:
            result.fail_reason = f"eps_declining({eps_g:.0%})"
            return result
        # Institutional ownership floor: real VCP stocks have institutional sponsorship.
        # Speculative pump-stocks, pre-revenue biotech and lottery tickets have near-zero
        # institutional ownership. ≥5% = at least one serious fund has done due diligence.
        # Only block if yfinance returns a confident reading (not None).
        if inst_pct is not None and inst_pct < 0.05:
            result.fail_reason = f"institutional_ownership_too_low({inst_pct:.1%})"
            return result

        # Balance sheet quality gate: Minervini — avoid financially stressed companies
        # FCF < 0 = burning cash; D/E ≥ 2.0 = over-leveraged; current ratio < 1 = liquidity risk
        try:
            _bs_info = ticker.info
            _fcf     = _bs_info.get("freeCashflow")
            _de      = _bs_info.get("debtToEquity")        # reported as percentage, e.g. 150 = 1.5×
            _cr      = _bs_info.get("currentRatio")
            _bs_fail = None
            if _fcf is not None and float(_fcf) < 0:
                _bs_fail = f"fcf_negative(${float(_fcf)/1e6:.0f}M)"
            elif _de is not None and float(_de) > 200:     # >200 = D/E > 2.0×
                _bs_fail = f"debt_equity_high({float(_de)/100:.1f}x)"
            elif _cr is not None and float(_cr) < 1.0:
                _bs_fail = f"current_ratio_low({float(_cr):.2f})"
            if _bs_fail:
                result.fail_reason = _bs_fail
                return result
        except Exception:
            pass    # yfinance data missing — allow through, don't block on data gaps

        result.rs_line_at_high = _rs_line_new_high(close, spy_close)
        if result.rs_line_at_high:
            _log.debug("[screen] %s RS line at 52-week high — strong signal", symbol)
        # RS line LEADING price = RS at high while price still 3–15% below 52w high
        # This is Minervini's strongest early signal: RS breaks out before price does
        result.rs_line_leading = (
            result.rs_line_at_high
            and -0.15 <= pct_from_high <= -0.03
        )
        if result.rs_line_leading:
            _log.info("[screen] %s RS LINE LEADING price breakout — elite setup", symbol)
        # RS trending: RS line slope improving over 4w > 8w > 12w = momentum building
        if len(close) >= 60 and len(spy_close) >= 60:
            _rs_now = float(close.iloc[-1])  / float(spy_close.iloc[-1])
            _rs_4w  = float(close.iloc[-21]) / float(spy_close.iloc[-21])
            _rs_8w  = float(close.iloc[-42]) / float(spy_close.iloc[-42])
            result.rs_trending = (_rs_now > _rs_4w > _rs_8w)
            if result.rs_trending:
                _log.debug("[screen] %s RS trending up — accumulation building", symbol)
        # Weekly RS confirmation: check if RS line also at 52w high on weekly bars
        if result.rs_line_at_high:
            try:
                _common   = close.index.intersection(spy_close.index)
                _rs_daily = (close.loc[_common] / spy_close.loc[_common]).dropna()
                _rs_w     = _rs_daily.resample("W").last().dropna()
                if len(_rs_w) >= 52:
                    result.rs_weekly_confirmed = (
                        float(_rs_w.iloc[-1]) >= float(_rs_w.tail(52).max()) * 0.995
                    )
                    if result.rs_weekly_confirmed:
                        _log.info("[screen] %s RS weekly CONFIRMED — daily+weekly at 52w high", symbol)
            except Exception:
                _log.debug("[%s] suppressed", __name__, exc_info=True)
        # 3-weeks tight: price H-L range <=1.5% across 3 consecutive weeks
        # Minervini's strongest compression signal — coiled spring before explosive move
        _three_wt = False
        try:
            _wk = ticker.history(period="4mo", interval="1wk", auto_adjust=True)
            if len(_wk) >= 4:
                for _wi in range(len(_wk) - 1, max(len(_wk) - 7, 1), -1):
                    _w3 = _wk.iloc[max(_wi - 2, 0):_wi + 1]
                    if len(_w3) == 3:
                        _rng = (_w3["High"].max() - _w3["Low"].min()) / float(_w3["Low"].min())
                        if _rng <= 0.015:
                            _three_wt = True
                            break
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.three_weeks_tight = _three_wt
        if _three_wt:
            _log.info("[screen] %s 3-WEEKS TIGHT — elite Minervini compression", symbol)

        # OBV: On-Balance Volume at/near 52-week high = institutional buying in base
        _obv_nh = False
        try:
            _dir = df["Close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
            _obv = (df["Volume"] * _dir).cumsum()
            if len(_obv) >= 50:
                _obv_nh = float(_obv.iloc[-1]) >= float(_obv.tail(252).max()) * 0.99
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.obv_new_high = _obv_nh

        # Base count: number of prior consolidation bases in last 18 months
        # Base 1-2 = fresh breakout (best R:R); base 3+ = late stage (higher failure)
        _bcnt = 1
        try:
            _cl18 = df["Close"].tail(378)
            if len(_cl18) >= 80:
                _pk_b  = float(_cl18.iloc[0])
                _in_b  = False
                for _p_b in _cl18.iloc[1:]:
                    _pf = float(_p_b)
                    if _pf > _pk_b * 1.02:
                        if _in_b:
                            _bcnt += 1
                            _in_b = False
                        _pk_b = _pf
                    elif _pf < _pk_b * 0.85:
                        _in_b = True
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.base_count = min(_bcnt, 5)
        if _bcnt >= 3:
            _log.debug("[screen] %s base count=%d — late stage penalty applied", symbol, _bcnt)

        # Base age: trading days since 52-week high (proxy for current base duration)
        # Long bases (>120d) lose momentum; fresh bases (<60d) have best breakout odds
        _base_age = 0
        try:
            _cl252 = df["Close"].tail(252)
            _peak_idx = int(_cl252.values.argmax())
            _base_age = len(_cl252) - 1 - _peak_idx
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.base_age_days = _base_age
        if _base_age > 120:
            _log.debug("[screen] %s base age=%dd — old base, momentum penalty", symbol, _base_age)

        # Volume contraction quality: each 20-day segment in base has lower avg volume
        # Perfect contraction = disciplined selling pressure drying up (ideal VCP)
        _vq = 0.0
        try:
            _v60 = df["Volume"].tail(60).reset_index(drop=True)
            if len(_v60) >= 45:
                _s1 = float(_v60.iloc[:20].mean())
                _s2 = float(_v60.iloc[20:40].mean())
                _s3 = float(_v60.iloc[40:].mean())
                if _s1 > 0 and _s2 < _s1 and _s3 < _s2:
                    _vq = 1.0
                elif _s1 > 0 and (_s2 < _s1 or _s3 < _s2):
                    _vq = 0.5
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.vol_contraction_quality = _vq

        # RVOL 5-day: uses df already fetched — no extra API call
        _rvol_5d = 0.0
        try:
            _rv_recent = float(df["Volume"].tail(5).mean())
            _rv_base   = float(df["Volume"].tail(65).iloc[:-5].mean())
            if _rv_base > 0:
                _rvol_5d = round(_rv_recent / _rv_base, 2)
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.rvol_5d = _rvol_5d

        # Near ATH + weekly Stage 2 + weekly breakout alignment: single 3-year weekly fetch
        _near_ath = False
        _wk_s2    = False
        _wk_bo    = False
        try:
            _wk3y = ticker.history(period="3y", interval="1wk", auto_adjust=True)
            if len(_wk3y) >= 10:
                _ath3    = float(_wk3y["High"].max())
                _wk_cur  = float(_wk3y["Close"].iloc[-1])
                _near_ath = _wk_cur >= _ath3 * 0.98
                if len(_wk3y) >= 35:
                    _c3y       = _wk3y["Close"]
                    _ma10w     = float(_c3y.tail(10).mean())
                    _ma30w     = float(_c3y.tail(30).mean())
                    _ma30w_prv = float(_c3y.iloc[-34:-4].mean())
                    _wk_s2     = _ma10w > _ma30w and _ma30w > _ma30w_prv
                if len(_wk3y) >= 6:
                    # Weekly breakout alignment: current week high >= 5-week prior high
                    _wk5h = float(_wk3y["High"].iloc[-6:-1].max())
                    _wk_bo = float(_wk3y["High"].iloc[-1]) >= _wk5h
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.near_ath               = _near_ath
        result.weekly_stage2          = _wk_s2
        result.weekly_breakout_aligned = _wk_bo

        # Analyst upgrades: net Buy/Outperform > Sell/Underperform in last 60 days
        _analyst_up = False
        try:
            _upg_df = ticker.upgrades_downgrades
            if _upg_df is not None and not _upg_df.empty:
                _upg_df = _upg_df.reset_index()
                _dcol = next((c for c in _upg_df.columns if "date" in c.lower()), None)
                _gcol = next((c for c in _upg_df.columns
                              if "tograde" in c.lower() or "action" in c.lower()), None)
                if _dcol and _gcol:
                    _upg_df["_dt"] = pd.to_datetime(_upg_df[_dcol], utc=True, errors="coerce")
                    _cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=60)
                    _gr = _upg_df[_upg_df["_dt"] >= _cutoff][_gcol].str.lower()
                    _pos = int(_gr.str.contains("buy|outperform|overweight|upgrade").sum())
                    _neg = int(_gr.str.contains("sell|underperform|underweight|downgrade").sum())
                    _analyst_up = _pos > _neg and _pos >= 1
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.analyst_upgrades = _analyst_up

        # Institutional accumulation: ≥3 holders with recent (≤90 day) 13F filings
        _inst_inc = False
        try:
            _ih = ticker.institutional_holders
            if _ih is not None and not _ih.empty and "Date Reported" in _ih.columns:
                _cutoff_ih = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)
                _ih_dt = pd.to_datetime(_ih["Date Reported"], utc=True, errors="coerce")
                _inst_inc = int((_ih_dt >= _cutoff_ih).sum()) >= 3
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.inst_ownership_increasing = _inst_inc

        # EPS revision momentum: analyst consensus raised for upcoming quarter/year
        _eps_rev_up = False
        try:
            _rev_df = ticker.eps_revisions
            if _rev_df is not None and not _rev_df.empty:
                _up30   = int(_rev_df.get("upLast30days",   pd.Series([0])).sum())
                _down30 = int(_rev_df.get("downLast30days", pd.Series([0])).sum())
                _eps_rev_up = _up30 > _down30 and _up30 >= 2
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.eps_revision_up = _eps_rev_up

        # Pocket pivot: today's up-day volume exceeds the highest down-day volume
        # in the prior 10 sessions — O'Neil/Morales early-entry confirmation signal
        _pp = False
        try:
            if len(df) >= 12:
                _df_pp = df.tail(11).reset_index(drop=True)
                _down_vols = [
                    float(_df_pp["Volume"].iloc[_i])
                    for _i in range(1, len(_df_pp) - 1)
                    if float(_df_pp["Close"].iloc[_i]) < float(_df_pp["Close"].iloc[_i - 1])
                ]
                _today_up = float(_df_pp["Close"].iloc[-1]) > float(_df_pp["Close"].iloc[-2])
                _today_vol = float(_df_pp["Volume"].iloc[-1])
                if _down_vols and _today_up and _today_vol > max(_down_vols):
                    _pp = True
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.pocket_pivot = _pp

        # Earnings acceleration: EPS growth rate increasing Q-over-Q = core Minervini SEPA criterion
        # Accelerating growth (e.g. +20%,+35%,+50%) signals institutional conviction; decelerating = danger
        _eps_accel = False
        try:
            _qe = ticker.quarterly_earnings
            if _qe is not None and not _qe.empty and len(_qe) >= 4:
                _qe_s = _qe.sort_index()
                _growths = []
                for _qi in range(1, len(_qe_s)):
                    _prev_e = float(_qe_s["Earnings"].iloc[_qi - 1])
                    _curr_e = float(_qe_s["Earnings"].iloc[_qi])
                    if _prev_e != 0:
                        _growths.append((_curr_e - _prev_e) / abs(_prev_e))
                if len(_growths) >= 3:
                    _eps_accel = (_growths[-1] > _growths[-2] > _growths[-3]
                                  and _growths[-1] > 0 and _growths[-2] > 0)
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.eps_accelerating = _eps_accel

        # 13-week accumulation score: O'Neil up/down volume weeks ratio
        # ≥8 of last 13 weeks closed up on positive volume = sustained institutional buying
        _accum_strong = False
        try:
            _wk_acc = ticker.history(period="4mo", interval="1wk", auto_adjust=True)
            if len(_wk_acc) >= 13:
                _wk_acc = _wk_acc.tail(13).reset_index(drop=True)
                _up_weeks = sum(
                    1 for _wi in range(len(_wk_acc))
                    if float(_wk_acc["Close"].iloc[_wi]) >= float(_wk_acc["Open"].iloc[_wi])
                )
                _accum_strong = _up_weeks >= 8
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.accum_weeks_strong = _accum_strong

        # Insider buying: recent C-suite/director purchase = strongest alignment signal
        # Filters out option exercises; only counts open-market purchases
        _insider_buy = False
        try:
            _it = ticker.insider_transactions
            if _it is not None and not _it.empty:
                _cutoff_it = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)
                for _, _row in _it.iterrows():
                    _tx_type = str(_row.get("Transaction", "")).lower()
                    _tx_date = pd.to_datetime(_row.get("Start Date", None), utc=True, errors="coerce")
                    _tx_sh   = float(_row.get("Shares", 0) or 0)
                    if (not pd.isnull(_tx_date)
                            and _tx_date >= _cutoff_it
                            and _tx_sh > 0
                            and "purchase" in _tx_type
                            and "sale" not in _tx_type):
                        _insider_buy = True
                        break
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.insider_buying = _insider_buy

        # Industry group momentum rank: stock's sector ETF in top-4 by 6-month return
        # O'Neil: 37% of a stock's move comes from its industry group trend
        _ind_leader = False
        try:
            from config import SECTOR_ETF_MAP as _etf_map_il
            _sec_sym = get_sector(symbol)
            _etf_sym = _etf_map_il.get(_sec_sym)
            if _etf_sym:
                _all_etfs = ["XLK", "XLV", "XLF", "XLE", "XLY", "XLI", "XLB", "XLRE", "XLU", "XLP", "XLC"]
                _idf = yf.download(_all_etfs, period="6mo", interval="1d",
                                   auto_adjust=True, progress=False)["Close"]
                _rets = {}
                for _e in _all_etfs:
                    if _e in _idf.columns:
                        _col = _idf[_e].dropna()
                        if len(_col) >= 100:
                            _rets[_e] = float(_col.iloc[-1] / _col.iloc[0] - 1)
                if _etf_sym in _rets and _rets:
                    _ranked_il = sorted(_rets, key=lambda x: -_rets[x])
                    _ind_leader = _etf_sym in _ranked_il[:4]
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.industry_leader = _ind_leader

        # Revenue acceleration: quarterly revenue growth rate increasing Q-over-Q
        # When BOTH EPS and revenue are accelerating = strongest Minervini SEPA signal
        _rev_accel = False
        try:
            _qfin = ticker.quarterly_financials
            if _qfin is not None and not _qfin.empty and "Total Revenue" in _qfin.index:
                _rev_s = _qfin.loc["Total Revenue"].sort_index()
                if len(_rev_s) >= 4:
                    _rev_gs = []
                    for _ri in range(1, len(_rev_s)):
                        _pr = float(_rev_s.iloc[_ri - 1])
                        _cr = float(_rev_s.iloc[_ri])
                        if _pr > 0:
                            _rev_gs.append((_cr - _pr) / _pr)
                    if len(_rev_gs) >= 3:
                        _rev_accel = (
                            _rev_gs[-1] > _rev_gs[-2] > _rev_gs[-3]
                            and _rev_gs[-1] > 0 and _rev_gs[-2] > 0
                        )
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.rev_accelerating = _rev_accel

        # 3-Weeks Tight: three consecutive weekly closes within 1.5% — O'Neil consolidation signal
        # Distinct from weekly tight base (H-L range): this specifically checks CLOSE prices
        # showing institutions are holding, not distributing, during the consolidation
        _twt = False
        try:
            _wk_twt = ticker.history(period="6wk", interval="1wk", auto_adjust=True)
            if len(_wk_twt) >= 4:
                _wc = _wk_twt["Close"].iloc[-4:].values
                _wc_max = max(_wc[-3:])
                _wc_min = min(_wc[-3:])
                if _wc_min > 0:
                    _twt = (_wc_max - _wc_min) / _wc_min <= 0.015
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.three_weeks_tight = _twt

        # Short interest monthly change: rapid build = bearish, rapid cover = squeeze fuel
        _si_pts = 0.0
        try:
            _info_si = ticker.info
            _si_cur  = float(_info_si.get("sharesShort", 0) or 0)
            _si_prev = float(_info_si.get("sharesShortPriorMonth", 0) or 0)
            if _si_prev > 0:
                _si_chg = (_si_cur - _si_prev) / _si_prev
                if _si_chg <= -0.20:
                    _si_pts = 0.25   # shorts covering aggressively = squeeze fuel
                elif _si_chg >= 0.25:
                    _si_pts = -0.25  # shorts building = someone knows something
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.short_mo_pts = _si_pts

        # Analyst consensus price target: >25% above current price = institutional expected upside
        _apt = False
        try:
            _info_apt = ticker.info
            _pt_mean  = float(_info_apt.get("targetMeanPrice", 0) or 0)
            _pt_cur   = float(_info_apt.get("currentPrice", price) or price)
            if _pt_mean > 0 and _pt_cur > 0:
                _apt = _pt_mean / _pt_cur >= 1.25
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
        result.analyst_pt_upside = _apt

        if _near_ath:
            _log.info("[screen] %s near 3-year ATH — no overhead supply", symbol)
        if _wk_s2:
            _log.debug("[screen] %s weekly Stage 2 confirmed", symbol)

        # ── Weinstein Stage classifier (Stan Weinstein method) ─────────────────
        # Stage 1: Basing — price sideways near MA200, MA200 flat
        # Stage 2: Advancing — price above rising MA200 (we only trade Stage 2)
        # Stage 3: Top — price extended, MA200 still rising but decelerating
        # Stage 4: Declining — price below MA200
        try:
            _ma200_slope_pct = ma200_slope  # already computed above
            _pct_above_ma200 = (price - ma200) / ma200 if ma200 > 0 else 0
            if price < ma200:
                result.weinstein_stage = 4  # Stage 4: below MA200
            elif _ma200_slope_pct <= 0.001:
                result.weinstein_stage = 1  # Stage 1: flat MA200 = basing
            elif _pct_above_ma200 > 0.20 and _ma200_slope_pct < 0.002:
                result.weinstein_stage = 3  # Stage 3: extended, MA200 decelerating
            else:
                result.weinstein_stage = 2  # Stage 2: price above rising MA200
            _log.debug("[screen] %s Weinstein Stage %d", symbol, result.weinstein_stage)
        except Exception:
            result.weinstein_stage = 2

        result.passed = True
        return result

    except Exception as e:
        return TrendResult(symbol=symbol, passed=False, fail_reason=f"error:{e}")


# ── Sector lookup with persistent cache ──────────────────────────────────────

_SECTOR_CACHE_FILE = LOG_DIR / "sector_cache.json"
_sector_mem: dict[str, str] = {}


def get_sector(symbol: str) -> str:
    """Return GICS sector for symbol. Cached to logs/sector_cache.json."""
    import json as _json
    global _sector_mem
    if not _sector_mem:
        try:
            if _SECTOR_CACHE_FILE.exists():
                _sector_mem = _json.loads(_SECTOR_CACHE_FILE.read_text())
        except Exception:
            _log.debug("[%s] suppressed", __name__, exc_info=True)
    if symbol in _sector_mem:
        return _sector_mem[symbol]
    try:
        info   = yf.Ticker(symbol).info
        sector = info.get("sector") or "Unknown"
    except Exception:
        sector = "Unknown"
    _sector_mem[symbol] = sector
    try:
        _SECTOR_CACHE_FILE.parent.mkdir(exist_ok=True)
        _SECTOR_CACHE_FILE.write_text(_json.dumps(_sector_mem, indent=2))
    except Exception:
        _log.debug("[%s] suppressed", __name__, exc_info=True)
    _log.debug("[screen] %s sector: %s", symbol, sector)
    return sector


def _bulk_download_daily(symbols: list[str], chunk_size: int = 150) -> dict[str, pd.DataFrame]:
    """Bulk-download 1-year daily OHLCV for all symbols in chunks of chunk_size.
    Returns {symbol: DataFrame}. Falls back gracefully per-chunk on error.
    """
    result: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        try:
            raw = yf.download(
                chunk, period="1y", interval="1d",
                auto_adjust=True, progress=False, threads=True,
            )
            if raw.empty:
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                for sym in chunk:
                    try:
                        sym_df = raw.xs(sym, axis=1, level=1).dropna(how="all")
                        if not sym_df.empty:
                            result[sym] = sym_df
                    except Exception:
                        _log.debug("[%s] suppressed", __name__, exc_info=True)
            elif chunk:
                result[chunk[0]] = raw
        except Exception as e:
            _log.warning("[screen] Bulk download chunk %d-%d failed: %s", i, i + chunk_size, e)
    return result


def run(symbols: list[str] | None = None, workers: int = 10) -> list[TrendResult]:
    if symbols is None:
        symbols = load_universe()

    _log.info("[screen] Bulk-fetching 1y daily bars for %d symbols + SPY...", len(symbols))
    bulk = _bulk_download_daily(list(symbols) + ["SPY"])
    spy_df = bulk.pop("SPY", None)
    try:
        spy_close = spy_df["Close"] if spy_df is not None and not spy_df.empty else pd.Series(dtype=float)
    except Exception:
        spy_close = pd.Series(dtype=float)
    _log.info("[screen] Bulk download complete — %d/%d symbols fetched", len(bulk), len(symbols))

    cfg     = TREND_TEMPLATE
    results: list[TrendResult] = []

    _log.info("[screen] Screening %d symbols...", len(symbols))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_symbol, sym, spy_close, cfg, bulk.get(sym)): sym
                   for sym in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                r = fut.result()
                results.append(r)
            except Exception as e:
                _log.warning("[screen] %s: %s", futures[fut], e)
            if i % 50 == 0:
                passed = sum(1 for r in results if r.passed)
                _log.info("[screen] %d/%d done (%d passed)", i, len(symbols), passed)

    results.sort(key=lambda r: (-int(r.passed), -r.rs_rating))
    passed_n = sum(1 for r in results if r.passed)
    _log.info("[screen] Done: %d/%d passed all filters", passed_n, len(results))

    # Log filter breakdown
    if results:
        fail_counts: dict[str, int] = {}
        for r in results:
            if not r.passed:
                key = r.fail_reason.split("(")[0].split("_")[0] if r.fail_reason else "unknown"
                fail_counts[key] = fail_counts.get(key, 0) + 1
        top_fails = sorted(fail_counts.items(), key=lambda x: -x[1])[:5]
        _log.info("[screen] Top fail reasons: %s", top_fails)

    return results
