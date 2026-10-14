# B2 接线执行记录：列契约激活 + 新旧并存

**日期**：2026-10-12 20:00
**对应立项**：`chain-veto-and-mainline-rebuild-plan.md` 项目 B2 / §3.3 红线
**上游依据**：`chain-mainline-backfill-diagnosis-2026-10-12.md`（1638 天回溯支持 B2）

---

## 1. 本轮做了什么

把链版主线探测接入**生产十步链**，同时激活列契约测试。采用**新旧并存**而非直接替换。

### 接线形态

```
十步链
  7/10  主线探测（旧版聚类）  → industry/mainline_detection.csv       19 列
  7b/10 链版主线探测（B2）    → industry/chain_mainline_detection.csv 22 列   ← 新增
```

两份产物**不同文件、互不覆盖**。任一步失败都不中断后续步骤（沿用 `run_step` 语义）。

### 为什么并存而不是直接替换

1. **signals_log 依赖旧版**：`src/signals/sources.py:103` 的 `"mainline"` source spec
   读的是旧版 `mainline_detection.csv`。直接替换会让信号台账断粮。
   ⇒ 将来若要真正替换，**必须同步改 sources.py**，这是独立的接线风险点。
2. 口径变更影响面大，先让两份产物同时在日更链里跑，用台账持续对照。
3. 观测成本为零：新版只多花约 1 秒。

---

## 2. 文件改动清单

| 文件 | 改动 |
|:--|:--|
| `scripts/chain_mainline_detector.py` | **从 `research/` 移入 `scripts/`**（进生产）；路径改为基于 `REPO` 与 `src.paths`；`VETO_CSV` 从沙箱改指仓库内固化文件；输出改生产固定路径 + `--out` 选项；docstring 补名单后见之明警示 |
| `docs/plan/chain_veto_verification_latest.csv` | **新增**。方案 C 的筛选依据从 `_local_dev` 固化进仓库（生产链不得依赖沙箱） |
| `src/paths.py` | ARTIFACTS 登记 `chain_mainline_detection` |
| `每日行业报告.command` | 新增第 7b 步；更新头部步骤说明 |
| `tests/_common.py` | `chain_mainline_detection` 从 `PENDING_UNDER_TEST` **移出**（激活）；注释更新为已激活 |
| `tests/selftest.py` | NEED 清单加新产物；新增破坏场景「链版产物删列 → B2 schema 漂移」 |
| `research/observation_snapshot.py` | DETECTOR 路径改 `scripts/`；改为读**生产产物** + 从 `industry_weighted_returns.parquet` 推参考日（产物本身无日期列） |
| `research/observation_backfill.py` | NEW 模块路径改 `scripts/` |

---

## 3. 一个实现坑（值得记住）

新版产物 `chain_mainline_detection.csv` **没有日期列**（22 列里没有 ref_date）。
`observation_snapshot.py` 原来靠"文件名里的参考日"取日期，改生产固定路径后失效。

解决：参考日改从 `industry_weighted_returns.parquet` 取 `date.max()`——
与探测器内部 `reference_date = returns['date'].max()` 同源，不会漂。

---

## 4. 验收结果

| 项 | 结果 |
|:--|:--:|
| 新版生产产物落盘 | ✅ `industry/chain_mainline_detection.csv`（12677 B，22 列） |
| 真实根回归 | ✅ **93 passed / 1 skipped**（与基线一致） |
| DRY-RUN 步骤核对 | ✅ 7/10 旧版 + 7b/10 新版并存 |
| 列契约**真的在测**（沙箱敏感性） | ✅ 删 `resonance_z_today` ⇒ 测试红，精确报 `chain_mainline_detection: 缺 ['resonance_z_today']` |
| selftest 结构破坏 | ✅ **12/12 被抓到**（含本轮新增的链版场景） |
| selftest 恢复后复验 | ✅ 全绿 |
| 观察期台账可读新产物 | ✅ 21 行，10-12 行 `ref_aligned=yes` |
| 旧版 signals_log 依赖未破坏 | ✅ 并存模式下旧版产物照旧产出 |

### 沙箱敏感性验证（不是"我觉得测了"）

`tests/selftest.py` 原本不覆盖链版契约，本轮**手工验证过一次**：
造 symlink 沙箱（103M 数据零复制）→ 只把新产物改坏一列 → `INDUSTRY_RADAR_DATA=<沙箱>` 跑测试
⇒ 立刻红并指出缺哪列。随后固化成 selftest 场景，以后每次 `make selftest` 自动复验。

---

## 5. ⚠️ 已知问题（非本轮引入，但本轮撞上了）

selftest 末尾报「真实数据根零写入」失败，泄漏 3 个文件：

```
/Users/hyc/quant-data/exchange/daily_info/2026/10/2026-10-12.parquet   19:58:37
/Users/hyc/quant-data/exchange/sz_daily_info/2026/10/2026-10-12.parquet 19:58:37
/Users/hyc/quant-data/ggt/daily/2026/10/2026-10-08.parquet             19:58:38
```

**这 3 个文件不是本套件写的**：时间戳落在 selftest 运行窗口内，但目录 `exchange/`、`ggt/`
属 **stock-doctor 项目**（同一数据根下的另一个 launchd 任务）。

⇒ selftest 的守卫只监控"数据根有没有新文件"，**不区分写入者是哪个项目**。
多项目共用数据根时这是既有误报，不是本轮改动导致。

未修（超出本轮范围，且修法需要决策：按目录白名单 or 按 PID 归属）。
仅记录，避免下次有人误以为是链版接线写坏了数据。

---

## 6. 当前状态

| 项 | 状态 |
|:--|:--|
| 列契约测试 | ✅ 已激活（22 列硬断言 + selftest 破坏场景） |
| 生产链新版产物 | ✅ 每日自动产出（第 7b 步） |
| 旧版产物 / signals_log | ✅ 照旧，未受影响 |
| 观察台账 | ✅ `_local_dev/observation_log.csv` 持续累积 |
| **旧版下线** | ⏳ 未做。前置：改 `src/signals/sources.py` 的 mainline source 指向新版 |
| 日报正文引用新版 | ⏳ 未做（日报当前不直接读主线产物，见 §7） |

---

## 7. 顺手核实到的一件事

`cron/industry_review_daily.py` **不直接读** `mainline_detection.csv`——
日报正文的主线信息经 `src/signals/sources.py` → `signals_log` 链路进来。

⇒ 这意味着：**光切换生产产物不会改变日报正文**。要让日报显示链版主线，
   还得改 `sources.py` 的 mainline source。这一步是独立的，风险和接线量都更大，
   **本轮刻意没做**。

---

## 8. 复现命令

```bash
# 生产（十步链第 7b 步等价）
/usr/bin/python3 scripts/chain_mainline_detector.py

# 回归 + 敏感性
make check                                   # syntax + tests
/usr/bin/python3 tests/selftest.py           # 注入破坏，12/12 应被抓到

# 观察台账
/usr/bin/python3 research/observation_snapshot.py --show
```

---

*相关：立项 `chain-veto-and-mainline-rebuild-plan.md`；回溯诊断
`chain-mainline-backfill-diagnosis-2026-10-12.md`；B2/B3 首跑
`chain-mainline-rebuild-execution-2026-10-11.md`。*
