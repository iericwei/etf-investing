"""
ETF 实时行情本地服务

特性：
  1. 实时行情优先级：mootdx → FUTU OpenD → 腾讯财经 → 东方财富
  2. 失败自动逐级降级
  3. 内置 5 秒缓存，避免重复请求
  4. CORS 已开启，可被 claude.ai 的前端直接调用
  5. 分时行情：FUTU OpenAPI + 东方财富历史分钟 K

依赖安装：
  pip install mootdx flask flask-cors requests akshare futu-api

启动：
  python etf_server.py
  服务地址：由 config.json 的 server.quote_port 配置

接口：
  GET /quote?codes=513130,518850,513100   返回多只 ETF 实时行情
  GET /quote?codes=513130&prefer=tencent  强制使用腾讯数据源
  GET /intraday?code=513130               返回 ETF 分时行情（默认1分钟）
  GET /intraday?code=513130&period=15     15分钟分时
  GET /intraday?code=513130&period=5&days=5  5分钟K线，最近5天
  GET /intraday/futu?code=513130&period=15  FUTU 当天 15 分钟分时
  GET /intraday?code=513130&period=15&source=futu  同上，兼容 /intraday 路由
  GET /health                              健康检查
"""

import re
import time
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import List, Dict

import requests
import pandas as pd
from .config import CONFIG, app_base_url, now_str, request_headers, tencent_realtime_url
from .data import _realtime_eastmoney, _realtime_futu, fetch_eastmoney_intraday_history
from .market_data import IntradayFetchResult, fetch_futu_today_intraday_history
from flask import Flask, jsonify, request
from flask_cors import CORS

# ---- mootdx 初始化（懒加载） ----
_mootdx_client = None
_mootdx_lock = threading.Lock()


def get_mootdx_client():
    global _mootdx_client
    if _mootdx_client is not None:
        return _mootdx_client if _mootdx_client is not False else None
    with _mootdx_lock:
        if _mootdx_client is None:
            try:
                from mootdx.quotes import Quotes
                _mootdx_client = Quotes.factory(market="std")
                print("[mootdx] 客户端初始化成功")
            except Exception as e:
                print(f"[mootdx] 初始化失败：{e}，降级使用腾讯财经")
                _mootdx_client = False
    return _mootdx_client if _mootdx_client else None


def detect_market(code: str) -> tuple:
    """根据代码判断交易所，返回 (mootdx_market_int, tencent_prefix)"""
    code = code.strip()
    if code.startswith(("5", "6", "9", "11", "12", "13", "18")):
        return 1, "sh"
    return 0, "sz"


# ---- mootdx 数据源 ----
def fetch_via_mootdx(codes: List[str]) -> List[Dict]:
    client = get_mootdx_client()
    if not client:
        return []
    results = []
    try:
        df = client.quotes(symbol=codes)
        if df is None or df.empty:
            return []
        for _, row in df.iterrows():
            code = str(row.get("code", "")).zfill(6)
            price = float(row.get("price", 0) or 0)
            last_close = float(row.get("last_close", 0) or 0)
            change_amt = price - last_close
            change_pct = (change_amt / last_close * 100) if last_close > 0 else 0.0
            results.append({
                "code": code,
                "name": row.get("name", code),
                "price": round(price, 4),
                "prev_close": round(last_close, 4),
                "change_pct": round(change_pct, 2),
                "change_amt": round(change_amt, 4),
                "open": round(float(row.get("open", 0) or 0), 4),
                "high": round(float(row.get("high", 0) or 0), 4),
                "low": round(float(row.get("low", 0) or 0), 4),
                "volume": int(row.get("vol", 0) or 0),
                "amount": float(row.get("amount", 0) or 0),
                "bid1": float(row.get("bid1", 0) or 0),
                "ask1": float(row.get("ask1", 0) or 0),
                "source": "mootdx",
                "updated": now_str("quote_updated_format"),
            })
        return results
    except Exception as e:
        print(f"[mootdx] 查询失败：{e}")
        return []


# ---- 腾讯财经数据源 ----
TENCENT_HEADERS = request_headers("tencent")


