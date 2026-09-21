"""
两连板涨停行业超额收益回测 v2
================================
核心命题: 行业中出现两连板(连续2天涨停)后，行业后续20日是否产生超额收益?

四组定义:
  A组(两连板行业) — 后20日行业等权涨跌幅(排除连板股)
  B组(首板行业)   — 后20日行业等权涨跌幅(排除首板股)
  C组(无涨停行业) — 后20日行业等权涨跌幅(基线)
  D组(连板股本身) — 后20日个股累计涨跌幅

超额定义(主人简化):
  A超额 = A收益 − C收益 (同日无涨停行业均值)
  B超额 = B收益 − C收益
  D超额 = D收益 − 同行业同行等权收益

涨跌停阈值分板块: 主板9.8%, 双创19.5%, 北交所29.5%, ST 4.8%
事件驱动法，不用截面IC
"""

import os, sys, time, warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

# ─── 配置 ───
DATA_ROOT = Path('/opt/data/quant-data')
MARKET_DIR = DATA_ROOT / 'market' / 'daily'
IND_DIR    = DATA_ROOT / 'industry'
OUTPUT_DIR = Path('/opt/data/quant/output')

HOLD_PERIOD = 20  # 后续观察天数

# t分布CDF近似(用于p值计算)
def _t_cdf_approx(t_val, df):
    """用正态分布近似t分布CDF(大样本时足够精确)"""
    # 当df>30时, t分布近似正态
    from math import erf, sqrt
    z = t_val / sqrt(1 + t_val**2/df)  # Fisher z变换近似
    return 0.5 * (1 + erf(z / sqrt(2)))

# 涨停阈值(含ST)
LIMIT_THRESHOLDS = {
    'main':    9.8,    # sh.600/601/603/605, sz.000/001/002/003
    'kcb_cyb': 19.5,   # sh.688/689, sz.300/301/302
    'bse':     29.5,   # sz.920
    'st':      4.8,    # 名称含ST
}

def get_limit_threshold(code, is_st):
    """根据代码前缀+ST标记返回涨停阈值"""
    if is_st:
        return LIMIT_THRESHOLDS['st']
    num = code.split('.')[1] if '.' in code else code[3:]
    if num.startswith('920'):
        return LIMIT_THRESHOLDS['bse']
    elif num.startswith(('300','301','302','688','689')):
        return LIMIT_THRESHOLDS['kcb_cyb']
    else:
        return LIMIT_THRESHOLDS['main']

def is_valid_a_share(code):
    """判断是否是A股(排除指数、ETF、B股)"""
    valid_prefixes = ['600','601','603','605','688','689',
                      '000','001','002','003','300','301','302','920']
    num = code.split('.')[1] if '.' in code else code[3:]
    return any(num.startswith(p) for p in valid_prefixes)


# ═══════════════════════════════════════════════════════════════════
# 步骤1: 加载行业映射 + ST标记
# ═══════════════════════════════════════════════════════════════════

print("=" * 60)
print("步骤1: 加载行业映射与ST标记")
print("=" * 60)

ind_members = pd.read_parquet(IND_DIR / 'industry_members.parquet')
ind_members['stock_code'] = ind_members['stock_code'].str.lower()
print(f"  行业成员: {ind_members['ind_code'].nunique()}行业, {ind_members['stock_code'].nunique()}股票")

ind_list = pd.read_parquet(IND_DIR / 'industry_list.parquet')
ind_name_map = dict(zip(ind_list['ts_code'], ind_list['name']))

# code → 行业映射
code_to_ind = dict(zip(ind_members['stock_code'], ind_members['ind_code']))

# 行业 → 成员股映射
ind_to_members = defaultdict(list)
for _, row in ind_members.iterrows():
    ind_to_members[row['ind_code']].append(row['stock_code'])

# ST标记
stock_basic = pd.read_parquet(DATA_ROOT / 'stock_basic.parquet')
stock_basic['code'] = stock_basic['ts_code'].apply(
    lambda x: f"{'sz' if x.endswith('.SZ') else 'sh'}.{x[:6]}"
)
stock_basic['is_st'] = stock_basic['name'].str.contains('ST', case=False, na=False)
st_codes = set(stock_basic[stock_basic['is_st']]['code'].tolist())
print(f"  ST股票数: {len(st_codes)}")


