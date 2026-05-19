"""Integration tests for exit steps Z, A, B1, W, D.
Freezes current behavior as regression baseline before SQLite migration.
Run: python -m unittest tests/test_exit_steps.py -v
"""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import position_monitor as pm
from position_monitor import _step_d_time_stop

# Fixed far-future date returned by ALL datetime.now(_ET).strftime() calls in tests.
# Blocks all once-per-day guards (Keltner, PM11, VWAP, MA20, gap-harvest, stale).
_FIXED_DATE = "2099-01-01"


# ── Synthetic data helpers ─────────────────────────────────────────────────────

def _ohlcv(n=60, close=100.0, swing_low=None):
    """Flat OHLCV DataFrame. If swing_low set, bar at index -10 is a local swing low."""
    idx = pd.date_range(start="2025-01-01", periods=n, freq="B")
    lows = np.full(n, close * 0.99)
    if swing_low is not None:
        lows[-10] = float(swing_low)
        lows[-11] = float(swing_low) * 1.02
        lows[-9]  = float(swing_low) * 1.02
    return pd.DataFrame({
        "Open":   np.full(n, close * 0.999),
        "High":   np.full(n, close * 1.005),
        "Low":    lows,
        "Close":  np.full(n, close),
        "Volume": np.full(n, 1_000_000),
    }, index=idx)


def _weekly(below_ma=False, n=14):
    """Synthetic weekly DataFrame. iloc[-2] = last completed bar; controls MA10w test.
    MA10w = mean(iloc[-11:-1]) = mean of 10 bars all at 105. Last completed bar
    at 85 triggers exit; at 110 does not.
    Fixed start avoids off-by-one from freq='W-FRI' + end=today on newer pandas.
    """
    idx = pd.date_range(start="2025-01-03", periods=n, freq="W-FRI")
    closes = np.full(n, 105.0)
    closes[-2] = 85.0 if below_ma else 110.0
    return pd.DataFrame({
        "Open":   closes * 0.99,
        "High":   closes * 1.01,
        "Low":    closes * 0.98,
        "Close":  closes,
        "Volume": np.full(n, 1_000_000),
    }, index=idx)


# ── Base test class ────────────────────────────────────────────────────────────

