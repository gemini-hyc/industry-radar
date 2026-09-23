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
    # ── 1. 加载每日行业成分快照（消前视偏差；替代当前静态成员表）──
    members_daily_path = INDUSTRY_DIR / "industry_members_daily.parquet"
    if not members_daily_path.exists():
        print("错误: 无每日成分快照，请先运行 build_members_daily.py")
        sys.exit(1)
    md = pd.read_parquet(members_daily_path)
    # date(Timestamp) -> {stock_code(bs格式): ind_code}
    mem_by_date = {
        pd.Timestamp(d): dict(zip(sub["stock_code"], sub["ind_code"]))
        for d, sub in md.groupby("date")
    }
    print(f"每日成分快照: {len(mem_by_date)} 天")

    # ── 2. 加载市场数据 ──
    market_files = sorted(MARKET_DIR.glob("*.parquet"))[-days:]
    if not market_files:
        print("错误: 无行情数据")
        sys.exit(1)

    market_parts = []
    skipped = []
    for f in market_files:
        df = pd.read_parquet(f, columns=["date", "code", "pctChg", "amount", "volume"])
        d = pd.to_datetime(df["date"].iloc[0]).normalize()
        day_map = mem_by_date.get(d)
        if day_map is None:
            skipped.append(d)  # 该日无成分快照（静默缺数的高危点，末尾统一告警）
            continue
        # 过滤: 只保留当日真实成分股（消除前视偏差）
        df = df[df["code"].isin(day_map)]
        if not df.empty:
            df["ind_code"] = df["code"].map(day_map)
            # ② 修复: 剔除 pctChg 缺失(停牌/新股首日无涨跌幅)，避免当 0% 污染加权值
            df = df.dropna(subset=["pctChg"])
            if not df.empty:
                market_parts.append(df)

    if not market_parts:
        print("错误: 无匹配行情")
        sys.exit(1)

    market = pd.concat(market_parts, ignore_index=True)
    print(f"行情: {len(market)} 条 ({market['date'].nunique()} 个交易日)")

    # 防静默缺数：行情已落地、但成分快照缺失的交易日会被整日跳过（不报错）
    # 新交易日必须先重建每日成分快照，否则该日永久缺失（窗口向前滚动后会掉出回溯范围）
    if skipped:
        print(f"⚠️ {len(skipped)} 个交易日因缺成分快照被跳过: "
              f"{', '.join(str(d.date()) for d in sorted(skipped))}")
        print("   → 请先运行 scripts/build_members_daily.py 重建每日成分快照")

    # ── 3. 加载市值数据 ──
    # 只读「行情窗口内」的日期：daily_basic 全量有 1600+ 个文件，--days 5 时 99% 的 IO 纯属浪费
    # （market/daily 与 daily_basic 的文件名同为 YYYY-MM-DD.parquet，直接按 stem 对齐）
    all_basic_files = sorted(BASIC_DIR.glob("*.parquet"))
    wanted_days = {f.stem for f in market_files}
    basic_files = [f for f in all_basic_files if f.stem in wanted_days]
    basic_parts = []
    for f in basic_files:
        df = pd.read_parquet(f, columns=["ts_code", "trade_date", "circ_mv"])
        df["code"] = df["ts_code"].apply(ts_to_bs)
        df["date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        # 用流通市值 circ_mv（非总市值 total_mv）：通达信/申万官方行业指数均为流通市值加权
        basic_parts.append(df[["date", "code", "circ_mv"]])

    basic = pd.concat(basic_parts, ignore_index=True)
    print(f"市值: {len(basic)} 条 ({basic['date'].nunique()} 个交易日)")

    # ── 4. 合并 ──
    merged = market.merge(basic, on=["date", "code"], how="inner")
    print(f"合并后: {len(merged)} 条")

    # ── 5. 计算加权涨跌幅：Σ(pctChg × circ_mv) / Σ(circ_mv) ──
    merged["weight"] = merged["pctChg"] * merged["circ_mv"]
    g = merged.groupby(["date", "ind_code"])
    result = g.agg(
        weighted_pct=("weight", "sum"),
        circ_mv=("circ_mv", "sum"),
        stock_n=("code", "nunique"),
    ).reset_index()
    result["weighted_pct"] = result["weighted_pct"] / result["circ_mv"]

    # ── 6. 保存（按日期 upsert：只替换本次窗口内的日期，保留窗口外历史）──
    # 注意：不可整文件覆盖 —— cron 每日以 --days 5 调用，覆盖写会把历史截断为 5 天
    if OUT_PATH.exists():
        prev = pd.read_parquet(OUT_PATH)
        if "circ_mv" not in prev.columns:
            print("  ⚠️ 旧文件为 total_mv 口径，与当前 circ_mv 口径不可混存，已忽略")
        else:
            lo, hi = result["date"].min(), result["date"].max()
            kept = prev[(prev["date"] < lo) | (prev["date"] > hi)]
            result = pd.concat([kept, result], ignore_index=True)
            result = result.drop_duplicates(subset=["date", "ind_code"], keep="last")
            result = result.sort_values(["date", "ind_code"]).reset_index(drop=True)
            print(f"  upsert: 保留窗口外 {len(kept)} 条，本次覆盖 {lo.date()} ~ {hi.date()}")

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