# ═══════════════════════════════════════════════════════════════════
# 步骤2: 逐日扫描 — 涨停识别 + 行业等权涨跌幅 + 宽度2
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤2: 逐日扫描(涨停+行业涨跌幅+宽度)")
print("=" * 60)

all_files = sorted([f for f in os.listdir(MARKET_DIR) if f.endswith('.parquet')])
print(f"  日线数据: {len(all_files)}天, {all_files[0]} ~ {all_files[-1]}")

t0 = time.time()

# 存储结构
limit_by_date     = {}   # {date: set(limit_up_codes)}
limit_days_counter = {}  # {code: 连续涨停天数} — 跨日累计

# 行业级日频数据
ind_daily_data = {}  # {date: {ind_code: {avg_pct, avg_pct_excl_limit, breadth2, stock_count, limit_codes}}}
market_avg_ret  = {}  # {date: 全市场行业等权均值}

# 连板事件
two_board_events  = []  # [(date, code, ind_code, limit_days)]
single_board_events = []  # [(date, code, ind_code)] — 仅首板且非连板日

for i, fname in enumerate(all_files):
    date = fname.replace('.parquet', '')
    fpath = os.path.join(MARKET_DIR, fname)
    
    df = pd.read_parquet(fpath)
    
    # 过滤A股 + 排除指数
    df = df[df['code'].apply(is_valid_a_share)].copy()
    df = df[~df['code'].str.match(r'^sh\.000')].copy()
    df = df[~df['code'].str.match(r'^sz\.399')].copy()
    
    if len(df) == 0:
        continue
    
    # ─── 涨停判定(排除ST股) ───
    # ST股涨停不算信号，但ST涨跌幅正常计入行业均值(真实市场数据)
    df['is_st'] = df['code'].isin(st_codes)
    df_non_st = df[~df['is_st']].copy()
    df_non_st['limit_threshold'] = df_non_st['code'].apply(lambda c: get_limit_threshold(c, False))
    df_non_st['is_limit'] = df_non_st['pctChg'] >= df_non_st['limit_threshold']
    
    current_limit_set = set(df_non_st[df_non_st['is_limit']]['code'].tolist())
    limit_by_date[date] = current_limit_set
    
    # ─── 连板计数(仅非ST) ───
    for code in df_non_st['code'].unique():
        if code in current_limit_set:
            limit_days_counter[code] = limit_days_counter.get(code, 0) + 1
        else:
            limit_days_counter[code] = 0
    
    # ─── 连板/首板事件 ───
    for code in current_limit_set:
        ind_code = code_to_ind.get(code)
        if ind_code is None:
            continue
        consecutive = limit_days_counter.get(code, 0)
        if consecutive >= 2:
            two_board_events.append((date, code, ind_code, consecutive))
        elif consecutive == 1:
            single_board_events.append((date, code, ind_code))
    
    # ─── 行业级统计(全量数据含ST，ST涨跌幅是真实市场) ───
    df['ind_code'] = df['code'].map(code_to_ind)
    df_with_ind = df[df['ind_code'].notna()].copy()
    # 标记涨停股(仅非ST涨停)
    df_with_ind['is_limit'] = df_with_ind['code'].isin(current_limit_set)
    
    date_ind_data = {}
    for ind_code, group in df_with_ind.groupby('ind_code'):
        # 全行业等权涨跌幅(含连板股)
        avg_pct = group['pctChg'].mean()
        # 排除涨停股后的等权涨跌幅
        non_limit_group = group[~group['is_limit']]
        avg_pct_excl = non_limit_group['pctChg'].mean() if len(non_limit_group) > 0 else avg_pct
        # 宽度2: 涨跌占比
        breadth2 = (group['pctChg'] > 0).mean()
        # 当日涨停股集合
        limit_in_ind = set(group[group['is_limit']]['code'].tolist())
        
        date_ind_data[ind_code] = {
            'avg_pct': avg_pct,
            'avg_pct_excl': avg_pct_excl,
            'breadth2': breadth2,
            'stock_count': len(group),
            'limit_codes': limit_in_ind,
        }
    
    ind_daily_data[date] = date_ind_data
    # 全市场行业等权均值
    if date_ind_data:
        market_avg_ret[date] = np.mean([v['avg_pct'] for v in date_ind_data.values()])
    
    # 进度
    if (i + 1) % 200 == 0 or i == len(all_files) - 1:
        elapsed = time.time() - t0
        print(f"  进度: {i+1}/{len(all_files)}, 连板事件{len(two_board_events)}条, 耗时{elapsed:.1f}s")

