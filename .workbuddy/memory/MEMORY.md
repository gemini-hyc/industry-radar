# Industry Radar · 项目长期备忘

## 机器角色（2026-10-01 实测确认，影响一切操作）

- 本机 = **黄宇澄的MacBook Air**（hostname `huangyudengdeMacBook-Air.local`），即 3-Mac 同步网中的**从机**。
- `/Users/hyc/quant-data` 在本机是**只读副本**：`~/.quant-sync/pull.sh` 主动从源端
  （Mac Studio，Tailscale `100.78.155.22` / LAN `192.168.0.105`）拉取。脚本注释原文：
  「本机的 quant-data 视为只读副本，本地修改会在下次拉取时被源端覆盖」。
- **结论：行业雷达日更不在本机跑。** 本机未注册 `com.industry-radar.daily`
  （`~/Library/LaunchAgents/` 只有 `com.quant.data-pull.plist`），项目内 `logs/` 目录也不存在
  —— 这是**正常的**，不要误判为"漏部署"。报告 19:30 的时间戳来自 Mac Studio 跑完后的同步。
- 在本机做开发/回测**不要写 `quant-data`**（会被覆盖，且可能污染下游）。
  回测产物应落在仓库内或 `/tmp`。

## 项目定位与路径

- 从 Hermes Quant（`~/.hermes/quant`）抽取的完整行业分析链路，独立演进；**本仓库是行业链路唯一演进方向**。
- 真实源码目录 = `/Users/hyc/Quant/industry-radar`（**大写 Q**）。README / cron 回退路径里写的是
  `~/quant/industry-radar`（小写），macOS 大小写不敏感所以能跑，但引用路径以大写为准。
- 解释器 = **系统 `/usr/bin/python3`**（Python 3.9.6，非 venv）；依赖 pandas/numpy/scipy/pyarrow/pyyaml，
  futu-api 仅行业成员拉取与富途同步需要。

## 链路（4 层）

1. **数据源（外部）**：openclaw `tushare-daily-fetch` 16:16 → `market/daily` + `daily_basic`；
   富途 OpenD（`localhost:11111`）→ 行业列表/成员；Tushare `index_member_all` → 申万成分（带 in/out 日期）
2. **计算层**：`build_members_daily.py`（PIT 成分快照，**新交易日必须跑**）→
   `compute_weighted_returns.py`（circ_mv 加权）/ `build_industry_daily_full.py`（等权 + 宽度 breadth）/
   `update_industry_factor.py`（拥挤度 ind_crowd，**个股级**）
3. **信号层**：`industry_tracker.py`（反转跟踪，四/五阶段）/ `mainline_detector.py`（方向群聚类）/
   `regime_classifier.py`（行情 regime）/ `build_nav_924.py`（924 起净值主表）
4. **编排与输出**：`cron/industry_review_daily.py` → `module_02` 三区报告 + `report_sections` 追加三章
   + `check_data_completeness.py` + 信号台账入账

## 改动铁律（源自 docs/system-audit-and-roadmap.md §5.2）

1. 所有路径走 `src/paths.py`，**禁止**硬编码 `/opt/data` 等容器路径。
2. **不改 `module_02` 的报告口径**；要加内容就在 `src/daily_review/report_sections.py` 里追加。
3. **任何新信号必须先入台账**（`signals/signals_log.parquet`）再评估 —— 没有台账数字的新信号等于没有证据。
4. 改口径（宽度定义 / 加权方式 / 基准）必须同步更新 `docs/`，并重跑台账评估。
5. 报告与台账更新必须包在 `try/except` 里，失败不能影响主报告。

## 已确认的口径要点

- 成分：**日更聚合一律用 `industry_members_daily.parquet`（PIT 当日真实成分，消前视偏差）**；
  `industry_members.parquet` 仅作行业全集/名称字典。
  **2026-10-01（P2-4）起**：PIT 快照覆盖**全历史 1636 天**（原硬编码 `BASE` 只 490 天）；
  `compute_concept_daily(…, members_daily=…)` 原生支持 PIT；`build_industry_daily_full.py`
  默认**强制**使用 PIT、缺快照即拒绝执行（逃生门 `--allow-static-fallback`）。
  ⚠️ **MA 必须先于成分过滤计算** —— 否则 PIT 下个股序列被截断（进入行业前/退出后的行情被丢），
  得出的不是该股真实的 20 日均线。
  ⚠️ `industry_tracker.py:168` / `module_02_industry.py:197` 仍用静态成分（读 30~60 天近日窗口，
  影响远小于 `daily_full`，且 `module_02` 受铁律 2 保护）—— 待决策。
