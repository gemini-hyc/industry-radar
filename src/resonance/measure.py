"""测量层核心：**收益层残差化** + (链, 日) 双输出。

为什么在**收益层**做残差化（review §12.2 的推荐，本项目采用）
-----------------------------------------------------------------------
规范的建议很明确："推荐在收益层做残差化（r̃ = r − Σβf），不要在相关层做偏相关
—— 这样原始共振与调整共振从同一残差面板派生，group_statistic 一处实现两用。"

    r̃⁰_{i,t} = r_{i,t} − [α + β_mkt · mkt_t]                     ← 原始（只有市场）
    r̃¹_{i,t} = r_{i,t} − [α + β_mkt · mkt_t + Σ_k β_{i,k} f_{k,t}] ← 调整（市场 + 风格）

两者**同一个估计器、同一段窗口、同一份面板**，只差回归因子集合 ⇒ 「原始 − 调整」的
差额就是风格的解释份额，可加可审计。

⛔ 分工边界（不要拿本模块的数去替换锁定的验证口径）：
   · **验证通道**（Q 检验三分类）：段内 in-sample beta 回归，见 chain-clustering-spec.md
   · **测量通道**（本模块）：trailing out-of-sample beta（用 [t−W, t−1] 估计、作用在 t），
     因为它是**每天要出数**的生产状态量。两者不可混用，混用即第三次"两套口径"翻车。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from src.resonance import config as C
from src.resonance import group as G


@dataclass
class ResidualResult:
    """残差化结果（两个残差面板 + 诊断信息）"""
    raw: pd.DataFrame            # date × ind_code
    adj: pd.DataFrame            # date × ind_code
    betas: pd.DataFrame          # date × (const, mkt, f_*) × ind_code 的最近一次 beta（长表）
    n_factors_used: pd.Series    # 每个交易日**实际纳入**的因子数（探针的 K 断言用）
    beta_std: pd.Series          # 每个交易日 beta 的横截面标准差（非退化判据）


# ─────────────────────────── 残差化 ───────────────────────────
def residualize(R: pd.DataFrame, F: pd.DataFrame,
                window: int = C.WINDOW_DAYS) -> ResidualResult:
    """trailing out-of-sample 残差化：β 用 [t−W, t−1] 估计，作用在 t。

    返回两个残差面板 + 诊断；`raw` 只剥市场，`adj` 剥市场 + 风格。
    """
    codes = list(R.columns)
    dates = list(R.index)
    X = R.to_numpy(dtype=float)
    mkt = np.nanmean(X, axis=1)
    Fv = F.reindex(index=dates).to_numpy(dtype=float)

    A0_full = np.column_stack([np.ones(len(dates)), mkt])
    A1_full = np.column_stack([np.ones(len(dates)), mkt, Fv])

    n_use: Dict = {}
    b_std: Dict = {}
    last_beta = {}
    # beta 面板不再全历史落盘（T×N×(K+2) 太大）；只保留每个 t 的横截面诊断。
    X_raw = np.full_like(X, np.nan)
    X_adj = np.full_like(X, np.nan)

    for t in range(1, len(dates)):
        lo = max(0, t - window)
        # 先看每个因子在估计窗内的有效行占比
        cols_ok = [True, True]                       # const, mkt 恒纳入
        for j in range(A1_full.shape[1] - 2):
            col = A1_full[lo:t, 2 + j]
            valid = np.isfinite(col).mean()
            cols_ok.append(bool(valid >= C.MIN_WINDOW_VALID_RATIO))
        keep = [0, 1] + [2 + j for j, ok in enumerate(cols_ok[2:]) if ok]
        n_use[dates[t]] = sum(cols_ok[2:])

        A1 = A1_full[lo:t][:, keep]
        y = X[lo:t]
        rows_ok = np.isfinite(A1).all(axis=1) & np.isfinite(y).all(axis=1)
        if rows_ok.sum() < max(window // 5, 20):
            b_std[dates[t]] = np.nan
            continue
        b1, *_ = np.linalg.lstsq(A1[rows_ok], y[rows_ok], rcond=None)
        X_adj[t] = X[t] - A1_full[t, keep] @ b1
        b_std[dates[t]] = float(np.nanstd(b1[2:]) if b1.shape[0] > 2 else np.nan)
        last_beta[dates[t]] = {"keep_cols": keep, "beta": b1}

        A0 = A0_full[lo:t]
        rows_ok0 = np.isfinite(A0).all(axis=1) & np.isfinite(y).all(axis=1)
        if rows_ok0.sum() >= max(window // 5, 20):
            b0, *_ = np.linalg.lstsq(A0[rows_ok0], y[rows_ok0], rcond=None)
            X_raw[t] = X[t] - A0_full[t] @ b0

    beta_rows = []
    for dt, info in last_beta.items():
        for i, code in enumerate(codes):
            row = {"date": dt, "ind_code": code}
            for ci, cname in zip(info["keep_cols"], ["const", "mkt"] + list(C.FACTORS)):
                row[f"beta_{cname}"] = float(info["beta"][ci, i])
            beta_rows.append(row)
    betas = pd.DataFrame(beta_rows)

    return ResidualResult(
        raw=pd.DataFrame(X_raw, index=dates, columns=codes),
        adj=pd.DataFrame(X_adj, index=dates, columns=codes),
        betas=betas,
        n_factors_used=pd.Series(n_use, index=dates).reindex(dates),
        beta_std=pd.Series(b_std, index=dates).reindex(dates),
    )


# ─────────────────────────── 滚动相关 ───────────────────────────
def rolling_corr(P: pd.DataFrame, window: int = C.WINDOW_DAYS) -> Dict[pd.Timestamp, np.ndarray]:
    """残差面板 → 每个交易日一张相关阵（窗口 = 最近 window 个交易日的残差）。"""
    X = P.to_numpy(dtype=float)
    dates = list(P.index)
    out: Dict[pd.Timestamp, np.ndarray] = {}
    for t in range(window, len(dates)):
        block = X[t - window + 1: t + 1]
        with np.errstate(invalid="ignore", divide="ignore"):
            Cm = np.corrcoef(block, rowvar=False)
        out[dates[t]] = Cm
    return out


# ─────────────────────────── 链测量 ───────────────────────────
def measure_chains(chains: Dict[str, List[str]], corrs_raw: Dict, corrs_adj: Dict,
                   codes: Sequence[str], l1_of: Dict[str, str],
                   pools: Dict[str, List[int]], n_iter: int = C.N_ITER,
                   with_null: bool = True) -> pd.DataFrame:
    """(链, 日) 双输出：**标量**（链内比较）+ **分层零分布 z 值**（跨链比较）+ 差额。

    chains: {chain_id: [ind_code, ...]}
    """
    pos = {c: i for i, c in enumerate(codes)}
    rows = []
    for date in sorted(set(corrs_raw) & set(corrs_adj)):
        Cr, Ca = corrs_raw[date], corrs_adj[date]
        for cid, members in chains.items():
            idx = [pos[m] for m in members if m in pos]
            if len(idx) < 2:
                continue
            sr, sa = G.group_statistic(Cr, idx), G.group_statistic(Ca, idx)
            rec = {"date": date, "chain_id": cid, "n_members": len(idx),
                   "resonance_raw": sr, "resonance_adj": sa,
                   "style_share": sr - sa}
            if with_null:
                comp = G.composition(idx, l1_of, list(codes))
                null = G.stratified_null(Ca, pools, comp, n_iter=n_iter)
                rec["z_adj"] = G.zscore(sa, null)
                rec["exceedance"] = 1.0 - G.exceedance(sa, null)
            rows.append(rec)
    return pd.DataFrame(rows)


def load_prior_chains(path, codes: Sequence[str]) -> Dict[str, List[str]]:
    """读先验名单 CSV（跳过 `#` 注释行）→ {chain_id: [ind_code]}，丢掉名单外行业。"""
    import csv
    from pathlib import Path
    text = Path(path).read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    rows = list(csv.DictReader(body.splitlines()))
    known = set(codes)
    chains: Dict[str, List[str]] = {}
    skipped: Dict[str, int] = {}
    for r in rows:
        code = r.get("ind_code", "").strip()
        if code not in known:
            skipped[r.get("chain_id", "?")] = skipped.get(r.get("chain_id", "?"), 0) + 1
            continue
        chains.setdefault(r.get("chain_id", "?"), []).append(code)
    if skipped:
        print(f"⚠️ 先验名单中有 {len(skipped)} 条链含面板外行业（已跳过对应成员）："
              f"{list(skipped)[:5]}")
    return {k: sorted(set(v)) for k, v in chains.items() if len(set(v)) >= 2}
