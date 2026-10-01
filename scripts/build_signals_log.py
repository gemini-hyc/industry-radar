#!/usr/bin/env python3
"""
信号台账构建与维护

用法:
  python3 scripts/build_signals_log.py                       # 每日：当日快照 + 回填前向收益
  python3 scripts/build_signals_log.py --backfill-cold-zone  # 首次：全历史回填冷区信号（约 1~3 分钟）
  python3 scripts/build_signals_log.py --backfill-cold-zone --start 2020-01-01 --end 2026-09-23
  python3 scripts/build_signals_log.py --no-snapshot         # 只回填收益，不采集当日快照
  python3 scripts/build_signals_log.py --out /tmp/x.parquet  # 覆盖台账路径（测试用）
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from src.signals import ledger as L  # noqa: E402
from src.signals.sources import cold_zone_history, snapshot_signals  # noqa: E402


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
    ap.add_argument("--backfill-cold-zone", action="store_true",
                    help="全历史回填冷区宽度反转信号（首次建账用）")
    ap.add_argument("--start", default=None, help="冷区回填起点 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="冷区回填终点 YYYY-MM-DD")
    ap.add_argument("--no-crowding", action="store_true", help="回填时不聚合拥挤度特征（更快）")
    ap.add_argument("--no-margin", action="store_true", help="回填时不附带两融占比分位特征")
    ap.add_argument("--no-snapshot", action="store_true", help="跳过当日快照采集")
    ap.add_argument("--no-backfill-returns", action="store_true", help="跳过前向收益回填")
    ap.add_argument("--out", default=None, help="台账路径（默认 <数据根>/signals/signals_log.parquet）")
    args = ap.parse_args()

    obs_date = args.date or _latest_trade_date()

    rows = []
    if args.backfill_cold_zone:
        print(f"🔁 全历史回填冷区信号：{args.start or '最早'} → {args.end or '最新'} …")
        cz = cold_zone_history(start=args.start, end=args.end,
                               with_crowding=not args.no_crowding,
                               with_margin=not args.no_margin)
        print(f"   冷区信号 {len(cz)} 条")
        rows += cz

    if not args.no_snapshot:
        snap = snapshot_signals(obs_date)
        print(f"📸 {obs_date} 快照：{len(snap)} 条（跟踪池入场事件 + 主线成员状态）")
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
