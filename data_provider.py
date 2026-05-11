"""
data_provider.py — fallback data layer for Three Masters Bot

Provider priority by data type:
  Earnings surprise : FMP → yfinance → Alpha Vantage  (last resort, 25 req/day)
  Earnings calendar : yfinance → FMP
  Fundamentals      : yfinance → FMP → Alpha Vantage  (last resort, 25 req/day)
  Options OI        : yfinance only
  OHLCV single      : yfinance → Massive REST          (unlimited daily bars)
  OHLCV bulk        : yfinance.download → Massive REST parallel (ThreadPool)
  Ticker info       : yfinance → Massive REST → FMP

API limits:
  FMP            ~250 req/day free
  Alpha Vantage   25 req/day free  ← use sparingly
  Massive REST    unlimited on aggs + reference endpoints
  Massive S3      unlimited flat file downloads (boto3/s3fs)
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any

import pandas as pd
import requests

_log = logging.getLogger(__name__)

_FMP_BASE             = "https://financialmodelingprep.com/api/v3"
_AV_BASE              = "https://www.alphavantage.co/query"
_MASSIVE_BASE         = "https://api.massive.com"
_MASSIVE_S3_ENDPOINT  = "https://files.massive.com"
_MASSIVE_BUCKET       = "flatfiles"

_FMP_KEY              = ""
_AV_KEY               = ""
_MASSIVE_KEY          = ""   # REST apiKey (= secret access key)
_MASSIVE_ACCESS_KEY_ID = ""  # S3 access key id
_MASSIVE_SECRET       = ""   # S3 secret access key


def _init() -> None:
    global _FMP_KEY, _AV_KEY, _MASSIVE_KEY
    global _MASSIVE_ACCESS_KEY_ID, _MASSIVE_SECRET
    global _MASSIVE_S3_ENDPOINT, _MASSIVE_BUCKET
    try:
        from config import (FMP_API_KEY, ALPHA_VANTAGE_KEY, MASSIVE_REST_KEY,
                            MASSIVE_ACCESS_KEY_ID, MASSIVE_SECRET_ACCESS_KEY,
                            MASSIVE_S3_ENDPOINT, MASSIVE_BUCKET)
        _FMP_KEY               = FMP_API_KEY
        _AV_KEY                = ALPHA_VANTAGE_KEY
        _MASSIVE_KEY           = MASSIVE_REST_KEY
        _MASSIVE_ACCESS_KEY_ID = MASSIVE_ACCESS_KEY_ID
        _MASSIVE_SECRET        = MASSIVE_SECRET_ACCESS_KEY
        _MASSIVE_S3_ENDPOINT   = MASSIVE_S3_ENDPOINT
        _MASSIVE_BUCKET        = MASSIVE_BUCKET
    except Exception:
        _FMP_KEY               = os.environ.get("FMP_API_KEY", "")
        _AV_KEY                = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        _MASSIVE_KEY           = os.environ.get("MASSIVE_SECRET_ACCESS_KEY", "")
        _MASSIVE_ACCESS_KEY_ID = os.environ.get("MASSIVE_ACCESS_KEY_ID", "")
        _MASSIVE_SECRET        = os.environ.get("MASSIVE_SECRET_ACCESS_KEY", "")
        _MASSIVE_S3_ENDPOINT   = os.environ.get("MASSIVE_S3_ENDPOINT", "https://files.massive.com")
        _MASSIVE_BUCKET        = os.environ.get("MASSIVE_BUCKET", "flatfiles")


_init()


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _fmp(endpoint: str, params: dict | None = None, timeout: int = 8) -> Any:
    if not _FMP_KEY:
        return None
    try:
        r = requests.get(
            f"{_FMP_BASE}/{endpoint}",
            params={"apikey": _FMP_KEY, **(params or {})},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and ("Error Message" in data or "message" in data):
            _log.warning("[fmp] %s: %s", endpoint, data.get("Error Message") or data.get("message"))
            return None
        return data
    except Exception as e:
        _log.debug("[fmp] %s failed: %s", endpoint, e)
        return None


def _av(params: dict, timeout: int = 10) -> Any:
    """Alpha Vantage call. 25 req/day on free tier — use sparingly."""
    if not _AV_KEY:
        return None
    try:
        r = requests.get(
            _AV_BASE,
            params={"apikey": _AV_KEY, **params},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if "Information" in data or "Note" in data:
            _log.warning("[av] rate limit: %s", data.get("Information") or data.get("Note"))
            return None
        return data
    except Exception as e:
        _log.debug("[av] failed: %s", e)
        return None


def _massive_rest(path: str, params: dict | None = None, timeout: int = 10) -> Any:
    """Massive REST API call (Polygon-compatible, unlimited on aggs/reference)."""
    if not _MASSIVE_KEY:
        return None
    try:
        r = requests.get(
            f"{_MASSIVE_BASE}/{path.lstrip('/')}",
            params={"apiKey": _MASSIVE_KEY, **(params or {})},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _log.debug("[massive] %s failed: %s", path, e)
        return None


# ── Earnings surprise ─────────────────────────────────────────────────────────

def get_earnings_surprise(symbol: str) -> list[dict]:
    """
    Return [{date, actual, estimate, surprise_pct}, ...] newest-first.
    Chain: FMP (primary) → yfinance → Alpha Vantage (last resort)
    FMP leads: more reliable structured historical EPS data than yfinance.
    """
    # 1. FMP — primary source for earnings history
    data = _fmp(f"earnings-surprises/{symbol}")
    if data and isinstance(data, list):
        rows = []
        for item in sorted(data, key=lambda x: x.get("date", ""), reverse=True):
            actual   = float(item.get("actualEarningResult", 0) or 0)
            estimate = float(item.get("estimatedEarning",    0) or 0)
            sp = ((actual - estimate) / abs(estimate)) if estimate else 0.0
            rows.append({
                "date":         item.get("date", "")[:10],
                "actual":       actual,
                "estimate":     estimate,
                "surprise_pct": round(sp, 4),
            })
        if rows:
            _log.debug("[data] %s earnings via FMP", symbol)
            return rows

    # 2. yfinance
    try:
        import yfinance as yf
        eh = yf.Ticker(symbol).earnings_history
        if eh is not None and not eh.empty:
            rows = []
            for dt, row in eh.sort_index(ascending=False).iterrows():
                rows.append({
                    "date":         str(dt)[:10],
                    "actual":       float(row.get("epsActual",    0) or 0),
                    "estimate":     float(row.get("epsEstimate",  0) or 0),
                    "surprise_pct": float(row.get("surprisePercent", 0) or 0),
                })
            if rows:
                _log.debug("[data] %s earnings via yfinance", symbol)
                return rows
    except Exception:
        _log.debug("[data] %s yfinance earnings_history failed", symbol)

    # 3. Alpha Vantage (last resort — 25 req/day)
    data = _av({"function": "EARNINGS", "symbol": symbol})
    if data and "quarterlyEarnings" in data:
        rows = []
        for item in data["quarterlyEarnings"]:
            try:
                actual   = float(item.get("reportedEPS",  0) or 0)
                estimate = float(item.get("estimatedEPS", 0) or 0)
                sp_str   = item.get("surprisePercentage", "0") or "0"
                sp       = float(sp_str) / 100.0
                rows.append({
                    "date":         item.get("fiscalDateEnding", "")[:10],
                    "actual":       actual,
                    "estimate":     estimate,
                    "surprise_pct": round(sp, 4),
                })
            except Exception:
                continue
        if rows:
            _log.info("[data] %s earnings via Alpha Vantage (AV req used)", symbol)
            return rows

    return []


def get_latest_surprise(symbol: str) -> tuple[str, float]:
    """(report_date, surprise_pct) for most recent quarter. ('', 0.0) on failure."""
    rows = get_earnings_surprise(symbol)
    return (rows[0]["date"], rows[0]["surprise_pct"]) if rows else ("", 0.0)


# ── Earnings calendar ─────────────────────────────────────────────────────────

def get_days_to_earnings(symbol: str) -> int | None:
    """
    Calendar days until next earnings. None if unknown.
    Chain: yfinance (has future calendar) → FMP
    Alpha Vantage excluded: AV only has historical reported dates, not forward calendar.
    """
    from datetime import datetime
    today = date.today()

    # 1. yfinance — only reliable free source for forward earnings dates
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        if cal and "Earnings Date" in cal:
            ed = cal["Earnings Date"]
            if isinstance(ed, list):
                ed = ed[0]
            days = (ed - today).days
            if days >= 0:
                return days
    except Exception:
        _log.debug("[data] %s yfinance calendar failed", symbol)

    # 2. FMP
    data = _fmp("earning_calendar", params={"symbol": symbol})
    if data and isinstance(data, list):
        for item in data:
            try:
                d    = datetime.strptime(item.get("date", ""), "%Y-%m-%d").date()
                days = (d - today).days
                if days >= 0:
                    return days
            except Exception:
                continue

    return None


# ── Fundamentals ──────────────────────────────────────────────────────────────

def get_fundamentals(symbol: str) -> dict:
    """
    {market_cap, short_ratio, roe, float_shares, inst_pct, eps_growth, rev_growth}
    Chain: yfinance → FMP (gap-fill) → Alpha Vantage (last resort)
    """
    result: dict = {}

    # 1. yfinance — comprehensive single-call fundamentals
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        result = {k: v for k, v in {
            "market_cap":   info.get("marketCap"),
            "short_ratio":  info.get("shortRatio"),
            "roe":          info.get("returnOnEquity"),
            "float_shares": info.get("floatShares"),
            "inst_pct":     info.get("heldPercentInstitutions"),
            "eps_growth":   info.get("earningsGrowth"),
            "rev_growth":   info.get("revenueGrowth"),
        }.items() if v is not None}
    except Exception:
        _log.debug("[data] %s yfinance info failed", symbol)

    # 2. FMP — fill missing fields
    missing = {"market_cap", "roe", "float_shares", "eps_growth", "rev_growth"} - result.keys()
    if missing:
        km = _fmp(f"key-metrics/{symbol}", params={"limit": 1, "period": "quarter"})
        if km and isinstance(km, list):
            m = km[0]
            if "market_cap"   in missing: result["market_cap"]   = m.get("marketCap")
            if "roe"          in missing: result["roe"]          = m.get("roe")
            if "float_shares" in missing: result["float_shares"] = m.get("floatSharesOutstanding")

        missing2 = {"eps_growth", "rev_growth"} - result.keys()
        if missing2:
            prof = _fmp(f"profile/{symbol}")
            if prof and isinstance(prof, list):
                p = prof[0]
                if "eps_growth" in missing2: result["eps_growth"] = p.get("earningsGrowth")
                if "rev_growth" in missing2: result["rev_growth"] = p.get("revenueGrowth")

    # 3. Alpha Vantage — last resort for market_cap / roe
    still_missing = {"market_cap", "roe", "eps_growth"} - {k for k, v in result.items() if v is not None}
    if still_missing:
        ov = _av({"function": "OVERVIEW", "symbol": symbol})
        if ov:
            if "market_cap" in still_missing:
                mc = ov.get("MarketCapitalization")
                if mc:
                    result["market_cap"] = float(mc)
            if "roe" in still_missing:
                roe = ov.get("ReturnOnEquityTTM")
                if roe:
                    result["roe"] = float(roe)
            _log.info("[data] %s fundamentals gap-filled via Alpha Vantage (AV req used)", symbol)

    return result


# ── Options OI ────────────────────────────────────────────────────────────────

def get_atm_options_oi(symbol: str, price: float) -> int | None:
    """
    ATM call OI within 7-45 days. None = data unavailable.
    yfinance only — FMP/AV free tiers don't include options chains.
    Massive options endpoint returns 403 on free plan.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        exps   = ticker.options
        if not exps:
            return 0
        today = pd.Timestamp.now()
        near  = next(
            (e for e in exps if 7 <= (pd.Timestamp(e) - today).days <= 45), None
        )
        if near:
            ch = ticker.option_chain(near).calls
            if not ch.empty:
                idx = (ch["strike"] - price).abs().idxmin()
                return int(ch.loc[idx, "openInterest"] or 0)
    except Exception:
        _log.debug("[data] %s yfinance options failed", symbol)

    return None


