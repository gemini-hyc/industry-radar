"""
冷区宽度反转信号 — 检测行业在极度悲观后的宽度连续扩张信号。

宽度定义：行业内收盘价站上MA20的个股占比 (close > MA20).mean()。
此定义与 compute_concept_daily() 和 industry_daily_full.parquet 一致。

触发条件：
  - 前20日均宽 ≤ 冷区阈值（默认30%，可随全市场宽度动态调整）
  - 宽度连续扩张 = 3天（只取第3天，同一扩张不重复触发）

信号分级（仅基于前20日均宽深度）：
  前20日均宽 <10%  → 高置信（high）
  前20日均宽 <20%  → 标准（standard）
  前20日均宽 <阈值  → 观察（watch）

市场阶段动态调制（v2）：
  全市场宽度 <35% → 冷区阈值放宽至35%（熊市底部，信号稀缺）
  全市场宽度 55-70% → 冷区阈值收紧至25%（牛市中期，严控质量）
  全市场宽度 >70% → 冷区阈值收紧至20%（牛市高潮，只取极冷反转）

v3 变更（2026-07-29）：
  砍掉 D1占比/三日量比/涨停情绪 三个评分维度。
  回测证实这三个维度在 MA20 宽度下 IC 均不显著，
  信号分级改为仅依据前20日均宽深度，简单透明。

纯计算函数，不涉及文件I/O、不涉及飞书推送。
"""

import logging
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# A/B 复核开关的合法取值（详见 detect_signals docstring）。
# 默认值 = 当前生产口径；另一档是旧设计文档口径，仅供对比，不得作为默认。
_EXPANSION_RULES = frozenset({"exactly3", "ge3"})
_THRESHOLD_MODES = frozenset({"dynamic", "fixed30"})


