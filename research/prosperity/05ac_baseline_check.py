#!/usr/bin/env python3
"""
05ac · 无条件基准检验：事件的"超额"里，有多少只是"基准 + 持有期"的算术？

05aa 暴露：持有 >120 日的 B 交易，绝对收益中位 +14.08%、超额中位 −10.75%。
05ab 排除了复利口径偏差（中位偏差 +0.26%）。
⇒ 亏损来自"同期市场涨得更多"。但市场是**市值加权**，而事件是单个行业；
   中位行业 vs 市值加权市场，本身就有一个结构性缺口（巨无霸拉动指数）。

本脚本回答：**事件的超额，是否显著不同于"同样持有期的全行业中位超额"？**
  基准曲线 base(h) = 全体 (行业 × 起始日) 的 h 日前瞻超额中位
  事件增量 = 事件超额 − base(该事件的实际持有期)
若增量 ≈ 0 ⇒ "低估不会涨"其实是"中位行业跑不赢市值加权市场"的另一种说法，与估值无关。
若增量显著为负 ⇒ 低估确实是额外信息。

同时并列"等权行业基准"版本，检验结论对基准选择的敏感性。
只读，不改任何既有产物。
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
    ew = R.mean(axis=1)                      # 等权行业基准

    EXm = R.sub(mkt, axis=0)                 # 口径①：超额 vs 市值加权市场（项目现行）
    EXw = R.sub(ew, axis=0)                  # 口径②：超额 vs 等权行业

    hgrid = list(range(5, 905, 5))

    def curve(EX):
        lg = np.log1p(EX)
        rows = {}
        for h in hgrid:
            f = np.expm1(lg.rolling(h).sum().shift(-h))
            v = f.values[np.isfinite(f.values)]
            rows[h] = (np.median(v), np.mean(v), (v > 0).mean())
        return pd.DataFrame(rows, index=["中位", "均值", "胜率"]).T

    base_m = curve(EXm)
    base_w = curve(EXw)

    print("=" * 96)
    print("【1】无条件基准：全体行业 × 全体起始日 的 h 日前瞻超额（口径① vs 市值加权市场）")
    show = base_m.loc[[5, 20, 60, 120, 250, 500, 750, 900]]
    print(pd.DataFrame({
        "持有": show.index, "超额中位": (show["中位"] * 100).map(lambda v: f"{v:+.2f}%"),
        "超额均值": (show["均值"] * 100).map(lambda v: f"{v:+.2f}%"),
        "胜率": (show["胜率"] * 100).map(lambda v: f"{v:.1f}%")}).to_string(index=False))

    print("\n【2】同一基准换成『等权行业』后")
    showw = base_w.loc[[5, 20, 60, 120, 250, 500, 750, 900]]
    print(pd.DataFrame({
        "持有": showw.index, "超额中位": (showw["中位"] * 100).map(lambda v: f"{v:+.2f}%"),
        "超额均值": (showw["均值"] * 100).map(lambda v: f"{v:+.2f}%"),
        "胜率": (showw["胜率"] * 100).map(lambda v: f"{v:.1f}%")}).to_string(index=False))

    ev = pd.read_csv(OUT / "05y_anchor_events_pb.csv")
    print("\n" + "=" * 96)
    print("【3】事件超额 vs 同持有期无条件基准（口径① 市值加权市场）")
    rows = []
    for h in [5, 20, 60, 120, 250]:
        e = ev[f"A_{h}"].dropna()
        b = base_m.loc[h, "中位"]
        rows.append({"持有": h, "事件n": len(e), "事件超额中位": e.median(), "基准中位": b,
                     "事件增量": e.median() - b})
    t = pd.DataFrame(rows)
    for c in ["事件超额中位", "基准中位", "事件增量"]:
        t[c] = (t[c] * 100).map(lambda v: f"{v:+.2f}%")
    print(t.to_string(index=False))

    print("\n【4】B 口径（进→出 区间持有）与『同持有期基准』的配对比较")
    d = pd.read_csv(OUT / "05aa_exit_decomp_pb.csv")
    d = d[~d["censored"]].copy()
    hh = d["hold"].values
    hh_c = np.clip(np.round(hh / 5) * 5, 5, 900).astype(int)
    d["base_m"] = base_m.loc[hh_c, "中位"].values
    d["base_w"] = base_w.loc[hh_c, "中位"].values
    d["inc_m"] = d["B_ex"] - d["base_m"]
    d["inc_w"] = d["B_ex"] - d["base_w"]
    d["grp"] = np.where(d["hold"] <= H_DIV, f"≤{H_DIV}日", f">{H_DIV}日")

    for g, sub in list(d.groupby("grp")) + [("全部", d)]:
        print(f"\n  ● {g}   n={len(sub)}  持有中位 {sub['hold'].median():.0f} 日")
        print(f"      B 超额中位            = {sub['B_ex'].median()*100:+.2f}%   "
              f"均值 {sub['B_ex'].mean()*100:+.2f}%")
        print(f"      base(市值加权) 中位   = {sub['base_m'].median()*100:+.2f}%   "
              f"⇒ 事件增量中位 = {sub['inc_m'].median()*100:+.2f}%")
        print(f"      base(等权行业) 中位   = {sub['base_w'].median()*100:+.2f}%   "
              f"⇒ 事件增量中位 = {sub['inc_w'].median()*100:+.2f}%")
        x = sub["inc_m"].dropna()
        print(f"      增量>0 占比 {((x>0).mean()*100):.1f}%   "
              f"（≈50% 表示事件与基准无区别）")

    d.to_csv(OUT / "05ac_event_vs_baseline.csv", index=False)

    print("\n" + "=" * 96)
    print("【5】一句话判据")
    allinc = d["inc_m"].median()
    gli = d.loc[d["hold"] > H_DIV, "inc_m"].median()
    print(f"  · 全部 B 事件相对『同持有期 · 市值加权基准』的增量中位 = {allinc*100:+.2f}%")
    print(f"  · >120 日子组增量中位 = {gli*100:+.2f}%")
    print("  · 若两者都贴近 0 ⇒ 现行『超额为负』主要由『中位行业 vs 市值加权市场』的结构性缺口造成，"
          "而非低估这个事件本身。")
    print(f"\n已写出：{OUT / '05ac_event_vs_baseline.csv'}")


if __name__ == "__main__":
    main()
