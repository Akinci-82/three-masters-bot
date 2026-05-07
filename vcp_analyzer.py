"""
Layer 2 — MINERVINI
VCP (Volatility Contraction Pattern) analysis.

Three-tier Claude strategy:
  Tier 0: Quantitative pre-filter  — free, eliminates ~70% of candidates
  Tier 1: Haiku  (20-bar OHLCV)   — cheap yes/no + score 1-10
  Tier 2: Sonnet (100-bar OHLCV)  — full Minervini VCP analysis, only if Haiku is_vcp=True
  Tier 3: Opus   (validation)     — final verdict for quality_score >= 4 setups

VCP characteristics (Minervini):
  - Stock in confirmed Stage 2 uptrend (passed Trend Template)
  - Price consolidates in contracting swings (each narrower than previous)
  - Volume diminishes on each contraction (institutions holding, not distributing)
  - Final handle very tight (< 8% range, ideally < 5%)
  - Breakout = pivot HIGH of the final handle area
  - Stop = pivot LOW of the final handle area
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

from config import VCP, CLAUDE_MODEL, CLAUDE_MODEL_DEEP, CLAUDE_MODEL_ULTRA, ANTHROPIC_API_KEY, CHART_DIR, LOG_DIR

_log = logging.getLogger(__name__)
_client: anthropic.Anthropic | None = None

_HAIKU_MODEL  = CLAUDE_MODEL        # haiku-4-5-20251001
_SONNET_MODEL = CLAUDE_MODEL_DEEP   # sonnet-4-6
_OPUS_MODEL   = CLAUDE_MODEL_ULTRA  # opus-4-7

_TOKEN_LOG = LOG_DIR / "token_usage.jsonl"


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _log_tokens(symbol: str, tier: str, model: str,
                input_tok: int, output_tok: int) -> None:
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
    tier_used: str = ""            # "quant_fail" | "haiku_rejected" | "sonnet" | "opus"
    rs_rating: float = 0.0
    quality_score: int = 0
    rs_line_at_high: bool = False
    vol_at_multiweek_low: bool = False
    measured_move_pct: float = 0.0     # Claude estimate: full base height / entry price

    @property
    def risk_reward(self) -> float:
        if self.stop_loss <= 0 or self.breakout_level <= 0:
            return 0.0
        return 3.0

    def summary(self) -> str:
        if not self.passed:
            return f"✗ {self.symbol} — {self.fail_reason}"
        vol_tag = " [VOL✓]" if self.breakout_volume else ""
        qs_tag  = f" Q{self.quality_score}/5" if self.quality_score else ""
        return (f"✓ {self.symbol} | conf={self.confidence:.0%} | "
                f"entry=${self.breakout_level:.2f} SL=${self.stop_loss:.2f} "
                f"depth={self.pattern_depth_pct:.1%} C={self.contractions}"
                f" candle={self.last_candle}{vol_tag}{qs_tag} [{self.tier_used}]")


def _atr_adjusted_stop(df: pd.DataFrame, entry: float, pivot_stop: float) -> float:
    """
    Clamp the VCP pivot stop to 1–3 ATR below entry.
    Pivot stop from Claude is authoritative if it stays within range.
    """
    try:
        recent = df.tail(15)
        high   = recent["High"]
        low    = recent["Low"]
        close  = recent["Close"].shift(1)
        tr = pd.concat([
            high - low,
            (high - close).abs(),
            (low  - close).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.mean())
        if atr <= 0:
            return pivot_stop
        min_stop = entry - 1.0 * atr
        max_stop = entry - 3.0 * atr
        if pivot_stop > min_stop:
            return min_stop
        if pivot_stop < max_stop:
            return max_stop
        return pivot_stop
    except Exception:
        return pivot_stop


# ── Tier 0: Quantitative pre-filter ──────────────────────────────────────────

def _check_breakout_volume(df: pd.DataFrame, multiplier: float = 1.5) -> bool:
    if len(df) < 21:
        return False
    vol = df["Volume"]
    avg_vol = float(vol.iloc[-21:-1].mean())
    return avg_vol > 0 and float(vol.iloc[-1]) >= avg_vol * multiplier


def _quantitative_vcp_check(df: pd.DataFrame, cfg: dict) -> tuple[bool, dict]:
    """
    Segment-based VCP pre-filter.

    Splits lookback into 4 equal segments and measures H-L range per segment.
    Breakout level  = HIGH of the final 10-bar handle (the actual pivot point).
    Stop candidate  = LOW  of the final 10-bar handle.
    This is the correct Minervini definition — NOT the 20-day high/low.
    """
    lookback = cfg.get("lookback_days", 60)
    df_r = df.tail(lookback).copy()
    close  = df_r["Close"]
    high   = df_r["High"]
    low    = df_r["Low"]
    volume = df_r["Volume"]

    if len(close) < max(lookback // 2, 20):
        return False, {"reason": "insufficient_data"}

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

    n_contractions = sum(
        1 for i in range(1, len(seg_ranges))
        if seg_ranges[i] < seg_ranges[i - 1] * 0.95
    )

    if n_contractions < cfg.get("min_contractions", 2):
        return False, {"reason": f"only_{n_contractions}_contractions"}

    pattern_high = float(high.max())
    current_low  = float(low.tail(10).min())
    depth = (pattern_high - current_low) / pattern_high
    if depth > cfg.get("max_depth_from_high", 0.35):
        return False, {"reason": f"pattern_too_deep_{depth:.1%}"}

    # Final handle: last 10 bars.
    # Breakout = handle HIGH, stop = handle LOW — correct Minervini pivot levels.
    last10_h = float(high.tail(10).max())
    last10_l = float(low.tail(10).min())
    last10_c = float(close.tail(10).mean())
    tight_rng = (last10_h - last10_l) / last10_c if last10_c > 0 else 1.0
    max_tight = cfg.get("final_tight_pct", 0.08)
    if tight_rng > max_tight:
        return False, {"reason": f"not_tight_enough_{tight_rng:.1%}"}

    vol_ma   = float(volume.mean())
    vol_last = float(volume.tail(10).mean())
    vol_declining = vol_last < vol_ma * 0.85

    vol_100d = float(volume.tail(100).mean()) if len(volume) >= 100 else vol_ma
    vol_5d   = float(volume.tail(5).mean())
    vol_at_multiweek_low = vol_5d < vol_100d * 0.60

    breakout_vol = _check_breakout_volume(df, cfg.get("breakout_volume_min", 1.5))

    return True, {
        "contractions":           n_contractions,
        "pattern_depth_pct":      depth,
        "tight_rng_pct":          tight_rng,
        "seg_ranges":             seg_ranges,
        "vol_declining":          vol_declining,
        "vol_at_multiweek_low":   vol_at_multiweek_low,
        "vol_5d_vs_100d_pct":     round(vol_5d / vol_100d, 3) if vol_100d > 0 else 1.0,
        "breakout_volume":        breakout_vol,
        "breakout_level":         last10_h,    # pivot HIGH of handle
        "stop_loss_candidate":    last10_l,    # pivot LOW of handle
    }


# ── Tier 1: Haiku pre-screen ──────────────────────────────────────────────────

def _build_haiku_prompt(symbol: str, df: pd.DataFrame, quant: dict,
                         last_candle: str) -> str:
    """
    Haiku prompt with last 20 bars of actual OHLCV so it can see the handle shape.
    Goal: reject obvious non-VCPs cheaply before Sonnet.
    """
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]
    price = float(close.iloc[-1])

    ma50  = float(close.rolling(50, min_periods=30).mean().iloc[-1])
    _ma200 = df["Close"].rolling(200).mean().iloc[-1]
    try:
        ma200_str = f"${float(_ma200):.2f}" if float(_ma200) > 0 else "N/A"
    except Exception:
        ma200_str = "N/A"

    # Last 20 bars OHLCV — gives Haiku actual bar data to assess the handle
    r20   = df.tail(20)
    dates = r20.index.strftime("%m/%d")
    rows  = [
        f"{dates[i]} H={high.iloc[-20+i]:.2f} L={low.iloc[-20+i]:.2f} "
        f"C={close.iloc[-20+i]:.2f} V={int(vol.iloc[-20+i]/1000)}K"
        for i in range(20)
    ]
    bar_table = "\n".join(rows)

    seg_str = " → ".join(f"{r:.1%}" for r in quant.get("seg_ranges", [])) or "n/a"

    vol_note = ""
    if quant.get("breakout_volume"):
        vol_note = " | BREAKOUT VOL surge today"
    elif quant.get("vol_at_multiweek_low"):
        vol_note = " | volume at multi-week lows (ideal dryup)"
    elif quant.get("vol_declining"):
        vol_note = " | volume drying up"

    return (
        f"VCP pre-screen: {symbol}\n"
        f"Price ${price:.2f} | MA50 ${ma50:.2f} | MA200 {ma200_str}\n"
        f"60-day segment ranges (oldest→newest): {seg_str}\n"
        f"Pattern depth: {quant.get('pattern_depth_pct',0):.1%} | "
        f"Handle range (10 bars): {quant.get('tight_rng_pct',0):.1%}{vol_note}\n\n"
        f"Last 20 bars:\n{bar_table}\n\n"
        f"Does the handle (last 10 bars) show a tight, low-volume consolidation "
        f"after a multi-week contraction? Is this a valid VCP setup?\n\n"
        f'JSON only: {{"vcp_score": 1-10, "is_vcp": true/false, "reason": "one sentence"}}'
    )


def _call_haiku(symbol: str, prompt: str) -> dict:
    try:
        resp = _get_client().messages.create(
            model=_HAIKU_MODEL,
            max_tokens=150,
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
    """
    100-day OHLCV prompt with explicit step-by-step Minervini VCP analysis.
    Forces Claude to identify each contraction, measure precisely, and pinpoint
    the exact pivot high (entry) and pivot low (stop).
    """
    recent = df.tail(100)
    close  = recent["Close"]
    high   = recent["High"]
    low    = recent["Low"]
    volume = recent["Volume"]
    dates  = recent.index.strftime("%Y-%m-%d")

    # Volume average for context
    vol_avg = int(volume.mean())

    rows = [
        f"{dates[i]}  H={high.iloc[i]:.2f}  L={low.iloc[i]:.2f}  "
        f"C={close.iloc[i]:.2f}  V={int(volume.iloc[i]):,}"
        for i in range(len(recent))
    ]
    price_table = "\n".join(rows)

    ma50  = float(close.rolling(50, min_periods=30).mean().iloc[-1])
    ma200 = float(df["Close"].rolling(200).mean().iloc[-1]) if len(df) >= 200 else 0.0

    # Pre-computed quant context
    seg_str = " → ".join(f"{r:.1%}" for r in quant.get("seg_ranges", [])) or "n/a"

    vol_ctx = ""
    if quant.get("breakout_volume"):
        vol_ctx = "\n- BREAKOUT VOLUME: Today ≥1.5× 20-day avg — potential active breakout"
    if quant.get("vol_at_multiweek_low"):
        vol_ctx += "\n- VOLUME DRY-UP: Last 5-day avg <60% of 100-day avg — textbook VCP contraction"
    elif quant.get("vol_declining"):
        vol_ctx += "\n- VOLUME TREND: Declining in handle — constructive"

    candle_ctx = ""
    if last_candle in ("hammer", "bullish_engulfing"):
        candle_ctx = f"\n- ENTRY CANDLE: {last_candle.upper()} — strong demand signal"
    elif last_candle == "doji":
        candle_ctx = "\n- ENTRY CANDLE: DOJI — indecision near pivot"
    elif last_candle == "bullish":
        candle_ctx = "\n- ENTRY CANDLE: Bullish close"
    elif last_candle == "bearish":
        candle_ctx = "\n- ENTRY CANDLE: Bearish close — caution"

    return f"""You are executing Mark Minervini's VCP analysis protocol on {symbol}.

