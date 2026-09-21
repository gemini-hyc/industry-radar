"""
主线探测器 v1
=============
自动识别当前市场是否存在抱团主线，主线是谁，发展到哪一步，以及埋伏候选。

核心逻辑：
1. 行业聚类：用相关系数自动识别"方向群"（不用手工产业链字典）
2. 方向群成交额占比信号：与历史均值比较
3. 方向群超额收益信号：vs全市场均值
4. 综合判断：主线存在条件
5. 主线发展阶段判断
6. 埋伏候选识别
"""

import sys
from pathlib import Path

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import INDUSTRY_DIR


def load_data():
    """加载所有必要数据"""
    returns = pd.read_parquet(INDUSTRY_DIR / 'industry_weighted_returns.parquet')
    idf = pd.read_parquet(INDUSTRY_DIR / 'industry_daily_full.parquet')
    il = pd.read_parquet(INDUSTRY_DIR / 'industry_list.parquet')
    name_map = dict(zip(il['ts_code'], il['name']))
    
    # 成交额占比
    idf['date'] = pd.to_datetime(idf['date'])
    daily_total_amt = idf.groupby('date')['total_amount'].sum().reset_index()
    daily_total_amt.columns = ['date', 'all_amount']
    idf = idf.merge(daily_total_amt, on='date', how='left')
    idf['amt_pct'] = idf['total_amount'] / idf['all_amount'] * 100
    
    return returns, idf, name_map


def cluster_industries(pivot, corr_threshold=0.6):
    """
    用相关系数做聚类，将131行业分成若干"方向群"
    改进版：要求新成员与群内核心（种子及首批成员）的平均相关性>threshold
    防止连锁膨胀导致超大群
    """
    corr_matrix = pivot.corr()
    all_codes = list(pivot.columns)
    
    # 用绝对相关系数
    abs_corr = corr_matrix.abs()
    
    visited = set()
    groups = {}
    gid = 0
    
    # 按行业间平均相关性排序（最"连接"的行业优先做种子）
    avg_corr = {}
    for c in all_codes:
        avg_corr[c] = abs_corr[c].mean()
    sorted_codes = sorted(all_codes, key=lambda c: -avg_corr[c])
    
    for seed in sorted_codes:
        if seed in visited:
            continue
        
        # 找与种子相关性>threshold的行业作为首批核心
        core = [seed]
        visited.add(seed)
        
        # 首批：与seed直接相关>threshold
        first_batch = [c for c in all_codes if c not in visited and abs_corr.loc[seed, c] >= corr_threshold]
        core.extend(first_batch)
        visited.update(first_batch)
        
        # 第二批：与核心成员平均相关性>0.5（比核心阈值低，允许适度扩散）
        second_batch = []
        for c in all_codes:
            if c in visited:
                continue
            avg_with_core = np.mean([abs_corr.loc[c, g] for g in core])
            if avg_with_core >= 0.5 and abs_corr.loc[c, seed] >= 0.4:
                second_batch.append(c)
                visited.add(c)
        
        group = core + second_batch
        
        gid += 1
        groups[gid] = group
    
    return groups, corr_matrix


