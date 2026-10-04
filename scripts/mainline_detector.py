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
from src.utils.provenance import save_dataframe


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
                          lookback=20, history_start='2020-01-01',
                          hist_min_periods=None):
    """
    计算每个方向群的成交额占比和超额收益信号

    ⚠️ hist_mean_amt 为「截至当日」的扩张窗口均值（PIT 正确）。
       原实现取 [history_start, 最后一天] 的**全样本均值**，等于把未来数据用于历史日期的分母，
       对历史日期构成前视偏差；已在 2026-10-03 修正（见 docs/action-items.md D12）。
       当参考日 = 数据最后一天时，扩张均值与全样本均值数值相同 ⇒ 本修正对日更产物零影响。
    """
    if hist_min_periods is None:
        hist_min_periods = lookback
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
        
        # 5. 历史均值基准 —— ⚠️ PIT 修正（D12）：改为「截至当日」的扩张窗口均值。
        #    原实现取 [history_start, 最后一天] 的全样本均值 ⇒ 历史日期的分母里混入了未来数据。
        hist = group_amt[group_amt['date'] >= pd.Timestamp(history_start)].sort_values('date')
        hist = hist[['date', 'amt_pct']].copy()
        hist['hist_mean_amt'] = hist['amt_pct'].expanding(min_periods=hist_min_periods).mean()
        merged = merged.merge(hist[['date', 'hist_mean_amt']], on='date', how='left')
        # 基准尚未成型的早期日期无值（NaN）→ 记 0，等价于"热度不可判定"
        merged['hist_mean_amt'] = merged['hist_mean_amt'].fillna(0.0)

        # 6. 热度倍数
        merged['heat_ratio'] = np.where(
            merged['hist_mean_amt'] > 0,
            merged['amt_ma20'] / merged['hist_mean_amt'],
            0.0,
        )

        group_signals[gid] = {
            'codes': codes,
            'names': group_names,
            'n_industries': len(codes),
            'data': merged,
        }
    
    return group_signals


def _raw_is_mainline(row, heat_threshold, excess_threshold, winrate_threshold):
    """单日三条件判定（不含任何跨日约束）—— 连续性过滤的基元。

    抽成独立函数，是为了让「单日是否主线」与「历史上某天是否主线」用**同一把尺子**；
    否则历史日的判定口径会与当日漂移，连续性过滤就失去意义。
    """
    return bool(
        row.get('heat_ratio', 0) >= heat_threshold
        and row.get('excess_ma20', 0) >= excess_threshold
        and row.get('excess_winrate', 0) >= winrate_threshold
    )


