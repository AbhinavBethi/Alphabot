const API = 'http://127.0.0.1:8000';
const state = {
  token:null, username:null, portfolioId:null,
  selectedTickers:new Set(), ws:null, wsPingInterval:null,
  signals:{}, prices:{}, charts:{}, portfolioHistory:[],
  portfolioData:null, activeSignalTab:'BTC-USD', activePriceTab:'BTC-USD',
  chartInstances:{}, lastSignalTimes:{}, tradingPaused:false,
};

/* ── Toast ── */
function toast(msg, type='info') {
  const old = document.getElementById('ab-toast');
  if (old) old.remove();
  const colors = {success:'#00c896',error:'#f23645',warning:'#f59e0b',info:'#2563eb'};
  const t = document.createElement('div');
  t.id = 'ab-toast';
  t.style.cssText = `background:${colors[type]||colors.info};color:#fff`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity='0'; t.style.transition='opacity 0.3s'; setTimeout(()=>t.remove(),300); }, 3500);
}

/* ── Screens ── */
function showScreen(id) {
  document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));
  document.getElementById('screen-'+id).classList.add('active');
}
function switchAuthTab(tab) {
  ['login','register'].forEach(t=>{
    document.getElementById('tab-'+t).classList.toggle('active',t===tab);
    document.getElementById('form-'+t).classList.toggle('active',t===tab);
  });
}
function setMsg(id, text, type) {
  const el = document.getElementById(id);
  el.textContent = text; el.className = 'auth-msg '+type;
}

/* ── Auth ── */
async function handleRegister(e) {
  e.preventDefault();
  const u=document.getElementById('reg-username').value.trim();
  const em=document.getElementById('reg-email').value.trim();
  const p=document.getElementById('reg-password').value;
  try {
    const res=await fetch(API+'/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,email:em,password:p})});
    const d=await res.json();
    if(!res.ok){setMsg('reg-msg',d.detail||'Failed','error');return;}
    setMsg('reg-msg','Account created! Signing in...','success');
    setTimeout(()=>doLogin(u,p),900);
  } catch{setMsg('reg-msg','Connection error','error');}
}
async function handleLogin(e) {
  e.preventDefault();
  await doLogin(document.getElementById('login-username').value.trim(),document.getElementById('login-password').value);
}
async function doLogin(username, password) {
  try {
    const res=await fetch(API+'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
    const d=await res.json();
    if(!res.ok){
      setMsg('login-msg',d.detail||'Wrong credentials','error');
      const card=document.querySelector('.auth-card');
      card.style.animation='none';card.offsetWidth;card.style.animation='shake 0.4s ease';
      return;
    }
    state.token=d.access_token; state.username=d.username;
    setMsg('login-msg','Welcome back, '+d.username+'!','success');
    setTimeout(goToPicker,700);
  } catch{setMsg('login-msg','Connection error','error');}
}

/* ── Picker ── */
async function goToPicker() {
  try {
    const res=await fetch(API+'/me/portfolio',{headers:{Authorization:'Bearer '+state.token}});
    const d=await res.json();
    state.portfolioId=d.id;
    d.stocks.forEach(s=>{state.selectedTickers.add(s.ticker);markCard(s.ticker,true);});
    if(d.stocks.length>0){await goToDashboard();return;}
  } catch{}
  showScreen('picker');
  loadPickerPrices();
}
async function loadPickerPrices() {
  try {
    const s=await (await fetch(API+'/api/snapshot/1')).json();
    const p=s.prices||{};
    [['GC=F','GCF'],['SPY','SPY'],['BTC-USD','BTCUSD']].forEach(([t,id])=>{
      const el=document.getElementById('picker-price-'+id);
      if(el&&p[t]) el.textContent=fmtD(p[t]);
    });
  } catch{}
}
function tid(t){return t.replace('=','').replace('-','');}
function toggleTicker(ticker) {
  if(state.selectedTickers.has(ticker)){state.selectedTickers.delete(ticker);markCard(ticker,false);}
  else{state.selectedTickers.add(ticker);markCard(ticker,true);}
}
function markCard(ticker, sel) {
  const el=document.getElementById('card-'+tid(ticker));
  if(el) el.classList.toggle('selected',sel);
}
async function confirmStockPick() {
  if(!state.selectedTickers.size){
    document.getElementById('picker-msg').textContent='Pick at least one asset.';
    document.getElementById('picker-msg').className='auth-msg error';return;
  }
  document.getElementById('picker-msg').textContent='Saving...';
  document.getElementById('picker-msg').className='auth-msg';
  for(const t of state.selectedTickers){
    try{await fetch(API+'/me/portfolio/stocks',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+state.token},body:JSON.stringify({ticker:t})});}catch{}
  }
  await goToDashboard();
}

