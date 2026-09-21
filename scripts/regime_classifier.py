"""
行情类型量化判断系统 v1
======================
逐日判断行情类型：结构性 / 轮动 / 全面上涨 / 全面下跌

核心指标：
1. up_pct: 上涨行业占比 — 区分涨跌方向
2. rank_autocorr: 行业排名自相关 — 区分"谁涨"是否维持（结构性=高自相关，轮动=低自相关）
3. top5_conc: Top5集中度 — 区分"少数领涨"vs"普涨"
4. std_ret: 行业间标准差 — 区分分化程度

判断逻辑：
- 全面上涨: up_pct>70% 且 top5_conc<0.25
- 全面下跌: up_pct<30%
- 结构性: up_pct<50% 且 rank_autocorr>0.15（连续几天都是这些行业涨）
- 轮动: up_pct 40-60% 且 rank_autocorr<0.1（涨的行业不停换）
- 偏弱轮动: up_pct<40% 且 rank_autocorr<0.1（多数行业不涨，但领涨的行业也在换）
"""

import pandas as pd
import numpy as np
from pathlib import Path

DATA_PATH = Path('/opt/data/quant-data/industry/industry_weighted_returns.parquet')
OUT_PATH = Path('/opt/data/quant-data/industry/daily_regime.parquet')

# ============================================================
# 1. 加载数据
# ============================================================
returns = pd.read_parquet(DATA_PATH)
dates_sorted = sorted(returns['date'].unique())

# ============================================================
# 2. 计算每日特征
# ============================================================
def calc_top5_conc(x):
    pcts = x['weighted_pct'].values
    sorted_pcts = np.sort(pcts)[::-1]
    top5_sum = sorted_pcts[:5].sum()
    total_pos = pcts[pcts > 0].sum()
    return top5_sum / total_pos if total_pos > 0 else 0

daily_rows = []
for d, group in returns.groupby('date'):
    pcts = group['weighted_pct'].values
    n = len(pcts)
    
    up_pct = (pcts > 0).mean() * 100
    mean_ret = pcts.mean()
    std_ret = pcts.std()
    skew_ret = float(np.mean((pcts - pcts.mean())**3) / (np.mean((pcts - pcts.mean())**2)**1.5 + 1e-10))
    top5_conc = calc_top5_conc(group)
    
    daily_rows.append({
        'date': d,
        'up_pct': up_pct,
        'mean_ret': mean_ret,
        'std_ret': std_ret,
        'skew_ret': skew_ret,
        'top5_conc': top5_conc,
    })

daily_df = pd.DataFrame(daily_rows).sort_values('date')

# ============================================================
# 3. 计算排名自相关（5日窗口）
# ============================================================
# 用过去5天的行业排名均值自相关，而非单日
# 这更稳定，也更能捕捉"主线持续性"

def rolling_rank_corr(returns_df, window=5):
    """计算过去window天的行业排名一致性"""
    dates = sorted(returns_df['date'].unique())
    results = []
    
    for i in range(len(dates)):
        if i < window:
            # 窗口不足，用可用天数
            start = 0
        else:
            start = i - window + 1
        
        window_dates = dates[start:i+1]
        if len(window_dates) < 2:
            results.append({'date': dates[i], 'rank_consistency': 0})
            continue
        
        # 计算窗口内每天的排名
        rank_matrix = []
        for wd in window_dates:
            day_data = returns_df[returns_df['date'] == wd].set_index('ind_code')['weighted_pct']
            rank_matrix.append(day_data.rank())
        
        # 合成DataFrame
        rank_df = pd.DataFrame(rank_matrix).T
        
        # 计算排名一致性：各行(行业)在窗口内的排名方差
        # 低方差 = 这个行业的排名位置稳定（结构性）
        # 高方差 = 这个行业的排名不停变（轮动）
        rank_var = rank_df.var(axis=1)
        mean_rank_var = rank_var.mean()
        
        # 归一化：用131个行业×window天的理论最大方差做基准
        # Spearman相关系数的另一种理解：
        # consistency = 1 - mean_rank_var / max_possible_var
        n_ind = rank_df.shape[0]
        max_var = (n_ind**2 - 1) / 12 * (1 - 1/len(window_dates))  # 排名的理论方差
        
        consistency = 1 - mean_rank_var / max_var if max_var > 0 else 0
        
        results.append({
            'date': dates[i],
            'rank_consistency': consistency,
            'rank_var_mean': mean_rank_var,
        })
    
    return pd.DataFrame(results)

rank_cons = rolling_rank_corr(returns, window=5)
daily_df = daily_df.merge(rank_cons, on='date', how='left')

# ============================================================
# 4. 判断行情类型
# ============================================================
def classify_regime(row):
    up = row['up_pct']
    rc = row['rank_consistency'] if pd.notna(row['rank_consistency']) else 0
    tc = row['top5_conc']
    std = row['std_ret']
    
    # 全面上涨：多数行业涨，且集中度低（普涨而非少数领涨）
    if up > 70 and tc < 0.25:
        return '全面上涨'
    
    # 全面下跌：多数行业跌
    if up < 30:
        return '全面下跌'
    
    # 结构性：上涨占比不高(<50%)，但排名一致性高(谁涨维持不变)，且集中度中等偏高
    # 这意味着少数行业持续领涨，其余行业不涨——主线行情
    if up < 50 and rc > 0.3 and tc > 0.30:
        return '结构性'
    
    # 偏强结构性：上涨占比中等(50-60%)，排名一致性中等偏高
    if 50 <= up < 60 and rc > 0.4 and tc > 0.30:
        return '偏强结构性'
    
    # 轮动：上涨占比中等，排名一致性低（涨的行业不停换）
    if 40 <= up < 60 and rc < 0.2:
        return '轮动'
    
    # 偏弱轮动：上涨占比偏低，但排名也不维持（跌的行业多，涨的行业也在换）
    if 30 <= up < 40 and rc < 0.2:
        return '偏弱轮动'
    
    # 边缘结构性：上涨占比偏低，排名一致性中等（可能正在形成主线）
    if 30 <= up < 50 and 0.2 <= rc < 0.3:
        return '边缘结构性'
    
    # 混合：无法明确归类
    return '混合'

