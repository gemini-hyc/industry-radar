"""tests/test_eval_inference.py —— 评估器**推断口径**的语义断言（硬失败，零数据依赖）。

为什么单列一个文件
------------------
2026-10-05 之前，`src/signals/evaluate.py` 用**朴素 t**（`mean/(std/√n)`）：
把「同一交易日的多个行业」与「重叠的前向窗口」都当成独立样本 ⇒ **t 系统性偏高**。
实测同一份台账：冷区 20 日口径 **朴素 t=+2.25 → 按日聚类 −0.06**；
另一个 20 日口径 **−5.49 → −1.38**（连"负向显著"也是假的）。

这类错误**不会**被 schema 断言、产物比对或数据快照抓到 —— 只能靠"估计量语义"断言。
本文件锁住三条：① 按日聚类必须按**日**而不是按**行**算 t；② `obs_type='state'` 的
连续交易日必须合并为一个事件，而 `obs_type='event'` **不得**被合并；
③ 非重叠抽样必须真的按 horizon 抽稀。

跑法：`python tests/test_eval_inference.py` 或 `python -m unittest discover -s tests`
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from _common import C  # noqa: F401  （确保 sys.path 就绪，风格与其它测试一致）

from src.signals.evaluate import (
    MIN_CLUSTER_N,
    cluster_t,
    dedup_events,
    nonoverlap_t,
    summarize,
)


def _mk(n_rows: int) -> pd.DataFrame:
    return pd.DataFrame({
        "source": ["mainline"] * n_rows,
        "obs_type": ["state"] * n_rows,
        "ind_code": ["A"] * n_rows,
        "signal_date": pd.to_datetime(pd.date_range("2026-01-05", periods=n_rows, freq="B")),
    })


class TestClusterT(unittest.TestCase):
    """① 按日聚类：t 必须按『日』算，而不是按『行业-日』算。"""

    def test_cluster_t_is_far_smaller_than_naive_when_same_day_correlated(self):
        rng = np.random.default_rng(20261005)
        n_days, n_ind = 40, 20
        day_effect = rng.normal(0.5, 1.0, n_days)          # 每日共同冲击（横截面完全相关）
        vals, dates = [], []
        for i, e in enumerate(day_effect):
            for _ in range(n_ind):
                vals.append(e)                              # 同日内无独立信息
                dates.append(pd.Timestamp("2026-01-05") + pd.Timedelta(days=i))
        v = pd.Series(vals, dtype=float)
        d = pd.Series(pd.to_datetime(dates))

        naive = float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v))))
        t_cl, n_days_out = cluster_t(v, d)

        # 按日聚类 = 先对每日求均值（此处每日恰好常数），再对 40 个日均值算 t
        g = v.groupby(d).mean()
        expect = float(g.mean() / (g.std(ddof=1) / np.sqrt(len(g))))
        self.assertEqual(n_days_out, n_days)
        self.assertAlmostEqual(t_cl, expect, places=10)
        self.assertLess(abs(t_cl), abs(naive) / 3.0,
                        "同日横截面相关时，聚类 t 必须显著小于朴素 t；"
                        "若二者接近 ⇒ 聚类没生效（估计量退化回朴素 t）")

    def test_single_day_returns_nan(self):
        v = pd.Series([1.0, 2.0, 3.0])
        d = pd.Series(pd.to_datetime(["2026-01-05"] * 3))
        t, nd = cluster_t(v, d)
        self.assertTrue(np.isnan(t), "只有 1 个独立日 ⇒ 无法聚类推断，应为 NaN")
        self.assertEqual(nd, 1)


class TestDedupEvents(unittest.TestCase):
    """② 事件级去重：state 合并、event 不合并。"""

    def test_state_rows_merge_into_one_event(self):
        df = _mk(5)                                     # 5 个连续交易日
        out = dedup_events(df)
        self.assertEqual(len(out), 1, "同行业连续 state 行必须合并为 1 个事件")

    def test_event_rows_are_not_merged(self):
        df = _mk(5).assign(obs_type="event", source="cold_zone")
        out = dedup_events(df)
        self.assertEqual(len(out), 5, "obs_type='event' 本身已是事件级 ⇒ 去重必须是 no-op")

    def test_gap_larger_than_threshold_stays_separate(self):
        df = _mk(1)
        df2 = _mk(1).assign(signal_date=[pd.Timestamp("2026-03-05")])   # 间隔 >> 7 天
        out = dedup_events(pd.concat([df, df2], ignore_index=True))
        self.assertEqual(len(out), 2, "间隔超过阈值应视为两个事件")


class TestNonOverlapT(unittest.TestCase):
    """③ 非重叠抽样：必须按 horizon 抽稀，而不是用全部行。"""

    def test_subsamples_every_horizon_dates(self):
        n = 100
        v = pd.Series(np.arange(n, dtype=float))
        d = pd.Series(pd.date_range("2026-01-05", periods=n, freq="B"))
        t20, n_sub = nonoverlap_t(v, d, 20)
        self.assertEqual(n_sub, 5, "100 个信号日按 20 抽稀 ⇒ 5 个")
        exp = v[::20]
        self.assertAlmostEqual(t20, float(exp.mean() / (exp.std(ddof=1) / np.sqrt(len(exp)))),
                               places=10)
        t100, n_sub100 = nonoverlap_t(v, d, 100)
        self.assertEqual(n_sub100, 1)
        self.assertTrue(np.isnan(t100), "子样本不足 2 ⇒ 应为 NaN")


class TestSummarize(unittest.TestCase):
    """④ 必须并列中位数与胜率；向后兼容（不传 dates 时不抛错）。"""

    def test_reports_median_and_win_and_cluster_t(self):
        v = pd.Series([-3.0, 1.0, 1.0, 1.0, 20.0])          # 均值被右尾拉动
        d = pd.Series(pd.to_datetime(
            ["2026-01-05", "2026-02-02", "2026-03-02", "2026-04-01", "2026-05-04"]))
        s = summarize(v, dates=d, horizon=5)
        self.assertEqual(s["median"], 1.0)
        self.assertGreater(s["mean"], s["median"], "本样例专门构造『均值 > 中位数』的尾部效应")
        self.assertEqual(s["win"], 0.8)
        self.assertEqual(s["n_days"], 5)
        self.assertFalse(np.isnan(s["t_cluster"]))
        # ⚠️ 只有 5 个信号日、horizon=5 ⇒ 抽稀后仅剩 1 个样本 ⇒ 非重叠 t 应为 NaN（不可推断）
        self.assertTrue(np.isnan(s["t_nonoverlap"]),
                        "子样本不足时非重叠 t 必须是 NaN，不得用全样本凑出一个 t")

    def test_nonoverlap_t_computable_with_enough_dates(self):
        n = 12
        v = pd.Series(np.linspace(-1, 1, n))
        d = pd.Series(pd.date_range("2026-01-05", periods=n, freq="B"))
        s = summarize(v, dates=d, horizon=4)
        self.assertEqual(s["n_days"], n)
        self.assertFalse(np.isnan(s["t_nonoverlap"]), "12 个信号日按 4 抽稀 ⇒ 3 个子样本 ⇒ 可算")

    def test_backward_compatible_without_dates(self):
        s = summarize(pd.Series([1.0, 2.0, 3.0]))
        self.assertEqual(s["n"], 3)
        self.assertFalse(np.isnan(s["t"]))              # 朴素 t 仍给出（仅供历史对照）
        self.assertTrue(np.isnan(s["t_cluster"]))       # 未给 dates ⇒ 聚类 t 缺失
        self.assertEqual(s["n_days"], 0)

    def test_thresholds_are_sane(self):
        self.assertGreaterEqual(MIN_CLUSTER_N, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
