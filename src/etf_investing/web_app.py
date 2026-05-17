"""
ETF 选股 Web Dashboard
启动: python etf_web.py
访问: 由 config.json 的 server.web_port 配置
"""

import json
import logging
import threading
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from .config import BASE_DIR, CONFIG, app_base_url, now_str, today_str, web_runtime_config
from .universe import fetch_universe
from .data import fetch_all_history, fetch_realtime
from .strategy import select_top

WEB_DIR = BASE_DIR / "web"

app = Flask(__name__, static_folder=str(WEB_DIR / "static"), static_url_path="/static")
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
        universe = fetch_universe(
            min_amount=float(CONFIG["selection"]["default_min_amount"]),
            max_count=int(CONFIG["selection"]["default_max_count"]),
        )

        # 读全量总数
        from .universe import _CACHE
        import json as _json
        universe_total = len(universe)
        try:
            cached_raw = _json.loads(_CACHE.read_text(encoding="utf-8"))
            universe_total = len(cached_raw.get("data", universe))
        except Exception:
            pass

        etf_map  = fetch_all_history(universe, days=int(CONFIG["selection"]["history_days"]))
        realtime = fetch_realtime(list(etf_map.keys()))
        results  = select_top(universe, etf_map, realtime, top_n=int(CONFIG["selection"]["web_top_n"]))

        with _lock:
            _cache.update(
                status         = "ready",
                results        = results,
                timestamp      = now_str("timestamp_format"),
                date           = today_str(),
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
        today = today_str()
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

_HOLDINGS_FILE = BASE_DIR / "holdings.json"


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
    from .strategy import compute_sell_signals

    holdings = _load_holdings()
    if not holdings:
        return jsonify({"data": [], "timestamp": None})

    rt = fetch_realtime(holdings)

    # 从 universe 缓存补充元数据
    meta: dict = {}
    try:
        from .universe import _CACHE as _UC
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
        from .data import fetch_history
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        max_workers = min(len(missing), int(CONFIG["selection"]["holding_history_workers"]))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fmap = {
                ex.submit(fetch_history, c, int(CONFIG["selection"]["history_days"])): c
                for c in missing
            }
            for future in _as_completed(fmap):
                code = fmap[future]
                try:
                    df = future.result()
                    if not df.empty and len(df) >= int(CONFIG["selection"]["holding_min_history_rows"]):
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
        "timestamp": now_str("timestamp_format"),
    })


# ── 前端页面 ────────────────────────────────────────────────────────────

@app.route("/api/config")
def api_config():
    return jsonify(web_runtime_config())

@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


# ── 入口 ───────────────────────────────────────────────────────────────

def main():
    print("=" * 52)
    print("  ETF 选股 Web Dashboard")
    print("=" * 52)
    hint = CONFIG["web"]["initial_load_hint_seconds"]
    print(f"  地址: {app_base_url('web_port')}")
    print(f"  全市场扫描模式，首次加载约需 {hint[0]}-{hint[1]} 秒")
    print("=" * 52)
    _ensure_fresh()   # 启动时立即开始后台拉取
    app.run(host=CONFIG["server"]["host"], port=int(CONFIG["server"]["web_port"]), debug=bool(CONFIG["server"]["debug"]))


if __name__ == "__main__":
    main()
