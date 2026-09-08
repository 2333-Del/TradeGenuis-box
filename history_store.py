"""PostgreSQL history and crash-safe immutable scan outbox. No market I/O."""
from __future__ import annotations
import functools
import hashlib
import inspect
import json
import os
from pathlib import Path
import threading
import uuid
from datetime import datetime, timezone, timedelta
from contextvars import ContextVar

ROOT = Path(__file__).resolve().parent
OUTBOX = ROOT / 'data' / 'history_outbox'
BJT = timezone(timedelta(hours=8))
_pool = None
_lock = threading.RLock()
STATE = {'status': 'unconfigured', 'pending': 0}
_cutoff = ContextVar('scan_cutoff',default=None)

def scan_cutoff():
    return _cutoff.get() or datetime.now(BJT)

def now():
    return datetime.now(BJT).isoformat()

def configured():
    return bool(os.getenv('DATABASE_URL') or os.getenv('PGHOST'))

def pool():
    global _pool
    with _lock:
        if _pool is None:
            if not configured():
                raise RuntimeError('PostgreSQL 未配置')
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row
            _pool = ConnectionPool(os.getenv('DATABASE_URL', ''), min_size=0, max_size=6, open=True,
                timeout=4, kwargs={'row_factory': dict_row, 'connect_timeout': 3},
                check=ConnectionPool.check_connection)
        return _pool

def query(sql, args=()):
    with pool().connection() as c:
        return c.execute(sql, args).fetchall()

def execute(sql, args=()):
    with pool().connection() as c:
        c.execute(sql, args)

def jb(value):
    from psycopg.types.json import Jsonb
    return Jsonb(value)

def migrate():
    with pool().connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(83082026)')
        c.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY)')
        for path in sorted((ROOT / 'migrations').glob('*.sql')):
            if not c.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (path.name,)).fetchone():
                c.execute(path.read_text(encoding='utf-8'))
                c.execute('INSERT INTO schema_migrations VALUES (%s)', (path.name,))

def persist(batch):
    with pool().connection() as c:
        inserted = c.execute('''INSERT INTO scan_batches
          (id,started_at,finished_at,as_of,trade_date,mode,version,status,legacy,meta)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id''',
          (batch['id'],batch['started_at'],batch['finished_at'],batch['as_of'],
           batch['as_of'][:10],batch['mode'],batch['version'],batch['status'],
           batch.get('legacy',False),jb(batch['meta']))).fetchone()
        if not inserted:
            return
        for row in batch['rows']:
            category = 'qualified' if row.get('qualified') else 'near' if row.get('resonance_level') == 'near' else 'other'
            c.execute('INSERT INTO signal_snapshots VALUES (%s,%s,%s,%s,%s,%s)',
                (batch['id'],row['code'],'etf' if row.get('market') == 'etf' else 'stock',
                 category,row.get('resonance_level','none'),jb(row)))

def archive(batch):
    # Write ahead even when PostgreSQL is online, so a process crash cannot lose a completed scan.
    target = OUTBOX / (batch['id'] + '.json')
    tmp = OUTBOX / (batch['id'] + '.' + uuid.uuid4().hex + '.tmp')
    try:
        OUTBOX.mkdir(parents=True,exist_ok=True)
        with tmp.open('w',encoding='utf-8') as f:
            json.dump(batch,f,ensure_ascii=False,allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,target)
    except Exception as exc:
        STATE.update(status='error')
        raise RuntimeError(f'历史归档失败：无法写入本地待归档队列（{exc}）') from exc
    flush()

def flush():
    with _lock:
        paths = sorted(OUTBOX.glob('*.json'))
        try:
            if paths:
                migrate()
            elif configured():
                query('SELECT 1')
            for path in paths:
                persist(json.loads(path.read_text(encoding='utf-8')))
                path.unlink()
            STATE.update(status='ready' if configured() else 'unconfigured')
        except Exception:
            STATE.update(status='pending' if paths else 'unavailable')
        STATE['pending'] = len(list(OUTBOX.glob('*.json')))
    return dict(STATE)

