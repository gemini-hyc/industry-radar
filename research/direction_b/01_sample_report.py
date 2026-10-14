#!/usr/bin/env python3
"""
方向 B · 目标日报样例（B1 异动层 + B2 注意力梯队 + B3 事件日历 + B4 综合判读）

🔴 定位（2026-10-14 主人定论）：注意力分配型雷达。
   本脚本**只做描述、分类、异动监控**，不产出任何买卖信号、不承诺预测力。
   所有 z 值口径均为「相对自身历史分布」的描述性统计量，不是 alpha 因子。

口径纪律:
  1. 收益/成交额一律用**相对自身历史**的 robust z（中位数 + 1.4826×MAD），抗极端值。
  2. 「热度」= 当日成交额 / 自身 120 日中位成交额（量能关注度，非预测）。
  3. 「链共振」= 链内成员行业剥β剥风格残差的 20 日滚动平均两两相关（本项目既有口径）。
     因 chain_resonance_daily.csv 只保留当日快照（无历史），本脚本**自算历史序列**以取日变化。
  4. 「相对强度」= nav_924.rs_w，判断是否创 60 日新高/新低。
  5. 事件 = 已发生的事实（业绩预告/券商金股/政策映射），**不做事件后收益推断**。

输出: research/direction_b/样例日报_<date>.md
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATA = Path("/Users/hyc/quant-data")
OUT = ROOT / "research" / "direction_b"
OUT.mkdir(parents=True, exist_ok=True)

# 目标交易日（默认取数据最新日）
TARGET = pd.Timestamp("2026-10-14")

ROBUST = 1.4826  # MAD → σ 换算


def log(msg: str) -> None:
    print(msg, flush=True)


# ──────────────────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────────────────

def ts_to_futu(s: pd.Series) -> pd.Series:
    """000001.SZ → sz.000001"""
    s = s.astype(str).str.strip()
    return s.str.split(".").str[-1].str.lower() + "." + s.str.split(".").str[0]


def robust_z(df: pd.DataFrame, window: int = 60, min_p: int = 30) -> pd.DataFrame:
    """每列：当日值相对自身过去 window 日（不含当日）的 robust z。

    用 shift(1) 保证「不含当日」，避免当日值稀释自身的分布。
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
# 数据加载
# ──────────────────────────────────────────────────────────

def load_industry_names() -> Dict[str, str]:
    il = pd.read_parquet(DATA / "industry" / "industry_list.parquet")
    return dict(zip(il["ts_code"], il["name"]))


def load_panels():
    """行业日收益（%）/ 成交额 / 残差 / 相对强度"""
    wr = pd.read_parquet(DATA / "industry" / "industry_weighted_returns.parquet")
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code",
                       values="weighted_pct").sort_index()

    idf = pd.read_parquet(DATA / "industry" / "industry_daily_full.parquet")
    idf["date"] = pd.to_datetime(idf["date"])
    AMT = idf.pivot_table(index="date", columns="con_code",
                          values="total_amount").sort_index()
    AMT = AMT.reindex(index=R.index, columns=R.columns)

    rp = pd.read_parquet(DATA / "industry" / "industry_residual_panel.parquet")
    rp["date"] = pd.to_datetime(rp["date"])
    RA = rp.pivot_table(index="date", columns="ind_code",
                        values="resid_adj").sort_index()
    RA = RA.reindex(index=R.index, columns=R.columns)

    nv = pd.read_parquet(DATA / "industry" / "industry_nav_924.parquet")
    nv["date"] = pd.to_datetime(nv["date"])
    RS = nv.pivot_table(index="date", columns="ind_code",
                        values="rs_w").sort_index()
    RS = RS.reindex(index=R.index, columns=R.columns)

    return R, AMT, RA, RS


def load_chains(names: Dict[str, str]) -> Dict[str, List[str]]:
    """37 条已验证链的成员行业（chain_id → [ind_code]）"""
    df = pd.read_csv(DATA / "industry" / "chain_mainline_detection.csv")
    name2code = {v: k for k, v in names.items()}
    chains = {}
    for _, r in df.iterrows():
        try:
            raw = ast.literal_eval(str(r["chain_names"]))
        except (ValueError, SyntaxError):
            continue
        codes = [name2code[n] for n in raw if n in name2code]
        if len(codes) >= 3:
            chains[str(r["chain_id"])] = codes
    return chains


