# 链版主线接进日报 · 实施方案（v2 · 已按审核意见修订）

> 状态：**v2 修订稿，待主人批准；批准前不执行**。
> 问题来源：2026-10-12 接线后，链版产物（`chain_mainline_detection.csv`）每天产出，
> 但日报正文与信号台账都仍读旧版（`mainline_detection.csv`）⇒ 新版文件躺在磁盘上没被消费。
> 本方案要彻底消除这个"产出但不消费"的断点。

### v2 修订依据与清单

审核报告：`docs/plan/chain-mainline-report-switch-plan-review-2026-10-12.md`（结论：**有条件批准**）。
本稿接受其全部 P0 / P1 / P2 结论，并做了 1 处加码与 1 项新增实测。逐条对应：

| 审核项 | 本稿处置 | 落在 |
|:--|:--|:--|
| **P0-1** 漏 `SOURCE_SPECS` 登记（会踩 `cold_zone` 同款坑） | 接受并**加码**：不只"补七字段"，连 `status` 该填什么都定死（= `observational`，理由见 §3 S2-A-③） | §2 行 5、§3 S2-A |
| **P0-2** `snapshot_signals` 默认元组未改 ⇒ S2 白做 | 接受：默认改为 `("mainline", "chain_mainline")`，并点名两个调用方 | §3 S2-A-②、§2 行 10 |
| **P1-1** 切换后日报出现两个"链"章节（37 子集 vs 74 全集） | 接受，采修法 a：标题显式标口径 | §3 S1-④、§5 ⑨ |
| **P2-1** "渲染零改动"不实（实为约 10 处）+ 缺 `import os` | 接受并更正；另核出 `方向群` 文案是 **4 处**（审核记 3 处，漏了 `:91`） | §0、§3 S1-③ |
| **P2-2** §2 清单漏 4 处读取方 | 接受：清单 9 → 13 项 | §2 |
| **P2-3** `cron:270` 是 import、`:272` 才是调用 | 接受，措辞改正 | §2 行 3 |
| **P2-4** `_common.py:130` 注释"15 列"已过期 | 接受，列入 S3 随手订正 | §3 S3 |
| **Q4** 默认 `chain` 与"先看 diff 再定稿"自相矛盾 | 接受：**默认先 `cluster`**，批准后才 flip | §3 S1-②、§7 执行序 |

**本稿新增（审核未覆盖）**：

- **N1 · 成员名解析率实测**（S2 前置风险）：链版 37 链共 **192 个成员行业名，192/192 全部**能在
  `industry_list.parquet` 解析到行业代码（旧版 131/131 同样全中）
  ⇒ `_snapshot_mainline` 的"成员名匹配失败 ⇒ 该群跳过"分支**不会对链版触发**，S2 入账行数不会因解析失败而归零。
  （实测命令见 §9 附录）
- **N2 · 回退必须可见**：加载器静默回退旧版会让读者不知道自己看的是哪一版 ⇒
  章节标题按 `kind` 显示口径（chain / cluster），回退发生时在章节内加一行 ⚠️ 提示。
- **N3 · 新 source 的 `status` 定为 `observational`**：审核只说"补字段"，没说填什么。
  按纪律，链版**对象认定正确、但前瞻有效性未验证**（1638 天回溯只证明"能出主线"，不是"主线有效"）
  ⇒ 不能填 `active`（可作交易依据），也不该填 `pending`（暗示"样本够了就有结论"）。详见 §3 S2-A-③、§6 Q5。

---

## 0. 一句话结论（v2 已更正）

切换的**风险**确实低于我此前的预估（原因见 §1：两份产物 schema 除主键列外**逐字相同**），
但**工作量**此前被低估：不是"零改动 / 改三处"，而是 **S1 约 12 个改动点 + S2 三个登记项**。
全部是机械替换与登记，**仍属低风险**，但执行时要按 §3 清单逐条对齐，不要按"三处"去找。

---

## 1. 关键发现：两份产物 schema 近同（已实测，v1 结论不变）

