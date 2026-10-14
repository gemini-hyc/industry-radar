# 审核报告：《链版主线接进日报 · 实施方案》

> 审核对象：`docs/plan/chain-mainline-report-switch-plan-2026-10-12.md`
> 审核日期：2026-10-12
> 审核方式：**逐条核对方案依赖的代码事实**（不采信方案自述，全部回到源码/产物复核）
> 审核范围：可行性 + 建议合理性
> 🔒 本报告不改任何生产代码，仅出结论。

---

## 0. 审核结论

**有条件批准**。方案的核心判断**经实测成立**——两份产物 schema 近同、切换本质是「列名归一 + 换路径」，改动面确实集中在 2 个文件。但方案存在：

- **2 处必须补的遗漏（P0）**——不补则 S2 会「白做」或踩 `cold_zone` 同款坑；
- **1 处未被识别的架构风险（P1）**——切换后日报会同时出现两个「链」章节，链集合还不同；
- **4 处需更正的表述（P2）**——不影响可行性，但会误导执行者低估工作量。

⇒ 建议：**补齐 P0 + 明确 P1 + 更正 P2 表述后再执行 S1**。

---

## 1. 逐条核实：方案断言哪些属实

| # | 方案断言 | 核实 | 证据 |
|:--|:--|:--:|:--|
| 1 | 两份产物 17 列同名同义、差异仅主键 + 3 共振列 | ✅ **属实** | 实测 19 / 22 列；同名 17 列逐字一致；仅旧版 `group_id/group_names`、新版 `chain_id/chain_names` + `resonance_adj_today/resonance_z_today/style_share_today` |
| 2 | `report_sections.py:33` `MAINLINE_PATH` 写死旧版 | ✅ 属实 | 第 33 行 |
| 3 | `cron/industry_review_daily.py:270` 调 `build_extra_sections` | ✅ 属实 | `:270` import、`:272` 调用；入口确为 `report_sections.py:275` |
| 4 | `sources.py:36 MAINLINE_PATH` + `_snapshot_mainline:162` | ✅ 属实 | 第 36 行常量、第 162 行函数 |
| 5 | 台账 `mainline` 仅 5 行（总 2260） | ✅ 属实 | 实测 `cold_zone 2234 / tracker_pool 21 / mainline 5` |
| 6 | `research/mainline_memory_probe*.py` 存在且读旧版 | ✅ 属实 | 3 个文件：`mainline_memory_probe.py` / `_ab.py` / `_stage2.py` |
| 7 | `chain_names` 与 `group_names` 同命名空间 | ✅ 属实 | 均为申万二级行业名（新版样例 `['玻璃玻纤','工业金属',…]`） |
| 8 | `tests/_common.py:180 LEDGER_VALID_SOURCES` | ✅ 属实 | 第 180 行 |
| 9 | `tests/test_invariants.py:385` 旧版结构检查 | ✅ 属实 | `TestMainlineArtifact.test_structure` |
| 10 | `baseline_snapshot.json` 明示「不作失败判据」 | ✅ 属实 | `_meta.note` |

**小结**：方案没有编造事实，主要断言全部可复核。§1 的「schema 近同」是全案最关键的判断，**成立**。

---

## 2. 必须补的遗漏（P0）

### P0-1 · S2-A 漏登记 `SOURCE_SPECS`，会踩 `cold_zone` 同款坑

- **证据**：`src/signals/evaluate.py:301 / :318 / :357` 一律 `spec = SOURCE_SPECS.get(src, {})`。对**未登记**的 source，`status` 回落 `"pending"`、`label` 为空串、`status_short` 为 `"—"`。
- **后果**：S2-A 若只按方案补 `LEDGER_VALID_SOURCES` 而**漏了 `SOURCE_SPECS`**，评估报告会把新来源 `chain_mainline` 渲染成「⏳ **待观察**」+ 空标签——而它其实**定位已定型**（链版是方法论认定的正确对象），语义完全错。
- **且这条是仓库自己写明的硬要求**：`tests/_common.py:179` 注释原文——

  > 台账合法 source（新增信号来源须同步此表 **+ `src/signals/sources.py` 的 SOURCE_SPECS**）

  方案 §2 行 5 只写了 `LEDGER_VALID_SOURCES`，**漏掉「+ SOURCE_SPECS」这半句**。
