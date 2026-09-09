"""审查问题的故障注入与跨模块回归；不访问外网，不写真实data目录。"""
import ast
import copy
from datetime import datetime, timedelta
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

import em
import market_data as md
import notifications as nt
import resonance as rs
import scanner as sc
import server
import ths

NOW = datetime(2026, 9, 8, 10, 15, tzinfo=rs.BJT)


def days():
    out = []
    day = NOW - timedelta(days=120)
    while day.date() <= NOW.date():
        if day.weekday() < 5:
            out.append(dict(date=day.strftime('%Y-%m-%d'), open=99, close=99, high=100, low=90, vol=100))
        day += timedelta(days=1)
    return md.Bars(out, source='tencent', adjustment='qfq')


def minute_payload():
    raw = []
    for b in days():
        for slot in rs.SLOTS:
            stamp = b['date'].replace('-', '') + slot.replace(':', '')
            if datetime.strptime(stamp, '%Y%m%d%H%M').replace(tzinfo=rs.BJT) <= NOW:
                raw.append([stamp, 99, 99, 100, 90, 10])
    q = [''] * 31
    q[30] = '20260908101500'
    return {'data': {'sh600519': {'m30': raw[-640:], 'qt': {'sh600519': q}}}}


class DailyTests(unittest.TestCase):
    def test_unclosed_daily_cannot_confirm_or_change_snapshot(self):
        bars = days()
        bars[-1].update(close=102, high=102)
        with patch.object(sc, 'http_json', return_value=minute_payload()):
            first = sc.fetch_stock_timeframes('600519', NOW, bars)
            bars[-1].update(close=150, high=150)
            second = sc.fetch_stock_timeframes('600519', NOW, bars)
        self.assertEqual(first, second)
        self.assertEqual(first['1d']['confirmed_at'], '2026-09-07')
        self.assertNotEqual(first['1d']['state'], 'BREAK')
        self.assertEqual(first['1d']['adjustment'], 'qfq')

    def test_close_boundary_future_duplicates_and_invalid(self):
        bars = days()
        self.assertEqual(md.daily(bars, NOW.replace(hour=15))[-1]['date'], '2026-09-08')
        for bad in (bars + [bars[0]], [dict(bars[0], high=float('nan'))], [dict(bars[0], low=101)]):
            with self.assertRaises(ValueError):
                md.daily(bad, NOW)

    def test_stale_daily_or_wrong_adjustment_fails_closed(self):
        with patch.object(sc, 'http_json', return_value=minute_payload()):
            with self.assertRaisesRegex(RuntimeError, '日线缺失'):
                sc.fetch_stock_timeframes('600519', NOW, days()[:-2])
            with self.assertRaisesRegex(RuntimeError, '不复权'):
                sc.fetch_stock_timeframes('600519', NOW, md.Bars(days(), 'ths', 'unadjusted'))

    def test_legacy_snapshot_is_not_qualified(self):
        row = dict(code='600519', score=100, resonance_as_of=NOW.isoformat(),
                   timeframes={p: {'state': 'BREAK', 'dist_pct': 1} for p in rs.PERIODS})
        self.assertFalse(rs.qualify(row)['qualified'])

    def test_exdiv_drift_blocks_confirmation(self):
        # 分钟窗口内除权：qfq 历史被下修 → 因子漂移超阈值 → 拒绝共振确认（fail-closed）
        bars = days()
        for b in bars[:-10]:
            for key in ('open', 'close', 'high', 'low'):
                b[key] *= 0.9
        with patch.object(sc, 'http_json', return_value=minute_payload()):
            with self.assertRaisesRegex(RuntimeError, '除权'):
                sc.fetch_stock_timeframes('600519', NOW, md.Bars(bars, 'tencent', 'qfq'))

    def test_factor_drift_window_and_direction(self):
        base = [dict(date=f'2026-08-{i:02d}', open=99, close=99, high=100, low=90, vol=1)
                for i in range(1, 29)]
        adjusted = [dict(b, **{k: b[k] * 0.9 for k in ('open', 'close', 'high', 'low')})
                    for b in base[:-5]] + [dict(base[-5:][i]) for i in range(5)]
        drift, ok = md.factor_drift(base, md.Bars(adjusted, 'tencent', 'qfq'), window=20)
        self.assertTrue(ok)
        self.assertGreater(drift, 0.003)
        drift, ok = md.factor_drift(base, md.Bars(list(base), 'tencent', 'qfq'), window=20)
        self.assertTrue(ok)
        self.assertAlmostEqual(drift, 0.0, places=6)
        drift, ok = md.factor_drift(base[:2], md.Bars(list(base[:2]), 'tencent', 'qfq'))
        self.assertFalse(ok)   # 重叠交易日不足 → 无法判定而非误报


