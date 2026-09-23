#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Industry Radar · 每日行业报告一键生成（完整版）
#  在 Finder 中双击本文件即可运行（macOS 会打开 Terminal 执行）
#
#  等价于手动依次执行:
#    1. scripts/build_members_daily.py           每日成分快照（新交易日必须重建）
#    2. scripts/compute_weighted_returns.py      行业市值加权涨跌幅
#    3. scripts/build_industry_daily_full.py     等权行业日频聚合（nav_e 上游）
#    4. scripts/update_industry_factor.py        拥挤度因子 ind_crowd
#    5. scripts/industry_tracker.py              贝叶斯反转跟踪
#    6. scripts/mainline_detector.py             主线探测
#    7. scripts/regime_classifier.py             行情 regime
#    8. scripts/build_nav_924.py                 行业走势主表（924 起累计净值）
#    9. cron/industry_review_daily.py            日报（自带前置补齐 + 完备性检查兜底）
#
#  成功后自动用系统文本编辑（TextEdit）打开当日报告；运行日志留存于 logs/。
#  调试: DRY_RUN=1 bash 每日行业报告.command （只打印将执行的命令，不计算不写数据）
#  定时: SCHEDULED=1 bash 每日行业报告.command （静默：不弹 TextEdit）
#        退出码 0=全部成功 / 1=日报未生成 / 2=日报已生成但有环节失败
#        定时入口见 scripts/run_daily_scheduled.sh（launchd com.industry-radar.daily）
# ─────────────────────────────────────────────────────────────
set -u
set -o pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || { echo "❌ 无法进入项目目录"; exit 1; }

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_$(date +%Y%m%d_%H%M%S).log"

