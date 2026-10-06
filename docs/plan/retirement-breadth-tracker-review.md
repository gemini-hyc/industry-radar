# 弃用「宽度 + 跟踪池」清理计划 · 可行性审查

> **审核对象**：主人 2026-10-06 提出的四批清理计划（断引用 → 删代码 → 摘产物 → 归档文档）
> **审核方式**：逐条对着**当前 HEAD** 核行号、核引用面、核测试依赖、核产物实况（不引旧文档）
> **审核日期**：2026-10-06
> **结论**：**骨架可行、依赖方向正确**；但有 **4 条必补**（会阻断验收或给未来埋雷）与 **5 项建议调整**。
>
> **本页状态**：⬜ 未开工（各条目执行后请就地更新状态列）
> **配套**：执行清单已按本页 §7 修订；裁决台账见 `docs/indicator-status-register.md`

---

## 0. 总评

| 维度 | 判断 |
|---|---|
| **批次顺序** | ✅ **正确** —— 「先断引用 → 再删代码 → 再摘产物 → 最后归档」符合依赖方向；且每批都留可回滚点 |
| **关键判断** | ✅ 三处判断我实测确认无误：① 先断引用后删文件；② 台账**标 retired 而非删行**（保住七年可追溯）；③ `mainline_tracker_backtest.csv` **不属于**跟踪池、须排除 |
| **行号准确度** | ✅ **抽查 10 处全部命中**（见 §6 核对表） |
| **行数** | ✅ 5 个文件行数**逐字准确**：996 / 207 / 164 / 317 / 623 |
| **主要缺口** | 🔴 **测试层完全缺席**（4 个测试文件依赖 `tracker_report`）⇒ 会让 `make check` 以新方式变红，从而**失去验证后续批次的能力** |
| **次要缺口** | 🔴 `SOURCE_SPECS` 应「标 retired」而非「删条目」（否则 2,255 行历史被误标为"待观察"）；🔴 删除顺序有一处静默耦合；🔴 通用层会造出第三份 `nonoverlap_t` |

---

## 1. 🔴 必补一：测试层在计划里完全缺席

`tracker_report.csv` 有 **4 个测试文件**在引用，而原计划第 1/3 批均未提及：

| 位置 | 依赖内容 |
|---|---|
| `tests/_common.py:80` | `EXPECTED_COLUMNS["tracker_report"]`（列契约）；`:53-84` 的 `UNDER_TEST` 含该键 |
| `tests/test_invariants.py:184-188` | 断言 `tracker_report.code` 集合 == 131 行业全集 |
| `tests/snapshot.py:56,108` | 快照采集字段（`S0/score/days`）+ `by_status` 分布 |
| `tests/selftest.py:55` | 沙箱 `NEED` 清单含 `tracker_report.csv` |

**后果**：不做这一步，第 3.1 批（摘 `paths.py:85` 登记）后 `make check` 会以**新方式**变红
（KeyError 或断言失败）—— 而 `make check` 正是验证第 2~4 批的**唯一工具**。

**建议**：在第 1 批加子项 **1.7 测试同步**——
① `_common.py` 去掉 `tracker_report` 的键与列契约；
② `test_invariants.py` 删那段 `tracker_report.code` 断言；
③ `snapshot.py` 去掉对应字段与 `by_status` 分支；
④ `selftest.py` 的 `NEED` 移除该文件。

> ⚠️ 补充：`tracker_report.csv` **文件本身**删掉不会让测试报错（`UNDER_TEST[name].exists()` 为假即跳过）；
> 真正必须改的是 **`_common.py` 的登记**——一旦从 `UNDER_TEST` 删键，`test_invariants` 的
> `UNDER_TEST["tracker_report"]` 就会 KeyError。

---

## 2. 🔴 必补二：`SOURCE_SPECS` 应「标 `retired`」而不是「删条目」

这是**设计层**问题，比行号重要：

- 第 3.3 批决定**保留** 2,255 行历史（`cold_zone` 2,234 + `tracker_pool` 21）——**这个决定是对的**；
- 但第 1.3 批要**删掉** `src/signals/sources.py` 里 `SOURCE_SPECS` 的同名条目；
- 而 `src/signals/evaluate.py` 是**按 source 查 `SOURCE_SPECS`** 渲染的
  （`spec.get('label','')` 与 `spec.get("status","pending")`）。

