#!/usr/bin/env python3
"""
构建「以 2024-09-24 收盘 = 100 为基期」的行业走势跟踪主表
industry/industry_nav_924.parquet（长表：每行 = 某交易日某行业）

列:
  date        交易日
  ind_code    行业码 (SH.LIST000x)
  ind_name    行业名
  nav_w       流通市值加权累计净值 (基期100)
  nav_e       等权累计净值 (基期100)
  nav_hs300   沪深300累计净值 (基期100) — 相对强度基准
  rs_w        相对强度 = nav_w / nav_hs300 * 100 (基期100；>100跑赢沪深300)
  mom20_w     加权净值 20 日动量 (nav_w/前20日nav_w - 1)
  mom60_w     加权净值 60 日动量

输入:
  industry/industry_weighted_returns.parquet  (circ_mv 加权日涨跌幅，已修口径)
  industry/industry_daily_full.parquet        (等权 avg_pct)
  index_daily/000300.SH.parquet              (沪深300 基准)
  industry/industry_members.parquet          (ind_code -> ind_name)
"""
import sys
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import DATA_DIR

INDUSTRY_DIR = DATA_DIR / "industry"
OUT_PATH = INDUSTRY_DIR / "industry_nav_924.parquet"
BASE = pd.Timestamp("2024-09-24")


def nav_from_pct(df: pd.DataFrame, pct_col: str) -> pd.DataFrame:
    """pivot 成 (date × ind_code)，以首行=100 的累计净值。"""
    piv = df.pivot(index="date", columns="ind_code", values=pct_col).sort_index()
    return (1 + piv / 100).cumprod() * 100


def main():
    # 1. 加权日涨跌幅
    w = pd.read_parquet(INDUSTRY_DIR / "industry_weighted_returns.parquet")
    w = w[w["date"] >= BASE][["date", "ind_code", "weighted_pct"]]

    # 2. 等权日涨跌幅
    e = pd.read_parquet(INDUSTRY_DIR / "industry_daily_full.parquet")
    e = e[e["date"] >= BASE].rename(columns={"con_code": "ind_code"})[["date", "ind_code", "avg_pct"]]

    # 3. 沪深300 基准
    hs = pd.read_parquet(DATA_DIR / "index_daily" / "000300.SH.parquet")
    hs["date"] = pd.to_datetime(hs["trade_date"].astype(str), format="%Y%m%d")
    hs = hs[hs["date"] >= BASE][["date", "pct_chg"]].rename(columns={"pct_chg": "bench_pct"})
    bench_nav = (1 + hs.set_index("date")["bench_pct"] / 100).cumprod() * 100

    # 4. 行业名
    m = pd.read_parquet(INDUSTRY_DIR / "industry_members.parquet")
    name = m.groupby("ind_code")["ind_name"].first()

    nav_w = nav_from_pct(w, "weighted_pct")
    nav_e = nav_from_pct(e, "avg_pct")
    # 统一对齐到加权表的交易日网格（两表日期集有细微差异，以加权表为准）
    nav_e = nav_e.reindex(index=nav_w.index, columns=nav_w.columns)
    # 基准对齐到行业交易日网格（交易日历一致，ffill 兜底）
    bench_nav = bench_nav.reindex(nav_w.index).ffill()

    # 5. 组装长表
    parts = []
    for ind in nav_w.columns:
        nw = nav_w[ind]
        ne = nav_e[ind] if ind in nav_e.columns else np.nan
        rs = nw / bench_nav * 100
        mom20 = nw / nw.shift(20) - 1
        mom60 = nw / nw.shift(60) - 1
        parts.append(pd.DataFrame({
            "date": nav_w.index,
            "ind_code": np.repeat(ind, len(nav_w.index)),
            "nav_w": nw.values,
            "nav_e": ne.values,
            "nav_hs300": bench_nav.values,
            "rs_w": rs.values,
            "mom20_w": mom20.values,
            "mom60_w": mom60.values,
        }))
    out = pd.concat(parts, ignore_index=True)
    out["ind_name"] = out["ind_code"].map(name)
    out = out[["date", "ind_code", "ind_name", "nav_w", "nav_e",
               "nav_hs300", "rs_w", "mom20_w", "mom60_w"]]
    out = out.sort_values(["date", "ind_code"]).reset_index(drop=True)
    out.to_parquet(OUT_PATH, index=False)

    print(f"✓ 保存: {OUT_PATH}")
    print(f"  行业数: {out['ind_code'].nunique()}  交易日: {out['date'].nunique()}")
    print(f"  基期: {BASE.date()} = 100")
    last = out[out["date"] == out["date"].max()]
    print(f"\n最新日 {out['date'].max().date()} 相对强度 Top8 (rs_w, >100跑赢沪深300):")
    for _, r in last.nlargest(8, "rs_w").iterrows():
        print(f"  {r['ind_name']:10s} rs={r['rs_w']:6.1f}  nav_w={r['nav_w']:7.1f}  mom20={r['mom20_w']*100:5.1f}%  mom60={r['mom60_w']*100:5.1f}%")


if __name__ == "__main__":
    main()
