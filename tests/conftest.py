"""
conftest.py — bootstrap fake Settings before any application module is imported.

All module-level init code (get_settings(), init_context(), configure_root_logging())
is intercepted here so test files can import app modules safely.
"""
from __future__ import annotations

import atexit
import logging
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# ── 1. Shared temp directory for the whole test session ──────────────────────
_TMP = Path(tempfile.mkdtemp(prefix="sk_dz_test_"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

for _d in [
    _TMP / "logs",
    _TMP / "bundle" / "runs",
    _TMP / "bundle" / "debug_html",
    _TMP / "bundle" / "latest",
]:
    _d.mkdir(parents=True, exist_ok=True)


# ── 2. Fake Settings injected into config._CACHED before app modules load ────
import config.config as _cfg  # noqa: E402 – must come after _TMP setup

_fake_paths = MagicMock()
_fake_paths.project_root = _TMP
_fake_paths.config_dir = _TMP / "config"
_fake_paths.bundle_dir = _TMP / "bundle"
_fake_paths.runs_dir = _TMP / "bundle" / "runs"
_fake_paths.latest_dir = _TMP / "bundle" / "latest"
_fake_paths.latest_bundle_path = _TMP / "bundle" / "latest" / "news_bundle.json"
_fake_paths.log_dir = _TMP / "logs"

_fake_settings = MagicMock()
_fake_settings.paths = _fake_paths
_fake_settings.bundle_dir = _TMP / "bundle"
_fake_settings.runs_dir = _TMP / "bundle" / "runs"
_fake_settings.log_dir = _TMP / "logs"
_fake_settings.log_level = "WARNING"
_fake_settings.log_max_bytes = 1024 * 1024
_fake_settings.log_backup_count = 1
_fake_settings.log_file = "test.log"
_fake_settings.preferred_domains = {"aps.dz", "tsa-algerie.com"}
_fake_settings.source_rank = {"aps.dz": 100, "tsa-algerie.com": 90}
_fake_settings.topic_keywords = {"diplomacy": ["Slovakia", "Bratislava"]}
_fake_settings.drop_query_keys = {"utm_source", "utm_medium", "fbclid"}
_fake_settings.extract_max = 50
_fake_settings.extract_only_missing = True
_fake_settings.serpapi_key = "test-serpapi-key"

# Inject before test modules trigger app module imports
_cfg._CACHED = _fake_settings


# ── 3. Patch configure_root_logging so it never touches the real filesystem ──
def _noop_configure_root_logging(settings, *, name: str = "cli") -> logging.Logger:
    return logging.getLogger(name)


_cfg.configure_root_logging = _noop_configure_root_logging
