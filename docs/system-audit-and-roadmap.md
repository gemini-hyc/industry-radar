# 行业分析系统 · 问题审计与改造路线（Agent 交接报告）

> 生成日期：2026-09-23
> 适用对象：接手本仓库的任何 agent / 工程师
> 目的：10 分钟内建立正确认知——系统能做什么、哪里是坏的、先改什么、别碰什么
> 阅读顺序建议：第 0 节速览 → 第 1 节现状 → 第 2 节问题 → 第 3 节路线 → 第 5 节操作须知

---

## 0. 速览（TL;DR）

1. **链路是健康的，模型是可疑的。** 数据管道（成分快照 / 加权 / 因子 / 报告 / 完备性检查）已相当扎实，问题集中在"把行业走势当生命周期来识别"这一层。
2. **"生命周期"层是手工参数机，不是可估计模型。** `industry_tracker.py` 有 **24 个手工常量**，而入场回测有效样本 **n≈95**（`wake_backtest_results.csv`）；`Sₜ = Sₜ₋₁ × λₜ` 自称"贝叶斯"但无似然/先验/后验。参数比样本多，注定过拟合、无法证伪。
3. **有三套互不校验的"阶段"体系**在同一份日报里并存：tracker 的 5~6 阶段、mainline 的 5 阶段、三区宽度框架。没有任何地方解释它们的关系或分歧。
4. **信号的实际强度远低于文档记载。** 新建立的信号台账实测（2020-02~2026-09，2234 条冷区信号）：20 日**超额 +0.31pp、胜率 49.9%、t=2.25**；文档记录为 +2.02%/62%。最冷档 +1.21pp/55.2%，文档记 +9.06%/84%。分年不稳定（2021/2024/2025 为负）。**任何基于旧文档数字的决策都会严重高估。**
5. **最强的调制变量是 regime，但它自己的一个维度是坏的。** 冷区信号在"全面上涨"日 +0.58pp（t=2.71）、"全面下跌"日 −0.32pp；而 `regime` 分类的"结构性"在 **1632 天里从未出现过**（`rank_consistency` 均值 −0.246，"结构性"要求 >0.15 仅 2 天满足）。
6. **验证底座已就位**：`signals/signals_log.parquet` + `scripts/evaluate_signals.py`。**从今往后，任何信号改动都必须先入台账、再跑评估**，否则又回到"每隔几个月重新争论一遍"的老路。

---

## 1. 系统现状速查

### 1.1 定位与技术栈

- 从 Hermes Quant 抽取的**行业分析链路**，独立演进；本仓库是唯一演进方向。
- Python 3.9.6（**系统解释器 `/usr/bin/python3`**，非虚拟环境），pandas 2.3.3 / numpy 2.0.2 / scipy 1.13.1 / pyarrow 21.0.0 / pyyaml / futu-api。
- 数据根：`/Users/hyc/quant-data`（**共享**，非本仓库内）；由 `config/config.yaml` 配置，可被环境变量 `INDUSTRY_RADAR_DATA` 覆盖。
- **铁律：所有路径走 `src/paths.py`，禁止硬编码 `/opt/data` 等容器路径。**

### 1.2 当前真实链路

```
[外部] openclaw tushare-daily-fetch 16:16 → market/daily + daily_basic
        │
        ├─ scripts/build_members_daily.py        每日成分快照（PIT，必须每日跑）
        ├─ scripts/compute_weighted_returns.py   circ_mv 加权涨跌幅（按日 upsert）
        ├─ scripts/build_industry_daily_full.py  等权行业日频聚合（MA20/13 宽度）
        ├─ scripts/update_industry_factor.py     拥挤度因子 ind_crowd
        ├─ scripts/industry_tracker.py           贝叶斯反转跟踪（生成本文件的"生命周期"）
        ├─ scripts/mainline_detector.py          主线探测（方向群 + 阶段）
        ├─ scripts/regime_classifier.py          行情 regime 分类
        └─ scripts/build_nav_924.py              行业走势主表（924 起净值/相对强度/动量）
        │
        ├─ cron/industry_review_daily.py         日频编排入口
        │    ├─ src/daily_review/module_02_industry.py   报告主体（三区框架）
        │    ├─ src/daily_review/report_sections.py      追加章节（跟踪池/主线/regime）
        │    ├─ scripts/check_data_completeness.py       数据完备性检查（防静默缺数）
        │    └─ src/signals/*                            信号台账入账 + 前向收益回填
        ▼
  reports/daily-analysis/YYYY-MM-DD_industry_review.md
  signals/signals_log.parquet + signals/eval_YYYYMMDD.md
```

