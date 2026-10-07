"""
日报附加章节 — 主线状态 / 行情 regime

🔴 2026-10-06：反转跟踪池、冷区资金面参考、宽度状态三节已移除
  （对应来源 `tracker_pool` / `cold_zone` 已弃用，依据
   docs/plan/retirement-breadth-tracker-plan.md）。
  模块二（module_02_industry.py）的三区框架（冷区/中区/热区）同期退场，
  现在只报「当日行业数据是否齐备」，不再输出信号章节。

设计约束:
  - 只读现有产出（mainline_detection.csv / daily_regime.parquet），
    不重算、不写业务数据
  - 任一数据源缺失或异常时降级为一行提示，绝不影响主报告生成
  - 章节风格与 module_02 保持一致（emoji 标题 + 分隔线 + 引用说明 + Markdown 表格）
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd

from src.paths import INDUSTRY_DIR, REPORTS_DIR, resolve_output_path

logger = logging.getLogger(__name__)

SEP = "━" * 22  # 与 module_02 报告一致的分隔线

MAINLINE_PATH = INDUSTRY_DIR / "mainline_detection.csv"
REGIME_PATH = INDUSTRY_DIR / "daily_regime.parquet"


# ── 小工具 ────────────────────────────────────────────────

def _fmt(v, spec: str = ".1f", suffix: str = "", scale: float = 1.0) -> str:
    """安全数值格式化：NaN/异常值返回 '—'"""
    try:
        f = float(v)
        if pd.isna(f):
            return "—"
        return format(f * scale, spec) + suffix
    except (TypeError, ValueError):
        return "—"


def _parse_group_names(raw) -> str:
    """把 \"['林业Ⅱ']\" 解析为 \"林业Ⅱ\"；解析失败时原样返回"""
    try:
        names = ast.literal_eval(str(raw))
        if isinstance(names, (list, tuple)) and names:
            return "、".join(str(n) for n in names)
    except (ValueError, SyntaxError):
        pass
    return str(raw)


def _short_date(v) -> str:
    s = str(v)
    return s[5:] if len(s) >= 10 else (s or "—")


# 🔴 2026-10-06 已移除：四、反转跟踪池（见 docs/plan/retirement-breadth-tracker-plan.md §2.3）

# ── 一、主线状态 ──────────────────────────────────────────

def format_mainline_section(trade_date: str) -> str:
    lines: List[str] = ["🧭 **一、主线状态**（方向群探测）", ""]
    try:
        df = pd.read_csv(MAINLINE_PATH)
    except Exception as e:
        logger.warning("主线数据读取失败: %s", e)
        lines += [f"> ⚠️ 主线数据读取失败（{MAINLINE_PATH.name}）：{e}", ""]
        return "\n".join(lines)

    if df.empty:
        lines += ["今日无方向群数据。", ""]
        return "\n".join(lines)

    mainlines = df[df["is_mainline"] == True]  # noqa: E712 — 列可能含 NaN
    if mainlines.empty:
        lines += ["> 今日无主线方向（判定：热度 > 1.3x 且 60 日超额 > 0）。", ""]
    else:
        lines.append(f"> 检测到 **{len(mainlines)}** 个主线方向（判定：热度 > 1.3x 且 60 日超额 > 0）")
        lines.append("")
        lines.append("| 方向群 | 代表行业 | 热度 | 超额(20d) | 胜率 | 阶段 | 风格 |")
        lines.append("|:------:|:---------|:----:|:---------:|:----:|:----:|:----:|")
        for _, r in mainlines.sort_values("heat_ratio", ascending=False).iterrows():
            lines.append(
                f"| 群{r.get('group_id')} | {_parse_group_names(r.get('group_names'))} "
                f"| {_fmt(r.get('heat_ratio'), '.2f', 'x')} "
                f"| {_fmt(r.get('excess_ma20'), '+.2f', '%')} "
                f"| {_fmt(r.get('winrate'), '.0f', '%', scale=100)} "
                f"| {r.get('stage', '')} | {r.get('style', '')} |"
            )
        lines.append("")

    top = df.nlargest(min(5, len(df)), "heat_ratio")
    lines += ["**热度 Top 5：**", "",
              "| 方向群 | 代表行业 | 热度 | 超额(20d) | 阶段 | 主线 |",
              "|:------:|:---------|:----:|:---------:|:----:|:----:|"]
    for _, r in top.iterrows():
        flag = "✅" if bool(r.get("is_mainline")) else "—"
        lines.append(
            f"| 群{r.get('group_id')} | {_parse_group_names(r.get('group_names'))} "
            f"| {_fmt(r.get('heat_ratio'), '.2f', 'x')} "
            f"| {_fmt(r.get('excess_ma20'), '+.2f', '%')} "
            f"| {r.get('stage', '')} | {flag} |"
        )
    lines.append("")
    return "\n".join(lines)


