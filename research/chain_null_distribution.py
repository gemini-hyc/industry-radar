"""chain_null_distribution.py —— §11.3 群级零分布（按 L1 分层抽取）的实现与验证（只读）。

对应 `docs/plan/chain-clustering-review.md` §11.3：
零分布若"从 131 行业全局随机抽同规模群"，则主要成员落在一级内部的候选链会
**赢在 L1 背景**而不是"赢在链"（实测同 L1 残差共振显著高于跨 L1：K=1 存活 74% vs 27%）。
⇒ 正确做法：**按候选链的 L1 构成分层抽取**（链在 L1 上有 {A:2,B:3,C:1} ⇒ 从 A 抽 2、B 抽 3、C 抽 1）。

本文件提供：
- `group_statistic()`      群级统计量（带符号残差相关的群内均值，review 修订 5 口径）；
- `stratified_null()`      分层零分布（**主判据用这个**）；
- `global_null()`          全局零分布（仅作对照，用于量化分层修掉了多少假阳性空间）；
- `main()`                 演示：同一 L1 构成下两套零分布的差异（**纯方法验证，不是链判决**）。

⛔ **链判决必须等先验名单锁定（§11.4）之后**才能跑；本演示只用合成构成模板，
   不产出任何"某某链显著"的结论。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WR = "/Users/hyc/quant-data/industry/industry_weighted_returns.parquet"
MEM = "/Users/hyc/quant-data/industry/index_member_all_raw.parquet"
T2L = "/Users/hyc/quant-data/industry/ind_code_to_sw2021.tsv"
SEG_START, SEG_END = "2024-01-01", "2026-09-30"   # 示例段（每段各自剥 beta 的同口径）


def strip_beta(X: np.ndarray) -> np.ndarray:
    mkt = X.mean(axis=1)
    A = np.column_stack([np.ones(len(mkt)), mkt])
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        return X - A @ np.linalg.lstsq(A, X, rcond=None)[0]


def load_residual_corr() -> tuple[list[str], np.ndarray, dict[str, str]]:
    """返回 (codes, 段内残差相关阵, ind_code→L1 名映射)。"""
    wr = pd.read_parquet(WR)
    R = (wr.pivot(index="date", columns="ind_code", values="weighted_pct")
           / 100.0).sort_index()
    m = ((R.index >= SEG_START) & (R.index <= SEG_END))
    C = np.corrcoef(strip_beta(R.values[m]).T)
    codes = R.columns.tolist()
    mem = pd.read_parquet(MEM)
    mem = mem[mem["is_new"] == "Y"]
    l2map = mem.drop_duplicates("index_code").set_index("index_code")["l1_name"]
    t2l = pd.read_csv(T2L, sep="\t").set_index("ind_code")["sw_l2_code"]
    l1_of = {c: l2map.get(t2l.get(c), "?") for c in codes}
    return codes, C, l1_of


def group_statistic(C: np.ndarray, idx: list[int]) -> float:
    """群级统计量 = 群内所有对的**带符号**残差相关均值（review 修订 5：链=同向传导）。

    ⚠️ 若改用群级 PC1 方差占比，零分布必须严格 size-matched（§11.7.1：占比对群规模敏感）。
    """
    k = len(idx)
    if k < 2:
        return float("nan")
    vals = [C[i, j] for a, i in enumerate(idx) for j in idx[a + 1:]]
    return float(np.mean(vals))


def _sample_null(l1_pool: dict[str, list[int]], comp: dict[str, int],
                 rng: np.random.Generator) -> list[int]:
    """按 L1 构成分层抽一个群：{A:2,B:3,C:1} ⇒ 从 A 池抽 2、B 池抽 3、C 池抽 1。"""
    idx: list[int] = []
    for l1, k in comp.items():
        pool = l1_pool[l1]
        if len(pool) < k:
            raise ValueError(f"L1 {l1!r} 池仅 {len(pool)} 个行业，抽不出 {k}")
        idx += list(rng.choice(pool, size=k, replace=False))
    return idx


def stratified_null(C: np.ndarray, l1_pool: dict[str, list[int]],
                    comp: dict[str, int], n_iter: int = 2000,
                    seed: int = 20261006) -> dict:
    """分层零分布。返回 {mean, p95, samples}。**群级验证的主判据用这个。**"""
    rng = np.random.default_rng(seed)
    stats = np.array([group_statistic(C, _sample_null(l1_pool, comp, rng))
                      for _ in range(n_iter)])
    return {"mean": float(stats.mean()), "p95": float(np.percentile(stats, 95)),
            "samples": stats}


def global_null(C: np.ndarray, all_idx: list[int], size: int,
                n_iter: int = 2000, seed: int = 20261006) -> dict:
    """全局零分布（对照用）：从 131 行业全局随机抽同规模群 —— §11.3 判定为**错误做法**。"""
    rng = np.random.default_rng(seed)
    stats = np.array([group_statistic(C, list(rng.choice(all_idx, size=size,
                                                          replace=False)))
                      for _ in range(n_iter)])
    return {"mean": float(stats.mean()), "p95": float(np.percentile(stats, 95)),
            "samples": stats}


def main() -> None:
    codes, C, l1_of = load_residual_corr()
    l1_pool: dict[str, list[int]] = {}
    for i, c in enumerate(codes):
        l1_pool.setdefault(l1_of[c], []).append(i)
    all_idx = list(range(len(codes)))
    pool_sizes = {k: len(v) for k, v in
                  sorted(l1_pool.items(), key=lambda x: -len(x[1]))[:5]}
    print(f"面板 {len(codes)} 行业，L1 池大小（前 5）：{pool_sizes} ...")

    # 演示模板：同一物理规模（4 行业）但 L1 构成不同 ⇒ 分层 vs 全局零分布的差异。
    # ⚠️ 这是**方法验证**（零分布本身长什么样），不是任何真实链的判决。
    demos = {
        "全同 L1（4 个同一级）": None,       # 取最大的 L1 池
        "2+2（两个一级）": None,
        "1+1+1+1（四个一级）": None,
    }
    big_l1 = max(l1_pool, key=lambda k: len(l1_pool[k]))
    l1_names = sorted(l1_pool, key=lambda k: -len(l1_pool[k]))
    demo_specs = [
        ("全同 L1（4 个同一级）", {big_l1: 4}),
        ("2+2（两个一级）", {l1_names[0]: 2, l1_names[1]: 2}),
        ("1+1+1+1（四个一级）", {l1_names[0]: 1, l1_names[1]: 1,
                                 l1_names[2]: 1, l1_names[3]: 1}),
    ]
    print(f"\n== 零分布对照（统计量=群内带符号残差 r 均值；示例段 {SEG_START}~{SEG_END}） ==")
    print(f"{'构成模板':22s} {'分层均值':>8s} {'分层p95':>8s} {'全局均值':>8s} {'全局p95':>8s} {'p95差':>8s}")
    for name, comp in demo_specs:
        s = stratified_null(C, l1_pool, comp)
        g = global_null(C, all_idx, size=sum(comp.values()))
        print(f"{name:22s} {s['mean']:8.3f} {s['p95']:8.3f} {g['mean']:8.3f} "
              f"{g['p95']:8.3f} {s['p95'] - g['p95']:+8.3f}")
    print("\n解读：全同 L1 构成下分层 p95 显著高于全局 p95 —— 若用全局零分布，"
          "\n一级内部为主的候选链会轻松『过线』（赢在 L1 背景，不是赢在链）。"
          "\n⇒ 这就是 §11.3 要求分层的量化理由。")
    print("\n⛔ 下一步（等先验名单锁定后才能做）：对真实候选链按本文件 stratified_null "
          "\n   计算 实际群统计量 vs 分层零分布 p95，产出链级判决。")


if __name__ == "__main__":
    main()
