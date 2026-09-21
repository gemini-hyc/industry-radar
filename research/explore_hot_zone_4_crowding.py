"""
探索4: 拥挤度交互
问题: 热区×拥挤度 的分层预测力如何？

核心逻辑:
  - 拥挤度 = 行业20日换手率 / 60日换手率，全市场百分位 0-1
  - 高拥挤: 资金已经大量涌入 → 热区+高拥挤 = 接盘风险
  - 低拥挤: 资金尚未涌入 → 热区+低拥挤 = 可能有上车价值

方法:
  1. 行业拥挤度聚合（成分股拥挤度中位数）
  2. 四象限交互: 热区/冷区 × 高拥挤/低拥挤
  3. 各象限未来超额收益分布
  4. 逐步收紧阈值检查稳健性
"""

import pandas as pd
import numpy as np
import os

DATA_DIR = "/Users/hyc/.hermes/quant/data"
os.makedirs(f"{DATA_DIR}/factors", exist_ok=True)

# ── 加载行业日频数据 ──
daily = pd.read_parquet(f"{DATA_DIR}/industry/industry_daily_full.parquet")
daily["date"] = pd.to_datetime(daily["date"])
daily = daily.sort_values(["con_code", "date"]).reset_index(drop=True)

print(f"行业数据: {daily.shape[0]} 行, {daily['con_code'].nunique()} 行业")
print(f"日期: {daily['date'].min().date()} → {daily['date'].max().date()}")

# ── 加载拥挤度因子 & 行业映射 ──
crowd = pd.read_parquet(f"{DATA_DIR}/factors/ind_crowd_turnover_daily.parquet")
crowd["date"] = pd.to_datetime(crowd["date"])
print(f"拥挤度因子: {crowd.shape[0]} 行, {crowd['code'].nunique()} 只股票")
print(f"日期: {crowd['date'].min().date()} → {crowd['date'].max().date()}")

members = pd.read_parquet(f"{DATA_DIR}/industry/industry_members.parquet")
members = members.rename(columns={"stock_code": "code", "ind_code": "con_code"})

# ── 合并: code → con_code, 然后 industry-level aggregation ──
crowd_ind = crowd.merge(members[["code", "con_code"]], on="code", how="inner")
print(f"合并后: {crowd_ind.shape[0]} 行, {crowd_ind['con_code'].nunique()} 行业")

# 行业拥挤度 = 成分股拥挤度中位数
ind_crowd = crowd_ind.groupby(["date", "con_code"])["value"].median().reset_index()
ind_crowd.columns = ["date", "con_code", "crowd_median"]

# ── 合并到行业日频数据 ──
daily = daily.merge(ind_crowd, on=["date", "con_code"], how="left")
daily["crowd_median"] = daily["crowd_median"].fillna(0.5)  # 缺失填中性值

print(f"\n合并后有效行: {daily['crowd_median'].notna().sum()} / {len(daily)}")
print(f"拥挤度行业覆盖: {daily.dropna(subset=['crowd_median'])['con_code'].nunique()}")
print(f"拥挤度分布: median={daily['crowd_median'].median():.3f}, "
      f"mean={daily['crowd_median'].mean():.3f}")

# ── 市场平均 & 超额 ──
daily["mkt_pct"] = daily.groupby("date")["avg_pct"].transform("mean")
daily["excess_pct"] = daily["avg_pct"] - daily["mkt_pct"]

# ── 前向累计超额收益 ──
print("\n计算前向收益...")
for horizon in [5, 10, 20]:
    col = f"fwd_excess_{horizon}d"
    daily[col] = np.nan
    for code, grp in daily.groupby("con_code"):
        grp = grp.sort_values("date")
        rev = grp["excess_pct"].values[::-1]
        rev_sum = pd.Series(rev).rolling(horizon, min_periods=int(horizon * 0.7)).sum().values[::-1]
        rev_sum_shifted = np.roll(rev_sum, -1)
        rev_sum_shifted[-1] = np.nan
        daily.loc[grp.index, col] = rev_sum_shifted
    print(f"  T+{horizon} ✓")

# ═══════════════════════════════════════════════
# 四象限交互分类
# ═══════════════════════════════════════════════

# 宽度区间
daily["zone"] = "neutral"
daily.loc[daily["breadth"] > 0.70, "zone"] = "hot"
daily.loc[daily["breadth"] <= 0.30, "zone"] = "cold"

