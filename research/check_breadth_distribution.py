#!/usr/bin/env python3
"""查看涨跌占比宽度的分布，判断冷区阈值是否合理"""
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path('/opt/data/quant-data')
MARKET_DAILY = DATA_DIR / 'market' / 'daily'
IND_DIR = DATA_DIR / 'industry'

# 加载行业成员（parquet格式）
members_df = pd.read_parquet(IND_DIR / 'industry_members.parquet')
ind_members = {}
for ind_code, grp in members_df.groupby('ind_code'):
    ind_members[ind_code] = grp['stock_code'].tolist()

print(f"行业数: {len(ind_members)}, 成分股总数: {sum(len(v) for v in ind_members.values())}")

# 加载全样本行情
files = sorted(MARKET_DAILY.glob('*.parquet'))
files = [f for f in files if f.stem[:4] >= '2020' and f.stem[:7] <= '202606']
dfs = [pd.read_parquet(f) for f in files]
df = pd.concat(dfs, ignore_index=True)
df['is_rising'] = (df['pctChg'] > 0).astype(int)

print(f"行情数据: {len(df)} 行")

# 按行业×日计算breadth
records = []
for ind_code, stock_codes in ind_members.items():
    con = df[df['code'].isin(stock_codes)]
    if con.empty:
        continue
    daily_ind = con.groupby('date').agg(
        breadth=('is_rising', 'mean'),
    ).reset_index()
    daily_ind['ind_code'] = ind_code
    records.append(daily_ind)

result = pd.concat(records, ignore_index=True)
b = result['breadth']

print(f"\n{'='*60}")
print(f"涨跌占比宽度分布 (2020-01 ~ 2026-06)")
print(f"{'='*60}")
print(f"  样本数: {len(b)}")
print(f"  均值:   {b.mean():.3f}")
print(f"  中位数: {b.median():.3f}")
print(f"  标准差: {b.std():.3f}")
for q in [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]:
    print(f"  {q*100:.0f}%分位: {b.quantile(q):.3f}")

print(f"\n  ≤30%占比: {(b <= 0.30).mean()*100:.1f}%  ← 冷区阈值")
print(f"  ≤40%占比: {(b <= 0.40).mean()*100:.1f}%")
print(f"  ≤50%占比: {(b <= 0.50).mean()*100:.1f}%")

# 按年看
result['year'] = pd.to_datetime(result['date']).dt.year
print(f"\n按年统计冷区占比 (breadth ≤ 30%):")
for yr, grp in result.groupby('year'):
    cold_pct = (grp['breadth'] <= 0.30).mean() * 100
    print(f"  {yr}: 冷区占比 {cold_pct:.1f}%")

# 冷区行业×日期总数
cold = result[result['breadth'] <= 0.30]
print(f"\n冷区行业×日期总数: {len(cold)}")
print(f"  涉及行业数: {cold['ind_code'].nunique()}")
print(f"  涉及交易日数: {pd.to_datetime(cold['date']).nunique()}")
