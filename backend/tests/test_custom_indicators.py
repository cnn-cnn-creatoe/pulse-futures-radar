"""Security, causality and persistence tests for custom indicator DSL."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monitor.custom_indicators import (
    CustomIndicatorRegistry,
    IndicatorNotValidatedError,
    IndicatorRegistryError,
    IndicatorValidationError,
    evaluate_indicator,
    validate_indicator_spec,
    validation_report,
)
from monitor.engine import Candle


def bars(closes: list[float], volumes: list[float] | None = None) -> list[Candle]:
    volumes = volumes or [100 + index for index in range(len(closes))]
    return [
        Candle(
            "TESTUSDT", "1m", index * 60_000, (index + 1) * 60_000 - 1,
            close - 0.2, close + 0.5, close - 0.5, close,
            close * volumes[index], close * volumes[index] / 2, 10, volumes[index], True,
        )
        for index, close in enumerate(closes)
    ]


def spec(**changes):
    result = {
        "id": "momentum_guard",
        "name": "Momentum guard",
        "description": "Causal RSI and moving-average confirmation",
        "long_expression": "close > sma(3) and rsi(3) > 55",
        "short_expression": "close < sma(3) and rsi(3) < 45",
    }
    result.update(changes)
    return result


class ExpressionSecurityTests(unittest.TestCase):
    def test_normal_rules_and_series_aware_functions_validate(self):
        checked = validate_indicator_spec(spec(long_expression="close > ema(close, 3) and volume > sma(volume, 2) and close < highest(5)", short_expression="close < lowest(low, 3) or atr(3) > 2"))
        self.assertEqual(checked["required_history"], 5)
        self.assertEqual(checked["language_version"], 1)

    def test_malicious_or_dynamic_python_is_rejected(self):
        malicious = [
            "__import__('os').system('whoami')",
            "close.__class__ == 1",
            "close[0] > 1",
            "(lambda: 1)() > 0",
            "[x for x in (1, 2)] == 1",
            "open('/tmp/pwned') == 1",
            "sma(period=14) > 1",
            "sma(close, int(close)) > 1",
            "(close := 1) > 0",
            "close ** 2 > 1",
        ]
        for expression in malicious:
            with self.subTest(expression=expression), self.assertRaises(IndicatorValidationError):
                validate_indicator_spec(spec(long_expression=expression, short_expression=""))

    def test_period_length_complexity_and_result_type_are_bounded(self):
        invalid = [
            "sma(0) > close", "sma(501) > close", "sma(1.5) > close",
            "close + 1", "close and high", "1e309 > close",
            " and ".join(["close > 1"] * 70),
        ]
        for expression in invalid:
            with self.subTest(expression=expression):
                report = validation_report(spec(long_expression=expression, short_expression=""))
                self.assertFalse(report["valid"])
                self.assertTrue(report["errors"])

    def test_built_in_ids_cannot_be_shadowed(self):
        for indicator_id in ("supertrend", "smc", "ema"):
            with self.subTest(indicator_id=indicator_id), self.assertRaises(IndicatorValidationError):
                validate_indicator_spec(spec(id=indicator_id))


class CausalEvaluationTests(unittest.TestCase):
    def test_normal_long_short_and_neutral_results(self):
        rising = bars([10, 10, 10, 11, 12, 13])
        falling = bars([13, 13, 13, 12, 11, 10])
        flat = bars([10] * 6)
        self.assertEqual(evaluate_indicator(spec(), rising)["signal"], "long")
        self.assertEqual(evaluate_indicator(spec(), falling)["signal"], "short")
        self.assertEqual(evaluate_indicator(spec(), flat)["signal"], "neutral")

    def test_future_bars_cannot_change_a_prefix_result(self):
        history = bars([10, 10, 10, 11, 12, 13])
        prefix = history[:5]
        before = evaluate_indicator(spec(), prefix)
        history[5] = replace(history[5], open=99.8, high=100.5, low=99.5, close=100)
        after = evaluate_indicator(spec(), history[:5])
        self.assertEqual(before, after)
        self.assertEqual(before["close_time"], prefix[-1].close_time)

    def test_open_gap_invalid_and_short_histories_fail_closed(self):
        complete = bars([10, 11, 12, 13])
        cases = [
            (complete[:3], "insufficient_history"),
            (complete[:-1] + [replace(complete[-1], closed=False)], "open_candle"),
            (complete[:2] + complete[3:], "gap"),
        ]
        for candidate, status in cases:
            with self.subTest(status=status):
                result = evaluate_indicator(spec(), candidate)
                self.assertFalse(result["ready"])
                self.assertEqual(result["status"], status)
                self.assertFalse(result["long"])
                self.assertFalse(result["short"])

        wrong_type = evaluate_indicator(spec(), [object()])
        self.assertEqual(wrong_type["status"], "invalid_data")
        self.assertIsNone(wrong_type["close_time"])

    def test_division_by_zero_fails_closed(self):
        result = evaluate_indicator(spec(long_expression="close / (high - high) > 1", short_expression=""), bars([10, 11, 12, 13]))
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "calculation_error")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nested" / "custom_indicators.json"
        self.registry = CustomIndicatorRegistry(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_draft_can_be_invalid_but_cannot_execute(self):
        draft = self.registry.save_draft(spec(long_expression="close >", short_expression=""))
        self.assertEqual(draft["status"], "draft")
        self.assertFalse(draft["validation"]["valid"])
        self.assertEqual(self.registry.get(draft["id"])["long_expression"], "close >")
        with self.assertRaises(IndicatorNotValidatedError):
            self.registry.evaluate(draft["id"], bars([10, 11, 12, 13]))

    def test_validated_save_list_get_and_evaluate_are_defensive(self):
        saved = self.registry.save_validated(spec())
        self.assertEqual(saved["status"], "validated")
        self.assertEqual(self.registry.evaluate(saved["id"], bars([10, 10, 10, 11, 12]))["signal"], "long")
        listed = self.registry.list_indicators()
        listed[0]["name"] = "tampered"
        self.assertEqual(self.registry.get_indicator(saved["id"])["name"], spec()["name"])
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["indicators"][0]["id"], saved["id"])

    def test_replace_failure_preserves_original_and_cleans_temporary_file(self):
        original = self.registry.save_validated(spec())
        original_bytes = self.path.read_bytes()
        with patch("monitor.custom_indicators.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(IndicatorRegistryError):
                self.registry.save_draft(spec(name="changed"))
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(self.registry.get(original["id"])["name"], original["name"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_or_tampered_registry_fails_closed(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")
        with self.assertRaises(IndicatorRegistryError):
            self.registry.list()
        self.path.write_text(json.dumps({"version": 1, "indicators": [{**spec(), "status": "validated", "long_expression": "__import__('os')"}]}), encoding="utf-8")
        with self.assertRaises(IndicatorRegistryError):
            self.registry.list()


if __name__ == "__main__":
    unittest.main()
