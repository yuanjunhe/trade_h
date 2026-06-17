"""
A股量化选股系统。

用法:
    python main.py download    # 全量下载历史日线
    python main.py update      # 每日增量更新
    python main.py scan        # 运行选股策略
    python main.py report      # 数据库统计报告
    python main.py monitor     # 实时行情监控
    python main.py info        # 数据库简要信息

示例:
    python main.py download --reset              # 清空数据库重新下载
    python main.py download --stocks 10          # 测试：只下载10只
    python main.py download --board cy           # 只下载创业板
    python main.py update --date 2026-06-16      # 更新到指定日期
    python main.py scan --strategy all --logic or
    python main.py scan --strategy volume_surge ma_breakout --output console,csv
    python main.py monitor --once                # 单次行情查询
    python main.py monitor --interval 30         # 每30秒循环刷新
"""

import argparse
import os
import sys

from datetime import datetime, timedelta

# 清除系统代理（代理会干扰 akshare/东方财富的 HTTP 连接）
for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# 修复 Windows 终端中文编码问题
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 默认起始日期：1 个月前
_DEFAULT_START = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")


def main():
    parser = argparse.ArgumentParser(
        description="A股量化选股系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py download --stocks 10      测试：下载10只股票
  python main.py download --board all      全市场下载
  python main.py update                    每日增量更新
  python main.py scan                      运行所有已启用的策略
  python main.py scan --strategy volume_surge ma_breakout
  python main.py monitor --once            快速行情查询
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # ── download：全量下载 ──
    dl = subparsers.add_parser("download", help="下载历史K线数据")
    dl.add_argument("--reset", action="store_true",
                    help="清空数据库，重新下载全部股票")
    dl.add_argument("--stocks", type=int, default=0,
                    help="限制下载前N只股票（0=全部）")
    dl.add_argument("--workers", type=int, default=10,
                    help="并发下载线程数（默认10）")
    dl.add_argument("--board", choices=["all", "sh", "sz", "cy", "kcb"],
                    default="all", help="板块过滤（默认all）")
    dl.add_argument("--start-date", type=str, default=_DEFAULT_START,
                    help=f"数据起始日期 YYYYMMDD（默认1月前: {_DEFAULT_START}，全量: 19900101）")

    # ── update：增量更新 ──
    up = subparsers.add_parser("update", help="每日增量数据更新")
    up.add_argument("--date", type=str, default=None,
                    help="目标日期 YYYY-MM-DD（默认今天）")
    up.add_argument("--workers", type=int, default=15,
                    help="并发线程数（默认15）")

    # ── scan：策略扫描 ──
    sc = subparsers.add_parser("scan", help="运行选股策略")
    sc.add_argument("--strategy", type=str, nargs="+",
                    default=["all"],
                    help="策略名称：volume_surge, ma_breakout, ma_crossover, "
                         "divergence, momentum, all")
    sc.add_argument("--logic", choices=["and", "or"], default="or",
                    help="多策略组合逻辑（默认or）")
    sc.add_argument("--board", choices=["all", "sh", "sz", "cy", "kcb"],
                    default="all", help="板块过滤（默认all）")
    sc.add_argument("--output", type=str, default="all",
                    help="输出格式：console, csv, excel, json, text, all")
    sc.add_argument("--workers", type=int, default=15,
                    help="线程池大小（默认15）")

    # ── report：统计报告 ──
    rp = subparsers.add_parser("report", help="生成数据库统计报告")

    # ── monitor：实时监控 ──
    mo = subparsers.add_parser("monitor", help="实时行情监控")
    mo.add_argument("--interval", type=int, default=30,
                    help="刷新间隔秒数（默认30）")
    mo.add_argument("--once", action="store_true",
                    help="执行一次后退出")

    # ── info：数据库信息 ──
    info = subparsers.add_parser("info", help="打印数据库统计信息")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    from execution.dispatcher import dispatch
    dispatch(args)


if __name__ == "__main__":
    main()
