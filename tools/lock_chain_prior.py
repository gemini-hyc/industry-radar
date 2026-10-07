"""lock_chain_prior.py —— §11.4 先验名单的**锁定机制**（不是名单本身）。

为什么要有它
------------
review §11.4 指出的陷阱：**已看过幸存名单的人，凭记忆写"事前名单"必然被污染，
命中率必被高估。** 修法是把项目已有的 provenance 纪律用在「先验」自身上：
**名单必须在跑数据之前落盘并锁定**（来源 + 锁定日期 + sha256 进 spec block）。

用法
----
    # 1) 主人/研究员撰写名单（模板见 docs/plan/chain-prior-list.template.csv），**不看数据**
    # 2) 锁定（首次）：
    python tools/lock_chain_prior.py --list docs/plan/chain-prior-list.csv \
        --source "申万语义 + 主人业务判断" --author "主人" --lock
    # 3) 之后每次跑验证管道前**必须先校验**（不 --lock 即为校验模式）：
    python tools/lock_chain_prior.py --list docs/plan/chain-prior-list.csv
    #   哈希不符 ⇒ 退出码 1 ⇒ 说明名单被改过（或从未锁定）

⛔ 纪律：锁定后**不得**再改名单去"对齐"数据；若必须改，应新建 v2 文件并记录理由，
   使 v1 的验证结果仍可追溯。

零依赖（标准库 hashlib / json / csv）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "docs" / "plan" / "chain-prior.lock.json"
SPEC_TXT = "docs/plan/chain-clustering-spec.md 第 ⑦ 项"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _read_rows(path: Path) -> list[dict]:
    """读名单 CSV，**跳过 `#` 注释行**。

    2026-10-07：程序推导的名单（`tools/build_chain_prior_from_etf.py`）会在顶部写
    生成脚本/日期/过滤规则等 `#` 注释行（project 的 provenance 风格）。注释行对机器不可见、
    对人可见 —— 这样做保留了可追溯性，代价是本函数必须显式跳过它们。
    """
    import io
    text = path.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    return list(csv.DictReader(io.StringIO(body)))


def header_meta(path: Path) -> dict:
    """提取名单头部的 `# key: value` 元信息（源文件哈希等），供锁定记录附带。"""
    meta = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.startswith("#"):
            break
        if ":" in ln:
            k, v = ln[1:].split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def summarize(path: Path) -> dict:
    rows = _read_rows(path)
    if not rows:
        raise SystemExit(f"名单为空：{path}（至少要有表头之外的一行）")
    req = {"chain_id", "chain_name", "ind_code"}
    missing = req - set(rows[0].keys())
    if missing:
        raise SystemExit(f"名单缺列 {sorted(missing)}；见模板 "
                         "docs/plan/chain-prior-list.template.csv")
    chains = {}
    for r in rows:
        chains.setdefault(r["chain_id"], []).append(r["ind_code"])
    return {"n_chains": len(chains), "n_members": len(rows),
            "chains": {k: sorted(v) for k, v in chains.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description="先验名单锁定 / 校验")
    ap.add_argument("--list", required=True, help="先验名单 CSV 路径")
    ap.add_argument("--source", default="", help="来源说明（锁定模式必填）")
    ap.add_argument("--author", default="", help="撰写者（锁定模式必填）")
    ap.add_argument("--lock", action="store_true", help="写入锁定记录（默认仅校验）")
    ap.add_argument("--force", action="store_true", help="允许覆盖既有锁定（须有理由）")
    a = ap.parse_args()

    p = Path(a.list)
    if not p.exists():
        print(f"❌ 名单不存在：{p}")
        return 1
    digest = sha256_of(p)
    info = summarize(p)
    print(f"名单 {p}｜链 {info['n_chains']} 条 / 成员 {info['n_members']} 行")
    print(f"sha256 = {digest}")

    if a.lock:
        if not (a.source and a.author):
            print("❌ 锁定模式必须给 --source 与 --author（spec ⑦ 要求「来源」可追溯）")
            return 1
        if LOCK.exists() and not a.force:
            print(f"❌ 已存在锁定记录 {LOCK}；改动名单请新建 v2 文件并锁定它，"
                  f"或用 --force 覆盖（须在 commit message 写明理由）")
            return 1
        try:                       # 路径在仓库内 ⇒ 记相对路径；否则记绝对路径
            list_file = str(p.resolve().relative_to(ROOT))
        except ValueError:
            list_file = str(p.resolve())
        rec = {"list_file": list_file, "sha256": digest,
               "source": a.source, "author": a.author,
               "locked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "n_chains": info["n_chains"], "n_members": info["n_members"]}
        hm = header_meta(p)
        if hm.get("源文件sha256"):          # 清单自带的**数据源**哈希 ≠ 名单文件哈希
            rec["source_sha256"] = hm["源文件sha256"]
        if hm.get("过滤规则"):
            rec["derivation_rules"] = hm["过滤规则"]
        if hm.get("⚠️ post-hoc") or hm.get("post-hoc"):
            rec["post_hoc"] = True
        LOCK.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print(f"✅ 已锁定 → {LOCK}")
        print(f"   请把「来源 {a.source} / 日期 {rec['locked_at'][:10]} / sha256 "
              f"{digest[:12]}…」写进 {SPEC_TXT}")
        return 0

    if not LOCK.exists():
        print(f"❌ 尚未锁定（{LOCK} 不存在）⇒ **不得跑群级验证**（review §11.4 铁律）")
        return 1
    rec = json.loads(LOCK.read_text(encoding="utf-8"))
    if rec["sha256"] != digest:
        print(f"❌ 哈希不符：锁定记录 {rec['sha256'][:12]}… ≠ 当前 {digest[:12]}…")
        print("   ⇒ 名单自锁定后已被修改。改动须新建 v2 并重新锁定，不得原地改。")
        return 1
    print(f"✅ 校验通过：与 {rec['locked_at'][:10]} 锁定的版本一致 "
          f"（来源 {rec['source']}，作者 {rec['author']}）")
    print(f"   可以跑群级验证；验证脚本须把 spec ⑦ 的哈希记录进自己的 meta。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
