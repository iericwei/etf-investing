"""
数据获取层
  历史日K  : 腾讯财经（主）→ mootdx（备）
  实时行情  : mootdx（主） → 腾讯财经（备）
"""

import re
import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd

from .config import CONFIG, request_headers, tencent_history_url, tencent_realtime_url

logger = logging.getLogger(__name__)

HEADERS = request_headers("tencent")

# ── mootdx 懒加载 ─────────────────────────────────────────────────────

_mootdx_client = None
_mootdx_lock = threading.Lock()


def _get_mootdx():
    global _mootdx_client
    if _mootdx_client is not None:
        return None if _mootdx_client is False else _mootdx_client
    with _mootdx_lock:
        if _mootdx_client is None:
            try:
                from mootdx.quotes import Quotes
                _mootdx_client = Quotes.factory(market="std")
                logger.info("mootdx 初始化成功")
            except Exception as e:
                logger.warning(f"mootdx 初始化失败: {e}")
                _mootdx_client = False
    return None if _mootdx_client is False else _mootdx_client


def detect_market(code: str):
    """返回 (mootdx_market_int, tencent_prefix)"""
    if code.startswith(("5", "6", "9")):
        return 1, "sh"
    return 0, "sz"


# ── 历史日 K ──────────────────────────────────────────────────────────

def _history_tencent(code: str, days: int) -> pd.DataFrame:
    """
    腾讯前复权日 K
    返回列: date, open, close, high, low, volume
    格式: [date, open, close, high, low, volume, amount]
    """
    _, prefix = detect_market(code)
    url = tencent_history_url(prefix, code, days)
    try:
        r = requests.get(url, headers=HEADERS, timeout=CONFIG["network"]["timeouts"]["tencent_history"])
        r.encoding = "utf-8"
        json_str = re.sub(r"^kline_dayhfq\s*=\s*", "", r.text.strip()).rstrip(";")
        obj = json.loads(json_str)
        etf_data = obj["data"][f"{prefix}{code}"]
        # 腾讯 qfq 接口返回 key 为 qfqday；无复权时为 day
        klines = etf_data.get("qfqday") or etf_data.get("day") or []
        rows = [
            {
                "date":   k[0],
                "open":   float(k[1]),
                "close":  float(k[2]),
                "high":   float(k[3]),
                "low":    float(k[4]),
                "volume": float(k[5]),
            }
            for k in klines if len(k) >= 6
        ]
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        logger.debug(f"[tencent_hist] {code}: {e}")
        return pd.DataFrame()


def _history_mootdx(code: str, days: int) -> pd.DataFrame:
    client = _get_mootdx()
    if not client:
        return pd.DataFrame()
    try:
        # frequency=9 → 日K线
        df = client.bars(symbol=code, frequency=9, offset=days)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"vol": "volume"})
        # 兼容不同版本的 index / datetime 列
        if "datetime" in df.columns:
            df["date"] = pd.to_datetime(df["datetime"])
        elif hasattr(df.index, "dtype") and str(df.index.dtype).startswith("datetime"):
            df = df.reset_index()
            df = df.rename(columns={df.columns[0]: "date"})
        else:
            df["date"] = pd.to_datetime(df.index)
            df = df.reset_index(drop=True)
        keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
        return df[keep].sort_values("date").reset_index(drop=True)
    except Exception as e:
        logger.debug(f"[mootdx_hist] {code}: {e}")
        return pd.DataFrame()


def fetch_history(code: str, days: int | None = None) -> pd.DataFrame:
    """获取历史日 K（腾讯优先，失败降级 mootdx），最少需要配置要求条数"""
    days = days or int(CONFIG["selection"]["history_days"])
    df = _history_tencent(code, days)
    if df.empty:
        df = _history_mootdx(code, days)
    return df


def fetch_all_history(pool: list, days: int | None = None, workers: int | None = None) -> Dict[str, pd.DataFrame]:
    """并发获取候选池内所有 ETF 历史数据，过滤不足配置要求条数的"""
    days = days or int(CONFIG["selection"]["history_days"])
    workers = workers or int(CONFIG["selection"]["history_workers"])
    min_rows = int(CONFIG["selection"]["min_history_rows"])
    result: Dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fmap = {ex.submit(fetch_history, item["code"], days): item["code"] for item in pool}
        for future in as_completed(fmap):
            code = fmap[future]
            try:
                df = future.result()
                if not df.empty and len(df) >= min_rows:
                    result[code] = df
            except Exception as e:
                logger.debug(f"[fetch_all] {code}: {e}")
    return result


# ── 15 分钟分时 K（回测成交价）──────────────────────────────────────────

