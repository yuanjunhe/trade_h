"""
多格式报告输出：终端表格、CSV、Excel、数据库缓存、文本日志。
"""

import csv
import logging
from datetime import datetime
from pathlib import Path

from utils.config import cfg
from utils.helpers import format_pct, format_volume

logger = logging.getLogger("quant.reporter")


def report_results(results: dict, output_formats: list[str] | None = None,
                   db=None):
    """将扫描结果分发到所有请求的输出格式。

    参数:
        results: scanner.scan_stocks() 返回的字典。
        output_formats: 如 ["console", "csv", "db", "excel", "text"]。
                       默认从配置读取。
        db: Database 实例（写入 scan_cache 表时需要）。
    """
    if output_formats is None:
        output_formats = _get_enabled_formats()

    for fmt in output_formats:
        fn = _REPORTERS.get(fmt)
        if fn:
            try:
                if fmt == "db":
                    fn(results, db)
                else:
                    fn(results)
            except Exception as e:
                logger.error(f"输出 '{fmt}' 失败: {e}")
        else:
            logger.warning(f"未知输出格式: {fmt}")


def _get_enabled_formats() -> list[str]:
    """从配置获取已启用的输出格式。"""
    conf = cfg("output")
    formats = ["console"]  # 终端输出始终开启
    if conf.get("db_cache", {}).get("enabled", True):
        formats.append("db")
    if conf.get("csv", {}).get("enabled", False):
        formats.append("csv")
    if conf.get("excel", {}).get("enabled", False):
        formats.append("excel")
    if conf.get("text_log", {}).get("enabled", True):
        formats.append("text")
    return formats


# ── 终端输出 ─────────────────────────────────────


def report_console(results: dict):
    """格式化打印扫描结果到终端。"""
    hits = results.get("hits", [])
    strats = results.get("strategies", [])
    logic = results.get("logic", "or")
    stats = results.get("stats", {})

    strategy_str = " + ".join(strats) if strats else "None"
    logic_str = "AND" if logic == "and" else "OR"

    print()
    print("=" * 90)
    print(f"  扫描结果 — {results.get('scan_time', '')}")
    print(f"  策略: {strategy_str}  (组合逻辑: {logic_str})")
    print(f"  命中: {len(hits)} / {stats.get('total_scanned', 0)} 只  "
          f"耗时: {stats.get('duration_seconds', 0)}s")
    print("=" * 90)

    if not hits:
        print("  无命中股票。")
        return

    max_rows = cfg("output", "console", "max_rows")
    display = hits[:max_rows]

    # 表头
    print(f"\n{'代码':<8} {'名称':<10} {'触发策略':<35} {'最新价':>8} {'涨跌幅':>8}")
    print("-" * 90)

    for h in display:
        code = h.get("code", "")
        name = h.get("name", "")
        strats_hit = ", ".join(h.get("strategies", ["?"]))
        close = _extract_close(h)
        pct_change = _extract_pct_change(h)
        close_str = f"{close:.2f}" if close else "---"
        pct_str = format_pct(pct_change) if pct_change else "---"

        print(f"{code:<8} {name:<10} {strats_hit:<35} {close_str:>8} {pct_str:>8}")

    if len(hits) > max_rows:
        print(f"  ... 仅显示前 {max_rows} 只，共 {len(hits)} 只（完整结果见输出文件）")

    # 各策略命中统计
    print()
    for s_name in strats:
        count = sum(1 for h in hits if s_name in h.get("strategies", []))
        print(f"  [{s_name}]: {count} 只")

    print("=" * 90)
    print()


def _extract_close(hit: dict) -> float | None:
    """从命中记录中提取最新收盘价。"""
    for strat_key in hit.get("strategies", []):
        strat_data = hit.get(strat_key, {})
        if "close" in strat_data:
            return strat_data["close"]
    return None


def _extract_pct_change(hit: dict) -> float | None:
    """从命中记录中提取涨跌幅相关信息。"""
    for strat_key in hit.get("strategies", []):
        strat_data = hit.get(strat_key, {})
        if "roc_pct" in strat_data:
            return strat_data["roc_pct"]
        if "vol_ratio" in strat_data:
            return (strat_data["vol_ratio"] - 1) * 100
    return None


# ── CSV 输出 ─────────────────────────────────────


def report_csv(results: dict):
    """将扫描结果导出为 CSV 文件。"""
    path = cfg("output", "csv", "filename")
    if not path:
        return

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    hits = results.get("hits", [])

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "代码", "名称", "触发策略", "最新价", "日期",
            "策略详情",
        ])
        for h in hits:
            strats = ", ".join(h.get("strategies", []))
            close = _extract_close(h)
            date = _extract_date(h)
            detail = _format_detail(h)
            writer.writerow([
                h.get("code", ""),
                h.get("name", ""),
                strats,
                close,
                date,
                detail,
            ])

        # 各策略命中统计
        writer.writerow([])
        writer.writerow(["--- 策略命中统计 ---"])
        for s_name in results.get("strategies", []):
            count = sum(1 for h in hits if s_name in h.get("strategies", []))
            writer.writerow([s_name, f"{count} 只"])

    logger.info(f"CSV 已导出: {path} ({len(hits)} 行)")


