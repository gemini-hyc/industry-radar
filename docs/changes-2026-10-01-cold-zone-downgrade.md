# 冷区宽度反转信号 · 降级为「观察项」（2026-10-01）

> 决策人：主人　｜　执行：2026-10-01
> 上位文档：`docs/changes-2026-10-01-cold-zone-event-oos.md`（事件级去重 + OOS 复核）
> 　　　　　`docs/changes-2026-10-01-cold-zone-recalib.md`（17 候选 × IS/OOS 校准研究）
> 相关：`docs/action-items.md` D7 / D17

---

## 一、决策

**冷区宽度反转信号（`cold_zone`）由「候选交易信号」降级为「观察项」。**

三选一的选项里取 **②**：

| 选项 | 结论 |
|---|---|
| ① 继续调参（收紧阈值 / 加量能或拥挤度过滤） | ❌ **否决** —— 证据显示调参过程本身失效（见 §二） |
| ② **降级为观察项** | ✅ **采纳（本次执行）** |
| ③ 换假设重做（如改为市场级择时） | ⏸ 未决 —— 需另行立项，不在现有参数上磨 |

**「观察项」的准确含义**（三件事，同时成立）：

1. **保留入账**：每日仍写入信号台账、仍回填前向收益 → 样本继续自然积累，将来可重评。
2. **保留展示**：日报第一章照常显示，带明确定位标注 → 它对"哪些冷门行业开始转暖"仍有描述价值。
3. **退出评估口径**：
   - **不作为交易依据**；
   - **不得据其调参**（因为样本内选择在样本外不成立，越调越自信）；
   - 评估报告里不再与其他信号并列为"候选信号"，而是单列为观察项。

> ⚠️ 注意：这不等于"删掉信号"。删掉会同时丢掉样本积累能力与历史可追溯性。

---

## 二、依据（为什么是「校准不可行」而不是「还没调好」）

来自 `docs/changes-2026-10-01-cold-zone-recalib.md`，17 个候选过滤器 × IS/OOS 切分（2023-07-01）×
两级口径（事件级 / 按日聚类）× 4 个持有期：

| 指标 | 数值 | 含义 |
|---|---|---|
| IS 上为正的候选 | **17 / 17（100%）** | 样本内"遍地黄金" |
| **IS 超额 vs OOS 超额的 Pearson 相关** | **+0.18**（Spearman +0.30） | 样本内排名几乎无法预测样本外 |
| OOS − IS 为负的候选 | **16 / 17** | 几乎全部衰减 |
| 最刺眼的组合（前宽 10~20% × 全面上涨） | IS **+1.20pp** → OOS **−0.36pp** | 由正转负 |

单个候选也经不起检验：

- **high 档（前宽<10%）** —— 唯一真正稳的（IS +0.93pp → OOS +0.89pp），但总样本仅 **245 条 / 1636 天**（日均 0.15 个），**无法构成组合**；按日聚类 t=1.20。
- **「前宽<15% × 10 日」** —— 68 次比较里唯一 IS/OOS 事件级都 t>2.3，但**按日聚类后 IS t=0.64、OOS t=0.10**，两级口径结论相反 → 不采纳。
- **regime 过滤** —— IS +0.98pp(t=3.14) → OOS **+0.18pp(t=0.60)**。这条同时**推翻了 `signal-ledger` 里"regime 内生化是最可能提升性价比的改动"的判断**（该建议已撤回）。

而信号自身在**两级去重后**也很弱（`docs/changes-2026-10-01-cold-zone-event-oos.md` §6）：

- 事件级：20 日 **+0.29pp (t=2.14)**、5 日 **+0.01pp (t=0.13)**；
- **按日聚类**（2,204 条只分布在 498 天）：20 日塌到 **+0.01pp (t=0.06)**；
- **2022 是唯一强年**（+1.33pp, t=4.06），**2023 年后连续不显著**（2024 −0.15 / 2025 −0.19）；
- 胜率长期 47~50%，20 日 +0.29pp **扣掉双边成本后基本不可交易**。

**结论**：问题不是"参数还没调好"，而是**在 1636 天 / 2,204 条样本上做样本内选优这件事本身不产生信息**。

---

## 三、改了什么

### 1. 信号注册表 —— 引入「定位」字段

`src/signals/sources.py` · `SOURCE_SPECS`

新增三个字段，成为"信号定位"的唯一真相来源：

| 字段 | 取值 | 用途 |
|---|---|---|
| `status` | `active` / `observational` / `pending` | 决定该来源在评估报告里的身份 |
| `status_short` | 一句话摘要 | 渲染进定位表 |
| `status_note` | 完整依据 | 渲染进该来源的详解章节 |

