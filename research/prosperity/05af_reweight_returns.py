#!/usr/bin/env python3
"""
05af · 重建行业收益：修正「当日市值加权」为「前一日市值加权」（只读 + 落缓存）

背景（2026-10-15 发现）：
  scripts/compute_weighted_returns.py 第 128/135 行：
      weighted_pct = Σ(pctChg_t × circ_mv_t) / Σ(circ_mv_t)
  用**当日收盘市值**加权**当日**涨跌幅，数学上引入偏差：
      Σ w_t r ≈ R_cw + Var_cw(r)      ⇒ 约 +σ² 的正偏（实测 σ≈2.5% ⇒ +6bp/日）
  实测：该口径逐年比中证全指高 10~25pp，六年累计 +272.8% vs 正确口径 +53.9%。

本脚本用**同一批股票、同一天**，同时算出三种口径，落缓存供 05ag 复验事件研究：
  A  buggy  : 权重 = circ_mv_t      （= 现行 production 口径）
  B  fixed  : 权重 = circ_mv_{t-1}  （正确：期初权重）
只读原始数据，不触碰 production 产物。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

DATA = Path("/Users/hyc/quant-data")
OUT = Path("/Users/hyc/quant/industry-radar/research/prosperity")


def ts2bs(s: str) -> str:
    p = str(s).split(".")
    return f"{p[1].lower()}.{p[0]}" if len(p) == 2 else str(s)


def main():
    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])
    mem_by_date = {d: dict(zip(sub["stock_code"], sub["ind_code"]))
                   for d, sub in mem.groupby("date")}
    print(f"成分快照 {len(mem_by_date)} 天", flush=True)

    files = sorted((DATA / "market" / "daily").glob("*.parquet"))
    print(f"行情文件 {len(files)} 个", flush=True)

    ind_rows = []
    mkt_rows = []
    prev_cm = None
    t_ok = t_bad = 0

    for k, f in enumerate(files):
        d = pd.Timestamp(f.stem)
        dm = mem_by_date.get(d)
        if dm is None:
            prev_cm = None
            continue
        mk = pd.read_parquet(f, columns=["code", "pctChg"])
        ba = pd.read_parquet(DATA / "daily_basic" / f"{f.stem}.parquet",
                             columns=["ts_code", "circ_mv"])
        ba["code"] = ba["ts_code"].map(ts2bs)
        cur_cm = dict(zip(ba["code"], ba["circ_mv"]))

        mk = mk[mk["code"].isin(dm)].dropna(subset=["pctChg"])
        mk = mk[mk["code"].map(cur_cm).notna() & (mk["code"].map(cur_cm) > 0)]
        mk["ind_code"] = mk["code"].map(dm)

        if prev_cm is None:
            prev_cm = cur_cm
            continue

        mk["cm_prev"] = mk["code"].map(prev_cm)
        ok = mk["cm_prev"].notna() & (mk["cm_prev"] > 0)
        mk_fix = mk[ok]
        if len(mk_fix) < 1000:
            prev_cm = cur_cm
            continue

        mk["cm_cur"] = mk["code"].map(cur_cm)
        # 口径 A（现行 production）：当日市值加权 —— 组内只有 cm_cur 有效的样本
        a = mk[mk["cm_cur"] > 0].copy()
        a["w"] = a["cm_cur"]
        a["wr"] = a["pctChg"] * a["w"]
        ga = a.groupby("ind_code")[["wr", "w"]].sum()
        ga["ret"] = ga["wr"] / ga["w"]
        # 口径 B（修正）：前一日市值加权
        b = mk_fix.copy()
        b["w"] = b["cm_prev"]
        b["wr"] = b["pctChg"] * b["w"]
        gb = b.groupby("ind_code")[["wr", "w"]].sum()
        gb["ret"] = gb["wr"] / gb["w"]

        j = ga[["ret"]].rename(columns={"ret": "ret_buggy"}).join(
            gb[["ret"]].rename(columns={"ret": "ret_fixed"}), how="outer")
        j["date"] = d
        ind_rows.append(j.reset_index())

        mkt_rows.append({"date": d,
                         "mkt_buggy": a["wr"].sum() / a["w"].sum(),
                         "mkt_fixed": b["wr"].sum() / b["w"].sum(),
                         "n_stock_a": len(a), "n_stock_b": len(b)})
        t_ok += 1
        prev_cm = cur_cm
        if k % 300 == 0:
            print(f"  {k}/{len(files)}  {f.stem}  行业 {len(j)}", flush=True)

    ind = pd.concat(ind_rows, ignore_index=True)
    mkt = pd.DataFrame(mkt_rows)
    ind["ret_buggy"] = ind["ret_buggy"] / 100.0
    ind["ret_fixed"] = ind["ret_fixed"] / 100.0
    mkt["mkt_buggy"] = mkt["mkt_buggy"] / 100.0
    mkt["mkt_fixed"] = mkt["mkt_fixed"] / 100.0

    ind.to_parquet(OUT / "05af_reweighted_returns.parquet", index=False)
    mkt.to_parquet(OUT / "05af_reweighted_mkt.parquet", index=False)

    print(f"\n完成：可比交易日 {t_ok}")
    print(f"行业行 {len(ind)}   市值行 {len(mkt)}")
    for tag in ["buggy", "fixed"]:
        c = (1 + mkt[f"mkt_{tag}"]).prod() - 1
        print(f"  mkt_{tag} 累计 {c*100:+.1f}%")
    print(f"\n已写出：05af_reweighted_returns.parquet / 05af_reweighted_mkt.parquet")


if __name__ == "__main__":
    main()