# ── 二、行情 regime ───────────────────────────────────────

def format_regime_section(trade_date: str) -> str:
    lines: List[str] = ["🌡️ **二、行情 regime**（结构性 / 轮动 / 全面涨跌）", ""]
    try:
        df = pd.read_parquet(REGIME_PATH)
    except Exception as e:
        logger.warning("regime 数据读取失败: %s", e)
        lines += [f"> ⚠️ regime 数据读取失败（{REGIME_PATH.name}）：{e}", ""]
        return "\n".join(lines)

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    target = pd.Timestamp(trade_date)
    rows = df[df["date"] == target]
    if rows.empty:
        latest = df["date"].max().date() if len(df) else "—"
        lines += [f"> 尚无 {trade_date} 的 regime 结果（数据最新至 {latest}）。", ""]
        return "\n".join(lines)

    r = rows.iloc[-1]
    lines.append("| 交易日 | 类型 | 上涨行业占比 | 行业均涨幅 | 分化度 | Top5集中度 |")
    lines.append("|:------:|:----:|:-----------:|:----------:|:------:|:----------:|")
    lines.append(
        f"| **{trade_date}** | **{r.get('regime', '—')}** "
        f"| {_fmt(r.get('up_pct'), '.1f', '%')} "
        f"| {_fmt(r.get('mean_ret'), '+.2f', '%')} "
        f"| {_fmt(r.get('std_ret'), '.2f')} "
        f"| {_fmt(r.get('top5_conc'), '.2f')} |"
    )
    lines.append("")

    recent = df[df["date"] <= target].sort_values("date").tail(5)
    seq = " ｜ ".join(
        f"{d.strftime('%m-%d')} {g}" for d, g in zip(recent["date"], recent["regime"])
    )
    lines += [f"**近 5 个交易日：** {seq}", ""]
    return "\n".join(lines)


# 🔴 2026-10-06 已移除：七、冷区信号资金面参考（见 docs/plan/retirement-breadth-tracker-plan.md §2.3）

# 🔴 2026-10-06 已移除：八、宽度状态（随宽度预测用途弃用而退场；
#    宽度仍作为**数据探针**保留在 market_data.py / check_data_completeness.py，
#    依据 docs/plan/retirement-breadth-tracker-plan.md §2.3/§6）

# ── 二点五、链共振（测量层 · 2026-10-07 新增）──────────────────

CHAIN_RESONANCE_PATH = INDUSTRY_DIR / "chain_resonance_daily.csv"
STYLE_PROBE_PATH = INDUSTRY_DIR / "style_probe_status.json"


