#!/usr/bin/env python3
"""构建**风格因子面板**一等产物 —— review §12.2「复现三件套」的第 ① 件。

产出（均已登记 src/paths.py:ARTIFACTS，落盘走 src/utils/provenance）：
    industry_style_panel.parquet    特征 X_{i,k,t}（长表：date × ind_code × 因子）
    industry_style_factors.parquet  因子收益 f_{k,t}（宽表：date × 因子）

meta 里会写入 **spec 全套**（因子集 / 估计窗口 / **标准化作用域** / 是否带截距 / 探针阈值），
这两件 + 残余面板 r̃三者齐备，"调整值可派生"才可复现。

用法:
    python3 scripts/build_style_panel.py                 # 全量重算并落盘
    python3 scripts/build_style_panel.py --dry-run       # 只算不写（体检/探针自测用）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.paths import INDUSTRY_DIR  # noqa: E402
from src.resonance import config as C  # noqa: E402
from src.resonance import panel as P  # noqa: E402
from src.utils.provenance import save_dataframe  # noqa: E402

STYLE_PANEL_PATH = INDUSTRY_DIR / "industry_style_panel.parquet"
STYLE_FACTOR_PATH = INDUSTRY_DIR / "industry_style_factors.parquet"


def main() -> int:
    ap = argparse.ArgumentParser(description="构建风格因子面板（一等产物）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    a = ap.parse_args()

    print("· 读取收益面板 / 聚合行业特征 …")
    chars, F, R = P.build_all()
    print(f"  收益面板 {R.shape[0]} 日 × {R.shape[1]} 行业")
    for k, df in chars.items():
        miss = float(df.isna().to_numpy().mean())
        print(f"  特征 {k:<6s} 缺失率 {miss:6.2%}  起 {str(df.index.min().date())}")

    print("\n· 因子收益（每日横截面多空组合，权重滞后一日）：")
    for k in F.columns:
        s = F[k].dropna()
        if len(s) == 0:
            print(f"  {k:<6s} ⚠️ 全空")
            continue
        print(f"  {k:<6s} n={len(s):5d}  年化收益 {s.mean()*252:7.2%}  日波动 {s.std():.4f}")

    if a.dry_run:
        print("\n--dry-run：未写入任何文件")
        return 0

    long = P.characteristics_to_long(chars)
    p1 = save_dataframe(long, STYLE_PANEL_PATH, producer="scripts/build_style_panel.py",
                        params=C.spec_dict(), extra={"artifact_role": "style_panel_X"})
    p2 = save_dataframe(F.reset_index(), STYLE_FACTOR_PATH,
                        producer="scripts/build_style_panel.py",
                        params=C.spec_dict(), extra={"artifact_role": "style_panel_f"})
    print(f"\n✅ 已写入：\n  {p1}\n  {p2}")
    print("   meta 中的 params 即为复现 spec（因子集 / 窗口 / 标准化作用域 / 截距 / 探针阈值）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
