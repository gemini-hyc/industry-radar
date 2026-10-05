"""
通用市场数据加载工具

从 growth_momentum 模块剥离的共享函数：
  - load_recent_market()   原始来源: step1_concept.py
  - compute_concept_daily() 原始来源: step1_concept.py
  - load_industry_members() 原始来源: step1_industry.py
"""
import json
import logging
import os
import sys
import pandas as pd
import numpy as np
from pathlib import Path

logger = logging.getLogger(__name__)

# 数据根统一走 src/paths.py（env INDUSTRY_RADAR_DATA > config.yaml > 默认共享数据目录）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.paths import DATA_DIR, MARKET_DIR, INDUSTRY_DIR
from src.utils.data_quality import load_list_dates, load_no_limit_flags, mark_abnormal

ADJ_DIR = DATA_DIR / "adj_factor"

# ── 均线口径（2026-10-05 修正）──────────────────
# ⛔ 铁律：**均线必须用复权价算**。`market/daily` 的 close 是**未复权**价，而 preclose
#    已做除权调整 —— 直接用 close 算 MA 会让除权日变成"假跳空"（实测 5,216 个疑似除权日、
#    幅度 99 分位 31%、最大 −67%：比亚迪 2025-07-29 337.00 → 111.01）。
#    依据 DATA_CONTRACT.md §四：「均线 / MACD / 布林 / 平台突破…必须先用 adj_factor 复权」。
#    实测影响：个股-天判定翻转 0.69%、行业-天 |Δbreadth|>5pp 占 3.9%、冷区信号集合差 27%。
#    （详见 docs/breadth-what-it-can-and-cannot-do.md §六）
MA20_WINDOW = 20
MA13_WINDOW = 13
ADJ_MIN_COVERAGE = 0.99     # 复权因子覆盖率下限；低于此值告警（宁可吵闹，不要静默错）


def attach_adj_close(mkt: pd.DataFrame) -> pd.DataFrame:
    """给行情表挂上复权因子并算出 `adj_close = close × adj_factor`。

    只有**价格水平类**指标需要它（本项目仅宽度用的 MA20/MA13）；
    `pctChg` 已是真实涨跌（除权调整过）⇒ 收益率类计算不受影响，无需改动。
    缺失或覆盖率不足时**不抛异常**（避免拖垮日更），但会 `logger.warning`。
    """
    if mkt.empty or "close" not in mkt.columns or "code" not in mkt.columns:
        return mkt
    m = mkt.copy()
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    frames, missing = [], 0
    for d in pd.DatetimeIndex(m["date"].unique()).sort_values():
        p = ADJ_DIR / f"{d:%Y-%m-%d}.parquet"
        if not p.exists():
            missing += 1
            continue
        a = pd.read_parquet(p, columns=["code", "adj_factor"])
        a["date"] = d
        frames.append(a)
    if not frames:
        logger.warning("复权因子不可用（%s 无文件）⇒ 均线将退化为未复权价，宽度口径不合规", ADJ_DIR)
        return m
    adj = pd.concat(frames, ignore_index=True)
    m = m.merge(adj, on=["date", "code"], how="left")
    m["adj_close"] = m["close"] * m["adj_factor"]
    cov = float(m["adj_factor"].notna().mean())
    if missing or cov < ADJ_MIN_COVERAGE:
        logger.warning("复权因子覆盖不足：缺 %d 个交易日文件，行覆盖率 %.4f（下限 %.2f）",
                       missing, cov, ADJ_MIN_COVERAGE)
    return m


# ── 异常样本剔除（2026-10-01）──────────────────
# 行业聚合不应包含「当日无涨跌幅限制」的观测（新股/复牌/退市整理）。
# 判据与实测影响见 src/utils/data_quality.py 的模块 docstring。

def drop_abnormal_samples(market_df: pd.DataFrame):
    """剔除不应进入行业聚合的异常观测。返回 (清洗后 df, 统计)。"""
    if market_df.empty or "pctChg" not in market_df.columns:
        return market_df, {"total": 0, "marked": 0}

    dates = pd.to_datetime(market_df["date"]).dt.normalize().unique()
    nolim = load_no_limit_flags(dates)
    mask, stats = mark_abnormal(market_df, no_limit=nolim, list_dates=load_list_dates())
    if stats.get("marked", 0) == 0:
        return market_df, stats
    return market_df[~mask].copy(), stats


# ── 行情加载 ──────────────────────────────────

