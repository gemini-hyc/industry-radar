"""
行业聚合的异常样本判定（共享模块）

── 背景 ──
行业「加权涨跌幅 / 等权涨跌幅 / 宽度」都不应包含**当日无涨跌幅限制**的观测：
新股上市首日与次新期、长期停牌复牌、退市整理期首日，这些观测单日涨跌幅可达
+1942% / −96%，在行业内制造出**虚假波动**。

实测（2024-09-24 起的 490 天成分窗口，131 行业）：
  - 命中判据且 |pctChg| > 11% 的观测 约 370 条，涉及 368 个「行业-天」；
  - 剔除后行业加权涨跌幅最大修正 **77.97pp**
    （2024-10-11 包装印刷 +75.66% → −2.31%）；平均 |修正| 1.16pp；
  - 加权涨跌幅的极端值从 max 75.66% 收敛到 19.14%（余额为 924 行情真实涨幅）。

── 判据（三者取并集）──
  A. 「无涨跌幅限制日」：stk_limit 的 up_limit / down_limit 取占位值
     （实测 up_limit=1000000、down_limit=0.01）—— 精度最高，且能覆盖
     `stock_basic.parquet` 里查不到上市日期的新股（实测 60 条仅 A 能命中）。
  B. 「次新股」：上市不足 `max_age_days` 个自然日（依赖 stock_basic.list_date，
     该表可能滞后，故仅作补充，不作唯一判据）。
  C. 「数据失真」：preclose 取占位值（≤0.02）或 |pctChg| 超 `extreme_abs_pct`
     —— 兜底，覆盖 stk_limit 缺失的情形。

── 阈值说明 ──
  `min_abs_pct=11.0` 是「只剔除有实质影响的观测」的闸门：主板涨跌停 ±10%，
  取其上方留 1pp 容差。低于该阈值的异常观测对行业聚合影响可忽略，
  剔除反而会无谓地减少样本数。
  ⚠️ 创业板/科创板的 ±20% 涨跌停属**正常交易**，不会被误伤 ——
  它们只有在同时命中 A/B/C 时才会被剔除。

── 使用 ──
    from src.utils.data_quality import load_no_limit_flags, abnormal_stats, mark_abnormal
    flags = load_no_limit_flags(dates)              # {(date, code)}
    marked, stats = mark_abnormal(df, no_limit=flags)
    df = df[~marked]                                # 剔除异常观测
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple

import numpy as np
import pandas as pd

from src.paths import STK_LIMIT_DIR, STOCK_BASIC_PATH

# stk_limit 的占位值：出现即表示「当日无涨跌幅限制」
NO_LIMIT_UP = 1e5
NO_LIMIT_DOWN = 0.02

# 默认阈值
DEFAULT_MAX_AGE_DAYS = 10      # 上市不足 N 个自然日 → 视为次新
DEFAULT_MIN_ABS_PCT = 11.0     # 只有 |pctChg| 超此值才剔除
DEFAULT_EXTREME_ABS_PCT = 300.0  # 无论如何都剔除


def _to_bs(ts_code: str) -> str:
    """000001.SZ → sz.000001"""
    parts = str(ts_code).split(".")
    if len(parts) == 2:
        return f"{parts[1].lower()}.{parts[0]}"
    return str(ts_code)


def load_list_dates() -> Dict[str, pd.Timestamp]:
    """加载 上市日期映射 {bs_code: list_date}。

    注意：stock_basic.parquet 可能滞后于行情（实测缺失部分 2026 年新股），
    因此**不可作为唯一判据** —— 见模块 docstring 判据 A。
    """
    if not Path(STOCK_BASIC_PATH).exists():
        return {}
    sb = pd.read_parquet(STOCK_BASIC_PATH, columns=["ts_code", "list_date"])
    sb["code"] = sb["ts_code"].apply(_to_bs)
    ld = pd.to_datetime(sb["list_date"].astype(str), format="%Y%m%d", errors="coerce")
    return dict(zip(sb["code"], ld))


def load_no_limit_flags(dates: Iterable) -> Set[Tuple[pd.Timestamp, str]]:
    """读取 stk_limit，返回「当日无涨跌幅限制」的 (date, code) 集合。

    只读传入日期对应的文件（日更 --days 5 时仅 5 个文件，IO 可忽略）。
    """
    flags: Set[Tuple[pd.Timestamp, str]] = set()
    for d in pd.to_datetime(pd.Series(list(dates))).dt.normalize().unique():
        path = Path(STK_LIMIT_DIR) / f"{pd.Timestamp(d).strftime('%Y-%m-%d')}.parquet"
        if not path.exists():
            continue
        try:
            s = pd.read_parquet(path, columns=["code", "up_limit", "down_limit"])
        except Exception:
            continue
        hit = s["up_limit"].gt(NO_LIMIT_UP) | s["down_limit"].lt(NO_LIMIT_DOWN)
        for c in s.loc[hit, "code"]:
            flags.add((pd.Timestamp(d), c))
    return flags


def mark_abnormal(
    df: pd.DataFrame,
    *,
    no_limit: Optional[Set[Tuple[pd.Timestamp, str]]] = None,
    list_dates: Optional[Dict[str, pd.Timestamp]] = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    min_abs_pct: float = DEFAULT_MIN_ABS_PCT,
    extreme_abs_pct: float = DEFAULT_EXTREME_ABS_PCT,
) -> Tuple[pd.Series, Dict[str, int]]:
    """标记不应计入行业聚合的异常观测。

    要求 df 含列：date, code, pctChg；可选 preclose。

    返回 (mask, stats)：mask 为 True 表示「异常，应剔除」；stats 为命中计数明细。
    """
    n = len(df)
    if n == 0:
        return pd.Series([], dtype=bool), {"total": 0}

    d = pd.to_datetime(df["date"]).dt.normalize()
    code = df["code"].astype(str)

    # ── 判据 A：无涨跌幅限制日 ──
    if no_limit:
        key = pd.MultiIndex.from_arrays([d.values, code.values])
        fa = pd.Series(key.isin(list(no_limit)), index=df.index)
    else:
        fa = pd.Series(False, index=df.index)

    # ── 判据 B：次新股 ──
    if list_dates:
        ld = code.map(list_dates)
        age_days = (d - pd.to_datetime(ld)).dt.days
        fb = age_days.le(max_age_days).fillna(False)
    else:
        age_days = pd.Series(np.nan, index=df.index)
        fb = pd.Series(False, index=df.index)

    # ── 判据 C：数据失真 ──
    if "preclose" in df.columns:
        fc = df["preclose"].le(NO_LIMIT_DOWN).fillna(False)
    else:
        fc = pd.Series(False, index=df.index)
    pct = pd.to_numeric(df["pctChg"], errors="coerce")
    extreme = pct.abs().gt(extreme_abs_pct).fillna(False)

    # ── 合并：命中任一「异常情形」且波动足够大，或绝对值极端到无条件剔除 ──
    special = fa | fb | fc
    sizeable = pct.abs().gt(min_abs_pct).fillna(False)
    mask = (special & sizeable) | extreme

    stats = {
        "total": int(n),
        "no_limit_day": int(fa.sum()),
        "new_stock": int(fb.sum()),
        "bad_preclose": int(fc.sum()),
        "marked": int(mask.sum()),
    }
    return mask, stats


def format_stats(stats: Dict[str, int], prefix: str = "  ") -> str:
    """把 stats 渲染成一行可读日志。"""
    if not stats or stats.get("total", 0) == 0:
        return f"{prefix}异常样本: 无输入"
    return (f"{prefix}异常样本剔除 {stats['marked']:,} / {stats['total']:,} 条"
            f"（无涨跌幅限制日 {stats['no_limit_day']:,}"
            f" + 次新 {stats['new_stock']:,}"
            f" + 价格失真 {stats['bad_preclose']:,}）")
