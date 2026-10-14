# 链版主线接进日报 · 执行记录（S1 + S2 + flip）

> 依据：`docs/plan/chain-mainline-report-switch-plan-2026-10-12.md`（v2，主人 2026-10-12 23:44 批准）
> 审核依据：`docs/plan/chain-mainline-report-switch-plan-review-2026-10-12.md`
> 执行时间：2026-10-12 23:45 ~ 23:55
> 结果：**S1 渲染 + S2 台账 + flip 全部完成并验收通过**。S3（旧版下线）按方案留待观察 ≥5 个交易日后单独立项。

---

## 1. 改了什么

### S1 · 渲染切链版 —— `src/daily_review/report_sections.py`（唯一文件）

| 改动 | 内容 |
|:--|:--|
| 顶部 | 新增 `import os`（原文件确实没有） |
| 常量区 | 新增 `CHAIN_MAINLINE_PATH` / `MAINLINE_SOURCE`（env `IR_MAINLINE_SOURCE`）/ `_KIND_LABEL` / `_load_mainline_frame()` |
| `_load_mainline_frame()` | 按开关读两版之一，**归一出 `gkey` / `gnames` / `kind`**；读不到自动回退另一版；返回 `(df, used_path, fell_back)` |
| `format_mainline_section` | 改用加载器；标题按 `kind` 动态 + **显式标口径**；`group_id`×4 → `gkey`；`group_names`×2 → `gnames`；`方向群` 文案×4 → 按 `kind`（产业链/方向群）；路径引用×2 → 实际路径 |
| Top5 表 | **新增「共振 z」列**（`resonance_z_today`）+ 脚注解释（Q2 采纳）；旧版无此列时整列自动不渲染 |
| 回退可见（N2） | `fell_back=True` 时章节内加 `> ⚠️ 首选产物（链版）不可用，本日**回退**到 xxx，口径为方向群` |
| `format_chain_resonance_section:212` | 标题加 **· 全 74 条**（P1-1 消歧） |

**合计 14 个改动点**（方案估 12，实做时多了「Top5 z 列脚注」「章节三标题」两处）。

### S2 · 台账切链版 —— 三处登记同批改完（P0）

| # | 位置 | 改动 |
|:--|:--|:--|
| 1 | `src/signals/sources.py:37` | 新增 `CHAIN_MAINLINE_PATH` |
| 2 | `src/signals/sources.py` `SOURCE_SPECS` | 新增 `chain_mainline` 条目，**八字段齐备**（label / score_hint / direction / status / status_since / status_doc / status_short / status_note）；`status="observational"`（N3） |
| 3 | `src/signals/sources.py` 快照层 | 抽出 `_snapshot_from_frame()`（旧版/链版共用）+ `_read_mainline_frame()`；新增 `_snapshot_chain_mainline()`；**默认元组改 `("mainline", "chain_mainline")`** |
| 4 | `tests/_common.py:180` | `LEDGER_VALID_SOURCES` 加 `chain_mainline` |

**旧版路径行为不变**（回归验证见 §2 ②）：`_snapshot_mainline` 产出的 features 键名与 `group_id` 数值类型（float）与改动前**逐字一致**，历史行兼容。

### flip · 默认 `cluster` → `chain`

`MAINLINE_SOURCE` 默认改为 `"chain"`，注释记录批准依据。开关**保留到 S3**（Q4）。
回退方式：`IR_MAINLINE_SOURCE=cluster`。

---

## 2. 验收（方案 §5 十一条，逐条）

| # | 项 | 结果 |
|:--|:--|:--|
| 1 | 主线章节出现链名 | ✅ `资源ETF博时` / `5GETF博时` / `船舶ETF富国`…（不再是"群18"） |
| 2 | 其他章节不变 | ✅ 两版 diff **仅 32 行、全部落在章节一**；章节二 regime 逐字未动。章节三仅标题多了 P1-1 的口径标注（两版相同，属预期） |
| 3 | 开关回退可用 | ✅ `IR_MAINLINE_SOURCE=cluster` ⇒ 标题回到「方向群探测」、Top5 回到"群18/群11/群14" |
| 4 | 回归测试 | ✅ **93 passed / 1 skipped**（与本轮前一致） |
| 5 | 列契约 | ✅ 未改任何产物列；两版 22/19 列契约均通过 |
| 6 | 台账（S2） | ✅ 构造验证：链版 37 链全置 `is_mainline=True` ⇒ 展开 **192 行**（= 成员名总数，解析率 192/192，N1 成立）。今日实际 0 行（当天无主线），属预期 |
| 7 | 数据根 | ✅ `industry/` 下两产物 mtime 仍为 19:30 / 19:56，本轮**零写入** |
| ⑧ | 定位不为「⏳ 待观察」 | ✅ `STATUS_LABEL[spec["status"]]` = **⛔ 观察项**，label = 主线产业链成员行业（每日状态） |
| ⑨ | 两章节标题能区分链集合 | ✅ 一 =「产业链探测 · **37 条已验证链**」；三 =「… · **全 74 条**」 |
| ⑩ | flip 前生产不变 | ✅ 出 diff 时默认仍 `cluster`，产物 mtime 未变 |
| ⑪ | 回退可见（N2） | ✅ 把链版路径指向不存在文件 ⇒ 渲染出 `> ⚠️ 首选产物（链版）不可用，本日**回退**到 mainline_detection.csv，口径为**方向群**`，且未开天窗 |