print(f"\n扫描完成! 耗时{time.time()-t0:.1f}s")
print(f"  两连板事件: {len(two_board_events)}条")
print(f"  首板事件: {len(single_board_events)}条")

# 交易日序列
trade_dates = sorted(ind_daily_data.keys())
date_idx = {d: i for i, d in enumerate(trade_dates)}
print(f"  有效交易日: {len(trade_dates)}")


# ═══════════════════════════════════════════════════════════════════
# 步骤3: 计算累计收益函数
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤3: 定义累计收益计算")
print("=" * 60)

def get_future_dates(event_date, n_days):
    """获取事件日后n个交易日日期列表"""
    idx = date_idx.get(event_date)
    if idx is None:
        return []
    end_idx = min(idx + n_days, len(trade_dates) - 1)
    return trade_dates[idx+1:end_idx+1]

def get_past_dates(event_date, n_days):
    """获取事件日前n个交易日日期列表"""
    idx = date_idx.get(event_date)
    if idx is None or idx < n_days:
        return []
    return trade_dates[idx-n_days:idx]

def calc_cumulative_ind_ret(ind_code, start_date, n_days, field='avg_pct_excl'):
    """计算行业后n日累计等权涨跌幅(排除涨停股版)"""
    future = get_future_dates(start_date, n_days)
    if not future:
        return None
    rets = []
    for d in future:
        d_data = ind_daily_data.get(d, {}).get(ind_code)
        if d_data is not None:
            rets.append(d_data[field])
    if len(rets) < n_days * 0.8:
        return None
    return sum(rets)  # pctChg已是百分数，直接累加

def calc_cumulative_market_ret(start_date, n_days):
    """计算全市场行业等权均值后n日累计涨跌幅"""
    future = get_future_dates(start_date, n_days)
    if not future:
        return None
    rets = []
    for d in future:
        r = market_avg_ret.get(d)
        if r is not None:
            rets.append(r)
    if len(rets) < n_days * 0.8:
        return None
    return sum(rets)

def calc_cumulative_c_group_ret(start_date, n_days):
    """计算C组基线: 同日无涨停行业后n日等权均值"""
    future = get_future_dates(start_date, n_days)
    if not future:
        return None
    
    # 信号日有涨停的行业
    limit_inds = set()
    for code in limit_by_date.get(start_date, set()):
        ind = code_to_ind.get(code)
        if ind:
            limit_inds.add(ind)
    
    rets = []
    for d in future:
        d_data = ind_daily_data.get(d, {})
        # 只取当天无涨停的行业
        no_limit_inds = [ind for ind in d_data if ind not in limit_inds]
        # 但更精确的做法: 用信号日的无涨停行业列表，追踪它们后续20日的涨跌幅
        # 这里简化: 每天取所有无涨停行业等权均值
        if no_limit_inds:
            avg = np.mean([d_data[ind]['avg_pct_excl'] for ind in no_limit_inds])
            rets.append(avg)
    
    if len(rets) < n_days * 0.8:
        return None
    return sum(rets)


# ═══════════════════════════════════════════════════════════════════
# 步骤4: 四组回测
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤4: 四组回测计算")
print("=" * 60)

