#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
观察期 · 历史回溯（B2 验收第 3 条，只读脚本）

为什么需要这个脚本
──────────────────
「每天手动跑一次、等两天」是最笨的观察方式：一次只能拿到 1 个交易日样本。
其实新版/旧版的判定逻辑都是 **纯函数**（`detect_mainline(group_signals, date)`），
只要 group_signals 覆盖足够长的历史，就能对**任意历史日期**重新判定。

⇒ 本脚本一口气把最近 N 个交易日全部重算，一次拿到完整对照序列。
  不用等今晚 18:30，现在就能看。

两套口径的 PIT 处置（重要）
──────────────────────────
- **新版（链）**：链名单位外生固定（`chain-prior-list.csv`，2026-10-11 锁定），
  对每个历史日期只用「截止当日」的滚动窗口算 heat/excess/winrate ⇒ 时序上干净。
  ⚠️ 但名单本身是 2026 年锁定的 ⇒ 回溯等于「用 2026 年的名单看过去」，
     存在**名单后见之明**。窗口越短影响越小（本脚本默认只看最近 20 个交易日）。

- **旧版（自由聚类）**：必须**每日重聚类**才是 PIT —— 这正是
  `scripts/mainline_detector.py::run_full_detection` 里 D13 修正做的事
  （`pivot.loc[pivot.index <= reference_date]`）。
  ⚠️ `run_full_detection` 会 `save_dataframe` 写生产产物 ⇒ **本脚本不能直接调它**，
     改成逐字复用它的内部步骤、去掉写盘那一行。
  注：每���重聚类 ⇒ 群成员会漂移，这正是旧版被判「测错对象」的病根之一，
      也是本对照要观察的对象本身。

零污染保证
──────────
- 只读 `~/quant-data/industry/`
- 只写 `~/quant-data/_local_dev/`
- 不碰生产十步链、不动名单、不动任何阈值

用法
────
    # 回溯最近 20 个交易日（默认）
    /usr/bin/python3 research/observation_backfill.py

    # 只回溯 10 月以来
    /usr/bin/python3 research/observation_backfill.py --start 2026-10-01

    # 回溯最近 40 个交易日
    /usr/bin/python3 research/observation_backfill.py --days 40

记录时间：2026-10-12
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import importlib.util as _ilu


def _load(name, path):
    """按文件路径直接加载模块（research/ 不是包，避免 import 包路径问题）。"""
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 新版（链）──
# 2026-10-12：脚本已从 research/ 移入 scripts/（进生产十步链 7b 步），
#             输出改到生产产物 industry/chain_mainline_detection.csv。
NEW = _load("new_detector", REPO / "scripts" / "chain_mainline_detector.py")

# ── 旧版（自由聚类）──
OLD = _load("old_detector", REPO / "scripts" / "mainline_detector.py")

DATA_ROOT = Path("/Users/hyc/quant-data")
INDUSTRY_DIR = DATA_ROOT / "industry"
OLD_CSV = INDUSTRY_DIR / "mainline_detection.csv"
OUT_DIR = DATA_ROOT / "_local_dev"
LOG_CSV = OUT_DIR / "observation_log.csv"
DAILY_RESONANCE_CSV = INDUSTRY_DIR / "chain_resonance_daily.csv"


def build_new_all(returns, chains):
    """新版：一次算全历史 group_signals（37 链）。"""
    idf = pd.read_parquet(INDUSTRY_DIR / 'industry_daily_full.parquet')
    il = pd.read_parquet(INDUSTRY_DIR / 'industry_list.parquet')
    name_map = dict(zip(il['ts_code'], il['name']))
    idf['date'] = pd.to_datetime(idf['date'])
    daily_total_amt = idf.groupby('date')['total_amount'].sum().reset_index()
    daily_total_amt.columns = ['date', 'all_amount']
    idf = idf.merge(daily_total_amt, on='date', how='left')
    idf['amt_pct'] = idf['total_amount'] / idf['all_amount'] * 100
    return NEW.compute_group_signals(returns, idf, name_map, chains)


def run_old_for_date(returns, idf, name_map, date):
    """旧版：对单个日期「截止当日重聚类」后判定（PIT，逐字复用 D13 逻辑，不写盘）。"""
    pivot_all = returns.pivot(index='date', columns='ind_code', values='weighted_pct')
    pivot = pivot_all.loc[pivot_all.index <= date]
    groups, corr_matrix = OLD.cluster_industries(pivot, corr_threshold=0.6)
    gs = OLD.compute_group_signals(returns, idf, name_map, groups, corr_matrix)
    res = OLD.detect_mainline(gs, date)
    return res, groups


