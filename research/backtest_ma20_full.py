"""
模块二宽度反转信号独立复核回测（MA20占比宽度版）
完整版 - 一次跑完所有分析
"""
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = '/opt/data/quant-data'

# ============================================================
# 0. 加载与预处理
# ============================================================
idf = pd.read_parquet(f'{DATA_DIR}/industry/industry_daily_full.parquet')
im = pd.read_parquet(f'{DATA_DIR}/industry/industry_members.parquet')
ind_names = im.drop_duplicates('ind_code')[['ind_code','ind_name']].rename(columns={'ind_code':'con_code'})
idf = idf.merge(ind_names, on='con_code', how='left')
idf = idf.sort_values(['con_code', 'date']).reset_index(drop=True)

print(f"数据: {idf.shape[0]}条, {idf['con_code'].nunique()}行业, {idf['date'].min()}~{idf['date'].max()}")
print(f"breadth(MA20占比): mean={idf['breadth'].mean():.3f}, std={idf['breadth'].std():.3f}")

# ============================================================
# 1. 核心变量计算
# ============================================================
# 前20日均宽（不含当日）
idf['pre_breadth_20d'] = idf.groupby('con_code')['breadth'].transform(
    lambda x: x.rolling(20, min_periods=10).mean().shift(1)
)

# 宽度日增幅与连续扩张天数
idf['breadth_diff'] = idf.groupby('con_code')['breadth'].diff()
idf['is_expanding'] = idf['breadth_diff'] > 0

def _count_consec(series):
    r = pd.Series(0, index=series.index, dtype=int)
    c = 0
    for i in range(len(series)):
        if series.iloc[i]:
            c += 1
            r.iloc[i] = c
        else:
            c = 0
    return r

idf['expansion_days'] = idf.groupby('con_code')['is_expanding'].transform(_count_consec)

# D1占比：扩张前3天中首日增幅占比 d1/(d1+d2+d3)
idf['d1'] = idf.groupby('con_code')['breadth_diff'].shift(2)
idf['d2'] = idf.groupby('con_code')['breadth_diff'].shift(1)
idf['d3'] = idf['breadth_diff']
idf['d1_ratio'] = np.where(
    (idf['expansion_days'] >= 3) & ((idf['d1'] + idf['d2'] + idf['d3']) > 0),
    idf['d1'] / (idf['d1'] + idf['d2'] + idf['d3']),
    np.nan
)

# 放量(三日量比)：扩张3天平均成交额 / 前20日平均成交额
idf['amount_3d'] = idf.groupby('con_code')['total_amount'].transform(
    lambda x: x.rolling(3, min_periods=2).mean()
)
idf['amount_20d'] = idf.groupby('con_code')['total_amount'].transform(
    lambda x: x.rolling(20, min_periods=10).mean().shift(1)
)
idf['vol_ratio'] = idf['amount_3d'] / idf['amount_20d']

# 冷区与信号
idf['is_cold_zone'] = idf['pre_breadth_20d'] <= 0.30
idf['signal_trigger'] = idf['is_cold_zone'] & (idf['expansion_days'] >= 3)

print(f"\n核心变量:")
print(f"  冷区行业-日总数: {idf['is_cold_zone'].sum()}")
print(f"  扩张≥3天总数(全样本): {(idf['expansion_days']>=3).sum()}")
print(f"  信号触发总数(冷区+扩张≥3): {idf['signal_trigger'].sum()}")

# ============================================================
# 2. 前向超额收益
# ============================================================
market_avg = idf.groupby('date')['avg_pct'].mean().reset_index()
market_avg.columns = ['date', 'market_avg_pct']
idf = idf.merge(market_avg, on='date', how='left')

for n in [5, 10, 20]:
    col = f'forward_{n}d_excess'
    parts = []
    for _, g in idf.groupby('con_code'):
        g = g.sort_values('date')
        ind_cum = g['avg_pct'].rolling(n, min_periods=n).sum().shift(-n)
        mkt_cum = g['market_avg_pct'].rolling(n, min_periods=n).sum().shift(-n)
        parts.append(pd.Series(ind_cum - mkt_cum, index=g.index))
    idf[col] = pd.concat(parts).sort_index()