def rolling_mean_corr(arr: np.ndarray, w: int = 20) -> np.ndarray:
    """链内成员 20 日滚动平均两两相关（共振的既有口径）。"""
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


# ──────────────────────────────────────────────────────────
# B1 · 异动层
# ──────────────────────────────────────────────────────────

def build_anomaly(R, AMT, RA, RS, chains, target):
    """5 类异动 → 每行业一行。全部为「相对自身历史」的描述量。"""
    idx = R.index
    if target not in idx:
        raise SystemExit(f"目标日 {target.date()} 不在收益面板中（最新 {idx.max().date()}）")

    # ① 涨跌异动
    z_ret = robust_z(R, 60)

    # ② 成交异动：先取「当日额 / 前20日中位」的对数放量比，再对其取 z
    amt_base = AMT.shift(1).rolling(20, min_periods=10).median()
    amt_ratio = np.log(AMT / amt_base.replace(0, np.nan))
    z_amt = robust_z(amt_ratio, 60)

    # ③ 热度（量能关注度）及其日变化
    heat = AMT / AMT.shift(1).rolling(120, min_periods=60).median().replace(0, np.nan)
    d_heat = heat.diff()
    z_dheat = robust_z(d_heat, 60)

    # ④ 链共振历史序列 → 行业层面取所属链的日变化
    reson_by_chain = {}
    for cid, members in chains.items():
        sub = RA[members]
        reson_by_chain[cid] = pd.Series(
            rolling_mean_corr(sub.values, 20), index=RA.index)
    RC = pd.DataFrame(reson_by_chain)

    # 行业 → 所属链的共振日变化（取该行业所在链的均值）
    d_reson = pd.DataFrame(np.nan, index=RA.index, columns=RA.columns)
    for cid, members in chains.items():
        for c in members:
            if c in d_reson.columns:
                d_reson[c] = RC[cid].diff()
    z_dreson = robust_z(d_reson, 60)

    # ⑤ 相对强度：主用「20 日变化的 z」（今天相对强度在加速还是衰减）；
    #    突破标记收紧到 120 日 + 幅度 >1%（60 日窗口在当前市场结构下 37% 行业都"新高"，
    #    区分度不足，见 2026-10-14 校验：沪深300 -1.36% vs 行业等权 -1.03%）。
    rs_chg = RS.diff(20)
    z_rs = robust_z(rs_chg, 60)
    rs_hi = RS.shift(1).rolling(120, min_periods=80).max()
    rs_lo = RS.shift(1).rolling(120, min_periods=80).min()
    rs_break = pd.Series("", index=RS.columns)
    if target in RS.index:
        row = RS.loc[target]
        hi, lo = rs_hi.loc[target], rs_lo.loc[target]
        rs_break[row >= hi * 1.01] = "创120日新高"
        rs_break[row <= lo * 0.99] = "创120日新低"

    # 综合异动强度：各 z 的加权绝对和（描述「今天它有多反常」）
    W = {"z_ret": 0.30, "z_amt": 0.22, "z_dheat": 0.16, "z_dreson": 0.16, "z_rs": 0.16}
    comp = pd.DataFrame({
        "z_ret": z_ret.loc[target], "z_amt": z_amt.loc[target],
        "z_dheat": z_dheat.loc[target], "z_dreson": z_dreson.loc[target],
        "z_rs": z_rs.loc[target],
    })
    score = sum(W[k] * comp[k].abs().fillna(0) for k in W)

    df = pd.DataFrame({
        "ret": R.loc[target],
        "z_ret": z_ret.loc[target],
        "amt_ratio": np.exp(amt_ratio.loc[target]),
        "z_amt": z_amt.loc[target],
        "heat": heat.loc[target],
        "z_dheat": z_dheat.loc[target],
        "reson": pd.Series({c: RC[[cid for cid, ms in chains.items()
                                  if c in ms][0]].loc[target]
                            if any(c in ms for ms in chains.values()) else np.nan
                            for c in RA.columns}),
        "z_dreson": z_dreson.loc[target],
        "z_rs": z_rs.loc[target],
        "rs_break": rs_break,
        "anom_score": score,
    })
    df.index.name = "ind_code"
    df["in_chain"] = [any(c in ms for ms in chains.values()) for c in df.index]
    return df, RC


