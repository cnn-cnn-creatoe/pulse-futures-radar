"""Pure checks for the read-only daily replay helpers."""
import json
import unittest

from monitor.daily_analysis import (DAY, _forward_return, _method_flags,
                                    _pattern_direction, reconstruct_oi)
from monitor.engine import Candle, TIMEFRAME_MS
from monitor.indicators import IndicatorConfig


class DailyAnalysisTests(unittest.TestCase):
    def test_pattern_direction_is_bidirectional(self):
        self.assertEqual(_pattern_direction("阳阳"), ("up", False))
        self.assertEqual(_pattern_direction("阴阴"), ("down", False))
        self.assertEqual(_pattern_direction("阴阳"), ("up", True))
        self.assertEqual(_pattern_direction("阳阴"), ("down", True))

    def test_forward_return_uses_next_open_and_costs_for_both_sides(self):
        interval = TIMEFRAME_MS["5m"]
        signal = Candle("TESTUSDT", "5m", 0, interval - 1, 100, 101, 99, 100)
        entry = Candle("TESTUSDT", "5m", interval, 2 * interval - 1,
                       100, 104, 99, 103)
        bars = {0: signal, interval: entry}
        long = _forward_return(bars, signal, "up", 1)
        short = _forward_return(bars, signal, "down", 1)
        self.assertTrue(long["available"])
        self.assertEqual(long["entry_time"], interval)
        self.assertGreater(long["net_return_pct"], 0)
        self.assertLess(short["net_return_pct"], 0)
        self.assertAlmostEqual(long["entry_after_slippage"], 100.03)
        self.assertAlmostEqual(short["entry_after_slippage"], 99.97)

    def test_oi_reconstruction_trims_a_range_wide_superset_to_rolling_window(self):
        interval = TIMEFRAME_MS["5m"]
        end = 9 * DAY
        rows = []
        quantity = 1000.0
        for timestamp in range(0, end + 60_000, 60_000):
            # Deliberately make the oldest, out-of-window section unlike the
            # current seven days. It must not alter the production baseline.
            growth = 1.01 if timestamp < 2 * DAY else 1.00001
            quantity *= growth
            rows.append({"timestamp": timestamp, "source_time": timestamp,
                         "open_interest_quantity": quantity})
        full = reconstruct_oi(rows, "5m", end - 1)
        window_start = end - 7 * DAY - 2 * interval
        sliced = reconstruct_oi(
            [row for row in rows if window_start <= row["timestamp"] <= end],
            "5m", end - 1)
        self.assertEqual(full["oi_sample_count"], sliced["oi_sample_count"])
        self.assertAlmostEqual(full["oi_change_pct"], sliced["oi_change_pct"])
        self.assertAlmostEqual(full["oi_ratio"], sliced["oi_ratio"])

    def test_method_flags_require_wavetrend_confirmation(self):
        from monitor.daily_analysis import _method_flags
        from monitor.indicators import IndicatorConfig
        context={'status':'ready','shortlist':{'wt_extreme_cross':'up'}}
        flags,_=_method_flags(context,IndicatorConfig(),'阳阳',100,'5m',True)
        self.assertEqual(flags,{'two_bar':True,'wt_extreme_cross':True})
        context['shortlist']['wt_extreme_cross']='neutral'
        flags,_=_method_flags(context,IndicatorConfig(),'阳阳',100,'5m',True)
        self.assertFalse(flags['wt_extreme_cross'])

    def test_oi_reconstruction_fails_closed_without_matched_samples(self):
        self.assertEqual(reconstruct_oi([], "5m", 10_000), {
            "oi_change_pct": None, "oi_ratio": None, "oi_sample_count": 0})
        # Invalid source timestamps cannot become historical OI evidence.
        rows = [{"timestamp": i * 60_000, "source_time": None,
                 "open_interest_quantity": 100 + i} for i in range(100)]
        result = reconstruct_oi(rows, "5m", 99 * 60_000 - 1)
        self.assertIsNone(result["oi_ratio"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
