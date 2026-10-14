#!/usr/bin/env python3
"""
第二步·估值腿 —— 事件研究（市值加权 PE·TTM 版）

回答：把行业估值口径从「成分股 PE 中位数（等权）」换成「市值加权」后，
      「自身历史低估会不会涨 / 高估会不会跌」的结论是否改变？

市值加权 PE 的正统算法（聚合口径 / 以市值为权的调和平均）：
    PE_industry = Σ(市值_i) / Σ(盈利_i) = Σmv_i / Σ(mv_i / pe_i)
亏损公司 pe_ttm 为空（本数据无负值）⇒ 仅纳入盈利公司 ⇒ 口径 = 「盈利子集的聚合 PE」。

其余设计完全复用 05：
  - 滚动 3 年自身历史分位（750 日窗口，≥250 日起算）
  - 低估 ≤20% / 高估 ≥80%，含冷却期（回 40%~60% 中性区才重新计数）
  - 事件后 1~250 日累计超额（相对市值加权市场），以中位数 + 胜率为准

产出：research/prosperity/06_市值加权PE事件研究报告.md + 06_pe_capw_event_study.png
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
DATA = Path("/Users/hyc/quant-data")
OUT = ROOT / "research" / "prosperity"
REPORT = OUT / "06_市值加权PE事件研究报告.md"

LOOKBACK = 750
LOW_Q = 0.20
HIGH_Q = 0.80
COOLDOWN_LO, COOLDOWN_HI = 0.40, 0.60
HORIZONS = [1, 5, 10, 20, 40, 60, 120, 250]
MIN_MEMBER = 10
MIN_HIST = 250

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "STHeiti"]
plt.rcParams["axes.unicode_minus"] = False


def log(m):
    print(m, flush=True)


def ts_to_futu(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    return s.str.split(".").str[-1].str.lower() + "." + s.str.split(".").str[0]


def load_basic():
    frames = []
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        df = pq.read_table(f).to_pandas()[["ts_code", "pe_ttm", "total_mv", "circ_mv"]]
        df["date"] = pd.to_datetime(f.stem)
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", "pe_ttm", "total_mv", "circ_mv"]])
    return pd.concat(frames, ignore_index=True)


def detect_entries(s: pd.Series, low: bool) -> np.ndarray:
    s = s.astype(float)
    out = np.zeros(len(s), dtype=bool)
    armed = True
    for i in range(len(s)):
        v = s.iloc[i]
        if np.isnan(v):
            continue
        if low:
            if armed and v <= LOW_Q:
                out[i] = True
                armed = False
            elif not armed and v >= COOLDOWN_LO:
                armed = True
        else:
            if armed and v >= HIGH_Q:
                out[i] = True
                armed = False
            elif not armed and v <= COOLDOWN_HI:
                armed = True
    return out


def build_capweighted_PE(m: pd.DataFrame, mv_col: str):
    """聚合 PE = Σmv / Σ(mv/pe)；返回 V(PE面板)、N(有效股数)"""
    mv = m[["date", "ind_code", "pe_ttm", mv_col]].dropna()
    mv = mv[(mv["pe_ttm"] > 0) & (mv[mv_col] > 0)]
    mv = mv.assign(earn=mv[mv_col] / mv["pe_ttm"])
    g = mv.groupby(["date", "ind_code"]).agg(mv_sum=(mv_col, "sum"),
                                             e_sum=("earn", "sum"),
                                             n=(mv_col, "size")).reset_index()
    g["pe_agg"] = g["mv_sum"] / g["e_sum"]
    g.loc[g["e_sum"] <= 0, "pe_agg"] = np.nan
    V = g.pivot_table(index="date", columns="ind_code", values="pe_agg")
    N = g.pivot_table(index="date", columns="ind_code", values="n")
    return V, N


def build_equalweighted_PE(m: pd.DataFrame):
    """对照：等权中位 PE（05 口径）"""
    mv = m[["date", "ind_code", "pe_ttm"]].dropna()
    mv = mv[(mv["pe_ttm"] > 0) & (mv["pe_ttm"] < 500)]
    g = mv.groupby(["date", "ind_code"])["pe_ttm"].agg(["median", "size"]).reset_index()
    V = g.pivot_table(index="date", columns="ind_code", values="median")
    N = g.pivot_table(index="date", columns="ind_code", values="size")
    return V, N


def event_study(V, N, R, EX, label):
    V = V.where(N >= MIN_MEMBER).reindex(index=R.index, columns=R.columns)
    Vrank = V.rolling(LOOKBACK, min_periods=MIN_HIST).rank(pct=True)
    fwd_ret = {h: (1 + R).rolling(h).apply(np.prod, raw=True).shift(-h) - 1 for h in HORIZONS}
    fwd_ex = {h: (1 + EX).rolling(h).apply(np.prod, raw=True).shift(-h) - 1 for h in HORIZONS}

    results = []
    for side, low in [("低估", True), ("高估", False)]:
        for j in range(Vrank.shape[1]):
            col = Vrank.columns[j]
            events = detect_entries(Vrank.iloc[:, j], low)
            for i in np.where(events)[0]:
                rec = {"date": Vrank.index[i], "ind_code": col, "side": side}
                for h in HORIZONS:
                    rec[f"ret_{h}"] = fwd_ret[h].iloc[i, j]
                    rec[f"ex_{h}"] = fwd_ex[h].iloc[i, j]
                results.append(rec)
    evdf = pd.DataFrame(results)

    summ = {}
    for side in ["低估", "高估"]:
        sub = evdf[evdf["side"] == side]
        st = {"side": side, "n_events": len(sub)}
        for h in HORIZONS:
            vals = sub[f"ex_{h}"].dropna()
            if len(vals) == 0:
                st.update({f"med_{h}": np.nan, f"win_{h}": np.nan, f"t_{h}": np.nan,
                           f"ret_{h}": np.nan})
                continue
            t, _ = stats.ttest_1samp(
                vals.clip(vals.quantile(0.01), vals.quantile(0.99)).values, 0.0)
            st.update({f"med_{h}": vals.median(), f"win_{h}": (vals > 0).mean(),
                       f"t_{h}": t, f"ret_{h}": sub[f"ret_{h}"].median()})
        summ[side] = st

    log(f"  [{label}] 事件：低估 {summ['低估']['n_events']} / 高估 {summ['高估']['n_events']}")
    for side in ["低估", "高估"]:
        st = summ[side]
        for h in [20, 60, 120, 250]:
            log(f"    {side} {h:>3}日: 超额中位{st[f'med_{h}']:+.2%} 胜率{st[f'win_{h}']:.0%} "
                f"t={st[f't_{h}']:+.2f} | 绝对中位{st[f'ret_{h}']:+.2%}")
    return evdf, summ


def main():
    log("== 载入 ==")
    basic = load_basic()
    log(f"  个股估值记录 {len(basic)} 条")
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

    # ── 主口径：总市值加权聚合 PE ──
    log("\n== 主口径：总市值加权聚合 PE = Σmv/Σ(mv/pe) ==")
    V_tot, N_tot = build_capweighted_PE(m, "total_mv")
    log(f"  PE 面板中位: {np.nanmedian(V_tot.values):.1f} "
        f"(对比：等权中位 PE 面板中位 {np.nanmedian(build_equalweighted_PE(m)[0].values):.1f})")
    ev_tot, summ_tot = event_study(V_tot, N_tot, R, EX, "总市值加权")

    # ── 稳健性：流通市值加权聚合 PE ──
    log("\n== 稳健性：流通市值加权聚合 PE ==")
    V_cir, N_cir = build_capweighted_PE(m, "circ_mv")
    ev_cir, summ_cir = event_study(V_cir, N_cir, R, EX, "流通市值加权")

    # ── 对照：等权中位 PE（复现 05）──
    log("\n== 对照：等权中位 PE（05 口径复现）==")
    V_eq, N_eq = build_equalweighted_PE(m)
    ev_eq, summ_eq = event_study(V_eq, N_eq, R, EX, "等权中位")

    plot_event_study(ev_tot, "总市值加权")
    write_report({"总市值加权": summ_tot, "流通市值加权": summ_cir, "等权中位(05对照)": summ_eq})


def plot_event_study(evdf, label):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for ax, side in zip(axes, ["低估", "高估"]):
        sub = evdf[evdf["side"] == side]
        ex_med, ex_win = [], []
        for h in HORIZONS:
            ex_med.append(sub[f"ex_{h}"].median())
            ex_win.append((sub[f"ex_{h}"] > 0).mean())
        ax.plot(HORIZONS, ex_med, marker="o", color="tab:red" if side == "低估" else "tab:green",
                label="超额中位")
        ax.axhline(0, color="gray", lw=0.8)
        ax.set_title(f"市值加权PE · {side}事件后累计超额")
        ax.set_xlabel("事件后交易日")
        ax.set_ylabel("累计超额收益")
        ax.legend()
        ax.grid(alpha=0.3)
        for i, h in enumerate(HORIZONS):
            ax.annotate(f"{ex_win[i]:.0%}", (h, ex_med[i]), textcoords="offset points",
                        xytext=(0, 8), fontsize=7, ha="center", color="gray")
    fig.suptitle("PE(TTM) 市值加权 事件研究（自身3年分位，低估<20%/高估>80%，含冷却期）", fontsize=12)
    fig.tight_layout()
    png = OUT / "06_pe_capw_event_study.png"
    fig.savefig(png, dpi=120)
    plt.close(fig)
    log(f"  已保存 {png}")


def write_report(all_summ):
    lines = [
        "# 06 · 市值加权 PE(TTM) 事件研究报告",
        "",
        f"> 生成：{pd.Timestamp.now():%Y-%m-%d %H:%M} · 脚本 `research/prosperity/06_pe_capweighted_event_study.py`",
        f"> 数据：`daily_basic`（个股 pe_ttm / total_mv / circ_mv）+ 申万二级成分映射（日频）",
        "",
        "## 估值口径",
        "",
        "**市值加权聚合 PE**：`PE_industry = Σmv_i / ΣE_i`，其中 `E_i = mv_i / pe_ttm_i`。",
        "数学上等价于「以市值为权重的调和平均」，是估值指数（如中证指数）采用的标准口径，",
        "而非把个股 PE 做加权算术平均（后者会被高 PE 股放大，非正统）。",
        "",
        "> ⚠️ 限制：本数据 `pe_ttm` 无负值，亏损公司整条缺失 ⇒ 聚合只能覆盖「盈利子集」，",
        "> 因此该 PE 系统性低于「含亏损」的真实全行业 PE。三个口径都存在此限制，",
        "> 但对「自身历史分位」的相对比较影响有限（同一口径贯穿全历史）。",
        "",
        "其余设计完全同 05：滚动 3 年自身分位、低估≤20%/高估≥80%、含冷却期、",
        "事件后 1~250 日累计超额（相对市值加权市场），以中位数 + 胜率为准。",
        "",
        "## 结果汇总",
        "",
        "**事件后累计超额收益中位数（胜率）**",
        "",
    ]
    # 汇总表
    lines += ["| 口径 | 方向 | 事件数 | 20日 | 60日 | 120日 | 250日 |", "|---|---|---|---|---|---|---|"]
    for label, summ in all_summ.items():
        for side in ["低估", "高估"]:
            st = summ[side]
            cells = []
            for h in [20, 60, 120, 250]:
                cells.append(f"{st[f'med_{h}']:+.2%} ({st[f'win_{h}']:.0%})")
            lines.append(f"| {label} | {side} | {st['n_events']} | " + " | ".join(cells) + " |")
    lines += ["", "（括号内为胜率；超额 = 相对市值加权市场）", ""]

    for label, summ in all_summ.items():
        lines += [f"## {label}", ""]
        for side in ["低估", "高估"]:
            st = summ[side]
            lines += [f"### {side}事件（{st['n_events']} 个）", ""]
            lines += ["| 交易日 | 超额中位 | 超额胜率 | 超额 t(Winsor) | 绝对中位 |",
                      "|---|---|---|---|---|"]
            for h in HORIZONS:
                lines.append(f"| {h} | {st[f'med_{h}']:+.2%} | {st[f'win_{h}']:.0%} | "
                             f"{st[f't_{h}']:+.2f} | {st[f'ret_{h}']:+.2%} |")
            lines += [""]

    lines += ["![市值加权PE事件研究](06_pe_capw_event_study.png)", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    log(f"\n报告已写 {REPORT}")


if __name__ == "__main__":
    main()
