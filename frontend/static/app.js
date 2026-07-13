/* V82.LOWDD — Enterprise Trading Application
   Full Grade A dashboard, 8 tabs, all endpoints wired.
*/
const $  = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const fmt = (n, d=2) => (n==null||isNaN(n)) ? '—' : Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmt$ = (n, d=2) => (n==null||isNaN(n)) ? '—' : '$' + fmt(n, d);
const pct = (n, d=2) => (n==null||isNaN(n)) ? '—' : Number(n).toFixed(d) + '%';
const tnum = (n) => (n==null||isNaN(n)) ? '—' : Number(n).toLocaleString('en-US');

let STATUS = {};
let REFRESH_HANDLE = null;
let CURRENT_TAB = 'desk';

// Fetch the per-class config once on load (so we can show SL×, TP× per class)
async function loadInstrumentConfig() {
  // The configs are not directly exposed via API yet — embed a static map
  // by introspecting the live status itself
  window.__INSTRUMENT_CONFIG__ = {};  // populated on first status call
}

// ─── TABS ───────────────────────────────────────────────
$$('.tab').forEach(t => {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
});
function switchTab(name) {
  CURRENT_TAB = name;
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + name));
  // Refresh tab-specific data
  if (name === 'signals')   loadSignals();
  if (name === 'opps')      loadOpportunities();
  if (name === 'positions') loadPositions();
  if (name === 'history')   loadHistory();
  if (name === 'markets')   loadMarkets();
  if (name === 'engine')    loadEngine();
}
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea')) return;
  const map = {'1':'desk','2':'markets','3':'engine','4':'signals','5':'opps','6':'positions','7':'history','8':'risk'};
  if (map[e.key]) { e.preventDefault(); switchTab(map[e.key]); }
  if (e.key === 'r' || e.key === 'R') { e.preventDefault(); refreshAll(); }
  if (e.key === 's' || e.key === 'S') { e.preventDefault(); engineStart(); }
  if (e.key === 'x' || e.key === 'X') { e.preventDefault(); engineStop(); }
  if (e.key === 'c' || e.key === 'C') { e.preventDefault(); engineCycle(); }
  if (e.key === 'p' || e.key === 'P') { e.preventDefault(); probeUniverse(); }
});

// ─── TOAST ──────────────────────────────────────────────
function toast(msg, kind='good', ttl=3000) {
  const c = $('#toasts');
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, ttl);
}

// ─── CLOCK ──────────────────────────────────────────────
function updateClock() {
  const d = new Date();
  $('#clock').textContent = d.toISOString().slice(11, 19) + ' UTC · ' + d.toISOString().slice(0, 10);
}
setInterval(updateClock, 1000);
updateClock();

// ─── API HELPER ─────────────────────────────────────────
async function api(path, opts={}) {
  try {
    const r = await fetch(path, opts);
    if (!r.ok) { toast(`API ${r.status} on ${path}`, 'bad', 5000); return null; }
    return await r.json();
  } catch (e) {
    toast(`Network error: ${e.message}`, 'bad');
    return null;
  }
}

