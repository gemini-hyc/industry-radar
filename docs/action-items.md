# 待办清单（Action Items）

> 维护：2026-10-01 起。**本文件分为「源端执行」与「待决策」两块。**
> 源端 = Mac Studio（`100.78.155.22`），本机 MacBook Air 只是开发机 + 只读数据副本。
> 完成一项就在该项前打 `[x]` 并注明日期；新增项追加到对应区块末尾。

---

## 一、源端（Mac Studio）待执行

### A. 代码同步（每轮改动都要做）

| # | 状态 | 内容 | 来源 |
|:--:|:--:|---|---|
| A1 | `[ ]` | 同步 **P1 的 5 个文件** + 跑台账回填 | `docs/changes-2026-10-01-p0p1.md` |
| A2 | `[ ]` | 同步 **`scripts/regime_classifier.py`** + 全量重算 + 重跑评估 | `docs/changes-2026-10-01-p2-regime.md` |
| A3 | `[x]` | **P2-2/P2-3 异常样本剔除 + 宽度分母修正**（2026-10-01 完成，待同步） | `docs/changes-2026-10-01-p2-extremes-width.md` |
| A4 | `[ ]` | **P2-4 全历史 PIT 成分 + 消除前视偏差**（2026-10-01 完成，待同步；**会再次改变行业聚合与冷区信号**） | `docs/changes-2026-10-01-p2-pit-members.md` |
| A5 | 🔶 | **P0-2 防御：`--days 60` 窗口（🔴 根因，2026-10-03 新立）** | **决策（2026-10-03）＝先加拒写护栏、暂不动窗口参数、暂不重算历史。** ✅ **护栏已落地**（`scripts/build_industry_daily_full.py` 新增 `_preflight_guard()`：① 行情窗口 <33 天即拒 ② 窗口起点=行情最早日则豁免 ③ 逐日「`breadth` 为 0 或 NaN」占比中位数 ≥80% 即拒；`--force-overwrite` 全部跳过）。验证：单元 8/8 通过 + 真实 `--days 60` 产物 sha 与基线**逐行逐列一致**（零回归）+ 真实 `--days 10` 拒写 exit=1 + 真实数据根零写入。⏳ **仍待拍板**：是否改窗口（`--days 2000`）/ 引入数据版本探测。详见 `docs/changes-2026-10-03-daily-full-breadth-anomaly.md` §5 |
| A6 | `[ ]` | **同步 2026-10-03 的缺陷修复（两个脚本）** + 同源 A/B 已通过 | `scripts/industry_tracker.py`（A1.1 拥挤度 None）、`scripts/mainline_detector.py`（A2 的 D12/D13/D14）。⚠️ **纯内部口径修正，不影响当日产物**（实测逐行逐列一致）⇒ 同步后**无需重算**。详见 `docs/changes-2026-10-03-defect-fixes.md` |
| A7 | `[ ]` | **同步 2026-10-03 的护栏改动**（`scripts/build_industry_daily_full.py`） | ⚠️ **须与 A5 一起在源端落地** —— 护栏只在源端跑日更时才有意义（本机不跑日更）。落地后观察下次日更是否出现「写入前自检未通过」，可反向验证 P0-2 根因 |

**A1 具体命令**
```bash
cd ~/Quant/industry-radar
# 同步 cron/industry_review_daily.py、scripts/{build_signals_log,build_industry_daily_full,compute_weighted_returns}.py、src/signals/evaluate.py
python3 scripts/build_signals_log.py --no-snapshot
# 预期：台账仍 2257 条，fwd_ret_5 填充数 2235 → 2255
```

**A2 具体命令**
```bash
cd ~/Quant/industry-radar
python3 scripts/regime_classifier.py     # 预期「行数护栏: 旧 1636 → 新 1636 行 ✓」
python3 scripts/evaluate_signals.py      # regime 分组由 5 类变 8 类
```

