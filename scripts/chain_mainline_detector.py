#!/usr/bin/env python3
"""chain_mainline_detector.py — 链版主线探测器（项目 B2：群定义换成已验证语义链）。

对应 `docs/plan/chain-veto-and-mainline-rebuild-plan.md` 项目 B2：
把 mainline 的"方向群"定义从"全市场自由聚类"换成"已验证语义链"。

口径（与 mainline_detector.py 逐字一致的部分）：
- `compute_group_signals`：成交额占比/超额/胜率算法、PIT 扩张窗口均值——口径相同
- `detect_mainline`：三条件 + 连续 ≥3 日 + 阶段判定 + 风格判定——口径相同
- 数据源、参考日、lookback=20 不变

唯一变化：
- `groups` 的来源从 `cluster_industries(pivot)` 改为**读已锁定的链名单 + 按方案 C 筛选**
- chain_id 替代 group_id；链名（来自 ETF 名）替代"群N"

方案 C 筛选规则：
1. 仅取 verified 链（`docs/plan/chain_veto_verification_latest.csv` 中 tier=='verified'）
2. 叠加 n_members ≥ 3 且 z_s3 > 0
3. 剔除成员不在 131 行业面板中的链（防止名单与面板错位）

⚠️ 名单后见之明（必读）：方案 C 用的是 2026-10-11 的验证结果 ⇒ 用本脚本回溯历史日期时，
   等于「用今天的名单看过去」。历史段主线数只能读作"在今天名单下会得到什么"，
   **不是**"当时真能发现什么"。详见
   `docs/plan/chain-mainline-backfill-diagnosis-2026-10-12.md` §10。

跑法：
    # 生产（写入 industry/chain_mainline_detection.csv，日更覆盖）
    /usr/bin/python3 scripts/chain_mainline_detector.py

    # 观察/测试（写到别处，不碰生产产物）
    /usr/bin/python3 scripts/chain_mainline_detector.py --out /path/to/out.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from src.paths import INDUSTRY_DIR  # noqa: E402

# 方案 C 的筛选依据：74 链否决权验证结果（项目 A 产物，2026-10-11）。
# 🔴 已从 _local_dev 固化进仓库 —— 生产链不得依赖沙箱文件（可能被清理）。
VETO_CSV = REPO / "docs" / "plan" / "chain_veto_verification_latest.csv"
PRIOR_CSV = REPO / "docs" / "plan" / "chain-prior-list.csv"
DAILY_RESONANCE_CSV = INDUSTRY_DIR / "chain_resonance_daily.csv"
# 生产产物：固定路径、日更覆盖（列契约测试 tests/_common.py 按此路径校验 22 列）
DEFAULT_OUT = INDUSTRY_DIR / "chain_mainline_detection.csv"


def load_chain_groups(verified_csv: str, prior_csv: str, all_codes):
    """按方案 C 筛链 → {chain_id: [ind_code]}。

    方案 C: verified + n_members>=3 + z_s3>0
    """
    v = pd.read_csv(verified_csv, comment='#')
    v = v[(v.tier == 'verified') & (v.n_members >= 3) & (v.z_s3 > 0)]
    keep_ids = set(v.chain_id.tolist())
    print(f"· 方案 C 筛选：verified {len(v)} 条 → n>=3 且 z>0 保留 {len(keep_ids)} 条")

    text = Path(prior_csv).read_text(encoding='utf-8')
    body = '\n'.join(ln for ln in text.splitlines() if not ln.lstrip().startswith('#'))
    rows = list(csv.DictReader(body.splitlines()))
    known = set(all_codes)
    chains: dict = {}
    skipped = 0
    for r in rows:
        cid = r.get('chain_id', '?')
        if cid not in keep_ids:
            continue
        code = r.get('ind_code', '').strip()
        if code not in known:
            skipped += 1
            continue
        chains.setdefault(cid, []).append(code)
    print(f"· 名单加载：{len(chains)} 条链（成员不在面板内被丢 {skipped} 个）")
    return chains


def compute_group_signals(returns, idf, name_map, groups, lookback=20, history_start='2020-01-01',
                          hist_min_periods=None):
    """与 mainline_detector.compute_group_signals 逐字等同（含 PIT 修正 D12）。"""
    if hist_min_periods is None:
        hist_min_periods = lookback
    market_daily = returns.groupby('date')['weighted_pct'].mean().reset_index()
    market_daily.columns = ['date', 'market_pct']

    idf_dated = idf.copy()
    group_signals = {}

    for cid, codes in groups.items():
        group_names = [name_map.get(c, c) for c in codes]
        group_amt = idf_dated[idf_dated['con_code'].isin(codes)].groupby('date')['amt_pct'].sum().reset_index()
        group_amt.columns = ['date', 'amt_pct']
        group_amt = group_amt.sort_values('date')
        group_ret = returns[returns['ind_code'].isin(codes)].groupby('date')['weighted_pct'].mean().reset_index()
        group_ret.columns = ['date', 'group_pct']
        merged = market_daily.merge(group_ret, on='date', how='left').merge(group_amt, on='date', how='left')
        merged = merged.sort_values('date')
        merged['excess'] = merged['group_pct'] - merged['market_pct']
        merged['amt_ma20'] = merged['amt_pct'].rolling(lookback, min_periods=lookback//2).mean()
        merged['excess_ma20'] = merged['excess'].rolling(lookback, min_periods=lookback//2).mean()
        merged['excess_winrate'] = merged['excess'].rolling(lookback, min_periods=lookback//2).apply(
            lambda x: (x > 0).mean(), raw=True)
        hist = group_amt[group_amt['date'] >= pd.Timestamp(history_start)].sort_values('date')
        hist = hist[['date', 'amt_pct']].copy()
        hist['hist_mean_amt'] = hist['amt_pct'].expanding(min_periods=hist_min_periods).mean()
        merged = merged.merge(hist[['date', 'hist_mean_amt']], on='date', how='left')
        merged['hist_mean_amt'] = merged['hist_mean_amt'].fillna(0.0)
        merged['heat_ratio'] = np.where(
            merged['hist_mean_amt'] > 0,
            merged['amt_ma20'] / merged['hist_mean_amt'],
            0.0,
        )
        group_signals[cid] = {
            'codes': codes,
            'names': group_names,
            'n_industries': len(codes),
            'data': merged,
        }
    return group_signals


def detect_mainline(group_signals, date,
                    heat_threshold=1.3, excess_threshold=0.3, winrate_threshold=0.6,
                    min_consecutive_days=3, lookback=20):
    """与 mainline_detector.detect_mainline 逐字等同（含 B1/D2 跨期稳定性 + P0 记忆字段）。"""
    results = []
    for cid, gs in group_signals.items():
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

        is_mainline_raw = bool(
            heat >= heat_threshold
            and excess_ma20 >= excess_threshold
            and winrate >= winrate_threshold
        )

        raw_series = (
            d['heat_ratio'].fillna(0).ge(heat_threshold)
            & d['excess_ma20'].fillna(0).ge(excess_threshold)
            & d['excess_winrate'].fillna(0).ge(winrate_threshold)
        )
        run_len_series = raw_series.groupby((~raw_series).cumsum()).cumsum().astype(int)
        consec_days = int(run_len_series.iloc[loc])
        consec_ok = consec_days >= min_consecutive_days
        is_mainline = is_mainline_raw and consec_ok

        hot_series = d['heat_ratio'].fillna(0).ge(heat_threshold)
        hot_age_series = hot_series.groupby((~hot_series).cumsum()).cumsum().astype(int)
        hot_age = int(hot_age_series.iloc[loc])

        heat_slope = float(d['heat_ratio'].iloc[loc]) - float(
            d['heat_ratio'].iloc[loc - lookback] if loc >= lookback else d['heat_ratio'].iloc[0]
        )

        blk = (~hot_series).cumsum()
        cum = d['excess'].fillna(0.0).groupby(blk).cumsum() - d['excess'].fillna(0.0).groupby(blk).transform('first')
        cum_excess_seg = float(cum.iloc[loc]) if bool(hot_series.iloc[loc]) else 0.0

        is_new_mainline = bool(is_mainline and consec_days == min_consecutive_days)

        if heat >= 1.7:
            stage = '见顶期' if excess_ma20 > 0 else '瓦解期'
        elif heat >= 1.3:
            stage = '加速期' if excess_ma20 >= excess_threshold else '启动→加速过渡'
        elif heat >= 1.0:
            stage = '启动期' if excess_ma20 > 0 else '蛰伏'
        else:
            stage = '蛰伏'

        d_before = d[d['date'] <= date].tail(60)
        up_days = d_before[d_before['market_pct'] > 0]
        down_days = d_before[d_before['market_pct'] < 0]
        if len(up_days) > 10 and len(down_days) > 10:
            offense = up_days['excess'].mean()
            defense = down_days['excess'].mean()
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
        else:
            od_ratio = float('nan')
            style = '未知'

        results.append({
            'chain_id': cid,
            'chain_names': gs['names'],
            'n_industries': gs['n_industries'],
            'heat_ratio': heat,
            'excess_ma20': excess_ma20,
            'winrate': winrate,
            'amt_ma20': amt_ma20,
            'hist_mean_amt': float(row.get('hist_mean_amt', 0)),
            'is_mainline': is_mainline,
            'is_mainline_raw': is_mainline_raw,
            'consec_days': consec_days,
            'min_consecutive_days': min_consecutive_days,
            'hot_age': hot_age,
            'heat_slope20': heat_slope,
            'cum_excess_seg': cum_excess_seg,
            'is_new_mainline': is_new_mainline,
            'stage': stage,
            'style': style,
            'od_ratio': od_ratio,
        })

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values('heat_ratio', ascending=False)


def main() -> None:
    ap = argparse.ArgumentParser(description="链版主线探测器（方案 C · 37 链）")
    ap.add_argument('--out', type=str, default=None,
                    help='输出 CSV 路径。默认写入生产产物 '
                         'industry/chain_mainline_detection.csv（列契约测试按此校验）')
    args = ap.parse_args()
    out_path = Path(args.out) if args.out else DEFAULT_OUT

    print("加载数据...")
    returns = pd.read_parquet(INDUSTRY_DIR / 'industry_weighted_returns.parquet')
    idf = pd.read_parquet(INDUSTRY_DIR / 'industry_daily_full.parquet')
    il = pd.read_parquet(INDUSTRY_DIR / 'industry_list.parquet')
    name_map = dict(zip(il['ts_code'], il['name']))

    idf['date'] = pd.to_datetime(idf['date'])
    daily_total_amt = idf.groupby('date')['total_amount'].sum().reset_index()
    daily_total_amt.columns = ['date', 'all_amount']
    idf = idf.merge(daily_total_amt, on='date', how='left')
    idf['amt_pct'] = idf['total_amount'] / idf['all_amount'] * 100

    all_codes = returns['ind_code'].unique().tolist()
    chains = load_chain_groups(VETO_CSV, PRIOR_CSV, all_codes)

    reference_date = returns['date'].max()
    print(f"\n参考日 = {reference_date.strftime('%Y-%m-%d')}")

    print("\n计算链信号...")
    group_signals = compute_group_signals(returns, idf, name_map, chains)

    print(f"\n检测 {reference_date.strftime('%Y-%m-%d')} 的主线...")
    results = detect_mainline(group_signals, reference_date)

    # ─── B3：链内残差共振连续量作新增列（不改判定）───
    # 来源：measure_chain_resonance.py 每日产出的 chain_resonance_daily.csv
    # 含义：z_adj = 当日链内残差相关 vs 同 L1 构成同规模分层零分布 z 值（跨链可比）
    # 用法：作解释列，与热度/超额/胜率平行显示，不进 is_mainline 判定
    if DAILY_RESONANCE_CSV.exists():
        dr = pd.read_csv(DAILY_RESONANCE_CSV)
        dr['date'] = pd.to_datetime(dr['date'])
        dr_day = dr[dr['date'] == reference_date]
        if len(dr_day) > 0:
            keep = dr_day[['chain_id', 'resonance_adj', 'z_adj', 'style_share']].rename(columns={
                'resonance_adj': 'resonance_adj_today',
                'z_adj': 'resonance_z_today',
                'style_share': 'style_share_today',
            })
            results = results.merge(keep, on='chain_id', how='left')
            n_with = int(results['resonance_z_today'].notna().sum())
            print(f"· B3 共振列：{n_with}/{len(results)} 链带当日共振数据（来自 measure_chain_resonance.py 每日产出）")
        else:
            print(f"⚠️ chain_resonance_daily.csv 无参考日 {reference_date} 数据，新增列为 NaN")
    else:
        print(f"⚠️ 未找到 {DAILY_RESONANCE_CSV}，共振列为 NaN")

    _mc = int(results['min_consecutive_days'].iloc[0]) if len(results) else 0
    mainlines = results[results['is_mainline']]
    non_mainlines = results[~results['is_mainline']]

    print("\n" + "=" * 80)
    print(f"链版主线检测结果 ({reference_date.strftime('%Y-%m-%d')})")
    print("=" * 80)
    print(f"\n主线口径：三条件 AND 连续 >= {_mc} 个交易日（与旧版同）")
    print(f"链总数 = {len(results)}；主线数 = {len(mainlines)}")

    if len(mainlines) > 0:
        print(f"\n✅ 检测到 {len(mainlines)} 条主线链：\n")
        for _, r in mainlines.iterrows():
            print(f"  【主线】{r['chain_id']}: {', '.join(r['chain_names'][:5])}{'...' if len(r['chain_names'])>5 else ''}")
            print(f"    热度={r['heat_ratio']:.1f}x均值, 超额={r['excess_ma20']:.3f}%/日, 胜率={r['winrate']:.0%}")
            print(f"    阶段={r['stage']}, 类型={r['style']}(进攻/防御={r['od_ratio']:.1f})")
            print(f"    连续达标={int(r['consec_days'])} 天 | 热度段龄={int(r['hot_age'])} 天"
                  f" | 热度20日变化={r['heat_slope20']:+.2f}x | 段内累计超额={r['cum_excess_seg']:+.2f}%")
    else:
        print("\n❌ 当前无链被判定为主线，市场处于轮动状态")

    print(f"\n热度排名 Top 10：\n")
    for i, (_, r) in enumerate(results.head(10).iterrows()):
        flag = "✅主线" if r['is_mainline'] else ""
        z_str = f" 共振z={r['resonance_z_today']:.1f}" if pd.notna(r.get('resonance_z_today')) else ""
        print(f"  {i+1}. {r['chain_id']}({r['n_industries']}行业): "
              f"热度={r['heat_ratio']:.1f}x, 超额={r['excess_ma20']:.3f}%, "
              f"阶段={r['stage']}{z_str} {flag}")
        print(f"     行业: {', '.join(r['chain_names'][:6])}")

    # 生产产物：固定路径、日更覆盖（列契约测试 tests/_common.py 按此路径校验 22 列）。
    # ⚠️ 观察期台账请用 research/observation_snapshot.py —— 它按「参考日」记账，
    #    不会因为本文件被覆盖而丢失历史对照。
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(out_path, index=False)
    print(f"\n结果已保存到 {out_path}")


if __name__ == '__main__':
    main()