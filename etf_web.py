"""
ETF 选股 Web Dashboard
启动: python etf_web.py
访问: http://localhost:8080
"""

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from etf_universe import fetch_universe
from etf_data import fetch_all_history, fetch_realtime
from etf_strategy import select_top

app = Flask(__name__)
CORS(app)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ── 全局缓存 ──────────────────────────────────────────────────────────

_cache = {
    "status":         "idle",   # idle | loading | ready | error
    "results":        None,
    "timestamp":      None,
    "date":           None,
    "error":          None,
    "universe_total": 0,
    "scanned":        0,
    "etf_map":        {},   # {code: DataFrame}，供持仓信号计算使用
}
_lock = threading.Lock()


def _run_selection():
    try:
        universe = fetch_universe(min_amount=5e7, max_count=300)

        # 读全量总数
        from etf_universe import _CACHE
        import json as _json
        universe_total = len(universe)
        try:
            cached_raw = _json.loads(_CACHE.read_text(encoding="utf-8"))
            universe_total = len(cached_raw.get("data", universe))
        except Exception:
            pass

        etf_map  = fetch_all_history(universe, days=65)
        realtime = fetch_realtime(list(etf_map.keys()))
        results  = select_top(universe, etf_map, realtime, top_n=50)

        with _lock:
            _cache.update(
                status         = "ready",
                results        = results,
                timestamp      = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                date           = date.today().isoformat(),
                error          = None,
                universe_total = universe_total,
                scanned        = len(universe),
                etf_map        = etf_map,
            )
    except Exception as e:
        with _lock:
            _cache.update(status="error", error=str(e))


def _ensure_fresh(force: bool = False):
    with _lock:
        today = date.today().isoformat()
        if not force and _cache["status"] == "loading":
            return
        if not force and _cache["status"] == "ready" and _cache["date"] == today:
            return
        _cache["status"] = "loading"
    threading.Thread(target=_run_selection, daemon=True).start()


# ── API ────────────────────────────────────────────────────────────────

@app.route("/api/select")
def api_select():
    _ensure_fresh()
    with _lock:
        return jsonify({
            "status":         _cache["status"],
            "results":        _cache["results"],
            "timestamp":      _cache["timestamp"],
            "date":           _cache["date"],
            "error":          _cache["error"],
            "universe_total": _cache["universe_total"],
            "scanned":        _cache["scanned"],
        })


@app.route("/api/refresh")
def api_refresh():
    _ensure_fresh(force=True)
    return jsonify({"status": "loading"})


# ── 持仓管理 ────────────────────────────────────────────────────────────

_HOLDINGS_FILE = Path(__file__).parent / "holdings.json"


