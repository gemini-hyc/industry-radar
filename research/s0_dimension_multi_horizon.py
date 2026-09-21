#!/usr/bin/env python3
"""
S₀ 四维度多horizon回测
======================
在 5日、10日、20日 三个horizon上分别做：
  1. 单维度分层（低/中/高）→ 均值超额、中位超额、胜率
  2. 留一法增量贡献
  3. OLS回归（含系数显著性）
  4. 双维度交叉2×2

数据源: wake_pure_backtest_detail.csv
输出:   s0_dimension_multi_horizon_report.txt
"""

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import combinations

# ─── 路径 ───
DATA_DIR = Path("/Users/hyc/.hermes/quant/data/industry")
OUT_DIR  = Path("/Users/hyc/.hermes/quant/output")
OUT_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH = DATA_DIR / "wake_pure_backtest_detail.csv"

HORIZONS = [5, 10, 20]

# ─── 维度分档 → 得分 ───
def score_c1(bw20):
    if bw20 < 5:   return 25
    if bw20 < 10:  return 20
    if bw20 < 20:  return 15
    if bw20 < 35:  return 10
    if bw20 < 45:  return 5
    return 0

def score_c2(delta):
    if delta > 30:  return 25
    if delta > 20:  return 20
    if delta > 15:  return 15
    if delta > 12:  return 10
    return 0

def score_c3(pct):
    if pct > 5:   return 25
    if pct > 3:   return 20
    if pct > 2:   return 15
    if pct > 1.5: return 10
    return 0

def score_c4(ratio):
    if ratio > 3.0:  return 25
    if ratio > 2.0:  return 20
    if ratio > 1.5:  return 15
    if ratio > 1.15: return 10
    return 0

SCORE_FUNCS = {"C1": score_c1, "C2": score_c2, "C3": score_c3, "C4": score_c4}

# ─── 加载数据 ───
df = pd.read_csv(CSV_PATH)
df["C1_score"] = df["bw20_pct"].apply(score_c1)
df["C2_score"] = df["bw_delta"].apply(score_c2)
df["C3_score"] = df["avg_pct"].apply(score_c3)
df["C4_score"] = df["amt_ratio"].apply(score_c4)
df["S0_calc"]  = df[["C1_score","C2_score","C3_score","C4_score"]].sum(axis=1).clip(upper=100)

LABELS = ["低", "中", "高"]
dim_config = {
    "C1": ("bw20_pct", "沉睡深度(bw20越低越好)"),
    "C2": ("bw_delta", "跳升力度(Δ越大越好)"),
    "C3": ("avg_pct",  "涨幅强度(pct越大越好)"),
    "C4": ("amt_ratio","放量确认(额比越大越好)"),
}

report_lines = []
def rp(s=""):
    print(s)
    report_lines.append(s)

rp("=" * 90)
rp("S₀ 四维度多horizon回测报告 (5日 / 10日 / 20日)")
rp("=" * 90)
rp(f"数据范围: {df['year'].min()} - {df['year'].max()}")
rp(f"唤醒事件数: {df.drop_duplicates(subset=['con_code','wake_date']).shape[0]}")
rp(f"总行数(含多horizon): {len(df)}")
rp("")

# ═══════════════════════════════════════════════
# 1. 单维度分层回测（5日/10日/20日）
# ═══════════════════════════════════════════════
rp("=" * 90)
rp("【1】单维度分层回测 — 各horizon详细统计")
rp("=" * 90)

for h in HORIZONS:
    rp(f"\n{'─'*90}")
    rp(f"  Horizon = {h} 日")
    rp(f"{'─'*90}")
    dfh = df[df["horizon"] == h].copy()

    # ─── 汇总表 ───
    summary_rows = []

    for dim, (col, desc) in dim_config.items():
        rp(f"\n  --- {dim}: {desc} ---")
        dfh["group"] = pd.qcut(dfh[col], q=3, labels=LABELS, duplicates="drop")
        grp = dfh.groupby("group", observed=True)["excess"]
        stats = grp.agg(["count", "mean", "median", "std"])
        stats["win_rate"] = grp.apply(lambda x: (x > 0).mean())
        stats.columns = ["数量", "均值超额%", "中位超额%", "标准差", "胜率"]
        rp(stats.to_string(float_format="%.2f"))

        ic = dfh[col].corr(dfh["excess"], method="spearman")
        rp(f"    Spearman IC: {ic:+.4f}")

        # 收集汇总行
        for g in LABELS:
            if g in stats.index:
                summary_rows.append({
                    "维度": dim, "分组": g,
                    "均值超额%": stats.loc[g, "均值超额%"],
                    "中位超额%": stats.loc[g, "中位超额%"],
                    "胜率": stats.loc[g, "胜率"],
                    "IC": ic
                })

    # ─── 汇总对比表 ───
    rp(f"\n  ── Horizon={h} 各维度分层对比 ──")
    rp(f"  {'维度':>4} | {'低均值':>8} {'低胜率':>7} | {'中均值':>8} {'中胜率':>7} | {'高均值':>8} {'高胜率':>7} | {'高-低':>6} | {'IC':>7}")
    rp(f"  {'─'*80}")
    for dim, (col, desc) in dim_config.items():
        dim_rows = [r for r in summary_rows if r["维度"] == dim]
        vals = {}
        for r in dim_rows:
            vals[r["分组"]] = r
        if all(g in vals for g in LABELS):
            spread = vals["高"]["均值超额%"] - vals["低"]["均值超额%"]
            rp(f"  {dim:>4} | {vals['低']['均值超额%']:+8.2f} {vals['低']['胜率']:7.1%} | "
               f"{vals['中']['均值超额%']:+8.2f} {vals['中']['胜率']:7.1%} | "
               f"{vals['高']['均值超额%']:+8.2f} {vals['高']['胜率']:7.1%} | "
               f"{spread:+6.2f} | {vals['低']['IC']:+7.4f}")