⇒ 中间态会把那 2,255 行渲染成「**待观察**」+ **空标签**。**这个语义是错的**：
它们是"已弃用 / 已证伪"，不是"样本不足"；而"待观察"恰好暗示"再等等就有结论"，
**正是本次清理要终结的那个误解**。

**建议**：
```python
# src/signals/sources.py
STATUS_LABEL["retired"] = "🚫 已弃用"
STATUS_ORDER["retired"] = 3                # 排在 observational 之后
SOURCE_SPECS["cold_zone"]["status"]    = "retired"   # 保留条目，只改状态
SOURCE_SPECS["tracker_pool"]["status"] = "retired"
```

收益：
1. 历史行**仍有正确标签**（满足 3.3 的可追溯目标）；
2. 评估报告自动出现「已弃用」分组，无需额外文档；
3. `tests/_common.py:136` 的 `LEDGER_VALID_SOURCES` **不必改动**（历史 source 仍合法）；
4. 比"删条目"少动一处唯一真相源。

---

## 3. 🔴 必补三：`factor_width_reversal.py` 的删除顺序有静默耦合

`src/signals/sources.py:184` 在 **`cold_zone_history()` 函数体内**惰性 import
`factor_width_reversal.detect_signals`。

原计划：
- 1.3 只删 `cold_zone` 的**规格条目** + 删 `_snapshot_tracker`，**未删 `cold_zone_history`**；
- 1.6 删掉 `build_signals_log.py:64` 与 `cron/industry_review_daily.py:315` 两个调用点；
- 第 2 批删 `factor_width_reversal.py`。

⇒ 结果是**留下一个内含失效 import 的死函数**，直接违背第 2 批的前置条件「零残留引用」。
（今天不报错仅仅因为没人调用它 —— 属于"埋雷"。）

**建议**：第 2 批删 `factor_width_reversal.py` 的**同一个提交**里，把 `sources.py` 的
`cold_zone_history` 及其辅助（`_load_crowding_panel` 等）一并删除。

---

## 4. 🔴 必补四：通用层会造出**第三份** `nonoverlap_t`

原计划「`breadth_eval.py` 保留通用原语、移到 `src/evaluation/`」——**核对属实**，
`src/research/breadth_eval.py` 里确实有：

| 函数 | 行号 |
|---|---|
| `cs_rank_ic` | `:92` |
| `nonoverlap_t` | `:113` |
| `cluster_se` | `:126` |
| `ts_quantile` | `:183` |
| `tail_concentration` | `:258` |
| `nonoverlap_t_sorted` | `:305` |

但**本会话已经存在两份**：
- `src/utils/forward.py`（前瞻窗口唯一实现）
- `src/signals/evaluate.py::nonoverlap_t`（2026-10-05 新增，提交 `80d233d`）

⇒ 再抽一层 = **三份 `nonoverlap_t`**。这正是 `breadth_eval.py` 当初立项要治的病（口径散落）。
**建议**：新 `src/evaluation/` 里 `nonoverlap_t` / `cluster_t` **只留一份**，另两处改为
re-export 或调用它；否则下一轮还会因为"用哪个 t"翻车。

> ⚠️ 另：`src/research/` 与 `tests/test_breadth_signal.py` 目前是**未跟踪**文件
> （另一路会话的成果，未提交）⇒ `git rm` 会报错，须先 `git add` 或直接 `mv` 后 add。

---

## 5. 🟠 建议调整（不阻断，但会返工）

