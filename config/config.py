# config.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# -----------------------------------------------------------------------------
# Paths (this file lives in: <project_root>/config/config.py)
# -----------------------------------------------------------------------------
CONFIG_DIR = Path(__file__).resolve().parent          # .../<project_root>/config
PROJECT_ROOT = CONFIG_DIR.parent                      # .../<project_root>

BUNDLE_DIR = PROJECT_ROOT / "bundle"
RUNS_DIR = BUNDLE_DIR / "runs"
LOGS_DIR_DEFAULT = PROJECT_ROOT / "logs"

BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def load_json(path: Path, default: Any):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logging.getLogger("config").warning(f"Failed to load JSON: {path} ({e})")
    return default


def _to_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    v = val.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _to_int(val: Optional[str], default: int) -> int:
    if val is None or not str(val).strip():
        return default
    try:
        return int(str(val).strip())
    except ValueError:
        return default


def _csv_set(val: Optional[str]) -> set[str]:
    if not val or not val.strip():
        return set()
    return {x.strip().lower() for x in val.split(",") if x.strip()}


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _normalize_domain_list(v: Any) -> set[str]:
    """
    Accept list/tuple/set of strings -> lowercased set
    """
    if not v:
        return set()
    if not isinstance(v, (list, tuple, set)):
        return set()
    out = set()
    for x in v:
        s = str(x).strip().lower()
        if s:
            out.add(s)
    return out


def _normalize_rank_map(v: Any) -> dict[str, int]:
    """
    Accept dict-like {domain: score} -> {domain(lower): int(score)}.
    Invalid items are ignored.
    """
    if not isinstance(v, dict):
        return {}
    out: dict[str, int] = {}
    for k, val in v.items():
        dk = str(k).strip().lower()
        if not dk:
            continue
        try:
            out[dk] = int(val)
        except Exception:
            continue
    return out


def _normalize_topics(v: Any) -> dict[str, list[str]]:
    """
    Accept dict-like {topic: [keywords...]} -> normalized dict.
    Non-list values are ignored.
    """
    if not isinstance(v, dict):
        return {}
    out: dict[str, list[str]] = {}
    for topic, words in v.items():
        if not isinstance(words, list):
            continue
        t = str(topic).strip().lower()
        if not t:
            continue
        cleaned = [str(w) for w in words if str(w).strip()]
        out[t] = cleaned
    return out


def _normalize_string_list(v: Any) -> set[str]:
    if not v or not isinstance(v, (list, tuple, set)):
        return set()
    out = set()
    for x in v:
        s = str(x).strip().lower()
        if s:
            out.add(s)
    return out


def init_context() -> tuple[Settings, ProjectPaths]:
    """
    Shared app/script context without reconfiguring root logging.
    Safe for Flask imports.
    """
    s = get_settings()
    return s, s.paths


def init_cli(name: str) -> tuple[Settings, ProjectPaths, logging.Logger]:
    """
    CLI initializer with root logging configuration.
    Use only from standalone scripts.
    """
    s = get_settings()
    logger = configure_root_logging(s, name=name)
    return s, s.paths, logger


# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectPaths:
    project_root: Path
    config_dir: Path

    bundle_dir: Path
    runs_dir: Path
    latest_dir: Path
    latest_bundle_path: Path

    log_dir: Path

# ---- Do budúcna ----
#    exports_dir: Path
#    cache_dir: Path
#    tmp_dir: Path


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    # --- Flask ---
    flask_port: int
    flask_secret_key: str

    # --- DB ---
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_pass: str

    # --- SerpAPI / search ---
    serpapi_key: str
    serp_hl: str
    serp_gl: str
    serp_num: int
    serp_when: str

    # --- Paths ---
    paths: ProjectPaths

    # --- Ingest to DZ ---
    drop_query_keys: set[str]

    # --- Extraction ---
    extract_max: int
    extract_only_missing: bool

    # --- Preferences (sources) ---
    preferred_domains: set[str]

    # --- JSON configs ---
    source_rank: dict[str, int]
    topic_keywords: dict[str, list[str]]

    # --- Logging ---
    log_file: str
    log_level: str
    log_max_bytes: int
    log_backup_count: int


_CACHED: Optional[Settings] = None


