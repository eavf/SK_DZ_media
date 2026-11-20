"""AI návrh relevancie článku k Slovensku / vzťahom SK–DZ.

Po extrakcii (extract_bulk.py, refetch_article.py) pošle titulok, snippet a výsek textu do LLM
(NVIDIA API, OpenAI-kompatibilné, rovnaký NVIDIA_API_KEY ako AI filter športu) a uloží NÁVRH:

  ai_relevance        1 = relevantné, 0 = nerelevantné (NULL = ešte neposúdené)
  ai_relevance_conf   istota modelu 0–100
  ai_relevance_reason krátke zdôvodnenie
  ai_relevance_at     kedy sa posudzovalo (každý článok len raz)

Návrh NIKDY nemení `relevance` ani `deleted_at` — rozhoduje človek (zobrazí sa v zozname a v detaile).
Posudzujú sa len aktívne, ručne neoznačené články s textom; domény z AI_RELEVANCE_SKIP_DOMAINS
(default mfa.gov.dz vrátane emb*.mfa.gov.dz — extrahovaný text je tam menu webu, meranie 11/2026:
zhoda 38 %) sa preskakujú. Mimo nich mal model Nemotron 3 Ultra zhodu 95 % s ručným označením
(eval_ai_relevance.py, 232 článkov).

Premenné (.env):
  AI_RELEVANCE_ENABLED       1 (default) / 0
  AI_RELEVANCE_MODEL         default nvidia/nemotron-3-ultra-550b-a55b
  AI_RELEVANCE_SKIP_DOMAINS  čiarkou oddelené domény (aj subdomény), default mfa.gov.dz
  AI_RELEVANCE_BATCH         koľko starších článkov dobehne extract_bulk za beh (default 20; ~10–25 s/článok)
  NVIDIA_API_KEY, NVIDIA_BASE_URL, AI_CLASSIFY_DELAY — spoločné s classify_ai.py

Kým migrácia 004 nie je spustená, posudzovanie sa ticho vypne (stĺpce chýbajú).

Použitie (dobehnutie starších článkov, default dry-run):
    python ai_relevance.py [--apply] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

import requests
from sqlalchemy import text

from classify_ai import ApiUnavailable, ClassificationStopped, RateLimited, _api_key, _base_url, _delay
from config.config import get_db_engine

logger = logging.getLogger("ai_relevance")

DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
DEFAULT_SKIP_DOMAINS = "mfa.gov.dz"
REQUEST_TIMEOUT = 120          # veľký model odpovedá ~5–15 s, občas dlhšie
MAX_TOKENS = 1500              # niektoré modely premýšľajú aj s vypnutým premýšľaním
MAX_SNIPPET_CHARS = 1500
MAX_CONSECUTIVE_FAILURES = 3

SYSTEM_PROMPT = (
    "You help the Slovak embassy in Algeria monitor Algerian media. Decide whether a news article is RELEVANT, "
    "i.e. it contains real information concerning Slovakia (Slovaquie, سلوفاكيا).\n"
    "Answer ONLY with JSON: {\"relevant\": true|false, \"confidence\": 0-100, \"reason\": \"<max 15 words>\"}.\n"
    "relevant=true when the text says something about Slovakia, even briefly or at the end of the article: "
    "Slovak–Algerian relations (visits, meetings, agreements, trade, cooperation, embassy activities), "
    "Slovak officials acting or speaking, Slovakia's position in an EU or international decision, "
    "Slovak companies or citizens, Slovak politics.\n"
    "relevant=false: sports (matches, results, qualifications, athletes); advertisements, travel offers, job "
    "postings; pages without real content (only a title or a photo, an empty text, a generic embassy homepage "
    "or contact page); Algerian elections and the organisation of diaspora electoral constituencies or seats "
    "abroad, even when Slovakia is listed among the countries of a zone; and when the matching word means "
    "something else (a person's name, another country).\n"
    "Rules of precedence: sports ALWAYS means relevant=false, also when Slovakia or a Slovak athlete or club "
    "is listed among participants, groups, fixtures or results. "
    "If the text consists only of website navigation (menus, section lists, links, contact details) or headlines "
    "of other news, treat it as a page without real content; a mention of Slovakia only in such parts does not count.\n"
    "Decide from the article text when it is given; title and snippet alone can be misleading. "
    "The article may be in French, Arabic or English."
)

# výseky textu okolo zmienky o Slovensku (zmienka býva aj v poslednom odseku)
SLOVAK_RE = re.compile(r"slovaqu|slovak|bratislav|سلوفاك|fico|فيتسو|blan[aá]r|بلانار", re.I)
TEXT_HEAD_CHARS = 1500
TEXT_WINDOW_CHARS = 500
TEXT_MAX_WINDOWS = 6

_JSON_RE = re.compile(r"\{.*?\}", re.S)
_consecutive_failures = 0
_columns_ready: bool | None = None


# ── nastavenia ────────────────────────────────────────────────────────────────

def _model() -> str:
    return os.getenv("AI_RELEVANCE_MODEL", "").strip() or DEFAULT_MODEL


def skip_domains() -> list[str]:
    raw = os.getenv("AI_RELEVANCE_SKIP_DOMAINS")
    raw = DEFAULT_SKIP_DOMAINS if raw is None else raw
    return [d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip()]


def is_skipped_domain(domain: str | None) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    return any(d == s or d.endswith("." + s) for s in skip_domains())


def ai_relevance_batch() -> int:
    """Koľko starších článkov dobehne extract_bulk v jednom behu (AI_RELEVANCE_BATCH, default 20)."""
    try:
        return max(0, int(os.getenv("AI_RELEVANCE_BATCH", "20")))
    except ValueError:
        return 20


def is_enabled() -> bool:
    enabled = os.getenv("AI_RELEVANCE_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    return enabled and bool(_api_key())


def columns_ready(conn) -> bool:
    """Existujú stĺpce z migrácie 004? Bez nich sa posudzovanie preskočí.

    Pamätá si len kladný výsledok — webová aplikácia tak migráciu zachytí aj bez reštartu.
    """
    global _columns_ready
    if _columns_ready:
        return True
    n = conn.execute(text("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'articles' AND COLUMN_NAME = 'ai_relevance_at'
    """)).scalar()
    if n:
        _columns_ready = True
    elif _columns_ready is None:
        _columns_ready = False
        logger.warning("AI relevancia: chýbajú stĺpce (migrations/004_ai_relevance.sql) — preskakujem.")
    return bool(n)


