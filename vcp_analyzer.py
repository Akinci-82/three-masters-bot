"""
Layer 2 — MINERVINI
VCP (Volatility Contraction Pattern) analysis.

Three-tier Claude strategy:
  Tier 0: Quantitative pre-filter  — free, eliminates ~70% of candidates
  Tier 1: Haiku  (40-bar OHLCV)   — cheap yes/no + score 1-10  (P3-fix: was 20)
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
import re
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import anthropic
import numpy as np
import pandas as pd
import yfinance as yf

from config import VCP, CLAUDE_MODEL, CLAUDE_MODEL_DEEP, CLAUDE_MODEL_ULTRA, ANTHROPIC_API_KEY, CHART_DIR, LOG_DIR

_log = logging.getLogger(__name__)
_client: anthropic.Anthropic | None = None
_CLIENT_LOCK = threading.Lock()

_HAIKU_MODEL  = CLAUDE_MODEL        # haiku-4-5-20251001
_SONNET_MODEL = CLAUDE_MODEL_DEEP   # sonnet-4-6
_OPUS_MODEL   = CLAUDE_MODEL_ULTRA  # opus-4-7

_TOKEN_LOG  = LOG_DIR / "token_usage.jsonl"
_VCP_CACHE  = LOG_DIR / "vcp_cache.json"

_VCP_CACHE_LOCK = threading.Lock()
_TOKEN_BUFFER: list = []          # P2-fix: buffer token log entries, flush in batches
_TOKEN_BUFFER_LOCK = threading.Lock()
_spy_weekly_cache: dict = {}  # {"df": pd.DataFrame, "ts": float}
_SPY_WEEKLY_CACHE_LOCK = threading.Lock()


def _load_vcp_cache() -> dict:
    with _VCP_CACHE_LOCK:
        try:
            if _VCP_CACHE.exists():
                raw = json.loads(_VCP_CACHE.read_text())
                from datetime import timedelta
                cutoff = str(date.today() - timedelta(days=1))
                return {k: v for k, v in raw.items() if v.get("date", "") >= cutoff}
        except json.JSONDecodeError as e:
            _log.warning("[vcp] cache corrupted, discarding: %s", e)
        except Exception as e:
            _log.warning("[vcp] cache read error: %s", e)
    return {}


def _save_vcp_cache(cache: dict) -> None:
    with _VCP_CACHE_LOCK:
        try:
            _VCP_CACHE.parent.mkdir(exist_ok=True)
            _tmp = _VCP_CACHE.with_suffix(".json.tmp")
            _tmp.write_text(json.dumps(cache, indent=2))
            _tmp.replace(_VCP_CACHE)
        except Exception as e:
            _log.warning("[vcp] cache save error: %s", e)


def _serialize_result(r) -> dict:
    import dataclasses
    d = {}
    for fld in dataclasses.fields(r):
        if fld.name == "df":
            continue
        v = getattr(r, fld.name)
        if isinstance(v, (bool, int, float, str)):
            d[fld.name] = v
        else:
            d[fld.name] = str(v)
    return d


def _deserialize_result(d: dict):
    import dataclasses
    fields = {fld.name: fld for fld in dataclasses.fields(VCPResult)}
    kwargs = {}
    for name, fld in fields.items():
        if name == "df":
            kwargs[name] = None
            continue
        if name not in d:
            continue
        raw = d[name]
        typ = str(fld.type)
        try:
            if "bool" in typ:
                kwargs[name] = raw is True or (isinstance(raw, str) and raw.lower() == "true")
            elif "int" in typ:
                kwargs[name] = int(raw)
            elif "float" in typ:
                kwargs[name] = float(raw)
            else:
                kwargs[name] = raw
        except Exception:
            kwargs[name] = raw
    return VCPResult(**kwargs)


def _get_client() -> anthropic.Anthropic:
    global _client
    with _CLIENT_LOCK:
        if _client is None:
            _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=20.0)
        return _client


def _log_tokens(symbol: str, tier: str, model: str,
                input_tok: int, output_tok: int) -> None:
    """Buffer token usage entries and flush to disk every 10 entries.
    P2-fix: was opening/writing/closing the file on every single Claude call
    (150+ open/write/close cycles per full run). Now batched.
    """
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
    with _TOKEN_BUFFER_LOCK:
        _TOKEN_BUFFER.append(entry)
        if len(_TOKEN_BUFFER) >= 10:
            _flush_token_buffer()


def _flush_token_buffer() -> None:
    """Write buffered token entries to disk. Must be called with _TOKEN_BUFFER_LOCK held."""
    if not _TOKEN_BUFFER:
        return
    try:
        _TOKEN_LOG.parent.mkdir(exist_ok=True)
        with open(_TOKEN_LOG, "a") as f:
            for _e in _TOKEN_BUFFER:
                f.write(json.dumps(_e) + "\n")
        _TOKEN_BUFFER.clear()
    except Exception:
        pass


def flush_token_log() -> None:
    """Public flush — call at end of batch_analyze to ensure all entries are written."""
    with _TOKEN_BUFFER_LOCK:
        _flush_token_buffer()


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
    pattern_type: str  = "vcp"          # "vcp" | "cwh" | "both"
    catalyst_score: float = 0.0         # parsed from ai_reasoning: strong/weak catalyst signals
    news_positive: bool = False          # Haiku news sentiment gate: positive headlines present
    news_negative: bool = False          # Haiku news sentiment gate: negative headlines → raised min_conf

    @property
    def risk_reward(self) -> float:
        """Compute actual R:R from measured_move_pct and stop distance.
        R:R = (measured_move_pct * breakout_level) / (breakout_level - stop_loss).
        Falls back to 3.0 if data is incomplete.
        """
        if self.stop_loss <= 0 or self.breakout_level <= 0:
            return 0.0
        risk_per_share = self.breakout_level - self.stop_loss
        if risk_per_share <= 0:
            return 0.0
        if self.measured_move_pct > 0:
            reward = self.measured_move_pct * self.breakout_level
            return round(reward / risk_per_share, 2)
        return 3.0  # default when Claude did not provide measured_move_pct

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
        if max_stop <= 0:
            max_stop = entry * 0.85
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

    pattern_high = float(high.quantile(0.97))
    current_low  = float(low.tail(10).min())
    depth = (pattern_high - current_low) / pattern_high
    if depth > cfg.get("max_depth_from_high", 0.35):
        return False, {"reason": f"pattern_too_deep_{depth:.1%}"}

    # Handle depth rule: handle must not retrace >50% of cup advance
    # Deep handles = failed breakout attempts or distribution — Minervini rejects these
    _cup_bottom   = float(df_r["Low"].iloc[:-10].min())
    _cup_advance  = pattern_high - _cup_bottom
    _handle_depth = pattern_high - current_low
    if _cup_advance > 0 and _handle_depth / _cup_advance > 0.50:
        return False, {"reason": f"handle_too_deep_{_handle_depth/_cup_advance:.0%}_of_cup"}

    # Base freshness: pattern high must have formed >= 25 bars ago (base has matured)
    # A fresh pivot means price is still correcting; true VCP needs time to consolidate
    _high_idx       = int(high.argmax())
    _bars_since_high = len(high) - _high_idx - 1
    if _bars_since_high < 25:
        return False, {"reason": f"pattern_high_too_recent_{_bars_since_high}bars"}

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

    # Multi-handle detection: count distinct swing highs in the base using a simple
    # peak filter — a bar is a "handle peak" if its high is the highest of the 5 bars
    # on each side. Each peak = one corrective cycle. 3-handle VCPs are the highest
    # quality (stock has retested resistance multiple times with decreasing volume).
    _n_handles = 1
    try:
        _h_arr = high.values
        _peaks = []
        for _pi in range(5, len(_h_arr) - 5):
            if _h_arr[_pi] == max(_h_arr[_pi - 5: _pi + 6]):
                # Must be > 1% above surrounding context to qualify as a distinct handle
                _local_min = min(_h_arr[max(0, _pi - 5): _pi + 6])
                if _local_min > 0 and (_h_arr[_pi] - _local_min) / _local_min > 0.01:
                    if not _peaks or _pi - _peaks[-1] >= 5:
                        _peaks.append(_pi)
        _n_handles = max(1, len(_peaks))
    except Exception:
        _n_handles = 1

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
        "n_handles":              _n_handles,
    }


# ── Tier 1: Haiku pre-screen ──────────────────────────────────────────────────

def _build_haiku_prompt(symbol: str, df: pd.DataFrame, quant: dict,
                         last_candle: str) -> str:
    """
    Haiku prompt with last 40 bars of OHLCV (P3-fix: was 20 — insufficient to see prior
    contraction context; Haiku needs at least one prior swing to judge if contractions tighten).
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

    # Last 40 bars OHLCV — P3-fix: was 20, now 40 to include at least one prior contraction
    # so Haiku can judge whether the handle is genuinely tightening vs earlier swings
    _n_haiku = 40
    r40   = df.tail(_n_haiku)
    dates = r40.index.strftime("%m/%d")
    rows  = [
        f"{dates[i]} H={high.iloc[-_n_haiku+i]:.2f} L={low.iloc[-_n_haiku+i]:.2f} "
        f"C={close.iloc[-_n_haiku+i]:.2f} V={int(vol.iloc[-_n_haiku+i]/1000)}K"
        for i in range(_n_haiku)
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
        f"Last {_n_haiku} bars:\n{bar_table}\n\n"
        f"Does the handle (last 10 bars) show a tight, low-volume consolidation "
        f"after a multi-week contraction? Is this a valid VCP setup?\n\n"
        f'JSON only: {{"vcp_score": 1-10, "is_vcp": true/false, "reason": "one sentence"}}'
    )


