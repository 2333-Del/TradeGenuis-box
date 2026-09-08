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
    @media(max-width:560px){.history-filters label{flex:1 1 42%}.history-filters input{width:100%}.history-table{font-size:12px}}
  `;
  document.head.append(style);
  let selectedBatch = null, batchPage = 1, symbolPage = 1, pollTimer = null, generation = 0;
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
  function sampleTable(items) {
    return table(['标的','当时价格','原箱顶 / 状态','目标日期','后续收盘','涨跌幅','确认突破','守住箱顶','更新于'],items.map(r=>{
      const s=r.snapshot||{},p=s.timeframes?.[$('histPeriod').value]||{},v=performance(r.result);
      return `<tr><td><button data-symbol="${esc(r.code)}">${esc(s.name)} ${esc(r.code)}</button></td><td>${fmt2(s.price)}</td><td>${fmt2(p.box_high)} / ${esc(p.state)}</td><td>${esc(v.target_date||'—')}</td><td>${fmt2(v.close)}</td><td title="${esc(v.errors?.return_pct||'')}">${pct(v.return_pct)}${v.stale?.return_pct?'（旧结果）':''}</td><td>${esc(outcome(v,'breakout'))}</td><td>${esc(outcome(v,'held'))}</td><td title="${esc(r.update_error||'')}">${esc(dateText(r.updated_at))}${r.update_error?'<br>本次更新异常（悬停查看）':''}</td></tr>`;
    }).join(''));
  }
  async function loadBatch() {
    const token=++generation;const data=await api(`history/batches/${selectedBatch}?`+qs({...query(),page:symbolPage}));if(token!==generation)return;
    msg(`扫描：${dateText(data.batch.as_of)} · ${label(data.batch.mode)} · ${label(data.batch.status)}${data.batch.meta?.partial_data?'（部分标的数据不可用）':''} · 版本 ${data.batch.version}。报价与信号时间保留原值。`);
    $('historyContent').innerHTML=`<div class="history-pager"><button class="act" id="histBack">返回批次列表</button><button class="act go" id="histUpdateBatch">更新本批次表现</button></div>`+sampleTable(data.items);
    $('histBack').onclick=()=>{selectedBatch=null;run(loadBatches);};$('histUpdateBatch').onclick=()=>run(()=>update({batch_id:selectedBatch}));
    $('historyContent').append(pager(data.total,symbolPage,n=>{symbolPage=n;run(loadBatch);}));
    $('historyContent').querySelectorAll('[data-symbol]').forEach(b=>b.onclick=()=>run(()=>detail(selectedBatch,b.dataset.symbol)));
    $('historyDetail').innerHTML='';
  }
  async function detail(batch,code) {
    const d=await api(`history/batches/${batch}/symbols/${code}`),s=d.snapshot;
    $('historyDetail').innerHTML=`<h3>${esc(s.name)} ${esc(code)} · 当时的判断</h3><p class="history-evidence">扫描：${esc(dateText(d.as_of))} ｜ 报价：${fmt2(s.price)}（${esc(dateText(s.as_of_quote))}）<br>评分 ${esc(s.score)} · 共振 ${esc(s.resonance_level)} · 倍量天数 ${esc(s.volume_days)} · 试盘 ${esc(s.tests)} 次<br>原始依据：${esc(JSON.stringify(s.flags||[]))}<br>最近复盘行情截至：${esc(dateText(d.result?.as_of))}</p>`;
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
  async function loadStats() {
    const token=++generation;const data=await api('review-stats?'+qs(query()));if(token!==generation)return;
    msg(`使用每日最后一次收盘全量扫描，共 ${data.batches} 批。${data.note} ${data.missing_dates.length?'缺失批次日期：'+data.missing_dates.join('、'):''}`);
    $('historyContent').innerHTML='';$('historyDetail').innerHTML='';
    if(!data.groups.length){$('historyContent').textContent='暂无有效样本。请先保存收盘全量扫描，再手动更新表现。';return;}
    for(const group of data.groups) {
      const section=document.createElement('section');section.innerHTML=`<h3 style="margin-top:24px">${group.asset==='etf'?'ETF':'A股'} · ${group.horizon} 日 · 策略 ${esc(group.version)}</h3><div class="history-summary"></div><p class="history-note">平均涨跌 ${pct(group.mean_return)} · 持平 ${group.flat} 只</p>`;
      $('historyContent').append(section);
      for(const [key,title] of [['up','上涨率'],['breakout','突破率'],['held','守住率']]) {
        const m=group.metrics[key],button=document.createElement('button');button.className='history-metric';
        button.innerHTML=`${title}<b>${m.rate==null?'—':fmt2(m.rate)+'%'}</b><small>${m.success} / ${m.valid} 有效样本</small><p class="history-note">${esc(Object.entries(m.excluded).map(([k,v])=>`${k} ${v}`).join('；'))}</p>`;
        button.onclick=()=>{
          $('historyDetail').innerHTML=`<h3>${title} · 样本明细</h3>`+table(['标的','信号日期','涨跌幅','突破','守住'],group.samples.map(s=>`<tr><td><button data-id="${esc(s.batch_id)}" data-code="${esc(s.code)}">${esc(s.name)} ${esc(s.code)}</button></td><td>${esc(s.trade_date)}</td><td>${pct(s.performance.return_pct)}</td><td>${esc(outcome(s.performance,'breakout'))}</td><td>${esc(outcome(s.performance,'held'))}</td></tr>`).join(''));
          $('historyDetail').querySelectorAll('button').forEach(b=>b.onclick=()=>run(()=>detail(b.dataset.id,b.dataset.code)));
        };section.querySelector('.history-summary').append(button);
      }
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
  $('histPeriod').onchange=$('histHorizon').onchange=()=>run(loadCurrent);
  $('histUpdateRange').onclick=()=>{
    const q=query();if(!q.from||!q.to){msg('请先选择开始和结束日期。');return;}run(()=>update({from:q.from,to:q.to,version:q.version}));
  };
})();
