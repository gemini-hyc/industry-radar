#!/usr/bin/env python3
"""
宽度2连续扩张事件 · 纯事件驱动回测
=================================
核心问题：宽度2（涨跌占比）连续≥3天扩张之后，会不会出现超额收益？

不做任何冷区过滤，只看"连续扩张"这个纯事件。
同时对比：
  A. 纯扩张事件（无冷区过滤）
  B. 冷区+扩张（前20日均宽≤30% + 连续扩张≥3天）
  C. 纯冷区对照（前20日均宽≤30%，无扩张要求）
  D. 随机对照（所有行业×日期的均值）

宽度2定义：行业内当日上涨个股占比 (pctChg>0).mean()
"""

import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from scipy import stats
from pathlib import Path

DATA_DIR = Path('/opt/data/quant-data')
MARKET_DAILY = DATA_DIR / 'market' / 'daily'
IND_DIR = DATA_DIR / 'industry'

# ═══════════════════════════════════════════
# 一、数据加载
# ═══════════════════════════════════════════

def load_market_data():
    """加载2020-01至最新的日线行情"""
    print("加载日线行情...")
    files = sorted(MARKET_DAILY.glob('*.parquet'))
    files = [f for f in files if f.stem[:4] >= '2020']
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)

    # 修复成交额单位
    if 'amount' in df.columns and 'volume' in df.columns:
        mask = (df['volume'] > 0) & (df['amount'] / df['volume'] < 0.1)
        if mask.sum() > 0:
            df.loc[mask, 'amount'] = df.loc[mask, 'amount'] * 10000

    # 过滤A股个股
    valid_prefixes = ["600","601","603","605","688","689",
                      "000","001","002","003","300","301","302","920"]
    code_prefix = df['code'].str.split('.').str[1].str[:3]
    a_stock = df[code_prefix.isin(valid_prefixes)].copy()
    a_stock = a_stock[~a_stock['code'].str.match(r'^sh\.000')]
    a_stock = a_stock[~a_stock['code'].str.match(r'^sz\.399')]
    a_stock = a_stock[~a_stock['code'].str.match(r'^sh\.5\d{5}')]
    a_stock = a_stock[~a_stock['code'].str.match(r'^sz\.15\d{5}')]
    a_stock = a_stock[~a_stock['stock_name'].str.contains('ST', na=False)]

    a_stock = a_stock.sort_values(['code', 'date']).reset_index(drop=True)
    print(f"  A股行情: {len(a_stock)} 行, {a_stock['code'].nunique()} 只, "
          f"日期 {a_stock['date'].min()} ~ {a_stock['date'].max()}")
    return a_stock


def load_industry_members():
    """加载申万二级行业成员映射"""
    print("加载行业成员映射...")
    members_path = IND_DIR / 'industry_members.parquet'
    df = pd.read_parquet(members_path)
    mapping = {}
    for ind_code, group in df.groupby('ind_code'):
        mapping[ind_code] = group['stock_code'].tolist()

    list_path = IND_DIR / 'industry_list.parquet'
    ind_df = pd.read_parquet(list_path)
    name_map = dict(zip(ind_df['ts_code'], ind_df['name']))

    # 只保留≥15只股票的行业
    mapping = {k: v for k, v in mapping.items() if len(v) >= 15}
    print(f"  有效行业数: {len(mapping)} (≥15只), 成分股总数: {sum(len(v) for v in mapping.values())}")
    return mapping, name_map


# ═══════════════════════════════════════════
# 二、宽度2计算（涨跌占比定义）
# ═══════════════════════════════════════════

def compute_breadth2(market_df, ind_members):
    """
    对每个行业，计算每日宽度2（当日上涨个股占比 pctChg>0）。
    返回 DataFrame: date, ind_code, breadth2, avg_pct, total_amount, stock_n
    """
    print("计算宽度2（涨跌占比定义）...")
    records = []

    for ind_code, stock_codes in ind_members.items():
        con_market = market_df[market_df['code'].isin(stock_codes)].copy()
        if con_market.empty:
            continue

        con_market['is_rising'] = (con_market['pctChg'] > 0).astype(int)

        daily = con_market.groupby('date').agg(
            avg_pct=('pctChg', 'mean'),
            total_amount=('amount', 'sum'),
            stock_n=('code', 'nunique'),
            breadth2=('is_rising', 'mean'),
        ).reset_index()

        daily['ind_code'] = ind_code
        records.append(daily)

    if not records:
        return pd.DataFrame()

    result = pd.concat(records, ignore_index=True)
    result = result.sort_values(['ind_code', 'date']).reset_index(drop=True)
    print(f"  行业日频数据: {len(result)} 行, {result['ind_code'].nunique()} 行业, "
          f"日期 {result['date'].min()} ~ {result['date'].max()}")
    return result


