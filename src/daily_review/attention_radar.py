"""
方向 B · 注意力分配型雷达 —— 日报正文生成（计算层 + 渲染层）

🔴 项目定位（2026-10-14 主人定论）
==================================================================
雷达 = **注意力分配工具**（描述 + 分类 + 异动监控），**不追求横截面预测 alpha**。
依据：02/03/04/05/06/07/08/09 共 9 份研究，景气、估值、两者交互、盈利预期
在申万二级上**全部证伪**；横截面 alpha 判定不成立。

⇒ 本模块**只回答「今天该把注意力放在哪里」**，不产出买卖信号、不承诺前瞻力。
   所有 z 值均为「相对自身历史分布」的描述性统计量，**不是 alpha 因子**。

为什么取代旧三章（2026-10-15 主人拍板「重排 + 不保留」）
==================================================================
旧日报三章 = 主线状态 / 行情 regime / 链共振。它们的问题不是算错，而是**语义错位**：
  1. 主线状态是**二元开关**（有/无主线），全历史只有 32.6% 的日子有主线 ⇒ 大多数日子空章；
     且它已经被判定「测错对象」（剥 beta 后行业间相关 0.410 → −0.006，聚的是 beta 影子）。
  2. regime 章只复述「涨/跌家数」，是行情复读机，不指向任何具体行业。
  3. 链共振章按**链**出表，但 37 条链只覆盖 69/131 个行业 ⇒ 62 个行业在该章里根本没有位置。
新日报改成**以「行业」为主语**的五章（今日一句话 / 注意力梯队 / 今日异动 /
事件日历 / 综合判读+数据校验），链共振**降级为输入**（并入梯队与异动的判据），
不再单独成章 —— 这就是「主线状态并入 T1 梯队」的落地方式。

口径纪律（与 research/direction_b/01_sample_report.py 逐条对齐，勿擅自改阈值）
==================================================================
  1. 收益/成交额一律用**相对自身历史**的 robust z（中位数 + 1.4826×MAD），抗极端值。
  2. 「热度」= 当日成交额 / 自身 120 日中位成交额（量能关注度，非预测）。
  3. 「链共振」= 链内成员行业剥β剥风格残差的 20 日滚动平均两两相关（项目既有口径）。
     ⚠️ chain_resonance_daily.csv 只保留**当日快照**（无历史），故本模块**自算历史**
     以取日变化；这是「共振突变」能算出来的唯一途径。
  4. 「相对强度」= nav_924.rs_w；主指标是**其 20 日变化的 z**（加速还是衰减），
     突破标记收紧到 **120 日 + 幅度 >1%**（60 日窗口在实测日有 37% 行业触发，无区分度）。
  5. 事件 = 已发生的事实（业绩预告/券商金股/政策映射），**不做事件后收益推断**。

架构约束
==================================================================
  - 数据根一律走 src.paths.DATA_DIR（禁止硬编码，环境差异由 paths 统一吸收）
  - 面板按 LOOKBACK 裁剪后再算滚动量（全量 1641 天没必要，预热足够即可）
  - 🔴 **每一章独立降级**：任一数据源缺失/异常 ⇒ 该章渲染成一行提示，
    **绝不阻断日报生成**。日报每晚无人值守跑，宁可少一章也不能整份不出。
"""

from __future__ import annotations

import ast
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.paths import DATA_DIR, INDUSTRY_DIR, MARKET_DIR, BASIC_DIR

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────
ROBUST = 1.4826          # MAD → σ 换算系数
LOOKBACK = 420           # 面板裁剪天数：≥ 120日(相对强度) + 60日(z窗口) + 预热，留足余量
Z_WINDOW = 60            # robust z 的回看窗口
Z_MIN_P = 30             # robust z 的最小样本
RESON_WINDOW = 20        # 链共振滚动窗口（项目既有口径）
HEAT_WINDOW = 120        # 热度分母窗口
RS_BREAK_WINDOW = 120    # 相对强度突破窗口（60 日区分度不足，已校准为 120）
RS_BREAK_MARGIN = 0.01   # 突破需超出 1%

# 异动分权重（样例校准值，勿随意改；改动须重跑样例对照）
ANOM_W = {"z_ret": 0.30, "z_amt": 0.22, "z_dheat": 0.16, "z_dreson": 0.16, "z_rs": 0.16}

# 梯队阈值（2026-10-14 校准：初版 T1=45 个「等于没看」⇒ 收紧到 90% 分位）
TIER = {
    "heat_strong": 0.90,   # T1：热度 ≥90% 分位
    "reson_strong": 0.90,  # T1：共振 ≥90% 分位
    "heat_mid": 0.70,      # T0 候选
    "reson_mid": 0.70,     # T0 候选
    "anom_top": 0.85,      # T0：异动前 15%
    "anom_some": 0.70,     # T2：异动前 30%
}

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_SEP = "────────────────────────────"


# ──────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────

