"""
多格式报告输出：终端表格、CSV、Excel、数据库缓存、文本日志。
"""

import csv
import logging
from datetime import datetime
from pathlib import Path

from utils.config import cfg
from utils.helpers import format_pct, format_volume, get_recent_field

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

    logic_str = "AND" if logic == "and" else "OR"

    print()
    print("=" * 90)
    print(f"  扫描结果 — {results.get('scan_time', '')}")
    print(f"  组合逻辑: {logic_str}")
    strategy_params = results.get("strategy_params", {})
    for s_name in strats:
        params = strategy_params.get(s_name, {})
        desc = _format_strategy_desc(s_name, params)
        print(f"  策略 [{_STRATEGY_ABBR.get(s_name, s_name)}]: {desc}")
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
    """从命中记录中提取最近信号日期。"""
    date = get_recent_field(hit, "date")
    if date:
        return str(date)
    # 回退：策略顶层 date 字段
    for strat_key in hit.get("strategies", []):
        d = hit.get(strat_key, {}).get("date", "")
        if d:
            return str(d)
    return ""


def _extract_volume(hit: dict) -> str:
    """从命中记录中提取信号日成交量（格式化字符串）。"""
    vol = get_recent_field(hit, "volume")
    if vol:
        return format_volume(vol)
    # 回退：取平均成交量
    for strat_key in hit.get("strategies", []):
        avg = hit.get(strat_key, {}).get("avg_volume", 0)
        if avg:
            return format_volume(avg)
    return "---"


# 策略名缩写映射
_STRATEGY_ABBR = {
    "VolumeSurgeStrategy": "放量",
    "MABreakoutStrategy": "突破",
    "MACrossoverStrategy": "交叉",
    "DivergenceStrategy": "背离",
    "MomentumStrategy": "动量",
}


def _abbreviate_strategies(hit: dict) -> str:
    """将策略名列表转为缩写字符串。"""
    return ", ".join(
        _STRATEGY_ABBR.get(s, s) for s in hit.get("strategies", [])
    )


def _format_strategy_desc(name: str, params: dict) -> str:
    """将策略参数格式化为可读的中文描述。"""
    if name == "VolumeSurgeStrategy":
        check = params.get("check_days", 2)
        avg = params.get("avg_days", 22)
        mult_min = params.get("multiplier_min", 2.1)
        mult_max = params.get("multiplier_max", 0)
        ratio = params.get("pass_ratio", 0.85)
        max_str = f" ~ {mult_max}" if mult_max > 0 else ""
        return (f"最近 {check} 天 vs 过去 {avg} 天均值，"
                f"放量倍数 ≥ {mult_min}{max_str}，"
                f"通过率阈值 ≥ {ratio * 100:.0f}%")
    elif name == "MABreakoutStrategy":
        short = params.get("ma_short", 5)
        long = params.get("ma_long", 20)
        vol = params.get("vol_multiplier", 1.5)
        return f"价格突破 MA{short}（基于 MA{long}），量比 ≥ {vol}"
    elif name == "MACrossoverStrategy":
        fast = params.get("fast_ma", 5)
        slow = params.get("slow_ma", 20)
        direction = params.get("direction", "golden")
        dir_cn = {"golden": "金叉", "death": "死叉", "both": "双向"}.get(direction, direction)
        return f"MA{fast} 与 MA{slow} {dir_cn}，确认 {params.get('confirmation_days', 1)} 天"
    elif name == "DivergenceStrategy":
        return (f"回顾 {params.get('lookback', 30)} 天，"
                f"缩量阈值 < {params.get('volume_shrink_threshold', 0.7)}，"
                f"价格峰窗口 {params.get('price_peak_window', 5)} 天")
    elif name == "MomentumStrategy":
        vol_str = f"，量比 ≥ {params.get('volume_multiplier', 1.2)}" if params.get('require_volume', True) else ""
        return f"{params.get('period', 20)} 日涨幅 ≥ {params.get('roc_threshold', 0.05) * 100:.1f}%{vol_str}"
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _format_detail(hit: dict) -> str:
    """将策略详情格式化为可读字符串。"""
    parts = []
    for strat_key in hit.get("strategies", []):
        data = hit.get(strat_key, {})
        if strat_key == "VolumeSurgeStrategy":
            avg_vol = data.get("avg_volume", 0)
            recent = data.get("recent", [])
            ratios = [f"{r.get('ratio', 0):.1f}x" for r in recent]
            parts.append(f"放量: 均值{format_volume(avg_vol)}, 倍数 {' → '.join(ratios)}")
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
        f.write(f"组合逻辑: {results.get('logic', 'or')}\n")
        f.write(f"命中: {len(hits)} / {stats.get('total_scanned', 0)} 只\n")
        f.write(f"耗时: {stats.get('duration_seconds', 0)}s\n")
        f.write("-" * 80 + "\n")
        # 策略及参数
        strategy_params = results.get("strategy_params", {})
        for s_name in results.get("strategies", []):
            abbr = _STRATEGY_ABBR.get(s_name, s_name)
            params = strategy_params.get(s_name, {})
            desc = _format_strategy_desc(s_name, params)
            f.write(f"策略: {s_name}（{abbr}），{desc}\n")
        f.write("=" * 80 + "\n\n")

        # 表头
        f.write(f"{'序号':<6} {'代码':<8} {'名称':<10} {'日期':<12} {'成交量':>10} {'策略':<8} {'策略详情'}\n")
        f.write("-" * 120 + "\n")

        for i, h in enumerate(hits, 1):
            strats_abbr = _abbreviate_strategies(h)
            date = _extract_date(h)
            volume = _extract_volume(h)
            detail = _format_detail(h)
            f.write(f"{i:<6} {h.get('code', ''):<8} {h.get('name', ''):<10} {date:<12} {volume:>10} {strats_abbr:<8} {detail}\n")

        # 各策略命中统计（单次遍历）
        counts: dict[str, int] = {}
        for h in hits:
            for s_name in h.get("strategies", []):
                counts[s_name] = counts.get(s_name, 0) + 1
        f.write("\n--- 策略命中统计 ---\n")
        for s_name in results.get("strategies", []):
            f.write(f"  [{s_name}]: {counts.get(s_name, 0)} 只\n")

    logger.info(f"文本日志已保存: {path}")


# ── 输出器注册表 ─────────────────────────────────


_REPORTERS = {
    "console": report_console,
    "csv": report_csv,
    "db": report_db,
    "excel": report_excel,
    "text": report_text,
}
