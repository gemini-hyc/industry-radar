#!/usr/bin/env python3
"""
S₀ 维度消融实验
===============
测试不同维度组合的预测力：

变体:
  1. C3-only (raw avg_pct)        — 纯涨幅强度，连续值
  2. C3-only (tiered 60/45/30/15) — 涨幅强度分档
  3. C4-only (raw amt_ratio)      — 纯放量确认，连续值
  4. C4-only (tiered 25/18/10/5)  — 放量确认分档
  5. C3+C4 3:1 (tiered 75/25)     — 涨量组合，涨为主
  6. C3+C4 2:1 (tiered ~67/33)    — 涨量组合
  7. C3 60 + C4 25 (drop C1/C2)   — 新权重去C1C2
  8. 旧等权 C1-C4 (各25分)         — baseline
  9. 新IC权重 C1-C4 (60/25/10/5)  — current best

评估指标:
  - Spearman IC (S₀ vs excess)
  - 高S₀组超额均值 & 胜率
  - 分层极差 (高-低)
  - 累计超额合计
"""

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from math import sqrt, erf
from pathlib import Path

DATA_DIR = Path("/Users/hyc/.hermes/quant/data/industry")
OUT_DIR  = Path("/Users/hyc/.hermes/quant/output")
OUT_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH = DATA_DIR / "wake_pure_backtest_detail.csv"


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


# ─── 打分函数 ───
def score_c1(bw20, max_s):
    if bw20 < 5:  return max_s
    if bw20 < 10: return max_s * 0.8
    if bw20 < 20: return max_s * 0.6
    if bw20 < 35: return max_s * 0.4
    if bw20 < 45: return max_s * 0.2
    return 0

def score_c2(delta, max_s):
    if delta > 30: return max_s
    if delta > 20: return max_s * 0.8
    if delta > 15: return max_s * 0.6
    if delta > 12: return max_s * 0.4
    return 0

def score_c3(pct, max_s):
    if pct > 5:   return max_s
    if pct > 3:   return max_s * 0.75
    if pct > 2:   return max_s * 0.5
    if pct > 1.5: return max_s * 0.25
    return 0

def score_c4(ratio, max_s):
    if ratio > 3.0:  return max_s
    if ratio > 2.0:  return max_s * 0.72
    if ratio > 1.5:  return max_s * 0.4
    if ratio > 1.15: return max_s * 0.2
    return 0


# ─── 变体定义 ───
# (name, compute_fn(df) -> pd.Series)
# Each compute_fn returns a Series indexed same as df

def make_variants():
    """Returns list of (name, fn) where fn(df) -> S0 Series"""
    variants = []

    # V1: C3-only raw (continuous)
    variants.append(("C3-only (raw avg_pct)", lambda df: df["avg_pct"].copy()))

    # V2: C3-only tiered
    variants.append(("C3-only (tiered 100分)", lambda df: df["avg_pct"].apply(lambda x: score_c3(x, 100))))

    # V3: C4-only raw
    variants.append(("C4-only (raw amt_ratio)", lambda df: df["amt_ratio"].copy()))

    # V4: C4-only tiered
    variants.append(("C4-only (tiered 100分)", lambda df: df["amt_ratio"].apply(lambda x: score_c4(x, 100))))

    # V5: C3+C4 raw 3:1
    variants.append(("C3+C4 raw 3:1", lambda df: df["avg_pct"] * 3 + df["amt_ratio"] * 1))

    # V6: C3+C4 raw 2:1
    variants.append(("C3+C4 raw 2:1", lambda df: df["avg_pct"] * 2 + df["amt_ratio"] * 1))

    # V7: C3(60) + C4(25) tiered — drop C1/C2 from new weights
    variants.append(("C3(60)+C4(25) tiered", lambda df: (
        df["avg_pct"].apply(lambda x: score_c3(x, 60)) +
        df["amt_ratio"].apply(lambda x: score_c4(x, 25))
    )))

    # V8: C3(70)+C4(30) tiered — more weight on C3
    variants.append(("C3(72)+C4(28) tiered", lambda df: (
        df["avg_pct"].apply(lambda x: score_c3(x, 72)) +
        df["amt_ratio"].apply(lambda x: score_c4(x, 28))
    )))

    # V9: Old equal weight (4×25)
    variants.append(("旧等权 C1-C4 (各25)", lambda df: (
        df["bw20_pct"].apply(lambda x: score_c1(x, 25)) +
        df["bw_delta"].apply(lambda x: score_c2(x, 25)) +
        df["avg_pct"].apply(lambda x: score_c3(x, 25)) +
        df["amt_ratio"].apply(lambda x: score_c4(x, 25))
    ).clip(upper=100)))

    # V10: New IC weight (60/25/10/5)
    variants.append(("新IC权重 C1-C4 (60/25/10/5)", lambda df: (
        df["bw20_pct"].apply(lambda x: score_c1(x, 10)) +
        df["bw_delta"].apply(lambda x: score_c2(x, 5)) +
        df["avg_pct"].apply(lambda x: score_c3(x, 60)) +
        df["amt_ratio"].apply(lambda x: score_c4(x, 25))
    ).clip(upper=100)))

    return variants


