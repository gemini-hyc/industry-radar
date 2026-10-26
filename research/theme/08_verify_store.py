#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主题层 · 步骤 8：从数据根 `~/quant-data/theme/` 重算，验证入库产物可用

与研究期脚本（02/04/06）的区别：本脚本**只读数据根**，不读 research/theme/ 下的
中间产物。它的作用是证明「入库后的数据能独立支撑主题层分析」，
也是未来日报「今日主题」章节的原型。

验收判据
────────
1. 2026-10-23 当日排行榜首 = 科创创业机器人（与研究期 638 全样本结果一致）
2. 行业中性超额可算（需要 theme_members + industry_members_daily + market/daily）
3. 三源交叉：主题指数 pct_chg vs 成分股加权 vs 行业层加权

用法
────
  /usr/bin/python3 research/theme/08_verify_store.py
  /usr/bin/python3 research/theme/08_verify_store.py --date 2026-10-23
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path.home() / "quant-data"
THEME = ROOT / "theme"
HERE = Path(__file__).resolve().parent


def norm_code(c: str) -> str | None:
    """Tushare `688777.SH` → 本地 `sh.688777`；非 A 股返回 None。"""
    s = str(c)
    if "." not in s:
        return None
    a, b = s.split(".", 1)
    if b.upper() not in ("SH", "SZ", "BJ"):
        return None
    return f"{b.lower()}.{a}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="默认取数据根最新交易日")
    args = ap.parse_args()

    print("=" * 78)
    print("主题层 · 数据根验收")
    print("=" * 78)

    daily = pd.read_parquet(THEME / "theme_daily.parquet")
    meta = pd.read_parquet(THEME / "theme_index_meta.parquet")
    name = dict(zip(meta["ts_code"], meta["name"]))
    daily["name"] = daily["ts_code"].map(name)

    d = args.date or str(daily["date"].max().date())
    print(f"\n数据根：{len(daily):,} 行 / {daily['ts_code'].nunique()} 个指数 / "
          f"{daily['date'].nunique()} 天")
    print(f"区间：{daily['date'].min().date()} ~ {daily['date'].max().date()}")
    print(f"分析日：{d}")

    # ── 一、当日排行（原始涨幅） ──────────────────────────
    dt = pd.Timestamp(d)
    day = daily[daily["date"] == dt]
    if len(day) == 0:
        print(f"❌ 数据根无 {d} 的数据")
        return 1

    # 基准：全样本中位（主题层没有统一的"市场"指数，用中位更稳）
    mkt = day["pct_chg"].median()
    day = day.copy()
    day["超额"] = day["pct_chg"] - mkt

    # 近 20 日动量
    win = daily[(daily["date"] > dt - pd.Timedelta(days=45)) & (daily["date"] <= dt)]
    mom = win.groupby("ts_code")["pct_chg"].apply(
        lambda s: (1 + s / 100).prod() - 1).mul(100)
    day["近20日%"] = day["ts_code"].map(mom)

    print(f"\n【一】当日涨幅 Top15（基准=当日全样本中位 {mkt:+.2f}%，{len(day)} 个指数）")
    top = day.nlargest(15, "pct_chg")
    print(f"{'主题':22s} {'当日%':>7s} {'超额%':>7s} {'近20日%':>8s}")
    for _, r in top.iterrows():
        nm = r["name"] or r["ts_code"]
        print(f"{nm[:22]:22s} {r['pct_chg']:+7.2f} {r['超额']:+7.2f} "
              f"{r['近20日%'] if pd.notna(r['近20日%']) else float('nan'):+8.1f}")

    # ── 二、行业中性超额（用成分股落地） ────────────────────
    mem_path = THEME / "theme_members.parquet"
    if not mem_path.exists():
        print("\n【二】⚠️ 数据根无 theme_members，跳过行业中性超额")
        return 0

    mem = pd.read_parquet(mem_path)
    snap = str(mem["trade_date"].max())
    mem = mem[mem["trade_date"] == snap]
    mem["code"] = mem["con_code"].map(norm_code)
    mem = mem[mem["code"].notna()]
    print(f"\n【二】行业中性超额（成分快照 {snap}，{len(mem):,} 行 / "
          f"{mem['ts_code'].nunique()} 个指数）")

    md = pd.read_parquet(ROOT / "market" / "daily")
    md = md[md["date"] == dt]
    px = dict(zip(md["code"], md["pctChg"]))

    mb = pd.read_parquet(ROOT / "industry" / "industry_members_daily.parquet")
    mb = mb[mb["date"] == dt]
    stk_ind = dict(zip(mb["stock_code"], mb["ind_code"]))

    wr = pd.read_parquet(ROOT / "industry" / "industry_weighted_returns.parquet")
    wr = wr[wr["date"] == dt]
    ind_pct = dict(zip(wr["ind_code"], wr["weighted_pct"]))

    m = mem.copy()
    m["个股当日%"] = m["code"].map(px)
    m["ind_code"] = m["code"].map(stk_ind)
    m["行业当日%"] = m["ind_code"].map(ind_pct)
    m = m[m["个股当日%"].notna()]

    def excess(g):
        w = g["weight"] / g["weight"].sum()
        raw = float((g["个股当日%"] * w).sum())
        ind = g["行业当日%"]
        neu = float((g["个股当日%"] * w).sum() - (ind.fillna(mkt) * w).sum()) \
            if ind.notna().any() else float("nan")
        return pd.Series({"原始加权%": raw, "行业中性%": neu, "n": len(g)})

    ex = m.groupby("ts_code").apply(excess, include_groups=False)
    ex["name"] = ex.index.map(name)
    ex = ex[ex["n"] >= 10]

    print(f"\nTop15（成分数 ≥10，{len(ex)} 个主题参与）")
    print(f"{'主题':22s} {'原始%':>7s} {'行业中性%':>9s} {'成分数':>6s}")
    for ts, r in ex.nlargest(15, "原始加权%").iterrows():
        nm = r["name"] or ts
        print(f"{nm[:22]:22s} {r['原始加权%']:+7.2f} {r['行业中性%']:+9.2f} {int(r['n']):6d}")

    print(f"\n行业中性 Top10（这个口径能捞出「涨幅不亮眼但自己在动」的主题）")
    for ts, r in ex.nlargest(10, "行业中性%").iterrows():
        nm = r["name"] or ts
        print(f"{nm[:22]:22s} 原始 {r['原始加权%']:+7.2f} → 中性 {r['行业中性%']:+7.2f} "
              f"（{int(r['n'])} 只）")

    # ── 三、交叉校验：指数 pct_chg vs 成分加权 ──────────────
    both = ex.join(day.set_index("ts_code")[["pct_chg", "name"]],
                   how="inner", rsuffix="_idx")
    if len(both):
        diff = (both["原始加权%"] - both["pct_chg"]).abs()
        print(f"\n【三】交叉校验：指数官方 pct_chg vs 本地成分加权")
        print(f"  参与 {len(both)} 个主题；绝对差中位 {diff.median():.2f}pp，"
              f"p90 {diff.quantile(.9):.2f}pp，最大 {diff.max():.2f}pp")
        print("  （差异来自：成分快照滞后于当日、本地剔除港股/美股成分、权重归一）")

    print("\n✅ 验收完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
