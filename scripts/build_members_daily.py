#!/usr/bin/env python3
"""
由 index_member_all_raw.parquet 重建 2024-09-24 起每日 (ind_code, stock_code) 成分快照。

消除前视偏差：聚合时按「当日真实成分」而非 industry_members.parquet 当前快照。
落盘 industry/industry_members_daily.parquet（长表：date, ind_code, stock_code）
"""
import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import DATA_DIR, MARKET_DIR

INDUSTRY_DIR = DATA_DIR / "industry"
BASE = pd.Timestamp("2024-09-24")


def ts_to_bs(ts_code: str) -> str:
    parts = ts_code.split(".")
    if len(parts) == 2:
        return f"{parts[1].lower()}.{parts[0]}"
    return ts_code


def main():
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
    dates = [d for d in dates if d >= BASE]
    print(f"交易日历: {len(dates)} 天 ({dates[0].date()} ~ {dates[-1].date()})")

    rows = []
    for d in dates:
        sub = raw[(raw["in_d"] <= d) & (raw["out_d"] > d)]
        if len(sub):
            tmp = sub[["ind_code", "stock_code"]].copy()
            tmp["date"] = d
            rows.append(tmp)
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["date", "ind_code", "stock_code"])
    out = out[["date", "ind_code", "stock_code"]]
    out.to_parquet(INDUSTRY_DIR / "industry_members_daily.parquet", index=False)
    print(f"✓ 保存 industry_members_daily.parquet: {out.shape[0]} 行 "
          f"{out['ind_code'].nunique()} 行业 × {out['date'].nunique()} 天")
    for d in [dates[0], dates[-1]]:
        sub = out[out["date"] == d]
        print(f"  {d.date()}: {sub['ind_code'].nunique()} 行业 / {len(sub)} 只成分股")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
