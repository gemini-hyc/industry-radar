#!/usr/bin/env python3
"""
S₀ 新旧权重对比回测
====================
基于 wake_pure_backtest_detail.csv 中的唤醒事件，
分别用原始等权(各25满分)和新IC比例权重(C3:60/C4:25/C1:10/C2:5)计算S₀，
按S₀三分位分层，对比累计超额收益。

原始权重 (满分100):
  C1 沉睡深度: bw20 <5%/10%/20%/35%/45% → 25/20/15/10/5
  C2 跳升力度: Δ>30/20/15/12pp → 25/20/15/10
  C3 涨幅强度: pct>5%/3%/2%/1.5% → 25/20/15/10
  C4 放量确认: ratio>3.0/2.0/1.5/1.15 → 25/20/15/10

新权重 (满分100):
  C1 沉睡深度: bw20 <5%/10%/20%/35%/45% → 10/7/4/2/1
  C2 跳升力度: Δ>30/20/15/12pp → 5/4/2/1
  C3 涨幅强度: pct>5%/3%/2%/1.5% → 60/45/30/15
  C4 放量确认: ratio>3.0/2.0/1.5/1.15 → 25/18/10/5
"""

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from math import sqrt, erf
from pathlib import Path

DATA_DIR  = Path("/Users/hyc/.hermes/quant/data/industry")
OUT_DIR   = Path("/Users/hyc/.hermes/quant/output")
OUT_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH  = DATA_DIR / "wake_pure_backtest_detail.csv"

def ttest_1samp(x, popmean=0):
    x = np.array(x, dtype=float)
    n = len(x)
    if n < 2: return 0.0, 1.0
    mean = x.mean()
    var = x.var(ddof=1)
    if var == 0: return 0.0, 1.0
    t_val = (mean - popmean) / sqrt(var / n)
    p_val = 2 * (1 - 0.5 * (1 + erf(abs(t_val) / sqrt(2))))
    return t_val, p_val

# ─── 原始打分 ───
def score_c1_old(bw20):
    if bw20 < 5:  return 25
    if bw20 < 10: return 20
    if bw20 < 20: return 15
    if bw20 < 35: return 10
    if bw20 < 45: return 5
    return 0

def score_c2_old(delta):
    if delta > 30: return 25
    if delta > 20: return 20
    if delta > 15: return 15
    if delta > 12: return 10
    return 0

def score_c3_old(pct):
    if pct > 5:   return 25
    if pct > 3:   return 20
    if pct > 2:   return 15
    if pct > 1.5: return 10
    return 0

def score_c4_old(ratio):
    if ratio > 3.0:  return 25
    if ratio > 2.0:  return 20
    if ratio > 1.5:  return 15
    if ratio > 1.15: return 10
    return 0

# ─── 新权重打分 ───
def score_c1_new(bw20):
    if bw20 < 5:  return 10
    if bw20 < 10: return 7
    if bw20 < 20: return 4
    if bw20 < 35: return 2
    if bw20 < 45: return 1
    return 0

def score_c2_new(delta):
    if delta > 30: return 5
    if delta > 20: return 4
    if delta > 15: return 2
    if delta > 12: return 1
    return 0

def score_c3_new(pct):
    if pct > 5:   return 60
    if pct > 3:   return 45
    if pct > 2:   return 30
    if pct > 1.5: return 15
    return 0

def score_c4_new(ratio):
    if ratio > 3.0:  return 25
    if ratio > 2.0:  return 18
    if ratio > 1.5:  return 10
    if ratio > 1.15: return 5
    return 0

# ─── 加载数据 ───
print("加载数据...")
df = pd.read_csv(CSV_PATH)
print(f"  {len(df)} 行, 唤醒事件×持有期")

