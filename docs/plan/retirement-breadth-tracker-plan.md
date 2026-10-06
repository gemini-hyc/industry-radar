# 弃用「宽度预测 + 跟踪池」清理计划

> **决定**（2026-10-06，主人）：宽度已证伪 ⇒ **弃用其预测用途**；`tracker_pool` 装置已失效 ⇒ **一并丢弃**。
> **报告口径铁律解除**（原 §5.2 第 1 条不再适用），但**第 2 条（新信号必须入台账）继续有效**。
> **本页状态**：🟡 **第 0~4 批全部执行完毕**（25 条状态列已就地更新）· **仅剩第 5 批（数据根 9 个文件删除）待源端执行或主人授权**
> **验收（2026-10-06 收尾）**：`make check` **64/64 全绿**（基线 63/64，唯一红项由 `TOLERATED_MISSING` 白名单修复）｜6 组同源 A/B 逐值一致｜沙箱日报 1802 B、禁词零残留｜数据根 `-newer` 守卫零写入｜`--audit-writes` 中 `tracker_report` 已消失
> **配套审查**：[`retirement-breadth-tracker-review.md`](../archive/retirement-breadth-tracker-review.md)（另一路会话的可行性审查，本页已吸收其 4 条必补、5 项建议、6 项事实订正；其中 2 项经实测推翻并已在本页订正）
> **裁决台账**：`docs/indicator-status-register.md`

---

## 0. 总则

### 0.1 三条不可动的原则

| 原则 | 内容 |
|:--|:--|
| **⛔ 宽度字段不删** | `breadth` / `breadth_ma13` 保留计算，理由见 §6。与预测力无关。 |
| **⛔ 台账历史不删行** | 2,255 行标`retired`，理由是可追溯（§5.3）。 |
| **⛔ 数据根操作不在本机做** | 本机是只读副本（`com.quant.data-pull` 已暂停），第 5 批须源端执行或明确授权。 |

### 0.2 批次顺序的依赖逻辑

```
第 0 批  建立可信基线（分清"我改红的"与"本来就红的"）
   ↓
第 1 批  断引用（不删文件）── 可回滚，先证明链路能跑通
   ↓
第 2 批  删代码 ── 前提是全仓零残留引用
   ↓
第 3 批  摘登记 + 加retired 列 ── 改schema，须同步 4 处
   ↓
第 4 批  归档文档（不移动 research/）
   ↓
第 5 批  数据根操作 ── 源端 / 需授权
```

**为什么不能先删文件**：`factor_width_reversal.py` 被 `sources.py:184` 在**函数体内惰性 import**，
今天不报错只是因为没人调用它。先删文件会留下一个"内含失效 import 的死函数"——正是第 2 批前置条件要防的东西。

---

## 1. 第 0 批 · 建立可信基线

**动机**：`make check` 当前有 **1 项既有失败**（`mainline_detection.csv` 缺 `is_mainline_raw`/`consec_days`/`min_consecutive_days`，本机旧版产物）。若不先处置，**分不清"清理改红的"与"本来就红的"**，第 1~4 批的验收全部失效。

| # | 事项 | 处置 | 状态 |
|:--|:--|:--|:--:|
| 0.1 | `mainline_detection` 缺 3 列 | **不做放宽断言**（铁律：`EXPECTED_COLUMNS` 是唯一真相来源，放宽等于掩盖 schema 变更）。两个选项留给主人：**①A** 源端重跑生成新产物后重验；**①B** 在 `tests/README.md` 标注为"预期红·待源端同步"，并在 `make check` 输出里显式提示。 | ✅ 走 ①B（主人拍板①）：新增 `TOLERATED_MISSING` 白名单，只豁免这 3 列，其余缺/多仍硬失败 |
| 0.2 | 记录基线数字 | 跑一次 `make check`，把**失败项清单**与日报当前字节数写进本页 §7 | ✅ 基线 63/64（1 红=mainline_detection 缺 3 列）已写入 §7；本轮收尾 `make check` **64/64 全绿** |

