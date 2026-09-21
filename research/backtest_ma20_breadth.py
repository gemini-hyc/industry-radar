"""
模块二宽度反转信号独立复核回测（MA20占比宽度版）

核心修正：使用与方案完全一致的MA20占比宽度定义
- 数据源：industry_daily_full.parquet（breadth = close>MA20的比例）
- 不再自算(pctChg>0).mean()，直接用生产数据的breadth字段

回测范围：2020-01-02 ~ 2026-07-22
"""

import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 0. 加载数据
# ============================================================
DATA_DIR = '/opt/data/quant-data'

idf = pd.read_parquet(f'{DATA_DIR}/industry/industry_daily_full.parquet')
im = pd.read_parquet(f'{DATA_DIR}/industry/industry_members.parquet')
ind_names = im.drop_duplicates('ind_code')[['ind_code','ind_name']].rename(columns={'ind_code':'con_code'})

# 合并行业名称
idf = idf.merge(ind_names, on='con_code', how='left')

# 按行业排序
idf = idf.sort_values(['con_code', 'date']).reset_index(drop=True)

print(f"数据加载完成：{idf.shape[0]}条, {idf['con_code'].nunique()}个行业, {idf['date'].min()}~{idf['date'].max()}")
print(f"breadth(MA20占比)统计: mean={idf['breadth'].mean():.3f}, std={idf['breadth'].std():.3f}")

# ============================================================
# 1. 计算前20日均宽（关键变量）
# ============================================================
# 方案定义：扩张起点前20个交易日宽度的算术平均
idf['pre_breadth_20d'] = idf.groupby('con_code')['breadth'].transform(
    lambda x: x.rolling(20, min_periods=10).mean().shift(1)  # shift(1)确保不含当日
)

# ============================================================
# 2. 计算宽度连续扩张
# ============================================================
# 方案定义：当日宽度 > 前一日宽度，持续3天以上
idf['breadth_diff'] = idf.groupby('con_code')['breadth'].diff()
idf['is_expanding'] = idf['breadth_diff'] > 0

# 连续扩张天数：从当天往回数，连续is_expanding=True的天数
def count_consecutive_expansion(series):
    """计算每个位置连续扩张天数（包含当天）"""
    result = pd.Series(0, index=series.index)
    count = 0
    for i in range(len(series)):
        if series.iloc[i]:
            count += 1
            result.iloc[i] = count
        else:
            count = 0
            result.iloc[i] = 0
    return result

idf['expansion_days'] = idf.groupby('con_code')['is_expanding'].transform(
    count_consecutive_expansion
)

# ============================================================
# 3. D1占比（首日力度）
# ============================================================
# 方案定义：d1/(d1+d2+d3)，扩张前3天各日宽度增幅的占比
# d1 = 最近一天的增幅，d2 = 前一天增幅，d3 = 前两天增幅

# 对每个扩张事件，需要找扩张的第1天、第2天、第3天的宽度增幅
# expansion_days=1时 d1=breadth_diff(today)
# expansion_days=2时 d1=breadth_diff(today-1), d2=breadth_diff(today)
# expansion_days=3时 d1=breadth_diff(today-2), d2=breadth_diff(today-1), d3=breadth_diff(today)

idf['d1'] = idf.groupby('con_code')['breadth_diff'].shift(2)  # 扩张第1天增幅
idf['d2'] = idf.groupby('con_code')['breadth_diff'].shift(1)  # 扩张第2天增幅  
idf['d3'] = idf.groupby('con_code')['breadth_diff']            # 扩张第3天增幅（当天）

# 只在expansion_days>=3时计算D1占比
idf['d1_ratio'] = np.where(
    (idf['expansion_days'] >= 3) & ((idf['d1'] + idf['d2'] + idf['d3']) > 0),
    idf['d1'] / (idf['d1'] + idf['d2'] + idf['d3']),
    np.nan
)

# ============================================================
# 4. 放量确认（三日量比）
# ============================================================
# 方案定义：扩张前3天平均日成交额 / 前20日平均日成交额
idf['amount_3d'] = idf.groupby('con_code')['total_amount'].transform(
    lambda x: x.rolling(3, min_periods=2).mean().shift(0)  # 当天+前2天
)
idf['amount_20d'] = idf.groupby('con_code')['total_amount'].transform(
    lambda x: x.rolling(20, min_periods=10).mean().shift(1)  # 前20天不含当天
)
idf['vol_ratio'] = idf['amount_3d'] / idf['amount_20d']

# ============================================================
# 5. 冷区定义与信号触发
# ============================================================
# 方案§3.1: 前20日均宽 ≤ 30% + 宽度连续扩张 ≥ 3天
idf['is_cold_zone'] = idf['pre_breadth_20d'] <= 0.30
idf['signal_trigger'] = idf['is_cold_zone'] & (idf['expansion_days'] >= 3)

