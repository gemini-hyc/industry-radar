# P2-4 成分口径修复：消除前视偏差 + 打通全历史（2026-10-01）

> 本轮解决 2026-10-01 核查中发现的两个🔴结构性问题（原编号 **D8 / D9**）。
> 两者是同一根链条的两端：D9 让 PIT 成分快照可用于全历史，D8 才可能真正用上它。
>
> **本机角色**：MacBook Air = 开发机 + 只读数据副本。本文所有实测均在 `/tmp` 沙箱完成，
> 未写入 `quant-data`。**代码需你同步到 Mac Studio（源端）执行**，步骤见第 5 节。

---

## 1. 问题

### D9 —— 成分快照只覆盖 490 天，聚合产物却有 1636 天

`scripts/build_members_daily.py:16` 硬编码：

```python
BASE = pd.Timestamp("2024-09-24")
...
dates = [d for d in dates if d >= BASE]     # ← 日历过滤，不是数据限制
```

后果：

- PIT 成分快照 `industry_members_daily.parquet` 只有 **490 天**；
- 而 `industry_weighted_returns` / `industry_daily_full` 有 **1636 天**；
- → **2024-09-24 之前的 1146 天不可能由当前脚本生成**。upsert 的「保留窗口外历史」
  把一套来源不明的旧口径与当前口径**拼在了同一份文件里**，而下游无从分辨。

**关键澄清**：原料本身支持全历史。`index_member_all_raw.parquet` 自带
`in_date` / `out_date`（`in_date` 最早 **1984-05-09**，131 行业 1:1 映射，零丢弃）。
`BASE` 只是源码里的日历过滤 —— 属于**主动截断，而非缺少原料**。

### D8 —— `daily_full` 全程有前视偏差

| 产物 | 成分口径 | 谁在用 |
|---|---|---|
| `industry_weighted_returns` | PIT（`industry_members_daily`） | 台账前向收益、regime、tracker |
| `industry_daily_full` | **静态快照**（`industry_members`） | **冷区信号**、mainline、nav_924 |

`build_industry_daily_full.py:29` 调 `load_industry_members()` 拿到的是
**2026-09 的当前成分**，却用它回填 **2020 年**的行情 —— 等于用未来信息聚合过去。

实测（2020-01-02）：静态表 **5644 只** vs 当日真实成分 **3817 只** → **混入 32% 当时尚未纳入该行业的个股**。

而 `src/signals/sources.py:33` 显示：**冷区宽度反转信号（旗舰信号）的唯一输入就是 `industry_daily_full`**。
也就是说前视偏差不是"两个表口径不一致"这种洁癖问题，而是**直接注入旗舰信号的 alpha**。

---

## 2. 修复

### D9：`BASE` 参数化，默认全历史重建

`scripts/build_members_daily.py`

- `BASE` 常量删除 → `main(base=None)`，新增 `--base YYYY-MM-DD`，**默认 None = 全历史**；
- 新增**写入护栏**：新结果行数 < 旧文件 90% 时拒绝写入（防交易日历/原料异常误删历史）。

### D8：`compute_concept_daily` 原生支持 PIT

`src/utils/market_data.py`

- 新增 `members_daily` 参数（PIT 长表 `date, ind_code, stock_code`），传入即按**当日真实成分**聚合；
- 新增 `_members_relation()` 统一构造归属关系（PIT 按 `(date, code)` 关联，静态按 `code` 关联）；
- 新增 `load_industry_members_daily()` 加载器；
- `n_stocks` 在 PIT 下变为**逐日**的当日成员数（原为整段历史的常数）；
- **不传 `members_daily` 时行为与旧版完全一致** —— 报告主体 `module_02` 与跟踪池不受影响。

**⚠️ 一个必须保持正确的细节：MA 计算顺序**

均线必须在**个股自身完整序列**上计算，**之后**才做成分归属过滤。若先按成分过滤再算 MA，
PIT 模式下个股序列会被截断（进入行业前 / 退出行业后的行情被丢弃），得到的并非该股真实的
20 日均线。静态快照下两种顺序等价（成员表不随日期变），所以旧实现没有暴露这个问题。

实测这个顺序在 250 天窗口内影响 **41 / 32,750 = 0.13%** 的行业-天（最大 |Δ| 0.1）——
量级不大，但方向确定（截断会使 breadth 偏低），且属**正确性**问题，已在代码注释中固化。

