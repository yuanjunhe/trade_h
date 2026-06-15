"""
A 股主板+创业板 成交量异动扫描工具（akshare 多线程版）。
依赖: pip install akshare pandas
"""

import json
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import akshare as ak
import numpy as np
import pandas as pd
import requests


# ═══════════════════════════════════════════════════════════
#  配置区 —— 修改参数只改这里
# ═══════════════════════════════════════════════════════════

# 检查期天数：检测最近 N 天是否放量（N 天中每天都要满足条件）
CHECK_DAYS = 2

# 基期天数：取最近 CHECK_DAYS 天之前的 N 天作为基期，用于逐日对比
AVG_DAYS = 22

# 放量倍数范围：检查期每天的成交量需大于基期对应天成交量的 MULTIPLIER_MIN 倍
MULTIPLIER_MIN = 2.1
MULTIPLIER_MAX = 0  # 0 表示不设上限

# 逐日对比通过率：检查期每天与基期逐一对比，至少需要多少比例的基期日满足倍数条件
# 例：0.8 表示只要战胜基期中 80% 的天数就算通过，可容忍 20% 的基期异常数据
PASS_RATIO = 0.85

# 查询历史数据时多取的自然日天数（用于覆盖周末/节假日，一般不用改）
EXTRA_DAYS = 10

# 扫描板块（True=扫描该板块，False=跳过）
SCAN_SH_MAIN = False    # 上证主板（600,601,603,605 等，排除 688/689 科创板）
SCAN_SZ_MAIN = False    # 深圳主板（000,001 开头）
SCAN_CHI = True         # 创业板（300,301 开头）

# 是否排除 ST / *ST 股票
EXCLUDE_ST = True

# 是否排除停牌股（最近 5 天成交量全为 0 的）
EXCLUDE_SUSPENDED = True

# 最低基期日均成交量（万手），过滤掉日均成交太低的冷门股
# 设为 0 表示不过滤；如设 100 表示基期日均至少 100 万手
MIN_AVG_VOLUME = 0

# 并发线程数
WORKERS = 15

# ── 策略开关 ──
ENABLE_VOLUME_SURGE = True   # 成交量异动策略
ENABLE_MA_BREAKOUT = True    # 放量突破均线策略

# ── 放量突破均线策略参数 ──
MA_SHORT = 5          # 短期均线天数
MA_LONG = 20          # 中期均线天数（趋势确认）
VOL_MULTIPLIER = 1.5  # 当日成交量需 ≥ 短期日均量的 N 倍

# ── 扫描缓存文件 ──
SCAN_CACHE_FILE = "db/scan_cache.json"  # 固定文件名，存放扫描命中的股票代码

# ── 历史数据本地缓存 ──
CACHE_DB = "db/stock_cache.db"          # SQLite 缓存，存放每只股票的日线数据
CACHE_MAX_AGE_DAYS = 4               # 缓存最大有效期，覆盖周末（周五→周一差3天）
CACHE_INCREMENTAL = True             # 缓存命中但数据不够新时，增量拉取缺失天数
EXCLUDE_TODAY_INTRADAY = True        # 盘中（15:00前）排除当天不完整数据

# ── Baostock 超时 ──
BAOSTOCK_TIMEOUT = 15                # Baostock 单股查询超时（秒），防止卡死
HTTP_TIMEOUT = 15                    # HTTP 数据源（akshare/新浪）超时（秒）


# 去除代理（macOS 系统代理会干扰 curl_cffi/libcurl 的连接）
for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"


# ═══════════════════════════════════════════════════════════
#  以下无需修改
# ═══════════════════════════════════════════════════════════


def _get_board_str() -> str:
    """根据配置的板块开关，生成板块名称字符串。

    返回示例：
      - 多个板块："上证主板+创业板"
      - 单个板块："创业板"
      - 都没开："无"
    """
    names = []
    if SCAN_SH_MAIN:
        names.append("上证主板")
    if SCAN_SZ_MAIN:
        names.append("深圳主板")
    if SCAN_CHI:
        names.append("创业板")
    return "+".join(names) if names else "无"


def _get_board_abbr() -> str:
    """根据配置的板块开关，生成板块英文缩写（用于文件名）。

    返回示例：
      - 多个板块："SH+CY"
      - 单个板块："CY"
      - 都没开："none"
    """
    abbrs = []
    if SCAN_SH_MAIN:
        abbrs.append("SH")
    if SCAN_SZ_MAIN:
        abbrs.append("SZ")
    if SCAN_CHI:
        abbrs.append("CY")
    return "+".join(abbrs) if abbrs else "none"


def _get_range_str() -> str:
    """根据配置的倍数范围，生成可读字符串。

    返回示例：
      - MULTIPLIER_MAX > 0："1.8-3.0"
      - MULTIPLIER_MAX = 0："1.8"（表示 1.8 倍及以上）
    """
    return f"{MULTIPLIER_MIN}-{MULTIPLIER_MAX}" if MULTIPLIER_MAX > 0 else f"{MULTIPLIER_MIN}"