def _load_holdings() -> list:
    try:
        return json.loads(_HOLDINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_holdings(codes: list):
    _HOLDINGS_FILE.write_text(json.dumps(codes, ensure_ascii=False), encoding="utf-8")


@app.route("/api/holdings")
def api_get_holdings():
    return jsonify({"holdings": _load_holdings()})


@app.route("/api/holdings/toggle", methods=["POST"])
def api_toggle_holding():
    code = (request.json or {}).get("code", "").strip()
    if not code:
        return jsonify({"ok": False}), 400
    holdings = _load_holdings()
    if code in holdings:
        holdings.remove(code)
        added = False
    else:
        holdings.append(code)
        added = True
    _save_holdings(holdings)
    return jsonify({"ok": True, "added": added, "holdings": holdings})


@app.route("/api/holdings/realtime")
def api_holdings_realtime():
    """
    持仓实时行情 + 卖出信号（每次调用均实时拉取行情，信号基于缓存历史数据计算）
    """
    from etf_strategy import compute_sell_signals

    holdings = _load_holdings()
    if not holdings:
        return jsonify({"data": [], "timestamp": None})

    rt = fetch_realtime(holdings)

    # 从 universe 缓存补充元数据
    meta: dict = {}
    try:
        from etf_universe import _CACHE as _UC
        raw = json.loads(_UC.read_text(encoding="utf-8"))
        meta = {i["code"]: i for i in raw.get("data", [])}
    except Exception:
        pass

    # 从选股缓存取评分排名和历史数据
    rank_map: dict = {}
    etf_map:  dict = {}
    with _lock:
        for r in (_cache.get("results") or []):
            rank_map[r["code"]] = r["rank"]
        etf_map = dict(_cache.get("etf_map", {}))

    # 对缓存中缺失的持仓代码实时补取历史 K 线（并发）
    missing = [c for c in holdings if c not in etf_map]
    if missing:
        from etf_data import fetch_history
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        with ThreadPoolExecutor(max_workers=min(len(missing), 5)) as ex:
            fmap = {ex.submit(fetch_history, c, 65): c for c in missing}
            for future in _as_completed(fmap):
                code = fmap[future]
                try:
                    df = future.result()
                    if not df.empty and len(df) >= 10:
                        etf_map[code] = df
                except Exception:
                    pass

    data = []
    for code in holdings:
        q = rt.get(code, {})
        m = meta.get(code, {})
        price = q.get("price", 0)

        # 卖出信号（使用全市场扫描时缓存的历史 K 线）
        if code in etf_map:
            sell = compute_sell_signals(etf_map[code], realtime_price=price)
        else:
            sell = {"signals": [], "urgency": "暂无数据", "urgency_level": -1}

        data.append({
            "code":         code,
            "name":         q.get("name") or m.get("name", code),
            "category":     m.get("category", ""),
            "price":        price,
            "change_pct":   q.get("change_pct", 0),
            "amount":       q.get("amount", 0),
            "rank":         rank_map.get(code),
            "sell_signals": sell,
        })

    return jsonify({
        "data":      data,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ── 前端页面 ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html; charset=utf-8")


# ── HTML ────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETF 选股日报</title>
<style>
:root {
  --bg:       #0d1117;
  --surface:  #161b22;
  --surface2: #21262d;
  --border:   #30363d;
  --text:     #e6edf3;
  --muted:    #8b949e;
  --green:    #3fb950;
  --red:      #f85149;
  --blue:     #58a6ff;
  --yellow:   #e3b341;
  --orange:   #f0883e;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  font-size: 13px;
  min-height: 100vh;
}

/* ── Header ─────────────────────────────────────────────────────── */
header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
  height: 52px;
  display: flex;
  align-items: center;
  gap: 14px;
  position: sticky;
  top: 0;
  z-index: 100;
}
.logo { font-size: 17px; font-weight: 700; color: var(--blue); letter-spacing: -0.3px; }
.logo em { color: var(--green); font-style: normal; }
.date-chip {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.spacer { flex: 1; }
.update-time { font-size: 12px; color: var(--muted); }
.btn {
  background: var(--surface2);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 5px 14px;
  cursor: pointer;
  font-size: 13px;
  transition: border-color .15s, color .15s;
  white-space: nowrap;
}
.btn:hover  { border-color: var(--blue); color: var(--blue); }
.btn:disabled { opacity: .45; cursor: not-allowed; }

/* ── Stats bar ───────────────────────────────────────────────────── */
.stats {
  display: flex;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.stat {
  padding: 10px 24px;
  border-right: 1px solid var(--border);
  min-width: 120px;
}
.stat-label { font-size: 11px; color: var(--muted); letter-spacing: .4px; text-transform: uppercase; margin-bottom: 3px; }
.stat-value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }

/* ── Main ────────────────────────────────────────────────────────── */
main { padding: 20px 24px; }

/* ── Loading ─────────────────────────────────────────────────────── */
#loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
  padding: 100px 24px;
  color: var(--muted);
}
.spinner {
  width: 34px; height: 34px;
  border: 3px solid var(--border);
  border-top-color: var(--blue);
  border-radius: 50%;
  animation: spin .75s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Error ───────────────────────────────────────────────────────── */
#error-box {
  display: none;
  text-align: center;
  padding: 60px 24px;
  color: var(--red);
  font-size: 14px;
}

/* ── Category tabs ───────────────────────────────────────────────── */
.tab-bar {
  display: none;
  padding: 0 24px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
  scrollbar-width: none;
}
.tab-bar::-webkit-scrollbar { display: none; }
.tab-inner { display: flex; gap: 0; white-space: nowrap; }
.tab {
  padding: 10px 14px;
  font-size: 13px;
  color: var(--muted);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color .15s;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 5px;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--blue); border-bottom-color: var(--blue); font-weight: 600; }
.tab .badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--surface2);
  color: var(--muted);
}
.tab.active .badge { background: rgba(88,166,255,.15); color: var(--blue); }