**A3 具体命令**
```bash
cd ~/Quant/industry-radar
# 同步：src/paths.py、src/utils/data_quality.py（新增）、src/utils/market_data.py、
#       scripts/compute_weighted_returns.py
python3 scripts/compute_weighted_returns.py --days 250    # 必须全量，不能 --days 5
python3 scripts/build_industry_daily_full.py --days 250
python3 scripts/regime_classifier.py                      # regime 依赖加权涨跌幅
python3 scripts/evaluate_signals.py
```

**A4 具体命令**（⚠️ 与 A3 共用同一份 `src/utils/market_data.py`，建议合并执行）
```bash
cd ~/Quant/industry-radar
# 同步：scripts/build_members_daily.py、scripts/build_industry_daily_full.py、src/utils/market_data.py
python3 scripts/build_members_daily.py                     # 全历史 PIT：预期 8,417,497 行 / 1636 天
python3 scripts/compute_weighted_returns.py --days 2000    # 现在能真正覆盖全历史
python3 scripts/build_industry_daily_full.py --force-overwrite
python3 scripts/regime_classifier.py
python3 scripts/build_nav_924.py
# 台账冷区信号必须重算（口径已变，约 10% 的信号不同）
cp <数据根>/signals/signals_log.parquet <数据根>/signals/signals_log.parquet.bak
python3 -c "
import pandas as pd
from src.paths import SIGNALS_LOG_PATH
df = pd.read_parquet(SIGNALS_LOG_PATH)
n = int((df['source'] == 'cold_zone').sum())
df[df['source'] != 'cold_zone'].to_parquet(SIGNALS_LOG_PATH, index=False)
print(f'已剔除 {n} 条旧 cold_zone 记录')
"
python3 scripts/build_signals_log.py --backfill-cold-zone --no-snapshot
python3 scripts/evaluate_signals.py
```

> ⚠️ **执行顺序（已修订）**：**A1 → A2 → A3 → A4**。
> **A3 与 A4 应合并成一次同步执行** —— 两者改的是同一个文件 `src/utils/market_data.py`，
> 分开同步会出现"文件版本不一致"的中间态。合并后的正确顺序：
> ① 同步全部代码 → ② `build_members_daily.py` → ③ `compute_weighted_returns.py --days 2000`
> → ④ `build_industry_daily_full.py --force-overwrite` → ⑤ `regime_classifier.py`
> → ⑥ 重算冷区台账 → ⑦ `evaluate_signals.py`。
> A2（regime）在 A3/A4 之后**再跑一次**（聚合口径变了）。

### B. 基础设施（一次性，优先级最高）

| # | 状态 | 内容 | 为什么重要 |
|:--:|:--:|---|---|
| B1 | `[ ]` | **确认源端在跑的代码版本**：对比关键文件 mtime / 行数 / md5 与本机这份 | 本机文件 mtime 统一为 2026-09-24，**无法确定源端是否同一版本**；不一致则 A1~A3 的同步可能是"用旧的覆盖新的" |
| B2 | `[ ]` | **建立代码到源端的同步通道**（git 推送 / rsync 脚本 / 手动拷贝均可，但要固化） | 目前 `~/.quant-sync` 只同步 `quant-data` **数据**，**不含代码**；本机改动没有任何机制到源端 |
| B3 | `[ ]` | 确认 `com.industry-radar.daily.plist` 的真实位置 | README 让用户 `launchctl bootstrap` 这个 plist，但**仓库内与本机 LaunchAgents 里都不存在** —— 文档照做会失败。需查清源端实际用什么调度 |
| B4 | `[ ]` | 修正路径大小写：`~/quant/industry-radar` → `~/Quant/industry-radar` | README 与 `cron/industry_review_daily.py` 回退路径写的是小写；macOS 不敏感所以能跑，跨到大小写敏感卷会挂 |
| B5 | `[ ]` | 记录源端 Python 解释器与依赖版本（`python3 -V`、`pip freeze`） | 本机 `/usr/bin/python3` **没有 pandas**，两边环境必然不同；出问题时需要这个基线 |

### C. 数据侧（一次性 / 定期）

