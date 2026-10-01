#!/usr/bin/env python3
"""
宽度反转策略 v3 回测 — 严格复现小爱 detect_signals 逻辑
=====================================================

核心问题：
  1. 信号触发后是否有显著超额收益？
  2. 超额来自"扩张"还是"冷区均值回归"？
  3. 动态阈值调制是否有增量价值？
  4. 信号分级（high/standard/watch）是否有效？
  5. 恰好3天 vs 其他扩张天数的对比

五组实验：
  A. 完整信号（动态冷区 + 恰好3天扩张）
  B. 固定冷区（30% + 恰好3天扩张）
  C. 冷区无扩张（动态冷区，不限扩张）
  D. 纯扩张（恰好3天扩张，不限冷区）
  E. 全市场基线

数据：industry_daily_full.parquet (2020-01 ~ 2026-07, 131行业)
"""

import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from scipy import stats

# ── 路径 ──
DATA_DIR = '/Users/hyc/.hermes/quant/data'
IND_DAILY_PATH = f'{DATA_DIR}/industry/industry_daily_full.parquet'


# ═══════════════════════════════════════════════════════════════
# 一、数据加载与预处理
# ═══════════════════════════════════════════════════════════════

def load_and_prepare():
    """加载数据并计算所有需要的中间变量"""
    df = pd.read_parquet(IND_DAILY_PATH)
    df = df.sort_values(['con_code', 'date']).reset_index(drop=True)
    print(f"数据: {len(df)} 行, {df['con_code'].nunique()} 行业, "
          f"{df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}")

    # ── 全市场宽度 ──
    mkt_b = df.groupby('date')['breadth'].mean().reset_index()
    mkt_b.columns = ['date', 'market_breadth']
    df = df.merge(mkt_b, on='date', how='left')

    # ── 动态冷区阈值 ──
    df['cold_threshold'] = 0.30
    df.loc[df['market_breadth'] < 0.35, 'cold_threshold'] = 0.35
    df.loc[(df['market_breadth'] >= 0.55) & (df['market_breadth'] < 0.70), 'cold_threshold'] = 0.25
    df.loc[df['market_breadth'] >= 0.70, 'cold_threshold'] = 0.20

    # ── 宽度差分 + 连续扩张天数 ──
    df['breadth_diff'] = df.groupby('con_code')['breadth'].diff()
    df['is_expanding'] = df['breadth_diff'] > 0

    def _cc(s):
        r = np.zeros(len(s), dtype=int)
        c = 0
        for i in range(len(s)):
            if s.iloc[i]: c += 1; r[i] = c
            else: c = 0; r[i] = 0
        return pd.Series(r, index=s.index)

    df['expansion_days'] = df.groupby('con_code')['is_expanding'].transform(_cc)

    # ── 扩张起点前20日均宽（用于所有扩张天数） ──
    # 对于恰好第N天扩张的行，扩张起点 = 当前位置 - (N-1)
    # 然后取起点前20日的breadth均值
    df['exp_start_pre_breadth'] = np.nan
    for con_code, group in df.groupby('con_code', sort=False):
        idx = group.index
        exp_days = group['expansion_days'].values
        breadth_vals = group['breadth'].values
        result = np.full(len(exp_days), np.nan)
        for i in range(len(exp_days)):
            n = exp_days[i]
            if n >= 2:  # 扩张≥2天才有意义
                first_i = i - (n - 1)  # 扩张第1天的索引
                if first_i >= 20:
                    pre_slice = breadth_vals[first_i - 20 : first_i]
                    if len(pre_slice) >= 15:
                        result[i] = np.mean(pre_slice)
        df.loc[idx, 'exp_start_pre_breadth'] = result

    # ── 前20日均宽（滚动窗口，用于C组冷区判断） ──
    df['pre_breadth_20d'] = df.groupby('con_code')['breadth'].transform(
        lambda x: x.rolling(20, min_periods=15).mean().shift(1)
    )

    # ── 前向收益 ──
    market_daily = df.groupby('date')['avg_pct'].mean().reset_index()
    market_daily.columns = ['date', 'market_avg_pct']
    df = df.merge(market_daily, on='date', how='left')

    for n in [5, 10, 20]:
        df[f'fwd_{n}d_ret'] = df.groupby('con_code')['avg_pct'].transform(
            lambda x: x.rolling(n, min_periods=n).sum().shift(-n)
        )
        df[f'fwd_{n}d_mkt'] = df.groupby('con_code')['market_avg_pct'].transform(
            lambda x: x.rolling(n, min_periods=n).sum().shift(-n)
        )
        df[f'fwd_{n}d_excess'] = df[f'fwd_{n}d_ret'] - df[f'fwd_{n}d_mkt']

    # ── 信号分级 ──
    df['tier'] = 'watch'
    df.loc[df['exp_start_pre_breadth'] < 0.20, 'tier'] = 'standard'
    df.loc[df['exp_start_pre_breadth'] < 0.10, 'tier'] = 'high'

    print(f"  前向收益计算完成")
    return df