/* ── Dashboard ── */
async function goToDashboard() {
  showScreen('dashboard');
  document.getElementById('dash-username').textContent=state.username;
  initCharts();
  await loadInitialData();
  connectWebSocket();
  checkBotOnline();
  setInterval(checkBotOnline,15000);
  setInterval(()=>{loadAnalytics();loadPending();loadControlStatus();},30000);
}
async function checkBotOnline() {
  try {
    const d=await (await fetch(API+'/health')).json();
    const last=Math.max(...Object.values(state.lastSignalTimes).map(t=>new Date(t).getTime()),0);
    const ok=d.bot_online===true||(Date.now()-last<5*60*1000);
    document.getElementById('ws-dot').className='ws-dot '+(ok?'online':'offline');
    document.getElementById('ws-label').textContent=ok?'Bot Live':'Bot Offline';
  } catch{
    document.getElementById('ws-dot').className='ws-dot offline';
    document.getElementById('ws-label').textContent='API Offline';
  }
}
async function loadInitialData() {
  try {
    const snap=await (await fetch(API+'/api/snapshot/'+(state.portfolioId||1))).json();
    handleSnapshot(snap);
    await Promise.all([loadTrades(),loadPending(),loadAnalytics(),loadControlStatus()]);
  } catch(e){console.error('Initial load:',e);}
}
async function loadTrades() {
  try{const r=await fetch(API+'/me/trades?limit=50',{headers:{Authorization:'Bearer '+state.token}});renderTrades(await r.json());}catch{}
}

/* ── Analytics ── */
async function loadAnalytics() {
  try{const d=await (await fetch(API+'/analytics/summary',{headers:{Authorization:'Bearer '+state.token}})).json();renderAnalytics(d);}catch{}
}
function renderAnalytics(d) {
  if(!d)return;
  const s=document.getElementById('an-sharpe');
  s.textContent=d.sharpe_ratio!=null?d.sharpe_ratio.toFixed(3):'--';
  s.style.color=d.sharpe_ratio>1?'var(--green)':d.sharpe_ratio<0?'var(--red)':'';
  document.getElementById('an-winrate').textContent=d.win_rate!=null?d.win_rate.toFixed(1)+'%':'--';
  document.getElementById('an-wincount').textContent=(d.winning_trades||0)+'W / '+(d.losing_trades||0)+'L';
  const dd=document.getElementById('an-drawdown');
  dd.textContent=d.max_drawdown!=null?d.max_drawdown.toFixed(2)+'%':'--';
  dd.style.color=d.max_drawdown>5?'var(--red)':'';
  const be=document.getElementById('an-best');
  be.textContent=fmtD(d.best_trade_pnl);be.style.color=(d.best_trade_pnl||0)>=0?'var(--green)':'var(--red)';
  const wo=document.getElementById('an-worst');
  wo.textContent=fmtD(d.worst_trade_pnl);wo.style.color=(d.worst_trade_pnl||0)>=0?'var(--green)':'var(--red)';
  document.getElementById('an-most').textContent=d.most_traded||'--';
  document.getElementById('an-total-trades').textContent=(d.total_trades||0)+' total trades';
}

