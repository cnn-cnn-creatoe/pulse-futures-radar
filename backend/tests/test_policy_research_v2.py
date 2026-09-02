"""Policy/research API contracts; all data and jobs remain isolated and offline."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from monitor.collector import MonitorService
from monitor.engine import Candle, MONITOR_TIMEFRAMES, TIMEFRAME_MS
from monitor.policy import Settings, THRESHOLD_PRIORS, apply_policy
from monitor.backtest import run_backtest


HEADERS = {"X-Pulse-Client": "dashboard", "Origin": "http://127.0.0.1:5173"}


def candles(count=3, timeframe="5m", start=0):
    step = TIMEFRAME_MS[timeframe]
    return [Candle("BTCUSDT", timeframe, start + i * step, start + (i + 1) * step - 1,
                   100, 102, 99, 101, 1000, 600, 20, 10) for i in range(count)]


def signal_for(series, ratio=4):
    last = series[-1]
    return {"symbol": last.symbol, "timeframe": last.timeframe, "close_time": last.close_time,
            "pattern": "阳阳", "direction": "up", "price": last.close, "volatility_ratio": ratio,
            "activity_score": 90, "relative_volume": 3, "liquidity_percentile": .8, "spread_bps": 2,
            "source": "live"}


class PolicyResearchApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"PULSE_DATA_DIR": self.folder.name, "PULSE_DISABLE_COLLECTOR": "1"})
        self.environment.start()
        self.collector_patch = patch.object(MonitorService, "start", side_effect=AssertionError("collector must never start in these tests"))
        self.collector_start = self.collector_patch.start()
        self.network_patch = patch("monitor.history_data._download", side_effect=AssertionError("real archive network is forbidden in these tests"))
        self.network_download = self.network_patch.start()
        self.module_name = "monitor._isolated_policy_research_api_test"
        spec = importlib.util.spec_from_file_location(self.module_name, Path(__file__).resolve().parents[1] / "monitor" / "api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.api
        spec.loader.exec_module(self.api)
        self.client = TestClient(self.api.app)
        self.client.__enter__()

    def tearDown(self):
        try:
            self.client.__exit__(None, None, None)
            self.collector_start.assert_not_called()
            self.network_download.assert_not_called()
        finally:
            self.network_patch.stop()
            self.collector_patch.stop()
            self.environment.stop()
            sys.modules.pop(self.module_name, None)
            self.folder.cleanup()

    def import_sample(self):
        response = self.client.post("/api/history/import?symbol=BTCUSDT&timeframe=5m&format=json",
                                    content=json.dumps([asdict(c) for c in candles()]), headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_default_policy_catalog_and_unvalidated_threshold_labels(self):
        data = self.client.get("/api/settings").json()
        self.assertEqual(data["indicators"]["enabled_indicators"], ["supertrend", "smc"])
        self.assertEqual(data["indicators"]["confirmation_mode"], "all")
        self.assertEqual(data["quality"]["min_activity_score"], 80)
        self.assertEqual(data["quality"]["min_risk_reward"], 2)
        self.assertFalse(data["quality"]["allow_missing_liquidity"])
        self.assertEqual(data["threshold_priors"], {tf:THRESHOLD_PRIORS[tf] for tf in MONITOR_TIMEFRAMES})
        self.assertEqual(data["threshold_prior_status"], "unvalidated_prior")
        self.assertEqual(data["policy_hash"], self.api.policy.fingerprint())
        catalog = self.client.get("/api/indicators").json()
        self.assertEqual({item["id"] for item in catalog["items"]}, {"supertrend", "smc", "ema"})
        self.assertFalse(catalog["custom_code_execution"])
        self.assertFalse(data["telegram_enabled"])

    def test_custom_indicator_editor_validates_and_saves_without_arbitrary_code(self):
        spec = {"id":"rsi_gate","name":"RSI 动量确认","description":"安全表达式示例",
                "long_expression":"rsi(14) > 55 and close > ema(20)",
                "short_expression":"rsi(14) < 45 and close < ema(20)"}
        validated = self.client.post("/api/custom-indicators/validate", json=spec, headers=HEADERS)
        self.assertEqual(validated.status_code, 200, validated.text)
        self.assertTrue(validated.json()["valid"])
        self.assertEqual(validated.json()["required_history"], 20)
        saved = self.client.post("/api/custom-indicators", json={**spec,"status":"validated"}, headers=HEADERS)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["item"]["status"], "validated")
        self.assertFalse(saved.json()["automatic_live_enable"])
        listed = self.client.get("/api/custom-indicators").json()
        self.assertEqual([item["id"] for item in listed["items"]], ["rsi_gate"])
        self.assertFalse(listed["automatic_live_enable"])

        malicious = {**spec,"id":"bad_code","long_expression":"__import__('os').system('whoami') > 0"}
        report = self.client.post("/api/custom-indicators/validate", json=malicious, headers=HEADERS)
        self.assertEqual(report.status_code, 200, report.text)
        self.assertFalse(report.json()["valid"])
        rejected = self.client.post("/api/custom-indicators", json={**malicious,"status":"validated"}, headers=HEADERS)
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual([item["id"] for item in self.client.get("/api/custom-indicators").json()["items"]], ["rsi_gate"])

    def test_select_one_or_many_indicators_persists_and_changes_fingerprint(self):
        original_hash = self.api.policy.fingerprint()
        for selection, mode in (([], "all"), (["ema"], "all"), (["supertrend", "smc", "ema"], "any")):
            requested = self.api.policy.model_dump()
            requested["indicators"].update(enabled_indicators=selection, confirmation_mode=mode)
            response = self.client.put("/api/settings", json=requested, headers=HEADERS)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["indicators"]["enabled_indicators"], selection)
            self.assertEqual(self.api.load_policy().model_dump(), self.api.policy.model_dump())
            self.assertEqual(json.loads(self.api.CONFIG_FILE.read_text("utf-8")), self.api.policy.model_dump())
            self.assertNotEqual(result["policy_hash"], original_hash)
            self.assertFalse(self.api.CONFIG_FILE.with_suffix(".tmp").exists())

    def test_invalid_ema_duplicate_or_unknown_indicator_never_persists(self):
        original = self.api.policy.model_dump()
        original_hash = self.api.policy.fingerprint()
        changes = [{"enabled_indicators": ["ema", "ema"]},
                   {"enabled_indicators": ["upload_python"]}, {"ema_fast_period": 60, "ema_slow_period": 50},
                   {"smc_internal_length": 20, "smc_swing_length": 10}, {"confirmation_mode": "majority"}]
        for fields in changes:
            request = deepcopy(original)
            request["indicators"].update(fields)
            with self.subTest(fields=fields):
                self.assertEqual(self.client.put("/api/settings", json=request, headers=HEADERS).status_code, 422)
                self.assertEqual(self.api.policy.fingerprint(), original_hash)
                self.assertFalse(self.api.CONFIG_FILE.exists())

    def test_demo_policy_preview_tracks_saved_indicator_selection_and_quality(self):
        request = self.api.policy.model_dump()
        request["indicators"]["enabled_indicators"] = ["ema"]
        request["quality"]["min_activity_score"] = 100
        self.assertEqual(self.client.put("/api/settings", json=request, headers=HEADERS).status_code, 200)
        rows = self.client.get("/api/signals?mode=demo").json()["items"]
        self.assertEqual(len(rows), 8)
        self.assertTrue(all([item["id"] for item in row["confirmations"]] == ["ema"] for row in rows))
        self.assertTrue(all(not row["qualified"] for row in rows))

    def test_policy_thresholds_use_maximum_of_global_and_timeframe(self):
        for timeframe, prior in THRESHOLD_PRIORS.items():
            series = candles(timeframe=timeframe)
            for global_min in (1.5, 2.8):
                result = apply_policy(signal_for(series, ratio=1.6), series, Settings(volatility_ratio_min=global_min))
                self.assertEqual(result["threshold_used"], max(global_min, prior))
                self.assertEqual(result["threshold_status"], "unvalidated_prior")
                if 1.6 < max(global_min, prior):
                    self.assertFalse(result["qualified"])
                    self.assertTrue(any("本周期门槛" in reason for reason in result["quality_reasons"]))
            manual = apply_policy(signal_for(series), series, Settings(volatility_ratio_min=2.3, threshold_profile="manual"))
            self.assertEqual(manual["threshold_used"], 2.3)
            self.assertEqual(manual["threshold_status"], "user_defined")

    def test_policy_snapshot_and_queued_transform_stay_frozen(self):
        settings = Settings()
        original = settings.model_dump()
        fingerprint = settings.fingerprint()
        transform = self.api.freeze_transform(settings)
        settings.indicators.enabled_indicators = ["ema"]
        settings.volatility_ratio_min = 4
        series = candles()
        result = self.client.portal.call(transform, signal_for(series), series, {})
        self.assertEqual(result["policy_hash"], fingerprint)
        self.assertEqual(result["policy_snapshot"], original)
        self.assertEqual(result["quality_schema_version"], 2)
        self.assertNotIn("indicator_context", result)
        self.assertEqual(result["threshold_used"], THRESHOLD_PRIORS["5m"])
        snapshot_before = deepcopy(result)
        self.api.store.save_alert(result)
        request = self.api.policy.model_dump()
        request["quality"]["min_activity_score"] = 99
        self.client.put("/api/settings", json=request, headers=HEADERS)
        saved = self.client.get("/api/signals?qualified=false").json()["items"][0]
        self.assertEqual(saved["policy_hash"], fingerprint)
        self.assertEqual(saved["policy_snapshot"], snapshot_before["policy_snapshot"])

    def test_qualified_feed_never_promotes_old_unscored_or_wrong_type_records(self):
        cases = [({}, "LEGACYUSDT"), ({"qualified": True}, "UNVERSIONEDUSDT"),
                 ({"qualified": True, "quality_schema_version": 1}, "OLDUSDT"),
                 ({"qualified": True, "quality_schema_version": 2}, "READYUSDT"),
                 ({"qualified": False, "quality_schema_version": 2}, "REJECTEDUSDT"),
                 ({"qualified": 1, "quality_schema_version": 2}, "NUMBERUSDT"),
                 ({"qualified": "true", "quality_schema_version": 2}, "STRINGUSDT"),
                 ({"qualified": True, "quality_schema_version": True}, "BOOLVERSIONUSDT")]
        for i, (fields, symbol) in enumerate(cases):
            self.api.store.save_alert({"source": "live", "symbol": symbol, "timeframe": "1m", "close_time": (i + 1) * 60000 - 1, **fields})
        qualified = self.client.get("/api/signals?qualified=true&limit=1").json()["items"]
        self.assertEqual([row["symbol"] for row in qualified], ["READYUSDT"])
        rejected = {row["symbol"] for row in self.client.get("/api/signals?qualified=false").json()["items"]}
        self.assertTrue({"LEGACYUSDT", "UNVERSIONEDUSDT", "REJECTEDUSDT", "NUMBERUSDT", "STRINGUSDT", "BOOLVERSIONUSDT"} <= rejected)
        self.assertNotIn("READYUSDT", rejected)
        self.assertEqual(len(self.client.get("/api/signals").json()["items"]), len(cases))

    def test_listing_endpoint_has_no_first_snapshot_flood_and_keeps_new_events(self):
        old = {"symbol": "BTCUSDT", "status": "TRADING", "onboardDate": 100, "quoteAsset": "USDT", "baseAsset": "BTC"}
        self.assertEqual(self.api.store.reconcile_universe([old], 1000), [])
        baseline = self.client.get("/api/listings").json()
        self.assertTrue(baseline["snapshot"]["initialized"])
        self.assertEqual(baseline["items"], [])
        new = {"symbol": "NEWUSDC", "status": "TRADING", "onboardDate": 1500, "quoteAsset": "USDC", "baseAsset": "NEW"}
        self.api.store.reconcile_universe([old, new], 2000)
        self.api.store.reconcile_universe([old, new], 3000)
        result = self.client.get("/api/listings").json()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["symbol"], "NEWUSDC")
        self.assertEqual(result["items"][0]["event_type"], "listed")
        self.assertFalse(result["items"][0]["telegram_sent"])
        self.assertFalse(result["telegram_enabled"])

    def test_dataset_registration_sha_load_and_isolation_from_live(self):
        registered = self.import_sample()
        self.assertRegex(registered["id"], r"^[a-f0-9]{24}$")
        self.assertRegex(registered["sha256"], r"^[a-f0-9]{64}$")
        path = self.api.research.root / (registered["id"] + ".json")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), registered["sha256"])
        loaded, meta = self.api.research.load(registered["id"])
        self.assertEqual(loaded, candles())
        self.assertEqual(meta, registered)
        listing = self.client.get("/api/history").json()
        self.assertEqual(listing["source"], "historical")
        self.assertEqual(listing["items"], [registered])
        self.assertFalse(listing["busy"])
        self.assertFalse(registered["provenance"]["verified_checksum"])
        self.assertFalse(registered["has_historical_oi"])
        self.assertFalse(registered["has_historical_spread"])
        self.assertEqual(self.api.store.counts(), {"candles": 0, "alerts": 0, "oi_snapshots": 0})
        self.assertEqual(list(self.api.research.root.glob("*.tmp")), [])

    def test_dataset_tampering_and_path_traversal_are_rejected(self):
        registered = self.import_sample()
        path = self.api.research.root / (registered["id"] + ".json")
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "校验"):
            self.api.research.load(registered["id"])
        for identity in ("../settings", "../../outside.json", "a" * 24 + "/../../settings", "A" * 24):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.api.research.load(identity)
        self.assertEqual(self.client.post("/api/backtests", json={"dataset_id": "../settings"}, headers=HEADERS).status_code, 422)
        self.assertIsNone(self.api.research.get_job("../../settings"))

    def test_import_rejects_invalid_format_symbol_identity_future_and_quota(self):
        base = "/api/history/import?symbol=BTCUSDT&timeframe=5m&format=json"
        valid = [asdict(c) for c in candles()]
        step = TIMEFRAME_MS["5m"]
        future_start = (int(time.time() * 1000) // step + 2) * step
        malformed = [("/api/history/import?symbol=BTCUSDT&timeframe=5m&format=xml", "[]", 422),
                     ("/api/history/import?symbol=BTC-USDT&timeframe=5m&format=json", json.dumps(valid), 400),
                     ("/api/history/import?symbol=BTCUSDT&timeframe=3m&format=json", json.dumps(valid), 400),
                     (base, json.dumps([{**valid[0], "symbol": "ETHUSDT"}]), 400),
                     (base, json.dumps([asdict(c) for c in candles(1, start=future_start)]), 400),
                     (base, json.dumps([{**valid[0], "closed": False}]), 400),
                     (base, "{", 400), (base, "[]", 400)]
        for route, raw, status in malformed:
            with self.subTest(route=route, status=status):
                response = self.client.post(route, content=raw, headers=HEADERS)
                self.assertEqual(response.status_code, status, response.text)
                self.assertFalse(self.api.research.busy)
                self.assertEqual(self.api.research.datasets(), [])
        with patch.object(self.api, "parse_candles_json", side_effect=AssertionError("oversized input must be rejected before parsing")):
            too_large = self.client.post(base, content=b" " * 20_000_001, headers=HEADERS)
        self.assertEqual(too_large.status_code, 413)
        self.assertFalse(self.api.research.busy)
        self.assertEqual(self.api.research.datasets(), [])

    def test_dataset_candle_quota_prevents_registration_writes(self):
        with self.assertRaisesRegex(ValueError, "120,000"):
            self.api.research.register(candles(120001), {"type": "test"})
        self.assertEqual(self.api.research.datasets(), [])
        self.assertEqual(list(self.api.research.root.glob("*.json")), [])

    def test_every_new_write_endpoint_requires_dashboard_marker_and_origin(self):
        payloads = [("/api/connection/retry", {}),
                    ("/api/history/download", {"symbol": "BTCUSDT", "timeframe": "5m", "months": ["2024-01"]}),
                    ("/api/backtests", {"dataset_id": "a" * 24})]
        bad_headers = ({}, {"X-Pulse-Client": "dashboard", "Origin": "https://attacker.example"},
                       {"X-Pulse-Client": "dashboard", "Origin": "null"})
        with patch.object(self.api.research, "start", side_effect=AssertionError("unauthorized jobs must never start")), \
                patch.object(self.api.service, "retry_connection", new=AsyncMock()) as retry:
            for route, payload in payloads:
                for headers in bad_headers:
                    with self.subTest(route=route, headers=headers):
                        self.assertEqual(self.client.post(route, json=payload, headers=headers).status_code, 403)
            for headers in bad_headers:
                response = self.client.post("/api/history/import?symbol=BTCUSDT&timeframe=5m&format=json", content="[]", headers=headers)
                self.assertEqual(response.status_code, 403)
            retry.assert_not_awaited()
        self.assertEqual(self.api.research.jobs, {})
        self.assertFalse(self.api.research.busy)

    def test_download_month_selection_and_candle_budget_are_bounded_before_jobs(self):
        current = datetime.now(timezone.utc).strftime("%Y-%m")
        base = {"symbol": "BTCUSDT", "timeframe": "5m", "months": ["2024-01"]}
        thirteen_months = [f"2024-{month:02d}" for month in range(1, 13)] + ["2025-01"]
        rejected = [({"months": []}, 422), ({"months": thirteen_months}, 422),
                    ({"months": ["2024-00"]}, 400), ({"months": ["2024-13"]}, 400),
                    ({"months": [current]}, 400), ({"months": ["2099-01"]}, 400),
                    ({"months": ["../../settings"]}, 400), ({"symbol": "BTC-USDT"}, 422),
                    ({"timeframe": "3m"}, 422),
                    ({"url": "https://attacker.example/archive.zip"}, 422)]
        with patch.object(self.api.research, "start", return_value={"id": "b" * 32, "state": "running"}) as start:
            for changes, status in rejected:
                with self.subTest(changes=changes):
                    response = self.client.post("/api/history/download", json={**base, **changes}, headers=HEADERS)
                    self.assertEqual(response.status_code, status, response.text)
            start.assert_not_called()
            self.assertEqual(self.client.post("/api/history/download", json=base, headers=HEADERS).status_code, 200)
            self.assertEqual(self.client.post("/api/history/download", json={**base, "months": ["2024-01", "2024-02", "2024-03"]}, headers=HEADERS).status_code, 200)
            self.assertEqual(self.client.post("/api/history/download", json={**base, "timeframe": "15m", "months": ["2024-01", "2024-02"]}, headers=HEADERS).status_code, 200)
            self.assertEqual(start.call_count, 3)

    def test_download_worker_sorts_and_deduplicates_trusted_months_without_network(self):
        calls = []

        def archive(symbol, timeframe, month, destination):
            calls.append((symbol, timeframe, month))
            index = 0 if month == "2024-01" else 1
            return {"candles": candles(1, timeframe=timeframe, start=index * TIMEFRAME_MS[timeframe]),
                    "metadata": {"month": month, "checksum_verified": True}}

        with patch("monitor.research.download_binance_month", side_effect=archive):
            record = self.api.research._work("download", {"symbol": "BTCUSDT", "timeframe": "5m", "months": ["2024-02", "2024-01", "2024-02"]}, self.api.policy.model_dump())
        self.assertEqual(calls, [("BTCUSDT", "5m", "2024-01"), ("BTCUSDT", "5m", "2024-02")])
        self.assertEqual(record["bars"], 2)
        self.assertTrue(record["provenance"]["verified_checksum"])
        self.assertEqual(record["provenance"]["type"], "binance_official_archive")

    def test_completed_backtest_exports_chinese_summary_and_trade_csv(self):
        job_id = "c" * 32
        result = run_backtest(candles(3))
        result["policy_hash"] = self.api.policy.fingerprint()
        result["provenance"] = {"provenance": {"type": "test_verified_archive"}}
        self.api.research.jobs[job_id] = {"id": job_id, "kind": "backtest",
            "state": "completed", "result": result}
        summary = self.client.get(f"/api/backtests/{job_id}/summary.csv")
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertTrue(summary.content.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\r\n", summary.content)
        decoded = summary.content.decode("utf-8-sig")
        self.assertIn("指标组合", decoded)
        self.assertIn("滚动时间外验证", decoded)
        trades = self.client.get(f"/api/backtests/{job_id}/trades.csv")
        self.assertEqual(trades.status_code, 200, trades.text)
        self.assertIn("信号收线UTC", trades.content.decode("utf-8-sig"))
        entry_events = self.api.research.root / f"job-{job_id}.entry-events.csv"
        entry_summary = self.api.research.root / f"job-{job_id}.entry-summary.csv"
        entry_events.write_bytes(b"\xef\xbb\xbfentry-events\r\n")
        entry_summary.write_bytes(b"\xef\xbb\xbfentry-summary\r\n")
        self.assertEqual(
            self.client.get(f"/api/backtests/{job_id}/entry-events.csv").content,
            entry_events.read_bytes(),
        )
        self.assertEqual(
            self.client.get(f"/api/backtests/{job_id}/entry-summary.csv").content,
            entry_summary.read_bytes(),
        )
        self.api.research.jobs[job_id]["state"] = "running"
        self.assertEqual(self.client.get(f"/api/backtests/{job_id}/summary.csv").status_code, 409)
        self.assertEqual(self.client.get(f"/api/backtests/{job_id}/entry-events.csv").status_code, 409)

    def test_busy_research_rejects_conflicting_import_and_jobs_and_releases(self):
        registered = self.import_sample()
        release = threading.Event()
        entered = threading.Event()
        snapshots = []

        def work(kind, payload, snapshot):
            snapshots.append(deepcopy(snapshot))
            entered.set()
            if not release.wait(5):
                raise AssertionError("test worker was not released")
            return {"source": "isolated_test", "not_a_market_backtest": True}

        original = self.api.policy.model_dump()
        with patch.object(self.api.research, "_work", side_effect=work):
            try:
                response = self.client.post("/api/backtests", json={"dataset_id": registered["id"]}, headers=HEADERS)
                self.assertEqual(response.status_code, 200, response.text)
                job_id = response.json()["id"]
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.client.get("/api/history").json()["busy"])
                blocked_import = self.client.post("/api/history/import?symbol=BTCUSDT&timeframe=5m&format=json", content="[]", headers=HEADERS)
                self.assertEqual(blocked_import.status_code, 409)
                blocked_job = self.client.post("/api/backtests", json={"dataset_id": registered["id"]}, headers=HEADERS)
                self.assertEqual(blocked_job.status_code, 400)
                changed = deepcopy(original)
                changed["quality"]["min_activity_score"] = 99
                self.client.put("/api/settings", json=changed, headers=HEADERS)
                self.assertEqual(snapshots, [original])
            finally:
                release.set()
                self.client.portal.call(self.api.research.close)
        self.assertFalse(self.api.research.busy)
        job = self.client.get("/api/jobs/" + job_id).json()
        self.assertEqual(job["state"], "completed")
        self.assertTrue((self.api.research.root / ("job-" + job_id + ".report.json")).exists())


if __name__ == "__main__":
    unittest.main()
