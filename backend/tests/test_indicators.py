"""Numerical, timing and fail-closed tests for live/backtest shared indicators."""

from copy import deepcopy
from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from monitor.engine import Candle, TIMEFRAME_MS
from monitor.indicators import IndicatorConfig, DEFAULT_QUALITY, _directional_position, _position_assessment, evaluate_candidate, evaluate_candidates, indicator_catalog, indicator_context, validate_quality_config


def bars(values, width=0.4):
    return [Candle("TESTUSDT", "1m", i * 60_000, (i + 1) * 60_000 - 1,
                   value - min(0.1, width / 2), value + width, value - width, value,
                   1000, 550, 20, 10) for i, value in enumerate(values)]


def config(**changes):
    return replace(IndicatorConfig(atr_period=3, smc_internal_length=2, smc_swing_length=4, ema_fast_period=2, ema_slow_period=4), **changes)


STRUCTURE = [10, 9, 8, 9, 10, 11, 10, 9, 8, 10, 12, 11, 10, 7, 8, 9, 10, 11]


def candidate_fixture():
    candles = bars([10, 9, 8, 9, 10, 11, 10, 9, 8.2])
    candles[-2] = replace(candles[-2], open=8, low=7.9)
    candles[-1] = replace(candles[-1], open=8)
    signal = {"id": "candidate", "symbol": "TESTUSDT", "timeframe": "1m", "close_time": candles[-1].close_time,
              "pattern": "阳阳", "direction": "up", "price": 8.2, "change_pct": 2.5,
              "activity_score": 95, "relative_volume": 4, "liquidity_percentile": 0.8, "spread_bps": 2,
              "warnings": [], "nested": {"unchanged": [1, 2]}}
    return signal, candles


