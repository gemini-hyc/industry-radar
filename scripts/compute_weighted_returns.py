#!/usr/bin/env python3
"""
计算行业市值加权涨跌幅，产出 data/industry/industry_weighted_returns.parquet

公式: weighted_pct = Σ(pctChg × circ_mv) / Σ(circ_mv)
"""
import sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paths import MARKET_DIR, BASIC_DIR, INDUSTRY_DIR

OUT_PATH = INDUSTRY_DIR / "industry_weighted_returns.parquet"


def ts_to_bs(ts_code: str) -> str:
    """000001.SZ → sz.000001"""
    parts = ts_code.split(".")
    if len(parts) == 2:
        return f"{parts[1].lower()}.{parts[0]}"
    return ts_code


def main(days: int = 250):
    # ── 1. 加载行业成员映射 ──
    members_path = INDUSTRY_DIR / "industry_members.parquet"
    if not members_path.exists():
        print("错误: 无行业成员数据")
        sys.exit(1)
    members_df = pd.read_parquet(members_path)
    # {stock_code: ind_code}
    stock_to_ind = dict(zip(members_df["stock_code"], members_df["ind_code"]))

    # ── 2. 加载市场数据 ──
    market_files = sorted(MARKET_DIR.glob("*.parquet"))[-days:]
    if not market_files:
        print("错误: 无行情数据")
        sys.exit(1)

    market_parts = []
    for f in market_files:
        df = pd.read_parquet(f, columns=["date", "code", "pctChg", "amount", "volume"])
        # 过滤: 只保留行业成分股
        df = df[df["code"].isin(stock_to_ind)]
        if not df.empty:
            market_parts.append(df)

    if not market_parts:
        print("错误: 无匹配行情")
        sys.exit(1)

    market = pd.concat(market_parts, ignore_index=True)
    market["ind_code"] = market["code"].map(stock_to_ind)
    print(f"行情: {len(market)} 条 ({market['date'].nunique()} 个交易日)")

    # ── 3. 加载市值数据 ──
    basic_files = sorted(BASIC_DIR.glob("*.parquet"))
    basic_parts = []
    for f in basic_files:
        df = pd.read_parquet(f)
        df["code"] = df["ts_code"].apply(ts_to_bs)
        df["date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        basic_parts.append(df[["date", "code", "total_mv"]])

    basic = pd.concat(basic_parts, ignore_index=True)
    print(f"市值: {len(basic)} 条 ({basic['date'].nunique()} 个交易日)")

    # ── 4. 合并 ──
    merged = market.merge(basic, on=["date", "code"], how="inner")
    print(f"合并后: {len(merged)} 条")

    # ── 5. 计算加权涨跌幅：Σ(pctChg × total_mv) / Σ(total_mv) ──
    merged["weight"] = merged["pctChg"] * merged["total_mv"]
    g = merged.groupby(["date", "ind_code"])
    result = g.agg(
        weighted_pct=("weight", "sum"),
        total_mv=("total_mv", "sum"),
        stock_n=("code", "nunique"),
    ).reset_index()
    result["weighted_pct"] = result["weighted_pct"] / result["total_mv"]

    # ── 6. 保存 ──
    result.to_parquet(OUT_PATH, index=False)
    print(f"\n✓ 保存: {OUT_PATH}")
    print(f"  行业数: {result['ind_code'].nunique()}")
    print(f"  日期范围: {result['date'].min().date()} ~ {result['date'].max().date()}")

    # ── 7. 验证 ──
    latest = result[result["date"] == result["date"].max()]
    print(f"\n最新日 ({result['date'].max().date()}): {len(latest)} 个行业")
    top = latest.nlargest(5, "weighted_pct")
    bot = latest.nsmallest(5, "weighted_pct")
    print("\n涨幅 Top 5:")
    for _, r in top.iterrows():
        print(f"  {r['ind_code']:20s} +{r['weighted_pct']:5.2f}%  ({r['stock_n']}只)")
    print("\n跌幅 Top 5:")
    for _, r in bot.iterrows():
        print(f"  {r['ind_code']:20s} {r['weighted_pct']:6.2f}%  ({r['stock_n']}只)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=250, help="回溯天数")
    args = parser.parse_args()
    main(days=args.days)