| # | 事项 | 说明与依据 |
|:--:|---|---|
| **1** | **铁律指针 off-by-one** | 原计划写「`system-audit-and-roadmap.md` §5.2 **第 2 条**」，实际**第 1 条**才是「不要改动 `module_02` 的报告口径…在 `report_sections.py` 里追加章节」。§5.2 第 2 条是「任何新信号必须入台账」——**这条要保留** |
| **2** | **铁律传播面 ≥5 处，需一次扫改** | 除 §5.2 外还在：`docs/improvement-roadmap-2026-10-04.md:244,279,319`、`docs/task-prompts/session4/01-improvement-kickoff.md:100,113`、`docs/data-remediation-plan-2026-10-04.md:289`。只改 §5.2，下个会话读到其余 4 处**仍会判定你违规** |
| **3** | **`research/` 已经是"存档"，不必再嵌套** | `README.md:221` 明确写「research/ 历史回测脚本存档（冻结，不维护）」；且**文档里有 7 处**引用 `research/*.py` 路径，移动后全部失效。**建议不移动**，改为新增 `research/README.md` 列一张"对应结论已作废"的清单——收益相同、代价为零 |
| **4** | **`docs/sentiment-data-evaluation.md` 不宜归档** | 它评的是**情绪数据层**（两融/龙虎榜/大宗/北向/南向 7 个接口），本次弃用的只是**冷区资金面那一节**（`format_margin_section`）。`docs/indicator-status-register.md` §4 仍把它当**活的**证据 ⇒ 留在 `docs/`，文首加一句"其中冷区资金面一节随冷区弃用而失效" |
| **5** | **`module_02` 章节编号需处理** | 第 1 批后「一、冷区」整节消失 ⇒ 报告会从「二、中区（待实现）」开头。原清单未提**重编号**，需明确：重编号 or 保留空占位 |

---

## 6. 🟡 事实订正（不影响可行性，但影响文档准确性）

| 原计划表述 | 实际（本页实测） |
|---|---|
| `sync_industries_to_futu.py`「**每天空跑**」 | grep 全仓**零代码引用**（唯一命中是 `report_sections.py:39` 的一句注释）⇒ 它是**从不运行的死代码**，不是"空跑"。⚠️ 但它同时是**唯一的人工富途同步入口**（`MEMORY.md` 记"手动触发"）⇒ 删它 = **放弃一个人工能力**，属产品决策，建议先确认 |
| 「台账 2,255 行标 retired」 | 台账**总行数 2,257**；**待弃用 2,255**（`cold_zone` 2,234 + `tracker_pool` 21），**`mainline` 2 行保留**。文档里要写清，否则会被读成"删完只剩 0 行" |
| 「删 9 个孤儿产物文件 … `wake_*.csv ×5`」 | wake 实为 **6 个 csv + 1 个 json**：`wake_abc_detail` / `wake_backtest_detail` / `wake_backtest_results` / `wake_backtest_v2_detail` / `wake_backtest_v2_results` / `wake_pure_backtest_detail` + `wake_tracking_state.json`；再加 `tracker_report.csv` + `tracker_state.json` 才是 9。**总数对，枚举错** |
| `indicator-status-register.md` §7 要"订正过时说法" | ✅ **本会话已完成**（提交 `80d233d`）：§7「评估器朴素 t」已由 🔴 阻塞项改为"已修"。第 4 批基于最新 HEAD 时**不要再改一遍** |

### 6.1 行号核对表（抽查 10 处，全部命中）

| 计划引用 | 实测 |
|---|---|
| `每日行业报告.command:11`（头部注释第 5 步） | ✅ `:11` = `#    5. scripts/industry_tracker.py              贝叶斯反转跟踪` |
| `每日行业报告.command:99`（第 5 步执行） | ✅ `:99` = `run_step "5/9 贝叶斯反转跟踪" …` |
| `cron/industry_review_daily.py:310-326` | ✅ `:310` 惰性 import、`:315` 调 `cold_zone_history` |
| `module_02_industry.py:61` `load_tracker_state` | ✅ `:61` |
| `module_02_industry.py:381` `format_cold_zone_section` | ✅ `:381`（`:473` 为 `_build_m02_conclusion_json_v2`） |
| `module_02_industry.py:7` 头注「唯一已验证有效的信号」 | ✅ `:7` |
| `report_sections.py:77` `format_tracking_section` | ✅ `:77` |
| `report_sections.py:236` `format_margin_section` | ✅ `:236` |
| `report_sections.py:304` `format_width_state_section` | ✅ `:304`（`build_extra_sections` 在 `:385`） |
| `build_signals_log.py:62-64` 回填分支 | ✅ `:62-64` |
| `paths.py:85` `tracker_report` 登记 | ✅ `:85` |

---

