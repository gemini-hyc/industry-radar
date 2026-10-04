#!/usr/bin/env python3
"""tests/run_tests.py —— 回归基线测试统一入口（零新增依赖）。

一条命令跑完两层：
    ① 结构不变量（硬失败）—— tests/test_invariants.py
    ② 数值快照对比（软报告）—— tests/snapshot.py

退出码语义（**只有第 ① 层影响退出码**）：
    0  结构不变量全绿（数值漂移只打印，不算失败）
    1  结构不变量有失败项
    2  快照基线缺失（提示先 --update）

用法：
    python tests/run_tests.py                 # 跑全部
    python tests/run_tests.py --quiet         # 只跑结构断言
    python tests/run_tests.py --verbose       # 快照连未变化的量也打印
    INDUSTRY_RADAR_DATA=<沙箱> python tests/run_tests.py   # 对沙箱产物跑
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import C, TESTS_DIR, data_root, hr  # noqa: E402
import snapshot as snapshot_mod  # noqa: E402


def run_invariants(verbosity: int = 1) -> bool:
    hr("═")
    print(f"{C.BOLD}① 结构不变量（硬失败 —— 失败即代表有真问题）{C.END}")
    print(f"{C.DIM}数据根: {data_root()}{C.END}")
    hr("─")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(TESTS_DIR), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stdout).run(suite)
    hr("─")
    if result.wasSuccessful():
        print(f"{C.OK}✅ 结构不变量全绿：{result.testsRun} 项通过{C.END}")
    else:
        print(f"{C.BAD}❌ 结构不变量失败："
              f"{len(result.failures)} 项断言失败 / {len(result.errors)} 项异常"
              f"（共 {result.testsRun} 项）{C.END}")
    return result.wasSuccessful()


def main() -> int:
    ap = argparse.ArgumentParser(description="回归基线测试")
    ap.add_argument("--quiet", "-q", action="store_true", help="只跑结构不变量")
    ap.add_argument("--verbose", "-v", action="store_true", help="快照连未变化的量也打印")
    args = ap.parse_args()

    ok = run_invariants(verbosity=2 if args.verbose else 1)

    if not args.quiet:
        base = snapshot_mod.read_baseline()
        cur = snapshot_mod.collect()
        if base is None:
            print(f"\n{C.WARN}⚠️ 快照基线缺失。生成：python tests/snapshot.py --update{C.END}")
        else:
            print()
            snapshot_mod.compare(cur, base, args.verbose)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
