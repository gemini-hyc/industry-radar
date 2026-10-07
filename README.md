# Industry Radar — 行业分析链路

从 Hermes Quant（`~/.hermes/quant`）抽取的完整行业分析链路，独立演进。
定位：行业板块的数据拉取 → 因子计算 → 反转跟踪 → 主线择时 → 每日行业复盘报告 → 富途分组同步。

> **接手必读**：[`docs/system-audit-and-roadmap.md`](docs/system-audit-and-roadmap.md) —— 系统问题审计与改造路线（2026-09-23）。
> 含：系统现状速查 / 11 项问题（附证据位置）/ 改造优先级与验收标准 / Agent 操作须知与陷阱 / 数值基线（回归检测用）。
>
> **⚠️ 本仓库文档与代码不符时，一律以代码为准。** 历史回测数字（旧口径）不可与信号台账的
> 超额数字直接比较 —— 两者在收益口径、样本规则、数据地基上均不同。
> 待办与决策清单：[`docs/action-items.md`](docs/action-items.md)（源端执行项 / 待拍板项 / 已完成归档）。
>
> 📌 **下一轮入口**：[`docs/indicator-status-register.md`](docs/indicator-status-register.md) ——
> **全部预测性装置 × 验证状态 × 依据**（哪些已证伪、哪些从未评估、哪些只是描述性、
> 哪些数据在但没接进链、以及**先修哪个测量工具**）。新指标入账前先读它的 §0 采纳门槛。

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
        scripts/mainline_detector.py             主线探测器（方向群聚类）
        scripts/regime_classifier.py             行业 regime 分类
                      ▼
        scripts/build_nav_924.py                 行业走势主表（924 起累计净值/相对强度/动量）
                      ▼
        cron/industry_review_daily.py            日频编排入口（自动补齐前置数据）
        └─ src/daily_review/module_02_industry.py  复盘模块二（当日行业数据状态）
        └─ scripts/check_data_completeness.py    数据完备性检查（防静默缺数）
                     ▼
        reports/daily-analysis/YYYY-MM-DD_industry_review.md
                     ▼
        signals/signals_log.parquet              信号台账（全系统信号统一留痕 + 前向收益回填）
        ├─ signals/industry_margin_daily.parquet  行业两融占比面板（情绪数据聚合，带增量缓存）
        └─ scripts/evaluate_signals.py           事件研究评估（按档位/年份/regime 的超额与胜率）
        scripts/sync_industries_to_futu.py       行业分组同步富途（手动触发）
