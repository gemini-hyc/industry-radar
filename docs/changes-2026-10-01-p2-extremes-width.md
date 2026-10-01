# 异常样本剔除 + 宽度分母修正（P2-2 / P2-3）

> 日期：2026-10-01 ｜ 对应审计文档「新股/退市首日极值未缩尾」「宽度分母含 NaN」
> 前置：P0+P1 见 `docs/changes-2026-10-01-p0p1.md`；P2-1 见 `docs/changes-2026-10-01-p2-regime.md`

---

## TL;DR

行业聚合（加权涨跌幅 / 等权涨跌幅 / 宽度 / 成交额）此前**包含「当日无涨跌幅限制」的观测**
—— 新股上市首日、长期停牌复牌、退市整理期。这些观测单日涨跌幅可达 **+1942% / −96%**，
在行业内制造出虚假波动。

**实测**：450 天窗口内 368 个「行业-天」受影响，加权涨跌幅最大修正 **77.97pp**
（2024-10-11 包装印刷 `+75.66% → −2.31%`）；等权涨跌幅最大修正 **66.40pp**。

**修复**：新增共享判据模块 `src/utils/data_quality.py`，在**4 个生产调用点**统一生效；
同时修正宽度分母（MA 未成型的个股不再被当作「未站上均线」计入）。

---

## 一、问题诊断

### 1.1 极端值的三个来源（实测）

对全历史 809 万条行情 + 涨跌停表逐日核查：

| 来源 | 特征 | 实测样本 |
|:---|:---|:---|
| **新股上市首日/次新期** | 无涨跌幅限制，涨幅可达 +1942% | 读客文化 +1942%、长联科技 +1704%、强邦新材 +1739% |
| **长期停牌复牌 / 恢复上市** | 无涨跌幅限制 | 盈方微 +488%、盐湖股份 +306%、皇台酒业 +315% |
| **退市整理期** | 首日无限制 | \*ST元道 −72.51%、\*ST清越 −20.55% |

**关键澄清**：全市场 `|pctChg| > 11%` 共 58,498 条，但其中 **95.7% 是创业板/科创板/北交所的
正常 20cm/30cm 涨跌停**，**不该动**。真正需要处理的只是「无涨跌幅限制日」那部分。

### 1.2 `stk_limit` 提供了精确判据

数据根里已有全历史 1636 天的 `stk_limit/`（每日涨跌停价）。其**占位值**
（`up_limit = 1000000`、`down_limit = 0.01`）正是「当日无涨跌幅限制」的标记：

```
2026-09-30 sz.001246 N力勤资源  pctChg=+206.59%
  stk_limit: up=999999.999  down=0.01   → 无涨跌幅限制 ✓
```

全历史占位值 6,824 条（0.0837%），490 天成分窗口内 1,008 条。

### 1.3 为什么不能只用 `list_date`

`stock_basic.parquet` 只有 5,569 只，**滞后于行情**（实测缺 N力勤资源等 2026 年新股）。
在 490 天窗口内，「仅 `stk_limit` 能抓到而 `list_date` 漏掉」的样本有 **60 条**，
其中 38 条 `|pctChg| > 11%`，包括 `sz.301716 +653.16%`、`sz.002898 −95.78%`。

**结论：必须以 `stk_limit` 为主判据，`list_date` 仅作补充。**

### 1.4 宽度分母问题

`src/utils/market_data.py`（原实现）：

```python
con_market["above_ma20"] = (con_market["close"] > con_market["ma20"]).astype(int)
breadth_daily = con_market.groupby("date")["above_ma20"].mean()
```

`ma20` 为 NaN 时（个股上市不足 5 个交易日）`close > NaN → False → 0`，
该股被当作「未站上均线」**计入分母**，系统性压低 breadth。
实测预热期 524 个「行业-天」被误判为冷区（`breadth ≤ 0.30`）。

---

## 二、修复内容

### 2.1 新增 `src/utils/data_quality.py`

三个判据取并集，**命中任一 且 `|pctChg| > 11%`** 才剔除：

