#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
观察期每日快照台账（B2 验收第 3 条，只读脚本）

用途
────
B2（链定义替换自由聚类）已跑通，但**未切换日报口径**。按立项文档
`chain-veto-and-mainline-rebuild-plan.md` §3.3 红线，切换前必须先观察
多个交易日的新旧对照稳定性。

本脚本把「每天手工跑一次」固化成一件事：
    ① 调 scripts/chain_mainline_detector.py 重跑新版（37 链，方案 C）
       —— 2026-10-12 起写入**生产产物** industry/chain_mainline_detection.csv
    ② 读生产链产出的旧版（23 自由聚类群）industry/mainline_detection.csv
    ③ 抽取关键对照指标，追加/更新一行到 `_local_dev/observation_log.csv`

⚠️ 新旧产物现在都在 `industry/` 下，但**是两份不同的文件**：
    旧版 = mainline_detection.csv（19 列，group_id 开头）
    新版 = chain_mainline_detection.csv（22 列，chain_id 开头）
    本脚本分别读它们做对照，不覆盖任何一方。

幂等性
──────
台账按「参考日」去重：同一参考日重复跑 ⇒ **更新**该行而不是追加重复行。
所以数据没更新时反复跑不会产生垃圾行。

零污染保证
──────────
- 只读 `~/quant-data/industry/`（生产产物）
- 只写 `~/quant-data/_local_dev/`（沙箱出口）
- 不碰生产十步链、不动名单、不动任何阈值

用法
────
    /usr/bin/python3 research/observation_snapshot.py            # 跑一次，记账
    /usr/bin/python3 research/observation_snapshot.py --no-run   # 只记账（用已有产物）
    /usr/bin/python3 research/observation_snapshot.py --show     # 打印台账

记录时间：2026-10-12
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DETECTOR = REPO / "scripts" / "chain_mainline_detector.py"

DATA_ROOT = Path("/Users/hyc/quant-data")
INDUSTRY_DIR = DATA_ROOT / "industry"
OLD_CSV = INDUSTRY_DIR / "mainline_detection.csv"
NEW_CSV = INDUSTRY_DIR / "chain_mainline_detection.csv"   # 新版生产产物（B2 接线后）
RETURNS_PARQUET = INDUSTRY_DIR / "industry_weighted_returns.parquet"
OUT_DIR = DATA_ROOT / "_local_dev"
LOG_CSV = OUT_DIR / "observation_log.csv"

LOG_COLUMNS = [
    "ref_date",           # 新版参考日（数据最后一天）
    "snapshot_at",        # 台账写入时间
    "old_ref_date",       # 旧版产出对应的交易日
    "ref_aligned",        # 新旧参考日是否一致（不一致则对照不可直接比）
    # ── 新版（37 链 / 方案 C）──
    "new_n_chains",
    "new_mainlines",
    "new_top1_chain",
    "new_top1_heat",
    "new_top1_excess",
    "new_top1_winrate",
    "new_top1_stage",
    "new_top1_z",
    "new_top1_is_mainline",
    "new_z_top1_chain",   # 共振 z 最高的链（≠ 热度最高的链）
    "new_z_top1_z",
    "new_mainline_chains",  # 新版被判主线的链（对照核心）
    # ── 旧版（23 自由聚类群）──
    "old_n_groups",
    "old_mainlines",
    "old_mainline_groups",   # 旧版被判主线的群 id（对照核心，别和「热度第一群」混淆）
    "old_top1_group",
    "old_top1_heat",
    "old_top1_excess",
    "old_top1_winrate",
    "old_top1_stage",
    "old_top1_n_ind",     # 旧版第一群有多少行业（n=1 就是伪群）
    # ── 对照 ──
    "delta_mainlines",    # 新 − 旧
    "note",
]


