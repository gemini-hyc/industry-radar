"""风格调整后状态量的**数据探针** —— review §11.7.3 + §12.5（含两处修正与金丝雀链）。

核心洞察（务必先读，它决定了探针的形状）
----------------------------------------
这个故障**不是"变垃圾"，而是"悄悄退化成原始值"**：因子面板一旦全缺/失效，
调整值 ≈ 原始值，**报表照常出数、看起来非常健康**。
这是 `BREADTH_ZERO` 的同型病 ⇒ **要探的是"退化"，不是"异常"**；
**失败要"响"不要"静"** —— 回落原始值**并且**要在报告正文标注
"未调整（因子面板不可用）"，**只写日志不算**。

§12.5 的两处修正（推翻了把"差额→0"当主探测器的初版）
------------------------------------------------------
1. **主探测器必须在输入端**："|原始−调整| < ε 的链占比 ≥80%"会**误报** ——
   风格本来就安静的交易日，差额天然接近 0。
   ⇒ 主探测 = 面板覆盖/新鲜度 + β 面板非退化 + **当日实际纳入因子数 == K**；
   差额探测器降为**长窗口佐证**（20 日滚动均差 → 0）。
2. **代数不变量的限度**：`corr(残差, 各因子) ≈ 0` 测的是**实现**而不是**数据**
   —— 某因子列全 NaN 时回归会**静默丢掉它**，而这个不变量**恰恰不会报警**（那列
   没法算相关）⇒ 必须配对"实际纳入因子数 == K"的断言才完整。
3. **金丝雀链（新增，最便宜且端到端）**：放一条**合成链**（成员 = 已知高风格暴露行业），
   断言其**调整值 ≈ 0**。若调整静默失效，金丝雀的调整值会回弹到很大的原始值
   ⇒ 一眼可见，且覆盖「因子面板 → 日报渲染」全链路。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from src.resonance import config as C
from src.resonance import group as G


@dataclass
class ProbeResult:
    issues: List[Dict] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否健康 —— **动态**判定：任何一条 critical 都立刻让它变 False。

        （写成属性而不是字段，是为了让"只调了子探针"的场景也得到正确结论 ——
        探针的语义是"有没有 critical"，不该依赖调用方记得回写 ok。）
        """
        return not any(i.get("level") == "critical" for i in self.issues)

    @property
    def degraded(self) -> bool:
        return not self.ok

    def banner(self) -> str:
        """日报正文必须带的标注（**失败要有声**：不得低于一个注释）"""
        if self.ok:
            return ""
        codes = "、".join(i["code"] for i in self.issues)
        return (f"> ⚠️ **本报告的共振强度为未调整值（因子面板不可用：{codes}）**\n"
                f"> ⇒ 该数值包含风格暴露成分，**不得用于归因/链假设**；"
                f"仅作「今日发生了什么」的状态描述。")


def _add(res: ProbeResult, code: str, detail: str, level: str = "critical") -> None:
    res.issues.append({"code": code, "detail": detail, "level": level})


# ─────────────────── ① 主探测器：输入端（面板 + β + 因子数）───────────────────
def probe_panel_input(F: pd.DataFrame, X: Optional[pd.DataFrame], res: ProbeResult,
                      target_date=None, panel_name: str = "style_factors") -> None:
    """覆盖 / 新鲜度 / 是否退化为一个常数。

    ⚠️ 覆盖判据**按因子各自的基线**（config.FACTOR_EXPECTED_INDUSTRIES），不是统一缺失率：
    `crowd` 源表只覆盖 94/131 个行业且起于 2020-02-20 —— 用统一阈值会天天假报警，
    真正要抓的是"覆盖率相对自己的基线**掉了**"。
    """
    if F is None or len(F) == 0:
        _add(res, "STYLE_PANEL_EMPTY", "因子面板为空")
        return
    dates = pd.to_datetime(F.index)
    td = pd.Timestamp(target_date) if target_date is not None else dates.max()
    lag = int((td - dates.max()).days)
    res.stats["panel_latest"] = str(dates.max().date())
    res.stats["panel_lag_days"] = lag
    if dates.max() < td - pd.Timedelta(days=C.MAX_PANEL_LAG_DAYS):
        _add(res, "STYLE_PANEL_STALE",
             f"{panel_name} 最新 {dates.max().date()}，落后目标日 {lag} 天"
             f"（上限 {C.MAX_PANEL_LAG_DAYS}）")

    if X is not None and len(X):
        tail = X.tail(C.WINDOW_DAYS)
        cov = {c: int(tail[c].notna().any() and tail[c].count() > 0) for c in X.columns}
        last_row = X.iloc[-1]
        res.stats["industry_coverage_last"] = {
            c: int(last_row[c].notna().sum()) for c in X.columns}
        for c, n in res.stats["industry_coverage_last"].items():
            base = C.FACTOR_EXPECTED_INDUSTRIES.get(c)
            if base is None:
                continue
            if n / base < C.MIN_COVERAGE_RATIO:
                _add(res, "STYLE_PANEL_COVERAGE",
                     f"因子 {c} 当日只覆盖 {n}/{base} 个行业（< "
                     f"{C.MIN_COVERAGE_RATIO:.0%} 基线）")

    # β 面板非退化：因子列不能是常数，也不能全 NaN
    stds = F.std(ddof=0)
    res.stats["factor_std"] = {k: round(float(v), 6) for k, v in stds.items()}
    for k, v in stds.items():
        if not np.isfinite(v) or v == 0:
            _add(res, "STYLE_PANEL_DEGENERATE", f"因子 {k} 的标准差为 {v}（退化为常数/全 NaN）")