| # | 状态 | 内容 | 说明 |
|:--:|:--:|---|---|
| C1 | `[ ]` | **补拉情绪数据**：`hsgt_top10` / `hk_hold` 的 2026-09-30 | 这正是当初阻断台账的那两个缺口（2×`SENTI_MISSING`） |
| C2 | `[ ]` | 补拉 `signals/industry_margin_daily.parquet` 最新数据（现停在 2026-09-23） | 滞后收益面板 4 个交易日 |
| C3 | ⏸ | 决定 `industry/mainline_state.json` 的去留 | **2026-10-03 复核：真孤儿** —— mtime 停在 **2026-07-26 18:12**（顶层键只有 `directions` / `last_update`），**仓库内零引用 + 数据根内零引用**，无脚本再生成。**建议删除**，但属产物去留，**待主人拍板**（选项：① 删 ② 补生成器 ③ 现状保留） |
| C4 | ⏸ | 历史缺数的追溯性检查 | `check_data_completeness.py` **只校验单个目标日**，历史任何一天缺数/坏值永远发现不了。**2026-10-03 已证明可定位**：用「台账里存的 signal `features` 对账当前重算值」这一技法，**成功定位到 D16 背后的真实静默坏值**（见 C5）。⏸ **决策（2026-10-03）：加「值异常」规则暂缓** —— 该规则**参与退出码**，可能让日报在健康日也报错，阈值口径未定。⚠️ 注意：A5 的写入护栏是**写入前**自检，**不参与退出码**，与本节互补、不是同一件事 |
| C5 | 🔶 | **重算 `daily_full` 的 2026-07-01~07-13 静默坏值段**（🔴 **P0-2**） | 该段 **131 个行业的 `breadth` 与 `breadth_ma13` 精确为 `0.0`**（1179 行全 0；同期 `avg_pct`/`total_amount`/`n_stocks` 正常）。根因 = `每日行业报告.command:97` 的 **`--days 60`** 窗口 × upsert「窗口外历史原样保留」⇒ 窗口滚过即**永久固化**；叠加 `market/daily` 的 2026-05~09 直到 **09-23 22:52** 才回填。**决策（2026-10-03）：本轮不重算**（护栏优先，见 A5）。重算命令 = `build_industry_daily_full.py --days 2000 --force-overwrite`（全量 1636 天）。✅ 已排除：坏值**当前不可复现**（任何窗口重算都不出全 0）；同链路 `industry_weighted_returns` **未受影响**（全历史零退化，实测） |

---

## 二、待决策（需要你拍板）