| | 旧版 `mainline_detection.csv` | 链版 `chain_mainline_detection.csv` |
|:--|:--|:--|
| 列数 | 19 | 22 |
| 主键 | `group_id`（整数 18/11/14…）+ `group_names` | `chain_id`（链名 资源ETF博时…）+ `chain_names` |
| **同名同义列** | **17 列完全一致**：n_industries / heat_ratio / excess_ma20 / winrate / amt_ma20 / hist_mean_amt / is_mainline / is_mainline_raw / consec_days / min_consecutive_days / hot_age / heat_slope20 / cum_excess_seg / is_new_mainline / stage / style / od_ratio | 同左 |
| 独有列 | — | `resonance_adj_today` / `resonance_z_today` / `style_share_today`（B3 共振，只作解释、不进判定） |

实测样例：旧版 `group_names` = `['林业Ⅱ']`（n=1）；链版 `chain_names` = `['玻璃玻纤','工业金属',…]`（n=3~8）。

⇒ **`chain_names` 与 `group_names` 是同一个命名空间（申万二级行业名）**，
所以 `sources.py` 里"行业名 → 行业代码"的映射逻辑对链版同样适用，无需改写（**N1 实测 192/192 全中**）。

---

## 2. 现状：谁在读旧版（改动点全清单 · v2 补 4 项）

| # | 位置 | 作用 | 本方案处理 |
|:--|:--|:--|:--|
| 1 | `src/daily_review/report_sections.py:33` | `MAINLINE_PATH = INDUSTRY_DIR / "mainline_detection.csv"`；`:84` 读取、`:128-135` 按 heat_ratio 取 Top5 渲染 | **S1 改**：换为可切换加载器 |
| 2 | `src/signals/sources.py:36` | `MAINLINE_PATH`（同名常量），`_snapshot_mainline:162` 读旧版 + 过滤 `is_mainline==True` 入台账 | **S2 改**：新增链版快照函数 |
| 3 | `cron/industry_review_daily.py:270` import / **`:272` 调用** | `build_extra_sections` → `report_sections.py:275` 调 `format_mainline_section` | 不改（入口不变） |
| 4 | `tests/_common.py:77 / :98` | 两版列契约白名单都已登记、都已激活 | **不改**（切换不改列） |
| 5 | `tests/_common.py:180` | `LEDGER_VALID_SOURCES` | **S2 改**：加 `chain_mainline` |
| 5b | `src/signals/sources.py:62` `SOURCE_SPECS` | 评估报告按此表渲染「定位 / 标签 / 依据」；**未登记的 source 会渲染成「⏳ 待观察」+ 空标签** | **S2 改（P0-1）**：新增 `chain_mainline` 条目 |
| 6 | `tests/test_invariants.py:385` | 旧版结构检查（非空、is_mainline 取值、n_industries≥1） | S3 下线旧版时清理 |
| 7 | `tests/selftest.py:55-57` | NEED 清单含两个产物 | S3 才动 |
| 8 | `research/mainline_memory_probe*.py` | 段龄/段内累计超额 IC 探针，读旧版 | **S3 前置**：改指链版或归档（字段同名，改路径即可） |
| 9 | `tests/baseline_snapshot.json` | 数值基线 `_meta.note` 明示"不作失败判据" | 不影响 |
| **10** | `scripts/build_signals_log.py:62` | `snapshot_signals(obs_date)` **第二调用方**（补 P0-2） | **S2 改**：默认元组改了即可，本行不动 |
| **11** | `cron/industry_review_daily.py:316` | `snapshot_signals(trade_date)` 第一调用方，同样**不传 sources** | **S2 改**：同上 |
| **12** | `research/observation_backfill.py:81` | 读旧版做新旧回溯对照 | **S3 前置**：改路径 / 归档 |
| **13** | `tests/snapshot.py:109` | baseline 快照对 `mainline_detection` 采 `n_mainline` | **S3 前置**：改指链版 |

---

## 3. 方案：四阶段（S0 已完成，S1 是核心）

### S0 生产链产出链版 —— ✅ 已完成（2026-10-12 20:00）
十步链第 7b 步，产物 `industry/chain_mainline_detection.csv`，22 列，列契约测试已激活。