@contextmanager
def _requests_without_environment_proxy():
    """akshare 内部直接调用 requests.get；临时关闭系统/环境代理避免本机代理不可用导致接口失败。"""
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


def fetch_etf_15m_history(code: str, days: int = 35) -> pd.DataFrame:
    """
    通过 akshare fund_etf_hist_min_em 获取 ETF 15 分钟分时行情。

    返回列: datetime, date, time, open, close, high, low, volume, amount。
    接口不可用、akshare 未安装或返回异常时返回空 DataFrame，调用方保持原有日 K 回测逻辑。
    """
    try:
        import akshare as ak
    except Exception as e:
        logger.debug(f"[akshare_15m] akshare 不可用: {e}")
        return pd.DataFrame()

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=max(int(days), 1))
    try:
        with _requests_without_environment_proxy():
            raw = ak.fund_etf_hist_min_em(
                symbol=str(code).zfill(6)[-6:],
                start_date=start_dt.strftime("%Y-%m-%d 09:30:00"),
                end_date=end_dt.strftime("%Y-%m-%d 15:00:00"),
                period="15",
                adjust="",
            )
        if raw is None or raw.empty:
            return pd.DataFrame()

        df = raw.rename(columns={
            "时间": "datetime",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        }).copy()
        required = ["datetime", "open", "close", "high", "low", "volume", "amount"]
        if any(col not in df.columns for col in required):
            return pd.DataFrame()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"])
        if df.empty:
            return pd.DataFrame()
        df["date"] = df["datetime"].dt.normalize()
        df["time"] = df["datetime"].dt.strftime("%H:%M")
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        keep = ["datetime", "date", "time", "open", "close", "high", "low", "volume", "amount"]
        return df[keep].dropna(subset=["close"]).sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        logger.debug(f"[akshare_15m] {code}: {e}")
        return pd.DataFrame()


# ── 实时行情 ───────────────────────────────────────────────────────────

def _realtime_mootdx(codes: List[str]) -> Dict[str, dict]:
    client = _get_mootdx()
    if not client:
        return {}
    try:
        df = client.quotes(symbol=codes)
        if df is None or df.empty:
            return {}
        out = {}
        for _, row in df.iterrows():
            code = str(row.get("code", "")).zfill(6)
            price = float(row.get("price", 0) or 0)
            prev = float(row.get("last_close", 0) or 0)
            out[code] = {
                "price":      price,
                "prev_close": prev,
                "change_pct": (price - prev) / prev * 100 if prev > 0 else 0,
                "volume":     int(row.get("vol", 0) or 0),
                "amount":     float(row.get("amount", 0) or 0),
                "name":       row.get("name", code),
                "source":     "mootdx",
            }
        return out
    except Exception as e:
        logger.debug(f"[mootdx_rt]: {e}")
        return {}


def _realtime_tencent(codes: List[str]) -> Dict[str, dict]:
    if not codes:
        return {}
    qq_codes = [f"{'sh' if detect_market(c)[0] == 1 else 'sz'}{c}" for c in codes]
    url = tencent_realtime_url(qq_codes)
    try:
        r = requests.get(url, headers=HEADERS, timeout=CONFIG["network"]["timeouts"]["tencent_realtime"])
        r.encoding = "gbk"
        out = {}
        for line in r.text.strip().split("\n"):
            m = re.match(r'^v_\w+="([^"]+)"', line.strip().rstrip(";"))
            if not m:
                continue
            f = m.group(1).split("~")
            if len(f) < 40:
                continue
            code = f[2]
            price = float(f[3] or 0)
            prev = float(f[4] or 0)
            out[code] = {
                "price":      price,
                "prev_close": prev,
                "change_pct": float(f[32] or 0),
                "volume":     int(float(f[6] or 0)),
                "amount":     float(f[37] or 0) * 10000,
                "name":       f[1],
                "source":     "tencent",
            }
        return out
    except Exception as e:
        logger.debug(f"[tencent_rt]: {e}")
        return {}


def fetch_realtime(codes: List[str], batch_size: int | None = None) -> Dict[str, dict]:
    """
    获取实时行情（mootdx 优先，未覆盖的降级腾讯）
    大批量时自动分批，避免单次请求过大
    """
    if not codes:
        return {}

    batch_size = batch_size or int(CONFIG["selection"]["realtime_batch_size"])
    result: Dict[str, dict] = {}
    for i in range(0, len(codes), batch_size):
        batch = codes[i: i + batch_size]
        batch_rt = _realtime_mootdx(batch)
        missing = [c for c in batch if c not in batch_rt]
        if missing:
            batch_rt.update(_realtime_tencent(missing))
        result.update(batch_rt)
    return result
