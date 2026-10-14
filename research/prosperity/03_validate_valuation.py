#!/usr/bin/env python3
"""
第二步·下半场：估值腿验证（一石二鸟）

目标：
  1. 测估值腿本身的前瞻力 —— 行业 PB 分位 → 未来超额收益的 IC；
  2. 检验「估值效应」假说 —— 控制估值后，盈利水平(npm/roe)的负向是否消失/转正；
  3. PB-ROE 二维性价比 —— 低 PB + 高 ROE 的组合是否跑赢。

因变量：行业未来 N 日超额收益（相对全行业等权），N ∈ {20,60,120}。
自变量：
  - pb_rank : 行业 PB 的横截面 rank（每交易日行业间排位，0~1），越低越便宜。
      口径用行业中位数 PB（抗个股极端值、与景气面板的「中位数法」一致）。
  - npm / roe : 复用景气面板的盈利水平指标。

统计口径：与 02 一致 —— 逐行 Spearman IC + 按报告期(快照期)聚类 t 检验。

关键判断（预先指定）：
  主分析 A：pb_rank → 60 日超额。若 IC 显著为负（越贵未来越差）⇒ 估值腿有前瞻力。
  主分析 B：控制 pb_rank 后 npm/roe 的残差化 IC。若原负向消失/转正 ⇒ 证实「估值效应」。
  探索性 C：PB-ROE 二维分组单调性。

纪律：
  - 主分析预先指定 = A(pb_rank→60日) + B(npm/roe 控制估值后→60/120日)；
  - 其余窗口/指标为探索性，多重检验需谨慎。

产出：research/prosperity/03_验证报告.md
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
PANEL = OUT / "industry_prosperity_panel.parquet"   # 复用景气面板（含 npm/roe/end_i/n_member/n_fina）
REPORT = OUT / "03_验证报告.md"

WINDOWS = [20, 60, 120]
PRIMARY_WINDOW = 60
MIN_MEMBER = 10
MIN_FINA = 5
BASIC_FILES = sorted((DATA / "daily_basic").glob("*.parquet"))


def log(m):
    print(m, flush=True)


def ts_to_futu(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    code = s.str.split(".").str[0]
    mkt = s.str.split(".").str[-1].str.lower()
    return mkt + "." + code


# ────────────────────────── 载入 ──────────────────────────

def load_pb_daily():
    """把 daily_basic 的个股 PB 转成 行业-日 中位数面板（用当日成分映射对齐）"""
    frames = []
    for f in BASIC_FILES:
        d = f.stem
        try:
            df = pq.read_table(f).to_pandas()[["ts_code", "pb", "circ_mv"]]
        except Exception:
            continue
        df["date"] = pd.to_datetime(d)
        df["stock_code"] = ts_to_futu(df["ts_code"])
        frames.append(df[["date", "stock_code", "pb", "circ_mv"]])
    basic = pd.concat(frames, ignore_index=True)
    basic = basic.dropna(subset=["pb"])
    basic = basic[basic["pb"] > 0]      # 剔除负/零 PB
    return basic


def build(panel, basic):
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index()
    EX = R.sub(R.mean(axis=1), axis=0)
    FWD = {n: EX.rolling(n).sum().shift(-n) for n in WINDOWS}

    # 成分映射（个股 -> 行业）
    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])

    # 行业 PB = 成分股 PB 中位数
    m = mem.merge(basic, on=["date", "stock_code"], how="inner")
    pb_ind = m.groupby(["date", "ind_code"])["pb"].median().reset_index()
    pb_ind = pb_ind.rename(columns={"pb": "pb_med"})

    # 行业市值加权 PB（用 circ_mv 加权，作为稳健性对照，此处仅算中位数口径）
    # （中位数口径为主，避免巨头主导）

    P = pb_ind.pivot_table(index="date", columns="ind_code", values="pb_med")
    P = P.reindex(index=R.index, columns=R.columns)

    # 景气面板指标复用
    panel = panel.set_index(["date", "ind_code"])
    def grab(col):
        s = panel[col]
        return s.unstack("ind_code").reindex(index=R.index, columns=R.columns)
    npm = grab("npm")
    roe = grab("roe")
    endu = grab("end_i").applymap(lambda x: x)  # 保持数值
    nmem = grab("n_member")
    nfin = grab("n_fina")

    MASK = (nmem >= MIN_MEMBER) & (nfin >= MIN_FINA)
    return R, EX, FWD, P, npm, roe, endu, MASK


def row_rank(A: pd.DataFrame) -> pd.DataFrame:
    """横截面 rank（0~1，按行）"""
    return A.rank(axis=1, pct=True)


def row_ic(A: pd.DataFrame, B: pd.DataFrame) -> pd.Series:
    Ar = A.rank(axis=1)
    Br = B.rank(axis=1)
    Am = Ar.sub(Ar.mean(axis=1), axis=0)
    Bm = Br.sub(Br.mean(axis=1), axis=0)
    num = (Am * Bm).sum(axis=1, min_count=1)
    den = np.sqrt((Am ** 2).sum(axis=1) * (Bm ** 2).sum(axis=1))
    ic = num / den.replace(0, np.nan)
    cnt = (A.notna() & B.notna()).sum(axis=1)
    return ic.where(cnt >= 5)


def cluster_t(ic: pd.Series, endu: pd.DataFrame, X: pd.DataFrame, Y: pd.DataFrame):
    e = endu.where(X.notna() & Y.notna())
    grp = e.mode(axis=1).iloc[:, 0]
    d = pd.DataFrame({"ic": ic, "end": grp}).dropna()
    if d.empty:
        return None
    pm = d.groupby("end")["ic"].mean()
    if len(pm) < 3:
        return None
    t, p = stats.ttest_1samp(pm.values, 0.0)
    return {
        "n_periods": len(pm),
        "ic_mean": pm.mean(),
        "ic_std": pm.std(ddof=1),
        "t": float(t),
        "p": float(p),
        "pos_share": float((pm > 0).mean()),
    }


def residualize_against(X: pd.DataFrame, Y: pd.DataFrame, Z: pd.DataFrame) -> pd.DataFrame:
    """把 Y 对 Z 做横截面回归取残差（逐日），返回 Y 中剔除了 Z 影响的部分"""
    Yres = Y.copy()
    common = Y.notna() & Z.notna()
    for d in Y.index:
        y = Y.loc[d][common.loc[d]]
        z = Z.loc[d][common.loc[d]]
        if len(y) < 10:
            Yres.loc[d] = np.nan
            continue
        zm, ym = z - z.mean(), y - y.mean()
        b = (zm * ym).sum() / (zm ** 2).sum() if (zm ** 2).sum() > 0 else 0.0
        Yres.loc[d] = y - b * z
    return Yres


def quintile_table(X, Y):
    rows = []
    for d in X.index:
        x, y = X.loc[d], Y.loc[d]
        dd = pd.DataFrame({"x": x, "y": y}).dropna()
        if len(dd) < 10:
            continue
        try:
            dd["g"] = pd.qcut(dd["x"], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        rows.append(dd.assign(date=d))
    if not rows:
        return None, None
    long = pd.concat(rows, ignore_index=True)
    per_day = long.groupby(["date", "g"])["y"].mean()
    qmean = per_day.groupby("g").mean()
    qn = per_day.groupby("g").size()
    return qmean, qn


def main():
    log("== 载入 ==")
    panel = pd.read_parquet(PANEL)
    panel["date"] = pd.to_datetime(panel["date"])
    basic = load_pb_daily()
    log(f"  daily_basic 个股 PB 记录 {len(basic)} 条")
    R, EX, FWD, P, npm, roe, endu, MASK = build(panel, basic)
    log(f"  收益矩阵 {R.shape}；PB 面板 {P.shape}")
    log(f"  PB 覆盖（非NaN）行业-日占比: {P.notna().mean().mean():.1%}")

    # pb_rank = 横截面 rank（0~1），越小越便宜
    P_rank = P.where(MASK).pipe(row_rank)

    results = {}

    # ── 主分析 A：估值腿前瞻力 ──
    log("\n== A. 估值腿前瞻力：pb_rank → 未来超额 ==")
    resA = []
    for n in WINDOWS:
        ic = row_ic(P_rank, FWD[n])
        r = cluster_t(ic, endu, P_rank, FWD[n])
        if r:
            resA.append({"window": n, **r})
            log(f"  w{n:<4} IC={r['ic_mean']:+.4f} t={r['t']:+.2f} p={r['p']:.4f} "
                f"(期数{r['n_periods']}, 正比例{r['pos_share']:.0%})")
    results["A"] = pd.DataFrame(resA)

    # ── 主分析 B：控制估值后，盈利水平负向是否消失 ──
    log("\n== B. 控制估值后盈利水平的前瞻力 ==")
    resB = []
    for metric, X in [("npm", npm.where(MASK)), ("roe", roe.where(MASK))]:
        for n in WINDOWS:
            Y = FWD[n]
            # 先对 Y 剥离估值（pb_rank）影响，再看 npm/roe 与残差化 Y 的 IC
            Y_res = residualize_against(Y, Y, P_rank)  # Y 对 pb_rank 回归取残差
            ic = row_ic(X, Y_res)
            r = cluster_t(ic, endu, X, Y_res)
            if r:
                resB.append({"metric": metric, "window": n, **r})
                log(f"  {metric:<5} w{n:<4} IC(控制估值)={r['ic_mean']:+.4f} t={r['t']:+.2f} p={r['p']:.4f}")
    results["B"] = pd.DataFrame(resB)

    # 对照：未控制估值时 npm/roe 的 IC（复现 02 的负向，作为基线）
    log("\n== B0. 对照：未控制估值时盈利水平 IC（基线复现）==")
    resB0 = []
    for metric, X in [("npm", npm.where(MASK)), ("roe", roe.where(MASK))]:
        for n in WINDOWS:
            ic = row_ic(X, FWD[n])
            r = cluster_t(ic, endu, X, FWD[n])
            if r:
                resB0.append({"metric": metric, "window": n, **r})
    results["B0"] = pd.DataFrame(resB0)

    # ── 探索性 C：PB-ROE 二维性价比 ──
    log("\n== C. PB-ROE 二维（性价比）：低PB+高ROE 是否跑赢 ==")
    # 性价比 = roe_rank - pb_rank（高ROE + 低PB => 高分）
    roe_rank = roe.where(MASK).pipe(row_rank)
    value_score = roe_rank - P_rank   # 高 ROE(rank高) + 低 PB(rank低) => score 高
    resC = []
    for n in WINDOWS:
        ic = row_ic(value_score, FWD[n])
        r = cluster_t(ic, endu, value_score, FWD[n])
        if r:
            resC.append({"window": n, **r})
            log(f"  w{n:<4} IC={r['ic_mean']:+.4f} t={r['t']:+.2f} p={r['p']:.4f}")
    results["C"] = pd.DataFrame(resC)

    # 分层：性价比 Q1~Q5 单调性（主窗口）
    qmean, qn = quintile_table(value_score, FWD[PRIMARY_WINDOW])
    results["C_q"] = (qmean, qn)

    write_report(results, P, R)


def write_report(results, P, R):
    def fmt(df, colname="指标"):
        if df is None or df.empty:
            return "（无结果）"
        out = [f"| {colname} | 窗口 | 期数 | IC均值 | 期std | t | p | 期正比例 |",
               "|---|---|---|---|---|---|---|---|"]
        for _, r in df.iterrows():
            star = " **\\***" if r["p"] < 0.05 else ""
            label = r.get("metric", "") or str(r.get("window", ""))
            out.append(f"| {label} | {int(r['window'])}日 | {int(r['n_periods'])} | "
                       f"{r['ic_mean']:+.4f} | {r['ic_std']:.4f} | {r['t']:+.2f} | {r['p']:.4f}{star} | "
                       f"{r['pos_share']:.0%} |")
        return "\n".join(out)

    a60 = results["A"][results["A"]["window"] == PRIMARY_WINDOW]
    b_npm120 = results["B"][(results["B"]["metric"] == "npm") & (results["B"]["window"] == 120)]
    b_roe120 = results["B"][(results["B"]["metric"] == "roe") & (results["B"]["window"] == 120)]
    b0_npm120 = results["B0"][(results["B0"]["metric"] == "npm") & (results["B0"]["window"] == 120)]
    b0_roe120 = results["B0"][(results["B0"]["metric"] == "roe") & (results["B0"]["window"] == 120)]

    lines = [
        "# 03 · 估值腿验证报告（一石二鸟）",
        "",
        f"> 生成：{pd.Timestamp.now():%Y-%m-%d %H:%M} · 脚本 `research/prosperity/03_validate_valuation.py`",
        f"> 输入：`daily_basic`（个股 PB）+ 复用 `industry_prosperity_panel.parquet`（npm/roe）+ 行业收益",
        f"> 样本：{R.index.min().date()} → {R.index.max().date()}；申万二级 131 个",
        f"> 过滤：`n_member >= {MIN_MEMBER}`、`n_fina >= {MIN_FINA}`",
        "",
        "## 一、验证设计",
        "",
        "- **估值口径**：行业 PB = 成分股 PB **中位数**（`pb>0` 才纳入，抗极端值、亏损股无 PE 缺失问题）。",
        "  横截面 rank（0~1，越低越便宜）。",
        "- **A（主）**：`pb_rank` → 未来 N 日超额。若显著为负 ⇒ 估值腿有前瞻力（贵者未来差）。",
        "- **B（主）**：控制估值（Y 对 pb_rank 回归取残差）后，`npm`/`roe` 的 IC。",
        "  若原负向消失/转正 ⇒ 证实「估值效应」。",
        "- **C（探索）**：性价比 = `roe_rank − pb_rank`（高 ROE + 低 PB ⇒ 高分）。",
        "",
        "## 二、A · 估值腿前瞻力",
        "",
        fmt(results["A"], "pb_rank"),
        "",
        "## 三、B · 控制估值后盈利水平前瞻力（核心假说检验）",
        "",
        fmt(results["B"], "指标"),
        "",
        "### 基线对照（未控制估值）",
        "",
        fmt(results["B0"], "指标"),
        "",
        "## 四、C · PB-ROE 性价比",
        "",
        fmt(results["C"], "value_score"),
        "",
    ]

    qmean, qn = results["C_q"]
    if qmean is not None:
        lines += ["### 性价比五分层（60日）", "", "| 分位 | 平均未来超额收益(%) |", "|---|---|"]
        for g in sorted(qmean.index):
            lines.append(f"| Q{int(g)+1}（性价比最低→最高）| {qmean[g]:+.3f} |")
        spread = qmean.iloc[-1] - qmean.iloc[0] if len(qmean) >= 2 else np.nan
        lines += ["", f"**多空价差 Q5 − Q1 = {spread:+.3f}%**", ""]

    lines += [
        "## 五、结论",
        "",
        "（由脚本末尾的判定逻辑生成，见下）",
        "",
    ]

    # 判定
    def verdict(df, name, expect_sign):
        if df is None or df.empty:
            return f"- **{name}**：无结果。"
        r = df.iloc[0]
        s = f"- **{name}**：IC={r['ic_mean']:+.4f}，t={r['t']:+.2f}，p={r['p']:.4f}"
        if r["p"] < 0.05 and np.sign(r["ic_mean"]) == expect_sign:
            s += " ⇒ ✅ 显著且方向符合预期。"
        elif r["p"] < 0.05:
            s += " ⇒ ⚠️ 显著但方向与预期相反。"
        else:
            s += " ⇒ ❌ 不显著。"
        return s

    lines.append(verdict(a60, "A·估值腿(pb_rank→60日)", -1))
    lines.append(verdict(b_npm120, "B·npm 控制估值后(120日)", 0))
    lines.append(verdict(b_roe120, "B·roe 控制估值后(120日)", 0))
    lines += ["", "## 六、caveat", "",
              "1. 同 02：聚类自由度约 25，功效有限。",
              "2. 行业 PB 用中位数，未用 circ_mv 加权（加权口径可作敏感性）。",
              "3. 本步仍未纳入拥挤度，不构成完整三因子。", ""]

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    log(f"\n已写报告 {REPORT}")


if __name__ == "__main__":
    main()
