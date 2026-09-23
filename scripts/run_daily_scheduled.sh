#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Industry Radar · 定时入口（launchd 专用，勿双击）
#
#  九步链（每日行业报告.command）本身是给「人在场双击」设计的，本脚本补上
#  无人值守必需的判断，职责严格分离：
#    1. 非交易日 → 直接跳过（不做无意义空跑）
#    2. 上游数据（market/daily + daily_basic 当日文件）未落地 → 本次跳过，
#       等下一次触发；若已是当日最后一次触发（≥21:00）仍无数据 → 告警
#    3. 当日已成功执行过 → 幂等退出（配合 plist 的多次触发实现「自动重试」）
#    4. 调用九步链（SCHEDULED=1 静默模式，不弹 TextEdit）
#    5. 读 completeness_status.json 核对结果（除退出码外的第二道判据）
#    6. 失败 / 有警告 / 数据陈旧 → 发 macOS 系统通知
#
#  ⚠️ 编码约定：变量引用后若紧跟中文全角标点，必须写成 ${VAR} 形式。
#     全角标点是多字节 UTF-8，bash 会把它并入变量名导致 unbound variable。
#
#  注册:     launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.industry-radar.daily.plist
#            ⚠️ 必须在用户自己的 Terminal 执行；Agent 上下文写 gui 域会被拒（errno 5）
#  手动测试: /bin/bash scripts/run_daily_scheduled.sh   （或 launchctl kickstart gui/$(id -u)/com.industry-radar.daily）
#
#  日志分散在两个目录，各司其职：
#    ① 本脚本的 stdout/stderr → /Users/hyc/quant-data/logs/industry-radar-scheduled.log
#       （由 plist 的 StandardOutPath 兜住；只看这一个就知道今天跑没跑、成没成）
#    ② 九步链内部的详细日志 → /Users/hyc/quant/industry-radar/logs/scheduled_*.log
#       幂等标记也在该目录: logs/.scheduled_done_YYYY-MM-DD
# ─────────────────────────────────────────────────────────────
set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="/usr/bin/python3"                      # launchd 的 PATH 不含 /usr/local/bin，必须绝对路径
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

TODAY="$(date +%F)"
DONE_MARK="$LOG_DIR/.scheduled_done_$TODAY"
RUN_LOG="$LOG_DIR/scheduled_$(date +%Y%m%d_%H%M%S).log"

log() { echo "[$(date '+%F %T')] $*"; }

notify() {  # $1=标题 $2=正文（macOS 通知中心，LaunchAgent 在 GUI 域内可直接发）
    /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Basso\"" >/dev/null 2>&1 || true
}

# ── 1. 解析数据根目录（与 src/paths.py 保持一致）──
DATA_DIR="$("$PY" -c "import sys; sys.path.insert(0, '$PROJECT_DIR'); import src.paths; print(src.paths.DATA_DIR)" 2>/dev/null)" || DATA_DIR=""
if [ -z "$DATA_DIR" ]; then
    log "❌ 无法解析数据根目录（src.paths 导入失败）"
    notify "行业日更失败" "无法解析数据根目录，请检查项目配置"
    exit 1
fi

# ── 2. 非交易日跳过（本地 trade_cal.parquet 只含开市日：命中=交易日）──
IS_TRADE="$("$PY" -c "
import pandas as pd
cal = pd.read_parquet('$DATA_DIR/trade_cal.parquet', columns=['cal_date'])
key = int(pd.Timestamp('$TODAY').strftime('%Y%m%d'))
print(1 if key in set(cal['cal_date'].astype('int64')) else 0)
" 2>/dev/null)"

if [ "$IS_TRADE" = "0" ]; then
    log "⏭  今日 $TODAY 非交易日，跳过"
    exit 0
elif [ "$IS_TRADE" != "1" ]; then
    # 判定失败不静默跳过（宁可空跑也不能漏跑），继续走后续流程
    log "⚠️  交易日历判定异常（输出='$IS_TRADE'），继续执行"
fi

# ── 3. 幂等：当日已成功执行过则退出 ──
if [ -f "$DONE_MARK" ]; then
    log "✔ 今日已成功执行过，跳过（标记: ${DONE_MARK}）"
    exit 0
fi

# ── 4. 上游数据就绪检查 ──
if [ ! -f "$DATA_DIR/market/daily/$TODAY.parquet" ] || [ ! -f "$DATA_DIR/daily_basic/$TODAY.parquet" ]; then
    log "⏳ 今日 $TODAY 行情/市值尚未落地，本次跳过（等待下一次触发）"
    # 21:00 是当日最后一次触发：此时仍无数据说明上游拉取出了问题
    if [ "$(date +%H)" -ge 21 ]; then
        log "❌ 已达当日最后一次触发（≥21:00），上游数据仍未落地"
        notify "行业日更未执行" "$TODAY 上游数据未落地，今日跳过。请检查 18:30 数据拉取。"
    fi
    exit 0
fi

# ── 5. 执行九步链（静默）──
log "▶ 开始执行九步链（静默）→ $RUN_LOG"
SCHEDULED=1 PYTHON="$PY" bash "$PROJECT_DIR/每日行业报告.command" > "$RUN_LOG" 2>&1
RC=$?

# ── 6. 核对完备性状态（第二道判据）──
COMP_TXT=""
COMP_RC=""
if [ -f "$DATA_DIR/industry/completeness_status.json" ]; then
    read -r COMP_RC COMP_TXT < <("$PY" -c "
import json
j = json.load(open('$DATA_DIR/industry/completeness_status.json'))
print(j.get('exit_code', '?'), f\"{j.get('status','?')}({j.get('target_date','?')})\")
" 2>/dev/null) || true
fi

# ── 7. 结果处理与告警 ──
case "$RC" in
    0)
        if [ "$COMP_RC" = "0" ]; then
            log "✅ 日更成功，完备性 $COMP_TXT"
        else
            log "✅ 日更成功，但完备性异常（exit=$COMP_RC, ${COMP_TXT}）"
            notify "行业日更完成（数据有警告）" "完备性 ${COMP_TXT}，详见 $(basename "$RUN_LOG")"
        fi
        : > "$DONE_MARK"
        exit 0
        ;;
    2)
        # 日报已生成但有环节失败：标记完成，避免反复重跑，但要告警
        log "⚠️  日更完成，但有环节失败（完备性 ${COMP_TXT}）"
        notify "行业日更部分失败" "有环节执行失败，详见 $(basename "$RUN_LOG")"
        : > "$DONE_MARK"
        exit 2
        ;;
    *)
        # 日报未生成：不写标记，下一次触发会自动重试
        log "❌ 日更失败 (exit=$RC)，日报未生成；下次触发将重试"
        notify "行业日更失败" "日报未生成 (exit=$RC)，详见 $(basename "$RUN_LOG")"
        exit 1
        ;;
esac
