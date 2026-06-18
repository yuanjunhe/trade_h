"""
多线程股票扫描器：编排策略执行的入口。
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from data.stock_list import filter_stocks
from strategy.combiner import (
    CombineLogic,
    StrategyCombiner,
    create_strategies_from_config,
)
from utils.config import cfg, get_config
from utils.helpers import filter_incomplete_today, get_recent_field

logger = logging.getLogger("quant.scanner")

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


def scan_stocks(
    db,
    strategies: list | None = None,
    logic: str = "or",
    stocks: list[dict] | None = None,
    workers: int = 15,
) -> dict[str, Any]:
    """对数据库中的股票运行选股策略。

    参数:
        db: Database 实例。
        strategies: 策略名称列表（字符串）或策略实例列表。
                    为 None 时使用配置中已启用的策略。
        logic: "and" 或 "or" — 多策略组合逻辑。
        stocks: 预过滤的股票列表。为 None 时从数据库/akshare 加载。
        workers: 线程池大小。

    返回:
        dict，包含:
            - scan_time: 扫描时间戳
            - strategies: 使用的策略名称列表
            - logic: 组合逻辑
            - hits: 命中结果列表
            - stats: {total_scanned, total_hits, duration_seconds}
    """
    start_time = datetime.now()

    # 解析策略
    if strategies is None:
        strats = create_strategies_from_config(get_config())
        strat_names = [s.name for s in strats]
    elif strategies and isinstance(strategies[0], str):
        strat_names = strategies
        strats = []
        for name in strategies:
            from strategy.combiner import create_strategy
            params = cfg("strategies", name)
            s = create_strategy(name, params)
            if s:
                strats.append(s)
    else:
        strats = strategies
        strat_names = [s.name for s in strats]

    if not strats:
        logger.warning("没有启用的策略。请在 config.yaml 中设置 strategies.*.enabled")
        return {"scan_time": start_time.isoformat(), "strategies": [],
                "logic": logic, "hits": [], "stats": {}}

    # 解析股票列表
    if stocks is None:
        logger.info("从数据库加载股票列表...")
        df = db.get_stock_info_df()
        if df.empty:
            logger.warning("stock_info 表为空，请先运行 update 或 download")
            return {"scan_time": start_time.isoformat(), "strategies": strat_names,
                    "logic": logic, "hits": [], "stats": {}}
        boards = get_config().get("scan", {}).get("boards", {})
        exclude_st = get_config().get("scan", {}).get("exclude_st", True)
        df = filter_stocks(df, boards=boards, exclude_st=exclude_st)
        stocks = df[["code", "name"]].to_dict("records")
        logger.info(f"  板块/ST 过滤后共 {len(stocks)} 只股票")

    # 运行策略
    logic_enum = CombineLogic.AND if logic == "and" else CombineLogic.OR
    combiner = StrategyCombiner(strats, logic=logic_enum)

    conn = db.get_conn()
    hits = combiner.scan(conn, stocks)

    # 按最近一天放量倍数倒序排列
    hits.sort(key=_last_ratio, reverse=True)

    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info(f"扫描完成: {len(hits)} 命中 / {len(stocks)} 只股票, 耗时 {elapsed:.1f}s")

    # 应用配置中的额外过滤器
    scan_config = get_config().get("scan", {})
    min_avg_volume = scan_config.get("min_avg_volume", 0)
    exclude_suspended = scan_config.get("exclude_suspended", True)

    if min_avg_volume > 0 or exclude_suspended:
        hits = _post_filter(hits, db, min_avg_volume, exclude_suspended)

    return {
        "scan_time": start_time.isoformat(),
        "strategies": strat_names,
        "logic": logic,
        "hits": hits,
        "stats": {
            "total_scanned": len(stocks),
            "total_hits": len(hits),
            "duration_seconds": round(elapsed, 1),
        },
        "strategy_params": {
            name: {k: v for k, v in s.params.items() if k != "enabled"}
            for name, s in zip(strat_names, strats)
        },
    }


def _post_filter(hits: list[dict], db, min_avg_volume: float,
                 exclude_suspended: bool) -> list[dict]:
    """扫描后过滤：最低成交量检查和停牌股排除。"""
    filtered = []
    for h in hits:
        code = h["code"]

        if exclude_suspended or min_avg_volume > 0:
            df = db.get_daily(code, columns=["date", "volume"])
            df = df.tail(10)
            if df.empty:
                continue

            # 停牌检查：最近5天成交量全为0
            if exclude_suspended and len(df) >= 5:
                if (df.tail(5)["volume"] == 0).all():
                    continue

            # 最低成交量检查
            if min_avg_volume > 0 and len(df) >= 5:
                avg_vol = df["volume"].mean()
                if avg_vol / 10000 < min_avg_volume:
                    continue

        filtered.append(h)
    return filtered


def _last_ratio(hit: dict) -> float:
    """从命中记录中提取最近一天的放量倍数，用于排序。"""
    return get_recent_field(hit, "ratio", 0)
