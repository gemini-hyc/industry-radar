"""
两连板涨停行业超额收益回测
核心命题: 行业中出现两连板(连续2天涨停)的股票后，行业后续20日是否产生超额收益?
方法: 事件驱动法(条件因子不能用截面IC)
排除ST涨停(名称含ST/星号ST)
涨跌停阈值分板块: 主板9.8%, 双创19.5%, 北交所29.5%
"""
import os
import sys
import time
import numpy as np
import pandas as pd
from collections import defaultdict

# ─── 配置 ───
MARKET_DIR = '/opt/data/quant-data/market/daily/'
IND_MEMBERS_PATH = '/opt/data/quant-data/industry/industry_members.parquet'
IND_LIST_PATH = '/opt/data/quant-data/industry/industry_list.parquet'
STOCK_BASIC_PATH = '/opt/data/quant-data/stock_basic.parquet'
OUTPUT_DIR = '/opt/data/quant/output/'
HOLD_PERIOD = 20  # 持仓天数

# 涨停阈值(不含ST)
LIMIT_THRESHOLDS = {
    'main': 9.8,       # sh.600/601/603/605, sz.000/001/002/003
    'kcb_cyb': 19.5,   # sh.688/689, sz.300/301/302
    'bse': 29.5,       # sz.920
}

def get_limit_threshold(code):
    """根据代码前缀返回涨停阈值"""
    if code.startswith('sz.920'):
        return LIMIT_THRESHOLDS['bse']
    elif code.startswith(('sz.300', 'sz.301', 'sz.302', 'sh.688', 'sh.689')):
        return LIMIT_THRESHOLDS['kcb_cyb']
    else:
        return LIMIT_THRESHOLDS['main']

def is_valid_a_share(code):
    """判断是否是A股(排除指数、ETF、B股)"""
    valid_prefixes = ['600', '601', '603', '605', '688', '689',
                      '000', '001', '002', '003', '300', '301', '302', '920']
    num = code.split('.')[1] if '.' in code else code[3:]
    return any(num.startswith(p) for p in valid_prefixes)


# ═══════════════════════════════════════════════════════════════════
# 步骤1: 加载行业映射 + 股票基本信息
# ═══════════════════════════════════════════════════════════════════

print("=" * 60)
print("步骤1: 加载行业映射与股票基本信息")
print("=" * 60)

ind_members = pd.read_parquet(IND_MEMBERS_PATH)
# 统一代码格式
ind_members['stock_code'] = ind_members['stock_code'].str.lower()
print(f"行业成员映射: {ind_members['ind_code'].nunique()}行业, {ind_members['stock_code'].nunique()}股票")

ind_list = pd.read_parquet(IND_LIST_PATH)
print(f"行业列表: {len(ind_list)}行业")

# 构建代码→行业映射
code_to_ind = dict(zip(ind_members['stock_code'], ind_members['ind_code']))
code_to_ind_name = dict(zip(ind_members['stock_code'], ind_members['ind_name']))

# 加载stock_basic获取ST标记
stock_basic = pd.read_parquet(STOCK_BASIC_PATH)
# BaoStock格式代码
stock_basic['bs_code'] = stock_basic['ts_code'].apply(
    lambda x: f"{x[:6].lower()}" if '.' in x else x.lower()
)
# 需要转成sz.000001格式
stock_basic['code'] = stock_basic['ts_code'].apply(
    lambda x: f"{'sz' if x.endswith('.SZ') else 'sh'}.{x[:6]}"
)
stock_basic['is_st'] = stock_basic['name'].str.contains('ST', case=False, na=False)
st_codes = set(stock_basic[stock_basic['is_st']]['code'].tolist())
print(f"ST股票数: {len(st_codes)}")


# ═══════════════════════════════════════════════════════════════════
# 步骤2: 逐日扫描涨停 + 识别连板
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤2: 逐日扫描涨停与连板识别")
print("=" * 60)

# 加载所有日线数据
all_files = sorted([f for f in os.listdir(MARKET_DIR) if f.endswith('.parquet')])
print(f"日线数据: {len(all_files)}天, {all_files[0]} ~ {all_files[-1]}")

# 逐日加载, 识别涨停股
t0 = time.time()
limit_by_date = {}  # {date: set of limit_up codes}
all_daily = {}      # {date: DataFrame} 只存必要字段

