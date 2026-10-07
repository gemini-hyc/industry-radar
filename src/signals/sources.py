"""
信号来源适配器

🔴 2026-10-06：冷区与跟踪池两个来源已弃用（依据
   docs/plan/retirement-breadth-tracker-plan.md）：
     - `cold_zone`：宽度→未来收益无预测力，2×2 条件检验交互项 ≈ −0.01pp（无交互）
     - `tracker_pool`：装置零产出（生产门槛 Sₜ≥50 下131 个行业命中 0 个），
       且引擎自身 entry 事件 h=20 按日聚类 t=−2.87
   二者的**历史行保留**在台账里（2,234 + 21 条），状态标 `retired`，只为可追溯。

**唯一在册来源**：`mainline`（每日状态快照）—— 主线方向群每日状态，
   状态文件只保存当前值、历史无法无损重建，故由日频链路每日快照入账。
   🔴 2026-10-07：`mainline` 由 `pending` 改 **`observational`** —— §2.2 判定其
   全市场相关聚类 = beta 聚合产物（**测错对象**），保留入账**仅为留痕与日报连续性**；
   ⛔ `status_note` 硬约束：**其台账评估结果不得用于支撑或否定任何链假设**。

原「可历史回填」类别的冷区实现（`cold_zone_history` 及其对
`factor_width_reversal.detect_signals` 的复用）已移除 —— 它曾保证
"台账验证的"与"日报发出的"同口径，该价值随信号退场而消失。
"""

from __future__ import annotations

import ast
import logging
from typing import Dict, List, Optional, Sequence

import pandas as pd

from src.paths import INDUSTRY_DIR
from src.signals.ledger import make_row

logger = logging.getLogger(__name__)

INDUSTRY_LIST_PATH = INDUSTRY_DIR / "industry_list.parquet"
MAINLINE_PATH = INDUSTRY_DIR / "mainline_detection.csv"

# 信号定位（status）：决定一个来源在评估报告里的"身份"
#   active        已被证据支持，可作为交易/决策依据
#   observational 观察项 —— 保留入账与展示以积累样本，但**不作为交易依据，也不得据此调参**
#   pending       样本不足以评估（向前积累中），结论未定
#   retired       已弃用 —— **该来源已被证伪或装置已拆除**，历史行保留只为可追溯，
#                 评估报告中单列「已弃用」分组。⛔ 不再新增入账。
#                 🔴 2026-10-06 新增。**不可用 pending 代替**：pending 暗示"再等等就有结论"，
#                    而 retired 的含义是"结论已下：无效"。两者混用会让弃用被误读成待观察。
STATUS_LABEL: Dict[str, str] = {
    "active": "✅ 生效",
    "observational": "⛔ 观察项",
    "pending": "⏳ 待观察",
    "retired": "🚫 已弃用",
}
STATUS_ORDER: Dict[str, int] = {
    "active": 0, "pending": 1, "observational": 2, "retired": 3,
}
ALL_STATUSES = tuple(STATUS_ORDER)

# 降级时点改为**按来源**记录在 SOURCE_SPECS[*]["status_since"]**（2026-10-05）。
# 原单一常量 COLD_ZONE_DOWNGRADED_ON 已删除：它把"只有一个来源会降级"写死进了代码，
# 当 tracker_pool 也降级时就会漏报日期（评估报告的观察项横幅原样硬编码 2026-10-01）。

