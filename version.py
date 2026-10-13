"""
version.py — Verzia bežiacej aplikácie (git commit + čas buildu).

V Docker image sa hodnoty nastavujú pri builde (deploy.sh → --build-arg → ENV):
  - GIT_COMMIT  — krátky hash commitu, s príponou "-dirty" ak boli necommitnuté zmeny
  - BUILD_DATE  — čas buildu (ISO 8601)
Lokálne (python app.py) sa commit zistí priamo z gitu, BUILD_DATE chýba.

Použitie:
    from version import get_version
    v = get_version()   # {"commit", "build_date", "started_at", "source", "repo_url"}
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

REPO_URL = "https://github.com/eavf/SK_DZ_media"

_STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M")
_CACHED: dict | None = None


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=Path(__file__).parent,
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout.strip()


def _commit_from_git() -> str | None:
    try:
        commit = _git("rev-parse", "--short", "HEAD")
        if _git("status", "--porcelain", "--untracked-files=no"):
            commit += "-dirty"
        return commit
    except Exception:
        return None


def get_version() -> dict:
    global _CACHED
    if _CACHED is None:
        commit = os.getenv("GIT_COMMIT", "").strip()
        source = "image"
        if not commit:
            commit = _commit_from_git() or "unknown"
            source = "git" if commit != "unknown" else "unknown"
        _CACHED = {
            "commit": commit,
            "build_date": os.getenv("BUILD_DATE", "").strip() or None,
            "started_at": _STARTED_AT,
            "source": source,
            "repo_url": REPO_URL,
        }
    return _CACHED


def version_string() -> str:
    v = get_version()
    s = f"v {v['commit']}"
    if v["build_date"]:
        s += f" · build {v['build_date']}"
    return s
