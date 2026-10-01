#!/usr/bin/env python3
"""
由 index_member_all_raw.parquet 重建每日 (ind_code, stock_code) PIT 成分快照。

消除前视偏差：聚合时按「当日真实成分」而非 industry_members.parquet 当前快照。
落盘 industry/industry_members_daily.parquet（长表：date, ind_code, stock_code）

起始日（2026-10-01 修订）:
  原实现硬编码 BASE = 2024-09-24，使快照只覆盖 490 天，而下游聚合产物有 1636 天
  —— 2024-09-24 之前的 1146 天不可能由本脚本生成，upsert 把新旧口径拼进了同一份文件。
  实测原料 index_member_all_raw.parquet 自带完整 in_date / out_date（最早 1984-05-09），
  BASE 只是日历过滤、并非数据限制，支持全历史重建（且 490 天重叠区间逐条一致）。
  现已参数化为 --base，默认 None = 全历史。

用法:
  python3 scripts/build_members_daily.py                    # 全历史重建（推荐）
  python3 scripts/build_members_daily.py --base 2024-09-24  # 只重建近段
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import DATA_DIR, MARKET_DIR

INDUSTRY_DIR = DATA_DIR / "industry"
OUT_NAME = "industry_members_daily.parquet"


def ts_to_bs(ts_code: str) -> str:
    parts = ts_code.split(".")
    if len(parts) == 2:
        return f"{parts[1].lower()}.{parts[0]}"
    return ts_code


def main(base: str = None) -> int:
    raw_path = INDUSTRY_DIR / "index_member_all_raw.parquet"
    mp_path = INDUSTRY_DIR / "ind_code_to_sw2021.tsv"
    for p, hint in [(raw_path, "先运行 scripts/fetch_sw_members.py"),
                    (mp_path, "先运行一次映射生成（见 ind_code_to_sw2021.tsv）")]:
        if not p.exists():
            print(f"错误: 缺输入文件 {p}")
            print(f"  修复: {hint}")
            return 1

    raw = pd.read_parquet(raw_path)
    mp = pd.read_csv(mp_path, sep="\t")
    sw2ind = dict(zip(mp["sw_l2_code"], mp["ind_code"]))

    raw["stock_code"] = raw["ts_code"].apply(ts_to_bs)
    raw["ind_code"] = raw["index_code"].map(sw2ind)
    dropped = int(raw["ind_code"].isna().sum())
    raw = raw.dropna(subset=["ind_code"]).copy()
    print(f"原始含本地映射 {len(raw)} 行，无映射(申万空壳/美容护理等)丢弃 {dropped} 行")

    raw["in_date"] = raw["in_date"].fillna("").astype(str).str.replace("None", "", regex=False)
    raw["out_date"] = raw["out_date"].fillna("").astype(str).str.replace("None", "", regex=False)
    raw["in_d"] = pd.to_datetime(raw["in_date"].replace("", "19900101"), format="%Y%m%d")
    raw["out_d"] = pd.to_datetime(raw["out_date"].replace("", "20991231"), format="%Y%m%d")

    # 交易日历：market/daily 实际存在数据的日期
    dates = sorted(pd.to_datetime([f.stem for f in MARKET_DIR.glob("*.parquet")]))
    if base:
        base_ts = pd.Timestamp(base)
        dates = [d for d in dates if d >= base_ts]
        print(f"起始日 = {base_ts.date()}（--base 指定）")
    else:
        print("起始日 = 全历史（未指定 --base）")
    if not dates:
        print("错误: 交易日历为空（检查 market/daily 是否有数据）")
        return 1
    print(f"交易日历: {len(dates)} 天 ({dates[0].date()} ~ {dates[-1].date()})")

    rows = []
    for d in dates:
        sub = raw[(raw["in_d"] <= d) & (raw["out_d"] > d)]
        if len(sub):
            tmp = sub[["ind_code", "stock_code"]].copy()
            tmp["date"] = d
            rows.append(tmp)
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["date", "ind_code", "stock_code"])
    out = out[["date", "ind_code", "stock_code"]]

    # ── 写入护栏：新结果不应比旧文件显著缩水（防交易日历/原料异常误删历史）──
    out_path = INDUSTRY_DIR / OUT_NAME
    if out_path.exists():
        try:
            n_prev = len(pd.read_parquet(out_path))
            n_new = len(out)
            if n_new < n_prev * 0.9:
                print(f"❌ 拒绝写入：新结果 {n_new:,} 行 < 旧文件 {n_prev:,} 行的 90%"
                      f"（阈值 {n_prev * 0.9:,.0f}）—— 疑似交易日历或原料异常。")
                return 1
            print(f"  护栏通过: 旧 {n_prev:,} 行 → 新 {n_new:,} 行")
        except Exception as e:
            print(f"  ⚠️ 旧文件读取失败（{e}），跳过护栏")

    out.to_parquet(out_path, index=False)
    print(f"✓ 保存 {OUT_NAME}: {out.shape[0]} 行 "
          f"{out['ind_code'].nunique()} 行业 × {out['date'].nunique()} 天")
    for d in [dates[0], dates[-1]]:
        sub = out[out["date"] == d]
        print(f"  {d.date()}: {sub['ind_code'].nunique()} 行业 / {len(sub)} 只成分股")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None,
                    help="起始日 YYYY-MM-DD（默认 None = 全历史重建）")
    args = ap.parse_args()
    sys.exit(main(args.base) or 0)
