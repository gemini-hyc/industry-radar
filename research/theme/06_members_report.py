#!/usr/bin/env python3
"""
主题层 · 步骤 6：成分股分析 —— 验证「落地个股」与「行业中性超额」

两件事
──────
① 主题 → 个股：主题能不能真的指向具体标的（雷达最后一环）
② 主题 → 申万二级映射 → **行业中性的当日超额**：
   把主题成分按所属行业分组，扣掉行业普涨部分，剩下的才是主题自身的超额。
   这是主题层区别于行业层的核心指标。

用法
────
  /usr/bin/python3 research/theme/06_members_report.py
"""
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = Path("/Users/hyc/quant-data")
D = "2026-10-23"          # 行业/个股表用 date
DT = "20261023"           # Tushare 表用 trade_date
SNAP = "20260930"         # 使用的成分快照


def norm_code(c: str):
    """Tushare `688777.SH` → 本地 `sh.688777`（本地 market/daily 与
    industry_members_daily 都用小写前缀加点号格式）。

    ⚠️ 成分里混有港股（`#####.HK`，3422 条）与美股代码（`PDD`/`NVDA` 等 251 条），
    它们不在本地行情表内 ⇒ 返回 None，后续 join 自然为 NaN 并被 dropna 剔除。
    """
    c = str(c)
    if "." not in c:
        return None
    a, b = c.split(".")
    if b not in ("SH", "SZ", "BJ"):
        return None
    return f"{b.lower()}.{a}"


