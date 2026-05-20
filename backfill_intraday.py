#!/usr/bin/env python3
"""收盘后运行：按综合标的池回填分钟行情并入库。"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing.config import BASE_DIR, CONFIG
from etf_investing.data import fetch_all_history, fetch_realtime
from etf_investing.market_data import DEFAULT_INTRADAY_PERIOD, backfill_intraday_history, normalize_code
from etf_investing.strategy import select_top
from etf_investing.universe import fetch_universe

WATCHLIST_FILE = BASE_DIR / "watchlist.json"
HOLDINGS_FILE = BASE_DIR / "holdings.json"
DEFAULT_HISTORY_DAYS = 30


def _load_codes_file(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    codes: list[str] = []
    for item in data:
        code = normalize_code(str(item))
        if code.isdigit() and code not in codes:
            codes.append(code)
    return codes


def _load_watchlist_codes() -> list[str]:
    return _load_codes_file(WATCHLIST_FILE)


def _load_holding_codes() -> list[str]:
    return _load_codes_file(HOLDINGS_FILE)


def _merge_pool_item(pool: dict[str, dict], code: str, source: str, *, item: dict | None = None) -> None:
    code = normalize_code(code)
    if not code.isdigit():
        return
    current = pool.setdefault(
        code,
        {
            "code": code,
            "name": (item or {}).get("name") or code,
            "category": (item or {}).get("category", ""),
            "score": (item or {}).get("score"),
            "sources": [],
        },
    )
    if item:
        if item.get("name") and current.get("name") == code:
            current["name"] = item.get("name")
        if item.get("category") and not current.get("category"):
            current["category"] = item.get("category")
        if item.get("score") is not None and current.get("score") is None:
            current["score"] = item.get("score")
    if source not in current["sources"]:
        current["sources"].append(source)


def build_model_selected_pool(
    *,
    top: int | None = None,
    min_amount: float | None = None,
    max_count: int | None = None,
) -> list[dict]:
    """用当前多因子模型从全市场 ETF 池筛出榜单标的。"""
    top = int(top if top is not None else CONFIG["selection"].get("web_top_n", CONFIG["selection"]["default_top_n"]))
    universe = fetch_universe(min_amount=min_amount, max_count=max_count)
    if not universe:
        return []
    etf_map = fetch_all_history(universe, days=int(CONFIG["selection"]["history_days"]))
    if not etf_map:
        return []
    realtime = fetch_realtime(list(etf_map.keys()))
    results = select_top(universe, etf_map, realtime, top_n=top, include_backtest=False)
    return [
        {
            "code": normalize_code(str(item["code"])),
            "name": item.get("name") or item.get("code"),
            "category": item.get("category", ""),
            "score": item.get("score"),
        }
        for item in results
        if item.get("code")
    ]


def build_backfill_pool(
    *,
    top: int | None = None,
    min_amount: float | None = None,
    max_count: int | None = None,
) -> list[dict]:
    """合并榜单、自选、持仓、硬过滤后的全市场池，作为本地行情库落地标的。"""
    top = int(top if top is not None else CONFIG["selection"].get("web_top_n", CONFIG["selection"]["default_top_n"]))
    hard_filtered = fetch_universe(min_amount=min_amount, max_count=max_count)
    leaderboard: list[dict] = []
    if hard_filtered:
        etf_map = fetch_all_history(hard_filtered, days=int(CONFIG["selection"]["history_days"]))
        if etf_map:
            realtime = fetch_realtime(list(etf_map.keys()))
            leaderboard = select_top(hard_filtered, etf_map, realtime, top_n=top, include_backtest=False)

    pool: dict[str, dict] = {}
    for item in leaderboard:
        if item.get("code"):
            _merge_pool_item(pool, str(item["code"]), "leaderboard", item=item)
    for code in _load_watchlist_codes():
        _merge_pool_item(pool, code, "watchlist")
    for code in _load_holding_codes():
        _merge_pool_item(pool, code, "holdings")
    for item in hard_filtered:
        if item.get("code"):
            _merge_pool_item(pool, str(item["code"]), "hard_filter", item=item)
    return list(pool.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 ETF 分钟行情到本地行情库")
    parser.add_argument("--codes", help="逗号分隔 ETF 代码；传入后跳过综合标的池")
    parser.add_argument("--period", default=DEFAULT_INTRADAY_PERIOD, help="分钟周期：1/5/15/30/60，默认 5")
    parser.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS, help="FUTU 历史分时回溯天数，默认 30")
    parser.add_argument("--top", type=int, default=int(CONFIG["selection"].get("web_top_n", 50)), help="榜单模型筛选数量，默认 web_top_n")
    parser.add_argument("--min-amount", type=float, default=None, help="全市场池成交额门槛；默认使用配置")
    parser.add_argument("--max-count", type=int, default=None, help="全市场池最大扫描数量；默认使用配置")
    args = parser.parse_args()

    if args.codes:
        codes = [normalize_code(c) for c in args.codes.split(",") if c.strip()]
        print(f"使用手动 codes 回填: {len(codes)} 只，period={args.period}, days={args.days}")
    else:
        selected = build_backfill_pool(top=args.top, min_amount=args.min_amount, max_count=args.max_count)
        codes = [item["code"] for item in selected]
        print(f"使用综合标的池回填: {len(codes)} 只，period={args.period}, days={args.days}")
        if selected:
            print("标的:", ",".join(f"{item['code']}({item.get('name') or ''};{'+'.join(item.get('sources', []))})" for item in selected))

    if not codes:
        print("未得到可回填标的")
        return 1
    result = backfill_intraday_history(codes, period=args.period, days=args.days)
    print(result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
