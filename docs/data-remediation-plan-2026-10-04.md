# 数据整改方案（源端执行清单）

> **产出**：2026-10-04 ｜ 产出地：本机 MacBook Air（3-Mac 网的**从机**）
> **适用**：回到源端 **Mac Studio**（`momingdeMac-Studio-2.local`）后**依次执行**
> **本文定位**：把**所有数据侧问题**一次性卸载到这里，本机不再纠缠数据，转入项目代码改进。
> **配套**：`docs/action-items.md`（原始台账）、`docs/stage-b-evidence-2026-10-04.md`（阶段 B 取证与复核）、
> `docs/zombie-scheduler-remediation-2026-10-03.md`（调度处置）、`docs/credentials-remediation-2026-10-02.md`（凭据）。

---

## 0. 为什么现在可以不修：当前数据「可用」的判定

**结论：本机现有数据副本足以支撑项目改进，不需要抢修。** 依据（均为本机实测，可跨机复核）：

| 数据问题 | 实测状态 | 是否阻塞 |
|---|---|---|
| **P0-3** `module_02` 报告主体吃 Hermes 废弃产物 | 缓存确为废弃流水线产物（mtime `2026-10-02 16:55`），但**日报当天实际未被污染** —— 冷区信号只依赖目标日前 20 日（09-02~09-30 窗口退化 0%，与现行产物**逐值一致**） | ❌ 不阻塞（属**结构性隐患**） |
| **P0-2** `daily_full` 2026-07-01~07-13 `breadth` 全 0 | 坏值段已滚出任何信号的前 20 日窗口；护栏已落地防新增 | ❌ 不阻塞 |
| **B7** regime 代码 8 类 vs 落盘 5 类 | 代码 `d3b024a` 提交于 **10-01 18:05**，产物 mtime **09-30 19:30** ⇒ **待重算**，源端下次日更自愈（前提：源端已同步该 commit） | ❌ 不阻塞 |
| **B2** 静态 5644 vs PIT 5913 差 269 只 | 归一化后仅 427 只真差异；**对产物几乎零影响**（仅静态 79 只行情命中 0/79；仅 PIT 348 只中 341 只无行情） | ❌ 不阻塞 |
| **B3** 宽度 `min_periods` 5→20 | 真实代价 **仅 26 条信号（1.2%）**，非报告的 230 条 | ❌ 不阻塞 |
| **B1/A8** mainline 跨期稳定性 | 代码已改、**本机产物已重算（15 列）**；源端同步后重跑即可。有主线日 873→677，**日报内容会变**（变少） | ❌ 不阻塞 |

> ⚠️ **但以上都只是「不阻塞」，不是「已解决」** —— 它们全部需要源端动手，即本文内容。

**三条原则**：
1. **顺序敏感**：代码同步 → 数据重算 → 校验。跳步会产生"用旧代码算新数据"的中间态。
2. **每步有验收**：不通过就停，不要带着失败继续下一步。
3. **备份先行**：凡涉及 `--force-overwrite` / 台账重算，先拷 `.bak`。

---

## 1. 执行总览（勾选表）

| 步 | 内容 | 预估 | 是否阻塞后续 |
|:--:|---|---|:--:|
| 0 | **到达源端先取证**（版本 / 调度 / 依赖），**不动手** | 30 min | ✅ 是 |
| 1 | **代码同步**：本机 → 源端（A1~A10 + P0-3 改动，共 17 个文件） | 1 h | ✅ 是 |
| 2 | **数据重算**：成分 → 加权 → 聚合 → **mainline** → regime → nav → 台账 → 评估 | 2~4 h | ✅ 是 |
| 3 | **回归自检**：`make check`（**同步了 A10 才跑**，见 §3.3） | 5 min | ❌ 否 |
| 4 | 基础设施固化：版本基线 / 同步通道 / 路径大小写 / 依赖记录 | 1 h | ❌ 否 |
| 5 | 数据治理：孤儿产物 / 追溯校验 / 补拉 / 未登记写入 / 大文件 | 2 h | ❌ 否 |
| 6 | 僵尸调度处置（session2 的 7 步） | 1~2 h | ❌ 否 |
| 7 | 凭据轮换（token 已止血，待替换） | 1 h | ❌ 否 |
| 8 | 阶段 B 口径议题（按 §9 的处置结论） | 视选择 | ❌ 否 |

---

