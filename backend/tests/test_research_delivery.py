import csv
import json
from pathlib import Path
import tempfile
import unittest

from monitor.backtest import run_backtest
from monitor.engine import Candle, TIMEFRAME_MS
from monitor.research import Research, _csv_text, backtest_trades_csv
from monitor.research_delivery import (deliver_batch, derive_directional_entry_study,
                                       derive_long_event_study, validate_csv_bytes)


def report(timeframe):
    step = TIMEFRAME_MS[timeframe]
    candles = [Candle("BTCUSDT", timeframe, i * step, (i + 1) * step - 1,
                      100, 102, 99, 101, 1000, 600, 20, 10) for i in range(20)]
    result = run_backtest(candles)
    result["policy_hash"] = "test-policy"
    result["provenance"] = {"provenance": {"type": "test_verified_archive"}}
    return {"kind": "backtest", "state": "completed", "result": result}


class DeliveryTests(unittest.TestCase):
    def test_long_event_study_filters_deduplicates_and_counts_cost_after_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wrapper = {"kind": "backtest", "state": "completed", "result": {
                "dataset": {"symbol": "BTCUSDT", "timeframe": "5m", "first_close_time": 1,
                            "last_close_time": 999999},
                "indicator_config": {"atr_period": 10, "atr_multiplier": 3, "atr_method": "wilder",
                                     "smc_internal_length": 5, "smc_swing_length": 50},
                "quality": {"fee_bps_per_side": 5, "slippage_bps_per_side": 3},
                "indicator_study": {"execution_assumptions": {"fee_bps_per_side": 5,
                                      "slippage_bps_per_side": 3}}, "policy_hash": "abc"}}
            report_path = root / "sample.report.json"
            report_path.write_text(json.dumps(wrapper), "utf-8")
            headers = ["标的", "周期", "指标编号", "策略口径", "验证分段", "信号收线UTC", "入场UTC",
                       "方向", "入场价", "两根K线形态", "异常倍数", "活跃度", "政策哈希",
                       "前瞻1根净收益%", "前瞻2根净收益%", "前瞻3根净收益%", "前瞻5根净收益%"]
            base = {"标的": "BTCUSDT", "周期": "5m", "指标编号": "supertrend_smc",
                    "策略口径": "K线价格指标消融（不代表完整实盘策略）", "方向": "做多", "入场价": "100",
                    "两根K线形态": "阳阳", "异常倍数": "2.5", "活跃度": "70", "政策哈希": "abc"}
            first = {**base, "验证分段": "训练集", "信号收线UTC": "a", "入场UTC": "a+1",
                     "前瞻1根净收益%": "1", "前瞻2根净收益%": "-0.5", "前瞻3根净收益%": "0", "前瞻5根净收益%": "2"}
            duplicate = {**first, "验证分段": "滚动时间外验证"}
            second = {**base, "验证分段": "时间外验证集", "信号收线UTC": "b", "入场UTC": "b+1",
                      "前瞻1根净收益%": "-1", "前瞻2根净收益%": "0.2", "前瞻3根净收益%": "0.3", "前瞻5根净收益%": ""}
            ignored = {**second, "信号收线UTC": "short", "方向": "做空"}
            trades_path = root / "trades.csv"
            trades_path.write_bytes(_csv_text(headers, [first, duplicate, second, ignored]).encode("utf-8"))
            events, summary = root / "events.csv", root / "summary.csv"
            result = derive_long_event_study(report_path, trades_path, events, summary)
            self.assertEqual(result["unique_buy_points"], 2)
            with summary.open(encoding="utf-8-sig", newline="") as stream:
                rows = {row["观察窗口"]: row for row in csv.DictReader(stream)}
            self.assertEqual(float(rows["买入后第1根K线收盘"]["胜率%"]), 50)
            self.assertEqual(float(rows["买入后第2根K线收盘"]["胜率%"]), 50)
            self.assertEqual(rows["买入后第5根K线收盘"]["有效结果数"], "1")
            self.assertEqual(rows["买入后第1根K线收盘"]["样本是否充足"], "否")
            entry_events, entry_summary = root / "entry-events.csv", root / "entry-summary.csv"
            directional = derive_directional_entry_study(
                report_path, trades_path, entry_events, entry_summary)
            self.assertEqual(directional["unique_entries"], 3)
            self.assertEqual(directional["direction_counts"], {"做多": 2, "做空": 1})
            with entry_summary.open(encoding="utf-8-sig", newline="") as stream:
                entry_rows = {(row["方向"], row["观察窗口"]): row for row in csv.DictReader(stream)}
            self.assertEqual(float(entry_rows[("做多", "入场后第1根K线收盘")]["胜率%"]), 50)
            self.assertEqual(float(entry_rows[("做空", "入场后第1根K线收盘")]["胜率%"]), 0)
            self.assertEqual(validate_csv_bytes(entry_events)["rows"], 3)

    def test_normalizes_merges_and_publishes_research_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, research = Path(folder) / "batch", Path(folder) / "research"
            batch.mkdir()
            items = {}
            for timeframe in ("5m", "15m"):
                value = report(timeframe)
                stem = f"BTCUSDT-{timeframe}-2025-08_2026-07"
                path = batch / f"{stem}.report.json"
                path.write_text(json.dumps(value), "utf-8")
                # Deliberately create CRCRLF to exercise normalization.
                trades = backtest_trades_csv(value).encode("utf-8").replace(b"\r\n", b"\r\r\n")
                (batch / f"{stem}.trades.csv").write_bytes(trades)
                items[f"BTCUSDT:{timeframe}"] = {"elapsed_seconds": 1}
            (batch / "batch.manifest.json").write_text(json.dumps({"items": items}), "utf-8")
            delivered = deliver_batch(batch, research)
            self.assertEqual(len(delivered["published_jobs"]), 2)
            self.assertEqual(delivered["merged_summary"]["rows"], 12)
            self.assertEqual(delivered["merged_trades"]["rows"], 0)
            self.assertEqual(delivered["merged_entry_summary"]["rows"], 16)
            for path in list(batch.glob("*.csv")) + list(research.glob("*.csv")):
                audit = validate_csv_bytes(path)
                self.assertTrue(audit["utf8_bom"])
                self.assertFalse(audit["crcrlf"])
            store = Research(research)
            visible = store.reports()
            self.assertEqual(len(visible), 2)
            for item in delivered["published_jobs"]:
                self.assertEqual(store.get_job(item["id"])["state"], "completed")
                saved = json.loads(Path(item["report"]).read_text("utf-8"))
                self.assertEqual(saved["result"]["trade_ledger"]["storage"], "separate_csv")


if __name__ == "__main__":
    unittest.main()