# ─── 去重事件表 ───
a_raw = pd.DataFrame(two_board_events, columns=['date','code','ind_code','limit_days'])
# 同日同行业只算一次事件，但记录连板股数量
a_count = a_raw.groupby(['date','ind_code']).size().reset_index(name='board_count')
a_max_days = a_raw.groupby(['date','ind_code'])['limit_days'].max().reset_index(name='max_limit_days')
a_events = a_raw.groupby(['date','ind_code']).first().reset_index()
a_events = a_events.merge(a_count, on=['date','ind_code'])
a_events = a_events.merge(a_max_days, on=['date','ind_code'])
a_events['ind_name'] = a_events['ind_code'].map(ind_name_map)

print(f"  A组去重事件: {len(a_events)}条")

# ─── A组: 两连板行业后20日超额 ───
print("计算A组(两连板行业超额)...")
a_results = []
for _, row in a_events.iterrows():
    # 行业收益(排除涨停股)
    ind_ret = calc_cumulative_ind_ret(row['ind_code'], row['date'], HOLD_PERIOD, 'avg_pct_excl')
    # C组基线: 同日无涨停行业均值
    c_ret = calc_cumulative_c_group_ret(row['date'], HOLD_PERIOD)
    # 行业前20日涨幅(追涨检验)
    prev_rets = []
    for d in get_past_dates(row['date'], HOLD_PERIOD):
        d_data = ind_daily_data.get(d, {}).get(row['ind_code'])
        if d_data:
            prev_rets.append(d_data['avg_pct'])
    prev_20 = sum(prev_rets) if len(prev_rets) >= HOLD_PERIOD * 0.8 else None
    
    if ind_ret is not None and c_ret is not None:
        a_results.append({
            'date': row['date'],
            'ind_code': row['ind_code'],
            'ind_name': row['ind_name'],
            'board_count': row['board_count'],
            'limit_days': row['max_limit_days'],
            'ind_ret_20': round(ind_ret, 4),
            'c_ret_20': round(c_ret, 4),
            'excess_20': round(ind_ret - c_ret, 4),
            'ind_ret_prev_20': round(prev_20, 4) if prev_20 is not None else None,
        })

a_df = pd.DataFrame(a_results)
print(f"  A组有效事件: {len(a_df)}条")

# ─── B组: 仅首板行业(非连板日) ───
print("计算B组(首板行业超额)...")

b_raw = pd.DataFrame(single_board_events, columns=['date','code','ind_code'])
# 剔除同日同行业有连板的(已归入A组)
a_ind_date_set = set(zip(a_events['date'], a_events['ind_code']))
b_raw = b_raw[~b_raw.apply(lambda r: (r['date'], r['ind_code']) in a_ind_date_set, axis=1)]

b_count = b_raw.groupby(['date','ind_code']).size().reset_index(name='single_count')
b_events = b_raw.groupby(['date','ind_code']).first().reset_index()
b_events = b_events.merge(b_count, on=['date','ind_code'])
b_events['ind_name'] = b_events['ind_code'].map(ind_name_map)

b_results = []
for _, row in b_events.iterrows():
    ind_ret = calc_cumulative_ind_ret(row['ind_code'], row['date'], HOLD_PERIOD, 'avg_pct_excl')
    c_ret = calc_cumulative_c_group_ret(row['date'], HOLD_PERIOD)
    if ind_ret is not None and c_ret is not None:
        b_results.append({
            'date': row['date'],
            'ind_code': row['ind_code'],
            'ind_name': row['ind_name'],
            'single_count': row['single_count'],
            'ind_ret_20': round(ind_ret, 4),
            'c_ret_20': round(c_ret, 4),
            'excess_20': round(ind_ret - c_ret, 4),
        })

b_df = pd.DataFrame(b_results)
print(f"  B组有效事件: {len(b_df)}条")

