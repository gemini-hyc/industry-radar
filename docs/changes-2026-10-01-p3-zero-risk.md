# 第一层：零风险修复（2026-10-01）

> 本轮 = 用户批准的「2+3 并行」中的 **2**（在本机继续攒零风险改动）。
> 全部改动**对生产产物零影响**，因此不构成对源端的紧急同步要求。
> 执行机：MacBook Air（从机）；验证全部在 `/tmp/ir-ab-test` 沙箱，
> 真实数据根经 `find -newer` 确认**零文件被写入**。

---

## 一、改动清单

### A. 真 bug（会出错，不是风格问题）

| 文件 | 改动 | 依据 |
|---|---|---|
| `scripts/mainline_detector.py` | **`find_ambush_candidates` 补上缺失的 `returns` 参数**。原函数体 `:271-272` 引用 `returns`，而它不在参数表（参数只有 `group_signals, corr_matrix, name_map, mainline_gid, date, ...`）→ **一旦被调用立即 `NameError`**；且 `run_full_detection` 从未调用它，所以一直没暴露。同时删掉无用的中间赋值 `idf_nc = group_signals`，并在 docstring 标注「当前未接入日更链路，结果不进任何产物」（去留见 D15） | AST 静态检查 + 人工核对 |
| `scripts/mainline_detector.py` | **`:305/:307` 的过滤声明与代码不符**：代码是 `valid_groups = groups`（**无任何过滤**，上一行注释也写着"不过滤大小"），但 `:307` 打印"其中 N 个有效(3-30行业)" → 日志声称有过滤、实际没有。已改为如实陈述，并注明"是否加约束见 D2" | 代码核对 |

### B. 隐患（当前环境无害，跨环境会挂）

| 文件 | 改动 | 依据 |
|---|---|---|
| `cron/industry_review_daily.py:29` | fallback 路径 `/Users/hyc/quant/industry-radar` → `/Users/hyc/Quant/industry-radar` | 真实目录是大写 `Quant`。macOS 大小写不敏感所以能跑，跨到大小写敏感卷会失败 |
| `README.md` | 同上（`cd ~/quant/industry-radar` → `~/Quant/industry-radar`） | 同上 |

### C. 文档与代码对齐（执行「**文档与代码不符时以代码为准**」这条项目铁律）

| 文件 | 改动 | 原文档 | 代码事实 |
|---|---|---|---|
| `README.md:32` + 口径备忘节 | 阶段数统一为**六阶段** | 链路图写"四阶段"；口径备忘写"五阶段"却列了 6 个 | `industry_tracker.py` 的 `phase` 取值为 `sleeping / early / growing / mature / alarming / decaying`。"四阶段"的来源是**跟踪池只统计 early/growing/mature/alarming 四态**（`sleeping`/`decaying` 是进出态）—— 已在文档中写明这个区分 |
| `README.md` 口径备忘节 | 主线口径按代码改写 | "31 申万一级方向群，主线 = 热度>1.3x + **超额60d**>0，**确认窗口法**判瓦解" | 方向群由**相关系数聚类**生成（`cluster_industries(corr_threshold=0.6)`，群数随数据变化，**不是固定 31 个申万一级群**）；主线判据 = 热度 ≥1.3x **且** **20 日**超额 ≥0.3%/日 **且** 20 日超额胜率 ≥0.6；**代码中没有"确认窗口法"** |
| `README.md` 口径备忘节 | 「131 行业」加注 | — | 早期交易日（申万分类演化期）实际只有 **127** 个行业有成分，属正常，不是缺数 |
| `README.md` 顶部 | 加「文档与代码不符时一律以代码为准 / 历史数字不可与台账直接比较」 | — | 项目铁律的显式化 |
| `docs/module_02_rebuild.md` | 顶部横幅 + §3.1 / §3.2 / §3.5 逐节标注 | §3.1 "固定 30% 阈值"、"连续扩张 ≥3 天"；§3.2 四维度 2–13 分评分；§3.5 +1.65%/+3.26%/+6.71% | ① 冷区阈值是**动态调制**；② 扩张判据是**恰好第 3 天**；③ 四维度评分已于 v3（2026-07-29）**废弃**，生产只用前宽深度分级，且「涨停情绪」维度**完全未计算**；④ 回测数字为历史口径 |
| `docs/module-02-backtest-findings.md` | 顶部加历史口径警示（含三处口径差说明） | — | 同上；并注明第五节「已证伪方向」的**方向性结论仍有效**，但具体数字不可引用 |
| `research/backtest_two_board_limit.py:16-20` | 硬编码 `/opt/data/...` 加注说明 | 直接写死容器路径 | 违反"路径一律走 `src/paths.py`"铁律；但 `research/` 已冻结，**保留原值仅作存档**，加注说明直接运行会失败 |
| `docs/system-audit-and-roadmap.md` | P7 标记「部分处置」；P10 标记「已修」 | — | 本轮核对结果 |

