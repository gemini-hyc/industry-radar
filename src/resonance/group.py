"""群统计量与 **L1 分层零分布** —— [(review §11.3) + (§12.1 双输出)] 的唯一实现处。

🔴 2026-10-07 从 `research/chain_null_distribution.py` 移入本包
   （§12.4 已批准移出 research/：README 声明该目录"冻结、不维护"，新生产级脚本放进去
     等于一写就进冷宫，且归档 research/ 时会被一起雪藏）。

两个必须记住的实证
------------------
1. **群内平均相关随群规模稀释**（成员多 ⇒ 对数多 ⇒ 均值被更多弱相关对拉低）
   ⇒ **同一链跨时间可比，不同链之间不可直接比**。
   修法（§12.1）：日报同时给两个数 —— **标量**（链内比较）+ **该规模的 L1 分层零分布 z 值**（跨链比较）。
2. **零分布必须按 L1 分层抽取**：同 L1 的残差共振显著高于跨 L1（K=1 存活 74% vs 27%）。
   若从 131 行业全局随机抽同规模群，则主要成员落在一级内部的候选链会"赢在 L1 背景"。
   实测（4 行业群）：全同 L1 分层 p95 **0.361** vs 全局 0.145（全局过松 2.5 倍）；
   1+1+1+1 分层 p95 0.078 vs 全局 0.145（全局过严）⇒ **全局零分布两个方向都错**。
"""
from __future__ import annotations

from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from src.paths import INDUSTRY_DIR
from src.resonance import config as C

MEM_RAW = INDUSTRY_DIR / "index_member_all_raw.parquet"
T2L_PATH = INDUSTRY_DIR / "ind_code_to_sw2021.tsv"


# ─────────────────────────── 群统计量 ───────────────────────────
def group_statistic(Cm: np.ndarray, idx: Sequence[int]) -> float:
    """群级统计量 = 群内所有对的**带符号**残差相关均值（review 修订 5：链 = 同向传导）。

    ⚠️ 这就是 (链, 日) 那一个标量。若改用群级 PC1 方差占比，零分布必须严格
    size-matched（§11.7.1：占比对群规模敏感），且与本函数不再同尺度。
    """
    k = len(idx)
    if k < 2:
        return float("nan")
    vals = [Cm[i, j] for a, i in enumerate(idx) for j in idx[a + 1:]]
    return float(np.mean(vals))


# ─────────────────────────── L1 映射 ───────────────────────────
def load_l1_of(codes: Sequence[str]) -> Dict[str, str]:
    """ind_code → 申万一级名（131 个行业零缺失）"""
    mem = pd.read_parquet(MEM_RAW)
    mem = mem[mem["is_new"] == "Y"]
    l2map = mem.drop_duplicates("index_code").set_index("index_code")["l1_name"]
    t2l = pd.read_csv(T2L_PATH, sep="\t").set_index("ind_code")["sw_l2_code"]
    return {c: l2map.get(t2l.get(c), "?") for c in codes}


def l1_pools(l1_of: Dict[str, str], codes: Sequence[str]) -> Dict[str, List[int]]:
    """L1 名 → 该一级内所有行业的**下标**列表"""
    pools: Dict[str, List[int]] = {}
    for i, c in enumerate(codes):
        pools.setdefault(l1_of.get(c, "?"), []).append(i)
    return pools


def composition(idx: Sequence[int], l1_of: Dict[str, str],
                codes: Sequence[str]) -> Dict[str, int]:
    """候选链在 L1 上的构成：{L1 名: 成员数}"""
    comp: Dict[str, int] = {}
    for i in idx:
        l1 = l1_of.get(codes[i], "?")
        comp[l1] = comp.get(l1, 0) + 1
    return comp


# ─────────────────────────── 零分布 ───────────────────────────
def _sample_null(pools: Dict[str, List[int]], comp: Dict[str, int],
                 rng: np.random.Generator) -> List[int]:
    """按 L1 构成分层抽一个群：{A:2,B:3,C:1} ⇒ 从 A 池抽 2、B 池抽 3、C 池抽 1。"""
    idx: List[int] = []
    for l1, k in comp.items():
        pool = pools.get(l1) or []
        if len(pool) < k:
            raise ValueError(f"L1 {l1!r} 池仅 {len(pool)} 个行业，抽不出 {k}")
        idx += list(rng.choice(pool, size=k, replace=False))
    return idx