def detect_mainline(group_signals, date,
                    heat_threshold=1.3, excess_threshold=0.3, winrate_threshold=0.6,
                    min_consecutive_days=3):
    """
    检测指定日期是否存在主线。

    ⚠️ 跨期稳定性约束（B1 / D2，2026-10-04 上线）：主线须**连续 >= min_consecutive_days 个交易日**
       满足三条件才成立。改动前无任何跨日约束，实测 **30.3% 的主线段只活 1 天**（单日孤噪）。

    🔑 **无前视**：判定 T 日只需 T 与其前 N-1 个交易日（均已发生），不触碰 T 之后的数据
       ⇒ 可直接进日更链路，无需等整段结束才敢确认。
       代价：每段**头 2 天不算主线**（与「回放后段内全保留」口径差 2 天/段，
       验收数字必须按本口径重算，不要沿用旧表）。

    回滚：传 `min_consecutive_days=1` 即完全恢复原行为。
    `is_mainline_raw` 永久保留纯三条件结果，供诊断与台账核对。
    """
    results = []

    for gid, gs in group_signals.items():
        d = gs['data'].reset_index(drop=True)
        locs = d.index[d['date'] == date]

        if len(locs) == 0:
            continue
        loc = locs[0]
        row = d.iloc[loc]

        heat = row.get('heat_ratio', 0)
        excess_ma20 = row.get('excess_ma20', 0)
        winrate = row.get('excess_winrate', 0)
        amt_ma20 = row.get('amt_ma20', 0)

        # 单日判定（三条件，与改动前逐字一致）
        is_mainline_raw = _raw_is_mainline(
            row, heat_threshold, excess_threshold, winrate_threshold)

        # 跨期稳定性：以当日结尾的连续主线天数
        consec_ok, consec_days = False, 0
        if min_consecutive_days <= 1:
            consec_ok = is_mainline_raw
            consec_days = 1 if is_mainline_raw else 0
        else:
            lo = loc - (min_consecutive_days - 1)
            if lo >= 0:
                flags = [_raw_is_mainline(r, heat_threshold, excess_threshold, winrate_threshold)
                         for _, r in d.iloc[lo: loc + 1].iterrows()]
                consec_ok = all(flags)
                for f in reversed(flags):          # 末尾连续天数
                    if not f:
                        break
                    consec_days += 1
            else:
                # 历史不足 N 个交易日 ⇒ 无法确认连续，一律不算主线（但记录已有天数）
                for _, r in d.iloc[0: loc + 1].iterrows():
                    if not _raw_is_mainline(r, heat_threshold, excess_threshold, winrate_threshold):
                        break
                    consec_days += 1

        is_mainline = is_mainline_raw and consec_ok

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
        
        # ⚠️ 修复（D14）：原实现把"样本不足"回退成 0 ⇒ 下跌日均值=0 ⇒ od_ratio=inf ⇒ 误判「进攻型」。
        #    改为：样本不足即标「未知」，不参与风格判定；样本充足时行为与原来逐字一致
        #    （含 abs(defense)≤0.01 ⇒ inf 的原语义保留）。
        if len(up_days) > 10 and len(down_days) > 10:
            offense = up_days['excess'].mean()
            defense = down_days['excess'].mean()

            if abs(defense) > 0.01:
                od_ratio = offense / abs(defense)
            else:
                od_ratio = float('inf')   # 下跌日超额≈0 ⇒ 比值发散（原语义保留）

            if od_ratio > 1:
                style = '进攻型'
            elif od_ratio < -0.5:
                style = '防御型'
            else:
                style = '混合型'
        else:
            # 上涨日或下跌日样本 <10 天：数据不足以判定风格，标「未知」
            od_ratio = float('nan')
            style = '未知'
        
        results.append({
            'group_id': gid,
            'group_names': gs['names'],
            'n_industries': gs['n_industries'],
            'heat_ratio': heat,
            'excess_ma20': excess_ma20,
            'winrate': winrate,
            'amt_ma20': amt_ma20,
            'hist_mean_amt': float(row.get('hist_mean_amt', 0)),
            'is_mainline': is_mainline,
            # —— B1/D2 新增：可审计字段（下游只读 is_mainline，这几列供诊断）——
            'is_mainline_raw': is_mainline_raw,      # 仅三条件，未过连续性门槛
            'consec_days': consec_days,             # 以当日结尾的连续主线天数
            'min_consecutive_days': min_consecutive_days,  # 生效阈值（产物自解释）
            'stage': stage,
            'style': style,
            'od_ratio': od_ratio,
        })
    
    if len(results) == 0:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values('heat_ratio', ascending=False)


