"""
5因子IC分析：涨幅/宽度/量能/拥挤度/情绪 → 行业未来5日/10日收益预测力
数据：2024年全市场，131个申万行业
自算行业加权涨跌幅（因为industry_weighted_returns只有2026年数据）
"""
import pandas as pd
import numpy as np
import os, sys, warnings
from scipy import stats
warnings.filterwarnings('ignore')

DATA_DIR = '/opt/data/quant-data'
MARKET_DAILY = f'{DATA_DIR}/market/daily'
IND_DIR = f'{DATA_DIR}/industry'

print("="*60)
print("5因子IC分析 — 评估权重配置")
print("="*60)

# ═══════════════════════════════════════════
# 1. 数据加载与过滤
# ═══════════════════════════════════════════
ind_members = pd.read_parquet(f'{IND_DIR}/industry_members.parquet')
ind_list = pd.read_parquet(f'{IND_DIR}/industry_list.parquet')

daily_files = sorted([f for f in os.listdir(MARKET_DAILY) 
                      if f.startswith('2024') and f.endswith('.parquet')])
# 也需要2025年初的数据（用于计算2024年底的10日未来收益）
daily_files_25 = sorted([f for f in os.listdir(MARKET_DAILY) 
                         if f.startswith('2025-01') and f.endswith('.parquet')])
all_files = daily_files + daily_files_25

frames = [pd.read_parquet(os.path.join(MARKET_DAILY, f)) for f in all_files]
all_daily = pd.concat(frames, ignore_index=True)

# 过滤A股个股
valid_prefixes = ["600","601","603","605","688","689","000","001","002","003","300","301","302","920"]
code_prefix = all_daily['code'].str.split('.').str[1].str[:3]
a_stock = all_daily[code_prefix.isin(valid_prefixes)].copy()
a_stock = a_stock[~a_stock['code'].str.match(r'^sh\.000')]
a_stock = a_stock[~a_stock['code'].str.match(r'^sz\.399')]
a_stock = a_stock[~a_stock['code'].str.match(r'^sh\.5\d{5}')]
a_stock = a_stock[~a_stock['code'].str.match(r'^sz\.15\d{5}')]
a_stock = a_stock[~a_stock['stock_name'].str.contains('ST', na=False)]

# 行业映射
code_to_ind = dict(zip(ind_members['stock_code'], ind_members['ind_code']))
a_stock['ind_code'] = a_stock['code'].map(code_to_ind)
a_stock_mapped = a_stock[a_stock['ind_code'].notna()].copy()

print(f"A股个股(非ST,有行业): {len(a_stock_mapped)} 行")
print(f"唯一股票: {a_stock_mapped['code'].nunique()}, 行业: {a_stock_mapped['ind_code'].nunique()}")
print(f"日期范围: {a_stock_mapped['date'].min()} ~ {a_stock_mapped['date'].max()}")

# ═══════════════════════════════════════════
# 2. 计算连板信息
# ═══════════════════════════════════════════
a_stock_mapped['is_limit_up'] = a_stock_mapped['pctChg'] >= 10
a_stock_mapped['is_limit_down'] = a_stock_mapped['pctChg'] <= -10

a_stock_mapped = a_stock_mapped.sort_values(['code', 'date'])

# 计算连续涨停天数
def calc_consecutive_limits(group):
    consecutive = []
    count = 0
    for is_up in group['is_limit_up']:
        if is_up:
            count += 1
        else:
            count = 0
        consecutive.append(count)
    return pd.Series(consecutive, index=group.index)

a_stock_mapped['consec_limit'] = a_stock_mapped.groupby('code', group_keys=False).apply(calc_consecutive_limits)

def limit_multiplier(n):
    if n <= 0: return 0
    if n == 1: return 1.0
    if n == 2: return 1.5
    if n == 3: return 2.0
    return 2.5

a_stock_mapped['limit_mult'] = a_stock_mapped['consec_limit'].apply(limit_multiplier)

