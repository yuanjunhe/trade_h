"""
分级数据获取：akshare（东方财富）→ baostock → 新浪。
所有函数返回的 DataFrame 列名与 stock_daily 表结构匹配。
"""

import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

from utils.helpers import (
    call_with_timeout,
    retry_with_backoff,
    to_sina_code,
)

logger = logging.getLogger("quant.fetcher")

# Baostock 不是线程安全的，所有 bs 调用必须通过此锁串行化
_bs_lock = threading.Lock()


def _fmt_bs_date(ak_date: str) -> str:
    """将 akshare 格式的日期 "YYYYMMDD" 转为 baostock 格式 "YYYY-MM-DD"。"""
    if len(ak_date) == 8:
        return f"{ak_date[:4]}-{ak_date[4:6]}-{ak_date[6:8]}"
    return ak_date  # 已经是 YYYY-MM-DD 格式


# ── 主数据源：akshare（东方财富） ─────────────────


def fetch_akshare_full(code: str, adjust: str = "qfq",
                       timeout: int = 30,
                       start_date: str = "19900101") -> pd.DataFrame | None:
    """通过 akshare 获取一只股票的历史数据。

    参数:
        code: 股票代码，如 "300750"。
        adjust: 复权方式，"qfq"（前复权）/ "hfq"（后复权）/ ""（不复权）。
        timeout: 超时秒数。
        start_date: 起始日期 "YYYYMMDD"，默认 19900101。

    返回:
        包含历史日线的 DataFrame，失败返回 None。
    """
    try:
        import akshare as ak

        def _do():
            return ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=datetime.now().strftime("%Y%m%d"),
                adjust=adjust,
            )

        df = call_with_timeout(_do, timeout)
        if df is None or df.empty:
            return None

        # 标准化列名
        df = df.rename(columns={
            "日期": "日期",
            "开盘": "开盘",
            "收盘": "收盘",
            "最高": "最高",
            "最低": "最低",
            "成交量": "成交量",
            "成交额": "成交额",
            "振幅": "振幅",
            "涨跌幅": "涨跌幅",
            "涨跌额": "涨跌额",
            "换手率": "换手率",
        })

        # 确保数值列类型正确
        for col in ["开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "换手率"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        return df
    except Exception as e:
        logger.debug(f"akshare 全量获取失败 {code}: {e}")
        return None


def fetch_akshare_incremental(code: str, from_date: str, to_date: str,
                              timeout: int = 15) -> pd.DataFrame | None:
    """通过 akshare 获取一只股票的增量数据（小日期范围，速度快）。

    参数:
        code: 股票代码。
        from_date: 起始日期 "YYYY-MM-DD"。
        to_date: 结束日期 "YYYY-MM-DD"。
        timeout: 超时秒数。

    返回:
        DataFrame 或 None。
    """
    try:
        import akshare as ak

        def _do():
            return ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=from_date.replace("-", ""),
                end_date=to_date.replace("-", ""),
                adjust="qfq",
            )

        df = call_with_timeout(_do, timeout)
        if df is None or df.empty:
            return None

        for col in ["开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "换手率"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        return df
    except Exception as e:
        logger.debug(f"akshare 增量获取失败 {code}: {e}")
        return None


# ── 备选数据源：Baostock ────────────────────────


def fetch_baostock_full(code: str, timeout: int = 15,
                        start_date: str = "1990-01-01") -> pd.DataFrame | None:
    """通过 Baostock 获取一只股票的历史数据（TCP 直连，无 HTTP 开销）。

    参数:
        code: 股票代码。
        timeout: 超时秒数。
        start_date: 起始日期 "YYYY-MM-DD"，默认 1990-01-01。

    返回:
        DataFrame，列名包括：日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 换手率。
        成交量已从股转换为手（手 = 100 股）。
    """
    try:
        import baostock as bs
        import threading as _threading

        prefix = "sh" if code.startswith("6") else "sz"
        bs_code = f"{prefix}.{code}"
        today = datetime.now().strftime("%Y-%m-%d")

        result = [None]
        error = [None]
        logged_in = [False]

        def _do():
            try:
                with _bs_lock:
                    lr = bs.login()
                if lr.error_code != "0":
                    error[0] = Exception(f"Baostock 登录失败: {lr.error_msg}")
                    return
                logged_in[0] = True

                with _bs_lock:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount,turn,amplitude,pctChg",
                        start_date=start_date,
                        end_date=today,
                        frequency="d",
                        adjustflag="2",  # 前复权
                    )
                if rs.error_code != "0":
                    error[0] = Exception(f"Baostock 查询失败: {rs.error_msg}")
                    return

                rows = []
                while rs.next():
                    row = rs.get_row_data()
                    if row[0] is None or row[0] == "":
                        continue
                    rows.append({
                        "日期": row[0],
                        "开盘": float(row[1]) if row[1] else 0.0,
                        "最高": float(row[2]) if row[2] else 0.0,
                        "最低": float(row[3]) if row[3] else 0.0,
                        "收盘": float(row[4]) if row[4] else 0.0,
                        "成交量": int(float(row[5])) // 100 if row[5] else 0,  # 股→手
                        "成交额": float(row[6]) if row[6] else 0.0,
                        "换手率": float(row[7]) if row[7] else 0.0,
                        "振幅": float(row[8]) if row[8] else 0.0,
                        "涨跌幅": float(row[9]) if row[9] else 0.0,
                    })
                if rows:
                    result[0] = pd.DataFrame(rows)
            except Exception as e:
                error[0] = e
            finally:
                if logged_in[0]:
                    bs.logout()

        t = _threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            logger.debug(f"Baostock 获取超时 {code}")
            return None
        if error[0]:
            raise error[0]
        return result[0]
    except Exception as e:
        logger.debug(f"Baostock 获取失败 {code}: {e}")
        return None


