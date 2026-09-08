import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
from datetime import datetime

import history_store as db
import reviews as rv

def bar(day,close=100,vol=100):
    return dict(date=day,open=100,high=max(102,close),low=min(99,close),close=close,vol=vol)

def fixture():
    prior=[bar('2026-09-04')]
    old=dict(state='NEAR',box_high=100,box_low=90,bars=prior,adjustment='qfq',confirmed_at='2026-09-04')
    snapshot=dict(code='600001',name='测试',qualified=True,resonance_level='soft',price=100,
                  as_of_quote='2026-09-04T15:00:00+08:00',timeframes={'1d':old})
    frames={'1d':dict(bars=copy.deepcopy(prior)+[bar('2026-09-07',101),bar('2026-09-08',99),bar('2026-09-09',102)],adjustment='qfq')}
    return snapshot,frames

def batch(stamp='2026-09-04T15:00:00+08:00',mode='market'):
    s,_=fixture()
    return dict(id=str(uuid.uuid4()),started_at=stamp,finished_at=stamp,as_of=stamp,mode=mode,
                version='test-v1',status='complete',meta={},rows=[s])

class CalculationTests(unittest.TestCase):
    def result(self,s=None,f=None,at='2026-09-09T16:00:00+08:00'):
        original,frames=fixture()
        return rv.evaluate(s or original,f or frames,['2026-09-04','2026-09-07','2026-09-08','2026-09-09','2026-09-10','2026-09-11'],datetime.fromisoformat(at),'2026-09-04')['windows']['1d']

    def test_fixed_horizons_and_threshold(self):
        r=self.result()
        self.assertEqual(r['1']['target_date'],'2026-09-07')
        self.assertEqual(r['1']['return_pct'],1)
        self.assertTrue(r['1']['breakout'])
        self.assertEqual(r['3']['return_pct'],2)
        self.assertIsNone(r['5']['return_pct'])
        s,f=fixture();f['1d']['bars'][1]['close']=100.5
        self.assertFalse(self.result(s,f)['1']['breakout'])

    def test_no_intraday_confirmation(self):
        r=self.result(at='2026-09-07T14:59:00+08:00')
        self.assertIsNone(r['1']['breakout'])
        self.assertEqual(r['1']['errors']['breakout'],'观察期未到')

    def test_missing_bars_do_not_shift_calendar(self):
        s,f=fixture();f['1d']['bars'].pop(1)
        r=self.result(s,f)
        self.assertEqual(r['1']['target_date'],'2026-09-07')
        self.assertIsNone(r['1']['return_pct'])
        self.assertEqual(r['3']['return_pct'],2)

    def test_suspension_and_reference_failure_are_not_loss(self):
        s,f=fixture();f['1d']['bars'][1]['vol']=0
        self.assertEqual(self.result(s,f)['1']['errors']['return_pct'],'停牌或无成交')
        s,f=fixture();s['price']=None
        r=self.result(s,f)['1']
        self.assertIsNone(r['return_pct']);self.assertTrue(r['breakout'])

    def test_exdiv_and_missing_overlap_excluded(self):
        s,f=fixture();f['1d']['bars'][0]['close']=95
        self.assertIsNone(self.result(s,f)['1']['breakout'])
        s,f=fixture();f['1d']['bars'].pop(0)
        self.assertIsNone(self.result(s,f)['1']['breakout'])

    def test_held_is_separate_from_breakout(self):
        s,f=fixture();s['timeframes']['1d']['state']='BREAK'
        r=self.result(s,f)['1'];self.assertTrue(r['held']);self.assertIsNone(r['breakout'])

    def test_stale_retains_evidence(self):
        old={'as_of':'old','windows':{'1d':{'1':{'return_pct':2}}}}
        new={'as_of':'new','windows':{'1d':{'1':{'return_pct':None,'errors':{'return_pct':'缺失'}}}}}
        result=rv.merge_results(old,new)
        self.assertEqual(result['windows']['1d']['1']['return_pct'],2)
        self.assertEqual(result['windows']['1d']['1']['stale']['return_pct']['as_of'],'old')
        self.assertEqual(result['windows']['1d']['1']['value_as_of']['return_pct'],'old')
        retried=rv.merge_results(result,{'as_of':'third','windows':{'1d':{'1':{'return_pct':None,'errors':{'return_pct':'缺失'}}}}})
        self.assertEqual(retried['windows']['1d']['1']['value_as_of']['return_pct'],'old')

    def test_outbox_survives_database_outage(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(db,'OUTBOX',Path(directory)),patch.object(db,'migrate',side_effect=RuntimeError):
            b=batch();db.archive(b)
            self.assertEqual(db.STATE['pending'],1)
            self.assertEqual(json.loads(next(Path(directory).glob('*.json')).read_text(encoding='utf-8'))['id'],b['id'])
            with patch.object(db,'migrate'),patch.object(db,'persist') as write:
                db.flush();write.assert_called_once();self.assertEqual(db.STATE['pending'],0)

    def test_outbox_failure_is_explicit(self):
        with patch.object(db.OUTBOX.__class__,'mkdir',side_effect=OSError('read only')):
            with self.assertRaisesRegex(RuntimeError,'历史归档失败'):db.archive(batch())

    def test_archive_cutoff_does_not_come_from_first_row(self):
        import scanner as sc
        with tempfile.TemporaryDirectory() as folder:
            watch=Path(folder)/'watch.json';watch.write_text('{"pool_size":1}',encoding='utf-8')
            @db.archived_scan('pool')
            def scan():
                self.assertEqual(db.scan_cutoff().isoformat(),'2026-09-08T15:10:00+08:00')
                return [dict(code='600001',resonance_as_of='2026-09-07T15:00:00+08:00')]
            # Decorator binds actual scanner globals also when scanner runs as __main__.
            scan.__wrapped__.__globals__.update({k:getattr(sc,k) for k in ('WATCH_FILE','BOX_LOOK','VOL_MULT','VOL_DAYS_REQ','SAVE_MIN_SCORE','MARKET_TOP')})
            scan.__wrapped__.__globals__['WATCH_FILE']=watch
            with patch.object(db,'import_legacy'),patch.object(db,'archive') as archive,patch.object(db,'now',return_value='2026-09-08T15:10:00+08:00'):
                scan()
                self.assertEqual(archive.call_args.args[0]['as_of'],'2026-09-08T15:10:00+08:00')

    def test_calendar_holidays(self):
        import exchange_calendars as xc
        dates=xc.get_calendar('XSHG').sessions_in_range('2026-09-30','2026-10-09').strftime('%Y-%m-%d').tolist()
        self.assertEqual(dates,['2026-09-30','2026-10-08','2026-10-09'])

    def test_corrupt_watchlist_does_not_block_startup(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(db,'ROOT',Path(directory)):
            folder=Path(directory)/'data';folder.mkdir()
            (folder/'watchlist.json').write_text('{"as_of": ',encoding='utf-8')  # 模拟写入中途截断
            db.import_legacy()
            self.assertTrue((folder/'history_imported').exists())

    def test_failed_scan_keeps_original_error_when_archive_fails(self):
        @db.archived_scan('pool')
        def scan():
            raise ValueError('行情断网')
        with patch.object(db,'import_legacy'),patch.object(db,'archive'):
            with self.assertRaisesRegex(ValueError,'行情断网'):
                scan()
        with patch.object(db,'import_legacy'),patch.object(db,'archive',side_effect=RuntimeError('磁盘只读')):
            with self.assertRaisesRegex(RuntimeError,'行情断网'):
                scan()

@unittest.skipUnless(os.getenv('HISTORY_TEST_DATABASE_URL'),'requires isolated HISTORY_TEST_DATABASE_URL')
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env=patch.dict(os.environ,{'DATABASE_URL':os.environ['HISTORY_TEST_DATABASE_URL']});cls.env.start()
        db._pool=None;db.migrate()

    def setUp(self):
        # Only an explicitly designated disposable test database may run this destructive cleanup.
        db.execute('TRUNCATE review_attempts,review_versions,review_observations,review_results,review_jobs,signal_snapshots,scan_batches,trading_calendar CASCADE')

    def test_atomic_immutable_and_pagination(self):
        b=batch();db.persist(b);b['rows'][0]['price']=200;db.persist(b)
        self.assertEqual(rv.symbol_detail(b['id'],'600001')['snapshot']['price'],100)
        self.assertEqual(rv.batches({'page_size':'1'})['total'],1)
        broken=batch();broken['rows'].append(dict(broken['rows'][0]))
        with self.assertRaises(Exception):db.persist(broken)
        self.assertEqual(rv.batches({})['total'],1)
        db.migrate()

    def test_last_closing_full_scan_only(self):
        morning=batch('2026-09-04T11:30:00+08:00');full=batch();later=batch('2026-09-04T15:30:00+08:00');quick=batch('2026-09-04T16:00:00+08:00','quick')
        for b in (morning,full,later,quick):db.persist(b)
        self.assertEqual(str(rv.selected_batches({})[0]['id']),later['id'])

    def test_review_worker_and_stats(self):
        b=batch();db.persist(b)
        identifier=str(uuid.uuid4());payload={'batch_ids':[b['id']],'total':0,'done':0,'errors':[]}
        db.execute("INSERT INTO review_jobs(id,status,payload) VALUES (%s,'running',%s)",(identifier,db.jb(payload)))
        _,f=fixture()
        with patch.object(rv,'fetch_frames',return_value=f):rv.worker(identifier,rv.selected_batches({}),payload)
        self.assertEqual(rv.job_detail(identifier)['status'],'partial')
        result=rv.stats({})['groups'][0]
        self.assertEqual(result['metrics']['up']['valid'],1)
        self.assertEqual(result['metrics']['breakout']['success'],1)
        self.assertEqual(rv.symbol_detail(b['id'],'600001')['observations'],f)

    def test_reuse_running_job(self):
        b=batch();db.persist(b)
        with patch('threading.Thread.start'):
            first=rv.start({'batch_id':b['id']});second=rv.start({'batch_id':b['id']})
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['reused'])

    def test_overlap_never_silently_drops_new_batch(self):
        a=batch();b=batch('2026-09-07T15:00:00+08:00')
        db.persist(a);db.persist(b)
        with patch('threading.Thread.start'):
            rv.start({'batch_id':a['id']})
            with self.assertRaises(rv.ReviewConflict):rv.start({'from':'2026-09-04','to':'2026-09-07'})
            job=rv.start({'batch_id':b['id']})
        self.assertFalse(job.get('reused',False))

    def test_refresh_failure_preserves_result_and_attempt(self):
        b=batch();db.persist(b)
        def run(fetch):
            identifier=str(uuid.uuid4());payload={'batch_ids':[b['id']],'total':0,'done':0,'errors':[]}
            db.execute("INSERT INTO review_jobs(id,status,payload) VALUES (%s,'running',%s)",(identifier,db.jb(payload)))
            with patch.object(rv,'fetch_frames',side_effect=fetch):rv.worker(identifier,rv.selected_batches({}),payload)
            return identifier
        _,frames=fixture();run(lambda *_:frames)
        old=rv.symbol_detail(b['id'],'600001')['result']
        run(RuntimeError('offline'))
        detail=rv.symbol_detail(b['id'],'600001')
        self.assertEqual(old,detail['result'])
        self.assertEqual(detail['update_status'],'failed')

    def test_restart_interrupts_jobs_and_import_is_idempotent(self):
        b=batch();db.persist(b)
        with patch('threading.Thread.start'):job=rv.start({'batch_id':b['id']})
        with tempfile.TemporaryDirectory() as directory,patch.object(db,'ROOT',Path(directory)),patch.object(db,'OUTBOX',Path(directory)/'outbox'),patch.object(db,'migrate'):
            folder=Path(directory)/'data';folder.mkdir()
            (folder/'watchlist.json').write_text(json.dumps({'as_of':b['as_of'],'candidates':b['rows']}),encoding='utf-8')
            db.initialize();db.import_legacy()
            self.assertEqual(rv.batches({})['total'],2)
            self.assertEqual(rv.job_detail(job['id'])['status'],'interrupted')

    def test_http_query_validation_and_database_failure(self):
        import server,threading
        from urllib.request import urlopen
        from urllib.error import HTTPError
        b=batch();db.persist(b)
        app=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{app.server_port}'
        try:
            with patch.dict(os.environ,{'DASHBOARD_PASSWORD':''}):
                with urlopen(base+'/api/history/batches') as r:self.assertEqual(json.load(r)['total'],1)
                with self.assertRaises(HTTPError) as err:urlopen(base+'/api/history/batches?page=0')
                self.assertEqual(err.exception.code,400);err.exception.close()
                with patch.object(db,'query',side_effect=RuntimeError('secret')):
                    with self.assertRaises(HTTPError) as err:urlopen(base+'/api/history/batches')
                    self.assertEqual(err.exception.code,503)
                    self.assertNotIn('secret',err.exception.read().decode());err.exception.close()
        finally:app.shutdown();app.server_close();thread.join()

    @classmethod
    def tearDownClass(cls):
        db._pool.close();db._pool=None;cls.env.stop()

if __name__=='__main__':unittest.main()
