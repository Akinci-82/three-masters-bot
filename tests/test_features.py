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
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, time as dt_time

sys.path.insert(0, '/home/habil/three-masters-bot')

import pandas as pd
import numpy as np


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
        def fake_stop(sym, qty, price):
            calls.append(("stop", sym, qty, price))
            return True
        def fake_trailing(sym, qty, pct):
            calls.append(("trailing", sym, qty, pct))
            return True
        def fake_cancel(*a, **kw):
            pass

        with patch("position_monitor._get_positions", return_value=fake_position), \
             patch("position_monitor._market_is_open", return_value=True), \
             patch("position_monitor._get_open_orders", return_value=[]), \
             patch("position_monitor._cancel_stop_orders", side_effect=fake_cancel), \
             patch("position_monitor._place_market_sell", side_effect=fake_market_sell), \
             patch("position_monitor._place_stop", side_effect=fake_stop), \
             patch("position_monitor._place_trailing_stop", side_effect=fake_trailing):
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
        self.assertTrue(len(sell_calls) >= 1, "Should sell 50% at +15%")
        # Should sell ~50 shares (50% of 100)
        self.assertEqual(sell_calls[0][2], 50)
        self.assertTrue(state["TST"]["partial_done"])

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

        def fake_analyze(symbol, df, use_deep_model=False, last_candle="neutral"):
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

if __name__ == "__main__":
    print("=" * 70)
    print("  Three Masters Bot — Feature Test Suite")
    print("  Tests: breakout volume, candlestick, position monitor, scheduler")
    print("=" * 70)
    unittest.main(verbosity=2)
