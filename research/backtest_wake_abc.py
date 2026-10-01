#!/usr/bin/env python3
"""
三方案唤醒条件回测对比
======================
方案 A（当前AND）: C0 AND C1 AND C2 AND C3 AND C4   (C2>12pp)
方案 B（C2提阈值）: C0 AND C1 AND C2 AND C3 AND C4   (C2>20pp)
方案 C（C3必须+1-of-N）: C0 AND C3 AND (C1 OR C2 OR C4)  (C2>20pp)

C0: 前5日平均breadth < 50%  (行业近期不热)
C1: breadth 20日百分位 < 45%  (沉睡)
C2: breadth 日跳升 > 12pp/20pp (跳升)
C3: avg_pct > 1.5%            (涨幅)
C4: 放量 > 1.15x              (放量)
"""

import pandas as pd
import numpy as np
from math import sqrt, erf
from pathlib import Path

# ── 配置 ──────────────────────────────────────
WAKE_SLEEP     = 0.45   # C1 百分位阈值
WAKE_DELTA_A   = 0.12   # C2 方案A/B跳升下限 (12pp)
WAKE_DELTA_C   = 0.20   # C2 方案C跳升下限 (20pp, 作为OR条件)
WAKE_PCT       = 1.5    # C3 涨幅下限
WAKE_AMT_RATIO = 1.15   # C4 放量下限
WAKE_AVG_BW5   = 0.50   # C0 前5日平均breadth上限
HOLD_DAYS      = [5, 10, 20]
COOLDOWN_DAYS  = 20
DATA_PATH      = Path("/Users/hyc/.hermes/quant/data/industry/industry_daily_full.parquet")
OUT_PATH       = Path("/Users/hyc/.hermes/quant/data/industry/wake_abc_detail.csv")

# ── 统计工具 ──────────────────────────────────
def ttest_1samp(x, popmean=0):
    x = np.array(x, dtype=float)
    n = len(x)
    if n < 2:
        return 0.0, 1.0
    mean = x.mean()
    var = x.var(ddof=1)
    if var == 0:
        return 0.0, 1.0
    t_val = (mean - popmean) / sqrt(var / n)
    p_val = 2 * (1 - 0.5 * (1 + erf(abs(t_val) / sqrt(2))))
    return t_val, p_val

# ── S₀ 评分函数（与 industry_tracker.py 一致） ──
def _score_c1(bw20_pct):
    if bw20_pct < 5:   return 25
    if bw20_pct < 10:  return 20
    if bw20_pct < 20:  return 15
    if bw20_pct < 35:  return 10
    if bw20_pct < 45:  return 5
    return 0

def _score_c2(delta_pp):
    if delta_pp > 30:  return 25
    if delta_pp > 20:  return 20
    if delta_pp > 15:  return 15
    if delta_pp > 12:  return 10
    return 0

def _score_c3(pct):
    if pct > 5:   return 25
    if pct > 3:   return 20
    if pct > 2:   return 15
    if pct > 1.5: return 10
    return 0

def _score_c4(ratio):
    if ratio > 3.0: return 25
    if ratio > 2.0: return 20
    if ratio > 1.5: return 15
    if ratio > 1.15: return 10
    return 0

def compute_S0(bw20_pct, delta_pp, pct, amt_ratio):
    return min(_score_c1(bw20_pct) + _score_c2(delta_pp) +
               _score_c3(pct) + _score_c4(amt_ratio), 100)

# ── 加载数据 ──────────────────────────────────
print("加载数据...")
df = pd.read_parquet(DATA_PATH)
df = df.sort_values(["con_code", "date"]).reset_index(drop=True)
print(f"  {df.shape[0]} 行, {df['con_code'].nunique()} 行业, "
      f"日期 {df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}")

# ── 计算每日唤醒条件 ──────────────────────────
print("计算唤醒条件...")