# ═══════════════════════════════════════════
# 三、收益计算框架
# ═══════════════════════════════════════════

def compute_returns(market_df, ind_members, event_df):
    """
    给事件表计算5/10/20日行业收益和全市场基准收益。
    超额 = 行业收益 - 全市场收益。
    """
    # 全市场日频收益（等权）
    market_daily = market_df.groupby('date')['pctChg'].mean().reset_index()
    market_daily.columns = ['date', 'market_avg_pct']
    market_daily = market_daily.sort_values('date')
    for n in [5, 10, 20]:
        market_daily[f'market_ret_{n}'] = market_daily['market_avg_pct'].rolling(n).sum().shift(-n)

    # 行业日频收益（等权）
    code_ind_map = {}
    for ind_code, stocks in ind_members.items():
        for s in stocks:
            code_ind_map[s] = ind_code

    ind_market = market_df[market_df['code'].isin(code_ind_map.keys())].copy()
    ind_market['ind_code'] = ind_market['code'].map(code_ind_map)
    ind_ret_daily = ind_market.groupby(['ind_code', 'date'])['pctChg'].mean().reset_index()
    ind_ret_daily.columns = ['ind_code', 'date', 'ind_avg_pct']
    ind_ret_daily = ind_ret_daily.sort_values(['ind_code', 'date'])
    for n in [5, 10, 20]:
        ind_ret_daily[f'ind_ret_{n}'] = ind_ret_daily.groupby('ind_code')['ind_avg_pct'].transform(
            lambda x: x.rolling(n).sum().shift(-n)
        )

    # 合并到事件表
    result = event_df.copy()
    result = result.merge(
        ind_ret_daily[['ind_code', 'date', 'ind_ret_5', 'ind_ret_10', 'ind_ret_20']],
        on=['ind_code', 'date'], how='left'
    )
    result = result.merge(
        market_daily[['date', 'market_ret_5', 'market_ret_10', 'market_ret_20']],
        on='date', how='left'
    )

    for n in [5, 10, 20]:
        result[f'excess_{n}'] = result[f'ind_ret_{n}'] - result[f'market_ret_{n}']

    return result


# ═══════════════════════════════════════════
# 四、事件扫描
# ═══════════════════════════════════════════

def scan_expansion_events(ind_daily, min_days=3):
    """
    扫描所有"宽度2连续≥min_days天扩张"事件。
    扩张 = 当日宽度2 > 前一日宽度2。
    同一行业同一扩张期只取扩张终点（最后一天）。
    
    返回 DataFrame: ind_code, date, expansion_days, breadth2_start, breadth2_end
    """
    print(f"扫描宽度2连续≥{min_days}天扩张事件...")
    events = []

    for ind_code in ind_daily['ind_code'].unique():
        hist = ind_daily[ind_daily['ind_code'] == ind_code].sort_values('date').reset_index(drop=True)
        if len(hist) < 25:
            continue

        in_expansion = False
        expansion_start_idx = None

        for i in range(1, len(hist)):
            bw_today = hist.iloc[i]['breadth2']
            bw_yesterday = hist.iloc[i-1]['breadth2']

            if bw_today > bw_yesterday:
                # 扩张延续
                if not in_expansion:
                    # 新扩张开始
                    in_expansion = True
                    expansion_start_idx = i
                # 继续扩张，不做记录（等扩张结束再记录）
            else:
                # 扩张结束（或根本没在扩张）
                if in_expansion:
                    expansion_days = i - expansion_start_idx
                    if expansion_days >= min_days:
                        # 扩张终点 = 前一天（i是扩张结束后的第一天）
                        end_idx = i - 1
                        events.append({
                            'ind_code': ind_code,
                            'date': hist.iloc[end_idx]['date'],
                            'expansion_days': expansion_days,
                            'breadth2_start': hist.iloc[expansion_start_idx - 1]['breadth2'],
                            'breadth2_end': hist.iloc[end_idx]['breadth2'],
                            'breadth2_pre20_mean': hist.iloc[expansion_start_idx - 20:expansion_start_idx - 1]['breadth2'].mean()
                                if expansion_start_idx >= 20 else np.nan,
                        })
                    in_expansion = False
                    expansion_start_idx = None

        # 处理文件末尾仍在扩张的情况
        if in_expansion:
            expansion_days = len(hist) - expansion_start_idx
            if expansion_days >= min_days:
                end_idx = len(hist) - 1
                events.append({
                    'ind_code': ind_code,
                    'date': hist.iloc[end_idx]['date'],
                    'expansion_days': expansion_days,
                    'breadth2_start': hist.iloc[expansion_start_idx - 1]['breadth2'],
                    'breadth2_end': hist.iloc[end_idx]['breadth2'],
                    'breadth2_pre20_mean': hist.iloc[expansion_start_idx - 20:expansion_start_idx - 1]['breadth2'].mean()
                        if expansion_start_idx >= 20 else np.nan,
                })

    df = pd.DataFrame(events)
    if df.empty:
        print("  未检测到任何扩张事件！")
        return df

    print(f"  扩张事件总数: {len(df)}")
    print(f"  日均事件数: {len(df) / df['date'].nunique():.2f}")
    print(f"  平均扩张天数: {df['expansion_days'].mean():.1f}")
    print(f"  平均宽度2增幅: {(df['breadth2_end'] - df['breadth2_start']).mean()*100:.1f}pp")
    return df


