#!/usr/bin/env python3
"""
Three Masters Bot — Monte Carlo Simulering

Slumpar ordningen på avslutade backtest-trades 1000× och beräknar distribution av:
  - Slutkapital (startkapital = $100k, 2% risk per trade)
  - Max drawdown per simulering
  - Sharpe-ratio (riskfri ränta = 4%)

Användning:
    python monte_carlo.py                             # läser logs/backtest_results.json
    python monte_carlo.py --file logs/custom.json    # alternativ fil
    python monte_carlo.py --n 5000                   # fler iterationer
    python monte_carlo.py --risk 0.015               # 1.5% risk per trade
"""
from __future__ import annotations
import argparse
import json
import random
from pathlib import Path

import numpy as np

from config import LOG_DIR

_RISK_FREE_RATE = 0.04   # 4% annual riskfri ränta för Sharpe


def _max_drawdown(equity: list[float]) -> float:
    """Beräknar max drawdown som procentandel av peak."""
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe(returns: list[float], risk_free_annual: float = _RISK_FREE_RATE) -> float:
    """Sharpe-ratio (annualiserad) från trade-returns."""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    avg = arr.mean()
    std = arr.std(ddof=1)
    if std == 0:
        return 0.0
    # Antagande: ~252 handelsdagar/år, varje trade ≈ 1 observation
    return float((avg - risk_free_annual / 252) / std * np.sqrt(252))


def run_monte_carlo(
    trades: list[dict],
    n_sims: int = 1000,
    start_capital: float = 100_000.0,
    risk_per_trade: float = 0.02,
) -> dict:
    """
    Kör n_sims Monte Carlo-iterationer.
    Varje iteration: blanda trade-ordningen, simulera equity-kurva med fast risk%.

    Returnerar dict med percentil-stats för slutkapital, drawdown och Sharpe.
    """
    if not trades:
        return {}

    pnl_pcts = [t["pnl_pct"] for t in trades]   # redan beräknade som decimaler

    final_caps: list[float]  = []
    max_dds:    list[float]  = []
    sharpes:    list[float]  = []

    for _ in range(n_sims):
        shuffled  = random.sample(pnl_pcts, len(pnl_pcts))
        capital   = start_capital
        equity    = [capital]
        trade_rets: list[float] = []

        for r in shuffled:
            # Risk-justerad trade: risk_per_trade% av aktuellt kapital
            risk_amt  = capital * risk_per_trade
            # Normalisera pnl_pct mot 7% stop (backtest-stop) → R-multiple
            r_multiple = r / 0.07
            trade_gain = risk_amt * r_multiple
            capital   += trade_gain
            capital    = max(capital, 0.0)   # ruinerat = 0
            equity.append(capital)
            trade_rets.append(r)

        final_caps.append(capital)
        max_dds.append(_max_drawdown(equity))
        sharpes.append(_sharpe(trade_rets))

    def _pct(arr: list[float], p: int) -> float:
        return round(float(np.percentile(arr, p)), 2)

    return {
        "n_sims":        n_sims,
        "n_trades":      len(trades),
        "start_capital": start_capital,
        "risk_per_trade": risk_per_trade,
        "final_capital": {
            "p5":    _pct(final_caps, 5),
            "p25":   _pct(final_caps, 25),
            "median": _pct(final_caps, 50),
            "p75":   _pct(final_caps, 75),
            "p95":   _pct(final_caps, 95),
            "mean":  round(float(np.mean(final_caps)), 2),
        },
        "max_drawdown_pct": {
            "p5":    round(_pct(max_dds, 5) * 100, 1),
            "p50":   round(_pct(max_dds, 50) * 100, 1),
            "p95":   round(_pct(max_dds, 95) * 100, 1),
        },
        "sharpe": {
            "p5":    _pct(sharpes, 5),
            "p50":   _pct(sharpes, 50),
            "p95":   _pct(sharpes, 95),
            "mean":  round(float(np.mean(sharpes)), 2),
        },
        "ruin_pct": round(sum(1 for c in final_caps if c <= 0) / n_sims * 100, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Three Masters Monte Carlo")
    parser.add_argument("--file",   default=str(LOG_DIR / "backtest_results.json"),
                        help="Sökväg till backtest_results.json")
    parser.add_argument("--n",      type=int,   default=1000, help="Antal simuleringar")
    parser.add_argument("--risk",   type=float, default=0.02, help="Risk per trade (0.02 = 2%%)")
    parser.add_argument("--capital", type=float, default=100_000.0, help="Startkapital")
    args = parser.parse_args()

    data_path = Path(args.file)
    if not data_path.exists():
        print(f"Filen {data_path} hittades inte. Kör backtest.py först.")
        return

    with open(data_path) as f:
        bt = json.load(f)

    trades = bt.get("trades", [])
    if not trades:
        print("Inga trades i filen — kör backtest.py --min-score 0 för att generera data.")
        return

    print(f"Monte Carlo: {len(trades)} trades × {args.n} iterationer "
          f"| startkapital ${args.capital:,.0f} | risk {args.risk:.1%}/trade")

    res = run_monte_carlo(trades, n_sims=args.n,
                          start_capital=args.capital, risk_per_trade=args.risk)
    fc  = res["final_capital"]
    dd  = res["max_drawdown_pct"]
    sh  = res["sharpe"]

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monte Carlo Resultat  ({res['n_sims']} simuleringar)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Slutkapital (startade ${res['start_capital']:,.0f}):
  5:e  percentil:  ${fc['p5']:>12,.0f}
  25:e percentil:  ${fc['p25']:>12,.0f}
  Median:          ${fc['median']:>12,.0f}
  75:e percentil:  ${fc['p75']:>12,.0f}
  95:e percentil:  ${fc['p95']:>12,.0f}
  Medelvärde:      ${fc['mean']:>12,.0f}

Max Drawdown:
  Median:          {dd['p50']:>5.1f}%
  Pessimistisk (95:e): {dd['p95']:>5.1f}%
  Optimistisk (5:e):   {dd['p5']:>5.1f}%

Sharpe-ratio (annualiserad):
  Median:          {sh['p50']:>6.2f}
  95:e percentil:  {sh['p95']:>6.2f}
  Medelvärde:      {sh['mean']:>6.2f}

Ruinrisk (kapital → 0): {res['ruin_pct']:.1f}%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""")


if __name__ == "__main__":
    main()