# ── OHLCV price history (single symbol) ───────────────────────────────────────

def get_price_history(symbol: str, days: int = 365, interval: str = "1d") -> pd.DataFrame | None:
    """
    Return OHLCV DataFrame (Date index, Open/High/Low/Close/Volume columns).
    Chain: yfinance → Massive REST (unlimited daily bars; no intraday on free plan).
    Returns None on total failure.
    """
    # 1. yfinance — best for single-symbol: handles splits/dividends, all intervals
    try:
        import yfinance as yf
        period = f"{max(days // 365, 1)}y" if days >= 365 else f"{days}d"
        df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
        if df is not None and len(df) >= 5:
            _log.debug("[data] %s OHLCV via yfinance (%d bars)", symbol, len(df))
            return df
    except Exception:
        _log.debug("[data] %s yfinance history failed", symbol)

    # 2. Massive REST (daily only — unlimited on free plan)
    if interval == "1d" and _MASSIVE_KEY:
        try:
            end   = date.today()
            start = end - timedelta(days=days + 10)
            data  = _massive_rest(
                f"v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}",
                params={"adjusted": "true", "sort": "asc", "limit": days + 10},
            )
            results = (data or {}).get("results", [])
            if results:
                df = pd.DataFrame(results)
                df["Date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
                df = df.rename(columns={"o": "Open", "h": "High", "l": "Low",
                                        "c": "Close", "v": "Volume"})
                df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
                _log.info("[data] %s OHLCV via Massive REST (%d bars)", symbol, len(df))
                return df
        except Exception as e:
            _log.debug("[data] %s Massive REST aggs failed: %s", symbol, e)

    return None


# ── OHLCV price history (bulk / screener) ────────────────────────────────────

def _massive_rest_single(symbol: str, days: int) -> pd.DataFrame | None:
    """One Massive REST call for a single symbol. Used in parallel bulk fetches."""
    if not _MASSIVE_KEY:
        return None
    try:
        end   = date.today()
        start = end - timedelta(days=days + 10)
        data  = _massive_rest(
            f"v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}",
            params={"adjusted": "true", "sort": "asc", "limit": days + 10},
            timeout=12,
        )
        results = (data or {}).get("results", [])
        if not results:
            return None
        df = pd.DataFrame(results)
        df["Date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low",
                                 "c": "Close", "v": "Volume"})
        return df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:
        return None