### S1 渲染切链版（低风险，本批核心）

改动集中在 `src/daily_review/report_sections.py` 一个文件，四处动作：

#### ① 常量区 + `import os`（文件当前**未** import os）

```python
import os                       # ← 新增（当前 import 列表里没有）

MAINLINE_PATH       = INDUSTRY_DIR / "mainline_detection.csv"        # 旧版（回退用）
CHAIN_MAINLINE_PATH = INDUSTRY_DIR / "chain_mainline_detection.csv"  # 链版（B2）

# 切换开关：chain（链版）| cluster（旧版）。
# 🔴 默认 **cluster**（不是 chain）—— 见 §7 执行序：先看两版 diff，批准后才 flip。
MAINLINE_SOURCE = os.environ.get("IR_MAINLINE_SOURCE", "cluster")

def _load_mainline_frame():
    """按开关读主线产物，归一出 gkey/gnames/kind ⇒ 下游渲染字段级复用。

    链版缺失（或开关切回）时自动回退旧版，保证日报永不空章。
    返回 (df, path, fell_back) —— fell_back=True 时渲染层要**显式提示**（见 ④）。
    """
    order = ((CHAIN_MAINLINE_PATH, MAINLINE_PATH) if MAINLINE_SOURCE == "chain"
             else (MAINLINE_PATH, CHAIN_MAINLINE_PATH))
    for i, p in enumerate(order):
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        if "chain_id" in df.columns:
            df["gkey"], df["gnames"], df["kind"] = df["chain_id"].astype(str), df["chain_names"], "chain"
        else:
            df["gkey"], df["gnames"], df["kind"] = "群" + df["group_id"].astype(str), df["group_names"], "cluster"
        return df, p, (i > 0)
    return None, None, False
```

#### ② 默认值先 `cluster`（Q4 时序修正）

**合并当晚生产链必须仍跑旧版**，等两版 diff 送审、主人批准后才把默认改成 `chain`
（或在 `每日行业报告.command` 里显式注入 `IR_MAINLINE_SOURCE=chain`）。
否则"先看 diff 再定稿"这一步形同虚设。

#### ③ 渲染替换：**约 12 个点**（v1 写"三处"，已更正）

| 行 | 现状 | 改为 |
|:--|:--|:--|
| `:33` | 单一 `MAINLINE_PATH` | 加 `CHAIN_MAINLINE_PATH` / `MAINLINE_SOURCE` / `_load_mainline_frame()` |
| 顶部 | 无 `import os` | 新增一行 |
| `:82` | `🧭 一、主线状态**（方向群探测）**` | 按 kind 动态：`（产业链探测 · 37 条已验证链）` / `（方向群探测）` |
| `:84` | `pd.read_csv(MAINLINE_PATH)` | `_load_mainline_frame()` |
| `:87` | 读取失败提示写死 `MAINLINE_PATH.name` | 用实际尝试的路径 |
| `:91` | `今日无方向群数据。` | 按 kind 显示「产业链 / 方向群」 |
| `:103` | 表头 `方向群` | 按 kind → `产业链` / `方向群` |
| `:107` | `群{group_id}` + `_parse_group_names(group_names)` | `gkey` + `gnames` |
| `:120` | `群{group_id}`（新晋主线提示） | `gkey` |
| `:123` | `群{group_id}`（放量不涨提示） | `gkey` |
| `:130` | Top5 表头 `方向群` | 按 kind |
| `:135` | `群{group_id}` + `group_names` | `gkey` + `gnames` |

统计：`group_id` ×4（107/120/123/135）、`group_names` ×2（107/135）、`方向群` 文案 ×4（82/91/103/130）、
路径引用 ×2（84/87），加常量区与 `import os` ⇒ **12 个改动点**。

**可选（Q2）**：热度 Top5 表增加一列 `共振 z`（取 `resonance_z_today`），与表头、单元格各加 1 处。

#### ④ 两个"链"章节的口径消歧（P1-1，必做）

实测：链版 37 条 `chain_id` 是共振章节 74 条的**真子集**（交集 37/37，仅共振有 37 条如
`TMTETF景顺` / `VRETF银华`），两者 `chain_id` 命名空间**相同** ⇒ 读者会以为是同一批链。

