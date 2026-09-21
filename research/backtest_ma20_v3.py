"""模块二宽度反转信号复核回测 v3 — MA20占比宽度，完整一次性脚本"""
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = '/opt/data/quant-data'

# 0. 加载
idf = pd.read_parquet(f'{DATA_DIR}/industry/industry_daily_full.parquet')
im = pd.read_parquet(f'{DATA_DIR}/industry/industry_members.parquet')
ind_names = im.drop_duplicates('ind_code')[['ind_code','ind_name']].rename(columns={'ind_code':'con_code'})
idf = idf.merge(ind_names, on='con_code', how='left')
idf = idf.sort_values(['con_code', 'date']).reset_index(drop=True)
print(f"数据: {idf.shape[0]}条, {idf['con_code'].nunique()}行业")

# 1. 前20日均宽(不含当日)
idf['pre_breadth_20d'] = idf.groupby('con_code')['breadth'].transform(
    lambda x: x.rolling(20, min_periods=10).mean().shift(1)
)

# 2. 连续扩张天数
idf['breadth_diff'] = idf.groupby('con_code')['breadth'].diff()
idf['is_expanding'] = idf['breadth_diff'] > 0
def _cc(s):
    r = pd.Series(0, index=s.index, dtype=int); c = 0
    for i in range(len(s)):
        if s.iloc[i]: c += 1; r.iloc[i] = c
        else: c = 0
    return r
idf['expansion_days'] = idf.groupby('con_code')['is_expanding'].transform(_cc)

# 3. 扩张首日前20日均宽
idf['exp_start_pre_breadth'] = np.nan
for con_code, group in idf.groupby('con_code'):
    idx = group.index
    exp_days = group['expansion_days'].values
    pre_b = group['pre_breadth_20d'].values
    result = np.full(len(exp_days), np.nan)
    for i in range(len(exp_days)):
        if exp_days[i] >= 3:
            first_i = i - (int(exp_days[i]) - 1)
            if first_i >= 0:
                result[i] = pre_b[first_i]
    idf.loc[idx, 'exp_start_pre_breadth'] = result

# 4. D1占比
idf['d1'] = idf.groupby('con_code')['breadth_diff'].shift(2)
idf['d2'] = idf.groupby('con_code')['breadth_diff'].shift(1)
idf['d3'] = idf['breadth_diff']
idf['d1_ratio'] = np.where(
    (idf['expansion_days'] >= 3) & ((idf['d1'] + idf['d2'] + idf['d3']) > 0),
    idf['d1'] / (idf['d1'] + idf['d2'] + idf['d3']), np.nan
)

# 5. 放量
idf['amount_3d'] = idf.groupby('con_code')['total_amount'].transform(
    lambda x: x.rolling(3, min_periods=2).mean()
)
idf['amount_20d'] = idf.groupby('con_code')['total_amount'].transform(
    lambda x: x.rolling(20, min_periods=10).mean().shift(1)
)
idf['vol_ratio'] = idf['amount_3d'] / idf['amount_20d']

# 6. 冷区与信号
idf['is_cold_zone'] = idf['exp_start_pre_breadth'] <= 0.30
idf['signal_trigger'] = idf['is_cold_zone'] & (idf['expansion_days'] >= 3)

# 7. 前向超额收益
market_avg = idf.groupby('date')['avg_pct'].mean().reset_index()
market_avg.columns = ['date', 'market_avg_pct']
idf = idf.merge(market_avg, on='date', how='left')

for n in [5, 10, 20]:
    parts = []
    for _, g in idf.groupby('con_code'):
        g = g.sort_values('date')
        ind_cum = g['avg_pct'].rolling(n, min_periods=n).sum().shift(-n)
        mkt_cum = g['market_avg_pct'].rolling(n, min_periods=n).sum().shift(-n)
        parts.append(pd.Series(ind_cum - mkt_cum, index=g.index))
    idf[f'forward_{n}d_excess'] = pd.concat(parts).sort_index()

idf['forward_20d_win'] = idf['forward_20d_excess'] > 0
print("前向收益计算完成")

# 8. 评分
signals = idf[idf['signal_trigger']].copy()
def _sc(row):
    b = 1
    pb = row['exp_start_pre_breadth']
    ps = 3 if pb < 0.10 else (2 if pb < 0.20 else (1 if pb < 0.30 else 0)) if pd.notna(pb) else 0
    d1r = row['d1_ratio']
    ds = 3 if d1r >= 0.60 else (2 if d1r >= 0.50 else (1 if d1r >= 0.40 else 0)) if pd.notna(d1r) else 0
    vr = row['vol_ratio']
    vs = 3 if vr >= 1.3 else (2 if vr >= 1.1 else (1 if vr >= 1.0 else 0)) if pd.notna(vr) else 0
    return b + ps + ds + vs
