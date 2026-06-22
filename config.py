"""
config.py — Centralised configuration for Capitol Gains.

Reads environment variables (via python-dotenv), validates required values
on import, exposes typed constants, and configures the loguru logger with
automatic log rotation.

Import this module before anything else that needs settings::

    from config import settings, configure_logging
    configure_logging()
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# ---------------------------------------------------------------------------
# Load .env (no-op if already loaded or file missing)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(_PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------

DATA_DIR: Path = _PROJECT_ROOT / "data"
LOG_DIR: Path = _PROJECT_ROOT / "logs"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

TICKER_OVERRIDES_PATH: Path = DATA_DIR / "ticker_overrides.json"


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Settings:
    """
    Immutable application settings resolved from environment variables.

    All values are read once at import time.  If a required value is
    missing the class raises a descriptive RuntimeError rather than failing
    later with a cryptic KeyError.
    """

    # ------------------------------------------------------------------ #
    # Discord
    # ------------------------------------------------------------------ #
    discord_token: str
    discord_channel_id: int
    discord_report_channel_id: int

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    database_url: str

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    log_level: str

    # ------------------------------------------------------------------ #
    # External APIs
    # ------------------------------------------------------------------ #
    quiver_api_key: str
    fmp_api_key: str
    alpha_vantage_key: str

    # ------------------------------------------------------------------ #
    # Scheduler timing
    # ------------------------------------------------------------------ #
    daily_job_hour: int          # 0–23 UTC
    weekly_job_day: int          # 0=Monday … 6=Sunday

    # ------------------------------------------------------------------ #
    # Alert thresholds
    # ------------------------------------------------------------------ #
    alert_score_threshold: float
    buy_cluster_min_politicians: int
    buy_cluster_window_days: int

    # ------------------------------------------------------------------ #
    # Derived paths (not from env)
    # ------------------------------------------------------------------ #
    project_root: Path = field(default=_PROJECT_ROOT)
    data_dir: Path = field(default=DATA_DIR)
    log_dir: Path = field(default=LOG_DIR)
    ticker_overrides_path: Path = field(default=TICKER_OVERRIDES_PATH)

    # ------------------------------------------------------------------ #
    # Convenience properties
    # ------------------------------------------------------------------ #

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return "postgresql" in self.database_url or "postgres" in self.database_url

    @property
    def debug_mode(self) -> bool:
        return self.log_level.upper() == "DEBUG"


# ---------------------------------------------------------------------------
# Factory — read env vars with defaults and validation
# ---------------------------------------------------------------------------

def _require(key: str) -> str:
    """Return env var value or raise a clear RuntimeError."""
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(
            f"[config] Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in your values."
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"[config] Environment variable '{key}' must be an integer, got: {raw!r}"
        )


def _float(key: str, default: float) -> float:
    raw = os.getenv(key, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(
            f"[config] Environment variable '{key}' must be a float, got: {raw!r}"
        )


def _build_settings() -> Settings:
    """Construct and validate the Settings singleton."""

    # Required
    discord_token = _require("DISCORD_TOKEN")

    channel_raw = _require("DISCORD_CHANNEL_ID")
    try:
        discord_channel_id = int(channel_raw)
    except ValueError:
        raise RuntimeError(
            f"[config] DISCORD_CHANNEL_ID must be an integer, got: {channel_raw!r}"
        )

    # Optional with sensible defaults
    report_channel_raw = _optional("DISCORD_REPORT_CHANNEL_ID", channel_raw)
    discord_report_channel_id = int(report_channel_raw)

    database_url = _optional(
        "DATABASE_URL", f"sqlite:///{DATA_DIR / 'capitol_gains.db'}"
    )

    log_level = _optional("LOG_LEVEL", "INFO").upper()
    _valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in _valid_levels:
        raise RuntimeError(
            f"[config] LOG_LEVEL must be one of {_valid_levels}, got: {log_level!r}"
        )

    # API keys — optional so tests can run without real keys
    quiver_api_key = _optional("QUIVER_API_KEY", "")
    alpha_vantage_key = _optional("ALPHA_VANTAGE_KEY", "")
    fmp_api_key = _optional("FMP_API_KEY", "")

    daily_job_hour = _int("DAILY_JOB_HOUR", 7)
    if not 0 <= daily_job_hour <= 23:
        raise RuntimeError(
            f"[config] DAILY_JOB_HOUR must be 0–23, got: {daily_job_hour}"
        )

    weekly_job_day = _int("WEEKLY_JOB_DAY", 0)
    if not 0 <= weekly_job_day <= 6:
        raise RuntimeError(
            f"[config] WEEKLY_JOB_DAY must be 0–6 (Mon–Sun), got: {weekly_job_day}"
        )

    alert_score_threshold = _float("ALERT_SCORE_THRESHOLD", 70.0)
    buy_cluster_min_politicians = _int("BUY_CLUSTER_MIN_POLITICIANS", 3)
    buy_cluster_window_days = _int("BUY_CLUSTER_WINDOW_DAYS", 30)

    return Settings(
        discord_token=discord_token,
        discord_channel_id=discord_channel_id,
        discord_report_channel_id=discord_report_channel_id,
        database_url=database_url,
        log_level=log_level,
        quiver_api_key=quiver_api_key,
        alpha_vantage_key=alpha_vantage_key,
        fmp_api_key=fmp_api_key,
        daily_job_hour=daily_job_hour,
        weekly_job_day=weekly_job_day,
        alert_score_threshold=alert_score_threshold,
        buy_cluster_min_politicians=buy_cluster_min_politicians,
        buy_cluster_window_days=buy_cluster_window_days,
    )


# Module-level singleton — validated once at import time
settings: Settings = _build_settings()


# ---------------------------------------------------------------------------
# Loguru configuration
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    """
    Configure loguru with:
      • Coloured stderr output at the configured log level
      • Rotating file handler (10 MB per file, 30-day retention)
      • Separate error-only log file for easy alerting

    Call this once at application startup (bot.py) before any other
    module emits log messages.
    """
    # Remove the default loguru handler
    logger.remove()

    # --- stderr (coloured) ----------------------------------------------- #
    logger.add(
        sys.stderr,
        level=settings.log_level,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        backtrace=True,
        diagnose=settings.debug_mode,
    )

    # --- rotating info log ------------------------------------------------- #
    logger.add(
        LOG_DIR / "capitol_gains_{time:YYYY-MM-DD}.log",
        level=settings.log_level,
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
            "{name}:{function}:{line} | {message}"
        ),
        backtrace=True,
        diagnose=settings.debug_mode,
        enqueue=True,   # thread-safe async logging
    )

    # --- error-only log ---------------------------------------------------- #
    logger.add(
        LOG_DIR / "errors.log",
        level="ERROR",
        rotation="5 MB",
        retention="90 days",
        compression="gz",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
            "{name}:{function}:{line} | {message}"
        ),
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )

    logger.info(
        "Logging configured — level={} project_root={}",
        settings.log_level,
        settings.project_root,
    )


# ---------------------------------------------------------------------------
# Scoring weights (centralised so the scoring engine and tests share them)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """
    Point values for each scoring signal.

    Centralised here so weights can be tuned in one place and referenced
    consistently by both the scoring engine and the test suite.
    """
    points_per_unique_buyer: float = 5.0
    points_per_net_buy: float = 3.0
    repeat_purchase_bonus: float = 5.0
    sector_momentum_bonus: float = 10.0
    large_transaction_bonus: float = 5.0

    # Raw score ceiling before normalisation to 0–100
    max_raw_score: float = 100.0


SCORING_WEIGHTS = ScoringWeights()


# ---------------------------------------------------------------------------
# Amount range parsing map
# ---------------------------------------------------------------------------

# STOCK Act mandates disclosure in these dollar ranges.
# Maps the textual range to (min, max) in USD.
AMOUNT_RANGE_MAP: dict[str, tuple[float, float]] = {
    "$1,001 - $15,000":       (1_001,    15_000),
    "$15,001 - $50,000":      (15_001,   50_000),
    "$50,001 - $100,000":     (50_001,  100_000),
    "$100,001 - $250,000":   (100_001,  250_000),
    "$250,001 - $500,000":   (250_001,  500_000),
    "$500,001 - $1,000,000": (500_001, 1_000_000),
    "$1,000,001 - $5,000,000": (1_000_001, 5_000_000),
    "Over $5,000,000":        (5_000_001, 25_000_000),
}


# ---------------------------------------------------------------------------
# Sector colour map for Discord embeds
# ---------------------------------------------------------------------------

SECTOR_COLOURS: dict[str, int] = {
    "Technology":             0x7289DA,
    "Healthcare":             0x2ECC71,
    "Financials":             0xF1C40F,
    "Energy":                 0xE67E22,
    "Consumer Discretionary": 0xE91E63,
    "Consumer Staples":       0x9B59B6,
    "Industrials":            0x3498DB,
    "Materials":              0x1ABC9C,
    "Real Estate":            0xE74C3C,
    "Utilities":              0x95A5A6,
    "Communication Services": 0x5865F2,
    "Unknown":                0x99AAB5,
}

DEFAULT_EMBED_COLOUR: int = 0x2F3136