| # | 状态 | 议题 | 选项 / 取舍 | 来源 |
|:--:|:--:|---|---|---|
| D1 | `[ ]` | **regime 细分值不值得留** | ① 简化为 4~5 类（中间 6 类挤在 −0.7~+0.6pp 窄带）② 引入新独立结构判据（收益离散度变化率 / 龙头-跟随领先滞后）把"结构性"做实 | P2-1 §7 |
| D2 | `[ ]` | **mainline 最小成员数与跨期约束** | 当前无任何约束，实测已出现 `n_industries=1` 的"单行业主线" | 审计 P3 |
| D3 | `[ ]` | **tracker 的 24 个手工常量怎么收敛** | 参数比有效样本还多（n≈95），过拟合不可避免；收敛到配置文件 vs 重新拟合 vs 砍掉部分 | 审计 P2 |
| D4 | `[ ]` | **三区（中区/热区）要不要补** | 目前只实现冷区 1/3，中区/热区是硬编码占位 | 审计 P4 |
| D5 | ✅ | ~~新股/退市首日极值：缩尾 vs 延迟纳入~~ **已采用第三方案：剔除「无涨跌幅限制日」的观测**（2026-10-01）。理由：`stk_limit` 的占位值给出了精确判据，比缩尾更可解释、比延迟纳入覆盖面更全（同时覆盖复牌/退市整理）。 | P2-2 |
| D6 | `[ ]` | **宽度的 `min_periods` 是否由 5 提到 20** | 现状：上市 5–20 个交易日的个股用「短窗口均值」充当 MA20，语义不严格。实测影响很小（最大 \|Δbreadth\| 0.107，232/15,720 个行业-天），但属**定义变更**。已修的只是「NaN 不进分母」（纯纠错）。 | P2-3 |
| D7 | ✅ | **触发条件口径：扩张规则「恰好第3天 vs ≥3天」+ 冷区阈值「固定30% vs 动态」** —— **已拍板并执行** | **事件级去重后，上游的倾向性推荐被推翻**：ge3 相对生产的真实增量只有 **14 个事件（0.6%）**，其余 **99.2% 的"新增信号"是同一波扩张的重复采样**。事件级表现逐位相同（20日 +0.29 vs +0.29pp）；同事件内**晚入场更差**（第4天−第3天 = **−0.39pp, t=−6.36**）；逐年增量 7 年中 4 年为负。阈值模式影响极小（±0.01pp）。<br>**决策（2026-10-01）：取 C，已执行** —— `module_02_industry.py` 两处文案已改为「宽度连续扩张**恰好第 3 天**触发一次」并补注「冷区阈值随全市场宽度动态调制」（渲染已验证）。**A 否决**；**B 未执行**（可选，须与信号分离、不进台账）。详见 `docs/changes-2026-10-01-cold-zone-event-oos.md` | 事件级复核 |
| D17 | ✅ | **冷区信号重新校准**（2026-10-01 完成研究 → **已执行选项 ②：降级为观察项**） | 17 个候选过滤器 × IS/OOS 切分实测：**IS 上 17/17 全为正**，但 **IS 与 OOS 的 Pearson 相关仅 +0.18**（Spearman +0.30），**16/17 候选在 OOS 衰减**。唯一稳定的是 high 档（前宽<10%，IS +0.93 → OOS +0.89），但总样本仅 245 条 / 1636 天（日均 0.15 个），无法构成组合。唯一"两边事件级 t>2.3"的「前宽<15% × 10 日」，**按日聚类后 t 塌到 0.10~0.64**。→ **调参过程本身失效**（非"还没调好"）。<br>**已执行 ②（2026-10-01）**：`source` 注册表新增 `status=observational` 字段（唯一真相来源），评估报告新增「信号定位」章节 + 逐来源警示，日报第一章加定位标注，台账基线文档同步。**保留入账/展示/统计，但不作交易依据、不得据此调参**。① 继续调参已否决；③ 换假设重做需另行立项。<br>依据与回滚见 `docs/changes-2026-10-01-cold-zone-downgrade.md` | 重新校准研究 |
| D16 | ✅ | ~~`cold_zone` 台账条数对不上~~ **已定位根因（2026-10-03）—— 不是"台账过时"，是数据坏了。** 复现：在同一份 `daily_full` 上跑生产档（exactly3+dynamic）得 **2,204** 条，台账 **2,234** 条（差 30）；集合差 = 交集 2,144 / 仅台账有 90 / 仅重算有 60，**差异 100% 集中在 2026-07~08**（2020~2025 完全一致）。根因 = `daily_full` 在 **2026-07-01~07-13 连续 9 个交易日、131 个行业的 `breadth` 全为 0**（详见 **P0-2** 与 `docs/changes-2026-10-03-daily-full-breadth-anomaly.md`）。🔑 定位技法：**用台账里存的 signal `features` 与当前重算值对账** ⇒ 可反向定位到具体静默坏值。 | A/B 复核 |
| D8 | ✅ | ~~成分表口径不一致 → `daily_full` 全程有前视偏差~~ **已修（2026-10-01）**：`compute_concept_daily` 原生支持 PIT，`build_industry_daily_full.py` 默认强制使用当日真实成分、缺快照则拒绝执行。实测 `avg_pct` 最大修正 **5.78pp**、**39%** 的行业-天变化，**冷区信号约 10% 不同**。 | `docs/changes-2026-10-01-p2-pit-members.md` |
| D9 | ✅ | ~~历史窗口断层：1146 天是另一套口径~~ **已修（2026-10-01）**：`BASE=2024-09-24` 参数化为 `--base`（默认全历史）。快照 **490 天 → 1636 天 / 8,417,497 行**，490 天重叠期**逐条完全一致**（零回归）。 | `docs/changes-2026-10-01-p2-pit-members.md` |
| D10 | `[ ]` | **`industry_tracker.py` / `module_02_industry.py` 是否也切到 PIT**（🔴 P2-4 新发现） | 同一 `compute_concept_daily` 的另外两个调用点仍用静态成分。它们读近日窗口（30~60 天），影响远小于 `daily_full`；`module_02` 属报告主体、受「不改报告口径」铁律保护。 | P2-4 §7 |
| D11 | `[ ]` | **静态表（富途 5644 只）与 PIT（申万 5913 只）的成分来源差异** | 差 269 只未做系统性对账。PIT 取自申万链路，更接近官方行业指数口径。 | P2-4 §7 |
| D12 | ✅ | ~~`heat_ratio` 分母含未来信息（前视偏差）~~ **已修（2026-10-03）** —— 改为 `expanding(min_periods=lookback).mean()`（**截至当日**）。参考日=最后一天时与全样本均值**数值相同** ⇒ 对日更产物零影响；实测仅 `heat_ratio` 有 ≤1 ulp 浮点尾数差异，**全部判定列逐字一致**。详见 `docs/changes-2026-10-03-defect-fixes.md` |
| D13 | ✅ | ~~方向群聚类用了全样本相关性~~ **已修（2026-10-03）** —— `run_full_detection` 先定 `reference_date`，再把 pivot 截断为 `<= reference_date` 后聚类。实测 **23 个群成员逐字相同** ⇒ 生产零影响；回测历史日期时的前视偏差已消除 |
| D14 | ✅ | ~~`od_ratio` 在数据不足时误判「进攻型」~~ **已修（2026-10-03）** —— 样本不足（上涨日或下跌日 <10 天）即标 `style='未知'`；样本充足时行为**逐字一致**（含 `abs(defense)≤0.01 ⇒ inf` 的原语义保留）。当前参考日样本充足，故产物未变 |
| D15 | ⏸ | **`find_ambush_candidates`（埋伏候选）去留** —— **2026-10-03 复核：现状即选项 ③**（保留 + docstring 标注"未被调用 / `amt_below_mean` 未实现"）；仓库内**零调用**，不会误导。**是否删除（选项 ②）属功能存废，待主人拍板** |
| D18 | ✅ | **`industry_tracker.py` 拥挤度 None 处理不对称**（原 `:574` / `:581`）**已修（2026-10-03）** —— 当「当日拥挤度缺失（None）而前 5 日有值」时 `crowd_vals.sort()` 抛 `TypeError`。修法：当日缺失即不做 w₄ 分量（中性回退）。**基线代码在同一沙箱精确复现该报错（`:581`）⇒ 既有缺陷，非本次引入**；改动后 `exit=0`。详见 `docs/changes-2026-10-03-defect-fixes.md` |

