"""
P1 假说 A/B 检验：换滚动分母能否解除 heat 与段龄的混淆？
==========================================================

背景：上一轮 P1 建议声称把 heat_ratio 分母从「2020 起扩张均值」改为
「滚动 250 日均值」可以「解除 heat 与段龄 +0.528 的混淆」。
该声称**未经检验**。本脚本做 A/B：

  A（现行）  heat_exp = amt_ma20 / expanding_mean(amt_pct, 自2020)
  B（提案）  heat_roll = amt_ma20 / rolling_mean(amt_pct, 250日)

检验三件事（都在主线样本内）：
  1. Spearman(hot_age, heat) 是否显著下降（解混淆是否成立）
  2. 主线判定数量的变化（同一阈值下 B 口径是否还标得出足够多的主线 —— 校准差异）
  3. 段龄分桶的 fwd20 单调性是否在 B 口径下仍然成立（结论是否口径依赖）
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from src.paths import INDUSTRY_DIR
import mainline_detector as md
from research.mainline_memory_probe import load_matrices, trailing_true_count

HEAT_T, EXCESS_T, WINRATE_T, MIN_CONSEC = 1.3, 0.3, 0.6, 3
LOOKBACK, FWD, SAMPLE_EVERY, ANCHOR_EVERY, PCTILE_WIN = 20, 20, 5, 120, 250


def build_frame(R, A, codes):
    codes = [c for c in codes if c in R.columns]
    if not codes:
        return None
    gp, amt, mkt = R[codes].mean(axis=1), A[codes].sum(axis=1), R.mean(axis=1)
    d = pd.DataFrame({'group_pct': gp, 'amt_pct': amt, 'market_pct': mkt})
    d['excess'] = d['group_pct'] - d['market_pct']
    d['amt_ma20'] = d['amt_pct'].rolling(LOOKBACK, min_periods=10).mean()
    d['excess_ma20'] = d['excess'].rolling(LOOKBACK, min_periods=10).mean()
    d['winrate'] = d['excess'].rolling(LOOKBACK, min_periods=10).apply(lambda x: (x > 0).mean(), raw=True)

    den_exp = d['amt_pct'].expanding(min_periods=LOOKBACK).mean()
    den_roll = d['amt_pct'].rolling(PCTILE_WIN, min_periods=60).mean()

    d['heat_exp'] = np.where(den_exp > 0, d['amt_ma20'] / den_exp, 0.0)
    d['heat_roll'] = np.where(den_roll > 0, d['amt_ma20'] / den_roll, 0.0)
    d['fwd20'] = d['excess'].rolling(FWD, min_periods=FWD).sum().shift(-FWD)

    ok_e = (d['excess_ma20'] >= EXCESS_T) & (d['winrate'] >= WINRATE_T)
    d['raw_exp'] = ok_e & (d['heat_exp'] >= HEAT_T)
    d['raw_roll'] = ok_e & (d['heat_roll'] >= HEAT_T)
    for tag in ('exp', 'roll'):
        d[f'run_{tag}'] = trailing_true_count(d[f'raw_{tag}'])
        d[f'age_{tag}'] = trailing_true_count(d[f'raw_{tag}'].astype(bool))
        d[f'ml_{tag}'] = d[f'run_{tag}'] >= MIN_CONSEC
    return d


def main():
    R, A, name_map = load_matrices()
    dates = list(R.index)
    anchors = dates[::ANCHOR_EVERY]
    if anchors[-1] != dates[-1]:
        anchors.append(dates[-1])

    rows = []
    for ai, a in enumerate(anchors):
        lo_i, hi_i = ai * ANCHOR_EVERY, min((ai + 1) * ANCHOR_EVERY, len(dates))
        groups, _ = md.cluster_industries(R.loc[R.index <= a], corr_threshold=0.6)
        for gid, codes in groups.items():
            d = build_frame(R, A, codes)
            if d is None:
                continue
            sub = d.iloc[lo_i:hi_i]
            for pos in range(0, len(sub), SAMPLE_EVERY):
                r = sub.iloc[pos]
                if pd.isna(r['fwd20']):
                    continue
                rows.append({
                    'date': sub.index[pos], 'gid': gid,
                    'ml_exp': bool(r['ml_exp']), 'ml_roll': bool(r['ml_roll']),
                    'heat_exp': r['heat_exp'], 'heat_roll': r['heat_roll'],
                    'age_exp': int(r['age_exp']), 'age_roll': int(r['age_roll']),
                    'fwd20': r['fwd20'],
                })

    df = pd.DataFrame(rows)
    me, mr = df[df['ml_exp']], df[df['ml_roll']]
    print(f"样本 {len(df)} 条")
    print(f"\n【检验2 · 校准差异】同一阈值下两口径的主线样本量")
    print(f"  A 扩张均值 : {len(me)} 条（占 {len(me)/len(df):.1%}）  heat 中位 {me['heat_exp'].median():.2f}x")
    print(f"  B 滚动均值 : {len(mr)} 条（占 {len(mr)/len(df):.1%}）  heat 中位 {mr['heat_roll'].median():.2f}x")
    both = df[df['ml_exp'] & df['ml_roll']]
    print(f"  两口径同时判为主线：{len(both)} 条（交集/并集 = "
          f"{len(both)/max(len(df[df['ml_exp'] | df['ml_roll']]),1):.0%}）")

    print(f"\n【检验1 · 解混淆是否成立】主线内 Spearman(hot_age, heat)")
    for tag, sub in [('A 扩张均值', me), ('B 滚动均值', mr)]:
        if len(sub) < 30:
            print(f"  {tag}: 样本不足（{len(sub)}）")
            continue
        key = 'exp' if tag.startswith('A') else 'roll'
        s = sub[[f'age_{key}', f'heat_{key}']].dropna()
        ic = s.iloc[:, 0].rank().corr(s.iloc[:, 1].rank())
        print(f"  {tag}: ρ = {ic:+.3f}  (n={len(s)})")

    print(f"\n【检验3 · 段龄分桶单调性是否口径依赖】")
    for tag, sub in [('A', me), ('B', mr)]:
        key = 'age_exp' if tag == 'A' else 'age_roll'
        print(f"  口径{tag} 主线 n={len(sub)}：")
        for lo, hi, lbl in [(1, 5, '≤5'), (6, 10, '6-10'), (11, 20, '11-20'), (21, 10**9, '21+')]:
            g = sub[(sub[key] >= lo) & (sub[key] <= hi)]
            if len(g):
                print(f"    段龄{lbl:<5} n={len(g):>4}  fwd20 中位={g['fwd20'].median():+.2f}%  为正={(g['fwd20']>0).mean():.0%}")
            else:
                print(f"    段龄{lbl:<5} n=   0")


if __name__ == '__main__':
    main()
