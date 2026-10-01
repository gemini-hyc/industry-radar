#!/usr/bin/env python3
"""
追查小克"日均1.1信号"的来源。
分别用两种宽度定义跑信号扫描，对比信号数量差异。
"""
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path('/opt/data/quant-data')
MARKET_DAILY = DATA_DIR / 'market' / 'daily'
IND_DIR = DATA_DIR / 'industry'

# 加载数据
print("加载行情数据...")
members_df = pd.read_parquet(IND_DIR / 'industry_members.parquet')
ind_members = {ind_code: grp['stock_code'].tolist() for ind_code, grp in members_df.groupby('ind_code')}

files = sorted(MARKET_DAILY.glob('*.parquet'))
files = [f for f in files if f.stem[:4] >= '2020' and f.stem[:7] <= '202606']
dfs = [pd.read_parquet(f) for f in files]
df = pd.concat(dfs, ignore_index=True)

# 修复成交额
if 'amount' in df.columns and 'volume' in df.columns:
    mask = (df['volume'] > 0) & (df['amount'] / df['volume'] < 0.1)
    if mask.sum() > 0:
        df.loc[mask, 'amount'] = df.loc[mask, 'amount'] * 10000

valid_prefixes = ["600","601","603","605","688","689","000","001","002","003","300","301","302","920"]
code_prefix = df['code'].str.split('.').str[1].str[:3]
df = df[code_prefix.isin(valid_prefixes)]
df['date'] = pd.to_datetime(df['date'])

print(f"  行情: {len(df)} 行, {df['code'].nunique()} 只")

# ═══════════════════════════════════════════
# 两种宽度计算
# ═══════════════════════════════════════════

def compute_breadth_ma20(market_df, ind_members):
    """MA20占比宽度"""
    records = []
    for ind_code, stock_codes in ind_members.items():
        con = market_df[market_df['code'].isin(stock_codes)].copy()
        if con.empty:
            continue
        con = con.sort_values(['code', 'date'])
        con['ma20'] = con.groupby('code')['close'].transform(
            lambda x: x.rolling(20, min_periods=5).mean()
        )
        con['above_ma20'] = (con['close'] > con['ma20']).astype(int)
        daily = con.groupby('date').agg(
            avg_pct=('pctChg', 'mean'),
            total_amount=('amount', 'sum'),
            breadth=('above_ma20', 'mean'),
        ).reset_index()
        daily['ind_code'] = ind_code
        records.append(daily)
    result = pd.concat(records, ignore_index=True)
    result = result.sort_values(['ind_code', 'date']).reset_index(drop=True)
    return result

def compute_breadth_rise(market_df, ind_members):
    """涨跌占比宽度"""
    records = []
    for ind_code, stock_codes in ind_members.items():
        con = market_df[market_df['code'].isin(stock_codes)].copy()
        if con.empty:
            continue
        con['is_rising'] = (con['pctChg'] > 0).astype(int)
        daily = con.groupby('date').agg(
            avg_pct=('pctChg', 'mean'),
            total_amount=('amount', 'sum'),
            breadth=('is_rising', 'mean'),
        ).reset_index()
        daily['ind_code'] = ind_code
        records.append(daily)
    result = pd.concat(records, ignore_index=True)
    result = result.sort_values(['ind_code', 'date']).reset_index(drop=True)
    return result

# ═══════════════════════════════════════════
# 信号扫描（不去重，看原始触发次数）
# ═══════════════════════════════════════════