/* ── Controls ── */
async function loadControlStatus() {
  try {
    const res=await fetch(API+'/controls/status',{headers:{Authorization:'Bearer '+state.token}});
    if(!res.ok){console.error('Controls status error:',await res.text());return;}
    renderControlStatus(await res.json());
  } catch(e){console.error('Controls load error:',e);}
}
function renderControlStatus(d) {
  if(!d)return;
  state.tradingPaused=d.trading_paused;
  const card=document.getElementById('ctrl-card');
  const badge=document.getElementById('ctrl-badge');
  const dot=document.getElementById('ctrl-dot');
  const btext=document.getElementById('ctrl-badge-text');
  const pb=document.getElementById('btn-pause');
  const rb=document.getElementById('btn-resume');
  const hint=document.getElementById('ctrl-hint');
  const hdr=document.getElementById('dash-header');

  if(d.trading_paused){
    card.className='ctrl-card is-paused';
    badge.className='ctrl-badge ctrl-badge-paused';
    dot.className='ctrl-dot ctrl-dot-paused';
    btext.textContent='Paused';
    pb.style.display='none'; rb.style.display='flex';
    hint.textContent='Trading is paused. Bot is running but no trades will execute until you resume.';
    hint.className='ctrl-hint paused-hint';
    hdr.classList.add('paused-header');
  } else {
    card.className='ctrl-card';
    badge.className='ctrl-badge ctrl-badge-active';
    dot.className='ctrl-dot ctrl-dot-active';
    btext.textContent='Active';
    pb.style.display='flex'; rb.style.display='none';
    hint.textContent='Pause stops new trades while keeping the bot running. Emergency Stop sells all open positions immediately.';
    hint.className='ctrl-hint';
    hdr.classList.remove('paused-header');
  }
  document.getElementById('ctrl-mode').textContent=d.auto_trade?'Auto-trade (30s)':'Manual approval';
  const pos=Object.values(d.positions||{}).filter(p=>p.shares>0).length;
  document.getElementById('ctrl-positions').textContent=pos>0?pos+' open position'+(pos>1?'s':''):'No open positions';
  document.getElementById('ctrl-invested').textContent=fmtD(d.total_invested);
}

async function pauseTrading() {
  const b=document.getElementById('btn-pause');
  b.disabled=true;
  b.querySelector('.ctrl-btn-title').textContent='Pausing...';
  try {
    const res=await fetch(API+'/controls/pause',{method:'PATCH',headers:{Authorization:'Bearer '+state.token}});
    const d=await res.json();
    if(!res.ok){toast(d.detail||'Could not pause','error');return;}
    renderControlStatus(d);
    toast('Trading paused. No new trades will execute.','warning');
  } catch{toast('Connection error','error');}
  finally{b.disabled=false;b.querySelector('.ctrl-btn-title').textContent='Pause Trading';}
}

async function resumeTrading() {
  const b=document.getElementById('btn-resume');
  b.disabled=true;
  b.querySelector('.ctrl-btn-title').textContent='Resuming...';
  try {
    const res=await fetch(API+'/controls/resume',{method:'PATCH',headers:{Authorization:'Bearer '+state.token}});
    const d=await res.json();
    if(!res.ok){toast(d.detail||'Could not resume','error');return;}
    renderControlStatus(d);
    toast('Trading resumed! Bot will start executing signals.','success');
  } catch{toast('Connection error','error');}
  finally{b.disabled=false;b.querySelector('.ctrl-btn-title').textContent='Resume Trading';}
}

function confirmEmergencyStop(){document.getElementById('estop-modal').style.display='flex';}
function closeModal(){document.getElementById('estop-modal').style.display='none';}

async function executeEmergencyStop() {
  closeModal();
  const b=document.getElementById('btn-estop');
  b.disabled=true;
  b.querySelector('.ctrl-btn-title').textContent='Executing...';
  try {
    const res=await fetch(API+'/controls/emergency-stop',{method:'POST',headers:{Authorization:'Bearer '+state.token}});
    const d=await res.json();
    if(!res.ok){toast(d.detail||'Emergency stop failed','error');return;}
    toast(d.message,'warning');
    await Promise.all([loadControlStatus(),loadTrades(),loadAnalytics()]);
    fetchAndRenderPortfolio();
  } catch{toast('Connection error','error');}
  finally{b.disabled=false;b.querySelector('.ctrl-btn-title').textContent='Emergency Stop';}
}

