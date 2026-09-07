import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
import threading
from urllib.request import urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from contextlib import ExitStack

import resonance as rs
import scanner as sc
import server


NOW = datetime(2026, 9, 7, 15, 0, tzinfo=rs.BJT)


def candles(closes=()):
    values = [99] * 60 + list(closes)
    return [dict(date=str(i), open=99, close=c, high=max(100, c), low=90, vol=100)
            for i, c in enumerate(values)]


def raw_days(count=65):
    raw = []
    day = NOW - timedelta(days=110)
    while len(raw) < count * 8:
        if day.weekday() < 5:
            for slot in rs.SLOTS:
                raw.append([day.strftime('%Y%m%d') + slot.replace(':', ''), '99', '99', '100', '90', '10'])
        day += timedelta(days=1)
    return raw


class RuleTests(unittest.TestCase):
    def test_recent_breakout_and_fixed_box(self):
        result = rs.evaluate(candles([101, 102, 103]))
        self.assertTrue(result['ok'])
        self.assertEqual(result['box_high'], 100)
        self.assertEqual(result['breakout_at'], '60')

    def test_intrabar_touch_is_not_breakout(self):
        bars = candles([99, 99, 99]); bars[-1]['high'] = 110
        self.assertFalse(rs.evaluate(bars)['ok'])

    def test_exact_threshold_is_not_breakout(self):
        self.assertFalse(rs.evaluate(candles([99, 99, 100.5]))['ok'])

    def test_return_to_top_invalidates(self):
        result = rs.evaluate(candles([101, 100, 103]))
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], '突破后跌回箱顶')

    def test_expired(self):
        self.assertFalse(rs.evaluate(candles([101, 101, 101, 101]))['ok'])

    def test_short_and_flat(self):
        self.assertEqual(rs.evaluate(candles([101]))['reason'], '历史不足')
        bars = [dict(date=str(i), open=100, close=100, high=100, low=100, vol=1) for i in range(63)]
        self.assertFalse(rs.evaluate(bars)['ok'])

    def test_near_state_when_pressing_the_top(self):
        # 贴近上沿未突破：距箱顶 -0.5%、箱内位置 95% → NEAR 而非直接出局
        result = rs.evaluate(candles([99, 99.5, 99.5]))
        self.assertEqual(result['state'], 'NEAR')
        self.assertFalse(result['ok'])
        self.assertEqual(result['dist_pct'], -0.5)

    def test_standing_above_box_is_break(self):
        # 首次上穿滑出窗口后持续站稳（近3根低点不破老箱体）→ BREAK
        bars = [dict(date=str(i), open=99, close=99, high=100, low=90, vol=1) for i in range(60)]
        for j, c in enumerate((103, 104, 105, 106)):
            bars.append(dict(date=str(60 + j), open=c, close=c, high=c * 1.005,
                             low=c * 0.998, vol=1))
        result = rs.evaluate(bars)
        self.assertEqual(result['state'], 'BREAK')
        self.assertEqual(result['reason'], '站稳箱顶')
        # 比较窗口混入首根高位K（突破已久之常态），箱顶为站稳平台的高点
        self.assertAlmostEqual(result['box_high'], 103.515, places=2)

    def test_period_specific_recent_window(self):
        # 1d 用宽窗口（RECENT['1d']=10）：10 根内的突破仍算 BREAK，14 根前的不算
        closes = [99] * 49 + [101] + [99] * 14      # 突破发生在倒数第 15 根
        self.assertNotEqual(rs.evaluate(candles(closes), rs.RECENT['1d'])['state'], 'BREAK')
        closes = [99] * 53 + [101] + [99] * 9       # 突破发生在倒数第 10 根
        result = rs.evaluate(candles(closes), rs.RECENT['1d'])
        self.assertNotEqual(result['state'], 'BREAK')  # 突破后跌回，非维持
        closes = [99] * 53 + [101] + [101.5] * 9
        self.assertEqual(rs.evaluate(candles(closes), rs.RECENT['1d'])['state'], 'BREAK')


class AggregationTests(unittest.TestCase):
    def test_session_aggregation(self):
        raw = raw_days()
        frames = rs.aggregate(raw, NOW)
        self.assertEqual([len(frames[p]) for p in rs.PERIODS], [520, 260, 65])
        self.assertEqual(frames['1h'][1]['date'][11:16], '11:30')
        self.assertEqual(frames['1h'][2]['date'][11:16], '14:00')
        self.assertEqual(frames['1d'][0]['vol'], 80)

    def test_boundary_and_incomplete_day(self):
        raw = [[NOW.strftime('%Y%m%d') + t.replace(':',''),99,99,100,90,10] for t in rs.SLOTS]
        frames = rs.aggregate(raw, NOW.replace(hour=11, minute=29))
        self.assertEqual([len(frames[p]) for p in rs.PERIODS], [3,1,0])
        frames = rs.aggregate(raw, NOW.replace(hour=11, minute=30))
        self.assertEqual([len(frames[p]) for p in rs.PERIODS], [4,2,0])

    def test_missing_duplicate_and_invalid(self):
        raw = raw_days()
        variants = [raw[:10]+raw[11:], raw+[raw[10]]]
        invalid = copy.deepcopy(raw); invalid[10][3] = 'NaN'; variants.append(invalid)
        for data in variants:
            with self.subTest(data=len(data)), self.assertRaises(ValueError):
                rs.aggregate(data, NOW)

    def test_no_future_leakage(self):
        raw = raw_days()
        cutoff = datetime.strptime(raw[-4][0], '%Y%m%d%H%M').replace(tzinfo=rs.BJT)
        before = rs.snapshot(raw, cutoff)
        for k in raw[-3:]: k[2:4] = [100000,100000]
        self.assertEqual(before, rs.snapshot(raw, cutoff))