## Price Data (last {len(recent)} trading days, vol avg={vol_avg:,})
```
Date        High    Low     Close   Volume
{price_table}
```

## Context
- MA50=${ma50:.2f} | MA200=${ma200:.2f} | Current=${float(close.iloc[-1]):.2f}
- Quant segment ranges (oldest→newest): {seg_str}
- Depth from pattern high: {quant.get('pattern_depth_pct',0):.1%}
- Handle range (last 10 bars): {quant.get('tight_rng_pct',0):.1%}{vol_ctx}{candle_ctx}

## Analysis Protocol — follow each step:

**Step 1 — Trend**: Is price above MA50 and MA200? Is this a Stage 2 advance?

**Step 2 — Contraction Map**: Walk the chart chronologically. Identify each swing:
- Find swing HIGH (local peak), then swing LOW (pullback trough).
- Label C1, C2, C3... Each contraction = swing high minus swing low / swing high.
- REQUIRED: Is each Cn range SMALLER than C(n-1)? By at least 10%?

**Step 3 — Volume per contraction**: At each contraction's LOW point, is volume LOWER than the prior contraction? The final handle should have the LOWEST average volume of all contractions.

**Step 4 — Handle identification**: The FINAL tight area (last 10–15 bars before today):
- Handle high = the highest bar in this area = PIVOT POINT = buy-stop entry
- Handle low  = the lowest bar in this area = initial stop-loss
- Handle % range = (high − low) / handle_mid. Must be < 10%, ideally < 5%.

