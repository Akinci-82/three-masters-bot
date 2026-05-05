"""
Layer 2 — MINERVINI
VCP (Volatility Contraction Pattern) analysis.

Step 1: Quantitative pre-filter — identify stocks that LOOK like a VCP
Step 2: Send price data to Claude AI for pattern confirmation,
        breakout level, and stop-loss identification.

VCP characteristics:
  - Stock is in a confirmed uptrend (passed Trend Template)
  - Price consolidates in a series of contracting swings
  - Each contraction has a narrower range (pivot-to-pivot)
  - Volume diminishes on each contraction (drying up = institutions holding)
  - Final pivot is very tight (< 5% range)
  - Breakout on volume surge (>= 1.5x average)
"""
from __future__ import annotations
import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Optional

import anthropic
import numpy as np
import pandas as pd

from config import VCP, CLAUDE_MODEL, CLAUDE_MODEL_DEEP, ANTHROPIC_API_KEY, CHART_DIR

_log = logging.getLogger(__name__)
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ── VCP quantitative pre-filter ───────────────────────────────────────────────

@dataclass
class VCPResult:
    symbol: str
    passed: bool
    confidence: float = 0.0          # 0-1 from Claude
    breakout_level: float = 0.0      # pivot high / entry price
    stop_loss: float = 0.0           # pivot low
    pattern_depth_pct: float = 0.0   # depth of consolidation from pivot high
    contractions: int = 0
    tight_pct: float = 0.0           # final contraction range %
    current_price: float = 0.0
    ai_verdict: str = ""
    ai_reasoning: str = ""
    fail_reason: str = ""
    breakout_volume: bool = False     # today's volume >= 1.5x avg
    last_candle: str = "neutral"      # candlestick pattern at entry

    @property
    def risk_reward(self) -> float:
        if self.stop_loss <= 0 or self.breakout_level <= 0:
            return 0.0
        measured_move = (self.breakout_level - self.stop_loss) * 3  # 3R target
        return measured_move / (self.breakout_level - self.stop_loss)

    def summary(self) -> str:
        if not self.passed:
            return f"✗ {self.symbol} — {self.fail_reason}"
        vol_tag = " [VOL✓]" if self.breakout_volume else ""
        return (f"✓ {self.symbol} | conf={self.confidence:.0%} | "
                f"entry=${self.breakout_level:.2f} SL=${self.stop_loss:.2f} "
                f"depth={self.pattern_depth_pct:.1%} contractions={self.contractions}"
                f" candle={self.last_candle}{vol_tag}")


def _find_pivots(close: pd.Series, window: int = 5) -> tuple[list[int], list[int]]:
    """Find local highs and lows using a rolling window."""
    highs, lows = [], []
    for i in range(window, len(close) - window):
        if close.iloc[i] == close.iloc[i-window:i+window+1].max():
            highs.append(i)
        if close.iloc[i] == close.iloc[i-window:i+window+1].min():
            lows.append(i)
    return highs, lows


def _check_breakout_volume(df: pd.DataFrame, multiplier: float = 1.5) -> bool:
    """
    Returns True if today's (last bar) volume is >= multiplier × 20-day average.
    A volume surge on the breakout bar is a key Minervini confirmation signal.
    """
    if len(df) < 21:
        return False
    vol = df["Volume"]
    avg_vol = float(vol.iloc[-21:-1].mean())   # 20-day avg excluding today
    today_vol = float(vol.iloc[-1])
    if avg_vol <= 0:
        return False
    return today_vol >= avg_vol * multiplier