daily_df['regime'] = daily_df.apply(classify_regime, axis=1)

# ============================================================
# 5. 统计分析
# ============================================================
print("="*80)
print("逐日行情类型判断结果")
print("="*80)

print("\n行情类型分布:")
regime_dist = daily_df['regime'].value_counts()
for r, n in regime_dist.items():
    pct = n / len(daily_df) * 100
    print(f"  {r}: {n}天 ({pct:.1f}%)")

print("\n各行情类型的特征均值:")
for regime in ['全面上涨', '结构性', '偏强结构性', '边缘结构性', '轮动', '偏弱轮动', '混合', '全面下跌']:
    sub = daily_df[daily_df['regime'] == regime]
    if len(sub) == 0:
        continue
    print(f"  {regime}: n={len(sub)}, up_pct={sub['up_pct'].mean():.1f}%, "
          f"rank_cons={sub['rank_consistency'].mean():.3f}, "
          f"top5_conc={sub['top5_conc'].mean():.3f}, "
          f"std_ret={sub['std_ret'].mean():.2f}, "
          f"mean_ret={sub['mean_ret'].mean():.3f}%")

# ============================================================
# 6. 月度汇总
# ============================================================
print("\n" + "="*80)
print("月度行情类型汇总")
print("="*80)

daily_df['month'] = daily_df['date'].dt.to_period('M')

monthly_summary = []
for month, group in daily_df.groupby('month'):
    regime_counts = group['regime'].value_counts()
    dominant = regime_counts.index[0] if len(regime_counts) > 0 else 'N/A'
    dominant_pct = regime_counts.iloc[0] / len(group) * 100 if len(regime_counts) > 0 else 0
    
    # 结构性占比
    structural_pct = regime_counts.get('结构性', 0) + regime_counts.get('偏强结构性', 0) + regime_counts.get('边缘结构性', 0)
    structural_pct = structural_pct / len(group) * 100
    
    monthly_summary.append({
        'month': str(month),
        'dominant_regime': dominant,
        'dominant_pct': dominant_pct,
        'structural_pct': structural_pct,
        'avg_up_pct': group['up_pct'].mean(),
        'avg_rank_cons': group['rank_consistency'].mean(),
        'avg_top5_conc': group['top5_conc'].mean(),
        'n_days': len(group),
    })

ms_df = pd.DataFrame(monthly_summary)

print(f"\n{'月份':>10s} {'主导类型':>12s} {'占比':>6s} {'结构性%':>8s} {'up_pct':>8s} {'rank_cons':>10s} {'top5_conc':>10s}")
print("-" * 70)
for _, r in ms_df.iterrows():
    print(f"{r['month']:>10s} {r['dominant_regime']:>12s} {r['dominant_pct']:>5.0f}% {r['structural_pct']:>7.1f}% "
          f"{r['avg_up_pct']:>7.1f}% {r['avg_rank_cons']:>9.3f} {r['avg_top5_conc']:>9.3f}")

# ============================================================
# 7. 行情类型与唤醒信号交互（验证策略适配）
# ============================================================
print("\n" + "="*80)
print("策略适配验证：不同行情类型下哪些因子有效")
print("="*80)

# 加载唤醒回测明细
detail = pd.read_csv('/opt/data/quant-data/industry/wake_backtest_v2_detail.csv')
detail['date'] = pd.to_datetime(detail['wake_date'])

# 给每个唤醒事件标注行情类型
daily_regime_map = dict(zip(daily_df['date'].dt.strftime('%Y-%m-%d'), daily_df['regime']))
detail['regime'] = detail['date'].dt.strftime('%Y-%m-%d').map(daily_regime_map)

print("\n唤醒事件在不同行情下的分布:")
regime_events = detail.groupby('regime')['name'].nunique()
for r, n in regime_events.items():
    print(f"  {r}: {n}个行业")

print("\n唤醒5日超额 by regime:")
for regime in detail['regime'].unique():
    if pd.isna(regime):
        continue
    sub = detail[(detail['regime'] == regime) & (detail['horizon_target'] == 5)]
    if len(sub) < 3:
        print(f"  {regime}: n={len(sub)}, 样本不足")
        continue
    print(f"  {regime}: n={len(sub)}, 超额={sub['excess'].mean():.2f}%, 胜率={(sub['excess']>0).mean():.0%}")

# ============================================================
# 8. 保存
# ============================================================
daily_df.to_parquet(OUT_PATH, index=False)
print(f"\n已保存: {OUT_PATH}")
print(f"  {daily_df.shape[0]}天 x {daily_df.shape[1]}列")

# 也保存月度汇总
ms_df.to_csv(OUT_PATH.parent / 'monthly_regime_summary.csv', index=False)
print(f"  月度汇总: {OUT_PATH.parent / 'monthly_regime_summary.csv'}")