def ts_to_futu(s: pd.Series) -> pd.Series:
    """tushare 代码 → 富途代码：600679.SH → sh.600679"""
    s = s.astype(str).str.strip()
    return s.str.split(".").str[-1].str.lower() + "." + s.str.split(".").str[0]


def robust_z(df: pd.DataFrame, window: int = Z_WINDOW, min_p: int = Z_MIN_P) -> pd.DataFrame:
    """每列：当日值相对自身过去 window 日（**不含当日**）的 robust z。

    shift(1) 是刻意的：不含当日，避免当日值稀释它自己要被比较的分布。
    """
    med = df.shift(1).rolling(window, min_periods=min_p).median()
    mad = df.shift(1).rolling(window, min_periods=min_p).apply(
        lambda x: np.nanmedian(np.abs(x - np.nanmedian(x))), raw=True)
    return (df - med) / (ROBUST * mad).replace(0, np.nan)


def pct_rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, na_option="keep")


def fmt(v, spec: str = "+.2f", suffix: str = "", na: str = "—") -> str:
    try:
        f = float(v)
        if pd.isna(f):
            return na
        return format(f, spec) + suffix
    except (TypeError, ValueError):
        return na


# ──────────────────────────────────────────────────────────
# 计算层
# ──────────────────────────────────────────────────────────

def load_industry_names() -> Dict[str, str]:
    il = pd.read_parquet(INDUSTRY_DIR / "industry_list.parquet")
    return dict(zip(il["ts_code"], il["name"]))


def load_panels(lookback: int = LOOKBACK):
    """行业日收益(%) / 成交额 / 剥风格残差 / 相对强度 四张宽表。

    全部按 R 的 index/columns 对齐，并裁剪到最近 lookback 天。
    """
    wr = pd.read_parquet(INDUSTRY_DIR / "industry_weighted_returns.parquet")
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code",
                       values="weighted_pct").sort_index()

    idf = pd.read_parquet(INDUSTRY_DIR / "industry_daily_full.parquet")
    idf["date"] = pd.to_datetime(idf["date"])
    AMT = idf.pivot_table(index="date", columns="con_code",
                          values="total_amount").sort_index()

    rp = pd.read_parquet(INDUSTRY_DIR / "industry_residual_panel.parquet")
    rp["date"] = pd.to_datetime(rp["date"])
    RA = rp.pivot_table(index="date", columns="ind_code",
                        values="resid_adj").sort_index()

    nv = pd.read_parquet(INDUSTRY_DIR / "industry_nav_924.parquet")
    nv["date"] = pd.to_datetime(nv["date"])
    RS = nv.pivot_table(index="date", columns="ind_code",
                        values="rs_w").sort_index()

    R = R.tail(lookback)
    idx, cols = R.index, R.columns
    AMT = AMT.reindex(index=idx, columns=cols)
    RA = RA.reindex(index=idx, columns=cols)
    RS = RS.reindex(index=idx, columns=cols)
    return R, AMT, RA, RS


def load_chains(names: Dict[str, str]) -> Dict[str, List[str]]:
    """37 条已验证链的成员行业（chain_id → [ind_code]）。

    ⚠️ 只取成员数 ≥3 的链：2 个成员的「平均两两相关」就是那一个相关系数，
       既不稳健也无法与多成员链横向比较。
    """
    path = INDUSTRY_DIR / "chain_mainline_detection.csv"
    df = pd.read_csv(path)
    name2code = {v: k for k, v in names.items()}
    chains: Dict[str, List[str]] = {}
    for _, r in df.iterrows():
        try:
            raw = ast.literal_eval(str(r["chain_names"]))
        except (ValueError, SyntaxError):
            continue
        codes = [name2code[n] for n in raw if n in name2code]
        if len(codes) >= 3:
            chains[str(r["chain_id"])] = codes
    return chains


def rolling_mean_corr(arr: np.ndarray, w: int = RESON_WINDOW) -> np.ndarray:
    """链内成员 w 日滚动平均两两相关（共振的既有口径）。"""
    n = arr.shape[0]
    out = np.full(n, np.nan)
    for i in range(w - 1, n):
        wnd = arr[i - w + 1:i + 1]
        ok = ~np.isnan(wnd).any(axis=0)
        if ok.sum() < 2:
            continue
        x = wnd[:, ok]
        sd = x.std(axis=0)
        good = sd > 0
        if good.sum() < 2:
            continue
        z = (x - x.mean(axis=0)) / np.where(sd == 0, 1, sd)
        C = np.corrcoef(z.T)
        m = C.shape[0]
        if m < 2:
            continue
        out[i] = (C.sum() - m) / (m * m - m)
    return out


def build_chain_resonance(RA: pd.DataFrame, chains: Dict[str, List[str]]) -> pd.DataFrame:
    """自算各链的共振历史序列（因为落盘产物只有当日快照）。"""
    cols = {}
    for cid, members in chains.items():
        sub = RA[[c for c in members if c in RA.columns]]
        if sub.shape[1] < 2:
            continue
        cols[cid] = pd.Series(rolling_mean_corr(sub.values, RESON_WINDOW), index=RA.index)
    return pd.DataFrame(cols)


