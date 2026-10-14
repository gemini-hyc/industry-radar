#!/usr/bin/env python3
"""
第二步 · 阶段3/4：行业景气前瞻力验证

因变量：行业未来 N 日**超额**收益（相对全行业等权）
自变量：行业景气（PIT，行业中位数）—— 主指标 `np_yoy`（累计净利同比）
方法：
  1. 日频横截面 rank IC（Spearman）
  2. **按快照期（报告期）聚类 t 检验** ← 关键：景气季频离散更新，按日聚类会严重高估显著性
  3. 五分层单调性
  4. IS / OOS 时间划分
  5. 层级对照：申万二级（主） vs 申万一级（对照）
  6. 稳健性：控制过去 20 日收益（排除"景气 IC 只是动量"）

纪律：主分析**预先指定** = `np_yoy` → 未来 60 日超额收益；其余指标/窗口为探索性，多重检验需谨慎。

产出：research/prosperity/02_验证报告.md
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
REPORT = OUT / "02_验证报告.md"

MIN_MEMBER = 10   # 对象论证结论：小行业噪声大
MIN_FINA = 5      # 每行业至少 5 只有财报
WINDOWS = [20, 60, 120]
PRIMARY_WINDOW = 60
PRIMARY_METRIC = "np_yoy"
IS_END = "2024-06-30"   # IS/OOS 时间切分


def log(m):
    print(m, flush=True)


# ────────────────────────── 载入 ──────────────────────────

def load():
    panel = pd.read_parquet(PANEL)
    panel["date"] = pd.to_datetime(panel["date"])

    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    return panel, wr


def build_matrices(panel, wr):
    """返回 收益矩阵 R、超额矩阵 EX、未来累计超额 FWD、景气矩阵 Xs、有效掩码 MASK"""
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct")
    R = R.sort_index()
    bench = R.mean(axis=1)                      # 全行业等权基准
    EX = R.sub(bench, axis=0)                   # 当日超额

    FWD = {}
    for n in WINDOWS:
        FWD[n] = EX.rolling(n).sum().shift(-n)  # t+1 .. t+n 累计超额

    metrics = ["np_yoy", "rev_yoy", "q_rev_yoy", "roe", "npm"]
    Xs = {}
    for m in metrics:
        Xs[m] = panel.pivot_table(index="date", columns="ind_code", values=m).reindex(
            index=R.index, columns=R.columns)

    nmem = panel.pivot_table(index="date", columns="ind_code", values="n_member").reindex(
        index=R.index, columns=R.columns)
    nfin = panel.pivot_table(index="date", columns="ind_code", values="n_fina").reindex(
        index=R.index, columns=R.columns)
    endu = panel.pivot_table(index="date", columns="ind_code", values="end_i",
                             aggfunc="max").reindex(index=R.index, columns=R.columns)

    MASK = (nmem >= MIN_MEMBER) & (nfin >= MIN_FINA)
    return R, EX, FWD, Xs, MASK, endu


# ────────────────────────── 统计 ──────────────────────────

def row_rank_corr(A: pd.DataFrame, B: pd.DataFrame) -> pd.Series:
    """逐行 Spearman（对秩做 Pearson）；行内样本 <5 置 NaN"""
    Ar = A.rank(axis=1)
    Br = B.rank(axis=1)
    Am = Ar.sub(Ar.mean(axis=1), axis=0)
    Bm = Br.sub(Br.mean(axis=1), axis=0)
    num = (Am * Bm).sum(axis=1, min_count=1)
    den = np.sqrt((Am ** 2).sum(axis=1) * (Bm ** 2).sum(axis=1))
    out = num / den.replace(0, np.nan)
    cnt = (A.notna() & B.notna()).sum(axis=1)
    return out.where(cnt >= 5)


def cluster_t(ic: pd.Series, endu: pd.Series):
    """
    按快照期（报告期）聚类：先把日频 IC 按 end_i 分组求均值，再对期均值做 one-sample t 检验。
    返回 dict。
    """
    d = pd.DataFrame({"ic": ic, "end": endu}).dropna()
    if d.empty:
        return None
    pm = d.groupby("end")["ic"].mean()
    if len(pm) < 3:
        return {"n_periods": len(pm), "ic_mean": np.nan, "t": np.nan, "p": np.nan,
                "ic_std": np.nan, "pos_share": np.nan}
    t, p = stats.ttest_1samp(pm.values, 0.0)
    return {
        "n_periods": len(pm),
        "ic_mean": pm.mean(),
        "ic_std": pm.std(ddof=1),
        "t": float(t),
        "p": float(p),
        "pos_share": float((pm > 0).mean()),
        "ic_daily_mean": ic.mean(),
    }


def quintile_table(X: pd.DataFrame, Y: pd.DataFrame):
    """
    五分层：按日把行业按景气分 5 组，先算每日每组未来收益均值，再对日取平均
    （每天权重相同，避免某些天行业数多而权重过大）。
    ⚠️ 曾踩坑：不要用 pd.DataFrame([Series,...]) 逐日拼接，会触发类型错误。
    """
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


# ────────────────────────── 主流程 ──────────────────────────

def run_level(Xs, FWD, MASK, endu, level_name):
    """对某层级跑全部指标 × 窗口"""
    rows = []
    for metric, X in Xs.items():
        Xm = X.where(MASK)
        for n in WINDOWS:
            Y = FWD[n]
            ic = row_rank_corr(Xm, Y)
            res = cluster_t(ic, endu.stack().reindex(ic.index) if False else endu_masked(endu, Xm, Y))
            if res is None:
                continue
            rows.append({
                "level": level_name, "metric": metric, "window": n,
                **res,
            })
    return pd.DataFrame(rows)


def endu_masked(endu, Xm, Y):
    """
    取每行的"代表报告期"（当日有效行业的 end_i 众数），用于聚类分组。
    ⚠️ 曾踩坑：mode() 结果的列名是 0,1,2...，不能拿原 DataFrame 的 columns 去判断，
       否则会误走 iloc[:,0] 分支（用"第一个行业"的报告期代表全市场），导致期数塌缩。
    """
    e = endu.where(Xm.notna() & Y.notna())
    return e.mode(axis=1).iloc[:, 0]


def main():
    log("== 载入 ==")
    panel, wr = load()
    R, EX, FWD, Xs, MASK, endu = build_matrices(panel, wr)
    log(f"  收益矩阵 {R.shape}（{R.index.min().date()} → {R.index.max().date()}）")
    log(f"  有效行业-日（过滤后）: {int(MASK.sum().sum())}")

    # ── 主分析：二级 ──
    log("\n== 申万二级（主）==")
    res_l2 = []
    for metric, X in Xs.items():
        Xm = X.where(MASK)
        for n in WINDOWS:
            Y = FWD[n]
            ic = row_rank_corr(Xm, Y)
            grp = endu_masked(endu, Xm, Y)
            r = cluster_t(ic, grp)
            if r is None:
                log(f"  {metric:<14} w{n:<4} 样本不足")
                continue
            res_l2.append({"metric": metric, "window": n, **r})
            flag = "*" if r["p"] < 0.05 else " "
            log(f"  {metric:<14} w{n:<4} IC均值{r['ic_mean']:+.4f}  期t={r['t']:+.2f}  p={r['p']:.3f}{flag}"
                f"  (期数{r['n_periods']}, 正比例{r['pos_share']:.0%})")
    res_l2 = pd.DataFrame(res_l2)

    # ── 分层单调性（主指标 + 主窗口）──
    log(f"\n== 五分层单调性（{PRIMARY_METRIC} → {PRIMARY_WINDOW}日）==")
    Xp = Xs[PRIMARY_METRIC].where(MASK)
    Yp = FWD[PRIMARY_WINDOW]
    qmean, qn = quintile_table(Xp, Yp)
    log("  分位 0(最差) → 4(最好) 平均未来超额收益(%):")
    for g in sorted(qmean.index):
        log(f"    Q{int(g)+1}: {qmean[g]:+.3f}  (日-组样本 {int(qn[g])})")
    mono = qmean.dropna()
    spread = mono.iloc[-1] - mono.iloc[0] if len(mono) >= 2 else np.nan
    log(f"  多空价差 Q5-Q1 = {spread:+.3f}%")

    # ── 层级对照：申万一级 ──
    log("\n== 申万一级（对照）==")
    l1_map = panel.dropna(subset=["l1_name"]).drop_duplicates("ind_code").set_index("ind_code")["l1_name"]
    il = pq.read_table(DATA / "industry" / "industry_list.parquet").to_pandas()

    def to_l1(df: pd.DataFrame, how="median"):
        df = df.copy()
        g = df.T.groupby(l1_map.reindex(df.columns))
        return (g.median() if how == "median" else g.mean()).T

    Xs_l1 = {m: to_l1(X.where(MASK)) for m, X in Xs.items()}
    FWD_l1 = {n: to_l1(FWD[n]) for n in WINDOWS}
    endu_l1 = to_l1(endu, how="median")

    res_l1 = []
    for metric in Xs_l1:
        Xm = Xs_l1[metric]
        for n in WINDOWS:
            Y = FWD_l1[n]
            ic = row_rank_corr(Xm, Y)
            grp = endu_masked(endu_l1, Xm, Y)
            r = cluster_t(ic, grp)
            if r is None:
                continue
            res_l1.append({"metric": metric, "window": n, **r})
            flag = "*" if r["p"] < 0.05 else " "
            log(f"  {metric:<14} w{n:<4} IC均值{r['ic_mean']:+.4f}  期t={r['t']:+.2f}  p={r['p']:.3f}{flag}")
    res_l1 = pd.DataFrame(res_l1)

    # ── IS / OOS ──
    log("\n== IS / OOS（主指标）==")
    oos = {}
    for name, sub in [("IS", Xp.index <= IS_END), ("OOS", Xp.index > IS_END)]:
        Xi, Yi = Xp[sub], Yp[sub]
        ic = row_rank_corr(Xi, Yi)
        grp = endu_masked(endu[sub], Xi, Yi)
        r = cluster_t(ic, grp)
        oos[name] = r
        if r:
            log(f"  {name:<4} 期数{r['n_periods']:>3}  IC均值{r['ic_mean']:+.4f}  t={r['t']:+.2f}  p={r['p']:.3f}")

    # ── 稳健性：控制过去 20 日收益 ──
    log("\n== 稳健性：剥离过去20日收益后（排除'景气IC只是动量'）==")
    mom = EX.rolling(20).sum()            # 过去 20 日累计超额（截至当日）
    robust = {}
    for n in WINDOWS:
        Y = FWD[n]
        # 残差化：把未来收益对动量做横截面回归取残差
        Yres = Y.copy()
        common = Y.notna() & mom.notna()
        for d in Y.index:
            y = Y.loc[d][common.loc[d]]
            x = mom.loc[d][common.loc[d]]
            if len(y) < 10:
                Yres.loc[d] = np.nan
                continue
            xm, ym = x - x.mean(), y - y.mean()
            b = (xm * ym).sum() / (xm ** 2).sum() if (xm ** 2).sum() > 0 else 0.0
            Yres.loc[d] = y - b * x
        ic = row_rank_corr(Xp, Yres)
        grp = endu_masked(endu, Xp, Yres)
        r = cluster_t(ic, grp)
        robust[n] = r
        if r:
            log(f"  w{n:<4} IC均值{r['ic_mean']:+.4f}  t={r['t']:+.2f}  p={r['p']:.3f}")

    write_report(res_l2, res_l1, oos, robust, qmean, spread, R)


def write_report(res_l2, res_l1, oos, robust, qmean, spread, R):
    def fmt(df):
        if df is None or df.empty:
            return "（无结果）"
        out = ["| 指标 | 窗口 | 期数 | IC均值 | 期均值标准差 | t | p | 期正比例 |",
                "|---|---|---|---|---|---|---|---|"]
        for _, r in df.iterrows():
            star = " **\\***" if r["p"] < 0.05 else ""
            out.append(
                f"| `{r['metric']}` | {int(r['window'])}日 | {int(r['n_periods'])} | "
                f"{r['ic_mean']:+.4f} | {r['ic_std']:.4f} | {r['t']:+.2f} | {r['p']:.3f}{star} | "
                f"{r['pos_share']:.0%} |")
        return "\n".join(out)

    primary = res_l2[(res_l2["metric"] == PRIMARY_METRIC) & (res_l2["window"] == PRIMARY_WINDOW)]
    pr = primary.iloc[0] if len(primary) else None

    verdict = "❌ 未通过" if pr is None or pr["p"] >= 0.05 or pr["ic_mean"] <= 0 else "✅ 通过"
    verdict_note = (
        f"主分析 `{PRIMARY_METRIC}` → {PRIMARY_WINDOW} 日超额：IC 均值 {pr['ic_mean']:+.4f}，"
        f"按 {int(pr['n_periods'])} 个快照期聚类 t={pr['t']:+.2f}，p={pr['p']:.4f} ⇒ **{verdict}**。"
        if pr is not None else "主分析无结果。"
    )

    lines = [
        "# 02 · 行业景气前瞻力验证报告",
        "",
        f"> 生成：{pd.Timestamp.now():%Y-%m-%d %H:%M} · 脚本 `research/prosperity/02_validate.py`",
        f"> 输入：`01_build_panel.py` 产出的 PIT 景气面板 + `industry_weighted_returns.parquet`",
        f"> 样本：{R.index.min().date()} → {R.index.max().date()}；申万二级 131 个",
        f"> 过滤：行业成分股 `n_member >= {MIN_MEMBER}`、有财报成分 `n_fina >= {MIN_FINA}`",
        "",
        "## 一、验证设计",
        "",
        "- **因变量**：行业未来 N 日**超额**收益（`t+1 … t+N` 累计，基准=全行业等权）",
        "- **自变量**：行业景气（PIT，行业中位数）。主指标 `np_yoy`（累计净利同比）",
        "- **显著性口径**：**按快照期（报告期）聚类 t 检验**。",
        "  理由：景气季频离散更新，日频相邻交易日自变量完全相同，按日聚类会严重高估显著性。",
        f"- **纪律**：主分析**预先指定**为 `np_yoy` → {PRIMARY_WINDOW} 日；其余指标/窗口为探索性，",
        "  多重检验需谨慎解读（5 指标 × 3 窗口 = 15 次）。",
        "",
        "## 二、主分析结果",
        "",
        verdict_note,
        "",
        "### 申万二级（主）",
        "",
        fmt(res_l2),
        "",
        "### 申万一级（对照）",
        "",
        fmt(res_l1),
        "",
        "## 三、五分层单调性（主指标 → 60 日）",
        "",
        "| 分位 | 平均未来超额收益(%) |",
        "|---|---|",
    ]
    for g in sorted(qmean.index):
        lines.append(f"| Q{int(g)+1}（景气最低→最高）| {qmean[g]:+.3f} |")
    lines += [
        "",
        f"**多空价差 Q5 − Q1 = {spread:+.3f}%**（60 日累计超额）",
        "",
        "## 四、IS / OOS（主指标 → 60 日）",
        "",
        "| 区间 | 期数 | IC均值 | t | p |",
        "|---|---|---|---|---|",
    ]
    for k, v in oos.items():
        if v:
            lines.append(f"| {k} | {int(v['n_periods'])} | {v['ic_mean']:+.4f} | {v['t']:+.2f} | {v['p']:.3f} |")
        else:
            lines.append(f"| {k} | — | — | — | — |")
    lines += [
        "",
        "## 五、稳健性：剥离过去 20 日收益后（排除「景气 IC 只是动量」）",
        "",
        "| 窗口 | IC均值 | t | p |",
        "|---|---|---|---|",
    ]
    for n, v in robust.items():
        if v:
            lines.append(f"| {n}日 | {v['ic_mean']:+.4f} | {v['t']:+.2f} | {v['p']:.3f} |")
        else:
            lines.append(f"| {n}日 | — | — | — |")

    l2_main = res_l2[(res_l2["metric"] == PRIMARY_METRIC) & (res_l2["window"] == PRIMARY_WINDOW)]
    l1_main = res_l1[(res_l1["metric"] == PRIMARY_METRIC) & (res_l1["window"] == PRIMARY_WINDOW)]
    cmp = ""
    if len(l2_main) and len(l1_main):
        a, b = l2_main.iloc[0], l1_main.iloc[0]
        cmp = (f"主指标 {PRIMARY_WINDOW} 日：二级 IC={a['ic_mean']:+.4f}(t={a['t']:+.2f}) vs "
               f"一级 IC={b['ic_mean']:+.4f}(t={b['t']:+.2f})")

    lines += [
        "",
        "## 六、层级对照结论",
        "",
        cmp or "（数据不足）",
        "",
        "## 七、🔴 caveat 与解读边界",
        "",
        "1. **聚类后自由度有限**：独立快照期为 26 个（季频），t 检验自由度约 25 ⇒ 统计功效有限，",
        "   边缘显著结果不应过度解读。",
        "2. **本验证只测「横截面 IC」**，未做交易成本、行业权重约束、调仓频率等实务约束下的回测；",
        "   IC 显著 ≠ 策略可盈利。",
        "3. **一级景气为近似**：由二级景气取中位数聚合而成（未从个股级重算），对照结论为方向性。",
        "4. `fina_indicator` 覆盖率约 40%~70%（非全市场），行业中位数建立在子集上，可能有选择性偏差。",
        "5. 景气为**累计同比**（`np_yoy`）与**单季同比**（`q_rev_yoy`）语义不同，不可混用。",
        "6. 本步只回答「景气有无前瞻力」，尚未纳入估值/拥挤 ⇒ 不构成为完整的三因子结论。",
        "",
        "## 八、下一步（依结果分支）",
        "",
        "- 若主分析**通过**：进入三因子框架，把估值（`daily_basic` PE/PB 分位）与拥挤度（过滤器）接进来，",
        "  做横截面排序 → watchlist → 日报。",
        "- 若主分析**未通过**：需转向其他腿（估值 / 产业链基本面传导 / 宏观政策），或重新审视景气口径",
        "  （如改用「景气变化/预期修正」而非「景气水平」）。",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    log(f"\n已写报告 {REPORT}")


if __name__ == "__main__":
    main()
