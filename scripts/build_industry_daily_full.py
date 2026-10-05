#!/usr/bin/env python3
"""
构建行业日频全量数据集 industry_daily_full.parquet

数据链: market/daily 全历史 + PIT 成分快照 → compute_concept_daily 聚合
背景: 原数据集为 hermes 时期 ad-hoc 产出（无代码生成器，2026-08-06 后断更），
本脚本补上生成器，供 mainline_detector / 回测脚本使用。

成分口径（2026-10-01 修订）:
  聚合按【当日真实成分】进行（PIT，来自 industry_members_daily.parquet）。
  旧实现传静态快照 industry_members.parquet，等于用 2026 年的成分回填 2020 年，
  早期日期混入当时尚未纳入该行业的个股（实测 2020-01-02 静态 5644 只 vs 真实 3817 只）。
  本产物是冷区信号（src/signals/sources.py）的唯一输入，偏差会直接传导为信号 alpha。
  缺 PIT 快照时默认报错退出，需显式加 --allow-static-fallback 才回退。

用法:
  python3 scripts/build_industry_daily_full.py [--days 2000]
  python3 scripts/build_members_daily.py && python3 scripts/build_industry_daily_full.py --force-overwrite

日更建议: --days 60（breadth 依赖 MA20/MA13，需 ≥33 天预热；按日期 upsert 不截断历史）
"""
import sys
import argparse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paths import INDUSTRY_DIR, MARKET_DIR
from src.utils.market_data import (
    MA20_WINDOW,
    load_recent_market,
    compute_concept_daily,
    load_industry_members,
    load_industry_members_daily,
)
from src.utils.provenance import save_dataframe

OUT_PATH = INDUSTRY_DIR / "industry_daily_full.parquet"


# ── 写入护栏（2026-10-03）──────────────────────────────────────────────
# 背景：2026-07-01~07-13 连续 9 个交易日、131 个行业的 breadth 全为 0（同期
# avg_pct / total_amount / n_stocks 正常）。根因 = 当时本次窗口不含 MA20 预热所需的
# 先验历史 ⇒ ma20 未成型 ⇒ above_ma20 全为 False ⇒ breadth 落成 0；而 upsert 对
# 「窗口外历史」原样保留，使该坏值在窗口滚过后被永久固化、不再重算。
# 详见 docs/changes-2026-10-03-daily-full-breadth-anomaly.md
#
# 教训：窗口式 upsert × 上游延迟补数 = 不可逆历史坏值，且任何单日校验都发现不了。
MIN_WARMUP_DAYS = 33          # breadth 依赖 MA20/MA13，预热需 ≥33 天（见模块 docstring）
MAX_DEGENERATE_RATIO = 0.80   # 新窗口内「各交易日 breadth 为 0/NaN 占比」的【中位数】上限


def _global_market_start() -> pd.Timestamp:
    """**全局**最早的行情日（不是本次窗口的起点）。

    ⚠️ 2026-10-05 修正：`_preflight_guard` 与 `_drop_warmup` 原先都用
    `daily.date.min() <= mkt.date.min()` 判「是否全历史重建」—— 但窗口式运行时
    这两个值**恒等**（聚合只覆盖加载窗口）⇒ 该判据**恒真** ⇒ 判据③（P0-2 退化检测）
    在日更链路上**永远不会执行**（单元测试直接构造帧，所以没暴露）。
    正确的参照系是全局数据起点。
    """
    files = sorted(MARKET_DIR.glob("*.parquet"))
    return pd.Timestamp(files[0].stem) if files else pd.NaT


