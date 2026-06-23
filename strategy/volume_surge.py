"""
成交量异动策略：检测成交量异常放大的股票。
从 stock_volume.py 的 check_volume_surge() 移植。
"""

import numpy as np

from strategy.base import BaseStrategy


class VolumeSurgeStrategy(BaseStrategy):
    """成交量异动检测：检查期每一天的成交量都远超基期平均水平。

    通过 numpy 向量化逐日对比实现高效计算。

    参数:
        check_days: int = 2         — 检查期天数（连续N天都要满足）
        avg_days: int = 22          — 基期天数（参考窗口）
        multiplier_min: float = 2.1 — 最低放量倍数
        multiplier_max: float = 0   — 最高放量倍数（0=不设上限）
        pass_ratio: float = 0.85    — 逐日对比通过率阈值
        require_increasing: bool = True — check_days>=2 时，检查期成交量需逐日递增
    """

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.check_days = self.params.get("check_days", 2)
        self.avg_days = self.params.get("avg_days", 22)
        self.multiplier_min = self.params.get("multiplier_min", 2.1)
        self.multiplier_max = self.params.get("multiplier_max", 0)
        self.pass_ratio = self.params.get("pass_ratio", 0.85)
        self.min_avg_volume = self.params.get("min_avg_volume", 0)
        self.require_increasing = self.params.get("require_increasing", True)

    @property
    def required_days(self) -> int:
        return self.avg_days + self.check_days

    def check(self, stock_code: str) -> dict | None:
        """检测单只股票的成交量异动。

        返回:
            dict，含 surged, avg_volume, threshold_min, threshold_max,
            recent（每日检测结果列表）；数据不足时返回 None。
        """
        df = self.get_stock_data(stock_code)
        if df is None:
            return None

        total_needed = self.avg_days + self.check_days
        if len(df) < total_needed:
            return None

        df = df.tail(total_needed).reset_index(drop=True)

        # 前 avg_days 天为基期，后 check_days 天为检查期
        base_volumes = df.iloc[:self.avg_days]["成交量"].to_numpy(dtype=np.float64)
        recent_df = df.iloc[self.avg_days:]

        # 过滤基期中成交量为 0 的无效数据
        valid_mask = base_volumes > 0
        valid_bases = base_volumes[valid_mask]
        valid_base_count = len(valid_bases)
        if valid_base_count == 0:
            return None

        # 最低日均成交量过滤
        avg_vol = base_volumes.mean()
        if self.min_avg_volume > 0 and avg_vol / 10000 < self.min_avg_volume:
            return None

        # 向量化逐日对比：recent_vols (n_recent, 1) / valid_bases (1, n_valid)
        recent_vols = recent_df["成交量"].to_numpy(dtype=np.float64)
        ratios_matrix = recent_vols[:, np.newaxis] / valid_bases[np.newaxis, :]

        # 判定矩阵：比值是否在 [multiplier_min, multiplier_max) 范围内
        if self.multiplier_max > 0:
            pass_matrix = (ratios_matrix >= self.multiplier_min) & (
                ratios_matrix < self.multiplier_max)
        else:
            pass_matrix = ratios_matrix >= self.multiplier_min

        pass_counts = pass_matrix.sum(axis=1)
        required_pass = int(valid_base_count * self.pass_ratio)
        day_ok_flags = pass_counts >= required_pass

        # 每个检查日的中位数倍数
        median_ratios = np.median(ratios_matrix, axis=1)

        results = []
        for i in range(min(self.check_days, len(recent_df))):
            results.append({
                "date": str(recent_df.iloc[i].get("date", "")),
                "volume": int(recent_vols[i]),
                "ratio": round(float(median_ratios[i]), 2),
                "pass_count": int(pass_counts[i]),
                "total_count": valid_base_count,
                "surged": bool(day_ok_flags[i]),
            })

        all_surged = bool(all(day_ok_flags))

        # check_days>=2 时，要求检查期成交量逐日递增
        # （最近一天 > 次最近一天 > ...）
        increasing_ok = True
        if self.require_increasing and len(recent_vols) >= 2:
            increasing_ok = bool(np.all(np.diff(recent_vols) > 0))

        return {
            "surged": all_surged and increasing_ok,
            "avg_volume": round(float(avg_vol), 2),
            "threshold_min": round(float(avg_vol * self.multiplier_min), 2),
            "threshold_max": (
                round(float(avg_vol * self.multiplier_max), 2)
                if self.multiplier_max > 0 else None
            ),
            "recent": results,
        }