## 2. 第 0 步 · 到达源端先取证（**不要跳过、不要先改**）

> 目的：本机是副本，**无法确认源端代码/数据版本**。先取证，否则第 1 步可能是"用旧的覆盖新的"。

```bash
cd ~/Quant/industry-radar          # ⚠️ 大写 Q；源端若有小写副本需确认哪个在跑
git log --oneline -3                # 记录 HEAD，与本机 dc4513b 比对
git status --short                  # 记录未提交改动（源端可能有本机没有的手工修改！）
md5 ~/Quant/industry-radar/scripts/industry_tracker.py
wc -l ~/Quant/industry-radar/scripts/*.py
```

**必须记录的 5 项**（对应 action-items 的 B1/B3/B5）：

| # | 项目 | 命令 / 位置 |
|:--:|---|---|
| 1 | 代码版本 | `git log --oneline -3` + 关键文件 `md5` |
| 2 | 数据版本 | `ls -la ~/quant-data/industry/*.parquet \| head`（记 mtime） |
| 3 | **实际调度** | `ls ~/Library/LaunchAgents/ \| grep -E "industry\|stock\|market"`；`launchctl list \| grep -iE "industry\|quant"` |
| 4 | 解释器与依赖 | `which python3 && python3 -V && python3 -m pip freeze \| head -30` |
| 5 | 日更日志 | `ls -la ~/quant-data/logs/ \| tail`；找最近的 `industry-radar-scheduled.log` |

> ⚠️ **已知不一致**：README 与仓库都**找不到** `com.industry-radar.daily.plist`（action-items **B3**）——照 README 做会失败。第 3 项取证就是为查清它实际在哪。

---

## 3. 第 1 步 · 代码同步（A 表）

> 🔴 **前提**：`~/.quant-sync` **只同步数据、不含代码**（action-items **B2**）⇒ **本机任何改动都不会自动到源端**，必须手工同步。

**需要同步的文件**（本机 HEAD **`dc4513b`**，按主题分组）：

| 组 | 文件 | 来源文档 | 同步后是否需重算 |
|---|---|---|---|
| **A1** 台账止血 | `cron/industry_review_daily.py`、`scripts/{build_signals_log,build_industry_daily_full,compute_weighted_returns}.py`、`src/signals/evaluate.py` | `changes-2026-10-01-p0p1.md` | 是（台账回填） |
| **A2** regime | `scripts/regime_classifier.py` | `changes-2026-10-01-p2-regime.md` | 是（全量重算） |
| **A3** 极值/宽度 | `src/paths.py`、`src/utils/data_quality.py`（新）、`src/utils/market_data.py`、`scripts/compute_weighted_returns.py` | `changes-2026-10-01-p2-extremes-width.md` | 是 |
| **A4** PIT 成分 | `scripts/build_members_daily.py`、`scripts/build_industry_daily_full.py`、`src/utils/market_data.py` | `changes-2026-10-01-p2-pit-members.md` | 是（**会改变冷区信号约 10%**） |
| **A6** 缺陷修复 | `scripts/industry_tracker.py`、`scripts/mainline_detector.py` | `changes-2026-10-03-defect-fixes.md` | **否**（纯内部口径，实测逐行逐列一致） |
| **A7** 写入护栏 | `scripts/build_industry_daily_full.py` | `changes-2026-10-03-daily-full-breadth-anomaly.md` | 否 |
| **P0-3** 数据源修正 | `src/daily_review/module_02_industry.py`（见 §9.1） | 本文 §9.1 | 否（改的是取数路径） |
| **A8** mainline 跨期稳定性 | `scripts/mainline_detector.py` | 本文 §3.1 | **是** —— 改判定口径，**日报与信号内容都会变** |
| **A9** 环境声明纠偏<br>（2026-10-04 新增） | `requirements.txt`、`README.md` | 本文 §3.2 | **否**（纯文档，但**有实操价值**） |
| **A10** 回归基线 + 轻量 CI<br>（2026-10-04 新增） | `tests/`（7 文件）、`tools/syntax_check.py`、`Makefile` | 本文 §3.3 | **否**（不参与生产链路） |

> ⚠️ **A3 与 A4 共用 `src/utils/market_data.py`，必须合并成一次同步** —— 分两次会产生版本不一致的中间态。
> ⚠️ **A6 与 A8 都改 `scripts/mainline_detector.py`** —— 同上理由，合并成一次。