def load_recent_market(days: int = 250, end_date: str = None) -> pd.DataFrame:
    files = sorted(Path(MARKET_DIR).glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    if end_date:
        end_ts = pd.Timestamp(end_date)
        files = [f for f in files if pd.Timestamp(f.stem[:10]) <= end_ts]
    files = files[-days:]
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

    # 修复: 2026-05-29起数据源的amount单位变成了万元(volume正常)
    # 判断: 均价(amount/volume) < 0.1元 → 单位错误，乘10000还原
    if not df.empty and "amount" in df.columns and "volume" in df.columns:
        mask = (df["volume"] > 0) & (df["amount"] / df["volume"] < 0.1)
        bad_count = mask.sum()
        if bad_count > 0:
            df.loc[mask, "amount"] = df.loc[mask, "amount"] * 10000

    # 复权价（2026-10-05）：均线必须用复权价，详见 attach_adj_close 的说明
    df = attach_adj_close(df)
    return df


# ── 概念/行业日频聚合 ─────────────────────────

def _members_relation(concept_members: dict, members_daily):
    """
    构造「股票 → 行业」归属关系，供 compute_concept_daily 使用。

    返回 (rel, is_pit)：
      - is_pit=True ：rel 为 (date, con_code, code) —— 按【当日真实成分】关联，消除前视偏差；
      - is_pit=False：rel 为 (con_code, code)       —— 按静态快照关联（date 无关，沿用旧行为）。
    """
    if members_daily is not None and len(members_daily) > 0:
        need = {"date", "ind_code", "stock_code"}
        missing = need - set(members_daily.columns)
        if missing:
            raise ValueError(
                f"members_daily 缺少列 {sorted(missing)}；需要 {sorted(need)}"
                "（长表：date, ind_code, stock_code）")
        rel = (members_daily[["date", "ind_code", "stock_code"]]
               .rename(columns={"ind_code": "con_code", "stock_code": "code"})
               .drop_duplicates())
        rel["date"] = pd.to_datetime(rel["date"]).dt.normalize()
        return rel, True

    rel = pd.DataFrame(
        [(k, c) for k, vs in concept_members.items() for c in vs],
        columns=["con_code", "code"],
    ).drop_duplicates()
    return rel, False


def compute_concept_daily(market_df: pd.DataFrame,
                          concept_members: dict,
                          drop_abnormal: bool = True,
                          breadth_dropna: bool = True,
                          members_daily: pd.DataFrame = None) -> pd.DataFrame:
    """
    为每个概念/行业计算每日聚合指标。
    返回 DataFrame: date, con_code, avg_pct, breadth(MA20基准),
                     breadth_ma13(MA13基准,预警期用), total_amount, stock_n,
                     avg_open(等权均开盘价), avg_close(等权均收盘价), n_stocks

    参数:
      drop_abnormal : 剔除「当日无涨跌幅限制」的观测（新股/复牌/退市整理）。
                      2026-10-01 起默认开启 —— 见 src/utils/data_quality.py。
      breadth_dropna: 宽度分母只计「MA 已成型」的个股。原实现里
                      `close > ma20` 在 ma20 为 NaN 时得 False，使新上市
                      (<5 日) 个股被当作「未站上均线」计入分母，系统性压低 breadth。
      members_daily : 可选，PIT 成分长表 (date, ind_code, stock_code)。
                      传入时按【当日真实成分】聚合 —— 这是消除前视偏差的正确口径：
                      若用 concept_members 的当前快照回填全历史，早期日期会混入
                      当时尚未纳入该行业的个股（实测 2020-01-02 静态表 5644 只
                      vs 真实 3817 只，混入 32%），且 n_stocks 会退化成常数。
                      不传则沿用静态快照（旧行为，适合报告/跟踪池等短窗口调用）。

    注意（MA 计算顺序与价格基准）:
      均线必须在【个股自身完整序列】上计算，再做成分归属过滤。若先按成分过滤
      再算 MA，PIT 模式下个股序列会被截断（进入行业前 / 退出行业后的行情被丢弃），
      得到的并非该股真实的 20 日均线。静态快照下两种顺序等价（成员表不随日期变）。
      ⛔ **MA 用 `adj_close`（复权价），不是 `close`**（2026-10-05 起）：未复权价在除权日
      有假跳空，会让 MA 判错整天。`market_df` 应由 `load_recent_market`（已自动挂 adj_close）
      或 `attach_adj_close` 提供；缺失时退化为 `close` 并告警。
      `min_periods` = 窗口长度（20 / 13）—— 未满窗口的个股**不进宽度分母**，与文档语义一致。
      ⚠️ 由此产生一条**窗口约束**：调用方加载的交易日必须比目标日多至少 19 天，
      否则目标日所有个股的 MA 都未成型、宽度退化为 NaN。`build_industry_daily_full.py`
      已内置该护栏（拒绝写窗口前 19 天）。
    """
    if market_df.empty or not concept_members:
        return pd.DataFrame()

    mkt = market_df.copy()
    mkt["date"] = pd.to_datetime(mkt["date"]).dt.normalize()

    # ── ① n_stocks（行业成员总数）在剔除异常样本【前】统计，保持原语义 ──
    #    仅静态快照路径使用；PIT 路径下 n_stocks 逐日不同，在 ⑤ 里按日计算。
    n_stocks_map = {}
    if drop_abnormal and members_daily is None:
        for con_code, codes in concept_members.items():
            n_stocks_map[con_code] = mkt.loc[mkt["code"].isin(codes), "code"].nunique()

    # ── ② 剔除「当日无涨跌幅限制」的观测 ──
    if drop_abnormal:
        mkt, _stats = drop_abnormal_samples(mkt)
        if mkt.empty:
            return pd.DataFrame()

    # ── ③ 均线在「个股自身完整序列」上计算（与成分归属无关）──
    #    ⛔ 必须用**复权价**（2026-10-05）：未复权 close 在除权日有假跳空，MA 会整天判错。
    #    min_periods = 窗口长度（不再是 5）—— 5 日均值不是"MA20"，与"MA 未成型不进分母"的
    #    文档语义不符（旧口径让上市第 5~19 日的个股用短窗口均值充当 MA20，且使宽度依赖加载窗口）。
    price_col = "adj_close" if ("adj_close" in mkt.columns and mkt["adj_close"].notna().any()) else "close"
    if price_col == "close":
        logger.warning(
            "compute_concept_daily 未收到 adj_close ⇒ 均线将用未复权价计算（口径违规，"
            "见 DATA_CONTRACT.md §四.3）；调用方应先用 market_data.attach_adj_close 或 load_recent_market")
    mkt = mkt.sort_values(["code", "date"]).copy()
    mkt["ma20"] = mkt.groupby("code")[price_col].transform(
        lambda x: x.rolling(MA20_WINDOW, min_periods=MA20_WINDOW).mean())
    _a20 = mkt[price_col] > mkt["ma20"]
    # breadth_dropna: ma20 未成型(NaN)的个股不参与分母。
    # 原实现下 `close > NaN` → False → 被当作「未站上均线」计入分母，系统性压低 breadth。
    mkt["above_ma20"] = (_a20.where(mkt["ma20"].notna()) if breadth_dropna
                         else _a20.astype(int))
    mkt["ma13"] = mkt.groupby("code")[price_col].transform(
        lambda x: x.rolling(MA13_WINDOW, min_periods=MA13_WINDOW).mean())
    _a13 = mkt[price_col] > mkt["ma13"]
    mkt["above_ma13"] = (_a13.where(mkt["ma13"].notna()) if breadth_dropna
                         else _a13.astype(int))

    # ── ④ 关联成分归属 ──
    rel, is_pit = _members_relation(concept_members, members_daily)
    join_on = ["date", "code"] if is_pit else ["code"]
    mkt = mkt.merge(rel, on=join_on, how="inner")
    if mkt.empty:
        return pd.DataFrame()

    # PIT: n_stocks = 当日该行业【成员数】（与当日是否有行情无关）
    ns_by_date = (rel.groupby(["con_code", "date"]).size().rename("n_stocks")
                  if is_pit else None)
    has_open = "open" in mkt.columns

    # ── ⑤ 按行业聚合 ──
    records = []
    for con_code, g in mkt.groupby("con_code", sort=True):
        daily = g.groupby("date").agg(
            avg_pct=("pctChg", "mean"),
            total_amount=("amount", "sum"),
            stock_n=("code", "nunique"),
            breadth=("above_ma20", "mean"),
            breadth_ma13=("above_ma13", "mean"),
        )
        # 等权平均开盘价/收盘价（预警A3大实体阴线用）
        # 市场daily数据无total_mv，用等权均值替代（5%阈值足够粗，等权不影响判断方向）
        if has_open:
            daily = daily.join(g.groupby("date").agg(
                avg_open=("open", "mean"),
                avg_close=("close", "mean"),
            ))
        daily = daily.reset_index()

        if is_pit:
            daily = daily.merge(ns_by_date.loc[con_code].reset_index(),
                                on="date", how="left")
        else:
            daily["n_stocks"] = n_stocks_map.get(con_code, g["code"].nunique())

        daily["con_code"] = con_code
        records.append(daily)

    if not records:
        return pd.DataFrame()

    result = pd.concat(records, ignore_index=True)
    # 确保列存在（数据不可用时补NaN）
    for col in ["breadth_ma13", "avg_open", "avg_close"]:
        if col not in result.columns:
            result[col] = float("nan")
    return result


# ── 行业成员加载 ──────────────────────────────

def load_industry_members() -> dict:
    """加载富途行业板块 → 成分股映射 {ind_code: [stock_codes]}（静态快照）"""
    members_path = INDUSTRY_DIR / "industry_members.parquet"
    if not members_path.exists():
        return {}

    df = pd.read_parquet(members_path)
    mapping = {}
    for ind_code, group in df.groupby("ind_code"):
        mapping[ind_code] = group["stock_code"].tolist()
    return mapping


def load_industry_members_daily() -> pd.DataFrame:
    """
    加载 PIT（point-in-time）成分长表 (date, ind_code, stock_code)。

    来源 industry_members_daily.parquet，由 scripts/build_members_daily.py 生成，
    基于申万成分的 in_date / out_date 逐日重建 —— 用于消除聚合时的前视偏差。

    文件不存在或结构不符时返回空表，调用方据此决定回退或报错。
    """
    path = INDUSTRY_DIR / "industry_members_daily.parquet"
    if not Path(path).exists():
        return pd.DataFrame()

    df = pd.read_parquet(path)
    need = {"date", "ind_code", "stock_code"}
    if not need.issubset(df.columns):
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df[["date", "ind_code", "stock_code"]]
