"""
Backtest module with Claude API cost accounting.
Reads trade_journal.jsonl and token_usage.jsonl to simulate net returns
including actual Haiku/Sonnet/Opus API costs per trade.

Usage:
    python3 backtest.py                  # run with all available trades
    python3 backtest.py --iterations 2000
    python3 backtest.py --report-file /path/to/report.md
"""
from __future__ import annotations
import argparse
import json
import logging
import random
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np

_log = logging.getLogger(__name__)

# ── Claude API pricing (USD per 1M tokens, as of 2026) ────────────────────────
_PRICES: dict[str, dict[str, float]] = {
    "haiku":  {"in": 0.80,  "out": 4.00},
    "sonnet": {"in": 3.00,  "out": 15.00},
    "opus":   {"in": 15.00, "out": 75.00},
}

# Map tier names from token_usage.jsonl to price keys
_TIER_MAP = {
    "tier0_news":    "haiku",
    "tier1_haiku":   "haiku",
    "tier2_sonnet":  "sonnet",
    "tier3_opus":    "opus",
}

COMMISSION_PER_TRADE = 1.00  # $1 flat per round-trip (Alpaca)


def _load_trades(log_dir: Path) -> list[dict]:
    """Load completed trades from trade_journal.jsonl."""
    path = log_dir / "trade_journal.jsonl"
    if not path.exists():
        _log.warning("[backtest] trade_journal.jsonl not found at %s", path)
        return []
    trades = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                # Only include completed trades (have pnl_pct)
                if t.get("pnl_pct") is not None and t.get("symbol"):
                    trades.append(t)
            except json.JSONDecodeError:
                continue
    _log.info("[backtest] Loaded %d completed trades", len(trades))
    return trades


def _load_token_costs(log_dir: Path) -> dict[str, float]:
    """
    Load token_usage.jsonl and compute total Claude API cost per symbol.
    Returns {symbol: total_usd}.
    """
    path = log_dir / "token_usage.jsonl"
    if not path.exists():
        _log.warning("[backtest] token_usage.jsonl not found — costs will be estimated")
        return {}
    costs: dict[str, float] = defaultdict(float)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                sym  = rec.get("symbol", "")
                tier = rec.get("tier", "")
                model_key = _TIER_MAP.get(tier, "haiku")
                price = _PRICES[model_key]
                in_tok  = int(rec.get("input_tokens", 0))
                out_tok = int(rec.get("output_tokens", 0))
                usd = (in_tok * price["in"] + out_tok * price["out"]) / 1_000_000
                costs[sym] += usd
            except (json.JSONDecodeError, KeyError):
                continue
    _log.info("[backtest] Token costs loaded for %d symbols", len(costs))
    return dict(costs)


def _estimate_claude_cost(trade: dict) -> float:
    """
    Fallback cost estimate when token_usage.jsonl has no entry for a symbol.
    Assumes: Haiku screen (always) + Sonnet if passed (60% of trades) + Opus (20%).
    Average token counts from production observations.
    """
    # Haiku: ~1200 in / 80 out
    cost = (1200 * _PRICES["haiku"]["in"] + 80 * _PRICES["haiku"]["out"]) / 1_000_000
    # 60% chance Sonnet was called: ~3500 in / 700 out
    cost += 0.60 * (3500 * _PRICES["sonnet"]["in"] + 700 * _PRICES["sonnet"]["out"]) / 1_000_000
    # 20% chance Opus: ~5000 in / 500 out
    cost += 0.20 * (5000 * _PRICES["opus"]["in"] + 500 * _PRICES["opus"]["out"]) / 1_000_000
    return cost


def _trade_net_return(trade: dict, token_costs: dict[str, float]) -> float:
    """
    Net dollar return for a single trade:
    gross_pnl_$ - claude_api_cost - commission
    """
    pnl_pct  = float(trade.get("pnl_pct", 0))
    notional = float(trade.get("notional", trade.get("shares", 0) *
                               trade.get("avg_cost", 100)))
    gross_pnl = pnl_pct * notional

    sym = trade.get("symbol", "")
    claude_cost = token_costs.get(sym, _estimate_claude_cost(trade))

    return gross_pnl - claude_cost - COMMISSION_PER_TRADE