# ═══════════════════════════════════════════
# 3. 自算行业加权涨跌幅（用total_mv加权）
# ═══════════════════════════════════════════
# 加载daily_basic获取total_mv
db_dir = f'{DATA_DIR}/daily_basic'
db_files = sorted([f for f in os.listdir(db_dir) 
                   if (f.startswith('2024') or f.startswith('2025-01')) and f.endswith('.parquet')])

if len(db_files) > 0:
    db_frames = [pd.read_parquet(os.path.join(db_dir, f)) for f in db_files]
    all_db = pd.concat(db_frames, ignore_index=True)
    print(f"daily_basic: {len(all_db)} 行, 列: {all_db.columns.tolist()}")
    
    # 对齐code格式
    if 'ts_code' in all_db.columns:
        # Tushare格式 → BaoStock格式
        all_db['code_bs'] = all_db['ts_code'].apply(
            lambda x: f"{x.split('.')[1].lower()}.{x.split('.')[0]}" if '.' in str(x) else x
        )
        
        # 合并total_mv到a_stock_mapped
        mv_data = all_db[['code_bs', 'trade_date', 'total_mv']].copy()
        mv_data['date'] = pd.to_datetime(mv_data['trade_date'])
        mv_data = mv_data.rename(columns={'total_mv': 'total_mv'})
        
        a_stock_mapped = a_stock_mapped.merge(
            mv_data[['code_bs', 'date', 'total_mv']], 
            left_on=['code', 'date'], 
            right_on=['code_bs', 'date'], 
            how='left'
        )
        print(f"合并total_mv后非空: {a_stock_mapped['total_mv'].notna().sum()}/{len(a_stock_mapped)}")
    else:
        print("daily_basic没有ts_code列，需检查格式")
        all_db = None
else:
    print("无daily_basic数据，用等权计算行业涨跌幅")
    a_stock_mapped['total_mv'] = np.nan

# 计算行业加权涨跌幅
if a_stock_mapped['total_mv'].notna().sum() > len(a_stock_mapped) * 0.5:
    # 有足够的市值数据，用总市值加权
    def weighted_pct(group):
        valid = group.dropna(subset=['total_mv', 'pctChg'])
        if len(valid) == 0:
            return group['pctChg'].mean()
        return (valid['pctChg'] * valid['total_mv']).sum() / valid['total_mv'].sum()
    
    ind_daily_ret = a_stock_mapped.groupby(['ind_code', 'date']).apply(weighted_pct).reset_index()
    ind_daily_ret.columns = ['ind_code', 'date', 'weighted_pct']
else:
    # 无足够市值数据，用等权
    ind_daily_ret = a_stock_mapped.groupby(['ind_code', 'date'])['pctChg'].mean().reset_index()
    ind_daily_ret.columns = ['ind_code', 'date', 'weighted_pct']

print(f"行业日收益: {len(ind_daily_ret)} 行, 行业数: {ind_daily_ret['ind_code'].nunique()}")

# ═══════════════════════════════════════════
# 4. 计算未来5日/10日行业累计收益
# ═══════════════════════════════════════════
ind_daily_ret = ind_daily_ret.sort_values(['ind_code', 'date'])

# 未来5日累计收益
ind_daily_ret['future_ret_5'] = ind_daily_ret.groupby('ind_code')['weighted_pct'].transform(
    lambda x: x.rolling(5).sum().shift(-5)
)

# 未来10日累计收益
ind_daily_ret['future_ret_10'] = ind_daily_ret.groupby('ind_code')['weighted_pct'].transform(
    lambda x: x.rolling(10).sum().shift(-10)
)

# 过滤2024年
ind_daily_ret_2024 = ind_daily_ret[(ind_daily_ret['date'] >= '2024-01-01') & 
                                    (ind_daily_ret['date'] <= '2024-12-31')].copy()

