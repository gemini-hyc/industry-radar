#!/usr/bin/env python3
"""
拉取申万2021 全量行业成分（带 in_date / out_date），落盘 industry/index_member_all_raw.parquet
直连 Tushare http://api.tushare.pro（MCP 层会报 additional properties，直连更稳）

输出列: index_code(801xxx.SI) | ts_code(000001.SZ) | in_date | out_date | is_new
"""
import sys
import time
import yaml
import requests
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import DATA_DIR

INDUSTRY_DIR = DATA_DIR / "industry"
OUT_PATH = INDUSTRY_DIR / "index_member_all_raw.parquet"
TOKEN = yaml.safe_load(open(DATA_DIR / "config.yaml"))["tushare"]["token"]
API = "http://api.tushare.pro"
FIELDS = "l2_code,l2_name,ts_code,in_date,out_date,is_new"


def fetch(is_new: str, retry: int = 3) -> pd.DataFrame:
    req = {
        "api_name": "index_member_all",
        "token": TOKEN,
        # Tushare 该接口当前必须传 index_code（被忽略但必填，传任意有效L2码返回全量）；
        # 不传 index_code / 传 limit / 传 fields 均返回空结果。
        # Tushare 该接口当前：必须传 index_code（被忽略但必填，传任意有效L2码返回全量）；
        # 不传 index_code / 传 limit / 传 fields / 传 src 均返回空结果。
        "params": {"is_new": is_new, "index_code": "801093.SI"},
    }
    for i in range(retry):
        try:
            r = requests.post(API, json=req, timeout=120)
            j = r.json()
            if j.get("code") != 0:
                raise RuntimeError(f"Tushare err: {j.get('msg')} | {j.get('code')}")
            items = j["data"]["items"]
            df = pd.DataFrame(items, columns=j["data"]["fields"])
            # Tushare 无 index_code 字段，l2_code 即申万二级指数码(801xxx.SI)
            return df.rename(columns={"l2_code": "index_code", "l2_name": "index_name"})
        except Exception as e:
            print(f"  ⚠️ is_new={is_new} 第{i+1}次失败: {e}")
            time.sleep(3)
    raise RuntimeError(f"is_new={is_new} 拉取失败")


def main():
    print("拉取 index_member_all(is_new=Y) ...")
    y = fetch("Y")
    print(f"  Y: {len(y)} 行")
    print("拉取 index_member_all(is_new=N) ...")
    n = fetch("N")
    print(f"  N: {len(n)} 行")
    df = pd.concat([y, n], ignore_index=True)
    df["in_date"] = df["in_date"].apply(lambda x: str(x) if x else "")
    df["out_date"] = df["out_date"].apply(lambda x: str(x) if x else "")
    print(f"合并: {len(df)} 行, 行业数(index_code唯一): {df['index_code'].nunique()}")
    df.to_parquet(OUT_PATH, index=False)
    print(f"✓ 保存: {OUT_PATH}")


if __name__ == "__main__":
    main()
