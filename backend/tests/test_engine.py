"""Contract and numerical tests for the standalone detection engine."""

from monitor.periods import ceil_bar, shift
from dataclasses import replace
import math
import unittest

from monitor.engine import (
    Candle, EngineConfig, LOOKBACK, TIMEFRAME_MS, aggregate_minutes, analyze,
    body_score, detect_signal, displacement_score, efficiency_score, flow_score,
    liquidity_score, oi_score, volume_score,
)


def fixture(pattern="阳阳", ratio=1.5, timeframe="1d", history_count=None):
    """Baseline NTR is .02 per bar; current pair has exactly requested ratio."""
    start = ceil_bar(0, timeframe)
    count = LOOKBACK[timeframe]["minimum_bars"] + 1 if history_count is None else history_count
    candles = [
        Candle("TESTUSDT", timeframe, shift(start, timeframe, i), shift(start, timeframe, i + 1) - 1,
               99.8, 101, 99, 100, 1000, 550, 20, 10)
        for i in range(count)
    ]
    for char in pattern:
        i = len(candles)
        candles.append(Candle("TESTUSDT", timeframe, shift(start, timeframe, i), shift(start, timeframe, i + 1) - 1,
                              99.6 if char == "阳" else 100.4, 100 + ratio, 100 - ratio, 100,
                              3000, 2100, 30, 30))
    return candles


