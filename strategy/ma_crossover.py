"""
均线交叉策略：检测金叉（看涨）和死叉（看跌）信号。
"""

import numpy as np

from strategy.base import BaseStrategy


class MACrossoverStrategy(BaseStrategy):
    """检测移动平均线交叉。

    金叉（看涨）: 快线上穿慢线。
    死叉（看跌）: 快线下穿慢线。

    参数:
        fast_ma: int = 5            — 快线周期
        slow_ma: int = 20           — 慢线周期
        confirmation_days: int = 1  — 交叉需持续确认的天数
        direction: str = "golden"   — "golden"（金叉）/ "death"（死叉）/ "both"（两者）
        require_volume: bool = False — 是否需要放量确认
        vol_multiplier: float = 1.2 — 放量确认倍数
    """

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.fast_ma = self.params.get("fast_ma", 5)
        self.slow_ma = self.params.get("slow_ma", 20)
        self.confirmation_days = self.params.get("confirmation_days", 1)
        self.direction = self.params.get("direction", "golden")
        self.require_volume = self.params.get("require_volume", False)
        self.vol_multiplier = self.params.get("vol_multiplier", 1.2)

    @property
    def required_days(self) -> int:
        return self.slow_ma + self.confirmation_days + 2

    def check(self, stock_code: str) -> dict | None:
        """检测单只股票的均线交叉信号。"""
        df = self.get_stock_data(stock_code)
        if df is None:
            return None

        closes = df["收盘"].to_numpy(dtype=np.float64)
        volumes = df["成交量"].to_numpy(dtype=np.float64)

        # 计算快慢均线
        fast_ma_vals = self._compute_ma(closes, self.fast_ma)
        slow_ma_vals = self._compute_ma(closes, self.slow_ma)

        # diff > 0 表示快线在慢线上方
        diff = fast_ma_vals - slow_ma_vals
        valid = ~np.isnan(diff)

        if np.sum(valid) < 2:
            return None

        # 检查最近几天是否存在交叉
        check_len = self.confirmation_days + 2
        recent_diff = diff[-check_len:]

        # 通过符号变化检测交叉
        sign_before = np.sign(recent_diff[-2])
        sign_now = np.sign(recent_diff[-1])

        is_golden = sign_before <= 0 and sign_now > 0
        is_death = sign_before >= 0 and sign_now < 0

        crossed = False
        cross_type = ""
        if self.direction in ("golden", "both") and is_golden:
            crossed = True
            cross_type = "golden"
        elif self.direction in ("death", "both") and is_death:
            crossed = True
            cross_type = "death"

        if not crossed:
            return {
                "surged": False,
                "fast_ma": round(float(fast_ma_vals[-1]), 2),
                "slow_ma": round(float(slow_ma_vals[-1]), 2),
                "diff": round(float(diff[-1]), 2),
                "cross_type": None,
                "close": round(float(closes[-1]), 2),
                "date": str(df.iloc[-1].get("date", "")),
            }

        # 可选放量确认
        vol_confirmed = True
        vol_ratio = 1.0
        if self.require_volume:
            avg_vol = np.mean(volumes[-self.fast_ma:])
            if avg_vol > 0:
                vol_ratio = volumes[-1] / avg_vol
                vol_confirmed = vol_ratio >= self.vol_multiplier
            else:
                vol_confirmed = False

        surged = crossed and vol_confirmed

        return {
            "surged": surged,
            "cross_type": cross_type,
            "fast_ma": round(float(fast_ma_vals[-1]), 2),
            "slow_ma": round(float(slow_ma_vals[-1]), 2),
            "diff": round(float(diff[-1]), 2),
            "close": round(float(closes[-1]), 2),
            "volume": int(volumes[-1]),
            "vol_ratio": round(float(vol_ratio), 2),
            "vol_confirmed": vol_confirmed,
            "date": str(df.iloc[-1].get("date", "")),
        }
