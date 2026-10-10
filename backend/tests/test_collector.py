"""Offline integration checks for public collection safety and persistence."""
import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from monitor.collector import (DAY, MINUTE, BinanceRateLimitError, GAP_WORKERS, INDICATOR_RULES_NOT_CONFIGURED, MONITOR_TIMEFRAMES, REST_BUDGET_FRACTION, MonitorService, WeightBudget, eligible_contract, eligible_crypto_perpetual, eligible_um_contract,
                               completion_percentage, rest_candle, retry_after_seconds)
from monitor.engine import Candle, LOOKBACK, TIMEFRAME_MS
from monitor.store import DAY_MS, Store, is_trade_qualified, shanghai_day_start


def bar(start, symbol="1000PEPEUSDT", timeframe="1m", **changes):
    values = dict(symbol=symbol, timeframe=timeframe, open_time=start,
                  close_time=start + TIMEFRAME_MS[timeframe] - 1,
                  open=100, high=102, low=99, close=101, quote_volume=1000,
                  taker_buy_quote=600, trades=10, volume=10, closed=True)
    values.update(changes)
    return Candle(**values)


def meta(symbol="1000PEPEUSDT", **changes):
    result = dict(symbol=symbol, status="TRADING", contractType="PERPETUAL",
                  quoteAsset="USDT", marginAsset=changes.get("quoteAsset", "USDT"), futuresType="U_MARGINED",
                  underlyingType="COIN", underlyingSubType=[], onboardDate=0)
    result.update(changes)
    return result


