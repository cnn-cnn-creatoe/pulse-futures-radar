"""Exact persisted download inventory; every database is temporary or in-memory."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from monitor.engine import Candle, TIMEFRAME_MS
from monitor.store import Store


MINUTE = TIMEFRAME_MS["1m"]
DAY = TIMEFRAME_MS["1d"]


def bar(start=0, symbol="BTCUSDT", timeframe="1m", **changes):
    values = {"symbol": symbol, "timeframe": timeframe, "open_time": start,
              "close_time": start + TIMEFRAME_MS[timeframe] - 1,
              "open": 100, "high": 103, "low": 99, "close": 101,
              "quote_volume": 1000, "taker_buy_quote": 600, "trades": 10,
              "volume": 10, "closed": True}
    return Candle(**{**values, **changes})


class DownloadStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_empty_inventory_and_window_have_no_invented_time_or_price(self):
        self.assertEqual(self.store.candle_inventory(), [])
        self.assertEqual(self.store.candle_window_stats("BTCUSDT", "1m"), {
            "symbol": "BTCUSDT", "timeframe": "1m", "bars": 0,
            "first_open_time": None, "last_open_time": None, "last_close_time": None,
            "last_price": None, "latest_candle": None,
        })
        self.assertEqual(self.store.counts(), {"candles": 0, "alerts": 0, "oi_snapshots": 0})

    def test_upserts_duplicate_pages_and_open_bars_do_not_inflate_counts(self):
        first, second, third = bar(0), bar(MINUTE), bar(2 * MINUTE)
        self.store.save_candles([third, first, second, second, bar(3 * MINUTE, closed=False),
                                 bar(4 * MINUTE, closed="true")])
        self.store.save_candles([first, second, third])
        self.store.save_candles([replace(third, close=102, quote_volume=1400)])
        inventory = self.store.candle_inventory()
        self.assertEqual(inventory, [{"symbol": "BTCUSDT", "timeframe": "1m", "bars": 3,
                                     "first_open_time": 0, "last_open_time": 2 * MINUTE,
                                     "last_close_time": 3 * MINUTE - 1, "last_price": 102}])
        self.assertEqual(self.store.counts()["candles"], 3)
        window = self.store.candle_window_stats("BTCUSDT", "1m")
        self.assertEqual(window["bars"], 3)
        self.assertEqual(window["latest_candle"]["quote_volume"], 1400)
        self.assertEqual(window["latest_candle"]["close"], 102)
        self.assertIs(window["latest_candle"]["closed"], True)

    def test_contract_timeframe_grouping_and_filtering_are_exact(self):
        self.store.save_candles([bar(0), bar(MINUTE), bar(0, timeframe="5m"),
                                 bar(0, symbol="BTCUSDC"), bar(0, symbol="ETHUSDT", timeframe="1d")])
        groups = self.store.candle_inventory()
        self.assertEqual([(item["symbol"], item["timeframe"], item["bars"]) for item in groups],
                         [("BTCUSDC", "1m", 1), ("BTCUSDT", "1m", 2), ("BTCUSDT", "5m", 1), ("ETHUSDT", "1d", 1)])
        self.assertEqual(self.store.candle_inventory(["BTCUSDT", "BTCUSDT", "UNKNOWN"]), [row for row in groups if row["symbol"] == "BTCUSDT"])
        self.assertEqual(self.store.candle_inventory("BTCUSDC"), [groups[0]])
        self.assertEqual(self.store.candle_inventory([]), [])
        self.assertEqual(self.store.candle_inventory(["' OR 1=1 --"]), [])
        self.assertEqual(self.store.candle_inventory(iter(["ETHUSDT"])), [groups[-1]])

    def test_large_symbol_filter_is_chunked_without_duplicates(self):
        self.store.save_candles([bar(0), bar(0, symbol="ETHUSDT")])
        selected = [f"ABSENT{index}" for index in range(1000)] + ["BTCUSDT", "ETHUSDT", "BTCUSDT"]
        self.assertEqual(self.store.candle_inventory(selected), self.store.candle_inventory())

    def test_window_includes_open_time_boundaries_and_latest_row_is_inside_window(self):
        original = [bar(i * MINUTE, close=101 + i / 10) for i in range(6)]
        self.store.save_candles(original)
        window = self.store.candle_window_stats("BTCUSDT", "1m", start=MINUTE, end=3 * MINUTE)
        self.assertEqual(window["bars"], 3)
        self.assertEqual(window["first_open_time"], MINUTE)
        self.assertEqual(window["last_open_time"], 3 * MINUTE)
        self.assertEqual(window["last_close_time"], 4 * MINUTE - 1)
        self.assertEqual(window["last_price"], 101.3)
        self.assertEqual(window["latest_candle"], asdict(original[3]))
        self.assertEqual(self.store.candle_window_stats("BTCUSDT", "1m", start=MINUTE)["bars"], 5)
        self.assertEqual(self.store.candle_window_stats("BTCUSDT", "1m", end=MINUTE)["bars"], 2)
        self.assertEqual(self.store.candle_window_stats("BTCUSDT", "1m", MINUTE, MINUTE)["bars"], 1)
        empty = self.store.candle_window_stats("BTCUSDT", "1m", 3 * MINUTE, MINUTE)
        self.assertEqual(empty["bars"], 0)
        self.assertIsNone(empty["latest_candle"])

    def test_gaps_count_only_real_bars_and_return_detached_latest_candle(self):
        self.store.save_candles([bar(0), bar(10 * MINUTE), bar(100 * MINUTE)])
        window = self.store.candle_window_stats("BTCUSDT", "1m", 0, 100 * MINUTE)
        self.assertEqual(window["bars"], 3)
        self.assertEqual(self.store.candle_inventory()[0]["bars"], 3)
        window["latest_candle"]["close"] = -1
        self.assertEqual(self.store.candle_window_stats("BTCUSDT", "1m")["last_price"], 101)

    def test_existing_database_is_available_after_restart_without_summary_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy-market.sqlite3"
            # Create the existing candles-only schema independently of Store.
            with closing(sqlite3.connect(path)) as database:
                database.execute("""CREATE TABLE candles (
                    symbol TEXT NOT NULL,timeframe TEXT NOT NULL,open_time INTEGER NOT NULL,
                    close_time INTEGER NOT NULL,open REAL NOT NULL,high REAL NOT NULL,
                    low REAL NOT NULL,close REAL NOT NULL,quote_volume REAL NOT NULL,
                    taker_buy_quote REAL NOT NULL,trades INTEGER NOT NULL,volume REAL NOT NULL,
                    closed INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(symbol,timeframe,open_time)
                    ) WITHOUT ROWID""")
                database.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                    ("BTCUSDT", "1m", 0, 59999, 100, 103, 99, 101, 1000, 600, 10, 10, 1),
                    ("BTCUSDT", "1m", 60000, 119999, 100, 103, 99, 102, 1000, 600, 10, 10, 1),
                    ("BTCUSDT", "1m", 120000, 179999, 100, 103, 99, 103, 1000, 600, 10, 10, 0),
                ])
                database.commit()
            first = Store(path)
            try:
                initial = first.candle_inventory()
                self.assertEqual(initial[0]["bars"], 2)
                self.assertEqual(first.candle_window_stats("BTCUSDT", "1m")["last_price"], 102)
            finally:
                first.close()
            restarted = Store(path)
            try:
                self.assertEqual(restarted.candle_inventory(), initial)
                restarted.save_candles([bar(120000)])
                self.assertEqual(restarted.candle_inventory()[0]["bars"], 3)
            finally:
                restarted.close()

    def test_file_database_counts_do_not_wait_for_writer_connection_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            disk_store = Store(Path(folder) / "market.sqlite3")
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                disk_store.save_candles([bar(0), bar(MINUTE)])
                disk_store._lock.acquire()
                future = executor.submit(disk_store.counts)
                try:
                    self.assertEqual(future.result(timeout=1), {
                        "candles": 2, "alerts": 0, "oi_snapshots": 0,
                    })
                finally:
                    disk_store._lock.release()
            finally:
                executor.shutdown(wait=True)
                disk_store.close()

    def test_file_inventory_scan_does_not_hold_or_wait_for_writer_connection_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            disk_store = Store(Path(folder) / "market.sqlite3")
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                disk_store.save_candles([bar(0), bar(MINUTE)])
                disk_store._lock.acquire()
                future = executor.submit(disk_store.candle_inventory)
                try:
                    self.assertEqual(future.result(timeout=1), [{
                        "symbol": "BTCUSDT", "timeframe": "1m", "bars": 2,
                        "first_open_time": 0, "last_open_time": MINUTE,
                        "last_close_time": 2 * MINUTE - 1, "last_price": 101,
                    }])
                finally:
                    disk_store._lock.release()
            finally:
                executor.shutdown(wait=True)
                disk_store.close()

    def test_file_status_snapshot_does_not_wait_for_writer_connection_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            disk_store = Store(Path(folder) / "market.sqlite3")
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                disk_store.reconcile_universe([{
                    "symbol": "BTCUSDT", "quoteAsset": "USDT", "status": "TRADING",
                    "onboardDate": 1,
                }], 1000)
                expected = (disk_store.universe_snapshot_status(), disk_store.list_listing_events(5))
                disk_store._lock.acquire()
                future = executor.submit(disk_store.status_snapshot, 5)
                try:
                    self.assertEqual(future.result(timeout=1), expected)
                finally:
                    disk_store._lock.release()
            finally:
                executor.shutdown(wait=True)
                disk_store.close()

    def test_prune_is_reflected_immediately_without_stale_progress_counts(self):
        now = 400 * DAY
        minute_boundary = now - 14 * DAY
        intraday_boundary = now - 366 * DAY
        self.store.save_candles([
            bar(minute_boundary - MINUTE), bar(minute_boundary), bar(now - MINUTE),
            bar(intraday_boundary - TIMEFRAME_MS["2h"], timeframe="2h"),
            bar(intraday_boundary, timeframe="2h"), bar(now - TIMEFRAME_MS["2h"], timeframe="2h"),
            bar(0, timeframe="1d"), bar(DAY, timeframe="1d"),
            bar(0, symbol="OLDUSDT"),
        ])
        self.assertEqual(sum(row["bars"] for row in self.store.candle_inventory()), 9)
        self.store.prune(now)
        inventory = {(row["symbol"], row["timeframe"]): row for row in self.store.candle_inventory()}
        self.assertNotIn(("OLDUSDT", "1m"), inventory)
        self.assertEqual(inventory[("BTCUSDT", "1m")]["bars"], 2)
        self.assertEqual(inventory[("BTCUSDT", "1m")]["first_open_time"], minute_boundary)
        self.assertEqual(inventory[("BTCUSDT", "2h")]["first_open_time"], intraday_boundary)
        self.assertEqual(inventory[("BTCUSDT", "1d")]["bars"], 2)
        self.assertEqual(self.store.candle_window_stats("BTCUSDT", "1m", 0, minute_boundary - MINUTE)["bars"], 0)
        self.assertEqual(self.store.counts(), {"candles": 6, "alerts": 0, "oi_snapshots": 0})

    def test_threaded_reads_and_writes_return_coherent_inventory(self):
        def writer():
            for index in range(80):
                self.store.save_candles([bar(index * MINUTE), bar(index * MINUTE)])

        def reader():
            for _ in range(80):
                window = self.store.candle_window_stats("BTCUSDT", "1m")
                if window["bars"]:
                    self.assertEqual(window["bars"], window["last_open_time"] // MINUTE + 1)
                    self.assertEqual(window["latest_candle"]["open_time"], window["last_open_time"])
                inventory = self.store.candle_inventory()
                if inventory:
                    self.assertEqual(inventory[0]["bars"], inventory[0]["last_open_time"] // MINUTE + 1)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(writer), executor.submit(reader), executor.submit(reader)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.store.candle_inventory()[0]["bars"], 80)
        self.assertEqual(self.store.counts()["candles"], 80)

    def test_compact_signal_drops_full_context_but_keeps_decision_evidence_and_detail(self):
        context = {"supertrend": {"ready": True, "direction": "up", "value": 98},
                   "smc": {"events": [{"event": "BOS", "time": index * MINUTE, "direction": "up", "price": 100 + index / 10} for index in range(300)],
                           "equal_levels": [{"price": 95 + index / 10, "kind": "support", "confirmed_at": index * MINUTE} for index in range(100)]},
                   "chart": {"supertrend": [{"time": index * MINUTE, "value": 100 + index / 10} for index in range(300)]}}
        original = {"symbol": "BTCUSDT", "timeframe": "1m", "close_time": 299 * MINUTE - 1,
                    "source": "live", "qualified": True, "quality_schema_version": 2,
                    "quality_reasons": [], "confirmations": [{"id": "supertrend", "passed": True, "reason": "方向一致"}],
                    "trade_plan": {"reference_entry": 101, "stop_loss": 99, "target": 106, "risk_reward": 2.3, "not_order": True},
                    "policy_hash": "a123", "score_breakdown": {"volume": 31, "structure": 15}, "activity_score": 90,
                    "candles": [asdict(bar(index * MINUTE)) for index in range(50)],
                    "indicators": context, "indicator_context": deepcopy(context),
                    "policy_snapshot": {"indicators": {"enabled_indicators": ["supertrend", "smc"]}, "quality": {"min_risk_reward": 2}}}
        pristine = deepcopy(original)
        self.assertTrue(self.store.save_alert(original))
        full = self.store.list_alerts()[0]
        compact = self.store.list_alerts(include_candles=False, qualified=True, schema_version=2)[0]
        removed = {"candles", "indicators", "indicator_context", "policy_snapshot"}
        self.assertEqual(compact, {key: value for key, value in full.items() if key not in removed})
        self.assertTrue(removed.isdisjoint(compact))
        self.assertTrue(compact["qualified"])
        self.assertEqual(compact["confirmations"], pristine["confirmations"])
        self.assertEqual(compact["trade_plan"], pristine["trade_plan"])
        self.assertEqual(compact["policy_hash"], "a123")
        self.assertEqual(original, pristine)
        self.assertEqual(self.store.get_alert(compact["id"]), full)
        self.assertEqual(self.store.list_alerts()[0], full)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)) * .1)
        compact["trade_plan"]["target"] = -1
        self.assertEqual(self.store.get_alert(full["id"])["trade_plan"]["target"], 106)

    def test_compact_plain_signal_removes_only_fields_actually_present(self):
        self.store.save_alert({"symbol": "ETHUSDT", "timeframe": "5m", "close_time": 299999,
                               "source": "live", "activity_score": 71, "candles": [{"close": 101}],
                               "quality_reasons": ["历史确认不足"]})
        full = self.store.list_alerts()[0]
        compact = self.store.list_alerts(include_candles=False)[0]
        self.assertEqual(compact, {key: value for key, value in full.items() if key != "candles"})
        self.assertEqual(self.store.get_alert(compact["id"]), full)


if __name__ == "__main__":
    unittest.main()
