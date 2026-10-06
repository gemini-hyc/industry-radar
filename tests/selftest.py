#!/usr/bin/env python3
"""tests/selftest.py —— 「测试的测试」：注入已知破坏，验证 `tests/` 真能抓到。

**为什么需要这个文件**

一个回归测试套件最大的失败模式不是「有 bug 没抓到」，而是**它其实永远绿**：
断言写错（拿到错误的对象）、异常被吞、目录指错、退出码恒 0 —— 任何一种都让
套件看起来在工作，实际形同虚设。本文件用**受控注入**证明每条断言都有抓取能力：

    ① 结构破坏 11 例 → 每个都应让**特定的那个测试**失败（不是「随便红一下」）
    ② 快照篡改  3 例 → 验证「数据增长 / 可疑变动」的分类判据方向正确
    ③ 源码破坏  1 例 → 把前瞻窗口改回 2026-10-05 的错误写法，语义断言必须抓到
    ④ 完备性探针 1 例 → 把某日 breadth 集体置 0，check_data_completeness 必须报 BREADTH_ZERO
    ⑤ 恢复后复验     → 确认破坏与恢复本身没有副作用

**安全边界**
- 只在 `/tmp` 下的**隔离沙箱**里改数据（从 `~/quant-data` 拷贝出来的副本），
  **绝不碰真实数据根**；结束时用 `-newer` 守卫复核。
- 快照篡改只针对仓库内的 `tests/baseline_snapshot.json`，用 `.selftest-bak`
  兜底（进程被中断也能手工恢复），且 `try/finally` 保证恢复。

用法：
    /Users/hyc/Quant/stock-doctor/.venv/bin/python tests/selftest.py
    IR_REGRESS_TMP=/tmp/xxx python tests/selftest.py     # 自定义沙箱路径

⚠️ 本文件**不参与**日常测试（会写 /tmp 且耗时约 30 秒），只在**改动测试套件本身**
   或怀疑套件失效时跑。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
PY = sys.executable
SRC = Path(os.environ.get("IR_REGRESS_DATA_SRC", Path.home() / "quant-data"))
SB = Path(os.environ.get("IR_REGRESS_TMP", "/tmp/ir-regress-selftest")) / "data"
BASELINE = TESTS_DIR / "baseline_snapshot.json"
BAK = TESTS_DIR / "baseline_snapshot.json.selftest-bak"

# 只拷测试真正读到的文件（约 22 MB，秒级）
NEED = {
    "industry": [
        "industry_daily_full.parquet", "industry_weighted_returns.parquet",
        "industry_members_daily.parquet", "industry_members.parquet",
        "daily_regime.parquet", "industry_nav_924.parquet",
        "mainline_detection.csv",
        # 🔴 2026-10-06：tracker_report.csv 随跟踪池弃用从沙箱必需清单移除
    ],
    "signals": ["signals_log.parquet"],
}

CASES: list[tuple[str, str, callable]] = []


def case(name: str, expect: str):
    """注册一个破坏场景：name 描述，expect = 期望失败的**测试方法名**。"""
    def deco(fn):
        CASES.append((name, expect, fn))
        return fn
    return deco


# ══════════════════════════════════════════════════════════════════
# ① 结构破坏（改沙箱里的产物）
# ══════════════════════════════════════════════════════════════════
@case("删列 → schema 漂移", "test_columns_exact_set")
def b1():
    p = SB / "industry/industry_daily_full.parquet"
    pd.read_parquet(p).drop(columns=["breadth_ma13"]).to_parquet(p, index=False)


@case("删掉一个行业的全部行 → 行业集合探针", "test_industry_sets_identical_across_artifacts")
def b2():
    p = SB / "industry/industry_weighted_returns.parquet"
    d = pd.read_parquet(p)
    victim = sorted(d["ind_code"].unique())[0]
    d[d["ind_code"] != victim].to_parquet(p, index=False)


@case("删掉某交易日的一行 → 网格缺格探针", "test_full_grid")
def b3():
    p = SB / "industry/industry_daily_full.parquet"
    d = pd.read_parquet(p)
    d.drop(index=d.index[5]).to_parquet(p, index=False)


@case("复制一行 → 键重复探针", "test_no_duplicate_keys")
def b4():
    p = SB / "industry/daily_regime.parquet"
    d = pd.read_parquet(p)
    pd.concat([d, d.tail(1)], ignore_index=True).to_parquet(p, index=False)


@case("regime 出现陌生类别", "test_labels_are_known")
def b5():
    p = SB / "industry/daily_regime.parquet"
    d = pd.read_parquet(p)
    d.loc[d.index[0], "regime"] = "未知新类别"
    d.to_parquet(p, index=False)


@case("台账出现未登记来源", "test_sources_known")
def b6():
    p = SB / "signals/signals_log.parquet"
    d = pd.read_parquet(p)
    d.loc[d.index[0], "source"] = "rogue_source"
    d.to_parquet(p, index=False)


@case("日期回跳（乱序拼接）", "test_dates_monotonic")
def b7():
    p = SB / "industry/industry_nav_924.parquet"
    d = pd.read_parquet(p)
    pd.concat([d.iloc[100:], d.iloc[:100]], ignore_index=True).to_parquet(p, index=False)


@case("值域越界（up_pct=150）", "test_ranges")
def b8():
    p = SB / "industry/daily_regime.parquet"
    d = pd.read_parquet(p)
    d.loc[d.index[0], "up_pct"] = 150.0
    d.to_parquet(p, index=False)


@case("关键列注入 NaN", "test_no_nan")
def b9():
    p = SB / "industry/industry_daily_full.parquet"
    d = pd.read_parquet(p)
    d.loc[d.index[0], "avg_pct"] = None
    d.to_parquet(p, index=False)


@case("已知缺陷异常扩散（普涨日 top5_conc 为负）",
      "test_top5_conc_negative_only_when_few_risers")
def b10():
    p = SB / "industry/daily_regime.parquet"
    d = pd.read_parquet(p)
    d.loc[d["up_pct"].idxmax(), "top5_conc"] = -3.0
    d.to_parquet(p, index=False)


@case("宽度整体退化（breadth 半数为 NaN）", "test_breadth_not_degenerated")
def b11():
    p = SB / "industry/industry_daily_full.parquet"
    d = pd.read_parquet(p)
    d.loc[d.index[: len(d) // 2], "breadth"] = None
    d.to_parquet(p, index=False)


# ══════════════════════════════════════════════════════════════════
# ② 快照篡改（改仓库内的基线 JSON）
#    期望 (WARN 数下限, INFO 数下限)
# ══════════════════════════════════════════════════════════════════
SNAP_CASES = [
    ("基线行数偏大 ⇒ 当前「减少」→ 应 WARN", "rows_bigger", 1, 0),
    ("基线行数偏小且日期未推进 ⇒ 「增加但可疑」→ 应 WARN", "rows_smaller", 1, 0),
    ("行数增加 + 日期推进 ⇒ 应判为 INFO 而非 WARN", "rows_smaller_date_adv", 0, 1),
]


# ══════════════════════════════════════════════════════════════════
# ③ 源码破坏（前瞻窗口写法 —— 2026-10-05 事故的回归）
#    与前两节不同：注入点必须是**仓库源码**而不是沙箱数据 ——
#    因为这条断言考的是「代码语义」，不是「数据形状」。
#    兜底：改前存字节，finally 逐字节还原，并复核文件与原文一致。
# ══════════════════════════════════════════════════════════════════
CODE_TARGET = REPO / "src/signals/ledger.py"
CODE_BAK = CODE_TARGET.with_name(CODE_TARGET.name + ".selftest-bak")

CODE_CORRECT = "    return frame.rolling(horizon, min_periods=horizon).sum().shift(-horizon)"
CODE_BUGGY = "    return frame.shift(-1).rolling(horizon, min_periods=horizon).sum()"

# (描述, 期望失败的测试方法名, 正确行, 破坏后行)
CODE_CASES = [
    ("把 forward_sum 改回 shift(-1).rolling(k) 错误写法",
     "test_matches_manual_construction", CODE_CORRECT, CODE_BUGGY),
]


# ══════════════════════════════════════════════════════════════════
# ④ 完备性探针（考的是 scripts/check_data_completeness.py，不是 tests/）
#    注入点=沙箱数据：把最新交易日的 breadth 集体置 0（P0-2 的特征值）
# ══════════════════════════════════════════════════════════════════
BREADTH_CASE_CODE = "BREADTH_ZERO"


# ══════════════════════════════════════════════════════════════════
def build_sandbox() -> int:
    if SB.exists():
        shutil.rmtree(SB)
    n = 0
    for sub, files in NEED.items():
        (SB / sub).mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(SRC / sub / f, SB / sub / f)
            n += 1
    return n


def _run(script: str, extra_env: dict | None = None) -> tuple[int, str]:
    env = {**os.environ, "NO_COLOR": "1", **(extra_env or {})}
    r = subprocess.run([PY, str(TESTS_DIR / script)], cwd=REPO, env=env,
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def run_invariants() -> tuple[int, list[str]]:
    rc, out = _run("test_invariants.py", {"INDUSTRY_RADAR_DATA": str(SB)})
    return rc, sorted(set(re.findall(r"^FAIL: (\w+)", out, re.M)))


def run_forward() -> tuple[int, list[str]]:
    """前瞻窗口语义断言（零数据依赖，考的是仓库源码）。"""
    rc, out = _run("test_forward_returns.py", {"INDUSTRY_RADAR_DATA": str(SB)})
    return rc, sorted(set(re.findall(r"^FAIL: (\w+)", out, re.M)))


def run_completeness(target_date: str) -> str:
    """跑完备性检查（读沙箱），返回合并输出 —— 用 grep 判定是否报出指定 code。"""
    env = {**os.environ, "NO_COLOR": "1", "INDUSTRY_RADAR_DATA": str(SB)}
    r = subprocess.run(
        [PY, str(REPO / "scripts/check_data_completeness.py"),
         "--date", target_date, "--no-write"],
        cwd=REPO, env=env, capture_output=True, text=True)
    return r.stdout + r.stderr


def run_snapshot() -> tuple[int, int]:
    rc, out = _run("snapshot.py")
    m = re.search(r"(\d+)\s+项需关注", out)
    warns = int(m.group(1)) if m else -1
    m2 = re.search(r"(\d+)\s+项数据推进", out)
    return warns, (int(m2.group(1)) if m2 else -1)


def mutate_baseline(kind: str) -> None:
    b = json.loads(BAK.read_text(encoding="utf-8"))
    a = b["artifacts"]["industry_daily_full"]
    if kind == "rows_bigger":
        a["rows"] += 5000
    elif kind == "rows_smaller":
        a["rows"] -= 5000
    elif kind == "rows_smaller_date_adv":
        a["rows"] -= 5000
        a["date_max"] = "2026-09-25"
    BASELINE.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    print("═" * 76)
    print("回归套件敏感性自测（注入已知破坏，验证能否抓到）")
    print(f"  解释器: {PY}")
    print(f"  数据源: {SRC}")
    print(f"  沙箱:   {SB}")
    print("═" * 76)

    if not (SRC / "industry" / "industry_daily_full.parquet").exists():
        print(f"❌ 数据源不可用: {SRC}")
        return 2
    if not BASELINE.exists():
        print(f"❌ 快照基线缺失，先跑: python tests/snapshot.py --update")
        return 2

    # 🔴 守卫 marker 必须在**任何测试动作之前**创建，否则守卫无意义
    guard_marker = Path("/tmp/ir-regress-selftest-guard")
    guard_marker.touch()

    # 基线文件兜底：若上次被中断留下 .bak，先恢复
    if BAK.exists():
        print("⚠️  发现上次残留的 .selftest-bak，先恢复基线")
        shutil.copy2(BAK, BASELINE)

    # 源码兜底：若上次被中断留下 .selftest-bak，先还原源码
    if CODE_BAK.exists():
        print(f"⚠️  发现残留 {CODE_BAK.name}，先还原源码 {CODE_TARGET.name}")
        shutil.copy2(CODE_BAK, CODE_TARGET)
        CODE_BAK.unlink()

    n = build_sandbox()
    print(f"✅ 沙箱就绪（{n} 个文件）\n")

    print("─── 基线（未破坏，应全绿）───")
    rc, failed = run_invariants()
    if rc != 0:
        print(f"❌ 基线未全绿：exit={rc} failed={failed}")
        print("   ⇒ 先修测试本身，敏感性自测无意义")
        return 1
    print("✅ 基线全绿\n")

    # ── ① 结构破坏 ──────────────────────────────────────────────
    print("─── ① 结构破坏（每例都应让指定的那个测试失败）───")
    caught = missed = 0
    for name, expect, fn in CASES:
        saved = [(p, p.read_bytes()) for p in SB.rglob("*") if p.is_file()]
        try:
            fn()
            rc, failed = run_invariants()
            hit = rc != 0 and expect in failed
            if hit:
                caught += 1
            else:
                missed += 1
            print(f"  {'✅' if hit else '❌'} {name}")
            print(f"       期望失败={expect} | exit={rc} | 实际失败={failed or '（无）'}")
            if not hit:
                print("       ⚠️ 注入未被对应的断言捕获 —— 该断言可能已失效")
        finally:
            for p, b in saved:
                p.write_bytes(b)
    print()

    # ── ② 快照篡改 ──────────────────────────────────────────────
    print("─── ② 快照分类判据（篡改基线，验证 INFO/WARN 方向）───")
    snap_ok = 0
    shutil.copy2(BASELINE, BAK)
    try:
        for name, kind, min_warn, min_info in SNAP_CASES:
            mutate_baseline(kind)
            w, i = run_snapshot()
            ok = w >= min_warn and i >= min_info
            snap_ok += 1 if ok else 0
            print(f"  {'✅' if ok else '❌'} {name}")
            print(f"       期望 WARN≥{min_warn} INFO≥{min_info} | 实际 WARN={w} INFO={i}")
    finally:
        shutil.copy2(BAK, BASELINE)
        BAK.unlink(missing_ok=True)
    print()

    # ── ③ 源码破坏（前瞻窗口语义）──────────────────────────────
    print("─── ③ 源码破坏（把前瞻窗口改回错误写法，必须被语义断言抓到）───")
    code_ok = 0
    original = CODE_TARGET.read_bytes()
    shutil.copy2(CODE_TARGET, CODE_BAK)
    try:
        for name, expect, good, bad in CODE_CASES:
            text = original.decode("utf-8")
            if text.count(good) != 1:
                print(f"  ❌ {name}")
                print(f"       目标行未唯一匹配（命中 {text.count(good)} 次）"
                      f" —— 自测失效，请同步 CODE_CORRECT")
                continue
            CODE_TARGET.write_text(text.replace(good, bad), encoding="utf-8")
            rc, failed = run_forward()
            hit = rc != 0 and expect in failed
            code_ok += 1 if hit else 0
            print(f"  {'✅' if hit else '❌'} {name}")
            print(f"       期望失败={expect} | exit={rc} | 实际失败={failed or '（无）'}")
            if not hit:
                print("       ⚠️ 注入未被对应的断言捕获 —— 该断言可能已失效")
    finally:
        CODE_TARGET.write_bytes(original)
        CODE_BAK.unlink(missing_ok=True)
    source_restored = CODE_TARGET.read_bytes() == original
    print(f"  {'✅' if source_restored else '❌'} 源码已逐字节还原 {CODE_TARGET.name}")
    print()

    # ── ④ 完备性探针（宽度零值）─────────────────────────────────
    print("─── ④ 完备性探针（把最新交易日 breadth 集体置 0，应报 BREADTH_ZERO）───")
    comp_ok = 0
    full_p = SB / "industry/industry_daily_full.parquet"
    saved_full = full_p.read_bytes()
    try:
        dd = pd.read_parquet(full_p)
        dd["date"] = pd.to_datetime(dd["date"])
        tgt = dd["date"].max().strftime("%Y-%m-%d")
        dd.loc[dd["date"] == dd["date"].max(), "breadth"] = 0.0
        dd.to_parquet(full_p, index=False)
        out = run_completeness(tgt)
        comp_hit = BREADTH_CASE_CODE in out
        comp_ok += 1 if comp_hit else 0
        print(f"  {'✅' if comp_hit else '❌'} {tgt} breadth 置 0 ⇒ 期望 {BREADTH_CASE_CODE}")
        if not comp_hit:
            print(f"       ⚠️ 探针未命中 —— 检查 check_breadth 是否接入 main()；"
                  f"输出片段: {out.strip().splitlines()[-3:] if out.strip() else '（空）'}")
    finally:
        full_p.write_bytes(saved_full)
    print()

    # ── ⑤ 恢复后复验 + 真实数据根守卫 ────────────────────────────
    print("─── ⑤ 恢复后复验 + 真实数据根守卫 ───")
    rc, failed = run_invariants()
    rc_fwd, failed_fwd = run_forward()
    gt = guard_marker.stat().st_mtime
    leaked = [p for p in SRC.rglob("*") if p.is_file() and p.stat().st_mtime > gt]
    baseline_clean = not BAK.exists()

    print(f"  {'✅' if rc == 0 else '❌'} 恢复后结构断言 exit={rc} failed={failed}")
    print(f"  {'✅' if rc_fwd == 0 else '❌'} 恢复后前瞻语义断言 exit={rc_fwd} failed={failed_fwd}")
    print(f"  {'✅' if not leaked else '❌'} 真实数据根零写入（{SRC}）"
          + (f" —— 泄漏 {[str(x) for x in leaked[:3]]}" if leaked else ""))
    print(f"  {'✅' if baseline_clean else '❌'} 基线兜底文件已清理（无 .selftest-bak 残留）")
    print(f"  {'✅' if source_restored else '❌'} 源码兜底文件已清理 + 逐字节还原")

    total_ok = (caught == len(CASES) and snap_ok == len(SNAP_CASES)
                and code_ok == len(CODE_CASES) and comp_ok == 1
                and rc == 0 and rc_fwd == 0 and not leaked
                and baseline_clean and source_restored)
    print("\n" + "═" * 76)
    print(f"结构破坏  {caught}/{len(CASES)} 被抓到"
          f"｜快照判据  {snap_ok}/{len(SNAP_CASES)} 符合预期"
          f"｜源码破坏  {code_ok}/{len(CODE_CASES)} 被抓到"
          f"｜完备性探针  {comp_ok}/1"
          f"｜恢复后 {'全绿' if (rc == 0 and rc_fwd == 0) else '未全绿'}")
    print(("✅ 套件有效 —— 每条断言都具备抓取能力" if total_ok
           else "❌ 套件存在问题，见上方明细"))
    return 0 if total_ok else 1


if __name__ == "__main__":
    sys.exit(main())
