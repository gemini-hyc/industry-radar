#!/usr/bin/env python3
"""
计算行业市值加权涨跌幅，产出 data/industry/industry_weighted_returns.parquet

公式: weighted_pct = Σ(pctChg × circ_mv) / Σ(circ_mv)

异常样本剔除（2026-10-01）：
  当日无涨跌幅限制的观测（新股/复牌/退市整理）不计入聚合，否则会在行业内
  制造虚假波动（实测最大修正 77.97pp）。判据见 src/utils/data_quality.py。
"""
import sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paths import MARKET_DIR, BASIC_DIR, INDUSTRY_DIR, STK_LIMIT_DIR
from src.utils.data_quality import load_list_dates, load_no_limit_flags, mark_abnormal

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

    # ── 2b. 异常样本判据（新股无涨跌幅限制日 / 次新 / 价格失真）──
    list_dates = load_list_dates()
    print(f"上市日期字典: {len(list_dates)} 只（stock_basic，可能滞后于行情）")
    if not Path(STK_LIMIT_DIR).exists():
        print(f"⚠️ 无 stk_limit 目录（{STK_LIMIT_DIR}）→ 判据「无涨跌幅限制日」不可用，"
              f"仅靠 次新/价格失真 兜底")

    market_parts = []
    skipped = []
    n_raw = n_dropped = n_nolimit_days = 0
    for f in market_files:
        df = pd.read_parquet(f, columns=["date", "code", "pctChg", "amount", "volume",
                                         "preclose"])
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
                # ③ 修复(2026-10-01): 剔除「当日无涨跌幅限制」的观测，
                #    否则新股首日 +1900% / 复牌 +488% 这类极值会污染行业聚合
                nolim = load_no_limit_flags([d])
                n_nolimit_days += len(nolim)
                mask, _ = mark_abnormal(df, no_limit=nolim, list_dates=list_dates)
                n_raw += len(df)
                n_dropped += int(mask.sum())
                df = df[~mask]
            if not df.empty:
                market_parts.append(df)

    if not market_parts:
        print("错误: 无匹配行情")
        sys.exit(1)

    market = pd.concat(market_parts, ignore_index=True)
    print(f"行情: {len(market)} 条 ({market['date'].nunique()} 个交易日)")
    print(f"异常样本: 剔除 {n_dropped} / {n_raw} 条（{n_dropped/max(n_raw,1)*100:.3f}%）"
          f"；其中 stk_limit 标记的无涨跌幅限制日 {n_nolimit_days} 个「日×股」")

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
    # 保护规则（2026-10-01）：口径不匹配或 upsert 不可信时**拒绝写入**，不再退回整文件覆盖。
    if OUT_PATH.exists():
        prev = pd.read_parquet(OUT_PATH)
        if "circ_mv" not in prev.columns:
            print("❌ 旧文件为 total_mv 口径，与当前 circ_mv 口径不可混存。")
            print(f"   已拒绝覆盖 —— 否则会把历史截断为本次窗口的 {len(result)} 行。")
            print("   处理：先移走旧文件再重跑本脚本（建议 --days 250 重建窗口）")
            print(f"     mv {OUT_PATH} {OUT_PATH}.bak-totalmv-$(date +%Y%m%d)")
            sys.exit(1)
        n_existing = len(prev)
        lo, hi = result["date"].min(), result["date"].max()
        kept = prev[(prev["date"] < lo) | (prev["date"] > hi)]
        merged = pd.concat([kept, result], ignore_index=True)
        merged = merged.drop_duplicates(subset=["date", "ind_code"], keep="last")
        merged = merged.sort_values(["date", "ind_code"]).reset_index(drop=True)
        if len(merged) < n_existing:
            print(f"❌ upsert 后 {len(merged)} 行 < 原文件 {n_existing} 行，疑似历史丢失，已拒绝写入。")
            sys.exit(1)
        result = merged
        print(f"  upsert: 保留窗口外 {len(kept)} 条，本次覆盖 {lo.date()} ~ {hi.date()}"
              f"，{n_existing} → {len(result)} 行")

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
