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
        "rank_consistency", "rank_var_mean", "regime", "month",
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
    ],
    "tracker_report": [
        "code", "name", "status", "phase", "signal_date", "S0", "score",
        "entry_confidence", "days", "last_date", "elim_reason",
    ],
    "signals_log": [
        "signal_date", "source", "obs_type", "ind_code", "ind_name", "score",
        "tier", "features", "logged_at",
        "fwd_ret_5", "fwd_ret_10", "fwd_ret_20", "fwd_ret_60",
        "fwd_excess_5", "fwd_excess_10", "fwd_excess_20", "fwd_excess_60",
    ],
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
