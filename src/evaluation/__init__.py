"""`src.evaluation` —— 统计推断原语的**唯一实现处**。

⛔ 禁止在其他模块重新实现 `cluster_t` / `nonoverlap_t` / `cluster_se` /
`cs_rank_ic` / `ts_quantile` / `tail_concentration` —— 请从这里导入。

**为什么**：本项目在「前瞻窗口」上翻过三次车（`docs/changes-2026-10-05-breadth-window-bug.md`），
根因是同一统计量有多份实现、口径漂移。2026-10-06 建立本模块把口径收敛为一份。

只依赖 `numpy` / `pandas`（本机解释器**无 scipy**，勿引入）。
"""
from .inference import (  # noqa: F401
    cluster_se,
    cluster_t,
    cs_rank_ic,
    nonoverlap_t,
    nonoverlap_t_on_index,
    nonoverlap_t_sorted,
    tail_concentration,
    ts_quantile,
)

__all__ = [
    "cluster_se",
    "cluster_t",
    "cs_rank_ic",
    "nonoverlap_t",
    "nonoverlap_t_on_index",
    "nonoverlap_t_sorted",
    "tail_concentration",
    "ts_quantile",
]