signals['score'] = signals.apply(_sc, axis=1)
print(f"评分完成, N={len(signals)}")

# ============================================================
# A. §四 冷区信号按前20日均宽分层
# ============================================================
print("\n" + "="*70)
print("A. §四 冷区信号按扩张前20日均宽分层")
print("="*70)

for label, lo, hi in [('极低档<10%', 0, 0.10), ('10-20%', 0.10, 0.20), ('20-30%', 0.20, 0.31)]:
    sub = signals[(signals['exp_start_pre_breadth'] >= lo) & (signals['exp_start_pre_breadth'] < hi)]
    if len(sub) == 0: print(f"  {label}: N=0"); continue
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    t_val = excess.mean() / (excess.std() / np.sqrt(len(excess))) if len(excess) > 1 else 0
    print(f"  {label}: N={len(sub)}, 20日超额={excess.mean():.2f}%, 胜率={win:.1%}, t={t_val:.2f}")
    xk = {'极低档<10%': (280, 9.06, 84), '10-20%': (1499, 3.41, 68), '20-30%': (2507, 2.46, 65)}
    if label in xk:
        xn, xe, xw = xk[label]
        print(f"    小克: N={xn}, 超额={xe:.2f}%, 胜率={xw}%")
        print(f"    偏差: N差{len(sub)-xn}, 超额差{excess.mean()-xe:.2f}pp, 胜率差{win*100-xw:.1f}pp")

# ============================================================
# B. §八叠加效果对照
# ============================================================
print("\n" + "="*70)
print("B. §八叠加效果对照")
print("="*70)

combos = [
    ('仅触发(无过滤)', signals, (2432, 2.02, 62)),
    ('①<20%+③≥1.1', signals[(signals['exp_start_pre_breadth']<0.20)&(signals['vol_ratio']>=1.1)], (467, 2.91, 66)),
    ('②≥50%+③≥1.1', signals[(signals['d1_ratio']>=0.50)&(signals['vol_ratio']>=1.1)], (205, 3.38, 68)),
    ('②≥60%+③≥1.1', signals[(signals['d1_ratio']>=0.60)&(signals['vol_ratio']>=1.1)], (111, 4.20, 73)),
    ('①<10%+②≥60%+③≥1.2', signals[(signals['exp_start_pre_breadth']<0.10)&(signals['d1_ratio']>=0.60)&(signals['vol_ratio']>=1.2)], (50, 4.50, 74)),
]

for label, sub, (xn, xe, xw) in combos:
    if len(sub) == 0: print(f"  {label}: N=0"); continue
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2f}%, 胜率={win:.1%}")
    print(f"    小克: N={xn}, 超额={xe:.2f}%, 胜率={xw}%")
    print(f"    偏差: N差{len(sub)-xn}, 超额差{excess.mean()-xe:.2f}pp, 胜率差{win*100-xw:.1f}pp")

# ============================================================
# C. §3.5按得分档位
# ============================================================
print("\n" + "="*70)
print("C. §3.5按得分档位（不含涨停维度）")
print("="*70)

xk_score = {3: (1.1, 1.65, 62, 1.15), 5: (0.4, 3.26, 67, 1.54), 7: (0.1, 6.71, 74, 2.43)}
n_days = signals['date'].nunique()

for threshold in [3, 5, 7]:
    sub = signals[signals['score'] >= threshold]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    daily_n = len(sub) / n_days
    ir = excess.mean() / excess.std() * np.sqrt(252) if excess.std() > 0 else 0
    print(f"  ≥{threshold}分: N={len(sub)}, 日均{daily_n:.2f}个, 超额={excess.mean():.2f}%, 胜率={win:.1%}, IR={ir:.2f}")
    xn_d, xe, xw, xir = xk_score[threshold]
    print(f"    小克: 日均{xn_d}个, 超额={xe:.2f}%, 胜率={xw}%, IR={xir}")
    print(f"    偏差: 超额差{excess.mean()-xe:.2f}pp, 胜率差{win*100-xw:.1f}pp, IR差{ir-xir:.2f}")

# ============================================================
# D. ABC四组对照
# ============================================================
print("\n" + "="*70)
print("D. ABC四组对照")
print("="*70)

idf['is_hot_zone'] = idf['exp_start_pre_breadth'] > 0.70

for label, mask in [
    ('A:纯扩张≥3天', idf['expansion_days'] >= 3),
    ('B:纯冷区≤30%', idf['exp_start_pre_breadth'] <= 0.30),
    ('C:冷区+扩张', idf['signal_trigger']),
    ('D:热区+扩张', idf['is_hot_zone'] & (idf['expansion_days'] >= 3)),
]:
    g = idf[mask]
    excess = g['forward_20d_excess'].dropna()
    win = g['forward_20d_win'].mean()
    t_val = excess.mean() / (excess.std() / np.sqrt(len(excess))) if len(excess) > 1 else 0
    print(f"  {label}: N={len(g)}, 超额={excess.mean():.2f}%, 超额={excess.mean():.2f}%, 胜率={win:.1%}, t={t_val:.2f}")

