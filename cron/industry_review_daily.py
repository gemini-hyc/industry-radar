"""行业跟踪日频 cron 入口

用法: python3 cron/industry_review_daily.py [--date YYYY-MM-DD]

前置依赖:
  - 日线行情已由共享数据源的 tushare fetch 任务更新（openclaw 16:16）
  - 行业加权涨跌幅已更新 (scripts/compute_weighted_returns.py)
  - 拥挤度因子已更新 (scripts/update_industry_factor.py)

本脚本会在运行前检查前置数据是否就绪：
  - 如果缺失 → 自动调用更新脚本补齐
  - 如果自动更新失败 → 给出明确报错和修复指引
"""
import subprocess
import sys
from datetime import datetime as _dt
from pathlib import Path


def _find_project_root() -> Path:
    """自动探测项目根目录（本仓库优先，回退 hermes 旧仓库）"""
    # 优先从当前文件推断
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "src" / "daily_review" / "module_02_industry.py").exists():
        return candidate
    # 回退到已知路径
    for p in ["/Users/hyc/quant/industry-radar", "/Users/hyc/.hermes/quant"]:
        pp = Path(p)
        if (pp / "src" / "daily_review" / "module_02_industry.py").exists():
            return pp
    return candidate


def _detect_python() -> str:
    """探测 Python 解释器路径（环境变量 PYTHON 可覆盖，默认当前解释器）"""
    import os
    return os.environ.get("PYTHON") or sys.executable


def _run_update_script(project_root: Path, script_rel: str, desc: str) -> bool:
    """
    运行更新脚本，返回是否成功

    参数:
        project_root: 项目根目录
        script_rel: 脚本相对路径 (如 "scripts/compute_weighted_returns.py")
        desc: 人类可读的描述
    """
    python = _detect_python()
    script_path = project_root / script_rel
    if not script_path.exists():
        print(f"  ⚠️ 脚本不存在: {script_path}")
        return False

    cmd = [python, str(script_path)]
    # compute_weighted_returns.py 支持 --days 参数
    if "compute_weighted_returns" in script_rel:
        cmd.extend(["--days", "5"])

    env = {"PYTHONPATH": str(project_root), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    try:
        print(f"  🔧 正在运行 {desc}…")
        result = subprocess.run(
            cmd,
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,  # 5 分钟超时
        )
        if result.returncode == 0:
            print(f"  ✅ {desc} 完成")
            # 打印关键输出行（跳过空行）
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line:
                    print(f"     {line}")
            return True
        else:
            print(f"  ❌ {desc} 失败 (exit={result.returncode})")
            if result.stderr.strip():
                print(f"  stderr: {result.stderr.strip()[-500:]}")
            if result.stdout.strip():
                print(f"  stdout: {result.stdout.strip()[-500:]}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  ❌ {desc} 超时（>5分钟）")
        return False
    except Exception as e:
        print(f"  ❌ {desc} 异常: {e}")
        return False


def _ensure_prerequisites(trade_date: str, project_root: Path) -> bool:
    """
    确保前置数据就绪，缺失时自动补齐

    返回:
        True — 数据就绪，可以继续
        False — 数据缺失且无法自动补齐
    """
    from src.daily_review.module_02_industry import check_prerequisites

    check = check_prerequisites(trade_date)

    if check["all_ready"]:
        print("✅ 前置数据已就绪")
        return True

    # ── 有数据缺失，尝试自动补齐 ──
    missing = check["missing"]
    details = check["details"]

    print(f"⚠️ 前置数据不完整，缺失: {', '.join(missing)}")
    for key, info in details.items():
        status = "✅" if info["ready"] else "❌"
        print(f"  {status} {key}: 最新日期={info['latest_date']}")

    # ── 自动补齐 ──
    success = True

    if "weighted_returns" in missing:
        print("\n📊 行业加权涨跌幅缺失，尝试自动补齐…")
        if _run_update_script(project_root, "scripts/compute_weighted_returns.py", "计算加权涨跌幅"):
            # 重新检查
            check2 = check_prerequisites(trade_date)
            if check2["weighted_returns"]:
                print("  ✅ 加权涨跌幅已补齐")
            else:
                print("  ⚠️ 加权涨跌幅仍然缺失（可能 daily_basic 数据未拉取）")
                print(f"  修复指引: 手动运行 python3 {project_root}/scripts/compute_weighted_returns.py --days 5")
                success = False
        else:
            print(f"  修复指引: 手动运行 python3 {project_root}/scripts/compute_weighted_returns.py --days 5")
            success = False

    if "crowding" in missing:
        print("\n📊 拥挤度因子缺失，尝试自动补齐…")
        if _run_update_script(project_root, "scripts/update_industry_factor.py", "更新拥挤度因子"):
            check2 = check_prerequisites(trade_date)
            if check2["crowding"]:
                print("  ✅ 拥挤度因子已补齐")
            else:
                print("  ⚠️ 拥挤度因子仍然缺失")
                print(f"  修复指引: 手动运行 python3 {project_root}/scripts/update_industry_factor.py")
                success = False
        else:
            print(f"  修复指引: 手动运行 python3 {project_root}/scripts/update_industry_factor.py")
            success = False

    return success


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="行业跟踪日报")
    parser.add_argument("--date", type=str, default=None, help="交易日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--skip-check", action="store_true", help="跳过前置数据检查（调试用）")
    args = parser.parse_args()

    project_root = _find_project_root()
    sys.path.insert(0, str(project_root))

    trade_date = args.date or _dt.now().strftime("%Y-%m-%d")

    # ── 前置数据检查 ──
    if not args.skip_check:
        print(f"🔍 检查 {trade_date} 前置数据…")
        if not _ensure_prerequisites(trade_date, project_root):
            print(f"\n❌ 前置数据缺失，无法生成 {trade_date} 行业跟踪报告。请按上述指引补齐数据后重试。")
            sys.exit(1)
        print()

    # ── 运行行业跟踪 ──
    from src.daily_review.module_02_industry import run

    report = run(trade_date)
    print(report)