class StoreTests(unittest.TestCase):
    def test_store_only_accepts_closed_and_returns_chronological(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.save_candles([bar(2 * MINUTE), bar(0), bar(MINUTE), bar(3 * MINUTE, closed=False),
                            bar(4 * MINUTE, closed="true")])
        rows = store.get_candles("1000PEPEUSDT", "1m", 10)
        self.assertEqual([c.open_time for c in rows], [0, MINUTE, 2 * MINUTE])
        store.save_candles([bar(MINUTE, close=100.5)])
        self.assertEqual(store.counts()["candles"], 3)
        self.assertEqual(store.get_candles("1000PEPEUSDT", "1m", 10)[1].close, 100.5)

    def test_signal_dedup_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market.sqlite"
            store = Store(path)
            signal = dict(symbol="1000PEPEUSDT", timeframe="1m", close_time=MINUTE - 1,
                          pattern="阴阳", activity_score=65, source="live")
            self.assertTrue(store.save_alert(signal))
            self.assertFalse(store.save_alert({**signal, "activity_score": 90}))
            store.close()
            store = Store(path)
            self.assertFalse(store.save_alert(signal))
            self.assertEqual(store.list_alerts()[0]["activity_score"], 65)
            store.close()

    def test_signal_daily_counts_use_utc8_day_and_never_increment_on_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market.sqlite"
            store = Store(path)
            boundary = shanghai_day_start(0) + DAY_MS
            previous = dict(symbol="BTCUSDT", timeframe="5m", close_time=boundary - 1,
                            pattern="阳阳", source="live")
            first = dict(symbol="BTCUSDT", timeframe="5m", close_time=boundary + 299_999,
                         pattern="阳阳", source="live")
            second = dict(symbol="BTCUSDT", timeframe="15m", close_time=boundary + 899_999,
                          pattern="阴阴", source="live")
            third = dict(symbol="BTCUSDT", timeframe="5m", close_time=boundary + 1_199_999,
                         pattern="阳阴", source="live")
            other = dict(symbol="ETHUSDT", timeframe="5m", close_time=boundary + 1_499_999,
                         pattern="阴阳", source="live")
            for signal in (previous, first, second, third, other):
                self.assertTrue(store.save_alert(signal))
                signal.update(store.daily_counts_for_alert(signal))
            self.assertEqual(first["daily_signal_count"], 1)
            self.assertEqual(second["daily_signal_count"], 2)
            self.assertEqual(third["daily_signal_count"], 3)
            self.assertEqual(third["daily_timeframe_signal_count"], 2)
            self.assertEqual(third["daily_direction_signal_count"], 2)
            self.assertEqual(other["daily_signal_count"], 1)
            self.assertFalse(store.save_alert({**third, "activity_score": 99}))

            rows = store.list_alerts()
            current_btc = [row for row in rows if row["symbol"] == "BTCUSDT"
                           and row["close_time"] >= boundary]
            self.assertEqual(len(current_btc), 3)
            self.assertTrue(all(row["daily_signal_count"] == 3 for row in current_btc))
            old = next(row for row in rows if row["close_time"] < boundary)
            self.assertEqual(old["daily_signal_count"], 1)
            store.close()

            # Counter rows and the no-double-count guarantee survive restarts.
            store = Store(path)
            try:
                self.assertFalse(store.save_alert(third))
                latest = store.get_alert(
                    f"BTCUSDT:5m:{third['close_time']}:TWO_BAR_VOLATILITY")
                self.assertEqual(latest["daily_signal_count"], 3)
                self.assertEqual(latest["daily_timeframe_signal_count"], 2)
            finally:
                store.close()

    def test_existing_alert_history_backfills_daily_counts_on_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite"
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE alerts (
                event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL, close_time INTEGER NOT NULL,
                created_at INTEGER NOT NULL, payload TEXT NOT NULL)""")
            for index, (timeframe, pattern) in enumerate((("5m", "阳阳"), ("15m", "阴阴"))):
                close_time = 60_000 + index * 300_000
                payload = {"id": str(index), "symbol": "BTCUSDT",
                           "timeframe": timeframe, "close_time": close_time,
                           "pattern": pattern, "source": "live"}
                db.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?)", (
                    str(index), "BTCUSDT", timeframe, close_time, close_time,
                    json.dumps(payload, ensure_ascii=False)))
            db.commit()
            db.close()

            store = Store(path)
            try:
                rows = store.list_alerts()
                self.assertEqual([row["daily_signal_count"] for row in rows], [2, 2])
                self.assertEqual([row["daily_timeframe_signal_count"] for row in rows], [1, 1])
                self.assertEqual([row["daily_direction_signal_count"] for row in rows], [1, 1])
            finally:
                store.close()

            # Simulate an alert written by the previous collector build after
            # its one-time counter backfill.  Reopening must reconcile the
            # existing group instead of leaving it permanently under-counted.
            db = sqlite3.connect(path)
            close_time = 660_000
            payload = {"id": "late", "symbol": "BTCUSDT", "timeframe": "5m",
                       "close_time": close_time, "pattern": "阳阳", "source": "live"}
            db.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?)", (
                "late", "BTCUSDT", "5m", close_time, close_time,
                json.dumps(payload, ensure_ascii=False)))
            db.commit()
            db.close()
            store = Store(path)
            try:
                rows = store.list_alerts()
                self.assertTrue(all(row["daily_signal_count"] == 3 for row in rows))
                latest = next(row for row in rows if row["id"] == "late")
                self.assertEqual(latest["daily_timeframe_signal_count"], 2)
                self.assertEqual(latest["daily_direction_signal_count"], 2)
            finally:
                store.close()

    def test_oi_roundtrip_has_quantity_value_and_source_time(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.save_oi(dict(symbol="BTCUSDT", timestamp=120000, open_interest_quantity=12,
                           mark_price=50000, source_time=119998))
        sample = store.load_oi("BTCUSDT")[0]
        self.assertEqual(sample["estimated_value"], 600000)
        self.assertEqual(sample["source_time"], 119998)

    def test_listing_baseline_survives_restart_and_never_relabels_old_contracts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market.sqlite"
            store = Store(path)
            self.assertEqual(store.reconcile_universe([meta(onboardDate=100)], 1000), [])
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.reconcile_universe([meta(onboardDate=100), meta("OLDUSDT", onboardDate=50)], 2000), [])
                events = store.reconcile_universe([meta(onboardDate=100), meta("NEWUSDC", quoteAsset="USDC", onboardDate=2500)], 3000)
                self.assertEqual([r["symbol"] for r in events], ["NEWUSDC"])
                self.assertEqual(events[0]["event_type"], "listed")
                self.assertEqual(store.reconcile_universe([meta("NEWUSDC", quoteAsset="USDC", onboardDate=2500)], 3500), [])
                self.assertEqual(len(store.list_listing_events()), 1)
                self.assertEqual(store.universe_snapshot_status()["initialized_at"], 1000)
                self.assertEqual([r["symbol"] for r in store.list_universe()], ["NEWUSDC"])
                self.assertEqual(len(store.list_universe(active_only=False)), 3)
            finally:
                store.close()

    def test_pending_then_trading_are_separate_events_and_are_deduplicated(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        pending = meta("NEWUSDT", status="PENDING_TRADING", onboardDate=2000)
        events = store.reconcile_universe([pending], 1000)
        self.assertEqual(events[0]["event_type"], "upcoming")
        self.assertFalse(events[0]["tradable"])
        self.assertEqual(store.reconcile_universe([pending], 1500), [])
        listed = meta("NEWUSDT", onboardDate=2000)
        events = store.reconcile_universe([listed], 2500)
        self.assertEqual(events[0]["event_type"], "listed")
        self.assertTrue(events[0]["tradable"])
        self.assertEqual(store.reconcile_universe([listed], 3000), [])
        self.assertEqual(store.universe_snapshot_status()["listed_events"], 1)
        self.assertEqual(store.universe_snapshot_status()["upcoming_events"], 1)

    def test_empty_snapshot_does_not_create_false_initialization(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        self.assertEqual(store.reconcile_universe([], 1000), [])
        self.assertFalse(store.universe_snapshot_status()["initialized"])
        self.assertEqual(store.reconcile_universe([meta(onboardDate=1500)], 2000), [])

    def test_qualified_filter_does_not_promote_legacy_and_preserves_raw_details(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        base = dict(symbol="BTCUSDT", timeframe="1m", source="live")
        store.save_alert({**base, "close_time": 59999})
        store.save_alert({**base, "close_time": 119999, "qualified": True})
        qualified = {**base, "close_time": 179999, "qualified": True, "quality_schema_version": 2,
                     "candles": [1], "indicators": {"chart": [2], "current": "ok"},
                     "indicator_context": {"chart": [3], "confirmations": ["st"]}}
        store.save_alert(qualified)
        store.save_alert({**base, "close_time": 239999, "qualified": False, "quality_schema_version": 2})
        self.assertEqual(len(store.list_alerts(qualified=True, schema_version=2)), 1)
        self.assertEqual(len(store.list_alerts(qualified=False)), 3)
        compact = store.list_alerts(qualified=True, include_candles=False)[0]
        self.assertNotIn("candles", compact)
        self.assertNotIn("indicators", compact)
        self.assertNotIn("indicator_context", compact)
        self.assertEqual(store.get_alert(compact["id"])["indicators"]["chart"], [2])
        self.assertTrue(is_trade_qualified(qualified, 2))
        self.assertFalse(is_trade_qualified(qualified, 3))
        self.assertFalse(is_trade_qualified({**qualified, "quality_schema_version": True}))
        for changes in ({"source": "demo"}, {"synthetic": True}):
            with self.assertRaises(ValueError):
                store.save_alert({**qualified, **changes})


class MetadataTests(unittest.TestCase):
    def test_number_prefixed_crypto_is_not_a_tradfi_heuristic(self):
        self.assertTrue(eligible_contract(meta()))
        self.assertTrue(eligible_contract(meta("BTCUSDT_260925", contractType="CURRENT_QUARTER")))

    def test_all_asset_classes_and_contract_tenors_are_included_for_usdt_um(self):
        for change in ({"contractType": "TRADIFI_PERPETUAL"}, {"underlyingType": "INDEX"},
                       {"underlyingType": "STOCK"}, {"underlyingType": None},
                       {"underlyingSubType": ["TRADFI"]}, {"underlyingType": "HK_EQUITY"},
                       {"underlyingType": "KR_EQUITY"}, {"underlyingType": "CN_EQUITY"},
                       {"underlyingType": "COMMODITY"}, {"underlyingType": "PREMARKET"},
                       {"contractType": "CURRENT_QUARTER"}, {"contractType": "NEXT_QUARTER"},
                       {"underlyingType": "FUTURE_CLASS", "contractType": "FUTURE_TENOR"}):
            with self.subTest(change=change):
                self.assertTrue(eligible_contract(meta(**change)))

    def test_non_usdt_non_um_and_non_trading_are_excluded_without_symbol_guessing(self):
        for change in ({"quoteAsset": "BTC"}, {"quoteAsset": "USDC"}, {"quoteAsset": "USD1"},
                       {"quoteAsset": "U"}, {"status": "SETTLING"}, {"status": "PENDING_TRADING"},
                       {"futuresType": "COIN_MARGINED"}, {"futuresType": None}, {"st": 2}, {"st": True}):
            with self.subTest(change=change):
                self.assertFalse(eligible_contract(meta(**change)))
        self.assertTrue(eligible_contract(meta(futuresType=None), "U_MARGINED"))
        self.assertTrue(eligible_um_contract(meta("ETHBTC", quoteAsset="BTC", marginAsset="BTC")))
        self.assertFalse(eligible_contract(meta("ETHBTC", quoteAsset="BTC", marginAsset="BTC")))

    def test_upcoming_crypto_is_metadata_eligible_but_never_tradable(self):
        pending = meta(status="PENDING_TRADING")
        self.assertTrue(eligible_crypto_perpetual(pending))
        self.assertFalse(eligible_contract(pending))

    def test_rest_partial_candles_are_not_closed(self):
        row = [0, "100", "102", "99", "101", "10", 59999, "1000", 10, "6", "600", "0"]
        self.assertFalse(rest_candle("BTCUSDT", "1m", row, 59000).closed)
        self.assertTrue(rest_candle("BTCUSDT", "1m", row, 60000).closed)

    def test_retry_after_seconds_respects_server_delay(self):
        self.assertEqual(retry_after_seconds("123"), 123)
        self.assertEqual(retry_after_seconds(None), 60)
        for bad in ("nan","inf","-1"):
            self.assertEqual(retry_after_seconds(bad,120),120)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.service = MonitorService(self.store)
        self.service._metadata = {"1000PEPEUSDT": meta()}
        self.now_ms = 10 * DAY + 1000
        self.service.now = lambda: self.now_ms
        self.service._watermark = self.now_ms

    async def asyncTearDown(self):
        await self.service.stop()
        self.store.close()

    def test_active_contracts_are_balanced_without_losing_unicode_or_scope(self):
        symbols = [f"COIN{i:03d}USDT" for i in range(560)] + ["币安人生USDC"]
        self.service._metadata = {symbol: meta(symbol) for symbol in symbols}
        self.service._ticker = {symbol: {"quote_volume": 10000 - i} for i, symbol in enumerate(symbols)}
        shards = self.service._balanced_kline_shards()
        self.assertEqual(len(shards), 15)
        self.assertTrue(all(len(shard) <= 40 for shard in shards))
        self.assertEqual(sorted(s for shard in shards for s in shard), sorted(symbols))
        self.assertEqual([shard[0] for shard in shards], symbols[:15])
        self.assertLessEqual(max(map(len, shards)) - min(map(len, shards)), 1)

    async def test_ticker_rank_changes_do_not_reconnect_unchanged_universe(self):
        self.service._running = True
        symbols = [meta(f"TEST{i:03d}USDT") for i in range(80)] + [meta("中文USDT")]
        never = asyncio.Event()

        async def fake_request(path, params=None, weight=1):
            return {"symbols": symbols} if path.endswith("exchangeInfo") else []

        async def fake_stream(symbols, index):
            await never.wait()

        self.service._request = fake_request
        self.service._kline_stream = fake_stream
        self.service._market_stream = fake_stream
        self.service._book_stream = lambda: fake_stream([], -1)
        self.service._market_control_stream = lambda: fake_stream([], -2)
        await self.service._refresh_universe()
        original_tasks = list(self.service._shards)
        original_map = dict(self.service._symbol_shard)
        self.service._ticker = {row["symbol"]: {"quote_volume": i} for i, row in enumerate(symbols)}
        await self.service._refresh_universe()
        self.assertEqual(self.service._shards, original_tasks)
        self.assertEqual(self.service._symbol_shard, original_map)
        self.assertEqual(set(original_map), {row["symbol"] for row in symbols})
        self.assertTrue(all(not task.cancelled() for task in original_tasks))

    async def test_scope_change_recycles_used_streams_without_idle_mark_price_subscriptions(self):
        self.service._running = True
        symbols = [meta(f"TEST{i:03d}USDT") for i in range(560)] + [meta("中文USDT")]
        active = {}
        cancelled = []
        never = asyncio.Event()

        async def fake_request(path, params=None, weight=1):
            return {"symbols": symbols} if path.endswith("exchangeInfo") else []

        async def fake_stream(family, symbols, index):
            key = f"{family}-{index}"
            active[key] = list(symbols)
            try:
                await never.wait()
            finally:
                cancelled.append(key)
                active.pop(key, None)

        async def fake_global_stream(family):
            active[family] = []
            try:
                await never.wait()
            finally:
                cancelled.append(family)
                active.pop(family, None)

        self.service._request = fake_request
        self.service._kline_stream = lambda symbols, index: fake_stream("kline", symbols, index)
        self.service._market_stream = lambda symbols, index: fake_stream("market", symbols, index)
        self.service._book_stream = lambda: fake_global_stream("book-ticker")
        self.service._market_control_stream = lambda: fake_global_stream("market-control")
        await self.service._refresh_universe()
        await asyncio.sleep(0)
        originals = list(self.service._shards)
        self.assertEqual(len(originals), 17)
        self.assertEqual(len(active), 17)
        for index in range(15):
            self.assertLessEqual(len(active[f"kline-{index}"]), 40)
            self.assertNotIn(f"market-{index}", active)
        all_market = [symbol for name, group in active.items() if name.startswith("kline-") for symbol in group]
        self.assertEqual(sorted(all_market), sorted(row["symbol"] for row in symbols))
        symbols = [*symbols[1:], meta("NEWUSDT")]
        self.service._ticker["TEST000USDT"] = {"quote_volume": 1, "price": 1, "time": self.now_ms}
        self.service._mark["TEST000USDT"] = (1, self.now_ms)
        await self.service._refresh_universe()
        await asyncio.sleep(0)
        self.assertTrue(all(task.cancelled() for task in originals))
        self.assertEqual(len(cancelled), 17)
        self.assertEqual(len(active), 17)
        self.assertNotIn("TEST000USDT", self.service._symbol_shard)
        self.assertNotIn("TEST000USDT", self.service._ticker)
        self.assertNotIn("TEST000USDT", self.service._mark)
        self.assertIn("NEWUSDT", self.service._symbol_shard)
        await self.service.stop()
        self.assertEqual(active, {})
        self.assertEqual(len(cancelled), 34)
        self.assertFalse(self.service._shards)
        self.assertFalse(self.service._tasks)

    def test_mini_ticker_preserves_quote_volume_price_change_and_event_time(self):
        symbol = "1000PEPEUSDT"
        self.service._consume_market_event({"e": "24hrMiniTicker", "s": symbol, "st": 1,
            "E": self.now_ms - 1234, "q": "123456.75", "c": "110", "o": "100", "P": "999"})
        ticker = self.service._ticker[symbol]
        self.assertEqual(ticker["quote_volume"], 123456.75)
        self.assertEqual(ticker["price"], 110)
        self.assertAlmostEqual(ticker["change_pct"], 10)
        self.assertEqual(ticker["time"], self.now_ms - 1234)
        self.service._consume_market_event({"e": "markPriceUpdate", "s": symbol, "st": 1,
            "E": self.now_ms - 500, "p": "109.5"})
        self.assertEqual(self.service._mark[symbol], (109.5, self.now_ms - 500))
        self.assertEqual(self.service._ticker[symbol], ticker)
        self.service._consume_tickers([{"symbol": symbol, "quoteVolume": "2345.5", "lastPrice": "90",
                                        "priceChangePercent": "-12.5"}], rest=True)
        self.assertEqual(self.service._ticker[symbol]["change_pct"], -12.5)
        self.assertEqual(self.service._ticker[symbol]["time"], self.now_ms)

    def test_market_events_reject_foreign_stale_reordered_and_invalid_content(self):
        symbol = "1000PEPEUSDT"
        valid = {"e": "24hrMiniTicker", "s": symbol, "st": 1, "E": self.now_ms,
                 "q": "100", "c": "101", "o": "100"}
        self.service._consume_market_event(valid)
        original = dict(self.service._ticker[symbol])
        for change in ({"s": "OTHERUSDT"}, {"st": 2}, {"E": self.now_ms - 90_000},
                       {"E": self.now_ms + 60_000}, {"E": None}, {"E": self.now_ms - 100},
                       {"e": "24hrTicker"}, {"o": "0"}, {"q": "NaN"}, {"c": "Infinity"}):
            self.service._consume_market_event({**valid, **change})
        self.assertEqual(self.service._ticker, {symbol: original})
        self.service._consume_market_event({"e": "markPriceUpdate", "s": symbol, "E": self.now_ms, "p": "100"})
        self.service._consume_market_event({"e": "markPriceUpdate", "s": symbol, "E": self.now_ms - 1000, "p": "500"})
        self.assertEqual(self.service._mark[symbol], (100, self.now_ms))

    def test_single_symbol_tickers_refresh_values_without_resorting_every_event(self):
        symbol = "1000PEPEUSDT"
        self.service._metadata["PEERUSDT"] = meta("PEERUSDT")
        self.service._ticker["PEERUSDT"] = {"time": self.now_ms, "quote_volume": 200}
        self.service._ranks = {"old": .5}
        self.service._rank_refreshed_at = time.monotonic()
        event = {"e": "24hrMiniTicker", "s": symbol, "E": self.now_ms, "q": "100", "c": "101", "o": "100"}
        for volume in range(100, 120):
            self.service._consume_market_event({**event, "q": str(volume)})
        self.assertEqual(self.service._ticker[symbol]["quote_volume"], 119)
        self.assertEqual(self.service._ranks, {"old": .5})
        self.service._rank_refreshed_at -= 2
        self.service._consume_market_event(event)
        self.assertEqual(self.service._ranks, {symbol: 0, "PEERUSDT": 1})

    def test_whole_market_ranking_requires_fresh_peers_without_shrinking_denominator(self):
        symbol, peer = "1000PEPEUSDT", "PEERUSDT"
        self.service._metadata[peer] = meta(peer)
        self.service._ticker = {symbol: {"time": self.now_ms, "quote_volume": 100},
                                peer: {"time": self.now_ms - 89_000, "quote_volume": 1000}}
        self.service._refresh_volume_ranks(force=True)
        self.assertEqual(self.service._base_enrichment(symbol)["liquidity_percentile"], 0)
        self.now_ms += 1001
        # Expiry is checked even before another ticker triggers the next sort.
        self.assertIsNone(self.service._base_enrichment(symbol)["liquidity_percentile"])
        self.assertEqual(self.service._base_enrichment(symbol)["liquidity_rank_status"]["reason"], "market_ticker_expired")
        self.service._refresh_volume_ranks(force=True)
        self.assertEqual(self.service._ranks, {})
        coverage = self.service._liquidity_rank_status(symbol)
        self.assertEqual(coverage["fresh_symbols"], 1)
        self.assertEqual(coverage["stale_symbols"], 1)
        self.assertEqual(coverage["reason"], "market_ticker_coverage_incomplete")
        self.service._ticker.pop(peer)
        self.service._refresh_volume_ranks(force=True)
        self.assertIsNone(self.service._base_enrichment(symbol)["liquidity_percentile"])

    def test_quote_groups_never_mix_units_or_award_singleton_high_rank(self):
        # The live universe is USDT-only; keep generic grouping safe for old
        # in-memory metadata/tests rather than silently treating BTC q as USD.
        self.service._metadata.update({"PEERUSDT": meta("PEERUSDT"),
                                       "ETHBTC": meta("ETHBTC", quoteAsset="BTC")})
        self.service._ticker = {"1000PEPEUSDT": {"time": self.now_ms, "quote_volume": 100},
                                "PEERUSDT": {"time": self.now_ms, "quote_volume": 1000},
                                "ETHBTC": {"time": self.now_ms, "quote_volume": 999999999}}
        self.service._refresh_volume_ranks(force=True)
        usdt = self.service._base_enrichment("PEERUSDT")
        self.assertEqual(usdt["liquidity_percentile"], 1)
        self.assertEqual(usdt["liquidity_rank_scope"], "same_quote_asset")
        self.assertEqual(usdt["liquidity_peer_count"], 2)
        self.assertEqual(usdt["liquidity_quote_asset"], "USDT")
        btc = self.service._base_enrichment("ETHBTC")
        self.assertIsNone(btc["liquidity_percentile"])
        self.assertEqual(btc["liquidity_rank_status"]["reason"], "insufficient_quote_peers")

    async def test_removed_or_non_usdt_history_jobs_exit_without_new_requests_or_writes(self):
        self.service._running = True
        self.store.save_candles([bar(0, symbol="OLDUSDC")])
        self.service._metadata["OLDUSDC"] = meta("OLDUSDC", quoteAsset="USDC")
        self.service._warm_pending.add(("OLDUSDC", "1m"))
        self.service._warm_queue.put_nowait(("OLDUSDC", "1m"))
        self.service._request = AsyncMock(side_effect=AssertionError("non-USDT must not request history"))
        worker = asyncio.create_task(self.service._warm_worker())
        try:
            await asyncio.wait_for(self.service._warm_queue.join(), 2)
            self.assertEqual(await self.service._fetch_range("OLDUSDC", "1m", MINUTE, 2 * MINUTE - 1), [])
            self.service._request.assert_not_awaited()
            self.assertEqual(self.store.candle_window_stats("OLDUSDC", "1m")["bars"], 1)
            self.assertEqual(self.service._history_jobs_done, 0)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.service._metadata.pop("OLDUSDC")
        self.assertNotIn("OLDUSDC", {row["symbol"] for row in self.service.download_status()["items"]})

    async def test_scope_removal_during_rest_response_prevents_new_page_commit(self):
        self.service._running = True
        symbol = "1000PEPEUSDT"
        self.store.save_candles([bar(0)])

        async def removed_while_awaiting(path, params=None, weight=1):
            self.service._metadata.pop(symbol)
            return [[MINUTE, "100", "102", "99", "101", "10", 2 * MINUTE - 1, "1000", 10, "6", "600", "0"]]

        self.service._request = removed_while_awaiting
        self.assertEqual(await self.service._fetch_range(symbol, "1m", MINUTE, 2 * MINUTE - 1), [])
        self.assertEqual(self.store.candle_window_stats(symbol, "1m")["bars"], 1)
        self.assertFalse(any(event["type"] == "page_saved" for event in self.service._download_events))

    async def test_market_websocket_subscribes_only_its_group_and_dispatches_by_event(self):
        self.service._running = True
        symbol = "中文USDC"
        self.service._metadata = {symbol: meta(symbol), "OTHERUSDT": meta("OTHERUSDT")}
        events = [
            {"stream": "misleading@markPrice", "data": {"e": "24hrMiniTicker", "s": symbol, "st": 1,
                "E": self.now_ms - 1200, "q": "200", "c": "95", "o": "100"}},
            {"stream": "misleading@miniTicker", "data": {"e": "markPriceUpdate", "s": symbol, "st": 1,
                "E": self.now_ms - 500, "p": "96"}},
            {"data": {"e": "markPriceUpdate", "s": "OTHERUSDT", "st": 1, "E": self.now_ms, "p": "500"}},
        ]
        drained, never = asyncio.Event(), asyncio.Event()
        addresses = []

        class FakeSocket:
            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *args):
                return False

            async def receive(inner):
                if events:
                    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(events.pop(0)))
                drained.set()
                await never.wait()

        def connect(url, **kwargs):
            addresses.append(url)
            return FakeSocket()

        self.service._session = SimpleNamespace(ws_connect=connect, close=AsyncMock())
        task = asyncio.create_task(self.service._market_stream([symbol], 2))
        try:
            await asyncio.wait_for(drained.wait(), 2)
            self.assertIn("中文usdc@markPrice", addresses[0])
            self.assertNotIn("@miniTicker", addresses[0])
            self.assertNotIn("!", addresses[0])
            self.assertNotIn("@1s", addresses[0])
            self.assertNotIn("otherusdt", addresses[0])
            self.assertNotIn(symbol, self.service._ticker)
            self.assertEqual(self.service._mark[symbol], (96, self.now_ms - 500))
            self.assertNotIn("OTHERUSDT", self.service._mark)
            connection = self.service.status()["connections"][0]
            self.assertEqual(connection["name"], "market-2")
            self.assertEqual(connection["subscriptions"], 1)
            self.assertEqual(connection["latency_samples"], 1)
            self.assertEqual(connection["event_delay_ms"], 500)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_market_control_websocket_updates_all_tickers_and_requests_scope_refresh(self):
        self.service._running = True
        symbol = "1000PEPEUSDT"
        events = [
            {"stream": "!miniTicker@arr", "data": [
                {"e": "24hrMiniTicker", "s": symbol, "st": 1, "E": self.now_ms - 500,
                 "q": "200", "c": "95", "o": "100"},
                {"e": "24hrMiniTicker", "s": "CMUSD", "st": 2, "E": self.now_ms - 400,
                 "q": "999", "c": "1", "o": "1"},
            ]},
            {"stream": "!contractInfo", "data": {"e": "contractInfo", "s": "NEWUSDT", "st": 1,
                "E": self.now_ms - 100, "cs": "TRADING"}},
        ]
        drained, never = asyncio.Event(), asyncio.Event()
        addresses = []

        class FakeSocket:
            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *args):
                return False

            async def receive(inner):
                if events:
                    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(events.pop(0)))
                drained.set()
                await never.wait()

        def connect(url, **kwargs):
            addresses.append(url)
            return FakeSocket()

        self.service._session = SimpleNamespace(ws_connect=connect, close=AsyncMock())
        task = asyncio.create_task(self.service._market_control_stream())
        try:
            await asyncio.wait_for(drained.wait(), 2)
            self.assertIn("!miniTicker@arr/!contractInfo", addresses[0])
            self.assertAlmostEqual(self.service._ticker[symbol]["change_pct"], -5)
            self.assertNotIn("CMUSD", self.service._ticker)
            self.assertTrue(self.service._refresh_requested.is_set())
            connection = next(row for row in self.service.status()["connections"]
                              if row["name"] == "market-control")
            self.assertEqual(connection["subscriptions"], 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_kline_shard_reconnect_fences_symbols_without_scheduling_whole_group(self):
        self.service._running = True
        symbols = ["1000PEPEUSDT", "OTHERUSDT"]
        self.service._metadata = {symbol: meta(symbol) for symbol in symbols}
        self.service._last_minute = {symbol: self.now_ms - MINUTE for symbol in symbols}
        second_connected, never = asyncio.Event(), asyncio.Event()
        connect_count = 0

        class FakeSocket:
            close_code = 1006

            def __init__(inner, first):
                inner.first = first

            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *args):
                return False

            async def receive(inner):
                if inner.first:
                    return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None, extra=None)
                second_connected.set()
                await never.wait()

            def exception(inner):
                return None

        def connect(url, **kwargs):
            nonlocal connect_count
            connect_count += 1
            return FakeSocket(connect_count == 1)

        self.service._session = SimpleNamespace(ws_connect=connect, close=AsyncMock())
        with patch("monitor.collector.asyncio.sleep", new=AsyncMock(return_value=None)):
            task = asyncio.create_task(self.service._kline_stream(symbols, 0))
            try:
                await asyncio.wait_for(second_connected.wait(), 2)
                self.assertEqual(connect_count, 2)
                self.assertEqual(self.service._reconnect_fence, set(symbols))
                self.assertEqual(self.service._gap_pending, set())
                self.assertTrue(self.service._gap_queue.empty())
                self.assertTrue(all(symbol in self.service._symbol_gate for symbol in symbols))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_fresh_market_connections_cannot_mask_missing_kline_connections(self):
        self.service._state = "warming"
        self.service._connections["market-0"] = {"state": "connected", "last_message": self.now_ms,
                                                   "last_event_time": self.now_ms}
        status = self.service.status()
        self.assertTrue(status["connections"][0]["content_fresh"])
        self.assertEqual(status["state"], "reconnecting")

    def test_metadata_freshness_follows_control_stream_without_rest_polling(self):
        self.service._last_refresh = 1
        self.service._connections["market-control"] = {
            "state": "connected", "last_message": self.now_ms,
            "last_event_time": self.now_ms,
        }
        self.assertFalse(self.service.status()["metadata_stale"])
        self.service._running = True
        self.service._state = "ready"
        self.service._symbol_shard["1000PEPEUSDT"] = "kline-0"
        self.service._connections["kline-0"] = {"state": "connected"}
        self.service._symbol_heartbeat["1000PEPEUSDT"] = self.now_ms
        self.assertTrue(self.service._symbol_live("1000PEPEUSDT"))
        self.service._connections["market-control"]["last_event_time"] = self.now_ms - 90_001
        self.assertTrue(self.service.status()["metadata_stale"])
        self.assertFalse(self.service._symbol_live("1000PEPEUSDT"))

    def test_connection_latency_is_bounded_and_transport_cannot_hide_stale_content(self):
        self.service._state = "warming"
        self.service._connections["kline-0"] = {"state": "connected"}
        for delay in range(300):
            self.service._record_connection_event("kline-0", self.now_ms - delay, self.now_ms)
        snapshot = self.service.status()["connections"][0]
        self.assertEqual(snapshot["latency_samples"], 256)
        self.assertEqual(snapshot["event_delay_ms"], 299)
        self.assertEqual(snapshot["latency_max_ms"], 299)
        self.assertEqual(snapshot["latency_p95_ms"], 287)
        self.assertTrue(snapshot["content_fresh"])
        self.service._record_connection_event("kline-0", self.now_ms - 90_001, self.now_ms)
        status = self.service.status()
        self.assertEqual(status["connections"][0]["last_message"], self.now_ms)
        # An out-of-order packet cannot erase a fresh high-watermark.
        self.assertTrue(status["connections"][0]["content_fresh"])
        self.service._record_connection_event("kline-0", self.now_ms + 160_000, self.now_ms)
        self.assertEqual(self.service._connections["kline-0"]["last_event_time"], self.now_ms)
        self.service.now = lambda: self.now_ms + 90_001
        self.service._record_connection_event("kline-0", self.now_ms, self.now_ms + 90_001)
        self.assertFalse(self.service.status()["connections"][0]["content_fresh"])
        self.assertEqual(self.service.status()["state"], "reconnecting")

    async def test_transport_packets_cannot_keep_stale_connection_open(self):
        ws = SimpleNamespace(receive=AsyncMock())
        self.service._connections["kline-0"] = {
            "last_event_time": self.now_ms-90_001, "last_message": self.now_ms}
        with self.assertRaises(TimeoutError):
            await self.service._receive_stream(ws,"kline-0")
        ws.receive.assert_not_awaited()

    async def test_gap_repair_fetches_only_missing_formal_buckets(self):
        interval = TIMEFRAME_MS["5m"]
        self.store.save_candles([bar(0,timeframe="5m"),bar(2*interval,timeframe="5m")])
        self.service._fetch_range = AsyncMock()
        await self.service._fetch_missing_formal_range("1000PEPEUSDT","5m",0,3*interval-1)
        self.service._fetch_range.assert_awaited_once_with("1000PEPEUSDT","5m",interval,2*interval-1)
        self.service._fetch_range.reset_mock()
        await self.service._fetch_missing_formal_range("1000PEPEUSDT","5m",0,interval-1)
        self.service._fetch_range.assert_not_awaited()

    def test_old_packet_does_not_reconnect_but_sustained_staleness_does(self):
        old = self.now_ms - 100_000
        with patch("monitor.collector.time.monotonic", return_value=10):
            self.assertFalse(self.service._accept_stream_event("kline-0", old, self.now_ms))
        with patch("monitor.collector.time.monotonic", return_value=39):
            self.assertFalse(self.service._accept_stream_event("kline-0", old, self.now_ms))
        self.assertTrue(self.service._accept_stream_event("kline-0", self.now_ms, self.now_ms))
        self.assertNotIn("invalid_since", self.service._connections["kline-0"])
        with patch("monitor.collector.time.monotonic", return_value=50):
            self.assertFalse(self.service._accept_stream_event("kline-0", old, self.now_ms))
        with patch("monitor.collector.time.monotonic", return_value=81):
            with self.assertRaises(TimeoutError):
                self.service._accept_stream_event("kline-0", old, self.now_ms)

    def test_access_cooldown_survives_restart_and_ws_shares_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cooldown.json"
            budget = WeightBudget(cooldown_path=path)
            budget.defer(120)
            restored = WeightBudget(cooldown_path=path)
            self.assertGreater(restored.cooldown_until - time.monotonic(), 118)
            self.service._budget = restored
            self.service._ws_failure(SimpleNamespace(status=418, headers={}))
            self.assertGreater(restored.cooldown_until - time.monotonic(), 259198)
            restored_again = WeightBudget(cooldown_path=path)
            self.assertGreater(restored_again.cooldown_until - time.monotonic(), 259198)

    async def test_parallel_gap_recovery_is_independent_and_deduplicates_symbols(self):
        self.service._running = True
        self.service._metadata["OTHERUSDT"] = meta("OTHERUSDT")
        blocked, other_finished, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(symbol, timeframe, start, end):
            calls.append((symbol, timeframe))
            if symbol == "1000PEPEUSDT" and timeframe == MONITOR_TIMEFRAMES[0]:
                blocked.set()
                await release.wait()
            if symbol == "OTHERUSDT" and timeframe == MONITOR_TIMEFRAMES[-1]:
                other_finished.set()
            return []

        self.service._fetch_range = fetch
        self.service._schedule_gap("1000PEPEUSDT", 9 * DAY)
        self.service._schedule_gap("1000PEPEUSDT", 9 * DAY)
        self.service._schedule_gap("OTHERUSDT", 9 * DAY)
        self.assertEqual(self.service._gap_queue.qsize(), 2)
        workers = [asyncio.create_task(self.service._recover_worker()) for _ in range(GAP_WORKERS)]
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            await asyncio.wait_for(other_finished.wait(), 2)
            self.assertEqual(calls.count(("1000PEPEUSDT", MONITOR_TIMEFRAMES[0])), 1)
            self.assertIn("1000PEPEUSDT", self.service._gap_pending)
            self.assertFalse(self.service._symbol_locks.get("1000PEPEUSDT", asyncio.Lock()).locked())
            release.set()
            await asyncio.wait_for(self.service._gap_queue.join(), 2)
            self.assertFalse(self.service._gap_pending)
            self.assertEqual(len(calls), 2 * len(MONITOR_TIMEFRAMES))
            self.assertIn("OTHERUSDT", self.service._symbol_locks)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def test_eight_gap_workers_bound_slow_rest_without_holding_persistence_locks(self):
        """A whole-market repair may use eight slots, but never an unbounded fanout."""
        self.assertEqual(GAP_WORKERS, 8)
        self.service._running = True
        symbols = [f"GAP{i:02d}USDT" for i in range(GAP_WORKERS + 3)]
        self.service._metadata = {symbol: meta(symbol) for symbol in symbols}
        entered = 0
        maximum = 0
        all_workers_busy = asyncio.Event()
        release = asyncio.Event()

        async def fetch(_symbol, timeframe, _start, _end):
            nonlocal entered, maximum
            if timeframe == MONITOR_TIMEFRAMES[0]:
                entered += 1
                maximum = max(maximum, entered)
                if entered == GAP_WORKERS:
                    all_workers_busy.set()
                await release.wait()
                entered -= 1
            return []

        self.service._fetch_range = fetch
        for symbol in symbols:
            self.service._schedule_gap(symbol, 9 * DAY)
        workers = [asyncio.create_task(self.service._recover_worker()) for _ in range(GAP_WORKERS)]
        try:
            await asyncio.wait_for(all_workers_busy.wait(), 2)
            await asyncio.sleep(0)
            self.assertEqual(maximum, GAP_WORKERS)
            self.assertEqual(self.service._gap_queue.qsize(), 3)
            self.assertEqual(self.service.status()["scheduler"]["gap_workers"], GAP_WORKERS)
            self.assertTrue(all(not lock.locked() for lock in self.service._symbol_locks.values()))
            # Closed-candle persistence must remain available while every REST
            # recovery slot is waiting on the network.
            await asyncio.wait_for(asyncio.to_thread(
                self.store.save_candles, [bar(9 * DAY, symbol=symbols[0])]), 1)
            release.set()
            await asyncio.wait_for(self.service._gap_queue.join(), 2)
            self.assertFalse(self.service._gap_pending)
        finally:
            release.set()
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def test_gap_recovery_stays_gated_while_frozen_warmup_range_is_active(self):
        symbol = "1000PEPEUSDT"
        start = 9 * DAY
        self.service._running = True
        self.service._warm_pending.add((symbol, MONITOR_TIMEFRAMES[0]))
        self.service._fetch_range = AsyncMock(side_effect=AssertionError("active warmup owns REST I/O"))
        self.service._schedule_gap(symbol, start)
        worker = asyncio.create_task(self.service._recover_worker())
        try:
            await asyncio.wait_for(self.service._gap_queue.join(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        self.assertIn(symbol, self.service._gap_pending)
        self.assertEqual(self.service._gap_start[symbol], start)
        self.assertFalse(self.service._can_alert(symbol, self.now_ms + MINUTE))
        self.assertEqual(self.service._fetch_range.await_count, 0)
        self.assertTrue(any(task.get_name() == f"gap-delay-{symbol}" for task in self.service._tasks))

    async def test_reconnect_fence_releases_on_durable_continuous_close_but_jump_repairs(self):
        self.service._running = True
        self.service._state = "ready"
        self.service._watermark = 0
        continuous, jumped = "CONTINUOUSUSDT", "JUMPEDUSDT"
        self.service._metadata = {continuous: meta(continuous), jumped: meta(jumped)}
        minute_floor = self.now_ms // MINUTE * MINUTE
        last = minute_floor - MINUTE
        self.store.save_candles([bar(last, symbol=continuous), bar(last, symbol=jumped)])
        self.service._last_minute = {continuous: last, jumped: last}
        self.service._fence_reconnected_symbols([continuous, jumped])
        gate_time = self.now_ms
        self.assertEqual(self.service._reconnect_fence, {continuous, jumped})
        self.assertEqual(self.service._gap_pending, set())

        # A duplicate from before reconnect does not prove a new continuous close.
        await self.service._on_closed(bar(last, symbol=jumped))
        self.assertIn(jumped, self.service._reconnect_fence)
        self.assertTrue(self.service._gap_queue.empty())

        self.now_ms = minute_floor + MINUTE + 1000
        continuous_bar = bar(last + MINUTE, symbol=continuous)
        self.assertGreater(continuous_bar.close_time, gate_time)
        self.assertFalse(self.service._can_alert(continuous, continuous_bar.close_time))
        await self.service._on_closed(continuous_bar)
        self.assertNotIn(continuous, self.service._reconnect_fence)
        self.assertNotIn(continuous, self.service._gap_pending)
        self.assertTrue(self.service._gap_queue.empty())
        self.assertEqual([row.open_time for row in self.store.get_candles(continuous, "1m")],
                         [last, last + MINUTE])
        self.assertTrue(self.service._can_alert(continuous, continuous_bar.close_time))

        self.now_ms = minute_floor + 2 * MINUTE + 1000
        jumped_bar = bar(last + 2 * MINUTE, symbol=jumped)
        await self.service._on_closed(jumped_bar)
        self.assertNotIn(jumped, self.service._reconnect_fence)
        self.assertIn(jumped, self.service._gap_pending)
        self.assertEqual(self.service._gap_start[jumped], last + MINUTE)
        self.assertEqual(self.service._gap_queue.qsize(), 1)
        self.assertFalse(self.service._can_alert(jumped, jumped_bar.close_time))
        self.assertEqual([row.open_time for row in self.store.get_candles(jumped, "1m")],
                         [last, last + 2 * MINUTE])

    def test_rest_budget_keeps_shared_ip_headroom_and_backs_off_early(self):
        budget = WeightBudget(2400)
        self.assertEqual(budget.per_minute, int(2400 * REST_BUDGET_FRACTION))
        budget.configure(1200)
        self.assertEqual(budget.per_minute, int(1200 * REST_BUDGET_FRACTION))
        before = time.monotonic()
        budget.observe_used(960)
        self.assertGreaterEqual(budget.cooldown_until, before + 64)

    async def test_request_records_all_weight_headers_and_never_retries_429(self):
        calls = 0

        class FakeResponse:
            status = 429
            headers = {"X-MBX-USED-WEIGHT-1M": "1888", "X-MBX-USED-WEIGHT-10S": "77",
                       "Retry-After": "120"}
            request_info = None
            history = ()

            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *args):
                return False

        def get(url, params=None):
            nonlocal calls
            calls += 1
            return FakeResponse()

        self.service._session = SimpleNamespace(get=get, close=AsyncMock())
        before = time.monotonic()
        with self.assertRaises(BinanceRateLimitError) as caught:
            await self.service._request("/fapi/v1/time")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(calls, 1)
        self.assertGreaterEqual(self.service._budget.cooldown_until, before + 119)
        self.assertEqual(self.service._budget.used_headers, {
            "X-MBX-USED-WEIGHT-1M": 1888, "X-MBX-USED-WEIGHT-10S": 77})
        self.assertEqual(self.service._rest_usage_history[-1]["status"], 429)

    async def test_rate_limit_body_also_stops_requests(self):
        class Response:
            status=200
            headers={}
            async def __aenter__(inner): return inner
            async def __aexit__(inner,*args): return False
            def raise_for_status(inner): pass
            async def json(inner): return {'code':-1003,'msg':'Too many requests'}
        get=__import__('unittest.mock',fromlist=['Mock']).Mock(return_value=Response())
        self.service._session=SimpleNamespace(get=get,close=AsyncMock())
        with self.assertRaises(BinanceRateLimitError):
            await self.service._request('/fapi/v1/time')
        self.assertEqual(get.call_count,1)
        self.assertGreater(self.service._budget.cooldown_until,time.monotonic()+59)

    def test_steady_state_open_interest_rest_poller_is_removed(self):
        self.assertFalse(hasattr(self.service, "_oi_poller"))
        self.assertFalse(self.service.status()["scheduler"]["steady_state_open_interest_rest"])

    def test_watermark_staleness_and_gap_gate(self):
        self.assertFalse(self.service._can_alert("1000PEPEUSDT", self.now_ms - 1))
        self.now_ms += MINUTE
        self.assertTrue(self.service._can_alert("1000PEPEUSDT", self.now_ms - 100))
        self.service._schedule_gap("1000PEPEUSDT", self.now_ms - 3 * MINUTE)
        self.assertFalse(self.service._can_alert("1000PEPEUSDT", self.now_ms - 100))

    async def test_history_paging_stores_without_live_signals(self):
        self.service._running = True
        calls = []

        async def fake_request(path, params=None, weight=1):
            calls.append((path, params, weight))
            return [[t, "100", "102", "99", "101", "10", t + MINUTE - 1,
                     "1000", 10, "6", "600", "0"]
                    for t in range(params["startTime"], min(params["endTime"] + 1,
                                  params["startTime"] + params["limit"] * MINUTE), MINUTE)]

        self.service._request = fake_request
        await self.service._fetch_range("1000PEPEUSDT", "1m", 0, 1000 * MINUTE - 1)
        self.assertEqual(self.store.counts()["candles"], 1000)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[1]["limit"] <= 499 for call in calls))
        self.assertEqual(self.store.list_alerts(), [])
        self.assertTrue(self.service._candidate_queue.empty())

    async def test_page_telemetry_is_committed_unique_inventory_and_resume_fetches_only_missing(self):
        self.service._running = True
        timeframe = MONITOR_TIMEFRAMES[0]
        interval = TIMEFRAME_MS[timeframe]
        calls = []
        fail_second = True

        async def fake_request(path, params=None, weight=1):
            calls.append(params.copy())
            if fail_second and len(calls) == 2:
                raise ConnectionError("temporary page interruption")
            return [[t, "100", "102", "99", "101", "10", t + interval - 1,
                     "1000", 10, "6", "600", "0"]
                    for t in range(params["startTime"], min(params["endTime"] + 1,
                                  params["startTime"] + params["limit"] * interval), interval)]

        self.service._request = fake_request
        with self.assertRaises(ConnectionError):
            await self.service._warm_pair("1000PEPEUSDT", timeframe)
        failed = self.service.download_status("1000PEPEUSDT", timeframe)
        self.assertEqual(failed["items"][0]["bars"], 499)
        self.assertEqual(failed["items"][0]["downloaded_bars"], 499)
        self.assertEqual(failed["items"][0]["state"], "error")
        self.assertEqual(self.store.candle_window_stats("1000PEPEUSDT", timeframe)["bars"], 499)
        saved_event = next(e for e in failed["events"] if e["type"] == "page_saved")
        self.assertEqual(saved_event["stored_bars"], 499)
        self.assertTrue(saved_event["latest_candle"]["closed"])
        first_missing = calls[0]["startTime"] + 499 * interval
        fail_second = False
        calls.clear()
        await self.service._warm_pair("1000PEPEUSDT", timeframe)
        self.assertEqual(calls[0]["startTime"], first_missing)
        completed = self.service.download_status("1000PEPEUSDT", timeframe)["items"][0]
        self.assertEqual(completed["cached_bars"], 499)
        self.assertEqual(completed["bars"], completed["target_bars"])
        self.assertEqual(completed["downloaded_bars"], completed["bars"])
        self.assertTrue(completed["download_complete"])
        self.assertEqual(completed["state"], "ready")

    async def test_download_window_inventory_never_counts_older_or_new_live_bars(self):
        self.service._running = True
        timeframe = MONITOR_TIMEFRAMES[-1]
        interval = TIMEFRAME_MS[timeframe]
        target = LOOKBACK[timeframe]["days"] * DAY // interval + 3
        self.now_ms = 20 * DAY + 10_000
        symbol = "1000PEPEUSDT"
        requested_end = self.now_ms // interval * interval - 1
        requested_start = requested_end + 1 - target * interval
        missing_open = requested_start + 10 * interval
        self.store.save_candles([
            bar(requested_start - interval, timeframe=timeframe),
            *[bar(open_time, timeframe=timeframe)
              for open_time in range(requested_start, requested_end + 1, interval)
              if open_time != missing_open],
        ])

        async def empty_history(path, params=None, weight=1):
            return []

        self.service._request = empty_history
        await self.service._warm_pair(symbol, timeframe)
        row = self.service.download_status(symbol, timeframe)["items"][0]
        self.assertEqual(row["requested_start"], requested_start)
        self.assertEqual(row["requested_end"], requested_end)
        self.assertEqual(row["target_bars"], target)
        self.assertEqual(row["bars"], target - 1)
        self.assertEqual(row["monitor_bars"], target)
        self.assertEqual(row["first_open_time"], requested_start)
        self.assertEqual(row["last_close_time"], requested_end)
        self.assertEqual(row["progress_pct"], completion_percentage(target - 1, target))
        self.assertEqual(row["window_missing_bars"], 1)
        self.assertTrue(row["request_complete"])
        self.assertFalse(row["window_complete"])
        self.assertFalse(row["download_complete"])
        self.assertEqual(row["state"], "gap")
        self.assertEqual(self.service.status()["history_progress"], 0)
        # A new closed live bar after the frozen request window may change
        # monitor readiness, but cannot increase this download's inventory.
        new_open = requested_end + 1
        self.store.save_candles([bar(new_open, timeframe=timeframe)])
        self.now_ms = new_open + interval + 10_000
        recent = self.store.get_candles(symbol, timeframe, target)
        self.service._set_pair_state(symbol, timeframe, recent)
        after = self.service.download_status(symbol, timeframe)["items"][0]
        self.assertEqual(after["bars"], target - 1)
        self.assertEqual(after["first_open_time"], requested_start)
        self.assertEqual(after["last_close_time"], requested_end)
        self.assertEqual(after["latest_candle"]["close_time"], requested_end)
        self.assertEqual(after["monitor_last_close_time"], new_open + interval - 1)
        self.assertEqual(after["progress_pct"], completion_percentage(target - 1, target))
        self.assertFalse(after["download_complete"])

    async def test_verified_zero_target_window_completes_download_but_never_monitor_readiness(self):
        self.service._running = True
        symbol = "JUSTLISTEDUSDT"
        interval = TIMEFRAME_MS["15m"]
        onboard = 20 * DAY
        self.now_ms = onboard + 1000
        self.service._metadata = {symbol: meta(symbol, onboardDate=onboard)}
        self.service._request = AsyncMock(side_effect=AssertionError("an empty closed window needs no REST page"))

        await self.service._warm_pair(symbol, "15m")
        row = self.service.download_status(symbol, "15m")["items"][0]
        self.assertEqual(row["requested_start"], onboard)
        self.assertEqual(row["requested_end"], onboard - 1)
        self.assertEqual(row["target_bars"], 0)
        self.assertEqual(row["bars"], 0)
        self.assertEqual(row["window_missing_bars"], 0)
        self.assertEqual(row["progress_pct"], 100)
        self.assertTrue(row["request_complete"])
        self.assertTrue(row["window_complete"])
        self.assertTrue(row["download_complete"])
        self.assertEqual(row["state"], "insufficient_history")
        self.assertEqual(row["reason"], "new_insufficient")
        self.assertEqual(self.store.get_candles(symbol, "15m"), [])
        self.assertEqual(self.service._request.await_count, 0)

        ordinary_empty = self.service._download_update(symbol, "5m", requested_start=0,
            requested_end=TIMEFRAME_MS["5m"] - 1, target_bars=1, bars=0,
            first_open_time=None, last_close_time=None, request_complete=True)
        self.assertFalse(ordinary_empty["window_complete"])
        self.assertFalse(ordinary_empty["download_complete"])
        malformed_zero = self.service._download_update(symbol, "5m", requested_start=0,
            requested_end=0, target_bars=0, bars=0, first_open_time=None,
            last_close_time=None, request_complete=True)
        self.assertFalse(malformed_zero["window_complete"])
        self.assertFalse(malformed_zero["download_complete"])

    async def test_listing_prefix_requires_one_confirmed_bar_and_complete_continuity_after_it(self):
        self.service._running = True
        timeframe = "5m"
        interval = TIMEFRAME_MS[timeframe]
        onboard = 20 * DAY
        self.now_ms = onboard + 10 * interval + 1000
        accepted = "PREFIXONEUSDT"
        internal_gap = "PREFIXGAPUSDT"
        two_missing = "PREFIXTWOUSDT"
        self.service._metadata = {
            accepted: meta(accepted, onboardDate=onboard),
            internal_gap: meta(internal_gap, onboardDate=onboard),
            two_missing: meta(two_missing, onboardDate=onboard),
        }
        self.store.save_candles([
            *[bar(onboard + i * interval, symbol=accepted, timeframe=timeframe) for i in range(1, 10)],
            *[bar(onboard + i * interval, symbol=internal_gap, timeframe=timeframe)
              for i in range(1, 10) if i != 5],
            *[bar(onboard + i * interval, symbol=two_missing, timeframe=timeframe) for i in range(2, 10)],
        ])
        calls = []

        async def confirmed_empty_page(path, params=None, weight=1):
            calls.append(params.copy())
            return []

        self.service._request = confirmed_empty_page
        await self.service._warm_pair(accepted, timeframe)
        row = self.service.download_status(accepted, timeframe)["items"][0]
        self.assertEqual([(call["startTime"], call["endTime"]) for call in calls],
                         [(onboard, onboard + interval - 1)])
        self.assertEqual(row["target_bars"], 10)
        self.assertEqual(row["bars"], 9)
        self.assertEqual(row["confirmed_unavailable_prefix_bars"], 1)
        self.assertEqual(row["window_missing_bars"], 0)
        self.assertTrue(row["window_complete"])
        self.assertTrue(row["download_complete"])
        self.assertEqual(row["state"], "insufficient_history")
        self.assertIn("挂牌起点前缀", row["stage"])

        for symbol in (internal_gap, two_missing):
            calls.clear()
            await self.service._warm_pair(symbol, timeframe)
            rejected = self.service.download_status(symbol, timeframe)["items"][0]
            self.assertGreaterEqual(len(calls), 1)
            self.assertEqual(calls[0]["startTime"], onboard)
            self.assertEqual(rejected["confirmed_unavailable_prefix_bars"], 0)
            self.assertEqual(rejected["window_missing_bars"], 2)
            self.assertFalse(rejected["window_complete"])
            self.assertFalse(rejected["download_complete"])

    async def test_startup_cached_inventory_waits_for_window_verification_before_percentage(self):
        self.store.save_candles([bar(i * 5 * MINUTE, timeframe="5m") for i in range(2200)])
        never = asyncio.Event()

        async def fake_supervisor():
            await never.wait()

        self.service._run = fake_supervisor
        await self.service.start()
        self.assertEqual(self.service.download_status()["items"], [])
        self.assertEqual(self.service._download_jobs[("1000PEPEUSDT", "5m")]["cached_bars"], 2200)
        self.service._metadata = {"1000PEPEUSDT": meta()}
        row = self.service.download_status("1000PEPEUSDT", "5m")["items"][0]
        self.assertEqual(row["cached_bars"], 2200)
        self.assertEqual(row["bars"], 2200)
        self.assertIsNone(row["requested_start"])
        self.assertIsNone(row["progress_pct"])
        self.assertFalse(row["window_complete"])
        self.assertFalse(row["download_complete"])

    def test_one_missing_bar_never_rounds_download_percentage_up_to_complete(self):
        timeframe = MONITOR_TIMEFRAMES[0]
        interval = TIMEFRAME_MS[timeframe]
        self.assertEqual(completion_percentage(4322, 4323), 99.9)
        self.assertEqual(completion_percentage(4323, 4323), 100)
        self.service._download_update("1000PEPEUSDT", timeframe, requested_start=0,
            requested_end=4323 * interval - 1, first_open_time=0, last_close_time=4323 * interval - 1,
            target_bars=4323, bars=4322, request_complete=True)
        row = self.service.download_status("1000PEPEUSDT", timeframe)["items"][0]
        self.assertEqual(row["progress_pct"], 99.9)
        self.assertFalse(row["window_complete"])
        self.assertFalse(row["download_complete"])
        self.service._download_update("1000PEPEUSDT", timeframe, bars=4323)
        completed = self.service.download_status("1000PEPEUSDT", timeframe)["items"][0]
        self.assertEqual(completed["progress_pct"], 100)
        self.assertTrue(completed["window_complete"])
        self.assertTrue(completed["download_complete"])

    async def test_failed_database_commit_never_emits_page_saved(self):
        self.service._running = True
        timeframe = MONITOR_TIMEFRAMES[0]
        interval = TIMEFRAME_MS[timeframe]

        async def fake_request(path, params=None, weight=1):
            return [[0, "100", "102", "99", "101", "10", interval - 1,
                     "1000", 10, "6", "600", "0"]]

        self.service._request = fake_request
        with patch.object(self.store, "save_candles", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.service._fetch_range("1000PEPEUSDT", timeframe, 0, interval - 1)
        snapshot = self.service.download_status("1000PEPEUSDT", timeframe)
        self.assertEqual(snapshot["items"][0]["downloaded_bars"], 0)
        self.assertFalse(any(e["type"] == "page_saved" for e in snapshot["events"]))

    def test_download_snapshot_is_readonly_filtered_and_has_bounded_cursor_events(self):
        timeframe = MONITOR_TIMEFRAMES[0]
        self.service._download_update("1000PEPEUSDT", timeframe,
                                      latest_candle=bar(0, timeframe=timeframe).to_dict())
        for i in range(650):
            self.service._download_event("page_saved", "1000PEPEUSDT", timeframe, count=1)
        with patch.object(self.store, "candle_inventory", side_effect=AssertionError("no DB")), \
             patch.object(self.store, "candle_window_stats", side_effect=AssertionError("no DB")):
            snapshot = self.service.download_status("1000pepeusdt", timeframe, after=600)
        self.assertEqual(len(snapshot["items"]), 1)
        self.assertEqual(len(snapshot["events"]), 50)
        self.assertEqual(snapshot["cursor"], 650)
        self.assertEqual(snapshot["oldest_available_seq"], 151)
        self.assertEqual(len(self.service.download_status()["events"]), 100)
        self.assertTrue(self.service.download_status(after=0)["events_truncated"])
        snapshot["items"][0]["latest_candle"]["close"] = 999
        self.assertEqual(self.service.download_status("1000PEPEUSDT", timeframe)["items"][0]["latest_candle"]["close"], 101)

    def test_pending_warmup_cannot_be_overwritten_or_reported_as_complete(self):
        self.service._state = "warming"
        self.service._running = True
        self.service._last_refresh = self.now_ms
        self.service._connections["kline-0"] = {"state": "connected", "last_message": self.now_ms}
        for tf in MONITOR_TIMEFRAMES:
            self.service._queue_warm("1000PEPEUSDT", tf)
            self.service._set_pair_state("1000PEPEUSDT", tf, [])
        self.assertTrue(all(v["state"] == "queued" for v in self.service._pair_state.values()))
        # Also guard old/inconsistent state injected by an earlier recovery path.
        for state in self.service._pair_state.values():
            state["state"] = "insufficient_history"
        status = self.service.status()
        self.assertEqual(status["history_progress"], 0)
        self.assertEqual(status["ready_pairs"], 0)
        self.assertEqual(status["warming_pairs"], len(MONITOR_TIMEFRAMES))
        self.assertEqual(status["state"], "warming")

    def test_unclosed_live_quote_is_visible_without_storage_or_candidate(self):
        start = self.now_ms // MINUTE * MINUTE
        event = {"s": "1000PEPEUSDT", "E": self.now_ms,
                 "k": {"i": "1m", "t": start, "T": start + MINUTE - 1, "o": "100", "h": "102",
                       "l": "99", "c": "101", "v": "10", "q": "1000", "Q": "600", "n": 10, "x": False}}
        self.assertIsNone(self.service._consume_live_kline(event))
        quote = self.service.download_status("1000PEPEUSDT", MONITOR_TIMEFRAMES[0])["items"][0]["live_quote"]
        self.assertFalse(quote["closed"])
        self.assertEqual(quote["price"], 101)
        self.assertEqual(self.store.counts()["candles"], 0)
        self.assertTrue(self.service._candidate_queue.empty())
        self.now_ms += 90_001
        self.assertTrue(self.service.download_status("1000PEPEUSDT", MONITOR_TIMEFRAMES[0])["items"][0]["live_quote"]["stale"])

    async def test_clock_sync_uses_midpoint_and_excludes_budget_wait_sample(self):
        async def fake_clock(path, params=None, weight=1):
            return {"serverTime": 1_001_000}

        self.service._request = fake_clock
        with patch("monitor.collector.time.time", return_value=1000), \
             patch("monitor.collector.time.monotonic", side_effect=[10.0, 12.0]):
            await self.service._sync_clock()
        self.assertEqual(self.service._clock_offset, 0)
        self.assertEqual(self.service._clock_rtt_ms, 2000)

        async def sampled_clock(path, params=None, weight=1):
            self.service._clock_sample = {"began": 11.0, "server_time": 1_002_200,
                                           "midpoint": 1_002_000, "rtt_ms": 500}
            return {"serverTime": 1_002_200}

        self.service._request = sampled_clock
        with patch("monitor.collector.time.time", return_value=1000), \
             patch("monitor.collector.time.monotonic", side_effect=[10.0, 12.0]):
            await self.service._sync_clock()
        self.assertEqual(self.service._clock_offset, 200)
        self.assertEqual(self.service._clock_rtt_ms, 500)
        self.service._clock_sample = {}
        self.service._request = fake_clock
        with patch("monitor.collector.time.time", return_value=1001), \
             patch("monitor.collector.time.monotonic", side_effect=[20.0, 28.0]):
            await self.service._sync_clock()
        self.assertEqual(self.service._clock_offset, 200)
        self.assertEqual(self.service._clock_rtt_ms, 500)

    async def test_spread_uses_only_fresh_websocket_book_ticker(self):
        self.service._request = AsyncMock(side_effect=AssertionError("spread must not call REST"))
        self.service._consume_book_event({"e": "bookTicker", "s": "1000PEPEUSDT", "st": 1,
            "E": self.now_ms + 2000, "T": self.now_ms + 1900, "b": "100", "a": "100.01"})
        self.assertGreater(await self.service._spread("1000PEPEUSDT"), 0)
        self.service._book_cache.clear()
        for stamp in (self.now_ms - 90_001, self.now_ms + 10_000):
            self.service._consume_book_event({"e": "bookTicker", "s": "1000PEPEUSDT", "st": 1,
                "E": stamp, "T": stamp, "b": "100", "a": "100.01"})
            with self.assertRaises(ValueError):
                await self.service._spread("1000PEPEUSDT")
        self.service._request.assert_not_awaited()

    async def test_book_ticker_uses_one_public_websocket_stream(self):
        self.service._running = True
        events = [
            {"data": {"e": "bookTicker", "s": "1000PEPEUSDT", "st": 1,
                "E": self.now_ms - 100, "T": self.now_ms - 120, "b": "100", "a": "100.02"}},
            {"data": {"e": "bookTicker", "s": "CMUSD", "st": 2,
                "E": self.now_ms - 50, "T": self.now_ms - 60, "b": "1", "a": "2"}},
        ]
        drained, never = asyncio.Event(), asyncio.Event()
        addresses = []

        class FakeSocket:
            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *args):
                return False

            async def receive(inner):
                if events:
                    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(events.pop(0)))
                drained.set()
                await never.wait()

        def connect(url, **kwargs):
            addresses.append(url)
            return FakeSocket()

        self.service._session = SimpleNamespace(ws_connect=connect, close=AsyncMock())
        task = asyncio.create_task(self.service._book_stream())
        try:
            await asyncio.wait_for(drained.wait(), 2)
            self.assertIn("/public/stream?streams=!bookTicker", addresses[0])
            self.assertGreater(await self.service._spread("1000PEPEUSDT"), 0)
            self.assertNotIn("CMUSD", self.service._book_cache)
            connection = next(row for row in self.service.status()["connections"]
                              if row["name"] == "book-ticker")
            self.assertEqual(connection["subscriptions"], 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_midpoint_uncertainty_never_promotes_forming_rest_candle(self):
        self.service._running = True
        self.service._clock_synced_at = self.now_ms
        self.service._clock_rtt_ms = 3000
        boundary = self.now_ms // MINUTE * MINUTE
        self.now_ms = boundary + 1000  # midpoint clock may be ahead at the minute edge
        start = boundary - MINUTE

        async def fake_request(path, params=None, weight=1):
            return [[start, "100", "102", "99", "101", "10", boundary - 1,
                     "1000", 10, "6", "600", "0"]]

        self.service._request = fake_request
        self.assertGreater(self.now_ms, boundary - 1)
        self.assertLess(self.service._confirmed_history_now(), boundary - 1)
        rows = await self.service._fetch_range("1000PEPEUSDT", "1m", start, boundary - 1)
        self.assertEqual(rows, [])
        self.assertEqual(self.store.counts()["candles"], 0)

    async def test_repeated_resume_preserves_active_tasks_pages_cursor_and_watermark(self):
        never = asyncio.Event()

        async def fake_supervisor():
            await never.wait()

        self.service._run = fake_supervisor
        await self.service.start()
        self.service._state = "warming"
        self.service._metadata = {"1000PEPEUSDT": meta()}
        timeframe = MONITOR_TIMEFRAMES[0]
        self.service._queue_warm("1000PEPEUSDT", timeframe)
        self.service._download_update("1000PEPEUSDT", timeframe,
            state="downloading", bars=499, downloaded_bars=499, pages=1)
        self.service._download_event("page_saved", "1000PEPEUSDT", timeframe, count=499)
        identity, cursor, watermark, runner = (self.service._download_session_id, self.service._download_cursor,
                                              self.service._watermark, self.service._runner)
        results = await asyncio.gather(self.service.retry_connection(), self.service.retry_connection())
        self.assertTrue(all(r["resume_action"] == "continued" for r in results))
        self.assertIs(self.service._runner, runner)
        self.assertEqual(self.service._download_session_id, identity)
        self.assertEqual(self.service._watermark, watermark)
        self.assertGreater(self.service._download_cursor, cursor)
        self.assertEqual(self.service._warm_queue.qsize(), 1)
        row = self.service.download_status("1000PEPEUSDT", timeframe)["items"][0]
        self.assertEqual(row["downloaded_bars"], 499)
        self.assertEqual(row["bars"], 499)

    async def test_retry_immediately_requeues_effective_stale_but_skips_active_work(self):
        timeframe = MONITOR_TIMEFRAMES[0]
        stale, repairing, warming = "STALEUSDT", "REPAIRINGUSDT", "WARMINGUSDT"
        self.service._metadata = {symbol: meta(symbol) for symbol in (stale, repairing, warming)}
        old_close = self.now_ms - TIMEFRAME_MS[timeframe] - 90_001
        for symbol in self.service._metadata:
            self.service._pair_state[(symbol, timeframe)] = {"state": "ready", "last_close": old_close}
        self.service._gap_pending.add(repairing)
        self.service._warm_pending.add((warming, timeframe))
        self.service._running = True
        never = asyncio.Event()
        self.service._runner = asyncio.create_task(never.wait())

        self.assertEqual(self.service._pair_state[(stale, timeframe)]["state"], "ready")
        self.assertEqual(self.service._effective_pair_states()[(stale, timeframe)]["state"], "stale")
        result = await self.service.retry_connection()

        self.assertEqual(result["resume_action"], "continued")
        self.assertEqual(result["requeued_pairs"], 1)
        self.assertIn((stale, timeframe), self.service._warm_pending)
        self.assertIn((warming, timeframe), self.service._warm_pending)
        self.assertNotIn((repairing, timeframe), self.service._warm_pending)
        self.assertEqual(self.service._warm_queue.qsize(), 1)

    async def test_live_utc_aggregation_uses_seeded_minutes(self):
        start = self.now_ms // (5 * MINUTE) * (5 * MINUTE)
        self.now_ms = start + 5 * MINUTE + 1000
        self.store.save_candles([bar(start + i * MINUTE) for i in range(4)])
        await self.service._on_closed(bar(start + 4 * MINUTE))
        rows = self.store.get_candles("1000PEPEUSDT", "5m")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].open_time, start)
        self.assertEqual(rows[0].quote_volume, 5000)
        self.assertEqual(rows[0].close_time, start + 5 * MINUTE - 1)
        self.assertEqual(self.store.list_alerts(), [])

    async def test_boundary_persistence_never_waits_for_full_history_analysis_queue(self):
        timeframe = "5m"
        interval = TIMEFRAME_MS[timeframe]
        start = self.now_ms // interval * interval
        final_minute = bar(start + interval - MINUTE)
        self.now_ms = final_minute.close_time + 1000
        self.service._watermark = final_minute.close_time - MINUTE
        count = LOOKBACK[timeframe]["minimum_bars"] + 3
        history = [bar(start - (count - i) * interval, timeframe=timeframe) for i in range(count)]
        self.store.save_candles(history)
        self.service._set_pair_state("1000PEPEUSDT", timeframe, history)
        self.store.save_candles([bar(start + i * MINUTE) for i in range(4)])
        self.service._analysis_queue = asyncio.Queue(maxsize=1)
        self.service._analysis_queue.put_nowait(("FULLUSDT", timeframe, 0, {}, None))

        with patch("monitor.collector.analyze", side_effect=AssertionError("analysis ran on ingest path")), \
             patch.object(self.store, "get_candles", wraps=self.store.get_candles) as reads:
            await self.service._on_closed(final_minute)

        self.assertEqual(self.store.get_candles("1000PEPEUSDT", timeframe)[-1].close_time,
                         final_minute.close_time)
        self.assertEqual([(call.args[1], call.args[2]) for call in reads.call_args_list], [("1m", 5)])
        self.assertEqual(self.service._analysis_dropped, 1)
        self.assertEqual(self.service._analysis_queue.qsize(), 1)
        self.assertFalse(self.service._gap_pending)

    async def test_fifteen_minute_boundary_enqueues_only_formal_5m_and_15m_scans(self):
        start = self.now_ms // TIMEFRAME_MS["15m"] * TIMEFRAME_MS["15m"]
        final_minute = bar(start + TIMEFRAME_MS["15m"] - MINUTE)
        self.now_ms = final_minute.close_time + 1000
        self.service._watermark = final_minute.close_time - MINUTE
        for timeframe, last_open in (("5m", start + 5 * MINUTE), ("15m", start - 15 * MINUTE)):
            interval = TIMEFRAME_MS[timeframe]
            count = LOOKBACK[timeframe]["minimum_bars"] + 3
            history = [bar(last_open - (count - 1 - i) * interval, timeframe=timeframe)
                       for i in range(count)]
            self.store.save_candles(history)
            self.service._set_pair_state("1000PEPEUSDT", timeframe, history)
        self.store.save_candles([bar(start + i * MINUTE) for i in range(14)])

        await self.service._on_closed(final_minute)
        jobs = [self.service._analysis_queue.get_nowait() for _ in range(2)]
        for _ in jobs:
            self.service._analysis_queue.task_done()
        self.assertEqual({job[1] for job in jobs}, set(MONITOR_TIMEFRAMES))
        self.assertTrue(all(job[2] == final_minute.close_time for job in jobs))
        await self.service._on_closed(final_minute)
        self.assertTrue(self.service._analysis_queue.empty())

    async def test_first_fifteen_minute_boundary_after_restart_uses_verified_five_minute_prefix(self):
        symbol = "1000PEPEUSDT"
        start = self.now_ms // TIMEFRAME_MS["15m"] * TIMEFRAME_MS["15m"]
        final_minute = bar(start + 14 * MINUTE)
        self.now_ms = final_minute.close_time + 1000
        self.service._watermark = 0
        # Native warmup ended at the last confirmed boundary before this live
        # session: two 5m children from the current 15m bucket already exist,
        # while the 15m state ends at the preceding bucket.
        for timeframe, last_open in (("5m", start + 5 * MINUTE),
                                     ("15m", start - 15 * MINUTE)):
            interval = TIMEFRAME_MS[timeframe]
            count = LOOKBACK[timeframe]["minimum_bars"] + 3
            history = [bar(last_open - (count - 1 - index) * interval,
                           timeframe=timeframe) for index in range(count)]
            self.store.save_candles(history)
            self.service._set_pair_state(symbol, timeframe, history)
        # Model a restart at minute 10. Raw 1m minute 0..9 is intentionally not
        # downloaded, but minute 10..14 completes the third verified 5m child.
        self.store.save_candles([bar(start + index * MINUTE) for index in range(10, 14)])

        await self.service._on_closed(final_minute)

        self.assertEqual([c.open_time for c in self.store.get_candles(
            symbol, "1m", 20, start=start, end=final_minute.open_time)],
            [start + index * MINUTE for index in range(10, 15)])
        children = self.store.get_candles(symbol, "5m", 3, start=start)
        fifteen = self.store.get_candles(symbol, "15m", 1, start=start)
        self.assertEqual([c.open_time for c in children],
                         [start, start + 5 * MINUTE, start + 10 * MINUTE])
        self.assertEqual(len(fifteen), 1)
        self.assertEqual(fifteen[0].quote_volume,
                         sum(child.quote_volume for child in children))
        self.assertEqual(fifteen[0].trades, sum(child.trades for child in children))
        self.assertFalse(self.service._gap_pending)
        self.assertTrue(self.service._gap_queue.empty())
        jobs = [self.service._analysis_queue.get_nowait() for _ in range(2)]
        for _ in jobs:
            self.service._analysis_queue.task_done()
        self.assertEqual({job[1] for job in jobs}, set(MONITOR_TIMEFRAMES))

    async def test_received_close_watermark_prevents_backlog_from_becoming_false_gap(self):
        symbol = "1000PEPEUSDT"
        durable = self.now_ms - 4 * MINUTE
        self.service._last_minute[symbol] = durable
        self.service._last_received_minute[symbol] = self.now_ms - MINUTE

        async def one_cycle(_delay):
            self.service._running = False

        self.service._running = True
        with patch("monitor.collector.asyncio.sleep", side_effect=one_cycle):
            await self.service._health_loop()
        self.assertFalse(self.service._gap_pending)

        self.service._last_received_minute.clear()
        self.service._running = True
        with patch("monitor.collector.asyncio.sleep", side_effect=one_cycle):
            await self.service._health_loop()
        self.assertEqual(self.service._gap_start[symbol], durable + MINUTE)

    async def test_received_watermark_cannot_invent_formal_stale_repair(self):
        symbol = "1000PEPEUSDT"
        interval = TIMEFRAME_MS["5m"]
        boundary = self.now_ms // interval * interval
        self.now_ms = boundary + 7 * MINUTE + 1000
        self.service._pair_state[(symbol, "5m")] = {
            "state": "ready", "last_close": boundary - 1}
        # Receipt has crossed the missing 5m boundary, but the durable writer
        # has not. Neither a raw nor a formal REST repair may be inferred yet.
        self.service._last_minute[symbol] = boundary + 3 * MINUTE
        self.service._last_received_minute[symbol] = boundary + 6 * MINUTE
        self.service._running = True

        async def one_cycle(_delay):
            self.service._running = False

        with patch("monitor.collector.asyncio.sleep", side_effect=one_cycle):
            await self.service._health_loop()

        self.assertFalse(self.service._gap_pending)
        self.assertTrue(self.service._gap_queue.empty())

    async def test_health_repairs_proved_formal_tail_with_native_period_only(self):
        symbol = "1000PEPEUSDT"
        boundary = self.now_ms // TIMEFRAME_MS["15m"] * TIMEFRAME_MS["15m"]
        self.now_ms = boundary + 7 * MINUTE + 1000
        self.service._watermark = 0
        for timeframe in MONITOR_TIMEFRAMES:
            interval = TIMEFRAME_MS[timeframe]
            count = LOOKBACK[timeframe]["minimum_bars"] + 3
            history = [bar(boundary - (count - i) * interval, timeframe=timeframe)
                       for i in range(count)]
            self.store.save_candles(history)
            self.service._set_pair_state(symbol, timeframe, history)
        # Durable raw progress proves that the 5m bucket ending at +4:59 must
        # exist. The 15m pair ending at -0:01 is already current.
        durable = bar(boundary + 6 * MINUTE)
        self.store.save_candles([durable])
        self.service._last_minute[symbol] = durable.open_time
        self.service._running = True

        async def one_cycle(_delay):
            self.service._running = False

        with patch("monitor.collector.asyncio.sleep", side_effect=one_cycle):
            await self.service._health_loop()

        self.assertEqual(self.service._gap_start[symbol], boundary)
        self.assertEqual(self.service._gap_queue.qsize(), 1)
        calls = []

        async def native_fetch(request_symbol, timeframe, start, end):
            calls.append((request_symbol, timeframe, start, end))
            self.assertIn(timeframe, MONITOR_TIMEFRAMES)
            repaired = bar(boundary, timeframe=timeframe)
            self.store.save_candles([repaired])
            return [repaired]

        self.service._fetch_range = native_fetch
        self.service._running = True
        worker = asyncio.create_task(self.service._recover_worker())
        try:
            await asyncio.wait_for(self.service._gap_queue.join(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual(calls, [(symbol, "5m", boundary, boundary + 5 * MINUTE - 1)])
        self.assertTrue(all(timeframe != "1m" for _, timeframe, _, _ in calls))
        self.assertEqual(self.service._pair_state[(symbol, "5m")]["state"], "ready")
        self.assertEqual(self.service._pair_state[(symbol, "5m")]["last_close"],
                         boundary + 5 * MINUTE - 1)
        self.assertNotIn(symbol, self.service._gap_pending)
        self.assertGreater(self.service._symbol_gate[symbol], boundary + 5 * MINUTE - 1)

    async def test_missing_minutes_never_create_complete_higher_bar(self):
        start = self.now_ms // (5 * MINUTE) * (5 * MINUTE)
        self.now_ms = start + 5 * MINUTE + 1000
        self.store.save_candles([bar(start + i * MINUTE) for i in (0, 1, 3)])
        await self.service._on_closed(bar(start + 4 * MINUTE))
        self.assertEqual(self.store.get_candles("1000PEPEUSDT", "5m"), [])
        self.assertEqual(self.service._gap_start["1000PEPEUSDT"], start)
        self.assertEqual(self.service._gap_queue.qsize(), 1)
        self.assertFalse(self.service._can_alert("1000PEPEUSDT", start + 5 * MINUTE - 1))

    async def test_unseen_late_final_close_is_persisted_and_formal_bucket_stays_gated(self):
        symbol = "1000PEPEUSDT"
        start = self.now_ms // TIMEFRAME_MS["5m"] * TIMEFRAME_MS["5m"]
        self.now_ms = start + 7 * MINUTE
        self.service._watermark = 0
        for minute in range(4):
            await self.service._on_closed(bar(start + minute * MINUTE))
        # The following minute arrives first and proves a raw jump. The delayed
        # boundary-final close must not be discarded merely because it is older.
        await self.service._on_closed(bar(start + 5 * MINUTE))
        self.assertNotIn(start + 4 * MINUTE,
                         [c.open_time for c in self.store.get_candles(symbol, "1m")])

        late = bar(start + 4 * MINUTE)
        await self.service._on_closed(late)

        raw = self.store.get_candles(symbol, "1m")
        formal = self.store.get_candles(symbol, "5m")
        self.assertEqual([c.open_time for c in raw],
                         [start + minute * MINUTE for minute in range(6)])
        self.assertEqual([c.open_time for c in formal], [start])
        self.assertIn(symbol, self.service._gap_pending)
        self.assertEqual(self.service._gap_start[symbol], start + 4 * MINUTE)
        self.assertFalse(self.service._can_alert(symbol, formal[-1].close_time))

        # A true duplicate still has no side effects or extra persistence count.
        received = self.service._received_closed
        await self.service._on_closed(late)
        self.assertEqual(self.service._received_closed, received)
        self.assertEqual(self.store.counts()["candles"], 7)

    async def test_startup_prefix_gap_repairs_native_period_without_raw_rest(self):
        symbol = "1000PEPEUSDT"
        timeframe = "5m"
        interval = TIMEFRAME_MS[timeframe]
        # Five minutes after a 15m boundary makes only the 5m bucket due, which
        # lets this test prove the recovery request is precisely native-period.
        start = 10 * DAY + 5 * MINUTE
        final_minute = bar(start + interval - MINUTE)
        self.now_ms = final_minute.close_time + 1000
        self.service._watermark = 0
        count = LOOKBACK[timeframe]["minimum_bars"] + 3
        history = [bar(start - (count - i) * interval, timeframe=timeframe) for i in range(count)]
        self.store.save_candles(history)
        self.service._set_pair_state(symbol, timeframe, history)
        self.assertEqual(self.service._pair_state[(symbol, timeframe)]["state"], "ready")

        # Model a restart that connected after the first minute had already
        # closed: raw receipt continues normally, but the boundary is partial.
        self.store.save_candles([bar(start + i * MINUTE) for i in (1, 2, 3)])
        await self.service._on_closed(final_minute)
        self.assertEqual(self.store.get_candles(symbol, timeframe)[-1].close_time, start - 1)
        self.assertIn(symbol, self.service._gap_pending)
        self.assertEqual(self.service._gap_start[symbol], start)
        self.assertFalse(self.service._can_alert(symbol, final_minute.close_time))

        calls = []

        async def native_fetch(request_symbol, request_timeframe, request_start, request_end):
            calls.append((request_symbol, request_timeframe, request_start, request_end))
            self.assertIn(request_timeframe, MONITOR_TIMEFRAMES)
            self.assertNotEqual(request_timeframe, "1m")
            if request_timeframe == timeframe:
                repaired = bar(start, timeframe=timeframe)
                self.store.save_candles([repaired])
                return [repaired]
            return []

        self.service._fetch_range = native_fetch
        self.service._running = True
        worker = asyncio.create_task(self.service._recover_worker())
        try:
            await asyncio.wait_for(self.service._gap_queue.join(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual([(row[0], row[1]) for row in calls], [(symbol, timeframe)])
        self.assertEqual(self.service._pair_state[(symbol, timeframe)]["state"], "ready")
        self.assertEqual(self.service._pair_state[(symbol, timeframe)]["last_close"], final_minute.close_time)
        self.assertNotIn(symbol, self.service._gap_pending)
        # Recovery suppresses the repaired close itself; only later closes may
        # become actionable after continuity is established again.
        self.assertFalse(self.service._can_alert(symbol, final_minute.close_time))

    async def test_fifteen_minute_partial_boundary_queues_one_symbol_repair_before_analysis(self):
        symbol = "1000PEPEUSDT"
        interval = TIMEFRAME_MS["15m"]
        start = self.now_ms // interval * interval
        final_minute = bar(start + interval - MINUTE)
        self.now_ms = final_minute.close_time + 1000
        self.service._watermark = 0
        for timeframe, last_open in (("5m", start + 5 * MINUTE), ("15m", start - interval)):
            period = TIMEFRAME_MS[timeframe]
            count = LOOKBACK[timeframe]["minimum_bars"] + 3
            history = [bar(last_open - (count - 1 - i) * period, timeframe=timeframe)
                       for i in range(count)]
            self.store.save_candles(history)
            self.service._set_pair_state(symbol, timeframe, history)
        # Missing minute 10 invalidates the final 5m bucket and the enclosing
        # 15m bucket. Both failures must coalesce into one symbol repair.
        self.store.save_candles([bar(start + i * MINUTE) for i in range(14) if i != 10])

        await self.service._on_closed(final_minute)

        self.assertEqual(self.service._gap_queue.qsize(), 1)
        self.assertEqual(self.service._gap_start[symbol], start)
        self.assertTrue(self.service._analysis_queue.empty())
        self.assertFalse(self.service._can_alert(symbol, final_minute.close_time))

    async def test_duplicate_closed_event_is_idempotent(self):
        start = self.now_ms // MINUTE * MINUTE - MINUTE
        await self.service._on_closed(bar(start))
        await self.service._on_closed(bar(start))
        self.assertEqual(self.service._received_closed, 1)
        self.assertEqual(self.store.counts()["candles"], 1)

    async def test_pending_pair_skips_baseline_read_but_saves_close_and_other_period(self):
        self.now_ms = 10 * DAY + 5 * MINUTE + 1000
        self.store.save_candles([bar(10 * DAY + i * MINUTE) for i in range(4)])
        self.service._queue_warm("1000PEPEUSDT", "5m")
        with patch.object(self.store, "get_candles", wraps=self.store.get_candles) as reads:
            await self.service._on_closed(bar(10 * DAY + 4 * MINUTE))
        self.assertEqual(self.store.candle_window_stats("1000PEPEUSDT", "1m")["bars"], 5)
        self.assertEqual(self.store.candle_window_stats("1000PEPEUSDT", "5m")["bars"], 1)
        # Reading five minute components to aggregate is still necessary;
        # reading the pending 2019-bar 5m baseline is not.
        self.assertEqual([(call.args[1], call.args[2]) for call in reads.call_args_list],
                         [("1m", 5)])
        self.assertEqual(self.service._pair_state[("1000PEPEUSDT", "5m")]["state"], "queued")
        self.assertIn(("1000PEPEUSDT", "5m"), self.service._pair_state)

    def test_internal_gap_never_reports_ready(self):
        minimum = LOOKBACK["1m"]["minimum_bars"] + 3
        candles = [bar(i * MINUTE) for i in range(minimum + 10) if i != 1]
        self.now_ms = candles[-1].close_time + 1
        self.service._set_pair_state("1000PEPEUSDT", "1m", candles)
        self.assertEqual(self.service._pair_state[("1000PEPEUSDT", "1m")]["state"], "gap")

    def test_ready_status_ages_out_without_new_candles(self):
        self.service._state = "warming"
        for tf in MONITOR_TIMEFRAMES:
            self.service._pair_state[("1000PEPEUSDT", tf)] = {"state": "ready", "last_close": self.now_ms - 1}
        self.service._connections["kline-0"] = {"state": "connected", "last_message": self.now_ms}
        self.assertEqual(self.service.status()["ready_pairs"], len(MONITOR_TIMEFRAMES))
        self.now_ms += max(TIMEFRAME_MS[tf] for tf in MONITOR_TIMEFRAMES) + 90_001
        self.assertLess(self.service.status()["ready_pairs"], len(MONITOR_TIMEFRAMES))
        self.assertEqual(self.service.status()["state"], "reconnecting")

    async def test_health_loop_periodically_requeues_effective_stale_only_when_idle(self):
        timeframe = MONITOR_TIMEFRAMES[0]
        stale, repairing, warming = "STALEUSDT", "REPAIRINGUSDT", "WARMINGUSDT"
        self.service._metadata = {symbol: meta(symbol) for symbol in (stale, repairing, warming)}
        old_close = self.now_ms - TIMEFRAME_MS[timeframe] - 90_001
        for symbol in self.service._metadata:
            self.service._pair_state[(symbol, timeframe)] = {"state": "ready", "last_close": old_close}
        self.service._gap_pending.add(repairing)
        self.service._warm_pending.add((warming, timeframe))
        self.service._running = True
        sleeps = 0

        async def twenty_fast_ticks(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 20:
                self.service._running = False

        with patch("monitor.collector.asyncio.sleep", new=twenty_fast_ticks):
            await self.service._health_loop()

        self.assertEqual(sleeps, 20)
        self.assertIn((stale, timeframe), self.service._warm_pending)
        self.assertIn((warming, timeframe), self.service._warm_pending)
        self.assertNotIn((repairing, timeframe), self.service._warm_pending)
        self.assertEqual(self.service._warm_queue.qsize(), 1)

    def test_connection_before_first_payload_does_not_break_status(self):
        self.service._state = "warming"
        self.service._connections["kline-0"] = {"state": "connected", "last_message": None}
        self.assertEqual(self.service.status()["state"], "reconnecting")

    def test_oi_needs_same_duration_history_and_excludes_current_window(self):
        self.assertIsNone(self.service.oi_enrichment("1000PEPEUSDT", "1m", 99 * MINUTE - 1)["oi_ratio"])
        for i in range(100):
            self.store.save_oi(dict(symbol="1000PEPEUSDT", timestamp=i * MINUTE + 1000,
                                   source_time=i * MINUTE + 1000,
                                   open_interest_quantity=1000 * 1.001**i, mark_price=1))
        values = self.service.oi_enrichment("1000PEPEUSDT", "1m", 100 * MINUTE - 1)
        self.assertGreaterEqual(values["oi_sample_count"], 30)
        self.assertAlmostEqual(values["oi_ratio"], 1, places=8)
        self.assertAlmostEqual(values["oi_change_pct"], (1.001**2 - 1) * 100, places=8)

    def test_oi_gaps_remain_unscored(self):
        for i in range(100):
            self.store.save_oi(dict(symbol="1000PEPEUSDT", timestamp=i * 5 * MINUTE,
                                   source_time=i * 5 * MINUTE,
                                   open_interest_quantity=1000 + i, mark_price=1))
        values = self.service.oi_enrichment("1000PEPEUSDT", "1m", 500 * MINUTE - 1)
        self.assertIsNone(values["oi_ratio"])

    def test_old_oi_content_cannot_be_refreshed_by_receipt_timestamp(self):
        for symbol, lag in (("LEGACYUSDT", None), ("STALEUSDT", 900_000), ("DELAYUSDT", 70_000)):
            for i in range(100):
                observed = DAY + i * MINUTE + 1000
                self.store.save_oi(dict(symbol=symbol, timestamp=observed,
                    source_time=observed - lag if lag is not None else None,
                    open_interest_quantity=1000 * 1.001**i, mark_price=1))
            # 70s between source and receipt is inside the accepted transport
            # bound, but source content at this candle boundary is >75s old.
            self.assertIsNone(self.service.oi_enrichment(symbol, "1m", DAY + 100 * MINUTE - 1)["oi_ratio"])

    def test_oi_source_boundaries_only_use_samples_already_received(self):
        for i in range(100):
            self.store.save_oi(dict(symbol="1000PEPEUSDT", timestamp=DAY + i * MINUTE + 1000,
                source_time=DAY + i * MINUTE - 1000,
                open_interest_quantity=1000 * 1.001**i, mark_price=1))
        values = self.service.oi_enrichment("1000PEPEUSDT", "1m", DAY + 100 * MINUTE - 1)
        self.assertAlmostEqual(values["oi_ratio"], 1, places=8)
        self.assertAlmostEqual(values["oi_change_pct"], (1.001**2 - 1) * 100, places=8)

    async def test_repair_new_gap_arriving_during_repair_is_requeued(self):
        self.service._running = True
        symbol = "1000PEPEUSDT"
        self.service._schedule_gap(symbol, 9 * DAY)
        entered_again = asyncio.Event()
        never = asyncio.Event()
        calls = 0

        async def fake_fetch(symbol, timeframe, start, end):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.service._schedule_gap(symbol, 9 * DAY + MINUTE)
            if calls == len(MONITOR_TIMEFRAMES) + 1:
                entered_again.set()
                await never.wait()
            return []

        self.service._fetch_range = fake_fetch
        task = asyncio.create_task(self.service._recover_worker())
        try:
            await asyncio.wait_for(entered_again.wait(), timeout=2)
            self.assertIn(symbol, self.service._gap_pending)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_recovery_clock_safety_requeues_off_worker_then_recomputes_period_end(self):
        self.service._running = True
        boundary = self.now_ms // TIMEFRAME_MS["15m"] * TIMEFRAME_MS["15m"]
        self.now_ms = boundary + 1000
        self.service._clock_synced_at = self.now_ms
        self.service._clock_rtt_ms = 4000  # 3s conservative history safety.
        calls = []
        finished = asyncio.Event()
        sleeper_names = []
        real_sleep = asyncio.sleep

        async def fake_fetch(symbol, timeframe, start, end):
            calls.append((timeframe, end))
            if len(calls) == len(MONITOR_TIMEFRAMES):
                finished.set()
            return []

        async def advance_safety(_delay):
            sleeper_names.append(asyncio.current_task().get_name())
            self.now_ms += 4000
            await real_sleep(0)

        self.service._fetch_range = fake_fetch
        self.service._schedule_gap("1000PEPEUSDT", boundary - 10 * MINUTE)
        worker = asyncio.create_task(self.service._recover_worker(), name="recovery-test-worker")
        try:
            with patch("monitor.collector.asyncio.sleep", side_effect=advance_safety):
                await asyncio.wait_for(finished.wait(), timeout=2)
                await asyncio.wait_for(self.service._gap_queue.join(), timeout=2)
            self.assertTrue(sleeper_names)
            self.assertTrue(all(name.startswith("gap-delay-") for name in sleeper_names))
            self.assertEqual(calls, [(timeframe, boundary - 1) for timeframe in MONITOR_TIMEFRAMES])
            self.assertNotIn("1000PEPEUSDT", self.service._gap_pending)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_recovery_missing_confirmed_latest_bucket_schedules_slow_retry(self):
        self.service._running = True
        timeframe = MONITOR_TIMEFRAMES[0]
        interval = TIMEFRAME_MS[timeframe]
        boundary = self.now_ms // interval * interval
        self.now_ms = boundary + 1000
        count = LOOKBACK[timeframe]["minimum_bars"] + 3
        last_open = boundary - 2 * interval
        history = [bar(last_open - (count - 1 - i) * interval, timeframe=timeframe)
                   for i in range(count)]
        self.store.save_candles(history)
        # A durable raw close at the boundary proves the missing native bucket;
        # wall-clock staleness alone intentionally does not cause retries.
        self.service._last_minute["1000PEPEUSDT"] = boundary - MINUTE

        async def no_rows(symbol, timeframe, start, end):
            return []

        self.service._fetch_range = no_rows
        self.service._schedule_gap("1000PEPEUSDT", boundary - MINUTE)
        worker = asyncio.create_task(self.service._recover_worker())
        try:
            await asyncio.wait_for(self.service._gap_queue.join(), timeout=2)
            self.assertIn("1000PEPEUSDT", self.service._gap_pending)
            self.assertEqual(self.service._gap_start["1000PEPEUSDT"], boundary - interval)
            self.assertTrue(any(task.get_name() == "gap-delay-1000PEPEUSDT" for task in self.service._tasks))
            self.assertFalse(worker.done())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_delisted_gap_is_discarded_without_hot_requeue(self):
        self.service._running = True
        self.service._schedule_gap("DELISTEDUSDT", self.now_ms - MINUTE)
        task = asyncio.create_task(self.service._recover_worker())
        try:
            await asyncio.wait_for(self.service._gap_queue.join(), 2)
            self.assertNotIn("DELISTEDUSDT", self.service._gap_pending)
            self.assertNotIn("DELISTEDUSDT", self.service._gap_start)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_pause_resume_rebuilds_abandoned_warmup_state(self):
        never = asyncio.Event()

        async def fake_supervisor():
            await never.wait()

        self.service._run = fake_supervisor
        self.service._warm_pending.add(("1000PEPEUSDT", "1m"))
        self.service._pair_state[("1000PEPEUSDT", "1m")] = {"state": "warming"}
        await self.service.start()
        self.assertEqual(self.service._warm_pending, set())
        self.assertEqual(self.service._pair_state, {})
        await self.service.stop()
        self.now_ms += MINUTE
        await self.service.start()
        self.assertEqual(self.service._watermark, self.now_ms)
        self.assertEqual(self.service._state, "connecting")

    async def test_retry_preserves_persisted_scope_and_configuration(self):
        never = asyncio.Event()

        async def fake_supervisor():
            await never.wait()

        self.service._run = fake_supervisor
        self.service.config.volatility_ratio_min = 3
        self.store.reconcile_universe([meta()], self.now_ms)
        result = await self.service.retry_connection()
        self.assertEqual(result["state"], "connecting")
        self.assertEqual(self.service.config.volatility_ratio_min, 3)
        scope = self.service.universe_status()
        self.assertEqual(scope["summary"]["snapshot_count"], 1)
        self.assertEqual(scope["summary"]["verified_trading_count"], 0)
        self.assertFalse(scope["items"][0]["live"])

    async def test_refresh_includes_all_usdt_asset_classes_and_delivery_but_no_other_quotes(self):
        self.service._running = True
        never = asyncio.Event()
        symbols = [meta(), meta("BTCUSDC", quoteAsset="USDC"),
                   meta("NEWUSDT", status="PENDING_TRADING", onboardDate=self.now_ms + DAY),
                   meta("UNVERIFIEDUSDT", underlyingType=None),
                   meta("STOCKUSDT", contractType="TRADIFI_PERPETUAL", underlyingType="EQUITY"),
                   meta("BTCUSDT_260925", contractType="CURRENT_QUARTER", deliveryDate=self.now_ms + DAY),
                   meta("ETHUSDT_261225", contractType="NEXT_QUARTER", deliveryDate=self.now_ms + 90 * DAY),
                   meta("ETHBTC", quoteAsset="BTC", marginAsset="BTC"), meta("BTCU", quoteAsset="U"),
                   meta("SPCXUSD1", quoteAsset="USD1", contractType="TRADIFI_PERPETUAL", underlyingType="EQUITY"),
                   meta("CMUSDT", st=2), meta("SETTLEDUSDT", status="SETTLING")]
        # Production exchangeInfo gives the UM family at the response root.
        symbols = [{key: value for key, value in row.items() if key != "futuresType"} for row in symbols]

        async def fake_request(path, params=None, weight=1):
            return {"symbols": symbols, "futuresType": "U_MARGINED"} if path.endswith("exchangeInfo") else []

        async def fake_stream(symbols, index):
            await never.wait()

        self.service._request = fake_request
        self.service._kline_stream = fake_stream
        self.service._market_stream = fake_stream
        self.service._book_stream = lambda: fake_stream([], -1)
        self.service._market_control_stream = lambda: fake_stream([], -2)
        await self.service._refresh_universe()
        expected = {"1000PEPEUSDT", "UNVERIFIEDUSDT", "STOCKUSDT", "BTCUSDT_260925", "ETHUSDT_261225"}
        self.assertEqual(set(self.service._metadata), expected)
        self.assertEqual(self.service.status()["unknown_underlying_contracts"], 1)
        self.assertEqual(self.service.status()["live_ready_pairs"], 0)
        self.assertEqual(self.service.status()["quote_assets"], ["USDT"])
        self.assertEqual(self.service.status()["tradfi_contracts"], 1)
        self.assertEqual(self.service.status()["contract_type_counts"]["CURRENT_QUARTER"], 1)
        self.assertEqual(self.service.status()["exclusion_reasons"]["not_usdt_quote"], 4)
        self.assertEqual(self.service._warm_queue.qsize(), len(MONITOR_TIMEFRAMES) * (len(expected) - 1))
        scope = self.service.universe_status()
        self.assertEqual(scope["summary"]["snapshot_count"], 6)
        self.assertEqual(scope["quote_assets"], ["USDT"])
        delivery = next(row for row in scope["items"] if row["symbol"] == "BTCUSDT_260925")
        self.assertEqual(delivery["contract_type"], "CURRENT_QUARTER")
        self.assertEqual(delivery["delivery_date"], self.now_ms + DAY)
        self.assertEqual(delivery["margin_asset"], "USDT")
        stock = next(row for row in self.service.universe if row["symbol"] == "STOCKUSDT")
        self.assertTrue(stock["is_tradfi"])
        self.assertIsNone(stock["delivery_date"])
        job = self.service.download_status("BTCUSDT_260925", MONITOR_TIMEFRAMES[-1])["items"][0]
        self.assertEqual(job["contract_type"], "CURRENT_QUARTER")
        self.assertEqual(job["delivery_date"], self.now_ms + DAY)
        self.assertEqual(self.service.download_status("BTCUSDC")["items"], [])
        self.assertTrue(self.service._base_enrichment("STOCKUSDT")["is_tradfi"])
        upcoming = next(r for r in scope["items"] if r["symbol"] == "NEWUSDT")
        self.assertFalse(upcoming["subscribed"])
        self.assertEqual(upcoming["timeframes"][MONITOR_TIMEFRAMES[0]]["state"], "not_trading")

    async def test_scope_expansion_does_not_relabel_existing_tradfi_as_new_listing(self):
        self.service._running = True
        self.store.reconcile_universe([meta()], self.now_ms - 2 * DAY)
        self.store.reconcile_universe([meta()], self.now_ms - MINUTE)
        existing = meta("TSLAUSDT", contractType="TRADIFI_PERPETUAL", underlyingType="EQUITY",
                        onboardDate=self.now_ms - DAY)
        pending = meta("FUTUREUSDT", status="PENDING_TRADING", onboardDate=self.now_ms + DAY)
        never = asyncio.Event()

        async def request(path, params=None, weight=1):
            return {"futuresType": "U_MARGINED", "symbols": [meta(), existing, pending]} if path.endswith("exchangeInfo") else []

        async def stream(symbols, index):
            await never.wait()

        self.service._request = request
        self.service._kline_stream = stream
        self.service._market_stream = stream
        self.service._book_stream = lambda: stream([], -1)
        self.service._market_control_stream = lambda: stream([], -2)
        notified = []
        self.service.notification_sink = lambda kind, event: notified.append((kind, event["symbol"], event["event_type"]))
        await self.service._refresh_universe()
        events = self.store.list_listing_events()
        self.assertEqual([(event["symbol"], event["event_type"]) for event in events], [("FUTUREUSDT", "upcoming")])
        self.assertEqual(notified, [("listing", "FUTUREUSDT", "upcoming")])
        self.assertIn("TSLAUSDT", self.service._metadata)

    def test_tradfi_and_delivery_closed_klines_allow_missing_st_but_reject_explicit_cm(self):
        self.service._metadata = {"TSLAUSDT": meta("TSLAUSDT", contractType="TRADIFI_PERPETUAL", underlyingType="EQUITY"),
                                  "BTCUSDT_260925": meta("BTCUSDT_260925", contractType="CURRENT_QUARTER")}
        start = self.now_ms // MINUTE * MINUTE - MINUTE
        for symbol in self.service._metadata:
            event = {"e": "kline", "E": self.now_ms, "s": symbol,
                     "k": {"t": start, "T": start + MINUTE - 1, "i": "1m", "o": "100", "h": "102",
                           "l": "99", "c": "101", "v": "10", "q": "1000", "Q": "600", "n": 10, "x": True}}
            candle = self.service._consume_live_kline(event)
            self.assertIsNotNone(candle)
            self.assertEqual(candle.symbol, symbol)
            self.assertTrue(candle.closed)
            self.assertIsNone(self.service._consume_live_kline({**event, "st": 2}))

    def test_per_symbol_heartbeat_and_history_are_both_required_for_live_coverage(self):
        self.service._running = True
        self.service._state = "warming"
        self.service._last_refresh = self.now_ms
        self.service._connections["kline-0"] = {"state": "connected", "last_message": self.now_ms}
        self.service._symbol_shard["1000PEPEUSDT"] = "kline-0"
        for tf in MONITOR_TIMEFRAMES:
            self.service._pair_state[("1000PEPEUSDT", tf)] = {"state": "ready", "last_close": self.now_ms - 1}
        self.assertEqual(self.service.status()["ready_pairs"], len(MONITOR_TIMEFRAMES))
        self.assertEqual(self.service.status()["live_ready_pairs"], 0)
        self.service._symbol_heartbeat["1000PEPEUSDT"] = self.now_ms
        self.assertEqual(self.service.status()["live_ready_pairs"], len(MONITOR_TIMEFRAMES))
        self.service._connections["kline-0"]["state"] = "reconnecting"
        self.assertEqual(self.service.status()["live_ready_pairs"], 0)
        self.service._connections["kline-0"]["state"] = "connected"
        self.now_ms += 90_001
        self.assertEqual(self.service.status()["live_ready_pairs"], 0)

    def test_new_contract_insufficient_history_reason_is_explicit(self):
        self.service._metadata["1000PEPEUSDT"]["onboardDate"] = self.now_ms - MINUTE
        timeframe = MONITOR_TIMEFRAMES[0]
        self.service._set_pair_state("1000PEPEUSDT", timeframe, [])
        result = self.service.universe_status()["items"][0]["timeframes"][timeframe]
        self.assertEqual(result["state"], "insufficient_history")
        self.assertEqual(result["reason"], "new_insufficient")
        self.assertGreater(result["estimated_earliest_ready_at"], self.now_ms)

    async def test_analysis_prefilter_uses_causal_close_and_skips_non_signal_enrichment(self):
        timeframe = "5m"
        interval = TIMEFRAME_MS[timeframe]
        count = LOOKBACK[timeframe]["minimum_bars"] + 3
        close_open = self.now_ms // interval * interval - interval
        candles = [bar(close_open - (count - 1 - i) * interval, timeframe=timeframe)
                   for i in range(count)]
        self.store.save_candles([*candles, bar(close_open + interval, timeframe=timeframe)])
        close_time = candles[-1].close_time
        self.now_ms = close_time + 1000
        self.service._watermark = close_time - MINUTE
        self.service._running = True
        seen = []

        def no_signal(series, config, enrichment, tick):
            seen.append(series[-1].close_time)
            return {"status": "ready", "signal": None}

        self.service._analysis_queue.put_nowait(
            ("1000PEPEUSDT", timeframe, close_time, {"engine_config_at_close": {}}, None))
        with patch("monitor.collector.analyze", side_effect=no_signal), \
             patch.object(self.service, "oi_enrichment", side_effect=AssertionError("OI ran before anomaly")), \
             patch.object(self.service, "_spread", side_effect=AssertionError("spread ran before anomaly")):
            task = asyncio.create_task(self.service._analysis_worker())
            try:
                await asyncio.wait_for(self.service._analysis_queue.join(), timeout=2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(seen, [close_time])
        self.assertTrue(self.service._candidate_queue.empty())

    async def test_analysis_expiry_telemetry_separates_deadline_from_gap_conditions(self):
        timeframe = "5m"
        interval = TIMEFRAME_MS[timeframe]
        count = LOOKBACK[timeframe]["minimum_bars"] + 3
        close_open = self.now_ms // interval * interval - interval
        candles = [bar(close_open - (count - 1 - i) * interval, timeframe=timeframe)
                   for i in range(count)]
        self.store.save_candles(candles)
        close_time = candles[-1].close_time
        self.now_ms = close_time + 1000
        self.service._watermark = close_time - MINUTE
        self.service._running = True
        self.service._gap_pending.add("1000PEPEUSDT")
        self.service._symbol_gate["1000PEPEUSDT"] = self.now_ms
        enrichment = {"engine_config_at_close": {}}

        self.service._analysis_queue.put_nowait(
            ("1000PEPEUSDT", timeframe, close_time, enrichment, None))
        task = asyncio.create_task(self.service._analysis_worker())
        try:
            await asyncio.wait_for(self.service._analysis_queue.join(), timeout=2)
            self.assertEqual(self.service._analysis_expired, 1)
            self.assertEqual(self.service._analysis_expired_conditions, {
                "deadline": 0, "gap_pending": 1, "symbol_gate": 1,
                "reconnect_fence": 0, "watermark": 0,
            })

            self.service._gap_pending.clear()
            self.service._symbol_gate.clear()
            self.now_ms = close_time + 90_000
            self.service._analysis_queue.put_nowait(
                ("1000PEPEUSDT", timeframe, close_time, enrichment, None))
            await asyncio.wait_for(self.service._analysis_queue.join(), timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(self.service._analysis_expired, 2)
        self.assertEqual(self.service._analysis_expired_conditions["deadline"], 1)
        self.assertEqual(self.service._analysis_expired_conditions["gap_pending"], 1)
        self.assertEqual(self.service._analysis_expired_conditions["symbol_gate"], 1)
        status = self.service.status()
        self.assertEqual(status["analysis_expired_conditions"],
                         status["scheduler"]["analysis_expired_conditions"])
        self.assertEqual(status["analysis_expired_conditions"]["deadline"], 1)

    async def test_indicator_failure_keeps_base_alert_as_rejected(self):
        count = LOOKBACK["1m"]["minimum_bars"] + 3
        end = self.now_ms // MINUTE * MINUTE
        candles = [bar(end - (count - i) * MINUTE) for i in range(count)]
        candles[-2] = bar(candles[-2].open_time, high=110, close=109)
        candles[-1] = bar(candles[-1].open_time, open=109, high=120, low=108, close=119)
        self.store.save_candles(candles)
        close = candles[-1].close_time
        self.service._watermark = close - MINUTE
        self.service._running = True

        async def fake_spread(symbol):
            return 1

        def broken_indicator(signal, candles, enrichment):
            raise ValueError("unavailable")

        self.service._spread = fake_spread
        self.service.candidate_transform = broken_indicator
        self.service._candidate_queue.put_nowait(("1000PEPEUSDT", "1m", close, {}))
        task = asyncio.create_task(self.service._candidate_worker())
        try:
            await asyncio.wait_for(self.service._candidate_queue.join(), timeout=2)
            rows = self.store.list_alerts()
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["qualified"])
            self.assertEqual(rows[0]["rejection_reasons"], ["indicator_evaluation_failed"])
            self.assertEqual(self.store.list_alerts(qualified=True), [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_successful_indicator_rejection_removes_only_unconfigured_marker(self):
        count = LOOKBACK["1m"]["minimum_bars"] + 3
        end = self.now_ms // MINUTE * MINUTE
        candles = [bar(end - (count - i) * MINUTE) for i in range(count)]
        self.store.save_candles(candles)
        close = candles[-1].close_time
        self.service._watermark = close - MINUTE
        self.service._running = True

        async def fake_spread(symbol):
            return 1

        def anomaly(series, config, enrichment, tick):
            latest = series[-1]
            return {"signal": {"symbol": latest.symbol, "timeframe": latest.timeframe,
                               "close_time": latest.close_time, "pattern": "阳阳",
                               "direction": "up", "activity_score": 90}}

        def rejected_policy(signal, series, enrichment):
            self.assertEqual(signal["rejection_reasons"], [INDICATOR_RULES_NOT_CONFIGURED])
            # The real evaluator deep-copies its input before adding strategy
            # evidence, which used to copy the placeholder into every result.
            return {**signal, "qualified": False, "quality_schema_version": 2,
                    "quality_reasons": ["所选指标尚未满足全部共振"]}

        self.service._spread = fake_spread
        self.service.candidate_transform = rejected_policy
        notified = []
        self.service.notification_sink = lambda kind, event: notified.append(
            (kind, event["close_time"], event.get("daily_signal_count")))
        self.service._candidate_queue.put_nowait(("1000PEPEUSDT", "1m", close, {}))
        with patch("monitor.collector.analyze", side_effect=anomaly):
            task = asyncio.create_task(self.service._candidate_worker())
            try:
                await asyncio.wait_for(self.service._candidate_queue.join(), timeout=2)
                rows = self.store.list_alerts()
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]["qualified"])
                self.assertEqual(rows[0]["quality_schema_version"], 2)
                self.assertEqual(rows[0]["quality_reasons"], ["所选指标尚未满足全部共振"])
                self.assertEqual(rows[0]["rejection_reasons"], [])
                self.assertNotIn("qualification_error", rows[0])
                self.assertEqual(notified, [("signal", close, 1)])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_live_queue_freezes_policy_callable_and_base_config_at_acceptance(self):
        timeframe = MONITOR_TIMEFRAMES[0]
        interval = TIMEFRAME_MS[timeframe]
        count = LOOKBACK[timeframe]["minimum_bars"] + 4
        final_open = self.now_ms // interval * interval - interval
        history = [bar(final_open - (count - 1 - i) * interval, timeframe=timeframe)
                   for i in range(count - 1)]
        self.store.save_candles(history)
        self.service._set_pair_state("1000PEPEUSDT", timeframe, history)
        # This close is also a 15m boundary. Seed that enclosing bucket so the
        # policy-freezing test does not intentionally trip formal-gap repair.
        enclosing_start = final_open // TIMEFRAME_MS["15m"] * TIMEFRAME_MS["15m"]
        self.store.save_candles([bar(enclosing_start + i * MINUTE)
                                 for i in range((final_open - enclosing_start) // MINUTE
                                                + interval // MINUTE - 1)])
        final_minute = bar(final_open + interval - MINUTE)
        final_close = final_open + interval - 1
        self.service._watermark = final_close - MINUTE
        self.service._running = True
        called = []

        def old_policy(signal, candles, enrichment):
            called.append("old")
            return {**signal, "qualified": True, "quality_schema_version": 2, "policy_hash": "old-policy"}

        def new_policy(signal, candles, enrichment):
            called.append("new")
            return {**signal, "qualified": False, "quality_schema_version": 2, "policy_hash": "new-policy"}

        async def fake_spread(symbol):
            return 1

        def deterministic_analyze(candles, config, enrichment, tick):
            latest = candles[-1]
            return {"signal": {"symbol": latest.symbol, "timeframe": latest.timeframe,
                               "close_time": latest.close_time, "pattern": "阳阳",
                               "activity_score": 90}}

        self.service._spread = fake_spread
        self.service.candidate_transform = old_policy
        self.service._state = "ready"
        self.service._last_refresh = self.now_ms
        self.service._symbol_shard["1000PEPEUSDT"] = "kline-0"
        self.service._connections["kline-0"] = {"state": "connected"}
        self.service._symbol_heartbeat["1000PEPEUSDT"] = self.now_ms
        with patch("monitor.collector.analyze", side_effect=deterministic_analyze):
            await self.service._on_closed(final_minute)
            self.assertEqual(self.service._analysis_queue.qsize(), 1)
            self.assertEqual(self.service._candidate_queue.qsize(), 0)
            self.service.candidate_transform = new_policy
            self.service.config.volatility_ratio_min = 20
            scan = asyncio.create_task(self.service._analysis_worker())
            candidate = asyncio.create_task(self.service._candidate_worker())
            try:
                await asyncio.wait_for(self.service._analysis_queue.join(), timeout=2)
                await asyncio.wait_for(self.service._candidate_queue.join(), timeout=2)
                self.assertEqual(called, ["old"])
                rows = self.store.list_alerts()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["timeframe"], timeframe)
                self.assertEqual(rows[0]["policy_hash"], "old-policy")
                self.assertTrue(rows[0]["qualified"])
                self.assertFalse(rows[0]["delayed"])
                self.assertEqual(rows[0]["rejection_reasons"], [])
            finally:
                scan.cancel()
                candidate.cancel()
                await asyncio.gather(scan, candidate, return_exceptions=True)

    async def test_indicator_runtime_crossing_deadline_is_rejected_but_persisted(self):
        count = LOOKBACK["1m"]["minimum_bars"] + 3
        end = self.now_ms // MINUTE * MINUTE
        candles = [bar(end - (count - i) * MINUTE) for i in range(count)]
        candles[-2] = bar(candles[-2].open_time, high=110, close=109)
        candles[-1] = bar(candles[-1].open_time, open=109, high=120, low=108, close=119)
        self.store.save_candles(candles)
        close = candles[-1].close_time
        self.service._watermark = close - MINUTE
        self.service._running = True

        async def fake_spread(symbol):
            return 1

        async def slow_policy(signal, candles, enrichment):
            self.assertFalse(signal["delayed"])
            self.now_ms += 4 * MINUTE
            return {**signal, "qualified": True, "quality_schema_version": 2, "quality_reasons": []}

        self.service._spread = fake_spread
        self.service._candidate_queue.put_nowait(("1000PEPEUSDT", "1m", close, {}, slow_policy))
        task = asyncio.create_task(self.service._candidate_worker())
        try:
            await asyncio.wait_for(self.service._candidate_queue.join(), timeout=2)
            rows = self.store.list_alerts()
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["qualified"])
            self.assertTrue(rows[0]["delayed"])
            self.assertEqual(rows[0]["quality_schema_version"], 2)
            self.assertEqual(rows[0]["rejection_reasons"], ["delayed_candidate"])
            self.assertTrue(rows[0]["quality_reasons"])
            self.assertTrue(rows[0]["warnings"])
            self.assertEqual(self.store.list_alerts(qualified=True, schema_version=2), [])
            self.assertEqual(len(self.store.list_alerts(qualified=False, schema_version=2)), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_gap_discovered_during_indicator_revokes_qualification_but_keeps_history(self):
        count = LOOKBACK["1m"]["minimum_bars"] + 3
        end = self.now_ms // MINUTE * MINUTE
        candles = [bar(end - (count - i) * MINUTE) for i in range(count)]
        candles[-2] = bar(candles[-2].open_time, high=110, close=109)
        candles[-1] = bar(candles[-1].open_time, open=109, high=120, low=108, close=119)
        self.store.save_candles(candles)
        close = candles[-1].close_time
        self.service._watermark = close - MINUTE
        self.service._running = True

        async def fake_spread(symbol):
            return 1

        async def slow_policy(signal, candles, enrichment):
            self.assertFalse(signal["delayed"])
            self.service._gap_pending.add("1000PEPEUSDT")
            return {**signal, "qualified": True, "quality_schema_version": 2, "quality_reasons": []}

        self.service._spread = fake_spread
        self.service._candidate_queue.put_nowait(("1000PEPEUSDT", "1m", close, {}, slow_policy))
        task = asyncio.create_task(self.service._candidate_worker())
        try:
            await asyncio.wait_for(self.service._candidate_queue.join(), timeout=2)
            rows = self.store.list_alerts()
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["qualified"])
            self.assertFalse(rows[0]["delayed"])
            self.assertEqual(rows[0]["quality_schema_version"], 2)
            self.assertEqual(rows[0]["rejection_reasons"], ["market_not_ready_at_publish"])
            self.assertTrue(rows[0]["warnings"])
            self.assertEqual(self.store.list_alerts(qualified=True, schema_version=2), [])
            self.assertEqual(len(self.store.list_alerts(qualified=False, schema_version=2)), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_accepted_live_candidate_survives_slow_enrichment(self):
        count = LOOKBACK["1m"]["minimum_bars"] + 3
        end = self.now_ms // MINUTE * MINUTE
        candles = [bar(end - (count - i) * MINUTE) for i in range(count)]
        candles[-2] = bar(candles[-2].open_time, open=100, high=110, low=99, close=109)
        candles[-1] = bar(candles[-1].open_time, open=109, high=120, low=108, close=119)
        self.store.save_candles(candles)
        close = candles[-1].close_time
        self.service._watermark = close - MINUTE
        self.now_ms += 4 * MINUTE
        self.service._running = True

        async def fake_spread(symbol):
            return 1

        self.service._spread = fake_spread
        self.service._candidate_queue.put_nowait(("1000PEPEUSDT", "1m", close, {"quote_asset": "USDT"}))
        task = asyncio.create_task(self.service._candidate_worker())
        try:
            await asyncio.wait_for(self.service._candidate_queue.join(), timeout=2)
            alerts = self.store.list_alerts()
            self.assertEqual(len(alerts), 1)
            self.assertTrue(alerts[0]["delayed"])
            self.assertFalse(alerts[0]["qualified"])
            self.assertEqual(alerts[0]["quality_schema_version"], 2)
            self.assertIn(INDICATOR_RULES_NOT_CONFIGURED, alerts[0]["rejection_reasons"])
            self.assertIn("delayed_candidate", alerts[0]["rejection_reasons"])
            self.assertEqual(alerts[0]["source"], "live")
            self.assertEqual(alerts[0]["close_time"], close)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