# ─── C组: 无涨停行业基线 ───
print("计算C组(无涨停行业基线)...")
# 抽样计算: 每天取5个无涨停行业，避免20万+条数据
c_results = []
np.random.seed(42)
for date in trade_dates:
    limit_inds = set()
    for code in limit_by_date.get(date, set()):
        ind = code_to_ind.get(code)
        if ind:
            limit_inds.add(ind)
    
    d_data = ind_daily_data.get(date, {})
    no_limit_inds = sorted([ind for ind in d_data if ind not in limit_inds])
    # 抽样5个(如果少于5则全取)
    sampled = no_limit_inds if len(no_limit_inds) <= 5 else np.random.choice(no_limit_inds, 5, replace=False)
    
    for ind_code in sampled:
        ind_ret = calc_cumulative_ind_ret(ind_code, date, HOLD_PERIOD, 'avg_pct_excl')
        c_ret = calc_cumulative_c_group_ret(date, HOLD_PERIOD)
        if ind_ret is not None and c_ret is not None:
            c_results.append({
                'date': date,
                'ind_code': ind_code,
                'ind_name': ind_name_map.get(ind_code, ind_code),
                'ind_ret_20': round(ind_ret, 4),
                'c_ret_20': round(c_ret, 4),
                'excess_20': round(ind_ret - c_ret, 4),
            })

c_df = pd.DataFrame(c_results)
print(f"  C组抽样事件: {len(c_df)}条")

# ─── D组: 连板股本身 ───
print("计算D组(连板股本身超额)...")

# 需要逐日加载个股后续20日涨跌幅
# 高效做法: 先批量加载所有需要的日期数据到dict
needed_dates = set()
for _, row in a_events.iterrows():
    for fd in get_future_dates(row['date'], HOLD_PERIOD):
        needed_dates.add(fd)

print(f"  需加载{len(needed_dates)}天日线数据用于D组...")
stock_daily_cache = {}
for fd in needed_dates:
    fpath = os.path.join(MARKET_DIR, f"{fd}.parquet")
    if os.path.exists(fpath):
        df_day = pd.read_parquet(fpath)
        df_day = df_day[df_day['code'].apply(is_valid_a_share)]
        # 只取code和pctChg
        stock_daily_cache[fd] = dict(zip(df_day['code'], df_day['pctChg']))

d_results = []
for _, row in a_events.iterrows():
    event_date = row['date']
    stock_code = row['code']
    future = get_future_dates(event_date, HOLD_PERIOD)
    
    stock_rets = []
    for fd in future:
        pct = stock_daily_cache.get(fd, {}).get(stock_code)
        if pct is not None:
            stock_rets.append(pct)
    
    if len(stock_rets) < HOLD_PERIOD * 0.8:
        continue
    
    stock_cum = sum(stock_rets)
    # D组超额基准: 同行业同行等权收益(排除连板股)
    peer_rets = []
    for fd in future:
        d_data = ind_daily_data.get(fd, {}).get(row['ind_code'])
        if d_data:
            peer_rets.append(d_data['avg_pct_excl'])
    
    if len(peer_rets) < HOLD_PERIOD * 0.8:
        continue
    
    peer_cum = sum(peer_rets)
    d_results.append({
        'date': event_date,
        'code': stock_code,
        'ind_code': row['ind_code'],
        'ind_name': row['ind_name'],
        'limit_days': row['max_limit_days'],
        'stock_ret_20': round(stock_cum, 4),
        'peer_ret_20': round(peer_cum, 4),
        'excess_20': round(stock_cum - peer_cum, 4),
    })

d_df = pd.DataFrame(d_results)
print(f"  D组有效事件: {len(d_df)}条")


# ═══════════════════════════════════════════════════════════════════
# 步骤5: 统计汇总
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤5: 四组统计汇总")
print("=" * 60)

def calc_stats(df, label, excess_col='excess_20'):
    """计算一组事件的统计指标"""
    if len(df) == 0:
        return {'组': label, '事件数': 0}
    
    excess = df[excess_col].dropna()
    if len(excess) == 0:
        return {'组': label, '事件数': len(df), '有效数': 0}
    
    mean_ex = excess.mean()
    std_ex  = excess.std()
    win_rate = (excess > 0).mean() * 100
    t_val = mean_ex / (std_ex / np.sqrt(len(excess))) if std_ex > 0 else 0
    ir = mean_ex / std_ex if std_ex > 0 else 0
    
    return {
        '组': label,
        '事件数': len(df),
        '有效数': len(excess),
        '平均超额(%)': round(mean_ex, 4),
        '超额标准差(%)': round(std_ex, 4),
        '胜率(%)': round(win_rate, 2),
        't值': round(t_val, 2),
        'IR': round(ir, 4),
    }

