#!/usr/bin/env python3
"""
行业数据日更完备性监控 — 防「静默缺数」

日更链路跑完后，检查目标交易日的关键产出是否出现：
  1. 整行缺失 —— 某行业当日完全没有行（会被下游静默忽略）
  2. 列级空洞 —— 行存在但某列整列为空（如 nav_e 因上游滞后而全 NaN）
  3. 成分漂移 —— 某行业当日成分股数相对自身历史骤降（数据面缺股）
  4. 源数据缺位 —— market/daily 或 daily_basic 缺当日文件
  5. 上游滞后 —— 等权源 industry_daily_full 落后于目标日
  6. 产物指纹缺位 —— 已登记产物没有 <文件名>.meta.json（来历不明 / 接入指纹前的存量）

用法:
    python3 scripts/check_data_completeness.py [--date YYYY-MM-DD]
                                               [--json] [--quiet] [--no-write]
    python3 scripts/check_data_completeness.py --audit-writes   # 审计未登记写入（人工，不判 rc）

退出码:
    0  健康
    1  严重（CRITICAL）
    2  警告（WARNING）
    3  跳过（目标日非交易日）

产出:
    stdout 报告 + industry/completeness_status.json（供程序消费/事后追溯）
"""
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import (MARKET_DIR, BASIC_DIR, INDUSTRY_DIR, DATA_DIR,
                       ARTIFACTS, DERIVED_DIRS)
from src.utils.provenance import (load_meta, git_info, sidecar_path, save_json,
                                 shared_code_dirty, SIDECAR_SUFFIX)

ADJ_DIR = DATA_DIR / "adj_factor"
LIMIT_DIR = DATA_DIR / "stk_limit"

WR_PATH = INDUSTRY_DIR / "industry_weighted_returns.parquet"
NAV_PATH = INDUSTRY_DIR / "industry_nav_924.parquet"
MEMBERS_PATH = INDUSTRY_DIR / "industry_members.parquet"
MEMBERS_DAILY_PATH = INDUSTRY_DIR / "industry_members_daily.parquet"
FULL_PATH = INDUSTRY_DIR / "industry_daily_full.parquet"
STATUS_PATH = INDUSTRY_DIR / "completeness_status.json"

CRITICAL, WARNING = "CRITICAL", "WARNING"

# ── 告警阈值 ──
STOCK_N_DROP_RATIO = 0.5    # 行业当日成分股数 < 自身历史中位数 × 0.5 → 警告
STOCK_N_MIN_MEDIAN = 5      # 仅对历史中位数 ≥ 5 的行业做骤降检测（小行业噪声大）
MEMBERS_DROP_RATIO = 0.85   # 当日全市场成分股数 < 前一交易日 × 0.85 → 警告
NAV_COLS = ["nav_w", "nav_e", "rs_w"]

# 宽度异常探针（2026-10-05）：阈值经全历史校准，见 check_breadth 的 docstring
BREADTH_JUMP_K = 6.0        # |Δ| > K × (1.4826×MAD)；K=6 ⇒ 全历史仅 2/1635 触发（0.1%）
BREADTH_MAD_WINDOW = 250    # 稳健尺度回看窗口（不含目标日）
BREADTH_MAD_MIN = 60        # 回看样本下限，不足则跳过跳变判据（零值判据不受限）


def issue(level: str, code: str, message: str, fix: str = "", **detail) -> dict:
    d = {"level": level, "code": code, "message": message}
    if fix:
        d["fix"] = fix
    if detail:
        d["detail"] = detail
    return d


# ─────────────────────────── 目标交易日 ───────────────────────────
def resolve_target_date(explicit: str = None):
    """显式指定优先；否则取 market/daily 最新文件日期（天然规避非交易日）"""
    if explicit:
        return pd.Timestamp(explicit).normalize(), "显式指定"
    files = sorted(MARKET_DIR.glob("*.parquet"))
    if not files:
        return None, "无行情文件"
    return pd.Timestamp(files[-1].stem).normalize(), "market/daily 最新文件"


def is_trade_day(t: pd.Timestamp):
    """查 trade_cal 判断是否开市日。

    注意：本地 trade_cal.parquet **只含开市日**（is_open 恒为 1，无周末/节假日行），
    因此「未命中」即非交易日；仅当目标日超出日历覆盖范围时返回 None（无法判定）。
    """
    cal_path = DATA_DIR / "trade_cal.parquet"
    if not cal_path.exists():
        return None
    try:
        cal = pd.read_parquet(cal_path, columns=["cal_date", "is_open"])
        keys = cal["cal_date"].astype("int64")
        key = int(t.strftime("%Y%m%d"))
        hit = cal[keys == key]
        if len(hit):
            return bool(hit["is_open"].iloc[0])
        if key < int(keys.min()) or key > int(keys.max()):
            return None  # 超出日历覆盖范围，无法判定
        return False     # 日历只放开市日 → 未命中即非交易日
    except Exception:
        return None


def prev_trade_day(t: pd.Timestamp):
    """目标日之前最近一个行情文件日期（用于日间对比）"""
    files = sorted(MARKET_DIR.glob("*.parquet"))
    prior = [f for f in files if pd.Timestamp(f.stem) < t]
    return pd.Timestamp(prior[-1].stem) if prior else None


# ─────────────────────────── 各项检查 ───────────────────────────
def check_sources(t: pd.Timestamp):
    """1. 源数据：market/daily、daily_basic、adj_factor、stk_limit 当日文件

    suspend_d 不在此列：多数交易日 Tushare 返回 0 行、当日无文件属正常，强制要求会天天误报。
    """
    out = []
    targets = [("market/daily 行情", MARKET_DIR / f"{t:%Y-%m-%d}.parquet"),
               ("daily_basic 市值", BASIC_DIR / f"{t:%Y-%m-%d}.parquet"),
               ("adj_factor 复权因子", ADJ_DIR / f"{t:%Y-%m-%d}.parquet"),
               ("stk_limit 涨跌停价", LIMIT_DIR / f"{t:%Y-%m-%d}.parquet")]
    for label, p in targets:
        if not p.exists():
            out.append(issue(
                CRITICAL, "SOURCE_MISSING", f"{label} 缺当日文件: {p.name}",
                fix="确认源端 Tushare 日更（stock-doctor）已跑完，或从源端 pull.sh 同步",
                path=str(p)))
    return out


# ── 值分布 / 列结构阈值（防静默缺数：文件在 ≠ 数据对）──
MAX_ADJ_DIRTY = 0.001    # market/daily 中「非分币精度」占比上限（复权价污染探针）
BASIC_MIN_COLS = 18      # daily_basic 最少列数（曾有 240 天只有 4 列）
MIN_ADJ_COVERAGE = 0.99  # adj_factor 对 market/daily 股票的覆盖率下限