idf['forward_20d_win'] = idf['forward_20d_excess'] > 0
print("前向收益计算完成")

# ============================================================
# 3. 信号评分
# ============================================================
signals = idf[idf['signal_trigger']].copy()

def _score(row):
    base = 1
    pb = row['pre_breadth_20d']
    ps = 3 if pb < 0.10 else (2 if pb < 0.20 else (1 if pb < 0.30 else 0)) if pd.notna(pb) else 0
    d1r = row['d1_ratio']
    ds = 3 if d1r >= 0.60 else (2 if d1r >= 0.50 else (1 if d1r >= 0.40 else 0)) if pd.notna(d1r) else 0
    vr = row['vol_ratio']
    vs = 3 if vr >= 1.3 else (2 if vr >= 1.1 else (1 if vr >= 1.0 else 0)) if pd.notna(vr) else 0
    return base + ps + ds + vs

signals['score'] = signals.apply(_score, axis=1)

print(f"\n信号评分完成, 得分分布:")
print(signals['score'].value_counts().sort_index())

# ============================================================
# 4. 冷区信号按前20日均宽分层
# ============================================================
print("\n" + "="*70)
print("§四 冷区信号按前20日均宽分层")
print("="*70)

for label, lo, hi in [('极低档<10%', 0, 0.10), ('10-20%', 0.10, 0.20), ('20-30%', 0.20, 0.31)]:
    sub = signals[(signals['pre_breadth_20d'] >= lo) & (signals['pre_breadth_20d'] < hi)]
    if len(sub) == 0:
        print(f"  {label}: N=0")
        continue
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    t_val = excess.mean() / (excess.std() / np.sqrt(len(excess))) if len(excess) > 1 else 0
    print(f"  {label}: N={len(sub)}, 20日超额={excess.mean():.2%}, 胜率={win:.1%}, t={t_val:.2f}")
    # 小克对照
    xk = {'极低档<10%': (280, 9.06, 84), '10-20%': (1499, 3.41, 68), '20-30%': (2507, 2.46, 65)}
    if label in xk:
        xn, xe, xw = xk[label]
        print(f"    小克: N={xn}, 超额={xe:.2f}%, 胜率={xw}%")
        print(f"    偏差: N差{len(sub)-xn}, 超额差{excess.mean()*100-e:.2f}pp, 胜率差{win*100-xw:.1f}pp")

# ============================================================
# 5. §八叠加效果对照
# ============================================================
print("\n" + "="*70)
print("§八 叠加效果对照")
print("="*70)

combos = [
    ('仅触发(无过滤)', signals, (2432, 2.02, 62)),
    ('①<20%+③≥1.1', signals[(signals['pre_breadth_20d']<0.20)&(signals['vol_ratio']>=1.1)], (467, 2.91, 66)),
    ('②≥50%+③≥1.1', signals[(signals['d1_ratio']>=0.50)&(signals['vol_ratio']>=1.1)], (205, 3.38, 68)),
    ('②≥60%+③≥1.1', signals[(signals['d1_ratio']>=0.60)&(signals['vol_ratio']>=1.1)], (111, 4.20, 73)),
    ('①<10%+②≥60%+③≥1.2', signals[(signals['pre_breadth_20d']<0.10)&(signals['d1_ratio']>=0.60)&(signals['vol_ratio']>=1.2)], (50, 4.50, 74)),
]

for label, sub, (xn, xe, xw) in combos:
    if len(sub) == 0:
        print(f"  {label}: N=0")
        continue
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2%}, 胜率={win:.1%}")
    print(f"    小克: N={xn}, 超额={xe:.2f}%, 胜率={xw}%")
    print(f"    偏差: N差{len(sub)-xn}, 超额差{excess.mean()*100-xe:.2f}pp, 胜率差{win*100-xw:.1f}pp")

# ============================================================
# 6. §3.5 按得分档位
# ============================================================
print("\n" + "="*70)
print("§3.5 按得分档位（方案评分体系）")
print("="*70)
print("注意：评分不含涨停维度(④)，所以得分范围1-10而非2-13")