# 用内存友好的方式: 逐日加载, 只保留涨停标记
# 需要连续2天数据来判断连板, 所以保留最近2天的涨停集合
prev_limit_set = None
prev_date = None

# 存储: 连板事件列表
two_board_events = []  # [(date, code, industry, ind_name, limit_days)]
single_board_events = []  # [(date, code, industry, ind_name)]
limit_days_counter = {}  # {code: 连续涨停天数}

# 全量行业等权涨跌幅
ind_eq_ret = {}  # {date: {ind_code: avg_pctChg}}
market_avg_ret = {}  # {date: 全市场行业等权均值}

for i, fname in enumerate(all_files):
    date = fname.replace('.parquet', '')
    fpath = os.path.join(MARKET_DIR, fname)
    
    df = pd.read_parquet(fpath)
    
    # 过滤A股
    df = df[df['code'].apply(is_valid_a_share)].copy()
    
    # 排除指数
    df = df[~df['code'].str.match(r'^sh\.000')].copy()
    df = df[~df['code'].str.match(r'^sz\.399')].copy()
    
    # 排除ST涨停(ST股的涨停不算信号, 但ST股后续涨跌幅仍正常计入行业均值)
    # 涨停判定时排除ST, 但行业均值计算时不排除ST(ST涨跌幅是真实市场数据)
    df['is_st'] = df['code'].isin(st_codes)
    
    # 涨停判定(非ST)
    df_non_st = df[~df['is_st']].copy()
    df_non_st['limit_threshold'] = df_non_st['code'].apply(get_limit_threshold)
    df_non_st['is_limit'] = df_non_st['pctChg'] >= df_non_st['limit_threshold']
    
    current_limit_set = set(df_non_st[df_non_st['is_limit']]['code'].tolist())
    limit_by_date[date] = current_limit_set
    
    # ─── 连板识别 ───
    # 连续涨停天数: 如果今天涨停且昨天涨停, counter+1; 否则重置为1(涨停)或0(不涨停)
    for code in df_non_st['code'].unique():
        if code in current_limit_set:
            limit_days_counter[code] = limit_days_counter.get(code, 0) + 1
        else:
            limit_days_counter[code] = 0
    
    # 映射到行业
    for code in current_limit_set:
        ind_code = code_to_ind.get(code)
        if ind_code is None:
            continue
        ind_name = code_to_ind_name.get(code, ind_code)
        consecutive = limit_days_counter.get(code, 0)
        
        if consecutive >= 2:
            two_board_events.append((date, code, ind_code, ind_name, consecutive))
        elif consecutive == 1:
            single_board_events.append((date, code, ind_code, ind_name))
    
    # ─── 计算行业等权涨跌幅 ───
    # 用全量数据(含ST)计算行业均值, 因为ST的涨跌也是行业真实表现
    df_with_ind = df.merge(ind_members[['stock_code', 'ind_code']], 
                           left_on='code', right_on='stock_code', how='left')
    ind_ret = df_with_ind.groupby('ind_code')['pctChg'].mean()
    ind_eq_ret[date] = ind_ret.to_dict()
    market_avg_ret[date] = ind_ret.mean()  # 131行业等权均值
    
    # 进度报告
    if (i + 1) % 200 == 0 or i == len(all_files) - 1:
        print(f"  进度: {i+1}/{len(all_files)}天, 累计连板事件{len(two_board_events)}条, 耗时{time.time()-t0:.1f}s")

print(f"\n扫描完成! 耗时{time.time()-t0:.1f}s")
print(f"  两连板事件: {len(two_board_events)}条")
print(f"  单板事件: {len(single_board_events)}条")


# ═══════════════════════════════════════════════════════════════════
# 步骤3: 构建事件表 + 计算超额收益
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤3: 构建事件表与超额收益计算")
print("=" * 60)

# 交易日序列(用于查找后N日)
trade_dates = sorted(all_daily.keys()) if all_daily else sorted(limit_by_date.keys())
date_idx = {d: i for i, d in enumerate(trade_dates)}

def get_future_dates(event_date, n_days):
    """获取事件日后n个交易日的日期列表"""
    idx = date_idx.get(event_date)
    if idx is None:
        return []
    end_idx = min(idx + n_days, len(trade_dates) - 1)
    return trade_dates[idx+1:end_idx+1]

