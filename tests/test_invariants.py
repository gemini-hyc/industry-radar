"""tests/test_invariants.py —— **结构不变量**回归断言（硬失败）。

这里只放「由**代码**决定、不该随数据增长而变」的断言。
凡是「由**数据**决定、会随源端日更而变」的量（行数 / 日期范围 / 统计量），
一律去 `tests/snapshot.py`，**不要**写进本文件 —— 否则健康的数据增长会误报红。

跑法：
    python tests/test_invariants.py            # 直接跑，输出友好
    python -m unittest discover -s tests      # 标准 unittest 生态亦可
    INDUSTRY_RADAR_DATA=/tmp/ir-verify/data python tests/test_invariants.py   # 对沙箱产物跑

⚠️ 与 `tools/dev_verify.sh` 的分工：
    dev_verify   回答「我这次的改动，产物 diff 是什么」（同一份数据，两份代码）
    test_invariants 回答「当前产物结构是否健康」（单一时刻，一套断言）
  两者互补，不能互相替代。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

from _common import (
    EXPECTED_COLUMNS,
    LEDGER_VALID_SOURCES,
    PENDING_UNDER_TEST,
    RANGE_CONSTRAINTS,
    REGIME_VALID_LABELS,
    TOLERATED_MISSING,
    TOP5_CONC_NEG_UP_PCT_MAX,
    UNDER_TEST,
    C,
    data_root,
    load,
    load_ledger,
)

# ── 结构性常量（改动它们 = 承认行业分类体系变了，需人工确认）────────────
EXPECTED_INDUSTRY_COUNT = 131

# 语义上「永远不该有 NaN」的列（缺一个都说明上游抓取或聚合断了）
NO_NAN_COLUMNS: dict[str, list[str]] = {
    "industry_daily_full": ["date", "con_code", "avg_pct", "total_amount", "stock_n", "n_stocks"],
    "industry_weighted_returns": ["date", "ind_code", "weighted_pct", "circ_mv", "stock_n"],
    "daily_regime": ["date", "up_pct", "mean_ret", "regime"],
    "industry_nav_924": ["date", "ind_code", "nav_w"],
}

# 宽度列：结构性允许个别 NaN（MA 未成型期），但**整体退化**（P0-2 那种）必须抓住
BREADTH_COLUMNS = {"industry_daily_full": ["breadth", "breadth_ma13"]}
BREADTH_NAN_RATIO_MAX = 0.05


def _read_columns_fast(path: Path) -> list[str]:
    """只读文件**元数据**取列名（不加载数据 ⇒ parquet 秒级）。"""
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        return list(pq.read_schema(path).names)
    # CSV：读表头即可
    return list(pd.read_csv(path, nrows=0).columns)


class TestArtifactSchema(unittest.TestCase):
    """① 产物存在性 + 列集合不变量（schema 漂移检测）。"""

    def test_artifacts_exist_and_readable(self):
        missing = [f"{n} → {p}" for n, p in UNDER_TEST.items() if not p.exists()]
        self.assertFalse(missing, "以下登记产物不存在：\n  " + "\n  ".join(missing))

    def test_columns_exact_set(self):
        bad = []
        tolerated_notes = []
        for name, expected in EXPECTED_COLUMNS.items():
            # 预备白名单（未激活）跳过 —— 它们的产物还没进生产链路、没登记 ARTIFACTS，
            # 直接 UNDER_TEST[name] 会 KeyError。激活方式见 tests/_common.py:PENDING_UNDER_TEST。
            if name in PENDING_UNDER_TEST:
                continue
            path = UNDER_TEST[name]
            if not path.exists():
                continue
            actual = _read_columns_fast(path)
            allowed = TOLERATED_MISSING.get(name, {})
            # 白名单只吃掉「允许缺失」的列，其余（缺或多）一律仍算失败
            hard_expected = set(expected) - set(allowed)
            if set(actual) != set(hard_expected):
                missing = sorted(set(hard_expected) - set(actual))
                extra = sorted(set(actual) - set(hard_expected))
                bad.append(f"{name}: 缺 {missing} | 多 {extra}")
            # 白名单命中项单独记录：产物一旦补上就不该再报
            still_missing = sorted(set(allowed) & set(expected) - set(actual))
            if still_missing:
                tolerated_notes.extend(
                    f"    {name}.{c} — {allowed[c]}" for c in still_missing
                )
        if tolerated_notes:
            print(f"  {C.WARN}ℹ️  以下列在EXPECTED_COLUMNS 中但本机产物尚缺（已按白名单豁免，"
                  f"属产物滞后而非口径变更）：{C.END}")
            for line in tolerated_notes:
                print(line)
        self.assertFalse(
            bad,
            "列集合与 EXPECTED_COLUMNS 不符（改口径请同步更新 tests/_common.py）：\n  "
            + "\n  ".join(bad),
        )

    def test_column_order_hint(self):
        """列序变化通常无害，只提示不失败。"""
        for name, expected in EXPECTED_COLUMNS.items():
            if name in PENDING_UNDER_TEST:
                continue
            path = UNDER_TEST[name]
            if not path.exists():
                continue
            actual = _read_columns_fast(path)
            if set(actual) == set(expected) and actual != expected:
                print(f"  {C.WARN}ℹ️  {name}: 列集合一致但顺序不同"
                      f"（若下游按位置取值需留意）{C.END}")


class TestKeyIntegrity(unittest.TestCase):
    """② 唯一键 —— 键重复会静默放大下游统计，是最隐蔽的一类脏数据。"""

    KEYS = {
        "industry_daily_full": ["date", "con_code"],
        "industry_weighted_returns": ["date", "ind_code"],
        "industry_members_daily": ["date", "ind_code", "stock_code"],
        "industry_members": ["stock_code", "ind_code"],
        "industry_nav_924": ["date", "ind_code"],
        "daily_regime": ["date"],
    }

    def test_no_duplicate_keys(self):
        bad = []
        for name, keys in self.KEYS.items():
            if not UNDER_TEST[name].exists():
                continue
            df = load(name)
            dup = int(df.duplicated(subset=keys).sum())
            if dup:
                bad.append(f"{name}: {keys} 有 {dup} 行重复")
        self.assertFalse(bad, "唯一键重复：\n  " + "\n  ".join(bad))


class TestGridCompleteness(unittest.TestCase):
    """③ 完整网格 —— 行业 × 交易日 应为满格；缺格 = 静默漏算（而非报错）。"""

    # 🔴 2026-10-15：industry_weighted_returns 的网格基准改为 industry_members_daily。
    #    原因：SW2021 分类改革把「电子化学品Ⅱ / 休闲食品 / 房地产服务 / 旅游零售Ⅱ」
    #    4 个行业的建立日定在 **2021-07-30**（index_member_all_raw 里最早 in_date=20210730），
    #    此前它们本就不该有行。旧判据要求「131 行业 × 1642 日」满格，等于倒逼 1528 格
    #    不存在行业的假数据 —— 旧表里那些 2020 年的行正是 PIT 成员改造前的陈旧残留。
    #    新判据更强：要求与当日真实成员**双向一致**（零缺、零多余），既防漏算也防幽灵行。
    GRID_FROM_MEMBERS = {"industry_weighted_returns": "ind_code"}

    @staticmethod
    def _pairs(df, icol):
        d = pd.to_datetime(df["date"])
        return set(zip(d.tolist(), df[icol].tolist()))

    def test_full_grid(self):
        for name, icol in [
            ("industry_daily_full", "con_code"),
            ("industry_weighted_returns", "ind_code"),
            ("industry_nav_924", "ind_code"),
        ]:
            if not UNDER_TEST[name].exists():
                continue
            with self.subTest(artifact=name):
                df = load(name)
                if name in self.GRID_FROM_MEMBERS:
                    self._assert_grid_matches_members(name, icol, df)
                    continue
                ni, nd = df[icol].nunique(), df["date"].nunique()
                self.assertEqual(
                    ni * nd, len(df),
                    f"{name}: {ni} 行业 × {nd} 交易日 = {ni*nd:,}，实际 {len(df):,} "
                    f"⇒ 缺 {ni*nd - len(df)} 格（或有多余组合）",
                )

    def _assert_grid_matches_members(self, name, icol, df):
        mem = load("industry_members_daily")
        exp = self._pairs(mem, icol)
        act = self._pairs(df, icol)
        miss, extra = exp - act, act - exp
        self.assertFalse(
            miss or extra,
            f"{name} 与 industry_members_daily 的「行业-天」网格不一致："
            f"有成员却无收益行 {len(miss)} 格、无成员却有收益行 {len(extra)} 格"
            + (f"。缺失样例 {sorted(miss)[:3]}" if miss else "")
            + (f"。多余样例 {sorted(extra)[:3]}" if extra else ""),
        )

    def test_members_daily_not_grid(self):
        """PIT 成员表**刻意**不是满格（每日真实成分不同）—— 反向断言，防误改。"""
        name = "industry_members_daily"
        if not UNDER_TEST[name].exists():
            self.skipTest("产物不存在")
        df = load(name)
        ni, nd = df["ind_code"].nunique(), df["date"].nunique()
        self.assertNotEqual(
            ni * nd, len(df),
            f"{name} 变成了满格（{ni}×{nd}）—— 若真如此，说明 PIT 语义被破坏，"
            f"退回了静态成分（见 docs/action-items.md D8/D10）",
        )


class TestIndustryUniverse(unittest.TestCase):
    """④ 行业集合 —— 131 个且**各产物完全一致**（口径分裂的探针）。"""

    def test_industry_count(self):
        df = load("industry_daily_full")
        n = df["con_code"].nunique()
        self.assertEqual(
            n, EXPECTED_INDUSTRY_COUNT,
            f"行业数 {n} ≠ {EXPECTED_INDUSTRY_COUNT}。若申万调整了行业分类，"
            f"请同步 tests/test_invariants.py 的常量并写 change doc。",
        )

    def test_industry_sets_identical_across_artifacts(self):
        ref = set(load("industry_daily_full")["con_code"])
        pairs = [
            ("industry_weighted_returns", "ind_code"),
            ("industry_members_daily", "ind_code"),
            ("industry_members", "ind_code"),
            ("industry_nav_924", "ind_code"),
        ]
        bad = []
        for name, col in pairs:
            if not UNDER_TEST[name].exists():
                continue
            s = set(load(name)[col])
            if s != ref:
                bad.append(f"{name}.{col}: 缺 {sorted(ref - s)[:5]} | 多 {sorted(s - ref)[:5]}")
        # 🔴 2026-10-06：tracker_report 的行业全集断言随跟踪池弃用一并移除
        #    （装置零产出、脚本与产物在第 2/5 批清理）。依据
        #    docs/plan/retirement-breadth-tracker-plan.md §2.4。
        self.assertFalse(bad, "行业集合不一致（口径分裂）：\n  " + "\n  ".join(bad))


class TestDateIntegrity(unittest.TestCase):
    """⑤ 日期 —— 单调、无重复、跨产物子集关系。"""

    def test_dates_monotonic(self):
        """⚠️ **不能**断言「date 无重复」—— 面板数据里每天天然有 131 行。

        要抓的错误模式是「日期回跳」（upsert 把旧日期 append 到末尾、或乱序拼接）：
        那会让 `is_monotonic_increasing` 为假。同时校验日期去重后无断裂。
        """
        for name in ["industry_daily_full", "industry_weighted_returns",
                     "industry_members_daily", "industry_nav_924", "daily_regime"]:
            if not UNDER_TEST[name].exists():
                continue
            with self.subTest(artifact=name):
                d = load(name)["date"]
                self.assertTrue(
                    bool(d.is_monotonic_increasing),
                    f"{name}: date 非单调递增 ⇒ 拼接/upsert 顺序可能出错"
                    f"（实测末 3 个值：{list(d.tail(3))}）",
                )
                uniq = d.drop_duplicates()
                self.assertEqual(len(uniq), d.nunique(), f"{name}: 日期集合自相矛盾")

    def test_date_subsets_of_full(self):
        ref = set(load("industry_daily_full")["date"])
        for name in ["industry_weighted_returns", "industry_members_daily",
                     "industry_nav_924", "daily_regime"]:
            if not UNDER_TEST[name].exists():
                continue
            with self.subTest(artifact=name):
                s = set(load(name)["date"])
                extra = sorted(x.date().isoformat() for x in (s - ref))
                self.assertFalse(
                    extra,
                    f"{name} 有 {len(extra)} 个日期不在 industry_daily_full 里：{extra[:5]} "
                    f"⇒ 下游产物跑在了主面板之前（时序错位）",
                )


class TestValueRanges(unittest.TestCase):
    """⑥ 值域 —— 比率类列越界说明口径或计算有误。"""

    def test_ranges(self):
        bad = []
        for name, cols in RANGE_CONSTRAINTS.items():
            if not UNDER_TEST[name].exists():
                continue
            df = load(name)
            for col, (lo, hi) in cols.items():
                if col not in df.columns:
                    continue
                v = df[col].dropna()
                if len(v) == 0:
                    continue
                n_out = int(((v < lo) | (v > hi)).sum())
                if n_out:
                    bad.append(f"{name}.{col}: {n_out} 个值越界 [{lo}, {hi}]，"
                               f"实测范围 [{v.min():.6g}, {v.max():.6g}]")
        self.assertFalse(bad, "值域越界：\n  " + "\n  ".join(bad))


class TestKnownDefects(unittest.TestCase):
    """⑥′ 已知缺陷的**条件化**断言 —— 允许已知行为，抓住异常扩散。

    把「代码里写明的已知缺陷」和「真正的回归」分开，是回归基线测试最容易被忽略的一环：
    若直接断言「top5_conc ∈ [0,1]」，测试从第一天起就红，久而久之没人再看它。
    """

    def test_top5_conc_negative_only_when_few_risers(self):
        """`regime_classifier.py:50` 记载：上涨行业不足 5 个时 `calc_top5_conc` 返回负值。

        ⇒ 负值是**已知行为**；但若它出现在普涨日（up_pct 高），说明逻辑坏了。
        """
        name = "daily_regime"
        if not UNDER_TEST[name].exists():
            self.skipTest("产物不存在")
        df = load(name)
        neg = df[df["top5_conc"] < 0]
        if neg.empty:
            return  # 缺陷已被修掉，也通过
        offenders = neg[neg["up_pct"] > TOP5_CONC_NEG_UP_PCT_MAX]
        self.assertFalse(
            len(offenders),
            f"top5_conc 负值出现在 up_pct > {TOP5_CONC_NEG_UP_PCT_MAX}% 的日子 "
            f"（{len(offenders)} 行，如 {offenders[['date', 'up_pct', 'top5_conc']].head(3).to_dict('records')}）"
            f" ⇒ 与 regime_classifier.py:50 记载的已知行为不符，疑似计算逻辑回归",
        )
        print(f"  {C.DIM}ℹ️  top5_conc 有 {len(neg)} 个负值，全部在 up_pct ≤ "
              f"{neg['up_pct'].max():.2f}% 的下跌日 —— 符合已知缺陷描述{C.END}")


class TestNoMissingValues(unittest.TestCase):
    """⑦ 关键列缺失 —— 缺一个都说明上游断链。"""

    def test_no_nan(self):
        bad = []
        for name, cols in NO_NAN_COLUMNS.items():
            if not UNDER_TEST[name].exists():
                continue
            df = load(name)
            for col in cols:
                if col not in df.columns:
                    continue
                n = int(df[col].isna().sum())
                if n:
                    bad.append(f"{name}.{col}: {n}/{len(df)} 行为 NaN")
        self.assertFalse(bad, "关键列存在缺失：\n  " + "\n  ".join(bad))

    def test_breadth_not_degenerated(self):
        """宽度列允许个别 NaN（MA 未成型），但整体退化必须抓（P0-2 那类事故）。"""
        for name, cols in BREADTH_COLUMNS.items():
            if not UNDER_TEST[name].exists():
                continue
            df = load(name)
            for col in cols:
                with self.subTest(artifact=name, column=col):
                    ratio = float(df[col].isna().mean())
                    self.assertLess(
                        ratio, BREADTH_NAN_RATIO_MAX,
                        f"{name}.{col}: NaN 占比 {ratio:.1%} ≥ {BREADTH_NAN_RATIO_MAX:.0%} "
                        f"⇒ 疑似 MA 未成型导致宽度整体退化，见 "
                        f"docs/changes-2026-10-03-daily-full-breadth-anomaly.md",
                    )


class TestRegimeLabels(unittest.TestCase):
    """⑧ regime 类别 —— 「⊆ 合法集合」而非「== 8 类」。

    本机副本落到 5 类属**待重算**（B7，见 docs/stage-b-evidence-2026-10-04.md），
    不该让测试红；但冒出**陌生类名**（真正的代码/数据 bug）必须红。
    """

    def test_labels_are_known(self):
        name = "daily_regime"
        if not UNDER_TEST[name].exists():
            self.skipTest("产物不存在")
        unknown = sorted(set(load(name)["regime"].dropna()) - REGIME_VALID_LABELS)
        self.assertFalse(
            unknown,
            f"daily_regime 出现未知类别 {unknown}；合法集合见 scripts/regime_classifier.py 与 "
            f"tests/_common.py:REGIME_VALID_LABELS",
        )


class TestLedger(unittest.TestCase):
    """⑨ 信号台账 —— 来源合法、日期可被主面板覆盖。"""

    def test_sources_known(self):
        lg = load_ledger()
        unknown = sorted(set(lg["source"].dropna()) - LEDGER_VALID_SOURCES)
        self.assertFalse(
            unknown,
            f"台账出现未登记来源 {unknown} ⇒ 新增信号须走 {{{{SOURCE_SPECS}}}} 登记 + "
            f"tests/_common.py:LEDGER_VALID_SOURCES（铁律③：新信号先入台账）",
        )

    def test_signal_dates_within_panel(self):
        lg = load_ledger()
        ref = set(load("industry_daily_full")["date"])
        extra = sorted(x.date().isoformat() for x in (set(lg["signal_date"]) - ref))
        self.assertFalse(
            extra,
            f"台账有 {len(extra)} 个 signal_date 超出主面板覆盖范围：{extra[:5]} "
            f"⇒ 台账与数据不同步",
        )

    def test_key_columns_present(self):
        lg = load_ledger()
        for col in ("signal_date", "source", "ind_code"):
            self.assertGreater(int(lg[col].notna().sum()), 0, f"台账 {col} 全为空")


class TestMainlineArtifact(unittest.TestCase):
    """⑩ mainline 快照 —— 只做结构检查（它是单点快照，行数由当日数据决定）。"""

    def test_structure(self):
        name = "mainline_detection"
        if not UNDER_TEST[name].exists():
            self.skipTest("产物不存在")
        df = load(name)
        self.assertGreater(len(df), 0, "mainline_detection.csv 为空")
        self.assertTrue(
            set(df["is_mainline"].unique()) <= {True, False, 1, 0},
            f"is_mainline 取值异常：{sorted(df['is_mainline'].unique())}",
        )
        self.assertTrue(
            (df["n_industries"] >= 1).all(),
            "n_industries 出现 < 1 的值",
        )


if __name__ == "__main__":
    print(f"{C.BOLD}═══ 结构不变量回归断言 ═══{C.END}")
    print(f"数据根: {data_root()}")
    print(f"{C.DIM}（硬失败项 = 由代码决定的契约；数值漂移不在此处判定）{C.END}")
    print("─" * 76)
    # ⚠️ `unittest.main(exit=False)` 不会设置退出码 ⇒ 脚本化调用会拿到**假绿**。
    #    必须自己取 wasSuccessful() 后显式 sys.exit —— 这是最容易埋雷的一处。
    _prog = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if _prog.result.wasSuccessful() else 1)