> 📌 **最省事的同步方式**：本机与源端都是 git 仓库 ⇒ 直接
> `git log --oneline 6a0df65..dc4513b` 拿到这 5 个提交，
> `git diff --name-only 6a0df65..dc4513b` 拿到 **17 个文件**的完整清单（本表即按此核对）。
> 若源端 HEAD 已在 `6a0df65` 之后，则**以源端实际 HEAD 为基准重算这个区间**（第 0 步已让你记录）。

### 3.1 🔴 A8 同步纪律（改判定口径，最容易漏）

`mainline_detection.csv` 的**列集合已变**（新增 `is_mainline_raw` / `consec_days` / `min_consecutive_days`）。

- **必须**：同步 A8 代码 → 重跑 `mainline_detector.py` → 再跑报告与台账。
- **判据：产物列数应为 15**（原 12 + 新 3）。
- ✅ **本机已重算完毕**（`INDUSTRY_RADAR_READONLY=off` 跑 `mainline_detector.py`），实测 15 列
  ⇒ **源端同步后只需重跑，无口径漂移风险**。本机 12 列旧版留档在 `~/quant-data/_local_dev/backups/`
  （源端不需备份 —— 产物会被覆盖重算）。
- 🔴 **日报与信号内容会变**：有主线日 **873 → 677（保留 77.5%）** ⇒ 按铁律④**必须重跑一次台账评估**。
- ⚠️ 旧文档可能写「连续 ≥3 天保住 **93.1%** 主线日」—— **那是错的**（见 §9.6），**以 77.5% 为准**。

### 3.2 ✅ A9 环境声明（纯文档，但有实操价值）

`requirements.txt` 头注释已写明 **源端 / 本机 / 新机器三种解释器情形**。
源端执行时请**顺手核对第 0 步取证第 4 项**（`python3 -V` + `pip freeze`）与该声明是否一致，不一致就改文档。
本机已实测：`/usr/bin/python3` **没有 pandas**；真实解释器在 `stock-doctor/.venv`（Python 3.9.6 / pandas 2.3.3）。
⚠️ 若源端确实用系统 `python3` 且它有依赖，请在 README 注明**「此为源端专用」**，避免他人照搬。

### 3.3 ✅ A10 测试与 CI（**强烈建议同步**）

本机已建 `tests/`（19 项结构断言）+ `make check`（语法 + 回归基线一条命令），**零新增依赖**。

**为什么源端也要有**：源端是数据权威，**重算后唯一能判断「这次重算有没有搞坏结构」的工具就在这里**：

```bash
cd ~/Quant/industry-radar
make check PY=$(which python3)      # 期望：19/19 全绿 + 0 WARN，退出码 0
```

⚠️ **测试分两层，别改坏**：**结构**（代码决定）硬断言进退出码 ｜ **数值**（数据决定）只报告。
若把数值写成硬断言，源端每多跑一次日更测试就红一次 —— **那种红没有信息量，只会训练人忽略测试**。
⚠️ 数值基线是**本机副本**的（`tests/baseline_snapshot.json`），源端首跑会报数值差异 —— **属预期**（源端数据更新），
**只关注结构层是否全绿**。
⚠️ 改产物口径时**必须同步 `tests/_common.py`**（期望 schema 写在那里），否则假回归会被 `--update` 无脑刷掉
= 悄悄关掉了测试。
**同步方式**（三选一，建议固化下来）：

```bash
# ① 推荐：git 打包 + scp（保持可追溯）
cd ~/Quant/industry-radar && git archive HEAD | gzip > /tmp/ir-code-$(git rev-parse --short HEAD).tar.gz
scp /tmp/ir-code-*.tar.gz hyc@100.78.155.22:/tmp/
# 源端：备份现版 → 解包
cd ~/Quant/industry-radar && cp -R . /tmp/ir-backup-$(date +%Y%m%d) && tar xzf /tmp/ir-code-*.tar.gz
```

> ⚠️ 解包会**覆盖工作树** ⇒ 源端若有未提交的手工改动，**第 0 步已让你 `git status` 记下来**，先处理再解包。
> ⚠️ **不要**直接 `rsync --delete` 整个仓库到源端（会删掉源端独有的未跟踪文件，如 `logs/`）。

---

## 4. 第 2 步 · 数据重算（**顺序不可乱**）

> 顺序依据：`docs/action-items.md` §A 的「执行顺序（已修订）」。**A3 与 A4 合并执行**。