def get_bulk_price_history(
    symbols: list[str], days: int = 365
) -> dict[str, pd.DataFrame]:
    """
    OHLCV for multiple symbols — optimised for screener's 500-stock batch.

    Chain:
      1. yfinance.download  — single batch call, most efficient when working
      2. Massive REST       — parallel ThreadPool (unlimited, each call per-symbol)

    Returns {symbol: DataFrame(Date index, Open/High/Low/Close/Volume)} for
    symbols that have at least 5 bars. Missing symbols are omitted.
    """
    out: dict[str, pd.DataFrame] = {}

    # 1. yfinance.download — one call for all symbols, fast when yf cooperates
    try:
        import yfinance as yf
        period = f"{max(days // 365, 1)}y" if days >= 365 else f"{days}d"
        df_all = yf.download(
            symbols,
            period=period,
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        if df_all is not None and not df_all.empty:
            if isinstance(df_all.columns, pd.MultiIndex):
                for sym in symbols:
                    try:
                        df = df_all.xs(sym, axis=1, level=1).dropna(how="all")
                        if len(df) >= 5:
                            out[sym] = df
                    except Exception:
                        pass
            elif len(symbols) == 1 and len(df_all) >= 5:
                out[symbols[0]] = df_all

            if len(out) >= len(symbols) * 0.9:  # >=90% coverage = success
                _log.info("[data] bulk OHLCV via yfinance.download (%d/%d symbols)",
                          len(out), len(symbols))
                return out
            _log.debug("[data] yfinance.download partial (%d/%d) — topping up via Massive",
                       len(out), len(symbols))
    except Exception as e:
        _log.debug("[data] yfinance.download failed: %s", e)

    # 2. Massive REST in parallel — fill any gaps or replace entire batch
    missing = [s for s in symbols if s not in out]
    if missing and _MASSIVE_KEY:
        _log.info("[data] fetching %d symbols via Massive REST (parallel)", len(missing))
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_massive_rest_single, sym, days): sym for sym in missing}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    df = fut.result()
                    if df is not None and len(df) >= 5:
                        out[sym] = df
                except Exception:
                    pass
        _log.info("[data] bulk OHLCV final: %d/%d symbols", len(out), len(symbols))

    return out


