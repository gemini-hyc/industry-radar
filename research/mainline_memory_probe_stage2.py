"""
主线记忆探针 · 第二阶段：拆混淆
================================

第一阶段出现了自相矛盾的两个现象：
  · 按 hot_age 分桶：3-5天 -0.51% → 21天+ +2.04%（单调，似乎"热得越久越好"）
  · 连续 IC：hot_age 仅 +0.070，而 heat / heat_pctile / amt_pctile / heat_slope20
              全部显著为负（IC ≈ -0.19 ~ -0.26）

二者打架 ⇒ 怀疑 hot_age 与热度水平负相关，段龄效应是**混淆**出来的。

假说（分母稀释）
----------------
heat_ratio = amt_ma20 / hist_mean_amt，分母是 2020 年起的**扩张均值**。
一个群持续热 ⇒ 它自己的历史均值被自己抬高 ⇒ heat_ratio 被压低
⇒ 长命热群反而测不出高热度 ⇒ 落到"加速期"而非"见顶期"。
而热度越高未来越差（IC=-0.189）⇒ "热得久"看起来好，实际是"被测成低热度"。

本脚本验证三件事：
  A. 独立性：主线样本到底覆盖几个独立 episode？（防止 1 段长牛贡献 50 条伪样本）
  B. 混淆：hot_age 与 heat_ratio / heat_pctile 的相关性
  C. 控制热度后，段龄是否还有增量（分层 + 条件分析）
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

df = pd.read_csv('/tmp/mainline_memory_probe.csv', parse_dates=['date'])
ml = df[df['is_mainline']].copy().sort_values(['gid', 'date'])

print("=" * 78)
print("A. 独立性检验：主线样本来自几个独立 episode？")
print("=" * 78)
# 段龄回落即新段：hot_age[t] < hot_age[t-1] ⇒ 新段起点
ml['new_seg'] = ml.groupby('gid')['hot_age'].diff().fillna(1) <= 0
ml['seg_id'] = ml.groupby('gid')['new_seg'].cumsum()
ml['episode'] = ml['gid'].astype(str) + '-' + ml['seg_id'].astype(str)

ep = ml.groupby('episode').agg(
    n=('fwd20', 'size'), max_age=('hot_age', 'max'),
    first=('date', 'min'), last=('date', 'max'),
    med_fwd=('fwd20', 'median')).sort_values('n', ascending=False)
print(f"  主线样本 {len(ml)} 条，来自 {ml['episode'].nunique()} 个独立 episode（群×段）")
print(f"  episode 样本量分布：中位 {ep['n'].median():.0f}，最大 {ep['n'].max()}，"
      f"top5 合计占 {ep['n'].head(5).sum()/len(ml):.0%}")
print("\n  最大的 8 个 episode：")
for eid, r in ep.head(8).iterrows():
    days = (r['last'] - r['first']).days
    print(f"    {eid:<10} n={int(r['n']):>3}  最长段龄={int(r['max_age']):>3}  "
          f"{r['first'].date()}~{r['last'].date()} ({days}天)  中位fwd20={r['med_fwd']:+.2f}%")

for b, lo, hi in [('3-5天', 1, 5), ('6-10天', 6, 10), ('11-20天', 11, 20), ('21天+', 21, 10**9)]:
    g = ml[(ml['hot_age'] >= lo) & (ml['hot_age'] <= hi)]
    print(f"    {b:<8} 样本 {len(g):>3} 条 / 独立 episode {g['episode'].nunique():>3} 个  "
          f"→ 每 episode 平均 {len(g)/max(g['episode'].nunique(),1):.1f} 条")

print("\n" + "=" * 78)
print("B. 混淆检验：段龄 vs 热度水平 的相关（主线内）")
print("=" * 78)
for f in ['heat', 'heat_pctile', 'amt_pctile', 'heat_slope20', 'excess_ma20']:
    s = ml[['hot_age', f]].dropna()
    print(f"  Spearman(hot_age, {f:<13}) = {s['hot_age'].rank().corr(s[f].rank()):+.3f}")
print("  ⇒ 若为负：长命热群确实被测成「低热度」，分母稀释假说成立")

# 直接看分母稀释：同一个群，段龄长时 hist_mean 是否被抬高
print("\n  分母稀释的直接证据（按段龄分桶看 heat 与构成）：")
for b, lo, hi in [('3-5天', 1, 5), ('11-20天', 11, 20), ('21天+', 21, 10**9)]:
    g = ml[(ml['hot_age'] >= lo) & (ml['hot_age'] <= hi)]
    print(f"    {b:<8} heat 中位={g['heat'].median():.2f}x  "
          f"heat_pctile 中位={g['heat_pctile'].median():.2f}  "
          f"amt_pctile 中位={g['amt_pctile'].median():.2f}  "
          f"heat_slope20 中位={g['heat_slope20'].median():+.2f}")

print("\n" + "=" * 78)
print("C. 控制热度后，段龄还有增量吗？（分层条件分析）")
print("=" * 78)
ml['heat_lvl'] = pd.cut(ml['heat'], [0, 1.45, 1.7, 99],
                        labels=['低热<1.45', '中热1.45-1.7', '高热≥1.7'])
for lvl in ['低热<1.45', '中热1.45-1.7', '高热≥1.7']:
    g = ml[ml['heat_lvl'] == lvl]
    if len(g) < 10:
        print(f"  【{lvl}】样本 {len(g)} 条，不足")
        continue
    young, old = g[g['hot_age'] <= 10], g[g['hot_age'] > 10]
    line = f"  【{lvl}】n={len(g):>3}  "
    line += f"段龄≤10天: n={len(young):>3} 中位={young['fwd20'].median():+.2f}%  " if len(young) else "段龄≤10天: n=0  "
    line += f"段龄>10天: n={len(old):>3} 中位={old['fwd20'].median():+.2f}%" if len(old) else "段龄>10天: n=0"
    print(line)

print("\n  用「热度分位」替代「热度倍数」再分层（分位不受分母漂移影响）：")
ml['hp_lvl'] = pd.cut(ml['heat_pctile'], [0, 0.33, 0.67, 1.0],
                      labels=['分位低', '分位中', '分位高'])
for lvl in ['分位低', '分位中', '分位高']:
    g = ml[ml['hp_lvl'] == lvl]
    if len(g) < 10:
        print(f"  【{lvl}】样本 {len(g)} 条，不足")
        continue
    young, old = g[g['hot_age'] <= 10], g[g['hot_age'] > 10]
    y = f"n={len(young):>3} 中位={young['fwd20'].median():+.2f}% 为正={(young['fwd20']>0).mean():.0%}" if len(young) else "n=0"
    o = f"n={len(old):>3} 中位={old['fwd20'].median():+.2f}% 为正={(old['fwd20']>0).mean():.0%}" if len(old) else "n=0"
    print(f"  【{lvl}】段龄≤10天: {y}   |   段龄>10天: {o}")

print("\n" + "=" * 78)
print("D. 换个记忆定义：是否「二次加速」才是关键（段龄 × 热度斜率）")
print("=" * 78)
ml['re_accel'] = (ml['hot_age'] > 20) & (ml['heat_slope20'] > 0)
ml['old_decay'] = (ml['hot_age'] > 20) & (ml['heat_slope20'] <= 0)
for nm, g in [('老段+热度仍在升', ml[ml['re_accel']]),
              ('老段+热度已回落', ml[ml['old_decay']]),
              ('新段(≤10天)', ml[ml['hot_age'] <= 10])]:
    if len(g):
        print(f"  {nm:<18} n={len(g):>3}  episode={g['episode'].nunique():>3}  "
              f"中位={g['fwd20'].median():+.2f}%  为正={(g['fwd20']>0).mean():.1%}")

print("\n" + "=" * 78)
print("E. 结论口径")
print("=" * 78)
print("  · 看 episode 数而非样本条数：条数多但集中在少数 episode ⇒ 不能当独立证据。")
print("  · 分层后若段龄效应消失 ⇒ 第一阶段的分桶单调是热度混淆的产物。")