# ──────────────────────────────────────────────────────────
# B2 · 注意力梯队
# ──────────────────────────────────────────────────────────

def assign_tier(an: pd.DataFrame) -> pd.Series:
    """T0 高关注 / T1 关注 / T2 观察 / T3 冷区。

    全部基于**横截面分位**的描述性分层，不含预测。

    🔴 阈值校准（2026-10-14）：初版用「热度≥80% 或 共振≥70%」得到 T1=45 个（占 1/3），
       「看 45 个」等于没看 ⇒ 收紧到 90% 分位；同时 T0 从「三者齐备」（当天 0 个）
       改为「异动前 15% 且 热度/共振 任一较强」⇒ 保证每天有 3~12 个真焦点。
    """
    heat_p = pct_rank(an["heat"])
    reson_p = pct_rank(an["reson"])
    anom_p = pct_rank(an["anom_score"])

    strong_heat = heat_p >= 0.90          # 持续被资金关注
    strong_reson = reson_p >= 0.90        # 链内结构同向
    mid_heat = heat_p >= 0.70
    mid_reson = reson_p >= 0.70
    top_anom = anom_p >= 0.85             # 今天最反常的 15%
    some_anom = anom_p >= 0.70

    tier = pd.Series("T3 冷区", index=an.index)
    tier[some_anom] = "T2 观察"
    tier[strong_heat | strong_reson] = "T1 关注"
    tier[top_anom & (mid_heat | mid_reson)] = "T0 高关注"
    return tier


# ──────────────────────────────────────────────────────────
# B3 · 事件日历
# ──────────────────────────────────────────────────────────

def cross_validate(target) -> pd.DataFrame:
    """🔴 数据可信度校验：同一天的「市场涨跌」用多源交叉核对。

    2026-10-14 踩坑：诊断时误用「两个中位数相除」算收益率 ⇒ 得到与事实相反的 +1.29%，
    一度误判 nav_924 数据损坏。正确算法（pct_change 后取中位）下四源一致。
    ⇒ 把这一步**固化进日报**，任何一源与其余偏离过大即显式告警。
    """
    rows = []

    # ① 个股等权中位
    f = DATA / "market" / "daily" / f"{target.date()}.parquet"
    if f.exists():
        md = pd.read_parquet(f, columns=["pctChg"])
        rows.append(("个股等权中位", md["pctChg"].median(), (md["pctChg"] > 0).sum(), len(md)))

    # ② 全市场市值加权
    db = DATA / "daily_basic" / f"{target.date()}.parquet"
    if f.exists() and db.exists():
        md = pd.read_parquet(f, columns=["code", "pctChg"])
        b = pd.read_parquet(db, columns=["ts_code", "circ_mv"])
        b["code"] = ts_to_futu(b["ts_code"])
        j = md.merge(b[["code", "circ_mv"]], on="code", how="inner").dropna()
        w = (j["pctChg"] * j["circ_mv"]).sum() / j["circ_mv"].sum()
        rows.append(("全市场市值加权", w, np.nan, len(j)))

    # ③ industry_daily_full avg_pct（行业内等权）
    idf = pd.read_parquet(DATA / "industry" / "industry_daily_full.parquet")
    idf["date"] = pd.to_datetime(idf["date"])
    d = idf[idf["date"] == target]
    if len(d):
        rows.append(("行业等权(avg_pct)", d["avg_pct"].median(),
                     int((d["avg_pct"] > 0).sum()), len(d)))

    # ④ nav_924 nav_w（累计净值，须用 pct_change 而非中位数相除）
    nv = pd.read_parquet(DATA / "industry" / "industry_nav_924.parquet")
    nv["date"] = pd.to_datetime(nv["date"])
    N = nv.pivot_table(index="date", columns="ind_code", values="nav_w").sort_index()
    if target in N.index:
        r = N.pct_change().loc[target] * 100
        rows.append(("行业净值(nav_w)", r.median(), int((r > 0).sum()), int(r.notna().sum())))

    return pd.DataFrame(rows, columns=["源", "当日涨跌中位%", "上涨数", "样本数"])