class _Base(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_sf = pm._STATE_FILE
        pm._STATE_FILE = os.path.join(self._tmpdir, "state.json")

    def tearDown(self):
        pm._STATE_FILE = self._orig_sf

    @staticmethod
    def _broker_pos(cur_price, avg_cost=100.0, qty=100):
        return [{"symbol": "TST", "qty": str(qty),
                 "avg_entry_price": str(avg_cost), "current_price": str(cur_price)}]

    def _run(self, sym, cur_price, avg_cost=100.0, qty=100,
             days=5, stop_alive=False, hist=None, weekly=None):
        """Run one check_positions() cycle; return (call_log, final_sym_state)."""
        calls = {"sell": [], "stop": [], "lsell": [], "trail": []}
        pm._save_state({"TST": sym})

        mock_df  = hist if hist is not None else _ohlcv(close=cur_price)
        mock_yf  = MagicMock()
        mock_yf.download.return_value = mock_df
        mock_yf.Ticker.return_value.history.return_value = mock_df

        weekly_df = weekly if weekly is not None else _weekly()

        # Mock datetime so all once-per-day guards see _FIXED_DATE and are blocked
        # (Keltner, PM11, VWAP, MA20, gap-harvest, stale). hour=10 keeps VWAP inactive.
        mock_dt = MagicMock()
        mock_dt.now.return_value.strftime.return_value = _FIXED_DATE
        mock_dt.now.return_value.hour = 10

        # Feed PM11 stop-revalidation the existing stop so it won't re-place one
        stop_oid   = sym.get("stop_order_id", "")
        open_orders = (
            [{"id": stop_oid, "type": "stop", "side": "sell"}] if stop_oid else []
        )

        # Return metadata mirroring the test sym so any meta-reload is idempotent
        meta_return = {
            "stop_loss":         float(sym.get("stop_loss", 0.0)),
            "quality_score":     int(sym.get("quality_score", 5)),
            "composite_score":   float(sym.get("composite_score", 0.0) or 0.0),
            "measured_move_pct": float(sym.get("measured_move_pct", 0.0) or 0.0),
            "buy_stop":          float(sym.get("buy_stop", 0.0)),
            "active_signals":    sym.get("active_signals", []),
        }

        with ExitStack() as stk:
            stk.enter_context(patch("position_monitor.datetime", mock_dt))
            stk.enter_context(patch("position_monitor._market_is_open", return_value=True))
            stk.enter_context(patch("position_monitor._check_drawdown_proximity"))
            stk.enter_context(patch("position_sync.sync_all"))
            stk.enter_context(patch("position_monitor._get_positions",
                                    return_value=self._broker_pos(cur_price, avg_cost, qty)))
            stk.enter_context(patch("position_monitor._get_open_orders",
                                    return_value=open_orders))
            stk.enter_context(patch("position_monitor._stop_order_alive",
                                    return_value=stop_alive))
            stk.enter_context(patch("position_monitor._cancel_stop_orders"))
            stk.enter_context(patch("position_monitor._trading_days_held",
                                    return_value=days))
            stk.enter_context(patch("position_monitor._tg"))
            stk.enter_context(patch("risk_manager.get_state",
                                    return_value={"daily_pnl_pct": 0.0}))
            stk.enter_context(patch("position_monitor.yf", mock_yf))
            stk.enter_context(patch("position_monitor._get_weekly_hist",
                                    return_value=weekly_df))
            stk.enter_context(patch("position_monitor._lookup_position_metadata",
                                    return_value=meta_return))
            stk.enter_context(patch("position_monitor._place_market_sell",
                side_effect=lambda s, q: calls["sell"].append((s, q)) or True))
            stk.enter_context(patch("position_monitor._place_stop",
                side_effect=lambda s, q, p: calls["stop"].append((s, q, p)) or "oid1"))
            stk.enter_context(patch("position_monitor._place_limit_sell",
                side_effect=lambda s, q, p: calls["lsell"].append((s, q, p)) or "oid2"))
            stk.enter_context(patch("position_monitor._place_trailing_stop",
                side_effect=lambda s, q, pct: calls["trail"].append((s, q, pct)) or "oid3"))
            pm.check_positions()

        return calls, pm._load_state().get("TST", {})


# ── Step Z: Failed breakout ────────────────────────────────────────────────────

class TestStepZ(_Base):
    """Step Z fires when price falls decisively back below the VCP pivot within 20 days."""

    def _z_sym(self, **overrides):
        base = {
            "avg_cost":             100.0,
            "initial_qty":          100,
            "buy_stop":             105.0,
            "entry_date":           "2026-01-01",
            "partial1_done":        False,
            "failed_breakout_done": False,
            "partial_qty":          0,
            "trailing_stop_placed": True,
            "stop_order_id":        "oid0",
            "slippage_exited":      False,
            # Block once-per-day guards
            "_meta_loaded":         True,
            "_meta_date":           _FIXED_DATE,
            "_vol_checked":         True,
            "_keltner_date":        _FIXED_DATE,
            "_sv_date":             _FIXED_DATE,
            "_vwap_check_date":     _FIXED_DATE,
            "_gap_check_date":      _FIXED_DATE,
            "_ma20_check_date":     _FIXED_DATE,
            "_pivot_trail_date":    _FIXED_DATE,
            "_stale_alert_date":    _FIXED_DATE,
        }
        base.update(overrides)
        return base

    def test_z_fires_below_pivot(self):
        """Price < pivot*0.99, pnl < 3% → market sell + failed_breakout_done=True."""
        sym = self._z_sym()
        # cur=102 < buy_stop=105*0.99=103.95; pnl=2% < 3%
        calls, state = self._run(sym, cur_price=102.0, avg_cost=100.0,
                                 days=5, stop_alive=True)
        self.assertTrue(calls["sell"], "Expected market sell on failed breakout")
        self.assertTrue(state.get("failed_breakout_done"))

    def test_z_blocked_by_flag(self):
        """failed_breakout_done=True → guard blocks, no action."""
        sym = self._z_sym(failed_breakout_done=True)
        calls, _ = self._run(sym, cur_price=102.0, avg_cost=100.0,
                             days=5, stop_alive=True)
        self.assertFalse(calls["sell"], "Should not sell when flag already set")

    def test_z_blocked_by_runner(self):
        """pnl >= +3% → position is a winner, Z does not fire."""
        sym = self._z_sym()
        # avg=90, cur=103 → pnl=14.4% >= 3%; price still < pivot*0.99=103.95
        calls, _ = self._run(sym, cur_price=103.0, avg_cost=90.0,
                             days=5, stop_alive=True)
        self.assertFalse(calls["sell"], "Runner with pnl >= 3% should not trigger Z")

    def test_z_blocked_at_pivot(self):
        """Price at 104.5 > buy_stop*0.99=103.95 → decisive break not confirmed."""
        sym = self._z_sym()
        calls, _ = self._run(sym, cur_price=104.5, avg_cost=100.0,
                             days=5, stop_alive=True)
        self.assertFalse(calls["sell"],
                         "Price above 1%-below-pivot threshold — Z should not fire")


# ── Step A: Stop placement + pivot ratchet ────────────────────────────────────

class TestStepA(_Base):
    """Step A places initial stops and ratchets them up to swing lows."""

    def _a_sym(self, **overrides):
        base = {
            "avg_cost":        100.0,
            "initial_qty":     100,
            "entry_date":      "2026-01-01",
            "buy_stop":        0.0,
            "partial_qty":     0,
            "partial1_done":   False,
            "breakeven_done":  False,
            "partial_done":    False,
            "slippage_exited": False,
            # Block once-per-day guards
            "_meta_loaded":         True,
            "_meta_date":           _FIXED_DATE,
            "_vol_checked":         True,
            "_keltner_date":        _FIXED_DATE,
            "_sv_date":             _FIXED_DATE,
            "_vwap_check_date":     _FIXED_DATE,
            "_gap_check_date":      _FIXED_DATE,
            "_ma20_check_date":     _FIXED_DATE,
            "_stale_alert_date":    _FIXED_DATE,
            # _pivot_trail_date intentionally NOT set for ratchet tests
        }
        base.update(overrides)
        return base

    def test_a_places_hard_stop_first_cycle(self):
        """stop_loss=95, no existing stop → _place_stop(95) called once."""
        sym = self._a_sym(stop_loss=95.0, trailing_stop_placed=False,
                          _pivot_trail_date=_FIXED_DATE)
        calls, state = self._run(sym, cur_price=97.0, avg_cost=100.0, days=3)
        self.assertTrue(calls["stop"], "Expected _place_stop to be called")
        placed = [p for _, _, p in calls["stop"]]
        self.assertIn(95.0, placed, f"Expected hard stop at 95.0, got {placed}")
        self.assertTrue(state.get("trailing_stop_placed"))

    def test_a_places_trailing_when_no_hard_stop(self):
        """stop_loss=0, no existing stop → _place_trailing_stop called."""
        sym = self._a_sym(stop_loss=0.0, trailing_stop_placed=False,
                          _pivot_trail_date=_FIXED_DATE)
        calls, _ = self._run(sym, cur_price=97.0, avg_cost=100.0, days=3)
        self.assertTrue(calls["trail"], "Expected trailing stop to be placed")
        self.assertFalse(calls["stop"], "Should not call _place_stop when no hard level")

    def test_a_skips_if_stop_alive(self):
        """Existing stop alive → needs_stop=False, no new stop placed."""
        sym = self._a_sym(
            stop_loss=95.0,
            trailing_stop_placed=True,
            stop_order_id="oid0",
            _pivot_trail_date=_FIXED_DATE,
        )
        calls, _ = self._run(sym, cur_price=97.0, avg_cost=100.0,
                             days=3, stop_alive=True)
        self.assertFalse(calls["stop"],  "Should not replace alive hard stop")
        self.assertFalse(calls["trail"], "Should not place trailing when stop is alive")

    def test_a_ratchets_up_only(self):
        """Swing low=95 → pivot_stop=94.05 > cur_stop=90 → ratchet fires.
        cur_price=105 (5% pnl) keeps breakeven (8%) from firing.
        """
        sym = self._a_sym(
            stop_loss=90.0,
            trailing_stop_placed=True,
            stop_order_id="oid0",
            # _pivot_trail_date NOT set → pivot trail can run
        )
        hist = _ohlcv(n=60, close=105.0, swing_low=95.0)
        calls, state = self._run(sym, cur_price=105.0, avg_cost=100.0,
                                 days=7, stop_alive=True, hist=hist)
        placed = [round(p, 2) for _, _, p in calls["stop"]]
        self.assertTrue(any(abs(p - 94.05) < 0.02 for p in placed),
                        f"Expected pivot stop ~94.05, got {placed}")
        self.assertGreater(state.get("stop_loss", 0), 90.0,
                           "stop_loss in state should ratchet up from 90")

    def test_a_no_ratchet_down(self):
        """Swing low=88 → pivot_stop=87.12 < cur_stop=90 → ratchet must not fire.
        cur_price=105 (5% pnl) keeps breakeven (8%) from firing.
        """
        sym = self._a_sym(
            stop_loss=90.0,
            trailing_stop_placed=True,
            stop_order_id="oid0",
            # _pivot_trail_date NOT set → pivot trail runs but finds no ratchet-up
        )
        hist = _ohlcv(n=60, close=105.0, swing_low=88.0)
        calls, state = self._run(sym, cur_price=105.0, avg_cost=100.0,
                                 days=7, stop_alive=True, hist=hist)
        placed = [round(p, 2) for _, _, p in calls["stop"]]
        self.assertFalse(any(p < 90.0 for p in placed),
                         f"Ratchet must never lower the stop, got {placed}")
        self.assertAlmostEqual(state.get("stop_loss", 90.0), 90.0, places=1,
                               msg="stop_loss should remain 90.0 when pivot is lower")


# ── Step B1: First partial exit ────────────────────────────────────────────────

class TestStepB1(_Base):
    """Step B1 sells 1/3 of initial position at 10% gain (15% for elite setups)."""

    def _b1_sym(self, **overrides):
        base = {
            "avg_cost":             100.0,
            "initial_qty":          100,
            "entry_date":           "2026-01-01",
            "buy_stop":             0.0,
            "partial_qty":          0,
            "partial1_done":        False,
            "composite_score":      5.0,
            "trailing_stop_placed": True,
            "stop_order_id":        "oid0",
            "slippage_exited":      False,
            # Block once-per-day guards
            "_meta_loaded":         True,
            "_meta_date":           _FIXED_DATE,
            "_vol_checked":         True,
            "_keltner_date":        _FIXED_DATE,
            "_sv_date":             _FIXED_DATE,
            "_vwap_check_date":     _FIXED_DATE,
            "_gap_check_date":      _FIXED_DATE,
            "_ma20_check_date":     _FIXED_DATE,
            "_pivot_trail_date":    _FIXED_DATE,
            "_stale_alert_date":    _FIXED_DATE,
        }
        base.update(overrides)
        return base

    def test_b1_fires_at_10_pct(self):
        """Standard setup (score=5): partial fires at +11% (>= 10% trigger)."""
        sym = self._b1_sym()
        calls, state = self._run(sym, cur_price=111.0, avg_cost=100.0,
                                 days=10, stop_alive=True)
        self.assertTrue(calls["lsell"], "Expected limit sell for B1")
        self.assertTrue(state.get("partial1_done"))
        self.assertTrue(state.get("partial_done"))

    def test_b1_elite_needs_15_pct(self):
        """Elite setup (score=8.5): +11% not enough, needs +15%."""
        sym = self._b1_sym(composite_score=8.5)
        calls, state = self._run(sym, cur_price=111.0, avg_cost=100.0,
                                 days=10, stop_alive=True)
        self.assertFalse(calls["lsell"], "Elite setup should wait for +15%")
        self.assertFalse(state.get("partial1_done", False))

    def test_b1_elite_fires_at_15_pct(self):
        """Elite setup (score=8.5): +16% → B1 fires."""
        sym = self._b1_sym(composite_score=8.5)
        calls, state = self._run(sym, cur_price=116.0, avg_cost=100.0,
                                 days=10, stop_alive=True)
        self.assertTrue(calls["lsell"], "Elite setup should fire B1 at +16%")
        self.assertTrue(state.get("partial1_done"))

    def test_b1_blocked_by_fast_mover(self):
        """fast_mover + days<40 + pnl>5% → 8-week hold rule blocks B1."""
        sym = self._b1_sym(fast_mover=True)
        # pnl=25%, fast_mover=True, days=20 → _skip_b1_8w=True
        calls, state = self._run(sym, cur_price=125.0, avg_cost=100.0,
                                 days=20, stop_alive=True)
        self.assertFalse(calls["lsell"], "8-week rule should block B1 for fast movers")
        self.assertFalse(state.get("partial1_done", False))

    def test_b1_correct_sell_qty(self):
        """initial_qty=90 → sell_qty = round(90/3) = 30."""
        sym = self._b1_sym(initial_qty=90)
        calls, _ = self._run(sym, cur_price=111.0, avg_cost=100.0,
                             days=10, stop_alive=True, qty=90)
        self.assertTrue(calls["lsell"])
        sold_qty = calls["lsell"][0][1]
        self.assertEqual(sold_qty, 30, f"Expected sell_qty=30, got {sold_qty}")


# ── Step W: Weekly close under MA10w ──────────────────────────────────────────

class TestStepW(_Base):
    """Step W exits when the last completed weekly close falls below the 10-week MA."""

    def _w_sym(self, **overrides):
        base = {
            "avg_cost":             100.0,
            "initial_qty":          100,
            "entry_date":           "2026-01-01",
            "buy_stop":             0.0,
            "partial_qty":          0,
            "partial1_done":        True,   # block B1 / pivot trail
            "partial_done":         True,   # block Step D
            "trailing_stop_placed": True,
            "stop_order_id":        "oid0",
            "slippage_exited":      False,
            "weekly_close_exited":  False,
            "time_stopped":         False,
            "max_loss_exited":      False,
            # Block once-per-day guards
            "_meta_loaded":         True,
            "_meta_date":           _FIXED_DATE,
            "_vol_checked":         True,
            "_keltner_date":        _FIXED_DATE,
            "_sv_date":             _FIXED_DATE,
            "_vwap_check_date":     _FIXED_DATE,
            "_gap_check_date":      _FIXED_DATE,
            "_ma20_check_date":     _FIXED_DATE,
            "_pivot_trail_date":    _FIXED_DATE,
            "_stale_alert_date":    _FIXED_DATE,
            # _w_check_date NOT set → Step W will run
        }
        base.update(overrides)
        return base

    def test_w_fires_when_below_ma10w(self):
        """Last completed weekly close < MA10w → market sell, weekly_close_exited."""
        sym = self._w_sym()
        calls, state = self._run(sym, cur_price=100.0, avg_cost=100.0,
                                 days=30, stop_alive=True,
                                 weekly=_weekly(below_ma=True))
        self.assertTrue(calls["sell"], "Expected market sell on MA10w breakdown")
        self.assertTrue(state.get("weekly_close_exited"))

    def test_w_no_fire_above_ma10w(self):
        """Last completed weekly close > MA10w → no exit."""
        sym = self._w_sym()
        calls, state = self._run(sym, cur_price=100.0, avg_cost=100.0,
                                 days=30, stop_alive=True,
                                 weekly=_weekly(below_ma=False))
        self.assertFalse(calls["sell"], "Should not exit when close is above MA10w")
        self.assertFalse(state.get("weekly_close_exited", False))

    def test_w_blocked_by_time_stopped(self):
        """time_stopped=True → Step W guard blocks execution."""
        sym = self._w_sym(time_stopped=True)
        calls, _ = self._run(sym, cur_price=100.0, avg_cost=100.0,
                             days=30, stop_alive=True,
                             weekly=_weekly(below_ma=True))
        self.assertFalse(calls["sell"], "time_stopped should prevent Step W")

    def test_w_once_per_day(self):
        """_w_check_date == today → step already ran today, must be skipped.
        _FIXED_DATE matches what datetime.now mock returns in _run(), so the
        'already checked today' guard fires.
        """
        sym = self._w_sym(_w_check_date=_FIXED_DATE)
        calls, _ = self._run(sym, cur_price=100.0, avg_cost=100.0,
                             days=30, stop_alive=True,
                             weekly=_weekly(below_ma=True))
        self.assertFalse(calls["sell"], "Step W should run at most once per day")


# ── Step D: Time stop (direct function tests) ─────────────────────────────────

class TestStepD(unittest.TestCase):
    """Test _step_d_time_stop() directly — no full cycle needed."""

    _CFG = {"time_stop_min_gain_pct": 0.02}

    @staticmethod
    def _sym(**overrides):
        base = {
            "entry_date":      "2026-01-01",
            "partial_done":    False,
            "time_stopped":    False,
            "max_loss_exited": False,
            "partial_qty":     0,
        }
        base.update(overrides)
        return base

    def _call(self, sym, days, pnl, time_stop_days=21,
              pead=False, soft_dd=False):
        with patch("position_monitor._cancel_stop_orders"), \
             patch("position_monitor._place_market_sell", return_value=True), \
             patch("position_monitor._tg"):
            result = _step_d_time_stop(
                sym, "TST", 100, pnl, time_stop_days,
                days, pead, soft_dd, self._CFG,
            )
        return result, sym

    def test_d_fires_when_days_exceeded(self):
        """days=26 >= 21, pnl=-1% < 2% min_gain → fires (True), sets time_stopped."""
        sym = self._sym()
        result, state = self._call(sym, days=26, pnl=-0.01)
        self.assertTrue(result)
        self.assertTrue(state.get("time_stopped"))

    def test_d_holds_runner(self):
        """days=30 >= 21, pnl=+5% >= 2% min_gain → hold, returns False."""
        sym = self._sym()
        result, _ = self._call(sym, days=30, pnl=0.05)
        self.assertFalse(result, "Should not time-stop a winner")

    def test_d_blocked_by_partial_done(self):
        """partial_done=True → guard returns False immediately."""
        sym = self._sym(partial_done=True)
        result, _ = self._call(sym, days=30, pnl=-0.01)
        self.assertFalse(result)

    def test_d_soft_dd_tightens_days(self):
        """soft_dd=True → tsd = max(int(21*0.7), 10) = 14; fires at day 16."""
        sym = self._sym()
        result, state = self._call(sym, days=16, pnl=-0.01, soft_dd=True)
        self.assertTrue(result, "Soft-DD should tighten time stop to 14 days")
        self.assertTrue(state.get("time_stopped"))

    def test_d_pead_exception(self):
        """pead_active=True → PEAD hold prevents time stop regardless of days."""
        sym = self._sym()
        result, _ = self._call(sym, days=30, pnl=-0.01, pead=True)
        self.assertFalse(result, "PEAD hold should prevent time stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