### D8 配套：`build_industry_daily_full.py`

- 接入 `load_industry_members_daily()`，默认**必须**有 PIT 快照，缺失则 `exit 1` 并给出修复命令；
- 新增逃生门 `--allow-static-fallback`（显式回退静态快照，结果含前视偏差）；
- **upsert 护栏修订**：原判据是「总行数不得低于旧文件」，这在 PIT 口径下会**误报**
  —— 早期交易日只有 **127 个行业**（申万分类演化）而非 131，全量重建时总行数本就该下降。
  新判据改为「**窗口外历史必须逐条完整保留**」（`kept_keys ⊆ merged_keys`），这才是护栏的本意。

---

## 3. 实测影响

### 3.1 D9 全历史重建（零回归）

| 项 | 结果 |
|---|---|
| 覆盖 | **1636 天**（2020-01-02 ~ 2026-09-30），**8,417,497 行** |
| 原覆盖 | 490 天，2,823,022 行 |
| **490 天重叠期** | **2,823,022 vs 2,823,022 —— 逐条完全一致 ✅** |
| 新增 | 1146 天（2020-01-02 ~ 2024-09-23） |
| 每日规模 | 2020-01-02：127 行业 / 3817 只 → 2026-09-30：131 行业 / 5913 只 |
| 逐日成分股数 | min 3817 / q50 5383 / max 5913，**无缺口** |

### 3.2 D8 PIT vs 静态（250 天沙箱窗口，32,750 个行业-天）

| 列 | PIT 均值 | 静态均值 | Δ均值 | \|Δ\|最大 | 变化条数 |
|---|---|---|---|---|---|
| `avg_pct` | 0.00302 | 0.00436 | −0.00134 | **5.7754** | **12,797（39.1%）** |
| `breadth` | 0.44838 | 0.44871 | −0.00033 | 0.5000 | 11,439（34.9%） |
| `breadth_ma13` | 0.45442 | 0.45466 | −0.00024 | 0.5000 | 11,447（34.9%） |
| `stock_n` | 39.525 | 39.451 | +0.074 | 8 | 11,596 |
| `n_stocks` | 44.466 | 39.855 | **+4.611** | 36 | 27,113 |

**`n_stocks` 的形态变化最能说明问题**：静态下全历史只有 **67 个不同取值**（常数，退化），
PIT 下为 **179 个**（逐日真实）。

### 3.3 对旗舰信号的传导（同一 250 天窗口）

| 口径 | 冷区信号条数 | 行业数 |
|---|---|---|
| PIT | **340** | 119 |
| 静态 | 334 | 118 |
| 交集 | 320 | — |

**34 条信号不同（约占 10%）**，多为**日期漂移**而非增删，例如：
`SH.LIST0976` 由静态的 2025-10-29 提前到 PIT 的 2025-10-28；
2026-06-15 静态报 `SH.LIST0090`、PIT 报 `SH.LIST0031`。

→ **结论：台账中已入账的 2234 条冷区信号（基于静态口径）必须重算。** 见第 5 节。

### 3.4 护栏与幂等

| 用例 | 结果 |
|---|---|
| 缺 PIT 且未加逃生门 | `exit 1` + 打印修复命令 ✅ |
| `--allow-static-fallback` | 回退静态并警告 ✅ |
| 全量重跑幂等 | 32,750 → 32,750，关键列逐值一致 ✅ |
| 部分窗口 upsert（`--days 60`） | 保留窗口外 **24,890** 条，总行数不变 ✅ |
| `mainline_detector` | 退出码 0，列集合与生产一致 ✅ |

---

## 4. 改动文件

| 文件 | 改动 |
|---|---|
| `scripts/build_members_daily.py` | `BASE` 参数化（默认全历史）+ 写入护栏 |
| `src/utils/market_data.py` | `compute_concept_daily` 支持 PIT + MA 顺序修正 + `load_industry_members_daily()` |
| `scripts/build_industry_daily_full.py` | 接入 PIT（默认强制）+ `--allow-static-fallback` + upsert 护栏修订 |

`schema` 完全向后兼容：列集合与生产一致（仅 `n_stocks` 由常数变为逐日），
下游消费者只按列名取用，无需改动。

---

## 5. 源端（Mac Studio）执行步骤

> 前提：A1~A3 已同步。本轮在它们**之后**执行，且会**再次改变**行业聚合口径。

### 步骤 1：同步 3 个文件