- **修法**：S2 增加「`sources.py::SOURCE_SPECS` 新增 `chain_mainline` 条目（label / score_hint / direction / status / status_since / status_short / status_note 七字段齐备）」。

### P0-2 · `snapshot_signals` 默认 `sources` 未提，改法不落地

- **证据**：`sources.py:205` 默认签名 `sources: Sequence[str] = ("mainline",)`；两个调用方 **都不传参**——
  - `cron/industry_review_daily.py:316` → `snapshot_signals(trade_date)`
  - `scripts/build_signals_log.py:62` → `snapshot_signals(obs_date)`
- **后果**：即使写好 `_snapshot_chain_mainline()`，只要**默认元组不改**、调用方仍不传参，两个入口都不会采集链版行 ⇒ **台账里永远没有 `chain_mainline` 数据，S2 等于白做**。
- **修法**：默认改为 `("mainline", "chain_mainline")`，或在两个调用方显式传参。方案 §3 S2-A 写的是「`snapshot_signals(sources=...)` 增加该 source」——措辞含糊，落地时会漏。

---

## 3. 未被识别的架构风险（P1）

### P1-1 · 切换后日报出现**两个「链」章节**，且链集合不同

- **证据**：日报当前三节（`build_extra_sections`）——
  - 一、主线状态（**方向群探测**） ← `format_mainline_section`
  - 二、行情 regime
  - 三、链共振强度（**语义先验链 × 残差共振**） ← `format_chain_resonance_section`
- S1 把「一」的标题改成「（**产业链探测**）」后，**一和三是两个都叫「链」的章节**，但：

  | | 一、主线状态（切换后） | 三、链共振强度 |
  |:--|:--|:--|
  | 数据源 | `chain_mainline_detection.csv` | `chain_resonance_daily.csv` |
  | 链数 | **37**（方案 C 子集） | **74**（全部先验链） |
  | 指标 | 热度/超额/胜率/判定 | 残差共振 / 风格份额 / z |

- **关键**：实测两者的 `chain_id` **命名空间相同、且前者是后者的子集**（交集 = 37/37）。举例——「5GETF博时」在「一」里出现（37 之一），在「三」里也出现（74 之一）；而「TMTETF景顺」只在「三」里出现。**读者会以为两个章节是同一批链，其实一个是子集、一个是全集。**
- **后果**：日报可读性/口径一致性受损，且这是**切换后立刻可见**的变化，不是远期风险。
- **修法（二选一，建议 a）**：
  - **a.** 章节标题显式标口径：「一、主线状态（链版探测 · **37 条已验证链**）」「三、链共振强度（**全 74 条先验链**）」——标题即口径，最难误读；
  - b. 保持标题不变，在「一」文末加一行交叉说明，点明与「三」的关系（子集 / 全集）。
- 方案 §1 / §3 均未提及此点，需补。

---

## 4. 需更正的表述（P2，不影响可行性）

### P2-1 · 「渲染逻辑可以零改动复用」不实

- **实情**：`format_mainline_section` 内 `group_id` 出现 **4 次**、`group_names` **2 次**，另有 `方向群` 文案 **3 处**（1 个章节标题 + 2 个表头）⇒ **实际约 9 个改动点**。
- 这些是机械替换、**风险仍低**，但**不是「零改动」，也不是 §3 说的「改三处」**。
- **建议**：§3 更正为「约 9 处字段/文案替换」，避免执行者按「三处」低估。
- **附带**：`report_sections.py` **未 `import os`**，而方案 S1 用到 `os.environ` ⇒ 需补一行 import。

### P2-2 · §2 改动点清单不完整（影响 S3 判断）

方案 §2 列了 9 处，但实测**另有 4 处读取方**未列入：

| 遗漏位置 | 作用 | 影响阶段 |
|:--|:--|:--|
| `tests/snapshot.py:109` | baseline 快照对 `mainline_detection` 采 `n_mainline` | S3 |
| `research/observation_backfill.py:81` | 读旧版做新旧回溯对照 | S3 |
| `research/observation_snapshot.py:56` | 读旧版记账 | S3 |
| `scripts/build_signals_log.py:62` | `snapshot_signals` 第二调用方（见 P0-2） | S2 |

