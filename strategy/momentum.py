"""
动量策略：基于价格变化率 + 成交量确认的趋势跟踪。
"""

import numpy as np

from strategy.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    """动量/趋势跟踪策略。

    - 计算 N 日价格变化率 (ROC)。
    - ROC 超过阈值 → 动量信号。
    - 可选成交量放大确认。

    参数:
        period: int = 20              — ROC 计算周期
        roc_threshold: float = 0.05   — 最小绝对变化率（5%）
        direction: str = "up"         — "up" / "down" / "both"
        require_volume: bool = True   — 是否需要成交量确认
        volume_multiplier: float = 1.2
    """

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.period = self.params.get("period", 20)
        self.roc_threshold = self.params.get("roc_threshold", 0.05)
        self.direction = self.params.get("direction", "up")
        self.require_volume = self.params.get("require_volume", True)
        self.volume_multiplier = self.params.get("volume_multiplier", 1.2)

    @property
    def required_days(self) -> int:
        return self.period + 2

    def check(self, stock_code: str) -> dict | None:
        """检测单只股票的动量信号。"""
        df = self.get_stock_data(stock_code)
        if df is None:
            return None

        closes = df["收盘"].to_numpy(dtype=np.float64)
        volumes = df["成交量"].to_numpy(dtype=np.float64)

        if len(closes) < self.period + 1:
            return None

        # 计算变化率
        roc = (closes[-1] - closes[-self.period - 1]) / closes[-self.period - 1]

        # 方向过滤
        if self.direction == "up" and roc <= 0:
            return {"surged": False, "roc": round(float(roc), 4),
                    "close": round(float(closes[-1]), 2),
                    "date": str(df.iloc[-1].get("date", ""))}
        if self.direction == "down" and roc >= 0:
            return {"surged": False, "roc": round(float(roc), 4),
                    "close": round(float(closes[-1]), 2),
                    "date": str(df.iloc[-1].get("date", ""))}

        # ROC 阈值判定
        roc_triggered = abs(roc) >= self.roc_threshold

        # 成交量确认
        vol_confirmed = True
        vol_ratio = 1.0
        if self.require_volume:
            avg_vol = np.mean(volumes[-self.period:])
            if avg_vol > 0:
                vol_ratio = volumes[-1] / avg_vol
                vol_confirmed = vol_ratio >= self.volume_multiplier
            else:
                vol_confirmed = False

        surged = roc_triggered and vol_confirmed

        return {
            "surged": surged,
            "roc": round(float(roc), 4),
            "roc_pct": round(float(roc * 100), 2),
            "roc_triggered": roc_triggered,
            "close": round(float(closes[-1]), 2),
            "volume": int(volumes[-1]),
            "avg_volume_short": round(float(np.mean(volumes[-5:])), 2) if len(volumes) >= 5 else 0,
            "vol_ratio": round(float(vol_ratio), 2),
            "vol_confirmed": vol_confirmed,
            "date": str(df.iloc[-1].get("date", "")),
        }