# ── 列级非空覆盖下限（防「行齐列空」）──
# 2026-10-21 事故：daily_basic 当日 **5568 行、18+ 列，但 pb / pe_ttm 两列 0 非空**。
# 行数、列数、文件时间戳全部正常 ⇒ 既有检查一路放行 ⇒ 坏文件入库 ⇒
# value 因子当日 NaN ⇒ residualize 的 A1[t] 一个 NaN 把 131 个行业的 adj 全灭 ⇒
# rolling_corr 的 250 日窗把 NaN 传染 60+ 个交易日。**没有任何一处报错**。
# 结论：只查「文件在不在 / 行数 / 列数」永远抓不到这一类，必须查**每列的非空率**。
#
# 阈值口径（写死前先跑稳态基线，2026-10-23 取近 60 个交易日实测最小值）：
#     daily_basic  pb 0.990 / pe_ttm 0.705 / total_mv 1.000 / circ_mv 1.000 / dv_ratio 0.675
#     market/daily close 1.000 / volume 1.000 / amount 1.000
#     adj_factor   adj_factor 1.000
# 阈值取「基线最小值再留 5~10pct 余量」，保证健康日 **0 触发**。
# ⚠️ 新增监控规则必须先跑一遍稳态基线再上线 —— hk_hold 与 PROV_DIRTY 两条永久红灯
#    都是跳过了这一步（假设了"当日可取"、"工作区不脏"而没验证）。
COL_COVERAGE_MIN = {
    "market/daily": {"close": 0.98, "volume": 0.98, "amount": 0.98},
    "daily_basic": {"pb": 0.90, "pe_ttm": 0.60,
                    "total_mv": 0.95, "circ_mv": 0.95, "dv_ratio": 0.55},
    "adj_factor": {"adj_factor": 0.98},
}


def _col_coverage(p, cols):
    """读 parquet → {列: 非空率}，并返回缺失列集合（只读需要的列，避免整表加载）。"""
    import pyarrow.parquet as pq
    have = set(pq.ParquetFile(p).schema.names)
    use = [c for c in cols if c in have]
    if not use:
        return {}, set(cols)
    df = pd.read_parquet(p, columns=use)
    return {c: float(df[c].notna().mean()) for c in use}, set(cols) - have


def check_col_coverage(t: pd.Timestamp):
    """1b-2. 列级非空覆盖 —— 堵「行齐列空」。见 COL_COVERAGE_MIN 的说明。"""
    out, stats = [], {}
    for ds, expect in COL_COVERAGE_MIN.items():
        p = DATA_DIR / ds / f"{t:%Y-%m-%d}.parquet"
        if not p.exists():
            continue          # 文件缺失由 check_sources 负责，这里不重复报
        try:
            cov, absent = _col_coverage(p, expect)
        except Exception as e:
            out.append(issue(WARNING, "COL_PROBE_FAIL",
                             f"{ds} 列覆盖检查异常: {e}", date=str(t.date())))
            continue
        stats[ds] = {k: round(v, 4) for k, v in cov.items()}
        if absent:
            out.append(issue(
                WARNING, "COL_ABSENT",
                f"{ds} 缺列 {sorted(absent)}（口径变更或抓取漏字段）",
                fix="核对抓取脚本列清单；口径变更须同步 docs/DATA_CONTRACT",
                date=str(t.date()), dataset=ds, absent=sorted(absent)))
        for c, got in cov.items():
            need = expect[c]
            if got < need:
                out.append(issue(
                    CRITICAL, "COL_EMPTY",
                    f"{ds}.{c} 当日非空率 {got:.1%} < {need:.0%}"
                    f"（行齐列空：行数正常但该列基本无值，下游因子会当日失效）",
                    fix=f"重下 {ds}/{t:%Y-%m-%d}.parquet（fetch_daily.py 已加列覆盖门禁，"
                        f"不达标拒盘）；若为源端问题，确认 Tushare 该字段当日是否有值",
                    date=str(t.date()), dataset=ds, column=c, coverage=round(got, 4)))
    return out, stats


def check_hygiene(t: pd.Timestamp):
    """1b. 值分布与列结构 —— 堵住「文件都在、数据悄悄不对」的静默问题。

    2026-09-23 两次静默事故都是这一类：daily_basic 有 240 天只有 4 列、
    market/daily 混入复权价（2020 年占 82%）。两者都不改变文件存在性，
    只靠「文件在不在」的检查永远发现不了。
    """
    out, stats = [], {}
    mp = MARKET_DIR / f"{t:%Y-%m-%d}.parquet"

    if mp.exists():
        try:
            px = pd.read_parquet(mp, columns=["code", "close"])
            n = len(px)
            # 复权价污染探针：A 股报价两位小数，复权价会带长小数
            vals = (px["close"].dropna() * 100).to_numpy()
            dirty = int((abs(vals - vals.round()) > 1e-6).sum())
            stats["price_dirty_ratio"] = round(dirty / max(n, 1), 6)
            if dirty / max(n, 1) > MAX_ADJ_DIRTY:
                out.append(issue(
                    CRITICAL, "PRICE_ADJ_DIRTY",
                    f"market/daily 混入复权价：非分币精度 {dirty}/{n} = {dirty/max(n,1):.2%}",
                    fix="用 refresh_daily_unadjusted.py 重拉当日未复权价",
                    date=str(t.date())))
            # 指数行污染
            idx_rows = int(px["code"].astype(str).str.startswith("sh.000").sum())
            stats["index_rows"] = idx_rows
            if idx_rows:
                out.append(issue(
                    CRITICAL, "PRICE_INDEX_ROWS",
                    f"market/daily 混入指数行 {idx_rows} 行（sh.000xxx）",
                    fix="指数应只在 index_daily/；重拉当日行情",
                    date=str(t.date())))
        except Exception as e:
            out.append(issue(WARNING, "PRICE_PROBE_FAIL", f"行情卫生检查异常: {e}"))

    bp = BASIC_DIR / f"{t:%Y-%m-%d}.parquet"
    if bp.exists():
        try:
            ncol = len(pd.read_parquet(bp).columns)
            stats["daily_basic_cols"] = ncol
            if ncol < BASIC_MIN_COLS:
                out.append(issue(
                    CRITICAL, "BASIC_FEW_COLS",
                    f"daily_basic 只有 {ncol} 列（应 ≥{BASIC_MIN_COLS}），估值因子缺失",
                    fix="用 patch_daily_basic.py 补齐残缺列",
                    date=str(t.date())))
        except Exception as e:
            out.append(issue(WARNING, "BASIC_PROBE_FAIL", f"daily_basic 检查异常: {e}"))

    ap = ADJ_DIR / f"{t:%Y-%m-%d}.parquet"
    if mp.exists() and ap.exists():
        try:
            px_codes = set(pd.read_parquet(mp, columns=["code"])["code"])
            adj_codes = set(pd.read_parquet(ap, columns=["code"])["code"])
            cov = len(px_codes & adj_codes) / max(len(px_codes), 1)
            stats["adj_coverage"] = round(cov, 6)
            if cov < MIN_ADJ_COVERAGE:
                out.append(issue(
                    WARNING, "ADJ_LOW_COVERAGE",
                    f"adj_factor 覆盖率 {cov:.2%} < {MIN_ADJ_COVERAGE:.0%}"
                    f"（缺 {len(px_codes - adj_codes)} 只）",
                    fix="重跑 fetch_price_basis.py，检查北交所代码映射",
                    date=str(t.date())))
        except Exception as e:
            out.append(issue(WARNING, "ADJ_PROBE_FAIL", f"复权因子检查异常: {e}"))

    return out, stats