```

## 环境与依赖

⚠️ **各机器的解释器不一样，照抄命令会踩空。**

| 机器 | 用哪个解释器 | 说明 |
|---|---|---|
| 源端 Mac Studio | `/usr/bin/python3`（系统 python 3.9.6） | 日更链实际使用的解释器，依赖装在系统解释器里；**开发与回归也用它** |
| 新机器 | 自建 venv | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |

- **本文档下面的命令一律写 `python3`**（= 源端用法）。从机若解释器不同，
  执行前替换 `python3`，否则要么 `ModuleNotFoundError: pandas`，要么更糟 ——
  被解析到**另一个恰好装了 pandas 的解释器**，版本不同、**数值结果可能不同**。
- 源端实测版本（2026-10-07，Python 3.9.6）：pandas 2.3.3 / numpy 2.0.2 / pyarrow 21.0.0 /
  scipy 1.13.1 / PyYAML 6.0.3，futu-api 亦可用（详见 `requirements.txt` 头注释）。
- ⚠️ 缺依赖时**不要临时安装**（改环境需先获许可），按替代方案处理。
- 两个入口脚本的解释器来源：`每日行业报告.command` 用 `${PYTHON:-python3}`（可用 `PYTHON=` 覆盖）；
  `scripts/run_daily_scheduled.sh` **硬编码 `/usr/bin/python3`**（launchd 的 PATH 不含 homebrew，故写死）。
- `make check` / `tools/dev_verify.sh` 的默认解释器同样指向 `/usr/bin/python3`；
  不合格时回落 `PATH` 里的 `python3`，覆盖用 `make check PY=/path/to/python`。

## 开发与验证（`make`）

**改完代码提交前跑一条命令即可**：

```bash
make check      # = 语法检查 + 回归基线（结构硬断言 + 数值快照软报告）
make help       # 列出全部目标
```

| 目标 | 作用 | 何时用 |
|---|---|---|
| `make check` | **提交前跑这条** | 每次改代码后 |
| `make verify SCRIPT=scripts/xxx.py` | **同源 A/B**：`git archive HEAD` 基线 vs 改动后，同一份沙箱数据双跑并逐产物比对 ⇒ 回答「**改动 diff 是什么**」 | 改了会动产物的脚本 |
| `make selftest` | 注入破坏，验证每条断言**真能抓到**（11+3 例） | 改了测试之后 |
| `make snapshot` | 更新数值快照基线 | **仅改产物口径后** |
| `make syntax` | 只做语法检查（AST 解析，**零副作用、不写 `__pycache__`**） | 快速自查 |
| `make clean-verify` | 清理 A/B 沙箱（4.5 G，可随时重建） | 磁盘紧张时 |

- 换机器用 `make check PY=/path/to/python` 覆盖解释器（不要改 `Makefile` 里的默认值）。
- ⚠️ **`make check` 目前会红 1 项**（`mainline_detection` 列集合）：B1 改了产物口径但真实数据根的
  产物还是旧的 12 列，重算后即转绿 —— 红是真信号，不要当成误报绕过。

## 数据与配置

- **单一配置源**：`config/config.yaml`，可用环境变量 `INDUSTRY_RADAR_DATA` 覆盖数据根目录，默认 `/Users/hyc/quant-data`
- 数据读写全部经 `src/paths.py`，**禁止**硬编码 `/opt/data` 容器路径
- 输入：`market/daily/*.parquet`、`daily_basic/*.parquet`（由现有 openclaw tushare-daily-fetch 任务每日 16:16 更新，本项目不重复拉取）
- 输出：`industry/*.parquet`、`factors/ind_crowd_turnover_daily.parquet`、`reports/daily-analysis/*.md`、`signals/signals_log.parquet`（信号台账）、`signals/eval_*.md`（评估报告）、`signals/industry_margin_daily.parquet`（两融占比面板缓存，**2026-10-06 起不再对外呈现**）
- ⚠️ `industry/tracker_state.json` **2026-10-06 起不再产出**（反转跟踪系统已删除）；`scripts/sync_industries_to_futu.py`（人工同步入口，主人拍板保留）仍读该文件 ⇒ 该脚本目前**无输入可用**，恢复需先重建跟踪池

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
cd ~/Quant/industry-radar
python3 scripts/build_members_daily.py                 # 日更：每日成分快照（新交易日必须重建，否则该日被静默跳过）
python3 scripts/compute_weighted_returns.py            # 日更：加权涨跌幅（circ_mv 加权，按日期 upsert）
python3 scripts/build_industry_daily_full.py --days 60 # 日更：等权行业日频聚合（MA20/13 预热需 ≥33 天）
python3 scripts/update_industry_factor.py              # 日更：拥挤度因子
python3 scripts/mainline_detector.py                   # 日更：主线探测
python3 scripts/regime_classifier.py                   # 日更：行情类型 regime
python3 scripts/build_nav_924.py                       # 日更：行业走势主表（924 起净值/相对强度/动量）
python3 cron/industry_review_daily.py                  # 日更：行业跟踪报告（自动补齐前置 + 完备性检查）

bash scripts/run_daily_scheduled.sh                    # 定时入口：交易日/就绪/幂等判断 + 静默执行 + 结果告警
python3 scripts/check_data_completeness.py             # 检查：数据完备性（防静默缺数，可单独跑）
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

- 行业划分：富途 Plate.INDUSTRY（申万二级近似），约定 131 个行业；名称与申万2021 二级 131/131 匹配。
  注：早期交易日（申万分类演化期）实际只有 127 个行业有成分，属正常，**不是缺数**
- **成分口径**：日更聚合用 `industry_members_daily.parquet`（Tushare `index_member_all` 申万原始成分，
  带 `in_date/out_date`），按**当日真实成分**聚合以消除前视偏差；`industry_members.parquet` 仅作行业全集/名称字典
- **加权口径**：`circ_mv`（流通市值）加权，与通达信 881xxx / 申万官方行业指数同口径；
  实测对标通达信官方点位平均绝对误差 ~0.10pp（总市值口径为 ~0.14pp）
- 🚫 **复盘模块三区框架已退场（2026-10-06）**：冷区 / 中区 / 热区（`format_cold_zone_section`）连同
  反转跟踪系统一并拆除，日报不再输出该章节。历史证据见 `docs/archive/`
- 🚫 **信号定位**（2026-10-06 起）：宽度反转（`cold_zone`）与反转跟踪池（`tracker_pool`）均标记
  **`retired` = 🚫 已弃用**，台账条目保留仅供追溯，**不可作决策依据**。`mainline` 为「待观察」（仅向前样本）。
  代码唯一真相来源 = `src/signals/sources.py` 的 `SOURCE_SPECS[*]["status"]`（状态写在此处，
  **不进台账 schema**，避免第二个真相源）。弃用依据见 `docs/plan/retirement-breadth-tracker-plan.md`
- 📐 **宽度指标能做什么 / 不能做什么** → `docs/archive/breadth-what-it-can-and-cannot-do.md`：
  **七个方向已测且为空**（横截面 alpha / 时点择时 / 风险预测 / 状态分位表 / 背离项 / 换构造 /
  个股级经典形态，t 值均已做重叠校正），**不得重复**；宽度是**滞后状态量**（半衰期 ≈5 日，
  与 20 日动量 rank 相关 **+0.694**）⇒ 只做**当期描述 + 注意力分流 + 数据监控**，⛔ 不做预测。
  🚫 **2026-10-06 起「宽度状态」章节已从日报删除**（`format_width_state_section` 一并移除）；
  `industry_daily_full.breadth` 列**保留为数据探针**（仅用于 `BREADTH_ZERO` 零值判据），不再对外呈现
- 🔴 **反转跟踪系统已删除**（2026-10-06，`scripts/industry_tracker.py` 原 996 行 + `factor_width_reversal.py`
  原 207 行）：Sₜ = Sₜ₋₁ × λₜ 递推与六阶段生命周期判定一并退场
- ~~生命周期（六阶段）：沉睡 → 早期(唤醒) → 成长(R<8%) → 成熟(R≥8% 不可逆) → 预警(2-of-4，
  或保底：MA5 下穿 MA21 持续 2 天且间距>0.5%) → 衰退(死叉确认)~~ 🚫 **已随反转跟踪系统退场（2026-10-06）**，
  原实现文件 `scripts/industry_tracker.py` 已删除；设计文档存 `docs/archive/tracker-design-and-issues.md`
- 主线择时（**以 `mainline_detector.py` 为准**）：方向群由**相关系数聚类**自动生成
  （`cluster_industries(corr_threshold=0.6)`，群数与成员随数据变化，**不是固定 31 个申万一级群**）；
  主线判据 = 热度 ≥1.3x **且** 20 日超额 ≥0.3%/日 **且** 20 日超额胜率 ≥0.6；
  **且须连续 ≥3 个交易日同时满足**（2026-10-04 加的跨期稳定性约束，`min_consecutive_days=3`；
  判定只用当天与前 2 个交易日 ⇒ **无前视**，但每段头 2 天不算主线）；
  阶段（蛰伏/启动期/加速期/见顶期/瓦解期）只用 `heat_ratio` + `excess_ma20` 两维判定，
  代码中**没有**"确认窗口法"。⚠️ 仍**无最小成员数约束** ⇒ 单行业方向群仍可能被判为主线
  （实测跨期约束对独舞几乎无改善：69.7%→67.9%，见 `docs/action-items.md` D2）；
  产物另存 `is_mainline_raw` / `consec_days` 两列以便审计

## 目录结构

```
src/          可复用库：paths(配置)/utils(数据加载)/daily_review(模块二+宽度反转因子+日报附加章节)/signals(信号台账+评估)
scripts/      各环节可执行脚本
cron/         日频编排入口
research/     历史回测脚本存档（冻结，不维护）
docs/         设计文档与回测结论存档
```

## 与 Hermes 的关系

- 代码抽取自 `~/.hermes/quant`（v0 基线 commit 与源逐字节一致）
- **Hermes 侧已于 2026-09-24 退出 git 仓库状态**：`.git` 已移除，工作树文件原样保留（6068 个文件未动）。
  全部历史（`master` / 9 个提交）归档在 `~/.hermes/quant-git-backup-20260924.tar.gz`，
  提交清单见 `~/.hermes/quant-git-log-20260924.txt`；需要逐字节 diff 追溯时解包该备份即可
- Hermes 侧原文件暂不删除；切换调度前旧链路保持运行
- 本仓库是行业链路的唯一演进方向，后续修改完善在此进行
