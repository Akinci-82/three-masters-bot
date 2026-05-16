#!/usr/bin/env python3
"""
Three Masters Bot — Backtest Module
Replays the full strategy pipeline against historical daily bars:
  - Minervini Trend Template  (quantitative screener)
  - Tier-0 quantitative VCP   (contraction + volume dry-up, no Claude)
  - Three-stage exit rules    (mirrors actual bot: breakeven, partial exits, trailing stop)
  - Time stop                 (15 trading days, <2% gain)

Usage:
    python backtest.py [--symbols AAPL,MSFT,...] [--years 2] [--start 2022-01-01]
    python backtest.py --min-score 6.0  # filter by quant composite >= threshold
"""
from __future__ import annotations
import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from config import LOG_DIR, TREND_TEMPLATE, VCP
from screener import load_universe

_log = logging.getLogger(__name__)
BASE = Path(__file__).parent

# ── Exit parameters (mirrors actual bot config) ──────────────────────────────
INITIAL_STOP_PCT   = 0.07   # 7% trailing stop at entry
BREAKEVEN_AT       = 0.08   # move stop to entry cost at +8%
PARTIAL1_AT        = 0.10   # sell 33% at +10%
PARTIAL2_AT        = 0.20   # sell 33% at +20% (measured move proxy)
TRAIL_TIGHT_PCT    = 0.05   # tighten trailing stop to 5% after first partial
TIME_STOP_DAYS     = 15     # close if held ≥15 trading days with <2% gain
TIME_STOP_MIN_GAIN = 0.02


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


def _trend_template(df: pd.DataFrame, cfg: dict | None = None) -> bool:
    cfg = cfg or TREND_TEMPLATE
    if len(df) < 200:
        return False
    close  = df["Close"]
    price  = float(close.iloc[-1])
    ma50   = float(close.rolling(50).mean().iloc[-1])
    ma150  = float(close.rolling(150).mean().iloc[-1])
    ma200  = float(close.rolling(200).mean().iloc[-1])
    hi52   = float(close.rolling(252).max().iloc[-1])
    lo52   = float(close.rolling(252).min().iloc[-1])
    ma200_20ago = float(close.rolling(200).mean().iloc[-21]) if len(close) >= 220 else ma200
    return (
        price > ma50 > ma150 > ma200
        and ma200 > ma200_20ago                           # MA200 slope rising
        and price >= hi52 * (1 - cfg.get("pct_from_high", 0.25))
        and price >= lo52 * 1.30
        and price >= cfg.get("min_price", 10)
    )


def _quant_vcp_score(df: pd.DataFrame) -> tuple[bool, float, float, float]:
    """
    Simplified Tier-0 VCP check (no Claude). Returns (passed, score, breakout_lvl, stop_lvl).
    Score 0-10: measures contraction quality + volume dry-up.
    """
    try:
        from vcp_analyzer import _quantitative_vcp_check
        ok, quant = _quantitative_vcp_check(df, VCP)
        if not ok:
            return False, 0.0, 0.0, 0.0
        score = 0.0
        # Contractions (max 3 pts)
        nc = int(quant.get("contractions", 0))
        score += min(nc, 4) * 0.75
        # Volume dry-up (2 pts)
        if quant.get("vol_at_multiweek_low", False):
            score += 2.0
        # Tight final handle (2 pts)
        tight = float(quant.get("tight_rng_pct", 1.0))
        score += 2.0 if tight < 0.05 else (1.0 if tight < 0.08 else 0.0)
        # Pattern depth (1 pt for moderate correction)
        depth = float(quant.get("pattern_depth_pct", 0.5))
        score += 1.0 if 0.10 <= depth <= 0.35 else 0.5 if depth <= 0.50 else 0.0
        # Breakout volume (2 pts)
        if quant.get("breakout_volume", False):
            score += 2.0
        bl  = float(quant.get("breakout_level", 0.0))
        sl  = float(quant.get("stop_loss_candidate", 0.0))
        return True, round(min(score, 10.0), 2), bl, sl
    except Exception as e:
        _log.debug("quant_vcp error: %s", e)
        return False, 0.0, 0.0, 0.0


