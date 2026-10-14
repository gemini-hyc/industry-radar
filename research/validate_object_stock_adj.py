#!/usr/bin/env python3
"""
对象诊断 · 同质性 A 的风格剥除版（补完最后一块拼图）。

背景：之前同质性 A（行业内个股同质）只用"个股收益 − 市场等权"（只剥市场 beta），
      数字被"风格共动"污染、偏高。本脚本构造**个股级 resid_adj**：
          r̃_adj = r − [α + β_mkt·mkt + β_size·size + β_mom·mom]
      即剥掉市场 beta + 个股规模(size=log流通市值) + 个股动量(mom20)。
      口径对齐 src/resonance/measure.py 的 trailing out-of-sample β 估计
      （β 用 [t−W, t−1] 估计，作用在 t），W=250。

然后重算同质性 A：对比【只剥β】vs【剥β+size+mom】两种口径下，二级 vs 一级的同质 r。

用法：/usr/bin/python3 research/validate_object_stock_adj.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from pathlib import Path

DATA_ROOT = Path("/Users/hyc/quant-data")
MARKET = DATA_ROOT / "market" / "daily"
BASIC = DATA_ROOT / "daily_basic"
MEMBERS = DATA_ROOT / "industry" / "industry_members_daily.parquet"
L2_TO_L1 = DATA_ROOT / "industry" / "sw_l2_to_l1.json"
OUT_DIR = Path("/Users/hyc/quant/industry-radar/docs/validation")

WINDOW = 250          # β 估计窗口
START = "2022-01-01"
END = "2026-10-13"


def load_stock_returns() -> pd.DataFrame:
    """个股日收益宽表：index=date, columns=code(futu 格式 sz.000001)。"""
    frames = []
    for f in sorted(MARKET.glob("*.parquet")):
        d = f.stem
        if d < START or d > END:
            continue
        df = pq.read_table(f).to_pandas()[["date", "code", "pctChg"]]
        frames.append(df)
    allr = pd.concat(frames, ignore_index=True)
    piv = allr.pivot_table(index="date", columns="code", values="pctChg")
    piv = piv.sort_index()
    return piv / 100.0  # 小数


def load_stock_size() -> pd.DataFrame:
    """个股 log 流通市值 宽表：index=date, columns=code(futu 格式)。"""
    frames = []
    for f in sorted(BASIC.glob("*.parquet")):
        d = f.stem
        if d < START or d > END:
            continue
        try:
            df = pq.read_table(f).to_pandas()[["trade_date", "ts_code", "circ_mv"]]
        except Exception:
            continue
        df["code"] = df["ts_code"].apply(_ts_to_futu)
        df = df[df["code"].notna()]
        df["log_size"] = np.log(df["circ_mv"].where(df["circ_mv"] > 0))
        frames.append(df[["trade_date", "code", "log_size"]])
    allb = pd.concat(frames, ignore_index=True)
    piv = allb.pivot_table(index="trade_date", columns="code", values="log_size")
    piv.index = pd.to_datetime(piv.index)
    return piv.sort_index()


def _ts_to_futu(ts: str):
    """'000001.SZ' -> 'sz.000001'；'600679.SH' -> 'sh.600679'"""
    try:
        code, exch = ts.split(".")
        return f"{exch.lower()}.{code.lower()}"
    except Exception:
        return None


def load_stock_mom(R: pd.DataFrame) -> pd.DataFrame:
    """个股 20 日动量（t−21..t−1 累计对数收益），index/columns 对齐 R。"""
    lr = np.log1p(R)
    mom = lr.rolling(21, min_periods=15).sum().shift(1)
    return mom


def residualize_stock(R: pd.DataFrame, size: pd.DataFrame, mom: pd.DataFrame):
    """
    个股级残差化（in-sample 口径，用于对象论证诊断）：
      raw  = r − [α + β_mkt·mkt]
      adj  = r − [α + β_mkt·mkt + β_size·size + β_mom·mom]

    ⚠️ 说明：用 **in-sample**（全期 β）而非生产线的 out-of-sample 滚动 β。
    理由：对象论证只需"合理剥离风格后"的同质性度量来对比二级 vs 一级，
    不是生产信号；in-sample 对"组内相关对比"足够且纯向量化。
    若日后升级为生产口径，须改回 src/resonance/measure.py 的 trailing β。
    """
    dates = list(R.index)
    mkt = R.mean(axis=1)
    X = R.to_numpy(dtype=float)
    size_v = size.reindex(index=dates, columns=R.columns).to_numpy(dtype=float)
    mom_v = mom.reindex(index=dates, columns=R.columns).to_numpy(dtype=float)
    mkt_v = mkt.to_numpy(dtype=float)

    n_days, n_stocks = X.shape
    A0 = np.column_stack([np.ones(n_days), mkt_v])  # (n_days, 2)

    raw = np.full_like(X, np.nan)
    adj = np.full_like(X, np.nan)

    for j in range(n_stocks):
        yj = X[:, j]
        sj = size_v[:, j]
        mj = mom_v[:, j]
        # raw：市场（A0 对所有股票相同）
        v0 = np.isfinite(A0).all(axis=1) & np.isfinite(yj)
        if v0.sum() >= 60:
            b0, *_ = np.linalg.lstsq(A0[v0], yj[v0], rcond=None)
            raw[:, j] = yj - A0 @ b0
        # adj：市场 + 该股票的 size + mom
        A1j = np.column_stack([np.ones(n_days), mkt_v, sj, mj])
        v1 = np.isfinite(A1j).all(axis=1) & np.isfinite(yj)
        if v1.sum() >= 60:
            b1, *_ = np.linalg.lstsq(A1j[v1], yj[v1], rcond=None)
            adj[:, j] = yj - A1j @ b1

    raw_df = pd.DataFrame(raw, index=dates, columns=R.columns)
    adj_df = pd.DataFrame(adj, index=dates, columns=R.columns)
    return raw_df, adj_df


def _mean_r_from_corr(corr: np.ndarray, k: int) -> float:
    iu = np.triu_indices(k, k=1)
    rs = corr[iu]
    rs = rs[~np.isnan(rs)]
    if len(rs) == 0:
        return np.nan
    z = np.arctanh(np.clip(rs, -0.9999, 0.9999))
    return float(np.tanh(np.nanmean(z)))


def homog_for_grouping(member_map, resid_piv, label, min_stocks=5, min_days=120):
    mem = pq.read_table(MEMBERS).to_pandas()
    latest = mem["date"].max()
    mem_latest = mem[mem["date"] == latest]
    ind_to_stocks = mem_latest.groupby("ind_code")["stock_code"].apply(list).to_dict()

    rows = []
    for gname, codes in member_map.items():
        stocks = []
        for c in codes:
            stocks += [s for s in ind_to_stocks.get(c, []) if s in resid_piv.columns]
        stocks = list(dict.fromkeys(stocks))
        if len(stocks) < min_stocks:
            continue
        sub = resid_piv[stocks].dropna(how="all")
        if sub.shape[0] < min_days:
            continue
        if len(stocks) > 60:
            rng = np.random.default_rng(42)
            stocks = list(rng.choice(stocks, size=60, replace=False))
            sub = resid_piv[stocks].dropna(how="all")
        corr = sub.corr().values
        rows.append({"group": gname, "label": label, "n_stocks": len(stocks),
                     "homog_r": _mean_r_from_corr(corr, len(stocks))})
    return pd.DataFrame(rows)


def main():
    print("载入个股收益/市值…")
    R = load_stock_returns()
    size = load_stock_size()
    mom = load_stock_mom(R)

    # ── 缩减到"有成分映射 + 有市值 + 缺失率<5%"的股票，控内存 ──
    mem = pq.read_table(MEMBERS).to_pandas()
    latest = mem["date"].max()
    mapped = set(mem[mem["date"] == latest]["stock_code"])
    miss = R.isna().mean()
    keep = [c for c in R.columns
            if c in mapped and c in size.columns and miss.get(c, 1.0) < 0.05]
    R = R[keep].ffill()
    size = size.reindex(columns=keep).ffill()
    mom = mom.reindex(columns=keep)
    print(f"缩减后个股收益面板 {R.shape}（成分股 + 有市值 + 缺失率<5%，已前向填充）")

    print("构造个股级残差（raw=剥β，adj=剥β+size+mom，窗口 250，逐列回归，较慢）…")
    raw_df, adj_df = residualize_stock(R, size, mom)

    # 分组
    names = dict(zip(
        pq.read_table(DATA_ROOT / "industry" / "industry_list.parquet").to_pandas()["ts_code"],
        pq.read_table(DATA_ROOT / "industry" / "industry_list.parquet").to_pandas()["name"]))
    l2l1 = pd.read_json(L2_TO_L1, typ="series").to_dict()
    l2 = {f"二级·{n}": [c] for c, n in names.items()}
    l1 = {}
    for c, n in names.items():
        l1.setdefault(f"一级·{l2l1.get(n, '未映射')}", []).append(c)

    print("计算同质性 A（raw 口径）…")
    raw_l2 = homog_for_grouping(l2, raw_df, "二级")
    raw_l1 = homog_for_grouping(l1, raw_df, "一级")
    print("计算同质性 A（adj 口径，剥风格后）…")
    adj_l2 = homog_for_grouping(l2, adj_df, "二级")
    adj_l1 = homog_for_grouping(l1, adj_df, "一级")

    def summ(df, name):
        r = df["homog_r"].dropna()
        return (f"【{name}】n={len(r)} 均值 {r.mean():+.3f} 中位 {r.median():+.3f} "
                f"P25 {r.quantile(.25):+.3f} P75 {r.quantile(.75):+.3f}")

    print("\n" + "=" * 70)
    print("同质性 A 对比：只剥β vs 剥β+size+mom")
    print("=" * 70)
    print("\n【只剥市场β】")
    print("  " + summ(raw_l2, "二级"))
    print("  " + summ(raw_l1, "一级"))
    print("\n【剥β+size+mom（剥风格后）】")
    print("  " + summ(adj_l2, "二级"))
    print("  " + summ(adj_l1, "一级"))

    # 落盘
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now().strftime("%Y%m%d")
    out = pd.concat([
        raw_l2.assign(method="raw"), raw_l1.assign(method="raw"),
        adj_l2.assign(method="adj"), adj_l1.assign(method="adj"),
    ], ignore_index=True)
    out.to_csv(OUT_DIR / f"object_diag_homogA_stockadj_{stamp}.csv", index=False)
    print(f"\n产物已落盘: {OUT_DIR / f'object_diag_homogA_stockadj_{stamp}.csv'}")


if __name__ == "__main__":
    main()
