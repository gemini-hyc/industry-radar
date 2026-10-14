#!/usr/bin/env python3
"""
对象诊断（第 0 步）—— 论证"行业雷达"该用哪个研究对象作载体。

问题：申万二级（现用）作为研究对象是否合理？还是申万一级 / 产业链更优？

四个判据：
  1. 同质性（两层，别混淆）：
     A. 行业内同质 —— 同一行业内的**个股**残差相关高不高（判断一级/二级分组是否真同质）
     B. 组间同质 —— 产业链/一级组内的**行业**残差相关高不高（判断产业链是否同向）
  2. 稳定性  —— 边界随时间不漂
  3. 可交易性—— 映射漏损小
  4. 规模    —— 组内成分够多

本脚本只做测量与落盘。输出 docs/validation/object_diagnosis_*.csv + 摘要。

用法：/usr/bin/python3 research/validate_object.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

DATA_ROOT = Path("/Users/hyc/quant-data")
IND_DIR = DATA_ROOT / "industry"
OUT_DIR = Path("/Users/hyc/quant/industry-radar/docs/validation")

RESIDUAL = IND_DIR / "industry_residual_panel.parquet"   # 行业级残差（resid_adj）
RETURNS = IND_DIR / "industry_weighted_returns.parquet"
MEMBERS = IND_DIR / "industry_members_daily.parquet"     # 个股→行业 映射
L2_TO_L1 = IND_DIR / "sw_l2_to_l1.json"
TREE = IND_DIR / "sw2021_industry_tree.tsv"
CHAIN_PRIOR = Path("/Users/hyc/quant/industry-radar/docs/plan/chain-prior-list.csv")


def load_residual_panel() -> pd.DataFrame:
    return pq.read_table(RESIDUAL).to_pandas()


def load_industry_names() -> dict:
    df = pq.read_table(IND_DIR / "industry_list.parquet").to_pandas()
    return dict(zip(df["ts_code"], df["name"]))


def load_l2_l1_map() -> dict:
    return json.loads(L2_TO_L1.read_text(encoding="utf-8"))


def load_chain_members() -> dict:
    df = pd.read_csv(CHAIN_PRIOR, comment="#")
    out = {}
    for chain, g in df.groupby("chain_id"):
        out[chain] = list(g["ind_code"].unique())
    return out


def load_tree_nmembers() -> dict:
    df = pd.read_csv(TREE, sep="\t", comment="#")
    l2 = df[df["level"] == "L2"]
    return dict(zip(l2["name"], l2["n_members"]))


def _mean_r_from_corr(corr: np.ndarray, k: int) -> float:
    """取相关矩阵上三角（k×k），Fisher z 平均后反变换。"""
    iu = np.triu_indices(k, k=1)
    rs = corr[iu]
    rs = rs[~np.isnan(rs)]
    if len(rs) == 0:
        return np.nan
    z = np.arctanh(np.clip(rs, -0.9999, 0.9999))
    return float(np.tanh(np.nanmean(z)))


# ── 同质性 A：行业内个股同质（个股级残差，判断一级/二级分组）──

def _build_stock_residual_panel(start="2022-01-01", end="2026-10-13"):
    """
    构造个股级残差面板：pctChg 对市场等权收益回归，取残差。
    简化口径：残差 = pctChg − 市场等权 pctChg（剥市场 beta；风格剥除留待后续）。
    返回宽表：index=date, columns=stock_code(futu 格式), values=残差。
    """
    daily_files = sorted((DATA_ROOT / "market" / "daily").glob("*.parquet"))
    frames = []
    for f in daily_files:
        d = str(f.stem)
        if d < start or d > end:
            continue
        df = pq.read_table(f).to_pandas()[["date", "code", "pctChg"]]
        frames.append(df)
    if not frames:
        raise RuntimeError("market/daily 无可用文件")
    allr = pd.concat(frames, ignore_index=True)
    # 市场等权收益（按日）
    mkt = allr.groupby("date")["pctChg"].mean().rename("mkt")
    allr = allr.merge(mkt, left_on="date", right_index=True)
    allr["resid"] = allr["pctChg"] - allr["mkt"]
    piv = allr.pivot_table(index="date", columns="code", values="resid")
    return piv


def compute_homogeneity_stock(member_map: dict[str, list[str]],
                              label: str,
                              resid_piv: pd.DataFrame,
                              min_days: int = 120,
                              min_stocks: int = 5) -> pd.DataFrame:
    """
    同质性 A：组内**个股**残差的平均两两相关。

    member_map: 组名 -> 该组包含的 ind_code 列表。
    个股残差面板 resid_piv 的列是 futu 格式 stock_code（sh.600679）。
    """
    # 需要 ind_code -> 个股列表。从 industry_members_daily 取最近一日的映射。
    mem = pq.read_table(MEMBERS).to_pandas()
    latest = mem["date"].max()
    mem_latest = mem[mem["date"] == latest]
    ind_to_stocks = mem_latest.groupby("ind_code")["stock_code"].apply(list).to_dict()

    rows = []
    for gname, codes in member_map.items():
        stocks = []
        for c in codes:
            stocks += [s for s in ind_to_stocks.get(c, []) if s in resid_piv.columns]
        stocks = list(dict.fromkeys(stocks))  # 去重保序
        if len(stocks) < min_stocks:
            rows.append({"group": gname, "label": label, "n_units": len(codes),
                         "n_stocks": len(stocks), "pair_n": 0,
                         "homog_stock_r": np.nan})
            continue
        sub = resid_piv[stocks].dropna(how="all")
        # 要求足够多共同观测日
        if sub.shape[0] < min_days:
            rows.append({"group": gname, "label": label, "n_units": len(codes),
                         "n_stocks": len(stocks), "pair_n": 0,
                         "homog_stock_r": np.nan})
            continue
        # 为控制计算量，若个股很多则抽样
        if len(stocks) > 60:
            rng = np.random.default_rng(42)
            stocks = list(rng.choice(stocks, size=60, replace=False))
            sub = resid_piv[stocks].dropna(how="all")
        corr = sub.corr().values
        rows.append({"group": gname, "label": label, "n_units": len(codes),
                     "n_stocks": len(stocks),
                     "pair_n": len(stocks) * (len(stocks) - 1) // 2,
                     "homog_stock_r": _mean_r_from_corr(corr, len(stocks))})
    return pd.DataFrame(rows)


# ── 同质性 B：组内行业同质（行业级残差，判断产业链是否同向）──

def compute_homogeneity_industry(resid: pd.DataFrame,
                                 grouping: dict[str, list[str]],
                                 label: str) -> pd.DataFrame:
    piv = resid.pivot_table(index="date", columns="ind_code", values="resid_adj")
    rows = []
    for gname, codes in grouping.items():
        codes = [c for c in codes if c in piv.columns]
        if len(codes) < 2:
            continue
        sub = piv[codes].dropna(how="all")
        if sub.shape[0] < 30:
            continue
        corr = sub.corr().values
        rows.append({"group": gname, "label": label, "n_units": len(codes),
                     "homog_ind_r": _mean_r_from_corr(corr, len(codes))})
    return pd.DataFrame(rows)


def compute_size(grouping, label, nmembers, name_of):
    rows = []
    for gname, codes in grouping.items():
        ns = [nmembers.get(name_of.get(c, ""), np.nan) for c in codes]
        ns = [n for n in ns if pd.notna(n)]
        rows.append({"group": gname, "label": label, "n_units": len(codes),
                     "member_sum": int(np.sum(ns)) if ns else 0,
                     "member_min": int(np.min(ns)) if ns else 0,
                     "member_median": float(np.median(ns)) if ns else np.nan,
                     "member_mean": float(np.mean(ns)) if ns else np.nan})
    return pd.DataFrame(rows)


def main():
    print("载入数据…")
    resid = load_residual_panel()
    names = load_industry_names()
    l2l1 = load_l2_l1_map()
    chains = load_chain_members()
    nmembers = load_tree_nmembers()

    # 二级（每个组 = 单个行业）
    l2 = {f"二级·{name}": [code] for code, name in names.items()}
    # 一级（组 = 一级下所有二级行业）
    l1 = {}
    for code, name in names.items():
        parent = l2l1.get(name, "未映射")
        l1.setdefault(f"一级·{parent}", []).append(code)
    # 链
    ch = {f"链·{cid}": codes for cid, codes in chains.items()}

    print("构造个股残差面板（2022-01 起，可能较慢）…")
    stock_resid = _build_stock_residual_panel()

    # 同质性 A（个股级）：二级 / 一级
    print("计算同质性 A（行业内个股同质）…")
    homog_a = pd.concat([
        compute_homogeneity_stock(l2, "二级", stock_resid),
        compute_homogeneity_stock(l1, "一级", stock_resid),
    ], ignore_index=True)

    # 同质性 B（行业级）：一级 / 链
    print("计算同质性 B（组内行业同质）…")
    homog_b = pd.concat([
        compute_homogeneity_industry(resid, l1, "一级"),
        compute_homogeneity_industry(resid, ch, "链"),
    ], ignore_index=True)

    # 规模
    size = pd.concat([
        compute_size(l2, "二级", nmembers, names),
        compute_size(l1, "一级", nmembers, names),
        compute_size(ch, "链", nmembers, names),
    ], ignore_index=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now().strftime("%Y%m%d")
    homog_a.to_csv(OUT_DIR / f"object_diag_homogA_stock_{stamp}.csv", index=False)
    homog_b.to_csv(OUT_DIR / f"object_diag_homogB_industry_{stamp}.csv", index=False)
    size.to_csv(OUT_DIR / f"object_diag_size_{stamp}.csv", index=False)

    # 摘要
    print("\n" + "=" * 70)
    print("同质性 A（行业内个股同质，越高 = 行业越'真'）")
    print("=" * 70)
    for lbl in ("二级", "一级"):
        sub = homog_a[homog_a["label"] == lbl].dropna(subset=["homog_stock_r"])
        print(f"【{lbl}】组数 {len(sub)} ｜ 同质 r：均值 {sub['homog_stock_r'].mean():+.3f} "
              f"中位 {sub['homog_stock_r'].median():+.3f} "
              f"P25 {sub['homog_stock_r'].quantile(.25):+.3f} "
              f"P75 {sub['homog_stock_r'].quantile(.75):+.3f}")

    print("\n" + "=" * 70)
    print("同质性 B（组内行业同质，判断产业链/一级是否同向）")
    print("=" * 70)
    for lbl in ("一级", "链"):
        sub = homog_b[homog_b["label"] == lbl].dropna(subset=["homog_ind_r"])
        print(f"【{lbl}】组数 {len(sub)} ｜ 同质 r：均值 {sub['homog_ind_r'].mean():+.3f} "
              f"中位 {sub['homog_ind_r'].median():+.3f}")

    print("\n" + "=" * 70)
    print("规模（组内成分股数，越小 = 越易噪声大）")
    print("=" * 70)
    for lbl in ("二级", "一级", "链"):
        sub = size[size["label"] == lbl]
        print(f"【{lbl}】组数 {len(sub)} ｜ 组内成分中位(中位) {sub['member_median'].median():.0f} "
              f"｜ 最小成分 {sub['member_min'].min():.0f}")

    print("\n产物已落盘:", OUT_DIR)


if __name__ == "__main__":
    main()