# ============================================================
# E. 扩张天数分档
# ============================================================
print("\n" + "="*70)
print("E. §一扩张天数分档(全样本)")
print("="*70)

for days in [3, 5, 7]:
    sub = idf[idf['expansion_days'] >= days]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  扩张≥{days}天: N={len(sub)}, 超额={excess.mean():.2f}%, 超额={excess.mean():.2f}%, 超额={excess.mean():.2f}%, 胜率={win:.1%}")
print(f"  小克: ≥3天超额0.89%胜率61.5%; ≥5天超额1.82%胜率67.8%")

# ============================================================
# F. 三阶段稳定性
# ============================================================
print("\n" + "="*70)
print("F. 三阶段稳定性")
print("="*70)

for label, mask in [
    ('P1结构牛2020-2021', (idf['date'] >= '2020-01-01') & (idf['date'] <= '2021-12-31')),
    ('P2震荡熊2022-2024', (idf['date'] >= '2022-01-01') & (idf['date'] <= '2024-09-30')),
    ('P3政策牛2024-2026', (idf['date'] >= '2024-10-01') & (idf['date'] <= '2026-12-31')),
]:
    sub = signals[mask]
    if len(sub) == 0: print(f"  {label}: N=0"); continue
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2f}%, 胜率={win:.1%}")

# ============================================================
# G. 加速vs减速
# ============================================================
print("\n" + "="*70)
print("G. §七加速vs减速")
print("="*70)

exp3 = idf[idf['expansion_days'] >= 3].copy()
exp3['is_decel'] = (exp3['d1'] > exp3['d2']) & (exp3['d2'] > exp3['d3']) & (exp3['d1'] > 0)
exp3['is_accel'] = (exp3['d1'] < exp3['d2']) & (exp3['d2'] < exp3['d3']) & (exp3['d3'] > 0)

for label, col in [('减速(d1>d2>d3)', 'is_decel'), ('加速(d1<d2<d3)', 'is_accel')]:
    sub = exp3[exp3[col]]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  全样本{label}: N={len(sub)}, 超额={excess.mean():.2f}%, 超额={excess.mean():.2f}%, 胜率={win:.1%}")

cold_exp3 = exp3[exp3['signal_trigger']]
for label, col in [('冷区减速', 'is_decel'), ('冷区加速', 'is_accel')]:
    sub = cold_exp3[cold_exp3[col]]
    if len(sub) == 0: print(f"  {label}: N=0"); continue
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2f}%, 超额={excess.mean():.2f}%, 胜率={win:.1%}")
print(f"  小克: 减速N=888/冷区减速N=275超额+2.60%; 加速N=998/冷区加速N=419超额+1.67%")

# ============================================================
# H. 因果归因: 冷区中扩张 vs 无扩张
# ============================================================
print("\n" + "="*70)
print("H. 因果归因验证: 冷区中扩张 vs 无扩张")
print("="*70)

cold = idf[idf['exp_start_pre_breadth'] <= 0.30].copy()
cold_exp = cold[cold['expansion_days'] >= 3]
cold_no_exp = cold[cold['expansion_days'] < 3]

for label, sub in [('冷区+扩张≥3天', cold_exp), ('冷区无扩张', cold_no_exp)]:
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    t_val = excess.mean() / (excess.std() / np.sqrt(len(excess))) if len(excess) > 1 else 0
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2f}%, 胜率={win:.1%}, t={t_val:.2f}")

diff = cold_exp['forward_20d_excess'].mean() - cold_no_exp['forward_20d_excess'].mean()
print(f"\n  扩张增量效应: {diff:.2f}%")
print(f"  >0 → 因果归因'宽度反转'成立; ≈0/<0 → 冷区均值回归为主")

# ============================================================
# I. 热区与中区信号
# ============================================================
print("\n" + "="*70)
print("I. 热区与中区候选信号")
print("="*70)

hot = idf[idf['exp_start_pre_breadth'] > 0.70].copy()
for label, m in [('宽高+急跌(<-3%)', hot['avg_pct'] < -3)]:
    sub = hot[m]
    excess = sub['forward_20d_excess'].dropna()
    win = sub['forward_20d_win'].mean()
    print(f"  {label}: N={len(sub)}, 超额={excess.mean():.2f}%, 胜率={win:.1%}")
print(f"  小克: 急跌N=1087超额+3.01%胜率65%")

print("\n=== MA20宽度复核回测完成 ===")