def _call_haiku(symbol: str, prompt: str) -> dict:
    try:
        resp = _get_client().messages.create(
            model=_HAIKU_MODEL,
            max_tokens=150,
            system=[{"type": "text", "text": "Respond with raw JSON only, no markdown fences.",
                      "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        _log_tokens(symbol, "tier1_haiku", _HAIKU_MODEL,
                    resp.usage.input_tokens, resp.usage.output_tokens)
        # P4.2: robust fence extraction via regex
        _m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if _m:
            raw = _m.group(1).strip()
        elif raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except anthropic.APIError as e:
        _log.warning("[vcp] %s Haiku API error (not a VCP rejection): %s", symbol, e)
        return {"_api_error": True, "vcp_score": 0, "is_vcp": None, "reason": f"api_error:{e}"}
    except (json.JSONDecodeError, ValueError) as e:
        _log.warning("[vcp] %s Haiku parse error: %s", symbol, e)
        return {"vcp_score": 0, "is_vcp": False, "reason": f"parse_error:{e}"}
    except Exception as e:
        _log.warning("[vcp] %s Haiku unexpected error: %s", symbol, e)
        return {"_api_error": True, "vcp_score": 0, "is_vcp": None, "reason": str(e)}


# ── Tier 2: Sonnet full analysis ──────────────────────────────────────────────

def _get_weekly_context(symbol: str, df: pd.DataFrame) -> str:
    """
    Build a concise weekly-chart summary appended to the Sonnet prompt.
    Resamples the daily df already in memory to weekly bars.
    SPY weekly is cached at module level with a 4-hour TTL.
    Returns an empty string on any error (graceful degradation).
    """
    import time as _t
    try:
        weekly = df.resample("W-FRI").agg({
            "Open": "first", "High": "max",
            "Low": "min", "Close": "last", "Volume": "sum",
        }).dropna()
        if len(weekly) < 10:
            return ""

        _now = _t.time()
        with _SPY_WEEKLY_CACHE_LOCK:
            if "df" in _spy_weekly_cache and _now - _spy_weekly_cache.get("ts", 0) < 14400:
                spy_wk = _spy_weekly_cache["df"]
            else:
                try:
                    spy_wk = yf.Ticker("SPY").history(period="6mo", interval="1wk", auto_adjust=True)
                    _spy_weekly_cache["df"] = spy_wk
                    _spy_weekly_cache["ts"] = _now
                except Exception:
                    spy_wk = pd.DataFrame()

        weekly["ma10w"] = weekly["Close"].rolling(10, min_periods=5).mean()
        w20 = weekly.tail(20)
        latest_close = float(w20["Close"].iloc[-1])
        latest_ma10w = float(w20["ma10w"].iloc[-1]) if not pd.isna(w20["ma10w"].iloc[-1]) else 0.0
        above_ma10w  = latest_close > latest_ma10w if latest_ma10w > 0 else None

        ma10w_slope = 0.0
        if len(w20) >= 4 and latest_ma10w > 0:
            _ma4w = float(w20["ma10w"].iloc[-4])
            if not pd.isna(_ma4w) and _ma4w > 0:
                ma10w_slope = (latest_ma10w - _ma4w) / _ma4w * 100

        rs_summary = ""
        if not spy_wk.empty:
            # Normalize both to UTC to avoid tz-mismatch empty intersection
            try:
                if w20.index.tz is not None:
                    w20 = w20.copy()
                    w20.index = w20.index.tz_convert("UTC")
                elif w20.index.tz is None:
                    w20 = w20.copy()
                    w20.index = w20.index.tz_localize("UTC")
                if not spy_wk.empty:
                    if spy_wk.index.tz is not None:
                        spy_wk = spy_wk.copy()
                        spy_wk.index = spy_wk.index.tz_convert("UTC")
                    elif spy_wk.index.tz is None:
                        spy_wk = spy_wk.copy()
                        spy_wk.index = spy_wk.index.tz_localize("UTC")
            except Exception:
                pass
            common = w20.index.intersection(spy_wk.index)
            if len(common) >= 5:
                rs_line = w20["Close"].loc[common] / spy_wk["Close"].loc[common]
                rs_now  = float(rs_line.iloc[-1])
                rs_high = float(rs_line.max())
                pct_from_high = (rs_now - rs_high) / rs_high * 100 if rs_high > 0 else 0.0
                at_high = pct_from_high >= -3.0
                rs_summary = (
                    f"Weekly RS line: {rs_now:.4f} | 20-wk RS high: {rs_high:.4f} | "
                    + ("AT 20-WEEK HIGH ✓" if at_high else f"{pct_from_high:.1f}% below 20-wk high")
                )

        vol_avg10 = float(w20["Volume"].tail(10).mean()) if len(w20) >= 10 else 0
        vol_last  = float(w20["Volume"].iloc[-1])
        vol_note  = ""
        if vol_avg10 > 0:
            if vol_last < vol_avg10 * 0.60:
                vol_note = "Weekly vol dry-up: last week <60% of 10-wk avg — institutions holding"
            elif vol_last > vol_avg10 * 1.50:
                vol_note = "Heavy weekly volume — potential distribution, verify"

        rows = []
        for _dt, _row in w20.tail(10).iterrows():
            _ma = f"{_row['ma10w']:.2f}" if not pd.isna(_row["ma10w"]) else "n/a"
            rows.append(
                f"  {str(_dt)[:10]}  C={_row['Close']:.2f}  H={_row['High']:.2f}  "
                f"L={_row['Low']:.2f}  V={int(_row['Volume']/1000)}K  MA10w={_ma}"
            )

        above_str = ("ABOVE MA10w" if above_ma10w else
                     "BELOW MA10w" if above_ma10w is not None else "MA10w n/a")
        lines = [
            "\n\n## Weekly Context (last 10 of 20 weeks)",
            f"Stage: {above_str} | MA10w slope (4w): {ma10w_slope:+.1f}%",
        ]
        if rs_summary:
            lines.append(rs_summary)
        if vol_note:
            lines.append(vol_note)
        lines.append("```")
        lines.append("Week-end     Close   High    Low     Volume    MA10w")
        lines.extend(rows)
        lines.append("```")
        return "\n".join(lines)
    except Exception as _e:
        _log.debug("[vcp] weekly context error %s: %s", symbol, _e)
        return ""


def _build_sonnet_prompt(symbol: str, df: pd.DataFrame, quant: dict, last_candle: str, fundamentals: dict | None = None, headlines: str | None = None) -> str:
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

    # P3-fix: normalize volume as ×avg so Claude sees relative volume directly
    # (raw integers like 3,482,000 give Claude no context about what's heavy or light)
    _vol_mean = float(volume.mean()) if float(volume.mean()) > 0 else 1.0
    rows = [
        f"{dates[i]}  H={high.iloc[i]:.2f}  L={low.iloc[i]:.2f}  "
        f"C={close.iloc[i]:.2f}  V={volume.iloc[i]/_vol_mean:.2f}x"
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

    # P4.4: reuse headlines cached upstream to avoid double yfinance/web fetch
    news_headlines = headlines if headlines is not None else _get_recent_news(symbol, n=4)
    news_ctx = f"\n\n## Recent News (catalyst check)\n{news_headlines}"
    weekly_ctx = _get_weekly_context(symbol, df)

    n_handles = quant.get("n_handles", 1)
    handle_ctx = (
        f"\n- MULTI-HANDLE BASE: {n_handles} corrective cycles detected — "
        "each handle tighter than previous (highest quality VCP signal)"
        if n_handles >= 3 else
        f"\n- TWO-HANDLE BASE: {n_handles} corrective cycles — solid structure"
        if n_handles == 2 else ""
    )

    return f"""You are executing Mark Minervini's VCP analysis protocol on {symbol}.

## Price Data (last {len(recent)} trading days, vol avg={vol_avg:,})
```
Date        High    Low     Close   Vol(×avg)
{price_table}
```

## Context
- MA50=${ma50:.2f} | MA200=${ma200:.2f} | Current=${float(close.iloc[-1]):.2f}
- Quant segment ranges (oldest→newest): {seg_str}
- Depth from pattern high: {quant.get('pattern_depth_pct',0):.1%}
- Handle range (last 10 bars): {quant.get('tight_rng_pct',0):.1%}{handle_ctx}{vol_ctx}{candle_ctx}{news_ctx}{weekly_ctx}

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

**Step 7 — Pattern classification** (VCP vs Cup-with-Handle):
Also check for Cup-with-Handle (CwH) shape:
- CwH: U-shaped base lasting 7–65 weeks, cup depth 15–33%,
  handle forms in upper half of cup, handle depth <15%, duration ≤5 weeks.
- VCP = progressively tighter contracting swings within a consolidation.
- These overlap: a VCP within a CwH handle is the strongest possible setup.
Assign pattern_type:
  "vcp"  — contracting VCP swings dominate; no clear U-cup shape
  "cwh"  — distinct U-cup with handle; handle lacks clear VCP contractions
  "both" — handle shows VCP contractions inside a cup-with-handle base (ideal)

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
  "measured_move_pct": <full base height (from pattern low to pattern high) divided by current price, as decimal e.g. 0.22>,
  "pattern_type": "vcp|cwh|both"
}}"""


def _call_sonnet(symbol: str, prompt: str) -> dict:
    try:
        resp = _get_client().messages.create(
            model=_SONNET_MODEL,
            max_tokens=1000,
            system=[{"type": "text", "text": "Respond with raw JSON only. No prose before or after the JSON object.",
                      "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        _log_tokens(symbol, "tier2_sonnet", _SONNET_MODEL,
                    resp.usage.input_tokens, resp.usage.output_tokens)
        # P4.2: robust fence extraction via regex
        _m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if _m:
            raw = _m.group(1).strip()
        elif raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except anthropic.APIError as e:
        _log.warning("[vcp] %s Sonnet API error: %s", symbol, e)
        return {"_api_error": True}
    except (json.JSONDecodeError, ValueError) as e:
        _log.warning("[vcp] %s Sonnet parse error: %s", symbol, e)
        return {}
    except Exception as e:
        _log.warning("[vcp] %s Sonnet unexpected error: %s", symbol, e)
        return {"_api_error": True}


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

    _bl = sonnet.get("breakout_level", 0)
    _sl_v = sonnet.get("stop_loss", 0)
    _bl_disp = f"${_bl:.2f}" if _bl else "N/A"
    _sl_disp = f"${_sl_v:.2f}" if _sl_v else "N/A"

    return f"""You are a strict senior VCP analyst trained by Mark Minervini. A junior analyst (Sonnet) flagged {symbol} as a potential VCP setup. Your job: independently verify the pattern and give an unbiased verdict — trade, watch, or skip. Apply the same strict Minervini criteria regardless of the junior's assessment.

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
3. Is the proposed breakout level ({_bl_disp}) the correct pivot high — or is there a better level?
4. Is the stop loss ({_sl_disp}) logical — just below the handle low, not too wide?
5. Weigh risk/reward: entry {_bl_disp} stop {_sl_disp}. Is the R:R at least 2:1 given the measured move? Be precise.
6. Use "skip" freely when the pattern is marginal — a missed trade is better than a bad entry. Use "watch" when setup needs 1-2 more days of confirmation. Use "trade" only for high-conviction setups.

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
            system=[{"type": "text", "text": "Respond with raw JSON only. No prose before or after the JSON object.",
                      "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        _log_tokens(symbol, "tier3_opus", _OPUS_MODEL,
                    resp.usage.input_tokens, resp.usage.output_tokens)
        # P4.2: robust fence extraction via regex
        _m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if _m:
            raw = _m.group(1).strip()
        elif raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except anthropic.APIError as e:
        _log.warning("[vcp] %s Opus API error: %s", symbol, e)
        return {"_api_error": True}
    except (json.JSONDecodeError, ValueError) as e:
        _log.warning("[vcp] %s Opus parse error: %s", symbol, e)
        return {}
    except Exception as e:
        _log.warning("[vcp] %s Opus unexpected error: %s", symbol, e)
        return {"_api_error": True}


# ── Public API ────────────────────────────────────────────────────────────────

def _get_recent_news(symbol: str, n: int = 5) -> str:
    """Return up to n recent news headlines via yfinance for catalyst check."""
    try:
        t = yf.Ticker(symbol)
        news = t.news or []
        headlines = []
        for item in news[:n]:
            title = item.get("content", {}).get("title") or item.get("title", "")
            if title:
                headlines.append(f"• {title}")
        return "\n".join(headlines) if headlines else "No recent news available."
    except Exception:
        return "News unavailable."


def analyze(symbol: str, df: pd.DataFrame, last_candle: str = "neutral", fundamentals: dict | None = None) -> VCPResult:
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

    # -- Cache: skip Claude if same breakout setup seen today ---------------
    _vcp_cache = _load_vcp_cache()
    _cache_key = f"{symbol}:{date.today().isoformat()}"
    if _cache_key in _vcp_cache:
        _cached = _vcp_cache[_cache_key]
        _cached_lvl = float(_cached.get("breakout_level", 0.0))
        if _cached_lvl > 0 and abs(breakout_lvl / _cached_lvl - 1) < 0.02:
            _log.info("[vcp] %s CACHE HIT -- skipping Claude (breakout=%.2f)",
                      symbol, _cached_lvl)
            try:
                return _deserialize_result(_cached)
            except Exception as _ce:
                _log.debug("[vcp] cache deserialize error %s: %s", symbol, _ce)

    # ── News sentiment pre-filter ────────────────────────────────────────────
    # Haiku reads 3 recent headlines — negative news raises min_conf threshold,
    # positive news is stored in result for broker report badge.
    _news_conf_penalty = 0.0
    _news_positive     = False
    _news_negative     = False
    try:
        _headlines = _get_recent_news(symbol, 4)  # P4.4: fetch n=4, reuse in Sonnet prompt
        if _headlines and "unavailable" not in _headlines.lower() and "No recent" not in _headlines:
            _ns_prompt = (
                f"Company ticker: {symbol}\nRecent news headlines:\n{_headlines}\n\n"
                f"Rate the OVERALL sentiment of these headlines for a swing trader. "
                f'Respond ONLY with JSON: {{"sentiment": "positive" or "neutral" or "negative", '
                f'"reason": "<8 words>"}}'
            )
            _ns_resp = _get_client().messages.create(
                model=_HAIKU_MODEL, max_tokens=60,
                system=[{"type": "text", "text": "Respond with raw JSON only, no markdown fences.",
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": _ns_prompt}],
            )
            # P4.3: log tokens for news-sentiment call (was bypassing _log_tokens)
            _log_tokens(symbol, "tier0_news", _HAIKU_MODEL,
                        _ns_resp.usage.input_tokens, _ns_resp.usage.output_tokens)
            _ns_raw = _ns_resp.content[0].text.strip()
            # P4.2: robust fence extraction
            _ns_m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', _ns_raw, re.DOTALL)
            if _ns_m:
                _ns_raw = _ns_m.group(1).strip()
            elif _ns_raw.startswith("```"):
                _ns_raw = _ns_raw.split("```")[1].lstrip("json").strip()
            try:
                _ns = json.loads(_ns_raw)
                _ns_sent = _ns.get("sentiment", "neutral")
                if _ns_sent == "negative":
                    _news_conf_penalty = 0.10
                    _news_negative     = True
                    _log.info("[vcp] %s news NEGATIVE (%s) → min_conf +0.10",
                              symbol, _ns.get("reason", "")[:50])
                elif _ns_sent == "positive":
                    _news_positive = True
                    _log.info("[vcp] %s news POSITIVE (%s)", symbol, _ns.get("reason", "")[:50])
            except json.JSONDecodeError:
                _news_conf_penalty = 0.05
                _log.debug("[vcp] %s news sentiment JSON parse failed — applying 0.05 penalty", symbol)
    except Exception:
        _log.debug("[vcp] %s news sentiment check suppressed", symbol, exc_info=True)

    # ── Tier 1: Haiku pre-screen ─────────────────────────────────────────────
    h_prompt = _build_haiku_prompt(symbol, df, quant, last_candle)
    h_data   = _call_haiku(symbol, h_prompt)

    # API errors are NOT VCP rejections — skip Haiku gate and fall through to Sonnet
    if h_data.get("_api_error"):
        _log.warning("[vcp] %s Haiku API error — falling through to Sonnet", symbol)
        h_is_vcp = True
        h_score  = 5
    else:
        h_score  = int(h_data.get("vcp_score", 0))
        h_is_vcp = bool(h_data.get("is_vcp", False))

    _log.info("[vcp] %s Haiku score=%d is_vcp=%s | %s",
              symbol, h_score, h_is_vcp, h_data.get("reason", "")[:80])

    _HAIKU_MIN_SCORE = 5  # require >= 5/10 confidence before spending on Sonnet
    if not h_is_vcp or h_score < _HAIKU_MIN_SCORE:
        _rej_tag = f"haiku_low_score={h_score}" if h_is_vcp else f"haiku_is_vcp=False_score={h_score}"
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            breakout_level=breakout_lvl, stop_loss=stop_cand,
            pattern_depth_pct=quant.get("pattern_depth_pct", 0),
            contractions=quant.get("contractions", 0),
            breakout_volume=breakout_vol, last_candle=last_candle,
            fail_reason=f"{_rej_tag} ({h_data.get('reason','')[:60]})",
            ai_verdict="haiku_rejected",
            tier_used="haiku_rejected",
        )

    # ── Tier 2: Sonnet deep analysis ─────────────────────────────────────────
    _log.info("[vcp] %s → Sonnet tier-2 (Haiku score=%d/10)", symbol, h_score)
    s_prompt = _build_sonnet_prompt(symbol, df, quant, last_candle, fundamentals, headlines=_headlines)
    s_data   = _call_sonnet(symbol, s_prompt)

    if s_data.get("_api_error"):
        return VCPResult(symbol=symbol, passed=False, current_price=price,
                         fail_reason="sonnet_api_error", last_candle=last_candle,
                         tier_used="sonnet_error")
    if not s_data:
        return VCPResult(symbol=symbol, passed=False, current_price=price,
                         fail_reason="sonnet_parse_error", last_candle=last_candle,
                         tier_used="sonnet_error")

    confirmed   = bool(s_data.get("vcp_confirmed", False))
    # Clamp values to valid ranges — Claude may return out-of-range numbers
    confidence  = max(0.0, min(1.0, float(s_data.get("confidence", 0) or 0)))
    breakout    = float(s_data.get("breakout_level") or s_data.get("pivot_high") or breakout_lvl)
    stop_loss   = float(s_data.get("stop_loss") or s_data.get("pivot_low") or stop_cand)
    depth       = quant.get("pattern_depth_pct", 0)
    contractions = int(s_data.get("contractions_identified") or quant.get("contractions", 0))
    tight_pct   = float(s_data.get("tight_area_pct") or quant.get("tight_rng_pct", 0))
    _raw_quality = int(s_data.get("quality_score", 0) or 0)
    if _raw_quality == 0:
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
            fail_reason="sonnet_quality_zero",
            ai_verdict="sonnet_rejected",
            ai_reasoning=s_data.get("pattern_notes", ""),
            breakout_volume=breakout_vol, last_candle=last_candle,
            quality_score=0, tier_used="sonnet",
        )
    quality     = max(1, min(5, _raw_quality))

    # Validate breakout and stop: must be positive and correctly ordered
    if breakout <= 0 or stop_loss <= 0:
        _log.warning("[vcp] %s invalid levels from Sonnet: breakout=%.2f SL=%.2f — "
                     "falling back to quant levels", symbol, breakout, stop_loss)
        breakout  = breakout_lvl if breakout <= 0 else breakout
        stop_loss = stop_cand    if stop_loss <= 0 else stop_loss
    if breakout <= stop_loss:
        _log.warning("[vcp] %s inverted R/R from Sonnet: breakout=%.2f <= SL=%.2f — rejecting",
                     symbol, breakout, stop_loss)
        return VCPResult(symbol=symbol, passed=False, current_price=price,
                         fail_reason="sonnet_inverted_rr", last_candle=last_candle,
                         tier_used="sonnet_error")
    # Multi-handle bonus: 3+ handles = highest-conviction base, cap quality at 5
    _n_hdl = quant.get("n_handles", 1)
    if _n_hdl >= 3:
        quality = min(5, quality + 1)
        _log.info("[vcp] %s multi-handle base (%d handles) → quality boosted to %d",
                  symbol, _n_hdl, quality)
    elif _n_hdl == 2 and quality < 5:
        quality = min(5, quality + 0)  # two-handle: no boost but already factored in prompt
    # Adaptive min_confidence: tighten in bear regime, loosen in bull
    _REGIME_CONF = {"bull": 0.60, "neutral": 0.65, "bear": 0.75}
    min_conf = cfg.get("min_confidence", 0.65)
    try:
        _rs_path = LOG_DIR / "risk_state.json"
        if _rs_path.exists():
            _rs_json = json.loads(_rs_path.read_text())
            _regime  = _rs_json.get("confirmed_regime", "neutral")
            min_conf = _REGIME_CONF.get(_regime, min_conf)
            _log.debug("[vcp] %s adaptive min_conf=%.2f (regime=%s)", symbol, min_conf, _regime)
    except Exception:
        pass
    # News sentiment penalty: negative headlines raise effective threshold
    min_conf = min(0.95, min_conf + _news_conf_penalty)
    if _news_conf_penalty > 0:
        _log.info("[vcp] %s effective min_conf=%.2f (news penalty +%.2f)",
                  symbol, min_conf, _news_conf_penalty)
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

    # ── Tier 3: Opus final validation for elite setups ────────────────────────
    # quality >= 4: worth spending ~$0.03 to validate before betting real money
    tier_label = "sonnet"
    _opus_watch = False
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
                    confidence=float(o_data.get("confidence") or confidence),
                    breakout_level=float(o_data.get("breakout_level") or breakout),
                    stop_loss=float(o_data.get("stop_loss") or stop_loss),
                    fail_reason=f"opus_veto: {o_data.get('risk_factors','')[:80]}",
                    ai_verdict="opus_vetoed",
                    ai_reasoning=o_data.get("pattern_notes", ""),
                    breakout_volume=breakout_vol, last_candle=last_candle,
                    quality_score=int(o_data.get("quality_score") or quality),
                    tier_used="opus",
                )
            # Refine levels with Opus precision (clamp to valid ranges)
            breakout   = float(o_data.get("breakout_level") or breakout)
            stop_loss  = float(o_data.get("stop_loss") or stop_loss)
            confidence = max(0.0, min(1.0, float(o_data.get("confidence", confidence) or confidence)))
            quality    = max(1, min(5, int(o_data.get("quality_score", quality) or quality)))
            tier_label = "opus"
            s_data = o_data   # use Opus notes for logging
            # "watch" = borderline setup — allow through but cap confidence at 0.70
            # so position sizing stays conservative
            if verdict == "watch":
                confidence *= 0.85  # proportional discount; preserves relative nuance
                _log.info("[vcp] %s Opus verdict=watch — confidence discounted 15%% to %.0f%%", symbol, confidence * 100)
            _opus_watch = (verdict == "watch")

    stop_loss = _atr_adjusted_stop(df, breakout, stop_loss)
    vol_multiweek = quant.get("vol_at_multiweek_low", False)

    if quality < min_quality:
        return VCPResult(
            symbol=symbol, passed=False, current_price=price,
            confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
            fail_reason=f"low_quality_q={quality}<{min_quality}",
            ai_verdict="sonnet_low_quality",
            ai_reasoning=s_data.get("pattern_notes", ""),
            breakout_volume=breakout_vol, last_candle=last_candle,
            quality_score=quality, tier_used=tier_label,
        )

    passed = confirmed and confidence >= min_conf and breakout > stop_loss

    result = VCPResult(
        symbol=symbol, passed=passed, current_price=price,
        confidence=confidence, breakout_level=breakout, stop_loss=stop_loss,
        pattern_depth_pct=depth, contractions=contractions, tight_pct=tight_pct,
        ai_verdict=("opus_watch" if _opus_watch and passed else "confirmed" if passed else "rejected"),
        ai_reasoning=(s_data.get("pattern_notes", "") + " | " +
                      s_data.get("risk_factors", "")),
        fail_reason="" if passed else f"rejected_conf={confidence:.0%}",
        breakout_volume=breakout_vol, last_candle=last_candle,
        tier_used=tier_label,
        quality_score=quality,
        vol_at_multiweek_low=vol_multiweek,
        measured_move_pct=float(s_data.get("measured_move_pct", 0.0) or 0.0),
        pattern_type=str(s_data.get("pattern_type", "vcp") or "vcp"),
        news_positive=_news_positive,
        news_negative=_news_negative,
    )

    if passed:
        vol_tag = " [BREAKOUT VOL✓]" if breakout_vol else ""
        _log.info("[vcp] ✓ %s | conf=%.0f%% | entry=$%.2f SL=$%.2f Q%d/5 candle=%s%s [%s]",
                  symbol, confidence * 100, breakout, stop_loss, quality,
                  last_candle, vol_tag, tier_label)
    # Cache result so same symbol skips Claude re-analysis tonight
    try:
        _cd = _serialize_result(result)
        _cd["date"] = str(date.today())
        _vcp_cache[_cache_key] = _cd
        _save_vcp_cache(_vcp_cache)
    except Exception as _se:
        _log.debug("[vcp] cache save failed %s: %s", symbol, _se)

    return result


def batch_analyze(trend_passed: list, max_symbols: int = 50,
                  tick_fn=None) -> list[VCPResult]:
    """Analyze TrendResult objects for VCP. Tier 0→1→2→3 per stock.

    tick_fn: optional callable invoked every 5 candidates — used by main.py
    to write a watchdog heartbeat during long Claude API batches.

    P2-fix: uses ThreadPoolExecutor(6) for parallel Haiku calls — up to 6×
    faster than the old sequential loop with time.sleep(0.3). Sonnet/Opus
    escalation happens inside analyze() so concurrency is naturally limited
    by how many symbols pass Haiku (typically <30%).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    results    = []
    candidates = [r for r in trend_passed if r.passed and r.df is not None][:max_symbols]
    _log.info("[vcp] Analyzing %d trend-passed stocks in parallel (Haiku→Sonnet→Opus)...",
              len(candidates))

    def _analyze_one(trend):
        last_candle = getattr(trend, "last_candle", "neutral")
        fund = {"eps_growth": getattr(trend, "eps_growth", None),
                "revenue_growth": getattr(trend, "revenue_growth", None)}
        result = analyze(trend.symbol, trend.df, last_candle=last_candle, fundamentals=fund)
        result.rs_rating       = getattr(trend, "rs_rating", 0.0)
        result.rs_line_at_high = getattr(trend, "rs_line_at_high", False)
        return result

    completed = 0
    with ThreadPoolExecutor(max_workers=6) as _pool:
        future_to_sym = {_pool.submit(_analyze_one, t): t.symbol for t in candidates}
        for fut in _as_completed(future_to_sym):
            sym = future_to_sym[fut]
            try:
                result = fut.result()
                results.append(result)
                completed += 1
                _log.info("[vcp] %d/%d done: %s (%s)", completed, len(candidates),
                          sym, result.tier_used)
                if tick_fn and completed % 5 == 0:
                    try:
                        tick_fn()
                    except Exception:
                        pass
            except Exception as _exc:
                _log.warning("[vcp] %s analysis failed: %s", sym, _exc)

    flush_token_log()
    passed    = [r for r in results if r.passed]
    haiku_rej = sum(1 for r in results if "haiku" in r.tier_used)
    sonnet_n  = sum(1 for r in results if r.tier_used == "sonnet")
    opus_n    = sum(1 for r in results if r.tier_used == "opus")
    _log.info("[vcp] Done: %d/%d passed | Haiku rejected: %d | Sonnet: %d | Opus: %d",
              len(passed), len(results), haiku_rej, sonnet_n, opus_n)
    return results