# ── správa pre model ──────────────────────────────────────────────────────────

def text_excerpt(body: str) -> str:
    """Začiatok textu + okolie každej zmienky o Slovensku (bez prekrývania), aby sa zmestil aj dlhý článok."""
    body = (body or "").strip()
    if len(body) <= TEXT_HEAD_CHARS + 2 * TEXT_WINDOW_CHARS:
        return body
    parts, last_end = [body[:TEXT_HEAD_CHARS]], TEXT_HEAD_CHARS
    for m in SLOVAK_RE.finditer(body, TEXT_HEAD_CHARS):
        if len(parts) > TEXT_MAX_WINDOWS:
            break
        start = max(m.start() - TEXT_WINDOW_CHARS, last_end)
        end = min(m.end() + TEXT_WINDOW_CHARS, len(body))
        if start >= end:
            continue
        parts.append(("[…] " if start > last_end else "") + body[start:end])
        last_end = end
    return " ".join(parts) + (" […]" if last_end < len(body) else "")


def build_user_msg(row: dict) -> str:
    """row: domain, title, snippet, voliteľne text (bez kľúča 'text' len titulok + snippet)."""
    msg = (f"Source domain: {row.get('domain') or ''}\n"
           f"Title: {row.get('title') or ''}\n\n"
           f"Snippet:\n{(row.get('snippet') or '')[:MAX_SNIPPET_CHARS]}")
    if "text" in row:
        body = (row.get("text") or "").strip()
        msg += (f"\n\nArticle text ({len(body)} chars):\n{text_excerpt(body)}" if body
                else "\n\nArticle text: (empty — no text could be extracted)")
    return msg


def parse_answer(answer: str) -> tuple[bool, int | None, str]:
    """JSON odpoveď modelu → (relevant, confidence, reason). Pri neplatnej odpovedi ValueError."""
    m = _JSON_RE.search(answer or "")
    if not m:
        raise ValueError(f"AI nevrátilo JSON: {(answer or '')[:200]!r}")
    data = json.loads(m.group(0))
    if not isinstance(data.get("relevant"), bool):
        raise ValueError(f"neplatná hodnota 'relevant': {answer[:200]!r}")
    conf = data.get("confidence")
    conf = max(0, min(100, int(conf))) if isinstance(conf, (int, float)) else None
    return data["relevant"], conf, str(data.get("reason") or "").strip()


# ── volanie API ───────────────────────────────────────────────────────────────

