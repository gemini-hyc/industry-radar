"""chain_three_class_qtest.py —— §11.2 检验版三分类的重算脚本（只读，零写入数据根）。

对应 `docs/plan/chain-clustering-review.md` §11.2（第三轮裁决）：
用 Cochran Q 同质性检验（Fisher-z，df=2 闭式）替代旧"三段 ≥0.30 + 极差 ≤0.13"拍系数版，
对 §2.2 实测四的 217 个选中对重算 结构/新生/衰减/闪烁 分类。

**锁定 spec（review §7/§11.5，两套实现对拍必须逐对一致）**：
① 市场代理 = 等权 131 行业均值；
② 选对 = 全样本剥一次 beta，全样本 |残差 r| ≥ 0.40；
③ 分段 = 2020-21 / 2022-23 / 2024-26，**每段各自重剥 beta**（⇒ 167 口径）；
④ 残差不另做标准化（Pearson 相关尺度不变；标准化作用域仅影响 SVD/PC 诊断）；
⑤ 缺数据行业 = 面板 pivot 后天然对齐，未另行剔除（131 全齐）。

**报告方式纪律**：一律写「实测 X 对 vs 噪声期望 Y 对」，不写「X 对稳定」。
旧计数 66/77/84+21（极差版）**作废**，仅作交叉验证输出。

跑法：/Users/hyc/Quant/stock-doctor/.venv/bin/python research/chain_three_class_qtest.py
输出：stdout 摘要 + ~/quant-data/_local_dev/chain_three_class_qtest_<日期>.csv
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/hyc/Quant/industry-radar")
from src.evaluation.inference import classify_three_segment  # noqa: E402

WR = "/Users/hyc/quant-data/industry/industry_weighted_returns.parquet"
MEM = "/Users/hyc/quant-data/industry/index_member_all_raw.parquet"
T2L = "/Users/hyc/quant-data/industry/ind_code_to_sw2021.tsv"
THETA_SELECT = 0.40
SEGS = [("2020-01-01", "2021-12-31"),
        ("2022-01-01", "2023-12-31"),
        ("2024-01-01", "2026-09-30")]


def strip_beta(X: np.ndarray) -> np.ndarray:
    """等权市场代理 + 截距 OLS 剥离（与 review 附录 A 逐字同口径）。"""
    mkt = X.mean(axis=1)
    A = np.column_stack([np.ones(len(mkt)), mkt])
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        return X - A @ np.linalg.lstsq(A, X, rcond=None)[0]


def main() -> None:
    wr = pd.read_parquet(WR)
    R = (wr.pivot(index="date", columns="ind_code", values="weighted_pct")
           / 100.0).sort_index()
    codes = R.columns.tolist()
    assert len(codes) == 131, f"行业数 {len(codes)} != 131"
    assert R.notna().all().all(), "面板含 NaN，与锁定 spec ⑤ 不符"

    # ① 全样本剥一次 → 选对
    C_full = np.corrcoef(strip_beta(R.values).T)
    sel = [(i, j, C_full[i, j])
           for i in range(131) for j in range(i + 1, 131)
           if abs(C_full[i, j]) >= THETA_SELECT]
    print(f"[spec ②] 全样本 |r|>={THETA_SELECT} 选中对数 = {len(sel)}  (文档锚点 217)")

    # ② 三段各自重剥 beta
    seg_corr, seg_ns = [], []
    for a, b in SEGS:
        m = ((R.index >= a) & (R.index <= b))
        seg_corr.append(np.corrcoef(strip_beta(R.values[m]).T))
        seg_ns.append(int(m.sum()))
    print(f"[spec ③] 段交易日 = {seg_ns}  (文档锚点 486/484/666)")

    rows = []
    for i, j, r0 in sel:
        rs = [float(seg_corr[k][i, j]) for k in range(3)]
        cls = classify_three_segment(rs, seg_ns)
        rows.append({
            "ind1": codes[i], "ind2": codes[j], "r_full": round(r0, 4),
            "r_s1": round(rs[0], 4), "r_s2": round(rs[1], 4), "r_s3": round(rs[2], 4),
            "Q": round(cls["Q"], 3), "p_hom": round(cls["p_survival"], 5),
            "klass": cls["class"],
            "rng": round(max(rs) - min(rs), 4),
            "stable_old": bool(min(abs(x) for x in rs) >= 0.30),
        })
    df = pd.DataFrame(rows)

    # ③ 检验版三分类
    n = len(df)
    print("\n== §11.2 检验版三分类（Q 检验，p>0.05 同质） ==")
    for k in ["structure", "emerging", "decaying", "flicker"]:
        c = int((df["klass"] == k).sum())
        print(f"  {k:10s} 实测 {c:3d} 对" +
              (f"  vs 噪声期望 {0.05 * n:.0f} 对（超额 {c - 0.05 * n:+.0f}）"
               if k == "structure" else ""))
    print(f"  合计 {n}（选中对）")

    # ④ 旧规则交叉验证（仅验证 review §11.2 的两个数字，不作结论）
    n_old_stable = int(df["stable_old"].sum())
    n_old_range = int((df["rng"] <= 0.13).sum())
    het_in_range = int(((df["rng"] <= 0.13) & (df["klass"] != "structure")).sum())
    print(f"\n== 交叉验证（旧口径，仅核对 review 锚点） ==")
    print(f"  旧判稳（三段 |r|>=0.30）= {n_old_stable}   (文档锚点 167)")
    print(f"  旧极差 <=0.13          = {n_old_range} 对，其中 Q 判不同质 {het_in_range} 对"
          f"   (文档锚点 67 / 22)")

    # ⑤ L1 内外分解（结构类）
    mem = pd.read_parquet(MEM)
    mem = mem[mem["is_new"] == "Y"]
    l2map = mem.drop_duplicates("index_code").set_index("index_code")["l1_name"]
    t2l = pd.read_csv(T2L, sep="\t").set_index("ind_code")["sw_l2_code"]
    l1_of = {c: l2map.get(t2l.get(c), "?") for c in codes}
    df["same_l1"] = [l1_of[r.ind1] == l1_of[r.ind2] for r in df.itertuples()]
    st = df[df["klass"] == "structure"]
    print(f"\n== 结构类 {len(st)} 对的 L1 分解 ==")
    print(f"  同 L1 {int(st['same_l1'].sum())} / 跨 L1 {int((~st['same_l1']).sum())}"
          f"   （旧判稳口径为 89/78——见 review §7；检验版口径此表为首次产出）")

    out = "/Users/hyc/quant-data/_local_dev/chain_three_class_qtest_2026-10-06.csv"
    df.to_csv(out, index=False)
    print(f"\n逐对结果已写 {out}")


if __name__ == "__main__":
    main()
