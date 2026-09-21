"""
逐行业IC分析：找出5因子与未来收益正相关最强的前20个行业
"""
import pandas as pd
import numpy as np
import os, sys, warnings
from scipy import stats
warnings.filterwarnings('ignore')

DATA_DIR = '/opt/data/quant-data'
MARKET_DAILY = f'{DATA_DIR}/market/daily'
IND_DIR = f'{DATA_DIR}/industry'

# ═══════════════════════════════════════════
# 1. 数据加载（复用之前的逻辑）
# ═══════════════════════════════════════════
ind_members = pd.read_parquet(f'{IND_DIR}/industry_members.parquet')
ind_list = pd.read_parquet(f'{IND_DIR}/industry_list.parquet')

# 行业名称映射
ind_name_map = dict(zip(ind_list['ts_code'], ind_list['name']))

daily_files = sorted([f for f in os.listdir(MARKET_DAILY) 
                      if (f.startswith('2024') or f.startswith('2025-01')) and f.endswith('.parquet')])
frames = [pd.read_parquet(os.path.join(MARKET_DAILY, f)) for f in daily_files]
all_daily = pd.concat(frames, ignore_index=True)

valid_prefixes = ["600","601","603","605","688","689","000","001","002","003","300","301","302","920"]
code_prefix = all_daily['code'].str.split('.').str[1].str[:3]
a_stock = all_daily[code_prefix.isin(valid_prefixes)].copy()
a_stock = a_stock[~a_stock['code'].str.match(r'^sh\.000')]
a_stock = a_stock[~a_stock['code'].str.match(r'^sz\.399')]
a_stock = a_stock[~a_stock['code'].str.match(r'^sh\.5\d{5}')]
a_stock = a_stock[~a_stock['code'].str.match(r'^sz\.15\d{5}')]
a_stock = a_stock[~a_stock['stock_name'].str.contains('ST', na=False)]

code_to_ind = dict(zip(ind_members['stock_code'], ind_members['ind_code']))
a_stock['ind_code'] = a_stock['code'].map(code_to_ind)
a_stock_mapped = a_stock[a_stock['ind_code'].notna()].copy()

# ═══════════════════════════════════════════
# 2. 连板计算
# ═══════════════════════════════════════════
a_stock_mapped['is_limit_up'] = a_stock_mapped['pctChg'] >= 10
a_stock_mapped['is_limit_down'] = a_stock_mapped['pctChg'] <= -10
a_stock_mapped = a_stock_mapped.sort_values(['code', 'date'])

def calc_consec(group):
    consecutive = []
    count = 0
    for is_up in group['is_limit_up']:
        if is_up: count += 1
        else: count = 0
        consecutive.append(count)
    return pd.Series(consecutive, index=group.index)

a_stock_mapped['consec_limit'] = a_stock_mapped.groupby('code', group_keys=False).apply(calc_consec)

def limit_mult(n):
    if n <= 0: return 0
    if n == 1: return 1.0
    if n == 2: return 1.5
    if n == 3: return 2.0
    return 2.5

a_stock_mapped['limit_mult'] = a_stock_mapped['consec_limit'].apply(limit_mult)

# ═══════════════════════════════════════════
# 3. 自算行业等权涨跌幅 + 未来收益
# ═══════════════════════════════════════════
ind_daily_ret = a_stock_mapped.groupby(['ind_code', 'date'])['pctChg'].mean().reset_index()
ind_daily_ret.columns = ['ind_code', 'date', 'weighted_pct']

ind_daily_ret = ind_daily_ret.sort_values(['ind_code', 'date'])
ind_daily_ret['future_ret_5'] = ind_daily_ret.groupby('ind_code')['weighted_pct'].transform(
    lambda x: x.rolling(5).sum().shift(-5)
)
ind_daily_ret['future_ret_10'] = ind_daily_ret.groupby('ind_code')['weighted_pct'].transform(
    lambda x: x.rolling(10).sum().shift(-10)
)

ind_daily_ret_2024 = ind_daily_ret[(ind_daily_ret['date'] >= '2024-01-01') & 
                                    (ind_daily_ret['date'] <= '2024-12-31')].copy()

# ═══════════════════════════════════════════
# 4. 计算行业级5因子
# ═══════════════════════════════════════════
a_stock_2024 = a_stock_mapped[(a_stock_mapped['date'] >= '2024-01-01') & 
                               (a_stock_mapped['date'] <= '2024-12-31')].copy()

grouped = a_stock_2024.groupby(['ind_code', 'date'])