# 拥挤度区间（median split，每天截面内）
daily["crowd_tier"] = "mid"
daily["crowd_rank"] = daily.groupby("date")["crowd_median"].rank(pct=True)

# 默认用 50% 分位切，后续会试不同阈值
daily["crowd_high"] = daily["crowd_rank"] > 0.50

# 交互分类
daily["interaction"] = "other"
daily.loc[(daily["zone"] == "hot") & daily["crowd_high"], "interaction"] = "hot_high_crowd"
daily.loc[(daily["zone"] == "hot") & ~daily["crowd_high"], "interaction"] = "hot_low_crowd"
daily.loc[(daily["zone"] == "cold") & daily["crowd_high"], "interaction"] = "cold_high_crowd"
daily.loc[(daily["zone"] == "cold") & ~daily["crowd_high"], "interaction"] = "cold_low_crowd"
daily.loc[(daily["zone"] == "neutral") & daily["crowd_high"], "interaction"] = "neutral_high_crowd"
daily.loc[(daily["zone"] == "neutral") & ~daily["crowd_high"], "interaction"] = "neutral_low_crowd"

# ═══════════════════════════════════════════════
# 分析输出
# ═══════════════════════════════════════════════

def fmt(n, v):
    return f"{v.mean():>+7.2f}%  {v.median():>+7.2f}%  {(v>0).mean():>6.1%}  {len(v):>7}"

def write_sep(title):
    print(f"\n{'═'*80}\n{title}\n{'═'*80}")

# ── 1. 分布 ──
write_sep("一、六象限分布")
LABELS = [
    ("hot_high_crowd",     "🔥热区+高拥挤"),
    ("hot_low_crowd",      "🔥热区+低拥挤"),
    ("neutral_high_crowd", "⚪中性+高拥挤"),
    ("neutral_low_crowd",  "⚪中性+低拥挤"),
    ("cold_high_crowd",    "❄️冷区+高拥挤"),
    ("cold_low_crowd",     "❄️冷区+低拥挤"),
    ("other",              " 其他"),
]

for label, desc in LABELS:
    n = (daily["interaction"] == label).sum()
    pct = n / len(daily)
    print(f"  {desc:<26} {n:>7} ({pct:>5.1%})")

# ── 2. 全样本各象限未来收益 ──
write_sep("二、六象限未来超额收益（50%分位切拥挤度）")

for horizon in [5, 10, 20]:
    col = f"fwd_excess_{horizon}d"
    print(f"\n── T+{horizon} ──")
    print(f"  {'象限':<26} {'均值':>9} {'中位数':>9} {'胜率':>7} {'N':>7}")
    print(f"  {'─'*60}")
    for label, desc in LABELS:
        v = daily.loc[daily["interaction"] == label, col].dropna()
        if len(v) > 0:
            print(f"  {desc:<26} {fmt(None, v)}")

# ── 3. 关键对比: 热区高拥 vs 热区低拥 ──
write_sep("三、关键对比：热区内部的拥挤度效应")

for horizon in [5, 10, 20]:
    col = f"fwd_excess_{horizon}d"
    high = daily.loc[daily["interaction"] == "hot_high_crowd", col].dropna()
    low = daily.loc[daily["interaction"] == "hot_low_crowd", col].dropna()
    diff_mean = high.mean() - low.mean()
    diff_med = high.median() - low.median()
    print(f"\n  T+{horizon}: 高拥挤-低拥挤 = 均值{diff_mean:+.2f}%  中位数{diff_med:+.2f}%")

# ── 4. 收紧阈值: 热区用更极端分位 ──
write_sep("四、阈值稳健性：逐步收紧拥挤度分位")

