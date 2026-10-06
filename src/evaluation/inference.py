"""统计推断原语 —— **全项目唯一的口径实现**（2026-10-06 建立）。

为什么有这个模块
----------------
本项目 24 小时内在「前瞻窗口」上翻过三次车，每次结论都变（见
`docs/changes-2026-10-05-breadth-window-bug.md`）。根因不是窗口算错，而是
**同一个统计量在多处各写一份**，改一处忘另一处 ⇒ 口径漂移。

建立本模块时全仓已有**两份**聚类/非重叠 t：

| 原位置| 签名 | 用途 |
|:--|:--|:--|
| `src/signals/evaluate.py::cluster_t` / `nonoverlap_t` | `(values, dates)` | 台账评估报告 |
| `src/research/breadth_eval.py::nonoverlap_t` / `nonoverlap_t_sorted` | `(Series[, horizon])` | 面板研究 |

⚠️ **两份的输入契约不同，不是重复实现**：
- 本模块 `nonoverlap_t(values, dates, horizon)`：**按日期去重后**每 h 个交易日取 1
  —— 事件流可能有多个同日观测（同一信号日多个行业），必须先去重再抽稀；
- 本模块 `nonoverlap_t_on_index(ic, horizon)`：作用在**已按索引排序**的 IC 序列上，
  直接 `iloc[::h]`；配套 `nonoverlap_t_sorted` 先排序再抽 —— ⛔ 未排序就抽稀
  会变成"按行业字母序取样"，**不报错但结论完全错**（2026-10-05 实测 t 从 −0.05 变−1.49）。

⇒ 保留两个函数，但**实现只有这一份**；`evaluate.py` 与 `breadth_eval.py` 均改为
从此处导入，⛔ 不得再各自实现。

零依赖：只用 `numpy` / `pandas`（本机解释器无 scipy，勿引入）。
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

__all__ = [
    "cluster_t",
    "nonoverlap_t",
    "nonoverlap_t_on_index",
    "nonoverlap_t_sorted",
    "cluster_se",
    "cs_rank_ic",
    "ts_quantile",
    "tail_concentration",
    "cochran_q_homogeneity",
    "chi2_survival_even_df",
    "classify_three_segment",
]


# ══════════════════════════════════════════════════════════════════
# ① 聚类 t（事件流口径：输入带日期）
# ══════════════════════════════════════════════════════════════════

def cluster_t(values: pd.Series, dates: pd.Series) -> Tuple[float, int]:
    """按交易日聚类的 t。返回 `(t, 独立日数)`。

    **为什么必须聚类**：同一信号日的多个行业观测共享同一市场冲击，
    当成独立样本会把 t 放大约 √(每日样本数)。实测冷区 h=20 朴素 t=+3.12
    → 聚类后 −0.48（2026-10-05）。
    """
    d = pd.DataFrame({"v": pd.to_numeric(values, errors="coerce"),
                      "d": pd.to_datetime(dates)}).dropna()
    if len(d) < 2:
        return float("nan"), 0
    g = d.groupby("d")["v"].mean()
    if len(g) < 2 or not g.std(ddof=1):
        return float("nan"), len(g)
    return float(g.mean() / (g.std(ddof=1) / math.sqrt(len(g)))), len(g)


def nonoverlap_t(values: pd.Series, dates: pd.Series,
                 horizon: int) -> Tuple[float, int]:
    """非重叠抽样的 t（**按日期去重后**每 horizon 个交易日取 1）。返回 `(t, n)`。

    ⚠️ 与 `nonoverlap_t_on_index` 的区别：这里**先按日期分组去重**。
    事件流里同一天可能有几十个行业观测，若不先去重就会把它们当独立样本。

    ⚠️ `horizon` 超���序列长度时必须返回 NaN —— 不能算出一个小样本的假t。
    """
    d = pd.DataFrame({"v": pd.to_numeric(values, errors="coerce"),
                      "d": pd.to_datetime(dates)}).dropna().sort_values("d")
    if len(d) == 0:
        return float("nan"), 0
    uniq = list(dict.fromkeys(d["d"].tolist()))          # 有序去重
    sub = d[d["d"].isin(set(uniq[::max(1, int(horizon))]))]
    if len(sub) < 2 or not sub["v"].std(ddof=1):
        return float("nan"), len(sub)
    return float(sub["v"].mean() /
                 (sub["v"].std(ddof=1) / math.sqrt(len(sub)))), len(sub)


# ══════════════════════════════════════════════════════════════════
# ② 序列口径：作用在已排序的 IC / 指标序列上
# ══════════════════════════════════════════════════════════════════

def nonoverlap_t_on_index(ic: pd.Series, horizon: int) -> Tuple[float, int]:
    """非重叠 t：每 horizon 个**位置**取 1 个观测。返回 `(t, n)`。

    🔴 **调用前必须确认序列已按索引排序**，否则抽到的是"行序"而非"时间"，
    而面板数据的事件表行序常是 `(con_code, exp_start_date)` ⇒ 变成按行业字母序抽样，
    **不报错、不告警，结论完全错**（实测同一子集 t 从 −0.05 变 −1.49）。
    不确定就用 `nonoverlap_t_sorted`。

    为什么必须用它：IC 序列自身有强自相关（宽度半衰期≈5 日），
    把 N 个重叠样本当独立会把 t 放大约 √h。h=20 实测朴素 t=+21.0 vs 非重叠 t=+3.94。

    ⚠️ `horizon` 超过序列长度时返回 NaN —— 不能算出假 t。
    """
    sub = ic.iloc[::max(1, int(horizon))]
    n = len(sub)
    if n < 2 or not sub.std(ddof=1):
        return float("nan"), n
    return float(sub.mean() / (sub.std(ddof=1) / math.sqrt(n))), n


def nonoverlap_t_sorted(x: pd.Series, horizon: int) -> Tuple[float, int]:
    """⛔ **安全版**：先按索引排序再抽稀。事件研究里请一律用这个。

    与 `nonoverlap_t_on_index` 的差别就是这一次排序 —— 但它是本项目
    踩过的最贵的坑之一（2026-10-05，结论差30 倍且零告警）。
    """
    return nonoverlap_t_on_index(pd.Series(x).sort_index(), horizon)


# ══════════════════════════════════════════════════════════════════
# ③ 聚类稳健回归（`evaluate.py` 完全没有的能力，交互项检验必需）
# ══════════════════════════════════════════════════════════════════

def cluster_se(df: pd.DataFrame, ycol: str, xcols: Sequence[str],
               cluster: str = "date") -> dict:
    """聚类稳健 OLS（按交易日聚类）。返回 {beta, se, t, n_clusters, n_obs}。

    🔴 2026-10-06 从 src/research/breadth_eval.py 逐字搬来（唯一实现处）。
    ⚠️ **改动前必须先跑 tests/test_breadth_signal.py 的 cluster_se 三项断言**：
    本实现对 y 与X 做了 z-score 标准化，小样本修正是 `G/(G-k)`（不是教科书的
    `G/(G-1)·(N-1)/(N-k)`），且返回字典**不含 const** —— 换任一条都会让
    实测 se 偏移 ~0.6% 而不报错。

    `cluster` 列可以**是列，也可以是索引的某一层** —— `build_panel` 产出的宽表
    经 `.stack()` 后日期在 MultiIndex 第 0 层（不在列里），
    若只按列查找会直接 KeyError（2026-10-05 实测踩到）。

    ⚠️ meat 必须是 `Σ_g (X_g'e_g)(X_g'e_g)'` —— 聚合**残差**再外积。
    ⛔ 不可写成 `(Σ_i e_i)²`：横截面回归已控制截距、残差和≈0 ⇒ 肉矩阵塌成 0
    ⇒ se→0 ⇒ t 虚高到 1e13（2026-10-05 实测踩过）。

    ⚠️ 共线控制变量会让系数符号翻转成伪影（实测 corr(vol_past, |ret_past|)=0.907
    时 beta 翻成 −0.234）。**一次只加一个控制变量**，或先查 VIF。

    ⚠️ 聚类 se **不保证**大于 i.i.d. 朴素 se：实测比值在 0.83~1.23 间摆动
    （取决于簇级共同冲击在 z-score 后还剩多少）。**不要**把"聚类 se 更大"
    当成实现正确性的判据。
    """
    def _grab(key: str) -> np.ndarray:
        if key in df.columns:
            return df[key].values
        if isinstance(df.index, pd.MultiIndex) and key in (df.index.names or []):
            return df.index.get_level_values(key).to_numpy()
        raise KeyError(f"cluster_se: 找不到聚类键 {key!r}（列={list(df.columns)}, "
                       f"索引层名={df.index.names}）")

    z = lambda s: (s - s.mean()) / s.std()          # noqa: E731
    X = np.column_stack([np.ones(len(df))] + [z(df[c]).values for c in xcols])
    y = z(df[ycol]).values
    # ⚠️ 本机 numpy（1.x）的 LAPACK 封装 `_core_matmul` 对**任何**线性代数路径
    #    （lstsq / solve / pinv 全部）都会抛 `divide by zero / overflow / invalid`
    #    RuntimeWarning，**即使输入全有限、数值完全正确**（实测两种算法
    #    系数差 2e-17）。这是环境级噪音，不改算法，只在本格窄范围屏蔽。
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        grp = _grab(cluster)
        uniq = np.unique(grp)
        k = X.shape[1]
        meat = np.zeros((k, k))
        for g in uniq:
            m = grp == g
            u = X[m].T @ resid[m]
            meat += np.outer(u, u)
        G = len(uniq)
        bread = np.linalg.pinv(X.T @ X)
        V = bread @ meat @ bread * (G / max(G - k, 1))
        se = np.sqrt(np.diag(V))
    return {
        "beta": {c: float(beta[i]) for i, c in enumerate(xcols, start=1)},
        "se": {c: float(se[i]) for i, c in enumerate(xcols, start=1)},
        "t": {c: float(beta[i] / se[i]) if se[i] > 0 else float("nan")
              for i, c in enumerate(xcols, start=1)},
        "n_clusters": G, "n_obs": len(df),
    }




# ════════════════════════════════════════════════════════════════
# ④ 横截面 IC 与时序分位
# ══════════════════════════════════════════════════════════════════


def cs_rank_ic(signal: pd.DataFrame, fwd: pd.DataFrame,
               min_industries: int = 20) -> pd.Series:
    """逐日横截面 RankIC（信号排序 vs 未来值排序），返回按日索引的 IC 序列。

    用 rank + Pearson 代替 `scipy.stats.spearmanr` —— 本机无 scipy。
    不用 pandas 的 `.corr(method='spearman')` 是为了统一"某日样本不足则跳过"的语义。

    ⚠️ 输入是**宽表面板**（index=交易日、columns=行业），不是 Series。
    `min_industries` 不足的交易日返回 NaN 并被丢弃 ——
    样本太少的截面 IC 噪声极大，混进来会污染整条 IC 序列的自相关结构。
    """
    df = pd.concat([signal.stack(), fwd.stack()], axis=1, keys=["s", "f"]).dropna()
    df.columns = ["s", "f"]
    df = df.reset_index()

    def _ic(g: pd.DataFrame) -> float:
        if len(g) < min_industries:
            return np.nan
        a, b = g["s"].rank(), g["f"].rank()
        if a.std() == 0 or b.std() == 0:
            return np.nan
        return float(np.corrcoef(a, b)[0, 1])

    return df.groupby("date").apply(_ic, include_groups=False).dropna()


def ts_quantile(frame: pd.DataFrame, window: int = 250,
                min_periods: int = 120) -> pd.DataFrame:
    """每个行业**自己的**历史分位（严格只用 t-1 及之前）。逐列独立计算。

    ⚠️ 必须先 `shift(1)`：不 shift 就把当日读数算进分位 = **前视**。
    实测前视版月度 IC=+0.054、正确版 −0.008 —— 符号都被带反了。
    测试 `test_ts_quantile_excludes_current_day` 锁住这一点。
    """
    return (frame.shift(1)
                 .rolling(window, min_periods=min_periods)
                 .apply(lambda x: x.rank(pct=True).iloc[-1], raw=False))


# ══════════════════════════════════════════════════════════════════
# ⑤ 分布诊断（均值假象识别）
# ══════════════════════════════════════════════════════════════════

def tail_concentration(x: pd.Series, top_frac: float = 0.01) -> Dict:
    """尾部集中度：正均值是不是全靠极少数右尾事件撑起来的。

    **为什么必须做**：冷区 h=20 均值 **+0.34pp** 看着是正 alpha，
    但中位数 **−0.13pp**、去 top5% 均值 **−0.42pp**，
    且 **前 1%（22/2200 个）贡献了 66% 的总收益**。
    ⇒ 只报均值会把"典型事件期望收益是负的"说成"平均多赚 0.34 个点"。

    🔴 `top_contrib_gross`（正数口径）比 `top_contrib_net`（净和口径）稳健：
    净和口径的分母在对称分布下趋近 0，会发散成 4.14（应对照 ≈0.06）。
    所以主判据用前两个**稳定量**（均值/中位数关系 + `net_over_abs`）。

    返回 `{n, mean, median, trimmed_mean, neg_ratio, net_over_abs, top_k,
    top_contrib_gross, top_contrib_net}`。
    """
    s = pd.Series(x).dropna().sort_index()
    n = len(s)
    if n < 10:
        return {}
    k = max(1, int(n * top_frac))
    top_sum = float(s.nlargest(k).sum())
    total = float(s.sum())
    abs_sum = float(s.abs().sum())
    pos_sum = float(s[s > 0].sum())
    thr = s.quantile(1 - top_frac)
    net_over_abs = abs(total) / abs_sum if abs_sum > 0 else float("nan")
    return {
        "n": n,
        "mean": float(s.mean()),
        "median": float(s.median()),
        "trimmed_mean": float(s[s <= thr].mean()),
        "neg_ratio": float((s < 0).mean()),
        "net_over_abs": net_over_abs,
        "top_k": k,
        "top_contrib_gross": top_sum / pos_sum if pos_sum > 0 else float("nan"),
        "top_contrib_net": (top_sum / total
                            if (abs(total) > 0 and net_over_abs > 0.02)
                            else float("nan")),
    }


# ══════════════════════════════════════════════════════════════════
# ⑥ 段间同质性检验与三分类（行业链验证 · 2026-10-06，review §11.2 检验版）
# ══════════════════════════════════════════════════════════════════

def chi2_survival_even_df(q: float, df: int) -> float:
    """偶数自由度卡方生存函数 `P(X > q)` 的**精确闭式**（无需 scipy）。

    对偶数 df：`P = exp(−q/2) · Σ_{k=0}^{df/2−1} (q/2)^k / k!`。
    本项目三段稳定性检验 df=2 ⇒ `P = exp(−q/2)`。
    奇数 df 需要正态 CDF（erf），本机无 scipy ⇒ 显式拒绝，**不得**用正态近似糊弄
    （近似在 q 小、df 大时偏差可达两位数百分点）。
    """
    if df <= 0 or df % 2 != 0:
        raise ValueError(f"chi2_survival_even_df: 仅支持偶数 df，收到 df={df}"
                         "（本项目三段检验 df=2；奇数 df 需 scipy，本机没有）")
    if q < 0:
        raise ValueError(f"chi2_survival_even_df: q 必须 ≥ 0，收到 {q}")
    half = q / 2.0
    term, s = 1.0, 1.0
    for k in range(1, df // 2):
        term *= half / k
        s += term
    return math.exp(-half) * s


def cochran_q_homogeneity(rs: Sequence[float], ns: Sequence[int]) -> Dict:
    """Cochran Q 段间同质性检验（Fisher-z 变换）。返回 `{Q, df, p_survival, z, w, zbar}`。

    用途：判断 k 段（如 2020-21 / 2022-23 / 2024-26）的残差相关 r 是否来自
    同一水平 —— `p_survival > 0.05` ⇒ 同质（结构），否则段间有真实漂移。

    口径（`docs/plan/chain-clustering-review.md` §11.2，勿改动）：
    - `z = arctanh(clip(r, ±0.999))`（Fisher-z）；
    - `w = n − 3`（各段权重，n = 段内交易日数）；
    - `zbar = Σwz / Σw`；`Q = Σw(z − zbar)²`；`df = k − 1`（三段 ⇒ 2）。
    """
    r = np.asarray(rs, dtype=float)
    n = np.asarray(ns, dtype=float)
    if r.shape != n.shape or r.ndim != 1 or len(r) < 2:
        raise ValueError("cochran_q_homogeneity: rs/ns 须为同长的一维序列且段数 ≥ 2")
    if np.any(n <= 3):
        raise ValueError(f"cochran_q_homogeneity: 段样本数必须 > 3（收到 {ns}）")
    if np.any(np.abs(r) >= 1.0):
        raise ValueError(f"cochran_q_homogeneity: |r| 必须 < 1（收到 {rs}）")
    z = np.arctanh(r)                       # 已确保 |r|<1，无需再 clip
    w = n - 3.0
    zbar = float((z * w).sum() / w.sum())
    q = float((w * (z - zbar) ** 2).sum())
    df = len(r) - 1
    return {
        "Q": q, "df": df,
        "p_survival": chi2_survival_even_df(q, df) if df % 2 == 0 else float("nan"),
        "z": z.tolist(), "w": w.tolist(), "zbar": zbar,
    }


def classify_three_segment(rs: Sequence[float], ns: Sequence[int],
                           alpha: float = 0.05) -> Dict:
    """三段相关 → 检验版三分类（**替代旧极差 ≤0.13 拍系数版，旧计数一律作废**）。

    判据（review §11.2，锚在零分布上不锚在门槛上）：
    - **结构**：Q 同质性检验 `p > alpha`（同水平 ⇒ 报告为"实测 vs 噪声期望"）；
    - **新生**：不同质 **且** z 严格单调递增 **且** 每级增量 > 自身
      `SE(Δz)=√(1/wᵢ+1/wⱼ)`。⚠️ 必须另加严格单调：3 点等距的线性对比 c=(−1,0,1)
      只看首尾（中间权重为 0），**不是单调性检验**；
    - **衰减**：不同质 且 z **严格单调递减（不设 SE 门槛）**。
      ⚠️ 规格钉死记录（2026-10-06）：给衰减也加 SE 门槛会得 7 对（差 5 对，
      全是"缓降未超噪声"如 [0.59,0.55,0.49]）；文档口径 = 仅严格递减 ⇒ 12 对，
      与 review §11.2 表逐格一致。衰减处置 = 拒，判据松紧不影响下游，故从宽；
      **新生须转正路径，判据从严** —— 这是不对称的理由；
    - **闪烁**：其余（非单调、无一致水平）。

    返回 `{class, Q, df, p_survival, homogeneous, steps}`，
    `steps = [(Δz₁₂, SE₁₂, 增量>SE), (Δz₂₃, SE₂₃, 增量>SE)]`。
    """
    info = cochran_q_homogeneity(rs, ns)
    z = np.asarray(info["z"])
    w = np.asarray(info["w"])
    homogeneous = info["p_survival"] > alpha
    steps = []
    for i in range(len(z) - 1):
        dz = float(z[i + 1] - z[i])
        se = float(math.sqrt(1.0 / w[i] + 1.0 / w[i + 1]))
        steps.append({"dz": dz, "se": se, "exceeds_se": abs(dz) > se})
    if homogeneous:
        cls = "structure"
    elif all(s["dz"] > s["se"] for s in steps):        # 递增且每级超噪声（从严：有转正路径）
        cls = "emerging"
    elif all(s["dz"] < 0 for s in steps):              # 严格递减即衰减（从宽：处置=拒）
        cls = "decaying"
    else:
        cls = "flicker"
    return {"class": cls, "Q": info["Q"], "df": info["df"],
            "p_survival": info["p_survival"], "homogeneous": homogeneous,
            "steps": steps}