**调度**：launchd `com.industry-radar.daily` 于每日 19:30 / 20:00 / 20:30 / 21:00 各触发一次，幂等标记 `logs/.scheduled_done_YYYY-MM-DD`（跑通即停；数据未到就等下一次；21:00 仍未到则系统通知）。入口 `scripts/run_daily_scheduled.sh`。

### 1.3 关键文件 → 真相来源对照

| 关心的东西 | 真相来源 |
|---|---|
| 行业全集 / 名称 / 成分 | `industry/industry_list.parquet`（131 行业）、`industry/industry_members_daily.parquet`（PIT 成分，带 in/out 日期） |
| 行业日收益（加权） | `industry/industry_weighted_returns.parquet`（`date, ind_code, weighted_pct, circ_mv, stock_n`） |
| 行业日频聚合（宽度等） | `industry/industry_daily_full.parquet`（`breadth` = 收盘站上 MA20 的个股占比） |
| 拥挤度 | `factors/ind_crowd_turnover_daily.parquet`（**个股级**，`date, code, value`；行业口径需按成分聚合） |
| 跟踪池状态 | `industry/tracker_state.json`（当前状态）、`industry/tracker_report.csv`（含入场日/阶段/Sₜ） |
| 主线方向群 | `industry/mainline_detection.csv`（每方向群一行，含 `is_mainline/stage/heat_ratio`） |
| 行情 regime | `industry/daily_regime.parquet`（`date, regime, up_pct, rank_consistency, ...`） |
| 日报 | `reports/daily-analysis/YYYY-MM-DD_industry_review.md` |
| 信号台账 | `signals/signals_log.parquet`（唯一键 `source+signal_date+ind_code`） |

---

## 2. 问题清单

严重度：🔴 影响结论正确性 / 🟠 影响可用性 / 🟡 影响可维护性

### 🔴 P1 "生命周期"层是手工参数机，统计上不可验证

- **现象**：`scripts/industry_tracker.py` 第 25–57 行定义 **24 个手工常量**（剔除分数/天数、保护期、金叉/二次金叉/死叉确认天数与间距、R=8% 真上涨阈值、A1–A4 预警的 2-of-4 规则、巨量倍数、保底预警间距……），且注释里带补丁日期（`07-03 从5下调`、`07-16 新增`、`C0/C2/C4 已废弃(2026-08-02)`）。
- **证据**：`scripts/industry_tracker.py:25-57`；入场回测有效样本 `industry/wake_backtest_results.csv` 首行 `n=95`（5 日/10 日各 95 条）。
- **影响**：参数数量与有效样本同量级甚至更多 → 过拟合不可避免；"贝叶斯引擎"名不符实（无似然、无先验、无后验，实际是手工衰减分数 + 阈值状态机）→ 结论无法证伪，改造反复回到原点。
- **建议**：见 P2（改造路线第 P2 期）。最低要求：每个阶段必须给出**前向收益分布 + 状态转移概率 + 期望停留天数**，否则它只能叫"展示"，不能叫"信号"。

### 🔴 P2 三套"阶段"体系并存且互不校验

- **现象**：同一份日报同时出现三种阶段语言：
  - tracker：沉睡→唤醒→成长→成熟→预警→衰退（README『口径备忘』节标为"五阶段"却列了 6 个；`industry_tracker.py:38` 注释写"四阶段生命周期参数"，README 链路图另处又写"四阶段"）
  - mainline：蛰伏 / 启动期 / 加速期 / 见顶期 / 瓦解期（`mainline_detection.csv` 的 `stage`）
  - 三区宽度：冷区（≤30%）/ 中区（30–70%）/ 热区（>70%）
- **影响**：agent 或人拿到报告无法判断"现在到底处于什么阶段"；三套标签可能互相矛盾却无人解释。
- **建议**：要么统一为一套阶段词汇，要么在报告中明确各自适用范围与优先级（例如：宽度定"位置"、mainline 定"资金方向"、tracker 定"事件生命周期"），并互相校验。