def _preflight_guard(daily: pd.DataFrame, mkt: pd.DataFrame,
                     force_overwrite: bool) -> str:
    """写入前自检。返回空串 = 通过；否则返回拒绝原因（非空即应拒写）。

    判据（顺序即优先级）：
      ① 预热充足性 —— 行情窗口天数 < MIN_WARMUP_DAYS 直接拒；
      ② 数据起点豁免 —— 窗口从最早的行情日开始（全历史重建）时，MA 未成型属正常；
      ③ 输出退化自检 —— 逐日算「breadth 为 0 或 NaN」的行业占比，取【中位数】与阈值比。
         - 0   = 旧语义下 ma20 未成型（`close > NaN` → False）；
         - NaN = 现语义下 `breadth_dropna=True` 把 ma20 未成型的个股排除出分母后、
                 分母为空的结果。两者都表示「MA 没算出来」，必须一起抓。
         用中位数而非均值/最大值：单日市场级跌停（如 2020-02-03 实测 56.5% 行业
         宽度为 0）属真实行情，不应触发；而坏值场景是【每一天】都接近 100%。
    """
    if force_overwrite:
        return ""      # --force-overwrite 用于全量重建，早期日期宽度低属正常

    d_new = pd.to_datetime(daily["date"]).dt.normalize()
    d_mkt = pd.to_datetime(mkt["date"]).dt.normalize()

    n_days = int(d_mkt.nunique())
    if n_days < MIN_WARMUP_DAYS:
        return (f"行情窗口仅 {n_days} 个交易日 < MA20/MA13 预热所需的 "
                f"{MIN_WARMUP_DAYS} 天，breadth 必然退化")

    if d_new.min() <= _global_market_start():
        return ""      # 窗口起点即**全局**数据起点 ⇒ 全历史重建，豁免

    degenerate = ((daily["breadth"] == 0) | daily["breadth"].isna()).to_numpy()
    per_day = (pd.DataFrame({"date": d_new, "bad": degenerate})
               .groupby("date")["bad"].mean())
    med = float(per_day.median())
    if med >= MAX_DEGENERATE_RATIO:
        return (f"本次结果中 breadth 为 0/NaN 的行业占比，逐日中位数为 {med:.1%}"
                f"（上限 {MAX_DEGENERATE_RATIO:.0%}）—— 疑似 MA 未成型导致宽度整体退化")
    return ""


# ── 预热护栏（2026-10-05）──────────────────────────────────────────────
# 背景：MA20/MA13 的 min_periods 已从 5 改为**窗口长度**（"MA 未成型不进分母"的正确语义）。
# 于是「加载窗口最前面的 19 个交易日」所有个股的均线都未成型 ⇒ 宽度退化为 NaN；
# 旧口径（min_periods=5）下这些日期不是 NaN，而是**用不足 20 日的短窗口算出的错值**
# —— 实测 60 日窗口前 19 天有 56.8% 的行与全历史口径不一致、平均 |Δ|=8.5pp，
# 且会被 upsert 固化。两种都不该写进产物。
MA_WARMUP_DAYS = MA20_WINDOW - 1     # 19：窗口最前面这 19 天，MA20 一律未成型


def _drop_warmup(daily: pd.DataFrame, mkt: pd.DataFrame,
                 force_overwrite: bool = False,
                 warmup: int = MA_WARMUP_DAYS):
    """丢弃加载窗口最前面 `warmup` 个交易日的行，避免把"均线未成型"写进产物。

    豁免（与 `_preflight_guard` 的判据②一致）：
      · `--force-overwrite` 全量重建；
      · 窗口起点 = 数据起点（同样是全历史重建）。
      这两种情况下这些日期**应当存在**（否则 daily_full 的日期集合会小于
      weighted_returns，直接违反 tests 的 test_date_subsets_of_full），
      故保留行、宽度为 NaN —— 这是"未成型"的诚实表达。

    返回 (新 daily, 丢弃行数, 豁免原因)；豁免原因为空串表示未豁免。
    """
    if warmup <= 0 or daily.empty:
        return daily, 0, ""
    d_mkt = pd.DatetimeIndex(sorted(pd.to_datetime(mkt["date"]).dt.normalize().unique()))
    if force_overwrite:
        return daily, 0, "--force-overwrite（全量重建）"
    if pd.to_datetime(daily["date"]).min() <= _global_market_start():
        return daily, 0, "窗口起点=**全局**数据起点（全历史重建）"
    if len(d_mkt) <= warmup:
        return daily.iloc[0:0], len(daily), ""
    cut = d_mkt[warmup]                      # 第一个"预热充分"的交易日
    keep = pd.to_datetime(daily["date"]).dt.normalize() >= cut
    return daily[keep].copy(), int((~keep).sum()), ""