def _quote_map_to_list(quotes: Dict[str, dict]) -> List[Dict]:
    results = []
    for code, quote in quotes.items():
        price = float(quote.get("price") or 0)
        prev = float(quote.get("prev_close") or 0)
        change_amt = float(quote.get("change_amt", price - prev if prev > 0 else 0) or 0)
        change_pct = float(quote.get("change_pct") or 0)
        results.append({
            "code": code,
            "name": quote.get("name") or code,
            "price": round(price, 4),
            "prev_close": round(prev, 4),
            "change_pct": round(change_pct, 2),
            "change_amt": round(change_amt, 4),
            "open": round(float(quote.get("open") or 0), 4),
            "high": round(float(quote.get("high") or 0), 4),
            "low": round(float(quote.get("low") or 0), 4),
            "volume": int(float(quote.get("volume") or 0)),
            "amount": float(quote.get("amount") or 0),
            "bid1": round(float(quote.get("bid1") or 0), 4),
            "ask1": round(float(quote.get("ask1") or 0), 4),
            "turnover": float(quote.get("turnover") or 0),
            "source": quote.get("source") or "unknown",
            "updated": quote.get("updated") or now_str("quote_updated_format"),
        })
    return results


def fetch_via_futu(codes: List[str]) -> List[Dict]:
    return _quote_map_to_list(_realtime_futu(codes))


def fetch_via_tencent(codes: List[str]) -> List[Dict]:
    if not codes:
        return []
    qq_codes = []
    for c in codes:
        _, prefix = detect_market(c)
        qq_codes.append(f"{prefix}{c}")
    url = tencent_realtime_url(qq_codes)
    try:
        r = requests.get(url, headers=TENCENT_HEADERS, timeout=CONFIG["network"]["timeouts"]["tencent_realtime"])
        r.encoding = "gbk"
        text = r.text
    except Exception as e:
        print(f"[tencent] 请求失败：{e}")
        return []

    results = []
    for line in text.strip().split("\n"):
        m = re.match(r'^v_\w+="([^"]+)"', line.strip().rstrip(";"))
        if not m:
            continue
        f = m.group(1).split("~")
        if len(f) < 40:
            continue
        try:
            results.append({
                "code": f[2],
                "name": f[1],
                "price": round(float(f[3] or 0), 4),
                "prev_close": round(float(f[4] or 0), 4),
                "change_pct": round(float(f[32] or 0), 2),
                "change_amt": round(float(f[31] or 0), 4),
                "open": round(float(f[5] or 0), 4),
                "high": round(float(f[33] or 0), 4),
                "low": round(float(f[34] or 0), 4),
                "volume": int(float(f[6] or 0)),
                "amount": float(f[37] or 0) * 10000,  # 万元 → 元
                "bid1": round(float(f[9] or 0), 4),
                "ask1": round(float(f[19] or 0), 4),
                "turnover": float(f[38] or 0),
                "source": "tencent",
                "updated": f[30][8:14] if len(f[30]) >= 14 else now_str("quote_updated_compact_format"),
            })
        except (ValueError, IndexError) as e:
            print(f"[tencent] 解析失败：{e}")
            continue
    return results


def fetch_via_eastmoney(codes: List[str]) -> List[Dict]:
    return _quote_map_to_list(_realtime_eastmoney(codes))


# ---- 分时行情（东方财富 push2his，自封装；akshare 仅保留为健康检查兼容） ----

_VALID_PERIODS = {"1", "5", "15", "30", "60"}

_akshare_available: bool | None = None
_akshare_lock = threading.Lock()
_futu_available: bool | None = None
_futu_lock = threading.Lock()


def _check_akshare() -> bool:
    global _akshare_available
    if _akshare_available is not None:
        return _akshare_available
    with _akshare_lock:
        if _akshare_available is None:
            try:
                import akshare  # noqa: F401
                _akshare_available = True
                print("[akshare] 可用")
            except Exception as e:
                _akshare_available = False
                print(f"[akshare] 不可用: {e}")
    return _akshare_available


def _check_futu() -> bool:
    global _futu_available
    if _futu_available is not None:
        return _futu_available
    with _futu_lock:
        if _futu_available is None:
            try:
                import futu as ft
                ctx = ft.OpenQuoteContext(
                    host=CONFIG.get("futu", {}).get("host", "127.0.0.1"),
                    port=int(CONFIG.get("futu", {}).get("port", 11111)),
                )
                try:
                    ret, _data = ctx.get_global_state()
                    _futu_available = ret == ft.RET_OK
                finally:
                    ctx.close()
                print(f"[futu] OpenD {'可用' if _futu_available else '不可用'}")
            except Exception as e:
                _futu_available = False
                print(f"[futu] 不可用: {e}")
    return bool(_futu_available)


