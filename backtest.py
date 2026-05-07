#!/usr/bin/env python3
"""
Three Masters Bot — Simple Backtest Module
Replays the Simons trend-template screener against historical daily bars
and simulates Minervini-style entries/exits (breakout buy-stop, 7% hard stop,
20% profit target or 20 trading-day time-stop).

Usage:
    python backtest.py [--symbols AAPL,MSFT,...] [--years 2] [--start 2022-01-01]
"""
from __future__ import annotations
import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from config import LOG_DIR
from screener import load_universe

_log = logging.getLogger(__name__)
BASE = Path(__file__).parent

# ── Default parameters ────────────────────────────────────────────────────────
STOP_PCT       = 0.07    # hard stop: -7% from entry
TARGET_PCT     = 0.20    # profit target: +20%
TIME_STOP_DAYS = 20      # exit if flat after 20 trading days


def _fetch(symbol: str, start: str, end: str) -> pd.DataFrame | None:
    try:
        df = yf.download(symbol, start=start, end=end, interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or len(df) < 50:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return df
    except Exception as e:
        _log.debug("fetch %s: %s", symbol, e)
        return None


def _trend_template(df: pd.DataFrame) -> bool:
    """Simplified Simons Trend Template: price > MA50 > MA150 > MA200, near 52w high."""
    if len(df) < 200:
        return False
    close  = df["Close"]
    ma50   = close.rolling(50).mean().iloc[-1]
    ma150  = close.rolling(150).mean().iloc[-1]
    ma200  = close.rolling(200).mean().iloc[-1]
    price  = float(close.iloc[-1])
    hi52   = float(close.rolling(252).max().iloc[-1])
    lo52   = float(close.rolling(252).min().iloc[-1])
    return (
        price > ma50 > ma150 > ma200          # trend alignment
        and price >= hi52 * 0.75              # within 25% of 52w high
        and price >= lo52 * 1.30              # at least 30% above 52w low
    )


def _sim_trade(df_fwd: pd.DataFrame, entry_price: float) -> dict:
    """Simulate one trade from entry bar forward. Returns result dict."""
    stop   = entry_price * (1 - STOP_PCT)
    target = entry_price * (1 + TARGET_PCT)
    for i, (ts, row) in enumerate(df_fwd.iterrows()):
        lo  = float(row["Low"])
        hi  = float(row["High"])
        cls = float(row["Close"])
        # Stop hit
        if lo <= stop:
            exit_p = stop
            return {"exit": str(ts.date()), "exit_price": exit_p,
                    "pnl_pct": (exit_p - entry_price) / entry_price,
                    "days": i + 1, "reason": "stop"}
        # Target hit
        if hi >= target:
            exit_p = target
            return {"exit": str(ts.date()), "exit_price": exit_p,
                    "pnl_pct": (exit_p - entry_price) / entry_price,
                    "days": i + 1, "reason": "target"}
        # Time stop
        if i + 1 >= TIME_STOP_DAYS:
            return {"exit": str(ts.date()), "exit_price": cls,
                    "pnl_pct": (cls - entry_price) / entry_price,
                    "days": i + 1, "reason": "time_stop"}
    # End of data
    exit_p = float(df_fwd["Close"].iloc[-1])
    return {"exit": str(df_fwd.index[-1].date()), "exit_price": exit_p,
            "pnl_pct": (exit_p - entry_price) / entry_price,
            "days": len(df_fwd), "reason": "data_end"}


def run_backtest(symbols: list[str], start: str, end: str) -> dict:
    """
    For each symbol and each scan date (weekly on Mondays), check Trend Template.
    If passed, simulate a next-day open entry with hard stop / target / time-stop.
    Returns aggregated results dict.
    """
    trades: list[dict] = []
    scanned = 0
    signals = 0

    scan_dates = pd.bdate_range(start=start, end=end, freq="W-MON")

    for sym in symbols:
        df = _fetch(sym, start, end)
        if df is None:
            continue
        df.index = pd.to_datetime(df.index).tz_localize(None)

        for scan_dt in scan_dates:
            scan_dt = pd.Timestamp(scan_dt).tz_localize(None)
            hist = df[df.index <= scan_dt]
            if len(hist) < 200:
                continue
            scanned += 1

            if not _trend_template(hist):
                continue
            signals += 1

            # Entry: next trading day open
            fwd = df[df.index > scan_dt]
            if len(fwd) < 2:
                continue
            entry_price = float(fwd.iloc[0]["Open"])
            if entry_price <= 0:
                continue

            result = _sim_trade(fwd.iloc[1:], entry_price)
            result.update({
                "symbol":       sym,
                "entry":        str(scan_dt.date()),
                "entry_price":  round(entry_price, 2),
            })
            trades.append(result)

    if not trades:
        return {"trades": [], "stats": {}}

    df_t = pd.DataFrame(trades)
    wins  = df_t[df_t["pnl_pct"] > 0]
    stats = {
        "total_trades":    len(df_t),
        "win_rate":        round(len(wins) / len(df_t), 3),
        "avg_pnl_pct":     round(df_t["pnl_pct"].mean() * 100, 2),
        "avg_win_pct":     round(wins["pnl_pct"].mean() * 100, 2) if len(wins) else 0,
        "avg_loss_pct":    round(df_t[df_t["pnl_pct"] < 0]["pnl_pct"].mean() * 100, 2) if len(df_t[df_t["pnl_pct"] < 0]) else 0,
        "expectancy_pct":  round(df_t["pnl_pct"].mean() * 100, 2),
        "max_dd_pct":      round(df_t["pnl_pct"].min() * 100, 2),
        "signals_found":   signals,
        "scan_checks":     scanned,
        "signal_rate":     round(signals / max(scanned, 1), 3),
    }
    return {"trades": trades, "stats": stats}


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Three Masters Backtest")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols (default: load universe)")
    parser.add_argument("--years",   type=float, default=2.0, help="Years of history to test")
    parser.add_argument("--start",   default="",  help="Override start date YYYY-MM-DD")
    parser.add_argument("--out",     default="",  help="Save results to JSON file")
    args = parser.parse_args()

    end   = datetime.today().strftime("%Y-%m-%d")
    start = args.start or (datetime.today() - timedelta(days=int(args.years * 365))).strftime("%Y-%m-%d")

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        print("Loading universe...")
        symbols = load_universe()

    print(f"Backtest: {len(symbols)} symbols | {start} → {end}")
    results = run_backtest(symbols, start, end)
    stats   = results.get("stats", {})

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Three Masters Backtest Results
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Period:        {start} → {end}
Symbols:       {len(symbols)}
Scans:         {stats.get('scan_checks', 0)}
Signals:       {stats.get('signals_found', 0)} ({stats.get('signal_rate', 0):.1%} hit rate)
Trades taken:  {stats.get('total_trades', 0)}
Win rate:      {stats.get('win_rate', 0):.1%}
Avg P&L:       {stats.get('avg_pnl_pct', 0):+.2f}%
Avg win:       {stats.get('avg_win_pct', 0):+.2f}%
Avg loss:      {stats.get('avg_loss_pct', 0):+.2f}%
Max single loss: {stats.get('max_dd_pct', 0):+.2f}%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""")

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(results, indent=2))
        print(f"Results saved to {out_path}")
    else:
        out_path = LOG_DIR / "backtest_results.json"
        out_path.write_text(json.dumps(results, indent=2))
        print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
