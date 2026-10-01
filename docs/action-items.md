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
| C3 | `[ ]` | 决定 `industry/mainline_state.json` 的去留（mtime 停在 2026-07-26，无脚本再生成） | 遗留产物，要么补生成器要么删 |
| C4 | `[ ]` | 历史缺数的追溯性检查 | `check_data_completeness.py` **只校验单个目标日**，历史任何一天缺数永远发现不了 —— 需要一次性扫描 |

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
| D7 | `[ ]` | **报告文案与代码不符**（"连续扩张 ≥3 天" vs "恰好第 3 天"） | 改文案 还是 改代码逻辑（已加进 `docs/action-items.md`） | 审计 P6 |
| D8 | ✅ | ~~成分表口径不一致 → `daily_full` 全程有前视偏差~~ **已修（2026-10-01）**：`compute_concept_daily` 原生支持 PIT，`build_industry_daily_full.py` 默认强制使用当日真实成分、缺快照则拒绝执行。实测 `avg_pct` 最大修正 **5.78pp**、**39%** 的行业-天变化，**冷区信号约 10% 不同**。 | `docs/changes-2026-10-01-p2-pit-members.md` |
| D9 | ✅ | ~~历史窗口断层：1146 天是另一套口径~~ **已修（2026-10-01）**：`BASE=2024-09-24` 参数化为 `--base`（默认全历史）。快照 **490 天 → 1636 天 / 8,417,497 行**，490 天重叠期**逐条完全一致**（零回归）。 | `docs/changes-2026-10-01-p2-pit-members.md` |
| D10 | `[ ]` | **`industry_tracker.py` / `module_02_industry.py` 是否也切到 PIT**（🔴 P2-4 新发现） | 同一 `compute_concept_daily` 的另外两个调用点仍用静态成分。它们读近日窗口（30~60 天），影响远小于 `daily_full`；`module_02` 属报告主体、受「不改报告口径」铁律保护。 | P2-4 §7 |
| D11 | `[ ]` | **静态表（富途 5644 只）与 PIT（申万 5913 只）的成分来源差异** | 差 269 只未做系统性对账。PIT 取自申万链路，更接近官方行业指数口径。 | P2-4 §7 |

---

## 三、已完成（归档）

| 日期 | 内容 | 提交 |
|---|---|---|
| 2026-10-01 | P0 版本控制 + P1 止血（5 文件） | `d59518d` |
| 2026-10-01 | P2-1 regime 口径修复（ddof 基准 + 阈值重校准） | `d3b024a` |
| 2026-10-01 | P2-2/P2-3 异常样本剔除 + 宽度分母修正 | 见 `git log` |
| 2026-10-01 | P2-4 全历史 PIT 成分 + 消除 `daily_full` 前视偏差 | 见 `git log` |
