"""Parity checks for the optimized read-only range replay."""
from __future__ import annotations

from datetime import datetime
import json
import math
import random
import sqlite3
import tempfile
from pathlib import Path
import unittest

from monitor.engine import Candle, EngineConfig, TIMEFRAME_MS, analyze
from array import array

from monitor.daily_analysis import run as run_daily
from monitor.daily_analysis import reconstruct_oi
from monitor.range_analysis import (_OIReconstructor, _RollingTrimmedMean, _liquidity_for_candidates,
                                    _listed_window, _metadata_payloads, _parse_range,
                                    _series_candidates, run as run_range)
from monitor.store import Store


class RangeAnalysisTests(unittest.TestCase):
    def test_batched_canonical_oi_matches_authoritative_per_signal_reconstruction(self):
        five = TIMEFRAME_MS["5m"]
        end = 9 * 86_400_000
        rows = []
        quantity = 10_000.0
        for timestamp in range(five, end + five, five):
            if timestamp % (37 * five) == 0:
                continue
            quantity *= 1 + math.sin(timestamp / five) * .0002 + .00005
            rows.append({"timestamp": timestamp, "source_time": timestamp,
                         "open_interest_quantity": quantity})
        batch = _OIReconstructor(rows)
        self.assertTrue(batch.canonical)
        for timeframe in ("5m", "15m"):
            interval = TIMEFRAME_MS[timeframe]
            closes = [end - offset * interval - 1 for offset in (0, 1, 5, 23, 71)]
            actual = batch.many(timeframe, closes)
            for close_time in closes:
                expected = reconstruct_oi(rows, timeframe, close_time)
                self.assertEqual(actual[close_time]["oi_sample_count"],
                                 expected["oi_sample_count"])
                self.assertEqual(actual[close_time]["oi_change_pct"] is None,
                                 expected["oi_change_pct"] is None)
                if expected["oi_change_pct"] is not None:
                    self.assertAlmostEqual(actual[close_time]["oi_change_pct"],
                                           expected["oi_change_pct"])
                    self.assertAlmostEqual(actual[close_time]["oi_ratio"],
                                           expected["oi_ratio"])

    def test_batched_oi_matches_delayed_legacy_rows(self):
        five = TIMEFRAME_MS["5m"]
        end = 9 * 86_400_000
        rows = [{"timestamp": timestamp + 1_000, "source_time": timestamp,
                 "open_interest_quantity": 1_000 + index}
                for index, timestamp in enumerate(range(five, end, five))]
        batch = _OIReconstructor(rows)
        self.assertFalse(batch.canonical)
        for timeframe in ("5m", "15m"):
            interval = TIMEFRAME_MS[timeframe]
            closes = [end - offset * interval - 1 for offset in (1, 2, 9, 31)]
            actual = batch.many(timeframe, closes)
            for close_time in closes:
                expected = reconstruct_oi(rows, timeframe, close_time)
                self.assertEqual(actual[close_time]["oi_sample_count"],
                                 expected["oi_sample_count"])
                self.assertAlmostEqual(actual[close_time]["oi_change_pct"],
                                       expected["oi_change_pct"])
                self.assertAlmostEqual(actual[close_time]["oi_ratio"],
                                       expected["oi_ratio"])

    def test_exact_backfill_rows_supersede_later_duplicate_legacy_receipts(self):
        five = TIMEFRAME_MS["5m"]
        rows = []
        for index, source in enumerate(range(five, 2 * 86_400_000, five)):
            rows.append({"timestamp": source, "source_time": source,
                         "open_interest_quantity": 1_000 + index})
            rows.append({"timestamp": source + 1_000, "source_time": source,
                         "open_interest_quantity": 9_999_999})
        rows.sort(key=lambda row: row["timestamp"])
        batch = _OIReconstructor(rows)
        self.assertTrue(batch.canonical)
        close_time = 2 * 86_400_000 - five - 1
        actual = batch.many("5m", [close_time])[close_time]
        expected = reconstruct_oi(rows, "5m", close_time)
        self.assertEqual(actual["oi_sample_count"], expected["oi_sample_count"])
        self.assertAlmostEqual(actual["oi_change_pct"], expected["oi_change_pct"])
        self.assertAlmostEqual(actual["oi_ratio"], expected["oi_ratio"])

    def test_rolling_trimmed_mean_matches_direct_tail_removal(self):
        values = [9.0, 1.0, 5.0, 5.0, 3.0, 20.0, 2.0, 7.0] * 8
        rolling = _RollingTrimmedMean(values, 2.5)
        active = []
        for value in values:
            rolling.add(value)
            active.append(value)
        trim = math.floor(len(active) * .025)
        ordered = sorted(active)
        self.assertAlmostEqual(rolling.mean("trimmed_mean"),
                               sum(ordered[trim:len(ordered) - trim]) /
                               (len(ordered) - 2 * trim))
        for value in values[:11]:
            rolling.remove(value)
            active.remove(value)
        trim = math.floor(len(active) * .025)
        ordered = sorted(active)
        self.assertAlmostEqual(rolling.mean("trimmed_mean"),
                               sum(ordered[trim:len(ordered) - trim]) /
                               (len(ordered) - 2 * trim))

    def test_fast_candidate_features_match_authoritative_engine(self):
        random.seed(27)
        interval = TIMEFRAME_MS["15m"]
        candles = []
        price = 100.0
        for index in range(420):
            move = random.uniform(-.003, .003)
            if index in {350, 351, 380, 381, 410, 411}:
                move *= 15
            opened = price
            closed = opened * (1 + move)
            high = max(opened, closed) * (1 + random.uniform(.0002, .0015))
            low = min(opened, closed) * (1 - random.uniform(.0002, .0015))
            quote = random.uniform(50_000, 150_000)
            candles.append(Candle("TESTUSDT", "15m", index * interval,
                                  (index + 1) * interval - 1, opened, high, low,
                                  closed, quote, quote * random.uniform(.35, .7),
                                  100, quote / opened, True))
            price = closed
        config = EngineConfig()
        fast, _ = _series_candidates(candles, 300 * interval,
                                     420 * interval, 0.01, config)
        self.assertGreater(len(fast), 0)
        authoritative = {}
        for index in range(300, 420):
            result = analyze(candles[:index + 1], config, {}, .01)
            if result["signal"] is not None:
                authoritative[result["signal"]["close_time"]] = result["signal"]
        self.assertEqual({item["signal"]["close_time"] for item in fast},
                         set(authoritative))
        for item in fast:
            expected = authoritative[item["signal"]["close_time"]]
            actual = item["signal"]
            self.assertAlmostEqual(actual["volatility_ratio"], expected["volatility_ratio"])
            self.assertAlmostEqual(actual["relative_volume"], expected["relative_volume"])
            self.assertEqual(actual["pattern"], expected["pattern"])
            self.assertEqual(actual["direction"], expected["direction"])
            self.assertEqual(actual["score_breakdown"], expected["score_breakdown"])

    def test_invalid_row_blocks_candidates_until_full_lookback_rolls_out(self):
        interval = TIMEFRAME_MS["15m"]
        candles = []
        for index in range(420):
            move = .0005
            if index in {350, 351}:
                move = .05
            opened = 100 + index * .01
            closed = opened * (1 + move)
            quote = 100_000.0
            candles.append(Candle("TESTUSDT", "15m", index * interval,
                                  (index + 1) * interval - 1, opened,
                                  max(opened, closed) * 1.001,
                                  min(opened, closed) * .999, closed, quote,
                                  quote * .55, 100, 1_000, True))
        candles[300].quote_volume = -1
        fast, stats = _series_candidates(
            candles, 301 * interval, 420 * interval, .01, EngineConfig())
        authoritative = [
            analyze(candles[:index + 1], EngineConfig(), {}, .01)["signal"]
            for index in range(301, 420)
        ]
        self.assertFalse(any(authoritative))
        self.assertEqual(fast, [])
        self.assertGreater(stats["statuses"].get("invalid_data_in_lookback", 0), 0)

    def test_listed_window_counts_unaligned_onboard_and_delivery_opens(self):
        interval = TIMEFRAME_MS["5m"]
        first, last, expected = _listed_window(
            0, 20 * interval, 60_000, 2 * interval + 30_000, interval)
        self.assertEqual(first, interval)
        self.assertEqual(last, 2 * interval + 30_000)
        self.assertEqual(expected, 2)  # opens at 5m and 10m

    def test_delivery_boundary_keeps_final_candle_symbol_in_liquidity_peers(self):
        interval = TIMEFRAME_MS["5m"]
        boundary = 100 * interval
        series = {
            "AUSDT": (array("q", [boundary - interval]), array("d", [0.0, 10.0])),
            "BUSDT": (array("q", [boundary - interval]), array("d", [0.0, 20.0])),
        }
        candidates = [{}]
        targets = {boundary: {"AUSDT": [0]}}
        with tempfile.TemporaryDirectory() as directory:
            audit = _liquidity_for_candidates(
                series, ["AUSDT", "BUSDT"], {"AUSDT": 0, "BUSDT": 0},
                {"AUSDT": boundary, "BUSDT": boundary + interval},
                targets, candidates, 0.0, Path(directory) / "progress.json")
        self.assertEqual(audit["peer_count_min"], 2)
        self.assertEqual(candidates[0]["liquidity"]["peer_count"], 2)

    def test_range_run_never_uses_pre_onboard_candles_as_signal_history(self):
        start, end = _parse_range("2026-08-01", "2026-08-02")
        interval = TIMEFRAME_MS["5m"]
        query_start = start - 7 * 86_400_000 - 2 * TIMEFRAME_MS["15m"]
        candles = []
        for opened_at in range(query_start, start + 3 * interval, interval):
            opened = 100.0
            closed = 100.05
            if opened_at >= start + interval:
                closed = 110.0
            candles.append(Candle("NEWUSDT", "5m", opened_at,
                                  opened_at + interval - 1, opened,
                                  max(opened, closed) + .1,
                                  min(opened, closed) - .1, closed,
                                  100_000, 55_000, 100, 1_000, True))
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "replay.sqlite3"
            output_path = Path(directory) / "result.json"
            store = Store(database_path)
            store.save_candles(candles)
            payload = {"symbol": "NEWUSDT", "quoteAsset": "USDT",
                       "onboardDate": start, "deliveryDate": end + 30 * 86_400_000,
                       "contractType": "PERPETUAL", "underlyingType": "COIN"}
            with store._db:
                store._db.execute(
                    "INSERT INTO universe_symbols VALUES (?,?,?,?,?,?,?)",
                    ("NEWUSDT", start, "TRADING", start, start, 1,
                     json.dumps(payload)))
            store.close()
            report = run_range(database_path, output_path, "2026-08-01", "2026-08-02")
        self.assertEqual(report["candidate_funnel"]["anomaly_events"]["all"], 0)
        pair = next(row for row in report["coverage"]["pairs"]
                    if row["symbol"] == "NEWUSDT" and row["timeframe"] == "5m")
        self.assertEqual(pair["observed_range_bars"], 3)
        self.assertEqual(pair["history_ready_range_bars"], 0)

    def test_score_only_candidate_is_counted_without_retaining_event_payload(self):
        start, end = _parse_range("2026-08-01", "2026-08-02")
        interval = TIMEFRAME_MS["5m"]
        first_open = start - 7 * 86_400_000 - 2 * TIMEFRAME_MS["15m"]
        last_signal_open = start + interval
        candles = []
        price = 100.0
        for opened_at in range(first_open, last_signal_open + interval, interval):
            opened = price
            is_pair = opened_at in {start, last_signal_open}
            closed = opened * (1.01 if is_pair else 1.0001)
            quote = 200_000.0 if is_pair else 100_000.0
            candles.append(Candle(
                "LIGHTUSDT", "5m", opened_at, opened_at + interval - 1,
                opened, max(opened, closed) * 1.0001,
                min(opened, closed) * .9999, closed, quote, quote * .60,
                100, quote / opened, True))
            price = closed
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "light.sqlite3"
            store = Store(database_path)
            store.save_candles(candles)
            payload = {"symbol": "LIGHTUSDT", "quoteAsset": "USDT",
                       "onboardDate": first_open,
                       "deliveryDate": end + 30 * 86_400_000,
                       "contractType": "PERPETUAL", "underlyingType": "COIN"}
            with store._db:
                store._db.execute(
                    "INSERT INTO universe_symbols VALUES (?,?,?,?,?,?,?)",
                    ("LIGHTUSDT", first_open, "TRADING", start, start, 1,
                     json.dumps(payload)))
            store.close()
            report = run_range(database_path, Path(directory) / "light.json",
                               "2026-08-01", "2026-08-02")
        anomalies = report["candidate_funnel"]["anomaly_events"]["5m"]
        self.assertGreaterEqual(anomalies, 1)
        self.assertEqual(report["events"], [])
        self.assertEqual(report["enrichment_audit"]["counts"]["anomaly_candidates"], anomalies)
        self.assertEqual(report["candidate_funnel"]["max_scores"]["5m"]["comparison_score"], 60)
        self.assertTrue(report["self_checks"]["all_anomalies_accounted_after_memory_pruning"])
        self.assertTrue(report["self_checks"]["all_sensitivity_buckets_match_anomalies"])

    def test_one_day_range_events_match_authoritative_daily_replay(self):
        random.seed(81)
        start, end = _parse_range("2026-08-01", "2026-08-02")
        interval = TIMEFRAME_MS["15m"]
        first_open = start - 7 * 86_400_000 - 2 * interval
        candles = []
        price = 100.0
        count = (end + 5 * interval - first_open) // interval
        for index in range(count):
            opened_at = first_open + index * interval
            move = random.uniform(-.001, .001)
            if opened_at in {start + 10 * interval, start + 11 * interval,
                             start + 40 * interval, start + 41 * interval}:
                move *= 40
            opened = price
            closed = opened * (1 + move)
            quote = random.uniform(80_000, 130_000)
            candles.append(Candle(
                "TESTUSDT", "15m", opened_at, opened_at + interval - 1,
                opened, max(opened, closed) * 1.001,
                min(opened, closed) * .999, closed, quote, quote * .56,
                100, quote / opened, True))
            price = closed
        found, _ = _series_candidates(candles, start, end, .01, EngineConfig())
        self.assertTrue(found)
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "parity.sqlite3"
            store = Store(database_path)
            store.save_candles(candles)
            payload = {"symbol": "TESTUSDT", "quoteAsset": "USDT",
                       "onboardDate": first_open - 86_400_000,
                       "deliveryDate": end + 30 * 86_400_000,
                       "contractType": "PERPETUAL", "underlyingType": "COIN",
                       "filters": [{"filterType": "PRICE_FILTER", "tickSize": ".01"}]}
            with store._db:
                store._db.execute(
                    "INSERT INTO universe_symbols VALUES (?,?,?,?,?,?,?)",
                    ("TESTUSDT", payload["onboardDate"], "TRADING", start,
                     start, 1, json.dumps(payload)))
                for item in found:
                    signal = item["signal"]
                    alert = {"source": "live", "activity_score": 90}
                    store._db.execute(
                        "INSERT INTO alerts VALUES (?,?,?,?,?,?)",
                        (signal["id"], "TESTUSDT", "15m", signal["close_time"],
                         signal["close_time"], json.dumps(alert)))
            store.close()
            daily = run_daily(database_path, Path(directory) / "daily.json", "2026-08-01")
            ranged = run_range(database_path, Path(directory) / "range.json",
                               "2026-08-01", "2026-08-02")
        self.assertEqual(daily["candidate_funnel"]["anomaly_events"],
                         ranged["candidate_funnel"]["anomaly_events"])
        daily_events = {event["event_key"]: event for event in daily["events"]}
        range_events = {event["event_key"]: event for event in ranged["events"]}
        self.assertEqual(set(daily_events), set(range_events))
        for key in daily_events:
            self.assertEqual(daily_events[key]["methods"], range_events[key]["methods"])
            self.assertEqual(daily_events[key]["entry_direction"],
                             range_events[key]["entry_direction"])
            for horizon in ("1", "2", "3", "5"):
                self.assertEqual(daily_events[key]["forward"][horizon]["available"],
                                 range_events[key]["forward"][horizon]["available"])
                if daily_events[key]["forward"][horizon]["available"]:
                    self.assertAlmostEqual(
                        daily_events[key]["forward"][horizon]["net_return_pct"],
                        range_events[key]["forward"][horizon]["net_return_pct"])

    def test_range_is_end_exclusive_and_ordered(self):
        start, end = _parse_range("2026-08-01", "2026-09-02")
        self.assertLess(start, end)
        with self.assertRaises(ValueError):
            _parse_range("2026-09-02", "2026-09-02")

    def test_explicit_scope_includes_settled_range_intersection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scope.json"
            path.write_text(json.dumps({"symbols": [
                {"symbol": "OLDUSDT", "quoteAsset": "USDT",
                 "onboardDate": 1_700_000_000_000,
                 "deliveryDate": 1_800_000_000_000},
                {"symbol": "AFTERUSDT", "quoteAsset": "USDT",
                 "onboardDate": 1_900_000_000_000,
                 "deliveryDate": 2_000_000_000_000},
                {"symbol": "COINUSD", "quoteAsset": "USD",
                 "onboardDate": 1_700_000_000_000,
                 "deliveryDate": 1_800_000_000_000},
            ]}), encoding="utf-8")
            metadata, source = _metadata_payloads(
                connection, 1_750_000_000_000, 1_760_000_000_000, path)
        self.assertEqual(set(metadata), {"OLDUSDT"})
        self.assertIn("explicit scope metadata", source)


if __name__ == "__main__":
    unittest.main()