# 计算两种S0
df["C1_old"] = df["bw20_pct"].apply(score_c1_old)
df["C2_old"] = df["bw_delta"].apply(score_c2_old)
df["C3_old"] = df["avg_pct"].apply(score_c3_old)
df["C4_old"] = df["amt_ratio"].apply(score_c4_old)
df["S0_old"] = (df["C1_old"] + df["C2_old"] + df["C3_old"] + df["C4_old"]).clip(upper=100)

df["C1_new"] = df["bw20_pct"].apply(score_c1_new)
df["C2_new"] = df["bw_delta"].apply(score_c2_new)
df["C3_new"] = df["avg_pct"].apply(score_c3_new)
df["C4_new"] = df["amt_ratio"].apply(score_c4_new)
df["S0_new"] = (df["C1_new"] + df["C2_new"] + df["C3_new"] + df["C4_new"]).clip(upper=100)

# ─── 构建事件级分tier映射 ───
df5 = df[df["horizon_target"] == 5].copy()
q33_old = df5["S0_old"].quantile(0.33)
q66_old = df5["S0_old"].quantile(0.66)
q33_new = df5["S0_new"].quantile(0.33)
q66_new = df5["S0_new"].quantile(0.66)

tier_map_old = {}
tier_map_new = {}
for _, row in df5.iterrows():
    key = (row["con_code"], row["wake_date"])
    v_old = row["S0_old"]
    v_new = row["S0_new"]
    tier_map_old[key] = "低" if v_old <= q33_old else ("高" if v_old > q66_old else "中")
    tier_map_new[key] = "低" if v_new <= q33_new else ("高" if v_new > q66_new else "中")

df["tier_old"] = df.apply(lambda r: tier_map_old.get((r["con_code"], r["wake_date"]), "中"), axis=1)
df["tier_new"] = df.apply(lambda r: tier_map_new.get((r["con_code"], r["wake_date"]), "中"), axis=1)

# ─── 报告 ───
report = []
def rp(s=""):
    print(s)
    report.append(s)

rp("=" * 100)
rp("S₀ 新旧权重对比回测")
rp("=" * 100)
rp(f"数据范围: {df['year'].min()} - {df['year'].max()}")
rp(f"唤醒事件数: {df5.shape[0]}")
rp()

# ─── 1. S0分布 ───
rp("=" * 100)
rp("【1】S₀ 分布对比")
rp("=" * 100)
rp(f"\n原始权重 S0: 均值={df5['S0_old'].mean():.1f}, 中位={df5['S0_old'].median():.1f}, "
   f"25%={df5['S0_old'].quantile(0.25):.1f}, 75%={df5['S0_old'].quantile(0.75):.1f}")
rp(f"新权重 S0:   均值={df5['S0_new'].mean():.1f}, 中位={df5['S0_new'].median():.1f}, "
   f"25%={df5['S0_new'].quantile(0.25):.1f}, 75%={df5['S0_new'].quantile(0.75):.1f}")

rp("\n原始权重各维度得分分布 (5日事件):")
for c in ["C1_old", "C2_old", "C3_old", "C4_old"]:
    rp(f"  {c}: 均值={df5[c].mean():.1f}, 中位={df5[c].median():.0f}, "
       f"0分占比={((df5[c]==0).mean()*100):.1f}%")

rp("\n新权重各维度得分分布 (5日事件):")
for c in ["C1_new", "C2_new", "C3_new", "C4_new"]:
    rp(f"  {c}: 均值={df5[c].mean():.1f}, 中位={df5[c].median():.0f}, "
       f"0分占比={((df5[c]==0).mean()*100):.1f}%")

# ─── 2. 分层回测 ───
rp("\n" + "=" * 100)
rp("【2】按 S₀ 三分位分层 — 超额收益对比")
rp("=" * 100)