def scan_signals(ind_daily, label, dedup_days=0):
    """
    扫描宽度反转信号。
    dedup_days=0: 不去重，看原始触发次数
    dedup_days=30: 30天去重
    """
    signals = []
    last_signal_date = {}
    
    for ind_code in ind_daily['ind_code'].unique():
        hist = ind_daily[ind_daily['ind_code'] == ind_code].copy()
        hist = hist.sort_values('date').reset_index(drop=True)
        
        if len(hist) < 25:
            continue
        
        in_expansion = False
        
        for i in range(20, len(hist)):
            row = hist.iloc[i]
            bw_today = row['breadth']
            trade_date = row['date']
            
            if in_expansion:
                if bw_today <= hist.iloc[i-1]['breadth']:
                    in_expansion = False
                else:
                    continue
            
            # C1: 前20日均宽 ≤ 30%
            bw20 = hist.iloc[i-20:i]['breadth'].mean()
            if bw20 > 0.30:
                continue
            
            # C2: 连续扩张 ≥ 3天
            exp_days = 0
            for j in range(i, max(i-10, 19), -1):
                if j > 0 and hist.iloc[j]['breadth'] > hist.iloc[j-1]['breadth']:
                    exp_days += 1
                else:
                    break
            
            if exp_days < 3:
                continue
            
            # 评分
            score = 1
            bw_pct = bw20 * 100
            if bw_pct < 10: score += 3
            elif bw_pct < 20: score += 2
            else: score += 1
            
            # D1占比
            d_vals = []
            for k in range(exp_days):
                idx = i - k
                if idx > 0:
                    delta = hist.iloc[idx]['breadth'] - hist.iloc[idx-1]['breadth']
                    d_vals.append(delta)
            d_vals = d_vals[:3]
            d_sum = sum(d_vals)
            d1_ratio = d_vals[0] / d_sum if d_sum > 0 else 0
            if d1_ratio >= 0.60: score += 3
            elif d1_ratio >= 0.50: score += 2
            elif d1_ratio >= 0.40: score += 1
            
            # 三日量比
            exp_start_idx = i - exp_days + 1
            if exp_start_idx >= 20:
                exp_amt = hist.iloc[exp_start_idx:exp_start_idx+3]['total_amount'].mean()
                pre_amt = hist.iloc[exp_start_idx-20:exp_start_idx]['total_amount'].mean()
                vol_ratio = exp_amt / pre_amt if pre_amt > 0 else 1.0
                if vol_ratio >= 1.3: score += 3
                elif vol_ratio >= 1.1: score += 2
                elif vol_ratio >= 1.0: score += 1
            else:
                vol_ratio = 0
            
            if score < 3:
                in_expansion = True
                continue
            
            # 去重
            if dedup_days > 0:
                if ind_code in last_signal_date:
                    days_since = (pd.Timestamp(trade_date) - pd.Timestamp(last_signal_date[ind_code])).days
                    if days_since < dedup_days:
                        in_expansion = True
                        continue
                last_signal_date[ind_code] = trade_date
            
            signals.append({
                'ind_code': ind_code,
                'date': trade_date,
                'score': score,
                'bw20_avg': bw20,
                'exp_days': exp_days,
            })
            in_expansion = True
    
    return pd.DataFrame(signals)


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

print("\n" + "=" * 70)
print("MA20占比宽度")
print("=" * 70)
ind_ma20 = compute_breadth_ma20(df, ind_members)
print(f"  行业日频: {len(ind_ma20)} 行")

for dedup in [0, 30]:
    label = f"{'30天去重' if dedup == 30 else '不去重'}"
    sig = scan_signals(ind_ma20, "MA20", dedup_days=dedup)
    if len(sig) > 0:
        dates = pd.to_datetime(sig['date'])
        n_years = (dates.max() - dates.min()).days / 365.25 + 1
        trading_days = len(dates.dt.date.unique())
        for thresh in [3, 5, 7]:
            n = len(sig[sig['score'] >= thresh])
            daily_avg = n / trading_days if trading_days > 0 else 0
            print(f"  [{label}] ≥{thresh}分: {n}条, 日均{daily_avg:.2f}, "
                  f"覆盖{trading_days}交易日")
    else:
        print(f"  [{label}] 无信号")

# 看MA20下冷区行业数
cold_ma20 = ind_ma20.copy()
cold_ma20['bw20_rolling'] = cold_ma20.groupby('ind_code')['breadth'].transform(
    lambda x: x.rolling(20, min_periods=15).mean()
)
cold_daily = cold_ma20[cold_ma20['bw20_rolling'] <= 0.30]
print(f"\n  MA20冷区(前20日均宽≤30%)行业×日期: {len(cold_daily)}")
print(f"  日均冷区行业数: {cold_daily.groupby('date').ngroups and len(cold_daily)/cold_daily['date'].nunique():.1f}")

# 看MA20下连续扩张≥3天的触发次数（不要求冷区）
print("\n  --- 连续扩张≥3天触发统计（不要求冷区）---")
exp_count = 0
for ind_code in ind_ma20['ind_code'].unique():
    hist = ind_ma20[ind_ma20['ind_code'] == ind_code].sort_values('date').reset_index(drop=True)
    for i in range(1, len(hist)):
        exp = 0
        for j in range(i, max(i-10, 0), -1):
            if j > 0 and hist.iloc[j]['breadth'] > hist.iloc[j-1]['breadth']:
                exp += 1
            else:
                break
        if exp >= 3:
            exp_count += 1
total_rows = len(ind_ma20)
print(f"  连续扩张≥3天的行业×日期: {exp_count} / {total_rows} = {exp_count/total_rows*100:.1f}%")


print("\n" + "=" * 70)
print("涨跌占比宽度")
print("=" * 70)
ind_rise = compute_breadth_rise(df, ind_members)
print(f"  行业日频: {len(ind_rise)} 行")

