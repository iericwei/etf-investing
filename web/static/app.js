let APP_CONFIG = {
  selectionPollIntervalMs: 2500,
  holdingsRefreshSeconds: 120,
  holdingsCountdownIntervalMs: 1000,
  autoRefreshIntervalMs: 600000,
  autoRefreshStartMinute: 565,
  autoRefreshEndMinute: 905,
};

async function loadRuntimeConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const cfg = await res.json();
    APP_CONFIG = Object.assign(APP_CONFIG, cfg || {});
  } catch (_) {}
}
const CAT_COLORS = {
  '宽基':'#3b82f6','科技':'#8b5cf6','新能源':'#10b981',
  '医药':'#ef4444','金融':'#f59e0b','军工':'#6b7280',
  '商品':'#d97706','港股':'#ec4899','海外':'#14b8a6',
  '消费':'#84cc16','红利':'#a855f7',
};

let _timer = null;
let _allResults = [];
let _activeCat = '全部';
let _holdings = new Set();
let _holdingsTimer = null;
let _holdingsCountdown = null;
let _holdingsSecs = 0;
let _activeSellWrap = null;
let _floatingSellTip = null;
let _sortField = null;
let _sortDir = 'desc';

const SORT_LABELS = {
  change_pct: '今日',
  ret3: '3日',
  ret5: '5日',
  ret10: '10日',
  backtest_return_pct: '回测1月',
  score: '评分',
};

function pct(v) {
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}
function pctCls(v) {
  return v > 0.05 ? 'pos' : v < -0.05 ? 'neg' : 'neu';
}
function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
}
function rsiCls(v) {
  return v > 75 ? 'rsi-hot' : v > 65 ? 'rsi-warm' : v < 35 ? 'rsi-cold' : 'rsi-good';
}
function catBadge(c) {
  const col = CAT_COLORS[c] || '#8b949e';
  return `<span class="cat" style="background:${col}22;color:${col};border:1px solid ${col}44">${c}</span>`;
}
function signals(r) {
  let s = '';
  if (r.ma_aligned)      s += '<span class="sig s-ma">↑均线多头</span>';
  if (r.macd_bullish)    s += '<span class="sig s-macd">⚡ MACD</span>';
  if (r.vol_ratio>=1.5)  s += `<span class="sig s-vol">🔥 ${r.vol_ratio.toFixed(1)}x</span>`;
  if (r.rsi < 45)        s += '<span class="sig s-rsi">超卖回弹</span>';
  return `<div class="sigs">${s || '<span style="color:#8b949e;font-size:11px">—</span>'}</div>`;
}
function tradeSignalBadge(sig) {
  if (!sig || !sig.action) return '<span class="trade-badge trade-hold">观望</span>';
  const cls = sig.action === 'buy' ? 'trade-buy' : sig.action === 'sell' ? 'trade-sell' : 'trade-hold';
  const buyRows = (sig.buy_signals || []).map(s =>
    `<div class="sig-item"><span>${s.name}</span><span class="sig-lv-${s.level}">${s.level}</span></div>`
  ).join('');
  const sellRows = (((sig.sell_signals || {}).signals) || []).map(s =>
    `<div class="sig-item"><span>${s.name}</span><span class="sig-lv-${s.level}">${s.level}</span></div>`
  ).join('');
  const tip = (buyRows || sellRows)
    ? `<div class="tip-content">${buyRows}${sellRows ? '<div class="tip-sep">卖出风险</div>' + sellRows : ''}</div>`
    : '';
  return `<div class="trade-wrap"><span class="trade-badge ${cls}">${sig.label || sig.action}</span>${tip}</div>`;
}
function backtestCell(r) {
  if (r.backtest_return_pct == null) return '<span class="muted">—</span>';
  const bt = r.backtest || {};
  const curve = Array.isArray(bt.curve) ? bt.curve : [];
  const points = Array.isArray(bt.trade_points) ? bt.trade_points : [];
  if (!curve.length) return `<span class="${pctCls(r.backtest_return_pct)}">${pct(r.backtest_return_pct)}</span>`;

  const w = 250, h = 86, pad = 8;
  const returns = curve.map(p => Number(p.return_pct)).filter(Number.isFinite);
  const min = Math.min(...returns, 0);
  const max = Math.max(...returns, 0);
  const span = Math.max(max - min, 0.01);
  const xAt = i => pad + (curve.length <= 1 ? 0 : i * (w - pad * 2) / (curve.length - 1));
  const yAt = v => pad + (max - Number(v)) * (h - pad * 2) / span;
  const line = curve.map((p, i) => `${xAt(i).toFixed(1)},${yAt(p.return_pct).toFixed(1)}`).join(' ');
  const zeroY = yAt(0).toFixed(1);
  const markers = points.map(tp => {
    const idx = Math.max(0, curve.findIndex(p => p.date === tp.date));
    const p = curve[idx] || curve[0];
    const cls = tp.action === 'buy' ? 'bt-buy' : 'bt-sell';
    const label = tp.action === 'buy' ? 'B' : 'S';
    return `<g class="${cls}"><circle cx="${xAt(idx).toFixed(1)}" cy="${yAt(p.return_pct).toFixed(1)}" r="4"></circle><text x="${xAt(idx).toFixed(1)}" y="${(yAt(p.return_pct)+3).toFixed(1)}">${label}</text></g>`;
  }).join('');
  const tradeRows = points.length ? points.map(tp => `
    <div class="bt-trade ${tp.action === 'buy' ? 'bt-buy-text' : 'bt-sell-text'}">
      <div><b>${esc(tp.label || tp.action)}</b> ${esc(tp.date)} @ ${Number(tp.price).toFixed(3)}</div>
      <div class="bt-reason">${esc(tp.reason)}，当时收益 ${pct(Number(tp.return_pct) || 0)}</div>
    </div>`).join('') : '<div class="muted">回测期内未触发买卖点</div>';
  return `
    <div class="backtest-wrap">
      <span class="${pctCls(r.backtest_return_pct)}">${pct(r.backtest_return_pct)}</span>
      <div class="tip-content backtest-tip">
        <div class="bt-title">${esc(r.name)} 最近${bt.window_days || 22}日回测：${pct(r.backtest_return_pct)}</div>
        <svg class="bt-chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="回测收益曲线">
          <line x1="${pad}" y1="${zeroY}" x2="${w-pad}" y2="${zeroY}" class="bt-zero"></line>
          <polyline points="${line}" class="bt-line"></polyline>
          ${markers}
        </svg>
        <div class="bt-axis"><span>${esc(curve[0].date)}</span><span>${esc(curve[curve.length - 1].date)}</span></div>
        <div class="tip-sep">买卖点明细</div>
        ${tradeRows}
      </div>
    </div>`;
}
function sellBadge(sell) {
  if (!sell || sell.urgency_level == null) {
    return '<span class="sell-badge sell-na">暂无数据</span>';
  }
  const lvl = sell.urgency_level;
  const cls = lvl < 0 ? 'sell-na' : `sell-${lvl}`;
  const sigs = sell.signals || [];
  const tipRows = sigs.map(s =>
    `<div class="sig-item"><span>${s.name}</span><span class="sig-lv-${s.level}">${s.level}</span></div>`
  ).join('');
  const tip = sigs.length
    ? `<div class="tip-content">${tipRows}</div>`
    : '';
  return `<div class="sell-wrap"><span class="sell-badge ${cls}">${sell.urgency || '—'}</span>${tip}</div>`;
}
function rankCls(n) { return n===1?'r1':n===2?'r2':n===3?'r3':''; }