def archived_scan(mode):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args,**kwargs):
            from types import SimpleNamespace
            sc=SimpleNamespace(**fn.__globals__)
            import_legacy()
            started = now()
            bound = inspect.signature(fn).bind_partial(*args,**kwargs)
            actual = 'quick' if mode == 'market' and bound.arguments.get('full',True) is False else mode
            version = hashlib.sha256((ROOT/'resonance.py').read_bytes()+(ROOT/'scanner.py').read_bytes()).hexdigest()[:12]
            batch = dict(id=str(uuid.uuid4()),started_at=started,as_of=started,mode=actual,
                         version=version,rows=[],meta={},status='failed')
            cutoff_token=_cutoff.set(datetime.fromisoformat(started))
            try:
                rows = fn(*args,**kwargs)
                batch['rows'] = [r for r in rows if actual == 'pool' or (r.get('score') or 0)>=sc.SAVE_MIN_SCORE
                                  or r.get('resonance_level') in ('strong','soft','near')]
                payload = json.loads(sc.WATCH_FILE.read_text(encoding='utf-8'))
                batch['meta'] = {k:v for k,v in payload.items() if k!='candidates'}
                batch['meta']['parameters'] = {k:getattr(sc,k) for k in ('BOX_LOOK','VOL_MULT','VOL_DAYS_REQ','SAVE_MIN_SCORE')}
                batch['meta']['requested_top'] = bound.arguments.get('top',sc.MARKET_TOP)
                batch['status'] = 'complete' if rows or (actual=='pool' and payload.get('pool_size')==0) else 'incomplete'
                health=batch['meta'].get('data_health',{})
                batch['meta']['partial_data'] = bool(health.get('unavailable') or health.get('analysis_failed') or any(r.get('error') for r in rows))
                if actual in ('market','quick') and (not payload.get('universe_size') or not health.get('evaluated')):
                    batch['status']='incomplete'
            except Exception as exc:
                batch['meta']['error'] = type(exc).__name__
                batch['finished_at'] = now()
                try:
                    archive(batch)
                except Exception as archive_exc:
                    # 归档失败不能吞掉扫描的真实异常，否则故障原因无从排查。
                    raise RuntimeError(f'扫描失败（{type(exc).__name__}: {exc}），且归档也失败（{archive_exc}）') from exc
                raise
            finally:
                _cutoff.reset(cutoff_token)
            batch['finished_at'] = now()
            archive(batch)
            progress = kwargs.get('progress')
            if progress:
                progress('历史已归档' if STATE['status']=='ready' else '历史待归档（已保存本地队列）')
            return rows
        return wrapped
    return decorate

def import_legacy():
    path = ROOT/'data'/'watchlist.json'
    marker = ROOT/'data'/'history_imported'
    if marker.exists():
        return
    if not path.exists():
        marker.parent.mkdir(parents=True,exist_ok=True)
        marker.write_text(now(),encoding='utf-8')
        return
    raw = path.read_bytes()
    try:
        p = json.loads(raw)
        stamp = p.get('as_of') or now()
        parsed = datetime.fromisoformat(stamp)
    except Exception:
        # 文件可能在上次写入中途被截断；按「无可导入」处理并写标记，
        # 否则每次启动与扫描都会在同一份坏文件上失败。
        print(f'旧数据导入跳过：watchlist.json 无法解析', flush=True)
        marker.parent.mkdir(parents=True,exist_ok=True)
        marker.write_text(now(),encoding='utf-8')
        return
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BJT)
    stamp = parsed.isoformat()
    archive(dict(id=str(uuid.uuid5(uuid.NAMESPACE_URL,hashlib.sha256(raw).hexdigest())),
        started_at=stamp,finished_at=stamp,as_of=stamp,mode=p.get('scope','pool'),
        version='legacy',legacy=True,status='imported',meta={'label':'旧数据导入'},rows=p.get('candidates',[])))
    marker.write_text(now(),encoding='utf-8')

def initialize():
    if configured():
        migrate()
        execute("UPDATE review_jobs SET status='interrupted',updated_at=now() WHERE status='running'")
    import_legacy()
    flush()
