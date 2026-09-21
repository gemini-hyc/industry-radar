"""
唤醒信号系统性回测 v2
======================
v2升级：使用扩充后的加权涨跌幅(2025-01~2026-07)，19个月数据
新增：月度行情类型分析（结构性 vs 轮动）
"""

import pandas as pd
import numpy as np
from math import sqrt, erf

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

# ============================================================
# 1. 加载数据
# ============================================================
report = pd.read_csv('/opt/data/quant-data/industry/tracker_report.csv')
returns = pd.read_parquet('/opt/data/quant-data/industry/industry_weighted_returns.parquet')

dates = sorted(returns['date'].unique())
date_index = {d: i for i, d in enumerate(dates)}
n_dates = len(dates)

print(f"加权涨跌幅: {returns.shape}, 日期数={n_dates}")
print(f"日期范围: {dates[0]} ~ {dates[-1]}")

# ============================================================
# 2. 全市场基准
# ============================================================
daily_avg = returns.groupby('date')['weighted_pct'].mean().sort_index()

# ============================================================
# 3. 唤醒事件回测（与v1相同逻辑）
# ============================================================
cum_benchmark = {}
for horizon in [5, 10, 20]:
    cum_vals, cum_dates = [], []
    for i, d in enumerate(dates):
        if i + horizon <= n_dates:
            bench_cum = daily_avg.iloc[i:i+horizon].sum()
            cum_vals.append(bench_cum)
            cum_dates.append(d)
    cum_benchmark[horizon] = pd.Series(cum_vals, index=cum_dates)

results = []
for _, row in report.iterrows():
    ind_code = row['code']
    wake_date = pd.Timestamp(row['wake_date'])
    
    if wake_date not in date_index:
        available = [d for d in dates if d >= wake_date]
        if len(available) == 0:
            continue
        wake_date = available[0]
    
    idx = date_index[wake_date]
    ind_data = returns[returns['ind_code'] == ind_code].set_index('date')['weighted_pct']
    ind_data = ind_data.sort_index()
    
    for horizon in [5, 10, 20]:
        end_idx = idx + horizon
        if end_idx > n_dates:
            actual_days = n_dates - idx
            if actual_days < 3:
                continue
            horizon_actual = actual_days
        else:
            horizon_actual = horizon
        
        trade_dates_horizon = dates[idx:idx+horizon_actual]
        ind_cum = 0
        bench_cum = 0
        valid_days = 0
        
        for d in trade_dates_horizon:
            if d in ind_data.index:
                ind_cum += ind_data.loc[d]
                bench_cum += daily_avg.loc[d]
                valid_days += 1
        
        if valid_days < 3:
            continue
        
        excess = ind_cum - bench_cum
        results.append({
            'ind_code': ind_code, 'name': row['name'],
            'wake_date': str(row['wake_date']),
            'status': row['status'], 'phase': row['phase'],
            'wake_confidence': row['wake_confidence'],
            'S0': row['S0'], 'score': row['score'],
            'elim_reason': row.get('elim_reason', ''),
            'horizon': horizon_actual, 'horizon_target': horizon,
            'ind_cum': ind_cum, 'bench_cum': bench_cum, 'excess': excess,
            'valid_days': valid_days,
        })

df_results = pd.DataFrame(results)

# ============================================================
# 4. 分层统计函数
# ============================================================
def layer_stats(df, label, horizon_list=[5, 10, 20]):
    rows = []
    for h in horizon_list:
        sub = df[df['horizon_target'] == h]
        if sub.shape[0] < 3:
            rows.append({'label': label, 'horizon': h, 'n': sub.shape[0],
                         'excess_mean': None, 'win_rate': None, 't_value': None, 'p_value': None})
            continue
        excess = sub['excess']
        n = sub.shape[0]
        mean = excess.mean()
        win_rate = (excess > 0).mean()
        if n >= 5:
            t_val, p_val = ttest_1samp(excess.values, 0)
        else:
            t_val, p_val = None, None
        rows.append({
            'label': label, 'horizon': h, 'n': n,
            'excess_mean': mean, 'win_rate': win_rate,
            't_value': t_val, 'p_value': p_val,
            'ind_cum_mean': sub['ind_cum'].mean(),
            'bench_cum_mean': sub['bench_cum'].mean(),
        })
    return rows

all_stats = []
all_stats.extend(layer_stats(df_results, '全样本'))
for status in ['tracking', 'eliminated', 'woken']:
    all_stats.extend(layer_stats(df_results[df_results['status'] == status], f'status={status}'))