# ─── 加载数据 ───
print("加载数据...")
df = pd.read_csv(CSV_PATH)
df5 = df[df["horizon_target"] == 5].copy()
n_events = df5.shape[0]
print(f"  {len(df)} 行, {n_events} 个唤醒事件 × 3持有期")

# ─── 为每个变体计算 S0, 分层, 评估 ───
variants = make_variants()

results = []  # list of dicts

for vname, vfn in variants:
    print(f"\n评估: {vname} ...", end=" ", flush=True)

    # Compute S0
    s0_all = vfn(df)
    s0_5 = vfn(df5)

    # Tier split on 5d events
    q33 = s0_5.quantile(0.33)
    q66 = s0_5.quantile(0.66)

    # Map tiers
    tier_map = {}
    for i, row in df5.iterrows():
        key = (row["con_code"], row["wake_date"])
        v = s0_5.loc[i]
        tier_map[key] = "低" if v <= q33 else ("高" if v > q66 else "中")

    tier_all = df.apply(lambda r: tier_map.get((r["con_code"], r["wake_date"]), "中"), axis=1)

    # ─── Evaluate ───
    r = {"name": vname, "q33": q33, "q66": q66,
         "s0_mean": s0_5.mean(), "s0_std": s0_5.std(),
         "s0_median": s0_5.median()}

    for h in [5, 10, 20]:
        sub = df[df["horizon_target"] == h].copy()
        sub["_s0"] = s0_all.loc[sub.index]
        sub["_tier"] = tier_all.loc[sub.index]

        # IC
        ic = sub["_s0"].corr(sub["excess"], method="spearman")
        r[f"ic_{h}d"] = ic

        # High tier stats
        high = sub[sub["_tier"] == "高"]
        low  = sub[sub["_tier"] == "低"]
        r[f"high_n_{h}d"]   = len(high)
        r[f"high_exc_{h}d"] = high["excess"].mean()
        r[f"high_win_{h}d"] = (high["excess"] > 0).mean()
        r[f"high_ind_{h}d"] = high["ind_cum"].mean()

        # Low tier stats
        r[f"low_exc_{h}d"]  = low["excess"].mean()
        r[f"low_win_{h}d"]  = (low["excess"] > 0).mean()

        # Spread
        r[f"spread_{h}d"] = high["excess"].mean() - low["excess"].mean()
        r[f"win_spread_{h}d"] = (high["excess"] > 0).mean() - (low["excess"] > 0).mean()

        # Cumulative excess (sum, for comparison)
        r[f"cum_exc_{h}d"] = high["excess"].sum()

        # t-test for high tier
        t_val, p_val = ttest_1samp(high["excess"].values, 0)
        r[f"high_t_{h}d"] = t_val
        r[f"high_p_{h}d"] = p_val

    results.append(r)
    print("done")

# ─── Print Report ───
report = []
def rp(s=""):
    print(s)
    report.append(s)

rp("=" * 120)
rp("S₀ 维度消融实验 — 完整对比")
rp("=" * 120)
rp(f"数据范围: {df['year'].min()} - {df['year'].max()}, 唤醒事件数: {n_events}")
rp()