class DetectionTests(unittest.TestCase):
    def test_all_four_patterns(self):
        for pattern in ("阳阳", "阴阴", "阳阴", "阴阳"):
            with self.subTest(pattern=pattern):
                signal = detect_signal(fixture(pattern))
                self.assertIsNotNone(signal)
                self.assertEqual(signal["pattern"], pattern)
                self.assertAlmostEqual(signal["volatility_ratio"], 1.5)

    def test_threshold_149_rejected_and_150_accepted(self):
        self.assertIsNone(detect_signal(fixture(ratio=1.49)))
        self.assertIsNotNone(detect_signal(fixture(ratio=1.50)))
        self.assertEqual(analyze(fixture(ratio=1.49))["status"], "ready")

    def test_current_pair_never_enters_baseline(self):
        modest = analyze(fixture(ratio=1.5))
        large = analyze(fixture(ratio=10), EngineConfig(allow_doji=True))
        self.assertEqual(modest["baseline"], large["baseline"])
        self.assertEqual(modest["baseline_samples"], 29)
        self.assertAlmostEqual(large["volatility_ratio"], 10)

    def test_normalization_uses_preceding_close_and_includes_gaps(self):
        candles = fixture()
        # The predecessor itself is in history, so calculate its effect directly.
        candles[-3] = replace(candles[-3], open=100, high=200, low=100, close=200)
        candles[-2] = replace(candles[-2], open=100, high=110, low=90, close=105)
        candles[-1] = replace(candles[-1], open=105, high=115, low=100, close=110)
        result = analyze(candles, EngineConfig(baseline_method="simple_mean"))
        expected_v2 = (200 - 90) / 200 + (115 - 100) / 105
        self.assertAlmostEqual(result["v2_pct"], expected_v2 * 100)

    def test_scale_invariance_including_body_scoring(self):
        original = fixture(ratio=3)
        scaled = [replace(c, open=c.open * 0.0001, high=c.high * 0.0001,
                          low=c.low * 0.0001, close=c.close * 0.0001) for c in original]
        first = detect_signal(original)
        second = detect_signal(scaled, tick_size=1e-12)
        self.assertAlmostEqual(first["volatility_ratio"], second["volatility_ratio"])
        self.assertAlmostEqual(first["body_ratio"], second["body_ratio"])
        self.assertEqual(first["score_breakdown"], second["score_breakdown"])

    def test_minimum_history_for_every_timeframe(self):
        for timeframe, spec in LOOKBACK.items():
            with self.subTest(timeframe=timeframe):
                candles = fixture(timeframe=timeframe)
                self.assertEqual(analyze(candles)["status"], "ready")
                short = analyze(candles[1:])
                self.assertEqual(short["status"], "insufficient_history")
                self.assertEqual(short["required_bars"], spec["minimum_bars"])
                self.assertIsNone(short["signal"])

    def test_gaps_before_inside_and_at_current_pair_are_rejected(self):
        for index in (5, -3, -2):
            candles = fixture(history_count=40)
            del candles[index]
            with self.subTest(index=index):
                self.assertEqual(analyze(candles)["status"], "gap")

    def test_duplicate_candle_rejected(self):
        candles = fixture()
        candles.insert(10, candles[10])
        self.assertEqual(analyze(candles)["status"], "gap")

    def test_any_unclosed_bar_in_used_history_or_pair_rejected(self):
        for index in (0, 8, -2, -1):
            candles = fixture()
            candles[index] = replace(candles[index], closed=False)
            self.assertEqual(analyze(candles)["status"], "open_candle")

    def test_closed_flag_must_be_true_not_a_truthy_string(self):
        candles = fixture()
        candles[-1] = replace(candles[-1], closed="false")
        self.assertEqual(analyze(candles)["status"], "open_candle")

    def test_invalid_series_prices_and_times(self):
        changes = [({"symbol": "OTHERUSDT"}, "mixed_series"),
                   ({"timeframe": "5m"}, "mixed_series"),
                   ({"high": 1}, "invalid_data"),
                   ({"close": math.nan}, "invalid_data"),
                   ({"quote_volume": -1}, "invalid_data"),
                   ({"taker_buy_quote": 10_000}, "invalid_data"),
                   ({"close_time": 999}, "invalid_timestamp")]
        for change, expected in changes:
            candles = fixture()
            candles[10] = replace(candles[10], **change)
            with self.subTest(change=change):
                self.assertEqual(analyze(candles)["status"], expected)

    def test_doji_boundary_and_opt_in(self):
        candles = fixture()
        candles[-2] = replace(candles[-2], open=99.85)  # body .15 / range 3 = 5%
        self.assertEqual(analyze(candles)["status"], "pattern_filtered")
        signal = detect_signal(candles, EngineConfig(allow_doji=True))
        self.assertEqual(signal["pattern"], "十字组合")
        candles[-2] = replace(candles[-2], open=99.849)
        self.assertEqual(detect_signal(candles)["pattern"], "阳阳")

    def test_zero_range_doji_uses_tick_size_without_division_error(self):
        candles = fixture(ratio=4)
        candles[-2] = replace(candles[-2], open=100, high=100, low=100, close=100)
        self.assertIsNone(detect_signal(candles))
        self.assertEqual(detect_signal(candles, EngineConfig(allow_doji=True))["pattern"], "十字组合")

    def test_mixed_only(self):
        config = EngineConfig(pattern_mode="mixed_only")
        self.assertIsNone(detect_signal(fixture("阳阳"), config))
        self.assertIsNone(detect_signal(fixture("阴阴"), config))
        self.assertEqual(detect_signal(fixture("阳阴"), config)["pattern"], "阳阴")
        self.assertEqual(detect_signal(fixture("阴阳"), config)["pattern"], "阴阳")

    def test_legacy_alias_trims_instead_of_winsorizing(self):
        candles = fixture(history_count=61)
        candles[10] = replace(candles[10], high=1000)
        simple = analyze(candles, EngineConfig(baseline_method="simple_mean"))
        trimmed = analyze(candles, EngineConfig(trim_percent=10))
        legacy = analyze(candles, EngineConfig(baseline_method="winsorized_mean", trim_percent=10))
        self.assertGreater(simple["baseline"]["v2_pct"], trimmed["baseline"]["v2_pct"])
        self.assertAlmostEqual(trimmed["baseline"]["v2_pct"], 4)
        self.assertEqual(legacy["baseline"], trimmed["baseline"])
        self.assertEqual(legacy["baseline_method"], "trimmed_mean")

    def test_history_older_than_lookback_is_ignored(self):
        candles = fixture(history_count=100)
        candles[0] = replace(candles[0], closed=False, high=1)
        result = analyze(candles)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["history_bars"], 60)
        self.assertEqual(result["baseline_samples"], 59)

    def test_oi_missing_is_not_invented_and_score_is_not_rescaled(self):
        without = detect_signal(fixture(), enrichment={"liquidity_percentile": 0.99, "spread_bps": 1})
        with_oi = detect_signal(fixture(), enrichment={"oi_change_pct": 1.2, "oi_ratio": 5,
                                                      "liquidity_percentile": 0.99, "spread_bps": 1})
        self.assertIsNone(without["oi_change_pct"])
        self.assertEqual(without["score_breakdown"]["oi"], 0)
        self.assertEqual(without["score_available_max"], 85)
        self.assertEqual(with_oi["score_available_max"], 100)
        self.assertEqual(with_oi["activity_score"] - without["activity_score"], 15)
        self.assertIn("新增多头倾向", with_oi["oi_structure"])
        self.assertTrue(without["warnings"])

    def test_partial_invalid_oi_is_not_scored(self):
        for extra in ({"oi_ratio": 5}, {"oi_change_pct": 2}, {"oi_change_pct": math.nan, "oi_ratio": 5},
                      {"oi_change_pct": 2, "oi_ratio": -1}):
            signal = detect_signal(fixture(), enrichment=extra)
            self.assertEqual(signal["score_breakdown"]["oi"], 0)
            self.assertEqual(signal["score_available_max"], 75)
            self.assertTrue(any("当前可用满分 75 分" in item for item in signal["warnings"]))

    def test_missing_liquidity_is_null_and_not_high_quality(self):
        signal = detect_signal(fixture(), enrichment={"liquidity_percentile": 5, "spread_bps": -1})
        self.assertIsNone(signal["liquidity_percentile"])
        self.assertIsNone(signal["spread_bps"])
        self.assertEqual(signal["score_breakdown"]["liquidity"], 0)
        self.assertEqual(signal["score_available_max"], 75)

    def test_available_score_max_tracks_partial_liquidity_inputs(self):
        spread_only = detect_signal(fixture(), enrichment={"spread_bps": 4})
        percentile_only = detect_signal(fixture(), enrichment={"liquidity_percentile": .8})
        self.assertEqual(spread_only["score_available_max"], 77)
        self.assertEqual(percentile_only["score_available_max"], 83)

    def test_zero_volume_never_fakes_buy_sell_balance_or_relative_volume(self):
        candles = [replace(c, quote_volume=0, taker_buy_quote=0) for c in fixture()]
        signal = detect_signal(candles)
        self.assertIsNone(signal["relative_volume"])
        self.assertIsNone(signal["taker_buy_ratio"])
        self.assertEqual(signal["score_breakdown"]["volume"], 0)
        self.assertEqual(signal["score_breakdown"]["flow"], 0)

    def test_neutral_flow_capped_and_mixed_warning(self):
        candles = fixture("阳阴", ratio=3)
        candles[-2] = replace(candles[-2], open=99.8, close=100.6)
        candles[-1] = replace(candles[-1], open=100.6, close=99.8)
        signal = detect_signal(candles, enrichment={"oi_change_pct": 3, "oi_ratio": 5})
        self.assertEqual(signal["direction"], "neutral")
        self.assertLessEqual(signal["score_breakdown"]["flow"], 12)
        self.assertTrue(any("高分歧" in warning for warning in signal["warnings"]))

    def test_id_stable_and_payload_has_candle_data(self):
        first = detect_signal(fixture())
        second = detect_signal(fixture())
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["candles"][-1]["close_time"], first["close_time"])
        self.assertEqual(first["activity_score"], sum(first["score_breakdown"].values()))
        self.assertLessEqual(first["priority_score"], 100)

    def test_full_score_100_is_reachable_with_all_observed_inputs(self):
        candles = fixture()
        candles[-2] = replace(candles[-2], open=100, low=100, high=103, close=103,
                              quote_volume=4000, taker_buy_quote=2800)
        candles[-1] = replace(candles[-1], open=103, low=103, high=107, close=107,
                              quote_volume=4000, taker_buy_quote=2800)
        signal = detect_signal(candles, EngineConfig(volatility_ratio_min=1.5),
                               {"oi_change_pct": 2, "oi_ratio": 5,
                                "liquidity_percentile": 0.99, "spread_bps": 5})
        self.assertEqual(signal["score_breakdown"], {"volume": 35, "structure": 20, "flow": 20, "oi": 15, "liquidity": 10})
        self.assertEqual(signal["activity_score"], 100)
        self.assertEqual(signal["priority_score"], 100)

    def test_oi_structure_uses_signed_price_and_oi_change(self):
        for pattern, oi_change, label in (("阳阳", 2, "新增多头倾向"),
                                           ("阴阴", 2, "新增空头倾向"),
                                           ("阳阳", -2, "空头回补倾向"),
                                           ("阴阴", -2, "多头平仓或清算倾向")):
            signal = detect_signal(fixture(pattern), enrichment={"oi_change_pct": oi_change, "oi_ratio": 3})
            self.assertEqual(signal["oi_structure"], label)

    def test_extreme_low_volume_includes_review_warning(self):
        candles = fixture(ratio=21)
        # Keep non-doji bodies and intentionally little traded volume.
        candles[-2] = replace(candles[-2], open=97, quote_volume=1, taker_buy_quote=0.5, trades=1)
        candles[-1] = replace(candles[-1], open=97, quote_volume=1, taker_buy_quote=0.5, trades=1)
        signal = detect_signal(candles, enrichment={"liquidity_percentile": 0.1, "spread_bps": 20})
        self.assertEqual(signal["level"], "超级异常")
        self.assertTrue(any("数据复核" in warning for warning in signal["warnings"]))
        self.assertTrue(any("插针风险" in warning for warning in signal["warnings"]))
        self.assertTrue(any("滑点" in warning for warning in signal["warnings"]))

    def test_configuration_rejects_invalid_input(self):
        for kwargs in ({"volatility_ratio_min": 1.49}, {"trim_percent": 50}, {"baseline_method": "median"},
                       {"pattern_mode": "unknown"}, {"volatility_ratio_min": math.nan}):
            with self.assertRaises(ValueError):
                EngineConfig(**kwargs)