| 章节 | 数据源 | 链数 | 指标 |
|:--|:--|:--|:--|
| 一、主线状态（切换后） | `chain_mainline_detection.csv` | **37**（方案 C：verified + n≥3 + z_s3>0） | 热度/超额/胜率/判定 |
| 三、链共振强度 | `chain_resonance_daily.csv` | **74**（全部先验链） | 残差共振/风格份额/z |

修法（采 a）：**标题即口径**
- `:82` → `🧭 **一、主线状态**（产业链探测 · **37 条已验证链**）`
- `:212` → `🔗 **三、链共振强度**（语义先验链 × 残差共振 · **全 74 条**）`

**并补 N2**：当 `fell_back=True`（链版缺失、静默回退旧版）时，在章节内加一行
`> ⚠️ 链版产物缺失，本日回退旧版方向群（口径为方向群，非产业链）` —— 回退不能静默。

### S2 台账切链版（中风险，需拍板）

#### ① 走 A：注册新 source `chain_mainline`（Q1）

- **为什么不是 B（沿用 `source="mainline"`）**：不只为"语义整洁"——复用会让新行**继承
  `mainline` 的 `status_note`**（"方法已被判定测错对象…⛔ 不得用于支撑或否定任何链假设"），
  而 `evaluate.py:368` 会照这条渲染 ⇒ 等于给新方法贴上"已证伪"标签。A 几乎是唯一正确解。

#### ② 三个登记项必须同时改（P0，缺一不可）

1. `sources.py`：新增 `_snapshot_chain_mainline()`（复用 `_load_industry_names` / `_parse_group_list`，
   把 `group_names` 换为 `chain_names`、`group_id` 换为 `chain_id`，features 里同步改名）
2. `sources.py:62` **`SOURCE_SPECS["chain_mainline"]` 七字段**（P0-1，见 ③）
3. `sources.py:205` **默认元组** `sources=("mainline", "chain_mainline")`（P0-2）；
   两个调用方 `cron:316` 与 `build_signals_log.py:62` 都**不传参**，不动默认就等于没接
4. `tests/_common.py:180` `LEDGER_VALID_SOURCES` 加 `chain_mainline`
   （`test_invariants.py:365` 会对未登记 source 直接判红）

> 仓库自己的硬要求写在 `tests/_common.py:179`：
> 「新增信号来源须同步此表 **+ `src/signals/sources.py` 的 SOURCE_SPECS**」。

#### ③ 七字段怎么填（N3，本稿加码）

```python
"chain_mainline": {
    "label": "主线产业链成员行业（每日状态）",
    "score_hint": "heat_ratio 热度，越大越强",
    "direction": "desc",
    "status": "observational",          # ← 不是 active，也不是 pending
    "status_since": "2026-10-12",
    "status_doc": "docs/plan/chain-mainline-report-switch-plan-2026-10-12.md",
    "status_short": "对象认定正确（已验证产业链），但前瞻有效性未验证；⛔ 不作交易依据",
    "status_note": (...见下...),
}
```

**为什么是 `observational`**：
- ✅ 不是 `active`——链版**没有做过前瞻事件研究**。1638 天回溯证明的是"方法能正常出主线"
  （2026 H1 主线率 66%），**不是**"主线信号有 alpha"。项目纪律：无前瞻证据不标生效。
- ✅ 不是 `pending`——`pending` 的语义是"样本够了就有结论"，而这里**结论已部分下了**：
  链的对象认定正确（46/74 链跨段共振检验通过）。
- ✅ 是 `observational`——与 `mainline` 同档（"保留入账积累样本，不作交易依据"），
  但**理由完全不同**：`mainline` 是"测错对象"，`chain_mainline` 是"对象对、有效性待验"。
  `status_note` 里必须把这层区别写清楚，否则两个 observational 会被读成同一件事。

