#!/usr/bin/env python3
"""
诊断脚本（只读，不改任何产物）：
复刻 05_valuation_event_study.py 的 detect_entries 逻辑，把「低估事件」的
真实 entry 日期 / 当日自身历史分位 / 区间长度 / 距 re-arm 天数 / 其后 120 日真实超额
逐条打印，用于验证「t=0 到底锚在区间哪一天」。

常量与 05 完全一致：LOOKBACK=750, LOW_Q=0.20, COOLDOWN_LO=0.40, MIN_MEMBER=10。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

DATA = Path("/Users/hyc/quant-data")
OUT = Path("/Users/hyc/quant/industry-radar/research/prosperity")

METRIC = "pb"
LOOKBACK = 750
LOW_Q = 0.20
COOLDOWN_LO = 0.40
MIN_MEMBER = 10
MIN_HIST = 250
H = 120


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
            df = pq.read_table(f).to_pandas()[["ts_code", "pb"]]
        except Exception:
            continue
        df["date"] = pd.to_datetime(d)
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", "pb"]])
    return pd.concat(frames, ignore_index=True)


def detect_entries(s: pd.Series, low: bool = True) -> np.ndarray:
    """与 05 完全一致的状态机。"""
    s = s.astype(float)
    n = len(s)
    out = np.zeros(n, dtype=bool)
    armed = True
    for i in range(n):
        v = s.iloc[i]
        if np.isnan(v):
            continue
        if armed and v <= LOW_Q:
            out[i] = True
            armed = False
        elif not armed and v >= COOLDOWN_LO:
            armed = True
    return out


def episode_meta(s: pd.Series, i: int):
    """事件索引 i 的 (连续<=LOW_Q天数, 距re-arm天数)。均为向后扫描，含 i 本身。"""
    vals = s.values
    n = len(vals)
    # 连续便宜区间长度（从 i 起连续 v<=LOW_Q）
    runlen = 0
    j = i
    while j < n and not np.isnan(vals[j]) and vals[j] <= LOW_Q:
        runlen += 1
        j += 1
    # 距 re-arm：第一个 v>=COOLDOWN_LO 的位置
    to_rearm = None
    k = i + 1
    while k < n:
        if not np.isnan(vals[k]) and vals[k] >= COOLDOWN_LO:
            to_rearm = k - i
            break
        k += 1
    return runlen, to_rearm


def main():
    basic = load_basic()
    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    print("wr 列：", list(wr.columns))
    print("mem 列：", list(mem.columns))
    print("ind_code 样例：", wr["ind_code"].dropna().unique()[:5] if "ind_code" in wr.columns else "N/A")

    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index() / 100.0
    mkt = (wr.groupby("date").apply(
        lambda d: (d["circ_mv"] * d["weighted_pct"]).sum() / d["circ_mv"].sum(),
        include_groups=False)) / 100.0
    mkt = mkt.reindex(R.index)
    EX = R.sub(mkt, axis=0)

    m = mem.merge(basic, on=["date", "stock_code"], how="inner")
    mv = m[["date", "ind_code", METRIC]].dropna(subset=[METRIC])
    mv = mv[mv[METRIC] > 0]
    iv = mv.groupby(["date", "ind_code"])[METRIC].median().reset_index().rename(columns={METRIC: "val"})
    nn = mv.groupby(["date", "ind_code"]).size().rename("n").reset_index()
    iv = iv.merge(nn, on=["date", "ind_code"])
    V = iv.pivot_table(index="date", columns="ind_code", values="val")
    N = iv.pivot_table(index="date", columns="ind_code", values="n")
    V = V.where(N >= MIN_MEMBER)
    V = V.reindex(index=R.index, columns=R.columns)
    Vrank = V.rolling(LOOKBACK, min_periods=MIN_HIST).rank(pct=True)

    fwd_ex = (1 + EX).rolling(H).apply(np.prod, raw=True).shift(-H) - 1

    # 统计每行业事件数
    per_ind = {}
    detail = []
    for col in Vrank.columns:
        s = Vrank[col]
        events = np.where(detect_entries(s, low=True))[0]
        if len(events) == 0:
            continue
        per_ind[col] = len(events)
        for i in events:
            d = Vrank.index[i]
            pct = s.iloc[i]
            runlen, to_rearm = episode_meta(s, i)
            ex = fwd_ex.iloc[i][col] if col in fwd_ex.columns else np.nan
            detail.append({"ind": col, "date": d, "pct": pct, "runlen": runlen,
                           "to_rearm": to_rearm, "ex120": ex})

    dfall = pd.DataFrame(detail)
    dfall = dfall.sort_values(["date", "ind"]).reset_index(drop=True)
    dfall.to_csv(OUT / "05x_low_events.csv", index=False)
    ex_ok = dfall["ex120"].dropna()
    print(f"\n=== 总事件数（PB 低估，全行业）: {len(dfall)} ===")
    print(f"其中有未来120日数据的: {len(ex_ok)}")
    print(f"ex120 中位 = {ex_ok.median():.4f}  胜率(ex120>0) = {(ex_ok > 0).mean():.3f}")
    print(f"区间长度 runlen: 中位 {dfall['runlen'].median():.0f}  最大 {dfall['runlen'].max()}  "
          f"=1 的比例 {(dfall['runlen'] == 1).mean():.3f}")
    print(f"（已写出全量事件 CSV：research/prosperity/05x_low_events.csv）")

    # 触发最多的前 3 个行业
    top = sorted(per_ind.items(), key=lambda kv: -kv[1])[:3]
    print("\n事件数最多的前 3 个行业：")
    for col, cnt in top:
        print(f"  {col}: {cnt} 个事件")

    for col, _ in top:
        sub = dfall[dfall["ind"] == col].sort_values("date")
        print(f"\n--- 行业 {col}（共 {len(sub)} 个低估事件）---")
        print(f"{'entry_date':<12}{'分位':>7}{'区间天数':>9}{'距rearm':>9}{'其后120日超额':>14}")
        for _, r in sub.iterrows():
            pct_s = f"{r['pct']*100:5.1f}%"
            ra = "-" if pd.isna(r["to_rearm"]) else f"{int(r['to_rearm'])}"
            ex = "-" if pd.isna(r["ex120"]) else f"{r['ex120']*100:6.2f}%"
            print(f"{str(r['date'].date()):<12}{pct_s:>7}{int(r['runlen']):>9}{ra:>9}{ex:>14}")

    # 交叉验证：这些事件的 ex120 相对市场是否整体为负
    print("\n=== 交叉验证 ===")
    print("以上全部事件 ex120 中位 =", f"{ex_ok.median()*100:.2f}%",
          " 胜率 =", f"{(ex_ok>0).mean()*100:.1f}%", f" (n={len(ex_ok)})")


if __name__ == "__main__":
    main()
