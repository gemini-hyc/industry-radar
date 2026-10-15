#!/usr/bin/env python3
"""
计算行业市值加权涨跌幅，产出 data/industry/industry_weighted_returns.parquet

公式（2026-10-15 修正）：weighted_pct = Σ(pctChg_t × circ_mv_{t-1}) / Σ circ_mv_{t-1}

🔴 权重口径修正（2026-10-15）：
  旧口径用**当日收盘** circ_mv 加权**当日**涨跌幅，数学偏差 ≈ +Var_w(r)（行业内加权
  收益方差），实测 **+6bp/日 ≈ +15%/年**。同一天同一批股票只换权重的对照实验：
    2019-2025 累计  当日市值加权 +272.8%  /  前一日市值加权 +53.9%  /  中证全指对照
  逐年看，改前一日权重后几乎贴合中证全指；旧口径每年虚增 10~25pp。
  更致命的是偏差**不等于常数**：Δ行业 = Var_w(r_行业)，取决于行业内部分散度，
  低估值（老经济、低波动）行业虚增更少 ⇒ 在 EX = R − mkt 中**不抵消**，其超额被
  系统性压低。详见 research/prosperity/05ah_加权口径缺陷与事件研究复验.md。

  经济含义：portfolio 在 t-1 收盘按当时市值建仓，持有到 t 收盘 ⇒ 当日组合收益
  恰为 Σ w_{t-1} · r_t，w_{t-1} = circ_mv_{t-1} / Σ circ_mv_{t-1}。这才是可实现的
  持有收益；用当日市值加权当日收益，等于用「收盘后才知道的权重」去赚当日的钱。

边界：窗口首个交易日无前一日 circ_mv（数据起点 2020-01-02），该日回落为当日
  市值加权并在产物里记 n_weight_fallback。全历史仅影响 1 天 / 1642 天。

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
from src.utils.provenance import save_dataframe

OUT_PATH = INDUSTRY_DIR / "industry_weighted_returns.parquet"

# 求前一交易日权重时，向窗口起点之前回溯的日历天数（cover 节假日，取到最近若干个交易日）
LAG_LOOKBACK_DAYS = 10


def ts_to_bs(ts_code: str) -> str:
    """000001.SZ → sz.000001"""
    parts = ts_code.split(".")
    if len(parts) == 2:
        return f"{parts[1].lower()}.{parts[0]}"
    return ts_code


def main(days: int = 250, weight_day: str = "prev"):
    """
    weight_day:  'prev'（默认，2026-10-15 修正口径）用**前一交易日** circ_mv 作权重
                 'same'（旧口径，仅供对照复现）用当日 circ_mv 作权重
    """
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
    # 「前一交易日」加权口径需要额外的 lag 补给：窗口起点之前若干个交易日的 basic，
    # 否则窗口首日取不到 circ_mv_{t-1}，会被迫回落当日权重（引入旧口径偏差）
    lag_files = []
    if weight_day == "prev" and wanted_days:
        lo_stem = min(wanted_days)
        lag_cut = pd.Timestamp(lo_stem) - pd.Timedelta(days=LAG_LOOKBACK_DAYS)
        lag_files = [f for f in all_basic_files
                     if f.stem < lo_stem and pd.Timestamp(f.stem) >= lag_cut]
    basic_files = [f for f in all_basic_files if f.stem in wanted_days] + lag_files
    basic_parts = []
    for f in basic_files:
        df = pd.read_parquet(f, columns=["ts_code", "trade_date", "circ_mv"])
        df["code"] = df["ts_code"].apply(ts_to_bs)
        df["date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        # 用流通市值 circ_mv（非总市值 total_mv）：通达信/申万官方行业指数均为流通市值加权
        basic_parts.append(df[["date", "code", "circ_mv"]])

    basic = pd.concat(basic_parts, ignore_index=True)
    print(f"市值: {len(basic)} 条 ({basic['date'].nunique()} 个交易日)"
          + (f"；含窗口外 lag 补给 {len(lag_files)} 天" if lag_files else ""))

    # ── 3b. 构造「前一交易日」权重基准（核心修正点）──
    if weight_day == "prev":
        if len(lag_files) == 0:
            print("⚠️ 窗口外无 basic 文件可取 lag ⇒ 窗口首日将回落当日权重（旧口径）")
        basic = basic.sort_values(["code", "date"]).reset_index(drop=True)
        basic["circ_mv_prev"] = basic.groupby("code")["circ_mv"].shift(1)
        basic["prev_date"] = basic.groupby("code")["date"].shift(1)
    else:
        print("⚠️ weight_day='same'：旧口径（当日市值加权当日涨跌幅）"
              "，含 +Var_w(r) 系统性正偏差，仅用于对照复现")

    # ── 4. 合并 ──
    merged = market.merge(basic, on=["date", "code"], how="inner")
    print(f"合并后: {len(merged)} 条")

    # ── 5. 加权涨跌幅 ──
    #   prev（默认/正确）: Σ(pctChg_t × circ_mv_{t-1}) / Σ circ_mv_{t-1}
    #   same（旧口径）   : Σ(pctChg_t × circ_mv_t)     / Σ circ_mv_t
    if weight_day == "prev":
        bad = merged["circ_mv_prev"].isna() | (merged["circ_mv_prev"] <= 0)
        n_w_fallback = int(bad.sum())
        merged["w"] = merged["circ_mv_prev"].where(~bad, merged["circ_mv"])
        gap = (merged["date"] - merged["prev_date"]).dt.days
        print(f"权重基准确认: 前一交易日 circ_mv；lag 间隔中位 {gap.median():.0f} 天，"
              f"回落当日值 {n_w_fallback} 条（{n_w_fallback / max(len(merged), 1) * 100:.3f}%）")
    else:
        n_w_fallback = 0
        merged["w"] = merged["circ_mv"]

    merged["weight"] = merged["pctChg"] * merged["w"]
    g = merged.groupby(["date", "ind_code"])
    result = g.agg(
        weighted_pct=("weight", "sum"),
        circ_mv=("circ_mv", "sum"),     # 注意：仍为**当日**市值合计（size 语义，非权重）
        stock_n=("code", "nunique"),
        wsum=("w", "sum"),
    ).reset_index()
    result["weighted_pct"] = result["weighted_pct"] / result["wsum"]
    result = result.drop(columns=["wsum"])

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

    written = save_dataframe(
        result, OUT_PATH,
        producer="scripts/compute_weighted_returns.py",
        params={"days": days, "weight": "circ_mv",
                "weight_day": weight_day,  # prev=前一交易日加权（2026-10-15 修正）/ same=旧口径
                "abnormal_filter": "no_limit_flags+list_dates"},
        inputs=[members_daily_path, MARKET_DIR, BASIC_DIR, STK_LIMIT_DIR],
        extra={"n_abnormal_dropped": n_dropped, "n_abnormal_scanned": n_raw,
               "n_weight_fallback": n_w_fallback,
               "skipped_days_no_snapshot": [str(d.date()) for d in sorted(skipped)]},
    )
    print(f"\n✓ 保存: {written}")
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
    parser.add_argument("--weight-day", dest="weight_day", choices=["prev", "same"],
                        default="prev",
                        help="权重取值日：prev=前一交易日 circ_mv（默认，2026-10-15 修正口径）；"
                             "same=当日 circ_mv（旧口径，含 +Var_w(r) 正偏差，仅供对照复现）")
    args = parser.parse_args()
    main(days=args.days, weight_day=args.weight_day)