**selftest 守卫**：结构破坏 **12/12** 被抓到、源码破坏 1/1、恢复后全绿。
唯一红项「真实数据根泄漏 3 文件」= `stock/300401.SZ/fina_audit.parquet` 等，
属 **stock-doctor 项目**同时段写入（既有已知误报，非本轮引入）。

---

## 3. 两版 diff（章节一，主人已过目）

```diff
-🧭 **一、主线状态**（方向群探测）
+🧭 **一、主线状态**（产业链探测 · **37 条已验证链**）

-| 方向群 | 代表行业 | 热度 | 超额(20d) | 阶段 | 段龄 | 段内累计超额 | 主线 |
-| 群18 | 林业Ⅱ | 4.85x | +0.20% | 见顶期 | 27天 | +18.33% | — |
-| 群11 | 农业综合Ⅱ、种植业 | 2.00x | +0.00% | 见顶期 | 23天 | -0.97% | — |
-| 群14 | 航海装备Ⅱ | 1.64x | +0.18% | 启动→加速过渡 | 15天 | -3.04% | — |
-| 群17 | 非金属材料Ⅱ | 1.46x | -0.49% | 启动→加速过渡 | 18天 | -12.17% | — |
-| 群4 | 饰品、工业金属、贵金属 | 1.42x | -0.49% | 启动→加速过渡 | 78天 | +0.87% | — |
+| 产业链 | 代表行业 | 热度 | 超额(20d) | 共振 z | 阶段 | 段龄 | 段内累计超额 | 主线 |
+| 资源ETF博时 | 玻璃玻纤、工业金属、贵金属、小金属、煤炭开采、炼化及贸易 | 1.51x | -0.19% | +1.10 | 启动→加速过渡 | 85天 | +0.82% | — |
+| 5GETF博时 | 半导体、消费电子、通信设备、通信服务、元件 | 1.33x | -0.23% | +1.68 | 启动→加速过渡 | 9天 | -11.53% | — |
+| 船舶ETF富国 | 航海装备Ⅱ、其他电源设备Ⅱ、专用设备、通用设备、航运港口 | 1.28x | -0.06% | +2.87 | 蛰伏 | 0天 | +0.00% | — |
+| 国泰中证新材料主题ETF | 半导体、玻璃玻纤、化学制品、元件、电子化学品Ⅱ、光伏设备、电池 | 1.25x | -0.14% | +0.74 | 蛰伏 | 0天 | +0.00% | — |
+| 大宗商品ETF国联安 | 工业金属、贵金属、小金属、农化制品、煤炭开采、炼化及贸易 | 1.25x | -0.25% | +2.61 | 蛰伏 | 0天 | +0.00% | — |
+
+> `共振 z` = 链内成分残差同向性相对**分层零分布**的 z 值（链版独有）…
```

**读法提示**：旧版 Top5 里有 3 个是 **n=1 单行业伪群**（群18 林业Ⅱ / 群14 航海装备Ⅱ / 群17 非金属材料Ⅱ），
链版已全部消失 —— 这正是旧版"测错对象"的日常表现。链版 Top5 全是真实产业链，且给出 z：
船舶ETF富国 z=+2.87 但热度 1.28（结构强、资金未到）、资源ETF博时 z=+1.10 热度 1.51（有量、结构一般）。

---

## 4. 本轮的两个坑（下次注意）

1. **中文引号写进 Python 字符串**：`status_note` 里我第一次写成 ASCII 直引号 `"…"` ⇒ 直接
   `SyntaxError`（`py_compile` 立刻抓到）。自然语言里的引号要用弯引号「」或“”。
2. **改默认元组才算真接上**：`snapshot_signals` 的两个调用方都不传 `sources`，
   只写 `_snapshot_chain_mainline()` 而不改默认 = S2 白做（这正是审核 P0-2 的预警）。

---

## 5. 遗留（S3 待办，需观察 ≥5 个交易日）

- 生产链摘第 7 步（旧版主线探测）、`src/paths.py` 摘 ARTIFACTS、
  清 `EXPECTED_COLUMNS["mainline_detection"]`、`tests/selftest.py` NEED 移除、`test_invariants.py` 清理
- 处置旧版读取方：`research/mainline_memory_probe*.py`、`research/observation_backfill.py:81`、
  `research/observation_snapshot.py:56`、`tests/snapshot.py:109`
- 顺手订正 `tests/_common.py:130` 注释"实测 15 列"→ 19 列
- 观察项：链版入账后**前瞻样本**是否开始积累（当前台账 `chain_mainline` 行数为 0，因每日无主线）

---

*执行记录文件，不回写方案文档（沿用立项纪律）。*