all_stats.extend(layer_stats(df_results[df_results['wake_confidence'] >= 0.7], 'confidence≥0.7'))
all_stats.extend(layer_stats(df_results[df_results['wake_confidence'] < 0.7], 'confidence<0.7'))
all_stats.extend(layer_stats(df_results[df_results['S0'] >= 70], 'S0≥70'))
all_stats.extend(layer_stats(df_results[df_results['S0'] < 70], 'S0<70'))
stats_df = pd.DataFrame(all_stats)

# ============================================================
# 5. 输出唤醒回测结果（精简版）
# ============================================================
print("\n" + "="*80)
print("一、唤醒信号回测结果（v2, 19个月数据基准）")
print("="*80)

for label in ['全样本', 'status=tracking', 'status=eliminated', 'confidence≥0.7', 'confidence<0.7', 'S0≥70', 'S0<70']:
    sub = stats_df[stats_df['label'] == label]
    parts = []
    for _, r in sub.iterrows():
        if r['excess_mean'] is None:
            parts.append(f"{r['horizon']}日:n={r['n']}")
            continue
        sig = "✅" if r['p_value'] is not None and r['p_value'] < 0.05 else ""
        parts.append(f"{r['horizon']}日:超额={r['excess_mean']:.2f}%,胜率={r['win_rate']:.0%},t={r['t_value']:.1f}{sig}")
    print(f"【{label}】 {' | '.join(parts)}")

# ============================================================
# 6. 月度行情类型分析（新增！）
# ============================================================
print("\n" + "="*80)
print("二、月度行情类型分析（2025-01 ~ 2026-07）")
print("="*80)

# 按月统计行业涨跌分布
returns['month'] = returns['date'].dt.to_period('M')

monthly_stats = []
for month, group in returns.groupby('month'):
    # 每日每行业涨跌幅
    daily_ind = group.groupby('date')
    
    # 月度指标
    # 1. 上涨行业占比（日均）
    daily_up_pct = []
    daily_std = []
    daily_top5_conc = []
    daily_corr = []
    
    for d, dg in daily_ind:
        pcts = dg['weighted_pct'].values
        n_ind = len(pcts)
        
        # 上涨行业占比
        up_pct = (pcts > 0).mean() * 100
        daily_up_pct.append(up_pct)
        
        # 行业间标准差
        if n_ind > 5:
            daily_std.append(np.std(pcts))
        
        # Top5集中度: 涨幅最大的5个行业累计涨幅 / 全行业累计涨幅(正部分)
        sorted_pcts = np.sort(pcts)[::-1]
        top5_sum = sorted_pcts[:5].sum()
        total_pos = pcts[pcts > 0].sum()
        if total_pos > 0:
            daily_top5_conc.append(top5_sum / total_pos)
        
        # 行业间相关系数（用排名相关代替）
        # 简化：用偏度衡量分布是否集中
        if n_ind > 10:
            from numpy import mean as npmean
            skew = npmean((pcts - npmean(pcts))**3) / (npmean((pcts - npmean(pcts))**2)**1.5 + 1e-10)
            daily_corr.append(skew)
    
    avg_up_pct = np.mean(daily_up_pct)
    avg_std = np.mean(daily_std) if daily_std else 0
    avg_top5 = np.mean(daily_top5_conc) if daily_top5_conc else 0
    avg_skew = np.mean(daily_corr) if daily_corr else 0
    
    # 行情类型判断
    # 结构性: 上涨占比<45% 且 std>1.5 (少数行业涨，分化大)
    # 轮动: 上涨占比40-55% 且 std中等
    # 全面上涨: 上涨占比>60%
    # 全面下跌: 上涨占比<30%
    if avg_up_pct > 60:
        regime = '🟢全面上涨'
    elif avg_up_pct < 30:
        regime = '🔴全面下跌'
    elif avg_up_pct < 45 and avg_std > 1.5:
        regime = '🟠结构性'
    elif avg_up_pct < 45 and avg_std <= 1.5:
        regime = '🟡偏弱轮动'
    else:
        regime = '⚪轮动'
    
    monthly_stats.append({
        'month': str(month),
        'avg_up_pct': avg_up_pct,
        'avg_std': avg_std,
        'avg_top5_conc': avg_top5,
        'avg_skew': avg_skew,
        'regime': regime,
        'n_days': len(daily_up_pct),
    })

ms_df = pd.DataFrame(monthly_stats)