def scan_cold_zone_events(ind_daily, cold_threshold=0.30):
    """
    扫描所有"前20日宽度2均值≤cold_threshold"的行业×日期。
    这是不加任何扩张要求的纯冷区对照。
    """
    print(f"扫描冷区行业×日期（前20日均宽≤{cold_threshold*100:.0f}%）...")
    events = []

    for ind_code in ind_daily['ind_code'].unique():
        hist = ind_daily[ind_daily['ind_code'] == ind_code].sort_values('date').reset_index(drop=True)
        if len(hist) < 25:
            continue

        for i in range(20, len(hist)):
            bw20 = hist.iloc[i-20:i]['breadth2'].mean()
            if bw20 <= cold_threshold:
                events.append({
                    'ind_code': ind_code,
                    'date': hist.iloc[i]['date'],
                    'breadth2_pre20_mean': bw20,
                    'breadth2_today': hist.iloc[i]['breadth2'],
                })

    df = pd.DataFrame(events)
    if not df.empty:
        print(f"  冷区行业×日期总数: {len(df)}")
        print(f"  日均冷区行业数: {len(df) / df['date'].nunique():.2f}")
    return df


# ═══════════════════════════════════════════
# 五、对比分析
# ═══════════════════════════════════════════

def compare_groups(expansion_df, cold_expansion_df, cold_df, all_ind_daily, name_map):
    """
    四组对比:
    A. 纯扩张事件（无冷区过滤）
    B. 冷区+扩张
    C. 纯冷区（无扩张要求）
    D. 全量基准（所有行业×日期的均值超额）
    """
    print("\n" + "=" * 70)
    print("宽度2连续扩张事件 · 纯事件驱动回测")
    print("=" * 70)
    print("宽度2定义：行业内当日上涨个股占比 (pctChg>0).mean()")
    print("")

    groups = {}

    # A组：纯扩张
    if not expansion_df.empty and 'excess_20' in expansion_df.columns:
        valid = expansion_df.dropna(subset=['excess_20'])
        groups['A_纯扩张'] = valid

    # B组：冷区+扩张
    if not cold_expansion_df.empty and 'excess_20' in cold_expansion_df.columns:
        valid = cold_expansion_df.dropna(subset=['excess_20'])
        groups['B_冷区+扩张'] = valid

    # C组：纯冷区
    if not cold_df.empty and 'excess_20' in cold_df.columns:
        valid = cold_df.dropna(subset=['excess_20'])
        groups['C_纯冷区'] = valid

    # ── 打印各组统计 ──
    print("一、各组样本量与超额收益")
    print("-" * 70)
    print(f"{'组别':>12s} | {'样本数':>8s} | {'5日超额':>10s} | {'10日超额':>10s} | {'20日超额':>10s} | {'20日胜率':>10s} | {'20日IR':>10s} | {'t值':>10s}")
    print("-" * 70)

    for name, g in groups.items():
        n = len(g)
        if n == 0:
            print(f"{name:>12s} | {0:>8d} | — | — | — | — | — | —")
            continue

        e5 = g['excess_5'].mean() if 'excess_5' in g.columns else None
        e10 = g['excess_10'].mean() if 'excess_10' in g.columns else None
        e20 = g['excess_20'].mean()
        win = (g['excess_20'] > 0).mean() * 100
        std = g['excess_20'].std()
        ir = e20 / std if std > 0 else 0
        t_val, p_val = stats.ttest_1samp(g['excess_20'], 0)

        print(f"{name:>12s} | {n:>8d} | {e5:+.2f}% | {e10:+.2f}% | {e20:+.2f}% | {win:.0f}% | {ir:+.2f} | {t_val:+.2f}(p={p_val:.3f})")

    # ── A vs B vs C 对比 ──
    print("")
    print("二、关键对比：扩张是否有增量（A vs C）")
    print("-" * 70)

    if 'A_纯扩张' in groups and 'C_纯冷区' in groups:
        a = groups['A_纯扩张']
        c = groups['C_纯冷区']
        print(f"  A(纯扩张) 20日超额: {a['excess_20'].mean():+.2f}% (n={len(a)})")
        print(f"  C(纯冷区) 20日超额: {c['excess_20'].mean():+.2f}% (n={len(c)})")
        diff = a['excess_20'].mean() - c['excess_20'].mean()
        print(f"  差异(A-C): {diff:+.2f}%")
        if len(a) > 5 and len(c) > 5:
            t_val, p_val = stats.ttest_ind(a['excess_20'], c['excess_20'])
            print(f"  t检验: t={t_val:+.2f}, p={p_val:.3f} {'✅显著' if p_val < 0.05 else '❌不显著'}")

    if 'B_冷区+扩张' in groups and 'C_纯冷区' in groups:
        b = groups['B_冷区+扩张']
        c = groups['C_纯冷区']
        print(f"  B(冷区+扩张) 20日超额: {b['excess_20'].mean():+.2f}% (n={len(b)})")
        print(f"  C(纯冷区)    20日超额: {c['excess_20'].mean():+.2f}% (n={len(c)})")
        diff = b['excess_20'].mean() - c['excess_20'].mean()
        print(f"  差异(B-C): {diff:+.2f}%")
        if len(b) > 5 and len(c) > 5:
            t_val, p_val = stats.ttest_ind(b['excess_20'], c['excess_20'])
            print(f"  t检验: t={t_val:+.2f}, p={p_val:.3f} {'✅显著' if p_val < 0.05 else '❌不显著'}")

    # ── 跨阶段稳定性 ──
    print("")
    print("三、跨阶段稳定性（20日超额）")
    print("-" * 70)

    stages = [
        ('2020-2021 结构牛', '2020-01-01', '2021-12-31'),
        ('2022-2024 震荡熊', '2022-01-01', '2024-09-23'),
        ('2024后 政策牛',   '2024-09-24', '2026-12-31'),
    ]

    print(f"{'阶段':>25s} | ", end="")
    for name in groups:
        print(f"{name:>12s} | ", end="")
    print("")
    print("-" * 100)

    for stage_name, start, end in stages:
        start_dt = pd.Timestamp(start)
        end_dt = pd.Timestamp(end)
        print(f"{stage_name:>25s} | ", end="")
        for name, g in groups.items():
            stage_g = g[(g['date'] >= start_dt) & (g['date'] <= end_dt)]
            if len(stage_g) > 0:
                e20 = stage_g['excess_20'].mean()
                n = len(stage_g)
                print(f"{e20:+.2f}%({n}) | ", end="")
            else:
                print(f"     —     | ", end="")
        print("")

    # ── 扩张天数分档 ──
    print("")
    print("四、扩张天数分档（A组细分）")
    print("-" * 70)

    if 'A_纯扩张' in groups:
        a = groups['A_纯扩张']
        for days_threshold in [3, 4, 5, 7]:
            subset = a[a['expansion_days'] >= days_threshold]
            if len(subset) > 0:
                e20 = subset['excess_20'].mean()
                win = (subset['excess_20'] > 0).mean() * 100
                n = len(subset)
                print(f"  ≥{days_threshold}天扩张: 20日超额={e20:+.2f}%, 胜率={win:.0f}%, n={n}")
            else:
                print(f"  ≥{days_threshold}天扩张: 无数据")

    # ── 均值回归检查 ──
    print("")
    print("五、均值回归检查（超额来源分析）")
    print("-" * 70)

    if 'C_纯冷区' in groups:
        c = groups['C_纯冷区']
        if 'ind_ret_20' in c.columns:
            print(f"  C(纯冷区) 行业20日绝对收益: {c['ind_ret_20'].mean():+.2f}%")
            print(f"  C(纯冷区) 市场20日基准收益: {c['market_ret_20'].mean():+.2f}%")
            print(f"  → 冷区超额来自行业反弹({c['ind_ret_20'].mean():+.2f}%)减去市场基准({c['market_ret_20'].mean():+.2f}%)")

    if 'A_纯扩张' in groups:
        a = groups['A_纯扩张']
        if 'ind_ret_20' in a.columns:
            print(f"  A(纯扩张) 行业20日绝对收益: {a['ind_ret_20'].mean():+.2f}%")
            print(f"  A(纯扩张) 市场20日基准收益: {a['market_ret_20'].mean():+.2f}%")

    # ── 扩张前行业跌幅 ──
    if 'A_纯扩张' in groups:
        a = groups['A_纯扩张']
        if 'avg_pct' in a.columns:
            # 计算扩张前20日行业累计跌幅
            # 需要从ind_daily中回查
            print("")
            print("六、扩张前行业状态")
            print("-" * 70)
            
            # 合并扩张事件的breadth2_pre20_mean
            if 'breadth2_pre20_mean' in a.columns:
                pre_mean = a['breadth2_pre20_mean'].dropna()
                if len(pre_mean) > 0:
                    print(f"  A(纯扩张) 扩张前20日宽度2均值: {pre_mean.mean()*100:.1f}%")
                    # 分档
                    for threshold in [0.20, 0.30, 0.50, 0.70]:
                        subset = a[a['breadth2_pre20_mean'] <= threshold].dropna(subset=['excess_20'])
                        if len(subset) > 0:
                            e20 = subset['excess_20'].mean()
                            print(f"    前均宽≤{threshold*100:.0f}%: 20日超额={e20:+.2f}% (n={len(subset)})")

    print("")
    print("=" * 70)
    print("结论：宽度2连续≥3天扩张事件，是否有超额收益？")
    print("=" * 70)

    if 'A_纯扩张' in groups:
        a = groups['A_纯扩张']
        e20 = a['excess_20'].mean()
        win = (a['excess_20'] > 0).mean() * 100
        t_val, p_val = stats.ttest_1samp(a['excess_20'], 0)
        sig = "✅显著" if p_val < 0.05 else "❌不显著"

        print(f"  A(纯扩张) 20日超额: {e20:+.2f}%, 胜率: {win:.0f}%, t={t_val:+.2f} ({sig})")

        # 对比C组
        if 'C_纯冷区' in groups:
            c = groups['C_纯冷区']
            c_e20 = c['excess_20'].mean()
            diff = e20 - c_e20
            print(f"  C(纯冷区) 20日超额: {c_e20:+.2f}%")
            print(f"  扩张增量(A-C): {diff:+.2f}%")
            if abs(diff) < 0.5:
                print(f"  → 扩张的增量效果极小，超额主要来自冷区均值回归")
            elif diff > 0:
                print(f"  → 扩张有正向增量，但需检查是否仍为均值回归驱动")
            else:
                print(f"  → 扩张反而降低了超额，可能是追涨信号")