```bash
cd ~/Quant/industry-radar

# ① 全历史 PIT 成分快照（A4；若源端此前只 490 天则本步必做）
python3 scripts/build_members_daily.py            # 预期 8,417,497 行 / 1636 天

# ② 市值加权涨跌幅 —— 必须全量，不能 --days 5
python3 scripts/compute_weighted_returns.py --days 2000

# ③ 等权行业日频聚合 —— 全量覆盖，顺带修 P0-2 坏值段
python3 scripts/build_industry_daily_full.py --days 2000 --force-overwrite
#   ⚠️ --force-overwrite 会跳过写入前自检（A7 护栏）；全量重建时属预期行为

# ④ regime（依赖加权涨跌幅，必须在 ② 之后）
python3 scripts/regime_classifier.py              # 预期「行数护栏: 旧 1636 → 新 1636 行 ✓」

# ⑤ 净值主表
python3 scripts/build_nav_924.py

# ⑥ 冷区台账重算（口径已变；upsert 只增改不删 ⇒ 必须先剔除旧记录）
cp ~/quant-data/signals/signals_log.parquet ~/quant-data/signals/signals_log.parquet.bak
python3 -c "
import pandas as pd
from src.paths import SIGNALS_LOG_PATH
df = pd.read_parquet(SIGNALS_LOG_PATH)
n = int((df['source'] == 'cold_zone').sum())
df[df['source'] != 'cold_zone'].to_parquet(SIGNALS_LOG_PATH, index=False)
print(f'已剔除 {n} 条旧 cold_zone 记录')
"
python3 scripts/build_signals_log.py --backfill-cold-zone --no-snapshot

# ⑦ mainline 跨期稳定性（A8 新增，**漏了它 = 产物停在旧 12 列**）
python3 scripts/mainline_detector.py

# ⑧ 评估（regime 分组由 5 类变 8 类）
python3 scripts/evaluate_signals.py
```

**验收**（不通过即停）：

| 检查 | 期望 |
|---|---|
| `industry_members_daily.parquet` | **8,417,497 行 / 1636 天** |
| `daily_full` 在 `2026-07-01~07-13` 的 `breadth` | **不再全 0**（至少部分行业非 0） |
| `daily_regime.parquet` 类别数 | **8 类**（含「结构性 / 偏强结构性 / 边缘结构性」） |
| `mainline_detection.csv` **列数** | **15 列**（原 12 + `is_mainline_raw` / `consec_days` / `min_consecutive_days`） |
| 台账 `cold_zone` 条数 | 与重算一致（原 2234 / 本机复算 2204，重算后会变，**以源端实跑为准**） |
| `evaluate_signals.py` 退出码 | 0 |
| `make check`（若已同步 A10） | **19/19 结构断言全绿**（数值差异属预期，见 §3.3） |

> ⚠️ **护栏要不要临时摘掉**：`--force-overwrite` 已跳过，无需摘。但**重算完记得恢复日更默认参数**（见 §9.3）。
> ⚠️ **⑦ 的位置很关键**：必须在 ⑧ 之前 —— 台账与报告都读 `mainline_detection.csv` 的 `is_mainline`。
> 漏跑 ⑦ ⇒ 产物停在旧 12 列（**代码新 / 产物旧的漂移态**），且 `make check` 会因列集合不符而红。

---

## 5. 第 3 步 · 基础设施固化（B 表）

| # | 做什么 | 验收 |
|:--:|---|---|
| **B1** | 把 §2 取证的「源端版本」记入 `docs/action-items.md` 或本文附录 | 有 HEAD + md5 记录，可与本机比对 |
| **B2** | **固化代码同步通道**（git bare 仓库 / rsync 脚本 / 定时打包均可，但要写下来） | 下次改代码不再是"手工拷贝凭记忆" |
| **B3** | 查清 `com.industry-radar.daily` 真实位置（或确认用别的调度）并更正 README | README 的命令在源端**能跑通** |
| **B4** | 路径大小写：`~/quant/industry-radar` → `~/Quant/industry-radar` | `grep -rn "quant/industry-radar"` 无残留 |
| **B5** | 记录源端 `python3 -V` + `pip freeze`，写进 README 或 `requirements.txt` | 与环境实际一致 |

