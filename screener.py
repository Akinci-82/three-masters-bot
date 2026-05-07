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
    "MELI","BKNG","LULU","REGN","VRTX","IDXX","ANSS","CDNS","SNPS",
    "DXCM","SGEN","ALGN","ILMN","ZBRA","NXPI","MCHP","KLAC","MPWR",
    "ENPH","FSLR","CELH","APP","DUOL","AXON","DECK","CROX","LNTH",
    "ELF","SMCI","AEHR","FTNT","DDOG","MDB","NET","ZS","PANW","CRWD",
    "SNOW","NOW","HUBS","BILL","GTLB","IOT","TMDX","AAON","DOCS",
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
        pass
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
    fail_reason: str          = ""
    df: pd.DataFrame          = field(default=None, repr=False)

    def summary(self) -> str:
        status = "✓" if self.passed else "✗"
        earn   = f"  earn={self.days_to_earnings}d" if self.days_to_earnings is not None else ""
        return (f"{status} {self.symbol:<6} ${self.price:.2f}  "
                f"RSI={self.rsi:.0f}  RS={self.rs_rating:.0f}  "
                f"MA20={self.ma20:.0f}/{self.ma50:.0f}/{self.ma200:.0f}{earn}")


def _get_fundamentals(ticker) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (eps_quarterly_growth, revenue_growth, market_cap, short_ratio) from yfinance."""
    try:
        info = ticker.info
        eps_g = info.get("earningsQuarterlyGrowth") or info.get("earningsGrowth")
        rev_g = info.get("revenueGrowth")
        mcap  = info.get("marketCap")
        srat  = info.get("shortRatio")
        return (
            float(eps_g) if eps_g is not None else None,
            float(rev_g) if rev_g is not None else None,
            float(mcap)  if mcap  is not None else None,
            float(srat)  if srat  is not None else None,
        )
    except Exception:
        return None, None, None, None


def _check_symbol(symbol: str, spy_close: pd.Series, cfg: dict) -> TrendResult:
    try:
        ticker = yf.Ticker(symbol)
        df     = ticker.history(period="1y", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 150:
            return TrendResult(symbol=symbol, passed=False, fail_reason="insufficient_data")

        close  = df["Close"]
        volume = df["Volume"]
        price  = float(close.iloc[-1])

        if price < cfg.get("min_price", 10):
            return TrendResult(symbol=symbol, passed=False, price=price, fail_reason="price_too_low")
        avg_vol = float(volume.tail(50).mean())
        if avg_vol < cfg.get("min_avg_volume", 500_000):
            return TrendResult(symbol=symbol, passed=False, price=price, fail_reason="volume_too_low")

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

        # 5. Weekly context
        if cfg.get("weekly_context", True):
            weekly_ok, weekly_note = _check_weekly_context(symbol)
            result.weekly_ok   = weekly_ok
            result.weekly_note = weekly_note
            if not weekly_ok:
                result.fail_reason = weekly_note
                return result

        # 6. Fundamental filter — fetch only for stocks that passed all technical checks
        # Hard-reject only on clearly declining earnings (>10%); unknown data passes through
        eps_g, rev_g, market_cap, short_ratio = _get_fundamentals(ticker)
        result.eps_growth     = eps_g
        result.revenue_growth = rev_g
        result.market_cap     = market_cap
        result.short_ratio    = short_ratio
        # Minervini sweet spot: $200M–$25B (small/mid cap with growth potential)
        # Mega-caps rarely make 20-40% VCP breakout moves
        if market_cap is not None and (
                market_cap < 200_000_000 or market_cap > 25_000_000_000):
            result.fail_reason = f"market_cap_out_of_range(${market_cap/1e9:.1f}B)"
            return result
        if eps_g is not None and eps_g < -0.10:
            result.fail_reason = f"eps_declining({eps_g:.0%})"
            return result

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
                pass
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
            pass
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
        pass
    _log.debug("[screen] %s sector: %s", symbol, sector)
    return sector


def run(symbols: list[str] | None = None, workers: int = 10) -> list[TrendResult]:
    if symbols is None:
        symbols = load_universe()

    _log.info("[screen] Fetching SPY for RS calculation...")
    try:
        spy_close = yf.Ticker("SPY").history(
            period="1y", interval="1d", auto_adjust=True
        )["Close"]
    except Exception:
        spy_close = pd.Series(dtype=float)

    cfg     = TREND_TEMPLATE
    results: list[TrendResult] = []

    _log.info("[screen] Screening %d symbols...", len(symbols))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_symbol, sym, spy_close, cfg): sym
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
