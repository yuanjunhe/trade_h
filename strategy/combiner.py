"""
策略组合器：AND/OR 逻辑组合多个策略。
"""

from enum import Enum

from strategy.base import BaseStrategy


class CombineLogic(Enum):
    AND = "and"  # 全部策略都触发才算命中
    OR = "or"    # 任一策略触发即算命中


class StrategyCombiner:
    """组合多个策略，支持 AND/OR 逻辑。

    用法:
        combiner = StrategyCombiner([strat1, strat2], logic=CombineLogic.OR)
        # OR: 取命中并集，每只股票标记触发了哪些策略
        # AND: 取命中交集，需全部策略都触发

        hits = combiner.scan(conn, stocks)
    """

    def __init__(self, strategies: list[BaseStrategy],
                 logic: CombineLogic = CombineLogic.OR):
        self.strategies = strategies
        self.logic = logic

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.strategies]

    def scan(self, conn, stocks: list[dict]) -> list[dict]:
        """运行所有策略并合并结果。

        参数:
            conn: 数据库连接。
            stocks: 字典列表，含 'code' 和 'name'。

        返回:
            合并后的结果列表。

        OR 模式: 每只股票出现一次，附带 'strategies' 列表标记触发的策略。
        AND 模式: 仅返回触发全部策略的股票。
        """
        if not self.strategies:
            return []

        all_hits: dict[str, dict] = {}  # code → 合并结果

        for strat in self.strategies:
            hits = strat.scan(conn, stocks)
            for h in hits:
                code = h["code"]
                if code not in all_hits:
                    all_hits[code] = h
                    all_hits[code]["strategies"] = []
                all_hits[code]["strategies"].append(strat.name)
                # 合并各策略的专属字段
                all_hits[code][strat.name] = {
                    k: v for k, v in h.items()
                    if k not in ("code", "name", "strategy", "strategies", "surged")
                }

        if self.logic == CombineLogic.OR:
            # 按触发策略数降序排列
            return sorted(
                all_hits.values(),
                key=lambda x: len(x.get("strategies", [])),
                reverse=True,
            )
        else:  # AND
            n = len(self.strategies)
            return [h for h in all_hits.values()
                    if len(h.get("strategies", [])) == n]


def create_strategy(name: str, params: dict | None = None) -> BaseStrategy | None:
    """工厂函数：根据名称创建策略实例。

    参数:
        name: 策略名称，如 "volume_surge", "ma_breakout", "ma_crossover" 等。
        params: 参数字典（与配置默认值合并）。

    返回:
        策略实例；名称不识别时返回 None。
    """
    params = params or {}

    if name == "volume_surge":
        from strategy.volume_surge import VolumeSurgeStrategy
        return VolumeSurgeStrategy(params)
    elif name == "ma_breakout":
        from strategy.ma_breakout import MABreakoutStrategy
        return MABreakoutStrategy(params)
    elif name == "ma_crossover":
        from strategy.ma_crossover import MACrossoverStrategy
        return MACrossoverStrategy(params)
    elif name == "divergence":
        from strategy.divergence import DivergenceStrategy
        return DivergenceStrategy(params)
    elif name == "momentum":
        from strategy.momentum import MomentumStrategy
        return MomentumStrategy(params)
    else:
        return None


def create_strategies_from_config(config: dict) -> list[BaseStrategy]:
    """从配置字典创建所有已启用的策略。

    参数:
        config: 完整配置字典（来自 utils.config）。

    返回:
        已启用的策略实例列表。
    """
    strategies = []
    strat_configs = config.get("strategies", {})

    for name, params in strat_configs.items():
        if params.get("enabled", False):
            strat = create_strategy(name, params)
            if strat:
                strategies.append(strat)

    return strategies
