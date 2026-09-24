"""
情绪数据 → 行业级两融占比面板（带缓存 + 增量更新）

数据源：`<数据根>/sentiment/margin_detail/YYYY-MM-DD.parquet`（**逐只 A 股**两融明细）
聚合口径：按**当日成分**把 `rzye`（融资余额）求和到行业，再除以行业流通市值：

    因子 = Σ(行业成分股 rzye) / 行业 circ_mv      （越高 = 杠杆资金参与越深）

成分口径：优先 PIT（`industry_members_daily.parquet`，2024-09-24 起）；
          之前的历史用静态快照 `industry_members.parquet` **兜底**，并在 `members_src`
          列标记为 `static`，避免把两种口径混为一谈。

实证结论（docs/sentiment-data-evaluation.md）：
  · 独立因子：20 日 IC +0.103 / ICIR 0.364（剔除规模后不变，与拥挤度因子正交）
  · 但 2026 年 IC 降至 +0.016 → **当独立因子不稳**
  · 当冷区信号过滤器两年一致（低分位组 20 日超额 −1.67pp，高分位组 +1.50pp）

产出缓存：`<数据根>/signals/industry_margin_daily.parquet`（长表，含横截面分位）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from src.paths import DATA_DIR, INDUSTRY_DIR, SIGNALS_DIR

logger = logging.getLogger(__name__)

MARGIN_DIR = DATA_DIR / "sentiment" / "margin_detail"
# 缓存路径可用环境变量覆盖（便于测试 / 换数据根）
PANEL_PATH = Path(os.environ.get("INDUSTRY_RADAR_MARGIN_PANEL")
                  or (SIGNALS_DIR / "industry_margin_daily.parquet"))
WEIGHTED_RETURNS_PATH = INDUSTRY_DIR / "industry_weighted_returns.parquet"
MEMBERS_PIT_PATH = INDUSTRY_DIR / "industry_members_daily.parquet"
MEMBERS_STATIC_PATH = INDUSTRY_DIR / "industry_members.parquet"

PIT_START = pd.Timestamp("2024-09-24")  # PIT 成分起始日（早于此用静态快照兜底）
PANEL_COLS = ["date", "ind_code", "rzye", "circ_mv", "margin_ratio", "margin_pct", "members_src"]


def available_dates(start: Optional[str] = None, end: Optional[str] = None):
    """margin_detail 目录下可用的交易日"""
    if not MARGIN_DIR.exists():
        return []
    dates = sorted(
        pd.Timestamp(f.stem) for f in MARGIN_DIR.glob("*.parquet")
        if f.stem[:2] == "20" and len(f.stem) == 10
    )
    if start is not None:
        dates = [d for d in dates if d >= pd.Timestamp(start)]
    if end is not None:
        dates = [d for d in dates if d <= pd.Timestamp(end)]
    return dates


def _circ_mv_panel() -> pd.DataFrame:
    wr = pd.read_parquet(WEIGHTED_RETURNS_PATH, columns=["date", "ind_code", "circ_mv"])
    wr["date"] = pd.to_datetime(wr["date"])
    return wr.pivot(index="date", columns="ind_code", values="circ_mv").sort_index()


def _members_maps() -> Tuple[dict, pd.DataFrame]:
    """返回 (PIT 按日字典, 静态兜底表)"""
    pit = {}
    if MEMBERS_PIT_PATH.exists():
        m = pd.read_parquet(MEMBERS_PIT_PATH)
        m["date"] = pd.to_datetime(m["date"])
        pit = {d: g[["stock_code", "ind_code"]] for d, g in m.groupby("date")}
    static = pd.read_parquet(MEMBERS_STATIC_PATH)[["stock_code", "ind_code"]]
    return pit, static


def build_panel(
    start: Optional[str] = None,
    end: Optional[str] = None,
    refresh: bool = True,
    progress_every: int = 200,
) -> pd.DataFrame:
    """
    构建/增量刷新行业两融面板并落盘缓存。

    refresh=True 时只处理缓存未覆盖的新日期（日频链路增量更新）。
    """
    cached = pd.DataFrame(columns=PANEL_COLS)
    if refresh and PANEL_PATH.exists():
        cached = pd.read_parquet(PANEL_PATH)
        cached["date"] = pd.to_datetime(cached["date"])

    done_dates = set(cached["date"].unique()) if len(cached) else set()
    want = [d for d in available_dates(start, end) if d not in done_dates]

    if not want:
        logger.info("两融面板无需更新（已覆盖 %d 个交易日）", len(done_dates))
        return cached

    pit, static = _members_maps()
    circ = _circ_mv_panel()
    rows = []
    for i, d in enumerate(want, 1):
        f = MARGIN_DIR / f"{d:%Y-%m-%d}.parquet"
        try:
            m = pd.read_parquet(f, columns=["code", "rzye"])
        except Exception as e:
            logger.warning("读取两融明细失败，跳过 %s: %s", d, e)
            continue

        if d in pit:
            members, src = pit[d], "pit"
        else:
            members, src = static, "static"   # 兜底：静态快照（有轻微前视，已标记）

        mm = m.merge(members, left_on="code", right_on="stock_code", how="inner")
        if mm.empty:
            continue
        agg = mm.groupby("ind_code")["rzye"].sum()
        circ_d = circ.loc[d] if d in circ.index else None
        if circ_d is None:
            continue

        df = pd.DataFrame({"rzye": agg})
        df["circ_mv"] = circ_d.reindex(df.index) * 1e4   # 源表 circ_mv 单位为万元 → 换算为元
        df = df[df["circ_mv"] > 0]
        df["margin_ratio"] = df["rzye"] / df["circ_mv"]
        df["margin_pct"] = df["margin_ratio"].rank(pct=True)
        df["date"] = d
        df["members_src"] = src
        rows.append(df.reset_index())

        if progress_every and i % progress_every == 0:
            print(f"  … 两融面板增量 {i}/{len(want)} 天", flush=True)

    fresh = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=PANEL_COLS)
    panel = pd.concat([cached, fresh], ignore_index=True) if len(cached) else fresh
    panel = panel[PANEL_COLS].sort_values(["date", "ind_code"]).reset_index(drop=True)

    PANEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(PANEL_PATH, index=False)
    logger.info("两融面板已更新: %s（%d 天 / %d 行）", PANEL_PATH, panel["date"].nunique(), len(panel))
    return panel


def load_panel(auto_build: bool = True) -> pd.DataFrame:
    if PANEL_PATH.exists():
        p = pd.read_parquet(PANEL_PATH)
        p["date"] = pd.to_datetime(p["date"])
        return p
    if auto_build:
        return build_panel()
    return pd.DataFrame(columns=PANEL_COLS)


# ── 供台账 / 日报查询 ─────────────────────────────────────

def percentile_series(date, panel: Optional[pd.DataFrame] = None) -> pd.Series:
    """某日各行业的两融占比横截面分位（index=ind_code，0~1）"""
    p = load_panel() if panel is None else panel
    if p is None or len(p) == 0:
        return pd.Series(dtype=float)
    d = pd.Timestamp(date)
    sub = p[p["date"] == d]
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.set_index("ind_code")["margin_pct"]


def percentile_lookup(panel: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """(date × ind_code) 分位矩阵，供批量回填使用"""
    p = load_panel() if panel is None else panel
    if p is None or len(p) == 0:
        return pd.DataFrame()
    return p.pivot(index="date", columns="ind_code", values="margin_pct").sort_index()


def ratio_lookup(panel: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """(date × ind_code) 原始比值矩阵（融资余额/流通市值）"""
    p = load_panel() if panel is None else panel
    if p is None or len(p) == 0:
        return pd.DataFrame()
    return p.pivot(index="date", columns="ind_code", values="margin_ratio").sort_index()
