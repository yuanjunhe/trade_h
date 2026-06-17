"""
全量下载和每日增量更新流水线。
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd

from data.fetcher import fetch_stock_history, fetch_stock_incremental
from utils.config import cfg
from utils.helpers import filter_incomplete_today, last_trading_day

logger = logging.getLogger("quant.pipeline")

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    def tqdm(iterable, **kwargs):
        return iterable


def _make_progress(iterable, total: int, desc: str = ""):
    """tqdm 可用时创建进度条，否则返回原始可迭代对象。"""
    if HAS_TQDM:
        return tqdm(iterable, total=total, desc=desc, unit="只")
    return iterable


# ── 全量下载流水线 ─────────────────────────────


def download_full_history(
    db,
    stocks: list[dict],
    workers: int | None = None,
    rate_limit: float | None = None,
    resume: bool = True,
    reset: bool = False,
    start_date: str = "19900101",
) -> dict:
    """下载所有给定股票的历史数据。

    参数:
        db: Database 实例。
        stocks: 字典列表，含 "code" 和 "name"。
        workers: 并发线程数，默认读取配置。
        rate_limit: 请求间隔秒数，默认读取配置。
        resume: 为 True 时跳过已标记 'done' 的股票。
        reset: 为 True 时清空下载状态重新下载所有。
        start_date: 起始日期 "YYYYMMDD"，默认 19900101。

    返回:
        统计字典: {total, succeeded, failed, skipped, updated}。
    """
    if workers is None:
        workers = cfg("pipeline", "download", "workers")
    if rate_limit is None:
        rate_limit = cfg("data", "rate_limit")
    timeout = cfg("data", "timeout")
    primary = cfg("data", "primary_source")

    if reset:
        db.clear_download_state("pending")
        logger.info("已重置所有下载状态为 pending")

    # 过滤出需要下载的股票
    to_download = []
    skipped = 0
    for s in stocks:
        state = db.get_download_state(s["code"])
        if resume and state and state["status"] == "done":
            # 验证数据确实存在且是最新的
            min_d, max_d = db.get_date_range(s["code"])
            if max_d:
                trading_day = last_trading_day()
                if max_d >= trading_day:
                    skipped += 1
                    continue
        to_download.append(s)

    total = len(to_download)
    if total == 0:
        logger.info("所有股票数据已是最新，无需下载。")
        return {"total": len(stocks), "succeeded": 0, "failed": 0, "skipped": skipped, "updated": 0}

    logger.info(f"开始下载 {total} 只股票的历史数据（{skipped} 只已完成）")

    succeeded = 0
    failed = 0

    def download_one(stock):
        """单只股票的下载 worker。"""
        code = stock["code"]
        name = stock["name"]

        try:
            df = fetch_stock_history(code, timeout=timeout, primary=primary, start_date=start_date)
            if df is None or df.empty:
                db.set_download_state(code, "error", error_msg="所有数据源均无返回")
                return (code, False, "无数据")

            # 按 start_date 过滤：baostock 降级时可能返回全量数据，只保留目标范围
            start_date_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
            df = df[df["日期"] >= start_date_fmt]
            if df.empty:
                db.set_download_state(code, "error", error_msg="目标日期范围内无数据")
                return (code, False, "过滤后无数据")

            db.insert_daily_batch(code, df)
            max_date = str(df["日期"].max())
            db.set_download_state(
                code, "done",
                last_date=max_date,
                total_rows=len(df),
            )
            return (code, True, f"{len(df)} 行, 到 {max_date}")
        except Exception as e:
            db.set_download_state(code, "error", error_msg=str(e)[:200])
            return (code, False, str(e)[:100])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for stock in to_download:
            futures[pool.submit(download_one, stock)] = stock
            time.sleep(rate_limit)  # 控制请求频率

        pbar = _make_progress(as_completed(futures), total=total, desc="下载中")

        for future in pbar:
            code, ok, msg = future.result()
            if ok:
                succeeded += 1
            else:
                failed += 1
                logger.warning(f"  失败: {code} - {msg}")

            if HAS_TQDM and isinstance(pbar, tqdm):
                pbar.set_postfix({"成功": succeeded, "失败": failed})

    logger.info(f"下载完成: 成功 {succeeded}, 失败 {failed}, 跳过 {skipped}")
    return {
        "total": len(stocks),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "updated": succeeded,
    }


# ── 每日增量更新流水线 ─────────────────────────


def daily_update(
    db,
    stocks: list[dict],
    workers: int | None = None,
    target_date: str | None = None,
) -> dict:
    """为每只股票获取自上次更新以来的新数据。

    参数:
        db: Database 实例。
        stocks: 字典列表，含 "code" 和 "name"。
        workers: 并发线程数。
        target_date: 目标结束日期 "YYYY-MM-DD"，默认最近交易日。

    返回:
        统计字典: {total, updated, skipped, failed, up_to_date}。
    """
    if workers is None:
        workers = cfg("pipeline", "update", "workers")
    timeout = cfg("data", "timeout")

    if target_date is None:
        target_date = datetime.now().strftime("%Y-%m-%d")
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")

    logger.info(f"每日更新: 检查 {len(stocks)} 只股票，目标日期 {target_date}")

    # 构建待更新列表
    to_update = []
    up_to_date = 0

    for s in stocks:
        max_date = db.get_latest_date(s["code"])
        if max_date is None:
            # 尚无数据，需要全量下载，增量更新跳过
            continue
        if max_date >= target_date:
            up_to_date += 1
            continue
        to_update.append((s, max_date))

    total_to_update = len(to_update)
    logger.info(f"  {up_to_date} 只已是最新, {total_to_update} 只需要更新")

    if total_to_update == 0:
        return {"total": len(stocks), "updated": 0, "skipped": 0, "failed": 0, "up_to_date": up_to_date}

    updated = 0
    failed = 0

    def update_one(item):
        """单只股票的增量更新 worker。"""
        stock, max_date = item
        code = stock["code"]
        name = stock["name"]

        # from_date = max_date + 1 天
        from_dt = datetime.strptime(max_date, "%Y-%m-%d") + timedelta(days=1)
        from_date = from_dt.strftime("%Y-%m-%d")

        try:
            df = fetch_stock_incremental(code, from_date, target_date, timeout=timeout)
            if df is None or df.empty:
                return (code, False, "无新数据")

            # 过滤到目标日期范围
            df = df[(df["日期"] >= from_date) & (df["日期"] <= target_date)]
            if df.empty:
                return (code, True, "0 行新数据（过滤后为空）")

            db.insert_daily_batch(code, df)

            new_max = str(df["日期"].max())
            total_rows = db.get_daily(code).shape[0] if len(df) > 0 else 0
            db.set_download_state(code, "done", last_date=new_max, total_rows=total_rows)

            return (code, True, f"+{len(df)} 行, 到 {new_max}")
        except Exception as e:
            return (code, False, str(e)[:100])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(update_one, item): item for item in to_update}
        pbar = _make_progress(as_completed(futures), total=total_to_update, desc="更新中")

        for future in pbar:
            code, ok, msg = future.result()
            if ok:
                updated += 1
            else:
                failed += 1
                logger.warning(f"  失败: {code} - {msg}")

            if HAS_TQDM and isinstance(pbar, tqdm):
                pbar.set_postfix({"成功": updated, "失败": failed})

    logger.info(f"更新完成: {updated} 只更新, {failed} 只失败, {up_to_date} 只已是最新")
    return {
        "total": len(stocks),
        "updated": updated,
        "skipped": 0,
        "failed": failed,
        "up_to_date": up_to_date,
    }
