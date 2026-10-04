#!/usr/bin/env python3
"""零副作用语法检查 —— 逐个解析 .py 的 AST，**不写 __pycache__**。

为什么不直接用 `python -m compileall` / `py_compile`：
    两者都会在源目录生成 `__pycache__/*.pyc`，等于往仓库工作区里写产物、污染 `git status`。
    提交前检查必须**只读**。

为什么不用 `python -c "import ast..."` 塞进 Makefile：
    逻辑挪进脚本后可被单独调用与测试，Makefile 只留一行 —— 保持 Makefile 可读。

用法：
    python tools/syntax_check.py                # 扫默认目录
    python tools/syntax_check.py scripts src    # 扫指定目录/文件
"""
import ast
import sys
from pathlib import Path

DEFAULT_DIRS = ["scripts", "src", "cron", "tests", "tools", "research"]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".workbuddy",
             "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def iter_py_files(targets):
    """按目录递归 / 单文件直接收 .py，跳过虚拟环境与缓存目录。"""
    for t in targets:
        p = Path(t)
        if p.is_file():
            if p.suffix == ".py":
                yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*.py")):
                if not SKIP_DIRS & set(f.parts):
                    yield f
        else:
            print(f"⚠️  路径不存在，已跳过：{t}", file=sys.stderr)


def main():
    targets = sys.argv[1:] or DEFAULT_DIRS
    ok, bad = 0, []
    for f in iter_py_files(targets):
        try:
            src = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            bad.append((f, "非 UTF-8 编码（须与仓库一致）"))
            continue
        try:
            ast.parse(src, filename=str(f))
            ok += 1
        except SyntaxError as e:
            bad.append((f, f"第 {e.lineno} 行 {e.offset or '?'}: {e.msg}"))
        except Exception as e:                      # 兜底，别让检查本身崩掉
            bad.append((f, f"{type(e).__name__}: {e}"))

    for f, msg in bad:
        print(f"❌ {f}: {msg}")
    total = ok + len(bad)
    print(f"\n{'✅' if not bad else '❌'} 语法检查：{ok}/{total} 个文件通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