# ─── Table 1: IC comparison ───
rp("=" * 120)
rp("【表1】Spearman IC 对比 (S₀ vs 超额收益)")
rp("=" * 120)
rp()
header = f"  {'变体':<35s} | {'IC 5日':>8s} | {'IC 10日':>8s} | {'IC 20日':>8s} | {'IC均值':>8s} | {'vs旧等权':>8s}"
rp(header)
rp(f"  {'─'*len(header)}")

old_ic_mean = None
for r in results:
    if "旧等权" in r["name"]:
        old_ic_mean = (r["ic_5d"] + r["ic_10d"] + r["ic_20d"]) / 3
        break

for r in results:
    ic_mean = (r["ic_5d"] + r["ic_10d"] + r["ic_20d"]) / 3
    vs_old = ic_mean - old_ic_mean if old_ic_mean is not None else 0
    marker = " ◀" if "C3-only" in r["name"] or "C3(" in r["name"] or "C3+" in r["name"] else ""
    rp(f"  {r['name']:<35s} | {r['ic_5d']:>+8.4f} | {r['ic_10d']:>+8.4f} | {r['ic_20d']:>+8.4f} | {ic_mean:>+8.4f} | {vs_old:>+8.4f}{marker}")

# ─── Table 2: High tier excess ───
rp()
rp("=" * 120)
rp("【表2】高S₀组超额收益对比")
rp("=" * 120)
rp()
header2 = f"  {'变体':<35s} | {'5日超额':>10s} {'5日胜率':>8s} {'5日N':>6s} | {'10日超额':>10s} {'10日胜率':>8s} {'10日N':>6s} | {'20日超额':>10s} {'20日胜率':>8s} {'20日N':>6s}"
rp(header2)
rp(f"  {'─'*len(header2)}")

for r in results:
    rp(f"  {r['name']:<35s} | {r['high_exc_5d']:>+9.2f}% {r['high_win_5d']:>7.1%} {r['high_n_5d']:>6d} | "
       f"{r['high_exc_10d']:>+9.2f}% {r['high_win_10d']:>7.1%} {r['high_n_10d']:>6d} | "
       f"{r['high_exc_20d']:>+9.2f}% {r['high_win_20d']:>7.1%} {r['high_n_20d']:>6d}")

# ─── Table 3: Spread ───
rp()
rp("=" * 120)
rp("【表3】分层极差对比 (高S₀ - 低S₀)")
rp("=" * 120)
rp()
header3 = f"  {'变体':<35s} | {'5日极差':>10s} {'5日胜率差':>9s} | {'10日极差':>10s} {'10日胜率差':>9s} | {'20日极差':>10s} {'20日胜率差':>9s}"
rp(header3)
rp(f"  {'─'*len(header3)}")

for r in results:
    rp(f"  {r['name']:<35s} | {r['spread_5d']:>+9.2f}pp {r['win_spread_5d']:>+8.1%} | "
       f"{r['spread_10d']:>+9.2f}pp {r['win_spread_10d']:>+8.1%} | "
       f"{r['spread_20d']:>+9.2f}pp {r['win_spread_20d']:>+8.1%}")

# ─── Table 4: Cumulative excess ───
rp()
rp("=" * 120)
rp("【表4】累计超额合计 (高S₀组, 简单求和)")
rp("=" * 120)
rp()
header4 = f"  {'变体':<35s} | {'5日累计':>12s} | {'10日累计':>12s} | {'20日累计':>12s}"
rp(header4)
rp(f"  {'─'*len(header4)}")

old_cum = {}
for r in results:
    if "旧等权" in r["name"]:
        old_cum = {h: r[f"cum_exc_{h}d"] for h in [5, 10, 20]}
        break

for r in results:
    improvements = ""
    if old_cum:
        parts = []
        for h in [5, 10, 20]:
            pct = (r[f"cum_exc_{h}d"] / old_cum[h] - 1) * 100 if old_cum[h] != 0 else 0
            parts.append(f"{pct:+.0f}%")
        improvements = f"  (vs旧等权: {' / '.join(parts)})"
    rp(f"  {r['name']:<35s} | {r['cum_exc_5d']:>+11.2f}% | {r['cum_exc_10d']:>+11.2f}% | {r['cum_exc_20d']:>+11.2f}%{improvements}")

