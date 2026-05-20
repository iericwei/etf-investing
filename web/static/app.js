let APP_CONFIG = {
  selectionPollIntervalMs: 2500,
  holdingsRefreshSeconds: 120,
  holdingsCountdownIntervalMs: 1000,
  holdingsMarketCheckIntervalMs: 60000,
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
let _watchlist = new Set();
let _holdingsTimer = null;
let _holdingsCountdown = null;
let _holdingsMarketCheck = null;
let _holdingsSecs = 0;
let _activeSellWrap = null;
let _floatingSellTip = null;
let _sortField = null;
let _sortDir = 'desc';
const CUSTOM_TAB = '自选';

const SORT_LABELS = {
  change_pct: '今日',
  fund_size: '规模',
  premium_rate_pct: '折溢价',
  ret3: '3日',
  ret5: '5日',
  ret10: '10日',
  backtest_return_pct: '回测1月',
  score: '评分',
  rsi: 'RSI',
  vol_ratio: '量比',
  signal_sort: '模型信号',
};

function pct(v) {
  v = Number(v);
  if (!Number.isFinite(v)) return '—';
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}
function pctCls(v) {
  v = Number(v);
  if (!Number.isFinite(v)) return 'neu';
  return v > 0.05 ? 'pos' : v < -0.05 ? 'neg' : 'neu';
}
function num(v, digits = 2) {
  v = Number(v);
  return Number.isFinite(v) ? v.toFixed(digits) : '—';
}
function moneyYi(v) {
  v = Number(v);
  return Number.isFinite(v) && v > 0 ? (v / 1e8).toFixed(1) + '亿' : '—';
}
function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
}
function quoteMarketPrefix(code) {
  const c = String(code == null ? '' : code).trim();
  return /^[569]/.test(c) ? 'sh' : 'sz';
}
function quoteUrl(code) {
  const c = String(code == null ? '' : code).trim();
  if (!/^\d{6}$/.test(c)) return 'https://gu.qq.com/';
  return `https://gu.qq.com/${quoteMarketPrefix(c)}${c}`;
}
function quoteLink(row, text, cls = '') {
  const code = row && row.code;
  const label = esc(text == null ? code : text);
  const klass = cls ? ` class="${esc(cls)}"` : '';
  return `<a${klass} href="${esc(quoteUrl(code))}" target="_blank" rel="noopener noreferrer" title="在腾讯证券打开行情页">${label}</a>`;
}
function fundMetaHtml(r) {
  const premiumTitle = `估算净值：${num(r.estimate_nav, 4)}${r.nav_date ? ' · ' + esc(r.nav_date) : ''}`;
  return `<div class="fund-meta">
    <span title="基金规模">规模 ${moneyYi(r.fund_size)}</span>
    <span class="${pctCls(r.premium_rate_pct)}" title="${premiumTitle}">折溢价 ${pct(r.premium_rate_pct)}</span>
  </div>`;
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
  const tradeRows = points.length ? points.map(tp => {
    const sourceLabel = tp.price_source_label || (tp.price_source === 'akshare_15m' ? 'akshare 15分钟分时行情价' : (tp.price_source === 'close' ? '日K收盘价' : tp.price_source));
    const sourceNote = sourceLabel ? `<span class="muted">（价格来源：${esc(sourceLabel)}）</span>` : '';
    return `
    <div class="bt-trade ${tp.action === 'buy' ? 'bt-buy-text' : 'bt-sell-text'}">
      <div><b>${esc(tp.label || tp.action)}</b> ${esc(tp.date)} ${tp.time ? esc(tp.time) : ''} @ ${Number(tp.price).toFixed(3)} ${sourceNote}</div>
      <div class="bt-reason">${esc(tp.reason)}，当时收益 ${pct(Number(tp.return_pct) || 0)}</div>
    </div>`;
  }).join('') : '<div class="muted">回测期内未触发买卖点</div>';
  const schemeName = bt.scheme_display_name || bt.trade_timing_label || '回测';
  const priceNote = bt.price_note ? `<div class="muted">${esc(bt.price_note)}</div>` : '';
  return `
    <div class="backtest-wrap">
      <span class="${pctCls(r.backtest_return_pct)}">${pct(r.backtest_return_pct)}</span>
      <div class="tip-content backtest-tip">
        <div class="bt-title">${esc(r.name)} ${esc(schemeName)} 最近${bt.window_days || 22}日回测：${pct(r.backtest_return_pct)}</div>
        <svg class="bt-chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="回测收益曲线">
          <line x1="${pad}" y1="${zeroY}" x2="${w-pad}" y2="${zeroY}" class="bt-zero"></line>
          <polyline points="${line}" class="bt-line"></polyline>
          ${markers}
        </svg>
        <div class="bt-axis"><span>${esc(curve[0].date)}</span><span>${esc(curve[curve.length - 1].date)}</span></div>
        <div class="tip-sep">买卖点明细</div>
        ${tradeRows}
        ${priceNote}
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
function signalChangesHtml(r) {
  const changes = Array.isArray(r.signal_changes) ? r.signal_changes : [];
  if (!changes.length) return '';
  const fields = changes.map(c => c.field).filter(Boolean).join('、') || '信号';
  const text = `${fields}有变更`;
  return `<div class="signal-change" title="${esc(text)}">${esc(text)}</div>`;
}
function rankCls(n) { return n===1?'r1':n===2?'r2':n===3?'r3':''; }
function rankCell(r) {
  return r.is_custom ? '<span class="rank custom-rank">自选</span>' : `<span class="rank ${rankCls(r.rank)}">#${r.rank}</span>`;
}
function removeWatchBtn(r) {
  if (!r.is_custom) return '';
  const disabled = _holdings.has(r.code);
  return `<button class="btn-remove-custom" ${disabled ? 'disabled title="持仓中的标的不能从榜单移除"' : `onclick="event.stopPropagation();removeWatchlist('${r.code}')" title="从榜单移除"`}>移除</button>`;
}
function actionBtns(r) {
  return `<div class="row-actions">${holdBtn(r.code)}${removeWatchBtn(r)}</div>`;
}

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
    renderRows(currentList());
  } catch(e) {}
}