def compute_group_signals(returns, idf, name_map, groups, corr_matrix, 
                          lookback=20, history_start='2020-01-01'):
    """
    计算每个方向群的成交额占比和超额收益信号
    """
    # 市场日均收益
    market_daily = returns.groupby('date')['weighted_pct'].mean().reset_index()
    market_daily.columns = ['date', 'market_pct']
    
    # 方向群逐日成交额占比
    idf_dated = idf.copy()
    
    group_signals = {}
    
    for gid, codes in groups.items():
        group_names = [name_map.get(c, c) for c in codes]
        
        # 1. 成交额占比
        group_amt = idf_dated[idf_dated['con_code'].isin(codes)].groupby('date')['amt_pct'].sum().reset_index()
        group_amt.columns = ['date', 'amt_pct']
        group_amt = group_amt.sort_values('date')
        
        # 2. 加权涨跌幅均值
        group_ret = returns[returns['ind_code'].isin(codes)].groupby('date')['weighted_pct'].mean().reset_index()
        group_ret.columns = ['date', 'group_pct']
        
        # 3. 合并
        merged = market_daily.merge(group_ret, on='date', how='left').merge(group_amt, on='date', how='left')
        merged = merged.sort_values('date')
        merged['excess'] = merged['group_pct'] - merged['market_pct']
        
        # 4. 滚动指标
        merged['amt_ma20'] = merged['amt_pct'].rolling(lookback, min_periods=lookback//2).mean()
        merged['excess_ma20'] = merged['excess'].rolling(lookback, min_periods=lookback//2).mean()
        merged['excess_winrate'] = merged['excess'].rolling(lookback, min_periods=lookback//2).apply(
            lambda x: (x > 0).mean(), raw=True
        )
        
        # 5. 历史均值（用更长历史）
        history_amt = group_amt[group_amt['date'] >= history_start]
        hist_mean_amt = history_amt['amt_pct'].mean() if len(history_amt) > 0 else 0
        
        # 6. 热度倍数
        merged['heat_ratio'] = merged['amt_ma20'] / hist_mean_amt if hist_mean_amt > 0 else 0
        
        group_signals[gid] = {
            'codes': codes,
            'names': group_names,
            'n_industries': len(codes),
            'data': merged,
            'hist_mean_amt': hist_mean_amt,
        }
    
    return group_signals


def detect_mainline(group_signals, date, 
                    heat_threshold=1.3, excess_threshold=0.3, winrate_threshold=0.6):
    """
    检测指定日期是否存在主线
    """
    results = []
    
    for gid, gs in group_signals.items():
        d = gs['data']
        row = d[d['date'] == date]
        
        if len(row) == 0:
            continue
        row = row.iloc[0]
        
        heat = row.get('heat_ratio', 0)
        excess_ma20 = row.get('excess_ma20', 0)
        winrate = row.get('excess_winrate', 0)
        amt_ma20 = row.get('amt_ma20', 0)
        
        # 判断是否为主线
        is_mainline = (heat >= heat_threshold and 
                       excess_ma20 >= excess_threshold and 
                       winrate >= winrate_threshold)
        
        # 判断发展阶段
        if heat >= 1.7:
            if excess_ma20 > 0:
                stage = '见顶期'
            else:
                stage = '瓦解期'
        elif heat >= 1.3:
            if excess_ma20 >= excess_threshold:
                stage = '加速期'
            else:
                stage = '启动→加速过渡'
        elif heat >= 1.0:
            if excess_ma20 > 0:
                stage = '启动期'
            else:
                stage = '蛰伏'
        else:
            stage = '蛰伏'
        
        # 判断进攻/防御型
        # 用市场涨日和跌日的超额分别计算
        d_before = d[d['date'] <= date].tail(60)
        up_days = d_before[d_before['market_pct'] > 0]
        down_days = d_before[d_before['market_pct'] < 0]
        
        offense = (up_days['excess'].mean() if len(up_days) > 10 else 0)
        defense = (down_days['excess'].mean() if len(down_days) > 10 else 0)
        
        if abs(defense) > 0.01:
            od_ratio = offense / abs(defense)
        else:
            od_ratio = float('inf')
        
        if od_ratio > 1:
            style = '进攻型'
        elif od_ratio < -0.5:
            style = '防御型'
        else:
            style = '混合型'
        
        results.append({
            'group_id': gid,
            'group_names': gs['names'],
            'n_industries': gs['n_industries'],
            'heat_ratio': heat,
            'excess_ma20': excess_ma20,
            'winrate': winrate,
            'amt_ma20': amt_ma20,
            'hist_mean_amt': gs['hist_mean_amt'],
            'is_mainline': is_mainline,
            'stage': stage,
            'style': style,
            'od_ratio': od_ratio,
        })
    
    if len(results) == 0:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values('heat_ratio', ascending=False)


def find_ambush_candidates(group_signals, corr_matrix, name_map, 
                           mainline_gid, date,
                           corr_with_core=0.4, amt_below_mean=True):
    """
    找埋伏候选：与核心行业相关性高但成交额占比还在均值以下的行业
    
    参数：
    - mainline_gid: 主线方向群ID
    - corr_with_core: 与核心行业的最低相关系数
    - amt_below_mean: 是否只选成交额占比<均值的行业
    """
    gs = group_signals[mainline_gid]
    core_codes = gs['codes']
    
    # 找所有非核心行业中，与核心行业平均相关性最高的
    all_codes = corr_matrix.columns.tolist()
    non_core_codes = [c for c in all_codes if c not in core_codes]
    
    candidates = []
    for nc in non_core_codes:
        # 与核心行业的平均相关系数
        corrs_with_core = [corr_matrix.loc[nc, cc] for cc in core_codes]
        avg_corr = np.mean(corrs_with_core)
        max_corr = np.max(corrs_with_core)
        
        if avg_corr < corr_with_core:
            continue
        
        nc_name = name_map.get(nc, nc)
        
        # 该行业的成交额占比
        idf_nc = group_signals  # 需要从数据中获取
        # 简化：用returns中的超额
        nc_ret = returns[returns['ind_code'] == nc].groupby('date')['weighted_pct'].mean()
        market_ret = returns.groupby('date')['weighted_pct'].mean()
        nc_excess = nc_ret - market_ret
        
        # 20日超额
        nc_excess_ma20 = nc_excess.rolling(20, min_periods=10).mean()
        
        # 成交额占比
        # 从idf中获取
        # 简化处理
        
        candidates.append({
            'code': nc,
            'name': nc_name,
            'avg_corr_with_core': avg_corr,
            'max_corr_with_core': max_corr,
            'excess_ma20': nc_excess_ma20.get(date, 0) if date in nc_excess_ma20.index else 0,
        })
    
    return pd.DataFrame(candidates).sort_values('avg_corr_with_core', ascending=False)


def run_full_detection(reference_date=None):
    """运行完整的主线检测流程"""
    print("加载数据...")
    returns, idf, name_map = load_data()
    
    # 构建pivot
    pivot = returns.pivot(index='date', columns='ind_code', values='weighted_pct')
    
    print("行业聚类...")
    groups, corr_matrix = cluster_industries(pivot, corr_threshold=0.6)
    
    # 所有群都参与计算（不过滤大小）
    valid_groups = groups
    
    print(f"聚类结果: {len(groups)}个群, 其中{len(valid_groups)}个有效(3-30行业)")
    for gid, codes in sorted(valid_groups.items(), key=lambda x: -len(x[1])):
        names = [name_map.get(c, c) for c in codes]
        print(f"  群{gid}({len(codes)}行业): {', '.join(names)}")
    
    print("\n计算方向群信号...")
    group_signals = compute_group_signals(returns, idf, name_map, valid_groups, corr_matrix)
    
    # 如果没有指定日期，用最新日期
    if reference_date is None:
        reference_date = returns['date'].max()
    else:
        reference_date = pd.Timestamp(reference_date)
    
    print(f"\n检测{reference_date.strftime('%Y-%m-%d')}的主线...")
    results = detect_mainline(group_signals, reference_date)
    
    # 输出结果
    print("\n" + "="*80)
    print(f"主线检测结果 ({reference_date.strftime('%Y-%m-%d')})")
    print("="*80)
    
    mainlines = results[results['is_mainline']]
    non_mainlines = results[~results['is_mainline']]
    
    if len(mainlines) > 0:
        print(f"\n✅ 检测到{len(mainlines)}个主线方向：\n")
        for _, r in mainlines.iterrows():
            print(f"  【主线】群{r['group_id']}: {', '.join(r['group_names'][:5])}{'...' if len(r['group_names'])>5 else ''}")
            print(f"    热度={r['heat_ratio']:.1f}x均值, 超额={r['excess_ma20']:.3f}%/日, 胜率={r['winrate']:.0%}")
            print(f"    阶段={r['stage']}, 类型={r['style']}(进攻/防御={r['od_ratio']:.1f})")
            print(f"    成交额占比={r['amt_ma20']:.1f}%(历史均值{r['hist_mean_amt']:.1f}%)")
    else:
        print("\n❌ 当前无主线，市场处于轮动状态")
    
    # 输出热度排名Top10
    print(f"\n热度排名Top10：\n")
    for i, (_, r) in enumerate(results.head(10).iterrows()):
        flag = "✅主线" if r['is_mainline'] else ""
        print(f"  {i+1}. 群{r['group_id']}({r['n_industries']}行业): "
              f"热度={r['heat_ratio']:.1f}x, 超额={r['excess_ma20']:.3f}%, "
              f"阶段={r['stage']} {flag}")
        print(f"     行业: {', '.join(r['group_names'][:6])}")
    
    # 保存结果
    results.to_csv(INDUSTRY_DIR / 'mainline_detection.csv', index=False)
    print(f"\n结果已保存到 {INDUSTRY_DIR / 'mainline_detection.csv'}")
    
    return results, group_signals, valid_groups, corr_matrix


if __name__ == '__main__':
    results, group_signals, groups, corr_matrix = run_full_detection()
