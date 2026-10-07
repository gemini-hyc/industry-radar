"""风格因子面板（**一等产物**）—— review §12.2「复现三件套」的第 ① 件。

产出两张面板（均登记 `src/paths.py:ARTIFACTS`）：

    industry_style_panel.parquet    特征 X_{i,k,t}：date × ind_code × {size,value,mom20,vol20,crowd}
    industry_style_factors.parquet  因子收益 f_{k,t}：date × {size,value,mom20,vol20,crowd}

因子收益的构造（写死、可复现）
------------------------------
每天横截面上做一次多空组合：按 config.STANDARDIZE_SCOPE 把特征做当日横截面 z-score，
再去均值（使多空权重之和为零），

    f_{k,t} = Σ_i w_{i,k,t} · r_{i,t}

即因子 k 在 t 日的收益。权重取 **t−1 的特征**（滞后一日）⇒ 只用当日之前可知的信息，
不构成前视。

为什么不用主成分：PC 无经济身份、谱上无肘部、且会吸收多成员真链（锂电链死于 K=3）
—— review §5.2 已裁定 PC 投影只作辅助诊断。

数据源与诚实缺口
----------------
- size  ← `industry_weighted_returns.circ_mv`（现成）
- value ← `daily_basic/pb` 按**静态成分**聚合行业中位数。
          ⚠️ 数据根里**没有**行业层估值表，故 review §11.7 所说"value 现成"**不成立**，
          本模块自行聚合；静态成分 ⇒ 前约 1,150 天与 PIT 成分有偏差（与既有面板同口径）。
- mom20 / vol20 ← 收益面板自算（窗口 t−21..t−1，不含当日）
- crowd ← `factors/ind_crowd_turnover_daily.parquet`（个股级，需聚合；起于 2020-02-20
          ⇒ 起始约 30 个交易日缺失，这正是探针要抓的"面板缺失/滞后"）
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from src.paths import BASIC_DIR, FACTOR_DIR, INDUSTRY_DIR
from src.resonance import config as C

WR_PATH = INDUSTRY_DIR / "industry_weighted_returns.parquet"
MEM_PATH = INDUSTRY_DIR / "industry_members.parquet"
CROWD_PATH = FACTOR_DIR / "ind_crowd_turnover_daily.parquet"


# ─────────────────────────── 基础加载 ───────────────────────────
def load_returns() -> pd.DataFrame:
    """行业日收益宽表：index=date, columns=ind_code，单位小数"""
    wr = pd.read_parquet(WR_PATH, columns=["date", "ind_code", C.RETURN_COL])
    R = wr.pivot(index="date", columns="ind_code", values=C.RETURN_COL).sort_index()
    return R / C.RETURN_UNIT_DIV


def _stock_to_industry() -> pd.Series:
    """6 位股票代码 → ind_code（静态成分映射）"""
    mem = pd.read_parquet(MEM_PATH, columns=["stock_code", "ind_code"])
    mem["code6"] = mem["stock_code"].astype(str).str.split(".").str[-1]
    return mem.drop_duplicates("code6").set_index("code6")["ind_code"]


def _industry_median_from_daily_basic(col: str) -> pd.DataFrame:
    """从 daily_basic 逐日读一列，按静态成分聚合到行业（中位数）。"""
    s2i = _stock_to_industry()
    frames = []
    for p in sorted(BASIC_DIR.glob("*.parquet")):
        try:
            d = pd.read_parquet(p, columns=["ts_code", col])
        except Exception:
            continue
        d["code6"] = d["ts_code"].astype(str).str.split(".").str[0].str.zfill(6)
        d["ind_code"] = d["code6"].map(s2i)
        d = d.dropna(subset=["ind_code"])
        d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d[d[col] > 0]
        if not len(d):
            continue
        d["date"] = pd.Timestamp(p.stem)
        frames.append(d.groupby(["date", "ind_code"])[col].median().rename(col))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).reset_index().pivot(index="date", columns="ind_code",
                                                 values=col).sort_index()


def _industry_crowd() -> pd.DataFrame:
    """个股级拥挤度 → 行业等权均值。index=date, columns=ind_code"""
    s2i = _stock_to_industry()
    c = pd.read_parquet(CROWD_PATH, columns=["date", "code", "value"])
    c["code6"] = c["code"].astype(str).str.split(".").str[-1].str.zfill(6)
    c["ind_code"] = c["code6"].map(s2i)
    c = c.dropna(subset=["ind_code"])
    out = (c.groupby(["date", "ind_code"])["value"].mean()
             .rename("crowd").reset_index()
             .pivot(index="date", columns="ind_code", values="crowd").sort_index())
    out.index = pd.to_datetime(out.index)
    return out


# ─────────────────────────── 特征构造 ───────────────────────────
def build_characteristics(R: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """返回 {factor: DataFrame(date × ind_code)}；**均已滞后一日使用**。"""
    chars: Dict[str, pd.DataFrame] = {}

    # size：行业流通市值对数（同日可知，不滞后）
    wr = pd.read_parquet(WR_PATH, columns=["date", "ind_code", "circ_mv"])
    mv = wr.pivot(index="date", columns="ind_code", values="circ_mv").sort_index()
    mv = mv.reindex_like(R)
    chars["size"] = np.log(mv.where(mv > 0))

    # value：行业中位 PB 的倒数对数（高 = 便宜）
    pb = _industry_median_from_daily_basic("pb")
    if len(pb):
        pb = pb.reindex_like(R)
        chars["value"] = -np.log(pb.where(pb > 0))
    else:
        chars["value"] = pd.DataFrame(np.nan, index=R.index, columns=R.columns)

    # mom20 / vol20：t−21..t−1 的累计收益与波动
    lr = np.log1p(R)
    mom20 = lr.rolling(21, min_periods=15).sum().shift(1)
    vol20 = R.rolling(21, min_periods=15).std().shift(1)
    chars["mom20"] = mom20
    chars["vol20"] = vol20

    # crowd：滞后一日的行业拥挤度
    cr = _industry_crowd().reindex_like(R)
    chars["crowd"] = cr.shift(1)

    return chars


# ─────────────────────────── 因子收益 ───────────────────────────
def _cross_section_z(x: pd.Series) -> pd.Series:
    """当日横截面 z-score（config.STANDARDIZE_SCOPE 的唯一实现处）"""
    s = pd.to_numeric(x, errors="coerce")
    sd = s.std(ddof=0)
    if not sd or not np.isfinite(sd):
        return pd.Series(np.nan, index=x.index)
    return (s - s.mean()) / sd


def build_factor_returns(R: pd.DataFrame, chars: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """每天横截面做多空组合：f_{k,t} = Σ_i w_i · r_{i,t}，权重 = 去均值的横截面 z。

    当日有效行业数 < config.MIN_CS_N ⇒ 该日因子收益为 NaN（宁缺勿假）。
    """
    out = {}
    for k in C.FACTORS:
        Xk = chars.get(k)
        if Xk is None:
            out[k] = pd.Series(np.nan, index=R.index)
            continue
        Xk = Xk.reindex(index=R.index, columns=R.columns)
        vals = []
        for dt in R.index:
            xrow = Xk.loc[dt]
            rrow = R.loc[dt]
            okm = xrow.notna() & rrow.notna()
            if int(okm.sum()) < C.MIN_CS_N:
                vals.append(np.nan)
                continue
            z = _cross_section_z(xrow[okm])
            w = z - z.mean()                    # 多空：Σw = 0
            gross = float(w.abs().sum())        # 单位总暴露归一 ⇒ f 是"组合收益"而非"求和"
            vals.append(float((w * rrow[okm]).sum() / gross) if gross > 0 else np.nan)
        out[k] = pd.Series(vals, index=R.index)
    F = pd.DataFrame(out, index=R.index)
    want = [k for k in C.FACTORS if k in F.columns]
    return F[want]


def build_all() -> tuple[Dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    """一次性产出 (特征字典, 因子收益面板, 收益面板)"""
    R = load_returns()
    chars = build_characteristics(R)
    F = build_factor_returns(R, chars)
    return chars, F, R


def characteristics_to_long(chars: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """特征字典 → 长表 date × ind_code × factor（用于落盘为一等产物）"""
    frames = []
    for k, df in chars.items():
        s = df.stack().rename(k)
        frames.append(s)
    long = pd.concat(frames, axis=1).reset_index()
    long.columns = ["date", "ind_code"] + list(chars.keys())
    return long