# ═══════════════════════════════════════════════
# 2. 跨horizon趋势汇总（每个维度一张表）
# ═══════════════════════════════════════════════
rp("\n" + "=" * 90)
rp("【2】跨horizon趋势汇总 — 各维度高/低组均值超额 & 胜率")
rp("=" * 90)

for dim, (col, desc) in dim_config.items():
    rp(f"\n  --- {dim}: {desc} ---")
    rp(f"  {'Horizon':>8} | {'低均值':>8} {'低胜率':>7} | {'中均值':>8} {'中胜率':>7} | {'高均值':>8} {'高胜率':>7} | {'高-低':>6} | {'IC':>7}")
    rp(f"  {'─'*85}")
    for h in HORIZONS:
        dfh = df[df["horizon"] == h].copy()
        dfh["group"] = pd.qcut(dfh[col], q=3, labels=LABELS, duplicates="drop")
        grp = dfh.groupby("group", observed=True)["excess"]
        means = grp.mean()
        wr = grp.apply(lambda x: (x > 0).mean())
        ic = dfh[col].corr(dfh["excess"], method="spearman")
        spread = means.get("高", 0) - means.get("低", 0)
        rp(f"  {h:>8} | {means.get('低',0):+8.2f} {wr.get('低',0):7.1%} | "
           f"{means.get('中',0):+8.2f} {wr.get('中',0):7.1%} | "
           f"{means.get('高',0):+8.2f} {wr.get('高',0):7.1%} | "
           f"{spread:+6.2f} | {ic:+7.4f}")

# ═══════════════════════════════════════════════
# 3. 留一法 — 各horizon
# ═══════════════════════════════════════════════
rp("\n" + "=" * 90)
rp("【3】留一法 — 各horizon增量贡献")
rp("=" * 90)

X_cols = ["C1_score", "C2_score", "C3_score", "C4_score"]
dim_names = {"C1_score": "C1", "C2_score": "C2", "C3_score": "C3", "C4_score": "C4"}

for h in HORIZONS:
    rp(f"\n  Horizon = {h} 日")
    dfh = df[df["horizon"] == h].copy()

    # 完整S0分层
    dfh["S0_group"] = pd.qcut(dfh["S0_calc"], q=3, labels=LABELS, duplicates="drop")
    full_means = dfh.groupby("S0_group", observed=True)["excess"].mean()
    full_spread = full_means.get("高", 0) - full_means.get("低", 0)
    rp(f"  完整S0: 低={full_means.get('低',0):+.2f}% 中={full_means.get('中',0):+.2f}% 高={full_means.get('高',0):+.2f}%  极差={full_spread:.2f}%")

    loo = {}
    for drop_col in X_cols:
        keep = [c for c in X_cols if c != drop_col]
        dfh["S0_loo"] = dfh[keep].sum(axis=1).clip(upper=100)
        dfh["S0_loo_group"] = pd.qcut(dfh["S0_loo"], q=3, labels=LABELS, duplicates="drop")
        loo_means = dfh.groupby("S0_loo_group", observed=True)["excess"].mean()
        loo_spread = loo_means.get("高", 0) - loo_means.get("低", 0)
        delta = full_spread - loo_spread
        loo[dim_names[drop_col]] = {"极差": loo_spread, "增量": delta}
        rp(f"  去掉{dim_names[drop_col]}: 低={loo_means.get('低',0):+.2f}% 中={loo_means.get('中',0):+.2f}% 高={loo_means.get('高',0):+.2f}%  极差={loo_spread:.2f}%  (增量{delta:+.2f}pp)")

    rp(f"  增量排名: " + " > ".join(
        f"{k}={v['增量']:+.2f}pp" for k, v in sorted(loo.items(), key=lambda x: x[1]["增量"], reverse=True)
    ))