# ── 情绪数据（批次2，2026-09-24 接入）──
SENTI_DIR = DATA_DIR / "sentiment"
SENTI_DAILY = SENTI_DIR / "sentiment_daily.parquet"
# 这四个当日 18:30 即可取到（2026-10-23 实测：22:02 查当日均有行）
SENTI_SAME_DAY = ["top_list", "top_inst", "block_trade", "hsgt_top10"]
# hk_hold（沪深港通持股，2024-08-19 后只剩港股通 exchange=HK）实测是 **T+1 就绪**：
#   · 2026-10-23 22:02 实测：查 20261023 → 0 行；查 20261022 → 1017 行
#   · 本地最近 60 个文件 (写入日 − 数据日) 最小滞后就是 **1 天**，从未出现 0 天
# 原先把它误列入 SENTI_SAME_DAY，导致每晚都因「缺当日文件」报 CRITICAL ——
# 2026-10-12 ~ 10-23 连续 11 个交易日误报，把真告警彻底淹掉了（狼来了）。
# 港股休市日 fetch_sentiment 会落「空文件」，故这里只查文件在不在、不查行数。
SENTI_T1 = ["hk_hold"]
MARGIN_BAL_RANGE = (0.3e12, 6e12)   # 两融余额合理区间（元）
BLOCK_AMT_RANGE_YI = (5.0, 200.0)   # 大宗成交额日中位数合理区间（亿元，A股口径）


def check_sentiment(t: pd.Timestamp):
    """1c. 情绪数据（两融/龙虎榜/大宗/北向）—— 批次2 新增。

    关注点与行情不同：
      · margin_detail 是 **T+1 发布**，当日 19:30 必然拿不到当日值 → 查 t-1 即可，
        否则会天天误报。
      · 北向三接口在 2024-08-19 有披露口径断点（hk_hold 只剩港股通），
        空值合法性由 check_sentiment.py 用 hk_trade_cal 判定，这里只查文件在不在。
    """
    out, stats = [], {}
    if not SENTI_DIR.exists():
        out.append(issue(
            WARNING, "SENTI_DIR_MISSING",
            f"情绪数据目录不存在: {SENTI_DIR}",
            fix="跑 scripts/fetch_sentiment.py --full 建库",
            date=str(t.date())))
        return out, stats

    def nrows(p):
        try:
            import pyarrow.parquet as pq
            return pq.ParquetFile(p).metadata.num_rows
        except Exception:
            return -1

    for name in SENTI_SAME_DAY:
        p = SENTI_DIR / name / f"{t:%Y-%m-%d}.parquet"
        if not p.exists():
            out.append(issue(
                CRITICAL, "SENTI_MISSING",
                f"{name} 缺当日文件 {p.name}",
                fix="确认 18:30 stock-doctor 链里的 fetch_sentiment 已执行",
                date=str(t.date())))
        elif nrows(p) == 0:
            stats[f"{name}_empty"] = True   # 空值合法性由 check_sentiment.py 判定

    # hk_hold：T+1 就绪，查前一交易日即可（港股休市日由 fetch_sentiment 落空文件）
    pt = prev_trade_day(t)
    for name in SENTI_T1:
        hp = SENTI_DIR / name / f"{pt:%Y-%m-%d}.parquet"
        if not hp.exists():
            out.append(issue(
                WARNING, "SENTI_LAG",
                f"{name} 缺 {pt:%Y-%m-%d}（该接口 T+1 就绪，实际滞后 2 日以上）",
                fix="跑 fetch_sentiment.py --days 7 自愈；"
                    "若持续缺失，确认同花顺/港交所当日是否休市",
                date=str(t.date())))
        elif nrows(hp) == 0:
            stats[f"{name}_empty"] = True   # 港股休市日合法为空

    # 两融：T+1（margin_detail 明细 + margin_summary 官方汇总，同源同发布节奏）
    for nm in ("margin_detail", "margin_summary"):
        mp = SENTI_DIR / nm / f"{pt:%Y-%m-%d}.parquet"
        if not mp.exists() or nrows(mp) == 0:
            out.append(issue(
                WARNING, "SENTI_MARGIN_LAG",
                f"{nm} 缺 {pt:%Y-%m-%d}（两融 T+1 发布，滞后 2 日以上）",
                fix="跑 fetch_sentiment.py --days 7 自愈",
                date=str(t.date())))
        elif nm == "margin_summary":
            # 结构行数校验（2026-09-24 新增）：`margin` 是交易所分批发布，
            # **深交所晚于上交所**。实测当日只有 2 行（BSE+SSE）就落盘 → margin_bal
            # 从 2.66 万亿假崩到 1.37 万亿（−48.5%），而 1.37 万亿恰好落在
            # MARGIN_BAL_RANGE 内 → **量级哨兵也抓不到**。只有查行数结构才抓得到。
            # 判据：2023-02-13（北交所两融开通）前应 2 行，之后应 3 行。
            need = 3 if pt >= pd.Timestamp("2023-02-13") else 2
            got = nrows(mp)
            if 0 < got < need:
                out.append(issue(
                    CRITICAL, "SENTI_MARGIN_THIN",
                    f"margin_summary {pt:%Y-%m-%d} 仅 {got} 行（应 ≥{need} 行）"
                    f"——疑似交易所尚未发布齐，该日两融余额会明显偏低",
                    fix="删掉该残缺文件后跑 fetch_sentiment.py --days 7"
                        "（抓取器已改为「行数不足不落盘」，此告警表示历史遗留坏文件）",
                    date=str(t.date())))

    # 同类的结构残缺还有 hsgt_top10：**固定 20 行**（10 沪 + 10 深），
    # 实测 1044 个非空文件行数取值只有一种，且 2024-08-19 北向披露口径变化后仍是 20。
    # 行数少于 20 说明只发布了半个市场（沪或深），量级会直接腰斩而"文件在"检查看不见。
    hp = SENTI_DIR / "hsgt_top10" / f"{pt:%Y-%m-%d}.parquet"
    if hp.exists():
        got = nrows(hp)
        if 0 < got < 20:
            out.append(issue(
                CRITICAL, "SENTI_HSGT_THIN",
                f"hsgt_top10 {pt:%Y-%m-%d} 仅 {got} 行（应 ≥20 行=10沪+10深）"
                f"——疑似只发布了单边市场",
                fix="删掉该残缺文件后跑 fetch_sentiment.py --days 7",
                date=str(t.date())))

    if SENTI_DAILY.exists():
        try:
            sd = pd.read_parquet(SENTI_DAILY)
            if len(sd):
                sd["date"] = pd.to_datetime(sd["date"])
                mx = sd["date"].max()
                stats["sentiment_daily_max"] = str(mx.date())
                stats["sentiment_daily_rows"] = len(sd)
                if mx < t:
                    out.append(issue(
                        WARNING, "SENTI_DAILY_STALE",
                        f"情绪汇总表最新只到 {mx:%Y-%m-%d}，落后目标日 {t:%Y-%m-%d}",
                        fix="跑 scripts/build_sentiment_daily.py --days 60",
                        date=str(t.date())))
                # 量级探针：单位/口径跳变会让两融余额离谱
                bal = sd["margin_bal"].dropna()
                if len(bal):
                    stats["margin_bal"] = float(bal.iloc[-1])
                    if not (MARGIN_BAL_RANGE[0] <= bal.iloc[-1] <= MARGIN_BAL_RANGE[1]):
                        out.append(issue(
                            CRITICAL, "SENTI_MARGIN_SCALE",
                            f"两融余额 {bal.iloc[-1]:.3e} 超出合理区间（单位/口径可能变了）",
                            fix="margin_bal 取自官方汇总 margin_summary（含北交所）；"
                                "检查该表是否只拉到部分交易所",
                            date=str(t.date())))
                # 量级探针：block_trade 里股票(万元)与债券(元)单位不同，
                # 未按证券类型筛分时 2020 日均会虚高 ~1 万倍（2026-09-24 踩过，静默无报错）
                if "block_amt" in sd.columns:
                    ba = sd["block_amt"].dropna()
                    if len(ba):
                        med_yi = float(ba.median()) / 1e8
                        stats["block_amt_median_yi"] = round(med_yi, 2)
                        if not (BLOCK_AMT_RANGE_YI[0] <= med_yi <= BLOCK_AMT_RANGE_YI[1]):
                            out.append(issue(
                                CRITICAL, "SENTI_BLOCK_SCALE",
                                f"大宗成交额中位 {med_yi:.1f} 亿超出合理区间 {BLOCK_AMT_RANGE_YI}"
                                "（疑似证券类型单位混用：股票万元 vs 债券元）",
                                fix="跑 scripts/build_sentiment_daily.py --full（只取 A 股）",
                                date=str(t.date())))
        except Exception as e:
            out.append(issue(WARNING, "SENTI_DAILY_FAIL", f"情绪汇总表检查异常: {e}"))
    else:
        out.append(issue(
            WARNING, "SENTI_DAILY_MISSING",
            f"情绪汇总表不存在: {SENTI_DAILY}",
            fix="跑 scripts/build_sentiment_daily.py --full",
            date=str(t.date())))

    return out, stats