# ============================================================
# 6. 前向收益计算
# ============================================================
# 方案用"累计前向收益"（从T+1到T+n的逐日加总）
# 用行业等权涨跌幅avg_pct作为行业收益率
# 大盘基准用中证全指（000985.CSI），这里暂用全行业均值近似

# 计算每日全行业均值涨跌幅作为大盘基准
market_avg = idf.groupby('date')['avg_pct'].mean().reset_index()
market_avg.columns = ['date', 'market_avg_pct']
idf = idf.merge(market_avg, on='date', how='left')

# 20日累计前向超额 = sum(industry_pct - market_pct) from T+1 to T+20
for n_days in [5, 10, 20]:
    col_name = f'forward_{n_days}d_excess'
    idf[col_name] = idf.groupby('con_code')['avg_pct'].transform(
        lambda x: x.rolling(n_days, min_periods=n_days).sum().shift(-n_days)
    ) - idf.groupby('con_code')['market_avg_pct'].transform(
        lambda x: x.rolling(n_days, min_periods=n_days).sum().shift(-n_days)
    )

# 超额胜率：forward超额>0
idf['forward_20d_win'] = idf['forward_20d_excess'] > 0

print("\n前向收益字段已计算完成")

# ============================================================
# 7. 核心验证：冷区信号触发后的超额
# ============================================================
signals = idf[idf['signal_trigger']].copy()
print(f"\n信号触发总数（冷区+扩张≥3天）: N={len(signals)}")
print(f"时间范围: {signals['date'].min()} ~ {signals['date'].max()}")

# 7.1 按前20日均宽分层（方案§四的三个档位）
print("\n=== 冷区信号按前20日均宽分层 ===")
for label, lo, hi in [('极低档<10%', 0, 0.10), ('10-20%', 0.10, 0.20), ('20-30%', 0.20, 0.30)]:
    subset = signals[(signals['pre_breadth_20d'] >= lo) & (signals['pre_breadth_20d'] < hi)]
    if len(subset) == 0:
        print(f"  {label}: N=0")
        continue
    avg_excess = subset['forward_20d_excess'].mean()
    win_rate = subset['forward_20d_win'].mean()
    t_stat, p_val = stats.ttest_ind(subset['forward_20d_excess'].dropna(), 
                                     pd.Series([0]*len(subset['forward_20d_excess'].dropna())))
    print(f"  {label}: N={len(subset)}, 20日超额={avg_excess:.2%}, 胜率={win_rate:.1%}, t={t_stat:.2f}, p={p_val:.3f}")

# 7.2 小克方案§八的核心数据对照
print("\n=== 方案§八叠加效果对照 ===")
# 仅触发(无额外过滤) — 对应小克N=2432, 20日累计+2.02%, 胜率62%
all_signals = signals.copy()
avg_excess = all_signals['forward_20d_excess'].mean()
win_rate = all_signals['forward_20d_win'].mean()
print(f"  仅触发: N={len(all_signals)}, 20日超额={avg_excess:.2%}, 胜率={win_rate:.1%}")

# ①<20% + ③≥1.1 — 对应小克N=467, +2.91%, 66%
s1 = signals[(signals['pre_breadth_20d'] < 0.20) & (signals['vol_ratio'] >= 1.1)]
print(f"  ①<20%+③≥1.1: N={len(s1)}, 20日超额={s1['forward_20d_excess'].mean():.2%}, 胜率={s1['forward_20d_win'].mean():.1%}")

# ②≥50% + ③≥1.1 — 对应小克N=205, +3.38%, 68%
s2 = signals[(signals['d1_ratio'] >= 0.50) & (signals['vol_ratio'] >= 1.1)]
print(f"  ②≥50%+③≥1.1: N={len(s2)}, 20日超额={s2['forward_20d_excess'].mean():.2%}, 胜率={s2['forward_20d_win'].mean():.1%}")

# ②≥60% + ③≥1.1 — 对应小克N=111, +4.20%, 73%
s3 = signals[(signals['d1_ratio'] >= 0.60) & (signals['vol_ratio'] >= 1.1)]
print(f"  ②≥60%+③≥1.1: N={len(s3)}, 20日超额={s3['forward_20d_excess'].mean():.2%}, 胜率={s3['forward_20d_win'].mean():.1%}")

