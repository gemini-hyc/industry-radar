#!/usr/bin/env python3
"""
05z · B 口径（进→出 区间持有）长尾拆解
读取 05y 产出的逐事件 CSV，按住仓天数分桶，看「B 的中位为正、均值为零」到底
是长尾亏损造成、还是别的结构。只读 CSV，不重算、不改任何产物。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("/Users/hyc/quant/industry-radar/research/prosperity")


def decomp(metric: str):
    df = pd.read_csv(OUT / f"05y_anchor_events_{metric}.csv",
                     parse_dates=["entry_date", "exit_date"])
    print(f"\n{'#'*70}\n# 指标 = {metric}   （全部进事件 {len(df)}）\n{'#'*70}")

    b = df.dropna(subset=["B_ex"]).copy()
    censored = df[df["B_ex"].isna()]
    print(f"完成『出』的 B 交易 = {len(b)}；未完成（右删失）= {len(censored)}")

    print(f"\nB_ex 总体：中位 {b['B_ex'].median()*100:+.2f}%  均值 {b['B_ex'].mean()*100:+.2f}%  "
          f"胜率 {(b['B_ex']>0).mean()*100:.1f}%  偏度 {b['B_ex'].skew():+.2f}")
    print(f"B_hold 总体：中位 {b['B_hold'].median():.0f}  均值 {b['B_hold'].mean():.1f}  "
          f"最大 {b['B_hold'].max():.0f}")
    print(f"B_hold 与 B_ex 相关系数 = {b['B_hold'].corr(b['B_ex']):+.3f}")

    # 按持有天数分桶
    bins = [-1, 20, 60, 120, 250, 500, 10 ** 9]
    labels = ["≤20", "21-60", "61-120", "121-250", "251-500", ">500"]
    b["bucket"] = pd.cut(b["B_hold"], bins=bins, labels=labels)
    print("\n按住仓天数分桶：")
    print(f"{'桶':>9}{'n':>6}{'占比':>8}{'中位':>10}{'均值':>10}{'胜率':>9}{'累计占比':>10}")
    tot = len(b)
    cum = 0
    for lab in labels:
        sub = b[b["bucket"] == lab]
        if len(sub) == 0:
            continue
        cum += len(sub)
        print(f"{lab:>9}{len(sub):>6}{len(sub)/tot*100:>7.1f}%"
              f"{sub['B_ex'].median()*100:>+9.2f}%{sub['B_ex'].mean()*100:>+9.2f}%"
              f"{(sub['B_ex']>0).mean()*100:>8.1f}%{cum/tot*100:>9.1f}%")

    # 左尾：最差的 10 笔
    print("\nB_ex 最差的 10 笔（左尾）：")
    print(f"{'行业':>12}{'进':>12}{'出':>12}{'持有天':>8}{'B_ex':>10}")
    for _, r in b.nsmallest(10, "B_ex").iterrows():
        print(f"{r['ind']:>12}{str(r['entry_date'].date()):>12}{str(r['exit_date'].date()):>12}"
              f"{int(r['B_hold']):>8}{r['B_ex']*100:>+9.2f}%")

    # 右尾：最好的 5 笔
    print("\nB_ex 最好的 5 笔（右尾）：")
    for _, r in b.nlargest(5, "B_ex").iterrows():
        print(f"{r['ind']:>12}{str(r['entry_date'].date()):>12}{str(r['exit_date'].date()):>12}"
              f"{int(r['B_hold']):>8}{r['B_ex']*100:>+9.2f}%")

    # 把长尾（>250 天）剔除后重算
    short = b[b["B_hold"] <= 250]
    print(f"\n剔除持有>250天的样本后（剩 {len(short)} 笔）："
          f"中位 {short['B_ex'].median()*100:+.2f}%  均值 {short['B_ex'].mean()*100:+.2f}%  "
          f"胜率 {(short['B_ex']>0).mean()*100:.1f}%")

    # 删失样本按「买入至样本末」的近似未实现超额（用 A_250）看分布
    if len(censored) and "A_250" in df.columns:
        cc = censored["A_250"].dropna()
        print(f"\n删失 {len(censored)} 笔的 A_250（买入至样本末的近似未实现超额）："
              f"中位 {cc.median()*100:+.2f}%  均值 {cc.mean()*100:+.2f}%")


if __name__ == "__main__":
    for mt in ["pb", "pe_ttm"]:
        decomp(mt)
