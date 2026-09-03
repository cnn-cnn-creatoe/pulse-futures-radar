"""Exercise the local API without making any collector/network requests."""
import importlib.util
import json
import os
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch
from urllib.parse import quote

from fastapi.testclient import TestClient

from monitor.collector import MonitorService
from monitor.engine import Candle, MONITOR_TIMEFRAMES


HEADERS = {'X-Pulse-Client': 'dashboard', 'Origin': 'http://127.0.0.1:5173'}


class ApiTests(unittest.TestCase):
    def setUp(self):
        # Environment isolation happens BEFORE executing api.py: its module
        # initialization must never open the user's production database.
        self.folder = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            'PULSE_DATA_DIR': self.folder.name,
            'PULSE_DISABLE_COLLECTOR': '1',
        })
        self.environment.start()
        self.start_patch = patch.object(MonitorService, 'start', side_effect=AssertionError('collector network must stay disabled'))
        self.start_mock = self.start_patch.start()
        self.module_name = 'monitor._isolated_api_test'
        path = Path(__file__).resolve().parents[1] / 'monitor' / 'api.py'
        spec = importlib.util.spec_from_file_location(self.module_name, path)
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.api
        spec.loader.exec_module(self.api)
        self.client = TestClient(self.api.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.start_mock.assert_not_called()
        self.start_patch.stop()
        self.environment.stop()
        sys.modules.pop(self.module_name, None)
        self.folder.cleanup()

    def test_demo_and_live_are_isolated_in_both_directions(self):
        self.assertEqual(self.client.get('/api/signals').json()['items'], [])
        demo = self.client.get('/api/signals?mode=demo').json()
        self.assertEqual(demo['source'], 'demo')
        self.assertEqual(len(demo['items']), 8)
        self.assertEqual({row['pattern'] for row in demo['items']}, {'阳阳', '阴阴', '阳阴', '阴阳'})
        self.assertTrue(all(row['source'] == 'demo' and row['synthetic'] for row in demo['items']))
        self.assertEqual(self.api.store.counts(), {'candles': 0, 'alerts': 0, 'oi_snapshots': 0})
        production = {'symbol': 'REALUSDT', 'timeframe': '1m', 'close_time': 59999,
                      'source': 'live', 'pattern': '阳阳', 'activity_score': 70}
        self.api.store.save_alert(production)
        live = self.client.get('/api/signals?mode=live').json()
        self.assertEqual(live['source'], 'live')
        self.assertEqual([row['symbol'] for row in live['items']], ['REALUSDT'])
        self.assertEqual(live['items'][0]['daily_signal_count'], 1)
        self.assertEqual(live['items'][0]['daily_timeframe_signal_count'], 1)
        self.assertEqual(live['items'][0]['daily_direction_signal_count'], 1)
        self.assertNotIn('REALUSDT', {row['symbol'] for row in self.client.get('/api/signals?mode=demo').json()['items']})

    def test_telegram_and_trading_remain_disabled(self):
        for path in ('/api/status', '/api/settings', '/api/signals?mode=live', '/api/signals?mode=demo'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.json()['telegram_enabled'], False)
        status = self.client.get('/api/status').json()
        self.assertIs(status['automatic_trading'], False)
        self.assertEqual(status['universe_count'], 0)
        self.assertEqual(status['state'], 'stopped')
        self.assertFalse((Path(self.folder.name) / 'settings.json').exists())

    def test_invalid_settings_never_change_or_persist_configuration(self):
        original = asdict(self.api.service.config)
        invalid = [
            {'volatility_ratio_min': 1.49}, {'volatility_ratio_min': 20.01},
            {'trim_percent': -0.1}, {'trim_percent': 10.1},
            {'baseline_method': 'median'}, {'pattern_mode': 'long_only'},
            {'telegram_enabled': True}, {'api_key': 'must-never-be-accepted'},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.client.put('/api/settings', json={**original, **changes}, headers=HEADERS)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(asdict(self.api.service.config), original)
                self.assertFalse(self.api.CONFIG_FILE.exists())

    def test_write_requires_client_marker_and_rejects_untrusted_origins(self):
        original = asdict(self.api.service.config)
        changed = {**original, 'volatility_ratio_min': 2}
        rejected_headers = [
            {}, {'Origin': 'http://127.0.0.1:5173'},
            {'X-Pulse-Client': 'other', 'Origin': 'http://127.0.0.1:5173'},
            {'X-Pulse-Client': 'dashboard', 'Origin': 'https://attacker.example'},
            {'X-Pulse-Client': 'dashboard', 'Origin': 'null'},
            {'X-Pulse-Client': 'dashboard', 'Origin': 'http://127.0.0.1:5173.attacker.example'},
        ]
        for headers in rejected_headers:
            with self.subTest(headers=headers):
                response = self.client.put('/api/settings', json=changed, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(asdict(self.api.service.config), original)
        self.assertFalse(self.api.CONFIG_FILE.exists())

    def test_cors_preflight_only_allows_local_dashboard(self):
        accepted = self.client.options('/api/settings', headers={
            'Origin': 'http://localhost:5173', 'Access-Control-Request-Method': 'PUT',
            'Access-Control-Request-Headers': 'content-type,x-pulse-client',
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.headers['access-control-allow-origin'], 'http://localhost:5173')
        rejected = self.client.options('/api/settings', headers={
            'Origin': 'https://attacker.example', 'Access-Control-Request-Method': 'PUT',
            'Access-Control-Request-Headers': 'content-type,x-pulse-client',
        })
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn('access-control-allow-origin', rejected.headers)

    def test_update_status_is_read_only_but_apply_requires_local_dashboard(self):
        status = {
            'current_version': 'v1.0.0', 'latest_version': 'v1.1.0',
            'update_available': True, 'can_update': True,
            'message': '发现新版本 v1.1.0，可以立即下载并更新。',
            'checked_at': 1,
        }
        with patch.object(self.api.update_service, 'status', return_value=status) as check:
            response = self.client.get('/api/update/status')
            forced = self.client.get('/api/update/status?force=true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), status)
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(forced.json(), status)
        self.assertEqual(check.call_args_list, [call(force=False), call(force=True)])

        with patch.object(self.api.update_service, 'schedule_update',
                          return_value={'scheduled': True, 'message': '更新已安排'}) as schedule:
            for headers in ({}, {'Origin': 'http://127.0.0.1:5173'},
                            {'X-Pulse-Client': 'dashboard',
                             'Origin': 'https://attacker.example'}):
                with self.subTest(headers=headers):
                    self.assertEqual(
                        self.client.post('/api/update/apply', headers=headers).status_code,
                        403,
                    )
            accepted = self.client.post('/api/update/apply', headers=HEADERS)
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()['scheduled'])
        schedule.assert_called_once_with()

        with patch.object(self.api.update_service, 'schedule_update',
                          side_effect=self.api.UpdateError('当前没有可安装的新版本。')):
            unavailable = self.client.post('/api/update/apply', headers=HEADERS)
        self.assertEqual(unavailable.status_code, 409)
        self.assertEqual(unavailable.json()['detail'], '当前没有可安装的新版本。')

    def test_settings_are_saved_atomically_and_reload_identically(self):
        changed = {'volatility_ratio_min': 2, 'baseline_method': 'simple_mean',
                   'trim_percent': 1, 'pattern_mode': 'mixed_only', 'allow_doji': True}
        response = self.client.put('/api/settings', json=changed, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()['telegram_enabled'], False)
        self.assertEqual(asdict(self.api.service.config), changed)
        persisted = json.loads(self.api.CONFIG_FILE.read_text('utf-8'))
        self.assertEqual({key:persisted[key] for key in changed}, changed)
        self.assertEqual(persisted['indicators']['enabled_indicators'], ['supertrend','smc'])
        self.assertEqual(persisted['quality']['min_activity_score'], 80)
        self.assertFalse(self.api.CONFIG_FILE.with_suffix('.tmp').exists())
        self.assertEqual(asdict(self.api.load_config()), changed)
        # Deterministic demo cache must follow changed settings, not stale rows.
        demo = self.client.get('/api/signals?mode=demo').json()['items']
        self.assertTrue(demo)
        self.assertTrue({row['pattern'] for row in demo} <= {'阴阳', '阳阴', '十字组合'})
        settings = self.client.get('/api/settings').json()
        self.assertEqual(settings['timeframes'], list(MONITOR_TIMEFRAMES))
        self.assertEqual(set(settings['lookback']), set(MONITOR_TIMEFRAMES))
        self.assertEqual(settings['pattern_mode'], 'mixed_only')

    def test_valid_live_chart_returns_only_saved_closed_candles(self):
        self.api.store.save_candles([
            Candle('BTCUSDT', '5m', 0, 299999, 100, 102, 99, 101),
            Candle('BTCUSDT', '5m', 300000, 599999, 101, 104, 100, 103),
            Candle('BTCUSDT', '5m', 600000, 899999, 103, 104, 100, 102, closed=False),
        ])
        response = self.client.get('/api/chart/btcusdt/5m?mode=live')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['source'], 'live')
        self.assertEqual([row['open_time'] for row in data['items']], [0, 300000])
        self.assertTrue(all(row['closed'] for row in data['items']))

    def test_legacy_signal_detail_rebuilds_full_chart_without_future_bars(self):
        interval = 300_000
        candles = []
        for index in range(260):
            opened = index * interval
            price = 100 + index * .05
            candles.append(Candle('BTCUSDT', '5m', opened, opened + interval - 1,
                                  price, price + 1, price - 1, price + .2))
        self.api.store.save_candles(candles)
        cutoff = candles[239].close_time
        alert = {
            'id': 'legacy-chart', 'symbol': 'BTCUSDT', 'timeframe': '5m',
            'close_time': cutoff, 'second_candle_close_time': cutoff,
            'source': 'live', 'pattern': '阳阳', 'activity_score': 70,
            'indicators': {'chart': {'supertrend': [], 'ema_fast': [],
                                     'ema_slow': [], 'levels': []}},
        }
        self.api.store.save_alert(alert)

        signal_id = f'BTCUSDT:5m:{cutoff}:TWO_BAR_VOLATILITY'
        response = self.client.get('/api/signals/' + quote(signal_id, safe=''))
        self.assertEqual(response.status_code, 200)
        item = response.json()['item']
        chart = item['indicators']['chart']
        expected = {'supertrend', 'flips', 'ema_fast', 'ema_slow', 'levels',
                    'events', 'order_blocks', 'fvgs', 'equal_levels',
                    'zone_bounds', 'strong_weak', 'swing_points'}
        self.assertTrue(expected.issubset(chart))
        self.assertIs(item['chart_recomputed_from_closed_history'], True)
        confirmed = [point.get('confirmed_at') for values in chart.values()
                     if isinstance(values, list) for point in values
                     if isinstance(point, dict) and point.get('confirmed_at') is not None]
        self.assertTrue(confirmed)
        self.assertLessEqual(max(confirmed), cutoff)

    def test_formal_history_chart_and_download_scope_is_only_five_and_fifteen_minutes(self):
        self.assertEqual(self.client.get('/api/chart/BTCUSDT/1m').status_code, 400)
        rejected = self.client.post('/api/history/download', headers=HEADERS, json={
            'symbol':'BTCUSDT','timeframe':'1m','months':['2020-01']})
        self.assertEqual(rejected.status_code, 422)
        rejected = self.client.post('/api/history/import', headers=HEADERS,
            params={'symbol':'BTCUSDT','timeframe':'1m','format':'csv'},
            content=b'0,100,102,99,101,10,59999,1005,12,6,603,0\n')
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('5m', rejected.json()['detail'])

    def test_chinese_history_download_parameters_are_normalized_and_scheduled_without_network(self):
        symbols = ('币安人生USDT','我踏马来了USDT','牛来USDT','龙虾USDT')
        with patch.object(self.api.research,'start',return_value={'id':'fake-job','state':'running'}) as start:
            for symbol in symbols:
                with self.subTest(symbol=symbol):
                    start.reset_mock()
                    response = self.client.post('/api/history/download', headers=HEADERS, json={
                        'symbol':symbol[:-4]+'usdt','timeframe':'5m','months':['2020-01']})
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.json()['id'],'fake-job')
                    self.assertEqual(start.call_args.args[0],'download')
                    self.assertEqual(start.call_args.args[1],{'symbol':symbol,'timeframe':'5m','months':['2020-01']})
        self.assertEqual(self.api.research.jobs,{})

    def test_chinese_csv_json_history_imports_round_trip_through_dataset_store(self):
        symbols = ('币安人生USDT','我踏马来了USDT','牛来USDT','龙虾USDT')
        csv_body = '0,100,102,99,101,10,299999,1005,12,6,603,0\n'
        for symbol in symbols:
            for kind in ('csv','json'):
                with self.subTest(symbol=symbol,format=kind):
                    content = csv_body if kind=='csv' else json.dumps([
                        Candle(symbol,'5m',0,299999,100,102,99,101,1005,603,12,10).to_dict()],ensure_ascii=False)
                    response = self.client.post('/api/history/import',
                        params={'symbol':symbol,'timeframe':'5m','format':kind},
                        content=content.encode('utf-8'),headers={**HEADERS,'Content-Type':'application/octet-stream'})
                    self.assertEqual(response.status_code,200)
                    dataset = response.json()
                    self.assertEqual(dataset['symbol'],symbol)
                    self.assertEqual(dataset['bars'],1)
                    rows, manifest = self.api.research.load(dataset['id'])
                    self.assertEqual(manifest['symbol'],symbol)
                    self.assertEqual([c.symbol for c in rows],[symbol])
                    self.assertFalse(self.api.research.busy)

    def test_unicode_history_symbols_still_reject_path_formula_control_and_sql_injection(self):
        bad = ('=币安人生USDT','+龙虾USDT','牛来USDT/../../x','我踏马来了USDT?url=x',
               '币安人生USDT\r\nX:evil','龙虾USDT\u200b','牛来USDT;DROP TABLE candles','牛','币'*41)
        with patch.object(self.api.research,'start') as start:
            for symbol in bad:
                with self.subTest(symbol=symbol):
                    response = self.client.post('/api/history/download', headers=HEADERS, json={
                        'symbol':symbol,'timeframe':'5m','months':['2020-01']})
                    self.assertEqual(response.status_code,422)
                    response = self.client.post('/api/history/import', headers=HEADERS,
                        params={'symbol':symbol,'timeframe':'5m','format':'csv'},
                        content=b'0,100,102,99,101,10,299999,1005,12,6,603,0\n')
                    self.assertEqual(response.status_code,400)
                    self.assertFalse(self.api.research.busy)
            start.assert_not_called()
        self.assertEqual(self.api.research.datasets(),[])

    def test_delivery_history_download_import_and_chart_share_exact_safe_identity(self):
        for symbol in ('BTCUSDT_260925','ETHUSDT_261225','中文USDT_260925'):
            with self.subTest(symbol=symbol):
                with patch.object(self.api.research,'start',return_value={'id':'fake-delivery-job'}) as start:
                    response = self.client.post('/api/history/download',headers=HEADERS,json={
                        'symbol':symbol.lower(),'timeframe':'5m','months':['2020-01']})
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(start.call_args.args[1]['symbol'],symbol)
                bar = Candle(symbol,'5m',0,299999,100,102,99,101,1005,603,12,10)
                for kind, content in (
                    ('csv','0,100,102,99,101,10,299999,1005,12,6,603,0\n'),
                    ('json',json.dumps([bar.to_dict()],ensure_ascii=False))):
                    response = self.client.post('/api/history/import',headers=HEADERS,
                        params={'symbol':symbol,'timeframe':'5m','format':kind},content=content.encode('utf-8'))
                    self.assertEqual(response.status_code,200)
                    rows, manifest = self.api.research.load(response.json()['id'])
                    self.assertEqual(rows[0].symbol,symbol)
                    self.assertEqual(manifest['symbol'],symbol)
                self.api.store.save_candles([bar])
                response = self.client.get('/api/chart/'+quote(symbol.lower(),safe='')+'/5m')
                self.assertEqual(response.status_code,200)
                self.assertEqual([row['symbol'] for row in response.json()['items']],[symbol])

    def test_delivery_suffix_does_not_allow_chart_or_history_injection(self):
        invalid = ('=BTCUSDT_260925','BTCUSDT_260925;DROP TABLE candles',
                   'BTCUSDT_260925%2fsecret','BTCUSDT_260925\r\nX:x','__')
        with patch.object(self.api.research,'start') as start:
            for symbol in invalid:
                with self.subTest(symbol=symbol):
                    self.assertEqual(self.client.post('/api/history/download',headers=HEADERS,json={
                        'symbol':symbol,'timeframe':'5m','months':['2020-01']}).status_code,422)
                    self.assertIn(self.client.get('/api/chart/'+quote(symbol,safe='')+'/5m').status_code,(400,404))
            start.assert_not_called()

    def test_demo_chart_is_not_a_live_price_fallback(self):
        sample = self.client.get('/api/signals?mode=demo').json()['items'][0]
        route = f"/api/chart/{sample['symbol']}/{sample['timeframe']}"
        live = self.client.get(route + '?mode=live').json()
        self.assertEqual(live, {'items': [], 'source': 'live'})
        demo = self.client.get(route + '?mode=demo').json()
        self.assertEqual(demo['source'], 'demo')
        self.assertEqual(demo['items'], sample['candles'])
        self.assertEqual(self.api.store.counts()['candles'], 0)

    def test_invalid_chart_and_query_values_are_rejected(self):
        for route in ('/api/chart/BTCUSDT/3m', '/api/chart/BTC-USDT/5m', '/api/chart/' + 'A' * 41 + '/5m'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 400)
        for route in ('/api/signals?mode=anything', '/api/signals?limit=0', '/api/signals?limit=10001',
                      '/api/chart/BTCUSDT/5m?mode=anything'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 422)

    def test_status_returns_stale_counts_while_one_slow_refresh_runs(self):
        seeded = self.client.get('/api/status').json()
        original_counts = seeded['database']
        original_updated_at = seeded['database_counts_updated_at']
        refreshed_counts = {'candles': 123, 'alerts': 4, 'oi_snapshots': 5}
        started = threading.Event()
        release = threading.Event()

        def slow_counts():
            started.set()
            release.wait(2)
            return refreshed_counts

        self.api._counts_cache['checked_at'] -= 61
        with patch.object(self.api.store, 'counts', side_effect=slow_counts) as counter:
            try:
                began = time.perf_counter()
                stale = self.client.get('/api/status').json()
                self.assertLess(time.perf_counter() - began, 0.5)
                self.assertEqual(stale['database'], original_counts)
                self.assertEqual(stale['database_counts_updated_at'], original_updated_at)
                self.assertTrue(started.wait(1))

                began = time.perf_counter()
                repeated = [self.client.get('/api/status').json() for _ in range(3)]
                self.assertLess(time.perf_counter() - began, 0.5)
                self.assertTrue(all(row['database'] == original_counts for row in repeated))
                self.assertEqual(counter.call_count, 1)
            finally:
                release.set()

            deadline = time.monotonic() + 2
            while self.api._counts_cache['refreshing'] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(self.api._counts_cache['refreshing'])
            current = self.client.get('/api/status').json()
            self.assertEqual(current['database'], refreshed_counts)
            self.assertGreaterEqual(current['database_counts_updated_at'], original_updated_at)
            self.assertEqual(counter.call_count, 1)

    def test_status_snapshot_does_not_wait_for_store_writer_lock(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_writer_connection():
            with self.api.store._lock:
                entered.set()
                release.wait(2)

        holder = threading.Thread(target=hold_writer_connection)
        holder.start()
        try:
            self.assertTrue(entered.wait(1))
            began = time.perf_counter()
            response = self.client.get('/api/status')
            self.assertLess(time.perf_counter() - began, .5)
            self.assertEqual(response.status_code, 200)
        finally:
            release.set()
            holder.join(2)
        self.assertFalse(holder.is_alive())

    def test_compact_live_list_and_exact_detail_preserve_immutable_record(self):
        original = {'symbol': 'BTCUSDT', 'timeframe': '1m', 'close_time': 119999,
                    'source': 'live', 'pattern': '阳阳', 'activity_score': 71,
                    'candles': [{'open_time': 0, 'close': 101}, {'open_time': 60000, 'close': 103}],
                    'score_breakdown': {'volume': 31}}
        self.assertTrue(self.api.store.save_alert(original))
        full = self.client.get('/api/signals?mode=live').json()['items'][0]
        compact = self.client.get('/api/signals?mode=live&include_candles=false').json()['items'][0]
        self.assertNotIn('candles', compact)
        self.assertEqual(compact, {k: v for k, v in full.items() if k != 'candles'})
        self.assertEqual(self.api.store.list_alerts(include_candles=False), [compact])
        response = self.client.get('/api/signals/' + quote(full['id'], safe=''))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'source': 'live', 'item': full, 'telegram_enabled': False})
        self.assertEqual(self.api.store.get_alert(full['id']), full)
        # Neither slim reads, detail reads nor duplicate writes alter old evidence.
        self.assertFalse(self.api.store.save_alert({**original, 'activity_score': 100, 'candles': []}))
        self.assertEqual(self.api.store.get_alert(full['id']), full)
        self.assertEqual(self.api.store.list_alerts(), [full])

    def test_live_usdt_filter_precedes_limit_and_old_other_quote_remains_readable(self):
        self.api.store.save_alert({'symbol':'BTCUSDT_260925','timeframe':'1m','close_time':59999,'source':'live'})
        self.api.store.save_alert({'symbol':'ETHUSDC','timeframe':'1m','close_time':119999,'source':'live','quote_asset':'USDC'})
        self.api.store.save_alert({'symbol':'MISLEADINGUSDT','timeframe':'1m','close_time':179999,'source':'live','quote_asset':'USDC'})
        self.api.store.save_alert({'symbol':'MISLEADINGUSDT_260925','timeframe':'1m','close_time':239999,'source':'live','quoteAsset':'USDC'})
        response = self.client.get('/api/signals?mode=live&limit=1&include_candles=false')
        self.assertEqual(response.status_code,200)
        self.assertEqual([row['symbol'] for row in response.json()['items']],['BTCUSDT_260925'])
        self.assertEqual(response.json()['total'],1)
        retained = self.api.store.list_alerts()
        self.assertEqual(len(retained),4)
        other = next(row for row in retained if row['symbol']=='ETHUSDC')
        detail = self.client.get('/api/signals/'+quote(other['id'],safe=''))
        self.assertEqual(detail.status_code,200)
        self.assertEqual(detail.json()['item'],other)

    def test_compact_demo_list_does_not_mutate_cached_demo_or_store(self):
        original = self.client.get('/api/signals?mode=demo').json()['items']
        compact = self.client.get('/api/signals?mode=demo&include_candles=false').json()['items']
        self.assertEqual(len(compact), len(original))
        self.assertTrue(all('candles' not in row for row in compact))
        self.assertEqual(compact, [{k: v for k, v in row.items() if k != 'candles'} for row in original])
        self.assertEqual(self.client.get('/api/signals?mode=demo').json()['items'], original)
        self.assertEqual(self.api.store.list_alerts(), [])

    def test_detail_never_falls_back_to_demo_or_partial_id(self):
        demo = self.client.get('/api/signals?mode=demo').json()['items'][0]
        self.assertEqual(self.client.get('/api/signals/' + quote(demo['id'], safe='')).status_code, 404)
        self.assertIsNone(self.api.store.get_alert(demo['id']))
        self.assertEqual(self.client.get('/api/signals/not-found').status_code, 404)
        original = {'symbol': 'BTCUSDT', 'timeframe': '1m', 'close_time': 119999, 'source': 'live'}
        self.api.store.save_alert(original)
        for non_id in ('BTCUSDT', 'BTCUSDT:1m', "' OR 1=1 --"):
            with self.subTest(non_id=non_id):
                self.assertEqual(self.client.get('/api/signals/' + quote(non_id, safe='')).status_code, 404)
        self.assertEqual(self.api.store.counts()['alerts'], 1)


if __name__ == '__main__':
    unittest.main()