# ── Ticker reference info ─────────────────────────────────────────────────────

def get_ticker_info(symbol: str) -> dict:
    """
    Basic ticker reference: name, market_cap, description, sector.
    Chain: yfinance → Massive REST reference (unlimited) → FMP profile.
    """
    result: dict = {}

    # 1. yfinance
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        result = {k: v for k, v in {
            "name":       info.get("longName") or info.get("shortName"),
            "sector":     info.get("sector"),
            "market_cap": info.get("marketCap"),
        }.items() if v is not None}
        if len(result) >= 2:
            return result
    except Exception:
        _log.debug("[data] %s yfinance info failed", symbol)

    # 2. Massive REST reference (unlimited)
    if _MASSIVE_KEY:
        try:
            data = _massive_rest(f"v3/reference/tickers/{symbol}")
            d = (data or {}).get("results", {})
            if d:
                result.setdefault("name",       d.get("name"))
                result.setdefault("market_cap", d.get("market_cap"))
                result.setdefault("sector",     d.get("sic_description"))
                _log.debug("[data] %s ticker info via Massive REST", symbol)
                return {k: v for k, v in result.items() if v is not None}
        except Exception as e:
            _log.debug("[data] %s Massive REST reference failed: %s", symbol, e)

    # 3. FMP profile
    prof = _fmp(f"profile/{symbol}")
    if prof and isinstance(prof, list):
        p = prof[0]
        result.setdefault("name",       p.get("companyName"))
        result.setdefault("market_cap", p.get("mktCap"))
        result.setdefault("sector",     p.get("sector"))

    return {k: v for k, v in result.items() if v is not None}
