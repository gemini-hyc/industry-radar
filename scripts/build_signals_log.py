#!/usr/bin/env python3
"""
信号台账构建与维护

用法:
  python3 scripts/build_signals_log.py                       # 每日：当日快照 + 回填前向收益
  python3 scripts/build_signals_log.py --no-snapshot         # 只回填收益，不采集当日快照
  python3 scripts/build_signals_log.py --out /tmp/x.parquet  # 覆盖台账路径（测试用）

🔴 2026-10-06弃用：`--backfill-cold-zone` 全历史回填入口已移除（冷区信号退场）。
   台账中 2,234 条 cold_zone 历史保留仅为可追溯，不再新增。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from src.signals import ledger as L  # noqa: E402
from src.signals.sources import snapshot_signals  # noqa: E402


def _latest_trade_date() -> str:
    """
    默认快照日 = 最近一个交易日（而非"今天"）。

    原实现用 now()：在周末/节假日手工补跑时会写入 signal_date 不在收益面板内的行，
    其前向收益将永久为 NaN。生产链路（cron）显式传交易日，不受此影响。
    """
    try:
        from src.paths import INDUSTRY_DIR

        wr = pd.read_parquet(INDUSTRY_DIR / "industry_weighted_returns.parquet", columns=["date"])
        d = pd.to_datetime(wr["date"])
        d = d[d <= pd.Timestamp.now().normalize()]
        if len(d):
            return d.max().strftime("%Y-%m-%d")
    except Exception:
        pass
    return pd.Timestamp.now().strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(description="信号台账构建与维护")
    ap.add_argument("--date", default=None,
                    help="快照日期 YYYY-MM-DD（默认最近一个交易日，避免非交易日写坏信号日）")
    ap.add_argument("--no-snapshot", action="store_true", help="跳过当日快照采集")
    ap.add_argument("--no-backfill-returns", action="store_true", help="跳过前向收益回填")
    ap.add_argument("--out", default=None, help="台账路径（默认 <数据根>/signals/signals_log.parquet）")
    args = ap.parse_args()

    obs_date = args.date or _latest_trade_date()

    rows = []
    # 🔴 2026-10-06 弃用：冷区全历史回填入口（`--backfill-cold-zone/--start/--end/
    #    --no-crowding/--no-margin`）已移除。冷区信号退场，台账中2,234 条历史保留但不再新增。
    #    依据 docs/plan/retirement-breadth-tracker-plan.md §2.2。

    if not args.no_snapshot:
        snap = snapshot_signals(obs_date)
        print(f"📸 {obs_date} 快照：{len(snap)} 条（主线成员状态）")
        rows += snap

    ledger = L.load_ledger(args.out)
    before = len(ledger)
    merged = L.upsert(ledger, rows)
    added = len(merged) - before
    if not args.no_backfill_returns:
        merged = L.backfill_forward_returns(merged)

    path = L.save_ledger(merged, args.out)
    print(f"✅ 台账已保存: {path}")
    print(f"   记录数 {before} → {len(merged)}（净新增 {added}）")

    mt = L.maturity_table(merged)
    print("   到期情况: " + " | ".join(
        f"{int(r.horizon)}日 {int(r.matured)}/{int(r.total)}" for r in mt.itertuples()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