# ═══════════════════════════════════════════
# 六、主流程
# ═══════════════════════════════════════════

def main():
    market_df = load_market_data()
    ind_members, name_map = load_industry_members()

    # 计算宽度2
    ind_daily = compute_breadth2(market_df, ind_members)

    # A组：纯扩张事件（≥3天，无冷区过滤）
    expansion_df = scan_expansion_events(ind_daily, min_days=3)

    # B组：冷区+扩张（前20日均宽≤30% + 连续≥3天扩张）
    if not expansion_df.empty:
        cold_expansion_df = expansion_df[
            expansion_df['breadth2_pre20_mean'].notna() &
            (expansion_df['breadth2_pre20_mean'] <= 0.30)
        ].copy()
        print(f"  冷区+扩张事件: {len(cold_expansion_df)}")
    else:
        cold_expansion_df = pd.DataFrame()

    # C组：纯冷区（前20日均宽≤30%，无扩张要求）
    cold_df = scan_cold_zone_events(ind_daily, cold_threshold=0.30)

    # 给各组计算超额收益
    print("\n计算各组超额收益...")
    if not expansion_df.empty:
        expansion_df = compute_returns(market_df, ind_members, expansion_df)
    if not cold_expansion_df.empty:
        cold_expansion_df = compute_returns(market_df, ind_members, cold_expansion_df)
    if not cold_df.empty:
        cold_df = compute_returns(market_df, ind_members, cold_df)

    # 对比分析
    compare_groups(expansion_df, cold_expansion_df, cold_df, ind_daily, name_map)


if __name__ == '__main__':
    main()
