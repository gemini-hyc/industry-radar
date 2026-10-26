#!/usr/bin/env python3
"""
主题层 · 步骤 3：全量拉取产业主题指数日线（843 个，2020-01-01 起）

与步骤 1（144 个原型）的差别
────────────────────────
- 数量：144 → **全量 843**（口径完全一致：_theme_sel.parquet 的产业段，
  剔除跨境/汇率/非 A 股主题），保证与原型可对比（统计纪律：口径变更必须复验）。
- 区间：2025-10 ~ 2026-10 → **2020-01-01 起**（对齐项目行业收益数据起点，
  约 1640 个交易日，是「行业中性超额」所必需）。
- 工程：新增**断点续传**（每 200 个存盘一次，重跑自动跳过已取）、
  失败重试 1 次、限速率控制。

用法
────
  /usr/bin/python3 research/theme/03_fetch_theme_full.py           # 正常跑
  /usr/bin/python3 research/theme/03_fetch_theme_full.py --fresh   # 丢弃重来

产物
────
  research/theme/_theme_daily_full.parquet      日线（ts_code/trade_date/close/pct_chg/vol/amount/name）
  research/theme/_theme_fetch_state.json        断点状态（已取代码、失败清单）
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import tushare as ts

HERE = Path(__file__).resolve().parent
TOKEN = "7175b5dcc0bf344bdd10722aa7a5a7aa4f6b99e80217cd3702311469"

START, END = "20200101", "20261023"
OUT = HERE / "_theme_daily_full.parquet"
STATE = HERE / "_theme_fetch_state.json"

# 基准：用于算「当日超额」（主题层统一口径）
BENCH = {
    "000985.CSI": "中证全指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000001.SH": "上证指数",
}

# 账户 2120 积分 = 2000 档 = 200 次/分钟 ⇒ 每次间隔 ≥0.3s，取 0.32s 留余量
SLEEP = 0.32
CKPT_EVERY = 200


def load_universe() -> pd.DataFrame:
    """产业主题段（与步骤 1 完全相同的口径）。"""
    sel = pd.read_parquet(HERE / "_theme_sel.parquet")
    drop = sel["name"].str.contains(
        "港股通|香港|海外|亚洲|全球|美股|EUR|USD|人民币|沪港深|沪深港|央视|综指", na=False
    ) | sel["name"].str.endswith("R")
    return sel[~drop].copy()


def save(frames, done, fails):
    if not frames:
        return
    df = pd.concat(frames, ignore_index=True)
    df.to_parquet(OUT)
    STATE.write_text(
        json.dumps({"done": sorted(done), "fails": fails, "rows": len(df)},
                   ensure_ascii=False, indent=1)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="丢弃断点状态重跑")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个（调试用）")
    args = ap.parse_args()

    done, fails, frames = set(), [], []
    if not args.fresh and STATE.exists() and OUT.exists():
        st = json.loads(STATE.read_text())
        done, fails = set(st.get("done", [])), st.get("fails", [])
        frames = [pd.read_parquet(OUT)]
        print(f"断点续传：已完成 {len(done)} 个，已有 {len(frames[0])} 行")

    sel = load_universe()
    codes = list(sel["ts_code"]) + list(BENCH.keys())
    if args.limit:
        codes = codes[: args.limit]
    todo = [c for c in codes if c not in done]
    print(f"待拉 {len(todo)} 个（主题 {len(sel)} + 基准 {len(BENCH)}，已跳过 {len(done)}）")
    print(f"区间 {START} ~ {END}  限速 {SLEEP}s/次  预计 {len(todo)*SLEEP/60:.1f} 分钟\n")

    pro = ts.pro_api(TOKEN)
    t0 = time.time()
    for i, code in enumerate(todo, 1):
        got = None
        for attempt in range(2):
            try:
                df = pro.index_daily(ts_code=code, start_date=START, end_date=END)
                if len(df):
                    got = df[["ts_code", "trade_date", "close", "pct_chg", "vol", "amount"]]
                    break
                got = "EMPTY"
                break
            except Exception as e:
                if attempt == 1:
                    fails.append((code, str(e)[:80]))
                else:
                    time.sleep(1.0)
        if isinstance(got, pd.DataFrame):
            frames.append(got)
        done.add(code)

        if i % 50 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(todo)}  已取 {len(frames)} 失败 {len(fails)} "
                  f"用时 {el/60:.1f}min  预计剩余 {(len(todo)-i)*el/i/60:.1f}min")
        if i % CKPT_EVERY == 0:
            save(frames, done, fails)
            print(f"    [checkpoint] 已存 {len(done)} 个")
        time.sleep(SLEEP)

    save(frames, done, fails)

    df = pd.concat(frames, ignore_index=True)
    names = dict(zip(sel["ts_code"], sel["name"]))
    names.update(BENCH)
    df["name"] = df["ts_code"].map(names)
    df.to_parquet(OUT)

    print(f"\n✅ 落盘 {OUT}")
    print(f"   指数数 {df['ts_code'].nunique()}  行数 {len(df)}")
    print(f"   日期   {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    print(f"   失败   {len(fails)}")
    if fails:
        print(f"   失败样例: {fails[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
