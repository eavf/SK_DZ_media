# config.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine

# -----------------------------------------------------------------------------
# Paths (this file lives in: <project_root>/config/config.py)
# -----------------------------------------------------------------------------
CONFIG_DIR = Path(__file__).resolve().parent          # .../<project_root>/config
PROJECT_ROOT = CONFIG_DIR.parent                      # .../<project_root>

BUNDLE_DIR = PROJECT_ROOT / "bundle"
RUNS_DIR = BUNDLE_DIR / "runs"
LOGS_DIR_DEFAULT = PROJECT_ROOT / "logs"

_log = logging.getLogger("config")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def load_json(path: Path, default: Any):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning(f"Failed to load JSON: {path} ({e})")
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


def _env_str(name: str, default: str = "") -> str:
    """Env value stripped of whitespace; empty/missing → default."""
    return (os.getenv(name) or "").strip() or default


def _env_path(name: str, default: Path) -> Path:
    """Path from env (relative paths resolve against PROJECT_ROOT); missing → default."""
    raw = _env_str(name)
    if not raw:
        return default.resolve()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _normalize_string_list(v: Any) -> set[str]:
    """
    Accept list/tuple/set of strings -> lowercased set (blank items skipped).
    """
    if not v or not isinstance(v, (list, tuple, set)):
        return set()
    return {s for s in (str(x).strip().lower() for x in v) if s}


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
        out[t] = [str(w) for w in words if str(w).strip()]
    return out


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

    search_terms_path: Path
    queries_path: Path

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

    # --- DeepL ---
    deepl_api_key: str

    # --- SerpAPI / search ---
    serpapi_key: str
    serp_hl: str
    serp_gl: str
    serp_num: int
    serp_when: str
    serp_engine: str

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
    search_terms: dict[str, list[str]]
    blocklist_domains: set[str]
    ecom_path_markers: set[str]
    bad_path_markers: set[str]
    title_bad_words: set[str]

    # --- Logging ---
    log_file: str
    log_level: str
    log_max_bytes: int
    log_backup_count: int
    log_prefix: str

    # --- SMTP notifikácie ---
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    notify_to: str


_CACHED: Optional[Settings] = None


def _load_env(dotenv_path: Optional[str]) -> None:
    load_dotenv(dotenv_path=dotenv_path)
    # .env.local prepíše hodnoty z .env (lokálny dev override); SKIP_ENV_LOCAL=1 → produkčné hodnoty z .env
    if not _to_bool(os.getenv("SKIP_ENV_LOCAL")):
        load_dotenv(dotenv_path=PROJECT_ROOT / ".env.local", override=True)


def _build_paths() -> ProjectPaths:
    bundle_dir = _ensure_dir(_env_path("BUNDLE_DIR", BUNDLE_DIR))
    runs_dir = _ensure_dir(_env_path("RUNS_DIR", RUNS_DIR))
    log_dir = _ensure_dir(_env_path("LOG_DIR", LOGS_DIR_DEFAULT))

    latest_dir = bundle_dir / "latest"
    latest_bundle_path = _env_path("LATEST_BUNDLE_PATH", latest_dir / "news_bundle.json")
    _log.debug("Latest bundle path: %s", latest_bundle_path)

    return ProjectPaths(
        project_root=PROJECT_ROOT,
        config_dir=CONFIG_DIR,
        bundle_dir=bundle_dir,
        runs_dir=runs_dir,
        latest_dir=latest_dir,
        latest_bundle_path=latest_bundle_path,
        log_dir=log_dir,
        search_terms_path=CONFIG_DIR / "search_terms.json",
        queries_path=CONFIG_DIR / "queries.json",
    )


