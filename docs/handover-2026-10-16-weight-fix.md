# 交接清单 2026-10-16 · 加权口径缺陷修复 + 估值事件锚点复核

> 覆盖会话：2026-10-15 ~ 2026-10-16
> 主题：① 追问「低估事件为何不回升还大亏」→ ② 挖出 production 数据缺陷 → ③ 修复上线 → ④ 闭环复验
> 关联：`docs/changes-2026-10-15-weighted-return-weight-day-fix.md`（变更记录）
> 　　：`research/prosperity/05ah_加权口径缺陷与事件研究复验.md`（研究证据链）

---

## 0. 一句话

**修好了一个让「行业收益」系统性虚增 15%/年 的生产级缺陷；顺带把「低估不会涨」这条结论在
三个 PIT 合法锚点 × 两种估值口径下钉死，并揭示其内部结构是「多数小赢 + 少数致命亏」的杠铃。**

---

## 1. 本次做了什么（时间线）

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 | 定位 B 口径「不回升、大亏」的机理：分位有三条抬升通道（价格涨 / 净资产缩水 / **窗口滚动**） | ✅ 已解释 |
| 2 | 三条 PIT 合法锚点对照（A 进→h日 / B 进→出区间 / C 出→h日） | ✅ 结论一致 |
| 3 | B 口径长尾拆解：证明「+1.27% 中位」是杠铃，非 edge | ✅ 已证伪 |
| 4 | 追查市场序列逐年异常 → 定位 `weighted_pct` 加权日错误 | ✅ 根因确认 |
| 5 | 修复 + 整表重建 + 下游重建 + 回归 | ✅ 已上线 |
| 6 | 用上线后的官方表闭环复验 05 事件研究 | ✅ 方向不变、幅度收敛 |

### ⛔ 一次被纠正的错误（须记住）

我曾把「**区间最低点**」列为候选锚点 —— 那是**后视镜**（要等区间走完才知道哪里是最低点），
属前视偏差，违反项目铁律。**只有阈值穿越日（首次 ≤20% 进 / 首次 ≥40% 出）是 PIT 合法的。**

---

## 2. 代码改动清单

| 文件 | 改动 | 可逆性 |
|---|---|---|
| `scripts/compute_weighted_returns.py` | 权重 `Σ(pctChg_t×circ_mv_t)` → **`Σ(pctChg_t×circ_mv_{t-1})`**；新增 `--weight-day prev/same`；窗口首日回补前 10 日历日 basic 作 lag；provenance 记 `weight_day` + `n_weight_fallback` | `--weight-day same` 可复现旧行为 |
| `src/daily_review/attention_radar.py` | 日报第五章 ② 源「全市场市值加权」**同一错误**，改前一日 `circ_mv`；补 `circ_mv>0` 过滤 | 同文件回滚 |
| `tests/test_invariants.py` | 网格判据：矩形满格 → **与 `industry_members_daily` 行业-天集合双向一致** | 同文件回滚 |
| `tests/baseline_snapshot.json` | 数值基线 `--update` 刷新 | 快照非失败判据 |

**未改动**：输出列结构（`date, ind_code, weighted_pct, circ_mv, stock_n`）不变；
`circ_mv` 仍为**当日**市值合计（size 语义），不随本次修复变化。

---

## 3. 数据产物（在 `~/quant-data`，**不在 git**）

| 产物 | 状态 |
|---|---|
| `industry/industry_weighted_returns.parquet` | ✅ 已重建：1642 天 × 131 行业 = **213,574** 行 |
| `industry/_backup_20261015_weight_fix/*.bak-sameday-weight` | 🟡 **保留**，回滚用 |
| `industry/industry_nav_924.parquet` | ✅ 已重建（`nav_w` 均值 173.1 → 148.3） |
| `industry/daily_regime.parquet` / `monthly_regime_summary.*` | ✅ 已重建（`mean_ret` 0.0801 → 0.0314 pp/日） |
| `industry/mainline_detection.csv` | ✅ 已重建（群数 22 → 20） |
| `industry/chain_mainline_detection.csv` | ✅ 已重建 |
| `reports/daily-analysis/2026-10-15_industry_review.md` | ✅ 已重生成（唯一 CRITICAL = `hk_hold` 未拉取，与本改动无关） |

### 回滚方式