def run_monte_carlo(
    trades: list[dict],
    token_costs: dict[str, float],
    iterations: int = 1000,
    sample_size: Optional[int] = None,
) -> dict:
    """
    Run Monte Carlo simulation by sampling from trade history with replacement.
    Returns statistics dict.
    """
    if not trades:
        return {"error": "no trades"}

    n = sample_size or len(trades)
    net_returns = [_trade_net_return(t, token_costs) for t in trades]
    gross_returns = [float(t.get("pnl_pct", 0)) *
                     float(t.get("notional", 0)) for t in trades]
    claude_costs = [token_costs.get(t.get("symbol", ""),
                    _estimate_claude_cost(t)) for t in trades]

    # Per-trade statistics
    wins = [r for r in net_returns if r > 0]
    losses = [r for r in net_returns if r <= 0]

    # Monte Carlo
    mc_totals = []
    for _ in range(iterations):
        sample = random.choices(net_returns, k=n)
        mc_totals.append(sum(sample))

    mc_arr = np.array(mc_totals)

    return {
        "n_trades":            len(trades),
        "win_rate":            len(wins) / len(net_returns) if net_returns else 0,
        "avg_gross_pnl":       float(np.mean(gross_returns)) if gross_returns else 0,
        "avg_net_return":      float(np.mean(net_returns)),
        "avg_claude_cost":     float(np.mean(claude_costs)),
        "total_claude_cost":   float(sum(claude_costs)),
        "total_gross":         float(sum(gross_returns)),
        "total_net":           float(sum(net_returns)),
        "cost_drag_pct":       (sum(claude_costs) / abs(sum(gross_returns)) * 100
                                if sum(gross_returns) != 0 else 0),
        "mc_median":           float(np.median(mc_arr)),
        "mc_p5":               float(np.percentile(mc_arr, 5)),
        "mc_p25":              float(np.percentile(mc_arr, 25)),
        "mc_p75":              float(np.percentile(mc_arr, 75)),
        "mc_p95":              float(np.percentile(mc_arr, 95)),
        "mc_positive_pct":     float(np.mean(mc_arr > 0) * 100),
        "iterations":          iterations,
    }


def format_report(stats: dict, trades: list[dict],
                  token_costs: dict[str, float]) -> str:
    """Format Monte Carlo results as Obsidian-friendly markdown."""
    today = date.today().isoformat()
    lines = [
        f"---",
        f"updated: {today}",
        f"---",
        f"",
        f"# Three Masters — Backtest-rapport",
        f"",
        f"*Senast uppdaterad: {today} | {stats.get('n_trades', 0)} trades | "
        f"{stats.get('iterations', 0)} MC-iterationer*",
        f"",
        f"---",
        f"",
        f"## Sammanfattning",
        f"",
        f"| Mätvärde | Värde |",
        f"|---------|-------|",
        f"| Antal trades | {stats['n_trades']} |",
        f"| Win rate (netto) | {stats['win_rate']*100:.1f}% |",
        f"| Snitt brutto P&L/trade | ${stats['avg_gross_pnl']:+,.2f} |",
        f"| Snitt Claude-kostnad/trade | ${stats['avg_claude_cost']:.4f} |",
        f"| **Snitt netto P&L/trade** | **${stats['avg_net_return']:+,.2f}** |",
        f"| Total brutto | ${stats['total_gross']:+,.2f} |",
        f"| Total Claude API-kostnad | ${stats['total_claude_cost']:.2f} |",
        f"| **Total netto** | **${stats['total_net']:+,.2f}** |",
        f"| API-kostnad som % av brutto | {stats['cost_drag_pct']:.2f}% |",
        f"",
        f"---",
        f"",
        f"## Monte Carlo ({stats['iterations']} iterationer, n={stats['n_trades']})",
        f"",
        f"| Percentil | Netto total |",
        f"|-----------|-------------|",
        f"| P5 (pessimistiskt) | ${stats['mc_p5']:+,.2f} |",
        f"| P25 | ${stats['mc_p25']:+,.2f} |",
        f"| P50 (median) | ${stats['mc_median']:+,.2f} |",
        f"| P75 | ${stats['mc_p75']:+,.2f} |",
        f"| P95 (optimistiskt) | ${stats['mc_p95']:+,.2f} |",
        f"| Sannolikhet positivt resultat | {stats['mc_positive_pct']:.1f}% |",
        f"",
        f"---",
        f"",
        f"## Kostnad per tier",
        f"",
    ]

    # Per-tier breakdown
    tier_costs: dict[str, float] = defaultdict(float)
    try:
        log_dir = Path(__file__).parent / "logs"
        token_path = log_dir / "token_usage.jsonl"
        if token_path.exists():
            with open(token_path) as tf:
                for line in tf:
                    try:
                        rec = json.loads(line.strip())
                        tier = rec.get("tier", "")
                        mk = _TIER_MAP.get(tier, "haiku")
                        price = _PRICES[mk]
                        in_t = int(rec.get("input_tokens", 0))
                        out_t = int(rec.get("output_tokens", 0))
                        tier_costs[tier] += (in_t * price["in"] + out_t * price["out"]) / 1_000_000
                    except Exception:
                        pass
    except Exception:
        pass

    lines.append("| Tier | Total kostnad |")
    lines.append("|------|--------------|")
    for tier, cost in sorted(tier_costs.items(), key=lambda x: -x[1]):
        lines.append(f"| {tier} | ${cost:.4f} |")
    if not tier_costs:
        lines.append("| (token_usage.jsonl saknas) | — |")

    lines += [
        "",
        "---",
        "",
        f"*Rapporten genereras automatiskt av `backtest.py` — kör `python3 backtest.py` för att uppdatera.*",
    ]
    return "\n".join(lines)