def get_ind_cumulative_ret(ind_code, start_date, n_days):
    """计算行业后n日累计等权涨跌幅(百分数直接累加)"""
    future_dates = get_future_dates(start_date, n_days)
    if not future_dates:
        return None
    rets = []
    for d in future_dates:
        r = ind_eq_ret.get(d, {}).get(ind_code)
        if r is not None:
            rets.append(r)
    if len(rets) < n_days * 0.8:  # 至少覆盖80%交易日
        return None
    return sum(rets)  # pctChg百分数累加=累计收益(百分数)

def get_market_cumulative_ret(start_date, n_days):
    """计算全市场行业等权均值后n日累计涨跌幅"""
    future_dates = get_future_dates(start_date, n_days)
    if not future_dates:
        return None
    rets = []
    for d in future_dates:
        r = market_avg_ret.get(d)
        if r is not None:
            rets.append(r)
    if len(rets) < n_days * 0.8:
        return None
    return sum(rets)


# ─── A组: 两连板行业的后20日超额 ───
print("计算A组(两连板行业超额)...")

# 去重: 同一天同一行业只算一次事件(即使行业内多只连板)
a_events_raw = pd.DataFrame(two_board_events, columns=['date', 'code', 'ind_code', 'ind_name', 'limit_days'])

# 添加连板股数量(同行业同日有几只连板)
a_event_count = a_events_raw.groupby(['date', 'ind_code']).size().reset_index(name='board_count')
a_events = a_events_raw.groupby(['date', 'ind_code']).first().reset_index()
a_events = a_events.merge(a_event_count, on=['date', 'ind_code'])

# 计算超额收益
a_results = []
for _, row in a_events.iterrows():
    ind_ret_20 = get_ind_cumulative_ret(row['ind_code'], row['date'], HOLD_PERIOD)
    mkt_ret_20 = get_market_cumulative_ret(row['date'], HOLD_PERIOD)
    if ind_ret_20 is not None and mkt_ret_20 is not None:
        excess = ind_ret_20 - mkt_ret_20
        # 行业前20日涨幅(检查追涨陷阱)
        ind_ret_prev_20 = get_ind_cumulative_ret_past(row['ind_code'], row['date'])
        a_results.append({
            'date': row['date'],
            'ind_code': row['ind_code'],
            'ind_name': row['ind_name'],
            'board_count': row['board_count'],
            'limit_days': row['limit_days'],
            'ind_ret_20': ind_ret_20,
            'mkt_ret_20': mkt_ret_20,
            'excess_20': excess,
            'ind_ret_prev_20': ind_ret_prev_20,
        })

# ─── 需要计算前20日涨幅 ───
# 添加前20日函数
def get_ind_cumulative_ret_past(ind_code, event_date, n_days=20):
    """计算行业前n日累计等权涨跌幅"""
    idx = date_idx.get(event_date)
    if idx is None or idx < n_days:
        return None
    past_dates = trade_dates[idx-n_days:idx]
    rets = []
    for d in past_dates:
        r = ind_eq_ret.get(d, {}).get(ind_code)
        if r is not None:
            rets.append(r)
    if len(rets) < n_days * 0.8:
        return None
    return sum(rets)

# 重新计算含前20日涨幅
a_results = []
for _, row in a_events.iterrows():
    ind_ret_20 = get_ind_cumulative_ret(row['ind_code'], row['date'], HOLD_PERIOD)
    mkt_ret_20 = get_market_cumulative_ret(row['date'], HOLD_PERIOD)
    ind_ret_prev_20 = get_ind_cumulative_ret_past(row['ind_code'], row['date'], HOLD_PERIOD)
    if ind_ret_20 is not None and mkt_ret_20 is not None:
        excess = ind_ret_20 - mkt_ret_20
        a_results.append({
            'date': row['date'],
            'ind_code': row['ind_code'],
            'ind_name': row['ind_name'],
            'board_count': row['board_count'],
            'limit_days': row['limit_days'],
            'ind_ret_20': ind_ret_20,
            'mkt_ret_20': mkt_ret_20,
            'excess_20': excess,
            'ind_ret_prev_20': ind_ret_prev_20,
        })

a_df = pd.DataFrame(a_results)
print(f"  A组(两连板行业): {len(a_df)}条有效事件")


