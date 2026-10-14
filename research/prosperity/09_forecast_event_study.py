#!/usr/bin/env python3
"""
09 · 业绩预告事件研究 —— 方向A第一步：盈利预期是否携带「价格之外、领先价格」的信号

动机：02~08 已证伪「价格结构 + 已实现财务 + 估值」三类横截面 alpha。方法论铁律指出
      真信号必须携带「价格之外、又领先于价格」的信息。业绩预告(forecast)是首个这样的数据：
      - 内容关于「未来盈利」（预告净利润变动区间），公告日早于正式财报；
      - 按公告日存储 = 天然事件流，PIT 无前视。

数据（~/quant-data/reports/forecast/）：
  8082 条 / 3901 股 / 354 个公告日（2024-10-15 → 2026-10-14）。
  字段：ts_code / ann_date(公告日) / end_date(报告期) / type(预增/首亏/扭亏…) /
        p_change_min/max(净利变动%) / net_profit_min/max / change_reason(文本) / update_flag。

口径决策：
  - 预告「领先性」相对正式财报：lead = end_date - ann_date 中位 -16 天（期后约16天披露，
    但正式财报滞后1~4个月）⇒ 预告是「财报前」的前瞻信息。
  - 个股超额基准 = 全市场市值加权（daily_basic.circ_mv）；行业超额 = 行业中位。
  - 事件后 horizon：1/5/10/20/60/120 日，用复利累计；中位数+胜率为主，长窗口做非重叠复核。

方法：
  A. 个股级：按 type 分组 → 事件后超额中位/胜率（相对全市场市值加权）
  B. 幅度维度：p_change 中值分桶（大幅预增 vs 大幅预减）→ 超额
  C. 行业聚合：预告股按申万二级聚合，算「行业景气预期变化」，看行业未来超额
  D. 探索：预增 vs 预减 的多空（买预增、卖预减）

产出：research/prosperity/09_预告事件研究报告.md + 事件曲线图
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "STHeiti"]
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parents[2]
DATA = Path("/Users/hyc/quant-data")
OUT = ROOT / "research" / "prosperity"
REPORT = OUT / "09_预告事件研究报告.md"

HORIZONS = [1, 5, 10, 20, 60, 120]


def ts_to_futu(s: pd.Series) -> pd.Series:
    """Tushare ts_code (000978.SZ) -> futu (sz.000978)"""
    s = s.astype(str).str.strip()
    return s.str.split(".").str[-1].str.lower() + "." + s.str.split(".").str[0]


def load_forecast():
    frames = []
    for f in sorted((DATA / "reports" / "forecast").glob("*/*.parquet")):
        df = pd.read_parquet(f)
        frames.append(df)
    fc = pd.concat(frames, ignore_index=True)
    fc["ann_date"] = pd.to_datetime(fc["ann_date"])
    fc["end_date"] = pd.to_datetime(fc["end_date"])
    fc["stock_code"] = ts_to_futu(fc["ts_code"])
    fc["p_mid"] = (fc["p_change_min"] + fc["p_change_max"]) / 2
    # 去掉 p_change 缺失的（少数"不确定"类可能无幅度）
    return fc


def load_market_daily():
    """个股日线 pctChg（小数）矩阵。"""
    frames = []
    for f in sorted((DATA / "market" / "daily").glob("*.parquet")):
        df = pd.read_parquet(f, columns=["date", "code", "pctChg"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.rename(columns={"code": "stock_code"})
        frames.append(df)
    md = pd.concat(frames, ignore_index=True)
    # pctChg 是百分数 → 转小数
    md["ret"] = md["pctChg"] / 100.0
    return md


def load_cap():
    """个股市值 circ_mv（用于市场市值加权基准）。"""
    frames = []
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        df = pd.read_parquet(f, columns=["ts_code", "trade_date", "circ_mv"])
        df["date"] = pd.to_datetime(df["trade_date"])
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", "circ_mv"]])
    cap = pd.concat(frames, ignore_index=True)
    return cap


def load_industry_member():
    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])
    return mem


def build():
    fc = load_forecast()
    md = load_market_daily()
    cap = load_cap()
    mem = load_industry_member()

    # 全市场市值加权日收益（基准）
    cap_daily = cap.groupby("date").agg(circ_mv=("circ_mv", "sum")).reset_index()
    # 个股收益 + 市值，算市场收益
    ret_cap = md.merge(cap, on=["date", "stock_code"], how="inner")
    mkt = ret_cap.groupby("date").apply(
        lambda d: (d["ret"] * d["circ_mv"]).sum() / d["circ_mv"].sum(),
        include_groups=False,
    ).rename("mkt_ret")
    mkt = mkt.to_frame()

    # 个股日收益面板（宽表）
    R = md.pivot_table(index="date", columns="stock_code", values="ret").sort_index()

    return fc, md, cap, mem, mkt, R


def forward_excess(R, mkt, stock_code, ann_date, horizon):
    """个股在公告日后 horizon 日的累计超额（相对市值加权市场）。"""
    dates = R.index
    if ann_date not in dates:
        return np.nan
    i = dates.get_loc(ann_date)
    j = i + horizon
    if j >= len(dates):
        return np.nan
    if stock_code not in R.columns:
        return np.nan
    rets = R.iloc[i + 1: j + 1][stock_code].dropna()
    mrets = mkt.iloc[i + 1: j + 1]["mkt_ret"].dropna()
    if len(rets) == 0:
        return np.nan
    # 复利
    cum = (1 + rets).prod() - 1
    cum_mkt = (1 + mrets).prod() - 1
    return cum - cum_mkt


def main():
    fc, md, cap, mem, mkt, R = build()
    print(f"forecast: {len(fc)} 条 | 个股收益面板 {R.shape} | 市场基准 {len(mkt)} 日")

    # 事件后超额：每条预告事件
    rows = []
    for _, e in fc.iterrows():
        ex = {f"ex_{h}": forward_excess(R, mkt, e["stock_code"], e["ann_date"], h)
              for h in HORIZONS}
        rec = {"ann_date": e["ann_date"], "stock_code": e["stock_code"],
               "type": e["type"], "p_mid": e["p_mid"], "end_date": e["end_date"]}
        rec.update(ex)
        rows.append(rec)
    ev = pd.DataFrame(rows)
    ev["p_mid"] = pd.to_numeric(ev["p_mid"], errors="coerce")
    print(f"事件面板: {len(ev)} 条；有超额收益的: {ev['ex_20'].notna().sum()}")

    # 个股级：按 type 分组
    print("\n=== A. 个股级：预告 type → 事件后超额中位/胜率 ===")
    type_order = ["预增", "略增", "续盈", "扭亏", "略减", "预减", "首亏", "续亏", "增亏", "减亏", "不确定"]
    for t in type_order:
        sub = ev[ev["type"] == t]
        if len(sub) < 10:
            continue
        parts = []
        for h in [5, 20, 60, 120]:
            v = sub[f"ex_{h}"].dropna()
            if len(v) < 10:
                parts.append(f"{h}日 n/a")
                continue
            parts.append(f"{h}日 {v.median():+.2%}(胜{(v > 0).mean():.0%})")
        print(f"  {t:<4} n={len(sub):<5} " + " | ".join(parts))

    # B. 幅度维度：大幅预增 vs 大幅预减
    print("\n=== B. 幅度维度（p_change 中值分桶）===")
    # 只保留有幅度、且 type 属于增/减方向明确的
    ev_pos = ev[ev["p_mid"].notna()]
    buckets = [("大幅预增(>50%)", (ev_pos["type"].isin(["预增", "略增", "扭亏"])) & (ev_pos["p_mid"] >= 50)),
               ("温和预增(0~50%)", (ev_pos["type"].isin(["预增", "略增", "扭亏"])) & (ev_pos["p_mid"] < 50)),
               ("大幅预减(<-50%)", (ev_pos["type"].isin(["预减", "略减", "首亏", "增亏"])) & (ev_pos["p_mid"] <= -50)),
               ("温和预减(-50~0%)", (ev_pos["type"].isin(["预减", "略减", "首亏", "增亏"])) & (ev_pos["p_mid"] > -50)),
               ]
    for name, msk in buckets:
        sub = ev_pos[msk]
        if len(sub) < 10:
            continue
        parts = []
        for h in [5, 20, 60, 120]:
            v = sub[f"ex_{h}"].dropna()
            parts.append(f"{h}日 {v.median():+.2%}(胜{(v > 0).mean():.0%})")
        print(f"  {name:<16} n={len(sub):<5} " + " | ".join(parts))

    # C. 行业聚合：预告股按公告日的申万二级行业聚合
    print("\n=== C. 行业聚合（预告股所属申万二级）===")
    # 用公告日当天的成分映射
    ev_ind = ev.merge(mem, left_on=["ann_date", "stock_code"], right_on=["date", "stock_code"], how="left")
    ind_events = ev_ind.dropna(subset=["ind_code"])
    print(f"  有行业映射的预告事件: {len(ind_events)}")
    # 按行业 × 报告期聚合"预增占比"和"平均幅度"
    ind_agg = ind_events.groupby("ind_code").agg(
        n_events=("stock_code", "size"),
        pos_ratio=("p_mid", lambda s: (s > 0).mean() if s.notna().any() else np.nan),
        p_mid_mean=("p_mid", "mean"),
    ).reset_index()
    ind_agg = ind_agg[ind_agg["n_events"] >= 5].sort_values("p_mid_mean", ascending=False)
    print("  预告平均净利变动 Top10 行业:")
    for _, r in ind_agg.head(10).iterrows():
        print(f"    {r['ind_code']:<14} 事件{r['n_events']:>3} 均幅{r['p_mid_mean']:+.0f}% 预增占比{(r['pos_ratio']*100):.0f}%")

    # 保存中间产物
    ev.to_parquet(OUT / "forecast_events_panel.parquet", index=False)
    ind_agg.to_parquet(OUT / "forecast_ind_agg.parquet", index=False)
    print(f"\n中间产物已存: forecast_events_panel.parquet / forecast_ind_agg.parquet")


if __name__ == "__main__":
    main()
