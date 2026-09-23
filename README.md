# Industry Radar — 行业分析链路

从 Hermes Quant（`~/.hermes/quant`）抽取的完整行业分析链路，独立演进。
定位：行业板块的数据拉取 → 因子计算 → 反转跟踪 → 主线择时 → 每日行业复盘报告 → 富途分组同步。

> **接手必读**：[`docs/system-audit-and-roadmap.md`](docs/system-audit-and-roadmap.md) —— 系统问题审计与改造路线（2026-09-23）。
> 含：系统现状速查 / 11 项问题（附证据位置）/ 改造优先级与验收标准 / Agent 操作须知与陷阱 / 数值基线（回归检测用）。

## 链路全景

```
富途OpenD(localhost:11111)                      Tushare fetch（openclaw 16:16，已存在）
      │ 拉取行业列表+成员                               │ market/daily + daily_basic
      ▼                                               ▼
scripts/pull_futu_industry.py              /Users/hyc/quant-data/  ← 共享数据根
      ▼                                               │
 industry/industry_members.parquet                    │
      │                                               │
      │  Tushare index_member_all（申万2021, 带 in/out 日期）— 低频拉取，无需每日
      ▼                                               │
scripts/fetch_sw_members.py                           │
      ▼ → industry/index_member_all_raw.parquet       │
scripts/build_members_daily.py  ← 日更：按交易日历展开每日快照
      ▼                                               │
 industry/industry_members_daily.parquet（每日真实成分, 消前视偏差）
      └───────────────┬───────────────────────────────┘
                      ▼
        scripts/compute_weighted_returns.py      行业市值加权涨跌幅（circ_mv 加权）
        scripts/build_industry_daily_full.py     等权行业日频聚合（nav_e 上游）
        scripts/update_industry_factor.py        拥挤度因子 ind_crowd
                      ▼
        scripts/industry_tracker.py              贝叶斯反转跟踪引擎（四阶段生命周期）
        scripts/mainline_detector.py             主线探测器（方向群聚类）
        scripts/regime_classifier.py             行业 regime 分类
                      ▼
        scripts/build_nav_924.py                 行业走势主表（924 起累计净值/相对强度/动量）
                      ▼
        cron/industry_review_daily.py            日频编排入口（自动补齐前置数据）
        └─ src/daily_review/module_02_industry.py  复盘模块二·三区框架（冷区/中区/热区）
        └─ scripts/check_data_completeness.py    数据完备性检查（防静默缺数）
                     ▼
        reports/daily-analysis/YYYY-MM-DD_industry_review.md
                     ▼
        signals/signals_log.parquet              信号台账（全系统信号统一留痕 + 前向收益回填）
        └─ scripts/evaluate_signals.py           事件研究评估（按档位/年份/regime 的超额与胜率）
        scripts/sync_industries_to_futu.py       行业分组同步富途（手动触发）
```

## 数据与配置

- **单一配置源**：`config/config.yaml`，可用环境变量 `INDUSTRY_RADAR_DATA` 覆盖数据根目录，默认 `/Users/hyc/quant-data`
- 数据读写全部经 `src/paths.py`，**禁止**硬编码 `/opt/data` 容器路径
- 输入：`market/daily/*.parquet`、`daily_basic/*.parquet`（由现有 openclaw tushare-daily-fetch 任务每日 16:16 更新，本项目不重复拉取）
- 输出：`industry/*.parquet`、`factors/ind_crowd_turnover_daily.parquet`、`industry/tracker_state.json`、`reports/daily-analysis/*.md`、`signals/signals_log.parquet`（信号台账）、`signals/eval_*.md`（评估报告）

## 用法

### 定时自动（launchd）

`com.industry-radar.daily` 每日 **19:30 / 20:00 / 20:30 / 21:00** 各触发一次，跑通即停：

```bash
# 一次性加载（首次部署；之后开机 / 登录会自动生效）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.industry-radar.daily.plist
launchctl print gui/$(id -u)/com.industry-radar.daily      # 查看状态
launchctl bootout gui/$(id -u)/com.industry-radar.daily    # 取消
```

设计要点：

- 上游数据 18:30 才开始拉取，故从 19:30 起跑；**多次触发 + 幂等标记**（`logs/.scheduled_done_YYYY-MM-DD`）实现「数据没到就等下一次」，跑通即不再重复。
- 非交易日直接跳过；数据到 21:00 仍未落地 → 发系统通知告警。
- 失败 / 有警告 → macOS 系统通知；日志见 `logs/scheduled_*.log` 与 `~/quant-data/logs/industry-radar-scheduled.log`。
- 不依赖 launchd 的手动入口：`bash scripts/run_daily_scheduled.sh`

### 手动

一键完整版：Finder 双击 `每日行业报告.command`，或：

```bash
open 每日行业报告.command                # 依次执行日频全链（下述 9 步），自动用文本编辑打开当日报告，日志留存 logs/
SCHEDULED=1 bash 每日行业报告.command    # 静默模式（不弹 TextEdit）；退出码 0=成功 / 1=日报未生成 / 2=部分环节失败
```

单步手动：