**为什么必须登记**：`evaluate.py:301/318/357` 一律 `SOURCE_SPECS.get(src, {})`，
未登记 ⇒ `status` 回落 `pending`、`label` 空串 ⇒ 评估报告把定位已写清的新来源渲染成
「⏳ **待观察**」+ 空标签。`sources.py:69-71` 的 `cold_zone` 注释正是为这个坑写的，别再踩第二次。

#### ④ S2 前置风险已实测排除（N1）

链版 37 链 192 个成员名 **192/192** 可解析为行业代码 ⇒ `_snapshot_chain_mainline`
不会因匹配失败而整条链跳过 ⇒ 入账行数只取决于当天有无 `is_mainline=True` 的链。

### S3 旧版下线（收尾，需观察后再做）

- 生产链摘第 7 步、`src/paths.py` 摘 ARTIFACTS、清 `EXPECTED_COLUMNS["mainline_detection"]`、
  `tests/selftest.py` NEED 移除、`test_invariants.py:385` 清理
- **v2 补全的处置清单**：`research/mainline_memory_probe*.py`、`research/observation_backfill.py:81`、
  `research/observation_snapshot.py:56`、`tests/snapshot.py:109`（均为改路径/归档）
- **随手订正（P2-4）**：`tests/_common.py:130` 注释"实测 15 列"已过期（现 19 列）
- **前置**：S1+S2 稳定运行 ≥5 个交易日 + 上述四处处置完毕 + 主人确认

---

## 4. 风险与回退（v2 已更新）

| 风险 | 对策 |
|:--|:--|
| 报告文案/内容大变（群18 → 资源ETF博时） | 开关双跑，**先出两份 diff 给你看再定稿**；**默认先 `cluster`**，合并即生效的风险已消除 |
| 链版文件某天缺失 ⇒ 日报开天窗 | 加载器内置回退（链版缺失自动读旧版）；**且回退要在章节内显式提示**（N2），不静默 |
| 台账 source 语义污染 | S2 走 A（新 source）；历史行保留可追溯 |
| **S2 白做**（只改一处登记） | P0-1/P0-2 三项登记同批改完，验收 ⑧ 卡住"⏳ 待观察" |
| **两个链章节口径混淆**（37 子集 vs 74 全集） | P1-1 标题显式标口径，验收 ⑨ |
| 一条链含多行业 ⇒ 入账行数变多 | 预期行为（链=多个行业）；N1 已排除"解析失败导致 0 行"的混淆 |
| 探针 / 观察脚本失效 | S3 前置处置（字段同名，改路径即可）；v2 清单已补全 4 处 |
| 改崩了 | 改动集中在 1~2 个文件的常量/函数；`git revert` 一行回退；两版产物并存 |

---

## 5. 验收标准（可测）

| # | 项 | 判据 |
|:--|:--|:--|
| 1 | 报告主线章节 | 出现链名（如"资源ETF博时"），不再是"群18" |
| 2 | 其他章节不变 | regime / 链共振章节与切换前**逐字一致**（diff 验证） |
| 3 | 开关回退可用 | `IR_MAINLINE_SOURCE=cluster` 重跑 ⇒ 报告回到旧版内容；反向亦成立 |
| 4 | 回归测试 | `pytest tests/ -q` 保持 93 passed / 1 skipped |
| 5 | 列契约 | 两版 22/19 列契约均不被破坏（本次不改列） |
| 6 | 台账（S2） | 新入账行 source = `chain_mainline`、行业代码可解析、行数符合链成员数（N1：解析率 192/192） |
| 7 | 数据根 | 只写 `reports/` 与 `signals/`，`industry/` 下无意外改动 |
| **⑧** | **（P0-1）** 评估报告「信号定位」表 | `chain_mainline` 的定位列**不为「⏳ 待观察」**，label 非空，一句话说明可见 |
| **⑨** | **（P1-1）** 章节标题 | 「一」含"37 条已验证链"、「三」含"全 74 条"，一眼可区分 |
| **⑩** | **（Q4 时序）** 合并当日 | 生产产物与日报正文**未变**（默认仍 `cluster`），直到主人批准 flip |
| **⑪** | **（N2）** 回退可见性 | 人为把链版改名后重跑 ⇒ 日报出现回退提示行，且未开天窗 |

