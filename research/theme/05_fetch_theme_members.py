#!/usr/bin/env python3
"""
主题层 · 步骤 5：拉取主题指数成分股权重（PIT 季度快照）

关键事实（2026-10-25 实测）
──────────────────────────
- `index_weight` **只在季度末（调仓日）有数据**，非调仓交易日返回 0 行。
  ⇒ 必须用「季度末的最后一个交易日」作为快照日，用日历季度末会踩空
    （如 20240331/20240630/20230930 是周末 → 返回 0）。
- 成分**确实在变**：931071.CSI 在 20260630 → 20260930 之间换掉 11 只。
  ⇒ 拿「最新成分」回溯历史会造成前视偏差（违反统计纪律第 7 条），必须按季度存快照。
- `index_weight` **无限频**（实测 20 次调用 0 失败 7.1 秒）。

用途
────
① 主题 → 个股落地（雷达"发现叙事 → 发现行业 → 找个股"的最后一环）
② 主题 → 申万二级映射，算「行业中性的当日超额」

用法
────
  /usr/bin/python3 research/theme/05_fetch_theme_members.py --quarters 1   # 只拉最新（约 4 分钟）
  /usr/bin/python3 research/theme/05_fetch_theme_members.py --quarters 8   # 最近 2 年（约 25 分钟）
  /usr/bin/python3 research/theme/05_fetch_theme_members.py --quarters all # 全历史（约 90 分钟）

产物
────
  research/theme/_theme_members.parquet        成分快照（ts_code/trade_date/con_code/weight）
  research/theme/_theme_members_state.json     断点状态
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

OUT = HERE / "_theme_members.parquet"
STATE = HERE / "_theme_members_state.json"
SLEEP = 0.32
CKPT_EVERY = 500


def quarter_end_dates() -> list:
    """取真正的季度末交易日（3/6/9/12 月的最后一个交易日）。

    ⚠️ 教训：不能简单对 (年,季) 取 max —— 若数据止于季中（如 2026-10-23），
    会把它误当成 2026Q4 的「季度末」，而 index_weight 在非调仓日返回 0 行，
    曾因此白跑 673 次调用。
    """
    df = pd.read_parquet(HERE / "_theme_daily_full.parquet")
    dates = pd.DataFrame({"d": sorted(df["trade_date"].astype(str).unique())})
    dates["ym"] = dates["d"].str[:6]
    month_end = dates.groupby("ym")["d"].max()
    return sorted(month_end[month_end.str[4:6].isin(["03", "06", "09", "12"])].tolist())


def index_span() -> pd.DataFrame:
    df = pd.read_parquet(HERE / "_theme_daily_full.parquet")
    df["trade_date"] = df["trade_date"].astype(str)
    return df.groupby("ts_code")["trade_date"].agg(["min", "max"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", default="1", help="最近 N 个季度，或 all")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    qd = quarter_end_dates()
    if args.quarters == "all":
        targets = qd
    else:
        targets = qd[-int(args.quarters):]
    # 从近到远：先拿到最新的，中断也不耽误用
    targets = list(reversed(targets))
    print(f"目标快照日（从近到远）: {targets}")

    span = index_span()
    jobs = []
    for q in targets:
        keep = span[(span["min"] <= q) & (span["max"] >= q)].index
        jobs += [(c, q) for c in keep]
    print(f"待拉 (指数×季度) = {len(jobs)}   预计 {len(jobs)*SLEEP/60:.1f} 分钟")

    done, frames = set(), []
    if not args.fresh and STATE.exists() and OUT.exists():
        st = json.loads(STATE.read_text())
        done = set(tuple(x) for x in st.get("done", []))
        # ⚠️ 教训（2026-10-26）：旧 checkpoint 里的列已叫 ts_code，新拉的叫 index_code，
        # 直接 concat 会得到 5 列（静默错位），落盘时 columns= 赋值才报 Length mismatch。
        # 断点续传读取后**必须统一列名**再进 frames。
        old = pd.read_parquet(OUT)
        if "index_code" in old.columns and "ts_code" in old.columns:
            # 两列并存（本次事故的现场）：先合并再删，避免 rename 出重复列名
            old["index_code"] = old["index_code"].fillna(old["ts_code"])
            old = old.drop(columns=["ts_code"])
        elif "ts_code" in old.columns:
            old = old.rename(columns={"ts_code": "index_code"})
        frames = [old[["index_code", "trade_date", "con_code", "weight"]]]
        print(f"断点续传：已完成 {len(done)} 项，既有 {len(frames[0]):,} 行")

    todo = [j for j in jobs if j not in done]
    pro = ts.pro_api(TOKEN)
    t0, fails = time.time(), []
    for i, (code, q) in enumerate(todo, 1):
        try:
            df = pro.index_weight(index_code=code, trade_date=q)
            if len(df):
                frames.append(df[["index_code", "trade_date", "con_code", "weight"]])
        except Exception as e:
            fails.append((code, q, str(e)[:60]))
        done.add((code, q))

        if i % 200 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(todo)}  已取 {len(frames)-1} 失败 {len(fails)} "
                  f"用时 {el/60:.1f}min  剩余 {(len(todo)-i)*el/i/60:.1f}min")
        if i % CKPT_EVERY == 0:
            if frames:
                pd.concat(frames, ignore_index=True).to_parquet(OUT)
            STATE.write_text(json.dumps({"done": [list(x) for x in done]}))
            print(f"    [checkpoint] {len(done)} 项")
        time.sleep(SLEEP)

    if not frames:
        print("❌ 一条都没拉到"); return 1
    all_df = pd.concat(frames, ignore_index=True)
    all_df.columns = ["ts_code", "trade_date", "con_code", "weight"]
    all_df = all_df.drop_duplicates(["ts_code", "trade_date", "con_code"])
    all_df.to_parquet(OUT)
    STATE.write_text(json.dumps({"done": [list(x) for x in done]}))

    print(f"\n✅ 落盘 {OUT}")
    print(f"   指数数 {all_df['ts_code'].nunique()}  快照日 {all_df['trade_date'].nunique()} 个")
    print(f"   行数 {len(all_df):,}  个股数 {all_df['con_code'].nunique()}")
    print(f"   失败 {len(fails)}")
    if fails:
        print(f"   失败样例: {fails[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