def _quantitative_vcp_check(df: pd.DataFrame, cfg: dict) -> tuple[bool, dict]:
    """
    Quick quantitative check to see if a stock looks like a VCP.
    Returns (looks_like_vcp, details_dict).
    """
    lookback = cfg.get("lookback_days", 60)
    df_recent = df.tail(lookback).copy()
    close  = df_recent["Close"]
    volume = df_recent["Volume"]

    if len(close) < lookback // 2:
        return False, {"reason": "insufficient_data"}

    # 1. Find pivot highs and lows
    highs, lows = _find_pivots(close)
    if len(highs) < 2 or len(lows) < 2:
        return False, {"reason": "no_pivots_found"}

    # 2. Analyze contractions between pivot highs
    high_prices = [float(close.iloc[i]) for i in highs[-4:]]  # last 4 pivot highs
    if len(high_prices) < 2:
        return False, {"reason": "too_few_pivots"}

    # Check contractions: each pivot high should be lower than the previous
    n_contractions = sum(1 for i in range(1, len(high_prices))
                         if high_prices[i] <= high_prices[i-1])

    if n_contractions < cfg.get("min_contractions", 2):
        return False, {"reason": f"only_{n_contractions}_contractions"}

    # 3. Pattern depth from the consolidation high
    pattern_high = max(high_prices)
    current_low  = float(close.tail(10).min())
    depth = (pattern_high - current_low) / pattern_high

    if depth > cfg.get("max_depth_from_high", 0.35):
        return False, {"reason": f"pattern_too_deep_{depth:.1%}"}

    # 4. Final tight area — last 10 bars should be in a tight range
    last_10  = close.tail(10)
    tight_rng = (last_10.max() - last_10.min()) / last_10.mean()
    if tight_rng > cfg.get("final_tight_pct", 0.10):
        return False, {"reason": f"not_tight_enough_{tight_rng:.1%}"}

    # 5. Volume declining on contractions
    vol_ma   = float(volume.mean())
    vol_last = float(volume.tail(10).mean())
    vol_declining = vol_last < vol_ma * 0.8  # volume 20%+ below average in tight zone

    # 6. Breakout volume check — today's bar vs 20-day avg
    breakout_vol = _check_breakout_volume(df, multiplier=cfg.get("breakout_volume_min", 1.5))

    breakout_level = float(close.tail(20).max())  # recent swing high
    stop_loss_raw  = float(close.tail(20).min())   # recent swing low

    return True, {
        "contractions": n_contractions,
        "pattern_depth_pct": depth,
        "tight_rng_pct": tight_rng,
        "vol_declining": vol_declining,
        "breakout_volume": breakout_vol,
        "breakout_level": breakout_level,
        "stop_loss_candidate": stop_loss_raw,
    }


def _build_claude_prompt(symbol: str, df: pd.DataFrame, quant: dict,
                          last_candle: str = "neutral") -> str:
    """Build the prompt for Claude's VCP analysis."""
    recent = df.tail(60)
    close  = recent["Close"]
    high   = recent["High"]
    low    = recent["Low"]
    volume = recent["Volume"]
    dates  = recent.index.strftime("%Y-%m-%d")

    # Format as OHLCV table (last 60 days)
    rows = []
    for i in range(len(recent)):
        rows.append(
            f"{dates[i]}  H={high.iloc[i]:.2f}  L={low.iloc[i]:.2f}  "
            f"C={close.iloc[i]:.2f}  V={int(volume.iloc[i]):,}"
        )
    price_table = "\n".join(rows)

    ma50  = float(close.rolling(50, min_periods=30).mean().iloc[-1])
    ma200_full = float(df["Close"].rolling(200).mean().iloc[-1]) if len(df) >= 200 else 0

    vol_context = ""
    if quant.get("breakout_volume"):
        vol_context = "\n- **TODAY'S VOLUME**: Breakout-level surge detected (≥1.5× 20-day avg) — potential active breakout"
    if quant.get("vol_declining"):
        vol_context += "\n- **VOLUME TREND**: Volume has been drying up in the tight zone (bullish for VCP)"

    candle_context = ""
    if last_candle in ("hammer", "bullish_engulfing"):
        candle_context = f"\n- **ENTRY CANDLE**: {last_candle.upper()} pattern — strong bullish signal at the base"
    elif last_candle == "doji":
        candle_context = "\n- **ENTRY CANDLE**: DOJI — indecision, watch for follow-through"
    elif last_candle == "bullish":
        candle_context = "\n- **ENTRY CANDLE**: Bullish close — price closing near highs"

    return f"""You are an expert technical analyst specializing in Mark Minervini's VCP (Volatility Contraction Pattern) methodology.

## Task
Analyze the following 60-day price data for {symbol} and determine if it shows a valid VCP setup ready for a breakout.

## Price Data (last 60 trading days)
```
Date        High    Low     Close   Volume
{price_table}
```

## Context
- 50-day MA: ${ma50:.2f}
- 200-day MA: ${ma200_full:.2f}
- Current price: ${close.iloc[-1]:.2f}
- Quantitative pre-filter: {quant.get("contractions", 0)} contractions, depth={quant.get("pattern_depth_pct", 0):.1%}, tight_range={quant.get("tight_rng_pct", 0):.1%}
{vol_context}{candle_context}

## VCP Criteria to Evaluate
1. **Contractions**: 2-5 price contractions, each progressively narrower (both range and depth)
2. **Volume**: Volume drying up on each contraction — ideally the lowest volume of the pattern in the final tight area
3. **Tight pivot**: Final contraction should be < 5% range (tightest part of pattern)
4. **Above key MAs**: Price should be above 50-day and 200-day MA
5. **Breakout level**: The pivot high from the most recent tight consolidation
6. **Stop-loss**: The pivot low (lowest point of final contraction)

## Required Response (JSON only, no other text)
{{
  "vcp_confirmed": true/false,
  "confidence": 0.0-1.0,
  "quality_score": 1-5,
  "breakout_level": <price>,
  "stop_loss": <price>,
  "pivot_high": <price>,
  "pivot_low": <price>,
  "contractions_identified": <count>,
  "volume_pattern": "declining|mixed|increasing",
  "tight_area_pct": <range_as_decimal>,
  "pattern_notes": "<one sentence summary>",
  "risk_factors": "<one sentence if any>"
}}"""


