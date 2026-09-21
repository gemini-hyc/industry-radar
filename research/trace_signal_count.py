#!/usr/bin/env python3
"""追查小克"日均1.1信号"来源——只数信号，不跑回测"""
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path('/opt/data/quant-data')
MARKET_DAILY = DATA_DIR / 'market' / 'daily'
IND_DIR = DATA_DIR / 'industry'

# 加载
members_df = pd.read_parquet(IND_DIR / 'industry_members.parquet')
ind_members = {ind_code: grp['stock_code'].tolist() for ind_code, grp in members_df.groupby('ind_code')}

files = sorted(MARKET_DAILY.glob('*.parquet'))
files = [f for f in files if f.stem[:4] >= '2020' and f.stem[:7] <= '202606']
dfs = [pd.read_parquet(f, columns=['code','date','pctChg','amount','volume']) for f in files]
df = pd.concat(dfs, ignore_index=True)

# 修复成交额
if 'volume' in df.columns:
    mask = (df['volume'] > 0) & (df['amount'] / df['volume'] < 0.1)
    if mask.sum() > 0:
        df.loc[mask, 'amount'] = df.loc[mask, 'amount'] * 10000

valid_prefixes = ["600","601","603","605","688","689","000","001","002","003","300","301","302","920"]
code_prefix = df['code'].str.split('.').str[1].str[:3]
df = df[code_prefix.isin(valid_prefixes)]
df['is_rising'] = (df['pctChg'] > 0).astype(int)
df['date'] = pd.to_datetime(df['date'])

print(f"行情: {len(df)} 行, {df['code'].nunique()} 只")

# 涨跌占比宽度（与小克定义一致）
records = []
for ind_code, stock_codes in ind_members.items():
    con = df[df['code'].isin(stock_codes)]
    if con.empty:
        continue
    daily = con.groupby('date').agg(
        avg_pct=('pctChg', 'mean'),
        total_amount=('amount', 'sum'),
        breadth=('is_rising', 'mean'),
    ).reset_index()
    daily['ind_code'] = ind_code
    records.append(daily)

ind_daily = pd.concat(records, ignore_index=True)
ind_daily = ind_daily.sort_values(['ind_code', 'date']).reset_index(drop=True)

# 前20日滚动均宽
ind_daily['bw20_rolling'] = ind_daily.groupby('ind_code')['breadth'].transform(
    lambda x: x.rolling(20, min_periods=15).mean()
)

# 全样本统计
total_days = ind_daily['date'].nunique()
total_years = (ind_daily['date'].max() - ind_daily['date'].min()).days / 365.25 + 1
trading_days_per_year = total_days / total_years
print(f"\n总交易日: {total_days}, 约{total_years:.1f}年, 年均{trading_days_per_year:.0f}交易日")

# ═══════════════════════════════════════════
# 问题1：冷区行业×日期有多少？
# ═══════════════════════════════════════════
cold_all = ind_daily[ind_daily['bw20_rolling'] <= 0.30]
print(f"\n=== 冷区(前20日均宽≤30%) ===")
print(f"  行业×日期总数: {len(cold_all)}")
print(f"  日均冷区行业数: {len(cold_all)/total_days:.1f}")
print(f"  冷区行业占比: {len(cold_all)/len(ind_daily)*100:.1f}%")

# ═══════════════════════════════════════════
# 问题2：连续扩张≥3天的触发情况
# ═══════════════════════════════════════════
print(f"\n=== 连续扩张≥3天（全行业，不限冷区）===")
exp3_count = 0
for ind_code in ind_daily['ind_code'].unique():
    hist = ind_daily[ind_daily['ind_code'] == ind_code].sort_values('date').reset_index(drop=True)
    for i in range(1, len(hist)):
        exp = 0
        j = i
        while j > 0 and hist.iloc[j]['breadth'] > hist.iloc[j-1]['breadth']:
            exp += 1
            j -= 1
        if exp >= 3:
            exp3_count += 1

print(f"  行业×日期中连续扩张≥3天: {exp3_count}")
print(f"  日均: {exp3_count/total_days:.2f}")

# ═══════════════════════════════════════════
# 问题3：冷区+连续扩张≥3天的交叉
# ═══════════════════════════════════════════
print(f"\n=== 冷区 AND 连续扩张≥3天 ===")

# 先算每行是否连续扩张≥3天
ind_daily['is_exp3'] = False
for ind_code in ind_daily['ind_code'].unique():
    mask = ind_daily['ind_code'] == ind_code
    hist = ind_daily.loc[mask].sort_values('date').reset_index(drop=True)
    exp3_indices = set()
    for i in range(1, len(hist)):
        exp = 0
        j = i
        while j > 0 and hist.iloc[j]['breadth'] > hist.iloc[j-1]['breadth']:
            exp += 1
            j -= 1
        if exp >= 3:
            exp3_indices.add(i)
    
    # 映射回原DataFrame
    orig_indices = ind_daily.loc[mask].sort_values('date').index
    for idx in exp3_indices:
        if idx < len(orig_indices):
            ind_daily.loc[orig_indices[idx], 'is_exp3'] = True

