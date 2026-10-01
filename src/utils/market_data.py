"""
通用市场数据加载工具

从 growth_momentum 模块剥离的共享函数：
  - load_recent_market()   原始来源: step1_concept.py
  - compute_concept_daily() 原始来源: step1_concept.py
  - load_industry_members() 原始来源: step1_industry.py
"""
import json
import os
import sys
import pandas as pd
import numpy as np
from pathlib import Path

# 数据根统一走 src/paths.py（env INDUSTRY_RADAR_DATA > config.yaml > 默认共享数据目录）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.paths import DATA_DIR, MARKET_DIR, INDUSTRY_DIR
from src.utils.data_quality import load_list_dates, load_no_limit_flags, mark_abnormal


# ── 异常样本剔除（2026-10-01）──────────────────
# 行业聚合不应包含「当日无涨跌幅限制」的观测（新股/复牌/退市整理）。
# 判据与实测影响见 src/utils/data_quality.py 的模块 docstring。

def drop_abnormal_samples(market_df: pd.DataFrame):
    """剔除不应进入行业聚合的异常观测。返回 (清洗后 df, 统计)。"""
    if market_df.empty or "pctChg" not in market_df.columns:
        return market_df, {"total": 0, "marked": 0}

    dates = pd.to_datetime(market_df["date"]).dt.normalize().unique()
    nolim = load_no_limit_flags(dates)
    mask, stats = mark_abnormal(market_df, no_limit=nolim, list_dates=load_list_dates())
    if stats.get("marked", 0) == 0:
        return market_df, stats
    return market_df[~mask].copy(), stats


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
                          concept_members: dict,
                          drop_abnormal: bool = True,
                          breadth_dropna: bool = True) -> pd.DataFrame:
    """
    为每个概念/行业计算每日聚合指标。
    返回 DataFrame: date, con_code, con_name, avg_pct, breadth(MA20基准),
                     breadth_ma13(MA13基准,预警期用), total_amount, stock_n,
                     avg_open(等权均开盘价), avg_close(等权均收盘价)

    参数:
      drop_abnormal : 剔除「当日无涨跌幅限制」的观测（新股/复牌/退市整理）。
                      2026-10-01 起默认开启 —— 见 src/utils/data_quality.py。
      breadth_dropna: 宽度分母只计「MA 已成型」的个股。原实现里
                      `close > ma20` 在 ma20 为 NaN 时得 False，使新上市
                      (<5 日) 个股被当作「未站上均线」计入分母，系统性压低 breadth。
    """
    if market_df.empty or not concept_members:
        return pd.DataFrame()

    # n_stocks 用**剔除前**的成员表统计，保持「行业成员总数」语义不变
    n_stocks_map = {}
    if drop_abnormal:
        for con_code, codes in concept_members.items():
            n_stocks_map[con_code] = market_df.loc[
                market_df["code"].isin(codes), "code"].nunique()

    if drop_abnormal:
        market_df, _stats = drop_abnormal_samples(market_df)
        if market_df.empty:
            return pd.DataFrame()

    records = []

    for con_code, stock_codes in concept_members.items():
        con_market = market_df[market_df["code"].isin(stock_codes)]
        if con_market.empty:
            continue

        n_stocks = n_stocks_map.get(con_code, con_market["code"].nunique())

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
        above20 = con_market["close"] > con_market["ma20"]
        # breadth_dropna: ma20 未成型(NaN)的个股不参与分母。
        # 原实现下 `close > NaN` → False → 被当作「未站上均线」计入分母，系统性压低 breadth。
        con_market["above_ma20"] = (above20.where(con_market["ma20"].notna())
                                    if breadth_dropna else above20.astype(int))
        breadth_daily = con_market.groupby("date")["above_ma20"].mean().reset_index()
        breadth_daily.columns = ["date", "breadth"]
        daily = daily.merge(breadth_daily, on="date", how="left")

        # ── 计算宽度：站上MA13的比例（预警期用，更灵敏）──
        con_market["ma13"] = con_market.groupby("code")["close"].transform(
            lambda x: x.rolling(13, min_periods=5).mean()
        )
        above13 = con_market["close"] > con_market["ma13"]
        con_market["above_ma13"] = (above13.where(con_market["ma13"].notna())
                                    if breadth_dropna else above13.astype(int))
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