class ProviderTests(unittest.TestCase):
    def test_quote_timeout_and_malformed_response_reach_ths(self):
        fallback = dict(price=10, chg=1, name='test', turnover=0, volume_ratio=0)
        for response in (sc.requests.Timeout(), Mock(content=b'bad', raise_for_status=Mock())):
            with patch.object(em, 'get_json', side_effect=RuntimeError()), \
                 patch.object(sc.HTTP, 'get', **({'side_effect': response} if isinstance(response, Exception) else {'return_value': response})), \
                 patch.object(ths, 'fetch_quote', return_value=fallback) as fetch:
                self.assertEqual(sc.fetch_quote('600519'), fallback)
                fetch.assert_called_once()

    def test_cooldown_suppresses_network_until_expiry(self):
        with patch.dict(em._cooldown, {'test': 200}, clear=True), patch.object(em.time, 'time', return_value=100), patch.object(em.HTTP, 'get') as get:
            with self.assertRaises(RuntimeError):
                em.get_json(['test'], '/x')
            get.assert_not_called()
        with patch.dict(em._cooldown, {'test': 200}, clear=True), patch.object(em.time, 'time', return_value=201):
            self.assertEqual(em._pick(['test']), ['test'])

    def test_ths_today_replaces_year_value_once_even_with_previous_year(self):
        def response(url, **kwargs):
            if 'today.js' in url:
                data = {'hs_600519': {'1': '20260908', '7': 99, '8': 102, '9': 99, '11': 102, '13': 100}}
                marker = 'hs_600519_01_today'
            else:
                year = int(url.split('/')[-1][:-3])
                marker = 'hs_600519_01_' + str(year)
                dates = ['20260908'] if year == datetime.now().year else ['202512%02d' % i for i in range(1, 31)]
                data = {'data': ';'.join(d + ',99,100,90,99,100' for d in dates)}
            return Mock(text=marker + '(' + json.dumps(data) + ')', raise_for_status=Mock())
        with patch.object(ths.HTTP, 'get', side_effect=response):
            bars = ths.fetch_kline('600519', 160)
        self.assertEqual([b['close'] for b in bars if b['date'] == '2026-09-08'], [102])

    def test_cached_universe_refreshes_dynamic_fields(self):
        p = ['0'] * 50
        p[2], p[3], p[30], p[32], p[38], p[49] = '600519', '102', '20260908101500', '2', '3', '1.8'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'universe.json'
            path.write_text(json.dumps(dict(as_of=sc.now_str()[:10], stocks=[dict(code='600519', price=99, chg=0, turnover=0, vr=0)])))
            with patch.object(sc, 'UNIVERSE_FILE', path), patch.object(sc.HTTP, 'get', return_value=Mock(content=('~'.join(p)+';').encode(), raise_for_status=Mock())):
                self.assertEqual(sc.fetch_universe()[0]['price'], 102)
            with patch.object(sc, 'UNIVERSE_FILE', path), patch.object(sc.HTTP, 'get', side_effect=sc.requests.Timeout()):
                self.assertIsNone(sc.fetch_universe()[0]['price'])

    def test_empty_concepts_do_not_poison_cache(self):
        with patch.object(sc, '_mkt_cache', {}), patch.object(sc, 'fetch_concepts', side_effect=[[], ['科技']]), patch.object(sc, '_mkt_cache_save'):
            self.assertEqual(sc._cached_concepts('600519'), [])
            self.assertEqual(sc._cached_concepts('600519'), ['科技'])

    def test_pool_bypass_and_deep_calc_survive_quote_outage(self):
        # P1回归：腾讯批量行情失败置 price/chg=None 时，自选池保送不被击穿，
        # 且缺行情字段的股票能走完深算（不因 None 字段抛错丢行）
        stock = dict(code='600519', name='pool', price=None, chg=None, turnover=0.0, vr=0.0,
                     quote_source='unavailable')
        active = dict(code='000001', name='act', price=10.0, chg=5.0, turnover=1.0, vr=2.0)
        picked = sc.screen_universe([stock, active], top=1, pool_codes={'600519'})
        self.assertTrue(any(s['code'] == '600519' for s in picked))
        with patch.object(sc, 'fetch_kline', return_value=days()), \
             patch.object(sc, 'fetch_fund_flow', return_value=[]), \
             patch.object(sc, '_cached_holder', return_value=None), \
             patch.object(sc, '_cached_concepts', return_value=[]), \
             patch.object(sc, 'fetch_stock_timeframes', side_effect=RuntimeError('offline')):
            row = sc.analyze_market(stock, set(), NOW)
        self.assertIsNotNone(row)
        self.assertIsNone(row['price'])
        self.assertEqual(row['resonance_status'], '数据不可用')


