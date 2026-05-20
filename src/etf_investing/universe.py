"""
全市场 ETF 列表（东方财富数据源）
每日本地缓存，避免重复拉取
"""

import json
import logging
import requests
from .config import BASE_DIR, CONFIG, request_headers, today_str

logger = logging.getLogger(__name__)

_CACHE = BASE_DIR / ".universe_cache.json"

_URL = CONFIG["urls"]["eastmoney_universe"]
_HEADERS = request_headers("eastmoney")

# 排除类别（不适合短线动量策略：货币/债券/理财类）
_EXCLUDE_KEYWORDS = [
    "货币", "理财", "现金", "债", "利率", "同业",
    "日利", "添益", "短融", "优享", "活期", "安盈", "薪金宝",
]
_EXCLUDE_PREFIXES = ["511", "519", "177", "170"]   # 债券ETF(511xxx)和货币ETF代码段


def _category(name: str) -> str:
    """从 ETF 名称中提取主题归类，用于榜单集中展示。
    例: '半导体设备ETF国泰' -> '半导体设备', '光伏ETF' -> '光伏'
    """
    idx = name.find("ETF")
    if idx > 0:
        return name[:idx].strip()
    # 联接基金等其他类型
    for marker in ["联接", "LOF", "REIT"]:
        idx = name.find(marker)
        if idx > 0:
            return name[:idx].strip()
    return name.strip()


def _excluded(code: str, name: str) -> bool:
    if any(kw in name for kw in _EXCLUDE_KEYWORDS):
        return True
    if any(code.startswith(p) for p in _EXCLUDE_PREFIXES):
        return True
    return False


def fetch_universe(min_amount: float | None = None, max_count: int | None = None,
                   force: bool = False) -> list:
    """
    获取全市场 ETF 列表

    min_amount : 日成交额最低门槛（元），默认 5000 万
    max_count  : 按成交额降序取前 N 只，防止历史数据拉取过慢
    force      : 忽略今日缓存，强制重拉

    返回: [{"code", "name", "category", "market", "price", "change_pct", "amount", "fund_size"}]
    """
    min_amount = float(min_amount if min_amount is not None else CONFIG["selection"]["default_min_amount"])
    max_count = int(max_count if max_count is not None else CONFIG["selection"]["default_max_count"])
    today = today_str()

    if not force and _CACHE.exists():
        try:
            cached = json.loads(_CACHE.read_text(encoding="utf-8"))
            if cached.get("date") == today:
                items = cached["data"]
                if items and not any("fund_size" in item for item in items[:20]):
                    raise ValueError("缓存缺少基金规模字段，重新拉取")
                result = _apply_filter(items, min_amount, max_count)
                logger.info(
                    f"[universe] 缓存: 全市场 {len(items)} 只 → "
                    f"流动性筛选后 {len(result)} 只"
                )
                return result
        except Exception:
            pass

    try:
        session = requests.Session()
        session.trust_env = False
        r = session.get(_URL, headers=_HEADERS, timeout=CONFIG["network"]["timeouts"]["eastmoney_universe"])
        r.raise_for_status()
        rows = r.json().get("data", {}).get("diff") or []
    except Exception as e:
        logger.error(f"[universe] 东方财富接口失败: {e}")
        # 降级使用静态候选池
        try:
            from .pool import ETF_POOL
            logger.warning("[universe] 降级使用静态候选池")
            return ETF_POOL
        except ImportError:
            return []

    items = []
    for row in rows:
        code   = str(row.get("f12", "")).zfill(6)
        name   = str(row.get("f14") or "")
        market = "sh" if int(row.get("f13") or 0) == 1 else "sz"
        price  = float(row.get("f2") or 0)
        chg    = float(row.get("f3") or 0)
        amount = float(row.get("f6") or 0)
        fund_size = float(row.get("f20") or 0)

        if price <= 0 or _excluded(code, name):
            continue

        items.append({
            "code":       code,
            "name":       name,
            "category":   _category(name),
            "market":     market,
            "price":      price,
            "change_pct": chg,
            "amount":     amount,
            "fund_size":  fund_size,
        })

    try:
        _CACHE.write_text(
            json.dumps({"date": today, "data": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    result = _apply_filter(items, min_amount, max_count)
    logger.info(
        f"[universe] 东方财富: 全市场 {len(items)} 只 → "
        f"成交额≥{min_amount/1e8:.1f}亿 且 前{max_count}只: {len(result)} 只"
    )
    return result


def _apply_filter(items: list, min_amount: float, max_count: int) -> list:
    filtered = [i for i in items if i.get("amount", 0) >= min_amount]
    if not filtered and items:
        logger.warning(
            f"[universe] 当前成交额均低于 {min_amount/1e8:.1f} 亿，"
            "降级按成交额排序取前 N 只，避免早盘榜单为空"
        )
        filtered = list(items)
    filtered.sort(key=lambda x: x.get("amount", 0), reverse=True)
    return filtered[:max_count]
