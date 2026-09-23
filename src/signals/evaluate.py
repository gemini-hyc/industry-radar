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

from src.paths import INDUSTRY_DIR, SIGNALS_DIR
from src.signals import ledger as L
from src.signals.sources import SOURCE_SPECS

logger = logging.getLogger(__name__)

REGIME_PATH = INDUSTRY_DIR / "daily_regime.parquet"
MIN_BUCKET_N = 5  # 少于该样本数的分组不展示（避免小样本噪声结论）


def summarize(values: pd.Series) -> Dict:
    """一组前向收益的统计量"""
    x = pd.to_numeric(values, errors="coerce").dropna()
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": np.nan, "median": np.nan, "win": np.nan, "t": np.nan}
    mean = float(x.mean())
    std = float(x.std(ddof=1)) if n > 1 else np.nan
    t = mean / (std / math.sqrt(n)) if (n > 1 and std and std > 0) else np.nan
    return {"n": n, "mean": mean, "median": float(x.median()),
            "win": float((x > 0).mean()), "t": t}


# ── 统计 ──────────────────────────────────────────────────

def event_study(df: pd.DataFrame, horizons: Sequence[int] = L.HORIZONS) -> pd.DataFrame:
    """按持有期的总体表现"""
    rows = []
    for h in horizons:
        ex = summarize(df[f"fwd_excess_{h}"])
        ret = pd.to_numeric(df[f"fwd_ret_{h}"], errors="coerce").dropna()
        rows.append({
            "持有期": f"{h}日",
            "样本": ex["n"],
            "平均超额(pp)": ex["mean"],
            "中位超额(pp)": ex["median"],
            "胜率": ex["win"],
            "t值": ex["t"],
            "平均收益(%)": float(ret.mean()) if len(ret) else np.nan,
        })
    return pd.DataFrame(rows)


def tier_study(df: pd.DataFrame, horizon: int = 20, min_n: int = MIN_BUCKET_N) -> pd.DataFrame:
    """按 tier 分档（冷区信号自带 high/standard/watch 分级）"""
    d = df[df["tier"].notna() & (df["tier"].astype(str) != "")]
    rows = []
    for tier, g in d.groupby(d["tier"].astype(str)):
        s = summarize(g[f"fwd_excess_{horizon}"])
        if s["n"] < min_n:
            continue
        rows.append({"档位": tier, "样本": s["n"], "平均超额(pp)": s["mean"],
                     "胜率": s["win"], "t值": s["t"]})
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
        s = summarize(g[f"fwd_excess_{horizon}"])
        if s["n"] < min_n:
            continue
        rows.append({"档位": str(b), "score区间": f"{g['_score'].min():.2f} ~ {g['_score'].max():.2f}",
                     "样本": s["n"], "平均超额(pp)": s["mean"], "胜率": s["win"], "t值": s["t"]})
    out = pd.DataFrame(rows)
    return out.sort_values("档位") if len(out) else out


def year_study(df: pd.DataFrame, horizon: int = 20, min_n: int = MIN_BUCKET_N) -> pd.DataFrame:
    """按年份的稳定性"""
    d = df.copy()
    d["_year"] = pd.to_datetime(d["signal_date"]).dt.year
    rows = []
    for y, g in d.groupby("_year"):
        s = summarize(g[f"fwd_excess_{horizon}"])
        if s["n"] < min_n:
            continue
        rows.append({"年份": int(y), "样本": s["n"], "平均超额(pp)": s["mean"],
                     "胜率": s["win"], "t值": s["t"]})
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
        s = summarize(g[f"fwd_excess_{horizon}"])
        if s["n"] < min_n:
            continue
        rows.append({"regime": r, "样本": s["n"], "平均超额(pp)": s["mean"],
                     "胜率": s["win"], "t值": s["t"]})
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
        f"> 台账唯一键：(source, signal_date, ind_code)；样本不足 {MIN_BUCKET_N} 的分组不展示",
        "",
    ]

    if ledger is None or len(ledger) == 0:
        lines += ["**台账为空。**先运行 `python3 scripts/build_signals_log.py --backfill-cold-zone`。", ""]
        return "\n".join(lines)

    df = ledger
    if obs_type:
        df = df[df["obs_type"] == obs_type]

    # ── 概览 ──
    lines += ["## 台账概览", ""]
    rows = []
    for src, g in df.groupby("source"):
        matured20 = pd.to_numeric(g["fwd_excess_20"], errors="coerce").notna().sum()
        rows.append({
            "来源": f"{src}（{SOURCE_SPECS.get(src, {}).get('label', '')}）",
            "记录数": len(g),
            "类型": "、".join(sorted(g["obs_type"].astype(str).unique())),
            "日期范围": f"{pd.to_datetime(g['signal_date']).min():%Y-%m-%d} ~ "
                        f"{pd.to_datetime(g['signal_date']).max():%Y-%m-%d}",
            "20日已到期": int(matured20),
            "20日未到期": int(len(g) - matured20),
        })
    lines += [_md_table(pd.DataFrame(rows))]

    # ── 各来源详解 ──
    for src, g in df.groupby("source"):
        spec = SOURCE_SPECS.get(src, {})
        lines += [f"## 来源：{src} · {spec.get('label', '')}", ""]
        if spec.get("score_hint"):
            lines += [f"> 强度口径：{spec['score_hint']}", ""]

        lines += ["### 总体表现（事件研究）", ""]
        es = event_study(g, horizons)
        lines += [_md_table(es, float_cols=["平均超额(pp)", "中位超额(pp)", "t值"], pct_cols=["胜率"])]

        lines += [f"### 分档表现（{horizons[-2] if len(horizons) >= 2 else 20}日超额）", ""]
        h_mid = horizons[-2] if len(horizons) >= 2 else 20
        by_tier = tier_study(g, horizon=h_mid)
        if len(by_tier):
            lines += ["> 按信号自带分级（tier）", ""]
            lines += [_md_table(by_tier, float_cols=["平均超额(pp)", "t值"], pct_cols=["胜率"])]
        by_score = score_bucket_study(g, src, horizon=h_mid)
        if len(by_score):
            direction = "越小越强" if spec.get("direction") == "asc" else "越大越强"
            lines += [f"> 按 score 分位（Q1 = score 最小；本来源 {direction}）", ""]
            lines += [_md_table(by_score, float_cols=["平均超额(pp)", "t值"], pct_cols=["胜率"])]

        lines += [f"### 分年稳定性（{h_mid}日超额）", ""]
        lines += [_md_table(year_study(g, horizon=h_mid),
                            float_cols=["平均超额(pp)", "t值"], pct_cols=["胜率"])]

        lines += [f"### 按 regime 分组（{h_mid}日超额）—— 检验子阶段调制", ""]
        lines += [_md_table(regime_study(g, horizon=h_mid),
                            float_cols=["平均超额(pp)", "t值"], pct_cols=["胜率"])]

    # ── 到期情况 ──
    lines += ["## 到期情况（未到期 = 前向收益尚未走完）", ""]
    mt = L.maturity_table(df)
    mt["持有期"] = mt["horizon"].map(lambda h: f"{h}日")
    lines += [_md_table(mt[["持有期", "matured", "pending", "total"]].rename(
        columns={"matured": "已到期", "pending": "未到期", "total": "合计"}))]
    return "\n".join(lines)


def save_eval_report(markdown: str, path: Optional[Path] = None) -> Path:
    p = Path(path) if path else SIGNALS_DIR / f"eval_{datetime.now():%Y%m%d}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(markdown, encoding="utf-8")
    logger.info("评估报告已保存: %s", p)
    return p
