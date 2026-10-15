#!/usr/bin/env python3
"""
05ad · 同日截面控制：事件超额的"异常部分"到底是多少？（只读）

05ac 发现：无条件基准本身就随持有期单调走负
  （120 日 −4.41%、250 日 −7.41% vs 市值加权市场）
—— 这是"中位行业跑不赢市值加权指数"的结构性缺口，与估值无关。

但 05ac 的基准是"全体行业 × 全体日期"混合的，混杂了【持有期效应】和【时期效应】。
本脚本用最严格的控制：**在同一入场日，取其它行业的截面中位作为对照**。
  增量 = 事件超额 − 同日·其它行业·同持有期 的截面中位
这样同时剔除：持有期效应 + 时期/市场状态效应 + 基准选择。

判据：增量是否显著异于 0（按 entry 月份聚类 t 检验）。
只读，不改任何既有产物。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

DATA = Path("/Users/hyc/quant-data")
OUT = Path("/Users/hyc/quant/industry-radar/research/prosperity")
HORIZONS = [5, 20, 60, 120, 250]
H_DIV = 120


def clustered_t(df, col, freq="ME"):
    d = df[["entry_date", col]].dropna()
    if len(d) < 3:
        return (np.nan, np.nan, np.nan)
    g = d.groupby(pd.Grouper(key="entry_date", freq=freq))[col].mean().dropna()
    if len(g) < 3:
        return (len(g), np.nan, np.nan)
    t, p = stats.ttest_1samp(g.values, 0.0)
    return (len(g), t, p)


def main():
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index() / 100.0
    mkt = (wr.groupby("date").apply(
        lambda d: (d["circ_mv"] * d["weighted_pct"]).sum() / d["circ_mv"].sum(),
        include_groups=False)) / 100.0
    mkt = mkt.reindex(R.index)
    EX = R.sub(mkt, axis=0)
    L = np.log1p(EX)
    lv = L.values
    cols = list(R.columns)
    colpos = {c: k for k, c in enumerate(cols)}
    dpos = {d: k for k, d in enumerate(R.index)}

    ev = pd.read_csv(OUT / "05y_anchor_events_pb.csv")
    ev["entry_date"] = pd.to_datetime(ev["entry_date"])
    ev["i"] = ev["entry_date"].map(dpos)
    ev = ev[ev["i"].notna()].copy()
    ev["i"] = ev["i"].astype(int)

    fwd = {h: np.expm1(L.rolling(h).sum().shift(-h)) for h in HORIZONS}

    print("=" * 100)
    print("【1】A 口径：事件超额 vs『同日 · 其它行业 · 同持有期』截面中位")
    rows = []
    detail = {}
    for h in HORIZONS:
        F = fwd[h]
        ev_h = F.values[ev["i"].values, [colpos[c] for c in ev["ind"]]]
        ctrl = np.array([F.values[i, :].copy() for i in ev["i"].values])
        ctrl = np.where(np.isfinite(ctrl), ctrl, np.nan)
        ctrl_med = np.nanmedian(ctrl, axis=1)
        ev["abn"] = ev_h - ctrl_med
        ev[f"ex_{h}"] = ev_h
        detail[h] = ev["abn"].copy()
        n, t, p = clustered_t(ev.assign(**{"_x": ev["abn"]}), "_x")
        rows.append({"持有": h, "n": int(np.isfinite(ev["abn"]).sum()),
                     "事件超额中位": np.nanmedian(ev_h),
                     "同日截面中位": np.nanmedian(ctrl_med),
                     "增量中位": np.nanmedian(ev["abn"]),
                     "增量均值": np.nanmean(ev["abn"]),
                     "增量>0占比": np.nanmean(ev["abn"] > 0),
                     "簇数": n, "t": t, "p": p})
    t1 = pd.DataFrame(rows)
    for c in ["事件超额中位", "同日截面中位", "增量中位", "增量均值"]:
        t1[c] = (t1[c] * 100).map(lambda v: f"{v:+.2f}%")
    t1["增量>0占比"] = (pd.to_numeric(t1["增量>0占比"]) * 100).map(lambda v: f"{v:.1f}%")
    t1["t"] = pd.to_numeric(t1["t"]).map(lambda v: "-" if pd.isna(v) else f"{v:+.2f}")
    t1["p"] = pd.to_numeric(t1["p"]).map(lambda v: "-" if pd.isna(v) else f"{v:.3f}")
    print(t1.to_string(index=False))

    print("\n【2】B 口径：事件（进→出）超额 vs『同日 · 其它行业 · 相同持有期』截面中位")
    d = pd.read_csv(OUT / "05aa_exit_decomp_pb.csv")
    d = d[~d["censored"]].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"])
    d["exit_date"] = pd.to_datetime(d["exit_date"])
    d["i"] = d["entry_date"].map(dpos)
    d = d[d["i"].notna()].copy()
    d["i"] = d["i"].astype(int)

    inc_m, inc_w = [], []
    for _, r in d.iterrows():
        i = int(r["i"])
        h = int(r["hold"])
        j = i + h
        if j >= len(R.index):
            inc_m.append(np.nan)
            inc_w.append(np.nan)
            continue
        seg = lv[i + 1: j + 1, :]
        # 每一列自身有效天数一致（EX 同列 NaN 模式一致），直接按列求和
        n_ok = np.isfinite(seg).all(axis=0)
        cs = np.full(len(cols), np.nan)
        cs[n_ok] = np.expm1(np.nansum(seg[:, n_ok], axis=0))
        k = colpos[r["ind"]]
        others = np.delete(cs, k)
        inc_m.append(cs[k] - np.nanmedian(others))
        inc_w.append(np.nan)
    d["abn_B"] = inc_m

    d["grp"] = np.where(d["hold"] <= H_DIV, f"≤{H_DIV}日", f">{H_DIV}日")
    for g, sub in list(d.groupby("grp")) + [("全部", d)]:
        x = sub["abn_B"].dropna()
        n, t, p = clustered_t(sub, "abn_B")
        print(f"\n  ● {g}  n={len(x)}  持有中位 {sub['hold'].median():.0f} 日")
        print(f"      B 超额中位       = {sub['B_ex'].median()*100:+.2f}%")
        print(f"      同日截面参考中位 = {(sub['B_ex'] - sub['abn_B']).median()*100:+.2f}%")
        print(f"      ★ 增量中位       = {x.median()*100:+.2f}%   均值 {x.mean()*100:+.2f}%   "
              f"增量>0 占比 {(x>0).mean()*100:.1f}%")
        print(f"      聚类 t = {t:+.2f}   p = {p:.3f}   簇数 {n}")

    d.to_csv(OUT / "05ad_sameDate_control.csv", index=False)
    ev.to_csv(OUT / "05ad_A_increment.csv", index=False)

    print("\n" + "=" * 100)
    print("【3】判据")
    a120 = detail[120]
    print(f"  A_120 增量：中位 {np.nanmedian(a120)*100:+.2f}%  "
          f"均值 {np.nanmean(a120)*100:+.2f}%  >0占比 {np.nanmean(a120>0)*100:.1f}%")
    print("  · 若增量中位≈0 ⇒ 事件本身不含信息，此前『超额为负』全部来自基准/持有期结构。")
    print("  · 若增量显著为正 ⇒ 低估事件相对同日同业确实略好，但绝对水平仍受结构性负漂移支配。")
    print(f"\n已写出：05ad_sameDate_control.csv / 05ad_A_increment.csv")


if __name__ == "__main__":
    main()