/* ── Pending ── */
async function loadPending() {
  try{const r=await fetch(API+'/me/pending',{headers:{Authorization:'Bearer '+state.token}});renderPending(await r.json());}catch{}
}
function renderPending(pending) {
  const tb=document.getElementById('pending-tbody');
  document.getElementById('pending-count').textContent=pending.length+' pending';
  if(!pending.length){tb.innerHTML='<tr class="empty-row"><td colspan="8">No pending trades</td></tr>';return;}
  const tc=t=>t==='GC=F'?'td-gold':t==='SPY'?'td-spy':'td-btc';
  const ac=a=>a==='BUY'?'td-buy':'td-sell';
  tb.innerHTML=pending.map(p=>{
    const sec=Math.max(0,Math.round((new Date(p.expires_at)-Date.now())/1000));
    return '<tr id="pr-'+p.id+'"><td>'+fmtTime(p.created_at)+'</td><td class="'+tc(p.ticker)+'">'+p.ticker+'</td><td class="'+ac(p.action)+'">'+p.action+'</td><td>'+fmtD(p.price)+'</td><td>'+(p.quantity!=null?p.quantity.toFixed(4):'--')+'</td><td>'+(p.signal_value!=null?p.signal_value.toFixed(4):'--')+'</td><td class="'+(sec<10?'expires-soon':'')+'">'+(sec>0?sec+'s':'exp')+'</td><td><div class="pending-actions"><button class="btn-approve" onclick="resolvePending('+p.id+',\'approve\')">Approve</button><button class="btn-reject" onclick="resolvePending('+p.id+',\'reject\')">Reject</button></div></td></tr>';
  }).join('');
}
async function resolvePending(id, action) {
  try {
    const res=await fetch(API+'/me/pending/'+id+'/resolve',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+state.token},body:JSON.stringify({action})});
    if(res.ok){
      const row=document.getElementById('pr-'+id);
      if(row){row.style.opacity='0';row.style.transition='opacity 0.3s';}
      toast(action==='approve'?'Trade approved':'Trade rejected',action==='approve'?'success':'info');
      setTimeout(()=>{loadPending();loadTrades();loadAnalytics();},350);
    } else{const d=await res.json();toast(d.detail||'Error','error');loadPending();}
  } catch{toast('Connection error','error');}
}

/* ── WebSocket ── */
function connectWebSocket() {
  if(state.ws) state.ws.close();
  state.ws=new WebSocket('ws://127.0.0.1:8000/ws?token='+state.token);
  state.ws.onopen=()=>{
    state.wsPingInterval=setInterval(()=>{
      if(state.ws?.readyState===WebSocket.OPEN) state.ws.send(JSON.stringify({type:'ping'}));
    },25000);
  };
  state.ws.onmessage=evt=>{
    try{const d=JSON.parse(evt.data);if(d.type==='snapshot')handleSnapshot(d);else if(d.type==='signal')handleSignalUpdate(d);}catch{}
  };
  state.ws.onclose=()=>{clearInterval(state.wsPingInterval);setTimeout(connectWebSocket,3000);};
}

/* ── Data handlers ── */
function handleSnapshot(d) {
  if(d.signals){state.signals=d.signals;renderSigTab(state.activeSignalTab);}
  if(d.prices) state.prices=d.prices;
  if(d.portfolio){
    state.portfolioData=d.portfolio;
    renderPortStats(d.portfolio);renderDonut(d.portfolio);
    if(d.portfolio.total_value>0){state.portfolioHistory.push({value:d.portfolio.total_value,time:d.portfolio.timestamp||new Date().toISOString()});updatePortChart();}
  }
  if(d.charts){state.charts=d.charts;renderPriceChart(state.activePriceTab);}
}
function handleSignalUpdate(d) {
  const t=d.ticker;
  state.lastSignalTimes[t]=d.timestamp;
  state.signals[t]=d; state.prices[t]=d.price;
  if(t===state.activeSignalTab) renderSigTab(t);
  if(!state.charts[t]) state.charts[t]=[];
  state.charts[t].push({price:d.price,time:d.timestamp});
  if(state.charts[t].length>300) state.charts[t].shift();
  if(t===state.activePriceTab) renderPriceChart(t);
  if(d.portfolio){state.portfolioData=d.portfolio;renderPortStats(d.portfolio);renderDonut(d.portfolio);
    if(d.portfolio.total_value>0){state.portfolioHistory.push({value:d.portfolio.total_value,time:d.timestamp});if(state.portfolioHistory.length>300)state.portfolioHistory.shift();updatePortChart();}
  } else if(d.portfolio_value>0){
    document.getElementById('stat-total').textContent=fmtD(d.portfolio_value);
    state.portfolioHistory.push({value:d.portfolio_value,time:d.timestamp});
    if(state.portfolioHistory.length>300)state.portfolioHistory.shift();
    updatePortChart(); fetchAndRenderPortfolio();
  }
  loadPending();
  if(d.action!=='HOLD'){loadTrades();loadAnalytics();}
}
async function fetchAndRenderPortfolio() {
  try{const s=await (await fetch(API+'/api/snapshot/'+(state.portfolioId||1))).json();if(s.portfolio){state.portfolioData=s.portfolio;renderPortStats(s.portfolio);renderDonut(s.portfolio);}}catch{}
}