def format_chain_resonance_section(trade_date: str, top_n: int = 8) -> str:
    """链共振强度（**状态描述 = 原始值**）+ 风格解释份额 + 退化标注。

    三条纪律（review §12.2 / §12.5）：
      ① 日报用**原始值**（§3.3：日频层 = 状态描述）；调整值留给研究/归因；
      ② 「**原始 − 调整**」差额（风格解释份额）**必须可见**，不在测量层静默抹平；
      ③ 因子面板退化时**要在报告正文说出来**（只写日志不算）—— 这就是 `banner()` 的作用。
    """
    lines: List[str] = ["🔗 **三、链共振强度**（语义先验链 × 残差共振）", ""]
    try:
        df = pd.read_csv(CHAIN_RESONANCE_PATH)
    except Exception as e:
        logger.warning("链共振表读取失败: %s", e)
        lines += [f"> ⚠️ 链共振表读取失败（{CHAIN_RESONANCE_PATH.name}）：{e} —"
                  f"本章节依赖 scripts/measure_chain_resonance.py，未跑即无此表。", ""]
        return "\n".join(lines)

    # ① 退化标注先行（失败要"响"）
    try:
        st = json.loads(Path(STYLE_PROBE_PATH).read_text(encoding="utf-8"))
    except Exception:
        st = None
    if st is not None and not st.get("ok"):
        codes = "、".join(i.get("code", "?") for i in (st.get("issues") or []))
        lines += [
            f"> ⚠️ **本节的共振强度为未调整值（因子面板不可用：{codes}）**",
            "> ⇒ 该数值包含风格暴露成分，**不得用于归因 / 链假设**；仅作「今日发生了什么」的状态描述。",
            "",
        ]

    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    day = df[df["date"] <= trade_date]
    if day.empty:
        lines += [f"> 未找到 ≤{trade_date} 的测量行。", ""]
        return "\n".join(lines)
    last = day["date"].max()
    d = day[day["date"] == last].copy()

    share = d["style_share"] if "style_share" in d.columns else None
    if share is not None and len(share):
        lines += [
            f"**测量日 {last}** ｜ 链 {len(d)} 条 ｜ 风格解释份额（原始−调整）："
            f"均值 **{share.mean():+.3f}** / 中位 **{share.median():+.3f}** / 最大 **{share.max():+.3f}**",
            "",
        ]
    lines += [
        "| 链 | 成员 | 原始共振 | 调整后 | 风格份额 | 分层 z |",
        "|:---|---:|---:|---:|---:|---:|",
    ]
    cols = [c for c in ("resonance_raw", "resonance_adj", "style_share", "z_adj")
            if c in d.columns]
    for _, r in d.sort_values("resonance_raw", ascending=False).head(top_n).iterrows():
        row = [f"| {str(r.get('chain_id', '—'))[:22]} | {_fmt(r.get('n_members'), '.0f')} |"]
        for c in ("resonance_raw", "resonance_adj", "style_share", "z_adj"):
            row.append(f" {_fmt(r.get(c) if c in d.columns else None, '+.3f')} |"
                       if c in cols else " — |")
        lines.append("".join(row))
    lines += ["", "> 原始共振 = 只剥市场 beta；调整后 = 再剥 5 个风格因子。**跨链比较请看 z 列**"
                  "（群内均值随群规模稀释 ⇒ 标量只在**同一条链**内跨时间可比）。", ""]
    return "\n".join(lines)


# ── 组装与落盘 ────────────────────────────────────────────

def build_extra_sections(trade_date: str) -> str:
    """渲染附加章节（主线状态 / 行情 regime / 链共振），用分隔线与主报告衔接。

    🔴 2026-10-06：反转跟踪池、冷区资金面参考、宽度状态三节已随对应来源弃用而移除。
    🆕 2026-10-07：新增链共振（测量层）；缺表时降级为一行提示，不影响主报告。
    """
    parts = [
        format_mainline_section(trade_date),
        format_regime_section(trade_date),
        format_chain_resonance_section(trade_date),
    ]
    blocks: List[str] = []
    for part in parts:
        blocks += [SEP, "", part.rstrip(), ""]
    return "\n".join(blocks).rstrip() + "\n"


def append_to_report(trade_date: str, extra: str) -> Optional[Path]:
    """
    把附加章节追加到当日报告末尾。

    报告文件不存在（如 module_02 未产出）时不创建、返回 None，
    以免掩盖主报告生成失败的事实。

    写入路径经 src.paths.resolve_output_path —— 从端写保护（拒绝 / 重定向到沙箱）。
    不加这一步，在只读副本上会绕过保护、直接追加进真实数据根的报告里。
    """
    path = Path(resolve_output_path(REPORTS_DIR / f"{trade_date}_industry_review.md"))
    if not path.exists():
        logger.warning("报告文件不存在，跳过附加章节: %s", path)
        return None
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + extra)
    return path