# ─── Table 5: Yearly stability (for top variants) ───
rp()
rp("=" * 120)
rp("【表5】逐年稳健性 — 高S₀组超额均值 (仅展示Top变体)")
rp("=" * 120)

# Pick top variants by IC mean
top_variants = sorted(results, key=lambda r: (r["ic_5d"] + r["ic_10d"] + r["ic_20d"]) / 3, reverse=True)[:6]
top_names = {r["name"] for r in top_variants}
# Always include baselines
top_names.add("旧等权 C1-C4 (各25)")
top_names.add("新IC权重 C1-C4 (60/25/10/5)")

for h in [5, 10, 20]:
    rp(f"\n  ── {h}日持有 ──")
    years = sorted(df["year"].unique())

    # Header
    header_y = f"  {'年份':>6s}"
    for r in results:
        if r["name"] in top_names:
            header_y += f" | {r['name']:<28s}"
    rp(header_y)
    rp(f"  {'─'*len(header_y)}")

    for y in years:
        line = f"  {y:>6d}"
        for r in results:
            if r["name"] not in top_names:
                continue
            # Compute yearly stats for this variant
            s0_all_v = results[results.index(r)]["_s0_series"] if False else None
            # Recompute properly
            # We stored the s0 computation inline; let's recalc yearly
            pass

        rp(line)

    # Instead do it properly below
    rp(f"  (逐年数据见下方详细展开)")

# ─── Detailed yearly for key variants ───
rp()
rp("=" * 120)
rp("【表5-详细】逐年稳健性")
rp("=" * 120)

# Recompute yearly stats for the top variants
key_variants = [
    ("C3-only (raw avg_pct)", lambda df: df["avg_pct"].copy()),
    ("C3-only (tiered 100分)", lambda df: df["avg_pct"].apply(lambda x: score_c3(x, 100))),
    ("C3+C4 raw 3:1", lambda df: df["avg_pct"] * 3 + df["amt_ratio"] * 1),
    ("C3(60)+C4(25) tiered", lambda df: (
        df["avg_pct"].apply(lambda x: score_c3(x, 60)) +
        df["amt_ratio"].apply(lambda x: score_c4(x, 25))
    )),
    ("旧等权 C1-C4 (各25)", lambda df: (
        df["bw20_pct"].apply(lambda x: score_c1(x, 25)) +
        df["bw_delta"].apply(lambda x: score_c2(x, 25)) +
        df["avg_pct"].apply(lambda x: score_c3(x, 25)) +
        df["amt_ratio"].apply(lambda x: score_c4(x, 25))
    ).clip(upper=100)),
    ("新IC权重 C1-C4 (60/25/10/5)", lambda df: (
        df["bw20_pct"].apply(lambda x: score_c1(x, 10)) +
        df["bw_delta"].apply(lambda x: score_c2(x, 5)) +
        df["avg_pct"].apply(lambda x: score_c3(x, 60)) +
        df["amt_ratio"].apply(lambda x: score_c4(x, 25))
    ).clip(upper=100)),
]

for h in [5, 10, 20]:
    rp(f"\n  ── {h}日持有 ──")
    years = sorted(df["year"].unique())

    # Build header
    header_y = f"  {'年份':>6s}"
    for vname, _ in key_variants:
        short = vname[:24]
        header_y += f" | {short:>24s}"
    rp(header_y)
    rp(f"  {'─'*len(header_y)}")

    for y in years:
        line = f"  {y:>6d}"
        for vname, vfn in key_variants:
            sub_all = df[df["horizon_target"] == h].copy()
            sub_y = sub_all[sub_all["year"] == y]
            if len(sub_y) < 3:
                line += f" | {'N<3':>24s}"
                continue

            # Recompute S0 for this subset and tier
            s0_v = vfn(sub_y)
            s0_all_5 = vfn(df5)
            q33_v = s0_all_5.quantile(0.33)
            q66_v = s0_all_5.quantile(0.66)

            # Map tiers using the global 5d thresholds
            tier_map_v = {}
            for i, row in df5.iterrows():
                key = (row["con_code"], row["wake_date"])
                tv = s0_all_5.loc[i]
                tier_map_v[key] = "低" if tv <= q33_v else ("高" if tv > q66_v else "中")

            sub_y_tier = sub_y.apply(
                lambda r: tier_map_v.get((r["con_code"], r["wake_date"]), "中"), axis=1
            )
            high_y = sub_y[sub_y_tier == "高"]

            if len(high_y) >= 3:
                exc_m = high_y["excess"].mean()
                win_m = (high_y["excess"] > 0).mean()
                line += f" | {exc_m:>+7.2f}%({win_m:.0%}){len(high_y):>3d}"
            else:
                line += f" | {'N<3':>24s}"
        rp(line)