# 四组统计
stats_all = [
    calc_stats(a_df, 'A-两连板行业'),
    calc_stats(b_df, 'B-首板行业'),
    calc_stats(c_df, 'C-无涨停行业(基线)'),
    calc_stats(d_df, 'D-连板股本身'),
]

stats_df = pd.DataFrame(stats_all)
print("\n=== 四组对照统计汇总 ===")
print(stats_df.to_string(index=False))


# ═══════════════════════════════════════════════════════════════════
# 步骤6: 维度分解
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤6: 维度分解")
print("=" * 60)

dim_results = []

# ─── 6.1 连板强度 ───
print("\n--- 6.1 连板强度分解 ---")
if len(a_df) > 0:
    two_only = a_df[a_df['limit_days'] == 2]
    three_plus = a_df[a_df['limit_days'] >= 3]
    dim_results.append(calc_stats(two_only, '连板强度-仅2天'))
    dim_results.append(calc_stats(three_plus, '连板强度-≥3天'))
    print(f"  仅2天: {calc_stats(two_only, '仅2天')}")
    print(f"  ≥3天: {calc_stats(three_plus, '≥3天')}")

# ─── 6.2 连板股数量 ───
print("\n--- 6.2 行业内连板股数量分解 ---")
if len(a_df) > 0:
    single_board_ind = a_df[a_df['board_count'] == 1]
    multi_board_ind  = a_df[a_df['board_count'] >= 2]
    dim_results.append(calc_stats(single_board_ind, '连板数量-1只'))
    dim_results.append(calc_stats(multi_board_ind, '连板数量-≥2只'))
    print(f"  1只连板: {calc_stats(single_board_ind, '1只连板')}")
    print(f"  ≥2只连板: {calc_stats(multi_board_ind, '≥2只连板')}")

# ─── 6.3 行业冷热(宽度2前20日均值) ───
print("\n--- 6.3 行业冷热分解(宽度2定义) ---")
if len(a_df) > 0:
    # 计算每个信号日行业的宽度2前20日均值(宽度1 = MA20占比)
    a_widths = []
    for _, row in a_df.iterrows():
        past = get_past_dates(row['date'], 20)
        bw_rets = []
        for d in past:
            d_data = ind_daily_data.get(d, {}).get(row['ind_code'])
            if d_data:
                bw_rets.append(d_data['breadth2'])
        bw20_mean = np.mean(bw_rets) if len(bw_rets) >= 15 else None
        a_widths.append(bw20_mean)
    
    a_df['bw20_mean'] = a_widths
    a_valid_bw = a_df[a_df['bw20_mean'].notna()]
    
    cold = a_valid_bw[a_valid_bw['bw20_mean'] <= 0.3]  # 冷区: MA20宽度≤30%
    warm = a_valid_bw[(a_valid_bw['bw20_mean'] > 0.3) & (a_valid_bw['bw20_mean'] <= 0.5)]
    hot  = a_valid_bw[a_valid_bw['bw20_mean'] > 0.5]  # 热区: MA20宽度>50%
    
    dim_results.append(calc_stats(cold, '冷热-冷区(宽度1≤30%)'))
    dim_results.append(calc_stats(warm, '冷热-温区(30%~50%)'))
    dim_results.append(calc_stats(hot,  '冷热-热区(宽度1>50%)'))
    print(f"  冷区(≤30%): {calc_stats(cold, '冷区')}")
    print(f"  温区(30~50%): {calc_stats(warm, '温区')}")
    print(f"  热区(>50%): {calc_stats(hot, '热区')}")