@contextmanager
def _requests_without_env_proxy():
    """akshare 内部直接调用 requests.get；临时关闭环境代理避免本机代理不可用导致接口失败。"""
    original_request = requests.sessions.Session.request

    def request_no_env_proxy(self, method, url, **kwargs):
        old_trust_env = self.trust_env
        self.trust_env = False
        try:
            return original_request(self, method, url, **kwargs)
        finally:
            self.trust_env = old_trust_env

    requests.sessions.Session.request = request_no_env_proxy
    try:
        yield
    finally:
        requests.sessions.Session.request = original_request


def _intraday_frame_to_response(code: str, period: str, days: int, df: pd.DataFrame, source: str = "eastmoney") -> dict:
    if df is None or df.empty:
        return {"success": False, "code": code, "source": source, "error": f"{source} 返回空数据"}

    rows = []
    for _, row in df.sort_values("datetime").iterrows():
        dt = pd.to_datetime(row["datetime"])
        rows.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M"),
            "date":     dt.strftime("%Y-%m-%d"),
            "time":     str(row.get("time") or dt.strftime("%H:%M")),
            "open":     round(float(row["open"]), 4) if pd.notna(row.get("open")) else 0,
            "close":    round(float(row["close"]), 4) if pd.notna(row.get("close")) else 0,
            "high":     round(float(row["high"]), 4) if pd.notna(row.get("high")) else 0,
            "low":      round(float(row["low"]), 4) if pd.notna(row.get("low")) else 0,
            "volume":   int(row["volume"]) if pd.notna(row.get("volume")) else 0,
            "amount":   round(float(row["amount"]), 2) if pd.notna(row.get("amount")) else 0,
        })

    return {
        "success":   True,
        "code":      code,
        "period":    period,
        "days":      days,
        "source":    source,
        "count":     len(rows),
        "data":      rows,
        "timestamp": now_str("timestamp_format"),
    }


def _fetch_intraday(code: str, period: str = "1", days: int = 5) -> dict:
    """
    通过自封装东方财富 push2his 接口获取 ETF 分时行情。

    参数:
      code   : 6 位 ETF 代码
      period : K 线周期，可选 "1", "5", "15", "30", "60"（分钟）
      days   : 回溯天数

    返回:
      {"success": True, "code": ..., "period": ..., "source": "eastmoney", "count": N, "data": [...]}
      失败时返回 {"success": False, "code": ..., "error": "..."}
    """
    code = str(code).strip().zfill(6)[-6:]
    period = str(period)
    days = max(int(days), 1)

    if period not in _VALID_PERIODS:
        return {"success": False, "code": code, "error": f"不支持的 period={period}，可选: {sorted(_VALID_PERIODS)}"}

    try:
        df = fetch_eastmoney_intraday_history(code, period=period, days=days)
        return _intraday_frame_to_response(code, period, days, df, source="eastmoney")
    except Exception as e:
        return {"success": False, "code": code, "source": "eastmoney", "error": str(e)}


# ---- 分时行情缓存 ----
_intraday_cache: dict = {}
_intraday_cache_lock = threading.Lock()
# 分时数据缓存 TTL：1 分钟 K 线 30 秒，其余 60 秒
_INTRADAY_CACHE_TTL = {"1": 30, "5": 60, "15": 60, "30": 120, "60": 120}


def _fetch_intraday_cached(code: str, period: str = "1", days: int = 5) -> dict:
    cache_key = f"{code}:{period}:{days}"
    ttl = _INTRADAY_CACHE_TTL.get(period, 60)
    now = time.time()

    with _intraday_cache_lock:
        cached = _intraday_cache.get(cache_key)
        if cached and now - cached["t"] < ttl:
            return cached["data"]

    result = _fetch_intraday(code, period, days)

    with _intraday_cache_lock:
        _intraday_cache[cache_key] = {"t": now, "data": result}

    return result


# ---- 统一查询 ----
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = int(CONFIG["server"]["quote_cache_ttl_seconds"])