- 加权：`circ_mv` 流通市值加权（对标通达信 881xxx / 申万官方，平均绝对误差 ~0.10pp）。
- 宽度：`(close > MA20).mean()`（`research/ic_analysis_5factors.py` 里那套 `pctChg > 0` 是**另一套口径**，勿混）。
  **2026-10-01 起**：MA 未成型的个股（`ma20` 为 NaN）**不进分母**（`above.where(ma20.notna())`）；
  且**当日无涨跌幅限制**的观测不计入聚合（见 `src/utils/data_quality.py`）。
- 冷区信号生产规则：前 20 日均宽 ≤ 动态阈值 + 宽度连续扩张**恰好第 3 天**才触发（极度稀疏）。
  注：报告文案写的是"连续扩张 ≥ 3 天"（`module_02_industry.py:382/385`），**与代码不符**。
- **regime 口径**（2026-10-01 起）：`rank_consistency` 锚点已修正为 0 = 随机排名水平，
  可解释（>0 比随机稳、<0 比随机乱），但**无预测力**，仅作状态描述。
- ⚠️ **评估信号时必须用超额**（`fwd_excess_*`），不要用绝对收益（`fwd_ret_*`）：
  绝对收益混入市场 beta，会让"市场强弱"伪装成"信号 alpha"（实测同一分组下
  绝对收益差 2.2pp，超额口径只剩 <0.8pp）。
- ⚠️ **评估信号时必须做两级去重，否则 t 值虚高**（2026-10-01 实测教训）：
  ① **事件级**：冷区信号的 `expansion_length` 是"连续递增步数"，起点可精确反推 ——
     `exp_start_date = 交易日历[signal_date 位置 − (expansion_days−1)]`，
     事件 ID = `(ind_code, exp_start_date)`。**同一波扩张每天共享同一起点日期**，
     是严格等价类，不用启发式聚类。实测 ge3 表面多 1,851 条信号（+46%），
     事件级只剩 **14 个新事件（0.6%）**，其余 99.2% 全是重复采样。
  ② **按日聚类**：同日往往多行业同时出信号（2204 条信号只分布在 498 天），
     先取每日均值再检验。生产冷区信号 20 日超额 **+0.29pp(t=2.14) → +0.01pp(t=0.06)**。
  两者都做才敢下结论；只做其一都会给出偏乐观的读数。

## 基础设施现状（2026-10-01）

- **版本控制已建立**：`git init` + 首次提交 `d59518d`（81 文件，含 `.workbuddy/memory`）；
  仓库级身份 `黄宇澄 <hyc@huangyudengdeMacBook-Air.local>`。改动前先确认工作区干净。
- **代码仍无自动同步通道**：`~/.quant-sync`（pull.sh / RUNBOOK）只同步 `quant-data` **数据**，不含代码。
  改动到 Mac Studio 靠手工（rsync / git / 拷贝），操作通知见 `docs/changes-2026-10-01-p0p1.md`。
- **本机 `/usr/bin/python3` 没有 pandas**（只有 `stock-doctor/.venv` 有）→ 本机跑不动链路。
  做验证用 `/Users/hyc/Quant/stock-doctor/.venv/bin/python`，并先把 `INDUSTRY_RADAR_DATA` 指向沙箱。
- ⚠️ `cron` 的子进程 env 曾丢 `INDUSTRY_RADAR_DATA`（**已修**）。未修时自定义数据根下会检查/写入
  错误数据集 —— 沙箱测试因此误写过一次真实数据根的 `completeness_status.json`。

## 缺陷台账（2026-10-01）

### 已修（P1，commit d59518d）
1. ✅ **台账停更** —— 入账移出 `sys.exit(1)` 短路，新增 `_blocks_ledger`（`SENTI_*` 类 CRITICAL 不阻断
   台账），退出码重定义为 0 健康 / 1 关键数据缺失且台账阻断 / 2 报告与台账已生成。
2. ✅ **两处静默截断历史的写入点** —— `build_industry_daily_full.py` / `compute_weighted_returns.py`
   改为拒绝写入 + 行数护栏（+ `--force-overwrite` 逃生门）。
3. ✅ `src/signals/evaluate.py`「平均收益(%)」列未格式化。
4. ✅ `build_signals_log.py` 默认快照日 `now()` → 最近交易日。