```bash
cp ~/quant-data/industry/_backup_20261015_weight_fix/industry_weighted_returns.parquet.bak-sameday-weight \
   ~/quant-data/industry/industry_weighted_returns.parquet
# 然后重跑 build_nav_924 / regime_classifier / mainline_detector / chain_mainline_detector
```

---

## 4. 修复效果（对账中证全指 000985.CSI）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 日均偏离全指 | +0.0757pp（年化 **+18.5%**） | **+0.0323pp（年化 +7.9%）** |
| 全区间累计 | +317.0% | **+104.5%** |
| 单日行业收益 \|值\|>25% | **6 个**（最大 +92.01%） | **0 个** |
| 保真对照（旧口径复跑） | — | 3930/3930 行逐位一致，最大差 0.0 |

逐年（新表 vs 中证全指）：2020 +29.4%/+24.9%、2021 +16.0%/+6.2%、2022 −11.4%/−20.3%、
2023 +0.6%/−7.0%、2024 +19.0%/+7.4%、2025 +30.0%/+24.6%。

回归：**93 passed / 1 skipped**（与基线一致）。

---

## 5. 🔴 附带发现（非本次引入，但必须知道）

### 5.1 研究侧「市场基准」有同一个 bug

行业收益再聚合成市场序列时若用**今日** `circ_mv` 加权**今日**收益，再添 **+0.022pp/日** 偏差。
修正后残余偏离降到 **+0.0104pp/日（年化 +2.5%）**，可由「中证全指 = 自由流通市值加权 + 剔 ST」
与我们「流通市值加权 + 含 ST」的成分差异解释。

> ⛔ 凡是引用 `weighted_pct` 做「超额」的研究，**两处都要改**。收益表已改；研究脚本内自建的
> 市场序列需各自复核（`05*` 系列等）。

### 5.2 成员快照漂移（旧表 1528 行是陈旧残留）

缺的 1528 格 = **4 个行业 × 382 天**（电子化学品Ⅱ / 休闲食品 / 房地产服务 / 旅游零售Ⅱ）。
上层 `index_member_all_raw` 里这 4 个 SW 二级最早 `in_date = 20210730`（SW2021 分类改革），
此前本就不存在 ⇒ **旧表 2020 年那批行才是错的**。用旧口径重跑同样缺 ⇒ 与权重无关。
新表 vs 成员快照：**零缺零余**。

---

## 6. 研究结论（可引用数字）

### 6.1 三条 PIT 合法锚点（PB）

| 口径 | n | 中位 | 均值 | 胜率 | 聚类 t | p |
|---|---|---|---|---|---|---|
| A 进→120日 | 694 | −3.50% | −1.41% | 39.5% | −3.22 | 0.002 |
| B 进→出 区间持有 | 726 | +1.27% | **+0.08%** | 58.3% | **−0.48** | **0.633** |
| C 出→120日 | 685 | −3.33% | −1.18% | 38.1% | −2.96 | 0.004 |

⇒ **结论对 PIT 合法锚点不敏感**：「低估不会涨」三条口径一致成立。

### 6.2 B 口径的「正」是杠铃，不是 edge

| 持有天数 | n | 占比 | 中位 | 胜率 |
|---|---|---|---|---|
| ≤20 | 306 | 42.1% | +2.08% | 71.6% |
| 21–60 | 207 | 28.5% | +2.19% | 63.8% |
| 61–120 | 87 | 12.0% | +1.30% | 52.9% |
| 121–250 | 73 | 10.1% | **−6.79%** | 24.7% |
| 251–500 | 38 | 5.2% | **−11.05%** | 21.1% |
| >500 | 15 | 2.1% | **−35.56%** | **0.0%** |

82.6% 快速回升小赢；17.4% 长期不回升大亏（持 >500 天胜率 0%）。**致命亏事前无法识别**。
> ⚠️ 我曾口误说「长尾抬高中位」，实际相反：剔 >250 天后中位反而升到 +1.75%。

### 6.3 修正口径后的权威数字（`05aj`）

保真交叉：`旧表/今日基准` 跑出 h=120 = **−3.50%**，与 05 原报告逐位一致 ✅

| 组合 | h=120 | h=250 |
|---|---|---|
| 旧表 · 今日加权基准（原口径） | −3.50% / 胜39.5% | −5.22% |
| **新表 · 前日加权基准（全修）** | **−1.19%** / 胜45.1% | −1.63% |