# ─── 6.4 市场状态(924分段) ───
print("\n--- 6.4 市场状态分解(924前后) ---")
if len(a_df) > 0:
    pre_924  = a_df[a_df['date'] < '2024-09-24']
    post_924 = a_df[a_df['date'] >= '2024-09-24']
    recent   = a_df[a_df['date'] >= '2026-01-01']
    
    dim_results.append(calc_stats(pre_924,  '时段-924前'))
    dim_results.append(calc_stats(post_924, '时段-924后'))
    dim_results.append(calc_stats(recent,   '时段-近半年2026'))
    
    print(f"  924前: {calc_stats(pre_924, '924前')}")
    print(f"  924后: {calc_stats(post_924, '924后')}")
    print(f"  近半年: {calc_stats(recent, '近半年')}")

    # B组也做924分段
    if len(b_df) > 0:
        b_pre  = b_df[b_df['date'] < '2024-09-24']
        b_post = b_df[b_df['date'] >= '2024-09-24']
        dim_results.append(calc_stats(b_pre,  'B组-924前'))
        dim_results.append(calc_stats(b_post, 'B组-924后'))
        print(f"  B组924前: {calc_stats(b_pre, 'B-924前')}")
        print(f"  B组924后: {calc_stats(b_post, 'B-924后')}")

# ─── 6.5 超额来源检验(追涨陷阱) ───
print("\n--- 6.5 超额来源检验 ---")
if len(a_df) > 0 and 'ind_ret_prev_20' in a_df.columns:
    a_valid = a_df[a_df['ind_ret_prev_20'].notna()]
    if len(a_valid) > 10:
        print(f"  前20日涨幅分布:")
        print(f"    均值: {a_valid['ind_ret_prev_20'].mean():.2f}%")
        print(f"    中位: {a_valid['ind_ret_prev_20'].median():.2f}%")
        print(f"    <0(冷区): {(a_valid['ind_ret_prev_20'] < 0).mean()*100:.1f}%")
        print(f"    ≥5%(热区): {(a_valid['ind_ret_prev_20'] >= 5).mean()*100:.1f}%")
        
        # 纯numpy计算Pearson相关系数
        valid_mask = a_valid['ind_ret_prev_20'].notna() & a_valid['excess_20'].notna()
        x = a_valid.loc[valid_mask, 'ind_ret_prev_20'].values.astype(float)
        y = a_valid.loc[valid_mask, 'excess_20'].values.astype(float)
        r = np.corrcoef(x, y)[0, 1] if len(x) > 2 else 0.0
        n = len(x)
        t_stat = r * np.sqrt((n - 2) / (1 - r**2 + 1e-10))
        p_val = 2 * (1 - _t_cdf_approx(abs(t_stat), n - 2))  # 双尾
        print(f"\n  前20日涨幅 vs 后20日超额: r={r:.3f}, p={p_val:.4f}")
        if r < 0:
            print("  → 负相关: 前期跌→后续超额高(均值回归)")
        elif r > 0:
            print("  → 正相关: 前期涨→后续超额高(趋势延续)")
        else:
            print("  → 无相关: 超额与前期走势无关")


# ═══════════════════════════════════════════════════════════════════
# 步骤7: 保存结果 + 生成中文报告
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤7: 保存结果与生成报告")
print("=" * 60)

# 保存CSV
OUTPUT_DIR.mkdir(exist_ok=True)
if len(a_df) > 0:
    a_df.to_csv(OUTPUT_DIR / 'two_board_events_v2.csv', index=False)
    print(f"  A组事件明细: {len(a_df)}行")
if len(b_df) > 0:
    b_df.to_csv(OUTPUT_DIR / 'single_board_events_v2.csv', index=False)
    print(f"  B组事件明细: {len(b_df)}行")
stats_df.to_csv(OUTPUT_DIR / 'two_board_stats_v2.csv', index=False)
if dim_results:
    pd.DataFrame(dim_results).to_csv(OUTPUT_DIR / 'two_board_dimensions_v2.csv', index=False)