# ═══════════════════════════════════════════════════════════════
# 二、信号去重
# ═══════════════════════════════════════════════════════════════

def dedup(df, min_gap=5):
    """同一行业min_gap天内只保留首次信号"""
    if df.empty:
        return df
    df = df.sort_values(['con_code', 'date']).copy()
    df['prev_date'] = df.groupby('con_code')['date'].shift(1)
    df['gap'] = (df['date'] - df['prev_date']).dt.days
    df['keep'] = df['gap'].isna() | (df['gap'] > min_gap)
    return df[df['keep']].drop(columns=['prev_date', 'gap', 'keep']).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════
# 三、评估
# ═══════════════════════════════════════════════════════════════

def eval_group(df, name, horizons=(5, 10, 20)):
    print(f"\n{'─' * 70}")
    print(f"  {name}")
    print(f"{'─' * 70}")
    if df.empty:
        print("  ❌ 无数据")
        return {}

    res = {'group': name, 'n': len(df)}
    for n in horizons:
        col = f'fwd_{n}d_excess'
        ex = df[col].dropna()
        if len(ex) < 5:
            print(f"  {n}日超额: N={len(ex)}, 不足")
            continue

        mean = ex.mean()
        std = ex.std()
        win = (ex > 0).mean()
        ir = mean / std * np.sqrt(252) if std > 0 else 0
        t, p = stats.ttest_1samp(ex, 0)
        sig = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else ""

        print(f"  {n:2d}日超额: {mean:+.2f}%  IR={ir:.2f}  胜率={win:.1%}  t={t:+.2f}{sig}  N={len(ex)}")
        res[f'{n}d_excess'] = mean
        res[f'{n}d_ir'] = ir
        res[f'{n}d_win'] = win
        res[f'{n}d_t'] = t
        res[f'{n}d_p'] = p
    return res


def eval_tier(df, name):
    print(f"\n  📊 分级效果 ({name})")
    for tier in ['high', 'standard', 'watch']:
        sub = df[df['tier'] == tier]
        if sub.empty:
            print(f"    {tier:8s}: N=0")
            continue
        ex = sub['fwd_20d_excess'].dropna()
        if len(ex) < 3:
            print(f"    {tier:8s}: N={len(sub)}, 超额数据不足")
            continue
        t_val = stats.ttest_1samp(ex, 0)[0] if len(ex) > 1 else 0
        print(f"    {tier:8s}: N={len(sub):4d}  20日超额={ex.mean():+.2f}%  胜率={((ex>0).mean()):.1%}  t={t_val:+.2f}")


def eval_period(df, name):
    print(f"\n  📊 分期稳定性 ({name})")
    for label, start, end in [
        ('P1 结构牛', '2020-01-01', '2021-12-31'),
        ('P2 震荡熊', '2022-01-01', '2024-09-30'),
        ('P3 政策牛', '2024-10-01', '2026-12-31'),
    ]:
        sub = df[(df['date'] >= start) & (df['date'] <= end)]
        if sub.empty:
            print(f"    {label}: N=0")
            continue
        ex = sub['fwd_20d_excess'].dropna()
        if len(ex) < 3:
            print(f"    {label}: N={len(sub)}, 超额数据不足")
            continue
        print(f"    {label}: N={len(sub):4d}  20日超额={ex.mean():+.2f}%  胜率={((ex>0).mean()):.1%}")


