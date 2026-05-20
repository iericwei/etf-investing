"""独立本地行情库服务。"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS

from .config import CONFIG, now_str
from .market_data import (
    DEFAULT_INTRADAY_PERIOD,
    DEFAULT_DB_PATH,
    MarketDataStore,
    backfill_today,
    get_intraday_from_store_or_fetch,
    normalize_code,
    normalize_period,
)

app = Flask(__name__)
CORS(app)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

_STORE = MarketDataStore()


def _frame_to_payload(df: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    if df is None or df.empty:
        return rows
    for _, row in df.sort_values("datetime").iterrows():
        dt = pd.to_datetime(row["datetime"])
        rows.append(
            {
                "datetime": dt.strftime("%Y-%m-%d %H:%M"),
                "date": dt.strftime("%Y-%m-%d"),
                "time": str(row.get("time") or dt.strftime("%H:%M")),
                "open": round(float(row.get("open") or 0), 4),
                "close": round(float(row.get("close") or 0), 4),
                "high": round(float(row.get("high") or 0), 4),
                "low": round(float(row.get("low") or 0), 4),
                "volume": int(float(row.get("volume") or 0)),
                "amount": round(float(row.get("amount") or 0), 2),
                "source": row.get("source", "local_store"),
            }
        )
    return rows


@app.route("/health")
def health():
    return jsonify({"status": "ok", "db": str(DEFAULT_DB_PATH), "time": now_str("timestamp_format")})


@app.route("/intraday")
def intraday():
    code = (request.args.get("code") or "").strip()
    if not code or not re.match(r"^\d{6}$", code.zfill(6)[-6:]):
        payload = {"success": False, "error": "请传入有效的 6 位 ETF 代码，如 code=513180"}
        _STORE.log_request(endpoint="intraday", success=False, rows=0, error=payload["error"])
        return jsonify(payload), 400
    code = normalize_code(code)

    try:
        period = normalize_period(request.args.get("period", DEFAULT_INTRADAY_PERIOD))
    except ValueError as e:
        _STORE.log_request(endpoint="intraday", code=code, success=False, rows=0, error=str(e))
        return jsonify({"success": False, "code": code, "error": str(e)}), 400

    try:
        days = max(min(int(request.args.get("days", "5")), 365), 1)
    except ValueError:
        error = f"days 必须为整数，收到: {request.args.get('days')}"
        _STORE.log_request(endpoint="intraday", code=code, period=period, success=False, rows=0, error=error)
        return jsonify({"success": False, "code": code, "error": error}), 400

    refresh = str(request.args.get("refresh", "0")).lower() in {"1", "true", "yes", "y"}
    df, source, error = get_intraday_from_store_or_fetch(code, period, days, refresh=refresh, store=_STORE)
    data = _frame_to_payload(df)
    success = bool(data)
    _STORE.log_request(
        endpoint="intraday",
        code=code,
        period=period,
        days=days,
        source=source,
        success=success,
        rows=len(data),
        error=error if not success else None,
        detail={"refresh": refresh},
    )
    status_code = 200 if success else 502
    return jsonify(
        {
            "success": success,
            "code": code,
            "period": period,
            "days": days,
            "source": source,
            "count": len(data),
            "data": data,
            "error": error if not success else None,
            "timestamp": now_str("timestamp_format"),
        }
    ), status_code


@app.route("/backfill/today", methods=["GET", "POST"])
def backfill_today_route():
    codes_param = request.values.get("codes", "")
    codes = [normalize_code(c) for c in codes_param.split(",") if c.strip()]
    if not codes:
        error = "请传入 codes 参数，如 codes=513180,513130"
        _STORE.log_request(endpoint="backfill_today", success=False, rows=0, error=error)
        return jsonify({"success": False, "error": error}), 400
    try:
        period = normalize_period(request.values.get("period", DEFAULT_INTRADAY_PERIOD))
    except ValueError as e:
        _STORE.log_request(endpoint="backfill_today", success=False, rows=0, error=str(e))
        return jsonify({"success": False, "error": str(e)}), 400
    result = backfill_today(codes, period=period, store=_STORE)
    return jsonify(result), 200 if result.get("success") else 502


@app.route("/logs")
def logs():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    return jsonify({"success": True, "data": _STORE.recent_logs(limit=limit)})


def main():
    port = int(CONFIG.get("server", {}).get("market_data_port", 5680))
    host = CONFIG.get("server", {}).get("host", "0.0.0.0")
    print("=" * 56)
    print("📚 ETF 本地分钟行情库服务")
    print("=" * 56)
    print(f"地址  : http://localhost:{port}")
    print(f"DB    : {DEFAULT_DB_PATH}")
    print(f"测试  : http://localhost:{port}/intraday?code=***&period={DEFAULT_INTRADAY_PERIOD}&days=5")
    print(f"回填  : http://localhost:{port}/backfill/today?codes=513180,513130&period={DEFAULT_INTRADAY_PERIOD}")
    print("=" * 56)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