def _weekly_close_under_ma10w(df_fwd: pd.DataFrame, day_idx: int) -> bool:
    """True if the most recently completed weekly bar closed below its 10-week MA.
    Resamples daily bars to weekly to mirror position_monitor Step W logic.
    """
    try:
        hist_slice = df_fwd.iloc[: day_idx + 1]
        if len(hist_slice) < 15:   # need at least ~3 weeks to form a meaningful MA
            return False
        weekly = hist_slice["Close"].resample("W-FRI").last().dropna()
        if len(weekly) < 12:
            return False
        last_completed = float(weekly.iloc[-2])   # -1 may be in-progress week
        ma10w = float(weekly.iloc[-11:-1].mean())
        return last_completed < ma10w
    except Exception:
        return False


def _sim_trade(df_fwd: pd.DataFrame, entry_price: float) -> dict:
    """
    Simulate one trade with actual bot exit rules:
      - 7% trailing stop, breakeven at +8%
      - Early pyramid (F): add 25% at +4% if first 5 days (simple proxy for RS check)
      - Sell 33% at +10% (B1), 33% at +20% (B2)
      - Pyramid (P): add 30% at +12-20% after B1
      - Tighten trailing to 5% after first partial
      - Step W: exit if weekly close falls under MA10w
      - Time stop: 15 days and <2% gain
    Returns dict with outcome details.
    """
    stop_trail    = entry_price * (1 - INITIAL_STOP_PCT)
    peak_price    = entry_price
    partial1_done = False
    partial2_done = False
    breakeven_set = False
    pyramid_done  = False
    step_f_done   = False
    shares        = 1.0          # normalised to 1 share
    proceeds      = 0.0
    trail_pct     = INITIAL_STOP_PCT
    exit_step     = "stop"

    # Resample to weekly for Step W check (done once, not per-bar)
    _weekly_dates = set()
    try:
        _wk = df_fwd["Close"].resample("W-FRI").last().dropna()
        _weekly_dates = set(_wk.index.normalize())
    except Exception:
        pass

    for i, (ts, row) in enumerate(df_fwd.iterrows()):
        lo  = float(row["Low"])
        hi  = float(row["High"])
        cls = float(row["Close"])

        # Update peak + trailing stop
        if hi > peak_price:
            peak_price = hi
        new_trail = peak_price * (1 - trail_pct)
        if new_trail > stop_trail:
            stop_trail = new_trail

        # Breakeven: move stop to entry at +8%
        if not breakeven_set and cls >= entry_price * (1 + BREAKEVEN_AT):
            stop_trail = max(stop_trail, entry_price)
            breakeven_set = True

        # Step F: early pyramid +25% at +4% (simple proxy: no RS check in backtest)
        pnl_now = (cls - entry_price) / entry_price
        if not step_f_done and not partial1_done and 0.04 <= pnl_now < 0.10:
            shares       += 0.25
            step_f_done   = True

        # Partial exit 1 (B1): sell 33% at +10%
        if not partial1_done and hi >= entry_price * (1 + PARTIAL1_AT):
            exit1          = entry_price * (1 + PARTIAL1_AT)
            sell_qty       = shares * 0.33 / shares   # normalised: always 1/3 of current
            proceeds      += shares * 0.33 * exit1
            shares        -= shares * 0.33
            trail_pct      = TRAIL_TIGHT_PCT
            partial1_done  = True

        # Pyramid (P): add 30% of original qty between +12-20% after B1
        if (partial1_done and not pyramid_done and not partial2_done
                and entry_price * 1.12 <= cls <= entry_price * 1.20):
            shares       += 0.30
            pyramid_done  = True

        # Partial exit 2 (B2): sell 33% at +20%
        if partial1_done and not partial2_done and hi >= entry_price * (1 + PARTIAL2_AT):
            exit2          = entry_price * (1 + PARTIAL2_AT)
            proceeds      += shares * 0.33 * exit2
            shares        -= shares * 0.33
            partial2_done  = True

        # Step W: weekly close under MA10w — check on each Monday (start of new week)
        _ts_norm = pd.Timestamp(ts).normalize()
        if (_ts_norm.weekday() == 0       # Monday = start of new completed week
                and not partial2_done
                and _weekly_close_under_ma10w(df_fwd, i)):
            proceeds  += shares * cls
            total_pnl  = (proceeds - entry_price) / entry_price
            return {"exit": str(ts.date()), "exit_price": cls,
                    "pnl_pct": round(total_pnl, 4), "days": i + 1,
                    "reason": "W_weekly_close",
                    "partials": int(partial1_done) + int(partial2_done)}

        # Stop hit
        if lo <= stop_trail:
            proceeds += shares * stop_trail
            total_pnl = (proceeds - entry_price) / entry_price
            return {"exit": str(ts.date()), "exit_price": stop_trail,
                    "pnl_pct": round(total_pnl, 4), "days": i + 1,
                    "reason": "stop",
                    "partials": int(partial1_done) + int(partial2_done)}

        # Time stop: ≥15 days and gain < 2%
        if i + 1 >= TIME_STOP_DAYS and not partial1_done:
            paper_gain = (cls - entry_price) / entry_price
            if paper_gain < TIME_STOP_MIN_GAIN:
                proceeds += shares * cls
                total_pnl = (proceeds - entry_price) / entry_price
                return {"exit": str(ts.date()), "exit_price": cls,
                        "pnl_pct": round(total_pnl, 4), "days": i + 1,
                        "reason": "time_stop",
                        "partials": int(partial1_done) + int(partial2_done)}

    # End of data — close at last close
    final = float(df_fwd["Close"].iloc[-1]) if len(df_fwd) > 0 else entry_price
    proceeds += shares * final
    total_pnl = (proceeds - entry_price) / entry_price
    return {"exit": str(df_fwd.index[-1].date()), "exit_price": final,
            "pnl_pct": round(total_pnl, 4), "days": len(df_fwd),
            "reason": "data_end",
            "partials": int(partial1_done) + int(partial2_done)}