class ScoreBoundaryTests(unittest.TestCase):
    def test_every_lower_score_boundary(self):
        cases = (
            (volume_score, ((0.8, 4), (1, 8), (1.3, 16), (1.8, 24), (2.5, 31), (4, 35))),
            (body_score, ((1, 3), (1.5, 6), (2, 8), (3, 10))),
            (efficiency_score, ((0.2, 2), (0.35, 4), (0.55, 5), (0.75, 6))),
            (displacement_score, ((0.2, 1), (0.4, 2), (0.6, 3), (0.8, 4))),
            (flow_score, ((0.5, 3), (0.53, 7), (0.56, 12), (0.6, 16), (0.65, 20))),
            (oi_score, ((1, 3), (1.5, 6), (2, 10), (3, 13), (5, 15))),
        )
        for function, boundaries in cases:
            previous_score = 0
            for threshold, expected in boundaries:
                with self.subTest(function=function.__name__, threshold=threshold):
                    self.assertEqual(function(threshold), expected)
                    self.assertEqual(function(math.nextafter(threshold, -math.inf)), previous_score)
                previous_score = expected

    def test_liquidity_boundaries(self):
        for percentile, expected in ((0.199, 0), (0.2, 2), (0.4, 4), (0.6, 6), (0.8, 7), (0.95, 8)):
            self.assertEqual(liquidity_score(percentile, None), expected)
        for spread, expected in ((0, 2), (5, 2), (5.0001, 1), (15, 1), (15.0001, 0)):
            self.assertEqual(liquidity_score(None, spread), expected)
        self.assertEqual(liquidity_score(0.99, 2), 10)
        self.assertEqual(flow_score(0.99, neutral=True), 12)


