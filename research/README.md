# `research/` — 历史回测脚本存档（**冻结，不维护**）

> 依据 `README.md` 的目录约定：本目录是**存档**，不是代码库。
> **不新增脚本、不修bug、不保证可运行**（多数硬编码了数据根路径，
> 2026-10-26 已统一指向 `~/quant-data`，但存档脚本仍按「冻结」处理）。
>
> 🔴 **2026-10-06：脚本本身不移动，但它们的结论已部分作废。**
> 下面列出「对应结论已作废」的映射，避免下一个读代码的人把旧结论当现行结论。

---

## 一、结论已作废（脚本保留作为推理过程存档）

### 1.1 宽度预测力 —— 全部作废

| 脚本 | 原结论 | 现状 |
|:--|:--|:--|
| `backtest_width_reversal_v3.py`、`backtest_width_reversal_v4.py`、`backtest_width_reversal_v5.py` | 宽度反转有正向 alpha |⛔ **作废**。宽度→未来 20 日收益 IC ≈ **−0.004**（非重叠 t= −0.74） |
| `backtest_ma20_breadth.py`、`backtest_ma20_full.py`、`backtest_ma20_v2.py`、`backtest_ma20_v3.py` | "距 MA20"是有效的选股因子 | ⛔ **作废**。实测「距 MA20」与「个股过去 20 日收益」几乎是同一个因子（各自单独系数 −0.043 / −0.045，同放后腰斩） |
| `breadth2_expansion_backtest.py` | 宽度 2 日扩张有增量信息 | ⛔ **作废**。3 日变化没有增量信息，20 日变化才有 |
| `ic_analysis_5factors.py` | 五因子横截面 IC 显著 | ⚠️ **口径已废弃**。它用 `pctChg > 0` 算宽度，与生产口径 `(close > MA20).mean()` **不是同一个量** |
| `check_breadth_distribution.py` | 宽度分布诊断 | ✅ 工具仍可用（只做分布诊断，不涉预测） |

**定稿依据**：`docs/archive/breadth-what-it-can-and-cannot-do.md`
（v1.2，2026-10-05/06 修订）。

### 1.2 冷区 + 宽度扩张组合 —— 作废

| 脚本 | 原结论 | 现状 |
|:--|:--|:--|
| `backtest_cold_expansion_combo.py` | 冷区叠加宽度扩张是最佳组合 | ⛔ **作废**。2×2 因子设计下**交互项 ≈ −0.01pp**（无交互）；唯一"好看"的均值 +0.34pp 是**尾部假象**（中位数 −0.13pp、抵消率 0.080） |

**定稿依据**：`docs/industry-analysis-methodology.md` §3.5。

### 1.3 反转跟踪池（S₀ 门槛体系）—— 装置已拆除

| 脚本 | 原结论 | 现状 |
|:--|:--|:--|
| `s0_new_gate_backtest.py`、`s0_new_weight_backtest.py`、`s0_dimension_ablation.py`、`s0_dimension_backtest.py`、`s0_dimension_multi_horizon.py` | S₀ 加权 / 维度消融可提升选股质量 | ⛔ **作废**。引擎自身 entry 事件 h=20 均值 **−0.219pp**、按日聚类 **t=−2.87**，IS/OOS 同号 ⇒ 前提被**反驳**，非样本不足 |
| `backtest_wake_signal.py`、`backtest_wake_signal_v2.py`、`backtest_wake_abc.py`、`backtest_wake_pure.py`、`backtest_wake_pure_vA.py` | "唤醒"事件是反转起点 | ⛔ **作废**（同上，是同一装置的早期版本） |
| `backtest_reversal_params.py`、`and_backtest_industry.py`、`and_backtest_industry_b.py`、`bt_final.py`、`bt_final_v2.py`、`bt_verify_v3.py`、`backtest_two_board_limit.py`、`backtest_two_board_limit_v2.py` | 反转参数网格 / 两板限制 / 行业反转组合 | ⛔ **作废**（反转跟踪体系的一部分） |
| `explore_hot_zone_4_crowding.py` | 热区拥挤度筛选 | ⛔ **作废**（热区框架从未实现，无实证内容） |

**定稿依据**：`docs/archive/tracker-design-and-issues.md` §8.2。

### 1.4 数量溯源工具 —— 仍可用

| 脚本 | 说明 |
|:--|:--|
| `trace_signal_count.py`、`trace_signal_count_gap.py` | 统计信号条数、追查"为什么比预期少"。与预测无关，**工具仍有效** |
| `ic_industry_top20.py` | 行业 Top20 榜单 —— ⚠️ 口径依赖已作废的宽度结论，用前先核对 |

---

## 二、为什么归档而不删除

1. **推理过程本身是有价值的**：这些脚本记录了「当时怎么想的、为什么错了」。
   删掉它们，后来者会重新踩同一个坑。
2. **结论作废 ≠ 过程作废**：本项目 24 小时内在前瞻窗口上翻过三次车，
   每次结论都变（`docs/changes-2026-10-05-breadth-window-bug.md`）。
   这些脚本正是那三次翻车的现场证据。
3. **路径引用未失效**：`README.md` 与 10 个文档、18 处引用 `research/*.py`，
   移动会全部失效 ⇒ **保持原地不动**（这也是审查文档的建议 3）。

---

## 三、当前状态

- **活跃脚本**：0个（无任何 `research/` 脚本在生产链路上）
- **生产代码引用 research/**：0 处（已实测 `grep` 确认）
- **处置决定**：2026-10-06 主人拍板**归档不移动**，只补这份映射表
- **新增脚本**：⛔ 不允许（本目录冻结）

相关：`docs/plan/retirement-breadth-tracker-plan.md` §5.1