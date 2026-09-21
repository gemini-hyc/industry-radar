"""
唤醒信号系统性回测脚本
======================
逻辑：
1. 取95个唤醒事件，每个事件有 (ind_code, wake_date, status, phase, wake_confidence, S0, score)
2. 对每个事件，计算唤醒后 5/10/20 交易日的行业累计加权涨幅
3. 同期全市场基准 = 所有131行业当日加权涨幅均值（等权）
4. 超额 = 行业累计 - 基准累计
5. 按维度分层统计：
   - 真假醒: tracking(还在跑) vs eliminated(已被剔除)
   - 拥挤度分层: wake_confidence >= 0.7 vs < 0.7
   - S0分层: S0 >= 70(强信号) vs S0 < 70(弱信号)
   - phase分层: waking vs growing vs alarming
6. 统计指标: 超额均值、胜率(超额>0的比例)、t值、样本量
"""

import pandas as pd
import numpy as np
import json
from math import sqrt, erf

def ttest_1samp(x, popmean=0):
    """纯Python实现单样本t检验，替代scipy.stats.ttest_1samp"""
    x = np.array(x, dtype=float)
    n = len(x)
    if n < 2:
        return 0.0, 1.0
    mean = x.mean()
    var = x.var(ddof=1)
    if var == 0:
        return 0.0, 1.0
    t_val = (mean - popmean) / sqrt(var / n)
    # 近似p值（双侧）用正态近似，n>=5时足够好
    # 更精确的可用不完全贝塔函数，但这里正态近似即可
    p_val = 2 * (1 - 0.5 * (1 + erf(abs(t_val) / sqrt(2))))
    return t_val, p_val

# ============================================================
# 1. 加载数据
# ============================================================
report = pd.read_csv('/opt/data/quant-data/industry/tracker_report.csv')
returns = pd.read_parquet('/opt/data/quant-data/industry/industry_weighted_returns.parquet')

# 日期索引构建
dates = sorted(returns['date'].unique())
date_index = {d: i for i, d in enumerate(dates)}
n_dates = len(dates)

print(f"报告行业数: {report.shape[0]}")
print(f"加权涨跌幅: {returns.shape}, 日期数={n_dates}")
print(f"日期范围: {dates[0]} ~ {dates[-1]}")
print(f"唤醒日期范围: {report['wake_date'].min()} ~ {report['wake_date'].max()}")

# ============================================================
# 2. 构建全市场基准（每日131行业等权均值累计）
# ============================================================
daily_avg = returns.groupby('date')['weighted_pct'].mean().sort_index()
# 累计涨幅
cum_benchmark = {}
for horizon in [5, 10, 20]:
    cum_vals = []
    cum_dates = []
    for i, d in enumerate(dates):
        if i + horizon <= n_dates:
            # 基准累计 = 从d开始的horizon天全市场均值之和
            bench_cum = daily_avg.iloc[i:i+horizon].sum()
            cum_vals.append(bench_cum)
            cum_dates.append(d)
    cum_benchmark[horizon] = pd.Series(cum_vals, index=cum_dates)

print(f"\n基准统计 (5日累计均值): {cum_benchmark[5].mean():.3f}")
print(f"基准统计 (10日累计均值): {cum_benchmark[10].mean():.3f}")
print(f"基准统计 (20日累计均值): {cum_benchmark[20].mean():.3f}")

# ============================================================
# 3. 对每个唤醒事件计算后续涨幅
# ============================================================
results = []

for _, row in report.iterrows():
    ind_code = row['code']
    wake_date = pd.Timestamp(row['wake_date'])
    
    # 找到唤醒日在日期索引中的位置
    if wake_date not in date_index:
        # wake_date可能在parquet日期范围外或非交易日
        # 找最近的交易日
        available = [d for d in dates if d >= wake_date]
        if len(available) == 0:
            continue  # 无法回测
        wake_date = available[0]
    
    idx = date_index[wake_date]
    
    # 获取该行业的日度涨跌幅序列
    ind_data = returns[returns['ind_code'] == ind_code].set_index('date')['weighted_pct']
    ind_data = ind_data.sort_index()
    
    for horizon in [5, 10, 20]:
        # 计算从唤醒日开始的horizon天累计涨幅
        end_idx = idx + horizon
        if end_idx > n_dates:
            # 数据不足，跳过
            actual_days = n_dates - idx
            if actual_days < 3:
                continue  # 至少需要3天数据
            horizon_actual = actual_days
        else:
            horizon_actual = horizon
        
        # 行业累计涨幅
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
        
        # 超额 = 行业累计 - 基准累计
        excess = ind_cum - bench_cum
        
        results.append({
            'ind_code': ind_code,
            'name': row['name'],
            'wake_date': str(row['wake_date']),
            'status': row['status'],
            'phase': row['phase'],
            'wake_confidence': row['wake_confidence'],
            'S0': row['S0'],
            'score': row['score'],
            'elim_reason': row.get('elim_reason', ''),
            'horizon': horizon_actual,
            'horizon_target': horizon,
            'ind_cum': ind_cum,
            'bench_cum': bench_cum,
            'excess': excess,
            'valid_days': valid_days,
        })