# ─── Distribution analysis ───
rp()
rp("=" * 120)
rp("【表6】S₀分布特征对比")
rp("=" * 120)
header6 = f"  {'变体':<35s} | {'均值':>8s} | {'中位':>8s} | {'标准差':>8s} | {'偏度':>8s} | {'变异系数':>8s}"
rp(header6)
rp(f"  {'─'*len(header6)}")

for r in results:
    rp(f"  {r['name']:<35s} | {r['s0_mean']:>8.2f} | {r['s0_median']:>8.2f} | "
       f"{r['s0_std']:>8.2f} | {r.get('s0_skew', 0):>8.2f} | "
       f"{r['s0_std']/r['s0_mean'] if r['s0_mean'] > 0 else 0:>8.3f}")

# ─── Correlation between variants ───
rp()
rp("=" * 120)
rp("【表7】C3-only vs C1/C2/C4 的交叉信息")
rp("=" * 120)
rp()

# How much does C3 alone explain the 4-dim score?
c3_raw = df5["avg_pct"]
c1_s = df5["bw20_pct"].apply(lambda x: score_c1(x, 25))
c2_s = df5["bw_delta"].apply(lambda x: score_c2(x, 25))
c3_s = df5["avg_pct"].apply(lambda x: score_c3(x, 25))
c4_s = df5["amt_ratio"].apply(lambda x: score_c4(x, 25))
old_s0 = (c1_s + c2_s + c3_s + c4_s).clip(upper=100)

rp(f"  avg_pct(原始值) vs 旧S₀  Pearson r = {c3_raw.corr(old_s0):.4f}")
rp(f"  avg_pct(原始值) vs C3分档  Pearson r = {c3_raw.corr(c3_s):.4f}")
rp(f"  avg_pct(原始值) vs C1分档  Pearson r = {c3_raw.corr(c1_s):.4f}")
rp(f"  avg_pct(原始值) vs C2分档  Pearson r = {c3_raw.corr(c2_s):.4f}")
rp(f"  avg_pct(原始值) vs C4分档  Pearson r = {c3_raw.corr(c4_s):.4f}")
rp()
rp(f"  C1分档 vs C3分档         r = {c1_s.corr(c3_s):.4f}  (沉睡深度 vs 涨幅强度)")
rp(f"  C2分档 vs C3分档         r = {c2_s.corr(c3_s):.4f}  (跳升力度 vs 涨幅强度)")
rp(f"  C4分档 vs C3分档         r = {c4_s.corr(c3_s):.4f}  (放量确认 vs 涨幅强度)")

# ─── OLS: does adding C4 help beyond C3? ───
rp()
rp("=" * 120)
rp("【表8】增量贡献检验：加入C4是否在C3之上显著改善IC？")
rp("=" * 120)

from scipy import stats as sp_stats

