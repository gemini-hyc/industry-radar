#!/usr/bin/env python3
"""
第二步·估值腿 —— 事件研究 v3（最终版）

回答核心直觉：「自身历史极低估值的行业，未来会不会涨？等多久、涨多少？
                  自身历史极高估值，未来会不会跌？能撑多久、跌多少？」

v3 关键修正（相对 v1/v2）：
  1. 单位：weighted_pct 是「百分比」，除以 100 转小数，收益用复利 cumprod 而非简单累加
     （v1/v2 的 +1935% 是「百分比累加」伪象）。
  2. 基准：超额 = 行业收益 − 市值加权市场收益（剥离牛熊，最干净），
     而非「全行业等权」（等权被极端行业污染）。
  3. 主指标：超额收益的「中位数 + 胜率」，直接回答「相对市场会不会涨/跌」。
  4. 事件重叠：冷却期（须回到 40%~60% 中性区才重新计数）。
  5. 中文字体。

产出：research/prosperity/05_估值事件研究报告.md + 05_*.png
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
REPORT = OUT / "05_估值事件研究报告.md"

METRICS = ["pb", "pe_ttm"]
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
    code = s.str.split(".").str[0]
    mkt = s.str.split(".").str[-1].str.lower()
    return mkt + "." + code


def load_basic():
    frames = []
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        d = f.stem
        try:
            df = pq.read_table(f).to_pandas()[["ts_code", "pb", "pe_ttm"]]
        except Exception:
            continue
        df["date"] = pd.to_datetime(d)
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", "pb", "pe_ttm"]])
    return pd.concat(frames, ignore_index=True)


def detect_entries(s: pd.Series, low: bool) -> np.ndarray:
    s = s.astype(float)
    n = len(s)
    out = np.zeros(n, dtype=bool)
    armed = True
    for i in range(n):
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


def main():
    log("== 载入 ==")
    basic = load_basic()
    log(f"  个股估值记录 {len(basic)} 条")

    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])

    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])

    # 收益矩阵（小数），行业日收益 = weighted_pct / 100
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index() / 100.0

    # 市值加权市场基准（小数）
    mkt = (wr.groupby("date").apply(
        lambda d: (d["circ_mv"] * d["weighted_pct"]).sum() / d["circ_mv"].sum(),
        include_groups=False)) / 100.0
    mkt = mkt.reindex(R.index)
    EX = R.sub(mkt, axis=0)   # 行业超额 = 行业收益 − 市场

    m = mem.merge(basic, on=["date", "stock_code"], how="inner")

    all_results = {}

    for metric in METRICS:
        log(f"\n== 指标 {metric} ==")
        mv = m[["date", "ind_code", metric]].dropna(subset=[metric])
        if metric == "pb":
            mv = mv[mv[metric] > 0]
        else:
            mv = mv[(mv[metric] > 0) & (mv[metric] < 500)]

        iv = mv.groupby(["date", "ind_code"])[metric].median().reset_index().rename(columns={metric: "val"})
        n = mv.groupby(["date", "ind_code"]).size().rename("n").reset_index()
        iv = iv.merge(n, on=["date", "ind_code"])
        V = iv.pivot_table(index="date", columns="ind_code", values="val")
        N = iv.pivot_table(index="date", columns="ind_code", values="n")
        V = V.where(N >= MIN_MEMBER)
        V = V.reindex(index=R.index, columns=R.columns)
        Vrank = V.rolling(LOOKBACK, min_periods=MIN_HIST).rank(pct=True)

        # 未来累计收益（复利）与累计超额
        fwd_ret = {h: (1 + R).rolling(h).apply(np.prod, raw=True).shift(-h) - 1 for h in HORIZONS}
        fwd_ex = {h: (1 + EX).rolling(h).apply(np.prod, raw=True).shift(-h) - 1 for h in HORIZONS}

        results = []
        for side, low, tag in [("低估", True, "low"), ("高估", False, "high")]:
            for j, col in enumerate(V.columns):
                s = Vrank.iloc[:, j]
                events = detect_entries(s, low)
                for i in np.where(events)[0]:
                    d = Vrank.index[i]
                    rec = {"date": d, "ind_code": col, "side": side}
                    for h in HORIZONS:
                        rec[f"ret_{h}"] = fwd_ret[h].iloc[i, j]
                        rec[f"ex_{h}"] = fwd_ex[h].iloc[i, j]
                    results.append(rec)

        evdf = pd.DataFrame(results)
        if evdf.empty:
            log(f"  {metric}: 无事件")
            continue

        summ = {}
        for side, tag in [("低估", "low"), ("高估", "high")]:
            sub = evdf[evdf["side"] == side]
            st = {"side": side, "n_events": len(sub)}
            for h in HORIZONS:
                for kind in ["ret", "ex"]:
                    c = f"{kind}_{h}"
                    vals = sub[c].dropna()
                    if len(vals) == 0:
                        st.update({f"{kind}_{h}_med": np.nan, f"{kind}_{h}_win": np.nan,
                                   f"{kind}_{h}_t": np.nan})
                        continue
                    med = vals.median()
                    win = (vals > 0).mean()
                    t, p = stats.ttest_1samp(vals.clip(vals.quantile(0.01), vals.quantile(0.99)).values, 0.0)
                    st.update({f"{kind}_{h}_med": med, f"{kind}_{h}_win": win, f"{kind}_{h}_t": t})
            summ[tag] = st

        all_results[metric] = {"events": evdf, "summary": summ}

        for side, tag in [("低估", "low"), ("高估", "high")]:
            st = summ[tag]
            log(f"  [{metric}·{side}] 事件数 {st['n_events']}")
            for h in [20, 60, 120, 250]:
                log(f"    {h:>3}日: 超额中位{st[f'ex_{h}_med']:+.2%} 胜率{st[f'ex_{h}_win']:.0%} "
                    f"t={st[f'ex_{h}_t']:+.2f} | 绝对中位{st[f'ret_{h}_med']:+.2%}")

        plot_event_study(evdf, metric)

    write_report(all_results)


def plot_event_study(evdf, metric):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for ax, (side, tag) in zip(axes, [("低估", "low"), ("高估", "high")]):
        sub = evdf[evdf["side"] == side]
        ret_med, ex_med, ex_win = [], [], []
        for h in HORIZONS:
            ret_med.append(sub[f"ret_{h}"].median())
            ex_med.append(sub[f"ex_{h}"].median())
            ex_win.append((sub[f"ex_{h}"] > 0).mean())
        ax.plot(HORIZONS, ex_med, marker="o", color="tab:red" if side == "低估" else "tab:green",
                label="超额中位（相对市值加权市场）")
        ax.axhline(0, color="gray", lw=0.8)
        ax.set_title(f"{metric.upper()} · {side}事件后累计超额收益")
        ax.set_xlabel("事件后交易日")
        ax.set_ylabel("累计超额收益")
        ax.legend()
        ax.grid(alpha=0.3)
        for i, h in enumerate(HORIZONS):
            ax.annotate(f"{ex_win[i]:.0%}", (h, ex_med[i]),
                        textcoords="offset points", xytext=(0, 8), fontsize=7,
                        ha="center", color="gray")
    fig.suptitle(f"{metric.upper()} 事件研究（自身3年分位，低估<20%/高估>80%，含冷却期）", fontsize=12)
    fig.tight_layout()
    png = OUT / f"05_{metric}_event_study.png"
    fig.savefig(png, dpi=120)
    plt.close(fig)
    log(f"  已保存 {png}")


def write_report(all_results):
    lines = [
        "# 05 · 估值事件研究报告（低估会不会涨 / 高估会不会跌）",
        "",
        f"> 生成：{pd.Timestamp.now():%Y-%m-%d %H:%M} · 脚本 `research/prosperity/05_valuation_event_study.py`（v3）",
        f"> 数据：`daily_basic`（个股 PB/PE）+ 申万二级成分映射 + 行业收益",
        f"> 方法：事件研究 —— 行业估值滚动 3 年自身历史分位，低估≤{LOW_Q:.0%} / 高估≥{HIGH_Q:.0%}，",
        f"> 含冷却期（须回到 {COOLDOWN_LO:.0%}~{COOLDOWN_HI:.0%} 中性区才重新计数）。",
        f"> 收益用复利、超额基准 = 市值加权市场收益。以中位数 + 胜率为准。",
        "",
        "## 与 03/04 的本质区别",
        "",
        "- 03/04 测「横截面排序预测力」（相对贵/便宜），已证伪。",
        "- 本报告测「自身历史极值后的时间分布」——直接回答直觉问题。",
        "",
    ]

    for metric, res in all_results.items():
        summ = res["summary"]
        lines += [f"## {metric.upper()}", ""]
        lines += [f"事件总数：低估 {summ['low']['n_events']} / 高估 {summ['high']['n_events']}", ""]
        for side, tag in [("低估", "low"), ("高估", "high")]:
            st = summ[tag]
            lines += [f"### {side}事件", ""]
            lines += ["| 交易日 | 超额中位 | 超额胜率 | 超额 t(Winsor) | 绝对中位 |",
                      "|---|---|---|---|---|"]
            for h in HORIZONS:
                lines.append(f"| {h} | {st[f'ex_{h}_med']:+.2%} | {st[f'ex_{h}_win']:.0%} | "
                             f"{st[f'ex_{h}_t']:+.2f} | {st[f'ret_{h}_med']:+.2%} |")
            lines += ["", f"![{metric} {side}](05_{metric}_event_study.png)", ""]

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    log(f"\n报告已写 {REPORT}")


if __name__ == "__main__":
    main()