df_results = pd.DataFrame(results)
print(f"\n回测事件数: {df_results.shape[0]}")
print(f"按目标horizon分布:")
for h in [5, 10, 20]:
    sub = df_results[df_results['horizon_target'] == h]
    print(f"  {h}日: {sub.shape[0]}个事件, 实际天数均值={sub['horizon'].mean():.1f}")

# ============================================================
# 4. 分层统计
# ============================================================
def layer_stats(df, label, horizon_list=[5, 10, 20]):
    """对子集统计超额均值、胜率、t值"""
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

# 4a. 全样本统计
all_stats.extend(layer_stats(df_results, '全样本'))

# 4b. 真假醒分层: tracking vs eliminated vs woken
for status in ['tracking', 'eliminated', 'woken']:
    sub = df_results[df_results['status'] == status]
    all_stats.extend(layer_stats(sub, f'status={status}'))

# 4c. 拥挤度分层: wake_confidence >= 0.7 vs < 0.7
all_stats.extend(layer_stats(df_results[df_results['wake_confidence'] >= 0.7], 'confidence≥0.7(高置信)'))
all_stats.extend(layer_stats(df_results[df_results['wake_confidence'] < 0.7], 'confidence<0.7(低置信)'))

# 4d. S0分层: >=70 vs <70
all_stats.extend(layer_stats(df_results[df_results['S0'] >= 70], 'S0≥70(强信号)'))
all_stats.extend(layer_stats(df_results[df_results['S0'] < 70], 'S0<70(弱信号)'))

# 4e. phase分层
for phase in ['waking', 'growing', 'alarming']:
    sub = df_results[df_results['phase'] == phase]
    if sub.shape[0] > 0:
        all_stats.extend(layer_stats(sub, f'phase={phase}'))

# 4f. 剔除原因分层
for reason in df_results['elim_reason'].unique():
    if reason == '' or pd.isna(reason):
        continue
    sub = df_results[(df_results['elim_reason'] == reason)]
    if sub.shape[0] > 3:
        all_stats.extend(layer_stats(sub, f'elim={reason}'))

stats_df = pd.DataFrame(all_stats)

# ============================================================
# 5. 输出结果
# ============================================================
print("\n" + "="*80)
print("唤醒信号回测结果")
print("="*80)

# 格式化输出
for label in stats_df['label'].unique():
    sub = stats_df[stats_df['label'] == label]
    print(f"\n【{label}】")
    for _, r in sub.iterrows():
        n_str = f"n={r['n']}"
        if r['excess_mean'] is None:
            print(f"  {r['horizon']}日: {n_str} — 样本不足")
            continue
        excess_str = f"超额={r['excess_mean']:.3f}%"
        win_str = f"胜率={r['win_rate']:.1%}"
        ind_str = f"行业累涨={r['ind_cum_mean']:.3f}%"
        bench_str = f"基准累涨={r['bench_cum_mean']:.3f}%"
        t_str = ""
        if r['t_value'] is not None:
            t_str = f"t={r['t_value']:.2f}(p={r['p_value']:.3f})"
        print(f"  {r['horizon']}日: {n_str}, {excess_str}, {win_str}, {ind_str}, {bench_str}, {t_str}")

# ============================================================
# 6. 汇总表格（方便阅读）
# ============================================================
print("\n" + "="*80)
print("汇总表：5日超额")
print("="*80)
h5 = stats_df[stats_df['horizon'] == 5].copy()
h5['excess_mean_fmt'] = h5['excess_mean'].apply(lambda x: f"{x:.2f}%" if x is not None else "—")
h5['win_rate_fmt'] = h5['win_rate'].apply(lambda x: f"{x:.0%}" if x is not None else "—")
h5['t_fmt'] = h5.apply(lambda r: f"t={r['t_value']:.2f}" if r['t_value'] is not None else "—", axis=1)
print(h5[['label', 'n', 'excess_mean_fmt', 'win_rate_fmt', 't_fmt']].to_string(index=False))

print("\n" + "="*80)
print("汇总表：10日超额")
print("="*80)
h10 = stats_df[stats_df['horizon'] == 10].copy()
h10['excess_mean_fmt'] = h10['excess_mean'].apply(lambda x: f"{x:.2f}%" if x is not None else "—")
h10['win_rate_fmt'] = h10['win_rate'].apply(lambda x: f"{x:.0%}" if x is not None else "—")
h10['t_fmt'] = h10.apply(lambda r: f"t={r['t_value']:.2f}" if r['t_value'] is not None else "—", axis=1)
print(h10[['label', 'n', 'excess_mean_fmt', 'win_rate_fmt', 't_fmt']].to_string(index=False))

