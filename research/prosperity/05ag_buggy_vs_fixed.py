#!/usr/bin/env python3
"""
05ag · 复验：修正加权口径后，「低估事件」的超额结论是否还成立？（只读）

输入：05af 重建的 ret_buggy / ret_fixed 与 mkt_buggy / mkt_fixed。
同一条事件定义（PB 自身 3 年分位首次 ≤20% 为进、首次 ≥40% 为出，与 05/05y 完全一致），
在两种收益口径下并排跑：
  A 口径  进→固定 h 日
  A 增量  事件超额 − 同日·其它行业·同持有期 截面中位（剔除基准与时期效应）
  日历时间组合  每天等权持有所有在仓行业（唯一无偏的策略检验）
判据：若修正后结论不变 ⇒ 原结论稳健；若显著改变 ⇒ 01~09 需按修正口径重跑。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

DATA = Path("/Users/hyc/quant-data")
OUT = Path("/Users/hyc/quant/industry-radar/research/prosperity")
LOOKBACK, LOW_Q, COOLDOWN_LO, MIN_MEMBER, MIN_HIST = 750, 0.20, 0.40, 10, 250
HORIZONS = [5, 20, 60, 120, 250]


def ts_to_futu(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    return s.str.split(".").str[-1].str.lower() + "." + s.str.split(".").str[0]


def detect_entries(s: pd.Series) -> np.ndarray:
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


def find_exit(vals, i):
    k = i + 1
    while k < len(vals):
        if not np.isnan(vals[k]) and vals[k] >= COOLDOWN_LO:
            return k
        k += 1
    return None


def clustered_t(d, col, freq="ME"):
    x = d[["entry_date", col]].dropna()
    if len(x) < 3:
        return (np.nan, np.nan, np.nan)
    g = x.groupby(pd.Grouper(key="entry_date", freq=freq))[col].mean().dropna()
    if len(g) < 3:
        return (len(g), np.nan, np.nan)
    t, p = stats.ttest_1samp(g.values, 0.0)
    return (len(g), t, p)


def main():
    ind = pd.read_parquet(OUT / "05af_reweighted_returns.parquet")
    ind["date"] = pd.to_datetime(ind["date"])
    mkt = pd.read_parquet(OUT / "05af_reweighted_mkt.parquet")
    mkt["date"] = pd.to_datetime(mkt["date"])

    Rf = ind.pivot_table(index="date", columns="ind_code", values="ret_fixed").sort_index()
    Rb = ind.pivot_table(index="date", columns="ind_code", values="ret_buggy").sort_index()
    mf = mkt.set_index("date")["mkt_fixed"].reindex(Rf.index)
    mb = mkt.set_index("date")["mkt_buggy"].reindex(Rf.index)
    EXf = Rf.sub(mf, axis=0)
    EXb = Rb.sub(mb, axis=0)

    print("=" * 100)
    print(f"区间 {Rf.index.min().date()} ~ {Rf.index.max().date()}  交易日 {len(Rf)}  行业 {Rf.shape[1]}")
    print(f"市值累计：buggy {(1+mb).prod()-1:+.1%}   fixed {(1+mf).prod()-1:+.1%}")
    idx = pd.read_parquet(DATA / "index_daily" / "000985.CSI.parquet")
    dc = [c for c in idx.columns if "date" in c.lower()][0]
    idx[dc] = pd.to_datetime(idx[dc])
    idx = idx.set_index(dc).sort_index()
    cc = [x for x in idx.columns if x.lower().startswith("close")][0]
    ir = idx[cc].pct_change().reindex(Rf.index).dropna()
    print(f"对照中证全指累计（同区间）{(1+ir).prod()-1:+.1%}")

    # ── PB 分位面板（与 05/05aa 完全一致）──
    frames = []
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        if f.stem < Rf.index.min().strftime("%Y-%m-%d"):
            continue
        try:
            df = pq.read_table(f).to_pandas()[["ts_code", "pb"]]
        except Exception:
            continue
        df["date"] = pd.to_datetime(f.stem)
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", "pb"]])
    basic = pd.concat(frames, ignore_index=True)
    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])
    m = mem.merge(basic, on=["date", "stock_code"], how="inner")
    mv = m.dropna(subset=["pb"])
    mv = mv[mv["pb"] > 0]
    iv = mv.groupby(["date", "ind_code"])["pb"].median().reset_index().rename(columns={"pb": "val"})
    nn = mv.groupby(["date", "ind_code"]).size().rename("n").reset_index()
    iv = iv.merge(nn, on=["date", "ind_code"])
    V = iv.pivot_table(index="date", columns="ind_code", values="val")
    N = iv.pivot_table(index="date", columns="ind_code", values="n")
    V = V.where(N >= MIN_MEMBER).reindex(index=Rf.index, columns=Rf.columns)
    Vrank = V.rolling(LOOKBACK, min_periods=MIN_HIST).rank(pct=True)

    cols = list(Rf.columns)
    colpos = {c: k for k, c in enumerate(cols)}
    dates = Rf.index
    dpos = {d: k for k, d in enumerate(dates)}

    print("\n" + "=" * 100)
    print("【1】A 口径事件超额：修正前后并排（中位）")
    rows = []
    for tag, EX in [("buggy(现行)", EXb), ("fixed(修正)", EXf)]:
        lg = np.log1p(EX)
        fwd = {h: np.expm1(lg.rolling(h).sum().shift(-h)) for h in HORIZONS}
        evs = []
        for col in Vrank.columns:
            s = Vrank[col]
            vals = s.values
            for i in np.where(detect_entries(s))[0]:
                rec = {"ind": col, "entry_date": dates[i], "i": i}
                for h in HORIZONS:
                    rec[f"A_{h}"] = fwd[h].iat[i, colpos[col]]
                j = find_exit(vals, i)
                rec["hold"] = (j - i) if j is not None else np.nan
                rec["exit_date"] = dates[j] if j is not None else pd.NaT
                evs.append(rec)
        d = pd.DataFrame(evs)
        for h in HORIZONS:
            e = d[f"A_{h}"].dropna()
            F = fwd[h].values
            ii = d.loc[e.index, "i"].values
            kk = [colpos[c] for c in d.loc[e.index, "ind"]]
            ev_h = F[ii, kk]
            ctrl = np.array([F[i, :] for i in ii])
            ctrl = np.where(np.isfinite(ctrl), ctrl, np.nan)
            abn = ev_h - np.nanmedian(ctrl, axis=1)
            d.loc[e.index, "abn"] = abn
            n, t, p = clustered_t(d.loc[e.index].assign(_a=abn), "_a")
            rows.append({"口径": tag, "h": h, "n": len(e),
                         "事件超额中位": e.median(), "增量中位": np.nanmedian(abn),
                         "增量均值": np.nanmean(abn), "t": t, "p": p, "簇": n})
        if tag.startswith("fixed"):
            d.to_csv(OUT / "05ag_events_fixed.csv", index=False)
            print("\n【2】B 口径（进→出）与日历时间组合：修正前后并排")
            mask = np.zeros((len(dates), len(cols)), dtype=bool)
            for _, r in d.iterrows():
                if pd.isna(r["exit_date"]):
                    continue
                i = dpos[r["entry_date"]]
                j = dpos[r["exit_date"]]
                mask[i + 1: j + 1, colpos[r["ind"]]] = True
            for t2, EXx in [("buggy(现行)", EXb), ("fixed(修正)", EXf)]:
                exv = EXx.values
                hn = mask.sum(axis=1)
                port = np.where(hn > 0, np.nansum(np.where(mask, exv, 0.0), axis=1) / np.maximum(hn, 1), np.nan)
                ps = pd.Series(port, index=dates).dropna()
                tn = ps.mean() / ps.std() * np.sqrt(len(ps))
                print(f"  {t2}: 日均超额 {ps.mean()*100:+.4f}%   t={tn:+.2f}   "
                      f"累计净值 {(1+ps).prod():.4f}   持仓日 {len(ps)}")
    t = pd.DataFrame(rows)
    t["事件超额中位"] = (t["事件超额中位"] * 100).map(lambda v: f"{v:+.2f}%")
    t["增量中位"] = (t["增量中位"] * 100).map(lambda v: f"{v:+.2f}%")
    t["增量均值"] = (t["增量均值"] * 100).map(lambda v: f"{v:+.2f}%")
    t["t"] = pd.to_numeric(t["t"]).map(lambda v: "-" if pd.isna(v) else f"{v:+.2f}")
    t["p"] = pd.to_numeric(t["p"]).map(lambda v: "-" if pd.isna(v) else f"{v:.3f}")
    print(t.to_string(index=False))
    t.to_csv(OUT / "05ag_buggy_vs_fixed.csv", index=False)
    print(f"\n已写出：05ag_buggy_vs_fixed.csv / 05ag_events_fixed.csv")


if __name__ == "__main__":
    main()
