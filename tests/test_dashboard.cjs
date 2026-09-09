// Uses synthetic API fixtures; never changes the user's watchlist or triggers scans.
const { chromium } = require('playwright');
const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('assert');

(async () => {
  const app = http.createServer((req, res) => {
    if (req.url === '/static/history.js') {
      res.setHeader('Content-Type', 'text/javascript; charset=utf-8');
      res.end(fs.readFileSync(path.join(__dirname, '..', 'static', 'history.js'))); return;
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(fs.readFileSync(path.join(__dirname, '..', 'dashboard.html')));
  });
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({channel:'msedge', headless:true});
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors = []; const requests = [];
    page.on('pageerror', e => errors.push(e.message));
    const bars = Array.from({length:63}, (_, i) => ({date:`2026-09-07T${i}:00`, open:99,
      close:i < 60 ? 99 : 101, high:i < 60 ? 100 : 102, low:90, vol:100}));
    const frame = {state:'BREAK',ok:true,reason:'已确认突破',box_high:100,box_low:90,
      breakout_at:bars[60].date,confirmed_at:bars[62].date};
    let row = {code:'600519',name:'测试股票',qualified:true,score:100,price:101,chg:1,
      resonance_level:'strong',resonance_status:'已评估',timeframes:Object.fromEntries(['30m','1h','1d'].map(p=>[p,frame]))};
    let extra = []; const quoteRequests = [];
    await page.route('**/api/**', async route => {
      const u = new URL(route.request().url());
      let data = {};
      if (u.pathname === '/api/watchlist') data = {candidates:[...extra,row]};
      if (u.pathname === '/api/quotes') {
        const codes = u.searchParams.get('codes').split(','); quoteRequests.push(codes);
        data = Object.fromEntries(codes.map(code => [code, {price:102,chg:2}]));
      }
      if (u.pathname === '/api/crypto') data = {candidates:[]};
      if (u.pathname === '/api/hot') data = {hot_topics:[]};
      if (u.pathname === '/api/status') data = {scanning:false,scan_log:[]};
      if (u.pathname === '/api/kline') {
        requests.push(u.searchParams.get('interval'));
        data = {bars,box:frame,interval:u.searchParams.get('interval')};
      }
      await route.fulfill({json:data});
    });
    const url = `http://127.0.0.1:${app.address().port}`;
    await page.goto(url); await page.waitForLoadState('networkidle');
    assert((await page.locator('.resonance').innerText()).includes('共振 强'));
    await page.waitForFunction(()=>document.querySelector('[data-px]').textContent === '102.00');
    await page.waitForFunction(()=>document.querySelector('[data-chg]').textContent.includes('2.00'));
    // 快扫按钮：应发出 mode=quick 的扫描请求（快扫仅 A股）
    const scanBodies = [];
    await page.route('**/api/scan', async route => {
      scanBodies.push(route.request().postDataJSON());
      await route.fulfill({json: {}});
    });
    await page.click('#btnQuick');
    await page.waitForTimeout(150);
    assert.deepEqual(scanBodies, [{mode:'quick'}]);
    await Promise.all([
      page.waitForResponse(r => r.url().includes('interval=30m')),
      page.getByLabel('K线周期').selectOption('30m')
    ]);
    await page.waitForFunction(()=>document.querySelector('[data-confirm]').textContent.includes('确认至'));
    await Promise.all([
      page.waitForResponse(r => r.url().includes('interval=1h')),
      page.getByLabel('K线周期').selectOption('1h')
    ]);
    await page.waitForLoadState('networkidle');
    // 周期选择跨重渲染保留（回归：renderAll 重建卡片曾把 select 重置回 1d）
    await page.evaluate(() => renderAll());
    assert.equal(await page.locator('select[aria-label="K线周期"]').first().inputValue(), '1h');
    assert(requests.includes('30m') && requests.includes('1h') && requests.includes('1d'), JSON.stringify({requests,errors}));
    assert((await page.locator('[data-confirm]').innerText()).includes('突破：'));
    extra = Array.from({length:125}, (_, i) => ({...row, code:String(600100+i), score:50,
      qualified:false,resonance_level:'near'}));
    quoteRequests.length = 0;
    await page.reload(); await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('.sig').count(),60);
    assert(quoteRequests.some(codes => codes.includes('600519')), 'visible strong stock must refresh despite raw ordering');
    await page.getByRole('button',{name:'加载更多'}).click();
    assert.equal(await page.locator('.sig').count(),120);
    await page.getByRole('button',{name:'加载更多'}).click();
    assert.equal(await page.locator('.sig').count(),126);
    // 共振级别筛选 + 页内重渲染一致性（回归：#poolCount 残留写入曾使第二次 renderAll 抛错，
    // 表现为横幅与标题数字脱钩、筛选片点击无效）
    await page.click('.res-chip[data-res="near"]');
    assert.equal(await page.locator('.sig').count(),60);
    await page.click('.res-chip[data-res="strong"]');
    assert.equal(await page.locator('.sig').count(),1);
    await page.evaluate(() => renderAll());
    const banner = (await page.locator('#hpHit').innerText()).trim();
    const title = await page.locator('#cardsTitle').innerText();
    assert(banner === '1' && title.includes('达标 1'), JSON.stringify({banner,title}));
    extra = [];
    row = {...row,qualified:false,resonance_status:'数据不可用'};
    await page.reload(); await page.waitForLoadState('networkidle');
    assert((await page.locator('.empty').innerText()).includes('行情获取失败'));
    delete row.resonance_status;
    await page.reload(); await page.waitForLoadState('networkidle');
    assert((await page.locator('.empty').innerText()).includes('待重新扫描'));
    await page.getByRole('button',{name:'加密货币',exact:true}).click();
    assert(await page.locator('.empty').isVisible());
    assert(!(await page.locator('#btnQuick').isVisible()), '快扫按钮在币圈页应隐藏');
    assert.deepEqual(errors,[]);
    console.log('PASS dashboard: period switching, confirmations, visible quotes, pagination, errors, legacy data, crypto tab');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => app.close(resolve));
  }
})().catch(e => {console.error(e); process.exitCode=1;});
