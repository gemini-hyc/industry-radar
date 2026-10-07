"""tests/test_resonance.py —— 链共振测量层的语义断言（**全合成数据，不读数据根**）。

为什么要有这个文件
------------------
review §12 的三项裁决把测量层从"讨论"变成了"生产状态量"，而它的**典型故障是静默的**：
风格调整失效时，调整值会悄悄退化成原始值，报表照常出数、看着很健康（`BREADTH_ZERO`
的同型病）。⇒ 必须有断言锁住三件事：

① **双输出**：(链, 日) 同时给**标量**与**该规模的 L1 分层零分布 z 值**（跨链不可直接比标量）；
② **差额可观测**：`style_share = 原始 − 调整` 必须出现在产物里，不得在测量层抹平；
③ **退化可报警**：**定向破坏自证** —— 把因子暴露清零 / 丢掉一个因子，探针必须报警。
   本项目的惯例是"这句话也要可复跑" ⇒ 探针不是写了就行，必须能被破坏并被抓住。

跑法：`python tests/test_resonance.py`
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.resonance import config as C  # noqa: E402
from src.resonance import group as G  # noqa: E402
from src.resonance import measure as M  # noqa: E402
from src.resonance import probe as PR  # noqa: E402


def synthetic_panel(n_days: int = 220, n_ind: int = 24,
                    seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """合成收益 + 风格因子。

    ⚠️ 因子必须是**市场中性**的（前一半 +β、后一半 −β，像真实的多空因子一样）：
    若因子与等权市场收益共线，市场回归会把整个因子吸收掉 ⇒ 原始残差里根本没有
    风格暴露，"调整"也就无从谈起（这会让本文件的断言全部失去意义）。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    codes = [f"I{i:03d}" for i in range(n_ind)]
    f = rng.normal(0, 0.008, size=n_days)
    beta = np.where(np.arange(n_ind) < n_ind // 2, 1.2, -1.2)
    eps = rng.normal(0, 0.0015, size=(n_days, n_ind))
    R = pd.DataFrame(np.outer(f, beta) + eps, index=dates, columns=codes)
    F = pd.DataFrame({"size": f, "value": rng.normal(0, 0.005, n_days),
                      "mom20": rng.normal(0, 0.005, n_days),
                      "vol20": rng.normal(0, 0.005, n_days),
                      "crowd": rng.normal(0, 0.004, n_days)}, index=dates)
    return R, F


def synthetic_corrs(n_ind: int = 24, seed: int = 3):
    """块结构相关阵：前 12 个行业同 L1（块内高相关），后 12 个分属不同 L1"""
    rng = np.random.default_rng(seed)
    codes = [f"I{i:03d}" for i in range(n_ind)]
    l1_of = {c: ("A" if i < 12 else f"L{i}") for i, c in enumerate(codes)}
    base = rng.normal(0, 0.02, size=(n_ind, n_ind))
    Cm = (base + base.T) / 2
    np.fill_diagonal(Cm, 1.0)
    blk = 0.30 + rng.normal(0, 0.03, size=(12, 12))     # 同 L1 块：中高相关 + 异质
    blk = (blk + blk.T) / 2
    np.fill_diagonal(blk, 1.0)
    Cm[:12, :12] = blk
    pools = G.l1_pools(l1_of, codes)
    return Cm, codes, l1_of, pools


class TestGroupStatistic(unittest.TestCase):
    def test_signed_and_pairwise_mean(self):
        Cm = np.array([[1.0, 0.5, -0.5], [0.5, 1.0, 0.2], [-0.5, 0.2, 1.0]])
        self.assertAlmostEqual(G.group_statistic(Cm, [0, 1, 2]),
                               (0.5 - 0.5 + 0.2) / 3, places=6)
        self.assertAlmostEqual(G.group_statistic(Cm, [0, 1]), 0.5, places=6)

    def test_too_small_group_is_nan(self):
        self.assertTrue(np.isnan(G.group_statistic(np.eye(3), [0])))

    def test_z_and_exceedance_are_finite(self):
        null = {"mean": 0.1, "std": 0.05, "samples": np.random.default_rng(0)
                .normal(0.1, 0.05, 500)}
        self.assertAlmostEqual(G.zscore(0.2, null), 2.0, places=6)
        self.assertTrue(0.0 <= G.exceedance(0.2, null) <= 1.0)


class TestStratifiedNull(unittest.TestCase):
    def test_same_l1_group_must_be_judged_against_same_l1_null(self):
        """§11.3 的量化理由：同 L1 构成下，全局零分布会**过松** ⇒ 分层 p95 必须更高。"""
        Cm, codes, l1_of, pools = synthetic_corrs()
        comp = {"A": 4}
        strat = G.stratified_null(Cm, pools, comp, n_iter=600)
        glob = G.global_null(Cm, list(range(len(codes))), size=4, n_iter=600)
        self.assertGreater(strat["p95"], glob["p95"],
                           "同 L1 构成下分层 p95 必须高于全局（全局过松 ⇒ 假阳性）")

    def test_seed_is_reproducible(self):
        Cm, codes, l1_of, pools = synthetic_corrs()
        a = G.stratified_null(Cm, pools, {"A": 3}, n_iter=200)
        b = G.stratified_null(Cm, pools, {"A": 3}, n_iter=200)
        self.assertEqual(a["p95"], b["p95"])


class TestResidualization(unittest.TestCase):
    def test_adjusted_residual_removes_style_exposure(self):
        R, F = synthetic_panel()
        rr = M.residualize(R, F, window=60)
        self.assertEqual(int(rr.n_factors_used.dropna().iloc[-1]), C.K)

        def mean_abs_corr(Pn, k, cols=slice(0, 12)):
            """只看**有暴露的那半**行业（另一半的 |corr| 是纯噪声，会稀释判据）"""
            X = Pn.tail(120).to_numpy(float)[:, cols]
            f = F[k].reindex(Pn.index).tail(120).to_numpy(float)
            m = np.isfinite(X).all(axis=1) & np.isfinite(f)
            ys, xs = X[m], f[m]
            c = (ys * xs[:, None]).mean(0) / (ys.std(0) * xs.std() + 1e-15)
            return float(np.nanmean(np.abs(c)))

        raw_c = mean_abs_corr(rr.raw, "size")
        adj_c = mean_abs_corr(rr.adj, "size")
        self.assertLess(adj_c, raw_c * 0.6,
                        f"调整残差对 size 的暴露必须大幅下降：raw={raw_c:.3f} adj={adj_c:.3f}")

    def test_style_share_is_positive_when_style_drives(self):
        """前 12 个行业纯由 size 驱动 ⇒ 它们的原始共振应显著高于调整共振。"""
        R, F = synthetic_panel()
        rr = M.residualize(R, F, window=60)
        cr = M.rolling_corr(rr.raw, window=60)
        ca = M.rolling_corr(rr.adj, window=60)
        last = max(set(cr) & set(ca))
        idx = list(range(0, 12))
        self.assertGreater(G.group_statistic(cr[last], idx),
                           G.group_statistic(ca[last], idx) + 0.1)


class TestDualOutput(unittest.TestCase):
    def test_measure_chains_gives_scalar_z_and_gap(self):
        R, F = synthetic_panel()
        rr = M.residualize(R, F, window=60)
        cr = M.rolling_corr(rr.raw, window=60)
        ca = M.rolling_corr(rr.adj, window=60)
        last = max(set(cr) & set(ca))
        cr = {last: cr[last]}
        ca = {last: ca[last]}
        codes = list(R.columns)
        l1_of = {"I%03d" % i: ("A" if i < 12 else f"L{i}") for i in range(len(codes))}
        pools = G.l1_pools(l1_of, codes)
        chains = {"pure_style": codes[:6], "cross": codes[12:18]}
        df = M.measure_chains(chains, cr, ca, codes, l1_of, pools, n_iter=400)
        self.assertEqual(len(df), 2)
        for col in ("resonance_raw", "resonance_adj", "style_share", "z_adj", "exceedance"):
            self.assertIn(col, df.columns, f"双输出缺列 {col}")
        row = df[df["chain_id"] == "pure_style"].iloc[0]
        self.assertAlmostEqual(row["style_share"],
                               row["resonance_raw"] - row["resonance_adj"], places=9)
        self.assertTrue(np.isfinite(row["z_adj"]))
        # 纯风格链的差额必须显著为正 ⇒ 差额**可观测**，没有被测量层抹平
        self.assertGreater(row["style_share"], 0.1)


class TestProbeDirectedBreak(unittest.TestCase):
    """**定向破坏自证**：把因子面板弄坏，探针必须抓到（本项目的惯例：这句话要可复跑）"""

    def _run(self, break_mode: str = "none"):
        R, F = synthetic_panel()
        if break_mode == "zero":          # 面板全废：调整值悄悄退化成原始值
            F = F.copy()
            F[:] = np.nan
        elif break_mode == "drop_one":    # 丢掉一个因子（回归会静默忽略它）
            F = F.copy()
            F["crowd"] = np.nan
        rr = M.residualize(R, F, window=60)
        cr = M.rolling_corr(rr.raw, window=60)
        ca = M.rolling_corr(rr.adj, window=60)
        if break_mode == "zero":
            ca = {d: cr[d] for d in cr}   # 退化：调整值 == 原始值
        codes = list(R.columns)
        return PR.run_probes(F, None, rr.n_factors_used, rr.adj, rr.raw, rr.betas,
                             codes, cr, ca, {}), rr

    def test_healthy_panel_passes(self):
        res, _ = self._run("none")
        self.assertTrue(res.ok, f"健康面板不该报警：{[i['code'] for i in res.issues]}")

    def test_zeroed_panel_is_caught(self):
        res, _ = self._run("zero")
        self.assertFalse(res.ok)
        codes = [i["code"] for i in res.issues]
        self.assertTrue(any(c.startswith("STYLE_PANEL") or c.startswith("RESID_ORTHO")
                            or c.startswith("CANARY") for c in codes), codes)
        self.assertIn("未调整", res.banner())          # 失败要**有声**：正文标注

    def test_dropped_factor_is_caught_by_count_not_by_orthogonality(self):
        """§12.5 修正二：corr(残差,因子)≈0 测的是实现，丢因子时它不报警 ⇒ 必须靠因子数断言。"""
        res, rr = self._run("drop_one")
        codes = [i["code"] for i in res.issues]
        self.assertIn("STYLE_FACTORS_DROPPED", codes, codes)
        self.assertLess(int(rr.n_factors_used.dropna().iloc[-1]), C.K)

    def test_canary_collapses_when_adjusted_and_alive_when_broken(self):
        canary = PR.canary_chain(self._betas_like(), [f"I{i:03d}" for i in range(24)])
        self.assertTrue(canary, "金丝雀必须能被构造出来")

        r = PR.ProbeResult()
        PR.probe_canary(0.276, 0.699, r)      # 健康：塌陷到 0.395
        self.assertTrue(r.ok)

        r2 = PR.ProbeResult()
        PR.probe_canary(0.699, 0.699, r2)     # 退化：adj == raw ⇒ 比值 1.0
        self.assertFalse(r2.ok)
        self.assertIn("CANARY_ALIVE", [i["code"] for i in r2.issues])

    def test_weak_canary_is_warning_not_silence(self):
        """金丝雀原始共振太低 ⇒ 它不会报警（假健康）⇒ 必须报 CANARY_WEAK 提醒。"""
        r = PR.ProbeResult()
        PR.probe_canary(0.03, 0.03, r)
        self.assertIn("CANARY_WEAK", [i["code"] for i in r.issues])

    @staticmethod
    def _betas_like() -> pd.DataFrame:
        rng = np.random.default_rng(11)
        codes = [f"I{i:03d}" for i in range(24)]
        b = rng.normal(0, 1, size=len(codes))
        b[:4] = 3.0                       # 前 4 个行业 size 暴露极端 ⇒ 金丝雀成员
        return pd.DataFrame({"date": ["2024-06-01"] * len(codes),
                             "ind_code": codes, "beta_size": b})


class TestProbeAcceptsRealPanelShape(unittest.TestCase):
    """探针必须吃**生产实际传入的形状**（2026-10-07 源端沙箱实跑暴露的缺陷）。

    背景：`scripts/measure_chain_resonance.py` 传给探针的 X 是 `build_style_panel.py`
    落盘的**long 表**（date × ind_code × 因子），而 `probe_panel_input` 原实现隐含假设
    X 是 wide 且末行是行业 ⇒ 真数据下直接抛
    `AttributeError: 'Timestamp' object has no attribute 'notna'`。

    这条路径此前从未被真数据走到过：`INDUSTRY_RADAR_READONLY` 只重定向**写入**，
    第 6 步读不到第 5 步产物 ⇒ X=None ⇒ 覆盖判据整段被跳过（假绿）。
    ⇒ 必须有测试把「long 表 + date 为索引」两种真形状钉死。
    """

    @staticmethod
    def _long_panel(n_ind: int = 40) -> pd.DataFrame:
        rng = np.random.default_rng(5)
        dates = pd.to_datetime(["2024-06-03", "2024-06-04"])
        rows = []
        for d in dates:
            for i in range(n_ind):
                rec = {"date": d, "ind_code": f"I{i:03d}"}
                for k in C.FACTORS:
                    rec[k] = rng.normal(0, 1)
                rows.append(rec)
        return pd.DataFrame(rows)

    @staticmethod
    def _factors() -> pd.DataFrame:
        rng = np.random.default_rng(7)
        idx = pd.to_datetime(["2024-06-03", "2024-06-04"])
        return pd.DataFrame(rng.normal(0, 0.01, size=(len(idx), len(C.FACTORS))),
                            index=idx, columns=list(C.FACTORS))

    def test_long_panel_with_ind_code_is_accepted(self):
        """生产形状（long + ind_code）必须能算出每因子当日覆盖数，且不误报。"""
        X = self._long_panel()
        r = PR.ProbeResult()
        PR.probe_panel_input(self._factors(), X, r, target_date=pd.Timestamp("2024-06-04"))
        cov = r.stats["industry_coverage_last"]
        self.assertIsInstance(cov, dict, f"覆盖统计必须是逐因子计数，实际 {type(cov)}")
        self.assertEqual(cov["size"], 40)
        self.assertEqual(r.stats["industry_rows_last"], 40)
        # 合成面板造不出 crowd 的 94/131 基线 ⇒ 允许且只允许报覆盖不足
        codes = [i["code"] for i in r.issues]
        self.assertTrue(set(codes) <= {"STYLE_PANEL_COVERAGE"}, codes)

    def test_panel_without_ind_code_skips_coverage_without_crashing(self):
        """无行业维度的面板（wide）谈「覆盖多少行业」无意义 ⇒ 跳过而非假报警。"""
        X = self._factors()
        r = PR.ProbeResult()
        PR.probe_panel_input(self._factors(), X, r,
                             target_date=pd.Timestamp("2024-06-04"))
        self.assertEqual(r.stats["industry_coverage_last"], "skipped(no ind_code)")
        self.assertEqual([i["code"] for i in r.issues], [])

    def test_missing_target_date_is_a_warning_not_a_silent_pass(self):
        """目标日没有行 ⇒ 「没跑」不等于「通过」，必须留痕。"""
        X = self._long_panel()
        r = PR.ProbeResult()
        PR.probe_panel_input(self._factors(), X, r, target_date=pd.Timestamp("2024-06-05"))
        self.assertIn("STYLE_PANEL_NO_ROWS", [i["code"] for i in r.issues])

    def test_normalize_panel_handles_date_index_and_column(self):
        a, has_a = PR._normalize_panel(self._factors())
        self.assertTrue(has_a is False)
        self.assertIn("date", a.columns)
        b, has_b = PR._normalize_panel(self._long_panel())
        self.assertTrue(has_b)
        self.assertEqual(len(b), 80)


class TestSpecIsDeclared(unittest.TestCase):
    def test_spec_carries_the_freedom_that_moved_three_pairs(self):
        spec = C.spec_dict()
        self.assertEqual(spec["standardize_scope"], "cross_section_daily")
        self.assertIn("window_days", spec)
        self.assertIn("with_intercept", spec)
        self.assertEqual(spec["K"], len(C.FACTORS))

    def test_probe_baseline_declares_per_factor_coverage(self):
        """crowd 只覆盖 94/131 个行业 ⇒ 覆盖判据必须按因子基线，不能用统一缺失率。"""
        self.assertEqual(C.FACTOR_EXPECTED_INDUSTRIES["crowd"], 94)
        self.assertEqual(C.FACTOR_EXPECTED_INDUSTRIES["size"], 131)


if __name__ == "__main__":
    unittest.main(verbosity=2)
