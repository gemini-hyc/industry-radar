"""tests/test_forward_returns.py —— 前瞻收益口径的**语义断言**（硬失败，零数据依赖）。

为什么单列一个文件
------------------
2026-10-05 出过一次代价很大的事故：探针把"未来 20 日"写成

    s.shift(-1).rolling(20).sum()        # ❌ t 处给的是 [t-18, t+1]

它与正确写法 `s.rolling(20).sum().shift(-20)` **形状完全相同**，因此
schema 断言、产物比对、IS/OOS 切分**全都抓不到**；唯一能抓到的是
"这个因变量与它的控制变量重合度 0.89" 这类**语义**断言。

本文件只断言「由**代码**决定的窗口语义」，不读任何数据 ⇒ 数据根缺失/滞后也能跑，
归入 `tests/run_tests.py` 的硬失败层（结构不变量）。

跑法：
    python tests/test_forward_returns.py
    python -m unittest discover -s tests
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from _common import C  # noqa: F401  （只为与其他测试文件一致的导入风格 + 确保 sys.path 就绪）

from src.signals.ledger import forward_sum
from src.utils.forward import forward_excess


def _toy() -> pd.DataFrame:
    """玩具序列 1..10（位置 T0..T9），窗口语义可手工核算。"""
    return pd.DataFrame({"x": np.arange(1, 11, dtype=float)},
                        index=[f"T{i}" for i in range(10)])


class TestForwardWindowSemantics(unittest.TestCase):
    """① 窗口语义：t 处必须恰好是 [t+1, t+h]，一天不多一天不少。"""

    def test_matches_manual_construction(self):
        """k=3：T2 处必须是 s[T3]+s[T4]+s[T5] = 4+5+6 = 15（**不是** 2+3+4=9）。"""
        out = forward_sum(_toy(), 3)["x"]
        self.assertEqual(out.loc["T0"], 2 + 3 + 4)      # 未来 3 天：T1..T3
        self.assertEqual(out.loc["T2"], 4 + 5 + 6)      # 未来 3 天：T3..T5
        self.assertEqual(out.loc["T6"], 8 + 9 + 10)     # 未来 3 天：T7..T9

    def test_tail_is_nan_not_fabricated(self):
        """末尾 h 天历史不足 ⇒ 必须是 NaN（宁标"未到期"，不臆造收益）。"""
        out = forward_sum(_toy(), 3)["x"]
        self.assertTrue(out.loc[["T7", "T8", "T9"]].isna().all(),
                        "末尾 3 天应因未来不足而缺失")
        self.assertEqual(int(out.notna().sum()), 7)

    def test_window_excludes_present_and_past(self):
        """前视守卫：篡改"当日及之前"不得改变结果；篡改"未来任一天"必须改变。"""
        base = pd.Series(np.arange(30, dtype=float))
        out = forward_sum(base.to_frame("x"), 5)["x"]

        for t in (0, 5, 17):
            past = base.copy()
            past.iloc[: t + 1] += 1000.0            # 变动当日与全部历史
            self.assertAlmostEqual(
                out.iloc[t], forward_sum(past.to_frame("x"), 5)["x"].iloc[t],
                places=10,
                msg=f"t={t}: 结果不应依赖当日及之前的数据（否则窗口含过去 ⇒ 前视）")

            future = base.copy()
            future.iloc[t + 3] += 1000.0            # 变动窗口内的某一天
            self.assertNotAlmostEqual(
                out.iloc[t], forward_sum(future.to_frame("x"), 5)["x"].iloc[t],
                places=6,
                msg=f"t={t}: 结果必须依赖窗口内的未来数据")


class TestWrongFormIsPinnedDown(unittest.TestCase):
    """② 把 2026-10-05 的错误写法钉死 —— 两者数值不同，防止"看起来一样"。"""

    def test_wrong_shift_order_differs_from_correct(self):
        toy = _toy()
        s = toy["x"]
        wrong = s.shift(-1).rolling(3, min_periods=3).sum()
        correct = forward_sum(toy, 3)["x"]

        self.assertEqual(wrong.loc["T2"], 2 + 3 + 4,
                         "错误写法在 T2 给的是 [t-1, t+1]（含 1 天未来）")
        self.assertEqual(correct.loc["T2"], 4 + 5 + 6,
                         "正确写法在 T2 给的是 [t+1, t+3]")
        self.assertNotEqual(wrong.loc["T2"], correct.loc["T2"],
                            "两种写法必须可区分；若相等说明 forward_sum 被改错了")

    def test_buggy_window_is_collinear_with_trailing_return(self):
        """事故机制复现：错误窗口与"过去 h 日收益"高度重合 ⇒ 同期被伪装成预测。

        这就是"IS/OOS 不衰减"却仍然错的原因：同期关系天然跨样本稳定。
        ⇒ 纪律：任何前瞻因变量，必须先做与**控制变量**的重合度体检。
        """
        rng = np.random.default_rng(20261005)
        r = pd.Series(rng.normal(0.0, 1.0, 4000))
        k = 20

        past = r.rolling(k, min_periods=k).sum().shift(1)
        wrong = r.shift(-1).rolling(k, min_periods=k).sum()
        right = forward_sum(r.to_frame("x"), k)["x"]

        # 说明性断言（用**手写**的错误形式，与本模块实现无关，恒定成立）
        self.assertGreater(wrong.corr(past), 0.5,
                           "手写错误写法应与『过去 h 日』高度重合（事故根因）")
        # 纪律性断言（考的是本模块实现）：正确窗口与"过去"无关
        self.assertLess(abs(right.corr(past)), 0.2,
                        "正确的前瞻窗口必须与过去收益近乎无关；"
                        "若这里红了 ⇒ forward_sum 的 shift/rolling 次序被改坏了")


class TestSingleImplementation(unittest.TestCase):
    """③ 唯一实现：utils 层只做 re-export，防止两处各写一份而漂移。"""

    def test_utils_reexports_same_object(self):
        from src.signals.ledger import forward_sum as a
        from src.utils.forward import forward_sum as b
        self.assertIs(a, b, "src/utils/forward.py 必须 re-export 台账的那一个实现")

    def test_forward_excess_subtracts_same_window_cross_section(self):
        """超额 = 前向窗口和 − **同窗口**横截面均值（台账 fwd_excess 口径）。"""
        panel = pd.DataFrame({"a": [1.0, 1.0, 1.0, 1.0, 1.0],
                              "b": [3.0, 3.0, 3.0, 3.0, 3.0]})
        fwd = forward_sum(panel, 2)
        exc = forward_excess(panel, 2)
        expected = fwd.sub(fwd.mean(axis=1), axis=0)
        pd.testing.assert_frame_equal(exc, expected)
        self.assertAlmostEqual(float(exc["a"].dropna().iloc[0]), -2.0, places=10)
        self.assertAlmostEqual(float(exc["b"].dropna().iloc[0]), +2.0, places=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
