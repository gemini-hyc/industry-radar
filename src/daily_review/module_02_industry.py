"""
每日复盘 · 行业雷达日报**编排与落盘**

🔴 2026-10-15 重排（主人拍板：日报重排为方向 B 五章，旧三章不保留）
==================================================================
本模块的职责收敛为三件事：
  1. **前置检查**（check_prerequisites）—— 供 cron 判断数据是否就绪、缺什么；
  2. **编排**：调用 `attention_radar.build_report_markdown()` 生成方向 B 日报正文；
  3. **落盘**：经 `resolve_output_path` 从端写保护 + provenance 指纹写入当日报告。

不再自己渲染任何章节。历史沿革（保留以免重蹈）：
  - 2026-10-06 三区框架（冷区/中区/热区）整体退场：冷区宽度反转无预测力
    （IC≈−0.004，非重叠 t=−0.74），中区/热区从未实现。
  - 2026-10-15 旧的「当日行业数据状态」占位章删除 —— 它只是三区退场后的空壳，
    对「今天看哪个行业」毫无信息；由方向 B 的「今日一句话」取代。

数据源（方向 B 实际读取，见 attention_radar）：
  - 行业加权涨跌幅 → industry/industry_weighted_returns.parquet
  - 行业等权日频聚合 → industry/industry_daily_full.parquet（成交额/avg_pct）
  - 剥风格残差面板 → industry/industry_residual_panel.parquet（自算链共振历史）
  - 行业走势净值 → industry/industry_nav_924.parquet（相对强度 rs_w）
  - 链定义 → industry/chain_mainline_detection.csv（37 条已验证链）
  - 事件 → reports/forecast/、reports/broker_recommend/、policy/sector_policy_map.json
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

# 项目路径（数据根统一走 src/paths.py：env INDUSTRY_RADAR_DATA > config.yaml > 默认共享数据目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))  # 兼容脚本直跑: python3 src/daily_review/module_02_industry.py
from src.paths import DATA_DIR, INDUSTRY_DIR
from src.utils.provenance import save_text

# 数据路径常量
WEIGHTED_RETURNS_PATH = DATA_DIR / "industry" / "industry_weighted_returns.parquet"
INDUSTRY_MEMBERS_PATH = DATA_DIR / "industry" / "industry_members.parquet"
INDUSTRY_LIST_PATH = DATA_DIR / "industry" / "industry_list.parquet"
MARKET_DAILY_DIR = DATA_DIR / "market" / "daily"
CROWDING_PATH = DATA_DIR / "factors" / "ind_crowd_turnover_daily.parquet"
REPORTS_DIR = DATA_DIR / "reports" / "daily-analysis"


# ═══════════════════════════════════════════════
# 公共工具函数
# ═══════════════════════════════════════════════

def load_industry_names() -> Dict[str, str]:
    """加载行业代码→名称映射"""
    df = pd.read_parquet(INDUSTRY_LIST_PATH)
    return dict(zip(df["ts_code"], df["name"]))


def load_industry_members() -> pd.DataFrame:
    """加载行业成员映射"""
    return pd.read_parquet(INDUSTRY_MEMBERS_PATH)


def load_weighted_returns() -> pd.DataFrame:
    """加载行业加权涨跌幅"""
    return pd.read_parquet(WEIGHTED_RETURNS_PATH)


def get_recent_trade_dates(n: int, end_date: str = None) -> List[str]:
    """
    获取最近n个交易日日期列表（从weighted_returns中提取）
    返回格式: ['2026-07-01', '2026-07-02', ...]，从旧到新
    """
    wr = load_weighted_returns()
    dates = sorted(wr["date"].unique())
    if end_date:
        end_dt = pd.Timestamp(end_date)
        dates = [d for d in dates if d <= end_dt]
    return [d.strftime("%Y-%m-%d") for d in dates[-n:]]


def check_prerequisites(trade_date: str) -> Dict:
    """
    检查前置数据是否就绪（加权收益率 + 拥挤度因子）

    参数:
        trade_date: 目标交易日期 YYYY-MM-DD

    返回:
        {
            "all_ready": True/False,
            "weighted_returns": True/False,
            "crowding": True/False,
            "missing": ["weighted_returns", "crowding"],
            "details": {
                "weighted_returns": {"ready": True/False, "path": str, "latest_date": str},
                "crowding": {"ready": True/False, "path": str, "latest_date": str},
            },
        }
    """
    trade_dt = pd.Timestamp(trade_date)
    result: Dict = {
        "all_ready": True,
        "weighted_returns": False,
        "crowding": False,
        "missing": [],
        "details": {},
    }

    # 检查加权收益率
    wr_ready = False
    wr_latest = "N/A"
    if WEIGHTED_RETURNS_PATH.exists():
        try:
            wr = pd.read_parquet(WEIGHTED_RETURNS_PATH, columns=["date"])
            wr_dates = pd.DatetimeIndex(wr["date"].unique())
            wr_ready = trade_dt in wr_dates
            wr_latest = wr_dates.max().strftime("%Y-%m-%d") if len(wr_dates) > 0 else "N/A"
        except Exception:
            wr_ready = False
            wr_latest = "读取失败"
    result["weighted_returns"] = wr_ready
    result["details"]["weighted_returns"] = {
        "ready": wr_ready,
        "path": str(WEIGHTED_RETURNS_PATH),
        "latest_date": wr_latest,
    }

    # 检查拥挤度因子
    cr_ready = False
    cr_latest = "N/A"
    if CROWDING_PATH.exists():
        try:
            cr = pd.read_parquet(CROWDING_PATH, columns=["date"])
            cr_dates = pd.DatetimeIndex(cr["date"].unique())
            cr_ready = trade_dt in cr_dates
            cr_latest = cr_dates.max().strftime("%Y-%m-%d") if len(cr_dates) > 0 else "N/A"
        except Exception:
            cr_ready = False
            cr_latest = "读取失败"
    result["crowding"] = cr_ready
    result["details"]["crowding"] = {
        "ready": cr_ready,
        "path": str(CROWDING_PATH),
        "latest_date": cr_latest,
    }

    # 汇总
    if not wr_ready:
        result["missing"].append("weighted_returns")
    if not cr_ready:
        result["missing"].append("crowding")
    result["all_ready"] = len(result["missing"]) == 0

    return result


def aggregate_industry_amount(trade_date: str, members: pd.DataFrame = None) -> pd.DataFrame:
    """
    聚合个股成交额到行业级别

    参数:
        trade_date: 交易日期 YYYY-MM-DD
        members: 行业成员映射（可选，避免重复加载）

    返回:
        DataFrame: ind_code, amount_yi(亿元)
    """
    parquet_path = MARKET_DAILY_DIR / f"{trade_date}.parquet"
    if not parquet_path.exists():
        return pd.DataFrame(columns=["ind_code", "amount_yi"])

    mkt = pd.read_parquet(parquet_path, columns=["code", "amount"])
    if members is None:
        members = load_industry_members()

    # code 格式两者一致: sz.000001, sh.600000 等
    merged = mkt.merge(members[["stock_code", "ind_code"]], left_on="code", right_on="stock_code", how="inner")

    # amount 单位: 千元 → 亿元 (÷100000)
    industry_amt = merged.groupby("ind_code")["amount"].sum().reset_index()
    industry_amt.columns = ["ind_code", "amount_yi"]
    industry_amt["amount_yi"] = industry_amt["amount_yi"] / 100000  # 千元→亿元

    return industry_amt


def aggregate_industry_amounts_multi(dates: List[str], members: pd.DataFrame = None) -> pd.DataFrame:
    """
    聚合多日个股成交额到行业级别

    参数:
        dates: 日期列表 ['2026-07-01', '2026-07-02', ...]
        members: 行业成员映射（可选）

    返回:
        DataFrame: ind_code, date, amount_yi

    优化(P0-3): 先concat所有天行情数据，再一次性merge+groupby，避免逐日循环merge
    """
    if not dates:
        return pd.DataFrame(columns=["ind_code", "date", "amount_yi"])

    if members is None:
        members = load_industry_members()

    # 先读取所有存在日期的行情数据，一次性concat
    parts = []
    for d in dates:
        parquet_path = MARKET_DAILY_DIR / f"{d}.parquet"
        if not parquet_path.exists():
            continue
        mkt = pd.read_parquet(parquet_path, columns=["code", "amount"])
        mkt["date"] = d
        parts.append(mkt)

    if not parts:
        return pd.DataFrame(columns=["ind_code", "date", "amount_yi"])

    all_mkt = pd.concat(parts, ignore_index=True)

    # 一次性merge行业成员映射
    merged = all_mkt.merge(members[["stock_code", "ind_code"]], left_on="code", right_on="stock_code", how="inner")

    # 一次性groupby: (ind_code, date) → sum(amount)，再转亿元
    result = merged.groupby(["ind_code", "date"])["amount"].sum().reset_index()
    result["amount_yi"] = result["amount"] / 100000  # 千元→亿元
    result = result.drop(columns=["amount"])

    return result


def aggregate_industry_crowding(trade_date: str, members: pd.DataFrame = None) -> pd.DataFrame:
    """
    聚合个股拥挤度到行业级别（取中位数）

    参数:
        trade_date: 交易日期
        members: 行业成员映射

    返回:
        DataFrame: ind_code, crowding_median
    """
    # 776万行数据，先按日期过滤再join
    # pyarrow 支持行级过滤，避免全量加载
    crowding = pd.read_parquet(
        CROWDING_PATH,
        filters=[("date", "==", pd.Timestamp(trade_date))],
        columns=["code", "value"],
    )

    if crowding.empty:
        return pd.DataFrame(columns=["ind_code", "crowding_median"])

    if members is None:
        members = load_industry_members()

    # code 格式统一
    merged = crowding.merge(members[["stock_code", "ind_code"]], left_on="code", right_on="stock_code", how="inner")

    # 行业级中位数（右偏分布，中位数比均值更稳健）
    result = merged.groupby("ind_code")["value"].median().reset_index()
    result.columns = ["ind_code", "crowding_median"]

    return result


def fmt_pct(val: float) -> str:
    """格式化百分比，带正号"""
    if pd.isna(val):
        return "—"
    return f"{val:+.2f}%"


def fmt_amount(val: float) -> str:
    """格式化成交额（亿元）"""
    if pd.isna(val) or val == 0:
        return "—"
    if val >= 100:
        return f"{val:.0f}亿"
    if val >= 10:
        return f"{val:.1f}亿"
    return f"{val:.2f}亿"


# ═══════════════════════════════════════════════
# 🔴 2026-10-06 已移除：冷区宽度反转格式化（format_cold_zone_section）
#    与结构化结论 JSON（_build_m02_conclusion_json_v2 / _write_m02_conclusion_json）。
#    依据 docs/plan/retirement-breadth-tracker-plan.md §2.3。


# ═══════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════

def run(trade_date: str = None) -> str:
    """
    生成当日行业雷达日报（方向 B · 注意力分配），落盘并返回 Markdown。

    🔴 2026-10-15：日报**重排**为方向 B 五章（今日一句话 / 注意力梯队 / 今日异动 /
       事件日历 / 综合判读+数据校验）。正文全部由 `attention_radar` 产出，
       本函数只负责「非交易日防护 + 编排 + 落盘」，不再自己渲染任何章节。

    参数:
        trade_date: 交易日期，默认为今天

    返回:
        Markdown 格式的行业雷达日报
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    td = datetime.strptime(trade_date, "%Y-%m-%d")
    weekday = weekday_names[td.weekday()]

    # 周末/非交易日防护：直接返回且**不落盘**
    # （落盘会在数据根留下空壳日报，下游完备性检查会把它当成「已生成」）
    if td.weekday() >= 5:
        return (
            f"# 📊 行业雷达 · {trade_date}（{weekday}）\n\n"
            f"⏸️ 今日为非交易日（{weekday}），跳过行业雷达。\n"
        )

    # ── 编排：正文由 attention_radar 产出（内部每章独立降级）──
    try:
        from src.daily_review.attention_radar import build_report_markdown

        report_md = build_report_markdown(trade_date)
    except Exception as e:
        logging.error("方向B 日报生成失败: %s", e, exc_info=True)
        report_md = "\n".join([
            f"# 📊 行业雷达 · {trade_date}（{weekday}）",
            "",
            f"> ⚠️ 日报生成失败：{e}",
            "",
            "> 排查：确认 `industry_weighted_returns` / `industry_daily_full` / "
            "`industry_residual_panel` / `industry_nav_924` / `chain_mainline_detection` "
            "五份产物均已更新到该交易日。",
        ])

    # ── 落盘 ──
    # ⚠️ 路径必须经 resolve_output_path（从端写保护）：本机数据根是源端单向同步的副本，
    #    直接写既会被下次 pull 覆盖、又可能污染下游。旧实现绕过该保护，此处已修正。
    try:
        from src.daily_review.report_sections import resolve_report_path

        report_path = resolve_report_path(trade_date)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        save_text(
            report_md, report_path,
            producer="src/daily_review/module_02_industry.py",
            params={"trade_date": trade_date, "kind": "industry_review_report",
                    "layout": "direction_b_v1"},
            inputs=[WEIGHTED_RETURNS_PATH,
                    INDUSTRY_DIR / "industry_daily_full.parquet",
                    INDUSTRY_DIR / "industry_residual_panel.parquet",
                    INDUSTRY_DIR / "industry_nav_924.parquet",
                    INDUSTRY_DIR / "chain_mainline_detection.csv"],
        )
        logging.info("行业雷达日报已保存: %s", report_path)
    except Exception as e:
        logging.error("行业雷达日报保存失败: %s", e)

    return report_md


if __name__ == "__main__":
    # 命令行运行：python -m src.daily_review.module_02_industry [YYYY-MM-DD]
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    date = None
    args = sys.argv[1:]
    # 支持 --date YYYY-MM-DD 或直接传日期
    if len(args) >= 2 and args[0] == "--date":
        date = args[1]
    elif len(args) >= 1:
        date = args[0]
    report = run(date)
    print(report)