class AggregationTests(unittest.TestCase):
    @staticmethod
    def minutes(count, start=0, symbol="TESTUSDT"):
        return [Candle(symbol, "1m", start + i * 60_000, start + (i + 1) * 60_000 - 1,
                       100 + i, 102 + i, 99 + i, 101 + i, 10, 6, 2, 3)
                for i in range(count)]

    def test_every_timeframe_complete_utc_bucket(self):
        for timeframe, interval in TIMEFRAME_MS.items():
            if timeframe == "1s":
                with self.assertRaises(ValueError):
                    aggregate_minutes(self.minutes(1), timeframe)
                continue
            start = ceil_bar(1706745600000, timeframe)  # February 2024, including leap day.
            interval = shift(start, timeframe) - start
            count = interval // 60_000
            minutes = self.minutes(count, start)
            result = aggregate_minutes(minutes, timeframe)
            with self.subTest(timeframe=timeframe):
                self.assertEqual(len(result), 1)
                candle = result[0]
                self.assertEqual(candle.open_time, start)
                self.assertEqual(candle.close_time, start + interval - 1)
                self.assertEqual(candle.open, 100)
                self.assertEqual(candle.close, 100 + count)
                self.assertEqual(candle.high, 101 + count)
                self.assertEqual(candle.low, 99)
                self.assertEqual(candle.quote_volume, count * 10)
                self.assertEqual(candle.taker_buy_quote, count * 6)
                self.assertEqual(candle.trades, count * 2)
                self.assertEqual(candle.volume, count * 3)

    def test_daily_boundary_is_utc_midnight(self):
        # Four minutes around midnight must never form a complete daily candle.
        around_midnight = self.minutes(4, 86_400_000 - 2 * 60_000)
        self.assertEqual(aggregate_minutes(around_midnight, "1d"), [])
        complete_day = self.minutes(1440, 86_400_000)
        daily = aggregate_minutes(around_midnight[:2] + complete_day, "1d")
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0].open_time, 86_400_000)

    def test_incomplete_leading_trailing_buckets_omitted(self):
        result = aggregate_minutes(self.minutes(13, 60_000), "5m")
        self.assertEqual([c.open_time for c in result], [300_000])

    def test_missing_duplicate_and_unclosed_buckets_rejected(self):
        base = self.minutes(5)
        bad_inputs = [base[:-1], base + [base[-1]], base[:2] + base[3:],
                      [replace(c, closed=False) if i == 2 else c for i, c in enumerate(base)],
                      [replace(c, close_time=c.close_time + 1) if i == 2 else c for i, c in enumerate(base)]]
        for minutes in bad_inputs:
            self.assertEqual(aggregate_minutes(minutes, "5m"), [])

    def test_interleaved_symbols_and_out_of_order_input_do_not_mix(self):
        candles = self.minutes(5, symbol="AAAUSDT") + self.minutes(5, symbol="BBBUSDC")
        result = aggregate_minutes(list(reversed(candles)), "5m")
        self.assertEqual({c.symbol for c in result}, {"AAAUSDT", "BBBUSDC"})
        self.assertTrue(all(c.quote_volume == 50 for c in result))


if __name__ == "__main__":
    unittest.main()