xk_score = {3: (1.1, 1.65, 62, 1.15), 5: (0.4, 3.26, 67, 1.54), 7: (0.1, 6.71, 74, 2.43)}

n_days_total = signals['date'].nunique()
for threshold in [3, 5, 7]:
    sub = signals[signals['score'] >= threshold]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    daily_n = len(sub) / n_days_total
    # IR = mean/std * sqrt(252)
    ir = excess.mean() / excess.std() * np.sqrt(252) if excess.std() > 0 else 0
    print(f"  ≥{threshold}分: N={len(sub)}, 日均{daily_n:.1f}个, 超额={excess.mean():.2%}, 胜率={win:.1%}, IR={ir:.2f}")
    xn_daily, xe, xw, xir = xk_score[threshold]
    print(f"    小克: 日均{xn_daily}个, 超额={xe:.2f}%, 胜率={xw}%, IR={xir}")
    print(f"    偏差: 超额差{excess.mean()*100-xe:.2f}pp, 胜率差{win*100-xw:.1f}pp, IR差{ir-xir:.2f}")

# ============================================================
# 7. ABC四组对照
# ============================================================
print("\n" + "="*70)
print("ABC四组对照")
print("="*70)

idf['is_hot_zone'] = idf['pre_breadth_20d'] > 0.70

groups = {
    'A:纯扩张≥3天': idf[idf['expansion_days'] >= 3],
    'B:纯冷区≤30%': idf[idf['is_cold_zone']],
    'C:冷区+扩张': idf[idf['signal_trigger']],
    'D:热区+扩张': idf[idf['is_hot_zone'] & (idf['expansion_days'] >= 3)],
}

for label, g in groups.items():
    excess = g['forward_20d_excess'].dropna()
    win = g['forward_20d_win'].mean()
    t_val = excess.mean() / (excess.std() / np.sqrt(len(excess))) if len(excess) > 1 else 0
    print(f"  {label}: N={len(g)}, 超额={excess.mean():.2%}, 胜率={win:.1%}, t={t_val:.2f}")

# ============================================================
# 8. 扩张天数分档
# ============================================================
print("\n" + "="*70)
print("§一 扩张天数分档")
print("="*70)

for days in [3, 5, 7]:
    sub = idf[idf['expansion_days'] >= days]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  扩张≥{days}天: N={len(sub)}, 超额={excess.mean():.2%}, 胜率={win:.1%}")

xk_exp = {3: (0.89, 61.5), 5: (1.82, 67.8)}
for days, (xe, xw) in xk_exp.items():
    print(f"    小克扩张≥{days}: 超额={xe:.2f}%, 胜率={xw}%")

# ============================================================
# 9. 三阶段稳定性
# ============================================================
print("\n" + "="*70)
print("三阶段稳定性")
print("="*70)

phases = {
    'P1结构牛2020-2021': (idf['date'] >= '2020-01-01') & (idf['date'] <= '2021-12-31'),
    'P2震荡熊2022-2024': (idf['date'] >= '2022-01-01') & (idf['date'] <= '2024-09-30'),
    'P3政策牛2024-2026': (idf['date'] >= '2024-10-01') & (idf['date'] <= '2026-12-31'),
}

for label, mask in phases.items():
    phase_sig = signals[mask]
    excess = phase_sig['forward_20d_excess'].dropna()
    win = phase_sig['forward_20d_win'].mean()
    print(f"  {label}: N={len(phase_sig)}, 超额={excess.mean():.2%}, 胜率={win:.1%}")

# ============================================================
# 10. 加速vs减速模式
# ============================================================
print("\n" + "="*70)
print("§七 加速vs减速模式")
print("="*70)

exp3 = idf[idf['expansion_days'] >= 3].copy()
exp3['is_decel'] = (exp3['d1'] > exp3['d2']) & (exp3['d2'] > exp3['d3']) & (exp3['d1'] > 0)
exp3['is_accel'] = (exp3['d1'] < exp3['d2']) & (exp3['d2'] < exp3['d3']) & (exp3['d3'] > 0)

for label, col in [('减速(d1>d2>d3)', 'is_decel'), ('加速(d1<d2<d3)', 'is_accel')]:
    sub = exp3[exp3[col]]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  全样本{label}: N={len(sub)}, 超额={excess.mean():.2%}, 胜率={win:.1%}")

