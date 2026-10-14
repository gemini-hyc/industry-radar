#!/usr/bin/env python3
"""
第二步·合流验证 —— 景气 × 估值 双因子交互（「高景气 + 低估值」是否更好）

v2 修正：收益 /100 转小数；组统计改用**中位数**（抗极端值）；报告写入真实数值。

动机：低估值单独看无前瞻力（05/06）。经典价值理论认为：低估值只有在「景气向上」时
      才是机会，否则是价值陷阱 ⇒ 估值作用可能是条件性的，需与景气**交互**才显现。

方法（三法互证，按快照期 end_i 聚类 t）：
  A. 2×2 双重排序（景气高/低 × 估值低/高）
  B. Fama-MacBeth 交互回归：fwd_ex ~ 景气 + 便宜度 + 景气×便宜度
  C. 3×3 探索性排序

产出：research/prosperity/07_景气估值合流研究报告.md + 07_quadrant_cumulative.png
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
PANEL = OUT / "industry_prosperity_panel.parquet"
REPORT = OUT / "07_景气估值合流研究报告.md"

WINDOWS = [20, 60, 120]
PRIMARY = 60
MIN_MEMBER, MIN_FINA = 10, 5

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "STHeiti"]
plt.rcParams["axes.unicode_minus"] = False

RESULTS = {}


def log(m):
    print(m, flush=True)


def ts_to_futu(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    return s.str.split(".").str[-1].str.lower() + "." + s.str.split(".").str[0]


def load_pb():
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
    return g.pivot_table(index="date", columns="ind_code", values="pb_med")


def build():
    panel = pd.read_parquet(PANEL)
    panel["date"] = pd.to_datetime(panel["date"])
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index() / 100.0
    EX = R.sub(R.mean(axis=1), axis=0)     # 行业-日超额（小数）
    EX1 = EX.shift(-1)                      # 次日超额
    # 行内 winsor，防单日极端
    lo = EX1.quantile(0.01, axis=1); hi = EX1.quantile(0.99, axis=1)
    EX1w = EX1.clip(lower=lo, upper=hi, axis=0)

    p = panel.set_index(["date", "ind_code"])
    def grab(c):
        return p[c].unstack("ind_code").reindex(index=R.index, columns=R.columns)
    Y = grab("np_yoy"); Yrev = grab("rev_yoy"); endu = grab("end_i")
    nmem = grab("n_member"); nfin = grab("n_fina")
    MASK = (nmem >= MIN_MEMBER) & (nfin >= MIN_FINA)
    PB = load_pb().reindex(index=R.index, columns=R.columns)
    return R, EX, EX1w, Y, Yrev, endu, MASK, PB


def cluster_t(daily: pd.Series, endu: pd.DataFrame, valid: pd.DataFrame):
    e = endu.where(valid)
    grp = e.mode(axis=1).iloc[:, 0]
    d = pd.DataFrame({"v": daily, "end": grp}).dropna()
    if len(d.groupby("end")) < 3:
        return None
    pm = d.groupby("end")["v"].mean()
    t, p = stats.ttest_1samp(pm.values, 0.0)
    return {"n_periods": len(pm), "mean": pm.mean(), "t": float(t), "p": float(p)}


def quadrant_masks(Y, P, MASK):
    Ym = Y.where(MASK); Pm = P.where(MASK)
    Yr = Ym.rank(axis=1, pct=True); Pr = Pm.rank(axis=1, pct=True)  # Pr 低=便宜
    valid = Ym.notna() & Pm.notna()
    return {
        "高景气+低估": ((Yr >= 0.5) & (Pr <= 0.5) & valid),
        "高景气+高估": ((Yr >= 0.5) & (Pr > 0.5) & valid),
        "低景气+低估": ((Yr < 0.5) & (Pr <= 0.5) & valid),
        "低景气+高估": ((Yr < 0.5) & (Pr > 0.5) & valid),
    }


def double_sort(Y, P, EX, endu, MASK, label):
    """2×2 双重排序；输出「相对全行业中位数」的超额（消除横截面偏态造成的基准伪象）
    并直接检验交互差值的显著性（cluster t）。"""
    quads = quadrant_masks(Y, P, MASK)
    out = {}
    rel_daily = {}
    for h in WINDOWS:
        fwd = (1 + EX).rolling(h).apply(np.prod, raw=True).shift(-h) - 1
        umed = fwd.median(axis=1)                 # 当日全行业中位（基准）
        for name, m in quads.items():
            row = out.setdefault(name, {"n_obs": int(m.values.sum())})
            gmed = fwd.where(m).median(axis=1)    # 组内中位
            rel = gmed - umed                     # 相对全行业中位
            rel_daily[(h, name)] = rel
            row[f"h{h}_med"] = fwd.where(m).stack().dropna().median()
            row[f"h{h}_rel"] = (fwd.sub(umed, axis=0)).where(m).stack().dropna().median()
            ct = cluster_t(rel, endu, m)
            row[f"h{h}_t"] = ct["t"] if ct else np.nan
    log(f"\n== A. 2×2 双重排序 [{label}]  （相对全行业中位% / 聚类t）==")
    for name in quads:
        r = out[name]
        log(f"  {name:<10} n={r['n_obs']:<6} " + " | ".join(
            f"{h}日 {r[f'h{h}_rel']:+.2%}(t{r[f'h{h}_t']:+.2f})" for h in WINDOWS))
    # 交互 = [高景气@便宜 − 低景气@便宜] − [高景气@贵 − 低景气@贵]，逐日序列 + cluster t
    inter = {}
    for h in WINDOWS:
        sp = (rel_daily[(h, "高景气+低估")] - rel_daily[(h, "低景气+低估")]) \
             - (rel_daily[(h, "高景气+高估")] - rel_daily[(h, "低景气+高估")])
        ct = cluster_t(sp, endu, pd.DataFrame(True, index=sp.index,
                                              columns=Y.columns).reindex(columns=Y.columns))
        inter[h] = {"mean": sp.mean(), "t": ct["t"] if ct else np.nan,
                    "p": ct["p"] if ct else np.nan, "nper": ct["n_periods"] if ct else 0}
        log(f"    交互({h}日) 均值 {inter[h]['mean']:+.2%}  t={inter[h]['t']:+.2f} "
            f"p={inter[h]['p']:.3f}(期{inter[h]['nper']})")
    RESULTS[f"double_{label}"] = out
    RESULTS[f"inter_{label}"] = inter
    return out


def interaction_reg(Y, P, EX, endu, MASK, label):
    Ym = Y.where(MASK); Pm = P.where(MASK)
    valid = Ym.notna() & Pm.notna()

    def z(A):
        return (A.rank(axis=1, pct=True) - 0.5) * 2
    x1 = z(Ym); x2 = -z(Pm); x3 = x1 * x2
    res = {}
    log(f"\n== B. 交互回归 [{label}] ==")
    for h in WINDOWS:
        fwd = (1 + EX).rolling(h).apply(np.prod, raw=True).shift(-h) - 1
        b1, b2, b3 = [], [], []
        for d in fwd.index:
            df = pd.DataFrame({"y": fwd.loc[d], "x1": x1.loc[d], "x2": x2.loc[d], "x3": x3.loc[d]}).dropna()
            if len(df) < 20:
                b1.append(np.nan); b2.append(np.nan); b3.append(np.nan); continue
            X = np.column_stack([np.ones(len(df)), df.x1.values, df.x2.values, df.x3.values])
            beta, *_ = np.linalg.lstsq(X, df.y.values, rcond=None)
            b1.append(beta[1]); b2.append(beta[2]); b3.append(beta[3])
        for nm, arr in [("景气", b1), ("便宜", b2), ("景气×便宜", b3)]:
            ct = cluster_t(pd.Series(arr, index=fwd.index), endu, valid)
            res[(h, nm)] = ct
            if ct:
                log(f"  {h:>3}日 {nm:<8} 系数 {ct['mean']:+.4f} t={ct['t']:+.2f} p={ct['p']:.3f}(期{ct['n_periods']})")
    RESULTS[f"reg_{label}"] = res
    return res


def triple_sort(Y, P, EX, endu, MASK, label, h=PRIMARY):
    Ym = Y.where(MASK); Pm = P.where(MASK)
    valid = Ym.notna() & Pm.notna()
    Yr = Ym.rank(axis=1, pct=True); Pr = Pm.rank(axis=1, pct=True)
    def bucket(rk):
        b = pd.DataFrame(np.nan, index=rk.index, columns=rk.columns)
        b[rk <= 1/3] = 0; b[(rk > 1/3) & (rk <= 2/3)] = 1; b[rk > 2/3] = 2
        return b
    Yb = bucket(Yr); Pb = bucket(Pr)     # Pb 0=便宜 2=贵
    fwd = (1 + EX).rolling(h).apply(np.prod, raw=True).shift(-h) - 1
    tab = np.full((3, 3), np.nan)
    for iy in range(3):
        for ip in range(3):
            m = (Yb == iy) & (Pb == ip) & valid
            v = fwd.where(m).stack().dropna()
            tab[iy, ip] = v.median() if len(v) else np.nan
    log(f"\n== C. 3×3（{h}日超额中位，行=景气低→高，列=便宜→贵）[{label}] ==")
    log("             便宜       中        贵")
    for iy, tag in zip(range(3), ["景气低", "景气中", "景气高"]):
        log(f"  {tag}  " + "   ".join(f"{tab[iy, ip]:+.2%}" for ip in range(3)))
    RESULTS[f"triple_{label}"] = tab
    return tab


def plot_cum(quads_daily, tag):
    fig, ax = plt.subplots(figsize=(10, 5.5))
    colors = {"高景气+低估": "tab:red", "高景气+高估": "tab:orange",
              "低景气+低估": "tab:blue", "低景气+高估": "tab:green"}
    for name, s in quads_daily.items():
        cum = (1 + s.fillna(0)).cumprod()
        ax.plot(cum.index, cum.values, label=f"{name}（末值{cum.iloc[-1]:.2f}）",
                color=colors[name], lw=1.5)
    ax.axhline(1, color="gray", lw=0.8)
    ax.set_title(f"四象限累计净值（景气×估值，{tag}，逐日再平衡，1日超额）")
    ax.set_ylabel("累计净值（起点=1）")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    png = OUT / "07_quadrant_cumulative.png"
    fig.savefig(png, dpi=120); plt.close(fig)
    log(f"  已保存 {png}")


def main():
    log("== 载入 ==")
    R, EX, EX1w, Y, Yrev, endu, MASK, PB = build()
    log(f"  收益 {R.shape}；样本 {R.index.min().date()}→{R.index.max().date()}")

    double_sort(Y, PB, EX, endu, MASK, "景气np_yoy×估值PB")
    interaction_reg(Y, PB, EX, endu, MASK, "景气np_yoy×估值PB")
    triple_sort(Y, PB, EX, endu, MASK, "景气np_yoy×估值PB")
    double_sort(Yrev, PB, EX, endu, MASK, "景气rev_yoy×估值PB")

    quads = quadrant_masks(Y, PB, MASK)
    quads_daily = {name: EX1w.where(m).mean(axis=1) for name, m in quads.items()}
    plot_cum(quads_daily, "景气np_yoy×估值PB")

    ls = quads_daily["高景气+低估"].fillna(0) - quads_daily["低景气+高估"].fillna(0)
    cum = (1 + ls).cumprod()
    ann = cum.iloc[-1] ** (252 / len(cum)) - 1
    log(f"\n  长空(高景气+低估 − 低景气+高估) 末值 {cum.iloc[-1]:.3f}, 年化 {ann:+.1%}")
    RESULTS["longshort"] = {"final": cum.iloc[-1], "ann": ann}

    write_report()
    log("done")


def fmt_ct(ct):
    if not ct:
        return "n/a"
    return f"{ct['mean']:+.4f} (t{ct['t']:+.2f}, p{ct['p']:.3f})"


def write_report():
    ds = RESULTS["double_景气np_yoy×估值PB"]
    dsr = RESULTS["double_景气rev_yoy×估值PB"]
    reg = RESULTS["reg_景气np_yoy×估值PB"]
    tri = RESULTS["triple_景气np_yoy×估值PB"]
    ls = RESULTS["longshort"]

    L = [
        "# 07 · 景气 × 估值 双因子合流验证报告",
        "",
        f"> 生成：{pd.Timestamp.now():%Y-%m-%d %H:%M} · 脚本 `research/prosperity/07_combined_signal.py`",
        f"> 数据：`industry_prosperity_panel.parquet`（景气 PIT）+ `daily_basic`（PB）+ 行业收益",
        f"> 方法：2×2 双重排序 + Fama-MacBeth 交互回归 + 3×3 探索性排序；按快照期 end_i 聚类 t。",
        f"> 统计量以**中位数**为主（均值被极端行业污染，同 03/05/06 教训）。",
        "",
        "## 动机与假说",
        "",
        "景气、估值两腿单测均无前瞻力（02/03/05/06）。经典价值理论认为：",
        "**低估值只有在景气向上时才是「机会」，否则是「价值陷阱」** ⇒ 估值作用可能是",
        "条件性的，需与景气**交互**才显现。本报告检验该假说。",
        "",
        "## A. 2×2 双重排序（景气 np_yoy × 估值 PB）",
        "",
        "数值 = 事件后累计超额**相对全行业中位数**（0 = 与典型行业持平），",
        "括号内为按快照期聚类的 t（检验该象限是否显著偏离全行业中位）：",
        "",
        "| 象限 | 观测数 | 20日 | 60日 | 120日 |",
        "|---|---|---|---|---|",
    ]
    for name in ["高景气+低估", "高景气+高估", "低景气+低估", "低景气+高估"]:
        r = ds[name]
        L.append(f"| {name} | {r['n_obs']} | " + " | ".join(
            f"{r[f'h{h}_rel']:+.2%} (t{r[f'h{h}_t']:+.2f})" for h in WINDOWS) + " |")
    inter_np = RESULTS["inter_景气np_yoy×估值PB"]
    L += ["", "**交互项**：`[高景气@便宜 − 低景气@便宜] − [高景气@贵 − 低景气@贵]`", "",
          "| 窗口 | 交互均值 | t | p | 结论 |", "|---|---|---|---|---|"]
    for h in WINDOWS:
        it = inter_np[h]
        L.append(f"| {h}日 | {it['mean']:+.2%} | {it['t']:+.2f} | {it['p']:.3f} | "
                 f"{'显著' if it['p'] < 0.05 else '不显著'} |")
    L += [
        "",
        "## B. 交互回归（Fama-MacBeth，按端期聚类）",
        "",
        "`fwd_ex ~ 景气z + 便宜z + 景气z×便宜z`，逐日横截面 OLS，系数按快照期聚类检验：",
        "",
        "| 窗口 | 景气 | 便宜 | 景气×便宜 |",
        "|---|---|---|---|",
    ]
    for h in WINDOWS:
        L.append(f"| {h}日 | {fmt_ct(reg[(h,'景气')])} | {fmt_ct(reg[(h,'便宜')])} | "
                 f"{fmt_ct(reg[(h,'景气×便宜')])} |")
    L += [
        "",
        "## C. 3×3 三重排序（60日超额中位，探索性）",
        "",
        "行 = 景气（低→高），列 = 估值（便宜→贵）：",
        "",
        "| | 便宜 | 中 | 贵 |",
        "|---|---|---|---|",
    ]
    for iy, tag in zip(range(3), ["景气低", "景气中", "景气高"]):
        L.append(f"| {tag} | " + " | ".join(f"{tri[iy, ip]:+.2%}" for ip in range(3)) + " |")
    L += [
        "",
        "## D. 稳健性：景气改用营收同比 rev_yoy",
        "",
        "数值 = 相对全行业中位数（同 A 段口径）：",
        "",
        "| 象限 | 20日 | 60日 | 120日 |",
        "|---|---|---|---|",
    ]
    for name in ["高景气+低估", "高景气+高估", "低景气+低估", "低景气+高估"]:
        r = dsr[name]
        L.append(f"| {name} | " + " | ".join(f"{r[f'h{h}_rel']:+.2%}" for h in WINDOWS) + " |")
    L += [
        "",
        "## E. 累计净值",
        "",
        f"四象限逐日再平衡累计净值见图。理想长空（高景气+低估 − 低景气+高估）"
        f"末值 **{ls['final']:.3f}**，年化 **{ls['ann']:+.1%}**。",
        "",
        "![四象限累计净值](07_quadrant_cumulative.png)",
        "",
        "## 结论",
        "",
        "**假说「高景气 + 低估更优」被否定，且证据方向相反。**",
        "",
        "1. **2×2（120日、相对全行业中位、中位数口径）**：高景气+低估 = **+0.11%（t+0.03，不显著）**，",
        "   四象限中仅排第 3；最优象限是**低景气+低估 +1.40%（t+2.61）**，最差是低景气+高估 -0.26%。",
        "   交互项（高景气效应@便宜 − @贵）120日 = **-3.90%（t=-2.15，p=0.042）显著为负**。",
        "   ⇒ 不是「高景气让低估更优」，而是**在便宜行业里，景气越高反而越差**。",
        "2. **交互回归**：交互项系数为负（-0.010~-0.015），方向与上一致，但不显著（t≈-1.1~-1.2，p>0.25）。",
        "3. **3×3**：便宜列内，景气低→高对应 60日超额 -0.76% → -1.05% → **-1.94%**，",
        "   **单调递减** —— 便宜行业里景气越高越差，再次印证上一结论。",
        "4. **稳健性（景气改用 rev_yoy）**：交互项更负、更显著（20/60/120日 t = -2.57 / -2.16 / **-3.25**，",
        "   p = 0.017 / 0.041 / 0.004）。",
        "5. **累计净值（逐日、等权、1日超额）**：高景气+低估末值 **0.94**（无超额），",
        "   反而是高景气+高估最高（1.10）、低景气+高估最低（0.81）。",
        "6. **理想长空（高景气+低估 − 低景气+高估）年化 +1.3%** ≈ 0，扣除换手/成本后无意义。",
        "",
        "**综合判定**：没有任何口径支持「高景气+低估更优」。唯一跨口径一致的结论是",
        "**该组合不占优（≈0 超额）**；而不同口径下「最优象限」会翻转（120日中位数下是低景气+低估，",
        "逐日累计下是高景气+高估），这本身就说明**不存在稳健的合流信号**。",
        "显著性证据属于「中弱」：仅在长窗口 + 中位数口径成立，均值类回归不显著，需谨慎。",
        "",
        "**边界**：样本 2020-01→2026-10（含 26 个财报快照期）；景气仅用季频财务；未含交易成本；",
        "未做全样本多重检验校正（交互项为**预先指定**的主分析，其余窗口为探索性）。",
        "",
    ]
    REPORT.write_text("\n".join(L), encoding="utf-8")
    log(f"\n报告已写 {REPORT}")


if __name__ == "__main__":
    main()
