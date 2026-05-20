"""
数据获取层
  历史日K  : 腾讯财经（主）→ mootdx（备）
  实时行情  : mootdx（主） → 腾讯财经（备）
"""

import re
import json
import logging
import random
import shlex
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
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


# ── 东方财富历史分时 K（回测成交价）──────────────────────────────────────

_VALID_INTRADAY_PERIODS = {"1", "5", "15", "30", "60"}
_USER_AGENTS_FILE = Path(__file__).with_name("user_agents.txt")
_BROWSER_HEADERS_FILE = Path(__file__).with_name("headers.txt")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EASTMONEY_CURL_LOG_FILE = _PROJECT_ROOT / "logs" / "eastmoney_intraday_curl.log"


@lru_cache(maxsize=1)
def _load_user_agents() -> list[str]:
    try:
        lines = _USER_AGENTS_FILE.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        logger.debug(f"[eastmoney_intraday] user-agent 文件读取失败: {e}")
        return [CONFIG["headers"]["user_agent"]]
    user_agents = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return user_agents or [CONFIG["headers"]["user_agent"]]


def _parse_curl_headers_file(path: Path) -> dict[str, str]:
    """解析浏览器复制出来的 curl header 文件（-H / -b 格式）。"""
    text = path.read_text(encoding="utf-8")
    headers = {name.strip(): value.strip() for name, value in re.findall(r"-H '([^:]+): (.*?)'", text)}
    cookie = re.search(r"-b '([^']+)'", text)
    if cookie:
        headers["Cookie"] = cookie.group(1).strip()
    return headers


def _eastmoney_cookie() -> str:
    """生成和东方财富页面相似的统计 Cookie，降低接口被当作脚本请求的概率。"""
    now = datetime.now()
    ymd_hms = now.strftime("%Y%m%d%H%M%S")
    rand14 = lambda: random.randint(10**13, 10**14 - 1)
    rand32 = lambda: f"{random.getrandbits(128):032x}"
    return "; ".join([
        f"qgqp_b_id={rand32()}",
        f"st_si={rand14()}",
        "st_asi=delete",
        f"st_pvi={rand14()}",
        f"st_sp={now.strftime('%Y-%m-%d')}%20{now.strftime('%H%%3A%M%%3A%S')}",
        "st_inirUrl=https%3A%2F%2Fwww.google.com.hk%2F",
        f"st_sn={random.randint(1, 9)}",
        f"st_psi={ymd_hms}{random.randint(100, 999)}-113200301327-{random.randint(10**9, 10**10 - 1)}",
    ])


def _chrome_header_profile(user_agent: str, platform: str, sec_ch_ua: str | None = None) -> dict[str, str]:
    if sec_ch_ua is None:
        sec_ch_ua = '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"'
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,zh-TW;q=0.6",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": random.choice(["max-age=0", "no-cache"]),
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": random.choice(["none", "same-site"]),
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": user_agent,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
        "Cookie": _eastmoney_cookie(),
    }


@lru_cache(maxsize=1)
def _load_eastmoney_header_profiles() -> list[dict[str, str]]:
    """加载一批完整浏览器指纹 header；优先包含 headers.txt 里实测成功的完整 header。"""
    profiles: list[dict[str, str]] = []
    if _BROWSER_HEADERS_FILE.exists():
        try:
            parsed = _parse_curl_headers_file(_BROWSER_HEADERS_FILE)
            if parsed.get("User-Agent"):
                profiles.append(parsed)
        except Exception as e:
            logger.debug(f"[eastmoney_intraday] headers.txt 读取失败: {e}")

    profiles.extend([
        _chrome_header_profile(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "macOS",
        ),
        _chrome_header_profile(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Windows",
        ),
        _chrome_header_profile(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Linux",
        ),
        _chrome_header_profile(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
            "Windows",
            '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
        ),
    ])
    return profiles


def _eastmoney_intraday_header_candidates() -> list[dict[str, str]]:
    profiles = list(_load_eastmoney_header_profiles())
    if len(profiles) <= 1:
        ordered = profiles
    else:
        # headers.txt 里的实测成功指纹优先尝试，其余相似指纹随机轮换。
        ordered = [profiles[0], *random.sample(profiles[1:], k=len(profiles) - 1)]
    return [_eastmoney_intraday_headers(profile) for profile in ordered]


def _eastmoney_intraday_headers(profile: dict[str, str] | None = None) -> dict[str, str]:
    headers = request_headers("eastmoney")
    if profile is None:
        profile = random.choice(_load_eastmoney_header_profiles())
    headers.update(dict(profile))
    return headers


def _build_curl_command(method: str, prepared_url: str, headers: dict[str, str]) -> str:
    parts = ["curl", "--noproxy", "*", "-L", "--max-time", "20", "-X", method.upper()]
    for name, value in headers.items():
        parts.extend(["-H", f"{name}: {value}"])
    parts.append(prepared_url)
    return " ".join(shlex.quote(str(part)) for part in parts)


