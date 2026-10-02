# 产物指纹 + 从端写保护 + 未登记写入审计（2026-10-02）

> 路线一（单源）第一批：**让「谁在往数据根写东西」变成机器可读**。
> 本批只改 industry-radar 仓库内代码，**不依赖源端配合、不动任何现有数据**。

---

## 1. 为什么先做这一件

症结不是「产物放在哪」，而是「往共享数据根写入」这件事**从头到尾没有被声明出来**：

- `DATA_CONTRACT.md` 登记的全是**内容契约**（口径 / 主键 / 频率），**没有一条登记「谁能写」**；
- 数据根实际有两类写入者（Tushare 抓取器 + 本地计算生产者），文档只描述了前者；
- 代码走 git、产物走 rsync，**两条通道互不关联** —— 改了口径，历史产物会**静默**变成混合口径。

而契约里想写「发现未登记写入就告警」，判定依据只能是「派生目录里的文件有没有 `.meta.json` 伴随」。
**没有指纹，这条校验根本写不出来。** 所以顺序必须是：指纹 → 写保护 → 契约登记 → 代码同步。

---

## 2. 交付物

### 2.1 新增 `src/utils/provenance.py`（约 370 行）

落盘时同时写 `<文件名>.meta.json`，字段：

| 字段 | 含义 |
|---|---|
| `schema` / `produced_at` / `hostname` | 指纹格式版本 / 生产时刻 / 生产主机 |
| `producer` | 生产脚本（仓库相对路径） |
| `git.commit` / `git.dirty` / `git.branch` | 产出时的代码版本；`dirty=True` 表示出自未提交的工作区 |
| `target` / `written_to` | 逻辑落点 / 物理落点（只在被重定向时出现后者） |
| `rows` / `columns` / `columns_hash` / `date_range` | 产物形状；`columns_hash` 是**口径指纹** |
| `params` | 生产参数，如加权方式、最小成员数、窗口长度 |
| `inputs` | 输入指纹（路径 + size + mtime） |

对外三个入口（推荐按序取用）：

```python
from src.utils.provenance import save_dataframe, save_json, save_text
written = save_dataframe(df, OUT_PATH, producer="scripts/xxx.py",
                         params={...}, inputs=[...])   # 落盘 + 落指纹，返回**实际**路径
```

要点：
- **返回实际路径**（可能因写保护被重定向），调用方应用返回值做打印/后续引用；
- 指纹写入失败**只告警、不抛异常**，绝不阻塞主产物；
- `record()` 供「调用方已自行落盘」的场景补指纹。

### 2.2 `src/paths.py`：产物登记表 + 从端写保护

**产物登记表 `ARTIFACTS`**（17 项，唯一真相来源）：

| kind | 含义 | 条目 |
|---|---|---|
| `derived` | 本项目计算产出（可再生） | industry_daily_full / industry_weighted_returns / industry_members_daily / industry_nav_924 / daily_regime / monthly_regime_summary / mainline_detection / tracker_report / industry_universe / ind_crowd_turnover_daily / industry_margin_daily / signals_log |
| `fetched` | 外部抓取原料（Tushare / 富途） | index_member_all_raw / industry_list / industry_members |
| `cache` | 可随时重建的中间缓存 | industry_daily_cache |
| `status` | 运行状态记录 | completeness_status |

`DERIVED_DIRS = (INDUSTRY_DIR, FACTOR_DIR, SIGNALS_DIR)` —— `--audit-writes` 的扫描范围。

**从端写保护**（`resolve_output_path()`，唯一入口）：

| `INDUSTRY_RADAR_READONLY` | 行为 |
|---|---|
| 未设 / `0` / `off` | 正常写入 |
| `1` / `true` / `yes` / `redirect` | 数据根内的写入整体重定向到 `INDUSTRY_RADAR_OUTPUT_DIR`（默认 `/tmp/industry-radar-out`），保持相对层级 |
| `strict` | 数据根内的写入直接抛 `ReadOnlyError` |

数据根之外的路径（仓库内、`/tmp`）任何模式下都放行。

> 这说明「本机是只读消费端」不再是靠人记得挂环境变量，而是**物理事实**。
> 触发场景可考：沙箱测试曾误写过真实数据根的 `completeness_status.json`。

### 2.3 接入 25 个落盘点（18 个文件）