def expected_industries():
    """期望行业全集 = 静态成员快照的 ind_code（长期稳定的 131 个）"""
    m = pd.read_parquet(MEMBERS_PATH, columns=["ind_code"])
    return set(m["ind_code"].unique())


def check_weighted_returns(t: pd.Timestamp, expected: set):
    """2. 加权涨跌幅：整行缺失 + stock_n 骤降"""
    out, stats = [], {}
    if not WR_PATH.exists():
        return [issue(CRITICAL, "WR_MISSING", "industry_weighted_returns.parquet 不存在",
                      fix="运行 scripts/compute_weighted_returns.py --days 5")], {}

    wr = pd.read_parquet(WR_PATH, columns=["date", "ind_code", "stock_n", "circ_mv"])
    day = wr[wr["date"] == t]
    if day.empty:
        out.append(issue(CRITICAL, "WR_NO_DATE", f"加权涨跌幅无 {t:%Y-%m-%d} 任何数据",
                         fix="运行 scripts/compute_weighted_returns.py --days 5",
                         latest_date=str(wr["date"].max().date())))
        return out, {"industries": 0}

    actual = set(day["ind_code"])
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    stats = {"expected_n": len(expected), "actual_n": len(actual),
             "missing_n": len(missing), "missing": missing}

    if missing:
        detail = ind_names(missing[:20])
        out.append(issue(
            CRITICAL, "WR_MISSING_INDUSTRY",
            f"{len(missing)}/{len(expected)} 个行业在 {t:%Y-%m-%d} 无行",
            fix="检查 industry_members_daily 当日快照是否完整；重跑 compute_weighted_returns.py",
            **{"count": len(missing), "industries": detail}))
    if extra:
        out.append(issue(
            WARNING, "WR_EXTRA_INDUSTRY",
            f"{len(extra)} 个行业不在期望全集内（成员快照已更新？）",
            industries=ind_names(extra[:20])))

    # circ_mv 异常
    bad_mv = day[day["circ_mv"] <= 0]
    if len(bad_mv):
        out.append(issue(CRITICAL, "WR_BAD_CIRCMV",
                         f"{len(bad_mv)} 个行业流通市值 ≤ 0（市值数据异常）",
                         industries=bad_mv["ind_code"].tolist()[:20]))

    # stock_n 骤降（相对自身历史中位数）
    med = wr.groupby("ind_code")["stock_n"].median()
    drops = []
    for _, r in day.iterrows():
        m = med.get(r["ind_code"])
        if m and m >= STOCK_N_MIN_MEDIAN and r["stock_n"] < m * STOCK_N_DROP_RATIO:
            drops.append({"ind_code": r["ind_code"], "ind_name": ind_names([r["ind_code"]]).get(r["ind_code"], "?"),
                          "stock_n": int(r["stock_n"]), "median": int(m)})
    if drops:
        drops.sort(key=lambda x: x["stock_n"] / x["median"])
        out.append(issue(
            WARNING, "WR_STOCKN_DROP",
            f"{len(drops)} 个行业当日成分股数骤降（< 自身历史中位数 50%）",
            fix="多为单日数据面缺股（停牌/同步不全），确认后可重跑",
            industries=drops[:10]))
    return out, stats


def ind_names(codes):
    """ind_code -> ind_name（尽力而为）"""
    if not codes:
        return {}
    try:
        m = pd.read_parquet(MEMBERS_PATH, columns=["ind_code", "ind_name"])
        mp = m.drop_duplicates("ind_code").set_index("ind_code")["ind_name"].to_dict()
        return {c: mp.get(c, "?") for c in codes}
    except Exception:
        return {c: "?" for c in codes}


def check_members_daily(t: pd.Timestamp, expected: set):
    """3. 每日成分快照：当日缺失 + 总成分数骤降"""
    out, stats = [], {}
    if not MEMBERS_DAILY_PATH.exists():
        return [issue(CRITICAL, "MD_MISSING", "industry_members_daily.parquet 不存在",
                      fix="运行 scripts/fetch_sw_members.py && scripts/build_members_daily.py")], {}

    md = pd.read_parquet(MEMBERS_DAILY_PATH, columns=["date", "ind_code", "stock_code"])
    cur = md[md["date"] == t]
    if cur.empty:
        out.append(issue(CRITICAL, "MD_NO_DATE", f"每日成分快照无 {t:%Y-%m-%d}",
                         fix="运行 scripts/build_members_daily.py",
                         latest_date=str(md["date"].max().date())))
        return out, {}

    ind_cur = set(cur["ind_code"])
    miss = sorted(expected - ind_cur)
    n_today = len(cur)
    stats = {"members_total": n_today, "industries": len(ind_cur)}
    if miss:
        out.append(issue(CRITICAL, "MD_MISSING_INDUSTRY",
                         f"{len(miss)}/{len(expected)} 个行业当日无成分快照",
                         fix="运行 scripts/build_members_daily.py",
                         industries=ind_names(miss[:20])))

    prev = prev_trade_day(t)
    if prev is not None:
        prev_n = len(md[md["date"] == prev])
        if prev_n and n_today < prev_n * MEMBERS_DROP_RATIO:
            out.append(issue(
                WARNING, "MD_MEMBERS_DROP",
                f"当日全市场成分股数 {n_today} 较前一交易日 {prev_n} 骤降 "
                f"({n_today / prev_n:.1%})",
                fix="确认 Tushare index_member_all 拉取是否完整",
                prev_date=str(prev.date()), prev_n=prev_n, today_n=n_today))
    return out, stats


