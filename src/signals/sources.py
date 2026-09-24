"""
信号来源适配器

两类来源，入账方式不同：

1. **可历史回填**（`cold_zone`）：冷区宽度反转信号完全由 `industry_daily_full`
   的宽度序列决定，可对全历史重算。回填直接复用生产函数
   `factor_width_reversal.detect_signals`，保证台账口径与日报完全一致——
   否则"台账验证的"与"日报发出的"不是同一个东西，验证就失去意义。

2. **仅向前留痕**（`tracker_pool` / `mainline`）：这两者是有状态引擎，
   状态文件只保存当前值（tracker_state.json / mainline_detection.csv），
   历史无法无损重建。因此由日频链路每日快照入账，样本今后自然累积。
   - tracker_pool 以"入场事件"入账（signal_date = 入场日），重复快照自动去重
   - mainline 以"每日状态"入账（signal_date = 观察日），记录状态演化
"""

from __future__ import annotations

import ast
import json
import logging
from typing import Dict, List, Optional, Sequence

import pandas as pd

from src.paths import FACTOR_DIR, INDUSTRY_DIR
from src.signals.ledger import make_row

logger = logging.getLogger(__name__)

INDUSTRY_LIST_PATH = INDUSTRY_DIR / "industry_list.parquet"
INDUSTRY_DAILY_FULL_PATH = INDUSTRY_DIR / "industry_daily_full.parquet"
INDUSTRY_MEMBERS_PATH = INDUSTRY_DIR / "industry_members.parquet"
CROWD_PATH = FACTOR_DIR / "ind_crowd_turnover_daily.parquet"
TRACKER_REPORT_PATH = INDUSTRY_DIR / "tracker_report.csv"
MAINLINE_PATH = INDUSTRY_DIR / "mainline_detection.csv"

# 各来源的强度口径与方向（评估时据此决定排序/分档方向）
SOURCE_SPECS: Dict[str, Dict] = {
    "cold_zone": {
        "label": "冷区宽度反转",
        "score_hint": "前20日均宽（%），越小越强",
        "direction": "asc",
    },
    "tracker_pool": {
        "label": "贝叶斯反转跟踪池（入场事件）",
        "score_hint": "Sₜ，越大越强",
        "direction": "desc",
    },
    "mainline": {
        "label": "主线方向成员行业（每日状态）",
        "score_hint": "heat_ratio 热度，越大越强",
        "direction": "desc",
    },
}


# ── 基础数据 ──────────────────────────────────────────────

def _load_industry_names() -> Dict[str, str]:
    il = pd.read_parquet(INDUSTRY_LIST_PATH)
    return dict(zip(il["ts_code"].astype(str), il["name"].astype(str)))


def _load_crowding_panel() -> Optional[pd.DataFrame]:
    """
    个股拥挤度 → (date, ind_code) 中位数面板。

    与 module_02.aggregate_industry_crowding 的口径一致（行业内个股中位数）。
    """
    crowd = pd.read_parquet(CROWD_PATH)[["date", "code", "value"]]
    members = pd.read_parquet(INDUSTRY_MEMBERS_PATH)[["stock_code", "ind_code"]]

    code_map = dict(zip(
        members["stock_code"].astype(str).str.upper(),
        members["ind_code"].astype(str),
    ))
    crowd["code"] = crowd["code"].astype(str).str.upper()
    crowd = crowd[crowd["code"].isin(code_map)].copy()
    crowd["ind_code"] = crowd["code"].map(code_map)
    crowd["date"] = pd.to_datetime(crowd["date"])
    return crowd.groupby(["date", "ind_code"])["value"].median().unstack()