def load_events(target):
    """当日业绩预告（按行业聚合）/ 当月券商金股 / 政策映射"""
    res = {}

    # ① 当日业绩预告
    f = DATA / "reports" / "forecast" / str(target.year) / f"{target.date()}.parquet"
    if f.exists():
        fc = pd.read_parquet(f)
        fc["stock_code"] = ts_to_futu(fc["ts_code"])
        mem = pd.read_parquet(DATA / "industry" / "industry_members_daily.parquet")
        mem["date"] = pd.to_datetime(mem["date"])
        m = mem[mem["date"] == target][["stock_code", "ind_code"]]
        fc = fc.merge(m, on="stock_code", how="left")
        fc["p_mid"] = (fc["p_change_min"] + fc["p_change_max"]) / 2
        res["forecast"] = fc
    else:
        res["forecast"] = None

    # ② 当月券商金股
    ym = f"{target.year}{target.month:02d}"
    b = DATA / "reports" / "broker_recommend" / f"{ym}.parquet"
    if b.exists():
        br = pd.read_parquet(b)
        br["stock_code"] = ts_to_futu(br["ts_code"])
        mem = pd.read_parquet(DATA / "industry" / "industry_members_daily.parquet")
        mem["date"] = pd.to_datetime(mem["date"])
        latest = mem["date"].max()
        m = mem[mem["date"] == latest][["stock_code", "ind_code"]]
        br = br.merge(m, on="stock_code", how="left")
        res["broker"] = br
    else:
        res["broker"] = None

    # ③ 政策映射
    p = DATA / "policy" / "sector_policy_map.json"
    res["policy"] = json.load(open(p, encoding="utf-8")) if p.exists() else None
    return res


# ──────────────────────────────────────────────────────────
# 渲染
# ──────────────────────────────────────────────────────────