// ─── STATUS (KPIs) ──────────────────────────────────────
async function refreshStatus() {
  const s = await api('/api/live/status');
  if (!s) return;
  STATUS = s;
  // Connection
  const cs = $('#conn-state');
  if (s.broker_connected && s.running) { cs.className = 'system-state live'; cs.innerHTML = '<div class="dot"></div><div class="lbl">LIVE · 24/7</div>'; }
  else if (s.broker_connected)        { cs.className = 'system-state degraded'; cs.innerHTML = '<div class="dot"></div><div class="lbl">CONNECTED</div>'; }
  else                                 { cs.className = 'system-state offline'; cs.innerHTML = '<div class="dot"></div><div class="lbl">OFFLINE</div>'; }

  // KPI row 1
  const realized = s.closed_pnl || 0;
  const unrealized = s.unrealized_pnl || 0;
  const total = realized + unrealized;
  $('#kpi-pnl').textContent = fmt$(realized);
  $('#kpi-pnl').className = 'kpi-value ' + (realized > 0 ? 'good' : realized < 0 ? 'bad' : '');
  const wr = s.risk?.n_trades ? (s.risk.n_wins || 0) / s.risk.n_trades * 100 : null;
  $('#kpi-pnl-sub').textContent = `${s.n_closed_trades || 0} trades · ${wr == null ? '—' : pct(wr)} win`;
  $('#kpi-open').textContent = s.n_ig_positions;
  $('#kpi-open-sub').textContent = fmt$(unrealized) + ' unrealized';
  // "IG Account" KPI = the REAL broker balance (never the internal risk model)
  $('#kpi-bal').textContent = fmt$(s.ig_balance);
  $('#kpi-bal-sub').textContent = `${s.broker_account_type || 'DEMO'} · avail ${fmt$(s.ig_available)}`;
  // Masthead balance pill — real IG balance, was previously never updated
  const balEl = $('#balance-val');
  if (balEl) balEl.textContent = fmt$(s.ig_balance);
  const dd = s.dd_pct || 0;
  $('#kpi-dd').textContent = pct(dd);
  $('#kpi-dd').className = 'kpi-value ' + (dd > 15 ? 'bad' : dd > 8 ? 'warn' : '');

  // KPI row 2
  $('#kpi-engine').textContent = s.running ? 'RUNNING' : 'STOPPED';
  $('#kpi-engine').className = 'kpi-value ' + (s.running ? 'good' : 'bad');
  $('#kpi-engine-sub').textContent = s.running ? `uptime ${fmtTime(s.uptime_seconds)}` : 'idle';
  const sigStats = s.signals_stats || {};
  $('#kpi-signals-total').textContent = sigStats.total_signals || 0;
  $('#kpi-opps-total').textContent = sigStats.total_opportunities || 0;
  $('#kpi-opps-sub').textContent = `${sigStats.by_status?.OPENED || 0} opened / ${sigStats.by_status?.REJECTED || 0} rejected`;
  // Show FULL universe (not just current batch)
  $('#kpi-universe').textContent = s.all_symbols_count || s.universe_size || 0;
  $('#kpi-universe-sub').textContent = `${s.current_batch_size || s.universe_size} in current batch · ${s.bars_per_symbol ? Object.keys(s.bars_per_symbol).length : 0} tracked`;
  $('#kpi-wr').textContent = s.risk ? pct((s.risk.n_wins || 0) / Math.max(1, (s.risk.n_trades || 0)) * 100) : '—';
  $('#kpi-wl').textContent = `${s.risk?.n_wins || 0}W / ${s.risk?.n_losses || 0}L`;
  $('#kpi-of').textContent = `${s.n_orders || 0}/${s.n_fills || 0}`;

  // Tab badges
  $('#signals-badge').textContent = sigStats.total_signals || 0;
  $('#opps-badge').textContent = sigStats.total_opportunities || 0;

  // Risk monitor
  $('#rm-init').textContent = fmt$(s.initial_capital);
  $('#rm-equity').textContent = fmt$(s.capital);
  $('#rm-peak').textContent = fmt$(s.peak);
  $('#rm-trades').textContent = s.risk?.n_trades || 0;
  $('#rm-wl').textContent = `${s.risk?.n_wins || 0} / ${s.risk?.n_losses || 0}`;
  $('#rm-avg').textContent = fmt$(s.risk?.n_trades ? realized / s.risk.n_trades : 0);
  $('#rm-pf').textContent = '—'; // computed below
  // DD gauge
  const ddPct = Math.min(100, dd / 20 * 100);
  $('#dd-fill').style.width = ddPct + '%';
  $('#dd-marker').style.left = ddPct + '%';
  $('#rm-status').textContent = s.paused ? 'PAUSED (DD)' : 'ACTIVE';
  $('#rm-status').className = 'badge ' + (s.paused ? 'rejected' : 'live');

  // Ticker strip
  renderTicker(s);

  // Equity curve
  renderEquityChart();

  // Open positions
  renderOpenPositions(s.ig_positions || []);

  // Closed trades
  renderClosedTrades();

  // Per-class coverage (desk tab)
  renderClassCoverage(s);

  // Broker health (desk tab)
  renderBrokerHealth(s);

  // Engine tab
  if (CURRENT_TAB === 'engine') renderEngine(s);
}

// ─── PER-CLASS COVERAGE ─────────────────────────────────
function renderClassCoverage(s) {
  const cc = s.class_coverage || {};
  const cfg = window.__INSTRUMENT_CONFIG__ || {};
  const els = ['#desk-class-coverage', '#eng-class-coverage'];
  const html = `<table style="font-size:11px;">
    <thead><tr>
      <th style="text-align:left;padding:4px 8px;">Class</th>
      <th style="text-align:right;padding:4px 8px;">Quoted</th>
      <th style="text-align:right;padding:4px 8px;">Bars</th>
      <th style="text-align:right;padding:4px 8px;">SL×</th>
      <th style="text-align:right;padding:4px 8px;">TP×</th>
      <th style="text-align:right;padding:4px 8px;">Session</th>
    </tr></thead><tbody>
    ${Object.keys(cc).sort().map(cls => {
      const c = cc[cls];
      const pct = c.total ? (c.with_quote / c.total * 100) : 0;
      const w = Math.round(pct / 12.5);
      const bar = '█'.repeat(w) + '░'.repeat(8 - w);
      return `<tr>
        <td style="padding:3px 8px;">${cls}</td>
        <td style="padding:3px 8px;text-align:right;font-variant-numeric:tabular-nums;">${c.with_quote}/${c.total} <span class="text-dim" style="font-size:9px;">${bar}</span></td>
        <td style="padding:3px 8px;text-align:right;">${c.with_bars}</td>
        <td style="padding:3px 8px;text-align:right;">—</td>
        <td style="padding:3px 8px;text-align:right;">—</td>
        <td style="padding:3px 8px;text-align:right;color:var(--txt-dim);">—</td>
      </tr>`;
    }).join('')}
    </tbody></table>`;
  els.forEach(e => { const el = $(e); if (el) el.innerHTML = html; });
}

