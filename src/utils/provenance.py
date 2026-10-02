"""产物指纹（provenance sidecar）—— 让「谁写的 / 什么时候写的 / 哪版代码写的」成为数据的一部分。

动机
----
代码走 git、产物走 rsync，两条通道互不关联。于是：
  · 改了口径 → 历史产物**静默**变成混合口径，没人发现得了；
  · 从端 pull 到产物 → 无法判断它对应哪一版代码；
  · 台账行数对不上（如 2234 vs 2204）→ 查不出是哪一次产出造成的。

本模块在落盘时同步写一份 ``<文件名>.meta.json``，记录：
  produced_at / hostname / producer（生产脚本）/ git（提交 + dirty 标记）/ params /
  rows / columns / columns_hash / date_range / inputs（输入文件指纹）

设计约束
--------
1. **纯增量**：只多写一个 sidecar，不改任何口径、不动任何现有数据。
2. **永不阻塞**：sidecar 写失败只告警，绝不影响主产物落盘。
3. **与写保护同点**：``save_dataframe`` / ``save_json`` / ``save_text`` 同时承担
   「从端写保护」（``src.paths.resolve_output_path``）与「写指纹」，保证两者覆盖同一批落盘点。

用法::

    from src.utils.provenance import save_dataframe
    save_dataframe(daily, OUT_PATH, producer="scripts/build_industry_daily_full.py",
                   params={"days": days}, inputs=[MARKET_DIR, members_daily_path])
"""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import pandas as pd

from src.paths import DATA_DIR, PROJECT_ROOT, resolve_output_path

SIDECAR_SUFFIX = ".meta.json"
SCHEMA_VERSION = 1
DATE_COLUMNS = ("date", "signal_date", "trade_date")

PathLike = Union[str, Path]

_GIT_CACHE: Optional[Dict[str, Any]] = None


# ── 路径与元信息 ──────────────────────────────────────────────

def sidecar_path(target: PathLike) -> Path:
    """产物路径 → 其指纹文件路径（``X.parquet`` → ``X.parquet.meta.json``）。

    用「全名 + 后缀」而非替换后缀，避免 ``a.parquet`` 与 ``a.csv`` 撞名。
    """
    p = Path(target)
    return p.with_name(p.name + SIDECAR_SUFFIX)


def relative_to_known_roots(path: PathLike) -> str:
    """尽量用相对路径记录（仓库根优先，其次数据根），跨机可读。"""
    p = Path(path)
    for root in (PROJECT_ROOT, Path(DATA_DIR)):
        try:
            return str(p.resolve().relative_to(Path(root).resolve()))
        except Exception:
            continue
    return str(p)


def default_producer() -> str:
    """默认生产者 = 当前进程的入口脚本（相对仓库/数据根的路径）。

    脚本直接调用时 ``sys.argv[0]`` 就是 ``scripts/xxx.py``；被 cron 或库模块调用时
    调用方应显式传 ``producer=``，否则记成入口脚本名（cron 入口也是有效信息）。
    """
    argv0 = sys.argv[0] if getattr(sys, "argv", None) else ""
    if not argv0:
        return "<unknown>"
    if argv0.startswith("-"):        # python -c / -m：没有脚本文件
        return f"<inline:{argv0}>"
    return relative_to_known_roots(Path(argv0))


def git_info() -> Dict[str, Any]:
    """当前仓库的提交信息（进程内缓存一次）。

    dirty 只看**已跟踪文件**的改动（``--untracked-files=no``）：未跟踪文件不进提交，
    不影响「这份产物能否由 commit X 复现」这个判断，否则研究脚本一多就天天报脏。
    """
    global _GIT_CACHE
    if _GIT_CACHE is not None:
        return _GIT_CACHE

    info: Dict[str, Any] = {"commit": None, "branch": None, "dirty": None}
    try:
        def _git(*args: str):
            return subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), *args],
                capture_output=True, text=True, timeout=15,
            )

        r = _git("rev-parse", "--short", "HEAD")
        if r.returncode == 0:
            info["commit"] = r.stdout.strip()
        r = _git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode == 0:
            info["branch"] = r.stdout.strip() or None
        r = _git("status", "--porcelain", "--untracked-files=no")
        if r.returncode == 0:
            info["dirty"] = bool(r.stdout.strip())
    except Exception as e:                                   # 无 git / 超时 / 权限
        info["error"] = f"{type(e).__name__}: {e}"
    _GIT_CACHE = info
    return info