当前取值：`cold_zone = observational`；`tracker_pool` / `mainline` = `pending`（仅向前样本）。
同时新增 `STATUS_LABEL` / `STATUS_ORDER` / `ALL_STATUSES` / `COLD_ZONE_DOWNGRADED_ON` 常量。

### 2. 评估报告 —— 抬头先讲定位

`src/signals/evaluate.py` · `render_markdown()`

- 新增 **「## 信号定位」** 章节（排在台账概览之前），列出每个来源的定位与一句话说明，按 `active → pending → observational` 排序。
- 每个来源的详解章节新增 **`> 定位：**⛔ 观察项**　<完整依据>`** 一行。
- 观察项来源在该章开头追加统一警示（`OBSERVATIONAL_BANNER`）：**"仅用于持续观测样本积累，不作为交易依据，也不得据此调参"**。

> 统计表**照常计算与展示** —— 这是观察项的要求（要能持续看到样本积累的效果），不是遗漏。

### 3. 日报 —— 第一章加定位标注

`src/daily_review/module_02_industry.py` · `format_cold_zone_section()`

- 新增模块常量 `COLD_ZONE_STATUS_NOTE`，在**有信号 / 无信号两个分支**都输出一行引用块标注定位。
- 补一行消歧说明：分级档位里的 **「★ 观察」≠ 信号定位的「观察项」**。

### 4. 文档

- `docs/signal-ledger.md`：§一 增加定位声明；§四 抬头改为"观察项基线"；§五 撤回 regime 内生化建议并标注降级结论。
- `docs/action-items.md`：D7 标记方案 C 已完成；D17 标记 ② 已执行；追加归档行。

---

## 四、没改什么（同样重要）

- ❌ **未改 `detect_signals` 的任何逻辑**：触发条件、冷区阈值动态调制、分级档位、`expansion_rule` 默认值全部原样。
- ❌ **未改台账结构、未删任何台账记录**，`signals_log.parquet` 不动。
- ❌ **未改 `cron/industry_review_daily.py`**：冷区信号仍逐日入账。
- ❌ **数值零变化**：本次改动只影响「文本呈现」，任何统计量、任何信号条数都不变。

> ✅ **零回归验证**：沙箱 250 天窗口跑 `evaluate_signals.py` + 日报渲染，
> 统计表逐位与改动前一致（对照见 §六 自检命令）。真实数据根守卫通过（零文件被写入）。

---

## 五、回滚方式

改动全部是"标注"，回滚成本极低：

```bash
# 1) 改回定位（一行）
#    src/signals/sources.py -> SOURCE_SPECS["cold_zone"]["status"] = "active"
# 2) 报告标注随之消失（evaluate / module_02 的判定都读这个字段）
# 3) 若还要恢复 module_02 的旧文案：
git show <本次提交>^:src/daily_review/module_02_industry.py > src/daily_review/module_02_industry.py
```

**何时该回滚**：只有在拿到**新的、样本外的、两级口径同时成立**的证据之后 —— 而不是
"再调一版参数试试"。这是本次决策最核心的一条纪律。

---

## 六、自检命令（复现用）

```bash
cd ~/Quant/industry-radar

# 1) 沙箱评估报告：定位章节是否出现，统计表是否与改动前一致
INDUSTRY_RADAR_DATA=/tmp/ir-ab-test /Users/hyc/Quant/stock-doctor/.venv/bin/python \
  scripts/evaluate_signals.py --report /tmp/eval_check.md
sed -n '/## 信号定位/,/## 台账概览/p' /tmp/eval_check.md

# 2) 日报第一章渲染（两个分支）
/Users/hyc/Quant/stock-doctor/.venv/bin/python -c "
import importlib.util, sys
s = importlib.util.spec_from_file_location('m2', 'src/daily_review/module_02_industry.py')
m = importlib.util.module_from_spec(s); sys.modules['m2'] = m; s.loader.exec_module(m)
print(m.format_cold_zone_section([]))
"
```

---

## 七、后续（不在本次范围）

1. **A1~A4 源端同步仍未执行** —— 源端目前落后多个 commit，届时冷区台账会因 PIT 口径变更重算
   （约 10% 信号不同）。**重算 ≠ 重新启用**：定位仍是观察项。
2. **③ 换假设重做** 若要做，建议立独立项目（市场级择时 / 宽度动量的横截面含义），
   并且**先用真 OOS 设计**（参数在 IS 定、OOS 只验一次），否则会重现本次 +0.18 的困境。
3. `D16`：本机台账 2,234 条 vs 同数据生产档 2,204 条，差 30 条方向说不通，建议源端核对。