> ⚠️ 审查文档给的参考数字：**2026-10-02** 的日报已是 **591 B**，含冷区的日子为 **2.7~4.2 KB**。
> 清理后日报会显著变短——这是**已知代价**，要用数字确认程度而不是凭感觉。

---

## 2. 第 1 批 · 断开生产链引用（不删任何文件）

本批只改引用，**目标是让链路跑通且不再出现相关内容**。全部完成后跑 `make check` + 手工跑一次日报。

### 2.1 生产链

| # | 文件:行 | 动作 | 状态 |
|:--|:--|:--|:--:|
| 1.1 | `每日行业报告.command:11` | 删头部注释里的第 5 步「贝叶斯反转跟踪」 | ✅ |
| 1.2 | `每日行业报告.command:99` | 删 `run_step "5/9 贝叶斯反转跟踪"`；后续 `6/9→5/8` … `9/9→8/8` 重编号；`:15` 注释同步 | ✅ |
| 1.3 | `cron/industry_review_daily.py:310-317` | 删 `cold_zone_history` 惰性 import与调用；`:326` 打印里的「冷区 N」计数 | ✅ |

### 2.2 台账（唯一真相来源）

| # | 文件:行 | 动作 | 状态 |
|:--|:--|:--|:--:|
| 1.4 | `src/signals/sources.py:48` | `STATUS_ORDER` 增 `"retired": 3`；`STATUS_LABEL` 增 `"retired": "🚫 已弃用"`（置 `observational` 之后） | ✅ |
| 1.5 | `src/signals/sources.py:56-88` | `cold_zone` / `tracker_pool` 的 `status` **改为 `retired`**（保留条目，只改状态），`status_since` 改弃用日，`status_short` 改写为已证伪结论 | ✅ |
| 1.6 | `src/signals/sources.py:289` | 删 `_snapshot_tracker` | ✅ 同批另删 `cold_zone_history` / `_margin_pct_or_none` / `_load_crowding_panel`（§2.3）；**保留 `_load_industry_names`/`_num`/`_parse_group_list`**（`_snapshot_mainline` 在用，审查文档误判） |
| 1.7 | `src/signals/sources.py:361-366` | `snapshot_signals` 默认 `sources` 去掉 `tracker_pool` | ✅ |
| 1.8 | `scripts/build_signals_log.py:62-64` | 删 `--backfill-cold-zone` 分支与 `:21` 的 import | ✅ |

> 🔴 **1.4/1.5 为什么是"标 retired"而不是"删条目"**：`evaluate.py` 是按 source 查 `SOURCE_SPECS` 渲染的
> （`spec.get('label','')` / `spec.get("status","pending")`）。删条目 ⇒ 那 2,255 行会被渲染成
> **「⏳ 待观察」+ 空标签**——而"待观察"暗示"再等等就有结论"，**恰是本次清理要终结的误解**。
> 标 `retired` 后：历史行仍有正确标签、评估报告自动出现「已弃用」分组、`tests/_common.py:136` 的
> `LEDGER_VALID_SOURCES` **不必改**（历史 source 仍合法）。

### 2.3 报告层

| # | 文件:行 | 动作 | 状态 |
|:--|:--|:--|:--:|
| 1.9 | `module_02_industry.py:7` | 改文件头「冷区宽度反转因子 → **唯一已验证有效的信号**」—— **这句话现在是错的** | ✅ |
| 1.10 | `module_02_industry.py:61-72` | 删 `load_tracker_state`（实测全文无调用点，死代码） | ✅ |
| 1.11 | `module_02_industry.py:381-471` | 删 `format_cold_zone_section` | ✅ |
| 1.12 | `module_02_industry.py:590-611` | 删 `run()` 里的冷区检测块（`detect_signals` 调用 + `market_breadth` 计算） | ✅ |
| 1.13 | `module_02_industry.py:473-505` + `:628-631` | 删 `_build_m02_conclusion_json_v2` 与 `_write_m02_conclusion_json` 及调用 —— 🔴 见 §2.5 | ✅ |
| 1.14 | `module_02_industry.py:614-624` | **章节重编号**：「二、中区（待实现）」「三、热区（待实现）」→「一、」「二、」，并决定是否保留两个空占位 | ✅ 中/热区**不留空占位**（主人拍板⑤）→ 直接删除，编号重排为「一、主线状态」「二、行情 regime」 |
| 1.15 | `report_sections.py:77-134` | 删 `format_tracking_section` | ✅ |
| 1.16 | `report_sections.py:236-302` | 删 `format_margin_section`（冷区信号资金面参考） | ✅ |
| 1.17 | `report_sections.py:304-382` | 删 `format_width_state_section` | ✅ |
| 1.18 | `report_sections.py:385-398` | `build_extra_sections` 只保留主线 + regime 两节 | ✅ |
| 1.19 | `report_sections.py:34,39` | 删 `TRACKER_REPORT_PATH` 常量与 `SYNC_SCORE_MIN`（`:39`，唯一使用方是已删的跟踪池章节） | ✅ |