| 文件 | 落盘产物 |
|---|---|
| `scripts/build_industry_daily_full.py` | industry_daily_full |
| `scripts/compute_weighted_returns.py` | industry_weighted_returns |
| `scripts/build_members_daily.py` | industry_members_daily |
| `scripts/build_nav_924.py` | industry_nav_924 |
| `scripts/update_industry_factor.py` | ind_crowd_turnover_daily（全量 / 增量两条分支） |
| `scripts/regime_classifier.py` | daily_regime + monthly_regime_summary |
| `scripts/mainline_detector.py` | mainline_detection |
| `scripts/industry_tracker.py` | tracker_state + tracker_report |
| `scripts/build_industry_universe.py` | industry_universe |
| `scripts/fetch_sw_members.py` | index_member_all_raw |
| `scripts/pull_futu_industry.py` | industry_list + industry_members |
| `scripts/check_data_completeness.py` | completeness_status |
| `src/signals/ledger.py` | signals_log |
| `src/signals/sentiment.py` | industry_margin_daily |
| `src/signals/evaluate.py` | eval_*.md |
| `src/daily_review/module_02_industry.py` | industry_daily_cache + module_02_conclusion + 日报 md |
| `src/daily_review/report_sections.py` | 报告追加章节（**补写保护漏洞**，见 §5） |

`research/` 下的研究脚本**未改动** —— 不属生产链路。

### 2.4 `scripts/check_data_completeness.py`：两项新检查

**① 产物指纹校验**（参与退出码）

| 情形 | 级别 |
|---|---|
| 已登记产物存在但缺 `.meta.json` | WARNING `PROV_MISSING` |
| 指纹记录 `dirty=True`（出自未提交代码，不可复现） | WARNING `PROV_DIRTY` |
| 指纹 `commit ≠ HEAD`（代码漂移） | **只进 summary 不告警** —— 每次提交都会让存量产物「漂移」，升级成 issue 会天天误报 |

**② 未登记写入审计**（人工调用 `--audit-writes`，不参与退出码）

扫 `DERIVED_DIRS` 下既不在 `ARTIFACTS` 内、也无指纹伴随的文件，按目录归组打印。

---

## 3. 验证

### 3.1 同源 A/B 对照（零回归证据）

基线代码从 `HEAD` 导出到 `/tmp/ir-repo-base`，与改动后代码**各跑一遍同一份沙箱输入**
（11 个步骤：build_members_daily → build_industry_daily_full → compute_weighted_returns →
build_nav_924 → update_industry_factor → regime_classifier → mainline_detector →
industry_tracker → build_signals_log → check_data_completeness → cron/industry_review_daily）。

**结果：产物逐字节一致（sha256 相同）**

`industry_members_daily` / `industry_daily_full` / `industry_weighted_returns` / `industry_nav_924` /
`ind_crowd_turnover_daily` / `daily_regime` / `monthly_regime_summary` / `mainline_detection` /
`tracker_report` / `tracker_state` / `industry_daily` —— **11 项全等**。

唯一差异两处，均**预期内**：

- `signals/signals_log.parquet`：仅 `logged_at` 列在**本次新追加的 4 行**上不同（墙钟时间戳）；
- `completeness_status.json`：新增 `summary.provenance` 段（本改造目的）。

### 3.2 产出的指纹

A/B 沙箱内新侧落 **12 个** `.meta.json`；基线侧 **0 个**（正确）。

### 3.3 强制写入覆盖测试（补测被 A/B 跳过的写盘点）

A/B 中因「数据已是最新」而被跳过的分支，逐个强制触发：

| 测试 | 结果 |
|---|---|
| `update_industry_factor.py` 增量写入分支 | ✅ `ind_crowd_turnover_daily.parquet.meta.json` |
| `industry_tracker.py run_forward` | ✅ `tracker_state.json.meta.json` + `tracker_report.csv.meta.json` |
| `build_industry_universe.py` | ✅ `industry_universe.parquet.meta.json` |
| `evaluate.save_eval_report` | ✅ `eval_20261002.md.meta.json` |
| `sentiment.build_panel` | ✅ `industry_margin_daily.parquet.meta.json`（211,912 行） |
| 静态关键字校验（AST 扫全仓 25 个调用点） | ✅ 全部关键字都在签名内 |

> ⚠️ 沙箱 venv 无 `scipy`，`update_industry_factor.py` 用**最小 `stats.rankdata` 替身**跑通落盘分支
> （替身只用 7 行，仅验证钩子落盘，不验证因子数值）。

### 3.4 真实数据根守卫

跑前 `touch /tmp/guard`，跑后 `find ~/quant-data -type f -newer /tmp/guard` —— 全程**零数据文件写入**。

> 唯一例外是数据根顶层的 `.DS_Store`（18 KB，mtime 20:10）—— 这是 **Finder 的目录元数据**，
> 全仓代码零处引用 `.DS_Store`，与链路无关；**306 个数据文件与全部产物的 mtime 均未变**。

### 3.5 真实数据根上的实跑

```
产物指纹  : 0/16 有指纹（缺 16，脏 0）  当前提交 2d9eb0c
⚠️ 完备性: WARNING（严重 0 / 警告 1）
```