## 7. 🟠 `retired` 列（3.3）的连带改动清单

给台账加 `retired_at` / `retired_reason` 会**改 `signals_log.parquet` 的 schema**，
必须同步 **4 处**，否则 `make check` 红：

| # | 位置 | 改什么 |
|:--:|---|---|
| 1 | `src/signals/ledger.py` | `ALL_COLS` / `_normalize` 加两列 |
| 2 | `tests/_common.py` | `EXPECTED_COLUMNS["signals_log"]` 加两列 |
| 3 | `tests/snapshot.py` | 数值快照基线需 `--update` |
| 4 | `src/signals/evaluate.py` | 加过滤 + 概览区分「在册 / 已弃用」 |

⚠️ **顺序**：3.3 必须**先于或同步于** 1.3（动 `SOURCE_SPECS`），否则中间态的评估器
会把历史来源误标（见 §2）。

---

## 8. 修订后的批次表

| 批次 | 内容 | 与原计划的差异 |
|:--:|---|---|
| **0** | **先给 `make check` 一个干净基线**：决定 `mainline_detection` 那条红怎么处理（标注预期红 / 源端重跑） | 🆕 否则分不清"我改红的"与"本来就红的" |
| **1** | 断引用 **+ 1.7 测试同步（§1）** **+ `SOURCE_SPECS` 改标 `retired` 而非删（§2）** | +2 项 |
| **2** | 删代码 **+ `cold_zone_history` 同批删（§3）** **+ 通用原语先落 `src/evaluation/` 并三处收敛为一份（§4）** | +2 项 |
| **3** | 产物与登记 **+ `retired` 列及其 4 处连带改动（§7）**（源端执行） | +连带清单 |
| **4** | 归档 —— **不移动 `research/`**、**不归档 `sentiment-data-evaluation.md`** | −2 项 |

**验收方式**（认同原计划，补一条）：
- 每批跑 `make check` 全绿（第 0 批之后才有意义）；
- 第 1 批结束**手工跑一次日报**，并**贴出实际字节数与章节列表** —— "报告变空"是本计划的已知代价，
  要用数字确认程度（参考：`2026-10-02` 的报告已是 **591 B**，含冷区的日子为 **2.7~4.2 KB**）；
- 第 3 批的数据根操作**由源端执行或明确授权**，不在本机擅自删数据（本机数据根是只读副本）。

---

## 9. 待拍板事项（开工前需要主人一句话）

| # | 事项 | 为什么需要你 |
|:--:|---|---|
| 1 | **`mainline_detection` 那条红怎么处理** | 涉及"是否放宽断言"——按铁律我不会擅自放宽 `EXPECTED_COLUMNS`；也可以选择源端重跑 |
| 2 | **`sync_industries_to_futu.py` 是否保留** | 它是唯一的人工富途同步入口；删掉=放弃一个能力，属产品决策 |
| 3 | **`retired` 状态是否采纳**（§2 的方案） | 动的是唯一真相源 `SOURCE_SPECS` 的状态词表 |
| 4 | **铁律扫改范围**（§5-2 的 ≥5 处） | 涉及历史交接文档，是否连同旧 session 提示一起改，由你定 |

---

## 附录 · 本页结论的核对方法（可复现）

```bash
# 引用面（"零残留"的判据）
grep -rn "industry_tracker\|factor_width_reversal\|sync_industries_to_futu\|cold_zone_history\|\
_snapshot_tracker\|tracker_report\|module_02_conclusion" --include=*.py --include=*.sh --include=*.command .

# 测试依赖
sed -n '178,195p' tests/test_invariants.py
grep -n "UNDER_TEST = \|tracker_report\|LEDGER_VALID_SOURCES" tests/_common.py tests/snapshot.py tests/selftest.py

# 通用原语是否真存在
grep -n "^def \|^class " src/research/breadth_eval.py

# 数据根实况
ls -la /Users/hyc/quant-data/industry/ | grep -iE "wake|tracker"

# 铁律原文
sed -n '/^### 5.2/,/^### 5.3/p' docs/system-audit-and-roadmap.md
```

**未经核对、仅采信原计划的部分**：第 3 批"源端执行"相关的同步链路细节（本机无法复核）。