/* ── Render ── */
function renderSigTab(ticker) {
  const sig=state.signals[ticker];
  if(!sig){
    ['sig-price','sig-value','sig-std','sig-shares'].forEach(id=>document.getElementById(id).textContent='--');
    document.getElementById('sig-ticker').textContent=ticker;
    const p=document.getElementById('sig-action');p.textContent='--';p.className='action-pill';return;
  }
  document.getElementById('sig-price').textContent='$'+Number(sig.price).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  document.getElementById('sig-ticker').textContent=sig.ticker+' · '+fmtTime(sig.timestamp);
  document.getElementById('sig-value').textContent=sig.signal!=null?sig.signal.toFixed(4):'--';
  document.getElementById('sig-std').textContent=sig.signal_std!=null?sig.signal_std.toFixed(4):'--';
  document.getElementById('sig-shares').textContent=sig.shares!=null?sig.shares.toFixed(4):'--';
  const p=document.getElementById('sig-action');p.textContent=sig.action||'--';p.className='action-pill '+(sig.action||'');
}
function switchSignalTab(ticker,btn){
  state.activeSignalTab=ticker;
  document.querySelectorAll('.signals-card .signal-tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');renderSigTab(ticker);
}
function renderPortStats(p) {
  if(!p)return;
  const total=p.total_value??p.balance??0;
  const cash=p.balance??0; const inv=p.total_invested??0;
  const pnl=p.pnl??(total-10000000); const pct=p.pnl_pct??((total-10000000)/10000000*100);
  document.getElementById('stat-total').textContent=fmtD(total);
  document.getElementById('stat-cash').textContent=fmtD(cash);
  document.getElementById('stat-invested').textContent=fmtD(inv);
  document.getElementById('stat-trades').textContent=(p.trades_today??0)+'/1000';
  const pe=document.getElementById('stat-pnl'); const pce=document.getElementById('stat-pnl-pct');
  pe.textContent=(pnl>=0?'+':'')+fmtD(pnl); pe.style.color=pnl>=0?'var(--green)':'var(--red)';
  pce.textContent=(pct>=0?'+':'')+Number(pct).toFixed(3)+'%'; pce.className='stat-sub '+(pnl>=0?'pos':'neg');
}
function renderDonut(p) {
  const chart=state.chartInstances.donut;
  if(!p||!chart)return;
  const total=p.total_value||p.balance||10000000; if(total<=0)return;
  let cash=p.balance||0,gcf=0,spy=0,btc=0;
  if(p.holdings){gcf=p.holdings['GC=F']?.market_value||0;spy=p.holdings['SPY']?.market_value||0;btc=p.holdings['BTC-USD']?.market_value||0;}
  else{gcf=(state.signals['GC=F']?.shares||0)*(state.prices['GC=F']||0);spy=(state.signals['SPY']?.shares||0)*(state.prices['SPY']||0);btc=(state.signals['BTC-USD']?.shares||0)*(state.prices['BTC-USD']||0);}
  const cp=cash/total*100,gp=gcf/total*100,sp=spy/total*100,bp=btc/total*100;
  const zero=gp+sp+bp===0;
  chart.data.datasets[0].data=zero?[100,0,0,0]:[Math.max(cp,0),Math.max(gp,0),Math.max(sp,0),Math.max(bp,0)];
  chart.update('none');
  renderDonutLegend(['Cash','Gold','SPY','BTC'],['#e4e6eb','#d4a843','#2563eb','#f7931a'],zero?[100,0,0,0]:[cp,gp,sp,bp]);
}
function renderTrades(trades) {
  const tb=document.getElementById('trades-tbody');
  document.getElementById('trades-count').textContent=trades.length+' trade'+(trades.length!==1?'s':'');
  if(!trades.length){tb.innerHTML='<tr class="empty-row"><td colspan="7">No trades yet</td></tr>';return;}
  const tc=t=>t==='GC=F'?'td-gold':t==='SPY'?'td-spy':'td-btc';
  const ac=a=>a==='BUY'?'td-buy':a==='SELL'?'td-sell':'td-hold';
  tb.innerHTML=trades.map(t=>'<tr><td>'+fmtTime(t.timestamp)+'</td><td class="'+tc(t.ticker)+'">'+t.ticker+'</td><td class="'+ac(t.action)+'">'+t.action+'</td><td>'+fmtD(t.price)+'</td><td>'+(t.quantity!=null?t.quantity.toFixed(4):'--')+'</td><td>'+(t.signal_value!=null?t.signal_value.toFixed(4):'--')+'</td><td>'+fmtD(t.total_value)+'</td></tr>').join('');
}

