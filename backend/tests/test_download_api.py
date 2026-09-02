"""Download API integration tests: isolated SQLite, no production or network I/O."""
import csv
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import time as real_time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, unquote

from fastapi.testclient import TestClient

from monitor.collector import MonitorService
from monitor.engine import Candle, MONITOR_TIMEFRAMES, TIMEFRAME_MS


HEADERS = {"X-Pulse-Client": "dashboard", "Origin": "http://127.0.0.1:5173"}
CHINESE_CONTRACTS = ("币安人生USDT", "我踏马来了USDT", "牛来USDT", "龙虾USDT")


def candle(index=0, symbol="BTCUSDT", timeframe="5m", **changes):
    interval = TIMEFRAME_MS[timeframe]
    values = {"symbol": symbol, "timeframe": timeframe,
              "open_time": index * interval, "close_time": (index + 1) * interval - 1,
              "open": 100, "high": 103, "low": 99, "close": 102,
              "quote_volume": 1020, "taker_buy_quote": 600,
              "volume": 10, "trades": 12, "closed": True}
    values.update(changes)
    return Candle(**values)


def snapshot(items=None, **changes):
    return {"items": items or [], "summary": {"downloaded_bars": 0, "completed_pairs": 0},
            "events": [], "cursor": 0, "session_id": "test-session", **changes}


class DownloadApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.environment = patch.dict(os.environ, {
            "PULSE_DATA_DIR": self.folder.name,
            "PULSE_DISABLE_COLLECTOR": "1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.start_patch = patch.object(MonitorService, "start", side_effect=AssertionError("network collector must not start"))
        self.start_mock = self.start_patch.start()
        self.addCleanup(self.start_patch.stop)
        self.request_patch = patch.object(MonitorService, "_request", side_effect=AssertionError("network requests forbidden"))
        self.request_mock = self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.module_name = "monitor._isolated_download_api_test"
        module_path = Path(__file__).resolve().parents[1] / "monitor" / "api.py"
        spec = importlib.util.spec_from_file_location(self.module_name, module_path)
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.api
        self.addCleanup(sys.modules.pop, self.module_name, None)
        spec.loader.exec_module(self.api)
        self.client = TestClient(self.api.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        # API merge/projection tests are independent of live scheduler internals.
        self.status_patch = patch.object(self.api.service, "download_status", return_value=snapshot(), create=True)
        self.download_status = self.status_patch.start()
        self.addCleanup(self.status_patch.stop)

    def tearDown(self):
        self.start_mock.assert_not_called()
        self.request_mock.assert_not_called()

    def test_saved_inventory_survives_unknown_collector_without_claiming_live_scope(self):
        self.api.store.save_candles([
            candle(0), candle(1), candle(1, close=101.5),
            candle(2, closed=False), candle(0, "ETHUSDC", "5m"),
        ])
        self.assertEqual(self.api.service.universe, [])
        response = self.client.get("/api/downloads")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        rows = {(r["symbol"], r["timeframe"]): r for r in data["items"]}
        self.assertEqual(set(rows), {("BTCUSDT", "5m")})
        btc = rows[("BTCUSDT", "5m")]
        self.assertEqual(btc["state"], "cached")
        self.assertFalse(btc["scope_verified"])
        self.assertIsNone(btc["progress_pct"])
        self.assertEqual(btc["stored_bars"], 2)
        self.assertEqual(btc["stored_last_price"], 101.5)
        self.assertEqual(data["summary"]["stored_candles"], 2)
        self.assertEqual(data["summary"]["stored_pairs"], 1)
        self.assertEqual(data["summary"]["stored_symbols"], 1)
        self.assertEqual(data["summary"]["retained_candles"], 3)
        self.assertEqual(data["summary"]["outside_scope_candles"], 1)
        self.assertEqual(data['scope_items_total'],0)
        self.assertFalse(data["telegram_enabled"])
        self.assertFalse(data["automatic_trading"])
        self.assertIsNotNone(data["inventory_updated_at"])

    def test_unverified_cache_prefers_saved_quote_metadata_over_symbol_suffix(self):
        self.api.store.save_candles([candle(0,'MISLEADINGUSDT'),candle(0,'OFFICIALPAIR'),
                                     candle(0,'BTCUSDT_260925'),candle(0,'BTCUSDT_260925_EXTRA')])
        self.api.store.reconcile_universe([
            {'symbol':'MISLEADINGUSDT','quoteAsset':'USDC','status':'TRADING','onboardDate':1},
            {'symbol':'OFFICIALPAIR','quoteAsset':'USDT','status':'TRADING','onboardDate':1}],1000)
        data = self.client.get('/api/downloads').json()
        self.assertEqual({row['symbol'] for row in data['items']},{'OFFICIALPAIR','BTCUSDT_260925'})
        self.assertTrue(all(row['state']=='cached' and not row['scope_verified'] for row in data['items']))
        self.assertEqual(data['scope_items_total'],0)
        self.assertEqual(data['summary']['retained_candles'],4)
        self.assertEqual(data['summary']['stored_candles'],2)
        self.assertEqual(self.api.store.counts()['candles'],4)

    def test_identity_filters_cached_inventory_and_normalizes_symbol(self):
        self.api.store.save_candles([candle(), candle(0, timeframe="5m"), candle(0, symbol="ETHUSDT")])
        data = self.client.get("/api/downloads", params={"symbol": "btcusdt", "timeframe": "5m"}).json()
        self.assertEqual([(r["symbol"], r["timeframe"]) for r in data["items"]], [("BTCUSDT", "5m")])
        self.download_status.assert_called_once_with(symbol="BTCUSDT", timeframe="5m", limit=10000, after=None)

    def test_verified_usdt_scope_never_reimports_excluded_inventory_or_progress(self):
        self.api.store.save_candles([candle(), candle(0,"ETHUSDC"), candle(1,"ETHUSDC"),
                                     candle(0,"BTCUSD1"), candle(0,"ETHBTC")])
        self.api.service._metadata = {"BTCUSDT": {"symbol":"BTCUSDT","quoteAsset":"USDT"}}
        current = {"symbol":"BTCUSDT","timeframe":"5m","state":"downloading","pages":4}
        self.download_status.return_value = snapshot([current],items_total=1,
            summary={"scope_verified":True,"total_pairs":2,"completed_pairs":0,"downloaded_bars":4},
            events=[{"symbol":"ETHUSDC","type":"page_saved","count":2},
                    {"symbol":"BTCUSDT","type":"page_saved","count":1}])
        data = self.client.get('/api/downloads').json()
        self.assertEqual([(row['symbol'],row['state']) for row in data['items']],[('BTCUSDT','downloading')])
        self.assertEqual(data['items_total'],1)
        self.assertEqual(data['summary']['total_pairs'],2)
        self.assertEqual(data['summary']['stored_candles'],1)
        self.assertEqual(data['summary']['stored_symbols'],1)
        self.assertEqual(data['summary']['retained_candles'],5)
        self.assertEqual(data['summary']['outside_scope_candles'],4)
        self.assertEqual(data['summary']['outside_scope_symbols'],3)
        self.assertEqual(data['summary']['inventory_scope'],'current_verified_scope')
        self.assertEqual([event['symbol'] for event in data['events']],['BTCUSDT'])
        # Explicit archive inspection still works; nothing was deleted.
        response = self.client.get('/api/downloads/ETHUSDC/5m/export')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['X-Candle-Count'],'2')
        self.assertEqual(self.api.store.counts()['candles'],5)
        self.download_status.return_value = snapshot([],items_total=0,summary={'scope_verified':True,'total_pairs':2})
        self.assertEqual(self.client.get('/api/downloads?symbol=ETHUSDC').json()['items'],[])

    def test_more_than_one_thousand_current_pairs_are_returned_without_cached_relabelling(self):
        # Synthetic future scope exercises pagination safety independently of
        # today's number of official contracts. No collector/network is started.
        symbols = [f'TEST{i:04d}USDT' for i in range(752)]
        self.api.service._metadata = {symbol:{'symbol':symbol,'quoteAsset':'USDT','status':'TRADING',
            'contractType':'PERPETUAL','underlyingType':'COIN','futuresType':'U_MARGINED'} for symbol in symbols}
        self.api.service._last_refresh = self.api.service.now()
        pairs = [(symbol,timeframe) for symbol in symbols for timeframe in MONITOR_TIMEFRAMES]
        self.api.service._download_jobs = {pair:{'symbol':pair[0],'timeframe':pair[1],'pages':index+1}
                                           for index,pair in enumerate(pairs)}
        self.api.store.save_candles([candle(0,symbols[-1],'15m')])
        self.download_status.side_effect = lambda **kwargs: MonitorService.download_status(self.api.service,**kwargs)
        data = self.client.get('/api/downloads').json()
        self.assertEqual(data['items_total'],1504)
        self.assertEqual(data['scope_items_total'],1504)
        self.assertFalse(data['truncated'])
        self.assertEqual({(row['symbol'],row['timeframe']) for row in data['items']},set(pairs))
        tail = next(row for row in data['items'] if (row['symbol'],row['timeframe']) == pairs[-1])
        self.assertEqual(tail['pages'],1504)
        self.assertNotEqual(tail['state'],'cached')
        self.assertEqual(tail['stored_bars'],1)

    def test_delivery_symbols_support_details_filters_and_safe_csv_names(self):
        symbols = ('BTCUSDT_260925','ETHUSDT_261225','中文USDT_260925')
        self.api.store.save_candles([candle(index,symbol) for symbol in symbols for index in (0,1)])
        for symbol in symbols:
            with self.subTest(symbol=symbol):
                path = '/api/downloads/'+quote(symbol.lower(),safe='')+'/5m'
                detail = self.client.get(path).json()
                self.assertEqual(detail['symbol'],symbol)
                self.assertEqual([row['symbol'] for row in detail['candles']],[symbol,symbol])
                rows = self.client.get('/api/downloads',params={'symbol':symbol.lower(),'timeframe':'5m'}).json()['items']
                self.assertEqual([row['symbol'] for row in rows],[symbol])
                response = self.client.get(path+'/export')
                records = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
                self.assertEqual([row['symbol'] for row in records],[symbol,symbol])
                self.assertIn(quote(symbol,safe=''),response.headers['content-disposition'])
        for symbol in ('=BTCUSDT_260925','BTCUSDT_260925;DROP TABLE candles','BTCUSDT_260925%2fsecret','__'):
            with self.subTest(invalid=symbol):
                self.assertIn(self.client.get('/api/downloads/'+quote(symbol,safe='')+'/5m/export').status_code,(400,404))
        self.assertEqual(self.api.store.counts()['candles'],6)

    def test_real_chinese_contract_names_support_saved_candle_details(self):
        # Symbols are real exchange identifiers; candles remain synthetic test data.
        self.api.store.save_candles([
            candle(index, symbol=symbol) for symbol in CHINESE_CONTRACTS for index in (0, 1)
        ])
        for symbol in CHINESE_CONTRACTS:
            with self.subTest(symbol=symbol):
                self.download_status.reset_mock()
                response = self.client.get("/api/downloads/" + quote(symbol, safe="") + "/5m?limit=2")
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["symbol"], symbol)
                self.assertEqual(data["inventory"]["bars"], 2)
                self.assertEqual([row["open_time"] for row in data["candles"]], [300000, 0])
                self.assertEqual({row["symbol"] for row in data["candles"]}, {symbol})
                self.assertTrue(all(row["closed"] is True for row in data["candles"]))
                self.download_status.assert_called_once_with(symbol=symbol, timeframe="5m", limit=1)

    def test_real_chinese_contract_names_filter_inventory_without_mixing_coins(self):
        self.api.store.save_candles([
            candle(0, symbol=symbol, timeframe=tf)
            for symbol in (*CHINESE_CONTRACTS, "BTCUSDT") for tf in ("1m", "5m")
        ])
        for symbol in CHINESE_CONTRACTS:
            with self.subTest(symbol=symbol):
                self.download_status.reset_mock()
                response = self.client.get("/api/downloads", params={"symbol": symbol[:-4] + "usdt", "timeframe": "5m"})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual([(row["symbol"], row["timeframe"]) for row in data["items"]], [(symbol, "5m")])
                self.assertEqual(data["items_total"], 1)
                self.assertEqual(data["items"][0]["stored_bars"], 1)
                self.download_status.assert_called_once_with(symbol=symbol, timeframe="5m", limit=10000, after=None)

    def test_chinese_csv_exports_keep_utf8_symbols_and_rfc5987_download_names(self):
        self.api.store.save_candles([
            candle(index, symbol=symbol) for symbol in CHINESE_CONTRACTS for index in (0, 1)
        ])
        for symbol in CHINESE_CONTRACTS:
            with self.subTest(symbol=symbol):
                response = self.client.get("/api/downloads/" + quote(symbol, safe="") + "/5m/export?limit=2")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["x-candle-count"], "2")
                self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
                records = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
                self.assertEqual([row["symbol"] for row in records], [symbol, symbol])
                self.assertEqual([int(row["open_time"]) for row in records], [0, 300000])
                disposition = response.headers["content-disposition"]
                self.assertTrue(disposition.isascii())
                parts = [part.strip() for part in disposition.split(";")]
                fallback = next(part.split("=", 1)[1].strip('"') for part in parts if part.startswith("filename="))
                self.assertTrue(fallback.isascii())
                self.assertTrue(fallback.endswith(".csv"))
                encoded = next(part.split("=", 1)[1] for part in parts if part.startswith("filename*="))
                self.assertTrue(encoded.startswith("UTF-8''"))
                self.assertEqual(unquote(encoded[len("UTF-8''"):]), f"{symbol}-5m-saved-candles.csv")

    def test_live_open_quote_is_separate_from_saved_closed_detail_and_export(self):
        self.api.store.save_candles([candle(0), candle(1, close=102.5), candle(2, close=999, high=1000, closed=False)])
        live_quote = {"price": 999, "open_time": 600000, "closed": False, "event_time": 760000}
        self.download_status.return_value = snapshot([{
            "symbol": "BTCUSDT", "timeframe": "5m", "state": "downloading",
            "bars": 2, "downloaded_bars": 2, "live_quote": live_quote,
            "latest_candle": candle(1, close=102.5).to_dict(),
        }])
        response = self.client.get("/api/downloads/btcusdt/5m?limit=1")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["live_quote"], live_quote)
        self.assertEqual(data["inventory"]["bars"], 2)
        self.assertEqual(data["inventory"]["last_price"], 102.5)
        self.assertEqual([row["open_time"] for row in data["candles"]], [300000])
        self.assertTrue(all(row["closed"] is True for row in data["candles"]))
        self.assertNotIn(999, [row["close"] for row in data["candles"]])
        exported = self.client.get("/api/downloads/BTCUSDT/5m/export")
        records = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        self.assertEqual([float(row["close"]) for row in records], [102, 102.5])
        self.assertTrue(all(row["closed"] == "True" for row in records))

    def test_slim_inventory_keeps_one_quote_per_symbol_and_never_mutates_collector_snapshot(self):
        live_quote = {"price": 104, "closed": False}
        raw = [
            {"symbol": "BTCUSDT", "timeframe": tf, "live_quote": live_quote,
             "latest_candle": candle(0, timeframe=tf).to_dict(), "pages": 3}
            for tf in MONITOR_TIMEFRAMES
        ]
        self.download_status.return_value = snapshot(raw)
        rows = self.client.get("/api/downloads").json()["items"]
        self.assertNotIn("latest_candle", rows[0])
        self.assertNotIn("latest_candle", rows[1])
        self.assertEqual(rows[0]["live_quote"], live_quote)
        self.assertNotIn("live_quote", rows[1])
        self.assertTrue(all("latest_candle" in row and "live_quote" in row for row in raw))

    def test_detail_descending_page_and_export_ascending_page_are_exact_and_bounded(self):
        self.api.store.save_candles([candle(i) for i in range(5)])
        detail = self.client.get("/api/downloads/BTCUSDT/5m?limit=2").json()
        self.assertEqual([row["open_time"] for row in detail["candles"]], [1200000, 900000])
        response = self.client.get("/api/downloads/BTCUSDT/5m/export?limit=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-candle-count"], "2")
        self.assertIn('filename="BTCUSDT-5m-saved-candles.csv"', response.headers["content-disposition"])
        self.assertTrue(response.headers["content-type"].startswith("text/csv"))
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["symbol", "timeframe", "open_time", "close_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote", "trades", "closed"])
        self.assertEqual([int(row[2]) for row in rows[1:]], [900000, 1200000])
        self.assertEqual(rows[1][0:4], ["BTCUSDT", "5m", "900000", "1199999"])
        self.assertEqual(self.api.store.counts()["candles"], 5)

    def test_bad_identity_cursors_and_unbounded_limits_never_reach_collector(self):
        routes = [
            ("/api/downloads?timeframe=3m", 400),
            ("/api/downloads?symbol=BTC-USDT", 400),
            ("/api/downloads?symbol=" + "A" * 41, 400),
            ("/api/downloads?after=-1", 422),
            ("/api/downloads?after=1.5", 422),
            ("/api/downloads?after=not-a-cursor", 422),
            ("/api/downloads/BTCUSDT/1m?limit=0", 422),
            ("/api/downloads/BTCUSDT/1m?limit=101", 422),
            ("/api/downloads/BTCUSDT/1m/export?limit=60001", 422),
            ("/api/downloads/BTCUSDT/1m/export?limit=-5", 422),
            ("/api/downloads/BTCUSDT/3m", 400),
        ]
        for route, expected in routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, expected)
        self.download_status.assert_not_called()

    def test_symbol_formula_and_sql_injection_are_rejected_before_csv_or_sql(self):
        for symbol in ("=1+1", "+SUM(1,2)", "@SUM(1,2)", "-BTC", 'BTCUSDT";DROP TABLE candles;--', "BTCUSDT\r\nX-Injected:yes"):
            with self.subTest(symbol=symbol):
                response = self.client.get("/api/downloads/" + quote(symbol, safe="") + "/1m/export")
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("content-disposition", response.headers)
        self.assertEqual(self.api.store.counts()["candles"], 0)

    def test_cursor_is_forwarded_without_hiding_current_job_snapshot(self):
        self.download_status.return_value = snapshot(
            [{"symbol": "BTCUSDT", "timeframe": "5m", "pages": 12}],
            cursor=105, events=[{"seq": 105, "type": "page_saved", "count": 499}])
        data = self.client.get("/api/downloads", params={"after": 104}).json()
        self.download_status.assert_called_once_with(symbol=None, timeframe=None, limit=10000, after=104)
        self.assertEqual(data["cursor"], 105)
        self.assertEqual(data["events"][0]["seq"], 105)
        self.assertEqual(data["items"][0]["pages"], 12)

    def test_real_collector_event_cursor_is_bounded_and_reports_truncation(self):
        for i in range(650):
            self.api.service._download_event("page_saved", "BTCUSDT", "5m", count=i)
        delegate = lambda **kwargs: MonitorService.download_status(self.api.service, **kwargs)
        with patch.object(self.api.service, "download_status", side_effect=delegate):
            first = self.client.get("/api/downloads?after=0").json()
            self.assertEqual(first["cursor"], 650)
            self.assertEqual(len(first["events"]), 100)
            self.assertEqual(first["oldest_available_seq"], 151)
            self.assertTrue(first["events_truncated"])
            self.assertEqual([e["seq"] for e in first["events"]], list(range(551, 651)))
            last = self.client.get("/api/downloads?after=649").json()
            self.assertEqual([e["seq"] for e in last["events"]], [650])
            self.assertFalse(last["events_truncated"])
            self.assertEqual(first["session_id"], last["session_id"])
            self.api.service._download_event("resume_requested")
            self.api.service._download_event("page_saved", "BTCUSDT", "5m")
            self.api.service._download_event("page_saved", "ETHUSDT", "15m")
            filtered = self.client.get("/api/downloads?symbol=ETHUSDT&timeframe=15m&after=650").json()
            self.assertEqual([e["seq"] for e in filtered["events"]], [651, 653])
            self.assertEqual(filtered["cursor"], 653)
            future = self.client.get("/api/downloads?after=999999999999").json()
            self.assertEqual(future["events"], [])
            self.assertEqual(future["cursor"], 653)
            self.assertEqual(future["session_id"], first["session_id"])

    def test_absent_saved_data_stays_empty_without_demo_or_live_quote_substitution(self):
        self.download_status.return_value = snapshot()
        data = self.client.get("/api/downloads/UNKNOWNUSDT/1h").json()
        self.assertEqual(data["candles"], [])
        self.assertEqual(data["inventory"]["bars"], 0)
        self.assertIsNone(data["inventory"]["latest_candle"])
        self.assertIsNone(data["job"])
        self.assertIsNone(data["live_quote"])

    def test_inventory_cache_does_not_cache_page_counters_and_refreshes_saved_totals(self):
        import monitor.download_api as routes
        clock = SimpleNamespace(monotonic=lambda: 1000.0, time=lambda: 100000.0)
        self.api.store.save_candles([candle(0)])
        rows = [{"symbol": "BTCUSDT", "timeframe": "5m", "pages": 1, "downloaded_bars": 1}]
        self.download_status.return_value = snapshot(rows, cursor=1)
        original_inventory = self.api.store.candle_inventory
        with patch.object(routes, "time", clock), patch.object(self.api.store, "candle_inventory", wraps=original_inventory) as inventory:
            first = self.client.get("/api/downloads").json()
            self.api.store.save_candles([candle(1)])
            self.download_status.return_value = snapshot([{**rows[0], "pages": 2, "downloaded_bars": 2}], cursor=2)
            second = self.client.get("/api/downloads?after=1").json()
            self.assertEqual(inventory.call_count, 1)
            self.assertEqual(second["items"][0]["stored_bars"], 1)
            self.assertEqual(second["items"][0]["pages"], 2)
            self.assertEqual(second["items"][0]["downloaded_bars"], 2)
            self.assertEqual(second["cursor"], 2)
            self.assertEqual(first["inventory_updated_at"], second["inventory_updated_at"])
            clock.monotonic = lambda: 1009.0
            clock.time = lambda: 100009.0
            third = self.client.get("/api/downloads?after=2").json()
            self.assertEqual(inventory.call_count, 2)
            # Stale-while-revalidate never makes this poll wait for the scan.
            # Depending on the tiny test scan's scheduling, this response may
            # contain either exact snapshot; the cache must soon publish two.
            deadline = real_time.monotonic() + 2
            while self.api.download_inventory_cache._refreshing and real_time.monotonic() < deadline:
                real_time.sleep(0.01)
            fourth = self.client.get("/api/downloads?after=2").json()
            self.assertEqual(fourth["items"][0]["stored_bars"], 2)
            self.assertNotEqual(second["inventory_updated_at"], fourth["inventory_updated_at"])
            self.assertTrue(third["inventory_ready"])

    def test_slow_stale_inventory_refresh_is_singleflight_and_does_not_block_status(self):
        import monitor.download_api as routes
        clock = SimpleNamespace(monotonic=lambda: 1000.0, time=lambda: 100000.0)
        self.api.store.save_candles([candle(0)])
        original_inventory = self.api.store.candle_inventory
        with patch.object(routes, "time", clock):
            seeded = self.client.get("/api/downloads").json()
            self.assertEqual(seeded["summary"]["stored_candles"], 1)
            self.api.store.save_candles([candle(1)])
            clock.monotonic = lambda: 1009.0
            clock.time = lambda: 100009.0
            started = threading.Event()
            release = threading.Event()

            def slow_inventory():
                started.set()
                release.wait(2)
                return original_inventory()

            with patch.object(self.api.store, "candle_inventory", side_effect=slow_inventory) as inventory:
                began = real_time.perf_counter()
                stale = self.client.get("/api/downloads").json()
                self.assertLess(real_time.perf_counter() - began, .5)
                self.assertTrue(started.wait(1))
                self.assertEqual(stale["summary"]["stored_candles"], 1)

                began = real_time.perf_counter()
                repeated = [self.client.get("/api/downloads").json() for _ in range(3)]
                status = self.client.get("/api/status")
                self.assertLess(real_time.perf_counter() - began, .5)
                self.assertEqual(status.status_code, 200)
                self.assertTrue(all(row["summary"]["stored_candles"] == 1 for row in repeated))
                self.assertEqual(inventory.call_count, 1)
                release.set()

                deadline = real_time.monotonic() + 2
                while self.api.download_inventory_cache._refreshing and real_time.monotonic() < deadline:
                    real_time.sleep(.01)
                current = self.client.get("/api/downloads").json()
                self.assertEqual(current["summary"]["stored_candles"], 2)
                self.assertEqual(inventory.call_count, 1)

    def test_resume_csrf_and_repeated_requests_delegate_without_erasing_saved_data(self):
        self.api.store.save_candles([candle(0), candle(1)])
        resume = AsyncMock(return_value={"state": "warming", "resume_action": "continued"})
        with patch.object(self.api.service, "retry_connection", resume):
            for headers in ({}, {"Origin": "http://127.0.0.1:5173"},
                            {"X-Pulse-Client": "dashboard", "Origin": "https://attacker.example"},
                            {"X-Pulse-Client": "other", "Origin": "http://127.0.0.1:5173"}):
                with self.subTest(headers=headers):
                    self.assertEqual(self.client.post("/api/downloads/resume", headers=headers).status_code, 403)
            resume.assert_not_awaited()
            for _ in range(2):
                response = self.client.post("/api/downloads/resume", headers=HEADERS)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["state"], "warming")
                self.assertFalse(response.json()["telegram_enabled"])
            self.assertEqual(resume.await_count, 2)
        self.assertEqual(self.api.store.counts()["candles"], 2)


if __name__ == "__main__":
    unittest.main()
