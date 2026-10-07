#!/usr/bin/env python3
"""从**外部主题篮子**机械推导候选行业链（review §12.6 的 A 路径）—— 只读数据根。

背景
----
先验名单若由研究员凭记忆撰写，则必然向已知的「7 对幸存名单」倾斜
（主人、研究员、AI 三方已在对话中看过），命中率被高估且高估幅度不可估计。
⇒ 本脚本把「哪些行业构成一条链」的决定权交给**第三方编制的篮子**（ETF 持仓），
研究员不参与逐条挑选 —— **规则写死在 RULES 里，可审计**。

污染处置与抽检结论（2026-10-07，见 chain-clustering-review.md §12）
------------------------------------------------------------------
原始 `industry/etf_holdings/` **全集已污染，不能直接用**：
  ① 32 组共 114/249 只（46%）共享完全相同的 `(持仓数, top1股, top1权重)` 指纹
     ⇒ 同一个错的指数成分被错配给多只 ETF；
  ② 19 只持仓数 > 300（含 2,183 只持仓的"电网设备 ETF"，top1 = 农业银行）；
  ③ `TMT50ETF招商`(159909) 原始篮子 top5 = 茅台/平安/紫金/招行/长江电力
     —— 污染在**原始篮子**，不只是派生的相似度图。

**机械过滤后**（去重复指纹组 + 去持仓 >300）：全集 249 → **130 只可映射**，
名称含明确主题词的 34 只里 **32 只语义一致（94%）**；另 2 只经复核是**正则表达式漏字**
（环保 ETF 持光伏/电力 = 中证环保口径；钢铁 ETF 第一大行业"普钢"属钢铁子类），
不是数据错 ⇒ **过滤后的子集可作外部先验源，但过滤 spec 必须与名单一起锁定**。

⚠️ 诚实标注（写进文件头与 spec ⑦）：
清单本身是**程序推导**（无逐条挑选），但**过滤规则是在看过幸存名单之后写的**
⇒ 按 §12.6 只能作 **① 对象清单（定义"要测量哪些群"）** 与
**② 方法能否复现已知篮子的佐证**；⛔ **不得用于命中率 / 漏检率检验**。

用法
----
    python3 tools/build_chain_prior_from_etf.py --probe        # 只打印过滤与一致性抽检
    python3 tools/build_chain_prior_from_etf.py \
            --out docs/plan/chain-prior-list.csv               # 生成名单（含来源头）
    python3 tools/lock_chain_prior.py --list docs/plan/chain-prior-list.csv \
            --source "..." --author "..." --lock
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.paths import INDUSTRY_DIR  # noqa: E402

ETF_DIR = INDUSTRY_DIR / "etf_holdings"
DET_PATH = ETF_DIR / "etf_holdings_detail.parquet"
SUM_PATH = ETF_DIR / "etf_holdings_summary.parquet"
MEM_PATH = INDUSTRY_DIR / "industry_members.parquet"

# ── 过滤与推导规则（写死、可审计；改这里必须同步 spec ⑦ 并重跑锁定）──
RULES = {
    "drop_duplicate_fingerprint": True,   # (持仓数, top1股, top1权重) 指纹重复 ⇒ 整组剔除
    "max_holdings": 300,                  # 持仓数 > 此值 ⇒ 剔除（疑似捞错指数 / 全市场污染）
    "min_member_weight": 5.0,             # 篮子权重 ≥ 此值(%) 的申万二级才成为链成员
    "min_members": 2,                     # 少于该数量不构成链（单行业 ETF 是行业不是链）
    "max_members": 8,                     # 超过则只取权重最大的前 N 个（防群过大稀释统计量）
    "min_industry_coverage": 60.0,        # 可映射到申万二级的持仓权重占比下限(%)
}

# 语义抽检用（只用于**报告**，不用于过滤 —— 过滤规则必须机械）
KW = {
    "半导体": "半导体|集成电路|光学光电子|元件|计算机设备|消费电子",
    "证券": "证券", "银行": "银行", "军工": "航天装备|航空装备|军工电子|兵器兵装",
    "光伏": "光伏设备", "医药": "医疗|医药|生物|制药", "酒": "白酒|非白酒",
    "汽车": "汽车|乘用车|商用车|汽车零部件", "计算机": "计算机|软件开发|IT服务",
    "家电": "家电|白色家电|小家电|厨卫", "钢铁": "钢铁|普钢|特钢", "煤炭": "煤炭",
    "有色": "工业金属|贵金属|小金属|能源金属", "农业": "养殖|饲料|种植|农牧|农产品",
    "通信": "通信设备|通信服务", "传媒": "游戏|广告营销|影视院线|出版|数字媒体",
    "运输": "航运港口|铁路公路|物流|航空机场", "环保": "环保设备|环境治理|光伏设备|电力",
    "电力": "电力|火力发电|水力发电|核电", "旅游": "旅游|酒店|景区|航空机场",
}


# ─────────────────────────── 读取与映射 ───────────────────────────
def _stock_to_industry() -> pd.Series:
    """``000016`` 形式的 6 位代码 → 申万二级行业名"""
    mem = pd.read_parquet(MEM_PATH)
    mem["code6"] = mem["stock_code"].astype(str).str.split(".").str[-1]
    return mem.drop_duplicates("code6").set_index("code6")["ind_name"]


def clean_subset() -> tuple[pd.DataFrame, dict]:
    """机械过滤后的干净篮子子集（不触碰明细，先筛 summary 再 join）"""
    s = pd.read_parquet(SUM_PATH)
    stats = {"total_etf": len(s)}
    s["fp"] = (s["num_holdings"].astype(str) + "|" + s["top1_stock"].astype(str)
               + "|" + s["top1_weight"].round(3).astype(str))
    grp = s.groupby("fp").size()
    dup_fp = set(grp[grp > 1].index)
    n_dup = int(s["fp"].isin(dup_fp).sum())
    n_big = int((s["num_holdings"] > RULES["max_holdings"]).sum())
    keep = s[~s["fp"].isin(dup_fp) & (s["num_holdings"] <= RULES["max_holdings"])].copy()
    stats.update({"dropped_dup_fingerprint": n_dup, "dropped_holdings_gt_300": n_big,
                  "dup_fp_groups": len(dup_fp), "kept_etf": len(keep)})
    return keep, stats


def _industry_weights(keep: pd.DataFrame) -> dict[str, pd.Series]:
    """每个 ETF → 「申万二级 → 篮子权重(%)」"""
    s2i = _stock_to_industry()
    det = pd.read_parquet(DET_PATH)
    det = det[det["etf_name"].isin(set(keep["etf_name"]))].copy()
    det["code6"] = (det["stock_code"].astype(str)
                    .str.replace(r"^(sh|sz)\.", "", regex=True).str.zfill(6))
    det["ind"] = det["code6"].map(s2i)
    det = det.dropna(subset=["ind"])
    out = {}
    for name, g in det.groupby("etf_name"):
        out[name] = g.groupby("ind")["weight"].sum().sort_values(ascending=False)
    return out


def build_chains(keep: pd.DataFrame) -> tuple[list[dict], dict]:
    """把每个干净篮子变成一条候选链（成员 = 权重达标截止到 max_members 的申万二级）"""
    iw = _industry_weights(keep)
    code_of = _industry_code_map()
    rows, seen, dropped = [], {}, {}
    for name, w in iw.items():
        cover = float(w.sum())
        if cover < RULES["min_industry_coverage"]:
            dropped[name] = f"可映射权重仅 {cover:.0f}%"
            continue
        members = w[w >= RULES["min_member_weight"]]
        if len(members) < RULES["min_members"]:
            dropped[name] = f"达标成员 {len(members)} < {RULES['min_members']}"
            continue
        if len(members) > RULES["max_members"]:
            members = members.iloc[: RULES["max_members"]]
        key = tuple(sorted(members.index))
        # 同���员集去重：保留第一大行业权重最高的那只 ETF（篮子更集中）
        if key in seen and members.iloc[0] <= seen[key][1]:
            continue
        if key in seen:
            rows = [r for r in rows if r["chain_id"] != seen[key][0]]
        seen[key] = (name, members.iloc[0])
        for ind, wt in members.items():
            rows.append({
                "chain_id": name,
                "chain_name": name,
                "ind_code": code_of.get(ind, ""),
                "ind_name": ind,
                "source": "ETF 持仓篮子（第三方发行人/指数公司编制）",
                "note": f"篮子权重 {wt:.1f}%",
            })
    return rows, dropped


def _industry_code_map() -> dict[str, str]:
    mem = pd.read_parquet(MEM_PATH)
    mem = mem.drop_duplicates("ind_name")
    return dict(zip(mem["ind_name"], mem["ind_code"]))


def semantic_probe(rows: list[dict]) -> tuple[int, int, list[str]]:
    """抽��：名称含主题关键词的链，其**主要成员**是否语义一致（只报告，不过滤）"""
    chains: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        wt = float(str(r["note"]).replace("篮子权重 ", "").replace("%", ""))
        chains.setdefault(r["chain_id"], []).append((r["ind_name"], wt))

    hits = bad = 0
    bad_list: list[str] = []
    for cid, members in chains.items():
        kw = next((k for k in KW if k in cid), "")
        if not kw:
            continue
        members.sort(key=lambda x: -x[1])
        top2 = " / ".join(m[0] for m in members[:2])
        okv = bool(re.search(KW[kw], members[0][0])) or (
            len(members) > 1 and bool(re.search(KW[kw], members[1][0])))
        if okv:
            hits += 1
        else:
            bad += 1
            bad_list.append(f"{cid}: top2={top2}")
    return hits, bad, bad_list


def probe_report() -> None:
    keep, stats = clean_subset()
    rows, dropped = build_chains(keep)
    hits, bad, bad_list = semantic_probe(rows)
    print("== 外部先验源抽检 · industry/etf_holdings/ ==")
    print(f"  全集 ETF            {stats['total_etf']}")
    print(f"  重复指纹组/命中只数  {stats['dup_fp_groups']} 组 / {stats['dropped_dup_fingerprint']} 只 ⇒ 剔除")
    print(f"  持仓 > {RULES['max_holdings']}            {stats['dropped_holdings_gt_300']} 只 ⇒ 剔除")
    print(f"  干净子集            {stats['kept_etf']} 只")
    print(f"  推导出的候选链      {len(set(r['chain_id'] for r in rows))} 条"
          f"（被规则挡下 {len(dropped)} 只：覆盖率不足或达标成员太少）")
    print(f"  主题词语义一致率    {hits}/{hits + bad} = "
          f"{hits / max(hits + bad, 1) * 100:.0f}%")
    for line in bad_list:
        print(f"      ⚠️ {line}")


def write_list(rows: list[dict], out: Path, src_hash: str) -> None:
    header = (
        "# 候选行业链 · 外部先验（程序推导，非人工挑选）\n"
        f"# 生成脚本 : tools/build_chain_prior_from_etf.py\n"
        f"# 生成日期 : {datetime.now().strftime('%Y-%m-%d')}\n"
        "# 来源     : industry/etf_holdings/ （ETF 持仓篮子，发行方/指数公司编制，第三方）\n"
        f"# 源文件sha256: {src_hash}\n"
        "# 过滤规则 : " + "; ".join(f"{k}={v}" for k, v in RULES.items()) + "\n"
        "# ⚠️ post-hoc: 清单由程序推导（无逐条挑选），但**过滤规则是在研究员看过幸存名单之后写的**\n"
        "#    ⇒ 按 review §12.6：只能作①对象清单（定义要测量哪些群）②方法能否复现已知篮子的佐证；\n"
        "#      ⛔ 不得用于命中率 / 漏检率检验。\n"
    )
    if not rows:
        raise SystemExit("没有任何候选链被推导出来 —— 检查过滤规则与数据源")
    df = pd.DataFrame(rows)[["chain_id", "chain_name", "ind_code", "ind_name",
                             "source", "note"]]
    df = df.sort_values(["chain_id", "ind_code"])
    with out.open("w", encoding="utf-8") as f:
        f.write(header)
        df.to_csv(f, index=False)
    print(f"✅ 已写入 {out}：{df['chain_id'].nunique()} 条链 / {len(df)} 行成员")
    print("   下一步：python3 tools/lock_chain_prior.py --list "
          f"{out} --source '...' --author '...' --lock")


def main() -> int:
    ap = argparse.ArgumentParser(description="从外部主题篮子机械推导候选行业链")
    ap.add_argument("--out", default=None, help="输出名单 CSV（不给出则只打印抽检）")
    ap.add_argument("--probe", action="store_true", help="只打印抽检结论")
    a = ap.parse_args()

    keep, _ = clean_subset()
    rows, _ = build_chains(keep)

    if a.out:
        h = hashlib.sha256()
        for p in (SUM_PATH, DET_PATH):
            h.update(p.read_bytes())
        write_list(rows, Path(a.out), h.hexdigest())
        return 0
    probe_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