def check_nav(t: pd.Timestamp):
    """4. 净值主表：是否刷新到目标日 + 关键列空洞"""
    out, stats = [], {}
    if not NAV_PATH.exists():
        return [issue(WARNING, "NAV_MISSING", "industry_nav_924.parquet 不存在",
                      fix="运行 scripts/build_nav_924.py")], {}

    nav = pd.read_parquet(NAV_PATH, columns=["date", "ind_code"] + NAV_COLS)
    last = nav["date"].max()
    stats["latest"] = str(last.date())
    if last < t:
        out.append(issue(
            WARNING, "NAV_STALE", f"净值主表最新仅 {last:%Y-%m-%d}，未覆盖 {t:%Y-%m-%d}",
            fix="运行 scripts/build_nav_924.py（建议加入日更链）", latest_date=str(last.date())))
        return out, stats

    day = nav[nav["date"] == t]
    stats["industries"] = len(day)
    for col in NAV_COLS:
        n_nan = int(day[col].isna().sum())
        if n_nan:
            lvl = CRITICAL if n_nan > len(day) * 0.9 else WARNING
            hint = ""
            if col == "nav_e":
                hint = "（等权源 industry_daily_full 滞后所致）"
            out.append(issue(
                lvl, f"NAV_NULL_{col.upper()}",
                f"净值主表 {t:%Y-%m-%d} 的 {col} 有 {n_nan}/{len(day)} 个空值{hint}",
                fix="先补齐上游（等权源 build_industry_daily_full.py），再重跑 build_nav_924.py",
                column=col, null_n=n_nan, total=len(day)))
    return out, stats


def check_daily_full(t: pd.Timestamp):
    """5. 等权源时效（nav_e 的上游）"""
    if not FULL_PATH.exists():
        return [issue(WARNING, "FULL_MISSING", "industry_daily_full.parquet 不存在",
                      fix="运行 scripts/build_industry_daily_full.py")], {}
    df = pd.read_parquet(FULL_PATH, columns=["date"])
    last = pd.to_datetime(df["date"]).max()
    if last < t:
        return [issue(
            WARNING, "FULL_STALE",
            f"等权源 industry_daily_full 最新 {last:%Y-%m-%d}，落后目标日 {t:%Y-%m-%d}",
            fix="运行 scripts/build_industry_daily_full.py（否则 nav_e 会成列空洞）",
            latest_date=str(last.date()))], {"latest": str(last.date())}
    return [], {"latest": str(last.date())}


def check_breadth(t: pd.Timestamp):
    """5b. 宽度异常探针 —— **数据探针，不是信号**（2026-10-05 新增）。

    为什么需要：P0-2（2026-07-01~07-13 全 131 行业 breadth 精确为 0）曾长期无人发现，
    因为它既不改文件存在性、也不改行数。本检查把「宽度物理上不可能的值」变成可告警事件。

    两条判据（**均为 WARNING** —— 本检查参与退出码，不允许把正常行情日变成失败）：
      ① `BREADTH_ZERO`：目标日**全行业宽度均值恰为 0** ⇒ 物理上不可能
          （要求 131 个行业里没有任何一只股票站上自己的 MA20），是 P0-2 的特征值。
          ⚠️ 这条是**确定性**判据，必须有 —— 实测 P0-2 的**起点**（0.223 → 0.000）
          只有 −0.22，纯 |Δ| 阈值抓不到它，只能抓到 9 天后的「恢复跳变」。
      ② `BREADTH_JUMP`：目标日 |Δ| > K × 稳健尺度（1.4826×MAD，回看 250 个日变化）。
          实测校准（2020-2026 全历史 1,635 个日变化）：稳健尺度 0.0669，
          K=6 ⇒ 阈值 0.40，**触发 2 天（0.1%）**：2020-02-28（新冠暴跌，真实行情）
          与 2026-07-14（P0-2 的恢复日）。K=5 会触发 6 天（0.4%，多为真实行情）。
    """
    if not FULL_PATH.exists():
        return [], {"skipped": "industry_daily_full 不存在"}
    try:
        df = pd.read_parquet(FULL_PATH, columns=["date", "con_code", "breadth", "n_stocks"])
        df["date"] = pd.to_datetime(df["date"])
        df["breadth"] = pd.to_numeric(df["breadth"], errors="coerce")
        df = df[pd.to_numeric(df["n_stocks"], errors="coerce").fillna(0) >= 10]
        agg = df.groupby("date")["breadth"].mean().dropna().sort_index()
        if t not in agg.index:
            return [], {"skipped": f"宽度表未覆盖 {t:%Y-%m-%d}"}

        cur = float(agg.loc[t])
        stats = {"breadth": round(cur, 4)}

        # ① 零值判据（确定性）
        if cur == 0.0:
            return [issue(
                WARNING, "BREADTH_ZERO",
                f"{t:%Y-%m-%d} 全行业宽度均值恰为 0（131 个行业无一只个股站上 MA20，物理上不可能）",
                fix="疑似 P0-2 类静默坏值：运行 scripts/build_industry_daily_full.py --force-overwrite 重算，"
                    "并检查 market/daily 该段是否已回填",
                breadth=cur)], stats

        # ② 跳变判据（稳健尺度，回看窗口不含目标日）
        d = agg.diff().dropna()
        pos = d.index.get_loc(t)
        hist = d.iloc[max(0, pos - BREADTH_MAD_WINDOW): pos]
        if len(hist) < BREADTH_MAD_MIN:
            stats["jump_skipped"] = f"历史仅 {len(hist)} 个日变化"
            return [], stats
        mad = float((hist - hist.median()).abs().median())
        scale = 1.4826 * mad
        delta = float(d.iloc[pos])
        thr = BREADTH_JUMP_K * scale
        stats.update({"delta": round(delta, 4), "threshold": round(thr, 4)})
        if scale > 0 and abs(delta) > thr:
            return [issue(
                WARNING, "BREADTH_JUMP",
                f"{t:%Y-%m-%d} 全市场宽度单日变化 {delta:+.3f}，超过稳健阈值 {thr:.3f}"
                f"（{BREADTH_JUMP_K:.0f}×1.4826×MAD={scale:.4f}，回看 {len(hist)} 日）",
                fix="先判「真实行情」还是「数据事故」：查 market/daily 该日覆盖与 breadth 是否退化；"
                    "若是坏值用 --force-overwrite 重算",
                breadth=round(cur, 4), delta=round(delta, 4), threshold=round(thr, 4))], stats
        return [], stats
    except Exception as e:
        return [issue(WARNING, "BREADTH_PROBE_FAIL", f"宽度异常检查异常: {e}")], {}


STYLE_PROBE_PATH = INDUSTRY_DIR / "style_probe_status.json"
STYLE_PROBE_MAX_LAG_DAYS = 5   # 探针状态落后目标日超过此天数 ⇒ 视为没跑（不是"通过"）