### 已修（P2-1，2026-10-01）
5. ✅ **regime 三个"结构性"类是死类** —— 根因是 `rolling_rank_corr` 的 **ddof 基准不一致**
   （分子 ddof=1、分母 ddof=0 → 随机基线漂移到 −0.25，实测 −0.2456 吻合）。
   已修 + 重校准阈值 + 新增 `rank_autocorr` + 加 `main()` + 写入行数护栏。8 类全部出现（最小 3.0%）。
   ⚠️ **但效度检验证明 `rank_consistency` 无预测力**（→次日延续性 r=−0.029），
   且 `top5_conc` 与 `up_pct` 共线 → **regime 只作【市场状态描述】用，不得作交易信号**。
   详见 `docs/changes-2026-10-01-p2-regime.md`。

### 已修（P2-2/P2-3，2026-10-01）
6. ✅ **新股/复牌/退市首日极值 + 宽度分母** —— 新增共享判据模块 **`src/utils/data_quality.py`**：
   以 `stk_limit` 的**占位值**（`up_limit>1e5` / `down_limit<0.02` = 当日无涨跌幅限制）为主判据，
   辅以「上市不足 10 自然日」与「preclose 失真」，命中且 `|pctChg|>11%` 才剔除；
   在 **4 个生产调用点**统一生效（`compute_weighted_returns` / `build_industry_daily_full` /
   `industry_tracker` / `module_02_industry`，均经 `compute_concept_daily(drop_abnormal=True)`）。
   ⚠️ **关键教训**：全市场 `|pctChg|>11%` 的 58,498 条里 **95.7% 是创业板/科创板正常 20cm 涨跌停**，
   **不该动**；`stock_basic.parquet` 滞后（缺 2026 年新股），**不能只靠 `list_date`**。
   宽度分母修正：`above = (close>ma20).where(ma20.notna())` —— 原实现下 `close>NaN→False`
   使次新股被当作「未站上均线」计入分母，系统性压低 breadth（预热期 524 个行业-天被误判为冷区）。
   实测影响：450 天窗口 368 个行业-天受影响，加权最大修正 **77.97pp**、等权 **66.40pp**；
   breadth 系统性 +0.19pp。详见 `docs/changes-2026-10-01-p2-extremes-width.md`。

### 已修（P2-4，2026-10-01）
7. ✅ **`daily_full` 前视偏差 + 成分快照历史断层**（同一条链条的两端）：
   - **前视**：`build_industry_daily_full.py` 原用**静态快照**回填全历史 → 2020-01-02 静态 5644 只
     vs 真实 3817 只，**混入 32% 当时尚未纳入该行业的个股**。因冷区信号的唯一输入就是本产物
     （`src/signals/sources.py:33`），偏差**直接注入旗舰信号** —— 同一 250 天窗口 PIT 340 条
     vs 静态 334 条、交集仅 320，**约 10% 的信号不同**（多为日期漂移而非增删）。
   - **断层**：`build_members_daily.py:16` 硬编码 `BASE=2024-09-24` 只是**日历过滤、非数据限制**
     （原料 `index_member_all_raw` 的 `in_date` 最早 1984-05-09，131 行业 1:1 映射零丢弃）。
     已参数化为 `--base`，**默认全历史**。
   - 重建结果：**1636 天 / 8,417,497 行**；490 天重叠期**逐条完全一致**（零回归）。
   - 实测影响：`avg_pct` 最大修正 **5.78pp**、**39%** 的行业-天变化；`breadth` 35% 变化；
     `n_stocks` 由「67 个常数」→「179 个逐日值」；MA 计算顺序影响 0.13%（最大 0.1，属正确性修正）。
   - ⚠️ **台账冷区信号必须重算**（旧 2234 条基于静态口径）：先剔除 `source=='cold_zone'` 再回填
     —— 台账唯一键 `(source, signal_date, ind_code)`，`upsert` **只增改不删**，不清旧记录会残留。
   - 详见 `docs/changes-2026-10-01-p2-pit-members.md`。

### 待修（P3）
8. **文案与代码不符**：`module_02_industry.py:382` 写"连续扩张 ≥ 3 天"，代码是"恰好第 3 天"。
9. **mainline 缺约束**：无最小成员数/跨期稳定性（当前主线 = 单行业「航海装备Ⅱ」）；
   另有死函数引用未定义变量、热度分母用全样本均值（前视）。
10. **三区只实现 1/3**：中区/热区仍是硬编码占位（`module_02_industry.py:594-604`）。

### 待决策项
- `industry_tracker.py` / `module_02_industry.py` 是否也切到 PIT 成分（P2-4 §7）。
- 静态表（富途 5644 只）与 PIT（申万 5913 只）的成分来源差异 269 只未对账。
- 宽度的 `min_periods` 是否由 5 提到 20（定义变更，实测影响很小）。
- regime 是否简化 4~5 类，或引入新的独立结构判据。
- 完整待办清单见 `docs/action-items.md`。
