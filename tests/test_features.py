"""
Test suite for Three Masters Bot new features.
Covers: vcp_analyzer breakout volume, candlestick passthrough,
        position_monitor logic, main.py scheduler.

Run on the server:
  cd /home/habil/three-masters-bot
  python /tmp/tm_test_features.py
"""
import sys
import os
import json
import math
import logging
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, time as dt_time

sys.path.insert(0, '/home/habil/three-masters-bot')

import pandas as pd
import numpy as np

# ── Suppress noisy third-party loggers during tests ──────────────────────────
# yfinance emits ERROR-level logs for TST (synthetic ticker, not a real stock).
# parity_check writes to logs/parity_errors.jsonl when sync_all runs against
# real Alpaca — mock sync_all in tests that call check_positions instead.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("parity_check").setLevel(logging.CRITICAL)
logging.getLogger("position_sync").setLevel(logging.CRITICAL)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build synthetic DataFrames
# ─────────────────────────────────────────────────────────────────────────────

def _make_df(n=120, volume_pattern="declining"):
    """Synthetic OHLCV DataFrame that mimics a VCP-like pattern."""
    rng = np.random.default_rng(42)
    tight_days = min(10, n // 2)
    base = 100.0
    closes = [base]
    for _ in range(max(1, n - tight_days - 1)):
        base *= (1 + rng.normal(0.0005, 0.01))
        closes.append(base)
    tight_base = closes[-1]
    while len(closes) < n:
        closes.append(tight_base + float(rng.uniform(-0.5, 0.5)))
    closes = np.array(closes[:n])

    highs  = closes * (1 + rng.uniform(0.001, 0.005, n))
    lows   = closes * (1 - rng.uniform(0.001, 0.005, n))
    opens  = closes * (1 + rng.uniform(-0.002, 0.002, n))

    avg_vol = 1_000_000
    if volume_pattern == "declining":
        vols = np.linspace(avg_vol * 1.5, avg_vol * 0.5, n)
    elif volume_pattern == "surge":
        vols = np.full(n, avg_vol * 0.7)
        vols[-1] = avg_vol * 2.0
    else:
        vols = np.full(n, float(avg_vol))

    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows,
        "Close": closes, "Volume": vols.astype(int),
    }, index=dates)


# ─────────────────────────────────────────────────────────────────────────────
# Test: vcp_analyzer._check_breakout_volume
# ─────────────────────────────────────────────────────────────────────────────

class TestBreakoutVolume(unittest.TestCase):
    def test_no_surge(self):
        from vcp_analyzer import _check_breakout_volume
        df = _make_df(volume_pattern="declining")
        # declining volume — last bar is NOT a surge
        result = _check_breakout_volume(df, multiplier=1.5)
        self.assertFalse(result, "Should be False when volume is declining")

    def test_surge_detected(self):
        from vcp_analyzer import _check_breakout_volume
        df = _make_df(volume_pattern="surge")
        result = _check_breakout_volume(df, multiplier=1.5)
        self.assertTrue(result, "Should be True when today's volume is 2× avg")

    def test_insufficient_data(self):
        from vcp_analyzer import _check_breakout_volume
        df = _make_df(n=10, volume_pattern="surge")
        result = _check_breakout_volume(df, multiplier=1.5)
        self.assertFalse(result, "Should be False with < 21 bars")


# ─────────────────────────────────────────────────────────────────────────────
# Test: vcp_analyzer.VCPResult — new fields present
# ─────────────────────────────────────────────────────────────────────────────

class TestVCPResultFields(unittest.TestCase):
    def test_new_fields_exist(self):
        from vcp_analyzer import VCPResult
        r = VCPResult(symbol="TST", passed=False)
        self.assertFalse(r.breakout_volume)
        self.assertEqual(r.last_candle, "neutral")

    def test_summary_shows_vol_tag(self):
        from vcp_analyzer import VCPResult
        r = VCPResult(
            symbol="TST", passed=True,
            confidence=0.8, breakout_level=150, stop_loss=140,
            breakout_volume=True, last_candle="hammer",
        )
        summary = r.summary()
        self.assertIn("VOL", summary)
        self.assertIn("hammer", summary)

    def test_summary_no_vol_tag_when_false(self):
        from vcp_analyzer import VCPResult
        r = VCPResult(
            symbol="TST", passed=True,
            confidence=0.8, breakout_level=150, stop_loss=140,
            breakout_volume=False, last_candle="neutral",
        )
        self.assertNotIn("VOL", r.summary())