for s0_col, tier_col, label, q33, q66 in [
    ("S0_old", "tier_old", "原始等权 (C1/C2/C3/C4 各25满分)", q33_old, q66_old),
    ("S0_new", "tier_new", "新IC比例权重 (C3:60/C4:25/C1:10/C2:5)", q33_new, q66_new),
]:
    rp(f"\n  ── {label} ── (分位: 低≤{q33:.0f}, 中≤{q66:.0f}, 高>{q66:.0f})")
    rp(f"    {'层级':>5s} {'Horizon':>8s} {'N':>5s} | {'超额均值':>8s} {'超额胜率':>8s} | {'绝对均值':>8s} {'绝对胜率':>8s} | {'t值':>7s} {'显著性':>5s}")
    rp(f"    {'─'*75}")
    
    for tier in ["低", "中", "高"]:
        for h in [5, 10, 20]:
            sub = df[(df[tier_col] == tier) & (df["horizon_target"] == h)]
            n = len(sub)
            if n < 3:
                rp(f"    {tier+'S₀':>5s} {h:>5d}日 {n:>5d} | (不足)")
                continue
            exc = sub["excess"]
            ind = sub["ind_cum"]
            t_val, p_val = ttest_1samp(exc.values, 0)
            sig = "***" if p_val < 0.001 else "**" if p_val < 0.01 else "*" if p_val < 0.05 else ""
            rp(f"    {tier+'S₀':>5s} {h:>5d}日 {n:>5d} | "
               f"{exc.mean():+7.2f}% {(exc>0).mean():7.1%} | "
               f"{ind.mean():+7.2f}% {(ind>0).mean():7.1%} | "
               f"{t_val:+6.2f} {sig:>5s}")
    
    # 高低差
    rp(f"\n    高低差 (高S₀ - 低S₀):")
    for h in [5, 10, 20]:
        low = df[(df[tier_col] == "低") & (df["horizon_target"] == h)]["excess"]
        high = df[(df[tier_col] == "高") & (df["horizon_target"] == h)]["excess"]
        if len(low) >= 3 and len(high) >= 3:
            diff_exc = high.mean() - low.mean()
            diff_win = (high > 0).mean() - (low > 0).mean()
            rp(f"      {h:2d}日: 超额差={diff_exc:+.2f}pp, 胜率差={diff_win:+.1%}")

# ─── 3. 累计收益模拟 ───
rp("\n" + "=" * 100)
rp("【3】累计收益模拟 — 高S₀组事件等权组合")
rp("=" * 100)
rp("\n模拟方法：高S₀组所有唤醒事件，等权买入，持有N日，统计逐年超额收益\n")

for s0_col, tier_col, label in [("S0_old", "tier_old", "原始权重"), ("S0_new", "tier_new", "新权重")]:
    high = df[(df[tier_col] == "高")]
    rp(f"\n  ── {label} ──")
    for h in [5, 10, 20]:
        sub = high[high["horizon_target"] == h]
        yearly = sub.groupby("year")["excess"].agg(["mean", "count", lambda x: (x > 0).mean()])
        yearly.columns = ["超额均值%", "事件数", "胜率"]
        rp(f"\n    {h}日持有 | 总事件={len(sub)}")
        rp(f"    {'年份':>6s} {'超额均值':>10s} {'事件数':>6s} {'胜率':>8s}")
        rp(f"    {'─'*35}")
        for year, row in yearly.iterrows():
            rp(f"    {year:>6d} {row['超额均值%']:>+9.2f}% {row['事件数']:>6.0f} {row['胜率']:>7.1%}")
        total_avg = sub["excess"].mean()
        total_win = (sub["excess"] > 0).mean()
        total_cum = sub["excess"].sum()
        rp(f"    {'全期':>6s} {total_avg:>+9.2f}% {len(sub):>6d} {total_win:>7.1%}")
        rp(f"    累计超额合计: {total_cum:+.2f}%")

# ─── 4. 逐年对比 ───
rp("\n" + "=" * 100)
rp("【4】逐年对比：高S₀组超额收益（新旧权重）")
rp("=" * 100)