def _log_eastmoney_intraday_curl(
    prepared_url: str,
    headers: dict[str, str],
    *,
    code: str,
    period: str,
    days: int,
    profile_index: int,
    attempt: int,
    result: str = "pending",
) -> None:
    """把每次东方财富分时测试请求写成可复制执行的完整 curl，方便手工复现。"""
    try:
        _EASTMONEY_CURL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        curl = _build_curl_command("GET", prepared_url, headers)
        line = (
            f"\n# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
            f"code={code} period={period} days={days} profile={profile_index} attempt={attempt} result={result}\n"
            f"{curl}\n"
        )
        with _EASTMONEY_CURL_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.debug(f"[eastmoney_intraday] curl 日志写入失败: {e}")


def _eastmoney_direct_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int | float) -> requests.Response:
    """直接请求东方财富，明确关闭 requests 对环境代理的继承。"""
    session = requests.Session()
    session.trust_env = False
    try:
        return session.get(url, params=params, headers=headers, timeout=timeout)
    finally:
        session.close()


def _prepare_request_url(url: str, params: dict[str, str], headers: dict[str, str]) -> str:
    request = requests.Request("GET", url, params=params, headers=headers)
    return request.prepare().url or url


def _eastmoney_secid(code: str) -> str:
    code = str(code).strip().zfill(6)[-6:]
    _, prefix = detect_market(code)
    market = "1" if prefix == "sh" else "0"
    return f"{market}.{code}"


def fetch_eastmoney_intraday_history(code: str, period: str = "15", days: int = 35) -> pd.DataFrame:
    """
    直接调用东方财富 push2his 历史分时接口获取 ETF 分钟 K 线。

    返回列: datetime, date, time, open, close, high, low, volume, amount。
    接口不可用、参数异常或返回异常时返回空 DataFrame。
    """
    code = str(code).strip().zfill(6)[-6:]
    period = str(period)
    if period not in _VALID_INTRADAY_PERIODS:
        logger.debug(f"[eastmoney_intraday] unsupported period={period}")
        return pd.DataFrame()

    days = max(int(days), 1)
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    params = {
        "secid": _eastmoney_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": period,
        "fqt": "1",
        "beg": start_dt.strftime("%Y%m%d"),
        "end": end_dt.strftime("%Y%m%d"),
        "lmt": "100000",
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    try:
        timeout = CONFIG["network"]["timeouts"].get("eastmoney_intraday", 8)
        last_error: Exception | None = None
        payload = None
        klines = []
        for profile_index, headers in enumerate(_eastmoney_intraday_header_candidates(), start=1):
            prepared_url = _prepare_request_url(url, params, headers)
            for attempt in range(1, 4):
                _log_eastmoney_intraday_curl(
                    prepared_url,
                    headers,
                    code=code,
                    period=period,
                    days=days,
                    profile_index=profile_index,
                    attempt=attempt,
                )
                try:
                    response = _eastmoney_direct_get(url, params, headers, timeout)
                    response.raise_for_status()
                    payload = response.json()
                    klines = ((payload or {}).get("data") or {}).get("klines") or []
                    _log_eastmoney_intraday_curl(
                        prepared_url,
                        headers,
                        code=code,
                        period=period,
                        days=days,
                        profile_index=profile_index,
                        attempt=attempt,
                        result=f"http={response.status_code} klines={len(klines)}",
                    )
                    if klines:
                        break
                except Exception as e:
                    last_error = e
                    _log_eastmoney_intraday_curl(
                        prepared_url,
                        headers,
                        code=code,
                        period=period,
                        days=days,
                        profile_index=profile_index,
                        attempt=attempt,
                        result=f"error={type(e).__name__}: {e}",
                    )
            if klines:
                break
        if not klines and last_error is not None:
            logger.debug(f"[eastmoney_intraday] {code}: 所有浏览器 header 均失败或无数据，最后错误: {last_error}")
        rows = []
        for item in klines:
            parts = str(item).split(",")
            if len(parts) < 7:
                continue
            rows.append({
                "datetime": parts[0],
                "open": parts[1],
                "close": parts[2],
                "high": parts[3],
                "low": parts[4],
                "volume": parts[5],
                "amount": parts[6],
            })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
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
        logger.debug(f"[eastmoney_intraday] {code}: {e}")
        return pd.DataFrame()


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
    通过东方财富 push2his 接口获取 ETF 15 分钟分时行情。

    返回列: datetime, date, time, open, close, high, low, volume, amount。
    东方财富接口不可用时返回空 DataFrame，调用方保持原有日 K 回测逻辑。
    """
    return fetch_eastmoney_intraday_history(code, period="15", days=days)


def fetch_etf_15m_history_akshare(code: str, days: int = 35) -> pd.DataFrame:
    """
    通过 akshare fund_etf_hist_min_em 获取 ETF 15 分钟分时行情（保留为备用实现）。

    返回列: datetime, date, time, open, close, high, low, volume, amount。
    接口不可用、akshare 未安装或返回异常时返回空 DataFrame。
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
