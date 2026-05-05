"""
Layer 1 — SIMONS
Quantitative screening: fetch OHLCV for 500+ stocks, apply Minervini's
Trend Template, return only stocks in a confirmed uptrend.

Trend Template criteria (all must pass):
  1. Price > 150-day MA and 200-day MA
  2. 150-day MA > 200-day MA
  3. 200-day MA trending up for at least 20 trading days
  4. 50-day MA > 150-day MA and 200-day MA
  5. Price > 50-day MA
  6. Price within 25% of 52-week high
  7. Price at least 30% above 52-week low
  8. Relative Strength vs SPY >= 70
"""
from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import yfinance as yf

from config import TREND_TEMPLATE, LOG_DIR

_log = logging.getLogger(__name__)

# ── Universe loading ──────────────────────────────────────────────────────────

def load_universe() -> list[str]:
    """Fetch S&P 500 + Nasdaq 100 + Russell 1000 symbols."""
    symbols = set()
    try:
        # S&P 500 from Wikipedia
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        symbols.update(sp500["Symbol"].str.replace(".", "-", regex=False).tolist())
        _log.info("[screen] S&P 500: %d symbols", len(symbols))
    except Exception as e:
        _log.warning("[screen] S&P 500 fetch failed: %s", e)

    # Fallback core universe
    fallback = [
        "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AVGO","LLY","V",
        "MA","JPM","UNH","PG","HD","JNJ","ABBV","MRK","CRM","NFLX",
        "AMD","INTC","QCOM","ADBE","ORCL","NOW","SNOW","PANW","CRWD","ZS",
        "MELI","SE","SHOP","BABA","JD","PINS","DDOG","MDB","NET","GTLB",
        "CELH","ENPH","FSLR","SEDG","PLUG","RIVN","LCID","NIO","XPEV","LI",
        "SPY","QQQ","IWM",
    ]
    symbols.update(fallback)
    # Remove known bad tickers
    symbols -= {"BRK.B", "BF.B", "CARR", "OTIS"}
    return sorted(symbols)


# ── Relative Strength calculation ─────────────────────────────────────────────

def _rs_rating(close: pd.Series, spy_close: pd.Series) -> float:
    """
    IBD-style Relative Strength Rating 1-99.
    Compares 12-month performance with emphasis on recent quarters.
    """
    try:
        # Use minimum available length
        n = min(len(close), len(spy_close), 252)
        if n < 60:
            return 50.0
        stock_perf = (close.iloc[-1] / close.iloc[-n] - 1)
        spy_perf   = (spy_close.iloc[-1] / spy_close.iloc[-n] - 1)
        # Normalize to 1-99 scale (simplified)
        relative = stock_perf - spy_perf
        rs = 50 + relative * 200  # rough scaling
        return float(np.clip(rs, 1, 99))
    except Exception:
        return 50.0


# ── Per-symbol trend template check ──────────────────────────────────────────

@dataclass
class TrendResult:
    symbol: str
    passed: bool
    price: float = 0.0
    ma50: float = 0.0
    ma150: float = 0.0
    ma200: float = 0.0
    ma200_slope_20d: float = 0.0
    high_52w: float = 0.0
    low_52w: float = 0.0
    pct_from_high: float = 0.0
    pct_from_low: float = 0.0
    rs_rating: float = 0.0
    avg_volume: float = 0.0
    fail_reason: str = ""
    df: pd.DataFrame = field(default=None, repr=False)

    def summary(self) -> str:
        status = "✓" if self.passed else "✗"
        return (f"{status} {self.symbol:<6} ${self.price:.2f}  "
                f"MA50={self.ma50:.0f} MA200={self.ma200:.0f}  "
                f"RS={self.rs_rating:.0f}  {self.pct_from_high:.0%}fromHigh")


