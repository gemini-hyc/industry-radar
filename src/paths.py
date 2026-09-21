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
REPORTS_DIR = DATA_DIR / "reports" / "daily-analysis"  # 日报输出
STOCK_BASIC_PATH = DATA_DIR / "stock_basic.parquet"  # 股票列表

# ── 富途 OpenD 连接 ──
FUTU_HOST, FUTU_PORT = resolve_futu()
