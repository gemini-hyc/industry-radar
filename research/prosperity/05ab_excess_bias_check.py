#!/usr/bin/env python3
"""
05ab · 超额收益口径体检：日频算术差复利 vs 真实相对收益（只读）

疑点（05aa 暴露）：持有 >120 天的 B 交易"绝对收益中位 +14.08%，超额中位 −10.75%"。
若绝对为正、超额为负，只有两种可能：
  (a) 同期市场涨得更多（真实的相对跑输）；
  (b) 05y/05z 的超额定义有长窗口偏差：
      EX_t = R_t − M_t 是**日频算术差**，再 Π(1+EX_t) 复利。
      但几何相对收益应是 (1+Π(1+R_t)) / (1+Π(1+M_t)) − 1。
      二者之差 ≈ −0.5·(σ_R²+σ_M²−2σ_RM)·T，随窗口 T 线性放大 ⇒ 长窗口系统性压低超额。

本脚本并排计算两种定义，判定到底是 (a) 还是 (b)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

DATA = Path("/Users/hyc/quant-data")
OUT = Path("/Users/hyc/quant/industry-radar/research/prosperity")
H_DIV = 120


def main():
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index() / 100.0
    mkt = (wr.groupby("date").apply(
        lambda d: (d["circ_mv"] * d["weighted_pct"]).sum() / d["circ_mv"].sum(),
        include_groups=False)) / 100.0
    mkt = mkt.reindex(R.index)

    print("=" * 92)
    print("【0】市场收益序列体检（日频）")
    print(f"  样本区间 : {R.index.min().date()} ~ {R.index.max().date()}   交易日 {len(R)}")
    print(f"  mkt 日收益: 均值 {mkt.mean()*100:+.4f}%  中位 {mkt.median()*100:+.4f}%  标准差 {mkt.std()*100:.3f}%")
    print(f"  行业 R 日均标准差(截面中位) = {R.std().median()*100:.3f}%")
    cum_mkt = (1 + mkt).cumprod()
    print(f"  mkt 累计净值 {R.index.min().date()}→{R.index.max().date()}: {cum_mkt.iloc[-1]:.3f}"
          f"  ({(cum_mkt.iloc[-1]-1)*100:+.1f}%)")

    ex_day = R.sub(mkt, axis=0)
    print(f"  EX=R−mkt 截面均值(日均): {ex_day.mean(axis=1).mean()*100:+.4f}%   "
          f"截面中位(日均): {ex_day.median(axis=1).mean()*100:+.4f}%")
    print("  （若两者都≈0，说明超额定义本身无系统性偏移，偏差只来自复利方式）")

    df = pd.read_csv(OUT / "05aa_exit_decomp_pb.csv")
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    done = df[~df["censored"]].copy()

    dpos = {d: k for k, d in enumerate(R.index)}
    cols = list(R.columns)
    colpos = {c: k for k, c in enumerate(cols)}
    rv = R.values
    mv = mkt.values

    recs = []
    for _, r in done.iterrows():
        i = dpos.get(r["entry_date"])
        j = dpos.get(r["exit_date"])
        if i is None or j is None:
            continue
        k = colpos[r["ind"]]
        wr_ = rv[i + 1:j + 1, k]
        wm = mv[i + 1:j + 1]
        abs_ret = np.prod(1 + wr_) - 1
        mkt_ret = np.prod(1 + wm) - 1
        ex_comp = np.prod(1 + (wr_ - wm)) - 1              # 05y/05z 口径
        ex_true = (1 + abs_ret) / (1 + mkt_ret) - 1         # 真实相对收益
        recs.append({"ind": r["ind"], "hold": j - i, "abs_ret": abs_ret, "mkt_ret": mkt_ret,
                     "ex_compound": ex_comp, "ex_true": ex_true, "bias": ex_comp - ex_true})
    d = pd.DataFrame(recs)
    d.to_csv(OUT / "05ab_excess_bias_check.csv", index=False)

    d["grp"] = np.where(d["hold"] <= H_DIV, f"≤{H_DIV}日", f">{H_DIV}日")
    print("\n" + "=" * 92)
    print("【1】两种超额定义并排（按持有天数分组，中位）")
    g = d.groupby("grp").agg(n=("hold", "size"), 持有=("hold", "median"),
                             行业绝对收益=("abs_ret", "median"), 同期市场收益=("mkt_ret", "median"),
                             超额_复利差口径=("ex_compound", "median"),
                             超额_真实相对=("ex_true", "median"),
                             口径偏差=("bias", "median"))
    for c in ["行业绝对收益", "同期市场收益", "超额_复利差口径", "超额_真实相对", "口径偏差"]:
        g[c] = (g[c] * 100).map(lambda v: f"{v:+.2f}%")
    print(g.to_string(index=False))

    print("\n【2】口径偏差与持有天数的关系")
    print(f"  corr(口径偏差, 持有天数) = {d[['bias', 'hold']].corr().iloc[0, 1]:.3f}")
    q = d.assign(hb=pd.cut(d["hold"], [0, 20, 60, 120, 250, 500, 10000],
                           labels=["≤20", "21-60", "61-120", "121-250", "251-500", ">500"]))
    t = q.groupby("hb", observed=True).agg(n=("bias", "size"),
                                           口径偏差中位=("bias", "median"),
                                           超额_复利差=("ex_compound", "median"),
                                           超额_真实=("ex_true", "median"))
    for c in ["口径偏差中位", "超额_复利差", "超额_真实"]:
        t[c] = (t[c] * 100).map(lambda v: f"{v:+.2f}%")
    print(t.to_string())

    print("\n【3】5 个最极端样本的对照（看真实相对收益 vs 复利差口径）")
    e = d.nsmallest(5, "ex_compound")[["ind", "hold", "abs_ret", "mkt_ret", "ex_compound", "ex_true", "bias"]].copy()
    for c in ["abs_ret", "mkt_ret", "ex_compound", "ex_true", "bias"]:
        e[c] = (e[c] * 100).map(lambda v: f"{v:+.1f}%")
    print(e.to_string(index=False))

    print("\n【4】结论性对照：>120 日 且 复利差口径亏损 的样本，改用真实相对收益后有多少仍亏")
    ll = d[(d["hold"] > H_DIV) & (d["ex_compound"] < 0)]
    print(f"  n = {len(ll)}")
    print(f"  · 复利差口径: 亏损占比 100.0%（入选条件），中位 {ll['ex_compound'].median()*100:+.2f}%")
    print(f"  · 真实相对口径: 亏损占比 {(ll['ex_true'] < 0).mean()*100:.1f}%，"
          f"中位 {ll['ex_true'].median()*100:+.2f}%")
    print(f"  · 该组绝对收益中位 {ll['abs_ret'].median()*100:+.2f}%，"
          f"同期市场收益中位 {ll['mkt_ret'].median()*100:+.2f}%")
    print(f"  · 中位口径偏差 = {ll['bias'].median()*100:+.2f}%（纯属复利方式造成）")

    print(f"\n已写出：{OUT / '05ab_excess_bias_check.csv'}")


if __name__ == "__main__":
    main()