/* ── Charts ── */
const COPTS={responsive:true,maintainAspectRatio:false,interaction:{intersect:false,mode:'index'},plugins:{legend:{display:false},tooltip:{backgroundColor:'rgba(10,14,26,0.85)',titleColor:'#9ca3af',bodyColor:'#f9fafb',borderColor:'rgba(0,0,0,0.1)',borderWidth:1,padding:10,callbacks:{label:ctx=>` $${ctx.parsed.y.toLocaleString('en-US',{minimumFractionDigits:2})}`}}},scales:{x:{ticks:{display:false},grid:{display:false},border:{display:false}},y:{ticks:{color:'#9ca3af',font:{size:10,family:"'DM Mono',monospace"}},grid:{color:'rgba(0,0,0,0.04)'},border:{display:false}}}};
function initCharts(){
  state.chartInstances.portfolio=new Chart(document.getElementById('chart-portfolio').getContext('2d'),{type:'line',data:{labels:[],datasets:[{label:'P',data:[],borderColor:'#2563eb',backgroundColor:'rgba(37,99,235,0.07)',borderWidth:2,tension:0.4,fill:true,pointRadius:0}]},options:COPTS});
  state.chartInstances.price=new Chart(document.getElementById('chart-price').getContext('2d'),{type:'line',data:{labels:[],datasets:[{label:'P',data:[],borderColor:'#f7931a',backgroundColor:'rgba(247,147,26,0.07)',borderWidth:2,tension:0.3,fill:true,pointRadius:0}]},options:COPTS});
  state.chartInstances.donut=new Chart(document.getElementById('chart-donut').getContext('2d'),{type:'doughnut',data:{labels:['Cash','Gold','SPY','BTC'],datasets:[{data:[100,0,0,0],backgroundColor:['#e4e6eb','#d4a843','#2563eb','#f7931a'],borderWidth:0,hoverOffset:6}]},options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>` ${ctx.label}: ${ctx.parsed.toFixed(1)}%`}}}}});
  renderDonutLegend(['Cash','Gold','SPY','BTC'],['#e4e6eb','#d4a843','#2563eb','#f7931a'],[100,0,0,0]);
}
function updatePortChart(){
  const c=state.chartInstances.portfolio;if(!c||!state.portfolioHistory.length)return;
  c.data.labels=state.portfolioHistory.map(p=>fmtTime(p.time));
  c.data.datasets[0].data=state.portfolioHistory.map(p=>p.value);c.update('none');
}
function renderPriceChart(ticker){
  const c=state.chartInstances.price;const pts=state.charts[ticker]||[];if(!c)return;
  const col={'GC=F':'#d4a843','SPY':'#2563eb','BTC-USD':'#f7931a'}[ticker]||'#2563eb';
  c.data.labels=pts.map(p=>fmtTime(p.time));c.data.datasets[0].data=pts.map(p=>p.price);
  c.data.datasets[0].borderColor=col;c.data.datasets[0].backgroundColor=col+'12';c.update('none');
}
function switchPriceTab(ticker,btn){
  state.activePriceTab=ticker;
  document.querySelectorAll('.price-charts-card .signal-tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');renderPriceChart(ticker);
}
function renderDonutLegend(labels,colors,values){
  document.getElementById('donut-legend').innerHTML=labels.map((l,i)=>'<div class="donut-legend-item"><div class="donut-legend-dot" style="background:'+colors[i]+'"></div><span>'+l+' '+Number(values[i]||0).toFixed(1)+'%</span></div>').join('');
}

/* ── Utils ── */
function fmtD(val){if(val==null||isNaN(val))return '$--';return '$'+Number(val).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}
function fmtTime(iso){if(!iso)return '';try{return new Date(iso).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit'});}catch{return iso;}}

/* ── Logout ── */
function logout(){
  if(state.ws)state.ws.close();clearInterval(state.wsPingInterval);
  Object.values(state.chartInstances).forEach(c=>c?.destroy());
  Object.assign(state,{token:null,username:null,portfolioId:null,signals:{},prices:{},charts:{},portfolioHistory:[],portfolioData:null,chartInstances:{},lastSignalTimes:{},tradingPaused:false});
  state.selectedTickers.clear();showScreen('auth');
}

showScreen('auth');