def stratified_null(Cm: np.ndarray, pools: Dict[str, List[int]],
                    comp: Dict[str, int], n_iter: int = C.N_ITER,
                    seed: int = C.NULL_SEED) -> Dict:
    """**主判据**。返回 {mean, std, p95, samples}"""
    rng = np.random.default_rng(seed)
    stats = np.array([group_statistic(Cm, _sample_null(pools, comp, rng))
                      for _ in range(n_iter)])
    return {"mean": float(stats.mean()), "std": float(stats.std(ddof=1)),
            "p95": float(np.percentile(stats, C.NULL_LEVEL * 100)),
            "samples": stats}


def global_null(Cm: np.ndarray, all_idx: Sequence[int], size: int,
                n_iter: int = C.N_ITER, seed: int = C.NULL_SEED) -> Dict:
    """全局零分布（**仅作对照**）—— §11.3 判定为错误做法，不要用它做判决。"""
    rng = np.random.default_rng(seed)
    stats = np.array([group_statistic(Cm, list(rng.choice(list(all_idx), size=size,
                                                          replace=False)))
                      for _ in range(n_iter)])
    return {"mean": float(stats.mean()), "std": float(stats.std(ddof=1)),
            "p95": float(np.percentile(stats, C.NULL_LEVEL * 100)),
            "samples": stats}


def zscore(stat: float, null: Dict) -> float:
    """标量 → 相对该零分布的 z 值（**跨链比较用这个，不要直接比标量**）"""
    sd = null.get("std")
    if not sd or not np.isfinite(sd):
        return float("nan")
    return float((stat - null["mean"]) / sd)


def exceedance(stat: float, null: Dict) -> float:
    """观测值超过多少比例的零分布样本（1 − 分位）"""
    s = null.get("samples")
    if s is None or len(s) == 0:
        return float("nan")
    return float((s > stat).mean())


# ─────────────────────────── 演示（可复跑的证据）───────────────────────────
def demo_stratified_vs_global(seg: tuple = ("2024-01-01", "2026-09-30"),
                              n_iter: int = C.N_ITER) -> pd.DataFrame:
    """复现 §11.3 的那张表：同一物理规模（4 个行业）不同 L1 构成下，分层 vs 全局零分布。

    ⚠️ 这是**方法验证**（零分布本身长什么样），不是任何真实链的判决。
    跑法：`python -m src.resonance.group`
    """
    from src.resonance.panel import load_returns
    R = load_returns()
    sl = R.loc[(R.index >= seg[0]) & (R.index <= seg[1])]
    X = sl.to_numpy(dtype=float)
    A = np.column_stack([np.ones(len(X)), X.mean(axis=1)])
    E = X - A @ np.linalg.lstsq(A, X, rcond=None)[0]
    Cm = np.corrcoef(E.T)
    codes = list(R.columns)
    l1_of = load_l1_of(codes)
    pools = l1_pools(l1_of, codes)
    names = sorted(pools, key=lambda k: -len(pools[k]))
    specs = [("全同 L1（4 个同一级）", {names[0]: 4}),
             ("2+2（两个一级）", {names[0]: 2, names[1]: 2}),
             ("1+1+1+1（四个一级）", {names[0]: 1, names[1]: 1, names[2]: 1, names[3]: 1})]
    rows = []
    for name, comp in specs:
        s = stratified_null(Cm, pools, comp, n_iter=n_iter)
        g = global_null(Cm, list(range(len(codes))), size=sum(comp.values()), n_iter=n_iter)
        rows.append({"构成模板": name, "分层均值": round(s["mean"], 3),
                     "分层p95": round(s["p95"], 3), "全局均值": round(g["mean"], 3),
                     "全局p95": round(g["p95"], 3), "p95差": round(s["p95"] - g["p95"], 3)})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    print("== 分层 vs 全局零分布（统计量=群内带符号残差 r 均值，示例段 2024-01-01~2026-09-30）==")
    print(demo_stratified_vs_global().to_string(index=False))
    print("\n解读：全同 L1 构成下分层 p95 显著高于全局 p95 ⇒ 用全局零分布会让一级内部为主的"
          "\n候选链轻松『过线』；反过来对真跨 L1 的链又过严 ⇒ 全局零分布两个方向都错。")
