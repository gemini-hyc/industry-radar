#!/usr/bin/env python3
"""
第二步 · 阶段1：构造 PIT 行业景气面板（严格无前视）

课题：行业景气（PIT 净利润/营收同比增速）对未来行业超额收益是否有前瞻力？
本脚本只做「数据 + 特征」：把个股财务指标聚合成行业景气面板，并输出数据质量报告。

🔴 数据源决策（2026-10-13）：
  最初用 `financial_statements/income`（自己拼同比基期），但实测发现其早期报告期几乎为空：
    2020Q4=2 条、2021Q4=25 条、2022Q4=330 条、2023Q1=21 条、2023Q2=76 条、2023Q3=29 条
    ⇒ 2023Q4 才有完整覆盖(5582) ⇒ 双向完整的同比期只剩 2024Q4~2026Q2 共 7 期，样本过短。
  改用 `fundamental/fina_*.parquet`（Tushare fina_indicator）：
    - 覆盖 2020Q1~2026Q2 共 26 期，早期每期约 2000~4100 只（远好于 income 的个位数）
    - **自带 Tushare 预计算的同比字段** `netprofit_yoy`/`or_yoy`/`q_sales_yoy`，无需本地拼基期
    - `ann_date` 100% 非空 ⇒ 可做严格 PIT
  代价：无绝对净利润字段 ⇒ 聚合只能用**中位数法**（不能用加总法）。
      这对本课题反而是优点：中位数对成分覆盖率不敏感、抗极端值（同比常有 -125%/+1000% 这类）。

PIT 规则（关键）：
  - 交易日 t 只能使用 ann_date <= t 的财报（公告日，非报告期 end_date）。
  - 最新报告期无值时沿时间轴前向填充，并记录 end_date_used 与 staleness_days 诊断陈旧度。

产出：
  research/prosperity/industry_prosperity_panel.parquet
  research/prosperity/01_数据质量报告.md
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
DATA = Path("/Users/hyc/quant-data")
OUT = ROOT / "research" / "prosperity"
OUT.mkdir(parents=True, exist_ok=True)

PANEL_OUT = OUT / "industry_prosperity_panel.parquet"
REPORT_OUT = OUT / "01_数据质量报告.md"

START = "2020-01-01"

# 景气候选指标（Tushare fina_indicator 字段 -> 面板列名）
METRICS = {
    "netprofit_yoy": "np_yoy",      # 累计净利润同比（主）
    "or_yoy": "rev_yoy",            # 累计营收同比
    "q_sales_yoy": "q_rev_yoy",     # 单季营收同比（更灵敏）
    "roe": "roe",                   # ROE 水平（估值锚/盈利质量）
    "netprofit_margin": "npm",      # 净利率水平
}


def ts_to_futu(ts: pd.Series) -> pd.Series:
    """Tushare '600679.SH' / '000001.SZ' -> 富途 'sh.600679' / 'sz.000001'"""
    s = ts.astype(str).str.strip()
    code = s.str.split(".").str[0]
    mkt = s.str.split(".").str[-1].str.lower()
    return mkt + "." + code


def log(msg: str) -> None:
    print(msg, flush=True)


# ────────────────────────── 1. 财报 ──────────────────────────

def load_fina() -> pd.DataFrame:
    """读取 fina_indicator 全部分期文件（排除精简版 fina_all）"""
    files = [f for f in sorted((DATA / "fundamental").glob("**/*.parquet"))
             if f.name != "fina_all.parquet"]
    frames = []
    for f in files:
        d = pq.read_table(f).to_pandas()
        need = ["ts_code", "end_date", "ann_date"] + list(METRICS.keys())
        if not all(c in d.columns for c in need):
            log(f"  [warn] {f.name} 缺字段，跳过")
            continue
        frames.append(d[need].copy())
    df = pd.concat(frames, ignore_index=True)

    df["end_date"] = df["end_date"].astype(str).str.strip()
    df["ann_date"] = pd.to_datetime(df["ann_date"].astype(str), errors="coerce")
    df["code"] = ts_to_futu(df["ts_code"])
    df = df.dropna(subset=["ann_date"])

    # 同一 (code, end_date) 可能来自重复文件（2026Q1 有顶层与子目录两份）⇒ 去重，保留 ann_date 最新
    df = df.sort_values(["code", "end_date", "ann_date"]).drop_duplicates(
        ["code", "end_date"], keep="last"
    )
    for c in METRICS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["end_i"] = df["end_date"].astype(int)
    return df


# ────────────────────────── 2. 展开到日频 ──────────────────────────

def expand_to_dates(df: pd.DataFrame, dates: pd.DatetimeIndex):
    """对每只股票，找出每个交易日可见的最新财报（ann_date <= t）"""
    df = df.sort_values(["code", "ann_date"], kind="stable")
    codes = list(dict.fromkeys(df["code"]))
    code_idx = {c: j for j, c in enumerate(codes)}

    d_i64 = dates.values.astype("datetime64[ns]").astype(np.int64)
    n_d, n_s = len(dates), len(codes)

    keys = list(METRICS.keys()) + ["end_i"]
    mats = {k: np.full((n_d, n_s), np.nan) for k in keys}

    for c, g in df.groupby("code", sort=False):
        j = code_idx[c]
        a = g["ann_date"].values.astype("datetime64[ns]").astype(np.int64)
        idx = np.searchsorted(a, d_i64, side="right") - 1
        ok = idx >= 0
        if not ok.any():
            continue
        ii = idx[ok]
        for k in keys:
            mats[k][ok, j] = g[k].to_numpy(float)[ii]

    return codes, mats


# ────────────────────────── 3. 行业聚合 ──────────────────────────

def aggregate_to_industry(codes, mats, dates, members: pd.DataFrame) -> pd.DataFrame:
    """
    中位数法（本数据源唯一可行，且对覆盖率/极端值稳健）：
      行业景气 = 成分股该指标的**中位数**。
    同时给出 n_fina（有财报的成分股数）用于后续过滤小样本行业。
    """
    mapped = set(members["stock_code"].unique())
    keep = [j for j, c in enumerate(codes) if c in mapped]
    if not keep:
        raise RuntimeError("成分映射与财报代码无交集，检查代码格式转换")

    n_d, n_k = len(dates), len(keep)
    keep_codes = np.array([codes[j] for j in keep])

    data = {"date": np.repeat(dates.values, n_k), "stock_code": np.tile(keep_codes, n_d)}
    for k in list(METRICS.keys()) + ["end_i"]:
        data[k] = mats[k][:, keep].ravel()
    long = pd.DataFrame(data)

    mm = members.merge(long, on=["date", "stock_code"], how="inner")
    if mm.empty:
        raise RuntimeError("成分与财报长表 merge 后为空")

    g = mm.groupby(["date", "ind_code"], sort=False)
    out = g.agg(
        n_fina=("netprofit_yoy", "count"),
        n_member=("stock_code", "nunique"),
        end_i=("end_i", "max"),
    )
    for src, dst in METRICS.items():
        out[dst] = g[src].median()
    out = out.reset_index()
    out["coverage"] = out["n_fina"] / out["n_member"].replace(0, np.nan)
    return out


def apply_ffill_and_staleness(panel: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """各指标沿时间轴前向填充；end_i 同步填充以记录"当前使用的报告期"与陈旧度"""
    cols = ["end_i", "n_fina", "n_member"] + list(METRICS.values())
    out = []
    for ic, g in panel.groupby("ind_code", sort=False):
        g = g.set_index("date").sort_index().reindex(dates)
        g["ind_code"] = ic
        for c in cols:
            if c in g.columns:
                g[c] = g[c].ffill()
        out.append(g.reset_index().rename(columns={"index": "date"}))
    res = pd.concat(out, ignore_index=True)

    end_dt = pd.to_datetime(res["end_i"].astype("Int64").astype(str), format="%Y%m%d", errors="coerce")
    res["end_date_used"] = end_dt
    res["staleness_days"] = (res["date"] - end_dt).dt.days
    return res


# ────────────────────────── main ──────────────────────────

def main() -> None:
    log("== 1. 载入 fina_indicator ==")
    fin = load_fina()
    log(f"  记录 {len(fin)} 条；股票 {fin['code'].nunique()}；报告期 {fin['end_date'].nunique()} 个")
    log(f"  报告期: {sorted(fin['end_date'].unique())[0]} … {sorted(fin['end_date'].unique())[-1]}")
    for src in METRICS:
        log(f"    {src:<18} 非空 {fin[src].notna().mean():.1%}")

    log("== 2. 载入交易日 & 成分映射 ==")
    wr = pq.read_table(DATA / "industry" / "industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    all_dates = pd.DatetimeIndex(sorted(wr["date"].unique()))
    dates = all_dates[all_dates >= START]
    log(f"  交易日 {len(dates)} 个（{dates.min().date()} → {dates.max().date()}）")

    mem = pq.read_table(DATA / "industry" / "industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])
    mem = mem[mem["date"].isin(dates)]
    log(f"  成分映射 {len(mem)} 行")

    log("== 3. 展开到日频（PIT）==")
    codes, mats = expand_to_dates(fin, dates)
    log(f"  股票 {len(codes)}；日频 netprofit_yoy 覆盖率 {np.isfinite(mats['netprofit_yoy']).mean():.1%}")

    log("== 4. 行业聚合（中位数）==")
    panel = aggregate_to_industry(codes, mats, dates, mem)
    log(f"  行业-日 {len(panel)} 条；行业 {panel['ind_code'].nunique()} 个")

    log("== 5. 前向填充 & 陈旧度 ==")
    panel = apply_ffill_and_staleness(panel, dates)

    il = pq.read_table(DATA / "industry" / "industry_list.parquet").to_pandas()
    name_map = dict(zip(il["ts_code"], il["name"]))
    l2l1 = json.load(open(DATA / "industry" / "sw_l2_to_l1.json"))
    panel["ind_name"] = panel["ind_code"].map(name_map)
    panel["l1_name"] = panel["ind_name"].map(lambda n: l2l1.get(n, n) if isinstance(n, str) else None)

    panel.to_parquet(PANEL_OUT, index=False)
    log(f"  已保存 {PANEL_OUT} ({len(panel)} 行)")

    log("\n== 6. 质量诊断 ==")
    eff = panel[panel["np_yoy"].notna()]
    log(f"  有有效 np_yoy 的行业-日: {len(eff)} ({len(eff)/max(len(panel),1):.1%})")
    log(f"  有效区间: {eff['date'].min().date()} → {eff['date'].max().date()}")
    snap = (eff.groupby("end_date_used")
              .agg(起始日=("date", "min"), 行业数=("ind_code", "nunique"),
                   staleness中位=("staleness_days", "median"))
              .reset_index())
    log(f"  独立景气快照(报告期)数: {len(snap)}")
    log("    " + snap.to_string(index=False).replace("\n", "\n    "))
    log(f"  coverage 中位: {eff['coverage'].median():.2f}；n_fina 中位: {eff['n_fina'].median():.0f}")
    log(f"  np_yoy 分布: {eff['np_yoy'].describe()[['min','25%','50%','75%','max']].round(2).to_dict()}")

    write_report(panel, eff, snap, fin, dates)


def write_report(panel, eff, snap, fin, dates) -> None:
    n_ind = panel["ind_code"].nunique()
    d = eff["np_yoy"].describe()
    lines = [
        "# 01 · 数据质量报告（PIT 行业景气面板）",
        "",
        "> 课题：行业景气（PIT 净利润/营收同比增速）对未来行业超额收益的前瞻力验证",
        f"> 生成：{pd.Timestamp.now():%Y-%m-%d %H:%M} · 脚本 `research/prosperity/01_build_panel.py`",
        f"> 产出：`industry_prosperity_panel.parquet`（{len(panel)} 行）",
        "",
        "## 一、🔴 数据源决策（重要，曾踩坑）",
        "",
        "**最初用 `financial_statements/income` 自行拼同比基期，实测其早期报告期几乎为空**：",
        "",
        "| 报告期 | 2020Q4 | 2021Q4 | 2022Q4 | 2023Q1 | 2023Q2 | 2023Q3 | 2023Q4 |",
        "|---|---|---|---|---|---|---|---|",
        "| 记录数 | **2** | **25** | **330** | **21** | **76** | **29** | 5582 |",
        "",
        "⇒ 双向完整（本期+去年同报告期）的同比期只剩 **2024Q4~2026Q2 共 7 期**，样本过短，无法支撑验证。",
        "",
        "**改用 `fundamental/fina_*.parquet`（Tushare `fina_indicator`）**：",
        "",
        f"- 覆盖 **{fin['end_date'].nunique()} 期**（{sorted(fin['end_date'].unique())[0]} → {sorted(fin['end_date'].unique())[-1]}），"
        f"每期约 2000~4100 只",
        "- **自带 Tushare 预计算的同比字段** `netprofit_yoy` / `or_yoy` / `q_sales_yoy` ⇒ 无需本地拼基期，绕开上面那个坑",
        "- `ann_date` 100% 非空 ⇒ 可做严格 PIT",
        f"- 股票 {fin['code'].nunique()} 只，记录 {len(fin)} 条",
        "",
        "**代价 / 影响**：该源无绝对净利润字段 ⇒ **聚合只能用中位数法，不能用加总法**。",
        "对本课题这反而是优点：同比常有 -125% / +1000% 这类极端值，**中位数对覆盖率不敏感且抗极端值**，",
        "而加总法会被个别巨头或巨亏股主导。",
        "",
        "## 二、PIT 规则（防前视）",
        "",
        "1. 交易日 t **只用 `ann_date <= t` 的财报**（公告日，非报告期 `end_date`）。",
        "2. 最新报告期无值时沿时间轴前向填充，并记录 `end_date_used` / `staleness_days` 诊断陈旧度。",
        "3. 同一 (code, end_date) 来自重复文件时保留 `ann_date` 最新的一条。",
        "",
        "## 三、景气指标定义（行业中位数）",
        "",
        "| 面板列 | 源字段 | 含义 | 角色 |",
        "|---|---|---|---|",
        "| `np_yoy` | `netprofit_yoy` | 累计净利润同比增速 | **主指标** |",
        "| `rev_yoy` | `or_yoy` | 累计营收同比增速 | 辅（更上游，少受非经常损益影响）|",
        "| `q_rev_yoy` | `q_sales_yoy` | **单季**营收同比增速 | 辅（更灵敏，反映最新季度边际）|",
        "| `roe` | `roe` | ROE 水平 | 盈利质量 / 估值锚 |",
        "| `npm` | `netprofit_margin` | 净利率水平 | 盈利质量 |",
        "",
        "## 四、面板质量",
        "",
        f"- 行业-日记录 {len(panel)} 条，行业 {n_ind} 个，交易日 {len(dates)} 个"
        f"（{dates.min().date()} → {dates.max().date()}）",
        f"- 有有效 `np_yoy` 的行业-日：**{len(eff)}** ({len(eff)/max(len(panel),1):.1%})",
        f"- 有效区间：**{eff['date'].min().date()} → {eff['date'].max().date()}**",
        f"- **独立景气快照（报告期）数：{len(snap)}** ← 统计上的有效独立样本量",
        f"- 成分财报覆盖率 coverage 中位 **{eff['coverage'].median():.2f}**；每行业有财报的成分股数中位 **{eff['n_fina'].median():.0f}**",
        f"- staleness 中位 **{eff['staleness_days'].median():.0f} 天**",
        f"- `np_yoy` 分布：min {d['min']:.1f} / P25 {d['25%']:.1f} / 中位 {d['50%']:.1f} / "
        f"P75 {d['75%']:.1f} / max {d['max']:.1f}",
        "",
        "### 各快照期明细",
        "",
        "| 报告期 | 起效日 | 覆盖行业数 | staleness中位 |",
        "|---|---|---|---|",
    ]
    for _, r in snap.iterrows():
        lines.append(
            f"| {r['end_date_used'].date()} | {r['起始日'].date()} | {int(r['行业数'])} | "
            f"{r['staleness中位']:.0f}d |"
        )
    lines += [
        "",
        "## 五、🔴 关键 caveat（影响后续验证设计）",
        "",
        "1. **景气是季频离散更新的**，日频上相邻交易日景气值完全相同 ⇒ 日频 IC 高度自相关，",
        "   **有效独立样本 ≈ 快照期数（见上表），不是交易日数**。",
        "   后续验证必须**按快照期（报告期）聚类**做显著性检验；按日聚类会严重高估显著性。",
        f"2. **独立快照期 {len(snap)} 个** ⇒ 聚类后自由度约 {len(snap)-1}，统计功效有限，结论须谨慎。",
        "3. **staleness 衰减**：距报告期末越久，景气越旧（中位约 2~3 个月）。",
        "   后续可做敏感性分析（限制 staleness 上限）。",
        "4. **`fina_indicator` 覆盖率非 100%**（每期约 2000~4100 只，占全市场 ~40%~70%）⇒",
        "   行业中位数建立在子集上，可能有一定选择性偏差（大市值/优质公司更可能被覆盖）。",
        "5. `netprofit_yoy` 为**累计口径**同比；`q_sales_yoy` 为单季，两者经济含义不同，不可混用。",
        "",
        "## 六、下一步",
        "",
        "- 阶段3/4：用本面板跑验证 —— 横截面 rank IC + 分层单调性 + **按快照期聚类 t 检验** + IS/OOS，",
        "  并在**申万二级（主）与一级（对照）**上同时跑。",
        "- 主分析过滤 `n_member >= 10`（对象论证结论：小行业噪声大）且 `n_fina >= 5`。",
        "",
    ]
    REPORT_OUT.write_text("\n".join(lines), encoding="utf-8")
    log(f"\n已写报告 {REPORT_OUT}")


if __name__ == "__main__":
    main()