def summarize(res, id_col, name_col, date):
    """从一张判定表抽汇总指标。"""
    if res is None or len(res) == 0:
        return {}
    d = res.copy()
    if 'heat_ratio' in d.columns:
        d = d.sort_values('heat_ratio', ascending=False)
    top = d.iloc[0]
    ml = d[d['is_mainline']]
    ml_names = "、".join(str(x) for x in ml[id_col]) if len(ml) else "（无）"
    return {
        'n_groups': len(d),
        'mainlines': int(d['is_mainline'].sum()),
        'mainline_names': ml_names,
        'top1': str(top.get(id_col, '')),
        'top1_heat': round(float(top.get('heat_ratio', float('nan'))), 2),
        'top1_excess': round(float(top.get('excess_ma20', float('nan'))), 3),
        'top1_winrate': round(float(top.get('winrate', float('nan'))), 2),
        'top1_stage': str(top.get('stage', '')),
        'top1_n': int(top.get('n_industries', 0)),
    }


def scan_all(exc_threshold=0.3, heat_threshold=1.3, win_threshold=0.6, min_consec=3):
    """
    全历史扫描：新版（37 链）在 2020 年至今的每一天，判出几条主线。

    目的
    ────
    区分两种可能：
      (a) 最近 20 天 0 主线 = **当前市场确实无主线**（阈值没问题，是市场状态）
      (b) 历史上也极少出主线 = **阈值在链尺度上根本不适配**（需要重标定）

    只跑新版 —— 旧版已被方法论判「测错对象」，长历史对照意义有限，
    且其每日重聚类成本高（0.35s/天 × 1600 天 ≈ 9 分钟）。
    """
    D = INDUSTRY_DIR
    print("全历史扫描：加载数据...")
    returns = pd.read_parquet(D / 'industry_weighted_returns.parquet')
    idf = pd.read_parquet(D / 'industry_daily_full.parquet')
    il = pd.read_parquet(D / 'industry_list.parquet')
    name_map = dict(zip(il['ts_code'], il['name']))
    returns['date'] = pd.to_datetime(returns['date'])
    idf['date'] = pd.to_datetime(idf['date'])
    _t = idf.groupby('date')['total_amount'].sum().reset_index()
    _t.columns = ['date', 'all_amount']
    idf = idf.merge(_t, on='date', how='left')
    idf['amt_pct'] = idf['total_amount'] / idf['all_amount'] * 100

    chains = NEW.load_chain_groups(NEW.VETO_CSV, NEW.PRIOR_CSV,
                                   returns['ind_code'].unique().tolist())
    print(f"  方案 C 链数 = {len(chains)}")
    gs = NEW.compute_group_signals(returns, idf, name_map, chains)

    # 对每条链预计算三条件 raw + 连续达标天数（只依赖过去 ⇒ PIT 干净）
    per_chain = {}
    for cid, g in gs.items():
        d = g['data'].sort_values('date').reset_index(drop=True)
        raw = ((d['heat_ratio'].fillna(0) >= heat_threshold)
               & (d['excess_ma20'].fillna(0) >= exc_threshold)
               & (d['excess_winrate'].fillna(0) >= win_threshold))
        run = raw.groupby((~raw).cumsum()).cumsum().astype(int)
        per_chain[cid] = pd.Series(run.values, index=d['date'].values)

    mat = pd.DataFrame(per_chain).sort_index()
    ml_per_day = (mat >= min_consec).sum(axis=1)

    df = pd.DataFrame({
        'date': [pd.Timestamp(x).strftime('%Y-%m-%d') for x in ml_per_day.index],
        'new_mainlines': ml_per_day.values,
    })
    df['year'] = df['date'].str[:4]
    df['month'] = df['date'].str[:7]

    out = OUT_DIR / f"observation_scan_all_exc{exc_threshold}.csv"
    df.to_csv(out, index=False)

    print("\n" + "=" * 90)
    print(f"全历史扫描 · 超额门槛 = {exc_threshold}%/日（热度>={heat_threshold}, 胜率>={win_threshold}, 连续>={min_consec}天）")
    print("=" * 90)
    n_days = len(df)
    n_with = int((df['new_mainlines'] > 0).sum())
    print(f"交易日总数 = {n_days}；有主线的天数 = {n_with}（{n_with / n_days * 100:.1f}%）")

    y = df.groupby('year').agg(
        days=('new_mainlines', 'size'),
        days_with_ml=('new_mainlines', lambda s: int((s > 0).sum())),
        avg_ml=('new_mainlines', 'mean'),
    )
    y['pct_days_with_ml'] = (y['days_with_ml'] / y['days'] * 100).round(1)
    print("\n按年统计：")
    print(y.round(2).to_string())

    print(f"\n明细 → {out}")
    return df