class ResponseTests(unittest.TestCase):
    """看板响应瘦身：列表剥K线数组、大JSON按Accept-Encoding gzip。"""

    def watch_file(self, tmp):
        bars = [dict(date=str(i), open=99, close=99, high=100, low=90, vol=10) for i in range(60)]
        frames = {p: dict(state='BREAK', box_high=100, breakout_at='x', confirmed_at='y', bars=bars)
                  for p in rs.PERIODS}
        row = dict(code='600519', score=100, resonance_as_of='t', resonance_version=2,
                   resonance_status='已评估', timeframes=frames)
        file = Path(tmp) / 'watch.json'
        file.write_text(json.dumps(dict(candidates=[row], padding='x' * 20000)), encoding='utf-8')
        return file

    def test_watchlist_strips_bars_and_gzips(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'WATCH_FILE', self.watch_file(tmp)):
            app = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            worker = threading.Thread(target=app.serve_forever, daemon=True)
            worker.start()
            try:
                root = f'http://127.0.0.1:{app.server_port}'
                # 不带 Accept-Encoding：明文返回，candidates 已不含 bars
                with urlopen(root + '/api/watchlist') as resp:
                    self.assertIsNone(resp.headers.get('Content-Encoding'))
                    body = json.loads(resp.read())
                candidate = body['candidates'][0]
                self.assertEqual(candidate['code'], '600519')
                self.assertEqual(candidate['resonance_level'], 'strong')
                for frame in candidate['timeframes'].values():
                    self.assertNotIn('bars', frame)
                    self.assertEqual(frame['state'], 'BREAK')
                # 带 gzip：响应被压缩且可解压还原
                req = Request(root + '/api/watchlist', headers={'Accept-Encoding': 'gzip'})
                with urlopen(req) as resp:
                    self.assertEqual(resp.headers.get('Content-Encoding'), 'gzip')
                    restored = gzip.decompress(resp.read())
                self.assertEqual(json.loads(restored)['candidates'][0]['code'], '600519')
            finally:
                app.shutdown(); app.server_close(); worker.join()

    def test_watch_payload_cached_until_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = self.watch_file(tmp)
            with patch.object(server, 'WATCH_FILE', file), patch.object(server, '_WATCH_CACHE', [None]):
                p1 = server._watch_payload()
                with patch.object(server, 'read_json') as rj:   # 同 stat 命中缓存，不再读盘解析
                    self.assertIs(server._watch_payload(), p1)
                    rj.assert_not_called()
                file.write_text(json.dumps(dict(candidates=[dict(code='000001')], pad='y' * 33)), encoding='utf-8')
                p2 = server._watch_payload()
                self.assertIsNot(p1, p2)
                self.assertEqual(p2['candidates'][0]['code'], '000001')

    def test_watchlist_handler_keeps_kline_cache_intact(self):
        # /api/watchlist 会替换 candidates 键；必须浅拷贝保护共享缓存，
        # 否则随后的 /api/kline 拿不到 bars，所有图表变空白
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'WATCH_FILE', self.watch_file(tmp)), \
             patch.object(server, '_WATCH_CACHE', [None]):
            app = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            worker = threading.Thread(target=app.serve_forever, daemon=True)
            worker.start()
            try:
                root = f'http://127.0.0.1:{app.server_port}'
                with urlopen(root + '/api/watchlist') as resp:
                    self.assertEqual(json.loads(resp.read())['candidates'][0]['code'], '600519')
                with urlopen(root + '/api/kline?code=600519&interval=1d&market=stock&lmt=100') as resp:
                    body = json.loads(resp.read())
                self.assertIsNone(body.get('error'))
                self.assertEqual(len(body.get('bars') or []), 60)
            finally:
                app.shutdown(); app.server_close(); worker.join()