def build_anomaly(R, AMT, RA, RS, chains, target) -> pd.DataFrame:
    """5 类异动 → 每行业一行。全部为「相对自身历史」的描述量。"""
    if target not in R.index:
        raise ValueError(f"目标日 {target.date()} 不在收益面板中（最新 {R.index.max().date()}）")

    # ① 涨跌异动
    z_ret = robust_z(R)

    # ② 成交异动：当日额 / 前20日中位的对数放量比，再对其取 z
    amt_base = AMT.shift(1).rolling(20, min_periods=10).median()
    amt_ratio = np.log(AMT / amt_base.replace(0, np.nan))
    z_amt = robust_z(amt_ratio)

    # ③ 热度（量能关注度）及其日变化
    heat = AMT / AMT.shift(1).rolling(HEAT_WINDOW, min_periods=60).median().replace(0, np.nan)
    z_dheat = robust_z(heat.diff())

    # ④ 链共振 → 行业层面取所属链的日变化
    RC = build_chain_resonance(RA, chains)
    d_reson = pd.DataFrame(np.nan, index=RA.index, columns=RA.columns)
    for cid, members in chains.items():
        if cid not in RC.columns:
            continue
        for c in members:
            if c in d_reson.columns:
                d_reson[c] = RC[cid].diff()
    z_dreson = robust_z(d_reson)

    # ⑤ 相对强度：主用 20 日变化的 z；突破标记用 120 日 + 幅度 >1%
    z_rs = robust_z(RS.diff(20))
    rs_hi = RS.shift(1).rolling(RS_BREAK_WINDOW, min_periods=80).max()
    rs_lo = RS.shift(1).rolling(RS_BREAK_WINDOW, min_periods=80).min()
    rs_break = pd.Series("", index=RS.columns)
    if target in RS.index:
        row = RS.loc[target]
        rs_break[row >= rs_hi.loc[target] * (1 + RS_BREAK_MARGIN)] = "创120日新高"
        rs_break[row <= rs_lo.loc[target] * (1 - RS_BREAK_MARGIN)] = "创120日新低"

    # 综合异动强度：各 z 的加权绝对和（描述「今天它有多反常」）
    comp = pd.DataFrame({
        "z_ret": z_ret.loc[target], "z_amt": z_amt.loc[target],
        "z_dheat": z_dheat.loc[target], "z_dreson": z_dreson.loc[target],
        "z_rs": z_rs.loc[target],
    })
    score = sum(ANOM_W[k] * comp[k].abs().fillna(0) for k in ANOM_W)

    # 行业 → 所属链的当日共振（多链归属取第一条，口径在附录里写明）
    chain_of = {}
    for cid, members in chains.items():
        for c in members:
            chain_of.setdefault(c, cid)
    reson = pd.Series(
        {c: (RC[chain_of[c]].loc[target] if c in chain_of and chain_of[c] in RC.columns
             else np.nan) for c in RA.columns}
    )

    df = pd.DataFrame({
        "ret": R.loc[target],
        "z_ret": z_ret.loc[target],
        "amt_ratio": np.exp(amt_ratio.loc[target]),
        "z_amt": z_amt.loc[target],
        "heat": heat.loc[target],
        "z_dheat": z_dheat.loc[target],
        "reson": reson,
        "z_dreson": z_dreson.loc[target],
        "z_rs": z_rs.loc[target],
        "rs_break": rs_break,
        "anom_score": score,
    })
    df.index.name = "ind_code"
    # 链归属标记：链外行业在表里显式标「链外」，其梯队判定只用热度与异动
    df["in_chain"] = df.index.isin(chain_of.keys())
    return df


def assign_tier(an: pd.DataFrame) -> pd.Series:
    """T0 高关注 / T1 关注 / T2 观察 / T3 冷区 —— 全部基于横截面分位的**描述性**分层。

    🔴 阈值校准（2026-10-14，勿擅自放宽）：
      初版「热度≥80% 或 共振≥70%」得到 T1=45 个（占 1/3）——「看 45 个」等于没看；
      收紧到 90% 分位后 T1=12 个。T0 初版「三者齐备」当天 0 个，
      改为「异动前 15% 且 热度/共振 任一 ≥70%」⇒ 保证每天有真焦点。
    """
    heat_p = pct_rank(an["heat"])
    reson_p = pct_rank(an["reson"])
    anom_p = pct_rank(an["anom_score"])

    strong_heat = heat_p >= TIER["heat_strong"]
    strong_reson = reson_p >= TIER["reson_strong"]
    mid_heat = heat_p >= TIER["heat_mid"]
    mid_reson = reson_p >= TIER["reson_mid"]
    top_anom = anom_p >= TIER["anom_top"]
    some_anom = anom_p >= TIER["anom_some"]

    tier = pd.Series("T3 冷区", index=an.index)
    tier[some_anom] = "T2 观察"
    tier[strong_heat | strong_reson] = "T1 关注"
    tier[top_anom & (mid_heat | mid_reson)] = "T0 高关注"
    return tier


