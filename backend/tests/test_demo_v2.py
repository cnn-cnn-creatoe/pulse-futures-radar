"""Demo fixtures keep old engine values and disclose actual computed quality."""

import hashlib
import json
import unittest
from unittest.mock import patch

from monitor.demo import demo_signals
from monitor.engine import EngineConfig, LOOKBACK, MONITOR_TIMEFRAMES
from monitor.indicators import IndicatorConfig, evaluate_candidate, indicator_context


ORIGINAL_FIELDS = {
    "activity_level", "activity_score", "baseline_method", "baseline_samples", "baseline_v2_pct", "body_efficiency",
    "body_ratio", "candles", "change_pct", "close_time", "created_at", "direction", "displacement_efficiency", "id", "level",
    "liquidity_percentile", "metadata_note", "oi_change_pct", "oi_ratio", "oi_structure", "open_time", "pattern", "price",
    "priority_score", "quote_volume", "range_pct", "relative_volume", "score_available_max", "score_breakdown", "source",
    "spread_bps", "symbol", "synthetic", "taker_buy_ratio", "timeframe", "token_address", "v2_pct", "volatility_ratio", "warnings",
}


class ExpandedDemoTests(unittest.TestCase):
    def test_covers_four_patterns_on_each_active_period_and_every_engine_value(self):
        rows = demo_signals()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({row["id"] for row in rows}), 8)
        for timeframe in MONITOR_TIMEFRAMES:
            self.assertEqual({row["pattern"] for row in rows if row["timeframe"] == timeframe}, {"阳阳", "阴阴", "阳阴", "阴阳"})
        original = [{key: row[key] for key in ORIGINAL_FIELDS} for row in rows]
        # Snapshot captured before adding indicator enrichment: locks the seeded
        # OHLCV, engine scores and identities while permitting new context keys.
        digest = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(digest, "57d1f80ec858d22c9e55a3d8827e93ccf00dd48de91b120be1e93c012e9ed548")

    def test_indicators_are_computed_from_full_generated_history(self):
        calls = {}

        def capture(signal, candles, **kwargs):
            calls[signal["id"]] = list(candles)
            return evaluate_candidate(signal, candles, **kwargs)

        with patch("monitor.demo.evaluate_candidate", side_effect=capture):
            rows = demo_signals()
        for row in rows:
            candles = calls[row["id"]]
            self.assertEqual(len(candles), LOOKBACK[row["timeframe"]]["minimum_bars"] + 20)
            self.assertEqual(row["indicators"], indicator_context(candles))
            self.assertEqual(row["indicators"]["available_history"], len(candles))
            self.assertEqual(row["indicator_context"], row["indicators"])
            self.assertEqual(len(row["candles"]), 50)
            self.assertTrue(all(point["time"] <= row["close_time"] for point in row["indicators"]["chart"]["supertrend"]))
        self.assertTrue(any(len(row["indicators"]["chart"]["supertrend"]) > len(row["candles"]) for row in rows))

    def test_quality_is_honest_and_deterministic_and_synthetic_is_explicit(self):
        first, second = demo_signals(), demo_signals()
        self.assertEqual(first, second)
        for row in first:
            self.assertEqual(row["source"], "demo")
            self.assertIs(row["synthetic"], True)
            self.assertEqual(row["quality_schema_version"], 2)
            self.assertEqual(row["qualified"], not row["quality_reasons"])
            self.assertEqual({item["id"] for item in row["confirmations"]}, {"wt_extreme_cross"})
            if row["qualified"]:
                self.assertTrue(all(item["passed"] for item in row["confirmations"]))
                self.assertIsNotNone(row["trade_plan"])
            if row["trade_plan"]:
                self.assertTrue(row["trade_plan"]["not_order"])
        self.assertEqual({row["timeframe"] for row in first}, set(MONITOR_TIMEFRAMES))

    def test_optional_policy_preview_and_existing_engine_filter_signature(self):
        rows = demo_signals(EngineConfig(pattern_mode="mixed_only"), indicator_config=IndicatorConfig(enabled_indicators=("wt_extreme_cross",)), quality={"min_activity_score": 100})
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["pattern"] for row in rows}, {"阴阳", "阳阴"})
        self.assertTrue(all([item["id"] for item in row["confirmations"]] == ["wt_extreme_cross"] for row in rows))
        self.assertTrue(all(not row["qualified"] for row in rows))


if __name__ == "__main__":
    unittest.main()
