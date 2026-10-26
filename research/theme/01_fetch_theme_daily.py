#!/usr/bin/env python3
"""
主题层可行性验证 · 步骤 1：拉取中证/国证产业主题指数日线（原型，不进数据根）

目的
────
验证「用主题指数给热点命名」这条路能不能走通。只拉代表性子集做原型，
不建全库；产物落在 research/theme/ 下，不写 ~/quant-data。

背景（2026-10-25 探测结论）
────
- Tushare `ths_index` / `ths_daily`（同花顺概念指数）：**无权限**，走不通。
- Tushare `index_daily`（中证/国证指数日线）：**有权限且有真实历史**（PIT 安全）。
- 中证主题指数 1990 个、去重变体后 1129 个、产业主题段 843 个。
- ⚠️ 覆盖滞后：商业航天指数 932720.CSI 只有 4 天历史，低空经济/固态电池 0 个。

用法
────
  /usr/bin/python3 research/theme/01_fetch_theme_daily.py
"""
import time
import sys
from pathlib import Path

import pandas as pd
import tushare as ts

HERE = Path(__file__).resolve().parent
TOKEN = "7175b5dcc0bf344bdd10722aa7a5a7aa4f6b99e80217cd3702311469"

START, END = "20251001", "20261023"
BENCH = {"000985.CSI": "中证全指", "000300.SH": "沪深300", "000905.SH": "中证500"}

# 必选：能覆盖「当红概念」的关键词
MUST_KW = [
    "机器人", "航天", "航空", "低空", "飞行汽车", "人工智能", "算力", "数据",
    "半导体", "芯片", "集成电路", "电池", "光伏", "储能", "军工", "国防",
    "创新药", "生物医药", "光模块", "通信", "消费电子", "汽车", "机床",
    "稀土", "有色", "煤炭", "白酒", "食品", "银行", "证券", "地产",
    "中药", "医疗器械", "游戏", "传媒", "软件", "云计算", "物联网",
]


def load_universe() -> pd.DataFrame:
    sel = pd.read_parquet(HERE / "_theme_sel.parquet")
    # 剔除跨境/汇率/非 A 股主题
    drop = sel["name"].str.contains(
        "港股通|香港|海外|亚洲|全球|美股|EUR|USD|人民币|沪港深|沪深港|央视|综指", na=False
    ) | sel["name"].str.endswith("R")
    sel = sel[~drop].copy()
    return sel


def pick(sel: pd.DataFrame, cap: int = 150) -> pd.DataFrame:
    hit = sel[sel["name"].str.contains("|".join(MUST_KW), na=False)]
    rest = sel[~sel["ts_code"].isin(hit["ts_code"])]
    rest = rest.sample(min(len(rest), max(0, cap - len(hit))), random_state=7)
    out = pd.concat([hit, rest]).drop_duplicates("ts_code")
    return out[["ts_code", "name"]]


def main() -> int:
    pro = ts.pro_api(TOKEN)
    sel = load_universe()
    uni = pick(sel, cap=150)
    codes = list(uni["ts_code"]) + list(BENCH.keys())
    print(f"待拉指数: {len(codes)}（主题 {len(uni)} + 基准 {len(BENCH)}）")

    frames, fails = [], []
    for i, code in enumerate(codes, 1):
        try:
            df = pro.index_daily(ts_code=code, start_date=START, end_date=END)
            if len(df):
                frames.append(df[["ts_code", "trade_date", "close", "pct_chg", "amount"]])
        except Exception as e:
            fails.append((code, str(e)[:80]))
        if i % 25 == 0:
            print(f"  ...{i}/{len(codes)}  成功 {len(frames)} 失败 {len(fails)}")
        time.sleep(0.25)

    if not frames:
        print("❌ 一条都没拉到"); return 1
    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df.merge(uni, on="ts_code", how="left")
    all_df["name"] = all_df["name"].fillna(all_df["ts_code"].map(BENCH))
    all_df.to_parquet(HERE / "_theme_daily.parquet")

    print(f"\n✅ 落盘 {HERE/'_theme_daily.parquet'}")
    print(f"   指数数 {all_df['ts_code'].nunique()}  行数 {len(all_df)}")
    print(f"   日期 {all_df['trade_date'].min()} ~ {all_df['trade_date'].max()}")
    if fails:
        print(f"   ⚠️ 失败 {len(fails)} 个，样例: {fails[:3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