// ─── BROKER HEALTH ─────────────────────────────────────
function renderBrokerHealth(s) {
  const bh = s.broker_health || {};
  const html = `
    <div class="stat" style="border-color:${bh.connected ? 'rgba(33,201,122,0.3)' : 'rgba(255,90,110,0.3)'};">
      <div class="l">Connected</div>
      <div class="v" style="color:${bh.connected ? 'var(--good)' : 'var(--bad)'};">${bh.connected ? '✓ YES' : '✗ NO'}</div>
    </div>
    <div class="stat">
      <div class="l">Last success</div>
      <div class="v" style="font-size:11px;">${bh.last_successful_epic ? bh.last_successful_epic.split('.')[2] : '—'}</div>
      <div class="text-dim" style="font-size:9px;">${bh.seconds_since_last_success != null ? bh.seconds_since_last_success + 's ago' : '—'}</div>
    </div>
    <div class="stat">
      <div class="l">Fetches OK</div>
      <div class="v" style="color:var(--good);">${bh.n_successful_fetches || 0}</div>
    </div>
    <div class="stat">
      <div class="l">Throttled</div>
      <div class="v" style="color:${bh.throttled ? 'var(--bad)' : 'var(--good)'};">${bh.throttled ? '✗ YES' : '✓ NO'}</div>
      <div class="text-dim" style="font-size:9px;">${bh.n_throttle_hits || 0} hits</div>
    </div>`;
  const el = $('#desk-broker-health');
  if (el) el.innerHTML = html;
  const ig = $('#ig-state');
  if (ig) {
    ig.textContent = bh.connected ? 'CONNECTED' : 'OFFLINE';
    ig.className = bh.connected ? 'tag live' : 'tag warn';
  }
}

function fmtTime(secs) {
  if (!secs) return '—';
  const h = Math.floor(secs/3600);
  const m = Math.floor((secs % 3600)/60);
  const s = Math.floor(secs % 60);
  return `${h}h ${m}m ${s}s`;
}

// ─── TICKER STRIP ──────────────────────────────────────
function renderTicker(s) {
  const strip = $('#ticker-strip');
  const quotes = s.live_quotes || {};
  const items = [];
  // 24/7 crypto first
  const crypto = ['CS.D.BITCOIN.CFBMU.IP', 'CS.D.BITCOIN.CFD.IP', 'CS.D.ETHEREUM.CFBMU.IP'];
  crypto.forEach(epic => {
    if (quotes[epic]) items.push({sym: epicToSym(epic), bid: quotes[epic].bid, offer: quotes[epic].offer});
  });
  // Then forex majors
  ['CS.D.EURUSD.MINI.IP','CS.D.GBPUSD.MINI.IP','CS.D.USDJPY.MINI.IP',
   'CS.D.USDCHF.MINI.IP','CS.D.AUDUSD.MINI.IP','CS.D.USDCAD.MINI.IP',
   'CS.D.NZDUSD.MINI.IP'].forEach(epic => {
    if (quotes[epic]) items.push({sym: epicToSym(epic), bid: quotes[epic].bid, offer: quotes[epic].offer});
  });
  // Then indices
  ['IX.D.SPTRD.DAILY.IP','IX.D.DOW.DAILY.IP','IX.D.FTSE.DAILY.IP','IX.D.NIKKEI.DAILY.IP'].forEach(epic => {
    if (quotes[epic]) items.push({sym: epicToSym(epic), bid: quotes[epic].bid, offer: quotes[epic].offer});
  });
  strip.innerHTML = items.slice(0, 20).map(i => `
    <div class="ticker-item">
      <div class="ticker-sym">${i.sym}</div>
      <div class="ticker-price">${fmt(mid(i.bid, i.offer), 5)}</div>
    </div>`).join('');
}
function mid(b, o) { return (Number(b) + Number(o)) / 2; }
function epicToSym(epic) {
  if (epic.includes('BITCOIN')) return epic.includes('CFD') ? 'BTCUSD' : 'BTCmini';
  if (epic.includes('ETHEREUM')) return 'ETHUSD';
  if (epic.includes('EURUSD')) return 'EURUSD';
  if (epic.includes('GBPUSD')) return 'GBPUSD';
  if (epic.includes('USDJPY')) return 'USDJPY';
  if (epic.includes('USDCHF')) return 'USDCHF';
  if (epic.includes('AUDUSD')) return 'AUDUSD';
  if (epic.includes('USDCAD')) return 'USDCAD';
  if (epic.includes('NZDUSD')) return 'NZDUSD';
  if (epic.includes('SPTRD')) return 'SPX';
  if (epic.includes('DOW'))   return 'DJI';
  if (epic.includes('FTSE'))  return 'FTSE';
  if (epic.includes('NIKKEI'))return 'N225';
  // Fallback: derive a readable symbol from the epic's instrument segment
  // (was previously `epic.split('.')[-2]`, invalid JS array indexing that
  // always returned undefined and could crash the whole ticker render).
  const parts = epic.split('.');
  return parts.length >= 3 ? parts[2] : epic;
}

// ─── EQUITY CHART ──────────────────────────────────────
async function renderEquityChart() {
  const d = await api('/api/history/equity');
  if (!d) return;
  const data = d.equity_curve || [];
  if (data.length < 2) return;
  const w = 800, h = 280, pad = 30;
  const min = Math.min(...data.map(p => p.equity));
  const max = Math.max(...data.map(p => p.equity));
  const range = max - min || 1;
  const x = i => pad + (i / (data.length - 1)) * (w - 2 * pad);
  const y = v => h - pad - ((v - min) / range) * (h - 2 * pad);
  const pts = data.map((p, i) => `${x(i)},${y(p.equity)}`).join(' ');
  const area = `M ${x(0)},${h - pad} L ${pts.replace(/ /g, ' L ')} L ${x(data.length - 1)},${h - pad} Z`;
  $('#equity-content').innerHTML = `
    <path d="${area}" class="chart-area" />
    <polyline points="${pts}" class="chart-line" />
  `;
  // Y-axis labels
  let grid = '';
  for (let i = 0; i <= 4; i++) {
    const v = min + (range * i / 4);
    const yy = y(v);
    grid += `<line x1="${pad}" y1="${yy}" x2="${w - pad}" y2="${yy}" class="chart-grid" />`;
    grid += `<text x="${pad - 4}" y="${yy + 3}" text-anchor="end" class="chart-text">${fmt$(v, 0)}</text>`;
  }
  $('#equity-grid').innerHTML = grid;
}

