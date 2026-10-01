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
import json
import os
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
    for p in ["/Users/hyc/Quant/industry-radar", "/Users/hyc/.hermes/quant"]:
        pp = Path(p)
        if (pp / "src" / "daily_review" / "module_02_industry.py").exists():
            return pp
    return candidate


def _detect_python() -> str:
    """探测 Python 解释器路径（环境变量 PYTHON 可覆盖，默认当前解释器）"""
    return os.environ.get("PYTHON") or sys.executable


def _subprocess_env(project_root: Path) -> dict:
    """
    构造子进程环境。

    必须显式透传数据根覆盖 —— 否则子进程（完备性检查 / 前置补齐脚本）会退回
    默认数据根 /Users/hyc/quant-data。在自定义 INDUSTRY_RADAR_DATA 的部署下，
    那会让检查与写入落到错误的数据集上，本模块也会因此读不到完备性报告。
    """
    env = {"PYTHONPATH": str(project_root), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    for key in ("INDUSTRY_RADAR_DATA", "INDUSTRY_RADAR_FUTU_HOST"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


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

    env = _subprocess_env(project_root)
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


def _run_completeness_check(project_root: Path, trade_date: str) -> int:
    """
    运行数据完备性检查（防静默缺数：整行缺失 / 列级空洞 / 成分骤降）

    返回退出码: 0 健康 / 1 严重(CRITICAL) / 2 警告(WARNING) / 3 非交易日跳过
    """
    python = _detect_python()
    script = project_root / "scripts" / "check_data_completeness.py"
    if not script.exists():
        print("  ⚠️ 完备性检查脚本不存在，跳过（scripts/check_data_completeness.py）")
        return 0

    env = _subprocess_env(project_root)
    try:
        r = subprocess.run(
            [python, str(script), "--date", trade_date],
            cwd=str(project_root), env=env,
            capture_output=True, text=True, timeout=180,
        )
        if r.stdout.strip():
            print(r.stdout.strip())
        if r.stderr.strip():
            print(f"  stderr: {r.stderr.strip()[-400:]}")
        return r.returncode
    except subprocess.TimeoutExpired:
        print("  ❌ 完备性检查超时（>3分钟）")
        return 1
    except Exception as e:
        print(f"  ❌ 完备性检查异常: {e}")
        return 1


def _read_completeness_report() -> dict:
    """读取完备性检查落盘的 JSON 结果（判定台账是否放行时用）"""
    from src.paths import DATA_DIR

    try:
        with open(DATA_DIR / "industry" / "completeness_status.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# 情绪类（SENTI_*）缺数不在行业信号链路上：冷区信号只依赖 industry_daily_full 的
# 宽度序列，情绪数据仅是附带特征。因此这类 CRITICAL 只告警、不阻断台账入账。
_LEDGER_SAFE_PREFIXES = ("SENTI_",)


def _blocks_ledger(report: dict, trade_date: str) -> bool:
    """
    判断 CRITICAL 缺数是否触及行业信号的数据基础（决定是否阻断台账入账）。

    True  = 阻断：存在源数据/加权/成分/行情卫生类 CRITICAL，或拿不到可信报告
    False = 放行：CRITICAL 全部来自 SENTI_* 情绪类数据
    """
    if not report or report.get("target_date") != trade_date:
        # 报告缺失或不是本次目标日 → 无可信依据。台账是持久留痕，宁可停更也不污染
        return True
    for it in report.get("issues", []) or []:
        if it.get("level") != "CRITICAL":
            continue
        if not str(it.get("code", "")).startswith(_LEDGER_SAFE_PREFIXES):
            return True
    return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="行业跟踪日报")
    parser.add_argument("--date", type=str, default=None, help="交易日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--skip-check", action="store_true", help="跳过前置数据检查（调试用）")
    parser.add_argument("--skip-completeness", action="store_true",
                        help="跳过数据完备性检查（调试用）")
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

    # ── 追加链路其余产出（跟踪池 / 主线 / regime）──
    # 只读现有产出，不重算、不改 module_02 口径；任一环节异常都不影响主报告
    extra = ""
    try:
        from src.daily_review.report_sections import append_to_report, build_extra_sections

        extra = build_extra_sections(trade_date)
        if append_to_report(trade_date, extra) is None:
            extra = ""  # 主报告未落盘（非交易日/写入失败）→ 附加章节也不展示
        else:
            print("\n[附加章节] 已追加 跟踪池 / 主线状态 / 行情 regime 至报告文件")
    except Exception as e:
        extra = ""
        print(f"⚠️ 附加章节生成失败（不影响主报告）: {e}")

    print(report)
    if extra:
        print(extra)

    # ── 数据完备性检查（防静默缺数）──
    # 报告已落盘后再检查：目标是暴露「文件有行但行业/列悄悄为空」的情况
    completeness_rc = 0
    if not args.skip_completeness:
        print()
        print("🔎 数据完备性检查…")
        completeness_rc = _run_completeness_check(project_root, trade_date)
        if completeness_rc == 2:
            print("\n⚠️ 存在非致命数据异常（报告仍可用），建议按上方指引排查。")
        elif completeness_rc == 3:
            print(f"⏭️  {trade_date} 非交易日，跳过完备性检查。")

    # ── 信号台账：当日快照入账 + 前向收益回填（验证底座）──
    # 仍放在完备性检查之后（坏数据不入账），但**不再被退出码短路**：
    # 2026-09-24 起台账停更 6 个交易日，根因就是 SENTI_* 缺数触发的 sys.exit(1)
    # 位于此处之前，把与行业信号无关的缺数连带成了"台账杀手"。
    blocked = completeness_rc == 1 and _blocks_ledger(_read_completeness_report(), trade_date)

    if completeness_rc == 3:
        print(f"⏭️  {trade_date} 非交易日，跳过信号台账入账。")
    elif blocked:
        print("\n⛔ 完备性检查 CRITICAL 触及行业信号数据基础，跳过台账入账以避免污染。")
        print("   修复上方缺数后重跑：python3 cron/industry_review_daily.py --date " + trade_date)
    else:
        try:
            from src.signals import ledger as _ledger
            from src.signals.sources import cold_zone_history, snapshot_signals

            snapshots = snapshot_signals(trade_date)
            cold = []
            try:  # 当日冷区信号（旗舰信号逐日入账，不依赖手工回填）
                cold = cold_zone_history(start=trade_date, end=trade_date, with_crowding=False)
            except Exception as e:
                print(f"⚠️ 冷区信号入账失败（不影响其他来源）: {e}")

            rows = list(snapshots) + list(cold)
            led = _ledger.load_ledger()
            before = len(led)
            led = _ledger.upsert(led, rows)
            led = _ledger.backfill_forward_returns(led)
            path = _ledger.save_ledger(led)
            print(f"\n[信号台账] {trade_date} 入账 {len(rows)} 条"
                  f"（快照 {len(snapshots)} + 冷区 {len(cold)}）；"
                  f"台账 {before} → {len(led)} 条 → {path}")
        except Exception as e:
            print(f"⚠️ 信号台账更新失败（不影响主报告）: {e}")

    # ── 退出码（供 scripts/run_daily_scheduled.sh 决策）──
    #   1 = 关键数据缺失且台账已阻断 → 不写幂等标记，下次触发自动重试
    #   2 = 报告与台账已生成，仅存在非阻断缺陷（如情绪类缺数）→ 写标记 + 告警一次，
    #       避免当日 4 次触发反复重跑整条链
    if completeness_rc == 1:
        if blocked:
            print(f"\n❌ 检测到严重缺数（触及行业信号数据基础），"
                  f"{trade_date} 报告可能不完整，请按上方指引修复后重跑。")
            sys.exit(1)
        print(f"\n⚠️ 检测到严重缺数，但仅为情绪类数据（SENTI_*），不影响行业信号；"
              f"报告与台账均已生成。")
        sys.exit(2)