def columns_hash(columns: Optional[Sequence[str]],
                 dtypes: Optional[Sequence[str]] = None) -> Optional[str]:
    """列结构哈希（列名 + dtype）。

    口径变更往往同时体现为列名或 dtype 变化（如 total_mv → circ_mv、单位换算），
    因此 dtype 一并入哈希。同口径重跑 → 哈希不变；口径变了 → 哈希变，历史产物即可
    被自动识别为「混合口径」。
    """
    if not columns:
        return None
    items = [str(c) for c in columns]
    if dtypes is not None:
        items = [f"{c}:{d}" for c, d in zip(columns, dtypes)]
    digest = hashlib.sha256("|".join(items).encode("utf-8")).hexdigest()
    return "sha256:" + digest[:16]


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None


def _date_range(df: Optional[pd.DataFrame]) -> Optional[List[str]]:
    """产物覆盖的日期范围（取第一个命中的日期列）。"""
    if df is None:
        return None
    for col in DATE_COLUMNS:
        if col in df.columns:
            try:
                s = pd.to_datetime(df[col])
                if s.notna().any():
                    return [str(s.min().date()), str(s.max().date())]
            except Exception:
                continue
    return None


def inputs_fingerprint(inputs: Optional[Iterable[PathLike]]) -> Optional[List[Dict[str, Any]]]:
    """输入文件/目录指纹（路径 + 大小 + mtime；目录记文件数与最新 mtime）。

    用途：产物「新不新」不再只看自身时间戳，而是能对上输入。看到
    ``meta.inputs`` 里有比 ``produced_at`` 更晚的 mtime，就说明产物落后于输入。
    """
    if not inputs:
        return None
    out: List[Dict[str, Any]] = []
    for item in inputs:
        p = Path(item)
        rec: Dict[str, Any] = {"path": relative_to_known_roots(p)}
        try:
            if p.is_dir():
                files = [f for f in p.iterdir()
                         if f.is_file() and not f.name.endswith(SIDECAR_SUFFIX)]
                latest = max((f.stat().st_mtime for f in files), default=None)
                rec.update({"dir": True, "n_files": len(files), "latest_mtime": _iso(latest)})
            elif p.exists():
                st = p.stat()
                rec.update({"size": st.st_size, "mtime": _iso(st.st_mtime)})
            else:
                rec["missing"] = True
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        out.append(rec)
    return out


# ── 构造与写入 ────────────────────────────────────────────────

