#!/usr/bin/env python3
"""
构建行业日频全量数据集 industry_daily_full.parquet

数据链: market/daily 全历史 + PIT 成分快照 → compute_concept_daily 聚合
背景: 原数据集为 hermes 时期 ad-hoc 产出（无代码生成器，2026-08-06 后断更），
本脚本补上生成器，供 mainline_detector / 回测脚本使用。

成分口径（2026-10-01 修订）:
  聚合按【当日真实成分】进行（PIT，来自 industry_members_daily.parquet）。
  旧实现传静态快照 industry_members.parquet，等于用 2026 年的成分回填 2020 年，
  早期日期混入当时尚未纳入该行业的个股（实测 2020-01-02 静态 5644 只 vs 真实 3817 只）。
  本产物是冷区信号（src/signals/sources.py）的唯一输入，偏差会直接传导为信号 alpha。
  缺 PIT 快照时默认报错退出，需显式加 --allow-static-fallback 才回退。

用法:
  python3 scripts/build_industry_daily_full.py [--days 2000]
  python3 scripts/build_members_daily.py && python3 scripts/build_industry_daily_full.py --force-overwrite

日更建议: --days 60（breadth 依赖 MA20/MA13，需 ≥33 天预热；按日期 upsert 不截断历史）
"""
import sys
import argparse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paths import INDUSTRY_DIR
from src.utils.market_data import (
    load_recent_market,
    compute_concept_daily,
    load_industry_members,
    load_industry_members_daily,
)

OUT_PATH = INDUSTRY_DIR / "industry_daily_full.parquet"


def main(days: int = 2000, force_overwrite: bool = False,
         allow_static_fallback: bool = False):
    print("[1/3] 加载行业成员映射...")
    members = load_industry_members()
    if not members:
        print("❌ 无行业成员映射（先运行 scripts/pull_futu_industry.py）")
        return 1
    print(f"  {len(members)} 个行业（静态快照，仅用于行业全集/名称）")

    # PIT 成分快照：消除前视偏差的关键输入（2026-10-01）
    # 若不传，聚合会按【当前】成分回填全历史 —— 早期日期将混入当时尚未纳入
    # 该行业的个股（实测 2020-01-02 静态表 5644 只 vs 真实 3817 只），
    # 而下游冷区信号正是以本产物为唯一输入，偏差会直接传导成信号 alpha。
    members_daily = load_industry_members_daily()
    if members_daily.empty:
        msg = ("缺 PIT 成分快照 industry_members_daily.parquet，"
               "只能按当前成分回填全历史（前视偏差）")
        if not allow_static_fallback:
            print(f"❌ {msg}")
            print("   修复: python3 scripts/build_members_daily.py（重建 PIT 成分快照）")
            print("   确需回退静态快照时再加 --allow-static-fallback。")
            return 1
        print(f"  ⚠️ {msg} —— 已指定 --allow-static-fallback，按静态快照继续")
    else:
        print(f"  PIT 成分快照: {len(members_daily):,} 行 / "
              f"{members_daily['date'].nunique()} 天 "
              f"({members_daily['date'].min().date()} ~ {members_daily['date'].max().date()})")

    print("[2/3] 加载全历史行情...")
    mkt = load_recent_market(days=days)
    if mkt.empty:
        print("❌ 无行情数据")
        return 1
    print(f"  {len(mkt)} 行, {mkt['date'].nunique()} 个交易日")

    print("[3/3] 计算行业日频聚合（131 行业 × 全历史，约数分钟）...")
    daily = compute_concept_daily(
        mkt, members,
        members_daily=members_daily if not members_daily.empty else None,
    )
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
            # 护栏（2026-10-01 修订）：保证「窗口外历史」逐条完整保留。
            # 不再比对总行数 —— PIT 口径下同一天的行业数会变（实测 2020 年只有
            # 127 个行业而非 131），全量重建时总行数下降属正常，不是历史丢失。
            kept_keys = set(zip(pd.to_datetime(kept["date"]), kept["con_code"]))
            merged_keys = set(zip(pd.to_datetime(merged["date"]), merged["con_code"]))
            lost = kept_keys - merged_keys
            if lost:
                raise ValueError(
                    f"upsert 后窗口外历史丢失 {len(lost)} 条（例如 {sorted(lost)[:3]}）")
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
    parser.add_argument("--allow-static-fallback", action="store_true",
                        help="缺 PIT 成分快照时允许退回静态快照（结果含前视偏差，仅供应急）")
    args = parser.parse_args()
    sys.exit(main(args.days, args.force_overwrite, args.allow_static_fallback) or 0)