# ═══════════════════════════════════════════════════════════════
# 四、主流程
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("宽度反转策略 v3 回测")
    print("=" * 70)

    df = load_and_prepare()

    # ── 构建实验组 ──
    # A: 完整信号（动态冷区 + 恰好3天扩张）
    day3 = df[df['expansion_days'] == 3].copy()
    A = day3[(day3['exp_start_pre_breadth'] <= day3['cold_threshold']) &
             day3['exp_start_pre_breadth'].notna()].copy()
    A = dedup(A)
    print(f"\nA(完整信号·动态冷区): {len(A)}")

    # B: 固定冷区（30% + 恰好3天扩张）
    B = day3[(day3['exp_start_pre_breadth'] <= 0.30) &
             day3['exp_start_pre_breadth'].notna()].copy()
    B = dedup(B)
    print(f"B(固定冷区30%): {len(B)}")

    # C: 冷区无扩张（动态冷区，不限扩张）
    #    用 pre_breadth_20d（滚动20日均宽）判断冷区
    C = df[(df['pre_breadth_20d'] <= df['cold_threshold']) &
           df['pre_breadth_20d'].notna()].copy()
    C = dedup(C)
    print(f"C(冷区无扩张): {len(C)}")

    # D: 纯扩张（恰好3天扩张，不限冷区）
    D = day3.copy()
    D = dedup(D)
    print(f"D(纯扩张): {len(D)}")

    # ═══════════════════════════════════════════════════════════
    # 一、核心结果
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("一、核心回测结果")
    print("=" * 70)

    for name, gdf in [('A_完整信号', A), ('B_固定冷区', B), ('C_冷区无扩张', C), ('D_纯扩张', D)]:
        eval_group(gdf, name)

    # E: 全市场基线
    print(f"\n{'─' * 70}")
    print(f"  E_全市场基线")
    print(f"{'─' * 70}")
    ex = df['fwd_20d_excess'].dropna()
    print(f"  20日超额: {ex.mean():+.2f}%  胜率={((ex>0).mean()):.1%}  N={len(ex)}")

    # ═══════════════════════════════════════════════════════════
    # 二、分级效果
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("二、信号分级效果（仅A组）")
    print("=" * 70)
    eval_tier(A, 'A_完整信号')

    # ═══════════════════════════════════════════════════════════
    # 三、分期稳定性
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("三、分期稳定性")
    print("=" * 70)
    eval_period(A, 'A_完整信号')
    eval_period(C, 'C_冷区无扩张')

    # ═══════════════════════════════════════════════════════════
    # 四、因果归因
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("四、因果归因：超额来自扩张还是冷区均值回归？")
    print("=" * 70)

    a_ex = A['fwd_20d_excess'].dropna()
    c_ex = C['fwd_20d_excess'].dropna()
    d_ex = D['fwd_20d_excess'].dropna()

    if len(a_ex) > 0 and len(c_ex) > 0:
        delta_ac = a_ex.mean() - c_ex.mean()
        print(f"  A(完整信号):  20日超额 = {a_ex.mean():+.2f}% (N={len(a_ex)})")
        print(f"  C(冷区无扩张): 20日超额 = {c_ex.mean():+.2f}% (N={len(c_ex)})")
        print(f"  扩张增量(A-C): {delta_ac:+.2f}%")
        if abs(delta_ac) < 0.3:
            print(f"  ⚠️ 增量极小，超额主要来自冷区均值回归")
        elif delta_ac > 0.5:
            print(f"  ✅ 扩张有实质正向增量")
        else:
            print(f"  ➡️ 扩张有轻微正向增量")

    if len(a_ex) > 0 and len(d_ex) > 0:
        delta_ad = a_ex.mean() - d_ex.mean()
        print(f"\n  D(纯扩张):  20日超额 = {d_ex.mean():+.2f}% (N={len(d_ex)})")
        print(f"  冷区增量(A-D): {delta_ad:+.2f}%")

    # 双重差分：D-C = 扩张与冷区的交互效应
    if len(d_ex) > 0 and len(c_ex) > 0:
        delta_dc = d_ex.mean() - c_ex.mean()
        print(f"\n  D-C(扩张 vs 冷区基线): {delta_dc:+.2f}%")

    # ═══════════════════════════════════════════════════════════
    # 五、动态 vs 固定阈值
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("五、动态阈值 vs 固定阈值")
    print("=" * 70)

    a_ex = A['fwd_20d_excess'].dropna()
    b_ex = B['fwd_20d_excess'].dropna()
    if len(a_ex) > 0 and len(b_ex) > 0:
        print(f"  A(动态阈值): {a_ex.mean():+.2f}%  IR={a_ex.mean()/a_ex.std()*np.sqrt(252):.2f}  胜率={((a_ex>0).mean()):.1%}  N={len(a_ex)}")
        print(f"  B(固定30%):  {b_ex.mean():+.2f}%  IR={b_ex.mean()/b_ex.std()*np.sqrt(252):.2f}  胜率={((b_ex>0).mean()):.1%}  N={len(b_ex)}")
        diff = a_ex.mean() - b_ex.mean()
        print(f"  差值(A-B): {diff:+.2f}%")
        if diff > 0.1:
            print(f"  ✅ 动态阈值优于固定")
        elif diff < -0.1:
            print(f"  ❌ 动态阈值不如固定30%")
        else:
            print(f"  ≈ 两者基本持平")

    # 按市场宽度分档
    print(f"\n  按市场宽度分档对比:")
    print(f"  {'档位':10s}  {'动态N':>6s}  {'动态超额':>8s}  {'动态胜率':>8s}  {'固定N':>6s}  {'固定超额':>8s}  {'固定胜率':>8s}")
    for lo, hi, label in [(0, 0.35, '宽<35%'), (0.35, 0.55, '宽35-55%'),
                           (0.55, 0.70, '宽55-70%'), (0.70, 1.01, '宽>70%')]:
        sub_a = A[(A['market_breadth'] >= lo) & (A['market_breadth'] < hi)]
        sub_b = B[(B['market_breadth'] >= lo) & (B['market_breadth'] < hi)]
        ex_a = sub_a['fwd_20d_excess'].dropna()
        ex_b = sub_b['fwd_20d_excess'].dropna()
        wa = ((ex_a > 0).mean()) if len(ex_a) > 0 else 0
        wb = ((ex_b > 0).mean()) if len(ex_b) > 0 else 0
        print(f"  {label:10s}  {len(sub_a):6d}  {ex_a.mean() if len(ex_a)>0 else 0:+8.2f}%  {wa:8.1%}  {len(sub_b):6d}  {ex_b.mean() if len(ex_b)>0 else 0:+8.2f}%  {wb:8.1%}")

    # ═══════════════════════════════════════════════════════════
    # 六、扩张天数敏感性
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("六、扩张天数敏感性")
    print("=" * 70)

    print(f"\n  {'天数':4s}  {'冷区N':>6s}  {'20日超额':>8s}  {'胜率':>6s}  {'t值':>7s}  {'全部N':>6s}  {'全部超额':>8s}  {'全部胜率':>8s}")
    for target in [2, 3, 4, 5, 7, 10]:
        day_n = df[df['expansion_days'] == target].copy()
        # 冷区+恰好第N天
        cold_n = day_n[(day_n['exp_start_pre_breadth'] <= day_n['cold_threshold']) &
                       day_n['exp_start_pre_breadth'].notna()].copy()
        cold_n = dedup(cold_n)
        ex_cold = cold_n['fwd_20d_excess'].dropna() if not cold_n.empty else pd.Series()
        # 全部恰好第N天
        all_n = dedup(day_n)
        ex_all = all_n['fwd_20d_excess'].dropna() if not all_n.empty else pd.Series()

        cold_ex = f"{ex_cold.mean():+.2f}%" if len(ex_cold) > 2 else "N/A"
        cold_win = f"{((ex_cold>0).mean()):.1%}" if len(ex_cold) > 2 else "N/A"
        cold_t = f"{stats.ttest_1samp(ex_cold,0)[0]:+.2f}" if len(ex_cold) > 5 else "N/A"
        all_ex = f"{ex_all.mean():+.2f}%" if len(ex_all) > 2 else "N/A"
        all_win = f"{((ex_all>0).mean()):.1%}" if len(ex_all) > 2 else "N/A"

        print(f"  {target:4d}  {len(cold_n):6d}  {cold_ex:>8s}  {cold_win:>6s}  {cold_t:>7s}  {len(all_n):6d}  {all_ex:>8s}  {all_win:>8s}")

    # ═══════════════════════════════════════════════════════════
    # 七、排列检验
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("七、排列检验（Permutation Test）")
    print("  H0: A组超额 = 从全市场随机抽取同量样本的均值")
    print("=" * 70)

    a_ex = A['fwd_20d_excess'].dropna()
    if len(a_ex) > 10:
        observed = a_ex.mean()
        all_excess = df['fwd_20d_excess'].dropna()
        rng = np.random.RandomState(42)
        n_perm = 2000
        perm_means = np.array([
            all_excess.sample(n=len(a_ex), random_state=rng).mean()
            for _ in range(n_perm)
        ])
        p_perm = (perm_means >= observed).mean()
        print(f"  A组观测超额: {observed:+.2f}%")
        print(f"  随机基线: {perm_means.mean():+.2f}% ± {perm_means.std():.2f}%")
        print(f"  p值: {p_perm:.3f}")
        if p_perm < 0.05:
            print(f"  ✅ 显著优于随机 (p<0.05)")
        else:
            print(f"  ❌ 不显著 (p≥0.05)")

    # 也对C组做排列检验
    c_ex = C['fwd_20d_excess'].dropna()
    if len(c_ex) > 10:
        observed_c = c_ex.mean()
        rng2 = np.random.RandomState(123)
        perm_means_c = np.array([
            all_excess.sample(n=len(c_ex), random_state=rng2).mean()
            for _ in range(n_perm)
        ])
        p_perm_c = (perm_means_c >= observed_c).mean()
        print(f"\n  C组观测超额: {observed_c:+.2f}%  p值: {p_perm_c:.3f}")
        if p_perm_c < 0.05:
            print(f"  ✅ C组也显著优于随机")
        else:
            print(f"  ❌ C组不显著")

    # A vs C 的直接检验
    if len(a_ex) > 5 and len(c_ex) > 5:
        t_ac, p_ac = stats.ttest_ind(a_ex, c_ex, equal_var=False)
        print(f"\n  A vs C 直接比较: t={t_ac:+.2f}, p={p_ac:.3f}")
        if p_ac < 0.05:
            print(f"  ✅ A显著优于C")
        else:
            print(f"  ❌ A与C差异不显著")

    # ═══════════════════════════════════════════════════════════
    # 八、信号频率与实用度
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("八、信号频率与实用度")
    print("=" * 70)

    A['month'] = A['date'].dt.to_period('M')
    monthly = A.groupby('month').size()
    print(f"  月均信号数: {monthly.mean():.1f}")
    print(f"  月信号数分布: min={monthly.min()}, median={monthly.median()}, max={monthly.max()}")
    print(f"  无信号月份: {(monthly == 0).sum()}/{len(monthly)}")

    high = A[A['tier'] == 'high']
    if not high.empty:
        high_monthly = high.groupby(high['date'].dt.to_period('M')).size()
        print(f"  high置信度月均: {high_monthly.mean():.1f}, 无信号月份: {(high_monthly==0).sum()}/{len(high_monthly)}")

    # 年度汇总
    A['year'] = A['date'].dt.year
    yearly = A.groupby('year').agg(
        count=('con_code', 'size'),
        excess_20d=('fwd_20d_excess', 'mean'),
        win_rate=('fwd_20d_excess', lambda x: (x > 0).mean())
    )
    print(f"\n  年度汇总:")
    print(f"  {'年份':6s}  {'信号数':>6s}  {'20日超额':>8s}  {'胜率':>6s}")
    for year, row in yearly.iterrows():
        print(f"  {year:6d}  {row['count']:6.0f}  {row['excess_20d']:+8.2f}%  {row['win_rate']:6.1%}")

    # ═══════════════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("📋 总结")
    print("=" * 70)

    a_ex = A['fwd_20d_excess'].dropna()
    c_ex = C['fwd_20d_excess'].dropna()
    b_ex = B['fwd_20d_excess'].dropna()

    print(f"\n  1. 信号整体效果:")
    if len(a_ex) > 0:
        print(f"     A组20日超额 = {a_ex.mean():+.2f}%, 胜率 = {(a_ex>0).mean():.1%}, IR = {a_ex.mean()/a_ex.std()*np.sqrt(252):.2f}")
        print(f"     t={stats.ttest_1samp(a_ex,0)[0]:+.2f}, p={stats.ttest_1samp(a_ex,0)[1]:.3f}")

    print(f"\n  2. 因果归因（扩张 vs 冷区均值回归）:")
    if len(a_ex) > 0 and len(c_ex) > 0:
        delta = a_ex.mean() - c_ex.mean()
        print(f"     扩张增量(A-C) = {delta:+.2f}%")
        if abs(delta) < 0.3:
            print(f"     ⚠️ 增量极小 — 超额主要来自冷区均值回归，扩张条件信息增量有限")
        elif delta > 0.5:
            print(f"     ✅ 扩张有实质增量")
        else:
            print(f"     ➡️ 扩张有轻微增量")

    print(f"\n  3. 动态阈值 vs 固定30%:")
    if len(a_ex) > 0 and len(b_ex) > 0:
        diff = a_ex.mean() - b_ex.mean()
        if diff > 0.1:
            print(f"     ✅ 动态优于固定 (差值={diff:+.2f}%)")
        elif diff < -0.1:
            print(f"     ❌ 动态不如固定30% (差值={diff:+.2f}%)")
        else:
            print(f"     ≈ 两者持平 (差值={diff:+.2f}%)")

    print(f"\n  4. 分级有效性:")
    if 'tier' in A.columns and len(A) > 0:
        tier_means = A.groupby('tier')['fwd_20d_excess'].mean()
        if 'high' in tier_means and 'watch' in tier_means:
            gap = tier_means['high'] - tier_means['watch']
            print(f"     high({tier_means['high']:+.2f}%) - watch({tier_means['watch']:+.2f}%) = {gap:+.2f}%")
            if gap > 0.3:
                print(f"     ✅ 分级有区分度")
            else:
                print(f"     ❌ 分级区分度不足")

    print(f"\n  5. 跨期稳定性:")
    # 简单判断三期的超额是否都为正
    period_excess = []
    for label, start, end in [
        ('P1', '2020-01-01', '2021-12-31'),
        ('P2', '2022-01-01', '2024-09-30'),
        ('P3', '2024-10-01', '2026-12-31'),
    ]:
        sub = A[(A['date'] >= start) & (A['date'] <= end)]
        ex = sub['fwd_20d_excess'].dropna()
        if len(ex) > 0:
            period_excess.append(ex.mean())
    positive_count = sum(1 for x in period_excess if x > 0)
    print(f"     {positive_count}/{len(period_excess)}期超额为正")
    if positive_count == len(period_excess):
        print(f"     ✅ 全期稳定")
    elif positive_count >= len(period_excess) - 1:
        print(f"     ➡️ 基本稳定，偶有失效")
    else:
        print(f"     ❌ 跨期不稳定")


if __name__ == '__main__':
    main()