# 冷区内
cold_exp3 = exp3[exp3['is_cold_zone']]
for label, col in [('冷区减速', 'is_decel'), ('冷区加速', 'is_accel')]:
    sub = cold_exp3[cold_exp3[col]]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2%}, 胜率={win:.1%}")

print(f"\n小克数据: 减速N=888终点>60%=76.0%, 加速N=998终点>60%=62.4%")
print(f"小克数据: 冷区减速N=275超额+2.60%, 冷区加速N=419超额+1.67%")

# ============================================================
# 11. 热区候选信号
# ============================================================
print("\n" + "="*70)
print("§四 热区候选信号")
print("="*70)

hot = idf[idf['is_hot_zone']].copy()
# 急跌: avg_pct < -3
hot['is_sharp_drop'] = hot['avg_pct'] < -3
# 温跌: 5日累计 < -2%
hot['cum_5d'] = hot.groupby('con_code')['avg_pct'].transform(
    lambda x: x.rolling(5, min_periods=5).sum().shift(0)
)
hot['is_slow_drop'] = hot['cum_5d'] < -2

for label, mask in [('宽高+急跌(<-3%)', hot['is_sharp_drop']), ('宽高+温跌(5日<-2%)', hot['is_slow_drop'])]:
    sub = hot[mask]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2%}, 超额={excess.mean():.2%}, 胜率={win:.1%}")

print(f"  小克: 宽高+急跌N=1087超额+3.01%胜率65%")
print(f"  小克: 宽高+温跌N=1873超额+2.95%胜率63%")

# ============================================================
# 12. 中区候选信号
# ============================================================
print("\n" + "="*70)
print("§四 中区候选信号")
print("="*70)

mid = idf[(idf['pre_breadth_20d'] > 0.30) & (idf['pre_breadth_20d'] <= 0.70)].copy()
# 5日新高: avg_pct在近5日中为最高
mid['max_5d_pct'] = mid.groupby('con_code')['avg_pct'].transform(
    lambda x: x.rolling(5, min_periods=5).max().shift(1)
)
mid['is_5d_new_high'] = mid['avg_pct'] > mid['max_5d_pct']
# 放量+涨
mid['is_vol_up'] = (mid['avg_pct'] > 0) & (mid['vol_ratio'] >= 1.0)

for label, mask in [('5日新高+宽度上升', mid['is_5d_new_high'] & mid['is_expanding']), ('放量+涨', mid['is_vol_up'])]:
    sub = mid[mask]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2%}, 胜率={win:.1%}")

print(f"  小克: 价格突破+宽度上升N=1397超额+1.85%胜率64%")
print(f"  小克: 放量+涨N=2631超额+0.75%胜率57%")

# ============================================================
# 13. 宽度反转 vs 冷区均值回归（关键因果验证）
# ============================================================
print("\n" + "="*70)
print("因果归因验证：扩张 vs 冷区均值回归")
print("="*70)

# 冷区中：有扩张 vs 无扩张
cold = idf[idf['is_cold_zone']].copy()
cold_with_exp = cold[cold['expansion_days'] >= 3]
cold_no_exp = cold[cold['expansion_days'] < 3]

for label, sub in [('冷区+扩张', cold_with_exp), ('冷区无扩张', cold_no_exp)]:
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    t_val = excess.mean() / (excess.std() / np.sqrt(len(excess))) if len(excess) > 1 else 0
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2%}, 超额={excess.mean():.2%}, 胜率={win:.1%}, t={t_val:.2f}")

# 扩张增量效应：冷区+扩张 vs 冷区无扩张的超额差
diff = cold_with_exp['forward_20d_excess'].mean() - cold_no_exp['forward_20d_excess'].mean()
print(f"\n  扩张增量效应: {diff:.2%}")
print(f"  如果>0: 扩张确实增加冷区超额 → 支持因果归因'宽度反转'")
print(f"  如果≈0: 扩张不影响冷区超额 → 超额来自冷区均值回归而非扩张")

print("\n" + "="*70)
print("复核回测完成")
print("="*70)