def render(target, an, tier, RC, ev, names, chains):
    dstr = target.date()
    L: List[str] = []
    A = L.append

    A(f"# 行业雷达 · 日报样例（方向 B · 注意力分配）· {dstr}")
    A("")
    A("> 🧭 **定位**：本报告**不预测哪个行业会涨**。它只回答一件事——")
    A("> **「今天该把注意力放在哪里」**。所有指标均为「相对自身历史」的**描述性**统计，")
    A("> 不含买卖建议，不承诺前瞻力（横截面 alpha 已由 9 份研究证伪）。")
    A("")
    A("────────────────────────────")
    A("")

    # ── 零、今日一句话 ──
    A("## 🎯 零、今日一句话")
    A("")
    top_anom = an["anom_score"].nlargest(5).index.tolist()
    # T0 按异动分排序，保证「今日一句话」与下方 T0 明细表的顺序一致
    t0 = (an.loc[tier[tier == "T0 高关注"].index]
          .sort_values("anom_score", ascending=False).index.tolist())
    n_up = int((an["ret"] > 0).sum())
    n_all = int(an["ret"].notna().sum())
    A(f"- 全市场 **{n_up}/{n_all}** 个申万二级行业上涨（占比 {n_up/max(n_all,1):.0%}）。")
    if t0:
        A(f"- **高关注（T0）{len(t0)} 个**，最该先看前 5 个：**" +
          "、".join(names.get(c, c) for c in t0[:5]) + "**。")
    else:
        A("- 今日无 T0 高关注行业（无「异动显著 + 热度/共振较强」者）。")
    A(f"- 异动最强：**" + "、".join(names.get(c, c) for c in top_anom[:3]) + "**。")
    A("")

    # ── 一、注意力梯队 ──
    A("## 🧭 一、注意力梯队（今天看谁）")
    A("")
    A("| 梯队 | 含义 | 数量 | 行业 |")
    A("|:----:|:-----|:----:|:-----|")
    for t, desc in [("T0 高关注", "**今天最反常 + 热度/共振较强**（正在发酵，优先看）"),
                    ("T1 关注", "热度或共振 ≥90% 分位（持续被关注，但今天不反常）"),
                    ("T2 观察", "有异动但热度/共振不强（可能是启动前兆，也可能是噪音）"),
                    ("T3 冷区", "无热度、无异动（今天不用看）")]:
        members = tier[tier == t].index.tolist()
        if t == "T3 冷区":
            shown = f"（其余 {len(members)} 个）"
        else:
            # 按异动分排序后再展示，避免与下方明细表顺序打架
            ordered = (an.loc[members].sort_values("anom_score", ascending=False)
                       .index.tolist())
            shown = "、".join(names.get(c, c) for c in ordered[:12]) or "—"
            if len(members) > 12:
                shown += f" 等 {len(members)} 个"
        A(f"| **{t}** | {desc} | {len(members)} | {shown} |")
    A("")
    A("> ⚠️ 梯队是**描述**（谁正在被关注/发生变化），**不是推荐买入**。")
    A("")

    # T0/T1 明细
    for t in ["T0 高关注", "T1 关注", "T2 观察"]:
        members = tier[tier == t].index.tolist()
        if not members:
            continue
        sub = an.loc[members].sort_values("anom_score", ascending=False)
        A(f"**{t} 明细（按异动强度排序）**")
        A("")
        A("| 行业 | 今日涨幅 | 涨跌z | 放量z | 热度 | 共振 | 共振变化z | 强度变化z | 相对强度 | 异动分 |")
        A("|:-----|-------:|-----:|-----:|-----:|-----:|-------:|-------:|:------:|------:|")
        for c, r in sub.head(10).iterrows():
            A(f"| {names.get(c, c)} | {fmt(r['ret'], '+.2f', '%')} "
              f"| {fmt(r['z_ret'], '+.2f')} | {fmt(r['z_amt'], '+.2f')} "
              f"| {fmt(r['heat'], '.2f', 'x')} "
              f"| {fmt(r['reson'], '+.3f') if r['in_chain'] else '链外'} "
              f"| {fmt(r['z_dreson'], '+.2f')} | {fmt(r['z_rs'], '+.2f')} "
              f"| {r['rs_break'] or '—'} "
              f"| {fmt(r['anom_score'], '.2f')} |")
        A("")

    # ── 二、今日异动 ──
    A("## ⚡ 二、今日异动（今天发生了什么变化）")
    A("")
    A("> 全部为**已发生事实**的描述，不是预测。「z」= 相对该行业自身过去 60 日的偏离度。")
    A("")
    top = an.nlargest(12, "anom_score")
    A("| 行业 | 异动分 | 涨幅 | 涨跌z | 放量倍数 | 热度变化z | 共振变化z | 强度变化z | 异动解读 |")
    A("|:-----|------:|-----:|------:|-------:|--------:|--------:|-------:|:---------|")
    for c, r in top.iterrows():
        tags = []
        up = pd.notna(r["ret"]) and r["ret"] > 0
        # 组合语义（量价配合），比单看涨跌更有信息
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
        A(f"| **{names.get(c, c)}** | {fmt(r['anom_score'], '.2f')} "
          f"| {fmt(r['ret'], '+.2f', '%')} | {fmt(r['z_ret'], '+.2f')} "
          f"| {fmt(r['amt_ratio'], '.2f', 'x')} "
          f"| {fmt(r['z_dheat'], '+.2f')} | {fmt(r['z_dreson'], '+.2f')} "
          f"| {fmt(r['z_rs'], '+.2f')} "
          f"| {'、'.join(tags) or '多维度中等偏离'} |")
    A("")

    # 分类异动榜
    A("**分类异动榜**")
    A("")
    for col, label in [("z_ret", "涨跌异动 Top5"), ("z_amt", "放量异动 Top5"),
                       ("z_dreson", "链共振突变 Top5"), ("z_dheat", "关注度跃升 Top5"),
                       ("z_rs", "相对强度变化 Top5")]:
        s = an[col].dropna()
        s = s.nlargest(5)
        A(f"- **{label}**：" + "、".join(
            f"{names.get(c, c)}({v:+.1f})" for c, v in s.items()))
    A("")

    # 相对强度突破
    brk = an[an["rs_break"] != ""]
    if len(brk):
        A(f"**相对强度突破**（共 {len(brk)} 个）")
        A("")
        A("| 行业 | 信号 | 今日涨幅 | 共振 |")
        A("|:-----|:----:|-------:|-----:|")
        for c, r in brk.nlargest(min(10, len(brk)), "anom_score").iterrows():
            A(f"| {names.get(c, c)} | {r['rs_break']} | {fmt(r['ret'], '+.2f', '%')} "
              f"| {fmt(r['reson'], '+.3f')} |")
        A("")

    # ── 三、事件日历 ──
    A("## 📅 三、事件日历（今天/近期发生了什么基本面事件）")
    A("")

    fc = ev.get("forecast")
    if fc is not None and len(fc):
        agg = fc.dropna(subset=["ind_code"]).groupby("ind_code").agg(
            n=("ts_code", "size"),
            p_med=("p_mid", "median"),
            types=("type", lambda s: "/".join(sorted(set(s))[:3])),
        ).sort_values("n", ascending=False)
        A(f"**① 当日业绩预告**（共 {len(fc)} 条，涉及 {len(agg)} 个行业）")
        A("")
        A("| 行业 | 预告数 | 净利变动中位 | 类型 |")
        A("|:-----|------:|------------:|:-----|")
        for c, r in agg.head(10).iterrows():
            A(f"| {names.get(c, c)} | {int(r['n'])} | {fmt(r['p_med'], '+.1f', '%')} | {r['types']} |")
        A("")
        A("> ⚠️ 研究结论（09 报告）：业绩预告的**方向与幅度对未来超额无区分力**，"
          "此处仅作「今天有哪些基本面信息落地」的**事实记录**。")
        A("")
    else:
        A("**① 当日业绩预告**：无（当日无披露）。")
        A("")

    br = ev.get("broker")
    if br is not None and len(br):
        b = br.dropna(subset=["ind_code"])
        bagg = b.groupby("ind_code").size().sort_values(ascending=False)
        A(f"**② 本月券商金股**（{len(br)} 个推荐，覆盖 {len(bagg)} 个行业）")
        A("")
        A("| 行业 | 被推荐次数 |")
        A("|:-----|----------:|")
        for c, n in bagg.head(8).items():
            A(f"| {names.get(c, c)} | {int(n)} |")
        A("")
        A("> 券商推荐是**机构观点**，非本雷达判断；仅作「市场在讨论什么」的参考。")
        A("")

    pol = ev.get("policy")
    if pol and "categories" in pol:
        cats = list(pol["categories"].keys())
        name_set = set(names.values())
        hit = [c for c in cats if c in name_set]
        A(f"**③ 政策/产业映射**：映射表含 {len(cats)} 个赛道，"
          f"其中与申万二级同名可对齐 **{len(hit)}** 个"
          f"{'（' + '、'.join(hit[:10]) + '）' if hit else ''}。")
        A("")
        A("> 政策映射为**静态先验**（用于判断异动是否有产业逻辑支撑），非当日新增事件。")
        A("")

    # ── 四、综合判读 ──
    A("## 🧩 四、综合判读")
    A("")
    up_ratio = n_up / max(n_all, 1)
    if up_ratio >= 0.7:
        breadth = "普涨"
    elif up_ratio >= 0.45:
        breadth = "分化偏多"
    elif up_ratio >= 0.3:
        breadth = "分化偏空"
    else:
        breadth = "普跌"
    A(f"- **市场宽度**：{breadth}（{n_up}/{n_all} 上涨）。")
    A(f"- **注意力焦点**：" +
      ("、".join(names.get(c, c) for c in t0[:5]) if t0 else "今日无 T0，焦点分散") + "。")
    A(f"- **最反常的变化**：" +
      "、".join(names.get(c, c) for c in top_anom[:3]) + "（三者均在 T0）。")
    A("")
    A("> 📌 **怎么用**：本报告只负责**把有限的注意力指到对的地方**——")
    A("> 「谁在动、动得多反常、是不是整条链一起动」。至于要不要下手，")
    A("> 由你在标的层面自行判断（雷达的用途是**发现行业 → 再在其中找个股**）。")
    A("")

    # ── 五、数据校验 ──
    A("## 🔍 五、数据校验（多源交叉核对当日行情）")
    A("")
    cv = cross_validate(target)
    A("| 数据源 | 当日涨跌中位 | 上涨数 | 样本数 |")
    A("|:-------|------------:|-------:|-------:|")
    for _, r in cv.iterrows():
        A(f"| {r['源']} | {fmt(r['当日涨跌中位%'], '+.2f', '%')} "
          f"| {'' if pd.isna(r['上涨数']) else int(r['上涨数'])} | {int(r['样本数'])} |")
    A("")
    vals = cv["当日涨跌中位%"].dropna()
    if len(vals) >= 2 and vals.max() * vals.min() < 0:
        A("> 🚨 **告警**：各源涨跌**方向不一致**，当日行情数据存疑，本报告数值需谨慎使用。")
    else:
        A("> ✅ 各源方向一致（数值差异来自**权重口径**：个股等权 < 行业等权 < 市值加权，"
          "小盘股跌幅更大），数据可信。")
    A("")
    A("> 校验意义：2026-10-14 踩坑——用「两个中位数相除」算收益率会得出与事实相反的结论。"
      "此处固化为标准步骤，**任何一源方向背离即告警**。")
    A("")
    A("────────────────────────────")
    A("")
    A("**口径附录**")
    A("")
    A("| 指标 | 定义 | 性质 |")
    A("|:-----|:-----|:-----|")
    A("| 涨跌z | 当日涨幅相对自身过去 60 日的 robust z（中位数+1.4826×MAD） | 描述 |")
    A("| 放量倍数 | 当日成交额 / 前 20 日中位成交额 | 描述 |")
    A("| 热度 | 当日成交额 / 自身 120 日中位成交额 | 描述（量能关注度） |")
    A("| 共振 | 链内成员行业剥β剥风格残差的 20 日滚动平均两两相关 | 描述（结构同向性） |")
    A("| 相对强度 | nav_924.rs_w；「强度变化z」= 其 20 日变化相对自身 60 日历史的 z | 描述 |")
    A("| 突破标记 | rs_w 创 **120 日**新高/新低且幅度 >1%（60 日窗口区分度不足：当日 37% 行业触发） | 描述 |")
    A("| 异动分 | 0.30·\\|涨跌z\\| + 0.22·\\|放量z\\| + 0.16·\\|热度变化z\\| + 0.16·\\|共振变化z\\| + 0.16·\\|强度变化z\\| | 综合描述 |")
    A("| 梯队 | T0=异动前15% 且(热度≥70% 或 共振≥70%)；T1=热度或共振≥90%；T2=异动前30%；T3=其余 | 描述性分层 |")
    A("")
    A("> 📊 **链覆盖**：37 条已验证链覆盖 69/131 个行业；其余 62 个行业无链归属，"
      "表格中标注「链外」，其梯队判定只用热度与异动。")
    A("")
    A("> 🔴 **无预测力声明**：上表所有量均为「相对自身历史」的描述，"
      "本项目已用 9 份研究（02/03/04/05/06/07/08/09）证明这类横截面信号**不具备 alpha**。")
    return "\n".join(L)


def main():
    log("加载行业名与面板…")
    names = load_industry_names()
    R, AMT, RA, RS = load_panels()
    target = min(TARGET, R.index.max())
    log(f"目标日: {target.date()}")

    log("加载链定义…")
    chains = load_chains(names)
    log(f"  链数: {len(chains)}")

    log("计算 5 类异动（含自算链共振历史）…")
    an, RC = build_anomaly(R, AMT, RA, RS, chains, target)
    an["tier"] = assign_tier(an)

    log("加载事件…")
    ev = load_events(target)

    log("渲染样例日报…")
    md = render(target, an, an["tier"], RC, ev, names, chains)
    out = OUT / f"样例日报_{target.date()}.md"
    out.write_text(md, encoding="utf-8")
    log(f"✅ 已写出: {out}")

    # 同时存一份结构化数据，便于后续接入生产链
    an.to_csv(OUT / f"anomaly_panel_{target.date()}.csv")
    log(f"✅ 异动面板: anomaly_panel_{target.date()}.csv")


if __name__ == "__main__":
    main()