def actionable_position(candles, direction="up"):
    up = direction == "up"
    return {
        "assessed_at": candles[-1].close_time,
        "uses_closed_bars_only": True,
        "code": "breakout_up_held" if up else "breakout_down_held",
        "label": "向上突破已站稳" if up else "向下突破已站稳",
        "reason": "连续3根已收盘K线确认突破",
        "reference_type": "former_swing_resistance" if up else "former_swing_support",
        "level": candles[-1].close,
        "zone_low": candles[-1].close * 0.99,
        "zone_high": candles[-1].close * 1.01,
        "distance_pct": 0.0,
        "breakout_confirmed": True,
        "long_allowed": up,
        "short_allowed": not up,
        "long_advice": "突破站稳，可观察顺势做多" if up else "不宜逆势做多",
        "short_advice": "不宜逆势做空" if up else "突破站稳，可观察顺势做空",
    }


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_allowlist_registry(self):
        self.assertEqual(IndicatorConfig().enabled_indicators, ("supertrend", "smc"))
        self.assertEqual({item["id"] for item in indicator_catalog()}, {"supertrend", "smc", "ema"})
        catalog = indicator_catalog()
        catalog[0]["parameters"].append("bad")
        self.assertNotIn("bad", indicator_catalog()[0]["parameters"])
        self.assertEqual(IndicatorConfig(enabled_indicators=["ema"]).enabled_indicators, ("ema",))

    def test_catalog_exposes_chinese_visual_layers_and_formula_defaults(self):
        catalog = {item["id"]: item for item in indicator_catalog()}
        self.assertEqual(catalog["supertrend"]["defaults"], {
            "source": "HL2（最高价与最低价的平均值）", "atr_period": 10,
            "atr_multiplier": 3.0, "atr_method": "Wilder RMA",
        })
        self.assertEqual(catalog["smc"]["glossary"]["BOS"], "结构突破")
        self.assertEqual(catalog["smc"]["glossary"]["CHoCH"], "趋势变化")
        self.assertEqual({layer["id"] for layer in catalog["smc"]["visual_layers"]},
                         {"events", "order_blocks", "equal_levels", "strong_weak", "fvgs", "zone_bounds"})
        self.assertEqual(catalog["smc"]["license"], "CC BY-NC-SA 4.0")

    def test_bad_indicator_configuration_rejected(self):
        for change in ({"enabled_indicators": ["arbitrary_code"]}, {"enabled_indicators": ["ema", "ema"]},
                       {"enabled_indicators": "ema"}, {"confirmation_mode": "maybe"}, {"atr_method": "random"},
                       {"atr_period": 0}, {"atr_period": True}, {"atr_multiplier": math.nan},
                       {"atr_multiplier": 0}, {"smc_swing_length": 3, "smc_internal_length": 5},
                       {"ema_fast_period": 50, "ema_slow_period": 20}, {"structure_max_age": -1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                IndicatorConfig(**change)

    def test_quality_defaults_and_validation(self):
        quality = validate_quality_config({"min_activity_score": 85})
        self.assertEqual(quality["min_relative_volume"], 1.8)
        self.assertEqual(DEFAULT_QUALITY["min_activity_score"], 80)
        for change in ({"typo": 1}, {"allow_missing_liquidity": 1}, {"require_oi": "false"},
                       {"min_activity_score": 101}, {"min_risk_reward": math.inf},
                       {"min_relative_volume": -1}, {"max_spread_bps": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_quality_config(change)


class SupertrendTests(unittest.TestCase):
    def test_wilder_seed_and_recursive_atr_have_known_values(self):
        candles = bars([10, 10, 10, 10], width=1)
        candles[1] = replace(candles[1], high=12, low=8)  # TRs: 2,4,6,8
        candles[2] = replace(candles[2], high=13, low=7)
        candles[3] = replace(candles[3], high=14, low=6)
        self.assertFalse(indicator_context(candles[:2], config())["supertrend"]["ready"])
        at_seed = indicator_context(candles[:3], config())["supertrend"]
        self.assertAlmostEqual(at_seed["atr"], 4)
        self.assertAlmostEqual(at_seed["value"], -2)
        self.assertAlmostEqual(indicator_context(candles, config())["supertrend"]["atr"], 16 / 3)

    def test_sma_tr_skips_first_unknown_previous_close(self):
        candles = bars([10, 10, 10, 10], width=1)
        candles[1] = replace(candles[1], high=12, low=8)
        candles[2] = replace(candles[2], high=13, low=7)
        candles[3] = replace(candles[3], high=14, low=6)
        self.assertFalse(indicator_context(candles[:3], config(atr_method="sma"))["supertrend"]["ready"])
        self.assertAlmostEqual(indicator_context(candles, config(atr_method="sma"))["supertrend"]["atr"], 6)

    def test_previous_close_true_range_includes_price_jump(self):
        candles = bars([10, 20], width=1)
        self.assertAlmostEqual(indicator_context(candles, config(atr_period=2))["supertrend"]["atr"], 6.5)

    def test_previous_band_crossing_flip_and_trailing_line(self):
        candles = bars([10, 10, 10, 11, 12, 8, 7, 13], width=0.5)
        cfg = config(atr_multiplier=1)
        fifth = indicator_context(candles[:5], cfg)["supertrend"]
        sixth = indicator_context(candles[:6], cfg)["supertrend"]
        seventh = indicator_context(candles[:7], cfg)["supertrend"]
        eighth = indicator_context(candles, cfg)["supertrend"]
        self.assertEqual(fifth["direction"], "up")
        self.assertTrue(sixth["flipped"])
        self.assertEqual(sixth["direction"], "down")
        self.assertEqual(sixth["flip_time"], candles[5].close_time)
        self.assertFalse(seventh["flipped"])
        self.assertEqual(seventh["flip_time"], sixth["flip_time"])
        self.assertTrue(eighth["flipped"])
        self.assertEqual(eighth["direction"], "up")
        self.assertGreaterEqual(fifth["value"], indicator_context(candles[:4], cfg)["supertrend"]["value"])

    def test_supertrend_chart_has_causal_chinese_flip_markers_and_parameters(self):
        candles = bars([10, 10, 10, 11, 12, 8, 7, 13], width=0.5)
        result = indicator_context(candles, config(atr_multiplier=1))
        self.assertEqual(result["supertrend"]["source"], "HL2")
        self.assertEqual(result["supertrend"]["atr_period"], 3)
        self.assertEqual(result["supertrend"]["atr_method_label"], "Wilder RMA")
        self.assertEqual([item["code"] for item in result["chart"]["flips"]], ["SELL", "BUY"])
        self.assertEqual([item["label"] for item in result["chart"]["flips"]], ["转为下跌", "转为上涨"])
        for item in result["chart"]["flips"]:
            self.assertEqual(item["confirmed_at"], item["time"] + TIMEFRAME_MS["1m"] - 1)
        self.assertTrue(all(point["label"].startswith("超级趋势") for point in result["chart"]["supertrend"]))

    def test_ema_seed_and_output_warmup(self):
        candles = bars([10, 11, 12, 13])
        short = indicator_context(candles[:3], config())["ema"]
        complete = indicator_context(candles, config())["ema"]
        self.assertFalse(short["ready"])
        self.assertTrue(complete["ready"])
        self.assertEqual(complete["direction"], "up")
        self.assertAlmostEqual(complete["fast"], 12.518518518518519)
        self.assertAlmostEqual(complete["slow"], 11.824)


class StructureTests(unittest.TestCase):
    def test_directional_position_distinguishes_adverse_from_waiting_for_structure(self):
        common = {
            "assessed_at": 123, "uses_closed_bars_only": True,
            "label": "测试位置", "reason": "已确认位置", "level": 100,
            "zone_low": 99, "zone_high": 101, "distance_pct": 0,
            "reference_type": "confirmed_test", "breakout_confirmed": False,
        }
        support_with_down_structure = {
            **common, "code": "lower_support", "long_allowed": False, "short_allowed": False,
            "long_advice": "支撑附近，可观察做多", "short_advice": "不宜追空",
        }
        waiting_long = _directional_position(support_with_down_structure, "up")
        blocked_short = _directional_position(support_with_down_structure, "down")
        self.assertEqual((waiting_long["status"], waiting_long["blocked"]), ("wait", True))
        self.assertEqual(waiting_long["advice"], "有利位置，等待结构转为向上")
        self.assertIn("结构方向尚未与做多同步", waiting_long["reason"])
        self.assertEqual((blocked_short["status"], blocked_short["advice"]), ("blocked", "不宜追空"))

        pressure_with_up_structure = {
            **common, "code": "upper_resistance", "long_allowed": False, "short_allowed": False,
            "long_advice": "不宜追多", "short_advice": "压力附近，可观察做空",
        }
        blocked_long = _directional_position(pressure_with_up_structure, "up")
        waiting_short = _directional_position(pressure_with_up_structure, "down")
        self.assertEqual((blocked_long["status"], blocked_long["advice"]), ("blocked", "不宜追多"))
        self.assertEqual((waiting_short["status"], waiting_short["blocked"]), ("wait", True))
        self.assertEqual(waiting_short["advice"], "有利位置，等待结构转为向下")

    def test_breakout_direction_waits_for_structure_and_blocks_countertrend_entry(self):
        common = {
            "assessed_at": 456, "uses_closed_bars_only": True,
            "label": "突破已站稳", "reason": "连续3根收盘确认", "level": 100,
            "zone_low": 99, "zone_high": 101, "distance_pct": 1,
            "reference_type": "former_swing_resistance", "breakout_confirmed": True,
            "long_allowed": False, "short_allowed": False,
            "long_advice": "突破站稳但结构方向未同步", "short_advice": "不宜逆势做空",
        }
        up_break = {**common, "code": "breakout_up_held"}
        waiting_long = _directional_position(up_break, "up")
        blocked_short = _directional_position(up_break, "down")
        self.assertEqual((waiting_long["status"], waiting_long["advice"]),
                         ("wait", "有利位置，等待结构转为向上"))
        self.assertEqual((blocked_short["status"], blocked_short["advice"]),
                         ("blocked", "不宜追空"))

        down_break = {**common, "code": "breakout_down_held",
                      "reference_type": "former_swing_support"}
        waiting_short = _directional_position(down_break, "down")
        blocked_long = _directional_position(down_break, "up")
        self.assertEqual((waiting_short["status"], waiting_short["advice"]),
                         ("wait", "有利位置，等待结构转为向下"))
        self.assertEqual((blocked_long["status"], blocked_long["advice"]),
                         ("blocked", "不宜追多"))

    def test_pivot_is_visible_only_at_confirmation_close(self):
        candles = bars(STRUCTURE)
        before = indicator_context(candles[:4], config())
        after = indicator_context(candles[:5], config())
        self.assertEqual(before["chart"]["levels"], [])
        pivot = after["chart"]["levels"][0]
        self.assertEqual(pivot["price"], 7.6)
        self.assertEqual(pivot["pivot_time"], candles[2].open_time)
        self.assertEqual(pivot["confirmed_at"], candles[4].close_time)
        self.assertEqual(after["smc"]["events"], [])

    def test_bos_and_choch_record_actual_crossing_close(self):
        candles = bars(STRUCTURE)
        self.assertEqual(indicator_context(candles[:10], config())["smc"]["events"], [])
        bos = indicator_context(candles[:11], config())["smc"]
        self.assertEqual((bos["event"], bos["event_direction"], bos["event_time"]), ("BOS", "up", candles[10].close_time))
        choch = indicator_context(candles[:14], config())["smc"]
        self.assertEqual((choch["event"], choch["event_direction"], choch["event_time"]), ("CHoCH", "down", candles[13].close_time))
        self.assertTrue(all(event["confirmed_at"] <= event["time"] for event in choch["events"]))

    def test_smc_chart_payload_has_separate_chinese_causal_layers(self):
        result = indicator_context(bars(STRUCTURE), config())
        chart = result["chart"]
        self.assertEqual([item["code"] for item in chart["events"]], ["BOS", "CHoCH"])
        self.assertEqual([item["label"] for item in chart["events"]], ["波段结构突破", "波段趋势变化"])
        for event in chart["events"]:
            self.assertEqual(event["confirmed_at"], event["time"] + TIMEFRAME_MS["1m"] - 1)
            self.assertLessEqual(event["pivot_confirmed_at"], event["confirmed_at"])
            self.assertLess(event["pivot_time"], event["time"])
        self.assertTrue(chart["order_blocks"])
        self.assertEqual(chart["order_blocks"][0]["label"], "波段看跌订单块")
        self.assertEqual(chart["order_blocks"][0]["start_time"] + TIMEFRAME_MS["1m"] - 1,
                         chart["order_blocks"][0]["confirmed_at"])
        self.assertEqual([item["label"] for item in chart["zone_bounds"]], ["溢价区", "均衡区", "折价区"])
        self.assertEqual({item["label"] for item in chart["strong_weak"]}, {"强高点", "弱低点"})
        self.assertTrue({item["code"] for item in chart["swing_points"]} <= {"HH", "HL", "LH", "LL"})
        for key in ("events", "order_blocks", "fvgs", "equal_levels", "zone_bounds", "strong_weak", "swing_points"):
            for item in chart[key]:
                self.assertGreaterEqual(item["confirmed_at"], item.get("time", item.get("start_time")))

    def test_order_block_is_removed_after_highlow_mitigation(self):
        candles = bars(STRUCTURE)
        up = indicator_context(candles[:11], config())["smc"]["order_blocks"]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0]["direction"], "up")
        after = indicator_context(candles[:14], config())["smc"]["order_blocks"]
        self.assertFalse(any(block["direction"] == "up" for block in after))
        self.assertEqual(after[0]["confirmed_at"], candles[13].close_time)

    def test_fvg_requires_third_closed_bar_then_mitigates(self):
        candles = bars([100] * 20, width=0.1)
        candles += [Candle("TESTUSDT", "1m", 20 * 60_000, 21 * 60_000 - 1, 100, 110.1, 99.9, 110, 1000, 550, 20, 10),
                    Candle("TESTUSDT", "1m", 21 * 60_000, 22 * 60_000 - 1, 110, 111.1, 109.9, 111, 1000, 550, 20, 10)]
        self.assertEqual(indicator_context(candles[:-1], config())["smc"]["fvgs"], [])
        gap = indicator_context(candles, config())["smc"]["fvgs"][0]
        self.assertEqual(gap["direction"], "up")
        self.assertEqual(gap["confirmed_at"], candles[-1].close_time)
        self.assertEqual(gap["low"], 100.1)
        open_version = candles[:-1] + [replace(candles[-1], closed=False)]
        self.assertEqual(indicator_context(open_version, config())["smc"]["fvgs"], [])
        candles.append(Candle("TESTUSDT", "1m", 22 * 60_000, 23 * 60_000 - 1, 111, 111.1, 98, 99, 1000, 550, 20, 10))
        self.assertEqual(indicator_context(candles, config())["smc"]["fvgs"], [])

    def test_equal_levels_require_atr200_and_confirmed_pivots(self):
        values = [10 + math.sin(i * math.pi / 6) for i in range(250)]
        candles = bars(values, width=0.2)
        self.assertEqual(indicator_context(candles[:150], config())["smc"]["equal_levels"], [])
        complete = indicator_context(candles, config())["smc"]
        self.assertTrue(complete["equal_levels"])
        self.assertTrue(all(level["confirmed_at"] >= candles[199].close_time for level in complete["equal_levels"]))

    def test_prefix_invariance_and_no_retroactive_events(self):
        candles = bars([100 + 7 * math.sin(i / 6) + i / 100 for i in range(260)])
        full = indicator_context(candles, config())
        for size in (15, 37, 89, 150, 222):
            prefix = indicator_context(candles[:size], config())
            end = candles[size - 1].close_time
            for key in ("supertrend", "ema_fast", "ema_slow"):
                self.assertEqual(prefix["chart"][key], [point for point in full["chart"][key] if point["time"] <= end])
            self.assertEqual(prefix["smc"]["events"], [event for event in full["smc"]["events"] if event["time"] <= end])
            self.assertTrue(all(level["confirmed_at"] <= end for level in prefix["chart"]["levels"]))
            for collection in ("order_blocks", "fvgs", "equal_levels"):
                self.assertTrue(all(item["confirmed_at"] <= end for item in prefix["smc"][collection]))

    def test_output_tails_bounded_but_compute_uses_full_input(self):
        candles = bars([100 + math.sin(i / 5) for i in range(1000)])
        full = indicator_context(candles, config())
        for key, values in full["chart"].items():
            self.assertLessEqual(len(values), 300, key)
        self.assertEqual(full["available_history"], 1000)
        # Late points remain identical with a newly appended candle.
        later = indicator_context(candles + [replace(candles[-1], open_time=60_000_000, close_time=60_059_999)], config())
        self.assertEqual(full["chart"]["supertrend"][-1], later["chart"]["supertrend"][-2])

    def test_opposite_order_block_has_priority_over_bullish_structure(self):
        candles = bars([95, 96, 97, 98, 99, 101, 102])
        low = {"price": 90, "confirmed_at": candles[0].close_time}
        high = {"price": 100, "confirmed_at": candles[0].close_time}
        bearish_block = {"low": 98.5, "high": 103, "direction": "down"}
        position = _position_assessment(candles, "up", [], low, high, [bearish_block], 0.5)
        self.assertEqual(position["code"], "upper_resistance")
        self.assertEqual(position["reference_type"], "bearish_order_block")
        self.assertEqual(position["label"], "前方压力区（突破未站稳）")
        self.assertIn("看跌订单块内", position["reason"])
        self.assertFalse(position["long_allowed"])
        self.assertEqual(position["long_advice"], "不宜追多")
        self.assertFalse(position["breakout_confirmed"])
        self.assertEqual(position["assessed_at"], candles[-1].close_time)
        self.assertTrue(position["uses_closed_bars_only"])

    def test_breakout_needs_three_closed_holds_and_no_opposite_block(self):
        candles = bars([95, 101, 102, 103])
        low = {"price": 90, "confirmed_at": candles[0].close_time}
        high = {"price": 100, "confirmed_at": candles[0].close_time}
        pending = _position_assessment(candles[:-1], "up", [], low, high, [], 0.5)
        held = _position_assessment(candles, "up", [], low, high, [], 0.5)
        self.assertEqual(pending["code"], "upper_resistance")
        self.assertFalse(pending["breakout_confirmed"])
        self.assertEqual(held["code"], "breakout_up_held")
        self.assertTrue(held["breakout_confirmed"])
        self.assertTrue(held["long_allowed"])

    def test_support_blocks_short_and_marks_support_near_long_as_observable(self):
        candles = bars([95, 94, 93, 92, 91])
        low = {"price": 90, "confirmed_at": candles[0].close_time}
        high = {"price": 100, "confirmed_at": candles[0].close_time}
        bullish_block = {"low": 89, "high": 92, "direction": "up"}
        position = _position_assessment(candles, "down", [], low, high, [bullish_block], 0.5)
        self.assertEqual(position["code"], "lower_support")
        self.assertFalse(position["short_allowed"])
        self.assertEqual(position["short_advice"], "不宜追空")
        self.assertIn("看涨订单块内", position["reason"])


class InputIntegrityTests(unittest.TestCase):
    def test_empty_short_open_invalid_and_gapped_data_fail_closed(self):
        self.assertEqual(indicator_context([])["status"], "insufficient_history")
        candles = bars(STRUCTURE)
        cases = [(candles[:4] + candles[5:], "gap"), (candles + [candles[-1]], "gap"),
                 (candles[:-1] + [replace(candles[-1], closed=False)], "open_candle"),
                 (candles[:-1] + [replace(candles[-1], close=math.nan)], "invalid_data"),
                 (candles[:-1] + [replace(candles[-1], symbol="OTHER")], "mixed_series"),
                 (candles[:-1] + [replace(candles[-1], close_time=1)], "invalid_timestamp")]
        for candidate, expected in cases:
            with self.subTest(expected=expected):
                result = indicator_context(candidate, config())
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["supertrend"]["ready"])
                self.assertFalse(result["smc"]["ready"])
                self.assertEqual(result["chart"]["supertrend"], [])


class QualityTests(unittest.TestCase):
    def test_smc_fails_closed_when_support_and_resistance_position_is_unknown(self):
        signal, candles = candidate_fixture()
        context = indicator_context(candles, config())
        context["smc"].update(
            ready=True,
            direction="up",
            events=[{"event": "BOS", "direction": "up", "time": candles[-1].close_time}],
            position_assessment=None,
        )
        with patch("monitor.indicators.indicator_context", return_value=context):
            result = evaluate_candidate(signal, candles, config(enabled_indicators=("smc",)))
        self.assertFalse(result["confirmations"][0]["passed"])
        self.assertIn("位置未确认", result["confirmations"][0]["reason"])
        self.assertEqual(result["position_assessment"]["status"], "wait")
        self.assertTrue(result["position_assessment"]["blocked"])
        self.assertEqual(result["entry_advice"], "等待位置确认")

    def test_smc_same_direction_bos_fails_inside_opposite_order_block(self):
        signal, candles = candidate_fixture()
        context = indicator_context(candles, config())
        context["smc"].update(
            ready=True,
            direction="up",
            events=[{"event": "BOS", "direction": "up", "time": candles[-1].close_time}],
            position_assessment={
                "assessed_at": candles[-1].close_time,
                "uses_closed_bars_only": True,
                "code": "upper_resistance",
                "label": "前方压力区（突破未站稳）",
                "reason": "当前位于看跌订单块内，向上突破尚未满足连续3根收盘站稳",
                "reference_type": "bearish_order_block",
                "level": 8.2,
                "zone_low": 8.1,
                "zone_high": 8.3,
                "distance_pct": 0.0,
                "breakout_confirmed": False,
                "long_allowed": False,
                "short_allowed": True,
                "long_advice": "不宜追多",
                "short_advice": "压力附近，可观察做空",
            },
        )
        with patch("monitor.indicators.indicator_context", return_value=context):
            result = evaluate_candidate(signal, candles, config(enabled_indicators=("smc",)))
        smc = result["confirmations"][0]
        self.assertFalse(smc["passed"])
        self.assertIn("不宜追多", smc["reason"])
        self.assertEqual(result["entry_advice"], "不宜追多")
        self.assertTrue(result["position_assessment"]["blocked"])
        self.assertEqual(result["position_assessment"]["status"], "blocked")
        self.assertEqual(result["position_assessment"]["trade_direction"], "long")
        self.assertEqual(result["position_assessment"]["reference_type"], "bearish_order_block")
        self.assertTrue(any(reason.startswith("位置硬门禁") for reason in result["quality_reasons"]))

    def test_batch_policy_evaluation_matches_individual_results_and_computes_once(self):
        signal, candles = candidate_fixture()
        del signal["spread_bps"]
        policies = [{}, {"allow_missing_liquidity": True}]
        cfg = config(enabled_indicators=())
        expected = [evaluate_candidate(signal, candles, cfg, quality) for quality in policies]
        with patch("monitor.indicators.indicator_context", wraps=indicator_context) as calculate:
            actual = evaluate_candidates(signal, candles, cfg, policies)
        self.assertEqual(actual, expected)
        self.assertEqual(calculate.call_count, 1)
        self.assertFalse(actual[0]["qualified"])
        self.assertFalse(actual[1]["qualified"])
        self.assertEqual(actual[1]["position_assessment"]["status"], "wait")
        actual[0]["indicators"]["warnings"].append("mutated")
        self.assertNotIn("mutated", actual[1]["indicators"]["warnings"])
        for bad in ([], [{}] * 17, {}):
            with self.assertRaises(ValueError):
                evaluate_candidates(signal, candles, cfg, bad)

    def test_raw_mode_records_anomaly_but_waits_when_position_is_unknown(self):
        signal, candles = candidate_fixture()
        original = deepcopy(signal)
        result = evaluate_candidate(signal, candles, config(enabled_indicators=()))
        self.assertFalse(result["qualified"])
        self.assertEqual(result["position_assessment"]["status"], "wait")
        self.assertEqual(result["entry_advice"], "等待位置确认")
        self.assertTrue(any(reason.startswith("位置硬门禁") for reason in result["quality_reasons"]))
        self.assertTrue(result["raw_price_mode"])
        self.assertEqual(result["confirmations"], [])
        self.assertEqual(
            [item["id"] for item in result["available_confirmations"]],
            ["supertrend", "smc", "ema"],
        )
        self.assertEqual(signal, original)
        result["nested"]["unchanged"].append(3)
        self.assertEqual(signal, original)
        plan = result["trade_plan"]
        self.assertTrue(plan["not_order"])
        self.assertLess(plan["stop_loss"], plan["reference_entry"])
        self.assertGreater(plan["target"], plan["reference_entry"])
        self.assertLess(plan["risk_reward"], plan["gross_risk_reward"])

    def test_tiny_moves_rejected_even_with_high_anomaly_score(self):
        signal, candles = candidate_fixture()
        candles[-2] = replace(candles[-2], open=8.199)
        candles[-1] = replace(candles[-1], open=8.199)
        result = evaluate_candidate(signal, candles, config(enabled_indicators=()))
        self.assertFalse(result["qualified"])
        self.assertTrue(any("小信号" in reason for reason in result["quality_reasons"]))

    def test_missing_metrics_fail_closed_with_explicit_liquidity_ablation(self):
        for key in ("activity_score", "relative_volume", "liquidity_percentile", "spread_bps"):
            signal, candles = candidate_fixture()
            del signal[key]
            result = evaluate_candidate(signal, candles, config(enabled_indicators=()))
            self.assertFalse(result["qualified"], key)
        signal, candles = candidate_fixture()
        del signal["liquidity_percentile"]
        del signal["spread_bps"]
        result = evaluate_candidate(signal, candles, config(enabled_indicators=()), {"allow_missing_liquidity": True})
        self.assertFalse(result["qualified"])
        self.assertEqual(result["position_assessment"]["status"], "wait")
        self.assertTrue(result["liquidity_ablation"])
        self.assertEqual(result["estimated_cost_bps"], 24)

    def test_wide_spread_low_volume_low_score_and_required_oi_rejected(self):
        for change, quality in (({"spread_bps": 30}, {}), ({"activity_score": 70}, {}),
                                ({"relative_volume": 1}, {}), ({"liquidity_percentile": 0.1}, {}),
                                ({}, {"require_oi": True})):
            signal, candles = candidate_fixture()
            result = evaluate_candidate({**signal, **change}, candles, config(enabled_indicators=()), quality)
            self.assertFalse(result["qualified"])

    def test_plan_does_not_invent_target_after_breakout(self):
        candles = bars(STRUCTURE[:11])
        signal, _ = candidate_fixture()
        signal.update(close_time=candles[-1].close_time, price=candles[-1].close)
        result = evaluate_candidate(signal, candles, config(enabled_indicators=()))
        self.assertIsNone(result["trade_plan"])
        self.assertFalse(result["qualified"])

    def test_strict_timestamp_and_actual_pattern_match(self):
        signal, candles = candidate_fixture()
        for changes in ({"close_time": 1}, {"symbol": "OTHER"}, {"pattern": "阴阴"}, {"direction": "down"}):
            result = evaluate_candidate({**signal, **changes}, candles, config(enabled_indicators=()))
            self.assertFalse(result["qualified"])

    def test_any_vs_all_and_stale_reversal_confirmation(self):
        signal, candles = candidate_fixture()
        context = indicator_context(candles, config())
        context["supertrend"].update(ready=True, direction="up", flip_time=candles[-1].close_time)
        context["ema"].update(ready=True, direction="down")
        context["smc"]["position_assessment"] = actionable_position(candles)
        with patch("monitor.indicators.indicator_context", return_value=context):
            any_result = evaluate_candidate(signal, candles, config(enabled_indicators=("supertrend", "ema"), confirmation_mode="any"))
            all_result = evaluate_candidate(signal, candles, config(enabled_indicators=("supertrend", "ema"), confirmation_mode="all"))
        self.assertTrue(any_result["qualified"], any_result["quality_reasons"])
        self.assertFalse(all_result["qualified"])
        candles[-2] = replace(candles[-2], open=9.2)
        signal["pattern"] = "阴阳"
        with patch("monitor.indicators.indicator_context", return_value=context):
            recent = evaluate_candidate(signal, candles, config(enabled_indicators=("supertrend",)))
            context["supertrend"]["flip_time"] = candles[-5].close_time
            stale = evaluate_candidate(signal, candles, config(enabled_indicators=("supertrend",)))
        self.assertTrue(recent["confirmations"][0]["passed"])
        self.assertFalse(stale["confirmations"][0]["passed"])
        self.assertEqual(stale["setup"], "reversal")

    def test_smc_reversal_requires_choch_not_only_bos(self):
        signal, candles = candidate_fixture()
        candles[-2] = replace(candles[-2], open=9.2)
        signal["pattern"] = "阴阳"
        context = indicator_context(candles, config())
        event = {"event": "BOS", "direction": "up", "time": candles[-1].close_time}
        context["smc"].update(ready=True, direction="up", events=[event],
                              position_assessment=actionable_position(candles))
        with patch("monitor.indicators.indicator_context", return_value=context):
            bos_only = evaluate_candidate(signal, candles, config(enabled_indicators=("smc",)))
            event["event"] = "CHoCH"
            reversal = evaluate_candidate(signal, candles, config(enabled_indicators=("smc",)))
        self.assertFalse(bos_only["confirmations"][0]["passed"])
        self.assertTrue(reversal["confirmations"][0]["passed"])


if __name__ == "__main__":
    unittest.main()
