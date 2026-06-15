"""
A 股实时涨幅监控工具 —— 基于扫描缓存 + 新浪行情接口。
读取 stock_volume.py 扫描生成的 scan_cache.json，查询实时涨跌幅。
依赖: pip install requests
"""

import json
import os
import sys
import time
from datetime import datetime

import akshare as ak
import requests

# ═══════════════════════════════════════════════════════════
#  配置区
# ═══════════════════════════════════════════════════════════

# 扫描缓存文件路径（与 stock_volume.py 共用）
SCAN_CACHE_FILE = "db/scan_cache.json"

# 接口不可用定义：连续失败 N+1 次后降级
MAX_RETRIES = 2

# 重试退避基数（秒）
RETRY_BACKOFF = 1.0

# 新浪行情接口地址
SINA_QUOTE_URL = "http://hq.sinajs.cn/list={codes}"

# 每次请求最大股票数（新浪限制约 800，这里保守取 200）
BATCH_SIZE = 200

# 请求超时（秒）
REQUEST_TIMEOUT = 10

# 请求头（新浪需要 Referer）
HEADERS = {
    "Referer": "http://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

# 去除代理（避免 push2 代理问题）
for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]:
    os.environ.pop(k, None)


# ═══════════════════════════════════════════════════════════
#  以下无需修改
# ═══════════════════════════════════════════════════════════


def load_cache() -> dict | None:
    """读取扫描缓存文件。

    返回:
        dict，包含 scan_time, volume_surge, ma_breakout；
        文件不存在或解析失败时返回 None。
    """
    if not os.path.exists(SCAN_CACHE_FILE):
        return None
    try:
        with open(SCAN_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _code_to_sina(code: str) -> str:
    """将纯数字股票代码转为新浪格式。

    规则：
      - 6 开头 → sh + code（上海）
      - 0/3 开头 → sz + code（深圳）
    """
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


def fetch_realtime_quotes(codes: list[str]) -> dict[str, dict]:
    """批量查询实时行情（东方财富优先，新浪兜底）。

    参数:
        codes: 纯数字股票代码列表，如 ["300750", "600519"]

    返回:
        dict，key 为纯数字代码，value 为行情 dict；
        查询失败的股票不出现在返回结果中。
    """
    result = {}
    if not codes:
        return result

    code_set = set(codes)

    # ── 第1层：东方财富（主） ──
    for attempt in range(MAX_RETRIES + 1):
        try:
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                raise ValueError("empty")
            # 过滤出目标股票
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
                    "time": str(row["日期"]) if "日期" in df.columns else "",
                }
            return result  # 东财成功，直接返回
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            break  # 降级

    # ── 第2层：新浪（兜底） ──
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i : i + BATCH_SIZE]
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

        for line in text.strip().split(";"):
            line = line.strip()
            if not line or '=""' in line:
                continue
            try:
                eq_pos = line.index("=")
                var_part = line[:eq_pos]
                val_part = line[eq_pos + 2 : -1]
                sina_code = var_part.split("_")[-1]
                pure_code = sina_code[2:]
                fields = val_part.split(",")
                if len(fields) < 32:
                    continue
                name = fields[0]
                prev_close = float(fields[2])
                open_price = float(fields[1])
                current = float(fields[3])
                high = float(fields[4])
                low = float(fields[5])
                volume = int(float(fields[8]))
                amount = float(fields[9])
                quote_time = fields[31] if len(fields) > 31 else ""
                change_pct = (
                    round((current - prev_close) / prev_close * 100, 2)
                    if prev_close > 0 else 0.0
                )
                result[pure_code] = {
                    "name": name,
                    "open": open_price,
                    "close": current,
                    "high": high,
                    "low": low,
                    "prev_close": prev_close,
                    "volume": volume,
                    "amount": amount,
                    "change_pct": change_pct,
                    "time": quote_time,
                }
            except (ValueError, IndexError):
                continue

    return result


def _format_pct(pct: float) -> str:
    """格式化涨跌幅，涨红跌绿。"""
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.2f}%"


def _bar(pct: float, width: int = 20) -> str:
    """生成简易柱状条，直观表示涨跌幅度。"""
    half = width // 2
    filled = int(pct / 10 * half)  # 10% 占一半宽度
    filled = max(-half, min(half, filled))

    if filled >= 0:
        return " " * half + "│" + "█" * filled + " " * (half - filled)
    else:
        return " " * (half + filled) + "█" * (-filled) + "│" + " " * half


def print_monitor(cache: dict, quotes: dict):
    """打印实时监控结果。

    参数:
        cache: 扫描缓存 dict，包含 volume_surge 和 ma_breakout 列表。
        quotes: 实时行情 dict，key 为纯数字代码。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scan_time = cache.get("scan_time", "未知")

    print(f"\n{'═' * 76}")
    print(f"  实时行情监控  {now_str}  （扫描时间: {scan_time}）")
    print(f"{'═' * 76}")

    for strategy_key, strategy_name in [
        ("volume_surge", "【成交量异动】"),
        ("ma_breakout", "【放量突破均线】"),
    ]:
        stocks = cache.get(strategy_key, [])
        if not stocks:
            continue

        print(f"\n  {strategy_name} 共 {len(stocks)} 只")
        print(f"  {'代码':<8} {'名称':<10} {'最新价':>10} {'涨跌幅':>10} {'今开':>10} {'最高':>10} {'最低':>10} {'成交量(万手)':>12}")
        print(f"  {'-' * 72}")

        # 按涨跌幅降序
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

            vol_wan = q["volume"] / 10000  # 股 → 万手（1手=100股）
            pct_str = _format_pct(q["change_pct"])
            print(
                f"  {s['code']:<8} {q['name']:<10} "
                f"{q['close']:>10.2f} {pct_str:>10} "
                f"{q['open']:>10.2f} {q['high']:>10.2f} {q['low']:>10.2f} "
                f"{vol_wan:>12.2f}"
            )

    print(f"\n{'═' * 76}")


def run_once():
    """执行一次监控查询。"""
    cache = load_cache()
    if cache is None:
        print(f"错误: 未找到扫描缓存文件 {SCAN_CACHE_FILE}")
        print("请先运行: python stock_volume.py scan")
        return

    # 收集所有代码（去重）
    all_codes = []
    seen = set()
    for key in ("volume_surge", "ma_breakout"):
        for s in cache.get(key, []):
            if s["code"] not in seen:
                all_codes.append(s["code"])
                seen.add(s["code"])

    if not all_codes:
        print("缓存中无股票数据，请先运行扫描。")
        return

    quotes = fetch_realtime_quotes(all_codes)
    print_monitor(cache, quotes)


def run_loop(interval: int = 30):
    """循环监控模式。

    参数:
        interval: 刷新间隔（秒），默认 30 秒。
    """
    print(f"循环监控模式，每 {interval} 秒刷新一次（Ctrl+C 退出）\n")
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


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"

    if mode == "loop":
        # 循环模式，可选指定秒数: python stock_monitor.py loop 60
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        run_loop(interval)
    else:
        # 单次查询
        run_once()
