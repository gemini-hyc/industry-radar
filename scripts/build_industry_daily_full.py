#!/usr/bin/env python3
"""
构建行业日频全量数据集 industry_daily_full.parquet

数据链: market/daily 全历史 + industry_members → compute_concept_daily 聚合
背景: 原数据集为 hermes 时期 ad-hoc 产出（无代码生成器，2026-08-06 后断更），
本脚本补上生成器，供 mainline_detector / 回测脚本使用。

用法: python3 scripts/build_industry_daily_full.py [--days 2000]

日更建议: --days 60（breadth 依赖 MA20/MA13，需 ≥33 天预热；按日期 upsert 不截断历史）
"""
import sys
import argparse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paths import INDUSTRY_DIR
from src.utils.market_data import load_recent_market, compute_concept_daily, load_industry_members

OUT_PATH = INDUSTRY_DIR / "industry_daily_full.parquet"


def main(days: int = 2000, force_overwrite: bool = False):
    print("[1/3] 加载行业成员映射...")
    members = load_industry_members()
    if not members:
        print("❌ 无行业成员映射（先运行 scripts/pull_futu_industry.py）")
        return 1
    print(f"  {len(members)} 个行业")

    print("[2/3] 加载全历史行情...")
    mkt = load_recent_market(days=days)
    if mkt.empty:
        print("❌ 无行情数据")
        return 1
    print(f"  {len(mkt)} 行, {mkt['date'].nunique()} 个交易日")

    print("[3/3] 计算行业日频聚合（131 行业 × 全历史，约数分钟）...")
    daily = compute_concept_daily(mkt, members)
    if daily.empty:
        print("❌ 聚合结果为空")
        return 1

    # ── 按日期 upsert（不可整文件覆盖：日更只算近 --days 天，覆盖写会截断历史）──
    # 保护规则（2026-10-01）：upsert 拿不到可信结果时**拒绝写入**，不再退回整文件覆盖
    # —— 后者会把全历史静默截断成当次窗口（原实现的隐患）。
    key = ["date", "con_code"]
    n_existing = 0

    if force_overwrite:
        print(f"  ⚠️ --force-overwrite：跳过 upsert，按本次结果整表覆盖（{len(daily)} 行）")
    elif not OUT_PATH.exists():
        pass
    elif not all(k in daily.columns for k in key):
        print(f"❌ 已有 {OUT_PATH.name}，但本次结果缺少关键列 {key}，拒绝覆盖以免截断历史。")
        return 1
    else:
        try:
            prev = pd.read_parquet(OUT_PATH)
            if not all(k in prev.columns for k in key):
                raise ValueError(f"旧文件缺少关键列 {key}，实际列={list(prev.columns)[:8]}")
            n_existing = len(prev)
            d_new = pd.to_datetime(daily["date"])
            d_old = pd.to_datetime(prev["date"])
            lo, hi = d_new.min(), d_new.max()
            kept = prev[(d_old < lo) | (d_old > hi)]
            merged = pd.concat([kept, daily], ignore_index=True)
            merged = merged.drop_duplicates(subset=key, keep="last")
            merged = merged.sort_values(key).reset_index(drop=True)
            # 行数护栏：upsert 后不应少于原文件（窗口内旧行被同 key 覆盖，总数不降）
            if len(merged) < n_existing:
                raise ValueError(f"upsert 后 {len(merged)} 行 < 原文件 {n_existing} 行，疑似历史丢失")
            daily = merged
            print(f"  upsert: 保留窗口外 {len(kept)} 条，本次覆盖 {lo.date()} ~ {hi.date()}"
                  f"，{n_existing} → {len(daily)} 行")
        except Exception as e:
            print(f"❌ upsert 失败：{e}")
            print(f"   已拒绝写入 —— 否则会把 {n_existing} 行历史截断为本次窗口的 {len(daily)} 行。")
            print("   修复后重跑；确认要整表重建时加 --force-overwrite。")
            return 1

    daily.to_parquet(OUT_PATH, index=False)
    print(f"✅ 已保存 {OUT_PATH}")
    print(f"   {len(daily)} 行, {daily['con_code'].nunique()} 行业, "
          f"{daily['date'].min().date()} ~ {daily['date'].max().date()}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2000)
    parser.add_argument("--force-overwrite", action="store_true",
                        help="跳过 upsert，按本次结果整表覆盖（仅用于全量重建）")
    args = parser.parse_args()
    sys.exit(main(args.days, args.force_overwrite) or 0)