# ─── B组: 仅首板(非连板)行业的后20日超额 ───
print("计算B组(首板行业超额)...")

b_events_raw = pd.DataFrame(single_board_events, columns=['date', 'code', 'ind_code', 'ind_name'])
# 剔除: 如果同一天同一行业有两连板, 该行业的首板事件不算(已归入A组)
a_ind_date_set = set(zip(a_events['date'], a_events['ind_code']))
b_events_raw = b_events_raw[~b_events_raw.apply(lambda r: (r['date'], r['ind_code']) in a_ind_date_set, axis=1)]

# 去重: 同一天同一行业
b_event_count = b_events_raw.groupby(['date', 'ind_code']).size().reset_index(name='single_count')
b_events = b_events_raw.groupby(['date', 'ind_code']).first().reset_index()
b_events = b_events.merge(b_event_count, on=['date', 'ind_code'])

b_results = []
for _, row in b_events.iterrows():
    ind_ret_20 = get_ind_cumulative_ret(row['ind_code'], row['date'], HOLD_PERIOD)
    mkt_ret_20 = get_market_cumulative_ret(row['date'], HOLD_PERIOD)
    if ind_ret_20 is not None and mkt_ret_20 is not None:
        excess = ind_ret_20 - mkt_ret_20
        b_results.append({
            'date': row['date'],
            'ind_code': row['ind_code'],
            'ind_name': row['ind_name'],
            'single_count': row['single_count'],
            'ind_ret_20': ind_ret_20,
            'mkt_ret_20': mkt_ret_20,
            'excess_20': excess,
        })

b_df = pd.DataFrame(b_results)
print(f"  B组(首板行业): {len(b_df)}条有效事件")


# ─── C组: 同日无涨停行业的后20日超额(基线) ───
print("计算C组(无涨停行业基线)...")

# 每天找出有涨停的行业
limit_ind_by_date = {}
for date, limit_codes in limit_by_date.items():
    inds = set()
    for code in limit_codes:
        ind = code_to_ind.get(code)
        if ind:
            inds.add(ind)
    limit_ind_by_date[date] = inds

c_results = []
for date in trade_dates:
    limit_inds = limit_ind_by_date.get(date, set())
    for ind_code, ind_ret in ind_eq_ret.get(date, {}).items():
        if ind_code in limit_inds:
            continue  # 排除有涨停的行业
        ind_name = ind_list[ind_list['ts_code'] == ind_code]['name'].values
        ind_name = ind_name[0] if len(ind_name) > 0 else ind_code
        ind_ret_20 = get_ind_cumulative_ret(ind_code, date, HOLD_PERIOD)
        mkt_ret_20 = get_market_cumulative_ret(date, HOLD_PERIOD)
        if ind_ret_20 is not None and mkt_ret_20 is not None:
            c_results.append({
                'date': date,
                'ind_code': ind_code,
                'ind_name': ind_name,
                'excess_20': ind_ret_20 - mkt_ret_20,
            })

c_df = pd.DataFrame(c_results)
print(f"  C组(无涨停行业): {len(c_df)}条")


# ─── D组: 连板股本身后20日超额 ───
print("计算D组(连板股本身超额)...")

# 需要加载个股后续20日涨幅
# 从事件表获取连板股代码和日期
d_results = []
for _, row in a_events.iterrows():
    event_date = row['date']
    stock_code = row['code']
    # 后续20日个股累计涨幅
    future_dates = get_future_dates(event_date, HOLD_PERIOD)
    if not future_dates:
        continue
    stock_rets = []
    for fd in future_dates:
        fpath = os.path.join(MARKET_DIR, f"{fd}.parquet")
        if not os.path.exists(fpath):
            continue
        day_df = pd.read_parquet(fpath)
        day_df = day_df[day_df['code'].apply(is_valid_a_share)]
        day_df = day_df[~day_df['code'].str.match(r'^sh\.000')]
        day_df = day_df[~day_df['code'].str.match(r'^sz\.399')]
        stock_row = day_df[day_df['code'] == stock_code]
        if len(stock_row) > 0:
            stock_rets.append(stock_row['pctChg'].values[0])
    
    if len(stock_rets) < HOLD_PERIOD * 0.8:
        continue
    
    stock_cum_ret = sum(stock_rets)
    mkt_ret_20 = get_market_cumulative_ret(event_date, HOLD_PERIOD)
    if mkt_ret_20 is not None:
        d_results.append({
            'date': event_date,
            'code': stock_code,
            'ind_code': row['ind_code'],
            'ind_name': row['ind_name'],
            'limit_days': row['limit_days'],
            'stock_ret_20': stock_cum_ret,
            'mkt_ret_20': mkt_ret_20,
            'excess_20': stock_cum_ret - mkt_ret_20,
        })