for h in [5, 10, 20]:
    rp(f"\n  ── {h}日持有 ──")
    old_high = df[(df["tier_old"] == "高") & (df["horizon_target"] == h)]
    new_high = df[(df["tier_new"] == "高") & (df["horizon_target"] == h)]
    
    old_yr = old_high.groupby("year")["excess"].agg(["mean", "count", lambda x: (x>0).mean()])
    old_yr.columns = ["avg", "n", "win"]
    new_yr = new_high.groupby("year")["excess"].agg(["mean", "count", lambda x: (x>0).mean()])
    new_yr.columns = ["avg", "n", "win"]
    
    rp(f"  {'年份':>6s} | {'旧超额':>8s} {'旧胜率':>7s} {'旧N':>5s} | {'新超额':>8s} {'新胜率':>7s} {'新N':>5s} | {'差':>7s}")
    rp(f"  {'─'*70}")
    
    all_years = sorted(set(old_yr.index) | set(new_yr.index))
    for y in all_years:
        o = old_yr.loc[y] if y in old_yr.index else None
        n = new_yr.loc[y] if y in new_yr.index else None
        o_avg = o["avg"] if o is not None else np.nan
        o_win = o["win"] if o is not None else np.nan
        o_n = o["n"] if o is not None else 0
        n_avg = n["avg"] if n is not None else np.nan
        n_win = n["win"] if n is not None else np.nan
        n_n = n["n"] if n is not None else 0
        diff = n_avg - o_avg if not (np.isnan(o_avg) or np.isnan(n_avg)) else np.nan
        rp(f"  {y:>6d} | {o_avg:>+7.2f}% {o_win:>6.1%} {o_n:>5.0f} | "
           f"{n_avg:>+7.2f}% {n_win:>6.1%} {n_n:>5.0f} | {diff:>+6.2f}pp")

# ─── 5. Spearman IC 对比 ───
rp("\n" + "=" * 100)
rp("【5】S₀ 与超额收益的 Spearman IC 对比")
rp("=" * 100)

for h in [5, 10, 20]:
    sub = df[df["horizon_target"] == h]
    ic_old = sub["S0_old"].corr(sub["excess"], method="spearman")
    ic_new = sub["S0_new"].corr(sub["excess"], method="spearman")
    rp(f"  {h:2d}日: 旧IC={ic_old:+.4f}, 新IC={ic_new:+.4f}, 提升={ic_new - ic_old:+.4f}")

# ─── 6. 分层极差对比 ───
rp("\n" + "=" * 100)
rp("【6】分层极差对比（高S₀ - 低S₀ 超额均值差）")
rp("=" * 100)

rp(f"\n  {'Horizon':>8s} | {'旧极差':>8s} {'旧胜率差':>8s} | {'新极差':>8s} {'新胜率差':>8s} | {'极差提升':>8s}")
rp(f"  {'─'*60}")

for h in [5, 10, 20]:
    low_old = df[(df["tier_old"] == "低") & (df["horizon_target"] == h)]["excess"]
    high_old = df[(df["tier_old"] == "高") & (df["horizon_target"] == h)]["excess"]
    low_new = df[(df["tier_new"] == "低") & (df["horizon_target"] == h)]["excess"]
    high_new = df[(df["tier_new"] == "高") & (df["horizon_target"] == h)]["excess"]
    
    spread_old = high_old.mean() - low_old.mean()
    win_spread_old = (high_old > 0).mean() - (low_old > 0).mean()
    spread_new = high_new.mean() - low_new.mean()
    win_spread_new = (high_new > 0).mean() - (low_new > 0).mean()
    improvement = spread_new - spread_old
    
    rp(f"  {h:>5d}日 | {spread_old:>+7.2f}pp {win_spread_old:>+7.1%} | "
       f"{spread_new:>+7.2f}pp {win_spread_new:>+7.1%} | {improvement:>+7.2f}pp")

# ─── 写入 ───
report_path = OUT_DIR / "s0_new_weight_comparison.txt"
with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(report))
rp(f"\n报告已保存: {report_path}")
