#!/usr/bin/env python3
"""
05ai · 加权口径修复后的整表重建校验

对照对象：
  - 旧表（同日市值加权）  ~/quant-data/industry/_backup_20261015_weight_fix/*.bak-sameday-weight
  - 外部指数              中证全指 000985.CSI / 沪深300 000300.SH / 上证 000001.SH

检查项：
  1. 形状与schema不变（行数 / 行业数 / 日期数 / 列集合 / 日期范围）
  2. 市场收益序列（按 circ_mv 再加权）逐年 vs 指数 —— 应贴合而非系统性虚增
  3. 日频相关性 —— 应与指数高度相关且与旧表几乎同源（只是少了正漂移）
  4. 极端值收敛 —— 旧表里「不可能」的单日行业涨幅应显著减少
  5. 每日偏移量 old-new ≈ +Var_w(r) ≈ +6bp（理论预测）
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DATA = Path("/Users/hyc/quant-data")
NEW_PATH = DATA / "industry" / "industry_weighted_returns.parquet"
OLD_PATH = (DATA / "industry" / "_backup_20261015_weight_fix"
            / "industry_weighted_returns.parquet.bak-sameday-weight")


def load(p):
    d = pd.read_parquet(p)
    d["date"] = pd.to_datetime(d["date"])
    return d.sort_values(["date", "ind_code"]).reset_index(drop=True)


def market_series(wr):
    """按 circ_mv 把行业收益再加权成「市场」日收益序列。"""
    g = wr.groupby("date")
    return (g.apply(lambda d: (d["circ_mv"] * d["weighted_pct"]).sum() / d["circ_mv"].sum(),
                    include_groups=False) / 100.0).sort_index()


def index_ret(code):
    p = DATA / "index_daily" / f"{code}.parquet"
    if not p.exists():
        return None
    ix = pd.read_parquet(p)
    dc = [c for c in ix.columns if "date" in c.lower()][0]
    ix[dc] = pd.to_datetime(ix[dc])
    ix = ix.set_index(dc).sort_index()
    c = [x for x in ix.columns if x.lower().startswith("close")][0]
    return ix[c].pct_change().dropna().rename(code)


def main():
    new, old = load(NEW_PATH), load(OLD_PATH)

    print("=" * 78)
    print("1. 形状 / schema 对齐")
    print("=" * 78)
    rows = [("行数", len(old), len(new)),
            ("行业数", old["ind_code"].nunique(), new["ind_code"].nunique()),
            ("日期数", old["date"].nunique(), new["date"].nunique())]
    for k, a, b in rows:
        print(f"  {k:<8} 旧 {a:>8}   新 {b:>8}   {'✅ 一致' if a == b else '❌ 不一致'}")
    print(f"  日期范围   旧 {old['date'].min().date()} ~ {old['date'].max().date()}")
    print(f"             新 {new['date'].min().date()} ~ {new['date'].max().date()}")
    print(f"  列集合 一致: {set(old.columns) == set(new.columns)}   {list(new.columns)}")
    missing_dates = set(old["date"]) - set(new["date"])
    extra_dates = set(new["date"]) - set(old["date"])
    print(f"  缺失日期 {len(missing_dates)} 个，新增日期 {len(extra_dates)} 个")

    # ── 拼接日频对照 ──
    j = old.merge(new, on=["date", "ind_code"], how="inner", suffixes=("_old", "_new"))
    print(f"  可逐格对照行数: {len(j)}")
    diff = j["weighted_pct_new"] - j["weighted_pct_old"]
    print(f"  单格差异 (新-旧): 中位 {diff.median():+.4f}pp   "
          f"均值 {diff.mean():+.4f}pp   P1 {diff.quantile(.01):+.3f} / P99 {diff.quantile(.99):+.3f}")

    print()
    print("=" * 78)
    print("2. 市场收益序列 vs 外部指数（逐年累计）")
    print("=" * 78)
    mo, mn = market_series(old), market_series(new)
    indices = {c: index_ret(c) for c in ["000985.CSI", "000300.SH", "000001.SH"]}
    # 只用共同区间对账
    print(f"{'年份':<6}{'旧·自建市场':>13}{'新·自建市场':>13}{'中证全指':>12}"
          f"{'沪深300':>11}{'上证指数':>11}{'新-全指':>10}")
    for y in sorted(set(mn.index.year)):
        s = f"{y}-01-01"
        e = f"{y}-12-31"
        def cum(ser):
            x = ser[(ser.index >= s) & (ser.index <= e)]
            return (1 + x).prod() - 1 if len(x) else np.nan
        co, cn = cum(mo), cum(mn)
        ci = {k: cum(v) for k, v in indices.items() if v is not None}
        base = ci.get("000985.CSI", np.nan)
        gap = (cn - base) if base == base else np.nan
        print(f"{y:<6}{co*100:>12.2f}%{cn*100:>12.2f}%"
              f"{ci.get('000985.CSI', np.nan)*100:>11.2f}%"
              f"{ci.get('000300.SH', np.nan)*100:>10.2f}%"
              f"{ci.get('000001.SH', np.nan)*100:>10.2f}%"
              f"{gap*100:>+9.2f}pp")
    def full(ser):
        return (1 + ser).prod() - 1
    print(f"\n  全区间累计  旧 {full(mo)*100:+.1f}%   新 {full(mn)*100:+.1f}%   "
          f"中证全指 {full(indices['000985.CSI'].loc[mn.index.min():mn.index.max()])*100:+.1f}%")

    print()
    print("=" * 78)
    print("3. 日频质量")
    print("=" * 78)
    base = indices["000985.CSI"]
    common = mn.index.intersection(base.index)
    for name, ser in [("旧", mo), ("新", mn)]:
        s = ser.loc[common]
        b = base.loc[common]
        daily_gap = (s - b).mean() * 100
        print(f"  {name}: 与中证全指日相关 {s.corr(b):.4f}   "
              f"日均偏离 {daily_gap:+.4f}pp（年化 ≈ {daily_gap*244:+.1f}%）   "
              f"日收益均值 {s.mean()*100:+.4f}%")
    print(f"  旧-新 日均差 = {((mo - mn).mean()*100):+.4f}pp   "
          f"（理论 Var_w(r) ≈ {(mo - mn).var()**0.5*100/ (244**0.5):.4f} 的尺度参考："
          f"日差标准差 {((mo-mn).std()*100):.3f}pp）")

    print()
    print("=" * 78)
    print("4. 极端值收敛")
    print("=" * 78)
    for name, df in [("旧", old), ("新", new)]:
        v = df["weighted_pct"]
        print(f"  {name}: max {v.max():+.2f}%  min {v.min():+.2f}%   "
              f"|值|>25% 的行业-天 {(v.abs() > 25).sum()} 个   "
              f"|值|>15% {(v.abs() > 15).sum()} 个   std {v.std():.3f}")
    big_old = old[old["weighted_pct"].abs() > 25]
    if len(big_old):
        print(f"  旧表 |∏|>25% 明细（前 5）:")
        for _, r in big_old.reindex(big_old["weighted_pct"].abs().sort_values(
                ascending=False).index).head(5).iterrows():
            m = new[(new["date"] == r["date"]) & (new["ind_code"] == r["ind_code"])]
            nv = m["weighted_pct"].iloc[0] if len(m) else np.nan
            print(f"    {r['date'].date()} {r['ind_code']:<14} "
                  f"旧 {r['weighted_pct']:+8.2f}%  →  新 {nv:+8.2f}%  ({r['stock_n']}只)")

    print()
    print("=" * 78)
    print("5. 修正幅度在行业间的异质性（为什么要修：不是常数偏移）")
    print("=" * 78)
    j2 = j.copy()
    j2["gap"] = j2["weighted_pct_old"] - j2["weighted_pct_new"]
    per = j2.groupby("ind_code")["gap"].mean().sort_values()
    print(f"  逐行业日均虚增 (旧-新): 最小 {per.min():+.4f}pp  中位 {per.median():+.4f}pp  "
          f"最大 {per.max():+.4f}pp")
    print(f"  ⇒ 虚增在低/高分布行业间相差 {(per.max()-per.min()):.4f}pp/日"
          f"（年化 {((per.max()-per.min())*244):.1f}pp）"
          f"{'  ⚠️ 非等幅 ⇒ 在 EX=R-mkt 中不抵消' if (per.max()-per.min()) > 0.01 else ''}")
    print("\n  虚增最少 5 个行业（被压低估最轻）:")
    for k, v in per.head(5).items():
        print(f"    {k:<14} {v:+.4f}pp/日")
    print("  虚增最多 5 个行业:")
    for k, v in per.tail(5).items():
        print(f"    {k:<14} {v:+.4f}pp/日")

    # 落盘
    out = Path(__file__).parent / "05ai_rebuild_validation.csv"
    market_cmp = pd.DataFrame({"mkt_old": mo, "mkt_new": mn}).join(
        base.rename("csi985"), how="left")
    market_cmp.to_csv(out, encoding="utf-8-sig")
    print(f"\n✓ 市场序列对照已落盘: {out}")


if __name__ == "__main__":
    main()
