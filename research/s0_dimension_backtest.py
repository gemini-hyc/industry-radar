#!/usr/bin/env python3
"""
S₀ 四维度独立性回测
===================
对 C1(沉睡深度)、C2(跳升力度)、C3(涨幅强度)、C4(放量确认) 分别测试独立预测力。

数据源: wake_pure_backtest_detail.csv
输出:   s0_dimension_report.txt + 交互热力图(s0_interaction_heatmap.png)

分析方法:
  1. 单维度分层回测（分3组，看各horizon超额）
  2. 留一法（剔除某维度后重建S0，看分层变化）
  3. 逐步回归 + VIF
  4. 双维度交叉分析 (2x2)
"""

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import combinations

# ─── 路径 ───
DATA_DIR  = Path("/Users/hyc/.hermes/quant/data/industry")
OUT_DIR   = Path("/Users/hyc/.hermes/quant/output")
OUT_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH  = DATA_DIR / "wake_pure_backtest_detail.csv"

# ─── 维度分档 → 得分 (源自 SKILL.md:327-330) ───
def score_c1(bw20):
    """C1 沉睡深度: bw20_pct 越低分越高"""
    if bw20 < 5:   return 25
    if bw20 < 10:  return 20
    if bw20 < 20:  return 15
    if bw20 < 35:  return 10
    if bw20 < 45:  return 5
    return 0

def score_c2(delta):
    """C2 跳升力度: Δ越大分越高"""
    if delta > 30:  return 25
    if delta > 20:  return 20
    if delta > 15:  return 15
    if delta > 12:  return 10
    return 0

def score_c3(pct):
    """C3 涨幅强度: 涨幅越大分越高"""
    if pct > 5:   return 25
    if pct > 3:   return 20
    if pct > 2:   return 15
    if pct > 1.5: return 10
    return 0

def score_c4(ratio):
    """C4 放量确认: 额比越大分越高"""
    if ratio > 3.0:  return 25
    if ratio > 2.0:  return 20
    if ratio > 1.5:  return 15
    if ratio > 1.15: return 10
    return 0

SCORE_FUNCS = {"C1": score_c1, "C2": score_c2, "C3": score_c3, "C4": score_c4}

# ─── 加载数据 ───
df = pd.read_csv(CSV_PATH)
print(f"数据加载: {len(df)} 行, {df['con_code'].nunique()} 个行业, "
      f"{df['wake_date'].nunique()} 个唤醒日")

# 计算各维度得分
df["C1_score"] = df["bw20_pct"].apply(score_c1)
df["C2_score"] = df["bw_delta"].apply(score_c2)
df["C3_score"] = df["avg_pct"].apply(score_c3)
df["C4_score"] = df["amt_ratio"].apply(score_c4)
df["S0_calc"]  = df[["C1_score","C2_score","C3_score","C4_score"]].sum(axis=1).clip(upper=100)

# ─── 输出收集 ───
report_lines = []
def rp(s=""):
    print(s)
    report_lines.append(s)

rp("=" * 80)
rp("S₀ 四维度独立性回测报告")
rp("=" * 80)
rp(f"数据范围: {df['year'].min()} - {df['year'].max()}")
rp(f"唤醒事件数: {df.drop_duplicates(subset=['con_code','wake_date']).shape[0]}")
rp(f"总行数(含多horizon): {len(df)}")
rp("")

# ─────────────────────────────────────────────
# 1. 单维度分层回测
# ─────────────────────────────────────────────
rp("=" * 80)
rp("【1】单维度分层回测 (按维度原始值分3组)")
rp("=" * 80)

# 分位数切分
LABELS = ["低", "中", "高"]

dim_config = {
    "C1": ("bw20_pct", "沉睡深度(bw20越低越好)"),
    "C2": ("bw_delta", "跳升力度(Δ越大越好)"),
    "C3": ("avg_pct",  "涨幅强度(pct越大越好)"),
    "C4": ("amt_ratio","放量确认(额比越大越好)"),
}

ic_results = {}

