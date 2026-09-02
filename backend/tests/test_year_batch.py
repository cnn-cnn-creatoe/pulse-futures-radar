import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor.engine import Candle, TIMEFRAME_MS
from monitor.year_batch import YearBatch, month_range


class YearBatchTests(unittest.TestCase):
    def test_month_range_is_chronological_and_bounded(self):
        self.assertEqual(month_range("2024-11", "2025-02"),
                         ["2024-11", "2024-12", "2025-01", "2025-02"])
        with self.assertRaises(ValueError):
            month_range("2024-01", "2025-01")
        with self.assertRaises(ValueError):
            month_range("2025-02", "2025-01")

    def test_completed_item_resumes_without_repeating_backtest(self):
        step = TIMEFRAME_MS["5m"]
        starts = {"2024-01": 0, "2024-02": step}

        def archive(symbol, timeframe, month, destination):
            start = starts[month]
            candle = Candle(symbol, timeframe, start, start + step - 1,
                            100, 102, 99, 101, 1000, 600, 20, 10)
            return {"candles": [candle], "metadata": {"month": month,
                    "checksum_verified": True, "cache_hit": False}}

        report = {"status": "insufficient_data", "dataset": {"symbol": "BTCUSDT",
                  "timeframe": "5m", "bars": 2, "first_close_time": step - 1,
                  "last_close_time": step * 2 - 1}, "indicator_study": {"rows": []}}
        with tempfile.TemporaryDirectory() as folder:
            with patch("monitor.year_batch.download_binance_month", side_effect=archive) as download, \
                 patch("monitor.year_batch.run_backtest", return_value=report) as backtest:
                first = YearBatch(folder, ["BTCUSDT"], ["5m"], ["2024-01", "2024-02"]).run()
                self.assertEqual(first["state"], "completed")
                self.assertEqual(download.call_count, 2)
                backtest.assert_called_once()
            item = first["items"]["BTCUSDT:5m"]
            self.assertTrue(Path(item["summary_csv"]).exists())
            self.assertTrue(Path(item["trades_csv"]).exists())
            with patch("monitor.year_batch.download_binance_month") as download, \
                 patch("monitor.year_batch.run_backtest") as backtest:
                resumed = YearBatch(folder, ["BTCUSDT"], ["5m"], ["2024-01", "2024-02"]).run()
                self.assertEqual(resumed["state"], "completed")
                download.assert_not_called()
                backtest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