### 2.4 测试同步（🔴 必补，否则 `make check` 以新方式变红）

| # | 文件:行 | 动作 | 状态 |
|:--|:--|:--|:--:|
| 1.20 | `tests/_common.py:80` | 删 `EXPECTED_COLUMNS["tracker_report"]` | ✅ |
| 1.21 | `tests/_common.py:53-84` | 删 `UNDER_TEST` 的 `tracker_report` 键 | ✅ |
| 1.22 | `tests/test_invariants.py:184-188` | 删 `tracker_report.code == 131 行业全集` 断言 | ✅ |
| 1.23 | `tests/snapshot.py:56,108` | 删 `tracker_report` 的字段采集与 `by_status` 分支 | ✅ |
| 1.24 | `tests/selftest.py:55` | 沙箱 `NEED` 清单移除 `tracker_report.csv` | ✅ |
| 1.25 | `tests/test_eval_inference.py:88` | 该处用 `source="cold_zone"` 作 `obs_type="event"` 的**样本标签**（纯造数，不读产物）⇒ **可保留**；若想语义干净可改名为 `demo_event` | ✅ 保留（纯造数样本标签，不读产物）—— 语义不敏感，不改 |

> ⚠️ **实测修正**：审查文档说"删文件不会让测试报错，`exists()` 为假即跳过"。**部分正确**——
> `test_invariants.py` 各处确实有 `exists()` 保护（`:114/:132/:146/:179/:203`），
> 但 `tests/_common.py:67` 有一条**缺失即报**的汇总检查。
> **真正必须改的是 `_common.py` 的登记**：一旦从 `UNDER_TEST` 删键，
> `test_invariants.py:184` 的 `UNDER_TEST["tracker_report"]` 就是 **KeyError**（不是 skip）。

### 2.5 🔴 连带：module_02 的结论 JSON 应整体摘除

`_build_m02_conclusion_json_v2` 的唯一消费者是 `report_sections.py:260`
（读 `cold_zone_signals` 字段），而该章节在 1.16 被删。
⇒ 删掉 `cold_zone_signals` 后，JSON 只剩 `mid_zone: None` / `hot_zone: None`，
**是一个永远为空、却每天产出的壳**。故**连带把整个 JSON 输出一起摘掉**（1.13）。

### 2.6 第 1 批验收

- [ ] `make check` 的失败项与第 0 批基线**完全一致**（无新增）
- [ ] 手工跑一次日报：`python3 cron/industry_review_daily.py --date <最新交易日>`
- [ ] 报告中**不再出现**：冷区、宽度反转、反转跟踪、贝叶斯、宽度状态、两融分位参考
- [ ] 记录日报字节数，与第 0 批基线对比（记录"变空"的程度）
- [ ] `git status` 确认**零删除**（本批只改引用）

---

## 3. 第 2 批 · 删除代码文件

**前置条件**：第 1 批完成，且以下 grep 零命中：

```bash
grep -rn "industry_tracker\|factor_width_reversal\|sync_industries_to_futu\|\
cold_zone_history\|_snapshot_tracker\|tracker_report\|module_02_conclusion" \
  --include=*.py --include=*.sh --include=*.command .
```