def cross_validate(target) -> pd.DataFrame:
    """🔴 数据可信度校验：同一天的「市场涨跌」用多源交叉核对。

    2026-10-14 踩坑：诊断时误用「两个中位数相除」算收益率 ⇒ 得出与事实相反的 +1.29%，
    一度误判 nav_924 数据损坏。正确算法（pct_change 后取中位）下四源一致。
    ⇒ 把这一步**固化进日报**：任何一源与其余方向背离即显式告警。
    """
    rows = []
    dstr = str(target.date())

    # ① 个股等权中位
    f = MARKET_DIR / f"{dstr}.parquet"
    if f.exists():
        try:
            md = pd.read_parquet(f, columns=["pctChg"])
            rows.append(("个股等权中位", md["pctChg"].median(),
                         int((md["pctChg"] > 0).sum()), len(md)))
        except Exception as e:
            logger.warning("交叉校验·个股等权失败: %s", e)

    # ② 全市场市值加权
    db = BASIC_DIR / f"{dstr}.parquet"
    if f.exists() and db.exists():
        try:
            md = pd.read_parquet(f, columns=["code", "pctChg"])
            b = pd.read_parquet(db, columns=["ts_code", "circ_mv"])
            b["code"] = ts_to_futu(b["ts_code"])
            j = md.merge(b[["code", "circ_mv"]], on="code", how="inner").dropna()
            if len(j):
                w = (j["pctChg"] * j["circ_mv"]).sum() / j["circ_mv"].sum()
                rows.append(("全市场市值加权", w, np.nan, len(j)))
        except Exception as e:
            logger.warning("交叉校验·市值加权失败: %s", e)

    # ③ industry_daily_full avg_pct（行业内等权）
    try:
        idf = pd.read_parquet(INDUSTRY_DIR / "industry_daily_full.parquet")
        idf["date"] = pd.to_datetime(idf["date"])
        d = idf[idf["date"] == target]
        if len(d):
            rows.append(("行业等权(avg_pct)", d["avg_pct"].median(),
                         int((d["avg_pct"] > 0).sum()), len(d)))
    except Exception as e:
        logger.warning("交叉校验·行业等权失败: %s", e)

    # ④ nav_924（累计净值，必须用 pct_change，不能两中位数相除）
    try:
        nv = pd.read_parquet(INDUSTRY_DIR / "industry_nav_924.parquet")
        nv["date"] = pd.to_datetime(nv["date"])
        N = nv.pivot_table(index="date", columns="ind_code", values="nav_w").sort_index()
        if target in N.index:
            r = N.pct_change().loc[target] * 100
            rows.append(("行业净值(nav_w)", r.median(),
                         int((r > 0).sum()), int(r.notna().sum())))
    except Exception as e:
        logger.warning("交叉校验·nav_w 失败: %s", e)

    return pd.DataFrame(rows, columns=["源", "当日涨跌中位%", "上涨数", "样本数"])


def load_events(target) -> dict:
    """当日业绩预告 / 当月券商金股 / 政策映射。全部是**已发生的事实**。"""
    res: dict = {"forecast": None, "broker": None, "policy": None}

    # ① 当日业绩预告（按公告日存放的事件流）
    f = DATA_DIR / "reports" / "forecast" / str(target.year) / f"{target.date()}.parquet"
    if f.exists():
        try:
            fc = pd.read_parquet(f)
            fc["stock_code"] = ts_to_futu(fc["ts_code"])
            mem = pd.read_parquet(INDUSTRY_DIR / "industry_members_daily.parquet")
            mem["date"] = pd.to_datetime(mem["date"])
            m = mem[mem["date"] == target][["stock_code", "ind_code"]]
            fc = fc.merge(m, on="stock_code", how="left")
            fc["p_mid"] = (fc["p_change_min"] + fc["p_change_max"]) / 2
            res["forecast"] = fc
        except Exception as e:
            logger.warning("业绩预告加载失败: %s", e)

    # ② 当月券商金股
    ym = f"{target.year}{target.month:02d}"
    b = DATA_DIR / "reports" / "broker_recommend" / f"{ym}.parquet"
    if b.exists():
        try:
            br = pd.read_parquet(b)
            br["stock_code"] = ts_to_futu(br["ts_code"])
            mem = pd.read_parquet(INDUSTRY_DIR / "industry_members_daily.parquet")
            mem["date"] = pd.to_datetime(mem["date"])
            latest = mem["date"].max()
            m = mem[mem["date"] == latest][["stock_code", "ind_code"]]
            res["broker"] = br.merge(m, on="stock_code", how="left")
        except Exception as e:
            logger.warning("券商金股加载失败: %s", e)

    # ③ 政策映射（静态先验）
    p = DATA_DIR / "policy" / "sector_policy_map.json"
    if p.exists():
        try:
            res["policy"] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("政策映射加载失败: %s", e)
    return res