def _load_json_configs(paths: ProjectPaths) -> dict[str, Any]:
    """Load JSON configs from CONFIG_DIR → kwargs for Settings."""
    sources_cfg = load_json(CONFIG_DIR / "sources.json", {}) or {}
    ranking_cfg = load_json(CONFIG_DIR / "ranking.json", {})
    topics_cfg = load_json(CONFIG_DIR / "topics.json", {})
    url_cleanup_cfg = load_json(CONFIG_DIR / "url_cleanup.json", {}) or {}
    search_terms_cfg = load_json(paths.search_terms_path, {})

    preferred_domains = (
        _normalize_string_list(sources_cfg.get("preferred"))
        or _csv_set(os.getenv("PREFERRED_DOMAINS"))
    )

    return dict(
        preferred_domains=preferred_domains,
        drop_query_keys=_normalize_string_list(url_cleanup_cfg.get("drop_query_keys")),
        blocklist_domains=_normalize_string_list(url_cleanup_cfg.get("blocklist_domains")),
        ecom_path_markers=_normalize_string_list(url_cleanup_cfg.get("ecom_path_markers")),
        bad_path_markers=_normalize_string_list(url_cleanup_cfg.get("bad_path_markers")),
        title_bad_words=_normalize_string_list(url_cleanup_cfg.get("title_bad_words")),
        source_rank=_normalize_rank_map(ranking_cfg),
        topic_keywords=_normalize_topics(topics_cfg),
        search_terms=_normalize_topics(search_terms_cfg),
    )


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

    _load_env(dotenv_path)
    paths = _build_paths()

    s = Settings(
        # Flask
        flask_port=_to_int(os.getenv("FLASK_PORT"), 5088),
        flask_secret_key=_env_str("FLASK_SECRET_KEY"),

        # DB
        db_host=_env_str("DB_HOST", "127.0.0.1"),
        db_port=_to_int(os.getenv("DB_PORT"), 3306),
        db_name=_env_str("DB_NAME", "dz_news"),
        db_user=_env_str("DB_USER"),
        db_pass=_env_str("DB_PASS"),

        # DeepL
        deepl_api_key=_env_str("DEEPL_API_KEY"),

        # SerpAPI
        serpapi_key=_env_str("SERPAPI_KEY"),
        serp_hl=_env_str("SERP_HL", "fr"),
        serp_gl=_env_str("SERP_GL", "dz"),
        serp_num=_to_int(os.getenv("SERP_NUM"), 10),
        serp_when=_env_str("SERP_WHEN", "7d"),
        serp_engine=_env_str("SERP_ENGINE"),

        # Paths
        paths=paths,

        # Extraction
        extract_max=_to_int(os.getenv("EXTRACT_MAX"), 50),
        extract_only_missing=_to_bool(os.getenv("EXTRACT_ONLY_MISSING"), True),

        # JSON configs (+ preferred domains, URL normalization)
        **_load_json_configs(paths),

        # Logging
        log_file=_env_str("LOG_FILE", "app.log"),
        log_level=_env_str("LOG_LEVEL", "INFO"),
        log_max_bytes=_to_int(os.getenv("LOG_MAX_BYTES"), 5 * 1024 * 1024),
        log_backup_count=_to_int(os.getenv("LOG_BACKUP_COUNT"), 5),
        log_prefix=_env_str("LOG_PREFIX", "dznews"),

        # SMTP
        smtp_host=_env_str("SMTP_HOST"),
        smtp_port=_to_int(os.getenv("SMTP_PORT"), 587),
        smtp_user=_env_str("SMTP_USER"),
        smtp_pass=_env_str("SMTP_PASS"),
        notify_to=_env_str("NOTIFY_TO"),
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


# -----------------------------------------------------------------------------
# Init entry points
# -----------------------------------------------------------------------------
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

    # URL.create escapes special characters (@ / : #) in user/password
    url = URL.create(
        "mysql+pymysql",
        username=require(s.db_user, "DB_USER"),
        password=s.db_pass,
        host=s.db_host,
        port=s.db_port,
        database=s.db_name,
        query={"charset": "utf8mb4"},
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
    log_dir = _ensure_dir(settings.paths.log_dir)
    log_path = log_dir / f"{settings.log_prefix}.{name}.log"

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

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
    stream_handler = logging.StreamHandler()

    for h in (file_handler, stream_handler):
        h.setLevel(level)
        h.setFormatter(fmt)
        root.addHandler(h)

    return logging.getLogger(name)