### 🔴 P3 生产信号与自身回测结论矛盾，且从未复核

- **现象**：`docs/module_02_rebuild.md:45-67` 规定冷区信号按 **4 个质量维度**打 2–13 分、按 ≥9/≥6/≥4 分级（文档称 IR 1.15/1.54/2.43）；而生产代码 `src/daily_review/factor_width_reversal.py:21-24` 明确写着 **v3（2026-07-29）砍掉 D1占比 / 三日量比 / 涨停情绪 三个维度**，改为只按"前20日均宽"分级，并且 `:97` 只在 **`expansion_length == 3`**（恰好第 3 天）触发一次。
- **影响**：
  - 被砍掉的三维正是文档中增量最大的部分（记录为 +0.7~1.7pp）；
  - "恰好第 3 天"使信号极度稀疏：2026-09-21 仅 1 条，09-22、09-23 均为 **0 条** → 报告常年空白；
  - 两处结论互相矛盾却都留在仓库里，接手者不知道该信谁。
- **建议**：用信号台账做 A/B（"≥3 天" vs "恰好第 3 天"；含/不含质量维度），以同一数据、同一超额口径给出对比表。

### 🔴 P4 信号实测强度远低于文档记载（已用台账量化）

- **实测**（`signals/eval_20260923.md`，2020-02-07~2026-09-21，2234 条冷区信号，**超额**口径）：

| 持有期 | 平均超额 | 中位超额 | 胜率 | t 值 |
|:---|:---:|:---:|:---:|:---:|
| 5日 | +0.00pp | −0.15pp | 47.4% | 0.02 |
| 20日 | **+0.31pp** | −0.02pp | **49.9%** | 2.25 |
| 60日 | +0.67pp | −0.57pp | 47.6% | 2.61 |

| 档位（前20日均宽） | 样本 | 平均超额 | 胜率 | t 值 |
|:---|:---:|:---:|:---:|:---:|
| high（<10%） | 252 | +1.21pp | 55.2% | 2.86 |
| standard（10–20%） | 916 | +0.15pp | 49.7% | 0.70 |
| watch（20–30%） | 1062 | +0.23pp | 48.9% | 1.18 |

- **对照文档**：`docs/module-02-backtest-findings.md` 记录"仅触发 N=2432 / 20日 +2.02% / 胜率 62%"，最冷档 **+9.06% / 84%**。
- **必须同时说明的 3 处口径差**（不全是"旧数字错"）：① 旧表用"累计收益"、台账用"超额"；② 旧样本规则"扩张≥3天"、生产规则"恰好第 3 天"；③ 数据地基此后已重建（PIT 成分 / `circ_mv` 加权 / 等权聚合）。
- **影响**：任何沿用旧数字的仓位/权重假设都会高估。
- **建议**：以台账数字为**唯一现役基线**；旧文档数字在 docs 中标注"历史口径，勿直接比较"。

### 🔴 P5 regime 分类的"结构性"维度实际已死

- **实测**（`industry/daily_regime.parquet`，1632 个交易日，2020-01-02~2026-09-23）：
  - 分类结果只有：全面上涨 458 / 全面下跌 419 / 轮动 388 / 偏弱轮动 198 / 混合 169 —— **"结构性" 0 条**；
  - `rank_consistency`：均值 **−0.246**、最小 −0.467、最大 +0.223；而 docstring 规定"结构性 = `rank_autocorr > 0.15`"，全历史仅 **2 天**满足。
- **影响**：regime 退化为只靠 `up_pct` + `top5_conc` 两维判断；而 regime 恰恰是**最强的信号调制变量**（见 P4 与下方"按 regime 分组"）。
- **建议**：先修符号/口径（怀疑自相关计算方向或基准有误），让"结构性"能正常触发；再把 regime 作为冷区信号的条件或权重（这是当前性价比最高的改造）。

**按 regime 分组的冷区信号表现（20日超额，台账实测）**：

