"""
ETF 中短线每日选股（全市场扫描）
运行: python etf_daily.py
     python etf_daily.py --top 5           # 只显示前5名
     python etf_daily.py --min-amount 1e8  # 提高流动性门槛至1亿
     python etf_daily.py --list            # 列出今日扫描范围
"""

import sys
import argparse
import logging
from .config import CONFIG, now_str

from .universe import fetch_universe
from .data import fetch_all_history, fetch_realtime
from .strategy import select_top

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

LINE = "─" * 68


def _sign(v: float) -> str:
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"


def _signals(r: dict) -> str:
    parts = []
    if r["ma_aligned"]:
        parts.append("↑均线多头")
    if r["macd_bullish"]:
        parts.append("⚡MACD看多")
    if r["vol_ratio"] >= 1.5:
        parts.append(f"🔥量比{r['vol_ratio']:.1f}x")
    if r["rsi"] < 45:
        parts.append("超卖回弹")
    return "  ".join(parts) if parts else "无明显信号"


def print_report(results: list, universe_total: int, scanned: int):
    print()
    print("=" * 68)
    print(f"  ETF 中短线选股报告   {now_str('report_time_format')}")
    print(f"  全市场扫描 {universe_total} 只 → 流动性筛选 {scanned} 只 → 优选 {len(results)} 只")
    print("=" * 68)

    for r in results:
        score_bar = "█" * int(r["score"] / 5) + "░" * (20 - int(r["score"] / 5))
        print()
        print(
            f"  #{r['rank']}  {r['code']}  {r['name']}  [{r['category']}]  "
            f"评分 {r['score']:.1f}  [{score_bar}]"
        )
        print(
            f"      价格 {r['price']:.3f}   "
            f"今日 {_sign(r['change_pct'])}   "
            f"3日 {_sign(r['ret3'])}   "
            f"5日 {_sign(r['ret5'])}   "
            f"10日 {_sign(r['ret10'])}"
        )
        print(
            f"      RSI {r['rsi']:.1f}   量比 {r['vol_ratio']:.2f}x   "
            f"动量得分 {r['momentum_score']:.0f}   "
            f"技术得分 {r['technical_score']:.0f}   "
            f"量能得分 {r['volume_score']:.0f}"
        )
        print(f"      信号: {_signals(r)}")
        print(f"  {LINE}")

    print()
    print("  因子权重: 动量35% + 量能25% + 技术25% + 趋势15%")
    print("  过滤规则: RSI>82 | 5日跌>9% | 破MA20且持续下跌")
    print("  风险提示: 本报告仅供参考，不构成投资建议。")
    print("=" * 68)
    print()


def list_universe(universe: list):
    cats: dict = {}
    for item in universe:
        cats.setdefault(item["category"], []).append(item)
    print(f"\n今日扫描范围: {len(universe)} 只 ETF（按成交额降序）\n")
    for cat, items in sorted(cats.items()):
        codes = "  ".join(f"{i['code']}" for i in items[:8])
        extra = f"…共{len(items)}只" if len(items) > 8 else ""
        print(f"  [{cat:<4}]  {codes}{extra}")
    print()


def main():
    parser = argparse.ArgumentParser(description="ETF 中短线每日选股（全市场）")
    parser.add_argument("--top",        type=int,   default=int(CONFIG["selection"]["default_top_n"]),  help="展示前N名（默认配置值）")
    parser.add_argument("--min-amount", type=float, default=float(CONFIG["selection"]["default_min_amount"]), help="日成交额门槛，单位元（默认配置值）")
    parser.add_argument("--max-count",  type=int,   default=int(CONFIG["selection"]["default_max_count"]), help="按成交额取前N只（默认配置值）")
    parser.add_argument("--list",       action="store_true",     help="列出今日扫描范围后退出")
    args = parser.parse_args()

    print(f"\n[1/4] 正在获取全市场 ETF 列表...")
    universe = fetch_universe(min_amount=args.min_amount, max_count=args.max_count)
    if not universe:
        print("错误：无法获取 ETF 列表，请检查网络连接。")
        sys.exit(1)

    # 获取实际全市场总数（用于报告展示）
    from .universe import _CACHE
    import json
    universe_total = len(universe)
    try:
        cached = json.loads(_CACHE.read_text(encoding="utf-8"))
        universe_total = len(cached.get("data", universe))
    except Exception:
        pass
    print(f"      全市场共 {universe_total} 只，流动性筛选后 {len(universe)} 只")

    if args.list:
        list_universe(universe)
        return

    print(f"\n[2/4] 正在并发获取 {len(universe)} 只 ETF 历史数据...")
    etf_map = fetch_all_history(universe, days=int(CONFIG["selection"]["history_days"]))
    print(f"      成功获取 {len(etf_map)} 只")

    if not etf_map:
        print("错误：无法获取行情数据，请检查网络连接。")
        sys.exit(1)

    print(f"\n[3/4] 正在获取实时行情...")
    realtime = fetch_realtime(list(etf_map.keys()))
    print(f"      成功获取 {len(realtime)} 只")

    print(f"\n[4/4] 运行多因子评分模型...\n")
    results = select_top(universe, etf_map, realtime, top_n=args.top)

    if not results:
        print("未找到符合条件的 ETF（可能处于非交易时段或数据异常）。")
        sys.exit(0)

    print_report(results, universe_total=universe_total, scanned=len(universe))


if __name__ == "__main__":
    main()