| 判据 | 规则 | 作用 |
|:---|:---|:---|
| **A. 无涨跌幅限制日** | `stk_limit.up_limit > 1e5` 或 `down_limit < 0.02` | **主判据**，精度最高 |
| **B. 次新股** | 上市不足 10 个自然日（依赖 `stock_basic.list_date`） | 补充，覆盖 `stk_limit` 缺失 |
| **C. 价格失真** | `preclose ≤ 0.02` 或 `\|pctChg\| > 300` | 兜底 |

`min_abs_pct = 11.0` 是「只剔除有实质影响的观测」的闸门（主板涨跌停 ±10% 上方留 1pp 容差）。
**创业板/科创板 ±20% 的正常涨跌停不会被误伤** —— 它们只有在同时命中 A/B/C 时才会被剔除。

### 2.2 生效点（4 个生产调用点，一处修改全链路生效）

| 调用点 | 作用 |
|:---|:---|
| `scripts/compute_weighted_returns.py` | 加权涨跌幅 |
| `scripts/build_industry_daily_full.py` | 等权涨跌幅 + 宽度 + 成交额 |
| `scripts/industry_tracker.py` | 反转跟踪引擎 |
| `src/daily_review/module_02_industry.py` | 日报主体三区 |

均通过 `market_data.compute_concept_daily(..., drop_abnormal=True)`（新默认值）生效。

### 2.3 宽度分母修正

```python
above20 = con_market["close"] > con_market["ma20"]
con_market["above_ma20"] = above20.where(con_market["ma20"].notna())  # NaN 不进分母
```

MA13 同理。`breadth_dropna=True` 为新默认值，可用参数关闭以回滚。

### 2.4 `n_stocks` 语义保护

`n_stocks`（行业成员总数）改用**剔除前**的成员表统计，避免因子样本剔除而漂移。
`stock_n`（当日实际成分数）随之自然变化（均值 −0.006，可忽略）。

---

## 三、效果

### 3.1 各列的改动幅度（250 天窗口，32,750 个行业-天）

| 列 | 旧均值 | 新均值 | Δ均值 | 最大 \|Δ\| | Δ > 0.01 的条目 |
|:---|--:|--:|--:|--:|--:|
| `breadth` | 0.44133 | 0.44871 | **+0.000194** | 0.10000 | 213 |
| `breadth_ma13` | 0.44722 | 0.45466 | +0.000169 | 0.10000 | 194 |
| `avg_pct`（等权） | 0.01789 | 0.00436 | −0.013534 | **66.40462** | 194 |
| `stock_n` | 39.45731 | 39.45136 | −0.005954 | 2 | 194 |

**`breadth` 系统性上移 +0.19pp** —— 正是「不再把次新股当作未站上均线」的方向。

### 3.2 加权涨跌幅

| 指标 | 结果 |
|:---|:---|
| 剔除样本 | 294 / 1,371,303（0.021%） |
| 与生产数据对比（32,750 个行业-天） | 有差异 216 条（**0.660%**） |
| \|差异\| 分位 | 中位 0.134pp ｜ q90 2.011pp ｜ max **13.40pp** |
| 差异 > 0.5pp 的 | 53 条；> 2pp 的 23 条 |
| `max \|weighted_pct\|` | 生产数据含极值 → 新口径 **13.63%**，`\|wp\|>15%` 归零 |
| 全窗口（490 天）最大修正 | **77.97pp** |

### 3.3 冷区判据（信号生产的核心）

| 指标 | 旧 | 新 |
|:---|--:|--:|
| `breadth ≤ 0.30` 占比 | 38.43% | 37.43% |
| 判定变化 | — | 526 条 / 32,750（1.606%） |

其中 **524 条是预热期**（`breadth` 由错误的 0 变为 NaN，不再被误判为冷区），
**真实的冷区判定翻转只有 1 条**。

### 3.4 宽度 NaN 的出现位置

524 个行业-天（= 4 天 × 131 行业），全部位于**数据窗口前 4 个交易日**
（此时所有个股 MA 均未成型）。**非预热期不产生 NaN。**

---

## 四、影响面

| 对象 | 变化 |
|:---|:---|
| `industry_weighted_returns.parquet` | 0.66% 的行业-天数值变化；极端值收敛 |
| `industry_daily_full.parquet` | `breadth` / `breadth_ma13` / `avg_pct` / `total_amount` 微变 |
| `industry_tracker.py` 的跟踪池 | 宽度输入变化 → **阶段判定可能变化** |
| 冷区信号（`factor_width_reversal`） | 冷区判定翻转 **1 条**（窗口内） |
| `daily_regime.parquet` | **本次不重算**（regime 依赖加权涨跌幅，需在源端按 §5 重跑） |
| 日报三区 | 宽度口径变化，展示值微调 |
| **schema** | **无变化**，消费者代码无需改动 |