main() {
    printf '\e]0;Industry Radar · 每日行业报告\a'

    DRY_RUN="${DRY_RUN:-0}"
    SCHEDULED="${SCHEDULED:-0}"   # 1=定时/无人值守：不弹 TextEdit，并向调用方回传真实退出码
    PY="${PYTHON:-python3}"

    echo "══════════════════════════════════════════════════"
    echo "  Industry Radar · 每日行业报告（完整版九步链）"
    echo "  $(date '+%Y-%m-%d %H:%M:%S')  日志: $LOG_FILE"
    echo "══════════════════════════════════════════════════"

    # ── 前置检查 ─────────────────────────────────────────
    if ! command -v "$PY" >/dev/null 2>&1; then
        echo "❌ 未找到 ${PY}，请确认系统 python3 可用"
        exit 1
    fi

    # 数据根目录解析与 src/paths.py 完全一致（环境变量 > config.yaml > 默认）
    DATA_DIR="$("$PY" -c "import sys; sys.path.insert(0, '$PROJECT_DIR'); import src.paths; print(src.paths.DATA_DIR)")" \
        || { echo "❌ 解析数据根目录失败（src.paths 导入失败）"; exit 1; }
    [ -n "$DATA_DIR" ] || { echo "❌ 数据根目录为空"; exit 1; }

    MARKET_DIR="$DATA_DIR/market/daily"
    ls "$MARKET_DIR"/*.parquet >/dev/null 2>&1 \
        || { echo "❌ 行情目录无数据: $MARKET_DIR"; exit 1; }

    # 取最新交易日（自动适应周末/节假日/16:16 前运行）
    TRADE_DATE="$(ls "$MARKET_DIR"/*.parquet | sed 's:.*/::; s:\.parquet$::' | sort | tail -1)"
    TODAY="$(date +%F)"
    if [ "$TRADE_DATE" != "$TODAY" ]; then
        echo "ℹ️  最新交易日为 ${TRADE_DATE}（今日 $TODAY 数据尚未落地或非交易日，将生成该日报告）"
    fi

    # 成员映射周更提醒（超过 7 天未更新）
    MEMBERS="$DATA_DIR/industry/industry_members.parquet"
    if [ -n "$(find "$MEMBERS" -mtime +7 2>/dev/null)" ]; then
        echo "⚠️  行业成员映射已超 7 天未更新，建议择机运行: python3 scripts/pull_futu_industry.py"
    fi

    # ── 逐步执行（某步失败不中断，日报自带前置补齐兜底）──
    FAILED=""

    run_step() {
        local desc="$1"; shift
        echo
        echo "──── $desc ────"
        if [ "$DRY_RUN" = "1" ]; then
            echo "[DRY-RUN] $*"
            return 0
        fi
        "$@"
        local rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "⚠️  $desc 失败 (exit=$rc)，继续后续步骤…"
            FAILED="$FAILED
  - $desc (exit=$rc)"
        fi
        return 0
    }

    START=$SECONDS
    run_step "1/9 每日成分快照"           "$PY" "scripts/build_members_daily.py"
    run_step "2/9 行业市值加权涨跌幅"     "$PY" "scripts/compute_weighted_returns.py"
    run_step "3/9 等权行业日频聚合"       "$PY" "scripts/build_industry_daily_full.py" "--days" "60"
    run_step "4/9 拥挤度因子 ind_crowd"    "$PY" "scripts/update_industry_factor.py"
    run_step "5/9 贝叶斯反转跟踪"         "$PY" "scripts/industry_tracker.py"
    run_step "6/9 主线探测"               "$PY" "scripts/mainline_detector.py"
    run_step "7/9 行情 regime"            "$PY" "scripts/regime_classifier.py"
    run_step "8/9 行业走势主表(924起)"     "$PY" "scripts/build_nav_924.py"
    run_step "9/9 生成日报 ($TRADE_DATE)" "$PY" "cron/industry_review_daily.py" "--date" "$TRADE_DATE"

    # ── 结果汇总 ─────────────────────────────────────────
    REPORT="$DATA_DIR/reports/daily-analysis/${TRADE_DATE}_industry_review.md"
    echo
    echo "══════════════════════════════════════════════════"
    echo "  完成 · 耗时 $((SECONDS - START)) 秒"
    echo "══════════════════════════════════════════════════"

    local rc=0
    if [ "$DRY_RUN" = "1" ]; then
        echo "🔎 DRY-RUN 结束：未执行任何计算、未写入任何数据"
    elif [ -f "$REPORT" ]; then
        echo "✅ 日报已生成:"
        echo "   $REPORT"
        echo "   日志: $LOG_FILE"
        if [ -n "$FAILED" ]; then
            echo "⚠️  以下环节失败（报告已生成，建议事后排查）:$FAILED"
            rc=2
        fi
        # 定时/无人值守下不弹编辑器（SCHEDULED=1）
        [ "$SCHEDULED" != "1" ] && open -a "TextEdit" "$REPORT"
    else
        echo "❌ 日报未能生成: $REPORT"
        [ -n "$FAILED" ] && echo "失败环节:$FAILED"
        echo
        echo "排查建议:"
        echo "  1. 确认已过 16:16 且当日行情已落地:"
        echo "     ls $DATA_DIR/market/daily/$TRADE_DATE.parquet $DATA_DIR/daily_basic/$TRADE_DATE.parquet"
        echo "  2. 查看完整日志:"
        echo "     tail -80 $LOG_FILE"
        echo "  3. 单步复现:"
        echo "     cd $PROJECT_DIR && $PY cron/industry_review_daily.py --date $TRADE_DATE"
        rc=1
    fi

    echo
    # 交互式终端（双击打开的 Terminal）下等待回车再关窗
    # 必须同时判 stdin+stdout 都是 tty：否则「重定向输出到日志但 stdin 仍是终端」时会永久阻塞
    if [ -t 0 ] && [ -t 1 ]; then
        read -r -p "按回车键关闭窗口…" _
    fi
    return $rc
}

main 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}        # 取 main 的退出码（tee 恒为 0，不能用作判据）
exit "$RC"
