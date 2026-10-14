"""
第四步·估值腿完整验证：覆盖全估值口径。

背景：03 步只用「PB 横截面分位」就宣布「估值腿证伪」，下结论太草率。
本步补测所有估值口径 × 两种分位（横截面 / 历史），回答：
  估值这条腿，到底有没有任何可用的前瞻力？

口径设计：
  - 指标：pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm（7 个估值指标）
  - 分位口径（关键，之前遗漏）：
      ① 横截面 rank：当日该行业估值在全行业中的排名（谁相对便宜）
      ② 历史分位：该行业估值相对自身过去 5 年的分位（是否处于历史低位=修复空间）
  - 聚合：行业中位数（主）；circ_mv 加权（敏感性）
  - 检验：横截面 rank IC -> 未来 20/60/120 日超额，按报告期聚类 t 检验
  - 基准：等权全行业超额

产出：research/prosperity/04_验证报告.md
"""
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from pathlib import Path
from scipy import stats

DATA = Path("/Users/hyc/quant-data")
ROOT = Path("/Users/hyc/quant/industry-radar")
OUT_MD = ROOT / "research/prosperity/04_验证报告.md"

VALUATION_COLS = ["pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm"]
WINDOWS = [20, 60, 120]
HIST_WINDOW = 1250  # 约 5 年交易日，用于历史分位

# 行业成分最新一日（用末日成分，近似静态映射，避免逐日 844 万行 join 爆炸）
def ts_to_futu(ts):
    if not isinstance(ts, str) or "." not in ts:
        return None
    code, ex = ts.split(".")
    return f"{ex.lower()}.{code.lower()}"


def build_stock_to_ind(mem_latest):
    """个股 futu 代码 -> 申万二级 ind_code"""
    return dict(zip(mem_latest["stock_code"], mem_latest["ind_code"]))


def load_valuation():
    """载入每日 daily_basic，转成 个股(行)×日(列) 的宽表，返回 dict: col -> DataFrame"""
    frames = {c: [] for c in VALUATION_COLS}
    for f in sorted((DATA / "daily_basic").glob("*.parquet")):
        try:
            d = pq.read_table(f).to_pandas()
        except Exception:
            continue
        d = d.rename(columns={"trade_date": "date"})
        d["date"] = pd.to_datetime(d["date"])
        for c in VALUATION_COLS:
            if c in d.columns:
                sub = d[["ts_code", "date", c]].copy()
                sub["code"] = sub["ts_code"].map(ts_to_futu)
                sub = sub.dropna(subset=["code"])
                frames[c].append(sub)
    out = {}
    for c in VALUATION_COLS:
        allv = pd.concat(frames[c], ignore_index=True)
        piv = allv.pivot_table(index="date", columns="code", values=c).sort_index()
        out[c] = piv
    return out


def log(msg):
    print(msg, flush=True)


def main():
    log("载入行业成分与收益…")
    mem = pq.read_table(DATA / "industry/industry_members_daily.parquet").to_pandas()
    mem["date"] = pd.to_datetime(mem["date"])
    latest = mem["date"].max()
    mem_latest = mem[mem["date"] == latest]
    s2i = build_stock_to_ind(mem_latest)

    wr = pq.read_table(DATA / "industry/industry_weighted_returns.parquet").to_pandas()
    wr["date"] = pd.to_datetime(wr["date"])
    R = wr.pivot_table(index="date", columns="ind_code", values="weighted_pct").sort_index()
    EX = R.sub(R.mean(axis=1), axis=0)
    FWD = {n: EX.rolling(n).sum().shift(-n) for n in WINDOWS}

    # 市值权重（用 circ_mv 做敏感性）
    log("载入估值宽表…")
    val = load_valuation()

    # 市值宽表（用于加权聚合敏感性）
    mv_piv = None
    f_last = sorted((DATA / "daily_basic").glob("*.parquet"))[-1]
    try:
        d = pq.read_table(f_last).to_pandas().rename(columns={"trade_date": "date"})
        d["date"] = pd.to_datetime(d["date"])
        d["code"] = d["ts_code"].map(ts_to_futu)
        mv_piv = d.pivot_table(index="date", columns="code", values="circ_mv")
    except Exception:
        pass

    # 逐日把个股估值聚合成行业估值
    log("聚合个股估值到行业（中位数 + 历史分位）…")
    # 预构建：对每个估值列，转成 行业×日 面板
    ind_median = {}   # col -> DataFrame(行业×日) 行业中位数
    for c in VALUATION_COLS:
        v = val[c]
        # 个股 -> 行业（用静态映射）
        v = v.rename(columns=s2i)
        v = v.groupby(axis=1, level=0).median()  # 同行业多列取中位（pandas groupby on columns）
        v = v.reindex(index=R.index, columns=R.columns)
        ind_median[c] = v
        log(f"  {c}: 行业中位数面板 {v.shape}")

    # 计算两种分位口径 + 检验
    results = []
    for c in VALUATION_COLS:
        X = ind_median[c]
        # 口径① 横截面 rank
        X_cs = X.rank(axis=1, pct=True)
        # 口径② 历史分位（自身过去 5 年 rolling percentile）
        X_hist = X.rolling(HIST_WINDOW, min_periods=250).apply(
            lambda s: (s.rank(pct=True)).iloc[-1], raw=False
        )
        for label, Xv in [("横截面rank", X_cs), ("历史分位", X_hist)]:
            for w in WINDOWS:
                Y = FWD[w]
                r = compute_ic(Xv, Y)
                results.append({
                    "metric": c, "fractile": label, "window": w, **r,
                })
    df_res = pd.DataFrame(results)
    log("\n" + df_res.to_string(index=False))

    # 写报告
    write_report(df_res)


