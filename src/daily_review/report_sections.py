"""
日报附加章节 — 反转跟踪池 / 主线状态 / 行情 regime

模块二（module_02_industry.py）的日报只覆盖冷区/中区/热区三个板块；
本模块把同一条链路已经算出的另外三块结果渲染成追加章节，
由 cron/industry_review_daily.py 拼接在 module_02 报告之后。

设计约束:
  - 只读现有产出（tracker_report.csv / mainline_detection.csv / daily_regime.parquet），
    不重算、不写业务数据、不改 module_02 口径
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

from src.paths import INDUSTRY_DIR, REPORTS_DIR

logger = logging.getLogger(__name__)

SEP = "━" * 22  # 与 module_02 报告一致的分隔线

TRACKER_REPORT_PATH = INDUSTRY_DIR / "tracker_report.csv"
MAINLINE_PATH = INDUSTRY_DIR / "mainline_detection.csv"
REGIME_PATH = INDUSTRY_DIR / "daily_regime.parquet"

SYNC_SCORE_MIN = 50      # 富途同步候选门槛（与 sync_industries_to_futu.py 一致）
TRACKING_TOP_N = 30      # 跟踪池表格最多展示行数（防极端情况下报告过长）

PHASE_ORDER = {"early": 0, "growing": 1, "mature": 2, "alarming": 3}
PHASE_LABEL = {"early": "早期", "growing": "成长", "mature": "成熟", "alarming": "预警"}


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


# ── 四、反转跟踪池 ────────────────────────────────────────

def format_tracking_section(trade_date: str) -> str:
    lines: List[str] = ["📈 **四、反转跟踪池**（贝叶斯反转引擎）", ""]
    try:
        df = pd.read_csv(TRACKER_REPORT_PATH)
    except Exception as e:  # 文件缺失 / 读取失败 → 降级提示
        logger.warning("跟踪池数据读取失败: %s", e)
        lines += [f"> ⚠️ 跟踪数据读取失败（{TRACKER_REPORT_PATH.name}）：{e}", ""]
        return "\n".join(lines)

    if "status" not in df.columns or df.empty:
        lines += ["> 跟踪数据为空。", ""]
        return "\n".join(lines)

    df = df[df["status"] == "tracking"].copy()
    if df.empty:
        lines += ["今日无跟踪中的行业。", ""]
        return "\n".join(lines)

    df["_rank"] = df["phase"].map(PHASE_ORDER).fillna(9)
    df = df.sort_values(
        ["_rank", "entry_confidence", "score"],
        ascending=[True, False, False],
    )

    dist = df["phase"].map(lambda p: PHASE_LABEL.get(str(p), str(p))).value_counts()
    dist_txt = " / ".join(f"{k} {v}" for k, v in dist.items())
    sync = df[df["score"] >= SYNC_SCORE_MIN]

    lines.append(
        f"> 跟踪中 **{len(df)}** 个行业（{dist_txt}）；"
        f"富途同步候选（Sₜ ≥ {SYNC_SCORE_MIN}）**{len(sync)}** 个"
    )
    lines.append("")
    lines.append("| # | 行业 | 阶段 | 信号日 | S₀ | Sₜ | 置信 | 天数 |")
    lines.append("|:--:|:-----|:----:|:------:|:--:|:--:|:----:|:----:|")

    shown = df.head(TRACKING_TOP_N)
    for i, (_, r) in enumerate(shown.iterrows(), 1):
        phase = PHASE_LABEL.get(str(r.get("phase")), str(r.get("phase", "")))
        lines.append(
            f"| {i} | {r.get('name', '')} | {phase} | {_short_date(r.get('signal_date'))} "
            f"| {_fmt(r.get('S0'), '.0f')} | {_fmt(r.get('score'))} "
            f"| {_fmt(r.get('entry_confidence'), '.2f')} | {_fmt(r.get('days'), '.0f')} |"
        )

    if len(df) > len(shown):
        lines += ["", f"> 共 {len(df)} 个，仅列出前 {len(shown)} 个。"]

    if len(sync):
        lines += ["", "**富途同步候选（Sₜ ≥ 50）：** " + "、".join(
            f"{r.get('name', '')}（Sₜ={_fmt(r.get('score'))}）" for _, r in sync.iterrows()
        )]

    lines.append("")
    return "\n".join(lines)


# ── 五、主线状态 ──────────────────────────────────────────

def format_mainline_section(trade_date: str) -> str:
    lines: List[str] = ["🧭 **五、主线状态**（方向群探测）", ""]
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


# ── 六、行情 regime ───────────────────────────────────────

def format_regime_section(trade_date: str) -> str:
    lines: List[str] = ["🌡️ **六、行情 regime**（结构性 / 轮动 / 全面涨跌）", ""]
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


# ── 七、冷区信号资金面参考 ────────────────────────────────

_TIER_LABEL = {"high": "★★ 高置信", "standard": "★★ 标准", "watch": "★ 观察"}


def _industry_names() -> Dict[str, str]:
    try:
        il = pd.read_parquet(INDUSTRY_DIR / "industry_list.parquet")
        return dict(zip(il["ts_code"].astype(str), il["name"].astype(str)))
    except Exception:
        return {}


def format_margin_section(trade_date: str) -> str:
    """
    冷区信号的两融占比分位（展示列，不参与信号规则）。

    实证依据（docs/sentiment-data-evaluation.md）：2024-09~2026-09 的 583 条冷区信号中，
    低分位组 20 日超额 −1.67pp / 胜率 36.9%，高分位组 +1.50pp / 51.2%，且 2025/2026 两年一致。
    因此仅作展示与观察，暂不进入信号判定。
    """
    lines: List[str] = ["📊 **七、冷区信号资金面参考**（两融余额 / 流通市值分位）", ""]
    try:
        from src.signals.sentiment import percentile_series
        pct = percentile_series(trade_date)
    except Exception as e:
        logger.warning("两融占比数据不可用: %s", e)
        lines += [f"> ⚠️ 两融占比数据不可用：{e}", ""]
        return "\n".join(lines)

    if pct is None or len(pct) == 0:
        lines += [f"> 尚无 {trade_date} 的两融占比数据（面板未覆盖该日）。", ""]
        return "\n".join(lines)

    # 当日冷区信号取自 module_02 结论 JSON（避免重算，保持与报告口径一致）
    signals: List[dict] = []
    try:
        p = REPORTS_DIR / f"{trade_date}_module_02_conclusion.json"
        if p.exists():
            signals = json.loads(p.read_text(encoding="utf-8")).get("cold_zone_signals") or []
    except Exception as e:
        logger.warning("读取冷区信号失败: %s", e)

    lines += [
        "> 分位越高 = 杠杆资金参与越深。实证（583 条冷区信号，20 日超额）：",
        "> 低分位 **−1.67pp / 胜率 36.9%**　中分位 +0.28pp / 47.1%　高分位 **+1.50pp / 51.2%**",
        "> 仅作参考展示，尚未进入信号规则。",
        "",
    ]

    if signals:
        lines += [
            "| 行业 | 级别 | 前20日均宽 | 两融占比分位 | 参考 |",
            "|:-----|:----:|:----------:|:-----------:|:----:|",
        ]
        for s in signals:
            v = pct.get(s.get("industry_code"))
            has = v is not None and not pd.isna(v)
            v_txt = f"{float(v) * 100:.0f}%" if has else "—"
            tag = ("偏高" if float(v) >= 0.67 else "偏低" if float(v) <= 0.33 else "中性") if has else "—"
            tier = _TIER_LABEL.get(str(s.get("tier")), str(s.get("tier", "")))
            lines.append(
                f"| {s.get('industry_name', '')} | {tier} "
                f"| {_fmt(s.get('pre_breadth_20d'), '.0f', '%')} | {v_txt} | {tag} |"
            )
        lines.append("")
    else:
        names = _industry_names()
        top = pct.nlargest(3)
        bot = pct.nsmallest(3)
        lines += [
            f"> 今日无冷区信号。全行业两融占比分位中位数 **{pct.median() * 100:.0f}%**；"
            f"最高：{'、'.join(names.get(c, c) for c in top.index)}；"
            f"最低：{'、'.join(names.get(c, c) for c in bot.index)}",
            "",
        ]
    return "\n".join(lines)


# ── 组装与落盘 ────────────────────────────────────────────

def build_extra_sections(trade_date: str) -> str:
    """渲染四~七四个附加章节（跟踪池 / 主线 / regime / 资金面参考），用分隔线与主报告衔接"""
    parts = [
        format_tracking_section(trade_date),
        format_mainline_section(trade_date),
        format_regime_section(trade_date),
        format_margin_section(trade_date),
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
    """
    path = REPORTS_DIR / f"{trade_date}_industry_review.md"
    if not path.exists():
        logger.warning("报告文件不存在，跳过附加章节: %s", path)
        return None
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + extra)
    return path