def compute_wake_flags(g):
    g = g.sort_values("date").copy()

    # C0: 前5日平均breadth（不含今日）
    g["avg_bw5"] = g["breadth"].shift(1).rolling(5, min_periods=3).mean()

    # C1: breadth 20日百分位
    g["bw20_pct"] = g["breadth"].rolling(20, min_periods=10).apply(
        lambda x: (x.iloc[-1] > x.iloc[:-1]).mean() * 100, raw=False
    )

    # C2: breadth 跳升 (今日 - 昨日)
    g["bw_prev"] = g["breadth"].shift(1)
    g["bw_delta"] = g["breadth"] - g["bw_prev"]

    # C4: 放量 (今日 / 昨日)
    g["amt_prev"] = g["total_amount"].shift(1)
    g["amt_ratio"] = g["total_amount"] / g["amt_prev"]

    # 各条件标记
    g["c0"] = g["avg_bw5"] < WAKE_AVG_BW5
    g["c1"] = g["bw20_pct"] < (WAKE_SLEEP * 100)
    g["c2_12"] = g["bw_delta"] > WAKE_DELTA_A   # 方案A/B的C2
    g["c2_20"] = g["bw_delta"] > WAKE_DELTA_C   # 方案C的C2
    g["c3"] = g["avg_pct"] > WAKE_PCT
    g["c4"] = g["amt_ratio"] > WAKE_AMT_RATIO

    # 三方案唤醒
    g["wake_a"] = g["c0"] & g["c1"] & g["c2_12"] & g["c3"] & g["c4"]
    g["wake_b"] = g["c0"] & g["c1"] & g["c2_20"] & g["c3"] & g["c4"]
    g["wake_c"] = g["c0"] & g["c3"] & (g["c1"] | g["c2_20"] | g["c4"])

    return g

df = df.groupby("con_code", group_keys=False).apply(compute_wake_flags)

# ── 提取唤醒事件（含冷却期） ──────────────────
def extract_events(df, wake_col, plan_label):
    events = df[df[wake_col]].copy()
    events = events.sort_values(["con_code", "date"])
    filtered = []
    for code, grp in events.groupby("con_code"):
        last_date = None
        for _, row in grp.iterrows():
            if last_date is None or (row["date"] - last_date).days >= COOLDOWN_DAYS:
                row_copy = row.copy()
                row_copy["plan"] = plan_label
                filtered.append(row_copy)
                last_date = row["date"]
    if not filtered:
        return pd.DataFrame(columns=["plan"])
    return pd.DataFrame(filtered)

print("提取唤醒事件...")
evts_a = extract_events(df, "wake_a", "A")
evts_b = extract_events(df, "wake_b", "B")
evts_c = extract_events(df, "wake_c", "C")
print(f"  方案A: {len(evts_a)} 次, 方案B: {len(evts_b)} 次, 方案C: {len(evts_c)} 次")

# ── 计算S₀ ──────────────────────────────────
def add_S0(events):
    if len(events) == 0:
        return events
    events["S0"] = events.apply(
        lambda r: compute_S0(
            r["bw20_pct"],
            r["bw_delta"] * 100,  # 转pp
            r["avg_pct"],
            r["amt_ratio"],
        ), axis=1
    )
    return events

evts_a = add_S0(evts_a)
evts_b = add_S0(evts_b)
evts_c = add_S0(evts_c)

# ── 计算后续收益 ──────────────────────────────
print("计算后续收益...")

all_dates = sorted(df["date"].unique())
date_idx = {d: i for i, d in enumerate(all_dates)}
daily_market_avg = df.groupby("date")["avg_pct"].mean()

def compute_returns(events, plan_label):
    if len(events) == 0:
        return pd.DataFrame(columns=["plan", "con_code", "wake_date", "avg_bw5",
                                      "bw20_pct", "bw_delta_pp", "avg_pct", "amt_ratio",
                                      "c0", "c1", "c2_12", "c2_20", "c3", "c4",
                                      "S0", "horizon", "horizon_target",
                                      "ind_cum", "bench_cum", "excess"])
    results = []
    skipped = 0
    for _, evt in events.iterrows():
        code = evt["con_code"]
        wake_date = evt["date"]
        if wake_date not in date_idx:
            skipped += 1
            continue
        idx = date_idx[wake_date]

        ind_data = df[df["con_code"] == code].set_index("date")["avg_pct"].sort_index()

        for h in HOLD_DAYS:
            if idx + h > len(all_dates):
                actual = len(all_dates) - idx
                if actual < 3:
                    skipped += 1
                    continue
                h_actual = actual
            else:
                h_actual = h

            trade_dates = all_dates[idx: idx + h_actual]

            ind_cum = sum(ind_data.get(d, 0) for d in trade_dates)
            bench_cum = sum(daily_market_avg.get(d, 0) for d in trade_dates)

            valid_days = sum(1 for d in trade_dates if d in ind_data.index)
            if valid_days < 3:
                skipped += 1
                continue

            results.append({
                "plan": plan_label,
                "con_code": code,
                "wake_date": wake_date.strftime("%Y-%m-%d"),
                "avg_bw5": round(evt["avg_bw5"], 4),
                "bw20_pct": round(evt["bw20_pct"], 1),
                "bw_delta_pp": round(evt["bw_delta"] * 100, 1),
                "avg_pct": round(evt["avg_pct"], 2),
                "amt_ratio": round(evt["amt_ratio"], 2),
                "c0": bool(evt["c0"]),
                "c1": bool(evt["c1"]),
                "c2_12": bool(evt["c2_12"]),
                "c2_20": bool(evt["c2_20"]),
                "c3": bool(evt["c3"]),
                "c4": bool(evt["c4"]),
                "S0": evt["S0"],
                "horizon": h_actual,
                "horizon_target": h,
                "ind_cum": round(ind_cum, 2),
                "bench_cum": round(bench_cum, 2),
                "excess": round(ind_cum - bench_cum, 2),
            })

    print(f"  方案{plan_label}: 有效{len(results)}条, 跳过{skipped}")
    return pd.DataFrame(results) if results else pd.DataFrame(columns=["plan"])