**Step 5 — Entry precision**:
- breakout_level = handle HIGH + $0.01 (just above pivot)
- stop_loss = handle LOW − $0.01 (just below pivot)

**Step 6 — Quality score 1–5**:
- 5 = Textbook: ≥3 tight contractions each clearly smaller, volume perfectly dry in handle, RS line at 52-week high, handle < 5%
- 4 = Strong: ≥3 contractions, volume declining each step, handle < 7%
- 3 = Acceptable: 2–3 contractions, mostly declining volume, handle 7–10%
- 2 = Marginal: 2 contractions, uneven volume, handle near 10%
- 1 = Weak: barely passes quant but lacks visual conviction

## Response (JSON ONLY — no prose, no markdown outside JSON)
{{
  "vcp_confirmed": true/false,
  "confidence": 0.0-1.0,
  "quality_score": 1-5,
  "breakout_level": <handle high + 0.01>,
  "stop_loss": <handle low - 0.01>,
  "pivot_high": <handle high>,
  "pivot_low": <handle low>,
  "contractions_identified": <count>,
  "contraction_pcts": [<C1_%>, <C2_%>, ...],
  "volume_pattern": "declining|mixed|increasing",
  "tight_area_pct": <handle range as decimal e.g. 0.047>,
  "pattern_notes": "<describe each contraction with dates and %: C1=date→date 18%, C2=...>",
  "risk_factors": "<one sentence on what could invalidate this setup>",
  "measured_move_pct": <full base height (from pattern low to pattern high) divided by current price, as decimal e.g. 0.22>
}}"""


def _call_sonnet(symbol: str, prompt: str) -> dict:
    try:
        resp = _get_client().messages.create(
            model=_SONNET_MODEL,
            max_tokens=600,
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


# ── Tier 3: Opus final validation for elite setups ────────────────────────────

def _build_opus_prompt(symbol: str, df: pd.DataFrame, sonnet: dict,
                        quant: dict, last_candle: str) -> str:
    """
    Opus validation prompt. Sonnet pre-identified a high-quality VCP.
    Opus acts as final gatekeeper — confirm or veto with precise reasoning.
    Receives 100-bar chart + Sonnet's full analysis to critique.
    """
    recent = df.tail(100)
    close  = recent["Close"]
    high   = recent["High"]
    low    = recent["Low"]
    volume = recent["Volume"]
    dates  = recent.index.strftime("%Y-%m-%d")
    vol_avg = int(volume.mean())

    rows = [
        f"{dates[i]}  H={high.iloc[i]:.2f}  L={low.iloc[i]:.2f}  "
        f"C={close.iloc[i]:.2f}  V={int(volume.iloc[i]):,}"
        for i in range(len(recent))
    ]
    price_table = "\n".join(rows)

    ma50  = float(close.rolling(50, min_periods=30).mean().iloc[-1])
    ma200 = float(df["Close"].rolling(200).mean().iloc[-1]) if len(df) >= 200 else 0.0

    prev_analysis = json.dumps(sonnet, indent=2)

    return f"""You are Mark Minervini's most demanding technical analyst. A junior analyst (Sonnet) flagged {symbol} as a potential high-quality VCP setup. Your job: validate or veto.

