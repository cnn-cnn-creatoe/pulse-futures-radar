"""Offline, verified rebuild. Source is retained until a separate activation step.

Run with --build or --activate. A maintenance sentinel prevents application startup.
No Telegram or market network clients are imported or started.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from monitor.instance_lock import InstanceLock
from monitor.retention_worker import prepare_alert
from monitor.retention import (DAY_MS, RETENTION_DAYS, DETAIL_SCHEMA, DETAIL_INDEX,
                               candle_cutoff_sql)


def emit(**record):
    print(json.dumps(record, ensure_ascii=False), flush=True)


def fingerprint(path):
    result = {}
    for suffix in ('', '-wal'):
        p = Path(str(path) + suffix)
        stat = p.stat() if p.exists() else None
        # SQLite may create an empty WAL when a read-only connection opens.
        result[suffix] = [stat.st_size, stat.st_mtime_ns] if stat and (not suffix or stat.st_size) else None
    return result


def digest_row(digest, row):
    for value in row:
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        digest.update(len(raw).to_bytes(8, 'little'))
        digest.update(raw)


def prepared_rows(reader, src, cutoff, source_has_details):
    # Bounded workers/batches avoid loading the multi-gigabyte source into RAM.
    with ProcessPoolExecutor(max_workers=4) as pool:
        while batch := reader.fetchmany(128):
            jobs = []
            for row in batch:
                old = src.execute('SELECT payload FROM alert_details WHERE event_key=?', (row[1],)).fetchone() if source_has_details else None
                jobs.append((row, cutoff, old[0] if old else None))
            yield from pool.map(prepare_alert, jobs, chunksize=8)


def build(source, cutoff):
    target = source.with_name(source.stem + '.compact.sqlite3')
    report_path = source.with_name('retention-migration.json')
    sentinel = Path(str(source) + '.maintenance')
    if target.exists() or sentinel.exists():
        raise RuntimeError('An earlier maintenance run exists; inspect it before retrying.')
    sentinel.write_text(json.dumps({'pid': os.getpid(), 'started': time.time()}), encoding='utf-8')
    src = sqlite3.connect(source, timeout=5)
    # Flush existing committed WAL data before taking a stable read-only snapshot.
    checkpoint = src.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if checkpoint[0]:
        raise RuntimeError('Database is busy; close the application before maintenance.')
    src.close()
    original_fingerprint = fingerprint(source)
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    src.execute('BEGIN')
    dst = sqlite3.connect(target)
    dst.execute('PRAGMA auto_vacuum=INCREMENTAL')
    dst.execute('PRAGMA journal_mode=DELETE')
    dst.execute('PRAGMA synchronous=FULL')
    dst.execute('PRAGMA cache_size=-32768')
    schema = src.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name").fetchall()
    for kind, name, sql in schema:
        if kind == 'table':
            dst.execute(sql)
    dst.execute(DETAIL_SCHEMA)
    dst.commit()
    report = dict(status='building', cutoff_ms=cutoff,
                  cutoff_utc=datetime.fromtimestamp(cutoff / 1000, timezone.utc).isoformat(),
                  source=str(source), target=str(target), original_fingerprint=original_fingerprint,
                  tables={}, before_bytes=source.stat().st_size)
    predicates = {'candles': ('open_time>=' + candle_cutoff_sql(cutoff + RETENTION_DAYS * DAY_MS), None), 'open_interest': ('timestamp>=?', cutoff),
                  'market_evidence': ('close_time>=?', cutoff),
                  'indicator_research_events': ('day_start>=?', ((cutoff + 28800000) // DAY_MS) * DAY_MS - 28800000)}
    expected = {}
    source_has_details = any(name == 'alert_details' for _, name, _ in schema)
    total_alerts = src.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]
    started = time.monotonic()
    for kind, table, sql in schema:
        if kind != 'table' or table == 'alert_details':
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        condition, value = predicates.get(table, ('1=1', None))
        params = (value,) if value is not None else ()
        has_rowid = 'WITHOUT ROWID' not in sql.upper()
        columns = 'rowid,*' if has_rowid else '*'
        ordering = 'rowid' if has_rowid else ','.join('"' + r[1] + '"' for r in sorted(src.execute('PRAGMA table_info(' + quoted + ')'), key=lambda r:r[5]) if r[5])
        query = f'SELECT {columns} FROM {quoted} WHERE {condition} ORDER BY {ordering}'
        reader = src.execute(query, params)
        count = 0
        digest = hashlib.sha256()
        detail_count = 0
        expired_count = 0
        insert = None
        emit(stage='copy', table=table)
        stream = prepared_rows(reader, src, cutoff, source_has_details) if table == 'alerts' else ((r,None,False) for r in reader)
        for row, blob, expired in stream:
            row = list(row)
            if table == 'alerts':
                if expired:
                    expired_count += 1
                if blob is not None:
                    detail = (row[1], row[4], blob)
                    dst.execute('INSERT INTO alert_details VALUES (?,?,?)', detail)
                    # Verify this exact stored compressed blob, not only its in-memory source.
                    saved = dst.execute('SELECT payload FROM alert_details WHERE event_key=?', (row[1],)).fetchone()[0]
                    if saved != blob:
                        raise RuntimeError('Snapshot write verification failed')
                    detail_count += 1
            if insert is None:
                names = '(' + ','.join('"' + c[0] + '"' for c in reader.description) + ')'
                insert = f'INSERT INTO {quoted} {names} VALUES (' + ','.join('?' for _ in row) + ')'
            dst.execute(insert, row)
            digest_row(digest, row)
            count += 1
            if count % 1000 == 0:
                dst.commit()
                if shutil.disk_usage(target.parent).free < 10 * 1024**3:
                    raise RuntimeError('Stopped before exhausting disk space; original remains intact.')
                if count % 5000 == 0:
                    emit(table=table, rows=count, total=total_alerts if table == 'alerts' else None,
                         target_gb=round(target.stat().st_size/1024**3, 2), elapsed=round(time.monotonic()-started))
        dst.commit()
        report['tables'][table] = {'retained': count}
        if table == 'alerts':
            report['tables'][table].update(expired_summaries=expired_count, compressed_details=detail_count)
        expected[table] = (digest.hexdigest(), f'SELECT {columns} FROM {quoted} ORDER BY {ordering}')
    for kind, name, sql in schema:
        if kind != 'table':
            dst.execute(sql)
    dst.execute(DETAIL_INDEX)
    dst.commit()
    emit(stage='verify')
    for table, (wanted, query) in expected.items():
        actual = hashlib.sha256()
        for row in dst.execute(query):
            digest_row(actual, row)
        if actual.hexdigest() != wanted:
            raise RuntimeError('Row digest mismatch in ' + table)
        report['tables'][table]['sha256'] = wanted
        emit(verified=table)
    if dst.execute('SELECT COUNT(*) FROM alert_details').fetchone()[0] != report['tables']['alerts']['compressed_details']:
        raise RuntimeError('Detail row count mismatch')
    # SQLite checks every destination page; all retained payloads were round-trip checked above.
    check = [r[0] for r in dst.execute('PRAGMA quick_check')]
    if check != ['ok']:
        raise RuntimeError(str(check))
    dst.close()
    src.close()
    if fingerprint(source) != original_fingerprint:
        raise RuntimeError('Source changed during maintenance; refusing activation.')
    report.update(status='verified', after_bytes=target.stat().st_size, sqlite_check=check,
                  completed_utc=datetime.now(timezone.utc).isoformat())
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    emit(**report)


def activate(source):
    report_path = source.with_name('retention-migration.json')
    report = json.loads(report_path.read_text('utf-8'))
    target = Path(report['target'])
    backup = source.with_name(source.name + '.pre-retention')
    if report['status'] != 'verified' or fingerprint(source) != report['original_fingerprint']:
        raise RuntimeError('Source changed or rebuild is unverified; refusing replacement.')
    if backup.exists() or target.stat().st_size != report['after_bytes']:
        raise RuntimeError('Unexpected backup or target state')
    source.replace(backup)
    try:
        target.replace(source)
    except BaseException:
        backup.replace(source)
        raise
    # WAL was checkpointed before the read-only snapshot. Never attach an old WAL to the new DB.
    for suffix in ('-wal', '-shm'):
        Path(str(source) + suffix).unlink(missing_ok=True)
    report.update(status='activated', backup=str(backup))
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    Path(str(source) + '.maintenance').unlink()
    emit(status='activated', backup=str(backup), database=str(source))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--build', action='store_true')
    group.add_argument('--activate', action='store_true')
    parser.add_argument('--cutoff-ms', type=int)
    args = parser.parse_args()
    source = (ROOT / 'backend/data/pulse.sqlite3').resolve()
    lock = InstanceLock(ROOT / '.runtime/backend-instance.lock')
    lock.acquire()
    try:
        if args.build:
            build(source, args.cutoff_ms or int(time.time()*1000) - RETENTION_DAYS*DAY_MS)
        else:
            activate(source)
    finally:
        lock.close()