### D. 新增能力（为 A/B 复核服务，默认不改变行为）

| 文件 | 改动 |
|---|---|
| `src/daily_review/factor_width_reversal.py` | `detect_signals` 新增 `expansion_rule`（`exactly3` \| `ge3`）与 `cold_threshold_mode`（`dynamic` \| `fixed30`）两个开关，**默认值 = 当前生产口径**；非法取值 `raise ValueError`（避免拼错后静默走默认分支）；docstring 逐项说明。另新增 `_EXPANSION_RULES` / `_THRESHOLD_MODES` 常量 |
| `src/signals/sources.py` | `cold_zone_history` 新增同名透传参数，默认值同上 |

> 这两个开关的意义：把「文档口径」与「生产口径」变成**可切换的一次调用**，
> 让口径之争从此可以用数据回答，而不是靠读文档猜。见 `docs/archive/changes-2026-10-01-cold-zone-ab.md`。

---

## 二、验证

| 项 | 方法 | 结果 |
|---|---|---|
| 语法 | `py_compile` × 3 个改动的 .py | ✅ 全部通过 |
| 死函数修复 | AST 扫描 `find_ambush_candidates`：参数表是否含 `returns`、函数体内是否有未定义的全局引用 | ✅ `returns` 已在参数表；无未定义引用 |
| **零回归** | 全历史 1,636 个交易日，旧实现（`git show HEAD:`）vs 新实现（默认参数），同一份数据、同一个 `cold_zone_history` | ✅ **2,204 vs 2,204**；键集合完全一致；`score`/`tier`/`obs_type` 零不一致 |
| 真实数据根 | 跑前 `touch /tmp/guard_marker`，跑后 `find $SRC/{industry,signals} -type f -newer /tmp/guard_marker` | ✅ **零输出** |

> 零回归这一项同时覆盖了 C 组之外**唯一**能影响运行的改动（D 组的两个开关），
> 因此「本轮对产物零影响」是被验证过的结论，不是推断。

---

## 三、是否需要源端同步

**不紧急，但建议随下一轮 A1~A4 一并带上。**

- 本轮改动对产物**零影响**（已零回归验证），源端不同步也不会产生错误结果。
- 但代码已经分叉：本机与源端的 `factor_width_reversal.py` / `sources.py` /
  `mainline_detector.py` 内容不同。若将来在源端做 A/B 或调口径，
  存在"**用旧的覆盖新的**"风险（这正是 B1 担心的场景）。
- 建议：在 A1~A4 那次同步里，把本轮 9 个文件一起 rsync 过去（纯代码 + 文档，无数据依赖）。

---

## 四、本轮新发现（未修，已记入 `docs/action-items.md`）

| # | 位置 | 问题 | 为什么没顺手改 |
|---|---|---|---|
| D12 | `mainline_detector.py:136-140` | `heat_ratio` 分母取 `history_start='2020-01-01'` 起的**全样本均值**，含未来信息（**前视偏差**），且直接决定 `is_mainline` / `stage` | 属"纠错型**行为变更**"（会改主线判定与产物），需拍板 |
| D13 | `mainline_detector.py:50` | 方向群聚类 `pivot.corr()` 用全样本相关性 | 对生产（只看最新日）影响为 0；仅在**回测主线**时才注入偏差 → 做主线回测前必须先修 |
| D14 | `mainline_detector.py:203-216` | `od_ratio` 在上涨/下跌日样本不足时回退 0 → `inf` → `style='进攻型'` | 会改变 `style` 列输出，需拍板 |
| D15 | `mainline_detector.py:238-290` | `find_ambush_candidates` 从未被调用；`amt_below_mean` 声明了但未实现 | 去留是产品决策（接入 / 删除 / 保持现状），已先修掉 NameError 并加 docstring 标注 |

---

## 五、复现

```bash
cd /Users/hyc/Quant/industry-radar
git diff HEAD~1 --stat        # 本轮改动清单（若已提交）

# 零回归验证（缩短窗口示例）
INDUSTRY_RADAR_DATA=/tmp/ir-ab-test \
  /Users/hyc/Quant/stock-doctor/.venv/bin/python /tmp/ab_cold_zone.py --days 250 --verify-only
```