for h in [5, 10, 20]:
    sub = df[df["horizon_target"] == h].copy()

    # C3-only IC
    s0_c3 = sub["avg_pct"]
    ic_c3 = s0_c3.corr(sub["excess"], method="spearman")

    # C3 + C4 combo (raw 3:1)
    s0_c3c4 = sub["avg_pct"] * 3 + sub["amt_ratio"] * 1
    ic_c3c4 = s0_c3c4.corr(sub["excess"], method="spearman")

    # C3 + C4 combo (raw 1:1)
    s0_c3c4_11 = sub["avg_pct"] + sub["amt_ratio"]
    ic_c3c4_11 = s0_c3c4_11.corr(sub["excess"], method="spearman")

    # New IC weight 4-dim
    s0_new = (
        sub["bw20_pct"].apply(lambda x: score_c1(x, 10)) +
        sub["bw_delta"].apply(lambda x: score_c2(x, 5)) +
        sub["avg_pct"].apply(lambda x: score_c3(x, 60)) +
        sub["amt_ratio"].apply(lambda x: score_c4(x, 25))
    ).clip(upper=100)
    ic_new = s0_new.corr(sub["excess"], method="spearman")

    rp(f"\n  {h}日持有:")
    rp(f"    C3-only (raw avg_pct)              IC = {ic_c3:+.4f}")
    rp(f"    C3+C4 raw 3:1                       IC = {ic_c3c4:+.4f}  (Δ vs C3-only: {ic_c3c4 - ic_c3:+.4f})")
    rp(f"    C3+C4 raw 1:1                       IC = {ic_c3c4_11:+.4f}  (Δ vs C3-only: {ic_c3c4_11 - ic_c3:+.4f})")
    rp(f"    新IC权重 C1-C4 (60/25/10/5)         IC = {ic_new:+.4f}  (Δ vs C3-only: {ic_new - ic_c3:+.4f})")

# ─── 最优C3:C4比例网格搜索 ───
rp()
rp("=" * 120)
rp("【表9】C3:C4 权重比例网格搜索 (tiered scoring)")
rp("=" * 120)
rp()

grid_results = []
for c3_w in [50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100]:
    c4_w = 100 - c3_w
    s0_all = df["avg_pct"].apply(lambda x: score_c3(x, c3_w)) + df["amt_ratio"].apply(lambda x: score_c4(x, c4_w))
    s0_5 = df5["avg_pct"].apply(lambda x: score_c3(x, c3_w)) + df5["amt_ratio"].apply(lambda x: score_c4(x, c4_w))

    q33_g = s0_5.quantile(0.33)
    q66_g = s0_5.quantile(0.66)

    tier_map_g = {}
    for i, row in df5.iterrows():
        key = (row["con_code"], row["wake_date"])
        tier_map_g[key] = "低" if s0_5.loc[i] <= q33_g else ("高" if s0_5.loc[i] > q66_g else "中")

    tier_all_g = df.apply(lambda r: tier_map_g.get((r["con_code"], r["wake_date"]), "中"), axis=1)

    ics = {}
    spreads = {}
    for h in [5, 10, 20]:
        sub = df[df["horizon_target"] == h]
        sub_s0 = s0_all.loc[sub.index]
        ic_g = sub_s0.corr(sub["excess"], method="spearman")
        ics[h] = ic_g

        sub_tier = tier_all_g.loc[sub.index]
        high_g = sub[sub_tier == "高"]["excess"]
        low_g = sub[sub_tier == "低"]["excess"]
        spreads[h] = high_g.mean() - low_g.mean()

    ic_mean = (ics[5] + ics[10] + ics[20]) / 3
    grid_results.append({
        "c3_w": c3_w, "c4_w": c4_w,
        "ic5": ics[5], "ic10": ics[10], "ic20": ics[20],
        "ic_mean": ic_mean,
        "sp5": spreads[5], "sp10": spreads[10], "sp20": spreads[20],
    })

    rp(f"  C3={c3_w:>3d} / C4={c4_w:>3d}  |  IC: {ics[5]:>+.4f}  {ics[10]:>+.4f}  {ics[20]:>+.4f}  (均值{ic_mean:+.4f})  |  极差: {spreads[5]:>+.2f}  {spreads[10]:>+.2f}  {spreads[20]:>+.2f}")

# Find best by IC mean
best = max(grid_results, key=lambda g: g["ic_mean"])
rp(f"\n  ★ 最优比例: C3={best['c3_w']}/{best['c4_w']}  (IC均值={best['ic_mean']:+.4f})")

# ─── Save ───
report_path = OUT_DIR / "s0_dimension_ablation.txt"
with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(report))
rp(f"\n报告已保存: {report_path}")