def detect_signals(
    trade_date: str,
    daily_data: pd.DataFrame,
    names_map: Optional[Dict[str, str]] = None,
    crowding_map: Optional[Dict[str, float]] = None,
    market_breadth: Optional[float] = None,
    min_history_days: int = 15,
    expansion_rule: str = "exactly3",
    cold_threshold_mode: str = "dynamic",
) -> List[Dict]:
    """
    检测冷区宽度反转信号。

    参数:
        trade_date: 目标交易日期 YYYY-MM-DD
        daily_data: 行业日频数据，至少包含列 [date, con_code, breadth, total_amount]
        names_map: 行业代码→名称映射 {ind_code: ind_name}，可选
        crowding_map: 行业代码→拥挤度映射 {ind_code: crowding_median}，可选
        market_breadth: 当日全市场宽度均值，用于动态调整冷区阈值，可选
        min_history_days: 前20日窗口最少需要的数据天数，默认15
        expansion_rule: 扩张触发规则。**默认值即生产口径**，另一档仅供 A/B 复核：
            - "exactly3"（默认）：恰好扩张第 3 天触发，避免同一扩张事件重复出信号
            - "ge3"：扩张 ≥3 天即触发（旧设计文档 `docs/module_02_rebuild.md` §3.1 口径）
        cold_threshold_mode: 冷区阈值模式。**默认值即生产口径**，另一档仅供 A/B 复核：
            - "dynamic"（默认）：随全市场宽度调制（<0.35→0.35 / <0.55→0.30 / <0.70→0.25 / ≥0.70→0.20）
            - "fixed30"：固定 30%（旧设计文档 `docs/module_02_rebuild.md` §3.1 口径）

    返回:
        List[SignalDict]，按分级 → 前宽深度排序。每个 SignalDict 包含：
            industry_code, industry_name, tier,
            pre_breadth_20d, expansion_days, breadth_trajectory,
            d1, d2, d3, d1_ratio, vol_ratio, crowding
    """
    if expansion_rule not in _EXPANSION_RULES:
        raise ValueError(f"未知 expansion_rule={expansion_rule!r}，可选 {sorted(_EXPANSION_RULES)}")
    if cold_threshold_mode not in _THRESHOLD_MODES:
        raise ValueError(f"未知 cold_threshold_mode={cold_threshold_mode!r}，可选 {sorted(_THRESHOLD_MODES)}")

    if daily_data.empty:
        return []

    trade_dt = pd.Timestamp(trade_date)

    # 确保数据已排序，避免 groupby 内重复排序
    daily = daily_data.sort_values(["con_code", "date"]).reset_index(drop=True)

    signals: List[Dict] = []

    for con_code, group in daily.groupby("con_code", sort=False):
        # 按日期排序
        group = group.sort_values("date").reset_index(drop=True)

        # 定位目标日期
        today_mask = group["date"] == trade_dt
        if not today_mask.any():
            continue
        today_idx = int(today_mask.idxmax())  # type: ignore[arg-type]

        # 需要至少 20 个交易日的历史
        if today_idx < 20:
            continue

        # ── 检测以今日为终点的连续扩张天数 ──
        expansion_length = 0
        idx = today_idx
        while idx > 0:
            if float(group.loc[idx, "breadth"]) > float(group.loc[idx - 1, "breadth"]):
                expansion_length += 1
                idx -= 1
            else:
                break

        # 触发规则：exactly3 = 只在扩张第 3 天触发一次（生产口径，避免同一扩张事件重复外发）
        #           ge3      = 扩张 ≥3 天即触发（旧文档口径，A/B 复核用）
        if expansion_rule == "ge3":
            if expansion_length < 3:
                continue
        elif expansion_length != 3:
            continue

        # 扩张起点索引
        exp_start_idx = today_idx - expansion_length + 1

        # ── ① 前 20 日均宽（扩张起点前 20 个交易日） ──
        pre_start_idx = exp_start_idx - 20
        pre_breadth_series = group.loc[pre_start_idx : exp_start_idx - 1, "breadth"]
        if len(pre_breadth_series) < min_history_days:
            logger.debug(
                "行业 %s 于 %s 前20日数据不足（%d/%d），跳过",
                con_code, trade_date, len(pre_breadth_series), min_history_days,
            )
            continue
        pre_breadth = float(pre_breadth_series.mean())

        # ── 冷区阈值 ──
        # dynamic（生产口径）：随全市场宽度动态调制
        # fixed30（旧文档口径，A/B 复核用）：固定 30%
        if cold_threshold_mode == "fixed30":
            cold_threshold = 0.30
        elif market_breadth is not None and not pd.isna(market_breadth):
            if market_breadth < 0.35:
                cold_threshold = 0.35   # 熊市冰点：放宽准入
            elif market_breadth < 0.55:
                cold_threshold = 0.30   # 正常市场
            elif market_breadth < 0.70:
                cold_threshold = 0.25   # 牛市中期：收紧
            else:
                cold_threshold = 0.20   # 牛市高潮：严格
        else:
            cold_threshold = 0.30

        if pre_breadth > cold_threshold:
            continue

        # ── 信号分级（仅基于前宽深度）──
        if pre_breadth < 0.10:
            tier = "high"
        elif pre_breadth < 0.20:
            tier = "standard"
        else:
            tier = "watch"

        # ── 附属信息（仅供参考，不参与评分）──

        # D1/D2/D3 及 D1 占比
        d1 = float(group.loc[exp_start_idx, "breadth"]) - float(group.loc[exp_start_idx - 1, "breadth"])
        d2 = float(group.loc[exp_start_idx + 1, "breadth"]) - float(group.loc[exp_start_idx, "breadth"])
        d3 = float(group.loc[exp_start_idx + 2, "breadth"]) - float(group.loc[exp_start_idx + 1, "breadth"])
        d_sum = d1 + d2 + d3
        d1_ratio = d1 / d_sum if d_sum > 0 else 0.0

        # 三日量比
        exp_amt = float(group.loc[exp_start_idx : exp_start_idx + 2, "total_amount"].mean())
        pre_amt = float(group.loc[pre_start_idx : exp_start_idx - 1, "total_amount"].mean())
        vol_ratio = exp_amt / pre_amt if pre_amt > 0 else 1.0

        # ── 宽度轨迹（扩张前一日 → 今日） ──
        start_b = float(group.loc[exp_start_idx - 1, "breadth"])
        end_b = float(group.loc[today_idx, "breadth"])

        # ── 行业名称 ──
        ind_code = str(con_code)
        ind_name = names_map.get(ind_code, ind_code) if names_map else ind_code

        # ── 拥挤度 ──
        crowding_val = crowding_map.get(ind_code) if crowding_map else None

        signals.append({
            "industry_code": ind_code,
            "industry_name": ind_name,
            "tier": tier,
            "pre_breadth_20d": round(pre_breadth * 100, 1),
            "expansion_days": expansion_length,
            "breadth_trajectory": f"{start_b * 100:.0f}%→{end_b * 100:.0f}%",
            "d1": round(d1, 4),
            "d2": round(d2, 4),
            "d3": round(d3, 4),
            "d1_ratio": round(d1_ratio, 2),
            "vol_ratio": round(vol_ratio, 2),
            "crowding": round(float(crowding_val), 2) if crowding_val is not None else None,
        })

    # 按分级 → 前宽深度排序
    _tier_rank = {"high": 0, "standard": 1, "watch": 2}
    signals.sort(key=lambda s: (_tier_rank[s["tier"]], s["pre_breadth_20d"]))
    return signals