d_df = pd.DataFrame(d_results)
print(f"  D组(连板股本身): {len(d_df)}条")


# ═══════════════════════════════════════════════════════════════════
# 步骤4: 统计汇总
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤4: 统计汇总")
print("=" * 60)

def calc_stats(df, label, excess_col='excess_20'):
    """计算一组事件的统计指标"""
    if len(df) == 0:
        return {'组': label, '事件数': 0}
    
    excess = df[excess_col]
    mean_ex = excess.mean()
    std_ex = excess.std()
    win_rate = (excess > 0).mean() * 100
    t_val = mean_ex / (std_ex / np.sqrt(len(df))) if std_ex > 0 else 0
    ir = mean_ex / std_ex if std_ex > 0 else 0
    
    return {
        '组': label,
        '事件数': len(df),
        '平均超额(%)': round(mean_ex, 4),
        '超额标准差(%)': round(std_ex, 4),
        '胜率(%)': round(win_rate, 2),
        't值': round(t_val, 2),
        'IR': round(ir, 4),
    }

# 四组统计
stats_all = []
stats_all.append(calc_stats(a_df, 'A组-两连板行业'))
stats_all.append(calc_stats(b_df, 'B组-首板行业'))
stats_all.append(calc_stats(c_df, 'C组-无涨停行业'))
stats_all.append(calc_stats(d_df, 'D组-连板股本身'))

stats_df = pd.DataFrame(stats_all)
print("\n=== 四组对照统计汇总 ===")
print(stats_df.to_string(index=False))


# ═══════════════════════════════════════════════════════════════════
# 步骤5: 维度分解
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤5: 维度分解")
print("=" * 60)

# ─── 5.1 连板强度分解 ───
print("\n--- 5.1 连板强度分解 ---")
if len(a_df) > 0:
    two_only = a_df[a_df['limit_days'] == 2]
    three_plus = a_df[a_df['limit_days'] >= 3]
    s1 = calc_stats(two_only, '两连板(仅2天)')
    s2 = calc_stats(three_plus, '三连板+')
    print(f"  两连板(仅2天): {s1}")
    print(f"  三连板及以上: {s2}")

# ─── 5.2 连板股数量分解 ───
print("\n--- 5.2 行业内连板股数量分解 ---")
if len(a_df) > 0:
    single_board = a_df[a_df['board_count'] == 1]
    multi_board = a_df[a_df['board_count'] >= 2]
    s3 = calc_stats(single_board, '行业1只连板')
    s4 = calc_stats(multi_board, '行业≥2只连板')
    print(f"  行业1只连板: {s3}")
    print(f"  行业≥2只连板(群涨停): {s4}")

# ─── 5.3 行业冷热分解(宽度) ───
# 需要计算行业宽度(MA20占比=宽度1)
print("\n--- 5.3 行业冷热分解 ---")
print("  计算行业宽度(MA20占比)...")

# 加载足够多的日线来算MA20(最近60天即可, 但全历史需要逐日)
# 简化: 用信号日当天行业内pctChg>0的比例(宽度2)近似冷热
# 更精确: 用近20日均线, 但计算成本高
# 折中方案: 用信号日前20日行业内等权涨幅来判断冷热
#   前20日涨幅<0 → 冷区, 前20日涨幅>5% → 热区

if len(a_df) > 0 and 'ind_ret_prev_20' in a_df.columns:
    a_valid = a_df[a_df['ind_ret_prev_20'].notna()]
    cold = a_valid[a_valid['ind_ret_prev_20'] < 0]
    warm = a_valid[(a_valid['ind_ret_prev_20'] >= 0) & (a_valid['ind_ret_prev_20'] < 5)]
    hot = a_valid[a_valid['ind_ret_prev_20'] >= 5]
    s5 = calc_stats(cold, '冷区(前20日跌)')
    s6 = calc_stats(warm, '温区(前20日0~5%)')
    s7 = calc_stats(hot, '热区(前20日涨≥5%)')
    print(f"  冷区(前20日下跌): {s5}")
    print(f"  温区(前20日0~5%): {s6}")
    print(f"  热区(前20日涨≥5%): {s7}")