for dim, (col, desc) in dim_config.items():
    rp(f"\n--- {dim}: {desc} ---")
    # 对每个事件只用一次(取horizon=20)，避免重复
    df20 = df[df["horizon"] == 20].copy()
    df20["group"] = pd.qcut(df20[col], q=3, labels=LABELS, duplicates="drop")

    grp_stats = df20.groupby("group", observed=True)["excess"].agg(
        ["count", "mean", "median", "std"]
    )
    grp_stats["win_rate"] = df20.groupby("group", observed=True)["excess"].apply(
        lambda x: (x > 0).mean()
    )
    grp_stats.columns = ["数量", "均值超额%", "中位超额%", "标准差", "胜率"]
    rp(grp_stats.to_string(float_format="%.2f"))

    # Spearman IC (维度原始值 vs 20日超额)
    ic = df20[col].corr(df20["excess"], method="spearman")
    rp(f"  Spearman IC (vs 20日超额): {ic:.4f}")
    ic_results[dim] = ic

    # 多 horizon
    rp("  各horizon超额均值:")
    for h in [5, 10, 20]:
        dfh = df[df["horizon"] == h].copy()
        dfh["group"] = pd.qcut(dfh[col], q=3, labels=LABELS, duplicates="drop")
        means = dfh.groupby("group", observed=True)["excess"].mean()
        rp(f"    h={h}: 低={means.get('低',0):.2f}%  中={means.get('中',0):.2f}%  高={means.get('高',0):.2f}%")

rp("\n--- IC 汇总 ---")
for dim, ic in sorted(ic_results.items(), key=lambda x: abs(x[1]), reverse=True):
    rp(f"  {dim}: IC = {ic:+.4f}")

# ─────────────────────────────────────────────
# 2. 留一法 (Leave-One-Out)
# ─────────────────────────────────────────────
rp("\n" + "=" * 80)
rp("【2】留一法: 剔除某维度后重建S0，看分层力变化")
rp("=" * 80)

df20 = df[df["horizon"] == 20].copy()

# 完整S0的分层力 (按S0分3组)
df20["S0_group"] = pd.qcut(df20["S0_calc"], q=3, labels=LABELS, duplicates="drop")
full_spread = df20.groupby("S0_group", observed=True)["excess"].mean()
full_range = full_spread.max() - full_spread.min()
rp(f"完整S0分层(3组超额): 低={full_spread.get('低',0):.2f}% 中={full_spread.get('中',0):.2f}% 高={full_spread.get('高',0):.2f}%  极差={full_range:.2f}%")

loo_results = {}
for leave_out in ["C1", "C2", "C3", "C4"]:
    remain = [f"{c}_score" for c in ["C1","C2","C3","C4"] if c != leave_out]
    s0_partial = df20[remain].sum(axis=1).clip(upper=75)  # 最高75(缺一个25)
    df20["S0_no" + leave_out] = s0_partial
    df20["S0_no" + leave_out + "_g"] = pd.qcut(s0_partial, q=3, labels=LABELS, duplicates="drop")
    spread = df20.groupby("S0_no" + leave_out + "_g", observed=True)["excess"].mean()
    rng = spread.max() - spread.min()
    delta_rng = full_range - rng
    rp(f"\n  去掉{leave_out}: 低={spread.get('低',0):.2f}% 中={spread.get('中',0):.2f}% 高={spread.get('高',0):.2f}%  极差={rng:.2f}%  (下降{delta_rng:.2f}pp)")
    loo_results[leave_out] = {"极差": rng, "增量贡献": delta_rng}

rp("\n--- 留一法汇总(增量贡献越大=该维度越重要) ---")
for dim, v in sorted(loo_results.items(), key=lambda x: x[1]["增量贡献"], reverse=True):
    rp(f"  去掉{dim}: 极差={v['极差']:.2f}%, 增量贡献={v['增量贡献']:.2f}pp")

# ─────────────────────────────────────────────
# 3. 回归分析 + VIF
# ─────────────────────────────────────────────
rp("\n" + "=" * 80)
rp("【3】OLS回归: C1~C4得分 → 20日超额收益")
rp("=" * 80)

try:
    from sklearn.linear_model import LinearRegression
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

try:
    import statsmodels.api as sm
    HAS_SM = True
except ImportError:
    HAS_SM = False

df20 = df[df["horizon"] == 20].copy()
X_cols = ["C1_score", "C2_score", "C3_score", "C4_score"]
X = df20[X_cols].values
y = df20["excess"].values

if HAS_SM:
    X_const = sm.add_constant(X)
    model = sm.OLS(y, X_const).fit()
    rp(model.summary2().tables[1].to_string(float_format="%.4f"))
    rp(f"\n  R² = {model.rsquared:.4f}, Adj R² = {model.rsquared_adj:.4f}")
    rp(f"  F-stat = {model.fvalue:.2f}, p = {model.f_pvalue:.4e}")

    # VIF
    from statsmodels.stats.outliers_influence import variance_inflation_factor
    rp("\n  VIF (方差膨胀因子):")
    for i, col in enumerate(X_cols):
        vif = variance_inflation_factor(X_const, i + 1)  # +1 for const
        rp(f"    {col}: {vif:.2f}")