def probe_factor_count(n_used: pd.Series, res: ProbeResult) -> None:
    """**当日实际纳入因子数 == K** —— 与代数不变量配对的那一半。"""
    last = n_used.dropna()
    if last.empty:
        _add(res, "STYLE_FACTORS_NONE", "没有任何交易日的纳入因子数可用")
        return
    got = int(last.iloc[-1])
    res.stats["n_factors_used_last"] = got
    res.stats["n_factors_expected"] = C.K
    if got < C.K:
        _add(res, "STYLE_FACTORS_DROPPED",
             f"当日实际纳入因子 {got} 个 < 声明 K={C.K}"
             f"（某因子列被静默丢弃；corr(残差,因子)≈0 的不变量此时**不会**报警）")


# ─────────────────── ② 代数不变量（必须与 ① 配对）───────────────────
def _mean_abs_corr(Pn: pd.DataFrame, F: pd.DataFrame,
                   cols: Sequence[str], window: int) -> Dict[str, float]:
    """政策 instrumental：残差面板 vs 各因子的**逐行业相关绝对值均值**"""
    X = Pn.tail(window).to_numpy(dtype=float)
    Xf = F.reindex(Pn.index).tail(window)[list(cols)].to_numpy(dtype=float)
    out: Dict[str, float] = {}
    for j, name in enumerate(cols):
        mask = np.isfinite(X).all(axis=1) & np.isfinite(Xf[:, j])
        if mask.sum() < 30:
            out[name] = float("nan")
            continue
        ys, xs = X[mask], Xf[mask, j]
        c = (ys * xs[:, None]).mean(axis=0) / (ys.std(axis=0) * xs.std() + 1e-15)
        out[name] = float(np.nanmean(np.abs(c)))
    return out


def probe_resid_orthogonality(resid_adj: pd.DataFrame, resid_raw: pd.DataFrame,
                              F: pd.DataFrame, window: int, res: ProbeResult) -> None:
    """风格调整是否真的起作用 —— **相对口径**的代数不变量。

    ⚠️ 为什么不用「|corr(调整残差, 因子)| < ε」这种绝对判据：本项目的残差化是
    **out-of-sample**（β 用 [t−W, t−1] 估计后作用在 t），而 `corr(r̃,f)≈0`
    只对 **in-sample** 残差成立 ⇒ 绝对判据在健康样本上也会报警（实测 |corr| 0.05~0.11）。
    改用**相对判据**：`|corr|adj / |corr|raw ≤ RESID_ORTHO_REL_MAX`。
    健康样本实测比值 0.29~0.58；若调整静默失效则 adj = raw ⇒ 比值 = 1.0，**必报警**。
    """
    common = [c for c in C.FACTORS if c in F.columns]
    ca = _mean_abs_corr(resid_adj, F, common, window)
    cr = _mean_abs_corr(resid_raw, F, common, window)
    res.stats["resid_factor_abs_corr_adj"] = {k: (round(v, 4) if np.isfinite(v) else None)
                                              for k, v in ca.items()}
    res.stats["resid_factor_abs_corr_raw"] = {k: (round(v, 4) if np.isfinite(v) else None)
                                              for k, v in cr.items()}
    ratios: Dict[str, object] = {}
    negligible: List[str] = []
    for k, v in ca.items():
        base = cr.get(k)
        if not np.isfinite(v) or base is None or not np.isfinite(base):
            ratios[k] = None
            _add(res, "RESID_ORTHO_UNTESTABLE",
                 f"因子 {k} 的相关不可算（样本不足）", level="warning")
            continue
        if base < C.RESID_ORTHO_MIN_RAW:
            # 该因子本来就没暴露 ⇒ "去掉了几成"无意义（噪声因子会给出 >1 的假报警）
            ratios[k] = None
            negligible.append(k)
            continue
        ratios[k] = round(v / base, 3)
        if ratios[k] > C.RESID_ORTHO_REL_MAX:
            _add(res, "RESID_ORTHO_FAIL",
                 f"因子 {k}：|corr| adj/raw = {v:.3f}/{base:.3f} = {ratios[k]:.2f} "
                 f"> {C.RESID_ORTHO_REL_MAX} ⇒ 风格调整几乎没有去掉它的暴露"
                 f"（典型故障：该因子被静默丢弃，adj 退化成 raw）")
    res.stats["resid_factor_ratio"] = ratios
    res.stats["resid_factor_negligible"] = negligible