def _num(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _parse_group_list(raw) -> List[str]:
    """把 \"['林业Ⅱ']\" 解析为 ['林业Ⅱ']"""
    try:
        names = ast.literal_eval(str(raw))
        if isinstance(names, (list, tuple)):
            return [str(n) for n in names]
    except (ValueError, SyntaxError):
        pass
    return []


# ── 来源一：冷区宽度反转（可历史回填）────────────────────

def _margin_pct_or_none(margin_lookup, date, code):
    """从 (date × ind_code) 分位矩阵取标量；缺失返回 None"""
    if margin_lookup is None or code is None or len(margin_lookup) == 0:
        return None
    if date not in margin_lookup.index or code not in margin_lookup.columns:
        return None
    v = margin_lookup.at[date, code]
    return None if v is None or pd.isna(v) else round(float(v), 4)


def cold_zone_history(
    start: Optional[str] = None,
    end: Optional[str] = None,
    with_crowding: bool = True,
    with_margin: bool = True,
    window_days: int = 60,
    progress_every: int = 250,
) -> List[Dict]:
    """
    对全历史重算冷区宽度反转信号，返回台账记录列表。

    参数:
        start / end  日期区间（YYYY-MM-DD），None 表示不限
        with_crowding 是否附带行业拥挤度特征（首次运行需聚合 850 万行个股数据，较慢）
        with_margin  是否附带"行业两融余额/流通市值"横截面分位（读 signals 缓存，很快）
        window_days  每个信号日传入的窗口长度（需 >= 20，默认 60 足够覆盖"前20日均宽"）
    """
    from src.daily_review.factor_width_reversal import detect_signals

    # 注意：detect_signals 内部按 "con_code" 分组，这里保持原始列名不动，
    # 以保证台账与生产日报走的是同一套口径
    daily = pd.read_parquet(INDUSTRY_DAILY_FULL_PATH)[["date", "con_code", "breadth", "total_amount"]]
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.sort_values(["date", "con_code"]).reset_index(drop=True)

    names = _load_industry_names()

    crowd_panel = None
    if with_crowding:
        try:
            crowd_panel = _load_crowding_panel()
            logger.info("拥挤度面板已加载: %s", crowd_panel.shape)
        except Exception as e:  # 非致命：拥挤度只是特征快照
            logger.warning("拥挤度面板加载失败（跳过该特征）: %s", e)

    # 两融占比分位（行业融资余额/流通市值）—— 实证：2024-09 起 583 条冷区信号中，
    # 低分位组 20 日超额 −1.67pp、高分位组 +1.50pp（docs/sentiment-data-evaluation.md）
    margin_lookup = None
    if with_margin:
        try:
            from src.signals.sentiment import percentile_lookup
            margin_lookup = percentile_lookup()
            logger.info("两融占比分位面板已加载: %s",
                        None if margin_lookup is None else margin_lookup.shape)
        except Exception as e:
            logger.warning("两融占比面板加载失败（跳过该特征）: %s", e)

    # 按日期排序后，各交易日占连续行区间 —— 窗口切片 O(1)
    sizes = daily.groupby("date", sort=True).size()
    row_start: Dict[pd.Timestamp, int] = {}
    row_end: Dict[pd.Timestamp, int] = {}
    pos = 0
    for d, n in sizes.items():
        row_start[d] = pos
        row_end[d] = pos + int(n)
        pos += int(n)

    trade_dates = list(sizes.index)          # 全部交易日（窗口计算必须用全量）
    date_pos = {d: i for i, d in enumerate(trade_dates)}

    selected = trade_dates
    if start is not None:
        selected = [d for d in selected if d >= pd.Timestamp(start)]
    if end is not None:
        selected = [d for d in selected if d <= pd.Timestamp(end)]

    rows: List[Dict] = []
    for k, d in enumerate(selected):
        k_full = date_pos[d]
        lo = row_start[trade_dates[max(0, k_full - window_days)]]
        hi = row_end[d]
        window = daily.iloc[lo:hi]

        today = window[window["date"] == d]
        if today.empty:
            continue
        market_breadth = float(today["breadth"].mean())

        crowding_map = None
        if crowd_panel is not None and d in crowd_panel.index:
            crowding_map = crowd_panel.loc[d].dropna().to_dict()

        signals = detect_signals(
            d.strftime("%Y-%m-%d"),
            window,
            names,
            crowding_map,
            market_breadth=market_breadth,
        )
        for s in signals:
            rows.append(make_row(
                source="cold_zone",
                signal_date=d,
                ind_code=s.get("industry_code"),
                ind_name=s.get("industry_name", ""),
                score=s.get("pre_breadth_20d"),
                tier=s.get("tier"),
                obs_type="event",
                features={
                    "pre_breadth_20d": s.get("pre_breadth_20d"),
                    "expansion_days": s.get("expansion_days"),
                    "d1": s.get("d1"), "d2": s.get("d2"), "d3": s.get("d3"),
                    "d1_ratio": s.get("d1_ratio"),
                    "vol_ratio": s.get("vol_ratio"),
                    "breadth_trajectory": s.get("breadth_trajectory"),
                    "crowding": s.get("crowding"),
                    "margin_pct": _margin_pct_or_none(margin_lookup, d, s.get("industry_code")),
                    "market_breadth": round(market_breadth, 4),
                },
            ))

        if progress_every and (k + 1) % progress_every == 0:
            print(f"  … 冷区回填进度 {k + 1}/{len(selected)} 个交易日，累计 {len(rows)} 条信号", flush=True)

    logger.info("冷区历史回填完成: %d 条信号 / %d 个交易日", len(rows), len(selected))
    return rows


# ── 来源二/三：每日快照（仅向前留痕）──────────────────────

def _snapshot_tracker(obs_date: str) -> List[Dict]:
    if not TRACKER_REPORT_PATH.exists():
        return []
    df = pd.read_csv(TRACKER_REPORT_PATH)
    if "status" not in df.columns:
        return []
    df = df[df["status"] == "tracking"]

    rows: List[Dict] = []
    for _, r in df.iterrows():
        entry = r.get("signal_date")
        entry = str(entry) if pd.notna(entry) else str(obs_date)
        rows.append(make_row(
            source="tracker_pool",
            signal_date=entry,          # 以入场事件入账 → 每日重复快照自动去重
            ind_code=r.get("code"),
            ind_name=str(r.get("name", "")),
            score=_num(r.get("score")),
            tier=str(r.get("phase", "")),
            obs_type="event",
            features={
                "S0": _num(r.get("S0")),
                "days": _num(r.get("days")),
                "entry_confidence": _num(r.get("entry_confidence")),
                "last_date": str(r.get("last_date", "")),
                "observed_on": str(obs_date),
            },
        ))
    return rows


def _snapshot_mainline(obs_date: str) -> List[Dict]:
    if not MAINLINE_PATH.exists():
        return []
    df = pd.read_csv(MAINLINE_PATH)
    if "is_mainline" not in df.columns:
        return []
    df = df[df["is_mainline"] == True]  # noqa: E712

    names = _load_industry_names()
    name_to_code = {v: k for k, v in names.items()}

    rows: List[Dict] = []
    for _, r in df.iterrows():
        member_names = _parse_group_list(r.get("group_names"))
        matched = [(n, name_to_code[n]) for n in member_names if n in name_to_code]
        if not matched:
            logger.warning("主线方向群 %s 成员名未匹配到行业代码: %s",
                           r.get("group_id"), member_names)
            continue
        for name, code in matched:
            rows.append(make_row(
                source="mainline",
                signal_date=obs_date,   # 每日状态快照
                ind_code=code,
                ind_name=name,
                score=_num(r.get("heat_ratio")),
                tier=str(r.get("stage", "")),
                obs_type="state",
                features={
                    "group_id": _num(r.get("group_id")),
                    "group_names": member_names,
                    "n_industries": _num(r.get("n_industries")),
                    "excess_ma20": _num(r.get("excess_ma20")),
                    "winrate": _num(r.get("winrate")),
                    "style": str(r.get("style", "")),
                    "od_ratio": _num(r.get("od_ratio")),
                },
            ))
    return rows


def snapshot_signals(
    trade_date: str,
    sources: Sequence[str] = ("tracker_pool", "mainline"),
) -> List[Dict]:
    """采集当日快照信号（跟踪池入场事件 + 主线成员行业状态）"""
    rows: List[Dict] = []
    if "tracker_pool" in sources:
        rows += _snapshot_tracker(trade_date)
    if "mainline" in sources:
        rows += _snapshot_mainline(trade_date)
    return rows
