"""
信号台账 — 全系统信号的统一登记与验证底座

动机
----
系统里存在多套信号（冷区宽度反转、贝叶斯反转跟踪、主线方向），但每套只在
自己的脚本里"打印一次"：信号发出后没有 follow-through，无法判断对错；回测
结论散落在 research/ 与 docs/，数据地基一变就不可复现。

本模块把所有信号写入同一张表，并回填前向收益，使"信号到底行不行"成为一条
可每日更新、与数据同源的结论。

台账结构（signals_log.parquet）
------------------------------
  signal_date     信号日（datetime64）
  source          来源：cold_zone / tracker_pool / mainline
  obs_type        event（一次性事件）/ state（每日状态快照）
  ind_code        行业代码（== industry_list.ts_code）
  ind_name        行业名称
  score           数值强度（口径随来源不同，见 sources.SOURCE_SPECS）
  tier            分级标签（可空）
  features        来源特有特征快照（JSON 字符串）
  logged_at       入账时间（审计）
  fwd_ret_{h}     信号日后 h 个交易日累计收益（%，T+1..T+h 逐日加总）
  fwd_excess_{h}  同期行业等权基准超额（pp）= fwd_ret - 基准同期收益

幂等性
------
唯一键 = (source, signal_date, ind_code)。重复写入时元数据取最后一次，
收益列保留已有非空值，因此回填/重跑不产生重复记录、也不丢已验证结果。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd

from src.paths import INDUSTRY_DIR, SIGNALS_LOG_PATH

logger = logging.getLogger(__name__)

HORIZONS: Sequence[int] = (5, 10, 20, 60)
RETURN_COLS: List[str] = [f"fwd_ret_{h}" for h in HORIZONS]
EXCESS_COLS: List[str] = [f"fwd_excess_{h}" for h in HORIZONS]
KEYS: List[str] = ["source", "signal_date", "ind_code"]
BASE_COLS: List[str] = [
    "signal_date", "source", "obs_type", "ind_code", "ind_name",
    "score", "tier", "features", "logged_at",
]
ALL_COLS: List[str] = BASE_COLS + RETURN_COLS + EXCESS_COLS

BENCHMARK_LABEL = "行业等权（全部行业当日加权涨跌幅的横截面均值）"
WEIGHTED_RETURNS_PATH = INDUSTRY_DIR / "industry_weighted_returns.parquet"

PathLike = Union[str, Path]


# ── 台账读写 ──────────────────────────────────────────────

def empty_ledger() -> pd.DataFrame:
    return pd.DataFrame(columns=ALL_COLS)


def _resolve_path(path: Optional[PathLike]) -> Path:
    return Path(path) if path else SIGNALS_LOG_PATH


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """补齐列、统一 dtype，保证下游处理稳定"""
    df = df.copy()
    for col in ("signal_date", "logged_at"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
    for col in ALL_COLS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[ALL_COLS]


def load_ledger(path: Optional[PathLike] = None) -> pd.DataFrame:
    p = _resolve_path(path)
    if not p.exists():
        return empty_ledger()
    return _normalize(pd.read_parquet(p))


def save_ledger(ledger: pd.DataFrame, path: Optional[PathLike] = None) -> Path:
    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _normalize(ledger).to_parquet(p, index=False)
    logger.info("信号台账已保存: %s (%d 条)", p, len(ledger))
    return p


def make_row(
    source: str,
    signal_date,
    ind_code: str,
    ind_name: str = "",
    score=None,
    tier=None,
    obs_type: str = "event",
    features: Optional[Dict] = None,
    logged_at=None,
) -> Dict:
    """构造一条台账记录（收益列留空，由回填补齐）"""
    return {
        "signal_date": pd.Timestamp(signal_date),
        "source": source,
        "obs_type": obs_type,
        "ind_code": str(ind_code),
        "ind_name": ind_name,
        "score": score,
        "tier": tier,
        "features": json.dumps(features or {}, ensure_ascii=False),
        "logged_at": pd.Timestamp(logged_at) if logged_at is not None else pd.Timestamp.now(),
    }


def upsert(ledger: pd.DataFrame, new_rows) -> pd.DataFrame:
    """按唯一键合并：元数据取最后一次写入，收益列保留已有非空值"""
    if isinstance(new_rows, list):
        new_rows = pd.DataFrame(new_rows)
    if new_rows is None or len(new_rows) == 0:
        return _normalize(ledger)

    new = _normalize(new_rows)
    old = _normalize(ledger)
    if old.empty:
        return new.sort_values(["signal_date", "source", "ind_code"]).reset_index(drop=True)

    # 收益列统一为数值 dtype，避免全 NA object 列参与 concat
    for col in RETURN_COLS + EXCESS_COLS:
        old[col] = pd.to_numeric(old[col], errors="coerce")
        new[col] = pd.to_numeric(new[col], errors="coerce")

    combined = pd.concat([old, new], ignore_index=True)
    if combined.empty:
        return combined

    # 元数据：同一键取最后一次写入
    meta = combined.drop_duplicates(subset=KEYS, keep="last")[BASE_COLS]

    # 收益列：groupby.last() 取每键最后一个非空值（保留既有验证结果）
    returns = combined.groupby(KEYS, dropna=False)[RETURN_COLS + EXCESS_COLS].last()

    out = meta.merge(returns, left_on=KEYS, right_index=True, how="left")
    out = out.sort_values(["signal_date", "source", "ind_code"]).reset_index(drop=True)
    return out[ALL_COLS]


# ── 前向收益回填 ──────────────────────────────────────────

def returns_panel(path: Optional[PathLike] = None) -> pd.DataFrame:
    """行业日收益面板：index=交易日，columns=行业代码，values=加权涨跌幅(%)"""
    p = Path(path) if path else WEIGHTED_RETURNS_PATH
    wr = pd.read_parquet(p)
    panel = wr.pivot(index="date", columns="ind_code", values="weighted_pct")
    return panel.sort_index()


def _forward_sums(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """
    前向窗口和：位置 t 处的值为 (t+1 .. t+horizon) 的逐日加总。

    rolling(h).sum() 给出截止 t 的向后窗口和，再 shift(-h) 即整体前移为前向窗口；
    min_periods=h 保证窗口内有任一缺失即返回 NaN（宁可标注"未到期"，不臆造收益）。
    """
    return frame.rolling(horizon, min_periods=horizon).sum().shift(-horizon)


def backfill_forward_returns(
    ledger: pd.DataFrame,
    returns_path: Optional[PathLike] = None,
) -> pd.DataFrame:
    """回填 fwd_ret_* / fwd_excess_*；历史不足或数据缺口处保持 NaN"""
    if ledger is None or len(ledger) == 0:
        return _normalize(ledger) if ledger is not None else empty_ledger()

    panel = returns_panel(returns_path)
    benchmark = panel.mean(axis=1)  # 行业等权基准：当日全部行业的横截面均值

    out = _normalize(ledger).copy()
    sig_dates = pd.to_datetime(out["signal_date"])
    codes = out["ind_code"].astype(str)

    date_pos = {d: i for i, d in enumerate(panel.index)}
    col_pos = {c: j for j, c in enumerate(panel.columns)}

    fwd_panel = {h: _forward_sums(panel, h).to_numpy(dtype=float) for h in HORIZONS}
    fwd_bench = {
        h: _forward_sums(benchmark.to_frame("bench"), h)["bench"].to_numpy(dtype=float)
        for h in HORIZONS
    }

    for h in HORIZONS:
        ret_vals = np.full(len(out), np.nan)
        excess_vals = np.full(len(out), np.nan)
        grid = fwd_panel[h]
        bench_grid = fwd_bench[h]
        for k, (d, code) in enumerate(zip(sig_dates, codes)):
            i = date_pos.get(d)
            j = col_pos.get(code)
            if i is None or j is None:
                continue
            v = grid[i, j]
            b = bench_grid[i]
            if np.isnan(v) or np.isnan(b):
                continue
            ret_vals[k] = v
            excess_vals[k] = v - b
        out[f"fwd_ret_{h}"] = ret_vals
        out[f"fwd_excess_{h}"] = excess_vals

    return out[ALL_COLS]


def maturity_table(ledger: pd.DataFrame) -> pd.DataFrame:
    """各持有期的到期情况（未到期 = 收益列为 NaN）"""
    rows = []
    for h in HORIZONS:
        col = f"fwd_ret_{h}"
        n_total = len(ledger)
        n_matured = int(pd.to_numeric(ledger[col], errors="coerce").notna().sum()) if n_total else 0
        rows.append({"horizon": h, "matured": n_matured,
                     "pending": n_total - n_matured, "total": n_total})
    return pd.DataFrame(rows)


def log_signals(
    new_rows,
    path: Optional[PathLike] = None,
    backfill: bool = True,
) -> pd.DataFrame:
    """一步到位：读台账 → 合并新信号 → 回填收益 → 落盘（日频链路的调用入口）"""
    ledger = load_ledger(path)
    merged = upsert(ledger, new_rows)
    if backfill:
        merged = backfill_forward_returns(merged)
    save_ledger(merged, path)
    return merged