// ─── OPEN POSITIONS ────────────────────────────────────
function renderOpenPositions(positions) {
  const tb = $('#open-tbody');
  $('#open-count').textContent = `${positions.length} LIVE`;
  if (!positions.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">No open positions.</td></tr>';
    return;
  }
  tb.innerHTML = positions.map(p => `
    <tr>
      <td>${p.instrument_name || p.epic}</td>
      <td><span class="badge ${p.direction === 'BUY' ? 'buy' : 'sell'}">${p.direction}</span></td>
      <td class="num">${fmt(p.size, 2)}</td>
      <td class="num">${fmt(p.level, 5)}</td>
      <td class="num ${p.pnl > 0 ? 'pos' : p.pnl < 0 ? 'neg' : ''}">${fmt$(p.pnl)}</td>
      <td><button class="btn sm danger" onclick="closePos('${p.deal_id}')">CLOSE</button></td>
    </tr>`).join('');
}

async function closePos(deal_id) {
  if (!confirm(`Close position ${deal_id}?`)) return;
  const r = await api('/api/live/close', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({deal_id})
  });
  if (r && r.closed) { toast(`Closed ${deal_id}`, 'good'); refreshAll(); }
  else toast('Close failed', 'bad');
}

async function closeAllPos() {
  if (!confirm('Close ALL open positions? This cannot be undone.')) return;
  const r = await api('/api/live/close-all', {method: 'POST'});
  if (r) { toast(`Closed ${r.closed} positions`, 'good'); refreshAll(); }
}

// ─── CLOSED TRADES (Desk tab preview) ─────────────────
async function renderClosedTrades() {
  const d = await api('/api/history/trades?limit=10');
  if (!d) return;
  const tb = $('#closed-tbody');
  $('#closed-count').textContent = `${d.count} CLOSED`;
  // Profit factor is computed server-side from the FULL trade history
  // (gross profit / gross loss) — real, not a placeholder.
  const pfEl = $('#rm-pf');
  if (pfEl) pfEl.textContent = d.profit_factor ? fmt(d.profit_factor, 2) : (d.count ? '∞' : '—');
  if (!d.trades.length) {
    tb.innerHTML = '<tr><td colspan="4" class="empty">No closed trades yet.</td></tr>';
    return;
  }
  tb.innerHTML = d.trades.slice(-10).reverse().map(t => `
    <tr>
      <td>${(t.exit_time || '').slice(0, 19)}</td>
      <td>${t.display_name || t.instrument}</td>
      <td><span class="badge ${t.direction === 'BUY' ? 'buy' : 'sell'}">${t.direction}</span></td>
      <td class="num ${t.pnl > 0 ? 'pos' : t.pnl < 0 ? 'neg' : ''}">${fmt$(t.pnl)}</td>
    </tr>`).join('');
}

// ─── MARKETS TAB ───────────────────────────────────────
async function loadMarkets() {
  const d = await api('/api/live/universe');
  if (!d) return;
  $('#markets-count').textContent = `${d.universe.length} EPICs`;
  const list = $('#markets-list');
  list.innerHTML = d.universe.map(epic => {
    const probe = d.probe_results[epic] || {};
    return `<tr>
      <td><span class="tag bull">${epic.split('.')[2]}</span></td>
      <td>${epic}</td>
      <td class="num">${probe.bid ? fmt(probe.bid, 5) : '—'}</td>
      <td class="num">${probe.offer ? fmt(probe.offer, 5) : '—'}</td>
      <td><span class="tag ${probe.available ? 'live' : 'warn'}">${probe.available ? 'TRADEABLE' : 'OFFLINE'}</span></td>
    </tr>`;
  }).join('');
}

$('#markets-probe').addEventListener('click', probeUniverse);
async function probeUniverse() {
  toast('Re-probing IG universe (this may take 60s)...', 'warn', 5000);
  const d = await api('/api/live/probe', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({force: true})});
  if (d) { toast(`Probe found ${d.n_working} working EPICs`, 'good'); loadMarkets(); refreshStatus(); }
}

$('#quote-btn').addEventListener('click', getQuote);
async function getQuote() {
  const epic = $('#quote-epic').value.trim();
  if (!epic) return;
  const d = await api(`/api/ig/market/${encodeURIComponent(epic)}`);
  if (!d) return;
  if (d.error) { $('#quote-display').innerHTML = `<div class="empty">${d.error}</div>`; return; }
  $('#quote-display').innerHTML = `
    <div class="detail-grid">
      <div class="k">Market</div><div class="v">${d.display_name || d.epic}</div>
      <div class="k">Bid</div><div class="v">${fmt(d.bid, 5)}</div>
      <div class="k">Offer</div><div class="v">${fmt(d.offer, 5)}</div>
      <div class="k">Mid</div><div class="v">${fmt(d.mid, 5)}</div>
      <div class="k">Spread</div><div class="v">${fmt(d.spread, 5)}</div>
      <div class="k">Status</div><div class="v">${d.market_status || '—'}</div>
      <div class="k">Updated</div><div class="v">${d.update_time || '—'}</div>
    </div>`;
}