def main():
    ap = argparse.ArgumentParser(description="观察期历史回溯：新版（链）vs 旧版（自由聚类）")
    ap.add_argument('--days', type=int, default=20, help='回溯最近 N 个交易日（默认 20）')
    ap.add_argument('--start', type=str, default=None, help='按日期下界回溯，如 2026-10-01')
    ap.add_argument('--quiet', action='store_true', help='少打印')
    ap.add_argument('--scan-all', action='store_true',
                    help='全历史扫描（只跑新版链，按年/月统计主线出现频率）')
    ap.add_argument('--exc', type=float, default=0.3, help='超额门槛（默认 0.3，配合 --scan-all 做敏感度）')
    args = ap.parse_args()

    if args.scan_all:
        return scan_all(exc_threshold=args.exc)

    print("加载数据...")
    returns = pd.read_parquet(INDUSTRY_DIR / 'industry_weighted_returns.parquet')
    idf = pd.read_parquet(INDUSTRY_DIR / 'industry_daily_full.parquet')
    il = pd.read_parquet(INDUSTRY_DIR / 'industry_list.parquet')
    name_map = dict(zip(il['ts_code'], il['name']))
    idf['date'] = pd.to_datetime(idf['date'])
    returns['date'] = pd.to_datetime(returns['date'])

    # ⚠️ 旧版 compute_group_signals 依赖 idf 里有 amt_pct（个股成交额占全市场比）。
    #    parquet 里没存这一列，生产链是在 run_full_detection 之前算好的 ⇒ 这里补齐，
    #    否则报 KeyError: 'amt_pct'。
    _tot = idf.groupby('date')['total_amount'].sum().reset_index()
    _tot.columns = ['date', 'all_amount']
    idf = idf.merge(_tot, on='date', how='left')
    idf['amt_pct'] = idf['total_amount'] / idf['all_amount'] * 100

    all_codes = returns['ind_code'].unique().tolist()
    chains = NEW.load_chain_groups(NEW.VETO_CSV, NEW.PRIOR_CSV, all_codes)
    print(f"  方案 C 链数 = {len(chains)}")

    # 新版：一次算全历史
    print("计算新版（链）全历史信号...")
    new_signals = build_new_all(returns, chains)

    # 交易日列表
    # ⚠️ group_signals 是 dict[cid] -> {'codes','names','n_industries','data': DataFrame}
    #    不是一张 DataFrame，所以不能 new_signals['date']；取所有链日期的并集。
    all_dates = sorted(
        set().union(*[set(gs['data']['date'].unique()) for gs in new_signals.values()])
    )
    if args.start:
        all_dates = [d for d in all_dates if pd.Timestamp(d) >= pd.Timestamp(args.start)]
    dates = all_dates[-args.days:]
    print(f"  回溯交易日 = {len(dates)} 个：{pd.Timestamp(dates[0]).date()} → {pd.Timestamp(dates[-1]).date()}")

    # 共振日线（可选，用于 z 列）
    dr_by_date = {}
    if DAILY_RESONANCE_CSV.exists():
        dr = pd.read_csv(DAILY_RESONANCE_CSV)
        dr['date'] = pd.to_datetime(dr['date'])
        for d, g in dr.groupby('date'):
            dr_by_date[d] = g.set_index('chain_id')[['resonance_adj', 'z_adj']].to_dict('index')

    rows = []
    detail_rows = []
    for i, d in enumerate(dates):
        d = pd.Timestamp(d)
        try:
            new_res = NEW.detect_mainline(new_signals, d)
        except Exception as e:
            print(f"  ⚠️ 新版 {d.date()} 失败：{e}")
            new_res = None
        try:
            old_res, _ = run_old_for_date(returns, idf, name_map, d)
        except Exception as e:
            print(f"  ⚠️ 旧版 {d.date()} 失败：{e}")
            old_res = None

        ns = summarize(new_res, 'chain_id', 'chain_names', d)
        os_ = summarize(old_res, 'group_id', 'group_names', d)

        z1_chain, z1_val = '', None
        if new_res is not None and len(new_res) and 'resonance_z_today' in new_res.columns:
            pass  # detect_mainline 不含共振列，走 daily
        if d in dr_by_date and new_res is not None and len(new_res):
            m = dr_by_date[d]
            cands = [(cid, (m.get(cid) or {}).get('z_adj')) for cid in new_res['chain_id']]
            cands = [(c, v) for c, v in cands if v is not None and pd.notna(v)]
            if cands:
                cands.sort(key=lambda x: -x[1])
                z1_chain, z1_val = str(cands[0][0]), round(float(cands[0][1]), 2)

        # ── 明细行：每条链一行，附当日新旧主线数（用于「看某天到底发生了什么」）──
        if new_res is not None and len(new_res):
            for _, r in new_res.iterrows():
                _z = None
                if d in dr_by_date:
                    _v = (dr_by_date[d].get(r['chain_id']) or {}).get('z_adj')
                    _z = round(float(_v), 2) if _v is not None and pd.notna(_v) else None
                detail_rows.append({
                    'date': d.strftime('%Y-%m-%d'),
                    'chain_id': r['chain_id'],
                    'n_industries': int(r['n_industries']),
                    'heat_ratio': round(float(r['heat_ratio']), 3),
                    'excess_ma20': round(float(r['excess_ma20']), 4),
                    'winrate': round(float(r['winrate']), 3),
                    'is_mainline_raw': bool(r['is_mainline_raw']),
                    'is_mainline': bool(r['is_mainline']),
                    'consec_days': int(r['consec_days']),
                    'stage': r['stage'],
                    'z_adj': _z,
                    'new_mainlines_thatday': int(new_res['is_mainline'].sum()),
                    'old_mainlines_thatday': (
                        int(old_res['is_mainline'].sum()) if old_res is not None and len(old_res) else None
                    ),
                    'old_mainline_groups': os_.get('mainline_names'),
                })

        rows.append({
            'ref_date': d.strftime('%Y-%m-%d'),
            'snapshot_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'old_ref_date': d.strftime('%Y-%m-%d'),
            'ref_aligned': 'yes',
            'new_n_chains': ns.get('n_groups'),
            'new_mainlines': ns.get('mainlines'),
            'new_top1_chain': ns.get('top1'),
            'new_top1_heat': ns.get('top1_heat'),
            'new_top1_excess': ns.get('top1_excess'),
            'new_top1_winrate': ns.get('top1_winrate'),
            'new_top1_stage': ns.get('top1_stage'),
            'new_top1_z': None,
            'new_top1_is_mainline': bool(ns.get('mainlines', 0) > 0),
            'new_z_top1_chain': z1_chain,
            'new_z_top1_z': z1_val,
            'new_mainline_chains': ns.get('mainline_names'),
            'old_n_groups': os_.get('n_groups'),
            'old_mainlines': os_.get('mainlines'),
            'old_mainline_groups': os_.get('mainline_names'),
            'old_top1_group': os_.get('top1'),
            'old_top1_heat': os_.get('top1_heat'),
            'old_top1_excess': os_.get('top1_excess'),
            'old_top1_winrate': os_.get('top1_winrate'),
            'old_top1_stage': os_.get('top1_stage'),
            'old_top1_n_ind': os_.get('top1_n'),
            'delta_mainlines': (ns.get('mainlines') or 0) - (os_.get('mainlines') or 0),
            'note': 'backfill',
        })
        if not args.quiet and (i + 1) % 5 == 0:
            print(f"    ...{i+1}/{len(dates)}")

    df = pd.DataFrame(rows)

    # 与已有台账合并（同 ref_date 覆盖，保留范围外的行）
    if LOG_CSV.exists():
        old_log = pd.read_csv(LOG_CSV)
        keep = old_log[~old_log['ref_date'].isin(df['ref_date'])]
        df = pd.concat([keep, df], ignore_index=True)
        df = df.sort_values('ref_date').reset_index(drop=True)
    df.to_csv(LOG_CSV, index=False)

    if detail_rows:
        ddet = pd.DataFrame(detail_rows)
        det_path = OUT_DIR / f"observation_backfill_detail_{dates[0].strftime('%Y%m%d')}_{dates[-1].strftime('%Y%m%d')}.csv"
        ddet.to_csv(det_path, index=False)
        print(f"每日明细 → {det_path}  （{len(ddet)} 行 = {len(dates)} 天 × 37 链）")

    # 汇总打印
    print("\n" + "=" * 110)
    print("观察期历史回溯：新版（37 链 / 方案 C） vs 旧版（自由聚类）")
    print("=" * 110)
    show = ['ref_date', 'new_n_chains', 'new_mainlines', 'new_mainline_chains',
            'old_n_groups', 'old_mainlines', 'old_mainline_groups', 'delta_mainlines']
    print(df[show].to_string(index=False))

    print(f"\n新版主线数序列：{list(df['new_mainlines'])}")
    print(f"旧版主线数序列：{list(df['old_mainlines'])}")
    print(f"新版有主线的交易日：{int((df['new_mainlines'] > 0).sum())}/{len(df)}")
    print(f"旧版有主线的交易日：{int((df['old_mainlines'] > 0).sum())}/{len(df)}")

    # 新版 top1 链的稳定性
    if 'new_top1_chain' in df.columns:
        vc = df['new_top1_chain'].value_counts()
        print(f"\n新版「热度第一链」分布：")
        for k, v in vc.items():
            print(f"  {k}: {v}/{len(df)} 天")

    print(f"\n台账已更新 → {LOG_CSV}  （note=backfill 表示历史回溯，非当日实跑）")


if __name__ == '__main__':
    main()