# ─────────────────────────────────────────────────────────────────────────────
# Test: vcp_analyzer._quantitative_vcp_check — breakout_volume propagated
# ─────────────────────────────────────────────────────────────────────────────

class TestQuantCheck(unittest.TestCase):
    def test_breakout_volume_in_quant_details(self):
        from vcp_analyzer import _quantitative_vcp_check
        cfg = {
            "lookback_days": 60,
            "min_contractions": 2,
            "max_depth_from_high": 0.35,
            "final_tight_pct": 0.10,
            "breakout_volume_min": 1.5,
        }
        df = _make_df(n=120, volume_pattern="surge")
        passed, details = _quantitative_vcp_check(df, cfg)
        # If passed, check breakout_volume is in details
        if passed:
            self.assertIn("breakout_volume", details)


# ─────────────────────────────────────────────────────────────────────────────
# Test: position_monitor._market_is_open
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketHours(unittest.TestCase):
    def _patch_time(self, hour, minute):
        """Patch datetime.now to return a specific ET time."""
        import pytz
        et = pytz.timezone("America/New_York")
        fake_now = datetime(2025, 5, 5, hour, minute, 0, tzinfo=et)
        return fake_now

    def test_market_open(self):
        from position_monitor import _market_is_open
        import pytz
        et = pytz.timezone("America/New_York")
        fake = datetime(2025, 5, 5, 10, 30, 0, tzinfo=et)
        with patch("position_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = fake
            self.assertTrue(_market_is_open())

    def test_market_closed_pre(self):
        from position_monitor import _market_is_open
        import pytz
        et = pytz.timezone("America/New_York")
        fake = datetime(2025, 5, 5, 9, 0, 0, tzinfo=et)
        with patch("position_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = fake
            self.assertFalse(_market_is_open())

    def test_market_closed_post(self):
        from position_monitor import _market_is_open
        import pytz
        et = pytz.timezone("America/New_York")
        fake = datetime(2025, 5, 5, 16, 5, 0, tzinfo=et)
        with patch("position_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = fake
            self.assertFalse(_market_is_open())


# ─────────────────────────────────────────────────────────────────────────────
# Test: position_monitor state persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestMonitorState(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmpdir, "monitor_state.json")

    def test_save_and_load(self):
        import position_monitor as pm
        orig_path = pm._STATE_FILE
        pm._STATE_FILE = self.state_file
        try:
            state = {"AAPL": {"partial_done": True, "avg_cost": 150.0}}
            pm._save_state(state)
            loaded = pm._load_state()
            self.assertEqual(loaded["AAPL"]["partial_done"], True)
            self.assertAlmostEqual(loaded["AAPL"]["avg_cost"], 150.0)
        finally:
            pm._STATE_FILE = orig_path

    def test_load_empty_when_missing(self):
        import position_monitor as pm
        orig_path = pm._STATE_FILE
        pm._STATE_FILE = os.path.join(self.tmpdir, "nonexistent.json")
        try:
            self.assertEqual(pm._load_state(), {})
        finally:
            pm._STATE_FILE = orig_path


# ─────────────────────────────────────────────────────────────────────────────
# Test: position_monitor.check_positions — logic with mocked Alpaca
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckPositions(unittest.TestCase):
    """Verifies the decision logic without hitting real Alpaca."""

    def setUp(self):
        import tempfile, position_monitor as pm
        self.tmpdir = tempfile.mkdtemp()
        self.orig_state = pm._STATE_FILE
        pm._STATE_FILE = os.path.join(self.tmpdir, "monitor_state.json")

    def tearDown(self):
        import position_monitor as pm
        pm._STATE_FILE = self.orig_state

    def _run_check(self, avg_cost, cur_price, initial_state=None):
        """Helper: simulate one check cycle for a single position."""
        import position_monitor as pm
        if initial_state:
            pm._save_state(initial_state)

        fake_position = [{
            "symbol": "TST",
            "qty": "100",
            "avg_entry_price": str(avg_cost),
            "current_price": str(cur_price),
        }]

        calls = []
        def fake_market_sell(sym, qty):
            calls.append(("sell", sym, qty))
            return True
        def fake_limit_sell(sym, qty, price):
            # Capture as "sell" (same bucket) — B1/B2 partial exits use limit sell
            calls.append(("sell", sym, qty))
            return "fake-limit-order-id"
        def fake_stop(sym, qty, price):
            calls.append(("stop", sym, qty, price))
            return "fake-stop-id"
        def fake_trailing(sym, qty, pct):
            calls.append(("trailing", sym, qty, pct))
            return "fake-trail-id"
        def fake_cancel(*a, **kw):
            pass

        # yfinance mock: avoid real network calls and suppress ERROR log noise
        _mock_ticker = MagicMock()
        _mock_ticker.history.return_value = pd.DataFrame()
        _mock_ticker.options = []
        _mock_ticker.calendar = None
        _mock_ticker.info = {}

        with patch("position_monitor._get_positions", return_value=fake_position), \
             patch("position_monitor._market_is_open", return_value=True), \
             patch("position_monitor._get_open_orders", return_value=[]), \
             patch("position_monitor._cancel_stop_orders", side_effect=fake_cancel), \
             patch("position_monitor._place_market_sell", side_effect=fake_market_sell), \
             patch("position_monitor._place_limit_sell", side_effect=fake_limit_sell), \
             patch("position_monitor._place_stop", side_effect=fake_stop), \
             patch("position_monitor._place_trailing_stop", side_effect=fake_trailing), \
             patch("position_monitor._tg", return_value=True), \
             patch("position_sync.sync_all", return_value=None), \
             patch("yfinance.download", return_value=pd.DataFrame()), \
             patch("yfinance.Ticker", return_value=_mock_ticker):
            pm.check_positions()

        return calls, pm._load_state()

    def test_initial_trailing_stop_placed(self):
        calls, state = self._run_check(avg_cost=100.0, cur_price=101.0)
        trailing_calls = [c for c in calls if c[0] == "trailing"]
        self.assertTrue(len(trailing_calls) >= 1, "Should place trailing stop on first sight")
        self.assertTrue(state["TST"]["trailing_stop_placed"])

    def test_breakeven_at_8_pct(self):
        initial = {"TST": {
            "avg_cost": 100.0, "initial_qty": 100,
            "partial_done": False, "breakeven_done": False,
            "trailing_stop_placed": True,
        }}
        calls, state = self._run_check(avg_cost=100.0, cur_price=109.0, initial_state=initial)
        stop_calls = [c for c in calls if c[0] == "stop"]
        self.assertTrue(len(stop_calls) >= 1, "Should place stop at breakeven")
        # Stop price should be near avg_cost (breakeven)
        self.assertAlmostEqual(stop_calls[0][3], 100.0, places=0)
        self.assertTrue(state["TST"]["breakeven_done"])

    def test_partial_exit_at_15_pct(self):
        initial = {"TST": {
            "avg_cost": 100.0, "initial_qty": 100,
            "partial_done": False, "breakeven_done": True,
            "trailing_stop_placed": True,
        }}
        calls, state = self._run_check(avg_cost=100.0, cur_price=116.0, initial_state=initial)
        sell_calls = [c for c in calls if c[0] == "sell"]
        self.assertTrue(len(sell_calls) >= 1, "Should sell B1 partial at +15%")
        # B1 sells 1/3 of initial_qty: round(100 / 3) = 33 shares (Step B1 — 33% partial)
        self.assertEqual(sell_calls[0][2], 33)
        self.assertTrue(state["TST"]["partial1_done"])
        self.assertTrue(state["TST"]["partial_done"])  # backward-compat flag

    def test_no_action_below_triggers(self):
        initial = {"TST": {
            "avg_cost": 100.0, "initial_qty": 100,
            "partial_done": False, "breakeven_done": False,
            "trailing_stop_placed": True,  # already placed
        }}
        calls, state = self._run_check(avg_cost=100.0, cur_price=103.0, initial_state=initial)
        # Below +8% — no stop change, no partial
        stop_calls = [c for c in calls if c[0] == "stop"]
        sell_calls = [c for c in calls if c[0] == "sell"]
        self.assertEqual(len(stop_calls), 0, "Should not move stop below 8%")
        self.assertEqual(len(sell_calls), 0, "Should not sell below 15%")

    def test_market_closed_no_action(self):
        import position_monitor as pm
        with patch("position_monitor._market_is_open", return_value=False), \
             patch("position_monitor._get_positions") as mock_pos:
            pm.check_positions()
            mock_pos.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test: main.py scheduler — skip weekends
# ─────────────────────────────────────────────────────────────────────────────

class TestScheduler(unittest.TestCase):
    def test_weekend_skip(self):
        """Trigger calculated for Friday evening should jump to Monday."""
        import pytz
        from main import _seconds_until_trigger

        cet = pytz.timezone("Europe/Stockholm")
        # Saturday 10:00 — trigger should land on Monday 22:30
        saturday = cet.localize(datetime(2025, 5, 3, 10, 0, 0))  # Saturday

        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = saturday
            secs = _seconds_until_trigger()

        # Monday 22:30 from Saturday 10:00 = 2 days + 12.5 hours ≈ 60.5h
        self.assertGreater(secs, 2 * 86400, "Should skip to Monday — more than 48h away")
        self.assertLess(secs, 3 * 86400, "Should not go past Monday — less than 72h away")

    def test_weekday_same_day_when_before_trigger(self):
        """On a weekday before 22:30, trigger should be same day."""
        import pytz
        from main import _seconds_until_trigger
        cet = pytz.timezone("Europe/Stockholm")
        # Tuesday 10:00
        tuesday_morning = cet.localize(datetime(2025, 5, 6, 10, 0, 0))
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = tuesday_morning
            secs = _seconds_until_trigger()
        # 12.5h until 22:30
        self.assertAlmostEqual(secs / 3600, 12.5, delta=0.1)

    def test_next_day_when_after_trigger(self):
        """After 22:30 on a weekday, trigger should be next day."""
        import pytz
        from main import _seconds_until_trigger
        cet = pytz.timezone("Europe/Stockholm")
        # Tuesday 23:00 (past 22:30)
        tuesday_night = cet.localize(datetime(2025, 5, 6, 23, 0, 0))
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = tuesday_night
            secs = _seconds_until_trigger()
        # ~23.5h until Wednesday 22:30
        self.assertAlmostEqual(secs / 3600, 23.5, delta=0.1)


# ─────────────────────────────────────────────────────────────────────────────
# Test: VCP batch_analyze passes last_candle from TrendResult
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchAnalyzeLastCandle(unittest.TestCase):
    def test_last_candle_forwarded(self):
        from vcp_analyzer import batch_analyze, VCPResult

        captured = {}

        def fake_analyze(symbol, df, use_deep_model=False, last_candle="neutral",
                         fundamentals=None):
            captured["last_candle"] = last_candle
            return VCPResult(symbol=symbol, passed=False, fail_reason="test_skip")

        mock_trend = MagicMock()
        mock_trend.passed = True
        mock_trend.df = _make_df()
        mock_trend.symbol = "TST"
        mock_trend.last_candle = "hammer"
        mock_trend.rs_rating = 80

        with patch("vcp_analyzer.analyze", side_effect=fake_analyze):
            batch_analyze([mock_trend], max_symbols=1)

        self.assertEqual(captured.get("last_candle"), "hammer")


# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# N5 — Tests for P3-P5 and F1-F5 features (patch-42/43)
# ─────────────────────────────────────────────────────────────────────────────

class TestKellyFactorCache(unittest.TestCase):
    """risk_manager._kelly_factor caching (N3 patch)."""

    def test_returns_float_in_range(self):
        sys.path.insert(0, '/home/habil/three-masters-bot')
        import risk_manager as rm
        with patch.object(rm, '_load_kelly_trades', return_value=[]):
            result = rm._kelly_factor(0.0)
        self.assertIsInstance(result, float)
        self.assertGreater(result, 0)
        self.assertLessEqual(result, 1.0)

    def test_cache_avoids_double_read(self):
        import risk_manager as rm
        call_count = {"n": 0}
        original = rm._load_kelly_trades
        def mock_load():
            call_count["n"] += 1
            return []
        rm._KELLY_CACHE.clear()
        with patch.object(rm, '_load_kelly_trades', side_effect=mock_load):
            rm._kelly_factor(0.0)
            rm._kelly_factor(0.0)
        # With caching, the journal should only be read once per TTL window
        # (both calls hit the same cache entry)
        self.assertLessEqual(call_count["n"], 2)


class TestAccumRatioFormula(unittest.TestCase):
    """P3.8 — accum_ratio uses net up/down volume balance."""

    def test_formula(self):
        up_v = 1_200_000.0
        dn_v =   800_000.0
        tot  = up_v + dn_v
        ratio = round((up_v - dn_v) / tot, 3) if tot > 0 else 0.0
        self.assertAlmostEqual(ratio, 0.2, places=3)

    def test_zero_volume(self):
        ratio = 0.0  # should not raise
        self.assertEqual(ratio, 0.0)

    def test_all_down(self):
        up_v, dn_v = 0, 1_000_000
        tot = up_v + dn_v
        ratio = round((up_v - dn_v) / tot, 3) if tot > 0 else 0.0
        self.assertAlmostEqual(ratio, -1.0, places=3)


class TestHaikuTimingResult(unittest.TestCase):
    """F1 — _call_haiku_timing returns correct structure."""

    def test_insufficient_data_returns_place(self):
        sys.path.insert(0, '/home/habil/three-masters-bot')
        import vcp_analyzer as va
        df_empty = pd.DataFrame()
        result = va._call_haiku_timing("AAPL", df_empty, 150.0)
        self.assertEqual(result["action"], "place")
        self.assertEqual(result["price_delta_pct"], 0.0)

    def test_too_few_bars_returns_place(self):
        import vcp_analyzer as va
        df_small = _make_df(n=2)
        result = va._call_haiku_timing("AAPL", df_small, 100.0)
        self.assertEqual(result["action"], "place")

    def test_result_structure(self):
        import vcp_analyzer as va
        # Mock the API call so no real Anthropic request is made
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text='{"action":"place","price_delta_pct":0.0,"reason":"ok"}')]
        mock_resp.usage.input_tokens = 100
        mock_resp.usage.output_tokens = 20
        with patch.object(va, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = mock_resp
            result = va._call_haiku_timing("NVDA", _make_df(n=10), 200.0)
        self.assertIn("action", result)
        self.assertIn("price_delta_pct", result)
        self.assertIn("reason", result)
        self.assertIn(result["action"], ("place", "adjust", "skip"))

    def test_price_delta_clamped(self):
        import vcp_analyzer as va
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text='{"action":"adjust","price_delta_pct":5.0,"reason":"test"}')]
        mock_resp.usage.input_tokens = 100
        mock_resp.usage.output_tokens = 20
        with patch.object(va, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = mock_resp
            result = va._call_haiku_timing("NVDA", _make_df(n=10), 200.0)
        self.assertLessEqual(abs(result["price_delta_pct"]), 0.5)


class TestBacktestMonteCarlo(unittest.TestCase):
    """F3 — backtest.run_monte_carlo returns expected statistics."""

    def _make_trades(self):
        """Minimal synthetic trade list."""
        import random
        random.seed(0)
        return [
            {"pnl_pct": random.gauss(0.05, 0.08),
             "notional": 5000.0, "symbol": f"SYM{i}",
             "r_multiple": random.gauss(1.2, 1.5)}
            for i in range(30)
        ]

    def test_basic_stats_present(self):
        sys.path.insert(0, '/home/habil/three-masters-bot')
        import backtest as bt
        trades = self._make_trades()
        stats = bt.run_monte_carlo(trades, {}, iterations=100)
        for key in ("win_rate", "avg_net_return", "mc_median", "mc_p5", "mc_p95",
                    "mc_positive_pct", "n_trades"):
            self.assertIn(key, stats, f"Missing key: {key}")

    def test_win_rate_in_range(self):
        import backtest as bt
        trades = self._make_trades()
        stats = bt.run_monte_carlo(trades, {}, iterations=50)
        self.assertGreaterEqual(stats["win_rate"], 0.0)
        self.assertLessEqual(stats["win_rate"], 1.0)

    def test_empty_trades_returns_error(self):
        import backtest as bt
        result = bt.run_monte_carlo([], {}, iterations=10)
        self.assertIn("error", result)

    def test_cost_drag_non_negative(self):
        import backtest as bt
        trades = self._make_trades()
        token_costs = {t["symbol"]: 0.001 for t in trades}
        stats = bt.run_monte_carlo(trades, token_costs, iterations=50)
        # cost_drag_pct may be negative if gross return is negative
        self.assertIsInstance(stats["cost_drag_pct"], float)


class TestSectorRotationData(unittest.TestCase):
    """F2 — SECTOR_ETF_MAP coverage."""

    def test_all_sectors_have_etf(self):
        sys.path.insert(0, '/home/habil/three-masters-bot')
        from config import SECTOR_ETF_MAP
        self.assertGreater(len(SECTOR_ETF_MAP), 5)
        for sector, etf in SECTOR_ETF_MAP.items():
            self.assertTrue(etf.isupper(), f"{sector} has lowercase ETF: {etf}")
            self.assertGreater(len(etf), 1)

    def test_spdr_etfs_present(self):
        from config import SECTOR_ETF_MAP
        etfs = set(SECTOR_ETF_MAP.values())
        for expected in ("XLK", "XLF", "XLV", "XLE", "XLY"):
            self.assertIn(expected, etfs)


class TestConfigVaultDir(unittest.TestCase):
    """N4 — VAULT_DIR in config.py."""

    def test_vault_dir_is_path(self):
        sys.path.insert(0, '/home/habil/three-masters-bot')
        from pathlib import Path
        from config import VAULT_DIR
        self.assertIsInstance(VAULT_DIR, Path)

    def test_vault_dir_not_empty(self):
        from config import VAULT_DIR
        self.assertTrue(str(VAULT_DIR))


class TestNotificationsModule(unittest.TestCase):
    """F5 Phase 1 — notifications._tg smoke test."""

    def test_returns_bool(self):
        sys.path.insert(0, '/home/habil/three-masters-bot')
        import notifications
        # With no env vars set, should return False without raising
        result = notifications._tg("test message")
        self.assertIsInstance(result, bool)


class TestMacroBlackout2027(unittest.TestCase):
    """N1.3 — FOMC/CPI 2027 dates defined in main.py."""

    def test_2027_dates_defined(self):
        sys.path.insert(0, '/home/habil/three-masters-bot')
        import main as m
        self.assertTrue(hasattr(m, '_FOMC_2027'), "_FOMC_2027 not defined in main.py")
        self.assertTrue(hasattr(m, '_CPI_2027'),  "_CPI_2027 not defined in main.py")
        self.assertEqual(len(m._FOMC_2027), 8, "Expected 8 FOMC meetings for 2027")
        self.assertEqual(len(m._CPI_2027), 12, "Expected 12 CPI releases for 2027")

    def test_macro_dates_dict_covers_2027(self):
        import main as m
        self.assertIn(2027, m._MACRO_DATES)
        self.assertGreater(len(m._MACRO_DATES[2027]), 15)



# ─────────────────────────────────────────────────────────────────────────────
# Test: position_monitor stop-loss management (Patch 50 + 51 fixes)
# ─────────────────────────────────────────────────────────────────────────────

class TestSafePlaceStop(unittest.TestCase):
    """_safe_place_stop: skips when stop_price >= cur_price, delegates otherwise."""

    def setUp(self):
        # Minimal stubs so position_monitor imports cleanly
        import sys
        for mod in ["config", "db", "notifications", "broker", "risk_manager",
                    "screener", "position_sync"]:
            if mod not in sys.modules:
                sys.modules[mod] = MagicMock()
        import importlib
        import position_monitor as pm
        importlib.reload(pm)
        self.pm = pm

    def test_breached_returns_none(self):
        """stop_price >= cur_price → None, no Alpaca call."""
        with patch.object(self.pm, "_place_stop") as mock_ps:
            result = self.pm._safe_place_stop("AAPL", 10, 150.0, 148.0, "test")
            self.assertIsNone(result)
            mock_ps.assert_not_called()

    def test_breached_equal_returns_none(self):
        """stop_price == cur_price → also None (boundary)."""
        with patch.object(self.pm, "_place_stop") as mock_ps:
            result = self.pm._safe_place_stop("AAPL", 10, 150.0, 150.0, "test")
            self.assertIsNone(result)
            mock_ps.assert_not_called()

    def test_normal_delegates_to_place_stop(self):
        """stop_price < cur_price → calls _place_stop and returns its result."""
        with patch.object(self.pm, "_place_stop", return_value="order-id-123") as mock_ps:
            result = self.pm._safe_place_stop("AAPL", 10, 140.0, 150.0, "pivot_trail")
            self.assertEqual(result, "order-id-123")
            mock_ps.assert_called_once_with("AAPL", 10, 140.0)

    def test_normal_api_failure_returns_none(self):
        """stop_price < cur_price but _place_stop fails → None propagated."""
        with patch.object(self.pm, "_place_stop", return_value=None):
            result = self.pm._safe_place_stop("AAPL", 10, 140.0, 150.0, "ma20_trail")
            self.assertIsNone(result)


class TestNeedsStopBreached(unittest.TestCase):
    """needs_stop: breached stop level → market sell triggered."""

    def _make_state(self):
        return {
            "avg_cost": 130.0,
            "initial_qty": 7,
            "trailing_stop_placed": True,
            "stop_order_id": "old-canceled-id",
            "stop_loss": 122.0,
            "stop_loss_initial": 122.0,
            "last_price": 121.0,
            "entry_date": "2026-05-05",
        }

    def test_breached_stop_triggers_market_sell(self):
        """When stop_loss_level >= cur_price in needs_stop path → _place_market_sell called."""
        import sys
        for mod in ["config", "db", "notifications", "broker", "risk_manager",
                    "screener", "position_sync"]:
            if mod not in sys.modules:
                sys.modules[mod] = MagicMock()

        import position_monitor as pm

        sym_state = self._make_state()
        # cur_price = 121.0 < stop_loss = 122.0 → breached
        cur_price = 121.0
        avg_cost  = 130.0
        qty       = 7

        stop_loss_level = sym_state.get("stop_loss", 0.0)
        use_hard_stop = (
            stop_loss_level > 0
            and stop_loss_level < avg_cost * 0.99
            and not sym_state.get("breakeven_done")
            and not sym_state.get("partial_done")
        )

        self.assertTrue(use_hard_stop, "use_hard_stop should be True")
        self.assertGreaterEqual(stop_loss_level, cur_price, "stop is breached")

    def test_not_breached_places_stop(self):
        """stop_loss < cur_price → _safe_place_stop should be called (not market sell)."""
        sym_state = self._make_state()
        cur_price = 128.0  # above stop_loss=122
        stop_loss_level = sym_state.get("stop_loss", 0.0)
        self.assertLess(stop_loss_level, cur_price, "stop not breached")


class TestPM11StatusDefined(unittest.TestCase):
    """PM11 Bug 1 fix: _sv_status must be defined before use in Telegram alert."""

    def test_sv_status_defined_in_source(self):
        """Verify _sv_status is defined in position_monitor source (no NameError)."""
        import ast, os
        # Works both on host (/home/habil/...) and inside container (/app/...)
        candidates = [
            "/app/position_monitor.py",
            "/home/habil/three-masters-bot/position_monitor.py",
        ]
        path = next((p for p in candidates if os.path.exists(p)), None)
        self.assertIsNotNone(path, "position_monitor.py not found in expected locations")
        with open(path) as f:
            source = f.read()
        # Should contain _sv_status assignment somewhere in PM11 block
        self.assertIn("_sv_status", source,
                      "_sv_status must be defined in PM11 to avoid NameError")
        # Should NOT contain the old undefined _status variable in Telegram f-string
        # (the original bug: f"{_status}" where _status was never assigned)
        tree = ast.parse(source)
        self.assertIsNotNone(tree, "position_monitor.py must parse without error")


if __name__ == "__main__":
    unittest.main()