| # | 文件 | 行数 | 动作 | 状态 |
|:--|:--|--:|:--|:--:|
| 2.1 | `scripts/industry_tracker.py` | 996 | 删整个文件 | ✅ |
| 2.2 | `src/daily_review/factor_width_reversal.py` | 207 | 删整个文件 | ✅ |
| 2.3 | `src/signals/sources.py:161-287` | ~127 | 🔴 **同批删 `cold_zone_history`** 及其专用辅助（`_load_crowding_panel`、`_margin_pct_or_none`、`_parse_group_list`）。**必补三**：该函数体内有对 2.2 的惰性 import，只删文件会留下死函数 | ✅ 实测修正：`cold_zone_history` 已删；但 `_parse_group_list`/`_load_industry_names` **被 `_snapshot_mainline` 使用，保留**（审查文档误判为冷区专用） |
| 2.4 | `scripts/sync_industries_to_futu.py` | 164 | ⚠️ **待拍板**（见 §7第2 项） | ⏸️ 保留下（主人拍板） |

### 3.1 🔴 必补四：通用统计原语收敛为**一份**

现状实测，`nonoverlap_t` / 聚类 t 已有**两份**，再加一层就是三份：

| 位置 | 函数 |
|:--|:--|
| `src/signals/evaluate.py:80` | `cluster_t` |
| `src/signals/evaluate.py:92` | `nonoverlap_t` |
| `src/research/breadth_eval.py:113` | `nonoverlap_t` |
| `src/research/breadth_eval.py:126` | `cluster_se`（聚类稳健 OLS + 交互项，唯一实现） |
| `src/research/breadth_eval.py:183` | `ts_quantile`（内建 `shift(1)` 前视防护） |
| `src/research/breadth_eval.py:258` | `tail_concentration`（均值假象诊断） |
| `src/research/breadth_eval.py:305` | `nonoverlap_t_sorted` |

**动作**：新建 `src/evaluation/` 作为**唯一统计口径层**——
① `nonoverlap_t` / `cluster_t` **只保留一份**，`evaluate.py` 与 `breadth_eval` 改为调用它；
② `cluster_se`（回归 + 交互项，evaluate.py 完全没有的能力）迁入；
③ `tail_concentration` / `ts_quantile` 迁入；
④ **删除**三个宽度专用函数：`build_panel`、`breadth_forward_vol_ic`、`breadth_forward_return_ic`。

> **为什么值得做**：本项目 24 小时内在"前瞻窗口"上翻过三次车，每次结论都变。
> 再写第三份聚类 t 是**纪律上的自我拆台**。新体系第 3 层（景气+估值检验）必然要用回归与交互项。
> ⚠️ 但**不要在本批做大规模重构**——只做"收敛为一份"，报告层 400 行不动。

**零依赖确认**：`breadth_eval` 只依赖 `numpy`/`pandas` + 自家 `forward_sum`，**不需要 scipy** ⇒ 抽层不引入新依赖。

---

## 4. 第 3 批 · 产物登记与台账 schema

| # | 项 | 动作 | 状态 |
|:--|:--|:--|:--:|
| 3.1 | `src/paths.py:85` | 摘掉 `tracker_report` 登记（`tracker_state.json` 本就**未登记**，一并确认无需处理） | ✅ `tracker_report` 已摘除；`--audit-writes` 复核零命中 |
| 3.2 | 台账 `retired` 列 | 见 §4.1 | ✅ **偏离原建议**：只改 `SOURCE_SPECS`，不加台账列（理由见 §4.1） |

### 4.1 ✅ `retired` 状态：**只改 `SOURCE_SPECS`，不加台账列**（⚠️ 偏离原建议，见下）

**本节偏离了审查文档 §7 与本计划初稿**：两者都建议给 `signals_log.parquet`
加 `retired_at` / `retired_reason` 两列。**实测后决定不做**，理由：

1. **`SOURCE_SPECS` 已经是唯一真相源**（`src/signals/sources.py:56`）。
   把同一状态复制进台账 = **制造第二个真相源**，两者必然漂移
   （改一处忘另一处 ⇒ 又一次口径事故）。
