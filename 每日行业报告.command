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
#    5. scripts/build_style_panel.py             风格因子面板 X/f（复现三件套①）
#    6. scripts/measure_chain_resonance.py       链共振测量 + 退化探针（复现三件套②③）
#    7. scripts/mainline_detector.py             主线探测（旧版：全市场自由聚类群）
#   7b. scripts/chain_mainline_detector.py      链版主线探测（B2：方案 C · 37 链，与旧版并存）
#    8. scripts/regime_classifier.py             行情 regime
#    9. scripts/build_nav_924.py                 行业走势主表（924 起累计净值）
#   10. cron/industry_review_daily.py            日报（自带前置补齐 + 完备性检查兜底）
#
#  🔴 2026-10-06：原第 5 步「scripts/industry_tracker.py 贝叶斯反转跟踪」已移除
#     （装置零产出：生产门槛 Sₜ≥50 下 131 个行业命中 0 个；引擎自身事件 h=20 按日聚类 t=−2.87）。
#     链路由九步变八步。依据 docs/plan/retirement-breadth-tracker-plan.md
#
#  🟢 2026-10-07：接入链共振测量层，八步链 → 十步链（review §12.2 / §12.5）
#     · 第 5 步必须排在 update_industry_factor 之后：crowd 因子读的是上一步刚写的
#       ind_crowd_turnover_daily，顺序颠倒会拿到上一交易日的拥挤度。
#     · 第 6 步会直接读第 5 步落盘的 industry_style_factors.parquet（不重算）。
#     · 第 6 步退出码语义：0=探针健康，2=探针判定退化（**产物照样落盘**，日报正文
#       会自动带「未调整（因子面板不可用）」标注）。退化不是崩链，故仍继续后续步骤。
#
#  🟢 2026-10-12：接入链版主线探测（B2），十步链 → 十步 + 7b
#     · 第 7b 步与第 7 步**并存**，产物是两份不同文件，互不覆盖：
#       旧版 industry/mainline_detection.csv（19 列）/ 新版 industry/chain_mainline_detection.csv（22 列）
#     · 新版依赖第 6 步的 chain_resonance_daily.csv，必须排在其后
#     · 暂不替换旧版：口径变更影响面大，先并存观察，台账见
#       research/observation_snapshot.py → _local_dev/observation_log.csv
#     · 依据 docs/plan/chain-mainline-backfill-diagnosis-2026-10-12.md（1638 天回溯支持 B2）
#
#  🟢 2026-10-15：日报**重排**为方向 B 五章（主人拍板「重排 + 旧三章不保留」）
#     · 旧「主线状态 / 行情 regime / 链共振」三章从日报下线：
#       主线信息并入注意力梯队 T1，regime 并入综合判读的「市场宽度」一句，
#       链共振降级为梯队/异动的**输入**（随行业出表，不再单独成章）。
#     · 日报正文由 src/daily_review/attention_radar.py 一次性产出，
#       cron 不再追加任何附加章节。
#     · ⚠️ 第 7/7b/8 步的**脚本暂不下线**：chain_mainline_detection.csv 仍提供
#       37 条链定义（方向 B 的链共振输入），daily_regime 仅不再渲染。
#       十步链的精简（是否下线 7/7b/8）待主人另行确认。
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
    echo "  Industry Radar · 每日行业报告（完整版十步链）"
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

    # 退化感知步骤：退出码 2 =「产物已落盘，但探针判定退化」。
    # 这不是崩链（后续步骤照常跑），但必须比普通失败更显眼—— 风格调整一旦失效，
    # 故障形态是「悄悄退化成原始值」而非「报错」，报表照常出数且看着很健康。
    # 故这里不并入 FAILED（不把正常日判成失败），但强制在控制台与日志里留醒目记录。
    run_degrade_step() {
        local desc="$1"; shift
        echo
        echo "──── $desc ────"
        if [ "$DRY_RUN" = "1" ]; then
            echo "[DRY-RUN] $*"
            return 0
        fi
        "$@"
        local rc=$?
        if [ "$rc" -eq 2 ]; then
            echo "🔻 [退化] $desc 返回 2：产物已落盘，但**风格调整探针未通过**"
            echo "     ⇒ 共振数值为未调整值（含风格暴露成分），不得用于归因/ 链假设。"
            echo "     ⇒ 日报正文会自动带退化标注；完备性检查会报 STYLE_* WARNING。"
        elif [ "$rc" -ne 0 ]; then
            echo "⚠️  $desc 失败 (exit=$rc)，继续后续步骤…"
            FAILED="$FAILED
  - $desc (exit=$rc)"
        fi
        return 0
    }

    START=$SECONDS
    run_step "1/10 每日成分快照"          "$PY" "scripts/build_members_daily.py"
    run_step "2/10 行业市值加权涨跌幅"     "$PY" "scripts/compute_weighted_returns.py"
    run_step "3/10 等权行业日频聚合"       "$PY" "scripts/build_industry_daily_full.py" "--days" "60"
    run_step "4/10 拥挤度因子 ind_crowd"    "$PY" "scripts/update_industry_factor.py"
    run_step "5/10 风格因子面板X/f"        "$PY" "scripts/build_style_panel.py"
    run_degrade_step "6/10 链共振测量+探针"  "$PY" "scripts/measure_chain_resonance.py"
    run_step "7/10 主线探测(旧版聚类)"   "$PY" "scripts/mainline_detector.py"
    # ── B2（2026-10-12）：链版主线探测，与旧版**并存**跑，产物是两份不同文件 ──
    #    旧版 → industry/mainline_detection.csv      （19 列，group_id 开头）
    #    新版 → industry/chain_mainline_detection.csv（22 列，chain_id 开头）
    #    为什么要并存而非直接替换：新版口径变更影响面大，先让两份产物同时在
    #    日更链里跑一段时间，用 research/observation_snapshot.py 记账对照，
    #    稳定后再决定是否下线旧版。任一步失败都不中断后续步骤。
    #    新版依赖第 6 步产出的 chain_resonance_daily.csv ⇒ 必须排在其后。
    run_step "7b/10 链版主线探测(B2)"   "$PY" "scripts/chain_mainline_detector.py"
    run_step "8/10 行情 regime"           "$PY" "scripts/regime_classifier.py"
    run_step "9/10 行业走势主表(924起)"    "$PY" "scripts/build_nav_924.py"
    run_step "10/10 生成日报 ($TRADE_DATE)" "$PY" "cron/industry_review_daily.py" "--date" "$TRADE_DATE"

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
