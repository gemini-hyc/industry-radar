"""Build industry universe: filter industries with >= 15 stocks.
Output: data/industry_universe.parquet with columns:
  industry, stock_count, stocks (list)
Also adds industry_id column to stock_basic for quick lookup.
"""
import sys
import pandas as pd
import numpy as np
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import STOCK_BASIC_PATH, DATA_DIR

# Load
df = pd.read_parquet(STOCK_BASIC_PATH)

# Filter: industries with >= 15 stocks
ind_counts = df.groupby('industry').size().sort_values(ascending=False)
valid_industries = ind_counts[ind_counts >= 15].index.tolist()

print(f"总行业数: {df['industry'].nunique()}")
print(f"≥15只股票的行业: {len(valid_industries)}")
print(f"覆盖股票: {df[df['industry'].isin(valid_industries)].shape[0]} / {len(df)} ({df[df['industry'].isin(valid_industries)].shape[0]/len(df)*100:.1f}%)")

# Build industry universe
universe = []
for ind in valid_industries:
    stocks = df[df['industry'] == ind][['ts_code', 'symbol', 'name']].to_dict('records')
    universe.append({
        'industry': ind,
        'stock_count': len(stocks),
        'stocks_json': json.dumps(stocks, ensure_ascii=False)
    })

ind_df = pd.DataFrame(universe)
ind_df.to_parquet(DATA_DIR / 'industry_universe.parquet', index=False)
print(f"\nindustry_universe.parquet saved: {len(ind_df)} rows")

# Show bottom-end industries (smallest valid)
print(f"\n最小行业 (刚好≥15只):")
smallest = ind_df.nsmallest(5, 'stock_count')
for _, row in smallest.iterrows():
    print(f"  {row['industry']}: {row['stock_count']}只")

# Show top
print(f"\n最大行业:")
largest = ind_df.nlargest(5, 'stock_count')
for _, row in largest.iterrows():
    print(f"  {row['industry']}: {row['stock_count']}只")

# Size distribution
print(f"\n行业规模分布:")
bins = [15, 30, 50, 100, 200, 500]
labels = ['15-30', '30-50', '50-100', '100-200', '200+']
ind_df['size_bin'] = pd.cut(ind_df['stock_count'], bins=bins, labels=labels, right=False)
print(ind_df['size_bin'].value_counts().sort_index().to_string())