def assess(row: dict, model: str | None = None) -> tuple[bool, int | None, str]:
    """Jedno volanie modelu. 429 → RateLimited, ostatné chyby ako výnimka."""
    resp = requests.post(
        f"{_base_url()}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"},
        json={
            "model": model or _model(),
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": build_user_msg(row)}],
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            # reasoning modely (Nemotron) by inak minuli tokeny na premýšľanie
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 429:
        raise RateLimited("NVIDIA API rate limit (429)")
    resp.raise_for_status()
    return parse_answer(resp.json()["choices"][0]["message"]["content"] or "")


def _assess_counted(row: dict) -> tuple[bool, int | None, str]:
    """assess() s počítadlom zlyhaní za sebou — po MAX_CONSECUTIVE_FAILURES vyhodí ApiUnavailable."""
    global _consecutive_failures
    try:
        result = assess(row)
    except RateLimited:
        raise
    except Exception as e:
        _consecutive_failures += 1
        if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise ApiUnavailable(f"{_consecutive_failures} zlyhaní za sebou, posledné: {e}") from e
        raise
    _consecutive_failures = 0
    return result


# ── DB ────────────────────────────────────────────────────────────────────────

_ARTICLE_SQL = """
    SELECT a.id, a.title, a.snippet, a.content_text AS text, a.relevance, a.deleted_at,
           a.ai_relevance_at, s.domain
    FROM articles a JOIN sources s ON s.id = a.source_id
"""


def save_assessment(conn, article_id: int, relevant: bool, conf: int | None, reason: str) -> None:
    conn.execute(text("""
        UPDATE articles
        SET ai_relevance = :rel, ai_relevance_conf = :conf, ai_relevance_reason = :reason, ai_relevance_at = NOW()
        WHERE id = :id
    """), {"id": article_id, "rel": int(relevant), "conf": conf, "reason": reason[:255]})


def _eligible(row) -> bool:
    return (row is not None and row["ai_relevance_at"] is None and row["relevance"] is None
            and row["deleted_at"] is None and bool((row["text"] or "").strip())
            and not is_skipped_domain(row["domain"]))


def assess_and_save(article_id: int) -> int | None:
    """Posúdi jeden článok (ak sa má) a uloží návrh. Vracia 1/0, alebo None ak sa neposudzoval.

    Neposudzuje: vypnuté/bez migrácie, už posúdené, ručne označené, zmazané, bez textu, preskočené domény.
    Bežná chyba API sa zaloguje (článok sa skúsi neskôr cez assess_pending); ClassificationStopped
    prepúšťa volajúcemu, aby v behu prestal volať API.
    """
    if not is_enabled():
        return None
    engine = get_db_engine()
    with engine.begin() as conn:
        if not columns_ready(conn):
            return None
        row = conn.execute(text(_ARTICLE_SQL + " WHERE a.id = :id"), {"id": article_id}).mappings().fetchone()
    if not _eligible(row):
        return None
    try:
        relevant, conf, reason = _assess_counted(dict(row))
    except ClassificationStopped:
        raise
    except Exception as e:
        logger.warning("AI relevancia zlyhala pre id=%s: %s", article_id, e)
        return None
    with engine.begin() as conn:
        save_assessment(conn, article_id, relevant, conf, reason)
    logger.info("AI relevancia id=%s → %s (%s) %s", article_id, int(relevant), conf, reason)
    return int(relevant)


def assess_pending(limit: int = 50, apply: bool = True) -> dict:
    """Dobehne návrhy pre aktívne, neoznačené, ešte neposúdené články s textom (najnovšie prvé).

    apply=False: zavolá AI a vypíše výsledok, nič nezapíše. Vracia {'relevant', 'irrelevant', 'failed'}.
    """
    counts = {"relevant": 0, "irrelevant": 0, "failed": 0}
    if not is_enabled():
        logger.info("AI relevancia je vypnutá (NVIDIA_API_KEY / AI_RELEVANCE_ENABLED).")
        return counts
    engine = get_db_engine()
    with engine.begin() as conn:
        if not columns_ready(conn):
            return counts
        rows = conn.execute(text(_ARTICLE_SQL + """
            WHERE a.ai_relevance_at IS NULL AND a.relevance IS NULL AND a.deleted_at IS NULL
              AND s.is_avoided = 0 AND a.content_text IS NOT NULL AND a.content_text <> ''
            ORDER BY a.id DESC
        """)).mappings().fetchall()
    rows = [r for r in rows if not is_skipped_domain(r["domain"])][:limit]
    if not rows:
        return counts

    logger.info("AI relevancia: %s článkov (apply=%s)", len(rows), apply)
    for i, row in enumerate(rows):
        if i:
            time.sleep(_delay())
        try:
            if apply:
                res = assess_and_save(row["id"])
            else:
                relevant, conf, reason = _assess_counted(dict(row))
                res = int(relevant)
                logger.info("[DRY-RUN] id=%s → %s (%s) %s | %s", row["id"], res, conf, reason, (row["title"] or "")[:80])
        except ClassificationStopped as e:
            logger.warning("AI relevancia zastavená (%s) — zvyšok pri ďalšom behu.", e)
            counts["failed"] += 1
            break
        except Exception as e:
            logger.warning("AI relevancia zlyhala pre id=%s: %s", row["id"], e)
            res = None
        counts["relevant" if res == 1 else "irrelevant" if res == 0 else "failed"] += 1
    logger.info("AI relevancia hotovo: relevant=%s, irrelevant=%s, failed=%s",
                counts["relevant"], counts["irrelevant"], counts["failed"])
    return counts


def main() -> int:
    from config.config import init_cli
    ap = argparse.ArgumentParser(description="Dobehnutie AI návrhov relevancie (default dry-run).")
    ap.add_argument("--apply", action="store_true", help="zapísať návrhy do DB")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    init_cli("ai_relevance")
    assess_pending(limit=args.limit, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