print("\n" + "="*80)
print("汇总表：20日超额")
print("="*80)
h20 = stats_df[stats_df['horizon'] == 20].copy()
h20['excess_mean_fmt'] = h20['excess_mean'].apply(lambda x: f"{x:.2f}%" if x is not None else "—")
h20['win_rate_fmt'] = h20['win_rate'].apply(lambda x: f"{x:.0%}" if x is not None else "—")
h20['t_fmt'] = h20.apply(lambda r: f"t={r['t_value']:.2f}" if r['t_value'] is not None else "—", axis=1)
print(h20[['label', 'n', 'excess_mean_fmt', 'win_rate_fmt', 't_fmt']].to_string(index=False))

# ============================================================
# 7. 关键发现总结
# ============================================================
print("\n" + "="*80)
print("关键发现")
print("="*80)

# 全样本 vs tracking vs eliminated
for h in [5, 10, 20]:
    full = stats_df[(stats_df['label']=='全样本') & (stats_df['horizon']==h)]
    track = stats_df[(stats_df['label']=='status=tracking') & (stats_df['horizon']==h)]
    elim = stats_df[(stats_df['label']=='status=eliminated') & (stats_df['horizon']==h)]
    
    if full.shape[0] > 0 and track.shape[0] > 0 and elim.shape[0] > 0:
        f_ex = full.iloc[0]['excess_mean']
        t_ex = track.iloc[0]['excess_mean']
        e_ex = elim.iloc[0]['excess_mean']
        f_win = full.iloc[0]['win_rate']
        t_win = track.iloc[0]['win_rate']
        e_win = elim.iloc[0]['win_rate']
        print(f"\n{h}日: 全样本超额={f_ex:.3f}%胜率={f_win:.1%} | tracking超额={t_ex:.3f}%胜率={t_win:.1%} | eliminated超额={e_ex:.3f}%胜率={e_win:.1%}")
        if t_ex is not None and e_ex is not None:
            diff = t_ex - e_ex
            print(f"  真假醒差异(tracking-eliminated): {diff:.3f}%")

# 拥挤度对比
for h in [5, 10, 20]:
    high = stats_df[(stats_df['label']=='confidence≥0.7(高置信)') & (stats_df['horizon']==h)]
    low = stats_df[(stats_df['label']=='confidence<0.7(低置信)') & (stats_df['horizon']==h)]
    if high.shape[0] > 0 and low.shape[0] > 0:
        h_ex = high.iloc[0]['excess_mean']
        l_ex = low.iloc[0]['excess_mean']
        h_win = high.iloc[0]['win_rate']
        l_win = low.iloc[0]['win_rate']
        print(f"\n{h}日: 高置信超额={h_ex:.3f}%胜率={h_win:.1%}(n={high.iloc[0]['n']}) | 低置信超额={l_ex:.3f}%胜率={l_win:.1%}(n={low.iloc[0]['n']})")
        if h_ex is not None and l_ex is not None:
            diff = h_ex - l_ex
            print(f"  高低置信差异: {diff:.3f}%")

# S0对比
for h in [5, 10, 20]:
    strong = stats_df[(stats_df['label']=='S0≥70(强信号)') & (stats_df['horizon']==h)]
    weak = stats_df[(stats_df['label']=='S0<70(弱信号)') & (stats_df['horizon']==h)]
    if strong.shape[0] > 0 and weak.shape[0] > 0:
        s_ex = strong.iloc[0]['excess_mean']
        w_ex = weak.iloc[0]['excess_mean']
        s_win = strong.iloc[0]['win_rate']
        w_win = weak.iloc[0]['win_rate']
        print(f"\n{h}日: S0≥70超额={s_ex:.3f}%胜率={s_win:.1%}(n={strong.iloc[0]['n']}) | S0<70超额={w_ex:.3f}%胜率={w_win:.1%}(n={weak.iloc[0]['n']})")
        if s_ex is not None and w_ex is not None:
            diff = s_ex - w_ex
            print(f"  强弱信号差异: {diff:.3f}%")

# ============================================================
# 8. 保存结果
# ============================================================
stats_df.to_csv('/opt/data/quant-data/industry/wake_backtest_results.csv', index=False)
df_results.to_csv('/opt/data/quant-data/industry/wake_backtest_detail.csv', index=False)
print("\n结果已保存:")
print("  wake_backtest_results.csv — 分层统计汇总")
print("  wake_backtest_detail.csv — 每个事件明细")