⇒ S3 下线旧版时，方案目前只提了「处置 `mainline_memory_probe*.py`」，**漏了 observation_* 两个脚本与 snapshot.py**。虽都是「改路径即可」，但清单不全会让下线时遗漏。

### P2-3 · §2 行 3 的行号表述

「`cron/industry_review_daily.py:270` **调** `build_extra_sections`」——`:270` 是 **import** 行，`:272` 才是调用行。措辞可精确。

### P2-4 · 顺带发现（非方案责任）

`tests/_common.py:130` 注释写「实测 **15 列**，mtime 2026-10-09」——当前产物实为 **19 列**，该注释已过期。与本次切换无关，建议随手订正。

---

## 5. 对 4 个待决策项的评价

| | 方案建议 | 审核意见 |
|:--|:--|:--|
| **Q1** S2 走 A / B | A（新 source） | ✅ **同意，且理由要加码**：B 不只是「语义混乱」——复用 `source="mainline"` 会让**新行继承 `mainline` 的 `status_note`**（「方法已被判定测错对象」），而 `evaluate.py:368` 会照这个标签渲染 ⇒ 等于给自己的新方法贴上「已证伪」标签。A 不是「更好」，而是**几乎唯一正确**。 |
| **Q2** Top5 加共振 z 列 | 加 | ✅ **可行、建议加**：链版 df 已含 `resonance_z_today`，渲染加一列即可。且 z 是**唯一跨链可比**的量（原始共振受链规模稀释）——加它正好能挡住「n=2 链原始值虚高」被误读，与报告三的既有说明一致。 |
| **Q3** S1 / S2 同批 | 同批 | ✅ 同意，但注意 S2 的「同批」成本因 P0-1/P0-2 上升（多改 2 个文件），仍可控。 |
| **Q4** 开关保留到 S3 | 保留 | ⚠️ 同意保留，但**触发一个方案自身没解决的时序风险**：方案 §3 默认 `MAINLINE_SOURCE="chain"`，而 §4 又要求「先出两份 diff 给你看再定稿」——**若默认就是 chain，合并当晚生产链就跑成了链版，与「先看 diff 再定稿」自相矛盾**。修法：默认先设 `"cluster"`，手动渲染两版 diff 送审 → 批准后再把默认改成 `"chain"`（或在生产链里显式注入 `IR_MAINLINE_SOURCE=chain`）。**「合并即生效」的时序必须写进方案。** |

---

## 6. 修订后的建议执行序

1. **出 diff（不改默认）**：本地以 `build_extra_sections(date)` **只读渲染**两版（`IR_MAINLINE_SOURCE=chain` / `cluster`），逐行 diff 送审——**不动生产、不落盘**（脚本 293 行 docstring 明示支持「只读渲染不落盘」）。
2. **改方案文档**：补 P1-1 标题口径消歧；更正 P2-1/P2-2/P2-3 表述。
3. **S1 改动**：`report_sections.py` 加 `import os` + `CHAIN_MAINLINE_PATH` + `_load_mainline_frame()` + 约 9 处渲染替换；**默认值先设 `"cluster"`**。
4. **S2 改动（补齐 P0）**：`sources.py` 新增 `_snapshot_chain_mainline()` **+ `SOURCE_SPECS["chain_mainline"]` 七字段** + 改默认 `sources` 元组；`_common.py` 补 `LEDGER_VALID_SOURCES`。
5. **批准后 flip**：默认 `"chain"`（或生产链显式注入），观察 1 日。
6. **S3**（观察后）：下线旧版，处置 observation_* / snapshot.py / memory_probe。

**新增验收项**（在方案 §5 七条之外）：
- ⑧ 评估报告里 `chain_mainline` 的「定位」列**不为「⏳ 待观察」**（验证 P0-1 已修）；
- ⑨ 报告「一」与「三」章节标题**能一眼区分链集合**（验证 P1-1）；
- ⑩ 合并当日生产产物**未变**（默认仍 cluster）——验证 Q4 时序受控。

---

## 7. 一句话总结

方案**方向正确、核心判断属实、改动面确实小**——可以批准。但它**低估了工作量（9 处 vs 三处）、漏了两个会让 S2 白做的登记项（SOURCE_SPECS / snapshot_signals 默认值）、且没发现切换后日报会出现「两个链章节、一子集一全集」的可读性问题**。把这三点补上即可安全执行。