function hideSellTip() {
  if (_floatingSellTip) {
    _floatingSellTip.remove();
    _floatingSellTip = null;
  }
  _activeSellWrap = null;
}

function showSellTip(wrap) {
  const src = wrap.querySelector('.tip-content');
  if (!src) return;
  if (_activeSellWrap === wrap && _floatingSellTip) return;

  hideSellTip();
  _activeSellWrap = wrap;
  _floatingSellTip = src.cloneNode(true);
  _floatingSellTip.classList.add('floating-tip');
  document.body.appendChild(_floatingSellTip);

  const gap = 6;
  const margin = 8;
  const rect = wrap.getBoundingClientRect();
  const tipRect = _floatingSellTip.getBoundingClientRect();
  let left = rect.right - tipRect.width;
  let top = rect.bottom + gap;

  if (top + tipRect.height > window.innerHeight - margin) {
    top = rect.top - tipRect.height - gap;
  }
  left = Math.max(margin, Math.min(left, window.innerWidth - tipRect.width - margin));
  top = Math.max(margin, Math.min(top, window.innerHeight - tipRect.height - margin));

  _floatingSellTip.style.left = `${left}px`;
  _floatingSellTip.style.top = `${top}px`;
}

document.addEventListener('mouseover', e => {
  const wrap = e.target.closest('.sell-wrap, .trade-wrap, .backtest-wrap');
  if (!wrap || !wrap.contains(e.target)) return;
  showSellTip(wrap);
});