---

## 三、已完成（归档）

| 日期 | 内容 | 提交 |
|---|---|---|
| 2026-10-01 | P0 版本控制 + P1 止血（5 文件） | `d59518d` |
| 2026-10-01 | P2-1 regime 口径修复（ddof 基准 + 阈值重校准） | `d3b024a` |
| 2026-10-01 | P2-2/P2-3 异常样本剔除 + 宽度分母修正 | 见 `git log` |
| 2026-10-01 | P2-4 全历史 PIT 成分 + 消除 `daily_full` 前视偏差 | 见 `git log` |
| 2026-10-01 | **P3 第一层：零风险修复** —— `mainline_detector` 死函数 NameError + 过滤文案、路径大小写 `quant→Quant`、README 阶段数/主线口径按代码统一、旧口径文档加警示标注、research 硬编码路径加注 | 见 `git log` |
| 2026-10-01 | **冷区规则 A/B 复核** —— 新增 `expansion_rule` / `cold_threshold_mode` 开关（默认即生产口径），全历史 4 组实测；零回归通过（2,204 条键集合与字段逐条一致） | 见 `docs/changes-2026-10-01-cold-zone-ab.md` |
| 2026-10-01 | **冷区事件级去重 + OOS 复核** —— 按「扩张起点日期」精确还原事件（自检：生产侧 信号数==事件数）；**ge3 真实增量仅 14 个事件（0.6%）**，99.2% 为重复采样；同事件内晚入场 −0.39pp(t=−6.36)；**否决策略 A**，新立 D17（信号自身有效性存疑） | 见 `docs/changes-2026-10-01-cold-zone-event-oos.md` |
| 2026-10-01 | **冷区重新校准研究** —— 17 个候选过滤器 × IS/OOS × 两级去重口径 × 4 持有期；**IS↔OOS 相关仅 +0.18，16/17 候选样本外衰减** → 判定「调参过程失效」 | 见 `docs/changes-2026-10-01-cold-zone-recalib.md` |
| 2026-10-01 | **D7 方案 C 执行** —— 冷区触发条件文案与代码对齐（「宽度连续扩张**恰好第 3 天**触发一次」+ 补注「冷区阈值随全市场宽度动态调制」）；`module_02_industry.py` 两分支渲染已验证；`signal-ledger.md` 同步 | 见 `git log` |
| 2026-10-01 | **D17 选项 ② 执行：冷区信号降级为「观察项」** —— `SOURCE_SPECS` 新增 `status` / `status_short` / `status_note`（定位的唯一真相来源）；评估报告新增「## 信号定位」章节 + 逐来源定位行与警示；日报第一章（冷区段落两个分支）加定位标注 + 「观察」档位消歧；`signal-ledger.md` 新增 §〇 定位声明。**✅ 仅文本呈现变更，触发逻辑与统计量零变化；沙箱零回归通过、真实数据根守卫通过** | 见 `docs/changes-2026-10-01-cold-zone-downgrade.md` |
| 2026-10-03 | **A1.1 修 `industry_tracker.py` 拥挤度 None 处理不对称**（D18 → 已修）—— 当日拥挤度缺失而前 5 日有值时 `crowd_vals.sort()` 抛 `TypeError`；修法 = 当日缺失即不做 w₄ 分量（中性回退 1.0）。**基线代码在同一沙箱精确复现该报错 ⇒ 既有缺陷，非本次引入**；改动后 `exit=0`。**同源 A/B 零回归** | 见 `docs/changes-2026-10-03-defect-fixes.md` |
| 2026-10-03 | **A2 修 `mainline_detector.py` 三处口径缺陷**（D12 `heat_ratio` 前视 / D13 全样本相关矩阵 / D14 `od_ratio` 数据不足误判进攻型）。实测 **23 个群成员逐字相同，逻辑零回归**；仅 `heat_ratio` 有 ≤1 ulp 浮点尾数差异 | 见 `docs/changes-2026-10-03-defect-fixes.md` |
| 2026-10-03 | **发现并定位 P0-2：`industry_daily_full` 静默历史坏值** —— 2026-07-01~07-13 连续 9 个交易日 131 个行业 `breadth` 全为 0。根因 = `--days 60` 窗口 × upsert「窗口外历史原样保留」× 上游 09-23 才回填行情。**由此一并解释 D16**（台账/重算差 30 条，差异 100% 集中在 2026-07~08）。新立 **A5 / C5** | 见 `docs/changes-2026-10-03-daily-full-breadth-anomaly.md` |
| 2026-10-03 | **P0-2 写入护栏落地**（决策：先加护栏、暂不动窗口、暂不重算）—— `build_industry_daily_full.py` 新增 `_preflight_guard()`（预热天数 / 数据起点豁免 / 0-NaN 退化中位数三判据）。**单元 8/8 通过；真实 `--days 60` 产物 sha 与基线逐行逐列一致（零回归）；`--days 10` 拒写 exit=1；真实数据根零写入**。另实测：坏值**当前不可复现**、`weighted_returns` **不受影响**。新立 **A7** | 见 `docs/changes-2026-10-03-daily-full-breadth-anomaly.md` §4.5/§5.1 |