for dedup in [0, 30]:
    label = f"{'30天去重' if dedup == 30 else '不去重'}"
    sig = scan_signals(ind_rise, "涨跌占比", dedup_days=dedup)
    if len(sig) > 0:
        dates = pd.to_datetime(sig['date'])
        trading_days = len(dates.dt.date.unique())
        for thresh in [3, 5, 7]:
            n = len(sig[sig['score'] >= thresh])
            daily_avg = n / trading_days if trading_days > 0 else 0
            print(f"  [{label}] ≥{thresh}分: {n}条, 日均{daily_avg:.2f}, "
                  f"覆盖{trading_days}交易日")
    else:
        print(f"  [{label}] 无信号")

# 涨跌占比下冷区行业数
cold_rise = ind_rise.copy()
cold_rise['bw20_rolling'] = cold_rise.groupby('ind_code')['breadth'].transform(
    lambda x: x.rolling(20, min_periods=15).mean()
)
cold_daily_rise = cold_rise[cold_rise['bw20_rolling'] <= 0.30]
print(f"\n  涨跌占比冷区(前20日均宽≤30%)行业×日期: {len(cold_daily_rise)}")
print(f"  日均冷区行业数: {len(cold_daily_rise)/cold_daily_rise['date'].nunique():.1f}")

# 涨跌占比下连续扩张≥3天的触发次数
print("\n  --- 连续扩张≥3天触发统计（不要求冷区）---")
exp_count_rise = 0
for ind_code in ind_rise['ind_code'].unique():
    hist = ind_rise[ind_rise['ind_code'] == ind_code].sort_values('date').reset_index(drop=True)
    for i in range(1, len(hist)):
        exp = 0
        for j in range(i, max(i-10, 0), -1):
            if j > 0 and hist.iloc[j]['breadth'] > hist.iloc[j-1]['breadth']:
                exp += 1
            else:
                break
        if exp >= 3:
            exp_count_rise += 1
print(f"  连续扩张≥3天的行业×日期: {exp_count_rise} / {total_rows} = {exp_count_rise/total_rows*100:.1f}%")

# ═══════════════════════════════════════════
# 关键对比：两种宽度下连续扩张的难度
# ═══════════════════════════════════════════
print("\n" + "=" * 70)
print("关键对比：宽度日间变化特征")
print("=" * 70)

# 取几个行业看宽度日间变化
sample_inds = list(ind_members.keys())[:5]
for ind_code in sample_inds:
    h_ma20 = ind_ma20[ind_ma20['ind_code'] == ind_code].sort_values('date').tail(100)
    h_rise = ind_rise[ind_rise['ind_code'] == ind_code].sort_values('date').tail(100)
    
    # 日间变化
    diff_ma20 = h_ma20['breadth'].diff().dropna()
    diff_rise = h_rise['breadth'].diff().dropna()
    
    # 连续正变化
    def max_consecutive_pos(series):
        max_len = cur = 0
        for v in series:
            if v > 0:
                cur += 1
                max_len = max(max_len, cur)
            else:
                cur = 0
        return max_len
    
    print(f"  {ind_code}: MA20宽度日变化std={diff_ma20.std():.4f}, "
          f"最大连续正变化={max_consecutive_pos(diff_ma20)}; "
          f"涨跌占比日变化std={diff_rise.std():.4f}, "
          f"最大连续正变化={max_consecutive_pos(diff_rise)}")

# 全行业统计
print("\n  全行业统计（最近100天）:")
ma20_max_cons = []
rise_max_cons = []
ma20_stds = []
rise_stds = []

for ind_code in ind_members.keys():
    h_ma20 = ind_ma20[ind_ma20['ind_code'] == ind_code].sort_values('date').tail(100)
    h_rise = ind_rise[ind_rise['ind_code'] == ind_code].sort_values('date').tail(100)
    
    diff_ma20 = h_ma20['breadth'].diff().dropna()
    diff_rise = h_rise['breadth'].diff().dropna()
    
    if len(diff_ma20) > 0:
        ma20_max_cons.append(max_consecutive_pos(diff_ma20))
        ma20_stds.append(diff_ma20.std())
    if len(diff_rise) > 0:
        rise_max_cons.append(max_consecutive_pos(diff_rise))
        rise_stds.append(diff_rise.std())

print(f"  MA20占比: 日变化std均值={np.mean(ma20_stds):.4f}, "
      f"最大连续正变化均值={np.mean(ma20_max_cons):.1f}, "
      f"≥3天占比={np.mean([x>=3 for x in ma20_max_cons])*100:.1f}%")
print(f"  涨跌占比: 日变化std均值={np.mean(rise_stds):.4f}, "
      f"最大连续正变化均值={np.mean(rise_max_cons):.1f}, "
      f"≥3天占比={np.mean([x>=3 for x in rise_max_cons])*100:.1f}%")

print("\n" + "=" * 70)
print("结论：涨跌占比日间波动大→连续扩张≥3天极难→信号极少")
print("=" * 70)