# ─── 生成Markdown中文报告 ───
report_lines = []
report_lines.append("# 两连板涨停行业超额收益回测报告")
report_lines.append(f"\n**回测区间**: {trade_dates[0]} ~ {trade_dates[-1]} ({len(trade_dates)}个交易日)")
report_lines.append(f"\n**核心命题**: 行业中出现两连板涨停后，行业后续{HOLD_PERIOD}日是否产生超额收益？")
report_lines.append("\n## 一、方法论")
report_lines.append("- 事件驱动法(稀疏事件因子不能用截面IC)")
report_lines.append("- 涨停阈值分板块: 主板9.8%, 双创19.5%, 北交所29.5%, ST 4.8%")
report_lines.append("- 行业收益排除涨停股(分离龙头vs同行)")
report_lines.append("- 超额 = 信号组收益 − 对照组收益")
report_lines.append("\n## 二、四组对照")
report_lines.append("| 组 | 定义 | 超额基准 |")
report_lines.append("|:---|:---|:---|")
report_lines.append("| A | 两连板行业后20日收益 | 同日无涨停行业均值(C组) |")
report_lines.append("| B | 仅首板行业后20日收益 | 同日无涨停行业均值(C组) |")
report_lines.append("| C | 无涨停行业后20日收益 | 基线 |")
report_lines.append("| D | 连板股本身后20日收益 | 同行业同行等权收益 |")

report_lines.append("\n## 三、统计汇总\n")
report_lines.append(stats_df.to_markdown(index=False) if hasattr(stats_df, 'to_markdown') else stats_df.to_string(index=False))

if dim_results:
    report_lines.append("\n## 四、维度分解\n")
    dim_df = pd.DataFrame(dim_results)
    report_lines.append(dim_df.to_markdown(index=False) if hasattr(dim_df, 'to_markdown') else dim_df.to_string(index=False))

# 追涨检验
if len(a_df) > 0 and 'ind_ret_prev_20' in a_df.columns:
    a_valid = a_df[a_df['ind_ret_prev_20'].notna()]
    if len(a_valid) > 10:
        report_lines.append("\n## 五、超额来源检验(追涨陷阱)\n")
        report_lines.append(f"- 信号日行业前20日涨幅均值: **{a_valid['ind_ret_prev_20'].mean():.2f}%**")
        report_lines.append(f"- 信号日行业前20日涨幅中位: **{a_valid['ind_ret_prev_20'].median():.2f}%**")
        # 纯numpy计算Pearson相关系数
        # 先对齐有效索引
        valid_mask = a_valid['ind_ret_prev_20'].notna() & a_valid['excess_20'].notna()
        x = a_valid.loc[valid_mask, 'ind_ret_prev_20'].values.astype(float)
        y = a_valid.loc[valid_mask, 'excess_20'].values.astype(float)
        r = np.corrcoef(x, y)[0, 1] if len(x) > 2 else 0.0
        n = len(x)
        t_stat_r = r * np.sqrt((n - 2) / (1 - r**2 + 1e-10))
        p_val = 2 * (1 - _t_cdf_approx(abs(t_stat_r), n - 2))
        report_lines.append(f"- 前20日涨幅 vs 后20日超额相关: **r={r:.3f}, p={p_val:.4f}**")
        if r < 0:
            report_lines.append("- 结论: 负相关 → **均值回归特征**(前期跌幅越大→后续超额越高)")
        elif r > 0:
            report_lines.append("- 结论: 正相关 → **趋势延续特征**(前期涨幅越大→后续超额越高)")
        else:
            report_lines.append("- 结论: 无相关 → 超额与前期走势无关(独立信号)")

report_lines.append("\n## 六、事件频率统计\n")
# 月度事件频率
if len(a_df) > 0:
    a_df['month'] = a_df['date'].str[:7]
    monthly_freq = a_df.groupby('month').size()
    report_lines.append(f"- 月均连板行业事件数: **{monthly_freq.mean():.1f}**")
    report_lines.append(f"- 最少月: {monthly_freq.min()}条, 最多月: {monthly_freq.max()}条")

report_lines.append("\n---\n*报告生成时间: " + pd.Timestamp.now().strftime('%Y-%m-%d %H:%M') + "*")

report_text = '\n'.join(report_lines)
with open(OUTPUT_DIR / 'two_board_report_v2.md', 'w') as f:
    f.write(report_text)
print(f"  中文报告已保存: {OUTPUT_DIR / 'two_board_report_v2.md'}")

print("\n" + "=" * 60)
print("回测完成!")
print("=" * 60)