def build_meta(
    target: PathLike,
    *,
    df: Optional[pd.DataFrame] = None,
    producer: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    inputs: Optional[Iterable[PathLike]] = None,
    rows: Optional[int] = None,
    columns: Optional[Sequence[str]] = None,
    dtypes: Optional[Sequence[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    logical: Optional[PathLike] = None,
) -> Dict[str, Any]:
    """组装指纹内容。给了 df 就从 df 自动取行数/列/日期范围。

    ``target`` 是**实际写入**的位置；``logical`` 是调用方原本想要的产物位置。
    两者不同（只读重定向）时，``target`` 记逻辑落点、另加 ``written_to`` 记物理位置 ——
    这样指纹描述的是「产物代表什么」，跨机/跨沙箱都读得懂。
    """
    if df is not None:
        if rows is None:
            rows = int(len(df))
        if columns is None:
            columns = [str(c) for c in df.columns]
        if dtypes is None:
            dtypes = [str(d) for d in df.dtypes]

    meta: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "produced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "producer": producer or default_producer(),
        "git": git_info(),
        "target": relative_to_known_roots(logical if logical is not None else target),
        "rows": rows,
        "columns": list(columns) if columns is not None else None,
        "columns_hash": columns_hash(columns, dtypes),
        "date_range": _date_range(df),
        "params": params or {},
        "inputs": inputs_fingerprint(inputs),
    }
    if logical is not None and str(Path(logical)) != str(Path(target)):
        meta["written_to"] = relative_to_known_roots(target)
    if extra:
        meta["extra"] = extra
    return meta


def write_sidecar(target: PathLike, meta: Dict[str, Any]) -> Optional[Path]:
    """落盘指纹。失败只告警，绝不抛给调用方（不阻塞主产物）。"""
    try:
        p = sidecar_path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return p
    except Exception as e:
        print(f"⚠️ 产物指纹写入失败（不影响主流程）: {sidecar_path(target)} — {e}")
        return None


def record(
    target: PathLike,
    *,
    df: Optional[pd.DataFrame] = None,
    producer: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    inputs: Optional[Iterable[PathLike]] = None,
    rows: Optional[int] = None,
    columns: Optional[Sequence[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    logical: Optional[PathLike] = None,
) -> Optional[Path]:
    """为「已由调用方自己落盘」的产物补写指纹。"""
    meta = build_meta(target, df=df, producer=producer, params=params,
                      inputs=inputs, rows=rows, columns=columns, extra=extra,
                      logical=logical)
    return write_sidecar(target, meta)


# ── 带指纹 + 写保护的落盘入口（推荐用法）──────────────────────

def save_dataframe(
    df: pd.DataFrame,
    path: PathLike,
    *,
    fmt: Optional[str] = None,
    index: bool = False,
    sep: Optional[str] = None,
    producer: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    inputs: Optional[Iterable[PathLike]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """写 DataFrame 产物（parquet / csv / tsv），并落指纹。返回**实际**写入路径。

    实际路径可能因从端写保护（``INDUSTRY_RADAR_READONLY``）与传入路径不同，
    调用方应用返回值做打印/后续引用。
    """
    target = Path(resolve_output_path(path))          # 写保护在此生效
    target.parent.mkdir(parents=True, exist_ok=True)
    kind = (fmt or target.suffix.lstrip(".") or "parquet").lower()
    if kind == "parquet":
        df.to_parquet(target, index=index)
    elif kind in ("csv", "tsv", "txt"):
        df.to_csv(target, index=index, sep=sep if sep is not None else ("\t" if kind == "tsv" else ","))
    else:
        raise ValueError(f"save_dataframe 不支持的格式: {kind}（{target}）")
    record(target, df=df, producer=producer, params=params, inputs=inputs,
           extra=extra, logical=path)
    return target


def save_json(
    obj: Any,
    path: PathLike,
    *,
    indent: int = 2,
    producer: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    inputs: Optional[Iterable[PathLike]] = None,
    rows: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """写 JSON 产物并落指纹（用于结论文件、状态文件等非表格产物）。"""
    target = Path(resolve_output_path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    record(target, producer=producer, params=params, inputs=inputs, rows=rows,
           extra=extra, logical=path)
    return target


def save_text(
    text: str,
    path: PathLike,
    *,
    encoding: str = "utf-8",
    producer: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    inputs: Optional[Iterable[PathLike]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """写文本产物（Markdown 报告等）并落指纹。"""
    target = Path(resolve_output_path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding=encoding)
    record(target, producer=producer, params=params, inputs=inputs,
           extra=extra, logical=path)
    return target


# ── 读取（供 check_data_completeness.py 等消费）────────────────

def load_meta(target: PathLike) -> Optional[Dict[str, Any]]:
    """读指纹；缺失或损坏返回 None。"""
    try:
        p = sidecar_path(target)
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