def main(days: int = 2000, force_overwrite: bool = False,
         allow_static_fallback: bool = False):
    print("[1/3] 加载行业成员映射...")
    members = load_industry_members()
    if not members:
        print("❌ 无行业成员映射（先运行 scripts/pull_futu_industry.py）")
        return 1
    print(f"  {len(members)} 个行业（静态快照，仅用于行业全集/名称）")

    # PIT 成分快照：消除前视偏差的关键输入（2026-10-01）
    # 若不传，聚合会按【当前】成分回填全历史 —— 早期日期将混入当时尚未纳入
    # 该行业的个股（实测 2020-01-02 静态表 5644 只 vs 真实 3817 只），
    # 而下游冷区信号正是以本产物为唯一输入，偏差会直接传导成信号 alpha。
    members_daily = load_industry_members_daily()
    if members_daily.empty:
        msg = ("缺 PIT 成分快照 industry_members_daily.parquet，"
               "只能按当前成分回填全历史（前视偏差）")
        if not allow_static_fallback:
            print(f"❌ {msg}")
            print("   修复: python3 scripts/build_members_daily.py（重建 PIT 成分快照）")
            print("   确需回退静态快照时再加 --allow-static-fallback。")
            return 1
        print(f"  ⚠️ {msg} —— 已指定 --allow-static-fallback，按静态快照继续")
    else:
        print(f"  PIT 成分快照: {len(members_daily):,} 行 / "
              f"{members_daily['date'].nunique()} 天 "
              f"({members_daily['date'].min().date()} ~ {members_daily['date'].max().date()})")

    print("[2/3] 加载全历史行情...")
    mkt = load_recent_market(days=days)
    if mkt.empty:
        print("❌ 无行情数据")
        return 1
    print(f"  {len(mkt)} 行, {mkt['date'].nunique()} 个交易日")

    print("[3/3] 计算行业日频聚合（131 行业 × 全历史，约数分钟）...")
    daily = compute_concept_daily(
        mkt, members,
        members_daily=members_daily if not members_daily.empty else None,
    )
    if daily.empty:
        print("❌ 聚合结果为空")
        return 1

    # ── 预热护栏（2026-10-05）：丢弃加载窗口前 N 天的行 ──
    # 原因：MA20/MA13 的 min_periods 已改为窗口长度（"MA 未成型不进分母"的正确语义），
    #   于是加载窗口最前面的 N-1 个交易日**所有个股的均线都未成型** ⇒ 宽度退化为 NaN。
    #   旧口径（min_periods=5）下这些日期不是 NaN 而是**用不足 20 日的短窗口算出的错值**
    #   （实测 60 日窗口前 19 天：56.8% 的行与全历史口径不一致、平均 |Δ|=8.5pp），
    #   且会被 upsert 固化。这里**直接不写**这些日期 —— 让文件里已有的历史值原样保留，
    #   既不写入错值、也不写入 NaN。
    daily, n_warm, warm_exempt = _drop_warmup(daily, mkt, force_overwrite)
    if n_warm:
        print(f"  ⏭️ 丢弃加载窗口前 {MA_WARMUP_DAYS} 个交易日的 {n_warm:,} 行"
              f"（均线未成型；这些日期的旧值由 upsert 原样保留）")
    elif warm_exempt:
        print(f"  ℹ️ 预热护栏豁免：{warm_exempt} ⇒ 窗口前 {MA_WARMUP_DAYS} 天的宽度为 NaN（未成型）")
    if daily.empty:
        print("❌ 预热护栏后无剩余行（--days 过小）；请提高到 ≥"
              f"{MA_WARMUP_DAYS + 1} 天")
        return 1

    # ── 写入护栏（2026-10-03）：窗口内输入不足即拒写 ──
    # 不检查的结果会被 upsert 固化进历史，且窗口滚过后不再重算（不可逆）。
    reason = _preflight_guard(daily, mkt, force_overwrite)
    if reason:
        print(f"❌ 写入前自检未通过：{reason}")
        print("   已拒绝写入 —— 否则该退化结果会被 upsert 固化进历史、窗口滚过后不再重算。")
        print("   排查顺序：① 该段日期在 market/daily 里是否缺文件 / 文件内容不全"
              "（上游回填晚于本脚本）；② --days 是否过小（需 ≥33）；"
              "③ 确属全量重建时改用 --force-overwrite。")
        return 1

    # ── 按日期 upsert（不可整文件覆盖：日更只算近 --days 天，覆盖写会截断历史）──
    # 保护规则（2026-10-01）：upsert 拿不到可信结果时**拒绝写入**，不再退回整文件覆盖
    # —— 后者会把全历史静默截断成当次窗口（原实现的隐患）。
    key = ["date", "con_code"]
    n_existing = 0

    if force_overwrite:
        print(f"  ⚠️ --force-overwrite：跳过 upsert，按本次结果整表覆盖（{len(daily)} 行）")
    elif not OUT_PATH.exists():
        pass
    elif not all(k in daily.columns for k in key):
        print(f"❌ 已有 {OUT_PATH.name}，但本次结果缺少关键列 {key}，拒绝覆盖以免截断历史。")
        return 1
    else:
        try:
            prev = pd.read_parquet(OUT_PATH)
            if not all(k in prev.columns for k in key):
                raise ValueError(f"旧文件缺少关键列 {key}，实际列={list(prev.columns)[:8]}")
            n_existing = len(prev)
            d_new = pd.to_datetime(daily["date"])
            d_old = pd.to_datetime(prev["date"])
            lo, hi = d_new.min(), d_new.max()
            kept = prev[(d_old < lo) | (d_old > hi)]
            merged = pd.concat([kept, daily], ignore_index=True)
            merged = merged.drop_duplicates(subset=key, keep="last")
            merged = merged.sort_values(key).reset_index(drop=True)
            # 护栏（2026-10-01 修订）：保证「窗口外历史」逐条完整保留。
            # 不再比对总行数 —— PIT 口径下同一天的行业数会变（实测 2020 年只有
            # 127 个行业而非 131），全量重建时总行数下降属正常，不是历史丢失。
            kept_keys = set(zip(pd.to_datetime(kept["date"]), kept["con_code"]))
            merged_keys = set(zip(pd.to_datetime(merged["date"]), merged["con_code"]))
            lost = kept_keys - merged_keys
            if lost:
                raise ValueError(
                    f"upsert 后窗口外历史丢失 {len(lost)} 条（例如 {sorted(lost)[:3]}）")
            daily = merged
            print(f"  upsert: 保留窗口外 {len(kept)} 条，本次覆盖 {lo.date()} ~ {hi.date()}"
                  f"，{n_existing} → {len(daily)} 行")
        except Exception as e:
            print(f"❌ upsert 失败：{e}")
            print(f"   已拒绝写入 —— 否则会把 {n_existing} 行历史截断为本次窗口的 {len(daily)} 行。")
            print("   修复后重跑；确认要整表重建时加 --force-overwrite。")
            return 1

    written = save_dataframe(
        daily, OUT_PATH,
        producer="scripts/build_industry_daily_full.py",
        params={"days": days, "force_overwrite": force_overwrite,
                "allow_static_fallback": allow_static_fallback},
        inputs=[MARKET_DIR,
                INDUSTRY_DIR / "industry_members_daily.parquet",
                INDUSTRY_DIR / "industry_members.parquet"],
        extra={"n_existing_before": n_existing},
    )
    print(f"✅ 已保存 {written}")
    print(f"   {len(daily)} 行, {daily['con_code'].nunique()} 行业, "
          f"{daily['date'].min().date()} ~ {daily['date'].max().date()}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2000)
    parser.add_argument("--force-overwrite", action="store_true",
                        help="跳过 upsert，按本次结果整表覆盖（仅用于全量重建）")
    parser.add_argument("--allow-static-fallback", action="store_true",
                        help="缺 PIT 成分快照时允许退回静态快照（结果含前视偏差，仅供应急）")
    args = parser.parse_args()
    sys.exit(main(args.days, args.force_overwrite, args.allow_static_fallback) or 0)