def analyze(symbol: str, df: pd.DataFrame, use_deep_model: bool = False,
            last_candle: str = "neutral") -> VCPResult:
    """
    Run full VCP analysis on a stock.
    Returns VCPResult with Claude's verdict.

    Args:
        symbol: Ticker symbol
        df: OHLCV DataFrame with at least 60 rows
        use_deep_model: Use the more capable Claude model (slower)
        last_candle: Candlestick pattern detected by screener
    """
    cfg = VCP
    price = float(df["Close"].iloc[-1])

    # Step 1: Quantitative pre-filter
    looks_like_vcp, quant = _quantitative_vcp_check(df, cfg)
    if not looks_like_vcp:
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            fail_reason=quant.get("reason", "quant_filter_failed"),
            last_candle=last_candle,
        )

    breakout_vol = quant.get("breakout_volume", False)

    # Step 2: Send to Claude AI for confirmation
    prompt = _build_claude_prompt(symbol, df, quant, last_candle=last_candle)
    model  = CLAUDE_MODEL_DEEP if use_deep_model else CLAUDE_MODEL

    try:
        response = _get_client().messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code blocks if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)

    except json.JSONDecodeError as e:
        _log.warning("[vcp] %s JSON parse error: %s | raw=%s", symbol, e, raw[:200])
        return VCPResult(symbol=symbol, passed=False, current_price=price,
                         fail_reason="claude_parse_error", last_candle=last_candle)
    except Exception as e:
        _log.warning("[vcp] %s Claude error: %s", symbol, e)
        return VCPResult(symbol=symbol, passed=False, current_price=price,
                         fail_reason=f"claude_error:{e}", last_candle=last_candle)

    confirmed   = bool(data.get("vcp_confirmed", False))
    confidence  = float(data.get("confidence", 0))
    breakout    = float(data.get("breakout_level") or data.get("pivot_high") or quant.get("breakout_level", price))
    stop_loss   = float(data.get("stop_loss") or data.get("pivot_low") or quant.get("stop_loss_candidate", price * 0.92))
    depth       = quant.get("pattern_depth_pct", 0)
    contractions = int(data.get("contractions_identified") or quant.get("contractions", 0))
    tight_pct   = float(data.get("tight_area_pct") or quant.get("tight_rng_pct", 0))

    # Minimum confidence threshold
    if confidence < cfg.get("min_confidence", 0.55):
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
            fail_reason=f"low_confidence_{confidence:.0%}",
            ai_verdict="rejected",
            ai_reasoning=data.get("pattern_notes", ""),
            breakout_volume=breakout_vol,
            last_candle=last_candle,
        )

    passed = confirmed and confidence >= cfg.get("min_confidence", 0.55) and breakout > stop_loss

    result = VCPResult(
        symbol=symbol, passed=passed, current_price=price,
        confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
        pattern_depth_pct=depth, contractions=contractions, tight_pct=tight_pct,
        ai_verdict="confirmed" if confirmed else "rejected",
        ai_reasoning=data.get("pattern_notes", "") + " | " + data.get("risk_factors", ""),
        fail_reason="" if passed else f"claude_rejected_conf={confidence:.0%}",
        breakout_volume=breakout_vol,
        last_candle=last_candle,
    )

    if passed:
        vol_tag = " [BREAKOUT VOL✓]" if breakout_vol else ""
        _log.info("[vcp] ✓ %s | conf=%.0f%% | entry=$%.2f SL=$%.2f candle=%s%s",
                  symbol, confidence * 100, breakout, stop_loss, last_candle, vol_tag)

    return result


def batch_analyze(trend_passed: list, max_symbols: int = 50) -> list[VCPResult]:
    """
    Analyze a list of TrendResult objects for VCP patterns.
    Processes up to max_symbols, uses Haiku for speed.
    """
    from screener import TrendResult
    results = []
    candidates = [r for r in trend_passed if r.passed and r.df is not None][:max_symbols]

    _log.info("[vcp] Analyzing %d trend-passed stocks for VCP...", len(candidates))
    for i, trend in enumerate(candidates, 1):
        _log.info("[vcp] %d/%d: %s", i, len(candidates), trend.symbol)
        last_candle = getattr(trend, "last_candle", "neutral")
        result = analyze(trend.symbol, trend.df, last_candle=last_candle)
        results.append(result)

        # Rate limiting — avoid Claude API rate limits
        import time
        time.sleep(0.5)

    passed = [r for r in results if r.passed]
    _log.info("[vcp] Done: %d/%d passed VCP analysis", len(passed), len(results))
    return results
