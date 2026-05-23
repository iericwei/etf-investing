"""
全市场 ETF 列表
优先级：mootdx → FUTU OpenD → 东方财富
每日本地缓存，避免重复拉取
"""

import json
import logging
import requests
from .config import BASE_DIR, CONFIG, request_headers, today_str

logger = logging.getLogger(__name__)

_CACHE = BASE_DIR / ".universe_cache.json"
_CACHE_SOURCE_ORDER = ["mootdx", "futu", "eastmoney"]

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


def _market_from_code(code: str) -> str:
    return "sh" if str(code).startswith(("5", "6", "9")) else "sz"


def _base_item(code: str, name: str, *, source: str) -> dict | None:
    code = str(code or "").split(".")[-1].zfill(6)[-6:]
    name = str(name or "")
    if not code.isdigit() or _excluded(code, name):
        return None
    return {
        "code":       code,
        "name":       name or code,
        "category":   _category(name),
        "market":     _market_from_code(code),
        "price":      0,
        "change_pct": 0,
        "amount":     0,
        "fund_size":  0,
        "source":     source,
    }


def _enrich_with_realtime(items: list[dict]) -> list[dict]:
    if not items:
        return items
    try:
        from .data import fetch_realtime

        realtime = fetch_realtime([item["code"] for item in items])
    except Exception as e:
        logger.debug(f"[universe] 实时行情补充失败: {e}")
        realtime = {}

    enriched = []
    for item in items:
        rt = realtime.get(item["code"], {})
        merged = dict(item)
        for key in ("price", "change_pct", "amount", "fund_size"):
            if rt.get(key):
                merged[key] = rt[key]
        if rt.get("name"):
            merged["name"] = rt["name"]
            merged["category"] = _category(rt["name"])
        if merged.get("price", 0) > 0:
            enriched.append(merged)
    return enriched or items


def _fetch_universe_mootdx() -> list[dict]:
    try:
        from .data import _get_mootdx

        client = _get_mootdx()
        if not client or not hasattr(client, "stocks"):
            return []
        items: list[dict] = []
        seen = set()
        for market in (0, 1):
            df = client.stocks(market=market)
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                code = str(row.get("code", "")).zfill(6)
                name = str(row.get("name") or "")
                if code in seen or "ETF" not in name.upper():
                    continue
                item = _base_item(code, name, source="mootdx")
                if item:
                    items.append(item)
                    seen.add(code)
        return _enrich_with_realtime(items)
    except Exception as e:
        logger.debug(f"[universe] mootdx 列表失败: {e}")
        return []


def _fetch_universe_futu() -> list[dict]:
    try:
        import futu as ft
    except Exception as e:
        logger.debug(f"[universe] futu-api 不可用: {e}")
        return []

    ctx = None
    try:
        ctx = ft.OpenQuoteContext(
            host=CONFIG.get("futu", {}).get("host", "127.0.0.1"),
            port=int(CONFIG.get("futu", {}).get("port", 11111)),
        )
        items: list[dict] = []
        seen = set()
        for market in (getattr(ft.Market, "SH", "SH"), getattr(ft.Market, "SZ", "SZ")):
            ret, data = ctx.get_stock_basicinfo(market, getattr(ft.SecurityType, "ETF", "ETF"))
            if ret != getattr(ft, "RET_OK", 0) or data is None or data.empty:
                continue
            for _, row in data.iterrows():
                code = str(row.get("code", "")).split(".")[-1].zfill(6)[-6:]
                name = row.get("name") or row.get("stock_name") or code
                if code in seen:
                    continue
                item = _base_item(code, name, source="futu")
                if item:
                    items.append(item)
                    seen.add(code)
        return _enrich_with_realtime(items)
    except Exception as e:
        logger.debug(f"[universe] FUTU 列表失败: {e}")
        return []
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


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
                if cached.get("source_order") != _CACHE_SOURCE_ORDER:
                    raise ValueError("缓存数据源顺序已变更，重新拉取")
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

    items = _fetch_universe_mootdx()
    source = "mootdx"
    if not items:
        items = _fetch_universe_futu()
        source = "futu"
    if not items:
        source = "eastmoney"
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
                "source":     "eastmoney",
            })

    try:
        _CACHE.write_text(
            json.dumps({"date": today, "source_order": _CACHE_SOURCE_ORDER, "source": source, "data": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    result = _apply_filter(items, min_amount, max_count)
    logger.info(
        f"[universe] {source}: 全市场 {len(items)} 只 → "
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