> ✅ **B5 已部分完成**（2026-10-04 A9）：`requirements.txt` 头注释已写明**源端 / 本机 / 新机器三种解释器情形**。
> 源端只需**核对并按实情微调**（见 §3.2）。
> ⚠️ 本机已实测：`/usr/bin/python3` **没有 pandas**；真实解释器在 `stock-doctor/.venv`。
> ⚠️ **另有一处 README 内部矛盾待源端确认**：`:58` 说上游数据 **16:16** 更新、`:76` 说 **18:30** 才开始拉 —— 属调度口径。

---

## 6. 第 4 步 · 数据治理（C 表）

| # | 内容 | 依据 | 动作 |
|:--:|---|---|---|
| **C1** | 补拉情绪数据 `hsgt_top10` / `hk_hold` 的 2026-09-30 | 当初阻断台账的两个 `SENTI_MISSING` | 补拉 + 重跑台账 |
| **C2** | 补拉 `signals/industry_margin_daily.parquet`（停在 09-23） | 滞后 4 个交易日 | 增量重算 |
| **C3** | `industry/mainline_state.json` 去留 | **真孤儿**（mtime `2026-07-26`，仓库内 + 数据根内**零引用**） | **建议删除**；若保留请补生成器 |
| **C4** | 历史缺数/坏值的**追溯性检查** | `check_data_completeness.py` **只校验单目标日** | ⏸ **暂缓**（见 §9.4） |
| **C5** | 重算 `daily_full` 的 2026-07-01~07-13 坏值段 | P0-2 | **已并入 §4 第 ③ 步**（`--force-overwrite` 全量重算即覆盖） |

**另两项（本机实测新发现，建议一并处置）**：

| 内容 | 依据 | 建议 |
|---|---|---|
| **306 个未登记写入 / 90.7 MB**（`--audit-writes` 实测） | 签名审计首测 | 逐类归因：该登记的登记进 `src/paths.py:ARTIFACTS`，该删的删 |
| `mf_factors.parquet` **190 MB** 停滞 4 个月 | Hermes 废弃残留 | 属 session2 处置范围（§7） |

> 📌 **台账基线数修正**：`docs/action-items.md` 原写「2256 条」，实测为 **2257**（cold 2234 / tracker_pool 21 / mainline 2）。

---

## 7. 第 5 步 · 僵尸调度处置（session2，**暂停未取消**）

> 完整序列见 `docs/zombie-scheduler-remediation-2026-10-03.md` 与 `docs/task-prompts/session2/`（7 步）。
> **本机已取证完毕，只差源端动手。** 三条硬约束先记住：

1. 🔴🔴 **`~/.hermes/venv` 是活的生产依赖**：`com.market.web.plist` 用它跑 market-lens，
   **`KeepAlive` + `RunAtLoad` 且无 `ThrottleInterval`** ⇒ **删 `~/.hermes` 会让该服务立刻挂并无限重启**。
   ⇒ **「不删代码树」是硬约束**，处置调度 ≠ 删目录。
2. 🔴 **「废弃」≠「停止写入」**：`~/.openclaw/cron/jobs.json` 13 job / 11 enabled，其中 5 个仍 `cd ~/.hermes/quant`。
   **mtime 实测分层：仍活 = 16:30 + 16:55；已停 = 16:35；待源端复核 = 16:25 / 16:40**
   ⇒ 「5 个都在写」是错的，**可证实只有 2 个**。
3. **已拍板**：三个 job 选 **(c)** —— **只改 `enabled` 一个字段**（不含不可逆动作）；`daily-review-m2-industry` 停用；
   `market-evening-review` 先补取证。**备份一律不写进数据根。**

---

## 8. 第 6 步 · 凭据轮换（安全）

> 详见 `docs/credentials-remediation-2026-10-02.md`。**明文凭据已止血，但 token 未轮换。**

- 源端 **10 个脚本读 `config.yaml`**，且**无 plist 注入 token** ⇒ **配置文件不能直接删**。
- 待办：① 轮换 Tushare token ② 同步到 MacMini 侧 ③ 轮换后清空废纸篓。

---

## 9. 第 7 步 · 阶段 B 口径议题的处置结论

> 取证与复核见 `docs/stage-b-evidence-2026-10-04.md`。**主控已独立复算，报告的两处结论被改写。**

### 9.1 🔴 P0-3（本机可改代码，源端生效）

