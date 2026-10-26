#!/usr/bin/env python3
"""
主题层可行性验证 · 步骤 2：周五主题排行，回答「今天最火的概念是什么」

与申万二级行业层对照，检验主题层是否真的能「点名」热点。

用法
────
  /usr/bin/python3 research/theme/02_theme_ranking.py
"""
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
D = "20261023"
DATA = Path("/Users/hyc/quant-data")


def main() -> int:
    df = pd.read_parquet(HERE / "_theme_daily.parquet")
    df["trade_date"] = df["trade_date"].astype(str)
    print(f"载入 {df['ts_code'].nunique()} 个指数 / {len(df)} 行")

    # 基准
    bench = df[df["ts_code"] == "000985.CSI"].set_index("trade_date")["pct_chg"]
    if bench.empty:
        bench = df[df["ts_code"] == "000300.SH"].set_index("trade_date")["pct_chg"]
    b_day = float(bench.get(D, float("nan")))
    print(f"基准当日: {b_day:+.2f}%\n")

    # 每个主题的覆盖天数（判断历史是否够用）
    cover = df.groupby("ts_code").agg(天数=("trade_date", "size")).reset_index()
    names = df.drop_duplicates("ts_code").set_index("ts_code")["name"]
    cover["name"] = cover["ts_code"].map(names)

    day = df[df["trade_date"] == D].copy()
    day["天数"] = day["ts_code"].map(cover.set_index("ts_code")["天数"])
    day["超额"] = day["pct_chg"] - b_day

    # 近 20 日动量
    hist = df[df["trade_date"] <= D].sort_values("trade_date")
    def mom(g):
        s = g["pct_chg"].tail(20)
        return (1 + s / 100).prod() * 100 - 100 if len(s) >= 5 else float("nan")
    m20 = hist.groupby("ts_code").apply(mom).rename("近20日%")

    day = day.merge(m20, on="ts_code", how="left")

    print("=" * 74)
    print(f"【主题层】{D} 当日涨幅 Top15（对照基准 {b_day:+.2f}%）")
    print("=" * 74)
    top = day.sort_values("pct_chg", ascending=False).head(15)
    print(top[["name", "pct_chg", "超额", "近20日%", "天数"]]
          .rename(columns={"pct_chg": "当日%"}).to_string(index=False,
          formatters={"当日%": "{:+.2f}".format, "超额": "{:+.2f}".format,
                      "近20日%": "{:+.1f}".format}))

    print("\n" + "=" * 74)
    print("【主题层】当日涨幅 Bottom5")
    print("=" * 74)
    bot = day.sort_values("pct_chg").head(5)
    print(bot[["name", "pct_chg", "超额", "近20日%"]]
          .rename(columns={"pct_chg": "当日%"}).to_string(index=False,
          formatters={"当日%": "{:+.2f}".format, "超额": "{:+.2f}".format,
                      "近20日%": "{:+.1f}".format}))

    # 关键：机器人 / 商业航天 是否被测到
    print("\n" + "=" * 74)
    print("【关键检验】当红概念能否被点名")
    print("=" * 74)
    for kw in ["机器人", "航天", "航空", "人工智能", "算力", "低空", "半导体", "电池"]:
        hit = day[day["name"].str.contains(kw, na=False)]
        if hit.empty:
            print(f"  「{kw}」: 无对应主题指数 ❌")
        else:
            h = hit.sort_values("pct_chg", ascending=False)
            s = "  ".join(f"{r['name']}({r['pct_chg']:+.2f}%,{r['天数']}天史)" for _, r in h.head(3).iterrows())
            print(f"  「{kw}」: {len(hit)} 个 -> {s}")

    # 历史覆盖度
    print("\n" + "=" * 74)
    print("【历史覆盖度】主题指数的历史长度分布（决定能否算 z / 动量）")
    print("=" * 74)
    bins = [0, 5, 20, 60, 120, 250, 9999]
    labels = ["<5天", "5-20天", "20-60天", "60-120天", "120-250天", ">=250天"]
    cover["档"] = pd.cut(cover["天数"], bins=bins, labels=labels, right=False)
    vc = cover["档"].value_counts().reindex(labels)
    for k, v in vc.items():
        print(f"  {k:>10s}: {v:4d} 个 ({v/len(cover)*100:5.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