# ── 兜底数据源：新浪 ────────────────────────────


def fetch_sina_full(code: str, datalen: int = 2000,
                    timeout: int = 15) -> pd.DataFrame | None:
    """从新浪财经 API 获取日线数据。

    新浪 API 不支持日期范围查询，固定返回最近 N 条 K 线。

    参数:
        code: 股票代码。
        datalen: 最多返回多少条 K 线（新浪上限不详，≈2000 安全）。
        timeout: 超时秒数。

    返回:
        DataFrame，列名包括：日期, 开盘, 收盘, 最高, 最低, 成交量。
    """
    try:
        import requests

        sina_code = to_sina_code(code)
        url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": sina_code, "scale": "240", "ma": "no", "datalen": datalen}
        headers = {
            "Referer": "http://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }

        def _do():
            r = requests.get(url, params=params, headers=headers, timeout=10)
            r.encoding = "gbk"
            return r.json()

        data = call_with_timeout(_do, timeout)
        if not data or not isinstance(data, list):
            return None

        rows = []
        for item in data:
            rows.append({
                "日期": item.get("day", ""),
                "开盘": float(item.get("open", 0) or 0),
                "最高": float(item.get("high", 0) or 0),
                "最低": float(item.get("low", 0) or 0),
                "收盘": float(item.get("close", 0) or 0),
                "成交量": int(float(item.get("volume", 0) or 0)) // 100,  # 股→手
                "成交额": 0.0,  # 新浪日线 API 不提供成交额
                "换手率": 0.0,
                "振幅": 0.0,
                "涨跌幅": 0.0,
            })

        if rows:
            return pd.DataFrame(rows)
        return None
    except Exception as e:
        logger.debug(f"新浪获取失败 {code}: {e}")
        return None


# ── 组合获取器 ──────────────────────────────────


def fetch_stock_history(code: str, timeout: int = 30,
                        primary: str = "akshare",
                        start_date: str = "19900101") -> pd.DataFrame | None:
    """获取单只股票的历史数据，自动降级。

    优先级: primary → baostock → sina。

    参数:
        code: 股票代码。
        timeout: 每个数据源的超时秒数。
        primary: 主数据源，"akshare"（默认）或 "baostock"。
        start_date: 起始日期 "YYYYMMDD"（akshare格式），默认 19900101。

    返回:
        DataFrame，全部数据源失败则返回 None。
    """
    sources = []
    if primary == "akshare":
        sources = [
            ("akshare", lambda: fetch_akshare_full(code, timeout=timeout, start_date=start_date)),
            ("baostock", lambda: fetch_baostock_full(code, timeout=timeout, start_date=_fmt_bs_date(start_date))),
            ("sina", lambda: fetch_sina_full(code, timeout=timeout)),
        ]
    else:
        sources = [
            ("baostock", lambda: fetch_baostock_full(code, timeout=timeout, start_date=_fmt_bs_date(start_date))),
            ("akshare", lambda: fetch_akshare_full(code, timeout=timeout, start_date=start_date)),
            ("sina", lambda: fetch_sina_full(code, timeout=timeout)),
        ]

    for name, fn in sources:
        try:
            df = fn()
            if df is not None and not df.empty:
                logger.debug(f"{code}: 从 {name} 获取 {len(df)} 行")
                return df
        except Exception as e:
            logger.debug(f"{code}: {name} 失败: {e}")
            continue

    return None


def fetch_stock_incremental(code: str, from_date: str, to_date: str,
                            timeout: int = 15) -> pd.DataFrame | None:
    """获取单只股票的增量数据（快速通道）。

    优先使用 akshare（东方财富 HTTP，小日期范围很快），失败降级到新浪。

    参数:
        code: 股票代码。
        from_date: 起始日期 "YYYY-MM-DD"。
        to_date: 结束日期 "YYYY-MM-DD"。
        timeout: 每个数据源的超时秒数。

    返回:
        DataFrame 或 None。
    """
    # 快速通道：akshare 增量
    try:
        df = fetch_akshare_incremental(code, from_date, to_date, timeout=timeout)
        if df is not None and not df.empty:
            logger.debug(f"{code}: 增量 {from_date}~{to_date}: {len(df)} 行")
            return df
    except Exception:
        pass

    # 兜底：新浪（全量获取后按日期过滤）
    try:
        delta = (datetime.strptime(to_date, "%Y-%m-%d") -
                 datetime.strptime(from_date, "%Y-%m-%d")).days
        df = fetch_sina_full(code, datalen=max(delta + 10, 30), timeout=timeout)
        if df is not None and not df.empty:
            df = df[(df["日期"] >= from_date) & (df["日期"] <= to_date)]
            if not df.empty:
                return df
    except Exception:
        pass

    return None