# ①<10% + ②≥60% + ③≥1.2 — 对应小克~50, +4.5%+, 74%+
s4 = signals[(signals['pre_breadth_20d'] < 0.10) & (signals['d1_ratio'] >= 0.60) & (signals['vol_ratio'] >= 1.2)]
print(f"  ①<10%+②≥60%+③≥1.2: N={len(s4)}, 20日超额={s4['forward_20d_excess'].mean():.2%}, 胜率={s4['forward_20d_win'].mean():.1%}")

# ============================================================
# 8. 评分体系验证
# ============================================================
print("\n=== 评分体系验证（方案§3.2） ===")

def compute_score(row):
    """按方案§3.2计算得分：基础+前宽+D1占比+放量+涨停情绪"""
    base = 1
    # ①前20日均宽
    pre_b = row['pre_breadth_20d']
    if pd.notna(pre_b):
        if pre_b < 0.10: pre_score = 3
        elif pre_b < 0.20: pre_score = 2
        elif pre_b < 0.30: pre_score = 1
        else: pre_score = 0
    else:
        pre_score = 0
    
    # ②D1占比
    d1r = row['d1_ratio']
    if pd.notna(d1r):
        if d1r >= 0.60: d1_score = 3
        elif d1r >= 0.50: d1_score = 2
        elif d1r >= 0.40: d1_score = 1
        else: d1_score = 0
    else:
        d1_score = 0
    
    # ③放量(三日量比)
    vr = row['vol_ratio']
    if pd.notna(vr):
        if vr >= 1.3: vol_score = 3
        elif vr >= 1.1: vol_score = 2
        elif vr >= 1.0: vol_score = 1
        else: vol_score = 0
    else:
        vol_score = 0
    
    # ④涨停情绪 — 无法从行业日频数据直接计算，暂设为0
    limit_score = 0
    
    total = base + pre_score + d1_score + vol_score + limit_score
    return total, pre_score, d1_score, vol_score, limit_score

# 计算每个信号的得分
scores = signals.apply(compute_score, axis=1)
signals['score'] = scores.apply(lambda x: x[0])
signals['pre_score'] = scores.apply(lambda x: x[1])
signals['d1_score'] = scores.apply(lambda x: x[2])
signals['vol_score'] = scores.apply(lambda x: x[3])
signals['limit_score'] = scores.apply(lambda x: x[4])

# 按得分档位统计
print("得分分布(不含涨停维度):")
for threshold in [3, 5, 7]:
    sub = signals[signals['score'] >= threshold]
    if len(sub) == 0:
        print(f"  ≥{threshold}分: N=0")
        continue
    avg_excess = sub['forward_20d_excess'].mean()
    win_rate = sub['forward_20d_win'].mean()
    n_signals_per_day = len(sub) / len(subset['date'].nunique()) if len(sub) > 0 else 0
    print(f"  ≥{threshold}分: N={len(sub)}, 20日超额={avg_excess:.2%}, 胜率={win_rate:.1%}")

# ============================================================
# 9. ABC四组对照（与小克§四的三区框架对照）
# ============================================================
print("\n=== ABC四组对照 ===")
# A组：纯扩张（宽度连续扩张≥3天，不限前宽）
# B组：纯冷区（前20日均宽≤30%，不限扩张）
# C组：冷区+扩张（信号触发 = is_cold_zone & expansion_days>=3）
# D组：热区+扩张（前20日均宽>70%，扩张≥3天）

idf['is_hot_zone'] = idf['pre_breadth_20d'] > 0.70

A = idf[idf['expansion_days'] >= 3].copy()
B = idf[idf['is_cold_zone']].copy()
C = idf[idf['signal_trigger']].copy()
D = idf[idf['is_hot_zone'] & (idf['expansion_days'] >= 3)].copy()

for label, group in [('A:纯扩张≥3天', A), ('B:纯冷区≤30%', B), ('C:冷区+扩张', C), ('D:热区+扩张', D)]:
    avg_excess = group['forward_20d_excess'].mean()
    win_rate = group['forward_20d_win'].mean()
    t_val, p_val = stats.ttest_1samp(group['forward_20d_excess'].dropna(), 0)
    print(f"  {label}: N={len(group)}, 20日超额={avg_excess:.2%}, 胜率={win_rate:.1%}, t={t_val:.2f}, p={p_val:.4f}")

# ============================================================
# 10. 扩张天数分档（方案§一的关键数据）
# ============================================================
print("\n=== 扩张天数分档 ===")
expansion_signals = idf[idf['expansion_days'] >= 1].copy()
for days in [3, 5, 7]:
    sub = expansion_signals[expansion_signals['expansion_days'] >= days]
    avg_excess = sub['forward_20d_excess'].mean()
    win_rate = sub['forward_20d_win'].mean()
    print(f"  扩张≥{days}天: N={len(sub)}, 20日超额={avg_excess:.2%}, 胜率={win_rate:.1%}")

