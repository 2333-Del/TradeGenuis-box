"""Read-only history APIs and explicit asynchronous signal reviews."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
import math
import threading
import uuid
import history_store as db

PERIODS = ('1d','30m','1h')
HORIZONS = (1,3,5)
_job_lock = threading.Lock()

class ReviewConflict(Exception):
    pass

def positive(value):
    return isinstance(value,(float,int)) and math.isfinite(value) and value > 0

def calendar_days(start,end):
    import exchange_calendars as xc
    calendar = xc.get_calendar('XSHG')
    # Calendar bounds are authoritative: never extrapolate weekdays beyond published holidays.
    sessions = calendar.sessions_in_range(start,end)
    days = [x.strftime('%Y-%m-%d') for x in sessions]
    with db.pool().connection() as c:
        date = datetime.fromisoformat(start).date()
        stop = datetime.fromisoformat(end).date()
        opened = set(days)
        while date <= stop:
            c.execute('INSERT INTO trading_calendar VALUES (%s,%s,%s) ON CONFLICT(day) DO UPDATE SET is_open=EXCLUDED.is_open,source=EXCLUDED.source',
                (date,date.isoformat() in opened,'exchange_calendars '+xc.__version__+' XSHG'))
            date += timedelta(days=1)
    return days

def scale_reason(old,new):
    if old.get('adjustment') != new.get('adjustment') or old.get('adjustment') not in ('qfq','unadjusted'):
        return '复权口径不一致'
    previous = {b['date']:b for b in old.get('bars',[])}
    common = [(previous[b['date']],b) for b in new.get('bars',[]) if b['date'] in previous]
    if not common:
        return '历史重叠不足，无法核对价格尺度'
    for a,b in common:
        if any(abs(a[k]-b[k]) > max(0.011,abs(a[k])*0.001) for k in ('open','high','low','close')):
            return '历史价格尺度变化（疑似除权或数据修订）'
    return None

def evaluate(snapshot,frames,days,as_of,scan_date):
    """Pure evaluator. days are market sessions; missing individual bars never shift horizons."""
    future = sorted(d for d in days if d > scan_date)
    output = {'version':1,'as_of':as_of.isoformat(),'windows':{},'latest':{}}
    daily_old = snapshot.get('timeframes',{}).get('1d',{})
    daily_new = frames.get('1d',{})
    daily_scale = scale_reason(daily_old,daily_new)
    for period in PERIODS:
        old = snapshot.get('timeframes',{}).get(period,{})
        new = frames.get(period,{})
        bars = new.get('bars',[])
        scale = daily_scale or scale_reason(old,new)
        output['windows'][period] = {}
        def calc(target):
            r = dict(target_date=target,close=None,return_pct=None,breakout=None,held=None,touched=None,errors={})
            reason = None
            if not target or datetime.fromisoformat(target+'T15:00:00').replace(tzinfo=db.BJT)>as_of:
                reason = '观察期未到'
            target_bars = [b for b in bars if str(b['date'])[:10]==target]
            bar = target_bars[-1] if target_bars else None
            if not reason and (not bar or (period!='1d' and str(bar['date'])[11:16]!='15:00')):
                reason = new.get('error') or '目标日行情缺失（无法确认是否停牌）'
            if not reason and not positive(bar.get('vol')):
                reason = '停牌或无成交'
            reason = reason or scale
            if reason:
                r['errors'] = dict.fromkeys(('return_pct','breakout','held','touched'),reason)
                return r
            r['close'] = bar['close']
            price = snapshot.get('price')
            quote_at = snapshot.get('as_of_quote')
            quote_valid = bool(quote_at and str(quote_at)[:10] == scan_date)
            if positive(price) and quote_valid:
                r['return_pct'] = round((bar['close']/price-1)*100,6)
            else:
                r['errors']['return_pct'] = '选出时参考报价缺失或过期'
            high = old.get('box_high')
            if positive(high):
                threshold = Decimal(str(high))*Decimal('1.005')
                if old.get('state')=='NEAR':
                    r['breakout'] = Decimal(str(bar['close']))>threshold
                else:
                    r['errors']['breakout'] = '原状态非NEAR'
                if old.get('state')=='BREAK':
                    r['held'] = bar['close']>high
                else:
                    r['errors']['held'] = '原状态非BREAK'
                interval = [b for b in bars if scan_date<str(b['date'])[:10]<=target]
                expected = [d for d in future if d<=target]
                if set(expected)<=set(str(b['date'])[:10] for b in interval):
                    r['touched'] = any(Decimal(str(b['high']))>threshold for b in interval)
                else:
                    r['errors']['touched'] = '观察区间行情不完整'
            else:
                r['errors'].update(dict.fromkeys(('breakout','held','touched'),'原箱顶缺失'))
            return r
        for n in HORIZONS:
            output['windows'][period][str(n)] = calc(future[n-1] if len(future)>=n else None)
        completed = [d for d in future if datetime.fromisoformat(d+'T15:00:00').replace(tzinfo=db.BJT)<=as_of]
        output['latest'][period] = calc(completed[-1] if completed else None)
    return output

def fetch_frames(code,as_of):
    import scanner as sc
    from market_data import daily
    bars = daily(sc.fetch_kline(code, lmt=640),as_of)
    frames = {'1d':dict(bars=list(bars),source=bars.source,adjustment=bars.adjustment)}
    for fetch in (lambda:sc._m30_tencent(sc.tx_symbol(code)),lambda:sc._m30_sina(sc.tx_symbol(code)),lambda:sc._m30_em(code,sc.tx_symbol(code))):
        try:
            raw,_,source = fetch()
            aggregated = sc.rs.aggregate(raw,as_of)
            for p in ('30m','1h'):
                frames[p] = dict(bars=aggregated[p],source=source,adjustment='unadjusted')
            break
        except Exception:
            continue
    return frames

def filters(q):
    clauses, args = [],[]
    for key,column in (('from','b.trade_date >= %s'),('to','b.trade_date <= %s')):
        if q.get(key):
            datetime.strptime(q[key],'%Y-%m-%d')
            clauses.append(column);args.append(q[key])
    for key,column,allowed in (('asset','s.asset',('stock','etf')),('category','s.category',('qualified','near','other','all')),
                               ('level','s.level',('strong','soft','near','none'))):
        value=q.get(key)
        if value:
            if value not in allowed: raise ValueError('非法筛选项 '+key)
            if value!='all': clauses.append(column+'=%s');args.append(value)
    if q.get('version'):
        clauses.append('b.version=%s');args.append(q['version'])
    if q.get('from') and q.get('to') and q['from']>q['to']: raise ValueError('日期范围错误')
    return clauses,args

def page(q):
    number=int(q.get('page',1));size=int(q.get('page_size',50))
    if number<1 or not 1<=size<=100:raise ValueError('分页参数错误')
    return size,(number-1)*size

def uuid_str(value):
    return str(uuid.UUID(str(value)))

def batches(q):
    clauses,args=filters({k:v for k,v in q.items() if k in ('from','to','version')})
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    size,offset=page(q)
    rows=db.query('SELECT b.*,(SELECT count(*) FROM signal_snapshots s WHERE s.batch_id=b.id) AS count FROM scan_batches b'+where+' ORDER BY as_of DESC,id DESC LIMIT %s OFFSET %s',args+[size,offset])
    total=db.query('SELECT count(*) AS n FROM scan_batches b'+where,args)[0]['n']
    return dict(items=rows,total=total,archive=dict(db.STATE))

def batch_detail(identifier,q):
    identifier=uuid_str(identifier)
    found=db.query('SELECT * FROM scan_batches WHERE id=%s',(identifier,))
    if not found:raise LookupError('批次不存在')
    clauses,args=filters(q)
    clauses.insert(0,'s.batch_id=%s');args.insert(0,identifier)
    where=' WHERE '+' AND '.join(clauses)
    size,offset=page(q)
    rows=db.query('''SELECT s.*,r.result,r.updated_at,a.status AS update_status,a.error AS update_error,a.updated_at AS attempted_at
      FROM signal_snapshots s JOIN scan_batches b ON b.id=s.batch_id
      LEFT JOIN review_results r ON r.batch_id=s.batch_id AND r.code=s.code
      LEFT JOIN review_attempts a ON a.batch_id=s.batch_id AND a.code=s.code'''+where+' ORDER BY s.code LIMIT %s OFFSET %s',args+[size,offset])
    total=db.query('SELECT count(*) n FROM signal_snapshots s JOIN scan_batches b ON b.id=s.batch_id'+where,args)[0]['n']
    for row in rows:
        row['snapshot']={k:v for k,v in row['snapshot'].items() if k!='timeframes'} | {'timeframes':
            {p:{k:v for k,v in f.items() if k!='bars'} for p,f in row['snapshot'].get('timeframes',{}).items()}}
    return dict(batch=found[0],items=rows,total=total)

def symbol_detail(identifier,code):
    identifier=uuid_str(identifier)
    if not code.isdigit() or len(code)!=6:raise ValueError('股票代码错误')
    rows=db.query('''SELECT s.*,b.as_of,b.meta,r.result,r.updated_at,r.job_id,a.status AS update_status,a.error AS update_error,a.updated_at AS attempted_at FROM signal_snapshots s
      JOIN scan_batches b ON b.id=s.batch_id LEFT JOIN review_results r ON r.batch_id=s.batch_id AND r.code=s.code
      LEFT JOIN review_attempts a ON a.batch_id=s.batch_id AND a.code=s.code
      WHERE s.batch_id=%s AND s.code=%s''',(identifier,code))
    if not rows:raise LookupError('标的不存在')
    row=rows[0]
    observed=db.query('SELECT frames FROM review_observations WHERE job_id=%s AND code=%s',(row['job_id'],code)) if row.get('job_id') else []
    row['observations']=observed[0]['frames'] if observed else {}
    return row

def selected_batches(q):
    if q.get('batch_id'):
        return db.query("SELECT * FROM scan_batches WHERE id=%s AND status IN ('complete','incomplete','imported')",(uuid_str(q['batch_id']),))
    clauses,args=filters({k:v for k,v in q.items() if k in ('from','to','version')})
    clauses += ["b.mode='market'","b.status='complete'","NOT b.legacy",
                "(b.as_of AT TIME ZONE 'Asia/Shanghai')::time >= TIME '15:00'"]
    return db.query('SELECT DISTINCT ON (trade_date) b.* FROM scan_batches b WHERE '+' AND '.join(clauses)+' ORDER BY trade_date,as_of DESC,id DESC',args)

def stats(q):
    period=q.get('period','1d')
    if period not in PERIODS:raise ValueError('周期错误')
    chosen=selected_batches(q)
    clauses,args=filters(dict(q,category=q.get('category','qualified')))
    ids=[b['id'] for b in chosen]
    clauses.insert(0,'s.batch_id=ANY(%s)');args.insert(0,ids)
    if not q.get('batch_id'):
        clauses.append("left(s.snapshot->'timeframes'->'1d'->>'confirmed_at',10)=b.trade_date::text")
    rows=db.query('''SELECT s.code,s.asset,s.level,s.category,s.snapshot->>'name' AS name,b.id AS batch_id,b.trade_date,b.version,r.result
       FROM signal_snapshots s JOIN scan_batches b ON b.id=s.batch_id LEFT JOIN review_results r ON r.batch_id=s.batch_id AND r.code=s.code
       WHERE '''+' AND '.join(clauses),args)
    groups={}
    for row in rows:
        for n in HORIZONS:
            key=(row['asset'],row['version'],n)
            g=groups.setdefault(key,dict(asset=key[0],version=key[1],horizon=n,samples=[],metrics={}))
            result=(row.get('result') or {}).get('windows',{}).get(period,{}).get(str(n),{})
            g['samples'].append({k:v for k,v in row.items() if k!='result'} | {'performance':result})
    for g in groups.values():
        for name,field in (('up','return_pct'),('breakout','breakout'),('held','held')):
            valid=[s['performance'][field] for s in g['samples'] if s['performance'].get(field) is not None]
            successes=sum(v>0 if name=='up' else bool(v) for v in valid)
            excluded=Counter(s['performance'].get('errors',{}).get(field,'尚未复盘') for s in g['samples'] if s['performance'].get(field) is None)
            g['metrics'][name]=dict(success=successes,valid=len(valid),rate=successes/len(valid)*100 if valid else None,excluded=dict(excluded))
            if name=='up':g.update(mean_return=sum(valid)/len(valid) if valid else None,flat=sum(v==0 for v in valid))
    dates={str(b['trade_date']) for b in chosen}
    missing=[]
    if q.get('from') and q.get('to'):
        # Read-only: do not fetch or construct a calendar in a GET request.
        missing=[str(r['day']) for r in db.query('SELECT day FROM trading_calendar WHERE is_open AND day BETWEEN %s AND %s',(q['from'],q['to'])) if str(r['day']) not in dates]
    return dict(groups=list(groups.values()),batches=len(chosen),missing_dates=missing,
                note='每日信号样本，跨日重复入选重复计数；非独立交易次数。交易日历未覆盖的日期不推算。')

def start(q):
    if not isinstance(q,dict):raise ValueError('请求必须为对象')
    chosen=selected_batches(q)
    if not chosen:raise ValueError('没有可更新的批次')
    if len(chosen)>366:raise ValueError('单次最多更新366个批次')
    ids=[str(b['id']) for b in chosen]
    with _job_lock:
        with db.pool().connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(83082027)')
            active=c.execute("SELECT * FROM review_jobs WHERE status='running'").fetchall()
            for job in active:
                if set(ids)<=set(job['payload']['batch_ids']):return {'id':str(job['id']),'status':'running','reused':True}
                if set(ids)&set(job['payload']['batch_ids']):
                    raise ReviewConflict('部分批次正在更新，请完成后重新更新整个范围；本次未创建任务')
            identifier=str(uuid.uuid4())
            payload=dict(batch_ids=ids,done=0,total=0,errors=[])
            c.execute("INSERT INTO review_jobs(id,status,payload) VALUES (%s,'running',%s)",(identifier,db.jb(payload)))
        threading.Thread(target=worker,args=(identifier,chosen,payload),daemon=True).start()
    return dict(id=identifier,status='running')

def job_detail(identifier):
    rows=db.query('SELECT * FROM review_jobs WHERE id=%s',(uuid_str(identifier),))
    if not rows:raise LookupError('任务不存在')
    return rows[0]

def merge_results(old,new):
    """Failed individual metrics keep their last valid value with explicit stale evidence."""
    def merge(value,previous):
        value['value_as_of']={}
        for field in ('close','return_pct','breakout','held','touched'):
            previous_times=previous.get('value_as_of',{})
            previous_time=previous_times.get(field,old.get('as_of')) if isinstance(previous_times,dict) else previous_times
            if value.get(field) is None and previous.get(field) is not None and value.get('target_date')==previous.get('target_date'):
                value[field]=previous[field]
                stamp=previous.get('stale',{}).get(field,{}).get('as_of',previous_time)
                value.setdefault('stale',{})[field]=dict(as_of=stamp,error=value['errors'].get(field,'本次行情不可用'))
                value['value_as_of'][field]=stamp
            elif value.get(field) is not None:
                value['value_as_of'][field]=new['as_of']
        value['attempted_as_of']=new['as_of']
    for period,windows in new['windows'].items():
        for horizon,value in windows.items():
            merge(value,old.get('windows',{}).get(period,{}).get(horizon,{}))
    for period,value in new.get('latest',{}).items():
        merge(value,old.get('latest',{}).get(period,{}))
    return new

def record_attempt(identifier,row,status,error=None,connection=None):
    execute=connection.execute if connection else db.execute
    execute('''INSERT INTO review_attempts(batch_id,code,job_id,status,error) VALUES (%s,%s,%s,%s,%s)
      ON CONFLICT(batch_id,code) DO UPDATE SET job_id=EXCLUDED.job_id,status=EXCLUDED.status,error=EXCLUDED.error,updated_at=now()''',
      (row['batch_id'],row['code'],identifier,status,error))

def worker(identifier,chosen,payload):
    try:
        as_of=datetime.now(db.BJT)
        start_day=min(str(b['trade_date']) for b in chosen)
        # Include future sessions for pending horizons, subject to the calendar's published bounds.
        end=(as_of.date()+timedelta(days=20)).isoformat()
        import exchange_calendars as xc
        end=min(end,xc.get_calendar('XSHG').last_session.strftime('%Y-%m-%d'))
        days=calendar_days(start_day,end)
        rows=db.query('SELECT s.*,b.trade_date FROM signal_snapshots s JOIN scan_batches b ON b.id=s.batch_id WHERE s.batch_id=ANY(%s)',([b['id'] for b in chosen],))
        payload['total']=len(rows)
        cache={}
        for row in rows:
            code=row['code']
            try:
                if code not in cache:
                    try:cache[code]=fetch_frames(code,as_of)
                    except Exception as exc:cache[code]=exc
                frames=cache[code]
                if isinstance(frames,Exception):raise frames
                result=evaluate(row['snapshot'],frames,days,as_of,str(row['trade_date']))
                issues=sorted({reason for windows in result['windows'].values() for window in windows.values()
                               for reason in window['errors'].values()
                               if reason not in ('观察期未到','原状态非NEAR','原状态非BREAK','原箱顶缺失','选出时参考报价缺失或过期')})
                previous=db.query('SELECT result FROM review_results WHERE batch_id=%s AND code=%s',(row['batch_id'],code))
                result=merge_results(previous[0]['result'] if previous else {},result)
                with db.pool().connection() as c:
                    c.execute('INSERT INTO review_observations VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',(identifier,code,as_of,db.jb(frames)))
                    c.execute('INSERT INTO review_versions VALUES (%s,%s,%s,%s)',(identifier,row['batch_id'],code,db.jb(result)))
                    c.execute('''INSERT INTO review_results(batch_id,code,job_id,result) VALUES (%s,%s,%s,%s)
                       ON CONFLICT(batch_id,code) DO UPDATE SET job_id=EXCLUDED.job_id,result=EXCLUDED.result,updated_at=now()''',
                       (row['batch_id'],code,identifier,db.jb(result)))
                    record_attempt(identifier,row,'partial' if issues else 'complete','；'.join(issues) if issues else None,connection=c)
                if issues:payload['errors'].append(dict(code=code,batch_id=str(row['batch_id']),error='；'.join(issues)))
            except Exception as exc:
                payload['errors'].append(dict(code=code,batch_id=str(row['batch_id']),error=type(exc).__name__))
                record_attempt(identifier,row,'failed','行情更新失败：'+type(exc).__name__)
            payload['done']+=1
            db.execute('UPDATE review_jobs SET payload=%s,updated_at=now() WHERE id=%s',(db.jb(payload),identifier))
        status='partial' if payload['errors'] else 'complete'
    except Exception as exc:
        payload['errors'].append({'error':type(exc).__name__})
        status='failed'
    try:db.execute('UPDATE review_jobs SET status=%s,payload=%s,updated_at=now() WHERE id=%s',(status,db.jb(payload),identifier))
    except Exception:pass  # restart recovery marks abandoned jobs interrupted
