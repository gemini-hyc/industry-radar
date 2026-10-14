"""tests/_common.py —— 回归基线测试的共享层。

设计前提（**必须理解，否则会写出错误的断言**）：

1. 本机 `~/quant-data` 是**只读副本**，会随源端同步而变化 ⇒ 测试断言的**对象**必须区分两类：
   - **结构不变量**（structure invariant）：行业数、列名、键唯一性、值域、跨产物日期关系。
     它们由**代码**决定，不该随数据增长而变 ⇒ 可以**硬断言**。
   - **数值快照**（numeric snapshot）：行数、日期范围、统计量。
     它们由**数据**决定，随源端日更而变 ⇒ **只记录并报告差异，不做失败项**。
   把数值快照写成硬断言，测试会在健康的数据增长下误报红；反之把结构不变量写成软报告，
   真正的 schema 漂移就会被放过。

2. 数据根一律走 `src/paths.py`（铁律①），因此：
   - 默认对 `~/quant-data` 跑；
   - 设 `INDUSTRY_RADAR_DATA=<沙箱>` 即可对**改动后的产物**跑同一套断言。
   ⇒ 与 `tools/dev_verify.sh` 天然配套：它产出 A/B 两份产物，本套件校验其结构。

3. 零新增依赖：只用标准库 + pandas（本机唯一有 pandas 的解释器见 README）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 让 `python tests/xxx.py` 能找到仓库包（src/）
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from src.paths import (  # noqa: E402
    ARTIFACTS,
    DATA_DIR,
    INDUSTRY_DIR,
    SIGNALS_DIR,
    SIGNALS_LOG_PATH,
)

TESTS_DIR = Path(__file__).resolve().parent
BASELINE_PATH = TESTS_DIR / "baseline_snapshot.json"

# ──────────────────────────────────────────────────────────────────────
# 被测产物的**期望 schema**（列集合，唯一真相来源 = 实测的生产产物）
#
# ⚠️ 改口径导致列增删时，**必须同步改这里** —— 否则测试红，而红得有理由：
#    它就是在提醒你「schema 变了，下游可能有消费者」。
# ⚠️ 只断言**列集合**（无序）；列序变化只给提示（下游按列名访问，重排通常无害）。
# ──────────────────────────────────────────────────────────────────────
EXPECTED_COLUMNS: dict[str, list[str]] = {
    "industry_daily_full": [
        "date", "avg_pct", "total_amount", "stock_n", "breadth", "breadth_ma13",
        "avg_open", "avg_close", "con_code", "n_stocks",
    ],
    "industry_weighted_returns": [
        "date", "ind_code", "weighted_pct", "circ_mv", "stock_n",
    ],
    "industry_members_daily": ["date", "ind_code", "stock_code"],
    "industry_members": ["stock_code", "ind_code", "ind_name"],
    "daily_regime": [
        "date", "up_pct", "mean_ret", "std_ret", "skew_ret", "top5_conc",
        # rank_autocorr：2026-10-01 regime v2 引入（changes-2026-10-01-p2-regime.md），
        #   但当时**漏登记进本契约**，潜伏至 2026-10-08 首次真实根刷新才转红
        #   ⇒ 2026-10-09 补登（本次修绿动作之一）。
        "rank_consistency", "rank_var_mean", "rank_autocorr", "regime", "month",
        "margin_bal", "margin_bal_chg_pct", "margin_rz_buy_share", "margin_leverage",
        "lhb_stock_n", "lhb_net", "lhb_inst_net", "lhb_hsgt_net",
        "block_amt", "block_disc", "north_top10_amt", "north_top10_net",
        "south_hold_ratio", "north_hold_ratio",
    ],
    "industry_nav_924": [
        "date", "ind_code", "ind_name", "nav_w", "nav_e", "nav_hs300",
        "rs_w", "mom20_w", "mom60_w",
    ],
    "mainline_detection": [
        "group_id", "group_names", "n_industries", "heat_ratio", "excess_ma20",
        "winrate", "amt_ma20", "hist_mean_amt", "is_mainline", "stage", "style", "od_ratio",
        # B1/D2（2026-10-04 跨期稳定性约束）新增 —— 改 mainline 口径时连同这三列一起维护
        "is_mainline_raw", "consec_days", "min_consecutive_days",
        # P0（2026-10-09 主线「记忆」只读字段）：解释用，**禁止**参与判定。
        #   同时修正 consec_days 的截断缺陷（原值被锁死在 ≤3）。
        "hot_age", "heat_slope20", "cum_excess_seg", "is_new_mainline",
    ],
    "signals_log": [
        "signal_date", "source", "obs_type", "ind_code", "ind_name", "score",
        "tier", "features", "logged_at",
        "fwd_ret_5", "fwd_ret_10", "fwd_ret_20", "fwd_ret_60",
        "fwd_excess_5", "fwd_excess_10", "fwd_excess_20", "fwd_excess_60",
    ],
    # ── B2/B3 链版主线（2026-10-12 已激活）──────────────────────
    # 来源：scripts/chain_mainline_detector.py（生产十步链第 7b 步）
    # 产物：industry/chain_mainline_detection.csv（22 列，chain_id 开头）
    # 与旧版 mainline_detection.csv（19 列，group_id 开头）**并存**，互不覆盖。
    # 历史：本条目 2026-10-11 先作「预备白名单」备好但未激活（B2 未接线，
    #   新版产物只在 _local_dev/，测试覆盖不到）；2026-10-12 B2 接入生产链后激活。
    "chain_mainline_detection": [
        # B2：群定义换成语义链 —— chain_id 替代 group_id
        "chain_id", "chain_names", "n_industries", "heat_ratio", "excess_ma20",
        "winrate", "amt_ma20", "hist_mean_amt", "is_mainline", "stage", "style", "od_ratio",
        # 与旧版同名的判定/可审计列
        "is_mainline_raw", "consec_days", "min_consecutive_days",
        "hot_age", "heat_slope20", "cum_excess_seg", "is_new_mainline",
        # B3（2026-10-11）：链内残差共振连续量，作**新增解释列**，不进 is_mainline 判定
        "resonance_adj_today", "resonance_z_today", "style_share_today",
    ],
}

# ⛔ 未激活的被测产物 —— 列名已在 EXPECTED_COLUMNS 备好，但**暂不纳入回归**。
#    激活方式：把名字从上方的 PENDING_UNDER_TEST 移除（即加进 UNDER_TEST）。
#    （2026-10-12：chain_mainline_detection 已激活并移出本集合。）
PENDING_UNDER_TEST: set[str] = set()

# ──────────────────────────────────────────────────────────────────────
# 允许缺失的列（白名单）—— 只豁免「**本机产物滞后于代码**」这一个原因
# ──────────────────────────────────────────────────────────────────────
# ⚠️ **不要用白名单掩盖真正的口径变更**：白名单只解决"代码已加列、本机产物还没重跑"
#    这一种情况；**新增/删除任何其他列仍会硬失败**。
#
# 为什么需要它：`mainline_detection.csv` 在本机停在 2026-09-30 的旧版，
#   而 B1/D2 的三列已由 `mainline_detector.py:303-305` 产出（2026-10-04 上线）。
#   本机数据根是源端的只读副本，同步已暂停 ⇒ 产物不会自己变新。
#   直接从EXPECTED_COLUMNS 删掉三列会让测试**永久失明**：源端重跑后新列出现也不报警。
#
# 🔴 源端重跑后应**回到严格模式**：把下面对应条目清空，
#    让 test_columns_exact_set 恢复硬断言（验收标准：产物 mtime > 2026-10-04）。
#
# ✅ 2026-10-09 已回到严格模式：十步链于 10-08 19:30 首次写入真实数据根，
#    mainline_detection.csv 已含 B1/D2 三列（实测 15 列，mtime 2026-10-09 19:30）
#    ⇒ 白名单清空，test_columns_exact_set 恢复硬断言。
#    （本次清理的直接原因：白名单未清导致套件静默红了约 3 天 —— 十步链不跑测试套件。）
TOLERATED_MISSING: dict[str, dict[str, str]] = {
    # 空字典 = 严格模式。新增豁免必须写明「产物滞后」这一唯一合法理由与撤销条件。
}

# 被测产物 → 文件路径。以 ARTIFACTS 登记表为真相来源（铁律：新增产物必须登记）。
# 只挑**有稳定结构语义**的产物；`industry_daily_cache`（Hermes 废弃流水线产物）
# 有意**不纳入**：它的存亡取决于是否停掉那条流水线，不该成为回归门槛。
UNDER_TEST: dict[str, Path] = {
    name: ARTIFACTS[name][0]
    for name in EXPECTED_COLUMNS
    if name in ARTIFACTS
}

# ──────────────────────────────────────────────────────────────────────
# 值域约束：列 → (下界, 上界)。**只列语义上确定有界的列。**
# ⚠️ `regime.up_pct` 是**百分数**（0~100），不是 0~1 —— 实测确认，勿想当然。
# ──────────────────────────────────────────────────────────────────────
RANGE_CONSTRAINTS: dict[str, dict[str, tuple[float, float]]] = {
    "industry_daily_full": {
        "breadth": (0.0, 1.0),
        "breadth_ma13": (0.0, 1.0),
        "n_stocks": (0, 6000),
    },
    "daily_regime": {
        "up_pct": (0.0, 100.0),
        # ⚠️ `top5_conc` **不能**约束为 [0,1]：`scripts/regime_classifier.py:50` 明确记载
        #    「【已知缺陷，暂不改】calc_top5_conc 在上涨行业不足 5 个时会返回负值」。
        #    实测 10 个负值全部落在全面下跌日（up_pct ≤ 1.6%）。
        #    ⇒ 改用**条件化**断言，见 `test_invariants.TestKnownDefects`。
        "rank_consistency": (-1.0, 1.0),
    },
}

# 已知缺陷的**条件化**约束：把「代码里写明的已知行为」与「异常扩散」区分开。
# 若负值出现在 up_pct 很高的日子 ⇒ 计算逻辑坏了，必须红。
TOP5_CONC_NEG_UP_PCT_MAX = 5.0   # 上涨行业 <5 个 ⇔ up_pct ≲ 3.8%，留安全边际

# regime 合法类别（唯一真相来源 = `scripts/regime_classifier.py:4-5` 的 docstring）。
# ⚠️ 断言的是「**产物的类别 ⊆ 合法集合**」而非「== 8 类」：
#    本机副本落到 5 类属**待重算**（见 docs/stage-b-evidence-2026-10-04.md B7），
#    不该让测试红；但若冒出**陌生的类名**（真正的代码/数据 bug），必须红。
REGIME_VALID_LABELS: frozenset[str] = frozenset({
    "全面上涨", "偏强结构性", "结构性", "边缘结构性",
    "轮动", "偏弱轮动", "混合", "全面下跌",
})

# 台账合法 source（新增信号来源须同步此表 + `src/signals/sources.py` 的 SOURCE_SPECS）
LEDGER_VALID_SOURCES: frozenset[str] = frozenset({
    "cold_zone", "tracker_pool", "mainline", "hot_zone", "mid_zone",
    # 🆕 2026-10-12：链版主线（已同步 sources.py::SOURCE_SPECS["chain_mainline"]）
    "chain_mainline",
})


def load(name: str) -> pd.DataFrame:
    """按登记表读取产物（CSV / parquet 自适应）。"""
    path = UNDER_TEST[name]
    if path.suffix == ".csv":
        df = pd.read_csv(path)
    else:
        df = pd.read_parquet(path)
    # 日期列统一为 datetime，避免「存储类型不同」造成的假差异
    for col in ("date", "signal_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
    return df


def load_ledger() -> pd.DataFrame:
    df = pd.read_parquet(SIGNALS_LOG_PATH)
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    return df


def data_root() -> Path:
    return Path(DATA_DIR)


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def snapshot_path() -> Path:
    return BASELINE_PATH


def read_baseline() -> dict | None:
    if BASELINE_PATH.exists():
        try:
            return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def write_baseline(obj: dict) -> None:
    BASELINE_PATH.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


# ──────────────────────────────────────────────────────────────────────
# 输出小工具（统一格式，便于人读）
# ──────────────────────────────────────────────────────────────────────
class C:
    """ANSI 颜色（非 TTY 时自动降级为空串）。"""
    _on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    OK, BAD, WARN, DIM, BOLD, END = (
        ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
        if _on else ("", "", "", "", "", "")
    )


def hr(char: str = "─", width: int = 76) -> None:
    print(char * width)