# ============================================================
# 11. 三阶段稳定性验证
# ============================================================
print("\n=== 三阶段稳定性 ===")
# P1: 2020-2021结构牛, P2: 2022-2024震荡熊, P3: 2024-2026政策牛
p1_mask = (idf['date'] >= '2020-01-01') & (idf['date'] <= '2021-12-31')
p2_mask = (idf['date'] >= '2022-01-01') & (idf['date'] <= '2024-09-30')
p3_mask = (idf['date'] >= '2024-10-01') & (idf['date'] <= '2026-12-31')

for label, mask in [('P1结构牛2020-2021', p1_mask), ('P2震荡熊2022-2024', p2_mask), ('P3政策牛2024-2026', p3_mask)]:
    phase_signals = signals[mask]
    if len(phase_signals) == 0:
        print(f"  {label}: N=0")
        continue
    avg_excess = phase_signals['forward_20d_excess'].mean()
    win_rate = phase_signals['forward_20d_win'].mean()
    print(f"  {label}: N={len(phase_signals)}, 20日超额={avg_excess:.2%}, 胜率={win_rate:.1%}")

# ============================================================
# 12. 加速vs减速模式（方案§七）
# ============================================================
print("\n=== 加速vs减速模式 ===")
exp3 = idf[idf['expansion_days'] >= 3].copy()
# 减速: d1>d2>d3 (首日增幅最大)
exp3['is_decel'] = (exp3['d1'] > exp3['d2']) & (exp3['d2'] > exp3['d3']) & (exp3['d1'] > 0)
# 加速: d1<d2<d3 (增幅递增)
exp3['is_accel'] = (exp3['d1'] < exp3['d2']) & (exp3['d2'] < exp3['d3']) & (exp3['d3'] > 0)

for label, mask_col in [('减速(d1>d2>d3)', 'is_decel'), ('加速(d1<d2<d3)', 'is_accel')]:
    sub = exp3[exp3[mask_col]]
    avg_excess = sub['forward_20d_excess'].mean()
    win_rate = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 20日超额={avg_excess:.2%}, 胜率={win_rate:.1%}")

# 冷区内加速vs减速
cold_exp3 = exp3[exp3['is_cold_zone']]
for label, mask_col in [('冷区减速', 'is_decel'), ('冷区加速', 'is_accel')]:
    sub = cold_exp3[cold_exp3[mask_col]]
    avg_excess = sub['forward_20d_excess'].mean()
    win_rate = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 20日超额={avg_excess:.2%}, 胜率={win_rate:.1%}")

# ============================================================
# 13. 输出完整对照表
# ============================================================
print("\n" + "="*70)
print("与小克回测发现文档逐项对照")
print("="*70)

xiaoke_data = {
    '§四极低档<10%': {'N': 280, '超额': 9.06, '胜率': 84},
    '§四10-20%': {'N': 1499, '超额': 3.41, '胜率': 68},
    '§四20-30%': {'N': 2507, '超额': 2.46, '胜率': 65},
    '§八仅触发': {'N': 2432, '超额': 2.02, '胜率': 62},
    '§八①<20%+③≥1.1': {'N': 467, '超额': 2.91, '胜率': 66},
    '§八②≥50%+③≥1.1': {'N': 205, '超额': 3.38, '胜率': 68},
    '§八②≥60%+③≥1.1': {'N': 111, '超额': 4.20, '胜率': 73},
    '§3.5≥3分': {'日均': '0.4个', '超额': 1.65, '胜率': 62, 'IR': 1.15},
    '§3.5≥5分': {'日均': '0.4个', '超额': 3.26, '胜率': 67, 'IR': 1.54},
    '§3.5≥7分': {'日均': '<0.1个', '超额': 6.71, '胜率': 74, 'IR': 2.43},
    '§一扩张≥5天': {'超额': 1.82, '胜率': 67.8},
    '§一扩张≥3天': {'超额': 0.89, '胜率': 61.5},
}

print("\n(详细对照见上方各节输出)")

# ============================================================
# 14. IR计算
# ============================================================
print("\n=== IR计算（与小克§九对照） ===")
# Long-Only IR = mean(超额) / std(超额) × sqrt(252)
# 按得分档位

for threshold in [3, 5, 7]:
    sub = signals[signals['score'] >= threshold]
    excess = sub['forward_20d_excess'].dropna()
    if len(excess) < 10:
        print(f"  ≥{threshold}分: N={len(excess)}, IR无法计算")
        continue
    daily_ir = excess.mean() / excess.std() * np.sqrt(252)
    print(f"  ≥{threshold}分: N={len(excess)}, IR={daily_ir:.2f}")

print("\n=== 回测完成 ===")
