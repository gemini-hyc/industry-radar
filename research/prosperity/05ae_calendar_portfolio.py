#!/usr/bin/env python3
"""
05ae · 日历时间组合检验：无偏地判定「买低估、卖出区间」这条规则到底赚不赚（只读）

为什么必须做这一步：
  05ad 用「同日 · 其它行业 · 相同持有期」作对照，但 B 口径的**持有期本身由出场规则决定**
  （涨回区间才卖）⇒ 窗口长度是"结果"的函数，按窗口对照仍被选择效应污染。
  唯一无偏的检验：**日历时间组合**——
    每一天，等权持有所有「今日处于持仓中（已入场且未出场）」的行业，
    记当日超额收益，得出一条日频净值曲线。持仓集合只由过去信息决定 ⇒ PIT 干净。

判据：日频超额均值是否显著异于 0（含 Newey-West 修正）；净值曲线是否单调走高。
输入：05y 的 PB 事件表（entry_date / exit_date）。只读，不改任何既有产物。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

DATA = Path("/Users/hyc/quant-data")
OUT = Path("/Users/hyc/quant/industry-radar/research/prosperity")


def nw_t(x: np.ndarray, lag: int = 5):
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return (np.nan, np.nan)
    m = x.mean()
    e = x - m
    g0 = (e ** 2).mean()
    var = g0
    for L in range(1, lag + 1):
        gl = (e[L:] * e[:-L]).mean()
        var += 2 * (1 - L / (lag + 1)) * gl
    se = np.sqrt(var / n)
    return (m / se, se)


def main():
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index() / 100.0
    mkt = (wr.groupby("date").apply(
        lambda d: (d["circ_mv"] * d["weighted_pct"]).sum() / d["circ_mv"].sum(),
        include_groups=False)) / 100.0
    mkt = mkt.reindex(R.index)
    EX = R.sub(mkt, axis=0)

    dates = R.index
    cols = list(R.columns)
    colpos = {c: k for k, c in enumerate(cols)}
    dpos = {d: k for k, d in enumerate(dates)}

    ev = pd.read_csv(OUT / "05y_anchor_events_pb.csv")
    ev["entry_date"] = pd.to_datetime(ev["entry_date"])
    ev["exit_date"] = pd.to_datetime(ev["exit_date"])
    done = ev[ev["exit_date"].notna()].copy()
    cens = ev[ev["exit_date"].isna()].copy()
    print(f"事件总数 {len(ev)}  已完成（含出场）{len(done)}  删失 {len(cens)}")
    print("注：删失样本（从未回到 40%）无法确定出场，本检验不纳入 —— 这本身是一个已存在的乐观偏差。")

    nD, nC = len(dates), len(cols)
    mask = np.zeros((nD, nC), dtype=bool)
    opened = 0
    for _, r in done.iterrows():
        i = dpos.get(r["entry_date"])
        j = dpos.get(r["exit_date"])
        if i is None or j is None or j <= i:
            continue
        mask[i + 1: j + 1, colpos[r["ind"]]] = True
        opened += 1
    print(f"已布仓事件 {opened}")

    exv = EX.values
    hold_n = mask.sum(axis=1)
    with np.errstate(invalid="ignore"):
        port = np.where(hold_n > 0, np.nansum(np.where(mask, exv, 0.0), axis=1) / np.maximum(hold_n, 1), np.nan)
    s = pd.Series(port, index=dates).dropna()
    print(f"\n有持仓的交易日数 = {len(s)} / {nD}  （覆盖率 {len(s)/nD*100:.1f}%）")
    print(f"平均每日持仓行业数 = {hold_n[hold_n>0].mean():.1f}")

    print("\n" + "=" * 92)
    print("【1】日历时间组合：日频超额收益（等权，仅统计有持仓的日子）")
    print(f"  日均超额 = {s.mean()*100:+.4f}%   日标准差 = {s.std()*100:.3f}%")
    t0 = s.mean() / s.std() * np.sqrt(len(s))
    tn, se = nw_t(s.values, lag=5)
    from scipy import stats as st
    pnw = 2 * st.norm.sf(abs(tn)) if np.isfinite(tn) else np.nan
    print(f"  朴素 t = {t0:+.2f}   Newey-West(lag5) t = {tn:+.2f}   p ≈ {pnw:.4f}")
    print(f"  年化（按 244 交易日）= {s.mean()*244*100:+.2f}%  （仅计持仓日，未扣空仓期）")
    cum = (1 + s).cumprod()
    print(f"  持仓日累计净值 = {cum.iloc[-1]:.4f}  ({(cum.iloc[-1]-1)*100:+.2f}%)")
    print(f"  正的交易日占比 = {(s>0).mean()*100:.1f}%")

    print("\n【2】逐年（仅持仓日）")
    y = s.groupby(s.index.year).agg(天数="size", 日均超额="mean")
    y["日均超额"] = (y["日均超额"] * 100).map(lambda v: f"{v:+.4f}%")
    y["年化近似"] = (s.groupby(s.index.year).mean() * 244 * 100).map(lambda v: f"{v:+.1f}%")
    print(y.to_string())

    print("\n【3】对照：同期『全行业中位 vs 市值加权市场』的日频超额（无条件基准）")
    ex_med = EX.median(axis=1)
    print(f"  无条件下日均超额中位 = {ex_med.mean()*100:+.4f}%")
    print(f"  本组合日均超额      = {s.mean()*100:+.4f}%")
    print(f"  ⇒ 组合相对无条件基准的日频增量 = {(s.mean()-ex_med.mean())*100:+.4f}%")

    print("\n" + "=" * 92)
    print("【4】判据")
    if tn > 1.96:
        print("  · 日频超额显著为正 ⇒ 该规则在日历时间上确实赚钱（需进一步查是否被少数时期驱动）。")
    elif tn < -1.96:
        print("  · 日频超额显著为负 ⇒ 规则亏钱。")
    else:
        print("  · 日频超额与 0 无显著差异 ⇒ **按持有日计算，这条规则没有可交易的正向漂移**；")
        print("    05ad 里看到的『正向增量』只是『按出场结果切窗口』带来的选择效应。")

    pd.DataFrame({"date": s.index, "port_ex": s.values}).to_csv(
        OUT / "05ae_calendar_portfolio.csv", index=False)
    print(f"\n已写出：{OUT / '05ae_calendar_portfolio.csv'}")


if __name__ == "__main__":
    main()
