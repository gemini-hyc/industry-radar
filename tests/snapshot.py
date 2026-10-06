"""tests/snapshot.py —— **数值快照**（软报告，**永不判失败**）。

为什么需要它、又为什么它不能是硬断言：

本机 `~/quant-data` 是只读副本，会随源端日更而变化。如果把「行数 = 214,316」写成
硬断言，那么源端每多跑一天日更、副本一同步，测试就红 —— 这种红没有信息量，
只会训练人忽略测试。所以：

    tests/test_invariants.py   → 应该恒绿的**契约**（结构），失败 = 有真问题
    tests/snapshot.py          → 会随数据漂移的**观测值**，差异 = 供人判断

它的真正用途是审计文档 §6 那句叮嘱：「任何改动后若数字显著变化，
先确认是**数据变化**还是**行为回归**」。本文件把「显著变化」自动分类出来：

    ⬆️ INFO  数据推进（行数增、日期右端前移）—— 正常，除非你没跑日更
    ⚠️ WARN  超出预期的漂移（行数减、日期回退、行业数变、统计量剧变）—— 要看

用法：
    python tests/snapshot.py --update     # 把当前状态写成基线（提交进仓库）
    python tests/snapshot.py              # 与基线对比并分类报告
    python tests/snapshot.py --verbose    # 连未变化的量也打印

    INDUSTRY_RADAR_DATA=<沙箱> python tests/snapshot.py   # 对沙箱产物取快照
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import pandas as pd

from _common import (
    C,
    UNDER_TEST,
    data_root,
    hr,
    load,
    load_ledger,
    read_baseline,
    snapshot_path,
    write_baseline,
)

# 统计量漂移的告警阈值（相对变化 + 绝对变化**同时**满足才告警，避免小样本噪声）
DRIFT_REL = 0.25
DRIFT_ABS = 0.01

# 数值列快照里记录统计量的列（只挑有业务含义的；避免把 id 类列也统计进去）
STATS_COLUMNS: dict[str, list[str]] = {
    "industry_daily_full": ["avg_pct", "total_amount", "stock_n", "breadth", "breadth_ma13", "n_stocks"],
    "industry_weighted_returns": ["weighted_pct", "circ_mv", "stock_n"],
    "daily_regime": ["up_pct", "mean_ret", "std_ret", "top5_conc", "rank_consistency"],
    "industry_nav_924": ["nav_w", "nav_e", "nav_hs300", "rs_w"],
    # 🔴 2026-10-06：tracker_report 的快照字段随跟踪池弃用一并移除
}


def _col_stats(s: pd.Series) -> dict:
    v = pd.to_numeric(s, errors="coerce").dropna()
    if v.empty:
        return {"nan": int(s.isna().sum()), "n": 0}
    return {
        "n": int(len(v)),
        "nan": int(s.isna().sum()),
        "mean": round(float(v.mean()), 6),
        "min": round(float(v.min()), 6),
        "max": round(float(v.max()), 6),
    }


def collect() -> dict:
    """采集当前所有产物的数值观测值。"""
    snap: dict = {
        "_meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_root": str(data_root()),
            "note": "数值快照基线 —— 只用于「数据变化 vs 行为回归」的区分，不作失败判据",
        },
        "artifacts": {},
    }
    try:
        from src.utils.provenance import git_info
        g = git_info()
        snap["_meta"]["git_commit"] = g.get("commit")
    except Exception:
        snap["_meta"]["git_commit"] = None

    for name in UNDER_TEST:
        if not UNDER_TEST[name].exists():
            snap["artifacts"][name] = {"exists": False}
            continue
        df = load(name)
        entry: dict = {"exists": True, "rows": int(len(df)), "cols": int(df.shape[1])}
        dcol = next((c for c in ("date", "trade_date", "signal_date") if c in df.columns), None)
        if dcol is not None:
            d = pd.to_datetime(df[dcol])
            entry["date_col"] = dcol
            entry["date_min"] = d.min().date().isoformat()
            entry["date_max"] = d.max().date().isoformat()
            entry["n_days"] = int(d.nunique())
        icol = next((c for c in ("con_code", "ind_code", "code") if c in df.columns), None)
        if icol is not None:
            entry["n_industries"] = int(df[icol].nunique())
        if name == "daily_regime" and "regime" in df.columns:
            entry["by_label"] = {str(k): int(v) for k, v in df["regime"].value_counts().items()}
        # 🔴 2026-10-06：tracker_report 的 by_status 分布随跟踪池弃用一并移除
        if name == "mainline_detection" and "is_mainline" in df.columns:
            entry["n_mainline"] = int(df["is_mainline"].astype(bool).sum())
        stats = {}
        for col in STATS_COLUMNS.get(name, []):
            if col in df.columns:
                stats[col] = _col_stats(df[col])
        if stats:
            entry["stats"] = stats
        snap["artifacts"][name] = entry

    # 台账（不在 ARTIFACTS 的 parquet 直接读取路径之外，单独采）
    try:
        lg = load_ledger()
        snap["ledger"] = {
            "rows": int(len(lg)),
            "by_source": {str(k): int(v) for k, v in lg["source"].value_counts().items()},
            "date_min": lg["signal_date"].min().date().isoformat(),
            "date_max": lg["signal_date"].max().date().isoformat(),
        }
    except Exception as e:
        snap["ledger"] = {"error": str(e)}
    return snap


# ──────────────────────────────────────────────────────────────────────
# 对比与分类
# ──────────────────────────────────────────────────────────────────────
def _cmp_num(cur, base, key: str, label: str, fmt: str, out: list) -> None:
    """对比一个数值；把「增长」判为 INFO、「回退」判为 WARN。"""
    if base is None or cur is None:
        return
    if cur == base:
        out.append(("same", f"{label} {fmt.format(cur)}（未变）"))
    elif cur > base:
        out.append(("info", f"{label} {fmt.format(base)} → {fmt.format(cur)}（增加）"))
    else:
        out.append(("warn", f"{label} {fmt.format(base)} → {fmt.format(cur)}（**减少**）"))


def _cmp_stats(name: str, cur: dict, base: dict, out: list) -> None:
    for col, cb in (cur.get("stats") or {}).items():
        pb = (base.get("stats") or {}).get(col)
        if not pb or cb.get("n", 0) == 0 or pb.get("n", 0) == 0:
            continue
        for stat in ("mean", "min", "max"):
            if stat not in cb or stat not in pb:
                continue
            b, c = pb[stat], cb[stat]
            denom = max(abs(b), 1e-9)
            rel, absd = abs(c - b) / denom, abs(c - b)
            if rel > DRIFT_REL and absd > DRIFT_ABS:
                out.append(("warn", f"{name}.{col}.{stat}: {b:.6g} → {c:.6g}"
                                    f"（{'+' if c > b else ''}{(c - b) / denom:.1%}）"))
            elif c != b:
                out.append(("drift", f"{name}.{col}.{stat}: {b:.6g} → {c:.6g}"))
        if cb.get("nan", 0) != pb.get("nan", 0):
            out.append(("drift", f"{name}.{col}.NaN: {pb.get('nan')} → {cb.get('nan')}"))


def compare(cur: dict, base: dict, verbose: bool) -> tuple[int, int]:
    warns = infos = 0
    hr("═")
    print(f"{C.BOLD}═══ 数值快照对比（软报告，不判失败）═══{C.END}")
    print(f"基线: {base['_meta'].get('generated_at')}  @{base['_meta'].get('git_commit')}")
    print(f"当前: {cur['_meta'].get('generated_at')}  @{cur['_meta'].get('git_commit')}")
    if base["_meta"].get("data_root") != cur["_meta"].get("data_root"):
        print(f"{C.WARN}⚠️ 数据根不同：基线 {base['_meta'].get('data_root')} "
              f"vs 当前 {cur['_meta'].get('data_root')} —— 两边的数值本就不可直接比较{C.END}")

    for name in cur["artifacts"]:
        cb, pb = cur["artifacts"][name], base["artifacts"].get(name, {})
        if not cb.get("exists"):
            print(f"\n{C.BAD}── {name}: 产物不存在{C.END}"); warns += 1; continue
        if not pb.get("exists"):
            print(f"\n{C.WARN}── {name}: 基线中不存在（新增产物？rows={cb['rows']:,}）{C.END}")
            infos += 1; continue

        out: list = []
        # 行数：**不能**无条件把「增加」判为正常 —— 数据没更新却多算了行，是典型的回归征兆
        # （重复写入 / 去重失效）。故结合「日期右端是否推进」来判断。
        rb, rc_ = pb.get("rows"), cb.get("rows")
        date_adv = bool(cb.get("date_max") and pb.get("date_max") and cb["date_max"] > pb["date_max"])
        if rb == rc_:
            out.append(("same", f"行数 {rc_:,}（未变）"))
        elif rc_ > rb:
            d = rc_ - rb
            if date_adv or cb.get("date_max") is None:
                why = "日期右端推进" if date_adv else "该产物无日期列（如抓取类）"
                out.append(("info", f"行数 {rb:,} → {rc_:,}（+{d:,}；{why}，属数据增长）"))
            else:
                out.append(("warn", f"行数 {rb:,} → {rc_:,}（+{d:,}）但**日期右端未推进** "
                                    f"⇒ 可疑（重复写入 / 去重失效？）"))
        else:
            out.append(("warn", f"行数 **减少** {rb:,} → {rc_:,}（{rc_ - rb:,}）"
                                f"⇒ 数据被截断或上游缺数"))
        _cmp_num(cb.get("n_days"), pb.get("n_days"), "n_days", "交易日数", "{}", out)
        if cb.get("n_industries") != pb.get("n_industries"):
            out.append(("warn", f"行业数 {pb.get('n_industries')} → {cb.get('n_industries')}（**变动**）"))
        elif cb.get("n_industries") is not None:
            out.append(("same", f"行业数 {cb['n_industries']}（未变）"))
        # 日期右端推进 = 数据更新，属正常
        if cb.get("date_max") != pb.get("date_max"):
            if (cb.get("date_max") or "") > (pb.get("date_max") or ""):
                out.append(("info", f"日期右端 {pb.get('date_max')} → {cb.get('date_max')}（数据推进）"))
            else:
                out.append(("warn", f"日期右端 **回退** {pb.get('date_max')} → {cb.get('date_max')}"))
        _cmp_stats(name, cb, pb, out)

        for kind, msg in out:
            if kind == "same":
                if verbose:
                    print(f"  {C.DIM}✅ {msg}{C.END}")
            elif kind == "info":
                print(f"  {C.OK}⬆️  {msg}{C.END}"); infos += 1
            elif kind == "drift":
                if verbose:
                    print(f"  {C.DIM}·  {msg}{C.END}")
            else:
                print(f"  {C.WARN}⚠️  {msg}{C.END}"); warns += 1

    # 台账
    lb, lc = base.get("ledger", {}), cur.get("ledger", {})
    if lb.get("rows") != lc.get("rows") or lb.get("by_source") != lc.get("by_source"):
        print(f"\n{C.BOLD}── ledger（信号台账）{C.END}")
        _cmp_num(lc.get("rows"), lb.get("rows"), "rows", "台账条数", "{:,}", [])
        if lc.get("by_source") != lb.get("by_source"):
            print(f"  {C.WARN}⚠️  来源分布变化: {lb.get('by_source')} → {lc.get('by_source')}{C.END}")
            warns += 1
        elif lb.get("rows") != lc.get("rows"):
            print(f"  {C.OK}⬆️  台账条数 {lb.get('rows')} → {lc.get('rows')}{C.END}")
            infos += 1
        if lb.get("date_max") != lc.get("date_max"):
            print(f"  {C.DIM}·  台账日期右端 {lb.get('date_max')} → {lc.get('date_max')}{C.END}")
    elif verbose:
        print(f"\n  {C.DIM}✅ ledger: {lc.get('rows')} 条，来源分布未变{C.END}")

    hr("─")
    print(f"{C.OK}⬆️  {infos} 项数据推进（INFO，通常正常）{C.END}   "
          f"{C.WARN}⚠️  {warns} 项需关注（WARN）{C.END}")
    if warns:
        print(f"{C.DIM}   提示：先确认是「数据变化」还是「行为回归」。"
              f"若是改动代码所致，用 tools/dev_verify.sh 做同源 A/B。{C.END}")
    return warns, infos


def main() -> int:
    ap = argparse.ArgumentParser(description="数值快照（软报告）")
    ap.add_argument("--update", action="store_true", help="把当前状态写成基线")
    ap.add_argument("--verbose", "-v", action="store_true", help="连未变化的量也打印")
    args = ap.parse_args()

    print(f"采集数值快照 … 数据根: {data_root()}")
    cur = collect()

    if args.update:
        write_baseline(cur)
        print(f"{C.OK}✅ 已写入基线: {snapshot_path()}{C.END}")
        print(f"   共 {len(cur['artifacts'])} 个产物；请把该文件提交进仓库作为对比锚点。")
        for name, e in cur["artifacts"].items():
            if not e.get("exists"):
                continue
            span = (f"{e['date_min']} ~ {e['date_max']}"
                    if e.get("date_min") else f"（无日期列，{e.get('cols')} 列）")
            print(f"     {name:28s} {e['rows']:>10,} 行  {span}")
        lg = cur.get("ledger", {})
        if lg.get("by_source"):
            print(f"     台账来源分布: {lg['by_source']}")
        return 0

    base = read_baseline()
    if base is None:
        print(f"{C.WARN}⚠️ 尚无基线（{snapshot_path()}）。先跑：python tests/snapshot.py --update{C.END}")
        return 2
    warns, _ = compare(cur, base, args.verbose)
    print(f"\n{C.DIM}（本报告不参与退出码 —— 数值漂移不是失败）{C.END}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
