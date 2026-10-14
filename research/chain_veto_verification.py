#!/usr/bin/env python3
"""chain_veto_verification.py — 74 链否决权验证（项目 A：只读，零写入数据根）。

对应 `docs/plan/chain-veto-and-mainline-rebuild-plan.md` 项目 A：
对已锁定的 74 条候选链补做"否决权验证"——检验每条链的链内残差相关
在三段（2020-01-01~2021-12-31 / 2022-01-01~2023-12-31 / 2024-01-01~2026-09-30）上是否同质。

口径（与 `research/chain_three_class_qtest.py` 同来源，验证通道 **in-sample beta**）：
① 市场代理 = 等权 131 行业均值；截距 + 市场两列 OLS；
② 不做"全样本 |残差|>=0.40 选对"——名单已外部锁定，直接验全部 74 条链；
③ 分段每段各自 in-sample 重剥 beta（⇒ 与 chain_three_class_qtest 同口径）；
④ 链级统计量 = group_statistic（链内所有成员对的**带符号**残差相关均值）；
⑤ 主判据 = classify_three_segment（Cochran Q 同质性，p>0.05 ⇒ structure）；
⑥ 分层零分布 z 值（L1 构成匹配）作辅助诊断（跨链可比），不是判据。

⚠️ 验证通道与测量通道的 beta 口径不同（measure.py 顶部明文警告，混用即翻车）：
   · 验证通道（本脚本）：段内 in-sample beta；
   · 测量通道（measure.py）：trailing out-of-sample beta（[t−W, t−1] 估计、作用在 t）。

⚠️ 本验证的证据地位（必须传递给下游结论）：
   名单 `post_hoc=true`（`chain-prior.lock.json`）⇒ 本验证只在「对象清单」层面自洽，
   ⛔ **不构成**「共振方法发现了多少真链」的命中率证据。
   后者需要一份先于查看结果撰写的外部先验（当前没有）。

跑法：/usr/bin/python3 research/chain_veto_verification.py
输出：stdout 摘要 + ~/quant-data/_local_dev/chain_veto_verification_<日期>.csv
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/hyc/quant/industry-radar")
from src.evaluation.inference import classify_three_segment  # noqa: E402
from src.resonance import config as C  # noqa: E402
from src.resonance import group as G  # noqa: E402
from src.resonance import measure as M  # noqa: E402

WR_PATH = "/Users/hyc/quant-data/industry/industry_weighted_returns.parquet"
PRIOR = "/Users/hyc/quant/industry-radar/docs/plan/chain-prior-list.csv"
LOCK = "/Users/hyc/quant/industry-radar/docs/plan/chain-prior.lock.json"
OUT_DIR = Path("/Users/hyc/quant-data/_local_dev")
TODAY = "2026-10-11"
SEGS = [("2020-01-01", "2021-12-31"),
        ("2022-01-01", "2023-12-31"),
        ("2024-01-01", "2026-09-30")]

TIER_MAP = {
    "structure": "verified",
    "emerging": "observing",
    "decaying": "failed",
    "flicker": "failed",
}


def strip_beta(X: np.ndarray) -> np.ndarray:
    """等权市场代理 + 截距 OLS（与 chain_three_class_qtest.py:strip_beta 逐字同口径）。"""
    mkt = X.mean(axis=1)
    A = np.column_stack([np.ones(len(mkt)), mkt])
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        return X - A @ np.linalg.lstsq(A, X, rcond=None)[0]


def assert_locked() -> dict:
    """⛔ 与 measure_chain_resonance.py:assert_prior_locked 同口径，名单未锁/被改即退出。"""
    if not Path(PRIOR).exists():
        raise SystemExit(f"❌ 名单不存在：{PRIOR}")
    if not Path(LOCK).exists():
        raise SystemExit(f"❌ 锁定文件不存在：{LOCK}")
    rec = json.loads(Path(LOCK).read_text(encoding="utf-8"))
    digest = hashlib.sha256(Path(PRIOR).read_bytes()).hexdigest()
    if rec.get("sha256") != digest:
        raise SystemExit(
            "❌ 名单自锁定后被修改（哈希不符）⇒ 请新建 v2 并重新锁定，不得原地改后再跑")
    print(f"✅ 名单已锁定：{rec['n_chains']} 链 / {rec['n_members']} 成员 / "
          f"sha256 {rec['sha256'][:12]}…（来源 {rec['source']}；post_hoc={rec.get('post_hoc')}）")
    return rec


def main() -> None:
    lock = assert_locked()

    wr = pd.read_parquet(WR_PATH)
    R = (wr.pivot(index="date", columns="ind_code", values="weighted_pct")
           / 100.0).sort_index()
    codes = list(R.columns)
    assert len(codes) == 131, f"行业数 {len(codes)} != 131"
    assert R.notna().all().all(), "面板含 NaN（与 chain_three_class_qtest 锁定 spec ⑤ 不符）"
    print(f"· 面板 {R.shape[0]} 日 × {len(codes)} 行业")

    chains = M.load_prior_chains(PRIOR, codes)
    print(f"· 链 {len(chains)} 条（面板内可用）")

    # 三段各自 in-sample 剥 beta → 残差相关矩阵（与 chain_three_class_qtest.py:strip_beta 同口径）
    seg_corr, seg_ns = [], []
    for a, b in SEGS:
        m = (R.index >= a) & (R.index <= b)
        seg_corr.append(np.corrcoef(strip_beta(R.values[m]).T))
        seg_ns.append(int(m.sum()))
    print(f"· 段交易日 = {seg_ns}（锚点 486/484/666）")

    l1_of = G.load_l1_of(codes)
    pools = G.l1_pools(l1_of, codes)
    pos = {c: i for i, c in enumerate(codes)}

    # 缓存分层零分布：同段 + 同 L1 构成 ⇒ 同一零分布（同 seed ⇒ 同结果），无需重复抽样
    null_cache: dict = {}

    def get_null(seg_idx: int, comp: dict) -> dict:
        key = (seg_idx, tuple(sorted(comp.items())))
        if key not in null_cache:
            null_cache[key] = G.stratified_null(
                seg_corr[seg_idx], pools, comp, n_iter=C.N_ITER)
        return null_cache[key]

    rows = []
    for cid in sorted(chains):
        members = chains[cid]
        idx = [pos[m] for m in members if m in pos]
        if len(idx) < 2:
            continue
        rs_raw = [float(G.group_statistic(seg_corr[k], idx)) for k in range(3)]
        # 链级统计量是均值，理论上 |r|<1；但成员=2 且单对相关极高时会接近 ±1。
        # classify_three_segment 内部 cochran_q_homogeneity 校验 |r|<1，故先 clip 到安全范围。
        rs = [float(np.clip(r, -0.999, 0.999)) for r in rs_raw]
        cls = classify_three_segment(rs, seg_ns)
        comp = G.composition(idx, l1_of, codes)
        zs = []
        for k in range(3):
            null = get_null(k, comp)
            zs.append(G.zscore(rs_raw[k], null))
        rows.append({
            "chain_id": cid, "n_members": len(idx),
            "l1_composition": ",".join(f"{k}={v}" for k, v in sorted(comp.items())),
            "r_s1": round(rs[0], 4), "r_s2": round(rs[1], 4), "r_s3": round(rs[2], 4),
            "z_s1": round(zs[0], 3), "z_s2": round(zs[1], 3), "z_s3": round(zs[2], 3),
            "Q": round(cls["Q"], 3), "p_hom": round(cls["p_survival"], 5),
            "klass": cls["class"],
            "tier": TIER_MAP.get(cls["class"], "?"),
        })

    df = pd.DataFrame(rows)
    n = len(df)

    # ───── 三档分级（主判据 = classify_three_segment）─────
    print("\n== 74 链否决权验证：三档分级 ==")
    tc = df["tier"].value_counts()
    n_s = int(tc.get("verified", 0))
    n_e = int(tc.get("observing", 0))
    n_f = int(tc.get("failed", 0))
    print(f"  已验证结构 (verified)  : 实测 {n_s:3d} 条"
          f"   vs 噪声期望 {0.05 * n:.0f} 条（超额 {n_s - 0.05 * n:+.0f}）")
    print(f"  观察中     (observing) : 实测 {n_e:3d} 条")
    print(f"  未通过     (failed)    : 实测 {n_f:3d} 条  （decaying + flicker）")
    print(f"  合计 {n} 条")

    # ───── classify 明细 ─────
    print("\n== classify_three_segment 明细 ==")
    for k in ["structure", "emerging", "decaying", "flicker"]:
        c = int((df["klass"] == k).sum())
        print(f"  {k:10s} {c:3d}")

    # ───── 成员数 × 档位（验证 Q1：成员=2 的链是否噪声更大）─────
    print("\n== 成员数 × 档位 ==")
    ct = pd.crosstab(df["n_members"], df["tier"], margins=True)
    print(ct.to_string())

    # ───── 落盘（含证据地位声明）─────
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"chain_veto_verification_{TODAY}.csv"
    header = (
        "# 74 链否决权验证（项目 A）\n"
        f"# 日期 {TODAY}；名单 sha256 {lock['sha256'][:12]}…；post_hoc={lock.get('post_hoc')}\n"
        "# 口径：三段 in-sample 剥市场 beta（与 chain_three_class_qtest.py 同口径）\n"
        "# ⚠️ 证据地位：本验证只在「对象清单」层面自洽，\n"
        "#    ⛔ 不构成「共振方法发现了多少真链」的命中率证据（名单 post_hoc=true）。\n"
        "# 主判据：classify_three_segment（Cochran Q 同质性 p>0.05 ⇒ structure）\n"
        "# z_s{k}：分层零分布 z 值（L1 构成匹配），跨链可比，仅辅助诊断。\n"
    )
    out.write_text(header + df.to_csv(index=False), encoding="utf-8")
    print(f"\n逐链结果 → {out}")


if __name__ == "__main__":
    main()