/* ── Table ───────────────────────────────────────────────────────── */
#table-section { display: none; }
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
}
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
thead th {
  background: var(--surface2);
  padding: 9px 12px;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .4px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  text-align: left;
}
th.r, td.r { text-align: right; }
tbody tr { border-bottom: 1px solid var(--border); transition: background .12s; }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: rgba(88,166,255,.05); }
td { padding: 10px 12px; white-space: nowrap; vertical-align: middle; }

/* rank */
.rank { font-size: 12px; font-weight: 700; color: var(--muted); }
.r1 { color: #fbbf24; } .r2 { color: #9ca3af; } .r3 { color: #cd7f32; }

/* code */
.code { font-family: 'SF Mono','Fira Code','Consolas',monospace; font-size: 12px; font-weight: 600; color: var(--blue); }

/* category badge */
.cat { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px; font-weight: 500; }

/* returns */
.pos { color: var(--green); font-weight: 500; }
.neg { color: var(--red); font-weight: 500; }
.neu { color: var(--muted); }

/* RSI coloring */
.rsi-hot  { color: var(--red); font-weight: 600; }
.rsi-warm { color: var(--orange); }
.rsi-good { color: var(--green); }
.rsi-cold { color: var(--blue); }

/* signals */
.sigs { display: flex; gap: 4px; flex-wrap: wrap; min-width: 100px; }
.sig { font-size: 11px; padding: 2px 6px; border-radius: 3px; white-space: nowrap; }
.s-ma   { background: rgba(63,185,80,.12);  color: var(--green);  border: 1px solid rgba(63,185,80,.25); }
.s-macd { background: rgba(227,179,65,.10); color: var(--yellow); border: 1px solid rgba(227,179,65,.25); }
.s-vol  { background: rgba(240,136,62,.10); color: var(--orange); border: 1px solid rgba(240,136,62,.25); }
.s-rsi  { background: rgba(88,166,255,.10); color: var(--blue);   border: 1px solid rgba(88,166,255,.25); }

/* score */
.score-cell { display: flex; align-items: center; gap: 8px; justify-content: flex-end; }
.score-val  { font-weight: 700; font-size: 15px; min-width: 38px; text-align: right; }
.bar-bg  { width: 56px; height: 5px; background: var(--border); border-radius: 3px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, var(--blue), var(--green)); }

/* tooltip for score breakdown */
.score-tip {
  position: relative;
  cursor: default;
}
.tip-content {
  display: none;
  position: absolute;
  right: 0; top: calc(100% + 6px);
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 14px;
  min-width: 180px;
  z-index: 50;
  box-shadow: 0 4px 16px rgba(0,0,0,.5);
  font-size: 12px;
  line-height: 2;
}
.score-tip:hover .tip-content { display: block; }
.tip-row { display: flex; justify-content: space-between; gap: 16px; }
.tip-label { color: var(--muted); }
.tip-val   { font-weight: 600; }

/* legend */
.legend {
  margin-top: 14px;
  padding: 12px 16px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--muted);
  font-size: 12px;
  display: flex;
  gap: 28px;
  flex-wrap: wrap;
  line-height: 1.8;
}
.legend strong { color: var(--text); }