| regime | 样本 | 平均超额 | 胜率 | t 值 |
|:---|:---:|:---:|:---:|:---:|
| 全面上涨 | 1026 | +0.58pp | 50.9% | 2.71 |
| 混合 | 230 | +0.28pp | 50.0% | 0.70 |
| 偏弱轮动 | 235 | +0.27pp | 48.5% | 0.62 |
| 轮动 | 577 | +0.03pp | 51.0% | 0.14 |
| 全面下跌 | 162 | −0.32pp | 42.0% | −0.63 |

→ 信号本质**顺市场状态**，不是独立反转 alpha。`docs/module-02-backtest-findings.md` 第二节记录的"子阶段调制"是文档里最大的未落地发现，至今未进入任何信号规则。

### 🟠 P6 三区框架只实现了 1/3

- **现象**：`src/daily_review/` 只有 `factor_width_reversal.py`；`module_02_industry.py:593-605` 的中区/热区仍是"（待实现）"占位。
- **对照**：文档里中区、热区候选信号的**回测数字都已存在**（热区"宽高+急跌 +3.01%/65%"、"缩量+宽度横盘 +3.72%/63%"是最强的一批；中区"价破5日新高+宽度升 +1.85%/64%"）。
- **影响**：日报信息量上限 = 一张冷区信号表；冷区零信号的日子报告近乎空白（2026-09-22/23 即是）。
- **建议**：补齐中区/热区实现，并**统一入台账**评估（否则又是"文档说好、实测未知"）。

### 🟠 P7 主线探测缺少规模与稳定性约束

- **现象**：`mainline_detection.csv` 中"群18（林业Ⅱ）"以 **1 个行业**被判定为主线（热度 3.95x、阶段=见顶期、`is_mainline=True`）。
- **影响**：单行业"方向群"当主线，噪声大、可操作性差。
- **建议**：加最小成员数约束（如 ≥2~3 个行业）+ 跨期稳定性约束（连续 N 日维持），并把结果纳入台账检验。

### 🟠 P8 报告曾"空转"（已修复，勿重复劳动）

- **现象（修复前）**：`module_02` 报告只输出冷区板块；而链路明明算出了跟踪池（24 行业）、主线（群18 见顶期）、regime，全都不进报告，只在日志/CSV 里。
- **已修复**：`src/daily_review/report_sections.py` 在 cron 中**追加**三章节（跟踪池 / 主线状态 / 行情 regime），2026-09-22 报告从 591B → 3828B。采用**追加而非改写**，`module_02` 原文逐字节不变，保留与旧链路的可比性。
- **遗留**：中区/热区仍空（P6）。

### 🟠 P9 无可交易载体与组合规则

- **现象**：行业指数不可直接交易；数据根内已有 `industry/etf_industry_similarity*.parquet` / `etf_holdings` 等 ETF 映射资产，但**未接入任何决策环节**；也没有持仓数、再平衡频率、成本假设。
- **影响**：即使超额为正也无法落地为可执行策略。
- **建议**：定义载体（ETF 或龙头组合）、持仓规则与成本，再做组合层回测。

### 🟡 P10 文档—代码漂移、口径并存

- README 自身阶段数不一致（"四阶段"/"五阶段"/列 6 个）。
- 宽度定义两套口径并存：生产用 `(close > MA20).mean()`，而 `research/ic_analysis_5factors.py` 用 `pctChg > 0`（`docs/module_02_rebuild.md:24` 已专门警告）。
- **建议**：单一真相来源；口径变更必须同步 docs，并在台账里重新评估。

### 🟡 P11 研究流程无统一 harness

- `research/` 有 **33 个脚本**，命名呈版本堆叠（`backtest_width_reversal_v3/v4/v5`、`bt_final.py`、`bt_final_v2.py`、`s0_dimension_*`），且目录已"冻结不维护"。
- **影响**：结论不可追溯、无法复现（这也是"每隔几个月重新争论"的根源之一）。
- **已缓解**：台账 + 评估器。**新实验一律走台账，不再新增一次性脚本。**

### ✅ 已解决（勿重复劳动）

