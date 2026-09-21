#!/usr/bin/env python3
"""
构建行业日频全量数据集 industry_daily_full.parquet

数据链: market/daily 全历史 + industry_members → compute_concept_daily 聚合
背景: 原数据集为 hermes 时期 ad-hoc 产出（无代码生成器，2026-08-06 后断更），
本脚本补上生成器，供 mainline_detector / 回测脚本使用。

用法: python3 scripts/build_industry_daily_full.py [--days 2000]
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paths import INDUSTRY_DIR
from src.utils.market_data import load_recent_market, compute_concept_daily, load_industry_members

OUT_PATH = INDUSTRY_DIR / "industry_daily_full.parquet"


def main(days: int = 2000):
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

    daily.to_parquet(OUT_PATH, index=False)
    print(f"✅ 已保存 {OUT_PATH}")
    print(f"   {len(daily)} 行, {daily['con_code'].nunique()} 行业, "
          f"{daily['date'].min().date()} ~ {daily['date'].max().date()}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2000)
    args = parser.parse_args()
    sys.exit(main(args.days) or 0)
