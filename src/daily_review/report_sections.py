"""
日报落盘工具 —— 从端写保护的唯一入口

🔴 2026-10-15 重构：旧「附加章节」三章（主线状态 / 行情 regime / 链共振）**整体下线**。
   主人拍板：日报**重排**为方向 B 五章结构，旧三章**不保留**。

下线理由（不是算错，是**语义错位**）：
  1. **主线状态**是二元开关（有/无主线），全历史仅 32.6% 的日子有主线 ⇒ 大多数日子空章；
     且已判定「测错对象」——剥 beta 后行业间相关 0.410 → −0.006，聚的是 beta 影子。
     ⇒ 其信息被**并入注意力梯队 T1**（热度/共振强 = 持续被关注），不再单独成章。
  2. **行情 regime** 只复述涨跌家数，是行情复读机，不指向任何具体行业。
     ⇒ 并入「综合判读」的市场宽度一句。
  3. **链共振**按**链**出表，但 37 条链只覆盖 69/131 个行业 ⇒ 62 个行业在该章里没有位置。
     ⇒ 共振降级为**梯队与异动的输入**，随行业出表（见 attention_radar）。

新日报正文全部由 `src/daily_review/attention_radar.py::build_report_markdown()` 产出，
本模块只剩**落盘**这一件事：把正文安全地写到当日报告文件。

⚠️ 为什么落盘必须走这里：
   本机（从端）的数据根是源端单向同步下来的副本，直接写既会被下次 pull 覆盖、
   又可能污染下游。`resolve_output_path()` 让「写不进去」成为物理事实，
   而不是靠人记得挂环境变量 —— 旧 report 落盘路径曾绕过它，已在此统一。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from src.paths import REPORTS_DIR, resolve_output_path

logger = logging.getLogger(__name__)

SEP = "─" * 22  # 与历史报告一致的分隔线


def build_extra_sections(trade_date: str) -> str:
    """兼容旧调用签名：**恒返回空串**。

    旧三章（主线/regime/链共振）已于 2026-10-15 下线，日报正文改由
    `attention_radar.build_report_markdown()` 一次性产出完整五章。
    保留此函数是为了让尚未更新的调用方（含 cron 的历史分支）不炸，
    而不是为了再追加任何内容。
    """
    return ""


def append_to_report(trade_date: str, extra: str) -> Optional[Path]:
    """
    把文本追加到当日报告末尾。

    报告文件不存在（如上游未产出）时不创建、返回 None，
    以免掩盖主报告生成失败的事实。

    写入路径经 src.paths.resolve_output_path —— 从端写保护（拒绝 / 重定向到沙箱）。
    """
    if not extra:
        return None
    path = Path(resolve_output_path(REPORTS_DIR / f"{trade_date}_industry_review.md"))
    if not path.exists():
        logger.warning("报告文件不存在，跳过追加: %s", path)
        return None
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + extra)
    return path


def resolve_report_path(trade_date: str) -> Path:
    """当日报告的实际写入路径（经从端写保护解析）。"""
    return Path(resolve_output_path(REPORTS_DIR / f"{trade_date}_industry_review.md"))
