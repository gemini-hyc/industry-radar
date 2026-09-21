#!/usr/bin/env python3
"""
行业因子日频增量更新
===================
每天运行一次，追加最新交易日的 ind_crowd_turnover 因子值。
运行时机: DE 完成后 (16:16 CST 之后)

输入:
  - data/market/daily/ (最新日线)
  - data/industry/industry_members.parquet (富途申万二级行业映射)

输出:
  - 更新 data/factors/ind_crowd_turnover_daily.parquet

用法: PYTHONPATH=/opt/data/quant python scripts/update_industry_factor.py
"""

import sys, os, glob
sys.path.insert(0, "/opt/data/quant")

import pandas as pd
import numpy as np

DAILY_DIR = "/opt/data/quant-data/market/daily"
FACTOR_DIR = "/opt/data/quant-data/factors"
INDUSTRY_DIR = "/opt/data/quant-data/industry"


def main():
    # 1. 找到最新的日线文件
    files = sorted(glob.glob(f"{DAILY_DIR}/*.parquet"))
    if not files:
        print("❌ 无日线文件")
        return 1

    latest_file = files[-1]
    trade_date_str = os.path.basename(latest_file)[:10]
    trade_date = pd.Timestamp(trade_date_str)
    print(f"最新交易日: {trade_date.date()}")

    # 2. 检查是否已更新
    factor_path = os.path.join(FACTOR_DIR, "ind_crowd_turnover_daily.parquet")
    if os.path.exists(factor_path):
        existing = pd.read_parquet(factor_path)
        existing['date'] = pd.to_datetime(existing['date'])
        existing_dates = set(existing['date'].dt.date)
        if trade_date.date() in existing_dates:
            print(f"✅ {trade_date.date()} 已存在，跳过")
            return 0
        print(f"  已有 {existing['date'].nunique()} 天数据，增量模式")
        full_compute = False
        compute_files = files[-60:]
    else:
        print("  ⚠️ 因子文件不存在，将全量计算...")
        existing = None
        full_compute = True
        compute_files = files

    # 3. 加载日线数据
    print(f"  加载 {len(compute_files)} 个日线文件...")
    frames = []
    for f in compute_files:
        df = pd.read_parquet(f)
        frames.append(df[['date', 'code', 'turn']])
    data = pd.concat(frames, ignore_index=True)
    data['date'] = pd.to_datetime(data['date'])

    # 过滤指数
    data = data[~data['code'].str.match(r'^(sh\.000|sz\.399)')]
    data = data.sort_values(['code', 'date'])

    # 4. 加载行业映射 — 富途申万二级行业分类 (131个)
    members_path = os.path.join(INDUSTRY_DIR, "industry_members.parquet")
    if not os.path.exists(members_path):
        print(f"❌ 行业映射文件不存在: {members_path}")
        return 1
    stock_ind = pd.read_parquet(members_path)
    stock_ind = stock_ind.rename(columns={'stock_code': 'code', 'ind_code': 'industry'})
    stock_ind = stock_ind[['code', 'industry']].dropna()
    ind_counts = stock_ind['industry'].value_counts()
    valid_inds = ind_counts[ind_counts >= 15].index.tolist()
    stock_ind = stock_ind[stock_ind['industry'].isin(valid_inds)]
    print(f"  行业映射: {len(valid_inds)} 个有效行业, {len(stock_ind)} 只股票")

    # 合并
    data = data.merge(stock_ind[['code', 'industry']], on='code', how='inner')

    # 5. 计算 ind_crowd_turnover
    # 行业日换手率
    print("  计算行业拥挤度因子...")
    ind_turn = data.groupby(['date', 'industry'])['turn'].mean().reset_index()
    ind_turn.columns = ['date', 'industry', 'ind_turn']
    ind_turn = ind_turn.sort_values(['industry', 'date'])

    # 短期/长期均值
    ind_turn['turn_20'] = ind_turn.groupby('industry')['ind_turn'].transform(
        lambda x: x.rolling(20, min_periods=10).mean())
    ind_turn['turn_60'] = ind_turn.groupby('industry')['ind_turn'].transform(
        lambda x: x.rolling(60, min_periods=30).mean())
    ind_turn['ind_crowd_turnover'] = ind_turn['turn_20'] / ind_turn['turn_60']

    # 6. 下发到个股 — 全量模式输出所有日期，增量模式只输出最新日
    from scipy import stats

    if full_compute:
        # 全量：逐日计算百分位
        all_days = sorted(ind_turn['date'].unique())
        print(f"  全量计算: {len(all_days)} 天数据")
        result_frames = []
        for i, d in enumerate(all_days):
            day_ind = ind_turn[ind_turn['date'] == d]
            day_stock = day_ind.merge(stock_ind[['code', 'industry']], on='industry', how='inner')
            day_stock = day_stock[['date', 'code', 'ind_crowd_turnover']].dropna()
            valid = day_stock['ind_crowd_turnover'].notna()
            pct = np.full(len(day_stock), np.nan)
            if valid.sum() > 50:
                pct[valid] = stats.rankdata(day_stock.loc[valid, 'ind_crowd_turnover']) / valid.sum()
            day_stock['value'] = pct
            result_frames.append(day_stock[['date', 'code', 'value']])
            if (i + 1) % 50 == 0:
                print(f"    进度: {i + 1}/{len(all_days)}")
        updated = pd.concat(result_frames, ignore_index=True)
        os.makedirs(FACTOR_DIR, exist_ok=True)
        updated.to_parquet(factor_path, index=False)
        print(f"✅ 全量写入: {len(updated)} 行, {updated['date'].nunique()} 天")
    else:
        # 增量：只处理最新日
        latest = ind_turn[ind_turn['date'] == ind_turn['date'].max()]
        stock_factor = latest.merge(stock_ind[['code', 'industry']], on='industry', how='inner')
        stock_factor = stock_factor[['date', 'code', 'ind_crowd_turnover']].dropna()

        # 百分位化
        valid = stock_factor['ind_crowd_turnover'].notna()
        pct = np.full(len(stock_factor), np.nan)
        if valid.sum() > 50:
            pct[valid] = stats.rankdata(stock_factor.loc[valid, 'ind_crowd_turnover']) / valid.sum()
        stock_factor['value'] = pct  # 0-1 percentile

        print(f"  新增: {len(stock_factor)} 只股票, {stock_factor['code'].nunique()} 只有效")

        # 7. 追加到已有文件
        new_row = stock_factor[['date', 'code', 'value']].copy()
        updated = pd.concat([existing, new_row], ignore_index=True)
        updated.to_parquet(factor_path, index=False)
        print(f"✅ 已更新: {len(updated)} 行, {updated['date'].nunique()} 天")


if __name__ == "__main__":
    sys.exit(main() or 0)