---

## 6. 待你拍板（5 项）

1. **Q1 · S2 走 A 还是 B**：推荐 A（新 source `chain_mainline`）。B 会让新行继承 `mainline` 的
   "测错对象"标签，等于自贴"已证伪"。
2. **Q2 · 热度 Top5 是否加「共振 z」列**：链版独有，把"结构强度"与"被关注程度"并列；
   且 z 是唯一跨链可比的量（原始共振受链规模稀释）。代价：表格变宽一格。
3. **Q3 · S1 与 S2 是否同批执行**：推荐**同批**。S2 因 P0 多改 2 处登记，仍可控。
4. **Q4 · 回退开关保留多久**：保留到 S3 完成为止。**v2 新增**：默认先 `cluster`，批准后 flip（见 §7）。
5. **Q5 · 新 source 的 `status`**（N3 新增待确认）：建议 **`observational`**
   ——对象认定正确、前瞻有效性未验证。若你认为应标 `pending`（我不同意：`pending` 暗示"样本够了就有结论"，
   而链的对象问题已有结论），请明确。

---

## 7. 执行序（v2，按审核 §6 重排）

1. **出 diff（不改默认，只读渲染）**：本地以 `build_extra_sections(date)` 渲染两版
   （`IR_MAINLINE_SOURCE=chain` / `cluster`），逐行 diff 送审——**不动生产、不落盘**。
2. **批准本方案**（含 §6 五项拍板）。
3. **S1 改动**：`report_sections.py` 加 `import os` + 常量区 + `_load_mainline_frame()`
   + 12 处渲染替换 + 标题口径消歧（含 `:212`）+ 回退提示；**默认仍 `cluster`**。
4. **S2 改动（补齐 P0）**：`sources.py` 新增 `_snapshot_chain_mainline()` **+ `SOURCE_SPECS` 七字段**
   **+ 默认 `sources` 元组**；`_common.py` 补 `LEDGER_VALID_SOURCES`。
5. **批准后 flip**：默认改 `"chain"`（或生产链显式注入），观察 ≥1 个交易日。
6. **S3**（观察 ≥5 日后）：下线旧版，处置 §2 行 8/12/13 三处读取方 + 订正 `_common.py:130` 注释。

> 每一步单独提交，便于 `git revert` 单点回退。

---

## 8. 非目标（本次不做）

- 不动判定阈值（热度 1.3x / 超额 0.3% / 胜率 60% / 连续 ≥3 日）——无新证据不动阈值
  （1638 天回溯已验证：门槛放宽 3 倍主线率仅 +3.3pp，阈值不是瓶颈）
- 不动链名单（方案 C 37 链已锁定）
- 不改链共振测量口径（章节三保持 74 条全集）
- 不在本方案内做旧版下线（S3 需观察后单独立项）

---

## 9. 附录：本稿新增的实测命令（可复跑）

```bash
cd /Users/hyc/quant/industry-radar && /usr/bin/python3 -c "
import ast, pandas as pd
il = pd.read_parquet('/Users/hyc/quant-data/industry/industry_list.parquet')
name_to_code = {str(n): str(c) for c, n in zip(il['ts_code'], il['name'])}
for f, key in [('chain_mainline_detection','chain_names'), ('mainline_detection','group_names')]:
    df = pd.read_csv(f'/Users/hyc/quant-data/industry/{f}.csv')
    tot = miss = 0
    for _, r in df.iterrows():
        for n in ast.literal_eval(str(r[key])):
            tot += 1
            if n not in name_to_code: miss += 1
    print(f, '成员名', tot, '未匹配', miss)
"
# 实测输出：chain_mainline_detection 192 / 0 ；mainline_detection 131 / 0
```

链集合关系实测：链版 37 ∩ 共振 74 = 37，仅共振有 37 条（如 `TMTETF景顺`、`VRETF银华`）。

---

*v1 → v2 修订于 2026-10-12，依据审核报告 `chain-mainline-report-switch-plan-review-2026-10-12.md`。
本文件为方案，批准通过后另开执行记录（沿用立项文档纪律：方案文件不原地追加执行结果）。*