| 旧问题 | 现状 |
|---|---|
| 成分前视偏差（历史回测用当前成分） | 已用 `industry_members_daily.parquet`（Tushare `index_member_all`，带 `in_date/out_date`）按当日真实成分聚合 |
| 加权口径（总市值） | 已改 `circ_mv` 流通市值加权，对标通达信官方点位平均绝对误差 ~0.10pp |
| 静默缺数（文件有行但内容为空） | 已加 `scripts/check_data_completeness.py`（CRITICAL/WARNING 分级 + 退出码 + `industry/completeness_status.json`） |
| 信号无留痕、无法验证 | 已建信号台账 `signals/signals_log.parquet` + 事件研究评估器 |
| 报告信息量低 | 已追加跟踪池/主线/regime 三章节（中区/热区仍待做） |

**⚠️ 待确认（不要假设已修）**：新股上市首日极值污染行业加权涨幅（历史上长鑫科技 688825 首日 +465.8% 曾把行业加权涨幅从 1.08% 拉到 100.66%，见 `docs/validation-2026-09-21.md:34`）。README 未说明当前是否已做剔除/缩尾处理，**需查 `compute_weighted_returns.py` 确认**。

---

## 3. 改造路线（按优先级，含验收标准）

### P0 ✅ 已完成：信号台账 + 事件研究框架

**交付物**：`src/signals/{ledger,sources,evaluate}.py`、`scripts/build_signals_log.py`、`scripts/evaluate_signals.py`、`docs/signal-ledger.md`；产物 `signals/signals_log.parquet`、`signals/eval_*.md`。

**要点**（后续 agent 必须遵守）：
- 台账唯一键 `(source, signal_date, ind_code)`，重复写入幂等；收益列保留已有非空值。
- 前向收益 = 信号日 **T+1..T+h 逐日加总**；基准 = **行业等权**（全部行业当日加权涨跌幅的横截面均值）；未到期保持 NaN。
- 两类来源：`cold_zone` 可**全历史回填**（复用生产函数，保证台账=日报口径）；`tracker_pool` / `mainline` 是**有状态引擎，只能向前快照**（每天约 20 条，半年后可做首轮统计）。
- cron 在**完备性检查通过之后**自动入账 + 回填，异常不影响主报告。

### P1 下一步建议（按此顺序）

| 序 | 任务 | 验收标准 |
|:--:|---|---|
| 1 | **冷区规则 A/B 复核**：加"≥3天"变体；分别评估含/不含质量维度（D1占比、量比、涨停） | eval 报告给出对比表；明确旧文档 +2.02%/62% 与新基线 +0.31pp/49.9% 的差异来自规则还是数据 |
| 2 | **修 regime + 内生化**：先修 `rank_consistency` 使"结构性"可触发；再把 regime 作为冷区信号的前置条件/权重 | 修复后"结构性"不再是 0 条；eval 显示条件化后的超额与 t 值明显优于无条件 |
| 3 | **补齐中区/热区信号**并统一入账 | 三区均有台账样本与 eval 数字；热区逆向信号是否复现 +3%/65% 有明确结论 |
| 4 | **mainline 加最小规模 + 跨期稳定性约束** | 不再出现"1 个行业的主线"；约束前后有台账对比 |
| 5 | **报告升级为"排序榜 + 跟踪"**：每日横截面打分替代纯事件触发，并列出昨日信号的今日跟踪结果 | 冷区零信号日报告仍有可读信息；信号有 follow-through 记录 |

### P2 生命周期模型化（等 P0 数据积累后再做）

- **首选：生存分析**（离散时间 hazard / Cox）——把"行业行情持续多久"建成生存过程，输出"已走 k 天、未来 n 日结束的条件概率"；天然处理删失、参数少、可检验。
- **次选：HMM / 马尔可夫状态**——状态由数据估计而非手写阈值，附带状态转移矩阵。
- **验收标准**：参数 ≤ 5~8 个；时间切分 OOS（如 2020–2023 训练 / 2024–2026 验证）可复现；相对现有 phase 框架有可量化的增量（同样走台账评估）。
- **若保留现有 phase 框架**：必须补上每阶段的前向收益分布、转移概率、期望停留天数。

### P3 落地层

定义可交易载体（ETF 映射 / 龙头组合）、持仓数与权重规则、再平衡频率、交易成本，再做组合层回测。

---

## 4. 已被证伪的方向（不要回头）

来源：`docs/module-02-backtest-findings.md` 第五节、`docs/factor_cheatsheet.md` 第四节（历史口径，但方向性结论仍应尊重）

