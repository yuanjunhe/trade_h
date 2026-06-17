"""
放量突破均线策略：检测价格首次突破短期均线并伴随放量。
从 stock_volume.py 的 check_ma_breakout() 移植。
"""

import numpy as np

from strategy.base import BaseStrategy


class MABreakoutStrategy(BaseStrategy):
    """放量突破均线检测。

    信号条件（全部满足才触发）:
        1. 当日收盘 > 短期均线（价格突破）
        2. 前一日收盘 ≤ 前一日短期均线（首次突破，排除一直在均线上方的情况）
        3. 当日收盘 > 中期均线（中期趋势向上确认）
        4. 当日成交量 ≥ 短期日均量 × vol_multiplier（放量确认）

    参数:
        ma_short: int = 5           — 短期均线周期
        ma_long: int = 20           — 中期均线周期（趋势确认）
        vol_multiplier: float = 1.5 — 成交量需 ≥ 短期日均量的倍数
    """

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.ma_short = self.params.get("ma_short", 5)
        self.ma_long = self.params.get("ma_long", 20)
        self.vol_multiplier = self.params.get("vol_multiplier", 1.5)

    @property
    def required_days(self) -> int:
        return self.ma_long + 1

    def check(self, stock_code: str) -> dict | None:
        """检测单只股票的放量突破信号。

        返回:
            dict，含 surged, close, ma_short, ma_long, volume,
            avg_volume, vol_ratio, date 等字段；数据不足时返回 None。
        """
        df = self.get_stock_data(stock_code)
        if df is None:
            return None

        needed = self.ma_long + 1
        if len(df) < needed:
            return None

        df = df.tail(needed).reset_index(drop=True)

        closes = df["收盘"].to_numpy(dtype=np.float64)
        volumes = df["成交量"].to_numpy(dtype=np.float64)

        today_idx = -1
        yesterday_idx = -2

        # 当日短期均线（含当日）
        ma_short_today = np.mean(closes[-self.ma_short:])
        # 前一日短期均线（不含当日）
        ma_short_yesterday = np.mean(closes[-self.ma_short - 1:-1])
        # 当日中期均线
        ma_long_today = np.mean(closes[-self.ma_long:])

        close_today = closes[today_idx]
        close_yesterday = closes[yesterday_idx]
        vol_today = volumes[today_idx]
        avg_vol = np.mean(volumes[-self.ma_short:])

        if avg_vol <= 0 or vol_today <= 0:
            return None

        vol_ratio = vol_today / avg_vol

        # 四个判定条件
        breakout = close_today > ma_short_today           # 突破短期均线
        first_cross = close_yesterday <= ma_short_yesterday  # 首次穿越
        trend_up = close_today > ma_long_today            # 中期趋势向上
        volume_surge = vol_ratio >= self.vol_multiplier   # 放量确认

        surged = breakout and first_cross and trend_up and volume_surge

        return {
            "surged": surged,
            "close": round(float(close_today), 2),
            "ma_short": round(float(ma_short_today), 2),
            "ma_long": round(float(ma_long_today), 2),
            "volume": int(vol_today),
            "avg_volume": round(float(avg_vol), 2),
            "vol_ratio": round(float(vol_ratio), 2),
            "date": str(df.iloc[today_idx].get("date", "")),
            # 子条件详情（调试用）
            "_breakout": breakout,
            "_first_cross": first_cross,
            "_trend_up": trend_up,
            "_volume_surge": volume_surge,
        }
