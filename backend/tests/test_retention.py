import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

from monitor.retention import DAY_MS, split_alert, restore_alert
from monitor.store import Store
from monitor.engine import Candle
from monitor.daily_journal import DailyArchives, day_start


def signal(stamp):
    return dict(symbol='BTCUSDT', timeframe='5m', close_time=stamp, price=100,
                pattern='阳阳', direction='long', qualified=True, quality_schema_version=2,
                candles=[dict(close=100, close_time=stamp)]*4,
                indicators=dict(status='ready', supertrend=dict(atr=1,direction='up'),
                                chart={'large': list(range(1000))}),
                trade_plan=dict(direction='long',stop_loss=99,risk_reward=3),
                policy_snapshot={'test': 1})


def test_compression_is_lossless_and_does_not_mutate_signal():
    original = signal(100)
    before = copy.deepcopy(original)
    summary, blob = split_alert(original)
    assert original == before
    assert len(blob) < len(json.dumps(original))
    restored = restore_alert(json.loads(summary), blob)
    assert all(restored[k] == v for k,v in original.items())


def test_recent_reads_and_scores_match_and_expiry_preserves_counters():
    store = Store(':memory:')
    try:
        now = 100 * DAY_MS
        cutoff = now - 30 * DAY_MS
        old, boundary = signal(cutoff-1), signal(cutoff)
        assert store.save_alert(old)
        assert store.save_alert(boundary)
        assert not store.save_alert(boundary)
        before = store.list_alerts()
        detail_id = before[0]['id']
        assert before[0]['indicators'] == boundary['indicators']
        compact = store.list_alerts(include_candles=False)[0]
        assert compact['actionability'] == before[0]['actionability']
        assert 'indicators' not in compact
        counters = store.alert_days()
        store.prune(now)
        assert store.alert_days() == counters
        assert store.get_alert(detail_id)['candles'] == boundary['candles']
        older = store.get_alert(before[1]['id'])
        assert older['snapshot_expired'] is True
        assert 'candles' not in older and 'policy_snapshot' not in older
        assert older['actionability'] == before[1]['actionability']
        assert store._db.execute('SELECT COUNT(*) FROM alert_details').fetchone()[0] == 1
        store.prune(now)
        assert store.counts()['alerts'] == 2
    finally:
        store.close()


def test_archive_pruning_only_removes_expired_generated_files():
    with tempfile.TemporaryDirectory() as folder:
        store = Store(':memory:')
        try:
            archive = DailyArchives(store, folder)
            root = Path(folder)
            for name in ('2026-08-01.csv','2026-08-01.meta.json','2026-10-01.csv','manual.csv'):
                (root/name).write_text('test')
            with patch('monitor.daily_journal.time.time',return_value=day_start('2026-10-03')/1000):
                archive.prune_files()
            assert not (root/'2026-08-01.csv').exists()
            assert not (root/'2026-08-01.meta.json').exists()
            assert (root/'2026-10-01.csv').exists()
            assert (root/'manual.csv').exists()
        finally:
            store.close()


def test_offline_rebuild_verifies_then_activates_without_deleting_original():
    script = Path(__file__).resolve().parents[2]/'scripts/compact_database.py'
    spec = importlib.util.spec_from_file_location('compact_database_test',script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder)/'pulse.sqlite3'
        store = Store(source)
        now = 100 * DAY_MS
        store.save_alert(signal(now))
        store.save_alert(signal(now-31*DAY_MS))
        store.save_candles([Candle('BTCUSDT','1m',t,t+59999,100,101,99,100.5)
                            for t in (now-DAY_MS,now-31*DAY_MS)])
        with store._db:
            store._db.execute('INSERT INTO universe_snapshot VALUES (1,?,?)',(now-1,now))
        expected = store.list_alerts()[0]
        store.close()
        # Mix a legacy inline payload with the new compressed format.
        with sqlite3.connect(source) as db:
            old = signal(now-32*DAY_MS)
            db.execute('INSERT INTO alerts VALUES (?,?,?,?,?,?)',('legacy','BTCUSDT','5m',old['close_time'],now,json.dumps(old)))
        db.close()
        module.build(source,now-30*DAY_MS)
        # Exercise the native continuation path against an already verified alert stage.
        finish_spec = importlib.util.spec_from_file_location('finish_rebuild_test',script.with_name('finish_database_rebuild.py'))
        finish_module = importlib.util.module_from_spec(finish_spec)
        sys.path.insert(0,str(script.parent))
        try:
            finish_spec.loader.exec_module(finish_module)
        finally:
            sys.path.pop(0)
        stage_log = Path(folder)/'stage.log'
        stage_log.write_text('{"stage": "copy", "table": "candles"}',encoding='utf-8')
        finish_module.finish(source,now_ms=now,stage_log=stage_log)
        assert Path(str(source)+'.maintenance').exists()
        try:
            Store(source)
            assert False, 'maintenance must block startup'
        except RuntimeError:
            pass
        module.activate(source)
        assert Path(str(source)+'.pre-retention').exists()
        store = Store(source)
        try:
            assert store.get_alert(expected['id'])['indicators'] == expected['indicators']
            assert store.get_alert('legacy')['snapshot_expired']
            assert store._db.execute('PRAGMA auto_vacuum').fetchone()[0] == 2
            assert store.counts()['candles'] == 1
            assert tuple(store._db.execute('SELECT * FROM universe_snapshot').fetchone()) == (1,now-1,now)
        finally:
            store.close()
