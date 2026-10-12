"""
classify_ai.py — AI klasifikácia článkov: je to športové spravodajstvo?

Používa NVIDIA API (build.nvidia.com), ktoré je OpenAI-kompatibilné.

Konfigurácia (.env):
  - NVIDIA_API_KEY       — API kľúč z build.nvidia.com (bez kľúča je klasifikácia vypnutá)
  - NVIDIA_MODEL         — model (default meta/llama-3.2-11b-vision-instruct)
  - NVIDIA_BASE_URL      — default https://integrate.api.nvidia.com/v1
  - AI_CLASSIFY_ENABLED  — 0 = vypnuté aj keď je kľúč nastavený (default 1)
  - AI_CLASSIFY_DELAY    — min. sekúnd medzi volaniami (default 1.5, free tier má rate limit)

Športový článok:
  - ai_label='sport', ai_checked_at=NOW()
  - deleted_at=NOW() (soft delete), relevance=0, relevance_note='auto: sport (AI) – <dôvod>'
Ostatné:
  - ai_label='other', ai_checked_at=NOW()

Ak API zlyhá, článok ostáva neklasifikovaný (ai_checked_at IS NULL) a skúsi sa pri ďalšom behu
(classify_pending). Výpadok API teda nikdy nič nevyradí. Pri rate limite (429) alebo po
MAX_CONSECUTIVE_FAILURES zlyhaniach za sebou sa vyhodí ClassificationStopped a volajúci
klasifikáciu v danom behu ukončí — pipeline tak nečaká na nedostupné API.

Použitie:
    from classify_ai import classify_and_mark, classify_pending
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests
from sqlalchemy import text

from config.config import get_db_engine

logger = logging.getLogger("classify_ai")

MAX_TEXT_CHARS = 2000
REQUEST_TIMEOUT = 30           # sekúnd na jedno volanie API
MAX_CONSECUTIVE_FAILURES = 3   # po toľkých zlyhaniach za sebou sa klasifikácia v behu zastaví

_consecutive_failures = 0


# Env sa číta pri volaní (nie pri importe) — .env sa načíta až v init_context()/init_cli()
def _api_key() -> str:
    return os.getenv("NVIDIA_API_KEY", "").strip()


def _model() -> str:
    return os.getenv("NVIDIA_MODEL", "").strip() or "meta/llama-3.2-11b-vision-instruct"


def _base_url() -> str:
    return (os.getenv("NVIDIA_BASE_URL", "").strip() or "https://integrate.api.nvidia.com/v1").rstrip("/")


def _delay() -> float:
    try:
        return float(os.getenv("AI_CLASSIFY_DELAY", "1.5"))
    except ValueError:
        return 1.5

_SYSTEM_PROMPT = (
    "You classify news articles. Answer ONLY with JSON: "
    '{"sport": true|false, "reason": "<max 10 words>"}.\n'
    "sport=true if the main subject is sports news, i.e. what happens in sport itself: match reports, "
    "results, rankings, qualifications (e.g. a team qualifying for the World Cup), competitions and "
    "tournaments (announcements, participants, schedule), athletes, coaches, transfers, team line-ups.\n"
    "sport=false for everything else: politics, diplomacy, economy, culture, security, society. "
    "Also sport=false for statements or interviews of ministers and officials about sports policy, "
    "ambitions to host events or sports infrastructure; for sports diplomacy and government or ministry "
    "cooperation agreements in the field of sport; and for articles that mention sport only in passing.\n"
    "Decide from the main subject, not from single words. The \"sport\" value must be consistent with "
    "your reason.\n"
    "The article may be in French, Arabic or English."
)

_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


class ClassificationStopped(Exception):
    """Klasifikáciu treba v tomto behu ukončiť a zvyšok skúsiť neskôr."""


class RateLimited(ClassificationStopped):
    """NVIDIA API vrátilo 429."""


class ApiUnavailable(ClassificationStopped):
    """API zlyhalo MAX_CONSECUTIVE_FAILURES-krát za sebou (timeout, 5xx, nezmyselná odpoveď)."""


def is_enabled() -> bool:
    enabled = os.getenv("AI_CLASSIFY_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    return enabled and bool(_api_key())


def classify_sport(title: str | None, content: str | None) -> tuple[bool, str]:
    """Zavolá AI a vráti (is_sport, reason). Pri chybe vyhodí výnimku."""
    user_msg = f"Title: {title or ''}\n\nText:\n{(content or '')[:MAX_TEXT_CHARS]}"
    resp = requests.post(
        f"{_base_url()}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"},
        json={
            "model": _model(),
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0,
            "max_tokens": 80,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 429:
        raise RateLimited("NVIDIA API rate limit (429)")
    resp.raise_for_status()

    answer = resp.json()["choices"][0]["message"]["content"] or ""
    m = _JSON_RE.search(answer)
    if not m:
        raise ValueError(f"AI nevrátilo JSON: {answer[:200]!r}")
    data = json.loads(m.group(0))
    if not isinstance(data.get("sport"), bool):
        raise ValueError(f"AI vrátilo neplatnú hodnotu 'sport': {answer[:200]!r}")
    return data["sport"], str(data.get("reason") or "").strip()


def _call_ai(title: str | None, content: str | None) -> tuple[bool, str]:
    """classify_sport() s počítadlom zlyhaní za sebou.

    Bežnú chybu prepustí ďalej; po MAX_CONSECUTIVE_FAILURES zlyhaniach vyhodí ApiUnavailable.
    """
    global _consecutive_failures
    try:
        result = classify_sport(title, content)
    except RateLimited:
        raise
    except Exception as e:
        _consecutive_failures += 1
        if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise ApiUnavailable(f"{_consecutive_failures} zlyhaní za sebou, posledné: {e}") from e
        raise
    _consecutive_failures = 0
    return result


def mark_classified(conn, article_id: int, is_sport: bool, reason: str) -> None:
    """Zapíše výsledok klasifikácie. Športový článok soft-deletne a označí ako irelevantný."""
    if is_sport:
        note = f"auto: sport (AI) – {reason}" if reason else "auto: sport (AI)"
        conn.execute(text("""
            UPDATE articles
            SET ai_label       = 'sport',
                ai_checked_at  = NOW(),
                deleted_at     = COALESCE(deleted_at, NOW()),
                relevance      = 0,
                relevance_note = :note
            WHERE id = :id
        """), {"id": article_id, "note": note[:255]})
    else:
        conn.execute(text("""
            UPDATE articles
            SET ai_label = 'other', ai_checked_at = NOW()
            WHERE id = :id
        """), {"id": article_id})


def classify_and_mark(article_id: int, title: str | None, content: str | None) -> str | None:
    """Klasifikuje jeden článok a uloží výsledok.

    Vracia 'sport' | 'other', alebo None ak sa neklasifikovalo (vypnuté, už klasifikované,
    ručne označený ako relevantný, chyba API). ClassificationStopped prepúšťa volajúcemu.
    """
    if not is_enabled() or not content:
        return None

    engine = get_db_engine()
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT relevance, ai_checked_at FROM articles WHERE id = :id
        """), {"id": article_id}).mappings().fetchone()
    # Klasifikujeme len raz a nikdy neprepisujeme ručné rozhodnutie
    if not row or row["ai_checked_at"] is not None or row["relevance"] is not None:
        return None

    try:
        is_sport, reason = _call_ai(title, content)
    except ClassificationStopped:
        raise
    except Exception as e:
        logger.warning("AI klasifikácia zlyhala pre id=%s: %s", article_id, e)
        return None

    with engine.begin() as conn:
        mark_classified(conn, article_id, is_sport, reason)

    label = "sport" if is_sport else "other"
    logger.info("AI klasifikácia id=%s → %s (%s)", article_id, label, reason)
    return label


