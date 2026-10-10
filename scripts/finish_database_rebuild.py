"""Finish a rebuild whose individually verified alert stage is already committed.

Market tables use native SQLite bulk copy and exact keyed comparisons. This never
discards the completed compressed alert stage or modifies the original data.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from monitor.instance_lock import InstanceLock
from monitor.retention import RETENTION_DAYS, DAY_MS, DETAIL_INDEX, candle_cutoff_sql
from compact_database import fingerprint, emit


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def verify_table(db, table, condition='1=1', *, metadata_only=False):
    info = db.execute('PRAGMA original.table_info(' + qi(table) + ')').fetchall()
    columns = [r[1] for r in info if not (metadata_only and r[1] == 'payload')]
    keys = [r[1] for r in sorted(info, key=lambda r:r[5]) if r[5]]
    if not keys:
        keys = ['rowid']
    join = ' AND '.join('d.'+qi(k)+'=s.'+qi(k) for k in keys)
    differences = ' OR '.join('d.'+qi(k)+' IS NOT s.'+qi(k) for k in columns)
    count = db.execute(f'SELECT COUNT(*) FROM original.{qi(table)} AS s WHERE {condition}').fetchone()[0]
    actual = db.execute(f'SELECT COUNT(*) FROM main.{qi(table)}').fetchone()[0]
    if count != actual:
        raise RuntimeError(f'{table}: source/target row count differs ({count}/{actual})')
    mismatch = db.execute(f'''SELECT 1 FROM original.{qi(table)} AS s
        LEFT JOIN main.{qi(table)} AS d ON {join}
        WHERE ({condition}) AND ({differences}) LIMIT 1''').fetchone()
    if mismatch:
        raise RuntimeError(table + ': a retained field differs from its source')
    emit(verified=table, rows=count)
    return count


def check_blob(blob):
    # zlib verifies the checksum of every stored snapshot, including all overflow pages.
    zlib.decompress(blob)


def finish(source, *, now_ms=None, stage_log=None):
    target = source.with_name(source.stem + '.compact.sqlite3')
    sentinel = Path(str(source) + '.maintenance')
    stage_log = stage_log or ROOT / '.runtime/retention-migration.log'
    if not sentinel.exists() or not target.exists():
        raise RuntimeError('No paused rebuild was found')
    if '"stage": "copy", "table": "candles"' not in stage_log.read_text('utf-8'):
        raise RuntimeError('The verified alert stage has not completed; refusing reuse')
    now_ms = now_ms or int(time.time()*1000)
    cutoff = now_ms - RETENTION_DAYS*DAY_MS
    original_fingerprint = fingerprint(source)
    db = sqlite3.connect(target.as_uri(), uri=True, timeout=10)
    db.execute('PRAGMA cache_size=-65536')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('ATTACH DATABASE ? AS original', (source.as_uri()+'?mode=ro',))
    db.execute('PRAGMA original.cache_size=-65536')
    report = dict(status='building', source=str(source), target=str(target), cutoff_ms=cutoff,
                  cutoff_utc=datetime.fromtimestamp(cutoff/1000, timezone.utc).isoformat(),
                  before_bytes=source.stat().st_size, original_fingerprint=original_fingerprint,
                  method='completed per-alert round-trip verification plus native keyed table verification', tables={})
    emit(stage='verify_completed_alert_metadata')
    report['tables']['alerts'] = {'retained':verify_table(db, 'alerts', metadata_only=True),
                                 'recent_snapshots_round_trip_verified':True}
    conditions = {
        'candles': 's.open_time>=' + candle_cutoff_sql(now_ms).replace('CASE timeframe', 'CASE s.timeframe'),
        'open_interest': f's.timestamp>={cutoff}',
        'market_evidence': f's.close_time>={cutoff}',
        'indicator_research_events': 's.day_start>=' + str(((cutoff+28800000)//DAY_MS)*DAY_MS-28800000),
    }
    schema = db.execute("SELECT type,name,sql FROM original.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
    for kind, table, sql in schema:
        if kind != 'table' or table in ('alerts','alert_details'):
            continue
        emit(stage='bulk_copy', table=table)
        info = db.execute('PRAGMA original.table_info('+qi(table)+')').fetchall()
        columns = [r[1] for r in info]
        if 'WITHOUT ROWID' not in sql.upper():
            columns = ['rowid',*columns]
        names = ','.join(qi(n) for n in columns)
        selected = ','.join('s.'+qi(n) for n in columns)
        condition = conditions.get(table, '1=1')
        # Native copy keeps numeric values and JSON text exactly unchanged.
        with db:
            db.execute('DELETE FROM main.'+qi(table))
            db.execute(f'INSERT INTO main.{qi(table)} ({names}) SELECT {selected} FROM original.{qi(table)} AS s WHERE {condition}')
        report['tables'][table] = {'retained':verify_table(db,table,condition), 'exact_fields_verified':True}
    for kind, name, sql in schema:
        if kind == 'index' and not db.execute('SELECT 1 FROM main.sqlite_master WHERE name=?',(name,)).fetchone():
            db.execute(sql)
    db.execute(DETAIL_INDEX)
    db.commit()
    # The copy has taken time: expire the small additional interval that just crossed 30 days.
    with db:
        db.execute("""UPDATE alerts SET payload=json_set(json_remove(payload,
            '$.policy_snapshot','$.custom_indicator_snapshot'),'$.snapshot_expired',json('true'))
            WHERE event_key IN (SELECT event_key FROM alert_details WHERE close_time<?)""", (cutoff,))
        db.execute('DELETE FROM alert_details WHERE close_time<?', (cutoff,))
    emit(stage='verify_all_compressed_snapshots')
    digest = hashlib.sha256()
    count = 0
    reader = db.execute('SELECT payload FROM alert_details ORDER BY rowid')
    with ThreadPoolExecutor(max_workers=4) as pool:
        while batch := reader.fetchmany(64):
            blobs = [r[0] for r in batch]
            list(pool.map(check_blob, blobs))
            for blob in blobs:
                digest.update(len(blob).to_bytes(8,'little'))
                digest.update(blob)
            count += len(blobs)
            if count % 64000 == 0:
                emit(verified_snapshots=count)
    report['tables']['alerts'].update(compressed_details=count, details_sha256=digest.hexdigest())
    emit(stage='sqlite_quick_check')
    checks = [r[0] for r in db.execute('PRAGMA main.quick_check')]
    if checks != ['ok']:
        raise RuntimeError(str(checks))
    db.close()
    if fingerprint(source) != original_fingerprint:
        raise RuntimeError('Original database changed; refusing replacement')
    report.update(status='verified', sqlite_check=checks, after_bytes=target.stat().st_size,
                  completed_utc=datetime.now(timezone.utc).isoformat())
    source.with_name('retention-migration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    emit(**report)


if __name__ == '__main__':
    lock = InstanceLock(ROOT / '.runtime/backend-instance.lock')
    lock.acquire()
    try:
        finish((ROOT/'backend/data/pulse.sqlite3').resolve())
    finally:
        lock.close()