factor_rise = grouped['pctChg'].mean().reset_index()
factor_rise.columns = ['ind_code', 'date', 'rise']

factor_breadth = grouped.apply(lambda g: (g['pctChg'] > 0).mean()).reset_index()
factor_breadth.columns = ['ind_code', 'date', 'breadth']

factor_amount = grouped['amount'].sum().reset_index()
factor_amount.columns = ['ind_code', 'date', 'amount']

factor_crowd = grouped['turn'].mean().reset_index()
factor_crowd.columns = ['ind_code', 'date', 'crowd']

factor_emotion = grouped.apply(lambda g: 
    g.loc[g['is_limit_up'], 'limit_mult'].sum() - g['is_limit_down'].sum()
).reset_index()
factor_emotion.columns = ['ind_code', 'date', 'emotion']

factors = factor_rise.merge(factor_breadth, on=['ind_code', 'date'])
factors = factors.merge(factor_amount, on=['ind_code', 'date'])
factors = factors.merge(factor_crowd, on=['ind_code', 'date'])
factors = factors.merge(factor_emotion, on=['ind_code', 'date'])

# 截面rank标准化
factor_cols = ['rise', 'breadth', 'amount', 'crowd', 'emotion']
for col in factor_cols:
    factors[col + '_rank'] = factors.groupby('date')[col].rank(pct=True)

# 合并因子 + 未来收益
panel = factors.merge(
    ind_daily_ret_2024[['ind_code', 'date', 'future_ret_5', 'future_ret_10']], 
    on=['ind_code', 'date'], how='inner'
)

# ═══════════════════════════════════════════
# 5. 逐行业IC计算
# ═══════════════════════════════════════════
print("="*70)
print("逐行业IC分析：5因子与未来5日收益的正相关行业TOP20")
print("="*70)

# 对每个行业，计算5个因子各自的IC，以及合成分（等权）的IC
industry_results = []

for ind_code in panel['ind_code'].unique():
    ind_panel = panel[panel['ind_code'] == ind_code].copy()
    n_days = len(ind_panel)
    
    if n_days < 50:  # 至少50天才有意义
        continue
    
    # 每个因子的IC（与未来5日收益的Spearman相关）
    ics = {}
    for fc in factor_cols:
        valid = ind_panel[[fc, 'future_ret_5']].dropna()
        if len(valid) < 30:
            ics[fc] = np.nan
            continue
        ic_val, _ = stats.spearmanr(valid[fc], valid['future_ret_5'])
        ics[fc] = ic_val
    
    # 合成分IC（等权合成分 vs future_ret_5）
    ind_panel['composite_eq'] = ind_panel[[fc + '_rank' for fc in factor_cols]].mean(axis=1)
    valid_comp = ind_panel[['composite_eq', 'future_ret_5']].dropna()
    if len(valid_comp) < 30:
        ics['composite'] = np.nan
    else:
        ic_comp, _ = stats.spearmanr(valid_comp['composite_eq'], valid_comp['future_ret_5'])
        ics['composite'] = ic_comp
    
    # 也计算与未来10日的IC
    ics_10 = {}
    for fc in factor_cols:
        valid = ind_panel[[fc, 'future_ret_10']].dropna()
        if len(valid) < 30:
            ics_10[fc] = np.nan
            continue
        ic_val, _ = stats.spearmanr(valid[fc], valid['future_ret_10'])
        ics_10[fc] = ic_val
    
    valid_comp10 = ind_panel[['composite_eq', 'future_ret_10']].dropna()
    if len(valid_comp10) < 30:
        ics_10['composite'] = np.nan
    else:
        ic_comp10, _ = stats.spearmanr(valid_comp10['composite_eq'], valid_comp10['future_ret_10'])
        ics_10['composite'] = ic_comp10
    
    ind_name = ind_name_map.get(ind_code, '未知')
    industry_results.append({
        'ind_code': ind_code,
        'ind_name': ind_name,
        'n_days': n_days,
        'rise_5': ics.get('rise', np.nan),
        'breadth_5': ics.get('breadth', np.nan),
        'amount_5': ics.get('amount', np.nan),
        'crowd_5': ics.get('crowd', np.nan),
        'emotion_5': ics.get('emotion', np.nan),
        'composite_5': ics.get('composite', np.nan),
        'rise_10': ics_10.get('rise', np.nan),
        'breadth_10': ics_10.get('breadth', np.nan),
        'amount_10': ics_10.get('amount', np.nan),
        'crowd_10': ics_10.get('crowd', np.nan),
        'emotion_10': ics_10.get('emotion', np.nan),
        'composite_10': ics_10.get('composite', np.nan),
    })

