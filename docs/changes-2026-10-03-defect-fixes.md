# 数据问题收口 · 缺陷修复（A1 + A2）

> 日期：2026-10-03 ｜ 基线 HEAD：`edc041d`
> 范围：**只改本仓库代码，不写 `~/quant-data`**；验证全部在同源沙箱完成。
> 相关：`docs/action-items.md`（D12 / D13 / D14 / P3-12）

---

## 0. 一句话

修掉 1 个"时序错位即崩溃"的健壮性缺陷 + 3 个**前视偏差**（与已修的 D8 同族），
并用**同源 A/B**证明：生产产物**零逻辑回归**。

---

## 1. 改动清单（2 个文件 / 4 处）

### 1.1 `scripts/industry_tracker.py` — 拥挤度 None 处理不对称（P3-12）

**位置**：`_update_score()`，原 `:574 / :581`

**缺陷**：原实现无条件把 `crowd_today` 放进待排序列表：

```python
crowd_vals = [crowd_today]          # crowd_today 可能是 None
for offset in range(1, 6):
    cv = self._get_crowd_percentile(back_date, code)
    if cv is not None:              # ← 回溯值判了 None
        crowd_vals.append(cv)
crowd_vals.sort()                   # ← 若列表里混入 None，此处 TypeError
```

当「**当日拥挤度缺失（None）而前 5 日有值**」时，`sort()` 抛
`TypeError: '<' not supported between instances of 'float' and 'NoneType'`。

**触发条件**：拥挤度因子脚本（`update_industry_factor.py`）先于行情数据更新造成的时序错位。

**修法**：当日缺失即不做 w₄ 分量（`crowd_baseline=None` → `_lambda_crowd_direction` 中性回退 `1.0`），
其余分量照常更新。正常路径（当日有值）代码逐字未变。

### 1.2 `scripts/mainline_detector.py` — 三处前视偏差 / 误判

| 编号 | 缺陷 | 原实现 | 修法 |
|---|---|---|---|
| **D12** | `heat_ratio` 分母含未来信息 | `hist_mean_amt = mean(amt_pct over [history_start, 最后一天])` —— **全样本均值**，历史日期的分母里混入了未来数据 | 改为 `expanding(min_periods=lookback).mean()`（**截至当日**）。参考日=最后一天时两者数值相同，故**对日更产物零影响** |
| **D13** | 方向群聚类用全样本相关矩阵 | `pivot.corr()` 用**全部日期**（含参考日之后） | 先定 `reference_date`，再把 pivot 截断为 `<= reference_date` 后聚类。生产时参考日=最后一天 ⇒ **截断后与原来逐字节一致** |
| **D14** | `od_ratio` 数据不足误判「进攻型」 | `offense = up.mean() if len(up)>10 else 0` —— 把**样本不足**回退成 **0**，于是 `defense=0 ⇒ od_ratio=inf ⇒ style='进攻型'` | 样本不足（上涨日或下跌日 <10 天）即标 **`style='未知'`**；样本充足时行为逐字一致（含 `abs(defense)≤0.01 ⇒ inf` 的**原语义保留**） |

**D12 的语义变化**：`hist_mean_amt` 由「每组一个常数」变为**逐日值**（PIT 的正确形态）。
CSV 只输出参考日那一行，故该列取值不变。

---

## 2. 验证方法：同源 A/B（**不是**拿沙箱比陈旧的真实根）

> 依据 `quant-sandbox-verify` §6e：真实数据根本身落后于一个未执行的步骤
> （`industry_members_daily` 只 **490 天** ⇒ P2-4 全历史 PIT 重建**尚未在源端执行**），
> 拿沙箱结果比真实根会产生一堆假回归。

**做法**：`git archive HEAD` 导出基线代码到 `/tmp/ir-repo-base`，
**同一份输入**分别用「基线代码」与「工作区代码」各跑一遍，逐产物比对。

**守卫**：每次运行前后用 `find -newer /tmp/guard_marker` 扫 `~/quant-data`
⇒ **零输出**，真实数据根全程未被写入。

### 2.1 `mainline_detector.py`（A2）

| 项 | 结果 |
|---|---|
| 聚类结果 | **23 个群，成员逐字相同** ✅（证明 D13 对生产零影响） |
| 产物行数 | 23 = 23 ✅ |
| `group_id` / `n_industries` / `is_mainline` / `stage` / `style` | **完全一致** ✅ |
| `excess_ma20` / `winrate` / `amt_ma20` / `hist_mean_amt` / `od_ratio` | **完全一致**（max_abs = 0） ✅ |
| `heat_ratio` | max_abs = **6.661e-16**（max_rel 6.2e-16）—— **1 ulp 浮点尾数** |

**判定**：`expanding().mean()` 与 `mean()` 数学等价、数值实现不同，产生 ≤1 ulp 的尾数差异。
**逻辑零回归**。

> 直接采用 D14 的"标记未知"分支在当前参考日**未被触发**（近 60 日涨跌日样本均 >10），
> 故 `style` 与基线逐字一致 —— 该分支的正确性由代码审查保证，**未做运行时触发**。

### 2.2 `industry_tracker.py`（A1）— 两个实验

**实验 1（正常路径零回归）**：`--start 2026-09-01`

| 产物 | 结果 |
|---|---|
| `tracker_report.csv` | **逐字节一致** ✅ |
| `tracker_state.json` | **逐字节一致** ✅ |

**实验 2（bug 复现 + 修复验证）**：把沙箱的 `ind_crowd_turnover_daily.parquet`
**删掉最新一日**（8,529,956 → 8,524,648 行），精确构造「当日 None + 前 5 日有值」：

| 代码 | 结果 |
|---|---|
| **基线代码** | `exit=1`，在 `industry_tracker.py:581` 抛 `TypeError: '<' not supported between instances of 'float' and 'NoneType'` ✅ **精确复现** |
| **改动后代码** | `exit=0`，零 TypeError，正常跑完 ✅ **修复有效** |

> 用基线代码在同一沙箱复现同一报错 ⇒ **既有缺陷，非本次引入**。

---

## 3. 影响评估

| 产物 | 是否变化 | 说明 |
|---|---|---|
| `mainline_detection.csv` | ⚠️ 仅 `heat_ratio` 的浮点尾数（≤1 ulp） | 所有判定列不变 |
| `tracker_report.csv` / `tracker_state.json` | ❌ 不变 | 逐字节一致 |
| 冷区信号 / 台账 | ❌ 不变 | 未触碰信号规则 |

**本次改动不引入任何需要重算产物的口径变更。**
（真正需要重算的是 P2-4 的 PIT —— 见 `docs/action-items.md` A4。）

---

## 4. 回滚

单文件级回滚，无数据迁移：

```bash
git checkout edc041d -- scripts/industry_tracker.py scripts/mainline_detector.py
```

---

## 5. 未做 / 待拍板

| 项 | 说明 |
|---|---|
| `find_ambush_candidates` 死函数（D15） | **保留现状**（已带 `⚠️ 未被调用` docstring）。是否删除属功能存废，**待拍板** |
| `mainline_detector` 最小成员数 / 跨期约束（D2） | 属**口径变更**，**待拍板**（阶段 B） |
| `industry_tracker` / `module_02` 是否切 PIT（D10） | **待拍板**（阶段 B） |
| `check_data_completeness.py` 加「值异常」检测 | 见 `docs/changes-2026-10-03-daily-full-breadth-anomaly.md` §5；改检测规则会影响日报退出码，**待拍板** |