**问题**：`src/daily_review/module_02_industry.py:42` 的 `INDUSTRY_DAILY_CACHE = industry_daily.parquet`
是 **Hermes 16:55 废弃流水线**的产物；`:183-190` 缓存**优先命中**（覆盖目标日 + ≥30 天，实测 80 天 ⇒ **100% 命中**）
⇒ `:198` 的 `compute_concept_daily` **生产中几乎从不执行**。

**额外代价**：缓存只 80 天 vs `daily_full` 1636 天 ⇒ **日报拿不到长历史**（`:563` 请求 `days=80`）。

**处置**（顺序重要）：
1. **本机**：把 `module_02` 的取数改到 `industry_daily_full.parquet`（**不动其报告口径**，符合铁律 ②）。
   ⚠️ 改前须做公平对照：`avg_pct`/`n_stocks` 应零差异；`breadth` 会有差异（缓存坏值 + 语义漂移），属**修正**。
2. **源端**：重算 §4 第 ③ 步后生效。
3. ⚠️ **不要**再让 Hermes 那份 `industry_daily.parquet` 参与任何生产读取。

### 9.2 ✅ B7（regime）—— 确认 **重算**，不简化

- 定性 = **待重算**（代码 10-01 改、产物 09-30 未重跑），**不是缺陷、不是副本滞后**。
- **前提**：源端**已同步 `d3b024a`** ⇒ 属 §3 代码同步范围。
- ⛔ 「中间类样本不够所以简化」**不成立**：8 类下台账 2257 条信号**全 8 桶都过 `MIN_BUCKET_N=5`**（最小结构性 43 条）。
- 真正的风险不是类数，而是 **`MIN_BUCKET_N=5` 太松**（43 vs 1048 差 24 倍仍出结论）。
  ⇒ 建议给 `regime_study` 结果表**加一列 `n`**，让读者自己看到小样本桶。

### 9.3 🔶 B3（宽度 `min_periods`）—— 可缓；若做，走 **变体 B** + IS/OOS

- 🔴 **原提案不可行**：`min_periods=20` 对 `window=13` 的 ma13 **非法**（pandas 抛 `ValueError`）。
- 真实代价（主控复算，**推翻报告的 230 条**）：`real → 仅重算` **222 条**、`仅重算 → 改 min_periods` **仅 26 条**；
  **96.5% 是「重算顺带修好 7 月坏值」**，`min_periods` 本身只值 **1.2%**。
- 变体 A 与 B 集合差 **0 条** ⇒ **变体 B 就够**（只动 ma20 的 `min_periods`）。
- 结论：**不急**。若要做，必须按 IS/OOS 切分评估（§5 纪律 3）。

### 9.4 ⏸ C4（值异常规则）—— 暂缓

`check_data_completeness.py` 增「值异常」规则**会改变日报退出码**，可能让健康日也报错，阈值口径未定。
> ⚠️ 注意：A7 的写入护栏是**写入前**自检、**不参与退出码**，与本条互补、不是同一件事。

### 9.5 其他

| 议题 | 结论 |
|---|---|
| **B1 / D10** tracker 切 PIT | 等 9.1 处置后做；`module_02` 部分随 9.1 消失。🔑 **结构性结论**：`sources.py:173 cold_zone_history` 读的是 `daily_full`、**不经过 tracker** ⇒ **tracker 切 PIT 对冷区信号零影响**；真实影响面 = tracker 自身产物 + 台账 `tracker_pool`（21 条）+ 富途同步 |
| **B2 / D11** 成分对账 | **不改**（当下零影响）。若治，最小动作 = 修 `build_members_daily.py:63` 的 `out_date` **空串未归一化**（一行） |
| **B4 / D2** mainline 约束 | ✅ **已部分落地（`6f8bd6b`）** = 只上「连续 ≥3 天」。⏸ **成员数约束主人明确暂缓** —— 治独舞需另加「成员 ≥2」，代价是主线日 873→约 370 |
| **B5 / D3** 常量外置 | **本机侧改进**，见 `docs/improvement-roadmap-2026-10-04.md`。只做「外置」不做「重新拟合」（拟合已被证伪） |
| **D15** `find_ambush_candidates` 去留 | 现状即选项 ③（保留 + docstring 标注），零调用不会误导；**删不删待定** |

### 9.6 ⚠️ 数字纠错：「连续 ≥3 天保住 93.1% 主线日」是**错的**

旧路线图曾写这个数，**B1 落地时实测推翻**。若你在别处看到 93.1%，以本节为准：

