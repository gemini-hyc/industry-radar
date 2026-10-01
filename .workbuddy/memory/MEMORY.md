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

- 成分：日更聚合用 `industry_members_daily.parquet`（当日真实成分，消前视偏差）；
  `industry_members.parquet` 仅作行业全集/名称字典。
- 加权：`circ_mv` 流通市值加权（对标通达信 881xxx / 申万官方，平均绝对误差 ~0.10pp）。
- 宽度：`(close > MA20).mean()`（`research/ic_analysis_5factors.py` 里那套 `pctChg > 0` 是**另一套口径**，勿混）。
- 冷区信号生产规则：前 20 日均宽 ≤ 动态阈值 + 宽度连续扩张**恰好第 3 天**才触发（极度稀疏）。
  注：报告文案写的是"连续扩张 ≥ 3 天"（`module_02_industry.py:382/385`），**与代码不符**。

## 基础设施缺口（2026-10-01 查明，影响所有改动）

- **本项目无版本控制**：`/Users/hyc/Quant/industry-radar` 及上级目录都不是 git 仓库 → 改动无法回滚。
- **代码无同步通道**：`~/.quant-sync`（pull.sh / RUNBOOK）只同步 `quant-data` **数据**，不含代码。
  在本机改的代码目前没有机制到 Mac Studio。仓库内文件 mtime 统一为 2026-09-24，
  源端实际在跑的版本**待核对**。

## 已确证的高优先级缺陷（细节见 memory/2026-10-01.md）

1. **台账自 2026-09-24 停更**：根因 `cron/industry_review_daily.py:239-241` 完备性检查
   `rc==1` 时 `sys.exit(1)`，位于台账入账（`:247-271`）**之前** —— 情绪数据缺口连带阻断了台账更新。
2. **regime 三个"结构性"类是死类**：`rank_consistency` 随机基线在 **−0.25** 却按 0 中心设阈值
   （`regime_classifier.py:106/113` 与 `:147/151/163`）→ 全历史 `rc≥0.3` 零次。
3. **新股/退市首日极值未缩尾**：`compute_weighted_returns.py:62` 只 `dropna` 不 `clip`，
   实测 +206.59% 已进入行业聚合；新上市 <5 日个股 `ma20` 为 NaN 仍计入宽度分母（系统性压低 breadth）。
4. **可静默截断历史**：`build_industry_daily_full.py:50-65` 的 try/except 吞异常后仍整文件覆盖。