def fetch_quotes(codes: List[str], prefer: str = "auto") -> Dict:
    now = time.time()
    cache_key = f"{prefer}:{','.join(sorted(codes))}"
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached["t"] < CACHE_TTL:
            return cached["data"]

    source_fetchers = {
        "mootdx": fetch_via_mootdx,
        "futu": fetch_via_futu,
        "tencent": fetch_via_tencent,
        "eastmoney": fetch_via_eastmoney,
    }
    source_order = ["mootdx", "futu", "tencent", "eastmoney"]
    if prefer != "auto":
        source_order = [prefer] if prefer in source_fetchers else source_order

    final_map: Dict[str, Dict] = {}
    used_sources: list[str] = []
    for source in source_order:
        missing = [code for code in codes if code not in final_map]
        if not missing:
            break
        data = source_fetchers[source](missing)
        if data:
            used_sources.append(source)
        for item in data:
            final_map[item["code"]] = item

    final = [final_map[code] for code in codes if code in final_map]

    result = {
        "success": True,
        "count": len(final),
        "data": final,
        "sources": {
            "primary": used_sources[0] if used_sources else source_order[0],
            "order": source_order,
            "used": used_sources,
            "fallback_used": len(used_sources) > 1,
        },
        "timestamp": now_str("timestamp_format"),
    }
    with _cache_lock:
        _cache[cache_key] = {"t": now, "data": result}
    return result


# ---- Flask ----
app = Flask(__name__)
CORS(app)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


@app.route("/")
def index():
    return """
    <html><head><title>ETF 行情服务</title>
    <style>
      body { font-family: -apple-system, Segoe UI, sans-serif; max-width: 680px; margin: 40px auto; padding: 20px; color: #333; }
      code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-family: Menlo, monospace; font-size: 13px; }
      h2 { color: #222; }
      .tag { display: inline-block; padding: 2px 8px; background: #1d9e75; color: #fff; border-radius: 3px; font-size: 12px; margin-left: 8px; }
      .tag-new { background: #e67e22; }
      a { color: #0066cc; }
      table { border-collapse: collapse; margin: 8px 0; }
      td, th { border: 1px solid #ddd; padding: 4px 10px; font-size: 13px; }
      th { background: #f8f8f8; }
    </style></head><body>
    <h2>📈 ETF 实时行情服务<span class="tag">RUNNING</span></h2>
    <p>数据源：mootdx → FUTU OpenD → 腾讯财经 → 东方财富</p>
    <h3>接口</h3>
    <ul>
      <li><a href="/quote?codes=513130,518850,513100,513050,159202,515880,588990">/quote?codes=513130,518850,...</a></li>
      <li><a href="/quote?codes=513130&prefer=tencent">/quote?codes=513130&prefer=tencent</a> — 强制腾讯源</li>
      <li><a href="/intraday?code=513130">/intraday?code=513130</a> <span class="tag tag-new">NEW</span> — 分时行情（默认1分钟）</li>
      <li><a href="/intraday?code=513130&period=5">/intraday?code=513130&period=5</a> — 5分钟K线</li>
      <li><a href="/intraday?code=513130&period=15&days=10">/intraday?code=513130&period=15&days=10</a> — 15分钟K线，10天</li>
      <li><a href="/intraday?code=513130&period=15&source=futu">/intraday?code=513130&period=15&source=futu</a> — FUTU 当天分时</li>
      <li><a href="/intraday/futu?code=513130&period=15">/intraday/futu?code=513130&period=15</a> — FUTU 当天分时专用接口</li>
      <li><a href="/health">/health</a></li>
    </ul>
    <h3>分时行情参数 <span class="tag tag-new">NEW</span></h3>
    <table>
      <tr><th>参数</th><th>说明</th><th>默认</th></tr>
      <tr><td><code>code</code></td><td>6位ETF代码（必填）</td><td>—</td></tr>
      <tr><td><code>period</code></td><td>K线周期：1/5/15/30/60 分钟</td><td>1</td></tr>
      <tr><td><code>days</code></td><td>回溯天数（1-60）；<code>source=futu</code> 时固定返回当天数据</td><td>5</td></tr>
      <tr><td><code>source</code></td><td>可选 <code>futu</code>，强制通过 FUTU OpenAPI 获取当天分时</td><td>eastmoney</td></tr>
    </table>
    <p>在 claude.ai 监控工具中填入：<code>__QUOTE_BASE_URL__</code></p>
    </body></html>
    """.replace("__QUOTE_BASE_URL__", app_base_url("quote_port"))