def _to_sina_code(stock_code: str) -> str:
    """将纯数字股票代码转为新浪格式（sh600519 / sz300750）。"""
    return f"sh{stock_code}" if stock_code.startswith("6") else f"sz{stock_code}"


# ── 本地缓存层 ──────────────────────────────────────

_cache_lock = threading.Lock()
_bs_lock = threading.Lock()

# ── 线程本地连接池（每线程复用一个 SQLite 连接，避免重复 open/close） ──
_thread_local = threading.local()


def _get_cache_conn():
    """获取当前线程的缓存连接（懒初始化、复用）。"""
    conn = getattr(_thread_local, "cache_conn", None)
    if conn is None:
        conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_daily (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                PRIMARY KEY (code, date)
            )
        """)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        _thread_local.cache_conn = conn
    return conn

def _init_cache_db() -> sqlite3.Connection:
    """初始化 SQLite 缓存数据库。

    如果表不存在则创建，同时清理 60 天前的过期数据。
    """
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_daily (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("DELETE FROM stock_daily WHERE date < date('now', '-60 days')")
    conn.commit()
    return conn


def _get_cached_stock(code: str, min_date: str) -> pd.DataFrame | None:
    """从缓存读取某只股票指定日期起的日线数据。

    Args:
        code: 股票代码
        min_date: 起始日期 "YYYY-MM-DD"

    Returns:
        DataFrame[日期, 收盘, 成交量] 或 None。
    """
    conn = _get_cache_conn()
    cursor = conn.execute(
        "SELECT date, close, volume FROM stock_daily WHERE code = ? AND date >= ? ORDER BY date",
        (code, min_date),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    return pd.DataFrame(rows, columns=["日期", "收盘", "成交量"])


def _cache_stock_data(code: str, df: pd.DataFrame):
    """将单只股票的日线数据写入本地缓存。

    使用线程本地连接，避免多线程重复创建/关闭 SQLite 连接。
    """
    conn = _get_cache_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO stock_daily (code, date, close, volume) VALUES (?, ?, ?, ?)",
        [(code, str(row["日期"]), float(row["收盘"]), int(row["成交量"])) for _, row in df.iterrows()],
    )
    conn.commit()


def _call_with_timeout(fn, timeout, *args, **kwargs):
    """在 daemon 线程中执行 fn，主线程等待 timeout 秒后超时返回 None。

    用于保护无超时参数的第三方 HTTP 调用（akshare），防止网络卡死。
    """
    result = [None]
    error = [None]

    def _do():
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return None  # 超时
    if error[0] is not None:
        raise error[0]
    return result[0]


def _incremental_fetch(stock_code: str, from_date: str, to_date: str) -> pd.DataFrame | None:
    """增量拉取 from_date 到 to_date 之间的数据（快速通道，跳过 Baostock）。

    仅使用东方财富和新浪两个 HTTP 源，带超时保护，失败或超时返回 None。
    """
    # 第1层：东方财富（最快）
    try:
        df = _call_with_timeout(
            ak.stock_zh_a_hist, HTTP_TIMEOUT,
            symbol=stock_code, period="daily", adjust="",
            start_date=from_date.replace("-", ""), end_date=to_date.replace("-", ""),
        )
        if df is not None and not df.empty:
            return df[["日期", "收盘", "成交量"]].copy()
    except Exception:
        pass

    # 第2层：新浪
    try:
        sina_code = _to_sina_code(stock_code)
        delta = (datetime.strptime(to_date, "%Y-%m-%d") - datetime.strptime(from_date, "%Y-%m-%d")).days
        datalen = max(delta + 5, 10)
        url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": sina_code, "scale": "240", "ma": "no", "datalen": datalen}
        headers = {
            "Referer": "http://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }

        def _fetch_sina():
            r = requests.get(url, params=params, headers=headers, timeout=10)
            r.encoding = "gbk"
            return r.json()

        data = _call_with_timeout(_fetch_sina, HTTP_TIMEOUT)

        if data and isinstance(data, list):
            rows = [{
                "日期": item["day"],
                "收盘": float(item["close"]),
                "成交量": int(float(item["volume"])) // 100,
            } for item in data]
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df[(df["日期"] >= from_date) & (df["日期"] <= to_date)]
            return df if not df.empty else None
    except Exception:
        pass

    return None


def _filter_incomplete_today(df: pd.DataFrame) -> pd.DataFrame:
    """盘中过滤当天不完整的成交量数据。

    交易日 15:00 前，当天成交量还在累计中，不代表全天真实成交，
    直接参与放量检测会导致结果失真。此函数在盘中删掉当天行。

    非交易日或盘后（≥15:00）不做过滤。
    """
    if not EXCLUDE_TODAY_INTRADAY or df is None or df.empty:
        return df
    now = datetime.now()
    if now.weekday() < 5 and now.hour < 15:
        today_str = now.strftime("%Y-%m-%d")
        df = df[df["日期"] != today_str]
    return df


def _query_stock_hist(stock_code: str) -> pd.DataFrame | None:
    """查询单只股票的历史日线数据（Baostock → 东方财富 → 新浪）。

    策略:
        1. 优先 Baostock（TCP 直连，稳定可靠）
        2. 失败后降级到东方财富 (akshare)
        3. 再失败降级到新浪
        所有路径失败返回 None。

    返回:
        DataFrame，列名为 ["日期", "收盘", "成交量"]，按日期正序排列；
        成交量单位为"手"。
    """
    today = datetime.now()
    total_needed_days = AVG_DAYS + CHECK_DAYS + EXTRA_DAYS
    start = today - timedelta(days=total_needed_days)

    # ── 尝试从本地缓存读取 ──
    cached = _get_cached_stock(stock_code, start.strftime("%Y-%m-%d"))
    if cached is not None and len(cached) >= AVG_DAYS + CHECK_DAYS:
        latest_dt = datetime.strptime(str(cached["日期"].iloc[-1]), "%Y-%m-%d")
        if (today - latest_dt).days <= CACHE_MAX_AGE_DAYS:
            # 缓存命中，检查是否缺了最新交易日数据
            if CACHE_INCREMENTAL and latest_dt.date() < today.date():
                inc = _incremental_fetch(
                    stock_code,
                    (latest_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
                    today.strftime("%Y-%m-%d"),
                )
                if inc is not None and not inc.empty:
                    # 合并新旧数据，去重后写回缓存
                    # 确保日期列类型一致（akshare 可能返回 date 对象而非 str）
                    cached["日期"] = cached["日期"].astype(str)
                    inc["日期"] = inc["日期"].astype(str)
                    merged = pd.concat([cached, inc], ignore_index=True)
                    merged = merged.drop_duplicates(subset=["日期"], keep="last")
                    merged = merged.sort_values("日期").reset_index(drop=True)
                    _cache_stock_data(stock_code, inc)
                    return merged
            return cached

    # ── 第1层：Baostock（带超时，防止单股请求卡死拖垮所有线程） ──
    def _bs_query():
        """在 daemon 线程中执行 Baostock 查询，主线程超时即跳过。"""
        import baostock as bs
        prefix = "sh" if stock_code.startswith("6") else "sz"
        return bs.query_history_k_data_plus(
            f"{prefix}.{stock_code}",
            "date,close,volume",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=today.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="2",
        )

    with _bs_lock:
        try:
            _result = [None]
            _error = [None]

            def _do():
                try:
                    _result[0] = _bs_query()
                except Exception as e:
                    _error[0] = e

            t = threading.Thread(target=_do, daemon=True)
            t.start()
            t.join(timeout=BAOSTOCK_TIMEOUT)

            if t.is_alive():
                pass  # 超时，跳过该股票，daemon 线程自行结束
            elif _error[0] is not None:
                raise _error[0]
            else:
                rs = _result[0]
                rows = []
                while rs.next():
                    row = rs.get_row_data()
                    if row[0] is None or row[0] == "":
                        continue
                    rows.append({
                        "日期": row[0],
                        "收盘": float(row[1]),
                        "成交量": int(float(row[2])) // 100,  # 股 → 手
                    })

                if rows:
                    df = pd.DataFrame(rows)
                    _cache_stock_data(stock_code, df)
                    return df
        except Exception:
            pass

    # ── 第2层：东方财富 ──
    try:
        df = _call_with_timeout(
            ak.stock_zh_a_hist, HTTP_TIMEOUT,
            symbol=stock_code, period="daily", adjust="",
            start_date=start.strftime("%Y%m%d"), end_date=today.strftime("%Y%m%d"),
        )
        if df is not None and not df.empty:
            df = df[["日期", "收盘", "成交量"]].copy()
            _cache_stock_data(stock_code, df)
            return df
    except Exception:
        pass

    # ── 第3层：新浪 ──
    try:
        sina_code = _to_sina_code(stock_code)
        datalen = AVG_DAYS + CHECK_DAYS + EXTRA_DAYS
        url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": sina_code, "scale": "240", "ma": "no", "datalen": datalen}
        headers = {
            "Referer": "http://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }

        def _fetch_sina():
            r = requests.get(url, params=params, headers=headers, timeout=10)
            r.encoding = "gbk"
            return r.json()

        data = _call_with_timeout(_fetch_sina, HTTP_TIMEOUT)

        if data and isinstance(data, list):
            rows = [{
                "日期": item["day"],
                "收盘": float(item["close"]),
                "成交量": int(float(item["volume"])) // 100,
            } for item in data]

            df = pd.DataFrame(rows)
            if not df.empty:
                _cache_stock_data(stock_code, df)
                return df
    except Exception:
        pass

    return None


def check_volume_surge(df: pd.DataFrame) -> dict | None:
    """检测单只股票的成交量是否放量。

    参数:
        df: 历史日线数据，列名为 ["日期", "成交量"]，按日期正序排列。

    检测逻辑:
        1. 从 df 中取最近 (AVG_DAYS + CHECK_DAYS) 个交易日。
        2. 前 AVG_DAYS 天作为"基期"，后 CHECK_DAYS 天作为"检查期"。
        3. 检查期每一天，与基期每一天做逐日对比（numpy 向量化）：
           比值 = 检查期该日成交量 / 基期各日成交量
           统计比值在 [MULTIPLIER_MIN, MULTIPLIER_MAX) 范围内的基期天数。
        4. 如果满足条件的基期天数占比 >= PASS_RATIO，则该检查日判定为放量。
        5. 检查期所有天都放量，才算该股票整体触发信号。

    返回:
        dict，包含以下字段：
          - surged (bool): 是否整体触发信号
          - avg_volume (float): 基期日均成交量
          - threshold_min (float): 放量下限（avg_volume × MULTIPLIER_MIN）
          - threshold_max (float|None): 放量上限（MULTIPLIER_MAX=0 时为 None）
          - recent (list[dict]): 检查期每天的结果，包含：
              date, volume, ratio(中位倍数), pass_count, total_count, surged
        数据不足时返回 None。
    """
    total_needed = AVG_DAYS + CHECK_DAYS

    if df is None or len(df) < total_needed:
        return None

    # 取最近 total_needed 条
    df = df.tail(total_needed).reset_index(drop=True)

    base_volumes = df.iloc[:AVG_DAYS]["成交量"].to_numpy(dtype=np.float64)
    recent_df = df.iloc[AVG_DAYS:]

    # 过滤全 0 基期
    valid_mask = base_volumes > 0
    valid_bases = base_volumes[valid_mask]
    valid_base_count = len(valid_bases)
    if valid_base_count == 0:
        return None

    # 过滤日均成交太低的股票
    avg_vol = base_volumes.mean()
    if MIN_AVG_VOLUME > 0 and avg_vol / 10000 < MIN_AVG_VOLUME:
        return None

    # 向量化逐日对比：recent_vols (n_recent, 1) / valid_bases (1, n_valid) -> (n_recent, n_valid)
    recent_vols = recent_df["成交量"].to_numpy(dtype=np.float64)
    ratios_matrix = recent_vols[:, np.newaxis] / valid_bases[np.newaxis, :]

    # 每天计算满足条件的基期日数量
    if MULTIPLIER_MAX > 0:
        pass_matrix = (ratios_matrix >= MULTIPLIER_MIN) & (ratios_matrix < MULTIPLIER_MAX)
    else:
        pass_matrix = ratios_matrix >= MULTIPLIER_MIN

    pass_counts = pass_matrix.sum(axis=1)
    required_pass = int(valid_base_count * PASS_RATIO)
    day_ok_flags = pass_counts >= required_pass

    # 中位数倍数
    median_ratios = np.median(ratios_matrix, axis=1)

    results = []
    for i in range(CHECK_DAYS):
        results.append(dict(
            date=recent_df.iloc[i]["日期"],
            volume=int(recent_vols[i]),
            ratio=round(float(median_ratios[i]), 2),
            pass_count=int(pass_counts[i]),
            total_count=valid_base_count,
            surged=bool(day_ok_flags[i]),
        ))

    all_surged = all(day_ok_flags)

    return dict(
        surged=all_surged,
        avg_volume=round(float(avg_vol), 2),
        threshold_min=round(avg_vol * MULTIPLIER_MIN, 2),
        threshold_max=round(avg_vol * MULTIPLIER_MAX, 2) if MULTIPLIER_MAX > 0 else None,
        recent=results,
    )


def check_ma_breakout(df: pd.DataFrame) -> dict | None:
    """检测单只股票是否放量突破均线。

    参数:
        df: 历史日线数据，列名为 ["日期", "收盘", "成交量"]，按日期正序排列。

    检测逻辑:
        1. 取最近 (MA_LONG + 1) 个交易日，计算短期均线和中期均线。
        2. 当日收盘 > 当日短期均线（价格突破短期均线）。
        3. 前一日收盘 ≤ 前一日短期均线（首次突破，排除一直在均线上方的股票）。
        4. 当日收盘 > 当日中期均线（中期趋势向上确认）。
        5. 当日成交量 ≥ 短期日均量 × VOL_MULTIPLIER（放量确认）。

    返回:
        dict，包含以下字段：
          - surged (bool): 是否触发信号
          - close (float): 当日收盘价
          - ma_short (float): 短期均线值
          - ma_long (float): 中期均线值
          - volume (int): 当日成交量
          - avg_volume (float): 短期日均成交量
          - vol_ratio (float): 量比（当日成交量 / 短期日均量）
          - date: 当日日期
        数据不足时返回 None。
    """
    needed = MA_LONG + 1

    if df is None or len(df) < needed:
        return None

    # 取最近 needed 条
    df = df.tail(needed).reset_index(drop=True)

    closes = df["收盘"].to_numpy(dtype=np.float64)
    volumes = df["成交量"].to_numpy(dtype=np.float64)

    # 当日和前一日索引
    today_idx = -1
    yesterday_idx = -2

    # 计算短期均线（含当日）
    ma_short_today = np.mean(closes[-MA_SHORT:])
    # 计算前一日短期均线（不含当日，取前 MA_SHORT 天）
    ma_short_yesterday = np.mean(closes[-MA_SHORT - 1:-1])

    # 计算中期均线（含当日）
    ma_long_today = np.mean(closes[-MA_LONG:])

    # 当日收盘和成交量
    close_today = closes[today_idx]
    close_yesterday = closes[yesterday_idx]
    vol_today = volumes[today_idx]

    # 短期日均量（含当日在内的 MA_SHORT 天）
    avg_vol = np.mean(volumes[-MA_SHORT:])

    # 过滤成交量为 0 的情况
    if avg_vol <= 0 or vol_today <= 0:
        return None

    vol_ratio = vol_today / avg_vol

    # 条件判定
    breakout = close_today > ma_short_today          # 突破短期均线
    first_cross = close_yesterday <= ma_short_yesterday  # 前一日未突破（首次穿越）
    trend_up = close_today > ma_long_today           # 中期趋势向上
    volume_surge = vol_ratio >= VOL_MULTIPLIER       # 放量确认

    surged = breakout and first_cross and trend_up and volume_surge

    if not surged:
        return dict(
            surged=False,
            close=round(float(close_today), 2),
            ma_short=round(float(ma_short_today), 2),
            ma_long=round(float(ma_long_today), 2),
            volume=int(vol_today),
            avg_volume=round(float(avg_vol), 2),
            vol_ratio=round(float(vol_ratio), 2),
            date=df.iloc[today_idx]["日期"],
            _breakout=breakout,
            _first_cross=first_cross,
            _trend_up=trend_up,
            _volume_surge=volume_surge,
        )

    return dict(
        surged=True,
        close=round(float(close_today), 2),
        ma_short=round(float(ma_short_today), 2),
        ma_long=round(float(ma_long_today), 2),
        volume=int(vol_today),
        avg_volume=round(float(avg_vol), 2),
        vol_ratio=round(float(vol_ratio), 2),
        date=df.iloc[today_idx]["日期"],
    )


# ── 线程 worker ─────────────────────────────────────────

def _worker(stock: dict) -> dict | None:
    """多线程扫描单只股票（线程池 worker 函数）。

    参数:
        stock: dict，包含 "code"（股票代码）和 "name"（股票名称）。

    流程:
        1. 排除 ST / *ST 股票（如果 EXCLUDE_ST=True）。
        2. 查询历史日线数据。
        3. 排除停牌股：最近 5 天成交量全为 0（如果 EXCLUDE_SUSPENDED=True）。
        4. 根据策略开关，分别调用对应的检测函数。

    返回:
        任一策略触发时返回 dict，包含：
          - code, name: 股票信息
          - strategies (list[str]): 触发的策略名称列表
          - volume_surge (dict|省略): 成交量异动结果
          - ma_breakout (dict|省略): 放量突破均线结果
        所有策略均未触发或数据异常时返回 None。
    """
    if EXCLUDE_ST and ("ST" in stock["name"] or "*ST" in stock["name"]):
        return None

    # 至少一个策略开启才查数据
    if not (ENABLE_VOLUME_SURGE or ENABLE_MA_BREAKOUT):
        return None

    try:
        df = _query_stock_hist(stock["code"])
        if df is None or df.empty:
            return None

        # 盘中过滤当天不完整数据，过滤后数据不足则跳过
        df = _filter_incomplete_today(df)
        if df is None or df.empty or len(df) < AVG_DAYS + CHECK_DAYS:
            return None

        # 排除停牌股（最近 5 天成交量全为 0）
        if EXCLUDE_SUSPENDED and len(df) >= 5:
            if (df.tail(5)["成交量"] == 0).all():
                return None

        hit = dict(code=stock["code"], name=stock["name"], strategies=[])

        # 成交量异动策略
        if ENABLE_VOLUME_SURGE:
            r = check_volume_surge(df)
            if r and r["surged"]:
                hit["volume_surge"] = r
                hit["strategies"].append("volume_surge")

        # 放量突破均线策略
        if ENABLE_MA_BREAKOUT:
            r = check_ma_breakout(df)
            if r and r["surged"]:
                hit["ma_breakout"] = r
                hit["strategies"].append("ma_breakout")

        if hit["strategies"]:
            return hit

    except Exception as e:
        print(f"  [!] {stock['code']} {stock['name']}: {e}")

    return None


# ── 获取股票列表 ─────────────────────────────────────────

def get_all_stocks() -> list[dict]:
    """从 akshare 获取全 A 股股票列表，按配置的板块过滤。

    返回:
        list[dict]，每个 dict 包含 "code"（纯数字，如 "300750"）和 "name"（股票名称）。
        过滤后的结果只包含配置中开启的板块（SCAN_SH_MAIN / SCAN_SZ_MAIN / SCAN_CHI）。
        获取失败时返回空列表。
    """
    try:
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            return []

        code_col = df["code"].astype(str)
        name_col = df["name"].astype(str)

        is_sh = code_col.str.startswith("6") & ~code_col.str.startswith("688") & ~code_col.str.startswith("689")
        is_sz = code_col.str.startswith("00") | code_col.str.startswith("001")
        is_chi = code_col.str.startswith("30")

        mask = (
            (is_sh & SCAN_SH_MAIN)
            | (is_sz & SCAN_SZ_MAIN)
            | (is_chi & SCAN_CHI)
        )

        filtered = df[mask]
        return [{"code": c, "name": n} for c, n in zip(filtered["code"], filtered["name"])]
    except Exception as e:
        print(f"获取股票列表失败: {e}")
        return []


# ── 扫描入口 ─────────────────────────────────────────────

def scan_all_stocks() -> dict:
    """扫描目标板块所有股票，多线程并行执行。

    流程:
        1. 调用 get_all_stocks() 获取板块内所有股票。
        2. 打印扫描参数（板块、策略、倍数、通过率等）。
        3. 使用 ThreadPoolExecutor 多线程并行调用 _worker。
        4. 每 200 只打印一次进度。
        5. 汇总结果，按策略分类返回。

    返回:
        dict，包含：
          - "volume_surge": 成交量异动命中列表，按最大倍数降序
          - "ma_breakout": 放量突破均线命中列表，按量比降序
    """
    stocks = get_all_stocks()
    if not stocks:
        return {"volume_surge": [], "ma_breakout": []}

    board_str = _get_board_str()
    range_str = _get_range_str()

    strategy_names = []
    if ENABLE_VOLUME_SURGE:
        strategy_names.append("成交量异动")
    if ENABLE_MA_BREAKOUT:
        strategy_names.append("放量突破均线")

    print(f"板块: {board_str}（共 {len(stocks)} 只股票）")
    print(f"策略: {' + '.join(strategy_names)}")
    if ENABLE_VOLUME_SURGE:
        print(f"  [成交量异动] 最近 {CHECK_DAYS} 天 vs 过去 {AVG_DAYS} 天，"
              f"倍数 {range_str}，通过率 >= {PASS_RATIO*100:.0f}%")
    if ENABLE_MA_BREAKOUT:
        print(f"  [放量突破均线] 突破 {MA_SHORT} 日均线，"
              f"{MA_LONG} 日均线趋势向上，量比 >= {VOL_MULTIPLIER}")
    if EXCLUDE_ST:
        print("      排除 ST / *ST 股票")
    if MIN_AVG_VOLUME > 0:
        print(f"      最低日均成交: {MIN_AVG_VOLUME} 万手")
    print(f"线程数: {WORKERS}")
    print()

    # Baostock 全局登录（线程共享连接，串行查询）
    _bs_ready = False
    try:
        import baostock as bs
        r = bs.login()
        _bs_ready = r.error_code == "0"
        if not _bs_ready:
            print(f"Baostock 登录失败: {r.error_msg}")
    except Exception as e:
        print(f"Baostock 不可用: {e}")

    hits = []
    total = len(stocks)
    done = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_worker, s): s for s in stocks}
        for future in as_completed(futures):
            done += 1
            if done % 200 == 0 or done == total:
                print(f"  进度: {done}/{total}")

            try:
                result = future.result()
                if result:
                    hits.append(result)
            except Exception:
                errors += 1

    # 关闭 Baostock 全局连接
    if _bs_ready:
        try:
            bs.logout()
        except Exception:
            pass

    # 清理线程本地 SQLite 连接
    conn = getattr(_thread_local, "cache_conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
        del _thread_local.cache_conn

    if errors > 0:
        print(f"异常数: {errors}")

    # 按策略分类
    surge_hits = [h for h in hits if "volume_surge" in h["strategies"]]
    ma_hits = [h for h in hits if "ma_breakout" in h["strategies"]]

    # 排序
    surge_hits.sort(key=lambda x: max(r["ratio"] for r in x["volume_surge"]["recent"]), reverse=True)
    ma_hits.sort(key=lambda x: x["ma_breakout"]["vol_ratio"], reverse=True)

    return {"volume_surge": surge_hits, "ma_breakout": ma_hits}


# ── 格式化工具 ───────────────────────────────────────────

def format_volume(vol: float) -> str:
    """将成交量数值格式化为可读字符串。

    参数:
        vol: 成交量，单位"万手"。

    返回:
        格式化后的字符串。例：
          - 12345 -> "1.23 亿手"
          - 45.67 -> "45.67 万手"
    """
    if vol >= 10_000:
        return f"{vol / 10_000:.2f} 亿手"
    return f"{vol:.2f} 万手"


def _format_hit_row(h: dict) -> str:
    """格式化单只命中股票的一行表格输出（成交量异动策略）。

    参数:
        h: 命中的股票信息 dict，需包含 volume_surge 子字段。

    返回:
        固定宽度的格式化字符串，取检查期中 ratio 最大的那天展示。
    """
    vs = h["volume_surge"]
    r = max(vs["recent"], key=lambda x: x["ratio"])
    return (f"{h['code']:<8} {h['name']:<10} "
            f"{r['date']!s:<12} {format_volume(r['volume']):<14} "
            f"{format_volume(vs['avg_volume']):<12} {r['ratio']:<8.2f}")


def _format_ma_row(h: dict) -> str:
    """格式化单只命中股票的一行表格输出（放量突破均线策略）。

    参数:
        h: 命中的股票信息 dict，需包含 ma_breakout 子字段。

    返回:
        固定宽度的格式化字符串。
    """
    mb = h["ma_breakout"]
    return (f"{h['code']:<8} {h['name']:<10} "
            f"{mb['date']!s:<12} {mb['close']:<10.2f} "
            f"{mb['ma_short']:<10.2f} {mb['ma_long']:<10.2f} "
            f"{mb['vol_ratio']:<8.2f}")


def _print_results(results: dict, board_str: str, range_str: str):
    """将扫描结果打印到终端。

    参数:
        results: 按策略分类的命中结果 dict，包含：
          - "volume_surge": 成交量异动命中列表
          - "ma_breakout": 放量突破均线命中列表
        board_str: 板块名称
        range_str: 倍数范围字符串

    行为:
        - 每个策略独立一个表格区域展示
        - 无命中时打印对应提示
    """
    surge_hits = results["volume_surge"]
    ma_hits = results["ma_breakout"]

    # ── 成交量异动 ──
    if ENABLE_VOLUME_SURGE:
        print(f"\n{'='*72}")
        if surge_hits:
            print(f" 【成交量异动】发现 {len(surge_hits)} 只股票")
            print(f" 板块: {board_str}")
            print(f" 条件: 最近 {CHECK_DAYS} 天放量倍数在 {range_str} 范围")
            print(f"{'='*72}\n")
            print(f"{'代码':<8} {'名称':<10} {'日期':<12} {'成交量':<14} {'基准均值':<12} {'倍数':<8}")
            print("-" * 64)
            for h in surge_hits:
                print(_format_hit_row(h))
            print(f"\n成交量异动：共 {len(surge_hits)} 只股票触发信号。")
        else:
            print(f"\n{'='*72}")
            print(" 【成交量异动】未发现成交量异动股票。")
            print(f" 板块: {board_str}")
            print(f" 条件: 最近 {CHECK_DAYS} 天放量倍数在 {range_str} 范围")
            print(f"{'='*72}")

    # ── 放量突破均线 ──
    if ENABLE_MA_BREAKOUT:
        print(f"\n{'='*72}")
        if ma_hits:
            print(f" 【放量突破均线】发现 {len(ma_hits)} 只股票")
            print(f" 板块: {board_str}")
            print(f" 条件: 突破 {MA_SHORT} 日均线 + {MA_LONG} 日均线向上 + 量比 >= {VOL_MULTIPLIER}")
            print(f"{'='*72}\n")
            print(f"{'代码':<8} {'名称':<10} {'日期':<12} {'收盘价':<10} {'MA{MA_SHORT}':<10} {'MA{MA_LONG}':<10} {'量比':<8}".replace("{MA_SHORT}", str(MA_SHORT)).replace("{MA_LONG}", str(MA_LONG)))
            print("-" * 68)
            for h in ma_hits:
                print(_format_ma_row(h))
            print(f"\n放量突破均线：共 {len(ma_hits)} 只股票触发信号。")
        else:
            print(" 【放量突破均线】未发现放量突破均线股票。")
            print(f" 板块: {board_str}")
            print(f" 条件: 突破 {MA_SHORT} 日均线 + {MA_LONG} 日均线向上 + 量比 >= {VOL_MULTIPLIER}")
            print(f"{'='*72}")


def _save_results(results: dict, board_str: str, range_str: str):
    """将扫描结果保存到文本文件。

    参数:
        results: 按策略分类的命中结果 dict。
        board_str: 板块名称
        range_str: 倍数范围字符串

    文件名规则:
        scan_{板块名称}_{日期时间}.txt

    文件内容:
        包含扫描参数和各策略的结果表格，无论有无命中都会生成文件。
    """
    surge_hits = results["volume_surge"]
    ma_hits = results["ma_breakout"]

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    board_abbr = _get_board_abbr()
    os.makedirs("scanlog", exist_ok=True)
    filename = f"scanlog/scan_{board_abbr}_{now_str}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("股票策略扫描结果\n")
        f.write(f"扫描日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"板块: {board_str}\n")
        f.write(f"排除 ST: {EXCLUDE_ST}\n")
        f.write(f"排除停牌: {EXCLUDE_SUSPENDED}\n")
        f.write(f"最低日均成交: {MIN_AVG_VOLUME} 万手\n")
        f.write(f"{'='*72}\n")

        # 成交量异动
        if ENABLE_VOLUME_SURGE:
            f.write(f"\n【成交量异动】\n")
            f.write(f"参数: 最近 {CHECK_DAYS} 天 vs 过去 {AVG_DAYS} 天均值\n")
            f.write(f"放量倍数: {range_str}\n")
            f.write(f"通过率阈值: {PASS_RATIO*100:.0f}%\n")
            if surge_hits:
                f.write(f"{'代码':<8} {'名称':<10} {'日期':<12} {'成交量':<14} {'基准均值':<12} {'倍数':<8}\n")
                f.write("-" * 64 + "\n")
                for h in surge_hits:
                    f.write(_format_hit_row(h) + "\n")
            f.write(f"共 {len(surge_hits)} 只股票触发信号。\n")

        # 放量突破均线
        if ENABLE_MA_BREAKOUT:
            f.write(f"\n【放量突破均线】\n")
            f.write(f"参数: 突破 {MA_SHORT} 日均线 + {MA_LONG} 日均线向上 + 量比 >= {VOL_MULTIPLIER}\n")
            if ma_hits:
                f.write(f"{'代码':<8} {'名称':<10} {'日期':<12} {'收盘价':<10} {'MA'+str(MA_SHORT):<10} {'MA'+str(MA_LONG):<10} {'量比':<8}\n")
                f.write("-" * 68 + "\n")
                for h in ma_hits:
                    f.write(_format_ma_row(h) + "\n")
            f.write(f"共 {len(ma_hits)} 只股票触发信号。\n")

    print(f"结果已保存到: {filename}")


def _save_cache(results: dict):
    """将扫描命中的股票代码写入缓存文件，供实时监控程序读取。

    参数:
        results: 按策略分类的命中结果 dict。

    文件格式 (JSON):
        {
            "scan_time": "2026-06-10 14:58:00",
            "volume_surge": [{"code": "300197", "name": "节能铁汉"}, ...],
            "ma_breakout": [{"code": "301337", "name": "亚华电子"}, ...]
        }

    行为:
        每次调用覆盖写入，文件固定为 SCAN_CACHE_FILE。
    """
    cache = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "volume_surge": [
            {"code": h["code"], "name": h["name"]}
            for h in results["volume_surge"]
        ],
        "ma_breakout": [
            {"code": h["code"], "name": h["name"]}
            for h in results["ma_breakout"]
        ],
    }
    with open(SCAN_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    total = len(cache["volume_surge"]) + len(cache["ma_breakout"])
    print(f"缓存已更新: {SCAN_CACHE_FILE}（共 {total} 只股票）")


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "single"

    if mode == "scan":
        results = scan_all_stocks()
        board_str = _get_board_str()
        range_str = _get_range_str()
        _print_results(results, board_str, range_str)
        _save_results(results, board_str, range_str)
        _save_cache(results)

    else:
        code = sys.argv[2] if len(sys.argv) > 2 else "300750"
        rows = _query_stock_hist(code)
        if rows is not None and not rows.empty:
            rows = _filter_incomplete_today(rows)

        if rows is None or rows.empty:
            print(f"获取股票 {code} 数据失败。")
        else:
            # 成交量异动策略
            if ENABLE_VOLUME_SURGE:
                result = check_volume_surge(rows)
                if result:
                    range_str = _get_range_str()
                    print(f"\n=== 股票 {code} 成交量异动检测 ===")
                    print(f"  基准均值: {format_volume(result['avg_volume'])}")
                    max_vol_str = format_volume(result['threshold_max']) if result['threshold_max'] else "无上限"
                    print(f"  放量范围 ({range_str}倍): "
                          f"{format_volume(result['threshold_min'])} ~ {max_vol_str}")
                    for r in result["recent"]:
                        tag = "[是]" if r["surged"] else "[否]"
                        print(f"  {tag} {r['date']}  {r['volume']/10000:.2f} 万手 = 中位倍数 {r['ratio']}x "
                              f"(通过 {r['pass_count']}/{r['total_count']} 天)")
                    print(f"  结论: {'★ 放量信号' if result['surged'] else '无放量信号'}")
                else:
                    print(f"\n=== 股票 {code} 成交量异动检测 ===")
                    print("  数据不足，无法检测。")

            # 放量突破均线策略
            if ENABLE_MA_BREAKOUT:
                result = check_ma_breakout(rows)
                if result:
                    print(f"\n=== 股票 {code} 放量突破均线检测 ===")
                    print(f"  当日收盘: {result['close']:.2f}")
                    print(f"  {MA_SHORT}日均线: {result['ma_short']:.2f}  "
                          f"{MA_LONG}日均线: {result['ma_long']:.2f}")
                    print(f"  当日成交量: {result['volume']/10000:.2f} 万手  "
                          f"短期均量: {result['avg_volume']/10000:.2f} 万手  "
                          f"量比: {result['vol_ratio']:.2f}x")
                    if result["surged"]:
                        print(f"  结论: ★ 放量突破均线信号")
                    else:
                        # 显示各条件是否满足
                        reasons = []
                        if not result.get("_breakout", True):
                            reasons.append("未突破短期均线")
                        if not result.get("_first_cross", True):
                            reasons.append("非首次突破（已在均线上方）")
                        if not result.get("_trend_up", True):
                            reasons.append("中期均线趋势未向上")
                        if not result.get("_volume_surge", True):
                            reasons.append(f"量比 {result['vol_ratio']:.2f}x < {VOL_MULTIPLIER}x（放量不足）")
                        print(f"  结论: 无信号（{', '.join(reasons)}）")
                else:
                    print(f"\n=== 股票 {code} 放量突破均线检测 ===")
                    print("  数据不足，无法检测。")