```bash
cd ~/quant/industry-radar
python3 scripts/build_members_daily.py                 # 日更：每日成分快照（新交易日必须重建，否则该日被静默跳过）
python3 scripts/compute_weighted_returns.py            # 日更：加权涨跌幅（circ_mv 加权，按日期 upsert）
python3 scripts/build_industry_daily_full.py --days 60 # 日更：等权行业日频聚合（MA20/13 预热需 ≥33 天）
python3 scripts/update_industry_factor.py              # 日更：拥挤度因子
python3 scripts/industry_tracker.py                    # 日更：反转跟踪
python3 scripts/mainline_detector.py                   # 日更：主线探测
python3 scripts/regime_classifier.py                   # 日更：行情类型 regime
python3 scripts/build_nav_924.py                       # 日更：行业走势主表（924 起净值/相对强度/动量）
python3 cron/industry_review_daily.py                  # 日更：行业跟踪报告（自动补齐前置 + 完备性检查）

bash scripts/run_daily_scheduled.sh                    # 定时入口：交易日/就绪/幂等判断 + 静默执行 + 结果告警
python3 scripts/check_data_completeness.py             # 检查：数据完备性（防静默缺数，可单独跑）
python3 scripts/build_signals_log.py --backfill-cold-zone  # 首次：全历史回填冷区信号台账（约 1~3 分钟）
python3 scripts/build_signals_log.py                   # 日更：当日信号快照 + 前向收益回填（cron 已自动执行）
python3 scripts/evaluate_signals.py                    # 评估：事件研究（超额/胜率/t值，按档位/年份/regime）
python3 scripts/pull_futu_industry.py                  # 周更：富途行业成员快照（限频 30s/10次，约10分钟）
python3 scripts/fetch_sw_members.py                    # 低频：Tushare 申万成分全量（申万调整成分后重拉，带 in/out 日期）
python3 scripts/sync_industries_to_futu.py             # 手动：行业分组同步富途
```

> **成分数据的两个层次**（别混淆）：
> - `fetch_sw_members.py` 走网络拉 Tushare `index_member_all` 全量（7919 行，含 `in_date/out_date`）。
>   申万调整成分不频繁，**无需每日拉**；拉一次即可支持任意历史日期的成分还原。
> - `build_members_daily.py` 是**纯本地**按交易日历展开成分快照，**必须每日跑**：
>   新交易日没有快照时，`compute_weighted_returns.py` 会整天跳过且不报错（静默缺数）。

### 数据完备性检查（防静默缺数）

`scripts/check_data_completeness.py` 在日更链末尾运行，检查「文件有行但内容悄悄为空」这类静默缺数：

| 检查项 | 级别 | 触发条件 |
|---|---|---|
| 源数据缺位 | CRITICAL | `market/daily` 或 `daily_basic` 缺当日文件 |
| 行业整行缺失 | CRITICAL | 目标日某行业在加权表无行（期望 131/131） |
| 流通市值异常 | CRITICAL | `circ_mv ≤ 0` |
| 净值列空洞 | CRITICAL/WARNING | 净值主表关键列（`nav_w`/`nav_e`/`rs_w`）当日出现空值 |
| 成分股骤降 | WARNING | 行业当日成分股数 < 自身历史中位数 × 50%（中位数 ≥5 才判） |
| 成分表骤降 | WARNING | 当日全市场成分股数 < 前一交易日 × 85% |
| 净值表/等权源滞后 | WARNING | 未刷新到目标日 |

退出码：`0` 健康 / `1` 严重 / `2` 警告 / `3` 非交易日跳过（结果落盘 `industry/completeness_status.json`）。
cron 入口检测到 `1` 会以非零码退出，避免缺数被静默吞掉。

## 口径备忘（继承自 Hermes，详见 docs/）

- 行业划分：富途 Plate.INDUSTRY（申万二级近似），约定 131 个行业；名称与申万2021 二级 131/131 匹配
- **成分口径**：日更聚合用 `industry_members_daily.parquet`（Tushare `index_member_all` 申万原始成分，
  带 `in_date/out_date`），按**当日真实成分**聚合以消除前视偏差；`industry_members.parquet` 仅作行业全集/名称字典
- **加权口径**：`circ_mv`（流通市值）加权，与通达信 881xxx / 申万官方行业指数同口径；
  实测对标通达信官方点位平均绝对误差 ~0.10pp（总市值口径为 ~0.14pp）
- 复盘模块二三区框架：冷区（宽度<阈值，宽度反转信号）/ 中区 / 热区（待实现信号挖掘）
- 反转跟踪：Sₜ = Sₜ₋₁ × λₜ，S₀ = C1(宽度) × C3(涨幅)，C0/C2/C4 已废弃（2026-08-02）
- 五阶段生命周期：沉睡→唤醒→成长(R<8%)→成熟(R≥8%不可逆)→预警(2-of-4)→衰退
- 主线择时 v3：31 申万一级方向群，主线 = 热度>1.3x + 超额60d>0，确认窗口法判瓦解

## 目录结构

```
src/          可复用库：paths(配置)/utils(数据加载)/daily_review(模块二+宽度反转因子+日报附加章节)/signals(信号台账+评估)
scripts/      各环节可执行脚本
cron/         日频编排入口
research/     历史回测脚本存档（冻结，不维护）
docs/         设计文档与回测结论存档
```

## 与 Hermes 的关系

- 代码抽取自 `~/.hermes/quant`（v0 基线 commit 与源逐字节一致，可 diff 追溯）
- Hermes 侧原文件暂不删除；切换调度前旧链路保持运行
- 本仓库是行业链路的唯一演进方向，后续修改完善在此进行
