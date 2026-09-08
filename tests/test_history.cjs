// Synthetic data only. No scan, database mutation, market request or notification.
const {chromium}=require('playwright');
const http=require('http'),fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
  const app=http.createServer((req,res)=>{
    const js=req.url==='/static/history.js';
    res.setHeader('Content-Type',js?'text/javascript; charset=utf-8':'text/html; charset=utf-8');
    res.end(fs.readFileSync(path.join(__dirname,'..',js?'static/history.js':'dashboard.html')));
  });
  await new Promise(r=>app.listen(0,'127.0.0.1',r));let browser;
  try{
    browser=await chromium.launch({channel:'msedge',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],calls=[];
    page.on('pageerror',e=>errors.push(e.message));
    const id='00000000-0000-4000-8000-000000000001';
    const bars=Array.from({length:65},(_,i)=>({date:`2026-${i<31?'07':'08'}-${String(i%28+1).padStart(2,'0')}`,open:100,close:100+i/20,high:105,low:98,vol:200}));
    const frame={box_high:102,box_low:98,state:'NEAR',bars,adjustment:'qfq'};
    const snapshot={code:'600001',name:'历史测试股票',price:101,score:90,qualified:true,resonance_level:'soft',as_of_quote:'2026-09-04T15:00:00+08:00',flags:['倍量确认'],timeframes:{'1d':frame,'30m':frame,'1h':frame}};
    const value={target_date:'2026-09-07',close:103,return_pct:1.980198,breakout:true,held:null,touched:true,errors:{held:'原状态非BREAK'}};
    const result={as_of:'2026-09-07T16:00:00+08:00',windows:Object.fromEntries(['1d','30m','1h'].map(p=>[p,{'1':value,'3':value,'5':value}])),latest:{'1d':value}};
    const batch={id,as_of:'2026-09-04T15:00:00+08:00',mode:'market',status:'complete',count:1,version:'v-test'};
    let updated=false;
    await page.route('**/api/**',async route=>{
      const u=new URL(route.request().url());calls.push({path:u.pathname,method:route.request().method()});let data={};
      if(u.pathname==='/api/watchlist'||u.pathname==='/api/crypto')data={candidates:[]};
      if(u.pathname==='/api/status')data={scanning:false};
      if(u.pathname==='/api/history/batches')data={items:[batch],total:1,archive:{pending:0,status:'ready'}};
      if(u.pathname===`/api/history/batches/${id}`)data={batch,items:[{code:'600001',snapshot,result,updated_at:result.as_of,update_error:updated?'分钟历史不足':null}],total:1};
      if(u.pathname.includes('/symbols/'))data={snapshot,result,as_of:batch.as_of,observations:{'1d':{bars:[{...bars[0],date:'2026-09-07',close:103}]}}};
      if(u.pathname==='/api/reviews'){updated=true;data={id,status:'running'};}
      if(u.pathname===`/api/reviews/${id}`)data={status:'partial',payload:{done:1,total:1,errors:[{code:'600001',error:'分钟历史不足'}]}};
      if(u.pathname==='/api/review-stats')data={batches:1,missing_dates:[],note:'每日信号样本，非独立交易次数。',groups:[{asset:'stock',version:'v-test',horizon:1,mean_return:1.98,flat:0,metrics:Object.fromEntries(['up','breakout','held'].map(k=>[k,{rate:k==='held'?null:100,success:k==='held'?0:1,valid:k==='held'?0:1,excluded:{}}])),samples:[{batch_id:id,code:'600001',name:snapshot.name,trade_date:'2026-09-04',performance:value}]}]};
      await route.fulfill({json:data});
    });
    await page.goto(`http://127.0.0.1:${app.address().port}`);await page.waitForLoadState('networkidle');
    await page.getByRole('button',{name:'选股历史',exact:true}).click();
    await page.getByRole('button',{name:'2026-09-04 15:00:00'}).click();
    await page.waitForFunction(()=>document.querySelector('#historyContent').textContent.includes('历史测试股票'));
    const before=calls.length;await page.waitForTimeout(3500);
    assert(!calls.slice(before).some(c=>['/api/quotes','/api/watchlist','/api/hot','/api/kline'].includes(c.path)),'history must not poll live market');
    assert(!calls.some(c=>c.method==='POST'),'opening history cannot start update');
    await page.getByRole('button',{name:'历史测试股票 600001'}).click();
    await page.waitForFunction(()=>document.querySelectorAll('#historyDetail canvas').length===4);
    assert((await page.locator('#historyDetail').innerText()).includes('原箱顶 102.00'));
    await page.getByRole('button',{name:'更新本批次表现'}).click();
    await page.waitForFunction(()=>document.querySelector('#reviewProgress').textContent.includes('部分失败'));
    assert((await page.locator('#historyContent').innerText()).includes('旧')===false);
    await page.getByRole('button',{name:'效果统计',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.history-metric').length===3);
    await page.locator('.history-metric').first().click();
    assert((await page.locator('#historyDetail').innerText()).includes('样本明细'));
    const artifacts=path.join(__dirname,'..','data','history_test_artifacts');fs.mkdirSync(artifacts,{recursive:true});
    await page.screenshot({path:path.join(artifacts,'stats-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(artifacts,'stats-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS history: read-only navigation, frozen chart, manual review, partial failure, statistics, desktop/mobile');
  }finally{if(browser)await browser.close();await new Promise(r=>app.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