document.addEventListener('mouseout', e => {
  const wrap = e.target.closest('.sell-wrap, .trade-wrap, .backtest-wrap');
  if (!wrap) return;
  if (e.relatedTarget && wrap.contains(e.relatedTarget)) return;
  hideSellTip();
});

window.addEventListener('scroll', hideSellTip, true);
window.addEventListener('resize', hideSellTip);

/* ── 持仓管理 ────────────────────────────────────────────────────── */
async function loadHoldings() {
  try {
    const d = await (await fetch('/api/holdings')).json();
    _holdings = new Set(d.holdings || []);
  } catch(e) {}
}

async function toggleHolding(code) {
  try {
    const d = await (await fetch('/api/holdings/toggle', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({code}),
    })).json();
    _holdings = new Set(d.holdings || []);
    applyHoldings();
    buildTabs(_allResults);   // 更新持仓 Tab 计数
  } catch(e) {}
}

function holdBtn(code) {
  const active = _holdings.has(code);
  return `<button class="btn-hold${active?' active':''}"
    onclick="event.stopPropagation();toggleHolding('${code}')"
    title="${active?'取消持仓':'标记持仓'}">
    ${active ? '✓ 持仓' : '+ 持仓'}
  </button>`;
}

function applyHoldings() {
  // 更新所有行的高亮和按钮状态
  document.querySelectorAll('tbody tr[data-code]').forEach(tr => {
    const code = tr.dataset.code;
    const active = _holdings.has(code);
    tr.classList.toggle('holding', active);
    const btn = tr.querySelector('.btn-hold');
    if (btn) {
      btn.className = 'btn-hold' + (active ? ' active' : '');
      btn.textContent = active ? '✓ 持仓' : '+ 持仓';
    }
  });
}

