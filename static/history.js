/* Historical views only call persisted-data endpoints; quote refresh belongs to live view. */
(() => {
  const style = document.createElement('style');
  style.textContent = `
    [hidden]{display:none!important}.history-filters{display:flex;gap:12px;flex-wrap:wrap;align-items:end;padding:12px 0 20px;border-bottom:1px solid var(--line)}
    .history-filters label{display:grid;gap:6px;color:var(--ink-dim);font-size:12px}
    .history-filters input,.history-filters select{background:var(--bg);color:var(--ink);border:1px solid var(--card-border);border-radius:6px;padding:8px;color-scheme:dark;min-height:36px}
    #historyView .act{background:var(--bg);color:var(--ink);border:1px solid var(--card-border);border-radius:8px;padding:9px 14px;cursor:pointer}
    #historyView .act.go{background:var(--up);color:var(--mint-ink);border-color:var(--up)}#historyView .act:disabled{opacity:.4;cursor:default}
    .history-note{font-size:12px;line-height:1.8;color:var(--ink-dim);margin:14px 0;overflow-wrap:anywhere}
    .history-scroll{overflow:auto}.history-table{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}
    .history-table th{text-align:left;color:var(--ink-faint);font-weight:500;padding:12px 10px;border-bottom:1px solid var(--line)}
    .history-table td{padding:14px 10px;border-bottom:1px solid var(--line);font-family:var(--mono)}
    .history-table tr:hover{background:rgba(229,212,182,.035)}.history-table button,.history-metric{background:none;color:var(--ink);border:0;text-align:left;cursor:pointer}
    .history-table button:hover{text-decoration:underline}.history-pager{display:flex;gap:12px;margin:18px 0;align-items:center;font-size:12px}
    .history-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:16px 0}
    .history-metric{padding:18px;border:1px solid var(--card-border);border-radius:10px}.history-metric b{display:block;font:700 30px var(--num);margin:10px 0;color:var(--up)}
    .history-metric small{color:var(--ink-faint)}#historyDetail{margin-top:24px}.history-evidence{line-height:1.9;font-size:13px;overflow-wrap:anywhere}
    .history-chart{background:var(--card);padding:14px;border:1px solid var(--card-border);border-radius:10px;margin:12px 0}.history-chart canvas{width:100%;height:264px}
    .history-chart.sm{margin:0}.history-chart.sm canvas{height:230px}.history-chart h4{font-size:13px;margin-bottom:8px}
    .history-charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin:14px 0}
    #histCards .resonance select{background:var(--bg);color:var(--ink);border:1px solid #596078;border-radius:5px;padding:3px 8px}
    .hist-name{background:none;border:0;color:var(--ink);font:inherit;font-weight:700;cursor:pointer;text-align:left;padding:0}
    .hist-name:hover{text-decoration:underline}
    @media(max-width:560px){.history-filters label{flex:1 1 42%}.history-filters input{width:100%}.history-table{font-size:12px}}
  `;
  document.head.append(style);
  let selectedBatch = null, batchPage = 1, symbolPage = 1, pollTimer = null, generation = 0;
  const histPeriodSel = {};
  const labels = {complete:'已完成',incomplete:'数据不完整',failed:'失败',imported:'旧数据导入',running:'更新中',partial:'部分失败',interrupted:'已中断',market:'全市场',quick:'快扫',pool:'自选池'};
  const label = v => labels[v] || v || '—';
  const dateText = v => v ? String(v).replace('T',' ').slice(0,19) : '—';
  const pct = v => v == null ? '—' : `${sign(v)}%`;
  const msg = text => { $('historyMessage').textContent = text; };
  async function api(path,body) {
    const r = await fetch('api/'+path,body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : undefined);
    const data = await r.json(); if(!r.ok) throw Error((data.error || `请求失败 ${r.status}`)+(data.archive?.pending?`；${data.archive.pending} 批快照已保存本地，等待归档`:'')); return data;
  }
  function query() {
    return Object.fromEntries([['from','histFrom'],['to','histTo'],['asset','histAsset'],['category','histCategory'],['level','histLevel'],['version','histVersion'],['period','histPeriod']].map(([key,id])=>[key,$(id).value]).filter(([,v])=>v));
  }
  const qs = q => new URLSearchParams(q).toString();
  function table(head,rows) {return `<div class="history-scroll"><table class="history-table"><thead><tr>${head.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows || `<tr><td colspan="${head.length}">没有符合条件的记录</td></tr>`}</tbody></table></div>`;}
  function pager(total,current,action) {
    const div=document.createElement('div'); div.className='history-pager';
    for(const [text,next] of [['上一页',current-1],['下一页',current+1]]) {
      const b=document.createElement('button');b.className='act';b.textContent=text;b.disabled=next<1||(next-1)*50>=total;b.onclick=()=>action(next);div.append(b);
    }
    const span=document.createElement('span');span.textContent=`第 ${current} 页 · ${total} 条`;div.append(span);return div;
  }
  async function loadBatches() {
    const token=++generation; const data=await api('history/batches?'+qs({...query(),page:batchPage})); if(token!==generation)return;
    msg(`原始快照不会随刷新改变。归档队列：${data.archive.pending} 批${data.archive.status==='pending'?'（等待数据库恢复）':''}。`);
    $('historyContent').innerHTML=table(['扫描时间','模式','状态','候选数','策略版本'],data.items.map(b=>`<tr><td><button data-batch="${esc(b.id)}">${esc(dateText(b.as_of))}</button></td><td>${esc(label(b.mode))}</td><td>${esc(label(b.status))}</td><td>${b.count}</td><td>${esc(b.version)}</td></tr>`).join(''));
    $('historyContent').append(pager(data.total,batchPage,n=>{batchPage=n;run(loadBatches);}));
    $('historyContent').querySelectorAll('[data-batch]').forEach(b=>b.onclick=()=>{selectedBatch=b.dataset.batch;symbolPage=1;run(loadBatch);});
    $('historyDetail').innerHTML='';
  }
  function performance(r) {const h=$('histHorizon').value,p=$('histPeriod').value;return h==='latest'?r?.latest?.[p]||{}:r?.windows?.[p]?.[h]||{};}
  function outcome(v,field) {if(v[field]==null)return v.errors?.[field]||'尚未复盘';return (v[field]?'是':'否')+(v.stale?.[field]?'（旧结果）':'');}

  /* ================= 批次明细：卡片布局（迷你图懒加载） ================= */
  const histIO = ("IntersectionObserver" in window) ? new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) { histIO.unobserve(e.target); loadHistChart(e.target.dataset.code); }
  }, { rootMargin: "300px" }) : null;

  function histCard(r) {
    const s=r.snapshot||{}, period=$('histPeriod').value, tf=s.timeframes||{}, v=performance(r.result), p=tf[period]||{};
    const stateName={BREAK:'突破',NEAR:'临界',INSIDE:'箱内',BELOW:'箱下',NA:'—'};
    const qBadge=s.resonance_level==='strong'?'<span class="q-badge">强共振</span>'
      :s.resonance_level==='soft'?'<span class="q-badge soft">准共振</span>'
      :s.resonance_level==='near'?'<span class="q-badge near">临界</span>':'';
    const retCls=v.return_pct>0?'up':v.return_pct<0?'down':'flat';
    const tfSpan=pp=>{const f=tf[pp]||{},st=f.state;if(!st)return `<span class="no">${pp} · 无快照</span>`;
      const dist=f.dist_pct!=null?` ${f.dist_pct>=0?'+':''}${f.dist_pct.toFixed(1)}%`:'';
      return `<span class="${{BREAK:'',NEAR:'warn',INSIDE:'no',BELOW:'no',NA:'no'}[st]||'no'}">${pp}·${stateName[st]||st}${dist}</span>`;};
    return `<article class="sig${s.qualified?' q':''}" data-hcode="${esc(r.code)}">
    <div class="sig-top">
      <div><div class="nm"><button class="hist-name" data-symbol="${esc(r.code)}">${esc(s.name)} ${esc(r.code)}</button>${s.market==='etf'?' <span class="cd">ETF</span>':''}${qBadge}</div>
      <div class="cd">选出价 ${fmt2(s.price)} · 评分 ${esc(s.score ?? '—')}</div></div>
      <div class="sig-top-right"><span class="chg ${retCls}">${pct(v.return_pct)}</span></div>
    </div>
    <div class="sig-line resonance"><b>${esc(period)}箱体 ${fmt2(p.box_low)}–${fmt2(p.box_high)}</b>
      ${['30m','1h','1d'].map(tfSpan).join('')}
      <label>图表 <select aria-label="K线周期" data-hperiod="${esc(r.code)}">${['30m','1h','1d'].map(pp=>`<option value="${pp}" ${pp===period?'selected':''}>${pp}</option>`).join('')}</select></label>
      <span data-confirm style="flex-basis:100%;font-size:11px;overflow-wrap:anywhere"></span></div>
    <div class="sig-chart"><canvas height="264" data-code="${esc(r.code)}"></canvas><div class="tip" data-code="${esc(r.code)}"></div></div>
    <div class="sig-line">
      <span>目标 <b>${esc(v.target_date||'—')}</b></span>
      <span>收盘 ${fmt2(v.close)}</span>
      <span>突破 ${esc(outcome(v,'breakout'))}</span>
      <span>守住 ${esc(outcome(v,'held'))}</span>
      <span class="box" title="${esc(v.errors?.return_pct||'')}">涨跌 ${pct(v.return_pct)}${v.stale?.return_pct?'（旧结果）':''}</span>
    </div>
    <div class="sig-line">${r.update_error?`<span class="no" title="${esc(r.update_error)}">本次更新异常（悬停查看）</span>`:''}<span class="no">更新于 ${esc(dateText(r.updated_at))}</span></div>
  </article>`;
  }

  async function loadHistChart(code,interval) {
    const card=document.querySelector(`#histCards .sig[data-hcode="${code}"]`); if(!card||!selectedBatch) return;
    interval = interval || histPeriodSel[code] || $('histPeriod').value || '1d';
    histPeriodSel[code]=interval; card.dataset.interval=interval;
    let d; try { d=await api(`history/batches/${selectedBatch}/symbols/${code}/kline?interval=${interval}`); } catch(e) { d={error:e.message}; }
    if(!card.isConnected || card.dataset.interval!==interval) return;
    const cv=card.querySelector('canvas'), status=card.querySelector('[data-confirm]');
    if(d.error || !d.bars?.length) {
      cv.getContext('2d').clearRect(0,0,cv.width,cv.height);
      if(status) status.textContent=d.error||'图表数据不可用';
      return;
    }
    const signalDay=String(d.as_of).slice(0,10);
    // 原始K线(尾段) + 选出后K线 拼接，总量控制在 drawChart 的 100 根窗口内，避免分隔线错位
    const original=d.bars.slice(-72), follow=(d.follow||[]).slice(-28);
    if(status) status.textContent=`选出 ${signalDay} · 后续 ${follow.length} 根${d.observations_as_of?` · 行情截至 ${dateText(d.observations_as_of)}`:''}`;
    drawChart(cv,{bars:[...original,...follow],box:d.box,interval},{signal_date:follow.length?signalDay:null});
  }

  function observeHistCharts() {
    const canvases=[...document.querySelectorAll('#histCards canvas[data-code]')];
    if(!histIO) { canvases.forEach(cv=>loadHistChart(cv.dataset.code)); return; }
    canvases.forEach(cv=>histIO.observe(cv));
  }

  async function loadBatch() {
    const token=++generation;const data=await api(`history/batches/${selectedBatch}?`+qs({...query(),page:symbolPage}));if(token!==generation)return;
    msg(`扫描：${dateText(data.batch.as_of)} · ${label(data.batch.mode)} · ${label(data.batch.status)}${data.batch.meta?.partial_data?'（部分标的数据不可用）':''} · 版本 ${data.batch.version}。报价与信号时间保留原值。`);
    const cards=data.items.length
      ? `<div class="cards" id="histCards">${data.items.map(histCard).join('')}</div>`
      : `<div class="empty"><div class="big">NO SNAPSHOT</div><p>该批次没有符合条件的标的</p></div>`;
    $('historyContent').innerHTML=`<div class="history-pager"><button class="act" id="histBack">返回批次列表</button><button class="act go" id="histUpdateBatch">更新本批次表现</button></div>`+cards;
    $('histBack').onclick=()=>{selectedBatch=null;run(loadBatches);};$('histUpdateBatch').onclick=()=>run(()=>update({batch_id:selectedBatch}));
    $('historyContent').append(pager(data.total,symbolPage,n=>{symbolPage=n;run(loadBatch);}));
    observeHistCharts();
    $('historyDetail').innerHTML='';
  }

  async function detail(batch,code) {
    const d=await api(`history/batches/${batch}/symbols/${code}`),s=d.snapshot;
    $('historyDetail').innerHTML=`<h3>${esc(s.name)} ${esc(code)} · 当时的判断</h3><p class="history-evidence">扫描：${esc(dateText(d.as_of))} ｜ 报价：${fmt2(s.price)}（${esc(dateText(s.as_of_quote))}）<br>评分 ${esc(s.score)} · 共振 ${esc(s.resonance_level)} · 倍量天数 ${esc(s.volume_days)} · 试盘 ${esc(s.tests)} 次<br>原始依据：${esc(JSON.stringify(s.flags||[]))}<br>行情证据截至：${esc(dateText(d.observations_as_of))}（该标的最近一次成功拉取） · 复盘结果更新：${esc(dateText(d.result?.as_of))}</p>`;
    if(d.update_error){const warning=document.createElement('p');warning.className='history-note';warning.textContent=`本次更新异常：${d.update_error}（${dateText(d.attempted_at)}），已有有效结果保留。`;$('historyDetail').append(warning);}
    for(const p of ['1d','30m','1h']) {
      const old=s.timeframes?.[p]||{},next=d.observations?.[p]||{};
      const section=document.createElement('section');section.className='history-chart';
      section.innerHTML=`<h4>${esc(p)} · 原箱顶 ${fmt2(old.box_high)} · ${esc(old.state||'无快照')}</h4><p class="history-note">原始图与后续行情分开保存；水平线始终使用原箱顶。${esc(old.reason||'')}</p><div class="history-original"></div><div class="history-follow"></div>`;
      $('historyDetail').append(section);
      for(const [selector,title,bars] of [['.history-original','选出时的 K 线（右端为信号截止）',old.bars||[]],['.history-follow','选出后的 K 线', (next.bars||[]).filter(b=>b.date.slice(0,10)>String(d.as_of).slice(0,10))]]) {
        const host=section.querySelector(selector),heading=document.createElement('p');heading.className='history-note';heading.textContent=title;host.append(heading);
        if(!bars.length){host.append(document.createTextNode('暂无完整行情'));continue;}
        const cv=document.createElement('canvas');cv.dataset.code=code;cv.setAttribute('aria-label',title);host.append(cv);
        drawChart(cv,{bars,box:old,interval:p});
      }
      const v=d.result?.windows?.[p]||{};const evidence=document.createElement('p');evidence.className='history-evidence';
      evidence.textContent=Object.entries(v).map(([n,r])=>`${n}日：${r.target_date||'待到期'} · 涨跌 ${pct(r.return_pct)} · 突破 ${outcome(r,'breakout')} · 守住 ${outcome(r,'held')} · 曾触及 ${outcome(r,'touched')}`).join('；');section.append(evidence);
    }
    $('historyDetail').scrollIntoView({behavior:'smooth',block:'start'});
  }

  /* ================= 效果统计：指标卡 + 三张图 ================= */
  function prep(cv,h) {
    const dpr=window.devicePixelRatio||1, W=cv.clientWidth||cv.parentElement.clientWidth||560;
    cv.width=W*dpr; cv.height=h*dpr;
    const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr);
    return [ctx,W,h];
  }
  function frame(ctx,W,H,lo,hi) {
    const padL=8,padR=58,padT=30,vh=H-44, y=v=>padT+(hi-v)/(hi-lo)*vh;
    ctx.font='10px IBM Plex Mono, monospace'; ctx.lineWidth=1;
    for(let i=0;i<=4;i++) {
      const yy=padT+i/4*vh, v=hi-i/4*(hi-lo);
      ctx.strokeStyle='rgba(229,212,182,.07)'; ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
      ctx.fillStyle='rgba(229,212,182,.4)'; ctx.textAlign='left'; ctx.fillText(v.toFixed(1),W-padR+6,yy+3);
    }
    return {padL,padR,padT,vh,y};
  }
  function emptyChart(ctx,W,H) {
    ctx.fillStyle='rgba(229,212,182,.4)'; ctx.font='12px IBM Plex Mono, monospace'; ctx.textAlign='center';
    ctx.fillText('暂无数据',W/2,H/2);
  }
  function zeroLine(ctx,padL,x2,y) {
    ctx.save(); ctx.strokeStyle='rgba(229,212,182,.18)'; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(x2,y); ctx.stroke(); ctx.restore();
  }
  function seriesChart(cv,daily) {   // 按日胜率 + 平均涨跌（双线）
    const [ctx,W,H]=prep(cv,230);
    if(!daily.length) return emptyChart(ctx,W,H);
    const ys=daily.flatMap(d=>[d.rate,d.mean_return]).filter(v=>v!=null);
    if(!ys.length) return emptyChart(ctx,W,H);
    let lo=Math.min(...ys),hi=Math.max(...ys);
    if(hi-lo<1e-9){hi+=1;lo-=1;}
    const pad=(hi-lo)*0.15; lo-=pad; hi+=pad;
    const {padL,padR,vh,y}=frame(ctx,W,H,lo,hi);
    zeroLine(ctx,padL,W-padR,y(Math.max(lo,Math.min(hi,0))));
    const x=i=>padL+(W-padL-padR)*(daily.length===1?0.5:i/(daily.length-1));
    const line=(key,color)=>{
      ctx.strokeStyle=color; ctx.fillStyle=color; ctx.lineWidth=1.5; ctx.beginPath(); let started=false;
      daily.forEach((d,i)=>{ if(d[key]==null)return; const xx=x(i),yy=y(d[key]); started?(ctx.lineTo(xx,yy)):(ctx.moveTo(xx,yy),started=true); });
      ctx.stroke();
      daily.forEach((d,i)=>{ if(d[key]==null)return; ctx.beginPath(); ctx.arc(x(i),y(d[key]),2.2,0,7); ctx.fill(); });
    };
    line('rate','#8FC98B'); line('mean_return','#E8C468');
    ctx.fillStyle='#8FC98B'; ctx.textAlign='left'; ctx.fillText('— 胜率%',padL+4,14);
    ctx.fillStyle='#E8C468'; ctx.fillText('— 平均涨跌%',padL+72,14);
    ctx.fillStyle='rgba(229,212,182,.4)'; ctx.textAlign='center';
    [0,Math.floor(daily.length/2),daily.length-1].forEach(i=>ctx.fillText(String(daily[i].date).slice(5),x(i),H-6));
  }
  function excessChart(cv,daily) {   // 按日超额（正绿负红柱）
    const [ctx,W,H]=prep(cv,230);
    const items=daily.filter(d=>d.mean_excess!=null);
    if(!items.length) return emptyChart(ctx,W,H);
    const ys=items.map(d=>d.mean_excess);
    let lo=Math.min(0,...ys),hi=Math.max(0,...ys);
    if(hi-lo<1e-9){hi+=1;lo-=1;}
    const pad=(hi-lo)*0.15; lo-=pad; hi+=pad;
    const {padL,padR,vh,y}=frame(ctx,W,H,lo,hi);
    const step=(W-padL-padR)/items.length, bw=Math.max(2,Math.min(18,step*0.66)), zero=y(0);
    zeroLine(ctx,padL,W-padR,zero);
    items.forEach((d,i)=>{
      const xx=padL+i*step+step/2, yy=y(d.mean_excess);
      ctx.fillStyle=d.mean_excess>=0?'#8FC98B':'#F07A63';
      ctx.fillRect(xx-bw/2,Math.min(yy,zero),bw,Math.max(1,Math.abs(yy-zero)));
    });
    ctx.fillStyle='rgba(229,212,182,.4)'; ctx.textAlign='center';
    [0,Math.floor(items.length/2),items.length-1].forEach(i=>ctx.fillText(String(items[i].date).slice(5),padL+i*step+step/2,H-6));
  }
  function distChart(cv,values) {   // 涨跌分布直方图 + 中位数
    const [ctx,W,H]=prep(cv,230);
    if(!values.length) return emptyChart(ctx,W,H);
    let lo=Math.min(...values),hi=Math.max(...values);
    if(hi-lo<1e-9){hi+=1;lo-=1;}
    const nbins=Math.max(6,Math.min(21,Math.ceil((hi-lo)/2))), width=(hi-lo)/nbins, bins=new Array(nbins).fill(0);
    values.forEach(v=>bins[Math.min(nbins-1,Math.floor((v-lo)/width))]++);
    const {padL,padR,padT,vh,y}=frame(ctx,W,H,0,Math.max(...bins)*1.15||1);
    const step=(W-padL-padR)/nbins, bw=Math.max(2,step*0.8);
    bins.forEach((n,i)=>{
      const xx=padL+i*step+step/2, top=y(n);
      ctx.fillStyle='rgba(143,201,139,.72)';
      ctx.fillRect(xx-bw/2,top,bw,Math.max(1,padT+vh-top));
    });
    const sorted=[...values].sort((a,b)=>a-b), median=sorted[Math.floor(sorted.length/2)];
    const mx=padL+(median-lo)/(hi-lo)*(W-padL-padR);
    ctx.save(); ctx.strokeStyle='#E8C468'; ctx.setLineDash([4,4]); ctx.beginPath(); ctx.moveTo(mx,padT); ctx.lineTo(mx,padT+vh); ctx.stroke(); ctx.restore();
    ctx.fillStyle='#E8C468'; ctx.textAlign='left'; ctx.fillText(`中位数 ${sign(median)}%`,Math.min(mx+4,W-padR-78),padT+10);
    ctx.fillStyle='rgba(229,212,182,.4)'; ctx.textAlign='center';
    ctx.fillText(sign(lo)+'%',padL+12,H-6); ctx.fillText(sign(hi)+'%',W-padR-12,H-6);
  }
  function chartNode(title) {
    const sec=document.createElement('section'); sec.className='history-chart sm';
    const h=document.createElement('h4'); h.textContent=title; sec.append(h);
    const cv=document.createElement('canvas'); cv.setAttribute('role','img'); cv.setAttribute('aria-label',title); sec.append(cv);
    return [sec,cv];
  }
  function drawGroupCharts(host,group) {
    const daily=group.daily||[];
    const values=group.samples.map(s=>s.performance.return_pct).filter(v=>v!=null);
    const [s1,c1]=chartNode('按日胜率与平均涨跌'),[s2,c2]=chartNode('相对上证指数超额（按日均值）'),[s3,c3]=chartNode(`${group.horizon}日涨跌分布 · ${values.length} 个有效样本`);
    host.append(s1,s2,s3);
    seriesChart(c1,daily); excessChart(c2,daily); distChart(c3,values);
  }

  async function loadStats() {
    const token=++generation;const data=await api('review-stats?'+qs(query()));if(token!==generation)return;
    msg(`使用每日最后一次收盘全量扫描，共 ${data.batches} 批。${data.note} ${data.missing_dates.length?'缺失批次日期：'+data.missing_dates.join('、'):''}`);
    $('historyContent').innerHTML='';$('historyDetail').innerHTML='';
    if(!data.groups.length){$('historyContent').textContent='暂无有效样本。请先保存收盘全量扫描，再手动更新表现。';return;}
    for(const group of data.groups) {
      const section=document.createElement('section');
      section.innerHTML=`<h3 style="margin-top:24px">${group.asset==='etf'?'ETF':'A股'} · ${group.horizon} 日 · 策略 ${esc(group.version)}</h3><div class="history-summary"></div><div class="history-charts"></div><p class="history-note">平均涨跌 ${pct(group.mean_return)} · 平均超额 ${pct(group.mean_excess)} · 持平 ${group.flat} 只</p>`;
      $('historyContent').append(section);
      for(const [key,title] of [['up','上涨率'],['breakout','突破率'],['held','守住率'],['excess','超额胜率']]) {
        const m=group.metrics[key],button=document.createElement('button');button.className='history-metric';
        button.innerHTML=`${title}<b>${m.rate==null?'—':fmt2(m.rate)+'%'}</b><small>${m.success} / ${m.valid} 有效样本</small><p class="history-note">${esc(Object.entries(m.excluded).map(([k,v])=>`${k} ${v}`).join('；'))}</p>`;
        button.onclick=()=>{
          $('historyDetail').innerHTML=`<h3>${title} · 样本明细</h3>`+table(['标的','信号日期','涨跌幅','超额','突破','守住'],group.samples.map(s=>`<tr><td><button data-id="${esc(s.batch_id)}" data-code="${esc(s.code)}">${esc(s.name)} ${esc(s.code)}</button></td><td>${esc(s.trade_date)}</td><td title="${esc(s.performance.errors?.return_pct||'')}">${pct(s.performance.return_pct)}</td><td title="${esc(s.performance.errors?.excess||'')}">${pct(s.performance.excess)}</td><td>${esc(outcome(s.performance,'breakout'))}</td><td>${esc(outcome(s.performance,'held'))}</td></tr>`).join(''));
          $('historyDetail').querySelectorAll('button').forEach(b=>b.onclick=()=>run(()=>detail(b.dataset.id,b.dataset.code)));
        };section.querySelector('.history-summary').append(button);
      }
      drawGroupCharts(section.querySelector('.history-charts'),group);
    }
  }
  async function update(body) {
    const task=await api('reviews',body);clearTimeout(pollTimer);
    const poll=async()=>{
      try {
        const j=await api('reviews/'+task.id),p=j.payload;
        $('reviewProgress').textContent=`${label(j.status)}：${p.done}/${p.total} · 失败 ${p.errors.length} 项`+(p.errors.length?' · '+p.errors.map(e=>`${e.code||''} ${e.error}`).join('；'):'');
        if(j.status==='running'){pollTimer=setTimeout(poll,1500);}else if(activeView!=='live'){run(loadCurrent);}
      }catch(e){$('reviewProgress').textContent=e.message;}
    };await poll();
  }
  function loadCurrent(){return activeView==='stats'?loadStats():selectedBatch?loadBatch():loadBatches();}
  async function run(fn){try{await fn();}catch(e){msg(e.message);}}
  $('viewTabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{
    activeView=b.dataset.view;generation++;
    $('viewTabs').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
    $('liveView').hidden=activeView!=='live';$('historyView').hidden=activeView==='live';
    $('marketTabs').hidden=activeView!=='live';$('btnScan').hidden=activeView!=='live';$('btnQuick').hidden=activeView!=='live';
    if(activeView==='live'){startLiveTimers();refresh();}else{clearInterval(quoteTimer);clearInterval(liveTimer);if(chartIO)chartIO.disconnect();$('historyTitle').textContent=activeView==='stats'?'效果统计':'选股历史';run(loadCurrent);}
  });
  $('histQuery').onclick=()=>{batchPage=1;symbolPage=1;run(loadCurrent);};
  $('histPeriod').onchange=()=>{Object.keys(histPeriodSel).forEach(k=>delete histPeriodSel[k]);run(loadCurrent);};
  $('histHorizon').onchange=()=>run(loadCurrent);
  $('histUpdateRange').onclick=()=>{
    const q=query();if(!q.from||!q.to){msg('请先选择开始和结束日期。');return;}run(()=>update({from:q.from,to:q.to,version:q.version}));
  };
  // 卡片交互走事件委托：卡片DOM随翻页/筛选重建，容器级监听不丢失
  $('historyContent').addEventListener('click',e=>{
    const name=e.target.closest('.hist-name[data-symbol]');
    if(name&&selectedBatch) run(()=>detail(selectedBatch,name.dataset.symbol));
  });
  $('historyContent').addEventListener('change',e=>{
    const sel=e.target.closest('select[data-hperiod]');
    if(sel) loadHistChart(sel.dataset.hperiod,sel.value);
  });
})();