print(f"2024年行业日收益: {len(ind_daily_ret_2024)} 行")
print(f"future_ret_5 非空: {ind_daily_ret_2024['future_ret_5'].notna().sum()}")
print(f"future_ret_10 非空: {ind_daily_ret_2024['future_ret_10'].notna().sum()}")

# ═══════════════════════════════════════════
# 5. 按行业×日期计算5个因子
# ═══════════════════════════════════════════
# 只用2024年的a_stock数据
a_stock_2024 = a_stock_mapped[(a_stock_mapped['date'] >= '2024-01-01') & 
                               (a_stock_mapped['date'] <= '2024-12-31')].copy()

grouped = a_stock_2024.groupby(['ind_code', 'date'])

# 因子1：涨幅 — 行业内个股涨幅均值
factor_rise = grouped['pctChg'].mean().reset_index()
factor_rise.columns = ['ind_code', 'date', 'rise']

# 因子2：宽度 — 行业内上涨个股占比
factor_breadth = grouped.apply(lambda g: (g['pctChg'] > 0).mean()).reset_index()
factor_breadth.columns = ['ind_code', 'date', 'breadth']

# 因子3：量能 — 行业总成交额
factor_amount = grouped['amount'].sum().reset_index()
factor_amount.columns = ['ind_code', 'date', 'amount']

# 因子4：拥挤度 — 行业内个股换手率均值
factor_crowd = grouped['turn'].mean().reset_index()
factor_crowd.columns = ['ind_code', 'date', 'crowd']

# 因子5：情绪 — Σ(涨停×连板倍率) - Σ(跌停数)
factor_emotion = grouped.apply(lambda g: 
    g.loc[g['is_limit_up'], 'limit_mult'].sum() - g['is_limit_down'].sum()
).reset_index()
factor_emotion.columns = ['ind_code', 'date', 'emotion']

# 合并因子
factors = factor_rise.merge(factor_breadth, on=['ind_code', 'date'])
factors = factors.merge(factor_amount, on=['ind_code', 'date'])
factors = factors.merge(factor_crowd, on=['ind_code', 'date'])
factors = factors.merge(factor_emotion, on=['ind_code', 'date'])

print(f"\n因子面板: {len(factors)} 行, {factors['ind_code'].nunique()} 行业, {factors['date'].nunique()} 天")

# ═══════════════════════════════════════════
# 6. 截面rank标准化
# ═══════════════════════════════════════════
factor_cols = ['rise', 'breadth', 'amount', 'crowd', 'emotion']
for col in factor_cols:
    factors[col + '_rank'] = factors.groupby('date')[col].rank(pct=True)

# ═══════════════════════════════════════════
# 7. 合并因子 + 未来收益
# ═══════════════════════════════════════════
panel = factors.merge(
    ind_daily_ret_2024[['ind_code', 'date', 'future_ret_5', 'future_ret_10']], 
    on=['ind_code', 'date'], 
    how='inner'
)
print(f"合并面板: {len(panel)} 行, {panel['ind_code'].nunique()} 行业, {panel['date'].nunique()} 天")

# ═══════════════════════════════════════════
# 8. IC分析
# ═══════════════════════════════════════════
print("\n" + "="*60)
print("IC分析：各因子(rank标准化)与未来收益的Spearman Rank IC")
print("="*60)

def calc_ic(panel, factor_col, target_col):
    daily_ics = []
    for date, group in panel.groupby('date'):
        valid = group[[factor_col, target_col]].dropna()
        if len(valid) < 10:
            continue
        ic, _ = stats.spearmanr(valid[factor_col], valid[target_col])
        daily_ics.append({'date': date, 'ic': ic})
    
    if len(daily_ics) == 0:
        return {'mean_ic': 0, 'std_ic': 0, 'ic_ir': 0, 'positive_pct': 0, 'n_days': 0}
    
    ic_df = pd.DataFrame(daily_ics)
    mean_ic = ic_df['ic'].mean()
    std_ic = ic_df['ic'].std()
    ic_ir = mean_ic / std_ic if std_ic > 0 else 0
    positive_pct = (ic_df['ic'] > 0).mean()
    
    return {'mean_ic': mean_ic, 'std_ic': std_ic, 'ic_ir': ic_ir, 
            'positive_pct': positive_pct, 'n_days': len(ic_df)}

