#!/usr/bin/env python3
"""
05aa · 「估值分位涨回 ≥40% 却仍然亏钱」的机制拆解（只读）

背景（主人 2026-10-15 提问）：
  B 口径 = 进(自身3年分位首次 ≤20%) 买入 → 出(首次 ≥40%) 卖出。
  既然分位从 ≤20% 抬到 ≥40%，为什么 17.4% 的 B 交易仍然亏钱？难道估值涨了、股价没涨？

三个候选机制（与预测力无关，纯粹是 PB 的构造 + 排名窗口的算术）：
  M1 分子通道：PB 升 ← 价格升          ⇒ 股价必涨，无法解释亏损
  M2 分母塌陷：PB 升 ← 每股净资产跌（亏损侵蚀净资产）⇒ 股价可以继续跌
  M3 窗口滚动：分位 = 750 日**滚动窗口内**的排名。三年前的高估值样本滚出窗口后，
                当前 PB 即使原地不动甚至下跌，排名也会机械抬升 ⇒ 与股价无关

本脚本复刻 05y 的判定（同 LOOKBACK/LOW_Q/COOLDOWN_LO/MIN_MEMBER/MIN_HIST），
对每个有「出」的事件测：
  分位变化 entry_pct→exit_pct / PB 原始水平变化 / 价格收益 / 隐含净资产变化
只读，不改任何既有产物。
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
METRIC = "pb"
H_DIV = 120  # 杠铃两端的分界（交易日）


def ts_to_futu(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    code = s.str.split(".").str[0]
    mkt = s.str.split(".").str[-1].str.lower()
    return mkt + "." + code


def load_basic() -> pd.DataFrame:
    frames = []
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        d = f.stem
        try:
            df = pq.read_table(f).to_pandas()[["ts_code", METRIC]]
        except Exception:
            continue
        df["date"] = pd.to_datetime(d)
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", METRIC]])
    return pd.concat(frames, ignore_index=True)


def detect_entries(s: pd.Series) -> np.ndarray:
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
    k = i + 1
    while k < len(vals):
        if not np.isnan(vals[k]) and vals[k] >= COOLDOWN_LO:
            return k
        k += 1
    return None


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

    m = mem.merge(basic, on=["date", "stock_code"], how="inner")
    mv = m[["date", "ind_code", METRIC]].dropna(subset=[METRIC])
    mv = mv[mv[METRIC] > 0]
    iv = mv.groupby(["date", "ind_code"])[METRIC].median().reset_index().rename(columns={METRIC: "val"})
    nn = mv.groupby(["date", "ind_code"]).size().rename("n").reset_index()
    iv = iv.merge(nn, on=["date", "ind_code"])
    V = iv.pivot_table(index="date", columns="ind_code", values="val")
    N = iv.pivot_table(index="date", columns="ind_code", values="n")
    V = V.where(N >= MIN_MEMBER).reindex(index=R.index, columns=R.columns)
    Vrank = V.rolling(LOOKBACK, min_periods=MIN_HIST).rank(pct=True)

    exv = EX.values
    rv = R.values
    cols = list(R.columns)
    colpos = {c: k for k, c in enumerate(cols)}
    dates = R.index

    rows = []
    for col in Vrank.columns:
        s = Vrank[col]
        vals = s.values
        for i in np.where(detect_entries(s))[0]:
            j = find_exit(vals, i)
            if j is None:
                rows.append({"ind": col, "entry_date": dates[i], "exit_date": pd.NaT,
                             "censored": True})
                continue
            k = colpos[col]
            win_ex = exv[i + 1: j + 1, k]
            win_r = rv[i + 1: j + 1, k]
            b_ex = np.prod(1 + win_ex) - 1
            abs_ret = np.prod(1 + win_r) - 1
            pb_i = V.iat[i, k]
            pb_j = V.iat[j, k]
            pb_chg = pb_j / pb_i - 1
            # 隐含净资产变化：PB = P/B ⇒ B_dyn = (1+价格收益)/(1+PB变化)-1（口径混合，仅作指示）
            bk_chg = (1 + abs_ret) / (1 + pb_chg) - 1 if (1 + pb_chg) != 0 else np.nan
            rows.append({
                "ind": col, "entry_date": dates[i], "exit_date": dates[j], "censored": False,
                "hold": j - i, "entry_pct": s.iloc[i], "exit_pct": s.iloc[j],
                "B_ex": b_ex, "abs_ret": abs_ret,
                "pb_entry": pb_i, "pb_exit": pb_j, "pb_chg": pb_chg, "bk_chg": bk_chg,
            })

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "05aa_exit_decomp_pb.csv", index=False)
    done = df[~df["censored"]].copy()
    cens = df[df["censored"]]

    print("=" * 92)
    print(f"PB 事件总数 = {len(df)}   有『出』(分位真回到≥40%) = {len(done)}   删失(从未回到40%) = {len(cens)}")
    print(f"删失样本被 05y 的 dropna 整条丢弃 ⇒ B 统计里的 {len(done)} 笔**全部**是分位真的回到 ≥40% 的")
    print("=" * 92)

    done["grp"] = np.where(done["hold"] <= H_DIV, f"≤{H_DIV}日", f">{H_DIV}日")
    print("\n【一】按持有天数分两组，看各量的中位")
    g = done.groupby("grp").agg(
        n=("B_ex", "size"),
        B超额=("B_ex", "median"),
        B超额均值=("B_ex", "mean"),
        绝对收益=("abs_ret", "median"),
        PB原始变化=("pb_chg", "median"),
        隐含净资产变化=("bk_chg", "median"),
        进分位=("entry_pct", "median"),
        出分位=("exit_pct", "median"),
        持有=("hold", "median"),
    )
    for c in ["B超额", "B超额均值", "绝对收益", "PB原始变化", "隐含净资产变化", "进分位", "出分位"]:
        g[c] = (g[c] * 100).map(lambda v: f"{v:+.2f}%")
    print(g.to_string())

    long_lose = done[(done["hold"] > H_DIV) & (done["B_ex"] < 0)]
    print(f"\n【二】>120日 且 亏钱 的样本：n = {len(long_lose)}")
    print(f"  · 价格确实下跌(abs_ret<0) 占比 = {(long_lose['abs_ret'] < 0).mean()*100:.1f}%")
    print(f"  · PB 原始水平**反而下跌**(pb_chg<0) 占比 = {(long_lose['pb_chg'] < 0).mean()*100:.1f}%"
          f"   ← 若显著>0，说明『分位上升』并不等于『PB 上升』")
    print(f"  · 分位上升但 PB 原始水平下跌 的占比 = "
          f"{((long_lose['exit_pct'] > long_lose['entry_pct']) & (long_lose['pb_chg'] < 0)).mean()*100:.1f}%")
    print(f"  · 隐含净资产下跌(bk_chg<0) 占比 = {(long_lose['bk_chg'] < 0).mean()*100:.1f}%")
    print(f"  · 出时分位中位 = {long_lose['exit_pct'].median()*100:.1f}%   "
          f"（若贴近 40%，说明是『刚够阈值就卖』）")
    print(f"  · 绝对收益中位 = {long_lose['abs_ret'].median()*100:+.2f}%   "
          f"PB原始变化中位 = {long_lose['pb_chg'].median()*100:+.2f}%   "
          f"隐含净资产中位 = {long_lose['bk_chg'].median()*100:+.2f}%")

    print("\n【三】全体有『出』事件的 价格收益 vs PB原始变化 相关性（若接近1，说明PB≈价格）")
    print(f"  corr(abs_ret, pb_chg) = {done[['abs_ret', 'pb_chg']].corr().iloc[0, 1]:.3f}")
    print(f"  PB变化解释价格变化的 R² = {done[['abs_ret', 'pb_chg']].corr().iloc[0, 1]**2:.3f}")

    print("\n【四】亏得最惨的 10 笔（看它们出在什么分位、PB 到底涨没涨）")
    worst = done.nsmallest(10, "B_ex")[["ind", "entry_date", "exit_date", "hold",
                                        "entry_pct", "exit_pct", "B_ex", "abs_ret", "pb_chg", "bk_chg"]]
    w = worst.copy()
    w["entry_date"] = w["entry_date"].dt.strftime("%Y-%m-%d")
    w["exit_date"] = w["exit_date"].dt.strftime("%Y-%m-%d")
    for c in ["entry_pct", "exit_pct", "B_ex", "abs_ret", "pb_chg", "bk_chg"]:
        w[c] = (w[c] * 100).map(lambda v: f"{v:+.1f}%")
    print(w.to_string(index=False))

    print(f"\n已写出：{OUT / '05aa_exit_decomp_pb.csv'}")


if __name__ == "__main__":
    main()