# ──────────────────────────────────────────────────────────
# 渲染层（每一章独立降级）
# ──────────────────────────────────────────────────────────

def _degraded(title: str, err: Exception) -> List[str]:
    return [f"## {title}", "", f"> ⚠️ 本章数据不可用：{err}", ""]


def render_summary(an: pd.DataFrame, tier: pd.Series, names: Dict[str, str]) -> List[str]:
    """零、今日一句话 —— 把最该看的 5 个行业直接点名。"""
    L: List[str] = ["## 🎯 零、今日一句话", ""]
    top_anom = an["anom_score"].nlargest(5).index.tolist()
    # T0 按异动分排序，保证与下方 T0 明细表顺序一致（否则读者会以为是两个不同的排序）
    t0 = (an.loc[tier[tier == "T0 高关注"].index]
          .sort_values("anom_score", ascending=False).index.tolist())
    n_up = int((an["ret"] > 0).sum())
    n_all = int(an["ret"].notna().sum())
    L.append(f"- 全市场 **{n_up}/{n_all}** 个申万二级行业上涨（占比 {n_up/max(n_all,1):.0%}）。")
    if t0:
        L.append("- **高关注（T0）{} 个**，最该先看前 5 个：**{}**。".format(
            len(t0), "、".join(names.get(c, c) for c in t0[:5])))
    else:
        L.append("- 今日无 T0 高关注行业（无「异动显著 + 热度/共振较强」者）。")
    L.append("- 异动最强：**" + "、".join(names.get(c, c) for c in top_anom[:3]) + "**。")
    L.append("")
    return L


def render_tier(an: pd.DataFrame, tier: pd.Series, names: Dict[str, str]) -> List[str]:
    """一、注意力梯队 —— 用连续梯队替代旧的「有/无主线」二元开关。"""
    L: List[str] = ["## 🧭 一、注意力梯队（今天看谁）", ""]
    L.append("| 梯队 | 含义 | 数量 | 行业 |")
    L.append("|:----:|:-----|:----:|:-----|")
    for t, desc in [("T0 高关注", "**今天最反常 + 热度/共振较强**（正在发酵，优先看）"),
                    ("T1 关注", "热度或共振 ≥90% 分位（持续被关注，但今天不反常）"),
                    ("T2 观察", "有异动但热度/共振不强（可能是启动前兆，也可能是噪音）"),
                    ("T3 冷区", "无热度、无异动（今天不用看）")]:
        members = tier[tier == t].index.tolist()
        if t == "T3 冷区":
            shown = f"（其余 {len(members)} 个）"
        else:
            ordered = (an.loc[members].sort_values("anom_score", ascending=False)
                       .index.tolist())
            shown = "、".join(names.get(c, c) for c in ordered[:12]) or "—"
            if len(members) > 12:
                shown += f" 等 {len(members)} 个"
        L.append(f"| **{t}** | {desc} | {len(members)} | {shown} |")
    L += ["", "> ⚠️ 梯队是**描述**（谁正在被关注/发生变化），**不是推荐买入**。", ""]

    for t in ["T0 高关注", "T1 关注", "T2 观察"]:
        members = tier[tier == t].index.tolist()
        if not members:
            continue
        sub = an.loc[members].sort_values("anom_score", ascending=False)
        L.append(f"**{t} 明细（按异动强度排序）**")
        L.append("")
        L.append("| 行业 | 今日涨幅 | 涨跌z | 放量z | 热度 | 共振 | 共振变化z | 强度变化z | 相对强度 | 异动分 |")
        L.append("|:-----|-------:|-----:|-----:|-----:|-----:|-------:|-------:|:------:|------:|")
        for c, r in sub.head(10).iterrows():
            L.append(
                f"| {names.get(c, c)} | {fmt(r['ret'], '+.2f', '%')} "
                f"| {fmt(r['z_ret'], '+.2f')} | {fmt(r['z_amt'], '+.2f')} "
                f"| {fmt(r['heat'], '.2f', 'x')} "
                f"| {fmt(r['reson'], '+.3f') if r['in_chain'] else '链外'} "
                f"| {fmt(r['z_dreson'], '+.2f')} | {fmt(r['z_rs'], '+.2f')} "
                f"| {r['rs_break'] or '—'} "
                f"| {fmt(r['anom_score'], '.2f')} |")
        L.append("")
    return L