def main() -> int:
    mem = pd.read_parquet(HERE / "_theme_members.parquet")
    mem["code"] = mem["con_code"].map(norm_code)
    print("=" * 78)
    print("【一】成分股覆盖度")
    print("=" * 78)
    print(f"指数数 {mem['ts_code'].nunique()}   快照日 {sorted(mem['trade_date'].unique())}")
    print(f"行数 {len(mem):,}   个股数 {mem['con_code'].nunique()}")
    sz = mem.groupby(["ts_code", "trade_date"]).size()
    print(f"\n单指数成分数: 中位 {sz.median():.0f}  均值 {sz.mean():.0f}  "
          f"最小 {sz.min()}  最大 {sz.max()}")

    snap = mem[mem["trade_date"] == SNAP].copy()
    print(f"\n使用快照 {SNAP}: {snap['ts_code'].nunique()} 个指数 / {len(snap):,} 行")

    # ① 主题 → 个股
    print("\n" + "=" * 78)
    print("【二】主题 → 个股落地（以 2026-10-23 榜首主题为例）")
    print("=" * 78)
    try:
        md = pd.read_parquet(DATA / "market/daily")
        md = md[md["date"] == D]
        px = md.set_index("code")["pctChg"]
    except Exception as e:
        print(f"  行情读取失败: {e}"); return 1

    full = pd.read_parquet(HERE / "_theme_daily_full.parquet")
    full["trade_date"] = full["trade_date"].astype(str)
    day = full[full["trade_date"] == DT].sort_values("pct_chg", ascending=False)
    top = day.iloc[0]
    print(f"榜首主题: {top['name']} ({top['ts_code']})  当日 {top['pct_chg']:+.2f}%")

    hold = snap[snap["ts_code"] == top["ts_code"]].copy()
    if hold.empty:
        print(f"  ⚠️ {top['ts_code']} 在快照 {SNAP} 无成分，改用最新快照")
        latest = mem["trade_date"].max()
        hold = mem[(mem["ts_code"] == top["ts_code"]) & (mem["trade_date"] == latest)].copy()
    hold["个股当日%"] = hold["code"].map(px)
    hold = hold.sort_values("weight", ascending=False)
    print(f"\n  成分 Top15（按权重，快照 {hold['trade_date'].iloc[0]}）:")
    print(hold.head(15)[["con_code", "weight", "个股当日%"]].to_string(
        index=False, formatters={"weight": "{:.2f}".format, "个股当日%": "{:+.2f}".format}))
    hit = hold["个股当日%"].dropna()
    print(f"\n  成分当日涨幅: 中位 {hit.median():+.2f}%  均值 {hit.mean():+.2f}%  "
          f"上涨 {(hit>0).mean()*100:.0f}%   涨停(≥9.8%) {(hit>=9.8).sum()} 只")

    # ② 主题 → 申万二级 → 行业中性超额
    print("\n" + "=" * 78)
    print("【三】行业中性超额（核心指标）")
    print("=" * 78)
    try:
        mb = pd.read_parquet(DATA / "industry/industry_members_daily.parquet")
        mb = mb[mb["date"] == D]
        il = pd.read_parquet(DATA / "industry/industry_list.parquet")
        ind_name = dict(zip(il["ts_code"], il["name"]))
        w = pd.read_parquet(DATA / "industry/industry_weighted_returns.parquet")
        w = w[w["date"] == D]
        ind_ret = dict(zip(w["ind_code"], w["weighted_pct"]))
    except Exception as e:
        print(f"  行业数据读取失败: {e}"); return 1

    m = hold.merge(mb[["ind_code", "stock_code"]], left_on="code",
                   right_on="stock_code", how="left")
    m["行业"] = m["ind_code"].map(ind_name).fillna("(未分类)")
    m["行业当日%"] = m["ind_code"].map(ind_ret)
    m["个股超额"] = m["个股当日%"] - m["行业当日%"]

    grp = m.dropna(subset=["个股当日%"]).groupby("行业").agg(
        只数=("con_code", "size"), 权重和=("weight", "sum"),
        个股中位=("个股当日%", "median"), 行业中位=("行业当日%", "median"),
        超额中位=("个股超额", "median"))
    grp = grp.sort_values("只数", ascending=False)
    print(f"  「{top['name']}」的成分分布在 {len(grp)} 个申万二级行业:")
    print(grp.head(10).to_string(formatters={
        "权重和": "{:.1f}".format, "个股中位": "{:+.2f}".format,
        "行业中位": "{:+.2f}".format, "超额中位": "{:+.2f}".format}))

    wa = m.dropna(subset=["个股超额", "weight"])
    raw = (wa["个股当日%"] * wa["weight"]).sum() / wa["weight"].sum()
    neutral = (wa["个股超额"] * wa["weight"]).sum() / wa["weight"].sum()
    print(f"\n  主题当日加权收益（原始）      : {raw:+.2f}%")
    print(f"  主题当日加权收益（行业中性）  : {neutral:+.2f}%")
    print(f"  ⇒ 扣掉行业普涨后，主题自身超额 = {neutral:+.2f}%")

    # 全市场对照：所有主题的行业中性超额 Top10
    print("\n" + "=" * 78)
    print("【四】全主题「行业中性超额」Top10（当日）")
    print("=" * 78)
    stk_ind = dict(zip(mb["stock_code"], mb["ind_code"]))
    allm = snap.copy()
    allm["个股当日%"] = allm["code"].map(px)
    allm["ind_code"] = allm["code"].map(stk_ind)
    allm["行业当日%"] = allm["ind_code"].map(ind_ret)
    allm["个股超额"] = allm["个股当日%"] - allm["行业当日%"]

    def agg(g):
        g = g.dropna(subset=["个股超额", "weight"])
        if not len(g) or g["weight"].sum() <= 0:
            return pd.Series({"只数": 0, "原始": float("nan"), "行业中性": float("nan")})
        return pd.Series({
            "只数": len(g),
            "原始": (g["个股当日%"] * g["weight"]).sum() / g["weight"].sum(),
            "行业中性": (g["个股超额"] * g["weight"]).sum() / g["weight"].sum(),
        })

    res = allm.groupby("ts_code").apply(agg)
    res = res[res["只数"] >= 5]
    nm = full.drop_duplicates("ts_code").set_index("ts_code")["name"]
    res["name"] = res.index.map(nm)
    res = res.sort_values("行业中性", ascending=False)
    print(res.head(10)[["name", "只数", "原始", "行业中性"]].to_string(
        formatters={"原始": "{:+.2f}".format, "行业中性": "{:+.2f}".format}))
    print("\n  --- 行业中性超额 Bottom5 ---")
    print(res.tail(5)[["name", "只数", "原始", "行业中性"]].to_string(
        formatters={"原始": "{:+.2f}".format, "行业中性": "{:+.2f}".format}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
