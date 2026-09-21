"""
通用市场数据加载工具

从 growth_momentum 模块剥离的共享函数：
  - load_recent_market()   原始来源: step1_concept.py
  - compute_concept_daily() 原始来源: step1_concept.py
  - load_industry_members() 原始来源: step1_industry.py
"""
import json
import os
import pandas as pd
import numpy as np
from pathlib import Path


def _resolve_data_root() -> Path:
    """解析数据根目录，适配开发机(macOS)与生产容器双环境"""
    # 1. 环境变量优先
    env_root = os.environ.get("HERMES_DATA_DIR")
    if env_root:
        return Path(env_root)
    # 2. 生产容器路径
    prod_root = Path("/opt/data/quant")
    if prod_root.exists():
        return prod_root
    # 3. 开发环境：从本模块位置推断项目根目录
    module_dir = Path(__file__).resolve().parent  # src/utils/
    return module_dir.parent.parent  # 项目根目录


DATA_ROOT = _resolve_data_root()
MARKET_DIR = DATA_ROOT / "data/market/daily"
INDUSTRY_DIR = DATA_ROOT / "data/industry"


# ── 行情加载 ──────────────────────────────────

def load_recent_market(days: int = 250, end_date: str = None) -> pd.DataFrame:
    files = sorted(Path(MARKET_DIR).glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    if end_date:
        end_ts = pd.Timestamp(end_date)
        files = [f for f in files if pd.Timestamp(f.stem[:10]) <= end_ts]
    files = files[-days:]
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

    # 修复: 2026-05-29起数据源的amount单位变成了万元(volume正常)
    # 判断: 均价(amount/volume) < 0.1元 → 单位错误，乘10000还原
    if not df.empty and "amount" in df.columns and "volume" in df.columns:
        mask = (df["volume"] > 0) & (df["amount"] / df["volume"] < 0.1)
        bad_count = mask.sum()
        if bad_count > 0:
            df.loc[mask, "amount"] = df.loc[mask, "amount"] * 10000
    return df


# ── 概念/行业日频聚合 ─────────────────────────

def compute_concept_daily(market_df: pd.DataFrame,
                          concept_members: dict) -> pd.DataFrame:
    """
    为每个概念/行业计算每日聚合指标。
    返回 DataFrame: date, con_code, con_name, avg_pct, breadth(MA20基准),
                     breadth_ma13(MA13基准,预警期用), total_amount, stock_n,
                     avg_open(等权均开盘价), avg_close(等权均收盘价)
    """
    if market_df.empty or not concept_members:
        return pd.DataFrame()

    records = []

    for con_code, stock_codes in concept_members.items():
        con_market = market_df[market_df["code"].isin(stock_codes)]
        if con_market.empty:
            continue

        n_stocks = con_market["code"].nunique()

        # ── 按日期聚合：基础指标 ──
        daily = con_market.groupby("date").agg(
            avg_pct=("pctChg", "mean"),
            total_amount=("amount", "sum"),
            stock_n=("code", "nunique"),
        ).reset_index()

        # ── 计算宽度：站上MA20的比例（唤醒期用）──
        con_market = con_market.sort_values(["code", "date"]).copy()
        con_market["ma20"] = con_market.groupby("code")["close"].transform(
            lambda x: x.rolling(20, min_periods=5).mean()
        )
        con_market["above_ma20"] = (con_market["close"] > con_market["ma20"]).astype(int)
        breadth_daily = con_market.groupby("date")["above_ma20"].mean().reset_index()
        breadth_daily.columns = ["date", "breadth"]
        daily = daily.merge(breadth_daily, on="date", how="left")

        # ── 计算宽度：站上MA13的比例（预警期用，更灵敏）──
        con_market["ma13"] = con_market.groupby("code")["close"].transform(
            lambda x: x.rolling(13, min_periods=5).mean()
        )
        con_market["above_ma13"] = (con_market["close"] > con_market["ma13"]).astype(int)
        breadth13_daily = con_market.groupby("date")["above_ma13"].mean().reset_index()
        breadth13_daily.columns = ["date", "breadth_ma13"]
        daily = daily.merge(breadth13_daily, on="date", how="left")

        # ── 等权平均开盘价/收盘价（预警A3大实体阴线用）──
        # 市场daily数据无total_mv，用等权均值替代（5%阈值足够粗，等权不影响判断方向）
        if "open" in con_market.columns:
            oc_daily = con_market.groupby("date").agg(
                avg_open=("open", "mean"),
                avg_close=("close", "mean"),
            ).reset_index()
            daily = daily.merge(oc_daily, on="date", how="left")

        daily["con_code"] = con_code
        daily["n_stocks"] = n_stocks

        records.append(daily)

    if not records:
        return pd.DataFrame()

    result = pd.concat(records, ignore_index=True)
    # 确保列存在（数据不可用时补NaN）
    for col in ["breadth_ma13", "avg_open", "avg_close"]:
        if col not in result.columns:
            result[col] = float("nan")
    return result


# ── 行业成员加载 ──────────────────────────────

def load_industry_members() -> dict:
    """加载富途行业板块 → 成分股映射 {ind_code: [stock_codes]}"""
    members_path = INDUSTRY_DIR / "industry_members.parquet"
    if not members_path.exists():
        return {}

    df = pd.read_parquet(members_path)
    mapping = {}
    for ind_code, group in df.groupby("ind_code"):
        mapping[ind_code] = group["stock_code"].tolist()
    return mapping
