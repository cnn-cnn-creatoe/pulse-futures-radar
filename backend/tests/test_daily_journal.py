from unittest.mock import patch as fixed_clock
"""Regression coverage for full-day evidence, causal replay and export parity."""
import csv
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest

from monitor.custom_indicators import CustomIndicatorRegistry
from monitor.daily_journal import (DailyArchives, ReplayRequest, SimpleReplay, archive_row,
    day_start, forward_outcome, record_custom, rule_pass)
from monitor.engine import Candle, TIMEFRAME_MS
from monitor.policy import Settings
from monitor.research import Research
from monitor.store import Store
from test_engine import fixture


class DailyJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'test.sqlite3')
        self.start = day_start('2026-09-05')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def alert(self, stamp, symbol='TESTUSDT', **extra):
        return {'symbol': symbol, 'timeframe': '5m', 'close_time': stamp,
            'pattern': '阳阳', 'price': 100, 'activity_score': 10, 'source': 'live',
            'volatility_ratio': 2, **extra}

    def test_full_day_over_1000_and_late_arrival_do_not_lose_rows(self):
        self.store.save_alert(self.alert(self.start - 1))
        for i in range(1205):
            self.store.save_alert(self.alert(self.start + i, symbol=f'TEST{i % 8}USDT'))
        first = self.store.day_alert_page(self.start, limit=1000)
        self.assertEqual(len(first['items']), 1000)
        self.assertTrue(first['has_more'])
        # An older close inserted during pagination must appear after the cursor.
        self.store.save_alert(self.alert(self.start + 2, symbol='LATEUSDT'))
        second = self.store.day_alert_page(self.start, after=first['cursor'], limit=1000)
        rows = first['items'] + second['items']
        self.assertEqual(len(rows), 1206)
        self.assertEqual(len({r['id'] for r in rows}), 1206)
        self.assertIn('LATEUSDT', {r['symbol'] for r in second['items']})
        self.assertFalse(second['has_more'])
        self.assertEqual(self.store.day_alert_page(self.start + 86400000)['items'], [])

    def test_symbol_day_includes_all_timeframes_patterns_and_scores(self):
        for i, pattern in enumerate(['阴阴', '阳阳', '阴阳', '阳阴']):
            self.store.save_alert(self.alert(self.start + i, pattern=pattern, timeframe='5m' if i % 2 else '15m'))
        self.store.save_alert(self.alert(self.start + 5, symbol='OTHERUSDT'))
        rows = self.store.day_alert_page(self.start, symbol='TESTUSDT')['items']
        self.assertEqual(len(rows), 4)
        self.assertEqual({r['timeframe'] for r in rows}, {'5m', '15m'})
        self.assertEqual(self.store.daily_counts_for_alert(rows[0])['daily_signal_count'], len(rows))

    @fixed_clock("monitor.daily_journal.time.time", return_value=1788652800)
    def test_archive_preserves_snapshots_combinations_and_restart(self, _clock):
        sample = self.alert(self.start + 1, candles=[{'heavy': 'chart'}], indicators={'heavy': 'chart'},
            policy_snapshot={'old_parameter': 3},
            available_confirmations=[{'id': 'wt_extreme_cross', 'passed': True}])
        self.store.save_alert(sample)
        archives = DailyArchives(self.store, self.root / 'daily')
        path = archives.export('2026-09-05')
        with path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['空策略'], '通过')
        self.assertEqual(rows[0]['WaveTrend · 超买超卖交叉'], '通过')
        saved = json.loads(rows[0]['完整记录JSON'])
        self.assertEqual(saved['policy_snapshot'], {'old_parameter': 3})
        self.assertNotIn('candles', saved)
        self.assertNotIn('indicators', saved)
        self.store.close()
        self.store = Store(self.root / 'test.sqlite3')
        restored = DailyArchives(self.store, self.root / 'daily')
        self.assertTrue(restored.listing()['items'][0]['ready'])
        self.assertEqual(len(self.store.day_alert_page(self.start)['items']), 1)

    def test_csv_missing_indicators_remain_unknown_and_formula_is_escaped(self):
        row = archive_row(self.alert(self.start, symbol='=TEST'))
        self.assertEqual(row['WaveTrend · 超买超卖交叉'], '未记录')
        self.assertEqual(row['交易对'], "'=TEST")
        for value in ['2026-9-5', '../2026-09-05', '2026-02-30', None]:
            with self.assertRaises(ValueError):
                day_start(value)

    def future(self, up=True):
        interval = TIMEFRAME_MS['5m']
        return [Candle('TESTUSDT', '5m', self.start + i * interval, self.start + (i + 1) * interval - 1,
            100, 111, 89, 110 if up else 90) for i in range(5)]

    def test_forward_results_long_short_costs_missing_and_gap(self):
        options = ReplayRequest(date='2026-09-05').model_dump()
        signal = self.alert(self.start - 1)
        long = forward_outcome(signal, self.future(), options)
        self.assertEqual(long['status'], '已完成')
        self.assertGreater(long['net_return_pct'], 9)
        self.assertLess(long['net_return_pct'], 10)
        short = forward_outcome({**signal, 'pattern': '阴阴'}, self.future(False), options)
        self.assertLess(short['price_change_pct'], 0)
        self.assertGreater(short['net_return_pct'], 9)
        self.assertEqual(forward_outcome(signal, self.future()[:2], options)['status'], '等待后续K线')
        self.assertIsNone(forward_outcome(signal, self.future()[:2], options)['net_return_pct'])
        self.assertEqual(forward_outcome(signal, self.future()[1:], options)['status'], '后续K线有缺口')


    def test_daily_replay_keeps_all_selected_and_csv_matches_results(self):
        candles = self.future()
        self.store.save_candles(candles)
        for i in range(3):
            self.store.save_alert(self.alert(candles[i].close_time))
        request = ReplayRequest(date='2026-09-05', holding_bars=2)
        research = Research(self.root / 'history')
        runner = SimpleReplay(self.store, research, CustomIndicatorRegistry(self.root / 'rules.json'), self.root / 'replays')
        job = {'id': 'a' * 32, 'request': request.model_dump(), 'processed': 0, 'indicator_names': []}
        runner.work(job, Settings(), [])
        self.assertEqual(job['metrics']['selected'], 3)
        self.assertEqual(job['metrics']['completed'], 3)
        with (runner.root / ('a' * 32 + '.csv')).open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['本次策略'], '空策略')



if __name__ == '__main__':
    unittest.main()