/* ── 持仓面板 ────────────────────────────────────────────────────── */
async function refreshHoldings() {
  try {
    hideSellTip();
    const d = await (await fetch('/api/holdings/realtime')).json();
    if (d.timestamp) document.getElementById('hpTs').textContent = '更新于 ' + d.timestamp;
    const items = d.data || [];
    const body = document.getElementById('hpBody');
    if (!items.length) {
      body.innerHTML = '<div class="hp-empty">暂无持仓标的，在列表中点击「+ 持仓」添加</div>';
      return;
    }
    body.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>代码</th><th>名称</th><th>类别</th>
            <th class="r">实时价</th><th class="r">涨跌幅</th>
            <th class="r">成交额</th><th style="text-align:center">榜单</th>
            <th style="text-align:center">卖出信号</th>
            <th style="text-align:center">操作</th>
          </tr></thead>
          <tbody>
            ${items.map(r => `
              <tr data-code="${r.code}">
                <td><span class="code">${r.code}</span></td>
                <td>${r.name}</td>
                <td>${catBadge(r.category)}</td>
                <td class="r">${r.price > 0 ? r.price.toFixed(3) : '—'}</td>
                <td class="r ${pctCls(r.change_pct)}">${r.price > 0 ? pct(r.change_pct) : '—'}</td>
                <td class="r">${r.amount > 0 ? (r.amount/1e8).toFixed(2)+'亿' : '—'}</td>
                <td style="text-align:center">
                  ${r.rank ? `<span class="rank-badge">#${r.rank}</span>` : '<span style="color:var(--muted);font-size:11px">未入榜</span>'}
                </td>
                <td style="text-align:center">${sellBadge(r.sell_signals)}</td>
                <td style="text-align:center">
                  <button class="btn-hold active" onclick="toggleHolding('${r.code}')">移除</button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch(e) { console.error(e); }
}

function _tickCountdown() {
  _holdingsSecs = Math.max(0, _holdingsSecs - 1);
  const el = document.getElementById('hpCountdown');
  if (el) el.textContent = _holdingsSecs > 0 ? `(${_holdingsSecs}秒后刷新)` : '';
}

function startHoldingsTimer() {
  clearInterval(_holdingsTimer);
  clearInterval(_holdingsCountdown);
  _holdingsSecs = 120;
  refreshHoldings();
  _holdingsTimer = setInterval(() => {
    _holdingsSecs = APP_CONFIG.holdingsRefreshSeconds;
    refreshHoldings();
  }, APP_CONFIG.holdingsRefreshSeconds * 1000);
  _holdingsCountdown = setInterval(_tickCountdown, APP_CONFIG.holdingsCountdownIntervalMs);
}

function stopHoldingsTimer() {
  clearInterval(_holdingsTimer);
  clearInterval(_holdingsCountdown);
  _holdingsTimer = null;
  _holdingsCountdown = null;
  const el = document.getElementById('hpCountdown');
  if (el) el.textContent = '';
}

function scoreCell(r) {
  const w = Math.min(r.score, 100);
  return `
    <div class="score-cell score-tip">
      <div class="bar-bg"><div class="bar-fill" style="width:${w}%"></div></div>
      <div class="score-val">${r.score.toFixed(1)}</div>
      <div class="tip-content factor-tip">
        <div class="tip-row"><span class="tip-label factor-help">动量 (35%)<small>3日/5日涨跌幅，衡量短期价格强弱</small></span><span class="tip-val">${r.momentum_score.toFixed(1)}</span></div>
        <div class="tip-row"><span class="tip-label factor-help">量能 (25%)<small>量比 × 短期涨幅，衡量放量上涨协同</small></span><span class="tip-val">${r.volume_score.toFixed(1)}</span></div>
        <div class="tip-row"><span class="tip-label factor-help">技术 (25%)<small>RSI 健康度、MACD、均线结构综合评分</small></span><span class="tip-val">${r.technical_score.toFixed(1)}</span></div>
        <div class="tip-row"><span class="tip-label factor-help">趋势 (15%)<small>10日涨跌幅，衡量更长一点的趋势延续</small></span><span class="tip-val">${(r.trend_score || 0).toFixed(1)}</span></div>
        <div class="tip-row total"><span class="tip-label">综合得分</span><span class="tip-val">${r.score.toFixed(1)}</span></div>
      </div>
    </div>`;
}

function numericValue(row, field) {
  const value = row[field];
  if (value == null || Number.isNaN(Number(value))) return null;
  return Number(value);
}

function sortRows(list) {
  const rows = [...list];
  if (!_sortField) return rows;
  const dir = _sortDir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const av = numericValue(a, _sortField);
    const bv = numericValue(b, _sortField);
    if (av == null && bv == null) return a.rank - b.rank;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (av === bv) return a.rank - b.rank;
    return (av - bv) * dir;
  });
  return rows;
}

function currentList() {
  if (_activeCat === '全部') return _allResults;
  return _allResults.filter(r => r.category === _activeCat);
}

function sortBy(field) {
  if (_sortField === field) {
    _sortDir = _sortDir === 'desc' ? 'asc' : 'desc';
  } else {
    _sortField = field;
    _sortDir = 'desc';
  }
  renderRows(currentList());
}

function updateSortHeaders() {
  document.querySelectorAll('th.sortable').forEach(th => {
    const field = th.dataset.sort;
    const label = SORT_LABELS[field] || th.textContent.replace(/[↓↑↕]/g, '').trim();
    th.classList.toggle('active', field === _sortField);
    const icon = field === _sortField ? (_sortDir === 'desc' ? '↓' : '↑') : '↕';
    th.innerHTML = `${label}<span class="sort-icon">${icon}</span>`;
  });
}

function buildTabs(results) {
  const order = ['全部', '持仓'];
  const counts = {'全部': results.length, '持仓': _holdings.size};
  for (const r of results) {
    if (!counts[r.category]) { order.push(r.category); counts[r.category] = 0; }
    counts[r.category]++;
  }
  document.getElementById('tabBar').style.display = 'block';
  document.getElementById('tabInner').innerHTML = order.map(cat =>
    `<div class="tab${cat === _activeCat ? ' active' : ''}" onclick="selectTab('${cat}')">
       ${cat}<span class="badge">${counts[cat] ?? 0}</span>
     </div>`
  ).join('');
}

function selectTab(cat) {
  stopHoldingsTimer();
  _activeCat = cat;
  document.querySelectorAll('.tab').forEach(el => {
    el.classList.toggle('active', el.textContent.trim().startsWith(cat));
  });

  const table   = document.querySelector('.table-wrap');
  const hpPanel = document.getElementById('holdings-panel');

  if (cat === '持仓') {
    table.style.display   = 'none';
    hpPanel.style.display = 'block';
    startHoldingsTimer();
  } else {
    table.style.display   = '';
    hpPanel.style.display = 'none';
    const filtered = currentList();
    renderRows(filtered);
  }
}

function renderRows(list) {
  const rows = sortRows(list);
  document.getElementById('tbody').innerHTML = rows.map(r => `
    <tr data-code="${r.code}" class="${_holdings.has(r.code) ? 'holding' : ''}">
      <td class="r"><span class="rank ${rankCls(r.rank)}">#${r.rank}</span></td>
      <td><span class="code">${r.code}</span></td>
      <td>${r.name}</td>
      <td>${catBadge(r.category)}</td>
      <td class="r">${r.price.toFixed(3)}</td>
      <td class="r ${pctCls(r.change_pct)}">${pct(r.change_pct)}</td>
      <td class="r ${pctCls(r.ret3)}">${pct(r.ret3)}</td>
      <td class="r ${pctCls(r.ret5)}">${pct(r.ret5)}</td>
      <td class="r ${pctCls(r.ret10)}">${pct(r.ret10)}</td>
      <td class="r ${rsiCls(r.rsi)}">${r.rsi.toFixed(1)}</td>
      <td class="r">${r.vol_ratio.toFixed(2)}</td>
      <td>${signals(r)}</td>
      <td style="text-align:center">${tradeSignalBadge(r.trade_signal)}</td>
      <td class="r">${backtestCell(r)}</td>
      <td class="r">${scoreCell(r)}</td>
      <td style="text-align:center">${holdBtn(r.code)}</td>
    </tr>`).join('');
  updateSortHeaders();
}

function render(data) {
  _allResults = data.results || [];
  document.getElementById('sTotal').textContent    = data.universe_total ? data.universe_total + '只' : '—';
  document.getElementById('sScanned').textContent  = data.scanned ? data.scanned + '只' : '—';
  document.getElementById('sSelected').textContent = _allResults.length;
  document.getElementById('sTop').textContent      = _allResults.length ? _allResults[0].score.toFixed(1) : '—';
  const sStatus = document.getElementById('sStatus');
  sStatus.textContent = '实时'; sStatus.style.color = '#3fb950';
  if (data.timestamp) document.getElementById('updateTime').textContent = '更新于 ' + data.timestamp;
  if (data.date)      document.getElementById('dateChip').textContent   = data.date;

  _activeCat = '全部';
  buildTabs(_allResults);
  renderRows(_allResults);

  document.getElementById('loading').style.display       = 'none';
  document.getElementById('error-box').style.display     = 'none';
  document.getElementById('table-section').style.display = 'block';
  document.getElementById('btnRefresh').disabled = false;
}

function showError(msg) {
  clearInterval(_timer); _timer = null;
  document.getElementById('loading').style.display   = 'none';
  document.getElementById('table-section').style.display = 'none';
  const eb = document.getElementById('error-box');
  eb.style.display = 'block';
  eb.textContent = '获取失败：' + msg;
  const s = document.getElementById('sStatus');
  s.textContent = '错误'; s.style.color = '#f85149';
  document.getElementById('btnRefresh').disabled = false;
}

async function poll() {
  try {
    const res  = await fetch('/api/select');
    const data = await res.json();
    if (data.status === 'ready') {
      clearInterval(_timer); _timer = null;
      render(data);
    } else if (data.status === 'error') {
      showError(data.error || '未知错误');
    } else {
      // loading: update progress hint
      const msg = _loadMsgs[Math.min(_pollCount, _loadMsgs.length - 1)];
      document.getElementById('loadMsg').textContent = msg;
      _pollCount++;
    }
  } catch(e) { showError(e.message); }
}

const _loadMsgs = [
  '正在获取全市场 ETF 列表…',
  '流动性筛选，并发拉取历史行情…',
  '获取实时价格数据…',
  '运行多因子评分模型…',
  '即将完成，请稍候…',
];
let _pollCount = 0;

function startPolling() {
  document.getElementById('loading').style.display       = 'flex';
  document.getElementById('table-section').style.display = 'none';
  document.getElementById('error-box').style.display     = 'none';
  const s = document.getElementById('sStatus');
  s.textContent = '加载中…'; s.style.color = '#e3b341';
  _pollCount = 0;
  clearInterval(_timer);
  poll();
  _timer = setInterval(poll, APP_CONFIG.selectionPollIntervalMs);
}

async function doRefresh() {
  document.getElementById('btnRefresh').disabled = true;
  await fetch('/api/refresh').catch(()=>{});
  startPolling();
}

function startAutoRefresh() {
  setInterval(() => {
    const m = new Date(); const t = m.getHours()*60 + m.getMinutes();
    if (t >= APP_CONFIG.autoRefreshStartMinute && t <= APP_CONFIG.autoRefreshEndMinute) doRefresh();
  }, APP_CONFIG.autoRefreshIntervalMs);
}

async function bootstrap() {
  await loadRuntimeConfig();
  startAutoRefresh();
  await loadHoldings();
  startPolling();
}

bootstrap();
