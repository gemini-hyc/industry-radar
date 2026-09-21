# Industry Radar — 行业分析链路

从 Hermes Quant（`~/.hermes/quant`）抽取的完整行业分析链路，独立演进。
定位：行业板块的数据拉取 → 因子计算 → 反转跟踪 → 主线择时 → 每日行业复盘报告 → 富途分组同步。

## 链路全景

```
富途OpenD(localhost:11111)                      Tushare fetch（openclaw 16:16，已存在）
      │ 拉取行业列表+成员                               │ market/daily + daily_basic
      ▼                                               ▼
scripts/pull_futu_industry.py              /Users/hyc/quant-data/  ← 共享数据根
      ▼                                               │
 industry_members/list.parquet                        │
      └───────────────┬───────────────────────────────┘
                      ▼
        scripts/compute_weighted_returns.py      行业市值加权涨跌幅
        scripts/update_industry_factor.py        拥挤度因子 ind_crowd
                      ▼
        scripts/industry_tracker.py              贝叶斯反转跟踪引擎（四阶段生命周期）
        scripts/mainline_detector.py             主线探测器（方向群聚类）
        scripts/regime_classifier.py             行业 regime 分类
                      ▼
        cron/industry_review_daily.py            日频编排入口（自动补齐前置数据）
        └─ src/daily_review/module_02_industry.py  复盘模块二·三区框架（冷区/中区/热区）
                     ▼
        reports/daily-analysis/YYYY-MM-DD_industry_review.md
        scripts/sync_industries_to_futu.py       行业分组同步富tu（手动触发）
```

## 数据与配置

- **单一配置源**：`config/config.yaml`，可用环境变量 `INDUSTRY_RADAR_DATA` 覆盖数据根目录，默认 `/Users/hyc/quant-data`
- 数据读写全部经 `src/paths.py`，**禁止**硬编码 `/opt/data` 容器路径
- 输入：`market/daily/*.parquet`、`daily_basic/*.parquet`（由现有 openclaw tushare-daily-fetch 任务每日 16:16 更新，本项目不重复拉取）
- 输出：`industry/*.parquet`、`factors/ind_crowd_turnover_daily.parquet`、`industry/tracker_state.json`、`reports/daily-analysis/*.md`

## 用法（当前为手动阶段）

```bash
cd ~/quant/industry-radar
python3 scripts/pull_futu_industry.py          # 周更：行业成员映射（限频 30s/10次，约10分钟）
python3 scripts/compute_weighted_returns.py    # 日更：加权涨跌幅
python3 scripts/update_industry_factor.py      # 日更：拥挤度因子
python3 scripts/industry_tracker.py            # 日更：反转跟踪
python3 cron/industry_review_daily.py          # 日更：行业跟踪报告（自动检查+补齐前置）
python3 scripts/sync_industries_to_futu.py     # 手动：行业分组同步富途
```

## 口径备忘（继承自 Hermes，详见 docs/）

- 行业划分：富途 Plate.INDUSTRY（申万二级近似），不手工维护行业映射
- 复盘模块二三区框架：冷区（宽度<阈值，宽度反转信号）/ 中区 / 热区（待实现信号挖掘）
- 反转跟踪：Sₜ = Sₜ₋₁ × λₜ，S₀ = C1(宽度) × C3(涨幅)，C0/C2/C4 已废弃（2026-08-02）
- 五阶段生命周期：沉睡→唤醒→成长(R<8%)→成熟(R≥8%不可逆)→预警(2-of-4)→衰退
- 主线择时 v3：31 申万一级方向群，主线 = 热度>1.3x + 超额60d>0，确认窗口法判瓦解

## 目录结构

```
src/          可复用库：paths(配置)/utils(数据加载)/daily_review(模块二+宽度反转因子)
scripts/      各环节可执行脚本
cron/         日频编排入口
research/     历史回测脚本存档（冻结，不维护）
docs/         设计文档与回测结论存档
```

## 与 Hermes 的关系

- 代码抽取自 `~/.hermes/quant`（v0 基线 commit 与源逐字节一致，可 diff 追溯）
- Hermes 侧原文件暂不删除；切换调度前旧链路保持运行
- 本仓库是行业链路的唯一演进方向，后续修改完善在此进行
