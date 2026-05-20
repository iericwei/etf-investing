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
from datetime import date, datetime
from typing import Any
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from .config import BASE_DIR, CONFIG, app_base_url, now_str, today_str, web_runtime_config
from .universe import fetch_universe
from .data import fetch_all_history, fetch_etf_15m_history, fetch_fund_nav_estimates, fetch_fund_quote_metrics, fetch_history, fetch_realtime
from .strategy import (
    backtest_model,
    compute_indicators,
    compute_sell_signals,
    compute_trade_signal,
    get_backtest_scheme_config,
    get_selection_model,
    select_top,
)
from .notifications import (
    SIGNAL_STATE_FILE,
    load_json_state,
    maybe_send_watch_reminder,
    save_json_state,
    send_feishu_text,
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
_trade_dates_cache: dict[str, object] = {"loaded_at": 0.0, "dates": set()}

_HOLDINGS_FILE = BASE_DIR / "holdings.json"
_WATCHLIST_FILE = BASE_DIR / "watchlist.json"

_CODE_RE = re.compile(r"^\d{6}$")


def _active_backtest_meta() -> dict:
    try:
        name, cfg = get_backtest_scheme_config()
        return {
            "scheme": name,
            "scheme_display_name": cfg.get("display_name", name),
            "trade_time": cfg.get("trade_time", "14:45"),
            "trade_timing_label": cfg.get("trade_timing_label", "收盘前15分钟"),
        }
    except Exception:
        return {
            "scheme": "before_close_15m",
            "scheme_display_name": "收盘前15分钟",
            "trade_time": "14:45",
            "trade_timing_label": "收盘前15分钟",
        }


def _load_china_trade_dates() -> set[date] | None:
    """从 akshare 读取 A 股交易日历；失败时返回 None，让调用方降级到工作日判断。"""
    now_ts = time.time()
    cached = _trade_dates_cache.get("dates")
    loaded_at = _trade_dates_cache.get("loaded_at", 0.0)
    if cached and now_ts - float(loaded_at if isinstance(loaded_at, (int, float)) else 0.0) < 24 * 60 * 60:
        return set(cached)  # type: ignore[arg-type]

    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        if df.empty:
            return None
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        dates = {d.date() if hasattr(d, "date") else datetime.strptime(str(d)[:10], "%Y-%m-%d").date() for d in df[col]}
        _trade_dates_cache.update(loaded_at=now_ts, dates=dates)
        return dates
    except Exception:
        return None


def _is_china_trading_day(day: date | None = None) -> bool:
    day = day or date.today()
    if day.weekday() >= 5:
        return False
    trade_dates = _load_china_trade_dates()
    if trade_dates is None:
        # 网络/akshare 不可用时退化为工作日，避免接口异常导致前端永久暂停。
        return True
    return day in trade_dates


def _market_status(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    start_minute = int(CONFIG["web"].get("auto_refresh_start_minute", 9 * 60 + 25))
    end_minute = int(CONFIG["web"].get("auto_refresh_end_minute", 15 * 60 + 5))
    minute = now.hour * 60 + now.minute
    trading_day = _is_china_trading_day(now.date())
    after_close = trading_day and minute > end_minute
    before_open = trading_day and minute < start_minute
    in_window = trading_day and start_minute <= minute <= end_minute
    if not trading_day:
        reason = "节假日/非交易日"
    elif after_close:
        reason = "已收盘"
    elif before_open:
        reason = "未开盘"
    else:
        reason = "交易时段"
    return {
        "date": now.strftime(CONFIG["time"]["date_format"]),
        "time": now.strftime("%H:%M:%S"),
        "is_trading_day": trading_day,
        "in_auto_refresh_window": in_window,
        "auto_refresh_allowed": in_window,
        "after_close": after_close,
        "before_open": before_open,
        "reason": reason,
        "start_minute": start_minute,
        "end_minute": end_minute,
    }


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


def _load_signal_state() -> dict:
    return load_json_state(SIGNAL_STATE_FILE)


def _save_signal_state(state: dict) -> None:
    save_json_state(SIGNAL_STATE_FILE, state)


def _trade_signal_label(signal: dict | None) -> str:
    if not isinstance(signal, dict):
        return "观望"
    return str(signal.get("label") or {"buy": "买入/持有", "sell": "卖出", "hold": "观望"}.get(signal.get("action"), "观望"))


def _sell_signal_label(sell: dict | None) -> str:
    if not isinstance(sell, dict):
        return "暂无数据"
    return str(sell.get("urgency") or "暂无数据")


def _normalize_signal_label(label: Any) -> str | None:
    if label is None:
        return None
    text = str(label).strip()
    if not text:
        return text
    # Older saved state included detailed reasons like “高风险（跌破均线）”.
    # For reminder decisions only compare the visible signal text, not reason details.
    return text.split("（", 1)[0].strip()


def _annotate_holding_signal_changes(rows: list[dict], previous_state: dict | None = None) -> tuple[list[dict], dict, list[dict]]:
    previous_state = previous_state or {}
    next_state: dict = {}
    changed_rows: list[dict] = []
    for row in rows:
        code = str(row.get("code") or "")
        trade_label = _trade_signal_label(row.get("trade_signal"))
        sell_label = _sell_signal_label(row.get("sell_signals"))
        old = previous_state.get(code, {}) if isinstance(previous_state.get(code), dict) else {}
        changes: list[dict] = []
        old_trade = _normalize_signal_label(old.get("trade_signal_label"))
        old_sell = _normalize_signal_label(old.get("sell_signal_label"))
        if old_trade is not None and old_trade != trade_label:
            changes.append({"field": "模型信号", "from": old_trade, "to": trade_label})
        if old_sell is not None and old_sell != sell_label:
            changes.append({"field": "卖出信号", "from": old_sell, "to": sell_label})
        row["signal_changes"] = changes
        next_state[code] = {"trade_signal_label": trade_label, "sell_signal_label": sell_label}
        if changes:
            changed_rows.append(row)
    return rows, next_state, changed_rows


def _format_holding_change_message(rows: list[dict]) -> str:
    lines = ["持仓信号变动"]
    for row in rows:
        code = row.get("code")
        name = row.get("name") or code
        lines.append(f"{code} {name}")
        for change in row.get("signal_changes", []):
            lines.append(f"- {change['field']}有变更")
    return "\n".join(lines)


def _notify_holding_signal_changes(rows: list[dict]) -> None:
    if rows:
        send_feishu_text(_format_holding_change_message(rows))


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

    nav_map = fetch_fund_nav_estimates(codes)
    quote_metrics = fetch_fund_quote_metrics(codes)

    rows = []
    for offset, code in enumerate(codes):
        if code not in score_map:
            continue
        row = score_map[code]
        rt = realtime.get(code, {})
        last = enriched[code].iloc[-1]
        m = meta.get(code, {})
        price = rt.get("price") or float(last.get("close", 0))
        nav_info = nav_map.get(code, {})
        quote_info = quote_metrics.get(code, {})
        estimate_nav = float(nav_info.get("estimate_nav") or 0)
        premium_rate_pct = round((float(price) / estimate_nav - 1) * 100, 2) if estimate_nav > 0 and price else None
        fund_size = float(m.get("fund_size") or quote_info.get("fund_size") or 0)
        trade_signal = compute_trade_signal(enriched[code], realtime_price=float(price or 0))
        rows.append({
            "rank":            rank_start + offset,
            "code":            code,
            "name":            rt.get("name") or m.get("name", code),
            "category":        m.get("category", "自选"),
            "price":           price,
            "change_pct":      round(rt.get("change_pct", 0), 2),
            "fund_size":       fund_size,
            "premium_rate_pct": premium_rate_pct,
            "estimate_nav":    estimate_nav or None,
            "nav_date":        nav_info.get("nav_date"),
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


def _refresh_cached_rows_for_codes(codes: list[str]) -> dict[str, dict]:
    """刷新指定代码的榜单行，并写回缓存；用于持仓/自选轻量刷新。"""
    wanted = []
    seen = set()
    for code in codes:
        code = _normalize_code(code)
        if _valid_code(code) and code not in seen:
            wanted.append(code)
            seen.add(code)
    if not wanted:
        return {}

    with _lock:
        current_results = [dict(r) for r in (_cache.get("results") or [])]
        etf_map = dict(_cache.get("etf_map", {}))

    for code in wanted:
        if code not in etf_map:
            df = fetch_history(code, int(CONFIG["selection"]["history_days"]))
            if not df.empty and len(df) >= int(CONFIG["selection"]["holding_min_history_rows"]):
                etf_map[code] = df

    available = [code for code in wanted if code in etf_map]
    if not available:
        return {}

    realtime = fetch_realtime(available)
    meta = _universe_meta()
    new_rows = _build_rows_for_codes(available, etf_map, realtime, meta, 1)
    new_by_code = {r.get("code"): r for r in new_rows if r.get("code")}
    if not new_by_code:
        return {}

    data_rows = [dict(r) for r in current_results if isinstance(r, dict) and not r.get("_is_group_header")]
    old_by_code = {r.get("code"): r for r in data_rows if r.get("code")}
    merged_by_code = dict(old_by_code)

    for code, fresh in new_by_code.items():
        old = old_by_code.get(code, {})
        merged = dict(old)
        preserved_rank = old.get("rank")
        preserved_backtest = (old.get("backtest"), old.get("backtest_return_pct"))
        merged.update(fresh)
        if preserved_rank is not None:
            merged["rank"] = preserved_rank
        if old.get("is_custom"):
            merged["is_custom"] = True
        elif old:
            merged.pop("is_custom", None)
        if preserved_backtest[0] is not None:
            merged["backtest"], merged["backtest_return_pct"] = preserved_backtest
        merged_by_code[code] = merged

    ordered: list[dict] = []
    emitted = set()
    for row in data_rows:
        code = row.get("code")
        if code in merged_by_code and code not in emitted:
            ordered.append(merged_by_code[code])
            emitted.add(code)
    for code in wanted:
        if code in merged_by_code and code not in emitted:
            ordered.append(merged_by_code[code])
            emitted.add(code)

    regrouped = _group_by_target(ordered) if ordered else []
    refreshed = {str(code): row for code, row in merged_by_code.items() if code and code in new_by_code}
    with _lock:
        _cache["results"] = regrouped
        _cache["etf_map"] = etf_map
        _cache["timestamp"] = now_str("timestamp_format")
    return refreshed


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
            _, backtest_cfg = get_backtest_scheme_config()
            window_days = int(backtest_cfg.get("window_days", 22))
            for r in results:
                code = r.get("code")
                if not code:
                    continue
                if code not in etf_map:
                    df = fetch_history(code, int(CONFIG["selection"]["history_days"]))
                    if not df.empty:
                        etf_map[code] = df
                if code in etf_map:
                    intraday = fetch_etf_15m_history(code, days=max(window_days * 2, window_days + 10))
                    bt = backtest_model(
                        etf_map[code],
                        window=window_days,
                        intraday=None if intraday.empty else intraday,
                        code=code,
                    )
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
                    **_active_backtest_meta(),
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
            now = datetime.now()
            maybe_send_watch_reminder(now=now, is_trading_day=_is_china_trading_day(now.date()))
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
            "backtest":       dict(_backtest_state, **_active_backtest_meta()),
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
        return jsonify({"ok": True, "backtest": dict(_backtest_state, **_active_backtest_meta())})


@app.route("/api/backtest/status")
def api_backtest_status():
    with _lock:
        return jsonify(dict(_backtest_state, **_active_backtest_meta()))


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

    holdings = _load_holdings()
    if not holdings:
        return jsonify({"data": [], "timestamp": None})

    watchlist = _load_watchlist()
    refreshed_rows = _refresh_cached_rows_for_codes(holdings + watchlist)
    rt = fetch_realtime(holdings)
    meta = _universe_meta()

    # 从选股缓存取评分排名、模型信号和历史数据
    rank_map: dict = {}
    row_map: dict = dict(refreshed_rows)
    etf_map:  dict = {}
    with _lock:
        for r in (_cache.get("results") or []):
            if not isinstance(r, dict) or r.get("_is_group_header"):
                continue
            code = r.get("code")
            if code:
                rank_map[code] = r.get("rank")
                row_map.setdefault(code, r)
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
        cached_row = row_map.get(code, {})
        m = meta.get(code, {})
        price = q.get("price", 0)

        # 卖出信号（使用全市场扫描时缓存的历史 K 线）
        if code in etf_map:
            sell = compute_sell_signals(etf_map[code], realtime_price=price)
        else:
            sell = {"signals": [], "urgency": "暂无数据", "urgency_level": -1}

        data.append({
            "code":         code,
            "name":         q.get("name") or cached_row.get("name") or m.get("name", code),
            "category":     cached_row.get("category") or m.get("category", ""),
            "price":        price,
            "change_pct":   q.get("change_pct", cached_row.get("change_pct", 0)),
            "amount":       q.get("amount", 0),
            "fund_size":    cached_row.get("fund_size") or m.get("fund_size", 0),
            "premium_rate_pct": cached_row.get("premium_rate_pct"),
            "estimate_nav": cached_row.get("estimate_nav"),
            "nav_date":     cached_row.get("nav_date"),
            "rank":         rank_map.get(code),
            "trade_signal": cached_row.get("trade_signal"),
            "signal_sort":  cached_row.get("signal_sort"),
            "sell_signals": sell,
        })

    previous_state = _load_signal_state()
    data, next_state, changed_rows = _annotate_holding_signal_changes(data, previous_state)
    _save_signal_state(next_state)
    _notify_holding_signal_changes(changed_rows)

    return jsonify({
        "data":      data,
        "timestamp": now_str("timestamp_format"),
    })


# ── 前端页面 ────────────────────────────────────────────────────────────

@app.route("/api/config")
def api_config():
    return jsonify(web_runtime_config())


@app.route("/api/market/status")
def api_market_status():
    return jsonify(_market_status())


@app.route("/health")
def health():
    with _lock:
        status = _cache.get("status", "idle")
    return jsonify({"ok": True, "status": status, "market": _market_status()})


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
