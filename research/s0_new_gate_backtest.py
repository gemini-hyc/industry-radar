#!/usr/bin/env python3
"""
S₀ 新门槛策略回测：对比三种方案
================================
方案 A（基准）：原始 AND 五条件 + 等权S₀ (C1/C2/C3/C4 各25满分)
方案 B：       原始 AND 五条件 + 新权重S₀ (C3:60/C4:25/C1:10/C2:5)
方案 C：       新门槛 C3必须+任一其他 + C2阈值20pp + 新权重S₀

新门槛逻辑：
  C3 > 1.5% (必须)
  AND (C1 < 45% OR C2 > 20pp OR C4 > 1.15)  (任一其他)
  
注意：C0 (MA21<MA89) 无法从日数据计算，暂不包含。
      这意味着方案A的结果与原始回测可能有少量差异（原回测包含了C0过滤）。
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

# ═══════════════════════════════════════════
# 1. 加载 & 计算特征
# ═══════════════════════════════════════════
print("加载行业日数据...")
raw = pd.read_parquet(DATA_DIR / "industry_daily_full.parquet")
raw = raw.sort_values(['con_code','date']).reset_index(drop=True)

# 计算特征
raw['pct'] = raw['avg_pct']  # C3: 涨幅
raw['amt_ma20'] = raw.groupby('con_code')['total_amount'].transform(
    lambda x: x.rolling(20, min_periods=10).mean()
)
raw['amt_ratio'] = raw['total_amount'] / raw['amt_ma20']  # C4: 量比
raw['bw20_pct'] = raw.groupby('con_code')['breadth'].transform(
    lambda x: x.rolling(20, min_periods=10).mean()
) * 100  # C1: 前20日均宽(%)
raw['bw20_mean'] = raw.groupby('con_code')['breadth'].transform(
    lambda x: x.rolling(20, min_periods=10).mean()
)
raw['bw_delta'] = (raw['breadth'] - raw['bw20_mean']) * 100  # C2: 宽度跳升(pp)

# 清理无效行
raw = raw.dropna(subset=['bw20_pct','bw_delta','pct','amt_ratio']).reset_index(drop=True)
print(f"  有效行: {len(raw)}, 行业: {raw['con_code'].nunique()}, 日期: {raw['date'].min()} ~ {raw['date'].max()}")

# ═══════════════════════════════════════════
# 2. 唤醒事件识别
# ═══════════════════════════════════════════
print("\n识别唤醒事件...")

# AND 五条件（不含C0）
gate_and = (
    (raw['bw20_pct'] < 45) &
    (raw['bw_delta'] > 12) &
    (raw['pct'] > 1.5) &
    (raw['amt_ratio'] > 1.15)
)

# 新门槛：C3必须 + 任一其他（C2阈值20pp）
gate_new = (
    (raw['pct'] > 1.5) &
    ((raw['bw20_pct'] < 45) | (raw['bw_delta'] > 20) | (raw['amt_ratio'] > 1.15))
)

raw['wake_and'] = gate_and
raw['wake_new'] = gate_new

events_and = raw[raw['wake_and']].copy()
events_new = raw[raw['wake_new']].copy()

print(f"  AND 唤醒事件: {len(events_and)}")
print(f"  新门槛唤醒事件: {len(events_new)}")

# 分析差异
keys_and = set(zip(events_and['con_code'], events_and['date']))
keys_new = set(zip(events_new['con_code'], events_new['date']))
only_new = keys_new - keys_and
only_and = keys_and - keys_new
common = keys_and & keys_new
print(f"  共同事件: {len(common)}")
print(f"  AND独有（被新门槛排除）: {len(only_and)}")
print(f"  新门槛独有（AND不会触发）: {len(only_new)}")

# 分析新门槛独有事件的特征
if only_new:
    new_only_df = events_new[events_new.apply(lambda r: (r['con_code'], r['date']) in only_new, axis=1)]
    print(f"\n  新门槛独有事件特征:")
    print(f"    C1 bw20_pct: 均值={new_only_df['bw20_pct'].mean():.1f}%, >=45%: {(new_only_df['bw20_pct']>=45).sum()} ({(new_only_df['bw20_pct']>=45).mean():.1%})")
    print(f"    C2 bw_delta: 均值={new_only_df['bw_delta'].mean():.1f}pp, >20pp: {(new_only_df['bw_delta']>20).sum()} ({(new_only_df['bw_delta']>20).mean():.1%}), 12-20pp: {((new_only_df['bw_delta']>12)&(new_only_df['bw_delta']<=20)).sum()}")
    print(f"    C3 pct: 均值={new_only_df['pct'].mean():.2f}%, >1.5%: {(new_only_df['pct']>1.5).sum()} ({(new_only_df['pct']>1.5).mean():.1%})")
    print(f"    C4 amt_ratio: 均值={new_only_df['amt_ratio'].mean():.2f}x, >1.15: {(new_only_df['amt_ratio']>1.15).sum()} ({(new_only_df['amt_ratio']>1.15).mean():.1%})")
    
    # 哪些条件让它们通过新门槛？
    c1_only = (new_only_df['bw20_pct'] < 45) & (new_only_df['bw_delta'] <= 12) & (new_only_df['amt_ratio'] <= 1.15)
    c2_only = (new_only_df['bw_delta'] > 20) & (new_only_df['bw20_pct'] >= 45) & (new_only_df['amt_ratio'] <= 1.15)
    c4_only = (new_only_df['amt_ratio'] > 1.15) & (new_only_df['bw20_pct'] >= 45) & (new_only_df['bw_delta'] <= 12)
    c1_c4 = (new_only_df['bw20_pct'] < 45) & (new_only_df['amt_ratio'] > 1.15) & (new_only_df['bw_delta'] <= 12)
    c1_c2 = (new_only_df['bw20_pct'] < 45) & (new_only_df['bw_delta'] > 20) & (new_only_df['amt_ratio'] <= 1.15)
    c2_c4 = (new_only_df['bw_delta'] > 20) & (new_only_df['amt_ratio'] > 1.15) & (new_only_df['bw20_pct'] >= 45)
    all3 = (new_only_df['bw20_pct'] < 45) & (new_only_df['bw_delta'] > 20) & (new_only_df['amt_ratio'] > 1.15)
    print(f"\n  通过条件分析:")
    print(f"    仅C1(沉睡): {c1_only.sum()}")
    print(f"    仅C2(强跳升>20pp): {c2_only.sum()}")
    print(f"    仅C4(放量): {c4_only.sum()}")
    print(f"    C1+C4: {c1_c4.sum()}")
    print(f"    C1+C2: {c1_c2.sum()}")
    print(f"    C2+C4: {c2_c4.sum()}")
    print(f"    C1+C2+C4: {all3.sum()}")

# ═══════════════════════════════════════════
# 3. 计算持有期收益
# ═══════════════════════════════════════════
print("\n计算持有期收益...")

# 构建行业日收益查找表
# 使用原始回测数据中的已有收益（更精确）
bt = pd.read_csv("/Users/hyc/.hermes/quant/data/industry/wake_pure_backtest_detail.csv")
bt['wake_date'] = pd.to_datetime(bt['wake_date'])

# 从raw数据计算行业累计收益（用于不在回测中的新事件）
# 预计算每个行业每天的累计收益
def compute_cum_returns(df, horizons=[5, 10, 20]):
    """计算每个(code, date)的forward累计收益"""
    results = []
    for code in df['con_code'].unique():
        sub = df[df['con_code']==code].sort_values('date').reset_index(drop=True)
        prices = (1 + sub['avg_pct']/100).cumprod()
        for h in horizons:
            if len(sub) > h:
                cum = np.full(len(sub), np.nan)
                cum[:len(sub)-h] = ((prices.values[h:] / prices.values[:-h]) - 1) * 100
                sub[f'ind_cum_{h}'] = cum
        results.append(sub)
    return pd.concat(results, ignore_index=True)

raw_cum = compute_cum_returns(raw)

# 市场基准：所有行业等权平均的累计收益
def compute_bench_cum(df, horizons=[5, 10, 20]):
    """计算市场等权平均的forward累计收益"""
    # 先计算市场日收益
    mkt = df.groupby('date')['avg_pct'].mean().reset_index()
    mkt = mkt.sort_values('date').reset_index(drop=True)
    mkt_prices = (1 + mkt['avg_pct']/100).cumprod()
    
    results = []
    for h in horizons:
        if len(mkt) > h:
            cum = np.full(len(mkt), np.nan)
            cum[:len(mkt)-h] = ((mkt_prices.values[h:] / mkt_prices.values[:-h]) - 1) * 100
            mkt[f'bench_cum_{h}'] = cum
    results.append(mkt)
    return mkt

bench_cum = compute_bench_cum(raw)
bench_lookup = {}
for h in [5, 10, 20]:
    for _, row in bench_cum.iterrows():
        bench_lookup[(row['date'], h)] = row.get(f'bench_cum_{h}', np.nan)

# 构建行业累计收益查找表
ind_cum_lookup = {}
for h in [5, 10, 20]:
    col = f'ind_cum_{h}'
    if col in raw_cum.columns:
        for _, row in raw_cum.iterrows():
            ind_cum_lookup[(row['con_code'], row['date'], h)] = row[col]

# 先尝试使用回测数据中的收益
bt_lookup = {}
for _, row in bt.iterrows():
    key = (row['con_code'], row['wake_date'], row['horizon_target'])
    bt_lookup[key] = {'ind_cum': row['ind_cum'], 'bench_cum': row['bench_cum'], 'excess': row['excess']}

# ═══════════════════════════════════════════
# 4. 构建回测数据框
# ═══════════════════════════════════════════
print("构建回测数据框...")

def build_backtest_df(events_df, gate_name):
    """为给定唤醒事件构建回测数据框"""
    rows = []
    for _, ev in events_df.iterrows():
        for h in [5, 10, 20]:
            key = (ev['con_code'], ev['date'], h)
            # 优先使用回测数据
            if key in bt_lookup:
                ind_cum = bt_lookup[key]['ind_cum']
                bench_cum = bt_lookup[key]['bench_cum']
                excess = bt_lookup[key]['excess']
            else:
                # 从原始数据计算
                ind_cum = ind_cum_lookup.get(key, np.nan)
                bench_cum = bench_lookup.get((ev['date'], h), np.nan)
                if not (np.isnan(ind_cum) or np.isnan(bench_cum)):
                    excess = ind_cum - bench_cum
                else:
                    excess = np.nan
            
            rows.append({
                'con_code': ev['con_code'],
                'wake_date': ev['date'],
                'bw20_pct': ev['bw20_pct'],
                'bw_delta': ev['bw_delta'],
                'avg_pct': ev['pct'],
                'amt_ratio': ev['amt_ratio'],
                'horizon_target': h,
                'ind_cum': ind_cum,
                'bench_cum': bench_cum,
                'excess': excess,
                'year': ev['date'].year,
            })
    
    df = pd.DataFrame(rows)
    df = df.dropna(subset=['excess'])
    return df

df_and = build_backtest_df(events_and, "AND")
df_new = build_backtest_df(events_new, "NEW")

print(f"  AND 回测行: {len(df_and)}")
print(f"  新门槛回测行: {len(df_new)}")

# ═══════════════════════════════════════════
# 5. S₀ 打分
# ═══════════════════════════════════════════

# 等权打分 (各25满分)
def score_c1_eq(bw20):
    if bw20 < 5:  return 25
    if bw20 < 10: return 20
    if bw20 < 20: return 15
    if bw20 < 35: return 10
    if bw20 < 45: return 5
    return 0

def score_c2_eq(delta):
    if delta > 30: return 25
    if delta > 20: return 20
    if delta > 15: return 15
    if delta > 12: return 10
    return 0

def score_c3_eq(pct):
    if pct > 5:   return 25
    if pct > 3:   return 20
    if pct > 2:   return 15
    if pct > 1.5: return 10
    return 0

def score_c4_eq(ratio):
    if ratio > 3.0:  return 25
    if ratio > 2.0:  return 20
    if ratio > 1.5:  return 15
    if ratio > 1.15: return 10
    return 0

# 新权重打分
def score_c1_nw(bw20):
    if bw20 < 5:  return 10
    if bw20 < 10: return 7
    if bw20 < 20: return 4
    if bw20 < 35: return 2
    if bw20 < 45: return 1
    return 0

def score_c2_nw(delta):
    if delta > 30: return 5
    if delta > 20: return 4
    if delta > 15: return 2
    if delta > 12: return 1
    return 0

def score_c3_nw(pct):
    if pct > 5:   return 60
    if pct > 3:   return 45
    if pct > 2:   return 30
    if pct > 1.5: return 15
    return 0

def score_c4_nw(ratio):
    if ratio > 3.0:  return 25
    if ratio > 2.0:  return 18
    if ratio > 1.5:  return 10
    if ratio > 1.15: return 5
    return 0

# 方案A: AND + 等权
df_and['C1'] = df_and['bw20_pct'].apply(score_c1_eq)
df_and['C2'] = df_and['bw_delta'].apply(score_c2_eq)
df_and['C3'] = df_and['avg_pct'].apply(score_c3_eq)
df_and['C4'] = df_and['amt_ratio'].apply(score_c4_eq)
df_and['S0_A'] = (df_and['C1'] + df_and['C2'] + df_and['C3'] + df_and['C4']).clip(upper=100)

# 方案B: AND + 新权重
df_and['C1_nw'] = df_and['bw20_pct'].apply(score_c1_nw)
df_and['C2_nw'] = df_and['bw_delta'].apply(score_c2_nw)
df_and['C3_nw'] = df_and['avg_pct'].apply(score_c3_nw)
df_and['C4_nw'] = df_and['amt_ratio'].apply(score_c4_nw)
df_and['S0_B'] = (df_and['C1_nw'] + df_and['C2_nw'] + df_and['C3_nw'] + df_and['C4_nw']).clip(upper=100)

# 方案C: 新门槛 + 新权重 (C2阈值20pp在打分中已体现)
df_new['C1_nw'] = df_new['bw20_pct'].apply(score_c1_nw)
df_new['C2_nw'] = df_new['bw_delta'].apply(score_c2_nw)
df_new['C3_nw'] = df_new['avg_pct'].apply(score_c3_nw)
df_new['C4_nw'] = df_new['amt_ratio'].apply(score_c4_nw)
df_new['S0_C'] = (df_new['C1_nw'] + df_new['C2_nw'] + df_new['C3_nw'] + df_new['C4_nw']).clip(upper=100)

# ═══════════════════════════════════════════
# 6. 分层 & 报告
# ═══════════════════════════════════════════
print("\n" + "=" * 100)
print("生成报告...")

report = []
def rp(s=""):
    print(s)
    report.append(s)

rp("=" * 100)
rp("S₀ 三方案对比回测")
rp("=" * 100)
rp()
rp("方案 A（基准）：AND五条件 + 等权S₀ (C1/C2/C3/C4 各25满分)")
rp("方案 B：       AND五条件 + 新权重S₀ (C3:60/C4:25/C1:10/C2:5)")
rp("方案 C：       新门槛(C3必须+任一其他, C2阈值20pp) + 新权重S₀")
rp()
rp(f"数据范围: 2020 ~ 2026")
rp(f"注意: C0 (MA21<MA89) 未包含在此回测中（缺少均线数据），")
rp(f"      因此方案A的结果可能与原始回测（含C0）有少量差异。")
rp()

# 事件数
df_and_5 = df_and[df_and['horizon_target']==5]
df_new_5 = df_new[df_new['horizon_target']==5]
rp(f"方案A/B 事件数: {len(df_and_5)}")
rp(f"方案C 事件数: {len(df_new_5)} ({len(df_new_5)-len(df_and_5):+d} vs AND)")
rp()

# ─── S0分布 ───
rp("=" * 100)
rp("【1】S₀ 分布对比")
rp("=" * 100)
rp()
rp(f"方案A S0: 均值={df_and_5['S0_A'].mean():.1f}, 中位={df_and_5['S0_A'].median():.1f}, "
   f"25%={df_and_5['S0_A'].quantile(0.25):.1f}, 75%={df_and_5['S0_A'].quantile(0.75):.1f}")
rp(f"方案B S0: 均值={df_and_5['S0_B'].mean():.1f}, 中位={df_and_5['S0_B'].median():.1f}, "
   f"25%={df_and_5['S0_B'].quantile(0.25):.1f}, 75%={df_and_5['S0_B'].quantile(0.75):.1f}")
rp(f"方案C S0: 均值={df_new_5['S0_C'].mean():.1f}, 中位={df_new_5['S0_C'].median():.1f}, "
   f"25%={df_new_5['S0_C'].quantile(0.25):.1f}, 75%={df_new_5['S0_C'].quantile(0.75):.1f}")
rp()

# ─── 分层 ───
def assign_tiers(df, s0_col, q33, q66):
    """按S0分三层"""
    tiers = []
    for v in df[s0_col]:
        if v <= q33:
            tiers.append("低")
        elif v <= q66:
            tiers.append("中")
        else:
            tiers.append("高")
    return tiers

# 方案A分层
q33_A = df_and_5['S0_A'].quantile(0.33)
q66_A = df_and_5['S0_A'].quantile(0.66)
df_and['tier_A'] = assign_tiers(df_and, 'S0_A', q33_A, q66_A)

# 方案B分层
q33_B = df_and_5['S0_B'].quantile(0.33)
q66_B = df_and_5['S0_B'].quantile(0.66)
df_and['tier_B'] = assign_tiers(df_and, 'S0_B', q33_B, q66_B)

# 方案C分层
q33_C = df_new_5['S0_C'].quantile(0.33)
q66_C = df_new_5['S0_C'].quantile(0.66)
df_new['tier_C'] = assign_tiers(df_new, 'S0_C', q33_C, q66_C)

rp("=" * 100)
rp("【2】按 S₀ 三分位分层 — 超额收益对比")
rp("=" * 100)

for label, dfx, tier_col, q33, q66 in [
    ("方案A (AND+等权)", df_and, 'tier_A', q33_A, q66_A),
    ("方案B (AND+新权重)", df_and, 'tier_B', q33_B, q66_B),
    ("方案C (新门槛+新权重)", df_new, 'tier_C', q33_C, q66_C),
]:
    rp(f"\n  ── {label} ── (分位: 低≤{q33:.0f}, 中≤{q66:.0f}, 高>{q66:.0f})")
    rp(f"       层级  Horizon     N |     超额均值     超额胜率 |     绝对均值     绝对胜率 |      t值   显著性")
    rp(f"    ───────────────────────────────────────────────────────────────────────────")
    
    for tier in ["低", "中", "高"]:
        for h in [5, 10, 20]:
            sub = dfx[(dfx[tier_col] == tier) & (dfx['horizon_target'] == h)]
            if len(sub) < 3: continue
            exc_mean = sub['excess'].mean()
            exc_win = (sub['excess'] > 0).mean()
            abs_mean = sub['ind_cum'].mean()
            abs_win = (sub['ind_cum'] > 0).mean()
            t_val, p_val = ttest_1samp(sub['excess'].values)
            sig = "***" if p_val < 0.001 else "**" if p_val < 0.01 else "*" if p_val < 0.05 else ""
            rp(f"      {tier}S₀    {h:2d}日  {len(sub):5d} |   {exc_mean:+.2f}%   {exc_win:5.1%} |"
               f"   {abs_mean:+.2f}%   {abs_win:5.1%} | {t_val:+6.2f}   {sig}")
    
    # 高低差
    rp(f"\n    高低差 (高S₀ - 低S₀):")
    for h in [5, 10, 20]:
        low = dfx[(dfx[tier_col] == "低") & (dfx['horizon_target'] == h)]['excess']
        high = dfx[(dfx[tier_col] == "高") & (dfx['horizon_target'] == h)]['excess']
        if len(low) >= 3 and len(high) >= 3:
            diff_exc = high.mean() - low.mean()
            diff_win = (high > 0).mean() - (low > 0).mean()
            rp(f"       {h:2d}日: 超额差={diff_exc:+.2f}pp, 胜率差={diff_win:+.1%}")

# ─── Spearman IC 对比 ───
rp("\n" + "=" * 100)
rp("【3】S₀ 与超额收益的 Spearman IC 对比")
rp("=" * 100)
rp()
rp(f"  {'Horizon':>8s} | {'方案A IC':>10s} | {'方案B IC':>10s} | {'方案C IC':>10s} | {'B-A提升':>10s} | {'C-A提升':>10s} | {'C-B提升':>10s}")
rp(f"  {'─'*80}")

for h in [5, 10, 20]:
    sub_and = df_and[df_and['horizon_target'] == h]
    sub_new = df_new[df_new['horizon_target'] == h]
    
    ic_a = sub_and['S0_A'].corr(sub_and['excess'], method='spearman')
    ic_b = sub_and['S0_B'].corr(sub_and['excess'], method='spearman')
    ic_c = sub_new['S0_C'].corr(sub_new['excess'], method='spearman')
    
    rp(f"  {h:>5d}日 | {ic_a:+10.4f} | {ic_b:+10.4f} | {ic_c:+10.4f} | {ic_b-ic_a:+10.4f} | {ic_c-ic_a:+10.4f} | {ic_c-ic_b:+10.4f}")

# ─── 分层极差对比 ───
rp("\n" + "=" * 100)
rp("【4】分层极差对比（高S₀ - 低S₀ 超额均值差）")
rp("=" * 100)
rp()
rp(f"  {'Horizon':>8s} | {'A极差':>8s} {'A胜率差':>8s} | {'B极差':>8s} {'B胜率差':>8s} | {'C极差':>8s} {'C胜率差':>8s} | {'B-A':>8s} | {'C-A':>8s} | {'C-B':>8s}")
rp(f"  {'─'*85}")

for h in [5, 10, 20]:
    # A
    low_A = df_and[(df_and['tier_A'] == "低") & (df_and['horizon_target'] == h)]['excess']
    high_A = df_and[(df_and['tier_A'] == "高") & (df_and['horizon_target'] == h)]['excess']
    spread_A = high_A.mean() - low_A.mean() if len(low_A) >= 3 and len(high_A) >= 3 else np.nan
    winspread_A = (high_A > 0).mean() - (low_A > 0).mean() if len(low_A) >= 3 and len(high_A) >= 3 else np.nan
    
    # B
    low_B = df_and[(df_and['tier_B'] == "低") & (df_and['horizon_target'] == h)]['excess']
    high_B = df_and[(df_and['tier_B'] == "高") & (df_and['horizon_target'] == h)]['excess']
    spread_B = high_B.mean() - low_B.mean() if len(low_B) >= 3 and len(high_B) >= 3 else np.nan
    winspread_B = (high_B > 0).mean() - (low_B > 0).mean() if len(low_B) >= 3 and len(high_B) >= 3 else np.nan
    
    # C
    low_C = df_new[(df_new['tier_C'] == "低") & (df_new['horizon_target'] == h)]['excess']
    high_C = df_new[(df_new['tier_C'] == "高") & (df_new['horizon_target'] == h)]['excess']
    spread_C = high_C.mean() - low_C.mean() if len(low_C) >= 3 and len(high_C) >= 3 else np.nan
    winspread_C = (high_C > 0).mean() - (low_C > 0).mean() if len(low_C) >= 3 and len(high_C) >= 3 else np.nan
    
    ba = spread_B - spread_A if not (np.isnan(spread_B) or np.isnan(spread_A)) else np.nan
    ca = spread_C - spread_A if not (np.isnan(spread_C) or np.isnan(spread_A)) else np.nan
    cb = spread_C - spread_B if not (np.isnan(spread_C) or np.isnan(spread_B)) else np.nan
    
    rp(f"  {h:>5d}日 | {spread_A:>+7.2f}pp {winspread_A:>+7.1%} |"
       f" {spread_B:>+7.2f}pp {winspread_B:>+7.1%} |"
       f" {spread_C:>+7.2f}pp {winspread_C:>+7.1%} |"
       f" {ba:>+7.2f}pp | {ca:>+7.2f}pp | {cb:>+7.2f}pp")

# ─── 高S₀组累计超额 ───
rp("\n" + "=" * 100)
rp("【5】高S₀组逐年超额收益对比")
rp("=" * 100)

for h in [5, 10, 20]:
    rp(f"\n  ── {h}日持有 ──")
    
    # A
    high_A = df_and[(df_and['tier_A'] == "高") & (df_and['horizon_target'] == h)]
    yr_A = high_A.groupby('year')['excess'].agg(['mean','count',lambda x:(x>0).mean()])
    yr_A.columns = ['avg','n','win']
    
    # B
    high_B = df_and[(df_and['tier_B'] == "高") & (df_and['horizon_target'] == h)]
    yr_B = high_B.groupby('year')['excess'].agg(['mean','count',lambda x:(x>0).mean()])
    yr_B.columns = ['avg','n','win']
    
    # C
    high_C = df_new[(df_new['tier_C'] == "高") & (df_new['horizon_target'] == h)]
    yr_C = high_C.groupby('year')['excess'].agg(['mean','count',lambda x:(x>0).mean()])
    yr_C.columns = ['avg','n','win']
    
    rp(f"  {'年份':>6s} | {'A超额':>8s} {'A胜率':>7s} {'AN':>5s} | {'B超额':>8s} {'B胜率':>7s} {'BN':>5s} | {'C超额':>8s} {'C胜率':>7s} {'CN':>5s}")
    rp(f"  {'─'*85}")
    
    all_years = sorted(set(yr_A.index) | set(yr_B.index) | set(yr_C.index))
    for y in all_years:
        def fmt_yr(yr_df, y):
            if y in yr_df.index:
                r = yr_df.loc[y]
                return f"{r['avg']:>+7.2f}% {r['win']:>6.1%} {r['n']:>5.0f}"
            return f"{'—':>8s} {'—':>7s} {'—':>5s}"
        
        rp(f"  {y:>6d} | {fmt_yr(yr_A,y)} | {fmt_yr(yr_B,y)} | {fmt_yr(yr_C,y)}")
    
    # 全期汇总
    rp(f"  {'─'*85}")
    def fmt_total(df_high):
        return f"{df_high['excess'].mean():>+7.2f}% {(df_high['excess']>0).mean():>6.1%} {len(df_high):>5d}"
    rp(f"  {'全期':>6s} | {fmt_total(high_A)} | {fmt_total(high_B)} | {fmt_total(high_C)}")
    
    rp(f"  累计超额合计: A={high_A['excess'].sum():>+.1f}% | B={high_B['excess'].sum():>+.1f}% | C={high_C['excess'].sum():>+.1f}%")

# ─── 新门槛独有事件表现 ───
rp("\n" + "=" * 100)
rp("【6】新门槛独有事件表现（AND不会触发的额外事件）")
rp("=" * 100)

# 标记新门槛独有事件
df_new['is_new_only'] = df_new.apply(
    lambda r: (r['con_code'], r['wake_date']) in only_new, axis=1
)
df_new_only = df_new[df_new['is_new_only']]

if len(df_new_only) > 0:
    rp(f"\n  新门槛独有事件数: {len(df_new_only[df_new_only['horizon_target']==5])}")
    rp(f"\n  {'Horizon':>8s} | {'N':>6s} | {'超额均值':>10s} {'胜率':>8s} | {'绝对均值':>10s} {'胜率':>8s} | {'t值':>8s} {'显著性':>6s}")
    rp(f"  {'─'*75}")
    
    for h in [5, 10, 20]:
        sub = df_new_only[df_new_only['horizon_target'] == h]
        if len(sub) < 3: 
            rp(f"  {h:>5d}日 | {len(sub):>6d} | 数据不足")
            continue
        exc_mean = sub['excess'].mean()
        exc_win = (sub['excess'] > 0).mean()
        abs_mean = sub['ind_cum'].mean()
        abs_win = (sub['ind_cum'] > 0).mean()
        t_val, p_val = ttest_1samp(sub['excess'].values)
        sig = "***" if p_val < 0.001 else "**" if p_val < 0.01 else "*" if p_val < 0.05 else ""
        rp(f"  {h:>5d}日 | {len(sub):>6d} | {exc_mean:>+9.2f}% {exc_win:>7.1%} |"
           f" {abs_mean:>+9.2f}% {abs_win:>7.1%} | {t_val:>+7.2f}   {sig}")
    
    # 与AND共同事件对比
    rp(f"\n  与AND共同事件对比:")
    df_new_common = df_new[~df_new['is_new_only']]
    rp(f"  {'Horizon':>8s} | {'共同N':>6s} {'共同超额':>10s} {'共同胜率':>8s} | {'独有N':>6s} {'独有超额':>10s} {'独有胜率':>8s} | {'差':>7s}")
    rp(f"  {'─'*80}")
    
    for h in [5, 10, 20]:
        sub_common = df_new_common[df_new_common['horizon_target'] == h]
        sub_only = df_new_only[df_new_only['horizon_target'] == h]
        if len(sub_common) >= 3 and len(sub_only) >= 3:
            c_mean = sub_common['excess'].mean()
            c_win = (sub_common['excess'] > 0).mean()
            o_mean = sub_only['excess'].mean()
            o_win = (sub_only['excess'] > 0).mean()
            diff = o_mean - c_mean
            rp(f"  {h:>5d}日 | {len(sub_common):>6d} {c_mean:>+9.2f}% {c_win:>7.1%} |"
               f" {len(sub_only):>6d} {o_mean:>+9.2f}% {o_win:>7.1%} | {diff:>+6.2f}pp")
else:
    rp("  无新门槛独有事件")

# ─── 关键结论对比表 ───
rp("\n" + "=" * 100)
rp("【7】三方案核心指标汇总")
rp("=" * 100)
rp()
rp(f"  {'指标':>20s} | {'方案A':>12s} | {'方案B':>12s} | {'方案C':>12s}")
rp(f"  {'─'*70}")

# 5日 IC
ic_a5 = df_and[df_and['horizon_target']==5]['S0_A'].corr(df_and[df_and['horizon_target']==5]['excess'], method='spearman')
ic_b5 = df_and[df_and['horizon_target']==5]['S0_B'].corr(df_and[df_and['horizon_target']==5]['excess'], method='spearman')
ic_c5 = df_new[df_new['horizon_target']==5]['S0_C'].corr(df_new[df_new['horizon_target']==5]['excess'], method='spearman')
rp(f"  {'5日IC':>20s} | {ic_a5:>+11.4f} | {ic_b5:>+11.4f} | {ic_c5:>+11.4f}")

ic_a10 = df_and[df_and['horizon_target']==10]['S0_A'].corr(df_and[df_and['horizon_target']==10]['excess'], method='spearman')
ic_b10 = df_and[df_and['horizon_target']==10]['S0_B'].corr(df_and[df_and['horizon_target']==10]['excess'], method='spearman')
ic_c10 = df_new[df_new['horizon_target']==10]['S0_C'].corr(df_new[df_new['horizon_target']==10]['excess'], method='spearman')
rp(f"  {'10日IC':>20s} | {ic_a10:>+11.4f} | {ic_b10:>+11.4f} | {ic_c10:>+11.4f}")

ic_a20 = df_and[df_and['horizon_target']==20]['S0_A'].corr(df_and[df_and['horizon_target']==20]['excess'], method='spearman')
ic_b20 = df_and[df_and['horizon_target']==20]['S0_B'].corr(df_and[df_and['horizon_target']==20]['excess'], method='spearman')
ic_c20 = df_new[df_new['horizon_target']==20]['S0_C'].corr(df_new[df_new['horizon_target']==20]['excess'], method='spearman')
rp(f"  {'20日IC':>20s} | {ic_a20:>+11.4f} | {ic_b20:>+11.4f} | {ic_c20:>+11.4f}")

# 高S₀组5日超额均值 & 胜率
for h in [5, 10, 20]:
    high_A = df_and[(df_and['tier_A']=="高") & (df_and['horizon_target']==h)]
    high_B = df_and[(df_and['tier_B']=="高") & (df_and['horizon_target']==h)]
    high_C = df_new[(df_new['tier_C']=="高") & (df_new['horizon_target']==h)]
    
    rp(f"  {h}日高S₀超额均值 | {high_A['excess'].mean():>+10.2f}% | {high_B['excess'].mean():>+10.2f}% | {high_C['excess'].mean():>+10.2f}%")
    rp(f"  {h}日高S₀胜率     | {(high_A['excess']>0).mean():>11.1%} | {(high_B['excess']>0).mean():>11.1%} | {(high_C['excess']>0).mean():>11.1%}")
    rp(f"  {h}日高S₀事件数   | {len(high_A):>12d} | {len(high_B):>12d} | {len(high_C):>12d}")

# ─── 写入 ───
report_path = OUT_DIR / "s0_three_scheme_comparison.txt"
with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(report))
rp(f"\n报告已保存: {report_path}")