/* ── 持仓按钮 & 行高亮 ────────────────────────────────────────────── */
.btn-hold {
  font-size: 11px;
  padding: 3px 9px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  white-space: nowrap;
  transition: all .15s;
  line-height: 1.6;
}
.btn-hold:hover   { border-color: var(--green); color: var(--green); }
.btn-hold.active  {
  background: rgba(63,185,80,.12);
  border-color: rgba(63,185,80,.4);
  color: var(--green);
  font-weight: 600;
}
tr.holding        { background: rgba(63,185,80,.04) !important; }

/* ── 持仓面板 ────────────────────────────────────────────────────── */
#holdings-panel { display: none; }
.hp-header {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 12px;
}
.hp-title  { font-size: 14px; font-weight: 600; }
.hp-ts     { font-size: 12px; color: var(--muted); }
.hp-hint   { font-size: 12px; color: var(--muted); margin-left: auto; }
.hp-empty  {
  padding: 60px;
  text-align: center;
  color: var(--muted);
  font-size: 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.rank-badge {
  display: inline-block;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(88,166,255,.12);
  color: var(--blue);
  border: 1px solid rgba(88,166,255,.25);
}

/* ── 卖出信号 ────────────────────────────────────────────────────── */
.sell-wrap { position: relative; cursor: default; display: inline-block; }
.sell-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 4px;
  white-space: nowrap;
}
.sell-0 { background: rgba(63,185,80,.12);  color: var(--green);  border: 1px solid rgba(63,185,80,.3);  }
.sell-1 { background: rgba(227,179,65,.12); color: var(--yellow); border: 1px solid rgba(227,179,65,.3); }
.sell-2 { background: rgba(240,136,62,.14); color: var(--orange); border: 1px solid rgba(240,136,62,.3); }
.sell-3 { background: rgba(248,81,73,.12);  color: var(--red);    border: 1px solid rgba(248,81,73,.3);  }
.sell-na{ background: var(--surface2);      color: var(--muted);  border: 1px solid var(--border); }
.sell-wrap .tip-content { min-width: 200px; }
.sell-wrap:hover .tip-content { display: none; }
.floating-tip {
  display: block;
  position: fixed;
  right: auto;
  bottom: auto;
  z-index: 1000;
  pointer-events: none;
}
.sig-item {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 2px 0;
  font-size: 12px;
}
.sig-lv-强 { color: var(--red); }
.sig-lv-中 { color: var(--orange); }
.sig-lv-弱 { color: var(--yellow); }

/* ── 持仓面板倒计时 ───────────────────────────────────────────────── */
.hp-countdown { font-size: 12px; color: var(--muted); margin-left: 4px; }
</style>
</head>
<body>

<header>
  <div class="logo">ETF <em>选股</em>日报</div>
  <div class="date-chip" id="dateChip">—</div>
  <div class="spacer"></div>
  <div class="update-time" id="updateTime"></div>
  <button class="btn" id="btnRefresh" onclick="doRefresh()">↺ 刷新</button>
</header>

<div class="stats">
  <div class="stat"><div class="stat-label">全市场ETF</div><div class="stat-value" id="sTotal">—</div></div>
  <div class="stat"><div class="stat-label">流动性筛选</div><div class="stat-value" id="sScanned">—</div></div>
  <div class="stat"><div class="stat-label">本日优选</div><div class="stat-value" id="sSelected">—</div></div>
  <div class="stat"><div class="stat-label">最高评分</div><div class="stat-value" id="sTop">—</div></div>
  <div class="stat"><div class="stat-label">数据状态</div><div class="stat-value" id="sStatus" style="font-size:14px">初始化…</div></div>
</div>

<div class="tab-bar" id="tabBar"><div class="tab-inner" id="tabInner"></div></div>

