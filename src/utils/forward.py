"""
前瞻收益的唯一入口 —— 研究脚本 / 探针一律 import 本模块，禁止自行实现。

背景（2026-10-05 事故）
----------------------
一个探针把"未来 20 日"写成：

    s.shift(-1).rolling(20).sum()        # ❌ 在 t 处给的是 [t-18, t+1]

形状与正确写法**完全一致**、也"看起来像"未来窗口，但 20 天里 19 天在过去。
后果：所谓"未来收益"与它自己的控制变量（过去 20 日收益）横截面相关高达
**+0.89**，把**同期**关系伪装成**预测**关系 —— 宽度检验偏系数 t=+15.2（IS/OOS
都不衰减、阳性对照 t=+25.3）⇒ 改正后 **t=+0.6**，阳性对照也塌到 t=−1.5。
详见 docs/changes-2026-10-05-breadth-window-bug.md。

正确写法（本模块唯一实现，与生产台账 src/signals/ledger.py:forward_sum 同源）：

    s.rolling(20, min_periods=20).sum().shift(-20)   # ✅ 在 t 处给的是 [t+1, t+20]

⚠️ horizon=1 时两者等价（`shift(-1)` 本身就是次日）⇒ 错误只在 horizon>1 时出现。
⚠️ 这条错误的隐蔽性在于：**IS/OOS 稳定性抓不到它**（同期关系天然稳定），
   能抓到它的是"因变量 vs 控制变量"的重合度体检（见 tests/test_forward_returns.py）。

用法
----
    from src.utils.forward import forward_sum, forward_excess

    fwd    = forward_sum(panel, 20)        # 面板: index=交易日, columns=行业
    excess = forward_excess(panel, 20)     # 减同期横截面均值（= 台账的 fwd_excess 口径）
"""
from __future__ import annotations

import pandas as pd

from src.signals.ledger import forward_sum

__all__ = ["forward_sum", "forward_excess"]


def forward_excess(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """前瞻 h 日的**超额**：前向窗口和 − 同窗口的横截面均值（行业等权基准）。

    与 src/signals/ledger.py 的 `fwd_excess_{h}` 同口径（基准 = 当日全部行业的
    横截面均值），只是这里以宽表（index=日期, columns=行业）形式给出。
    """
    fwd = forward_sum(frame, horizon)
    return fwd.sub(fwd.mean(axis=1), axis=0)
