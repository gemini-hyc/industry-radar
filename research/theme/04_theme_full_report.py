#!/usr/bin/env python3
"""
主题层 · 步骤 4：全量主题指数排行 + 数据质量校验

回答三个问题
────────────
1. 全样本（673 个）下，2026-10-23 最热的主题是什么？与 144 原型口径是否一致？
2. 数据质量能不能支撑生产（覆盖度 / 异常值 / 僵尸指数）？
3. 主题层相对申万二级行业层，增值在哪（纯度、命名度）？

用法
────
  /usr/bin/python3 research/theme/04_theme_full_report.py
"""
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
D = "20261023"
BENCH_CODE = "000985.CSI"


def load():
    df = pd.read_parquet(HERE / "_theme_daily_full.parquet")
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def quality(df):
    print("=" * 78)
    print("【一】数据质量校验")
    print("=" * 78)
    n = df["ts_code"].nunique()
    print(f"指数数 {n}   总行数 {len(df):,}   日期 {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    dup = df.duplicated(["ts_code", "trade_date"]).sum()
    print(f"重复(指数,日期) 行数: {dup}")
    print(f"pct_chg 缺失: {df['pct_chg'].isna().sum()} ({df['pct_chg'].isna().mean()*100:.2f}%)")
    print(f"close  缺失: {df['close'].isna().sum()}")

    cover = df.groupby("ts_code").agg(
        天数=("trade_date", "size"), 起=("trade_date", "min"), 止=("trade_date", "max")
    )
    print(f"\n历史长度: 中位 {cover['天数'].median():.0f} 天  均值 {cover['天数'].mean():.0f} 天")
    bins = [0, 20, 60, 120, 250, 500, 10**9]
    labels = ["<20天", "20-60天", "60-120天", "120-250天", "250-500天", ">=500天"]
    vc = pd.cut(cover["天数"], bins=bins, labels=labels, right=False).value_counts().reindex(labels)
    for k, v in vc.items():
        print(f"  {k:>10s}: {int(v):4d} 个 ({v/len(cover)*100:5.1f}%)")

    # 僵尸指数：近 20 日零成交 / 无更新
    last = df[df["trade_date"] == D]
    stale = cover[cover["止"] < D]
    print(f"\n当日(2026-10-23)有行情的指数: {len(last)} / {n}")
    print(f"末日早于当日的指数(已停更): {len(stale)}")
    zomb = last[last["amount"].fillna(0) <= 0]
    print(f"当日成交额为 0 的(僵尸): {len(zomb)}")
    if len(zomb):
        print("  样例:", list(zomb["name"])[:5])
    return cover


def ranking(df, cover):
    print("\n" + "=" * 78)
    print("【二】2026-10-23 全样本主题排行")
    print("=" * 78)
    bench = df[df["ts_code"] == BENCH_CODE].set_index("trade_date")["pct_chg"]
    if bench.empty:
        bench = df[df["ts_code"] == "000300.SH"].set_index("trade_date")["pct_chg"]
    b = float(bench.get(D, float("nan")))
    print(f"基准 {BENCH_CODE} 当日 {b:+.2f}%\n")

    day = df[df["trade_date"] == D].copy()
    day["天数"] = day["ts_code"].map(cover["天数"])
    day["超额"] = day["pct_chg"] - b

    hist = df[df["trade_date"] <= D].sort_values("trade_date")

    def mom(g, win=20):
        s = g["pct_chg"].tail(win)
        return (1 + s / 100).prod() * 100 - 100 if len(s) >= 5 else float("nan")

    day = day.merge(hist.groupby("ts_code").apply(mom).rename("近20日%"),
                    on="ts_code", how="left")

    cols = ["name", "ts_code", "pct_chg", "超额", "近20日%", "天数"]
    fm = {"pct_chg": "{:+.2f}".format, "超额": "{:+.2f}".format, "近20日%": "{:+.1f}".format}
    print(f"--- 当日涨幅 Top20（全样本 {len(day)} 个）---")
    print(day.sort_values("pct_chg", ascending=False).head(20)[cols].to_string(
        index=False, formatters=fm))
    print(f"\n--- 当日涨幅 Bottom5 ---")
    print(day.sort_values("pct_chg").head(5)[cols].to_string(index=False, formatters=fm))
    return day


def key_concepts(day):
    print("\n" + "=" * 78)
    print("【三】关键概念覆盖检验（中证缺的是哪些）")
    print("=" * 78)
    for kw in ["机器人", "航天", "航空", "人工智能", "算力", "低空", "半导体",
               "电池", "固态", "光伏", "军工", "创新药", "稀土", "光模块", "卫星"]:
        hit = day[day["name"].str.contains(kw, na=False)]
        if hit.empty:
            print(f"  「{kw}」: 0 个 ❌ 无指数覆盖")
        else:
            h = hit.sort_values("pct_chg", ascending=False)
            s = "  ".join(f"{r['name']}={r['pct_chg']:+.2f}%" for _, r in h.head(3).iterrows())
            print(f"  「{kw}」: {len(hit):3d} 个  前三 -> {s}")


def compare_with_proto(day):
    """与 144 原型对比：口径是否一致（机器人是否仍第一）。"""
    print("\n" + "=" * 78)
    print("【四】与 144 原型口径一致性")
    print("=" * 78)
    proto = HERE / "_theme_daily.parquet"
    if not proto.exists():
        print("  原型文件不存在，跳过"); return
    p = pd.read_parquet(proto)
    p["trade_date"] = p["trade_date"].astype(str)
    pd_day = p[p["trade_date"] == D]
    pt = pd_day.sort_values("pct_chg", ascending=False).head(5)
    print(f"  原型(144) Top5: " + "  ".join(f"{r['name']}={r['pct_chg']:+.2f}%" for _, r in pt.iterrows()))
    ft = day.sort_values("pct_chg", ascending=False).head(5)
    print(f"  全量(673) Top5: " + "  ".join(f"{r['name']}={r['pct_chg']:+.2f}%" for _, r in ft.iterrows()))
    top1p, top1f = pt.iloc[0]["name"], ft.iloc[0]["name"]
    print(f"  ⇒ 榜首一致: {'✅' if top1p == top1f else f'❌ 原型={top1p} 全量={top1f}'}")


def vs_industry(day):
    """与申万二级行业层对比：主题层增值（命名 + 纯度）。"""
    print("\n" + "=" * 78)
    print("【五】主题层 vs 申万二级行业层")
    print("=" * 78)
    DATA = Path("/Users/hyc/quant-data")
    try:
        w = pd.read_parquet(DATA / "industry/industry_weighted_returns.parquet")
        il = pd.read_parquet(DATA / "industry/industry_list.parquet")
        w = w[w["date"] == "2026-10-23"]
        name = dict(zip(il["ts_code"], il["name"]))
        w["name"] = w["ind_code"].map(name)
        top_i = w.sort_values("weighted_pct", ascending=False).head(8)
        print("  行业层 Top8（申万二级加权收益）:")
        print("   " + "  ".join(f"{r['name']}={r['weighted_pct']:+.2f}%" for _, r in top_i.iterrows()))
        tbest = day.sort_values("pct_chg", ascending=False).iloc[0]
        ibest = top_i.iloc[0]
        print(f"\n  主题层榜首 {tbest['name']} = {tbest['pct_chg']:+.2f}%")
        print(f"  行业层榜首 {ibest['name']} = {ibest['weighted_pct']:+.2f}%")
        print(f"  ⇒ 主题层纯度增益 {tbest['pct_chg'] - ibest['weighted_pct']:+.2f}pp")
    except Exception as e:
        print(f"  行业数据读取失败: {type(e).__name__} {str(e)[:80]}")


def main() -> int:
    df = load()
    cover = quality(df)
    day = ranking(df, cover)
    key_concepts(day)
    compare_with_proto(day)
    vs_industry(day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