# ═══════════════════════════════════════════════
# 4. OLS回归 — 各horizon
# ═══════════════════════════════════════════════
rp("\n" + "=" * 90)
rp("【4】OLS回归 — 各horizon系数与显著性")
rp("=" * 90)

try:
    import statsmodels.api as sm

    for h in HORIZONS:
        rp(f"\n  Horizon = {h} 日")
        dfh = df[df["horizon"] == h].copy()
        X = dfh[X_cols].values
        y = dfh["excess"].values
        X_const = sm.add_constant(X)
        model = sm.OLS(y, X_const).fit()

        rp(f"  {'维度':>4} | {'系数':>8} | {'p值':>10} | {'显著':>4}")
        rp(f"  {'─'*40}")
        for i, col in enumerate(X_cols):
            coef = model.params[i + 1]
            pval = model.pvalues[i + 1]
            sig = "***" if pval < 0.001 else "**" if pval < 0.01 else "*" if pval < 0.05 else ""
            rp(f"  {dim_names[col]:>4} | {coef:+8.4f} | {pval:10.4f} | {sig}")
        rp(f"  R²={model.rsquared:.4f}  Adj R²={model.rsquared_adj:.4f}  F={model.fvalue:.2f}(p={model.f_pvalue:.2e})")

except ImportError:
    rp("  (需要statsmodels)")

# ═══════════════════════════════════════════════
# 5. 双维度交叉2×2 — 各horizon
# ═══════════════════════════════════════════════
rp("\n" + "=" * 90)
rp("【5】双维度交叉分析(2×2) — 各horizon交互效应")
rp("=" * 90)

pairs = list(combinations(["C1", "C2", "C3", "C4"], 2))

for h in HORIZONS:
    rp(f"\n  Horizon = {h} 日")
    dfh = df[df["horizon"] == h].copy()
    for dim, (col, _) in dim_config.items():
        dfh[dim + "_hilo"] = np.where(dfh[col] > dfh[col].median(), "高", "低")

    for d1, d2 in pairs:
        g = dfh.groupby([d1+"_hilo", d2+"_hilo"])["excess"].agg(["mean", "count"])
        means = {}
        for v1 in ["低", "高"]:
            for v2 in ["低", "高"]:
                if (v1, v2) in g.index:
                    means[(v1, v2)] = g.loc[(v1, v2), "mean"]
        if len(means) == 4:
            interaction = (means[("高","高")] + means[("低","低")]) - (means[("高","低")] + means[("低","高")])
            rp(f"  {d1}×{d2}: 交互={interaction:+.2f}pp | "
               f"{d1}低{d2}低={means[('低','低')]:+.2f}%  {d1}高{d2}高={means[('高','高')]:+.2f}%  "
               f"{d1}高{d2}低={means[('高','低')]:+.2f}%  {d1}低{d2}高={means[('低','高')]:+.2f}%")

# ═══════════════════════════════════════════════
# 汇总结论
# ═══════════════════════════════════════════════
rp("\n" + "=" * 90)
rp("【汇总结论】")
rp("=" * 90)

rp("\nIC跨horizon对比:")
rp(f"  {'维度':>4} | {'5日IC':>8} | {'10日IC':>8} | {'20日IC':>8}")
rp(f"  {'─'*40}")
for dim, (col, _) in dim_config.items():
    ics = []
    for h in HORIZONS:
        dfh = df[df["horizon"] == h].copy()
        ics.append(dfh[col].corr(dfh["excess"], method="spearman"))
    rp(f"  {dim:>4} | {ics[0]:+8.4f} | {ics[1]:+8.4f} | {ics[2]:+8.4f}")

rp("\nC3高-低组超额跨horizon:")
rp(f"  {'Horizon':>8} | {'低组均值':>8} | {'高组均值':>8} | {'高-低':>6} | {'低组胜率':>8} | {'高组胜率':>8}")
rp(f"  {'─'*60}")
for h in HORIZONS:
    dfh = df[df["horizon"] == h].copy()
    dfh["group"] = pd.qcut(dfh["avg_pct"], q=3, labels=LABELS, duplicates="drop")
    grp = dfh.groupby("group", observed=True)["excess"]
    means = grp.mean()
    wr = grp.apply(lambda x: (x > 0).mean())
    spread = means.get("高", 0) - means.get("低", 0)
    rp(f"  {h:>8} | {means.get('低',0):+8.2f}% | {means.get('高',0):+8.2f}% | {spread:+6.2f} | {wr.get('低',0):7.1%} | {wr.get('高',0):7.1%}")

# ─── 保存 ───
report_path = OUT_DIR / "s0_dimension_multi_horizon_report.txt"
with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(report_lines))
rp(f"\n报告已保存: {report_path}")