res_a = compute_returns(evts_a, "A")
res_b = compute_returns(evts_b, "B")
res_c = compute_returns(evts_c, "C")

all_results = pd.concat([res_a, res_b, res_c], ignore_index=True)

# ── 统计输出 ──────────────────────────────────
def stats_block(sub, label=""):
    rows = []
    for h in HOLD_DAYS:
        hsub = sub[sub["horizon_target"] == h]
        n = len(hsub)
        if n < 3:
            rows.append(f"  {h:2d}日 | n={n:3d} | 样本不足")
            continue

        ind = hsub["ind_cum"]
        exc = hsub["excess"]

        ind_mean = ind.mean()
        ind_win = (ind > 0).mean()
        exc_mean = exc.mean()
        exc_win = (exc > 0).mean()

        t_ind, p_ind = ttest_1samp(ind.values, 0)
        t_exc, p_exc = ttest_1samp(exc.values, 0)

        sig_ind = "***" if p_ind < 0.001 else "**" if p_ind < 0.01 else "*" if p_ind < 0.05 else ""
        sig_exc = "***" if p_exc < 0.001 else "**" if p_exc < 0.01 else "*" if p_exc < 0.05 else ""

        rows.append(
            f"  {h:2d}日 | n={n:3d} | "
            f"绝对 均值={ind_mean:+6.2f}% 胜率={ind_win:5.1%} t={t_ind:+5.1f}{sig_ind} | "
            f"超额 均值={exc_mean:+6.2f}% 胜率={exc_win:5.1%} t={t_exc:+5.1f}{sig_exc}"
        )
    return rows

def S0_stats(sub):
    h5 = sub[sub["horizon_target"] == 5]
    if len(h5) < 5:
        return "  S₀: 样本不足"
    s0 = h5["S0"]
    return (f"  S₀: 均值={s0.mean():.1f}, 中位={s0.median():.0f}, "
            f"≥70比例={((s0>=70).mean()):.1%}, ≥50比例={((s0>=50).mean()):.1%}")

print("\n" + "=" * 110)
print("三方案唤醒条件回测对比")
print("=" * 110)
print(f"数据范围: {df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}")
print(f"C0: 前5日平均breadth < {WAKE_AVG_BW5*100:.0f}%")
print(f"方案A: C0∧C1∧C2(>12pp)∧C3∧C4  [当前AND逻辑]")
print(f"方案B: C0∧C1∧C2(>20pp)∧C3∧C4  [仅收紧C2]")
print(f"方案C: C0∧C3∧(C1∨C2(>20pp)∨C4)  [C3必须+1-of-N]")
print()

