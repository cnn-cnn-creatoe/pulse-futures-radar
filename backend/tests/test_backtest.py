"""Execution and leakage tests use explicitly synthetic candles, not market claims."""
from dataclasses import replace
import json
import math
import random
import unittest
from unittest.mock import patch

from monitor.backtest import (_causal_anomaly_prefilter, _execute, _historical_enrichment,
                              _metrics, _recommend, _simulate, run_backtest)
from monitor.engine import Candle, EngineConfig, LOOKBACK, TIMEFRAME_MS, analyze
from monitor.indicators import indicator_context


def synthetic(count=100):
    rng = random.Random(7281)
    interval = TIMEFRAME_MS["1d"]
    candles = []
    for i in range(count):
        close = 100 + rng.uniform(-.2, .2)
        candles.append(Candle("TESTUSDT", "1d", i * interval, (i + 1) * interval - 1,
                              100, 101, 99, close, 10000, 6000, 100, 100))
    return candles


def candidate(index=0, direction="long", stop=95, target=110):
    return {"index": index, "pattern": "阳阳" if direction == "long" else "阴阴",
            "volatility_ratio": 2.5, "activity_score": 80, "qualified": True,
            "quality_reasons": [], "missing_required": False,
            "trade_plan": {"direction": direction, "stop_loss": stop, "target": target}}


OPTIONS = {"max_holding_bars": 5, "fee_bps_per_side": 0, "slippage_bps_per_side": 0}


class ExecutionTests(unittest.TestCase):
    def test_next_open_costs_both_sides_and_known_close(self):
        bars = synthetic(4)
        bars[0] = replace(bars[0], close=100.2)
        bars[1] = replace(bars[1], open=100, high=104, low=99, close=103)
        trade = _execute(bars, candidate(), {**OPTIONS, "max_holding_bars": 1, "fee_bps_per_side": 5, "slippage_bps_per_side": 3})
        expected_entry, expected_exit = 100 * 1.0003, 103 * .9997
        expected_net = ((expected_exit - expected_entry) / expected_entry - .0005 * (expected_entry + expected_exit) / expected_entry) * 100
        self.assertEqual(trade["entry_time"], bars[1].open_time)
        self.assertAlmostEqual(trade["entry_price"], expected_entry)
        self.assertAlmostEqual(trade["net_return_pct"], expected_net)
        self.assertEqual(trade["holding_bars"], 1)

    def test_long_and_short_ambiguous_bar_stop_first(self):
        bars = synthetic(4)
        bars[1] = replace(bars[1], high=115, low=85)
        for side, stop, target in (("long", 95, 110), ("short", 105, 90)):
            with self.subTest(side=side):
                trade = _execute(bars, candidate(direction=side, stop=stop, target=target), OPTIONS)
                self.assertEqual(trade["exit_reason"], "stop_first_ambiguous")
                self.assertAlmostEqual(trade["net_return_pct"], -5)

    def test_stop_gap_fills_open_and_invalid_entry_gap_is_excluded(self):
        bars = synthetic(4)
        bars[2] = replace(bars[2], open=90, high=94, low=89, close=91)
        trade = _execute(bars, candidate(), OPTIONS)
        self.assertEqual(trade["exit_price"], 90)
        self.assertEqual(trade["net_return_pct"], -10)
        bars[1] = replace(bars[1], open=94, high=101, low=93)
        self.assertIsNone(_execute(bars, candidate(), OPTIONS))

    def test_no_overlapping_positions_and_horizons(self):
        bars = synthetic(20)
        rows = [candidate(i) for i in range(12)]
        result = _simulate(bars, rows, 2, 0, len(bars), OPTIONS)
        self.assertEqual([t["entry_index"] for t in result["trades"]], [1, 6, 11])
        self.assertEqual(result["sample_counts"]["excluded_overlap"], 9)
        self.assertEqual(set(result["trades"][0]["forward_returns_net_pct"]), {"1", "2", "3", "5"})

    def test_metrics_no_infinity_and_drawdown_from_closed_equity(self):
        trades = []
        for value in (10, -20, 5):
            trades.append({"net_return_pct": value, "holding_bars": 1, "mfe_pct": 0,
                           "mae_pct": 0, "fee_pct": .1, "slippage_bps_per_side": 3})
        metrics = _metrics(trades)
        self.assertAlmostEqual(metrics["max_drawdown_pct"], 20)
        self.assertAlmostEqual(metrics["profit_factor"], .75)
        self.assertAlmostEqual(metrics["hypothetical_fixed_1x_return_pct"], -7.6)
        self.assertIsNone(_metrics(trades[:1])["profit_factor"])
        json.dumps(metrics, allow_nan=False)


