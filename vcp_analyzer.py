"""
Layer 2 — MINERVINI
VCP (Volatility Contraction Pattern) analysis.

Two-tier Claude strategy to minimise token costs (same pattern as trading-bot):
  Tier 0: Quantitative pre-filter  — free, eliminates ~70% of candidates
  Tier 1: Haiku  (short prompt)    — cheap yes/no + score 1-10 for remaining
  Tier 2: Sonnet (full OHLCV)      — deep analysis only for Tier-1 score >= 7

Estimated savings vs calling Sonnet for everything: ~90%+

VCP characteristics (Minervini):
  - Stock in confirmed uptrend (passed Trend Template)
  - Price consolidates in contracting swings (each narrower than previous)
  - Volume diminishes on each contraction (institutions holding, not distributing)
  - Final pivot very tight (< 10% range over last 10 bars)
  - Breakout on volume surge (>= 1.5x average)
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import anthropic
import numpy as np
import pandas as pd

from config import VCP, CLAUDE_MODEL, CLAUDE_MODEL_DEEP, ANTHROPIC_API_KEY, CHART_DIR, LOG_DIR

_log = logging.getLogger(__name__)
_client: anthropic.Anthropic | None = None

# Tier thresholds
_HAIKU_SCORE_THRESHOLD = 7   # Haiku score >= this → escalate to Sonnet
_HAIKU_MODEL  = CLAUDE_MODEL       # haiku-4-5-20251001
_SONNET_MODEL = CLAUDE_MODEL_DEEP  # sonnet-4-6

_TOKEN_LOG = LOG_DIR / "token_usage.jsonl"


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _log_tokens(symbol: str, tier: str, model: str,
                input_tok: int, output_tok: int) -> None:
    """Append token usage to logs/token_usage.jsonl."""
    rates = {
        "haiku":  (0.25e-6, 1.25e-6),
        "sonnet": (3.00e-6, 15.00e-6),
        "opus":   (15.0e-6, 75.00e-6),
    }
    key = next((k for k in rates if k in model), "sonnet")
    cost = input_tok * rates[key][0] + output_tok * rates[key][1]
    entry = {
        "ts": datetime.now().isoformat(), "date": str(date.today()),
        "symbol": symbol, "tier": tier, "model": model,
        "input_tokens": input_tok, "output_tokens": output_tok,
        "cost_usd": round(cost, 6),
    }
    try:
        _TOKEN_LOG.parent.mkdir(exist_ok=True)
        with open(_TOKEN_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class VCPResult:
    symbol: str
    passed: bool
    confidence: float = 0.0
    breakout_level: float = 0.0
    stop_loss: float = 0.0
    pattern_depth_pct: float = 0.0
    contractions: int = 0
    tight_pct: float = 0.0
    current_price: float = 0.0
    ai_verdict: str = ""
    ai_reasoning: str = ""
    fail_reason: str = ""
    breakout_volume: bool = False
    last_candle: str = "neutral"
    tier_used: str = ""            # "quant_fail" | "haiku_rejected" | "sonnet"
    rs_rating: float = 0.0         # RS rating from screener (for final sort priority)

    @property
    def risk_reward(self) -> float:
        if self.stop_loss <= 0 or self.breakout_level <= 0:
            return 0.0
        return 3.0  # 3R measured-move target

    def summary(self) -> str:
        if not self.passed:
            return f"✗ {self.symbol} — {self.fail_reason}"
        vol_tag = " [VOL✓]" if self.breakout_volume else ""
        return (f"✓ {self.symbol} | conf={self.confidence:.0%} | "
                f"entry=${self.breakout_level:.2f} SL=${self.stop_loss:.2f} "
                f"depth={self.pattern_depth_pct:.1%} contractions={self.contractions}"
                f" candle={self.last_candle}{vol_tag} [{self.tier_used}]")


# ── Tier 0: Quantitative pre-filter ──────────────────────────────────────────

def _check_breakout_volume(df: pd.DataFrame, multiplier: float = 1.5) -> bool:
    """Today's volume >= multiplier × 20-day average."""
    if len(df) < 21:
        return False
    vol = df["Volume"]
    avg_vol = float(vol.iloc[-21:-1].mean())
    return avg_vol > 0 and float(vol.iloc[-1]) >= avg_vol * multiplier


