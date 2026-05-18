"""
ETF 选股 Web Dashboard
启动: python etf_web.py
访问: 由 config.json 的 server.web_port 配置
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from .config import BASE_DIR, CONFIG, app_base_url, now_str, today_str, web_runtime_config
from .universe import fetch_universe
from .data import fetch_all_history, fetch_history, fetch_realtime
from .strategy import (
    backtest_model,
    compute_indicators,
    compute_trade_signal,
    get_selection_model,
    select_top,
)

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
    "etf_map":        {},   # {code: DataFrame}，供持仓信号/回测计算使用
}
_backtest_state = {
    "status": "idle",       # idle | running | ready | error
    "timestamp": None,
    "date": None,
    "error": None,
    "last_auto_date": None,
}
_lock = threading.Lock()
_backtest_lock = threading.Lock()
_scheduler_started = False

_HOLDINGS_FILE = BASE_DIR / "holdings.json"
_WATCHLIST_FILE = BASE_DIR / "watchlist.json"

_CODE_RE = re.compile(r"^\d{6}$")


def _normalize_code(code: str) -> str:
    return str(code or "").strip().zfill(6)[-6:]


def _valid_code(code: str) -> bool:
    return bool(_CODE_RE.match(code or ""))


def _load_json_list(path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x).strip() for x in data if str(x).strip()] if isinstance(data, list) else []
    except Exception:
        return []


def _save_json_list(path, codes: list):
    unique = []
    for code in codes:
        code = _normalize_code(code)
        if _valid_code(code) and code not in unique:
            unique.append(code)
    path.write_text(json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_holdings() -> list:
    return _load_json_list(_HOLDINGS_FILE)


def _save_holdings(codes: list):
    _save_json_list(_HOLDINGS_FILE, codes)


def _load_watchlist() -> list:
    return _load_json_list(_WATCHLIST_FILE)


def _save_watchlist(codes: list):
    _save_json_list(_WATCHLIST_FILE, codes)


def _universe_meta() -> dict:
    meta: dict = {}
    try:
        from .universe import _CACHE as _UC
        raw = json.loads(_UC.read_text(encoding="utf-8"))
        meta = {i["code"]: i for i in raw.get("data", []) if i.get("code")}
    except Exception:
        pass
    return meta


def _build_rows_for_codes(codes: list[str], etf_map: dict, realtime: dict, meta: dict, rank_start: int) -> list[dict]:
    """按当前模型为指定代码生成榜单行；自定义标的不做硬过滤，确保添加后可见。"""
    codes = [c for c in codes if c in etf_map]
    if not codes:
        return []

    model = get_selection_model()
    enriched = {}
    for code in codes:
        df = etf_map[code].copy()
        rt = realtime.get(code)
        if rt and rt.get("price", 0) > 0:
            df.loc[df.index[-1], "close"] = rt["price"]
            if rt.get("volume", 0) > 0:
                df.loc[df.index[-1], "volume"] = rt["volume"]
        enriched[code] = compute_indicators(df)

    score_map = {}
    try:
        df_score = model.score_all(enriched).set_index("code")
        score_map = {code: row for code, row in df_score.iterrows()}
    except Exception:
        score_map = {}

    rows = []
    for offset, code in enumerate(codes):
        if code not in score_map:
            continue
        row = score_map[code]
        rt = realtime.get(code, {})
        last = enriched[code].iloc[-1]
        m = meta.get(code, {})
        price = rt.get("price") or float(last.get("close", 0))
        trade_signal = compute_trade_signal(enriched[code], realtime_price=float(price or 0))
        rows.append({
            "rank":            rank_start + offset,
            "code":            code,
            "name":            rt.get("name") or m.get("name", code),
            "category":        m.get("category", "自选"),
            "price":           price,
            "change_pct":      round(rt.get("change_pct", 0), 2),
            "ret3":            round(float(row["ret3"]), 2),
            "ret5":            round(float(row["ret5"]), 2),
            "ret10":           round(float(row["ret10"]), 2),
            "rsi":             round(float(row["rsi"]), 1),
            "vol_ratio":       round(float(row["vol_ratio"]), 2),
            "ma_aligned":      bool(row["ma_aligned"]),
            "macd_bullish":    bool(row["macd_hist"] > 0),
            "score":           float(row["score"]),
            "momentum_score":  float(row["momentum_score"]),
            "volume_score":    float(row["volume_score"]),
            "technical_score": float(row["technical_score"]),
            "trend_score":     float(row.get("trend_score", 0)),
            "model":           model.name,
            "trade_signal":    trade_signal,
            "buy_signal":      trade_signal["action"] == "buy",
            "sell_signal":     trade_signal["action"] == "sell",
            "signal_sort":     {"buy": 3, "hold": 2, "sell": 1}.get(trade_signal["action"], 2),
            "backtest":        None,
            "backtest_return_pct": None,
            "is_custom":       True,
        })
    return rows


def _merge_custom_rows(results: list[dict], etf_map: dict, realtime: dict | None = None) -> list[dict]:
    watchlist = _load_watchlist()
    if not watchlist:
        return results

    existing = {r.get("code") for r in results}
    missing = [code for code in watchlist if code not in existing]
    if not missing:
        return results

    meta = _universe_meta()
    for code in missing:
        if code not in etf_map:
            df = fetch_history(code, int(CONFIG["selection"]["history_days"]))
            if not df.empty and len(df) >= int(CONFIG["selection"]["holding_min_history_rows"]):
                etf_map[code] = df
    rt = realtime or fetch_realtime(missing)
    custom_rows = _build_rows_for_codes(missing, etf_map, rt, meta, len(results) + 1)
    return results + custom_rows


_ETF_ISSUER_SUFFIX_RE = re.compile(
    r"(ETF|ＥＴＦ|LOF|QDII|联接|基金|指数|增强|优选).*$",
    re.IGNORECASE,
)


def _target_group_name(name: str, fallback: str = "其他") -> str:
    """从 ETF 名称提取标的名称，用于把同一标的的不同发行方集中展示。"""
    text = str(name or "").strip()
    if not text:
        return fallback
    text = re.sub(r"[\s（）()\-_/]+", "", text)
    target = _ETF_ISSUER_SUFFIX_RE.sub("", text).strip()
    return target or text or fallback


def _group_by_target(results: list[dict]) -> list[dict]:
    """按标的名称分组：如“半导体设备ETF国泰/招商”归为“半导体设备”。"""
    groups: dict[str, list[dict]] = {}
    group_order: list[str] = []
    for r in results:
        target = _target_group_name(r.get("name", ""), r.get("category", "其他"))
        if target not in groups:
            groups[target] = []
            group_order.append(target)
        groups[target].append(r)
    # 按组内最高评分对分组排序
    group_order.sort(key=lambda c: max(r.get("score", 0) for r in groups[c]), reverse=True)
    out: list[dict] = []
    new_rank = 1
    for target in group_order:
        items = sorted(groups[target], key=lambda r: r.get("score", 0), reverse=True)
        out.append({"_is_group_header": True, "category": target})
        for item in items:
            item["rank"] = new_rank
            new_rank += 1
            out.append(item)
    return out


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

        with _lock:
            previous_backtests = {
                r.get("code"): (r.get("backtest"), r.get("backtest_return_pct"))
                for r in (_cache.get("results") or [])
                if r.get("backtest") is not None
            }

        etf_map  = fetch_all_history(universe, days=int(CONFIG["selection"]["history_days"]))
        realtime = fetch_realtime(list(etf_map.keys()))
        results  = select_top(
            universe,
            etf_map,
            realtime,
            top_n=int(CONFIG["selection"]["web_top_n"]),
            include_backtest=False,
        )
        results = _merge_custom_rows(results, etf_map)
        results = _group_by_target(results)
        for item in results:
            prev = previous_backtests.get(item.get("code"))
            if prev:
                item["backtest"], item["backtest_return_pct"] = prev

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
            if not previous_backtests:
                _backtest_state.update(status="idle", error=None)
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


def _append_custom_to_ready_cache(code: str):
    with _lock:
        if _cache.get("status") != "ready" or not isinstance(_cache.get("results"), list):
            return
        if any(r.get("code") == code for r in _cache["results"]):
            return
        etf_map = dict(_cache.get("etf_map", {}))

    df = fetch_history(code, int(CONFIG["selection"]["history_days"]))
    if df.empty:
        return
    etf_map[code] = df
    rt = fetch_realtime([code])
    rows = _build_rows_for_codes(code and [code] or [], etf_map, rt, _universe_meta(), len(_cache.get("results") or []) + 1)
    if not rows:
        return

    with _lock:
        current = list(_cache.get("results") or [])
        if not any(r.get("code") == code for r in current):
            current.extend(rows)
            _cache["results"] = current
            _cache["etf_map"] = etf_map
            _cache["timestamp"] = now_str("timestamp_format")


def _run_backtest_async(force: bool = False):
    with _backtest_lock:
        with _lock:
            if _backtest_state["status"] == "running":
                return
            if _cache.get("status") != "ready" or not _cache.get("results"):
                _backtest_state.update(status="error", error="榜单尚未加载完成，无法回测")
                return
            if not force and _backtest_state.get("date") == today_str() and _backtest_state.get("status") == "ready":
                return
            _backtest_state.update(status="running", error=None)
            results = [dict(r) for r in (_cache.get("results") or [])]
            etf_map = dict(_cache.get("etf_map", {}))

        try:
            for r in results:
                code = r.get("code")
                if not code:
                    continue
                if code not in etf_map:
                    df = fetch_history(code, int(CONFIG["selection"]["history_days"]))
                    if not df.empty:
                        etf_map[code] = df
                if code in etf_map:
                    bt = backtest_model(etf_map[code], window=22)
                    r["backtest"] = bt
                    r["backtest_return_pct"] = bt["return_pct"]

            with _lock:
                _cache["results"] = results
                _cache["etf_map"] = etf_map
                _cache["timestamp"] = now_str("timestamp_format")
                _backtest_state.update(
                    status="ready",
                    timestamp=now_str("timestamp_format"),
                    date=today_str(),
                    error=None,
                )
        except Exception as e:
            with _lock:
                _backtest_state.update(status="error", error=str(e))


def _start_backtest(force: bool = False):
    threading.Thread(target=_run_backtest_async, kwargs={"force": force}, daemon=True).start()


def _is_after_close_now() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    close_minute = int(CONFIG["web"].get("auto_refresh_end_minute", 15 * 60 + 5))
    return now.hour * 60 + now.minute >= close_minute


def _backtest_scheduler_loop():
    while True:
        try:
            today = today_str()
            with _lock:
                already = _backtest_state.get("last_auto_date") == today
                ready = _cache.get("status") == "ready"
            if not already and ready and _is_after_close_now():
                with _lock:
                    _backtest_state["last_auto_date"] = today
                _start_backtest(force=True)
        except Exception:
            pass
        time.sleep(60)


def _ensure_scheduler_started():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_backtest_scheduler_loop, daemon=True).start()


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
            "watchlist":      _load_watchlist(),
            "backtest":       dict(_backtest_state),
        })


@app.route("/api/refresh")
def api_refresh():
    # 刷新只重跑选股/实时数据，不运行回测；回测由收盘后定时任务或手动按钮触发。
    _ensure_fresh(force=True)
    return jsonify({"status": "loading", "backtest": "skipped"})


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    _start_backtest(force=True)
    with _lock:
        return jsonify({"ok": True, "backtest": dict(_backtest_state)})


@app.route("/api/backtest/status")
def api_backtest_status():
    with _lock:
        return jsonify(dict(_backtest_state))


@app.route("/api/watchlist")
def api_get_watchlist():
    return jsonify({"watchlist": _load_watchlist()})


@app.route("/api/watchlist", methods=["POST"])
def api_add_watchlist():
    code = _normalize_code((request.json or {}).get("code", ""))
    if not _valid_code(code):
        return jsonify({"ok": False, "error": "请输入 6 位数字代码"}), 400
    watchlist = _load_watchlist()
    if code not in watchlist:
        watchlist.append(code)
        _save_watchlist(watchlist)
        _append_custom_to_ready_cache(code)
    return jsonify({"ok": True, "code": code, "watchlist": _load_watchlist()})


@app.route("/api/watchlist/<code>", methods=["DELETE"])
def api_remove_watchlist(code):
    code = _normalize_code(code)
    if code in _load_holdings():
        return jsonify({"ok": False, "error": "该标的仍在持仓中，不能从榜单移除"}), 400
    watchlist = _load_watchlist()
    if code in watchlist:
        watchlist.remove(code)
        _save_watchlist(watchlist)
    with _lock:
        if isinstance(_cache.get("results"), list):
            _cache["results"] = [r for r in _cache["results"] if not (r.get("code") == code and r.get("is_custom"))]
    return jsonify({"ok": True, "watchlist": _load_watchlist()})


# ── 持仓管理 ────────────────────────────────────────────────────────────

@app.route("/api/holdings")
def api_get_holdings():
    return jsonify({"holdings": _load_holdings()})


@app.route("/api/holdings/toggle", methods=["POST"])
def api_toggle_holding():
    code = _normalize_code((request.json or {}).get("code", ""))
    if not _valid_code(code):
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
    meta = _universe_meta()

    # 从选股缓存取评分排名和历史数据
    rank_map: dict = {}
    etf_map:  dict = {}
    with _lock:
        for r in (_cache.get("results") or []):
            if not isinstance(r, dict) or r.get("_is_group_header"):
                continue
            code = r.get("code")
            if code:
                rank_map[code] = r.get("rank")
        etf_map = dict(_cache.get("etf_map", {}))

    # 对缓存中缺失的持仓代码实时补取历史 K 线（并发）
    missing = [c for c in holdings if c not in etf_map]
    if missing:
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
    _ensure_scheduler_started()
    _ensure_fresh()   # 启动时立即开始后台拉取（不运行回测）
    app.run(host=CONFIG["server"]["host"], port=int(CONFIG["server"]["web_port"]), debug=bool(CONFIG["server"]["debug"]))


if __name__ == "__main__":
    main()