class ChronologyTests(unittest.TestCase):
    def test_fast_prefilter_never_skips_an_authoritative_signal(self):
        bars = synthetic(260)
        for index in range(35, len(bars), 17):
            bars[index] = replace(bars[index], high=108, low=94, close=106)
        config = EngineConfig(volatility_ratio_min=1.5)
        warmup = LOOKBACK["1d"]["minimum_bars"] + 2
        marks = _causal_anomaly_prefilter(bars, config, warmup)
        skipped = 0
        for index in range(warmup, len(bars) - 1):
            authoritative = analyze(bars[:index + 1], config)
            if authoritative.get("signal") is not None:
                self.assertEqual(marks[index], "analyze")
            elif marks[index] != "analyze":
                skipped += 1
                self.assertEqual(marks[index], authoritative["status"])
        self.assertGreater(skipped, len(marks) // 2)

    def test_historical_enrichment_cannot_use_future_or_untimestamped_snapshot(self):
        self.assertEqual(_historical_enrichment({"oi_ratio": 5}, 100), {})
        self.assertEqual(_historical_enrichment({"oi_ratio": 5, "observed_at": 101}, 100), {})
        self.assertEqual(_historical_enrichment({"oi_ratio": 5, "observed_at": 100, "close_time": 99}, 100), {})
        self.assertEqual(_historical_enrichment({"oi_ratio": 5, "observed_at": 100}, 100), {"oi_ratio": 5})

    def test_selection_uses_training_only_and_requires_thirty_holdout_trades(self):
        rows = [
            {"threshold": 2, "train": {"metrics": {"trades": 40, "expectancy_net_pct": 1}}, "holdout": {"metrics": {"trades": 29, "expectancy_net_pct": 3}}},
            {"threshold": 3, "train": {"metrics": {"trades": 40, "expectancy_net_pct": .5}}, "holdout": {"metrics": {"trades": 300, "expectancy_net_pct": 99}}},
        ]
        recommendation = _recommend(rows, 30, "5m")
        self.assertEqual(recommendation["selected_threshold"], 2)
        self.assertEqual(recommendation["status"], "insufficient_holdout")
        self.assertFalse(recommendation["validated"])
        self.assertFalse(recommendation["holdout_used_for_selection"])

    def test_purge_keeps_training_exits_before_holdout(self):
        bars = synthetic(100)
        # Signal64 enters65, exits69; signal65 is inside five-bar purge to70.
        result = _simulate(bars, [candidate(64), candidate(65)], 2, 32, 65, OPTIONS,
                           outcome_end=70)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["exit_index"], 69)
        self.assertLess(result["trades"][0]["exit_time"], bars[70].open_time)

    def test_prefix_only_purged_split_and_strict_missing_policy(self):
        bars = synthetic(110)
        seen = []
        configurations = []
        def analysis(prefix, config, enrichment):
            return {"status": "ready", "signal": {"symbol": "TESTUSDT", "timeframe": "1d",
                "close_time": prefix[-1].close_time, "pattern": "阳阳", "volatility_ratio": 2.5, "activity_score": 60}}
        def evaluate(signal, prefix, config, qualities):
            seen.append((signal["close_time"], max(c.close_time for c in prefix), len(prefix)))
            configurations.append(tuple(config.enabled_indicators))
            return [{**signal, "qualified": True, "quality_reasons": [], "trade_plan": candidate()["trade_plan"]} for _ in qualities]
        with patch("monitor.backtest.analyze", side_effect=analysis), patch("monitor.backtest.evaluate_candidates", side_effect=evaluate):
            report = run_backtest(bars)
        self.assertTrue(all(signal_time == last_time for signal_time, last_time, _ in seen))
        self.assertTrue(all(length >= 33 for _, _, length in seen))
        self.assertEqual(report["dataset"]["purged_signal_bars"], 5)
        self.assertEqual(report["strategy"]["strict_full_policy"]["metrics"]["trades"], 0)
        self.assertGreater(report["strategy"]["strict_full_policy"]["sample_counts"]["excluded_missing_data"], 0)
        ablation = report["strategy"]["price_only_ablation"]
        self.assertGreater(ablation["metrics"]["trades"], 0)
        self.assertEqual(ablation["quality"]["min_activity_score"], 0)
        self.assertFalse(report["historical_enrichment"]["full_100_point_policy_validated"])
        study = report["indicator_study"]
        self.assertEqual(study["status"], "completed")
        self.assertEqual(study["primary_policy"], "price_only_ablation")
        self.assertFalse(study["selection_performed"])
        self.assertIsNone(study["best_combination"])
        self.assertEqual(study["trial_count"], 1)
        self.assertTrue(study["predeclared_combinations"])
        self.assertEqual(study["validation_status"], "chronological_holdout_and_anchored_walk_forward")
        self.assertEqual(study["minimum_closed_trades_per_segment"], 30)
        warning = " ".join(study["warnings"])
        for missing_audit in ("walk-forward", "DSR", "PBO", "cannot be used to select"):
            self.assertIn(missing_audit, warning)
        self.assertEqual([row["enabled_indicators"] for row in study["rows"]], [
            ["wt_extreme_cross"]])
        requested = {tuple(row["enabled_indicators"]) for row in study["rows"]}
        self.assertTrue(requested <= set(configurations))
        for row in study["rows"]:
            self.assertEqual(row["confirmation_mode"], "all")
            self.assertEqual(row["evaluation_policy"], "price_only_ablation")
            for partition in (row["train"], row["holdout"]):
                self.assertTrue({"trades", "win_rate", "expectancy_net_pct", "profit_factor",
                                 "max_drawdown_pct"} <= set(partition["metrics"]))
                self.assertEqual(partition["sample_status"], "insufficient_sample")
            self.assertEqual(row["policies"]["strict_full_policy"]["train"]["metrics"]["trades"], 0)
            self.assertEqual(row["walk_forward"]["fold_count"], 3)
            self.assertEqual(len(row["walk_forward"]["folds"]), 3)
        self.assertIn("support/resistance", study["execution_assumptions"]["trade_plan_basis"])
        self.assertEqual(study["execution_assumptions"]["fee_bps_per_side"], 5)
        self.assertEqual(study["execution_assumptions"]["slippage_bps_per_side"], 3)
        json.dumps(report, allow_nan=False)

    def test_indicator_study_marks_each_partition_and_changes_only_confirmations(self):
        bars = synthetic(140)
        calls = []
        def analysis(prefix, config, enrichment):
            return {"status": "ready", "signal": {"symbol": "TESTUSDT", "timeframe": "1d",
                "close_time": prefix[-1].close_time, "pattern": "阳阳", "direction": "up",
                "volatility_ratio": 2.5, "activity_score": 90}}
        def evaluate(signal, prefix, config, qualities):
            enabled = tuple(config.enabled_indicators)
            calls.append((enabled, signal["close_time"], prefix[-1].close_time))
            passed = enabled == ("wt_extreme_cross",)
            plan = {**candidate()["trade_plan"], "support": 95, "resistance": 110,
                    "risk_reward": 2.5, "basis": "confirmed_structure_levels"}
            return [{**signal, "qualified": passed, "quality_reasons": [] if passed else ["confirmation"],
                     "trade_plan": plan} for _ in qualities]
        with patch("monitor.backtest.analyze", side_effect=analysis), patch("monitor.backtest.evaluate_candidates", side_effect=evaluate):
            report = run_backtest(bars, options={"max_holding_bars": 1})
        self.assertTrue(all(signal_time == prefix_time for _, signal_time, prefix_time in calls))
        rows = {row["id"]: row for row in report["indicator_study"]["rows"]}
        supertrend = rows["wt_extreme_cross"]
        self.assertGreaterEqual(supertrend["train"]["metrics"]["trades"], 30)
        self.assertGreaterEqual(supertrend["holdout"]["metrics"]["trades"], 30)
        self.assertTrue(supertrend["sample_sufficient_both"])
        self.assertEqual(supertrend["train"]["sample_status"], "sufficient")
        for study_id, row in rows.items():
            if study_id != "wt_extreme_cross":
                self.assertEqual(row["train"]["metrics"]["trades"], 0)
                self.assertEqual(row["holdout"]["metrics"]["trades"], 0)
                self.assertFalse(row["sample_sufficient_both"])
        # Costs are actually deducted from each next-open/max-hold execution.
        self.assertGreater(supertrend["train"]["metrics"]["fees_pct_sum"], 0)
        self.assertEqual(supertrend["train"]["metrics"]["slippage_bps_per_side"], 3)
        json.dumps(report["indicator_study"], allow_nan=False)

    def test_indicator_study_reuses_one_identical_causal_context_per_signal(self):
        bars = synthetic(90)
        target_time = bars[50].close_time
        def analysis(prefix, config, enrichment):
            if prefix[-1].close_time != target_time:
                return {"status": "no_anomaly"}
            first, last = prefix[-2:]
            pattern = ("阳" if first.close > first.open else "阴") + ("阳" if last.close > last.open else "阴")
            direction = "up" if pattern in {"阳阳", "阴阳"} else "down"
            return {"status": "ready", "signal": {"symbol": last.symbol, "timeframe": last.timeframe,
                "close_time": last.close_time, "pattern": pattern, "direction": direction,
                "volatility_ratio": 2.5, "activity_score": 90, "relative_volume": 2,
                "liquidity_percentile": .8, "spread_bps": 2}}
        with patch("monitor.backtest.analyze", side_effect=analysis), \
             patch("monitor.indicators.indicator_context", wraps=indicator_context) as calculate:
            report = run_backtest(bars)
        self.assertEqual(calculate.call_count, 1)
        self.assertEqual(len(report["indicator_study"]["rows"]), 1)
        self.assertEqual({tuple(row["enabled_indicators"]) for row in report["indicator_study"]["rows"]}, {
            ("wt_extreme_cross",)})

    def test_invalid_series_options_and_insufficient_warmup(self):
        insufficient = run_backtest(synthetic(20))
        self.assertEqual(insufficient["status"], "insufficient_data")
        self.assertEqual(len(insufficient["indicator_study"]["rows"]), 1)
        self.assertEqual(insufficient["indicator_study"]["trial_count"], 1)
        self.assertTrue(insufficient["indicator_study"]["predeclared_combinations"])
        self.assertFalse(insufficient["indicator_study"]["selection_performed"])
        self.assertEqual(insufficient["indicator_study"]["validation_status"],
                         "chronological_holdout_and_anchored_walk_forward")
        self.assertEqual(insufficient["indicator_study"]["minimum_closed_trades_per_segment"], 30)
        self.assertIn("walk-forward", " ".join(insufficient["indicator_study"]["warnings"]))
        self.assertTrue(all(row["train"]["sample_status"] == "insufficient_sample"
                            for row in insufficient["indicator_study"]["rows"]))
        report = run_backtest(synthetic(20), EngineConfig(volatility_ratio_min=4), options={"threshold_profile": "timeframe"})
        self.assertEqual(report["active_threshold"], 4)
        bars = synthetic()
        del bars[10]
        with self.assertRaisesRegex(ValueError, "continuous"):
            run_backtest(bars)
        for options in ({"max_holding_bars": 0}, {"slippage_bps_per_side": float("nan")}, {"min_validation_trades": 3}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    run_backtest(synthetic(), options=options)


if __name__ == "__main__":
    unittest.main()