def _quantitative_vcp_check(df: pd.DataFrame, cfg: dict) -> tuple[bool, dict]:
    """
    Segment-based VCP pre-filter.

    Splits lookback into 4 equal segments and measures H-L range in each.
    A VCP requires the range to CONTRACT across at least 3 of 4 segments
    (each segment tighter than the previous).  This is more robust than
    pivot-based detection after V-shaped recoveries.
    """
    lookback = cfg.get("lookback_days", 60)
    df_r = df.tail(lookback).copy()
    close  = df_r["Close"]
    high   = df_r["High"]
    low    = df_r["Low"]
    volume = df_r["Volume"]

    if len(close) < max(lookback // 2, 20):
        return False, {"reason": "insufficient_data"}

    # ── Segment contraction ───────────────────────────────────────────────────
    n_seg  = 4
    seg_sz = len(close) // n_seg
    if seg_sz < 5:
        return False, {"reason": "insufficient_data"}

    seg_ranges = []
    for i in range(n_seg):
        s = i * seg_sz
        e = (i + 1) * seg_sz if i < n_seg - 1 else len(close)
        h = float(high.iloc[s:e].max())
        l = float(low.iloc[s:e].min())
        mid = (h + l) / 2
        seg_ranges.append((h - l) / mid if mid > 0 else 0)

    # Count segments where range NARROWS vs previous segment
    n_contractions = sum(
        1 for i in range(1, len(seg_ranges))
        if seg_ranges[i] < seg_ranges[i - 1] * 0.95  # at least 5% tighter
    )

    if n_contractions < cfg.get("min_contractions", 2):
        return False, {"reason": f"only_{n_contractions}_contractions"}

    # ── Pattern depth from overall high ──────────────────────────────────────
    pattern_high = float(high.max())
    current_low  = float(low.tail(10).min())
    depth = (pattern_high - current_low) / pattern_high
    if depth > cfg.get("max_depth_from_high", 0.35):
        return False, {"reason": f"pattern_too_deep_{depth:.1%}"}

    # ── Final tight zone: last 10 bars H-L range vs mean close ───────────────
    last10_h = float(high.tail(10).max())
    last10_l = float(low.tail(10).min())
    last10_c = float(close.tail(10).mean())
    tight_rng = (last10_h - last10_l) / last10_c if last10_c > 0 else 1.0
    max_tight = cfg.get("final_tight_pct", 0.10)  # 10% default (was 5% — too strict)
    if tight_rng > max_tight:
        return False, {"reason": f"not_tight_enough_{tight_rng:.1%}"}

    # ── Volume metrics ────────────────────────────────────────────────────────
    vol_ma   = float(volume.mean())
    vol_last = float(volume.tail(10).mean())
    vol_declining = vol_last < vol_ma * 0.85

    breakout_vol  = _check_breakout_volume(df, cfg.get("breakout_volume_min", 1.5))
    breakout_lvl  = float(high.tail(20).max())
    stop_loss_raw = float(low.tail(20).min())

    return True, {
        "contractions":       n_contractions,
        "pattern_depth_pct":  depth,
        "tight_rng_pct":      tight_rng,
        "seg_ranges":         seg_ranges,
        "vol_declining":      vol_declining,
        "breakout_volume":    breakout_vol,
        "breakout_level":     breakout_lvl,
        "stop_loss_candidate": stop_loss_raw,
    }


# ── Tier 1: Haiku short pre-screen ───────────────────────────────────────────

def _build_haiku_prompt(symbol: str, df: pd.DataFrame, quant: dict,
                         last_candle: str) -> str:
    """Short Haiku prompt — only summary stats, no full OHLCV table."""
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    price = float(close.iloc[-1])

    ma50  = float(close.rolling(50, min_periods=30).mean().iloc[-1])
    _ma200_raw = df["Close"].rolling(200).mean().iloc[-1]
    ma200_str = f"${float(_ma200_raw):.2f}" if not (
        hasattr(_ma200_raw, '__float__') and (
            __import__('math').isnan(float(_ma200_raw)) or float(_ma200_raw) == 0
        )
    ) else "N/A"

    # Last 20 bars summary
    r20 = df.tail(20)
    h20, l20 = float(r20["High"].max()), float(r20["Low"].min())
    rng20_pct = (h20 - l20) / price * 100

    seg_ranges = quant.get("seg_ranges", [])
    seg_str = " → ".join(f"{r:.1%}" for r in seg_ranges) if seg_ranges else "n/a"

    vol_note = ""
    if quant.get("breakout_volume"):
        vol_note = " | BREAKOUT VOL surge today (≥1.5x avg)"
    elif quant.get("vol_declining"):
        vol_note = " | volume drying up in tight zone"

    candle_note = ""
    if last_candle in ("hammer", "bullish_engulfing"):
        candle_note = f" | entry candle: {last_candle.upper()}"
    elif last_candle not in ("neutral", ""):
        candle_note = f" | entry candle: {last_candle}"

    return (
        f"VCP pre-screen for {symbol}.\n"
        f"Price ${price:.2f} | MA50 ${ma50:.2f} | MA200 {ma200_str}\n"
        f"Segment H-L ranges (oldest→newest): {seg_str}\n"
        f"Pattern depth from high: {quant.get('pattern_depth_pct',0):.1%} | "
        f"Last-10-bar tight range: {quant.get('tight_rng_pct',0):.1%}\n"
        f"Last 20 bars: H=${h20:.2f} L=${l20:.2f} ({rng20_pct:.1f}%){vol_note}{candle_note}\n\n"
        f"Is this a valid Minervini VCP setup (2-5 contracting swings, volume drying up, "
        f"tight final pivot, above both MAs)?\n\n"
        f"Respond with JSON only: "
        f'{{ "vcp_score": 1-10, "is_vcp": true/false, "reason": "one sentence" }}'
    )


def _call_haiku(symbol: str, prompt: str) -> dict:
    """Call Haiku tier-1 and return parsed dict with score + is_vcp."""
    try:
        resp = _get_client().messages.create(
            model=_HAIKU_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        _log_tokens(symbol, "tier1_haiku", _HAIKU_MODEL,
                    resp.usage.input_tokens, resp.usage.output_tokens)
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        _log.debug("[vcp] %s Haiku error: %s", symbol, e)
        return {"vcp_score": 0, "is_vcp": False, "reason": str(e)}


# ── Tier 2: Sonnet full analysis ──────────────────────────────────────────────

def _build_sonnet_prompt(symbol: str, df: pd.DataFrame, quant: dict,
                          last_candle: str) -> str:
    """Full 60-day OHLCV prompt for Sonnet deep analysis."""
    recent = df.tail(60)
    close  = recent["Close"]
    high   = recent["High"]
    low    = recent["Low"]
    volume = recent["Volume"]
    dates  = recent.index.strftime("%Y-%m-%d")

    rows = [
        f"{dates[i]}  H={high.iloc[i]:.2f}  L={low.iloc[i]:.2f}  "
        f"C={close.iloc[i]:.2f}  V={int(volume.iloc[i]):,}"
        for i in range(len(recent))
    ]
    price_table = "\n".join(rows)

    ma50  = float(close.rolling(50, min_periods=30).mean().iloc[-1])
    ma200 = float(df["Close"].rolling(200).mean().iloc[-1]) if len(df) >= 200 else 0

    vol_ctx = ""
    if quant.get("breakout_volume"):
        vol_ctx = "\n- **TODAY'S VOLUME**: Breakout surge ≥1.5× 20-day avg — active breakout"
    if quant.get("vol_declining"):
        vol_ctx += "\n- **VOLUME TREND**: Volume drying up in tight zone (bullish)"

    candle_ctx = ""
    if last_candle in ("hammer", "bullish_engulfing"):
        candle_ctx = f"\n- **ENTRY CANDLE**: {last_candle.upper()} — strong bullish signal"
    elif last_candle == "doji":
        candle_ctx = "\n- **ENTRY CANDLE**: DOJI — indecision, watch follow-through"
    elif last_candle == "bullish":
        candle_ctx = "\n- **ENTRY CANDLE**: Bullish close near highs"

    return f"""You are an expert technical analyst specializing in Mark Minervini's VCP methodology.

## Task
Analyze 60-day OHLCV for {symbol}. Confirm VCP, identify exact entry and stop-loss.

## Price Data (last 60 trading days)
```
Date        High    Low     Close   Volume
{price_table}
```

## Context
- 50-day MA: ${ma50:.2f} | 200-day MA: ${ma200:.2f} | Current: ${close.iloc[-1]:.2f}
- Quant: {quant.get('contractions',0)} contractions | depth={quant.get('pattern_depth_pct',0):.1%} | tight={quant.get('tight_rng_pct',0):.1%}
{vol_ctx}{candle_ctx}

## VCP Criteria
1. 2-5 contractions, each progressively narrower (range AND depth shrinking)
2. Volume drying up on each contraction (lowest vol in final tight zone)
3. Final pivot < 10% range (< 5% ideal)
4. Price above both 50-day and 200-day MA
5. Breakout level = pivot high of final tight area
6. Stop-loss = pivot low of final contraction

## Response (JSON only, no other text)
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
  "tight_area_pct": <decimal>,
  "pattern_notes": "<one sentence>",
  "risk_factors": "<one sentence>"
}}"""


def _call_sonnet(symbol: str, prompt: str) -> dict:
    """Call Sonnet tier-2 and return parsed dict."""
    try:
        resp = _get_client().messages.create(
            model=_SONNET_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        _log_tokens(symbol, "tier2_sonnet", _SONNET_MODEL,
                    resp.usage.input_tokens, resp.usage.output_tokens)
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        _log.warning("[vcp] %s Sonnet error: %s", symbol, e)
        return {}


# ── Public API ────────────────────────────────────────────────────────────────

def analyze(symbol: str, df: pd.DataFrame, last_candle: str = "neutral") -> VCPResult:
    """
    Full two-tier VCP analysis.
      Tier 0: quantitative filter (free)
      Tier 1: Haiku short pre-screen — all quant-passed stocks
      Tier 2: Sonnet full analysis   — only if Haiku score >= 7
    """
    cfg   = VCP
    price = float(df["Close"].iloc[-1])

    # ── Tier 0 ───────────────────────────────────────────────────────────────
    ok, quant = _quantitative_vcp_check(df, cfg)
    if not ok:
        _log.debug("[vcp] %s quant fail: %s", symbol, quant.get("reason"))
        return VCPResult(symbol=symbol, passed=False, current_price=price,
                         fail_reason=quant.get("reason", "quant_fail"),
                         last_candle=last_candle, tier_used="quant_fail")

    breakout_vol = quant.get("breakout_volume", False)
    breakout_lvl = quant.get("breakout_level", price)
    stop_cand    = quant.get("stop_loss_candidate", price * 0.93)

    # ── Tier 1: Haiku pre-screen ─────────────────────────────────────────────
    h_prompt = _build_haiku_prompt(symbol, df, quant, last_candle)
    h_data   = _call_haiku(symbol, h_prompt)
    h_score  = int(h_data.get("vcp_score", 0))
    h_is_vcp = bool(h_data.get("is_vcp", False))

    _log.info("[vcp] %s Haiku score=%d is_vcp=%s | %s",
              symbol, h_score, h_is_vcp, h_data.get("reason", "")[:80])

    # Reject only if Haiku explicitly says it's NOT a VCP.
    # Score is informational — any is_vcp=True proceeds to Sonnet for final verdict.
    if not h_is_vcp:
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            breakout_level=breakout_lvl, stop_loss=stop_cand,
            pattern_depth_pct=quant.get("pattern_depth_pct", 0),
            contractions=quant.get("contractions", 0),
            breakout_volume=breakout_vol, last_candle=last_candle,
            fail_reason=f"haiku_rejected_score={h_score} ({h_data.get('reason','')[:60]})",
            ai_verdict="haiku_rejected",
            tier_used="haiku_rejected",
        )

    # ── Tier 2: Sonnet deep analysis ─────────────────────────────────────────
    _log.info("[vcp] %s → Sonnet tier-2 (Haiku score=%d)", symbol, h_score)
    s_prompt = _build_sonnet_prompt(symbol, df, quant, last_candle)
    s_data   = _call_sonnet(symbol, s_prompt)

    if not s_data:
        return VCPResult(symbol=symbol, passed=False, current_price=price,
                         fail_reason="sonnet_error", last_candle=last_candle,
                         tier_used="sonnet_error")

    confirmed   = bool(s_data.get("vcp_confirmed", False))
    confidence  = float(s_data.get("confidence", 0))
    breakout    = float(s_data.get("breakout_level") or s_data.get("pivot_high") or breakout_lvl)
    stop_loss   = float(s_data.get("stop_loss") or s_data.get("pivot_low") or stop_cand)
    depth       = quant.get("pattern_depth_pct", 0)
    contractions = int(s_data.get("contractions_identified") or quant.get("contractions", 0))
    tight_pct   = float(s_data.get("tight_area_pct") or quant.get("tight_rng_pct", 0))
    min_conf    = cfg.get("min_confidence", 0.55)

    if confidence < min_conf:
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
            fail_reason=f"low_confidence_{confidence:.0%}",
            ai_verdict="sonnet_rejected",
            ai_reasoning=s_data.get("pattern_notes", ""),
            breakout_volume=breakout_vol, last_candle=last_candle,
            tier_used="sonnet",
        )

    passed = confirmed and confidence >= min_conf and breakout > stop_loss
    result = VCPResult(
        symbol=symbol, passed=passed, current_price=price,
        confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
        pattern_depth_pct=depth, contractions=contractions, tight_pct=tight_pct,
        ai_verdict="confirmed" if confirmed else "rejected",
        ai_reasoning=(s_data.get("pattern_notes", "") + " | " +
                      s_data.get("risk_factors", "")),
        fail_reason="" if passed else f"sonnet_rejected_conf={confidence:.0%}",
        breakout_volume=breakout_vol, last_candle=last_candle,
        tier_used="sonnet",
    )

    if passed:
        vol_tag = " [BREAKOUT VOL✓]" if breakout_vol else ""
        _log.info("[vcp] ✓ %s | conf=%.0f%% | entry=$%.2f SL=$%.2f candle=%s%s [Sonnet]",
                  symbol, confidence * 100, breakout, stop_loss, last_candle, vol_tag)
    return result


def batch_analyze(trend_passed: list, max_symbols: int = 50) -> list[VCPResult]:
    """Analyze TrendResult objects for VCP. Tier 0→1→2 per stock."""
    import time
    results    = []
    candidates = [r for r in trend_passed if r.passed and r.df is not None][:max_symbols]
    _log.info("[vcp] Analyzing %d trend-passed stocks (Haiku→Sonnet tiering)...",
              len(candidates))

    for i, trend in enumerate(candidates, 1):
        _log.info("[vcp] %d/%d: %s", i, len(candidates), trend.symbol)
        last_candle = getattr(trend, "last_candle", "neutral")
        result = analyze(trend.symbol, trend.df, last_candle=last_candle)
        result.rs_rating = getattr(trend, "rs_rating", 0.0)
        results.append(result)
        time.sleep(0.3)  # gentle rate-limit

    passed = [r for r in results if r.passed]
    haiku_rej  = sum(1 for r in results if "haiku" in r.tier_used)
    sonnet_ran = sum(1 for r in results if "sonnet" in r.tier_used)
    _log.info("[vcp] Done: %d/%d passed | Haiku rejected: %d | Sonnet ran: %d",
              len(passed), len(results), haiku_rej, sonnet_ran)
    return results