class RuleParityTests(unittest.TestCase):
    def test_second_breakout_after_failure(self):
        bars = list(days())[:-3]
        for c in (101, 99, 102):
            bars.append(dict(date=str(len(bars)), open=99, close=c, high=max(100,c), low=90, vol=1))
        result = rs.evaluate(bars, 10)
        self.assertEqual(result['state'], 'BREAK')
        self.assertEqual(result['breakout_at'], bars[-1]['date'])
        self.assertEqual(result['box_high'], 101)

    def test_joinquant_uses_identical_pure_functions(self):
        root = Path(sc.__file__).parent
        def sources(path):
            text = path.read_text(encoding='utf-8')
            return {n.name: ast.get_source_segment(text, n) for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)}
        live, backtest = sources(root/'resonance.py'), sources(root/'joinquant_strategy.py')
        for name in ('aggregate', 'evaluate', '_level'):
            self.assertEqual(live[name], backtest[name])
        self.assertNotIn('prev_top', (root/'joinquant_strategy.py').read_text(encoding='utf-8'))


class NotificationTests(unittest.TestCase):
    def row(self):
        return dict(code='600519', name='test', qualified=True, resonance_level='strong',
                    resonance_status='已评估', timeframes={'30m': dict(state='BREAK', box_high=100, breakout_at='a', confirmed_at='b')})

    def test_dedupe_failure_retry_and_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'
            row = self.row()
            send = Mock(return_value=False)
            self.assertFalse(nt.dispatch([row], path, 'market', send, lambda _: 'test'))
            self.assertFalse(path.exists())
            send.return_value = True
            self.assertTrue(nt.dispatch([row], path, 'market', send, lambda _: 'test'))
            row['timeframes']['30m']['confirmed_at'] = 'later'
            nt.dispatch([row], path, 'market', send, lambda _: 'test')
            self.assertEqual(send.call_count, 2)
            nt.dispatch([dict(row, qualified=False, resonance_status='数据不可用')], path, 'market', send, str)
            self.assertEqual(send.call_count, 2)
            nt.dispatch([dict(row, qualified=False)], path, 'market', send, str)
            self.assertEqual(send.call_count, 3)

    def test_long_telegram_is_chunked_without_real_network(self):
        with patch.object(sc, 'telegram_credentials', return_value=('fake', 'fake')), patch.object(sc.HTTP, 'post', return_value=Mock(ok=True, json=lambda: {'ok':True})) as post:
            self.assertTrue(sc.telegram_send('😀中' * 3000))
            sent = [c.kwargs['json']['text'] for c in post.call_args_list]
            self.assertEqual(''.join(sent), '😀中' * 3000)
            self.assertTrue(all(len(t.encode('utf-16-le'))//2 <= 3500 for t in sent))

    def test_feishu_signed_body_and_success(self):
        with patch.object(sc, 'feishu_credentials', return_value=('https://open.feishu.cn/open-apis/bot/v2/hook/fake', 's3cret')), \
             patch.object(sc.HTTP, 'post', return_value=Mock(ok=True, json=lambda: {'code': 0, 'msg': 'success'})) as post:
            self.assertTrue(sc.feishu_send('hello'))
            body = post.call_args.kwargs['json']
            self.assertEqual(body['msg_type'], 'text')
            self.assertEqual(body['content']['text'], 'hello')
            self.assertIn('timestamp', body)
            self.assertEqual(body['sign'], sc._feishu_sign(body['timestamp'], 's3cret'))
            self.assertEqual(len(body['sign']), 44)  # base64(32字节SHA256摘要)

    def test_feishu_unsigned_when_no_secret(self):
        with patch.object(sc, 'feishu_credentials', return_value=('https://open.feishu.cn/open-apis/bot/v2/hook/fake', '')), \
             patch.object(sc.HTTP, 'post', return_value=Mock(ok=True, json=lambda: {'code': 0})) as post:
            self.assertTrue(sc.feishu_send('hi'))
            body = post.call_args.kwargs['json']
            self.assertNotIn('sign', body)
            self.assertNotIn('timestamp', body)

    def test_feishu_error_body_returns_false(self):
        # 飞书签名错误等仍返回 HTTP 200，必须看响应体
        with patch.object(sc, 'feishu_credentials', return_value=('https://open.feishu.cn/open-apis/bot/v2/hook/fake', 'bad')), \
             patch.object(sc.HTTP, 'post', return_value=Mock(ok=True, json=lambda: {'code': 19021, 'msg': 'sign match fail'})):
            self.assertFalse(sc.feishu_send('hi'))

    def test_feishu_legacy_status_code_zero(self):
        resp = Mock(ok=True, json=lambda: {'StatusCode': 0, 'StatusMessage': 'success'})
        with patch.object(sc, 'feishu_credentials', return_value=('https://open.feishu.cn/open-apis/bot/v2/hook/fake', '')), \
             patch.object(sc.HTTP, 'post', return_value=resp):
            self.assertTrue(sc.feishu_send('hi'))

    def test_long_feishu_text_is_chunked_without_real_network(self):
        with patch.object(sc, 'feishu_credentials', return_value=('https://open.feishu.cn/open-apis/bot/v2/hook/fake', '')), \
             patch.object(sc.HTTP, 'post', return_value=Mock(ok=True, json=lambda: {'code': 0})) as post:
            self.assertTrue(sc.feishu_send('😀中' * 3000))
            sent = [c.kwargs['json']['content']['text'] for c in post.call_args_list]
            self.assertEqual(''.join(sent), '😀中' * 3000)
            self.assertTrue(all(len(t.encode('utf-16-le'))//2 <= 3500 for t in sent))

    def qualified_row(self):
        return dict(code='600519', name='test', score=90,
                    timeframes={p: dict(state='BREAK', box_high=100, breakout_at='a', confirmed_at='b')
                                for p in ('30m', '1h', '1d')})

    def test_push_scan_dual_channel_independent_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sc, 'DATA', Path(tmp)), \
                 patch.object(sc, 'telegram_credentials', return_value=('tk', 'chat')), \
                 patch.object(sc, 'feishu_credentials', return_value=('https://open.feishu.cn/open-apis/bot/v2/hook/fake', '')), \
                 patch.object(sc, 'telegram_send', return_value=True) as tg, \
                 patch.object(sc, 'feishu_send', return_value=True) as fs:
                self.assertTrue(sc.push_scan([self.qualified_row()], 'market'))
                self.assertTrue(tg.called and fs.called)
                self.assertTrue((Path(tmp) / 'telegram_state.json').exists())
                self.assertTrue((Path(tmp) / 'feishu_state.json').exists())
                # 单渠道失败不拖累另一渠道；失败渠道状态不落盘（下次重试）
                (Path(tmp) / 'feishu_state.json').unlink()
                fs.return_value = False
                self.assertFalse(sc.push_scan([self.qualified_row()], 'market'))
                self.assertFalse((Path(tmp) / 'feishu_state.json').exists())
                self.assertTrue((Path(tmp) / 'telegram_state.json').exists())

    def test_push_scan_skips_unconfigured_channels(self):
        with patch.object(sc, 'telegram_credentials', return_value=('', '')), \
             patch.object(sc, 'feishu_credentials', return_value=('', '')), \
             patch.object(sc, 'telegram_send') as tg, patch.object(sc, 'feishu_send') as fs:
            self.assertTrue(sc.push_scan([self.qualified_row()], 'market'))
            tg.assert_not_called()
            fs.assert_not_called()

    def test_scan_worker_calls_push(self):
        with patch.object(sc, 'run_market_scan', return_value=[]), patch.object(sc, 'push_scan', return_value=True) as push, patch.object(server, 'log'):
            server.scan_worker('market')
            push.assert_called_once_with([], 'market')


if __name__ == '__main__':
    unittest.main()
