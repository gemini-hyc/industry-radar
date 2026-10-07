"""测量层的**锁定规格**（spec）—— 与 `config.py` 同源 SHA256 复现三件套的第 ③ 件。

为什么规格必须写成代码常量
--------------------------
review §7 的两个悬案（167/170、19/29）都是"同一个名字下面，没人意识到的自由度"造成的；
其中 19 vs 16 那一处，差异**纯粹来自「z 标准化在全样本做还是段内做」**。
⇒ 规格不能靠注释传达，必须是代码里的常量 + meta 里的记录（随产物一起同步/归档）。

改这里的任何一项 ⇒ 历史产物口径变更 ⇒ 必须**新建版本**并重跑，不得原地改。
"""
from __future__ import annotations

from typing import Any, Dict

# ── ① 收益与市场代理 ────────────────────────────────────────────
RETURN_SOURCE = "industry/industry_weighted_returns.parquet"
RETURN_COL = "weighted_pct"           # 行业内流通市值加权涨跌幅(%)
RETURN_UNIT_DIV = 100.0               # → 小数
MARKET_PROXY = "equal_weight_131"     # 131 行业等权均值（日收益）

# ── ② 风格因子 ──────────────────────────────────────────────────
# 顺序即回归列顺序；**K = len(FACTORS)**（探针断言"当日实际纳入因子数 == K"的 K 就是它）。
FACTORS = (
    "size",      # log(行业流通市值)                    ← industry_weighted_returns.circ_mv
    "value",     # −log(行业中位 PB)                    ← daily_basic.pb 行业聚合
    "mom20",     # 过去 20 日累计收益（t−21..t−1）        ← 收益面板自算
    "vol20",     # 过去 20 日已实现波动（t−21..t−1）      ← 收益面板自算
    "crowd",     # 拥挤度（t−1）                        ← factors/ind_crowd_turnover_daily（个股→行业）
)
K = len(FACTORS)

# ── ③ 估计窗口与作用域（容易产生"+3 对"级差异的三项）────────────
WINDOW_DAYS = 250                     # β 估计窗口（交易日）；回看 [t−W, t−1]，**不含 t**
STANDARDIZE_SCOPE = "cross_section_daily"   # 特征标准化：**每日横截面 z-score**
WITH_INTERCEPT = True                 # 回归带截距（带 ⇒ 残差自动剥离等权市场收益）
MIN_CS_N = 30                         # 当日有效行业数下限：不足则该日不产因子收益
MIN_WINDOW_VALID_RATIO = 0.90         # 窗口内某因子的有效行占比下限：不足则**剔除该因子**
                                      # （⇒ 纳入因子数 < K ⇒ 探针报警，见 probe.py）

# ── ④ 零分布 ────────────────────────────────────────────────────
NULL_SEED = 20261006
N_ITER = 2000                         # 分层零分布抽样次数
NULL_LEVEL = 0.95                     # 报告用的分位（p95）

# ── ⑤ 探针阈值 ──────────────────────────────────────────────────
MAX_PANEL_LAG_DAYS = 3                # 面板相对最新交易日的最大允许滞后
# ⚠️ **覆盖基线**（2026-10-07 实测）：不再是"全表缺失率"，而是**每个因子声明自己覆盖多少行业**。
#    原因：`crowd` 源表**只覆盖 94/131 个行业**（且起于 2020-02-20）—— 若用统一缺失率阈值，
#    它从第一天起就天天报警（假阳性），而真正要抓的是"覆盖率相对自己的基线**掉了**"。
FACTOR_EXPECTED_INDUSTRIES = {
    "size": 131, "value": 131, "mom20": 131, "vol20": 131, "crowd": 94,
}
FACTOR_START = {"crowd": "2020-02-20"}      # 已知的结构性起始滞后
MIN_COVERAGE_RATIO = 0.90                   # 当日覆盖行业数 / 该因子基线 的下限
# ⚠️ 正交性判据**用相对口径**：本项目的残差化是 **out-of-sample**（β 用 [t−W, t−1]
#   估计、作用在 t），`corr(r̃, f_k) ≈ 0` 只对 **in-sample** 残差成立。
#   实测健康样本：|corr| raw 0.083~0.329 → adj 0.048~0.113，比值 **0.29~0.58**。
#   ⇒ 判据 = 比值 ≤ RESID_ORTHO_REL_MAX（若调整静默失效，adj = raw ⇒ 比值 = 1.0，必报警）。
RESID_ORTHO_REL_MAX = 0.75
# 只有**原始暴露足够大**的因子才适用比值判据：若某因子本来就与残差几乎无关
# （raw |corr| < 此值），"去掉多少比例"没有意义（噪声因子会出现比值 > 1 的假报警）。
RESID_ORTHO_MIN_RAW = 0.05
GAP_WINDOW = 20                       # 差额（原始−调整）的长窗口佐证长度
GAP_COLLAPSE_TOL = 0.01               # 20 日均差低于此值 ⇒ 疑似"悄悄退化成原始值"
CANARY_FACTOR = "size"                # 金丝雀按哪个因子取极端暴露组（实测 size 最有效）
CANARY_SIZE = 4                       # 金丝雀成员数
CANARY_MIN_RAW = 0.25                 # 金丝雀的原始共振下限：低于它说明"构造失效"，起不到端到端作用
CANARY_RATIO_MAX = 0.70               # 金丝雀 adj/raw 上限：调整生效 ⇒ 大幅塌陷                                        # （实测 size 高暴露四人组 raw 0.699 → adj 0.276 = 0.395；调整失效 ⇒ 1.0）

# 面板文件名（登记在 src.paths.ARTIFACTS）
STYLE_PANEL_NAME = "industry_style_panel"        # 特征面板（date × ind_code × 因子）
STYLE_FACTOR_NAME = "industry_style_factors"     # 因子收益面板（date × factor）
RESIDUAL_PANEL_NAME = "industry_residual_panel"  # 残差收益面板 r̃（raw / adj）


def spec_dict() -> Dict[str, Any]:
    """写进 meta 的规格快照（复现三件套 ③）。"""
    return {
        "return_source": RETURN_SOURCE,
        "return_col": RETURN_COL,
        "return_unit_div": RETURN_UNIT_DIV,
        "market_proxy": MARKET_PROXY,
        "factors": list(FACTORS),
        "K": K,
        "window_days": WINDOW_DAYS,
        "beta_estimation": "trailing [t-W, t-1], applied at t (out-of-sample)",
        "standardize_scope": STANDARDIZE_SCOPE,
        "with_intercept": WITH_INTERCEPT,
        "min_cs_n": MIN_CS_N,
        "min_window_valid_ratio": MIN_WINDOW_VALID_RATIO,
        "null_seed": NULL_SEED,
        "n_iter": N_ITER,
        "null_level": NULL_LEVEL,
        "probe": {
            "max_panel_lag_days": MAX_PANEL_LAG_DAYS,
            "factor_expected_industries": FACTOR_EXPECTED_INDUSTRIES,
            "factor_start": FACTOR_START,
            "min_coverage_ratio": MIN_COVERAGE_RATIO,
            "resid_ortho_rel_max": RESID_ORTHO_REL_MAX,
            "resid_ortho_min_raw": RESID_ORTHO_MIN_RAW,
            "gap_window": GAP_WINDOW,
            "gap_collapse_tol": GAP_COLLAPSE_TOL,
            "canary": {"factor": CANARY_FACTOR, "size": CANARY_SIZE,
                       "min_raw": CANARY_MIN_RAW, "ratio_max": CANARY_RATIO_MAX},
        },
    }