async function loadWatchlist() {
  try {
    const d = await (await fetch('/api/watchlist')).json();
    _watchlist = new Set(d.watchlist || []);
  } catch(e) {}
}

async function addWatchlist() {
  const input = document.getElementById('watchCode');
  const code = (input?.value || '').trim();
  if (!/^\d{6}$/.test(code)) {
    alert('请输入 6 位 ETF 代码');
    return;
  }
  const res = await fetch('/api/watchlist', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({code}),
  }).catch(() => null);
  if (!res) return;
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) {
    alert(d.error || '添加失败');
    return;
  }
  _watchlist = new Set(d.watchlist || []);
  if (input) input.value = '';
  await reloadSelectOnce();
}

async function removeWatchlist(code) {
  const res = await fetch(`/api/watchlist/${code}`, {method: 'DELETE'}).catch(() => null);
  if (!res) return;
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) {
    alert(d.error || '移除失败');
    return;
  }
  _watchlist = new Set(d.watchlist || []);
  _allResults = _allResults.filter(r => !(r.code === code && r.is_custom));
  buildTabs(_allResults);
  renderRows(currentList());
}

async function reloadSelectOnce(preserveActiveCat = false) {
  try {
    const data = await (await fetch('/api/select')).json();
    if (data.status === 'ready') render(data, preserveActiveCat);
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
async function loadMarketStatus() {
  try {
    const res = await fetch('/api/market/status');
    if (!res.ok) throw new Error('market status unavailable');
    return await res.json();
  } catch (_) {
    const m = new Date();
    const t = m.getHours() * 60 + m.getMinutes();
    const day = m.getDay();
    const allowed = day !== 0 && day !== 6 && t >= APP_CONFIG.autoRefreshStartMinute && t <= APP_CONFIG.autoRefreshEndMinute;
    return {
      auto_refresh_allowed: allowed,
      is_trading_day: day !== 0 && day !== 6,
      reason: allowed ? '交易时段' : (day === 0 || day === 6 ? '节假日/非交易日' : '非交易时段'),
    };
  }
}

function setHoldingsPauseMessage(status) {
  const el = document.getElementById('hpCountdown');
  if (!el) return;
  const reason = status?.reason || '非交易时段';
  el.textContent = `(自动刷新已暂停：${reason})`;
}

async function refreshHoldingsAuto() {
  const status = await loadMarketStatus();
  if (!status.auto_refresh_allowed) {
    clearInterval(_holdingsTimer);
    clearInterval(_holdingsCountdown);
    _holdingsTimer = null;
    _holdingsCountdown = null;
    _holdingsSecs = 0;
    setHoldingsPauseMessage(status);
    return false;
  }
  _holdingsSecs = APP_CONFIG.holdingsRefreshSeconds;
  await refreshHoldings();
  return true;
}

async function refreshHoldings() {
  try {
    hideSellTip();
    const d = await (await fetch('/api/holdings/realtime')).json();
    await reloadSelectOnce(true);
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
            <th>代码</th><th>名称 / 规模折溢价</th><th>类别</th>
            <th class="r">实时价</th><th class="r">涨跌幅</th>
            <th class="r">成交额</th><th style="text-align:center">榜单</th>
            <th style="text-align:center">模型信号</th>
            <th style="text-align:center">卖出信号</th>
            <th style="text-align:center">操作</th>
          </tr></thead>
          <tbody>
            ${items.map(r => `
              <tr data-code="${r.code}">
                <td>${quoteLink(r, r.code, 'code quote-link')}</td>
                <td class="name-cell">${quoteLink(r, r.name, 'quote-link')}${fundMetaHtml(r)}</td>
                <td>${catBadge(r.category)}</td>
                <td class="r">${r.price > 0 ? r.price.toFixed(3) : '—'}</td>
                <td class="r ${pctCls(r.change_pct)}">${r.price > 0 ? pct(r.change_pct) : '—'}</td>
                <td class="r">${r.amount > 0 ? (r.amount/1e8).toFixed(2)+'亿' : '—'}</td>
                <td style="text-align:center">
                  ${r.rank ? `<span class="rank-badge">#${r.rank}</span>` : '<span style="color:var(--muted);font-size:11px">未入榜</span>'}
                </td>
                <td style="text-align:center">${tradeSignalBadge(r.trade_signal)}${signalChangesHtml(r)}</td>
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

async function startHoldingsTimer() {
  stopHoldingsTimer();
  const active = await refreshHoldingsAuto();
  if (active) {
    _holdingsTimer = setInterval(refreshHoldingsAuto, APP_CONFIG.holdingsRefreshSeconds * 1000);
    _holdingsCountdown = setInterval(_tickCountdown, APP_CONFIG.holdingsCountdownIntervalMs);
  }
  _holdingsMarketCheck = setInterval(async () => {
    if (_activeCat !== '持仓') return;
    if (_holdingsTimer) return;
    const resumed = await refreshHoldingsAuto();
    if (resumed) {
      _holdingsTimer = setInterval(refreshHoldingsAuto, APP_CONFIG.holdingsRefreshSeconds * 1000);
      _holdingsCountdown = setInterval(_tickCountdown, APP_CONFIG.holdingsCountdownIntervalMs);
    }
  }, APP_CONFIG.holdingsMarketCheckIntervalMs);
}

function stopHoldingsTimer() {
  clearInterval(_holdingsTimer);
  clearInterval(_holdingsCountdown);
  clearInterval(_holdingsMarketCheck);
  _holdingsTimer = null;
  _holdingsCountdown = null;
  _holdingsMarketCheck = null;
  const el = document.getElementById('hpCountdown');
  if (el) el.textContent = '';
}

function scoreCell(r) {
  const score = Number(r.score);
  if (!Number.isFinite(score)) return '<span class="muted">—</span>';
  const momentum = Number(r.momentum_score);
  const volume = Number(r.volume_score);
  const technical = Number(r.technical_score);
  const trend = Number(r.trend_score || 0);
  const w = Math.min(score, 100);
  return `
    <div class="score-cell score-tip">
      <div class="bar-bg"><div class="bar-fill" style="width:${w}%"></div></div>
      <div class="score-val">${score.toFixed(1)}</div>
      <div class="tip-content factor-tip">
        <div class="tip-row"><span class="tip-label factor-help">动量 (35%)<small>3日/5日涨跌幅，衡量短期价格强弱</small></span><span class="tip-val">${Number.isFinite(momentum) ? momentum.toFixed(1) : '—'}</span></div>
        <div class="tip-row"><span class="tip-label factor-help">量能 (25%)<small>量比 × 短期涨幅，衡量放量上涨协同</small></span><span class="tip-val">${Number.isFinite(volume) ? volume.toFixed(1) : '—'}</span></div>
        <div class="tip-row"><span class="tip-label factor-help">技术 (25%)<small>RSI 健康度、MACD、均线结构综合评分</small></span><span class="tip-val">${Number.isFinite(technical) ? technical.toFixed(1) : '—'}</span></div>
        <div class="tip-row"><span class="tip-label factor-help">趋势 (15%)<small>10日涨跌幅，衡量更长一点的趋势延续</small></span><span class="tip-val">${Number.isFinite(trend) ? trend.toFixed(1) : '—'}</span></div>
        <div class="tip-row total"><span class="tip-label">综合得分</span><span class="tip-val">${score.toFixed(1)}</span></div>
      </div>
    </div>`;
}

function dataRows(list) {
  return (list || []).filter(r => r && !r._is_group_header);
}

function targetGroupName(row) {
  const fallback = row && row.category ? String(row.category) : '其他';
  let text = row && row.name ? String(row.name).trim() : '';
  if (!text) return fallback;
  text = text.replace(/[\s（）()\-_/]+/g, '');
  const target = text.replace(/(ETF|ＥＴＦ|LOF|QDII|联接|基金|指数|增强|优选).*$/i, '').trim();
  return target || text || fallback;
}

function numericValue(row, field) {
  const value = row[field];
  if (value == null || Number.isNaN(Number(value))) return null;
  return Number(value);
}

function sortRows(list) {
  const rows = dataRows(list);
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
  if (_activeCat === CUSTOM_TAB) return dataRows(_allResults).filter(r => r.is_custom);
  return dataRows(_allResults).filter(r => r.category === _activeCat);
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
  const rows = dataRows(results);
  const customCount = rows.filter(r => r.is_custom).length;
  const primary = ['全部', CUSTOM_TAB, '持仓'];
  const extra = [];
  const counts = {'全部': rows.length, [CUSTOM_TAB]: customCount, '持仓': _holdings.size};
  for (const r of rows) {
    if (r.category === CUSTOM_TAB) continue;
    if (!counts[r.category]) { extra.push(r.category); counts[r.category] = 0; }
    counts[r.category]++;
  }
  document.getElementById('tabBar').style.display = 'block';
  const primaryHtml = primary.map(cat =>
    `<div class="tab${cat === _activeCat ? ' active' : ''}" onclick="selectTab('${cat}')">
       ${cat}<span class="badge">${counts[cat] ?? 0}</span>
     </div>`
  ).join('');
  const extraHtml = extra.length ? `
    <select id="categorySelect" class="tab-select${extra.includes(_activeCat) ? ' active' : ''}" onchange="if(this.value) selectTab(this.value)">
      <option value="">其他分类</option>
      ${extra.map(cat => `<option value="${esc(cat)}" ${cat === _activeCat ? 'selected' : ''}>${esc(cat)} (${counts[cat] ?? 0})</option>`).join('')}
    </select>` : '';
  document.getElementById('tabInner').innerHTML = primaryHtml + extraHtml;
}

function selectTab(cat) {
  stopHoldingsTimer();
  _activeCat = cat;
  document.querySelectorAll('.tab').forEach(el => {
    el.classList.toggle('active', el.textContent.trim().startsWith(cat));
  });
  const categorySelect = document.getElementById('categorySelect');
  if (categorySelect) {
    const hasOption = Array.from(categorySelect.options || []).some(opt => opt.value === cat);
    categorySelect.value = hasOption ? cat : '';
    categorySelect.classList.toggle('active', hasOption);
  }

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
  const sorted = sortRows(list);
  // 当用户未手动排序时，按分组插入标题行
  const rows = _sortField
    ? sorted
    : insertGroupHeaders(list);
  document.getElementById('tbody').innerHTML = rows.map(r => {
    if (r._is_group_header) {
      return `<tr class="group-header" data-cat="${esc(r.category)}"><td colspan="16">${esc(r.category)}</td></tr>`;
    }
    return `<tr data-code="${r.code}" class="${_holdings.has(r.code) ? 'holding' : ''}">
      <td class="r">${rankCell(r)}</td>
      <td>${quoteLink(r, r.code, 'code quote-link')}</td>
      <td class="name-cell">${quoteLink(r, r.name, 'quote-link')}${fundMetaHtml(r)}</td>
      <td>${catBadge(r.category || '自选')}</td>
      <td class="r">${num(r.price, 3)}</td>
      <td class="r ${pctCls(r.change_pct)}">${pct(r.change_pct)}</td>
      <td class="r ${pctCls(r.ret3)}">${pct(r.ret3)}</td>
      <td class="r ${pctCls(r.ret5)}">${pct(r.ret5)}</td>
      <td class="r ${pctCls(r.ret10)}">${pct(r.ret10)}</td>
      <td class="r ${rsiCls(Number(r.rsi) || 0)}">${num(r.rsi, 1)}</td>
      <td class="r">${num(r.vol_ratio, 2)}</td>
      <td>${signals(r)}</td>
      <td style="text-align:center">${tradeSignalBadge(r.trade_signal)}</td>
      <td class="r">${backtestCell(r)}</td>
      <td class="r">${scoreCell(r)}</td>
      <td style="text-align:center">${actionBtns(r)}</td>
    </tr>`;
  }).join('');
  updateSortHeaders();
}

function insertGroupHeaders(list) {
  const rows = dataRows(list);
  const groups = new Map();
  for (const r of rows) {
    const target = targetGroupName(r);
    if (!groups.has(target)) groups.set(target, []);
    groups.get(target).push(r);
  }

  const groupOrder = Array.from(groups.keys()).sort((a, b) => {
    const bestA = Math.max(...groups.get(a).map(r => Number(r.score) || 0));
    const bestB = Math.max(...groups.get(b).map(r => Number(r.score) || 0));
    return bestB - bestA;
  });

  const out = [];
  for (const target of groupOrder) {
    out.push({_is_group_header: true, category: target});
    out.push(...groups.get(target).sort((a, b) => (Number(b.score) || 0) - (Number(a.score) || 0)));
  }
  return out;
}

function updateBacktestStatus(backtest) {
  const el = document.getElementById('backtestStatus');
  const btn = document.getElementById('btnBacktest');
  if (!el) return;
  const st = backtest || {};
  const scheme = st.scheme_display_name || st.trade_timing_label || '收盘前15分钟';
  if (btn) btn.disabled = st.status === 'running';
  if (st.status === 'running') {
    el.textContent = '回测：运行中…';
    el.style.color = '#e3b341';
  } else if (st.status === 'ready') {
    el.textContent = `回测：${scheme}方案已更新 ` + (st.timestamp || '');
    el.style.color = '#3fb950';
  } else if (st.status === 'error') {
    el.textContent = '回测：' + (st.error || '失败');
    el.style.color = '#f85149';
  } else {
    el.textContent = `回测：${scheme}方案，收盘后自动执行，也可手动运行`;
    el.style.color = '';
  }
}

function render(data, preserveActiveCat = false) {
  const previousCat = _activeCat;
  _allResults = data.results || [];
  const rows = dataRows(_allResults);
  const topScore = rows.reduce((best, r) => {
    const score = Number(r.score);
    return Number.isFinite(score) ? Math.max(best, score) : best;
  }, -Infinity);
  _watchlist = new Set(data.watchlist || Array.from(_watchlist));
  document.getElementById('sTotal').textContent    = data.universe_total ? data.universe_total + '只' : '—';
  document.getElementById('sScanned').textContent  = data.scanned ? data.scanned + '只' : '—';
  document.getElementById('sSelected').textContent = rows.length;
  document.getElementById('sTop').textContent      = Number.isFinite(topScore) ? topScore.toFixed(1) : '—';
  const sStatus = document.getElementById('sStatus');
  sStatus.textContent = '实时'; sStatus.style.color = '#3fb950';
  if (data.timestamp) document.getElementById('updateTime').textContent = '更新于 ' + data.timestamp;
  if (data.date)      document.getElementById('dateChip').textContent   = data.date;
  updateBacktestStatus(data.backtest);

  _activeCat = preserveActiveCat && previousCat ? previousCat : '全部';
  buildTabs(_allResults);
  if (_activeCat === '持仓') {
    const table = document.querySelector('.table-wrap');
    const hpPanel = document.getElementById('holdings-panel');
    if (table) table.style.display = 'none';
    if (hpPanel) hpPanel.style.display = 'block';
  } else {
    const table = document.querySelector('.table-wrap');
    const hpPanel = document.getElementById('holdings-panel');
    if (table) table.style.display = '';
    if (hpPanel) hpPanel.style.display = 'none';
    renderRows(currentList());
  }

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
    if (data.backtest) updateBacktestStatus(data.backtest);
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

async function runBacktest() {
  const btn = document.getElementById('btnBacktest');
  if (btn) btn.disabled = true;
  updateBacktestStatus({status: 'running'});
  await fetch('/api/backtest/run', {method: 'POST'}).catch(()=>{});
  pollBacktestStatus();
}

async function pollBacktestStatus() {
  try {
    const st = await (await fetch('/api/backtest/status')).json();
    updateBacktestStatus(st);
    if (st.status === 'running') {
      setTimeout(pollBacktestStatus, 2500);
    } else if (st.status === 'ready') {
      await reloadSelectOnce();
    }
  } catch(e) {
    updateBacktestStatus({status: 'error', error: e.message});
  }
}

function startAutoRefresh() {
  setInterval(async () => {
    const status = await loadMarketStatus();
    if (status.auto_refresh_allowed) doRefresh();
  }, APP_CONFIG.autoRefreshIntervalMs);
}

async function bootstrap() {
  await loadRuntimeConfig();
  startAutoRefresh();
  await loadHoldings();
  await loadWatchlist();
  startPolling();
}

bootstrap();
