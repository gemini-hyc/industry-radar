#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
# tools/dev_verify.sh —— 同源 A/B 验证基座（**开发工具，不参与生产链路**）
#
# 背景：改一个脚本后，怎么证明「没改坏」？
#   本仓库的数据根在本机是**只读副本**，且真实副本落后于源端未执行的步骤
#   ⇒ **绝不能**拿"沙箱产物"去比"真实数据根产物"（会产生假回归）。
#   唯一可信的做法 = **同源 A/B**：
#     基线代码（git archive HEAD）  vs  当前工作树
#     跑在**同一份**沙箱数据上，逐产物比对。
#
# 本脚本把这条流程固化成一条命令（原先每次手工 8~10 步）。
#
# 用法：
#   tools/dev_verify.sh scripts/build_industry_daily_full.py --days 60
#   tools/dev_verify.sh scripts/mainline_detector.py
#   tools/dev_verify.sh scripts/industry_tracker.py --start 2026-06-01 --end 2026-09-30
#   tools/dev_verify.sh --clean            # 清理沙箱（重新建）
#   tools/dev_verify.sh --where            # 显示沙箱路径与占用
#
# 环境变量：
#   IR_VERIFY_PY       Python 解释器（默认与生产日更链同：/usr/bin/python3；
#                      留空则在该解释器缺 pandas 时回落到 PATH 里的 python3）
#   IR_VERIFY_DATA_SRC 数据来源（默认 ~/quant-data）
#   IR_VERIFY_ROOT     工作区根（默认 /tmp/ir-verify）
#   IR_VERIFY_KEEP     非空则不删本次产物（默认保留，便于人工复核）
#
# 三条纪律（脚本自动执行，不需要你记得）：
#   ① 结束时校验真实数据根「零写入」（-newer marker 守卫）
#   ② 沙箱用 APFS clonefile（cp -c）⇒ 秒级、几乎不占额外空间
#   ③ 基线 = git archive HEAD ⇒ **未提交的新文件不在基线里**，跨文件改动请先 commit
# ══════════════════════════════════════════════════════════════════════════
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 解释器：与生产日更链同源（run_daily_scheduled.sh 写死 /usr/bin/python3）。
# 该解释器若缺 pandas，回落到 PATH 里的 python3；都不合格则由下面的 -x 检查报错。
if [ -n "${IR_VERIFY_PY:-}" ]; then
  PY="$IR_VERIFY_PY"
elif /usr/bin/python3 -c 'import pandas, pyarrow' >/dev/null 2>&1; then
  PY="/usr/bin/python3"
else
  PY="$(command -v python3 || echo /usr/bin/python3)"
fi
DATA_SRC="${IR_VERIFY_DATA_SRC:-$HOME/quant-data}"
ROOT="${IR_VERIFY_ROOT:-/tmp/ir-verify}"
SB_DATA="$ROOT/data"
SB_OUT="$ROOT/out"
SB_REPO="$ROOT/base-repo"

die() { echo "❌ $*" >&2; exit 1; }
hr()  { printf '─%.0s' {1..72}; echo; }