# ─────────────────── ③ 长窗口佐证：差额是否塌陷 ───────────────────
def probe_gap_collapse(gap_series: pd.Series, res: ProbeResult) -> None:
    """风格解释份额的 20 日滚动均值 → 0，说明"调整"可能已经不再起作用。"""
    if gap_series is None or len(gap_series) == 0:
        return
    roll = gap_series.abs().rolling(C.GAP_WINDOW, min_periods=C.GAP_WINDOW).mean().dropna()
    if roll.empty:
        return
    last = float(roll.iloc[-1])
    res.stats["gap_20d_mean"] = round(last, 5)
    if last < C.GAP_COLLAPSE_TOL:
        _add(res, "STYLE_GAP_COLLAPSED",
             f"风格解释份额的 {C.GAP_WINDOW} 日滚动均值 {last:.5f} < {C.GAP_COLLAPSE_TOL}"
             f"（差额天然会随行情安静而变小 ⇒ 这条只作长窗口佐证，不单独报警）",
             level="warning")


# ─────────────────── ④ 金丝雀链（端到端）──────────────────
def canary_chain(betas: pd.DataFrame, codes: Sequence[str],
                 factor: str = C.CANARY_FACTOR, n: int = C.CANARY_SIZE) -> Dict[str, List[str]]:
    """合成一条**必然由风格驱动**的链：取某风格因子 β **同向极端**的 n 个行业。

    ⚠️ 必须取**同向**（全部高暴露），不能取"两端各一半" ——
    实测 size 两端混合组的原始共振只有 −0.013（本来就没共振，调不调都是 0，
    这种金丝雀**根本不会报警**）；而 size 高暴露四人组 raw **0.699 → adj 0.276**。
    """
    col = f"beta_{factor}"
    if betas is None or len(betas) == 0 or col not in betas.columns:
        return {}
    last_date = betas["date"].max()
    b = betas[betas["date"] == last_date].set_index("ind_code")[col].dropna()
    b = b.reindex(list(codes)).dropna()
    if len(b) < n:
        return {}
    members = sorted(set(list(b.sort_values(ascending=False).index[:n])))
    return {"__CANARY__": members}


def probe_canary(resonance_adj: float, resonance_raw: float, res: ProbeResult) -> None:
    """金丝雀的**调整值必须相对原始值塌陷**（判据用比值，不用绝对阈值）。

    实测健康样本：raw 0.699 → adj 0.276（比值 0.395）。若风格调整静默失效，
    adj = raw ⇒ 比值 1.0 > CANARY_RATIO_MAX ⇒ 报警。
    """
    res.stats["canary_raw"] = resonance_raw
    res.stats["canary_adj"] = resonance_adj
    if resonance_adj is None or not np.isfinite(resonance_adj) or \
            resonance_raw is None or not np.isfinite(resonance_raw):
        _add(res, "CANARY_MISSING", "金丝雀链未产出共振值（链路可能已断）")
        return
    if resonance_raw < C.CANARY_MIN_RAW:
        _add(res, "CANARY_WEAK",
             f"金丝雀原始共振仅 {resonance_raw:+.3f} < {C.CANARY_MIN_RAW}"
             f"（构造失效 ⇒ 起不到端到端作用；多半是 β 面板取错了日期或因子）",
             level="warning")
        return
    ratio = resonance_adj / resonance_raw
    res.stats["canary_ratio"] = round(float(ratio), 3)
    if ratio > C.CANARY_RATIO_MAX:
        _add(res, "CANARY_ALIVE",
             f"金丝雀 adj/raw = {resonance_adj:+.3f}/{resonance_raw:+.3f} = {ratio:.2f} "
             f"> {C.CANARY_RATIO_MAX} ⇒ 风格调整**没有**生效却仍在出数"
             f" —— 这正是『悄悄退化成原始值』的特征")


# ─────────────────── 汇总入口 ───────────────────
def run_probes(F: pd.DataFrame, X: Optional[pd.DataFrame], n_used: pd.Series,
               resid_adj: pd.DataFrame, resid_raw: pd.DataFrame,
               betas: pd.DataFrame, codes: Sequence[str],
               corrs_raw: Dict, corrs_adj: Dict, l1_of: Dict[str, str],
               gap_series: Optional[pd.Series] = None,
               target_date=None) -> ProbeResult:
    """跑全套探针，返回 ProbeResult（`ok=False` ⇒ 日报必须带未调整标注）。"""
    res = ProbeResult()
    probe_panel_input(F, X, res, target_date=target_date)
    probe_factor_count(n_used, res)
    probe_resid_orthogonality(resid_adj, resid_raw, F, C.WINDOW_DAYS, res)
    if gap_series is not None:
        probe_gap_collapse(gap_series, res)

    canary = canary_chain(betas, codes)
    if canary:
        idx_map = {c: i for i, c in enumerate(codes)}
        ccs = canary["__CANARY__"]
        idx = [idx_map[c] for c in ccs if c in idx_map]
        last = max(set(corrs_raw) & set(corrs_adj), default=None)
        if last is not None and len(idx) >= 2:
            probe_canary(G.group_statistic(corrs_adj[last], idx),
                         G.group_statistic(corrs_raw[last], idx), res)
    else:
        _add(res, "CANARY_UNBUILDABLE", "无法构造金丝雀链（β 面板缺失）")

    return res