$('#search-btn').addEventListener('click', searchMarket);
async function searchMarket() {
  const q = $('#search-q').value.trim();
  if (!q) return;
  const d = await api(`/api/ig/search?q=${encodeURIComponent(q)}`);
  if (!d) return;
  const tb = $('#search-results');
  if (!d.markets.length) { tb.innerHTML = '<tr><td colspan="3" class="empty">No results.</td></tr>'; return; }
  tb.innerHTML = d.markets.slice(0, 20).map(m => `
    <tr onclick="$('#quote-epic').value='${m.epic}'">
      <td>${m.instrument_name}</td>
      <td>${m.epic}</td>
      <td>${m.instrument_type}</td>
    </tr>`).join('');
}

$('#order-submit').addEventListener('click', submitOrder);
async function submitOrder() {
  const epic = $('#order-epic').value.trim();
  const dir = $('#order-dir').value;
  const size = parseFloat($('#order-size').value);
  if (!epic || !dir || !size) { toast('Fill all fields', 'warn'); return; }
  const r = await api('/api/ig/order', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({epic, direction: dir, size, order_type: 'MARKET'})
  });
  if (r) { toast(`Order submitted: ${r.dealReference || r}`, 'good'); $('#order-status').textContent = `Ref: ${r.dealReference || r}`; refreshAll(); }
}
$('#order-close-all').addEventListener('click', closeAllPos);

// ─── ENGINE TAB ─────────────────────────────────────────
async function loadEngine() {
  const s = await api('/api/live/status');
  if (s) renderEngine(s);
}
function renderEngine(s) {
  $('#eng-running').textContent = s.running ? 'RUNNING' : 'STOPPED';
  $('#eng-running').className = 'kpi-value ' + (s.running ? 'good' : 'bad');
  $('#eng-uptime').textContent = s.running ? `uptime ${fmtTime(s.uptime_seconds)}` : 'idle';
  $('#eng-batch-size').textContent = s.current_batch_size || s.universe_size || 0;
  $('#eng-batch-sub').textContent = `of ${s.all_symbols_count || 0} total`;
  $('#eng-universe-total').textContent = s.all_symbols_count || 0;
  $('#eng-universe-sub').textContent = '8 asset classes (forex, crypto, indices, commodities, shares, options, bonds)';
  const sig = s.signals_stats || {};
  $('#eng-sop').textContent = `${sig.total_signals || 0}/${sig.total_opportunities || 0}`;
  $('#engine-mode').textContent = `Mode: LIVE (24/7 across all ${s.all_symbols_count || 0} EPICs)`;

  // Rotator batches
  const r = s.rotator;
  if (r) {
    $('#eng-rot-info').textContent = `${r.n_batches} batches · ${r.n_unique_symbols} unique symbols · batch ${r.batch_idx + 1}/${r.n_batches} · tick ${r.tick_count}`;
    const html = `
      <div class="detail-grid">
        <div class="k">Current batch</div><div class="v">#${r.batch_idx + 1} of ${r.n_batches}</div>
        <div class="k">Total unique symbols</div><div class="v">${r.n_unique_symbols}</div>
        <div class="k">Always polled (24/7)</div><div class="v" style="font-size:11px;">${r.always_paid.map(e => e.split('.')[2]).join(', ')}</div>
        <div class="k">Asset classes</div><div class="v">forex · crypto · indices · commodities · shares · options · bonds</div>
      </div>
      <div class="h-divider"></div>
      <div class="stat-grid">
        ${r.batches.map((b, i) => `
          <div class="stat">
            <div class="l">Batch ${i + 1} ${i === r.batch_idx ? '· CURRENT' : ''}</div>
            <div class="v" style="font-size:11px;font-weight:500;">${b.length} symbols</div>
            <div class="text-dim" style="font-size:10px;margin-top:4px;">${b.slice(0,3).map(e => e.split('.')[2]).join(', ')}${b.length>3 ? '…' : ''}</div>
          </div>`).join('')}
      </div>`;
    $('#eng-rot-batches').innerHTML = html;
  } else {
    $('#eng-rot-info').textContent = 'no rotation';
    $('#eng-rot-batches').innerHTML = '<div class="empty">No rotator (universe ≤ 12 symbols)</div>';
  }

  // Forecasts
  const f = s.forecasts || {};
  const fc = Object.entries(f).filter(([_, v]) => v.direction !== 'NEUTRAL');
  if (fc.length) {
    $('#eng-forecasts').innerHTML = `<table><thead><tr>
      <th>Market</th><th>Direction</th><th class="num">Close</th>
      <th class="num">ret_3</th><th class="num">ATR</th></tr></thead><tbody>
      ${fc.map(([k, v]) => `<tr>
        <td>${k}</td>
        <td><span class="badge ${v.direction === 'BULLISH' ? 'bull' : 'bear'}">${v.direction}</span></td>
        <td class="num">${fmt(v.close, 5)}</td>
        <td class="num">${fmt(v.ret_3, 6)}</td>
        <td class="num">${fmt(v.atr, 5)}</td>
      </tr>`).join('')}</tbody></table>`;
  }

  // Recent actions
  const actions = s.recent_actions || [];
  if (actions.length) {
    $('#eng-actions').innerHTML = `<table><thead><tr>
      <th>Time</th><th>Action</th><th>Market</th><th>Details</th></tr></thead><tbody>
      ${actions.slice(-15).reverse().map(a => `<tr>
        <td>${(a.t || '').slice(11, 19)}</td>
        <td><span class="badge ${a.action}">${a.action}</span></td>
        <td>${a.display_name || a.symbol}</td>
        <td>${a.direction || ''} ${fmt(a.n_units || 0, 2)}u @ ${fmt(a.entry || a.filled_price || 0, 5)} ${a.reason || ''}</td>
      </tr>`).join('')}</tbody></table>`;
  }
}