- ❌ 四维综合评分 / 简单因子堆叠 → 不提升预测力
- ❌ 量价确认（放量上涨）做多 → A 股行业层面 **IC 为负**，方向相反
- ❌ 动量 / 低波 / 技术突破 / 资金流 / 质量 / 成长类横截面因子 → IC 接近 0 或反向
- ❌ "牛市放大动量信号" → 实际是**熊市放大一切有效信号**（因为稀缺）
- ❌ **用 IC 评估条件信号** → 稀疏信号会被零值稀释，必须用事件研究（这正是台账的用途）
- ❌ 把"阶段识别"直接当交易信号 → 旧模块二的"生命周期全景"子模块当年就因此被删除过

---

## 5. Agent 操作须知

### 5.1 命令清单

```bash
cd ~/quant/industry-radar        # 所有脚本从仓库根运行；解释器 = 系统 python3

# 日更全链（一般由 launchd 自动执行）
bash scripts/run_daily_scheduled.sh
python3 cron/industry_review_daily.py              # 单跑编排入口（含完备性检查与台账入账）

# 数据 / 报告
python3 scripts/build_members_daily.py             # 每日成分快照（新交易日必须跑，否则静默跳过）
python3 scripts/check_data_completeness.py         # 数据完备性（可单跑，退出码 0/1/2/3）

# 信号台账（P0）
python3 scripts/build_signals_log.py --backfill-cold-zone   # 首次全历史回填（1~3 分钟）
python3 scripts/build_signals_log.py                        # 日更快照 + 回填收益
python3 scripts/evaluate_signals.py                         # 事件研究评估
python3 scripts/evaluate_signals.py --source cold_zone --horizons 5,10,20,60
```

### 5.2 修改约定（重要）

1. **不要改动 `module_02` 的报告口径**；需要加内容就在 `src/daily_review/report_sections.py` 里追加章节（保证与旧链路逐字节可比）。
2. **任何新信号必须入台账**，并在 `docs/signal-ledger.md` 记录首轮评估结果。没有台账数字的新信号等于没有证据。
3. 报告与台账的更新**必须包在 try/except 里**，失败不能影响主报告生成（现有代码即如此）。
4. 路径一律走 `src/paths.py`；新增数据目录请在那里集中登记（如已登记的 `SIGNALS_DIR` / `SIGNALS_LOG_PATH`）。
5. 改口径（宽度定义、加权方式、基准）必须**同步更新 docs**，并重跑台账评估——否则新旧结论不可比。

### 5.3 已知陷阱

| 陷阱 | 说明 |
|---|---|
| `detect_signals` 的列名 | `factor_width_reversal.detect_signals` 内部按 **`con_code`** 分组，传入的 DataFrame 必须保留该列名（`industry_daily_full.parquet` 即用此名） |
| 窗口长度 | 该函数要求每个行业在信号日之前有 ≥20 个交易日历史；回填时需传入足够窗口（台账用 60 个交易日窗口 + 全量日期索引定位） |
| `industry_daily_full.parquet` 被截断 | 冷区**历史回填**依赖该文件的完整历史；若日更任务改为只保留近 N 天且未先回填，历史将不可重建（台账本身是持久留痕，已落盘 2234 条） |
| 拥挤度是**个股级** | `factors/ind_crowd_turnover_daily.parquet` 列为 `date, code, value`，行业口径需按当日成分聚合（中位数）；`module_02` 的 `aggregate_industry_crowding` 与台账 `sources._load_crowding_panel` 均为中位数口径 |
| 报告文件写入语义 | `module_02.run()` 是**全量重写**文件；追加章节发生在之后（`append_to_report`）。因此重复运行不会重复追加，但**任何对报告的手工编辑都会被下一次运行覆盖** |
| 非交易日 | `module_02.run()` 提前返回且**不落盘**；此时追加章节与台账快照会自动跳过（已处理） |
| 沙箱写权限 | 数据根 `/Users/hyc/quant-data` 在**会话工作区之外**：受 sandbox 限制的 agent 写入需申请提权；仓库内写入不受限 |

### 5.4 验证方法（本仓库实践过的，优先复用）