for plan_label, res in [("A", res_a), ("B", res_b), ("C", res_c)]:
    h5 = res[res["horizon_target"] == 5] if len(res) > 0 else pd.DataFrame()
    n_events = len(h5)
    print(f"{'─' * 110}")
    print(f"方案 {plan_label}  唤醒事件数: {n_events}")
    print(S0_stats(res))
    print()
    for line in stats_block(res):
        print(line)

    # 按年度分布
    if n_events > 10 and "year" not in res.columns:
        res["year"] = res["wake_date"].str[:4] if "wake_date" in res.columns else ""
    if n_events > 10 and "year" in res.columns:
        print(f"\n  按年度:")
        for year in sorted(res["year"].unique()):
            ysub = res[res["year"] == year]
            n_y = len(ysub[ysub["horizon_target"] == 5])
            exc_20 = ysub[ysub["horizon_target"] == 20]
            if len(exc_20) >= 3:
                exc_m = exc_20["excess"].mean()
                exc_w = (exc_20["excess"] > 0).mean()
                print(f"    {year}: n={n_y:3d}, 20日超额均值={exc_m:+.2f}%, 胜率={exc_w:.0%}")
            else:
                print(f"    {year}: n={n_y:3d}")

    # 按S₀分层
    if n_events > 20:
        h20_sub = res[res["horizon_target"] == 20]
        if len(h20_sub) >= 10:
            s0_med = h20_sub["S0"].median()
            low = h20_sub[h20_sub["S0"] <= s0_med]
            high = h20_sub[h20_sub["S0"] > s0_med]
            if len(low) >= 3 and len(high) >= 3:
                print(f"\n  按S₀分层 (中位数={s0_med:.0f}):")
                print(f"    低S₀: n={len(low)}, 超额均值={low['excess'].mean():+.2f}%, 胜率={(low['excess']>0).mean():.0%}")
                print(f"    高S₀: n={len(high)}, 超额均值={high['excess'].mean():+.2f}%, 胜率={(high['excess']>0).mean():.0%}")

    print()

# ── 方案C特有：满足哪几个子条件 ──────────────
if len(res_c) > 0:
    print(f"{'=' * 110}")
    print("方案C 子条件满足分析")
    print(f"{'=' * 110}")
    h5_c = res_c[res_c["horizon_target"] == 5]
    total = len(h5_c)
    if total > 0:
        c1_only = ((h5_c["c1"]) & ~h5_c["c2_20"] & ~h5_c["c4"]).sum()
        c2_only = (~h5_c["c1"] & h5_c["c2_20"] & ~h5_c["c4"]).sum()
        c4_only = (~h5_c["c1"] & ~h5_c["c2_20"] & h5_c["c4"]).sum()
        c1_c2 = (h5_c["c1"] & h5_c["c2_20"] & ~h5_c["c4"]).sum()
        c1_c4 = (h5_c["c1"] & ~h5_c["c2_20"] & h5_c["c4"]).sum()
        c2_c4 = (~h5_c["c1"] & h5_c["c2_20"] & h5_c["c4"]).sum()
        all3 = (h5_c["c1"] & h5_c["c2_20"] & h5_c["c4"]).sum()

        print(f"  总事件: {total}")
        print(f"  仅C1(沉睡):           {c1_only:4d} ({c1_only/total:.1%})")
        print(f"  仅C2(跳升>20pp):      {c2_only:4d} ({c2_only/total:.1%})")
        print(f"  仅C4(放量):           {c4_only:4d} ({c4_only/total:.1%})")
        print(f"  C1+C2:               {c1_c2:4d} ({c1_c2/total:.1%})")
        print(f"  C1+C4:               {c1_c4:4d} ({c1_c4/total:.1%})")
        print(f"  C2+C4:               {c2_c4:4d} ({c2_c4/total:.1%})")
        print(f"  C1+C2+C4:            {all3:4d} ({all3/total:.1%})")

        h20_c = res_c[res_c["horizon_target"] == 20]
        if len(h20_c) >= 10:
            print(f"\n  各组合20日超额:")
            combos = {
                "含C1": h20_c[h20_c["c1"]],
                "含C2(>20pp)": h20_c[h20_c["c2_20"]],
                "含C4": h20_c[h20_c["c4"]],
                "仅C1(无C2无C4)": h20_c[h20_c["c1"] & ~h20_c["c2_20"] & ~h20_c["c4"]],
                "C1+C2+C4": h20_c[h20_c["c1"] & h20_c["c2_20"] & h20_c["c4"]],
            }
            for label, sub in combos.items():
                if len(sub) >= 3:
                    print(f"    {label:20s}: n={len(sub):4d}, 超额均值={sub['excess'].mean():+.2f}%, 胜率={(sub['excess']>0).mean():.0%}")
                else:
                    print(f"    {label:20s}: n={len(sub):4d}, 样本不足")

# ── 保存 ──────────────────────────────────────
all_results.to_csv(OUT_PATH, index=False)
print(f"\n明细已保存 → {OUT_PATH}")
print(f"总记录数: {len(all_results)}")
