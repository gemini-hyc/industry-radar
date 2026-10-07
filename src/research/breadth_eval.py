"""宽度指标的**唯一评估口径**（2026-10-05）。

为什么要有这个模块
------------------
2026-10-05 一天之内，宽度检验的结论被推翻了三次，每次都因为口径散落在
临时探针里、没有单一真相来源：

| 时间 | 结论 | 实际错在哪 |
|:---|:---|:---|
| 上午 | 宽度有强正向预测力，月度 IC +0.59、t=15.2 | 前瞻窗口次序错（20 天里 19 天在过去） |
| 上午 | 宽度是逆向指标，h=20 非重叠 t=−1.29 | 窗口错位偏正、缺重叠校正偏负，方向相反互相掩盖 |
| 傍晚 | 40 日累计收益 +334% | `avg_pct` 单位已是百分数，又 `*100` |

⇒ **口径必须固化成代码，而不是每次重写探针。** 本模块是宽度/动量类
面板因子的唯一评估入口；`tests/test_breadth_signal.py` 只调用本模块，
不再自带统计代码。

两条不可协商的口径纪律（违反其一 ⇒ 结论无效）
----------------------------------------------
1. **前瞻窗口必须是 `[t+1, t+h]`，一天不含当日。**
   唯一实现 = `src.utils.forward.forward_sum`（= 台账 `forward_sum`）。
   ⛔ 禁止 `s.shift(-1).rolling(h).sum()`：**形状完全相同**，schema 断言、
   产物比对、IS/OOS 切分全都抓不到；IS/OOS 稳定**不能**证伪同期性。
2. **h>1 的重叠样本主判据用「非重叠 t」**（每 h 日取 1 天）。
   朴素 t 高估约 √h（h=20 时约 4.5 倍）。
   ⛔ 移动块 bootstrap 同样不可用：它仍把重叠样本当独立，给出假显著区间。

标准误说明
----------
本机**无 statsmodels**，Newey-West / 聚类稳健都需自行实现，而自实现的
NW 已被抓出两次 bug（i.i.d. 自检通过、但 `1+AR(1)ρ=0.5` 明显不显著却给
t=0.47）。⇒ **本模块只提供当前可全信的两个判据**：

- `nonoverlap_t` —— 不依赖任何标准误实现；
- `cluster_se` —— 聚类稳健回归（meat = Σ_g (X_g'e_g)(X_g'e_g)'，
  按**交易日**聚类）。这是自实现的，但 meat 构造简单、可自检。

**不要**在本模块里加 Newey-West。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.utils.forward import forward_sum

# 🔴 2026-10-06：统计原语已收敛到 src/evaluation/（全项目唯一实现处）。
#    本模块保留宽度专用层（build_panel / breadth_forward_*_ic）与历史结论，
#    通用原语一律 re-export，⛔ 不得在此重新实现。
from src.evaluation import (  # noqa: F401
    cluster_se,
    cs_rank_ic,
    nonoverlap_t_on_index,
    nonoverlap_t_sorted,
    tail_concentration,
    ts_quantile,
)

# 🔴 2026-10-07：原名 `nonoverlap_t = nonoverlap_t_on_index` 的兼容别名**已删除**。
#    它是收敛后残留的最后一个「同名不同义」陷阱：
#      `src.signals.evaluate.nonoverlap_t`      = **按日期去重**的事件流口径
#      `src.research.breadth_eval.nonoverlap_t` = **位置切片**的 Series 口径（旧别名）
#    两者同名、语义不同、导入错模块也不报错 ⇒ 与本项目已经翻过三次车同源。
#    现一律使用无歧义名字：`nonoverlap_t_on_index` / `nonoverlap_t_sorted`。

__all__ = [
    "MIN_STOCKS",
    "cs_rank_ic",
    "nonoverlap_t_sorted",
    "cluster_se",
    "ts_quantile",
    "build_panel",
    "ICResult",
    "breadth_forward_vol_ic",
    "breadth_forward_return_ic",
    "tail_concentration",
]

# 成分数下限。低于此值，breadth 的分母（MA 已成型的个股数）过小，
# 单只成分股就能让宽度跳 20pp ⇒ 读数是噪声不是状态。
# 实测：过滤前 131 行业，过滤后 109 行业。
MIN_STOCKS = 10


@dataclass
class ICResult:
    """一次横截面 IC 检验的完整结果（含所有主/辅判据）。"""

    n_days: int
    ic_mean: float
    t_naive: float
    t_nonoverlap: float
    pos_ratio: float
    horizon: int
    target: str
    nonoverlap_n: int = 0
    extra: dict = field(default_factory=dict)

    def line(self) -> str:
        return (f"{self.target:<22} h={self.horizon:<3} IC={self.ic_mean:+.4f} "
                f"朴素t={self.t_naive:+7.2f} 非重叠t={self.t_nonoverlap:+6.2f} "
                f"(n={self.n_days}日/{self.nonoverlap_n}非重叠, 阳性率={self.pos_ratio:.3f})")


# ──────────────────────────────────────────────────────────────────────
# 基础统计
# ──────────────────────────────────────────────────────────────────────
def build_panel(daily_full: pd.DataFrame,
                min_stocks: int = MIN_STOCKS) -> dict[str, pd.DataFrame]:
    """把 `industry_daily_full`（长表）转成评估用的宽表面板。

    返回 {'ret': 行业日均收益(%), 'breadth': 宽度, 'exc': 超额收益}，
    三者 index=交易日、columns=行业。

    ⚠️ `avg_pct` 的单位**已经是百分数**（实测单日 |值| 中位数 ≈ 1.02，
       q1%/q99% = −5.18/+5.29）。⛔ 不可再 `*100`。
    ⚠️ 行业主键列名是 `con_code`（不是 `ind_code`）—— 见 src/utils/market_data.py:174。
    """
    df = daily_full.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "con_code", "avg_pct", "breadth", "n_stocks"]].dropna(subset=["avg_pct"])
    df = df[df["n_stocks"] >= min_stocks]
    ret = df.pivot(index="date", columns="con_code", values="avg_pct")
    breadth = df.pivot(index="date", columns="con_code", values="breadth")
    ret = ret.sort_index()
    return {"ret": ret, "breadth": breadth.sort_index(), "exc": ret.sub(ret.mean(axis=1), axis=0)}


# ──────────────────────────────────────────────────────────────────────
# 宽度的两条已定论结论（2026-10-05 实测，见 docs/industry-analysis-methodology.md §3.3）
# ──────────────────────────────────────────────────────────────────────
def breadth_forward_return_ic(panel: dict[str, pd.DataFrame], horizon: int = 20) -> ICResult:
    """宽度 → 未来 h 日**超额收益**。**已实测无预测力**（|IC|<0.05、非重叠 t<1）。

    保留这个函数是为了**锁住"没有 alpha"这个结论**：若哪天它变显著了，
    说明口径被改坏或出现了新的市场结构，值得重新评估。
    """
    sig = panel["breadth"]
    fwd = forward_sum(panel["exc"], horizon)
    ic = cs_rank_ic(sig, fwd)
    t_no, n_no = nonoverlap_t_sorted(ic, horizon)
    return ICResult(n_days=len(ic), ic_mean=float(ic.mean()),
                    t_naive=float(ic.mean() * np.sqrt(len(ic)) / ic.std()),
                    t_nonoverlap=t_no, pos_ratio=float((ic > 0).mean()),
                    horizon=horizon, target="宽度→超额收益", nonoverlap_n=n_no)


def breadth_forward_vol_ic(panel: dict[str, pd.DataFrame], horizon: int = 20) -> ICResult:
    """宽度 → 未来 h 日**波动率**。**这是宽度唯一经检验成立的用途**（2026-10-05）。

    实测（131 行业、2020-01~2026-09、严格 shift(-h)）：
      IC=+0.1377、朴素 t=+33.5、**非重叠 t=+7.00**（IS +7.43 / OOS +4.65 / 剔除 covid +7.88）；
      控制"过去 20 日波动"后 beta=+0.0884、聚类稳健 t=+16.6（对照 beta=+0.3725/t=+42.0）。
    ⚠️ 朴素 t 高估约 4.5 倍（Q5−Q1 价差：朴素 +21.0 vs 非重叠 +3.94）—— **只看朴素 t 会高估 5 倍**。
    """
    sig = panel["breadth"]
    fwd_vol = panel["exc"].rolling(horizon, min_periods=horizon).std().shift(-horizon)
    ic = cs_rank_ic(sig, fwd_vol)
    t_no, n_no = nonoverlap_t_sorted(ic, horizon)
    return ICResult(n_days=len(ic), ic_mean=float(ic.mean()),
                    t_naive=float(ic.mean() * np.sqrt(len(ic)) / ic.std()),
                    t_nonoverlap=t_no, pos_ratio=float((ic > 0).mean()),
                    horizon=horizon, target="宽度→未来波动率", nonoverlap_n=n_no)


# ──────────────────────────────────────────────────────────────────────
# 事件研究：均值假象 / 非重叠 t 的排序陷阱（2026-10-05 实测两次踩坑）
# ──────────────────────────────────────────────────────────────────────