2. **不需要**：`evaluate.py` 按 `source` 查 `SOURCE_SPECS` 渲染，
   `status="retired"` 一改，评估报告**自动**正确显示（已实测验证）。
3. **零schema 变更** = 零连带改动：不必改 `ledger.ALL_COLS` /
   `EXPECTED_COLUMNS["signals_log"]` / 重跑 `snapshot.py --update`，
   也就不会因快照基线变动产生新的红。

**实际做法**（已落地并验证）：

| # | 位置 | 改动 | 状态 |
|:--:|:--|:--|:--:|
| 4.1.1 | `sources.py:STATUS_LABEL/STATUS_ORDER` | 新增 `retired`（排在 `observational` 之后） |✅ |
| 4.1.2 | `sources.py:SOURCE_SPECS` | `cold_zone`/`tracker_pool` 改 `status="retired"`，**保留条目** |✅ |
| 4.1.3 | `evaluate.py` 台账概览 | **在册 / 已弃用分组呈现** |✅ |

**实测效果**：`mainline` 2 条列入「在册来源」；`cold_zone` 2,234 + `tracker_pool` 21
列入「🚫 已弃用来源（数字仅供追溯，**不可作决策依据**）」，并附合计与弃用清单。

> **为什么分组是必需的而不是锦上添花**：混排时读者会把弃用来源的漂亮历史数字
> 误当成在册证据—— 冷区 2,234 行里均值 **+0.34pp** 看着像 alpha，
> 而它**中位数是 −0.13pp**（均值假象）。分组后「已弃用」标题自带警告。

### 4.2 台账数字（写清以免误读）

| 来源 | 行数 | 处置 |
|:--|--:|:--|
| `cold_zone` | 2,234 | 标 `retired`（2020-02-07~ 2026-09-21，**502 个信号日**，七年证据） |
| `tracker_pool` | 21 | 标 `retired`（2026-09-14~09-22，5 个信号日） |
| `mainline` | 2 | **保留在册** |
| **合计** | **2,257** | 待弃用 **2,255**，保留 **2** |

---

## 5. 第 4 批（文档）与第 5 批（数据根）

### 5.1 第 4 批 · 文档处置（✅ 已执行）

| 动作 | 对象 | 说明 | 状态 |
|:--|:--|:--|:--:|
| **归档** → `docs/archive/` | `changes-2026-10-01-cold-zone-ab.md`、`-downgrade.md`、`-event-oos.md`、`-recalib.md`、`breadth-what-it-can-and-cannot-do.md`、`tracker-design-and-issues.md`、`backtest_width_reversal.md`、`retirement-breadth-tracker-review.md` | 归档而非删除，git 历史可追；8 篇**全部 `git mv`**（字节/行数逐篇核验一致），活文档 26 处引用路径已改写 | ✅ |
| **⛔ 不移动** | `research/` 全部 33 个脚本 | `README.md` 已声明「历史回测脚本存档（**冻结，不维护**）」；且**10 个文档、18 处**引用 `research/*.py` 路径，移动后全部失效。改为新建 `research/README.md` 列出「结论已作废」映射（宽度预测力 / 冷区组合 / 反转跟踪池三组），33 个脚本**逐个 grep 可命中** | ✅ |
| **⛔ 不归档** | `docs/sentiment-data-evaluation.md` | 它评的是**情绪数据层**（两融/龙虎榜/大宗/北向/南向 7 个接口），本次弃用的只是**冷区资金面那一节**。留在 `docs/`，文首加**分条失效声明**（5 条逐项列出，含行号），同时声明情绪数据层结论**仍有效、面板仍产出可供新体系复用** | ✅ |
| **改写** | `docs/indicator-status-register.md` | 页首加弃用声明 + 新真相源指针（`src/evaluation/inference.py`）；§1 两处表格 + 文末速查同步标「🚫 已弃用（条目保留，标 `retired`）」；§2 反转跟踪标删除、宽度状态标章节删除（注明 `breadth` 列保留为探针） | ✅ |
| **改写** | `docs/industry-analysis-methodology.md` §3.5 | 冷区条件检验定稿（交互项≈0、**均值假象**）是**有效的负面结论**，不删；只把 3 处**状态描述**由「观察项」改为 `retired`（`:367` 结论 1 / `:506` 整合表 / `:522` 边界声明） | ✅ |
| **改写** | `README.md` | 5 处：三区框架退场 / 信号定位改 `retired` / 反转跟踪标删除 / 目录树删 `industry_tracker.py` + `module_02` 描述 / **产物清单删 `tracker_state.json` 并警示 `sync_industries_to_futu.py` 因此无输入** / 用法删 `industry_tracker.py` 与 `--backfill-cold-zone` 两行 | ✅ |