def _extract_date(hit: dict) -> str:
    """从命中记录中提取日期。"""
    for strat_key in hit.get("strategies", []):
        strat_data = hit.get(strat_key, {})
        if "date" in strat_data:
            return strat_data["date"]
    return ""


def _format_detail(hit: dict) -> str:
    """将策略详情格式化为可读字符串。"""
    parts = []
    for strat_key in hit.get("strategies", []):
        data = hit.get(strat_key, {})
        if strat_key == "VolumeSurgeStrategy":
            avg_vol = data.get("avg_volume", 0)
            recent = data.get("recent", [])
            ratios = [f"{r.get('ratio', 0):.1f}x" for r in recent]
            parts.append(f"放量: 均值{format_volume(avg_vol)}, 倍数{','.join(ratios)}")
        elif strat_key == "MABreakoutStrategy":
            parts.append(
                f"突破: {data.get('close', 0):.2f} "
                f"MA{data.get('ma_short', '?')}={data.get('ma_short', 0):.2f} "
                f"量比{data.get('vol_ratio', 0):.1f}"
            )
        elif strat_key == "MACrossoverStrategy":
            parts.append(
                f"{data.get('cross_type', '')}交叉: "
                f"快线{data.get('fast_ma', 0):.2f} "
                f"慢线{data.get('slow_ma', 0):.2f}"
            )
        elif strat_key == "DivergenceStrategy":
            parts.append(f"{data.get('divergence_type', '')}背离: 量比{data.get('vol_ratio', 0):.2f}")
        elif strat_key == "MomentumStrategy":
            parts.append(f"动量: ROC={data.get('roc_pct', 0):.2f}% 量比{data.get('vol_ratio', 0):.1f}")
    return "; ".join(parts)


# ── 数据库缓存输出 ────────────────────────────────


def report_db(results: dict, db=None):
    """将扫描结果写入 scan_cache 数据库表，与 monitor.py 共享。"""
    if db is None:
        logger.warning("数据库实例未传入，跳过 scan_cache 写入")
        return

    db.save_scan_cache(
        scan_time=results.get("scan_time", ""),
        strategies=results.get("strategies", []),
        hits=results.get("hits", []),
    )
    logger.info("扫描缓存已写入 scan_cache 表")


# ── Excel 输出 ───────────────────────────────────


def report_excel(results: dict):
    """将扫描结果导出为 Excel 文件。"""
    path = cfg("output", "excel", "filename")
    if not path:
        return

    try:
        import pandas as pd
    except ImportError:
        logger.warning("需要 pandas 和 openpyxl 才能导出 Excel。安装: pip install pandas openpyxl")
        return

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    hits = results.get("hits", [])

    rows = []
    for h in hits:
        rows.append({
            "代码": h.get("code", ""),
            "名称": h.get("name", ""),
            "触发策略": ", ".join(h.get("strategies", [])),
            "最新价": _extract_close(h),
            "日期": _extract_date(h),
        })

    df = pd.DataFrame(rows)
    df.to_excel(path, index=False, engine="openpyxl")
    logger.info(f"Excel 已导出: {path} ({len(rows)} 行)")


# ── 文本日志输出 ─────────────────────────────────


def report_text(results: dict):
    """将扫描结果写入带时间戳的文本文件。"""
    directory = cfg("output", "text_log", "directory")
    if not directory:
        return

    Path(directory).mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(directory) / f"scan_{now}.txt"

    hits = results.get("hits", [])
    stats = results.get("stats", {})

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"扫描时间: {results.get('scan_time', '')}\n")
        f.write(f"策略: {', '.join(results.get('strategies', []))}\n")
        f.write(f"组合逻辑: {results.get('logic', 'or')}\n")
        f.write(f"命中: {len(hits)} / {stats.get('total_scanned', 0)} 只\n")
        f.write(f"耗时: {stats.get('duration_seconds', 0)}s\n")
        f.write("=" * 80 + "\n\n")

        for i, h in enumerate(hits, 1):
            strats_hit = ", ".join(h.get("strategies", []))
            f.write(f"{i}. {h.get('code', '')} {h.get('name', '')}  [{strats_hit}]\n")
            detail = _format_detail(h)
            if detail:
                f.write(f"   {detail}\n")

        # 各策略命中统计
        f.write("\n--- 策略命中统计 ---\n")
        for s_name in results.get("strategies", []):
            count = sum(1 for h in hits if s_name in h.get("strategies", []))
            f.write(f"  [{s_name}]: {count} 只\n")

    logger.info(f"文本日志已保存: {path}")


# ── 输出器注册表 ─────────────────────────────────


_REPORTERS = {
    "console": report_console,
    "csv": report_csv,
    "db": report_db,
    "excel": report_excel,
    "text": report_text,
}