def render_anomaly(an: pd.DataFrame, names: Dict[str, str]) -> List[str]:
    """二、今日异动 —— 今天发生了什么变化（含量价组合语义）。"""
    L: List[str] = ["## ⚡ 二、今日异动（今天发生了什么变化）", ""]
    L.append("> 全部为**已发生事实**的描述，不是预测。「z」= 相对该行业自身过去 60 日的偏离度。")
    L.append("")
    top = an.nlargest(12, "anom_score")
    L.append("| 行业 | 异动分 | 涨幅 | 涨跌z | 放量倍数 | 热度变化z | 共振变化z | 强度变化z | 异动解读 |")
    L.append("|:-----|------:|-----:|------:|-------:|--------:|--------:|-------:|:---------|")
    for c, r in top.iterrows():
        tags: List[str] = []
        up = pd.notna(r["ret"]) and r["ret"] > 0
        # 量价组合语义比单看涨跌更有信息：放量上涨 vs 放量下跌含义完全不同
        if pd.notna(r["z_amt"]) and r["z_amt"] >= 2:
            tags.append("放量上涨" if up else "放量下跌")
        elif pd.notna(r["z_amt"]) and r["z_amt"] <= -2:
            tags.append("缩量上涨" if up else "缩量下跌")
        if pd.notna(r["z_ret"]) and abs(r["z_ret"]) >= 2 and not tags:
            tags.append("急涨" if r["z_ret"] > 0 else "急跌")
        if pd.notna(r["z_dreson"]) and r["z_dreson"] >= 2:
            tags.append("链内共振走强")
        elif pd.notna(r["z_dreson"]) and r["z_dreson"] <= -2:
            tags.append("链内共振瓦解")
        if pd.notna(r["z_dheat"]) and r["z_dheat"] >= 2:
            tags.append("关注度跃升")
        if pd.notna(r["z_rs"]) and r["z_rs"] >= 2:
            tags.append("相对强度加速")
        elif pd.notna(r["z_rs"]) and r["z_rs"] <= -2:
            tags.append("相对强度衰减")
        if r["rs_break"]:
            tags.append(r["rs_break"])
        L.append(
            f"| **{names.get(c, c)}** | {fmt(r['anom_score'], '.2f')} "
            f"| {fmt(r['ret'], '+.2f', '%')} | {fmt(r['z_ret'], '+.2f')} "
            f"| {fmt(r['amt_ratio'], '.2f', 'x')} "
            f"| {fmt(r['z_dheat'], '+.2f')} | {fmt(r['z_dreson'], '+.2f')} "
            f"| {fmt(r['z_rs'], '+.2f')} "
            f"| {'、'.join(tags) or '多维度中等偏离'} |")
    L += ["", "**分类异动榜**", ""]
    for col, label in [("z_ret", "涨跌异动 Top5"), ("z_amt", "放量异动 Top5"),
                       ("z_dreson", "链共振突变 Top5"), ("z_dheat", "关注度跃升 Top5"),
                       ("z_rs", "相对强度变化 Top5")]:
        s = an[col].dropna().nlargest(5)
        if len(s):
            L.append("- **{}**：".format(label) + "、".join(
                f"{names.get(c, c)}({v:+.1f})" for c, v in s.items()))
    L.append("")

    brk = an[an["rs_break"] != ""]
    if len(brk):
        L.append(f"**相对强度突破**（共 {len(brk)} 个）")
        L.append("")
        L.append("| 行业 | 信号 | 今日涨幅 | 共振 |")
        L.append("|:-----|:----:|-------:|-----:|")
        for c, r in brk.nlargest(min(10, len(brk)), "anom_score").iterrows():
            L.append(f"| {names.get(c, c)} | {r['rs_break']} | {fmt(r['ret'], '+.2f', '%')} "
                     f"| {fmt(r['reson'], '+.3f')} |")
        L.append("")
    return L


def render_events(ev: dict, names: Dict[str, str]) -> List[str]:
    """三、事件日历 —— 只记录「今天有哪些基本面信息落地」，不做收益推断。"""
    L: List[str] = ["## 📅 三、事件日历（今天/近期发生了什么基本面事件）", ""]

    fc = ev.get("forecast")
    if fc is not None and len(fc):
        agg = fc.dropna(subset=["ind_code"]).groupby("ind_code").agg(
            n=("ts_code", "size"),
            p_med=("p_mid", "median"),
            types=("type", lambda s: "/".join(sorted(set(s))[:3])),
        ).sort_values("n", ascending=False)
        L.append(f"**① 当日业绩预告**（共 {len(fc)} 条，涉及 {len(agg)} 个行业）")
        L.append("")
        L.append("| 行业 | 预告数 | 净利变动中位 | 类型 |")
        L.append("|:-----|------:|------------:|:-----|")
        for c, r in agg.head(10).iterrows():
            L.append(f"| {names.get(c, c)} | {int(r['n'])} | "
                     f"{fmt(r['p_med'], '+.1f', '%')} | {r['types']} |")
        L += ["", "> ⚠️ 研究结论（09 报告）：业绩预告的**方向与幅度对未来超额无区分力**，"
                  "此处仅作「今天有哪些基本面信息落地」的**事实记录**。", ""]
    else:
        L += ["**① 当日业绩预告**：无（当日无披露）。", ""]

    br = ev.get("broker")
    if br is not None and len(br):
        b = br.dropna(subset=["ind_code"])
        bagg = b.groupby("ind_code").size().sort_values(ascending=False)
        L.append(f"**② 本月券商金股**（{len(br)} 个推荐，覆盖 {len(bagg)} 个行业）")
        L.append("")
        L.append("| 行业 | 被推荐次数 |")
        L.append("|:-----|----------:|")
        for c, n in bagg.head(8).items():
            L.append(f"| {names.get(c, c)} | {int(n)} |")
        L += ["", "> 券商推荐是**机构观点**，非本雷达判断；仅作「市场在讨论什么」的参考。", ""]

    pol = ev.get("policy")
    if pol and "categories" in pol:
        cats = list(pol["categories"].keys())
        name_set = set(names.values())
        hit = [c for c in cats if c in name_set]
        L.append(f"**③ 政策/产业映射**：映射表含 {len(cats)} 个赛道，"
                 f"其中与申万二级同名可对齐 **{len(hit)}** 个"
                 f"{'（' + '、'.join(hit[:10]) + '）' if hit else ''}。")
        L += ["", "> 政策映射为**静态先验**（用于判断异动是否有产业逻辑支撑），非当日新增事件。", ""]
    return L


