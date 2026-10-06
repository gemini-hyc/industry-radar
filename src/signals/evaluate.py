"""
信号评估 — 事件研究（event study）

回答三个问题：
  1. 信号整体有没有用？  → 按持有期：样本数 / 平均超额 / 中位超额 / 胜率 / t 值
  2. 哪个档位最有用？    → 按 tier（冷区）或 score 分位
  3. 什么时候有用？      → 按年份（稳定性）、按 regime（子阶段调制）

设计原则：只做统计与呈现，不修改信号本身。任何结论都必须能被这张表复现。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from src.evaluation import cluster_t as _cluster_t
from src.evaluation import nonoverlap_t as _nonoverlap_t
from src.paths import INDUSTRY_DIR, SIGNALS_DIR
from src.signals import ledger as L
from src.signals.sources import SOURCE_SPECS, STATUS_LABEL, STATUS_ORDER
from src.utils.provenance import save_text

logger = logging.getLogger(__name__)

REGIME_PATH = INDUSTRY_DIR / "daily_regime.parquet"
MIN_BUCKET_N = 5  # 少于该样本数的分组不展示（避免小样本噪声结论）

# 观察项的统一定位声明 —— 出现在每个观察项来源的详解之前。
# ⚠️ 2026-10-05：原实现把降级日期**硬编码**成「（2026-10-01）」；当 tracker_pool 也降级时
#    就会给它安上错误日期。现改为从 SOURCE_SPECS[*]["status_since"] 读取（按来源）。
_OBSERVATIONAL_BANNER_TMPL = (
    "> ⛔ **本来源已被降级为「观察项」{when}**："
    "下表仅用于**持续观测样本积累**，**不作为交易依据，也不得据此调参**。"
)


def observational_banner(spec: Dict) -> str:
    """按来源渲染观察项横幅（日期取自该来源的 status_since）"""
    since = spec.get("status_since")
    return _OBSERVATIONAL_BANNER_TMPL.format(when=f"（{since}）" if since else "")


# ── 统计口径（2026-10-05 修正：从"朴素 t"改为三层口径）──────────────
# ⛔ 原实现用 **朴素 t** = mean/(std/√n)，把「同一交易日的多个行业」与「重叠的前向窗口」
#    都当成独立样本 ⇒ **t 系统性偏高**。实测同一批数据：20 日口径朴素 t=−5.49，
#    按日聚类后 −1.38、非重叠抽样 −1.50（对照组也同步塌陷，说明是估计量问题而非信号）。
# 现改为三层 + 一个强制项：
#   ① **事件级去重**：`obs_type='state'` 的同一行业连续交易日合并为一个事件（间隔 ≤ STATE_MERGE_DAYS）
#   ② **按日聚类（主判据）**：先按 signal_date 求横截面均值，再对"日序列"算 t
#   ③ **非重叠抽样（辅助）**：按 signal_date 每 horizon 个取 1，消除前向窗口重叠
#   ④ **必须并列报中位数与胜率**：均值显著而中位数≈0 是**尾部效应**，不是效应
STATE_MERGE_DAYS = 7      # 状态型来源：同行业相邻信号日间隔 ≤ 此天数 ⇒ 视为同一事件
MIN_CLUSTER_N = 5         # 独立日数下限（聚类推断的功效看簇数，不看条数）


def dedup_events(df: pd.DataFrame) -> pd.DataFrame:
    """事件级去重（①）。

    ⚠️ `obs_type='event'` 的来源（`cold_zone` 的"恰好第 3 天"、`tracker_pool` 的入场事件）
    **本身已是事件级** ⇒ 去重对它们是 no-op。真正被合并的是 `mainline`（每日状态快照）。
    """
    if df is None or len(df) == 0 or "obs_type" not in df.columns:
        return df
    d = df.sort_values(["source", "ind_code", "signal_date"]).copy()
    keep = pd.Series(True, index=d.index)
    for _, g in d[d["obs_type"].astype(str) == "state"].groupby(["source", "ind_code"]):
        prev = None
        for idx, dt in zip(g.index, pd.to_datetime(g["signal_date"])):
            if prev is not None and (dt - prev).days <= STATE_MERGE_DAYS:
                keep[idx] = False
            prev = dt
    return d[keep]


def cluster_t(values: pd.Series, dates: pd.Series):
    """按日聚类的 t（②，**主判据**）。返回 (t, 独立日数)。

    🔴 2026-10-06：实现已收敛到 `src/evaluation/inference.py`（全项目唯一口径层）。
       此处保留函数名以兼容既有调用与测试；⛔ 不得再本地实现统计逻辑。
    """
    return _cluster_t(values, dates)


def nonoverlap_t(values: pd.Series, dates: pd.Series, horizon: int):
    """非重叠抽样的 t（③，辅助）：按 signal_date 每 horizon 个取 1。返回 (t, 子样本数)。

    🔴 2026-10-06：实现已收敛到 `src/evaluation/inference.py`。详见那里的
       「两种 nonoverlap_t 的输入契约差异」说明—— 事件流必须用本函数
       （按日期去重），⛔ 不可与 `nonoverlap_t_on_index`（Series 口径）混用。
    """
    return _nonoverlap_t(values, dates, horizon)


def summarize(values: pd.Series, dates: Optional[pd.Series] = None,
              horizon: Optional[int] = None) -> Dict:
    """一组前向收益的统计量（含聚类 t / 非重叠 t / 中位数 / 胜率）。

    `dates` 给出则同时计算 ②③；`horizon` 给出则计算 ③。
    """
    x = pd.to_numeric(values, errors="coerce").dropna()
    n = len(x)
    base = {"n": 0, "mean": np.nan, "median": np.nan, "win": np.nan,
            "t": np.nan, "t_cluster": np.nan, "n_days": 0, "t_nonoverlap": np.nan}
    if n == 0:
        return base
    mean = float(x.mean())
    std = float(x.std(ddof=1)) if n > 1 else np.nan
    out = dict(base)
    out.update({
        "n": n, "mean": mean, "median": float(x.median()),
        "win": float((x > 0).mean()),
        # 朴素 t 保留，仅供与历史报告对照 —— ⛔ 不得作为判据
        "t": mean / (std / math.sqrt(n)) if (n > 1 and std and std > 0) else np.nan,
    })
    if dates is not None:
        dd = dates.reindex(x.index)
        out["t_cluster"], out["n_days"] = cluster_t(x, dd)
        if horizon:
            out["t_nonoverlap"], _ = nonoverlap_t(x, dd, horizon)
    return out


# ── 统计 ──────────────────────────────────────────────────

def event_study(df: pd.DataFrame, horizons: Sequence[int] = L.HORIZONS) -> pd.DataFrame:
    """按持有期的总体表现（三层口径：事件级去重 → 按日聚类 → 非重叠抽样）"""
    rows = []
    for h in horizons:
        s = summarize(df[f"fwd_excess_{h}"], dates=df["signal_date"], horizon=h)
        ret = pd.to_numeric(df[f"fwd_ret_{h}"], errors="coerce").dropna()
        rows.append({
            "持有期": f"{h}日",
            "事件数": s["n"],
            "独立日数": s["n_days"],
            "平均超额(pp)": s["mean"],
            "中位超额(pp)": s["median"],
            "胜率": s["win"],
            "t(聚类)": s["t_cluster"],
            "t(非重叠)": s["t_nonoverlap"],
            "平均收益(%)": float(ret.mean()) if len(ret) else np.nan,
        })
    return pd.DataFrame(rows)


def tier_study(df: pd.DataFrame, horizon: int = 20, min_n: int = MIN_BUCKET_N) -> pd.DataFrame:
    """按 tier 分档（冷区信号自带 high/standard/watch 分级）"""
    d = df[df["tier"].notna() & (df["tier"].astype(str) != "")]
    rows = []
    for tier, g in d.groupby(d["tier"].astype(str)):
        s = summarize(g[f"fwd_excess_{horizon}"], dates=g["signal_date"], horizon=horizon)
        if s["n"] < min_n or s["n_days"] < MIN_CLUSTER_N:
            continue
        rows.append({"档位": tier, "事件数": s["n"], "独立日数": s["n_days"],
                     "平均超额(pp)": s["mean"], "中位超额(pp)": s["median"],
                     "胜率": s["win"], "t(聚类)": s["t_cluster"]})
    out = pd.DataFrame(rows)
    return out.sort_values("平均超额(pp)", ascending=False) if len(out) else out


def score_bucket_study(
    df: pd.DataFrame,
    source: str,
    horizon: int = 20,
    n_buckets: int = 3,
    min_n: int = MIN_BUCKET_N,
) -> pd.DataFrame:
    """按 score 分位分档；Q1 恒为 score 最小的一组（方向见 SOURCE_SPECS）"""
    d = df.copy()
    d["_score"] = pd.to_numeric(d["score"], errors="coerce")
    d = d.dropna(subset=["_score"])
    if d["_score"].nunique() < n_buckets:
        return pd.DataFrame()
    try:
        d["档位"] = pd.qcut(d["_score"], n_buckets, labels=[f"Q{i+1}" for i in range(n_buckets)],
                            duplicates="drop")
    except ValueError:
        return pd.DataFrame()

    rows = []
    for b, g in d.groupby("档位", observed=True):
        s = summarize(g[f"fwd_excess_{horizon}"], dates=g["signal_date"], horizon=horizon)
        if s["n"] < min_n or s["n_days"] < MIN_CLUSTER_N:
            continue
        rows.append({"档位": str(b), "score区间": f"{g['_score'].min():.2f} ~ {g['_score'].max():.2f}",
                     "事件数": s["n"], "独立日数": s["n_days"], "平均超额(pp)": s["mean"],
                     "中位超额(pp)": s["median"], "胜率": s["win"], "t(聚类)": s["t_cluster"]})
    out = pd.DataFrame(rows)
    return out.sort_values("档位") if len(out) else out


def year_study(df: pd.DataFrame, horizon: int = 20, min_n: int = MIN_BUCKET_N) -> pd.DataFrame:
    """按年份的稳定性"""
    d = df.copy()
    d["_year"] = pd.to_datetime(d["signal_date"]).dt.year
    rows = []
    for y, g in d.groupby("_year"):
        s = summarize(g[f"fwd_excess_{horizon}"], dates=g["signal_date"], horizon=horizon)
        if s["n"] < min_n or s["n_days"] < MIN_CLUSTER_N:
            continue
        rows.append({"年份": int(y), "事件数": s["n"], "独立日数": s["n_days"],
                     "平均超额(pp)": s["mean"], "中位超额(pp)": s["median"],
                     "胜率": s["win"], "t(聚类)": s["t_cluster"]})
    return pd.DataFrame(rows)


def regime_study(df: pd.DataFrame, horizon: int = 20, min_n: int = MIN_BUCKET_N) -> pd.DataFrame:
    """按当日 regime 分组 —— 检验"子阶段调制"这一历史回测最大发现"""
    if not REGIME_PATH.exists():
        return pd.DataFrame()
    reg = pd.read_parquet(REGIME_PATH)[["date", "regime"]].copy()
    reg["date"] = pd.to_datetime(reg["date"])
    d = df.copy()
    d["signal_date"] = pd.to_datetime(d["signal_date"])
    d = d.merge(reg, left_on="signal_date", right_on="date", how="left")

    rows = []
    for r, g in d.groupby(d["regime"].astype(str)):
        if r in ("nan", "None", ""):
            continue
        s = summarize(g[f"fwd_excess_{horizon}"], dates=g["signal_date"], horizon=horizon)
        if s["n"] < min_n or s["n_days"] < MIN_CLUSTER_N:
            continue
        rows.append({"regime": r, "事件数": s["n"], "独立日数": s["n_days"],
                     "平均超额(pp)": s["mean"], "中位超额(pp)": s["median"],
                     "胜率": s["win"], "t(聚类)": s["t_cluster"]})
    out = pd.DataFrame(rows)
    return out.sort_values("平均超额(pp)", ascending=False) if len(out) else out


# ── 呈现 ──────────────────────────────────────────────────

def _md_table(df: pd.DataFrame, float_cols: Sequence[str] = (), pct_cols: Sequence[str] = ()) -> str:
    if df is None or len(df) == 0:
        return "_（样本不足，暂不展示）_\n"
    d = df.copy()
    for c in float_cols:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").map(
                lambda v: "—" if pd.isna(v) else f"{v:.2f}")
    for c in pct_cols:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").map(
                lambda v: "—" if pd.isna(v) else f"{v * 100:.1f}%")
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "|" + "|".join([":---"] + [":---:" for _ in d.columns[1:]]) + "|"
    body = [
        "| " + " | ".join("—" if pd.isna(v) else str(v) for v in row) + " |"
        for row in d.itertuples(index=False)
    ]
    return "\n".join([head, sep] + body) + "\n"


def render_markdown(
    ledger: pd.DataFrame,
    horizons: Sequence[int] = L.HORIZONS,
    obs_type: Optional[str] = None,
) -> str:
    """把评估结果渲染成 markdown 报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines: List[str] = [f"# 📋 信号台账评估 · {today}", ""]
    lines += [
        f"> 基准：{L.BENCHMARK_LABEL}",
        "> 口径：前向收益 = 信号日 T+1..T+h 逐日加总；超额 = 前向收益 − 基准同期收益",
        f"> 台账唯一键：(source, signal_date, ind_code)；样本不足 {MIN_BUCKET_N} / "
        f"独立日数不足 {MIN_CLUSTER_N} 的分组不展示",
        "",
        "> 🔴 **推断口径（2026-10-05 修正）**：原用 **朴素 t**（mean/(std/√n)），把"
        "「同一日的多个行业」与「重叠的前向窗口」都当成独立样本 ⇒ **t 系统性偏高**"
        "（实测同批数据 20 日口径：朴素 −5.49 → 按日聚类 −1.38）。现改为三层：",
        "> ① **事件级去重**（`obs_type='state'` 的同行连续交易日合并为一个事件）；"
        "② **按日聚类 t**（**主判据**）；③ **非重叠抽样 t**（每 h 个信号日取 1，辅助对照）。",
        "> ④ **一律并列中位数与胜率** —— 均值显著而中位数≈0 是**尾部效应**，不是效应。",
        "> ⛔ 报告不再展示朴素 t（避免被当判据引用）；与 2026-10-05 之前的历史报告对比时须显式换算。",
        "",
    ]

    if ledger is None or len(ledger) == 0:
        lines += ["**台账为空。**先运行 `python3 scripts/build_signals_log.py --backfill-cold-zone`。", ""]
        return "\n".join(lines)

    df0 = ledger
    if obs_type:
        df0 = df0[df0["obs_type"] == obs_type]
    raw_n = df0.groupby("source").size()            # 去重前条数（供概览对照）
    df = dedup_events(df0)                          # ① 事件级去重

    # ── 信号定位（先讲清哪些是信号、哪些只是观察项，再看数字）──
    lines += ["## 信号定位", "",
              "> 定位决定数字该怎么用：`生效` 可作依据；`观察项` 只积累样本；`待观察` 样本不足。", ""]
    pos_rows = []
    for src in sorted(df["source"].unique(),
                      key=lambda s: (STATUS_ORDER.get(
                          SOURCE_SPECS.get(s, {}).get("status", "pending"), 9), str(s))):
        spec = SOURCE_SPECS.get(src, {})
        status = spec.get("status", "pending")
        pos_rows.append({
            "来源": f"{src}（{spec.get('label', '')}）",
            "定位": STATUS_LABEL.get(status, status),
            "一句话": spec.get("status_short", "—"),
        })
    lines += [_md_table(pd.DataFrame(pos_rows)), ""]

    # ── 概览 ──
    # 🔴 2026-10-06：在册与已弃用**分组呈现**，不混在一张表里。
    #   理由：混排时读者会把「已弃用来源的漂亮历史数字」误当成在册证据 ——
    #   冷区 2,234 行里均值 +0.34pp 看着像 alpha，而它中位数是 −0.13pp。
    #   分组后「已弃用」标题自带警告，数字不再有被误读的机会。
    lines += ["## 台账概览", ""]
    all_rows = []
    for src, g in df.groupby("source"):
        spec = SOURCE_SPECS.get(src, {})
        status = spec.get("status", "pending")
        matured20 = pd.to_numeric(g["fwd_excess_20"], errors="coerce").notna().sum()
        all_rows.append({
            "来源": f"{src}（{spec.get('label', '')}）",
            "定位": STATUS_LABEL.get(status, status),
            "记录数": int(raw_n.get(src, 0)),
            "去重事件数": len(g),
            "类型": "、".join(sorted(g["obs_type"].astype(str).unique())),
            "日期范围": f"{pd.to_datetime(g['signal_date']).min():%Y-%m-%d} ~ "
                        f"{pd.to_datetime(g['signal_date']).max():%Y-%m-%d}",
            "20日已到期": int(matured20),
            "20日未到期": int(len(g) - matured20),
        })
    ledger_df = pd.DataFrame(all_rows)
    if ledger_df.empty:
        lines += ["（台账为空）", ""]
    else:
        live = ledger_df[ledger_df["定位"] != STATUS_LABEL["retired"]]
        dead = ledger_df[ledger_df["定位"] == STATUS_LABEL["retired"]]
        if not live.empty:
            lines += ["### 在册来源", "",
                      "> 数字可用于决策判断。", ""]
            lines += [_md_table(live), ""]
        if not dead.empty:
            n_dead = int(dead["记录数"].sum())
            lines += ["### 🚫 已弃用来源（数字仅供追溯，**不可作决策依据**）", "",
                      f"> 合计 {n_dead} 条历史记录。弃用日期与理由见上方「信号定位」表。", ""]
            lines += [_md_table(dead), ""]
    retired_srcs = [s for s in df["source"].unique()
                    if SOURCE_SPECS.get(s, {}).get("status") == "retired"]
    lines += [
        f"> ⛔ 已弃用来源：**{'、'.join(sorted(retired_srcs)) or '无'}** —— "
        "不再新增入账，历史行保留只为可追溯。",
        "",
    ]

    # ── 各来源详解 ──
    for src, g in df.groupby("source"):
        spec = SOURCE_SPECS.get(src, {})
        status = spec.get("status", "pending")
        lines += [f"## 来源：{src} · {spec.get('label', '')}", ""]
        if status == "observational":
            lines += [observational_banner(spec)]
            if spec.get("status_note"):
                lines += [f"> 依据：{spec['status_note']}"]
            doc = spec.get("status_doc")
            lines += [f"> 定位变更与回滚方式见 `{doc}`。"] if doc else [
                "> 定位变更与回滚方式见对应 `docs/changes-*.md`。"]
        elif spec.get("status_note"):
            lines += [f"> 定位：**{STATUS_LABEL.get(status, status)}**　{spec['status_note']}"]
        lines += [""]
        if spec.get("score_hint"):
            lines += [f"> 强度口径：{spec['score_hint']}", ""]

        lines += ["### 总体表现（事件研究）", ""]
        es = event_study(g, horizons)
        lines += [_md_table(es,
                            float_cols=["平均超额(pp)", "中位超额(pp)", "t(聚类)", "t(非重叠)",
                                        "平均收益(%)"],
                            pct_cols=["胜率"])]

        lines += [f"### 分档表现（{horizons[-2] if len(horizons) >= 2 else 20}日超额）", ""]
        h_mid = horizons[-2] if len(horizons) >= 2 else 20
        by_tier = tier_study(g, horizon=h_mid)
        if len(by_tier):
            lines += ["> 按信号自带分级（tier）", ""]
            lines += [_md_table(by_tier,
                                float_cols=["平均超额(pp)", "中位超额(pp)", "t(聚类)"],
                                pct_cols=["胜率"])]
        by_score = score_bucket_study(g, src, horizon=h_mid)
        if len(by_score):
            direction = "越小越强" if spec.get("direction") == "asc" else "越大越强"
            lines += [f"> 按 score 分位（Q1 = score 最小；本来源 {direction}）", ""]
            lines += [_md_table(by_score,
                                float_cols=["平均超额(pp)", "中位超额(pp)", "t(聚类)"],
                                pct_cols=["胜率"])]

        lines += [f"### 分年稳定性（{h_mid}日超额）", ""]
        lines += [_md_table(year_study(g, horizon=h_mid),
                            float_cols=["平均超额(pp)", "中位超额(pp)", "t(聚类)"],
                            pct_cols=["胜率"])]

        lines += [f"### 按 regime 分组（{h_mid}日超额）—— 检验子阶段调制", ""]
        lines += [_md_table(regime_study(g, horizon=h_mid),
                            float_cols=["平均超额(pp)", "中位超额(pp)", "t(聚类)"],
                            pct_cols=["胜率"])]

    # ── 到期情况 ──
    lines += ["## 到期情况（未到期 = 前向收益尚未走完）", ""]
    mt = L.maturity_table(df)
    mt["持有期"] = mt["horizon"].map(lambda h: f"{h}日")
    lines += [_md_table(mt[["持有期", "matured", "pending", "total"]].rename(
        columns={"matured": "已到期", "pending": "未到期", "total": "合计"}))]
    return "\n".join(lines)


def save_eval_report(markdown: str, path: Optional[Path] = None) -> Path:
    p = Path(path) if path else SIGNALS_DIR / f"eval_{datetime.now():%Y%m%d}.md"
    saved = save_text(
        markdown, p,
        producer="src/signals/evaluate.py",
        params={"horizons": list(L.HORIZONS)},
        inputs=[SIGNALS_DIR / "signals_log.parquet", REGIME_PATH],
    )
    logger.info("评估报告已保存: %s", saved)
    return saved