## Price Data (last {len(recent)} trading days, vol avg={vol_avg:,})
```
Date        High    Low     Close   Volume
{price_table}
```
MA50=${ma50:.2f} | MA200=${ma200:.2f} | Entry candle: {last_candle}

## Junior Analyst's Assessment
```json
{prev_analysis}
```

## Your Validation Checklist
1. Do the contraction percentages actually shrink each time? Check the raw bar data.
2. Is the handle truly low-volume compared to the prior contractions?
3. Is the proposed breakout level (${ sonnet.get('breakout_level', '?')}) the correct pivot high — or is there a better level?
4. Is the stop loss (${ sonnet.get('stop_loss', '?')}) logical — just below the handle low, not too wide?
5. Would Minervini actually trade this? What would make him walk away?
6. Risk/reward: entry ${sonnet.get('breakout_level','?')} → projected +20–25% move. Does the pattern support this?

## Response (JSON ONLY)
{{
  "vcp_confirmed": true/false,
  "confidence": 0.0-1.0,
  "quality_score": 1-5,
  "breakout_level": <your refined entry>,
  "stop_loss": <your refined stop>,
  "contractions_verified": true/false,
  "volume_valid": true/false,
  "tight_area_pct": <decimal>,
  "pattern_notes": "<precise critique of the setup in 1-2 sentences>",
  "risk_factors": "<what specifically could go wrong — be precise>",
  "measured_move_pct": <full base height / current price as decimal e.g. 0.22>,
  "minervini_verdict": "trade|skip|watch"
}}"""


def _call_opus(symbol: str, prompt: str) -> dict:
    try:
        resp = _get_client().messages.create(
            model=_OPUS_MODEL,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        _log_tokens(symbol, "tier3_opus", _OPUS_MODEL,
                    resp.usage.input_tokens, resp.usage.output_tokens)
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        _log.warning("[vcp] %s Opus error: %s", symbol, e)
        return {}


# ── Public API ────────────────────────────────────────────────────────────────

def analyze(symbol: str, df: pd.DataFrame, last_candle: str = "neutral") -> VCPResult:
    """
    Three-tier VCP analysis:
      Tier 0: quantitative filter (free)
      Tier 1: Haiku pre-screen (20-bar OHLCV)
      Tier 2: Sonnet full analysis (100-bar, step-by-step Minervini protocol)
      Tier 3: Opus validation — only for quality_score >= 4 from Sonnet
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
    breakout_lvl = quant.get("breakout_level", price)   # handle HIGH
    stop_cand    = quant.get("stop_loss_candidate", price * 0.93)  # handle LOW

    # ── Tier 1: Haiku pre-screen ─────────────────────────────────────────────
    h_prompt = _build_haiku_prompt(symbol, df, quant, last_candle)
    h_data   = _call_haiku(symbol, h_prompt)
    h_score  = int(h_data.get("vcp_score", 0))
    h_is_vcp = bool(h_data.get("is_vcp", False))

    _log.info("[vcp] %s Haiku score=%d is_vcp=%s | %s",
              symbol, h_score, h_is_vcp, h_data.get("reason", "")[:80])

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
    s_prompt = _build_sonnet_prompt(symbol, df, quant, last_candle, fundamentals)
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
    quality     = int(s_data.get("quality_score", 0))
    min_conf    = cfg.get("min_confidence", 0.65)
    min_quality = cfg.get("min_quality_score", 3)

    # Reject if Sonnet is not confident enough or quality is too low
    if not confirmed or confidence < min_conf:
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
            fail_reason=f"sonnet_rejected_conf={confidence:.0%}_q={quality}",
            ai_verdict="sonnet_rejected",
            ai_reasoning=s_data.get("pattern_notes", ""),
            breakout_volume=breakout_vol, last_candle=last_candle,
            quality_score=quality, tier_used="sonnet",
        )

    if quality < min_quality:
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
            fail_reason=f"low_quality_q={quality}<{min_quality}",
            ai_verdict="sonnet_low_quality",
            ai_reasoning=s_data.get("pattern_notes", ""),
            breakout_volume=breakout_vol, last_candle=last_candle,
            quality_score=quality, tier_used="sonnet",
        )

    # ── Tier 3: Opus final validation for elite setups ────────────────────────
    # quality >= 4: worth spending ~$0.03 to validate before betting real money
    tier_label = "sonnet"
    if quality >= 4:
        _log.info("[vcp] %s → Opus tier-3 validation (quality=%d)", symbol, quality)
        o_prompt = _build_opus_prompt(symbol, df, s_data, quant, last_candle)
        o_data   = _call_opus(symbol, o_prompt)

        if o_data:
            # Opus overrides Sonnet's levels and verdict
            verdict = o_data.get("minervini_verdict", "watch")
            if verdict == "skip":
                return VCPResult(
                    symbol=symbol, passed=False, current_price=price,
                    confidence=float(o_data.get("confidence", confidence)),
                    breakout_level=float(o_data.get("breakout_level", breakout)),
                    stop_loss=float(o_data.get("stop_loss", stop_loss)),
                    fail_reason=f"opus_veto: {o_data.get('risk_factors','')[:80]}",
                    ai_verdict="opus_vetoed",
                    ai_reasoning=o_data.get("pattern_notes", ""),
                    breakout_volume=breakout_vol, last_candle=last_candle,
                    quality_score=int(o_data.get("quality_score", quality)),
                    tier_used="opus",
                )
            # Refine levels with Opus precision
            breakout   = float(o_data.get("breakout_level") or breakout)
            stop_loss  = float(o_data.get("stop_loss") or stop_loss)
            confidence = float(o_data.get("confidence", confidence))
            quality    = int(o_data.get("quality_score", quality))
            tier_label = "opus"
            s_data = o_data   # use Opus notes for logging

    stop_loss = _atr_adjusted_stop(df, breakout, stop_loss)
    vol_multiweek = quant.get("vol_at_multiweek_low", False)
    passed = confirmed and confidence >= min_conf and breakout > stop_loss

    result = VCPResult(
        symbol=symbol, passed=passed, current_price=price,
        confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
        pattern_depth_pct=depth, contractions=contractions, tight_pct=tight_pct,
        ai_verdict="confirmed" if passed else "rejected",
        ai_reasoning=(s_data.get("pattern_notes", "") + " | " +
                      s_data.get("risk_factors", "")),
        fail_reason="" if passed else f"rejected_conf={confidence:.0%}",
        breakout_volume=breakout_vol, last_candle=last_candle,
        tier_used=tier_label,
        quality_score=quality,
        vol_at_multiweek_low=vol_multiweek,
        measured_move_pct=float(s_data.get("measured_move_pct", 0.0) or 0.0),
    )

    if passed:
        vol_tag = " [BREAKOUT VOL✓]" if breakout_vol else ""
        _log.info("[vcp] ✓ %s | conf=%.0f%% | entry=$%.2f SL=$%.2f Q%d/5 candle=%s%s [%s]",
                  symbol, confidence * 100, breakout, stop_loss, quality,
                  last_candle, vol_tag, tier_label)
    return result