class IntegrationTests(unittest.TestCase):
    def frames(self, states=None):
        base = dict(rs.evaluate(candles([101, 102, 103])), bars=candles([101, 102, 103]))
        return {p: (dict(base, state=states[p]) if states else dict(base)) for p in rs.PERIODS}

    def test_resonance_grading(self):
        # 强共振不再被评分一票否决；准共振需评分≥70；临界池只跟踪不达标
        frames = self.frames()   # 三周期 BREAK
        for score in (50, 84, 100):
            row = rs.qualify(dict(score=score, timeframes=frames, resonance_status='已评估'))
            self.assertTrue(row['qualified'], score)
            self.assertEqual(row['mode'], '强共振达标')
        frames = self.frames(states={'30m': 'BREAK', '1h': 'INSIDE', '1d': 'NEAR'})
        row = rs.qualify(dict(score=75, timeframes=frames, resonance_status='已评估'))
        self.assertEqual(row['resonance_level'], 'soft')
        self.assertTrue(row['qualified'])
        row = rs.qualify(dict(score=50, timeframes=frames, resonance_status='已评估'))
        self.assertFalse(row['qualified'])
        self.assertEqual(row['mode'], '准共振·评分不足')
        frames = self.frames(states={'30m': 'INSIDE', '1h': 'INSIDE', '1d': 'NEAR'})
        row = rs.qualify(dict(score=90, timeframes=frames, resonance_status='已评估'))
        self.assertEqual(row['resonance_level'], 'near')
        self.assertFalse(row['qualified'])
        self.assertEqual(row['mode'], '临界跟踪')

    def test_stale_breakout_downgrades_from_strong(self):
        # 三周期 BREAK 但 1d 距箱顶 15%（突破已久）→ 趋势延续，降为准共振
        frames = self.frames()
        frames['1d']['dist_pct'] = 15.0
        row = rs.qualify(dict(score=80, timeframes=frames, resonance_status='已评估'))
        self.assertEqual(row['resonance_level'], 'soft')
        self.assertTrue(row['qualified'])

    def test_etf_score_blends_resonance(self):
        frames = self.frames(states={'30m': 'BREAK', '1h': 'BREAK', '1d': 'BREAK'})
        row = rs.qualify(dict(market='etf', base_score=35, score=35, timeframes=frames,
                              resonance_status='已评估'))
        self.assertEqual(row['score'], 85)      # 35 + strong 50
        self.assertTrue(row['qualified'])
        self.assertEqual(rs.qualify(row)['score'], 85)   # 重复调用幂等
        frames = self.frames(states={'30m': 'INSIDE', '1h': 'INSIDE', '1d': 'BELOW'})
        row = rs.qualify(dict(market='etf', base_score=50, score=50, timeframes=frames,
                              resonance_status='已评估'))
        self.assertEqual(row['score'], 50)      # none 不加分

    def test_old_result_and_crypto_unchanged(self):
        old = rs.qualify(dict(score=100, qualified=True))
        self.assertFalse(old['qualified']); self.assertEqual(old['mode'],'待重新扫描')
        crypto = dict(market='crypto', score=100, qualified=True)
        self.assertEqual(rs.qualify(crypto),crypto)
        crypto = sc.score_row(dict(market='crypto',volume_days=3,volume_ratio=2,tests=3,chg=12))
        self.assertTrue(crypto['qualified']); self.assertEqual(crypto['score'],100)

    def test_every_row_gets_resonance(self):
        # 共振是主筛：低分行也要评估（不再按 base_qualified 跳过）
        with patch.object(sc,'fetch_stock_timeframes',side_effect=RuntimeError('offline')) as fetch:
            result = sc.apply_resonance(dict(code='600519',score=40,base_qualified=False),NOW)
            self.assertEqual(fetch.call_count, 1)
            self.assertFalse(result['qualified'])
            self.assertEqual(result['resonance_status'],'数据不可用')

    def test_chart_snapshot_and_period_isolation(self):
        frames = self.frames(); frames['30m']['bars'][0]['close'] = 98
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp)/'watch.json'
            file.write_text(json.dumps(dict(candidates=[dict(code='600519',timeframes=frames)])),encoding='utf-8')
            with patch.object(server,'WATCH_FILE',file), patch.object(sc,'fetch_stock_timeframes') as fetch:
                for period in rs.PERIODS:
                    chart = server.get_kline('600519',interval=period)
                    self.assertEqual(chart['bars'],frames[period]['bars'])
                    self.assertEqual(chart['box']['box_high'],frames[period]['box_high'])
                fetch.assert_not_called()
                self.assertIn('error',server.get_kline('000001'))
                self.assertIn('error',server.get_kline('600519',interval='4h'))

    def test_fetch_retries_are_bounded(self):
        with patch.object(sc,'http_json',side_effect=RuntimeError('offline')) as fetch:
            with self.assertRaises(RuntimeError): sc.fetch_stock_timeframes('600519',NOW)
            self.assertEqual(fetch.call_count,2)

    def test_scan_entrypoints_share_cutoff(self):
        stock = dict(code='600519', name='test', theme='', vr=2, turnover=1)
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            stack.enter_context(patch.object(sc, 'DATA', Path(temp)))
            stack.enter_context(patch.object(sc, 'WATCH_FILE', Path(temp)/'watch.json'))
            stack.enter_context(patch.object(sc, 'load_pool', return_value=[stock,dict(stock,code='000001')]))
            stack.enter_context(patch.object(sc, 'fetch_universe', return_value=[stock,dict(stock,code='000001')]))
            stack.enter_context(patch.object(sc, 'screen_universe', side_effect=lambda stocks,*_,**__: stocks))
            stack.enter_context(patch.object(sc, 'fetch_etf_universe', return_value=[]))
            stack.enter_context(patch.object(sc, 'fetch_hot_topics', return_value=([],set())))
            stack.enter_context(patch.object(sc, '_mkt_cache_save'))
            analyze = stack.enter_context(patch.object(sc,'analyze',return_value=dict(score=0,qualified=False)))
            market = stack.enter_context(patch.object(sc,'analyze_market',return_value=dict(score=0,qualified=False)))
            sc.run_scan()
            calls = analyze.call_args_list
            self.assertEqual(calls[0].args[-1],calls[1].args[-1])
            for full in (True,False):
                market.reset_mock(); sc.run_market_scan(full=full,workers=1)
                calls = market.call_args_list
                self.assertEqual(len(calls),2)
                self.assertEqual(calls[0].args[-1],calls[1].args[-1])

    def test_stock_analysis_reaches_gate(self):
        quote = dict(price=101,chg=1,turnover=1,volume_ratio=2,name='test')
        with ExitStack() as stack:
            stack.enter_context(patch.object(sc,'fetch_quote',return_value=quote))
            stack.enter_context(patch.object(sc,'fetch_kline',return_value=candles([101,102,103])))
            stack.enter_context(patch.object(sc,'fetch_fund_flow',return_value=[]))
            for name in ('fetch_holder','_cached_holder'):
                stack.enter_context(patch.object(sc,name,return_value=None))
            for name in ('fetch_concepts','_cached_concepts'):
                stack.enter_context(patch.object(sc,name,return_value=[]))
            gate = stack.enter_context(patch.object(sc,'apply_resonance',side_effect=lambda row,cutoff,**kw: row))
            sc.analyze('600519','test','',set(),NOW)
            sc.analyze_market(dict(code='600519',name='test',price=101,chg=1,turnover=1,vr=2),set(),NOW)
            self.assertEqual(gate.call_count,2)
            self.assertTrue(all(c.args[1] == NOW for c in gate.call_args_list))

    def test_stale_same_day_feed_cannot_confirm(self):
        raw = [[NOW.strftime('%Y%m%d') + t.replace(':',''),99,99,100,90,10] for t in rs.SLOTS[:6]]
        quote = [''] * 31; quote[30] = '20260907140100'
        payload = dict(data={'sh600519':dict(m30=raw,qt={'sh600519':quote})})
        with patch.object(sc,'http_json',return_value=payload):
            with self.assertRaisesRegex(RuntimeError,'分钟行情不可用'):
                sc.fetch_stock_timeframes('600519',NOW)

    def test_http_legacy_and_interval_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp)/'watch.json'
            file.write_text(json.dumps(dict(candidates=[dict(code='600519',score=100,qualified=True)])),encoding='utf-8')
            with patch.object(server,'WATCH_FILE',file):
                app = server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
                worker = threading.Thread(target=app.serve_forever,daemon=True); worker.start()
                try:
                    root = f'http://127.0.0.1:{app.server_port}'
                    with urlopen(root+'/api/watchlist') as response:
                        self.assertFalse(json.load(response)['candidates'][0]['qualified'])
                    with self.assertRaises(HTTPError) as error:
                        urlopen(root+'/api/kline?code=600519&interval=4h')
                    self.assertEqual(error.exception.code,400)
                    error.exception.close()
                finally:
                    app.shutdown(); app.server_close(); worker.join()


if __name__ == '__main__':
    unittest.main()
