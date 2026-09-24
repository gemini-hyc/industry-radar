#!/usr/bin/env python3
"""
行业数据日更完备性监控 — 防「静默缺数」

日更链路跑完后，检查目标交易日的关键产出是否出现：
  1. 整行缺失 —— 某行业当日完全没有行（会被下游静默忽略）
  2. 列级空洞 —— 行存在但某列整列为空（如 nav_e 因上游滞后而全 NaN）
  3. 成分漂移 —— 某行业当日成分股数相对自身历史骤降（数据面缺股）
  4. 源数据缺位 —— market/daily 或 daily_basic 缺当日文件
  5. 上游滞后 —— 等权源 industry_daily_full 落后于目标日

用法:
    python3 scripts/check_data_completeness.py [--date YYYY-MM-DD]
                                               [--json] [--quiet] [--no-write]

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
from src.paths import MARKET_DIR, BASIC_DIR, INDUSTRY_DIR, DATA_DIR

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
# 这五个当日 18:30 即可取到；margin_detail 是 T+1 发布，只能等次日
SENTI_SAME_DAY = ["top_list", "top_inst", "block_trade", "hsgt_top10", "hk_hold"]
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

    # 两融：T+1（margin_detail 明细 + margin_summary 官方汇总，同源同发布节奏）
    pt = prev_trade_day(t)
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


# ─────────────────────────── 主流程 ───────────────────────────
def main():
    ap = argparse.ArgumentParser(description="行业数据日更完备性监控")
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD，默认取最新行情日")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出结论行")
    ap.add_argument("--no-write", action="store_true", help="不写状态文件")
    args = ap.parse_args()

    t, src = resolve_target_date(args.date)
    report = {"checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "target_date": None, "status": "OK", "exit_code": 0,
              "source": src, "summary": {}, "issues": []}

    def finish(code, status, msg=""):
        report["status"], report["exit_code"] = status, code
        if not args.no_write:
            try:
                STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(STATUS_PATH, "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"⚠️ 状态文件写入失败: {e}")
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