def batch_analyze(trend_passed: list, max_symbols: int = 50) -> list[VCPResult]:
    """Analyze TrendResult objects for VCP. Tier 0→1→2→3 per stock."""
    import time
    results    = []
    candidates = [r for r in trend_passed if r.passed and r.df is not None][:max_symbols]
    _log.info("[vcp] Analyzing %d trend-passed stocks (Haiku→Sonnet→Opus tiering)...",
              len(candidates))

    for i, trend in enumerate(candidates, 1):
        _log.info("[vcp] %d/%d: %s", i, len(candidates), trend.symbol)
        last_candle = getattr(trend, "last_candle", "neutral")
        fund = {"eps_growth": getattr(trend, "eps_growth", None),
                "revenue_growth": getattr(trend, "revenue_growth", None)}
        result = analyze(trend.symbol, trend.df, last_candle=last_candle, fundamentals=fund)
        result.rs_rating       = getattr(trend, "rs_rating", 0.0)
        result.rs_line_at_high = getattr(trend, "rs_line_at_high", False)
        results.append(result)
        time.sleep(0.3)

    passed    = [r for r in results if r.passed]
    haiku_rej = sum(1 for r in results if "haiku" in r.tier_used)
    sonnet_n  = sum(1 for r in results if r.tier_used == "sonnet")
    opus_n    = sum(1 for r in results if r.tier_used == "opus")
    _log.info("[vcp] Done: %d/%d passed | Haiku rejected: %d | Sonnet: %d | Opus: %d",
              len(passed), len(results), haiku_rej, sonnet_n, opus_n)
    return results