---

## 五、源端执行步骤

```bash
cd ~/Quant/industry-radar        # 注意大写 Q

# 1) 同步 5 个文件
#    src/paths.py
#    src/utils/data_quality.py        （新增）
#    src/utils/market_data.py
#    scripts/compute_weighted_returns.py
#    （scripts/build_industry_daily_full.py 无逻辑改动，可一并同步）

# 2) 全量重算加权涨跌幅（必须 --days 足够覆盖，不能只用 --days 5）
python3 scripts/compute_weighted_returns.py --days 250

# 3) 全量重建行业日频（等权 + 宽度）
python3 scripts/build_industry_daily_full.py --days 250

# 4) 重算 regime（依赖加权涨跌幅）与台账评估
python3 scripts/regime_classifier.py
python3 scripts/evaluate_signals.py
```

**预期输出（步骤 2）**：

```
异常样本: 剔除 N / M 条（~0.02%）；其中 stk_limit 标记的无涨跌幅限制日 N 个「日×股」
✓ 保存: .../industry_weighted_returns.parquet
  行业数: 131
  日期范围: ... ~ 2026-09-30
```

**回滚**：`git revert`（P0 已建基线），或把 `compute_concept_daily(..., drop_abnormal=False,
breadth_dropna=False)` 显式传旧行为。

---

## 六、已知遗留

1. **`min_periods=5` 未改**：上市 5–20 个交易日的个股仍用「短窗口均值」充当 MA20
   （语义不严格）。实测改成 `min_periods=20` 的影响同样很小
   （最大 \|Δbreadth\| 0.107，232/15,720 个行业-天），但属**定义变更**，
   留待单独决策 —— 见 `docs/action-items.md` D6。
2. **`total_amount` 也随之变化**：异常股票（新股首日换手极高）的成交额被一并剔除。
   口径上一致（量价同源），但下游量比判断会有微小变化。
3. **`stk_limit` 占位值依赖数据源惯例**。若源端 Tushare 版本改变占位方式，
   判据 A 会静默失效。已在代码中用 `NO_LIMIT_UP` / `NO_LIMIT_DOWN` 常量集中定义，
   并在目录缺失时打印告警。

---

## 七、本次附带发现（**需单独决策，本次未动**）

### 7.1 成分表口径不一致 —— `daily_full` 全程有前视偏差

| 脚本 | 成分来源 | 口径 |
|:---|:---|:---|
| `compute_weighted_returns.py` | `industry_members_daily.parquet` | **PIT**（当日真实成分） |
| `build_industry_daily_full.py` | `industry_members.parquet` | **静态快照（有前视偏差）** |

`load_industry_members()` 读的是**当前**成分表，用它回溯全历史 → 未来才纳入的股票
会被算进过去的行业表现。**两份产物的成分口径不同**，而它们被同一批下游消费。

### 7.2 历史窗口断层

| 产物 | 覆盖 | 天数 |
|:---|:---|--:|
| `industry_members_daily.parquet` | 2024-09-24 ~ 2026-09-30 | **490** |
| `industry_weighted_returns.parquet` | 2020-01-02 ~ 2026-09-30 | **1636** |
| `industry_daily_full.parquet` | 2020-01-02 ~ 2026-09-30 | **1636** |

`build_members_daily.py:16` 硬编码 `BASE = 2024-09-24`，因此 **2024-09-24 之前的
1,146 个交易日不可能由当前脚本生成** → 那段历史来自**更早的另一套口径**。
`compute_weighted_returns.py` 的「按日期 upsert 保留窗口外历史」假设的是同口径，
实际上**新旧口径被拼在同一份文件里**。

**建议**：要么把 `index_member_all_raw.parquet` 的 `in_date/out_date` 回溯到 2020 年
重建 PIT 成分（该表本身带 in/out 日期，具备条件），要么明确截断历史到 2024-09-24。
一并解决 7.1 与 7.2。