def run(log_dir: Optional[Path] = None, iterations: int = 1000,
        report_file: Optional[Path] = None) -> dict:
    """Main entry point. Returns stats dict."""
    if log_dir is None:
        log_dir = Path(__file__).parent / "logs"

    trades      = _load_trades(log_dir)
    token_costs = _load_token_costs(log_dir)

    if not trades:
        print("No completed trades found in trade_journal.jsonl")
        return {}

    stats  = run_monte_carlo(trades, token_costs, iterations=iterations)
    report = format_report(stats, trades, token_costs)

    # Write to Obsidian vault
    vault_path = Path("/home/habil/obsidian-vault/Three Masters/Backtest-rapport.md")
    if vault_path.parent.exists():
        vault_path.write_text(report)
        print(f"Report written to {vault_path}")
        # Git commit
        try:
            import subprocess
            vault_dir = vault_path.parent.parent
            subprocess.run(["git", "-C", str(vault_dir), "add", str(vault_path)],
                           capture_output=True, timeout=10)
            subprocess.run(["git", "-C", str(vault_dir), "commit", "-m",
                            f"backtest: report {date.today()}"],
                           capture_output=True, timeout=10)
        except Exception as e:
            print(f"Git commit failed: {e}")

    if report_file:
        Path(report_file).write_text(report)

    # Print summary
    print(f"\n=== Backtest Summary ({stats['n_trades']} trades) ===")
    print(f"  Win rate:          {stats['win_rate']*100:.1f}%")
    print(f"  Avg gross/trade:   ${stats['avg_gross_pnl']:+.2f}")
    print(f"  Avg Claude cost:   ${stats['avg_claude_cost']:.4f}")
    print(f"  Avg net/trade:     ${stats['avg_net_return']:+.2f}")
    print(f"  API cost drag:     {stats['cost_drag_pct']:.2f}% of gross")
    print(f"  MC P50 (total):    ${stats['mc_median']:+.2f}")
    print(f"  MC P5–P95:         ${stats['mc_p5']:+.0f} – ${stats['mc_p95']:+.0f}")
    print(f"  Positive outcome:  {stats['mc_positive_pct']:.0f}% of simulations")

    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Three Masters backtest with API cost accounting")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--log-dir", type=str, default=None)
    parser.add_argument("--report-file", type=str, default=None)
    args = parser.parse_args()

    run(
        log_dir=Path(args.log_dir) if args.log_dir else None,
        iterations=args.iterations,
        report_file=Path(args.report_file) if args.report_file else None,
    )