def run_detector():
    """重跑链版探测器，返回其输出文本。失败则抛异常（不静默）。"""
    print("· 重跑链版探测器 chain_mainline_detector.py ...")
    proc = subprocess.run(
        [sys.executable, str(DETECTOR)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        raise RuntimeError(f"探测器执行失败，退出码 {proc.returncode}")
    return proc.stdout


def find_latest_new():
    """新版生产产物 + 其参考日。

    2026-10-12 起新版产物改落生产路径 `industry/chain_mainline_detection.csv`
    （固定路径、日更覆盖）⇒ 不再 glob `_local_dev` 里的按日文件。
    参考日 = 数据最后一天（与探测器的 `returns['date'].max()` 同源）。
    """
    if not NEW_CSV.exists():
        raise FileNotFoundError(
            f"未找到新版生产产物 {NEW_CSV}，请先跑 scripts/chain_mainline_detector.py")
    ret = pd.read_parquet(RETURNS_PARQUET)
    ref = pd.to_datetime(ret['date']).max().strftime('%Y-%m-%d')
    return ref, NEW_CSV


def _fmt(v, nd=3):
    return round(float(v), nd) if pd.notna(v) else None


def build_row(ref_date, new_df, old_df):
    """从新旧两张表抽一行业对照指标。"""
    new = new_df.copy()
    old = old_df.copy()

    # 新版：按热度排序（探测器已排好，这里保险再排一次）
    if "heat_ratio" in new.columns:
        new = new.sort_values("heat_ratio", ascending=False)
    n1 = new.iloc[0]

    # 共振 z 最高的链
    z_row = None
    if "resonance_z_today" in new.columns and new["resonance_z_today"].notna().any():
        z_row = new.sort_values("resonance_z_today", ascending=False).iloc[0]

    # 旧版：按热度排序
    if "heat_ratio" in old.columns:
        old = old.sort_values("heat_ratio", ascending=False)
    o1 = old.iloc[0]

    # 旧版参考日：从它自己的 meta 或列里取；生产产物没有日期列 ⇒ 用文件 mtime 兜底
    old_ref = None
    meta = OLD_CSV.with_suffix(".csv.meta.json")
    if meta.exists():
        import json
        try:
            old_ref = json.loads(meta.read_text()).get("reference_date")
        except Exception:
            old_ref = None
    if old_ref is None:
        old_ref = datetime.fromtimestamp(OLD_CSV.stat().st_mtime).strftime("%Y-%m-%d")

    n_ml = int(new["is_mainline"].sum()) if "is_mainline" in new.columns else 0
    o_ml = int(old["is_mainline"].sum()) if "is_mainline" in old.columns else 0

    def names(x, k=3):
        try:
            v = x["chain_names"] if "chain_names" in new.columns else x["group_names"]
            if isinstance(v, str):
                v = eval(v)
            return "、".join(list(v)[:k])
        except Exception:
            return ""

    return {
        "ref_date": ref_date,
        "snapshot_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "old_ref_date": old_ref,
        "ref_aligned": "yes" if old_ref == ref_date else "NO",
        "new_n_chains": len(new),
        "new_mainlines": n_ml,
        "new_top1_chain": str(n1.get("chain_id", "")),
        "new_top1_heat": _fmt(n1.get("heat_ratio"), 2),
        "new_top1_excess": _fmt(n1.get("excess_ma20"), 3),
        "new_top1_winrate": _fmt(n1.get("winrate"), 2),
        "new_top1_stage": str(n1.get("stage", "")),
        "new_top1_z": _fmt(n1.get("resonance_z_today"), 2),
        "new_top1_is_mainline": bool(n1.get("is_mainline", False)),
        "new_z_top1_chain": str(z_row.get("chain_id", "")) if z_row is not None else "",
        "new_z_top1_z": _fmt(z_row.get("resonance_z_today"), 2) if z_row is not None else None,
        "new_mainline_chains": (
            "、".join(str(x) for x in new.loc[new["is_mainline"], "chain_id"])
            if "is_mainline" in new.columns and n_ml > 0 else "（无）"
        ),
        "old_n_groups": len(old),
        "old_mainlines": o_ml,
        "old_mainline_groups": (
            "、".join(f"群{int(x)}({int(y)}行业)" for x, y in
                     zip(old.loc[old["is_mainline"], "group_id"],
                         old.loc[old["is_mainline"], "n_industries"]))
            if "is_mainline" in old.columns and o_ml > 0 else "（无）"
        ),
        "old_top1_group": f"群{int(o1.get('group_id', 0))}",
        "old_top1_heat": _fmt(o1.get("heat_ratio"), 2),
        "old_top1_excess": _fmt(o1.get("excess_ma20"), 3),
        "old_top1_winrate": _fmt(o1.get("winrate"), 2),
        "old_top1_stage": str(o1.get("stage", "")),
        "old_top1_n_ind": int(o1.get("n_industries", 0)),
        "delta_mainlines": n_ml - o_ml,
        "note": "",
    }


def upsert_log(row):
    """按 ref_date 去重写入台账（幂等）。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_CSV.exists():
        log = pd.read_csv(LOG_CSV)
    else:
        log = pd.DataFrame(columns=LOG_COLUMNS)

    log = log[log["ref_date"] != row["ref_date"]]
    # 空台账直接赋值，避免 concat 空表的 FutureWarning
    log = pd.DataFrame([row]) if len(log) == 0 else pd.concat([log, pd.DataFrame([row])], ignore_index=True)
    log = log[LOG_COLUMNS]
    log = log.sort_values("ref_date").reset_index(drop=True)
    log.to_csv(LOG_CSV, index=False)
    return log


def show_log():
    if not LOG_CSV.exists():
        print("台账尚未建立")
        return
    log = pd.read_csv(LOG_CSV)
    cols = ["ref_date", "ref_aligned", "new_n_chains", "new_mainlines",
            "new_mainline_chains", "old_mainline_groups",
            "new_top1_chain", "new_top1_heat", "new_top1_excess", "new_top1_z",
            "old_mainlines", "old_top1_group", "old_top1_n_ind", "delta_mainlines"]
    cols = [c for c in cols if c in log.columns]
    print("\n" + "=" * 100)
    print("观察期台账：新版（37 链） vs 旧版（23 自由聚类群）")
    print("=" * 100)
    print(log[cols].to_string(index=False))
    print(f"\n累计快照数 = {len(log)}")
    if len(log) >= 2:
        print(f"主线数序列 = {list(log['new_mainlines'])}（新版） / {list(log['old_mainlines'])}（旧版）")


def main():
    ap = argparse.ArgumentParser(description="观察期每日快照台账")
    ap.add_argument("--no-run", action="store_true", help="不重跑探测器，只用已有产物记账")
    ap.add_argument("--show", action="store_true", help="只打印台账")
    args = ap.parse_args()

    if args.show:
        show_log()
        return

    if not args.no_run:
        out = run_detector()
        print(out.strip().splitlines()[-1])

    ref_date, new_path = find_latest_new()
    new_df = pd.read_csv(new_path)
    old_df = pd.read_csv(OLD_CSV)

    row = build_row(ref_date, new_df, old_df)
    log = upsert_log(row)

    print(f"\n台账已更新 → {LOG_CSV}")
    show_log()


if __name__ == '__main__':
    main()
