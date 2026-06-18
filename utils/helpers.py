"""
工具函数：股票代码转换、超时包装、格式化。
"""

import threading
import time
from datetime import datetime
from typing import Any, Callable


# ── 股票代码转换 ───────────────────────────


def to_sina_code(stock_code: str) -> str:
    """将纯数字代码转为新浪格式（sh600519 / sz300750）。"""
    return f"sh{stock_code}" if stock_code.startswith("6") else f"sz{stock_code}"


def to_eastmoney_code(stock_code: str) -> str:
    """将纯数字代码转为东方财富格式（1.600519 / 0.300750）。

    东方财富使用市场前缀：1=上海, 0=深圳。
    """
    return f"1.{stock_code}" if stock_code.startswith("6") else f"0.{stock_code}"


def get_market(stock_code: str) -> str:
    """根据股票代码前缀判断所属市场。

    返回:
        "SH"（6开头）或 "SZ"（0/3开头）。
    """
    return "SH" if stock_code.startswith("6") else "SZ"


def get_board(stock_code: str) -> str:
    """根据股票代码前缀判断所属板块。

    返回:
        "主板", "创业板", "科创板", "北交所", 或 None。
    """
    if stock_code.startswith(("600", "601", "603", "605")):
        return "主板"
    if stock_code.startswith(("688", "689")):
        return "科创板"
    if stock_code.startswith(("000", "001", "002", "003")):
        return "主板"
    if stock_code.startswith("30"):
        return "创业板"
    if stock_code.startswith(("8", "4")):
        return "北交所"
    return None


# ── 超时包装 ──────────────────────────────────


def call_with_timeout(fn: Callable, timeout: float, *args: Any, **kwargs: Any) -> Any | None:
    """在守护线程中执行 fn，超时返回 None。

    用于保护没有内置超时参数的第三方 HTTP 调用。

    参数:
        fn: 要执行的可调用对象。
        timeout: 最长等待秒数。
        *args, **kwargs: 透传给 fn。

    返回:
        fn 的返回值，超时则返回 None。

    抛出:
        fn 抛出的任何异常（在调用线程中重新抛出）。
    """
    result: list[Any] = [None]
    error: list[Exception | None] = [None]

    def _do() -> None:
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return None
    if error[0] is not None:
        raise error[0]
    return result[0]


def retry_with_backoff(
    fn: Callable,
    max_retries: int = 3,
    base_delay: float = 2.0,
    timeout: float = 15.0,
    *args: Any,
    **kwargs: Any,
) -> Any | None:
    """带指数退避的重试执行。

    延迟序列: base_delay * 2^0, base_delay * 2^1, base_delay * 2^2, ...

    参数:
        fn: 要执行的可调用对象。
        max_retries: 最大重试次数（总调用次数 = 1 + max_retries）。
        base_delay: 初始退避延迟秒数。
        timeout: 每次尝试的超时秒数。
        *args, **kwargs: 透传给 fn。

    返回:
        fn 的返回值，全部失败则返回 None。
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            result = call_with_timeout(fn, timeout, *args, **kwargs)
            if result is not None:
                return result
            last_error = TimeoutError(f"调用超时 ({timeout}s)")
        except Exception as e:
            last_error = e

        if attempt < max_retries:
            delay = base_delay * (2 ** attempt)
            time.sleep(delay)

    if last_error:
        raise last_error
    return None


# ── 格式化函数 ───────────────────────────────


def format_volume(vol: float) -> str:
    """将成交量（手）格式化为可读字符串。

    示例:
        1234567 -> "123.46 万手"
        1234567890 -> "12.35 亿手"
    """
    if vol >= 1_0000_0000:  # 亿
        return f"{vol / 1_0000_0000:.2f} 亿手"
    if vol >= 1_0000:  # 万
        return f"{vol / 1_0000:.2f} 万手"
    return f"{vol:.0f} 手"


def format_amount(amt: float) -> str:
    """将成交额（元）格式化为可读字符串。

    示例:
        50000000 -> "5000.00 万元"
        500000000 -> "5.00 亿元"
    """
    if abs(amt) >= 1_0000_0000:
        return f"{amt / 1_0000_0000:.2f} 亿元"
    if abs(amt) >= 1_0000:
        return f"{amt / 1_0000:.2f} 万元"
    return f"{amt:.2f} 元"


def format_pct(pct: float) -> str:
    """格式化涨跌幅，带正负号。

    示例:
        3.5 -> "+3.50%"
        -2.1 -> "-2.10%"
    """
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.2f}%"


def is_trading_day(dt: datetime | None = None) -> bool:
    """判断给定日期是否为交易日（周一至周五）。

    注意: 这是简单的星期几判断，不考虑中国节假日。
    精确的节假日判断需使用 akshare 交易日历。
    """
    if dt is None:
        dt = datetime.now()
    return dt.weekday() < 5


def last_trading_day(dt: datetime | None = None) -> str:
    """获取最近一个交易日的日期字符串。

    如果今天是工作日且时间晚于 15:00，返回今天；
    否则返回前一个工作日。

    返回:
        日期字符串，"YYYY-MM-DD" 格式。
    """
    if dt is None:
        dt = datetime.now()
    result = dt
    if dt.weekday() >= 5:
        # 周末：回退到周五
        result = dt.replace(day=dt.day - (dt.weekday() - 4))
    elif dt.hour < 15:
        # 收盘前：使用前一天
        result = dt.replace(day=dt.day - 1)
    if result.weekday() >= 5:
        result = result.replace(day=result.day - (result.weekday() - 4))
    return result.strftime("%Y-%m-%d")


def filter_incomplete_today(df, exclude: bool = True):
    """盘中过滤当天不完整的数据。

    在交易日 15:00 之前，当天的成交量仍在累积中，不代表全天真实成交。
    此函数在盘中删除当天行。

    参数:
        df: 含"日期"列的 DataFrame。
        exclude: 为 True 时，工作日 15:00 前过滤当天数据。

    返回:
        过滤后的 DataFrame。
    """
    import pandas as pd

    if not exclude or df is None or df.empty:
        return df
    now = datetime.now()
    if now.weekday() < 5 and now.hour < 15:
        today_str = now.strftime("%Y-%m-%d")
        df = df[df["日期"] != today_str]
    return df


def get_recent_field(hit: dict, field: str, default=None):
    """从命中记录中提取第一个策略最近一天的字段值。

    遍历 hit["strategies"]，找到第一个包含非空 recent 数组的策略，
    返回 recent[-1][field]。

    参数:
        hit: 扫描命中记录字典。
        field: 要提取的字段名，如 "date", "volume", "ratio"。
        default: 未找到时的默认值。

    返回:
        字段值，或 default。
    """
    for strat_key in hit.get("strategies", []):
        data = hit.get(strat_key, {})
        recent = data.get("recent", [])
        if recent and field in recent[-1]:
            return recent[-1][field]
    return default