for top_pct, desc in [(0.70, "Top 30%"), (0.80, "Top 20%"), (0.90, "Top 10%")]:
    print(f"\n── 拥挤度 > {top_pct:.0%} ({desc}) ──")
    high_mask = daily["crowd_rank"] > top_pct
    low_mask = daily["crowd_rank"] <= (1 - top_pct)  # symmetric bottom

    # Hot zone subsets
    hot_high = daily[(daily["zone"] == "hot") & high_mask]
    hot_low = daily[(daily["zone"] == "hot") & low_mask]
    cold_high = daily[(daily["zone"] == "cold") & high_mask]
    cold_low = daily[(daily["zone"] == "cold") & low_mask]

    for horizon in [10, 20]:
        col = f"fwd_excess_{horizon}d"
        print(f"  T+{horizon}:", end="")
        if len(hot_high) >= 10:
            v_hh = hot_high[col].dropna()
            v_hl = hot_low[col].dropna()
            v_ch = cold_high[col].dropna()
            v_cl = cold_low[col].dropna()
            print(f"  🔥高拥 {fmt(None, v_hh) if len(v_hh)>0 else 'n<10'}  "
                  f"🔥低拥 {fmt(None, v_hl) if len(v_hl)>0 else 'n<10'}  "
                  f"❄️高拥 {fmt(None, v_ch) if len(v_ch)>0 else 'n<10'}  "
                  f"❄️低拥 {fmt(None, v_cl) if len(v_cl)>0 else 'n<10'}")
        else:
            print(f"  n<10, 跳过")

# ── 5. 条件概率：热区 + 低拥挤 后续仍在热区的概率 ──
write_sep("五、拥挤度与宽度持续性")

for horizon in [5, 10, 20]:
    print(f"\n── T+{horizon} 时仍在热区的概率 ──")
    hot_today = daily[daily["zone"] == "hot"].copy()
    # 计算未来宽度
    for code, grp in hot_today.groupby("con_code"):
        grp = grp.sort_values("date")
        fwd_breadth = grp["breadth"].shift(-horizon)
        hot_today.loc[grp.index, f"fwd_breadth_{horizon}d"] = fwd_breadth

    for group_label, group_name in [
        ("hot_high_crowd", "🔥热区+高拥挤"),
        ("hot_low_crowd", "🔥热区+低拥挤"),
    ]:
        sub = hot_today[hot_today["interaction"] == group_label]
        col = f"fwd_breadth_{horizon}d"
        still_hot = (sub[col].dropna() > 0.70).mean()
        n_valid = sub[col].dropna().shape[0]
        mean_bw = sub[col].dropna().mean()
        print(f"  {group_name}: 仍热={still_hot:.1%}  T+{horizon}宽度均值={mean_bw:.1%}  N={n_valid}")

# ── 6. 拥挤度变化的增量信息 ──
write_sep("六、拥挤度变化（Δ拥挤度）的预测力")

# 计算 5 日拥挤度变化
daily["crowd_d5"] = daily.groupby("con_code")["crowd_median"].diff(5)

# 热区中，按拥挤度变化分组
hot = daily[daily["zone"] == "hot"].copy()
hot["crowd_direction"] = "flat"
hot.loc[hot["crowd_d5"] > 0.05, "crowd_direction"] = "crowding_up"    # 拥挤加剧
hot.loc[hot["crowd_d5"] < -0.05, "crowd_direction"] = "crowding_down"  # 拥挤缓解

for direction, desc in [
    ("crowding_up",   "拥挤加剧↑"),
    ("flat",          "拥挤稳定→"),
    ("crowding_down", "拥挤缓解↓"),
]:
    sub = hot[hot["crowd_direction"] == direction]
    print(f"\n  {desc} (热区内): n={len(sub)}")
    for horizon in [5, 10, 20]:
        col = f"fwd_excess_{horizon}d"
        v = sub[col].dropna()
        if len(v) >= 10:
            print(f"    T+{horizon}: " + fmt(None, v))
        else:
            print(f"    T+{horizon}: 样本不足")

# ── 7. 逐年稳定性 ──
write_sep("七、逐年稳定性：热区×低拥挤 vs 热区×高拥挤")
daily["year"] = pd.DatetimeIndex(daily["date"]).year

for horizon in [10, 20]:
    col = f"fwd_excess_{horizon}d"
    print(f"\n── T+{horizon} 超额中位数 by year ──")
    print(f"  {'Year':<6} {'🔥高拥挤':>12} {'🔥低拥挤':>12} {'diff':>8}")
    print(f"  {'─'*42}")
    for y in sorted(daily["year"].unique()):
        hh = daily.loc[(daily["interaction"] == "hot_high_crowd") & (daily["year"] == y), col].dropna().median()
        hl = daily.loc[(daily["interaction"] == "hot_low_crowd") & (daily["year"] == y), col].dropna().median()
        if not np.isnan(hh) and not np.isnan(hl):
            diff = hh - hl
            print(f"  {int(y)}    {hh:>+8.2f}%  {hl:>+8.2f}%  {diff:>+7.2f}%")

print("\n\n✅ 探索4 完成")