def run_backtest(symbols: list[str], start: str, end: str,
                 min_score: float = 0.0, scan_freq: str = "W-MON") -> dict:
    """
    For each symbol and each scan date, apply Trend Template + quant VCP.
    If score >= min_score, simulate entry with actual bot exit rules.
    """
    trades: list[dict] = []
    scanned = 0
    signals = 0

    scan_dates = pd.bdate_range(start=start, end=end, freq=scan_freq)

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

            passed_vcp, score, bl, sl = _quant_vcp_score(hist)
            if not passed_vcp or score < min_score:
                continue

            signals += 1
            fwd = df[df.index > scan_dt]
            if len(fwd) < 2:
                continue

            # Entry: buy-stop at breakout level or next-day open if already above.
            # Lookahead guard: if bl > open, the stop order only fills when high >= bl.
            # Without this check we'd simulate a fill that never happened.
            _fwd0_open = float(fwd.iloc[0]["Open"])
            _fwd0_high = float(fwd.iloc[0]["High"])
            entry_candidate = bl if bl > 0 else _fwd0_open
            entry_price = max(_fwd0_open, entry_candidate)
            if entry_price <= 0:
                continue
            if entry_price > _fwd0_open and _fwd0_high < entry_price:
                continue  # buy-stop never triggered on entry day

            result = _sim_trade(fwd.iloc[1:], entry_price)
            result.update({
                "symbol":       sym,
                "entry":        str(scan_dt.date()),
                "entry_price":  round(entry_price, 2),
                "quant_score":  score,
                "breakout_lvl": round(bl, 2),
                "stop_lvl":     round(sl, 2),
            })
            trades.append(result)

    if not trades:
        return {"trades": [], "stats": {}, "score_buckets": {}}

    df_t   = pd.DataFrame(trades)
    wins   = df_t[df_t["pnl_pct"] > 0]
    losses = df_t[df_t["pnl_pct"] <= 0]

    # Score bucket breakdown
    buckets = {"0-4": [], "4-6": [], "6-8": [], "8-10": []}
    for _, row in df_t.iterrows():
        s = row["quant_score"]
        r = row["pnl_pct"] / max(abs(row.get("stop_lvl", row["entry_price"] * 0.07) /
                                      row["entry_price"]), 0.01)
        bkt = ("8-10" if s >= 8 else "6-8" if s >= 6 else "4-6" if s >= 4 else "0-4")
        buckets[bkt].append(r)

    bucket_stats = {}
    for bkt, rs in buckets.items():
        if rs:
            wins_b = [r for r in rs if r > 0]
            bucket_stats[bkt] = {
                "count": len(rs),
                "win_rate": round(len(wins_b) / len(rs), 3),
                "avg_r": round(sum(rs) / len(rs), 3),
            }

    # Exit reason breakdown
    exit_reasons = df_t["reason"].value_counts().to_dict()

    stats = {
        "total_trades":    len(df_t),
        "win_rate":        round(len(wins) / len(df_t), 3),
        "avg_pnl_pct":     round(df_t["pnl_pct"].mean() * 100, 2),
        "avg_win_pct":     round(wins["pnl_pct"].mean() * 100, 2) if len(wins) else 0,
        "avg_loss_pct":    round(losses["pnl_pct"].mean() * 100, 2) if len(losses) else 0,
        "avg_hold_days":   round(df_t["days"].mean(), 1),
        "max_dd_pct":      round(df_t["pnl_pct"].min() * 100, 2),
        "signals_found":   signals,
        "scan_checks":     scanned,
        "signal_rate":     round(signals / max(scanned, 1), 4),
        "exit_reasons":    exit_reasons,
    }
    return {"trades": trades, "stats": stats, "score_buckets": bucket_stats}


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Three Masters Backtest")
    parser.add_argument("--symbols",   default="", help="Comma-separated (default: universe)")
    parser.add_argument("--years",     type=float, default=2.0)
    parser.add_argument("--start",     default="")
    parser.add_argument("--min-score", type=float, default=0.0,
                        help="Minimum quant VCP score (0-10, default: no filter)")
    parser.add_argument("--out",       default="")
    args = parser.parse_args()

    end   = datetime.today().strftime("%Y-%m-%d")
    start = args.start or (datetime.today() - timedelta(days=int(args.years * 365))).strftime("%Y-%m-%d")

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        print("Loading universe...")
        symbols = load_universe()

    print(f"Backtest: {len(symbols)} symbols | {start} → {end} | min_score={args.min_score}")
    results = run_backtest(symbols, start, end, min_score=args.min_score)
    stats   = results.get("stats", {})
    buckets = results.get("score_buckets", {})

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Three Masters Backtest  (Trend Template + Quant VCP)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Period:          {start} → {end}
Symbols:         {len(symbols)}
Scans:           {stats.get('scan_checks', 0)}
Signals:         {stats.get('signals_found', 0)} ({stats.get('signal_rate', 0):.2%} hit rate)
Trades taken:    {stats.get('total_trades', 0)}
Win rate:        {stats.get('win_rate', 0):.1%}
Avg P&L:         {stats.get('avg_pnl_pct', 0):+.2f}%
Avg win:         {stats.get('avg_win_pct', 0):+.2f}%
Avg loss:        {stats.get('avg_loss_pct', 0):+.2f}%
Avg hold:        {stats.get('avg_hold_days', 0):.1f} days
Max single loss: {stats.get('max_dd_pct', 0):+.2f}%
Exit reasons:    {stats.get('exit_reasons', {})}

Score buckets (quant VCP score):""")
    for bkt in sorted(buckets.keys()):
        b = buckets[bkt]
        print(f"  [{bkt}]  n={b['count']}  WR={b['win_rate']:.0%}  avg_R={b['avg_r']:+.2f}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    out_path = Path(args.out) if args.out else LOG_DIR / "backtest_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