def render_synthesis(an: pd.DataFrame, tier: pd.Series, names: Dict[str, str]) -> List[str]:
    """四、综合判读 —— 把宽度、焦点、最反常的变化合成一句可行动叙述。"""
    n_up = int((an["ret"] > 0).sum())
    n_all = int(an["ret"].notna().sum())
    up_ratio = n_up / max(n_all, 1)
    breadth = ("普涨" if up_ratio >= 0.7 else "分化偏多" if up_ratio >= 0.45
               else "分化偏空" if up_ratio >= 0.3 else "普跌")
    top_anom = an["anom_score"].nlargest(5).index.tolist()
    t0 = (an.loc[tier[tier == "T0 高关注"].index]
          .sort_values("anom_score", ascending=False).index.tolist())

    L: List[str] = ["## 🧩 四、综合判读", ""]
    L.append(f"- **市场宽度**：{breadth}（{n_up}/{n_all} 上涨）。")
    L.append("- **注意力焦点**：" +
             ("、".join(names.get(c, c) for c in t0[:5]) if t0 else "今日无 T0，焦点分散") + "。")
    L.append("- **最反常的变化**：" +
             "、".join(names.get(c, c) for c in top_anom[:3]) + "。")
    L += ["",
          "> 📌 **怎么用**：本报告只负责**把有限的注意力指到对的地方**——",
          "> 「谁在动、动得多反常、是不是整条链一起动」。至于要不要下手，",
          "> 由你在标的层面自行判断（雷达的用途是**发现行业 → 再在其中找个股**）。",
          ""]
    return L


def render_validation(target) -> List[str]:
    """五、数据校验 —— 多源交叉核对当日行情，方向背离即告警。"""
    L: List[str] = ["## 🔍 五、数据校验（多源交叉核对当日行情）", ""]
    cv = cross_validate(target)
    if cv.empty:
        L += ["> ⚠️ 无可用的交叉校验数据源。", ""]
        return L
    L.append("| 数据源 | 当日涨跌中位 | 上涨数 | 样本数 |")
    L.append("|:-------|------------:|-------:|-------:|")
    for _, r in cv.iterrows():
        L.append(f"| {r['源']} | {fmt(r['当日涨跌中位%'], '+.2f', '%')} "
                 f"| {'' if pd.isna(r['上涨数']) else int(r['上涨数'])} | {int(r['样本数'])} |")
    L.append("")
    vals = cv["当日涨跌中位%"].dropna()
    if len(vals) >= 2 and vals.max() * vals.min() < 0:
        L.append("> 🚨 **告警**：各源涨跌**方向不一致**，当日行情数据存疑，本报告数值需谨慎使用。")
    else:
        L.append("> ✅ 各源方向一致（数值差异来自**权重口径**：个股等权 < 行业等权 < 市值加权，"
                 "小盘股跌幅更大），数据可信。")
    L += ["",
          "> 校验意义：2026-10-14 踩坑——用「两个中位数相除」算收益率会得出与事实相反的结论。",
          "此处固化为标准步骤，**任何一源方向背离即告警**。",
          ""]
    return L