cold_exp3 = ind_daily[(ind_daily['bw20_rolling'] <= 0.30) & (ind_daily['is_exp3'])]
print(f"  交叉样本: {len(cold_exp3)} 行业×日期")
print(f"  日均: {len(cold_exp3)/total_days:.3f}")

# ═══════════════════════════════════════════
# 问题4：如果小克放宽条件，只看"冷区"不要求"连续扩张"
# ═══════════════════════════════════════════
print(f"\n=== 如果只看冷区(≤30%)行业数量 ===")
print(f"  小克声称日均1.1信号(≥3分)")
print(f"  实际冷区日均行业数: {len(cold_all)/total_days:.1f}")
print(f"  → 小克可能把'冷区行业数'当成了'信号数'？")

# ═══════════════════════════════════════════
# 问题5：涨跌占比宽度的日间变化特征
# ═══════════════════════════════════════════
print(f"\n=== 涨跌占比宽度的日间波动 ===")
# 取5个行业看breadth日间变化
sample_inds = ['SH.LIST0001', 'SH.LIST0002', 'SH.LIST0003', 'SH.LIST0004', 'SH.LIST0005']
for ind_code in sample_inds:
    h = ind_daily[ind_daily['ind_code'] == ind_code].sort_values('date').tail(100)
    if len(h) < 10:
        continue
    diff = h['breadth'].diff().dropna()
    
    # 连续正变化天数统计
    consec_pos = []
    cur = 0
    for v in diff:
        if v > 0:
            cur += 1
        else:
            if cur > 0:
                consec_pos.append(cur)
            cur = 0
    if cur > 0:
        consec_pos.append(cur)
    
    max_consec = max(consec_pos) if consec_pos else 0
    pct_ge3 = len([x for x in consec_pos if x >= 3]) / len(consec_pos) * 100 if consec_pos else 0
    
    print(f"  {ind_code}: std={diff.std():.4f}, 最大连续正={max_consec}, "
          f"连续正≥3出现{pct_ge3:.0f}%次")

# ═══════════════════════════════════════════
# 问题6：小克可能用了什么口径算出1.1？
# ═══════════════════════════════════════════
print(f"\n=== 小克1.1的可能来源 ===")

# 可能1：IC脚本的因子面板中breadth≤30%的行业数
# 小克IC脚本只跑2024年
ind_2024 = ind_daily[(ind_daily['date'] >= '2024-01-01') & (ind_daily['date'] <= '2024-12-31')]
cold_2024 = ind_2024[ind_2024['breadth'] <= 0.30]  # 注意：这里是当日breadth≤30%
days_2024 = ind_2024['date'].nunique()
print(f"  可能1: 2024年当日breadth≤30%行业数日均={len(cold_2024)/days_2024:.1f}")

# 可能2：IC脚本中breadth_rank≤某阈值（比如排名前20%）
low_rank_2024 = ind_2024.copy()
low_rank_2024['breadth_rank'] = low_rank_2024.groupby('date')['breadth'].rank(pct=True)
bottom20 = low_rank_2024[low_rank_2024['breadth_rank'] <= 0.20]
print(f"  可能2: 2024年breadth排名≤20%的行业日均={len(bottom20)/days_2024:.1f}")

# 可能3：前20日滚动均宽≤30%的行业数
bw20_cold_2024 = ind_2024[ind_2024['bw20_rolling'] <= 0.30]
bw20_cold_2024_valid = bw20_cold_2024[bw20_cold_2024['bw20_rolling'].notna()]
print(f"  可能3: 2024年前20日均宽≤30%的行业日均={len(bw20_cold_2024_valid)/days_2024:.1f}")

# 可能4：当日breadth≤30%的行业×日期（全样本）
cold_today = ind_daily[ind_daily['breadth'] <= 0.30]
print(f"  可能4: 全样本当日breadth≤30%的行业日均={len(cold_today)/total_days:.1f}")

# 可能5：小克IC脚本里的分组回测中G1(排名最低20%)行业数
# 131行业×20%≈26行业/天，不是1.1

print(f"\n=== 结论 ===")
print(f"小克声称日均1.1信号(≥3分)，0.4信号(≥5分)")
print(f"实际涨跌占比下冷区+连续扩张≥3天只有约0.01/天")
print(f"日均1.1最接近的口径是: ？？（看上面各口径哪个最接近1.1）")