@app.route("/quote")
def quote():
    codes_param = request.args.get("codes", "")
    prefer = request.args.get("prefer", "auto")
    codes = [c.strip() for c in codes_param.split(",") if c.strip()]
    if not codes:
        return jsonify({"success": False, "error": "请传入 codes 参数"}), 400
    return jsonify(fetch_quotes(codes, prefer=prefer))


def _validate_intraday_request(default_period: str = "15") -> tuple[str | None, str | None, int | None, tuple[dict, int] | None]:
    code = (request.args.get("code") or "").strip()
    if not code or not re.match(r"^\d{6}$", code.zfill(6)[-6:]):
        return None, None, None, ({"success": False, "error": "请传入有效的 6 位 ETF 代码，如 code=513130"}, 400)
    code = code.zfill(6)[-6:]

    period = request.args.get("period", default_period)
    if period not in _VALID_PERIODS:
        return code, None, None, ({"success": False, "error": f"period 不支持 {period}，可选: {sorted(_VALID_PERIODS)}"}, 400)

    days_str = request.args.get("days", "5")
    try:
        days = int(days_str)
    except ValueError:
        return code, period, None, ({"success": False, "error": f"days 必须为整数，收到: {days_str}"}, 400)

    return code, period, max(min(days, 60), 1), None


def _fetch_futu_today_response(code: str, period: str) -> dict:
    try:
        result = fetch_futu_today_intraday_history(code, period=period)
        if result.df is None or result.df.empty:
            return {"success": False, "code": code, "period": period, "days": 1, "source": "futu", "error": result.error or "futu 返回空数据"}
        return _intraday_frame_to_response(code, period, 1, result.df, source="futu")
    except Exception as e:
        return {"success": False, "code": code, "period": period, "days": 1, "source": "futu", "error": str(e)}


@app.route("/intraday/futu")
def intraday_futu():
    """FUTU OpenAPI 当天分时行情接口。"""
    code, period, _days, error = _validate_intraday_request(default_period="15")
    if error:
        payload, status = error
        return jsonify(payload), status
    assert code is not None and period is not None
    result = _fetch_futu_today_response(code, period)
    return jsonify(result), 200 if result.get("success") else 502


@app.route("/intraday")
def intraday():
    """
    ETF 分时行情接口（东方财富 push2his）

    参数:
      code   : 6 位 ETF 代码（必填）
      period : K 线周期，可选 1/5/15/30/60 分钟（默认 1）
      days   : 回溯天数（默认 5）
    """
    code, period, days, error = _validate_intraday_request(default_period="15")
    if error:
        payload, status = error
        return jsonify(payload), status
    assert code is not None and period is not None and days is not None
    if request.args.get("source", "").lower() == "futu":
        result = _fetch_futu_today_response(code, period)
        return jsonify(result), 200 if result.get("success") else 502

    result = _fetch_intraday_cached(code, period, days)
    status_code = 200 if result.get("success") else 502
    return jsonify(result), status_code


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "mootdx_available": get_mootdx_client() is not None,
        "akshare_available": _check_akshare(),
        "futu_available": _check_futu(),
        "futu_host": CONFIG.get("futu", {}).get("host", "127.0.0.1"),
        "futu_port": int(CONFIG.get("futu", {}).get("port", 11111)),
        "time": now_str("timestamp_format"),
    })


def main():
    print("=" * 56)
    print("📈 ETF 实时行情服务")
    print("=" * 56)
    base_url = app_base_url("quote_port")
    print(f"地址  : {base_url}")
    print(f"测试  : {base_url}/quote?codes=513130,518850")
    print(f"分时  : {base_url}/intraday?code=513130")
    print(f"FUTU  : {base_url}/intraday/futu?code=513130&period=15")
    print("数据源: mootdx → FUTU OpenD → 腾讯财经 → 东方财富")
    print("=" * 56)
    app.run(host=CONFIG["server"]["host"], port=int(CONFIG["server"]["quote_port"]), debug=bool(CONFIG["server"]["debug"]))


if __name__ == "__main__":
    main()