results_df = pd.DataFrame(industry_results)
print(f"有效行业数: {len(results_df)}")

# ═══════════════════════════════════════════
# 6. 排出正相关TOP20（按合成分IC排序）
# ═══════════════════════════════════════════
# 合成分等权IC（5日）排序
top20_5 = results_df.nlargest(20, 'composite_5')
print(f"\n--- TOP20正相关行业（合成分等权 vs 未来5日收益）---")
print(f"{'排名':>4s} | {'行业代码':>15s} | {'行业名称':>15s} | {'合成分IC(5日)':>14s} | {'涨幅IC':>8s} | {'宽度IC':>8s} | {'量能IC':>8s} | {'拥挤IC':>8s} | {'情绪IC':>8s} | {'天数':>4s}")
print("-" * 110)
for i, row in top20_5.iterrows():
    print(f"{top20_5.index.get_loc(i)+1:>4d} | {row['ind_code']:>15s} | {row['ind_name']:>15s} | {row['composite_5']:>14.4f} | {row['rise_5']:>8.4f} | {row['breadth_5']:>8.4f} | {row['amount_5']:>8.4f} | {row['crowd_5']:>8.4f} | {row['emotion_5']:>8.4f} | {row['n_days']:>4d}")

# 合成分等权IC（10日）排序
top20_10 = results_df.nlargest(20, 'composite_10')
print(f"\n--- TOP20正相关行业（合成分等权 vs 未来10日收益）---")
print(f"{'排名':>4s} | {'行业代码':>15s} | {'行业名称':>15s} | {'合成分IC(10日)':>14s} | {'涨幅IC':>8s} | {'宽度IC':>8s} | {'量能IC':>8s} | {'拥挤IC':>8s} | {'情绪IC':>8s} | {'天数':>4s}")
print("-" * 110)
for i, row in top20_10.iterrows():
    print(f"{top20_10.index.get_loc(i)+1:>4d} | {row['ind_code']:>15s} | {row['ind_name']:>15s} | {row['composite_10']:>14.4f} | {row['rise_10']:>8.4f} | {row['breadth_10']:>8.4f} | {row['amount_10']:>8.4f} | {row['crowd_10']:>8.4f} | {row['emotion_10']:>8.4f} | {row['n_days']:>4d}")

# ═══════════════════════════════════════════
# 7. 也看全市场汇总：正/负/零IC的行业分布
# ═══════════════════════════════════════════
print(f"\n--- IC方向分布（合成分等权 vs 未来5日收益）---")
positive = (results_df['composite_5'] > 0.05).sum()
negative = (results_df['composite_5'] < -0.05).sum()
neutral = len(results_df) - positive - negative
print(f"正相关(IC>0.05): {positive} 行业 ({positive/len(results_df)*100:.1f}%)")
print(f"负相关(IC<-0.05): {negative} 行业 ({negative/len(results_df)*100:.1f}%)")
print(f"近零相关: {neutral} 行业 ({neutral/len(results_df)*100:.1f}%)")

print(f"\n--- IC方向分布（合成分等权 vs 未来10日收益）---")
positive10 = (results_df['composite_10'] > 0.05).sum()
negative10 = (results_df['composite_10'] < -0.05).sum()
neutral10 = len(results_df) - positive10 - negative10
print(f"正相关(IC>0.05): {positive10} 行业 ({positive10/len(results_df)*100:.1f}%)")
print(f"负相关(IC<-0.05): {negative10} 行业 ({negative10/len(results_df)*100:.1f}%)")
print(f"近零相关: {neutral10} 行业 ({neutral10/len(results_df)*100:.1f}%)")

# ═══════════════════════════════════════════
# 8. 各因子逐行业IC统计
# ═══════════════════════════════════════════
print(f"\n--- 各因子逐行业IC统计（5日）---")
print(f"{'因子':>10s} | {'正IC(>0.05)':>12s} | {'负IC(<-0.05)':>12s} | {'均值IC':>8s} | {'中位IC':>8s}")
print("-" * 60)
for fc in factor_cols:
    col = fc + '_5'
    pos = (results_df[col] > 0.05).sum()
    neg = (results_df[col] < -0.05).sum()
    mean_ic = results_df[col].mean()
    med_ic = results_df[col].median()
    print(f"{fc:>10s} | {pos:>12d} | {neg:>12d} | {mean_ic:>8.4f} | {med_ic:>8.4f}")

print("\n=== 逐行业IC分析完成 ===")
