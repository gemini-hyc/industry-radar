#!/usr/bin/env python3
"""
05aj · 用**已上线的修正后官方表**重跑「低估事件」头条数字（只读）

与 05ag 的区别：05ag 用的是研究侧手搓的 05af 重算结果；本脚本直接读
~/quant-data/industry/industry_weighted_returns.parquet（修正后正式产物）
与它的同口径备份（ _backup_20261015_weight_fix/*.bak-sameday-weight ），
确认【产线修复】与【研究侧预估】结论一致。

事件定义与 05/05y/05ag 完全一致：PB 自身滚动 750 日分位首次 ≤20% 为进、
≥40% 为 cooling re-arm；持有期收益取 entry 日**之后** h 个交易日。

另附带一件事：市场基准的构造方式也要同步修正
  mkt_today = Σ w_t · r_t   （旧做法，含 +Var_w 偏差 ≈ +0.022pp/日）
  mkt_prev  = Σ w_{t-1} · r_t（正确）
两种都算，便于看出「修表」和「修基准」各自的贡献。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

DATA = Path("/Users/hyc/quant-data")
OUT = Path(__file__).parent
BACKUP = (DATA / "industry" / "_backup_20261015_weight_fix"
          / "industry_weighted_returns.parquet.bak-sameday-weight")
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


def load_matrix(path):
    """→ (R, mkt_today, mkt_prev)"""
    d = pd.read_parquet(path)
    d["date"] = pd.to_datetime(d["date"])
    R = (d.pivot_table(index="date", columns="ind_code", values="weighted_pct")
         .sort_index() / 100.0)
    C = d.pivot_table(index="date", columns="ind_code", values="circ_mv")
    C = C.reindex(index=R.index, columns=R.columns)
    Cp = C.shift(1)
    mt = (R * C).sum(axis=1) / C.sum(axis=1)
    mp = (R * Cp).sum(axis=1) / Cp.sum(axis=1)
    return R, mt, mp.dropna()


def clustered_t(x, freq="ME"):
    if len(x) < 3:
        return np.nan, np.nan
    g = x.groupby(pd.Grouper(level=0, freq=freq)).mean().dropna()
    if len(g) < 3:
        return np.nan, np.nan
    return stats.ttest_1samp(g.values, 0.0)


def main():
    Ro, mto, mpo = load_matrix(BACKUP)
    Rn, mtn, mpn = load_matrix(DATA / "industry" / "industry_weighted_returns.parquet")

    print("=" * 96)
    print(f"区间 {Rn.index.min().date()} ~ {Rn.index.max().date()}   "
          f"交易日 {len(Rn)}  行业 {Rn.shape[1]}")
    for nm, m in [("旧表·今日加权基准", mto), ("旧表·前日加权基准", mpo),
                  ("新表·今日加权基准", mtn), ("新表·前日加权基准", mpn)]:
        print(f"  {nm:<18} 市场累计 {(1 + m).prod() - 1:+9.1%}")
    ix = pd.read_parquet(DATA / "index_daily" / "000985.CSI.parquet")
    dc = [c for c in ix.columns if "date" in c.lower()][0]
    ix[dc] = pd.to_datetime(ix[dc])
    ix = ix.set_index(dc).sort_index()
    cc = [x for x in ix.columns if x.lower().startswith("close")][0]
    ir = ix[cc].pct_change().reindex(mpn.index).dropna()
    print(f"  中证全指同区间     {(1 + ir).prod() - 1:+9.1%}")

    # ── PB 面板 ──
    frames = []
    lo = Rn.index.min().strftime("%Y-%m-%d")
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        if f.stem < lo:
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
    iv = mv.groupby(["date", "ind_code"])["pb"].median().rename("val").reset_index()
    nn = mv.groupby(["date", "ind_code"]).size().rename("n").reset_index()
    iv = iv.merge(nn, on=["date", "ind_code"])
    V = iv.pivot_table(index="date", columns="ind_code", values="val")
    N = iv.pivot_table(index="date", columns="ind_code", values="n")
    V = V.where(N >= MIN_MEMBER).reindex(index=Rn.index, columns=Rn.columns)
    Vrank = V.rolling(LOOKBACK, min_periods=MIN_HIST).rank(pct=True)
    print(f"\nPB 面板：{V.notna().sum().sum():,} 个可用行业-天")

    colpos = {c: k for k, c in enumerate(Rn.columns)}
    rows = []
    for tag, R, mt, mp in [("旧表", Ro, mto, mpo), ("新表", Rn, mtn, mpn)]:
        for btag, mkt in [("今日加权基准", mt), ("前日加权基准", mp)]:
            EX = R.sub(mkt.reindex(R.index), axis=0)
            lg = np.log1p(EX)
            fwd = {h: np.expm1(lg.rolling(h).sum().shift(-h)) for h in HORIZONS}
            recs = []
            for col in Vrank.columns:
                s = Vrank[col]
                idx = np.where(detect_entries(s))[0]
                if len(idx) == 0:
                    continue
                for i in idx:
                    r = {"ind": col, "entry_date": EX.index[i], "i": i}
                    for h in HORIZONS:
                        r[f"A_{h}"] = fwd[h].iat[i, colpos[col]]
                    recs.append(r)
            d = pd.DataFrame(recs)
            for h in HORIZONS:
                e = d[f"A_{h}"].dropna()
                if len(e) == 0:
                    continue
                F = fwd[h].values
                ii = d.loc[e.index, "i"].values
                kk = [colpos[c] for c in d.loc[e.index, "ind"]]
                ev_h = F[ii, kk]
                ctrl = np.array([F[i, :] for i in ii], dtype=float)
                abn = ev_h - np.nanmedian(ctrl, axis=1)
                s2 = pd.Series(abn, index=pd.MultiIndex.from_arrays(
                    [EX.index[ii], d.loc[e.index, "ind"].values]))
                t, p = clustered_t(s2, freq="ME") if False else clustered_t(
                    pd.Series(abn, index=EX.index[ii]))
                rows.append({"表": tag, "基准": btag, "h": h, "n": len(e),
                             "事件超额中位": e.median(), "胜率": (e > 0).mean(),
                             "同日增量中位": np.nanmedian(abn), "t": t, "p": p})
            if tag == "新表" and btag == "前日加权基准":
                d.to_csv(OUT / "05aj_events_new.csv", index=False)

    t = pd.DataFrame(rows)
    print("\n" + "=" * 96)
    print("【1】A 口径（进 → 固定 h 日）低估事件超额")
    piv = t.pivot_table(index=["h"], columns=["表", "基准"],
                        values=["事件超额中位", "胜率", "同日增量中位"])
    for h in HORIZONS:
        if h not in piv.index:
            continue
        line = f"  h={h:<4}"
        for tab in ["旧表", "新表"]:
            for b in ["今日加权基准", "前日加权基准"]:
                try:
                    v = piv.loc[h, ("事件超额中位", tab, b)]
                    w = piv.loc[h, ("胜率", tab, b)]
                    a = piv.loc[h, ("同日增量中位", tab, b)]
                    line += f" | {tab}/{b[:2]}: {v*100:+6.2f}% 胜{w*100:4.1f}% 增{a*100:+5.2f}%"
                except Exception:
                    pass
        print(line)

    print("\n  t / p（按 entry 月份聚类，针对「同日增量」）")
    for _, r in t[t["h"].isin([20, 120, 250])].iterrows():
        print(f"    {r['表']}/{r['基准']:<12} h={r['h']:<4} t={r['t']:+.2f}  p={r['p']:.3f}")

    # ── 无条件基线 ──
    print("\n" + "=" * 96)
    print("【2】无条件基线：任意行业·任意日·持有 h 日的中位超额（新表）")
    for btag, mkt in [("今日加权基准", mtn), ("前日加权基准", mpn)]:
        EX = Rn.sub(mkt.reindex(Rn.index), axis=0)
        lg = np.log1p(EX)
        outs = []
        for h in HORIZONS:
            v = np.expm1(lg.rolling(h).sum().shift(-h)).stack().dropna()
            outs.append(f"h={h}: {v.median()*100:+.2f}%")
        print(f"  {btag}: " + "   ".join(outs))

    # ── 日历时间组合 ──
    print("\n" + "=" * 96)
    print("【3】日历时间组合（B 口径：进→出，等权持有人在仓行业）")
    d = pd.read_csv(OUT / "05aj_events_new.csv")
    dexit = {}
    vals_dict = {c: Vrank[c].values for c in Vrank.columns}
    date_pos = {x: k for k, x in enumerate(Rn.index)}
    mask = np.zeros((len(Rn.index), len(Rn.columns)), dtype=bool)
    for _, r in d.iterrows():
        col = r["ind"]
        i = np.searchsorted(Vrank.index, pd.Timestamp(r["entry_date"]))
        vals = vals_dict[col]
        k = i + 1
        while k < len(vals):
            if not np.isnan(vals[k]) and vals[k] >= COOLDOWN_LO:
                break
            k += 1
        else:
            continue
        j = date_pos.get(Vrank.index[k])
        if j is not None:
            mask[i + 1: j + 1, colpos[col]] = True
    for btag, mkt in [("今日加权基准", mtn), ("前日加权基准", mpn)]:
        EX = Rn.sub(mkt.reindex(Rn.index), axis=0)
        exv = EX.values
        hn = mask.sum(axis=1)
        port = np.where(hn > 0, np.nansum(np.where(mask, exv, 0.0), axis=1) / np.maximum(hn, 1), np.nan)
        ps = pd.Series(port, index=Rn.index).dropna()
        tn = ps.mean() / ps.std() * np.sqrt(len(ps))
        print(f"  {btag}: 日均超额 {ps.mean()*100:+.4f}%   t={tn:+.2f}   "
              f"累计净值 {(1 + ps).prod():.4f}   持仓日 {len(ps)}")

    t.to_csv(OUT / "05aj_buggy_vs_fixed_official.csv", index=False, encoding="utf-8-sig")
    print(f"\n✓ 落盘 {OUT / '05aj_buggy_vs_fixed_official.csv'}")


if __name__ == "__main__":
    main()