def check_style_probe(t: pd.Timestamp):
    """5c. **风格调整的退化探针**状态（review §12.5，2026-10-07 接入）。

    为什么进完备性检查：风格调整一旦失效，**故障不是变垃圾，而是悄悄退化成原始值**
    —— 报表照常出数、看着很健康（BREADTH_ZERO 的同型病）。只写日志不算，必须**可见**。

    三条判据（均为 **WARNING**，与 PROV_MISSING 同级 ⇒ 参与退出码、但不把正常日变失败）：
      ① 状态文件缺失 / 落后 ⇒ 说明测量层没跑（**没跑 ≠ 通过**）；
      ② `ok=false` ⇒ 探针判定退化 ⇒ 日报必须带「未调整（因子面板不可用）」标注；
      ③ 只统计 warning 级 issue 时不判失败（它们是"需要看一眼"，不是"数据错了"）。
    """
    if not STYLE_PROBE_PATH.exists():
        return [issue(
            WARNING, "STYLE_PROBE_MISSING",
            "风格调整探针状态不存在（industry/style_probe_status.json）"
            " ⇒ 未跑 scripts/measure_chain_resonance.py；**没跑不等于通过**",
            fix="在日更链里跑 scripts/measure_chain_resonance.py（它会同时产出探针状态）",
            date=str(t.date()))], {"status": "missing"}
    try:
        st = json.loads(STYLE_PROBE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return [issue(WARNING, "STYLE_PROBE_UNREADABLE",
                     f"探针状态解析失败: {e}", fix="重跑 scripts/measure_chain_resonance.py",
                     date=str(t.date()))], {"status": "unreadable"}

    stats = {"status": "ok" if st.get("ok") else "degraded",
             "target_date": st.get("target_date"),
             "n_issues": len(st.get("issues") or [])}
    out = []
    td = st.get("target_date")
    if td:
        lag = (t - pd.Timestamp(td)).days
        stats["lag_days"] = lag
        if lag > STYLE_PROBE_MAX_LAG_DAYS:
            out.append(issue(
                WARNING, "STYLE_PROBE_STALE",
                f"探针状态停在 {td}，落后目标日 {lag} 天（>{STYLE_PROBE_MAX_LAG_DAYS}）",
                fix="重跑 scripts/measure_chain_resonance.py",
                date=str(t.date())))
    if not st.get("ok"):
        codes = [i.get("code") for i in (st.get("issues") or [])]
        out.append(issue(
            WARNING, "STYLE_PANEL_DEGRADED",
            f"风格调整探针判定**退化**（{', '.join(codes)}）"
            f" ⇒ 共振强度回落为**未调整值**，日报正文必须标注"
            f"「未调整（因子面板不可用）」，且**不得用于归因/链假设**",
            fix="先查因子面板覆盖与新鲜度（面板故障的典型形态是'悄悄退化成原始值'，"
                "不是变垃圾）；修好后重跑",
            date=str(t.date()), codes=codes))
    return out, stats


# ─────────────────────── 产物指纹（provenance）───────────────────────
#: 自指排除：监控脚本**自己写**的状态快照不进 producer-dirty 判定。
#: 否则链条是「改监控脚本 → 它自己产出的状态文件脏 → 监控报自己脏」，
#: 于是只要还在调监控就永久红灯 —— 正是本函数要治的那种病。
#: 它是运行状态快照（每次运行重写），不是数据产物，不承担复现语义。
PROV_SELF_EXCLUDE = {"scripts/check_data_completeness.py"}


def check_provenance():
    """6. 已登记产物是否带 <文件名>.meta.json 指纹，且**生产脚本本身**可复现。

    判定规则（对应 src/paths.py:ARTIFACTS 登记表，唯一真相来源）：
      · **derived** 产物存在但缺指纹 → WARNING：计算结果的复现 spec（params/窗口/因子集）
        丢了，以后无法说明「这个数是怎么算出来的」，属真缺陷。
      · **fetched** 产物**不查指纹**：它是数据源快照而非计算结果，本身没有算法参数可复现
        —— 重下即得（且源端早已变化）。要求它带复现指纹没有意义，只会制造永久告警。
      · **PROV_PRODUCER_DIRTY** → WARNING：产物带指纹，但**产出它的那个脚本本身**
        有未提交改动（判据来自 meta.producer_state，逐脚本归因）。这是真风险
        —— 产物无法由任何 commit 复现，且脚本随时可能被下一次改动覆盖。
        可行动：提交该脚本后重跑（或明确接受"这就是当天的临场修复"）。
      · `commit≠HEAD` → 只进 summary：每次提交都会让存量产物漂移，属正常信息。

    🔴 2026-10-23 修正（原实现的两个错误，勿回退）
    -------------------------------------------------
    1. **归因错**：原先把 `git status` 的**工作区级** dirty 当成产物属性，只要工作区
       有任何改动，全部新产物都被判「不可复现」。实测 21 个 dirty 产物里，只有 2 个的生产
       脚本真的被改过 —— 其余 19 个是归因错误（生产脚本一行没动）。
       → 改为读 meta.producer_state（逐脚本 git status + 脚本 sha256）。
    2. **处置错**：当晚为了消掉这条"天天为真"的告警，把它整条降级成"只进 summary"。
       那是**治标** —— 把报警器关掉不等于修好管道：从此「产物出自未提交代码」这个
       真风险彻底不可见。正确处置是修归因，让它在**真脏**时才响（现在就是）。
       当晚的错误论证也一并记下：曾用「它没抓到 daily_basic 行齐列空」来否定它 ——
       这是**范畴错误**，provenance 查的是"数怎么算出来的"，不查"数值对不对"。
       数值类故障归 check_hygiene 的列级覆盖与 check_style_probe，各司其职。
    """
    head = (git_info() or {}).get("commit")
    out, missing, dirty, ok, stale, legacy = [], [], [], [], [], []
    for name, (path, kind) in sorted(ARTIFACTS.items()):
        if not Path(path).exists():
            continue
        meta = load_meta(path)
        if meta is None:
            if kind == "derived":
                missing.append(name)
            continue
        ok.append(name)
        ps = meta.get("producer_state")
        if isinstance(ps, dict) and ps.get("dirty") \
                and ps.get("path") not in PROV_SELF_EXCLUDE:
            dirty.append(f"{name}←{ps.get('path')}")
        elif not ps:
            # 接入 producer_state 之前的存量指纹：只有工作区级 dirty，无法归因
            if (meta.get("git") or {}).get("dirty"):
                legacy.append(name)
        if (meta.get("git") or {}).get("commit") and head \
                and meta["git"]["commit"] != head:
            stale.append(name)

    if missing:
        out.append(issue(
            WARNING, "PROV_MISSING",
            f"{len(missing)} 个**派生**产物缺指纹(.meta.json): {', '.join(missing)}",
            fix="重跑对应脚本即可补齐（派生产物应走 src/utils/provenance:save_dataframe）；"
                "若重跑后仍缺，说明该产物由未登记的写入方产出，请确认它是否应登记为 fetched",
            artifacts=missing))
    if dirty:
        out.append(issue(
            WARNING, "PROV_PRODUCER_DIRTY",
            f"{len(dirty)} 个产物的**生产脚本本身**有未提交改动（产物不可由 commit 复现）: "
            f"{', '.join(dirty)}",
            fix="提交对应脚本后重跑该步；若属当日临场修复，请在提交信息里写明并接受该产物作废重跑",
            artifacts=dirty))
    src_bad = shared_code_dirty()
    if src_bad:
        out.append(issue(
            WARNING, "PROV_SRC_DIRTY",
            f"共享库 src/ 有 {len(src_bad)} 个已跟踪文件未提交（{', '.join(src_bad[:6])}"
            f"{' …' if len(src_bad) > 6 else ''}） ⇒ **所有**派生结果的复现 spec 都受影响",
            fix="提交 src/ 改动（共享库一改，逐产物归因就不再必要 —— 这是全局面板级问题，只报一条）",
            paths=src_bad))

    stats = {"checked": len(ok) + len(missing), "with_meta": len(ok),
             "missing": len(missing), "producer_dirty": len(dirty),
             "shared_code_dirty": len(src_bad),
             "legacy_unattributed": len(legacy),
             "code_drift": stale, "current_commit": head}
    return out, stats


def audit_unregistered_writes():
    """审计「未登记写入」：派生目录里既不是登记产物、也不是其指纹的数据文件。

    这是「谁在往数据根写东西」的探针 —— 历史教训：signals/industry_margin_daily.parquet
    与 factors/stock/*.parquet 曾长期 owner 不明。不参与日更退出码，人工调用。
    """
    known = set()
    for path, _kind in ARTIFACTS.values():
        p = Path(path)
        known.add(p)
        known.add(sidecar_path(p))

    exts = {".parquet", ".csv", ".tsv", ".json", ".py"}
    groups: dict = {}
    for d in DERIVED_DIRS:
        d = Path(d)
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            if (not f.is_file() or f.name.startswith(".")
                    or f.name.endswith(SIDECAR_SUFFIX)
                    or f.suffix.lower() not in exts
                    or f in known):
                continue
            groups.setdefault(str(f.parent), []).append(f)
    return groups


def print_audit_report(groups: dict, samples: int = 6):
    """打印未登记写入审计结果（按目录归组，供人工清理 / 补齐 ARTIFACTS 登记表）。"""
    line = "═" * 62
    print(line)
    print("  数据根「未登记写入」审计")
    print(line)
    if not groups:
        print("  ✅ 派生目录内没有未登记的数据文件")
        return
    total = sum(len(v) for v in groups.values())
    root = str(DATA_DIR)
    print(f"  共 {total} 个文件：既不在 ARTIFACTS 登记表内，也没有 <文件名>.meta.json 指纹")
    for d, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        size = sum(f.stat().st_size for f in items if f.exists())
        print(f"\n  ▸ {d.replace(root, '<数据根>')}  （{len(items)} 个 / {size / 1e6:.1f} MB）")
        for f in items[:samples]:
            print(f"      {f.name:<46} {f.stat().st_size:>12,} B")
        if len(items) > samples:
            print(f"      … 另有 {len(items) - samples} 个")
    print("\n  处置：① 属产物的 → 登记到 src/paths.py:ARTIFACTS 并接入指纹；")
    print("        ② 属原料/外部抓取的 → 按 fetched 登记；③ 属历史遗留的 → 归档到 _archive/。")


# ─────────────────── 告警台账（防「永久红灯」）───────────────────
# 为什么需要它：本项目已经出过两次同类事故 ——
#   ① hk_hold 被当"当日可取"，实际 T+1 就绪（1647/1647 个文件全部滞后 ≥1 天），
#      规则**结构上不可满足** ⇒ 连续 11 个交易日 CRITICAL ⇒ 真告警被彻底淹掉；
#   ② PROV_DIRTY 取工作区级 dirty，21 个产物里 19 个归因错误 ⇒ 天天为真 ⇒ 被关掉。
# 两次的处置都只动了"单条规则"，没动"为什么会存在一条永久为真的规则"。
# 治本的做法是让**规则自身可被审计**：记录每次运行的 issue 码，定期看触发率，
# ≥80% 的规则一律按"设计缺陷"复核（口径写错 / 归因错 / 不可行动），而不是继续调参。
HISTORY_PATH = INDUSTRY_DIR / "completeness_history.jsonl"
ALARM_BUDGET_WINDOW = 20
ALARM_BUDGET_RED = 0.80     # 触发率 ≥80% ⇒ 疑似永久红灯（设计缺陷）
ALARM_BUDGET_AMBER = 0.30   # 30%~80% ⇒ 需要盯
ALARM_BUDGET_MIN_RUNS = 5   # 少于 5 次不下判定（1 次运行 100% 说明不了任何事）


def _append_history(report: dict) -> None:
    """追加一行运行历史（同一 target_date 相同结论只记一次，保留当天最后状态）。"""
    codes = sorted([[i.get("level"), i.get("code")] for i in report.get("issues", [])])
    rec = {"ts": report.get("checked_at"), "target_date": report.get("target_date"),
           "exit_code": report.get("exit_code"), "status": report.get("status"),
           "codes": codes}
    try:
        if HISTORY_PATH.exists():
            lines = HISTORY_PATH.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                prev = json.loads(lines[-1])
                if (prev.get("target_date") == rec["target_date"]
                        and prev.get("exit_code") == rec["exit_code"]
                        and prev.get("codes") == codes):
                    return
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ 告警历史写入失败（不影响检查结论）: {e}")


def print_alarm_budget(window: int = ALARM_BUDGET_WINDOW) -> None:
    """每条规则最近 N 次运行的触发率 + 判定。读的是 completeness_history.jsonl。"""
    if not HISTORY_PATH.exists():
        print(f"（无历史台账：{HISTORY_PATH}；先正常跑一次 check_data_completeness.py）")
        return
    recs = []
    for ln in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            recs.append(json.loads(ln))
        except Exception:
            continue
    recs = recs[-window:]
    n = len(recs)
    if not n:
        print("（历史台账为空）")
        return
    hit: dict = {}
    for r in recs:
        for lv, code in r.get("codes") or []:
            key = (lv, code)
            hit[key] = hit.get(key, 0) + 1
    print(f"告警触发率台账（最近 {n} 次运行 {recs[0].get('target_date')} → "
          f"{recs[-1].get('target_date')}）")
    print(f"{'触发率':>7}  {'次数':>5}  判定        规则")
    print("-" * 72)
    for (lv, code), c in sorted(hit.items(), key=lambda kv: -kv[1]):
        rate = c / n
        if n < ALARM_BUDGET_MIN_RUNS:
            verdict = "… 样本不足"
        elif rate >= ALARM_BUDGET_RED:
            verdict = "🔴 设计缺陷嫌疑"
        elif rate >= ALARM_BUDGET_AMBER:
            verdict = "🟠 需盯"
        else:
            verdict = "🟢 偶发"
        print(f"{rate:>6.0%}  {c:>5}  {verdict:<12} {lv}/{code}")
    if n < ALARM_BUDGET_MIN_RUNS:
        print(f"\n（样本 {n} 次 < {ALARM_BUDGET_MIN_RUNS} 次，暂不下判定）")
        return
    red = [k for k, c in hit.items() if c / n >= ALARM_BUDGET_RED]
    if red:
        print("\n⚠️ 上面标 🔴 的规则：请先复核**口径/归因是否正确**，不要先降级或加白名单。"
              "\n   本项目两次永久红灯（hk_hold 当日假设、PROV_DIRTY 全局 dirty）"
              "都是这么来的。")
    else:
        print("\n✅ 无永久红灯嫌疑。")


def main():
    ap = argparse.ArgumentParser(description="行业数据日更完备性监控")
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD，默认取最新行情日")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出结论行")
    ap.add_argument("--no-write", action="store_true", help="不写状态文件")
    ap.add_argument("--audit-writes", action="store_true",
                    help="审计派生目录里的未登记写入（人工用，不参与退出码）")
    ap.add_argument("--alarm-budget", nargs="?", type=int, const=ALARM_BUDGET_WINDOW,
                    default=None,
                    help="统计最近 N 次运行里每条规则的触发率，识别「永久红灯」（人工用）")
    args = ap.parse_args()

    if args.audit_writes:
        print_audit_report(audit_unregistered_writes())
        sys.exit(0)

    if args.alarm_budget is not None:
        print_alarm_budget(args.alarm_budget)
        sys.exit(0)

    t, src = resolve_target_date(args.date)
    report = {"checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "target_date": None, "status": "OK", "exit_code": 0,
              "source": src, "summary": {}, "issues": []}

    def finish(code, status, msg=""):
        report["status"], report["exit_code"] = status, code
        if not args.no_write:
            try:
                save_json(report, STATUS_PATH,
                          producer="scripts/check_data_completeness.py",
                          params={"target_date": report.get("target_date")},
                          extra={"status": report.get("status")})
            except Exception as e:
                print(f"⚠️ 状态文件写入失败: {e}")
            _append_history(report)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_report(report, msg)
        sys.exit(code)

    if t is None:
        report["issues"].append(issue(CRITICAL, "NO_MARKET_DATA", "行情目录无任何文件"))
        finish(1, "CRITICAL")

    report["target_date"] = str(t.date())

    # 非交易日 → 跳过
    td = is_trade_day(t)
    if td is False:
        report["issues"].append(issue(WARNING, "NON_TRADE_DAY", f"{t:%Y-%m-%d} 非交易日，跳过检查"))
        finish(3, "SKIPPED")

    expected = expected_industries()
    report["summary"]["expected_industries"] = len(expected)

    issues = []
    issues += check_sources(t)
    hy_issues, hy_stats = check_hygiene(t)
    issues += hy_issues
    report["summary"]["hygiene"] = hy_stats
    cc_issues, cc_stats = check_col_coverage(t)
    issues += cc_issues
    report["summary"]["col_coverage"] = cc_stats
    se_issues, se_stats = check_sentiment(t)
    issues += se_issues
    report["summary"]["sentiment"] = se_stats
    wr_issues, wr_stats = check_weighted_returns(t, expected)
    issues += wr_issues
    report["summary"]["weighted_returns"] = wr_stats
    md_issues, md_stats = check_members_daily(t, expected)
    issues += md_issues
    report["summary"]["members_daily"] = md_stats
    nav_issues, nav_stats = check_nav(t)
    issues += nav_issues
    report["summary"]["nav"] = nav_stats
    fu_issues, fu_stats = check_daily_full(t)
    issues += fu_issues
    report["summary"]["daily_full"] = fu_stats
    bd_issues, bd_stats = check_breadth(t)
    issues += bd_issues
    report["summary"]["breadth"] = bd_stats
    sp_issues, sp_stats = check_style_probe(t)
    issues += sp_issues
    report["summary"]["style_probe"] = sp_stats
    pv_issues, pv_stats = check_provenance()
    issues += pv_issues
    report["summary"]["provenance"] = pv_stats

    report["issues"] = issues
    n_crit = sum(1 for i in issues if i["level"] == CRITICAL)
    n_warn = sum(1 for i in issues if i["level"] == WARNING)

    if n_crit:
        finish(1, "CRITICAL")
    elif n_warn:
        finish(2, "WARNING")
    else:
        finish(0, "OK")


