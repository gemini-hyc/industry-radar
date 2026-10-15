#!/usr/bin/env python3
"""
05y · 事件研究锚点敏感性对照（**只含 PIT-valid 口径**）

主人 2026-10-15 纠正：区间「最低点」是事后才能确认的极值，属前视偏差，禁止使用。
本脚本只用两个**阈值穿越事件**作为锚点——二者在发生的当天即可判定：

  进 = 自身 3 年分位**首次** ≤ 20%
  出 = 进之后分位**首次** ≥ 40%（离开便宜区）

三条合法口径：
  A 进 → 固定 h 日      （现行 05 口径）
  B 进 → 出 区间持有     （buy 进 close, sell 出 close；两端 PIT 可观测）
  C 出 → 固定 h 日      （等离开便宜区后进场）

数据/常量与 05 完全一致。只读，不改任何既有产物；产出 CSV 与打印摘要。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

DATA = Path("/Users/hyc/quant-data")
OUT = Path("/Users/hyc/quant/industry-radar/research/prosperity")

LOOKBACK = 750
LOW_Q = 0.20
COOLDOWN_LO = 0.40
MIN_MEMBER = 10
MIN_HIST = 250
HORIZONS = [5, 20, 60, 120, 250]
METRICS = ["pb", "pe_ttm"]


def ts_to_futu(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    code = s.str.split(".").str[0]
    mkt = s.str.split(".").str[-1].str.lower()
    return mkt + "." + code


def load_basic():
    frames = []
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        d = f.stem
        try:
            df = pq.read_table(f).to_pandas()[["ts_code", "pb", "pe_ttm"]]
        except Exception:
            continue
        df["date"] = pd.to_datetime(d)
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", "pb", "pe_ttm"]])
    return pd.concat(frames, ignore_index=True)


def detect_entries(s: pd.Series) -> np.ndarray:
    """进：首次 ≤ LOW_Q，armed/disarmed 状态机（与 05 一致）。"""
    s = s.astype(float)
    out = np.zeros(len(s), dtype=bool)
    armed = True
    for i in range(len(s)):
        v = s.iloc[i]
        if np.isnan(v):
            continue
        if armed and v <= LOW_Q:
            out[i] = True
            armed = False
        elif not armed and v >= COOLDOWN_LO:
            armed = True
    return out


def find_exit(vals: np.ndarray, i: int):
    """出：i 之后首个 ≥ COOLDOWN_LO 的位置；找不到返回 None。"""
    k = i + 1
    while k < len(vals):
        if not np.isnan(vals[k]) and vals[k] >= COOLDOWN_LO:
            return k
        k += 1
    return None


def summarize(series, name):
    x = series.dropna()
    if len(x) == 0:
        return None
    return {"口径": name, "n": len(x), "中位": x.median(), "均值": x.mean(),
            "胜率": (x > 0).mean()}


def clustered_t(df, col, freq="ME"):
    """按 entry 月份聚类，对簇均值做单样本 t 检验（n 为簇数）。"""
    from scipy import stats
    d = df[["entry_date", col]].dropna()
    if len(d) < 3:
        return (np.nan, np.nan, np.nan)
    g = d.groupby(pd.Grouper(key="entry_date", freq=freq))[col].mean().dropna()
    if len(g) < 3:
        return (len(g), np.nan, np.nan)
    t, p = stats.ttest_1samp(g.values, 0.0)
    return (len(g), t, p)


def main():
    basic = load_basic()
    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])

    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index() / 100.0
    mkt = (wr.groupby("date").apply(
        lambda d: (d["circ_mv"] * d["weighted_pct"]).sum() / d["circ_mv"].sum(),
        include_groups=False)) / 100.0
    mkt = mkt.reindex(R.index)
    EX = R.sub(mkt, axis=0)
    print("EX 中 NaN 占比：", f"{EX.isna().mean().mean()*100:.3f}%")

    m = mem.merge(basic, on=["date", "stock_code"], how="inner")
    all_rows = []

    for metric in METRICS:
        mv = m[["date", "ind_code", metric]].dropna(subset=[metric])
        mv = mv[mv[metric] > 0]
        iv = mv.groupby(["date", "ind_code"])[metric].median().reset_index().rename(columns={metric: "val"})
        nn = mv.groupby(["date", "ind_code"]).size().rename("n").reset_index()
        iv = iv.merge(nn, on=["date", "ind_code"])
        V = iv.pivot_table(index="date", columns="ind_code", values="val")
        N = iv.pivot_table(index="date", columns="ind_code", values="n")
        V = V.where(N >= MIN_MEMBER).reindex(index=R.index, columns=R.columns)
        Vrank = V.rolling(LOOKBACK, min_periods=MIN_HIST).rank(pct=True)

        fwd = {h: (1 + EX).rolling(h).apply(np.prod, raw=True).shift(-h) - 1 for h in HORIZONS}

        rows = []
        exv = EX.values
        cols = list(R.columns)
        colpos = {c: k for k, c in enumerate(cols)}
        dates = R.index
        dpos = {d: k for k, d in enumerate(dates)}

        for col in Vrank.columns:
            s = Vrank[col]
            vals = s.values
            for i in np.where(detect_entries(s))[0]:
                d = dates[i]
                rec = {"metric": metric, "ind": col, "entry_date": d, "entry_pct": s.iloc[i]}
                # A: 进 → 固定 h
                for h in HORIZONS:
                    rec[f"A_{h}"] = fwd[h].iloc[i][col]
                # B/C 需要「出」
                j = find_exit(vals, i)
                if j is None:
                    rec["B_hold"] = np.nan
                    rec["B_ex"] = np.nan
                    for h in HORIZONS:
                        rec[f"C_{h}"] = np.nan
                    rec["exit_date"] = pd.NaT
                else:
                    dj = dates[j]
                    rec["exit_date"] = dj
                    rec["B_hold"] = j - i
                    # B：i close 买、j close 卖，持有 (i+1 .. j)
                    win = exv[i + 1: j + 1, colpos[col]]
                    rec["B_ex"] = np.prod(1 + win) - 1 if len(win) else np.nan
                    # C：j close 买，持有 (j+1 .. j+h)
                    for h in HORIZONS:
                        rec[f"C_{h}"] = fwd[h].iloc[j][col]
                rows.append(rec)

        df = pd.DataFrame(rows)
        df.to_csv(OUT / f"05y_anchor_events_{metric}.csv", index=False)
        all_rows.append(df)

        print(f"\n{'='*78}\n指标 = {metric}   进事件数 = {len(df)}   "
              f"（其中有『出』的 = {df['B_ex'].notna().sum()}）\n{'='*78}")

        cols_cal = [(f"A_{h}", f"A 进→{h}日") for h in HORIZONS]
        cols_cal += [("B_ex", "B 进→出 区间持有")]
        cols_cal += [(f"C_{h}", f"C 出→{h}日") for h in HORIZONS]

        summ = []
        for c, name in cols_cal:
            r = summarize(df[c], name)
            if not r:
                continue
            cn, ct, cp = clustered_t(df, c)
            r["簇数"] = cn
            r["t"] = ct
            r["p"] = cp
            r["显著"] = "" if (cp is None or np.isnan(cp)) else ("**" if cp < 0.05 else "")
            summ.append(r)

        tab = pd.DataFrame(summ)
        tab["中位"] = (tab["中位"] * 100).map(lambda v: f"{v:+.2f}%")
        tab["均值"] = (tab["均值"] * 100).map(lambda v: f"{v:+.2f}%")
        tab["胜率"] = (tab["胜率"] * 100).map(lambda v: f"{v:.1f}%")
        tab["t"] = pd.to_numeric(tab["t"], errors="coerce").map(
            lambda v: "-" if pd.isna(v) else f"{v:+.2f}")
        tab["p"] = pd.to_numeric(tab["p"], errors="coerce").map(
            lambda v: "-" if pd.isna(v) else f"{v:.3f}")
        tab = tab[["口径", "n", "簇数", "中位", "均值", "胜率", "t", "p", "显著"]]
        print(tab.to_string(index=False))
        if metric == "pb":
            bh = df["B_hold"].dropna()
            print(f"\nB 口径持有天数：中位 {bh.median():.0f} 天，均值 {bh.mean():.1f} 天，最大 {bh.max():.0f} 天")

    pd.concat(all_rows, ignore_index=True).to_csv(OUT / "05y_anchor_events_all.csv", index=False)
    print(f"\n已写出：05y_anchor_events_pb.csv / _pe_ttm.csv / _all.csv")


if __name__ == "__main__":
    main()