### 5.2 ✅ 第 4 批附带：铁律扫改（7 处全部完成）

| 位置 | 需改内容 | 状态 |
|:--|:--|:--:|
| `docs/system-audit-and-roadmap.md:355`（§5.2 **第 1 条**） | 标记为**已解除**（附主人决定与日期）。⚠️ **第 2 条「新信号必须入台账」保留有效**——审查文档指出我原计划写「第 2 条」是 off-by-one，**它对** | ✅ |
| `docs/improvement-roadmap-2026-10-04.md:279` | 标注已解除 | ✅ |
| `docs/task-prompts/session4/01-improvement-kickoff.md:113` | 标注已解除 | ✅ |
| `docs/task-prompts/session3/01-stage-b-kickoff.md:165` | 行内 `~~删除线~~（🟢 已解除）` | ✅ |
| `docs/action-items.md:127`（D10） | 行内标注；并注明 `industry_tracker.py` 已删除，只剩 `module_02` 一处 | ✅ |
| `docs/data-remediation-plan-2026-10-04.md:289` | 行内标注 | ✅ |
| `docs/stage-b-evidence-2026-10-04.md:294` | 行内标注（§8 拍板顺序表第 1 行） | ✅ |

> ⚠️ 只改 §5.2 是不够的——下个会话读到其余 6 处**仍会判定你违规**。
> 🟡 **订正审查文档的「重复一处」说法**：实测 `system-audit-and-roadmap.md` 中该铁律**只存在 1 处**
> （`grep -c "不要改动 module_02"` = 1，扫改后 = 0）⇒ 「`:355` 与正文重复」是**误判**，无第二处可扫。
> ✅ **复查**：全仓 7 处提及**均带解除标注，零漏网**。

### 5.3 第 5 批 · 数据根操作（⛔ 源端执行或明确授权）

**本机数据根是只读副本**（`com.quant.data-pull` 已暂停），本机只能写 `~/quant-data/_local_dev/`。

**删除清单（9 个）**：

| 文件 | 大小 | mtime |
|:--|--:|:--|
| `industry/tracker_report.csv` | 12 KB | 2026-09-30 |
| `industry/tracker_state.json` | 73 KB | 2026-09-30 |
| `industry/wake_abc_detail.csv` | 2.9 MB | 2026-08-01 |
| `industry/wake_pure_backtest_detail.csv` | 2.8 MB | 2026-08-01 |
| `industry/wake_backtest_detail.csv` | 39 KB | 2026-07-25 |
| `industry/wake_backtest_results.csv` | 4.8 KB | 2026-07-25 |
| `industry/wake_backtest_v2_detail.csv` | 45 KB | 2026-07-26 |
| `industry/wake_backtest_v2_results.csv` | 2.9 KB | 2026-07-26 |
| `industry/wake_tracking_state.json` | 3.0 KB | 2026-07-01 |

> ⚠️ **wake 实为 6 个 csv + 1 个 json**（审查文档订正了原计划"×5"的枚举错误，我原表述有误）；
> 加上 `tracker_report.csv` + `tracker_state.json` 才是 9 个。**总数对，枚举错**。

> 🔴 **⛔ 排除项**：`industry/mainline_tracker_backtest.csv`（399 行）名字里有 tracker，
> 但列是 `date/direction/name/heat/heat_zone/excess_ma20/stage` ⇒ **属于主线探测，不在删除范围**。

---

## 6. ⛔ 保留不动的部分（宽度作为数据探针）