print(f"\n{'月份':>10s} {'上涨占比':>8s} {'行业std':>8s} {'Top5集中度':>10s} {'偏度':>7s} {'行情类型':>10s}")
print("-" * 60)
for _, r in ms_df.iterrows():
    print(f"{r['month']:>10s} {r['avg_up_pct']:>7.1f}% {r['avg_std']:>8.2f} {r['avg_top5_conc']:>10.2f} {r['avg_skew']:>7.2f} {r['regime']:>10s}")

# ============================================================
# 7. 行情周期与唤醒信号交互分析
# ============================================================
print("\n" + "="*80)
print("三、唤醒事件在不同行情周期下的表现")
print("="*80)

# 给每个唤醒事件标注行情类型
wake_regime_map = {}
for _, r in ms_df.iterrows():
    wake_regime_map[r['month']] = r['regime']

df_results['wake_month'] = df_results['wake_date'].apply(lambda x: x[:7])
df_results['regime'] = df_results['wake_month'].map(wake_regime_map)

# 按行情类型分组统计5日超额
for regime in df_results['regime'].unique():
    if pd.isna(regime):
        continue
    sub = df_results[(df_results['regime'] == regime) & (df_results['horizon_target'] == 5)]
    if sub.shape[0] < 3:
        continue
    excess = sub['excess']
    t_val, p_val = ttest_1samp(excess.values, 0) if len(excess) >= 5 else (None, None)
    t_str = f"{t_val:.1f}" if t_val is not None else "N/A"
    sig = "✅" if p_val is not None and p_val < 0.05 else ""
    print(f"【{regime}】5日超额: n={sub.shape[0]}, 均值={excess.mean():.2f}%, 胜率={(excess>0).mean():.0%}, t={t_str}{sig}")

# 10日
print()
for regime in df_results['regime'].unique():
    if pd.isna(regime):
        continue
    sub = df_results[(df_results['regime'] == regime) & (df_results['horizon_target'] == 10)]
    if sub.shape[0] < 3:
        continue
    excess = sub['excess']
    t_val, p_val = ttest_1samp(excess.values, 0) if len(excess) >= 5 else (None, None)
    t_str = f"{t_val:.1f}" if t_val is not None else "N/A"
    sig = "✅" if p_val is not None and p_val < 0.05 else ""
    print(f"【{regime}】10日超额: n={sub.shape[0]}, 均值={excess.mean():.2f}%, 胜率={(excess>0).mean():.0%}, t={t_str}{sig}")

# ============================================================
# 8. 924政策牛前后对比
# ============================================================
print("\n" + "="*80)
print("四、924政策牛前后市场特征对比")
print("="*80)

# 2024-09-24政策牛（但数据从2025-01开始，只能看后续）
# 关键时段: 2025-01~03(反弹延续?) vs 2025-04~06(回落?) vs 2026-01~03(新行情?)
for period, start, end in [
    ('2025Q1(反弹?)', '2025-01-01', '2025-03-31'),
    ('2025Q2(调整?)', '2025-04-01', '2025-06-30'),
    ('2025Q3(低迷?)', '2025-07-01', '2025-09-30'),
    ('2025Q4(924后?)', '2025-10-01', '2025-12-31'),
    ('2026Q1', '2026-01-01', '2026-03-31'),
    ('2026Q2', '2026-04-01', '2026-06-30'),
    ('2026-07', '2026-07-01', '2026-07-31'),
]:
    mask = (returns['date'] >= pd.Timestamp(start)) & (returns['date'] <= pd.Timestamp(end))
    sub = returns[mask]
    if sub.empty:
        continue
    daily_avg_p = sub.groupby('date')['weighted_pct'].mean()
    daily_up = sub.groupby('date').apply(lambda x: (x['weighted_pct'] > 0).mean() * 100)
    print(f"【{period}】 均值={daily_avg_p.mean():.3f}%/日, 上涨行业占比={daily_up.mean():.1f}%, 交易日={sub['date'].nunique()}")

# ============================================================
# 9. 保存
# ============================================================
stats_df.to_csv('/opt/data/quant-data/industry/wake_backtest_v2_results.csv', index=False)
df_results.to_csv('/opt/data/quant-data/industry/wake_backtest_v2_detail.csv', index=False)
ms_df.to_csv('/opt/data/quant-data/industry/monthly_regime_analysis.csv', index=False)
print("\n结果已保存:")
print("  wake_backtest_v2_results.csv — 分层统计汇总")
print("  wake_backtest_v2_detail.csv — 每个事件明细")
print("  monthly_regime_analysis.csv — 月度行情类型分析")
