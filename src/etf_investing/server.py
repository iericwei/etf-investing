"""
ETF 实时行情本地服务（mootdx + 腾讯财经 双数据源）

特性：
  1. 优先使用 mootdx（通达信原生协议，速度快、无频次限制）
  2. 失败自动降级到腾讯财经 HTTP 接口
  3. 内置 5 秒缓存，避免重复请求
  4. CORS 已开启，可被 claude.ai 的前端直接调用

依赖安装：
  pip install mootdx flask flask-cors requests

启动：
  python etf_server.py
  服务地址：由 config.json 的 server.quote_port 配置

接口：
  GET /quote?codes=513130,518850,513100   返回多只 ETF 实时行情
  GET /quote?codes=513130&prefer=tencent  强制使用腾讯数据源
  GET /health                              健康检查
"""

import re
import time
import logging
import threading
from typing import List, Dict

import requests
from .config import CONFIG, app_base_url, now_str, request_headers, tencent_realtime_url
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

    primary_data = []
    primary_source = ""

    if prefer in ("auto", "mootdx"):
        primary_data = fetch_via_mootdx(codes)
        primary_source = "mootdx" if primary_data else ""

    got = {d["code"] for d in primary_data}
    missing = [c for c in codes if c not in got]

    tencent_data = []
    if missing or prefer == "tencent":
        tencent_data = fetch_via_tencent(missing if prefer == "auto" else codes)

    tencent_map = {d["code"]: d for d in tencent_data}
    final = []
    for d in primary_data:
        if (not d.get("name") or d["name"] == d["code"]) and d["code"] in tencent_map:
            d["name"] = tencent_map[d["code"]]["name"]
        final.append(d)
    for code in missing:
        if code in tencent_map:
            final.append(tencent_map[code])

    if prefer == "tencent":
        final = tencent_data

    result = {
        "success": True,
        "count": len(final),
        "data": final,
        "sources": {
            "primary": primary_source or "tencent",
            "fallback_used": bool(tencent_data) and primary_source == "mootdx",
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
      a { color: #0066cc; }
    </style></head><body>
    <h2>📈 ETF 实时行情服务<span class="tag">RUNNING</span></h2>
    <p>数据源：mootdx（主）+ 腾讯财经（备）</p>
    <h3>接口</h3>
    <ul>
      <li><a href="/quote?codes=513130,518850,513100,513050,159202,515880,588990">/quote?codes=513130,518850,...</a></li>
      <li><a href="/quote?codes=513130&prefer=tencent">/quote?codes=513130&prefer=tencent</a> — 强制腾讯源</li>
      <li><a href="/health">/health</a></li>
    </ul>
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


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "mootdx_available": get_mootdx_client() is not None,
        "time": now_str("timestamp_format"),
    })


def main():
    print("=" * 56)
    print("📈 ETF 实时行情服务")
    print("=" * 56)
    base_url = app_base_url("quote_port")
    print(f"地址  : {base_url}")
    print(f"测试  : {base_url}/quote?codes=513130,518850")
    print("数据源: mootdx（主）+ 腾讯财经（备）")
    print("=" * 56)
    app.run(host=CONFIG["server"]["host"], port=int(CONFIG["server"]["quote_port"]), debug=bool(CONFIG["server"]["debug"]))


if __name__ == "__main__":
    main()
