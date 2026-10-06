"""tests/test_breadth_signal.py —— 宽度指标的**双重定位**防回归（硬失败层）。

背景
----
2026-10-05 主人问「宽度要不要用、怎么用」。评估结论是**双向**的，
而双向都很容易被无声推翻：

  ⛔ 对【收益】无预测力 —— IC=−0.004、非重叠 t=−0.74。
     任何"宽度能选行业"的说法都是错的。
  ✅ 对【波动率】有预测力 —— IC=+0.147、非重叠 t=+7.47，
     且控制"过去波动"后仍有增量（beta=+0.088，聚类稳健 t=+16.6）。

**为什么"没有 alpha"这一半也要锁住**：它同样是会消失的结论。
若哪天宽度对收益的 IC 变显著了，说明口径被改坏（最可能是前瞻窗口）
或市场结构真的变了 —— 两者都值得重新评估，而不是默默生效。

分层（依据 tests/README.md §结构 vs 数值）
----------------------------------------
| 类别 | 由谁决定 | 处置 |
|:---|:---|:---|
| **结构语义**（窗口、量纲、方向、符号） | 代码 | **硬断言**，数据根缺失也能跑 |
| **判据阈值**（t>3、\\|IC\\|<0.05） | 数据 | **只报告**，进数值层 |

⚠️ 阈值若写成硬断言，源端每跑一次日更就可能让它变红 ⇒ 测试天天红 ⇒
人开始忽略 ⇒ 比没有测试更糟（见 tests/README.md）。

跑法：
    python tests/test_breadth_signal.py
    python -m unittest discover -s tests
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from _common import C, hr, load  # noqa: F401

from src.research.breadth_eval import (
    MIN_STOCKS,
    breadth_forward_return_ic,
    breadth_forward_vol_ic,
    build_panel,
    cluster_se,
    cs_rank_ic,
    nonoverlap_t,
    nonoverlap_t_sorted,
    tail_concentration,
    ts_quantile,
)
from src.utils.forward import forward_sum


def _panel():
    return build_panel(load("industry_daily_full"))


# ══════════════════════════════════════════════════════════════════════
# ① 结构语义层（硬断言，零数据依赖）—— 锁住"口径不会被人改坏"
# ══════════════════════════════════════════════════════════════════════
class TestUnitAndSchema(unittest.TestCase):
    """① 量纲与主键 —— 这轮我踩的 `*100` 与 `con_code` 陷阱。"""

    def test_avg_pct_is_already_percentage_not_decimal(self):
        """⛔ `avg_pct` 单位**已经是百分数**，不可再 ×100。

        这条值得硬断言：2026-10-05 我因多乘 100 打出"40 日累计 +334%"，
        并据此怀疑整套面板数据有问题。判据取单日 |值| 中位数 ——
        百分数口径下应在 0.3~5 量级；若是小数口径则 <0.1。
        """
        d = load("industry_daily_full")
        med = float(d["avg_pct"].abs().median())
        self.assertGreater(med, 0.2,
                           f"|avg_pct| 中位数 {med:.4f} 偏小 ⇒ 可能是小数口径，"
                           f"读取方是否已 ×100？确认前不要在本项目里用它算收益。")
        self.assertLess(med, 8.0,
                        f"|avg_pct| 中位数 {med:.4f} 偏大 ⇒ 单位不是百分数，"
                        f"存在极端值污染，需先查 drop_abnormal_samples 是否生效。")

    def test_industry_key_column_is_con_code(self):
        """⛔ `industry_daily_full` 的行业主键列名是 `con_code`，**不是** `ind_code`。

        `industry_weighted_returns` / `industry_members_daily` 才是 `ind_code`。
        写 `rename(columns={'ind_code':'con_code'})` 会静默不生效 ⇒ 后续全错。
        """
        cols = set(load("industry_daily_full").columns)
        self.assertIn("con_code", cols)
        self.assertNotIn("ind_code", cols,
                         "若哪天 ind_code 也出现了，说明 schema 变了 —— "
                         "需同步 src/research/breadth_eval.build_panel 与 tests/_common.py")

    def test_panel_uses_breadth_and_n_stocks_filter(self):
        """面板必须按 `n_stocks >= MIN_STOCKS` 过滤，且成分数下限写死在模块里。"""
        self.assertEqual(MIN_STOCKS, 10, "改成分数下限会改变结论（分母太小则宽度是噪声）")
        p = _panel()
        self.assertLess(p["ret"].shape[1], 131,
                        "过滤后行业数应少于未过滤的 131；若相等说明过滤没生效")


class TestForwardWindowIsStrictlyFuture(unittest.TestCase):
    """② 前瞻窗口 = `[t+1, t+h]`，**一天不含当日**。

    2026-10-05 的主事故：错误写法 `s.shift(-1).rolling(h).sum()` 形状完全相同，
    schema / 产物比对 / IS/OOS 切分全都抓不到。

    ⚠️ 本组断言的写法经过一次修正（2026-10-05）：初版**只查序列第一个点**，
    而错窗口在短序列上恰好给 NaN、真实数据上又被 `dropna()` 剔掉
    ⇒ 定向破坏注入时**打不红**。修法两条：
      ① 查**多点**（t=0,5,10,…）而非单点；
      ② 用**非对称**序列（递增）—— 对称序列下 `shift` 的两个候选窗口可能
      因滑动窗口换掉一个端点而数值几乎相同（实测 `std` 两者完全相等）。
    ⇒ 判据是「每个点都必须等于该点右侧 h 天的真实聚合」。
    """

    H = 20

    def _panel_or_toy(self):
        return _panel()

    def test_vol_target_excludes_current_day_at_every_position(self):
        """未来波动率：在**每个**检查位上都必须等于严格右侧 20 日的真实标准差。

        ⚠️ `ddof` 必须显式写 `1` —— `rolling().std()` 默认 `ddof=1`（样本标准差），
        而 `np.std()` 默认 `ddof=0`（总体标准差），两者在 n=20 时差 **2.6%**。
        本断言初版漏写 ddof ⇒ 红了，而被红的是**正确的实现**。
        （同源陷阱见 docs/changes-2026-10-03-*.md 记录的 `rolling_rank_corr` ddof 修正。）
        """
        exc = self._panel_or_toy()["exc"]
        got = exc.rolling(self.H, min_periods=self.H).std().shift(-self.H)
        for t in (0, 1, 5, 20, 57):
            col = got.columns[t]
            expected = exc[col].values[t + 1: t + 1 + self.H].std(ddof=1)
            actual = got[col].values[t]
            self.assertTrue(np.isclose(actual, expected, equal_nan=True),
                            f"t={t} ({col}): 未来波动窗口不是严格 [t+1,t+{self.H}]"
                            f"（得到 {actual}，应为 {expected}；注意 ddof=1）")

    def test_vol_target_is_not_shift_by_h_minus_one(self):
        """定向反证：直接锁住 `breadth_forward_vol_ic` **内部**用的窗口。

        ⚠️ 这条经一次修正（2026-10-05）。初版比较的是"我自己写的 strict vs
        inclusive 两份"—— 它们**与被测实现无关** ⇒ 结构上无法检测实现被改
        （定向破坏注入后测试仍全绿，是无效断言）。
        ⇒ 改为断言「实现输出 == 手工 strict 计算」；若有人把实现改成
        `shift(-(h-1))`，实现会等于手工 inclusive（IC 0.1466 → 0.1615）⇒ 红。
        """
        p = self._panel_or_toy()
        h = 20
        got = breadth_forward_vol_ic(p, h)
        manual_strict = cs_rank_ic(
            p["breadth"],
            p["exc"].rolling(h, min_periods=h).std().shift(-h))
        self.assertAlmostEqual(got.ic_mean, float(manual_strict.mean()), places=10,
                               msg="breadth_forward_vol_ic 的窗口不是严格 [t+1,t+h]")
        # 与含当日版本的 IC 拉开距离（实测 0.1466 vs 0.1615）
        manual_incl = cs_rank_ic(
            p["breadth"],
            p["exc"].rolling(h, min_periods=h).std().shift(-(h - 1)))
        self.assertGreater(abs(manual_strict.mean() - manual_incl.mean()), 1e-4,
                           "两种窗口的 IC 必须可区分；否则本组断言失去检测力")

    def test_return_target_ic_matches_manual_strict_window(self):
        """同上一条，锁住 `breadth_forward_return_ic` 的窗口。"""
        p = self._panel_or_toy()
        h = 20
        got = breadth_forward_return_ic(p, h)
        manual_strict = cs_rank_ic(p["breadth"], forward_sum(p["exc"], h))
        self.assertAlmostEqual(got.ic_mean, float(manual_strict.mean()), places=10,
                               msg="breadth_forward_return_ic 的窗口不是严格 [t+1,t+h]")
        manual_wrong = cs_rank_ic(
            p["breadth"],
            p["exc"].shift(-1).rolling(h, min_periods=h).sum())
        self.assertGreater(abs(manual_strict.mean() - manual_wrong.mean()), 1e-6,
                           "错窗口的 IC 必须与正确窗口可区分；否则本组断言失去检测力")

    def test_return_target_excludes_current_day_at_every_position(self):
        """未来超额收益：复用台账唯一实现 `forward_sum`，多点校验。"""
        exc = self._panel_or_toy()["exc"]
        got = forward_sum(exc, self.H)
        for t in (0, 3, 17, 40):
            col = got.columns[t]
            expected = exc[col].values[t + 1: t + 1 + self.H].sum()
            self.assertTrue(np.isclose(got[col].values[t], expected, equal_nan=True),
                            f"t={t} ({col}): forward_sum 窗口不是严格 [t+1,t+{self.H}]")

    def test_wrong_window_form_would_be_collinear_with_past(self):
        """把事故机制钉死：错窗口与"过去 h 日"高度重合，正确窗口则无关。

        这条是**说明性**断言（用手写错误形式，与实现无关），防的是"有人又写回错形式"。
        """
        rng = np.random.default_rng(20261005)
        r = pd.Series(rng.normal(0.0, 1.0, 3000))
        k = self.H
        past = r.rolling(k, min_periods=k).sum().shift(1)
        wrong = r.shift(-1).rolling(k, min_periods=k).sum()
        right = forward_sum(r.to_frame("x"), k)["x"]
        self.assertGreater(wrong.corr(past), 0.5,
                           "手写错窗口应与『过去 h 日』高度重合（事故根因）")
        self.assertLess(abs(right.corr(past)), 0.2,
                        "正确窗口必须与过去收益近乎无关；红了 ⇒ shift/rolling 次序被改坏")


class TestQuantileIsStrictlyBackwardLooking(unittest.TestCase):
    """③ 时序分位不得含当日（`shift(1)` 缺失 = 前视）。

    ⚠️ 本组断言经过一次修正（2026-10-05）：初版用**递增序列**当夹具，
    而递增序列在**任何**滚动窗口内分位恒等于 1.0 ⇒ 夹具**没有区分力**，
    定向破坏（去掉 `shift(1)`）打不红。
    ⇒ 夹具必须**非单调**（否则分位退化），且扰动要落在**窗口中部**，
    使得"含当日 / 不含当日"两种窗口给出可区分的分位。
    """

    W, MP = 250, 120

    def _fixture(self):
        # 非单调：随机游走，保证任意窗口内分位分散在 (0,1)
        rng = np.random.default_rng(4242)
        return pd.DataFrame({
            "a": rng.normal(size=400).cumsum(),
            "b": rng.normal(size=400).cumsum(),
        })

    def test_min_periods_forces_early_nan(self):
        """样本不足 `min_periods` 时必须 NaN（未到期不臆造），不得退化成全样本分位。"""
        f = pd.DataFrame({"a": np.random.default_rng(1).normal(size=50).cumsum()})
        q = ts_quantile(f, self.W, self.MP)
        self.assertTrue(q["a"].isna().all(),
                        "样本不足 min_periods 时必须为 NaN，而不是给出一个假分位")

    def test_ts_quantile_excludes_current_day(self):
        """抬高**当日**读数不得改变**当日**分位。

        实现上 `shift(1)` 使 t 处分位取自窗口 `[t-w, t-1]`（不含当日）。
        若有人去掉 `shift(1)`，则窗口变 `[t-w+1, t]`（含当日），
        当日被抬到 1e9 后分位必然跳到 1.0 ⇒ 此断言变红。
        """
        f = self._fixture()
        base = ts_quantile(f, self.W, self.MP)
        bumped = f.copy()
        bumped.loc[300, "a"] = 1e9
        after = ts_quantile(bumped, self.W, self.MP)
        self.assertTrue(np.isclose(base.loc[300, "a"], after.loc[300, "a"], equal_nan=True),
                        "当日读数影响了当日分位 ⇒ 缺 shift(1)，存在前视")

    def test_ts_quantile_window_is_right_aligned_excluding_today(self):
        """定向反证：直接核算 t 处分位对应的窗口是 `[t-w, t-1]`。

        用**非单调**夹具 + 与 `shift(1)` 版逐点比较 ——
        单点 + 单调夹具的组合曾让本断言恒成立（见类 docstring）。
        """
        f = self._fixture()
        got = ts_quantile(f, self.W, self.MP)

        # 参考实现：显式写出"不含当日"的窗口
        def reference(col: np.ndarray) -> np.ndarray:
            out = np.full(len(col), np.nan)
            for t in range(len(col)):
                lo = t - self.W
                if lo < 0:
                    continue
                w = col[lo:t]                     # [t-w, t-1]
                ok = w[~np.isnan(w)]
                if len(ok) < self.MP:
                    continue
                out[t] = (ok <= col[t - 1]).mean()   # 以 t-1 的值在不含当日的窗口内的分位
            return out

        for name in ("a", "b"):
            exp = reference(f[name].values)
            valid = ~np.isnan(exp) & ~np.isnan(got[name].values)
            self.assertTrue(valid.sum() > 50,
                            f"{name}: 有效样本太少（{valid.sum()}），夹具或窗口参数有问题")
            # ts_quantile 的 apply 作用在 shift 后的序列上，末位元素即 t-1 的值
            self.assertTrue(np.allclose(got[name].values[valid], exp[valid], atol=1e-9),
                            f"{name}: 分位未对齐『不含当日』的右对齐窗口")

    def test_ts_quantile_differs_from_no_shift_variant(self):
        """显式反证：含当日版与严格版在真实夹具上**必须不同**。

        若此断言红了（两者相同）⇒ 夹具退化，需换随机种子而不是删断言。
        """
        f = self._fixture()

        def no_shift(frame, window, min_periods):
            return (frame.rolling(window, min_periods=min_periods)
                        .apply(lambda x: x.rank(pct=True).iloc[-1], raw=False))

        strict = ts_quantile(f, self.W, self.MP)
        loose = no_shift(f, self.W, self.MP)
        diff = float((strict - loose).abs().max().max(skipna=True))
        self.assertGreater(diff, 1e-6,
                           f"严格版与含当日版最大差仅 {diff:.2e} ⇒ 夹具无区分力，"
                           f"换种子/夹具，不要删这条断言")


class TestClusterRobustness(unittest.TestCase):
    """④ 聚类稳健标准误的 meat 构造 —— 防"t 虚高到 1e13"复现。"""

    def test_meat_uses_residuals_not_sum_of_residuals(self):
        """回归残差和 ≈ 0（已控制截距）⇒ 若 meat 用 (Σe)² 会塌成 0 ⇒ se→0。

        构造一个残差和几乎为 0 的样本，se 必须仍为有限正数。
        """
        rng = np.random.default_rng(11)
        n, g = 4000, 200
        df = pd.DataFrame({
            "y": rng.normal(size=n),
            "x": rng.normal(size=n),
        })
        df["date"] = np.repeat(np.arange(g), n // g)
        out = cluster_se(df, "y", ["x"])
        self.assertTrue(np.isfinite(out["se"]["x"]) and out["se"]["x"] > 0,
                        "se 塌成 0 ⇒ meat 构造写错了（是否误用 (Σe)²？）")
        self.assertTrue(np.isfinite(out["t"]["x"]), "t 必须是有限值（1e13 量级 = 实现错误）")

    def test_cluster_se_matches_manual_meat_construction(self):
        """聚类 se 必须与**手算 meat** 逐位一致。

        ⚠️ 这里刻意**不断言"聚类 se > 朴素 se"** —— 2026-10-05 实测该关系
        并非单向（比值在 0.83~1.23 间摆动，取决于簇级冲击在 z-score 后
        还剩多少）。断言一个未经验证的方向性关系 = 写一条会误导后人的测试。
        ⇒ 只锁死"实现 == 定义"，方向性交给具体应用的诊断。
        """
        rng = np.random.default_rng(5)
        for G, per, rho in [(10, 200, 0.9), (20, 200, 0.95), (50, 200, 0.5)]:
            n = G * per
            x = rng.normal(size=n)
            shock = rng.normal(size=G) * rho
            d = np.repeat(np.arange(G), per)
            e = shock[d] + rng.normal(size=n) * np.sqrt(1 - rho ** 2)
            df = pd.DataFrame({"y": 0.5 * x + e, "x": x, "date": d})

            got = cluster_se(df, "y", ["x"])

            # 手算：一元情形 meat 是标量 Σ_g (X_g'e_g)²，bread = 1/Σx²
            # （errstate 同 breadth_eval.cluster_se：本机 numpy 1.x 的
            #   `_core_matmul` 对任何线性代数路径都抛伪 warning，数值正确）
            z = lambda s: (s - s.mean()) / s.std()        # noqa: E731
            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                X = np.column_stack([np.ones(n), z(df["x"]).values])
                y = z(df["y"]).values
                beta, *_ = np.linalg.lstsq(X, y, rcond=None)
                resid = y - X @ beta
                sxx = float((X[:, 1] ** 2).sum())
                u_g = pd.Series(X[:, 1] * resid).groupby(d).sum()
                var = (float((u_g ** 2).sum()) / sxx ** 2) * (G / (G - 2))
                expected = float(np.sqrt(var))

            self.assertAlmostEqual(got["se"]["x"], expected, places=10,
                                   msg=f"G={G}, rho={rho}: 聚类 se 与手算 meat 不一致")
            self.assertAlmostEqual(got["beta"]["x"], float(beta[1]), places=10,
                                   msg=f"G={G}, rho={rho}: 系数应等于 OLS 系数")

    def test_cluster_count_is_reported(self):
        """聚类数必须被报出来 —— 它决定估计的有效性。"""
        df = pd.DataFrame({
            "y": np.random.default_rng(1).normal(size=1000),
            "x": np.random.default_rng(2).normal(size=1000),
            "date": np.repeat(np.arange(50), 20),
        })
        out = cluster_se(df, "y", ["x"])
        self.assertEqual(out["n_clusters"], 50, "聚类数应等于交易日数")
        self.assertEqual(out["n_obs"], 1000)

    def test_cluster_key_may_live_in_index_not_columns(self):
        """回归：2026-10-05 修掉的真 bug。

        `build_panel` 产出的是**宽表**，经 `.stack()` 后日期在
        MultiIndex 第 0 层而**不在列里**。原实现只按 `df[cluster]` 查找 ⇒
        生产代码调用直接 `KeyError: 'date'`。
        这条测试若红了 ⇒ `_grab` 又退回只查列。
        """
        idx = pd.MultiIndex.from_product(
            [pd.date_range("2024-01-01", periods=40, freq="B"),
             ["A", "B", "C", "D", "E"]],
            names=["date", "ind_code"])
        rng = np.random.default_rng(9)
        df = pd.DataFrame({"b": rng.normal(size=len(idx)),
                           "vf": rng.normal(size=len(idx))}, index=idx)
        out = cluster_se(df, "vf", ["b"])            # 不传 cluster，用默认 "date"
        self.assertEqual(out["n_clusters"], 40, "应从索引层识别出 40 个交易日")
        self.assertEqual(out["n_obs"], 200)

    def test_unknown_cluster_key_raises_clear_error(self):
        """找不到聚类键时必须给出**可诊断**的报错，而不是裸 KeyError。"""
        df = pd.DataFrame({"y": [1.0, 2.0], "x": [1.0, 2.0]})
        with self.assertRaises(KeyError) as cm:
            cluster_se(df, "y", ["x"], cluster="日期")
        self.assertIn("找不到聚类键", str(cm.exception))


# ══════════════════════════════════════════════════════════════════════
# ② 判据层（只报告，不影响退出码）—— 锁住"两条结论的方向与量级"
# ══════════════════════════════════════════════════════════════════════
class TestBreadthVerdictReported(unittest.TestCase):
    """⛔ 以下断言的阈值由**数据**决定 ⇒ 只作报告。

    源端每跑一次日更，`industry_daily_full` 就会多一天；虽然 20 日窗口的
    统计量在数百个交易日上相当稳定，但一旦源端修正历史数据（该项目正在做
    数据整改），这些数字会变。变红不代表代码坏了 —— 故不计入退出码。

    想把它们变成硬断言前，请先确认：源端数据整改已收官、且连续 N 次
    日更后这些数字纹丝不动。届时把 self.assert* 前的 `return` 删掉即可。
    """

    VOL_IC_MIN = 0.05          # 宽度→波动率的 IC 下限
    VOL_T_MIN = 3.0            # 非重叠 t 下限（朴素 t 会高估约 4.5 倍，不可用于此）
    RET_IC_ABS_MAX = 0.05      # 宽度→超额收益的 |IC| 上限

    def setUp(self):
        self.panel = _panel()

    def test_breadth_predicts_volatility_but_not_return(self):
        """**双向定位**：对波动率有效 ⇒ 对收益无效。

        这是本项目关于宽度最核心的一条约束。若它变红，两种可能：
          (a) 口径被改坏（先查前瞻窗口与量纲）；
          (b) 市场结构真的变了（那才值得重新评估信号）。
        两种都不该被无声放过。
        """
        vol = breadth_forward_vol_ic(self.panel, 20)
        ret = breadth_forward_return_ic(self.panel, 20)
        hr("─")
        print(f"  {C.DIM}宽度→未来20日波动率{C.END}  {vol.line()}")
        print(f"  {C.DIM}宽度→未来20日超额收益{C.END}  {ret.line()}")
        print(f"  {C.DIM}判据: vol IC>{self.VOL_IC_MIN} & 非重叠t>{self.VOL_T_MIN}"
              f"  |  ret |IC|<{self.RET_IC_ABS_MAX}{C.END}")
        hr("─")

        verdict_ok = (vol.ic_mean > self.VOL_IC_MIN
                      and vol.t_nonoverlap > self.VOL_T_MIN
                      and abs(ret.ic_mean) < self.RET_IC_ABS_MAX)
        if not verdict_ok:
            print(f"  {C.WARN}⚠️ 宽度定位与 2026-10-05 定论不符 —— "
                  f"先查口径（前瞻窗口/量纲/主键列名），再考虑重估{C.END}")
        else:
            print(f"  {C.OK}✅ 宽度定位与定论一致：对波动率有效、对收益无效{C.END}")
        # 故意不 assert —— 见类 docstring 的分层理由。

    def test_naive_t_overestimates_overlap_factor(self):
        """朴素 t 必须显著高于非重叠 t（重叠膨胀的证据）。

        若二者接近，说明 `nonoverlap_t` 没起作用（h 取值或切片错了），
        这会让所有 IC 判据失去意义 —— 故这条**可以**硬断言：
        它断言的是"两个 t 的**关系**"，不是"某个数值"。
        """
        vol = breadth_forward_vol_ic(self.panel, 20)
        self.assertGreater(vol.t_naive, vol.t_nonoverlap,
                           "朴素 t 应大于非重叠 t（重叠样本放大 t）；"
                           "若接近 ⇒ nonoverlap_t 的切片逻辑坏了")
        self.assertGreater(vol.t_naive / max(vol.t_nonoverlap, 1e-9), 2.0,
                           "实测高估约 4~5 倍；低于 2 倍说明重叠处理方式变了")

    def test_nonoverlap_t_helper_rejects_short_series(self):
        ic = pd.Series(np.random.default_rng(1).normal(size=100))
        t, n = nonoverlap_t(ic, 20)
        self.assertEqual(n, 5, "每 20 日取 1 个 ⇒ 100 个观测应得 5 个")
        self.assertTrue(np.isfinite(t))
        t2, n2 = nonoverlap_t(ic, 5000)
        self.assertTrue(np.isnan(t2) and n2 <= 1,
                        "步长超过序列长度时必须返回 NaN 而不是算出假 t")


# ══════════════════════════════════════════════════════════════════════
# ③ 事件研究层 —— 均值假象 与 抽稀排序陷阱（**结构层硬断言**）
# ══════════════════════════════════════════════════════════════════════
class TestTailConcentration(unittest.TestCase):
    """`tail_concentration` 必须能识破「均值靠右尾撑起来」。

    这条是硬断言（纯语义，不依赖真实数据）：2026-10-05 冷区 h=20
    均值 **+0.34pp**、中位数 **−0.13pp**、前 1% 贡献 **66%** 收益 ——
    只报均值会把"典型事件期望为负"说成"平均多赚 0.34 点"。
    """

    def test_detects_tail_driven_mean(self):
        rng = np.random.default_rng(7)
        body = rng.normal(0.0, 2.0, 990)          # 主体：均值 ≈ 0
        tail = np.full(10, 80.0)                   # 1% 极端右尾
        s = pd.Series(np.concatenate([body, tail]))
        r = tail_concentration(s, top_frac=0.01)
        self.assertGreater(r["mean"], 0.5, "构造上均值应为正")
        self.assertLess(r["median"], 0.5, "但中位数应接近 0 —— 这正是均值假象")
        self.assertLessEqual(r["net_over_abs"], 1.0, "抵消率定义上不可能 >1")
        self.assertGreater(r["net_over_abs"], 0.15,
                           "厚尾构造下净和应明显大于零（对照：对称分布 <0.05）")
        self.assertGreater(r["top_contrib_gross"], 0.2,
                           "前 1% 应吃掉 >20% 的正收益；识别失败说明口径写错")
        # 有判别力的对照：同样的 n 与 top_frac，主体换成对称分布
        sym = tail_concentration(pd.Series(rng.normal(0.0, 1.0, 1000)),
                                 top_frac=0.01)
        self.assertGreater(r["top_contrib_gross"], sym["top_contrib_gross"],
                           "厚尾样本的尾部集中度必须高于对称对照，否则指标无判别力")

    def test_symmetric_distribution_does_not_alarm(self):
        """⛔ 回归测试：对称分布下 `top_sum/total` 会发散（实测 ±4.14）。

        冷区实测抵消率 |Σx|/Σ|x| 仅 **0.080**；N(0,1) 为 **0.008**。
        本测试锁定「对称分布不因尾部指标而报警」—— 曾因用 `top_sum/total`
        当主判据而对任何数据都报警。
        """
        rng = np.random.default_rng(11)
        s = pd.Series(rng.normal(0.0, 1.0, 2000))
        r = tail_concentration(s, top_frac=0.01)
        self.assertLess(r["net_over_abs"], 0.05,
                        "对称分布净和应几乎完全抵消；否则 net_over_abs 失效")
        self.assertTrue(np.isnan(r["top_contrib_net"]),
                        "净和接近 0 时 top_contrib_net 必须给 NaN 而不是发散值")
        self.assertLess(r["top_contrib_gross"], 0.15,
                        "正数口径下前 1% 只应占约 1~7%；超过 15% 说明算法有偏")
        self.assertAlmostEqual(r["mean"], r["trimmed_mean"], delta=0.3,
                               msg="无厚尾时均值与去尾均值应接近")

    def test_returns_empty_for_tiny_sample(self):
        self.assertEqual(tail_concentration(pd.Series([1.0, 2.0])), {},
                         "样本 <10 必须返回空 dict，不能给出半可信的诊断")

    def test_trimmed_mean_differs_from_mean_under_thick_tail(self):
        """⛔ `trimmed_mean` 必须真的在"剪掉头尾"，否则去尾诊断是假的。

        定向破坏实测：把 `trimmed_mean` 改成 `mean` 后，**原先 6 条破坏里唯一
        漏抓的一条** —— 因为没有断言锁住"去尾均值与均值的关系"。
        本条即那道缺失的锁。

        实测标定（rng=7，主体 N(0,2)×990 + 尾部 80×10）：
          厚尾 gap = mean − trimmed_mean = **0.80**
          对称对照 gap = **0.027**（近 30 倍差异）
        ⇒ 阈值取 0.3，两侧都有充分余量。
        """
        rng = np.random.default_rng(7)
        body = rng.normal(0.0, 2.0, 990)
        tail = np.full(10, 80.0)                   # 1% 极端右尾
        r = tail_concentration(pd.Series(np.concatenate([body, tail])), top_frac=0.01)
        self.assertGreater(r["mean"] - r["trimmed_mean"], 0.3,
                           "厚尾样本的去尾均值必须显著低于原均值（否则没剪掉任何东西）")
        sym = tail_concentration(pd.Series(rng.normal(0.0, 1.0, 1000)))
        self.assertAlmostEqual(sym["trimmed_mean"], sym["mean"], delta=0.25,
                               msg="对称分布下两者应接近（这是对照组）")

    def test_mean_exceeds_median_is_detectable(self):
        """反向保护：正常分布里均值应≈中位数，指标不应误报厚尾。"""
        rng = np.random.default_rng(3)
        s = pd.Series(rng.normal(0.0, 1.0, 5000))
        r = tail_concentration(s)
        self.assertLess(abs(r["mean"] - r["median"]), 0.2)


class TestNonoverlapTSortingTrap(unittest.TestCase):
    """⛔ 未排序就抽稀 ⇒ 得到的 t 毫无意义（实测 −0.05 → −1.49）。

    事件表的行顺序是 (con_code, exp_start_date)，`iloc[::20]` 于是变成
    "按行业字母序每 20 个取 1 个"。**不报错、不告警**，只是结论完全错。
    """

    def test_sorted_and_unsorted_differ_on_shuffled_index(self):
        rng = np.random.default_rng(5)
        dates = pd.date_range("2020-01-01", periods=120, freq="B")
        vals = pd.Series(rng.normal(0, 1, 120), index=dates)
        shuffled = vals.sample(frac=1.0, random_state=1)      # 打乱索引顺序
        t_sorted, n_sorted = nonoverlap_t_sorted(vals, 20)
        t_bad, n_bad = nonoverlap_t(shuffled, 20)
        self.assertEqual(n_sorted, 6, "120 个观测 / 步长 20 = 6")
        self.assertEqual(n_bad, 6, "样本数相同 —— 错的只是抽到了哪些")
        self.assertNotAlmostEqual(t_sorted, t_bad, places=3,
                                  msg="未排序抽稀不得与排序抽稀给出同一个 t；"
                                      "若相同说明排序被忽略了")

    def test_helper_is_equivalent_to_explicit_sort(self):
        rng = np.random.default_rng(9)
        dates = pd.date_range("2021-01-01", periods=200, freq="B")
        vals = pd.Series(rng.normal(0, 1, 200), index=dates).sample(
            frac=1.0, random_state=2)
        t1, n1 = nonoverlap_t_sorted(vals, 20)
        t2, n2 = nonoverlap_t(vals.sort_index(), 20)
        self.assertEqual(n1, n2)
        self.assertAlmostEqual(t1, t2, places=12,
                               msg="nonoverlap_t_sorted 必须等价于「先排序再调用」")


class TestColdZoneConditionalVerdictReported(unittest.TestCase):
    """⛔ 数据层判据（只报告）—— 锁住「冷区 × 扩张 无预测力」这个结论。

    2026-10-05 实测（131 行业 2020-01~2026-09，因变量=行业等权超额）：
      · 冷区+扩张3天 h=20 均值 **+0.34pp**、**中位数 −0.13pp**、
        去 top5% 均值 **−0.42pp**、非重叠 t **−0.05**
      · 交互项 A−B−C+D = **−0.01pp** ⇒ 冷区与扩张无交互
      · 阳性对照：同纪律下 宽度→未来20日波动率 非重叠 t = **+7.47**
    """

    COLD_MEAN = 0.34
    COLD_MEDIAN_MAX = 0.0        # 中位数应 ≤ 0（均值假象的核心证据）
    COLD_T_ABS_MAX = 1.0         # 非重叠 |t| 应 < 1

    def test_cold_zone_mean_is_a_tail_illusion(self):
        """均值 > 0 但中位数 ≤ 0 ⇒ 结论只能写成"观察项"。

        这条断言的是**三者之间的关系**，不是某个具体数值 ⇒ 硬断言。
        """
        rng = np.random.default_rng(21)
        ev = pd.read_parquet("/tmp/ir-coldcond/out/events_final.parquet")
        s = ev[ev.is_cold].fwd_exc_20.dropna()
        if len(s) < 100:
            self.skipTest("冷区事件表不存在（探针未跑）；本条只锁定三者的关系")
        r = tail_concentration(s, top_frac=0.01)
        hr("─")
        print(f"  {C.DIM}冷区 h=20 超额{C.END}  n={r['n']}  均值={r['mean']:+.2f}pp  "
              f"中位数={r['median']:+.2f}pp  去top1%均值={r['trimmed_mean']:+.2f}pp")
        print(f"  {C.DIM}抵消率 |Σx|/Σ|x|{C.END} {r['net_over_abs']:.3f}  "
              f"负收益占比 {r['neg_ratio']:.1%}  "
              f"前1%占正收益 {r['top_contrib_gross']:.1%}（净和口径 {r['top_contrib_net']:.0%}）")
        hr("─")
        self.assertGreater(r["mean"], 0, "实测均值为正（否则本测试的前提失效）")
        self.assertLessEqual(r["median"], self.COLD_MEDIAN_MAX,
                             "若中位数转正 ⇒ 均值不再是尾部假象，结论需重估")
        self.assertLess(r["net_over_abs"], 0.20,
                        "实测抵消率 0.080（净和仅为绝对量的 8%）；"
                        "若显著更高说明正负不再抵消、分布结构变了")
        self.assertGreater(r["top_contrib_gross"], 0.05,
                           "实测前 1% 吃掉 9.8% 的正收益（事件数占比仅 1%）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