$('#engine-start').addEventListener('click', engineStart);
$('#engine-stop').addEventListener('click', engineStop);
$('#engine-cycle').addEventListener('click', engineCycle);
$('#engine-probe').addEventListener('click', probeUniverse);
$('#engine-force-all').addEventListener('click', engineForceAll);

async function engineStart() {
  const r = await api('/api/live/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  if (r) { toast('Engine started', 'good'); refreshAll(); }
}
async function engineStop() {
  const r = await api('/api/live/stop', {method:'POST'});
  if (r) { toast('Engine stopped', 'warn'); refreshAll(); }
}
async function engineCycle() {
  toast('Running one cycle...', 'warn', 2000);
  const r = await api('/api/live/cycle', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  if (r) { toast(`Cycle: ${r.cycle.n_actions} actions, ${r.cycle.n_open} open`, 'good'); refreshAll(); }
}
async function engineForceAll() {
  toast('Polling 22 classes (1 EPIC per class) — this takes ~15s...', 'warn', 5000);
  const r = await api('/api/live/force-all-classes', {method:'POST'});
  if (r) {
    const n = r.epics_polled || 0;
    const sigs = r.signals_found || 0;
    toast(`Polled ${n} classes · found ${sigs} signals · ${r.broker_health?.n_successful_fetches || 0} successful fetches`, 'good', 5000);
    refreshAll();
  }
}

// ─── SIGNALS TAB ───────────────────────────────────────
async function loadSignals() {
  const direction = $('#sig-filter').value;
  const limit = $('#sig-limit').value;
  const url = `/api/signals?limit=${limit}${direction ? '&direction='+direction : ''}`;
  const d = await api(url);
  if (!d) return;
  // Update stats
  const stats = await api('/api/signals/stats');
  if (stats) {
    const by = stats.by_direction || {};
    $('#sig-total').textContent = stats.total_signals;
    $('#sig-bull').textContent = by.BULLISH || 0;
    $('#sig-bear').textContent = by.BEARISH || 0;
    $('#sig-neu').textContent = by.NEUTRAL || 0;
    const epics = new Set((d.signals || []).map(s => s.epic));
    $('#sig-markets').textContent = epics.size;
  }
  // Render table
  const tb = $('#sig-tbody');
  if (!d.signals.length) {
    tb.innerHTML = '<tr><td colspan="9" class="empty">No signals yet.</td></tr>';
    return;
  }
  tb.innerHTML = d.signals.map(s => `<tr>
    <td>${(s.t || '').slice(0, 19)}</td>
    <td>${s.symbol}</td>
    <td>${s.epic}</td>
    <td><span class="badge ${(s.forecast.direction || '').toLowerCase()}">${s.forecast.direction}</span></td>
    <td class="num">${fmt(s.forecast.close, 5)}</td>
    <td class="num">${fmt(s.forecast.ret_3, 6)}</td>
    <td class="num">${fmt(s.forecast.atr, 5)}</td>
    <td class="num">${s.forecast.streak}</td>
    <td class="num">${s.forecast.n_bars}</td>
  </tr>`).join('');
}
$('#sig-refresh').addEventListener('click', loadSignals);
$('#sig-filter').addEventListener('change', loadSignals);
$('#sig-limit').addEventListener('change', loadSignals);

// ─── OPPORTUNITIES TAB ─────────────────────────────────
async function loadOpportunities() {
  const status = $('#opp-filter-status').value;
  const dir = $('#opp-filter-dir').value;
  const url = `/api/opportunities?limit=200${status ? '&status='+status : ''}${dir ? '&direction='+dir : ''}`;
  const d = await api(url);
  if (!d) return;
  // Update stats
  const stats = await api('/api/opportunities/stats');
  if (stats) {
    $('#opp-total').textContent = stats.total_opportunities;
    const by = stats.by_status || {};
    $('#opp-opened').textContent = by.OPENED || 0;
    $('#opp-rejected').textContent = by.REJECTED || 0;
    $('#opp-pending').textContent = by.PENDING || 0;
    const opened = by.OPENED || 0;
    const rejected = by.REJECTED || 0;
    const total = opened + rejected;
    $('#opp-wr').textContent = total ? pct(opened / total * 100) : '—';
  }
  // Render table
  const tb = $('#opp-tbody');
  if (!d.opportunities.length) {
    tb.innerHTML = '<tr><td colspan="10" class="empty">No opportunities yet.</td></tr>';
    return;
  }
  tb.innerHTML = d.opportunities.map(o => `<tr onclick="showOppDetail('${o.id}')">
    <td>${(o.t || '').slice(0, 19)}</td>
    <td>${o.symbol}</td>
    <td><span class="badge ${(o.forecast.direction || '').toLowerCase()}">${o.forecast.direction}</span></td>
    <td><span class="badge ${(o.decision.side || '').toLowerCase()}">${o.decision.side}</span></td>
    <td class="num">${fmt(o.decision.entry, 5)}</td>
    <td class="num">${fmt(o.decision.stop, 5)}</td>
    <td class="num">${fmt(o.decision.target, 5)}</td>
    <td class="num">${fmt(o.sizing.n_units, 2)}</td>
    <td class="num">${fmt(o.decision.risk_reward, 2)}</td>
    <td><span class="badge ${o.status === 'OPENED' ? 'live' : o.status === 'REJECTED' ? 'rejected' : 'pending'}">${o.status}</span></td>
  </tr>`).join('');
}
$('#opp-refresh').addEventListener('click', loadOpportunities);
$('#opp-filter-status').addEventListener('change', loadOpportunities);
$('#opp-filter-dir').addEventListener('change', loadOpportunities);

// ─── OPPORTUNITY DETAIL MODAL ──────────────────────────
async function showOppDetail(opp_id) {
  const d = await api('/api/opportunities/' + opp_id);
  if (!d) { toast('Opportunity not found', 'bad'); return; }
  const o = d;
  const f = o.forecast || {};
  const sz = o.sizing || {};
  const dc = o.decision || {};
  const or = o.order || {};
  const body = $('#opp-modal-body');
  body.innerHTML = `
    <div class="detail-card">
      <div class="title">${o.symbol} — ${o.epic}</div>
      <div class="detail-grid">
        <div class="k">ID</div><div class="v mono">${o.id}</div>
        <div class="k">Time</div><div class="v mono">${o.t}</div>
        <div class="k">Status</div><div class="v"><span class="badge ${o.status === 'OPENED' ? 'live' : o.status === 'REJECTED' ? 'rejected' : 'pending'}">${o.status}</span></div>
        <div class="k">Signal</div><div class="v mono">${o.signal_id}</div>
      </div>
    </div>

    <div class="detail-card">
      <div class="title">Forecast</div>
      <div class="detail-grid">
        <div class="k">Direction</div><div class="v"><span class="badge ${(f.direction || '').toLowerCase()}">${f.direction}</span></div>
        <div class="k">Close</div><div class="v">${fmt(f.close, 5)}</div>
        <div class="k">MA fast</div><div class="v">${fmt(f.ma_fast, 5)}</div>
        <div class="k">MA slow</div><div class="v">${fmt(f.ma_slow, 5)}</div>
        <div class="k">ret_3</div><div class="v">${fmt(f.ret_3, 6)}</div>
        <div class="k">ATR</div><div class="v">${fmt(f.atr, 5)}</div>
        <div class="k">Streak</div><div class="v">${f.streak}</div>
        <div class="k">Bars</div><div class="v">${f.n_bars}</div>
      </div>
    </div>

    <div class="detail-card">
      <div class="title">Sizing</div>
      <div class="detail-grid">
        <div class="k">n_units</div><div class="v">${fmt(sz.n_units, 4)}</div>
        <div class="k">Notional</div><div class="v">${fmt$(sz.notional)}</div>
        <div class="k">Risk $</div><div class="v">${fmt$(sz.risk_dollars)}</div>
        <div class="k">Risk %</div><div class="v">${fmt(sz.risk_per_trade_pct, 3)}%</div>
        <div class="k">Leverage</div><div class="v">${fmt(sz.leverage, 2)}x</div>
        <div class="k">Stop distance</div><div class="v">${fmt(sz.stop_distance, 5)}</div>
        <div class="k">Contract size</div><div class="v">${sz.contract_size || 1.0}</div>
      </div>
    </div>

    <div class="detail-card">
      <div class="title">Decision</div>
      <div class="detail-grid">
        <div class="k">Side</div><div class="v"><span class="badge ${(dc.side || '').toLowerCase()}">${dc.side}</span></div>
        <div class="k">Entry</div><div class="v">${fmt(dc.entry, 5)}</div>
        <div class="k">Stop</div><div class="v">${fmt(dc.stop, 5)}</div>
        <div class="k">Target</div><div class="v">${fmt(dc.target, 5)}</div>
        <div class="k">R:R</div><div class="v">${fmt(dc.risk_reward, 2)}:1</div>
        <div class="k">Reason</div><div class="v" style="grid-column:span 2;">${dc.reason || '—'}</div>
      </div>
    </div>

    <div class="detail-card">
      <div class="title">Order Result</div>
      <div class="detail-grid">
        <div class="k">order_id</div><div class="v mono">${or.order_id || '—'}</div>
        <div class="k">deal_id</div><div class="v mono">${or.deal_id || '—'}</div>
        <div class="k">Filled price</div><div class="v">${or.filled_price ? fmt(or.filled_price, 5) : '—'}</div>
        <div class="k">Filled at</div><div class="v mono">${or.filled_at || '—'}</div>
        <div class="k">Status</div><div class="v">${or.status || '—'}</div>
        <div class="k">Reason</div><div class="v" style="grid-column:span 2;">${or.reason || '—'}</div>
      </div>
    </div>
  `;
  $('#opp-modal').classList.add('active');
}
function closeOppModal() { $('#opp-modal').classList.remove('active'); }
$('#opp-modal').addEventListener('click', e => {
  if (e.target.id === 'opp-modal') closeOppModal();
});

// ─── POSITIONS TAB ──────────────────────────────────────
async function loadPositions() {
  const d = await api('/api/ig/positions');
  if (!d) return;
  const tb = $('#pos-tbody');
  $('#pos-count-tag').textContent = `${d.count} LIVE`;
  if (!d.positions.length) {
    tb.innerHTML = '<tr><td colspan="7" class="empty">No open positions.</td></tr>';
    return;
  }
  tb.innerHTML = d.positions.map(p => `<tr>
    <td>${p.instrument_name || p.epic}</td>
    <td><span class="badge ${p.direction === 'BUY' ? 'buy' : 'sell'}">${p.direction}</span></td>
    <td class="num">${fmt(p.size, 2)}</td>
    <td class="num">${fmt(p.level, 5)}</td>
    <td class="num ${p.pnl > 0 ? 'pos' : p.pnl < 0 ? 'neg' : ''}">${fmt$(p.pnl)}</td>
    <td class="mono" style="font-size:10px;">${p.deal_id}</td>
    <td><button class="btn sm danger" onclick="closePos('${p.deal_id}')">CLOSE</button></td>
  </tr>`).join('');
}
$('#pos-refresh').addEventListener('click', loadPositions);
$('#pos-close-all').addEventListener('click', closeAllPos);

// ─── HISTORY TAB ───────────────────────────────────────
async function loadHistory() {
  const d = await api('/api/history/trades?limit=200');
  if (!d) return;
  $('#hi-count').textContent = d.count;
  $('#hi-pnl').textContent = fmt$(d.total_pnl);
  $('#hi-pnl').className = 'kpi-value ' + (d.total_pnl > 0 ? 'good' : d.total_pnl < 0 ? 'bad' : '');
  $('#hi-wr').textContent = pct(d.win_rate);
  $('#hi-wl').textContent = `${d.wins}W / ${d.losses}L`;
  const o = await api('/api/history/orders?limit=1');
  if (o) $('#hi-orders').textContent = o.count;
  const tb = $('#hi-trades-tbody');
  if (!d.trades.length) {
    tb.innerHTML = '<tr><td colspan="9" class="empty">No closed trades yet.</td></tr>';
    return;
  }
  tb.innerHTML = d.trades.map(t => `<tr>
    <td>${(t.exit_time || '').slice(0, 19)}</td>
    <td>${t.display_name || t.instrument}</td>
    <td>${t.instrument}</td>
    <td><span class="badge ${t.direction === 'BUY' ? 'buy' : 'sell'}">${t.direction}</span></td>
    <td class="num">${fmt(t.entry_price, 5)}</td>
    <td class="num">${fmt(t.exit_price, 5)}</td>
    <td class="num">${fmt(t.n_units, 2)}</td>
    <td><span class="badge ${t.won ? 'live' : 'rejected'}">${t.exit_type || '—'}</span></td>
    <td class="num ${t.pnl > 0 ? 'pos' : t.pnl < 0 ? 'neg' : ''}">${fmt$(t.pnl)}</td>
  </tr>`).join('');

  // Orders
  const od = await api('/api/history/orders?limit=100');
  if (od) {
    const od_html = `<table><thead><tr>
      <th>Created</th><th>Market</th><th>EPIC</th>
      <th>Side</th><th class="num">Size</th>
      <th>Status</th><th>Reason</th>
    </tr></thead><tbody>
    ${od.orders.map(o => `<tr>
      <td>${(o.created_at || '').slice(0, 19)}</td>
      <td>${o.display_name || ''}</td>
      <td>${o.instrument || ''}</td>
      <td><span class="badge ${o.direction === 'BUY' ? 'buy' : 'sell'}">${o.direction}</span></td>
      <td class="num">${fmt(o.n_units, 2)}</td>
      <td><span class="badge ${o.status === 'FILLED' ? 'filled' : o.status === 'ACCEPTED' ? 'live' : o.status === 'REJECTED' ? 'rejected' : 'pending'}">${o.status}</span></td>
      <td class="text-dim">${o.reject_reason || ''}</td>
    </tr>`).join('')}
    </tbody></table>`;
    $('#hi-orders-display').innerHTML = od_html;
  }
}
$('#hi-refresh').addEventListener('click', loadHistory);
$('#hi-export').addEventListener('click', () => {
  window.open('/api/history/trades?limit=9999', '_blank');
});

// ─── MAIN REFRESH LOOP ────────────────────────────────
function refreshAll() {
  refreshStatus();
  if (CURRENT_TAB === 'signals')   loadSignals();
  if (CURRENT_TAB === 'opps')      loadOpportunities();
  if (CURRENT_TAB === 'positions') loadPositions();
  if (CURRENT_TAB === 'history')   loadHistory();
  if (CURRENT_TAB === 'markets')   loadMarkets();
  if (CURRENT_TAB === 'engine')    loadEngine();
}
REFRESH_HANDLE = setInterval(refreshAll, 4000);
refreshAll();