elif HAS_SKLEARN:
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    reg = LinearRegression().fit(Xs, y)
    rp("  标准化系数:")
    for col, coef in zip(X_cols, reg.coef_):
        rp(f"    {col}: {coef:+.4f}")
    rp(f"  R² = {reg.score(Xs, y):.4f}")
    rp("  (安装statsmodels可获得完整回归统计+VIF)")
else:
    rp("  需安装 sklearn 或 statsmodels")

# ─────────────────────────────────────────────
# 4. 双维度交叉分析 (2×2)
# ─────────────────────────────────────────────
rp("\n" + "=" * 80)
rp("【4】双维度交叉分析 (2×2 高/低)")
rp("=" * 80)

df20 = df[df["horizon"] == 20].copy()

# 每个维度按中位数分高低
for dim, (col, desc) in dim_config.items():
    df20[dim + "_hilo"] = np.where(df20[col] > df20[col].median(), "高", "低")

pairs = list(combinations(["C1", "C2", "C3", "C4"], 2))
interaction_matrix = pd.DataFrame(index=["C1","C2","C3","C4"],
                                   columns=["C1","C2","C3","C4"], dtype=float)

for d1, d2 in pairs:
    g = df20.groupby([d1+"_hilo", d2+"_hilo"])["excess"].agg(["mean", "count"])
    rp(f"\n--- {d1} × {d2} ---")
    rp(f"  {'':>4} | {d2}低 | {d2}高")
    rp(f"  {'─'*20}")
    for v1 in ["低", "高"]:
        line = f"  {d1}{v1} |"
        for v2 in ["低", "高"]:
            key = (v1, v2)
            if key in g.index:
                m, n = g.loc[key, "mean"], g.loc[key, "count"]
                line += f" {m:+.2f}%({n}) |"
            else:
                line += "    N/A    |"
        rp(line)

    # 交互效应: (高高 + 低低) - (高低 + 低高)
    means = {}
    for v1 in ["低", "高"]:
        for v2 in ["低", "高"]:
            if (v1, v2) in g.index:
                means[(v1, v2)] = g.loc[(v1, v2), "mean"]

    if len(means) == 4:
        interaction = (means[("高","高")] + means[("低","低")]) - (means[("高","低")] + means[("低","高")])
        rp(f"  交互效应: {interaction:+.2f}pp")
        interaction_matrix.loc[d1, d2] = interaction
        interaction_matrix.loc[d2, d1] = interaction

rp("\n--- 交互效应热力图(数值) ---")
rp(interaction_matrix.fillna(0).round(2).to_string())

# 尝试画图
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import seaborn as sns

    fig, ax = plt.subplots(figsize=(6, 5))
    sns.heatmap(interaction_matrix.fillna(0).astype(float),
                annot=True, fmt=".2f", cmap="RdBu_r", center=0,
                xticklabels=["C1","C2","C3","C4"],
                yticklabels=["C1","C2","C3","C4"],
                ax=ax, linewidths=0.5)
    ax.set_title("S₀ 维度交互效应 (pp)")
    plt.tight_layout()
    fig.savefig(OUT_DIR / "s0_interaction_heatmap.png", dpi=150)
    rp(f"\n热力图已保存: {OUT_DIR / 's0_interaction_heatmap.png'}")
except Exception as e:
    rp(f"\n(画图失败: {e})")

# ─── 汇总结论 ───
rp("\n" + "=" * 80)
rp("【汇总结论】")
rp("=" * 80)
rp("\nIC 排名 (|IC|从大到小):")
for dim, ic in sorted(ic_results.items(), key=lambda x: abs(x[1]), reverse=True):
    rp(f"  {dim}: {ic:+.4f}")

rp("\n留一法增量贡献排名 (从大到小):")
for dim, v in sorted(loo_results.items(), key=lambda x: x[1]["增量贡献"], reverse=True):
    rp(f"  {dim}: {v['增量贡献']:.2f}pp")

rp("\n(回归系数与VIF见上方第3节)")

# ─── 写入文件 ───
report_path = OUT_DIR / "s0_dimension_report.txt"
with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(report_lines))
rp(f"\n报告已保存: {report_path}")