```bash
# 本机 → 源端（在源端跑）
rsync -av hyc@<MacBook地址>:~/Quant/industry-radar/scripts/build_members_daily.py      ~/Quant/industry-radar/scripts/
rsync -av hyc@<MacBook地址>:~/Quant/industry-radar/src/utils/market_data.py            ~/Quant/industry-radar/src/utils/
rsync -av hyc@<MacBook地址>:~/Quant/industry-radar/scripts/build_industry_daily_full.py ~/Quant/industry-radar/scripts/
```
（或用你惯用的 git / 拷贝方式。）

### 步骤 2：重建全历史 PIT 成分快照

```bash
cd ~/Quant/industry-radar
python3 scripts/build_members_daily.py
# 预期输出：
#   起始日 = 全历史（未指定 --base）
#   交易日历: 1636 天 (2020-01-02 ~ 2026-09-30)
#   ✓ 保存 industry_members_daily.parquet: 8417497 行 131 行业 × 1636 天
#   2020-01-02: 127 行业 / 3817 只成分股
#   2026-09-30: 131 行业 / 5913 只成分股
```
> 若输出「❌ 拒绝写入」→ 说明交易日历或原料异常，**不要**强行绕过，先排查。

### 步骤 3：全量重建聚合产物（顺序不可颠倒）

```bash
python3 scripts/compute_weighted_returns.py --days 2000   # 现在能真正覆盖全历史
python3 scripts/build_industry_daily_full.py --force-overwrite
# 预期："PIT 成分快照: 8,417,497 行 / 1636 天" + 输出行数 ~208k（< 原 214,316，属正常）
python3 scripts/regime_classifier.py
python3 scripts/build_nav_924.py
```

### 步骤 4：重算冷区信号台账（**必须先清旧记录**）

台账唯一键是 `(source, signal_date, ind_code)`，`upsert` **只增改、不删除** ——
口径变了以后旧记录会残留，所以必须先剔除 `cold_zone` 来源的行：

```bash
cd ~/Quant/industry-radar
cp <数据根>/signals/signals_log.parquet <数据根>/signals/signals_log.parquet.bak   # 先备份

python3 -c "
import pandas as pd
from src.paths import SIGNALS_LOG_PATH
df = pd.read_parquet(SIGNALS_LOG_PATH)
n = int((df['source'] == 'cold_zone').sum())
df[df['source'] != 'cold_zone'].to_parquet(SIGNALS_LOG_PATH, index=False)
print(f'已剔除 {n} 条旧 cold_zone 记录（其余来源保留）')
"

python3 scripts/build_signals_log.py --backfill-cold-zone --no-snapshot
python3 scripts/evaluate_signals.py
```

> 预期：冷区信号条数与日期分布会有**约 10% 的变化**（见 3.3）。这是**修正**，不是异常。

---

## 6. 回滚

| 场景 | 操作 |
|---|---|
| 只想回退 `daily_full` 口径 | `python3 scripts/build_industry_daily_full.py --days 2000 --allow-static-fallback --force-overwrite` |
| 回退成分快照范围 | `python3 scripts/build_members_daily.py --base 2024-09-24` |
| 回退台账 | `cp <数据根>/signals/signals_log.parquet.bak <数据根>/signals/signals_log.parquet` |
| 代码回滚 | `git revert <本次提交>` |

---

## 7. 本轮未动（残留 / 待决策）

1. **`industry_tracker.py:168` 与 `module_02_industry.py:197` 仍用静态成分**
   —— 同一函数的另外两个调用点。它们读的是近日窗口（30~60 天），成分变动小、影响远低于
   `daily_full`；且 `module_02` 属报告主体，受「不改报告口径」铁律保护，故本轮未动。
   **待你决定是否一并切到 PIT。**
2. **`src/signals/sources.py:_load_crowding_panel` 用静态 `INDUSTRY_MEMBERS_PATH`**
   —— 同样是前视偏差，但拥挤度只是特征快照、不参与信号判据，优先级低。
3. **静态表与 PIT 的"当前成分"本身就不一致**：富途板块 5644 只 vs 申万映射 5913 只（差 269 只）。
   本轮的 PIT 取自申万链路（`index_member_all_raw`），更接近官方行业指数口径；
   但两套来源的差异未做系统性对账。
4. **D6 宽度 `min_periods`（5 → 20）** 仍未决 —— 属定义变更，与本轮口径修正正交。