case "${1:-}" in
  --clean)
    [[ -d "$ROOT" ]] && du -sh "$ROOT" 2>/dev/null
    rm -rf "$ROOT" && echo "✅ 已清理 ${ROOT}（下次运行会重建，约 30 秒）"
    exit 0 ;;
  --where)
    echo "工作区:   $ROOT"
    echo "沙箱数据: $SB_DATA"
    echo "产物输出: $SB_OUT"
    [[ -d "$ROOT" ]] && du -sh "$ROOT"/* 2>/dev/null
    exit 0 ;;
  ""|-h|--help)
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0 ;;
esac

SCRIPT="$1"; shift
SCRIPT_ARGS=("$@")

[[ -f "$REPO/$SCRIPT" ]] || die "仓库内找不到脚本: $SCRIPT"
[[ -x "$PY" ]] || die "解释器不可执行: $PY（用 IR_VERIFY_PY 指定）"

echo "═══ 同源 A/B 验证 ═══"
echo "仓库:   $REPO  @ $(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet HEAD 2>/dev/null && echo '' || echo ' +工作树改动')"
echo "脚本:   $SCRIPT ${SCRIPT_ARGS[*]:-（无参数）}"
echo "解释器: $PY"

# ── 0. 守卫 marker ────────────────────────────────────────────────
GUARD="$(mktemp /tmp/ir-verify-guard.XXXXXX)"
trap 'rm -f "$GUARD"' EXIT

# ── 1. 沙箱（clonefile，可复用）───────────────────────────────────
hr
if [[ ! -d "$SB_DATA" ]]; then
  echo "▸ 建沙箱: ${DATA_SRC} → ${SB_DATA}（APFS clonefile）"
  mkdir -p "$ROOT"
  cp -c -R "$DATA_SRC" "$SB_DATA" 2>/dev/null \
    || { echo "  （clonefile 不可用，退回普通拷贝，较慢）"; cp -R "$DATA_SRC" "$SB_DATA"; } \
    || die "建沙箱失败"
  echo "  完成，占用 $(du -sh "$SB_DATA" 2>/dev/null | cut -f1)"
else
  echo "▸ 复用沙箱: ${SB_DATA}（$(du -sh "$SB_DATA" 2>/dev/null | cut -f1)）"
fi

# ── 2. 基线代码 = git archive HEAD ────────────────────────────────
hr
rm -rf "$SB_REPO"; mkdir -p "$SB_REPO"
git -C "$REPO" archive HEAD | tar -x -C "$SB_REPO" || die "导出基线代码失败"
echo "▸ 基线代码已导出到 $SB_REPO"

# 提示：未提交的新文件不会进基线
NEW_FILES="$(git -C "$REPO" status --porcelain | awk '$1=="??"{print $2}' | grep -E "\.(py|yaml)$" || true)"
if [[ -n "$NEW_FILES" ]]; then
  echo "  ⚠️ 有未跟踪的代码文件，它们不在基线里，若被基线路径引用会报 ModuleNotFoundError："
  echo "$NEW_FILES" | sed 's/^/     /'
fi

# ── 3. 双跑 ──────────────────────────────────────────────────────
run_side() {
  # ⚠️ macOS 自带 bash 3.2：local 复合赋值 `local a=x b=$a` 的展开顺序不可靠
  #    （实测报 "b: unbound variable"），一律拆成逐行声明。
  local tag="$1"
  local repo="$2"
  local out="${SB_OUT}/${tag}"
  rm -rf "$out"; mkdir -p "$out"
  echo "─── [${tag}] ${repo} ───"
  ( cd "$repo" \
    && INDUSTRY_RADAR_DATA="$SB_DATA" \
       INDUSTRY_RADAR_READONLY=1 \
       INDUSTRY_RADAR_OUTPUT_DIR="$out" \
       "$PY" "$repo/$SCRIPT" ${SCRIPT_ARGS[@]+"${SCRIPT_ARGS[@]}"} ) 2>&1 | tail -25
  local rc=${PIPESTATUS[0]}
  echo "    [${tag}] exit=${rc}"
  return 0
}
hr
run_side base "$SB_REPO"
hr
run_side new  "$REPO"

# ── 4. 逐产物比对 ────────────────────────────────────────────────
hr
echo "▸ 产物比对"
"$PY" - "$SB_OUT/base" "$SB_OUT/new" <<'PYEOF'
import sys, hashlib, pathlib
import pandas as pd, numpy as np

base, new = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
def relset(d): return {p.relative_to(d).as_posix(): p for p in d.rglob("*") if p.is_file()}
B, N = relset(base), relset(new)

if not B and not N:
    print("  ⚠️ 两边都没产出任何文件 —— 脚本可能把结果写进了「数据根之外」")
    print("     （写保护只重定向**数据根内**的路径；仓库内/绝对路径不受影响）")
    sys.exit(0)

only_b, only_n = sorted(set(B) - set(N)), sorted(set(N) - set(B))
for f in only_b: print(f"  ❌ 仅基线产出: {f}")
for f in only_n: print(f"  ❌ 仅改动后产出: {f}")

def h(path): return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

ok = diff = 0
for r in sorted(set(B) & set(N)):
    pb, pn = B[r], N[r]
    if pb.name.endswith(".meta.json"):
        # 产物指纹：以下字段随「运行环境」而非「代码」变化，不计入差异 ——
        #   produced_at 时间戳 ／ written_to 输出路径 ／ target 目标相对路径 ／ hostname 主机
        #   git 基线跑在 git archive 导出的无 .git 目录里（commit=null），与工作树必然不同
        # 其余字段（schema/producer/rows/columns/columns_hash/date_range/params/inputs）必须一致。
        # ⚠️ 用**精确字段名**而非关键词匹配：`date_range` 含 "date"，关键词会误伤业务字段。
        import json as _json
        SKIP = {"produced_at", "written_to", "written_at", "target", "hostname",
                "git", "host", "user", "timestamp", "elapsed_sec"}
        try:
            jb, jn = _json.loads(pb.read_text()), _json.loads(pn.read_text())
        except Exception as e:
            print(f"  ⚠️ {r}: 指纹解析失败 {e}"); diff += 1; continue
        fb = {k: v for k, v in jb.items() if k not in SKIP}
        fn = {k: v for k, v in jn.items() if k not in SKIP}
        if fb == fn:
            print(f"  ✅ {r}: 指纹一致（已忽略 {len(jb) - len(fb)} 个环境字段）"); ok += 1
        else:
            dk = sorted(k for k in set(fb) | set(fn) if fb.get(k) != fn.get(k))
            print(f"  ⚠️ {r}: 指纹业务字段不同 → {dk}"); diff += 1
    elif pb.suffix == ".parquet":
        try:
            db, dn = pd.read_parquet(pb), pd.read_parquet(pn)
        except Exception as e:
            print(f"  ⚠️ {r}: 读取失败 {e}"); diff += 1; continue
        if list(db.columns) != list(dn.columns):
            print(f"  ❌ {r}: 列不同 base={list(db.columns)} new={list(dn.columns)}"); diff += 1; continue
        if len(db) != len(dn):
            print(f"  ❌ {r}: 行数 {len(db)} → {len(dn)}"); diff += 1; continue
        changes = []
        for c in db.columns:
            a, b = db[c], dn[c]
            if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b):
                d = (a.astype(float) - b.astype(float)).abs()
                m = float(np.nanmax(d.values)) if len(d) and not np.all(np.isnan(d.values)) else 0.0
                if m > 0:
                    rel = m / max(abs(a.astype(float)).max(), 1e-12)
                    changes.append(f"{c}(max_abs={m:.3g}, rel={rel:.1e})")
            else:
                nb = int((a.astype(str) != b.astype(str)).sum())
                if nb: changes.append(f"{c}({nb}行不同)")
        if changes:
            print(f"  ⚠️ {r}: {len(db)} 行，差异列 → {', '.join(changes)}"); diff += 1
        else:
            print(f"  ✅ {r}: {len(db)} 行逐行逐列一致"); ok += 1
    else:
        if h(pb) == h(pn): print(f"  ✅ {r}: 字节一致（{pb.stat().st_size} B）"); ok += 1
        else:              print(f"  ⚠️ {r}: 内容不同（{pb.stat().st_size} → {pn.stat().st_size} B）"); diff += 1

print("─" * 72)
if diff == 0 and not only_b and not only_n:
    print("✅ 零回归 —— 改动前后产物完全一致")
else:
    print(f"⚠️ 存在差异：{diff} 个产物不同" + (f"，{len(only_b)+len(only_n)} 个单边产出" if (only_b or only_n) else ""))
    print("   （若是浮点尾数差异 1e-15 量级 ⇒ 通常为数值实现差异，属逻辑零回归）")
PYEOF

# ── 5. 真实数据根守卫 ────────────────────────────────────────────
hr
LEAK="$(find "$DATA_SRC" -type f -newer "$GUARD" 2>/dev/null | head -20)"
if [[ -z "$LEAK" ]]; then
  echo "✅ 真实数据根零写入（${DATA_SRC}）"
else
  echo "❌ 真实数据根被写入！以下文件晚于本次运行："
  echo "$LEAK" | sed 's/^/     /'
fi
echo
echo "产物留存：$SB_OUT/{base,new}   （清理：tools/dev_verify.sh --clean）"