# 各来源的强度口径与方向（评估时据此决定排序/分档方向）
SOURCE_SPECS: Dict[str, Dict] = {
    "cold_zone": {
        "label": "冷区宽度反转",
        "score_hint": "前20日均宽（%），越小越强",
        "direction": "asc",
        # 弃用（2026-10-06）：宽度作为预测因子已证伪，冷区信号随之退场。
        #   依据 docs/breadth-what-it-can-and-cannot-do.md + docs/plan/retirement-breadth-tracker-plan.md
        # 🔴 保留条目而非删除：evaluate.py 按 source 查本表渲染报告，删条目会让2,234 行历史
        #    被渲染成「⏳ 待观察」+ 空标签 —— 而"待观察"暗示"再等等就有结论"，
        #    恰是本次清理要终结的误解。
        "status": "retired",
        "status_since": "2026-10-06",          # 弃用生效日（评估报告按此渲染）
        "status_doc": "docs/plan/retirement-breadth-tracker-plan.md",
        "status_short": "宽度无预测力，信号已退场；**保留历史仅为可追溯**",
        "status_note": (
            "宽度→未来 20日收益IC ≈ −0.004（非重叠 t= −0.74）；唯一稳健的是宽度→未来波动率"
            "（IC +0.147 / 非重叠 t= +7.47），含义是成分股同涨同跌、**分散化失效**，"
            "不是可交易的 alpha。以冷区为前提的 2×2 条件检验交互项 ≈ −0.01pp（无交互）。"
            "⛔ 2026-10-06 弃用：宽度退回纯数据探针（防全零静默事故），不再作信号。"
        ),
    },
    "tracker_pool": {
        "label": "贝叶斯反转跟踪池（入场事件）",
        "score_hint": "Sₜ，越大越强",
        "direction": "desc",
        # 弃用（2026-10-06）：装置已拆除。依据 docs/tracker-design-and-issues.md §8.2
        #   —— 入场事件 h=20 均值 −0.219pp / 中位 −0.583pp / 按日聚类 t=−2.87，IS/OOS 同号。
        #   且实测生产门槛 min_score=50 下**池子恒为 0 个**（121/131 已 eliminated）。
        "status": "retired",
        "status_since": "2026-10-06",
        "status_doc": "docs/tracker-design-and-issues.md",
        "status_short": "入场事件前瞻超额**显著为负**，且生产门槛下池子恒为空；**装置已拆除**",
        "status_note": (
            "引擎自身 entry 事件（= 本来源入账事件）3,917 个：h=20 均值 −0.219pp / 中位 −0.583pp / "
            "为正 46%，按日聚类 t=−2.87、按月 −2.54，IS −0.79 与 OOS −3.39 **同号**。"
            "⇒ 核心前提「深睡+异动=反转起点」被**反驳**（方向相反），而非样本不足。"
            "另：生产门槛 Sₜ ≥ 50 下 131 个行业**命中 0 个**（121 已 eliminated / 7 triggered / 3 tracking），"
            "六阶段生命周期中 mature/alarming/decaying 三个阶段从未真实出现过。"
            "⛔ 2026-10-06 弃用：装置零产出，脚本与产物一并清理。历史 21 条仅留痕。"
        ),
    },
    "mainline": {
        "label": "主线方向成员行业（每日状态）",
        "score_hint": "heat_ratio 热度，越大越强",
        "direction": "desc",
        # 🔴 2026-10-07 由 pending 改 observational（review §12.3，主人已批准）。
        #    原因：方法论 §2.2 判定其全市场相关聚类 = beta 聚合产物，即「测错对象」。
        #    ⚠️ 五个字段必须同时改 —— 只改 status 会让定位表仍显示"样本不足"，语义是错的。
        #    （pending 的语义是"样本够了就有结论"，而这里的结论已下：方法本身错了。）
        "status": "observational",
        "status_since": "2026-10-06",
        "status_doc": "docs/plan/chain-clustering-review.md",
        "status_short": "方法已被判定「测错对象」，待重建；⛔ 不得用于链假设",
        "status_note": (
            "方法论 §2.2 判定其全市场相关聚类 = beta 聚合产物（58 行业巨群 / 群随数据漂移），"
            "即「测错对象」；改造方向 = 链内残差 + 最小成员数 + 共振强度连续量（未执行，须立项）。"
            "⛔ 其台账评估结果不得用于支撑或否定任何链假设 —— 因为它测的对象本身是错的"
            "（前车之鉴：wake_backtest_results.csv 的 status=tracking 两行曾被当作证据）。"
            "保留入账仅为留痕与日报连续性；不得通过调参「修复」。"
        ),
    },
}


# ── 基础数据 ──────────────────────────────────────────────

def _load_industry_names() -> Dict[str, str]:
    il = pd.read_parquet(INDUSTRY_LIST_PATH)
    return dict(zip(il["ts_code"].astype(str), il["name"].astype(str)))


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


# 🔴 2026-10-06 已移除：冷区宽度反转来源（_margin_pct_or_none / cold_zone_history，
#    含其对 factor_width_reversal 的惰性 import）与仅服务它的 _load_crowding_panel。
#    保留 _load_industry_names / _num / _parse_group_list —— 它们被 _snapshot_mainline 使用。
#    依据 docs/plan/retirement-breadth-tracker-plan.md §3.1（必补三）。

# ── 来源二/三：每日快照（仅向前留痕）──────────────────────

# 🔴 `_snapshot_tracker` 已于 2026-10-06 删除（tracker_pool 弃用）。
#    依赖的 TRACKER_REPORT_PATH 常量与 industry_tracker.py 一并清理（计划 §3.1/§3.2）。

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
    sources: Sequence[str] = ("mainline",),
) -> List[Dict]:
    """采集当日快照信号（主线成员行业状态）。

    🔴 2026-10-06：`tracker_pool` 已弃用，其快照分支与 `_snapshot_tracker` 一并移除。
    """
    rows: List[Dict] = []
    if "mainline" in sources:
        rows += _snapshot_mainline(trade_date)
    return rows
