"""
股票列表管理：获取、过滤、缓存全A股列表。
"""

import logging
from datetime import datetime

import pandas as pd

from utils.helpers import get_board, get_market

logger = logging.getLogger("quant.stock_list")


def _fetch_from_akshare() -> pd.DataFrame:
    """从 akshare 获取全A股列表。

    返回:
        DataFrame，列名: code, name, market, board, is_st。
        失败时返回空 DataFrame。
    """
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            logger.warning("akshare 返回空股票列表")
            return pd.DataFrame()

        code_col = df["code"].astype(str).str.zfill(6)
        name_col = df["name"].astype(str)

        result = pd.DataFrame({
            "code": code_col,
            "name": name_col,
        })
        result["market"] = result["code"].apply(get_market)
        result["board"] = result["code"].apply(get_board)
        result["is_st"] = result["name"].apply(
            lambda n: 1 if ("ST" in n or "*ST" in n) else 0
        )
        result["list_date"] = ""   # 后续可单独补充
        result["industry"] = ""    # 后续可单独补充

        return result
    except Exception as e:
        logger.error(f"从 akshare 获取股票列表失败: {e}")
        return pd.DataFrame()


def _fetch_stock_industry() -> dict[str, str]:
    """从 akshare 获取行业分类。

    返回:
        dict，code → 行业名称。
    """
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            return {}

        result: dict[str, str] = {}
        for _, row in df.iterrows():
            code = str(row.get("代码", "")).strip()
            industry = str(row.get("板块名称", "")).strip()
            if code and industry:
                result[code] = industry
        return result
    except Exception:
        return {}


def get_stock_list(force_refresh: bool = False) -> pd.DataFrame:
    """获取全A股列表。

    参数:
        force_refresh: 为 True 时强制从 akshare 重新获取。

    返回:
        DataFrame，列名: code, name, market, board, is_st, list_date, industry。
    """
    df = _fetch_from_akshare()
    if df.empty:
        logger.error("无法获取股票列表")
        return df

    # 补充行业信息
    try:
        industries = _fetch_stock_industry()
        if industries:
            df["industry"] = df["code"].map(industries).fillna("")
    except Exception:
        pass

    return df


def filter_stocks(
    df: pd.DataFrame,
    boards: dict | None = None,
    exclude_st: bool = True,
) -> pd.DataFrame:
    """按板块和 ST 状态过滤股票列表。

    参数:
        df: get_stock_list() 返回的 DataFrame。
        boards: 板块开关，如 {"sh_main": True, "sz_main": False, "chi": True, "kcb": False}。
                为 None 时包含全部板块。
        exclude_st: 为 True 时排除 ST/*ST 股票。

    返回:
        过滤后的 DataFrame。
    """
    if df.empty:
        return df

    mask = pd.Series(True, index=df.index)

    if boards:
        board_mask = pd.Series(False, index=df.index)
        if boards.get("sh_main"):
            board_mask |= df["code"].str.match(r"^60[0-3]")  # 600-603
            board_mask |= df["code"].str.match(r"^605")       # 605
        if boards.get("sz_main"):
            board_mask |= df["code"].str.match(r"^000|^001|^002|^003")
        if boards.get("chi"):
            board_mask |= df["code"].str.match(r"^30[0-1]")   # 300-301
        if boards.get("kcb"):
            board_mask |= df["code"].str.match(r"^688|^689")
        mask &= board_mask

    if exclude_st:
        mask &= df["is_st"] == 0

    return df[mask].reset_index(drop=True)


def sync_stock_info_to_db(db, force_refresh: bool = False) -> int:
    """获取股票列表并同步到 stock_info 表。

    参数:
        db: Database 实例。
        force_refresh: 为 True 时强制从数据源重新获取。

    返回:
        同步的股票数量。
    """
    df = get_stock_list(force_refresh=force_refresh)
    if df.empty:
        return 0

    stocks = []
    for _, row in df.iterrows():
        stocks.append({
            "code": row["code"],
            "name": row["name"],
            "market": row.get("market", ""),
            "board": row.get("board", ""),
            "industry": row.get("industry", ""),
            "list_date": row.get("list_date", ""),
            "is_st": int(row.get("is_st", 0)),
        })

    db.bulk_upsert_stock_info(stocks)
    logger.info(f"已同步 {len(stocks)} 只股票到 stock_info")
    return len(stocks)