def render_appendix(chains: Dict[str, List[str]], n_ind: int) -> List[str]:
    inchain = set()
    for ms in chains.values():
        inchain |= set(ms)
    L: List[str] = [_SEP, "", "**口径附录**", "",
                    "| 指标 | 定义 | 性质 |", "|:-----|:-----|:-----|"]
    L.append("| 涨跌z | 当日涨幅相对自身过去 60 日的 robust z（中位数+1.4826×MAD） | 描述 |")
    L.append("| 放量倍数 | 当日成交额 / 前 20 日中位成交额 | 描述 |")
    L.append(f"| 热度 | 当日成交额 / 自身 {HEAT_WINDOW} 日中位成交额 | 描述（量能关注度） |")
    L.append(f"| 共振 | 链内成员行业剥β剥风格残差的 {RESON_WINDOW} 日滚动平均两两相关 | 描述（结构同向性） |")
    L.append("| 相对强度 | nav_924.rs_w；「强度变化z」= 其 20 日变化相对自身 60 日历史的 z | 描述 |")
    L.append(f"| 突破标记 | rs_w 创 **{RS_BREAK_WINDOW} 日**新高/新低且幅度 >"
             f"{RS_BREAK_MARGIN:.0%}（60 日窗口区分度不足） | 描述 |")
    w = " + ".join(f"{v:.2f}·|{k}|" for k, v in ANOM_W.items())
    L.append(f"| 异动分 | {w} | 综合描述 |")
    L.append(f"| 梯队 | T0=异动前{1-TIER['anom_top']:.0%} 且(热度≥{TIER['heat_mid']:.0%} 或 "
             f"共振≥{TIER['reson_mid']:.0%})；T1=热度或共振≥{TIER['heat_strong']:.0%}；"
             f"T2=异动前{1-TIER['anom_some']:.0%}；T3=其余 | 描述性分层 |")
    L.append("")
    L.append(f"> 📊 **链覆盖**：{len(chains)} 条已验证链覆盖 {len(inchain)}/{n_ind} 个行业；"
             f"其余 {max(n_ind - len(inchain), 0)} 个行业无链归属，表格中标注「链外」，"
             f"其梯队判定只用热度与异动。")
    L.append("")
    L.append("> 🔴 **无预测力声明**：上表所有量均为「相对自身历史」的描述，"
             "本项目已用 9 份研究（02/03/04/05/06/07/08/09）"
             "证明这类横截面信号**不具备 alpha**。")
    L.append("")
    return L


# ──────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────

def build_report_markdown(trade_date: str) -> str:
    """生成方向 B 完整日报正文（标题 + 五章 + 附录）。

    🔴 纪律：每一步独立 try/except。日报每晚无人值守跑（launchd 4 次触发重试），
       宁可某章降级成一行提示，也不能整份不出 —— 历史上多次因为一个数据源
       缺失导致整条链白跑。

    参数:
        trade_date: YYYY-MM-DD

    返回:
        Markdown 全文（含标题）
    """
    target = pd.Timestamp(trade_date)
    td = datetime.strptime(trade_date, "%Y-%m-%d")
    weekday = WEEKDAY_NAMES[td.weekday()]

    L: List[str] = [f"# 📊 行业雷达 · {trade_date}（{weekday}）", ""]
    L.append("> 🧭 **定位**：本报告**不预测哪个行业会涨**。它只回答一件事——")
    L.append("> **「今天该把注意力放在哪里」**。所有指标均为「相对自身历史」的**描述性**统计，")
    L.append("> 不含买卖建议，不承诺前瞻力（横截面 alpha 已由 9 份研究证伪）。")
    L += ["", _SEP, ""]

    # ── 数据加载（失败则整份降级，但仍出标题）──
    try:
        names = load_industry_names()
        R, AMT, RA, RS = load_panels()
        if target not in R.index:
            latest = R.index.max().date()
            L.append(f"> ⚠️ 目标日 {trade_date} 不在收益面板中（最新 {latest}），"
                     f"无法生成本日雷达。")
            return "\n".join(L)
        chains = load_chains(names)
    except Exception as e:
        logger.error("方向B 数据加载失败: %s", e, exc_info=True)
        L.append(f"> ⚠️ 数据加载失败，本日雷达不可用：{e}")
        return "\n".join(L)

    # ── 异动面板 + 梯队（核心，失败则后面各章都无意义）──
    try:
        an = build_anomaly(R, AMT, RA, RS, chains, target)
        tier = assign_tier(an)
    except Exception as e:
        logger.error("方向B 异动面板计算失败: %s", e, exc_info=True)
        L.append(f"> ⚠️ 异动面板计算失败，本日雷达不可用：{e}")
        return "\n".join(L)

    # ── 逐章渲染，各自独立降级 ──
    for title, fn in [
        ("🎯 零、今日一句话", lambda: render_summary(an, tier, names)),
        ("🧭 一、注意力梯队（今天看谁）", lambda: render_tier(an, tier, names)),
        ("⚡ 二、今日异动（今天发生了什么变化）", lambda: render_anomaly(an, names)),
        ("📅 三、事件日历（今天/近期发生了什么基本面事件）",
         lambda: render_events(load_events(target), names)),
        ("🧩 四、综合判读", lambda: render_synthesis(an, tier, names)),
        ("🔍 五、数据校验（多源交叉核对当日行情）", lambda: render_validation(target)),
    ]:
        try:
            L += fn()
        except Exception as e:
            logger.warning("章节渲染失败 [%s]: %s", title, e, exc_info=True)
            L += _degraded(title.split(" ", 1)[-1], e)

    try:
        L += render_appendix(chains, len(an))
    except Exception as e:
        logger.warning("附录渲染失败: %s", e)

    return "\n".join(L)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    d = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    print(build_report_markdown(d))