- 事件同日增量 h=120 = **−0.04%**（t=+1.64, p=0.107）⇒ 不含增量信息
- 日历时间组合（唯一无偏检验）**t=+1.46 不显著** ⇒ 仍不可交易
- 修正幅度约 **2/3 来自口径缺陷**

---

## 7. ⛔ 待办（下一棒必须处理）

| # | 事项 | 优先级 | 说明 |
|---|---|---|---|
| 1 | **01~09 报告「超额」数字重跑** | 🔴 高 | 方向多半保留、幅度会变；当前数字**不可再引用**。优先做 05/06 两条事件研究 |
| 2 | **h=250 正向增量复核** | 🔴 高 | 新表 h=250 同日增量 **+0.98%, t=+2.76, p=0.008** —— 250 日窗口重叠严重，**必须先做非重叠抽样**才能判定，现记为待验未下结论 |
| 3 | 研究脚本内自建市场序列改前日加权 | 🟡 中 | `05*` 系列等，约再 −0.022pp/日 |
| 4 | 其余「历史某天绝对值」类引用注明口径日期 | 🟡 中 | 成员漂移会让旧值与新重建值不同 |
| 5 | 从机（MacBook / MacMini）同步 | 🟡 中 | 本次只改了 Mac Studio 源端；从机需 rsync 拉取新表 |
| 6 | `report_rc`（券商盈利预测）数据可用性复查 | 🟢 低 | 唯一符合「价格之外且领先价格」的候选，侦察时为空文件 |

---

## 8. 新增文件清单

**研究脚本（只读复刻，未改任何既有产物）**
```
research/prosperity/05x_entry_debug.py            # 事件锚点复刻验证
research/prosperity/05y_anchor_pit_compare.py     # 三条 PIT 锚点对照
research/prosperity/05z_b_hold_decomp.py          # B 口径长尾拆解
research/prosperity/05aa_exit_mechanism_decomp.py # 出场机制拆解
research/prosperity/05ab_excess_bias_check.py     # 复利口径偏差体检
research/prosperity/05ac_baseline_check.py        # 无条件基准
research/prosperity/05ad_samedate_control.py      # 同日截面控制
research/prosperity/05ae_calendar_portfolio.py    # 日历时间组合（唯一无偏）
research/prosperity/05af_reweight_returns.py      # 重建修正收益序列
research/prosperity/05ag_buggy_vs_fixed.py        # 缺陷复验
research/prosperity/05aj_official_recheck.py      # 官方表闭环复验
```

**报告**
```
research/prosperity/05x_锚点验证说明.md
research/prosperity/05y_锚点对照结果.md
research/prosperity/05z_B长尾拆解与修正.md
research/prosperity/05ah_加权口径缺陷与事件研究复验.md
docs/changes-2026-10-15-weighted-return-weight-day-fix.md
docs/handover-2026-10-15-research.md
docs/handover-2026-10-16-weight-fix.md   ← 本文件
```

**CSV 明细**（共 ~2MB，均已入库；`research/**/*.parquet` 按 `.gitignore` 不入库）
```
05x_low_events.csv, 05y_anchor_events_{pb,pe_ttm,all}.csv,
05aa_exit_decomp_pb.csv, 05ab_excess_bias_check.csv, 05ac_event_vs_baseline.csv,
05ad_A_increment.csv, 05ad_sameDate_control.csv, 05ae_calendar_portfolio.csv,
05ag_events_fixed.csv, 05ag_buggy_vs_fixed.csv,
05ai_rebuild_validation.csv, 05aj_events_new.csv, 05aj_buggy_vs_fixed_official.csv
```

---

## 9. 方法论教训（已固化进项目记忆）

1. **中位数也会骗人**：评估**策略**要看**均值 + 聚类 t**（B 口径 +1.27% 中位 vs ≈0 均值，
   是标准的"中位数高估 edge"陷阱）；评估**组统计**才用中位数。
2. **「按出场结果切窗口」= 选择效应**：B 口径同日截面增量 +2.23%/t=5.64 看似显著，
   日历时间 t=0.12 ⇒ 全是选择效应。事件研究最终判据必须是**日历时间组合**。
3. **后视镜禁令**：区间最低点 / 末日等需等区间走完才可知的量，一律不得作锚点。
4. **加权构造一律「权重在前、收益在后」**：`Σ w_{t-1}·r_t` 才是可实现的持有收益。
