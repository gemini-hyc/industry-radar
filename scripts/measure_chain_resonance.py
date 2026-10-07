#!/usr/bin/env python3
"""测量层日频产出 —— (链, 日) 的**双输出** + 风格解释份额 + 退化探针状态。

产出（均登记 src/paths.py:ARTIFACTS，走 src/utils/provenance 落指纹）：
    industry_residual_panel.parquet  残差收益面板 r̃（raw / adj），复现三件套第 ② 件
    chain_resonance_daily.csv        (链, 日)：resonance_raw / resonance_adj / style_share / z_adj
    style_probe_status.json          探针状态（供 scripts/check_data_completeness.py 消费）

⛔ **先验名单必须已锁定**（review §11.4）才能跑 —— 未锁定直接退出。

⚠️ **注意**：本脚本每一日都要为每条链算一次分层零分布（默认 2,000 次抽样），
全历史重算很慢 ⇒ 默认只算**最新交易日**；回溯用 `--days N`。

用法:
    python3 scripts/measure_chain_resonance.py                      # 只算最新交易日
    python3 scripts/measure_chain_resonance.py --days 20            # 回溯近 20 个交易日
    python3 scripts/measure_chain_resonance.py --prior docs/plan/chain-prior-list.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.paths import INDUSTRY_DIR  # noqa: E402
from src.resonance import config as C  # noqa: E402
from src.resonance import group as G  # noqa: E402
from src.resonance import measure as M  # noqa: E402
from src.resonance import panel as P  # noqa: E402
from src.resonance import probe as PR  # noqa: E402
from src.utils.provenance import save_dataframe, save_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRIOR = ROOT / "docs" / "plan" / "chain-prior-list.csv"
PRIOR_LOCK = ROOT / "docs" / "plan" / "chain-prior.lock.json"
RESIDUAL_PATH = INDUSTRY_DIR / "industry_residual_panel.parquet"
RESONANCE_PATH = INDUSTRY_DIR / "chain_resonance_daily.csv"
PROBE_STATUS_PATH = INDUSTRY_DIR / "style_probe_status.json"
STYLE_FACTOR_PATH = INDUSTRY_DIR / "industry_style_factors.parquet"
STYLE_PANEL_PATH = INDUSTRY_DIR / "industry_style_panel.parquet"


def assert_prior_locked(prior: Path) -> dict:
    """⛔ 未锁定的先验名单 ⇒ 拒绝跑数据（review §11.4 铁律）。"""
    import hashlib
    if not prior.exists():
        raise SystemExit(f"❌ 先验名单不存在：{prior}")
    if not PRIOR_LOCK.exists():
        raise SystemExit(
            f"❌ 先验名单尚未锁定（缺 {PRIOR_LOCK}）⇒ **不得跑链级测量**\n"
            f"   先锁定：python3 tools/lock_chain_prior.py --list {prior} "
            f"--source '...' --author '...' --lock")
    rec = json.loads(PRIOR_LOCK.read_text(encoding="utf-8"))
    digest = hashlib.sha256(prior.read_bytes()).hexdigest()
    if rec.get("sha256") != digest:
        raise SystemExit("❌ 先验名单自锁定后被修改（哈希不符）⇒ 请新建 v2 并重新锁定，"
                         "不得原地改动后再跑（否则命中率不可追溯）")
    print(f"✅ 先验名单已锁定：{rec['n_chains']} 条链 / {rec['n_members']} 成员 / "
          f"sha256 {rec['sha256'][:12]}…（来源 {rec['source']}）")
    return rec


def load_style_factors():
    """优先读已落盘的因子面板（一等产物）；没有就现算（要求 FR 面板存在）。"""
    if STYLE_FACTOR_PATH.exists():
        F = pd.read_parquet(STYLE_FACTOR_PATH)
        F["date"] = pd.to_datetime(F["date"])
        F = F.set_index("date").sort_index()
        print(f"· 读取因子面板产物 {STYLE_FACTOR_PATH.name}：{F.shape[0]} 日 × {F.shape[1]} 因子")
        return F
    print("· 未找到因子面板产物 ⇒ 现算（建议先跑 scripts/build_style_panel.py 落盘）")
    _, F, _ = P.build_all()
    return F


def load_characteristics() -> pd.DataFrame | None:
    if not STYLE_PANEL_PATH.exists():
        return None
    return pd.read_parquet(STYLE_PANEL_PATH)


def main() -> int:
    ap = argparse.ArgumentParser(description="(链, 日) 共振强度双输出 + 探针")
    ap.add_argument("--prior", default=str(DEFAULT_PRIOR), help="先验名单 CSV")
    ap.add_argument("--days", type=int, default=1, help="回溯交易日数（默认只算最新）")
    ap.add_argument("--n-iter", type=int, default=C.N_ITER, help="分层零分布抽样次数")
    ap.add_argument("--no-null", action="store_true", help="跳过零分布（只出标量，快）")
    ap.add_argument("--no-write", action="store_true", help="只打印不落盘")
    a = ap.parse_args()

    lock = assert_prior_locked(Path(a.prior))

    R = P.load_returns()
    F = load_style_factors().reindex(R.index)
    X = load_characteristics()

    print(f"· 面板 {R.shape[0]} 日 × {R.shape[1]} 行业；收益层残差化（窗口 {C.WINDOW_DAYS}，"
          f"β 用 [t−W, t−1] 估计后作用在 t）…")
    rr = M.residualize(R, F)
    print(f"  r̃ raw 非空 {int(rr.raw.notna().to_numpy().sum()):,} 格 / "
          f"adj 非空 {int(rr.adj.notna().to_numpy().sum()):,} 格")

    chains = M.load_prior_chains(a.prior, list(R.columns))
    print(f"· 先验链 {len(chains)} 条（面板内可用）；成员中位数 "
          f"{int(pd.Series([len(v) for v in chains.values()]).median())}")

    codes = list(R.columns)
    l1_of = G.load_l1_of(codes)
    pools = G.l1_pools(l1_of, codes)

    corrs_raw = M.rolling_corr(rr.raw)
    corrs_adj = M.rolling_corr(rr.adj)
    dates = sorted(set(corrs_raw) & set(corrs_adj))[-max(1, a.days):]
    corrs_raw = {d: corrs_raw[d] for d in dates}
    corrs_adj = {d: corrs_adj[d] for d in dates}
    print(f"· 测量日期 {len(dates)} 个：{dates[0].date()} … {dates[-1].date()}")

    res_df = M.measure_chains(chains, corrs_raw, corrs_adj, codes, l1_of, pools,
                              n_iter=a.n_iter, with_null=not a.no_null)

    # ── 探针（含金丝雀链）──
    gap_series = (res_df.groupby("date")["style_share"].mean()
                  if len(res_df) else pd.Series(dtype=float))
    probe = PR.run_probes(F, None if X is None else X.tail(C.WINDOW_DAYS),
                          rr.n_factors_used, rr.adj, rr.raw, rr.betas, codes,
                          corrs_raw, corrs_adj, l1_of,
                          gap_series=gap_series, target_date=dates[-1])

    print("\n" + "═" * 62)
    print(f"  探针结论：{'✅ 健康' if probe.ok else '⚠️ 退化（日报必须标注未调整）'}")
    for i in probe.issues:
        mark = "⚠️" if i["level"] == "warning" else "❌"
        print(f"  {mark} [{i['code']}] {i['detail']}")
    print("═" * 62)

    if len(res_df):
        top = res_df[res_df["date"] == res_df["date"].max()].sort_values(
            "resonance_raw", ascending=False)
        cols = ["chain_id", "n_members", "resonance_raw", "resonance_adj", "style_share"]
        if "z_adj" in top.columns:
            cols.append("z_adj")
        print(f"\n· 最新交易日 {top['date'].iloc[0].date()} 的前 8 条链（按原始共振）：")
        print(top[cols].head(8).to_string(index=False))
        print(f"\n  风格解释份额（原始−调整）均值 {res_df['style_share'].mean():+.4f}｜"
              f"中位 {res_df['style_share'].median():+.4f}｜"
              f"最大 {res_df['style_share'].max():+.4f}")
        if not probe.ok:
            print("\n  ⚠️ 探针未通过 ⇒ 日报正文需带：")
            print("  " + probe.banner().replace("\n", "\n  "))

    if a.no_write:
        print("\n--no-write：未落盘")
        return 0 if probe.ok else 2

    # ── 落盘 ──
    long_raw = rr.raw.stack().rename("resid_raw")
    long_adj = rr.adj.stack().rename("resid_adj")
    panel_df = pd.concat([long_raw, long_adj], axis=1).reset_index()
    panel_df.columns = ["date", "ind_code", "resid_raw", "resid_adj"]
    p1 = save_dataframe(panel_df, RESIDUAL_PATH, producer="scripts/measure_chain_resonance.py",
                        params=C.spec_dict(), extra={"artifact_role": "residual_panel"})
    p2 = save_dataframe(res_df, RESONANCE_PATH, producer="scripts/measure_chain_resonance.py",
                        params=C.spec_dict(),
                        extra={"artifact_role": "chain_resonance",
                               "prior_list": lock["list_file"],
                               "prior_sha256": lock["sha256"],
                               "null_stratified": not a.no_null})
    p3 = save_json({"ok": probe.ok, "issues": probe.issues, "stats": probe.stats,
                    "target_date": str(dates[-1].date()) if len(dates) else None,
                    "spec_window": C.WINDOW_DAYS, "factors": list(C.FACTORS)},
                   PROBE_STATUS_PATH, producer="scripts/measure_chain_resonance.py",
                   params=C.spec_dict())
    print(f"\n✅ 已写入：\n  {p1}\n  {p2}\n  {p3}")
    return 0 if probe.ok else 2


if __name__ == "__main__":
    sys.exit(main())