- `bash -n <script>` / `python3 <script> --help`：语法与启动自检（成本极低，先跑这个）
- `DRY_RUN=1 bash 每日行业报告.command`：只打印将执行的命令，不计算不写数据
- **只读渲染**：直接调用渲染函数打印，不落盘（如 `build_extra_sections('2026-09-22')`）
- **幂等检查**：同一命令连跑两次，看记录数/文件内容是否变化（台账已实测 2256 → 2256）
- **口径校验**：新产物与已知真相比对（如回填出的冷区信号需与当日日报中的信号一致）

---

## 6. 数值基线（2026-09-23，用于回归检测）

**数据**：131 个行业、1632 个交易日（2020-01-02 ~ 2026-09-23）；收益面板 1632×131、**零缺失**。

**台账**：2,256 条 = cold_zone 2,234 + tracker_pool 21 + mainline 1。

**cold_zone 冷区信号（超额口径）**：

| 项 | 数值 |
|---|---|
| 20 日 | +0.31pp / 胜率 49.9% / t=2.25 |
| 60 日 | +0.67pp / 胜率 47.6% / t=2.61 |
| high 档（<10%，N=252） | +1.21pp / 55.2% / t=2.86 |
| Q1 分位（score≤16.1，N=749） | +0.67pp / 53.7% / t=2.82 |
| 分年 | 2022 +1.33(t=4.06)、2026 +0.90(t=1.95)、2020 +0.60、2023 +0.05、2024 −0.15、2025 −0.19、2021 −0.30 |
| 按 regime | 全面上涨 +0.58(t=2.71)、混合 +0.28、偏弱轮动 +0.27、轮动 +0.03、全面下跌 −0.32 |

**regime**：全面上涨 458 / 全面下跌 419 / 轮动 388 / 偏弱轮动 198 / 混合 169；**结构性 0**；`rank_consistency` 均值 −0.246。

**报告**：`2026-09-23_industry_review.md` 3,657B（module_02 三板块 + 追加三章节）。

> 任何改动后若上述数字显著变化，先确认是**数据变化**还是**行为回归**。

---

## 7. 证据索引（快速核查用）

| 结论 | 核查位置 |
|---|---|
| 24 个手工常量、四阶段注释 | `scripts/industry_tracker.py:25-57`、`:38` |
| 冷区 v3 砍掉三维度、只在第 3 天触发 | `src/daily_review/factor_width_reversal.py:21-24`、`:96-98` |
| 旧方案 4 维度 2–13 分评分 | `docs/module_02_rebuild.md:45-67` |
| 旧回测数字（+2.02%/62%、+9.06%/84%、子阶段调制） | `docs/module-02-backtest-findings.md` 一/二/四/八节 |
| 已证伪清单 | `docs/module-02-backtest-findings.md` 五节、`docs/factor_cheatsheet.md` 四节 |
| 宽度双口径警告 | `docs/module_02_rebuild.md:24` |
| 新股首日极值事故 | `docs/validation-2026-09-21.md:34` |
| 台账实测数字 | `signals/eval_20260923.md`、`docs/signal-ledger.md` 四节 |
| 中区/热区占位 | `src/daily_review/module_02_industry.py:593-605` |
| 报告追加逻辑 | `src/daily_review/report_sections.py`、`cron/industry_review_daily.py` 末尾 |
| 1 个行业的主线 | `industry/mainline_detection.csv`（group_id=18，n_industries=1，is_mainline=True） |
| 完备性检查规则表 | `README.md`「数据完备性检查」节 |

---

## 附：本报告的核实程度说明

| 类别 | 内容 |
|---|---|
| ✅ **本会话实测**（可直接引用） | 台账 2234 条冷区信号的完整统计；regime 全历史分布与 `rank_consistency`；signal 稀疏度（09-22/23 零信号）；数据 schema 与零缺失；cron 幂等性；报告字节数；24 个常量；33 个 research 脚本 |
| 📄 **文档记载**（我读到但**未复现**） | 旧回测数字（+2.02%/62%、+9.06%/84%、IR 1.15~2.43、热区 +3.01%/65%）、`wake_backtest_results.csv` 的 n=95 为文件首行所载 |
| ⚠️ **未验证** | 现网是否已处理新股首日极值；`rank_consistency` 失效的具体成因（符号错误还是数据口径）；中区/热区信号在当前数据上的真实表现 |