| 口径 | 段数 | 单行业段 | 段长=1 的段 | 有主线日 |
|---|:--:|:--:|:--:|:--:|
| 基线（无约束） | 244 | 170（69.7%） | 74（30.3%） | 873（53.4%） |
| **连续 ≥3 天（实装）** | 131（−46.3%） | 89（67.9%） | **20（−73%）** | **677 = 保留 77.5%** |
| 连续 ≥2 天（备选，未实装） | 170 | 115（67.6%） | 39（22.9%） | 764 = 87.5% |

**93.1% 出自「回放后段内全保留」的事后口径，不可实盘** —— 日更产物 `mainline_detection.csv` 是
**当日快照、覆盖式**，第 1 天不可能知道第 2、3 天也是主线。实装刻意选**无前视**版本
（判定只用当天与前 2 个交易日）⇒ 才能进日更。
🔑 **通用教训：验收数字必须用「实盘可实现的口径」实测，先问「这个数字产得出来吗」。**
另一条实测结论：**跨期约束不治独舞**（单行业段 69.7%→67.9%，几乎没动）——**两种独立的病**。

---

## 10. 验收清单（一次性自检）

```bash
cd ~/Quant/industry-radar
python3 scripts/check_data_completeness.py            # 退出码 0
python3 -c "
import pandas as pd, os
from pathlib import Path
D = Path(os.path.expanduser('~/quant-data'))
f = pd.read_parquet(D/'industry/industry_daily_full.parquet'); f['date']=pd.to_datetime(f['date'])
r = pd.read_parquet(D/'industry/daily_regime.parquet');       r['date']=pd.to_datetime(r['date'])
m = pd.read_parquet(D/'industry/industry_members_daily.parquet')
s = pd.read_parquet(D/'signals/signals_log.parquet')
ml = pd.read_csv(D/'industry/mainline_detection.csv')          # A8 产物
bad = f[(f['date']>='2026-07-01')&(f['date']<='2026-07-13')]
print('daily_full 行数      ', len(f), '(期望 214,316+)')
print('07-01~07-13 breadth 全0?', bool((bad['breadth']==0).all()), '(期望 False)')
print('members_daily        ', len(m), '行 /', m['date'].nunique(), '天 (期望 8,417,497 / 1636)')
print('regime 类别数        ', r['regime'].nunique(), '(期望 8)')
print('mainline 列数        ', ml.shape[1], '(期望 15 —— 漏跑 §4 第 ⑦ 步会是 12)')
print('mainline 主线数      ', int(ml['is_mainline'].sum()), '/', len(ml), '个方向群')
print('台账总条数           ', len(s))
print('台账 cold_zone       ', int((s['source']=='cold_zone').sum()))
"
```

---

## 11. 回滚

| 动作 | 回滚 |
|---|---|
| 代码同步（§3） | 第 0 步的备份：`cp -R /tmp/ir-backup-YYYYMMDD/. ~/Quant/industry-radar/`（或用源端自己的 git） |
| 数据重算（§4） | 产物 `.bak`；`build_industry_daily_full.py` 本身是 upsert，`--force-overwrite` 前的旧文件建议先 `cp` |
| 台账重算（§4 ⑥） | `~/quant-data/signals/signals_log.parquet.bak` |
| 调度处置（§7） | 只改了 `enabled` 字段 ⇒ 改回即可（**这正是选 (c) 的理由**） |
| 凭据轮换（§8） | 新 token 生效后再删旧的；先确认 10 个脚本都能读到新值 |

> **总原则**：⚠️ **备份一律不写进 `~/quant-data`**（会被 rsync 同步到从端，污染副本）。

---

## 附：本方案的证据强度

| 结论 | 强度 |
|---|---|
| §0 的「当前可用」判定（5 条） | **本机实测**，可跨机复核 |
| §4 的重算命令与验收 | 来自 `action-items.md` §A，**已在源端跑过一轮**（2026-10-01） |
| §9.1 P0-3 的缓存命中与影响面 | **主控独立复算**（全表 131 行业逐日单边差） |
| §9.3 B3 的 26 条 | **主控效应分离复算**（推翻原报告的 230 条） |
| §7 调度三约束 | 本机取证 + 主人已拍板 |
| §3 同步方式、§10 自检脚本 | **未在源端实跑**（本机无写权限），属建议 |
