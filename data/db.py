"""
数据库：建表、线程本地连接池、增删改查。
"""

import os
import sqlite3
import threading
from datetime import datetime
from typing import Any

import pandas as pd


# 线程本地存储：每个线程复用同一个 SQLite 连接
_thread_local = threading.local()

# 策略类名 → 短 key 映射（兼容原 JSON 格式）
_STRATEGY_KEY_MAP = {
    "VolumeSurgeStrategy": "volume_surge",
    "MABreakoutStrategy": "ma_breakout",
    "MACrossoverStrategy": "ma_crossover",
    "DivergenceStrategy": "divergence",
    "MomentumStrategy": "momentum",
}


def _strategy_short_key(strategy_name: str) -> str:
    """策略全名 → 短 key，未知策略取小写。"""
    return _STRATEGY_KEY_MAP.get(strategy_name, strategy_name.lower())


class Database:
    """SQLite 连接管理器，支持线程本地连接复用。

    用法:
        db = Database("db/stock_data.db")
        db.init_schema()
        conn = db.get_conn()
        conn.execute("SELECT ...")
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    def get_conn(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接（懒初始化，复用）。"""
        conn = getattr(_thread_local, "db_conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA journal_mode={self._journal_mode()}")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-8000")  # 8MB 缓存
            conn.execute("PRAGMA busy_timeout=5000")
            _thread_local.db_conn = conn
        return conn

    def _journal_mode(self) -> str:
        """日志模式：优先 WAL。"""
        return "wal"

    def init_schema(self):
        """创建所有表和索引（幂等操作，重复执行无副作用）。"""
        conn = self.get_conn()
        conn.executescript("""
            -- 日K线主表：完整 OHLCV + 前复权价格
            CREATE TABLE IF NOT EXISTS stock_daily (
                code        TEXT    NOT NULL,   -- 股票代码，如 "300750"
                date        TEXT    NOT NULL,   -- 日期 "YYYY-MM-DD"
                open        REAL    NOT NULL,   -- 开盘价
                high        REAL    NOT NULL,   -- 最高价
                low         REAL    NOT NULL,   -- 最低价
                close       REAL    NOT NULL,   -- 收盘价
                volume      REAL    NOT NULL,   -- 成交量（手）
                amount      REAL    NOT NULL,   -- 成交额（元）
                turnover    REAL    DEFAULT 0.0,  -- 换手率（%）
                amplitude   REAL    DEFAULT 0.0,  -- 振幅（%）
                pct_change  REAL    DEFAULT 0.0,  -- 涨跌幅（%）
                adj_open    REAL,                -- 前复权开盘价
                adj_high    REAL,                -- 前复权最高价
                adj_low     REAL,                -- 前复权最低价
                adj_close   REAL,                -- 前复权收盘价
                PRIMARY KEY (code, date)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_code
                ON stock_daily(code);
            CREATE INDEX IF NOT EXISTS idx_daily_date
                ON stock_daily(date);
            CREATE INDEX IF NOT EXISTS idx_daily_code_date
                ON stock_daily(code, date);

            -- 股票元数据表
            CREATE TABLE IF NOT EXISTS stock_info (
                code        TEXT PRIMARY KEY,    -- 股票代码
                name        TEXT NOT NULL,       -- 股票名称
                market      TEXT,                -- 市场：SH / SZ
                board       TEXT,                -- 板块：主板 / 创业板 / 科创板 / 北交所
                industry    TEXT,                -- 行业分类
                list_date   TEXT,                -- 上市日期
                is_st       INTEGER DEFAULT 0,   -- 是否ST
                updated_at  TEXT NOT NULL        -- 最后更新时间
            );

            CREATE INDEX IF NOT EXISTS idx_info_board
                ON stock_info(board);
            CREATE INDEX IF NOT EXISTS idx_info_market
                ON stock_info(market);

            -- 下载状态表：支持断点续传
            CREATE TABLE IF NOT EXISTS download_state (
                code         TEXT PRIMARY KEY,    -- 股票代码
                last_date    TEXT,                -- 最新数据日期
                total_rows   INTEGER DEFAULT 0,   -- 已下载行数
                status       TEXT DEFAULT 'pending',  -- pending/done/error
                error_msg    TEXT,                -- 错误信息
                updated_at   TEXT NOT NULL        -- 最后更新时间
            );

            CREATE INDEX IF NOT EXISTS idx_state_status
                ON download_state(status);

            -- 扫描结果缓存表：桥接 scan ↔ monitor
            CREATE TABLE IF NOT EXISTS scan_cache (
                scan_time   TEXT    NOT NULL,   -- 扫描时间 "YYYY-MM-DDTHH:MM:SS"
                strategy    TEXT    NOT NULL,   -- 策略名 "VolumeSurgeStrategy" / "MABreakoutStrategy" / ...
                code        TEXT    NOT NULL,   -- 股票代码
                name        TEXT    NOT NULL,   -- 股票名称
                PRIMARY KEY (scan_time, strategy, code)
            );

            CREATE INDEX IF NOT EXISTS idx_scan_time
                ON scan_cache(scan_time);
        """)
        conn.commit()

    # ── stock_daily 增删改查 ─────────────────────

    def insert_daily_batch(self, code: str, df: pd.DataFrame):
        """批量插入或替换一只股票的日线数据。

        参数:
            code: 股票代码，如 "300750"。
            df: DataFrame，列名需匹配 akshare 返回格式。
        """
        conn = self.get_conn()
        # DataFrame 列名 → 数据库列名 映射
        col_map = {
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
            "换手率": "turnover",
            "振幅": "amplitude",
            "涨跌幅": "pct_change",
        }
        # 字符串列，不需要转为 float
        _str_columns = {"code", "date"}

        rows = []
        for _, row in df.iterrows():
            r: dict[str, Any] = {"code": code}
            for df_col, db_col in col_map.items():
                if df_col in df.columns:
                    val = row[df_col]
                    if db_col in _str_columns:
                        r[db_col] = str(val)
                    else:
                        if val is None or val == "" or val == "-":
                            val = 0.0
                        r[db_col] = float(val)
            # 缺失列填默认值
            r.setdefault("turnover", 0.0)
            r.setdefault("amplitude", 0.0)
            r.setdefault("pct_change", 0.0)
            # 前复权价格：若数据源已提供则直接使用，否则复制原始价格
            for adj_col, raw_col in [
                ("adj_open", "open"),
                ("adj_high", "high"),
                ("adj_low", "low"),
                ("adj_close", "close"),
            ]:
                r[adj_col] = r.get(adj_col, r[raw_col])
            rows.append(r)

        conn.executemany("""
            INSERT OR REPLACE INTO stock_daily
                (code, date, open, high, low, close, volume, amount,
                 turnover, amplitude, pct_change, adj_open, adj_high, adj_low, adj_close)
            VALUES
                (:code, :date, :open, :high, :low, :close, :volume, :amount,
                 :turnover, :amplitude, :pct_change, :adj_open, :adj_high, :adj_low, :adj_close)
        """, rows)
        conn.commit()

    def get_daily(self, code: str, min_date: str | None = None,
                  columns: list[str] | None = None) -> pd.DataFrame:
        """查询一只股票的日线数据。

        参数:
            code: 股票代码。
            min_date: 起始日期（含），"YYYY-MM-DD"。
            columns: 要查询的列。默认全部。

        返回:
            DataFrame，按日期升序排列；无数据时返回空 DataFrame。
        """
        conn = self.get_conn()
        cols = ", ".join(columns) if columns else "*"
        if min_date:
            sql = f"SELECT {cols} FROM stock_daily WHERE code = ? AND date >= ? ORDER BY date"
            rows = conn.execute(sql, (code, min_date)).fetchall()
        else:
            sql = f"SELECT {cols} FROM stock_daily WHERE code = ? ORDER BY date"
            rows = conn.execute(sql, (code,)).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])

    def get_latest_date(self, code: str) -> str | None:
        """获取某只股票的最新数据日期。无数据时返回 None。"""
        conn = self.get_conn()
        row = conn.execute(
            "SELECT MAX(date) as max_date FROM stock_daily WHERE code = ?",
            (code,),
        ).fetchone()
        if row and row["max_date"]:
            return row["max_date"]
        return None

    def get_date_range(self, code: str) -> tuple[str | None, str | None]:
        """获取某只股票的数据日期范围 (最早日期, 最晚日期)。"""
        conn = self.get_conn()
        row = conn.execute(
            "SELECT MIN(date) as min_date, MAX(date) as max_date, COUNT(*) as cnt "
            "FROM stock_daily WHERE code = ?",
            (code,),
        ).fetchone()
        if row and row["cnt"] > 0:
            return row["min_date"], row["max_date"]
        return None, None

    def count_stocks_with_data(self) -> int:
        """统计 stock_daily 中有多少只不同的股票。"""
        conn = self.get_conn()
        row = conn.execute(
            "SELECT COUNT(DISTINCT code) as cnt FROM stock_daily"
        ).fetchone()
        return row["cnt"] if row else 0

    def total_rows(self) -> int:
        """stock_daily 总行数。"""
        conn = self.get_conn()
        row = conn.execute("SELECT COUNT(*) as cnt FROM stock_daily").fetchone()
        return row["cnt"] if row else 0

    # ── stock_info 增删改查 ──────────────────────

    def upsert_stock_info(self, code: str, name: str, market: str = "",
                          board: str = "", industry: str = "",
                          list_date: str = "", is_st: int = 0):
        """插入或更新一只股票的元数据。"""
        conn = self.get_conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT OR REPLACE INTO stock_info (code, name, market, board, industry, list_date, is_st, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (code, name, market, board, industry, list_date, is_st, now))
        conn.commit()

    def bulk_upsert_stock_info(self, stocks: list[dict]):
        """批量插入/更新 stock_info。

        参数:
            stocks: 字典列表，每个包含 code, name, market, board, industry, list_date, is_st。
        """
        conn = self.get_conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany("""
            INSERT OR REPLACE INTO stock_info (code, name, market, board, industry, list_date, is_st, updated_at)
            VALUES (:code, :name, :market, :board, :industry, :list_date, :is_st, :updated_at)
        """, [{**s, "updated_at": now} for s in stocks])
        conn.commit()

    def get_stock_info(self, code: str) -> dict | None:
        """获取一只股票的元数据。"""
        conn = self.get_conn()
        row = conn.execute(
            "SELECT * FROM stock_info WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_stock_codes(self, board_filter: str | None = None) -> list[str]:
        """获取所有股票代码，可按板块过滤。

        参数:
            board_filter: "主板" / "创业板" / "科创板" / "北交所" / None（全部）。
        """
        conn = self.get_conn()
        if board_filter:
            rows = conn.execute(
                "SELECT code FROM stock_info WHERE board = ?", (board_filter,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT code FROM stock_info").fetchall()
        return [r["code"] for r in rows]

    # ── download_state 增删改查 ──────────────────

    def get_download_state(self, code: str) -> dict | None:
        """获取某只股票的下载状态。"""
        conn = self.get_conn()
        row = conn.execute(
            "SELECT * FROM download_state WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None

    def set_download_state(self, code: str, status: str, last_date: str = "",
                           total_rows: int = 0, error_msg: str = ""):
        """更新某只股票的下载状态。"""
        conn = self.get_conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT OR REPLACE INTO download_state (code, last_date, total_rows, status, error_msg, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (code, last_date, total_rows, status, error_msg, now))
        conn.commit()

    def clear_download_state(self, status: str = "pending"):
        """重置所有股票的下载状态。"""
        conn = self.get_conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE download_state SET status = ?, error_msg = '', updated_at = ?",
            (status, now),
        )
        conn.commit()

    # ── scan_cache 操作 ──────────────────────────

    def save_scan_cache(self, scan_time: str, strategies: list[str],
                        hits: list[dict]):
        """保存扫描结果到缓存表（每次 scan 先清再写）。

        参数:
            scan_time: 扫描时间戳字符串。
            strategies: 参与扫描的策略名列表。
            hits: 命中列表，每个含 code, name, strategies。
        """
        conn = self.get_conn()
        # 清空旧缓存
        conn.execute("DELETE FROM scan_cache")
        # 写入新结果
        for h in hits:
            for s in h.get("strategies", []):
                conn.execute(
                    "INSERT OR REPLACE INTO scan_cache (scan_time, strategy, code, name) "
                    "VALUES (?, ?, ?, ?)",
                    (scan_time, s, h["code"], h["name"]),
                )
        conn.commit()

    def load_scan_cache(self) -> dict | None:
        """从缓存表读取最近一次扫描结果（兼容原 JSON 格式）。

        返回:
            dict 格式: {scan_time, strategies, volume_surge, ma_breakout, ...}
            无缓存时返回 None。
        """
        conn = self.get_conn()
        row = conn.execute("SELECT MAX(scan_time) as latest FROM scan_cache").fetchone()
        if not row or not row["latest"]:
            return None
        scan_time = row["latest"]
        rows = conn.execute(
            "SELECT strategy, code, name FROM scan_cache WHERE scan_time = ?",
            (scan_time,),
        ).fetchall()

        strategies = list(dict.fromkeys(r["strategy"] for r in rows))
        cache = {
            "scan_time": scan_time,
            "strategies": strategies,
        }
        # 按策略分组
        for s in strategies:
            short_key = _strategy_short_key(s)
            cache[short_key] = [
                {"code": r["code"], "name": r["name"]}
                for r in rows if r["strategy"] == s
            ]
        return cache

    # ── 维护操作 ─────────────────────────────────

    def vacuum(self, retention_days: int = 0):
        """清理过期数据并优化数据库。

        参数:
            retention_days: 删除 N 天前的数据，0 = 保留全部。
        """
        conn = self.get_conn()
        if retention_days > 0:
            cutoff = datetime.now()
            conn.execute(
                "DELETE FROM stock_daily WHERE date < ?",
                (cutoff.strftime("%Y-%m-%d"),),
            )
        conn.execute("PRAGMA optimize")
        conn.commit()

    def stats(self) -> dict:
        """返回数据库统计摘要。"""
        conn = self.get_conn()
        info_cnt = conn.execute("SELECT COUNT(*) as cnt FROM stock_info").fetchone()["cnt"]
        ds_cnt = conn.execute("SELECT COUNT(DISTINCT code) as cnt FROM stock_daily").fetchone()["cnt"]
        total = conn.execute("SELECT COUNT(*) as cnt FROM stock_daily").fetchone()["cnt"]

        date_range = conn.execute(
            "SELECT MIN(date) as min_d, MAX(date) as max_d FROM stock_daily"
        ).fetchone()

        done = conn.execute(
            "SELECT COUNT(*) as cnt FROM download_state WHERE status = 'done'"
        ).fetchone()["cnt"]
        pending = conn.execute(
            "SELECT COUNT(*) as cnt FROM download_state WHERE status = 'pending'"
        ).fetchone()["cnt"]
        errors = conn.execute(
            "SELECT COUNT(*) as cnt FROM download_state WHERE status = 'error'"
        ).fetchone()["cnt"]

        return {
            "stocks_in_info": info_cnt,
            "stocks_with_data": ds_cnt,
            "total_daily_rows": total,
            "data_from": date_range["min_d"] if date_range else None,
            "data_to": date_range["max_d"] if date_range else None,
            "download_done": done,
            "download_pending": pending,
            "download_errors": errors,
        }

    def reset_all(self):
        """删除所有表并重建（慎用！）。"""
        conn = self.get_conn()
        conn.executescript("""
            DROP TABLE IF EXISTS stock_daily;
            DROP TABLE IF EXISTS stock_info;
            DROP TABLE IF EXISTS download_state;
        """)
        conn.commit()
        self.init_schema()