def _check_symbol(symbol: str, spy_close: pd.Series, cfg: dict) -> TrendResult:
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1y", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 150:
            return TrendResult(symbol=symbol, passed=False, fail_reason="insufficient_data")

        close  = df["Close"]
        volume = df["Volume"]
        price  = float(close.iloc[-1])

        # Minimum price / volume filters
        if price < cfg.get("min_price", 10):
            return TrendResult(symbol=symbol, passed=False, price=price, fail_reason="price_too_low")
        avg_vol = float(volume.tail(50).mean())
        if avg_vol < cfg.get("min_avg_volume", 500_000):
            return TrendResult(symbol=symbol, passed=False, price=price, fail_reason="volume_too_low")

        ma50  = float(close.rolling(50).mean().iloc[-1])
        ma150 = float(close.rolling(150).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1])

        # 200-MA slope over last 20 days
        ma200_series = close.rolling(200).mean()
        ma200_20d_ago = float(ma200_series.iloc[-20]) if len(ma200_series) >= 220 else ma200
        ma200_slope = (ma200 - ma200_20d_ago) / ma200_20d_ago

        high_52w = float(close.tail(252).max())
        low_52w  = float(close.tail(252).min())
        pct_from_high = (price - high_52w) / high_52w    # negative = below high
        pct_from_low  = (price - low_52w) / low_52w      # positive = above low

        rs = _rs_rating(close, spy_close)

        result = TrendResult(
            symbol=symbol, price=price,
            ma50=ma50, ma150=ma150, ma200=ma200,
            ma200_slope_20d=ma200_slope,
            high_52w=high_52w, low_52w=low_52w,
            pct_from_high=pct_from_high, pct_from_low=pct_from_low,
            rs_rating=rs, avg_volume=avg_vol, passed=False,
            df=df,
        )

        # ── Apply Trend Template ──────────────────────────────────────────
        checks = [
            (price > ma150 and price > ma200,             "price_below_ma150/ma200"),
            (ma150 > ma200,                                "ma150_below_ma200"),
            (ma200_slope > 0,                              "ma200_not_trending_up"),
            (ma50 > ma150 and ma50 > ma200,                "ma50_below_ma150/ma200"),
            (price > ma50,                                 "price_below_ma50"),
            (pct_from_high >= -cfg.get("within_pct_of_52w_high", 0.25),
                                                           "too_far_from_52w_high"),
            (pct_from_low >= cfg.get("above_pct_of_52w_low", 0.30),
                                                           "too_close_to_52w_low"),
            (rs >= cfg.get("rs_min", 70),                 "rs_too_low"),
        ]

        for condition, reason in checks:
            if not condition:
                result.fail_reason = reason
                return result

        result.passed = True
        return result

    except Exception as e:
        return TrendResult(symbol=symbol, passed=False, fail_reason=f"error:{e}")


def run(symbols: list[str] | None = None, workers: int = 12) -> list[TrendResult]:
    """
    Run Trend Template screening on the universe.
    Returns list of TrendResult objects (passed ones first).
    """
    if symbols is None:
        symbols = load_universe()

    _log.info("[screen] Fetching SPY for RS calculation...")
    try:
        spy_df    = yf.Ticker("SPY").history(period="1y", interval="1d", auto_adjust=True)
        spy_close = spy_df["Close"]
    except Exception:
        spy_close = pd.Series(dtype=float)

    cfg = TREND_TEMPLATE
    results: list[TrendResult] = []
    passed  = 0
    failed  = 0

    _log.info("[screen] Screening %d symbols with Trend Template...", len(symbols))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_symbol, sym, spy_close, cfg): sym for sym in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            try:
                r = fut.result()
                results.append(r)
                if r.passed:
                    passed += 1
                    _log.debug("[screen] PASS %s", r.summary())
                else:
                    failed += 1
            except Exception as e:
                _log.warning("[screen] %s: %s", sym, e)
                failed += 1

            if i % 50 == 0:
                _log.info("[screen] %d/%d done (%d passed so far)", i, len(symbols), passed)

    results.sort(key=lambda r: (-int(r.passed), -r.rs_rating))
    passed_list = [r for r in results if r.passed]
    _log.info("[screen] Done: %d/%d passed Trend Template", len(passed_list), len(results))
    return results