# ─── 5.4 市场状态分解(924前后) ───
print("\n--- 5.4 市场状态分解 ---")
if len(a_df) > 0:
    # 全样本
    s_all = calc_stats(a_df, '全样本(2020-2026)')
    
    # 924前
    pre_924 = a_df[a_df['date'] < '2024-09-24']
    s_pre = calc_stats(pre_924, '924前(2020~2024.09)')
    
    # 924后
    post_924 = a_df[a_df['date'] >= '2024-09-24']
    s_post = calc_stats(post_924, '924后(2024.09~2026)')
    
    # 最近半年
    recent = a_df[a_df['date'] >= '2026-01-01']
    s_recent = calc_stats(recent, '近半年(2026)')
    
    print(f"  全样本: {s_all}")
    print(f"  924前: {s_pre}")
    print(f"  924后: {s_post}")
    print(f"  近半年: {s_recent}")
    
    # B组也做924分段
    if len(b_df) > 0:
        b_pre924 = b_df[b_df['date'] < '2024-09-24']
        b_post924 = b_df[b_df['date'] >= '2024-09-24']
        print(f"\n  B组924前: {calc_stats(b_pre924, 'B组-924前')}")
        print(f"  B组924后: {calc_stats(b_post924, 'B组-924后')}")


# ═══════════════════════════════════════════════════════════════════
# 步骤6: 超额来源检验(追涨陷阱)
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤6: 超额来源检验")
print("=" * 60)

if len(a_df) > 0 and 'ind_ret_prev_20' in a_df.columns:
    a_valid = a_df[a_df['ind_ret_prev_20'].notna()]
    print(f"\n两连板行业前20日涨幅分布:")
    print(f"  均值: {a_valid['ind_ret_prev_20'].mean():.2f}%")
    print(f"  中位: {a_valid['ind_ret_prev_20'].median():.2f}%")
    print(f"  <0(冷区): {(a_valid['ind_ret_prev_20'] < 0).mean()*100:.1f}%")
    print(f"  0~5%: {((a_valid['ind_ret_prev_20'] >= 0) & (a_valid['ind_ret_prev_20'] < 5)).mean()*100:.1f}%")
    print(f"  ≥5%(热区): {(a_valid['ind_ret_prev_20'] >= 5).mean()*100:.1f}%")
    
    # 关键检验: 超额是否来自前20日大涨后的均值回归?
    # 如果前20日大涨+后20日正超额 → 可能是趋势延续而非均值回归
    # 如果前20日大跌+后20日正超额 → 均值回归
    from scipy import stats as sp_stats
    corr = sp_stats.pearsonr(a_valid['ind_ret_prev_20'], a_valid['excess_20'])
    print(f"\n前20日涨幅 vs 后20日超额相关系数: r={corr[0]:.3f}, p={corr[1]:.4f}")
    if corr[0] < 0:
        print("  → 负相关: 前期跌幅越大→后续超额越高(均值回归特征)")
    elif corr[0] > 0:
        print("  → 正相关: 前期涨幅越大→后续超额越高(趋势延续特征)")
    else:
        print("  → 无相关: 超额与前期走势无关(独立信号)")

# ═══════════════════════════════════════════════════════════════════
# 步骤7: 保存结果 + 生成报告
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("步骤7: 保存结果")
print("=" * 60)

# 保存事件明细CSV
if len(a_df) > 0:
    a_df.to_csv(os.path.join(OUTPUT_DIR, 'two_board_events_detail.csv'), index=False)
    print(f"  A组事件明细已保存: {len(a_df)}行")

if len(b_df) > 0:
    b_df.to_csv(os.path.join(OUTPUT_DIR, 'single_board_events_detail.csv'), index=False)
    print(f"  B组事件明细已保存: {len(b_df)}行")

stats_df.to_csv(os.path.join(OUTPUT_DIR, 'two_board_stats_summary.csv'), index=False)
print(f"  统计汇总已保存")

print("\n" + "=" * 60)
print("回测完成!")
print("=" * 60)
