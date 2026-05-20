#!/usr/bin/env python3
"""手动按指定日期回填 5 分钟历史分时行情到本地 SQLite 行情库。"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backfill_intraday import build_backfill_pool
from etf_investing.config import CONFIG
from etf_investing.market_data import (
    DEFAULT_INTRADAY_PERIOD,
    IntradayFetchResult,
    MarketDataStore,
    fetch_futu_intraday_history,
    normalize_code,
    normalize_period,
)

DEFAULT_FUTU_BATCH_SIZE = 55
DEFAULT_FUTU_PAUSE_SECONDS = 31.0


def _rows_for_date(df: pd.DataFrame, target_date: date) -> pd.DataFrame:
    if df is None or df.empty or "date" not in df.columns:
        return pd.DataFrame()
    data = df.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.date
    mask = data["date"] == target_date
    return data.loc[mask].reset_index(drop=True)


def backfill_intraday_for_date(
    codes: list[str],
    *,
    target_date: date,
    period: str = DEFAULT_INTRADAY_PERIOD,
    store: MarketDataStore | None = None,
    futu_batch_size: int = DEFAULT_FUTU_BATCH_SIZE,
    futu_pause_seconds: float = DEFAULT_FUTU_PAUSE_SECONDS,
) -> dict:
    """用 FUTU 历史分时接口回填指定日期的分钟行情。"""
    store = store or MarketDataStore()
    period = normalize_period(period)
    normalized_codes = [normalize_code(code) for code in codes if str(code).strip()]
    summary = {
        "success": True,
        "target_date": target_date.isoformat(),
        "period": period,
        "total_rows": 0,
        "items": [],
    }

    for index, code in enumerate(normalized_codes):
        if index > 0 and futu_batch_size > 0 and index % futu_batch_size == 0 and futu_pause_seconds > 0:
            time.sleep(futu_pause_seconds)
        fetched = fetch_futu_intraday_history(code, period=period, start_date=target_date, end_date=target_date)
        day_rows = _rows_for_date(fetched.df, target_date)
        saved = store.save_intraday(code, period, day_rows, fetched.source) if not day_rows.empty else 0
        ok = saved > 0
        error = fetched.error if fetched.error else (None if ok else f"futu {target_date.isoformat()} 分时数据为空")
        item = {"code": code, "success": ok, "source": fetched.source, "rows": saved, "error": error}
        summary["items"].append(item)
        summary["total_rows"] += saved
        store.log_request(
            endpoint="manual_backfill_date",
            code=code,
            period=period,
            days=1,
            source=fetched.source,
            success=ok,
            rows=saved,
            error=error,
            detail={"target_date": target_date.isoformat()},
        )

    summary["success"] = any(item["success"] for item in summary["items"])
    return summary


def _parse_target_date(value: str | None) -> date:
    if not value:
        return date.today()
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="手动回填指定日期 ETF 历史 5 分钟分时到本地行情库")
    parser.add_argument("--date", dest="target_date", help="落地日期，格式 YYYY-MM-DD；默认当天")
    parser.add_argument("--codes", help="逗号分隔 ETF 代码；传入后跳过综合标的池")
    parser.add_argument("--period", default=DEFAULT_INTRADAY_PERIOD, help="分钟周期：1/5/15/30/60，默认 5")
    parser.add_argument("--top", type=int, default=int(CONFIG["selection"].get("web_top_n", 50)), help="榜单模型筛选数量，默认 web_top_n")
    parser.add_argument("--min-amount", type=float, default=None, help="全市场池成交额门槛；默认使用配置")
    parser.add_argument("--max-count", type=int, default=None, help="全市场池最大扫描数量；默认使用配置")
    args = parser.parse_args()

    try:
        target = _parse_target_date(args.target_date)
    except ValueError:
        print("--date 格式错误，请使用 YYYY-MM-DD")
        return 2

    if args.codes:
        codes = [normalize_code(c) for c in args.codes.split(",") if c.strip()]
        print(f"手动按日期回填: {target.isoformat()}，手动 codes={len(codes)} 只，period={args.period}")
    else:
        selected = build_backfill_pool(top=args.top, min_amount=args.min_amount, max_count=args.max_count)
        codes = [item["code"] for item in selected]
        print(f"手动按日期回填: {target.isoformat()}，综合标的池={len(codes)} 只，period={args.period}")
        if selected:
            print("标的:", ",".join(f"{item['code']}({item.get('name') or ''};{'+'.join(item.get('sources', []))})" for item in selected))

    if not codes:
        print("未得到可回填标的")
        return 1

    result = backfill_intraday_for_date(codes, target_date=target, period=args.period)
    print(result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
