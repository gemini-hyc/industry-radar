"""
主线「阶段无记忆」实证探针
=========================

问题
----
`scripts/mainline_detector.py` 的 stage 是**单日查表**：
    heat_ratio × excess_ma20 → {蛰伏 / 启动期 / 启动→加速过渡 / 加速期 / 见顶期 / 瓦解期}
输入只有「当日快照」，没有任何跨日状态。
⇒ "刚热起来 3 天" 与 "已经热了半年" 可能得到完全相同的标签。

本脚本检验：**在主线/阶段标签内部，加入「记忆」类特征能否再切出未来收益差**。
若切不出 ⇒ 加记忆只是让报表好看，不产生信息量；
若切得出 ⇒ 阶段应升级为带状态机（含迟滞 + 段龄）的结构。

设计要点（防自欺）
------------------
1. **PIT**：聚类锚点每 120 个交易日重算一次，只用锚点 ≤ t 的数据定群成员；
   所有滚动/扩张窗口只用 ≤ t 的数据；未来收益取 t+1..t+20。
2. **重叠处理**：抽样每 5 个交易日（fwd20 窗口重叠），CI 用**按日期分块 bootstrap**
   （块 = 20 个连续抽样日），避免把重叠样本当独立样本而高估显著性。
3. **两臂同偏差**：群成员定义在锚点后冻结，含轻微前视；但「有记忆 vs 无记忆」两臂
   受同一偏差影响，**差值**仍可信。绝对收益水平不可直接当策略收益用。

用法
----
    /usr/bin/python3 research/mainline_memory_probe.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from src.paths import INDUSTRY_DIR
import mainline_detector as md

# —— 与生产 detector 逐字一致的阈值 ——
HEAT_T, EXCESS_T, WINRATE_T = 1.3, 0.3, 0.6
MIN_CONSEC = 3
LOOKBACK = 20
FWD = 20
SAMPLE_EVERY = 5          # 抽样步长（交易日）
ANCHOR_EVERY = 120        # 聚类重算步长（交易日）
PCTILE_WIN = 250          # 历史分位回看窗口
BOOT_ITERS = 2000
BLOCK = 20                # bootstrap 块长（抽样日计）

rng = np.random.default_rng(20261009)


def load_matrices():
    """构建 (date × industry) 矩阵，避免逐群 groupby 的 O(epochs×groups×rows) 开销。"""
    returns, idf, name_map = md.load_data()

    R = returns.pivot(index='date', columns='ind_code', values='weighted_pct').sort_index()
    A = idf.pivot(index='date', columns='con_code', values='amt_pct').sort_index()
    A = A.reindex(index=R.index, columns=R.columns)

    return R, A, name_map


def trailing_true_count(s: pd.Series) -> pd.Series:
    """以每个位置结尾的连续 True 个数（不截断）。"""
    s = s.fillna(False).astype(bool)
    blk = (~s).cumsum()
    return s.groupby(blk).cumsum().astype(int)


def rolling_pctile(s: pd.Series, win: int, min_p: int) -> pd.Series:
    """当日值在过去 win 日自身分布中的分位（0~1）。"""
    return s.rolling(win, min_periods=min_p).apply(lambda x: (x[-1] >= x).mean(), raw=True)


def bucketize(hot_rate_window=0.0):
    pass


def build_group_frame(R: pd.DataFrame, A: pd.DataFrame, codes: list) -> pd.DataFrame:
    """单群时间序列：复刻 compute_group_signals 的口径，再叠加记忆类特征。"""
    codes = [c for c in codes if c in R.columns]
    if not codes:
        return pd.DataFrame()

    gp = R[codes].mean(axis=1)
    amt = A[codes].sum(axis=1)
    mkt = R.mean(axis=1)

    d = pd.DataFrame({'group_pct': gp, 'amt_pct': amt, 'market_pct': mkt})
    d['excess'] = d['group_pct'] - d['market_pct']
    d['amt_ma20'] = d['amt_pct'].rolling(LOOKBACK, min_periods=LOOKBACK // 2).mean()
    d['excess_ma20'] = d['excess'].rolling(LOOKBACK, min_periods=LOOKBACK // 2).mean()
    d['winrate'] = d['excess'].rolling(LOOKBACK, min_periods=LOOKBACK // 2).apply(
        lambda x: (x > 0).mean(), raw=True)

    hist_mean = d['amt_pct'].expanding(min_periods=LOOKBACK).mean()
    d['hist_mean_amt'] = hist_mean
    d['heat_ratio'] = np.where(hist_mean > 0, d['amt_ma20'] / hist_mean.replace(0, np.nan), 0.0)
    d['heat_ratio'] = d['heat_ratio'].fillna(0.0)

    # —— 标签（与 detector 同尺）——
    raw = (d['heat_ratio'] >= HEAT_T) & (d['excess_ma20'] >= EXCESS_T) & (d['winrate'] >= WINRATE_T)
    d['raw'] = raw
    d['run_len'] = trailing_true_count(raw)                      # 严格三条件连续天数（不截断）
    d['is_mainline'] = d['run_len'] >= MIN_CONSEC

    # —— 记忆类特征 ——
    hot = d['heat_ratio'] >= HEAT_T
    d['hot'] = hot
    d['hot_age'] = trailing_true_count(hot)                      # 热度达标连续天数（段龄）

    # 段内累计超额（自本段起点，剔除起点那一天）
    blk = (~hot).cumsum()
    cum = d['excess'].groupby(blk).cumsum() - d['excess'].groupby(blk).transform('first')
    d['cum_excess_seg'] = cum.where(hot, 0.0)

    d['heat_pctile'] = rolling_pctile(d['heat_ratio'], PCTILE_WIN, 60)
    d['amt_pctile'] = rolling_pctile(d['amt_ma20'], PCTILE_WIN, 60)
    d['heat_slope20'] = d['heat_ratio'] - d['heat_ratio'].shift(LOOKBACK)

    # —— 未来 20 日累计超额（t+1 .. t+20）——
    d['fwd20'] = d['excess'].rolling(FWD, min_periods=FWD).sum().shift(-FWD)

    # —— 阶段（复刻 detector 查表）——
    def _stage(r):
        h, e = r['heat_ratio'], r['excess_ma20']
        if h >= 1.7:
            return '见顶期' if e > 0 else '瓦解期'
        if h >= HEAT_T:
            return '加速期' if e >= EXCESS_T else '启动→加速过渡'
        if h >= 1.0:
            return '启动期' if e > 0 else '蛰伏'
        return '蛰伏'
    d['stage'] = d.apply(_stage, axis=1)

    return d


def run():
    R, A, name_map = load_matrices()
    dates = list(R.index)
    print(f"数据：{len(dates)} 个交易日 × {R.shape[1]} 行业  "
          f"({dates[0].date()} ~ {dates[-1].date()})")

    anchors = dates[::ANCHOR_EVERY]
    if anchors[-1] != dates[-1]:
        anchors.append(dates[-1])
    print(f"聚类锚点 {len(anchors)} 个（每 {ANCHOR_EVERY} 交易日重算，PIT）")

    rows = []
    for ai, a in enumerate(anchors):
        lo_i = ai * ANCHOR_EVERY
        hi_i = min((ai + 1) * ANCHOR_EVERY, len(dates))
        pivot_e = R.iloc[:lo_i + 1] if False else R.loc[R.index <= a]
        groups, _ = md.cluster_industries(pivot_e, corr_threshold=0.6)

        for gid, codes in groups.items():
            d = build_group_frame(R, A, codes)
            if d.empty:
                continue
            sub = d.iloc[lo_i:hi_i]
            for pos in range(0, len(sub), SAMPLE_EVERY):
                r = sub.iloc[pos]
                if pd.isna(r['fwd20']) or pd.isna(r['heat_pctile']):
                    continue
                rows.append({
                    'date': sub.index[pos], 'gid': gid, 'n': len(codes),
                    'heat': r['heat_ratio'], 'excess_ma20': r['excess_ma20'],
                    'winrate': r['winrate'], 'stage': r['stage'],
                    'is_mainline': bool(r['is_mainline']), 'run_len': int(r['run_len']),
                    'hot_age': int(r['hot_age']), 'heat_pctile': r['heat_pctile'],
                    'amt_pctile': r['amt_pctile'], 'heat_slope20': r['heat_slope20'],
                    'cum_excess_seg': r['cum_excess_seg'], 'fwd20': r['fwd20'],
                })
        print(f"  锚点 {a.date()} → {len(groups)} 群，累计样本 {len(rows)}")

    df = pd.DataFrame(rows)
    df.to_csv('/tmp/mainline_memory_probe.csv', index=False)
    print(f"\n样本：{len(df)} 条（每 {SAMPLE_EVERY} 日抽样），主线 {int(df['is_mainline'].sum())} 条")
    return df


def _buckets(v):
    if v <= 5:
        return '3-5天'
    if v <= 10:
        return '6-10天'
    if v <= 20:
        return '11-20天'
    return '21天+'


def block_bootstrap_diff(a: pd.DataFrame, b: pd.DataFrame, stat='median', iters=BOOT_ITERS):
    """按日期分块 bootstrap，检验 stat(a) - stat(b) 是否显著异于 0。"""
    def _one(x):
        return x['fwd20'].median() if stat == 'median' else (x['fwd20'] > 0).mean()

    da = sorted(a['date'].unique())
    db = sorted(b['date'].unique())

    def _blocks(dates):
        return [dates[i:i + BLOCK] for i in range(0, len(dates), BLOCK)]

    ba, bb = _blocks(da), _blocks(db)
    obs = _one(a) - _one(b)
    diffs = []
    for _ in range(iters):
        sa = pd.concat([a[a['date'].isin(ba[i])] for i in rng.integers(0, len(ba), len(ba))])
        sb = pd.concat([b[b['date'].isin(bb[i])] for i in rng.integers(0, len(bb), len(bb))])
        if len(sa) == 0 or len(sb) == 0:
            continue
        diffs.append(_one(sa) - _one(sb))
    diffs = np.array(diffs)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    p = 2 * min((diffs <= 0).mean(), (diffs >= 0).mean())
    return obs, lo, hi, max(p, 1.0 / iters)


def summarize(df: pd.DataFrame, name: str, key: str = None):
    med = df['fwd20'].median()
    pos = (df['fwd20'] > 0).mean()
    if key:
        print(f"  {name:<22} n={len(df):>5}  中位={med:+.2f}%  为正={pos:.1%}  "
              f"段龄中位={df['hot_age'].median():.0f}")
    else:
        print(f"  {name:<22} n={len(df):>5}  中位={med:+.2f}%  为正={pos:.1%}")


def main():
    df = run()

    print("\n" + "=" * 78)
    print("一、主线样本里「热了多久」的真实分布（检验记忆是否被截断）")
    print("=" * 78)
    ml = df[df['is_mainline']]
    print(f"  主线样本 run_len（严格三条件连续天数）分布：")
    print(f"    min={ml['run_len'].min()}  中位数={ml['run_len'].median():.0f}  "
          f"p90={ml['run_len'].quantile(0.9):.0f}  max={ml['run_len'].max()}")
    print(f"  主线样本 hot_age（热度达标段龄）分布：")
    print(f"    min={ml['hot_age'].min()}  中位数={ml['hot_age'].median():.0f}  "
          f"p90={ml['hot_age'].quantile(0.9):.0f}  max={ml['hot_age'].max()}")
    print(f"  ⇒ 生产报表的 consec_days 字段被 min_consecutive_days 截断到 "
          f"≤{MIN_CONSEC}，最大只能显示 {min(MIN_CONSEC, int(ml['run_len'].max()))}")

    print("\n" + "=" * 78)
    print("二、基线对照")
    print("=" * 78)
    summarize(df, '全样本')
    summarize(df[~df['is_mainline']], '非主线')
    summarize(ml, '主线')

    print("\n" + "=" * 78)
    print("三、在主线内部，按「段龄 hot_age」分桶 —— 记忆是否有增量信息")
    print("=" * 78)
    ml = ml.copy()
    ml['bucket'] = ml['hot_age'].apply(_buckets)
    for b in ['3-5天', '6-10天', '11-20天', '21天+']:
        g = ml[ml['bucket'] == b]
        if len(g):
            summarize(g, f'主线·段龄{b}', key=True)

    print("\n  差值检验（块 bootstrap，块=20 抽样日）：")
    short = ml[ml['hot_age'] <= 5]
    for lbl, other in [('6-10天', ml[(ml['hot_age'] > 5) & (ml['hot_age'] <= 10)]),
                       ('11-20天', ml[(ml['hot_age'] > 10) & (ml['hot_age'] <= 20)]),
                       ('21天+', ml[ml['hot_age'] > 20])]:
        if len(short) < 10 or len(other) < 10:
            print(f"    段龄≤5 vs {lbl}：样本不足（{len(short)}/{len(other)}）")
            continue
        for stat, unit in [('median', '%'), ('pos', 'pp')]:
            obs, lo, hi, p = block_bootstrap_diff(short, other, stat=stat)
            tag = 'median' if stat == 'median' else 'P(>0)'
            star = '✅' if (lo > 0 or hi < 0) else '—'
            print(f"    ≤5天 − {lbl:<8} Δ{tag}={obs:+.3f}{unit}  "
                  f"95%CI=[{lo:+.3f},{hi:+.3f}]  p≈{p:.3f} {star}")

    print("\n" + "=" * 78)
    print("四、关键检验：同标签内再切 —— 阶段=见顶期 时，段龄还能区分吗")
    print("=" * 78)
    for st in ['加速期', '见顶期']:
        sub = ml[ml['stage'] == st]
        if len(sub) < 30:
            print(f"  {st}：主线样本仅 {len(sub)} 条，不做分桶")
            continue
        sub = sub.copy()
        sub['bucket'] = sub['hot_age'].apply(_buckets)
        print(f"  【{st}】n={len(sub)}")
        for b in ['3-5天', '6-10天', '11-20天', '21天+']:
            g = sub[sub['bucket'] == b]
            if len(g):
                summarize(g, f'    {b}', key=True)
        s2, o2 = sub[sub['hot_age'] <= 5], sub[sub['hot_age'] > 20]
        if len(s2) >= 10 and len(o2) >= 10:
            obs, lo, hi, p = block_bootstrap_diff(s2, o2, stat='median')
            print(f"    ≤5天 − 21天+  Δmedian={obs:+.3f}%  95%CI=[{lo:+.3f},{hi:+.3f}]  p≈{p:.3f}")
        else:
            print(f"    分桶样本不足（{len(s2)}/{len(o2)}），跳过差值检验")

    print("\n" + "=" * 78)
    print("五、连续特征 IC（Spearman）：记忆类特征 vs 未来20日超额")
    print("=" * 78)
    feats = ['run_len', 'hot_age', 'heat_pctile', 'amt_pctile', 'heat_slope20',
             'cum_excess_seg', 'heat', 'excess_ma20', 'winrate']
    labels = {'run_len': '连续达标天数', 'hot_age': '热度段龄', 'heat_pctile': '热度历史分位',
              'amt_pctile': '成交额历史分位', 'heat_slope20': '热度20日斜率',
              'cum_excess_seg': '段内累计超额', 'heat': '热度倍数', 'excess_ma20': '20日超额',
              'winrate': '超额胜率'}
    for scope, data in [('全样本', df), ('主线内', ml)]:
        print(f"  【{scope}】n={len(data)}")
        ics = []
        for f in feats:
            s = data[[f, 'fwd20']].dropna()
            if len(s) < 50:
                continue
            ic = s[f].rank().corr(s['fwd20'].rank())
            ics.append((labels[f], ic, len(s)))
        for nm, ic, n in sorted(ics, key=lambda x: -abs(x[1])):
            print(f"    {nm:<14} IC={ic:+.3f}  (n={n})")

    print("\n" + "=" * 78)
    print("六、口径提醒")
    print("=" * 78)
    print("  · 群成员在锚点后冻结（含轻微前视），两臂同偏差，差值可信；绝对收益水平不等同策略收益。")
    print("  · fwd20 = t+1..t+20 累计超额（相对全行业等权），非绝对收益。")
    print("  · 抽样每 5 日 ⇒ 窗口重叠，已用按日期分块 bootstrap 修正显著性。")


if __name__ == '__main__':
    main()
