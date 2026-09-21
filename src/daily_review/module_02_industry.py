"""
每日复盘系统 · 模块二：行业跟踪（v2 三区框架）
核心问题：钱在往哪里流？当前有哪些值得注意的信号？

v2 变更（2026-07-24）：
  - 用三区框架替换旧 5 子模块（排名/持续性/成交额/生命周期/综合研判）
  - 冷区宽度反转因子 → 唯一已验证有效的信号
  - 中区/热区预留接口

数据源：
  - 行业日频数据 → data/industry/industry_daily.parquet
  - 行业成员映射 → data/industry/industry_members.parquet
  - 行业名称列表 → data/industry/industry_list.parquet
  - 拥挤度因子 → data/factors/ind_crowd_turnover_daily.parquet
  - 行业加权涨跌幅 → data/industry/industry_weighted_returns.parquet
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

# 项目路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# 数据路径常量
WEIGHTED_RETURNS_PATH = DATA_DIR / "industry" / "industry_weighted_returns.parquet"
INDUSTRY_MEMBERS_PATH = DATA_DIR / "industry" / "industry_members.parquet"
INDUSTRY_LIST_PATH = DATA_DIR / "industry" / "industry_list.parquet"
TRACKER_STATE_PATH = DATA_DIR / "industry" / "tracker_state.json"
MARKET_DAILY_DIR = DATA_DIR / "market" / "daily"
CROWDING_PATH = DATA_DIR / "factors" / "ind_crowd_turnover_daily.parquet"
INDUSTRY_DAILY_CACHE = DATA_DIR / "industry" / "industry_daily.parquet"
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


def load_tracker_state() -> Dict:
    """加载跟踪状态JSON，文件缺失时返回空字典并容错降级"""
    try:
        with open(TRACKER_STATE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        logging.warning("跟踪状态文件缺失: %s，生命周期全景将为空", TRACKER_STATE_PATH)
        return {}
    except json.JSONDecodeError as e:
        logging.error("跟踪状态文件解析失败: %s — %s", TRACKER_STATE_PATH, e)
        return {}


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

    注意：行业日频数据 (industry_daily.parquet) 是缓存，由 load_or_compute_industry_daily()
    自动计算，不纳入前置检查。

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


def load_or_compute_industry_daily(days: int = 60, end_date: str = None) -> pd.DataFrame:
    """
    加载或计算行业日频数据（宽度、涨跌幅、成交额），优先使用 Parquet 缓存

    这是 run_wake_report.py 中 BayesianTracker.load_daily_data() 的轻量替代——
    只产出 daily_data，不运行状态机。

    返回:
        DataFrame: date, con_code, avg_pct, total_amount, stock_n, breadth,
                   breadth_ma13, avg_open, avg_close
    """
    # 检查缓存是否覆盖到目标日期
    if INDUSTRY_DAILY_CACHE.exists():
        cached = pd.read_parquet(INDUSTRY_DAILY_CACHE)
        cached_dates = pd.DatetimeIndex(cached["date"].unique())
        if len(cached_dates) > 0:
            max_cached = cached_dates.max()
            target = pd.Timestamp(end_date) if end_date else pd.Timestamp.now().normalize()
            if max_cached >= target and len(cached_dates) >= min(days, 30):
                return cached

    # 缓存未命中 → 从原始行情数据计算
    from src.utils.market_data import load_industry_members as _load_ind_members
    from src.utils.market_data import load_recent_market, compute_concept_daily

    members_dict = _load_ind_members()  # {ind_code: [stock_codes]}
    mkt = load_recent_market(days=days, end_date=end_date)
    daily = compute_concept_daily(mkt, members_dict)

    if daily.empty:
        return daily

    # 替换为市值加权涨跌幅（与 BayesianTracker 保持一致）
    if WEIGHTED_RETURNS_PATH.exists():
        wr = pd.read_parquet(WEIGHTED_RETURNS_PATH)
        wr["date"] = pd.to_datetime(wr["date"])
        daily = daily.merge(
            wr[["date", "ind_code", "weighted_pct"]],
            left_on=["date", "con_code"],
            right_on=["date", "ind_code"],
            how="left",
        )
        mask = daily["weighted_pct"].notna()
        daily.loc[mask, "avg_pct"] = daily.loc[mask, "weighted_pct"]
        daily = daily.drop(columns=["ind_code", "weighted_pct"])

    # 写入缓存（失败不阻塞主流程）
    try:
        INDUSTRY_DAILY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        daily.to_parquet(INDUSTRY_DAILY_CACHE, index=False)
    except Exception:
        pass

    return daily


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
# 冷区宽度反转信号 — 格式化
# ═══════════════════════════════════════════════

TIER_LABELS = {
    "high": "★★★ 高置信",
    "standard": "★★ 标准",
    "watch": "★ 观察",
}

TIER_ORDER = {"high": 0, "standard": 1, "watch": 2}


def format_cold_zone_section(signals: List[Dict]) -> str:
    """
    格式化冷区宽度反转信号为 Markdown。

    参数:
        signals: detect_signals() 返回的信号列表

    返回:
        Markdown 格式的冷区报告段落
    """
    lines = ["🧊 **一、宽度反转信号（冷区）**", ""]

    if not signals:
        lines.append("今日无冷区宽度反转信号。")
        lines.append("")
        lines.append("> 触发条件：前20日均宽 ≤ 冷区阈值，连续扩张 ≥ 3天")
        return "\n".join(lines)

    lines.append("> 触发条件：前20日均宽 ≤ 冷区阈值，连续扩张 ≥ 3天")
    lines.append(f"> 今日共 {len(signals)} 条信号（按冷度排序）")
    lines.append("")

    # ── 信号列表 ──
    lines.append("| 行业 | 级别 | 前20日均宽 | D1/D2/D3 | 量比 | 宽度轨迹 | 拥挤 |")
    lines.append("|:-----|:----:|:----------:|:---------|:----:|:---------|:----:|")

    for s in signals:
        name = s["industry_name"]
        tier = s["tier"]
        pre_bw = s["pre_breadth_20d"]
        d_vals = f"{s['d1']:.1%}/{s['d2']:.1%}/{s['d3']:.1%}"
        vol = f"{s['vol_ratio']:.1f}x"
        traj = s["breadth_trajectory"]
        crowd = s.get("crowding")
        crowd_str = f"{crowd:.0%}" if crowd is not None else "—"

        tier_icon = {"high": "🔴", "standard": "🟡", "watch": "🟢"}.get(tier, "")

        lines.append(
            f"| {tier_icon} {name} | **{TIER_LABELS[tier]}** | {pre_bw:.0f}% "
            f"| {d_vals} | {vol} | {traj} | {crowd_str} |"
        )

    lines.append("")

    # ── 信号分级说明 ──
    lines.append("### 信号分级")
    lines.append("")

    by_tier: Dict[str, List[Dict]] = {"high": [], "standard": [], "watch": []}
    for s in signals:
        by_tier[s["tier"]].append(s)

    for tier_key in ["high", "standard", "watch"]:
        tier_signals = by_tier[tier_key]
        if not tier_signals:
            continue
        label = TIER_LABELS[tier_key]
        lines.append(f"**{label}**：")
        lines.append("")
        for s in tier_signals:
            name = s["industry_name"]
            pre_bw = s["pre_breadth_20d"]

            # 关键特征
            features = [f"前20日均宽 {pre_bw:.0f}%"]
            if s["d1_ratio"] >= 0.60:
                features.append("首日爆发力强")
            if s["vol_ratio"] >= 1.3:
                features.append(f"显著放量({s['vol_ratio']:.1f}x)")
            elif s["vol_ratio"] >= 1.1:
                features.append(f"温和放量({s['vol_ratio']:.1f}x)")
            if s["crowding"] is not None and s["crowding"] > 0.6:
                features.append(f"⚠️拥挤度偏高({s['crowding']:.0%})")

            feature_str = "，".join(features)
            lines.append(f"- **{name}**：{feature_str}")
        lines.append("")

    lines.append("> 分级依据：前20日均宽 <10% 高置信 / <20% 标准 / <冷区阈值 观察")
    lines.append("> D1占比和量比为附属参考信息，不参与信号分级。")

    return "\n".join(lines)


# ═══════════════════════════════════════════════
# 结构化 JSON 输出（v2 — 三区框架）
# ═══════════════════════════════════════════════

def _build_m02_conclusion_json_v2(
    trade_date: str,
    cold_signals: List[Dict],
) -> Dict:
    """
    构建模块二 v2 结构化结论 JSON。

    参数:
        trade_date: 交易日期
        cold_signals: 冷区宽度反转信号列表

    返回:
        Dict 符合 plan/module_02_rebuild.md §5.2 规格
    """
    return {
        "date": trade_date,
        "module": "industry_tracking_v2",
        "cold_zone_signals": [
            {
                "industry_code": s["industry_code"],
                "industry_name": s["industry_name"],
                "tier": s["tier"],
                "pre_breadth_20d": s["pre_breadth_20d"],
                "expansion_days": s["expansion_days"],
                "breadth_trajectory": s["breadth_trajectory"],
                "crowding": s["crowding"],
            }
            for s in cold_signals
        ],
        "mid_zone": None,   # 待实现
        "hot_zone": None,   # 待实现
    }


def _write_m02_conclusion_json(trade_date: str, conclusion: Dict) -> Optional[Path]:
    """将模块二结构化结论写入 JSON 文件"""
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORTS_DIR / f"{trade_date}_module_02_conclusion.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(conclusion, f, ensure_ascii=False, indent=2)
        logging.info("模块二结构化结论已写入 %s", path)
        return path
    except Exception as e:
        logging.warning("模块二结构化结论写入失败: %s", e)
        return None


# ═══════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════

def run(trade_date: str = None) -> str:
    """
    运行行业跟踪全模块（v2 三区框架），返回 Markdown 格式报告。

    参数:
        trade_date: 交易日期，默认为今天

    返回:
        Markdown 格式的行业跟踪报告
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    # 标题
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    td = datetime.strptime(trade_date, "%Y-%m-%d")
    weekday = weekday_names[td.weekday()]

    # 周末/非交易日防护
    if td.weekday() >= 5:
        return (
            f"# 📊 行业跟踪 · {trade_date}（{weekday}）\n\n"
            f"⏸️ 今日为非交易日（{weekday}），跳过行业跟踪。\n"
        )

    lines = [
        f"# 📊 行业跟踪 · {trade_date}（{weekday}）",
        "",
    ]

    errors: List[str] = []

    # ── 加载数据 ──
    daily_data = None
    try:
        daily_data = load_or_compute_industry_daily(days=80, end_date=trade_date)
    except Exception as e:
        logging.error("行业日频数据加载失败: %s", e, exc_info=True)
        errors.append(f"行业日频数据加载失败: {e}")
        lines.append(f"⚠️ 行业日频数据加载失败：{e}")
        lines.append("")
        report_md = "\n".join(lines)
        return report_md

    names = {}
    try:
        names = load_industry_names()
    except Exception as e:
        logging.warning("行业名称加载失败: %s", e)

    # 加载行业成员映射（多处使用，提前加载）
    members = load_industry_members()

    # 拥挤度映射
    crowding_map = {}
    try:
        crowding_df = aggregate_industry_crowding(trade_date, members)
        if not crowding_df.empty:
            crowding_map = dict(zip(crowding_df["ind_code"], crowding_df["crowding_median"]))
    except Exception as e:
        logging.warning("拥挤度聚合失败（非致命）: %s", e)

    # ── 一、冷区宽度反转信号 ──
    cold_signals: List[Dict] = []
    try:
        from src.daily_review.factor_width_reversal import detect_signals

        # 当日全市场宽度（用于动态调制冷区阈值）
        today_data = daily_data[daily_data["date"] == trade_date]
        market_breadth = float(today_data["breadth"].mean()) if len(today_data) > 0 else None

        cold_signals = detect_signals(
            trade_date, daily_data, names, crowding_map,
            market_breadth=market_breadth,
        )
        lines.append(format_cold_zone_section(cold_signals))
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
    except Exception as e:
        logging.error("module_02 冷区信号检测失败: %s", e, exc_info=True)
        errors.append(f"冷区信号检测失败: {e}")
        lines.append(f"⚠️ 冷区信号检测失败：{e}")
        lines.append("")

    # ── 二、中区行业（待实现） ──
    lines.append("🟡 **二、中区行业**（待实现）")
    lines.append("")
    lines.append("> 中区（宽度30-70%）信号挖掘尚未完成，待回测验证后加入。")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    # ── 三、热区行业（待实现） ──
    lines.append("🔴 **三、热区行业**（待实现）")
    lines.append("")
    lines.append("> 热区（宽度>70%）信号挖掘尚未完成，待回测验证后加入。")
    lines.append("")

    # ── 结构化 JSON 输出 ──
    try:
        m02_json = _build_m02_conclusion_json_v2(trade_date, cold_signals)
        _write_m02_conclusion_json(trade_date, m02_json)
    except Exception as e:
        logging.warning("module_02 结构化JSON写入失败（非致命）: %s", e)

    # ── 错误汇总 ──
    if errors:
        lines.append("")
        lines.append("⚠️ **错误汇总：**")
        for err in errors:
            lines.append(f"- {err}")

    report_md = "\n".join(lines)

    # ── 持久化报告 ──
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / f"{trade_date}_industry_review.md"
        report_path.write_text(report_md, encoding="utf-8")
        logging.info("行业跟踪报告已保存: %s", report_path)
    except Exception as e:
        logging.error("行业跟踪报告保存失败: %s", e)

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
