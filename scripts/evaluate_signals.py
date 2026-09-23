#!/usr/bin/env python3
"""
信号台账评估（事件研究）

用法:
  python3 scripts/evaluate_signals.py                    # 评估现有台账
  python3 scripts/evaluate_signals.py --backfill         # 先回填前向收益再评估
  python3 scripts/evaluate_signals.py --source cold_zone # 只看某个来源
  python3 scripts/evaluate_signals.py --out /tmp/x.parquet --report /tmp/eval.md

产出：终端打印 markdown + 落盘 <数据根>/signals/eval_YYYYMMDD.md
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.signals import evaluate as E  # noqa: E402
from src.signals import ledger as L  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="信号台账评估（事件研究）")
    ap.add_argument("--out", default=None, help="台账路径（默认 <数据根>/signals/signals_log.parquet）")
    ap.add_argument("--report", default=None, help="评估报告输出路径")
    ap.add_argument("--source", default=None, help="只评估某个来源（cold_zone/tracker_pool/mainline）")
    ap.add_argument("--horizons", default=None, help="持有期列表，如 5,10,20,60")
    ap.add_argument("--backfill", action="store_true", help="评估前先回填前向收益并保存")
    args = ap.parse_args()

    ledger = L.load_ledger(args.out)
    if ledger.empty:
        print("❌ 台账为空。先运行: python3 scripts/build_signals_log.py --backfill-cold-zone")
        return 1

    if args.backfill:
        ledger = L.backfill_forward_returns(ledger)
        L.save_ledger(ledger, args.out)
        print("✅ 前向收益已回填")

    if args.source:
        before = len(ledger)
        ledger = ledger[ledger["source"] == args.source]
        print(f"🔎 过滤来源 {args.source}: {before} → {len(ledger)} 条")
        if ledger.empty:
            print("❌ 该来源无记录")
            return 1

    horizons = tuple(int(x) for x in args.horizons.split(",")) if args.horizons else L.HORIZONS

    md = E.render_markdown(ledger, horizons=horizons)
    print(md)
    path = E.save_eval_report(md, args.report)
    print(f"\n✅ 评估报告已保存: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