def classify_pending(limit: int = 50, apply: bool = True) -> dict:
    """Klasifikuje extrahované, neklasifikované a neoznačené články.

    apply=False: zavolá AI a vypíše výsledok, ale nič nezapíše (dry-run).
    Vracia počty {'sport', 'other', 'failed'}.
    """
    counts = {"sport": 0, "other": 0, "failed": 0}
    if not is_enabled():
        logger.info("AI klasifikácia je vypnutá (NVIDIA_API_KEY / AI_CLASSIFY_ENABLED).")
        return counts

    engine = get_db_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id,
                   COALESCE(title_fr, title)               AS title,
                   COALESCE(content_text_fr, content_text) AS content
            FROM articles
            WHERE ai_checked_at IS NULL
              AND relevance IS NULL
              AND deleted_at IS NULL
              AND extraction_ok = 1
              AND content_text IS NOT NULL
            ORDER BY id
            LIMIT :limit
        """), {"limit": limit}).mappings().fetchall()

    if not rows:
        logger.info("classify_pending: žiadne články na klasifikáciu.")
        return counts

    logger.info("classify_pending: %s článkov (apply=%s)", len(rows), apply)
    for i, row in enumerate(rows):
        if i:
            time.sleep(_delay())
        try:
            if apply:
                label = classify_and_mark(row["id"], row["title"], row["content"])
            else:
                is_sport, reason = _call_ai(row["title"], row["content"])
                label = "sport" if is_sport else "other"
                logger.info("[DRY-RUN] id=%s → %s (%s) | %s", row["id"], label, reason, (row["title"] or "")[:80])
        except ClassificationStopped as e:
            logger.warning("AI klasifikácia zastavená (%s) — zvyšné články sa klasifikujú pri ďalšom behu.", e)
            counts["failed"] += 1
            break
        except Exception as e:
            logger.warning("AI klasifikácia zlyhala pre id=%s: %s", row["id"], e)
            label = None

        counts[label or "failed"] += 1

    logger.info("AI klasifikácia hotovo: sport=%s, other=%s, failed=%s",
                counts["sport"], counts["other"], counts["failed"])
    return counts
