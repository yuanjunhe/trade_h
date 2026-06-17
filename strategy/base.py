"""
选股策略抽象基类，所有策略都继承此类。
"""

from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import pandas as pd


class BaseStrategy(ABC):
    """所有选股策略的抽象基类。

    生命周期:
        1. __init__(params)       — 接收策略参数
        2. load_data(conn, codes) — 从数据库批量加载所需数据到内存
        3. check(stock_code)      — 评估单只股票，返回信号字典或 None
        4. scan(conn, stocks)     — 扫描全部股票，返回命中列表

    子类必须实现:
        - required_days（int 属性）：最小所需交易日数
        - check(stock_code) -> dict | None：单股检测逻辑
    """

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self._data: dict[str, pd.DataFrame] = {}  # code -> DataFrame 缓存
        self._conn = None

    @property
    def name(self) -> str:
        """策略名称，从类名派生。"""
        return self.__class__.__name__

    @property
    @abstractmethod
    def required_days(self) -> int:
        """该策略最少需要多少个交易日的数据。"""
        ...

    def load_data(self, conn, codes: list[str],
                  columns: list[str] | None = None) -> None:
        """从 stock_daily 批量加载所需数据到内存。

        数据存入 self._data，格式为 {code: DataFrame}。
        每个 DataFrame 按日期升序排列。

        参数:
            conn: 数据库连接。
            codes: 股票代码列表。
            columns: 要加载的列。默认: date, adj_open, adj_high,
                     adj_low, adj_close, volume, amount, turnover。
        """
        if columns is None:
            columns = ["date", "adj_open", "adj_high", "adj_low", "adj_close",
                       "open", "high", "low", "close", "volume", "amount", "turnover"]

        self._conn = conn
        self._data.clear()

        if not codes:
            return

        # 分批加载，每批 500 只，避免 SQL IN 子句过大
        chunk_size = 500
        cols_str = ", ".join(columns)

        for i in range(0, len(codes), chunk_size):
            chunk = codes[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            sql = f"""
                SELECT code, {cols_str}
                FROM stock_daily
                WHERE code IN ({placeholders})
                ORDER BY code, date
            """
            rows = conn.execute(sql, chunk).fetchall()

            # 按 code 分组
            for row in rows:
                d = dict(row)
                code = d.pop("code")
                if code not in self._data:
                    self._data[code] = []
                self._data[code].append(d)

        # 列表转 DataFrame，统一列名方便策略使用
        for code in list(self._data.keys()):
            self._data[code] = pd.DataFrame(self._data[code])
            df = self._data[code]
            if "adj_close" in df.columns:
                df["收盘"] = df["adj_close"]
            if "volume" in df.columns:
                df["成交量"] = df["volume"]

    def get_stock_data(self, code: str) -> pd.DataFrame | None:
        """获取某只股票已加载的 DataFrame。

        数据不足 required_days 时返回 None。
        """
        df = self._data.get(code)
        if df is None or len(df) < self.required_days:
            return None
        return df

    @abstractmethod
    def check(self, stock_code: str) -> dict | None:
        """评估单只股票。由 scan() 调用。

        参数:
            stock_code: 如 "300750"

        返回:
            字典，至少包含 {'surged': True/False, ...策略字段...}
            数据不足时返回 None。
        """
        ...

    def scan(self, conn, stocks: list[dict]) -> list[dict]:
        """完整扫描：加载数据 → 逐只检测 → 返回命中列表。

        参数:
            conn: 数据库连接。
            stocks: 字典列表，含 'code' 和 'name'。

        返回:
            结果字典列表（仅包含触发的股票）。
        """
        codes = [s["code"] for s in stocks]
        self.load_data(conn, codes)

        results = []
        for s in stocks:
            try:
                result = self.check(s["code"])
                if result and result.get("surged"):
                    result["code"] = s["code"]
                    result["name"] = s["name"]
                    result["strategy"] = self.name
                    results.append(result)
            except Exception:
                # 单只股票出错不影响整体扫描
                pass

        return results

    def scan_with_data(self, stocks: list[dict]) -> list[dict]:
        """使用已加载的数据扫描（需先调用 load_data）。

        适合多个策略共享同一份数据时使用。
        """
        results = []
        for s in stocks:
            try:
                result = self.check(s["code"])
                if result and result.get("surged"):
                    result["code"] = s["code"]
                    result["name"] = s["name"]
                    result["strategy"] = self.name
                    results.append(result)
            except Exception:
                pass
        return results

    # ── 技术指标计算辅助方法 ─────────────────────

    def _compute_ma(self, closes: np.ndarray, period: int) -> np.ndarray:
        """计算简单移动平均线 (SMA)。

        返回与输入同长度的数组，前 period-1 个元素为 NaN。
        """
        if len(closes) < period:
            return np.full_like(closes, np.nan)
        ma = np.full_like(closes, np.nan, dtype=np.float64)
        cumsum = np.cumsum(np.insert(closes, 0, 0))
        ma[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
        return ma

    def _compute_ema(self, closes: np.ndarray, period: int) -> np.ndarray:
        """计算指数移动平均线 (EMA)。"""
        if len(closes) < period:
            return np.full_like(closes, np.nan)
        alpha = 2.0 / (period + 1)
        ema = np.full_like(closes, np.nan, dtype=np.float64)
        ema[period - 1] = np.mean(closes[:period])
        for i in range(period, len(closes)):
            ema[i] = alpha * closes[i] + (1 - alpha) * ema[i - 1]
        return ema

    def _compute_roc(self, closes: np.ndarray, period: int) -> np.ndarray:
        """计算变化率 (ROC): (close[t] - close[t-period]) / close[t-period]。"""
        if len(closes) < period + 1:
            return np.full_like(closes, np.nan)
        roc = np.full_like(closes, np.nan, dtype=np.float64)
        roc[period:] = (closes[period:] - closes[:-period]) / closes[:-period]
        return roc