def print_report(report: dict, msg: str = ""):
    t = report["target_date"]
    icon = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "❌", "SKIPPED": "⏭️"}[report["status"]]
    line = "═" * 62
    if not args_quiet():
        print(line)
        print(f"  行业数据完备性检查 · {t}   [{report['status']}]")
        print(line)
        s = report["summary"]
        if s:
            print(f"  期望行业数: {s.get('expected_industries', '?')}")
            wr = s.get("weighted_returns") or {}
            if wr:
                print(f"  加权涨跌幅: {wr.get('actual_n', '?')}/{wr.get('expected_n', '?')} 行业有行"
                      + (f"  缺 {wr.get('missing_n')}" if wr.get("missing_n") else ""))
            md = s.get("members_daily") or {}
            if md:
                print(f"  成分快照  : {md.get('industries', '?')} 行业 / {md.get('members_total', '?')} 只成分股")
            nv = s.get("nav") or {}
            if nv:
                print(f"  净值主表  : 最新 {nv.get('latest', '?')} / {nv.get('industries', '?')} 行业")
            fu = s.get("daily_full") or {}
            if fu:
                print(f"  等权源    : 最新 {fu.get('latest', '?')}")
            # 批次2 情绪数据（2026-09-24 加）：原先只写进 JSON、终端不显示，
            # 违反「监控必须可见」——数值异常时人看不到，等于没监控。
            se = s.get("sentiment") or {}
            if se:
                bits = []
                if se.get("sentiment_daily_max"):
                    bits.append(f"汇总表 {se['sentiment_daily_max']}"
                                f"({se.get('sentiment_daily_rows', '?')}行)")
                if se.get("margin_bal"):
                    bits.append(f"两融 {se['margin_bal'] / 1e12:.3f}万亿")
                if se.get("block_amt_median_yi") is not None:
                    bits.append(f"大宗中位 {se['block_amt_median_yi']:.1f}亿")
                empty = [k[:-6] for k, v in se.items()
                         if k.endswith("_empty") and v]
                if empty:
                    bits.append("空值: " + ",".join(empty))
                if bits:
                    print("  情绪数据  : " + " / ".join(bits))
            sp = s.get("style_probe") or {}
            if sp:
                bits = [f"状态 {sp.get('status')}"]
                if sp.get("target_date"):
                    bits.append(f"测量日 {sp['target_date']}")
                if sp.get("lag_days") is not None:
                    bits.append(f"滞后 {sp['lag_days']} 天")
                if sp.get("n_issues"):
                    bits.append(f"探针告警 {sp['n_issues']} 条")
                print("  风格探针  : " + " / ".join(bits))
            pv = s.get("provenance") or {}
            if pv.get("checked"):
                extra_txt = ""
                if pv.get("code_drift"):
                    extra_txt += f"  代码漂移 {len(pv['code_drift'])}"
                if pv.get("current_commit"):
                    extra_txt += f"  当前提交 {pv['current_commit']}"
                print(f"  产物指纹  : {pv.get('with_meta', 0)}/{pv.get('checked', 0)} 有指纹"
                      f"（缺 {pv.get('missing', 0)}，脚本脏 {pv.get('producer_dirty', 0)}，"
                      f"src脏 {pv.get('shared_code_dirty', 0)}）{extra_txt}")
    if report["issues"]:
        print()
        for it in report["issues"]:
            mark = "❌" if it["level"] == CRITICAL else "⚠️"
            print(f"  {mark} [{it['code']}] {it['message']}")
            if it.get("fix"):
                print(f"      ↳ 修复: {it['fix']}")
    if not args_quiet():
        print()
        print(line)
    n_c = sum(1 for i in report["issues"] if i["level"] == CRITICAL)
    n_w = sum(1 for i in report["issues"] if i["level"] == WARNING)
    print(f"{icon} 完备性: {report['status']}  （严重 {n_c} / 警告 {n_w}）")
    if msg:
        print(msg)


_ARGS_QUIET = {"v": False}


def args_quiet():
    return _ARGS_QUIET["v"]


if __name__ == "__main__":
    # --quiet 需在 print_report 前生效
    _ARGS_QUIET["v"] = "--quiet" in sys.argv
    main()