def find_ambush_candidates(returns, group_signals, corr_matrix, name_map,
                           mainline_gid, date,
                           corr_with_core=0.4, amt_below_mean=True):
    """
    找埋伏候选：与核心行业相关性高但成交额占比还在均值以下的行业

    ⚠️ 当前【未被 run_full_detection 调用】—— 本功能尚未接入日更链路，
    其结果也不进 mainline_detection.csv。保留是因为它承载了"埋伏候选"的设计意图；
    是否接入、按什么口径接入见 docs/action-items.md D2。

    参数：
    - returns: 行业加权涨跌幅长表（含 date / ind_code / weighted_pct）。
               修复前此处漏传该参数却直接引用，函数一旦被调用即 NameError。
    - mainline_gid: 主线方向群ID
    - corr_with_core: 与核心行业的最低相关系数
    - amt_below_mean: 是否只选成交额占比<均值的行业（⚠️ 尚未实现，当前被忽略）
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

        # 该行业的成交额占比：尚未实现（amt_below_mean 因此被忽略）；
        # 当前只用"相对全市场等权的超额"
        nc_ret = returns[returns['ind_code'] == nc].groupby('date')['weighted_pct'].mean()
        market_ret = returns.groupby('date')['weighted_pct'].mean()
        nc_excess = nc_ret - market_ret
        
        # 20日超额
        nc_excess_ma20 = nc_excess.rolling(20, min_periods=10).mean()

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

    # 参考日：未指定则取数据最后一天
    if reference_date is None:
        reference_date = returns['date'].max()
    else:
        reference_date = pd.Timestamp(reference_date)

    # 构建pivot —— ⚠️ PIT 修正（D13）：聚类只允许使用 <= reference_date 的数据。
    #    原实现用全样本（含参考日之后）算相关系数矩阵，对「回测历史日期」构成前视偏差；
    #    生产时 reference_date = 数据最后一天 ⇒ 截断后与原来逐字节一致。
    pivot_all = returns.pivot(index='date', columns='ind_code', values='weighted_pct')
    pivot = pivot_all.loc[pivot_all.index <= reference_date]

    print("行业聚类...")
    groups, corr_matrix = cluster_industries(pivot, corr_threshold=0.6)
    
    # 所有群都参与计算：**仍无**最小成员数约束（D2 的另一半 —— 2026-10-04 主人只拍板了
    # 跨期稳定性那一半），因此仍会出现"1 个行业的方向群被判定为主线"；
    # 跨期稳定性约束已在 detect_mainline 内落地（min_consecutive_days=3）。
    valid_groups = groups

    print(f"聚类结果: {len(groups)}个群，全部参与计算（当前无最小成员数约束）")
    for gid, codes in sorted(valid_groups.items(), key=lambda x: -len(x[1])):
        names = [name_map.get(c, c) for c in codes]
        print(f"  群{gid}({len(codes)}行业): {', '.join(names)}")
    
    print("\n计算方向群信号...")
    group_signals = compute_group_signals(returns, idf, name_map, valid_groups, corr_matrix)
    
    print(f"\n检测{reference_date.strftime('%Y-%m-%d')}的主线...")
    results = detect_mainline(group_signals, reference_date)
    
    # 输出结果
    print("\n" + "="*80)
    print(f"主线检测结果 ({reference_date.strftime('%Y-%m-%d')})")
    print("="*80)
    
    mainlines = results[results['is_mainline']]
    non_mainlines = results[~results['is_mainline']]

    _mc = int(results['min_consecutive_days'].iloc[0]) if len(results) else 0
    print(f"\n主线口径：三条件 AND 连续 >= {_mc} 个交易日（B1/D2 跨期稳定性约束）")

    if len(mainlines) > 0:
        print(f"\n✅ 检测到{len(mainlines)}个主线方向：\n")
        for _, r in mainlines.iterrows():
            print(f"  【主线】群{r['group_id']}: {', '.join(r['group_names'][:5])}{'...' if len(r['group_names'])>5 else ''}")
            print(f"    热度={r['heat_ratio']:.1f}x均值, 超额={r['excess_ma20']:.3f}%/日, 胜率={r['winrate']:.0%}")
            print(f"    阶段={r['stage']}, 类型={r['style']}(进攻/防御={r['od_ratio']:.1f})")
            print(f"    成交额占比={r['amt_ma20']:.1f}%(历史均值{r['hist_mean_amt']:.1f}%)")
            print(f"    连续主线={int(r['consec_days'])} 天")
    else:
        print("\n❌ 当前无主线，市场处于轮动状态")

    # B1 诊断：单日三条件成立、但被连续性门槛挡下的方向群（改动前会被误判为「主线」）
    blocked = results[results['is_mainline_raw'] & ~results['is_mainline']]
    if len(blocked) > 0:
        print(f"\n⚠️ 有 {len(blocked)} 个方向群单日达标但连续性不足（未计为主线）：")
        for _, r in blocked.iterrows():
            print(f"    群{r['group_id']}({r['n_industries']}行业): 连续仅 {int(r['consec_days'])}/{_mc} 天")
    
    # 输出热度排名Top10
    print(f"\n热度排名Top10：\n")
    for i, (_, r) in enumerate(results.head(10).iterrows()):
        flag = "✅主线" if r['is_mainline'] else ""
        print(f"  {i+1}. 群{r['group_id']}({r['n_industries']}行业): "
              f"热度={r['heat_ratio']:.1f}x, 超额={r['excess_ma20']:.3f}%, "
              f"阶段={r['stage']} {flag}")
        print(f"     行业: {', '.join(r['group_names'][:6])}")
    
    # 保存结果
    written = save_dataframe(
        results, INDUSTRY_DIR / 'mainline_detection.csv',
        producer="scripts/mainline_detector.py",
        inputs=[INDUSTRY_DIR / 'industry_weighted_returns.parquet',
                INDUSTRY_DIR / 'industry_daily_full.parquet',
                INDUSTRY_DIR / 'industry_list.parquet'],
    )
    print(f"\n结果已保存到 {written}")

    return results, group_signals, valid_groups, corr_matrix


if __name__ == '__main__':
    results, group_signals, groups, corr_matrix = run_full_detection()
