#!/usr/bin/env python3
"""
宽度反转策略 v5 回测 — 统一15%冷区阈值
====================================

核心问题：
  1. 信号触发后是否有显著超额收益？
  2. 超额来自"扩张"还是"冷区均值回归"？
  3. 15%阈值调制是否有增量价值？
  4. 信号分级（high/standard/watch）是否有效？
  5. 最优扩张天数是多少？
  6. 策略跨期是否稳定？

统一15%冷区阈值回测
  A. 完整信号（15%冷区 + 恰好3天扩张）
  B. 固定冷区20%（对比用 + 恰好3天扩张）
  C. 冷区无扩张（15%冷区，不限扩张天数）
  D. 纯扩张（恰好3天扩张，不限冷区）
  E. 随机基线（蒙特卡洛）
  F. 扩张天数对比（2/3/4/5天）

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

# ── 热启动期 ──
WARMUP_START = '2020-01-02'
SIGNAL_START = '2020-07-01'


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

    # ── 统一冷区阈值 15% ──
    df['cold_threshold'] = 0.15

    # ── 宽度差分 + 连续扩张天数 ──
    df['breadth_diff'] = df.groupby('con_code')['breadth'].diff()
    df['is_expanding'] = df['breadth_diff'] > 0

    def _consecutive_count(s):
        r = np.zeros(len(s), dtype=int)
        c = 0
        for i in range(len(s)):
            if s.iloc[i]:
                c += 1
            else:
                c = 0
            r[i] = c
        return pd.Series(r, index=s.index)

    df['expansion_days'] = df.groupby('con_code')['is_expanding'].transform(_consecutive_count)

    # ── 扩张起点前20日均宽 ──
    # 对于恰好第N天扩张的行，扩张起点索引 = 当前位置 - (N-1)
    # 然后取起点前20日的breadth均值
    df['exp_start_pre_breadth'] = np.nan
    for con_code, group in df.groupby('con_code', sort=False):
        idx = group.index
        exp_days = group['expansion_days'].values
        breadth_vals = group['breadth'].values
        result = np.full(len(exp_days), np.nan)
        for i in range(len(exp_days)):
            n = exp_days[i]
            if n >= 2:
                first_i = i - (n - 1)  # 扩张第1天的索引（在group内）
                if first_i >= 20:
                    pre_slice = breadth_vals[first_i - 20 : first_i]
                    if len(pre_slice) >= 15:
                        result[i] = np.mean(pre_slice)
        df.loc[idx, 'exp_start_pre_breadth'] = result

    # ── 前20日均宽（滚动窗口，用于C组冷区判断） ──
    df['pre_breadth_20d'] = df.groupby('con_code')['breadth'].transform(
        lambda x: x.rolling(20, min_periods=15).mean().shift(1)
    )

    # ── 市场日收益 ──
    market_daily = df.groupby('date')['avg_pct'].mean().reset_index()
    market_daily.columns = ['date', 'market_avg_pct']
    df = df.merge(market_daily, on='date', how='left')

    # ── 前向收益（5/10/20/30日） ──
    for n in [5, 10, 20, 30]:
        df[f'fwd_{n}d_ret'] = df.groupby('con_code')['avg_pct'].transform(
            lambda x: x.rolling(n, min_periods=n).sum().shift(-n)
        )
        df[f'fwd_{n}d_mkt'] = df.groupby('con_code')['market_avg_pct'].transform(
            lambda x: x.rolling(n, min_periods=n).sum().shift(-n)
        )
        df[f'fwd_{n}d_excess'] = df[f'fwd_{n}d_ret'] - df[f'fwd_{n}d_mkt']

    # ── 信号分级 ──（15%阈值下：high<8%, standard<12%, watch<15%）
    df['tier'] = 'watch'
    df.loc[df['exp_start_pre_breadth'] < 0.12, 'tier'] = 'standard'
    df.loc[df['exp_start_pre_breadth'] < 0.08, 'tier'] = 'high'

    # ── 日期列确保 ──
    df['date'] = pd.to_datetime(df['date'])

    print(f"  前向收益计算完成")
    return df


# ═══════════════════════════════════════════════════════════════
# 二、信号去重（同一行业同一次扩张只触发一次）
# ═══════════════════════════════════════════════════════════════

def deduplicate_signals(signals_df, expansion_n=3):
    """
    同一行业同一次扩张事件只保留恰好第N天的触发。
    expansion_n=3 表示恰好3天扩张时触发。
    """
    if signals_df.empty:
        return signals_df
    # 对于恰好第N天扩张的行，同一扩张事件只出现在第N天
    # 因为 expansion_days == N 已经是唯一触发点
    # 但仍需去重：如果行业间隔很短又触发，可能重叠
    signals_df = signals_df.sort_values(['con_code', 'date']).reset_index(drop=True)

    # 去重：同一行业，如果两次信号间隔 <= 5天，只保留第一个
    keep = []
    last_date_per_ind = {}
    for idx, row in signals_df.iterrows():
        ind = row['con_code']
        dt = row['date']
        if ind not in last_date_per_ind or (dt - last_date_per_ind[ind]).days > 5:
            keep.append(idx)
            last_date_per_ind[ind] = dt
    signals_df = signals_df.loc[keep].reset_index(drop=True)
    return signals_df


# ═══════════════════════════════════════════════════════════════
# 三、生成各实验组信号
# ═══════════════════════════════════════════════════════════════

def generate_groups(df):
    """生成 A~F 六组实验信号"""

    # 热启动期之后
    valid = df[df['date'] >= SIGNAL_START].copy()

    # A组：完整信号（动态冷区 + 恰好3天扩张）
    A = valid[
        (valid['expansion_days'] == 3) &
        (valid['exp_start_pre_breadth'] <= valid['cold_threshold']) &
        (valid['exp_start_pre_breadth'].notna())
    ].copy()
    A = deduplicate_signals(A, 3)

    # B组：固定冷区20% + 恰好3天扩张（对比用）
    B = valid[
        (valid['expansion_days'] == 3) &
        (valid['exp_start_pre_breadth'] <= 0.20) &
        (valid['exp_start_pre_breadth'].notna())
    ].copy()
    B = deduplicate_signals(B, 3)

    # C组：冷区无扩张（动态冷区，不限扩张天数）
    # 每个行业每个交易日，如果 pre_breadth_20d <= cold_threshold 即入选
    C = valid[
        (valid['pre_breadth_20d'] <= valid['cold_threshold']) &
        (valid['pre_breadth_20d'].notna())
    ].copy()
    # 去重：同一行业每隔20天才算一次独立冷区事件
    C = C.sort_values(['con_code', 'date']).reset_index(drop=True)
    keep_c = []
    last_date_per_ind = {}
    for idx, row in C.iterrows():
        ind = row['con_code']
        dt = row['date']
        if ind not in last_date_per_ind or (dt - last_date_per_ind[ind]).days >= 20:
            keep_c.append(idx)
            last_date_per_ind[ind] = dt
    C = C.loc[keep_c].reset_index(drop=True)

    # D组：纯扩张（恰好3天扩张，不限冷区）
    D = valid[
        (valid['expansion_days'] == 3) &
        (valid['exp_start_pre_breadth'].notna())
    ].copy()
    D = deduplicate_signals(D, 3)

    # F组：扩张天数对比（2/3/4/5天）
    F = {}
    for n in [2, 3, 4, 5]:
        Fn = valid[
            (valid['expansion_days'] == n) &
            (valid['exp_start_pre_breadth'] <= valid['cold_threshold']) &
            (valid['exp_start_pre_breadth'].notna())
        ].copy()
        Fn = deduplicate_signals(Fn, n)
        F[n] = Fn

    print(f"\n各组信号数量:")
    print(f"  A (完整信号):    {len(A)}")
    print(f"  B (固定20%):     {len(B)}")
    print(f"  C (冷区无扩张):  {len(C)}")
    print(f"  D (纯扩张):      {len(D)}")
    for n in [2, 3, 4, 5]:
        print(f"  F-{n}天扩张:     {len(F[n])}")

    return A, B, C, D, F


# ═══════════════════════════════════════════════════════════════
# 四、核心指标计算
# ═══════════════════════════════════════════════════════════════

def calc_metrics(excess_series, label=""):
    """计算超额收益的核心指标"""
    ex = excess_series.dropna()
    if len(ex) == 0:
        return {
            'label': label, 'n': 0, 'mean': np.nan, 'std': np.nan,
            'win_rate': np.nan, 'ir': np.nan, 't': np.nan, 'p': np.nan,
            'median': np.nan
        }
    mean = ex.mean()
    std = ex.std()
    win_rate = (ex > 0).mean()
    ir = mean / std * np.sqrt(252) if std > 0 else 0
    t_stat, p_val = stats.ttest_1samp(ex, 0)
    return {
        'label': label, 'n': len(ex), 'mean': mean, 'std': std,
        'win_rate': win_rate, 'ir': ir, 't': t_stat, 'p': p_val,
        'median': ex.median()
    }


def print_metrics_table(metrics_list, title=""):
    """打印指标表格"""
    print(f"\n{'─'*80}")
    print(f"  {title}")
    print(f"{'─'*80}")
    print(f"  {'组别':14s}  {'N':>5s}  {'均值%':>7s}  {'中位%':>7s}  {'标准差':>7s}  {'胜率':>6s}  {'IR':>6s}  {'t':>7s}  {'p':>6s}")
    print(f"  {'─'*14}  {'─'*5}  {'─'*7}  {'─'*7}  {'─'*7}  {'─'*6}  {'─'*6}  {'─'*7}  {'─'*6}")
    for m in metrics_list:
        if m['n'] == 0:
            print(f"  {m['label']:14s}  {'—':>5s}  {'—':>7s}  {'—':>7s}  {'—':>7s}  {'—':>6s}  {'—':>6s}  {'—':>7s}  {'—':>6s}")
        else:
            print(f"  {m['label']:14s}  {m['n']:5d}  {m['mean']:+7.2f}  {m['median']:+7.2f}  {m['std']:7.2f}  "
                  f"{m['win_rate']:6.1%}  {m['ir']:+6.2f}  {m['t']:+7.2f}  {m['p']:6.3f}")


# ═══════════════════════════════════════════════════════════════
# 五、蒙特卡洛随机基线（E组）
# ═══════════════════════════════════════════════════════════════

def monte_carlo_baseline(df, n_signals, n_sim=1000, seed=42):
    """
    在每个交易日随机抽取行业，等量于A组信号数，
    重复 n_sim 次，得到零假设分布。
    """
    rng = np.random.RandomState(seed)
    valid = df[df['date'] >= SIGNAL_START].copy()
    dates = sorted(valid['date'].unique())
    industries = valid['con_code'].unique()

    # A组每日信号数量分布
    # 简化：从valid中随机抽取 n_signals 条
    means_5d, means_10d, means_20d = [], [], []
    for _ in range(n_sim):
        sample = valid.sample(n=n_signals, random_state=rng, replace=True)
        ex5 = sample['fwd_5d_excess'].dropna()
        ex10 = sample['fwd_10d_excess'].dropna()
        ex20 = sample['fwd_20d_excess'].dropna()
        if len(ex5) > 0: means_5d.append(ex5.mean())
        if len(ex10) > 0: means_10d.append(ex10.mean())
        if len(ex20) > 0: means_20d.append(ex20.mean())

    return {
        '5d': np.array(means_5d),
        '10d': np.array(means_10d),
        '20d': np.array(means_20d),
    }


# ═══════════════════════════════════════════════════════════════
# 六、信号衰减曲线
# ═══════════════════════════════════════════════════════════════

def calc_decay_curve(df, signals_df, max_days=30):
    """计算持仓1~max_days逐日累计超额"""
    if signals_df.empty:
        return pd.DataFrame()

    # 需要逐日前向收益，这里用日频数据直接计算
    valid = df[df['date'] >= SIGNAL_START].copy()

    results = []
    for n in range(1, max_days + 1):
        # 计算n日前向超额
        col_ret = f'fwd_{n}d_ret' if f'fwd_{n}d_ret' in df.columns else None
        col_mkt = f'fwd_{n}d_mkt' if f'fwd_{n}d_mkt' in df.columns else None

        if col_ret is None:
            # 需要临时计算
            valid_temp = valid.copy()
            valid_temp[f'temp_ret_{n}'] = valid_temp.groupby('con_code')['avg_pct'].transform(
                lambda x: x.rolling(n, min_periods=n).sum().shift(-n)
            )
            valid_temp[f'temp_mkt_{n}'] = valid_temp.groupby('con_code')['market_avg_pct'].transform(
                lambda x: x.rolling(n, min_periods=n).sum().shift(-n)
            )
            valid_temp[f'temp_ex_{n}'] = valid_temp[f'temp_ret_{n}'] - valid_temp[f'temp_mkt_{n}']
            merged = signals_df[['date', 'con_code']].merge(
                valid_temp[['date', 'con_code', f'temp_ex_{n}']],
                on=['date', 'con_code'], how='left'
            )
            ex = merged[f'temp_ex_{n}'].dropna()
        else:
            merged = signals_df[['date', 'con_code']].merge(
                df[['date', 'con_code', col_ret, col_mkt]],
                on=['date', 'con_code'], how='left'
            )
            ex = (merged[col_ret] - merged[col_mkt]).dropna()

        if len(ex) > 0:
            results.append({'hold_days': n, 'excess_mean': ex.mean(), 'excess_median': ex.median(),
                           'win_rate': (ex > 0).mean(), 'n': len(ex)})
        else:
            results.append({'hold_days': n, 'excess_mean': np.nan, 'excess_median': np.nan,
                           'win_rate': np.nan, 'n': 0})

    return pd.DataFrame(results)


# ═══════════════════════════════════════════════════════════════
# 七、主函数
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("宽度反转策略 v5 — 统一15%冷区阈值回测")
    print("=" * 70)

    # ── 1. 加载数据 ──
    print("\n[1/8] 加载数据...")
    df = load_and_prepare()

    # ── 2. 生成各组信号 ──
    print("\n[2/8] 生成各组信号...")
    A, B, C, D, F = generate_groups(df)

    # ═══════════════════════════════════════════════════════════
    # 三、总览表：各组核心指标
    # ═══════════════════════════════════════════════════════════
    print("\n[3/8] 计算各组核心指标...")

    all_results = []
    for horizon in [5, 10, 20]:
        col = f'fwd_{horizon}d_excess'
        metrics = []
        metrics.append(calc_metrics(A[col], f'A 完整信号'))
        metrics.append(calc_metrics(B[col], f'B 固定20%'))
        metrics.append(calc_metrics(C[col], f'C 冷区无扩张'))
        metrics.append(calc_metrics(D[col], f'D 纯扩张'))
        for n in [2, 3, 4, 5]:
            metrics.append(calc_metrics(F[n][col], f'F-{n}天扩张'))

        print_metrics_table(metrics, f"前向 {horizon} 日超额收益")
        all_results.extend(metrics)

    # ═══════════════════════════════════════════════════════════
    # 四、蒙特卡洛随机基线（E组）
    # ═══════════════════════════════════════════════════════════
    print("\n[4/8] 蒙特卡洛随机基线（1000次）...")
    n_a = len(A)
    if n_a > 0:
        mc = monte_carlo_baseline(df, n_a, n_sim=1000)
        for horizon in [5, 10, 20]:
            key = f'{horizon}d'
            mc_dist = mc[key]
            a_ex = A[f'fwd_{horizon}d_excess'].dropna()
            if len(a_ex) > 0:
                a_mean = a_ex.mean()
                percentile = (mc_dist < a_mean).mean()
                p_mc = 1 - percentile if a_mean > mc_dist.mean() else percentile

                print(f"\n  {horizon}日超额 — A组 vs 随机基线:")
                print(f"    A组均值: {a_mean:+.2f}%")
                print(f"    随机基线: 均值={mc_dist.mean():+.2f}%, 95%CI=[{np.percentile(mc_dist,2.5):+.2f}%, {np.percentile(mc_dist,97.5):+.2f}%]")
                print(f"    A组在随机分布中的百分位: {percentile:.1%}")
                if percentile > 0.95:
                    print(f"    ✅ A组显著优于随机 (p<0.05)")
                elif percentile > 0.90:
                    print(f"    ➡️ A组边缘优于随机 (p<0.10)")
                else:
                    print(f"    ❌ A组不优于随机")

    # ═══════════════════════════════════════════════════════════
    # 五、分组对比（核心问题归因）
    # ═══════════════════════════════════════════════════════════
    print("\n[5/8] 分组对比分析...")

    for horizon in [5, 10, 20]:
        col = f'fwd_{horizon}d_excess'
        a_ex = A[col].dropna()
        b_ex = B[col].dropna()
        c_ex = C[col].dropna()
        d_ex = D[col].dropna()

        print(f"\n  {'='*50}")
        print(f"  {horizon}日超额 — 分组对比")
        print(f"  {'='*50}")

        # A vs C：扩张的增量价值
        if len(a_ex) > 0 and len(c_ex) > 0:
            delta_ac = a_ex.mean() - c_ex.mean()
            t_ac, p_ac = stats.ttest_ind(a_ex, c_ex, equal_var=False)
            print(f"  A vs C (扩张增量): Δ={delta_ac:+.2f}%, t={t_ac:+.2f}, p={p_ac:.3f}")
            if p_ac < 0.05 and delta_ac > 0:
                print(f"    ✅ 扩张有显著正增量")
            elif delta_ac > 0:
                print(f"    ➡️ 扩张有轻微正增量但不显著")
            else:
                print(f"    ❌ 扩张无增量或负增量")

        # A vs B：15% vs 20%阈值
        if len(a_ex) > 0 and len(b_ex) > 0:
            delta_ab = a_ex.mean() - b_ex.mean()
            t_ab, p_ab = stats.ttest_ind(a_ex, b_ex, equal_var=False)
            print(f"  A vs B (15%阈值): Δ={delta_ab:+.2f}%, t={t_ab:+.2f}, p={p_ab:.3f}")

        # A vs D：冷区的增量价值
        if len(a_ex) > 0 and len(d_ex) > 0:
            delta_ad = a_ex.mean() - d_ex.mean()
            t_ad, p_ad = stats.ttest_ind(a_ex, d_ex, equal_var=False)
            print(f"  A vs D (冷区增量): Δ={delta_ad:+.2f}%, t={t_ad:+.2f}, p={p_ad:.3f}")
            if p_ad < 0.05 and delta_ad > 0:
                print(f"    ✅ 冷区筛选有显著正增量")
            elif delta_ad > 0:
                print(f"    ➡️ 冷区筛选有轻微正增量但不显著")
            else:
                print(f"    ❌ 冷区筛选无增量或负增量")

    # ═══════════════════════════════════════════════════════════
    # 六、信号分级有效性
    # ═══════════════════════════════════════════════════════════
    print("\n[6/8] 信号分级有效性...")

    for horizon in [5, 10, 20]:
        col = f'fwd_{horizon}d_excess'
        print(f"\n  {horizon}日超额 — 分级对比:")
        tier_metrics = []
        for tier in ['high', 'standard', 'watch']:
            sub = A[A['tier'] == tier]
            tier_metrics.append(calc_metrics(sub[col], f'{tier}'))

        print_metrics_table(tier_metrics, f"分级对比 ({horizon}日)")

        # 检验 high vs watch
        high_ex = A[A['tier'] == 'high'][col].dropna()
        watch_ex = A[A['tier'] == 'watch'][col].dropna()
        if len(high_ex) > 0 and len(watch_ex) > 0:
            t_hw, p_hw = stats.ttest_ind(high_ex, watch_ex, equal_var=False)
            delta_hw = high_ex.mean() - watch_ex.mean()
            print(f"  high vs watch: Δ={delta_hw:+.2f}%, t={t_hw:+.2f}, p={p_hw:.3f}")

    # ═══════════════════════════════════════════════════════════
    # 七、跨期稳定性
    # ═══════════════════════════════════════════════════════════
    print("\n[7/8] 跨期稳定性...")

    periods = [
        ('P1 牛熊', '2020-07-01', '2021-12-31'),
        ('P2 震荡', '2022-01-01', '2024-09-30'),
        ('P3 近期', '2024-10-01', '2026-12-31'),
    ]

    print(f"\n  {'时期':12s}  {'信号数':>6s}  {'5日超额':>8s}  {'10日超额':>8s}  {'20日超额':>8s}  {'20日胜率':>8s}")
    print(f"  {'─'*12}  {'─'*6}  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*8}")
    for label, start, end in periods:
        sub = A[(A['date'] >= start) & (A['date'] <= end)]
        ex5 = sub['fwd_5d_excess'].dropna()
        ex10 = sub['fwd_10d_excess'].dropna()
        ex20 = sub['fwd_20d_excess'].dropna()
        n = len(sub)
        if n > 0:
            print(f"  {label:12s}  {n:6d}  {ex5.mean():+8.2f}%  {ex10.mean():+8.2f}%  "
                  f"{ex20.mean():+8.2f}%  {(ex20>0).mean():8.1%}")
        else:
            print(f"  {label:12s}  {n:6d}  {'—':>8s}  {'—':>8s}  {'—':>8s}  {'—':>8s}")

    # 逐年统计
    print(f"\n  逐年统计:")
    print(f"  {'年份':6s}  {'信号数':>6s}  {'5日超额':>8s}  {'10日超额':>8s}  {'20日超额':>8s}  {'20日胜率':>8s}")
    print(f"  {'─'*6}  {'─'*6}  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*8}")
    for year in sorted(A['date'].dt.year.unique()):
        sub = A[A['date'].dt.year == year]
        ex5 = sub['fwd_5d_excess'].dropna()
        ex10 = sub['fwd_10d_excess'].dropna()
        ex20 = sub['fwd_20d_excess'].dropna()
        n = len(sub)
        if n > 0:
            print(f"  {year:6d}  {n:6d}  {ex5.mean():+8.2f}%  {ex10.mean():+8.2f}%  "
                  f"{ex20.mean():+8.2f}%  {(ex20>0).mean():8.1%}")

    # 行业集中度
    print(f"\n  行业集中度:")
    ind_counts = A.groupby('con_code').size().sort_values(ascending=False)
    print(f"    总信号行业数: {len(ind_counts)}")
    print(f"    前10行业信号占比: {ind_counts.head(10).sum() / len(A):.1%}" if len(A) > 0 else "")
    print(f"    信号最多行业: {ind_counts.index[0]} ({ind_counts.iloc[0]}次)" if len(ind_counts) > 0 else "")

    # 月频信号数分布
    if not A.empty:
        A['month'] = A['date'].dt.to_period('M')
        monthly = A.groupby('month').size()
        print(f"\n  月频信号分布:")
        print(f"    月均: {monthly.mean():.1f}, 中位: {monthly.median():.0f}, "
              f"min={monthly.min()}, max={monthly.max()}")
        print(f"    无信号月份: {(monthly==0).sum()}/{len(monthly)}")

    # ═══════════════════════════════════════════════════════════
    # 八、信号衰减曲线
    # ═══════════════════════════════════════════════════════════
    print("\n[8/8] 信号衰减曲线（A组，持仓1~30日）...")
    # 只计算5/10/15/20/25/30天（减少计算量，已有5/10/20/30的前向收益）
    print(f"\n  {'持仓天数':>8s}  {'超额均值':>8s}  {'超额中位':>8s}  {'胜率':>6s}  {'N':>5s}")
    print(f"  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*6}  {'─'*5}")
    for n in [5, 10, 20, 30]:
        col = f'fwd_{n}d_excess'
        ex = A[col].dropna()
        if len(ex) > 0:
            print(f"  {n:8d}  {ex.mean():+8.2f}%  {ex.median():+8.2f}%  {(ex>0).mean():6.1%}  {len(ex):5d}")

    # ═══════════════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("📋 回测总结")
    print("=" * 70)

    a20 = A['fwd_20d_excess'].dropna()
    c20 = C['fwd_20d_excess'].dropna()
    b20 = B['fwd_20d_excess'].dropna()
    d20 = D['fwd_20d_excess'].dropna()

    print(f"\n  1️⃣  信号整体效果:")
    if len(a20) > 0:
        t_a, p_a = stats.ttest_1samp(a20, 0)
        ir_a = a20.mean() / a20.std() * np.sqrt(252) if a20.std() > 0 else 0
        print(f"      A组20日超额 = {a20.mean():+.2f}%, 胜率 = {(a20>0).mean():.1%}, IR = {ir_a:+.2f}")
        print(f"      t={t_a:+.2f}, p={p_a:.3f}")
        if p_a < 0.05 and a20.mean() > 0:
            print(f"      ✅ 信号有显著正超额")
        elif a20.mean() > 0:
            print(f"      ➡️ 信号超额为正但不显著")
        else:
            print(f"      ❌ 信号无正超额")

    print(f"\n  2️⃣  因果归因（扩张 vs 冷区均值回归）:")
    if len(a20) > 0 and len(c20) > 0:
        delta = a20.mean() - c20.mean()
        print(f"      扩张增量(A-C) = {delta:+.2f}%")
        if abs(delta) < 0.3:
            print(f"      ⚠️ 增量极小 — 超额主要来自冷区均值回归，扩张条件信息增量有限")
        elif delta > 0.5:
            print(f"      ✅ 扩张有实质增量")
        else:
            print(f"      ➡️ 扩张有轻微增量")

    print(f"\n  3️⃣  15% vs 20%阈值:")
    if len(a20) > 0 and len(b20) > 0:
        diff = a20.mean() - b20.mean()
        if diff > 0.2:
            print(f"      ✅ 15%优于20% (差值={diff:+.2f}%)")
        elif diff < -0.2:
            print(f"      ❌ 15%不如20% (差值={diff:+.2f}%)")
        else:
            print(f"      ≈ 两者持平 (差值={diff:+.2f}%)")

    print(f"\n  4️⃣  冷区筛选增量:")
    if len(a20) > 0 and len(d20) > 0:
        delta_ad = a20.mean() - d20.mean()
        print(f"      A vs D (冷区增量) = {delta_ad:+.2f}%")
        if delta_ad > 0.3:
            print(f"      ✅ 冷区筛选有价值")
        elif delta_ad < -0.3:
            print(f"      ❌ 冷区筛选反而拖累")
        else:
            print(f"      ≈ 冷区筛选增量不明显")

    print(f"\n  5️⃣  分级有效性:")
    if len(A) > 0:
        tier_means = A.groupby('tier')['fwd_20d_excess'].mean()
        parts = []
        for tier in ['high', 'standard', 'watch']:
            if tier in tier_means.index:
                parts.append(f"{tier}={tier_means[tier]:+.2f}%")
        print(f"      {' > '.join(parts)}")
        if 'high' in tier_means.index and 'watch' in tier_means.index:
            gap = tier_means['high'] - tier_means['watch']
            if gap > 0.5:
                print(f"      ✅ 分级有区分度 (gap={gap:+.2f}%)")
            elif gap > 0:
                print(f"      ➡️ 分级区分度有限 (gap={gap:+.2f}%)")
            else:
                print(f"      ❌ 分级无区分度 (gap={gap:+.2f}%)")

    print(f"\n  6️⃣  跨期稳定性:")
    period_excess = []
    for label, start, end in periods:
        sub = A[(A['date'] >= start) & (A['date'] <= end)]
        ex = sub['fwd_20d_excess'].dropna()
        if len(ex) > 0:
            period_excess.append((label, ex.mean(), len(ex)))
    positive_count = sum(1 for _, x, _ in period_excess if x > 0)
    for label, ex_val, n_val in period_excess:
        print(f"      {label}: {ex_val:+.2f}% (n={n_val})")
    if len(period_excess) > 0:
        if positive_count == len(period_excess):
            print(f"      ✅ 全期稳定（{positive_count}/{len(period_excess)}期正超额）")
        elif positive_count >= len(period_excess) - 1:
            print(f"      ➡️ 基本稳定（{positive_count}/{len(period_excess)}期正超额）")
        else:
            print(f"      ❌ 跨期不稳定（{positive_count}/{len(period_excess)}期正超额）")

    print(f"\n  🏁 最终建议:")
    if len(a20) > 0:
        if a20.mean() > 0 and (a20.mean() / a20.std() * np.sqrt(252)) > 0.5:
            print(f"      策略有微弱正超额，建议继续观察但暂不上线实盘")
        elif a20.mean() > 0:
            print(f"      策略超额为正但信噪比极低，建议重大改进后再评估")
        else:
            print(f"      策略超额为负，不建议上线，需重新思考逻辑")

    print()


if __name__ == '__main__':
    main()
