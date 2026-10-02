"""
Industry Radar 路径与连接配置 — 全项目唯一数据根解析入口

数据根目录解析优先级（高 → 低）:
  1. 环境变量 INDUSTRY_RADAR_DATA
  2. config/config.yaml 的 data_dir
  3. 默认 /Users/hyc/quant-data

富途 OpenD 连接: config/config.yaml 的 futu.host / futu.port，
默认 localhost:11111（macOS 本机直连；容器内运行时改为 host.docker.internal）。

铁律: 所有模块禁止再硬编码 /opt/data 容器路径，一律从本模块取路径。
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

DEFAULT_DATA_DIR = Path("/Users/hyc/quant-data")
DEFAULT_FUTU_HOST = "localhost"
DEFAULT_FUTU_PORT = 11111


def _read_config() -> dict:
    """读取 config.yaml；无 pyyaml 或文件缺失时静默降级为空配置"""
    try:
        import yaml
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def resolve_data_dir() -> Path:
    env = os.environ.get("INDUSTRY_RADAR_DATA")
    if env:
        return Path(env).expanduser().resolve()
    cfg = _read_config()
    if cfg.get("data_dir"):
        return Path(cfg["data_dir"]).expanduser().resolve()
    return DEFAULT_DATA_DIR


def resolve_futu() -> tuple:
    cfg = _read_config().get("futu") or {}
    host = os.environ.get("INDUSTRY_RADAR_FUTU_HOST") or cfg.get("host") or DEFAULT_FUTU_HOST
    port = cfg.get("port") or DEFAULT_FUTU_PORT
    return str(host), int(port)


# ── 数据路径（DATA_DIR 即共享数据根，如 /Users/hyc/quant-data）──
DATA_DIR = resolve_data_dir()
MARKET_DIR = DATA_DIR / "market" / "daily"          # 日线行情
BASIC_DIR = DATA_DIR / "daily_basic"                # 每日市值（tushare fetch 任务维护）
INDUSTRY_DIR = DATA_DIR / "industry"                # 行业数据与状态
FACTOR_DIR = DATA_DIR / "factors"                   # 因子库
SIGNALS_DIR = DATA_DIR / "signals"                  # 信号台账与评估（验证底座）
SIGNALS_LOG_PATH = SIGNALS_DIR / "signals_log.parquet"  # 全系统信号台账
REPORTS_DIR = DATA_DIR / "reports" / "daily-analysis"  # 日报输出
STOCK_BASIC_PATH = DATA_DIR / "stock_basic.parquet"  # 股票列表
STK_LIMIT_DIR = DATA_DIR / "stk_limit"              # 每日涨跌停价（用于识别「无涨跌幅限制日」）


# ── 产物登记表（唯一真相来源，2026-10-02）──────────────────────────
# 用途：
#   1) src/utils/provenance.py 落盘时据此判定「这是不是产物」，并写 <文件名>.meta.json 指纹；
#   2) scripts/check_data_completeness.py 据此扫描「已登记产物是否缺指纹」。
# kind 语义：
#   derived 本项目计算产出（可再生，口径随代码变）
#   fetched 从外部抓取（Tushare / 富途 OpenD），属原料层
#   cache   中间缓存（可随时重建，丢了不影响信号）
#   status  运行状态记录（check_data_completeness 的产物，非数据）
# 新增产物必须登记在此 —— 否则完备性检查的 --audit-writes 会把它报成「未登记写入」。
ARTIFACTS = {
    # ── derived：计算产出 ──
    "industry_daily_full":       (INDUSTRY_DIR / "industry_daily_full.parquet", "derived"),
    "industry_weighted_returns": (INDUSTRY_DIR / "industry_weighted_returns.parquet", "derived"),
    "industry_members_daily":    (INDUSTRY_DIR / "industry_members_daily.parquet", "derived"),
    "industry_nav_924":          (INDUSTRY_DIR / "industry_nav_924.parquet", "derived"),
    "daily_regime":              (INDUSTRY_DIR / "daily_regime.parquet", "derived"),
    "monthly_regime_summary":    (INDUSTRY_DIR / "monthly_regime_summary.csv", "derived"),
    "mainline_detection":        (INDUSTRY_DIR / "mainline_detection.csv", "derived"),
    "tracker_report":            (INDUSTRY_DIR / "tracker_report.csv", "derived"),
    "industry_universe":         (DATA_DIR / "industry_universe.parquet", "derived"),
    "ind_crowd_turnover_daily":  (FACTOR_DIR / "ind_crowd_turnover_daily.parquet", "derived"),
    "industry_margin_daily":     (SIGNALS_DIR / "industry_margin_daily.parquet", "derived"),
    "signals_log":               (SIGNALS_LOG_PATH, "derived"),
    # ── cache：中间缓存 ──
    "industry_daily_cache":      (INDUSTRY_DIR / "industry_daily.parquet", "cache"),
    # ── status：运行状态记录（非数据产物，登记只为免被审计误报）──
    "completeness_status":       (INDUSTRY_DIR / "completeness_status.json", "status"),
    # ── fetched：外部抓取原料 ──
    "index_member_all_raw":      (INDUSTRY_DIR / "index_member_all_raw.parquet", "fetched"),
    "industry_list":             (INDUSTRY_DIR / "industry_list.parquet", "fetched"),
    "industry_members":          (INDUSTRY_DIR / "industry_members.parquet", "fetched"),
}

# 派生数据的落地目录（--audit-writes 扫描范围）
DERIVED_DIRS = (INDUSTRY_DIR, FACTOR_DIR, SIGNALS_DIR)


# ── 从端写保护（2026-10-02）─────────────────────────────────────
# 背景：本机（从端）的量化数据根是源端单向同步下来的只读副本，写入既会被下次
#       pull 覆盖、又可能污染下游。此开关让「写不进去」成为物理事实，而不是靠人
#       记得挂环境变量。
#   INDUSTRY_RADAR_READONLY 未设 / 0 / off  → 正常写入
#   =1 / true / yes / redirect            → 数据根内的写入重定向到
#                                            INDUSTRY_RADAR_OUTPUT_DIR
#                                            （未设该变量则落到 /tmp/industry-radar-out）
#   =strict                               → 数据根内的写入直接抛 ReadOnlyError（硬拒绝）
# 数据根之外的路径（仓库内、/tmp）在任何模式下都原样放行。
READONLY_RAW = (os.environ.get("INDUSTRY_RADAR_READONLY") or "").strip().lower()
READONLY_MODE = ("redirect" if READONLY_RAW in ("1", "true", "yes", "redirect")
                 else "strict" if READONLY_RAW == "strict" else "off")
OUTPUT_OVERRIDE = Path(
    os.environ.get("INDUSTRY_RADAR_OUTPUT_DIR") or "/tmp/industry-radar-out"
).expanduser()

_REDIRECT_NOTIFIED: set = set()


class ReadOnlyError(RuntimeError):
    """从端写保护：拒绝写入共享数据根。"""


def _is_under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception:
        return False


def resolve_output_path(path):
    """把「打算写入的路径」解析为「实际应写入的路径」（从端写保护唯一入口）。

    READONLY_MODE=off      → 原样返回
    READONLY_MODE=redirect → 数据根内的路径整体搬到 OUTPUT_OVERRIDE 下（保持相对层级）
    READONLY_MODE=strict   → 数据根内的路径抛 ReadOnlyError
    """
    p = Path(path)
    if READONLY_MODE == "off" or not _is_under(p, DATA_DIR):
        return p
    if READONLY_MODE == "strict":
        raise ReadOnlyError(
            f"INDUSTRY_RADAR_READONLY=strict：拒绝写入共享数据根 {p}\n"
            f"  （本机为只读副本；确需写入请 unset 该变量，或改用 =1 重定向到沙箱）")
    rel = p.resolve().relative_to(Path(DATA_DIR).resolve())
    out = OUTPUT_OVERRIDE / rel
    if str(p) not in _REDIRECT_NOTIFIED:
        _REDIRECT_NOTIFIED.add(str(p))
        print(f"[只读重定向] {p} → {out}")
    return out


# ── 富途 OpenD 连接 ──
FUTU_HOST, FUTU_PORT = resolve_futu()
