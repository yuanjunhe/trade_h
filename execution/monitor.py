"""
实时行情监控器：读取扫描缓存，查询新浪/东方财富实时行情。
从 stock_monitor.py 重构。
"""

import logging
import os
import sys
import time
from datetime import datetime

from utils.helpers import format_pct

logger = logging.getLogger("quant.monitor")

try:
    import requests
except ImportError:
    requests = None

try:
    import akshare as ak
except ImportError:
    ak = None


# ── 配置 ──────────────────────────────────────────

from utils.config import cfg, get_config

MAX_RETRIES = 2
RETRY_BACKOFF = 1.0
BATCH_SIZE = 200
REQUEST_TIMEOUT = 10
SINA_QUOTE_URL = "http://hq.sinajs.cn/list={codes}"
HEADERS = {
    "Referer": "http://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

# 去除系统代理（避免干扰 HTTP 连接）
for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]:
    os.environ.pop(k, None)


# ── 工具函数 ─────────────────────────────────────


def _code_to_sina(code: str) -> str:
    """将纯数字代码转为新浪格式（sh600519 / sz300750）。"""
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


def load_cache() -> dict | None:
    """从数据库 scan_cache 表读取最近一次扫描缓存。"""
    from data.db import Database
    conf = get_config()
    db = Database(conf.get("database", {}).get("path", "db/stock_data.db"))
    db.init_schema()
    return db.load_scan_cache()


def fetch_realtime_quotes(codes: list[str]) -> dict[str, dict]:
    """批量查询实时行情。东方财富优先，新浪兜底。

    参数:
        codes: 纯数字股票代码列表。

    返回:
        dict，key 为纯数字代码，value 为行情信息字典。
        查询失败的股票不出现在结果中。
    """
    if not codes:
        return {}

    result: dict[str, dict] = {}
    code_set = set(codes)

    # ── 第1层：东方财富 ──
    if ak is not None:
        for attempt in range(MAX_RETRIES + 1):
            try:
                df = ak.stock_zh_a_spot_em()
                if df is None or df.empty:
                    raise ValueError("empty")
                df["code_str"] = df["代码"].astype(str)
                filtered = df[df["code_str"].isin(code_set)]
                for _, row in filtered.iterrows():
                    pure_code = str(row["代码"])
                    prev_close = float(row["昨收"]) if row["昨收"] != "-" else 0.0
                    current = float(row["最新价"]) if row["最新价"] != "-" else 0.0
                    change_pct = (
                        round((current - prev_close) / prev_close * 100, 2)
                        if prev_close > 0 else 0.0
                    )
                    result[pure_code] = {
                        "name": str(row["名称"]),
                        "open": float(row["今开"]) if row["今开"] != "-" else 0.0,
                        "close": current,
                        "high": float(row["最高"]) if row["最高"] != "-" else 0.0,
                        "low": float(row["最低"]) if row["最低"] != "-" else 0.0,
                        "prev_close": prev_close,
                        "volume": int(float(row["成交量"])) if row["成交量"] != "-" else 0,
                        "amount": float(row["成交额"]) if row["成交额"] != "-" else 0.0,
                        "change_pct": change_pct,
                        "time": str(row.get("日期", "")),
                    }
                return result
            except Exception:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue
                break

    # ── 第2层：新浪兜底 ──
    if requests is None:
        return result

    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        sina_codes = ",".join(_code_to_sina(c) for c in batch)
        url = SINA_QUOTE_URL.format(codes=sina_codes)

        for attempt in range(MAX_RETRIES + 1):
            try:
                r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                r.encoding = "gbk"
                text = r.text
                break
            except Exception:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue
                text = ""
        if not text:
            continue

        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or '=""' in line:
                continue
            try:
                eq_pos = line.index("=")
                var_part = line[:eq_pos]
                val_part = line[eq_pos + 2:-1]
                sina_code = var_part.split("_")[-1]
                pure_code = sina_code[2:]
                fields = val_part.split(",")
                if len(fields) < 32:
                    continue
                name = fields[0]
                prev_close = float(fields[2])
                current = float(fields[3])
                change_pct = (
                    round((current - prev_close) / prev_close * 100, 2)
                    if prev_close > 0 else 0.0
                )
                result[pure_code] = {
                    "name": name,
                    "open": float(fields[1]),
                    "close": current,
                    "high": float(fields[4]),
                    "low": float(fields[5]),
                    "prev_close": prev_close,
                    "volume": int(float(fields[8])),
                    "amount": float(fields[9]),
                    "change_pct": change_pct,
                    "time": fields[31] if len(fields) > 31 else "",
                }
            except (ValueError, IndexError):
                continue

    return result


def print_monitor(cache: dict, quotes: dict):
    """格式化打印监控表格。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scan_time = cache.get("scan_time", "未知")

    print(f"\n{'═' * 90}")
    print(f"  实时行情监控  {now_str}  （扫描时间: {scan_time}）")
    print(f"{'═' * 90}")

    for strategy_key, strategy_name in [
        ("volume_surge", "【成交量异动】"),
        ("ma_breakout", "【放量突破均线】"),
    ]:
        stocks = cache.get(strategy_key, [])
        if not stocks:
            continue

        print(f"\n  {strategy_name} 共 {len(stocks)} 只")
        print(f"  {'代码':<8} {'名称':<10} {'最新价':>10} {'涨跌幅':>10} "
              f"{'今开':>10} {'最高':>10} {'最低':>10} {'成交量(万手)':>12}")
        print(f"  {'-' * 88}")

        # 按涨跌幅降序排列
        sorted_stocks = sorted(
            stocks,
            key=lambda s: quotes.get(s["code"], {}).get("change_pct", 0),
            reverse=True,
        )

        for s in sorted_stocks:
            q = quotes.get(s["code"])
            if q is None:
                print(f"  {s['code']:<8} {s['name']:<10} {'---':>10} {'---':>10}")
                continue

            vol_wan = q["volume"] / 10000  # 股 → 万手
            pct_str = format_pct(q["change_pct"])
            print(
                f"  {s['code']:<8} {q['name']:<10} "
                f"{q['close']:>10.2f} {pct_str:>10} "
                f"{q['open']:>10.2f} {q['high']:>10.2f} {q['low']:>10.2f} "
                f"{vol_wan:>12.2f}"
            )

    print(f"\n{'═' * 90}")


def run_once():
    """单次监控查询。"""
    cache = load_cache()
    if cache is None:
        print("错误: 未找到扫描缓存（scan_cache 表为空）")
        print("请先运行: python main.py scan")
        return

    codes = []
    seen = set()
    for key in ("volume_surge", "ma_breakout"):
        for s in cache.get(key, []):
            if s["code"] not in seen:
                codes.append(s["code"])
                seen.add(s["code"])

    if not codes:
        print("缓存中无股票数据。")
        return

    quotes = fetch_realtime_quotes(codes)
    print_monitor(cache, quotes)


def run_loop(interval: int = 30):
    """持续循环监控。

    参数:
        interval: 刷新间隔秒数，默认 30。
    """
    print(f"循环监控模式，每 {interval} 秒刷新（Ctrl+C 退出）\n")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print("\n监控已停止。")
            break
        except Exception as e:
            print(f"\n查询异常: {e}")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n监控已停止。")
            break