def get_settings(*, force_reload: bool = False, dotenv_path: Optional[str] = None) -> Settings:
    """
    Load settings from .env + environment + JSON configs in CONFIG_DIR.
    - Call once at startup, reuse via cache.
    - force_reload=True if you changed env during runtime.
    - dotenv_path lets you point to a specific .env file if needed.
    """
    global _CACHED
    if _CACHED is not None and not force_reload:
        return _CACHED

    load_dotenv(dotenv_path=dotenv_path)
    # .env.local prepíše hodnoty z .env (lokálny dev override)
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env.local", override=True)

    def _resolve_path_from_env(env_name: str, default_path: Path) -> Path:
        raw = os.getenv(env_name)
        if raw and raw.strip():
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            return p.resolve()
        return default_path.resolve()

    # -------------------------------------------------------------------------
    # Directories / bundle paths
    # -------------------------------------------------------------------------
    bundle_dir = _resolve_path_from_env("BUNDLE_DIR", BUNDLE_DIR)
    runs_dir = _resolve_path_from_env("RUNS_DIR", RUNS_DIR)
    log_dir = _resolve_path_from_env("LOG_DIR", LOGS_DIR_DEFAULT)

    _ensure_dir(bundle_dir)
    _ensure_dir(runs_dir)
    _ensure_dir(log_dir)

    latest_dir = bundle_dir / "latest"

    latest_bundle_env = os.getenv("LATEST_BUNDLE_PATH")
    if latest_bundle_env and latest_bundle_env.strip():
        p = Path(latest_bundle_env).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        latest_bundle_path = p.resolve()
    else:
        latest_bundle_path = (latest_dir / "news_bundle.json").resolve()

    logging.getLogger("config").info("Latest bundle path: %s", latest_bundle_path)

    paths = ProjectPaths(
        project_root=PROJECT_ROOT,
        config_dir=CONFIG_DIR,
        bundle_dir=bundle_dir,
        runs_dir=runs_dir,
        latest_dir=latest_dir,
        latest_bundle_path=latest_bundle_path,
        log_dir=log_dir,
    )

    # -------------------------------------------------------------------------
    # JSON configs
    # -------------------------------------------------------------------------
    sources_path = CONFIG_DIR / "sources.json"
    ranking_path = CONFIG_DIR / "ranking.json"
    topics_path = CONFIG_DIR / "topics.json"
    url_cleanup_path = CONFIG_DIR / "url_cleanup.json"

    sources_cfg = load_json(sources_path, {})
    ranking_cfg = load_json(ranking_path, {})
    topics_cfg = load_json(topics_path, {})
    url_cleanup_cfg = load_json(url_cleanup_path, {})

    preferred_from_json = _normalize_domain_list((sources_cfg or {}).get("preferred"))
    preferred_from_env = _csv_set(os.getenv("PREFERRED_DOMAINS"))
    preferred_domains = preferred_from_json or preferred_from_env

    drop_query_keys = _normalize_string_list((url_cleanup_cfg or {}).get("drop_query_keys"))

    source_rank = _normalize_rank_map(ranking_cfg)
    topic_keywords = _normalize_topics(topics_cfg)

    s = Settings(
        # Flask
        flask_port=_to_int(os.getenv("FLASK_PORT"), 5088),
        flask_secret_key=os.getenv("FLASK_SECRET_KEY", "").strip(),

        # DB
        db_host=os.getenv("DB_HOST", "127.0.0.1"),
        db_port=_to_int(os.getenv("DB_PORT"), 3306),
        db_name=os.getenv("DB_NAME", "dz_news"),
        db_user=os.getenv("DB_USER", "").strip(),
        db_pass=os.getenv("DB_PASS", "").strip(),

        # SerpAPI
        serpapi_key=os.getenv("SERPAPI_KEY", "").strip(),
        serp_hl=os.getenv("SERP_HL", "fr"),
        serp_gl=os.getenv("SERP_GL", "dz"),
        serp_num=_to_int(os.getenv("SERP_NUM"), 10),
        serp_when=os.getenv("SERP_WHEN", "7d").strip() or "7d",

        # Paths
        paths=paths,

        # URL normalization
        drop_query_keys=drop_query_keys,

        # Extraction
        extract_max=_to_int(os.getenv("EXTRACT_MAX"), 50),
        extract_only_missing=_to_bool(os.getenv("EXTRACT_ONLY_MISSING"), True),

        # Preferences
        preferred_domains=preferred_domains,

        # JSON configs
        source_rank=source_rank,
        topic_keywords=topic_keywords,

        # Logging
        log_file=os.getenv("LOG_FILE", "app.log").strip() or "app.log",
        log_level=os.getenv("LOG_LEVEL", "INFO").strip() or "INFO",
        log_max_bytes=_to_int(os.getenv("LOG_MAX_BYTES"), 5 * 1024 * 1024),
        log_backup_count=_to_int(os.getenv("LOG_BACKUP_COUNT"), 5),
    )

    _CACHED = s
    return s


def require(value: str, name: str) -> str:
    """
    Small helper for required fields in CLI scripts.
    """
    v = (value or "").strip()
    if not v:
        raise SystemExit(f"Missing {name} in .env / environment.")
    return v


def build_preferred_site_or(domains: Iterable[str]) -> str:
    d = sorted({x.strip().lower() for x in domains if x and x.strip()})
    if not d:
        raise ValueError("Preferred domains set is empty (sources.json preferred / PREFERRED_DOMAINS).")
    return "(" + " OR ".join(f"site:{x}" for x in d) + ")"


# -----------------------------------------------------------------------------
# DB ENGINE (shared)
# -----------------------------------------------------------------------------
_DB_ENGINE: Engine | None = None


def get_db_engine(*, force_new: bool = False) -> Engine:
    """
    Shared SQLAlchemy engine for whole project.
    Safe for Flask + CLI.
    """
    global _DB_ENGINE

    if _DB_ENGINE is not None and not force_new:
        return _DB_ENGINE

    s = get_settings()

    if not s.db_user:
        raise SystemExit("Missing DB_USER in .env / environment.")

    url = (
        f"mysql+pymysql://{s.db_user}:{s.db_pass}"
        f"@{s.db_host}:{s.db_port}/{s.db_name}"
        "?charset=utf8mb4"
    )

    _DB_ENGINE = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )
    return _DB_ENGINE


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
def configure_root_logging(settings: Settings, *, name: str = "cli") -> logging.Logger:
    """
    Configure logging for CLI scripts.
    - Logs to file (rotating) in settings.paths.log_dir
    - Also logs to stderr (handy for cron)
    Returns a named logger.
    """
    settings.paths.log_dir.mkdir(parents=True, exist_ok=True)
    prefix = os.getenv("LOG_PREFIX", "dznews")
    log_path = settings.paths.log_dir / f"{prefix}.{name}.log"

    level = getattr(logging, settings.log_level.upper(), logging.ERROR)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s in %(pathname)s:%(lineno)d"
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(fmt)

    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    return logging.getLogger(name)