results = {}
for target in ['future_ret_5', 'future_ret_10']:
    print(f"\n--- 预测目标: {target} ---")
    print(f"{'因子':>10s} | {'Mean IC':>8s} | {'Std IC':>8s} | {'IC/IR':>8s} | {'IC>0%':>8s} | {'天数':>6s}")
    print("-" * 70)
    
    for fc in factor_cols:
        r = calc_ic(panel, fc + '_rank', target)
        results[(fc, target)] = r
        print(f"{fc:>10s} | {r['mean_ic']:>8.4f} | {r['std_ic']:>8.4f} | {r['ic_ir']:>8.3f} | {r['positive_pct']:>8.1%} | {r['n_days']:>6d}")

# ═══════════════════════════════════════════
# 9. IC_IR加权权重
# ═══════════════════════════════════════════
print("\n" + "="*60)
print("权重方案对比")
print("="*60)

suggested_weights = {'rise': 0.15, 'breadth': 0.25, 'amount': 0.25, 'crowd': 0.15, 'emotion': 0.20}

def compute_ic_weights(results, target):
    weights = {}
    total = 0
    for fc in factor_cols:
        ir = abs(results[(fc, target)]['ic_ir'])
        weights[fc] = ir
        total += ir
    if total > 0:
        for fc in weights:
            weights[fc] /= total
    return weights

w5 = compute_ic_weights(results, 'future_ret_5')
w10 = compute_ic_weights(results, 'future_ret_10')

print(f"\n{'因子':>10s} | {'IC加权(5日)':>12s} | {'IC加权(10日)':>12s} | {'主观建议':>12s} | {'等权':>12s}")
print("-" * 65)
for fc in factor_cols:
    print(f"{fc:>10s} | {w5[fc]:>12.1%} | {w10[fc]:>12.1%} | {suggested_weights[fc]:>12.1%} | {0.2:>12.1%}")

# ═══════════════════════════════════════════
# 10. 分组回测验证
# ═══════════════════════════════════════════
print("\n" + "="*60)
print("分组回测：IC加权 vs 等权 vs 主观建议 → 5组收益对比")
print("="*60)

# 三种合成分
panel['composite_ic5'] = sum(panel[fc + '_rank'] * w5[fc] for fc in factor_cols)
panel['composite_ic10'] = sum(panel[fc + '_rank'] * w10[fc] for fc in factor_cols)
panel['composite_eq'] = sum(panel[fc + '_rank'] for fc in factor_cols) / 5
panel['composite_suggest'] = sum(panel[fc + '_rank'] * suggested_weights[fc] for fc in factor_cols)

composite_names = {
    'composite_ic5': 'IC加权(基于5日IC)',
    'composite_ic10': 'IC加权(基于10日IC)',
    'composite_eq': '等权',
    'composite_suggest': '主观建议(15/25/25/15/20)'
}

for cname, clabel in composite_names.items():
    print(f"\n--- {clabel} ---")
    panel['group'] = panel.groupby('date')[cname].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates='drop') + 1
    )
    
    for target, tname in [('future_ret_5', '未来5日'), ('future_ret_10', '未来10日')]:
        group_stats = panel.groupby('group')[target].mean()
        print(f"  {tname}收益(%):")
        for g in sorted(group_stats.index):
            print(f"    G{int(g)}: {group_stats[g]:>7.2f}%")
        if 5 in group_stats.index and 1 in group_stats.index:
            spread = group_stats[5] - group_stats[1]
            print(f"    Spread(G5-G1): {spread:.2f}%")

print("\n=== 分析完成 ===")
