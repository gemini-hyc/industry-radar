#!/usr/bin/env python3
"""
08 · 分段回测 —— 景气 × 估值 合流信号是否随牛熊切换而翻转

动机：07 全样本证伪了「高景气+低估」合流信号。但全样本平均可能抹平「条件效应」——
      若信号在牛市/熊市方向相反，则全样本不显著不代表分段无信号。
      本脚本把样本切成主人的七段牛熊（数据仅覆盖 2020 起，故实际跑 3 段），
      在每段内独立重跑 2×2 双重排序 + 交互项 + 3×3，并做非重叠抽样复核。

数据覆盖约束（已核实）：
  - 行业收益 / daily_basic(PB) / 景气面板 均从 2020-01-02 起；景气 np_yoy 从 2020-04 起。
  - 主人的七段中，1~4 段（2012~2019）无数据；5 段只有后段(2020-01~2021-12)；6、7 段完整覆盖。
  - 实际跑 3 段：S5_上涨后段(2020-01-02~2021-12-13) / S6_下跌(2021-12-13~2024-02-05)
                / S7_上涨(2024-02-05~2026-05-14)。

方法（与 07 同口径，分段独立）：
  - 2×2：景气(np_yoy)中位切 × 估值(PB)中位切，事件后 h 日超额「相对全行业中位」的中位数
  - 交互项：[高景气@便宜−低景气@便宜] − [高景气@贵−低景气@贵]，聚类 t + 非重叠抽样复核
  - 3×3：便宜列内景气三档的相对中位超额
产出：08_分段回测报告.md + 分段四象限图
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
PANEL = OUT / "industry_prosperity_panel.parquet"
REPORT = OUT / "08_分段回测报告.md"

WINDOWS = [20, 60, 120]
MIN_MEMBER, MIN_FINA = 10, 5

# 主人的七段；仅跑有数据的三段
SEGMENTS = [
    ("S5_上涨后段", "2020-01-02", "2021-12-13"),
    ("S6_下跌",     "2021-12-13", "2024-02-05"),
    ("S7_上涨",     "2024-02-05", "2026-05-14"),
]


def ts_to_futu(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    return s.str.split(".").str[-1].str.lower() + "." + s.str.split(".").str[0]


def load_all():
    panel = pd.read_parquet(PANEL)
    panel["date"] = pd.to_datetime(panel["date"])
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index() / 100.0

    frames = []
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        df = pq.read_table(f).to_pandas()[["ts_code", "pb"]]
        df["date"] = pd.to_datetime(f.stem)
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", "pb"]])
    basic = pd.concat(frames, ignore_index=True)
    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])
    m = mem.merge(basic, on=["date", "stock_code"], how="inner").dropna(subset=["pb"])
    m = m[m["pb"] > 0]
    g = m.groupby(["date", "ind_code"])["pb"].median().reset_index().rename(columns={"pb": "pb_med"})
    PB = g.pivot_table(index="date", columns="ind_code", values="pb_med").reindex(index=R.index, columns=R.columns)

    p = panel.set_index(["date", "ind_code"])
    def grab(c):
        return p[c].unstack("ind_code").reindex(index=R.index, columns=R.columns)
    Y = grab("np_yoy")
    Yrev = grab("rev_yoy")
    endu = grab("end_i")
    nmem = grab("n_member")
    nfin = grab("n_fina")
    return R, PB, Y, Yrev, endu, nmem, nfin


def cluster_t(daily: pd.Series, endu: pd.DataFrame, valid_mask):
    """按快照期聚类 t 检验（daily 为逐日序列）。"""
    grp = endu.where(valid_mask).mode(axis=1).iloc[:, 0]
    d = pd.DataFrame({"v": daily, "end": grp}).dropna()
    if len(d.groupby("end")) < 3:
        return None
    pm = d.groupby("end")["v"].mean()
    t, p = stats.ttest_1samp(pm.values, 0.0)
    return {"n_periods": len(pm), "mean": pm.mean(), "t": float(t), "p": float(p), "per_mean": pm}


def quadrant_masks(Y, PB, MASK):
    Ym = Y.where(MASK); Pm = PB.where(MASK)
    Yr = Ym.rank(axis=1, pct=True); Pr = Pm.rank(axis=1, pct=True)
    valid = Ym.notna() & Pm.notna()
    return {
        "高景气+低估": ((Yr >= 0.5) & (Pr <= 0.5) & valid),
        "高景气+高估": ((Yr >= 0.5) & (Pr > 0.5) & valid),
        "低景气+低估": ((Yr < 0.5) & (Pr <= 0.5) & valid),
        "低景气+高估": ((Yr < 0.5) & (Pr > 0.5) & valid),
    }


def run_segment(R, PB, Y, Yrev, endu, nmem, nfin, seg_name, t0, t1):
    """在 [t0, t1] 内跑 2×2 + 交互 + 3×3。"""
    sel = (R.index >= t0) & (R.index <= t1)
    Rs = R.loc[sel]
    EX = Rs.sub(Rs.mean(axis=1), axis=0)
    Ys = Y.loc[sel]; Ps = PB.loc[sel]
    endus = endu.loc[sel]; nmems = nmem.loc[sel]; nfins = nfin.loc[sel]
    MASK = (nmems >= MIN_MEMBER) & (nfins >= MIN_FINA)

    quads = quadrant_masks(Ys, Ps, MASK)
    out = {"name": seg_name, "n_days": len(Rs), "quads": {}, "inter": {}, "triple": None}

    # 2×2 + 交互
    rel_daily = {}
    for h in WINDOWS:
        fwd = (1 + EX).rolling(h).apply(np.prod, raw=True).shift(-h) - 1
        umed = fwd.median(axis=1)
        for name, msk in quads.items():
            gmed = fwd.where(msk).median(axis=1)
            rel = gmed - umed
            rel_daily[(h, name)] = rel
            ct = cluster_t(rel, endus, msk)
            row = out["quads"].setdefault(name, {"n_obs": int(msk.values.sum())})
            row[f"h{h}"] = {
                "rel_med": float((fwd.sub(umed, axis=0)).where(msk).stack().dropna().median()),
                "t": ct["t"] if ct else np.nan,
                "p": ct["p"] if ct else np.nan,
            }
        # 交互项
        sp = (rel_daily[(h, "高景气+低估")] - rel_daily[(h, "低景气+低估")]) \
             - (rel_daily[(h, "高景气+高估")] - rel_daily[(h, "低景气+高估")])
        ct = cluster_t(sp, endus, pd.DataFrame(True, index=sp.index, columns=Ys.columns))
        # 非重叠抽样复核
        sp_thin = sp.dropna().iloc[::h]
        grp = endus.where(MASK).mode(axis=1).iloc[:, 0]
        d2 = pd.DataFrame({"v": sp_thin, "end": grp.reindex(sp_thin.index)}).dropna()
        pm2 = d2.groupby("end")["v"].mean()
        t2, p2 = stats.ttest_1samp(pm2.values, 0.0)
        out["inter"][h] = {
            "mean": sp.mean(), "t": ct["t"] if ct else np.nan, "p": ct["p"] if ct else np.nan,
            "nper": ct["n_periods"] if ct else 0,
            "t_thin": float(t2), "p_thin": float(p2),
        }

    # 3×3（60日，便宜列内景气三档，相对全行业中位）
    h = 60
    fwd = (1 + EX).rolling(h).apply(np.prod, raw=True).shift(-h) - 1
    umed = fwd.median(axis=1)
    Ym = Ys.where(MASK); Pm = Ps.where(MASK)
    Yr = Ym.rank(axis=1, pct=True); Pr = Pm.rank(axis=1, pct=True)
    valid = Ym.notna() & Pm.notna()
    def bucket(rk):
        b = pd.DataFrame(np.nan, index=rk.index, columns=rk.columns)
        b[rk <= 1/3] = 0; b[(rk > 1/3) & (rk <= 2/3)] = 1; b[rk > 2/3] = 2
        return b
    Yb = bucket(Yr); Pb = bucket(Pr)
    tab = np.full((3, 3), np.nan)
    for iy in range(3):
        for ip in range(3):
            mm = (Yb == iy) & (Pb == ip) & valid
            v = (fwd.sub(umed, axis=0)).where(mm).stack().dropna()
            tab[iy, ip] = v.median() if len(v) else np.nan
    out["triple"] = tab
    return out


def main():
    R, PB, Y, Yrev, endu, nmem, nfin = load_all()
    results = []
    for name, t0, t1 in SEGMENTS:
        r = run_segment(R, PB, Y, Yrev, endu, nmem, nfin, name,
                        pd.Timestamp(t0), pd.Timestamp(t1))
        results.append(r)
        _print_segment(r)
    write_report(results)


def _print_segment(r):
    print(f"\n{'='*70}\n== {r['name']}  ({r['n_days']}交易日) ==\n{'='*70}")
    print("  2×2（相对全行业中位% / 聚类t）:")
    for name in ["高景气+低估", "高景气+高估", "低景气+低估", "低景气+高估"]:
        q = r["quads"][name]
        parts = [f"{h}日 {q[f'h{h}']['rel_med']:+.2%}(t{q[f'h{h}']['t']:+.2f})" for h in WINDOWS]
        print(f"    {name:<10} n={q['n_obs']:<6} " + " | ".join(parts))
    print("  交互项:")
    for h in WINDOWS:
        it = r["inter"][h]
        print(f"    {h:>3}日 均值{it['mean']:+.2%} t={it['t']:+.2f} p={it['p']:.3f}"
              f" | 非重叠 t={it['t_thin']:+.2f} p={it['p_thin']:.3f}")
    print("  3×3（60日，行景气低→高，列便宜→贵）:")
    tab = r["triple"]
    print("           便宜       中       贵")
    for iy, tag in zip(range(3), ["景气低", "景气中", "景气高"]):
        print(f"    {tag}  " + "   ".join(f"{tab[iy, ip]:+.2%}" for ip in range(3)))


def write_report(results):
    L = [
        "# 08 · 分段回测报告 —— 景气×估值是否随牛熊切换而翻转",
        "",
        f"> 生成：{pd.Timestamp.now():%Y-%m-%d %H:%M} · 脚本 `research/prosperity/08_segmented_regime.py`",
        "> 方法：与 07 同口径（2×2 + 交互 + 3×3），但在主人的牛熊分段内独立重跑；",
        "> 交互项附带**非重叠抽样复核**（吸收 07 复核发现的「长窗口重叠虚增」教训）。",
        "",
        "## 数据覆盖约束（重要）",
        "",
        "行业收益 / PB / 景气面板均从 **2020-01-02** 起（景气 np_yoy 从 2020-04 起）。",
        "主人的七段中，**1~4 段（2012~2019）无数据，无法回测**；实际可跑 3 段：",
        "",
        "| 段 | 标签 | 数据窗口 | 交易日 |",
        "|---|---|---|---|",
    ]
    for r in results:
        L.append(f"| {r['name']} | 上涨/下跌 | {r['name'].split('_')[1] if len(r['name'].split('_'))>1 else ''} | {r['n_days']} |")
    L += [
        "",
        "## 各段结果",
        "",
    ]
    for r in results:
        L += [f"### {r['name']}（{r['n_days']} 交易日）", "",
              "**2×2（相对全行业中位数，中位数口径）**：", "",
              "| 象限 | 20日 | 60日 | 120日 |", "|---|---|---|---|"]
        for name in ["高景气+低估", "高景气+高估", "低景气+低估", "低景气+高估"]:
            q = r["quads"][name]
            L.append(f"| {name} | " + " | ".join(
                f"{q[f'h{h}']['rel_med']:+.2%} (t{q[f'h{h}']['t']:+.2f})" for h in WINDOWS) + " |")
        L += ["", "**交互项**（高景气效应@便宜 − @贵；负=便宜行业里景气越高越差）：", "",
              "| 窗口 | 均值 | t | p | 非重叠t | 非重叠p |", "|---|---|---|---|---|---|"]
        for h in WINDOWS:
            it = r["inter"][h]
            L.append(f"| {h}日 | {it['mean']:+.2%} | {it['t']:+.2f} | {it['p']:.3f} | "
                     f"{it['t_thin']:+.2f} | {it['p_thin']:.3f} |")
        L += ["", "**3×3（60日，相对全行业中位）**：", "",
              "| | 便宜 | 中 | 贵 |", "|---|---|---|---|"]
        tab = r["triple"]
        for iy, tag in zip(range(3), ["景气低", "景气中", "景气高"]):
            L.append(f"| {tag} | " + " | ".join(f"{tab[iy, ip]:+.2%}" for ip in range(3)) + " |")
        L += [""]

    L += [
        "## 跨段对照与结论",
        "",
        "> 关键看两点：①「高景气+低估」是否在任何段都不占优；②交互项符号是否随牛熊翻转。",
        "",
        "（待脚本产出后人工填写结论）",
        "",
    ]
    REPORT.write_text("\n".join(L), encoding="utf-8")
    print(f"\n报告已写 {REPORT}")


if __name__ == "__main__":
    main()
