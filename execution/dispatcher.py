"""
CLI 命令分发器：将 argparse 子命令映射到对应的处理函数。
"""

import logging
import sys
from datetime import datetime

from data.db import Database
from data.pipeline import daily_update, download_full_history
from data.stock_list import filter_stocks, get_stock_list, sync_stock_info_to_db
from execution.reporter import report_results
from utils.config import get_config, load_config
from utils.logging_setup import setup_logging

logger = logging.getLogger("quant.dispatcher")


def _get_db() -> Database:
    """从配置路径初始化数据库。"""
    config = get_config()
    db_path = config.get("database", {}).get("path", "db/stock_data.db")
    db = Database(db_path)
    db.init_schema()
    return db


def _get_stocks(db, board_filter: str = "all") -> list[dict]:
    """获取过滤后的股票列表。

    参数:
        db: Database 实例。
        board_filter: "all", "sh", "sz", "cy", "kcb"。
    """
    sync_stock_info_to_db(db)
    df = get_stock_list()

    # CLI 板块参数 → 配置格式映射
    if board_filter == "all":
        boards = {"sh_main": True, "sz_main": True, "chi": True, "kcb": True}
    elif board_filter == "sh":
        boards = {"sh_main": True, "sz_main": False, "chi": False, "kcb": False}
    elif board_filter == "sz":
        boards = {"sh_main": False, "sz_main": True, "chi": False, "kcb": False}
    elif board_filter == "cy":
        boards = {"sh_main": False, "sz_main": False, "chi": True, "kcb": False}
    elif board_filter == "kcb":
        boards = {"sh_main": False, "sz_main": False, "chi": False, "kcb": True}
    else:
        boards = get_config().get("scan", {}).get("boards", {})

    df = filter_stocks(df, boards=boards)
    return df[["code", "name"]].to_dict("records")


def cmd_download(args):
    """处理 'download' 子命令。"""
    db = _get_db()

    if args.reset:
        logger.info("重置数据库...")
        db.reset_all()

    stocks = _get_stocks(db, args.board)

    if args.stocks > 0:
        stocks = stocks[:args.stocks]
        logger.info(f"测试模式：只下载前 {args.stocks} 只")

    download_full_history(
        db,
        stocks,
        workers=args.workers,
        reset=False,
        start_date=args.start_date,
    )

    # 打印统计
    stats = db.stats()
    print(f"\n数据库统计:")
    print(f"  股票总数: {stats['stocks_in_info']}")
    print(f"  有数据: {stats['stocks_with_data']}")
    print(f"  总行数: {stats['total_daily_rows']}")
    print(f"  数据范围: {stats['data_from']} ~ {stats['data_to']}")


def cmd_update(args):
    """处理 'update' 子命令。"""
    db = _get_db()
    stocks = _get_stocks(db, "all")
    target_date = args.date or datetime.now().strftime("%Y-%m-%d")

    result = daily_update(db, stocks, target_date=target_date)
    print(f"\n更新结果: {result}")


def cmd_scan(args):
    """处理 'scan' 子命令。"""
    db = _get_db()
    stocks = _get_stocks(db, args.board)

    # 解析策略参数
    if args.strategy == ["all"]:
        strategies = None  # 使用配置中已启用的策略
    else:
        strategies = args.strategy

    # 解析输出格式
    if args.output == "all":
        output_formats = None  # 使用配置默认值
    else:
        output_formats = args.output.split(",")

    from execution.scanner import scan_stocks
    results = scan_stocks(
        db,
        strategies=strategies,
        logic=args.logic,
        stocks=stocks,
        workers=args.workers,
    )

    report_results(results, output_formats, db=db)


def cmd_report(args):
    """处理 'report' 子命令。"""
    db = _get_db()
    stats = db.stats()

    print(f"\n{'═' * 60}")
    print(f"  数据库报告 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * 60}")
    print(f"  数据库路径: {db.db_path}")
    print(f"  stock_info 股票数: {stats['stocks_in_info']}")
    print(f"  stock_daily 股票数: {stats['stocks_with_data']}")
    print(f"  stock_daily 总行数: {stats['total_daily_rows']}")
    print(f"  数据范围: {stats['data_from']} ~ {stats['data_to']}")
    print(f"  下载状态: 完成={stats['download_done']}, "
          f"待下载={stats['download_pending']}, 错误={stats['download_errors']}")
    print(f"{'═' * 60}\n")


def cmd_monitor(args):
    """处理 'monitor' 子命令。"""
    from execution.monitor import run_loop, run_once

    if args.once:
        run_once()
    else:
        run_loop(args.interval)


def cmd_info(args):
    """处理 'info' 子命令（与 report 相同）。"""
    cmd_report(args)


def dispatch(args):
    """根据 CLI 参数分发到对应处理函数。"""
    setup_logging("INFO")
    load_config()

    cmd_map = {
        "download": cmd_download,
        "update": cmd_update,
        "scan": cmd_scan,
        "report": cmd_report,
        "monitor": cmd_monitor,
        "info": cmd_info,
    }

    handler = cmd_map.get(args.command)
    if handler:
        handler(args)
    else:
        print(f"未知命令: {args.command}")
        sys.exit(1)