`0/16` 是**预期**：真实根的产物都产生于接入指纹之前。重跑对应脚本即可逐项补齐。
（登记表 17 项，其中 `industry_universe.parquet` 在真实根不存在，故只校验 16 项。）

**未登记写入审计**：**306 个文件 / 90.7 MB**

| 目录 | 数量 | 占用 |
|---|---|---|
| `<根>/factors/stock` | 2 | 31.8 MB |
| `<根>/industry` | 28 | 13.2 MB |
| `<根>/industry/etf_daily` | 250 | 11.6 MB |
| `<根>/industry/solo_grid` | 21 | 26.5 MB |
| `<根>/industry/reversal_grid` | 2 | 7.3 MB |
| `<根>/industry/etf_holdings` | 3 | 0.2 MB |

其中 `factors/stock/{momentum_nday, rs_relative}.parquet` 与历史记录一致 —— **owner 仍不明**，
正是这次审计要抓的对象。`industry/solo_grid/` 与 `industry/` 里还躺着**源码 `.py`**（数据目录污染）。

---

## 4. 本机不写真实根的操作方式

```sh
# 开发/验证：重定向到沙箱（推荐）
INDUSTRY_RADAR_READONLY=1 INDUSTRY_RADAR_OUTPUT_DIR=/tmp/xxx \
  INDUSTRY_RADAR_DATA=/tmp/sandbox python3 scripts/xxx.py

# 只读巡检：任何写入直接报错（最保险）
INDUSTRY_RADAR_READONLY=strict python3 scripts/check_data_completeness.py --no-write
```

⚠️ `strict` 模式下**读**不受影响，但任何落盘会抛 `ReadOnlyError` —— 这是有意的。

---

## 5. 顺带修掉的写保护漏洞

`src/daily_review/report_sections.py:append_to_report()` 原本以 `open(path, "a")` **直接追加**，
绕过了重定向 —— 在只读副本上会直接写进真实数据根的报告里。已改为先过 `resolve_output_path()`。

同类扫描（`open(..., "a"/"w")`）全仓复查，生产路径的文件写入**已全部走统一入口**。

---

## 6. 新发现的既有缺陷（未修，留档）

**`scripts/industry_tracker.py:581` —— 拥挤度 None 未过滤**

```python
crowd_today = self._get_crowd_percentile(date_str, code)
crowd_vals = [crowd_today]              # ← 未判 None
for offset in range(1, 6):
    cv = self._get_crowd_percentile(back_date, code)
    if cv is not None:                  # ← 这里判了
        crowd_vals.append(cv)
crowd_vals.sort()                       # TypeError: '<' not supported between 'float' and 'NoneType'
```

**处理不对称**：当日值不判 None、回溯值判。当 `crowd` 因子缺当日数据时必崩。

**已确认是既有缺陷**：用改动前的基线代码在同一沙箱复现，**同一行、同样报错**。
触发条件（沙箱里把因子文件截断掉最新日）在真实环境下对应「因子脚本先于行情数据更新」的时序错位。

建议修法：`crowd_vals = [v for v in ... if v is not None]`，并在空列表时跳过 λ 更新。

---

## 7. 未做 / 待办

- **源端同步**：本批 18 个文件尚未送到源端，源端日更仍跑旧代码 —— 那是最上层的 P0 前提。
- **契约写入权登记**（第 ③ 件）：往源端 `DATA_CONTRACT.md` 加「路径模式 | 层 | 唯一写者 | 可再生 | 同步策略」表，
  并把自指的「本机 = MacStudio」改成角色名（`source` / `replica`）——
  **必须在源端改**，否则下次 pull 被覆盖。
- **306 个未登记写入的处置**：逐个归入 ① 登记为产物 / ② 登记为 fetched / ③ 归档到 `_archive/`。
  **需要主人拍板，本次一个都没动。**
- **`fetch_sw_members.py` / `pull_futu_industry.py`** 的指纹钩子只做了静态校验：
  本机无 Tushare token（已按凭据治理移出），富途 OpenD（`127.0.0.1:11111`）未监听。
- **本批代码尚未提交**（工作区 18 改 + 1 新增未跟踪）。指纹里的 `dirty=True` 与
  完备性检查的 `PROV_DIRTY` 告警，都要等提交后重跑才会清掉。

---

## 8. 影响面

- 新增文件：`src/utils/provenance.py`
- 修改文件：18 个（`+401 / -62` 行）
- **不改任何现有口径**（宽度定义 / 加权方式 / 基准 / 阈值全部原样）
- **不写任何现有数据**（A/B 与守卫已证）
- 日更退出码语义不变（指纹章节的 `PROV_MISSING` / `PROV_DIRTY` 均为 WARNING）
