"""
足球数据采集系统 — 统一路径配置
所有数据路径统一指向 D:/FootballData
"""
from pathlib import Path

# ── 根目录 ─────────────────────────────────────────────────────────
DATA_ROOT = Path("D:/FootballData")

# ── 子目录 ─────────────────────────────────────────────────────────
RAW_DATA_DIR    = DATA_ROOT / "raw_data"
DATABASE_DIR    = DATA_ROOT / "database"
SNAPSHOTS_DIR   = DATA_ROOT / "snapshots"
EXPORTS_DIR     = DATA_ROOT / "exports"
MODELS_DIR      = DATA_ROOT / "models"

# PostgreSQL 连接 (仍由环境变量 DATABASE_URL 控制)
# SQLite 数据库统一放在 DATABASE_DIR

# ── SQLite 数据库路径 ──────────────────────────────────────────────
DB_FOOTBALL_HISTORY    = DATABASE_DIR / "football_history.db"
DB_TIME_SERIES         = DATABASE_DIR / "time_series.db"
DB_LIVE_ODDS           = DATABASE_DIR / "live_odds.db"
DB_LIVE_PREDICTIONS    = DATABASE_DIR / "live_predictions.db"
DB_PREDICTION_SNAPSHOTS = DATABASE_DIR / "prediction_snapshots.db"
DB_RECOMMENDATION      = DATABASE_DIR / "recommendation_filters.db"
DB_CLV_TRACKING        = DATABASE_DIR / "clv_tracking.db"

# ── 项目内路径 (非数据文件，保持在项目内) ──────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"
CONFIGS_DIR = PROJECT_ROOT / "configs"

# ── 确保目录存在 ───────────────────────────────────────────────────
for _dir in [RAW_DATA_DIR, DATABASE_DIR, SNAPSHOTS_DIR, EXPORTS_DIR, MODELS_DIR]:
    _dir.mkdir(parents=True, exist_ok=True)
