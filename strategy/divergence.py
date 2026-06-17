"""
量价背离策略：检测量价关系中的背离信号。

顶背离（看跌）：价格创新高，但成交量萎缩。
底背离（看涨）：价格创新低，但成交量放大。
"""

import numpy as np

from strategy.base import BaseStrategy


class DivergenceStrategy(BaseStrategy):
    """检测量价背离信号。

    - 顶背离（看跌反转信号）：价格创新高，但成交量相比前一个峰缩量。
    - 底背离（看涨反转信号）：价格创新低，但成交量相比前一个谷放量。

    参数:
        lookback: int = 30                — 查找波峰波谷的回看窗口
        volume_shrink_threshold: float = 0.7
            — 顶背离：当前峰量 < 前峰量 × 此阈值
        volume_expand_threshold: float = 1.5
            — 底背离：当前谷量 > 前谷量 × 此阈值
        price_peak_window: int = 5        — 波峰之间的最小间隔
        direction: str = "both"           — "bearish" / "bullish" / "both"
        require_volume_confirm: bool = True
    """

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.lookback = self.params.get("lookback", 30)
        self.volume_shrink_threshold = self.params.get("volume_shrink_threshold", 0.7)
        self.volume_expand_threshold = self.params.get("volume_expand_threshold", 1.5)
        self.price_peak_window = self.params.get("price_peak_window", 5)
        self.direction = self.params.get("direction", "both")
        self.require_volume_confirm = self.params.get("require_volume_confirm", True)

    @property
    def required_days(self) -> int:
        return self.lookback + self.price_peak_window

    def _find_peaks(self, prices: np.ndarray, window: int) -> np.ndarray:
        """找到局部最大值（波峰）。"""
        peaks = np.zeros(len(prices), dtype=bool)
        for i in range(window, len(prices) - window):
            if prices[i] == np.max(prices[i - window:i + window + 1]):
                peaks[i] = True
        return peaks

    def _find_troughs(self, prices: np.ndarray, window: int) -> np.ndarray:
        """找到局部最小值（波谷）。"""
        troughs = np.zeros(len(prices), dtype=bool)
        for i in range(window, len(prices) - window):
            if prices[i] == np.min(prices[i - window:i + window + 1]):
                troughs[i] = True
        return troughs

    def check(self, stock_code: str) -> dict | None:
        """检测单只股票的量价背离。"""
        df = self.get_stock_data(stock_code)
        if df is None:
            return None

        closes = df["收盘"].to_numpy(dtype=np.float64)
        volumes = df["成交量"].to_numpy(dtype=np.float64)

        if len(closes) < self.required_days:
            return None

        w = self.price_peak_window

        # 找到波峰和波谷
        peaks = self._find_peaks(closes, w)
        troughs = self._find_troughs(closes, w)

        # 聚焦回看窗口
        n = len(closes)
        start = max(0, n - self.lookback)

        recent_peaks = np.where(peaks[start:] & (np.arange(len(peaks) - start) >= w))[0] + start
        recent_troughs = np.where(troughs[start:] & (np.arange(len(troughs) - start) >= w))[0] + start

        result = {
            "surged": False,
            "divergence_type": None,
            "close": round(float(closes[-1]), 2),
            "volume": int(volumes[-1]),
            "date": str(df.iloc[-1].get("date", "")),
        }

        # ── 顶背离检测（价格更高、量更小） ──
        if self.direction in ("bearish", "both") and len(recent_peaks) >= 2:
            p2 = recent_peaks[-1]  # 最新峰
            p1 = recent_peaks[-2]  # 前一峰

            if closes[p2] > closes[p1]:  # 价格创新高
                vol_ratio = volumes[p2] / volumes[p1] if volumes[p1] > 0 else 1.0
                if not self.require_volume_confirm or vol_ratio < self.volume_shrink_threshold:
                    result["surged"] = True
                    result["divergence_type"] = "bearish"
                    result["peak1_price"] = round(float(closes[p1]), 2)
                    result["peak2_price"] = round(float(closes[p2]), 2)
                    result["peak1_vol"] = int(volumes[p1])
                    result["peak2_vol"] = int(volumes[p2])
                    result["vol_ratio"] = round(float(vol_ratio), 2)
                    result["peak1_date"] = str(df.iloc[p1].get("date", ""))
                    result["peak2_date"] = str(df.iloc[p2].get("date", ""))
                    return result

        # ── 底背离检测（价格更低、量更大） ──
        if self.direction in ("bullish", "both") and len(recent_troughs) >= 2:
            t2 = recent_troughs[-1]  # 最新谷
            t1 = recent_troughs[-2]  # 前一谷

            if closes[t2] < closes[t1]:  # 价格创新低
                vol_ratio = volumes[t2] / volumes[t1] if volumes[t1] > 0 else 1.0
                if not self.require_volume_confirm or vol_ratio > self.volume_expand_threshold:
                    result["surged"] = True
                    result["divergence_type"] = "bullish"
                    result["trough1_price"] = round(float(closes[t1]), 2)
                    result["trough2_price"] = round(float(closes[t2]), 2)
                    result["trough1_vol"] = int(volumes[t1])
                    result["trough2_vol"] = int(volumes[t2])
                    result["vol_ratio"] = round(float(vol_ratio), 2)
                    result["trough1_date"] = str(df.iloc[t1].get("date", ""))
                    result["trough2_date"] = str(df.iloc[t2].get("date", ""))
                    return result

        return result