| 项 | 位置 | 理由 |
|:--|:--|:--|
| `breadth` / `breadth_ma13` 列计算 | `src/utils/market_data.py:150,217,244-245`（14 处） | 字段本身，不是信号 |
| `BREADTH_ZERO` 判据 | `scripts/check_data_completeness.py:511` | 防2026-07 那种「全 131 行业宽度**精确为 0**」的静默事故——该事故长期无人发现，因为既不改文件存在性也不改行数。判据 ① 是确定性的（物理上不可能） |
| 退化自检 | `scripts/build_industry_daily_full.py:96` | 防 MA 未成型造成假零 |
| `BREADTH_COLUMNS` 断言 | `tests/test_invariants.py:50,300` | 验的是探针不是信号 |
| 自证第 ④ 例 | `tests/selftest.py:366-381` | 同上 |
| 前瞻窗口唯一实现 | `src/utils/forward.py` | 通用，非宽度专属 |

**一句话**：宽度从「指标」退回到「数据字段」，**只做哨兵，不做判断**。
这正是「持续性 ≠ 预测力」的最好例证——作为前瞻因子它是死的，作为数据探针它是活的。
判据越粗糙（全零）、越不需要口径精确，在这里反而是优点：
**信号要的是"准"，探针要的是"脏了能报警"，两者要求正好相反。**

---

## 7. 待拍板事项

| # | 事项 | 需要主人一句话的原因 |
|:--:|:--|:--|
| 1 | `mainline_detection` 那条红怎么处理 | 涉及「是否放宽断言」。按铁律不会擅自放宽 `EXPECTED_COLUMNS`；选项：源端重跑 / 标注预期红 |
| 2 | 🔴 `sync_industries_to_futu.py` 是否保留 | **它是唯一的人工富途同步入口**（`MEMORY.md` 记「手动触发」）。审查订正了我的表述：它不是「每天空跑」而是**从不运行的死代码**（全仓零引用，唯一命中是 `report_sections.py:39` 的一句注释）—— 但删它= **放弃一个人工能力**，属产品决策 |
| 3 | `retired` 状态是否采纳（§2.2/§4.1） | 动的是唯一真相源 `SOURCE_SPECS` 的状态词表 |
| 4 | 铁律扫改范围（§5.2，≥7 处） | 涉及历史交接文档，是否连同旧 session 提示一起改 |
| 5 | `module_02` 章节重编号后是否保留空占位（1.14） | 影响日报结构 |

---

## 8. 变更登记

| 日期 | 提交 | 内容 |
|:--|:--|:--|
| 2026-10-06 | — | 计划成文；吸收 `retirement-breadth-tracker-review.md` 的4 条必补 + 5 项建议 + 6 项事实订正 |

### 本计划已吸收的审查意见（逐条落地）

| 审查条目 | 落地位置 |
|:--|:--|
| 🔴 必补一 测试层缺席 | §2.4（新增 1.20~1.25），并**修正其"删文件即skip"的表述**（`_common.py:67` 缺失即报） |
| 🔴 必补二 应标 retired 而非删条目 | §2.2（1.4/1.5）、§4.2 |
| 🔴 必补三 惰性 import 死函数 | §3（2.3，与 2.2 同批） |
| 🔴 必补四 会造第三份 t | §3.1（收敛为一份） |
| 🟠 建议1 铁律 off-by-one | §5.2（**第 1 条**才是报告口径铁律，第 2 条保留） |
| 🟠 建议2 铁律传播面 ≥5 处 | §5.2（实测**≥7 处**，并发现 `system-audit` 自身重复一处） |
| 🟠 建议3 research 不必移动 | §5.1（并实测：10 文档 / 18 处引用） |
| 🟠 建议4 sentiment 文档不宜归档 | §5.1 |
| 🟠 建议5 module_02 编号 | §2.3（1.14） |
| 🟡 订正 sync_industries是死代码非空跑 | §7第 2 项 |
| 🟡 订正台账 2,257/2,255 | §4.2 |
| 🟡 订正 wake 枚举 6csv+1json | §5.3 |
| 🟡 订正 §7 已由 `80d233d` 修好 | §5.1（不要重复改） |