def compute_ic(X, Y):
    """横截面 rank IC + 按报告期聚类 t 检验（用行业日收益面板自带的日期做聚类近似）"""
    Ar = X.rank(axis=1)
    Br = Y.rank(axis=1)
    Am = Ar.sub(Ar.mean(axis=1), axis=0)
    Bm = Br.sub(Br.mean(axis=1), axis=0)
    num = (Am * Bm).sum(axis=1, min_count=1)
    den = np.sqrt((Am ** 2).sum(axis=1) * (Bm ** 2).sum(axis=1))
    ic = num / den.replace(0, np.nan)
    cnt = (X.notna() & Y.notna()).sum(axis=1)
    ic = ic.where(cnt >= 5)
    ic = ic.dropna()
    if len(ic) < 20:
        return {"ic": np.nan, "t": np.nan, "p": np.nan, "n": len(ic)}
    # 按「季度」聚类（行业估值慢变量，按日聚类会高估显著性；用季度做保守聚类）
    q = ic.index.to_period("Q")
    pm = ic.groupby(q).mean()
    t, p = stats.ttest_1samp(pm.values, 0.0)
    return {"ic": ic.mean(), "t": t, "p": p, "n": len(pm)}


def write_report(df_res):
    lines = []
    lines.append("# 第四步 · 估值腿完整验证报告\n")
    lines.append("> 生成 2026-10-14 · 脚本 `research/prosperity/04_validate_valuation_full.py`\n")
    lines.append("> 背景：03 步只用「PB 横截面分位」就宣布估值腿证伪，过于草率。本步补测全部估值口径。\n")
    lines.append("## 一、口径矩阵\n")
    lines.append("| 估值指标 | 含义 | 数据质量 |\n|---|---|---|\n")
    lines.append("| `pe` / `pe_ttm` | 市盈率（静态/TTM） | 约 71~74% 非空（亏损股缺失）|\n")
    lines.append("| `pb` | 市净率 | 99% 非空（最干净）|\n")
    lines.append("| `ps` / `ps_ttm` | 市销率 | 100% 非空（亏损股也有）|\n")
    lines.append("| `dv_ratio` / `dv_ttm` | 股息率 | 约 67% 非空 |\n")
    lines.append("\n两种分位口径（关键区别，03 步只测了第一种）：\n")
    lines.append("- **横截面 rank**：当日该行业估值在全行业中的排名 → 回答「谁相对便宜」\n")
    lines.append("- **历史分位**：该行业估值相对自身过去 5 年的分位 → 回答「是否处于自身历史低位（修复空间）」\n")
    lines.append("\n检验：横截面 rank IC → 未来 20/60/120 日超额（等权基准），按季度聚类 t 检验。\n")
    lines.append("\n## 二、结果\n\n")
    lines.append(df_res.to_markdown(index=False))
    lines.append("\n\n## 三、结论\n\n")
    # 判定
    sig = df_res[df_res["p"] < 0.05]
    if sig.empty:
        lines.append("**全部口径、全部窗口均无显著前瞻力（p 均 ≥ 0.05）。** 估值腿确认证伪。\n")
    else:
        lines.append(f"**发现 {len(sig)} 个显著结果**（p<0.05），需进一步看方向与稳健性：\n\n")
        lines.append(sig.to_markdown(index=False))
        lines.append("\n")
    lines.append("（脚本在跑通后由人工补充结论解读）\n")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