<main>
  <div id="loading">
    <div class="spinner"></div>
    <div id="loadMsg">正在获取全市场 ETF 列表…</div>
  </div>

  <div id="error-box"></div>

  <div id="table-section">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="r">#</th>
            <th>代码</th>
            <th>名称</th>
            <th>类别</th>
            <th class="r">现价</th>
            <th class="r">今日</th>
            <th class="r">3日</th>
            <th class="r">5日</th>
            <th class="r">10日</th>
            <th class="r">RSI</th>
            <th class="r">量比</th>
            <th>信号</th>
            <th class="r">评分↓</th>
            <th style="text-align:center">持仓</th>
          </tr>
        </thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>

    <div id="holdings-panel">
      <div class="hp-header">
        <span class="hp-title">持仓实时监控</span>
        <span class="hp-ts" id="hpTs"></span>
        <span class="hp-hint">每 2 分钟自动刷新实时行情 · 信号基于最近一次全市场扫描</span>
        <span class="hp-countdown" id="hpCountdown"></span>
        <button class="btn" onclick="refreshHoldings()">↺ 立即刷新</button>
      </div>
      <div id="hpBody"></div>
    </div>

    <div class="legend">
      <span><strong>因子权重</strong>：动量 35% · 量能 25% · 技术 25% · 趋势 15%</span>
      <span><strong>硬过滤</strong>：RSI > 82 | 5日跌幅 > 9% | 破 MA20 且持续下跌</span>
      <span><strong>信号</strong>：↑ 均线多头 · ⚡ MACD 看多 · 🔥 量比≥1.5x · 超卖回弹</span>
      <span style="color:#f85149"><strong>风险提示</strong>：仅供参考，不构成投资建议</span>
    </div>
  </div>
</main>

<script>
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

function pct(v) {
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}
function pctCls(v) {
  return v > 0.05 ? 'pos' : v < -0.05 ? 'neg' : 'neu';
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
  const wrap = e.target.closest('.sell-wrap');
  if (!wrap || !wrap.contains(e.target)) return;
  showSellTip(wrap);
});

document.addEventListener('mouseout', e => {
  const wrap = e.target.closest('.sell-wrap');
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
    _holdingsSecs = 120;
    refreshHoldings();
  }, 120000);
  _holdingsCountdown = setInterval(_tickCountdown, 1000);
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
      <div class="tip-content">
        <div class="tip-row"><span class="tip-label">动量 (35%)</span><span class="tip-val">${r.momentum_score.toFixed(1)}</span></div>
        <div class="tip-row"><span class="tip-label">量能 (25%)</span><span class="tip-val">${r.volume_score.toFixed(1)}</span></div>
        <div class="tip-row"><span class="tip-label">技术 (25%)</span><span class="tip-val">${r.technical_score.toFixed(1)}</span></div>
        <div class="tip-row"><span class="tip-label">综合得分</span><span class="tip-val">${r.score.toFixed(1)}</span></div>
      </div>
    </div>`;
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
    const filtered = cat === '全部' ? _allResults : _allResults.filter(r => r.category === cat);
    renderRows(filtered);
  }
}

function renderRows(list) {
  document.getElementById('tbody').innerHTML = list.map(r => `
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
      <td class="r">${scoreCell(r)}</td>
      <td style="text-align:center">${holdBtn(r.code)}</td>
    </tr>`).join('');
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
  _timer = setInterval(poll, 2500);
}

async function doRefresh() {
  document.getElementById('btnRefresh').disabled = true;
  await fetch('/api/refresh').catch(()=>{});
  startPolling();
}

// 交易时段内每 10 分钟自动刷新
setInterval(() => {
  const m = new Date(); const t = m.getHours()*60 + m.getMinutes();
  if (t >= 9*60+25 && t <= 15*60+5) doRefresh();
}, 10*60*1000);

loadHoldings().then(() => startPolling());
</script>
</body>
</html>"""

# ── 入口 ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 52)
    print("  ETF 选股 Web Dashboard")
    print("=" * 52)
    print("  地址: http://localhost:8080")
    print("  全市场扫描模式，首次加载约需 60-90 秒")
    print("=" * 52)
    _ensure_fresh()   # 启动时立即开始后台拉取
    app.run(host="0.0.0.0", port=8080, debug=False)
